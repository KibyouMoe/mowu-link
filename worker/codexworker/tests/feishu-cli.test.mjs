// CLI-mode Feishu channel: no network, no credentials, no real Codex process.
// A fake websocket drives FeishuWebsocketClient; a fake adapter stands in for
// CliNativeAdapter; a temp SessionStore backs the bridge-owned history.
import assert from "node:assert/strict";
import test from "node:test";
import { mkdir, mkdtemp, readdir, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { CliFeishuChannel, OutputCard, feishuMessageId } from "../feishu-cli.mjs";
import { SessionStore } from "../session-store.mjs";
import { encodeFrame } from "../feishu.mjs";

const SESSION = "11111111-2222-4333-8444-555555555555";
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

class ManualTimers {
  constructor() { this.now = 0; this.jobs = new Map(); this.next = 0; }
  setTimeout = (fn, delay) => { const id = ++this.next; this.jobs.set(id, { fn, at: this.now + delay }); return id; };
  clearTimeout = id => { this.jobs.delete(id); };
  async advance(ms) {
    this.now += ms;
    for (const [id, job] of [...this.jobs]) {
      if (job.at > this.now) continue;
      this.jobs.delete(id);
      job.fn();
    }
    await new Promise(resolve => setTimeout(resolve, 5));
  }
}

function fakeNotifier() {
  const sent = [];
  return {
    appId: "test-app", appSecret: "test-secret", enabled: true, sent,
    async getToken() { return "test-token"; },
    async sendMessage(text, options = {}) {
      sent.push({ text, options });
      return { code: 0, data: { message_id: `om_reply_${sent.length}` } };
    },
  };
}

function fakeTransport() {
  const sends = [], updates = [];
  return {
    sends, updates,
    async send(text, meta) { sends.push({ text, meta }); return { code: 0, messageId: `om_card_${sends.length}` }; },
    async update(messageId, text, meta) { updates.push({ messageId, text, meta }); return { code: 0 }; },
  };
}

// register() gives every conversation the same stable cli-<uuid> shape the
// HTTP /threads route uses; dispatch() echoes it back like CliNativeAdapter.
function fakeAdapter({ fail = null, threadId = SESSION } = {}) {
  const calls = [];
  let registered = 0;
  return {
    calls,
    async register({ cwd = null, title = null } = {}) {
      registered += 1;
      return { id: `cli-00000000-0000-4000-8000-${String(registered).padStart(12, "0")}`,
        cwd, title, createdAt: new Date().toISOString(), turns: 0 };
    },
    async dispatch(options) {
      calls.push(options);
      if (fail) throw fail;
      const id = options.threadId;
      options.onEvent?.({ type: "turn.started", threadId: null, payload: { resumed: Boolean(options.threadId) } });
      options.onEvent?.({ type: "tool.completed", threadId: id, payload: { name: "shell", command: "printf ok" } });
      options.onEvent?.({ type: "item.completed", threadId: id, payload: { kind: "agent_message", snapshot: "中间输出" } });
      return { threadId: id, sessionId: threadId, turnId: "turn-1", status: "completed",
        resultText: options.text === "再来" ? "上一轮是 OK" : "OK", toolCalls: [], durationMs: 12 };
    },
  };
}

class FakeSocket {
  constructor() { this.readyState = 0; this.sent = []; this.closes = 0; }
  send(bytes) { this.sent.push(bytes); }
  close() { this.closes += 1; this.readyState = 3; this.onclose?.(); }
  open() { this.readyState = 1; this.onopen?.(); }
  frame(payload) {
    this.onmessage?.({ data: encodeFrame({ method: 1, service: 0,
      headers: [["type", "event"]], payload: encoder.encode(JSON.stringify(payload)) }) });
  }
}

function textEvent(openId, messageId, text, overrides = {}) {
  return { event: {
    sender: { sender_id: { open_id: openId }, sender_type: "user" },
    message: { message_id: messageId, message_type: "text", chat_id: "oc_chat",
      chat_type: "p2p", content: JSON.stringify({ text }), ...overrides },
  } };
}

async function fixture(t, overrides = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-feishu-cli-"));
  const notifier = overrides.notifier ?? fakeNotifier();
  const transport = overrides.transport ?? fakeTransport();
  const adapter = overrides.adapter ?? fakeAdapter();
  const socket = new FakeSocket();
  const logs = [];
  const sessionStore = overrides.sessionStore ??
    await new SessionStore(path.join(root, "history"), { logger: () => {} }).init();
  const channel = new CliFeishuChannel({
    adapter, notifier, transport, sessionStore,
    feishu: { inboundEnabled: true, streamEnabled: true, ...overrides.feishu },
    bridgeName: "CodexWorker",
    sessionsPath: path.join(root, "feishu-sessions.json"),
    logger: (level, event, fields) => logs.push({ level, event, fields }),
    streamIntervalMs: 0,
    websocketFactory: () => socket,
    endpointResolver: async () => "wss://isolated.invalid/ws?service_id=1",
    ...overrides.options,
  });
  t.after(async () => { await channel.close(); });
  await channel.start();
  await flush();
  if (overrides.connect !== false) socket.open();
  return { channel, adapter, notifier, transport, socket, logs, root, sessionStore };
}

test("a p2p text runs one turn, posts 「输出」then「回复」, and the next message resumes the same thread", async t => {
  const f = await fixture(t);
  f.socket.frame(textEvent("ou_trainer", "om_1", "你好"));
  await waitFor(() => f.notifier.sent.length === 1, "reply card");

  assert.equal(f.adapter.calls.length, 1);
  const threadId = f.adapter.calls[0].threadId;
  assert.match(threadId, /^cli-/);
  assert.equal(f.adapter.calls[0].text, "你好");
  assert.equal(f.transport.sends.length, 1);
  assert.equal(f.transport.sends[0].meta.title, "CodexWorker-输出");
  assert.equal(f.transport.sends[0].meta.threadId, threadId);
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-回复");
  assert.equal(f.notifier.sent[0].options.threadId, threadId);
  assert.equal(f.notifier.sent[0].options.chatId, "oc_chat");
  assert.equal(f.notifier.sent[0].text, "OK");
  assert.match(f.transport.sends[0].text, /^⏳ 已收到/);
  assert.ok(f.transport.updates.length >= 1); // progress updates the same card
  assert.equal(f.transport.updates[0].messageId, "om_card_1");

  // Bridge-owned history: user message and reply are persisted, in order.
  const snapshot = await f.sessionStore.read(threadId);
  assert.equal(snapshot.ok, true);
  assert.deepEqual(snapshot.messages.map(message => [message.role, message.text]),
    [["user", "你好"], ["assistant", "OK"]]);
  assert.equal(snapshot.messages[0].messageId, feishuMessageId("om_1"));
  assert.equal(snapshot.meta.source, "feishu");
  assert.equal(snapshot.meta.sessionId, SESSION);

  await waitFor(() => f.channel.store.get("ou_trainer")?.threadId === threadId, "stored session");
  f.socket.frame(textEvent("ou_trainer", "om_2", "再来"));
  await waitFor(() => f.notifier.sent.length === 2, "second reply");
  assert.equal(f.adapter.calls.length, 2);
  assert.equal(f.adapter.calls[1].threadId, threadId); // same thread
  assert.equal(f.notifier.sent[1].text, "上一轮是 OK");
  const after = await f.sessionStore.read(threadId);
  assert.equal(after.totalMessages, 4);
});

test("card titles never carry the Desktop 「（历史受限）」suffix", async t => {
  const f = await fixture(t);
  f.socket.frame(textEvent("ou_trainer", "om_title", "你好"));
  await waitFor(() => f.notifier.sent.length === 1, "reply");
  for (const { options } of f.notifier.sent) assert.doesNotMatch(options.title, /历史受限/);
  for (const { meta } of f.transport.sends) assert.doesNotMatch(meta.title, /历史受限/);
  assert.deepEqual(f.transport.sends.map(item => item.meta.title), ["CodexWorker-输出"]);
});

test("an empty whitelist adopts the first sender and rejects everyone else", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: [] } });
  f.socket.frame(textEvent("ou_first", "om_first", "第一条"));
  await waitFor(() => f.notifier.sent.length === 1, "first reply");
  await waitFor(() => f.channel.store.state.ownerOpenId === "ou_first", "owner persisted");
  assert.equal(f.channel.whitelist.has("ou_first"), true);
  assert.ok(f.logs.some(entry => entry.event === "feishu_whitelist_bootstrap" && entry.fields.openId === "ou_first"));

  f.socket.frame(textEvent("ou_stranger", "om_second", "我也要"));
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(f.adapter.calls.length, 1);
  assert.equal(f.notifier.sent.length, 1);
  assert.ok(f.logs.some(entry => entry.event === "feishu_sender_rejected" && entry.fields.openId === "ou_stranger"));
  assert.equal(f.channel.status().rejected, 1);
});

test("groups, bot senders, images and duplicates never dispatch a turn", async t => {
  const f = await fixture(t);
  f.socket.frame(textEvent("ou_trainer", "om_group", "群里", { chat_type: "group" }));
  const bot = textEvent("ou_trainer", "om_bot2", "机器人");
  bot.event.sender.sender_type = "app";
  f.socket.frame(bot);
  f.socket.frame({ event: { sender: { sender_id: { open_id: "ou_trainer" }, sender_type: "user" },
    message: { message_id: "om_image", message_type: "image", chat_id: "oc_chat", chat_type: "p2p", content: "{}" } } });
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(f.adapter.calls.length, 0);
  // Only the image produces a hint card — never a Codex turn.
  assert.equal(f.notifier.sent.length, 1);
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-提示");

  f.socket.frame(textEvent("ou_trainer", "om_dup", "只算一次"));
  await waitFor(() => f.notifier.sent.length === 2, "first reply");
  f.socket.frame(textEvent("ou_trainer", "om_dup", "只算一次"));
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(f.adapter.calls.length, 1);
  assert.equal(f.channel.status().turns, 1);
  assert.equal((await f.sessionStore.list()).length, 1);
});

test("a failed turn still answers with a 回复 card and records the user message", async t => {
  const failure = Object.assign(new Error("codex_cli_unavailable"), {
    status: 503, body: { error: "codex_cli_unavailable", detail: "The codex CLI was not found." } });
  const f = await fixture(t, { adapter: fakeAdapter({ fail: failure }) });
  f.socket.frame(textEvent("ou_trainer", "om_fail", "你好"));
  await waitFor(() => f.notifier.sent.length === 1, "failure reply");
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-回复");
  assert.match(f.notifier.sent[0].text, /^本轮失败：/);
  assert.equal(f.channel.status().failures, 1);
  const threadId = f.channel.store.get("ou_trainer")?.threadId;
  assert.ok(threadId);
  const snapshot = await f.sessionStore.read(threadId);
  assert.deepEqual(snapshot.messages.map(message => message.role), ["user"]);
});

test("sessions persist atomically and a restart resumes from the stored thread", async t => {
  const f = await fixture(t);
  f.socket.frame(textEvent("ou_trainer", "om_persist", "你好"));
  await waitFor(() => f.notifier.sent.length === 1, "reply");
  const threadId = f.channel.store.get("ou_trainer")?.threadId;
  assert.ok(threadId);
  await f.channel.close();

  const file = path.join(f.root, "feishu-sessions.json");
  assert.equal((await stat(file)).mode & 0o777, 0o600);
  assert.equal((await readdir(f.root)).some(name => name.endsWith(".tmp")), false);

  const adapter = fakeAdapter();
  const reopened = await new SessionStore(path.join(f.root, "history"), { logger: () => {} }).init();
  const channel = new CliFeishuChannel({ adapter, notifier: fakeNotifier(), transport: fakeTransport(),
    sessionStore: reopened, feishu: {}, bridgeName: "CodexWorker", sessionsPath: file, logger: () => {},
    websocketFactory: () => new FakeSocket(), endpointResolver: async () => "wss://isolated.invalid/ws?service_id=1" });
  t.after(() => channel.close());
  await channel.start();
  assert.equal(channel.store.get("ou_trainer")?.threadId, threadId);
  const result = await channel.accept({ open_id: "ou_trainer", chat_id: "oc_chat", chat_type: "p2p",
    sender_type: "user", message_type: "text", message_id: "om_restart", content: "还在吗" });
  assert.equal(result.accepted, true);
  await waitFor(() => adapter.calls.length === 1, "resumed dispatch");
  assert.equal(adapter.calls[0].threadId, threadId);
  const snapshot = await reopened.read(threadId);
  assert.equal(snapshot.totalMessages, 4); // 2 from before the restart + 2 new
});

test("unreadable store degrades to a new session, logs the reason and never suffixes the title", async t => {
  const f = await fixture(t);
  const brokenThread = "cli-99999999-9999-4999-8999-999999999999";
  await f.sessionStore.create({ threadId: brokenThread, source: "feishu" });
  await writeFile(path.join(f.root, "history", `${brokenThread}.jsonl`), "not json\n{broken\n");
  await f.channel.store.set("ou_trainer", { threadId: brokenThread, chatId: "oc_chat", turns: 1 });

  f.socket.frame(textEvent("ou_trainer", "om_degraded", "继续"));
  await waitFor(() => f.notifier.sent.length === 1, "reply");
  assert.notEqual(f.adapter.calls[0].threadId, brokenThread);
  assert.match(f.transport.sends[0].text, /会话记录不可读|session_store_corrupt/);
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-回复");
  const degraded = f.logs.find(entry => entry.event === "feishu_history_degraded");
  assert.ok(degraded);
  assert.match(degraded.fields.reason, /^session_store_corrupt:/);
  assert.equal(degraded.fields.action, "new_session");
});

test("the 输出 card rewrites in place at most once per stream interval", async t => {
  const timers = new ManualTimers();
  const transport = fakeTransport();
  const card = new OutputCard({ transport, chatId: "oc_chat", threadId: SESSION, title: "CodexWorker-输出",
    intervalMs: 1800, now: () => timers.now, timers, logger: () => {} });
  await card.start("⏳ 已收到");
  assert.equal(transport.sends.length, 1);
  card.push("a"); card.push("b"); card.push("c");
  await flush();
  assert.equal(transport.updates.length, 0); // throttled: no write before the interval
  await timers.advance(1800);
  assert.equal(transport.updates.length, 1);
  assert.match(transport.updates[0].text, /c$/);
  assert.equal(transport.updates[0].messageId, "om_card_1");
  await card.close();
});

test("a rejected PATCH falls back to new 输出 messages instead of dropping progress", async t => {
  const transport = fakeTransport();
  transport.update = async (messageId, text, meta) => { transport.updates.push({ messageId, text, meta }); return { code: 99991400 }; };
  const card = new OutputCard({ transport, chatId: "oc_chat", threadId: SESSION, title: "CodexWorker-输出",
    intervalMs: 0, now: Date.now, logger: () => {} });
  await card.start("第一步");
  card.push("第二步");
  await card.finish();
  assert.equal(transport.updates.length, 1);
  assert.equal(transport.sends.length, 2); // degradation path sent a fresh message
  await card.close();
});

test("feishuMessageId is a stable uuid-shaped digest of the Feishu message id", () => {
  const a = feishuMessageId("om_abc");
  assert.equal(a, feishuMessageId("om_abc"));
  assert.match(a, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.notEqual(a, feishuMessageId("om_abcd"));
});
