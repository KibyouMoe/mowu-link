import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { PROTOCOL } from "../desktop-manager.mjs";
import { InProcessTransport } from "../interface/inprocess.mjs";
import { nativeAnswersInSnapshot, projectPublicQuestion } from "../interface/public-questions.mjs";
import { dshFixture as fixture, png, sha256 } from "./fixtures/dsh-harness.mjs";
const tick = () => new Promise(resolve => setImmediate(resolve));

test("DSH business can dispatch its first message using complete snapshots of an empty original thread", async t => {
  const f = await fixture(t);
  assert.ok((await f.transport.listTools()).some(tool => tool.name === "wait_threads"));
  assert.ok(!(await f.adapter.listTools()).some(tool => tool.name === "wait_threads"));
  const registry = f.transport.ctx.get("agents"), create = registry.create;
  registry.create = async (...args) => { await delay(100); return create(...args); };
  const messageId = await f.send("first input");
  assert.equal(f.agent.status, "running");
  assert.equal(f.records.filter(event => event.type === "user/message").length, 1);
  assert.equal(f.manager.state.receipts[messageId].nativeDispatch.status, "accepted");
  assert.equal(f.transport.threads.size, 1);
  assert.equal(f.transport.threads.get(f.threadId).sessionId, "native-original");
  await f.finish();
});

test("DSH text deltas reach the shared Feishu outbox incrementally on stable cards and preserve both paragraphs", async t => {
  const f = await fixture(t); await f.send("stream test");
  f.stream("first"); await f.drain();
  assert.ok(f.calls.some(call => call.method === "POST" && call.text === "first"));
  assert.equal(f.transport.threads.get(f.threadId).turns[0].status, "inProgress");
  f.publish("agent/assistant-stream", { agent: f.agent, frame: { type: "chunk", attemptId: "attempt-1",
    chunk: { type: "text-delta", text: " paragraph" } } });
  await f.drain();
  assert.ok(f.calls.some(call => call.method === "PATCH" && call.text === "first paragraph"));
  f.emit("assistant/message", { message: { content: [{ type: "text", text: "first paragraph" }] } });
  f.stream("second paragraph", "attempt-2"); await f.drain();
  f.emit("assistant/message", { message: { content: [{ type: "text", text: "second paragraph" }] } });
  await f.finish(); await f.drain();
  const first = f.calls.filter(call => call.text === "first" || call.text === "first paragraph");
  assert.deepEqual(first.map(call => call.method), ["POST", "PATCH"]);
  assert.equal(first[0].id, first[1].id);
  const second = f.calls.filter(call => call.text === "second paragraph");
  assert.equal(second.filter(call => call.method === "POST").length, 1);
  assert.notEqual(first[0].id, second[0].id);
  assert.deepEqual(f.transport.readThread({ threadId: f.threadId }).turns[0].items
    .filter(item => item.type === "agentMessage").map(item => item.text), ["first paragraph", "second paragraph"]);
});

test("DSH native tool start and result reach the Feishu progress card with one stable tool identity", async t => {
  const f = await fixture(t); await f.send("tool test");
  f.emit("tool/call", { callId: "terminal-1", name: "bash", arguments: '{"command":"echo visible"}' });
  await f.drain();
  const turnId = f.transport.threads.get(f.threadId).turns[0].id;
  const toolKey = turnId + "/" + f.session.id + ":terminal-1", session = f.manager.session(f.threadId);
  assert.equal(session.toolSeen[toolKey].status, "running");
  f.emit("tool/result", { message: { toolCallId: "terminal-1", isError: false, content: [{ text: "private tool output" }] } });
  await f.drain();
  assert.equal(session.toolSeen[toolKey].status, "completed");
  assert.deepEqual(f.manager.state.events.filter(event => event.type === "tool.activity")
    .map(event => event.payload.activity.status), ["running", "completed"]);
  assert.ok(f.calls.some(call => call.text.includes("echo visible")));
  assert.ok(!JSON.stringify(f.calls).includes("private tool output"));
  await f.finish();
});

test("DSH discovers scoped compaction after first agent creation and preserves native identity and receipts", async t => {
  const f = await fixture(t);
  assert.ok(!f.manager.tools.some(tool => tool.name === "compact_thread"));
  const messageId = await f.send("remember input"); await f.finish(); await f.drain();
  assert.ok(f.manager.tools.some(tool => tool.name === "compact_thread"));
  const receipt = structuredClone(f.manager.state.receipts[messageId]);
  await f.manager.message({ protocolVersion: PROTOCOL, messageId: randomUUID(), source: "trainer",
    targetThreadId: f.threadId, text: "/compact" });
  await f.drain(); await tick(); await f.drain();
  assert.equal(f.manager.session(f.threadId).contextState.compaction.status, "completed");
  assert.equal(f.transport.threads.get(f.threadId).sessionId, "native-original");
  assert.equal(f.transport.threads.get(f.threadId).turns.length, 1);
  assert.deepEqual(f.manager.state.receipts[messageId], receipt);
  assert.ok(!JSON.stringify(f.calls).includes("private native summary"));
});

test("a failed native resume reaches Feishu as an explicit error and retains the original session", async t => {
  const f = await fixture(t); await f.send("initial task"); await f.finish();
  f.transport.live.clear();
  const registry = f.transport.ctx.get("agents");
  registry.resume = async () => { throw new Error("session not found"); };
  registry.create = async () => { assert.fail("A failed resume cannot create another native session."); };
  await f.send("resume original"); await f.drain();
  const thread = f.transport.threads.get(f.threadId);
  assert.equal(thread.sessionId, "native-original");
  assert.equal(thread.turns.at(-1).status, "failed");
  assert.ok(f.calls.some(call => call.text.includes("原会话恢复失败") && call.text.includes("native-original")));
  assert.equal(f.transport.threads.size, 1);
});

test("a DSH clarification card accepts a Feishu option and fills the original native question", async t => {
  const f = await fixture(t); await f.send("question test");
  const pending = f.transport.questions.request({ agent: f.agent, wait: { callId: "question-1" },
    signal: new AbortController().signal,
    questions: [{ id: "choice", question: "Continue?", options: [{ label: "Yes" }, { label: "No" }] }] },
  () => Promise.reject(Object.assign(new Error("no native desktop"), { code: "NO_PROVIDER" })));
  await f.drain();
  const question = Object.values(f.manager.state.publicQuestions)[0];
  const action = f.manager.questions.state.entries[question.questionId];
  assert.deepEqual(question.questions, [{ title: "Continue?", options: ["Yes", "No"] }]);
  assert.equal(f.cards.length, 1);
  await f.manager.questions.acceptCard({ header: { event_type: "card.action.trigger", event_id: "dsh-option-click" },
    event: { operator: { open_id: "trainer" }, context: { open_message_id: action.messageId, open_chat_id: "chat" },
      action: { value: { bridgeQuestion: question.questionId, nonce: action.nonce,
        action: "form_select", questionIndex: 0, optionIndex: 0 }, form_value: {} } } });
  await f.manager.questions.process();
  assert.deepEqual((await pending).answers, [{ id: "choice", selected: ["Yes"] }]);
  await f.drain();
  assert.deepEqual(nativeAnswersInSnapshot(question, f.transport.readThread({ threadId: f.threadId })), ["Yes"]);
  assert.equal(f.transport.threads.get(f.threadId).turns.length, 1);
  assert.equal(f.manager.questions.get(question.questionId).status, "answered");
  await f.finish();
});

test("reloading during a question retains its native identity and clears the interrupted live wait", async t => {
  const f = await fixture(t); await f.send("question before reload");
  const controller = new AbortController();
  const pending = f.transport.questions.request({ agent: f.agent, wait: { callId: "persisted-question" },
    signal: controller.signal, questions: [{ id: "persisted-choice", question: "Continue?" }] },
  () => Promise.reject(Object.assign(new Error("no native desktop"), { code: "NO_PROVIDER" })));
  try {
    await f.transport.save();
    const loaded = Object.create(InProcessTransport.prototype);
    loaded.stateFile = f.transport.stateFile; loaded.threads = new Map(); loaded.load();
    const restored = loaded.threads.get(f.threadId);
    assert.equal(restored.sessionId, "native-original");
    assert.equal(restored.turns[0].status, "interrupted");
    assert.equal(restored.turns[0].waitingForInput, false);
    const identity = restored.turns[0].nativeQuestions[0].dshQuestion;
    assert.equal(identity.sessionId, "native-original");
    assert.equal(identity.callId, "persisted-question");
    assert.deepEqual(identity.ids, ["persisted-choice"]);
    assert.equal(identity.threadId, f.threadId);
    assert.equal(identity.sourceTurnId, restored.turns[0].id);
  } finally {
    controller.abort(new Error("test finished")); await assert.rejects(pending);
  }
  await f.finish();
});

test("DSH Feishu image ingress preserves original bytes and intermediate public images relay once", async t => {
  const f = await fixture(t);
  const received = await f.manager.receiveFeishu({ message_id: "om_dsh_image", message_type: "image", open_id: "trainer",
    chat_type: "p2p", sender_type: "user", chat_id: "chat", content: JSON.stringify({ image_key: "img_dsh_image" }) });
  await tick();
  const imagePath = f.manager.state.feishuReceipts[received.messageId].imagePath;
  assert.equal(sha256(await readFile(imagePath)), sha256(png));
  assert.ok(f.transport.threads.get(f.threadId).turns[0].prompt.includes(imagePath));
  const output = path.join(f.root, "displayed-output.png"); await writeFile(output, png);
  f.stream(`![test](<${output}>)`, "image-attempt"); await f.drain();
  f.emit("assistant/message", { message: { content: [{ type: "text", text: `![test](<${output}>)` }] } });
  f.stream("final reply", "final-attempt"); await f.finish(); await f.drain(); await f.drain();
  assert.equal(f.imageSends.length, 1);
  assert.equal(f.imageSends[0].sha256, sha256(png));
  assert.equal(f.imageSends[0].bytes, png.length);
  assert.equal(f.transport.threads.get(f.threadId).sessionId, "native-original");
});

test("loading legacy questions anchors the original public UUID and retains the existing card and nonce", async t => {
  const f = await fixture(t); await f.send("legacy question");
  const turn = f.transport.threads.get(f.threadId).turns[0];
  const item = { type: "agentMessage", kind: "assistant", phase: "commentary", delivery: "async", id: "legacy-question",
    text: "Continue?", questions: [{ title: "Continue?", options: ["Yes", "No"] }],
    dshQuestion: { sessionId: f.session.id, callId: "legacy-call", ids: ["choice"] } };
  turn.nativeQuestions = [item];
  const original = projectPublicQuestion(item, { threadId: f.threadId, turnId: turn.id });
  await f.drain(); await f.transport.save();
  const action = structuredClone(f.manager.questions.state.entries[original.questionId]);
  const loaded = Object.create(InProcessTransport.prototype);
  loaded.stateFile = f.transport.stateFile; loaded.threads = new Map(); loaded.load();
  const restored = loaded.threads.get(f.threadId).turns[0].nativeQuestions[0];
  assert.deepEqual(projectPublicQuestion(restored, { threadId: f.threadId, turnId: "new-wrapper" }), original);
  await f.manager.questions.publish(projectPublicQuestion(restored, { threadId: f.threadId, turnId: "new-wrapper" }));
  assert.equal(f.questionPosts.length, 1);
  assert.equal(f.manager.questions.state.entries[original.questionId].nonce, action.nonce);
  assert.equal(f.manager.questions.state.entries[original.questionId].messageId, action.messageId);
  await f.finish();
});

test("legacy media projections retain their old IDs and cannot replay historical relative images", async t => {
  const f = await fixture(t); await f.send("legacy media");
  const turn = f.transport.threads.get(f.threadId).turns[0];
  turn.publicMessages = [{ id: "legacy-output", text: "![old](evidence/old.png)" }];
  const output = f.transport.readThread({ threadId: f.threadId }).turns[0].items.filter(item => item.type === "agentMessage");
  assert.equal(output.length, 1); assert.equal(output[0].id, "legacy-output");
  assert.equal(output[0].text, "![old](evidence/old.png)");
  await f.drain(); assert.equal(f.imageSends.length, 0);
  await f.finish();
});

test("partial DSH MEDIA output stays pending until committed, then sends once with surrounding prose separate", async t => {
  const f = await fixture(t); await f.send("stream media");
  const filename = path.join(f.root, "streamed.png"); await writeFile(filename, png);
  f.stream("before\nMED"); await f.drain();
  assert.equal(f.imageSends.length, 0);
  assert.ok(f.calls.every(call => !call.text.includes("MED")));
  f.publish("agent/assistant-stream", { agent: f.agent, frame: { type: "chunk", attemptId: "attempt-1",
    chunk: { type: "text-delta", text: "IA: streamed.png" } } });
  await f.drain(); assert.equal(f.imageSends.length, 0);
  const text = "before\nMEDIA: streamed.png\nafter";
  f.emit("assistant/message", { message: { content: [{ type: "text", text }] } });
  await f.drain(); await f.drain();
  assert.equal(f.imageSends.length, 1);
  assert.ok(f.calls.some(call => call.text.includes("before")));
  assert.ok(f.calls.some(call => call.text.includes("after")));
  assert.ok(f.calls.every(call => !call.text.includes("MEDIA:")));
  await f.finish();
});
