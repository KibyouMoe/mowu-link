// CLI-mode Feishu channel: no network, no credentials, no real Codex process.
// A fake websocket drives FeishuWebsocketClient; a fake adapter stands in for
// CliNativeAdapter; a temp SessionStore backs the bridge-owned history.
import assert from "node:assert/strict";
import test from "node:test";
import { mkdir, mkdtemp, readdir, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { CliFeishuChannel, OutputCard, TurnProgress, feishuMessageId, isRateLimited, isStopCommand } from "../feishu-cli.mjs";
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

function fakeNotifier({ order = [], addReaction = null } = {}) {
  const sent = [];
  const reactions = [];
  return {
    appId: "test-app", appSecret: "test-secret", enabled: true, sent, reactions,
    async getToken() { return "test-token"; },
    async addReaction(messageId) {
      order.push(`addReaction:${messageId}`);
      if (addReaction) return addReaction(messageId);
      reactions.push(["add", messageId]);
      return `react_${messageId}`;
    },
    async removeReaction(messageId, reactionId) {
      order.push(`removeReaction:${messageId}`);
      reactions.push(["remove", messageId, reactionId]);
      return true;
    },
    async sendMessage(text, options = {}) {
      order.push(`send:${options.title}`);
      sent.push({ text, options });
      return { code: 0, data: { message_id: `om_reply_${sent.length}` } };
    },
  };
}

function fakeTransport(order = []) {
  const sends = [], updates = [];
  return {
    sends, updates,
    async send(text, meta) {
      order.push(`card:${meta.title}`);
      sends.push({ text, meta });
      return { code: 0, messageId: `om_card_${sends.length}` };
    },
    async update(messageId, text, meta) { updates.push({ messageId, text, meta }); return { code: 0 }; },
  };
}

// register() gives every conversation the same stable cli-<uuid> shape the
// HTTP /threads route uses; dispatch() echoes it back like CliNativeAdapter.
function fakeAdapter({ fail = null, threadId = SESSION, order = [] } = {}) {
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
      order.push("dispatch");
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

test("the trainer's message is reacted first, and the reaction is dropped after the turn", async t => {
  const order = [];
  const f = await fixture(t, { notifier: fakeNotifier({ order }),
    transport: fakeTransport(order), adapter: fakeAdapter({ order }) });
  f.socket.frame(textEvent("ou_trainer", "om_react", "你好"));
  await waitFor(() => f.notifier.reactions.some(item => item[0] === "remove"), "reaction removed");

  const at = label => order.indexOf(label);
  const add = at("addReaction:om_react");
  const card = at("card:CodexWorker-输出");
  const dispatch = at("dispatch");
  const reply = at("send:CodexWorker-回复");
  const remove = at("removeReaction:om_react");
  assert.ok(add >= 0, "addReaction must be called");
  assert.ok(add < card, "reaction must precede the 输出 card");
  assert.ok(card < dispatch && dispatch < reply, "输出 card, then the turn, then the 回复 card");
  assert.ok(reply < remove, "reaction must be removed after the reply");
  assert.deepEqual(f.notifier.reactions,
    [["add", "om_react"], ["remove", "om_react", "react_om_react"]]);
  assert.equal(f.channel.reactions.size, 0);
});

test("a failing addReaction only logs: the turn and both cards still happen", async t => {
  const denial = Object.assign(new Error("reaction_forbidden"), { code: 230027 });
  const f = await fixture(t, { notifier: fakeNotifier({ addReaction: async () => { throw denial; } }) });
  f.socket.frame(textEvent("ou_trainer", "om_react_fail", "你好"));
  await waitFor(() => f.notifier.sent.length === 1, "reply card");

  assert.equal(f.adapter.calls.length, 1);
  assert.equal(f.transport.sends.length, 1);           // 输出 card still posted
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-回复");
  assert.equal(f.notifier.sent[0].text, "OK");          // turn completed normally
  assert.equal(f.channel.status().failures, 0);
  assert.equal(f.notifier.reactions.some(item => item[0] === "remove"), false);
  const logged = f.logs.find(entry => entry.event === "feishu_reaction_failed");
  assert.ok(logged);
  assert.equal(logged.fields.stage, "add");
  assert.equal(logged.fields.code, 230027);
});

test("two concurrent turns keep their own reaction ids", async t => {
  const order = [];
  const notifier = fakeNotifier({ order });
  const adapter = fakeAdapter({ order });
  let started = 0;
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  adapter.dispatch = async options => {
    order.push("dispatch");
    started += 1;
    if (started === 2) release();
    await gate; // both turns must be in flight before either answers
    options.onEvent?.({ type: "tool.completed", threadId: options.threadId, payload: { name: "shell" } });
    return { threadId: options.threadId, sessionId: options.threadId, turnId: `turn-${started}`,
      status: "completed", resultText: "OK", toolCalls: [], durationMs: 5 };
  };
  const f = await fixture(t, { notifier, transport: fakeTransport(order), adapter,
    feishu: { trainerOpenIdWhitelist: ["ou_a", "ou_b"] } });
  f.socket.frame(textEvent("ou_a", "om_a", "甲"));
  f.socket.frame(textEvent("ou_b", "om_b", "乙"));
  await waitFor(() => notifier.reactions.filter(item => item[0] === "remove").length === 2, "both removals");

  assert.equal(started, 2); // genuinely overlapping
  const removes = new Map(notifier.reactions.filter(item => item[0] === "remove").map(item => [item[1], item[2]]));
  assert.equal(removes.get("om_a"), "react_om_a");
  assert.equal(removes.get("om_b"), "react_om_b");
  assert.deepEqual([...f.channel.reactions.keys()], []);
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
  // Only the image produces a hint card — never a Codex turn, never a reaction.
  assert.equal(f.notifier.sent.length, 1);
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-提示");
  assert.deepEqual(f.notifier.reactions, []);

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
  // The receipt is cleared even though the turn failed.
  await waitFor(() => f.notifier.reactions.some(item => item[0] === "remove"), "reaction removed");
  assert.deepEqual(f.notifier.reactions.find(item => item[0] === "remove"),
    ["remove", "om_fail", "react_om_fail"]);
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

// ---------------------------------------------------------------------------
// Event-driven 「输出」card: no timer, state boundaries only.
// ---------------------------------------------------------------------------

function outputCard(overrides = {}) {
  const transport = overrides.transport ?? fakeTransport();
  const timers = overrides.timers ?? { setTimeout, clearTimeout };
  const logs = overrides.logs ?? [];
  const card = new OutputCard({ transport, chatId: "oc_chat", threadId: SESSION,
    title: "CodexWorker-输出", now: overrides.now ?? Date.now, timers,
    floodRetryDelayMs: overrides.floodRetryDelayMs ?? 0,
    logger: (level, event, fields) => logs.push({ level, event, fields: fields ?? {} }),
    ...overrides.options });
  return { card, transport, timers, logs };
}

test("three back-to-back shell events cost one tool PATCH (turn start + tool = 2 writes)", async () => {
  const { card, transport } = outputCard();
  const progress = new TurnProgress({ startedAt: 0, now: () => 0 });
  await card.start("⏳ 已收到，正在执行…");

  let toolWrites = 0;
  for (let index = 0; index < 3; index++) {
    if (progress.noteTool("shell", "printf ok")) {
      toolWrites += 1;
      await card.push(progress.render());
    }
  }
  assert.equal(toolWrites, 1, "same tool back-to-back is one state change");
  assert.equal(transport.sends.length, 1);   // turn start
  assert.equal(transport.updates.length, 1); // the tool state
  assert.equal(transport.sends.length + transport.updates.length, 2);
  assert.match(transport.updates[0].text, /🔧 shell: printf ok/);
  await card.close();
});

test("a rate-limited PATCH retries once and carries the newest merged snapshot", async () => {
  const timers = new ManualTimers();
  const transport = fakeTransport();
  let attempts = 0;
  transport.update = async (messageId, text, meta) => {
    attempts += 1;
    transport.updates.push({ messageId, text, meta });
    return attempts === 1 ? { code: 99991400, msg: "request trigger frequency limit" } : { code: 0 };
  };
  const { card } = outputCard({ transport, timers, now: () => timers.now, floodRetryDelayMs: 50 });
  await card.start("开始");
  const pending = card.push("快照1");
  await flush();
  assert.equal(card.patches, 1, "first PATCH rejected with 429");
  card.push("快照2");
  card.push("快照3");
  await timers.advance(50);
  await pending;

  assert.equal(attempts, 2, "one initial attempt + one bounded retry");
  assert.match(transport.updates.at(-1).text, /快照3$/, "merged queue kept only the newest snapshot");
  assert.equal(transport.sends.length, 1, "no replacement message for a recoverable flood");
  assert.equal(card.lastOutcome, "delivered");
  assert.equal(card.queuedText, null);
  await card.close();
});

test("a flood-limited final PATCH whose preview is already complete settles as preview without a resend", async () => {
  const { card, transport } = outputCard();
  await card.start("开始");
  await card.push("完整快照");
  assert.equal(card.lastOutcome, "delivered");
  const sendsBefore = transport.sends.length;

  transport.update = async (messageId, text, meta) => {
    transport.updates.push({ messageId, text, meta });
    return { code: 99991400, msg: "frequency limit" };
  };
  await card.finish("完整快照"); // forced final PATCH, same body already visible
  assert.equal(card.lastOutcome, "preview");
  assert.equal(card.deliveryFailed, false);
  assert.equal(transport.sends.length, sendsBefore, "the ACKed preview is authoritative: no second bubble");
  await card.close();
});

test("a timed-out PATCH is ambiguous: logged, never re-sent as a duplicate", async () => {
  const { card, transport, logs } = outputCard();
  await card.start("开始");
  transport.update = async (messageId, text, meta) => {
    transport.updates.push({ messageId, text, meta });
    return { code: -1, msg: "network error: The operation was aborted due to timeout", httpStatus: 0 };
  };
  await card.push("新快照");

  assert.equal(card.lastOutcome, "ambiguous");
  assert.equal(card.deliveryAmbiguous, true);
  assert.equal(transport.sends.length, 1, "no duplicate message after a timeout");
  assert.ok(logs.some(entry => entry.event === "feishu_card_delivery_ambiguous"));
  assert.equal(card.deliveryFailed, false);
  await card.close();
});

test("PATCH stuck on 429: bounded retry is exhausted, then one fresh message carries the latest snapshot", async () => {
  const { card, transport } = outputCard();
  await card.start("开始");
  transport.update = async (messageId, text, meta) => {
    transport.updates.push({ messageId, text, meta });
    return { code: 99991400, msg: "request trigger frequency limit" };
  };
  await card.push("最新快照");
  await waitFor(() => transport.sends.length === 2, "replacement message after the flood schedule");

  assert.equal(card.retries, 1, "exactly one bounded retry before rotating");
  assert.equal(card.rotations, 1);
  assert.equal(card.messageId, "om_card_2");
  assert.match(transport.sends[1].text, /最新快照/);
  assert.equal(transport.sends[1].meta.title, "CodexWorker-输出");
  assert.equal(card.lastOutcome, "delivered");
  assert.equal(card.deliveryFailed, false);
  await waitFor(() => transport.updates.some(update => update.messageId === "om_card_1"
    && /本卡已停止更新，见下条/.test(update.text)), "old card marked as stopped");
  await card.close();
});

test("a rejected PATCH posts one replacement message, notes the stale card and retargets updates", async () => {
  const { card, transport } = outputCard();
  await card.start("开始");
  transport.update = async (messageId, text, meta) => {
    transport.updates.push({ messageId, text, meta });
    return messageId === "om_card_1" ? { code: 230002, msg: "message not found" } : { code: 0 };
  };
  await card.push("最新快照");
  await waitFor(() => transport.sends.length === 2, "replacement message");
  assert.equal(card.retries, 0, "an explicit rejection is not retried");
  assert.equal(card.rotations, 1);
  assert.equal(card.messageId, "om_card_2");
  assert.match(transport.sends[1].text, /最新快照/);
  assert.equal(transport.sends[1].meta.title, "CodexWorker-输出");
  assert.equal(card.deliveryFailed, false);

  await waitFor(() => transport.updates.some(update => update.messageId === "om_card_1"
    && /本卡已停止更新，见下条/.test(update.text)), "stale card note");

  // Later state changes PATCH the replacement, not the frozen card.
  await card.push("后续更新");
  assert.equal(transport.updates.at(-1).messageId, "om_card_2");
  assert.match(transport.updates.at(-1).text, /后续更新$/);
  await card.close();
});

test("when PATCH and the replacement message both stay flood-limited, delivery fails fast and bounded", async () => {
  const { card, transport, logs } = outputCard();
  await card.start("开始");
  const initialSends = transport.sends.length;
  transport.update = async (messageId, text, meta) => {
    transport.updates.push({ messageId, text, meta });
    return { code: 99991400, msg: "frequency limit" };
  };
  transport.send = async (text, meta) => {
    transport.sends.push({ text, meta });
    return { code: 99991400, msg: "frequency limit" };
  };
  await card.push("最新快照");
  await waitFor(() => card.deliveryFailed, "delivery failure");

  assert.equal(card.lastOutcome, "failed");
  // Initial send + two attempts at the replacement: bounded, never a ladder.
  assert.equal(transport.sends.length, initialSends + 2);
  // Two PATCH attempts + one best-effort note on the stale card.
  assert.equal(transport.updates.length, 3);
  assert.ok(logs.some(entry => entry.event === "feishu_card_delivery_failed"));
  await card.close();
});

test("rate-limit and stop-command classification helpers stay narrow", () => {
  assert.equal(isRateLimited({ httpStatus: 429 }), true);
  assert.equal(isRateLimited({ code: 99991400 }), true);
  assert.equal(isRateLimited({ code: 230002, msg: "message not found" }), false);
  assert.equal(isRateLimited(null), false);
  assert.equal(isStopCommand("/stop"), true);
  assert.equal(isStopCommand("  /stop@codexworker "), true);
  assert.equal(isStopCommand("/stop now"), false);
  assert.equal(isStopCommand("停止"), false);
});

// ---------------------------------------------------------------------------
// /stop on the Feishu side
// ---------------------------------------------------------------------------

test("/stop bypasses the running turn's queue, interrupts it and the 输出 card becomes 已中断", async t => {
  const adapter = fakeAdapter();
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let started;
  const running = new Promise(resolve => { started = resolve; });
  const stops = [];
  const dispatched = [];
  adapter.stop = async threadId => { stops.push(threadId); release(); return { status: "cancelled", threadId }; };
  adapter.dispatch = async options => {
    dispatched.push(options);
    started();
    await gate;
    options.onEvent?.({ type: "tool.completed", threadId: options.threadId, payload: { name: "shell" } });
    return { threadId: options.threadId, sessionId: options.threadId, turnId: "turn-stop",
      status: "cancelled", resultText: "" };
  };
  const f = await fixture(t, { adapter });
  f.socket.frame(textEvent("ou_trainer", "om_run", "长任务"));
  await running;

  f.socket.frame(textEvent("ou_trainer", "om_stop", "/stop"));
  await waitFor(() => stops.length === 1, "adapter stop");
  await waitFor(() => f.notifier.sent.some(message => /已中断当前回合/.test(message.text)), "stop notice");
  await waitFor(() => f.notifier.sent.some(message => message.options.title === "CodexWorker-回复"), "reply");

  assert.equal(stops[0], dispatched[0].threadId);
  const reply = f.notifier.sent.find(message => message.options.title === "CodexWorker-回复");
  assert.match(reply.text, /已中断/);
  assert.ok(f.transport.updates.some(update => update.meta.title === "CodexWorker-已中断"),
    "the 输出 card title changed to 已中断");
  assert.equal(f.channel.status().stops, 1);
  assert.equal(dispatched.length, 1, "/stop never dispatches a turn of its own");

  const snapshot = await f.sessionStore.read(dispatched[0].threadId);
  assert.deepEqual(snapshot.messages.map(message => message.role), ["user", "assistant"]);
  assert.match(snapshot.messages[1].text, /已中断/);
});

test("/stop with nothing running answers no_active_turn and dispatches nothing", async t => {
  const adapter = fakeAdapter();
  const stops = [];
  adapter.stop = async threadId => { stops.push(threadId); return { ok: true, status: "no_active_turn", threadId }; };
  const f = await fixture(t, { adapter });

  f.socket.frame(textEvent("ou_trainer", "om_idle_stop", "/stop"));
  await waitFor(() => f.notifier.sent.some(message => /no_active_turn/.test(message.text)), "idle stop notice");

  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-提示");
  assert.equal(stops.length, 1);
  assert.equal(f.adapter.calls.length, 0);
  assert.equal((await f.sessionStore.list()).length, 0);
  assert.equal(f.channel.status().stops, 1);
  await waitFor(() => f.notifier.reactions.some(item => item[0] === "remove"), "reaction cleared");
});

test("a cancelled turn keeps the reply card honest even when the adapter stops mid-tool", async t => {
  const adapter = fakeAdapter();
  const f = await fixture(t, { adapter });
  adapter.dispatch = async options => {
    options.onEvent?.({ type: "tool.completed", threadId: options.threadId, payload: { name: "shell", command: "sleep 9" } });
    return { threadId: options.threadId, sessionId: options.threadId, turnId: "turn-cancel",
      status: "cancelled", resultText: "半截输出" };
  };
  f.socket.frame(textEvent("ou_trainer", "om_cancel", "跑到一半"));
  await waitFor(() => f.notifier.sent.length === 1, "reply");
  assert.match(f.notifier.sent[0].text, /半截输出/);
  assert.match(f.notifier.sent[0].text, /已中断/);
  assert.ok(f.transport.updates.some(update => /⏹ 已中断/.test(update.text)));
});

test("feishuMessageId is a stable uuid-shaped digest of the Feishu message id", () => {
  const a = feishuMessageId("om_abc");
  assert.equal(a, feishuMessageId("om_abc"));
  assert.match(a, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.notEqual(a, feishuMessageId("om_abcd"));
});
