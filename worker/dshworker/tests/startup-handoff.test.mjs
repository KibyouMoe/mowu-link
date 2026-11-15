// Real stdio/supervisor/HTTP processes, but an isolated native pipe and machine
// state. No production Feishu/Hermes connection or real task write is used.
import assert from "node:assert/strict";
import test from "node:test";
import { once } from "node:events";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import { cp, mkdtemp, mkdir, readFile, writeFile, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { desktopOwner, processChain } from "../desktop-lifecycle.mjs";

const pluginRoot = fileURLToPath(new URL("..", import.meta.url));
for (const shutdown of ["EOF", "pipelined inventory EOF", "Windows inventory job cleanup", "Windows nested inventory job cleanup", "Windows inventory process-tree cleanup"]) test(`cold initialization hands off before ${shutdown}; the supervisor-owned writer survives`, { timeout: 90000 }, async t => {
  if (process.platform !== "win32" || !desktopOwner(await processChain(process.pid))) {
    t.skip("Requires a Windows MSIX Desktop ancestor; isolated unit coverage remains portable"); return;
  }
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-cold-handoff-"));
  const stateDir = path.join(root, ".codex", "state", "codexworker");
  await mkdir(stateDir, { recursive: true });
  const token = "isolated-handoff-token-not-a-real-secret-123456", tokenFile = path.join(root, "test.token");
  await writeFile(tokenFile, token);
  const reserve = net.createServer(); reserve.listen(0, "127.0.0.1"); await once(reserve, "listening");
  const port = reserve.address().port; await new Promise(resolve => reserve.close(resolve));
  const pipe = process.platform === "win32"
    ? `\\\\.\\pipe\\codexworker-isolated-handoff-${randomUUID()}`
    : path.join(os.tmpdir(), `codexworker-isolated-handoff-${randomUUID()}.sock`);
  const sockets = new Set(); let nativeWrites = 0, inventory, writer, inventoryJob;
  const names = ["read_thread", "list_projects", "send_message_to_thread", "create_thread", "fork_thread", "set_thread_title", "list_threads"];
  const native = net.createServer(socket => {
    sockets.add(socket); socket.once("close", () => sockets.delete(socket));
    let pending = Buffer.alloc(0);
    socket.on("data", chunk => {
      pending = Buffer.concat([pending, chunk]); if (pending.length < 4) return;
      const size = pending.readUInt32LE(0); assert.ok(size <= 8 * 1024 * 1024); if (pending.length < size + 4) return;
      const request = JSON.parse(pending.subarray(4, size + 4).toString()); let result;
      if (request.method === "tools/list") result = { tools: names.map(name => ({ name, namespace: "codex_app" })) };
      else {
        assert.equal(request.params.callerSource, "codex"); const name = request.params.tool;
        if (name === "read_thread") result = { thread: { id: "isolated-chat", title: "isolated-chat", cwd: root }, turns: [] };
        else if (name === "list_projects") result = { projects: [{ projectId: "isolated-project", path: root, hostId: "local" }] };
        else if (name === "list_threads") result = { pinnedThreads: [], threads: [] };
        else { nativeWrites++; result = {}; }
        result = { success: true, contentItems: [{ type: "inputText", text: JSON.stringify(result) }] };
      }
      const bytes = Buffer.from(JSON.stringify({ jsonrpc: "2.0", id: request.id, result })), frame = Buffer.alloc(bytes.length + 4);
      frame.writeUInt32LE(bytes.length); bytes.copy(frame, 4); socket.end(frame);
    });
  }); native.listen(pipe); await once(native, "listening");
  const health = async () => {
    const r = await fetch(`http://127.0.0.1:${port}/health`, { headers: { authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(1500) });
    assert.equal(r.status, 200); return r.json();
  };
  t.after(async () => {
    // These PIDs come only from this test's private state and isolated listener.
    // Stop its supervisor first so cleanup cannot launch a replacement worker.
    let status; try { status = JSON.parse(await readFile(path.join(stateDir, "desktop-lifecycle-status.json"), "utf8")); } catch {}
    try { writer ??= await health(); } catch {}
    const owned = new Set([status?.supervisorPid, writer?.integrations?.runtimeHostPid, writer?.integrations?.pid, inventory?.pid]);
    for (const pid of owned) if (Number.isSafeInteger(pid) && pid > 0 && pid !== process.pid) { try { process.kill(pid); } catch {} }
    inventoryJob?.stdin.end();
    for (const socket of sockets) socket.destroy(); await new Promise(resolve => native.close(resolve));
    assert.ok(root.startsWith(os.tmpdir())); await rm(root, { recursive: true, force: true });
  });
  await writeFile(path.join(stateDir, "manager-binding.json"), JSON.stringify({ managerThreadId: "isolated-chat", managerTurnId: "isolated-turn" }));
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ tokenFile, port, bridgeName: "handoff-test", workspaceRoots: [root], pollMs: 1000000 }));
  const env = { ...process.env, USERPROFILE: root, HOME: root, CODEX_APP_TOOLS_PIPE_PATH: pipe };
  delete env.RUDOLF_BRIDGE_LIFECYCLE_CHILD; delete env.RUDOLF_BRIDGE_DESKTOP_STARTED_AT;
  let inventoryRoot = pluginRoot;
  if (shutdown === "EOF") {
    inventoryRoot = path.join(root, "disposable-plugin-cache");
    await cp(pluginRoot, inventoryRoot, { recursive: true });
    await writeFile(path.join(stateDir, "runtime-source.local.json"), JSON.stringify({ sourceRoot: path.resolve(pluginRoot), autoReload: true }));
  }
  inventory = spawn(process.execPath, [path.join(inventoryRoot, "server.mjs")], { cwd: inventoryRoot, env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
  let output = "", diagnostics = "";
  inventory.stdout.setEncoding("utf8"); inventory.stdout.on("data", chunk => { output += chunk; });
  inventory.stderr.setEncoding("utf8"); inventory.stderr.on("data", chunk => { diagnostics += chunk; });
  const exited = once(inventory, "exit");
  if (shutdown.includes("inventory job cleanup")) {
    inventoryJob = spawn(path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
      ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned", "-File", path.join(pluginRoot, "tests", "inventory-job.ps1"), String(inventory.pid),
        ...(shutdown.includes("nested") ? ["-Nested"] : [])],
      { windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    let jobOutput = "", jobError = "";
    inventoryJob.stderr.on("data", chunk => { jobError += chunk; });
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("Isolated job setup timed out")), 15000);
      inventoryJob.once("exit", code => { clearTimeout(timeout); reject(new Error(`Job setup exited ${code}: ${jobError}`)); });
      inventoryJob.stdout.on("data", chunk => { jobOutput += chunk; if (jobOutput.includes("\n")) { clearTimeout(timeout); resolve(); } });
    });
    assert.equal(JSON.parse(jobOutput.trim()).assigned, true);
  }
  const request = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }) + "\n";
  if (shutdown === "EOF") {
    inventory.stdin.end(request);
    const [code] = await exited; assert.equal(code, 0, diagnostics);
  } else {
    inventory.stdin.write(request + (shutdown === "pipelined inventory EOF" ? JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }) + "\n" : ""));
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error(`Inventory initialize timed out: ${diagnostics}`)), 60000);
      inventory.stdout.on("data", () => { if (output.includes("\n")) { clearTimeout(timeout); resolve(); } });
    });
    writer = await health();
    if (shutdown === "pipelined inventory EOF") {
      inventory.stdin.end();
      const [code] = await exited; assert.equal(code, 0, diagnostics);
    } else if (inventoryJob) {
      const jobClosed = once(inventoryJob, "exit"); inventoryJob.stdin.end(); await jobClosed;
    } else {
      // Only this test's directly spawned inventory PID may be terminated. A
      // Windows job breakaway alone leaves its descendants in this process tree.
      assert.ok(Number.isSafeInteger(inventory.pid) && inventory.pid > 0);
      await promisify(execFile)(path.join(process.env.SystemRoot, "System32", "taskkill.exe"),
        ["/PID", String(inventory.pid), "/T", "/F"], { windowsHide: true });
    }
    await Promise.race([exited, new Promise((_, reject) => {
      const timer = setTimeout(() => reject(new Error("Inventory resource-job cleanup did not terminate its owner")), 5000); timer.unref();
    })]);
  }
  const responses = output.trim().split("\n").map(line => JSON.parse(line));
  const response = responses.find(item => item.id === 1); assert.ok(!response.error); assert.equal(response.result.serverInfo.version, "0.2.38");
  if (shutdown === "pipelined inventory EOF") assert.ok(responses.find(item => item.id === 2)?.result.tools.length > 0);
  writer = await health();
  assert.equal(writer.capabilities.desktopStartupHandoff.active, true);
  assert.equal(writer.capabilities.businessHotReload.desktopWatchdogVersion, "0.2.27");
  assert.notEqual(writer.integrations.runtimeHostPid, inventory.pid, "Inventory must not own the persistent writer");
  const status = JSON.parse(await readFile(path.join(stateDir, "desktop-lifecycle-status.json"), "utf8"));
  assert.equal(status.stage, "desktop_lifetime_bridge_active"); assert.equal(status.writerPid, writer.integrations.pid);
  assert.equal(status.version, "0.2.23"); assert.equal(status.launchMode, "windows-desktop-parent");
  assert.equal(status.watchdogVersion, "0.2.27");
  if (shutdown === "EOF") {
    assert.equal(path.dirname(inventoryRoot), root);
    await rm(inventoryRoot, { recursive: true, force: true });
  }
  const supervisorChain = await processChain(status.supervisorPid);
  assert.equal(supervisorChain[1]?.pid, status.desktopPid, "The supervisor must be a direct child of the verified Desktop, outside the inventory ancestry");
  await new Promise(resolve => setTimeout(resolve, 10000));
  assert.doesNotThrow(() => process.kill(status.supervisorPid, 0), "The acknowledged supervisor must remain alive across watchdog checks");
  assert.equal((await health()).integrations.pid, writer.integrations.pid, "The same writer must remain after MCP EOF");
  assert.equal(nativeWrites, 0);
  const journal = await readFile(path.join(stateDir, "desktop-lifecycle-events.jsonl"), "utf8");
  assert.ok(journal.includes("desktop_lifetime_bridge_active")); assert.ok(!journal.includes(token)); assert.ok(!journal.includes(pipe));
});
