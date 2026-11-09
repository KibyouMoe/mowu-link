// Feishu (Lark) outbound push — ported from the Claude bridge's FeishuNotifier /
// FeishuStreamBeat. Same discipline: failures only log, never affect the turn.
// Uses the global fetch (Node 18+), so the plugin carries no extra dependency.
import { downloadFeishuImage } from "./feishu-images.mjs";
import { createHash, randomUUID } from "node:crypto";
import { hermesChunks } from "./hermes-feishu-format.mjs";

export const FEISHU_API_BASE = "https://open.feishu.cn/open-apis";
const TOKEN_URL = "/auth/v3/tenant_access_token/internal";
const SEND_URL = "/im/v1/messages";
const TOKEN_TTL = 7200; // tenant_access_token lives 2h
const POST_MAX_CHARS = 20000;

async function postJson(url, body, { token = "", timeoutMs = 15000, method = "POST" } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = { "Content-Type": "application/json; charset=utf-8" };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const resp = await fetch(url, { method, headers, body: JSON.stringify(body), signal: controller.signal });
    return await resp.json();
  } catch (err) {
    return { code: -1, msg: `network error: ${err && err.message ? err.message : String(err)}` };
  } finally {
    clearTimeout(timer);
  }
}

async function deleteJson(url, { token = "", timeoutMs = 15000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = { "Content-Type": "application/json; charset=utf-8" };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const resp = await fetch(url, { method: "DELETE", headers, signal: controller.signal });
    return await resp.json();
  } catch (err) {
    return { code: -1, msg: `network error: ${err && err.message ? err.message : String(err)}` };
  } finally {
    clearTimeout(timer);
  }
}

// Escape backslashes OUTSIDE code spans to &#92; so Windows paths survive the
// interactive-card markdown renderer (which treats \ as an escape).
export function escapeMarkdown(text) {
  const out = [];
  let i = 0;
  const n = text.length;
  while (i < n) {
    const ch = text[i];
    if (ch === "`") {
      let j = i;
      while (j < n && text[j] === "`") j += 1;
      const run = text.slice(i, j);
      const close = text.indexOf(run, j);
      if (close !== -1) {
        out.push(text.slice(i, close + run.length));
        i = close + run.length;
      } else {
        out.push(run);
        i = j;
      }
      continue;
    }
    out.push(ch === "\\" ? "&#92;" : ch);
    i += 1;
  }
  return out.join("");
}

export const CARD_TEMPLATE = "indigo";

// Hermes `_card()` field structure: wide card, colored header (title object
// first), then one markdown element. The session-id note is this bridge's only
// addition; fenced code blocks ride the markdown element untouched.
export function cardPayload(text, { title, threadId = null, template = CARD_TEMPLATE } = {}) {
  const elements = [{ tag: "markdown", content: escapeMarkdown(String(text ?? "")) }];
  if (threadId) {
    elements.push({ tag: "note", elements: [{ tag: "plain_text", content: `会话 ${threadId}` }] });
  }
  return JSON.stringify({
    config: { wide_screen_mode: true },
    header: { title: { content: String(title ?? ""), tag: "plain_text" }, template },
    elements,
  });
}

export function markdownPayload(text, title, threadId) {
  return cardPayload(text, { title, threadId });
}

export function splitChunks(text, maxChars) {
  const cap = maxChars > 0 ? maxChars : POST_MAX_CHARS;
  if (text.length <= cap) return [text];
  const chunks = [];
  let remaining = text;
  while (remaining.length > cap) {
    const cut = remaining.lastIndexOf("\n", cap);
    if (cut > 0) {
      chunks.push(remaining.slice(0, cut + 1));
      remaining = remaining.slice(cut + 1);
    } else {
      chunks.push(remaining.slice(0, cap));
      remaining = remaining.slice(cap);
    }
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}

export function isTokenError(response) {
  const code = response && response.code;
  if (code === 99991663 || code === 99991664 || code === 401) return true;
  return String((response && response.msg) || "").toLowerCase().includes("token");
}

export class FeishuNotifier {
  constructor({ appId, appSecret, chatId, bridgeName, canSend = () => true }) {
    this.appId = appId;
    this.appSecret = appSecret;
    this.chatId = chatId;
    this.bridgeName = bridgeName;
    this.canSend = canSend;
    this._token = "";
    this._tokenExpiresAt = 0;
  }

  get enabled() {
    return Boolean(this.appId && this.appSecret);
  }

  invalidateToken() { this._token = ""; this._tokenExpiresAt = 0; }

  async getToken() {
    if (!this.enabled) return "";
    const now = Date.now() / 1000;
    if (this._token && now < this._tokenExpiresAt) return this._token;
    try {
      const response = await postJson(FEISHU_API_BASE + TOKEN_URL, { app_id: this.appId, app_secret: this.appSecret });
      const token = String(response.tenant_access_token || "");
      if (!token) {
        console.warn(`feishu token 获取失败: code=${response.code} msg=${response.msg}`);
        return "";
      }
      this._token = token;
      this._tokenExpiresAt = now + (TOKEN_TTL - 20); // 20s early-expiry margin
      return token;
    } catch (err) {
      console.warn(`feishu token 获取异常: ${err && err.message ? err.message : String(err)}`);
      return "";
    }
  }

  async sendMessage(text, { chatId, title, threadId, uuid, validateTarget, onDelivery } = {}) {
    const record = async value => { if (onDelivery) await onDelivery({ ...value, at: new Date().toISOString() }); };
    const allowed = () => this.canSend({ threadId }) && (validateTarget == null || typeof validateTarget === "function" && validateTarget());
    if (!this.enabled || !allowed()) { await record({ phase: "not_attempted", reason: "disabled_or_binding_changed" }); return null; }
    const target = chatId || this.chatId;
    if (!target) { await record({ phase: "not_attempted", reason: "target_missing" }); return null; }
    const token = await this.getToken();
    if (!token) { await record({ phase: "not_attempted", reason: "token_unavailable" }); return null; }
    const t = title || this.bridgeName || "Codex";
    const chunks = hermesChunks(text);
    let lastResponse = null;
    for (const [index, chunk] of chunks.entries()) {
      // Selection may change while fetching a token or sending an earlier
      // chunk. Never start another old-chat HTTP write after that change.
      if (!allowed()) { await record({ phase: "not_attempted", reason: "binding_changed", chunk: index }); return null; }
      const chunkUuid = uuid && (chunks.length === 1 ? uuid : createHash("sha256").update(`${uuid}:${index}`).digest("hex").slice(0, 32));
      const detail = { chunk: index, chunks: chunks.length, uuid: chunkUuid ?? null,
        payloadSha256: createHash("sha256").update(markdownPayload(chunk, t, threadId)).digest("hex") };
      const send = async credential => {
        await record({ ...detail, phase: "dispatching" }); // Durable before the possible write.
        const value = await this._postChunk(target, chunk, t, credential, threadId, chunkUuid);
        await record({ ...detail, phase: value?.code === 0 ? "accepted" : Number.isInteger(value?.code) && value.code > 0 ? "rejected" : "unknown",
          apiCode: Number.isInteger(value?.code) ? value.code : null,
          ...(value?.code === 0 && typeof value.data?.message_id === "string" ? { messageId: value.data.message_id } : {}) });
        return value;
      };
      let response = await send(token);
      if (response === null) return null;
      if (response.code !== 0 && isTokenError(response)) {
        console.warn(`feishu token 失效（code=${response.code} msg=${response.msg}），刷新 token 重试一次`);
        this._token = "";
        this._tokenExpiresAt = 0;
        const newToken = await this.getToken();
        response = newToken && allowed() ? await send(newToken) : null;
        if (response === null) return null;
      }
      if (response.code !== 0) {
        console.warn(`feishu 发送消息未确认: code=${Number.isInteger(response.code) ? response.code : "unknown"}`);
        return null;
      }
      lastResponse = response;
    }
    return lastResponse;
  }

  async downloadImage(messageId, imageKey) {
    return downloadFeishuImage(messageId, imageKey, await this.getToken());
  }

  async sendQuestionCard(card, { chatId, threadId, uuid } = {}) {
    if (!this.enabled || !this.canSend({ threadId })) return null;
    const target = chatId || this.chatId;
    if (!target || !uuid) return null;
    return this._questionRequest(`${FEISHU_API_BASE}${SEND_URL}?receive_id_type=chat_id`, {
      receive_id: target, msg_type: "interactive", content: JSON.stringify(card), uuid
    }, { threadId });
  }

  async updateQuestionCard(messageId, card, { threadId } = {}) {
    // Exact, previously sent question cards may be marked expired after a switch.
    // No new old-chat message is created. The manager authorizes this exact ID.
    if (!this.enabled || !messageId) return null;
    return this._questionRequest(`${FEISHU_API_BASE}/im/v1/messages/${encodeURIComponent(messageId)}`,
      { content: JSON.stringify(card) }, { method: "PATCH", threadId, metadataUpdate: true });
  }

  async _questionRequest(url, body, { method = "POST", threadId, metadataUpdate = false }) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const token = await this.getToken();
      if (!token || (!metadataUpdate && !this.canSend({ threadId }))) return null;
      const result = await postJson(url, body, { token, method });
      if (result?.code === 0) return result;
      if (attempt === 0 && isTokenError(result)) { this.invalidateToken(); continue; }
      return null; // Raw upstream errors/token/response bodies are not logged.
    }
  }

  async _postChunk(target, chunk, title, token, threadId, uuid) {
    try {
      return await postJson(
        `${FEISHU_API_BASE}${SEND_URL}?receive_id_type=chat_id`,
        { receive_id: target, msg_type: "interactive", content: markdownPayload(chunk, title, threadId), ...(uuid ? { uuid } : {}) },
        { token }
      );
    } catch (err) {
      console.warn(`feishu 发送消息异常: ${err && err.message ? err.message : String(err)}`);
      return null;
    }
  }

  // Emoji types the bridge uses: Typing while a turn runs, CrossMark when it
  // failed (Hermes parity). Both are verified against the live reaction API.
  async addReaction(messageId, emojiType = "Typing") {
    if (!this.enabled || !messageId) return null;
    const emoji = typeof emojiType === "string" && emojiType.trim() ? emojiType.trim() : "Typing";
    const token = await this.getToken();
    if (!token) return null;
    const url = `${FEISHU_API_BASE}/im/v1/messages/${encodeURIComponent(messageId)}/reactions`;
    const body = { reaction_type: { emoji_type: emoji } };
    let response = await postJson(url, body, { token });
    if (response && response.code !== 0 && isTokenError(response)) {
      console.warn(`feishu reaction token 失效（code=${response.code} msg=${response.msg}），刷新 token 重试一次`);
      this._token = "";
      this._tokenExpiresAt = 0;
      const newToken = await this.getToken();
      response = newToken ? await postJson(url, body, { token: newToken }) : null;
    }
    if (response === null || response.code !== 0) {
      console.warn(`feishu 添加 reaction 失败: emoji=${emoji} code=${(response || {}).code} msg=${(response || {}).msg} message_id=${messageId}`);
      return null;
    }
    return String((response.data || {}).reaction_id || "") || null;
  }

  async removeReaction(messageId, reactionId) {
    if (!this.enabled || !messageId || !reactionId) return false;
    const token = await this.getToken();
    if (!token) return false;
    const url = `${FEISHU_API_BASE}/im/v1/messages/${encodeURIComponent(messageId)}/reactions/${encodeURIComponent(reactionId)}`;
    let response = await deleteJson(url, { token });
    if (response && response.code !== 0 && isTokenError(response)) {
      console.warn(`feishu 删除 reaction token 失效（code=${response.code} msg=${response.msg}），刷新 token 重试一次`);
      this._token = "";
      this._tokenExpiresAt = 0;
      const newToken = await this.getToken();
      response = newToken ? await deleteJson(url, { token: newToken }) : null;
    }
    if (response === null || response.code !== 0) {
      console.warn(`feishu 删除 reaction 失败: code=${(response || {}).code} msg=${(response || {}).msg} reaction_id=${reactionId}`);
      return false;
    }
    return true;
  }

  async warmup() {
    if (!this.enabled) return;
    if (await this.getToken()) console.error("feishu token 预热成功");
    else console.warn("feishu token 预热失败：首次推送时会重试");
  }
}

// Turn progress beats: reasoning -> "💭 思考" summary, tool-call -> "🔧 工具调用".
// DSH content blocks differ from Claude stream-json: reasoning (not thinking),
// tool-call (not tool_use). The final text is left to the reply push, so text
// blocks are not re-sent here.
function thinkingSummary(thinking) {
  const lines = thinking.split("\n");
  if (lines.length > 10) return `${lines.slice(0, 10).join("\n")}\n…`;
  return thinking;
}

export class FeishuStreamBeat {
  constructor(notifier, { threadId, chatId, bridgeName }) {
    this.notifier = notifier;
    this.threadId = threadId;
    this.chatId = chatId;
    this.bridgeName = bridgeName;
    this._reasoningBuf = "";
    this._textBuf = "";
  }

  _push(message, title, onDelivered) {
    this.notifier.sendMessage(message, { chatId: this.chatId, title, threadId: this.threadId }).then(response => {
      // Only a successful Feishu delivery dismisses the waiting indicator.
      if (response?.code === 0) return onDelivered?.();
    }).catch((err) => {
      console.warn(`feishu 节拍发送异常: ${err && err.message ? err.message : String(err)}`);
    });
  }

  _flushReasoning(onDelivered) {
    const thinking = this._reasoningBuf.trim();
    if (thinking !== "") {
      console.error(`feishu 节拍: 推送思考摘要 (${thinking.length} 字符)`);
      this._push(`💭 思考\n\`\`\`\n${thinkingSummary(thinking)}\n\`\`\``, `${this.bridgeName}-思考`, onDelivered);
      this._reasoningBuf = "";
    }
  }

  _flushText(onDelivered) {
    const text = this._textBuf.trim();
    if (text !== "") {
      console.error(`feishu 节拍: 推送明文输出 (${text.length} 字符)`);
      this._push(text, `${this.bridgeName}-输出`, onDelivered);
      this._textBuf = "";
    }
  }

  // Real-time stream chunks: accumulate reasoning deltas and plain text
  // deltas; flush a reasoning summary and the accumulated plain text on each
  // beat (the caller polls ~1s). Tool calls flush reasoning but nothing else.
  feedChunk(chunk) {
    if (!chunk || typeof chunk !== "object") return;
    if (chunk.type === "reasoning-delta") {
      this._reasoningBuf += chunk.text || "";
    } else if (chunk.type === "text-delta") {
      this._flushReasoning();
      this._textBuf += chunk.text || "";
    } else if (chunk.type === "tool-call-delta") {
      this._flushReasoning();
    }
  }

  // Assembled assistant messages: intentionally NOT surfaced on Feishu — only
  // 💭 思考 (via feedChunk), plain 💬 输出 (via feedChunk text-delta), and the
  // final reply text are pushed. Tool calls are deliberately skipped so Feishu
  // stays clean.
  feedBlocks(_blocks) {
    // no-op: tools are not pushed to Feishu
  }

  // Called once per polling beat (approx 1s) to push accumulated plain text.
  flush(onDelivered) {
    this._flushReasoning(onDelivered);
    this._flushText(onDelivered);
  }
}

// ---- Feishu inbound: hand-rolled pbbp2.Frame protobuf + websocket client ----

const FEISHU_WS_BASE = "https://open.feishu.cn";
const FEISHU_WS_ENDPOINT_URI = "/callback/ws/endpoint";
const FEISHU_WS_PING_INTERVAL = 120000;
const FEISHU_WS_RECONNECT_INTERVAL = 5000;
const FEISHU_WS_HANDSHAKE_TIMEOUT = 15000;
const FEISHU_WS_PONG_TIMEOUT = 15000;

const _encoder = new TextEncoder();
const _decoder = new TextDecoder();

function encodeVarint(value) {
  const bytes = [];
  let v = BigInt(value);
  while (v >= 0x80n) {
    bytes.push(Number(v & 0x7fn) | 0x80);
    v >>= 7n;
  }
  bytes.push(Number(v));
  return bytes;
}

function decodeVarint(data, pos) {
  let result = 0n;
  let shift = 0n;
  while (true) {
    if (pos >= data.length) throw new Error("varint truncated");
    const byte = data[pos];
    pos += 1;
    result |= BigInt(byte & 0x7f) << shift;
    if (!(byte & 0x80)) return { value: result <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(result) : result, pos };
    shift += 7n;
    if (shift > 63n) throw new Error("varint too long");
  }
}

function encodeTag(fieldNumber, wireType) {
  return encodeVarint((fieldNumber << 3) | wireType);
}

function encodeLenDelimited(bytes) {
  return [...encodeVarint(bytes.length), ...bytes];
}

export function encodeFrame({ method, service, SeqID = 0, LogID = 0, headers = [], payload = new Uint8Array(0), payload_encoding, payload_type, LogIDNew }) {
  const parts = [];
  parts.push(0x08, ...encodeVarint(SeqID));         // SeqID = 1 (u64)
  parts.push(0x10, ...encodeVarint(LogID));         // LogID = 2 (u64)
  parts.push(0x18, ...encodeVarint(service >>> 0)); // service = 3 (int32)
  parts.push(0x20, ...encodeVarint(method >>> 0));  // method = 4 (int32)
  for (const [key, value] of headers) {
    const entry = [
      ...encodeTag(1, 2), ...encodeLenDelimited(_encoder.encode(key)),
      ...encodeTag(2, 2), ...encodeLenDelimited(_encoder.encode(value)),
    ];
    parts.push(...encodeTag(5, 2), ...encodeLenDelimited(new Uint8Array(entry)));
  }
  if (payload_encoding) parts.push(...encodeTag(6, 2), ...encodeLenDelimited(_encoder.encode(payload_encoding)));
  if (payload_type) parts.push(...encodeTag(7, 2), ...encodeLenDelimited(_encoder.encode(payload_type)));
  if (payload.length) parts.push(...encodeTag(8, 2), ...encodeLenDelimited(payload));
  if (LogIDNew) parts.push(...encodeTag(9, 2), ...encodeLenDelimited(_encoder.encode(LogIDNew)));
  return new Uint8Array(parts);
}

function parseHeaderEntry(bytes) {
  let key = "";
  let value = "";
  let pos = 0;
  while (pos < bytes.length) {
    const { value: tag, pos: p1 } = decodeVarint(bytes, pos);
    pos = p1;
    const fieldNumber = tag >>> 3;
    const wireType = tag & 0x7;
    if (wireType !== 2) throw new Error(`unexpected wire type ${wireType} in header entry`);
    const { value: length, pos: p2 } = decodeVarint(bytes, pos);
    pos = p2;
    const raw = bytes.slice(pos, pos + length);
    pos += length;
    if (fieldNumber === 1) key = _decoder.decode(raw);
    else if (fieldNumber === 2) value = _decoder.decode(raw);
  }
  return [key, value];
}

export function decodeFrame(data) {
  const frame = { method: 0, service: 0, headers: {}, payload: new Uint8Array(0) };
  let pos = 0;
  while (pos < data.length) {
    const { value: tag, pos: p1 } = decodeVarint(data, pos);
    pos = p1;
    const fieldNumber = tag >>> 3;
    const wireType = tag & 0x7;
    if (fieldNumber === 1) { const r = decodeVarint(data, pos); frame.SeqID = r.value; pos = r.pos; }
    else if (fieldNumber === 2) { const r = decodeVarint(data, pos); frame.LogID = r.value; pos = r.pos; }
    else if (fieldNumber === 3) { const r = decodeVarint(data, pos); frame.service = r.value; pos = r.pos; }
    else if (fieldNumber === 4) { const r = decodeVarint(data, pos); frame.method = r.value; pos = r.pos; }
    else if (fieldNumber === 5) {
      const r = decodeVarint(data, pos);
      const entry = data.slice(r.pos, r.pos + r.value);
      pos = r.pos + r.value;
      const [k, v] = parseHeaderEntry(entry);
      frame.headers[k] = v;
    }
    else if (fieldNumber === 6) { const r = decodeVarint(data, pos); frame.payload_encoding = _decoder.decode(data.slice(r.pos, r.pos + r.value)); pos = r.pos + r.value; }
    else if (fieldNumber === 7) { const r = decodeVarint(data, pos); frame.payload_type = _decoder.decode(data.slice(r.pos, r.pos + r.value)); pos = r.pos + r.value; }
    else if (fieldNumber === 8) { const r = decodeVarint(data, pos); frame.payload = data.slice(r.pos, r.pos + r.value); pos = r.pos + r.value; }
    else if (fieldNumber === 9) { const r = decodeVarint(data, pos); frame.LogIDNew = _decoder.decode(data.slice(r.pos, r.pos + r.value)); pos = r.pos + r.value; }
    else {
      if (wireType === 0) { const r = decodeVarint(data, pos); pos = r.pos; }
      else if (wireType === 1) pos += 8;
      else if (wireType === 2) { const r = decodeVarint(data, pos); pos = r.pos + r.value; }
      else if (wireType === 5) pos += 4;
      else throw new Error(`unsupported wire type ${wireType}`);
    }
  }
  return frame;
}

function parseMessageEvent(payload) {
  try {
    const data = JSON.parse(_decoder.decode(payload));
    const event = data && typeof data === "object" ? data.event : undefined;
    if (!event || typeof event !== "object") return null;
    const message = event.message;
    const sender = event.sender;
    if (!message || typeof message !== "object" || !sender || typeof sender !== "object") return null;
    const senderId = sender.sender_id && typeof sender.sender_id === "object" ? sender.sender_id : {};
    const openId = String(senderId.open_id || "");
    const chatId = String(message.chat_id || "");
    const rawContent = String(message.content || "");
    const messageType = String(message.message_type || "text");
    let content = rawContent;
    if (messageType === "text" && rawContent) {
      try { content = String(JSON.parse(rawContent).text || ""); } catch (err) { content = rawContent; }
    }
    return {
      open_id: openId,
      chat_id: chatId,
      content,
      message_id: String(message.message_id || ""),
      chat_type: String(message.chat_type || "p2p"),
      sender_type: String(sender.sender_type || ""),
      message_type: messageType,
    };
  } catch (err) {
    return null;
  }
}

export class FeishuWebsocketClient {
  constructor(notifier, { onMessage, onCardAction, onConnected, websocketFactory = url => new WebSocket(url),
    handshakeTimeoutMs = FEISHU_WS_HANDSHAKE_TIMEOUT, pongTimeoutMs = FEISHU_WS_PONG_TIMEOUT,
    timers = { setTimeout, clearTimeout }, clock = Date.now, watchdogIntervalMs = 180000 }) {
    this.notifier = notifier;
    this.onMessage = onMessage;
    this.onCardAction = onCardAction;
    this.onConnected = onConnected; this.websocketFactory = websocketFactory; this.connectionId = null;
    this._running = false;
    this._stopped = false;
    this._ws = null;
    this.lastError = "";
    this.handshakeTimeoutMs = handshakeTimeoutMs; this.pongTimeoutMs = pongTimeoutMs; this.timers = timers;
    this.pingIntervalMs = FEISHU_WS_PING_INTERVAL;
    this._generation = 0; this._attempt = null; this._retry = null;
    this.phase = "stopped"; this.attempts = 0; this.lastDisconnectAt = null;
    this.clock = clock; this.watchdogIntervalMs = watchdogIntervalMs;
    this.watchdog = { intervalMs: watchdogIntervalMs, checks: 0, repairs: 0,
      lastCheckedAt: null, lastRepairAt: null, lastRepairReason: null };
    this.lastFrameAt = null; this.lastPingAt = null; this.lastConnectedAt = null;
  }

  get running() {
    return this._running;
  }

  status() {
    return { policy: "bounded-handshake-heartbeat", running: this.running, phase: this.phase,
      attempts: this.attempts, handshakeTimeoutMs: this.handshakeTimeoutMs, pongTimeoutMs: this.pongTimeoutMs,
      pingIntervalMs: this.pingIntervalMs, retryIntervalMs: FEISHU_WS_RECONNECT_INTERVAL,
      lastError: this.lastError || null, lastDisconnectAt: this.lastDisconnectAt,
      lastFrameAt: this.lastFrameAt, lastPingAt: this.lastPingAt, lastConnectedAt: this.lastConnectedAt,
      liveness: this.phase !== "connected" ? "disconnected" : !this.lastFrameAt ? "awaiting_heartbeat"
        : this.clock() - Date.parse(this.lastFrameAt) > Math.max(this.pingIntervalMs, this.watchdogIntervalMs) + this.pongTimeoutMs ? "stale" : "responsive",
      watchdog: { ...this.watchdog, enabled: !this._stopped && Boolean(this._watchdogTimer) } };
  }

  setPhase(phase) { this.phase = phase; this.phaseSince = this.clock(); }

  scheduleWatchdog() {
    this.timers.clearTimeout(this._watchdogTimer);
    if (this._stopped) return;
    this._watchdogTimer = this.timers.setTimeout(() => {
      this._watchdogTimer = null;
      this.checkWatchdog();
      this.scheduleWatchdog();
    }, this.watchdogIntervalMs);
    this._watchdogTimer?.unref?.();
  }

  checkWatchdog() {
    if (this._stopped || !this.notifier.enabled) return;
    this.watchdog.checks++; this.watchdog.lastCheckedAt = new Date(this.clock()).toISOString();
    let reason;
    if (!this.running) reason = "reconnect_loop_stopped";
    else if (this.phase === "connected") {
      // Probe the real application heartbeat, not just WebSocket.OPEN. This
      // shares the original finite response deadline and sends no chat message.
      if (this._ws?.readyState === 1 && this._attempt?.ping) { this._attempt.ping(); return; }
      reason = "connected_socket_missing";
    } else if (this.clock() - this.phaseSince > Math.max(45000, this.handshakeTimeoutMs + this.pongTimeoutMs + 5000)) {
      reason = "reconnect_loop_stalled";
    }
    if (!reason) return; // Normal bounded connection/retry work is already active.
    this.watchdog.repairs++; this.watchdog.lastRepairAt = this.watchdog.lastCheckedAt;
    this.watchdog.lastRepairReason = reason;
    this.stop(); this.start(); // Retire only this transport generation; preserve durable inputs.
  }

  current(generation) { return !this._stopped && generation === this._generation; }

  start() {
    if (!this.notifier.enabled) return;
    if (this._running) return;
    this._running = true;
    this._stopped = false;
    this.scheduleWatchdog();
    const generation = ++this._generation;
    this._runLoop(generation).catch(() => {
      if (!this.current(generation)) return;
      this.lastError = "reconnect_loop_failed"; this.setPhase("failed");
      console.warn("[CodexWorker] Feishu reconnect loop failed; inspect connection status.");
      this._running = false;
    });
  }

  stop() {
    this._stopped = true;
    this.timers.clearTimeout(this._watchdogTimer); this._watchdogTimer = null;
    this._generation++;
    this._attempt?.finish("stopped");
    this._retry?.finish();
    this.connectionId = null;
    this.setPhase("stopped"); this._running = false;
  }

  async _runLoop(generation) {
    while (this.current(generation)) {
      try {
        await this._runOnce(generation);
      } catch {
        if (this.current(generation)) this.lastError = "connection_attempt_failed";
      }
      if (!this.current(generation)) break;
      this.setPhase("reconnecting");
      await new Promise(resolve => {
        const retry = { timer: null, finish: () => {
          this.timers.clearTimeout(retry.timer);
          if (this._retry === retry) this._retry = null;
          resolve();
        } };
        this._retry = retry;
        retry.timer = this.timers.setTimeout(retry.finish, FEISHU_WS_RECONNECT_INTERVAL);
      });
    }
    if (generation === this._generation) this._running = false;
  }

  async _runOnce(generation = this._generation) {
    this.setPhase("connecting"); this.attempts++;
    const connUrl = await this._getConnUrl();
    if (!connUrl || !this.current(generation)) return;
    const url = new URL(connUrl);
    const serviceId = parseInt(url.searchParams.get("service_id") || "0", 10) || 0;
    // Neither a signed endpoint URL nor a raw transport exception is diagnostic data.
    console.error("feishu 长连接正在连接");
    await new Promise((resolve) => {
      const ws = this.websocketFactory(connUrl);
      ws.binaryType = "arraybuffer";
      this._ws = ws;
      let settled = false, opened = false, pingTimer = null, pongTimer = null, handshakeTimer = null;
      const active = () => !settled && this.current(generation) && this._attempt === attempt && this._ws === ws;
      const finish = reason => {
        if (settled) return;
        settled = true;
        for (const timer of [pingTimer, pongTimer, handshakeTimer]) this.timers.clearTimeout(timer);
        if (this._attempt === attempt) {
          this._attempt = null; this._ws = null; this.connectionId = null;
          this.lastDisconnectAt = new Date().toISOString();
          if (reason !== "stopped") { this.lastError = reason; this.setPhase("reconnecting"); }
        }
        // A late close/open/error from a discarded connection must not clear a
        // newer one. Keep an error sink during the asynchronous close handshake.
        ws.onopen = null; ws.onmessage = null; ws.onclose = null; ws.onerror = () => {};
        if (reason !== "closed") { try { ws.close(); } catch { /* Already failed. */ } }
        resolve(); // Never depend on close/error being emitted after silent loss.
      };
      const attempt = { finish };
      this._attempt = attempt;
      handshakeTimer = this.timers.setTimeout(() => finish("handshake_timeout"), this.handshakeTimeoutMs);
      const ping = () => {
        if (!active()) return;
        this.timers.clearTimeout(pingTimer); pingTimer = null;
        if (ws.readyState !== 1) { finish("socket_not_open"); return; }
        // Arm before send: a synchronous response in tests must cancel it too.
        if (pongTimer === null) pongTimer = this.timers.setTimeout(() => finish("heartbeat_timeout"), this.pongTimeoutMs);
        this.lastPingAt = new Date(this.clock()).toISOString();
        try { ws.send(encodeFrame({ method: 0, service: serviceId, headers: [["type", "ping"]] })); }
        catch { finish("heartbeat_send_failed"); return; }
        if (active()) pingTimer = this.timers.setTimeout(ping, this.pingIntervalMs);
      };
      attempt.ping = ping;
      ws.onopen = () => {
        if (opened || !active()) return;
        opened = true; this.connectionId = randomUUID();
        this.lastFrameAt = null; this.lastConnectedAt = new Date(this.clock()).toISOString();
        this.timers.clearTimeout(handshakeTimer);
        this.lastError = ""; this.setPhase("connected");
        console.error("feishu 长连接已建立");
        ping();
        const connectionId = this.connectionId;
        Promise.resolve().then(() => active() && this.onConnected?.({ connectionId })).catch(() => {
          console.error("feishu 上线通知回调失败；请检查桥连接状态。");
        });
      };
      ws.onmessage = (event) => {
        if (!active()) return;
        try {
          const frame = decodeFrame(new Uint8Array(event.data));
          this.lastFrameAt = new Date(this.clock()).toISOString();
          // Like the official SDK, any decoded inbound frame proves liveness.
          // Do not re-arm on inbound: healthy idle sockets wait for the next ping.
          this.timers.clearTimeout(pongTimer); pongTimer = null;
          if (frame.method === 0 && frame.headers.type === "pong" && frame.payload.length) {
            try { this.configurePing(JSON.parse(_decoder.decode(frame.payload))); } catch { /* Invalid server config. */ }
          }
          void this._handleFrame(ws, frame, serviceId).catch(() => {
            console.error("[CodexWorker] Feishu event was not acknowledged; inspect durable ingress state.");
          });
        } catch {
          console.warn("[CodexWorker] Invalid Feishu frame; input was not acknowledged.");
        }
      };
      ws.onclose = () => finish("closed");
      ws.onerror = () => finish("socket_error");
    });
  }

  configurePing(config) {
    if (Number.isFinite(config?.PingInterval) && config.PingInterval > 0 && config.PingInterval <= 3600) {
      this.pingIntervalMs = Math.max(1000, config.PingInterval * 1000);
    }
  }

  async _getConnUrl() {
    const response = await postJson(FEISHU_WS_BASE + FEISHU_WS_ENDPOINT_URI, { AppID: this.notifier.appId, AppSecret: this.notifier.appSecret });
    if (response.code !== 0 || !response.data || typeof response.data !== "object") {
      this.lastError = "endpoint_unavailable";
      console.warn("[CodexWorker] Feishu endpoint unavailable; automatic retry remains active.");
      return "";
    }
    this.configurePing(response.data.ClientConfig);
    return String(response.data.URL || "");
  }

  async _handleFrame(ws, frame, serviceId) {
    if (this._stopped) return;
    const frameMethod = frame.method ?? 0;
    const msgType = frame.headers.type || "";
    // Frame diagnostics are opt-in: CODEXWORKER_DEBUG_FRAMES=1. A healthy
    // long connection logs nothing by default.
    if (process.env.CODEXWORKER_DEBUG_FRAMES === "1") {
      if (frameMethod !== 0) {
        let et = "";
        try { et = JSON.parse(_decoder.decode(frame.payload))?.header?.event_type || ""; } catch { et = "<unparsed>"; }
        console.error(`[feishu-diag] method=${frameMethod} type=${msgType} event_type=${et} bytes=${frame.payload?.length ?? 0}`);
      } else {
        console.error(`[feishu-diag] CONTROL type=${msgType} bytes=${frame.payload?.length ?? 0}`);
      }
    }
    if (frameMethod === 0) return; // CONTROL ping/pong, ignore
    if (msgType === "card" || (msgType === "event" && (() => {
      try { return JSON.parse(_decoder.decode(frame.payload))?.header?.event_type === "card.action.trigger"; } catch { return false; }
    })())) {
      // Only durable acceptance is awaited, never a native task write/PATCH.
      const response = this.onCardAction ? await this.onCardAction(frame.payload) : { toast: { type: "error", content: "当前桥不支持此提问卡片。" } };
      this._sendAck(ws, frame, serviceId, response); return;
    }
    if (msgType !== "event") return;
    const parsed = parseMessageEvent(frame.payload);
    if (parsed && this.onMessage) {
      // A hot handoff must not ACK an event before durable handling completes.
      // Explicitly rejected/ignored inputs resolve normally; uncertain handling
      // rejects and stays unacknowledged rather than pretending it was stored.
      await this.onMessage(parsed);
    }
    this._sendAck(ws, frame, serviceId);
  }

  _sendAck(ws, frame, serviceId, response) {
    const headers = Object.entries(frame.headers);
    headers.push(["biz_rt", "1"]);
    const ackPayload = _encoder.encode(JSON.stringify({ code: 200,
      data: response ? Buffer.from(JSON.stringify(response)).toString("base64") : "" }));
    try {
      ws.send(encodeFrame({ ...frame, method: 1, service: serviceId, headers, payload: ackPayload }));
    } catch (err) { /* ignore */ }
  }
}
