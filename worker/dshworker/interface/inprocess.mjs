import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { createHash, randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

import { ProgressWatch, noProgressTimeoutMs } from "../progress-watch.mjs";
import { createDshApprovalBridge } from "./approval-bridge.mjs";
import { installTaskControl, taskIdOf } from "./task-control.mjs";
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const VERSION = "0.3.0";
const TRANSPORT_NAME = "dsh-inprocess";
const NAMESPACE = "dsh";
const STATE_FILE_NAME = "dsh-plugin-state.json";
const SELF_TEST_FILE = "self-test.json";
const DEFAULT_WORKSPACE = path.join(os.homedir(), "projects");
const DEFAULT_STATE_DIR = path.join(os.homedir(), ".hermes", "state", "dshworker");
const MAX_TURNS_PER_THREAD = 200;
const MAX_PERSISTED_TEXT = 8_000;
const DEFAULT_TURN_TIMEOUT_MS = 0; // No implicit wall-clock limit.
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

export class InProcessTransport {
  constructor({ ctx, selection, cwd, stateDir, noProgressTimeoutMs: idleTimeoutMs, log, proxyHint = null }) {
    this.ctx = ctx;
    this.selection = selection;
    this.defaultCwd = cwd;
    this.stateFile = path.join(stateDir, STATE_FILE_NAME);
    this.noProgressTimeoutMs = noProgressTimeoutMs({ noProgressTimeoutMs: idleTimeoutMs });
    this.taskDeadlines = new Map();
    this.log = log;
    this.proxyHint = proxyHint;
    this.threads = new Map();
    this.live = new Map(); // threadId -> { handle, agent }
    this.closing = false;
    this.writeChain = Promise.resolve();
    this.approvals = createDshApprovalBridge({
      log: (level, event, fields) => this.log(level, event, fields),
    });
    this.stopApproval = ctx.on("approval/request", (request, next) => {
      const owned = [...this.live.entries()].find(([, value]) => this.agentsForTurn(value.agent).includes(request.agent));
      if (!owned) return next();
      const thread = this.threads.get(owned[0]);
      const turn = thread?.turns.find((item) => !TERMINAL_TURN_STATUSES.has(item.status));
      if (!turn) return Promise.resolve("rejected");
      const taskId = /^\[router (t-[^\]\s]+)\]/m.exec(turn.prompt)?.[1] ?? null;
      const last = request.agent.session.eventAt?.(request.agent.session.seq - 1);
      return this.approvals.request({ ...request, id: last?.type === "approval/asked" ? last.data.id : null }, {
        taskId, threadId: thread.id, turnId: turn.id,
        progress: (record) => {
          turn.approval = record;
          turn.approval_id = record.approval_id;
          turn.waiting_for_approval = record.state === "waiting_approval";
          turn.progress ??= [];
          if (record.text && !turn.progress.some((p) => p.approval_id === record.approval_id))
            turn.progress.push({ approval_id: record.approval_id, text: record.text });
          void this.save();
        },
      });
    }, { prepend: true });
    this.control = installTaskControl(this);
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
          turn.waiting_for_approval = false;
          if (turn.approval) turn.approval.state = "interrupted";
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
    turn.deadlineAt = this.taskDeadlines.get(taskIdOf(turn)) ?? null;
    thread.turns.push(turn);
    trimThread(thread);
    thread.updatedAt = turn.startedAt;
    if (this.control.isCancelled(taskIdOf(turn))) {
      turn.status = "interrupted";
      turn.cancelRequested = true;
      turn.error = "cancelled by router before dispatch";
      turn.reasonKind = "cancelled";
      turn.completedAt = new Date().toISOString();
      void this.save();
    } else {
      thread.status = "inProgress";
      if (thread.running) thread.queue.push(turn.id);
      else void this.startTurn(thread, turn);
    }
    return {
      threadId: thread.id, turnId: turn.id, accepted: true, status: turn.status,
      turn: { id: turn.id, status: turn.status, startedAt: turn.startedAt },
    };
  }

  // Registry ownership is exact live ancestry, independent of persisted session ids.
  agentsForTurn(root) {
    const registry = this.ctx.get?.("agents");
    const candidates = registry?.list?.() ?? [];
    const owned = new Set([root]);
    for (const parent of owned) {
      for (const candidate of candidates) {
        if (!owned.has(candidate) && registry.isOwnedBy?.(candidate.id, parent)) owned.add(candidate);
      }
    }
    return [...owned];
  }

  async startTurn(thread, turn) {
    thread.running = true;
    thread.status = "inProgress";
    let thinking = "", stopStream = null, agent = null, seenSeq = null;
    const watch = new ProgressWatch({ timeoutMs: this.noProgressTimeoutMs,
      deadline: turn.deadlineAt ?? Infinity });
    let rejectExpired;
    const expired = new Promise((_, reject) => { rejectExpired = reject; });
    const timer = setInterval(() => {
      if (agent) {
        const sequence = JSON.stringify(this.agentsForTurn(agent).map(subject => [subject.id ?? subject.session.id, subject.session.seq]));
        if (sequence !== seenSeq) {
          seenSeq = sequence; turn.eventSeq = (turn.eventSeq ?? 0) + 1; watch.touch();
        }
      }
      const failure = watch.failure();
      if (!failure) return;
      clearInterval(timer);
      this.log("warn", failure.reason === "stuck" ? "turn_stuck" : "turn_deadline",
        { threadId: thread.id, turnId: turn.id, message: failure.message });
      try { agent?.cancel({ kind: "user" }); } catch { /* report the liveness failure below */ }
      rejectExpired(Object.assign(new Error(failure.message), { reasonKind: failure.reason }));
    }, Math.max(1, Math.min(1000, this.noProgressTimeoutMs,
      Number.isFinite(watch.deadline) ? Math.max(1, watch.deadline - Date.now()) : 1000)));
    timer.unref?.();
    try {
      const work = async () => {
        agent = await this.ensureAgent(thread);
        // Initialization can finish after the liveness watchdog or router cancel.
        if (watch.failure() || turn.cancelRequested || this.closing) return null;
        seenSeq = JSON.stringify(this.agentsForTurn(agent).map(subject => [subject.id ?? subject.session.id, subject.session.seq]));
        stopStream = this.ctx.on("agent/assistant-stream", ({ agent: subject, frame }) => {
          if (!this.agentsForTurn(agent).includes(subject)) return;
          const chunk = frame?.type === "chunk" ? frame.chunk : null;
          if (chunk && chunk.type !== "heartbeat" && Object.entries(chunk).some(([key, value]) =>
              key !== "type" && value != null && value !== "")) {
            watch.touch(); turn.eventSeq = (turn.eventSeq ?? 0) + 1;
          }
          if (subject === agent && chunk?.type === "reasoning-delta" && typeof chunk.text === "string" && chunk.text) {
            thinking += chunk.text; turn.thinking = thinking;
          }
        });
        const fromSeq = agent.session.seq;
        agent.followup(userMessage(turn.prompt));
        const subagents = this.ctx.get("subagents");
        for (;;) {
          await agent.whenIdle();
          const idleSeq = agent.session.seq;
          if (subagents && await subagents.waitForChildren(agent)) continue;
          if (agent.status === "idle" && agent.session.seq === idleSeq) break;
        }
        const outcome = summarize(agent.session, fromSeq);
        try { await this.ctx.get("sessions")?.flush?.(agent.session); } catch { /* best effort */ }
        return outcome;
      };
      const outcome = await Promise.race([work(), expired]);
      if (!outcome) {
        const failure = watch.failure();
        this.finishTurn(thread, turn, { error: failure?.message ?? "cancelled by router",
          reasonKind: failure?.reason ?? "cancelled" });
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
      this.finishTurn(thread, turn, { thinking, error: message, reasonKind: error.reasonKind ?? "error" });
    } finally {
      clearInterval(timer);
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
      latestTurn: latest ? { id: latest.id, status: latest.status, eventSeq: latest.eventSeq ?? 0, ...approvalFields(latest),
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

  /**
   * 构造挂载 preset 的 setup 回调。
   * preset 只有在 Agent 的 setup 阶段 mount 才会挂上工具（bash / fs / web…）；
   * profile 里的 preset 声明本身不作用于 agents.create 建出来的 Agent。
   */
  async presetSetup() {
    const presets = this.ctx.get("agentPresets");
    if (!presets) return null;
    const wanted = "standard";
    let resolvedId;
    try {
      resolvedId = (await presets.resolve(wanted)).id;
    } catch (error) {
      this.log("warn", "preset_resolve_failed", {
        preset: wanted,
        message: error?.message ?? String(error),
      });
      return null;
    }
    this.log("info", "preset_mount_planned", { preset: resolvedId });
    return async (agentCtx) => {
      await presets.mount(agentCtx, resolvedId);
    };
  }

  async ensureAgent(thread) {
    const entry = this.live.get(thread.id);
    if (entry?.agent && entry.agent.status !== "disposed") return entry.agent;
    this.live.delete(thread.id);
    const agents = this.ctx.get("agents");
    const agentOptions = { ...this.selection };
    if (thread.sessionId) {
      try {
        const mountSetup = await this.presetSetup();
        const handle = await agents.resume({
          resumeSessionId: thread.sessionId,
          agentOptions,
          ...(mountSetup ? { setup: mountSetup } : {}),
        });
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
    // preset 只在 Agent 创建/恢复的 setup 里挂载才有工具（bash/fs/web…）。
    // 不挂的话 agent 只有 working_directory 一个工具，即使 profile 里配了 preset。
    const mountSetup = await this.presetSetup();
    const handle = await agents.create({
      sessionId,
      meta: { cwd: thread.cwd },
      agentOptions,
      ...(mountSetup ? { setup: mountSetup } : {}),
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
    this.approvals.close();
    this.stopApproval?.();
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

function approvalFields(turn) {
  return {
    approval_id: turn?.approval_id ?? null,
    waiting_for_approval: turn?.status === "inProgress" && turn?.approval?.state === "waiting_approval",
    approval: turn?.approval ? { ...turn.approval } : null,
    progress: (turn?.progress ?? []).map(p => ({ ...p })),
  };
}

function normalizeTurn(turn, maxChars) {
  return {
    id: turn.id, status: turn.status, eventSeq: turn.eventSeq ?? 0, ...approvalFields(turn), items: turnItems(turn, maxChars), itemsView: "full",
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
  // Approval progress has a separate stable item id; final replies keep their old id.
  for (const progress of turn.progress ?? []) {
    items.push({ type: "agentMessage", kind: "assistant",
      id: `${turn.id}-approval-${progress.approval_id}`,
      phase: "commentary", text: truncateText(progress.text, maxChars) });
  }
  // 等 turn 收尾再输出 assistant item：inProgress 阶段 scanner 会先扫到一次，
  // 那次如果把正文发出去，completed 时再整段发一遍，飞书上就是两条重复。
  // 返回里只带 user，scanner 对 trainer 类型不会往飞书发。
  const done = turn.status === "completed";
  if (done && (turn.text || turn.thinking)) {
    const parts = [];
    if (turn.thinking) {
      const thought = truncateText(turn.thinking, maxChars);
      parts.push(`💭 Reasoning:\n\n\`\`\`plaintext\n${thought}\n\`\`\``);
    }
    if (turn.text) parts.push(truncateText(turn.text, maxChars));
    items.push({ type: "agentMessage", kind: "assistant", id: `${turn.id}-agent`,
      text: parts.join("\n\n"), phase: "final_answer" });
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


export { VERSION, TRANSPORT_NAME, NAMESPACE, SELF_TEST_FILE, DEFAULT_WORKSPACE, DEFAULT_STATE_DIR, DEFAULT_TURN_TIMEOUT_MS, MAX_WAIT_MS, sleep, expandHome, stringArg, readJsonSync, writeJsonAtomic, createLogger, loadIdentity, loadConfig };
