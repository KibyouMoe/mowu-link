import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID, createHash } from "node:crypto";
import { mkdtemp, mkdir, writeFile, readFile, rm, readdir, stat, unlink } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { DesktopManager, PROTOCOL } from "../desktop-manager.mjs";
import { MessageJournal } from "../message-journal.mjs";
import { LocalPublicJournal, publicJournalItem } from "../local-public-journal.mjs";

const sha = text => createHash("sha256").update(JSON.stringify(text)).digest("hex");
const request = (text, extra = {}) => ({ protocolVersion: PROTOCOL, messageId: randomUUID(),
  source: "trainer", origin: "cli", targetThreadId: "thread", text, delivery: "queue", ...extra });

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "worker-transcript-"));
  const calls = [], nativeCalls = [];
  const native = async (name, args) => {
    nativeCalls.push({ name, args });
    if (name === "read_thread") return { thread: { id: args.threadId, cwd: root }, turns: [] };
    if (name === "list_threads") return { threads: [] };
    if (name === "send_message_to_thread") return { turnId: "native-turn" };
    return {};
  };
  const tools = ["read_thread", "send_message_to_thread", "list_threads", "set_thread_title", "create_thread", "fork_thread"];
  const options = { native, listNativeTools: async () => tools.map(name => ({ name })), stateDir: root,
    config: { token: "isolated-transcript-token", port: 0, pollMs: 1000000, workspaceRoots: [root],
      nativeSenderThreadId: "sender", feishu: { transcriptEnabled: true, streamEnabled: true } } };
  const manager = await new DesktopManager(options).initialize();
  manager.handoffPaused = true;
  if (!manager.state.sessions.thread) {
    const session = manager.makeSession({ id: "thread", cwd: root }, { alias: "main", cwd: root, hostId: "local" });
    session.feishuChatId = "chat"; session.seenImages = {}; session.relayStatus = {};
    manager.state.sessions.thread = session; manager.state.selectedThreadId = "thread"; manager.state.selectionEpoch = 7;
  }
  const notifier = {
    enabled: true, chatId: "chat",
    sendMessage: async (text, opts) => {
      calls.push({ method: "POST", text, opts });
      const id = "card-" + calls.length;
      await opts.onDelivery?.({ phase: "accepted", messageId: id });
      return { code: 0, data: { message_id: id } };
    },
    updateMessage: async (id, text, opts) => {
      calls.push({ method: "PATCH", id, text, opts });
      return { code: 0, data: { message_id: id } };
    },
  };
  manager.notifier = notifier;
  t.after(async () => { if (!manager.closed) await manager.close(); await rm(root, { recursive: true, force: true }); });
  const flush = async () => {
    manager.handoffPaused = false;
    await manager.drainPublicTexts(); await manager.tail;
    manager.handoffPaused = true; clearTimeout(manager.publicTextTimer); manager.publicTextTimer = null;
  };
  const emit = (type, req, payload, extra) => manager.serial(() => manager.emit(type, req, payload, extra));
  const output = (text, key = "turn/item", phase = "final_answer") =>
    emit(phase === "commentary" ? "item.delta" : "item.completed", { targetThreadId: "thread", source: "assistant" },
      { text, snapshot: text, phase, itemKey: key }, { turnId: "turn" });
  return { root, manager, session: manager.session("thread"), calls, nativeCalls, options, flush, emit, output };
}

test("CLI ingress is durably archived before model dispatch and mirrors once after acceptance", async t => {
  const f = await fixture(t), req = request("只抄送一次");
  await f.manager.mirrorInput(req);
  await f.manager.message(req);
  await f.flush();
  const page = await f.manager.messageJournal.list();
  assert.equal(page.records.length, 1); assert.equal(page.records[0].source, "cli");
  assert.equal(page.records[0].delivery.state, "sent");
  assert.deepEqual(f.calls.map(c => c.text), ["只抄送一次"]);
  assert.equal(f.nativeCalls.filter(c => c.name === "send_message_to_thread").length, 0);
  assert.equal(f.session.queue.length, 1);
});

test("Feishu originals are linked to their real message ID without another card", async t => {
  const f = await fixture(t), req = request("飞书已有", { origin: undefined });
  f.manager.state.feishuReceipts[req.messageId] = { incomingMessageId: "om_original" };
  await f.manager.message(req); await f.flush();
  const record = (await f.manager.messageJournal.list()).records[0];
  assert.equal(record.source, "feishu"); assert.equal(record.delivery.pages.original.messageId, "om_original");
  assert.equal(f.calls.length, 0);
});

test("native input observation never invokes a model writer and cannot echo an accepted ingress", async t => {
  const f = await fixture(t), req = request("同一原话");
  await f.manager.mirrorInput(req);
  f.manager.state.receipts[req.messageId] = { request: req, nativeTargetThreadId: "thread",
    nativeInputHash: sha("同一原话"), nativeTurnId: "native-turn", nativeDispatch: { status: "accepted" } };
  await f.manager.observeNativeInput(f.session, { key: "native/input", turnId: "native-turn", text: "同一原话" });
  await f.manager.observeNativeInput(f.session, { key: "native/another", turnId: "another-turn", text: "桌面直接输入" });
  await f.flush();
  assert.deepEqual(f.calls.map(c => c.text), ["同一原话", "桌面直接输入"]);
  assert.equal(f.nativeCalls.filter(c => c.name === "send_message_to_thread").length, 0);
  assert.equal((await f.manager.messageJournal.list()).records.filter(r => r.source === "native").length, 1);
});

test("two identical uncertain ingress writes stay uncertain and do not produce a third echo", async t => {
  const f = await fixture(t);
  for (const id of [randomUUID(), randomUUID()]) f.manager.state.receipts[id] = {
    request: request("重复", { messageId: id }), nativeTargetThreadId: "thread",
    nativeInputHash: sha("重复"), nativeDispatch: { status: "unknown" } };
  await f.manager.observeNativeInput(f.session, { key: "native/unknown", turnId: "turn", text: "重复" });
  await f.flush();
  assert.equal(f.calls.length, 0);
  assert.ok(f.manager.state.events.some(e => e.type === "message.correlation_unconfirmed"));
  assert.ok(Object.values(f.manager.state.receipts).every(r => r.nativeDispatch.status === "unknown"));
});

test("archive and pending delivery survive complete manager recreation on the same session", async t => {
  const f = await fixture(t); await f.output("断线期间的回复");
  await f.manager.close();
  const restored = await new DesktopManager(f.options).initialize();
  try {
    restored.handoffPaused = true; const calls = [];
    restored.notifier = { enabled: true, chatId: "chat", sendMessage: async text => {
      calls.push(text); return { code: 0, data: { message_id: "restored-card" } }; } };
    restored.handoffPaused = false; await restored.drainPublicTexts();
    assert.equal(restored.state.selectedThreadId, "thread");
    assert.deepEqual(calls, ["断线期间的回复"]);
    assert.equal((await restored.messageJournal.list()).records[0].delivery.state, "sent");
  } finally { await restored.close(); }
});

test("journal-only commit repairs its index and rehydrates an orphaned output after crash", async t => {
  const f = await fixture(t);
  const record = await f.manager.messageJournal.record({ seq: 200, logicalId: "output:thread:orphan",
    direction: "output", source: "assistant", threadId: "thread", text: "已留档但没入队", phase: "final_answer" });
  const ordered = await readdir(path.join(f.root, "message-journal/ordered"));
  await unlink(path.join(f.root, "message-journal/ordered", ordered[0]));
  assert.equal(await f.manager.messageJournal.recover(), 200);
  await f.manager.recoverTranscript(); await f.flush();
  assert.deepEqual(f.calls.map(c => c.text), ["已留档但没入队"]);
  assert.equal((await f.manager.messageJournal.receipt(record.id)).state, "sent");
});

test("input and output continue mirroring when another managed session is selected", async t => {
  const f = await fixture(t); await f.output("主会话仍在回复");
  f.manager.state.sessions.other = { ...f.session, threadId: "other", publicTextCards: {} };
  f.manager.selectSession("other");
  await f.flush();
  assert.equal(f.calls.length, 1); assert.equal(f.calls[0].opts.threadId, "thread");
});

test("upgrade preserves old card IDs/checkpoints and ignores historical native input", async t => {
  const f = await fixture(t);
  f.manager.config.feishu.transcriptEnabled = false;
  f.manager.enqueuePublicText(f.session, "turn", "turn/item", "旧卡片", 7, false);
  await f.flush();
  f.session.publicJournalCheckpoint = { epoch: 7, offset: 123 };
  f.manager.config.feishu.transcriptEnabled = true;
  f.manager.migrateTranscript();
  await f.output("旧卡片继续更新", "turn/item");
  await f.manager.observeNativeInput(f.session, { key: "old/input", text: "历史输入", turnId: "old" }, "2000-01-01T00:00:00Z");
  await f.flush();
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "PATCH"]);
  assert.equal(f.calls[1].id, "card-1");
  assert.equal(f.session.publicJournalCheckpoint.offset, 123);
});

test("known unsent failures retry safely; unknown create is retained without blocking later text", async t => {
  const f = await fixture(t);
  let attempts = 0;
  f.manager.notifier.sendMessage = async (text, opts) => {
    f.calls.push({ method: "POST", text, opts }); attempts++;
    if (attempts === 1) { await opts.onDelivery({ phase: "not_attempted", reason: "token_unavailable" }); return null; }
    return { code: 0, data: { message_id: "safe-retry" } };
  };
  await f.output("等待恢复"); await f.flush();
  const entry = Object.values(f.manager.state.publicTextOutbox)[0];
  assert.equal(entry.status, "pending"); assert.ok(entry.retryAt > Date.now());
  entry.retryAt = 0; await f.flush();
  assert.equal(f.calls[0].opts.uuid, f.calls[1].opts.uuid);
  assert.equal((await f.manager.messageJournal.list()).records[0].delivery.state, "sent");
  f.manager.notifier.sendMessage = async text => { f.calls.push({ text }); return null; };
  const unknown = await f.output("结果未知", "turn/unknown"); await f.flush();
  f.manager.notifier.sendMessage = async text => { f.calls.push({ text }); return { code: 0, data: { message_id: "later" } }; };
  await f.output("后续回复", "turn/later"); await f.flush();
  assert.equal(f.calls.filter(c => c.text === "结果未知").length, 1);
  assert.equal((await f.manager.messageJournal.receipt(unknown.transcriptId)).state, "unknown");
  assert.ok(f.calls.some(c => c.text === "后续回复"));
});

test("known card update retries PATCH without ever creating a replacement card", async t => {
  const f = await fixture(t);
  await f.output("a", "turn/stream", "commentary"); await f.flush();
  let attempts = 0;
  f.manager.notifier.updateMessage = async (id, text) => {
    f.calls.push({ method: "PATCH", id, text }); return ++attempts === 1 ? null : { code: 0, data: { message_id: id } };
  };
  await f.output("ab", "turn/stream"); await f.flush();
  const entry = Object.values(f.manager.state.publicTextOutbox)[0]; entry.retryAt = 0;
  await f.flush();
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "PATCH", "PATCH"]);
  assert.equal(f.calls[1].id, f.calls[2].id);
});

test("coalesced snapshots remain in the archive with honest superseded/sent receipts", async t => {
  const f = await fixture(t);
  const first = await f.output("a", "turn/stream", "commentary");
  const last = await f.output("abc", "turn/stream");
  await f.flush();
  assert.deepEqual(f.calls.map(c => c.text), ["abc"]);
  assert.equal((await f.manager.messageJournal.receipt(first.transcriptId)).state, "superseded");
  assert.equal((await f.manager.messageJournal.receipt(last.transcriptId)).state, "sent");
});

test("paged Unicode has complete receipts and input markdown never uploads a local file", async t => {
  const f = await fixture(t), text = "汉😀".repeat(4500);
  const event = await f.output(text); await f.flush();
  assert.equal(f.calls.map(c => c.text).join(""), text);
  const receipt = await f.manager.messageJournal.receipt(event.transcriptId);
  assert.equal(receipt.state, "sent"); assert.ok(receipt.expectedPages >= 2);
  const images = []; f.manager.publicImageRelay.observe = (...args) => images.push(args);
  await f.manager.mirrorInput(request("![输入](/tmp/private.png)")); await f.flush();
  assert.equal(images.length, 0);
});

test("public journal opt-in projects native user text while excluding reasoning and raw tools", async t => {
  const f = await fixture(t), id = "01a17e53-76b1-7e63-bd85-2161ef913ce8";
  const event = item => ({ timestamp: new Date().toISOString(), type: "event_msg", payload: {
    type: "item_completed", thread_id: id, turn_id: "turn", item } });
  const input = event({ type: "UserMessage", id: "native-user", content: [{ type: "Text", text: "原生输入" }] });
  assert.equal(publicJournalItem(input, id), null);
  assert.equal(publicJournalItem(input, id, { includeInputs: true }).item.text, "原生输入");
  const file = path.join(f.root, "rollout-" + id + ".jsonl");
  await writeFile(file, [ { type: "session_meta", payload: { id, cwd: f.root } }, input,
    event({ type: "Reasoning", id: "secret", text: "private-thought" }),
    event({ type: "CommandExecution", id: "tool", command: "secret-tool-args" }) ].map(x => JSON.stringify(x)).join("\n") + "\n");
  const batch = await new LocalPublicJournal(f.root).read({ threadId: id, cwd: f.root }, null,
    { epoch: 1, turnId: "turn", includeInputs: true });
  assert.deepEqual(batch.events.map(e => e.item.text), ["原生输入"]);
});

test("transcript HTTP is authenticated, paginated, and independent of bounded event retention", async t => {
  const f = await fixture(t); await f.output("first", "turn/first"); await f.output("second", "turn/second");
  f.manager.state.events = []; await f.manager.save();
  const url = await f.manager.listen();
  const get = (query = "", token = f.manager.config.token) => fetch(url + "/manager/transcript" + query,
    { headers: { authorization: "Bearer " + token } });
  assert.equal((await get("", "wrong")).status, 401);
  const first = await (await get("?limit=1")).json();
  assert.equal(first.records.length, 1); assert.ok(first.hasMore);
  const second = await (await get("?after=" + first.nextCursor)).json();
  assert.deepEqual(second.records.map(r => r.text), ["second"]);
  assert.equal((await get("?after=../../escape")).status, 400);
  assert.equal((await get("?limit=0")).status, 400);
  assert.equal((await get("?direction=analysis")).status, 400);
  const mode = (await stat(path.join(f.root, "message-journal/records", first.records[0].id + ".json"))).mode;
  // Windows mode represents writability, not POSIX permissions or ACLs.
  if (process.platform === "win32") assert.equal(mode & 0o200, 0o200);
  else assert.equal(mode & 0o777, 0o600);
});

test("public phases are archived; analysis and nonconversation payloads are excluded", async t => {
  const f = await fixture(t);
  await f.emit("item.completed", { targetThreadId: "thread" }, { phase: "analysis", text: "private" });
  await f.emit("tool.completed", { targetThreadId: "thread" }, { text: "tool arguments" });
  await f.manager.publicReply({ targetThreadId: "thread", messageId: randomUUID() }, "命令回执");
  await f.flush();
  assert.deepEqual((await f.manager.messageJournal.list()).records.map(r => r.text), ["命令回执"]);
  assert.deepEqual(f.calls.map(c => c.text), ["命令回执"]);
});

test("managed public images receive durable receipts after selection moves; manual target remains selected", async t => {
  const f = await fixture(t);
  f.manager.state.sessions.other = { ...f.session, threadId: "other" };
  f.manager.selectSession("other");
  f.manager.publicImageRelay.observe(f.session, { turns: [{ id: "turn", items: [
    { type: "imageView", id: "displayed", path: path.join(f.root, "public.png") }] }] }, 7);
  assert.throws(() => f.manager.imageTarget("thread"), error => error.body?.error === "selected_target_changed");
  const sent = [];
  f.manager.sendImage = async (body, validate, entry) => {
    validate(); assert.equal(f.manager.imageTarget(body.threadId, entry).threadId, "thread");
    sent.push(body); return { feishuMessageId: "om_public_image" };
  };
  await f.flush();
  assert.equal(sent.length, 1);
  const records = (await f.manager.messageJournal.list()).records;
  assert.equal(records[0].kind, "image"); assert.equal(records[0].delivery.state, "sent");
  assert.equal(Object.values(records[0].delivery.pages)[0].messageId, "om_public_image");
});

test("archive write failure rejects input before any model write or Feishu copy", async t => {
  const f = await fixture(t);
  f.manager.messageJournal.record = async () => { throw Error("simulated disk full"); };
  await assert.rejects(f.manager.message(request("不能丢的输入", { delivery: "steer" })));
  await f.flush();
  assert.equal(f.nativeCalls.filter(c => c.name === "send_message_to_thread").length, 0);
  assert.equal(f.calls.length, 0);
});

test("crash during unconfirmed card creation never POSTs it again after manager recreation", async t => {
  const f = await fixture(t), event = await f.output("创建结果未知");
  const entry = Object.values(f.manager.state.publicTextOutbox)[0];
  entry.status = "dispatching"; await f.manager.save(); await f.manager.close();
  const restored = await new DesktopManager(f.options).initialize();
  try {
    restored.handoffPaused = true; let posts = 0;
    restored.notifier = { enabled: true, chatId: "chat", sendMessage: async () => { posts++; return null; } };
    restored.handoffPaused = false; await restored.drainPublicTexts();
    assert.equal(posts, 0);
    assert.equal((await restored.messageJournal.receipt(event.transcriptId)).state, "unknown");
  } finally { await restored.close(); }
});

test("local CLI uses the authenticated shared ingress, retains its UUID, and never silently retries", async t => {
  const f = await fixture(t);
  const { sendWorkerMessage } = await import("../bin/message-worker.mjs");
  const tokenFile = path.join(f.root, "token"), configFile = path.join(f.root, "config.json");
  await writeFile(tokenFile, "  cli-test-token\n");
  await writeFile(configFile, JSON.stringify({ tokenFile, port: 18905 }));
  const calls = [], messageId = randomUUID();
  const accepted = await sendWorkerMessage({ configFile, text: "本地统一入口", messageId,
    fetchImpl: async (url, opts) => { calls.push({ url, opts }); return {
      ok: true, json: async () => ({ accepted: true, messageId }) }; } });
  assert.equal(accepted.messageId, messageId);
  assert.equal(calls[0].opts.headers.authorization, "Bearer cli-test-token");
  assert.equal(JSON.parse(calls[0].opts.body).origin, "cli");
  assert.equal(calls[0].url, "http://127.0.0.1:18905/manager/messages");
  let attempts = 0;
  await assert.rejects(sendWorkerMessage({ configFile, text: "未知", messageId,
    fetchImpl: async () => { attempts++; throw Error("lost reply"); } }), new RegExp(messageId));
  assert.equal(attempts, 1);
  await writeFile(configFile, JSON.stringify({ tokenFile, host: "remote.example", port: 18905 }));
  await assert.rejects(sendWorkerMessage({ configFile, text: "绝不泄露", fetchImpl: async () => { throw Error("must not send"); } }), /loopback/);
});

test("local slash-command input and reply share the archive without a model turn", async t => {
  const f = await fixture(t);
  const result = await f.manager.message(request("/status", { delivery: "steer" }));
  await f.flush();
  assert.equal(result.publicReplyArchived, true);
  assert.deepEqual((await f.manager.messageJournal.list()).records.map(r => r.direction), ["input", "output"]);
  assert.equal(f.calls.length, 2); assert.match(f.calls[1].text, /当前会话/);
  assert.equal(f.nativeCalls.filter(c => c.name === "send_message_to_thread").length, 0);
});

test("an inline image keeps its existing split card layout across transcript migration", async t => {
  const f = await fixture(t);
  const images = []; f.manager.publicImageRelay.observe = (...args) => images.push(args);
  f.manager.config.feishu.transcriptEnabled = false;
  f.manager.state.selectionEpoch = 3;
  f.manager.enqueuePublicText(f.session, "turn", "turn/item", "before\n\n![图](/tmp/photo.png)\n\nafter", 3, false);
  await f.flush();
  f.manager.config.feishu.transcriptEnabled = true;
  f.manager.migrateTranscript();
  f.manager.state.selectionEpoch = 7;
  await f.output("before\n\n![图](/tmp/photo.png)\n\nafter updated");
  await f.flush();
  assert.deepEqual(f.calls.map(c => c.method), ["POST", "POST", "PATCH", "PATCH"]);
  assert.equal(f.calls[2].id, "card-1"); assert.equal(f.calls[3].id, "card-2");
});

test("a stream returning to earlier text records a new revision instead of reusing a superseded record", async t => {
  const f = await fixture(t);
  const first = await f.output("a", "turn/revert", "commentary");
  await f.output("ab", "turn/revert", "commentary");
  const last = await f.output("a", "turn/revert", "commentary");
  await f.flush();
  assert.notEqual(first.transcriptId, last.transcriptId);
  const page = await f.manager.messageJournal.list();
  assert.deepEqual(page.records.map(r => r.text), ["a", "ab", "a"]);
  assert.equal((await f.manager.messageJournal.receipt(last.transcriptId)).state, "sent");
  assert.equal((await f.manager.messageJournal.receipt(first.transcriptId)).state, "superseded");
});

test("Feishu card answers retain their channel label and recover their pending mirror without an original text receipt",async t=>{
 const f=await fixture(t),req=request("飞书：提问的点选答案",{origin:"feishu"});
 await f.manager.mirrorInput(req);
 let record=(await f.manager.messageJournal.list()).records[0];assert.equal(record.source,"feishu");
 f.manager.state.publicTextOutbox={};await f.manager.save();
 await f.manager.recoverTranscript();await f.flush();
 assert.equal(f.calls.length,1);assert.ok(f.calls[0].opts.title.includes("飞书输入"));
 await f.manager.recoverTranscript();await f.flush();assert.equal(f.calls.length,1);
});
test("a Feishu-looking text prefix never changes an actual local API input source",async t=>{
 const f=await fixture(t);await f.manager.mirrorInput(request("飞书：这仍是本地 API 输入",{origin:undefined}));
 await f.flush();const record=(await f.manager.messageJournal.list()).records[0];
 assert.equal(record.source,"api");assert.ok(f.calls[0].opts.title.includes("本地 API 输入"));
});

test("peer input identifies its actual sender and keeps that title after transcript recovery", async t => {
  const f = await fixture(t);
  const req = request("这里是 TamamoCross。观察就绪。", { source: "hermes", origin: "worker-chat", workerName: "TamamoCross" });
  await f.manager.mirrorInput(req);
  const record = (await f.manager.messageJournal.list()).records[0];
  assert.equal(record.source, "worker"); assert.equal(record.workerName, "TamamoCross");
  f.manager.state.publicTextOutbox = {}; await f.manager.save();
  await f.manager.recoverTranscript(); await f.flush();
  assert.equal(f.calls.length, 1); assert.match(f.calls[0].opts.title, /TamamoCross私聊$/);
  assert.equal(f.calls[0].opts.title.includes("溪流"), false);
  assert.match(f.manager.inputMirrorTitle("worker"), /Worker私聊$/);
});

test("legacy Hermes-source peer records recover their sender from the original receipt", async t => {
  const f = await fixture(t);
  const req = request("legacy peer input", { source: "hermes", origin: "worker-chat", workerName: "TamamoCross" });
  f.manager.state.receipts[req.messageId] = { request: req };
  await f.manager.messageJournal.record({ seq: 1, logicalId: "input:" + req.messageId,
    direction: "input", source: "hermes", threadId: "thread", messageId: req.messageId,
    text: req.text, phase: "input" });
  await f.manager.recoverTranscript(); await f.flush();
  assert.equal(f.calls.length, 1); assert.match(f.calls[0].opts.title, /TamamoCross私聊$/);
});
