// DSHWorker · cordis 插件入口（进程内 agents）
//
// 与 cli.mjs 的关系：
//   cli.mjs      独立进程入口（spawn/headless 调试用），保持不变；
//   index.js     常驻 DSH profile 的插件形态，直接用 ctx.agents / ctx.sessions
//                在**本进程内**建 agent、跑多轮，不再起 dsh 子进程。
//
// 两种形态共用同一套业务层（desktop-manager.mjs / mcp-business.mjs /
// router-client.mjs / feishu.mjs）：插件把自己实现的原生传输注册成
// `dsh-inprocess`，业务层照旧通过 interface/transport/index.mjs 的
// resolveNativeTransport() 取用，工具名与返回结构与 interface/transport/dsh.mjs
// 逐字段一致，所以业务层一行都不用改。
//
// 身份仍然来自 identity.json（bridge/name/display/port/configFile/stateDir），
// 端口/飞书名来自 ~/.hermes/secrets/dshworker.config.json。插件默认**不**启动
// HTTP + 飞书业务层（8906 通常被 CLI 常驻进程占着），要启用就显式配置
// `business: true`（或 DSHWORKER_BUSINESS=1）；启动前会探测端口，占用则跳过。
//
// 2026-11-10 实测结论（写在这里，免得下次又绕回 spawn）：
//   进程内 agents 报 `DeepSeek Messages transport failed / TRANSPORT` 的根因
//   不是 provider 选择，而是进程继承了 ~/.zprofile 里那台局域网代理
//   （HTTPS_PROXY=http://192.168.3.102:10808，EHOSTDOWN）。dsh 的 HTTP 客户端
//   在请求时读环境变量，所以整轮只会重试 5 次后 TRANSPORT 失败。用回环代理
//   （或直接不设代理）就能正常跑通多轮。见 config.proxy：只在显式配置时才
//   改写进程代理，避免悄悄动宿主进程的环境。
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { createHash, randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

import {
  registerNativeTransport,
  resetNativeTransportCache,
} from "./interface/transport/index.mjs";
import { normalizeSubscriptionConfig, taskBoardTerminalPhase, taskBoardText, taskBoardTitle } from "./router-tasks.mjs";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
export const name = "dshworker";
// 进程内驱动一轮对话只需要这三个服务；agent-loop 由 profile 负责提供
// factory（缺了会在 agents.create 时报 "no agent factory registered"）。
export const inject = ["agents", "sessions", "agentDefaultModel"];

const VERSION = "0.3.0";
const TRANSPORT_NAME = "dsh-inprocess";
const NAMESPACE = "dsh";
const STATE_FILE_NAME = "dsh-plugin-state.json";
const SELF_TEST_FILE = "self-test.json";
const DEFAULT_WORKSPACE = path.join(os.homedir(), "projects");
const DEFAULT_STATE_DIR = path.join(os.homedir(), ".hermes", "state", "dshworker");
const MAX_TURNS_PER_THREAD = 200;
const MAX_PERSISTED_TEXT = 8_000;
const DEFAULT_TURN_TIMEOUT_MS = 600_000;
const DEFAULT_WAIT_MS = 30_000;
const MAX_WAIT_MS = 180_000;
const POLL_INTERVAL_MS = 200;
const TERMINAL_TURN_STATUSES = new Set(["completed", "interrupted", "failed", "cancelled", "canceled"]);

const TOOL_DEFINITIONS = [
  {
    name: "create_thread",
    namespace: NAMESPACE,
    description: "Create an in-process DSH thread; `prompt` starts the first turn.",
    inputSchema: { type: "object", properties: {
      title: { type: "string" }, prompt: { type: "string" }, cwd: { type: "string" }, model: { type: "string" },
    }, required: [] },
  },
  {
    name: "list_threads",
    namespace: NAMESPACE,
    description: "List in-process DSH threads, newest first.",
    inputSchema: { type: "object", properties: {
      limit: { type: "number" }, searchTerm: { type: "string" },
    }, required: [] },
  },
  {
    name: "read_thread",
    namespace: NAMESPACE,
    description: "Read one thread's turns (oldest first).",
    inputSchema: { type: "object", properties: {
      threadId: { type: "string" }, turnLimit: { type: "number" },
      includeOutputs: { type: "boolean" }, maxOutputCharsPerItem: { type: "number" },
    }, required: ["threadId"] },
  },
  {
    name: "send_message_to_thread",
    namespace: NAMESPACE,
    description: "Queue one prompt on an existing thread; returns immediately with a turn id.",
    inputSchema: { type: "object", properties: {
      threadId: { type: "string" }, prompt: { type: "string" }, text: { type: "string" },
      message: { type: "string" }, input: { type: "array" },
    }, required: ["threadId"] },
  },
  {
    name: "set_thread_title",
    namespace: NAMESPACE,
    description: "Rename a thread (local only).",
    inputSchema: { type: "object", properties: {
      threadId: { type: "string" }, title: { type: "string" },
    }, required: ["threadId", "title"] },
  },
  {
    name: "wait_threads",
    namespace: NAMESPACE,
    description: "Poll thread progress until every target is idle or the timeout hits.",
    inputSchema: { type: "object", properties: {
      targets: { type: "array" }, threadIds: { type: "array" }, threadId: { type: "string" },
      timeoutMs: { type: "number" },
    }, required: [] },
  },
  {
    name: "list_projects",
    namespace: NAMESPACE,
    description: "List workspaces derived from the threads' cwd values.",
    inputSchema: { type: "object", properties: { limit: { type: "number" } }, required: [] },
  },
];

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function expandHome(value) {
  if (typeof value !== "string" || !value) return value;
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

function stringArg(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function clampInteger(value, minimum, maximum, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.trunc(number)));
}

function truncateText(value, maxChars) {
  if (typeof value !== "string") return value;
  if (!maxChars || value.length <= maxChars) return value;
  return value.slice(0, maxChars);
}

function readJsonSync(file, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

async function writeJsonAtomic(file, value, mode = 0o600) {
  const { mkdir, rename, writeFile } = await import("node:fs/promises");
  await mkdir(path.dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}.tmp`;
  await writeFile(temp, JSON.stringify(value, null, 2) + "\n", { mode });
  await rename(temp, file);
}

function createLogger(file) {
  const target = expandHome(file);
  let ready = false;
  if (target) {
    try { fs.mkdirSync(path.dirname(target), { recursive: true }); ready = true; } catch { /* stderr only */ }
  }
  let chain = Promise.resolve();
  return (level, event, fields = {}) => {
    const stamp = new Date().toISOString();
    const extra = fields && Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : "";
    const line = `${stamp} [dshworker] ${level} ${event}${extra}`;
    try { process.stderr.write(line + "\n"); } catch { /* ignore */ }
    if (!ready) return;
    chain = chain
      .then(async () => {
        const { appendFile } = await import("node:fs/promises");
        await appendFile(target, line + "\n");
      })
      .catch(() => {});
  };
}

// ---------------------------------------------------------------------------
// 身份 / 配置
// ---------------------------------------------------------------------------

function loadIdentity(config, log) {
  const pinned = stringArg(config.identity) ?? stringArg(process.env.CODEXWORKER_IDENTITY);
  const candidates = pinned
    ? [path.resolve(expandHome(pinned))]
    : [path.resolve("identity.json"), path.join(ROOT, "identity.json")];
  for (const file of candidates) {
    const value = readJsonSync(file);
    if (!value) continue;
    const fallback = path.basename(ROOT);
    return {
      file,
      name: stringArg(value.name) ?? fallback,
      display: stringArg(value.display) ?? stringArg(value.name) ?? fallback,
      bridge: stringArg(value.bridge) ?? fallback,
      port: Number.isInteger(Number(value.port)) && Number(value.port) > 0 ? Number(value.port) : null,
      configFile: value.configFile ? path.resolve(expandHome(value.configFile)) : null,
      stateDir: value.stateDir ? path.resolve(expandHome(value.stateDir)) : null,
    };
  }
  log("warn", "identity_missing", { tried: candidates });
  return { file: null, name: "DshWorker", display: "DshWorker", bridge: "DshWorker", port: null, configFile: null, stateDir: null };
}

function loadConfig(identity, config, log) {
  const file = stringArg(config.config) ? path.resolve(expandHome(config.config))
    : stringArg(process.env.CODEXWORKER_CONFIG) ? path.resolve(expandHome(process.env.CODEXWORKER_CONFIG))
      : identity.configFile;
  if (!file) return { file: null, value: {} };
  const value = readJsonSync(file);
  if (!value) { log("warn", "config_unreadable", { file }); return { file, value: {} }; }
  return { file, value };
}

// ---------------------------------------------------------------------------
// 进程内传输：线程存储 + Agent 池
// ---------------------------------------------------------------------------

class InProcessTransport {
  constructor({ ctx, selection, cwd, stateDir, turnTimeoutMs, log, proxyHint = null }) {
    this.ctx = ctx;
    this.selection = selection;
    this.defaultCwd = cwd;
    this.stateFile = path.join(stateDir, STATE_FILE_NAME);
    this.turnTimeoutMs = turnTimeoutMs;
    this.log = log;
    this.proxyHint = proxyHint;
    this.threads = new Map();
    this.live = new Map(); // threadId -> { handle, agent }
    this.closing = false;
    this.writeChain = Promise.resolve();
    this.load();
  }

  // ---- 状态 ---------------------------------------------------------------

  load() {
    const parsed = readJsonSync(this.stateFile, null);
    for (const record of Array.isArray(parsed?.threads) ? parsed.threads : []) {
      if (!record?.id) continue;
      for (const turn of record.turns ?? []) {
        if (!TERMINAL_TURN_STATUSES.has(turn.status)) {
          turn.status = "interrupted";
          turn.error ??= "dshworker plugin restarted while this turn was running";
          turn.completedAt ??= new Date().toISOString();
        }
      }
      record.status = "idle";
      record.running = false;
      record.queue = [];
      this.threads.set(record.id, record);
    }
  }

  save() {
    const threads = [...this.threads.values()].map((thread) => ({
      id: thread.id, sessionId: thread.sessionId ?? null, title: thread.title ?? null,
      cwd: thread.cwd, createdAt: thread.createdAt, updatedAt: thread.updatedAt,
      turns: (thread.turns ?? []).map((turn) => ({
        ...turn,
        text: truncateText(turn.text, MAX_PERSISTED_TEXT),
        thinking: truncateText(turn.thinking, MAX_PERSISTED_TEXT),
      })),
    }));
    const payload = { version: 1, source: "dsh-inprocess", savedAt: new Date().toISOString(), threads };
    this.writeChain = this.writeChain.then(() => writeJsonAtomic(this.stateFile, payload)).catch(() => {});
    return this.writeChain;
  }

  // ---- transport 接口 -----------------------------------------------------

  async listTools() {
    return TOOL_DEFINITIONS.map((tool) => ({ ...tool, inputSchema: structuredClone(tool.inputSchema) }));
  }

  async execute(toolName, args = {}, timeoutMs = 5_000) {
    switch (toolName) {
      case "create_thread": return this.createThread(args);
      case "list_threads": return this.listThreads(args);
      case "read_thread": return this.readThread(args);
      case "send_message_to_thread": return this.sendMessage(args);
      case "set_thread_title": return this.setThreadTitle(args);
      case "wait_threads": return this.waitThreads(args, timeoutMs);
      case "list_projects": return this.listProjects(args);
      default: throw new Error(`${TRANSPORT_NAME} does not implement native tool "${toolName}"`);
    }
  }

  // ---- 线程 ---------------------------------------------------------------

  createThread(args = {}) {
    const cwd = this.resolveWorkspace(args.cwd ?? args.target?.cwd);
    const title = stringArg(args.title) ?? stringArg(args.name);
    const thread = {
      id: randomUUID(), sessionId: null, title: title ?? null, cwd,
      createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
      status: "idle", running: false, queue: [], turns: [],
    };
    this.threads.set(thread.id, thread);
    const payload = { threadId: thread.id, thread: normalizeThread(thread), title: thread.title };
    const prompt = stringArg(args.prompt) ?? stringArg(args.text);
    if (prompt) {
      const sent = this.sendMessage({ threadId: thread.id, prompt });
      payload.turnId = sent.turnId;
      payload.turn = sent.turn;
      payload.status = sent.status;
    }
    void this.save();
    return payload;
  }

  sendMessage(args = {}) {
    const thread = this.requireThread("send_message_to_thread", args);
    const prompt = stringArg(args.prompt) ?? stringArg(args.text) ?? stringArg(args.message)
      ?? textFromInput(args.input);
    if (!prompt) throw new Error("send_message_to_thread requires a non-empty `prompt` (or text `input`)");
    const turn = {
      id: randomUUID(), prompt, status: "inProgress", text: "", thinking: "",
      error: null, reasonKind: null, startedAt: new Date().toISOString(), completedAt: null,
    };
    thread.turns.push(turn);
    trimThread(thread);
    thread.updatedAt = turn.startedAt;
    thread.status = "inProgress";
    if (thread.running) thread.queue.push(turn.id);
    else void this.startTurn(thread, turn);
    return {
      threadId: thread.id, turnId: turn.id, accepted: true, status: "inProgress",
      turn: { id: turn.id, status: turn.status, startedAt: turn.startedAt },
    };
  }

  async startTurn(thread, turn) {
    thread.running = true;
    thread.status = "inProgress";
    let thinking = "";
    let stopStream = null;
    let timer = null;
    let timedOut = false;
    try {
      const agent = await this.ensureAgent(thread);
      stopStream = this.ctx.on("agent/assistant-stream", ({ agent: subject, frame }) => {
        if (subject !== agent) return;
        const chunk = frame?.type === "chunk" ? frame.chunk : null;
        if (chunk?.type === "reasoning-delta" && typeof chunk.text === "string" && chunk.text) {
          thinking += chunk.text;
          turn.thinking = thinking;
        }
      });
      const fromSeq = agent.session.seq;
      timer = setTimeout(() => {
        timedOut = true;
        try { agent.cancel({ kind: "user" }); } catch { /* already idle */ }
      }, this.turnTimeoutMs);
      timer.unref?.();

      agent.followup(userMessage(turn.prompt));
      const subagents = this.ctx.get("subagents");
      for (;;) {
        await agent.whenIdle();
        const idleSeq = agent.session.seq;
        if (subagents && await subagents.waitForChildren(agent)) continue;
        if (agent.status === "idle" && agent.session.seq === idleSeq) break;
      }
      const outcome = summarize(agent.session, fromSeq);
      try { await this.ctx.get("sessions")?.flush?.(agent.session); } catch { /* flush is best effort */ }
      if (timedOut) {
        this.finishTurn(thread, turn, { text: outcome.text, thinking,
          error: `dsh turn timed out after ${this.turnTimeoutMs}ms`, reasonKind: "timeout" });
        return;
      }
      const kind = outcome.reason?.kind ?? "completed";
      if (kind === "completed") {
        this.finishTurn(thread, turn, { text: outcome.text, thinking, reasonKind: "completed" });
        return;
      }
      const detail = this.decorate(outcome.reason?.error?.message ?? (kind === "error" ? "dsh turn failed" : kind));
      this.finishTurn(thread, turn, { text: outcome.text, thinking, error: detail, reasonKind: kind });
    } catch (error) {
      const message = this.decorate(error?.message ?? String(error));
      this.log("warn", "turn_failed", { threadId: thread.id, turnId: turn.id,
        message, code: error?.code ?? null });
      this.finishTurn(thread, turn, { thinking, error: message, reasonKind: "error" });
    } finally {
      if (timer) clearTimeout(timer);
      try { stopStream?.(); } catch { /* ignore */ }
    }
  }

  finishTurn(thread, turn, { text = "", thinking = "", error = null, reasonKind = "failed" }) {
    turn.status = error ? "failed" : "completed";
    turn.text = text;
    if (thinking) turn.thinking = thinking;
    turn.error = error ?? null;
    turn.reasonKind = reasonKind;
    turn.completedAt = new Date().toISOString();
    thread.updatedAt = turn.completedAt;
    const nextId = thread.queue.shift();
    const next = nextId ? thread.turns.find((candidate) => candidate.id === nextId) : null;
    if (next) {
      void this.save();
      void this.startTurn(thread, next);
      return;
    }
    thread.running = false;
    thread.status = reasonKind === "completed" ? "idle" : "failed";
    void this.save();
  }

  async waitThreads(args = {}, timeoutBudget = DEFAULT_WAIT_MS) {
    const targets = normalizeWaitTargets(args);
    const requested = Number(args.timeoutMs);
    const waitMs = Math.max(0, Math.min(Number.isFinite(requested) ? requested : timeoutBudget, MAX_WAIT_MS));
    const deadline = Date.now() + waitMs;
    for (;;) {
      const pending = targets.some(({ threadId }) => {
        const thread = this.threads.get(threadId);
        return Boolean(thread && (thread.running || thread.queue?.length));
      });
      if (!pending || waitMs === 0 || Date.now() >= deadline) {
        const timedOut = pending && waitMs > 0;
        return {
          polls: targets.map(({ threadId, hostId }) => this.pollThread(threadId, hostId)),
          timedOut, snapshot: waitMs === 0,
          ...(timedOut ? { reason: `wait_threads timed out after ${waitMs}ms; returning current progress` } : {}),
          timeoutMs: waitMs,
        };
      }
      await sleep(Math.max(1, Math.min(POLL_INTERVAL_MS, deadline - Date.now())));
    }
  }

  pollThread(threadId, hostId = "local") {
    const thread = this.threads.get(threadId);
    if (!thread) {
      return { thread: { id: threadId, hostId, title: null, cwd: null, status: null },
        latestTurn: null, latestAssistantMessage: null, cursor: null };
    }
    const latest = thread.turns.at(-1) ?? null;
    return {
      thread: { id: thread.id, hostId, title: thread.title ?? null, cwd: thread.cwd, status: thread.status },
      latestTurn: latest ? { id: latest.id, status: latest.status,
        startedAt: latest.startedAt ?? null, completedAt: latest.completedAt ?? null } : null,
      latestAssistantMessage: latestAssistantMessage(latest),
      cursor: null,
    };
  }

  readThread(args = {}) {
    const thread = this.requireThread("read_thread", args);
    const turnLimit = clampInteger(args.turnLimit, 0, 200, 10);
    const maxChars = Number.isFinite(Number(args.maxOutputCharsPerItem))
      ? Math.max(1, Number(args.maxOutputCharsPerItem)) : null;
    const all = thread.turns ?? [];
    const limited = turnLimit > 0 ? all.slice(-turnLimit) : all;
    const turns = limited.map((turn) => normalizeTurn(turn, maxChars));
    return {
      thread: normalizeThread(thread),
      turns,
      page: { order: "oldest_first", nextCursor: null, returnedTurns: turns.length },
      itemsSource: "dsh-session",
    };
  }

  setThreadTitle(args = {}) {
    const thread = this.requireThread("set_thread_title", args);
    const title = stringArg(args.title) ?? stringArg(args.name);
    if (!title) throw new Error("set_thread_title requires a non-empty `title`");
    thread.title = title;
    thread.updatedAt = new Date().toISOString();
    void this.save();
    return { updated: true, threadId: thread.id, title };
  }

  listThreads(args = {}) {
    const limit = clampInteger(args.limit, 1, 100, 20);
    const search = stringArg(args.searchTerm)?.toLowerCase() ?? null;
    const all = [...this.threads.values()]
      .map(normalizeThread)
      .filter((thread) => !search || `${thread.title ?? ""} ${thread.preview ?? ""}`.toLowerCase().includes(search))
      .sort((a, b) => (Date.parse(b.updatedAt ?? 0) || 0) - (Date.parse(a.updatedAt ?? 0) || 0) || a.id.localeCompare(b.id));
    return {
      schemaVersion: 4, source: NAMESPACE, pinnedThreads: [],
      threads: all.slice(0, limit), nextCursor: null, backwardsCursor: null,
    };
  }

  listProjects(args = {}) {
    const limit = clampInteger(args.limit, 1, 100, 100);
    const byPath = new Map();
    for (const thread of [...this.threads.values()].map(normalizeThread)) {
      if (!thread?.cwd) continue;
      const key = path.resolve(thread.cwd);
      let project = byPath.get(key);
      if (!project) {
        project = {
          projectId: `dsh:${createHash("sha256").update(key).digest("hex").slice(0, 32)}`,
          kind: "local", hostId: "local", name: path.basename(key) || key,
          path: thread.cwd, threadCount: 0, lastUsedAt: null,
        };
        byPath.set(key, project);
      }
      project.threadCount += 1;
      const usedAt = thread.updatedAt ?? thread.createdAt ?? null;
      if (usedAt && (!project.lastUsedAt || Date.parse(usedAt) > Date.parse(project.lastUsedAt))) {
        project.lastUsedAt = usedAt;
      }
    }
    const projects = [...byPath.values()]
      .sort((a, b) => (Date.parse(b.lastUsedAt ?? 0) || 0) - (Date.parse(a.lastUsedAt ?? 0) || 0)
        || a.path.localeCompare(b.path))
      .slice(0, limit);
    return { schemaVersion: 1, source: NAMESPACE, projects };
  }

  requireThread(toolName, args) {
    const threadId = stringArg(args.threadId);
    if (!threadId) throw new Error(`${toolName} requires a non-empty string \`threadId\``);
    const thread = this.threads.get(threadId);
    if (!thread) throw new Error(`${toolName}: unknown dsh thread "${threadId}"`);
    return thread;
  }

  resolveWorkspace(value) {
    const resolved = path.resolve(expandHome(stringArg(value) || this.defaultCwd || DEFAULT_WORKSPACE));
    try {
      if (!fs.statSync(resolved).isDirectory()) throw new Error("not a directory");
    } catch {
      throw new Error(`workspace does not exist: ${resolved}`);
    }
    return resolved;
  }

  // ---- Agent 池 -----------------------------------------------------------

  async ensureAgent(thread) {
    const entry = this.live.get(thread.id);
    if (entry?.agent && entry.agent.status !== "disposed") return entry.agent;
    this.live.delete(thread.id);
    const agents = this.ctx.get("agents");
    const agentOptions = { ...this.selection };
    if (thread.sessionId) {
      try {
        const handle = await agents.resume({ resumeSessionId: thread.sessionId, agentOptions });
        const agent = handle.agent;
        this.live.set(thread.id, { handle, agent });
        this.log("info", "agent_resumed", { threadId: thread.id, sessionId: thread.sessionId,
          seq: agent.session.seq });
        return agent;
      } catch (error) {
        this.log("warn", "agent_resume_failed", { threadId: thread.id, sessionId: thread.sessionId,
          message: error?.message ?? String(error) });
        thread.sessionId = null;
      }
    }
    const sessionId = `session-${randomUUID()}`;
    const handle = await agents.create({
      sessionId,
      meta: { cwd: thread.cwd },
      agentOptions,
    });
    const agent = handle.agent;
    thread.sessionId = typeof agent.session?.id === "string" ? agent.session.id : sessionId;
    this.live.set(thread.id, { handle, agent });
    this.log("info", "agent_created", { threadId: thread.id, sessionId: thread.sessionId, seq: agent.session.seq });
    return agent;
  }

  decorate(message) {
    const text = typeof message === "string" ? message : String(message ?? "");
    if (!this.proxyHint || !/transport failed/i.test(text)) return text;
    return `${text}（宿主代理 ${this.proxyHint} 可能不可达；dsh 在插件加载前就固定了代理，改用回环代理或清空代理再启动）`;
  }

  dropLive(threadId) {
    const entry = this.live.get(threadId);
    if (!entry) return;
    this.live.delete(threadId);
    void entry.handle.dispose().catch(() => {});
  }

  async close(reason = "plugin disposed") {
    this.closing = true;
    for (const thread of this.threads.values()) {
      for (const turn of thread.turns ?? []) {
        if (!TERMINAL_TURN_STATUSES.has(turn.status)) {
          turn.status = "interrupted";
          turn.error ??= reason;
          turn.completedAt ??= new Date().toISOString();
        }
      }
      thread.running = false;
      thread.queue = [];
      thread.status = "idle";
    }
    for (const threadId of [...this.live.keys()]) this.dropLive(threadId);
    await this.save().catch(() => {});
  }

  status() {
    return {
      threads: this.threads.size,
      liveAgents: this.live.size,
      runningTurns: [...this.threads.values()].filter((thread) => thread.running).length,
      provider: this.selection.provider ?? null,
      model: this.selection.model ?? null,
      stateFile: this.stateFile,
    };
  }
}

// ---------------------------------------------------------------------------
// 纯函数（与 interface/transport/dsh.mjs 的返回结构保持一致）
// ---------------------------------------------------------------------------

function userMessage(text) {
  return { id: randomUUID(), role: "user", content: [{ type: "text", text }], source: { kind: "user" } };
}

function summarize(session, fromSeq) {
  let started = false;
  let text = "";
  let reason;
  for (let seq = fromSeq; seq < session.seq; seq++) {
    const event = session.eventAt(seq);
    if (!event) continue;
    if (event.type === "turn/start") { started = true; reason = undefined; continue; }
    if (!started) continue;
    if (event.type === "assistant/message") {
      const joined = (event.data.message.content ?? [])
        .filter((block) => block.type === "text").map((block) => block.text).join("");
      if (joined !== "") text = joined;
    }
    if (event.type === "turn/end") reason = event.data.reason;
  }
  return { text, reason };
}

function normalizeThread(thread) {
  const turns = thread.turns ?? [];
  const last = turns.at(-1) ?? null;
  return {
    id: thread.id, kind: "dsh", hostId: "local",
    title: thread.title ?? null, name: thread.title ?? null,
    preview: last?.prompt ? String(last.prompt).slice(0, 200) : "",
    cwd: thread.cwd ?? null, createdAt: thread.createdAt ?? null, updatedAt: thread.updatedAt ?? null,
    recencyAt: thread.updatedAt ?? null, status: thread.status ?? "idle", source: "dsh",
    model: null, modelProvider: "deepseek", path: null, ephemeral: false, forkedFromId: null,
    sessionId: thread.sessionId ?? null,
  };
}

function normalizeTurn(turn, maxChars) {
  return {
    id: turn.id, status: turn.status, items: turnItems(turn, maxChars), itemsView: "full",
    startedAt: turn.startedAt ?? null, completedAt: turn.completedAt ?? null,
    durationMs: turn.startedAt && turn.completedAt
      ? Math.max(0, Date.parse(turn.completedAt) - Date.parse(turn.startedAt)) : null,
    error: turn.error ?? null, role: "assistant", text: turn.text ?? "",
  };
}

function turnItems(turn, maxChars) {
  const items = [{
    type: "userMessage", kind: "trainer", id: `${turn.id}-user`,
    text: truncateText(turn.prompt ?? "", maxChars),
  }];
  // 一个 turn 收完之后整段发一条：思考 + 正文合成同一个 assistant item。
  // 只在 turn 终结时带上思考：inProgress 阶段扫到的那次只发正文，
  // 否则同一个 turn 会在飞书上出现「输出」「回复」两条重复内容。
  if (turn.text || turn.thinking) {
    const parts = [];
    const done = turn.status === "completed";
    if (turn.thinking && done) {
      const thought = truncateText(turn.thinking, maxChars);
      parts.push(`💭 Reasoning:\n\n\`\`\`plaintext\n${thought}\n\`\`\``);
    }
    if (turn.text) parts.push(truncateText(turn.text, maxChars));
    items.push({ type: "agentMessage", kind: "assistant", id: `${turn.id}-agent`,
      text: parts.join("\n\n"), phase: done ? "final_answer" : null });
  }
  if (turn.error) items.push({ type: "error", id: `${turn.id}-error`, text: turn.error });
  return items;
}

function latestAssistantMessage(turn) {
  if (!turn?.text) return null;
  return { id: `${turn.id}-agent`, turnId: turn.id, text: turn.text,
    phase: turn.status === "completed" ? "final_answer" : null, truncated: false };
}

function trimThread(thread) {
  while (thread.turns.length > MAX_TURNS_PER_THREAD) thread.turns.shift();
}

function normalizeWaitTargets(args) {
  const raw = Array.isArray(args.targets)
    ? args.targets
    : Array.isArray(args.threadIds)
      ? args.threadIds.map((threadId) => ({ threadId }))
      : args.threadId ? [{ threadId: args.threadId }] : [];
  const targets = [];
  for (const target of raw) {
    const threadId = stringArg(typeof target === "string" ? target : target?.threadId);
    if (!threadId) throw new Error("wait_threads target is missing a string `threadId`");
    targets.push({ threadId, hostId: stringArg(target?.hostId) ?? "local" });
  }
  if (!targets.length) throw new Error("wait_threads requires at least one target thread");
  return targets;
}

function textFromInput(input) {
  if (!Array.isArray(input)) return null;
  const text = input
    .map((part) => (part && typeof part === "object" && part.type === "text" && typeof part.text === "string" ? part.text : ""))
    .filter(Boolean)
    .join("\n");
  return text || null;
}

// ---------------------------------------------------------------------------
// 插件入口
// ---------------------------------------------------------------------------

export function apply(ctx, config = {}) {
  const log = createLogger(config.logFile ?? "~/.hermes/logs/dshworker-plugin.log");
  const identity = loadIdentity(config, log);
  const { file: configFile, value: workerConfig } = loadConfig(identity, config, log);
  const stateDir = path.resolve(expandHome(
    stringArg(config.stateDir) ?? stringArg(process.env.CODEXWORKER_STATE_DIR)
      ?? identity.stateDir ?? DEFAULT_STATE_DIR));

  // dsh 的 HTTP 代理策略由启动器在任何插件加载前就装好了（dsh-http-proxy：
  // "the launcher resolves and installs the policy for every profile before the
  // first plugin loads"）。所以插件里再改 process.env 已经晚了——这里只做诊断，
  // 不改宿主环境。
  const proxyEnv = {
    httpsProxy: process.env.https_proxy ?? process.env.HTTPS_PROXY ?? null,
    allProxy: process.env.all_proxy ?? process.env.ALL_PROXY ?? null,
  };
  const proxyHost = (() => {
    const value = proxyEnv.httpsProxy ?? proxyEnv.allProxy;
    if (!value) return null;
    try { return new URL(value).hostname; } catch { return value; }
  })();
  const loopbackProxy = !proxyHost || /^(127\.|localhost$|::1$|\[::1\]$)/i.test(proxyHost);
  if (!loopbackProxy) {
    log("warn", "proxy_not_loopback", {
      httpsProxy: proxyEnv.httpsProxy, allProxy: proxyEnv.allProxy,
      hint: "DSH 在插件加载前就固定了代理；这台主机不可达时每轮都会 TRANSPORT 失败。用回环代理或清空代理再启动 dsh。",
    });
  }

  const defaultSelection = ctx.get("agentDefaultModel")?.currentSelection?.() ?? {};
  const selection = {
    provider: stringArg(config.provider) ?? defaultSelection.provider,
    model: stringArg(config.model) ?? defaultSelection.model,
  };
  if (stringArg(config.reasoningEffort) ?? defaultSelection.reasoningEffort) {
    selection.reasoningEffort = stringArg(config.reasoningEffort) ?? defaultSelection.reasoningEffort;
  }
  const requestedWorkspace = path.resolve(expandHome(
    stringArg(config.workspace) ?? stringArg(workerConfig.dsh?.workspace) ?? DEFAULT_WORKSPACE));
  const isDir = (candidate) => {
    try { return fs.statSync(candidate).isDirectory(); } catch { return false; }
  };
  // 身份文件里的 workspace 可能属于别的机器/还没建；退到 ~/projects，再退到 $HOME。
  const fallbackWorkspace = [DEFAULT_WORKSPACE, os.homedir()].find(isDir) ?? os.homedir();
  const workspace = isDir(requestedWorkspace) ? requestedWorkspace : fallbackWorkspace;
  if (workspace !== requestedWorkspace) {
    log("warn", "workspace_fallback", { requested: requestedWorkspace, using: workspace });
  }
  const turnTimeoutMs = Number(config.turnTimeoutMs) > 0 ? Number(config.turnTimeoutMs) : DEFAULT_TURN_TIMEOUT_MS;

  const transport = new InProcessTransport({
    ctx, selection, cwd: workspace, stateDir, turnTimeoutMs,
    proxyHint: loopbackProxy ? null : (proxyEnv.httpsProxy ?? proxyEnv.allProxy),
    log: (level, event, fields) => log(level, event, fields),
  });

  // 业务层通过这个选择器取传输；注册后立刻刷新缓存，保证 mcp-business
  // 首次 import 时拿到的就是进程内实现。
  registerNativeTransport(TRANSPORT_NAME, {
    name: TRANSPORT_NAME,
    capabilities: Object.freeze({ fork: false, desktopJournal: false, callerTurn: false }),
    listTools: () => transport.listTools(),
    callTool: async (toolName, args, _invocation, timeoutMs) => {
      try {
        return { success: true, tool: toolName, namespace: NAMESPACE,
          contentItems: [{ type: "text", text: JSON.stringify(await transport.execute(toolName, args ?? {}, timeoutMs)) }] };
      } catch (error) {
        return { success: false, tool: toolName, namespace: NAMESPACE,
          contentItems: [{ type: "text", text: JSON.stringify({ success: false, error: "tool_failed", tool: toolName,
            reason: error?.message ?? String(error) }) }] };
      }
    },
    shutdown: () => transport.close("transport shutdown"),
  });
  process.env.CODEXWORKER_NATIVE_TRANSPORT = TRANSPORT_NAME;
  process.env.CODEXWORKER_STATE_DIR = stateDir;
  resetNativeTransportCache();

  let business = null;
  let routerClient = null;
  const api = {
    version: VERSION,
    transport: TRANSPORT_NAME,
    identity: { name: identity.name, display: identity.display, configFile },
    status: () => ({ ready: true, identity: identity.display, business: business !== null,
      router: routerClient !== null, ...transport.status() }),
    listTools: () => transport.listTools(),
    callTool: (toolName, args, timeoutMs) => transport.execute(toolName, args ?? {}, timeoutMs),
    threads: () => transport.listThreads({ limit: 100 }).threads,
    /** 便捷入口：一轮对话跑完再返回（给脚本/自检用）。 */
    async run(prompt, { threadId = null, cwd = null, timeoutMs = turnTimeoutMs } = {}) {
      const recent = [...transport.threads.values()]
        .sort((a, b) => (Date.parse(b.updatedAt ?? 0) || 0) - (Date.parse(a.updatedAt ?? 0) || 0))[0] ?? null;
      const thread = threadId
        ? transport.requireThread("run", { threadId })
        : recent ?? transport.threads.get(
          transport.createThread({ title: `${identity.display} · plugin`, cwd: cwd ?? workspace }).threadId);
      const sent = transport.sendMessage({ threadId: thread.id, prompt });
      const waited = await transport.waitThreads({ targets: [{ threadId: thread.id }], timeoutMs: Math.min(timeoutMs, MAX_WAIT_MS) }, timeoutMs);
      const turn = thread.turns.find((candidate) => candidate.id === sent.turnId) ?? null;
      return { threadId: thread.id, turnId: sent.turnId, timedOut: waited.timedOut,
        status: turn?.status ?? null, text: turn?.text ?? "", error: turn?.error ?? null,
        thinking: turn?.thinking ?? "" };
    },
    async close(reason) {
      try { await routerClient?.stop(); } catch { /* ignore */ }
      routerClient = null;
      try { await business?.close(); } catch { /* ignore */ }
      business = null;
      await transport.close(reason ?? "plugin disposed");
    },
  };

  ctx.provide("dshworker", api);
  ctx.effect(() => () => { void api.close("plugin disposed").catch(() => {}); });

  const businessEnabled = config.business === true
    || process.env.DSHWORKER_BUSINESS === "1"
    || workerConfig.plugin?.business === true;
  const selfTestPrompt = stringArg(config.selfTest)
    ?? (process.env.DSHWORKER_SELFTEST === "1" ? "Reply with exactly: SELF-TEST-OK" : null);

  log("info", "plugin_loaded", {
    version: VERSION, identity: identity.display, configFile, stateDir,
    provider: selection.provider ?? null, model: selection.model ?? null,
    workspace, business: businessEnabled, transport: TRANSPORT_NAME,
  });

  void (async () => {
    await ctx.get("loader")?.await();
    if (selfTestPrompt) {
      await runSelfTest({ transport, identity, stateDir, workspace, prompt: selfTestPrompt, log })
        .catch((error) => log("error", "self_test_failed", { message: error?.message ?? String(error) }));
    }
    if (businessEnabled) {
      try {
        const started = await startBusiness({ ctx, transport, identity, workerConfig, config, stateDir,
          workspace, log });
        business = started.business;
        routerClient = started.routerClient;
      } catch (error) {
        log("error", "business_start_failed", { message: error?.message ?? String(error),
          code: error?.code ?? null });
      }
    }
  })().catch((error) => log("error", "boot_failed", { message: error?.message ?? String(error) }));
}

// ---------------------------------------------------------------------------
// 自检：用业务层同款工具跑两轮，第二轮必须能引用第一轮的答案
// ---------------------------------------------------------------------------

async function runSelfTest({ transport, identity, stateDir, workspace, prompt, log }) {
  const startedAt = Date.now();
  const created = transport.createThread({ title: `${identity.display} · self-test`, cwd: workspace });
  const threadId = created.threadId;
  log("info", "self_test_start", { threadId, prompt });

  const first = transport.sendMessage({ threadId, prompt });
  await transport.waitThreads({ targets: [{ threadId }], timeoutMs: 120_000 });
  const turn1 = transport.readThread({ threadId, turnLimit: 1, includeOutputs: true }).turns.at(-1) ?? null;

  const second = transport.sendMessage({
    threadId,
    prompt: "Repeat the exact token you replied with in my previous message. Answer with that token only.",
  });
  await transport.waitThreads({ targets: [{ threadId }], timeoutMs: 120_000 });
  const turn2 = transport.readThread({ threadId, turnLimit: 1, includeOutputs: true }).turns.at(-1) ?? null;

  const result = {
    ok: turn1?.status === "completed" && turn2?.status === "completed"
      && Boolean(turn1?.text) && String(turn2?.text ?? "").includes(String(turn1?.text ?? "").trim()),
    threadId, sessionId: transport.threads.get(threadId)?.sessionId ?? null,
    turn1: { id: first.turnId, status: turn1?.status ?? null, text: turn1?.text ?? "", error: turn1?.error ?? null },
    turn2: { id: second.turnId, status: turn2?.status ?? null, text: turn2?.text ?? "", error: turn2?.error ?? null },
    durationMs: Date.now() - startedAt,
    provider: transport.selection.provider ?? null, model: transport.selection.model ?? null,
  };
  await writeJsonAtomic(path.join(stateDir, SELF_TEST_FILE), result);
  log(result.ok ? "info" : "error", "self_test_done", {
    ok: result.ok, threadId, sessionId: result.sessionId, durationMs: result.durationMs,
    turn1: result.turn1.status, turn2: result.turn2.status,
    turn1Chars: result.turn1.text.length, turn2Chars: result.turn2.text.length,
  });
  return result;
}

// ---------------------------------------------------------------------------
// 可选：在本进程里跑完整业务层（HTTP + 飞书 + router）
// ---------------------------------------------------------------------------

function resolveFeishuConfig(value) {
  const configured = value.feishu?.configFile
    ?? path.join(os.homedir(), ".hermes", "secrets", "feishu-oguri.json");
  const file = path.resolve(expandHome(configured));
  const parsed = readJsonSync(file);
  if (!parsed?.app_id || !parsed?.app_secret) return { file, value: null };
  return { file, value: parsed };
}

function resolveWhitelist(value, feishuValue) {
  // 显式给的数组就是不等于"没配"：空数组表示明确关掉飞书入站，绝不回退到
  // 凭据文件里的 open_id（否则测试实例会悄悄连上真实飞书）。
  if (Array.isArray(value.feishu?.trainerOpenIdWhitelist)) {
    const ids = value.feishu.trainerOpenIdWhitelist
      .filter((id) => typeof id === "string" && id.trim()).map((id) => id.trim());
    return { ids, source: ids.length ? "config" : "config_empty" };
  }
  const trainer = stringArg(feishuValue?.trainer_open_id);
  const openId = stringArg(feishuValue?.open_id);
  const derived = trainer ?? openId;
  return derived ? { ids: [derived], source: trainer ? "feishu.trainer_open_id" : "feishu.open_id" } : { ids: [], source: "none" };
}

function portFree(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.once("listening", () => server.close(() => resolve(true)));
    try { server.listen(port, host); } catch { resolve(false); }
  });
}

async function startBusiness({ ctx, transport, identity, workerConfig, config, stateDir, workspace, log }) {
  const host = stringArg(config.host) ?? stringArg(workerConfig.host) ?? "127.0.0.1";
  const port = Number(config.port ?? identity.port ?? workerConfig.port ?? 8906);
  if (!await portFree(port, host)) {
    log("warn", "business_port_busy", { host, port,
      reason: "another worker owns this port; 插件不抢生产进程的 8906" });
    return { business: null, routerClient: null };
  }

  const tokenFile = path.resolve(expandHome(
    process.env.CODEXWORKER_TOKEN_FILE ?? workerConfig.tokenFile
      ?? path.join(os.homedir(), ".hermes", "secrets", "router.token")));
  const token = (() => {
    try { return fs.readFileSync(tokenFile, "utf8").trim().replace(/^Bearer\s+/i, ""); }
    catch { return ""; }
  })();
  const feishu = resolveFeishuConfig(workerConfig);
  if (!feishu.value) log("warn", "feishu_config_missing", { file: feishu.file });
  const whitelist = resolveWhitelist(workerConfig, feishu.value);

  // 任务看板目标：训练员与这个 worker 的私聊。显式 taskBoardOpenId 优先，其次
  // 入站白名单（就是训练员本人），最后才是凭据文件里的 open_id。和 cli.mjs 的
  // 表达式保持一致，写进 desktop-config 的那份也复用这里的值。
  const taskBoardOpenId = stringArg(workerConfig.feishu?.taskBoardOpenId) ?? whitelist.ids[0]
    ?? stringArg(feishu.value?.open_id) ?? null;
  const taskBoardEnabled = workerConfig.feishu?.taskBoardEnabled !== false && Boolean(taskBoardOpenId);

  const thread = transport.threads.values().next().value
    ?? transport.threads.get(transport.createThread({ title: `${identity.display} · DSH`, cwd: workspace }).threadId);
  const bindingPath = path.join(stateDir, "manager-binding.json");
  const desktopConfigPath = path.join(stateDir, "desktop-config.local.json");
  await writeJsonAtomic(bindingPath, {
    formatVersion: 1, integrationMode: "desktop-native",
    managerThreadId: thread.id, managerTurnId: null, boundAt: new Date().toISOString(),
  });
  const projects = await transport.execute("list_projects", {}, 5_000).catch(() => ({ projects: [] }));
  const project = (projects.projects ?? []).find((entry) => entry.path === thread.cwd && entry.hostId === "local");
  await writeJsonAtomic(desktopConfigPath, {
    host, port, bridgeName: identity.display, operatingMode: "feishu-local",
    machine: identity.name, tokenFile, workspaceRoots: [thread.cwd],
    ...(project?.projectId ? { projectId: project.projectId } : {}),
    pollMs: 1000,
    feishu: {
      configFile: feishu.file,
      inboundEnabled: true,
      trainerOpenIdWhitelist: whitelist.ids,
      taskBoardOpenId,
      taskBoardEnabled: workerConfig.feishu?.taskBoardEnabled !== false,
      onlineNotificationEnabled: workerConfig.feishu?.onlineNotificationEnabled ?? false,
      streamEnabled: workerConfig.feishu?.streamEnabled ?? true,
      defaultDelivery: workerConfig.feishu?.defaultDelivery ?? "steer",
      autoImageOutput: workerConfig.feishu?.autoImageOutput ?? true,
    },
  });
  log("info", "manager_config_written", { binding: bindingPath, config: desktopConfigPath,
    whitelistCount: whitelist.ids.length, whitelistSource: whitelist.source });

  process.env.RUDOLF_BRIDGE_LIFECYCLE_CHILD = "1";
  process.env.RUDOLF_BRIDGE_LIFECYCLE_READY ??= "1";
  const { createBusiness } = await import("./mcp-business.mjs");
  const startedAt = new Date().toISOString();
  const runtimeControl = {
    transition: false,
    status: () => ({ available: true, scope: "plugin-inprocess", featureVersion: VERSION, sha256: null,
      loadedAt: startedAt, loadCount: 0, activeImageSends: 0, reloadAvailable: false,
      autoReloadOnStart: false, lastReloadError: null }),
    reload: async () => ({ available: true, scope: "plugin-inprocess", reloadAvailable: false,
      lastReloadError: "business hot reload is not wired in plugin mode" }),
  };
  const business = createBusiness(runtimeControl);
  await business.activate();
  log("info", "business_started", { url: `http://${host}:${port}`, threadId: thread.id });

  // 飞书凭据是懒加载的：desktop-config 里只写 configFile，不写 appId/appSecret，
  // 所以 activate() 时 manager 的 notifier 还是 null——思考/正文的飞书投递全被
  // 静默跳过。reload 一次才会去读凭据、建 notifier、连 ws。
  try {
    const reloaded = await business.manager?.()?.reloadIntegrations?.();
    const ok = Boolean(reloaded?.feishu?.enabled);
    log(ok ? "info" : "warn", "business_integrations_reloaded", {
      feishuEnabled: ok, connected: Boolean(reloaded?.feishu?.connected) });
  } catch (error) {
    log("warn", "business_reload_failed", { message: error?.message ?? String(error) });
  }

  // 把绑定线程登记进 manager 的会话池：飞书/HTTP 才有明确的投递目标。
  // 与 cli.mjs 的 adoptSession 同一条路径（409 adopt_conflict 视为已登记）。
  try {
    const response = await fetch(`http://${host}:${port}/manager/sessions/adopt`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify({ protocolVersion: "1.1.1-draft", messageId: randomUUID(),
        source: "hermes", threadId: thread.id, hostId: "local" }),
      signal: AbortSignal.timeout(35_000),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok) log("info", "session_adopted", { threadId: thread.id, action: body.action ?? null });
    else log("warn", "session_adopt_failed", { threadId: thread.id, status: response.status,
      error: body.error ?? null, detail: body.detail ?? null });
  } catch (error) {
    log("warn", "session_adopt_failed", { threadId: thread.id, message: error?.message ?? String(error) });
  }

  // ---- 任务看板：本 worker 自己的飞书身份 -----------------------------------
  // 只把两条生命周期消息发到训练员与这个 worker 的私聊（start / failed）：
  // 成功后 manager 自己在这条会话里的回复已经承载了正文，再发一条「完成」只是
  // 重复。router 的回执（router → trigger）不受影响。
  // 优先借 manager 已经建好的 notifier（它带着绑定的 token / canSend 守卫），
  // manager 不可用时才用凭据文件自建一个，避免插件形态下多一条长连接。
  const taskBoardDryRun = process.env.DSHWORKER_FEISHU_DRY_RUN === "1"
    || process.env.CODEXWORKER_FEISHU_DRY_RUN === "1";
  let taskBoardFallback = null;
  async function taskBoardNotifier() {
    const managerNotifier = business.manager?.()?.notifier;
    if (managerNotifier?.enabled) return managerNotifier;
    if (!feishu.value) return null;
    if (!taskBoardFallback) {
      const { FeishuNotifier } = await import("./feishu.mjs");
      taskBoardFallback = new FeishuNotifier({ appId: feishu.value.app_id,
        appSecret: feishu.value.app_secret, bridgeName: identity.display });
    }
    return taskBoardFallback.enabled ? taskBoardFallback : null;
  }
  async function taskBoardNotify(taskId, phase, fields = {}) {
    let text;
    let title;
    try {
      text = taskBoardText({ phase, taskId, ...fields });
      title = taskBoardTitle({ phase, taskId }) ?? `${identity.display} · 任务看板`;
    } catch (error) {
      log("warn", "task_board_failed", { taskId, phase, message: error?.message ?? String(error) });
      return null;
    }
    // 干跑只打日志不发，隔离实例的测试就永远不会投到训练员私聊里。
    if (taskBoardDryRun) {
      log("info", "task_board_dry_run", { taskId, phase, title, text: text.slice(0, 200),
        textChars: text.length,
        targetOpenIdSha256: createHash("sha256").update(String(taskBoardOpenId)).digest("hex").slice(0, 12) });
      return "dry-run";
    }
    if (!taskBoardEnabled) { log("warn", "task_board_skipped", { taskId, phase, reason: "no_target" }); return null; }
    const notifier = await taskBoardNotifier().catch(() => null);
    if (!notifier) { log("warn", "task_board_skipped", { taskId, phase, reason: "feishu_disabled" }); return null; }
    let delivery = null;
    try {
      const response = await notifier.sendMessage(text, { openId: taskBoardOpenId, receiveIdType: "open_id",
        title, onDelivery: (entry) => { delivery = entry; } });
      const messageId = response?.data?.message_id ?? delivery?.messageId ?? null;
      log(messageId ? "info" : "warn", "task_board_sent", { taskId, phase, messageId,
        apiCode: Number.isInteger(response?.code) ? response.code : null, deliveryPhase: delivery?.phase ?? null });
      return messageId;
    } catch (error) {
      log("warn", "task_board_failed", { taskId, phase, message: error?.message ?? String(error) });
      return null;
    }
  }

  // router（可选）：任务进入 manager 选中的同一个会话，和飞书共享上下文。
  const routerConfig = workerConfig.router ?? {};
  const routerEnabled = config.router !== false && process.env.CODEXWORKER_ROUTER !== "0"
    && routerConfig.enabled !== false;
  let routerClient = null;
  if (routerEnabled) {
    if (token.length < 24) {
      log("warn", "router_token_unavailable", { tokenFile, reason: "missing or too short" });
    } else {
      const { RouterClient } = await import("./router-client.mjs");
      const { PROTOCOL } = await import("./desktop-manager.mjs");
      const { routerMarker, routerMessageId, lastAssistantText, findRouterTurn, TERMINAL_TURN } =
        await import("./router-tasks.mjs");
      const routerUrl = String(process.env.CODEXWORKER_ROUTER_URL ?? routerConfig.url ?? "http://127.0.0.1:8790");
      let chain = Promise.resolve();
      const runTask = async (task) => {
        const taskId = stringArg(task?.task_id);
        const goal = stringArg(task?.goal);
        if (!taskId) return;
        const startedTask = Date.now();
        const marker = routerMarker(taskId);
        const messageId = routerMessageId(taskId);
        let status = "failed";
        let summary = "";
        let failure = "task runner error";
        log("info", "router_task_start", { taskId, messageId });
        // 开始行：训练员私聊里要能看见「来自超级溪流的指令」+ 目标原文。
        await taskBoardNotify(taskId, "start", { goal });
        try {
          if (!goal) throw new Error("router task has an empty goal");
          const manager = business.manager?.();
          if (!manager?.message) throw new Error("manager is not active");
          const selected = manager.state?.selectedThreadId && manager.state.sessions?.[manager.state.selectedThreadId]
            ? manager.state.selectedThreadId : thread.id;
          await manager.message({ protocolVersion: PROTOCOL, messageId, source: "hermes", origin: "router",
            text: `${marker}\n${goal}`, targetThreadId: selected, delivery: "steer" });
          const deadline = startedTask + 30 * 60 * 1000;
          let turn = null;
          while (Date.now() < deadline) {
            const snapshot = await transport.execute("read_thread", { threadId: selected, turnLimit: 30, includeOutputs: false });
            if (!turn) { turn = findRouterTurn(snapshot, marker); }
            const found = turn ? snapshot.turns.find((item) => item.id === turn.id) ?? null : null;
            if (found && TERMINAL_TURN.has(found.status)) { turn = found; break; }
            await sleep(1_000);
          }
          status = turn?.status === "completed" ? "done" : "failed";
          summary = lastAssistantText(turn) || (turn ? "" : "task turn never appeared");
          if (status !== "done") {
            failure = turn ? `turn ${turn.status}` : "task turn never appeared";
            summary = summary || failure;
          }
        } catch (error) {
          summary = error?.message ?? String(error);
          failure = summary;
          log("error", "router_task_failed", { taskId, message: summary });
        }
        // 成功不单独发：manager 在这条会话里的回复已经到训练员私聊了。
        const boardPhase = taskBoardTerminalPhase(status);
        if (boardPhase) await taskBoardNotify(taskId, boardPhase, { summary, failure });
        await routerClient.result({ taskId, status, summary }).catch((error) => {
          log("error", "router_result_failed", { taskId, message: error?.message ?? String(error) });
        });
      };
      routerClient = new RouterClient({
        url: routerUrl, token, name: identity.name, version: VERSION,
        subscriptions: normalizeSubscriptionConfig(workerConfig.subscriptions),
        logger: (level, event, fields = {}) => log(level, event, { component: "router", ...fields }),
      });
      routerClient.start({
        onTask: (task) => { chain = chain.then(() => runTask(task)).catch((error) =>
          log("error", "router_chain_error", { message: error?.message ?? String(error) })); return chain; },
        onConnect: ({ attempt, reconnects }) => log("info", "router_connected", { attempt, reconnects }),
        onDisconnect: ({ reason, delayMs, reconnects }) => log("warn", "router_disconnected", { reason, delayMs, reconnects }),
      });
      log("info", "router_client_started", { url: routerUrl, name: identity.name });
    }
  }
  return { business, routerClient };
}
