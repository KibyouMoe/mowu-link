import test from "node:test";
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createNativeInput } from "../interface/desktop-connection/native-input.mjs";
import { sendNativeImageInput } from "../interface/native-image-input.mjs";
import { DesktopManager } from "../desktop-manager.mjs";

const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1WkAAAAASUVORK5CYII=", "base64");
async function fixture(t, { active = false, dropWrite = false, rejectWrite = false } = {}) {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "native-image-图片-"));
  const messageId = randomUUID(), threadId = randomUUID(), owner = { pid: 700, startedAt: "test Desktop lifetime" };
  const image = path.join(stateDir, "feishu-images", "original.png");
  await mkdir(path.dirname(image)); await writeFile(image, png);
  const request = { messageId, threadId, prompt: "飞书：看这张图片", images: [image], delivery: "steer" };
  const receipt = { request: { source: "trainer" }, nativeTargetThreadId: threadId,
    nativeInputHash: createHash("sha256").update(JSON.stringify(request.prompt)).digest("hex"), nativeDispatch: { status: "dispatching" } };
  const state = { selectedThreadId: threadId, sessions: { [threadId]: {} }, receipts: { [messageId]: receipt },
    feishuReceipts: { [messageId]: { incomingMessageId: "om_actual_feishu", imageThreadId: threadId, nativeImagePaths: [image] } } };
  const save = () => writeFile(path.join(stateDir, "desktop-manager-state.json"), JSON.stringify(state));
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ autoStart: true }));
  await save(); const calls = []; let controller, wrote;
  const writeStarted = new Promise(resolve => { wrote = resolve; });
  controller = await createNativeInput({ stateDir, owner, send: message => {
    calls.push(message);
    if (message.method !== "thread/turns/list") { wrote(); if (dropWrite) return; }
    const response = message.method === "thread/turns/list"
      ? { result: { data: active ? [{ id: "active-turn", status: "inProgress" }] : [] } }
      : rejectWrite ? { error: { code: -32000, message: "original native rejection" } }
      : { result: message.method === "turn/start" ? { turn: { id: "native-turn" } }
        : message.method === "thread/queue/add" ? { queuedSubmission: { id: messageId } } : { turnId: "active-turn" } };
    setImmediate(() => assert.equal(controller.server({ id: message.id, ...response }), false));
  } });
  const env = { ...controller.env, CODEXWORKER_DESKTOP_PID: String(owner.pid), RUDOLF_BRIDGE_DESKTOP_STARTED_AT: owner.startedAt };
  t.after(async () => { await controller.close(); await rm(stateDir, { recursive: true, force: true }); });
  return { stateDir, controller, request, env, state, save, calls, image, writeStarted,
    send: value => sendNativeImageInput(value ?? request, { env }),
    ledger: async () => JSON.parse(await readFile(path.join(stateDir, "native-image-receipts", messageId + ".json"), "utf8")) };
}

test("native images require the matching initialized Desktop and a saved Feishu dispatch", async t => {
  const f = await fixture(t);
  await assert.rejects(f.send(), /not_ready/); assert.equal(f.calls.length, 0);
  f.controller.client({ method: "initialized" });
  await assert.rejects(sendNativeImageInput(f.request, { env: { ...f.env, CODEXWORKER_DESKTOP_PID: "701" } }), /owner_mismatch/);
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
        const response = await sendNativeImageInput({ threadId: args.threadId, prompt: args.prompt,
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
  assert.equal(calls[1].params.input[0].text, "飞书：训练员从飞书发送了一张图片。");
  const saved = JSON.parse(await readFile(path.join(root, "native-image-receipts", result.messageId + ".json"), "utf8"));
  assert.equal(saved.status, "accepted"); assert.equal(manager.state.selectedThreadId, "original-chat");
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
