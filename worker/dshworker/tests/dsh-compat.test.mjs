import test from "node:test";
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { DesktopManager, PROTOCOL } from "../desktop-manager.mjs";
import { InProcessTransport } from "../interface/inprocess.mjs";
import { createInProcessAdapter } from "../interface/transport/inprocess.mjs";
import { registerNativeTransport, resetNativeTransportCache } from "../interface/transport/index.mjs";
import { nativeToolRejection } from "../interface/native-dispatch.mjs";
import { nativeAnswersInSnapshot } from "../interface/public-questions.mjs";

const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1WkAAAAASUVORK5CYII=", "base64");
const sha256 = bytes => createHash("sha256").update(bytes).digest("hex");
const tick = () => new Promise(resolve => setImmediate(resolve));

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "dsh-business-compat-"));
  const listeners = new Map(), records = [], waiters = [], calls = [], cards = [], imageSends = [];
  const session = { id: "native-original", seq: 0, eventAt: index => records[index] };
  const questions = { continued: () => [], answer: () => false };
  const compaction = { async compactNow(subject) {
    assert.equal(subject, agent);
    emit("compaction/start", { compactionId: "manual-compaction" });
    emit("compaction/summary", { summary: "private native summary" });
    emit("compaction/end", { compactionId: "manual-compaction" });
    return { compactionId: "manual-compaction" };
  } };
  const agent = { id: session.id, session, status: "idle",
    ctx: { get: name => name === "compaction" ? compaction : name === "userQuestions" ? questions : null },
    followup(message) { this.status = "running"; emit("turn/start", {}); emit("user/message", message); },
    steer(message) { emit("user/message", message); },
    whenIdle: () => agent.status === "idle" ? Promise.resolve() : new Promise(resolve => waiters.push(resolve)),
    cancel() { finish({ kind: "user" }); },
  };
  const handle = { agent, dispose: async () => { if (agent.status !== "idle") finish({ kind: "user" }); } };
  const registry = { list: () => [agent], create: async () => handle,
    resume: async ({ resumeSessionId }) => { assert.equal(resumeSessionId, session.id); return handle; } };
  const ctx = {
    get: name => ({ agents: registry, sessions: { flush: async () => {} }, userQuestions: questions })[name],
    on(name, listener) {
      const set = listeners.get(name) ?? new Set(); set.add(listener); listeners.set(name, set);
      return () => set.delete(listener);
    },
  };
  function publish(name, ...args) { for (const listener of listeners.get(name) ?? []) listener(...args); }
  function emit(type, data) {
    const event = { seq: session.seq++, type, data }; records.push(event); publish("session/event", session, event);
  }
  function stream(text, attemptId = "attempt-1") {
    publish("agent/assistant-stream", { agent, frame: { type: "start", attemptId } });
    publish("agent/assistant-stream", { agent, frame: { type: "chunk", attemptId, chunk: { type: "text-delta", text } } });
  }
  function finish(reason = { kind: "completed" }) {
    emit("turn/end", { reason }); agent.status = "idle";
    for (const resolve of waiters.splice(0)) resolve();
  }
  const transport = new InProcessTransport({ ctx, selection: {}, cwd: root, stateDir: root, log() {} });
  const threadId = transport.createThread({ cwd: root, title: "original main" }).threadId;
  const adapter = createInProcessAdapter(transport), previous = process.env.CODEXWORKER_NATIVE_TRANSPORT;
  const transportName = "dsh-compat-" + randomUUID();
  registerNativeTransport(transportName, adapter);
  process.env.CODEXWORKER_NATIVE_TRANSPORT = transportName; resetNativeTransportCache();
  const manager = await new DesktopManager({ stateDir: root,
    config: { token: "isolated-dsh-compat-token", projectId: "compat", workspaceRoots: [root],
      nativeSenderThreadId: threadId, nativeBusyDeliveryVerified: true, pollMs: 1000000,
      operatingMode: "feishu-local", bridgeName: "玉藻十字", messageSourceLabels: false,
      feishu: { transcriptEnabled: true, streamEnabled: true, streamUpdateIntervalMs: 250,
        toolProgressEnabled: true, trainerOpenIdWhitelist: ["trainer"] } },
    listNativeTools: () => adapter.listTools(),
    native: async (name, args) => {
      const response = await adapter.callTool(name, args);
      if (!response.success) throw nativeToolRejection(name, response);
      return JSON.parse(response.contentItems[0].text);
    },
  }).initialize();
  await manager.register({ protocolVersion: PROTOCOL, messageId: randomUUID(), source: "trainer", threadId });
  manager.session(threadId).feishuChatId = "chat";
  manager.notifier = { enabled: true, chatId: "chat", addReaction: async () => null, removeReaction: async () => true,
    sendMessage: async (text, options) => {
      const id = "card-" + (calls.length + 1); calls.push({ method: "POST", id, text, options });
      return { code: 0, data: { message_id: id } };
    },
    updateMessage: async (id, text, options) => {
      calls.push({ method: "PATCH", id, text, options }); return { code: 0, data: { message_id: id } };
    },
    sendQuestionCard: async card => { cards.push(card); return { code: 0, data: { message_id: "question-card" } }; },
    updateQuestionCard: async (_id, card) => { cards.push(card); return { code: 0 }; },
    downloadImage: async () => png,
  };
  manager.sendImage = async (request, validate) => {
    validate(); const bytes = await readFile(request.imagePath);
    imageSends.push({ ...request, sha256: sha256(bytes), bytes: bytes.length });
    return { feishuMessageId: "image-card", sha256: sha256(bytes) };
  };
  transport.control.attachManager(manager);
  async function drain() {
    await manager.scan(); await manager.tail;
    for (const entry of Object.values(manager.state.publicTextOutbox ?? {})) entry.lastAttemptMs = 0;
    await manager.drainPublicTexts(); await manager.tail;
    clearTimeout(manager.publicTextTimer); manager.publicTextTimer = null;
  }
  async function send(text) {
    const messageId = randomUUID();
    await manager.message({ protocolVersion: PROTOCOL, messageId, source: "trainer", targetThreadId: threadId, text });
    await tick(); return messageId;
  }
  t.after(async () => {
    if (agent.status !== "idle") { finish(); await tick(); }
    await manager.close(); await transport.close();
    if (previous == null) delete process.env.CODEXWORKER_NATIVE_TRANSPORT;
    else process.env.CODEXWORKER_NATIVE_TRANSPORT = previous;
    resetNativeTransportCache(); await rm(root, { recursive: true, force: true });
  });
  return { root, manager, transport, adapter, agent, threadId, session, cards, calls, imageSends,
    emit, publish, stream, finish: async () => { finish(); await tick(); }, drain, send };
}

test("DSH business can dispatch its first message using complete snapshots of an empty original thread", async t => {
  const f = await fixture(t);
  assert.ok((await f.transport.listTools()).some(tool => tool.name === "wait_threads"));
  assert.ok(!(await f.adapter.listTools()).some(tool => tool.name === "wait_threads"));
  const messageId = await f.send("first input");
  assert.equal(f.manager.state.receipts[messageId].nativeDispatch.status, "accepted");
  assert.equal(f.transport.threads.size, 1);
  assert.equal(f.transport.threads.get(f.threadId).sessionId, "native-original");
  await f.finish();
});

test("DSH text deltas reach the shared Feishu outbox incrementally on stable cards and preserve both paragraphs", async t => {
  const f = await fixture(t); await f.send("stream test");
  f.stream("first"); await f.drain();
  assert.equal(f.transport.threads.get(f.threadId).turns[0].status, "inProgress");
  f.publish("agent/assistant-stream", { agent: f.agent, frame: { type: "chunk", attemptId: "attempt-1",
    chunk: { type: "text-delta", text: " paragraph" } } });
  await f.drain();
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
    assert.deepEqual(restored.turns[0].nativeQuestions[0].dshQuestion,
      { sessionId: "native-original", callId: "persisted-question", ids: ["persisted-choice"] });
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
