import assert from "node:assert/strict";
import test from "node:test";
import { DesktopManager } from "../desktop-manager.mjs";
import { FeishuNotifier, markdownPayload } from "../feishu.mjs";

function fixture(t) {
  const manager = new DesktopManager({ native: async () => {}, listNativeTools: async () => [],
    stateDir: "/tmp/unused-feishu-stream-test", config: { bridgeName: "test", feishu: { streamUpdateIntervalMs: 250 } } });
  const session = { threadId: "thread", feishuChatId: "chat", hostId: "local", seen: {}, relayStatus: {},
    turnMessages: {}, observedTurns: {}, completedMessages: {}, pendingInputs: [], queue: [], steerQueue: [] };
  manager.state.sessions.thread = session; manager.state.selectedThreadId = "thread";
  manager.state.selectionEpoch = 1; manager.handoffPaused = true;
  const calls = [], saves = [];
  manager.save = async () => { saves.push(structuredClone(manager.state)); };
  manager.notifier = {
    sendMessage: async (text, opts) => { calls.push({ method: "POST", text, opts }); return { code: 0, data: { message_id: "card-" + calls.length } }; },
    updateMessage: async (id, text, opts) => { calls.push({ method: "PATCH", id, text, opts }); return { code: 0, data: { message_id: id } }; },
  };
  t.after(() => { manager.closed = true; clearTimeout(manager.publicTextTimer); });
  const enqueue = (text, final = false, key = "turn/item") => manager.enqueuePublicText(session, "turn", key, text, 1, final);
  const flush = async () => {
    for (const e of Object.values(manager.state.publicTextOutbox ?? {})) e.lastAttemptMs = 0;
    manager.handoffPaused = false;
    await manager.drainPublicTexts();
    manager.handoffPaused = true;
  };
  return { manager, session, calls, saves, enqueue, flush };
}
test("stream starts one card and PATCHes the same id with complete growing text", async t => {
  const f = fixture(t);
  f.enqueue("你"); await f.flush();
  f.enqueue("你好"); await f.flush();
  f.enqueue("你好，完成", true); await f.flush();
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "PATCH", "PATCH"]);
  assert.deepEqual(f.calls.map(c => c.text), ["你", "你好", "你好，完成"]);
  assert.equal(f.calls[1].id, "card-1"); assert.equal(f.calls[2].id, "card-1");
  assert.equal(Object.keys(f.manager.state.publicTextOutbox).length, 1);
});
test("fast snapshots coalesce to latest text before the first send", async t => {
  const f = fixture(t);
  for (const text of ["a", "ab", "abc"]) f.enqueue(text);
  await f.flush(); assert.equal(f.calls.length, 1); assert.equal(f.calls[0].text, "abc");
});
test("unchanged snapshots cause no writes; final phase patches in place", async t => {
  const f = fixture(t); f.enqueue("done"); await f.flush();
  f.enqueue("done"); await f.flush(); assert.equal(f.calls.length, 1);
  f.enqueue("done", true); await f.flush();
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "PATCH"]);
  assert.match(f.calls[1].opts.title, /回复/);
});
test("new snapshots arriving during POST survive the older response and reuse its card id", async t => {
  const f = fixture(t); let resolvePost, started;
  const start = new Promise(resolve => { started = resolve; });
  f.manager.notifier.sendMessage = (text, opts) => { f.calls.push({ method: "POST", text, opts });
    started(); return new Promise(resolve => { resolvePost = resolve; }); };
  f.enqueue("a"); f.manager.handoffPaused = false;
  const running = f.manager.drainPublicTexts(); await start;
  f.enqueue("abc", true);
  resolvePost({ code: 0, data: { message_id: "same-card" } }); await running;
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "PATCH"]);
  assert.equal(f.calls[1].id, "same-card"); assert.equal(f.calls[1].text, "abc");
});
test("updates are throttled and coalesced while final flush bypasses the wait", async t => {
  const f = fixture(t); f.enqueue("a"); await f.flush();
  f.enqueue("ab");
  f.manager.handoffPaused = false; await f.manager.drainPublicTexts();
  assert.equal(f.calls.length, 1); assert.ok(f.manager.publicTextTimer);
  f.enqueue("abc", true); await f.manager.drainPublicTexts();
  assert.equal(f.calls.length, 2); assert.equal(f.calls[1].text, "abc");
});
test("failed PATCH never falls back to POST; next revision targets the known card", async t => {
  const f = fixture(t); f.enqueue("a"); await f.flush();
  f.manager.notifier.updateMessage = async (id, text) => { f.calls.push({ method: "PATCH", id, text }); return null; };
  f.enqueue("ab"); await f.flush(); f.enqueue("abc", true); await f.flush();
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "PATCH", "PATCH"]);
  assert.equal(f.calls[2].id, "card-1");
});
test("unconfirmed creation keeps one stable receipt and never creates another card", async t => {
  const f = fixture(t);
  f.manager.notifier.sendMessage = async (text, opts) => { f.calls.push({ method: "POST", text, opts }); return null; };
  f.enqueue("a"); await f.flush(); f.enqueue("ab"); await f.flush(); f.enqueue("abc", true); await f.flush();
  assert.equal(f.calls.length, 1);
  assert.equal(Object.values(f.manager.state.publicTextOutbox)[0].status, "unknown");
});
test("persisted card mapping survives outbox pruning and process recreation", async t => {
  const f = fixture(t); f.enqueue("a"); await f.flush();
  const state = JSON.parse(JSON.stringify(f.manager.state)); state.publicTextOutbox = {};
  const g = fixture(t); g.manager.state = state;
  g.manager.enqueuePublicText(state.sessions.thread, "turn", "turn/item", "ab", 1, true);
  await g.flush();
  assert.equal(g.calls.length, 1); assert.equal(g.calls[0].method, "PATCH"); assert.equal(g.calls[0].id, "card-1");
});
test("selection changes cancel pending updates without creating a new message", async t => {
  const f = fixture(t); f.enqueue("a"); await f.flush(); f.enqueue("ab", true);
  f.manager.state.selectionEpoch++; await f.flush(); assert.equal(f.calls.length, 1);
});
test("different output items get distinct cards, not one shared mutable card", async t => {
  const f = fixture(t); f.enqueue("first", false, "turn/first"); f.enqueue("second", true, "turn/second"); await f.flush();
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "POST"]);
});
test("oversized replies keep fixed pages and update them instead of resending", async t => {
  const f = fixture(t); f.enqueue("x".repeat(9000)); await f.flush();
  f.enqueue("x".repeat(9100), true); await f.flush();
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "POST", "PATCH", "PATCH"]);
  assert.ok(f.calls.every(c => c.text.length <= 8000));
  assert.deepEqual(f.calls.slice(2).map(c => c.id), ["card-1", "card-2"]);
});
test("inline images retain one text card before and after the image", async t => {
  const f = fixture(t), images = [];
  f.manager.publicImageRelay.observe = (_session, snapshot) => images.push(snapshot);
  f.enqueue("before\n\n![image](/tmp/photo.png)\n\nafter"); await f.flush();
  f.enqueue("before\n\n![image](/tmp/photo.png)\n\nafter updated", true); await f.flush();
  assert.equal(f.calls.filter(c => c.method === "POST").length, 2);
  assert.equal(f.calls.filter(c => c.method === "PATCH").length, 2);
  assert.equal(images.length, 2);
});
test("new and updated card payloads declare shared updates", () => {
  assert.equal(JSON.parse(markdownPayload("hi", "title", "thread")).config.update_multi, true);
});
test("notifier PATCHes the known message with full markdown and preserves its id", async t => {
  const previous = globalThis.fetch, calls = [];
  globalThis.fetch = async (url, opts) => { calls.push({ url, ...opts }); return { json: async () => ({ code: 0 }) }; };
  t.after(() => { globalThis.fetch = previous; });
  const notifier = new FeishuNotifier({ appId: "test", appSecret: "test" });
  notifier.getToken = async () => "test-token";
  const result = await notifier.updateMessage("om_known", "**full**", { title: "reply", threadId: "thread" });
  assert.equal(calls.length, 1); assert.equal(calls[0].method, "PATCH");
  assert.ok(calls[0].url.endsWith("/im/v1/messages/om_known"));
  const card = JSON.parse(JSON.parse(calls[0].body).content);
  assert.equal(card.elements[0].content, "**full**"); assert.equal(card.config.update_multi, true);
  assert.equal(result.data.message_id, "om_known");
});
test("notifier refreshes an expired token but never POSTs on failed updates", async t => {
  const previous = globalThis.fetch, methods = []; let tokens = 0;
  globalThis.fetch = async (_url, opts) => { methods.push(opts.method); return { json: async () => ({ code: methods.length === 1 ? 99991663 : 0 }) }; };
  t.after(() => { globalThis.fetch = previous; });
  const notifier = new FeishuNotifier({ appId: "test", appSecret: "test" });
  notifier.getToken = async () => "token-" + ++tokens;
  await notifier.updateMessage("om_known", "latest");
  assert.deepEqual(methods, ["PATCH", "PATCH"]); assert.equal(tokens, 2);
});
test("notifier rechecks selection after token refresh and durable intent recording", async t => {
  const previous = globalThis.fetch; let writes = 0, allowed = true;
  globalThis.fetch = async () => { writes++; throw Error("must not write"); };
  t.after(() => { globalThis.fetch = previous; });
  const notifier = new FeishuNotifier({ appId: "test", appSecret: "test", canSend: () => allowed });
  notifier.getToken = async () => "token";
  await notifier.updateMessage("om_known", "latest", { onDelivery: async () => { allowed = false; } });
  assert.equal(writes, 0);
});

test("final_answer channel streams on the same card while its turn is still running", async t => {
  const f = fixture(t);
  let text = "a", status = "inProgress";
  f.manager.read = async () => ({ thread: { id: "thread" },
    turns: [{ id: "turn", status, items: [{ id: "item", type: "agentMessage", phase: "final_answer", text }] }] });
  f.manager.handoffPaused = false;
  await f.manager.scan();
  text = "ab"; await f.manager.scan();
  assert.equal(f.calls.length, 1); assert.ok(f.manager.publicTextTimer);
  text = "abc"; status = "completed"; await f.manager.scan();
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "PATCH"]);
  assert.equal(f.calls[1].text, "abc"); assert.equal(f.calls[1].id, "card-1");
});
test("upgrading a pre-stream snapshot baseline never replays historical replies", async t => {
  const f = fixture(t);
  const snapshot = { thread: { id: "thread" }, turns: [{ id: "turn", status: "completed",
    items: [{ id: "item", type: "agentMessage", phase: "final_answer", text: "old reply" }] }] };
  f.manager.baseline(f.session, snapshot); delete f.session.seen["turn/item"].final;
  f.manager.read = async () => snapshot; f.manager.handoffPaused = false;
  await f.manager.scan(); assert.equal(f.calls.length, 0);
});
test("a persisted accepted card id is reused even if the create response subsequently fails", async t => {
  const f = fixture(t);
  f.manager.notifier.sendMessage = async (text, opts) => {
    f.calls.push({ method: "POST", text, opts });
    await opts.onDelivery({ phase: "accepted", messageId: "accepted-card" });
    throw Error("response interrupted");
  };
  f.enqueue("a"); await f.flush();
  f.enqueue("ab", true); await f.flush();
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "PATCH"]);
  assert.equal(f.calls[1].id, "accepted-card");
});
