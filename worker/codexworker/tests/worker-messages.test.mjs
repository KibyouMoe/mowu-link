import assert from "node:assert/strict";
import test from "node:test";
import { createHash, randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { DesktopManager } from "../desktop-manager.mjs";
import RouterClient from "../router-client.mjs";
import { startWorkerMessaging } from "../worker-messages.mjs";

const worker = "OguriCap", peer = "SuperCreek";
const png = Buffer.from("89504e470d0a1a0a00000000", "hex");
const sha = createHash("sha256").update(png).digest("hex");
const ref = { id: sha, sha256: sha, size: png.length, media_type: "image/png", name: "image.png" };
const message = (extra = {}) => ({ message_id: randomUUID(), from: peer, to: worker, text: "hello",
  reply_to: null, attachments: [], seq: 1, status: "queued", created_at: 1, acked_at: null, ...extra });
const gate = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

// Match the server's full-message envelopes and exact-payload retry contract.
function routerFixture() {
  const messages = new Map(), payloads = new Map(), calls = [];
  const router = { messages, calls, add(value) { messages.set(value.message_id, structuredClone(value)); return value; },
    async request(method, endpoint, body) {
      calls.push({ method, endpoint, body: structuredClone(body) });
      const url = new URL(endpoint, "http://router.test");
      if (method === "POST" && url.pathname === "/messages") {
        const previous = messages.get(body.message_id);
        if (previous && JSON.stringify(payloads.get(body.message_id)) !== JSON.stringify(body))
          throw Object.assign(Error("conflict"), { status: 409, code: "message_id_conflict" });
        if (!previous) {
          payloads.set(body.message_id, structuredClone(body));
          router.add(message({ ...body, attachments: (body.attachments ?? []).map(a => ({ ...ref, ...a })), seq: messages.size + 1 }));
        }
        await router.afterPost?.(body);
        const current = messages.get(body.message_id);
        return { ok: true, message_id: current.message_id, status: current.status, message: structuredClone(current), duplicate: Boolean(previous) };
      }
      if (method === "GET" && url.pathname === "/messages") {
        const name = url.searchParams.get("worker"), after = Number(url.searchParams.get("after")), limit = Number(url.searchParams.get("limit"));
        const pending = [...messages.values()].filter(m => m.to === name && m.status === "queued" && m.seq > after).sort((a, b) => a.seq - b.seq);
        const page = pending.slice(0, Math.min(limit, router.pageSize ?? 100)), more = page.length < pending.length;
        return { ok: true, worker: name, count: page.length, messages: structuredClone(page),
          has_more: more, next_after: more ? page.at(-1).seq : null };
      }
      const ack = /^\/messages\/([^/]+)\/ack$/.exec(url.pathname);
      if (method === "POST" && ack) {
        const current = messages.get(ack[1]); assert.ok(current);
        assert.deepEqual(body, { worker: current.to }, "ACK derives its recipient from the local owner");
        await router.beforeAck?.(current);
        const already = current.status === "accepted";
        current.status = "accepted"; current.acked_at = 2;
        return { ok: true, message_id: current.message_id, status: "accepted", message: structuredClone(current), already };
      }
      assert.fail(`Unexpected endpoint: ${method} ${endpoint}`);
    },
  };
  return router;
}
async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "worker-messages-"));
  const router = routerFixture(), nativeCalls = [], mirrors = [], replies = [], requests = [];
  const native = async (name, args) => {
    nativeCalls.push({ name, args });
    if (name === "send_message_to_thread") return { threadId: args.threadId, turnId: "turn-1" };
    assert.fail(`Peer chat must not call native tool ${name}`);
  };
  const config = { nativeSenderThreadId: "sender", messageSourceLabels: false,
    workspaceRoots: [root], feishu: { transcriptEnabled: true } };
  const manager = new DesktopManager({ native, stateDir: root, config });
  for (const id of ["thread-1", "thread-2"])
    manager.state.sessions[id] = manager.makeSession({ id }, { alias: id, cwd: root, hostId: "local" });
  manager.state.selectedThreadId = "thread-1";
  manager.deliveryState = async () => ({ turns: [] });
  const mirrorInput = manager.mirrorInput.bind(manager), publicReply = manager.publicReply.bind(manager), nativeMessage = manager.message.bind(manager);
  manager.mirrorInput = async value => { mirrors.push(value); return mirrorInput(value); };
  manager.publicReply = async (...args) => { replies.push(args); return publicReply(...args); };
  manager.message = async value => { requests.push(value); return nativeMessage(value); };
  await manager.save();
  const client = { name: worker, url: "http://router.test", headers: () => ({ "X-Router-Token": "test-token" }),
    _request: (...args) => router.request(...args) };
  const options = { manager, client, worker }, service = startWorkerMessaging(options);
  const sentFile = id => path.join(root, "worker-messages", createHash("sha256").update(worker).digest("hex"), "sent", id + ".json");
  t.after(async () => { await service.close(); await manager.tail; await rm(root, { recursive: true, force: true }); });
  return { root, router, nativeCalls, mirrors, replies, requests, manager, client, options, service, nativeMessage, sentFile };
}
const ackCalls = router => router.calls.filter(c => c.endpoint.endsWith("/ack"));
async function dispatch(f) {
  const session = f.manager.state.sessions["thread-1"];
  await f.manager.dispatchQueued(session, session.queue.shift(), { turns: [] });
}

test("send derives the authenticated sender, persists router acceptance before mirroring and never sends native input", async t => {
  const f = await fixture(t), messageId = randomUUID(), original = f.manager.publicReply;
  f.manager.publicReply = async (...args) => {
    const receipt = JSON.parse(await readFile(f.sentFile(messageId), "utf8"));
    assert.equal(receipt.status, "accepted"); assert.equal(receipt.router.status, "queued");
    return original(...args);
  };
  const result = await f.service.send({ messageId, from: "forged", worker: "forged", to: peer, text: "ordinary chat" });
  assert.equal(result.status, "queued"); assert.equal(result.mirror, "accepted");
  assert.deepEqual(f.router.calls[0].body, { message_id: messageId, from: worker, to: peer, text: "ordinary chat" });
  assert.equal(f.replies.length, 1); assert.equal(f.requests.length, 0); assert.equal(f.nativeCalls.length, 0);
  const journal = await f.manager.messageJournal.list();
  assert.equal(journal.records.length, 1); assert.equal(journal.records[0].direction, "output");
  assert.match(journal.records[0].text, /ordinary chat/);
  assert.ok(Object.keys(f.manager.state.publicTextOutbox).length, "existing Feishu outbox owns the public mirror");
});

test("concurrent send retries and a service restart mirror once with the original UUID", async t => {
  const f = await fixture(t), request = { messageId: randomUUID(), to: peer, text: "once" };
  await Promise.all([f.service.send(request), f.service.send({ ...request })]);
  await f.service.close(); f.manager.state.selectedThreadId = "thread-2";
  const next = startWorkerMessaging(f.options); t.after(() => next.close());
  const retry = await next.send(request);
  assert.equal(retry.duplicate, true); assert.equal(f.router.calls.length, 1); assert.equal(f.replies.length, 1);
  assert.equal(f.replies[0][0].targetThreadId, "thread-1");
  await assert.rejects(next.send({ ...request, text: "changed" }), { code: "message_id_conflict" });
  assert.equal(f.router.calls.length, 1);
});

test("a new manager instance reads durable send/receive receipts without mirroring or dispatching twice", async t => {
  const f = await fixture(t), outgoing = { messageId: randomUUID(), to: peer, text: "persisted outgoing" };
  const value = f.router.add(message());
  await f.service.send(outgoing); await f.service.receive(value); await f.service.close();
  const restored = new DesktopManager({ native: async () => assert.fail("no new native write"), stateDir: f.root, config: f.manager.config });
  restored.state = JSON.parse(await readFile(restored.statePath, "utf8"));
  restored.publicReply = async () => assert.fail("no second outgoing mirror");
  restored.mirrorInput = async () => assert.fail("no second incoming mirror");
  const next = startWorkerMessaging({ ...f.options, manager: restored }); t.after(() => next.close());
  assert.equal((await next.send(outgoing)).duplicate, true);
  assert.equal((await next.receive(value)).duplicate, true);
  assert.equal(restored.state.sessions["thread-1"].queue.length, 1);
  assert.equal(f.router.calls.filter(c => c.endpoint === "/messages").length, 1);
});

test("an unknown POST outcome is durable and only an explicit exact-ID retry resolves it", async t => {
  const f = await fixture(t), request = { messageId: randomUUID(), to: peer, text: "lost response" };
  let failOnce = true;
  f.router.afterPost = async () => { if (failOnce) { failOnce = false; throw Error("connection lost after commit"); } };
  await assert.rejects(f.service.send(request), error => error.messageId === request.messageId && error.outcome === "unknown" && error.retrySameMessageId);
  assert.equal(f.router.calls.length, 1); assert.equal(f.replies.length, 0);
  assert.equal(JSON.parse(await readFile(f.sentFile(request.messageId), "utf8")).status, "unknown");
  await f.service.close(); const next = startWorkerMessaging(f.options); t.after(() => next.close());
  await next.send(request);
  assert.equal(f.router.messages.size, 1); assert.equal(f.router.calls.length, 2);
  assert.deepEqual(f.router.calls[0].body, f.router.calls[1].body); assert.equal(f.replies.length, 1);
});

test("uncertain public mirror is not replayed after restart", async t => {
  const f = await fixture(t), request = { messageId: randomUUID(), to: peer, text: "mirror outcome unknown" };
  let writes = 0;
  f.manager.publicReply = async () => { writes++; throw Error("lost after public write"); };
  assert.equal((await f.service.send(request)).mirror, "unknown");
  await f.service.close(); const next = startWorkerMessaging(f.options); t.after(() => next.close());
  assert.equal((await next.send(request)).mirror, "unknown");
  assert.equal(writes, 1); assert.equal(f.router.calls.length, 1);
});

test("send validation rejects bad IDs, destinations, text, reply IDs and local attachment paths before posting", async t => {
  const f = await fixture(t), request = { messageId: randomUUID(), to: peer, text: "hello" };
  for (const change of [{ messageId: undefined }, { messageId: "not-a-uuid" }, { to: "" }, { to: "bad\nname" },
    { to: worker }, { text: " " }, { text: "a".repeat(20001) }, { text: "\ud800" }, { replyTo: "bad" },
    { attachments: [{ id: sha, imagePath: "C:\\private\\photo.png" }] }, { attachments: [{ id: "../private" }] }])
    await assert.rejects(f.service.send({ ...request, ...change }));
  assert.equal(f.router.calls.length, 0); assert.equal(f.replies.length, 0);
  await f.service.send({ ...request, text: "😀".repeat(20000) });
});

test("duplicate full SSE payloads queue one manager input and mirror once, including after restart and switch", async t => {
  const f = await fixture(t), value = f.router.add(message({ reply_to: randomUUID(), text: "/goal this remains ordinary chat" }));
  const first = await f.service.receive(value);
  assert.equal(first.action, "message_queued"); assert.equal(first.acked, true);
  await f.service.close(); f.manager.state.selectedThreadId = "thread-2";
  const next = startWorkerMessaging(f.options); t.after(() => next.close());
  assert.equal((await next.receive({ message: { ...value, status: "accepted", acked_at: 2 } })).duplicate, true);
  assert.equal(f.mirrors.length, 1); assert.equal(f.manager.state.sessions["thread-1"].queue.length, 1);
  assert.equal(f.manager.state.sessions["thread-2"].queue.length, 0);
  const request = f.requests[0];
  assert.equal(request.messageId, value.message_id); assert.equal(request.peerMessageId, value.message_id);
  assert.equal(request.source, "hermes"); assert.equal(request.origin, "worker-chat"); assert.equal(request.workerName, peer);
  assert.equal(request.reply_to, value.reply_to);
  assert.ok(request.text.startsWith(`[worker ${peer} message ${value.message_id} reply_to ${value.reply_to}]\n`));
  assert.match(request.text, /ordinary worker chat/); assert.match(request.text, /bridge_send_worker_message/); assert.match(request.text, /avoid automatic reply loops/);
  await dispatch(f);
  assert.deepEqual(f.nativeCalls.map(c => c.name), ["send_message_to_thread"]);
  assert.ok(f.nativeCalls[0].args.prompt.startsWith("[worker "));
  assert.equal(f.router.calls.filter(c => c.endpoint === "/messages").length, 0, "receive never replies automatically");
  assert.equal((await f.manager.messageJournal.list({ direction: "input" })).records.length, 1);
});

test("foreign non-v4 UUIDs keep their original peer metadata and ACK while using a stable native v4 receipt", async t => {
  const f = await fixture(t), original = "6ba7b810-9dad-11d1-80b4-00c04fd430c8";
  const value = f.router.add(message({ message_id: original, reply_to: "6ba7b811-9dad-11d1-80b4-00c04fd430c8" }));
  const first = await f.service.receive(value);
  assert.notEqual(first.nativeMessageId, original);
  assert.match(first.nativeMessageId, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(f.requests[0].peerMessageId, original); assert.equal(f.requests[0].reply_to, value.reply_to);
  assert.ok(f.requests[0].text.includes(`message ${original} reply_to ${value.reply_to}`));
  assert.equal(ackCalls(f.router)[0].endpoint, `/messages/${original}/ack`);
  await f.service.close(); const next = startWorkerMessaging(f.options); t.after(() => next.close());
  const repeat = await next.receive(value);
  assert.equal(repeat.nativeMessageId, first.nativeMessageId); assert.equal(repeat.duplicate, true);
  assert.equal(f.manager.state.sessions["thread-1"].queue.length, 1); assert.equal(f.mirrors.length, 1);
  const sent = await next.send({ messageId: "6ba7b812-9dad-11d1-80b4-00c04fd430c8", to: peer, text: "foreign UUID sender", replyTo: original });
  assert.equal(sent.messageId, "6ba7b812-9dad-11d1-80b4-00c04fd430c8");
  assert.equal(f.router.calls.find(c => c.endpoint === "/messages").body.reply_to, original);
});

test("wrong recipient, conflicting UUID content and unrelated manager UUIDs are never acknowledged", async t => {
  const f = await fixture(t);
  await assert.rejects(f.service.receive(message({ to: "OtherWorker" })), { code: "wrong_recipient" });
  assert.equal(f.mirrors.length, 0); assert.equal(f.requests.length, 0); assert.equal(f.router.calls.length, 0);
  const value = f.router.add(message()); await f.service.receive(value);
  const count = ackCalls(f.router).length;
  await assert.rejects(f.service.receive({ ...value, text: "different" }), { code: "message_id_conflict" });
  assert.equal(ackCalls(f.router).length, count);
  const reused = message();
  f.manager.state.receipts[reused.message_id] = { request: { messageId: reused.message_id, text: "unrelated", targetThreadId: "thread-1" } };
  await assert.rejects(f.service.receive(reused), { code: "message_id_conflict" });
  assert.equal(f.mirrors.length, 1); assert.equal(ackCalls(f.router).length, count);
});

test("inbox excludes another recipient even if the router includes it in the current worker's page", async t => {
  const f = await fixture(t), local = message(), foreign = message({ to: "OtherWorker" });
  f.client._request = async () => ({ ok: true, worker, messages: [foreign, local], has_more: false });
  const result = await f.service.inbox({ worker: "OtherWorker" });
  assert.deepEqual(result.messages.map(m => m.message_id), [local.message_id]);
  assert.equal(f.requests.length, 0); assert.equal(f.mirrors.length, 0);
});

test("ACK waits for durable manager acceptance, even when the response is already accepted in memory", async t => {
  const f = await fixture(t), value = f.router.add(message()), entered = gate(), release = gate(), save = f.manager.save.bind(f.manager);
  t.after(() => release.resolve());
  f.manager.save = async () => {
    if (f.manager.state.receipts[value.message_id]?.response?.accepted) { entered.resolve(); await release.promise; }
    return save();
  };
  f.router.beforeAck = async () => {
    const disk = JSON.parse(await readFile(f.manager.statePath, "utf8"));
    assert.equal(disk.receipts[value.message_id].response.accepted, true);
    assert.equal(disk.sessions["thread-1"].queue[0].messageId, value.message_id);
  };
  const receiving = f.service.receive(value); assert.equal(f.service.busy(), true);
  await entered.promise; assert.equal(ackCalls(f.router).length, 0);
  const disk = JSON.parse(await readFile(f.manager.statePath, "utf8")); assert.equal(disk.receipts[value.message_id].response, undefined);
  release.resolve(); await receiving;
  assert.equal(ackCalls(f.router).length, 1); assert.equal(f.service.busy(), false);
});

test("accepted-looking results without a durable receipt, failures and unknown dispatches never ACK", async t => {
  const f = await fixture(t), value = f.router.add(message());
  f.manager.message = async request => {
    f.manager.state.receipts[request.messageId] = { request, response: { accepted: true }, nativeDispatch: { status: "accepted" } };
    return { accepted: true };
  };
  await assert.rejects(f.service.receive(value), { code: "manager_acceptance_unconfirmed" });
  assert.equal(ackCalls(f.router).length, 0);
  f.manager.message = async () => { throw Error("native acceptance unknown"); };
  await assert.rejects(f.service.receive(value), /native acceptance unknown/);
  assert.equal(f.mirrors.length, 1); assert.equal(ackCalls(f.router).length, 0);
});

test("a durable native deferral stays queued and same-ID redelivery can ACK it", async t => {
  const f = await fixture(t), value = f.router.add(message());
  f.router.beforeAck = async () => { throw Error("router unavailable"); };
  await assert.rejects(f.service.receive(value), /router unavailable/);
  f.manager.deliveryState = async () => ({ turns: [{ id: "busy-turn", status: "inProgress" }] });
  await dispatch(f);
  const receipt = f.manager.state.receipts[value.message_id];
  assert.equal(receipt.nativeDispatch.status, "deferred"); assert.equal(f.manager.state.sessions["thread-1"].queue[0].messageId, value.message_id);
  f.router.beforeAck = undefined;
  assert.equal((await f.service.receive(value)).acked, true);
  assert.equal(f.nativeCalls.length, 0); assert.equal(f.mirrors.length, 1);
});

test("unknown native write after an ACK failure is never sent or acknowledged again", async t => {
  const f = await fixture(t), value = f.router.add(message());
  f.router.beforeAck = async () => { throw Error("ACK connection failed"); };
  await assert.rejects(f.service.receive(value));
  let writes = 0; f.manager.native = async () => { writes++; throw Error("lost native response"); };
  await dispatch(f);
  assert.equal(f.manager.state.receipts[value.message_id].nativeDispatch.status, "unknown");
  const ackCount = ackCalls(f.router).length;
  f.router.beforeAck = undefined;
  await assert.rejects(f.service.receive(value)); await assert.rejects(f.service.receive(value));
  assert.equal(writes, 1); assert.equal(ackCalls(f.router).length, ackCount);
});

test("inbox is read-only and bound to the current worker; poll drains pages without tasks or automatic replies", async t => {
  const f = await fixture(t); f.router.pageSize = 1;
  for (let seq = 1; seq <= 3; seq++) f.router.add(message({ seq }));
  f.router.add(message({ to: "OtherWorker", seq: 4 }));
  const page = await f.service.inbox({ worker: "OtherWorker", limit: 1 });
  assert.equal(page.worker, worker); assert.equal(page.messages.length, 1); assert.equal(f.requests.length, 0);
  const result = await f.service.poll();
  assert.equal(result.accepted, 3); assert.deepEqual(result.failures, []); assert.equal(f.mirrors.length, 3);
  assert.equal((await f.service.poll()).accepted, 0);
  assert.ok(f.router.calls.every(call => call.method === "GET" || call.endpoint.endsWith("/ack")));
  assert.ok(f.router.calls.filter(c => c.method === "GET").every(c => new URL(c.endpoint, "http://router.test").searchParams.get("worker") === worker));
});

test("poll failures remain pending for the next poll, without blocking later messages", async t => {
  const f = await fixture(t), bad = f.router.add(message({ seq: 1 })), good = f.router.add(message({ seq: 2 }));
  const original = f.manager.message;
  f.manager.message = async request => { if (request.messageId === bad.message_id) throw Error("temporary failure"); return original(request); };
  const first = await f.service.poll();
  assert.equal(first.accepted, 1); assert.equal(first.failures[0].messageId, bad.message_id);
  assert.equal(f.router.messages.get(bad.message_id).status, "queued"); assert.equal(f.router.messages.get(good.message_id).status, "accepted");
  f.manager.message = original; assert.equal((await f.service.poll()).accepted, 1);
});

test("close interrupts a pending native acceptance without ACK, and no later continuation writes the router", async t => {
  const f = await fixture(t), started = gate(), released = gate(), value = f.router.add(message());
  f.manager.message = async () => { started.resolve(); await released.promise; return { accepted: true }; };
  const receiving = f.service.receive(value), failed = assert.rejects(receiving, { code: "worker_messaging_closed" });
  await started.promise; await f.service.close(); await failed; released.resolve();
  assert.equal(f.service.busy(), false); assert.equal(ackCalls(f.router).length, 0);
  await assert.rejects(f.service.send({ messageId: randomUUID(), to: peer, text: "closed" }), { code: "worker_messaging_closed" });
});

async function listen(t, handler) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }));
  return `http://127.0.0.1:${server.address().port}`;
}

test("real authenticated RouterClient uses the peer envelope and local sender", async t => {
  const f = await fixture(t), tokens = [];
  const url = await listen(t, async (req, res) => {
    tokens.push(req.headers["x-router-token"]);
    const chunks = []; for await (const bytes of req) chunks.push(bytes);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : undefined;
    const result = await f.router.request(req.method, req.url, body);
    res.writeHead(req.method === "POST" ? 202 : 200, { "content-type": "application/json" }); res.end(JSON.stringify(result));
  });
  await f.service.close();
  const client = new RouterClient({ url, token: "peer-test-token", name: worker });
  const service = startWorkerMessaging({ ...f.options, client }); t.after(() => service.close());
  await service.send({ messageId: randomUUID(), from: "forged", to: peer, text: "authenticated" }); await service.inbox();
  assert.deepEqual(tokens, ["peer-test-token", "peer-test-token"]); assert.equal(f.router.calls[0].body.from, worker);
});

test("image attachments download verified bytes to a real local path and never expose a sender's Windows path", async t => {
  const f = await fixture(t), gets = [];
  f.client.url = await listen(t, (req, res) => {
    gets.push(req.url); assert.equal(req.headers["x-router-token"], "test-token");
    res.writeHead(200, { "content-type": "image/png", "content-length": png.length, "x-attachment-sha256": sha }); res.end(png);
  });
  const value = f.router.add(message({ attachments: [{ ...ref, name: "C:\\remote\\image.png", path: "C:\\remote\\image.png", url: "https://untrusted.invalid/private" }] }));
  await f.service.receive(value); await f.service.receive(value);
  assert.deepEqual(gets, ["/attachments/" + sha]);
  const request = f.requests[0], local = request.attachments[0].localPath;
  assert.ok(path.isAbsolute(local)); assert.deepEqual(await readFile(local), png); assert.ok(request.text.includes(local));
  assert.ok(!request.text.includes("C:\\remote")); assert.ok(!JSON.stringify(request).includes("untrusted.invalid"));
  assert.equal(request.attachments[0].name, "image.png"); assert.equal(f.mirrors.length, 1);
});

for (const mode of ["hash", "length", "type", "redirect"]) test(`invalid image ${mode} never reaches the manager or ACK`, async t => {
  const f = await fixture(t);
  f.client.url = await listen(t, (_req, res) => {
    if (mode === "redirect") { res.writeHead(302, { location: "https://untrusted.invalid" }); res.end(); return; }
    const bytes = mode === "hash" ? Buffer.from("89504e470d0a1a0affffffff", "hex") : png;
    res.writeHead(200, { "content-type": mode === "type" ? "text/plain" : "image/png",
      "content-length": mode === "length" ? png.length + 1 : png.length, "x-attachment-sha256": sha }); res.end(bytes);
  });
  const value = f.router.add(message({ attachments: [ref] }));
  await assert.rejects(f.service.receive(value));
  assert.equal(f.requests.length, 0); assert.equal(f.mirrors.length, 0); assert.equal(ackCalls(f.router).length, 0);
});

test("image messages never download or send local paths to a remote native host", async t => {
  const f = await fixture(t); f.manager.state.sessions["thread-1"].hostId = "windows-remote";
  await assert.rejects(f.service.receive(message({ attachments: [ref] })), { code: "remote_image_target" });
  assert.equal(f.requests.length, 0); assert.equal(f.router.calls.length, 0);
});
