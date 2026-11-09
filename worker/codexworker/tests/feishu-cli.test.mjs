// CLI-mode Feishu channel: no network, no credentials, no real Codex process.
// A fake websocket drives FeishuWebsocketClient; a fake adapter stands in for
// CliNativeAdapter; a temp SessionStore backs the bridge-owned history.
//
// Hermes parity: one inbound text produces exactly ONE outbound message. The
// 「输出」progress card (and every PATCH that fed it) is gone; progress is a
// Typing reaction that is removed on success and swapped for CrossMark on
// failure.
import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readdir, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { CliFeishuChannel, FinalMessage, feishuMessageId, isRateLimited, isStopCommand } from "../feishu-cli.mjs";
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

function fakeNotifier({ order = [], addReaction = null } = {}) {
  const sent = [];
  const reactions = [];
  return {
    appId: "test-app", appSecret: "test-secret", enabled: true, sent, reactions,
    async getToken() { return "test-token"; },
    async addReaction(messageId, emojiType = "Typing") {
      order.push(`addReaction:${messageId}:${emojiType}`);
      if (addReaction) return addReaction(messageId, emojiType);
      reactions.push(["add", messageId, emojiType]);
      return `react_${messageId}_${emojiType}`;
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

// Only FinalMessage's seam: the channel itself talks to the notifier, so every
// assertion about "how many messages went out" is `notifier.sent.length`.
function fakeTransport() {
  const sends = [];
  return {
    sends,
    async send(text, meta) {
      sends.push({ text, meta });
      return { code: 0, messageId: `om_msg_${sends.length}` };
    },
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
  const adapter = overrides.adapter ?? fakeAdapter();
  const socket = new FakeSocket();
  const logs = [];
  const sessionStore = overrides.sessionStore ??
    await new SessionStore(path.join(root, "history"), { logger: () => {} }).init();
  const channel = new CliFeishuChannel({
    adapter, notifier, sessionStore,
    feishu: { inboundEnabled: true, ...overrides.feishu },
    bridgeName: "CodexWorker",
    sessionsPath: path.join(root, "feishu-sessions.json"),
    logger: (level, event, fields) => logs.push({ level, event, fields }),
    websocketFactory: () => socket,
    endpointResolver: async () => "wss://isolated.invalid/ws?service_id=1",
    // This file drives the raw native wire protocol; the SDK transport has its
    // own suite (feishu-sdk-ws.test.mjs) and its own fake-SDK channel test.
    feishuImpl: "native",
    ...overrides.options,
  });
  t.after(async () => { await channel.close(); });
  await channel.start();
  await flush();
  if (overrides.connect !== false) socket.open();
  return { channel, adapter, notifier, socket, logs, root, sessionStore };
}

test("a p2p text runs one turn, sends exactly one message, and the next message resumes the same thread", async t => {
  const f = await fixture(t);
  f.socket.frame(textEvent("ou_trainer", "om_1", "你好"));
  await waitFor(() => f.notifier.sent.length === 1, "the one reply");

  assert.equal(f.adapter.calls.length, 1);
  const threadId = f.adapter.calls[0].threadId;
  assert.match(threadId, /^cli-/);
  assert.equal(f.adapter.calls[0].text, "你好");
  // One message, one send attempt, no second bubble and no PATCH surface left.
  assert.equal(f.notifier.sent.length, 1);
  assert.deepEqual(Object.keys(f.channel.transport), ["send"]);
  assert.equal(f.channel.status().sendAttempts, 1);
  assert.equal(f.channel.status().replies, 1);
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-回复");
  assert.equal(f.notifier.sent[0].options.threadId, threadId);
  assert.equal(f.notifier.sent[0].options.chatId, "oc_chat");
  assert.equal(f.notifier.sent[0].text, "OK");
  assert.equal(f.notifier.sent.some(message => /输出/.test(message.options.title)), false);

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
  assert.equal(f.channel.status().sendAttempts, 2);
  const after = await f.sessionStore.read(threadId);
  assert.equal(after.totalMessages, 4);
});

test("a long reply is split at 4096 chars: the title rides the first message, continuations say （续 N/M）", async t => {
  const adapter = fakeAdapter();
  adapter.dispatch = async options => {
    const paragraph = "这一行用来把回复撑长，保证超出一条飞书消息的上限，同时保留段落结构。\n\n";
    return { threadId: options.threadId, sessionId: options.threadId, turnId: "turn-long",
      status: "completed", resultText: paragraph.repeat(300), toolCalls: [], durationMs: 9 };
  };
  const f = await fixture(t, { adapter });
  f.socket.frame(textEvent("ou_trainer", "om_long", "写长一点"));
  await waitFor(() => f.logs.some(entry => entry.event === "feishu_turn_completed"), "turn end");

  const titles = f.notifier.sent.map(message => message.options.title);
  assert.ok(f.notifier.sent.length >= 3, `expected 3+ messages, got ${titles.length}`);
  assert.equal(titles[0], "CodexWorker-回复");
  const total = titles.length;
  assert.deepEqual(titles.slice(1), Array.from({ length: total - 1 }, (_, i) => `CodexWorker-回复（续 ${i + 2}/${total}）`));
  for (const message of f.notifier.sent) assert.ok(message.text.length <= 4096, "every chunk fits the cap");
  assert.equal(f.channel.status().sendAttempts, total);
  assert.equal(f.channel.status().replies, total);
  // History still holds the whole reply once, not the chunked bodies.
  const threadId = f.channel.store.get("ou_trainer")?.threadId;
  const snapshot = await f.sessionStore.read(threadId);
  assert.equal(snapshot.messages[1].text.length > 4096, true);
});

test("the trainer's message is reacted first, the reply goes out once, then the reaction is dropped", async t => {
  const order = [];
  const f = await fixture(t, { notifier: fakeNotifier({ order }), adapter: fakeAdapter({ order }) });
  f.socket.frame(textEvent("ou_trainer", "om_react", "你好"));
  await waitFor(() => f.notifier.reactions.some(item => item[0] === "remove"), "reaction removed");

  const at = label => order.indexOf(label);
  const add = at("addReaction:om_react:Typing");
  const dispatch = at("dispatch");
  const reply = at("send:CodexWorker-回复");
  const remove = at("removeReaction:om_react");
  assert.ok(add >= 0, "addReaction must be called with Typing");
  assert.ok(add < dispatch, "the Typing badge precedes the turn");
  assert.ok(dispatch < reply, "the turn runs before the single reply");
  assert.ok(reply < remove, "the reaction is removed after the reply");
  assert.deepEqual(f.notifier.reactions,
    [["add", "om_react", "Typing"], ["remove", "om_react", "react_om_react_Typing"]]);
  assert.equal(f.channel.reactions.size, 0);
  assert.equal(f.notifier.sent.length, 1);
});

test("a failing addReaction only logs: the turn and its one reply still happen", async t => {
  const denial = Object.assign(new Error("reaction_forbidden"), { code: 230027 });
  const f = await fixture(t, { notifier: fakeNotifier({ addReaction: async () => { throw denial; } }) });
  f.socket.frame(textEvent("ou_trainer", "om_react_fail", "你好"));
  await waitFor(() => f.notifier.sent.length === 1, "reply");

  assert.equal(f.adapter.calls.length, 1);
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-回复");
  assert.equal(f.notifier.sent[0].text, "OK");          // turn completed normally
  assert.equal(f.channel.status().failures, 0);
  assert.equal(f.notifier.reactions.length, 0);         // nothing to remove
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
    return { threadId: options.threadId, sessionId: options.threadId, turnId: `turn-${started}`,
      status: "completed", resultText: "OK", toolCalls: [], durationMs: 5 };
  };
  const f = await fixture(t, { notifier, adapter,
    feishu: { trainerOpenIdWhitelist: ["ou_a", "ou_b"] } });
  f.socket.frame(textEvent("ou_a", "om_a", "甲"));
  f.socket.frame(textEvent("ou_b", "om_b", "乙"));
  await waitFor(() => notifier.reactions.filter(item => item[0] === "remove").length === 2, "both removals");

  assert.equal(started, 2); // genuinely overlapping
  const removes = new Map(notifier.reactions.filter(item => item[0] === "remove").map(item => [item[1], item[2]]));
  assert.equal(removes.get("om_a"), "react_om_a_Typing");
  assert.equal(removes.get("om_b"), "react_om_b_Typing");
  assert.deepEqual([...f.channel.reactions.keys()], []);
  assert.equal(notifier.sent.length, 2);
});

test("message titles never carry the Desktop 「（历史受限）」suffix", async t => {
  const f = await fixture(t);
  f.socket.frame(textEvent("ou_trainer", "om_title", "你好"));
  await waitFor(() => f.notifier.sent.length === 1, "reply");
  for (const { options } of f.notifier.sent) assert.doesNotMatch(options.title, /历史受限/);
  assert.deepEqual(f.notifier.sent.map(item => item.options.title), ["CodexWorker-回复"]);
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
  // Only the image produces a hint — never a Codex turn, never a reaction.
  assert.equal(f.notifier.sent.length, 1);
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-提示");
  assert.deepEqual(f.notifier.reactions, []);

  f.socket.frame(textEvent("ou_trainer", "om_dup", "只算一次"));
  await waitFor(() => f.notifier.sent.length === 2, "first reply");
  f.socket.frame(textEvent("ou_trainer", "om_dup", "只算一次"));
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(f.adapter.calls.length, 1);
  assert.equal(f.notifier.sent.length, 2);
  assert.equal(f.channel.status().turns, 1);
  assert.equal((await f.sessionStore.list()).length, 1);
});

test("a failed turn sends one error message and swaps Typing for CrossMark", async t => {
  const failure = Object.assign(new Error("codex_cli_unavailable"), {
    status: 503, body: { error: "codex_cli_unavailable", detail: "The codex CLI was not found." } });
  const f = await fixture(t, { adapter: fakeAdapter({ fail: failure }) });
  f.socket.frame(textEvent("ou_trainer", "om_fail", "你好"));
  await waitFor(() => f.notifier.reactions.some(item => item[0] === "add" && item[2] === "CrossMark"),
    "CrossMark receipt");

  // Exactly one message, same card shape, error summary as the body.
  assert.equal(f.notifier.sent.length, 1);
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-回复");
  assert.match(f.notifier.sent[0].text, /^本轮失败：/);
  assert.match(f.notifier.sent[0].text, /codex CLI was not found/);
  assert.equal(f.channel.status().failures, 1);
  assert.equal(f.channel.status().deliveryFailures, 0);

  const remove = f.notifier.reactions.find(item => item[0] === "remove");
  const cross = f.notifier.reactions.find(item => item[0] === "add" && item[2] === "CrossMark");
  assert.deepEqual(remove, ["remove", "om_fail", "react_om_fail_Typing"]);
  assert.deepEqual(cross, ["add", "om_fail", "CrossMark"]);
  assert.ok(f.notifier.reactions.indexOf(remove) < f.notifier.reactions.indexOf(cross),
    "Typing is removed before the failure badge is added");
  const marked = f.logs.find(entry => entry.event === "feishu_reaction_marked_failed");
  assert.ok(marked);
  assert.equal(marked.fields.emoji, "CrossMark");

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
  const channel = new CliFeishuChannel({ adapter, notifier: fakeNotifier(),
    sessionStore: reopened, feishu: {}, bridgeName: "CodexWorker", sessionsPath: file, logger: () => {},
    websocketFactory: () => new FakeSocket(), endpointResolver: async () => "wss://isolated.invalid/ws?service_id=1",
    feishuImpl: "native" });
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

test("unreadable store degrades to a new session and says so in the one reply", async t => {
  const f = await fixture(t);
  const brokenThread = "cli-99999999-9999-4999-8999-999999999999";
  await f.sessionStore.create({ threadId: brokenThread, source: "feishu" });
  await writeFile(path.join(f.root, "history", `${brokenThread}.jsonl`), "not json\n{broken\n");
  await f.channel.store.set("ou_trainer", { threadId: brokenThread, chatId: "oc_chat", turns: 1 });

  f.socket.frame(textEvent("ou_trainer", "om_degraded", "继续"));
  await waitFor(() => f.notifier.sent.length === 1, "reply");
  assert.notEqual(f.adapter.calls[0].threadId, brokenThread);
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-回复");
  assert.match(f.notifier.sent[0].text, /会话记录不可读|session_store_corrupt/);
  assert.match(f.notifier.sent[0].text, /本轮|OK/);
  const degraded = f.logs.find(entry => entry.event === "feishu_history_degraded");
  assert.ok(degraded);
  assert.match(degraded.fields.reason, /^session_store_corrupt:/);
  assert.equal(degraded.fields.action, "new_session");
});

// ---------------------------------------------------------------------------
// The single final message: one send, bounded flood retry, one resend on an
// explicit rejection, never a duplicate after a timeout.
// ---------------------------------------------------------------------------

function finalMessage(overrides = {}) {
  const transport = overrides.transport ?? fakeTransport();
  const logs = overrides.logs ?? [];
  const message = new FinalMessage({ transport, chatId: "oc_chat", threadId: SESSION,
    title: "CodexWorker-回复", floodRetryDelayMs: 0,
    logger: (level, event, fields) => logs.push({ level, event, fields: fields ?? {} }),
    ...overrides.options });
  return { message, transport, logs };
}

test("one delivery is one send and nothing else", async () => {
  const { message, transport } = finalMessage();
  const outcome = await message.deliver("OK");
  assert.equal(outcome, "delivered");
  assert.equal(message.sends, 1);
  assert.equal(message.retries, 0);
  assert.equal(message.resends, 0);
  assert.equal(message.lastOutcome, "delivered");
  assert.equal(transport.sends.length, 1);
  assert.equal(transport.sends[0].text, "OK");
  assert.equal(transport.sends[0].meta.title, "CodexWorker-回复");
  assert.equal(transport.sends[0].meta.threadId, SESSION);
  assert.equal(message.messageId, "om_msg_1");
  message.close();
});

test("the same body delivered twice is a preview, not a second bubble", async () => {
  const { message, transport } = finalMessage();
  await message.deliver("OK");
  const second = await message.deliver("OK");
  assert.equal(second, "preview");
  assert.equal(message.sends, 1);
  assert.equal(transport.sends.length, 1);
  message.close();
});

test("a rate-limited send retries once and still lands exactly one message", async () => {
  const transport = fakeTransport();
  let attempts = 0;
  transport.send = async (text, meta) => {
    attempts += 1;
    transport.sends.push({ text, meta });
    return attempts === 1 ? { code: 99991400, msg: "request trigger frequency limit" } : { code: 0, messageId: "om_msg_1" };
  };
  const { message, logs } = finalMessage({ transport });
  const outcome = await message.deliver("OK");
  assert.equal(outcome, "delivered");
  assert.equal(attempts, 2, "initial attempt + one bounded retry");
  assert.equal(message.sends, 2);
  assert.equal(message.retries, 1);
  assert.equal(message.resends, 0);
  assert.equal(transport.sends.length, 2);
  assert.ok(logs.some(entry => entry.event === "feishu_reply_rate_limited"));
  message.close();
});

test("a rate-limited send is retried once, then one fresh message carries the body", async () => {
  const transport = fakeTransport();
  let attempts = 0;
  transport.send = async (text, meta) => {
    attempts += 1;
    transport.sends.push({ text, meta });
    return attempts <= 2 ? { code: 99991400, msg: "frequency limit" }
      : { code: 0, messageId: `om_msg_${attempts}` };
  };
  const { message } = finalMessage({ transport });
  const outcome = await message.deliver("最新正文");
  assert.equal(outcome, "delivered");
  assert.equal(attempts, 3, "two attempts on the first message, one on the resend");
  assert.equal(message.retries, 1);
  assert.equal(message.resends, 1);
  assert.equal(message.messageId, "om_msg_3");
  assert.equal(message.deliveryFailed, false);
  message.close();
});

test("when the retry and the resend both stay flood-limited, delivery fails fast and bounded", async () => {
  const transport = fakeTransport();
  transport.send = async (text, meta) => {
    transport.sends.push({ text, meta });
    return { code: 99991400, msg: "frequency limit" };
  };
  const { message, logs } = finalMessage({ transport });
  const outcome = await message.deliver("正文");
  assert.equal(outcome, "failed");
  assert.equal(message.lastOutcome, "failed");
  assert.equal(message.deliveryFailed, true);
  // Initial + retry, then resend + retry: bounded, never a ladder.
  assert.equal(transport.sends.length, 4);
  assert.equal(message.sends, 4);
  assert.equal(message.retries, 2);
  assert.equal(message.resends, 1);
  assert.ok(logs.some(entry => entry.event === "feishu_reply_delivery_failed"));
  message.close();
});

test("a timed-out send is ambiguous: logged, never re-sent as a duplicate", async () => {
  const transport = fakeTransport();
  transport.send = async (text, meta) => {
    transport.sends.push({ text, meta });
    return { code: -1, msg: "network error: The operation was aborted due to timeout", httpStatus: 0 };
  };
  const { message, logs } = finalMessage({ transport });
  const outcome = await message.deliver("正文");
  assert.equal(outcome, "ambiguous");
  assert.equal(message.deliveryAmbiguous, true);
  assert.equal(message.deliveryFailed, false);
  assert.equal(transport.sends.length, 1, "no duplicate message after a timeout");
  assert.ok(logs.some(entry => entry.event === "feishu_reply_delivery_ambiguous"));
  message.close();
});

test("an explicit rejection posts exactly one replacement message", async () => {
  const transport = fakeTransport();
  let attempts = 0;
  transport.send = async (text, meta) => {
    attempts += 1;
    transport.sends.push({ text, meta });
    return attempts === 1 ? { code: 230002, msg: "message not found" }
      : { code: 0, messageId: "om_msg_2" };
  };
  const { message, logs } = finalMessage({ transport });
  const outcome = await message.deliver("正文");
  assert.equal(outcome, "delivered");
  assert.equal(attempts, 2);
  assert.equal(message.resends, 1);
  assert.equal(message.retries, 0, "an explicit rejection is not retried");
  assert.equal(message.messageId, "om_msg_2");
  assert.equal(transport.sends[1].text, "正文");
  assert.equal(transport.sends[1].meta.title, "CodexWorker-回复");
  assert.ok(logs.some(entry => entry.event === "feishu_reply_resend"));
  message.close();
});

test("a retry_after beyond the cap is not slept on, and no resend is attempted", async () => {
  const transport = fakeTransport();
  transport.send = async (text, meta) => {
    transport.sends.push({ text, meta });
    return { code: 99991400, msg: "frequency limit", retry_after: 120 };
  };
  const { message, logs } = finalMessage({ transport, options: { maxResends: 0 } });
  const started = Date.now();
  const outcome = await message.deliver("正文");
  assert.equal(outcome, "failed");
  assert.ok(Date.now() - started < 500, "no wait for a 120s retry_after");
  assert.equal(transport.sends.length, 1);
  assert.ok(logs.some(entry => entry.event === "feishu_reply_retry_too_long"));
  message.close();
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

test("/stop bypasses the queue and the interrupted turn is the only message, marked 已中断", async t => {
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
    return { threadId: options.threadId, sessionId: options.threadId, turnId: "turn-stop",
      status: "cancelled", resultText: "" };
  };
  const f = await fixture(t, { adapter });
  f.socket.frame(textEvent("ou_trainer", "om_run", "长任务"));
  await running;

  f.socket.frame(textEvent("ou_trainer", "om_stop", "/stop"));
  await waitFor(() => stops.length === 1, "adapter stop");
  await waitFor(() => f.notifier.sent.length === 1, "the interrupted turn's reply");
  await flush();

  assert.equal(stops[0], dispatched[0].threadId);
  // One message only: the cancelled turn's own reply. No extra 「提示」notice.
  assert.equal(f.notifier.sent.length, 1);
  assert.equal(f.notifier.sent[0].options.title, "CodexWorker-回复");
  assert.match(f.notifier.sent[0].text, /已中断/);
  assert.equal(f.channel.status().stops, 1);
  assert.equal(dispatched.length, 1, "/stop never dispatches a turn of its own");
  // Both Typing badges (turn + stop command) are gone; a cancel is not a failure.
  assert.equal(f.notifier.reactions.filter(item => item[0] === "add" && item[2] === "CrossMark").length, 0);
  assert.equal(f.notifier.reactions.filter(item => item[0] === "remove").length, 2);

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

test("a cancelled turn keeps its one reply honest even when the adapter stops mid-tool", async t => {
  const adapter = fakeAdapter();
  const f = await fixture(t, { adapter });
  adapter.dispatch = async options => {
    options.onEvent?.({ type: "tool.completed", threadId: options.threadId, payload: { name: "shell", command: "sleep 9" } });
    return { threadId: options.threadId, sessionId: options.threadId, turnId: "turn-cancel",
      status: "cancelled", resultText: "半截输出" };
  };
  f.socket.frame(textEvent("ou_trainer", "om_cancel", "跑到一半"));
  await waitFor(() => f.notifier.sent.length === 1, "reply");
  assert.equal(f.notifier.sent.length, 1);
  assert.match(f.notifier.sent[0].text, /半截输出/);
  assert.match(f.notifier.sent[0].text, /已中断/);
  assert.equal(f.notifier.reactions.filter(item => item[0] === "add" && item[2] === "CrossMark").length, 0);
});

test("feishuMessageId is a stable uuid-shaped digest of the Feishu message id", () => {
  const a = feishuMessageId("om_abc");
  assert.equal(a, feishuMessageId("om_abc"));
  assert.match(a, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.notEqual(a, feishuMessageId("om_abcd"));
});
