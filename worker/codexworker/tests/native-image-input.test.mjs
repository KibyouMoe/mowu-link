import test from "node:test";
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createNativeInput } from "../interface/desktop-connection/native-input.mjs";
import { sendNativeInput } from "../interface/native-input-client.mjs";
import { DesktopManager } from "../desktop-manager.mjs";
import { projectPublicQuestion, nativeQuestionReply } from "../interface/public-questions.mjs";
import { callTool } from "../interface/transport/desktop-pipe.mjs";

const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1WkAAAAASUVORK5CYII=", "base64");
async function fixture(t, { active = false, dropWrite = false, rejectWrite = false, question = false,
  cancel = false, finishOnCancel = true, message = false, chat = null, chatImages = false, closure = false, respond } = {}) {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "native-image-图片-"));
  const messageId = randomUUID(), threadId = randomUUID(), owner = { pid: 700, startedAt: "test Desktop lifetime" };
  const image = path.join(stateDir, "feishu-images", "original.png");
  await mkdir(path.dirname(image)); await writeFile(image, png);
  const request = { messageId, threadId, prompt: "飞书：看这张图片", images: [image], delivery: "steer" };
  const receipt = { request: { source: "trainer" }, nativeTargetThreadId: threadId,
    nativeInputHash: createHash("sha256").update(JSON.stringify(request.prompt)).digest("hex"), nativeDispatch: { status: "dispatching" } };
  const state = { selectedThreadId: threadId, sessions: { [threadId]: {} }, receipts: { [messageId]: receipt },
    feishuReceipts: { [messageId]: { incomingMessageId: "om_actual_feishu", imageThreadId: threadId, nativeImagePaths: [image] } } };
  let publicQuestion, action;
  if (closure) question = true;
  if (question) {
    publicQuestion = projectPublicQuestion({ type: "AgentMessage", id: "call_original_question", phase: "commentary",
      delivery: "async", questions: [{ title: "继续吗？", options: ["继续", "停止"] }] }, { threadId, turnId: "source-turn" });
    action = { nativeMessageId: messageId, sourceTurnId: "source-turn", status: "dispatching",
      answerActor: "trainer", answers: ["继续"], bindingEpoch: 4 };
    Object.assign(request, { operation: "questionReply", prompt: nativeQuestionReply(publicQuestion, action.answers) });
    delete request.images;
    receipt.nativeInputHash = createHash("sha256").update(JSON.stringify(request.prompt)).digest("hex");
    state.selectionEpoch = 4; state.publicQuestions = { [publicQuestion.questionId]: publicQuestion };
  }
  if (cancel) {
    Object.assign(request, { operation: 'cancel', taskId: 't-cancel-test', turnId: 'active-turn' });
    receipt.request = { source: 'hermes', origin: 'router', text: '[router t-cancel-test] original task' };
    receipt.nativeTurnId = 'active-turn'; receipt.nativeDispatch.status = 'accepted';
    delete request.images; delete request.prompt; delete request.delivery;
  }
  if (message) {
    Object.assign(request, { operation: "message", taskId: "t-message-test",
      prompt: "[router t-message-test] real queued task", delivery: "queue" });
    delete request.images;
    receipt.request = { source: "hermes", origin: "router", text: request.prompt };
    receipt.nativeInputHash = createHash("sha256").update(JSON.stringify(request.prompt)).digest("hex");
  }
  if (chat) {
    request.operation = "chat";
    receipt.request = { source: "trainer", messageId, text: request.prompt };
    if (!chatImages) { delete request.images; state.feishuReceipts[messageId].nativeImagePaths = []; }
    if (chat === "peer") {
      const peer = { messageId, from: "Hermes", to: "OguriCap" };
      const folder = path.join(stateDir, "worker-messages", createHash("sha256").update(peer.to).digest("hex"));
      receipt.request = { source: "hermes", origin: "worker-chat", messageId, targetThreadId: threadId,
        text: request.prompt, metadata: { workerChat: peer } };
      if (chatImages) {
        const cached = path.join(folder, "images", "original.png");
        await mkdir(path.dirname(cached), { recursive: true }); await writeFile(cached, png);
        request.images = [cached]; receipt.request.attachments = [{ localPath: cached }];
      }
      await mkdir(path.join(folder, "received"), { recursive: true });
      await writeFile(path.join(folder, "received", messageId + ".json"), JSON.stringify({
        request: receipt.request, payload: { message_id: messageId, from: peer.from, to: peer.to } }));
    }
  }
  if (closure) {
    Object.assign(request, { operation: "questionClose", questionId: publicQuestion.questionId });
    delete request.prompt; delete request.images; delete request.delivery;
    action.status = "expired"; action.answers = null;
    action.nativePanelSync = { kind: "closure", status: "dispatching", messageId };
  }
  const saveAction = () => writeFile(path.join(stateDir, "question-actions.json"),
    JSON.stringify({ entries: { [publicQuestion.questionId]: action } }));
  if (question) await saveAction();
  const save = () => writeFile(path.join(stateDir, "desktop-manager-state.json"), JSON.stringify(state));
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ autoStart: true }));
  await save(); const calls = []; let controller, wrote, turnStatus = active ? 'inProgress' : null;
  const writeStarted = new Promise(resolve => { wrote = resolve; });
  controller = await createNativeInput({ stateDir, owner, send: message => {
    calls.push(message);
    if (message.method === 'turn/interrupt' && finishOnCancel) turnStatus = 'interrupted';
    if (message.method !== "thread/turns/list") { wrote(); if (dropWrite) return; }
    const response = respond?.(message, turnStatus) ?? (message.method === "thread/turns/list"
      ? { result: { data: turnStatus ? [{ id: "active-turn", status: turnStatus }] : [] } }
      : message.method === "thread/items/list" ? { result: { data: [{ turnId: "active-turn", item: { id: "input", type: "userMessage" } }] } }
      : message.method === "thread/backgroundTerminals/list" ? { result: { data: [] } }
      : rejectWrite ? { error: { code: -32000, message: "original native rejection" } }
      : message.method === "thread/inject_items" ? { result: {} }
      : { result: message.method === "turn/start" ? { turn: { id: "native-turn" } }
        : message.method === "thread/queue/add" ? { queuedSubmission: { id: messageId } } : { turnId: "active-turn" } });
    setImmediate(() => assert.equal(controller.server({ id: message.id, ...response }), false));
  } });
  const env = { ...controller.env, CODEXWORKER_DESKTOP_PID: String(owner.pid), RUDOLF_BRIDGE_DESKTOP_STARTED_AT: owner.startedAt };
  t.after(async () => { await controller.close(); await rm(stateDir, { recursive: true, force: true }); });
  return { stateDir, controller, request, env, state, save, calls, image, writeStarted, action, saveAction,
    setTurnStatus: status => { turnStatus = status; },
    send: value => sendNativeInput(value ?? request, { env }),
    ledger: async () => JSON.parse(await readFile(path.join(stateDir, "native-image-receipts", messageId + ".json"), "utf8")) };
}

test("router text uses the existing native connection and one durable message UUID", async t => {
  const f = await fixture(t, { message: true });
  f.state.selectedThreadId = "another-selected-chat"; await f.save();
  f.controller.client({ method: "initialized" });
  const result = await f.send();
  assert.deepEqual(await f.send(), result);
  assert.deepEqual(f.calls.map(call => call.method), ["thread/turns/list", "turn/start"]);
  assert.deepEqual(f.calls[1].params.input, [{ type: "text", text: f.request.prompt }]);
  assert.equal(f.calls[1].params.clientUserMessageId, f.request.messageId);
  const saved = JSON.parse(await readFile(path.join(f.stateDir, "native-message-receipts", f.request.messageId + ".json"), "utf8"));
  assert.equal(saved.status, "accepted");
  assert.equal(saved.result.turnId, "native-turn");
});

for (const chat of ["feishu", "peer"]) for (const active of [false, true]) for (const chatImages of [false, true])
test(`${chat} native user input, busy=${active}, images=${chatImages}, uses one normal turn input`, async t => {
  const f = await fixture(t, { chat, active, chatImages }); f.controller.client({ method: "initialized" });
  const result = await f.send(); assert.deepEqual(await f.send(), result);
  assert.deepEqual(f.calls.map(call => call.method), ["thread/turns/list", active ? "turn/steer" : "turn/start"]);
  assert.equal(f.calls[1].params.expectedTurnId, active ? "active-turn" : undefined);
  assert.equal(f.calls[1].params.clientUserMessageId, f.request.messageId);
  assert.equal(f.calls[1].params.input[0].type, "text");
  assert.equal(f.calls[1].params.input[0].text, f.request.prompt);
  assert.equal(f.calls[1].params.input.length, chatImages ? 2 : 1);
  assert.equal(f.calls.some(call => call.method === "thread/queue/add"), false);
  const saved = JSON.parse(await readFile(path.join(f.stateDir, "native-chat-receipts", f.request.messageId + ".json"), "utf8"));
  assert.equal(saved.status, "accepted");
});

test("ordinary chat never uses the delegated app-tool path, even while its renderer pipe is unavailable", async t => {
  const f = await fixture(t, { chat: "feishu", active: true }); f.controller.client({ method: "initialized" });
  const values = { ...f.env, CODEX_APP_TOOLS_PIPE_PATH: "unavailable-renderer-pipe" };
  const previous = Object.keys(values).map(key => [key, process.env[key]]);
  Object.assign(process.env, values);
  t.after(() => { for (const [key, value] of previous) value === undefined ? delete process.env[key] : process.env[key] = value; });
  const response = await callTool("send_message_to_thread", { threadId: f.request.threadId, prompt: f.request.prompt,
    nativeChatInput: true, nativeMessageId: f.request.messageId, nativeDelivery: "steer" }, { threadId: f.request.threadId });
  assert.equal(JSON.parse(response.contentItems[0].text).turnId, "active-turn");
  assert.equal(f.calls[1].method, "turn/steer");
});

test("chat authority requires exact saved Feishu or peer admission and rejects task-shaped requests", async t => {
  const f = await fixture(t, { chat: "feishu" }); f.controller.client({ method: "initialized" });
  await assert.rejects(f.send({ ...f.request, taskId: "t-forged" }), /request_invalid/);
  delete f.state.feishuReceipts[f.request.messageId]; await f.save();
  await assert.rejects(f.send(), /receipt_mismatch/);
  f.state.receipts[f.request.messageId].request.source = "hermes";
  f.state.receipts[f.request.messageId].request.origin = "router"; await f.save();
  await assert.rejects(f.send(), /receipt_mismatch/);
  assert.equal(f.calls.length, 0);
});

for (const active of [false, true]) test(`closed native question withdraws its panel, active=${active}, without a new turn`, async t => {
  const f = await fixture(t, { closure: true, active }); f.controller.client({ method: "initialized" });
  const first = await f.send(); assert.deepEqual(await f.send(), first);
  assert.deepEqual(f.calls.map(call => call.method), ["thread/turns/list", active ? "turn/steer" : "thread/inject_items"]);
  const text = active ? f.calls[1].params.input[0].text : f.calls[1].params.items[0].content[0].text;
  assert.match(text, /系统撤回，未提供答案/);
  assert.match(text, /call_original_question/);
  assert.equal(f.calls.some(call => call.method === "turn/start"), false);
  assert.equal(f.action.answers, null);
  const saved = JSON.parse(await readFile(path.join(f.stateDir, "native-question-closures", f.request.messageId + ".json"), "utf8"));
  assert.equal(saved.status, "accepted");
});

test("native question withdrawal requires the exact closed question and saved control identity", async t => {
  const f = await fixture(t, { closure: true }); f.controller.client({ method: "initialized" });
  f.action.status = "open"; await f.saveAction();
  await assert.rejects(f.send(), /closure_mismatch/);
  f.action.status = "expired"; f.action.nativePanelSync.messageId = randomUUID(); await f.saveAction();
  await assert.rejects(f.send(), /closure_mismatch/);
  assert.equal(f.calls.length, 0);
});

test("a busy router target defers before writing and can retry the same UUID when idle", async t => {
  const f = await fixture(t, { message: true, active: true });
  f.controller.client({ method: "initialized" });
  await assert.rejects(f.send(), error => error.nativeErrorCode === "native_task_busy" && error.nativeImageOutcome === "notAttempted");
  await assert.rejects(readFile(path.join(f.stateDir, "native-message-receipts", f.request.messageId + ".json")), { code: "ENOENT" });
  assert.deepEqual(f.calls.map(call => call.method), ["thread/turns/list"]);
  f.setTurnStatus("completed");
  await f.send();
  assert.equal(f.calls.filter(call => call.method === "turn/start").length, 1);
  assert.equal(f.calls.some(call => call.method === "turn/steer"), false);
});

test("router native input rejects unrelated origins and mismatched tasks before RPC", async t => {
  const f = await fixture(t, { message: true });
  f.controller.client({ method: "initialized" });
  await assert.rejects(f.send({ ...f.request, taskId: "t-other" }), /task_receipt_mismatch/);
  f.state.receipts[f.request.messageId].request.origin = "worker-chat"; await f.save();
  await assert.rejects(f.send(), /task_receipt_mismatch/);
  assert.equal(f.calls.length, 0);
});

test("router native delivery remains available with the Desktop renderer pipe unavailable", async t => {
  const f = await fixture(t, { message: true });
  f.controller.client({ method: "initialized" });
  const values = { ...f.env, CODEX_APP_TOOLS_PIPE_PATH: "renderer-pipe-is-unavailable" };
  const previous = Object.keys(values).map(key => [key, process.env[key]]);
  Object.assign(process.env, values);
  t.after(() => { for (const [key, value] of previous) value === undefined ? delete process.env[key] : process.env[key] = value; });
  const response = await callTool("send_message_to_thread", { threadId: f.request.threadId, prompt: f.request.prompt,
    nativeTask: { taskId: f.request.taskId, messageId: f.request.messageId } }, { threadId: f.request.threadId });
  assert.equal(JSON.parse(response.contentItems[0].text).turnId, "native-turn");
  assert.equal(f.calls.filter(call => call.method === "turn/start").length, 1);
});

test('native task cancellation interrupts only the receipted turn and confirms its terminal state', async t => {
  const f = await fixture(t, { cancel: true, active: true });
  f.controller.client({ method: 'initialized' });
  const result = JSON.parse((await f.send()).contentItems[0].text);
  assert.equal(result.state, 'cancelled');
  assert.equal(result.executionStopped, true);
  assert.deepEqual(f.calls.map(call => call.method), ['thread/turns/list', 'thread/items/list', 'thread/backgroundTerminals/list', 'turn/interrupt',
    'thread/turns/list', 'thread/items/list', 'thread/backgroundTerminals/list']);
  assert.deepEqual(f.calls.find(call => call.method === 'turn/interrupt').params, { threadId: f.request.threadId, turnId: 'active-turn' });
  await f.send();
  assert.equal(f.calls.filter(call => call.method === 'turn/interrupt').length, 1);
});

test('native cancellation terminates only terminals owned by the exact task turn', async t => {
  let running = true;
  const f = await fixture(t, { cancel: true, active: true, respond: ({ method, params }) => {
    if (method === 'thread/items/list') {
      assert.equal(params.turnId, 'active-turn');
      assert.deepEqual(params.deferOutput, { commandExecution: true, mcpToolCall: true });
      return { result: { data: [{ turnId: 'active-turn', item: { id: 'owned-command', type: 'commandExecution', status: running ? 'inProgress' : 'completed' } }] } };
    }
    if (method === 'thread/backgroundTerminals/list') return { result: { data: [
      { itemId: 'old-background-command', processId: 'preserve-this' },
      ...(running ? [{ itemId: 'owned-command', processId: 'terminate-this' }] : [])] } };
    if (method === 'thread/backgroundTerminals/terminate') {
      assert.equal(params.processId, 'terminate-this');
      running = false;
      return { result: { terminated: true } };
    }
  } });
  f.controller.client({ method: 'initialized' });
  const result = JSON.parse((await f.send()).contentItems[0].text);
  assert.equal(result.state, 'cancelled');
  assert.equal(result.executionStopped, true);
  assert.equal(result.pendingTerminals, 0);
  assert.equal(result.pendingCommands, 0);
  assert.equal(f.calls.filter(call => call.method === 'thread/backgroundTerminals/terminate').length, 1);
  assert.equal(f.calls.some(call => call.method === 'thread/backgroundTerminals/clean'), false);
  assert.ok(f.calls.findIndex(call => call.method === 'thread/backgroundTerminals/terminate') <
    f.calls.findIndex(call => call.method === 'turn/interrupt'));
});

test('an empty terminal list cannot confirm a command that is still in progress', async t => {
  const f = await fixture(t, { cancel: true, active: true, respond: ({ method }) => {
    if (method === 'thread/items/list') return { result: { data: [
      { turnId: 'active-turn', item: { id: 'unlisted-tool', type: 'commandExecution',
        processId: 'native-session', status: 'inProgress' } }] } };
  } });
  f.controller.client({ method: 'initialized' });
  const result = JSON.parse((await f.send()).contentItems[0].text);
  assert.equal(result.nativeStatus, 'interrupted');
  assert.equal(result.state, 'cancelling');
  assert.equal(result.executionStopped, false);
  assert.equal(result.pendingTerminals, 0);
  assert.equal(result.pendingCommands, 1);
  assert.deepEqual(result.commands, [{ itemId: 'unlisted-tool', processId: 'native-session', status: 'inProgress' }]);
});

test('live command start events authorize exact cleanup before command items reach history', async t => {
  let running = true;
  const f = await fixture(t, { cancel: true, active: true, respond: ({ method, params }) => {
    if (method === 'thread/backgroundTerminals/list') return { result: { data: [
      ...(running ? [{ itemId: 'live-command', processId: 'live-session' }] : []),
      { itemId: 'another-turn-command', processId: 'untouched-session' }] } };
    if (method === 'thread/backgroundTerminals/terminate') {
      assert.equal(params.processId, 'live-session'); running = false;
      f.controller.server({ method: 'item/completed', params: { threadId: f.request.threadId,
        turnId: 'active-turn', item: { id: 'live-command', type: 'commandExecution', status: 'failed' } } });
      return { result: { terminated: true } };
    }
  } });
  f.controller.client({ method: 'initialized' });
  for (const [turnId, id, processId] of [['active-turn', 'live-command', 'live-session'],
    ['another-turn', 'another-turn-command', 'untouched-session']]) {
    assert.equal(f.controller.server({ method: 'item/started', params: { threadId: f.request.threadId,
      turnId, item: { id, type: 'commandExecution', processId } } }), true);
  }
  const result = JSON.parse((await f.send()).contentItems[0].text);
  assert.equal(result.executionStopped, true);
  assert.equal(result.commandItems, 1);
  assert.equal(result.pendingCommands, 0);
  assert.equal(result.observedTerminals, 1);
  assert.deepEqual(result.itemTypes, { userMessage: 1 });
  assert.equal(f.calls.filter(call => call.method === 'thread/backgroundTerminals/terminate').length, 1);
});

test('an interrupted turn retains live command ownership until the command completes', async t => {
  const f = await fixture(t, { cancel: true, active: true });
  f.controller.client({ method: 'initialized' });
  f.controller.server({ method: 'item/started', params: { threadId: f.request.threadId,
    turnId: 'active-turn', item: { id: 'unlisted-command', type: 'commandExecution', processId: 'running-session' } } });
  assert.equal(JSON.parse((await f.send()).contentItems[0].text).state, 'cancelling');
  const retry = JSON.parse((await f.send()).contentItems[0].text);
  assert.equal(retry.state, 'cancelling');
  assert.equal(retry.pendingCommands, 1);
  f.controller.server({ method: 'item/completed', params: { threadId: f.request.threadId,
    turnId: 'active-turn', item: { id: 'unlisted-command', type: 'commandExecution', status: 'completed' } } });
  assert.equal(JSON.parse((await f.send()).contentItems[0].text).executionStopped, true);
});

test('unavailable native item history cannot serve as proof of stopped execution', async t => {
  const f = await fixture(t, { cancel: true, active: true, respond: ({ method }) => {
    if (method === 'thread/items/list') return { result: { data: [] } };
  } });
  f.controller.client({ method: 'initialized' });
  const result = JSON.parse((await f.send()).contentItems[0].text);
  assert.equal(result.ok, false);
  assert.equal(result.state, 'cancelling');
  assert.equal(result.error, 'native_cancel_items_unavailable');
});

test('an interrupted turn stays cancelling while its tool process is still running', async t => {
  const f = await fixture(t, { cancel: true, active: true, respond: ({ method }) => {
    if (method === 'thread/items/list') return { result: { data: [
      { turnId: 'active-turn', item: { id: 'owned-command', type: 'commandExecution', status: 'inProgress' } }] } };
    if (method === 'thread/backgroundTerminals/list') return { result: { data: [
      { itemId: 'owned-command', processId: 'still-running' }] } };
    if (method === 'thread/backgroundTerminals/terminate') return { result: { terminated: true } };
  } });
  f.controller.client({ method: 'initialized' });
  const result = JSON.parse((await f.send()).contentItems[0].text);
  assert.equal(result.state, 'cancelling');
  assert.equal(result.nativeStatus, 'interrupted');
  assert.equal(result.executionStopped, false);
  assert.equal(result.pendingTerminals, 1);
});

test('terminal API failure leaves cancellation pending instead of claiming execution stopped', async t => {
  const f = await fixture(t, { cancel: true, active: true, respond: ({ method }) => {
    if (method === 'thread/backgroundTerminals/list') return { error: { code: -32601 } };
  } });
  f.controller.client({ method: 'initialized' });
  const result = JSON.parse((await f.send()).contentItems[0].text);
  assert.equal(result.ok, false);
  assert.equal(result.state, 'cancelling');
  assert.equal(result.executionStopped, false);
  assert.equal(result.error, 'native_image_rejected');
  assert.equal(result.nativeMethod, 'thread/backgroundTerminals/list');
});

test('cancellation paginates old turns, scoped command items and owned terminals', async t => {
  let running = true;
  const f = await fixture(t, { cancel: true, active: true, respond: ({ method, params }, status) => {
    if (method === 'thread/turns/list') return { result: params.cursor
      ? { data: [{ id: 'active-turn', status }] }
      : { data: [{ id: 'newer-turn', status: 'inProgress' }], nextCursor: 'older-turns' } };
    if (method === 'thread/items/list') return { result: params.cursor
      ? { data: [{ turnId: 'active-turn', item: { id: 'owned-command', type: 'commandExecution', status: running ? 'inProgress' : 'completed' } }] }
      : { data: [{ turnId: 'active-turn', item: { id: 'input', type: 'userMessage' } }], nextCursor: 'more-items' } };
    if (method === 'thread/backgroundTerminals/list') return { result: params.cursor
      ? { data: running ? [{ itemId: 'owned-command', processId: 'old-owned-process' }] : [] }
      : { data: [{ itemId: 'newer-command', processId: 'keep-running' }], nextCursor: 'more-terminals' } };
    if (method === 'thread/backgroundTerminals/terminate') {
      assert.equal(params.processId, 'old-owned-process'); running = false;
      return { result: { terminated: true } };
    }
  } });
  f.controller.client({ method: 'initialized' });
  assert.equal(JSON.parse((await f.send()).contentItems[0].text).executionStopped, true);
  assert.deepEqual(f.calls.find(call => call.method === 'turn/interrupt').params,
    { threadId: f.request.threadId, turnId: 'active-turn' });
});

test('invalid item ownership or repeated pagination cursors fail closed without a loop', async t => {
  for (const badItem of [true, false]) {
    const f = await fixture(t, { cancel: true, active: true, respond: ({ method }) => {
      if (method === 'thread/items/list') return { result: badItem
        ? { data: [{ turnId: 'another-turn', item: { id: 'another-command', type: 'commandExecution' } }] }
        : { data: [], nextCursor: 'same-cursor' } };
    } });
    f.controller.client({ method: 'initialized' });
    const result = JSON.parse((await f.send()).contentItems[0].text);
    assert.equal(result.state, 'cancelling');
    assert.equal(result.error, badItem ? 'native_cancel_item_invalid' : 'native_cancel_cursor_invalid');
    assert.ok(f.calls.length < 8);
    assert.equal(f.calls.some(call => call.method === 'thread/backgroundTerminals/terminate'), false);
  }
});

test('native cancellation remains pending until the execution is observed stopped', async t => {
  const f = await fixture(t, { cancel: true, active: true, finishOnCancel: false });
  f.controller.client({ method: 'initialized' });
  assert.equal(JSON.parse((await f.send()).contentItems[0].text).state, 'cancelling');
  f.state.receipts[f.request.messageId].nativeTurnId = 'different-turn'; await f.save();
  const count = f.calls.length;
  await assert.rejects(f.send(), /cancel_receipt_mismatch/);
  assert.equal(f.calls.length, count);
});

test('native cancellation rejects another task and trainer-only receipts before any RPC', async t => {
  const f = await fixture(t, { cancel: true, active: true });
  f.controller.client({ method: 'initialized' });
  await assert.rejects(f.send({ ...f.request, taskId: 't-other' }), /cancel_receipt_mismatch/);
  f.state.receipts[f.request.messageId].request.source = 'trainer'; await f.save();
  await assert.rejects(f.send(), /cancel_receipt_mismatch/);
  assert.equal(f.calls.length, 0);
});

test("real saved question answers enter native user input with original question IDs and message UUID", async t => {
  for (const active of [false, true]) {
    const f = await fixture(t, { question: true, active }); f.controller.client({ method: "initialized" });
    const result = await f.send(); assert.deepEqual(await f.send(), result);
    assert.equal(f.calls.length, 2);
    const call = f.calls[1]; assert.equal(call.method, active ? "turn/steer" : "turn/start");
    assert.equal(call.params.clientUserMessageId, f.request.messageId);
    assert.deepEqual(call.params.input, [{ type: "text", text: f.request.prompt }]);
    assert.ok(call.params.input[0].text.includes('call_original_question'));
    const saved = JSON.parse(await readFile(path.join(f.stateDir, "native-question-receipts", f.request.messageId + ".json"), "utf8"));
    assert.equal(saved.status, "accepted");
  }
});

test("question native writes require the exact durable answer and current binding, without image authority", async t => {
  const f = await fixture(t, { question: true }); f.controller.client({ method: "initialized" });
  await assert.rejects(f.send({ ...f.request, images: [f.image] }), /request_invalid/);
  f.action.answers = ["停止"]; await f.saveAction();
  await assert.rejects(f.send(), /question_receipt_mismatch/);
  f.action.answers = ["继续"]; f.action.bindingEpoch++; await f.saveAction();
  await assert.rejects(f.send(), /question_receipt_mismatch/);
  f.action.bindingEpoch--; f.action.status = "answered"; await f.saveAction();
  await assert.rejects(f.send(), /question_receipt_mismatch/);
  f.action.nativePanelSync = { status: "dispatching" }; await f.saveAction();
  f.state.receipts[f.request.messageId].nativeDispatch.status = "accepted"; await f.save();
  await f.send(); assert.equal(f.calls.length, 2);
});

test("uncertain native question input remains durable and cannot be replayed", async t => {
  const f = await fixture(t, { question: true, dropWrite: true }); f.controller.client({ method: "initialized" });
  const waiting = assert.rejects(f.send(), /outcome_unknown/);
  await f.writeStarted; await f.controller.close(); await waiting;
  const restored = await createNativeInput({ stateDir: f.stateDir, owner: { pid: 700, startedAt: "test Desktop lifetime" },
    send: () => assert.fail("uncertain question must not be sent again") });
  try { restored.client({ method: "initialized" }); await assert.rejects(f.send(), /outcome_unknown/); }
  finally { await restored.close(); }
  const saved = JSON.parse(await readFile(path.join(f.stateDir, "native-question-receipts", f.request.messageId + ".json"), "utf8"));
  assert.equal(saved.status, "unknown");
});

test("native images require the matching initialized Desktop and a saved Feishu dispatch", async t => {
  const f = await fixture(t);
  await assert.rejects(f.send(), /not_ready/); assert.equal(f.calls.length, 0);
  f.controller.client({ method: "initialized" });
  await assert.rejects(sendNativeInput(f.request, { env: { ...f.env, CODEXWORKER_DESKTOP_PID: "701" } }), /owner_mismatch/);
  f.state.receipts[f.request.messageId].request.source = "hermes"; await f.save();
  await assert.rejects(f.send(), error => error.message === "native_image_receipt_mismatch" &&
    error.nativeImageOutcome === "notAttempted" && error.nativeReachable === true);
  assert.equal(f.calls.length, 0);
  assert.equal(f.controller.server({ id: "ordinary-desktop-response", result: {} }), true);
});

test("actual Worker Feishu dispatch and native image validation use the same persisted text hash", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "native-image-worker-integration-"));
  const owner = { pid: 700, startedAt: "test Desktop lifetime" }; let controller, manager;
  const calls = [], env = { CODEXWORKER_DESKTOP_PID: "700", RUDOLF_BRIDGE_DESKTOP_STARTED_AT: owner.startedAt };
  t.after(async () => { await controller?.close(); await manager?.close(); await rm(root, { recursive: true, force: true }); });
  await writeFile(path.join(root, "desktop-config.local.json"), JSON.stringify({ autoStart: true }));
  const names = ["read_thread", "send_message_to_thread", "list_threads", "create_thread", "fork_thread", "set_thread_title"];
  manager = await new DesktopManager({ stateDir: root, listNativeTools: async () => names.map(name => ({ name })),
    native: async (name, args) => {
      if (name === "read_thread") return { thread: { id: args.threadId, cwd: root }, turns: [] };
      if (name === "send_message_to_thread") {
        const response = await sendNativeInput({ threadId: args.threadId, prompt: args.prompt,
          images: args.nativeImages, messageId: args.nativeMessageId, delivery: args.nativeDelivery }, { env });
        return JSON.parse(response.contentItems[0].text);
      }
      return {};
    }, config: { token: "isolated-native-image-integration-token", nativeImageInput: true, nativeSenderThreadId: "manager",
      workspaceRoots: [root], feishu: { trainerOpenIdWhitelist: ["trainer"], transcriptEnabled: true } } }).initialize();
  manager.handoffPaused = true;
  const session = manager.makeSession({ id: "original-chat", cwd: root }, { alias: "main", cwd: root });
  manager.state.sessions[session.threadId] = session; manager.state.selectedThreadId = session.threadId;
  manager.notifier = { enabled: true, chatId: "chat", addReaction: async () => null, removeReaction: async () => true,
    downloadImage: async () => png, sendMessage: async () => ({ code: 0, data: { message_id: "test-card" } }) };
  controller = await createNativeInput({ stateDir: root, owner, send: message => {
    calls.push(message);
    setImmediate(() => controller.server({ id: message.id, result: message.method === "thread/turns/list"
      ? { data: [] } : { turn: { id: "native-original-turn" } } }));
  } });
  Object.assign(env, controller.env); controller.client({ method: "initialized" });
  const result = await manager.receiveFeishu({ message_id: "om_native_image_integration", message_type: "image",
    open_id: "trainer", sender_type: "user", chat_type: "p2p", chat_id: "chat", content: JSON.stringify({ image_key: "img_integration" }) });
  assert.equal(result.action, "dispatch_pending");
  assert.equal(manager.state.receipts[result.messageId].nativeDispatch.status, "accepted");
  assert.equal(manager.state.receipts[result.messageId].nativeTurnId, "native-original-turn");
  assert.equal(calls[1].params.input[1].type, "localImage");
  assert.equal(calls[1].params.input[0].text, "飞书：\n训练员从飞书发送了一张图片。");
  const saved = JSON.parse(await readFile(path.join(root, "native-image-receipts", result.messageId + ".json"), "utf8"));
  assert.equal(saved.status, "accepted"); assert.equal(manager.state.selectedThreadId, "original-chat");
});

test("real question callback, shared manager and native IPC persist the same answer before native user input", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "native-question-worker-integration-"));
  const owner = { pid: 700, startedAt: "test Desktop lifetime" }; let controller, manager;
  const calls = [], env = { CODEXWORKER_DESKTOP_PID: "700", RUDOLF_BRIDGE_DESKTOP_STARTED_AT: owner.startedAt };
  t.after(async () => { await controller?.close(); await manager?.close(); await rm(root, { recursive: true, force: true }); });
  await writeFile(path.join(root, "desktop-config.local.json"), JSON.stringify({ autoStart: true }));
  const names = ["read_thread", "send_message_to_thread", "list_threads", "create_thread", "fork_thread", "set_thread_title"];
  manager = await new DesktopManager({ stateDir: root, listNativeTools: async () => names.map(name => ({ name })),
    native: async (name, args) => {
      if (name === "read_thread") return { thread: { id: args.threadId, cwd: root },
        turns: [{ id: "source-turn", status: "inProgress", items: [] }] };
      if (name === "send_message_to_thread") {
        assert.equal(args.nativeQuestionReply, true);
        const response = await sendNativeInput({ operation: "questionReply", threadId: args.threadId, prompt: args.prompt,
          messageId: args.nativeMessageId, delivery: args.nativeDelivery }, { env });
        return JSON.parse(response.contentItems[0].text);
      }
      return {};
    }, config: { token: "isolated-native-question-integration-token", nativeQuestionInput: true,
      nativeBusyDeliveryVerified: true, nativeSenderThreadId: "manager", workspaceRoots: [root],
      feishu: { trainerOpenIdWhitelist: ["trainer"], transcriptEnabled: true } } }).initialize();
  manager.handoffPaused = true;
  const session = manager.makeSession({ id: "original-chat", cwd: root }, { alias: "main", cwd: root });
  manager.state.sessions[session.threadId] = session; manager.state.selectedThreadId = session.threadId;
  manager.state.selectionEpoch = 4;
  session.feishuChatId = "chat";
  manager.notifier = { enabled: true, chatId: "chat", updateQuestionCard: async () => ({ code: 0 }),
    sendQuestionCard: async () => ({ code: 0, data: { message_id: "om_native_question" } }) };
  controller = await createNativeInput({ stateDir: root, owner, send: message => {
    calls.push(message);
    setImmediate(() => controller.server({ id: message.id, result: message.method === "thread/turns/list"
      ? { data: [{ id: "source-turn", status: "inProgress" }] } : { turnId: "source-turn" } }));
  } });
  Object.assign(env, controller.env); controller.client({ method: "initialized" });
  const q = projectPublicQuestion({ type: "AgentMessage", id: "call_native_answer", phase: "commentary",
    delivery: "async", questions: [{ title: "继续吗？", options: ["继续", "停止"] }] },
    { threadId: session.threadId, turnId: "source-turn" });
  manager.handoffPaused = false; await manager.questions.publish(q); manager.handoffPaused = true;
  const entry = manager.questions.state.entries[q.questionId];
  const callback = { header: { event_type: "card.action.trigger", event_id: randomUUID() }, event: {
    operator: { open_id: "trainer" }, context: { open_message_id: "om_native_question", open_chat_id: "chat" },
    action: { value: { bridgeQuestion: q.questionId, nonce: entry.nonce, action: "select", questionIndex: 0, optionIndex: 0 } } } };
  const acknowledgement = await manager.questions.acceptCard(callback);
  assert.equal(acknowledgement.toast.type, "success", acknowledgement.toast.content);
  assert.equal(calls.length, 0);
  manager.handoffPaused = false; await manager.questions.process();
  assert.equal(manager.questions.get(q.questionId).status, "answered");
  assert.equal(manager.questions.get(q.questionId).nativePanelSync, "accepted");
  assert.deepEqual(calls[1].params.input, [{ type: "text", text: nativeQuestionReply(q, ["继续"]) }]);
  assert.equal(calls[1].params.clientUserMessageId, entry.nativeMessageId);
  await manager.questions.process(); assert.equal(calls.length, 2);
});

test("idle input uses typed localImage on the same native connection and replays only its receipt", async t => {
  const f = await fixture(t); f.controller.client({ method: "initialized" });
  const result = await f.send(); const again = await f.send(); assert.deepEqual(again, result);
  assert.equal(f.calls.length, 2); assert.equal(f.calls[0].method, "thread/turns/list");
  assert.deepEqual(f.calls[0].params, { threadId: f.request.threadId, limit: 1, sortDirection: "desc", itemsView: "notLoaded" });
  assert.equal(f.calls[1].method, "turn/start");
  assert.deepEqual(f.calls[1].params, { threadId: f.request.threadId, clientUserMessageId: f.request.messageId,
    input: [{ type: "text", text: f.request.prompt }, { type: "localImage", path: f.image }] });
  assert.equal((await f.ledger()).status, "accepted");
  assert.equal(JSON.stringify(f.calls).includes(png.toString("base64")), false);
  assert.equal((await readFile(f.image)).equals(png), true);
  await assert.rejects(f.send({ ...f.request, prompt: "different" }), /message_conflict/);
  await f.controller.close();
  const restored = await createNativeInput({ stateDir: f.stateDir, owner: { pid: 700, startedAt: "test Desktop lifetime" },
    send: () => assert.fail("accepted image must not be sent twice after restart") });
  try { restored.client({ method: "initialized" }); assert.deepEqual(await f.send(), result); }
  finally { await restored.close(); }
});

test("busy images use the actual expected turn; queue keeps native queued-submission semantics", async t => {
  for (const delivery of ["steer", "queue"]) {
    const f = await fixture(t, { active: true }); f.controller.client({ method: "initialized" });
    await f.send({ ...f.request, delivery });
    const call = f.calls[1]; assert.equal(call.method, delivery === "steer" ? "turn/steer" : "thread/queue/add");
    assert.equal(call.params.expectedTurnId, delivery === "steer" ? "active-turn" : undefined);
    assert.equal(call.params.input[1].type, "localImage");
  }
});

test("image paths, original chat selection and persisted input hashes are enforced before writes", async t => {
  const f = await fixture(t); f.controller.client({ method: "initialized" });
  f.state.selectedThreadId = "another-chat"; await f.save();
  await assert.rejects(f.send(), /receipt_mismatch/);
  f.state.selectedThreadId = f.request.threadId;
  const outside = path.join(f.stateDir, "outside.png"); await writeFile(outside, png);
  f.state.feishuReceipts[f.request.messageId].nativeImagePaths = [outside]; await f.save();
  await assert.rejects(f.send({ ...f.request, images: [outside] }), /path_invalid/);
  await assert.rejects(f.send({ ...f.request, messageId: "../../arbitrary" }), /request_invalid/);
  assert.equal(f.calls.length, 0);
  await writeFile(path.join(f.stateDir, "desktop-config.local.json"), JSON.stringify({ autoStart: false }));
  await assert.rejects(f.send(), /autostart_disabled/);
});

test("native rejection is terminal and disconnect preserves an uncertain image write without resending", async t => {
  const rejected = await fixture(t, { rejectWrite: true }); rejected.controller.client({ method: "initialized" });
  await assert.rejects(rejected.send(), /rejected/);
  await assert.rejects(rejected.send(), /rejected/);
  assert.equal((await rejected.ledger()).status, "rejected"); assert.equal(rejected.calls.length, 2);
  const f = await fixture(t, { dropWrite: true }); f.controller.client({ method: "initialized" });
  const waiting = assert.rejects(f.send(), /outcome_unknown/);
  await f.writeStarted; await f.controller.close(); await waiting;
  assert.equal((await f.ledger()).status, "unknown"); assert.equal(f.calls.length, 2);
  const restored = await createNativeInput({ stateDir: f.stateDir, owner: { pid: 700, startedAt: "test Desktop lifetime" },
    send: () => assert.fail("uncertain image must not be retried after restart") });
  try { restored.client({ method: "initialized" }); await assert.rejects(f.send(), /outcome_unknown/); }
  finally { await restored.close(); }
});
