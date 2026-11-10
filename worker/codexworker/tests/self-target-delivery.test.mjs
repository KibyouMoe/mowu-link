import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, appendFile, readFile, rm } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { randomUUID } from "node:crypto";
import { DesktopManager, PROTOCOL } from "../desktop-manager.mjs";
import { nativeStatusInvocation } from "../interface/native-dispatch.mjs";

const id = "01a17cde-aa62-7dd2-b27d-a68088b163c2";
const line = value => JSON.stringify(value) + "\n";
const lifecycle = (type, turn = "turn-a") => ({ type: "event_msg", payload: { type, turn_id: turn } });
const request = (text = "hello", extra = {}) => ({ protocolVersion: PROTOCOL, messageId: randomUUID(), source: "trainer", text, ...extra });

async function fixture(t, { otherContext = false, verified = true, refreshCaller = false } = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "bridge-self-target-"));
  const sessions = path.join(root, "sessions"), folder = path.join(sessions, "2026", "10", "28");
  await mkdir(folder, { recursive: true });
  const file = path.join(folder, `rollout-test-${id}.jsonl`);
  await writeFile(file, line({ type: "session_meta", payload: { id, cwd: root } }));
  const callerId = "01a17cde-aa62-7dd2-b27d-a68088b163c3", callerFile = path.join(folder, `rollout-test-${callerId}.jsonl`);
  if (refreshCaller) await writeFile(callerFile, line({ type: "session_meta", payload: { id: callerId, cwd: root } }));
  const calls = [], state = { status: "inProgress", turn: "turn-a", sendError: false };
  let rejectHistory = false;
  const config = { token: "isolated-self-test-token-never-production-123456", port: 0, pollMs: 1000000, workspaceRoots: [root],
    nativeSenderThreadId: otherContext ? callerId : id, nativeSendFromTarget: true,
    nativeBusyDeliveryVerified: verified, nativeStateIndependentDeliveryVerified: true,
    publicJournalRoot: sessions, messageSourceLabels: false };
  const native = async (name, args, context) => {
    calls.push({ name, args, context });
    if (name === "read_thread") {
      if (rejectHistory) throw new Error("Thread history exceeds the read size limit.");
      return { thread: { id, cwd: root, title: "self test", hostId: "local", status: { type: "active" } }, turns: [] };
    }
    if (name === "wait_threads") {
      assert.equal(otherContext, true, "wait_threads must never target its real caller");
      if (refreshCaller) {
        try { state.invocation = await nativeStatusInvocation({ managerThreadId: callerId, managerTurnId: "saved-old-turn" }, manager, { threadId: callerId, cwd: root }); }
        catch (error) { error.nativeStatusPreflight = true; throw error; }
      }
      state.waitAttempts = (state.waitAttempts ?? 0) + 1;
      if (state.waitError) { const error = new Error("native status rejected"); error.nativeReachable = true; throw error; }
      return { polls: [{ thread: { id, hostId: "local" }, latestTurn: { id: state.turn, status: state.status } }] };
    }
    if (name === "send_message_to_thread") {
      assert.equal(args.threadId, id); assert.equal(context.sourceThreadId, id);
      if (state.sendError) throw new Error("native write timeout");
      return { threadId: id };
    }
    throw new Error("unexpected native tool");
  };
  const tools = async () => ["read_thread", "wait_threads", "send_message_to_thread", "create_thread", "fork_thread", "set_thread_title", "list_threads"].map(name => ({ name }));
  const manager = new DesktopManager({ native, listNativeTools: tools, stateDir: root, config });
  t.after(async () => { await manager.close(); await rm(root, { recursive: true, force: true }); });
  await manager.initialize();
  await manager.listen();
  await manager.register({ protocolVersion: PROTOCOL, messageId: randomUUID(), source: "trainer", threadId: id });
  rejectHistory = true; calls.length = 0;
  return { root, file, manager, calls, state,
    callerId, appendCaller: value => appendFile(callerFile, line(value)),
    append: value => appendFile(file, line(value)), writes: () => calls.filter(c => c.name === "send_message_to_thread") };
}

for (const busy of [false, true]) test(`self-target native contract accepts ${busy ? "busy" : "idle"} follow-up without history or self wait`, async t => {
  const f = await fixture(t); await f.append(lifecycle("task_started"));
  if (!busy) await f.append(lifecycle("task_complete"));
  const input = request();
  assert.equal((await f.manager.message(input)).accepted, true);
  assert.equal((await f.manager.message(input)).duplicate, true);
  assert.deepEqual(f.calls.map(c => c.name), ["send_message_to_thread"]);
  assert.equal(f.manager.state.receipts[input.messageId].nativeDispatch.status, "accepted");
  assert.equal(f.manager.session().pendingInputs.length, 0);
});

test("another manager context accepts verified ordinary follow-up without native state or history", async t => {
  const f = await fixture(t, { otherContext: true });
  await f.manager.message(request());
  assert.deepEqual(f.calls.map(c => c.name), ["send_message_to_thread"]);
});

test("native status keeps the real manager identity but follows new lifecycle turns instead of the saved binding turn", async t => {
  const f = await fixture(t, { otherContext: true, refreshCaller: true });
  await f.appendCaller(lifecycle("task_started", "caller-current"));
  assert.equal((await f.manager.deliveryState(f.manager.session())).deliveryObservation.turnId, "turn-a");
  assert.deepEqual(f.state.invocation, { threadId: f.callerId, turnId: "caller-current" });
  await f.appendCaller(lifecycle("task_complete", "caller-current"));
  await f.appendCaller(lifecycle("task_started", "caller-next"));
  await f.manager.deliveryState(f.manager.session());
  assert.equal(f.state.invocation.turnId, "caller-next");
  await f.manager.save();
  const saved = JSON.parse(await readFile(f.manager.statePath, "utf8"));
  assert.equal(saved.nativeStatusCaller.taskJournalCheckpoint.taskState.turnId, "caller-next");
});

test("unavailable caller context uses verified target lifecycle and cannot release a busy or partial-record queue", async t => {
  const f = await fixture(t, { otherContext: true, refreshCaller: true });
  await f.append(lifecycle("task_started"));
  const input = request("queue", { delivery: "queue" }); await f.manager.message(input);
  await f.manager.scan(); assert.equal(f.writes().length, 0); assert.equal(f.state.waitAttempts ?? 0, 0);
  assert.equal(f.manager.connected, true, "local preflight failure must not mark the native transport lost");
  const end = line(lifecycle("task_complete")); await appendFile(f.file, end.slice(0, -2));
  await f.manager.scan(); assert.equal(f.writes().length, 0);
  await appendFile(f.file, end.slice(-2));
  await f.manager.scan(); assert.equal(f.writes().length, 1);
  assert.equal(f.manager.state.receipts[input.messageId].nativeDispatch.status, "accepted");
});

test("a read-only native status rejection uses target lifecycle but never invents an idle target", async t => {
  const f = await fixture(t, { otherContext: true, refreshCaller: true });
  await f.appendCaller(lifecycle("task_started", "real-caller")); f.state.waitError = true;
  await assert.rejects(f.manager.deliveryState(f.manager.session()), /execution state is unknown/);
  await f.append(lifecycle("task_started", "target-running"));
  const observed = await f.manager.deliveryState(f.manager.session());
  assert.deepEqual(observed.deliveryObservation, { turnId: "target-running", turnStatus: "inProgress", source: "desktop-task-journal" });
  await assert.rejects(f.manager.message(request("answer"), { requireSourceTurnId: "old-target" }), /original question turn/);
  assert.equal(f.writes().length, 0);
});

test("self-target unverified busy delivery is refused; real completed lifecycle permits idle delivery", async t => {
  const f = await fixture(t, { verified: false });
  await f.append(lifecycle("task_started"));
  await assert.rejects(f.manager.message(request()), /Busy native delivery/);
  await f.append(lifecycle("task_complete"));
  await f.manager.message(request("idle")); assert.equal(f.writes().length, 1);
  assert.equal(f.calls.some(c => c.name === "read_thread" || c.name === "wait_threads"), false);
});

test("unknown/open sidebar status and a partial terminal event cannot release a queue", async t => {
  const f = await fixture(t);
  await f.manager.message(request("queued", { delivery: "queue" }));
  await f.manager.scan(); assert.equal(f.writes().length, 0);
  await f.append(lifecycle("task_started"));
  const end = line(lifecycle("task_complete")); await appendFile(f.file, end.slice(0, -1));
  await f.manager.scan(); assert.equal(f.writes().length, 0);
  await appendFile(f.file, "\n"); await f.manager.scan();
  assert.equal(f.writes().length, 1); assert.equal(f.manager.session().queue.length, 0);
});

test("queue waits for real idle even when stale transcript correlation remains pending", async t => {
  const f = await fixture(t); await f.append(lifecycle("task_started"));
  f.manager.session().pendingInputs.push({ messageId: "old-unknown", unknownOutcome: true });
  await f.manager.message(request("later", { delivery: "queue" }));
  await f.manager.scan(); assert.equal(f.writes().length, 0);
  await f.append(lifecycle("task_complete")); await f.manager.scan();
  assert.equal(f.writes().length, 1); assert.equal(f.manager.session().queue.length, 0);
});

test("a new task starting after queue intent persistence defers without losing order or sending", async t => {
  const f = await fixture(t); await f.append(lifecycle("task_started")); await f.append(lifecycle("task_complete"));
  const queued = request("later", { delivery: "queue" }); await f.manager.message(queued);
  const save = f.manager.save.bind(f.manager); let raced = false;
  f.manager.save = async () => { await save(); if (!raced && f.manager.state.receipts[queued.messageId]?.nativeDispatch?.status === "dispatching") {
    raced = true; await f.append(lifecycle("task_started", "turn-b"));
  } };
  await f.manager.scan(); assert.equal(f.writes().length, 0); assert.equal(f.manager.session().queue[0].messageId, queued.messageId);
  await f.append(lifecycle("task_complete", "turn-b")); await f.manager.scan(); assert.equal(f.writes().length, 1);
});

test("a specified question turn uses fresh self lifecycle state and rejects stale/ended sources", async t => {
  const f = await fixture(t); await f.append(lifecycle("task_started"));
  await f.manager.message(request("answer"), { requireSourceTurnId: "turn-a" });
  assert.equal(f.writes().length, 1);
  const q = { threadId: id }, s = { bindingEpoch: f.manager.state.selectionEpoch, sourceTurnId: "turn-a" };
  assert.deepEqual(await f.manager.questions.sourceState(q, s), { active: true });
  await f.append(lifecycle("task_complete"));
  await assert.rejects(f.manager.message(request("late answer"), { requireSourceTurnId: "turn-a" }), /original question turn/);
  assert.equal((await f.manager.questions.sourceState(q, s)).expired, true);
  assert.equal(f.calls.some(c => c.name === "read_thread" || c.name === "wait_threads"), false);
});

test("question completion racing with persisted intent cancels before native write", async t => {
  const f = await fixture(t); await f.append(lifecycle("task_started")); const input = request("answer");
  const save = f.manager.save.bind(f.manager); let raced = false;
  f.manager.save = async () => { await save(); if (!raced && f.manager.state.receipts[input.messageId]?.nativeDispatch?.status === "dispatching") {
    raced = true; await f.append(lifecycle("task_complete"));
  } };
  await assert.rejects(f.manager.message(input, { requireSourceTurnId: "turn-a" }), /question turn ended/);
  assert.equal(f.writes().length, 0); assert.equal(f.manager.state.receipts[input.messageId].nativeDispatch.status, "canceled");
});

test("an ABA binding change after intent persistence cancels ordinary native follow-up", async t => {
  const f = await fixture(t); const input = request(); const save = f.manager.save.bind(f.manager); let raced = false;
  f.manager.save = async () => { await save(); if (!raced && f.manager.state.receipts[input.messageId]?.nativeDispatch?.status === "dispatching") {
    raced = true; f.manager.state.selectionEpoch += 2;
  } };
  await assert.rejects(f.manager.message(input), /binding changed before native dispatch/);
  assert.equal(f.writes().length, 0);
});

test("unknown self-target writes remain durable and duplicate incoming messages never resend", async t => {
  const f = await fixture(t); f.state.sendError = true;
  const event = { message_id: "same-Feishu-message", chat_type: "p2p", sender_type: "user", message_type: "text",
    content: "uncertain", open_id: "trainer", chat_id: "chat" };
  f.manager.config.feishu.trainerOpenIdWhitelist = ["trainer"];
  await assert.rejects(f.manager.receiveFeishu(event)); await assert.rejects(f.manager.receiveFeishu(event));
  assert.equal(f.writes().length, 1);
  const saved = JSON.parse(await readFile(f.manager.statePath, "utf8"));
  assert.equal(Object.values(saved.receipts).find(r => r.request.text === "uncertain").nativeDispatch.status, "unknown");
});

test("self-target public text and image journal sync remains incremental and sends each output once", async t => {
  const f = await fixture(t); await f.append(lifecycle("task_started"));
  const item = value => ({ timestamp: new Date().toISOString(), type: "event_msg", payload: { type: "item_completed", thread_id: id, turn_id: "turn-a", item: value } });
  await f.append(item({ type: "AgentMessage", id: "public-message", phase: "commentary", content: [{ type: "Text", text: "公开进度" }] }));
  await f.append(item({ type: "ImageView", id: "public-image", path: path.join(f.root, "public.png") }));
  const texts = [], images = [];
  f.manager.notifier = { enabled: true, sendMessage: async text => { texts.push(text); return { code: 0 }; } };
  f.manager.sendImage = async body => { images.push(body); return { feishuMessageId: "sent" }; };
  await f.manager.scan(); await Promise.all([...f.manager.backgroundTasks]);
  await f.manager.scan(); await Promise.all([...f.manager.backgroundTasks]);
  assert.deepEqual(texts, ["公开进度"]); assert.equal(images.length, 1);
  assert.equal(f.manager.session().relayStatus.mode, "desktop-public-journal");
  assert.equal(f.manager.session().relayStatus.journal.pendingBytes, 0);
  assert.equal(f.calls.some(c => c.name === "wait_threads" || c.name === "read_thread"), false);
});

async function typingFixture(t, options = {}) {
  const f = await fixture(t, options), reactions = [], sent = [];
  await f.append(lifecycle("task_started"));
  f.manager.config.feishu.trainerOpenIdWhitelist = ["trainer"];
  f.manager.notifier = { enabled: true,
    addReaction: async messageId => { reactions.push(["add", messageId]); return "reaction-" + messageId; },
    removeReaction: async messageId => { reactions.push(["remove", messageId]); return true; },
    sendMessage: async text => { sent.push(text); return { code: 0 }; }
  };
  const receive = messageId => f.manager.receiveFeishu({ message_id: messageId, chat_type: "p2p", sender_type: "user",
    message_type: "text", content: messageId, open_id: "trainer", chat_id: "oc_test" });
  const output = (messageId, text, timestamp = new Date().toISOString()) => f.append({ timestamp, type: "event_msg",
    payload: { type: "item_completed", thread_id: id, turn_id: "turn-a",
      item: { type: "AgentMessage", id: messageId, phase: "commentary", content: [{ type: "Text", text }] } } });
  const settle = async () => { await Promise.all([...f.manager.backgroundTasks, ...f.manager.feishuReactionTasks.values()]); await f.manager.tail; };
  return { ...f, reactions, sent, receive, output, settle };
}

for (const otherContext of [false, true]) test(`accepted ${otherContext ? "other-context" : "self-target"} input keeps Typing until its first new public card succeeds`, async t => {
  const f = await typingFixture(t, { otherContext });
  await f.output("old-card", "积压的旧进度");
  await new Promise(resolve => setTimeout(resolve, 3));
  const input = await f.receive("typing-input"); await f.settle();
  assert.equal(f.manager.state.feishuReceipts[input.messageId].cleanupRequested, false);
  assert.deepEqual(f.reactions, [["add", "typing-input"]]);
  await f.manager.scan(); await f.settle();
  assert.deepEqual(f.sent, ["积压的旧进度"]); assert.equal(f.reactions.length, 1);
  await f.output("new-card", "这条新输入已收到");
  await f.manager.scan(); await f.settle();
  assert.deepEqual(f.reactions, [["add", "typing-input"], ["remove", "typing-input"]]);
  assert.equal(f.manager.state.receipts[input.messageId].nativeDispatch.status, "accepted");
  assert.equal(f.manager.session().pendingInputs.length, 0, "Typing must not gate native ingress or queue execution");
  assert.equal(f.writes().length, 1);
});

test("an in-flight older card cannot clear an input received after that card was created", async t => {
  const f = await typingFixture(t); let release, started = false;
  await f.receive("first-typing"); await f.settle();
  f.manager.notifier.sendMessage = async () => { started = true; await new Promise(resolve => { release = resolve; }); return { code: 0 }; };
  await f.output("first-card", "第一条回复"); await f.manager.scan();
  try {
    for (let i = 0; !started && i < 100; i++) await new Promise(resolve => setTimeout(resolve, 1));
    assert.equal(started, true);
    await f.receive("second-typing"); await Promise.all([...f.manager.feishuReactionTasks.values()]);
    assert.deepEqual(f.reactions.filter(([kind]) => kind === "remove"), []);
  } finally { release?.(); }
  await f.settle();
  assert.deepEqual(f.reactions.filter(([kind]) => kind === "remove"), [["remove", "first-typing"]]);
  f.manager.notifier.sendMessage = async () => ({ code: 0 });
  await f.output("second-card", "第二条回复"); await f.manager.scan(); await f.settle();
  assert.deepEqual(f.reactions.filter(([kind]) => kind === "remove"), [["remove", "first-typing"], ["remove", "second-typing"]]);
});

test("unknown card delivery keeps Typing and its original output UUID without resending", async t => {
  const f = await typingFixture(t); const input = await f.receive("unknown-card-input"); await f.settle();
  let writes = 0; f.manager.notifier.sendMessage = async () => { writes++; throw new Error("unknown outcome"); };
  await f.output("failed-card", "发送未确认"); await f.manager.scan(); await f.settle();
  await f.manager.scan(); await f.settle();
  assert.equal(writes, 1); assert.equal(f.manager.state.feishuReceipts[input.messageId].cleanupRequested, false);
  assert.equal(Object.values(f.manager.state.publicTextOutbox).filter(e => e.status === "unknown").length, 1);
  assert.deepEqual(f.reactions, [["add", "unknown-card-input"]]);
});

test("late-created Typing is removed after successful output, and the wait survives persisted state", async t => {
  const f = await typingFixture(t); let completeAdd; const removed = [];
  f.manager.notifier.addReaction = () => new Promise(resolve => { completeAdd = resolve; });
  f.manager.notifier.removeReaction = async messageId => { removed.push(messageId); return true; };
  const input = await f.receive("late-typing");
  try {
    const saved = JSON.parse(await readFile(f.manager.statePath, "utf8"));
    assert.equal(saved.feishuReceipts[input.messageId].cleanupRequested, false);
    assert.equal(saved.feishuReceipts[input.messageId].awaitingPublicOutput.threadId, id);
    await f.output("late-card", "新的公开回复"); await f.manager.scan();
    await Promise.all([...f.manager.backgroundTasks]);
    assert.equal(f.manager.state.feishuReceipts[input.messageId].cleanupRequested, true);
  } finally { completeAdd?.("late-reaction"); }
  await f.settle(); assert.deepEqual(removed, ["late-typing"]);
});

test("a restarted business worker retains the Typing wait without dispatching the native input again", async t => {
  const f = await typingFixture(t), input = await f.receive("restart-typing"); await f.settle();
  await f.manager.close();
  const restored = await new DesktopManager({ native: f.manager.native, listNativeTools: f.manager.listNativeTools,
    stateDir: f.root, config: f.manager.config }).initialize();
  try {
    restored.notifier = f.manager.notifier;
    assert.equal(restored.state.feishuReceipts[input.messageId].cleanupRequested, false);
    await f.output("after-restart-card", "重载之后的回复");
    await restored.scan(); await Promise.all([...restored.backgroundTasks]); await restored.tail;
    assert.deepEqual(f.reactions, [["add", "restart-typing"], ["remove", "restart-typing"]]);
    assert.equal(f.writes().length, 1);
  } finally { await restored.close(); }
});

test("real terminal cleanup waits for an observed running turn and cannot pass an unknown output", async t => {
  const f = await typingFixture(t), input = await f.receive("terminal-typing"); await f.settle();
  await f.manager.scan(); await f.settle();
  assert.equal(f.manager.state.feishuReceipts[input.messageId].cleanupRequested, false);
  f.manager.notifier.sendMessage = async () => { throw new Error("unknown send"); };
  await f.output("unknown-terminal-card", "终态之前未确认的回复");
  await f.append(lifecycle("task_complete")); await f.manager.scan(); await f.settle();
  assert.equal(f.manager.state.feishuReceipts[input.messageId].cleanupRequested, false);
  assert.deepEqual(f.reactions, [["add", "terminal-typing"]]);
});

test("an interrupted observed task still cleans its Typing when no public send is pending", async t => {
  const f = await typingFixture(t), input = await f.receive("interrupted-typing"); await f.settle();
  await f.manager.scan(); await f.settle();
  await f.append(lifecycle("turn_aborted")); await f.manager.scan(); await f.settle();
  assert.equal(f.manager.state.feishuReceipts[input.messageId].cleanupReason, "native_terminal");
  assert.deepEqual(f.reactions, [["add", "interrupted-typing"], ["remove", "interrupted-typing"]]);
});

test("mixed Markdown sends text, waits for its image, then sends the remaining text exactly once", async t => {
  const f = await fixture(t), sent = []; let release, started = false;
  f.manager.notifier = { enabled: true, sendMessage: async (text, opts) => { sent.push({ kind: "text", text: text.trim(), title: opts.title, threadId: opts.threadId }); return { code: 0 }; } };
  f.manager.sendImage = async () => { started = true; sent.push({ kind: "image" }); await new Promise(resolve => { release = resolve; }); return { feishuMessageId: "image-id" }; };
  f.manager.enqueuePublicText(f.manager.session(), "turn-a", "mixed", `前段😀\n![示例](<${path.join(f.root, 'public.png')}>)\n后段`, f.manager.state.selectionEpoch, true);
  const sending = f.manager.drainPublicTexts();
  try {
    for (let i = 0; !started && i < 100; i++) await new Promise(resolve => setTimeout(resolve, 1));
    assert.deepEqual(sent.map(x => x.kind), ["text", "image"]);
    assert.equal(sent[0].text, "前段😀"); assert.equal(sent[0].threadId, id); assert.match(sent[0].title, /回复/);
  } finally { release?.(); }
  await sending; await f.manager.drainPublicTexts();
  assert.deepEqual(sent.map(x => x.kind), ["text", "image", "text"]); assert.equal(sent[2].text, "后段");
  assert.equal(sent.some(x => x.text?.includes("![")), false);
});

test("an unsent preview moves into its Markdown position without a second upload", async t => {
  const f = await fixture(t), sent = [], filename = path.join(f.root, "preview.png");
  f.manager.notifier = { enabled: true, sendMessage: async text => { sent.push(text.trim()); return { code: 0 }; } };
  f.manager.sendImage = async () => { sent.push("IMAGE"); return { feishuMessageId: "image-id" }; };
  f.manager.publicImageRelay.observe(f.manager.session(), { turns: [{ id: "turn-a", items: [{ type: "imageView", id: "preview", path: filename }] }] }, f.manager.state.selectionEpoch);
  const imageUuid = Object.values(f.manager.state.autoImages)[0].messageId;
  f.manager.enqueuePublicText(f.manager.session(), "turn-a", "mixed", `前段\n![示例](<${filename}>)\n中段\n![再次引用](<${filename}>)\n后段`, f.manager.state.selectionEpoch, true);
  await f.manager.drainPublicTexts(); await f.manager.drainPublicTexts();
  assert.deepEqual(sent, ["前段", "IMAGE", "中段", "后段"]);
  assert.equal(Object.values(f.manager.state.autoImages)[0].messageId, imageUuid);
});

test("unknown prefix delivery blocks later media and prose; a binding switch cancels unsent parts", async t => {
  const f = await fixture(t); let writes = 0, images = 0;
  f.manager.notifier = { enabled: true, sendMessage: async () => { writes++; throw new Error("unknown send"); } };
  f.manager.sendImage = async () => { images++; return { feishuMessageId: "wrong" }; };
  f.manager.enqueuePublicText(f.manager.session(), "turn-a", "mixed", `前段\n![示例](<${path.join(f.root, 'public.png')}>)\n后段`, f.manager.state.selectionEpoch, true);
  await f.manager.drainPublicTexts(); await f.manager.drainPublicTexts();
  assert.equal(writes, 1); assert.equal(images, 0);
  assert.ok(Object.values(f.manager.state.publicTextOutbox).find(e => e.status === "unknown")?.text, "Unknown output must retain its exact body for investigation");
  f.manager.state.selectionEpoch++;
  await f.manager.drainPublicTexts();
  assert.equal(writes, 1); assert.equal(images, 0);
  assert.equal(Object.values(f.manager.state.publicTextOutbox).some(e => e.status === "pending"), false);
});

test("state catchup cannot hide an unknown image send behind healthy maintenance", async t => {
  const f = await fixture(t); await f.append(lifecycle("task_started")); f.manager.taskJournal.byteBudget = 16;
  f.manager.taskJournal.chunkBytes = 16;
  f.manager.state.autoImages = { uncertain: { threadId: id, bindingEpoch: f.manager.state.selectionEpoch,
    status: "failed", error: "image_send_outcome_unknown" } };
  await f.manager.scan(); assert.equal(f.manager.session().relayStatus.lastError, "feishu_public_send_unconfirmed");
});
