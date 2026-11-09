// Real Desktop-parent daemon and isolated loopback health. No production state,
// Feishu API or real native task write is used, even for failure acceptance.
import assert from "node:assert/strict";
import test from "node:test";
import http from "node:http";
import net from "node:net";
import { randomUUID } from "node:crypto";
import { mkdtemp, mkdir, readFile, writeFile, rm } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { desktopOwner, processChain, launchDesktopSupervisor } from "../desktop-lifecycle.mjs";

test("real script watchdog survives launcher return, repeats without native writes and stops when disabled", { timeout: 65000 }, async t => {
  const owner = desktopOwner(await processChain(process.pid));
  if (process.platform !== "win32" || !owner) { t.skip("Requires the Windows Desktop-owned launcher"); return; }
  const root = await mkdtemp(path.join(os.tmpdir(), "bridge-script-watchdog-live-"));
  const stateDir = path.join(root, ".codex", "state", "codexworker"); await mkdir(stateDir, { recursive: true });
  const tokenFile = path.join(root, "test.token"), token = "isolated-script-watchdog-credential-123456";
  await writeFile(tokenFile, token);
  let nativeRequests = 0, pid;
  const sockets = new Set(), pipe = "\\\\.\\pipe\\script-watchdog-isolated-" + randomUUID();
  const native = net.createServer(socket => { nativeRequests++; sockets.add(socket); socket.on("close", () => sockets.delete(socket)); socket.end(); });
  await new Promise(resolve => native.listen(pipe, resolve));
  const server = http.createServer((req, res) => {
    assert.equal(req.method, "GET"); assert.equal(req.url, "/health"); assert.equal(req.headers.authorization, "Bearer " + token);
    res.setHeader("content-type", "application/json"); res.end(JSON.stringify({ name: "isolated-watchdog", alive: true, ready: true,
      version: "0.2.27", machine: "codexworker", integrationMode: "desktop-native", operatingMode: "feishu-local",
      capabilities: { businessHotReload: { desktopWatchdogVersion: "0.2.27" } },
      integrations: { pid: process.pid, feishu: { enabled: true, inboundEnabled: true, connected: true,
        connection: { phase: "connected", lastFrameAt: new Date().toISOString(), pingIntervalMs: 90000, pongTimeoutMs: 15000,
          watchdog: { enabled: true, checks: 10, repairs: 0, intervalMs: 180000, lastCheckedAt: new Date().toISOString() } } } } }));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    if (pid) { try { process.kill(pid); } catch {} }
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => native.close(resolve)); await new Promise(resolve => server.close(resolve));
    assert.equal(path.dirname(root), os.tmpdir()); await rm(root, { recursive: true, force: true });
  });
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ tokenFile, port: server.address().port, operatingMode: "feishu-local", bridgeName: "isolated-watchdog" }));
  await writeFile(path.join(stateDir, "desktop-lifecycle-status.json"), JSON.stringify({ desktopPid: owner.pid, desktopStartedAt: owner.startedAt, supervisorPid: process.pid }));
  const settings = { enabled: true, maintenanceThreadId: "11111111-1111-4111-8111-111111111111", intervalMs: 15000 };
  const configPath = path.join(stateDir, "script-watchdog.local.json"); await writeFile(configPath, JSON.stringify(settings));
  const pluginRoot = fileURLToPath(new URL("..", import.meta.url));
  const env = { ...process.env, USERPROFILE: root, HOME: root, CODEX_APP_TOOLS_PIPE_PATH: pipe };
  const launched = await launchDesktopSupervisor({ pluginRoot, stateDir, owner, env, role: "watchdog" }); pid = launched.supervisorPid;
  const waitFor = async predicate => {
    const deadline = Date.now() + 35000;
    while (Date.now() < deadline) {
      try { const status = JSON.parse(await readFile(path.join(stateDir, "script-watchdog-status.json"), "utf8")); if (predicate(status)) return status; } catch {}
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    assert.fail("Isolated script watchdog did not reach the expected state");
  };
  let status = await waitFor(s => s.completed >= 1); assert.equal(status.lastResult.phase, "healthy");
  assert.equal((await processChain(pid))[1].pid, owner.pid);
  status = await waitFor(s => s.completed >= 2); assert.equal(status.pid, pid); assert.equal(nativeRequests, 0);
  const state = JSON.parse(await readFile(path.join(stateDir, "script-watchdog-state.json"), "utf8"));
  assert.ok(state.checks >= 2); assert.equal(state.lastAudit.status, "healthy"); assert.equal(state.incident, undefined);
  assert.ok(!JSON.stringify(status).includes(token)); assert.ok(!JSON.stringify(state).includes(pipe));
  await writeFile(configPath, JSON.stringify({ ...settings, enabled: false }));
  await waitFor(s => s.stage === "stopped"); assert.equal(nativeRequests, 0);
});
