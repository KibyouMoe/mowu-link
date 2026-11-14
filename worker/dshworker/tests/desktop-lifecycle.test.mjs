import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { cp, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { DesktopLifecycle, desktopOwner, desktopSupervisorReady, ensureDesktopSupervisor, launchDesktopSupervisor, processChain, resolveDesktopOwner, resolveLifecycleRoot } from "../desktop-lifecycle.mjs";

const owner = { pid: 700, name: "ChatGPT.exe", startedAt: "2026-10-03T02:42:05.0000000Z",
  executable: "C:/Program Files/WindowsApps/OpenAI.Codex_26.930.3748.0_x64__2p2nqsd0c76g0/app/ChatGPT.exe" };

test("independent script watchdog starts even when Bridge readiness never arrives", async t => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "watchdog-before-ready-"));
  t.after(() => rm(stateDir, { recursive: true, force: true }));
  await writeFile(path.join(stateDir, "desktop-config.local.json"), "{}");
  await writeFile(path.join(stateDir, "manager-binding.json"), "{}");
  let now = 0; const calls = [];
  await assert.rejects(ensureDesktopSupervisor({ pluginRoot: stateDir, stateDir, platform: "win32",
    env: { CODEX_APP_TOOLS_PIPE_PATH: "test-only-pipe" }, readChain: async () => [owner],
    clock: () => now, timeoutMs: 100, delay: async ms => { now += ms; }, ready: async () => false,
    ensureWatchdog: async options => { assert.equal(options.owner, owner); calls.push("watchdog"); },
    launchImpl: async () => { calls.push("bridge"); } }), /desktop_lifecycle_not_ready/);
  assert.deepEqual(calls, ["watchdog", "bridge"]);
});
function fixture() {
  let alive = true, ownerError = false, live = null, now = 0, nextPid = 100;
  const children = [], stopped = [], stages = [];
  const lifecycle = new DesktopLifecycle({ desktopAlive: async () => { if (ownerError) throw new Error("CIM timeout"); return alive; }, health: async () => live,
    clock: () => now, log: stage => stages.push(stage),
    launch: () => { const child = new EventEmitter(); child.pid = ++nextPid; children.push(child); return child; },
    stopChild: async child => { stopped.push(child.pid); child.emit("exit", 0); } });
  return { lifecycle, children, stopped, stages, setLive: value => { live = value; },
    setAlive: value => { alive = value; }, setOwnerError: value => { ownerError = value; }, setClock: value => { now = value; } };
}
const healthy = pid => ({ ready: true, integrationMode: "desktop-native", integrations: { pid } });

test("only the actual MSIX Desktop ancestor is accepted, never a same-named unrelated executable", () => {
  assert.deepEqual(desktopOwner([{ pid: 1, name: "node.exe" }, owner]), owner);
  assert.equal(desktopOwner([{ ...owner, executable: "C:/temp/ChatGPT.exe" }]), null);
  assert.equal(desktopOwner([{ ...owner, pid: -1 }]), null);
});
test("a healthy managed writer is observed without a competing adapter; its exit triggers recovery", async () => {
  const f = fixture(); f.setLive(healthy(900)); await f.lifecycle.tick();
  assert.equal(f.children.length, 0); assert.equal(f.lifecycle.closed, false);
  f.setLive(null); await f.lifecycle.tick(); assert.equal(f.children.length, 1);
  f.setLive(healthy(f.children[0].pid)); await f.lifecycle.tick();
  assert.equal(f.lifecycle.stage, "desktop_lifetime_bridge_active");
});
test("adapter process failure retries with bounded backoff and never calls a native send", async () => {
  const f = fixture(); await f.lifecycle.tick(); f.children[0].emit("exit", 1);
  await f.lifecycle.tick(); assert.equal(f.children.length, 1);
  f.setClock(1000); await f.lifecycle.tick(); assert.equal(f.children.length, 2);
  f.children[1].emit("exit", 1); f.setClock(2999); await f.lifecycle.tick(); assert.equal(f.children.length, 2);
  f.setClock(3000); await f.lifecycle.tick(); assert.equal(f.children.length, 3);
});
test("a managed-writer race closes only our proxy and continues watching the real writer", async () => {
  const f = fixture(); await f.lifecycle.tick(); f.setLive(healthy(900)); await f.lifecycle.tick();
  assert.deepEqual(f.stopped, [101]); assert.equal(f.lifecycle.closed, false);
  f.setLive(null); await f.lifecycle.tick(); assert.equal(f.children.length, 2);
});
test("Desktop exit closes only the owned adapter and cannot create a later replacement", async () => {
  const f = fixture(); await f.lifecycle.tick(); f.setAlive(false); await f.lifecycle.tick();
  assert.deepEqual(f.stopped, [101]); assert.equal(f.lifecycle.closed, true);
  await f.lifecycle.tick(); assert.equal(f.children.length, 1);
});
test("a hot-reloaded business PID belongs to its stable host, not a competing writer", async () => {
  const f = fixture(); await f.lifecycle.tick();
  f.setLive({ ...healthy(900), integrations: { pid: 900, runtimeHostPid: 101 } }); await f.lifecycle.tick();
  assert.deepEqual(f.stopped, []); assert.equal(f.lifecycle.child.pid, 101);
  f.setLive({ ...healthy(901), integrations: { pid: 901, runtimeHostPid: 101 } }); await f.lifecycle.tick();
  assert.deepEqual(f.stopped, []); assert.equal(f.children.length, 1);
});
test("a stalled startup is bounded, and a healthy writer is not restarted for disconnected Feishu", async () => {
  const f = fixture(); await f.lifecycle.tick(); f.setClock(45001); await f.lifecycle.tick();
  assert.deepEqual(f.stopped, [101]); f.setClock(46001); await f.lifecycle.tick();
  f.setLive({ ...healthy(102), integrations: { pid: 102, feishu: { connected: false } } });
  f.setClock(100000); await f.lifecycle.tick(); assert.deepEqual(f.stopped, [101]);
});
test("overlapping health checks cannot launch duplicate adapter processes", async () => {
  const f = fixture(); await Promise.all([f.lifecycle.tick(), f.lifecycle.tick(), f.lifecycle.tick()]);
  assert.equal(f.children.length, 1);
});

test("an old healthy process gets continuous unhealthy grace across short handoff gaps", async () => {
  const f = fixture(); await f.lifecycle.tick(); f.setLive(healthy(101)); await f.lifecycle.tick();
  f.setClock(3600000); f.setLive(null); await f.lifecycle.tick(); assert.deepEqual(f.stopped, []);
  f.setClock(3640000); f.setLive(healthy(101)); await f.lifecycle.tick();
  f.setClock(3700000); f.setLive(null); await f.lifecycle.tick(); assert.deepEqual(f.stopped, []);
  f.setClock(3745001); await f.lifecycle.tick(); assert.deepEqual(f.stopped, [101]);
});

test("lifetime startup uses trusted installed source even after the disposable cache entry disappears", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-stable-lifetime-"));
  try {
    const installed = await realpath(root).then(value => path.join(value, "installed"));
    await cp(new URL("..", import.meta.url), installed, { recursive: true });
    await writeFile(path.join(root, "runtime-source.local.json"), JSON.stringify({ sourceRoot: installed, autoReload: false }));
    const removedCache = path.join(root, "removed-cache");
    assert.equal(await resolveLifecycleRoot(removedCache, root), installed);
    let settings;
    await launchDesktopSupervisor({ pluginRoot: removedCache, stateDir: root, owner, platform: "win32", env: {}, exec: async (_exe, args, options) => {
      settings = options; assert.ok(args.includes(path.join(installed, "scripts", "launch-desktop-lifetime.ps1")));
      return { stdout: '{"supervisorPid":800,"launchMode":"windows-desktop-parent"}' };
    } });
    assert.equal(settings.cwd, installed);
    await writeFile(path.join(root, "runtime-source.local.json"), JSON.stringify({ sourceRoot: removedCache }));
    await assert.rejects(resolveLifecycleRoot(installed, root));
  } finally { await rm(root, { recursive: true, force: true }); }
});
test("bootstrap hands off the fresh pipe privately and prevents recursion or duplicate launch", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "codexworker-lifecycle-test-"));
  try {
    await writeFile(path.join(stateDir, "desktop-config.local.json"), "{}");
    await writeFile(path.join(stateDir, "manager-binding.json"), "{}");
    const launches = [];
    const options = { pluginRoot: "C:/plugins/codexworker", stateDir, platform: "win32", readChain: async () => [owner],
      env: { CODEX_APP_TOOLS_PIPE_PATH: "private-test-handle" }, ready: async () => launches.length > 0,
      launchImpl: async input => { launches.push(input); } };
    assert.equal(await ensureDesktopSupervisor(options), true);
    assert.equal(launches[0].env.CODEX_APP_TOOLS_PIPE_PATH, "private-test-handle");
    assert.equal(launches[0].owner.startedAt, owner.startedAt);
    assert.equal(await ensureDesktopSupervisor(options), true); assert.equal(launches.length, 1);
    assert.equal(await ensureDesktopSupervisor({ ...options, env: { ...options.env, RUDOLF_BRIDGE_LIFECYCLE_CHILD: "1" } }), false);
    assert.equal(await ensureDesktopSupervisor({ ...options, readChain: async () => [] }), false);
    assert.equal(launches.length, 1);
  } finally { await rm(stateDir, { recursive: true, force: true }); }
});
test("inventory handoff does not finish until a live Desktop supervisor and writer acknowledge readiness", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "codexworker-handoff-test-"));
  try {
    await writeFile(path.join(stateDir, "desktop-config.local.json"), "{}");
    await writeFile(path.join(stateDir, "manager-binding.json"), "{}");
    let now = 0, readyCalls = 0, finished = false;
    const result = ensureDesktopSupervisor({ pluginRoot: "C:/plugins/codexworker", stateDir, platform: "win32",
      readChain: async () => [owner], env: { CODEX_APP_TOOLS_PIPE_PATH: "private-test-handle" },
      clock: () => now, delay: async ms => { assert.equal(finished, false); now += ms; },
      ready: async () => ++readyCalls >= 3,
      launchImpl: async () => {} }).then(value => { finished = true; return value; });
    assert.equal(await result, true); assert.equal(readyCalls, 3); assert.equal(now, 200);
    await assert.rejects(ensureDesktopSupervisor({ pluginRoot: "C:/plugins/codexworker", stateDir, platform: "win32",
      readChain: async () => [owner], env: { CODEX_APP_TOOLS_PIPE_PATH: "private-test-handle" },
      clock: () => now, delay: async ms => { now += ms; }, timeoutMs: 400, ready: async () => false,
      launchImpl: async () => {} }), /desktop_lifecycle_not_ready/);
  } finally { await rm(stateDir, { recursive: true, force: true }); }
});
test("deployment auto-reload pause resumes only after the exact new startup acknowledgement", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "codexworker-startup-maintenance-"));
  try {
    await writeFile(path.join(stateDir, "desktop-config.local.json"), "{}");
    await writeFile(path.join(stateDir, "manager-binding.json"), "{}");
    const filename = path.join(stateDir, "runtime-source.local.json");
    const marker = { sourceRoot: "C:/owned/plugin", autoReload: false, resumeAutoReloadAfterBootstrap: "0.2.23", keep: "user-value" };
    await writeFile(filename, JSON.stringify(marker));
    let now = 0;
    const options = { pluginRoot: "C:/owned/plugin", stateDir, platform: "win32", readChain: async () => [owner],
      env: { CODEX_APP_TOOLS_PIPE_PATH: "private-test-handle" }, clock: () => now, delay: async ms => { now += ms; },
      launchImpl: async () => {}, timeoutMs: 200 };
    await assert.rejects(ensureDesktopSupervisor({ ...options, ready: async () => false }), /desktop_lifecycle_not_ready/);
    assert.deepEqual(JSON.parse(await readFile(filename, "utf8")), marker);
    assert.equal(await ensureDesktopSupervisor({ ...options, ready: async () => true }), true);
    assert.deepEqual(JSON.parse(await readFile(filename, "utf8")), { sourceRoot: marker.sourceRoot, autoReload: true, keep: marker.keep });
    const ordinaryPause = { sourceRoot: marker.sourceRoot, autoReload: false };
    await writeFile(filename, JSON.stringify(ordinaryPause));
    await ensureDesktopSupervisor({ ...options, ready: async () => true });
    assert.deepEqual(JSON.parse(await readFile(filename, "utf8")), ordinaryPause, "An unrelated user pause must remain paused");
  } finally { await rm(stateDir, { recursive: true, force: true }); }
});
test("supervisor readiness rejects stale/dead metadata and requires matching authenticated writer health", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "codexworker-ready-test-"));
  try {
    const tokenFile = path.join(stateDir, "test.token");
    await writeFile(tokenFile, "test-only-supervisor-token-not-secret-123456");
    await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ tokenFile }));
    const status = { desktopPid: owner.pid, desktopStartedAt: owner.startedAt, supervisorPid: 800, writerPid: 900,
      stage: "observing_managed_writer", version: "0.3.0", launchMode: "windows-desktop-parent" };
    const save = value => writeFile(path.join(stateDir, "desktop-lifecycle-status.json"), JSON.stringify(value));
    await save(status);
    let reads = 0;
    const options = { platform: "win32", alive: () => true, fetchImpl: async (_url, request) => {
      reads++; assert.match(request.headers.authorization, /^Bearer /);
      return { ok: true, json: async () => ({ ready: true, integrationMode: "desktop-native", name: "CodexWorker", integrations: { pid: 900 },
        capabilities: { desktopStartupHandoff: { active: true }, businessHotReload: {
          bootstrapVersion: "0.3.0", desktopLifetimeLaunchMode: "windows-desktop-parent" } } }) };
    } };
    assert.equal(await desktopSupervisorReady(stateDir, owner, options), true);
    assert.equal(await desktopSupervisorReady(stateDir, owner, { ...options, alive: () => false }), false);
    assert.equal(reads, 1);
    await save({ ...status, desktopStartedAt: "older Desktop" });
    assert.equal(await desktopSupervisorReady(stateDir, owner, options), false); assert.equal(reads, 1);
    await save({ ...status, writerPid: 901 });
    assert.equal(await desktopSupervisorReady(stateDir, owner, options), false);
    await save({ ...status, version: "0.2.14", launchMode: "windows-job-breakaway" });
    assert.equal(await desktopSupervisorReady(stateDir, owner, options), false, "An old breakaway supervisor cannot acknowledge the new startup guarantee");
    await save(status);
    assert.equal(await desktopSupervisorReady(stateDir, owner, { ...options, fetchImpl: async () => ({ ok: true,
      json: async () => ({ ready: true, integrationMode: "desktop-native", name: "CodexWorker", integrations: { pid: 900 } }) }) }), false,
      "A healthy ordinary MCP writer does not prove Desktop lifetime ownership");
  } finally { await rm(stateDir, { recursive: true, force: true }); }
});
test("Mac readiness requires the supervisor to remain a real Desktop descendant", async () => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "codexworker-mac-ancestry-"));
  try {
    const tokenFile = path.join(stateDir, "test.token");
    await writeFile(tokenFile, "isolated-desktop-supervisor-token-123456");
    await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ tokenFile }));
    const mac = { pid: 700, startedAt: "Sat Nov 14 12:14:32 2026", name: "ChatGPT",
      executable: "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT" };
    await writeFile(path.join(stateDir, "desktop-lifecycle-status.json"), JSON.stringify({
      desktopPid: mac.pid, desktopStartedAt: mac.startedAt, supervisorPid: 800, writerPid: 900,
      stage: "desktop_lifetime_bridge_active", version: "0.3.0", launchMode: "desktop-owned" }));
    let nativeProbes = 0;
    const options = { platform: "darwin", alive: () => true,
      readChain: async () => [{ pid: 800, name: "node", executable: "/runtime/node" }, mac],
      fetchImpl: async () => { nativeProbes++; return { ok: true, json: async () => ({
        ready: true, integrationMode: "desktop-native", name: "CodexWorker", integrations: { pid: 900 },
        capabilities: { desktopStartupHandoff: { active: true }, businessHotReload: {
          bootstrapVersion: "0.3.0", desktopLifetimeLaunchMode: "desktop-owned" } } }) }; } };
    assert.equal(await desktopSupervisorReady(stateDir, mac, options), true);
    assert.equal(await desktopSupervisorReady(stateDir, mac, { ...options, readChain: async () => [
      { pid: 800, name: "node", executable: "/runtime/node" }, { pid: 1, name: "launchd" }] }), false);
    assert.equal(nativeProbes, 1, "An orphan must be rejected before any native readiness traffic");
  } finally { await rm(stateDir, { recursive: true, force: true }); }
});

test("a retired MCP cannot launch another adapter after its owner probe finishes", async () => {
  let release, launches = 0;
  const lifecycle = new DesktopLifecycle({ desktopAlive: () => new Promise(resolve => { release = resolve; }),
    health: async () => null, launch: () => { launches++; }, stopChild: async () => {} });
  const tick = lifecycle.tick();
  await lifecycle.close(); release(true); await tick;
  assert.equal(launches, 0);
});

test("Windows lifetime launcher is hidden, uses the approved process-only policy, and keeps the native pipe out of arguments", async () => {
  let launch;
  const result = await launchDesktopSupervisor({ pluginRoot: "C:/plugins/codexworker", owner,
    platform: "win32", env: { CODEX_APP_TOOLS_PIPE_PATH: "private-test-handle" }, exec: async (exe, args, settings) => {
      launch = { exe, args, settings }; return { stdout: '{"supervisorPid":800,"launchMode":"windows-desktop-parent"}' };
    } });
  assert.equal(result.supervisorPid, 800); assert.equal(launch.settings.windowsHide, true);
  assert.equal(launch.settings.env.RUDOLF_BRIDGE_DESKTOP_STARTED_AT, owner.startedAt);
  assert.equal(launch.settings.env.CODEX_APP_TOOLS_PIPE_PATH, "private-test-handle");
  assert.ok(launch.args.includes("RemoteSigned")); assert.ok(!launch.args.includes("Bypass"));
  assert.ok(!JSON.stringify(launch.args).includes("private-test-handle"));
});
test("a denied Windows lifetime handoff is not retried through an alternate launcher and only exposes a safe code", async () => {
  let attempts = 0;
  await assert.rejects(launchDesktopSupervisor({ pluginRoot: "C:/plugins/codexworker", owner, platform: "win32", env: {}, exec: async () => {
    attempts++; const error = new Error("private raw exception"); error.stderr = "desktop_lifetime_launch_denied:5"; throw error;
  } }), /^Error: desktop_lifetime_launch_denied:5$/);
  assert.equal(attempts, 1);
  await assert.rejects(launchDesktopSupervisor({ pluginRoot: "C:/plugins/codexworker", owner, platform: "win32", env: {}, exec: async () => {
    throw new Error("private raw exception");
  } }), /^Error: desktop_lifetime_launch_unavailable$/);
});
test("transient Desktop owner lookup failure does not stop the healthy adapter or create a replacement", async () => {
  const f = fixture(); await f.lifecycle.tick(); f.setLive(healthy(101)); await f.lifecycle.tick();
  f.setOwnerError(true); await f.lifecycle.tick(); await f.lifecycle.tick();
  assert.equal(f.lifecycle.closed, false); assert.deepEqual(f.stopped, []); assert.equal(f.children.length, 1);
  assert.equal(f.lifecycle.stage, "desktop_owner_check_pending");
  f.setOwnerError(false); await f.lifecycle.tick(); assert.equal(f.lifecycle.stage, "desktop_lifetime_bridge_active");
  f.setAlive(false); await f.lifecycle.tick(); assert.equal(f.lifecycle.closed, true);
});
test("lifecycle evidence refreshes when a business writer changes under the same stage", async () => {
  const f = fixture(); f.setLive(healthy(900)); await f.lifecycle.tick();
  f.setLive(healthy(901)); await f.lifecycle.tick(); await f.lifecycle.tick();
  assert.equal(f.stages.length, 2); assert.equal(f.stages.at(-1).writerPid, 901);
});
test("process lookup rejects nonnumeric targets before launching a shell", async () => {
  let calls = 0;
  assert.deepEqual(await processChain("700; unsafe", { platform: "win32", exec: async () => { calls++; } }), []);
  assert.equal(calls, 0);
});
test("cold process-query failures retry within a bound without guessing a Desktop owner", async () => {
  let calls = 0;
  assert.equal((await resolveDesktopOwner(700, { delay: async () => {}, readChain: async () => {
    if (++calls === 1) throw new Error("cold CIM timeout"); return [owner];
  } })).pid, owner.pid);
  assert.equal(calls, 2); calls = 0;
  assert.equal(await resolveDesktopOwner(700, { delay: async () => {}, readChain: async () => { calls++; return []; } }), null);
  assert.equal(calls, 3);
});
