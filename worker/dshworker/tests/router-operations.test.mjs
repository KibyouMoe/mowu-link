import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { routerOperations } from "../router-operations.mjs";
import { routerPrompt } from "../router-tasks.mjs";

function fixture() {
  const calls = [], messages = [];
  const manager = { routerClient: { _request: async (...args) => { calls.push(args); return { ok: true, task_id: "t-project" }; } },
    workerMessages: { send: async request => { messages.push(request); return { status: "queued" }; }, inbox: async () => ({ messages: [] }) } };
  return { manager, calls, messages, ops: routerOperations(manager) };
}

test("ordinary peer chat and inbox never submit a work order", async () => {
  const f = fixture(), request = { messageId: randomUUID(), to: "Rudolf", text: "聊一下当前状态" };
  assert.equal((await f.ops.message("send", request)).status, "queued");
  assert.deepEqual(f.messages, [request]); assert.deepEqual(await f.ops.message("inbox"), { messages: [] });
  assert.equal(f.calls.length, 0);
});

test("formal work order carries its stable request UUID, native goal mode and no default deadline", async () => {
  const f = fixture(), messageId = randomUUID();
  const request = { messageId, worker: "Rudolf", goal: "验证插件生命周期", context: "既有项目的交付项" };
  assert.equal((await f.ops.createWorkOrder(request)).task_id, "t-project");
  await f.ops.createWorkOrder(request);
  assert.deepEqual(f.calls[0], f.calls[1]);
  const [method, route, body] = f.calls[0];
  assert.equal(method, "POST"); assert.equal(route, "/tasks");
  assert.equal(body.message_id, messageId); assert.equal(body.execution_mode, "goal");
  assert.equal(body.queue_if_offline, true); assert.equal(body.deadline_s, undefined);
  assert.equal(body.context, request.context);
});

test("only an explicit positive deadline is sent and invalid work orders perform no I/O", async () => {
  const f = fixture(), request = { messageId: randomUUID(), worker: "Rudolf", goal: "验收", deadlineS: 60, queueIfOffline: false };
  await f.ops.createWorkOrder(request); assert.equal(f.calls[0][2].deadline_s, 60); assert.equal(f.calls[0][2].queue_if_offline, false);
  for (const change of [{ messageId: "bad" }, { goal: " " }, { worker: " Rudolf " }, { deadlineS: 0 }, { deadlineS: NaN }, { queueIfOffline: "yes" }])
    assert.throws(() => f.ops.createWorkOrder({ ...request, ...change }), error => error.body.error === "invalid_work_order");
  assert.equal(f.calls.length, 1);
});

test("uncertain work-order submission retains the same message UUID in the error", async () => {
  const f = fixture(), messageId = randomUUID();
  f.manager.routerClient._request = async () => { throw new Error("connection reset"); };
  await assert.rejects(f.ops.createWorkOrder({ messageId, worker: "Rudolf", goal: "验收" }), error =>
    error.status === 503 && error.body.messageId === messageId && error.body.error === "router_request_unconfirmed");
});

test("worker list/status are exact read-only routes; route injection and absent owner fail closed", async () => {
  const f = fixture(); await f.ops.workers(); await f.ops.workOrder("t-project");
  assert.deepEqual(f.calls.map(c => c.slice(0, 2)), [["GET", "/workers"], ["GET", "/tasks/t-project"]]);
  assert.throws(() => f.ops.workOrder("t-a/../../ops"), error => error.status === 400);
  await assert.rejects(routerOperations({}).workers(), error => error.body.error === "router_unavailable");
  await assert.rejects(routerOperations({}).message("send", {}), error => error.body.error === "worker_chat_unavailable");
});

test("formal task prompt requires real goal tools without overwriting an active project", () => {
  const prompt = routerPrompt({ taskId: "t-project", goal: "实现并验收", context: "同一张工单的补充" });
  assert.ok(prompt.startsWith("[router t-project]"));
  for (const text of ["正式工单", "get_goal", "create_goal", "已有目标", "不能擅自替换", "以原生工具实际返回", "不自设 token 预算", "同一张工单的补充"]) assert.ok(prompt.includes(text), text);
});
