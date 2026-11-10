// app-server 审批协议：server→client 请求的识别、响应形状、回调接线。
//
// 不启动真的 codex app-server：把 child.stdin 换成记录器，直接把协议行喂给
// handleLine()。真实 method 名来自 `codex app-server generate-json-schema`
// 的 ServerRequest.json（codex-cli 0.167.0）。
import assert from "node:assert/strict";
import test from "node:test";
import {
  AppServerClient,
  approvalResponse,
  approvalRequestSummary,
  defaultApprovalPolicy,
  isApprovalRequest,
  setApprovalHandler,
} from "../interface/transport/app-server.mjs";

function harness(handler) {
  const client = new AppServerClient();
  const written = [];
  client.child = {
    stdin: {
      write(chunk) {
        written.push(JSON.parse(String(chunk).trim()));
        return true;
      },
    },
  };
  client.setLogger(() => {});
  client.setApprovalHandler(handler ?? null);
  return { client, written };
}

const commandRequest = {
  jsonrpc: "2.0",
  id: 7,
  method: "item/commandExecution/requestApproval",
  params: {
    threadId: "th-1",
    turnId: "tu-1",
    itemId: "it-1",
    startedAtMs: 1_760_000_000_000,
    command: "rm -rf ./tmp/build",
    cwd: "/Users/kibyou/projects/demo",
    reason: "命令不在沙箱允许列表里",
  },
};

test("isApprovalRequest knows the four approval methods and rejects the rest", () => {
  for (const method of [
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "applyPatchApproval",
    "execCommandApproval",
  ]) {
    assert.equal(isApprovalRequest(method), true, method);
  }
  for (const method of [
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "item/permissions/requestApproval",
    "item/tool/call",
    undefined,
  ]) {
    assert.equal(isApprovalRequest(method), false, String(method));
  }
});

test("approvalResponse uses the enum each protocol family expects", () => {
  assert.deepEqual(approvalResponse("item/commandExecution/requestApproval", "approved"), { decision: "accept" });
  assert.deepEqual(approvalResponse("item/commandExecution/requestApproval", "denied"), { decision: "decline" });
  assert.deepEqual(approvalResponse("item/fileChange/requestApproval", "approved"), { decision: "accept" });
  assert.deepEqual(approvalResponse("item/fileChange/requestApproval", "denied"), { decision: "decline" });
  assert.deepEqual(approvalResponse("execCommandApproval", "approved"), { decision: "approved" });
  assert.deepEqual(approvalResponse("applyPatchApproval", "denied"), { decision: "denied" });
  assert.equal(approvalResponse("item/tool/requestUserInput", "approved"), null);
});

test("approvalRequestSummary renders command and file-change requests for humans", () => {
  const command = approvalRequestSummary("item/commandExecution/requestApproval", commandRequest.params);
  assert.equal(command.kind, "command");
  assert.match(command.title, /rm -rf/);
  assert.match(command.detail, /cwd: \/Users\/kibyou\/projects\/demo/);
  assert.match(command.detail, /命令不在沙箱允许列表里/);

  const legacy = approvalRequestSummary("execCommandApproval", {
    parsedCmd: ["git", "push", "--force"], cwd: "/repo",
  });
  assert.match(legacy.title, /git push --force/);

  const change = approvalRequestSummary("item/fileChange/requestApproval", {
    threadId: "th-1", turnId: "tu-1", itemId: "it-2", grantRoot: "/etc",
  }, { changes: { "src/a.mjs": { type: "update" }, "src/b.mjs": { type: "add" } } });
  assert.equal(change.kind, "fileChange");
  assert.match(change.title, /src\/a\.mjs/);
  assert.match(change.detail, /M src\/a\.mjs/);
  assert.match(change.detail, /A src\/b\.mjs/);
  assert.match(change.detail, /grantRoot: \/etc/);
});

test("approval request -> handler approved -> accept written back with the same id", async () => {
  const seen = [];
  const { client, written } = harness(async (request) => {
    seen.push(request);
    return { decision: "approved", approvalId: "ap_1" };
  });
  const before = written.length;
  client.handleLine(JSON.stringify(commandRequest));
  await client.waitForServerRequests();
  assert.equal(seen.length, 1);
  assert.equal(seen[0].method, "item/commandExecution/requestApproval");
  assert.equal(seen[0].kind, "command");
  assert.equal(seen[0].threadId, "th-1");
  assert.equal(seen[0].itemId, "it-1");
  assert.match(seen[0].title, /rm -rf/);
  assert.equal(written.length, before + 1);
  assert.deepEqual(written.at(-1), { jsonrpc: "2.0", id: 7, result: { decision: "accept" } });
});

test("handler denied -> decline", async () => {
  const { client, written } = harness(async () => ({ decision: "denied", note: "训练员拒绝" }));
  client.handleLine(JSON.stringify({
    jsonrpc: "2.0", id: 8, method: "item/fileChange/requestApproval",
    params: { threadId: "th-1", turnId: "tu-1", itemId: "it-2", startedAtMs: 1 },
  }));
  await client.waitForServerRequests();
  assert.deepEqual(written.at(-1), { jsonrpc: "2.0", id: 8, result: { decision: "decline" } });
});

test("no handler -> explicit decline, never left hanging", async () => {
  const { client, written } = harness(null);
  client.handleLine(JSON.stringify(commandRequest));
  await client.waitForServerRequests();
  assert.deepEqual(written.at(-1), { jsonrpc: "2.0", id: 7, result: { decision: "decline" } });
});

test("handler throwing -> explicit decline", async () => {
  const { client, written } = harness(async () => { throw new Error("router died"); });
  client.handleLine(JSON.stringify(commandRequest));
  await client.waitForServerRequests();
  assert.deepEqual(written.at(-1), { jsonrpc: "2.0", id: 7, result: { decision: "decline" } });
});

test("legacy execCommandApproval maps to approved/denied", async () => {
  const { client, written } = harness(async () => ({ decision: "approved" }));
  client.handleLine(JSON.stringify({
    jsonrpc: "2.0", id: 9, method: "execCommandApproval",
    params: { callId: "c-1", conversationId: "th-1", command: "git push", parsedCmd: ["git", "push"], cwd: "/repo" },
  }));
  await client.waitForServerRequests();
  assert.deepEqual(written.at(-1), { jsonrpc: "2.0", id: 9, result: { decision: "approved" } });
});

test("non-approval server request keeps returning -32601", async () => {
  const { client, written } = harness(async () => ({ decision: "approved" }));
  client.handleLine(JSON.stringify({
    jsonrpc: "2.0", id: 10, method: "mcpServer/elicitation/request", params: {},
  }));
  await client.waitForServerRequests();
  assert.equal(written.at(-1).id, 10);
  assert.equal(written.at(-1).error.code, -32601);
  assert.equal(written.at(-1).result, undefined);
});

test("a normal JSON-RPC response still resolves its pending request", async () => {
  const { client } = harness(null);
  const pending = client.request("thread/start", { cwd: "/tmp" }, 5_000);
  const sent = JSON.parse(JSON.stringify({ id: 1 }));
  assert.equal(typeof sent.id, "number");
  client.handleLine(JSON.stringify({ jsonrpc: "2.0", id: 1, result: { thread: { id: "th-9" } } }));
  assert.deepEqual(await pending, { thread: { id: "th-9" } });
});

test("defaultApprovalPolicy: never by default, on-request only with a wired channel", () => {
  const previous = process.env.CODEXWORKER_APPROVAL_POLICY;
  try {
    delete process.env.CODEXWORKER_APPROVAL_POLICY;
    assert.equal(defaultApprovalPolicy(), "never");
    process.env.CODEXWORKER_APPROVAL_POLICY = "on-request";
    setApprovalHandler(null);
    assert.equal(defaultApprovalPolicy(), "never", "no channel -> fail closed");
    setApprovalHandler(async () => ({ decision: "denied" }));
    assert.equal(defaultApprovalPolicy(), "on-request");
    process.env.CODEXWORKER_APPROVAL_POLICY = "on-failure";
    assert.equal(defaultApprovalPolicy(), "on-failure");
  } finally {
    setApprovalHandler(null);
    if (previous === undefined) delete process.env.CODEXWORKER_APPROVAL_POLICY;
    else process.env.CODEXWORKER_APPROVAL_POLICY = previous;
  }
});
