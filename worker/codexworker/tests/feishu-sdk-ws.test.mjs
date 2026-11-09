// Feishu long connection through the official @larksuiteoapi/node-sdk.
// No network: the SDK module is injected as a fake (WSClient + EventDispatcher
// stand-ins), so these tests prove the adapter's contract, not Feishu's uptime.
//
// The bug this transport replaces: the hand-written client never merged the
// server's event fragments, so it dispatched/ACKed the first fragment and
// `im.message.receive_v1` arrived exactly once. Here the SDK owns fragment
// merging, ACK and reconnect; we only normalise its payloads.
import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {
  CliFeishuChannel, resolveFeishuWsImpl,
} from "../feishu-cli.mjs";
import {
  FeishuSdkWebsocketClient, normalizeCardAction, normalizeMessageEvent,
} from "../feishu-sdk-ws.mjs";
import { parseCardAction } from "../question-cards.mjs";
import { SessionStore } from "../session-store.mjs";

const encoder = new TextEncoder();
const flush = () => new Promise(resolve => setImmediate(resolve));

async function waitFor(predicate, label = "condition", timeoutMs = 4000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail(`timed out waiting for ${label}`);
}

// Minimal stand-in for @larksuiteoapi/node-sdk. It records every client and
// dispatcher and exposes the transitions the real WSClient would emit.
function fakeSdk() {
  const state = { clients: [], dispatchers: [] };
  class FakeEventDispatcher {
    constructor(options = {}) { this.options = options; this.handlers = new Map(); state.dispatchers.push(this); }
    register(handles) {
      for (const [key, handler] of Object.entries(handles)) this.handlers.set(key, handler);
      return this;
    }
    async invoke(type, data) { const handler = this.handlers.get(type); return handler ? await handler(data) : undefined; }
  }
  class FakeWSClient {
    constructor(params) {
      this.params = params; this.dispatcher = null; this.closed = 0;
      this.state = { state: "idle", reconnectAttempts: 0 };
      state.clients.push(this);
    }
    start({ eventDispatcher } = {}) {
      this.dispatcher = eventDispatcher;
      this.state = { state: "connecting", reconnectAttempts: 0 };
      return Promise.resolve();
    }
    close() { this.closed += 1; this.state = { state: "idle", reconnectAttempts: 0 }; }
    getConnectionStatus() { return { ...this.state }; }
    ready() { this.state = { state: "connected", lastConnectTime: 1000, reconnectAttempts: 0 }; this.params.onReady?.(); }
    reconnect() { this.state = { state: "connected", lastConnectTime: 2000, reconnectAttempts: 0 }; this.params.onReconnected?.(); }
    drop() { this.state = { state: "reconnecting", reconnectAttempts: 1 }; this.params.onReconnecting?.(); }
    fail(message) { this.state = { state: "failed", reconnectAttempts: 0 }; this.params.onError?.(new Error(message)); }
    emit(type, data) { return this.dispatcher?.invoke(type, data); }
  }
  return {
    module: { WSClient: FakeWSClient, EventDispatcher: FakeEventDispatcher,
      LoggerLevel: { fatal: 0, error: 1, warn: 2, info: 3, debug: 4, trace: 5 } },
    state,
  };
}

function messageData({ openId = "ou_trainer", messageId = "om_1", text = "你好", chatType = "p2p", type = "text",
  content = null, senderType = "user" } = {}) {
  return {
    schema: "2.0",
    event_id: "evt_1",
    event_type: "im.message.receive_v1",
    create_time: "1762500000000",
    sender: { sender_id: { open_id: openId }, sender_type: senderType },
    message: {
      message_id: messageId, message_type: type, chat_id: "oc_chat", chat_type: chatType,
      content: content ?? JSON.stringify({ text }), create_time: "1762500000000",
    },
  };
}

function cardData() {
  return {
    schema: "2.0",
    event_id: "evt_card",
    event_type: "card.action.trigger",
    create_time: "1762500000001",
    token: "callback-token",
    app_id: "cli_0123456789abcdef",
    tenant_key: "tenant",
    operator: { open_id: "ou_trainer" },
    action: { tag: "button", value: { bridgeQuestion: "q1", nonce: "n1", action: "select", questionIndex: 0, optionIndex: 1 } },
    context: { open_chat_id: "oc_chat", open_message_id: "om_card" },
  };
}

test("normalizeMessageEvent matches the fields accept() reads (SDK merged and raw envelopes)", () => {
  const expected = { open_id: "ou_trainer", chat_id: "oc_chat", content: "你好", message_id: "om_1",
    chat_type: "p2p", sender_type: "user", message_type: "text", create_time: "1762500000000" };
  const merged = normalizeMessageEvent(messageData());
  assert.deepEqual(merged, expected);
  // Same payload before EventDispatcher flattens it (`{event: {...}}`).
  assert.deepEqual(normalizeMessageEvent({ event: { sender: messageData().sender, message: messageData().message } }), expected);

  // Group chat, bot sender and non-text payloads keep their raw content; the
  // channel (#admit) is the component that decides to reject them.
  const image = normalizeMessageEvent(messageData({ chatType: "group", senderType: "app", type: "image",
    content: JSON.stringify({ image_key: "img_1" }) }));
  assert.equal(image.chat_type, "group");
  assert.equal(image.sender_type, "app");
  assert.equal(image.message_type, "image");
  assert.equal(image.content, JSON.stringify({ image_key: "img_1" }));

  // Text content that is not valid JSON stays verbatim instead of throwing.
  assert.equal(normalizeMessageEvent(messageData({ content: "plain text" })).content, "plain text");
  assert.equal(normalizeMessageEvent(null), null);
  assert.equal(normalizeMessageEvent({ message: {} }), null);
  // Missing ids are not "malformed": accept() rejects them as missing_ids.
  const incomplete = normalizeMessageEvent({ sender: {}, message: { content: "x" } });
  assert.deepEqual([incomplete.open_id, incomplete.message_id], ["", ""]);
});

test("normalizeCardAction produces the envelope parseCardAction already consumes", () => {
  const envelope = normalizeCardAction(cardData());
  assert.equal(envelope.header.event_type, "card.action.trigger");
  assert.equal(envelope.header.event_id, "evt_card");
  const parsed = parseCardAction(envelope);
  assert.equal(parsed.eventId, "evt_card");
  assert.equal(parsed.openId, "ou_trainer");
  assert.equal(parsed.chatId, "oc_chat");
  assert.equal(parsed.messageId, "om_card");
  assert.deepEqual(parsed.value, { bridgeQuestion: "q1", nonce: "n1", action: "select", questionIndex: 0, optionIndex: 1 });
  // A raw envelope passes through untouched.
  const raw = { schema: "2.0", header: { event_type: "card.action.trigger", event_id: "x" }, event: { action: { value: {} } } };
  assert.equal(normalizeCardAction(raw), raw);
});

test("start registers both event types and onMessage receives the normalised event", async () => {
  const fake = fakeSdk();
  const seen = [];
  const client = new FeishuSdkWebsocketClient({ enabled: true, appId: "cli_0123456789abcdef", appSecret: "secret" }, {
    sdk: fake.module, onMessage: event => { seen.push(event); },
  });
  client.start();
  await flush();

  assert.equal(fake.state.clients.length, 1);
  assert.equal(fake.state.clients[0].params.appId, "cli_0123456789abcdef");
  assert.equal(fake.state.clients[0].dispatcher, fake.state.dispatchers[0]);
  assert.deepEqual([...fake.state.dispatchers[0].handlers.keys()].sort(), ["card.action.trigger", "im.message.receive_v1"]);

  fake.state.clients[0].ready();
  const ack = await fake.state.clients[0].emit("im.message.receive_v1", messageData());
  assert.equal(ack, undefined); // plain message: no callback body
  assert.equal(seen.length, 1);
  assert.deepEqual(seen[0], { open_id: "ou_trainer", chat_id: "oc_chat", content: "你好", message_id: "om_1",
    chat_type: "p2p", sender_type: "user", message_type: "text", create_time: "1762500000000" });

  // A failing durable handler rejects: the SDK then ACKs with 500 instead of
  // 200, so Feishu redelivers instead of losing the message.
  client.onMessage = async () => { throw new Error("ingress_full"); };
  await assert.rejects(() => fake.state.clients[0].emit("im.message.receive_v1", messageData({ messageId: "om_2" })), /ingress_full/);
});

test("card.action.trigger goes to onCardAction and its return value is the ACK body", async () => {
  const fake = fakeSdk();
  const payloads = [];
  const client = new FeishuSdkWebsocketClient({ enabled: true, appId: "cli_0123456789abcdef", appSecret: "secret" }, {
    sdk: fake.module,
    onCardAction: payload => { payloads.push(payload); return { toast: { type: "success", content: "答案已保存" } }; },
  });
  client.start();
  await flush();
  fake.state.clients[0].ready();

  const ack = await fake.state.clients[0].emit("card.action.trigger", cardData());
  assert.deepEqual(ack, { toast: { type: "success", content: "答案已保存" } });
  assert.equal(payloads.length, 1);
  assert.equal(parseCardAction(payloads[0]).eventId, "evt_card");

  // No callback configured: same toast the native client returned.
  const bare = new FeishuSdkWebsocketClient({ enabled: true, appId: "cli_0123456789abcdef", appSecret: "secret" }, { sdk: fake.module });
  bare.start();
  await flush();
  fake.state.clients[1].ready();
  assert.deepEqual(await fake.state.clients[1].emit("card.action.trigger", cardData()), { toast: { type: "error", content: "当前桥不支持此提问卡片。" } });
});

test("status() reports connected/phase/attempts/timestamps/error with the native field set", async () => {
  const fake = fakeSdk();
  const connections = [];
  const client = new FeishuSdkWebsocketClient({ enabled: true, appId: "cli_0123456789abcdef", appSecret: "secret" }, {
    sdk: fake.module, onMessage: () => {}, onConnected: ({ connectionId }) => connections.push(connectionId),
  });

  // Before start: a stopped, disconnected snapshot (not undefined fields).
  const stopped = client.status();
  for (const key of ["policy", "running", "phase", "connected", "attempts", "lastError", "lastDisconnectAt",
    "lastFrameAt", "lastConnectedAt", "liveness", "watchdog"]) {
    assert.ok(key in stopped, `status().${key} missing`);
  }
  assert.equal(stopped.phase, "stopped");
  assert.equal(stopped.connected, false);
  assert.equal(stopped.running, false);
  assert.equal(stopped.lastError, null);

  client.start();
  await flush();
  const socket = fake.state.clients[0];
  assert.equal(client.status().phase, "connecting");
  assert.equal(client.status().attempts, 1);

  socket.ready();
  await flush();
  const live = client.status();
  assert.equal(live.impl, "sdk");
  assert.equal(live.connected, true);
  assert.equal(live.phase, "connected");
  assert.equal(live.running, true);
  assert.equal(live.liveness, "awaiting_heartbeat");
  assert.ok(Date.parse(live.lastConnectedAt) > 0);
  assert.equal(live.lastFrameAt, null);
  assert.equal(live.lastError, null);
  assert.equal(live.sdk.state, "connected");
  assert.deepEqual(live.watchdog.enabled, false);
  assert.equal(connections.length, 1);

  await socket.emit("im.message.receive_v1", messageData());
  assert.equal(client.status().liveness, "responsive");
  assert.ok(Date.parse(client.status().lastFrameAt) > 0);

  // Reconnect: a new connection id, attempts grows, connected again.
  socket.drop();
  assert.equal(client.status().phase, "reconnecting");
  assert.equal(client.status().attempts, 2);
  assert.ok(Date.parse(client.status().lastDisconnectAt) > 0);
  socket.reconnect();
  await flush();
  assert.equal(client.status().phase, "connected");
  assert.equal(connections.length, 2);
  assert.notEqual(connections[0], connections[1]);

  // Terminal SDK error: failed state, message preserved, running stops.
  socket.fail("endpoint returned 500?ticket=secret");
  const failed = client.status();
  assert.equal(failed.phase, "failed");
  assert.equal(failed.connected, false);
  assert.equal(failed.running, false);
  assert.equal(failed.lastError, "endpoint returned 500"); // query string (ticket) is dropped
});

test("stop() closes the SDK client once and reports stopped", async () => {
  const fake = fakeSdk();
  const client = new FeishuSdkWebsocketClient({ enabled: true, appId: "cli_0123456789abcdef", appSecret: "secret" }, { sdk: fake.module });
  client.start();
  await flush();
  fake.state.clients[0].ready();
  client.stop();
  const status = client.status();
  assert.equal(status.phase, "stopped");
  assert.equal(status.running, false);
  assert.equal(status.connected, false);
  assert.equal(fake.state.clients[0].closed, 1);
  client.stop();
  assert.equal(fake.state.clients[0].closed, 1);
});

test("a disabled notifier never constructs an SDK client", async () => {
  const fake = fakeSdk();
  const client = new FeishuSdkWebsocketClient({ enabled: false }, { sdk: fake.module });
  client.start();
  await flush();
  assert.equal(fake.state.clients.length, 0);
  assert.equal(client.status().phase, "stopped");
});

test("FEISHU_WS_IMPL resolves sdk by default and native on request", () => {
  assert.equal(resolveFeishuWsImpl(undefined), "sdk");
  assert.equal(resolveFeishuWsImpl(""), "sdk");
  assert.equal(resolveFeishuWsImpl("sdk"), "sdk");
  assert.equal(resolveFeishuWsImpl(" NATIVE "), "native");
  assert.equal(resolveFeishuWsImpl("nonsense"), "sdk");
});

// ---------------------------------------------------------------------------
// Channel integration: the trainer's message must reach accept() through the
// SDK transport, and the env var is the rollback switch.
// ---------------------------------------------------------------------------

function fakeNotifier() {
  const sent = [];
  return {
    appId: "cli_0123456789abcdef", appSecret: "test-secret", enabled: true, sent,
    async getToken() { return "test-token"; },
    async addReaction(messageId, emojiType = "Typing") { return `react_${messageId}_${emojiType}`; },
    async removeReaction() { return true; },
    async sendMessage(text, options = {}) {
      sent.push({ text, options });
      return { code: 0, data: { message_id: `om_reply_${sent.length}` } };
    },
  };
}

function fakeAdapter() {
  const calls = [];
  return {
    calls,
    async register() { return { id: "cli-00000000-0000-4000-8000-000000000001", cwd: null, title: null,
      createdAt: new Date().toISOString(), turns: 0 }; },
    async dispatch(options) {
      calls.push(options);
      return { threadId: options.threadId, sessionId: options.threadId, turnId: "turn-1", status: "completed",
        resultText: "OK", toolCalls: [], durationMs: 5 };
    },
  };
}

async function channelFixture(t, { impl = null, env = undefined } = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-feishu-sdk-"));
  const fake = fakeSdk();
  const adapter = fakeAdapter();
  const notifier = fakeNotifier();
  const sessionStore = await new SessionStore(path.join(root, "history"), { logger: () => {} }).init();
  const previous = process.env.FEISHU_WS_IMPL;
  if (env === undefined) delete process.env.FEISHU_WS_IMPL; else process.env.FEISHU_WS_IMPL = env;
  t.after(() => {
    if (previous === undefined) delete process.env.FEISHU_WS_IMPL; else process.env.FEISHU_WS_IMPL = previous;
  });
  const channel = new CliFeishuChannel({
    adapter, notifier, sessionStore,
    feishu: { inboundEnabled: true }, bridgeName: "CodexWorker",
    sessionsPath: path.join(root, "feishu-sessions.json"),
    logger: () => {},
    ...(impl ? { feishuImpl: impl } : {}),
    sdk: fake.module,
  });
  t.after(async () => { await channel.close(); });
  await channel.start();
  await flush();
  return { channel, adapter, notifier, fake, root };
}

test("default transport is the SDK and receive_v1 reaches accept() normalised", async t => {
  const f = await channelFixture(t);
  assert.equal(f.channel.status().impl, "sdk");
  assert.equal(f.channel.status().connection.policy, "official-sdk-ws");
  assert.equal(f.channel.status().connection.impl, "sdk");
  assert.equal(f.fake.state.clients.length, 1);

  f.fake.state.clients[0].ready();
  await flush();
  assert.equal(f.channel.status().connected, true);
  assert.equal(f.channel.status().phase, "connected");

  await f.fake.state.clients[0].emit("im.message.receive_v1", messageData({ text: "你好" }));
  await waitFor(() => f.notifier.sent.length === 1, "the one reply");
  assert.equal(f.adapter.calls.length, 1);
  assert.equal(f.adapter.calls[0].text, "你好");
  assert.equal(f.notifier.sent[0].text, "OK");
  // turns++ lands after the reply plus the history write, so wait for it.
  await waitFor(() => f.channel.status().turns === 1, "turn accounting");
  assert.equal(f.channel.status().replies, 1);
});

test("FEISHU_WS_IMPL=native still selects the self-written transport", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-feishu-native-"));
  const adapter = fakeAdapter();
  const notifier = fakeNotifier();
  const sessionStore = await new SessionStore(path.join(root, "history"), { logger: () => {} }).init();
  const { FeishuWebsocketClient } = await import("../feishu.mjs");
  const { encodeFrame } = await import("../feishu.mjs");
  const socket = {
    readyState: 0, sent: [], send(bytes) { this.sent.push(bytes); },
    close() { this.readyState = 3; this.onclose?.(); },
    frame(payload) { this.onmessage?.({ data: encodeFrame({ method: 1, service: 0,
      headers: [["type", "event"]], payload: encoder.encode(JSON.stringify(payload)) }) }); },
  };
  const previous = process.env.FEISHU_WS_IMPL;
  process.env.FEISHU_WS_IMPL = "native";
  t.after(() => { if (previous === undefined) delete process.env.FEISHU_WS_IMPL; else process.env.FEISHU_WS_IMPL = previous; });
  const channel = new CliFeishuChannel({
    adapter, notifier, sessionStore, feishu: { inboundEnabled: true }, bridgeName: "CodexWorker",
    sessionsPath: path.join(root, "feishu-sessions.json"), logger: () => {},
    websocketFactory: () => socket, endpointResolver: async () => "wss://isolated.invalid/ws?service_id=1",
  });
  t.after(async () => { await channel.close(); });
  await channel.start();
  await flush();
  assert.equal(channel.status().impl, "native");
  assert.equal(channel.client instanceof FeishuWebsocketClient, true);
  assert.equal(channel.status().connection.policy, "bounded-handshake-heartbeat");

  socket.readyState = 1; socket.onopen?.();
  socket.frame({ event: { sender: { sender_id: { open_id: "ou_trainer" }, sender_type: "user" },
    message: { message_id: "om_native", message_type: "text", chat_id: "oc_chat", chat_type: "p2p",
      content: JSON.stringify({ text: "回退" }) } } });
  await waitFor(() => notifier.sent.length === 1, "native reply");
  assert.equal(adapter.calls[0].text, "回退");
});
