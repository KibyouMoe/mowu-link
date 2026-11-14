import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import { fileURLToPath } from "node:url";
import { reviewNativePermission } from "../interface/permission-hook.mjs";
import { DesktopManager } from "../desktop-manager.mjs";

const event = { hook_event_name: "PermissionRequest", session_id: "registered-thread", turn_id: "active-turn",
  cwd: "/workspace", tool_name: "Bash", tool_input: { command: "echo approved", description: "Run the requested check" } };
function fixture(status = "approved") {
  const requests = [];
  const manager = { state: { sessions: { "registered-thread": {} } },
    read: async () => ({ turns: [{ id: "active-turn", status: "inProgress" }] }),
    routerClient: { activity: { taskId: "t-existing" }, async requestApproval(body) {
      requests.push(body); return { approval_id: "ap_native_hook", status }; },
      getApproval: async () => ({ status }) } };
  return { manager, requests };
}
test("native hook forwards the active request to router and returns its native allow shape", async () => {
  const f = fixture();
  const result = await reviewNativePermission(f.manager, event);
  assert.equal(result.hookSpecificOutput.hookEventName, "PermissionRequest");
  assert.deepEqual(result.hookSpecificOutput.decision, { behavior: "allow" });
  assert.match(result.systemMessage, /ap_native_hook approved/);
  assert.equal(f.requests.length, 1); assert.equal(f.requests[0].taskId, "t-existing");
  assert.equal(f.requests[0].kind, "command");
  const detail = JSON.parse(f.requests[0].detail);
  assert.equal(detail.threadId, event.session_id); assert.equal(detail.turnId, event.turn_id);
  assert.equal(detail.command, "echo approved"); assert.equal(detail.commandSha256.length, 64);
});
test("router rejection and exceptions deny the original permission request", async () => {
  const f = fixture("denied");
  assert.equal((await reviewNativePermission(f.manager, event)).hookSpecificOutput.decision.behavior, "deny");
  f.manager.routerClient.requestApproval = async () => { throw new Error("private credential error"); };
  const result = await reviewNativePermission(f.manager, event);
  assert.equal(result.hookSpecificOutput.decision.behavior, "deny");
  assert.ok(!JSON.stringify(result).includes("private credential"));
});
test("unmanaged chats retain native UI; stale or malformed requests never reach router", async () => {
  const f = fixture();
  assert.deepEqual(await reviewNativePermission(f.manager, { ...event, session_id: "other-thread" }), {});
  assert.equal((await reviewNativePermission(f.manager, { ...event, turn_id: "old-turn" })).hookSpecificOutput.decision.behavior, "deny");
  assert.equal((await reviewNativePermission(f.manager, {})).hookSpecificOutput.decision.behavior, "deny");
  assert.equal(f.requests.length, 0);
});
test("MCP review excludes arbitrary arguments and command review redacts credentials", async () => {
  const f = fixture();
  await reviewNativePermission(f.manager, { ...event, tool_name: "mcp__cua_repl__js",
    tool_input: { code: "private arguments", password: "private password" } });
  assert.equal(f.requests[0].kind, "elicitation");
  assert.ok(!JSON.stringify(f.requests[0]).includes("private"));
  await reviewNativePermission(f.manager, { ...event, tool_input: {
    command: 'curl -H "Authorization: Bearer test-secret-value" https://example.test' } });
  assert.ok(!f.requests[1].detail.includes("test-secret-value"));
  await reviewNativePermission(f.manager, { ...event, tool_name: "other_tool" });
  assert.equal(f.requests[2].kind, "approval");
});
test("authenticated loopback endpoint routes the real manager through the permission adapter", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "native-hook-manager-"));
  const token = "native-hook-test-token-long-enough";
  const manager = new DesktopManager({ stateDir: root, native: async () => ({}),
    listNativeTools: async () => ["read_thread", "send_message_to_thread", "list_threads", "create_thread", "fork_thread", "set_thread_title"]
      .map(name => ({ name })), config: { token, port: 0, pollMs: 1000000, workspaceRoots: [root], feishu: {} } });
  t.after(async () => { await manager.close(); await rm(root, { recursive: true, force: true }); });
  await manager.initialize();
  await manager.listen();
  const f = fixture(); manager.state.sessions = f.manager.state.sessions;
  manager.read = f.manager.read; manager.routerClient = f.manager.routerClient;
  const endpoint = `http://127.0.0.1:${manager.server.address().port}/bridge/permissions/review`;
  const request = { method: "POST", body: JSON.stringify(event), headers: { "content-type": "application/json" } };
  assert.equal((await fetch(endpoint, request)).status, 401);
  const response = await fetch(endpoint, { ...request, headers: { ...request.headers, authorization: "Bearer " + token } });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).hookSpecificOutput.decision.behavior, "allow");
  assert.equal(f.requests.length, 1);
});

test("packaged hook CLI handles paths with spaces and returns only the host decision", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "permission hook paths "));
  const state = path.join(root, "worker state"); await mkdir(state);
  const tokenFile = path.join(root, "test.token"), token = "isolated-hook-token-long-enough";
  await writeFile(tokenFile, token);
  const calls = []; let unavailable = false;
  const server = http.createServer(async (req, res) => {
    let text = ""; for await (const chunk of req) text += chunk;
    assert.equal(req.url, "/bridge/permissions/review");
    assert.equal(req.headers.authorization, "Bearer " + token);
    calls.push(JSON.parse(text));
    res.writeHead(unavailable ? 503 : 200, { "content-type": "application/json" });
    res.end(JSON.stringify({ hookSpecificOutput: { hookEventName: "PermissionRequest", decision: { behavior: "allow" } } }));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await rm(root, { recursive: true, force: true }); });
  const configFile = path.join(state, "desktop-config.local.json");
  const config = { port: server.address().port, tokenFile };
  await writeFile(configFile, JSON.stringify(config));
  const script = fileURLToPath(new URL("../scripts/review-permission-hook.mjs", import.meta.url));
  async function run() {
    const child = spawn(process.execPath, [script], { env: { ...process.env, CODEXWORKER_STATE_DIR: state }, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    child.stdout.on("data", data => { stdout += data; }); child.stderr.on("data", data => { stderr += data; });
    const exited = new Promise((resolve, reject) => { child.once("exit", resolve); child.once("error", reject); });
    child.stdin.end(JSON.stringify(event)); assert.equal(await exited, 0); assert.equal(stderr, "");
    return JSON.parse(stdout);
  }
  assert.equal((await run()).hookSpecificOutput.decision.behavior, "allow");
  assert.deepEqual(calls, [event]);
  unavailable = true;
  assert.deepEqual(await run(), {}); assert.equal(calls.length, 2);
  unavailable = false;
  await writeFile(configFile, JSON.stringify({ ...config, autoStart: false }));
  assert.deepEqual(await run(), {}); assert.equal(calls.length, 2);
  await writeFile(configFile, JSON.stringify({ ...config, host: "remote.example" }));
  assert.equal((await run()).hookSpecificOutput.decision.behavior, "deny"); assert.equal(calls.length, 2);
  const hooks = JSON.parse(await readFile(new URL("../hooks/hooks.json", import.meta.url), "utf8"));
  const manifest = JSON.parse(await readFile(new URL("../.codex-plugin/plugin.json", import.meta.url), "utf8"));
  assert.equal(manifest.hooks, "./hooks/hooks.json");
  assert.equal(hooks.hooks.PermissionRequest[0].hooks[0].command, 'node "${PLUGIN_ROOT}/scripts/review-permission-hook.mjs"');
});
