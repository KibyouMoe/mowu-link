// NativeTransport · dsh 实现
//
// 协议：`dsh --profile headless --json "<prompt>"`，stdout 是 NDJSON 事件流
//       （DeepSeek Harness 0.2.2-rc.1，2026-11-10 在本机 macOS arm64 实测）：
//
//   {"type":"session","sessionId":"session-…","cwd":"/…"}
//   {"type":"status","phase":"turn_start"|"step_start"|"step_end"|"turn_end",…}
//   {"type":"thinking","text":"…"}
//   {"type":"text","text":"…"}
//   {"type":"final","sessionId":"…","reason":{"kind":"completed"|"error",…},"text":"…"}
//
// 续聊：`dsh --profile headless --json --session-id <sessionId> "<prompt>"`
//       （未知 id 会报错，不会新建会话）。
//
// 与 app-server 的关键差异：dsh 是一次性进程，没有长驻 JSON-RPC 管道。
// 所以 thread 记录由本文件自己维护（内存 + <stateDir>/dsh-transport-state.json），
// 每轮 send 起一个子进程，callTool 立即返回 inProgress，真正的完成状态由
// wait_threads / read_thread 轮询读取。
//
// 环境：子进程必须显式给 http 代理（socks 会让整轮卡死）、
//       DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic，并读入
//       ~/.hermes/.env 里的 DEEPSEEK_API_KEY。dsh 默认走 /usr/local/bin/dsh。
//
// 本文件只实现 transport 接口（listTools / callTool），不碰业务语义。
// 参考实现：./app-server.mjs；选择器：./index.mjs。
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";

export const name = "dsh";
// Capabilities the business layer adapts to. Anything not declared here keeps
// the Desktop-compatible default (true), so only genuine dsh gaps are listed.
//   fork           – no Desktop fork primitive; /fork must fail explicitly.
//   desktopJournal – dsh turns are owned by this transport, nothing is written
//                    to ~/.codex/sessions, so the public-journal relay is absent.
//   callerTurn     – a dsh turn is not a bound Desktop caller turn; wait_threads
//                    status must not require a live caller invocation.
export const capabilities = Object.freeze({ fork: false, desktopJournal: false, callerTurn: false });
export const requiresCallerTurn = capabilities.callerTurn;
export const PROTOCOL = "dsh/headless-json";
export const VERIFIED_WITH = "dsh 0.2.2-rc.1 (2026-11-10)";

const NAMESPACE = "dsh";
const DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic";
const DEFAULT_WORKSPACE = path.join(os.homedir(), "projects");
const DEFAULT_STATE_DIR = path.join(os.homedir(), ".hermes", "state", "dshworker");
const STATE_FILE_NAME = "dsh-transport-state.json";
const MAX_TURNS_PER_THREAD = 200;
const MAX_PERSISTED_TEXT = 8_000;
const MAX_STDERR_CHARS = 8_192;
const DEFAULT_WAIT_MS = 30_000;
const MAX_WAIT_MS = 180_000;
const DEFAULT_TURN_TIMEOUT_MS = 600_000;
const POLL_INTERVAL_MS = 200;
const TERMINAL_TURN_STATUSES = new Set(["completed", "interrupted", "failed", "cancelled", "canceled"]);

const TOOL_DEFINITIONS = [
  {
    name: "create_thread",
    namespace: NAMESPACE,
    description: "Create a local DSH thread record; `prompt` starts the first dsh turn.",
    inputSchema: {
      type: "object",
      properties: {
        title: { type: "string", description: "Optional thread title." },
        prompt: { type: "string", description: "Optional first user message." },
        cwd: { type: "string", description: `Working directory; defaults to ${DEFAULT_WORKSPACE}.` },
        model: { type: "string", description: "Accepted for contract compatibility; dsh picks its own model." },
      },
      required: [],
    },
  },
  {
    name: "list_threads",
    namespace: NAMESPACE,
    description: "List local dsh thread records (desktop-shaped payload).",
    inputSchema: {
      type: "object",
      properties: {
        limit: { type: "integer", minimum: 1, maximum: 100 },
        cursor: { type: "string" },
        searchTerm: { type: "string" },
        archived: { type: "boolean" },
      },
      required: [],
    },
  },
  {
    name: "read_thread",
    namespace: NAMESPACE,
    description: "Read a dsh thread record and its turns (items are desktop-shaped).",
    inputSchema: {
      type: "object",
      properties: {
        threadId: { type: "string" },
        turnLimit: { type: "integer", minimum: 0, maximum: 200 },
        includeOutputs: { type: "boolean" },
        maxOutputCharsPerItem: { type: "integer", minimum: 1 },
      },
      required: ["threadId"],
    },
  },
  {
    name: "send_message_to_thread",
    namespace: NAMESPACE,
    description: "Start a dsh turn for a thread; returns immediately with status `inProgress`.",
    inputSchema: {
      type: "object",
      properties: {
        threadId: { type: "string" },
        prompt: { type: "string" },
        input: { type: "array", description: "Raw UserInput[]; only `text` parts are used." },
        model: { type: "string" },
        cwd: { type: "string" },
      },
      required: ["threadId"],
    },
  },
  {
    name: "set_thread_title",
    namespace: NAMESPACE,
    description: "Rename a local dsh thread record.",
    inputSchema: {
      type: "object",
      properties: { threadId: { type: "string" }, title: { type: "string" } },
      required: ["threadId", "title"],
    },
  },
  {
    name: "wait_threads",
    namespace: NAMESPACE,
    description: "Wait for dsh turns to reach a terminal status; bounded by timeoutMs.",
    inputSchema: {
      type: "object",
      properties: {
        targets: {
          type: "array",
          items: {
            type: "object",
            properties: { threadId: { type: "string" }, hostId: { type: "string" } },
            required: ["threadId"],
          },
        },
        threadIds: { type: "array", items: { type: "string" }, description: "Shorthand for targets." },
        timeoutMs: { type: "integer", minimum: 0, maximum: MAX_WAIT_MS },
      },
      required: [],
    },
  },
  {
    name: "list_projects",
    namespace: NAMESPACE,
    description: "List local workspace projects derived from dsh thread cwds (desktop-shaped payload).",
    inputSchema: {
      type: "object",
      properties: { limit: { type: "integer", minimum: 1, maximum: 100 } },
      required: [],
    },
  },
];

// Tools the desktop contract knows about but dsh cannot express.
const KNOWN_UNSUPPORTED = new Set(["fork_thread", "navigate_to_codex_page"]);

// ---------------------------------------------------------------------------
// 公开接口
// ---------------------------------------------------------------------------

export function available() {
  return Boolean(findDshBinary());
}

export function unavailableReason() {
  return "dsh executable was not found (install DeepSeek Harness or set DSHWORKER_DSH_BIN)";
}

/** @returns {Promise<Array<{name: string, namespace: string, description: string}>>} */
export async function listTools() {
  return TOOL_DEFINITIONS.map((tool) => ({
    ...tool,
    inputSchema: structuredClone(tool.inputSchema),
  }));
}

/**
 * @param {string} toolName
 * @param {object} args
 * @param {{threadId?: string, turnId?: string, callId?: string}} invocation
 * @param {number} timeoutMs
 */
export async function callTool(toolName, args, invocation, timeoutMs = 5000) {
  if (KNOWN_UNSUPPORTED.has(toolName)) {
    return unsupported(toolName, `${toolName} is a Codex Desktop action; the dsh transport has no equivalent.`);
  }
  const definition = TOOL_DEFINITIONS.find((tool) => tool.name === toolName);
  if (!definition) {
    throw new DshError(`dsh transport does not implement native tool "${toolName}"`, "dsh_unsupported_tool");
  }
  if (!available()) throw new DshError(unavailableReason(), "dsh_unavailable");
  try {
    const payload = await store.execute(toolName, args ?? {}, timeoutMs);
    return success(toolName, payload);
  } catch (error) {
    throw normalizeToolError(toolName, error);
  }
}

export async function shutdown() {
  await store.shutdown();
}

// Synchronous last-resort cleanup for `process.on("exit")`.
export function killSync() {
  store.killChildren();
}

// ---------------------------------------------------------------------------
// 错误与响应封装
// ---------------------------------------------------------------------------

export class DshError extends Error {
  constructor(message, code = "dsh_error") {
    super(message);
    this.name = "DshError";
    this.code = code;
  }
}

function normalizeToolError(toolName, error) {
  if (error instanceof DshError) {
    return new DshError(`dsh ${toolName} failed: ${error.message} [${error.code}]`, error.code);
  }
  const message = error instanceof Error ? error.message : String(error);
  return new DshError(`dsh ${toolName} failed: ${message}`, "dsh_error");
}

function success(toolName, payload) {
  return {
    success: true,
    tool: toolName,
    namespace: NAMESPACE,
    contentItems: [{ type: "text", text: JSON.stringify(payload) }],
  };
}

function unsupported(toolName, reason) {
  return {
    success: false,
    tool: toolName,
    namespace: NAMESPACE,
    unsupported: true,
    contentItems: [{
      type: "text",
      text: JSON.stringify({ success: false, error: "unsupported_tool", tool: toolName, reason }),
    }],
  };
}

// ---------------------------------------------------------------------------
// thread 存储 + dsh 子进程
// ---------------------------------------------------------------------------

class DshTransport {
  constructor() {
    this.file = path.join(stateDir(), STATE_FILE_NAME);
    this.threads = new Map();
    this.children = new Map(); // turnId -> ChildProcess
    this.loaded = false;
    this.writeChain = Promise.resolve();
  }

  async execute(toolName, args, timeoutMs) {
    this.load();
    switch (toolName) {
      case "create_thread": return this.createThread(args, timeoutMs);
      case "list_threads": return this.listThreads(args);
      case "read_thread": return this.readThread(args);
      case "send_message_to_thread": return this.sendMessage(args);
      case "set_thread_title": return this.setThreadTitle(args);
      case "wait_threads": return this.waitThreads(args);
      case "list_projects": return this.listProjects(args);
      default: throw new DshError(`dsh transport does not implement native tool "${toolName}"`, "dsh_unsupported_tool");
    }
  }

  // ---- 状态持久化 ---------------------------------------------------------

  load() {
    if (this.loaded) return;
    this.loaded = true;
    let raw;
    try { raw = fs.readFileSync(this.file, "utf8"); }
    catch { return; } // 首次运行：没有状态文件是正常情况。
    let parsed;
    try { parsed = JSON.parse(raw); }
    catch { return; } // 损坏的状态文件不影响本进程；下一次 save 会覆盖。
    const threads = Array.isArray(parsed?.threads) ? parsed.threads : [];
    for (const record of threads) {
      if (!record?.id) continue;
      // 上一个进程的 inProgress 轮已经不可能还在跑。
      for (const turn of record.turns ?? []) {
        if (!TERMINAL_TURN_STATUSES.has(turn.status)) {
          turn.status = "interrupted";
          turn.error ??= "dsh worker restarted while this turn was running";
          turn.completedAt ??= new Date().toISOString();
        }
      }
      record.status = record.running ? "inProgress" : "idle";
      record.running = false;
      record.queue = [];
      this.threads.set(record.id, record);
    }
  }

  save() {
    const threads = [...this.threads.values()].map((thread) => ({
      id: thread.id,
      sessionId: thread.sessionId ?? null,
      title: thread.title ?? null,
      cwd: thread.cwd,
      createdAt: thread.createdAt,
      updatedAt: thread.updatedAt,
      turns: (thread.turns ?? []).map((turn) => ({
        ...turn,
        text: truncateText(turn.text, MAX_PERSISTED_TEXT),
        thinking: truncateText(turn.thinking, MAX_PERSISTED_TEXT),
      })),
    }));
    const payload = JSON.stringify({ version: 1, source: "dsh", savedAt: new Date().toISOString(), threads }, null, 2) + "\n";
    this.writeChain = this.writeChain.then(async () => {
      const { mkdir, rename, writeFile } = await import("node:fs/promises");
      await mkdir(path.dirname(this.file), { recursive: true });
      const temp = `${this.file}.${process.pid}.tmp`;
      await writeFile(temp, payload, { mode: 0o600 });
      await rename(temp, this.file);
    }).catch(() => {}); // 状态落盘失败不该打断一轮对话。
    return this.writeChain;
  }

  // ---- 工具实现 -----------------------------------------------------------

  async createThread(args) {
    const cwd = resolveWorkspace(args.cwd ?? args.target?.cwd);
    const title = stringArg(args.title) ?? stringArg(args.name);
    const thread = {
      id: randomUUID(),
      sessionId: null,
      title: title ?? null,
      cwd,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      status: "idle",
      running: false,
      queue: [],
      turns: [],
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
    await this.save();
    return payload;
  }

  sendMessage(args) {
    const thread = this.requireThread("send_message_to_thread", args);
    const prompt = stringArg(args.prompt) ?? stringArg(args.text) ?? stringArg(args.message)
      ?? textFromInput(args.input);
    if (!prompt) {
      throw new DshError("send_message_to_thread requires a non-empty `prompt` (or text `input`)", "dsh_missing_argument");
    }
    const turn = {
      id: randomUUID(),
      prompt,
      status: "inProgress",
      text: "",
      error: null,
      reasonKind: null,
      startedAt: new Date().toISOString(),
      completedAt: null,
    };
    thread.turns.push(turn);
    trimThread(thread);
    thread.updatedAt = turn.startedAt;
    thread.status = "inProgress";

    if (thread.running) {
      // 同一 thread 的 session 文件由 dsh 独占；连续消息排队，避免并发写同一会话。
      thread.queue.push(turn.id);
    } else {
      this.startTurn(thread, turn);
    }

    return {
      threadId: thread.id,
      turnId: turn.id,
      accepted: true,
      status: "inProgress",
      turn: { id: turn.id, status: turn.status, startedAt: turn.startedAt },
    };
  }

  startTurn(thread, turn) {
    thread.running = true;
    thread.status = "inProgress";
    const binary = findDshBinary();
    if (!binary) {
      this.finishTurn(thread, turn, { error: unavailableReason(), reasonKind: "unavailable" });
      return;
    }

    const args = ["--profile", dshProfile()];
    args.push("--json");
    if (thread.sessionId) args.push("--session-id", thread.sessionId);
    args.push(turn.prompt);

    let child;
    try {
      child = spawn(binary, args, {
        cwd: thread.cwd,
        env: dshEnv(),
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch (error) {
      this.finishTurn(thread, turn, { error: `failed to spawn ${binary}: ${error.message}`, reasonKind: "spawn_failed" });
      return;
    }
    this.children.set(turn.id, child);
    let stderr = "";
    let settled = false;
    let timedOut = false;
    const chunks = [];
    const thoughts = [];
    let finalText = null;
    let reason = null;

    const timeoutMs = dshTurnTimeoutMs();
    const timer = setTimeout(() => {
      timedOut = true;
      try { child.kill("SIGTERM"); } catch { /* already gone */ }
    }, timeoutMs);
    timer.unref?.();

    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk).slice(-MAX_STDERR_CHARS);
    });
    readline.createInterface({ input: child.stdout }).on("line", (line) => {
      if (!line.trim()) return;
      let message;
      try { message = JSON.parse(line); }
      catch { return; } // 非 JSON 行忽略，不污染这一轮。
      if (!message || typeof message !== "object") return;
      switch (message.type) {
        case "session":
          if (typeof message.sessionId === "string" && message.sessionId) {
            turn.sessionId = message.sessionId;
            thread.sessionId = message.sessionId;
            void this.save();
          }
          break;
        case "text":
          if (typeof message.text === "string" && message.text) chunks.push(message.text);
          break;
        case "thinking":
          // dsh 的思考过程是可见的，存成单独一条 commentary，不混进正文。
          // 边收边写回 turn，让 read_thread 轮询能把它当流式输出推给飞书。
          if (typeof message.text === "string" && message.text) {
            thoughts.push(message.text);
            turn.thinking = thoughts.join("");
          }
          break;
        case "final":
          if (typeof message.sessionId === "string" && message.sessionId) {
            turn.sessionId = message.sessionId;
            thread.sessionId = message.sessionId;
          }
          if (typeof message.text === "string") finalText = message.text;
          if (message.reason && typeof message.reason === "object") reason = message.reason;
          break;
        case "status":
          if (message.phase === "turn_end" && message.reason && typeof message.reason === "object") reason = message.reason;
          break;
        case "error":
          if (typeof message.message === "string") stderr = `${stderr}\n${message.message}`.slice(-MAX_STDERR_CHARS);
          break;
        default:
          break; // 其余（status 之外的未来新增事件）一律容忍。
      }
    });

    const settle = (exitCode, signal) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      this.children.delete(turn.id);
      const text = finalText ?? chunks.join("");
      const thinking = thoughts.join("");
      if (timedOut) {
        this.finishTurn(thread, turn, { text, thinking, error: `dsh turn timed out after ${timeoutMs}ms`, reasonKind: "timeout" });
        return;
      }
      const kind = reason?.kind ?? (exitCode === 0 ? "completed" : "failed");
      if (kind === "completed") {
        this.finishTurn(thread, turn, { text, thinking, reasonKind: "completed" });
        return;
      }
      const detail = reason?.error?.message
        ?? (exitCode === 0 ? kind : `dsh exited with code ${exitCode ?? "null"}${signal ? `, signal ${signal}` : ""}`);
      const tail = stderr.trim();
      this.finishTurn(thread, turn, { text, thinking, reasonKind: kind,
        error: tail ? `${detail}; stderr: ${tail.slice(-1500)}` : detail });
    };

    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      this.children.delete(turn.id);
      this.finishTurn(thread, turn, { error: `failed to run ${binary}: ${error.message}`, reasonKind: "spawn_failed" });
    });
    child.on("exit", (code, signal) => settle(code, signal));
  }

  finishTurn(thread, turn, { text = "", thinking = null, error = null, reasonKind = "failed" }) {
    turn.status = error ? "failed" : "completed";
    turn.text = text;
    // 思考与正文分开存：turnItems 会把思考渲染成 commentary，正文才是最终回复。
    if (thinking != null) turn.thinking = thinking;
    turn.error = error ?? null;
    turn.reasonKind = reasonKind;
    turn.completedAt = new Date().toISOString();
    thread.updatedAt = turn.completedAt;

    const nextId = thread.queue.shift();
    const next = nextId ? thread.turns.find((candidate) => candidate.id === nextId) : null;
    if (next) {
      // 排队中的下一轮继续跑；thread 保持 inProgress。
      this.save();
      this.startTurn(thread, next);
      return;
    }
    thread.running = false;
    thread.status = reasonKind === "completed" ? "idle" : "failed";
    this.save();
  }

  async waitThreads(args) {
    const targets = normalizeWaitTargets(args);
    const requested = Number(args.timeoutMs);
    const waitMs = Math.max(0, Math.min(Number.isFinite(requested) ? requested : DEFAULT_WAIT_MS, MAX_WAIT_MS));
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
          timedOut,
          snapshot: waitMs === 0,
          ...(timedOut ? { reason: `wait_threads timed out after ${waitMs}ms; returning current progress` } : {}),
          timeoutMs: waitMs,
        };
      }
      await sleep(Math.max(1, Math.min(POLL_INTERVAL_MS, deadline - Date.now())));
    }
  }

  pollThread(threadId, hostId) {
    const thread = this.threads.get(threadId);
    if (!thread) {
      return {
        thread: { id: threadId, hostId, title: null, cwd: null, status: null },
        latestTurn: null,
        latestAssistantMessage: null,
        cursor: null,
      };
    }
    const latest = thread.turns.at(-1) ?? null;
    return {
      thread: {
        id: thread.id,
        hostId,
        title: thread.title ?? null,
        cwd: thread.cwd,
        status: thread.status,
      },
      latestTurn: latest ? {
        id: latest.id,
        status: latest.status,
        startedAt: latest.startedAt ?? null,
        completedAt: latest.completedAt ?? null,
      } : null,
      latestAssistantMessage: latestAssistantMessage(latest),
      cursor: null,
    };
  }

  readThread(args) {
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

  setThreadTitle(args) {
    const thread = this.requireThread("set_thread_title", args);
    const title = stringArg(args.title) ?? stringArg(args.name);
    if (!title) throw new DshError("set_thread_title requires a non-empty `title`", "dsh_missing_argument");
    thread.title = title;
    thread.updatedAt = new Date().toISOString();
    void this.save();
    return { updated: true, threadId: thread.id, title };
  }

  listThreads(args) {
    const limit = clampInteger(args.limit, 1, 100, 20);
    const search = stringArg(args.searchTerm)?.toLowerCase() ?? null;
    const all = [...this.threads.values()]
      .map(normalizeThread)
      .filter((thread) => !search || `${thread.title ?? ""} ${thread.preview ?? ""}`.toLowerCase().includes(search))
      .sort((a, b) => (Date.parse(b.updatedAt ?? 0) || 0) - (Date.parse(a.updatedAt ?? 0) || 0) || a.id.localeCompare(b.id));
    return {
      schemaVersion: 4,
      source: NAMESPACE,
      pinnedThreads: [],
      threads: all.slice(0, limit),
      nextCursor: null,
      backwardsCursor: null,
    };
  }

  listProjects(args) {
    const limit = clampInteger(args.limit, 1, 100, 100);
    const threads = [...this.threads.values()].map(normalizeThread);
    const byPath = new Map();
    for (const thread of threads) {
      if (!thread?.cwd) continue;
      const key = path.resolve(thread.cwd);
      let project = byPath.get(key);
      if (!project) {
        project = {
          projectId: `dsh:${createHash("sha256").update(key).digest("hex").slice(0, 32)}`,
          kind: "local",
          hostId: "local",
          name: path.basename(key) || key,
          path: thread.cwd,
          threadCount: 0,
          lastUsedAt: null,
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
      .sort((a, b) =>
        (Date.parse(b.lastUsedAt ?? 0) || 0) - (Date.parse(a.lastUsedAt ?? 0) || 0) ||
        a.path.localeCompare(b.path))
      .slice(0, limit);
    return { schemaVersion: 1, source: NAMESPACE, projects };
  }

  // ---- 生命周期 -----------------------------------------------------------

  requireThread(toolName, args) {
    const threadId = stringArg(args.threadId);
    if (!threadId) throw new DshError(`${toolName} requires a non-empty string \`threadId\``, "dsh_missing_argument");
    const thread = this.threads.get(threadId);
    if (!thread) throw new DshError(`${toolName}: unknown dsh thread "${threadId}"`, "dsh_thread_not_found");
    return thread;
  }

  killChildren() {
    for (const child of this.children.values()) {
      try { child.kill("SIGTERM"); } catch { /* already gone */ }
    }
    this.children.clear();
  }

  async shutdown() {
    this.killChildren();
    await this.writeChain.catch(() => {});
  }
}

const store = new DshTransport();

// ---------------------------------------------------------------------------
// 纯函数辅助
// ---------------------------------------------------------------------------

function stateDir() {
  const configured = process.env.CODEXWORKER_STATE_DIR?.trim() || process.env.DSHWORKER_STATE_DIR?.trim();
  return path.resolve(expandHome(configured || DEFAULT_STATE_DIR));
}

function dshProfile() {
  return process.env.DSHWORKER_DSH_PROFILE?.trim() || "headless";
}

function dshTurnTimeoutMs() {
  const configured = Number(process.env.DSHWORKER_TURN_TIMEOUT_MS);
  return Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_TURN_TIMEOUT_MS;
}

function findDshBinary() {
  const configured = process.env.DSHWORKER_DSH_BIN?.trim() || process.env.DSH_BIN?.trim();
  if (configured) {
    try { fs.accessSync(configured, fs.constants.X_OK); return configured; }
    catch { return null; }
  }
  // ENOENT 过一次：优先用绝对路径，再按 PATH 查找兜底。
  const fixed = "/usr/local/bin/dsh";
  try { fs.accessSync(fixed, fs.constants.X_OK); return fixed; }
  catch { /* fall through to PATH */ }
  const pathValue = process.env.PATH ?? "";
  const names = process.platform === "win32" ? ["dsh.exe", "dsh.cmd", "dsh.bat", "dsh"] : ["dsh"];
  for (const directory of pathValue.split(path.delimiter)) {
    if (!directory) continue;
    for (const name of names) {
      const candidate = path.join(directory, name);
      try { fs.accessSync(candidate, fs.constants.X_OK); return candidate; }
      catch { /* keep looking */ }
    }
  }
  return null;
}

function dshEnv() {
  const env = { ...process.env };
  for (const [key, value] of readEnvFile(envFilePath())) env[key] = value;

  // dsh 只走 http 代理；socks（ALL_PROXY）会让整轮超时。
  delete env.ALL_PROXY;
  delete env.all_proxy;

  const explicit = process.env.DSHWORKER_PROXY?.trim() || process.env.CODEXWORKER_APP_SERVER_PROXY?.trim();
  const inherited = env.HTTPS_PROXY?.trim() || env.https_proxy?.trim();
  // 只信任回环代理：父进程可能带着局域网地址（如 http://192.168.x.x:10808），
  // 对 api.deepseek.com 会 5s 超时，整轮直接 TRANSPORT 失败。
  const loopback = /^https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?\/?$/i.test(inherited ?? "");
  // 没有显式/回环代理就不注入：本机全局走 FlClash TUN（utun4），
  // 再套一层 shell 级代理会让 dsh 的 HTTP 客户端全超时。
  const proxy = explicit || (loopback ? inherited : null);
  if (proxy) {
    env.HTTPS_PROXY = proxy;
    env.https_proxy = proxy;
    env.HTTP_PROXY = explicit || (/^http:\/\//i.test(env.HTTP_PROXY ?? "") ? env.HTTP_PROXY : proxy);
    env.http_proxy = env.HTTP_PROXY;
    env.NODE_USE_ENV_PROXY = "1";
  } else {
    for (const key of ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"]) delete env[key];
    delete env.NODE_USE_ENV_PROXY;
  }

  // dsh 的 DeepSeek 客户端走 Anthropic 兼容端点；~/.hermes/.env 里的
  // DEEPSEEK_BASE_URL 是原生端点，必须覆盖（否则 Messages 请求 404）。
  const configuredBase = process.env.DSHWORKER_DEEPSEEK_BASE_URL?.trim();
  const inheritedBase = process.env.DEEPSEEK_BASE_URL?.trim();
  env.DEEPSEEK_BASE_URL = configuredBase
    || (inheritedBase && /\/anthropic\/?$/.test(inheritedBase) ? inheritedBase : DEFAULT_DEEPSEEK_BASE_URL);

  // GUI 启动的进程可能没有 /usr/local/bin；dsh 依赖它自己的 node 运行时。
  const segments = (env.PATH ?? "").split(path.delimiter).filter(Boolean);
  for (const directory of ["/usr/local/bin", "/opt/homebrew/bin"]) {
    if (!segments.includes(directory)) segments.push(directory);
  }
  env.PATH = segments.join(path.delimiter);
  return env;
}

function envFilePath() {
  const configured = process.env.DSHWORKER_ENV_FILE?.trim();
  return path.resolve(expandHome(configured || path.join(os.homedir(), ".hermes", ".env")));
}

function readEnvFile(file) {
  const values = new Map();
  let raw;
  try { raw = fs.readFileSync(file, "utf8"); }
  catch { return values; }
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(trimmed);
    if (!match) continue;
    let value = match[2].trim();
    if ((value.startsWith("\"") && value.endsWith("\"")) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    values.set(match[1], value);
  }
  return values;
}

function resolveWorkspace(value) {
  const candidate = stringArg(value);
  const resolved = path.resolve(expandHome(candidate || DEFAULT_WORKSPACE));
  let stat;
  try { stat = fs.statSync(resolved); }
  catch { throw new DshError(`workspace does not exist: ${resolved}`, "dsh_invalid_cwd"); }
  if (!stat.isDirectory()) throw new DshError(`workspace is not a directory: ${resolved}`, "dsh_invalid_cwd");
  return resolved;
}

function expandHome(value) {
  if (typeof value !== "string" || !value) return value;
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

function normalizeThread(thread) {
  const turns = thread.turns ?? [];
  const last = turns.at(-1) ?? null;
  return {
    id: thread.id,
    kind: "dsh",
    hostId: "local",
    title: thread.title ?? null,
    name: thread.title ?? null,
    preview: last?.prompt ? String(last.prompt).slice(0, 200) : "",
    cwd: thread.cwd ?? null,
    createdAt: thread.createdAt ?? null,
    updatedAt: thread.updatedAt ?? null,
    recencyAt: thread.updatedAt ?? null,
    status: thread.status ?? "idle",
    source: "dsh",
    model: null,
    modelProvider: "deepseek",
    path: null,
    ephemeral: false,
    forkedFromId: null,
    sessionId: thread.sessionId ?? null,
  };
}

function normalizeTurn(turn, maxChars) {
  const items = turnItems(turn, maxChars);
  return {
    id: turn.id,
    status: turn.status,
    items,
    itemsView: "full",
    startedAt: turn.startedAt ?? null,
    completedAt: turn.completedAt ?? null,
    durationMs: turn.startedAt && turn.completedAt
      ? Math.max(0, Date.parse(turn.completedAt) - Date.parse(turn.startedAt)) : null,
    error: turn.error ?? null,
    role: "assistant",
    text: turn.text ?? "",
  };
}

function turnItems(turn, maxChars) {
  const items = [{
    type: "userMessage",
    kind: "trainer",
    id: `${turn.id}-user`,
    text: truncateText(turn.prompt ?? "", maxChars),
  }];
  // 等 turn 收尾再输出 assistant item：inProgress 阶段 scanner 会先扫到一次，
  // 那次如果把正文发出去，completed 时再整段发一遍，飞书上就是两条重复。
  // 思考和正文必须同一条一起发——拆开的话，万一后一个 turn 就是最后一个，
  // 前面那条「只含思考」的消息就白白重复了。
  const done = turn.status === "completed";
  if (done && (turn.text || turn.thinking)) {
    const parts = [];
    if (turn.thinking) {
      const thought = truncateText(turn.thinking, maxChars);
      parts.push(`💭 Reasoning:\n\n\`\`\`plaintext\n${thought}\n\`\`\``);
    }
    if (turn.text) parts.push(truncateText(turn.text, maxChars));
    items.push({
      type: "agentMessage",
      kind: "assistant",
      id: `${turn.id}-agent`,
      text: parts.join("\n\n"),
      phase: "final_answer",
    });
  }
  if (turn.error) {
    items.push({ type: "error", id: `${turn.id}-error`, text: turn.error });
  }
  return items;
}

function latestAssistantMessage(turn) {
  if (!turn?.text) return null;
  return {
    id: `${turn.id}-agent`,
    turnId: turn.id,
    text: turn.text,
    phase: turn.status === "completed" ? "final_answer" : null,
    truncated: false,
  };
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
    if (!threadId) throw new DshError("wait_threads target is missing a string `threadId`", "dsh_missing_argument");
    targets.push({ threadId, hostId: stringArg(target?.hostId) ?? "local" });
  }
  if (!targets.length) throw new DshError("wait_threads requires at least one target thread", "dsh_missing_argument");
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

function truncateText(value, maxChars) {
  if (typeof value !== "string") return value;
  if (!maxChars || value.length <= maxChars) return value;
  return value.slice(0, maxChars);
}

function stringArg(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function clampInteger(value, minimum, maximum, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.trunc(number)));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

process.once("exit", () => store.killChildren());
