import assert from "node:assert/strict";
import test from "node:test";
import http from "node:http";
import { installTaskControl } from "../interface/task-control.mjs";

function fixture() {
  const turn = { id: "turn-old", prompt: "[router t-old]\nhello", status: "inProgress" };
  const newer = { id: "turn-new", prompt: "[router t-new]\nhello", status: "inProgress" };
  const thread = { id: "thread", queue: [newer.id], turns: [turn, newer] };
  const logs = [];
  let cancels = 0;
  const transport = { threads: new Map([["thread", thread]]),
    live: new Map([["thread", { agent: { cancel: () => { cancels++; } } }]]),
    save: async () => {}, approvals: { setRouter() {} },
    log: (...args) => logs.push(args) };
  const control = installTaskControl(transport);
  return { control, transport, thread, turn, newer, logs, cancels: () => cancels };
}
test("task cancellation targets the exact running task and invokes agent.cancel", async () => {
  const f = fixture();
  const result = await f.control.cancelTask("t-old");
  assert.equal(result.cancelled, true); assert.equal(f.cancels(), 1);
  assert.equal(f.turn.cancelRequested, true); assert.equal(f.newer.cancelRequested, undefined);
});
test("queued cancellation removes only that turn and never cancels its parent agent", async () => {
  const f = fixture();
  await f.control.cancelTask("t-new");
  assert.equal(f.cancels(), 0); assert.equal(f.newer.status, "interrupted");
  assert.deepEqual(f.thread.queue, []); assert.equal(f.turn.status, "inProgress");
});
test("late cancellation of a terminal task never cancels the next running task", async () => {
  const f = fixture(); f.turn.status = "completed"; f.thread.queue = [];
  assert.equal((await f.control.cancelTask("t-old")).reason, "already_terminal");
  assert.equal(f.cancels(), 0);
});
test("cancellation received before a turn exists records a tombstone", async () => {
  const f = fixture();
  assert.equal((await f.control.cancelTask("t-not-started")).reason, "task_not_started");
  assert.equal(f.control.isCancelled("t-not-started"), true);
});
test("worker registration advertises the control origin on reconnects", async () => {
  const f = fixture(), calls = [];
  const router = { _request: (...args) => calls.push(args) };
  f.control.bindRouter(router, "http://127.0.0.1:12345");
  router._request("POST", "/register", { name: "TamamoCross" });
  router._request("GET", "/approvals/ap_test", null);
  assert.deepEqual(calls[0][2], { name: "TamamoCross", control_url: "http://127.0.0.1:12345" });
  assert.equal(calls[1][2], null);
});
test("authenticated manager cancellation endpoint reaches only its named task", async t => {
  const f = fixture();
  const manager = { config: { token: "test-worker-token" },
    handle(req, res) { res.writeHead(404); res.end(); } };
  f.control.attachManager(manager);
  const server = http.createServer((req, res) => { void manager.handle(req, res); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const base = "http://127.0.0.1:" + server.address().port;
  let r = await fetch(base + "/manager/tasks/t-old/cancel", { method: "POST", body: "{}" });
  assert.equal(r.status, 401); assert.equal(f.cancels(), 0);
  const headers = { authorization: "Bearer test-worker-token" };
  r = await fetch(base + "/manager/tasks/t-old/cancel", { method: "POST", headers,
    body: JSON.stringify({ task_id: "t-other" }) });
  assert.equal(r.status, 400); assert.equal(f.cancels(), 0);
  r = await fetch(base + "/manager/tasks/t-old/cancel", { method: "POST", headers,
    body: JSON.stringify({ task_id: "t-old" }) });
  assert.equal(r.status, 200); assert.equal((await r.json()).task_id, "t-old");
  assert.equal(f.cancels(), 1);
  r = await fetch(base + "/manager/tasks/t-new", { headers });
  assert.equal(r.status, 200); assert.equal((await r.json()).turn_id, "turn-new");
});
test("worker cancellation errors become explicit failed HTTP receipts", async t => {
  const f = fixture();
  f.transport.live.get("thread").agent.cancel = () => { throw Error("cancel transport unavailable"); };
  const manager = { config: { token: "token" }, handle() {} };
  f.control.attachManager(manager);
  const server = http.createServer((req, res) => { void manager.handle(req, res); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const r = await fetch("http://127.0.0.1:" + server.address().port + "/manager/tasks/t-old/cancel",
    { method: "POST", headers: { authorization: "Bearer token" }, body: "{}" });
  assert.equal(r.status, 500); assert.equal((await r.json()).error, "worker_control_failed");
  assert.ok(f.logs.some(([, event]) => event === "router_task_cancel_failed"));
});
