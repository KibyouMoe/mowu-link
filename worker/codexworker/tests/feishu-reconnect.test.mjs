// No external network, machine state, credentials, native tasks or production
// process control. Advance only this client's timers; never global timers.
import assert from "node:assert/strict";
import test from "node:test";
import http from "node:http";
import { createHash } from "node:crypto";
import { FeishuWebsocketClient } from "../feishu.mjs";

const flush = () => new Promise(resolve => setImmediate(resolve));
class Timers {
  constructor() { this.now = 0; this.jobs = new Map(); this.next = 0; }
  setTimeout = (fn, delay) => { const id = ++this.next; this.jobs.set(id, { fn, at: this.now + delay }); return id; };
  clearTimeout = id => { this.jobs.delete(id); };
  async advance(ms) {
    const target = this.now + ms;
    for (let n = 0; n < 1000; n++) {
      const next = [...this.jobs].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > target) { this.now = target; await flush(); return; }
      this.now = next[1].at; this.jobs.delete(next[0]); next[1].fn(); await flush();
    }
    assert.fail("Unexpected timer loop");
  }
}

const encoder = new TextEncoder();
function varint(n) { const bytes = []; while (n >= 128) { bytes.push((n & 127) | 128); n >>>= 7; } return [...bytes, n]; }
function field(tag, bytes) { return [tag, ...varint(bytes.length), ...bytes]; }
function frame(type, payload = {}) {
  const header = [...field(0x0a, encoder.encode("type")), ...field(0x12, encoder.encode(type))];
  return new Uint8Array([0x20, type === "event" ? 1 : 0, ...field(0x2a, header), ...field(0x42, encoder.encode(JSON.stringify(payload)))]);
}
function fixture(t) {
  const timers = new Timers(), sockets = [], messages = [], connections = [];
  const client = new FeishuWebsocketClient({ enabled: true }, { timers, clock: () => timers.now,
    onMessage: async event => { messages.push(event.content); },
    onConnected: event => { connections.push(event.connectionId); },
    websocketFactory: () => {
      const ws = { readyState: 0, sent: [], closes: 0, failSend: false, respond: false,
        send(bytes) {
          if (this.failSend) throw new Error("isolated-network-down");
          this.sent.push(bytes);
          if (this.respond && bytes[7] === 0) this.onmessage?.({ data: frame("pong") });
        },
        // Intentionally emits no close: silent loss must not strand the loop.
        close() { this.closes++; this.readyState = 3; },
        open() { this.readyState = 1; this.onopen?.(); },
        pong(config = {}) { this.onmessage?.({ data: frame("pong", config) }); },
        message(text) { this.onmessage?.({ data: frame("event", { event: { sender: { sender_id: { open_id: "test-trainer" } },
          message: { message_id: "test-input", message_type: "text", chat_id: "test-chat", content: JSON.stringify({ text }) } } }) }); }
      };
      sockets.push(ws); return ws;
    } });
  client._getConnUrl = async () => "wss://isolated.invalid/ws?service_id=1";
  t.after(() => client.stop());
  return { client, timers, sockets, messages, connections };
}

test("three-minute watchdog probes responsive idle links without reconnecting or sending chat traffic", async t => {
  const f = fixture(t); f.client.configurePing({ PingInterval: 3600 }); f.client.start(); await flush();
  f.sockets[0].respond = true; f.sockets[0].open();
  await f.timers.advance(540000);
  const status = f.client.status();
  assert.equal(status.watchdog.checks, 3); assert.equal(status.watchdog.repairs, 0);
  assert.equal(status.lastFrameAt, new Date(540000).toISOString());
  assert.equal(status.liveness, "responsive"); assert.equal(f.sockets.length, 1);
  assert.deepEqual(f.messages, []); assert.equal(f.connections.length, 1);
});

test("watchdog recovers a failed reconnect loop once and leaves intentionally stopped clients stopped", async t => {
  const f = fixture(t), original = f.client._runLoop.bind(f.client);
  f.client._runLoop = async () => { throw new Error("isolated-loop-failure"); };
  f.client.start(); await flush(); assert.equal(f.client.running, false);
  f.client._runLoop = original; await f.timers.advance(180000);
  assert.equal(f.client.watchdog.repairs, 1); assert.equal(f.sockets.length, 1);
  f.sockets[0].respond = true; f.sockets[0].open(); await f.timers.advance(180000);
  assert.equal(f.client.watchdog.repairs, 1);
  f.client.stop(); await f.timers.advance(540000);
  assert.equal(f.sockets.length, 1); assert.equal(f.client.status().watchdog.enabled, false);
});

test("watchdog retires a stuck endpoint generation and ignores its eventual late response", async t => {
  const f = fixture(t); let release;
  f.client._getConnUrl = () => new Promise(resolve => { release = resolve; });
  f.client.start(); await flush();
  f.client._getConnUrl = async () => "wss://isolated.invalid/?service_id=1";
  await f.timers.advance(180000); assert.equal(f.sockets.length, 1);
  f.sockets[0].open(); f.sockets[0].pong(); await flush();
  release("wss://retired.invalid/?service_id=1"); await flush();
  assert.equal(f.sockets.length, 1); assert.equal(f.client.watchdog.lastRepairReason, "reconnect_loop_stalled");
});

test("watchdog ping detects a silent link within a bounded deadline even with an hour-long server interval", async t => {
  const f = fixture(t); f.client.configurePing({ PingInterval: 3600 }); f.client.start(); await flush();
  f.sockets[0].open(); assert.equal(f.client.status().liveness, "awaiting_heartbeat");
  f.sockets[0].pong(); await f.timers.advance(195000);
  assert.equal(f.sockets[0].closes, 1); assert.equal(f.client.lastError, "heartbeat_timeout");
  await f.timers.advance(5000); assert.equal(f.sockets.length, 2);
  f.sockets[1].open(); assert.equal(f.client.status().lastFrameAt, null);
});

test("failed heartbeat releases the attempt and automatically accepts input on a new connection", async t => {
  const f = fixture(t); f.client.start(); await flush();
  f.sockets[0].open(); f.sockets[0].pong(); await flush();
  f.sockets[0].failSend = true;
  await f.timers.advance(120000);
  assert.equal(f.client.status().lastError, "heartbeat_send_failed");
  assert.equal(f.client.running, true); assert.equal(f.client._ws, null);
  assert.equal(f.sockets[0].closes, 1);
  await f.timers.advance(5000); assert.equal(f.sockets.length, 2);
  f.sockets[1].open(); f.sockets[1].pong(); f.sockets[1].message("after-network-recovery"); await flush();
  assert.deepEqual(f.messages, ["after-network-recovery"]);
  assert.equal(f.connections.length, 2); assert.notEqual(f.connections[0], f.connections[1]);
  assert.equal(f.client.status().phase, "connected"); assert.equal(f.client.status().lastError, null);
});

test("an OPEN socket with silent blackholed sends is retired on heartbeat response timeout", async t => {
  const f = fixture(t); f.client.start(); await flush(); f.sockets[0].open(); f.sockets[0].pong();
  await f.timers.advance(120000); assert.equal(f.client._ws?.readyState, 1);
  await f.timers.advance(15000); assert.equal(f.client._ws, null);
  assert.equal(f.client.lastError, "heartbeat_timeout");
  await f.timers.advance(5000); assert.equal(f.sockets.length, 2);
  f.sockets[1].open(); f.sockets[1].pong(); f.sockets[1].message("restored"); await flush();
  assert.deepEqual(f.messages, ["restored"]);
});

test("a handshake emitting neither open nor error has a finite deadline and automatic retry", async t => {
  const f = fixture(t); f.client.start(); await flush();
  await f.timers.advance(15000);
  assert.equal(f.client.lastError, "handshake_timeout"); assert.equal(f.sockets[0].closes, 1);
  await f.timers.advance(5000); assert.equal(f.sockets.length, 2);
  f.sockets[1].open(); f.sockets[1].pong(); await flush();
  assert.equal(f.client.status().phase, "connected");
});

test("idle but responsive sockets do not require chat traffic to remain connected", async t => {
  const f = fixture(t); f.client.start(); await flush();
  f.sockets[0].respond = true; f.sockets[0].open();
  await f.timers.advance(400000);
  assert.equal(f.sockets.length, 1); assert.equal(f.sockets[0].closes, 0);
  assert.equal(f.client.status().phase, "connected"); assert.deepEqual(f.messages, []);
});

test("late callbacks from a retired socket cannot clear or notify a newer connection", async t => {
  const f = fixture(t); f.client.start(); await flush();
  const old = f.sockets[0]; old.open(); old.pong(); await flush();
  const late = { open: old.onopen, close: old.onclose, error: old.onerror, message: old.onmessage };
  old.onerror(); await flush(); await f.timers.advance(5000);
  f.sockets[1].open(); f.sockets[1].pong(); await flush(); const id = f.client.connectionId;
  late.open(); late.close(); late.error(); late.message({ data: frame("pong") }); await flush();
  assert.equal(f.client._ws, f.sockets[1]); assert.equal(f.client.connectionId, id);
  assert.equal(f.connections.length, 2); assert.equal(f.sockets[1].closes, 0);
});

test("stop ends a pending handshake or retry without waiting for a close event", async t => {
  const f = fixture(t); f.client.start(); await flush(); f.client.stop(); await flush();
  assert.equal(f.client._ws, null); assert.equal(f.timers.jobs.size, 0);
  await f.timers.advance(30000); assert.equal(f.sockets.length, 1);
  f.client.start(); await flush(); f.sockets[1].onerror(); await flush();
  assert.equal(f.client.status().phase, "reconnecting"); f.client.stop(); await flush();
  assert.equal(f.timers.jobs.size, 0); assert.equal(f.client.running, false);
  await f.timers.advance(30000); assert.equal(f.sockets.length, 2);
});

test("late endpoint discovery from a stopped generation cannot resurrect it or steal a restarted socket", async t => {
  const f = fixture(t); let release;
  f.client._getConnUrl = () => new Promise(resolve => { release = resolve; });
  f.client.start(); f.client.stop();
  f.client._getConnUrl = async () => "wss://isolated.invalid/?service_id=1";
  f.client.start(); await flush(); f.sockets[0].open(); f.sockets[0].pong(); await flush();
  release("wss://old.invalid/?service_id=1"); await flush();
  assert.equal(f.sockets.length, 1); assert.equal(f.client._ws, f.sockets[0]); assert.equal(f.client.running, true);
});

test("server ping interval is honored while one outstanding liveness deadline remains bounded", async t => {
  const f = fixture(t); f.client.configurePing({ PingInterval: 1 }); f.client.start(); await flush();
  f.sockets[0].open(); await f.timers.advance(14000);
  // Multiple outstanding pings must not leak uncancelable response timers.
  f.sockets[0].pong({ PingInterval: 2 }); await f.timers.advance(1000);
  assert.equal(f.sockets[0].closes, 0); assert.equal(f.client.pingIntervalMs, 2000);
  await f.timers.advance(15000);
  assert.equal(f.client.lastError, "heartbeat_timeout"); assert.equal(f.sockets[0].closes, 1);
});

test("real localhost WebSocket recovers from a TCP blackhole without restarting the client or relying on close", { timeout: 15000 }, async t => {
  // Only this test's loopback HTTP server and TCP sockets are manipulated.
  // The first socket acknowledges the initial ping, then silently drops frames.
  const sockets = new Set(); let attempts = 0, input, resolveInput;
  const delivered = new Promise(resolve => { resolveInput = resolve; });
  const server = http.createServer();
  const client = new FeishuWebsocketClient({ enabled: true }, {
    handshakeTimeoutMs: 1000, pongTimeoutMs: 300,
    onMessage: async event => { input = event.content; resolveInput(); }
  });
  t.after(async () => {
    client.stop(); for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  });
  server.on("upgrade", (request, socket) => {
    const attempt = ++attempts; sockets.add(socket); socket.on("error", () => {});
    socket.once("close", () => sockets.delete(socket));
    const key = createHash("sha1").update(request.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
    socket.write("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + key + "\r\n\r\n");
    const send = bytes => {
      const header = bytes.length < 126 ? Buffer.from([0x82, bytes.length]) : Buffer.from([0x82, 126, bytes.length >>> 8, bytes.length & 255]);
      socket.write(Buffer.concat([header, bytes]));
    };
    let initialized = false;
    socket.on("data", () => {
      if (initialized) return; initialized = true;
      send(frame("pong"));
      if (attempt > 1) send(frame("event", { event: { sender: { sender_id: { open_id: "isolated-trainer" } },
        message: { chat_id: "isolated-chat", message_id: "isolated-message", message_type: "text", content: JSON.stringify({ text: "real-blackhole-recovered" }) } } }));
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  client._getConnUrl = async () => `ws://127.0.0.1:${server.address().port}/?service_id=1`;
  client.configurePing({ PingInterval: 1 }); client.start();
  await delivered;
  assert.equal(input, "real-blackhole-recovered"); assert.equal(attempts, 2);
  assert.equal(client.running, true); assert.equal(client._ws?.readyState, 1);
});
