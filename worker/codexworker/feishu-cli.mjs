// feishu-cli.mjs — Feishu long-connection channel for the **CLI** (native-cli)
// backend. Glue only: it reuses feishu.mjs (FeishuNotifier /
// FeishuWebsocketClient) and native-cli.mjs (CliNativeAdapter.dispatch), and
// adds the CLI-specific pieces the Desktop manager used to own:
//
//   inbound p2p text -> typing reaction on the trainer's own message
//   -> one persistent Codex session per sender (open_id)
//   -> 「输出」card (throttled progress) -> 「回复」card (final text + session id)
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
const DEFAULT_STREAM_INTERVAL_MS = 1800; // 1.5–2s: enough for a live card, few PATCHes.
const MAX_CARD_CHARS = 6000;
const MAX_ACTIVITY_LINES = 6;

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
// 「输出」card: one message, PATCHed in place at most once per interval.
// A rejected PATCH (message too old, card API change) degrades to new messages,
// which is exactly what the Desktop beat does.
// ---------------------------------------------------------------------------

async function patchMessageCard(token, messageId, payload) {
  try {
    const response = await fetch(`${FEISHU_API_BASE}/im/v1/messages/${encodeURIComponent(messageId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json; charset=utf-8", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ content: payload }),
      signal: AbortSignal.timeout(15000),
    });
    return await response.json();
  } catch (error) {
    return { code: -1, msg: `network error: ${error?.message ?? String(error)}` };
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
  constructor({ transport, chatId, threadId = null, title, intervalMs = DEFAULT_STREAM_INTERVAL_MS,
    now = Date.now, timers = { setTimeout, clearTimeout }, logger = defaultLogger }) {
    this.transport = transport;
    this.chatId = chatId;
    this.threadId = threadId;
    this.title = title;
    this.intervalMs = Math.max(0, Number(intervalMs) || 0);
    this.now = now;
    this.timers = timers;
    this.logger = logger;
    this.messageId = null;
    this.updateBroken = false;
    this.closed = false;
    this.lastText = "";
    this.lastSentText = "";
    this.lastSentAt = 0;
    this.timer = null;
    this.sends = 0;
    this.updates = 0;
  }

  // First write is immediate: the trainer must see an acknowledgement now.
  async start(text) { await this.#write(text); }

  setThreadId(threadId) {
    if (typeof threadId === "string" && threadId) this.threadId = threadId;
  }

  push(text) {
    if (this.closed) return;
    this.lastText = text;
    const wait = this.lastSentAt === 0 ? 0 : this.intervalMs - (this.now() - this.lastSentAt);
    this.#schedule(wait > 0 ? wait : 0);
  }

  #schedule(delay) {
    if (this.closed || this.timer) return; // the pending flush will read lastText
    this.timer = this.timers.setTimeout(() => { this.timer = null; void this.#flush(); }, delay);
    this.timer?.unref?.();
  }

  async #flush() {
    if (this.closed || !this.lastText || this.lastText === this.lastSentText) return;
    await this.#write(this.lastText);
    if (!this.closed && this.lastText !== this.lastSentText) {
      const wait = Math.max(0, this.intervalMs - (this.now() - this.lastSentAt));
      this.#schedule(wait);
    }
  }

  // Final write of the pending body, then seal the card. Called at turn end so
  // a fast turn still leaves the tool/text progress it accumulated.
  async finish(text = null) {
    if (text != null) this.lastText = String(text);
    this.timers.clearTimeout(this.timer);
    this.timer = null;
    if (!this.closed && this.lastText && this.lastText !== this.lastSentText) await this.#write(this.lastText);
    await this.close();
  }

  async #write(text) {
    if (this.closed) return;
    const body = clamp(text);
    this.lastSentText = text;
    this.lastSentAt = this.now();
    const meta = { chatId: this.chatId, title: this.title, threadId: this.threadId };
    try {
      if (!this.messageId) {
        const response = await this.transport.send(body, meta);
        this.sends++;
        if (response?.messageId) this.messageId = response.messageId;
        else {
          this.updateBroken = true; // cannot PATCH an unknown id: keep sending
          this.logger("warn", "feishu_card_send_failed", { code: Number.isInteger(response?.code) ? response.code : null });
        }
        return;
      }
      if (!this.updateBroken) {
        const response = await this.transport.update(this.messageId, body, meta);
        if (response?.code === 0) { this.updates++; return; }
        this.updateBroken = true;
        this.logger("warn", "feishu_card_update_failed", { code: Number.isInteger(response?.code) ? response.code : null });
      }
      await this.transport.send(body, meta);
      this.sends++;
    } catch (error) {
      this.logger("warn", "feishu_card_write_failed", { message: error?.message ?? String(error) });
    }
  }

  async close() {
    this.closed = true;
    this.timers.clearTimeout(this.timer);
    this.timer = null;
  }
}

// ---------------------------------------------------------------------------
// Channel
// ---------------------------------------------------------------------------

export class CliFeishuChannel {
  constructor({
    adapter, credentials = null, notifier = null, feishu = {}, bridgeName = "CodexWorker",
    sessionsPath, logger = defaultLogger, now = Date.now,
    timers = { setTimeout, clearTimeout }, streamIntervalMs = DEFAULT_STREAM_INTERVAL_MS,
    websocketFactory, endpointResolver, transport = null, cwd = null, historyProbe = null,
    sessionStore = null,
  }) {
    this.adapter = adapter;
    this.bridgeName = bridgeName;
    this.logger = logger;
    this.now = now;
    this.timers = timers;
    this.streamIntervalMs = streamIntervalMs;
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
    const activity = [];
    let latestText = "";
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

    const render = () => {
      const seconds = Math.max(0, Math.round((this.now() - started) / 1000));
      const lines = [`⏳ 执行中（${seconds}s）`];
      for (const line of activity.slice(-MAX_ACTIVITY_LINES)) lines.push(line);
      if (latestText) lines.push("", clamp(latestText, MAX_CARD_CHARS - 400));
      return lines.join("\n");
    };

    if (this.streamEnabled) {
      card = new OutputCard({ transport: this.transport, chatId: event.chat_id, threadId,
        title: `${this.bridgeName}-输出`, intervalMs: this.streamIntervalMs,
        now: this.now, timers: this.timers, logger: this.logger });
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
          const detail = payload.command ? `: ${String(payload.command).slice(0, 120)}` : "";
          const line = `🔧 ${name}${detail}`;
          if (activity.at(-1) !== line) activity.push(line);
          card.push(render());
        } else if (emitted?.type === "item.completed" && emitted.payload?.kind === "agent_message") {
          if (typeof emitted.payload.snapshot === "string") latestText = emitted.payload.snapshot;
          card.push(render());
        }
      } catch { /* progress rendering never fails a turn */ }
    };

    this.logger("info", "feishu_turn_started", { messageId, threadId, chars: event.content.length });
    let result;
    try {
      result = await this.adapter.dispatch({ threadId, text: event.content, cwd: this.cwd, messageId, onEvent });
    } catch (error) {
      if (card) await card.close();
      await this.notifier.sendMessage(`本轮失败：${shortError(error)}`, {
        chatId: event.chat_id, title: `${this.bridgeName}-回复`, threadId,
      }).catch(() => {});
      throw error;
    }

    if (card) {
      card.setThreadId(result.threadId ?? threadId);
      await card.finish();
    }
    const text = String(result.resultText ?? "").trim() || "（本轮无文本输出）";
    // Reply first (the trainer's expectation), then persist — but both before
    // the next queued turn so history order matches what was seen.
    const reply = await this.notifier.sendMessage(text, {
      chatId: event.chat_id, title: `${this.bridgeName}-回复`, threadId: result.threadId ?? threadId,
    }).catch(() => null);
    const finalThreadId = result.threadId ?? threadId;
    try {
      const stored = String(result.resultText ?? "").trim();
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
