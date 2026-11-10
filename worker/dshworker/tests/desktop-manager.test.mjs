import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { DesktopManager, PROTOCOL, publicItems } from "../desktop-manager.mjs";
import { localHost, nativeSendSource, nativeToolInvocation, nativeToolRejection } from "../interface/native-dispatch.mjs";
import { escapeMarkdown, splitChunks, markdownPayload, FeishuNotifier, FeishuWebsocketClient } from "../feishu.mjs";
import { checkContract } from "./contract-check.mjs";
import { normalizeQuestion, questionText } from "../question-relay.mjs";
import { downloadFeishuImage, imageExtension, MAX_IMAGE_BYTES } from "../feishu-images.mjs";

const token = "test-only-token-not-a-production-secret-123456";
const liveStatus = (id = "existing", status = "inProgress") => ({ polls: [{ thread: { id, hostId: "local" },
  latestTurn: { id: "long-turn", status }, latestAssistantMessage: { truncated: true } }] });
const req = (text, extra = {}) => ({ protocolVersion: PROTOCOL, messageId: randomUUID(), source: "hermes", text, ...extra });

test("an unconfirmed head yields to later pending output without replay, and trainer abandonment keeps its evidence", async t => {
  const f = await fixture(t); await f.register(); const uuid = randomUUID(), next = randomUUID(); const epoch = f.manager.state.selectionEpoch;
  f.manager.state.publicTextOutbox = { [uuid]: { uuid, threadId: "existing", bindingEpoch: epoch, status: "unknown", order: 1, text: "old uncertain body" },
    [next]: { uuid: next, threadId: "existing", bindingEpoch: epoch, status: "pending", order: 2, text: "next known-unsent reply" } };
  const sent = []; f.manager.notifier = { sendMessage: async text => { sent.push(text); return { code: 0 }; } };
  await f.manager.drainPublicTexts();
  // The uncertain head is settled instead of stalling the queue, and never replayed.
  assert.deepEqual(sent, ["next known-unsent reply"]);
  assert.equal(f.manager.state.publicTextOutbox[uuid].status, "canceled"); assert.equal(f.manager.state.publicTextOutbox[uuid].text, undefined);
  await f.manager.drainPublicTexts(); assert.deepEqual(sent, ["next known-unsent reply"]);
  // A lone unknown keeps its exact body until the trainer decides; abandoning it never sends.
  const lone = randomUUID();
  f.manager.state.publicTextOutbox[lone] = { uuid: lone, threadId: "existing", bindingEpoch: epoch, status: "unknown", order: 3, text: "still uncertain body" };
  await f.manager.drainPublicTexts(); assert.deepEqual(sent, ["next known-unsent reply"]);
  assert.equal(f.manager.state.publicTextOutbox[lone].status, "unknown");
  const review = { uuid: lone, threadId: "existing", bindingEpoch: epoch, reason: "Trainer abandoned this old reply and requested later output." };
  assert.equal((await f.http("/bridge/text-receipts/abandon", review, "POST", "Bearer wrong")).status, 401);
  const result = await f.http("/bridge/text-receipts/abandon", review); assert.equal(result.status, 200);
  assert.equal(result.body.originalStatus, "unknown"); assert.equal(result.body.replayed, false); assert.equal(result.body.markedDelivered, false);
  const disk = JSON.parse(await readFile(f.manager.statePath, "utf8")); assert.equal(disk.publicTextOutbox[lone].review.decision, "abandon");
  assert.equal(disk.publicTextOutbox[lone].uuid, lone); assert.equal(disk.publicTextOutbox[lone].status, "unknown");
  await f.manager.drainPublicTexts(); assert.deepEqual(sent, ["next known-unsent reply"]);
  assert.equal((await f.http("/bridge/text-receipts/abandon", review)).body.duplicate, true);
  assert.equal(f.manager.integrationStatus().feishu.relay.unconfirmedTexts, 0);
  assert.equal(f.manager.integrationStatus().feishu.relay.abandonedUnknownTexts, 1);
  await f.manager.close();
  const restored = await new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config }).initialize();
  try { assert.equal(restored.state.publicTextOutbox[lone].status, "unknown"); assert.equal(restored.integrationStatus().feishu.relay.unconfirmedTexts, 0); }
  finally { await restored.close(); }
});

test("receipt review rejects another binding and in-flight sends; disk failure cannot release a barrier", async t => {
  const f = await fixture(t); await f.register(); const uuid = randomUUID(), epoch = f.manager.state.selectionEpoch;
  const entry = f.manager.state.publicTextOutbox = { [uuid]: { uuid, threadId: "existing", bindingEpoch: epoch, status: "unknown", order: 1 } };
  const body = { uuid, threadId: "existing", bindingEpoch: epoch, reason: "trainer decision" };
  assert.equal((await f.http("/bridge/text-receipts/abandon", { ...body, bindingEpoch: epoch + 1 })).status, 409);
  assert.equal((await f.http("/bridge/text-receipts/abandon", { ...body, threadId: "other" })).status, 409);
  entry[uuid].status = "dispatching"; assert.equal((await f.http("/bridge/text-receipts/abandon", body)).status, 409);
  entry[uuid].status = "unknown"; const save = f.manager.save.bind(f.manager);
  f.manager.save = async () => { throw new Error("disk failure"); };
  await assert.rejects(f.manager.abandonTextReceipt(body), /disk failure/); assert.equal(entry[uuid].review, undefined);
  f.manager.save = save; assert.equal(f.manager.integrationStatus().feishu.relay.unconfirmedTexts, 1);
});

test("large-turn Feishu inputs use fresh native state, never history, and duplicate receipts never resend", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  f.manager.tools.push({ name: "wait_threads" }); let writes = 0, reads = 0;
  f.manager.native = async (name, args) => {
    if (name === "wait_threads") return liveStatus();
    if (name === "read_thread") { reads++; throw new Error("oversized history"); }
    if (name === "send_message_to_thread") { writes++; return { threadId: args.threadId }; }
    throw new Error(name);
  };
  const event = { message_id: "om_big_round", chat_type: "p2p", sender_type: "user", message_type: "text", content: "长轮次继续工作", open_id: "trainer", chat_id: "oc_test" };
  const first = await f.manager.receiveFeishu(event); const second = await f.manager.receiveFeishu(event);
  assert.equal(first.action, "dispatch_pending"); assert.equal(second.duplicate, true); assert.equal(writes, 1); assert.equal(reads, 0);
  const receipt = Object.values(f.manager.state.receipts).find(r => r.request.text === event.content);
  assert.equal(receipt.nativeDispatch.status, "accepted"); assert.equal(f.manager.session().pendingInputs.length, 0);
});

test("status-only delivery keeps busy permission, source turn and selection checks", async t => {
  const f = await fixture(t); await f.register(); f.manager.tools.push({ name: "wait_threads" });
  f.manager.native = async name => { if (name === "wait_threads") return liveStatus(); throw new Error("unexpected write"); };
  await assert.rejects(f.manager.message(req("busy")), /Busy native delivery/);
  f.manager.config.nativeBusyDeliveryVerified = true;
  await assert.rejects(f.manager.message(req("stale"), { requireSourceTurnId: "old-turn" }), /original question turn/);
  f.manager.native = async name => { if (name === "wait_threads") { f.manager.state.selectionEpoch++; return liveStatus(); } throw new Error("unexpected write"); };
  await assert.rejects(f.manager.message(req("changed")), /selected chat changed/);
});

test("unknown status-only writes persist uncertainty and are never repeated", async t => {
  const f = await fixture(t); await f.register(); f.manager.tools.push({ name: "wait_threads" }); let writes = 0;
  f.manager.native = async name => { if (name === "wait_threads") return liveStatus("existing", "completed"); if (name === "send_message_to_thread") { writes++; throw new Error("timeout"); } throw new Error(name); };
  const request = req("only once"); await assert.rejects(f.manager.message(request)); await assert.rejects(f.manager.message(request));
  assert.equal(writes, 1); assert.equal(f.manager.state.receipts[request.messageId].nativeDispatch.status, "unknown");
});

test("journal relays all completed public texts/images once without bulk reads and persists outbox before send", async t => {
  const f = await fixture(t); await f.register(); f.manager.tools.push({ name: "wait_threads" });
  f.manager.native = async name => { if (name === "wait_threads") return liveStatus("existing", "completed"); throw new Error("bulk history must not run"); };
  f.manager.publicJournal = { locate: async () => ({}), read: async () => ({ checkpoint: { offset: 123, epoch: f.manager.state.selectionEpoch }, caughtUp: true, pendingBytes: 0, bytesRead: 123,
    events: [{ turnId: "long-turn", item: { id: "m1", type: "agentMessage", phase: "commentary", text: "第一条" } },
      { turnId: "long-turn", item: { id: "m2", type: "agentMessage", phase: "final_answer", text: "最后一条" } },
      { turnId: "long-turn", item: { id: "img", type: "imageView", path: path.join(f.root, "public.png") } }] }) };
  const sent = [], images = [];
  f.manager.notifier = { enabled: true, sendMessage: async (text, opts) => {
    const disk = JSON.parse(await readFile(f.manager.statePath, "utf8"));
    assert.equal(disk.publicTextOutbox[opts.uuid].status, "dispatching"); assert.equal(disk.sessions.existing.publicJournalCheckpoint.offset, 123);
    sent.push(text); return { code: 0 };
  } };
  f.manager.sendImage = async b => { images.push(b); return { feishuMessageId: "sent-img" }; };
  await f.manager.scan(); await Promise.all([...f.manager.backgroundTasks]); await f.manager.scan(); await Promise.all([...f.manager.backgroundTasks]);
  assert.deepEqual(sent, ["第一条", "最后一条"]); assert.equal(images.length, 1);
  assert.equal(f.manager.session().relayStatus.mode, "desktop-public-journal"); assert.equal(f.manager.session().relayStatus.lastError, null);
});

test("public text outbox keeps unknown outcomes and cancels an old binding without replay", async t => {
  const f = await fixture(t); await f.register(); let sent = 0;
  f.manager.state.publicTextOutbox = { first: { uuid: "first", threadId: "existing", bindingEpoch: f.manager.state.selectionEpoch, text: "unknown", status: "pending" },
    old: { uuid: "old", threadId: "existing", bindingEpoch: -1, text: "stale", status: "pending" } };
  f.manager.notifier = { sendMessage: async () => { sent++; throw new Error("timeout"); } };
  await f.manager.drainPublicTexts(); await f.manager.drainPublicTexts();
  assert.equal(sent, 1); assert.equal(f.manager.state.publicTextOutbox.first.status, "unknown"); assert.equal(f.manager.state.publicTextOutbox.old.status, "canceled");
});
test("handoff after claiming a public output retains its pending UUID and body", async t => {
  const f = await fixture(t); await f.register();
  f.manager.state.publicTextOutbox = { item: { uuid: "item", threadId: "existing", bindingEpoch: f.manager.state.selectionEpoch, text: "keep me", status: "pending" } };
  let writes = 0; f.manager.notifier = { sendMessage: async () => { writes++; return { code: 0 }; } };
  const save = f.manager.save.bind(f.manager);
  f.manager.save = async () => { await save(); if (f.manager.state.publicTextOutbox.item.status === "dispatching") f.manager.handoffPaused = true; };
  await f.manager.drainPublicTexts(); assert.equal(writes, 0);
  assert.equal(f.manager.state.publicTextOutbox.item.status, "pending"); assert.equal(f.manager.state.publicTextOutbox.item.text, "keep me");
  f.manager.handoffPaused = false; f.manager.save = save;
});

test("Feishu goal/help commands keep selected native task and report dispatch instead of fake activation", async t => {
  const f = await fixture(t); await f.register();
  const help = await f.manager.message(req("/help")); assert.match(help.text, /\/goal/); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  const result = await f.manager.message(req("/goal 完成这个目标"));
  assert.equal(result.action, "dispatch_pending"); const call = f.calls.find(c => c.name === "send_message_to_thread");
  assert.equal(call.args.threadId, "existing"); assert.match(call.args.prompt, /create_goal/);
});
test("feishu-local polls its selected chat without rereading inactive unrelated large histories", async t => {
  const f = await fixture(t, { operatingMode: "feishu-local" }); await f.register();
  f.tasks.set("selected", { thread: { id: "selected", cwd: f.root }, turns: [] }); await f.register("selected");
  const reads = []; f.manager.native = async (name, args, ctx) => { if (name === "read_thread") reads.push(args.threadId); return f.native(name, args, ctx); };
  await f.manager.scan(); assert.deepEqual(reads, ["selected"]);
});
test("Feishu ACK waits for durable handler acceptance and never acknowledges a failed handler", async () => {
  let release, ack = 0;
  const client = new FeishuWebsocketClient({ enabled: true }, { onMessage: () => new Promise(resolve => { release = resolve; }) });
  const frame = { method: 1, headers: { type: "event" }, payload: new TextEncoder().encode(JSON.stringify({ event: {
    sender: { sender_type: "user", sender_id: { open_id: "trainer" } }, message: { message_id: "ack-test", chat_id: "oc_test",
      chat_type: "p2p", message_type: "text", content: JSON.stringify({ text: "text" }) } } })) };
  const running = client._handleFrame({ send: () => { ack++; } }, frame, 1);
  assert.equal(ack, 0); release(); await running; assert.equal(ack, 1);
  client.onMessage = async () => { throw new Error("disk unavailable"); };
  await assert.rejects(client._handleFrame({ send: () => { ack++; } }, frame, 1)); assert.equal(ack, 1);
});
test("durable Feishu inbox acknowledges promptly during a slow native write and preserves logical UUID", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  const original = f.manager.native; let release;
  f.manager.native = async (...args) => { if (args[0] === "send_message_to_thread") await new Promise(resolve => { release = resolve; }); return original(...args); };
  const event = { message_id: "om_durable_inbox", chat_type: "p2p", sender_type: "user", message_type: "text", content: "stored before native send", open_id: "trainer", chat_id: "oc_test" };
  await f.manager.enqueueFeishu(event);
  const persisted = JSON.parse(await readFile(f.manager.inboxPath, "utf8")); assert.equal(persisted.entries[event.message_id].content, event.content);
  for (let i = 0; !release && i < 100; i++) await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(f.manager.hotBusy(), true); release(); await Promise.all([...f.manager.backgroundTasks]);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  await f.manager.enqueueFeishu(event); await Promise.all([...f.manager.backgroundTasks]);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  assert.deepEqual(JSON.parse(await readFile(f.manager.inboxPath, "utf8")).entries, {});
});
test("pending durable Feishu inbox resumes after a normal business generation restart", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register(); await f.manager.close();
  const event = { message_id: "om_inbox_restore", chat_type: "p2p", sender_type: "user", message_type: "text", content: "resume pending only", open_id: "trainer", chat_id: "oc_test" };
  await writeFile(f.manager.inboxPath, JSON.stringify({ version: 1, entries: { [event.message_id]: event } }));
  const restored = await new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config }).initialize();
  t.after(() => restored.close()); await Promise.all([...restored.backgroundTasks]);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  assert.deepEqual(JSON.parse(await readFile(restored.inboxPath, "utf8")).entries, {});
});
test("durably received images remain pinned before processing and cannot spill after selection changes", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  const runInbox = f.manager.runInbox.bind(f.manager); f.manager.runInbox = () => {};
  const event = { message_id: "om_image_inbox_pinned", chat_type: "p2p", sender_type: "user", message_type: "image",
    content: JSON.stringify({ image_key: "img_inbox" }), open_id: "trainer", chat_id: "oc_test" };
  await f.manager.enqueueFeishu(event);
  const saved = JSON.parse(await readFile(f.manager.inboxPath, "utf8")).entries[event.message_id];
  assert.equal(saved.bridgeImageThreadId, "existing");
  f.tasks.set("new-selected", { thread: { id: "new-selected", cwd: f.root }, turns: [] }); await f.register("new-selected");
  let downloads = 0; f.manager.notifier = { addReaction: async () => null, sendMessage: async () => ({ code: 0 }), downloadImage: async () => { downloads++; } };
  await assert.rejects(f.manager.receiveFeishu(saved), /no fallback/);
  assert.equal(downloads, 0); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  f.manager.runInbox = runInbox;
});
test("business handoff reconnection suppresses only its first online notice, not later real reconnects", async t => {
  const f = await fixture(t, { hotReloadTransition: true, feishu: { chatId: "oc_notice" } }); await f.register();
  const sent = []; f.manager.notifier = { enabled: true, sendMessage: async text => { sent.push(text); return { code: 0, data: { message_id: "notice" } }; } };
  const socket = { connectionId: "handoff", _ws: { readyState: 1 }, stop: () => {} }; f.manager.ws = socket;
  await f.manager.onlineAnnouncement.connected(socket, "handoff"); assert.equal(sent.length, 0);
  assert.equal(f.manager.onlineAnnouncement.status().status, "suppressed-hot-reload");
  socket.connectionId = "real-reconnect"; await f.manager.onlineAnnouncement.connected(socket, "real-reconnect"); assert.equal(sent.length, 1);
  f.manager.ws = null;
});

async function fixture(t, overrides = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-desktop-manager-"));
  const tasks = new Map(); const calls = [];
  let count = 0;
  const add = (id, turns = [], cwd = root) => tasks.set(id, { thread: { id, title: id, cwd, status: { type: "idle" } }, turns });
  add("existing");
  const native = async (name, args, deliveryContext) => {
    calls.push({ name, args: structuredClone(args), deliveryContext: structuredClone(deliveryContext) });
    if (name === "list_threads") {
      assert.ok(args.limit <= 50, "Desktop list_threads limits are capped at 50.");
      return { pinnedThreads: [], threads: [...tasks.values()].map((s, index) =>
        ({ ...s.thread, kind: "codex", hostId: "local", updatedAt: s.thread.updatedAt ?? index })) };
    }
    if (name === "read_thread") {
      if (!tasks.has(args.threadId)) throw new Error("missing");
      return structuredClone(tasks.get(args.threadId));
    }
    if (name === "send_message_to_thread") {
      const active = config.nativeBusyDeliveryVerified && tasks.get(args.threadId).turns.find(t => t.status === "inProgress");
      const source = config.nativeSendFromTarget && localHost(args.hostId) ? args.threadId : "manager-test";
      const item = { id: `input-${++count}`, type: "functionCallOutput", name: "send_message_to_thread", output: { text: `<codex_delegation><source_thread_id>${source}</source_thread_id><input>${args.prompt}</input></codex_delegation>` } };
      if (active) active.items.push(item);
      else tasks.get(args.threadId).turns.push({ id: `turn-${++count}`, status: "inProgress", items: [item] });
      return { threadId: args.threadId };
    }
    if (name === "create_thread") { const id = `created-${++count}`; add(id); return { threadId: id }; }
    if (name === "fork_thread") {
      const id = `fork-${++count}`; add(id, structuredClone(tasks.get(args.threadId).turns));
      tasks.get(id).thread.forkedFromId = args.threadId; return { threadId: id };
    }
    if (name === "set_thread_title") { tasks.get(args.threadId).thread.title = args.title; return { updated: true }; }
    throw new Error(`Unexpected tool ${name}`);
  };
  const listNativeTools = async () => ["read_thread", "send_message_to_thread", "create_thread", "fork_thread", "set_thread_title", "list_threads"].map(name => ({ name }));
  const config = { token, port: 0, pollMs: 1000000, projectId: "project-test", workspaceRoots: [root], nativeSenderThreadId: "manager-test", messageSourceLabels: false,
    ...overrides, feishu: { onlineStableMs: 0, ...overrides.feishu } };
  const manager = await new DesktopManager({ native, listNativeTools, stateDir: root, config,
    fetchImpl: async () => ({ ok: true }) }).initialize();
  const url = await manager.listen();
  const http = async (route, body, method = body ? "POST" : "GET", authorization = `Bearer ${token}`) => {
    const response = await fetch(url + route, { method, headers: { authorization, "content-type": "application/json" }, ...(body ? { body: JSON.stringify(body) } : {}) });
    return { status: response.status, body: await response.json() };
  };
  t.after(async () => { await manager.close(); assert.ok(root.startsWith(os.tmpdir())); await rm(root, { recursive: true, force: true }); });
  const register = async (id = "existing", extra = {}) => manager.register({ protocolVersion: PROTOCOL, messageId: randomUUID(), source: "trainer", threadId: id, ...extra });
  const finish = (id = "existing", text = "桌面回复") => {
    const turn = tasks.get(id).turns.at(-1); turn.status = "completed";
    turn.items.push({ type: "agentMessage", phase: "final_answer", text });
  };
  return { root, manager, tasks, calls, http, url, native, listNativeTools, config, register, finish };
}

test("handoff drains the current slow poll and prevents further sessions or intervals from starving it", async t => {
  const f = await fixture(t); await f.register();
  for (let i = 0; i < 5; i++) {
    const id = `extra-${i}`;
    f.tasks.set(id, { thread: { id, title: id, cwd: f.root }, turns: [] });
    await f.register(id);
  }
  let release, reads = 0;
  f.manager.native = async (...args) => {
    if (args[0] === "read_thread") { reads++; await new Promise(resolve => { release = resolve; }); }
    return f.native(...args);
  };
  const poll = f.manager.scan();
  assert.equal(f.manager.requestHotHandoff(), true);
  assert.equal(f.manager.hotHandoffStatus().blockers.poll, 1);
  for (let i = 0; i < 20; i++) await f.manager.scan();
  release(); await poll;
  assert.equal(reads, 1); assert.equal(f.manager.requestHotHandoff(), false);
  assert.equal(f.manager.hotHandoffStatus().phase, "ready");
  for (let i = 0; i < 10; i++) assert.equal((await f.http("/health")).status, 200);
  assert.equal(f.manager.hotBusy(), false);
  const rejected = await f.http("/manager/messages", req("not-admitted"));
  assert.equal(rejected.status, 503); assert.equal(rejected.body.error, "bridge_reloading");
  assert.equal(rejected.body.retryable, true); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  f.manager.resumeHotHandoff();
});

test("local Feishu monitoring keeps its selected native task and public replies", async t => {
  const f = await fixture(t, { operatingMode: "feishu-local",
    feishu: { trainerOpenIdWhitelist: ["trainer"], streamEnabled: false } });
  await f.register(); const cards = [];
  f.manager.notifier = { addReaction: async () => null, removeReaction: async () => true,
    sendMessage: async text => { cards.push(text); return { code: 0 }; } };
  await f.manager.receiveFeishu({ chat_type: "p2p", sender_type: "user", message_type: "text",
    content: "本机监控输入", message_id: "local-monitor-input", open_id: "trainer", chat_id: "oc_test" });
  await f.manager.scan(); f.finish("existing", "本机公开回复"); await f.manager.scan();
  assert.ok(cards.includes("本机公开回复")); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  assert.equal(f.manager.health().operatingMode, "feishu-local");
});

test("handoff accepts and persists new Feishu inbox entries but defers dispatch with the original UUID", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  assert.equal(f.manager.requestHotHandoff(), false);
  const event = { message_id: "during-handoff", chat_id: "oc_test", chat_type: "p2p", sender_type: "user",
    open_id: "trainer", message_type: "text", content: "durable during handoff" };
  await f.manager.enqueueFeishu(event);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  assert.equal(JSON.parse(await readFile(f.manager.inboxPath, "utf8")).entries[event.message_id].content, event.content);
  f.manager.resumeHotHandoff();
  const until = Date.now() + 3000;
  while (Object.keys(f.manager.inbox.entries).length && Date.now() < until) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  assert.equal(Object.keys(f.manager.inbox.entries).length, 0);
  const id = Object.keys(f.manager.state.receipts)[0];
  assert.ok(id); await f.manager.enqueueFeishu(event);
  await Promise.allSettled([...f.manager.backgroundTasks]);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  assert.ok(f.manager.state.receipts[id]);
});

test("handoff timeout resumes the healthy worker without cancelling an in-flight native write", async t => {
  const f = await fixture(t, { hotHandoffTimeoutMs: 50, hotHandoffRetryMs: 300 }); await f.register();
  let release, started;
  const sending = new Promise(resolve => { started = resolve; });
  f.manager.native = async (...args) => {
    if (args[0] === "send_message_to_thread") { started(); await new Promise(resolve => { release = resolve; }); }
    return f.native(...args);
  };
  const message = req("original-write"); const result = f.manager.message(message);
  await sending;
  assert.equal(f.manager.requestHotHandoff(), true);
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal(f.manager.handoffPaused, false);
  assert.equal(f.manager.hotHandoffStatus().error, "handoff_timeout");
  assert.equal(f.manager.hotHandoffStatus().blockers.native, 1);
  assert.equal(f.manager.requestHotHandoff(), true); // backoff, no new pause
  assert.equal(f.manager.handoffPaused, false);
  release(); await result;
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  assert.equal((await f.manager.message(message)).duplicate, true);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
});

test("a rejected candidate promptly resumes background work even if its error repeats", async t => {
  const f = await fixture(t); await f.register();
  let status = { lastReloadError: "runtime_reload_failed", reloadPending: false };
  assert.equal(f.manager.requestHotHandoff(() => status), false);
  status = { lastReloadError: "runtime_reload_failed", reloadPending: false };
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.equal(f.manager.handoffPaused, false);
  assert.equal(f.manager.hotHandoffStatus().error, "candidate_rejected");
  const before = f.calls.length; await f.manager.scan(); assert.ok(f.calls.length > before);
});

test("resuming a timed-out handoff never overlaps the previous online notice operation", async t => {
  const f = await fixture(t); await f.register();
  let release, runs = 0;
  const socket = { connectionId: "notice-drain", _ws: { readyState: 1 }, stop() {} };
  f.manager.ws = socket;
  const announcement = f.manager.onlineAnnouncement;
  announcement.record = { connectionId: socket.connectionId, status: "pending" };
  announcement.task = new Promise(resolve => { release = resolve; });
  announcement.run = async () => { runs++; };
  const resumed = announcement.resume();
  await Promise.resolve(); assert.equal(runs, 0);
  release(); await resumed; assert.equal(runs, 1);
  f.manager.ws = null;
});

async function autoImageFixture(t, overrides = {}) {
  const f = await fixture(t, overrides); await f.register();
  const outbound = [], notices = [];
  f.manager.notifier = { enabled: true, chatId: "oc_test", getToken: async () => "test-only-token",
    invalidateToken() {}, sendMessage: async text => { notices.push(text); return { code: 0 }; } };
  f.manager.fetch = async (url, options) => {
    outbound.push({ url, options });
    return { ok: true, json: async () => ({ code: 0, data: url.includes("/images") ? { image_key: `img_${outbound.length}` } : { message_id: `om_${outbound.length}` } }) };
  };
  const image = async (name = "result.png") => { const filename = path.join(f.root, name);
    await writeFile(filename, Buffer.from("89504e470d0a1a0a0000000049454e44", "hex")); return filename; };
  const add = (filename, id = "existing", turnId = "image-turn") => {
    let turn = f.tasks.get(id).turns.find(t => t.id === turnId);
    if (!turn) { turn = { id: turnId, status: "completed", items: [] }; f.tasks.get(id).turns.push(turn); }
    turn.items.push({ type: "imageGeneration", id: randomUUID(), status: "completed", savedPath: filename });
    return turn;
  };
  const scan = async () => { await f.manager.scan(); await f.manager.publicImageRelay.task; };
  return { ...f, outbound, notices, image, add, scan };
}

test("handoff finishes the current image and preserves the remaining ordered UUID queue", async t => {
  const f = await autoImageFixture(t);
  for (let i = 0; i < 3; i++) f.add(await f.image(`handoff-${i}.png`));
  let release, started;
  const uploading = new Promise(resolve => { started = resolve; });
  const originalFetch = f.manager.fetch;
  f.manager.fetch = async (...args) => {
    if (!release) { started(); await new Promise(resolve => { release = resolve; }); }
    return originalFetch(...args);
  };
  await f.manager.scan(); await uploading;
  const ids = Object.values(f.manager.state.autoImages).map(e => e.messageId);
  assert.equal(f.manager.requestHotHandoff(), true);
  release(); await f.manager.publicImageRelay.task;
  assert.deepEqual(Object.values(f.manager.state.autoImages).map(e => e.status), ["sent", "pending", "pending"]);
  assert.equal(f.manager.requestHotHandoff(), false);
  f.manager.resumeHotHandoff(); await f.manager.publicImageRelay.kick();
  assert.deepEqual(Object.values(f.manager.state.autoImages).map(e => e.messageId), ids);
  assert.ok(Object.values(f.manager.state.autoImages).every(e => e.status === "sent"));
  assert.equal(f.outbound.length, 6);
});

test("automatic image relay sends ten displayed images in order once without native writes", async t => {
  const f = await autoImageFixture(t);
  for (let i = 0; i < 10; i++) {
    const filename = await f.image(`result-${i}.png`), turn = f.add(filename);
    turn.items.push({ type: "imageView", path: filename }, { type: "agentMessage", text: `![preview](<${filename}>)`, phase: "final_answer" });
  }
  await f.scan(); await f.scan();
  assert.equal(f.outbound.length, 20);
  const receipts = Object.values(f.manager.state.autoImages);
  assert.equal(receipts.length, 10); assert.ok(receipts.every(e => e.status === "sent" && !e.imagePath));
  assert.deepEqual(f.outbound.filter(c => c.url.includes("/messages")).map(c => JSON.parse(c.options.body).uuid), receipts.map(e => e.messageId));
  assert.ok(!f.calls.some(c => c.name === "send_message_to_thread"));
  assert.equal(f.manager.health().capabilities.feishuPublicImageSync, "selected-thread-public-images");
});

test("adoption and upgrade seed old images; only subsequent public output is relayed", async t => {
  const f = await autoImageFixture(t); const filename = await f.image();
  f.add(filename, "existing", "old-turn"); delete f.manager.session().seenImages;
  await f.scan(); assert.equal(f.outbound.length, 0);
  f.add(filename, "existing", "new-turn"); await f.scan(); assert.equal(f.outbound.length, 2);
  f.tasks.set("adopted", { thread: { id: "adopted", cwd: f.root }, turns: [{ id: "past", status: "completed", items: [{ type: "imageView", path: filename }] }] });
  await f.register("adopted"); await f.scan(); assert.equal(f.outbound.length, 2);
});

test("switches follow the selected chat, suppress unread old images and never replay them on return", async t => {
  const f = await autoImageFixture(t); const filename = await f.image();
  f.tasks.set("other", { thread: { id: "other", cwd: f.root }, turns: [] }); await f.register("other");
  f.add(filename, "existing"); f.add(filename, "other"); await f.scan();
  assert.equal(f.outbound.length, 2); assert.equal(Object.values(f.manager.state.autoImages)[0].threadId, "other");
  f.add(filename, "existing", "unread-while-away");
  await f.manager.message(req("/switch existing")); await f.scan(); assert.equal(f.outbound.length, 2);
  f.add(filename, "existing", "new-selected-output"); await f.scan(); assert.equal(f.outbound.length, 4);
});

test("selection changes during native read and upload suppress old pictures, including away-and-back", async t => {
  const f = await autoImageFixture(t); const filename = await f.image(); f.add(filename);
  f.tasks.set("other", { thread: { id: "other", cwd: f.root }, turns: [] });
  // Change and restore selection while the first upload is in flight.
  const fetchImage = f.manager.fetch;
  f.manager.fetch = async (...args) => { const response = await fetchImage(...args);
    if (args[0].includes("/images")) { f.manager.selectSession("other"); f.manager.selectSession("existing"); }
    return response; };
  await f.scan(); assert.equal(f.outbound.length, 1); assert.equal(Object.values(f.manager.state.autoImages)[0].status, "canceled");
  await f.scan(); assert.equal(f.outbound.length, 1);
  const native = f.manager.native; f.add(filename, "existing", "read-race");
  f.manager.native = async (...args) => { const result = await native(...args);
    if (args[0] === "read_thread") { f.manager.selectSession("other"); f.manager.selectSession("existing"); } return result; };
  await f.scan(); assert.equal(f.outbound.length, 1);
});

test("automatic image upload does not hold native ingress lock and keeps hot handoff busy", async t => {
  const f = await autoImageFixture(t); const filename = await f.image(); f.add(filename);
  let release, entered; const waiting = new Promise(resolve => { entered = resolve; }); const fetchImage = f.manager.fetch;
  f.manager.fetch = async (...args) => { if (args[0].includes("/images")) { entered(); await new Promise(resolve => { release = resolve; }); } return fetchImage(...args); };
  await f.manager.scan(); await waiting;
  assert.equal(f.manager.hotBusy(), true);
  const result = await f.manager.message(req("native text while uploading", { delivery: "steer" }));
  assert.equal(result.action, "dispatch_pending");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  release(); await f.manager.publicImageRelay.task; assert.equal(f.outbound.length, 2);
});

test("automatic image errors are visible without exposing local paths or blindly retrying", async t => {
  const f = await autoImageFixture(t); f.add(path.join(os.tmpdir(), `outside-${randomUUID()}.png`));
  f.add(await f.image("not-an-image.txt")); // overwrite valid signature with non-image bytes
  await writeFile(path.join(f.root, "not-an-image.txt"), "not image");
  await f.scan(); await f.scan(); assert.equal(f.outbound.length, 0);
  assert.equal(f.manager.integrationStatus().feishu.publicImages.failed, 2);
  assert.equal(f.notices.length, 2); assert.ok(!f.notices.join().includes(f.root));
  assert.ok(Object.values(f.manager.state.autoImages).every(e => !e.imagePath));
});

test("disabled auto relay advances seen markers rather than replaying old output when enabled", async t => {
  const f = await autoImageFixture(t, { feishu: { autoImageOutput: false } }); const filename = await f.image(); f.add(filename);
  await f.scan(); f.manager.config.feishu.autoImageOutput = true; await f.scan(); assert.equal(f.outbound.length, 0);
  f.add(filename, "existing", "later"); await f.scan(); assert.equal(f.outbound.length, 2);
});

test("automatic relay excludes the Feishu input cache even when displayed by imageView", async t => {
  const f = await autoImageFixture(t);
  f.tasks.get("existing").turns.push({ id: "view-input", status: "completed", items: [{ type: "imageView", path: path.join(f.root, "feishu-images", "input.png") }] });
  await f.scan(); assert.equal(f.outbound.length, 0); assert.equal(Object.keys(f.manager.state.autoImages).length, 0);
});

test("automatic relay retains its durable UUID when an explicit send temporarily holds the image lease", async t => {
  const f = await autoImageFixture(t); f.add(await f.image());
  const send = f.manager.sendImage.bind(f.manager); let calls = 0;
  f.manager.sendImage = async (...args) => { if (++calls === 1) { const error = new Error("busy"); error.body = { error: "image_output_busy", retryable: true }; throw error; } return send(...args); };
  await f.scan(); const uuid = Object.values(f.manager.state.autoImages)[0].messageId;
  assert.equal(Object.values(f.manager.state.autoImages)[0].status, "pending");
  await f.scan(); assert.equal(f.outbound.length, 2); assert.equal(Object.values(f.manager.state.autoImages)[0].messageId, uuid);
});

test("manual and automatic images accept exact files outside legacy roots without expanding roots", async t => {
  const f = await autoImageFixture(t);
  const allowed = f.manager.generatedImageRoot("existing"); assert.equal(allowed.length, 1);
  assert.equal(path.basename(allowed[0]), "existing"); assert.equal(path.basename(path.dirname(allowed[0])), "generated_images");
  assert.deepEqual(f.manager.generatedImageRoot("../other"), []);
  const filename = await f.image(); f.manager.config.workspaceRoots = [path.join(f.root, "other-workspace")];
  f.manager.generatedImageRoot = id => id === "existing" ? [f.root] : [];
  f.add(filename); await f.scan(); assert.equal(f.outbound.length, 2);
  f.manager.generatedImageRoot = () => [path.join(f.root, "other-chat")];
  assert.equal((await f.manager.sendImage({ imagePath: filename, threadId: "existing", messageId: randomUUID() })).status, "sent");
  f.add(filename, "existing", "cross-chat"); await f.scan();
  assert.equal(f.outbound.length, 6); assert.equal(Object.values(f.manager.state.autoImages).at(-1).status, "sent");
  assert.equal(f.manager.health().capabilities.feishuPublicImagePathPolicy, "exact-public-display");
});

test("Wiki public previews auto-send and authenticated manual sends accept any exact local image without override fields", async t => {
  const f = await autoImageFixture(t); const filename = await f.image("wiki-original.png");
  f.manager.config.workspaceRoots = [path.join(f.root, "configured-project")];
  const body = { imagePath: filename, threadId: "existing", messageId: randomUUID() };
  assert.equal((await f.http("/bridge/images", body, "POST", "wrong")).status, 401);
  assert.equal((await f.http("/bridge/images", body)).body.status, "sent");
  for (const key of ["displayedFiles", "publicEntry", "force", "allowedRoots"]) {
    assert.equal((await f.http("/bridge/images", { ...body, [key]: [filename] })).status, 400);
  }
  await assert.rejects(f.manager.sendImage(body, () => {}, { ...body, status: "pending" }),
    e => e.body.error === "image_display_not_authorized");
  f.tasks.get("existing").turns.push({ id: "wiki-preview", status: "completed", items: [
    { type: "agentMessage", text: `![archived UI](<${filename}>)`, phase: "final_answer" }
  ] });
  await f.scan(); await f.scan(); assert.equal(f.outbound.length, 4);
  assert.equal((await f.http("/bridge/images", body)).body.duplicate, true);
  assert.equal(f.outbound.length, 4);
});

test("old pre-upload path rejection recovers once from fresh same-binding public history with its original UUID", async t => {
  const f = await autoImageFixture(t); const filename = await f.image(); f.add(filename);
  const send = f.manager.sendImage.bind(f.manager);
  f.manager.sendImage = async () => { const e = new Error("legacy root check"); e.body = { error: "image_path_forbidden" }; throw e; };
  await f.scan(); const entry = Object.values(f.manager.state.autoImages)[0], uuid = entry.messageId;
  delete entry.publicDisplayPolicy; // persisted 0.2.15 record, not a new-policy rejection
  f.manager.sendImage = send;
  await f.scan(); await f.scan();
  assert.equal(entry.status, "sent"); assert.equal(entry.messageId, uuid);
  assert.equal(entry.previousFailure.error, "image_path_forbidden");
  assert.equal(f.outbound.length, 2); assert.equal(JSON.parse(f.outbound[1].options.body).uuid, uuid);
});

test("path rejection recovery cannot resurrect a former binding or sent/unknown failures", async t => {
  const f = await autoImageFixture(t); const filename = await f.image(); f.add(filename);
  await f.scan(); const entry = Object.values(f.manager.state.autoImages)[0];
  for (const status of ["sent", "failed", "canceled"]) {
    Object.assign(entry, { status, error: "image_send_outcome_unknown" }); delete entry.publicDisplayPolicy;
    await f.scan(); assert.equal(f.outbound.length, 2);
  }
  Object.assign(entry, { status: "failed", error: "image_path_forbidden", bindingEpoch: f.manager.state.selectionEpoch - 1 });
  await f.scan(); assert.equal(f.outbound.length, 2); assert.equal(entry.status, "failed");
});

test("automatic image restart uses the durable UUID and sent ledger, not a second message", async t => {
  const f = await autoImageFixture(t); const filename = await f.image(); f.add(filename); await f.scan();
  const entry = Object.values(f.manager.state.autoImages)[0];
  Object.assign(entry, { status: "pending", imagePath: filename, chatId: "oc_test" }); await f.manager.save();
  await f.manager.close();
  const restored = await new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config, fetchImpl: f.manager.fetch }).initialize();
  t.after(() => restored.close()); restored.notifier = f.manager.notifier;
  await restored.scan(); await restored.publicImageRelay.task;
  assert.equal(f.outbound.length, 2); assert.equal(Object.values(restored.state.autoImages)[0].status, "sent");
});

test("automatic image unknown outcome stays failed across restart after bounded same-UUID recovery", async t => {
  const f = await autoImageFixture(t); const filename = await f.image(); f.add(filename);
  const fetchImage = f.manager.fetch;
  f.manager.fetch = async (...args) => { if (args[0].includes("/messages")) { f.outbound.push({ url: args[0] }); throw new Error("network lost after send"); } return fetchImage(...args); };
  await f.scan(); assert.equal(f.outbound.length, 4); assert.equal(Object.values(f.manager.state.autoImages)[0].error, "image_send_outcome_unknown");
  // Simulate interruption before the auto-relay terminal record was saved.
  const entry = Object.values(f.manager.state.autoImages)[0];
  Object.assign(entry, { status: "pending", imagePath: filename, chatId: "oc_test" }); await f.manager.save(); await f.manager.close();
  const restored = await new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config, fetchImpl: f.manager.fetch }).initialize();
  t.after(() => restored.close()); restored.notifier = f.manager.notifier;
  await restored.scan(); await restored.publicImageRelay.task;
  assert.equal(f.outbound.length, 4); assert.equal(Object.values(restored.state.autoImages)[0].status, "failed");
});

test("image HTTP output is authorized, selected-only and never writes a native task", async t => {
  const f = await fixture(t); await f.register();
  const imagePath = path.join(f.root, "image.png"); await writeFile(imagePath, Buffer.from("89504e470d0a1a0a0000000049454e44", "hex"));
  f.manager.notifier = { enabled: true, chatId: "oc_test", getToken: async () => "test-only-image-token", invalidateToken() {} };
  const imageCalls = [];
  f.manager.fetch = async (url, options) => { imageCalls.push({ url, options }); return { ok: true,
    json: async () => ({ code: 0, data: url.includes("/images") ? { image_key: "img_test" } : { message_id: "om_test" } }) }; };
  const request = { imagePath, threadId: "existing", messageId: randomUUID() };
  const before = f.calls.length; const selected = f.manager.state.selectedThreadId;
  assert.equal((await f.http("/bridge/images", request, "POST", "wrong")).status, 401);
  const sent = await f.http("/bridge/images", request);
  assert.equal(sent.status, 200); assert.equal(sent.body.status, "sent");
  assert.equal((await f.http("/bridge/images", request)).body.duplicate, true); assert.equal(imageCalls.length, 2);
  assert.equal((await f.http("/bridge/images", { ...request, threadId: "missing" })).status, 404);
  assert.equal((await f.http("/bridge/images", { ...request, chatId: "oc_arbitrary" })).status, 400);
  assert.equal(f.calls.length, before); assert.equal(f.manager.state.selectedThreadId, selected);
  assert.equal((await f.http("/bridge/runtime")).body.scope, "feishu-image-output");
  assert.equal((await f.http("/bridge/runtime/reload", { sourceRoot: f.root })).status, 400);
  assert.equal((await f.http("/bridge/runtime/reload", {})).body.error, "runtime_source_unavailable");
  assert.equal(f.manager.health().managerReady, false); assert.equal(f.manager.health().nativeTaskReady, false);
});

test("Windows restart ignores a reused diagnostic PID only after acquiring the authoritative OS lease", { skip: process.platform !== "win32" }, async t => {
  const f = await fixture(t); await f.manager.close();
  await writeFile(path.join(f.root, "desktop-manager.lock"), JSON.stringify({ pid: process.pid, nonce: "old-boot", startedAt: "2026-10-02T00:00:00Z" }));
  const recovered = new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config });
  t.after(() => recovered.close()); await recovered.initialize();
  const lock = JSON.parse(await readFile(path.join(f.root, "desktop-manager.lock"), "utf8"));
  assert.notEqual(lock.nonce, "old-boot"); assert.equal(lock.pid, process.pid);
});

test("public questions include complete options and are never inferred from private tool history", () => {
  const q = normalizeQuestion({ questionId: randomUUID(), threadId: "existing", questions: [{ title: "看到正文了吗？", options: ["看到了", "只有标题"] }], secret: "not-forwarded" });
  const text = questionText(q, "CodexWorker");
  assert.ok(text.includes("看到正文了吗？")); assert.ok(text.includes("1）看到了")); assert.ok(text.includes("2）只有标题"));
  assert.ok(!JSON.stringify(q).includes("not-forwarded"));
  for (const invalid of [{ ...q, questionId: "not-uuid" }, { ...q, questions: [] }, { ...q, questions: [{ title: "" }] },
    { ...q, questions: [{ title: "hi", options: ["one"] }] }]) assert.throws(() => normalizeQuestion(invalid), /required|nonempty/);
});

test("image resources are bounded and downloaded only from their matched official message-resource URL", async () => {
  const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1WkAAAAASUVORK5CYII=", "base64");
  let requested;
  const bytes = await downloadFeishuImage("om_valid", "img_valid", "test-secret", { fetchImpl: async (url, options) => {
    requested = { url, options }; return new Response(png, { headers: { "content-type": "image/png" } });
  } });
  assert.deepEqual(bytes, png); assert.equal(imageExtension(bytes), "png");
  assert.equal(requested.url, "https://open.feishu.cn/open-apis/im/v1/messages/om_valid/resources/img_valid?type=image");
  assert.equal(requested.options.redirect, "error"); assert.equal(requested.options.headers.authorization, "Bearer test-secret");
  for (const pair of [["../secret", "img_valid"], ["om_valid", "https://internal"], ["om_valid", "img_../escape"]]) {
    await assert.rejects(downloadFeishuImage(...pair, "test-secret"), /实际 message_id/);
  }
  assert.throws(() => imageExtension(Buffer.from("<svg><script>not-image</script></svg>")), /不是可查看/);
});

test("image download rejects API failure, redirect and oversized or incomplete binary streams", async () => {
  const get = fetchImpl => downloadFeishuImage("om_valid", "img_valid", "test-secret", { fetchImpl });
  await assert.rejects(get(async () => new Response('{"code":99991664}', { status: 401, headers: { "content-type": "application/json" } })), /无法读取/);
  await assert.rejects(get(async () => { throw new Error("redirect"); }), /下载未完成/);
  await assert.rejects(get(async () => new Response("x", { headers: { "content-length": String(MAX_IMAGE_BYTES + 1) } })), /10MiB/);
  await assert.rejects(get(async () => new Response(Buffer.alloc(MAX_IMAGE_BYTES + 1))), /10MiB/);
  await assert.rejects(get(async () => new Response(new ReadableStream({ start(c) { c.error(new Error("broken")); } }))), /传输中断/);
});

test("authorized Feishu images persist their actual bytes and native prompts reference a local viewable file only once", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1WkAAAAASUVORK5CYII=", "base64");
  let downloads = 0;
  f.manager.notifier = { addReaction: async () => null, removeReaction: async () => true, sendMessage: async () => ({ code: 0 }),
    downloadImage: async (id, key) => { downloads++; assert.equal(id, "om_incoming_image"); assert.equal(key, "img_incoming"); return png; } };
  const event = { open_id: "trainer", chat_type: "p2p", sender_type: "user", message_type: "image", message_id: "om_incoming_image",
    chat_id: "trainer-chat", content: JSON.stringify({ image_key: "img_incoming" }) };
  const first = await f.manager.receiveFeishu(event); const repeat = await f.manager.receiveFeishu(event);
  assert.equal(repeat.duplicate, true); assert.equal(downloads, 1);
  const record = f.manager.state.feishuReceipts[first.messageId]; assert.ok(record.imagePath.startsWith(path.join(f.root, "feishu-images")));
  assert.deepEqual(await readFile(record.imagePath), png);
  const sends = f.calls.filter(c => c.name === "send_message_to_thread"); assert.equal(sends.length, 1);
  assert.ok(sends[0].args.prompt.includes(record.imagePath)); assert.ok(sends[0].args.prompt.includes("view_image"));
  assert.ok(!sends[0].args.prompt.includes("img_incoming")); assert.ok(!sends[0].args.prompt.includes(first.messageId));
});

test("unauthorized or remote-host images cannot download resources or send inaccessible local paths", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  let downloads = 0; f.manager.notifier = { addReaction: async () => null, sendMessage: async () => ({ code: 0 }), downloadImage: async () => { downloads++; } };
  const event = { open_id: "stranger", chat_type: "p2p", sender_type: "user", message_type: "image", message_id: "om_private",
    content: JSON.stringify({ image_key: "img_private" }), chat_id: "trainer-chat" };
  await f.manager.receiveFeishu(event); assert.equal(downloads, 0);
  f.manager.session().hostId = "remote-notebook";
  await assert.rejects(f.manager.receiveFeishu({ ...event, open_id: "trainer" }), /暂不能/);
  assert.equal(downloads, 0); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("failed image downloads tell the trainer and never pretend a placeholder is an accepted native input", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  const notices = []; f.manager.notifier = { addReaction: async () => null, sendMessage: async text => { notices.push(text); return { code: 0 }; },
    downloadImage: async () => { throw Object.assign(new Error("download failed"), { body: { error: "image_download_failed", detail: "未获取图片本体" } }); } };
  await assert.rejects(f.manager.receiveFeishu({ open_id: "trainer", chat_type: "p2p", sender_type: "user", message_type: "image", message_id: "om_failed",
    content: JSON.stringify({ image_key: "img_failed" }), chat_id: "trainer-chat" }));
  assert.ok(notices.some(s => s.includes("未获取图片本体"))); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("a slow image download cannot spill an earlier picture into a newly selected chat", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  let release; let began; const started = new Promise(resolve => { began = resolve; });
  const bytes = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1WkAAAAASUVORK5CYII=", "base64");
  f.manager.notifier = { addReaction: async () => null, sendMessage: async () => ({ code: 0 }),
    downloadImage: async () => { began(); await new Promise(resolve => { release = resolve; }); return bytes; } };
  const incoming = f.manager.receiveFeishu({ open_id: "trainer", chat_type: "p2p", sender_type: "user", message_type: "image", message_id: "om_slow",
    content: JSON.stringify({ image_key: "img_slow" }), chat_id: "trainer-chat" });
  await started; f.tasks.set("new-selected", { thread: { id: "new-selected", cwd: f.root }, turns: [] }); await f.register("new-selected");
  release(); await assert.rejects(incoming, /no fallback/);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("question relay publishes to the existing Feishu channel once without starting another native turn", async t => {
  const f = await fixture(t, { feishu: { chatId: "trainer-chat" } }); await f.register();
  const cards = []; f.manager.notifier = { sendMessage: async (text, options) => { cards.push({ text, options }); return { code: 0 }; } };
  const q = { questionId: randomUUID(), threadId: "existing", questions: [{ title: "提问卡片正文可见吗？", options: ["可见", "不可见"] }] };
  const first = await f.http("/bridge/questions", q);
  assert.equal(first.status, 200); assert.equal(first.body.feishu, "sent");
  assert.equal(first.body.duplicate, false); assert.ok(cards[0].text.includes("1）可见")); assert.ok(cards[0].text.includes("2）不可见"));
  assert.equal(cards[0].options.title, "CodexWorker-提问"); assert.equal(cards[0].options.chatId, "trainer-chat");
  const repeat = await f.http("/bridge/questions", q); assert.equal(repeat.body.duplicate, true);
  assert.equal(cards.length, 1); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  assert.equal((await f.http("/bridge/questions", { ...q, questions: [{ title: "different" }] })).status, 409);
});

test("question relay is authenticated and does not publish to an old selected target", async t => {
  const f = await fixture(t, { feishu: { chatId: "trainer-chat" } }); await f.register();
  const q = { questionId: randomUUID(), threadId: "existing", questions: [{ title: "where?" }] };
  assert.equal((await f.http("/bridge/questions", q, "POST", "wrong")).status, 401);
  f.tasks.set("other", { thread: { id: "other", cwd: f.root }, turns: [] }); await f.register("other");
  assert.equal((await f.http("/bridge/questions", q)).status, 409);
});

test("question relay reports channel failure honestly and retries only an unsent question card", async t => {
  const f = await fixture(t, { feishu: { chatId: "trainer-chat" } }); await f.register();
  let sends = 0; f.manager.notifier = { sendMessage: async () => ++sends === 1 ? null : { code: 0 } };
  const q = { questionId: randomUUID(), threadId: "existing", questions: [{ title: "question stays pending" }] };
  const first = await f.manager.questions.publish(q);
  assert.equal(first.feishu, "pending");
  const retry = await f.manager.questions.publish(q); assert.equal(retry.duplicate, true);
  assert.equal(retry.feishu, "sent");
  await f.manager.questions.publish(q); assert.equal(sends, 2);
});

test("question relay concurrent exact retries share one send and preserve durable receipts across reload", async t => {
  const f = await fixture(t, { feishu: { chatId: "trainer-chat" } }); await f.register();
  let sends = 0; f.manager.notifier = { sendMessage: async () => { sends++; await new Promise(r => setTimeout(r, 10)); return { code: 0 }; } };
  const q = { questionId: randomUUID(), threadId: "existing", questions: [{ title: "same question" }] };
  const results = await Promise.all([f.manager.questions.publish(q), f.manager.questions.publish(q)]);
  assert.equal(sends, 1); assert.equal(results[1].duplicate, true);
  f.manager.state = JSON.parse(await readFile(f.manager.statePath, "utf8"));
  assert.equal((await f.manager.questions.publish(q)).duplicate, true); assert.equal(sends, 1);
});

test("an external plain-text answer still enters the selected native chat, not a hidden Codex panel", async t => {
  const f = await fixture(t, { feishu: { chatId: "trainer-chat", trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  f.manager.notifier = { sendMessage: async () => ({ code: 0 }) };
  await f.manager.questions.publish({ questionId: randomUUID(), threadId: "existing", questions: [{ title: "看到了吗？", options: ["看到了", "没看到"] }] });
  await f.manager.receiveFeishu({ open_id: "trainer", chat_type: "p2p", sender_type: "user", message_type: "text",
    message_id: "answer-1", chat_id: "trainer-chat", content: "看到了" });
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  assert.equal(f.calls.find(c => c.name === "send_message_to_thread").args.prompt, "看到了");
});

test("question callback integration ACK persists promptly despite a blocked native message lock", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, feishu: { chatId: "trainer-chat", trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  f.tasks.get("existing").turns.push({ id: "source-question-turn", status: "inProgress", items: [] });
  f.manager.notifier = { sendQuestionCard: async () => ({ code: 0, data: { message_id: "om_real_fixture_card" } }), updateQuestionCard: async () => ({ code: 0 }) };
  const q = { questionId: randomUUID(), threadId: "existing", questions: [{ title: "选哪个？", options: ["甲", "乙"] }] };
  await f.manager.questions.publish(q); let release;
  const lock = f.manager.serial(() => new Promise(resolve => { release = resolve; }));
  while (!release) await new Promise(r => setTimeout(r, 1));
  const click = { header: { event_type: "card.action.trigger", event_id: randomUUID() }, event: { operator: { open_id: "trainer" },
    context: { open_message_id: "om_real_fixture_card", open_chat_id: "trainer-chat" }, action: { value: {
      bridgeQuestion: q.questionId, nonce: f.manager.questions.state.entries[q.questionId].nonce, action: "select", questionIndex: 0, optionIndex: 1 } } } };
  const accepted = await Promise.race([f.manager.questions.acceptCard(click), new Promise((_, reject) => setTimeout(() => reject(new Error("slow ACK")), 1000).unref())]);
  assert.equal(accepted.toast.type, "success"); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  release(); await lock; await f.manager.questions.process();
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  assert.ok(f.calls.find(c => c.name === "send_message_to_thread").args.prompt.includes("答案：乙"));
  assert.equal(f.manager.questions.get(q.questionId).status, "answered");
});

test("real message dispatcher rejects stale source turns/ABA bindings immediately before native write", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true }); await f.register();
  const request = req("clarification answer", { source: "trainer", targetThreadId: "existing" });
  await assert.rejects(f.manager.message(request, { requireSourceTurnId: "ended-turn", requireSelectedTarget: true }), e => e.body.error === "source_turn_ended");
  f.tasks.get("existing").turns.push({ id: "active-question", status: "inProgress", items: [] });
  const epoch = f.manager.state.selectionEpoch;
  f.manager.native = async (...args) => { const value = await f.native(...args); if (args[0] === "read_thread") f.manager.state.selectionEpoch += 2; return value; };
  await assert.rejects(f.manager.message({ ...request, messageId: randomUUID() }, { requireSourceTurnId: "active-question", requireSelectionEpoch: epoch }), e => e.body.error === "selected_target_changed");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("all HTTP endpoints including health require Bearer auth; full manager gate stays closed", async t => {
  const f = await fixture(t);
  assert.equal((await f.http("/health", null, "GET", "wrong")).status, 401);
  const health = (await f.http("/health")).body;
  assert.equal(health.integrationMode, "desktop-native"); assert.equal(health.ready, true);
  assert.equal(health.managerReady, false); assert.equal(health.capabilities.stop, false);
});

test("DSH wait responses pair every message in a shared active turn, not only its first messageId", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const inputs = [req("first"), req("second")]; for (const input of inputs) await f.manager.message(input);
  await f.manager.scan(); f.finish(); await f.manager.scan();
  const results = await Promise.all(inputs.map(r => f.manager.waitResult(r.messageId, "existing")));
  assert.ok(results.every(r => r.status === "completed" && r.resultText === "桌面回复"));
  assert.deepEqual(results.map(r => r.messageId), inputs.map(r => r.messageId));
});

test("resume forwards only the selected chat's progress and final reply without stopping other chats", async t => {
  const f = await fixture(t); await f.register();
  f.tasks.set("new-selected", { thread: { id: "new-selected", title: "new-selected", cwd: f.root, updatedAt: 100 }, turns: [] });
  await f.manager.message(req("/resume")); await f.manager.message(req("/resume 0"));
  const sent = []; f.manager.notifier = { sendMessage: async (text, options) => { sent.push({ text, threadId: options.threadId }); return { code: 0 }; } };
  for (const id of ["existing", "new-selected"]) f.tasks.get(id).turns.push({ id: "output-" + id, status: "completed", items: [
    { id: "progress", type: "agentMessage", phase: "commentary", text: "progress-" + id },
    { id: "final", type: "agentMessage", phase: "final_answer", text: "final-" + id } ] });
  await f.manager.scan(); await f.manager.tail;
  assert.deepEqual(sent, [{ text: "progress-new-selected", threadId: "new-selected" }, { text: "final-new-selected", threadId: "new-selected" }]);
  assert.ok(f.manager.state.events.some(e => e.threadId === "existing" && e.type === "turn.completed"));
  await f.manager.message(req("/switch existing")); await f.manager.scan();
  assert.equal(sent.length, 2, "Suppressed old output must not replay on switching back");
  f.tasks.get("existing").turns.at(-1).items.push({ id: "after-return", type: "agentMessage", phase: "final_answer", text: "new-output-after-return" });
  await f.manager.scan(); assert.equal(sent.at(-1).text, "new-output-after-return");
  assert.equal(f.manager.health().capabilities.feishuOutputRouting, "selected-thread-only");
  assert.ok(!f.calls.some(c => ["send_message_to_thread", "set_thread_archived"].includes(c.name)), "Output selection must not mutate native chats");
});

test("Feishu final-reply-only mode also excludes every unselected registered chat", async t => {
  const f = await fixture(t, { feishu: { streamEnabled: false } }); await f.register();
  f.tasks.set("other", { thread: { id: "other", cwd: f.root }, turns: [] }); await f.register("other");
  const sent = []; f.manager.notifier = { sendMessage: async text => { sent.push(text); return { code: 0 }; } };
  for (const id of ["existing", "other"]) f.tasks.get(id).turns.push({ id: "turn-" + id, status: "completed", items: [
    { type: "agentMessage", phase: "commentary", text: "progress-" + id }, { type: "agentMessage", phase: "final_answer", text: "reply-" + id } ] });
  await f.manager.scan(); assert.deepEqual(sent, ["reply-other"]);
});

test("Feishu selection gate is rechecked after token waits and before later chunks or authentication retries", async () => {
  let selected = "old", writes = 0, release;
  const n = new FeishuNotifier({ appId: "test", appSecret: "test", chatId: "test", canSend: ({ threadId }) => threadId === selected });
  n.getToken = () => new Promise(resolve => { release = resolve; });
  n._postChunk = async () => { writes++; return { code: 0 }; };
  const awaitingToken = n.sendMessage("old-output", { threadId: "old" }); selected = "new"; release("test-token");
  assert.equal(await awaitingToken, null); assert.equal(writes, 0);
  n.getToken = async () => "test-token"; selected = "old";
  n._postChunk = async () => { writes++; selected = "new"; return { code: 0 }; };
  assert.equal(await n.sendMessage("x".repeat(40000), { threadId: "old" }), null); assert.equal(writes, 1);
  selected = "old"; writes = 0;
  n._postChunk = async () => { writes++; selected = "new"; return { code: 99991664 }; };
  assert.equal(await n.sendMessage("retry-must-not-spill", { threadId: "old" }), null); assert.equal(writes, 1);
});

test("actual configured Feishu notifier dynamically gates thread output but keeps bridge-level feedback available", async t => {
  const f = await fixture(t, { feishu: { appId: "test", appSecret: "test", chatId: "test", inboundEnabled: false } }); await f.register();
  const n = f.manager.notifier; n.getToken = async () => "test-token";
  const writes = []; n._postChunk = async (...args) => { writes.push(args); return { code: 0 }; };
  assert.equal(await n.sendMessage("inactive", { threadId: "not-selected" }), null);
  assert.equal((await n.sendMessage("active", { threadId: "existing" })).code, 0);
  assert.equal((await n.sendMessage("bridge error feedback")).code, 0);
  assert.equal(writes.length, 2);
});

test("multiple Desktop MCP instances cannot become concurrent writers of the same registry", async t => {
  const f = await fixture(t);
  const other = new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config });
  await assert.rejects(other.initialize(), e => e.body.error === "manager_instance_active");
  await other.close();
  await f.register(); assert.equal(f.manager.state.selectedThreadId, "existing");
});

test("existing native task registration is idempotent and workspace constrained", async t => {
  const f = await fixture(t);
  const request = { protocolVersion: PROTOCOL, messageId: randomUUID(), source: "trainer", threadId: "existing" };
  const first = await f.manager.register(request); const count = f.manager.state.seq;
  const again = await f.manager.register(request);
  assert.equal(first.session.threadId, "existing"); assert.equal(again.duplicate, true); assert.equal(f.manager.state.seq, count);
  await assert.rejects(f.register(), e => e.body.error === "adopt_conflict");
  f.tasks.set("outside", { thread: { id: "outside", cwd: path.resolve(f.root, "..") }, turns: [] });
  await assert.rejects(f.register("outside"), e => e.status === 422 && e.body.detail.includes("仍未受 Bridge 管理"));
});

test("send becomes a real native followup with matching started and completed events", async t => {
  const f = await fixture(t); await f.register();
  const request = req("训练员测试消息"); const accepted = await f.manager.message(request);
  assert.equal(accepted.action, "dispatch_pending");
  await f.manager.scan(); f.finish(); await f.manager.scan();
  const events = f.manager.state.events.filter(e => e.messageId === request.messageId);
  assert.equal(events.filter(e => e.type === "turn.started").length, 1);
  assert.equal(events.filter(e => e.type === "turn.completed").length, 1);
  const last = await f.http("/threads/existing/last-turn");
  assert.equal(last.body.lastUserText, "训练员测试消息"); assert.equal(last.body.lastResult, "桌面回复");
  assert.equal((await f.manager.message(request)).duplicate, true);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
});

test("idempotent retries ignore JSON key order and retain legacy persisted receipts", async t => {
  const f = await fixture(t); await f.register();
  const request = req("字段顺序不影响重试", { targetThreadId: "existing", delivery: "steer" });
  await f.manager.message(request);
  const reordered = Object.fromEntries(Object.entries(request).reverse());
  const seq = f.manager.state.seq;
  assert.equal((await f.manager.message(reordered)).duplicate, true);
  // Simulate the pre-0.2.5 receipt format, then restore from disk.
  f.manager.state.receipts[request.messageId].fingerprint = createHash("sha256").update(JSON.stringify(request)).digest("hex");
  await f.manager.save(); await f.manager.close();
  const restored = await new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config }).initialize();
  t.after(() => restored.close());
  assert.equal((await restored.message(reordered)).duplicate, true);
  assert.equal(restored.state.seq, seq);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  await assert.rejects(restored.message({ ...reordered, text: "确实改了内容" }), e => e.body.error === "message_id_conflict");
});

test("native prompt is the exact original text; identifiers stay backstage even for a fast completed turn", async t => {
  const f = await fixture(t); await f.register();
  const request = req("  训练员原文\n第二行：🙂 </input> 标签也保留  ", { source: "trainer" });
  await f.manager.message(request);
  const call = f.calls.findLast(c => c.name === "send_message_to_thread");
  assert.deepEqual(call.args, { threadId: "existing", prompt: request.text });
  assert.ok(!call.args.prompt.includes(request.messageId));
  f.finish(); await f.manager.scan(); await f.manager.scan();
  const paired = f.manager.state.events.filter(e => e.messageId === request.messageId);
  assert.equal(paired.filter(e => e.type === "turn.started").length, 1);
  assert.equal(paired.filter(e => e.type === "turn.completed").length, 1);
  assert.equal(f.manager.state.events.filter(e => e.type === "message.observed").length, 0);
});

test("identical queued messages still have independent native turns and receipts", async t => {
  const f = await fixture(t); await f.register();
  const requests = [req("同一句话"), req("同一句话", { delivery: "queue" })];
  await f.manager.message(requests[0]); await f.manager.scan();
  await f.manager.message(requests[1]);
  f.finish(); await f.manager.scan(); await f.manager.scan(); f.finish(); await f.manager.scan();
  const started = f.manager.state.events.filter(e => e.type === "turn.started");
  assert.deepEqual(started.map(e => e.messageId), requests.map(r => r.messageId));
  assert.equal(new Set(started.map(e => e.turnId)).size, 2);
  assert.equal(f.manager.state.events.filter(e => e.type === "turn.completed" && requests.some(r => r.messageId === e.messageId)).length, 2);
  assert.deepEqual(f.calls.filter(c => c.name === "send_message_to_thread").map(c => c.args.prompt), ["同一句话", "同一句话"]);
});

test("composer input, old matching history and another sender cannot steal plain-input correlation", async t => {
  const f = await fixture(t); const text = "相同文字";
  const delegate = source => ({ type: "functionCallOutput", name: "send_message_to_thread",
    output: { text: `<codex_delegation><source_thread_id>${source}</source_thread_id><input>${text}</input></codex_delegation>` } });
  f.tasks.get("existing").turns.push({ id: "old", status: "completed", items: [delegate("manager-test")] });
  await f.register(); const request = req(text); await f.manager.message(request);
  const actual = f.tasks.get("existing").turns.pop();
  f.tasks.get("existing").turns.push({ id: "composer", status: "completed", items: [{ type: "userMessage", text }] },
    { id: "foreign", status: "completed", items: [delegate("other-task")] });
  await f.manager.scan();
  assert.equal(f.manager.state.events.filter(e => e.type === "turn.started" && e.messageId === request.messageId).length, 0);
  assert.deepEqual(f.manager.state.sessions.existing.pendingInputs.map(p => p.messageId), [request.messageId]);
  f.tasks.get("existing").turns.push(actual); await f.manager.scan();
  const started = f.manager.state.events.find(e => e.type === "turn.started" && e.messageId === request.messageId);
  assert.equal(started.turnId, actual.id);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
});

test("ambiguous native matches fail closed instead of guessing a receipt or resending", async t => {
  const f = await fixture(t); await f.register(); const request = req("ambiguous");
  await f.manager.message(request);
  const copy = structuredClone(f.tasks.get("existing").turns.at(-1)); copy.id = "another-matching-turn";
  f.tasks.get("existing").turns.push(copy); await f.manager.scan(); await f.manager.scan();
  const rejected = f.manager.state.events.filter(e => e.type === "message.rejected" && e.messageId === request.messageId);
  assert.equal(rejected.length, 1); assert.equal(rejected[0].payload.error, "dispatch_outcome_unknown");
  assert.equal(rejected[0].payload.retryable, false);
  assert.equal(f.manager.state.events.filter(e => e.type === "turn.started" && e.messageId === request.messageId).length, 0);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
});

test("restart restores accepted plain-input correlation without another native write", async t => {
  const f = await fixture(t); await f.register(); const request = req("restart-safe plain message");
  await f.manager.message(request); await f.manager.close();
  const restored = await new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config }).initialize();
  t.after(() => restored.close());
  assert.equal((await restored.message(request)).duplicate, true);
  f.finish(); await restored.scan(); await restored.scan();
  assert.equal(restored.state.events.filter(e => e.type === "turn.started" && e.messageId === request.messageId).length, 1);
  assert.equal(restored.state.events.filter(e => e.type === "turn.completed" && e.messageId === request.messageId).length, 1);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
});

test("legacy in-flight headers remain recoverable but a quoted marker from another task is not trusted", async t => {
  const f = await fixture(t); await f.register(); const request = req("old version message");
  await f.manager.message(request);
  const session = f.manager.state.sessions.existing; session.pendingInputs = [];
  const turn = f.tasks.get("existing").turns.at(-1);
  const original = `[Hermes Bridge messageId=${request.messageId} source=${request.source}]\n${request.text}`;
  turn.items[0].output.text = `<codex_delegation><source_thread_id>manager-test</source_thread_id><input>${original}</input></codex_delegation>`;
  const quoted = structuredClone(turn); quoted.id = "foreign-quoted-marker";
  quoted.items[0].output.text = quoted.items[0].output.text.replace("manager-test", "other-task");
  f.tasks.get("existing").turns.unshift(quoted);
  await f.manager.scan(); f.finish(); await f.manager.scan();
  const started = f.manager.state.events.filter(e => e.type === "turn.started" && e.messageId === request.messageId);
  assert.equal(started.length, 1); assert.equal(started[0].turnId, turn.id);
});

test("missing native sender binding rejects plain delivery before any native write", async t => {
  const f = await fixture(t, { nativeSenderThreadId: null }); await f.register();
  await assert.rejects(f.manager.message(req("unbound")), e => e.status === 503 && e.body.error === "bridge_not_ready");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("local native delivery originates from its target, while remote and non-send tools retain their existing route", () => {
  const binding = { managerThreadId: "old-validator", managerTurnId: "old-turn" };
  const context = { sourceThreadId: "target", turnId: "active-turn" };
  assert.deepEqual(nativeToolInvocation("send_message_to_thread", { threadId: "target" }, binding, context),
    { threadId: "target", turnId: "active-turn" });
  assert.deepEqual(nativeToolInvocation("read_thread", { threadId: "target" }, binding),
    { threadId: "old-validator", turnId: "old-turn" });
  assert.deepEqual(nativeToolInvocation("send_message_to_thread", { threadId: "remote", hostId: "device" }, binding),
    { threadId: "old-validator", turnId: "old-turn" });
  assert.throws(() => nativeToolInvocation("send_message_to_thread", { threadId: "target" }, binding, { sourceThreadId: "other" }));
  assert.equal(nativeSendSource({ nativeSendFromTarget: true }, { threadId: "target" }), "target");
});

const seedBusy = f => f.tasks.get("existing").turns.push({ id: "native-active", status: "inProgress",
  items: [{ id: "trainer-input", type: "userMessage", text: "正在进行的工作" }] });

test("native busy message is delivered in the active turn without another manager chat or a new turn", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const request = req("先解释一下，再继续", { source: "trainer" });
  assert.equal((await f.manager.message(request)).action, "dispatch_pending");
  const write = f.calls.findLast(c => c.name === "send_message_to_thread");
  assert.deepEqual(write.deliveryContext, { sourceThreadId: "existing", turnId: "native-active" });
  assert.equal(write.args.prompt, request.text); assert.equal(f.tasks.get("existing").turns.length, 1);
  await f.manager.scan(); await f.manager.scan();
  const paired = f.manager.state.events.filter(e => e.messageId === request.messageId);
  assert.equal(paired.filter(e => e.type === "turn.steered").length, 1);
  assert.equal(paired.find(e => e.type === "turn.steered").turnId, "native-active");
  assert.equal(paired.filter(e => e.type === "turn.started").length, 0);
  assert.equal(f.manager.state.events.filter(e => e.type === "message.observed").length, 0);
  assert.equal(f.manager.state.sessions.existing.queue.length, 0);
});

test("identical rapid interjections are all inserted before any input readback or turn completion", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const requests = Array.from({ length: 5 }, () => req("一样的插话"));
  const accepted = await Promise.all(requests.map(request => f.manager.message(request)));
  assert.ok(accepted.every(r => r.action === "dispatch_pending"));
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 5);
  assert.equal(f.manager.state.events.filter(e => e.type === "turn.steered").length, 0);
  assert.equal(f.manager.state.sessions.existing.queue.length, 0);
  await f.manager.scan(); await f.manager.scan();
  const steered = f.manager.state.events.filter(e => e.type === "turn.steered");
  assert.deepEqual(steered.map(e => e.messageId), requests.map(r => r.messageId));
  assert.ok(steered.every(e => e.turnId === "native-active"));
  assert.equal(f.tasks.get("existing").turns[0].status, "inProgress");
  assert.equal(f.tasks.get("existing").turns.length, 1);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 5);
  f.finish(); await f.manager.scan(); await f.manager.scan();
  assert.deepEqual(f.manager.state.events.find(e => e.type === "turn.completed").payload.messageIds, requests.map(r => r.messageId));
});

test("explicit queue remains next-turn FIFO even when native busy delivery is enabled", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const request = req("留到下一轮", { delivery: "queue" });
  assert.equal((await f.manager.message(request)).action, "message_queued");
  await f.manager.scan(); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  f.finish(); await f.manager.scan(); await f.manager.scan();
  const started = f.manager.state.events.find(e => e.type === "turn.started" && e.messageId === request.messageId);
  assert.ok(started); assert.notEqual(started.turnId, "native-active");
});

test("a turn ending during native delivery is reported as started, never falsely as steered", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const original = f.manager.native;
  f.manager.native = async (name, args, context) => { if (name === "send_message_to_thread") f.finish(); return original(name, args, context); };
  const request = req("竞态插话"); await f.manager.message(request); await f.manager.scan();
  assert.equal(f.manager.state.events.filter(e => e.type === "turn.steered" && e.messageId === request.messageId).length, 0);
  assert.equal(f.manager.state.events.filter(e => e.type === "turn.started" && e.messageId === request.messageId).length, 1);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
});

test("restart preserves multiple in-flight native inputs without repeating writes", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const first = req("第一句"); const second = req("第二句");
  await f.manager.message(first); await f.manager.message(second); await f.manager.close();
  const restored = await new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config }).initialize();
  t.after(() => restored.close());
  await restored.scan(); await restored.scan();
  assert.deepEqual(restored.state.events.filter(e => e.type === "turn.steered").map(e => e.messageId), [first.messageId, second.messageId]);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 2);
  assert.equal(f.tasks.get("existing").turns.length, 1);
});

test("mixed-content burst preserves individual receipts and FIFO without waiting for readback", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const requests = [req("A"), req("B"), req("A"), req("C"), req("B")];
  const results = await Promise.all(requests.map(r => f.manager.message(r)));
  assert.ok(results.every(r => r.action === "dispatch_pending"));
  assert.deepEqual(f.calls.filter(c => c.name === "send_message_to_thread").map(c => c.args.prompt), requests.map(r => r.text));
  await f.manager.scan();
  assert.deepEqual(f.manager.state.events.filter(e => e.type === "turn.steered").map(e => e.messageId), requests.map(r => r.messageId));
  assert.deepEqual(f.manager.state.sessions.existing.pendingInputs, []);
});

test("partial identical input readback never holds a later write or duplicates the native send", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const original = f.manager.native; let visible = 0;
  f.manager.native = async (name, args, context) => {
    const value = await original(name, args, context);
    if (name === "read_thread") value.turns[0].items = value.turns[0].items.filter(i => i.type !== "functionCallOutput").concat(value.turns[0].items.filter(i => i.type === "functionCallOutput").slice(0, visible));
    return value;
  };
  const requests = [req("same"), req("same"), req("same")];
  await Promise.all(requests.slice(0, 2).map(r => f.manager.message(r)));
  visible = 1; await f.manager.scan();
  assert.equal(f.manager.state.events.filter(e => e.type === "message.observed").length, 0);
  assert.equal((await f.manager.message(requests[2])).action, "dispatch_pending");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 3);
  visible = 3; await f.manager.scan();
  assert.deepEqual(f.manager.state.events.filter(e => e.type === "turn.steered").map(e => e.messageId), requests.map(r => r.messageId));
});

test("a slow background poll does not hold the ingress writer lock", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const original = f.manager.native; let release; let started;
  const entered = new Promise(resolve => { started = resolve; });
  let block = true;
  f.manager.native = async (name, args, context) => {
    if (name === "read_thread" && block) {
      block = false; const snapshot = await original(name, args, context); started();
      await new Promise(resolve => { release = resolve; }); return snapshot;
    }
    return original(name, args, context);
  };
  const scan = f.manager.scan(); await entered;
  try {
    const result = await Promise.race([f.manager.message(req("poll 还没回来也能插话")), new Promise((_, reject) => setTimeout(() => reject(new Error("Ingress blocked by poll")), 2000))]);
    assert.equal(result.action, "dispatch_pending");
  } finally { release(); await scan; }
  await f.manager.scan(); assert.equal(f.manager.state.events.filter(e => e.type === "turn.steered").length, 1);
});

test("upgrade migrates the old readback slot and drains its unsent steer backlog without duplicate writes", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const first = req("old pending"); await f.manager.message(first);
  const session = f.manager.state.sessions.existing;
  session.dispatchCorrelation = session.pendingInputs[0]; session.dispatchPending = first.messageId;
  session.dispatchedAt = session.pendingInputs[0].dispatchedAt; delete session.pendingInputs;
  const second = req("old backlog", { targetThreadId: "existing" }); session.steerQueue = [second];
  f.manager.state.receipts[second.messageId] = { request: second, pending: false, response: { accepted: true, messageId: second.messageId, action: "message_queued" } };
  await f.manager.save(); await f.manager.close();
  const restored = await new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config }).initialize(); t.after(() => restored.close());
  await restored.message(req("new arrival"));
  assert.deepEqual(f.calls.filter(c => c.name === "send_message_to_thread").map(c => c.args.prompt), [first.text, second.text, "new arrival"]);
  await restored.scan();
  assert.equal(restored.state.events.filter(e => e.type === "turn.steered").length, 3);
});

test("Feishu defaults to busy delivery and every interjection's Typing reaction is cleaned at the shared terminal", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true,
    feishu: { trainerOpenIdWhitelist: ["trainer"], streamEnabled: false } }); seedBusy(f); await f.register();
  const reactions = [];
  f.manager.notifier = { addReaction: async id => { reactions.push(["add", id]); return "reaction-" + id; },
    removeReaction: async id => { reactions.push(["remove", id]); return true; }, sendMessage: async () => ({ code: 0 }) };
  const event = { chat_type: "p2p", sender_type: "user", message_type: "text", content: "实时插话", open_id: "trainer", chat_id: "oc_test" };
  const first = await f.manager.receiveFeishu({ ...event, message_id: "om_steer_1" });
  const second = await f.manager.receiveFeishu({ ...event, message_id: "om_steer_2" });
  await Promise.all([...f.manager.feishuReactionTasks.values()]);
  await f.manager.scan(); await f.manager.scan();
  assert.deepEqual(f.manager.state.events.filter(e => e.type === "turn.steered").map(e => e.messageId), [first.messageId, second.messageId]);
  assert.equal(f.tasks.get("existing").turns.length, 1);
  f.finish(); await f.manager.scan(); await new Promise(resolve => setTimeout(resolve, 0)); await f.manager.tail;
  assert.deepEqual(reactions.filter(r => r[0] === "remove").map(r => r[1]).sort(), ["om_steer_1", "om_steer_2"]);
});

test("first successfully delivered progress dismisses every received interjection, not a later input", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true,
    feishu: { trainerOpenIdWhitelist: ["trainer"], streamEnabled: true } }); seedBusy(f); await f.register();
  const reactions = []; const sent = [];
  f.manager.notifier = { addReaction: async id => "reaction-" + id,
    removeReaction: async id => { reactions.push(id); return true; },
    sendMessage: async text => { sent.push(text); return { code: 0 }; } };
  const event = { chat_type: "p2p", sender_type: "user", message_type: "text", content: "插话", open_id: "trainer", chat_id: "oc_test" };
  await Promise.all(["om_progress_1", "om_progress_2"].map(message_id => f.manager.receiveFeishu({ ...event, message_id })));
  await Promise.all([...f.manager.feishuReactionTasks.values()]); await f.manager.scan();
  assert.deepEqual(reactions, []);
  const turn = f.tasks.get("existing").turns[0];
  turn.items.push({ id: "progress-1", type: "agentMessage", phase: "commentary", text: "已收到，继续处理" });
  await f.manager.scan(); await new Promise(resolve => setTimeout(resolve, 0)); await f.manager.tail;
  assert.equal(turn.status, "inProgress"); assert.deepEqual(sent, ["已收到，继续处理"]);
  assert.deepEqual(reactions.sort(), ["om_progress_1", "om_progress_2"]);
  await f.manager.receiveFeishu({ ...event, message_id: "om_progress_3" });
  await Promise.all([...f.manager.feishuReactionTasks.values()]); await f.manager.scan();
  assert.ok(!reactions.includes("om_progress_3"), "An older progress card must not dismiss a newer input");
  turn.items.push({ id: "progress-2", type: "agentMessage", phase: "commentary", text: "第三条也已收到" });
  await f.manager.scan(); await new Promise(resolve => setTimeout(resolve, 0)); await f.manager.tail;
  assert.deepEqual(reactions.sort(), ["om_progress_1", "om_progress_2", "om_progress_3"]);
});

test("failed progress delivery keeps Typing; successful progress cleans even a late-created reaction", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true,
    feishu: { trainerOpenIdWhitelist: ["trainer"], streamEnabled: true } }); seedBusy(f); await f.register();
  let add; let delivered = false; const removed = [];
  f.manager.notifier = { addReaction: async () => new Promise(resolve => { add = resolve; }),
    removeReaction: async id => { removed.push(id); return true; }, sendMessage: async () => delivered ? { code: 0 } : null };
  const input = await f.manager.receiveFeishu({ message_id: "om_late_reaction", chat_type: "p2p", sender_type: "user", message_type: "text", content: "等过程就好", open_id: "trainer", chat_id: "oc_test" });
  const turn = f.tasks.get("existing").turns[0];
  turn.items.push({ id: "failed-progress", type: "agentMessage", phase: "commentary", text: "网络发送失败" });
  await f.manager.scan(); await new Promise(resolve => setTimeout(resolve, 0)); await f.manager.tail;
  assert.equal(f.manager.state.feishuReceipts[input.messageId].cleanupRequested, false);
  delivered = true;
  turn.items.push({ id: "successful-progress", type: "agentMessage", phase: "commentary", text: "过程发送成功" });
  await f.manager.scan(); await new Promise(resolve => setTimeout(resolve, 0)); await f.manager.tail;
  assert.equal(f.manager.state.feishuReceipts[input.messageId].cleanupRequested, true);
  add("late-reaction-id"); await Promise.all([...f.manager.feishuReactionTasks.values()]);
  assert.deepEqual(removed, ["om_late_reaction"]); assert.equal(turn.status, "inProgress");
});

test("uncertain native write cannot be claimed by a subsequent identical input or automatically retried", async t => {
  const f = await fixture(t, { nativeBusyDeliveryVerified: true, nativeSendFromTarget: true }); seedBusy(f); await f.register();
  const original = f.manager.native; let unknown = true;
  f.manager.native = async (name, args, context) => {
    const result = await original(name, args, context);
    if (name === "send_message_to_thread" && unknown) { unknown = false; throw new Error("timeout after insertion"); }
    return result;
  };
  const first = req("重复原文"); const second = req("重复原文");
  await assert.rejects(f.manager.message(first), e => e.body.retryable === false);
  await f.manager.message(second); await f.manager.scan();
  assert.deepEqual(f.manager.state.events.filter(e => e.type === "turn.steered").map(e => e.messageId), [second.messageId]);
  await assert.rejects(f.manager.message(first), e => e.body.retryable === false);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 2);
});

test("explicit unknown target never falls back to selected task; changed idempotency payload is rejected", async t => {
  const f = await fixture(t); await f.register();
  const request = req("hello", { targetThreadId: "unknown" });
  const response = await f.http("/manager/messages", request);
  assert.equal(response.status, 404); assert.equal(response.body.messageId, request.messageId);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  await assert.rejects(f.manager.message({ ...request, targetThreadId: "existing" }), e => e.body.error === "message_id_conflict");
});

test("queue drains strict FIFO into distinct native turns, with per-message scheduling terminals", async t => {
  const f = await fixture(t); await f.register();
  const first = req("first"); await f.manager.message(first); await f.manager.scan();
  const queued = [req("second", { delivery: "queue" }), req("third", { delivery: "queue" })];
  for (const request of queued) assert.equal((await f.manager.message(request)).action, "message_queued");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  for (let i = 0; i < 3; i++) { f.finish(); await f.manager.scan(); await f.manager.scan(); }
  const calls = f.calls.filter(c => c.name === "send_message_to_thread"); assert.equal(calls.length, 3);
  assert.match(calls[1].args.prompt, /second$/); assert.match(calls[2].args.prompt, /third$/);
  const started = f.manager.state.events.filter(e => e.type === "turn.started");
  assert.equal(new Set(started.map(e => e.turnId)).size, 3);
  assert.deepEqual(started.map(e => e.messageId), [first, ...queued].map(r => r.messageId));
});

test("busy steer and active stop are truthful capability errors; idle stop is idempotent", async t => {
  const f = await fixture(t); await f.register();
  assert.equal((await f.manager.message(req("/stop"))).action, "turn_already_idle");
  await f.manager.message(req("busy")); await f.manager.scan();
  await assert.rejects(f.manager.message(req("steer")), e => e.body.error === "writer_conflict" && Boolean(e.body.activeTurnId));
  await assert.rejects(f.manager.message(req("/stop")), e => e.status === 501 && e.body.error === "capability_unavailable");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
});

test("/new, /fork, automatic naming, alias and strict slash parsing", async t => {
  const f = await fixture(t); await f.register();
  const created = await f.manager.message(req("/new")); assert.equal(created.session.alias, "Hermes 会话 #001");
  const fork = await f.manager.message(req("/fork")); assert.equal(fork.session.alias, "Hermes 会话 #002");
  assert.equal(fork.session.forkedFromId, created.session.threadId);
  await assert.rejects(f.manager.message(req("/new name")), e => e.body.error === "command_arguments_not_supported");
  const number = f.manager.state.nextName;
  const renamed = await f.http(`/manager/sessions/${fork.session.threadId}`, { alias: "HoL 验收" }, "PATCH");
  assert.equal(renamed.body.session.alias, "HoL 验收"); assert.equal(f.manager.state.nextName, number);
  assert.equal((await f.manager.message(req("/switch existing"))).session.threadId, "existing");
  await assert.rejects(f.manager.message(req("/unknown")), e => e.body.error === "unknown_command");
  await f.manager.message(req("//not-a-command"));
  assert.equal(f.calls.findLast(c => c.name === "send_message_to_thread").args.prompt, "/not-a-command");
});

test("active fork source returns source_turn_active with original messageId and no fork call", async t => {
  const f = await fixture(t); await f.register(); await f.manager.message(req("busy"));
  const request = req("/fork", { sourceThreadId: "existing" });
  const result = await f.http("/manager/messages", request);
  assert.equal(result.status, 409); assert.equal(result.body.error, "source_turn_active");
  assert.equal(result.body.messageId, request.messageId); assert.ok(result.body.activeTurnId);
  assert.equal(f.calls.filter(c => c.name === "fork_thread").length, 0);
});

test("native trainer composer input is synchronized once, without being dispatched again", async t => {
  const f = await fixture(t); await f.register();
  f.tasks.get("existing").turns.push({ id: "trainer-turn", status: "completed", items: [
    { type: "userMessage", content: [{ type: "text", text: "训练员原生插话" }] },
    { type: "reasoning", content: ["PRIVATE_REASONING"], summary: ["not needed"] },
    { type: "mcpToolCall", arguments: { token: "PRIVATE_TOOL_SECRET" } },
    { type: "agentMessage", phase: "final_answer", text: "已收到插话" },
  ] });
  await f.manager.scan(); await f.manager.scan();
  const input = f.manager.state.events.filter(e => e.type === "message.observed"); assert.equal(input.length, 1);
  assert.equal(input[0].source, "trainer"); assert.equal(input[0].payload.alreadyDispatched, true);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  const persisted = await readFile(f.manager.statePath, "utf8");
  assert.ok(!persisted.includes("PRIVATE_")); assert.ok(!persisted.includes(token));
});

test("pagination, SSE replay with Last-Event-ID, and expired cursor guidance", async t => {
  const f = await fixture(t); await f.register();
  const page = (await f.http("/manager/events?after=0&stream=0&limit=1")).body;
  assert.equal(page.events.length, 1); assert.equal(page.hasMore, true);
  const controller = new AbortController();
  const response = await fetch(f.url + "/manager/events", { headers: { authorization: `Bearer ${token}`, "Last-Event-ID": "1" }, signal: controller.signal });
  const reader = response.body.getReader(); const chunk = new TextDecoder().decode((await reader.read()).value);
  assert.match(chunk, /id: 2/); assert.ok(!chunk.includes("id: 1\n")); controller.abort();
  await assert.rejects(reader.read()).catch(() => {});
  f.manager.state.replayFloor = 2;
  const expired = await f.http("/manager/events?after=0&stream=0");
  assert.equal(expired.body.error, "replay_too_old"); assert.ok(expired.body.latestSeq >= 2);
  assert.match(expired.body.detail, /Read the native thread/);
});

test("restart preserves receipts, selected native thread, counter, event sequence and FIFO", async t => {
  const f = await fixture(t); await f.register();
  const request = req("/new"); const created = await f.manager.message(request); await f.manager.close();
  const restored = await new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config }).initialize();
  t.after(() => restored.close());
  assert.equal(restored.state.selectedThreadId, created.session.threadId);
  assert.equal(restored.state.nextName, 2); assert.equal((await restored.message(request)).duplicate, true);
  assert.equal(f.calls.filter(c => c.name === "create_thread").length, 1);
});

test("remote send routes through Desktop hostId; explicit per-turn safety cannot be silently ignored", async t => {
  const f = await fixture(t); await f.register("existing", { hostId: "remote-control:test-device" });
  await f.manager.message(req("remote test"));
  assert.equal(f.calls.findLast(c => c.name === "send_message_to_thread").args.hostId, "remote-control:test-device");
  await assert.rejects(f.manager.message(req("unsafe", { safetyMode: "danger-full-access" })), e => e.status === 422);
});

test("Feishu duplicate inputs are idempotent; unauthorized visitors never cause a native write", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  const event = { chat_type: "p2p", sender_type: "user", message_type: "text", content: "飞书消息", message_id: "om_test", open_id: "visitor", chat_id: "oc_test" };
  await f.manager.receiveFeishu(event); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  const first = await f.manager.receiveFeishu({ ...event, open_id: "trainer" });
  const again = await f.manager.receiveFeishu({ ...event, open_id: "trainer" });
  assert.equal(first.duplicate, false); assert.equal(again.duplicate, true);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
});

test("Feishu multiline JSON, blank paragraphs, native dispatch and public readback retain the entire original text", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"], streamEnabled: false } }); await f.register();
  f.manager.notifier = { addReaction: async () => null, sendMessage: async () => ({ code: 0 }) };
  for (const text of ["第一段\n\n第二段\n\n\n第三段", "首句\r\n\r\n第二句", "  第一段\n\n末段  \n"]) {
    let parsed;
    const client = new FeishuWebsocketClient({ enabled: true }, { onMessage: event => { parsed = event; } });
    client._handleFrame({ send: () => {} }, { method: 1, headers: { type: "event" }, payload: new TextEncoder().encode(JSON.stringify({ event: {
      sender: { sender_type: "user", sender_id: { open_id: "trainer" } }, message: { message_type: "text", content: JSON.stringify({ text }),
        message_id: "om_multiline_" + randomUUID(), chat_id: "oc_test", chat_type: "p2p" } } })) }, 1);
    assert.equal(parsed.content, text);
    const accepted = await f.manager.receiveFeishu(parsed);
    assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").at(-1).args.prompt, text);
    assert.equal(f.manager.state.receipts[accepted.messageId].request.text, text);
    assert.equal(publicItems(f.tasks.get("existing")).filter(i => i.kind === "delegated").at(-1).text, text);
    assert.equal(JSON.parse(markdownPayload(text, "test")).elements[0].content, text);
    await f.manager.scan(); f.finish(); await f.manager.scan();
  }
});

test("visible Feishu and Hermes source labels preserve full originals, idempotency and native turn correlation", async t => {
  const f = await fixture(t, { messageSourceLabels: true, feishu: { trainerOpenIdWhitelist: ["trainer"], streamEnabled: false } }); await f.register();
  f.manager.notifier = { addReaction: async () => null, sendMessage: async () => ({ code: 0 }) };
  const original = "首句\n\n\n第二段：需要回顾的完整内容\n末段";
  const event = { chat_type: "p2p", sender_type: "user", message_type: "text", content: original,
    message_id: "om_label_multiline", open_id: "trainer", chat_id: "oc_test" };
  const accepted = await f.manager.receiveFeishu(event); await f.manager.receiveFeishu(event);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").at(-1).args.prompt, "飞书：" + original);
  assert.equal(f.manager.state.receipts[accepted.messageId].request.text, original);
  await f.manager.scan(); f.finish(); await f.manager.scan();
  assert.ok(f.manager.state.events.some(e => e.messageId === accepted.messageId && e.type === "turn.completed"));
  const hermes = req(original); await f.manager.message(hermes); await f.manager.message(hermes);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").at(-1).args.prompt, "超级溪流：" + original);
  assert.equal(f.manager.state.receipts[hermes.messageId].request.text, original);
  await f.manager.scan(); f.finish(); await f.manager.scan();
  assert.ok(f.manager.state.events.some(e => e.messageId === hermes.messageId && e.type === "turn.completed"));
  const before = f.calls.filter(c => c.name === "send_message_to_thread").length;
  await f.manager.receiveFeishu({ ...event, content: "/resume", message_id: "om_label_slash" });
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, before);
  await f.manager.message(req("原生工具消息", { source: "trainer" }));
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").at(-1).args.prompt, "原生工具消息");
});

test("labeled pending input survives restart without relabeling or resending an unknown native write", async t => {
  const f = await fixture(t, { messageSourceLabels: true }); await f.register();
  const request = req("第一段\n\n第二段"); await f.manager.message(request); await f.manager.close();
  const restored = await new DesktopManager({ native: f.native, listNativeTools: f.listNativeTools, stateDir: f.root, config: f.config }).initialize();
  t.after(() => restored.close()); await restored.scan();
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 1);
  assert.equal(restored.state.sessions.existing.pendingInputs.length, 0);
  assert.ok(restored.state.events.some(e => e.messageId === request.messageId && e.type === "turn.started"));
});

test("/resume lists beyond five and zero-based selection stays pinned to the displayed snapshot", async t => {
  const f = await fixture(t); await f.register();
  for (let i = 0; i < 7; i++) f.tasks.set(`recent-${i}`, {
    thread: { id: `recent-${i}`, title: `会话 ${i}`, cwd: f.root, updatedAt: i + 10 }, turns: [] });
  f.tasks.set("outside", { thread: { id: "outside", cwd: path.resolve(f.root, ".."), updatedAt: 100 }, turns: [] });
  const listed = await f.manager.message(req("/resume"));
  assert.deepEqual(listed.sessions.map(s => s.threadId), [...[6, 5, 4, 3, 2, 1, 0].map(i => `recent-${i}`), "existing"]);
  assert.deepEqual(listed.sessions.map(s => s.index), [0, 1, 2, 3, 4, 5, 6, 7]);
  f.tasks.get("recent-0").thread.updatedAt = 1000;
  assert.equal((await f.manager.message(req("/resume 0"))).session.threadId, "recent-6");
  assert.equal(f.manager.state.selectedThreadId, "recent-6");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  assert.equal((await f.manager.message(req("/resume 4"))).session.threadId, "recent-2");
  assert.equal((await f.manager.message(req("/resume 5"))).session.threadId, "recent-1");
  await assert.rejects(f.manager.message(req("/resume 99")), e => e.body.error === "thread_not_found");
});

test("resume pagination preserves all candidates and global indices while native activity changes", async t => {
  const f = await fixture(t); await f.register();
  for (let i = 0; i < 24; i++) f.tasks.set(`chat-${i}`, { thread: { id: `chat-${i}`, title: `老会话 ${i}`, cwd: f.root, updatedAt: i + 10 }, turns: [] });
  const first = await f.manager.message(req("/resume")); assert.equal(first.sessions.length, 10); assert.equal(first.resumePage.total, 25);
  const snapshot = structuredClone(f.manager.state.resumeCandidates);
  f.tasks.get("chat-0").thread.updatedAt = Date.now();
  const second = await f.manager.message(req("/resume next")); assert.equal(second.sessions[0].index, 10); assert.equal(second.resumePage.number, 2);
  const last = await f.manager.message(req("/resume page 3")); assert.equal(last.sessions.length, 5); assert.equal(last.sessions[0].index, 20);
  assert.deepEqual(f.manager.state.resumeCandidates, snapshot); assert.equal(f.manager.state.selectedThreadId, "existing");
  assert.equal((await f.manager.message(req("/resume 20"))).session.threadId, snapshot[20].threadId);
  const refreshed = await f.manager.message(req("/resume")); assert.equal(refreshed.sessions[0].threadId, snapshot[20].threadId);
});

test("resume exact title and ID restore old chats; search and duplicate titles never silently select", async t => {
  const f = await fixture(t); await f.register();
  for (const [id, title] of [["old", "曾经的设计"], ["one", "同名"], ["two", "同名"]]) f.tasks.set(id, { thread: { id, title, cwd: f.root, updatedAt: 1 }, turns: [] });
  const search = await f.manager.message(req("/resume search 设计")); assert.equal(search.sessions[0].threadId, "old"); assert.equal(f.manager.state.selectedThreadId, "existing");
  assert.equal((await f.manager.message(req('/resume "曾经的设计"'))).session.threadId, "old");
  const ambiguous = await f.manager.message(req("/resume 同名")); assert.equal(ambiguous.sessions.length, 2); assert.equal(f.manager.state.selectedThreadId, "old");
  assert.equal((await f.manager.message(req("/resume one"))).session.threadId, "one");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("accepted trainer input and explicit resume update activity, listing and rejected input do not", async t => {
  const f = await fixture(t); await f.register();
  f.tasks.set("other", { thread: { id: "other", title: "other", cwd: f.root, updatedAt: 3000 }, turns: [] });
  await f.manager.message(req("/resume")); assert.equal(f.manager.state.resumeActivity, undefined);
  await f.manager.message(req("continue", { source: "trainer" })); const touched = structuredClone(f.manager.state.resumeActivity);
  assert.ok(touched.existing.at); const listed = await f.manager.message(req("/resume")); assert.equal(listed.sessions[0].threadId, "existing");
  assert.deepEqual(f.manager.state.resumeActivity, touched);
  await assert.rejects(f.manager.message(req("again", { source: "trainer" })), /Busy native delivery/);
  assert.deepEqual(f.manager.state.resumeActivity, touched);
});

test("delayed queued delivery retains its original user activity time instead of moving it to the top again", async t => {
  const f = await fixture(t); await f.register(); await f.manager.message(req("busy"));
  await f.manager.message(req("/queue later", { source: "trainer" }));
  const earlier = "2020-01-01T00:00:00.000Z";
  f.manager.session().queue[0].trainerActivityAt = earlier; f.manager.state.resumeActivity.existing.at = earlier;
  f.finish(); await f.manager.scan();
  assert.equal(f.manager.session().queue.length, 0); assert.equal(f.manager.state.resumeActivity.existing.at, earlier);
});

test("/resume requires a prior list; /clear creates a new selected chat without removing the old one", async t => {
  const f = await fixture(t); await f.register();
  await assert.rejects(f.manager.message(req("/resume 0")), e => e.body.error === "thread_not_found");
  const result = await f.manager.message(req("/clear"));
  assert.equal(result.action, "session_created"); assert.equal(result.session.alias, "Hermes 会话 #001");
  assert.equal(f.manager.state.selectedThreadId, result.session.threadId);
  assert.ok(f.manager.state.sessions.existing); assert.ok(f.tasks.has("existing"));
});

test("/resume discovers schema-v4 section-only chats without switching or dispatching input", async t => {
  const f = await fixture(t); await f.register();
  f.tasks.set("section-chat", { thread: { id: "section-chat", title: "分组里的会话", cwd: f.root, updatedAt: 200 }, turns: [] });
  f.manager.native = async (name, args, context) => {
    if (name === "list_threads") return { schemaVersion: 4, threads: [{ id: "existing", title: "绘画", kind: "codex", hostId: "local", cwd: f.root, updatedAt: 100 }],
      sections: [{ itemKeys: ["codex:thread:local:section-chat", "codex:thread:local:missing", "codex:thread:remote-control:other:remote"] }] };
    return f.native(name, args, context);
  };
  const result = await f.manager.message(req("/resume"));
  assert.deepEqual(result.sessions.map(s => s.alias), ["分组里的会话", "绘画"]);
  assert.equal(result.currentSession.title, "绘画"); assert.equal(f.manager.state.selectedThreadId, "existing");
  assert.equal(result.resumeDiscovery.unavailable, 1); assert.equal(result.resumeDiscovery.partial, true);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  const read = f.calls.find(c => c.name === "read_thread" && c.args.threadId === "section-chat");
  assert.equal(read.args.turnLimit, 1); assert.equal(read.args.includeOutputs, false); assert.equal(read.args.hostId, "local");
  await f.manager.message(req("/resume 0")); assert.equal(f.manager.state.selectedThreadId, "section-chat");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("Feishu typing is added once for duplicate input and removed only on its matching native terminal", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"], streamEnabled: false } }); await f.register();
  const reactions = []; const replies = [];
  f.manager.notifier = { addReaction: async id => { reactions.push(["add", id]); return "reaction-test"; },
    removeReaction: async (id, reactionId) => { reactions.push(["remove", id, reactionId]); return true; },
    sendMessage: async text => { replies.push(text); return { code: 0 }; } };
  const event = { chat_type: "p2p", sender_type: "user", message_type: "text", content: "普通飞书消息",
    message_id: "om_typing", open_id: "trainer", chat_id: "oc_test" };
  const [first, again] = await Promise.all([f.manager.receiveFeishu(event), f.manager.receiveFeishu(event)]);
  assert.equal(first.duplicate, false); assert.equal(again.duplicate, true);
  await Promise.all([...f.manager.feishuReactionTasks.values()]);
  assert.deepEqual(reactions, [["add", "om_typing"]]);
  await f.manager.scan(); f.finish("existing", "普通回复"); await f.manager.scan();
  await new Promise(resolve => setTimeout(resolve, 0)); await f.manager.tail;
  assert.deepEqual(reactions, [["add", "om_typing"], ["remove", "om_typing", "reaction-test"]]);
  assert.deepEqual(replies, ["普通回复"]);
});

test("Feishu command lists and switches chats without dispatching a prompt, then clears typing", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  const replies = [];
  f.manager.notifier = { addReaction: async () => "reaction-command", removeReaction: async () => true,
    sendMessage: async text => { replies.push(text); return { code: 0 }; } };
  const event = { chat_type: "p2p", sender_type: "user", message_type: "text", content: "/resume",
    message_id: "om_resume", open_id: "trainer", chat_id: "oc_test" };
  await f.manager.receiveFeishu(event); await Promise.all([...f.manager.feishuReactionTasks.values()]);
  assert.match(replies[0], /编号 0｜existing/);
  assert.match(replies[0], /当前接入会话：`existing`（编号 0）/); assert.match(replies[0], /existing 【当前】/);
  assert.doesNotMatch(replies[0], /^\d+\.\s/m, "Never let Feishu auto-renumber selectable indices.");
  await f.manager.receiveFeishu({ ...event, content: "/resume 0", message_id: "om_select" });
  await Promise.all([...f.manager.feishuReactionTasks.values()]);
  assert.equal(replies[1], "已切换到：existing");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("Feishu resume list keeps literal zero-based selectors outside Markdown ordered lists", async t => {
  const f = await fixture(t, { feishu: { trainerOpenIdWhitelist: ["trainer"] } }); await f.register();
  for (let i = 0; i < 5; i++) f.tasks.set(`chat-${i}`, { thread: { id: `chat-${i}`, title: `标题 ${i}`, cwd: f.root, updatedAt: i + 10 }, turns: [] });
  let card;
  f.manager.notifier = { addReaction: async () => null, sendMessage: async text => { card = JSON.parse(markdownPayload(text, "列表")); return { code: 0 }; } };
  await f.manager.receiveFeishu({ chat_type: "p2p", sender_type: "user", message_type: "text", content: "/resume",
    message_id: "om_five_chats", open_id: "trainer", chat_id: "oc_test" });
  const text = card.elements[0].content;
  assert.deepEqual([...text.matchAll(/编号 (\d+)｜/g)].map(m => Number(m[1])), [0, 1, 2, 3, 4, 5]);
  assert.doesNotMatch(text, /^\d+\.\s/m);
  assert.match(text, /当前接入会话：`existing`（编号 5）/);
  assert.match(text, /【当前】/); assert.equal(f.manager.state.selectedThreadId, "existing");
  assert.equal((await f.manager.message(req("/resume 4"))).session.threadId, "chat-0");
});

function onlineSocket(f, readyState = 1) {
  const socket = { _ws: { readyState }, connectionId: randomUUID(), stop: () => {} };
  f.manager.ws = socket; return socket;
}

test("online notice waits for connection, reads the current native title and deduplicates a connection", async t => {
  const f = await fixture(t, { feishu: { chatId: "trainer-chat" } }); await f.register();
  f.tasks.get("existing").thread.title = "新的原生会话标题";
  f.manager.state.sessions.existing.alias = "过期 alias";
  f.manager.state.sessions.existing.feishuChatId = "selected-chat";
  const cards = [];
  f.manager.notifier = { sendMessage: async (text, options) => { cards.push({ text, options }); return { code: 0, data: { message_id: "om_online" } }; } };
  const socket = onlineSocket(f, 0);
  await f.manager.onlineAnnouncement.connected(socket, socket.connectionId); assert.equal(cards.length, 0);
  socket._ws.readyState = 1;
  await Promise.all([f.manager.onlineAnnouncement.connected(socket, socket.connectionId), f.manager.onlineAnnouncement.connected(socket, socket.connectionId)]);
  assert.equal(cards.length, 1); assert.match(cards[0].text, /CodexWorker已上线/); assert.match(cards[0].text, /新的原生会话标题/);
  assert.doesNotMatch(cards[0].text, /过期 alias/); assert.equal(cards[0].options.title, "CodexWorker-上线通知");
  assert.equal(cards[0].options.threadId, "existing"); assert.equal(cards[0].options.chatId, "selected-chat");
  assert.equal(cards[0].options.uuid, socket.connectionId);
  const read = f.calls.filter(c => c.name === "read_thread").at(-1);
  assert.equal(read.args.includeOutputs, false); assert.equal(read.args.turnLimit, 1);
  assert.equal(f.calls.filter(c => ["send_message_to_thread", "create_thread"].includes(c.name)).length, 0);
  assert.equal(f.manager.integrationStatus().feishu.onlineNotice.status, "sent");
  const state = JSON.parse(await readFile(f.manager.statePath, "utf8"));
  assert.equal(state.feishuOnlineNotice.messageId, "om_online"); assert.equal(state.feishuOnlineNotice.sessionTitle, "新的原生会话标题");
  assert.ok(!JSON.stringify(state.feishuOnlineNotice).includes(token));
  socket.connectionId = randomUUID();
  await f.manager.onlineAnnouncement.connected(socket, socket.connectionId); assert.equal(cards.length, 2);
  assert.equal(cards[1].options.title, "CodexWorker-连接恢复"); assert.match(cards[1].text, /连接已恢复/);
  assert.doesNotMatch(cards[1].text, /已上线/);
});

test("online failures are honest, bounded and retry with the same UUID and exact body", async t => {
  const f = await fixture(t, { feishu: { chatId: "trainer-chat" } }); await f.register();
  const cards = []; f.manager.notifier = { sendMessage: async (text, options) => { cards.push({ text, options }); return null; } };
  const socket = onlineSocket(f); const notice = f.manager.onlineAnnouncement;
  await notice.connected(socket, socket.connectionId); assert.equal(notice.status().status, "pending");
  for (let i = 0; i < 2; i++) { clearTimeout(notice.timer); await notice.run(socket, notice.record); }
  assert.equal(notice.status().status, "failed"); assert.equal(notice.status().attempts, 3);
  assert.deepEqual(cards[1], cards[0]); assert.deepEqual(cards[2], cards[0]);
  await notice.connected(socket, socket.connectionId); assert.equal(cards.length, 3);
  const stale = onlineSocket(f); await notice.connected(socket, socket.connectionId); assert.equal(cards.length, 3);
  assert.notEqual(stale, socket);
});

test("online notices do not guess inaccessible or absent bindings and can be disabled", async t => {
  const f = await fixture(t, { feishu: { chatId: "trainer-chat" } }); const cards = [];
  f.manager.notifier = { sendMessage: async (text, options) => { cards.push({ text, options }); return { code: 0 }; } };
  const socket = onlineSocket(f); await f.manager.onlineAnnouncement.connected(socket, socket.connectionId);
  assert.match(cards[0].text, /尚未接入会话/); assert.equal(cards[0].options.threadId, undefined);
  await f.register(); f.tasks.delete("existing"); socket.connectionId = randomUUID();
  await f.manager.onlineAnnouncement.connected(socket, socket.connectionId);
  assert.equal(cards.length, 1, "An unreadable bound chat cannot be announced as online");
  assert.equal(f.manager.onlineAnnouncement.status().status, "pending"); assert.equal(f.manager.state.selectedThreadId, "existing");
  assert.equal(f.calls.filter(c => c.name === "create_thread").length, 0);
  f.manager.config.feishu.onlineNotificationEnabled = false; socket.connectionId = randomUUID();
  await f.manager.onlineAnnouncement.connected(socket, socket.connectionId); assert.equal(cards.length, 1);
});

test("short first connections cannot announce success; a later stable connection emits one ready notice", async t => {
  const f = await fixture(t, { feishu: { chatId: "trainer-chat", onlineStableMs: 30000 } }); await f.register();
  let now = Date.now(); const notice = f.manager.onlineAnnouncement; notice.clock = () => now;
  const cards = []; f.manager.notifier = { sendMessage: async (text, options) => { cards.push({ text, options }); return { code: 0, data: { message_id: "ready" } }; } };
  const first = onlineSocket(f); await notice.connected(first, first.connectionId);
  assert.equal(cards.length, 0); assert.equal(notice.status().status, "stabilizing"); assert.equal(notice.status().attempts, 0);
  const abandoned = notice.record; first._ws.readyState = 3; now += 31000;
  await notice.run(first, abandoned); assert.equal(cards.length, 0);
  const second = onlineSocket(f); await notice.connected(second, second.connectionId); assert.equal(cards.length, 0);
  now += 30000; clearTimeout(notice.timer); await notice.run(second, notice.record);
  assert.equal(cards.length, 1); assert.match(cards[0].text, /连接和当前接入会话已就绪/);
  assert.equal(notice.status().status, "sent"); assert.equal(cards[0].options.uuid, second.connectionId);
});

test("online/recovery classification survives restart but a new Desktop lifetime is a new online notice", async t => {
  const f = await fixture(t, { desktopLifetimeStartedAt: "current-desktop", feishu: { chatId: "trainer-chat" } }); await f.register();
  const cards = []; f.manager.notifier = { sendMessage: async (text, options) => { cards.push({ text, options }); return { code: 0 }; } };
  let socket = onlineSocket(f); await f.manager.onlineAnnouncement.connected(socket, socket.connectionId);
  assert.equal(f.manager.state.feishuLastOnlineNotice.desktopLifetimeStartedAt, "current-desktop");
  f.manager.onlineAnnouncement.reset(); socket = onlineSocket(f);
  await f.manager.onlineAnnouncement.connected(socket, socket.connectionId); assert.equal(cards.at(-1).options.title, "CodexWorker-连接恢复");
  f.manager.config.desktopLifetimeStartedAt = "next-desktop"; socket = onlineSocket(f);
  await f.manager.onlineAnnouncement.connected(socket, socket.connectionId); assert.equal(cards.at(-1).options.title, "CodexWorker-上线通知");
});

test("an older hot-reload suppression record still distinguishes a later recovery in the same Desktop", async t => {
  const f = await fixture(t, { desktopLifetimeStartedAt: "2026-10-04T02:52:34.000Z", feishu: { chatId: "trainer-chat" } }); await f.register();
  f.manager.state.feishuOnlineNotice = { status: "suppressed-hot-reload", connectedAt: "2026-10-04T03:12:54.000Z" };
  const cards = []; f.manager.notifier = { sendMessage: async (text, options) => { cards.push({ text, options }); return { code: 0 }; } };
  const socket = onlineSocket(f); await f.manager.onlineAnnouncement.connected(socket, socket.connectionId);
  assert.equal(cards[0].options.title, "CodexWorker-连接恢复"); assert.doesNotMatch(cards[0].text, /已上线/);
});

test("online notice has no implicit recipient and handles a verified untitled thread honestly", async t => {
  const f = await fixture(t); await f.register(); let sends = 0;
  f.tasks.get("existing").thread.title = "";
  f.manager.notifier = { sendMessage: async () => { sends++; return { code: 0 }; } };
  const socket = onlineSocket(f); await f.manager.onlineAnnouncement.connected(socket, socket.connectionId);
  assert.equal(sends, 0); assert.equal(f.manager.onlineAnnouncement.status().status, "unavailable");
  assert.equal(f.manager.onlineAnnouncement.status().sessionTitle, "未命名会话");
});

test("online metadata lookup follows a concurrent selection change instead of reporting the old chat", async t => {
  const f = await fixture(t, { feishu: { chatId: "trainer-chat" } }); await f.register();
  f.tasks.set("new-selection", { thread: { id: "new-selection", title: "当前选择", cwd: f.root }, turns: [] });
  f.manager.state.sessions["new-selection"] = f.manager.makeSession(f.tasks.get("new-selection").thread, { alias: "alias", cwd: f.root });
  const original = f.manager.native;
  f.manager.native = async (name, args) => { if (name === "read_thread" && args.threadId === "existing") f.manager.state.selectedThreadId = "new-selection"; return original(name, args); };
  const cards = []; f.manager.notifier = { sendMessage: async (text, options) => { cards.push({ text, options }); return { code: 0 }; } };
  const socket = onlineSocket(f); await f.manager.onlineAnnouncement.connected(socket, socket.connectionId);
  assert.equal(cards.length, 1); assert.match(cards[0].text, /当前选择/); assert.equal(cards[0].options.threadId, "new-selection");
});

test("Feishu notifier sends an idempotency UUID to the API and reuses it during token refresh", async t => {
  const bodies = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    bodies.push(JSON.parse(options.body)); return { json: async () => bodies.length === 1 ? { code: 99991664, msg: "token expired" } : { code: 0 } };
  });
  const notifier = new FeishuNotifier({ appId: "test-app", appSecret: "test-secret", chatId: "test-chat", bridgeName: "test" });
  notifier.getToken = async () => "test-token";
  const uuid = randomUUID(); const result = await notifier.sendMessage("上线测试", { uuid });
  assert.equal(result.code, 0); assert.equal(bodies.length, 2); assert.equal(bodies[0].uuid, uuid); assert.deepEqual(bodies[1], bodies[0]);
});

test("WebSocket open callback fires once per real connection and not on a stopped connection", async () => {
  const sockets = []; const ids = [];
  const client = new FeishuWebsocketClient({ enabled: true }, { onConnected: ({ connectionId }) => { ids.push(connectionId); },
    websocketFactory: () => { const ws = { send: () => {}, readyState: 1 }; sockets.push(ws); return ws; } });
  client._getConnUrl = async () => "wss://example.invalid/ws?service_id=1";
  for (let i = 0; i < 2; i++) {
    const running = client._runOnce(); await new Promise(resolve => setImmediate(resolve));
    sockets[i].onopen(); sockets[i].onopen(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(ids.length, i + 1); sockets[i].onclose(); await running;
  }
  assert.notEqual(ids[0], ids[1]);
  const stopped = client._runOnce(); await new Promise(resolve => setImmediate(resolve));
  client._stopped = true; sockets[2].onopen(); sockets[2].onclose(); await stopped;
  assert.equal(ids.length, 2);
});

test("Feishu commentary is titled 输出 and final native reply is titled 回复, matching DSH", async t => {
  const f = await fixture(t); await f.register();
  const cards = [];
  f.manager.notifier = { sendMessage: async (text, options) => { cards.push({ text, title: options.title }); return { code: 0 }; } };
  await f.manager.message(req("标题测试", { source: "trainer" })); await f.manager.scan();
  f.tasks.get("existing").turns.at(-1).items.push({ id: "progress", type: "agentMessage", phase: "commentary", text: "中间输出" });
  await f.manager.scan(); f.finish("existing", "最终回复"); await f.manager.scan(); await f.manager.scan();
  assert.deepEqual(cards, [{ text: "中间输出", title: "CodexWorker-输出" }, { text: "最终回复", title: "CodexWorker-回复" }]);
});

test("integration diagnostics are authenticated and contain no credentials", async t => {
  const f = await fixture(t);
  assert.equal((await f.http("/bridge/integrations", null, "GET", "wrong")).status, 401);
  const status = (await f.http("/bridge/integrations")).body;
  assert.equal(status.feishu.connected, false); assert.equal(status.feishu.inboundEnabled, false);
  assert.ok(!JSON.stringify(status).includes(token));
});

test("explicit image recovery is authenticated, binding-pinned, original-UUID only and updates the auto receipt", async t => {
  const f = await fixture(t, { feishu: { chatId: "oc_test" } }); await f.register();
  f.manager.notifier = { enabled: true, chatId: "oc_test" };
  const messageId = randomUUID(), body = { imagePath: path.join(f.root, "public.png"), threadId: "existing", messageId };
  const entry = { messageId, threadId: "existing", bindingEpoch: f.manager.state.selectionEpoch, status: "failed", error: "image_send_outcome_unknown", completedAt: 1 };
  f.manager.state.autoImages = { sample: entry }; let calls = 0;
  f.manager.businessRuntime.sendImage = async (request, context) => {
    calls++; assert.equal(context.recoveryRequested, true); assert.equal(request.messageId, messageId);
    assert.equal((await context.getTarget(request.threadId)).chatId, "oc_test"); context.validateTarget({ threadId: "existing", chatId: "oc_test" });
    return { messageId, feishuMessageId: "om_recovered", status: "sent", duplicate: calls > 1 };
  };
  assert.equal((await f.http("/bridge/images/recover", body, "POST", "wrong")).status, 401);
  assert.equal((await f.http("/bridge/images/recover", { ...body, force: true })).status, 400);
  assert.equal((await f.http("/bridge/images/recover", { ...body, messageId: randomUUID() })).status, 409);
  assert.equal(calls, 0);
  f.manager.state.selectionEpoch++;
  assert.equal((await f.http("/bridge/images/recover", body)).status, 409); assert.equal(calls, 0);
  entry.bindingEpoch = f.manager.state.selectionEpoch;
  const result = await f.http("/bridge/images/recover", body); assert.equal(result.status, 200); assert.equal(result.body.messageId, messageId);
  assert.equal(entry.status, "sent"); assert.equal(entry.previousFailure.error, "image_send_outcome_unknown"); assert.equal(entry.feishuMessageId, "om_recovered");
  assert.equal((await f.http("/bridge/images/recover", body)).body.duplicate, true);
  assert.equal(f.manager.health().capabilities.feishuImageRecovery, "bounded-same-uuid-50m");
});

test("manual image send also rejects away-and-back selection during an authorization wait", async t => {
  const f = await fixture(t, { feishu: { chatId: "oc_test" } }); await f.register(); f.manager.notifier = { enabled: true, chatId: "oc_test" };
  f.manager.businessRuntime.sendImage = async (_request, context) => {
    const target = await context.getTarget("existing"); f.manager.state.selectionEpoch += 2;
    context.validateTarget(target); throw new Error("Should not reach send");
  };
  await assert.rejects(f.manager.sendImage({ imagePath: path.join(f.root, "image.png"), threadId: "existing", messageId: randomUUID() }), e => e.body.error === "selected_target_changed");
});

test("transient read snapshots retry boundedly, but native writes are never retried", async t => {
  const f = await fixture(t); const original = f.manager.native;
  let failures = 2; let writes = 0;
  f.manager.native = async (name, args) => {
    if (name === "read_thread" && failures-- > 0) throw new Error("transient snapshot");
    if (name === "send_message_to_thread") { writes++; throw new Error("unknown write outcome"); }
    return original(name, args);
  };
  await f.register(); assert.ok(failures < 0);
  await assert.rejects(f.manager.message(req("do not retry a write")), e => e.body.error === "desktop_integration_unavailable");
  assert.equal(writes, 1);
});

test("public-only history avoids oversized raw tool outputs while retaining replies and displayed images", async t => {
  const f = await fixture(t); let fullOutputReads = 0;
  f.manager.native = async (name, args, context) => {
    if (name === "read_thread" && args.includeOutputs !== false) {
      fullOutputReads++;
      throw nativeToolRejection(name, { success: false, contentItems: [{ type: "inputText", text: "Tool response exceeds the size limit" }] });
    }
    return f.native(name, args, context);
  };
  await f.register();
  f.tasks.get("existing").turns.push({ id: "public-only-turn", status: "completed", items: [
    { id: "public-reply", type: "agentMessage", phase: "final_answer", text: "长会话公开回复" },
    { id: "public-picture", type: "imageView", path: path.join(f.root, "public.png") },
  ] });
  await f.manager.scan();
  assert.equal(fullOutputReads, 0);
  assert.ok(f.manager.state.events.some(e => e.type === "item.completed" && e.payload.snapshot === "长会话公开回复"));
  assert.equal(Object.keys(f.manager.state.sessions.existing.seenImages).length, 1);
  assert.equal(f.manager.health().ready, true);
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("native request rejection marks only the affected session and keeps the responding channel ready", async t => {
  const f = await fixture(t); await f.register();
  f.manager.native = async () => { throw nativeToolRejection("read_thread", { success: false }); };
  await f.manager.scan();
  assert.equal(f.manager.state.sessions.existing.state, "error");
  assert.equal(f.manager.health().ready, true, "A native error response is not evidence of a dead native transport");
  f.manager.native = f.native; await f.manager.scan();
  assert.equal(f.manager.state.sessions.existing.state, "idle");
  assert.equal(f.manager.health().ready, true);
});

test("opaque failed bulk history still relays the current reply once without sending a native input", async t => {
  const f = await fixture(t); await f.register(); const sent = [];
  f.manager.notifier = { enabled: true, sendMessage: async text => { sent.push(text); return { code: 0 }; } };
  f.tasks.get("existing").turns.push({ id: "latest-turn", status: "completed", items: [
    { id: "latest-reply", type: "agentMessage", phase: "final_answer", text: "恢复后的完整回复" }] });
  const attempts = [];
  f.manager.native = async (name, args, context) => {
    if (name === "read_thread") {
      attempts.push(args.turnLimit);
      if (args.turnLimit > 1) throw nativeToolRejection(name, { success: false, contentItems: [{ text: "Codex app tool request failed" }] });
    }
    return f.native(name, args, context);
  };
  await f.manager.scan(); await f.manager.scan();
  assert.deepEqual(attempts, [10, 5, 2, 1, 1]); assert.deepEqual(sent, ["恢复后的完整回复"]);
  const relay = f.manager.integrationStatus().feishu.relay;
  assert.equal(relay.sessionState, "idle"); assert.equal(relay.lastError, null); assert.ok(relay.lastSuccessAt);
  assert.equal(relay.historyWindow.fallbackReason, "native_read_rejected");
  assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("selected relay failure remains visible even when another chat keeps global native health ready", async t => {
  const f = await fixture(t); await f.register();
  f.tasks.set("other", { thread: { id: "other", cwd: f.root }, turns: [] }); await f.register("other");
  f.manager.selectSession("existing");
  f.manager.native = async (name, args, context) => {
    if (name === "read_thread" && args.threadId === "existing") throw nativeToolRejection(name, { success: false,
      contentItems: [{ text: "Codex app tool request failed" }] });
    return f.native(name, args, context);
  };
  await f.manager.scan(); assert.equal(f.manager.health().ready, true);
  assert.equal(f.manager.integrationStatus().feishu.relay.sessionState, "error");
  assert.equal(f.manager.integrationStatus().feishu.relay.lastError, "native_read_rejected");
  f.manager.native = f.native; await f.manager.scan();
  assert.equal(f.manager.integrationStatus().feishu.relay.lastError, null);
  assert.equal(f.manager.integrationStatus().feishu.relay.failures, 0);
});

test("oversized single-turn output uses verified native progress without dispatching queued work or replaying text", async t => {
  const f = await fixture(t); await f.register(); f.manager.tools.push({ name: "wait_threads" });
  const sent = []; let oversized = true, messageId = "limited-1", calls = 0;
  f.manager.notifier = { enabled: true, sendMessage: async (text, options) => {
    const saved = JSON.parse(await readFile(f.manager.statePath, "utf8"));
    assert.equal(saved.sessions.existing.limitedTextDelivery.status, "dispatching");
    sent.push({ text, options }); return { code: 0 };
  } };
  const session = f.manager.state.sessions.existing;
  session.queue.push({ messageId: "must-not-dispatch" });
  f.manager.native = async (name, args, context) => {
    if (name === "wait_threads") { calls++; assert.equal(args.timeoutMs, 0); return { polls: [{ thread: { id: "existing", hostId: "local" },
      latestTurn: { id: "oversized", status: "completed" }, latestAssistantMessageId: messageId,
      latestAssistantMessage: { id: messageId, turnId: "oversized", phase: "final_answer", text: "原生最新公开回复" }, cursor: "progress-cursor" }] }; }
    if (name === "read_thread" && oversized) throw nativeToolRejection(name, { success: false,
      contentItems: [{ text: "Thread history exceeds the read size limit. A single oversized turn cannot be read by this tool." }] });
    return f.native(name, args, context);
  };
  await f.manager.scan(); assert.equal(sent.length, 1); assert.match(sent[0].options.title, /历史受限/);
  assert.ok(sent[0].options.uuid); assert.equal(session.limitedTextDelivery.status, "sent");
  assert.equal(session.queue.length, 1); assert.equal(f.calls.filter(c => c.name === "send_message_to_thread").length, 0);
  assert.equal(f.manager.integrationStatus().feishu.relay.mode, "latest-public-message");
  await f.manager.scan(); assert.equal(calls, 1); assert.equal(sent.length, 1);
  f.manager.nativeProgressPolls.clear(); await f.manager.scan(); assert.equal(sent.length, 1);
  oversized = false; session.queue = []; f.manager.nativeHistoryWindows.clear(); await f.manager.scan();
  assert.equal(f.manager.integrationStatus().feishu.relay.mode, "full-history");
  assert.equal(f.manager.integrationStatus().feishu.relay.lastError, null);
});

test("unknown limited-output delivery is recorded and not automatically repeated", async t => {
  const f = await fixture(t); await f.register(); f.manager.tools.push({ name: "wait_threads" }); let sends = 0;
  f.manager.notifier = { sendMessage: async () => { sends++; return null; } };
  f.manager.native = async (name, args, context) => name === "wait_threads" ? { polls: [{ thread: { id: "existing" },
    latestTurn: { id: "turn", status: "inProgress" }, latestAssistantMessageId: "text",
    latestAssistantMessage: { id: "text", turnId: "turn", phase: "commentary", text: "仅发送一次" } }] } : f.native(name, args, context);
  await f.manager.scanLimitedProgress("existing", f.manager.state.selectionEpoch);
  assert.equal(f.manager.state.sessions.existing.limitedTextDelivery.status, "unknown");
  f.manager.nativeProgressPolls.clear(); await f.manager.scanLimitedProgress("existing", f.manager.state.selectionEpoch);
  assert.equal(sends, 1);
});

test("limited progress cannot publish an old selected chat after a concurrent switch", async t => {
  const f = await fixture(t); await f.register(); f.tasks.set("other", { thread: { id: "other", cwd: f.root }, turns: [] }); await f.register("other");
  f.manager.selectSession("existing"); let release, started;
  const entered = new Promise(resolve => { started = resolve; }); let sends = 0;
  f.manager.notifier = { sendMessage: async () => { sends++; return { code: 0 }; } };
  f.manager.native = async (name, args, context) => {
    if (name !== "wait_threads") return f.native(name, args, context);
    started(); await new Promise(resolve => { release = resolve; });
    return { polls: [{ thread: { id: "existing" }, latestTurn: { id: "turn", status: "completed" }, latestAssistantMessageId: "reply",
      latestAssistantMessage: { id: "reply", turnId: "turn", phase: "final_answer", text: "旧会话内容" }, cursor: "must-not-advance" }] };
  };
  const pending = f.manager.scanLimitedProgress("existing", f.manager.state.selectionEpoch);
  await entered; f.manager.selectSession("other"); release(); await pending;
  assert.equal(sends, 0); assert.equal(f.manager.state.sessions.existing.seen["turn/reply"], undefined);
  assert.notEqual(f.manager.nativeProgressPolls.get("existing")?.cursor, "must-not-advance");
});
test("a per-send binding guard is rechecked after token acquisition and between chunks", async () => {
  const notifier = new FeishuNotifier({ appId: "isolated", appSecret: "test", chatId: "test-chat" });
  let current = true, posts = 0;
  notifier.getToken = async () => { current = false; return "test-token"; };
  notifier._postChunk = async () => { posts++; return { code: 0 }; };
  assert.equal(await notifier.sendMessage("public", { validateTarget: () => current }), null); assert.equal(posts, 0);
  current = true; notifier.getToken = async () => "test-token";
  notifier._postChunk = async () => { posts++; current = false; return { code: 0 }; };
  await notifier.sendMessage("x".repeat(20000), { validateTarget: () => current });
  assert.equal(posts, 1);
});

test("real native transport loss remains unready until a subsequent successful call", async t => {
  const f = await fixture(t); await f.register();
  f.manager.native = async () => { throw new Error("pipe unavailable"); };
  await f.manager.scan(); assert.equal(f.manager.health().ready, false);
  f.manager.native = f.native; await f.manager.scan(); assert.equal(f.manager.health().ready, true);
});

test("only explicit native rejection marks a live channel and raw response content never enters the error", () => {
  const error = nativeToolRejection("read_thread", { success: false, contentItems: [{ text: "private response must stay private" }] });
  assert.equal(error.nativeReachable, true); assert.equal(error.message, "Desktop rejected read_thread");
  assert.ok(!JSON.stringify(error).includes("private response"));
  for (const response of [undefined, null, {}, { success: null }]) assert.equal(nativeToolRejection("read_thread", response).nativeReachable, false);
});

test("resume can adopt a readable head turn when older history exceeds the native read limit", async t => {
  const f = await fixture(t); await f.register();
  f.tasks.set("large-chat", { thread: { id: "large-chat", title: "large-chat", cwd: f.root }, turns: [{ id: "head", status: "completed", items: [] }] });
  f.manager.state.resumeCandidates = [{ index: 0, threadId: "large-chat", alias: "large-chat", hostId: "local" }];
  f.manager.native = async (name, args, context) => {
    if (name === "read_thread" && args.threadId === "large-chat" && args.turnLimit > 1) {
      throw nativeToolRejection(name, { success: false, contentItems: [{ text: "Thread history exceeds the read size limit. Try fewer turns." }] });
    }
    return f.native(name, args, context);
  };
  const result = await f.manager.message(req("/resume 0"));
  assert.equal(result.session.threadId, "large-chat"); assert.equal(f.manager.state.selectedThreadId, "large-chat");
  assert.equal(f.calls.filter(c => ["send_message_to_thread", "create_thread"].includes(c.name)).length, 0);
  await f.manager.scan(); assert.equal(f.manager.state.sessions["large-chat"].state, "idle");
  assert.equal(f.manager.health().ready, true);
});

test("widening a reduced selection window seeds older replies and images instead of replaying them", async t => {
  const f = await fixture(t); await f.register(); let wide = false;
  const older = { id: "older", status: "completed", items: [
    { id: "old-reply", type: "agentMessage", phase: "final_answer", text: "历史回复不补发" },
    { id: "old-image", type: "imageView", path: path.join(f.root, "old.png") }] };
  const head = { id: "selection-head", status: "completed", items: [] };
  f.tasks.set("large-chat", { thread: { id: "large-chat", title: "large-chat", cwd: f.root }, turns: [older, head] });
  f.manager.state.resumeCandidates = [{ index: 0, threadId: "large-chat", alias: "large-chat", hostId: "local" }];
  f.manager.native = async (name, args, context) => {
    if (name !== "read_thread" || args.threadId !== "large-chat") return f.native(name, args, context);
    if (!wide && args.turnLimit > 1) throw nativeToolRejection(name, { success: false,
      contentItems: [{ text: "Thread history exceeds the read size limit. Try fewer turns." }] });
    const value = await f.native(name, args, context); value.turns = value.turns.slice(-args.turnLimit); return value;
  };
  await f.manager.message(req("/resume 0")); await f.manager.scan();
  const cards = []; f.manager.notifier = { enabled: true, chatId: "oc_test", sendMessage: async text => { cards.push(text); return { code: 0 }; } };
  wide = true; f.manager.nativeHistoryWindows.clear(); await f.manager.scan();
  assert.ok(!f.manager.state.events.some(e => e.type === "item.completed" && e.payload.snapshot === "历史回复不补发"));
  assert.equal(Object.values(f.manager.state.autoImages).length, 0);
  f.tasks.get("large-chat").turns.push({ id: "new-turn", status: "completed", items: [
    { id: "new-reply", type: "agentMessage", phase: "final_answer", text: "切换后的新回复" }] });
  await f.manager.scan(); assert.deepEqual(cards, ["切换后的新回复"]);
});

test("legacy /turn wait=true returns paired native reply rather than accepting silently", async t => {
  const f = await fixture(t); await f.register();
  const pending = f.http("/turn", { threadId: "existing", text: "兼容 DSH", wait: true });
  await new Promise(resolve => setTimeout(resolve, 30)); await f.manager.scan(); f.finish("existing", "DSH_COMPAT_OK"); await f.manager.scan();
  const response = await pending; assert.equal(response.body.status, "completed"); assert.equal(response.body.resultText, "DSH_COMPAT_OK"); assert.ok(response.body.turnId);
  assert.equal((await f.http("/threads/existing/release", {})).body.status, "notSubscribed");
});

test("protocol and text extraction do not export private reasoning; Feishu formatting retained", () => {
  assert.deepEqual(publicItems({ turns: [{ id: "x", items: [{ type: "reasoning", content: ["secret"] }] }] }), []);
  assert.equal(escapeMarkdown("C:\\work `C:\\code`"), "C:&#92;work `C:\\code`");
  assert.deepEqual(splitChunks("abcdef", 3), ["abc", "def"]);
});

test("runtime requests, responses, events, health and errors match the locked manager schema", async t => {
  const f = await fixture(t); await f.register();
  checkContract("healthResponse", f.manager.health());
  const request = req("schema probe"); checkContract("messageRequest", request);
  checkContract("messageAcceptedResponse", await f.manager.message(request));
  await f.manager.scan(); f.finish(); await f.manager.scan();
  for (const event of f.manager.state.events) checkContract("eventEnvelope", event);
  checkContract("pullEventsResponse", f.manager.pull(0));
  const bad = await f.http("/manager/messages", req("/unknown")); checkContract("errorResponse", bad.body);
  assert.throws(() => checkContract("eventEnvelope", { ...f.manager.state.events[0], seq: -1 }));
  assert.throws(() => checkContract("messageRequest", { ...request, messageId: "not-uuid" }));
});
