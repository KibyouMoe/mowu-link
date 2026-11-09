// NativeTransport · app-server 实现
//
// 协议：`codex app-server` 的 JSON-RPC 2.0 over stdio（Codex CLI 0.167.0，
//       app-server v2 schema，2026-11-09 在本机 macOS arm64 实测）。
// 实测链路：initialize → initialized → thread/start → turn/start →
//          turn/completed（通知）→ thread/read → thread/name/set。
//
// 协议事实（实测，不要再猜）：
//   - 每行一个 JSON；响应按 id 匹配，通知没有 id，未知通知必须容忍。
//   - initialize 之后必须补发 `{"method":"initialized","params":{}}`，否则
//     服务端不会进入正常工作状态。
//   - turn/start 立即返回 inProgress；最终内容通过 item/started、
//     item/completed、turn/completed 通知到达，turn/completed 自带摘要 items。
//   - thread/read 默认只返回元数据；includeTurns:true 才带完整 turns
//     （paginated 线程会附带 deprecationNotice，但实测可用）。
//   - 读不到 turns 时用 thread/items/list 兜底（本文件已实现）。
//
// 代理：本机 codex 只认 http 代理。spawn 时显式设置
//   HTTPS_PROXY=http://127.0.0.1:10808，并删除 ALL_PROXY/all_proxy（socks 会让
//   整轮超时）。可用 CODEXWORKER_APP_SERVER_PROXY 覆盖代理地址，
//   CODEXWORKER_CODEX_BIN 指定 codex 可执行文件。
//
// 本文件只实现 transport 接口（listTools / callTool），不碰业务语义。
// 参考实现：./desktop-pipe.mjs；选择器：./index.mjs。
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";

export const name = "app-server";
export const PROTOCOL = "app-server/v2";
export const VERIFIED_WITH = "codex-cli 0.167.0 (2026-11-09)";

const CLIENT_INFO = { name: "codexworker", version: "1.0.0", title: "CodexWorker" };
const DEFAULT_PROXY = "http://127.0.0.1:10808";
const STARTUP_TIMEOUT_MS = 20_000;
const CONTROL_TIMEOUT_FLOOR_MS = 15_000;
const READ_TIMEOUT_FLOOR_MS = 60_000;
const DEFAULT_WAIT_MS = 30_000;
const MAX_WAIT_MS = 180_000;
const MAX_TURNS_PER_THREAD = 200;
const MAX_STDERR_CHARS = 8_192;
const TERMINAL_TURN_STATUSES = new Set(["completed", "interrupted", "failed", "cancelled", "canceled"]);

const TOOL_DEFINITIONS = [
  {
    name: "create_thread",
    namespace: "app-server",
    description: "Create a Codex thread via app-server `thread/start`.",
    inputSchema: {
      type: "object",
      properties: {
        title: { type: "string", description: "Optional thread title (`thread/name/set`)." },
        prompt: { type: "string", description: "Optional first user message (`turn/start`)." },
        cwd: { type: "string", description: "Working directory; defaults to the worker process cwd." },
        model: { type: "string" },
        approvalPolicy: { type: "string", description: "Defaults to `never` for unattended CLI runs." },
        sandbox: { type: "string", description: "`read-only` | `workspace-write` | `danger-full-access`." },
      },
      required: [],
    },
  },
  {
    name: "fork_thread",
    namespace: "app-server",
    description: "Fork an existing thread via app-server `thread/fork`.",
    inputSchema: {
      type: "object",
      properties: { threadId: { type: "string" }, model: { type: "string" }, cwd: { type: "string" } },
      required: ["threadId"],
    },
  },
  {
    name: "list_threads",
    namespace: "app-server",
    description: "List threads via app-server `thread/list` (desktop-shaped payload).",
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
    namespace: "app-server",
    description: "Read a thread and its turns via `thread/read`, falling back to `thread/items/list`.",
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
    namespace: "app-server",
    description: "Send a user message to a thread via app-server `turn/start`.",
    inputSchema: {
      type: "object",
      properties: {
        threadId: { type: "string" },
        prompt: { type: "string" },
        input: { type: "array", description: "Raw app-server UserInput[]; overrides prompt." },
        model: { type: "string" },
        effort: { type: "string" },
        cwd: { type: "string" },
        approvalPolicy: { type: "string" },
      },
      required: ["threadId"],
    },
  },
  {
    name: "set_thread_title",
    namespace: "app-server",
    description: "Rename a thread via app-server `thread/name/set`.",
    inputSchema: {
      type: "object",
      properties: { threadId: { type: "string" }, title: { type: "string" } },
      required: ["threadId", "title"],
    },
  },
  {
    name: "wait_threads",
    namespace: "app-server",
    description: "Wait for target turns using `turn/completed` notifications; bounded by timeoutMs.",
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
        timeoutMs: { type: "integer", minimum: 0, maximum: MAX_WAIT_MS },
      },
      required: [],
    },
  },
  {
    name: "navigate_to_codex_page",
    namespace: "app-server",
    description: "Desktop-only UI navigation; app-server has no equivalent.",
    inputSchema: { type: "object", properties: { page: { type: "string" } }, required: [] },
  },
  {
    name: "list_projects",
    namespace: "app-server",
    description: "List local workspace projects derived from app-server `thread/list` (desktop-shaped payload).",
    inputSchema: {
      type: "object",
      properties: { limit: { type: "integer", minimum: 1, maximum: 100 } },
      required: [],
    },
  },
];

// ---------------------------------------------------------------------------
// 公开接口
// ---------------------------------------------------------------------------

export function available() {
  return Boolean(findCodexBinary());
}

export function unavailableReason() {
  return "codex executable was not found on PATH (install Codex CLI or set CODEXWORKER_CODEX_BIN)";
}

/** @returns {Promise<Array<{name: string, namespace: string, description: string}>>} */
export async function listTools() {
  // app-server 没有 tools/list；这是 Desktop app tools 的固定对照表。
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
  const definition = TOOL_DEFINITIONS.find((tool) => tool.name === toolName);
  if (!definition) {
    throw new AppServerError(
      `app-server transport does not implement native tool "${toolName}"`,
      "app_server_unsupported_tool",
    );
  }
  if (toolName === "navigate_to_codex_page") {
    return unsupported(toolName, "navigate_to_codex_page is a Codex Desktop UI action; the app-server CLI backend has no page to navigate.");
  }
  if (!available()) throw new AppServerError(unavailableReason(), "app_server_unavailable");
  try {
    const payload = await client.execute(toolName, args ?? {}, timeoutMs);
    return success(toolName, payload);
  } catch (error) {
    throw normalizeToolError(toolName, error);
  }
}

export async function shutdown() {
  await client.shutdown();
}

// Synchronous last-resort cleanup for `process.on("exit")`: the child must not
// outlive a CLI process that is exiting without awaiting async shutdown.
export function killSync() {
  client.killChild();
}

// ---------------------------------------------------------------------------
// 错误与响应封装
// ---------------------------------------------------------------------------

export class AppServerError extends Error {
  constructor(message, code = "app_server_error") {
    super(message);
    this.name = "AppServerError";
    this.code = code;
  }
}

function normalizeToolError(toolName, error) {
  if (error instanceof AppServerError) {
    return new AppServerError(`app-server ${toolName} failed: ${error.message} [${error.code}]`, error.code);
  }
  const message = error instanceof Error ? error.message : String(error);
  return new AppServerError(`app-server ${toolName} failed: ${message}`, "app_server_error");
}

function success(toolName, payload) {
  return {
    success: true,
    tool: toolName,
    namespace: "app-server",
    contentItems: [{ type: "text", text: JSON.stringify(payload) }],
  };
}

function unsupported(toolName, reason) {
  // 明确、可序列化的 unsupported 响应：上层能读到 reason，不会拿到 undefined。
  return {
    success: false,
    tool: toolName,
    namespace: "app-server",
    unsupported: true,
    contentItems: [{
      type: "text",
      text: JSON.stringify({ success: false, error: "unsupported_tool", tool: toolName, reason }),
    }],
  };
}

// ---------------------------------------------------------------------------
// 单例 client：进程、请求 id、通知分发、线程态
// ---------------------------------------------------------------------------

class AppServerClient {
  constructor() {
    this.child = null;
    this.ready = false;
    this.startPromise = null;
    this.nextId = 0;
    this.pending = new Map();
    this.waiters = new Set();
    this.stderr = "";
    this.serverInfo = null;
    this.threads = new Map();
    // Threads loaded in THIS app-server process. turn/start rejects a thread
    // that exists on disk but was never started/resumed here ("thread not
    // found"), which is exactly the state after a CLI restart.
    this.loadedThreads = new Set();
  }

  async execute(toolName, args, timeoutMs) {
    switch (toolName) {
      case "create_thread": return this.createThread(args, timeoutMs);
      case "fork_thread": return this.forkThread(args, timeoutMs);
      case "list_threads": return this.listThreads(args, timeoutMs);
      case "read_thread": return this.readThread(args, timeoutMs);
      case "send_message_to_thread": return this.sendMessage(args, timeoutMs);
      case "set_thread_title": return this.setThreadTitle(args, timeoutMs);
      case "wait_threads": return this.waitThreads(args, timeoutMs);
      case "list_projects": return this.listProjects(args, timeoutMs);
      default: throw new AppServerError(`app-server transport does not implement native tool "${toolName}"`, "app_server_unsupported_tool");
    }
  }

  // ---- 进程管理 -----------------------------------------------------------

  async ensureStarted() {
    if (this.ready && this.child) return;
    if (this.startPromise) return this.startPromise;
    this.startPromise = this.start().catch((error) => {
      this.startPromise = null;
      throw error;
    });
    return this.startPromise;
  }

  async start() {
    const binary = findCodexBinary();
    if (!binary) throw new AppServerError(unavailableReason(), "app_server_unavailable");
    const child = spawn(binary, ["app-server"], {
      stdio: ["pipe", "pipe", "pipe"],
      env: appServerEnv(),
      windowsHide: true,
    });
    this.child = child;
    this.ready = false;
    this.stderr = "";
    this.serverInfo = null;

    child.on("error", (error) => {
      if (this.child !== child) return;
      this.handleExit(child, null, null, new AppServerError(`failed to spawn ${binary}: ${error.message}`, "app_server_spawn_failed"));
    });
    child.stderr.on("data", (chunk) => {
      this.stderr = (this.stderr + chunk.toString("utf8")).slice(-MAX_STDERR_CHARS);
    });
    child.stdin.on("error", () => {
      // EPIPE 由 exit 处理器统一转成明确错误；这里只防止未捕获异常。
    });
    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => this.handleLine(line));
    child.on("exit", (code, signal) => this.handleExit(child, code, signal));

    try {
      const init = await this.request("initialize", { clientInfo: CLIENT_INFO }, STARTUP_TIMEOUT_MS);
      this.notify("initialized", {});
      this.serverInfo = init ?? null;
      this.ready = true;
      this.startPromise = null;
    } catch (error) {
      this.killChild(child);
      this.child = null;
      this.ready = false;
      throw new AppServerError(
        `codex app-server startup failed: ${error.message}${this.stderrTail()}`,
        error instanceof AppServerError ? error.code : "app_server_startup_failed",
      );
    }
  }

  handleExit(child, code, signal, cause = null) {
    if (this.child !== child) return;
    this.child = null;
    this.ready = false;
    this.startPromise = null;
    const detail = cause
      ? cause.message
      : `codex app-server exited (code ${code ?? "null"}${signal ? `, signal ${signal}` : ""})`;
    const error = new AppServerError(`${detail}${this.stderrTail()}`, "app_server_exited");
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      this.pending.delete(id);
      pending.reject(error);
    }
    for (const waiter of [...this.waiters]) {
      clearTimeout(waiter.timer);
      this.waiters.delete(waiter);
      waiter.reject(error);
    }
  }

  killChild(child = this.child) {
    if (!child) return;
    try { child.stdin.end(); } catch { /* already closed */ }
    try { child.kill("SIGTERM"); } catch { /* already gone */ }
  }

  async shutdown() {
    const child = this.child;
    this.child = null;
    this.ready = false;
    this.startPromise = null;
    const error = new AppServerError("app-server transport was shut down", "app_server_shutdown");
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      this.pending.delete(id);
      pending.reject(error);
    }
    for (const waiter of [...this.waiters]) {
      clearTimeout(waiter.timer);
      this.waiters.delete(waiter);
      waiter.reject(error);
    }
    if (!child) return;
    this.killChild(child);
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 1500);
      child.once("exit", () => { clearTimeout(timer); resolve(); });
    });
  }

  stderrTail() {
    const tail = this.stderr.trim();
    return tail ? `; stderr: ${tail.slice(-2000)}` : "";
  }

  // ---- JSON-RPC -----------------------------------------------------------

  request(method, params, timeoutMs = CONTROL_TIMEOUT_FLOOR_MS) {
    if (!this.child) {
      return Promise.reject(new AppServerError(`app-server is not running; cannot send ${method}`, "app_server_not_running"));
    }
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new AppServerError(`app-server request ${method} timed out after ${timeoutMs}ms`, "app_server_timeout"));
      }, Math.max(1, timeoutMs));
      this.pending.set(id, { resolve, reject, timer, method });
      try {
        this.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new AppServerError(`failed to write ${method} to app-server: ${error.message}`, "app_server_write_failed"));
      }
    });
  }

  notify(method, params) {
    if (!this.child) return;
    try {
      this.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method, params })}\n`);
    } catch {
      // 进程已退出；下一次 request 会给出明确错误。
    }
  }

  handleLine(line) {
    if (!line.trim()) return;
    let message;
    try { message = JSON.parse(line); }
    catch { return; } // 非 JSON 行（极少见）忽略，不污染协议状态。
    if (message == null || typeof message !== "object") return;

    if (message.id !== undefined && message.method) {
      // 服务端主动请求（审批、elicitation、动态工具等）。本 transport
      // 不实现这些交互，明确拒绝而不是让整轮永远挂着。
      this.respondUnsupported(message);
      return;
    }
    if (message.id !== undefined && this.pending.has(message.id)) {
      const pending = this.pending.get(message.id);
      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      if (message.error) {
        pending.reject(new AppServerError(
          `app-server ${pending.method} rejected: ${message.error.message ?? JSON.stringify(message.error)}`,
          "app_server_rpc_error",
        ));
      } else {
        pending.resolve(message.result);
      }
      return;
    }
    if (typeof message.method === "string") {
      try { this.handleNotification(message.method, message.params ?? {}); }
      catch { /* 通知处理失败不影响协议；状态缺失会在调用时显式报错。 */ }
    }
  }

  respondUnsupported(message) {
    if (!this.child) return;
    try {
      this.child.stdin.write(`${JSON.stringify({
        jsonrpc: "2.0",
        id: message.id,
        error: { code: -32601, message: `codexworker app-server transport does not implement server request ${message.method}` },
      })}\n`);
    } catch { /* ignore */ }
  }

  // ---- 通知与线程态 -------------------------------------------------------

  handleNotification(method, params) {
    const threadId = typeof params.threadId === "string" ? params.threadId : null;
    switch (method) {
      case "thread/started":
        this.rememberThread(params.thread);
        break;
      case "thread/name/updated":
        if (threadId && this.threads.has(threadId)) {
          this.threads.get(threadId).thread = {
            ...(this.threads.get(threadId).thread ?? { id: threadId }),
            name: params.threadName ?? null,
            title: params.threadName ?? null,
          };
        }
        break;
      case "turn/started":
        if (threadId && params.turn?.id) this.applyTurn(threadId, params.turn);
        break;
      case "turn/completed":
        if (threadId && params.turn?.id) {
          this.applyTurn(threadId, params.turn);
          this.resolveWaiters(threadId, params.turn.id);
        }
        break;
      case "item/started":
      case "item/completed":
        if (threadId && params.turnId && params.item) {
          this.applyItem(threadId, params.turnId, params.item, method === "item/completed");
        }
        break;
      case "item/agentMessage/delta":
        if (threadId && params.turnId && params.itemId && typeof params.delta === "string") {
          this.applyDelta(threadId, params.turnId, params.itemId, params.delta);
        }
        break;
      case "thread/closed":
        // 保留缓存：thread/read 仍可读，历史不需要丢。
        break;
      default:
        // remoteControl/status/changed、mcpServer/startupStatus/updated、
        // account/updated、deprecationNotice 等未知通知一律容忍。
        break;
    }
  }

  rememberThread(thread) {
    if (!thread?.id) return;
    const state = this.ensureThread(thread.id);
    state.thread = { ...(state.thread ?? {}), ...thread };
  }

  ensureThread(threadId) {
    let state = this.threads.get(threadId);
    if (!state) {
      state = { thread: { id: threadId }, turns: [], turnMap: new Map(), items: new Map() };
      this.threads.set(threadId, state);
    }
    return state;
  }

  applyTurn(threadId, incoming) {
    if (!incoming?.id) return;
    const state = this.ensureThread(threadId);
    let turn = state.turnMap.get(incoming.id);
    if (!turn) {
      turn = {
        id: incoming.id,
        rootTurnId: incoming.rootTurnId ?? null,
        status: incoming.status ?? "inProgress",
        items: [],
        itemsView: incoming.itemsView ?? "notLoaded",
        startedAt: incoming.startedAt ?? null,
        completedAt: incoming.completedAt ?? null,
        durationMs: incoming.durationMs ?? null,
        error: incoming.error ?? null,
      };
      state.turnMap.set(turn.id, turn);
      state.turns.push(turn);
    }
    if (incoming.status) turn.status = incoming.status;
    if (incoming.itemsView) turn.itemsView = incoming.itemsView;
    for (const key of ["rootTurnId", "startedAt", "completedAt", "durationMs", "error"]) {
      if (incoming[key] !== undefined) turn[key] = incoming[key];
    }
    const incomingItems = Array.isArray(incoming.items) ? incoming.items : [];
    for (const item of incomingItems) this.mergeItem(turn, item);
    if (incomingItems.length && turn.itemsView !== "full") turn.itemsView = "summary";
    this.trimThread(state);
  }

  applyItem(threadId, turnId, item, completed) {
    if (!item?.id) return;
    const state = this.ensureThread(threadId);
    let turn = state.turnMap.get(turnId);
    if (!turn) {
      turn = { id: turnId, rootTurnId: null, status: "inProgress", items: [], itemsView: "full",
        startedAt: null, completedAt: null, durationMs: null, error: null };
      state.turnMap.set(turnId, turn);
      state.turns.push(turn);
    }
    if (turn) this.mergeItem(turn, item, completed);
  }

  mergeItem(turn, item, completed = false) {
    if (!item?.id) return;
    const index = turn.items.findIndex((candidate) => candidate.id === item.id);
    if (index < 0) {
      turn.items.push({ ...item });
      return;
    }
    const existing = turn.items[index];
    const merged = { ...existing, ...item };
    // item/started 可能带空文本；item/completed 才是最终值。流式 delta
    // 已经累积过文本时，不能被一个空的中间态覆盖。
    if (existing.type === "agentMessage" && !item.text && existing.text) merged.text = existing.text;
    if (existing.type === "userMessage" && !(item.content?.length) && existing.content?.length) merged.content = existing.content;
    turn.items[index] = merged;
    if (completed) turn.itemsView = "full";
  }

  applyDelta(threadId, turnId, itemId, delta) {
    const state = this.ensureThread(threadId);
    const turn = state.turnMap.get(turnId);
    if (!turn) return;
    const item = turn.items.find((candidate) => candidate.id === itemId);
    if (item) item.text = `${item.text ?? ""}${delta}`;
    else turn.items.push({ type: "agentMessage", id: itemId, text: delta, phase: null });
  }

  trimThread(state) {
    while (state.turns.length > MAX_TURNS_PER_THREAD) {
      const dropped = state.turns.shift();
      state.turnMap.delete(dropped.id);
    }
  }

  latestTurn(threadId) {
    return this.threads.get(threadId)?.turns.at(-1) ?? null;
  }

  findTurn(threadId, turnId) {
    return this.threads.get(threadId)?.turnMap.get(turnId) ?? null;
  }

  resolveWaiters(threadId, turnId) {
    for (const waiter of [...this.waiters]) {
      if (waiter.threadId !== threadId || waiter.turnId !== turnId) continue;
      clearTimeout(waiter.timer);
      this.waiters.delete(waiter);
      waiter.resolve(true);
    }
  }

  waitForTurn(threadId, turnId, timeoutMs) {
    const current = this.findTurn(threadId, turnId);
    if (current && TERMINAL_TURN_STATUSES.has(current.status)) return Promise.resolve(true);
    if (timeoutMs <= 0) return Promise.resolve(false);
    return new Promise((resolve, reject) => {
      const waiter = { threadId, turnId, resolve, reject, timer: null };
      waiter.timer = setTimeout(() => {
        this.waiters.delete(waiter);
        resolve(false);
      }, Math.min(timeoutMs, MAX_WAIT_MS));
      this.waiters.add(waiter);
      // 注册后再检查一次，避免完成通知落在快照与注册之间。
      const after = this.findTurn(threadId, turnId);
      if (after && TERMINAL_TURN_STATUSES.has(after.status)) {
        clearTimeout(waiter.timer);
        this.waiters.delete(waiter);
        resolve(true);
      }
    });
  }

  // ---- 工具实现 -----------------------------------------------------------

  async createThread(args, timeoutMs) {
    const params = {
      cwd: stringArg(args.cwd) ?? stringArg(args.target?.cwd) ?? process.cwd(),
      approvalPolicy: stringArg(args.approvalPolicy) ?? "never",
      sandbox: stringArg(args.sandbox) ?? "workspace-write",
    };
    for (const key of ["model", "baseInstructions", "developerInstructions"]) {
      if (stringArg(args[key])) params[key] = args[key];
    }
    if (args.config && typeof args.config === "object" && !Array.isArray(args.config)) params.config = args.config;

    await this.ensureStarted();
    const result = await this.request("thread/start", params, this.controlTimeout(timeoutMs));
    const thread = result?.thread;
    if (!thread?.id) throw new AppServerError("thread/start returned no thread id", "app_server_invalid_response");
    this.rememberThread(thread);
    this.loadedThreads.add(thread.id);

    const title = stringArg(args.title) ?? stringArg(args.name);
    if (title) await this.setThreadTitle({ threadId: thread.id, title }, timeoutMs);

    const payload = {
      threadId: thread.id,
      thread: normalizeThread(this.threads.get(thread.id)?.thread ?? thread),
      title: title ?? null,
    };

    const prompt = stringArg(args.prompt) ?? stringArg(args.text);
    if (prompt) {
      const sent = await this.sendMessage({ threadId: thread.id, prompt }, timeoutMs);
      payload.turnId = sent.turnId;
      payload.turn = sent.turn;
    }
    return payload;
  }

  async forkThread(args, timeoutMs) {
    const threadId = requireThreadId("fork_thread", args);
    const params = { threadId, approvalPolicy: stringArg(args.approvalPolicy) ?? "never" };
    for (const key of ["model", "cwd", "sandbox"]) {
      if (stringArg(args[key])) params[key] = args[key];
    }
    await this.ensureStarted();
    const result = await this.request("thread/fork", params, this.controlTimeout(timeoutMs));
    const thread = result?.thread;
    if (!thread?.id) throw new AppServerError("thread/fork returned no thread id", "app_server_invalid_response");
    this.rememberThread(thread);
    this.loadedThreads.add(thread.id);
    return {
      threadId: thread.id,
      forkedFromId: thread.forkedFromId ?? threadId,
      thread: normalizeThread(thread),
    };
  }

  async listThreads(args, timeoutMs) {
    const limit = clampInteger(args.limit, 1, 100, 20);
    const params = { limit };
    for (const key of ["cursor", "searchTerm"]) {
      if (typeof args[key] === "string" && args[key].length) params[key] = args[key];
    }
    if (typeof args.archived === "boolean") params.archived = args.archived;
    await this.ensureStarted();
    const result = await this.request("thread/list", params, this.readTimeout(timeoutMs));
    const threads = Array.isArray(result?.data) ? result.data.map(normalizeThread) : [];
    return {
      schemaVersion: 4,
      source: "app-server",
      pinnedThreads: [],
      threads,
      nextCursor: result?.nextCursor ?? null,
      backwardsCursor: result?.backwardsCursor ?? null,
    };
  }

  async listProjects(args, timeoutMs) {
    const limit = clampInteger(args.limit, 1, 100, 100);
    await this.ensureStarted();
    const result = await this.request("thread/list", { limit }, this.readTimeout(timeoutMs));
    const threads = Array.isArray(result?.data) ? result.data.map(normalizeThread) : [];
    for (const thread of threads) this.rememberThread(thread);
    // The caller may have created a thread after the persisted list snapshot
    // was written; include in-memory threads too so the CLI session is present.
    const all = [...threads];
    for (const state of this.threads.values()) {
      const thread = normalizeThread(state.thread);
      if (thread && !all.some((candidate) => candidate.id === thread.id)) all.push(thread);
    }
    return {
      schemaVersion: 1,
      source: "app-server",
      projects: projectList(all),
    };
  }

  async readThread(args, timeoutMs) {
    const threadId = requireThreadId("read_thread", args);
    const turnLimit = clampInteger(args.turnLimit, 0, 200, 10);
    const includeOutputs = args.includeOutputs !== false;
    const maxChars = Number.isFinite(Number(args.maxOutputCharsPerItem))
      ? Math.max(1, Number(args.maxOutputCharsPerItem)) : null;

    await this.ensureStarted();
    const result = await this.request("thread/read", { threadId, includeTurns: true }, this.readTimeout(timeoutMs));
    const thread = result?.thread;
    if (!thread?.id) throw new AppServerError(`thread/read returned no thread for ${threadId}`, "app_server_invalid_response");
    this.rememberThread(thread);

    let turns = Array.isArray(thread.turns) ? thread.turns : [];
    let itemsSource = "thread/read";
    for (const turn of turns) this.applyTurn(threadId, turn);

    if (!turns.length) {
      // 兜底：paginated 线程或旧记录可能不返回 turns，按 items 反推。
      const fallback = await this.readItemsFallback(threadId, timeoutMs);
      if (fallback.turns.length) {
        turns = fallback.turns;
        itemsSource = fallback.source;
      }
    }

    // 把通知缓存与磁盘快照合并：流式 item 可能比 persisted 快照更新。
    const state = this.threads.get(threadId);
    const snapshotTurns = turns.map((turn) => mergeTurnWithCache(turn, state?.turnMap.get(turn.id)));
    for (const cached of state?.turns ?? []) {
      if (!snapshotTurns.some((turn) => turn.id === cached.id)) snapshotTurns.push({ ...cached, items: [...cached.items] });
    }
    snapshotTurns.sort((a, b) => (a.startedAt ?? Number.MAX_SAFE_INTEGER) - (b.startedAt ?? Number.MAX_SAFE_INTEGER));

    // 最新一轮没有 items（itemsView notLoaded）时，用 items/list 补齐。
    const latest = snapshotTurns.at(-1);
    if (latest && !latest.items?.length) {
      const fallback = await this.readItemsFallback(threadId, timeoutMs, latest.id);
      if (fallback.turns.length) latest.items = fallback.turns[0].items;
    }

    const limited = turnLimit > 0 ? snapshotTurns.slice(-turnLimit) : snapshotTurns;
    const normalizedTurns = limited.map((turn) => normalizeTurn(turn, includeOutputs, maxChars));
    return {
      thread: normalizeThread(this.threads.get(threadId)?.thread ?? thread),
      turns: normalizedTurns,
      page: { order: "oldest_first", nextCursor: null, returnedTurns: normalizedTurns.length },
      itemsSource,
    };
  }

  async readItemsFallback(threadId, timeoutMs, turnId = null) {
    const params = { threadId, limit: 200 };
    if (turnId) params.turnId = turnId;
    const result = await this.request("thread/items/list", params, this.readTimeout(timeoutMs));
    const entries = Array.isArray(result?.data) ? result.data : [];
    const turns = [];
    const byTurn = new Map();
    for (const entry of entries) {
      const item = entry?.item;
      const entryTurnId = entry?.turnId;
      if (!item || typeof entryTurnId !== "string") continue;
      let turn = byTurn.get(entryTurnId);
      if (!turn) {
        turn = { id: entryTurnId, status: this.findTurn(threadId, entryTurnId)?.status ?? "unknown", items: [], itemsView: "full" };
        byTurn.set(entryTurnId, turn);
        turns.push(turn);
      }
      turn.items.push(item);
    }
    return { turns, source: "thread/items/list", nextCursor: result?.nextCursor ?? null };
  }

  async sendMessage(args, timeoutMs) {
    const threadId = requireThreadId("send_message_to_thread", args);
    const prompt = stringArg(args.prompt) ?? stringArg(args.text) ?? stringArg(args.message);
    let input = null;
    if (Array.isArray(args.input) && args.input.length) input = args.input;
    if (!input && !prompt) {
      throw new AppServerError("send_message_to_thread requires a non-empty `prompt` (or `input`)", "app_server_missing_argument");
    }
    const params = { threadId, input: input ?? [{ type: "text", text: prompt }] };
    for (const key of ["model", "effort", "cwd", "approvalPolicy", "summary"]) {
      if (args[key] !== undefined && args[key] !== null) params[key] = args[key];
    }
    if (args.sandboxPolicy && typeof args.sandboxPolicy === "object") params.sandboxPolicy = args.sandboxPolicy;

    await this.ensureStarted();
    await this.ensureThreadLoaded(threadId, args, timeoutMs);
    const result = await this.request("turn/start", params, this.controlTimeout(timeoutMs));
    const turn = result?.turn;
    if (!turn?.id) throw new AppServerError("turn/start returned no turn id", "app_server_invalid_response");
    this.applyTurn(threadId, turn);
    return {
      threadId,
      turnId: turn.id,
      accepted: true,
      turn: { id: turn.id, status: turn.status ?? "inProgress", startedAt: turn.startedAt ?? null },
    };
  }

  async setThreadTitle(args, timeoutMs) {
    const threadId = requireThreadId("set_thread_title", args);
    const title = stringArg(args.title) ?? stringArg(args.name);
    if (!title) throw new AppServerError("set_thread_title requires a non-empty `title`", "app_server_missing_argument");
    await this.ensureStarted();
    await this.request("thread/name/set", { threadId, name: title }, this.controlTimeout(timeoutMs));
    const state = this.threads.get(threadId);
    if (state?.thread) state.thread = { ...state.thread, name: title, title };
    return { updated: true, threadId, title };
  }

  async waitThreads(args, timeoutMs) {
    const targets = normalizeWaitTargets(args);
    const requested = Number(args.timeoutMs);
    const waitMs = Math.max(0, Math.min(Number.isFinite(requested) ? requested : DEFAULT_WAIT_MS, MAX_WAIT_MS));

    await this.ensureStarted();
    const resolved = [];
    for (const target of targets) {
      let turn = this.latestTurn(target.threadId);
      if (!turn) {
        // 没有缓存时做一次只读刷新；失败则如实返回 idle，不伪造状态。
        try {
          const snapshot = await this.readThread({ threadId: target.threadId, turnLimit: 1, includeOutputs: false }, timeoutMs);
          turn = snapshot.turns.at(-1) ?? null;
        } catch {
          turn = null;
        }
      }
      resolved.push({ target, turn });
    }

    const pendingWaits = waitMs > 0
      ? resolved
        .filter(({ turn }) => turn && !TERMINAL_TURN_STATUSES.has(turn.status))
        .map(({ target, turn }) => this.waitForTurn(target.threadId, turn.id, waitMs))
      : [];
    let timedOut = false;
    if (pendingWaits.length) {
      const results = await Promise.all(pendingWaits);
      timedOut = results.some((completed) => completed === false);
    }

    const polls = resolved.map(({ target, turn: initialTurn }) => {
      const latest = this.latestTurn(target.threadId)
        ?? (initialTurn ? this.findTurn(target.threadId, initialTurn.id) : null)
        ?? initialTurn;
      const state = this.threads.get(target.threadId);
      const thread = normalizeThread(state?.thread ?? { id: target.threadId });
      const poll = {
        thread: { id: target.threadId, hostId: "local", title: thread?.title ?? null, cwd: thread?.cwd ?? null, status: thread?.status ?? null },
        latestTurn: latest?.id ? {
          id: latest.id,
          status: latest.status,
          startedAt: latest.startedAt ?? null,
          completedAt: latest.completedAt ?? null,
        } : null,
        latestAssistantMessage: latestAssistantMessage(latest),
        cursor: null,
      };
      return poll;
    });
    return {
      polls,
      timedOut,
      snapshot: waitMs === 0,
      ...(timedOut ? { reason: `wait_threads timed out after ${waitMs}ms; returning current progress` } : {}),
      timeoutMs: waitMs,
    };
  }

  async ensureThreadLoaded(threadId, args = {}, timeoutMs) {
    if (this.loadedThreads.has(threadId)) return;
    // Only override policy when the caller asked; otherwise keep the thread's
    // own saved policy. turn/start cannot run against an unloaded thread.
    const params = { threadId };
    for (const key of ["approvalPolicy", "cwd", "model"]) {
      if (stringArg(args[key])) params[key] = args[key];
    }
    const result = await this.request("thread/resume", params, this.controlTimeout(timeoutMs));
    if (result?.thread) this.rememberThread(result.thread);
    this.loadedThreads.add(threadId);
  }

  controlTimeout(timeoutMs) {
    return Math.max(Number(timeoutMs) || 0, CONTROL_TIMEOUT_FLOOR_MS);
  }

  readTimeout(timeoutMs) {
    return Math.max(Number(timeoutMs) || 0, READ_TIMEOUT_FLOOR_MS);
  }
}

const client = new AppServerClient();

// ---------------------------------------------------------------------------
// 纯函数辅助
// ---------------------------------------------------------------------------

function findCodexBinary() {
  const configured = process.env.CODEXWORKER_CODEX_BIN?.trim();
  if (configured) return configured;
  const pathValue = process.env.PATH ?? "";
  const names = process.platform === "win32"
    ? ["codex.exe", "codex.cmd", "codex.bat", "codex"]
    : ["codex"];
  for (const directory of pathValue.split(path.delimiter)) {
    if (!directory) continue;
    for (const name of names) {
      const candidate = path.join(directory, name);
      try {
        fs.accessSync(candidate, fs.constants.X_OK);
        return candidate;
      } catch { /* keep looking */ }
    }
  }
  return null;
}

function appServerEnv() {
  const env = { ...process.env };
  // 本机 codex 只认 http 代理；socks（ALL_PROXY）会让整轮请求超时。
  delete env.ALL_PROXY;
  delete env.all_proxy;
  const explicit = process.env.CODEXWORKER_APP_SERVER_PROXY?.trim();
  const configured = process.env.HTTPS_PROXY?.trim() || process.env.https_proxy?.trim();
  const proxy = explicit || (configured && /^http:\/\//i.test(configured) ? configured : DEFAULT_PROXY);
  env.HTTPS_PROXY = proxy;
  env.https_proxy = proxy;
  env.HTTP_PROXY = explicit || (/^http:\/\//i.test(process.env.HTTP_PROXY ?? "") ? process.env.HTTP_PROXY : proxy);
  const noProxy = new Set((env.NO_PROXY ?? env.no_proxy ?? "").split(",").map((value) => value.trim()).filter(Boolean));
  for (const value of ["127.0.0.1", "localhost", "::1"]) noProxy.add(value);
  env.NO_PROXY = [...noProxy].join(",");
  env.no_proxy = env.NO_PROXY;
  return env;
}

function projectList(threads) {
  const byPath = new Map();
  for (const thread of threads) {
    if (!thread?.cwd) continue;
    const key = path.resolve(thread.cwd);
    let project = byPath.get(key);
    if (!project) {
      project = {
        projectId: `app-server:${createHash("sha256").update(key).digest("hex").slice(0, 32)}`,
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
    const usedAt = thread.updatedAt ?? thread.recencyAt ?? thread.createdAt ?? null;
    if (usedAt && (!project.lastUsedAt || Date.parse(usedAt) > Date.parse(project.lastUsedAt))) {
      project.lastUsedAt = usedAt;
    }
  }
  return [...byPath.values()].sort((a, b) =>
    (Date.parse(b.lastUsedAt ?? 0) || 0) - (Date.parse(a.lastUsedAt ?? 0) || 0) ||
    a.path.localeCompare(b.path));
}

function normalizeThread(thread) {
  if (!thread?.id) return null;
  return {
    id: thread.id,
    kind: "codex",
    hostId: "local",
    title: thread.name ?? thread.title ?? (thread.preview || null),
    name: thread.name ?? null,
    preview: thread.preview ?? "",
    cwd: thread.cwd ?? null,
    createdAt: thread.createdAt ?? null,
    updatedAt: thread.updatedAt ?? null,
    recencyAt: thread.recencyAt ?? null,
    status: thread.status ?? null,
    source: thread.source ?? null,
    model: thread.model ?? null,
    modelProvider: thread.modelProvider ?? null,
    path: thread.path ?? null,
    ephemeral: Boolean(thread.ephemeral),
    forkedFromId: thread.forkedFromId ?? null,
  };
}

function normalizeTurn(turn, includeOutputs, maxChars) {
  const items = (turn.items ?? []).map((item) => normalizeItem(item, includeOutputs, maxChars));
  return {
    id: turn.id,
    status: turn.status ?? "unknown",
    items,
    itemsView: turn.itemsView ?? "full",
    startedAt: turn.startedAt ?? null,
    completedAt: turn.completedAt ?? null,
    durationMs: turn.durationMs ?? null,
    error: turn.error ?? null,
  };
}

function normalizeItem(item, includeOutputs, maxChars) {
  const copy = { ...item };
  const delegation = copy.type === "functionCallOutput" &&
    ["create_thread", "send_message_to_thread"].includes(copy.name);
  if (!includeOutputs && !delegation) {
    // 与 Desktop 的 includeOutputs:false 对齐：公开消息照常返回，
    // 工具输出/参数不进入上层快照。
    delete copy.output;
    delete copy.arguments;
    delete copy.result;
    delete copy.changes;
  }
  if (maxChars) truncateItemStrings(copy, maxChars);
  return copy;
}

function truncateItemStrings(item, maxChars) {
  for (const key of ["text", "output", "result"]) {
    if (typeof item[key] === "string" && item[key].length > maxChars) {
      item[key] = item[key].slice(0, maxChars);
      item.truncated = true;
    }
  }
}

function mergeTurnWithCache(turn, cached) {
  const merged = { ...turn, items: Array.isArray(turn.items) ? [...turn.items] : [] };
  if (!cached) return merged;
  if (!merged.status) merged.status = cached.status;
  for (const item of cached.items) {
    const index = merged.items.findIndex((candidate) => candidate.id === item.id);
    if (index < 0) merged.items.push({ ...item });
    else if (!merged.items[index].text && item.text) merged.items[index] = { ...item, ...merged.items[index], text: item.text };
  }
  return merged;
}

function latestAssistantMessage(turn) {
  if (!turn) return null;
  for (let index = (turn.items?.length ?? 0) - 1; index >= 0; index--) {
    const item = turn.items[index];
    if (item?.type !== "agentMessage" || typeof item.text !== "string" || !item.text) continue;
    return {
      id: item.id,
      turnId: turn.id,
      text: item.text,
      phase: item.phase ?? null,
      truncated: item.truncated === true,
    };
  }
  return null;
}

function normalizeWaitTargets(args) {
  const raw = Array.isArray(args.targets) ? args.targets : args.threadId ? [{ threadId: args.threadId }] : [];
  const targets = [];
  for (const target of raw) {
    const threadId = stringArg(target?.threadId);
    if (!threadId) throw new AppServerError("wait_threads target is missing a string `threadId`", "app_server_missing_argument");
    targets.push({ threadId, hostId: stringArg(target?.hostId) ?? "local" });
  }
  if (!targets.length) throw new AppServerError("wait_threads requires at least one target thread", "app_server_missing_argument");
  return targets;
}

function requireThreadId(toolName, args) {
  const threadId = stringArg(args.threadId);
  if (!threadId) throw new AppServerError(`${toolName} requires a non-empty string \`threadId\``, "app_server_missing_argument");
  return threadId;
}

function stringArg(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function clampInteger(value, minimum, maximum, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.trunc(number)));
}
