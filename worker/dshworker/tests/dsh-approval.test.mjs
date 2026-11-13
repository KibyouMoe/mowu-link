import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createDshApprovalBridge, APPROVAL_TIMEOUT_MS, APPROVAL_POLL_MS } from "../interface/approval-bridge.mjs";
import { InProcessTransport } from "../interface/inprocess.mjs";

const request = (signal) => ({ agent: { session: { id: "session-test" } }, signal,
  toolName: "bash", callId: "call-test", id: "native-asked-id",
  reason: "escalate sandbox to danger-full-access" });
const options = (extra = {}) => ({ timeoutMs: 1000, pollMs: 1, log: () => {}, ...extra });
test("DSH approvals use the proven nine-minute and 1.5-second defaults", () => {
  assert.equal(APPROVAL_TIMEOUT_MS, 540000); assert.equal(APPROVAL_POLL_MS, 1500);
});
test("DSH approval posts kind=approval with native, task, session and tool details", async () => {
  let submitted;
  const progress = [], logs = [];
  const bridge = createDshApprovalBridge(options({ router: {
    requestApproval: async (body) => { submitted = body; return { approval_id: "ap_test", status: "pending" }; },
    getApproval: async () => ({ status: "approved" }),
  }, log: (level, event) => logs.push(event) }));
  const result = await bridge.request(request(), { taskId: "t-test", threadId: "thread", turnId: "turn",
    progress: (p) => progress.push(p) });
  assert.equal(result, "allowed-once");
  assert.equal(submitted.kind, "approval"); assert.equal(submitted.taskId, "t-test");
  const detail = JSON.parse(submitted.detail);
  assert.equal(detail.toolName, "bash"); assert.equal(detail.session_id, "session-test");
  assert.equal(detail.native_approval_id, "native-asked-id"); assert.equal(detail.call_id, "call-test");
  assert.ok(progress.some(p => p.text === "等待审批 ap_test（escalate sandbox to danger-full-access）"));
  assert.equal(progress.at(-1).state, "approved");
  assert.deepEqual(logs, ["approval_request", "approval_resolved"]);
});
for (const status of ["denied", "expired", "invalid"]) {
  test("DSH approval fails closed for router status " + status, async () => {
    const bridge = createDshApprovalBridge(options({ router: {
      requestApproval: async () => ({ approval_id: "ap_test", status }),
      getApproval: async () => ({ status: "approved" }),
    } }));
    assert.equal(await bridge.request(request()), "rejected");
  });
}
test("DSH approval accepts an immediate router grant without polling", async () => {
  const bridge = createDshApprovalBridge(options({ router: {
    requestApproval: async () => ({ approval_id: "ap_test", status: "approved" }),
    getApproval: async () => { throw Error("must not poll"); },
  } }));
  assert.equal(await bridge.request(request()), "allowed-once");
});
for (const where of ["missing", "submit", "poll", "no-id"]) {
  test("DSH approval fails closed when channel is " + where, async () => {
    const router = where === "missing" ? null : {
      requestApproval: async () => { if (where === "submit") throw Error("offline");
        return where === "no-id" ? {} : { approval_id: "ap_test", status: "pending" }; },
      getApproval: async () => { throw Error("offline"); },
    };
    assert.equal(await createDshApprovalBridge(options({ router })).request(request()), "rejected");
  });
}
for (const where of ["submit", "poll"]) {
  test("DSH approval bounds a never-returning " + where, async () => {
    const progress = [];
    const bridge = createDshApprovalBridge(options({ timeoutMs: 15, router: {
      requestApproval: () => where === "submit" ? new Promise(() => {}) : Promise.resolve({ approval_id: "ap_test", status: "pending" }),
      getApproval: () => new Promise(() => {}),
    } }));
    assert.equal(await bridge.request(request(), { progress: p => progress.push(p) }), "rejected");
    assert.equal(progress.at(-1).state, "timeout");
  });
}
test("DSH turn cancellation rejects an approval immediately and ignores later grants", async () => {
  const abort = new AbortController(), progress = [];
  let grant;
  const bridge = createDshApprovalBridge(options({ router: {
    requestApproval: () => new Promise(resolve => { grant = resolve; }),
    getApproval: async () => ({ status: "approved" }),
  } }));
  const result = bridge.request(request(abort.signal), { progress: p => progress.push(p) });
  abort.abort(new Error("task cancelled"));
  assert.equal(await result, "rejected");
  grant({ approval_id: "ap_late", status: "approved" });
  assert.equal(progress.at(-1).state, "cancelled");
});
test("DSH approval ownership leaves other web sessions to their existing answerer", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "dsh-approval-"));
  try {
    let handler;
    const ctx = { on(name, callback, opts) { if (name !== "approval/request") return () => {};
      assert.equal(name, "approval/request");
      assert.equal(opts.prepend, true); handler = callback; return () => {}; } };
    const transport = new InProcessTransport({ ctx, selection: {}, cwd: dir, stateDir: dir,
      turnTimeoutMs: 1000, log: () => {} });
    let next = 0;
    assert.equal(await handler(request(), () => { next++; return "other"; }), "other");
    assert.equal(next, 1);
    const req = request();
    req.agent.session = { id: "session-test", seq: 1, eventAt: () => ({ type: "approval/asked", data: { id: "native" } }) };
    const turn = { id: "turn", status: "inProgress", prompt: "[router t-owned]\nhello" };
    transport.live.set("thread", { agent: req.agent });
    transport.threads.set("thread", { id: "thread", turns: [turn] });
    transport.save = async () => {};
    transport.approvals.setRouter({
      requestApproval: async () => ({ approval_id: "ap_owned", status: "approved" }),
      getApproval: async () => { throw Error("must not poll"); },
    });
    assert.equal(await handler(req, () => { throw Error("owned request leaked"); }), "allowed-once");
    assert.equal(turn.approval.task_id, "t-owned"); assert.equal(turn.approval_id, "ap_owned");
    assert.equal(turn.waiting_for_approval, false);
    assert.equal(turn.progress.length, 1);
    transport.approvals.close();
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("waiting approval is visible in read_thread, wait snapshots and terminal results", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "dsh-progress-"));
  try {
    let handler, resolveGrant;
    const ctx = { on(name, callback) { handler = callback; return () => {}; } };
    const transport = new InProcessTransport({ ctx, selection: {}, cwd: dir, stateDir: dir,
      turnTimeoutMs: 1000, log: () => {} });
    transport.save = async () => {};
    transport.approvals = createDshApprovalBridge(options({ router: {
      requestApproval: async () => ({ approval_id: "ap_visible", status: "pending" }),
      getApproval: () => new Promise(resolve => { resolveGrant = resolve; }),
    } }));
    const req = request();
    req.agent.session = { id: "session", seq: 1, eventAt: () => ({ type: "approval/asked", data: { id: "native" } }) };
    const turn = { id: "turn", prompt: "[router t-visible]\nchange config", status: "inProgress", startedAt: new Date().toISOString() };
    transport.threads.set("thread", { id: "thread", cwd: dir, turns: [turn], queue: [], status: "inProgress" });
    transport.live.set("thread", { agent: req.agent });
    const result = handler(req, () => { throw Error("unowned"); });
    while (!resolveGrant) await new Promise(resolve => setTimeout(resolve, 1));
    const snapshot = transport.readThread({ threadId: "thread", includeOutputs: false });
    assert.equal(snapshot.turns[0].approval_id, "ap_visible");
    assert.equal(snapshot.turns[0].waiting_for_approval, true);
    const item = snapshot.turns[0].items.find(i => i.phase === "commentary");
    assert.match(item.text, /^等待审批 ap_visible/);
    assert.equal(transport.pollThread("thread").latestTurn.approval_id, "ap_visible");
    resolveGrant({ status: "approved" });
    assert.equal(await result, "allowed-once");
    turn.status = "completed"; turn.text = "done";
    const finished = transport.readThread({ threadId: "thread" }).turns[0];
    assert.equal(finished.waiting_for_approval, false); assert.equal(finished.approval.state, "approved");
    assert.equal(finished.items.filter(i => i.phase === "final_answer").length, 1);
    assert.equal(finished.items.find(i => i.phase === "commentary").id, item.id);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("plugin close rejects a pending approval without waiting for its poll deadline", async () => {
  const bridge = createDshApprovalBridge(options({ router: {
    requestApproval: () => new Promise(() => {}),
    getApproval: () => new Promise(() => {}),
  } }));
  const result = bridge.request(request());
  bridge.close();
  assert.equal(await result, "rejected");
});
test("a broken logging sink cannot prevent fail-closed approval resolution", async () => {
  const bridge = createDshApprovalBridge(options({ log: () => { throw Error("logger closed"); } }));
  assert.equal(await bridge.request(request()), "rejected");
});
test("cancel before dispatch prevents the actual transport from starting a turn", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "dsh-cancel-before-"));
  try {
    const ctx = { on() { return () => {}; } };
    const transport = new InProcessTransport({ ctx, selection: {}, cwd: dir, stateDir: dir,
      turnTimeoutMs: 1000, log: () => {} });
    transport.save = async () => {};
    const created = transport.createThread({ cwd: dir });
    let started = false;
    transport.startTurn = () => { started = true; };
    await transport.control.cancelTask("t-before");
    const sent = transport.sendMessage({ threadId: created.threadId, prompt: "[router t-before]\nwork" });
    assert.equal(started, false); assert.equal(sent.status, "interrupted");
    transport.approvals.close();
  } finally { await rm(dir, { recursive: true, force: true }); }
});
