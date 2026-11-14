import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { createApprovalRelay } from "../interface/desktop-connection/approval-relay.mjs";
import { isToolApproval } from "../interface/native-approval.mjs";
import { desktopServerOptions, runDesktopServer } from "../interface/desktop-connection/entry.mjs";
import { reviewNativePermission } from "../interface/permission-hook.mjs";
import { requireNativeDesktopParent, runNativeConnection } from "../interface/desktop-connection/native-entry.mjs";

const request = { id: 17, method: "mcpServer/elicitation/request", params: {
  threadId: "managed", turnId: "active", serverName: "cua_repl", mode: "form",
  message: 'Allow Computer Use to use "Calculator"?',
  requestedSchema: { type: "object", properties: {} },
  _meta: { codex_approval_kind: "mcp_tool_call", persist: ["session", "always"] } } };
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture(status = "approved") {
  const calls = [];
  const manager = { state: { sessions: { managed: {} } },
    read: async () => ({ turns: [{ id: "active", status: "inProgress" }] }),
    routerClient: { requestApproval: async body => { calls.push(body); return { approval_id: "ap_real_id", status }; },
      getApproval: async () => ({ status }) } };
  return { manager, calls };
}

test("only native empty MCP approval forms reach router; other forms retain native UI", async () => {
  const f = fixture();
  for (const params of [{ ...request.params, mode: "url" }, { ...request.params, _meta: {} },
    { ...request.params, requestedSchema: { type: "object", properties: { password: { type: "string" } } } },
    { ...request.params, requestedSchema: { type: "object", properties: {}, required: ["password"] } }]) {
    assert.equal(isToolApproval({ ...request, params }), false);
    assert.deepEqual(await reviewNativePermission(f.manager,
      { type: "desktop-server-request", request: { ...request, params } }), {});
  }
  assert.equal(f.calls.length, 0);
});

test("router decides the original elicitation with no persistent or worker-local grant", async () => {
  for (const status of ["approved", "denied", "expired"]) {
    const f = fixture(status);
    const result = await reviewNativePermission(f.manager, { type: "desktop-server-request", request });
    assert.deepEqual(result.nativeResponse, status === "approved" ? { action: "accept", content: {} }
      : { action: "decline", content: null });
    assert.equal(result.approvalId, "ap_real_id");
    assert.equal(f.calls.length, 1); assert.equal(f.calls[0].kind, "elicitation");
    assert.equal("auto" in f.calls[0], false);
    assert.equal(JSON.parse(f.calls[0].detail).requestId, request.id);
  }
  const f = fixture();
  f.manager.routerClient.requestApproval = async () => { throw Error("private credential"); };
  assert.deepEqual((await reviewNativePermission(f.manager, { type: "desktop-server-request", request })).nativeResponse,
    { action: "decline", content: null });
});

test("unmanaged and stale turns never get an approval; a missing turn ID uses the active turn", async () => {
  const f = fixture();
  const review = params => reviewNativePermission(f.manager, { type: "desktop-server-request", request: { ...request, params } });
  assert.deepEqual(await review({ ...request.params, threadId: "other" }), {});
  assert.equal((await review({ ...request.params, turnId: "stale" })).nativeResponse.action, "decline");
  assert.equal(f.calls.length, 0);
  assert.equal((await review({ ...request.params, turnId: null })).nativeResponse.action, "accept");
  assert.equal(JSON.parse(f.calls[0].detail).turnId, "active");
});

test("manual response, resolved event and disconnect each cancel a pending router response", async () => {
  for (const finish of [relay => relay.client({ id: 17, result: { action: "decline" } }),
    relay => relay.server({ method: "serverRequest/resolved", params: { requestId: 17 } }), relay => relay.close()]) {
    let complete, signal, reviewed = 0; const sent = [];
    const relay = createApprovalRelay({ send: value => sent.push(value), review: (_, opts) => {
      signal = opts.signal; reviewed++; return new Promise(resolve => { complete = resolve; }); } });
    relay.server(request); relay.server(request); await tick();
    assert.equal(reviewed, 1);
    finish(relay); assert.equal(signal.aborted, true);
    complete({ nativeResponse: { action: "accept", content: {} }, approvalId: "ap_late" });
    await tick(); assert.deepEqual(sent, []);
  }
});

test("Desktop JSONL keeps the native prompt and returns router decision on the same request ID", async () => {
  const f = fixture();
  const channel = new EventEmitter(); channel.stdin = new PassThrough(); channel.stdout = new PassThrough();
  channel.kill = () => setImmediate(() => channel.emit("exit", 0));
  const input = new PassThrough(), output = new PassThrough(), errors = new PassThrough();
  let ui = "", upstream = "";
  output.on("data", bytes => { ui += bytes; }); channel.stdin.on("data", bytes => { upstream += bytes; });
  const done = runDesktopServer({}, { config: {} }, { stdin: input, stdout: output, stderr: errors,
    connect: async () => channel, review: event => reviewNativePermission(f.manager, event) });
  await tick();
  const bytes = Buffer.from(JSON.stringify({ ...request, params: { ...request.params, message: "计算器授权" } }) + "\n");
  const at = bytes.indexOf(Buffer.from("计")) + 1;
  channel.stdout.write(bytes.subarray(0, at)); channel.stdout.write(bytes.subarray(at));
  await tick(); await tick();
  assert.equal(JSON.parse(ui).id, 17);
  assert.deepEqual(JSON.parse(upstream), { id: 17, result: { action: "accept", content: {} } });
  assert.equal(f.calls.length, 1);
  input.write(JSON.stringify({ id: 17, result: { action: "decline", content: null } }) + "\n");
  assert.equal(upstream.trim().split("\n").length, 1);
  channel.stdout.write(JSON.stringify({ method: "serverRequest/resolved", params: { requestId: 17 } }) + "\n");
  input.end(); assert.equal(await done, 0);
});

test("unavailable review preserves native response and never invents an allow", async () => {
  const sent = [];
  const relay = createApprovalRelay({ send: value => sent.push(value), review: async () => { throw Error("offline"); } });
  relay.server(request); await tick(); assert.deepEqual(sent, []); relay.close();
});

test("native connection supports both Desktop process families and rejects manual launches", async () => {
  for (const [platform, owner] of [["darwin", { pid: 7, startedAt: "now", name: "ChatGPT",
    executable: "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT" }],
  ["win32", { pid: 8, startedAt: "now", name: "ChatGPT.exe",
    executable: "C:\\Program Files\\WindowsApps\\OpenAI.Codex_test\\app\\ChatGPT.exe" }]]) {
    assert.deepEqual(await requireNativeDesktopParent({ platform, readChain: async () => [owner] }), owner);
    await assert.rejects(requireNativeDesktopParent({ platform, readChain: async () => [] }), /requires_desktop_parent/);
  }
  await assert.rejects(runNativeConnection(["app-server"], { verifyParent: async () => { throw Error("denied"); },
    load: () => assert.fail("must not read"), spawnImpl: () => assert.fail("must not spawn") }), /denied/);
});

test("Desktop native wrapper preserves its exact argv, environment and owned-child lifecycle", async () => {
  const child = new EventEmitter();
  child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
  child.exitCode = null; child.signalCode = null;
  let stopped = 0; child.kill = () => assert.fail("native child should first receive EOF");
  child.stdin.once("finish", () => { stopped++; setImmediate(() => {
    child.exitCode = 3; child.emit("exit", 3); child.stdout.end(); child.stderr.end(); child.emit("close", 3);
  }); });
  const input = new PassThrough(), output = new PassThrough(), errors = new PassThrough();
  const argv = ["-c", 'model="native-model"', "app-server", "--analytics-default-enabled",
    "--native-startup-option", "future-value", "-c", "provider=native-toml-value"];
  let received = ""; child.stdin.on("data", bytes => { received += bytes; });
  const done = runNativeConnection(argv, { stdin: input, stdout: output, stderr: errors,
    verifyParent: async () => ({}), load: async () => ({ realCli: "native-desktop-cli" }),
    env: { CODEX_CLI_PATH: "wrapper", KEEP: "same-value" }, spawnImpl: (file, args, options) => {
      assert.equal(file, "native-desktop-cli"); assert.deepEqual(args, argv);
      assert.deepEqual(options.env, { KEEP: "same-value" });
      setImmediate(() => child.emit("spawn")); return child;
    } });
  await tick(); await tick();
  input.write(JSON.stringify({ id: 1, method: "initialize", params: { clientInfo: { name: "Codex Desktop" } } }) + "\n");
  assert.equal(JSON.parse(received).params.clientInfo.name, "Codex Desktop");
  input.end(); assert.equal(await done, 3); assert.equal(stopped, 1);
});

test("native option delegation keeps auxiliary checks and rejects independent listeners", () => {
  const native = { nativePassthrough: true };
  for (const argv of [["-c", "provider=native-toml-value", "app-server", "--print-startup-config"],
    ["app-server", "generate-json-schema"], ["app-server", "daemon", "version"]]) {
    assert.equal(desktopServerOptions(argv, native), null);
  }
  assert.deepEqual(desktopServerOptions(["app-server", "--listen=stdio://"], native), { config: {} });
  for (const argv of [["app-server", "daemon", "start"], ["app-server", "--listen", "ws://localhost:9000"],
    ["app-server", "--listen=ws://localhost:9000"]]) {
    assert.throws(() => desktopServerOptions(argv, native), /unsupported_native_desktop_listener/);
  }
});

test("optional image endpoint failure preserves the real Mac Desktop startup", { skip: process.platform !== "darwin" }, async () => {
  const child = new EventEmitter(); child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
  child.exitCode = null; child.signalCode = null; child.kill = () => assert.fail("must close native stdin first");
  child.stdin.once("finish", () => setImmediate(() => {
    child.exitCode = 0; child.emit("exit", 0); child.stdout.end(); child.stderr.end(); child.emit("close", 0);
  }));
  const input = new PassThrough(), output = new PassThrough(), errors = new PassThrough(); let diagnostics = "", checked = false;
  errors.on("data", bytes => { diagnostics += bytes; });
  const done = runNativeConnection(["app-server"], { stdin: input, stdout: output, stderr: errors,
    verifyParent: async () => ({ pid: process.ppid }), load: async () => ({ realCli: "native-desktop-cli" }),
    env: { CODEXWORKER_STATE_DIR: "/test-image-controller-no-files", CODEXWORKER_NATIVE_IMAGE_INPUT: "stale",
      CODEXWORKER_NATIVE_INPUT_DESCRIPTOR: "stale-descriptor" },
    createInput: async () => { checked = true; throw Error("private filesystem permission detail"); },
    spawnImpl: (_, __, options) => {
      assert.equal(options.env.CODEXWORKER_NATIVE_IMAGE_INPUT, undefined);
      assert.equal(options.env.CODEXWORKER_NATIVE_INPUT_DESCRIPTOR, undefined);
      setImmediate(() => child.emit("spawn")); return child;
    } });
  await tick(); await tick(); input.end();
  assert.equal(await done, 0); assert.equal(checked, true);
  assert.ok(diagnostics.includes("native image input unavailable")); assert.equal(diagnostics.includes("private filesystem"), false);
});
