// feishu-cli.mjs — Feishu long-connection channel for the **CLI** (native-cli)
// backend. Glue only: it reuses feishu.mjs (FeishuNotifier /
// FeishuWebsocketClient) and native-cli.mjs (CliNativeAdapter.dispatch), and
// adds the CLI-specific pieces the Desktop manager used to own:
//
//   inbound p2p text -> typing reaction on the trainer's own message
//   -> one persistent Codex session per sender (open_id)
//   -> exactly one final message (「回复」card: text + session id)
//   -> reaction removed (CrossMark instead when the turn failed)
//
// This mirrors Hermes' own Feishu adapter: no progress card, no PATCH, no
// timer. `edit_message` exists upstream but has no caller on the Feishu path;
// the trainer sees a Typing badge while the turn runs and one answer at the
// end, never the same content twice.
//
// It never touches the Desktop path (desktop-manager.mjs / server.mjs); a CLI
// process has no writer lease, no Desktop relay and no question cards.
import { createHash } from "node:crypto";
import { mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { FEISHU_API_BASE, FeishuNotifier, FeishuWebsocketClient, cardPayload } from "./feishu.mjs";
import { FeishuSdkWebsocketClient } from "./feishu-sdk-ws.mjs";
import { FEISHU_MAX_MESSAGE_CHARS, splitForFeishu } from "./hermes-feishu-format.mjs";
import { expandHome } from "./native-cli.mjs";

// Long-connection transport. `sdk` is the official @larksuiteoapi/node-sdk
// (fragment merging + ACK are its job); `native` is the self-written client
// kept for rollback. Anything unrecognised falls back to sdk — the transport
// that is actually maintained upstream.
export const FEISHU_WS_IMPLS = Object.freeze(["sdk", "native"]);
export function resolveFeishuWsImpl(value) {
  const impl = String(value ?? process.env.FEISHU_WS_IMPL ?? "").trim().toLowerCase();
  if (!impl) return "sdk";
  if (FEISHU_WS_IMPLS.includes(impl)) return impl;
  console.warn(`[CodexWorker] 未知 FEISHU_WS_IMPL=${impl}，回退 sdk`);
  return "sdk";
}

const STATE_VERSION = 1;
const MAX_SEEN_MESSAGES = 500;
// Reactions are the only in-flight feedback (Hermes parity): Typing while the
// turn runs, removed on success, replaced by CrossMark on failure. Both types
// are verified against the live Feishu reaction API.
const WORKING_REACTION = "Typing";
const FAILURE_REACTION = "CrossMark";
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
// transcripts. Only a genuinely unreadable store (missing/corrupt file)
// degrades to a new session, with the reason logged and shown once — the
// message title itself is always `-回复`.
// ---------------------------------------------------------------------------

export function historyProbeFor(sessionStore) {
  return async threadId => sessionStore.read(threadId, { limit: 1 });
}

// ---------------------------------------------------------------------------
// The one final message (Hermes parity).
//
// Hermes' Feishu adapter posts no progress card at all: the trainer's message
// gets a Typing reaction, the turn runs silently, and exactly one final message
// carries the answer. This class is that single send, keeping the delivery
// discipline the old progress card had:
//
//   delivered — Feishu acknowledged the message
//   preview   — the same body is already visible from an earlier ACK; no resend
//   ambiguous — timeout: the write may have landed, so a resend could duplicate
//   failed    — explicit rejection after the bounded retry: one fresh message
//
// A rate-limited send (HTTP 429 / code 99991400 / a frequency message) is
// retried once, after `retry_after` or 3s, capped at 30s. The retry is
// abandoned — never stacked — when the platform asks for longer than the cap.
// There is no endless 1/2/4/8 ladder, and nothing is ever PATCHed.
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
// error. Those are "ambiguous": resending would duplicate the message.
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

// The only outbound call left: one interactive card per turn. The notifier
// keeps markdown/chunk/token handling; this adapter only classifies the result.
export function notifierTransport(notifier) {
  return {
    async send(text, { chatId, title, threadId }) {
      const response = await notifier.sendMessage(text, { chatId, title, threadId });
      return { code: response?.code === 0 ? 0 : (Number.isInteger(response?.code) ? response.code : -1),
        messageId: response?.code === 0 ? String(response?.data?.message_id ?? "") || null : null };
    },
  };
}

export class FinalMessage {
  constructor({ transport, chatId, threadId = null, title,
    timers = { setTimeout, clearTimeout }, logger = defaultLogger,
    floodRetryDelayMs = DEFAULT_FLOOD_RETRY_DELAY_MS,
    maxFloodRetryDelayMs = MAX_FLOOD_RETRY_DELAY_MS,
    maxResends = 1, maxChars = FEISHU_MAX_MESSAGE_CHARS, split = splitForFeishu }) {
    this.transport = transport;
    this.chatId = chatId;
    this.threadId = threadId;
    this.title = title;
    this.timers = timers;
    this.logger = logger;
    this.floodRetryDelayMs = Math.max(0, Number(floodRetryDelayMs) || 0);
    this.maxFloodRetryDelayMs = Math.max(this.floodRetryDelayMs, Number(maxFloodRetryDelayMs) || 0);
    this.maxResends = Math.max(0, Number(maxResends) || 0);
    this.maxChars = maxChars;
    this.split = split;
    this.messageId = null;     // last acknowledged message id
    this.messageIds = [];      // one per delivered chunk
    this.chunks = 0;
    this.text = "";
    this.deliveredKeys = new Set(); // "title\u0000body" already ACKed (never re-sent)
    // Last delivery outcome, same vocabulary as Hermes' stream fallback:
    //   delivered / preview / ambiguous / failed
    this.lastOutcome = null;
    this.deliveryFailed = false;
    this.deliveryAmbiguous = false;
    this.closed = false;
    this.retryTimer = null;
    this.sleepResolve = null;
    this.sends = 0;      // transport.send attempts (initial + retries + resends)
    this.retries = 0;    // bounded flood retries
    this.resends = 0;    // replacement messages after an explicit rejection
  }

  // The turn is over: post the reply. A body over the per-message cap is split
  // at paragraph/newline boundaries; the title only rides the first message and
  // continuations are titled 「（续 N/M）」. Every chunk goes through the same
  // bounded retry and single-resend discipline, so a flood window can never
  // duplicate a chunk that already landed.
  async deliver(text, { title = null, threadId = null } = {}) {
    if (this.closed) return "failed";
    if (typeof title === "string" && title.trim()) this.title = title.trim();
    if (typeof threadId === "string" && threadId) this.threadId = threadId;
    const body = String(text ?? "");
    this.text = body;
    const chunks = this.split(body, this.maxChars);
    this.chunks = chunks.length;
    const pending = [];
    for (const [index, chunk] of chunks.entries()) {
      const chunkTitle = index === 0 ? this.title : `${this.title}（续 ${index + 1}/${chunks.length}）`;
      if (!this.deliveredKeys.has(`${chunkTitle}\u0000${chunk}`)) pending.push({ chunk, title: chunkTitle, index });
    }
    if (!pending.length) {
      // The exact chunks are already on screen from an earlier ACK: resending
      // would be the duplicate copy this refactor exists to remove.
      this.lastOutcome = "preview";
      return "preview";
    }
    let outcome = "delivered";
    for (const item of pending) {
      outcome = await this.#deliverChunk(item.chunk, item.title);
      this.lastOutcome = outcome;
      if (outcome === "ambiguous") {
        this.deliveryAmbiguous = true;
        this.logger("warn", "feishu_reply_delivery_ambiguous", {
          messageId: this.messageId, chunk: item.index + 1, chunks: chunks.length, chars: item.chunk.length });
        break;
      }
      if (outcome === "failed") {
        this.deliveryFailed = true;
        this.logger("error", "feishu_reply_delivery_failed", {
          messageId: this.messageId, resends: this.resends, chunk: item.index + 1,
          chunks: chunks.length, chars: item.chunk.length });
        break;
      }
      this.deliveredKeys.add(`${item.title}\u0000${item.chunk}`);
    }
    this.lastOutcome = outcome;
    return outcome;
  }

  async #deliverChunk(text, title) {
    let outcome = await this.#attemptWithRetry(text, title);
    if (outcome === "failed" && this.resends < this.maxResends && !this.closed) {
      // Explicit rejection: one fresh message, same title and body. A resend is
      // never attempted after an ambiguous write (that could duplicate).
      this.resends += 1;
      this.messageId = null;
      this.logger("warn", "feishu_reply_resend", { resend: this.resends, chars: text.length });
      outcome = await this.#attemptWithRetry(text, title);
    }
    return outcome;
  }

  // One body, at most two attempts (Hermes `_send_with_flood_retry`).
  async #attemptWithRetry(text, title) {
    let result = await this.#attempt(text, title);
    if (result.status !== "rate") return result.status;
    const delay = this.#boundedDelay(result.retryAfterMs);
    if (delay == null) return "failed"; // platform asked for longer than the cap
    this.retries += 1;
    this.logger("warn", "feishu_reply_rate_limited", {
      messageId: this.messageId, delayMs: delay, chars: String(text ?? "").length });
    await this.#sleep(delay);
    if (this.closed) return "failed";
    result = await this.#attempt(text, title);
    return result.status === "rate" ? "failed" : result.status;
  }

  async #attempt(text, title) {
    const meta = { chatId: this.chatId, title, threadId: this.threadId };
    try {
      this.sends += 1;
      const response = await this.transport.send(text, meta);
      if (response?.messageId) {
        this.messageId = String(response.messageId);
        this.messageIds.push(this.messageId);
        return { status: "delivered" };
      }
      if (isRateLimited(response)) return { status: "rate", retryAfterMs: retryAfterOf(response) };
      if (isAmbiguous(response)) return { status: "ambiguous" };
      this.logger("warn", "feishu_reply_send_failed", { code: codeOf(response), msg: messageOf(response) });
      return { status: "failed" };
    } catch (error) {
      const ambiguous = isTimeoutError(error);
      this.logger("warn", "feishu_reply_write_failed", { message: error?.message ?? String(error), ambiguous });
      return { status: ambiguous ? "ambiguous" : "failed" };
    }
  }

  #boundedDelay(requestedMs) {
    const value = Number(requestedMs);
    const delay = Number.isFinite(value) && value >= 0 ? value : this.floodRetryDelayMs;
    if (delay > this.maxFloodRetryDelayMs) {
      this.logger("warn", "feishu_reply_retry_too_long", {
        delayMs: delay, maxMs: this.maxFloodRetryDelayMs });
      return null;
    }
    return delay;
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

  close() {
    this.closed = true;
    this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    if (this.sleepResolve) { const resolve = this.sleepResolve; this.sleepResolve = null; resolve(); }
  }
}

// ---------------------------------------------------------------------------
// Channel
// ---------------------------------------------------------------------------

export class CliFeishuChannel {
  constructor({
    adapter, credentials = null, notifier = null, feishu = {}, bridgeName = "CodexWorker",
    sessionsPath, logger = defaultLogger,
    timers = { setTimeout, clearTimeout }, deliveryOptions = null,
    websocketFactory, endpointResolver, transport = null, cwd = null, historyProbe = null,
    sessionStore = null, feishuImpl = null, sdk = null,
  }) {
    this.adapter = adapter;
    this.bridgeName = bridgeName;
    this.logger = logger;
    this.timers = timers;
    // deliveryOptions tune the single final message (flood retry / resend cap).
    this.deliveryOptions = deliveryOptions ?? {};
    this.websocketFactory = websocketFactory;
    this.endpointResolver = endpointResolver;
    // Transport choice: explicit option (tests) > FEISHU_WS_IMPL > "sdk".
    // `sdk` is the official module, injectable so the unit suite never opens a
    // real long connection.
    this.feishuImpl = resolveFeishuWsImpl(feishuImpl);
    this.sdk = sdk;
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
    this.transport = transport ?? notifierTransport(this.notifier);
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
    this.replies = 0;         // final messages Feishu acknowledged
    this.sendAttempts = 0;    // transport.send calls (retries/resends included)
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
    const Impl = this.feishuImpl === "native" ? FeishuWebsocketClient : FeishuSdkWebsocketClient;
    this.client = new Impl(this.notifier, {
      onMessage: event => this.accept(event),
      onConnected: ({ connectionId }) => {
        this.logger("info", "feishu_ws_connected", { connectionId, impl: this.feishuImpl });
      },
      // Only the native transport understands these two seams; the SDK takes
      // its signed endpoint and socket from the official module.
      ...(this.feishuImpl === "native" && this.websocketFactory ? { websocketFactory: this.websocketFactory } : {}),
      ...(this.feishuImpl === "sdk" && this.sdk ? { sdk: this.sdk } : {}),
    });
    // Test seam: the reconnect loop's signed-endpoint POST is the only call the
    // unit suite must not make to the real open.feishu.cn.
    if (this.feishuImpl === "native" && this.endpointResolver) this.client._getConnUrl = this.endpointResolver;
    this.startedAt = new Date().toISOString();
    this.client.start();
    this.logger("info", "feishu_channel_started", {
      inbound: this.client.running, replyMode: "single-message", impl: this.feishuImpl,
      whitelistCount: this.whitelist.size, bootstrapOwner: this.bootstrapOwner,
    });
    return this;
  }

  status() {
    return {
      inboundEnabled: Boolean(this.client),
      connected: this.client?.connected === true,
      phase: this.client?.phase ?? "stopped",
      impl: this.feishuImpl,
      connection: this.client?.status?.() ?? null,
      lastError: this.client?.lastError || null,
      replyMode: "single-message",
      // What Feishu cards are titled with (the instance display name).
      displayName: this.bridgeName,
      whitelistCount: this.whitelist.size,
      bootstrapOwner: this.bootstrapOwner,
      sessions: Object.keys(this.store.state.sessions).length,
      activeTurns: this.queues.size,
      reactions: this.reactions.size,
      turns: this.turns,
      failures: this.failures,
      rejected: this.rejected,
      stops: this.stops,
      replies: this.replies,
      sendAttempts: this.sendAttempts,
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
    // the queue order stays synchronous; the turn waits for it so the badge
    // always lands before the single final message.
    const acknowledged = this.#acknowledge(event);
    this.#enqueue(event.open_id, async () => {
      await acknowledged;
      let failed = false;
      try { return await this.#turn(event, messageId); }
      catch (error) { failed = true; throw error; }
      finally { await this.#settleReaction(event.message_id, { failed }); }
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
    await acknowledged;
    // A cancelled turn answers for itself: its one final message carries
    // 「⏹ 已中断」, so a second notice here would be the duplicate this channel
    // just removed. Every other outcome has no turn reply and needs a notice.
    if (status !== "cancelled") {
      const text = status === "no_active_turn" ? "当前没有在执行的回合（no_active_turn）。"
        : status === "unsupported" ? "当前后端不支持停止。"
        : `停止失败：${outcome?.reason ?? status}`;
      await this.notifier.sendMessage(text, { chatId: event.chat_id,
        title: `${this.bridgeName}-提示`, threadId }).catch(() => {});
    }
    await this.#settleReaction(event.message_id, { failed: status === "failed" });
    this.logger("info", "feishu_stop", { threadId, status });
    return { status };
  }

  // Desktop parity (desktop-manager.mjs handleFeishuInput): the trainer's own
  // message gets a reaction as the receipt. A missing permission, a bad token
  // or a network error is only logged — the turn must run regardless.
  #acknowledge(event) {
    const messageId = event.message_id;
    if (typeof this.notifier?.addReaction !== "function") return Promise.resolve(null);
    return this.notifier.addReaction(messageId, WORKING_REACTION).then(reactionId => {
      const id = reactionId ? String(reactionId) : null;
      // Keyed by message id: two messages in flight never share a receipt.
      if (id) this.reactions.set(messageId, id);
      return id;
    }).catch(error => {
      this.logger("warn", "feishu_reaction_failed", { stage: "add", emoji: WORKING_REACTION, messageId,
        code: error?.code ?? null, message: error?.message ?? String(error) });
      return null;
    });
  }

  // Turn end: drop the Typing badge. A failed turn swaps it for CrossMark, the
  // way Hermes' adapter does; if the Typing removal did not land, no second
  // badge is stacked (the UI would read as both working and failed).
  async #settleReaction(messageId, { failed = false } = {}) {
    const reactionId = this.reactions.get(messageId) ?? null;
    if (reactionId) {
      if (typeof this.notifier?.removeReaction === "function") {
        let removed = false;
        try { removed = await this.notifier.removeReaction(messageId, reactionId); }
        catch (error) {
          this.logger("warn", "feishu_reaction_remove_failed", { messageId, reactionId,
            code: error?.code ?? null, message: error?.message ?? String(error) });
          return;
        }
        if (!removed) {
          this.logger("warn", "feishu_reaction_remove_failed", { messageId, reactionId });
          return;
        }
      }
      this.reactions.delete(messageId);
    }
    if (!failed || typeof this.notifier?.addReaction !== "function") return;
    try {
      const id = await this.notifier.addReaction(messageId, FAILURE_REACTION);
      // Terminal state: the badge stays on the message, so no handle is kept
      // (nothing will remove it later, and the map would only grow).
      if (id) this.logger("info", "feishu_reaction_marked_failed", { messageId, emoji: FAILURE_REACTION });
    } catch (error) {
      this.logger("warn", "feishu_reaction_failed", { stage: "failure", emoji: FAILURE_REACTION, messageId,
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

    // The bridge-owned store is the only history source. Only a genuinely
    // unreadable store (missing/corrupt) falls back to a fresh session; the
    // reason is logged and shown once in the reply, never in the title.
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

    // No progress card, no timer, no PATCH: the badge on the trainer's own
    // message is the only "working" signal until the answer is ready.
    this.logger("info", "feishu_turn_started", { messageId, threadId, chars: event.content.length });
    let result;
    try {
      result = await this.adapter.dispatch({ threadId, text: event.content, cwd: this.cwd, messageId });
    } catch (error) {
      // Same single message, same card shape, error summary as the body.
      const notice = `本轮失败：${shortError(error)}`;
      await this.#deliverFinal(historyNotice ? `${historyNotice}\n\n${notice}` : notice,
        { chatId: event.chat_id, threadId, title: `${this.bridgeName}-回复` });
      throw error;
    }

    const finalThreadId = result.threadId ?? threadId;
    const partial = String(result.resultText ?? "").trim();
    let text = result.status === "cancelled"
      ? (partial ? `${partial}\n\n⏹ 已中断` : "⏹ 已中断（/stop）")
      : partial || "（本轮无文本输出）";
    if (historyNotice) text = `${historyNotice}\n\n${text}`;
    // Reply first (the trainer's expectation), then persist — but both before
    // the next queued turn so history order matches what was seen.
    const delivery = await this.#deliverFinal(text, {
      chatId: event.chat_id, threadId: finalThreadId, title: `${this.bridgeName}-回复`,
    });
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
      // One send for a normal turn; more only when a flood retry or the single
      // allowed resend happened. `sendAttempts` counts everything on the wire.
      sendCount: delivery.sends, deliveryOutcome: delivery.lastOutcome,
      delivered: delivery.lastOutcome === "delivered" || delivery.lastOutcome === "preview",
    });
    return result;
  }

  // The only outbound write of a turn, shared by the success, failure and
  // /stop paths so none of them can invent a second message.
  async #deliverFinal(text, { chatId, threadId, title }) {
    const message = new FinalMessage({ transport: this.transport, chatId, threadId, title,
      timers: this.timers, logger: this.logger, ...this.deliveryOptions });
    const outcome = await message.deliver(text);
    this.sendAttempts += message.sends;
    if (message.deliveryFailed) this.deliveryFailures += 1;
    // One per message that landed (a long reply is several, by design).
    this.replies += message.messageIds.length;
    this.logger("info", "feishu_reply_delivery", {
      threadId: message.threadId, title: message.title, outcome,
      sends: message.sends, retries: message.retries, resends: message.resends,
      chunks: message.chunks, messageIds: message.messageIds,
      messageId: message.messageId, chars: String(text ?? "").length,
    });
    return message;
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
