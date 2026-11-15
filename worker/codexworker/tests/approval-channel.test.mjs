// 审批通道：router 审批单 + 轮询 + 统一口径。
//
// 不起真 router、不碰网络：router client 用脚本化的假实现，sleep/now 注入，
// 覆盖 approved / denied / expired / 超时 / 提交失败 / 记录消失。
import assert from "node:assert/strict";
import test from "node:test";
import { createApprovalChannel, approvalTimeoutMs, approvalPollMs } from "../approval-channel.mjs";

function scriptedRouter({ submit = null, statuses = [], failSubmit = null, failPoll = null } = {}) {
  const calls = { submitted: [], polls: [], decided: [] };
  let index = 0;
  return {
    calls,
    async requestApproval(request) {
      calls.submitted.push(request);
      if (failSubmit) throw failSubmit;
      return submit ?? { ok: true, approval_id: "ap_test0001", status: "pending" };
    },
    async getApproval(approvalId) {
      calls.polls.push(approvalId);
      if (failPoll) throw failPoll;
      const status = statuses[Math.min(index, statuses.length - 1)] ?? "pending";
      index += 1;
      return {
        approval_id: approvalId,
        status,
        note: status === "denied" ? "训练员说不行" : null,
      };
    },
  };
}

const silent = () => {};
const request = {
  method: "item/commandExecution/requestApproval",
  kind: "command",
  title: "执行命令：rm -rf ./tmp/build",
  detail: "命令: rm -rf ./tmp/build\ncwd: /tmp",
};

test("approved: submits one router approval and returns approved", async () => {
  const router = scriptedRouter({ statuses: ["pending", "pending", "approved"] });
  let clock = 0;
  const channel = createApprovalChannel({
    router, log: silent, taskId: () => "t-42",
    timeoutMs: 60_000, pollMs: 1_000,
    now: () => clock, sleepImpl: async (ms) => { clock += ms; },
  });
  const outcome = await channel.handler(request);
  assert.equal(outcome.decision, "approved");
  assert.equal(outcome.approvalId, "ap_test0001");
  assert.equal(router.calls.submitted.length, 1);
  const submitted = router.calls.submitted[0];
  assert.equal(submitted.taskId, "t-42");
  assert.equal(submitted.kind, "command");
  assert.equal(submitted.title, request.title);
  assert.equal(submitted.detail, request.detail);
  assert.ok(submitted.ttlMs >= 59_000 && submitted.ttlMs <= 60_000);
  assert.equal(router.calls.polls.length, 3);
  assert.deepEqual(channel.stats(), {
    requested: 1, approved: 1, denied: 0, expired: 0, failed: 0, timedOut: 0, polls: 3,
  });
  assert.equal(channel.active(), 0);
});

test("denied: returns denied with the human's note", async () => {
  const router = scriptedRouter({ statuses: ["pending", "denied"] });
  const channel = createApprovalChannel({
    router, log: silent, timeoutMs: 30_000, pollMs: 500,
    now: () => 0, sleepImpl: async () => {},
  });
  const outcome = await channel.handler(request);
  assert.equal(outcome.decision, "denied");
  assert.equal(outcome.note, "训练员说不行");
  assert.equal(channel.stats().denied, 1);
});

test("expired: returns denied and marks expired", async () => {
  const router = scriptedRouter({ statuses: ["expired"] });
  const channel = createApprovalChannel({
    router, log: silent, timeoutMs: 30_000, pollMs: 500,
    now: () => 0, sleepImpl: async () => {},
  });
  const outcome = await channel.handler(request);
  assert.equal(outcome.decision, "denied");
  assert.equal(outcome.expired, true);
  assert.equal(channel.stats().expired, 1);
});

test("submit failure: explicit deny, never throws", async () => {
  const router = scriptedRouter({ failSubmit: new Error("ECONNREFUSED") });
  const channel = createApprovalChannel({ router, log: silent, timeoutMs: 10_000 });
  const outcome = await channel.handler(request);
  assert.equal(outcome.decision, "denied");
  assert.match(outcome.reason, /ECONNREFUSED/);
  assert.equal(channel.stats().failed, 1);
});

test("approval record disappears (404): deny immediately instead of polling forever", async () => {
  const error = new Error("router GET /approvals/x -> 404 unknown_approval");
  error.status = 404;
  const router = scriptedRouter({ failPoll: error });
  const channel = createApprovalChannel({ router, log: silent, timeoutMs: 60_000, pollMs: 1_000 });
  const outcome = await channel.handler(request);
  assert.equal(outcome.decision, "denied");
  assert.match(outcome.reason, /disappeared/);
  assert.equal(router.calls.polls.length, 1);
  assert.equal(channel.stats().failed, 1);
});

test("timeout: no human answer -> explicit deny after the budget", async () => {
  const router = scriptedRouter({ statuses: ["pending"] });
  let clock = 0;
  const channel = createApprovalChannel({
    router, log: silent, timeoutMs: 5_000, pollMs: 1_000,
    now: () => clock, sleepImpl: async (ms) => { clock += ms; },
  });
  const outcome = await channel.handler(request);
  assert.equal(outcome.decision, "denied");
  assert.match(outcome.reason, /timed out/);
  assert.equal(channel.stats().timedOut, 1);
  assert.ok(router.calls.polls.length >= 5);
});

test("transient poll errors keep polling; recovery before deadline still approves", async () => {
  const router = scriptedRouter({ statuses: ["pending"] });
  let clock = 0;
  let failures = 2;
  const flaky = {
    requestApproval: router.requestApproval,
    async getApproval(id) {
      if (failures > 0) {
        failures -= 1;
        throw new Error("socket hang up");
      }
      router.calls.polls.push(id);
      return { approval_id: id, status: "approved", note: null };
    },
  };
  const channel = createApprovalChannel({
    router: flaky, log: silent, timeoutMs: 30_000, pollMs: 1_000,
    now: () => clock, sleepImpl: async (ms) => { clock += ms; },
  });
  const outcome = await channel.handler(request);
  assert.equal(outcome.decision, "approved");
  assert.equal(channel.stats().failed, 0);
});

test("env knobs: defaults and clamping", () => {
  const previous = { timeout: process.env.CODEXWORKER_APPROVAL_TIMEOUT_MS, poll: process.env.CODEXWORKER_APPROVAL_POLL_MS };
  try {
    delete process.env.CODEXWORKER_APPROVAL_TIMEOUT_MS;
    delete process.env.CODEXWORKER_APPROVAL_POLL_MS;
    assert.equal(approvalTimeoutMs(), 540_000);
    assert.equal(approvalPollMs(), 1_500);
    process.env.CODEXWORKER_APPROVAL_TIMEOUT_MS = "1000";
    assert.equal(approvalTimeoutMs(), 5_000, "timeout is clamped up to 5s");
    process.env.CODEXWORKER_APPROVAL_TIMEOUT_MS = "99999999";
    assert.equal(approvalTimeoutMs(), 600_000, "timeout is clamped to 10 min");
    process.env.CODEXWORKER_APPROVAL_POLL_MS = "10";
    assert.equal(approvalPollMs(), 200, "poll floor");
  } finally {
    for (const [key, value] of [["CODEXWORKER_APPROVAL_TIMEOUT_MS", previous.timeout], ["CODEXWORKER_APPROVAL_POLL_MS", previous.poll]]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

test("constructor rejects a router without the approval methods", () => {
  assert.throws(() => createApprovalChannel({ router: {} }), /requestApproval/);
  assert.throws(() => createApprovalChannel({}), /requestApproval/);
});
