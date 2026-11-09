// feishu-sdk-ws.mjs — Feishu long connection through the official
// `@larksuiteoapi/node-sdk`. Same public surface as FeishuWebsocketClient
// (feishu.mjs): constructor(notifier, { onMessage, onCardAction, onConnected }),
// start()/stop(), status(), and the same callback semantics. feishu-cli.mjs
// picks one implementation with FEISHU_WS_IMPL=sdk|native (default sdk), so the
// self-written transport stays in the tree for rollback.
//
// Why the SDK: the hand-written client decoded frames but never merged the
// fragments the server splits an event into (headers message_id/sum/seq). It
// dispatched and ACKed the first fragment, so `im.message.receive_v1` arrived
// once and then the connection looked "connected" while only CONTROL pongs
// came back. WSClient does that merge (10s cache, no dispatch/ACK until the
// event is complete) and owns reconnect/heartbeat, which is the same path
// Hermes' own Feishu adapter has been running on.
import { randomUUID } from "node:crypto";
import * as larkSdk from "@larksuiteoapi/node-sdk";

// Event types this bridge consumes. Everything else is registered nowhere and
// therefore ignored by EventDispatcher (it answers "no <type> event handle").
const MESSAGE_EVENT = "im.message.receive_v1";
const CARD_ACTION_EVENT = "card.action.trigger";
const CARD_UNSUPPORTED = { toast: { type: "error", content: "当前桥不支持此提问卡片。" } };

// The old diagnostic switch stays meaningful: it only adds stderr lines.
const debugFrames = () => process.env.CODEXWORKER_DEBUG_FRAMES === "1";

function iso(clock) {
  return new Date(clock()).toISOString();
}

// Error text only. The signed endpoint URL carries a short-lived ticket in its
// query string, so anything URL-shaped is dropped before it reaches status/logs.
function safeError(error) {
  const text = String(error?.message ?? error ?? "").split("?")[0];
  return text.slice(0, 300) || "sdk_error";
}

// ---------------------------------------------------------------------------
// Normalisation: SDK handler payload -> the shape accept() already reads.
// ---------------------------------------------------------------------------

// EventDispatcher merges `{schema, header, event}` into one flat object
// (`{...header, ...event}`). Accept either that or a raw envelope so this
// function is also usable on a payload captured straight from a frame.
function unwrapEvent(data) {
  if (!data || typeof data !== "object") return null;
  if (data.event && typeof data.event === "object") return data.event;
  return data;
}

export function normalizeMessageEvent(data) {
  try {
    const event = unwrapEvent(data);
    if (!event || typeof event !== "object") return null;
    const message = event.message;
    const sender = event.sender;
    if (!message || typeof message !== "object" || !sender || typeof sender !== "object") return null;
    const senderId = sender.sender_id && typeof sender.sender_id === "object" ? sender.sender_id : {};
    const rawContent = String(message.content || "");
    const messageType = String(message.message_type || "text");
    let content = rawContent;
    if (messageType === "text" && rawContent) {
      try { content = String(JSON.parse(rawContent).text || ""); } catch { content = rawContent; }
    }
    return {
      open_id: String(senderId.open_id || ""),
      chat_id: String(message.chat_id || ""),
      content,
      message_id: String(message.message_id || ""),
      chat_type: String(message.chat_type || "p2p"),
      sender_type: String(sender.sender_type || ""),
      message_type: messageType,
      create_time: String(message.create_time ?? ""),
    };
  } catch {
    return null;
  }
}

// The existing onCardAction consumers (question-cards.mjs parseCardAction) read
// the raw envelope: header.event_type/event_id + event.operator/action/context.
// Rebuild that envelope from the SDK's flat handler payload.
export function normalizeCardAction(data) {
  if (!data || typeof data !== "object") return null;
  if (data.event && typeof data.event === "object" && data.header && typeof data.header === "object") return data;
  return {
    schema: String(data.schema || "2.0"),
    header: {
      event_type: String(data.event_type || CARD_ACTION_EVENT),
      event_id: String(data.event_id || ""),
      create_time: String(data.create_time ?? ""),
      token: String(data.token || ""),
      app_id: String(data.app_id || ""),
      tenant_key: String(data.tenant_key || ""),
    },
    event: {
      operator: data.operator ?? {},
      action: data.action ?? {},
      context: data.context ?? {},
      token: data.token,
      host: data.host,
    },
  };
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

export class FeishuSdkWebsocketClient {
  // `sdk` is the dependency-injection seam used by the unit tests (a fake
  // { WSClient, EventDispatcher, LoggerLevel }); production always imports the
  // real module above.
  constructor(notifier, {
    onMessage, onCardAction, onConnected,
    sdk = larkSdk, loggerLevel = null, logger = null, clock = Date.now,
    connectionIdFactory = () => randomUUID(),
    handshakeTimeoutMs = null, wsConfig = null, domain = null,
  } = {}) {
    if (!sdk?.WSClient || !sdk?.EventDispatcher) {
      throw new TypeError("FeishuSdkWebsocketClient requires @larksuiteoapi/node-sdk (WSClient + EventDispatcher)");
    }
    this.notifier = notifier;
    this.onMessage = onMessage;
    this.onCardAction = onCardAction;
    this.onConnected = onConnected;
    this.sdk = sdk;
    this.loggerLevel = loggerLevel ?? sdk.LoggerLevel?.warn ?? 2;
    this.logger = logger;
    this.clock = clock;
    this.connectionIdFactory = connectionIdFactory;
    this.handshakeTimeoutMs = handshakeTimeoutMs;
    this.wsConfig = wsConfig;
    this.domain = domain;
    this.impl = "sdk";
    this._running = false;
    this._stopped = true;
    this._client = null;
    this._dispatcher = null;
    this.connectionId = null;
    this.phase = "stopped";
    this.attempts = 0;
    this.lastError = "";
    this.lastDisconnectAt = null;
    this.lastFrameAt = null;
    this.lastConnectedAt = null;
  }

  get running() {
    return this._running;
  }

  // FeishuWebsocketClient exposes `_ws.readyState`; this client has no raw
  // socket, so both classes also answer the same public `connected` question.
  get connected() {
    if (this._stopped || !this._running) return false;
    const state = this.#connectionStatus().state;
    return state ? state === "connected" : this.phase === "connected";
  }

  status() {
    const connection = this.#connectionStatus();
    const phase = this.#syncPhase(connection);
    const connected = phase === "connected";
    const lastSeen = this.lastFrameAt;
    return {
      policy: "official-sdk-ws",
      impl: this.impl,
      running: this.running,
      phase,
      connected,
      attempts: this.attempts,
      // Kept for field parity with FeishuWebsocketClient.status(); these knobs
      // belong to the SDK now and are not ours to tune.
      handshakeTimeoutMs: this.handshakeTimeoutMs,
      pongTimeoutMs: null,
      pingIntervalMs: null,
      retryIntervalMs: null,
      lastError: this.lastError || null,
      lastDisconnectAt: this.lastDisconnectAt,
      lastFrameAt: this.lastFrameAt,
      lastPingAt: null,
      lastConnectedAt: this.lastConnectedAt,
      // The SDK hides ping/pong frames, so liveness is answered from dispatch
      // activity: connected with no event yet is "awaiting_heartbeat".
      liveness: !connected ? "disconnected" : !lastSeen ? "awaiting_heartbeat" : "responsive",
      connectionId: this.connectionId,
      watchdog: { enabled: false, intervalMs: null, checks: 0, repairs: 0,
        lastCheckedAt: null, lastRepairAt: null, lastRepairReason: null },
      sdk: {
        state: connection.state ?? null,
        lastConnectTime: connection.lastConnectTime ?? null,
        nextConnectTime: connection.nextConnectTime ?? null,
        reconnectAttempts: connection.reconnectAttempts ?? 0,
      },
    };
  }

  setPhase(phase) {
    this.phase = phase;
    this.phaseSince = this.clock();
  }

  start() {
    if (!this.notifier?.enabled) return;
    if (this._running) return;
    const { WSClient, EventDispatcher } = this.sdk;
    this._stopped = false;
    this._running = true;
    this.attempts += 1;
    this.lastError = "";
    this.setPhase("connecting");

    const dispatcher = new EventDispatcher({
      loggerLevel: this.loggerLevel,
      ...(this.logger ? { logger: this.logger } : {}),
    });
    dispatcher.register({
      [MESSAGE_EVENT]: data => this.#handleMessage(data),
      [CARD_ACTION_EVENT]: data => this.#handleCardAction(data),
    });

    const client = new WSClient({
      appId: this.notifier.appId,
      appSecret: this.notifier.appSecret,
      loggerLevel: this.loggerLevel,
      ...(this.logger ? { logger: this.logger } : {}),
      ...(this.domain !== null ? { domain: this.domain } : {}),
      ...(this.wsConfig ? { wsConfig: this.wsConfig } : {}),
      ...(this.handshakeTimeoutMs ? { handshakeTimeoutMs: this.handshakeTimeoutMs } : {}),
      onReady: () => this.#handleConnected(),
      onReconnected: () => this.#handleConnected(),
      onReconnecting: () => this.#handleReconnecting(),
      onError: error => this.#handleError(error),
    });
    this._client = client;
    this._dispatcher = dispatcher;

    console.error("feishu 长连接正在连接");
    Promise.resolve()
      .then(() => client.start({ eventDispatcher: dispatcher }))
      .catch(error => this.#handleError(error));
  }

  stop() {
    this._stopped = true;
    const client = this._client;
    this._client = null;
    this._dispatcher = null;
    try { client?.close?.({}); } catch { /* Already failed. */ }
    this.connectionId = null;
    this.setPhase("stopped");
    this._running = false;
  }

  #connectionStatus() {
    try {
      return this._client?.getConnectionStatus?.() ?? {};
    } catch {
      return {};
    }
  }

  #syncPhase(connection) {
    if (this._stopped) {
      this.phase = "stopped";
      return this.phase;
    }
    // A terminal SDK error clears _running but must keep the "failed" label.
    if (!this._running) return this.phase;
    const state = connection?.state;
    const next = state === "connected" ? "connected"
      : state === "reconnecting" ? "reconnecting"
      : state === "failed" ? "failed"
      : state === "connecting" ? "connecting"
      : this.phase === "connected" ? "reconnecting" : this.phase;
    if (next !== this.phase) this.phase = next;
    return this.phase;
  }

  async #handleMessage(data) {
    if (this._stopped) return undefined;
    this.lastFrameAt = iso(this.clock);
    if (debugFrames()) {
      const event = unwrapEvent(data) || {};
      console.error(`[feishu-diag] sdk event=im.message.receive_v1 message_id=${event.message?.message_id ?? ""}`);
    }
    const parsed = normalizeMessageEvent(data);
    if (!parsed || !this.onMessage) return undefined;
    // Awaiting this is the ACK: the SDK only answers Feishu after the handler
    // resolves, so a failed durable write stays unacknowledged and is retried.
    await this.onMessage(parsed);
    return undefined;
  }

  async #handleCardAction(data) {
    if (this._stopped) return undefined;
    this.lastFrameAt = iso(this.clock);
    const envelope = normalizeCardAction(data);
    if (!envelope) return undefined;
    // The returned value is what the SDK base64-encodes into the callback ACK,
    // exactly like FeishuWebsocketClient._sendAck did with onCardAction's reply.
    if (!this.onCardAction) return CARD_UNSUPPORTED;
    return await this.onCardAction(envelope);
  }

  #handleConnected() {
    if (this._stopped) return;
    this.connectionId = this.connectionIdFactory();
    this.lastConnectedAt = iso(this.clock);
    this.lastFrameAt = null;
    this.lastError = "";
    this.setPhase("connected");
    console.error("feishu 长连接已建立");
    const connectionId = this.connectionId;
    Promise.resolve()
      .then(() => !this._stopped && this.onConnected?.({ connectionId }))
      .catch(() => console.error("feishu 上线通知回调失败；请检查桥连接状态。"));
  }

  #handleReconnecting() {
    if (this._stopped) return;
    this.attempts += 1;
    this.lastDisconnectAt = iso(this.clock);
    this.setPhase("reconnecting");
  }

  #handleError(error) {
    if (this._stopped) return;
    this.lastError = safeError(error);
    this.lastDisconnectAt = iso(this.clock);
    this.setPhase("failed");
    this._running = false;
  }
}
