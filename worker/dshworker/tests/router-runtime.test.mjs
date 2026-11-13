import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { startRouterRuntime } from "../router-runtime.mjs";
import { routerMessageId, subscriptionMessageId } from "../router-tasks.mjs";
import { getActiveRouterClient, setActiveRouterClient } from "../router-client.mjs";

async function until(predicate, label = "condition", timeout = 3000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (predicate()) return; await delay(10); }
  assert.fail(`timed out: ${label}`);
}

// Real loopback HTTP/SSE, no production credentials, router, Feishu or native
// processes. The fixture implements the worker-facing router wire contract.
async function fixture(t) {
  const state = { calls: [], streams: [], pending: [], subscriptions: [], results: [], approvalStatus: "approved", failResult: false };
  const server = http.createServer(async (req, res) => {
    let raw = ""; for await (const chunk of req) raw += chunk;
    const url = new URL(req.url, "http://localhost");
    const body = raw ? JSON.parse(raw) : null;
    state.calls.push({ method: req.method, path: url.pathname, body, token: req.headers["x-router-token"] });
    const json = (value, status = 200) => { res.writeHead(status, { "content-type": "application/json" }); res.end(JSON.stringify(value)); };
    if (url.pathname === "/stream") {
      res.writeHead(200, { "content-type": "text/event-stream" }); res.write(": ready\n\n");
      state.streams.push(res); return;
    }
    if (url.pathname === "/result") {
      if (state.failResult) { state.failResult = false; return json({ error: "temporary" }, 503); }
      state.results.push(body); return json({ state: body.status });
    }
    if (url.pathname === "/messages") return json({ messages: [], has_more: false, next_after: 0 });
    if (url.pathname === "/subscriptions/pending") return json({ events: state.pending.splice(0) });
    if (url.pathname === "/subscriptions") {
      if (req.method === "POST") { const sub = { id: state.subscriptions.length + 1, ...body }; state.subscriptions.push(sub); return json(sub); }
      return json({ subscriptions: state.subscriptions });
    }
    if (url.pathname === "/approvals") return json({ approval_id: "a-1", status: "pending" });
    if (url.pathname === "/approvals/a-1") return json({ approval: { status: state.approvalStatus } });
    if (url.pathname.startsWith("/tasks/")) return json({ task: { state: "running" } });
    json({ ok: true, new: true, queued: 0 });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  state.url = `http://127.0.0.1:${server.address().port}`;
  state.push = task => state.streams.at(-1).write(`event: task\ndata: ${JSON.stringify(task)}\n\n`);
  t.after(async () => {
    for (const stream of state.streams) stream.destroy();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  });
  return state;
}

function managerMock(router) {
  const manager = {
    server: { listening: true }, config: { token: "isolated-test-token", machine: "test-worker", bridgeName: "test",
      router: { url: router.url }, subscriptionPollMs: 250, feishu: { trainerOpenIdWhitelist: ["test-trainer"] } },
    state: { selectedThreadId: "thread-1", sessions: { "thread-1": { cwd: "/test", hostId: "local" }, "thread-2": { cwd: "/other" } },
      receipts: {}, publicQuestions: {} },
    questions: { state: { entries: {} } }, turns: [], writes: [], mirrors: [], boards: [], reads: [], archive: new Map(),
    transcriptEnabled: false, status: "completed",
    async mirrorInput(request) { this.mirrors.push(request); },
    async message(request) {
      const existing = this.state.receipts[request.messageId] ?? this.archive.get(request.messageId);
      if (existing) { assert.deepEqual(request, existing.request); return { duplicate: true }; }
      this.writes.push(request);
      const id = `turn-${this.writes.length}`;
      this.state.receipts[request.messageId] = { request: structuredClone(request), nativeTurnId: id,
        nativeTargetThreadId: request.targetThreadId, nativeDispatch: { status: "accepted" } };
      this.turns.push({ id, status: this.status, items: [{ type: "userMessage", text: request.text },
        { type: "agentMessage", phase: "final_answer", text: "finished" }] });
      return { accepted: true };
    },
    async read(threadId, hostId, options) {
      this.reads.push({ threadId, hostId, options });
      if (this.hangRead) return new Promise(() => {});
      return { thread: { id: threadId }, turns: this.turns };
    },
    async captureTaskQuestions() {},
    async publicReply(request, text, phase) { this.boards.push({ request, text, phase }); },
  };
  manager.notifier = { enabled: true, async sendMessage(text, options) { manager.boards.push({ text, options }); return { data: { message_id: "board" } }; } };
  manager.receiptArchive = { get: async id => manager.archive.get(id) };
  return manager;
}

function isolateEnv(t) {
  const keys = ["CODEXWORKER_ROUTER", "CODEXWORKER_ROUTER_URL", "CODEXWORKER_ROUTER_TOKEN_FILE", "CODEXWORKER_FEISHU_DRY_RUN"];
  const saved = keys.map(key => [key, process.env[key]]);
  for (const key of keys) delete process.env[key];
  t.after(() => { for (const [key, value] of saved) value === undefined ? delete process.env[key] : process.env[key] = value; });
}
async function setup(t, configure = () => {}) {
  isolateEnv(t);
  const router = await fixture(t), manager = managerMock(router), logs = [];
  manager.stateDir = await mkdtemp(path.join(os.tmpdir(), "router-runtime-peer-"));
  configure(manager, router);
  const transport = { handler: null, setApprovalHandler(handler) { this.handler = handler; } };
  const options = { manager, transport, identity: { name: "test-worker", display: "Test Worker" }, version: "test",
    nativeCall: () => assert.fail("runtime must use manager.read, never raw native wait_threads"),
    log: (level, event, fields) => logs.push({ level, event, fields }) };
  const runtime = await startRouterRuntime(options);
  t.after(async () => { await runtime.close(); await rm(manager.stateDir, { recursive: true, force: true }); });
  await until(() => runtime.client.connected, "SSE connected");
  return { router, manager, transport, options, runtime, logs };
}

test("SSE task uses selected conversation, provenance, board, receipts and terminal result", async t => {
  const { router, manager, runtime } = await setup(t);
  router.push({ task_id: "t-success", goal: "review", context: { detail: "context" }, workspace: "/ignored" });
  await until(() => router.results.length === 1);
  assert.equal(router.results[0].status, "done");
  assert.equal(router.results[0].summary, "finished");
  assert.deepEqual(router.results[0].evidence, [{ type: "codex_thread", thread_id: "thread-1", turn_id: "turn-1" }]);
  assert.equal(manager.writes[0].messageId, routerMessageId("t-success"));
  assert.equal(manager.writes[0].origin, "router");
  assert.equal(manager.writes[0].source, "hermes");
  assert.equal(manager.writes[0].delivery, "steer");
  assert.match(manager.writes[0].text, /来自超级溪流/);
  assert.match(manager.writes[0].text, /router context/);
  assert.equal(manager.boards.length, 1, "no duplicate success board");
  assert.equal(manager.boards[0].text, "review");
  assert.equal(manager.reads[0].options.turnLimit, 30);
  await until(() => !runtime.busy());
  assert.equal(manager.routerClient, runtime.client);
});

test("disabled, proxy and non-listening managers never register or attach approvals", async t => {
  isolateEnv(t);
  let installs = 0;
  const router = await fixture(t);
  for (const mode of ["disabled", "environment", "proxy", "not-listening"]) {
    const manager = managerMock(router);
    if (mode === "disabled") manager.config.router.enabled = false;
    if (mode === "environment") process.env.CODEXWORKER_ROUTER = "0"; else delete process.env.CODEXWORKER_ROUTER;
    if (mode === "proxy") manager.proxy = true;
    if (mode === "not-listening") manager.server.listening = false;
    const runtime = await startRouterRuntime({ manager, transport: { setApprovalHandler() { installs++; } } });
    assert.equal(runtime.client, null); assert.equal(runtime.busy(), false); await runtime.close();
  }
  assert.equal(installs, 0); assert.equal(router.calls.length, 0);
});

test("repeated startup shares one router, tokenFile overrides manager token", async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "router-runtime-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const tokenFile = path.join(dir, "token"); await writeFile(tokenFile, "file-token\n");
  const { router, options, runtime } = await setup(t, manager => { manager.config.router.tokenFile = tokenFile; });
  const second = await startRouterRuntime(options);
  assert.equal(second, runtime);
  assert.equal(router.calls.find(call => call.path === "/stream").token, "file-token");
  assert.equal(router.streams.length, 1);
});

test("result retry after runtime restart uses archived receipt and original target, never a second native write", async t => {
  const { router, manager, runtime, options, logs } = await setup(t, (_manager, router) => { router.failResult = true; });
  const task = { task_id: "t-retry", goal: "once" };
  router.push(task);
  await until(() => logs.some(row => row.event === "router_handler_failed"));
  await runtime.close();
  const id = routerMessageId(task.task_id);
  manager.archive.set(id, manager.state.receipts[id]); delete manager.state.receipts[id];
  manager.state.selectedThreadId = "thread-2";
  const next = await startRouterRuntime(options); t.after(() => next.close());
  await until(() => next.client.connected);
  router.push(task);
  await until(() => router.results.length === 1);
  assert.equal(manager.writes.length, 1);
  assert.equal(manager.mirrors.length, 1);
  assert.equal(manager.boards.length, 1);
  assert.equal(router.results[0].evidence[0].thread_id, "thread-1");
});

test("subscriptions declare, poll, serialize and use stable UUIDs without task results", async t => {
  const { router, manager, runtime, logs } = await setup(t, manager => {
    manager.config.subscriptions = [{ source: "build", kinds: ["ready"] }]; manager.status = "inProgress";
  });
  await until(() => router.subscriptions.length === 1);
  const event = { source: "build", kind: "ready", id: 7, text: "build available" };
  router.pending.push(event, event);
  await until(() => manager.writes.length === 1);
  assert.equal(runtime.busy(), true);
  manager.turns[0].status = "completed";
  await until(() => logs.filter(row => row.event === "subscription_event_finished").length === 2);
  assert.equal(manager.writes.length, 1);
  assert.equal(manager.writes[0].messageId, subscriptionMessageId(event));
  assert.equal(manager.writes[0].origin, "subscription");
  assert.match(manager.boards[0].text, /订阅事件 · build/);
  assert.equal(manager.boards.length, 1);
  assert.equal(router.results.length, 0);
  await until(() => !runtime.busy());
});

for (const mode of ["native read", "clarification", "approval"]) test(`close interrupts ${mode}, stops SSE, returns promptly and posts no false failure`, async t => {
  const { router, manager, runtime, transport } = await setup(t, (manager, router) => {
    if (mode === "native read") manager.hangRead = true;
    if (mode === "clarification") {
      manager.state.publicQuestions.q = { questionId: "q", threadId: "thread-1", recipient: { kind: "hermes", taskId: "t-close" } };
      manager.questions.state.entries.q = { status: "pending" };
    }
    router.approvalStatus = "pending";
  });
  let answer;
  if (mode === "approval") {
    answer = transport.handler({ method: "execute", kind: "command", title: "approve" });
    await until(() => router.calls.some(call => call.path === "/approvals/a-1"));
  } else {
    router.push({ task_id: "t-close", goal: "wait" });
    await until(() => mode === "native read" ? manager.reads.length : router.calls.some(call => call.path === "/tasks/t-close"));
  }
  assert.equal(runtime.busy(), true);
  const started = Date.now(); await runtime.close();
  assert.ok(Date.now() - started < 500, "shutdown must not wait for native/user completion");
  if (answer) assert.equal((await answer).decision, "denied");
  assert.equal(transport.handler, null);
  await until(() => !runtime.busy());
  await until(() => router.streams[0].destroyed);
  assert.equal(runtime.client.running, false);
  assert.equal(manager.routerClient, null);
  assert.equal(router.results.length, 0);
  assert.ok(manager.boards.every(row => !row.text.startsWith("失败")));
});

test("approval channel preserves grant and denial decisions", async t => {
  const { router, transport, runtime } = await setup(t);
  assert.equal((await transport.handler({ kind: "command", title: "grant" })).decision, "approved");
  router.approvalStatus = "denied";
  assert.equal((await transport.handler({ kind: "command", title: "deny" })).decision, "denied");
  assert.equal(runtime.busy(), false);
});

test("explicit deadline produces a failed result and one failure board, without canceling native task", async t => {
  const { router, manager } = await setup(t, manager => { manager.status = "inProgress"; });
  router.push({ task_id: "t-deadline", goal: "slow", deadline_s: 0.03 });
  await until(() => router.results.length === 1);
  assert.equal(router.results[0].status, "failed");
  assert.match(router.results[0].summary, /deadline_s/);
  assert.equal(manager.boards.length, 2);
  assert.equal(manager.turns[0].status, "inProgress");
});

test("no-progress timeout does not treat unchanged reads as progress", async t => {
  const { router } = await setup(t, manager => { manager.status = "inProgress"; manager.config.noProgressTimeoutMs = 30; });
  router.push({ task_id: "t-stuck", goal: "slow" });
  await until(() => router.results.length === 1);
  assert.match(router.results[0].summary, /无进展超时/);
});

test("old owner cleanup does not clear a newer manager/global router reference", async t => {
  const { manager, runtime } = await setup(t);
  const replacement = {};
  manager.routerClient = replacement; setActiveRouterClient(replacement);
  await runtime.close();
  assert.equal(manager.routerClient, replacement);
  assert.equal(getActiveRouterClient(), replacement);
  setActiveRouterClient(null);
});


test("missing router credentials keep the Feishu writer usable and permit a later retry", async t => {
  isolateEnv(t);
  const router = await fixture(t), manager = managerMock(router), logs = [];
  manager.stateDir = await mkdtemp(path.join(os.tmpdir(), "router-missing-credentials-"));
  t.after(() => rm(manager.stateDir, { recursive: true, force: true }));
  const tokenFile = path.join(manager.stateDir, "missing-router.token");
  manager.config.router.tokenFile = tokenFile;
  const options = { manager, log: (level, event, fields) => logs.push({ level, event, fields }) };
  const disabled = await startRouterRuntime(options);
  assert.equal(disabled.client, null); assert.equal(disabled.busy(), false); await disabled.close();
  assert.equal(manager.server.listening, true); assert.equal(manager.notifier.enabled, true);
  await manager.notifier.sendMessage("Feishu remains available", {});
  assert.equal(manager.boards.at(-1).text, "Feishu remains available");
  assert.equal(router.calls.length, 0);
  assert.ok(logs.some(entry => entry.event === "router_credentials_unavailable" && entry.fields.code === "ENOENT"));
  assert.ok(!JSON.stringify(logs).includes(tokenFile));
  await writeFile(tokenFile, "isolated-test-router-token");
  const resumed = await startRouterRuntime(options);
  t.after(() => resumed.close());
  await until(() => resumed.client.connected, "restored router credential accepted");
  assert.equal(manager.routerClient, resumed.client);
});
