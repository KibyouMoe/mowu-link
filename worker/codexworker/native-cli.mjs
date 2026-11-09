// native-cli.mjs — the "native-cli" backend for CodexWorker.
//
// The Desktop backend reaches Codex Desktop through the plugin-injected
// CODEX_APP_TOOLS_PIPE_PATH. A plain CLI process has no such pipe, so this
// module provides the same four native operations with the `codex` CLI:
//
//   dispatch()  start a turn          -> `codex exec --json -`
//   resume()    continue a session    -> `codex exec resume <id> --json -`
//   progress()  latest turn/message   -> Desktop `polls` shape (native-progress.mjs)
//   history()   sessions/transcripts  -> $CODEX_HOME/sessions/**/*.jsonl
//
// It never imports or modifies the Desktop path: server.mjs / mcp-business.mjs
// keep using the pipe whenever the Codex Desktop plugin host provides one.
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { createReadStream, existsSync } from "node:fs";
import { mkdir, readdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";

const SESSION_ID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
const DEFAULT_SANDBOX = "workspace-write";
const MAX_EVENTS = 500;

// `codex exec` has no dedicated --reasoning-effort flag; the value travels as
// a config override: `-c model_reasoning_effort="<value>"`. Anything else is a
// config typo, so it is warned about and dropped instead of failing startup.
export const REASONING_EFFORTS = Object.freeze(["low", "medium", "high", "xhigh", "max"]);

export function normalizeReasoningEffort(value, logger = () => {}) {
  if (value == null) return null;
  const text = String(value).trim().toLowerCase();
  if (!text) return null;
  if (REASONING_EFFORTS.includes(text)) return text;
  logger("warn", "reasoning_effort_invalid", { value: String(value), allowed: REASONING_EFFORTS.join("|") });
  return null;
}

export function expandHome(value) {
  if (typeof value !== "string") return value;
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

export function httpError(status, error, detail, retryable = false) {
  const value = new Error(detail);
  value.status = status;
  value.body = { error, detail, retryable };
  return value;
}

// Capability probe shared by the CLI entry and the MCP entry. The Desktop pipe
// is only usable from inside the Codex Desktop plugin host; a standalone CLI
// process can see the variable but cannot attach the pipe.
export function probeNativeBackend(env = process.env, config = {}) {
  const pipe = (env.CODEX_APP_TOOLS_PIPE_PATH ?? "").trim() || null;
  const configured = config.backend === "desktop" || config.backend === "cli" ? config.backend : null;
  const backend = configured ?? (pipe ? "desktop" : "cli");
  if (backend === "desktop") {
    return { backend, pipeConfigured: Boolean(pipe), available: Boolean(pipe), attached: false,
      reason: pipe
        ? "CODEX_APP_TOOLS_PIPE_PATH is set, but only the Codex Desktop plugin host can attach it; this process keeps the Desktop backend unstarted."
        : "Desktop backend selected without CODEX_APP_TOOLS_PIPE_PATH; start codexworker mcp inside the Codex Desktop plugin host." };
  }
  return { backend: "cli", pipeConfigured: Boolean(pipe), available: true, attached: true,
    reason: pipe
      ? "backend=cli explicitly selected; the injected Desktop pipe is ignored."
      : "CODEX_APP_TOOLS_PIPE_PATH is not set; turns run through `codex exec` instead of the Desktop pipe." };
}

// Resolve the codex executable: explicit config -> env -> PATH -> the local
// Hermes toolchain layout used on this machine. Never guesses credentials.
export async function resolveCodexBinary(config = {}, env = process.env) {
  const candidates = [];
  if (config.bin) candidates.push(expandHome(config.bin));
  if (env.CODEXWORKER_CODEX_BIN) candidates.push(expandHome(env.CODEXWORKER_CODEX_BIN));
  for (const dir of String(env.PATH ?? "").split(path.delimiter)) {
    if (!dir) continue;
    candidates.push(path.join(dir, process.platform === "win32" ? "codex.exe" : "codex"));
  }
  try {
    const toolsRoot = path.join(os.homedir(), ".hermes", "tools");
    for (const entry of await readdir(toolsRoot, { withFileTypes: true })) {
      if (entry.isDirectory()) candidates.push(path.join(toolsRoot, entry.name, "bin", "codex"));
    }
  } catch { /* optional convenience path only */ }
  for (const candidate of candidates) {
    if (!candidate) continue;
    try { if ((await stat(candidate)).isFile()) return candidate; } catch { /* next */ }
  }
  return null;
}

export async function detectCodexVersion(bin, env = process.env) {
  if (!bin) return null;
  return new Promise(resolve => {
    let settled = false;
    const done = value => { if (!settled) { settled = true; resolve(value); } };
    let child;
    try { child = spawn(bin, ["--version"], { env: childEnvironment(env), stdio: ["ignore", "pipe", "ignore"] }); }
    catch { return done(null); }
    let out = "";
    const timer = setTimeout(() => { child.kill("SIGKILL"); done(null); }, 5000);
    timer.unref();
    child.stdout.on("data", chunk => { out += chunk.toString("utf8"); });
    child.on("error", () => { clearTimeout(timer); done(null); });
    child.on("close", code => { clearTimeout(timer); done(code === 0 ? out.trim() : null); });
  });
}

// Signalling the whole tree: `codex exec` is spawned as a process-group leader
// on POSIX, so /stop can reach codex and every shell it started. Windows has no
// process groups here; the single child is signalled instead.
export function signalProcessTree(child, signal = "SIGTERM") {
  if (!child || typeof child.pid !== "number") return false;
  if (process.platform !== "win32") {
    try { process.kill(-child.pid, signal); return true; }
    catch { /* group already gone: fall back to the single child */ }
  }
  try { return child.kill(signal); } catch { return false; }
}

// SIGTERM first, SIGKILL after the grace period. Resolves once the child exits.
export async function terminateProcessTree(child, { graceMs = 2000 } = {}) {
  if (!child || child.exitCode != null || child.signalCode) return { exited: true, forced: false };
  let forced = false;
  const exited = new Promise(resolve => {
    if (child.exitCode != null || child.signalCode) return resolve(true);
    child.once("exit", () => resolve(true));
  });
  signalProcessTree(child, "SIGTERM");
  const timer = setTimeout(() => { forced = true; signalProcessTree(child, "SIGKILL"); }, Math.max(0, graceMs));
  timer.unref?.();
  await exited;
  clearTimeout(timer);
  return { exited: true, forced };
}

// codex rejects socks proxies, and ALL_PROXY on this host points at one. Strip
// it for the child and let HTTPS_PROXY (or an explicit configured proxy) carry
// the traffic. Never mutate the parent process environment.
export function childEnvironment(env = process.env, proxy = null) {
  const value = { ...env };
  delete value.ALL_PROXY;
  delete value.all_proxy;
  if (proxy) {
    value.HTTPS_PROXY = proxy; value.https_proxy = proxy;
    value.HTTP_PROXY = proxy; value.http_proxy = proxy;
  }
  return value;
}

function nowIso() { return new Date().toISOString(); }
function textOf(payload) {
  const content = payload?.content;
  if (!Array.isArray(content)) return null;
  const parts = content
    .filter(part => part && ["input_text", "output_text", "text"].includes(part.type) && typeof part.text === "string")
    .map(part => part.text);
  const text = parts.join("\n").trim();
  return text || null;
}
function injectedUserText(text) {
  return typeof text === "string" && (text.startsWith("# AGENTS.md instructions") ||
    text.includes("<skills_instructions>") || text.startsWith("<user_instructions>"));
}

// ---------------------------------------------------------------------------
// Session files
// ---------------------------------------------------------------------------

const indexCache = new Map();

// Filenames are rollout-<timestamp>-<session uuid>.jsonl, so an index only
// needs directory names + filenames; transcripts are read on demand.
export async function listSessionFiles(codexHome, { refresh = false } = {}) {
  const root = path.join(codexHome, "sessions");
  const cached = indexCache.get(root);
  if (!refresh && cached && Date.now() - cached.builtAt < 1500) return cached.files;
  let entries = [];
  try { entries = await readdir(root, { recursive: true, withFileTypes: true }); }
  catch { entries = []; }
  const files = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".jsonl")) continue;
    const match = SESSION_ID_RE.exec(entry.name);
    if (!match) continue;
    const dir = entry.parentPath ?? entry.path ?? root;
    const file = path.join(dir, entry.name);
    try { files.push({ id: match[0].toLowerCase(), path: file, mtimeMs: (await stat(file)).mtimeMs }); }
    catch { /* vanished between readdir and stat */ }
  }
  files.sort((a, b) => b.mtimeMs - a.mtimeMs);
  indexCache.set(root, { builtAt: Date.now(), files });
  return files;
}

async function findSessionFile(codexHome, sessionId) {
  if (!SESSION_ID_RE.test(sessionId)) return null;
  const id = String(sessionId).toLowerCase();
  const files = await listSessionFiles(codexHome);
  return files.find(file => file.id === id)?.path ?? null;
}

async function readSessionSummary(file, id) {
  const summary = { id, cwd: null, createdAt: null, updatedAt: null, title: null, turns: 0,
    lastAssistantText: null, lastTurnId: null, path: file.path };
  let input;
  try { input = createReadStream(file.path, { encoding: "utf8" }); }
  catch { return summary; }
  const reader = createInterface({ input, crlfDelay: Infinity });
  try {
    for await (const line of reader) {
      if (!line.trim()) continue;
      let record;
      try { record = JSON.parse(line); } catch { continue; }
      const payload = record?.payload;
      if (record.type === "session_meta") {
        summary.createdAt ??= payload?.timestamp ?? record.timestamp ?? null;
        summary.cwd ??= payload?.cwd ?? null;
      } else if (record.type === "response_item" && payload?.type === "message") {
        const text = textOf(payload);
        if (!text) continue;
        if (payload.role === "user" && !injectedUserText(text) && !summary.title) {
          summary.title = text.replace(/\s+/g, " ").slice(0, 80);
        } else if (payload.role === "assistant") {
          summary.lastAssistantText = text;
        }
      } else if (record.type === "event_msg" && payload?.type === "task_complete") {
        summary.turns += 1;
        summary.updatedAt = record.timestamp ?? summary.updatedAt;
        summary.lastTurnId = payload.turn_id ?? summary.lastTurnId;
        if (typeof payload.last_agent_message === "string" && payload.last_agent_message.trim()) {
          summary.lastAssistantText = payload.last_agent_message;
        }
      }
    }
  } finally { reader.close(); input.destroy(); }
  summary.updatedAt ??= new Date(file.mtimeMs).toISOString();
  summary.createdAt ??= summary.updatedAt;
  return summary;
}

export async function scanCodexSessions({ codexHome, limit = 50 } = {}) {
  const files = await listSessionFiles(codexHome);
  const selected = files.slice(0, Math.max(1, Math.min(Number(limit) || 50, 200)));
  return Promise.all(selected.map(file => readSessionSummary(file, file.id)));
}

export async function readCodexSession({ codexHome, sessionId, turnLimit = 10 } = {}) {
  const file = await findSessionFile(codexHome, sessionId);
  if (!file) return null;
  const turns = [];
  let current = null, meta = null, contextTurnId = null, title = null;
  const input = createReadStream(file, { encoding: "utf8" });
  const reader = createInterface({ input, crlfDelay: Infinity });
  try {
    for await (const line of reader) {
      if (!line.trim()) continue;
      let record;
      try { record = JSON.parse(line); } catch { continue; }
      const payload = record?.payload;
      if (record.type === "session_meta") {
        meta = { cwd: payload?.cwd ?? null, createdAt: payload?.timestamp ?? record.timestamp ?? null };
      } else if (record.type === "turn_context") {
        contextTurnId = payload?.turn_id ?? contextTurnId;
      } else if (record.type === "response_item" && payload?.type === "message") {
        const text = textOf(payload);
        if (!text) continue;
        if (payload.role === "user") {
          if (injectedUserText(text)) continue;
          title ??= text.replace(/\s+/g, " ").slice(0, 80);
          current = { id: contextTurnId ?? `turn-${turns.length + 1}`, status: "completed",
            createdAt: record.timestamp ?? null, items: [{ type: "userMessage", id: payload.id ?? null, text }] };
          turns.push(current);
        } else if (payload.role === "assistant" && current) {
          current.items.push({ type: "agentMessage", id: payload.id ?? null, text,
            phase: payload.phase ?? null });
        }
      } else if (record.type === "event_msg" && payload?.type === "task_complete") {
        if (current) {
          current.id = payload.turn_id ?? current.id;
          current.status = "completed";
          current.completedAt = record.timestamp ?? null;
        }
      } else if (record.type === "event_msg" && payload?.type === "task_started") {
        contextTurnId = payload?.turn_attribution?.turn_id ?? contextTurnId;
      }
    }
  } finally { reader.close(); input.destroy(); }
  const limited = turns.slice(-Math.max(1, Math.min(Number(turnLimit) || 10, 50)));
  return { thread: { id: sessionId, cwd: meta?.cwd ?? null, createdAt: meta?.createdAt ?? null,
      updatedAt: limited.at(-1)?.completedAt ?? null, title: title ?? null, hostId: "local" },
    page: { order: "oldest_first" }, turns: limited };
}

async function lastTurnOf(sessionId, codexHome) {
  const snapshot = await readCodexSession({ codexHome, sessionId, turnLimit: 1 });
  return snapshot?.turns?.at(-1) ?? null;
}

// ---------------------------------------------------------------------------
// Adapter
// ---------------------------------------------------------------------------

export class CliNativeAdapter {
  constructor(options = {}) {
    this.codexHome = expandHome(options.codexHome ?? process.env.CODEX_HOME ?? path.join(os.homedir(), ".codex"));
    this.stateDir = options.stateDir;
    this.codexBin = options.codexBin ?? null;
    this.defaultCwd = expandHome(options.defaultCwd ?? process.cwd());
    this.turnTimeoutMs = Number(options.turnTimeoutMs ?? 600000);
    this.sandbox = options.sandbox === undefined ? DEFAULT_SANDBOX : options.sandbox;
    this.approvalPolicy = options.approvalPolicy === undefined ? "never" : options.approvalPolicy;
    this.model = options.model ?? null;
    this.logger = options.logger ?? (() => {});
    // Validated once, at construction: an unknown effort is a config typo, so it
    // warns and degrades to "no override" rather than failing the whole bridge.
    this.reasoningEffort = normalizeReasoningEffort(options.reasoningEffort, this.logger);
    this.extraArgs = Array.isArray(options.extraArgs) ? options.extraArgs.map(String) : [];
    this.proxy = options.proxy ?? null;
    this.env = options.env ?? process.env;
    this.statePath = path.join(this.stateDir, "native-cli-state.json");
    this.state = { version: 1, seq: 0, threads: {} };
    this.events = [];
    this.running = new Map();
    this.children = new Set();
    this.codexVersion = null;
    this.closed = false;
  }

  async init() {
    await mkdir(this.stateDir, { recursive: true });
    try {
      const loaded = JSON.parse(await readFile(this.statePath, "utf8"));
      if (loaded?.version === 1 && loaded.threads && typeof loaded.threads === "object" && !Array.isArray(loaded.threads)) {
        this.state = { version: 1, seq: Number(loaded.seq) || 0, threads: loaded.threads };
      }
    } catch (error) {
      if (error.code !== "ENOENT") this.logger("warn", "state_unreadable", { message: error.message });
    }
    this.codexBin ??= await resolveCodexBinary({}, this.env);
    this.codexVersion = await detectCodexVersion(this.codexBin, this.env);
    this.available = Boolean(this.codexBin && this.codexVersion);
    return this.status();
  }

  status() {
    return { available: Boolean(this.available), backend: "cli", codexBin: this.codexBin ?? null,
      codexVersion: this.codexVersion ?? null, codexHome: this.codexHome,
      tracks: Object.keys(this.state.threads).length, activeTurns: this.running.size,
      sandbox: this.sandbox, model: this.model, reasoningEffort: this.reasoningEffort,
      turnTimeoutMs: this.turnTimeoutMs,
      tokenStreaming: false };
  }

  async saveState() {
    const payload = JSON.stringify(this.state, null, 0);
    const temporary = `${this.statePath}.${process.pid}.tmp`;
    await writeFile(temporary, payload, { mode: 0o600 });
    await rename(temporary, this.statePath);
  }

  emit(type, { threadId = null, turnId = null, messageId = null, payload = {}, onEvent } = {}) {
    const event = { seq: ++this.state.seq, type, threadId, turnId, messageId, at: nowIso(), payload };
    this.events.push(event);
    if (this.events.length > MAX_EVENTS) this.events.splice(0, this.events.length - MAX_EVENTS);
    try { onEvent?.(event); } catch { /* a broken SSE client must not fail the turn */ }
    return event;
  }

  async register({ cwd, title } = {}) {
    const id = `cli-${randomUUID()}`;
    const at = nowIso();
    const thread = { id, sessionId: null, cwd: expandHome(cwd) ?? this.defaultCwd, createdAt: at, updatedAt: at,
      title: title ?? null, turns: 0, lastTurn: null };
    this.state.threads[id] = thread;
    await this.saveState();
    return thread;
  }

  async adopt(threadId) {
    if (this.state.threads[threadId]) return this.state.threads[threadId];
    const file = await findSessionFile(this.codexHome, threadId);
    if (!file) throw httpError(404, "thread_not_found", "No managed or recorded codex session has this id.");
    const summary = await readSessionSummary({ path: file, id: threadId, mtimeMs: Date.now() }, threadId);
    const thread = { id: threadId, sessionId: threadId, cwd: summary.cwd ?? this.defaultCwd,
      createdAt: summary.createdAt ?? nowIso(), updatedAt: summary.updatedAt ?? nowIso(),
      title: summary.title, turns: summary.turns, lastTurn: null };
    this.state.threads[threadId] = thread;
    await this.saveState();
    return thread;
  }

  async dispatch({ threadId = null, text, cwd = null, messageId = null, onEvent = null } = {}) {
    if (this.closed) throw httpError(503, "bridge_closed", "The CLI backend is closed.");
    if (!this.available) throw httpError(503, "codex_cli_unavailable", "The codex CLI was not found or is not runnable; set codex.bin in the config.", true);
    if (typeof text !== "string" || !text.trim()) throw httpError(400, "invalid_request", "text is required.");
    return this.#turn({ threadId, text, cwd, messageId, onEvent });
  }

  async resume(threadId, options = {}) {
    if (!this.state.threads[threadId] && !(await findSessionFile(this.codexHome, threadId))) {
      throw httpError(404, "thread_not_found", "No managed or recorded codex session has this id.");
    }
    return this.dispatch({ ...options, threadId });
  }

  async #turn({ threadId, text, cwd, messageId, onEvent }) {
    let thread = threadId ? this.state.threads[threadId] : null;
    if (!thread && threadId) {
      const file = await findSessionFile(this.codexHome, threadId);
      thread = { id: threadId, sessionId: file ? threadId : null, cwd: expandHome(cwd) ?? this.defaultCwd,
        createdAt: nowIso(), updatedAt: nowIso(), title: null, turns: 0, lastTurn: null };
    }
    if (!thread) {
      thread = { id: null, sessionId: null, cwd: expandHome(cwd) ?? this.defaultCwd, createdAt: nowIso(),
        updatedAt: nowIso(), title: null, turns: 0, lastTurn: null };
    } else if (cwd) thread.cwd = expandHome(cwd);

    const key = thread.id ?? "new";
    if (this.running.has(key)) throw httpError(409, "turn_in_progress", "This thread already has a running CLI turn; wait for it to finish.", true);
    // Control block shared with #runTurn and /stop: the child is stored here as
    // soon as it exists, and `cancelled` is what turns the result into
    // status=cancelled instead of a generic error.
    const control = { startedAt: Date.now(), child: null, pid: null, cancelled: false };
    control.settled = new Promise(resolve => { control.resolveSettled = resolve; });
    this.running.set(key, control);
    try {
      const result = await this.#runTurn({ thread, text, messageId, onEvent, control });
      if (!thread.id) thread.id = result.sessionId ?? `cli-${randomUUID()}`;
      thread.sessionId ??= result.sessionId ?? null;
      thread.updatedAt = nowIso();
      thread.turns += 1;
      thread.title ??= text.replace(/\s+/g, " ").slice(0, 80);
      thread.lastTurn = { turnId: result.turnId, status: result.status, text: result.resultText,
        messageId, at: thread.updatedAt, durationMs: result.durationMs, usage: result.usage ?? null };
      this.state.threads[thread.id] = thread;
      await this.saveState();
      return { threadId: thread.id, sessionId: thread.sessionId, ...result };
    } finally {
      this.running.delete(key);
      control.resolveSettled?.();
    }
  }

  // /stop: terminate the running turn's codex process group. An idle thread
  // answers no_active_turn (200), not an error: stopping twice is harmless.
  async stop(threadId = null, { graceMs = 2000 } = {}) {
    const entry = threadId != null ? this.running.get(threadId) : null;
    if (!entry) {
      return { ok: true, status: "no_active_turn", threadId: threadId ?? null,
        activeTurns: this.running.size };
    }
    entry.cancelled = true;
    this.emit("turn.stopping", { threadId, payload: { reason: "stop_requested", pid: entry.pid ?? null } });
    const termination = await terminateProcessTree(entry.child, { graceMs });
    // Wait for the turn body to observe the exit and settle, but never hang a
    // stop request on a broken child: the response is bounded.
    await Promise.race([entry.settled, new Promise(resolve => { const timer = setTimeout(resolve, 5000); timer.unref?.(); })]);
    return { ok: true, status: "cancelled", threadId, pid: entry.pid ?? null, forced: termination.forced };
  }

  argsFor({ resumeId }) {
    if (resumeId) {
      const args = ["exec", "resume", resumeId, "--skip-git-repo-check", "--json"];
      args.push(...this.#sharedArgs({ resume: true }), "-");
      return args;
    }
    const args = ["exec", "--skip-git-repo-check", "--json"];
    if (this.sandbox) args.push("--sandbox", this.sandbox);
    args.push(...this.#sharedArgs({ resume: false }), "-");
    return args;
  }

  #sharedArgs({ resume }) {
    const args = [];
    if (this.approvalPolicy) args.push("-c", `approval_policy="${this.approvalPolicy}"`);
    // `codex exec resume` has no --sandbox flag; use the config override.
    if (resume && this.sandbox) args.push("-c", `sandbox_mode="${this.sandbox}"`);
    if (this.model) args.push("-m", this.model);
    // exec has no --reasoning-effort flag; the model override is the only hook.
    // Both the fresh and the resume path build argv through #sharedArgs.
    if (this.reasoningEffort) args.push("-c", `model_reasoning_effort="${this.reasoningEffort}"`);
    args.push(...this.extraArgs);
    return args;
  }

  async #runTurn({ thread, text, messageId, onEvent, control = null }) {
    const resumeId = thread.sessionId;
    const args = this.argsFor({ resumeId });
    const cwd = thread.cwd && existsSync(thread.cwd) ? thread.cwd : this.defaultCwd;
    const started = Date.now();
    this.emit("turn.started", { threadId: thread.id, messageId, onEvent,
      payload: { resumed: Boolean(resumeId), cwd, text } });

    let sessionId = resumeId ?? null;
    let localTurnId = randomUUID();
    let latestText = null;
    let usage = null, failure = null, completed = false, timedOut = false;
    let stderr = "";
    const toolCalls = [];

    // Detached on POSIX: the child leads its own process group, so /stop can
    // signal codex and every shell it spawned with one kill(-pid).
    const child = spawn(this.codexBin, args, { cwd, env: childEnvironment(this.env, this.proxy),
      stdio: ["pipe", "pipe", "pipe"], detached: process.platform !== "win32" });
    this.children.add(child);
    if (control) { control.child = child; control.pid = child.pid ?? null; }
    // A stop can land between the running-map insert and the spawn above.
    if (control?.cancelled) signalProcessTree(child, "SIGTERM");
    const killTimer = setTimeout(() => {
      timedOut = true;
      signalProcessTree(child, "SIGTERM");
      const force = setTimeout(() => signalProcessTree(child, "SIGKILL"), 2000);
      force.unref();
    }, this.turnTimeoutMs);
    killTimer.unref();

    child.stdin.on("error", () => {}); // codex may close stdin early; ignore EPIPE
    child.stdin.end(text, "utf8");

    const reader = createInterface({ input: child.stdout, crlfDelay: Infinity });
    const stdoutDone = new Promise(resolve => reader.on("close", resolve));
    const consume = line => {
      if (!line.trim()) return;
      let event;
      try { event = JSON.parse(line); } catch { return; }
      const item = event.item ?? {};
      if (event.type === "thread.started") {
        if (typeof event.thread_id === "string" && event.thread_id) sessionId = event.thread_id;
      } else if (event.type === "item.started") {
        if (item.type === "command_execution" || item.type === "file_change" || item.type === "mcp_tool_call") {
          this.emit("tool.started", { threadId: thread.id ?? sessionId, turnId: null, messageId, onEvent,
            payload: toolPayload(item) });
        }
      } else if (event.type === "item.completed") {
        if (item.type === "agent_message") {
          latestText = typeof item.text === "string" ? item.text : latestText;
          this.emit("item.completed", { threadId: thread.id ?? sessionId, turnId: null, messageId, onEvent,
            payload: { snapshot: latestText, itemId: item.id ?? null, kind: "agent_message" } });
        } else if (item.type === "command_execution" || item.type === "file_change" || item.type === "mcp_tool_call") {
          toolCalls.push(toolPayload(item));
          this.emit("tool.completed", { threadId: thread.id ?? sessionId, turnId: null, messageId, onEvent,
            payload: toolPayload(item) });
        } else if (item.type === "error") {
          failure = item.message ?? "codex_item_error";
        }
      } else if (event.type === "turn.completed") {
        completed = true; usage = event.usage ?? null;
      } else if (event.type === "turn.failed") {
        failure = event.error?.message ?? "codex_turn_failed";
      } else if (event.type === "error") {
        failure = event.message ?? "codex_error";
      }
    };

    const stderrReader = createInterface({ input: child.stderr, crlfDelay: Infinity });
    stderrReader.on("line", line => { if (stderr.length < 8000) stderr += line + "\n"; });
    reader.on("line", consume);

    const exitCode = await new Promise(resolve => {
      let settled = false;
      const done = value => { if (!settled) { settled = true; resolve(value); } };
      child.on("error", error => { failure ??= `spawn_failed: ${error.message}`; done(-1); });
      child.on("close", code => done(code ?? -1));
    });
    clearTimeout(killTimer);
    await stdoutDone;
    reader.close(); stderrReader.close();
    this.children.delete(child);

    let status = "completed";
    // A cancelled turn is not an error: the trainer asked for it. Checked
    // first so a killed child's non-zero exit never masquerades as a failure.
    if (control?.cancelled) status = "cancelled";
    else if (failure) status = "error";
    else if (timedOut) status = "timeout";
    else if (!completed || exitCode !== 0) status = "error";

    // The exec JSON stream has no turn id; the rollout file does. Best effort
    // only: a missing rollout never invalidates an otherwise completed turn.
    let turnId = localTurnId;
    if (sessionId) {
      try {
        const last = await lastTurnOf(sessionId, this.codexHome);
        if (last?.id) turnId = last.id;
        if (!latestText && last) {
          latestText = last.items.filter(item => item.type === "agentMessage").at(-1)?.text ?? null;
        }
      } catch { /* keep the local id */ }
    }
    if (!latestText && status !== "completed") {
      latestText = "";
    }

    const result = { sessionId, turnId, status, resultText: latestText ?? "", toolCalls,
      reason: status === "completed" ? "completed"
        : status === "cancelled" ? "cancelled_by_stop"
        : failure ?? (timedOut ? `codex exec exceeded ${this.turnTimeoutMs}ms`
        : stderr.trim().split("\n").slice(-3).join(" ").slice(0, 500) || `codex exec exited with code ${exitCode}`),
      usage, exitCode, durationMs: Date.now() - started, stderr: stderr.trim().slice(-2000) };
    const eventType = status === "completed" ? "turn.completed"
      : status === "cancelled" ? "turn.cancelled" : "turn.failed";
    this.emit(eventType,
      { threadId: thread.id ?? sessionId, turnId, messageId, onEvent,
        payload: { status, resultText: result.resultText, reason: result.reason,
          messageIds: messageId ? [messageId] : [], usage, durationMs: result.durationMs } });
    return result;
  }

  // Desktop-compatible `polls` snapshot: publicProgress() in native-progress.mjs
  // accepts it unchanged.
  progress(threadId) {
    const thread = this.state.threads[threadId];
    if (!thread) throw httpError(404, "thread_not_found", "Unknown CLI thread.", false);
    const last = thread.lastTurn;
    const messageId = last?.messageId ?? (last ? `${threadId}:${last.turnId}` : null);
    return { polls: [{
      thread: { id: threadId, hostId: "local", cwd: thread.cwd ?? null },
      cursor: last ? String(last.at) : null,
      latestTurn: { id: last?.turnId ?? null, status: last?.status ?? "idle" },
      latestAssistantMessageId: last?.text ? messageId : null,
      latestAssistantMessage: last?.text ? { id: messageId, turnId: last.turnId, phase: "final",
        text: last.text, truncated: false } : null,
    }] };
  }

  async history({ limit = 50 } = {}) {
    const managed = Object.values(this.state.threads);
    const scanned = await scanCodexSessions({ codexHome: this.codexHome, limit: Number(limit) + 10 });
    const known = new Set(managed.map(thread => thread.sessionId).filter(Boolean));
    for (const thread of managed) known.add(thread.id);
    const threads = managed.map(thread => ({
      id: thread.id, sessionId: thread.sessionId ?? null, cwd: thread.cwd ?? null,
      createdAt: thread.createdAt ?? null, updatedAt: thread.updatedAt ?? thread.createdAt ?? null,
      title: thread.title ?? null, turns: thread.turns ?? 0,
      lastText: thread.lastTurn?.text ?? null, live: this.running.has(thread.id),
      hostId: "local", source: "cli",
    }));
    for (const session of scanned) {
      if (known.has(session.id)) continue;
      threads.push({ id: session.id, sessionId: session.id, cwd: session.cwd, createdAt: session.createdAt,
        updatedAt: session.updatedAt, title: session.title, turns: session.turns,
        lastText: session.lastAssistantText, live: false, hostId: "local", source: "codex-session" });
    }
    threads.sort((a, b) => String(b.updatedAt ?? "").localeCompare(String(a.updatedAt ?? "")));
    return threads.slice(0, Math.max(1, Math.min(Number(limit) || 50, 200)));
  }

  async readThread(threadId, { turnLimit = 10 } = {}) {
    const thread = this.state.threads[threadId];
    const sessionId = thread?.sessionId ?? threadId;
    if (!thread && !(await findSessionFile(this.codexHome, threadId))) {
      throw httpError(404, "thread_not_found", "Unknown CLI thread.", false);
    }
    const snapshot = await readCodexSession({ codexHome: this.codexHome, sessionId, turnLimit });
    if (!snapshot) throw httpError(404, "thread_not_found", "Unknown codex session.", false);
    return { ...snapshot, thread: { ...snapshot.thread, id: threadId, aliasId: sessionId } };
  }

  eventsAfter(after = 0, limit = 500) {
    return { events: this.events.filter(event => event.seq > after).slice(0, limit), lastSeq: this.state.seq };
  }

  async close() {
    this.closed = true;
    for (const child of this.children) signalProcessTree(child, "SIGTERM");
    this.children.clear();
  }
}

function toolPayload(item) {
  const payload = { type: item.type, itemId: item.id ?? null, status: item.status ?? null };
  if (item.type === "command_execution") {
    payload.name = "shell"; payload.command = item.command ?? null;
    payload.exitCode = item.exit_code ?? null;
    payload.output = typeof item.aggregated_output === "string" ? item.aggregated_output.slice(0, 2000) : null;
  } else if (item.type === "file_change") {
    payload.name = "file_change"; payload.changes = item.changes ?? null;
  } else if (item.type === "mcp_tool_call") {
    payload.name = `mcp:${item.server ?? "?"}.${item.tool ?? "?"}`; payload.arguments = item.arguments ?? null;
  }
  return payload;
}
