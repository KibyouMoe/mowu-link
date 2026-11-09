import assert from "node:assert/strict";
import test from "node:test";
import { cp, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import net from "node:net";
import { once } from "node:events";
import { mkdir } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { PluginRuntime } from "../plugin-runtime.mjs";

const packageRoot = fileURLToPath(new URL("..", import.meta.url));
const sample = `import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path'; import { value } from './helper.mjs';
export const BUSINESS_API_VERSION=1, BUSINESS_STATE_VERSION=1, BUSINESS_VERSION='test';
export function createBusiness(control){let busy=false, owner=false; const file=path.join(process.env.USERPROFILE,'durable.json');
  const read=async()=>JSON.parse(await readFile(file,'utf8').catch(()=>'{"selected":"same-chat","queue":["one","two"],"count":0}'));
  return { busy:()=>busy, activate:async()=>{owner=true; return read()},
    quiesce:async()=>busy?{busy:true}:{busy:false,hadManager:owner}, close:async()=>{},
    handleRequest:async(method,p)=>{
      if(method==='ping')return {};
      if(method==='initialize'){owner=true;return {serverInfo:{version:'test'}}}
      if(method==='tools/list')return {tools:[{name:'test_value', inputSchema:{type:'object'}}]};
      if(method==='value')return {value,...await read()};
      if(method==='set-busy'){busy=p.busy;return {busy}};
      if(method==='count'){const state=await read();state.count++;await writeFile(file,JSON.stringify(state));return state};
      if(method==='reload')return control.reload();
      if(method==='status')return control.status();
      return {};
    }};
}`;
async function fixture(t, { pollMs = 1000000 } = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "rudolf-full-hotload-"));
  const source = path.join(root, "source"), stateDir = path.join(root, "state");
  await cp(packageRoot, source, { recursive: true });
  const { mkdir } = await import("node:fs/promises"); await mkdir(stateDir);
  await writeFile(path.join(source, "mcp-business.mjs"), sample);
  // Keep only a deterministic business fixture; never inherit a real pipe/config.
  for (const name of ["desktop-manager.mjs", "feishu.mjs", "feishu-images.mjs", "feishu-image-output.mjs", "hermes-ingress.mjs", "online-announcement.mjs", "question-relay.mjs", "question-cards.mjs", "native-dispatch.mjs"]) await rm(path.join(source, name));
  await writeFile(path.join(source, "helper.mjs"), "export const value='first';\n");
  let changes = 0;
  const runtime = new PluginRuntime({ pluginRoot: source, stateDir, pollMs,
    env: { ...process.env, USERPROFILE: root, HOME: root, CODEX_APP_TOOLS_PIPE_PATH: "", CODEXWORKER_LIFECYCLE_CHILD: "1" },
    onToolsChanged: () => { changes++; } });
  const runtimes = [runtime];
  t.after(async () => { for (const item of runtimes) await item.close(); assert.ok(root.startsWith(os.tmpdir())); await rm(root, { recursive: true, force: true }); });
  await runtime.call("initialize");
  return { runtime, root, source, stateDir, changes: () => changes,
    keep: item => runtimes.push(item), change: text => writeFile(path.join(source, "helper.mjs"), text) };
}

test("whole import graph refreshes, preserves durable selection/queue and retires old workers", async t => {
  const f = await fixture(t), oldPid = f.runtime.status().businessPid;
  await f.runtime.call("count"); await f.change("export const value='second';\n"); await f.runtime.check();
  assert.deepEqual(await f.runtime.call("value"), { value: "second", selected: "same-chat", queue: ["one", "two"], count: 1 });
  assert.equal(f.runtime.status().scope, "all-business"); assert.equal(f.runtime.status().bootstrapPid, process.pid);
  assert.notEqual(f.runtime.status().businessPid, oldPid); assert.equal(f.runtime.children.size, 1);
  assert.equal(f.changes(), 0); assert.equal(f.runtime.status().lastReloadError, null);
  await f.runtime.check(); assert.equal(f.runtime.status().generation, 2);
});
test("syntax, state API and malformed tool metadata fail before any working generation is closed", async t => {
  const f = await fixture(t), before = f.runtime.status();
  await f.change("invalid javascript !!!!"); await f.runtime.check();
  assert.equal(f.runtime.status().sha256, before.sha256); assert.equal((await f.runtime.call("value")).value, "first");
  assert.equal(f.runtime.status().lastReloadError, "runtime_reload_failed");
  await f.change("export const value='first';\n");
  await writeFile(path.join(f.source, "mcp-business.mjs"), sample.replace("BUSINESS_STATE_VERSION=1", "BUSINESS_STATE_VERSION=2"));
  await f.runtime.check(); assert.equal(f.runtime.status().businessPid, before.businessPid);
  await writeFile(path.join(f.source, "mcp-business.mjs"), sample.replace("type:'object'", "type:'string'"));
  await f.runtime.check(); assert.equal(f.runtime.status().businessPid, before.businessPid); assert.equal(f.runtime.children.size, 1);
});
test("busy work defers handoff without cancellation; source changes apply when idle", async t => {
  const f = await fixture(t), pid = f.runtime.status().businessPid;
  await f.runtime.call("set-busy", { busy: true }); await f.change("export const value='after-idle';\n");
  await f.runtime.check(); assert.equal(f.runtime.status().businessPid, pid); assert.equal(f.runtime.status().reloadPending, true);
  await f.runtime.call("set-busy", { busy: false }); await f.runtime.check();
  assert.equal((await f.runtime.call("value")).value, "after-idle"); assert.equal(f.runtime.status().reloadPending, false);
});
test("candidate activation failure rolls back to original business and durable state", async t => {
  const f = await fixture(t), before = f.runtime.status(); await f.runtime.call("count");
  await writeFile(path.join(f.source, "mcp-business.mjs"), sample.replace("activate:async()=>{owner=true; return read()}", "activate:async()=>{throw new Error('rejected candidate')}"));
  await f.runtime.check();
  assert.equal(f.runtime.status().sha256, before.sha256); assert.equal(f.runtime.status().businessPid, before.businessPid);
  assert.equal((await f.runtime.call("value")).count, 1); assert.equal(f.runtime.children.size, 1);
});
test("source polling and explicit reload control work without changing bootstrap or restarting a client", async t => {
  const f = await fixture(t, { pollMs: 30 }); await f.change("export const value='automatic';\n");
  const until = Date.now() + 5000;
  while (f.runtime.status().generation < 2 && Date.now() < until) await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal((await f.runtime.call("value")).value, "automatic");
  const scheduled = await f.runtime.call("reload"); assert.equal(scheduled.reloadScheduled, true);
});
test("more than 32 generations remain bounded to one retired ESM graph at a time", async t => {
  const f = await fixture(t);
  for (let i = 0; i < 34; i++) {
    await f.change(`export const value='generation-${i}';\n`); await f.runtime.check();
    assert.equal((await f.runtime.call("value")).value, `generation-${i}`);
    assert.equal(f.runtime.children.size, 1);
  }
  assert.equal(f.runtime.status().generation, 35); assert.equal(f.runtime.status().lastReloadError, null);
});
test("private config changes trigger generation refresh without exposing their hash or contents", async t => {
  const f = await fixture(t); const sha = f.runtime.status().sha256;
  await writeFile(path.join(f.stateDir, "desktop-config.local.json"), '{"testMarker":"private-not-for-health"}');
  await f.runtime.check(); assert.equal(f.runtime.status().generation, 2); assert.equal(f.runtime.status().sha256, sha);
  assert.ok(!JSON.stringify(f.runtime.status()).includes("private-not-for-health"));
});
test("tool definitions refresh and notify only when their metadata actually changes", async t => {
  const f = await fixture(t);
  await writeFile(path.join(f.source, "mcp-business.mjs"), sample.replace("name:'test_value'", "name:'test_value_updated'"));
  await f.runtime.check();
  assert.equal((await f.runtime.call("tools/list")).tools[0].name, "test_value_updated"); assert.equal(f.changes(), 1);
});
test("a tool notification failure after activation never rolls back the committed business", async t => {
  const f = await fixture(t), old = f.runtime.current;
  f.runtime.onToolsChanged = () => { throw new Error("closed client notification transport"); };
  await writeFile(path.join(f.source, "mcp-business.mjs"), sample.replace("name:'test_value'", "name:'updated_value'"));
  await f.runtime.check();
  assert.notEqual(f.runtime.current, old); assert.equal(f.runtime.status().generation, 2);
  assert.equal(f.runtime.status().lastReloadError, "runtime_tools_notification_failed");
  assert.equal(f.runtime.children.size, 1); assert.equal(old.child.exitCode, 0);
});
test("snapshot cleanup failure keeps the activated generation and does not revive the old worker", async t => {
  const f = await fixture(t), old = f.runtime.current, discard = f.runtime.discard.bind(f.runtime);
  f.runtime.discard = async directory => { if (directory === old.snapshot) throw new Error("test cleanup unavailable"); return discard(directory); };
  await f.change("export const value='committed';\n"); await f.runtime.check();
  assert.notEqual(f.runtime.current, old); assert.equal(f.runtime.status().lastReloadError, "runtime_cleanup_failed");
  assert.equal(old.child.exitCode, 0); assert.equal(f.runtime.children.size, 1);
  f.runtime.discard = discard;
  assert.equal((await f.runtime.call("value")).value, "committed");
});
test("trusted maintenance pause prevents half-edited code activation and resume applies the tested graph", async t => {
  const f = await fixture(t), pid = f.runtime.status().businessPid;
  await writeFile(path.join(f.stateDir, "runtime-source.local.json"), JSON.stringify({ sourceRoot: f.source, autoReload: false }));
  await f.change("export const value='verified-later';\n"); await f.runtime.check();
  assert.equal(f.runtime.status().businessPid, pid); assert.equal(f.runtime.status().reloadPaused, true);
  assert.equal((await f.runtime.call("value")).value, "first");
  await writeFile(path.join(f.stateDir, "runtime-source.local.json"), JSON.stringify({ sourceRoot: f.source, autoReload: true }));
  await f.runtime.check(); assert.equal((await f.runtime.call("value")).value, "verified-later");
});
test("bad development source at startup falls back to this installed package, not another session host", async t => {
  const f = await fixture(t);
  const development = path.join(f.root, "development"); await cp(f.source, development, { recursive: true });
  await writeFile(path.join(development, "helper.mjs"), "bad syntax !");
  await writeFile(path.join(f.stateDir, "runtime-source.local.json"), JSON.stringify({ sourceRoot: development }));
  await f.runtime.close();
  const replacement = new PluginRuntime({ pluginRoot: f.source, stateDir: f.stateDir, pollMs: 1000000,
    env: { ...process.env, USERPROFILE: f.root, HOME: f.root, CODEX_APP_TOOLS_PIPE_PATH: "", CODEXWORKER_LIFECYCLE_CHILD: "1" } });
  f.keep(replacement);
  assert.equal((await replacement.call("value")).value, "first");
  assert.equal(replacement.status().lastReloadError, "runtime_reload_failed");
});

for (const operatingMode of ["desktop-bridge", "feishu-local"]) test(`fixed loader drains sustained manager polls with an active native task, preserving UUID/FIFO and rollback (${operatingMode})`, async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "rudolf-manager-hotload-"));
  const source = path.join(root, "source"), stateDir = path.join(root, ".codex", "state", "codexworker");
  await cp(packageRoot, source, { recursive: true }); await mkdir(stateDir, { recursive: true });
  const token = "isolated-test-token-not-a-real-credential-123456", tokenFile = path.join(root, "test.token");
  await writeFile(tokenFile, token);
  const tcp = net.createServer(); tcp.listen(0, "127.0.0.1"); await once(tcp, "listening");
  const port = tcp.address().port; await new Promise(resolve => tcp.close(resolve));
  const toolNames = ["read_thread", "send_message_to_thread", "create_thread", "fork_thread", "set_thread_title", "list_threads", "list_projects"];
  const pipe = process.platform === "win32" ? `\\\\.\\pipe\\rudolf-hotload-test-${randomUUID()}` : path.join(root, "test.sock");
  let sends = 0;
  const nativePipe = net.createServer(socket => {
    let buffer = Buffer.alloc(0); socket.on("data", chunk => {
      buffer = Buffer.concat([buffer, chunk]); if (buffer.length < 4 || buffer.length < 4 + buffer.readUInt32LE(0)) return;
      const request = JSON.parse(buffer.subarray(4, 4 + buffer.readUInt32LE(0)).toString()); let result;
      if (request.method === "tools/list") result = { tools: toolNames.map(name => ({ name, namespace: "codex_app" })) };
      else {
        const name = request.params.tool;
        if (name === "read_thread") result = { thread: { id: "same-chat", cwd: root, title: "same-chat", status: { type: "running" } }, turns: [{ id: "busy-turn", status: "inProgress", items: [] }] };
        else if (name === "list_projects") result = { projects: [{ projectId: "test-project", path: root, hostId: "local" }] };
        else if (name === "list_threads") result = { pinnedThreads: [], threads: [] };
        else { if (name === "send_message_to_thread") sends++; result = {}; }
        result = { success: true, contentItems: [{ type: "inputText", text: JSON.stringify(result) }] };
      }
      const bytes = Buffer.from(JSON.stringify({ jsonrpc: "2.0", id: request.id, result })); const frame = Buffer.alloc(4 + bytes.length);
      frame.writeUInt32LE(bytes.length); bytes.copy(frame, 4);
      // A 1ms interval and 40ms native reads continuously re-create busy work
      // unless the BUSINESS layer actively pauses future polling on inspect.
      if (request.params?.tool === "read_thread") setTimeout(() => socket.end(frame), 40);
      else socket.end(frame);
    });
  }); nativePipe.listen(pipe); await once(nativePipe, "listening");
  await writeFile(path.join(stateDir, "manager-binding.json"), JSON.stringify({ managerThreadId: "same-chat", managerTurnId: "test-turn" }));
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ tokenFile, port, workspaceRoots: [root],
    operatingMode, ...(operatingMode === "feishu-local" ? {
      hermesIngress: { enabled: true, tokenFile: path.join(root, "missing-hermes.token") },
      xiliu: { enabled: true, url: "http://127.0.0.1:1" } } : {}), pollMs: 1, hotHandoffRetryMs: 50, managerProxyWaitMs: 100 }));
  const runtime = new PluginRuntime({ pluginRoot: source, stateDir, pollMs: 1000000,
    env: { ...process.env, USERPROFILE: root, HOME: root, CODEX_APP_TOOLS_PIPE_PATH: pipe, CODEXWORKER_LIFECYCLE_CHILD: "1" } });
  let proxy, failedStartupProxy;
  t.after(async () => { await failedStartupProxy?.close(); await proxy?.close(); await runtime.close(); await new Promise(resolve => nativePipe.close(resolve));
    assert.ok(root.startsWith(os.tmpdir())); await rm(root, { recursive: true, force: true }); });
  const request = async (route, body) => {
    const response = await fetch(`http://127.0.0.1:${port}${route}`, { method: body ? "POST" : "GET",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, ...(body ? { body: JSON.stringify(body) } : {}) });
    assert.equal(response.status, 200); return response.json();
  };
  const coldProxy = new PluginRuntime({ pluginRoot: source, stateDir, pollMs: 1000000,
    env: { ...process.env, USERPROFILE: root, HOME: root, CODEX_APP_TOOLS_PIPE_PATH: pipe,
      CODEXWORKER_LIFECYCLE_CHILD: "0", CODEXWORKER_LIFECYCLE_READY: "1" } });
  try {
    const cold = await coldProxy.call("tools/call", { name: "bridge_start_local_manager", arguments: {} });
    assert.equal(cold.isError, true);
    assert.equal(cold.structuredContent.error, "desktop_lifetime_writer_unavailable");
    assert.equal(cold.structuredContent.retryable, true);
    await assert.rejects(fetch(`http://127.0.0.1:${port}/health`), "a cold MCP must not create a short-lifetime writer");
  } finally { await coldProxy.close(); }
  failedStartupProxy = new PluginRuntime({ pluginRoot: source, stateDir, pollMs: 1000000,
    env: { ...process.env, USERPROFILE: root, HOME: root, CODEX_APP_TOOLS_PIPE_PATH: pipe,
      CODEXWORKER_LIFECYCLE_CHILD: "0", CODEXWORKER_LIFECYCLE_READY: "0" } });
  const failedCold = await failedStartupProxy.call("tools/call", { name: "bridge_start_local_manager", arguments: {} });
  assert.equal(failedCold.isError, true); assert.equal(failedCold.structuredContent.error, "desktop_lifetime_unavailable");
  assert.equal(failedCold.structuredContent.retryable, true);
  await assert.rejects(fetch(`http://127.0.0.1:${port}/health`), "failed initialize must not permit a new writer");
  const adopted = await runtime.call("tools/call", { name: "bridge_register_current_task", arguments: {}, _meta: { "openai/threadId": "same-chat" } });
  assert.equal(adopted.isError, false);
  const beforeRecoveredProxy = await request("/health");
  const recovered = await failedStartupProxy.call("tools/call", { name: "bridge_start_local_manager", arguments: {} });
  assert.equal(recovered.isError, false, "a cached failed initialize may recover only by connecting to this healthy writer");
  assert.equal((await request("/health")).integrations.pid, beforeRecoveredProxy.integrations.pid);
  assert.equal((await request("/health")).capabilities.businessHotReload.generation, beforeRecoveredProxy.capabilities.businessHotReload.generation);
  proxy = new PluginRuntime({ pluginRoot: source, stateDir, pollMs: 1000000,
    env: { ...process.env, USERPROFILE: root, HOME: root, CODEX_APP_TOOLS_PIPE_PATH: pipe,
      CODEXWORKER_LIFECYCLE_CHILD: "0", CODEXWORKER_LIFECYCLE_READY: "1" } });
  const proxied = await proxy.call("tools/call", { name: "bridge_start_local_manager", arguments: {} });
  assert.equal(proxied.isError, false);
  const message = { protocolVersion: "1.1.1-draft", source: "hermes", messageId: randomUUID(), text: "queued-preserve", delivery: "queue" };
  await request("/manager/messages", message); const before = await request("/health");
  assert.equal(before.activeTurns, 1);
  assert.equal(before.operatingMode, operatingMode);
  if (operatingMode === "feishu-local") {
    assert.equal(before.integrations.hermesIngress.enabled, false); assert.equal(before.integrations.xiliu.enabled, false);
  }
  assert.equal(before.capabilities.businessHotReload.bootstrapVersion, "0.2.23");
  const waitFor = async predicate => {
    const until = Date.now() + 5000;
    while (!predicate() && Date.now() < until) { await runtime.check(); await new Promise(resolve => setTimeout(resolve, 10)); }
    assert.ok(predicate(), "handoff must settle within a bounded interval under sustained polling");
  };
  const filename = path.join(source, "native-dispatch.mjs"), original = await readFile(filename, "utf8");
  await writeFile(filename, original + "\n// isolated hotload acceptance\n");
  await proxy.check();
  const stillOwned = await request("/health");
  assert.equal(stillOwned.integrations.pid, before.integrations.pid, "proxy reload must not close or activate a machine writer");
  assert.equal(stillOwned.capabilities.businessHotReload.generation, before.capabilities.businessHotReload.generation);
  await proxy.call("tools/call", { name: "bridge_start_local_manager", arguments: {} });
  assert.equal((await request("/health")).integrations.pid, before.integrations.pid);
  await runtime.check();
  await waitFor(() => runtime.status().generation === 2);
  const after = await request("/health"), sessions = await request("/manager/sessions");
  assert.notEqual(after.integrations.pid, before.integrations.pid); assert.equal(after.integrations.runtimeHostPid, process.pid);
  assert.equal(sessions.selectedThreadId, "same-chat"); assert.equal(sessions.sessions[0].queue.length, 1);
  assert.equal((await request("/manager/messages", message)).duplicate, true); assert.equal(sends, 0);
  assert.equal(after.activeTurns, 1); assert.equal(runtime.status().bootstrapPid, process.pid);
  // Syntax validation fails BEFORE close: the already paused old manager must
  // resume polling, not leave a healthy HTTP writer stuck in maintenance mode.
  await writeFile(filename, "invalid source !!!");
  await waitFor(() => runtime.status().lastReloadError === "runtime_reload_failed");
  await new Promise(resolve => setTimeout(resolve, 70));
  const resumed = await request("/health");
  assert.equal(resumed.integrations.pid, after.integrations.pid);
  assert.equal(resumed.capabilities.businessHotReload.handoff.paused, false);
  assert.equal(resumed.capabilities.businessHotReload.handoff.error, "candidate_rejected");
  await writeFile(filename, original + "\n// isolated hotload acceptance\n");
  const managerFile = path.join(source, "desktop-manager.mjs"), managerSource = await readFile(managerFile, "utf8");
  await writeFile(managerFile, managerSource.replace("if (!this.config.token ||", "if (true || !this.config.token ||"));
  await runtime.check();
  await waitFor(() => runtime.status().lastReloadError === "runtime_reload_failed" && !runtime.status().reloadPending);
  const rolledBack = await request("/health"); assert.equal(rolledBack.integrations.pid, after.integrations.pid);
  assert.equal(runtime.status().lastReloadError, "runtime_reload_failed");
  assert.equal((await request("/manager/sessions")).selectedThreadId, "same-chat"); assert.equal(sends, 0);
});
