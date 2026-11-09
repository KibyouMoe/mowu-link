// feishu-cli.mjs — Feishu long-connection channel for the **CLI** (native-cli)
// backend. Glue only: it reuses feishu.mjs (FeishuNotifier /
// FeishuWebsocketClient) and native-cli.mjs (CliNativeAdapter.dispatch), and
// adds the CLI-specific pieces the Desktop manager used to own:
//
//   inbound p2p text -> typing reaction on the trainer's own message
//   -> one persistent Codex session per sender (open_id)
//   -> 「输出」card (event-driven progress) -> 「回复」card (final text + session id)
//   -> reaction removed
//
// It never touches the Desktop path (desktop-manager.mjs / server.mjs); a CLI
// process has no writer lease, no Desktop relay and no question cards.
import { createHash } from "node:crypto";
import { mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { FEISHU_API_BASE, FeishuNotifier, FeishuWebsocketClient, markdownPayload } from "./feishu.mjs";
import { expandHome } from "./native-cli.mjs";

const STATE_VERSION = 1;
const MAX_SEEN_MESSAGES = 500;
const MAX_CARD_CHARS = 6000;
const MAX_ACTIVITY_LINES = 6;
// Bounded flood retry, mirroring Hermes' stream fallback: at most two attempts
// (initial + one retry). The retry delay honours Feishu's retry_after, or 3s
// by default, and is abandoned when the platform asks for longer than the cap.
// There is no endless 1/2/4/8 backoff ladder.
const DEFAULT_FLOOD_RETRY_DELAY_MS = 3000;
const MAX_FLOOD_RETRY_DELAY_MS = 30000;
// Feishu answers 429 / code 99991400 ("request trigger frequency limit") when
// the bot writes faster than the per-chat quota.
const RATE_LIMIT_CODES = new Set([99991400, 99991401]);
const RATE_LIMIT_TEXT = /(rate.?limit|frequency|too many requests|限频|频率)/i;
const STOP_COMMAND = /^\/stop(?:@\S+)?$/i;

function defaultLogger() {}

// Same stable UUID-shaped digest as the Desktop manager: duplicate WS delivery
// of one Feishu message maps to one dispatch id, even across a restart.
export function feishuMessageId(messageId) {
  const hex = createHash("sha256").update(`feishu:${messageId}`).digest("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

function clamp(text, max = MAX_CARD_CHARS) {
  const value = String(text ?? "");
  return value.length <= max ? value : `${value.slice(0, max)}\n…（已截断）`;
}

function shortError(error) {
  const detail = error?.body?.detail ?? error?.message ?? String(error);
  return String(detail).trim().slice(0, 500) || "未知错误";
}

// ---------------------------------------------------------------------------
// Credentials
// ---------------------------------------------------------------------------

// Reads the private Feishu config file (never logged, never copied into the
// repository). Accepts snake_case as written by ~/.hermes/secrets/feishu-*.json
// and camelCase for hand-written files.
export async function loadFeishuCredentials(file, { logger = defaultLogger } = {}) {
  const resolved = expandHome(file);
  let raw;
  try { raw = await readFile(resolved, "utf8"); }
  catch (error) { throw new Error(`无法读取飞书配置文件 ${resolved}: ${error.code ?? error.message}`); }
  let value;
  try { value = JSON.parse(raw); }
  catch (error) { throw new Error(`飞书配置文件不是合法 JSON (${resolved}): ${error.message}`); }
  const appId = String(value?.app_id ?? value?.appId ?? "").trim();
  const appSecret = String(value?.app_secret ?? value?.appSecret ?? "").trim();
  if (!appId || !appSecret) throw new Error(`飞书配置文件缺少 app_id/app_secret: ${resolved}`);
  try {
    const mode = (await stat(resolved)).mode & 0o777;
    if (mode & 0o077) logger("warn", "feishu_config_permissions", { file: resolved, mode: mode.toString(8) });
  } catch { /* the read above already proved the file exists */ }
  return { appId, appSecret, chatId: value?.chat_id ?? value?.chatId ?? null, file: resolved };
}

// ---------------------------------------------------------------------------
// Session map (~/.codex/state/codexworker/feishu-sessions.json)
// ---------------------------------------------------------------------------

export class FeishuSessionStore {
  constructor(file, { logger = defaultLogger } = {}) {
    this.file = file;
    this.logger = logger;
    this.state = { version: STATE_VERSION, ownerOpenId: null, sessions: {} };
    this.chain = Promise.resolve();
  }

  async load() {
    try {
      const loaded = JSON.parse(await readFile(this.file, "utf8"));
      if (loaded?.version === STATE_VERSION && loaded.sessions &&
          typeof loaded.sessions === "object" && !Array.isArray(loaded.sessions)) {
        this.state = { version: STATE_VERSION,
          ownerOpenId: typeof loaded.ownerOpenId === "string" && loaded.ownerOpenId ? loaded.ownerOpenId : null,
          sessions: loaded.sessions };
      } else {
        this.logger("warn", "feishu_sessions_ignored", { file: this.file, reason: "unsupported_shape" });
      }
    } catch (error) {
      // A corrupt map must not take the bridge down: sessions restart empty.
      if (error.code !== "ENOENT") this.logger("warn", "feishu_sessions_unreadable", { file: this.file, message: error.message });
    }
    return this.state;
  }

  get(openId) {
    const record = this.state.sessions[openId];
    return record && typeof record === "object" ? record : null;
  }

  async setOwner(openId) {
    this.state.ownerOpenId = openId;
    await this.save();
  }

  async set(openId, patch) {
    this.state.sessions[openId] = { ...(this.state.sessions[openId] ?? {}), ...patch,
      updatedAt: new Date().toISOString() };
    await this.save();
    return this.state.sessions[openId];
  }

  save() {
    const task = this.chain.then(() => this.#write());
    this.chain = task.catch(() => {});
    return task;
  }

  async #write() {
    await mkdir(path.dirname(this.file), { recursive: true });
    const temporary = `${this.file}.${process.pid}.tmp`;
    await writeFile(temporary, JSON.stringify(this.state, null, 2) + "\n", { mode: 0o600 });
    await rename(temporary, this.file);
  }
}

// ---------------------------------------------------------------------------
// History probe. The bridge-owned SessionStore is the only history source; the
// codex rollout jsonl is a process stream and is never used for /threads or
// card history. Only a genuinely unreadable store (missing/corrupt file)
// degrades to a new session, with the reason logged and shown once — the card
// title itself is always `-输出` / `-回复`.
// ---------------------------------------------------------------------------

export function historyProbeFor(sessionStore) {
  return async threadId => sessionStore.read(threadId, { limit: 1 });
}

// ---------------------------------------------------------------------------
// 「输出」card: one message per turn, written only on state boundaries.
//
// The card is PATCHed when the turn starts, when a *new* tool shows up (the
// same tool repeated back-to-back is one state change), when an agent message
// snapshot arrives, and when the turn ends. There is no timer re-PATCHing a
// stale body: a locked phone is no longer woken every 1.8s.
//
// A PATCH that Feishu rate-limits (HTTP 429 / body code 99991400 / a
// frequency message) is not dropped: the newest snapshot is kept in a
// one-slot queue and retried at 1s → 2s → 4s → 8s. Newer snapshots overwrite
// the queued one, so a burst costs one PATCH. When the whole schedule is
// exhausted the card is rotated to a fresh message (same title, latest full
// snapshot) and the stale card gets a best-effort 「本卡已停止更新」 note.
// Any other error is logged once and degrades to one fresh message: never a
// retry storm. A rotated message that also fails the schedule ends the turn
// as a logged delivery failure instead of retrying forever.
// ---------------------------------------------------------------------------

export function isStopCommand(text) {
  return typeof text === "string" && STOP_COMMAND.test(text.trim());
}

export function isRateLimited(response) {
  if (!response || typeof response !== "object") return false;
  if (response.httpStatus === 429 || response.status === 429) return true;
  const code = Number(response.code);
  if (Number.isInteger(code) && RATE_LIMIT_CODES.has(code)) return true;
  for (const value of [response.msg, response.message, response.data?.msg]) {
    if (typeof value === "string" && RATE_LIMIT_TEXT.test(value)) return true;
  }
  return false;
}

function codeOf(response) {
  return Number.isInteger(response?.code) ? response.code : null;
}

function messageOf(response) {
  const value = response?.msg ?? response?.message ?? null;
  return typeof value === "string" ? value.slice(0, 300) : null;
}

// Feishu sometimes returns retry_after (seconds) with a 429. The bounded retry
// uses it when present and otherwise falls back to the configured 3s.
function retryAfterOf(response) {
  const raw = response?.retry_after ?? response?.retryAfter ?? null;
  if (raw == null) return null;
  const seconds = Number(raw);
  return Number.isFinite(seconds) ? Math.max(0, seconds * 1000) : null;
}

// A timeout may have landed on Feishu's side even though the client saw an
// error. Those are "ambiguous": resending would duplicate the card.
function isTimeoutError(error) {
  return /timeout|timed out|abort/i.test(`${error?.name ?? ""} ${error?.message ?? String(error)}`);
}

function isAmbiguous(response) {
  if (!response) return false;
  if (response.httpStatus === 408 || response.httpStatus === 504) return true;
  const code = Number(response.code);
  if (code === 408 || code === 504) return true;
  return isTimeoutError({ name: "", message: `${response.code ?? ""} ${response.msg ?? response.message ?? ""}` });
}

async function patchMessageCard(token, messageId, payload) {
  try {
    const response = await fetch(`${FEISHU_API_BASE}/im/v1/messages/${encodeURIComponent(messageId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json; charset=utf-8", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ content: payload }),
      signal: AbortSignal.timeout(15000),
    });
    // Keep the HTTP status: a 429 is a rate limit even when the JSON body is
    // unhelpful, and the caller must not mistake it for a dead card.
    let value = null;
    try { value = await response.json(); }
    catch { value = { code: -1, msg: `http_${response.status}` }; }
    return { ...value, httpStatus: response.status };
  } catch (error) {
    return { code: -1, msg: `network error: ${error?.message ?? String(error)}`, httpStatus: 0 };
  }
}

export function notifierCardTransport(notifier) {
  return {
    async send(text, { chatId, title, threadId }) {
      const response = await notifier.sendMessage(text, { chatId, title, threadId });
      return { code: response?.code === 0 ? 0 : (Number.isInteger(response?.code) ? response.code : -1),
        messageId: response?.code === 0 ? String(response?.data?.message_id ?? "") || null : null };
    },
    async update(messageId, text, { title, threadId }) {
      const token = await notifier.getToken();
      if (!token) return { code: -1 };
      return patchMessageCard(token, messageId, markdownPayload(text, title, threadId));
    },
  };
}

export class OutputCard {
  constructor({ transport, chatId, threadId = null, title,
    now = Date.now, timers = { setTimeout, clearTimeout }, logger = defaultLogger,
    floodRetryDelayMs = DEFAULT_FLOOD_RETRY_DELAY_MS,
    maxFloodRetryDelayMs = MAX_FLOOD_RETRY_DELAY_MS,
    maxRotations = 1 }) {
    this.transport = transport;
    this.chatId = chatId;
    this.threadId = threadId;
    this.title = title;
    this.now = now;
    this.timers = timers;
    this.logger = logger;
    this.floodRetryDelayMs = Math.max(0, Number(floodRetryDelayMs) || 0);
    this.maxFloodRetryDelayMs = Math.max(this.floodRetryDelayMs, Number(maxFloodRetryDelayMs) || 0);
    this.maxRotations = Math.max(0, Number(maxRotations) || 0);
    this.messageId = null;
    // Last delivery outcome, same vocabulary as Hermes' stream fallback:
    //   delivered — write acknowledged by Feishu
    //   preview   — flood limit, but the ACKed body already shows the same text
    //   ambiguous — timeout: the write may have landed; never resend blindly
    //   failed    — explicit rejection after the bounded retry
    this.lastOutcome = null;
    this.deliveryFailed = false;
    this.deliveryAmbiguous = false;
    this.sealed = false;      // no new snapshots; the final write may still run
    this.closed = false;      // nothing else will ever be written
    this.lastText = "";
    this.lastSentText = "";   // body Feishu acknowledged: what the trainer sees
    this.queuedText = null;   // one-slot merge queue: only the newest snapshot
    this.rotations = 0;       // replacement messages created after a failed PATCH
    this.retryTimer = null;
    this.sleepResolve = null;
    this.busy = false;
    this.sends = 0;
    this.updates = 0;         // successful PATCHes
    this.patches = 0;         // PATCH attempts, including rejected ones
    this.retries = 0;
  }

  // First write is immediate: the trainer must see an acknowledgement now.
  async start(text) {
    this.lastText = String(text ?? "");
    await this.#enqueue(this.lastText);
  }

  setThreadId(threadId) {
    if (typeof threadId === "string" && threadId) this.threadId = threadId;
  }

  setTitle(title) {
    if (typeof title === "string" && title.trim()) this.title = title;
  }

  visibleText() { return this.lastSentText; }

  // A state boundary happened. Only the newest snapshot matters: while a write
  // is in flight, rate limited, or waiting for its one bounded retry, this
  // replaces the queued snapshot instead of stacking another write.
  push(text) {
    if (this.closed || this.sealed) return Promise.resolve();
    this.lastText = String(text ?? "");
    return this.#enqueue(this.lastText);
  }

  // Turn end: seal the card and force the final body out, so the header always
  // becomes 完成/失败/已中断 even when the body equals the last PATCH.
  async finish(text = null, { title = null } = {}) {
    if (text != null) this.lastText = String(text);
    if (title) this.setTitle(title);
    this.sealed = true;
    await this.#enqueue(this.lastText, { force: true });
  }

  #enqueue(text, { force = false } = {}) {
    const value = String(text ?? "");
    if (this.closed || !value) return Promise.resolve();
    if (!force && this.queuedText == null && value === this.lastSentText) return Promise.resolve();
    this.queuedText = value;
    return this.#drain();
  }

  // One writer at a time. The queue holds a single snapshot, so a burst of
  // events (or a flood-control window) collapses into one write of the newest
  // body instead of replaying every intermediate state. Callers that arrive
  // while a write is in flight join that loop, so finish() really waits for
  // the final body instead of racing the reply card.
  #drain() {
    if (this.closed) return Promise.resolve();
    if (this.busy) return this.drainPromise ?? Promise.resolve();
    if (this.retryTimer) return this.drainPromise ?? Promise.resolve();
    this.busy = true;
    this.drainPromise = this.#runDrain()
      .catch(error => {
        // A card write must never take a turn down; every transport call is
        // already classified, so this is belt-and-braces.
        this.logger("error", "feishu_card_drain_failed", { message: error?.message ?? String(error) });
      })
      .finally(() => {
        this.busy = false;
        this.drainPromise = null;
      });
    return this.drainPromise;
  }

  async #runDrain() {
    while (!this.closed && this.queuedText != null) {
      const text = this.queuedText;
      const outcome = await this.#deliver(text);
      if (outcome === "superseded") continue; // a newer snapshot took over
      if (outcome === "delivered" || outcome === "preview") {
        this.lastSentText = text;
        this.lastOutcome = outcome;
        if (this.queuedText === text) this.queuedText = null;
        continue;
      }
      if (outcome === "ambiguous") {
        // A timeout may already have landed. Sending a second copy would
        // duplicate the card, so delivery stays suppressed and logged.
        this.lastOutcome = "ambiguous";
        this.deliveryAmbiguous = true;
        this.queuedText = null;
        this.logger("warn", "feishu_card_delivery_ambiguous", {
          messageId: this.messageId, chars: String(text ?? "").length });
        return;
      }
      // Explicit failure. Never a retry storm: one replacement message is
      // allowed, then the turn is marked as a delivery failure.
      this.lastOutcome = "failed";
      if ((await this.#rotate(text)) === "failed") return;
    }
  }

  // One snapshot, at most two attempts (Hermes `_send_with_flood_retry`).
  async #deliver(text) {
    let result = await this.#attempt(text);
    if (result.status !== "rate") return result.status;
    // Flood control, but the body already showing is exactly this snapshot:
    // the ACKed preview IS the final content, so never resend it.
    if (this.visibleText() === text) return "preview";
    const delay = this.#boundedDelay(result.retryAfterMs);
    if (delay == null) return "failed"; // platform asked for longer than the cap
    this.retries += 1;
    this.logger("warn", "feishu_card_rate_limited", {
      messageId: this.messageId, delayMs: delay, chars: String(text ?? "").length });
    await this.#sleep(delay);
    if (this.closed) return "failed";
    if (this.queuedText !== text) return "superseded"; // merge: newest wins
    result = await this.#attempt(text);
    if (result.status === "rate") return this.visibleText() === text ? "preview" : "failed";
    return result.status;
  }

  async #attempt(text) {
    const body = clamp(text);
    const meta = { chatId: this.chatId, title: this.title, threadId: this.threadId };
    try {
      if (!this.messageId) {
        const response = await this.transport.send(body, meta);
        this.sends += 1;
        if (response?.messageId) { this.messageId = String(response.messageId); return { status: "delivered" }; }
        if (isRateLimited(response)) return { status: "rate", retryAfterMs: retryAfterOf(response) };
        if (isAmbiguous(response)) return { status: "ambiguous" };
        this.logger("warn", "feishu_card_send_failed", { code: codeOf(response), msg: messageOf(response) });
        return { status: "failed" };
      }
      this.patches += 1;
      const response = await this.transport.update(this.messageId, body, meta);
      if (response?.code === 0) { this.updates += 1; return { status: "delivered" }; }
      if (isRateLimited(response)) return { status: "rate", retryAfterMs: retryAfterOf(response) };
      if (isAmbiguous(response)) return { status: "ambiguous" };
      this.logger("warn", "feishu_card_update_failed", { code: codeOf(response), msg: messageOf(response) });
      return { status: "failed" };
    } catch (error) {
      const ambiguous = isTimeoutError(error);
      this.logger("warn", "feishu_card_write_failed", { message: error?.message ?? String(error), ambiguous });
      return { status: ambiguous ? "ambiguous" : "failed" };
    }
  }

  #boundedDelay(requestedMs) {
    const value = Number(requestedMs);
    const delay = Number.isFinite(value) && value >= 0 ? value : this.floodRetryDelayMs;
    if (delay > this.maxFloodRetryDelayMs) {
      this.logger("warn", "feishu_card_retry_too_long", {
        delayMs: delay, maxMs: this.maxFloodRetryDelayMs });
      return null;
    }
    return delay;
  }

  // The card is unusable (explicit rejection, or flood control survived the
  // bounded retry): post the latest full snapshot as a new message, keep the
  // stale card and point at the replacement. The send itself runs through the
  // same bounded path; after maxRotations the turn is a logged delivery failure.
  async #rotate(text) {
    if (!this.messageId || this.rotations >= this.maxRotations) {
      this.deliveryFailed = true;
      this.lastOutcome = "failed";
      this.queuedText = null;
      this.logger("error", "feishu_card_delivery_failed", { messageId: this.messageId,
        rotations: this.rotations, chars: String(text ?? "").length });
      return "failed";
    }
    const previous = this.messageId;
    this.rotations += 1;
    this.messageId = null; // the next attempt creates the replacement message
    this.logger("warn", "feishu_card_rotated", { previousMessageId: previous,
      rotation: this.rotations, chars: String(text ?? "").length });
    // Best-effort cleanup of the stale card, deliberately never retried.
    const meta = { chatId: this.chatId, title: this.title, threadId: this.threadId };
    this.patches += 1;
    Promise.resolve()
      .then(() => this.transport.update(previous, clamp(`${text}\n\n（本卡已停止更新，见下条）`), meta))
      .catch(() => {});
    return "rotated";
  }

  #sleep(ms) {
    return new Promise(resolve => {
      this.sleepResolve = resolve;
      this.retryTimer = this.timers.setTimeout(() => {
        this.retryTimer = null;
        this.sleepResolve = null;
        resolve();
      }, ms);
      this.retryTimer?.unref?.();
    });
  }

  async close() {
    this.closed = true;
    this.sealed = true;
    this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    if (this.sleepResolve) { const resolve = this.sleepResolve; this.sleepResolve = null; resolve(); }
    this.queuedText = null;
  }
}

// ---------------------------------------------------------------------------
// Turn progress: event-driven state, not a clock.
//
// A repeated tool is one state change even when codex runs it many times in a
// row; the caller only PATCHes when noteTool/noteText return true.
// ---------------------------------------------------------------------------

export class TurnProgress {
  constructor({ startedAt, now = Date.now, maxActivity = MAX_ACTIVITY_LINES } = {}) {
    this.startedAt = typeof startedAt === "number" ? startedAt : now();
    this.now = now;
    this.maxActivity = maxActivity;
    this.activity = [];
    this.latestText = "";
    this.lastTool = null;
  }

  noteTool(name, detail = "") {
    const label = String(name ?? "tool").trim() || "tool";
    if (label === this.lastTool) return false; // same tool back-to-back: no change
    this.lastTool = label;
    const suffix = detail ? `: ${String(detail).slice(0, 120)}` : "";
    const line = `🔧 ${label}${suffix}`;
    if (this.activity.at(-1) !== line) this.activity.push(line);
    if (this.activity.length > this.maxActivity) this.activity.splice(0, this.activity.length - this.maxActivity);
    return true;
  }

  noteText(text) {
    const value = String(text ?? "");
    if (!value || value === this.latestText) return false;
    this.latestText = value;
    return true;
  }

  render(status = null) {
    const seconds = Math.max(0, Math.round((this.now() - this.startedAt) / 1000));
    const head = { completed: "✅ 完成", error: "❌ 失败", cancelled: "⏹ 已中断", timeout: "⏱ 超时" }[status] ?? "⏳ 执行中";
    const lines = [`${head}（${seconds}s）`];
    for (const line of this.activity) lines.push(line);
    if (this.latestText) lines.push("", clamp(this.latestText, MAX_CARD_CHARS - 400));
    return lines.join("\n");
  }
}

// ---------------------------------------------------------------------------
// Channel
// ---------------------------------------------------------------------------

export class CliFeishuChannel {
  constructor({
    adapter, credentials = null, notifier = null, feishu = {}, bridgeName = "CodexWorker",
    sessionsPath, logger = defaultLogger, now = Date.now,
    timers = { setTimeout, clearTimeout }, cardOptions = null,
    websocketFactory, endpointResolver, transport = null, cwd = null, historyProbe = null,
    sessionStore = null,
  }) {
    this.adapter = adapter;
    this.bridgeName = bridgeName;
    this.logger = logger;
    this.now = now;
    this.timers = timers;
    this.cardOptions = cardOptions ?? {};
    this.websocketFactory = websocketFactory;
    this.endpointResolver = endpointResolver;
    this.streamEnabled = feishu?.streamEnabled !== false;
    this.cwd = cwd;
    // Bridge-owned history (SessionStore) is authoritative for sessions,
    // transcripts and card history; codex rollouts are only a process stream.
    this.sessionStore = sessionStore;
    this.historyProbe = historyProbe ?? (sessionStore ? historyProbeFor(sessionStore) : null);
    if (!this.historyProbe) throw new Error("CliFeishuChannel requires a sessionStore (bridge-owned history)");
    this.store = new FeishuSessionStore(sessionsPath, { logger });
    this.notifier = notifier ?? new FeishuNotifier({
      appId: credentials?.appId, appSecret: credentials?.appSecret,
      chatId: credentials?.chatId ?? null, bridgeName,
      canSend: () => !this.closed,
    });
    this.transport = transport ?? notifierCardTransport(this.notifier);
    this.whitelist = new Set((feishu?.trainerOpenIdWhitelist ?? []).filter(value => typeof value === "string" && value));
    this.bootstrapOwner = this.whitelist.size === 0;
    this.client = null;
    this.closed = false;
    this.queues = new Map();
    this.seen = new Map();
    // Feishu message_id -> reaction_id, memory only. Each message keeps its own
    // id so concurrent turns never clobber each other's typing receipt.
    this.reactions = new Map();
    this.turns = 0;
    this.failures = 0;
    this.rejected = 0;
    this.stops = 0;
    this.deliveryFailures = 0;
    this.startedAt = null;
  }

  async start() {
    await this.store.load();
    if (this.whitelist.size === 0 && this.store.state.ownerOpenId) {
      this.whitelist.add(this.store.state.ownerOpenId);
      this.bootstrapOwner = false;
      this.logger("info", "feishu_whitelist_restored", { count: this.whitelist.size });
    }
    this.client = new FeishuWebsocketClient(this.notifier, {
      onMessage: event => this.accept(event),
      onConnected: ({ connectionId }) => {
        this.logger("info", "feishu_ws_connected", { connectionId });
      },
      ...(this.websocketFactory ? { websocketFactory: this.websocketFactory } : {}),
    });
    // Test seam: the reconnect loop's signed-endpoint POST is the only call the
    // unit suite must not make to the real open.feishu.cn.
    if (this.endpointResolver) this.client._getConnUrl = this.endpointResolver;
    this.startedAt = new Date().toISOString();
    this.client.start();
    this.logger("info", "feishu_channel_started", {
      inbound: this.client.running, streamEnabled: this.streamEnabled,
      whitelistCount: this.whitelist.size, bootstrapOwner: this.bootstrapOwner,
    });
    return this;
  }

  status() {
    return {
      inboundEnabled: Boolean(this.client),
      connected: this.client?._ws?.readyState === 1,
      phase: this.client?.phase ?? "stopped",
      connection: this.client?.status?.() ?? null,
      lastError: this.client?.lastError || null,
      streamEnabled: this.streamEnabled,
      whitelistCount: this.whitelist.size,
      bootstrapOwner: this.bootstrapOwner,
      sessions: Object.keys(this.store.state.sessions).length,
      activeTurns: this.queues.size,
      reactions: this.reactions.size,
      turns: this.turns,
      failures: this.failures,
      rejected: this.rejected,
      stops: this.stops,
      deliveryFailures: this.deliveryFailures,
      startedAt: this.startedAt,
    };
  }

  // Resolves as soon as the input is durably accepted; the turn itself runs on
  // the per-sender queue so Feishu gets its ACK immediately.
  async accept(event) {
    const verdict = this.#admit(event);
    if (verdict.hint) {
      void this.notifier.sendMessage(verdict.hint, { chatId: event.chat_id, title: `${this.bridgeName}-提示` })
        .catch(() => {});
    }
    if (!verdict.ok) return { ignored: verdict.reason };
    if (this.bootstrapOwner) {
      this.whitelist.add(event.open_id);
      this.bootstrapOwner = false;
      this.logger("info", "feishu_whitelist_bootstrap", { openId: event.open_id, chatId: event.chat_id });
      await this.store.setOwner(event.open_id);
    }
    const messageId = feishuMessageId(event.message_id);
    if (this.seen.has(messageId)) return { duplicate: true, messageId };
    this.seen.set(messageId, true);
    if (this.seen.size > MAX_SEEN_MESSAGES) this.seen.delete(this.seen.keys().next().value);
    // /stop must bypass the per-sender queue: enqueueing it would make it wait
    // behind the very turn it is supposed to interrupt.
    if (isStopCommand(event.content)) {
      this.stops += 1;
      void this.#stop(event);
      return { accepted: true, messageId, command: "stop" };
    }
    // Typing receipt before anything visible. It starts now (so a message that
    // arrives while the sender is still busy is acknowledged immediately) while
    // the queue order stays synchronous, and the turn waits for it so the
    // reaction can never land after the 「输出」card.
    const acknowledged = this.#acknowledge(event);
    this.#enqueue(event.open_id, async () => {
      await acknowledged;
      try { return await this.#turn(event, messageId); }
      finally { await this.#clearReaction(event.message_id); }
    });
    return { accepted: true, messageId };
  }

  // Feishu-side /stop: interrupt the running turn for this sender. A thread
  // with nothing running answers no_active_turn instead of an error, so the
  // command is idempotent.
  async #stop(event) {
    const acknowledged = this.#acknowledge(event);
    const record = this.store.get(event.open_id) ?? {};
    const threadId = typeof record.threadId === "string" && record.threadId ? record.threadId : null;
    let outcome;
    try {
      outcome = typeof this.adapter.stop === "function"
        ? await this.adapter.stop(threadId)
        : { status: "unsupported" };
    } catch (error) {
      this.logger("error", "feishu_stop_failed", { threadId,
        message: error?.message ?? String(error) });
      outcome = { status: "failed", reason: shortError(error) };
    }
    const status = outcome?.status ?? "no_active_turn";
    const text = status === "cancelled" ? "⏹ 已中断当前回合。"
      : status === "no_active_turn" ? "当前没有在执行的回合（no_active_turn）。"
      : status === "unsupported" ? "当前后端不支持停止。"
      : `停止失败：${outcome?.reason ?? status}`;
    await acknowledged;
    await this.notifier.sendMessage(text, { chatId: event.chat_id,
      title: `${this.bridgeName}-提示`, threadId }).catch(() => {});
    await this.#clearReaction(event.message_id);
    this.logger("info", "feishu_stop", { threadId, status });
    return { status };
  }

  // Desktop parity (desktop-manager.mjs handleFeishuInput): the trainer's own
  // message gets a reaction as the receipt. A missing permission, a bad token
  // or a network error is only logged — the turn must run regardless.
  #acknowledge(event) {
    const messageId = event.message_id;
    if (typeof this.notifier?.addReaction !== "function") return Promise.resolve(null);
    return this.notifier.addReaction(messageId).then(reactionId => {
      const id = reactionId ? String(reactionId) : null;
      // Keyed by message id: two messages in flight never share a receipt.
      if (id) this.reactions.set(messageId, id);
      return id;
    }).catch(error => {
      this.logger("warn", "feishu_reaction_failed", { stage: "add", messageId,
        code: error?.code ?? null, message: error?.message ?? String(error) });
      return null;
    });
  }

  async #clearReaction(messageId) {
    const reactionId = this.reactions.get(messageId) ?? null;
    this.reactions.delete(messageId);
    if (!reactionId || typeof this.notifier?.removeReaction !== "function") return;
    try {
      const removed = await this.notifier.removeReaction(messageId, reactionId);
      if (!removed) this.logger("warn", "feishu_reaction_remove_failed", { messageId, reactionId });
    } catch (error) {
      this.logger("warn", "feishu_reaction_remove_failed", { messageId, reactionId,
        code: error?.code ?? null, message: error?.message ?? String(error) });
    }
  }

  #admit(event) {
    if (!event || typeof event !== "object") return { ok: false, reason: "malformed" };
    // Groups, bot senders and empty payloads are never trainer input.
    if (event.chat_type !== "p2p") return { ok: false, reason: "not_p2p" };
    if (event.sender_type && event.sender_type !== "user") return { ok: false, reason: "not_user" };
    if (event.message_type !== "text") {
      const hint = event.message_type === "image"
        ? "CLI 模式暂只支持文本消息：图片不会转发给 Codex。"
        : `CLI 模式暂不支持 ${event.message_type} 消息，请发送文本。`;
      return { ok: false, reason: "unsupported_type", hint: this.#allowed(event) ? hint : null };
    }
    if (!event.content || !event.content.trim()) return { ok: false, reason: "empty" };
    if (!event.open_id || !event.message_id) return { ok: false, reason: "missing_ids" };
    if (!this.#allowed(event)) {
      this.rejected++;
      this.logger("warn", "feishu_sender_rejected", { openId: event.open_id, chatId: event.chat_id });
      return { ok: false, reason: "sender_not_whitelisted" };
    }
    return { ok: true };
  }

  #allowed(event) {
    return this.bootstrapOwner || this.whitelist.has(event.open_id);
  }

  #enqueue(openId, task) {
    const tail = this.queues.get(openId) ?? Promise.resolve();
    const run = tail.then(task).catch(error => {
      this.failures++;
      this.logger("error", "feishu_turn_failed", {
        status: error?.status ?? null, message: error?.message ?? String(error),
      });
    });
    this.queues.set(openId, run);
    void run.finally(() => { if (this.queues.get(openId) === run) this.queues.delete(openId); });
    return run;
  }

  async #turn(event, messageId) {
    const record = this.store.get(event.open_id) ?? {};
    let threadId = typeof record.threadId === "string" && record.threadId ? record.threadId : null;
    const started = this.now();
    let card = null;

    // The bridge-owned store is the only history source. Only a genuinely
    // unreadable store (missing/corrupt) falls back to a fresh session; the
    // reason is logged and shown once, never appended to a card title.
    let historyNotice = "";
    if (threadId) {
      const probe = await this.historyProbe(threadId)
        .catch(error => ({ ok: false, reason: `history_probe_failed:${error?.message ?? String(error)}` }));
      if (probe && probe.ok === false) {
        historyNotice = `⚠️ 会话记录不可读（${probe.reason}），本轮已开新会话。`;
        this.logger("warn", "feishu_history_degraded", { threadId, reason: probe.reason, action: "new_session" });
        threadId = null;
      }
    }
    if (!threadId) {
      const title = event.content.replace(/\s+/g, " ").slice(0, 80);
      const thread = await this.adapter.register({ cwd: this.cwd ?? null, title });
      threadId = thread.id;
      await this.sessionStore.create({ threadId, cwd: thread.cwd ?? this.cwd ?? null,
        source: "feishu", title: thread.title ?? title });
      await this.store.set(event.open_id, { threadId, chatId: event.chat_id, turns: Number(record.turns ?? 0) });
    }
    // Durable before the turn: a crash mid-turn leaves the trainer's message on
    // record instead of losing it with the codex process. A disk error must not
    // kill the turn — it is logged and the reply still goes out.
    try {
      await this.sessionStore.appendMessage(threadId, { role: "user", text: event.content,
        messageId, source: "feishu" });
    } catch (error) {
      this.logger("error", "feishu_history_write_failed", { stage: "user", threadId,
        message: error?.message ?? String(error) });
    }

    // Event-driven progress: render() is only pushed when noteTool/noteText
    // report a real state change, so a repeated tool costs one PATCH, not one
    // per invocation, and no timer ever fires on its own.
    const progress = new TurnProgress({ startedAt: started, now: this.now });

    if (this.streamEnabled) {
      card = new OutputCard({ transport: this.transport, chatId: event.chat_id, threadId,
        title: `${this.bridgeName}-输出`, now: this.now, timers: this.timers,
        logger: this.logger, ...this.cardOptions });
      await card.start(historyNotice || "⏳ 已收到，正在执行…");
    }

    const onEvent = emitted => {
      if (!card) return;
      try {
        // adapter events carry the Codex session id as soon as `thread.started`
        // has been seen; use it so even the first 输出 card names the session.
        if (typeof emitted?.threadId === "string" && emitted.threadId) card.setThreadId(emitted.threadId);
        if (emitted?.type === "tool.started" || emitted?.type === "tool.completed") {
          const payload = emitted.payload ?? {};
          const name = payload.name ?? payload.type ?? "tool";
          const detail = payload.command ? String(payload.command) : "";
          // Same tool back-to-back is one state change: no PATCH at all.
          if (progress.noteTool(name, detail)) card.push(progress.render());
        } else if (emitted?.type === "item.completed" && emitted.payload?.kind === "agent_message") {
          if (progress.noteText(emitted.payload.snapshot)) card.push(progress.render());
        }
      } catch { /* progress rendering never fails a turn */ }
    };

    this.logger("info", "feishu_turn_started", { messageId, threadId, chars: event.content.length });
    let result;
    try {
      result = await this.adapter.dispatch({ threadId, text: event.content, cwd: this.cwd, messageId, onEvent });
    } catch (error) {
      if (card) {
        card.setTitle(`${this.bridgeName}-失败`);
        await card.finish(progress.render("error"));
        if (card.deliveryFailed) this.deliveryFailures += 1;
      }
      await this.notifier.sendMessage(`本轮失败：${shortError(error)}`, {
        chatId: event.chat_id, title: `${this.bridgeName}-回复`, threadId,
      }).catch(() => {});
      throw error;
    }

    if (card) {
      card.setThreadId(result.threadId ?? threadId);
      const status = result.status ?? "completed";
      // Turn end is always a PATCH moment: the header becomes 完成/失败/已中断
      // even when the body did not change since the last tool event.
      await card.finish(progress.render(status),
        status === "cancelled" ? { title: `${this.bridgeName}-已中断` } : {});
      if (card.deliveryFailed) {
        this.deliveryFailures += 1;
        this.logger("error", "feishu_turn_delivery_failed", {
          threadId: result.threadId ?? threadId, messageId: card.messageId,
        });
      }
    }
    const partial = String(result.resultText ?? "").trim();
    const text = result.status === "cancelled"
      ? (partial ? `${partial}\n\n⏹ 已中断` : "⏹ 已中断（/stop）")
      : partial || "（本轮无文本输出）";
    // Reply first (the trainer's expectation), then persist — but both before
    // the next queued turn so history order matches what was seen.
    const reply = await this.notifier.sendMessage(text, {
      chatId: event.chat_id, title: `${this.bridgeName}-回复`, threadId: result.threadId ?? threadId,
    }).catch(() => null);
    const finalThreadId = result.threadId ?? threadId;
    try {
      // A cancelled turn has no agent text; record the interruption so the
      // transcript matches the「已中断」card instead of looking like a no-op.
      const stored = result.status === "cancelled" ? text : String(result.resultText ?? "").trim();
      if (stored) {
        await this.sessionStore.appendMessage(finalThreadId, { role: "assistant", text: stored,
          messageId: result.turnId ?? null, turnId: result.turnId ?? null, source: "feishu" });
      }
      await this.sessionStore.setMeta(finalThreadId, {
        sessionId: result.sessionId ?? null,
        updatedAt: new Date().toISOString(),
        ...(result.status && result.status !== "completed" ? { lastStatus: result.status } : {}),
      });
    } catch (error) {
      this.logger("error", "feishu_history_write_failed", { stage: "assistant", threadId: finalThreadId,
        message: error?.message ?? String(error) });
    }
    await this.store.set(event.open_id, {
      threadId: finalThreadId,
      sessionId: result.sessionId ?? null,
      chatId: event.chat_id,
      turns: Number(record.turns ?? 0) + 1,
      lastStatus: result.status ?? null,
      lastAt: new Date().toISOString(),
    });
    this.turns++;
    this.logger("info", "feishu_turn_completed", {
      threadId: finalThreadId, sessionId: result.sessionId ?? null, status: result.status ?? null,
      durationMs: result.durationMs ?? null, chars: text.length,
      delivered: reply?.code === 0,
    });
    return result;
  }

  async close() {
    this.closed = true;
    this.client?.stop();
    this.client = null;
    await Promise.allSettled([...this.queues.values()]);
    this.queues.clear();
  }
}

// Factory used by cli.mjs: config + adapter in, started (or throwing) channel out.
export async function startCliFeishuChannel({ config = {}, adapter, stateDir, bridgeName = "CodexWorker",
  logger = defaultLogger, sessionStore = null, ...overrides }) {
  const feishu = config.feishu ?? {};
  if (!feishu.configFile) throw new Error("feishu.configFile 未配置，无法启动飞书长连接");
  if (!sessionStore) throw new Error("startCliFeishuChannel requires the bridge SessionStore");
  const credentials = await loadFeishuCredentials(feishu.configFile, { logger });
  const channel = new CliFeishuChannel({
    adapter, credentials, feishu, bridgeName, logger, sessionStore,
    sessionsPath: path.join(stateDir, "feishu-sessions.json"),
    cwd: config.workspace ?? config.cwd ?? null,
    ...overrides,
  });
  await channel.start();
  return channel;
}
