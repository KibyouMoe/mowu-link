import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { EventEmitter, once } from "node:events";
import { mkdir, mkdtemp, readFile, readdir, realpath, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { DesktopLifecycle, desktopOwner, ensureDesktopSupervisor, launchDesktopSupervisor,
  processChain, readDesktopSettings, stopOwnedChild } from "../desktop-lifecycle.mjs";

const pluginRoot = fileURLToPath(new URL("..", import.meta.url));
const macStartedAt = "Fri Nov 13 10:20:30 2026";
const owners = {
  darwin: { pid: 700, name: "Codex", executable: "/Applications/Codex.app/Contents/MacOS/Codex", startedAt: macStartedAt },
  win32: { pid: 700, name: "ChatGPT.exe", startedAt: "2026-11-13T02:20:30.0000000Z",
    executable: "C:\\Program Files\\WindowsApps\\OpenAI.Codex_26.1113.1.0_x64__2p2nqsd0c76g0\\app\\ChatGPT.exe" },
};

async function temporary(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "desktop-only-test-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}
const save = (root, name, value) => writeFile(path.join(root, name), JSON.stringify(value));
const rejectsWith = message => error => error instanceof Error && error.message === message;

// These tests select each implementation explicitly; mocked win32 execution on
// macOS is not native Windows acceptance. The native probes below are separate.
for (const platform of ["darwin", "win32"]) {
  test(platform + ": absent/default settings enable startup; explicit false disables it", async t => {
    const stateDir = await temporary(t);
    assert.deepEqual(await readDesktopSettings(stateDir), { autoStart: true });
    for (const config of [{}, { autoStart: true }, { autoStart: false }]) {
      await save(stateDir, "desktop-config.local.json", config);
      assert.deepEqual(await readDesktopSettings(stateDir), { autoStart: config.autoStart !== false });
    }
  });

  for (const childFlag of [undefined, "1"]) {
    test(platform + ": autoStart false skips owner/ready/launch/watchdog despite stale CLI and child=" + childFlag, async t => {
      const stateDir = await temporary(t), calls = [];
      await save(stateDir, "desktop-config.local.json", { autoStart: false });
      await save(stateDir, "manager-binding.json", { managerThreadId: "old-binding" });
      await save(stateDir, "desktop-cli.local.json", { enabled: true, command: process.execPath, args: ["cli.mjs"] });
      await save(stateDir, "script-watchdog.local.json", { enabled: true, maintenanceThreadId: "00000000-0000-4000-8000-000000000000" });
      const unexpected = name => async () => { calls.push(name); throw new Error("unexpected " + name); };
      assert.equal(await ensureDesktopSupervisor({ pluginRoot, stateDir, platform,
        env: { CODEX_APP_TOOLS_PIPE_PATH: "private-test-pipe", RUDOLF_BRIDGE_LIFECYCLE_CHILD: childFlag,
          RUDOLF_BRIDGE_LIFECYCLE_READY: "1" },
        readChain: unexpected("owner"), ready: unexpected("ready"), launchImpl: unexpected("launch"),
        ensureWatchdog: unexpected("watchdog"), delay: unexpected("delay") }), false);
      assert.deepEqual(calls, []);
    });
  }

  test(platform + ": invalid autoStart fails closed before any owner, readiness or launcher call", async t => {
    const stateDir = await temporary(t), calls = [];
    await save(stateDir, "manager-binding.json", { managerThreadId: "test-only-binding" });
    const unexpected = name => async () => { calls.push(name); throw new Error("unexpected " + name); };
    const options = { pluginRoot, stateDir, platform,
      env: { CODEX_APP_TOOLS_PIPE_PATH: "private-test-pipe" },
      readChain: unexpected("owner"), ready: unexpected("ready"), launchImpl: unexpected("launch"),
      ensureWatchdog: unexpected("watchdog"), delay: unexpected("delay") };
    const invalid = [null, "false", "true", 0, 1, [], {}, { value: false }];
    for (const autoStart of invalid) await t.test("autoStart=" + JSON.stringify(autoStart), async () => {
      calls.length = 0;
      await save(stateDir, "desktop-config.local.json", { autoStart });
      const results = await Promise.allSettled([readDesktopSettings(stateDir), ensureDesktopSupervisor(options)]);
      assert.deepEqual([...calls], [], "invalid autoStart must fail before owner/readiness/launcher work");
      for (const result of results) {
        assert.equal(result.status, "rejected", "invalid autoStart must never default to enabled");
        assert.equal(result.reason.message, "desktop_autostart_invalid");
      }
    });
    for (const config of [null, [], false, 0, "false"]) await t.test("config=" + JSON.stringify(config), async () => {
      await save(stateDir, "desktop-config.local.json", config);
      await assert.rejects(ensureDesktopSupervisor(options), rejectsWith("desktop_autostart_invalid"));
      assert.deepEqual(calls, []);
    });
    await writeFile(path.join(stateDir, "desktop-config.local.json"), '{"autoStart":');
    await assert.rejects(ensureDesktopSupervisor(options), rejectsWith("desktop_settings_invalid"));
    assert.deepEqual(calls, []);
  });

  test(platform + ": enabled plugin handoff launches once and requires a verified Desktop owner", async t => {
    const stateDir = await temporary(t), calls = [];
    await save(stateDir, "desktop-config.local.json", {});
    await save(stateDir, "manager-binding.json", {});
    let launched = false;
    const options = { pluginRoot, stateDir, platform, env: { CODEX_APP_TOOLS_PIPE_PATH: "private-test-pipe" },
      readChain: async () => { calls.push("owner"); return [owners[platform]]; },
      ready: async (_state, owner, selected) => { assert.equal(owner, owners[platform]); assert.equal(selected.platform, platform); calls.push("ready"); return launched; },
      launchImpl: async selected => { assert.equal(selected.platform, platform); assert.equal(selected.owner, owners[platform]); calls.push("launch"); launched = true; },
      ensureWatchdog: async () => { calls.push("watchdog"); }, delay: async () => { calls.push("delay"); } };
    assert.equal(await ensureDesktopSupervisor(options), true);
    assert.deepEqual(calls, platform === "win32" ? ["owner", "watchdog", "ready", "launch", "ready"] : ["owner", "ready", "launch", "ready"]);
    calls.length = 0;
    assert.equal(await ensureDesktopSupervisor(options), true);
    assert.ok(!calls.includes("launch"));
    for (const env of [{}, { ...options.env, RUDOLF_BRIDGE_LIFECYCLE_CHILD: "1" }]) {
      calls.length = 0;
      assert.equal(await ensureDesktopSupervisor({ ...options, env }), false);
      assert.deepEqual(calls, []);
    }
    calls.length = 0;
    assert.equal(await ensureDesktopSupervisor({ ...options, readChain: async () => { calls.push("unrelated"); return [{ ...owners[platform], name: "node", executable: "/usr/bin/node" }]; } }), false);
    assert.deepEqual(calls, ["unrelated", "delay", "unrelated", "delay", "unrelated"]);
  });
}

test("DesktopLifecycle disables only its owned child, remains dormant and re-enables a new child", async () => {
  let enabled = false, now = 0, healthCalls = 0;
  const children = [], stopped = [], stages = [];
  const lifecycle = new DesktopLifecycle({ desktopAlive: async () => true, enabled: async () => enabled,
    health: async () => { healthCalls++; return null; }, clock: () => now,
    launch: () => { const child = new EventEmitter(); child.pid = 800 + children.length; children.push(child); return child; },
    stopChild: async child => { stopped.push(child); child.emit("exit", 0); }, log: event => stages.push(event.stage) });
  try {
    await lifecycle.tick();
    assert.equal(children.length, 0); assert.equal(healthCalls, 0);
    enabled = true; await lifecycle.tick();
    const first = children[0]; assert.equal(lifecycle.child, first);
    enabled = false; await lifecycle.tick();
    assert.deepEqual(stopped, [first]); assert.equal(lifecycle.child, null); assert.equal(lifecycle.closed, false);
    for (let tick = 0; tick < 3; tick++) { now += 120000; await lifecycle.tick(); }
    assert.equal(children.length, 1); assert.deepEqual(stopped, [first]); assert.equal(healthCalls, 1);
    assert.equal(lifecycle.stage, "desktop_autostart_disabled");
    enabled = true; await lifecycle.tick();
    const second = children[1]; assert.ok(second); assert.notEqual(second, first); assert.notEqual(second.pid, first.pid);
    first.emit("exit", 1); first.emit("error", new Error("late retired child failure"));
    assert.equal(lifecycle.child, second); assert.ok(!stages.includes("adapter_retry_pending"));
    await lifecycle.close(); await lifecycle.close();
    assert.deepEqual(stopped, [first, second]);
  } finally { await lifecycle.close(); }
});

test("DesktopLifecycle never stops an external healthy writer when disabled", async () => {
  let enabled = true, healthCalls = 0;
  const stopped = [], launched = [];
  const lifecycle = new DesktopLifecycle({ desktopAlive: async () => true, enabled: async () => enabled,
    health: async () => { healthCalls++; return { ready: true, integrationMode: "desktop-native", integrations: { pid: 999 } }; },
    launch: () => { launched.push(true); throw new Error("external writer must not be replaced"); },
    stopChild: async child => { stopped.push(child); } });
  try {
    await lifecycle.tick(); assert.equal(lifecycle.stage, "observing_managed_writer");
    enabled = false; await lifecycle.tick(); await lifecycle.tick();
    assert.equal(healthCalls, 1); assert.deepEqual(stopped, []); assert.deepEqual(launched, []);
    enabled = true; await lifecycle.tick();
    assert.equal(lifecycle.stage, "observing_managed_writer"); assert.equal(healthCalls, 2);
  } finally { await lifecycle.close(); }
  assert.deepEqual(stopped, []);
});

test("Darwin desktopOwner accepts Codex/ChatGPT app executables and rejects lookalikes", () => {
  for (const app of ["Codex", "ChatGPT"]) {
    const owner = { ...owners.darwin, name: app, executable: "/Users/Shared/Work Apps/" + app + ".app/Contents/MacOS/" + app };
    assert.equal(desktopOwner([{ pid: 701, name: "node" }, owner]), owner);
  }
  for (const executable of ["/usr/local/bin/Codex", "/Applications/Other.app/Contents/MacOS/Codex",
    "/Applications/Codex.app/Contents/MacOS/node", "/Applications/Codex.app/Contents/MacOS/Codex --type=renderer",
    "/Applications/ChatGPT.app/Contents/Frameworks/ChatGPT Helper.app/Contents/MacOS/ChatGPT Helper"]) {
    assert.equal(desktopOwner([{ ...owners.darwin, executable }]), null, executable);
  }
  for (const override of [{ pid: 0 }, { pid: -1 }, { pid: "700" }, { startedAt: null }]) {
    assert.equal(desktopOwner([{ ...owners.darwin, ...override }]), null);
  }
  const childApp = { ...owners.darwin, pid: 701 };
  assert.equal(desktopOwner([childApp, owners.darwin]), owners.darwin);
});

test("Darwin processChain parses ps paths with spaces and finds the expected app ancestor", async () => {
  const calls = [], executable = "/Users/Shared/Work Apps/ChatGPT.app/Contents/MacOS/ChatGPT";
  const output = { 702: "702 700 " + macStartedAt + " /test/node\n", 700: "700 1 " + macStartedAt + " " + executable + "\n", 1: "1 0 " + macStartedAt + " /sbin/launchd\n" };
  const chain = await processChain(702, { platform: "darwin", exec: async (command, args, options) => {
    assert.equal(command, "/bin/ps"); assert.deepEqual(args.slice(2), ["-o", "pid=,ppid=,lstart=,comm="]);
    assert.ok(options.timeout > 0 && options.timeout <= 8000); calls.push(Number(args[1])); return { stdout: output[args[1]] };
  } });
  assert.deepEqual(calls, [702, 700, 1]);
  assert.deepEqual(chain[1], { pid: 700, startedAt: macStartedAt, executable, name: "ChatGPT" });
  assert.equal(desktopOwner(chain), chain[1]);
  assert.equal(desktopOwner(await processChain(702, { platform: "darwin", exec: async () => ({ stdout: "702 0 " + macStartedAt + " /usr/bin/node\n" }) })), null);
  let cycles = 0;
  assert.equal((await processChain(702, { platform: "darwin", exec: async () => { cycles++; return { stdout: "702 702 " + macStartedAt + " /test/node\n" }; } })).length, 1);
  assert.equal(cycles, 1);
});

test("Windows desktopOwner accepts the native Codex WindowsApps executable, rejects unrelated exe owners", () => {
  for (const executable of [owners.win32.executable, "D:/Apps/OpenAI.Codex_26.1113.1.0_arm64__test/app/ChatGPT.exe"]) {
    const owner = { ...owners.win32, executable, name: "CHATGPT.EXE" };
    assert.equal(desktopOwner([{ pid: 702, name: "node.exe" }, owner]), owner);
  }
  for (const override of [{ executable: "C:/Other/ChatGPT.exe" },
    { executable: "C:/Apps/OpenAI.Unrelated_1/app/ChatGPT.exe" },
    { executable: owners.win32.executable + ".bak" }, { name: "node.exe" }, { pid: 0 }, { startedAt: undefined }]) {
    assert.equal(desktopOwner([{ ...owners.win32, ...override }]), null);
  }
});

test("Windows processChain uses bounded CIM inspection and passes native executable identity to desktopOwner", async () => {
  const expected = [{ pid: 702, name: "node.exe", executable: "C:\\Node\\node.exe", startedAt: owners.win32.startedAt }, owners.win32];
  let queries = 0;
  const chain = await processChain(702, { platform: "win32", exec: async (command, args, options) => {
    queries++; assert.match(command, /[\\/]WindowsPowerShell[\\/]v1\.0[\\/]powershell\.exe$/i);
    assert.deepEqual(args.slice(0, -1), ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]);
    assert.match(args.at(-1), /\$bridgeInspectPid=702;/); assert.match(args.at(-1), /Get-CimInstance Win32_Process/);
    assert.match(args.at(-1), /\$bridgeDepth -lt 16/); assert.equal(options.windowsHide, true);
    assert.ok(options.timeout > 0 && options.timeout <= 8000); return { stdout: JSON.stringify(expected) };
  } });
  assert.equal(queries, 1); assert.deepEqual(chain, expected); assert.equal(desktopOwner(chain), chain[1]);
  assert.deepEqual(await processChain(702, { platform: "win32", exec: async () => ({ stdout: "[]" }) }), []);
});

for (const platform of ["darwin", "win32"]) {
  test(platform + ": processChain rejects invalid PIDs without executing a process", async () => {
    let executions = 0;
    for (const pid of [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, "700; unsafe", null]) {
      assert.deepEqual(await processChain(pid, { platform, exec: async () => { executions++; throw new Error("must not execute"); } }), []);
    }
    assert.equal(executions, 0);
  });
  test(platform + ": native processChain inspects this test's own Node process", { skip: process.platform !== platform, timeout: 30000 }, async () => {
    let chain;
    for (let attempt = 0; attempt < 3; attempt++) {
      try { chain = await processChain(process.pid); break; }
      catch (error) { if (!error.killed || attempt === 2) throw error; }
    }
    assert.equal(chain[0]?.pid, process.pid); assert.equal(typeof chain[0].startedAt, "string");
    assert.ok(chain[0].startedAt.length > 0);
    assert.equal(await realpath(chain[0].executable), await realpath(process.execPath));
    assert.ok(chain.length <= 16);
    const owner = desktopOwner(chain);
    if (owner) assert.ok(chain.includes(owner));
  });
}

for (const role of ["bridge", "watchdog"]) {
  test("Darwin " + role + " launcher uses injected spawn and keeps the private pipe only in env", async () => {
    const env = Object.freeze({ CODEX_APP_TOOLS_PIPE_PATH: "private-darwin-test-pipe", TEST_ONLY: "retained" });
    let invocation, unrefs = 0, execs = 0;
    const result = await launchDesktopSupervisor({ pluginRoot, platform: "darwin", owner: owners.darwin, env, role,
      exec: async () => { execs++; throw new Error("no alternate launcher"); },
      spawnImpl: (command, args, options) => {
        invocation = { command, args, options }; const child = new EventEmitter(); child.pid = 800;
        child.unref = () => { unrefs++; }; queueMicrotask(() => child.emit("spawn")); return child;
      } });
    assert.equal(invocation.command, process.execPath);
    assert.deepEqual(invocation.args, [path.join(pluginRoot, "scripts", role === "bridge" ? "run-desktop-bridge.mjs" : "run-script-watchdog.mjs"), "--desktop-pid", "700"]);
    assert.equal(invocation.options.cwd, pluginRoot); assert.equal(invocation.options.detached, true); assert.equal(invocation.options.stdio, "ignore");
    assert.deepEqual(invocation.options.env, { ...env, RUDOLF_BRIDGE_DESKTOP_STARTED_AT: owners.darwin.startedAt, RUDOLF_BRIDGE_LIFETIME_LAUNCH: "desktop-owned" });
    assert.ok(!JSON.stringify(invocation.args).includes(env.CODEX_APP_TOOLS_PIPE_PATH));
    assert.deepEqual(result, { supervisorPid: 800, launchMode: "desktop-owned" });
    assert.equal(unrefs, 1); assert.equal(execs, 0);
  });
}

for (const failure of ["synchronous", "error-event"]) {
  test("Darwin " + failure + " spawn failure rejects without an alternate launcher", async () => {
    const denied = new Error("test spawn denied"); let spawns = 0, execs = 0, unrefs = 0;
    await assert.rejects(launchDesktopSupervisor({ pluginRoot, platform: "darwin", owner: owners.darwin,
      env: { CODEX_APP_TOOLS_PIPE_PATH: "private-test-pipe" },
      exec: async () => { execs++; throw new Error("no alternate exec"); }, spawnImpl: () => {
        spawns++; if (failure === "synchronous") throw denied;
        const child = new EventEmitter(); child.unref = () => { unrefs++; }; queueMicrotask(() => child.emit("error", denied)); return child;
      } }), error => error === denied);
    assert.equal(spawns, 1); assert.equal(execs, 0); assert.equal(unrefs, 0);
  });
}

test("Windows launcher keeps the pipe in env and never falls back to spawn after handoff denial", async () => {
  let execs = 0, spawns = 0;
  const env = Object.freeze({ SystemRoot: "C:/Windows", CODEX_APP_TOOLS_PIPE_PATH: "private-windows-test-pipe" });
  const options = { pluginRoot, platform: "win32", owner: owners.win32, env,
    spawnImpl: () => { spawns++; throw new Error("no alternate spawn"); } };
  const result = await launchDesktopSupervisor({ ...options, exec: async (command, args, settings) => {
    execs++; assert.match(command, /[\\/]powershell\.exe$/i);
    assert.deepEqual(args.slice(0, 7), ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned", "-File", path.join(pluginRoot, "scripts", "launch-desktop-lifetime.ps1")]);
    assert.deepEqual(args.slice(7), [process.execPath, "700", "bridge"]);
    assert.equal(settings.windowsHide, true); assert.equal(settings.env.CODEX_APP_TOOLS_PIPE_PATH, env.CODEX_APP_TOOLS_PIPE_PATH);
    assert.equal(settings.env.RUDOLF_BRIDGE_DESKTOP_STARTED_AT, owners.win32.startedAt);
    assert.ok(!JSON.stringify(args).includes(env.CODEX_APP_TOOLS_PIPE_PATH));
    return { stdout: '{"supervisorPid":800,"launchMode":"windows-desktop-parent"}' };
  } });
  assert.deepEqual(result, { supervisorPid: 800, launchMode: "windows-desktop-parent" });
  await assert.rejects(launchDesktopSupervisor({ ...options, exec: async () => {
    execs++; const error = new Error("private failure"); error.stderr = "desktop_lifetime_launch_denied:5"; throw error;
  } }), rejectsWith("desktop_lifetime_launch_denied:5"));
  assert.equal(execs, 2); assert.equal(spawns, 0);
});

// Run the REAL package and its normal disposable business child. Only that
// exact fork is allowed; every network/listener or service/CLI launch is recorded
// and blocked, so a regression cannot reach Desktop or production credentials.
const auditPreload = String.raw`
import childProcess from "node:child_process";
import { appendFileSync } from "node:fs";
import net from "node:net";
import { syncBuiltinESMExports } from "node:module";
import path from "node:path";
const record = (event, detail = {}) => appendFileSync(process.env.DESKTOP_ONLY_TEST_AUDIT,
  JSON.stringify({ event, pid: process.pid, ...detail }) + "\n");
record("boot", { entry: process.argv[1], ready: process.env.RUDOLF_BRIDGE_LIFECYCLE_READY });
const deny = operation => () => { record("forbidden", { operation }); throw new Error("desktop_only_test_blocked_" + operation); };
const realFork = childProcess.fork, owned = new Set();
childProcess.fork = (entry, args, options) => {
  if (path.resolve(entry) !== path.join(process.env.DESKTOP_ONLY_TEST_PLUGIN_ROOT, "scripts", "business-worker.mjs") ||
      path.resolve(process.argv[1]) !== path.join(process.env.DESKTOP_ONLY_TEST_PLUGIN_ROOT, "server.mjs") || owned.size) {
    return deny("unexpected-fork")();
  }
  const child = realFork(entry, args, options); owned.add(child);
  record("business-fork", { childPid: child.pid, ready: options.env.RUDOLF_BRIDGE_LIFECYCLE_READY });
  child.once("exit", (code, signal) => { owned.delete(child); record("business-exit", { childPid: child.pid, code, signal }); });
  return child;
};
for (const name of ["spawn", "spawnSync", "exec", "execSync", "execFile", "execFileSync"]) childProcess[name] = deny(name);
net.Socket.prototype.connect = deny("socket-connect");
net.Server.prototype.listen = deny("server-listen");
globalThis.fetch = deny("fetch");
syncBuiltinESMExports();
process.on("exit", code => {
  record("exit", { code });
  for (const child of owned) if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
});
// Bounds even a broken generation's shutdown, including an orphan after a
// killed MCP host; no machine-wide process lookup or PID-file kill is used.
setTimeout(() => { record("deadline"); process.exit(97); }, process.platform === "win32" ? 60000 : 12000).unref();
`;

async function bounded(promise, milliseconds, label) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(label + " timed out")), milliseconds);
  })]); } finally { clearTimeout(timer); }
}

for (const childFlag of [undefined, "1"]) {
  test("real MCP cold boot: autoStart false exposes plugin tools without native/HTTP/service work, stale CLI, child=" + childFlag,
    { timeout: 20000 }, async t => {
      const root = await mkdtemp(path.join(os.tmpdir(), "desktop-only-cold-"));
      let child, closed;
      t.after(async () => {
        try {
          if (child && child.exitCode === null && child.signalCode === null) {
            child.stdin.end(); child.kill("SIGTERM");
            try { await bounded(closed, 1500, "owned MCP shutdown"); }
            catch { child.kill("SIGKILL"); await bounded(closed, 1500, "owned MCP termination"); }
          }
        } finally { await rm(root, { recursive: true, force: true }); }
      });
      const stateDir = path.join(root, "state"), audit = path.join(root, "audit.jsonl"), preload = path.join(root, "audit.mjs");
      await mkdir(stateDir); await writeFile(preload, auditPreload);
      const tokenFile = path.join(root, "test.token"); await writeFile(tokenFile, "test-only-token-never-a-real-credential");
      const binding = { formatVersion: 1, integrationMode: "desktop-native", managerThreadId: "00000000-0000-4000-8000-000000000001", managerTurnId: null };
      await save(stateDir, "desktop-config.local.json", { autoStart: false, host: "127.0.0.1", port: 0, tokenFile,
        bridgeName: "test-only", machine: "test-only", router: { url: "https://router.invalid", token: "test-only" } });
      await save(stateDir, "manager-binding.json", binding);
      await save(stateDir, "desktop-cli.local.json", { enabled: true, command: process.execPath, args: ["cli.mjs"],
        configFile: path.join(root, "old-cli-config.json"), identityFile: path.join(root, "old-identity.json") });
      await save(stateDir, "script-watchdog.local.json", { enabled: true, maintenanceThreadId: binding.managerThreadId });
      const protectedFiles = new Map();
      for (const name of await readdir(stateDir)) protectedFiles.set(name, await readFile(path.join(stateDir, name), "utf8"));
      // Deliberately do not spread process.env: real tokens, pipe handles,
      // NODE_OPTIONS, home settings and service endpoints cannot be inherited.
      const env = { HOME: root, USERPROFILE: root, CODEX_HOME: path.join(root, ".codex"),
        APPDATA: root, LOCALAPPDATA: root, XDG_CACHE_HOME: root, XDG_CONFIG_HOME: root,
        PATH: path.dirname(process.execPath), CODEXWORKER_STATE_DIR: stateDir,
        CODEXWORKER_NATIVE_TRANSPORT: "desktop-pipe", CODEXWORKER_FEISHU_DRY_RUN: "1",
        CODEX_APP_TOOLS_PIPE_PATH: process.platform === "win32" ? "\\\\.\\pipe\\desktop-only-test-" + path.basename(root) : path.join(root, "test-native.sock"),
        RUDOLF_BRIDGE_LIFECYCLE_READY: "1", DESKTOP_ONLY_TEST_AUDIT: audit, DESKTOP_ONLY_TEST_PLUGIN_ROOT: path.resolve(pluginRoot) };
      for (const name of ["SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP"]) if (process.env[name]) env[name] = process.env[name];
      if (childFlag) env.RUDOLF_BRIDGE_LIFECYCLE_CHILD = childFlag;
      child = spawn(process.execPath, ["--import", pathToFileURL(preload).href, path.join(pluginRoot, "server.mjs")], {
        cwd: pluginRoot, env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
      let stdout = "", stderr = "";
      child.stdout.setEncoding("utf8"); child.stdout.on("data", chunk => { stdout += chunk; });
      child.stderr.setEncoding("utf8"); child.stderr.on("data", chunk => { stderr += chunk; });
      child.stdin.on("error", () => {});
      closed = new Promise(resolve => {
        child.once("error", error => resolve({ error })); child.once("close", (code, signal) => resolve({ code, signal }));
      });
      // One inventory chunk also exercises the initialize/tools-list race and
      // verifies that caller-supplied readiness cannot override local settings.
      const requests = [
        { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {},
          clientInfo: { name: "desktop-only-test", version: "1" }, __bridgeLifecycleReady: true, __bridgeAutoStart: true } },
        { jsonrpc: "2.0", method: "notifications/initialized" },
        { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
        { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "bridge_binding_status", arguments: {} } },
      ];
      child.stdin.end(requests.map(request => JSON.stringify(request)).join("\n") + "\n");
      const result = await bounded(closed, process.platform === "win32" ? 65000 : 15000, "real package MCP cold boot");
      assert.deepEqual(result, { code: 0, signal: null }, stderr);
      const responses = stdout.trim().split("\n").filter(Boolean).map(line => JSON.parse(line));
      const reply = id => { const found = responses.filter(message => message.id === id); assert.equal(found.length, 1, stdout); assert.equal(found[0].error, undefined, stdout); return found[0].result; };
      assert.equal(reply(1).protocolVersion, "2025-06-18"); assert.equal(reply(1).capabilities.tools.listChanged, true);
      const names = reply(2).tools.map(tool => tool.name);
      for (const name of ["bridge_probe", "bridge_bind_manager", "bridge_binding_status", "bridge_message", "bridge_reload_business_modules"]) assert.ok(names.includes(name), name);
      assert.equal(new Set(names).size, names.length);
      assert.deepEqual(reply(3).structuredContent.binding, binding); assert.equal(reply(3).isError, false);
      assert.ok(!stdout.includes(env.CODEX_APP_TOOLS_PIPE_PATH)); assert.ok(!stdout.includes("test-only-token-never-a-real-credential"));
      const events = (await readFile(audit, "utf8")).trim().split("\n").map(line => JSON.parse(line));
      assert.deepEqual(events.filter(event => ["forbidden", "deadline"].includes(event.event)), []);
      const forks = events.filter(event => event.event === "business-fork");
      assert.equal(forks.length, 1, "only the disposable plugin business child may start");
      assert.equal(forks[0].ready, "0", "the child flag and stale readiness cannot bypass autoStart false");
      const businessBoot = events.find(event => event.event === "boot" && event.pid === forks[0].childPid);
      assert.ok(businessBoot, "the actual child package booted with the same IO guard"); assert.equal(businessBoot.ready, "0");
      assert.ok(events.some(event => event.event === "business-exit" && event.childPid === forks[0].childPid && event.code === 0 && event.signal === null), "the owned business child exited normally");
      for (const [name, contents] of protectedFiles) assert.equal(await readFile(path.join(stateDir, name), "utf8"), contents, name + " must be preserved");
      assert.deepEqual((await readdir(stateDir)).filter(name => !protectedFiles.has(name)), ["business-generations"]);
      assert.deepEqual(await readdir(path.join(stateDir, "business-generations")), [], "owned generation snapshots are retired");
    });
}


test("darwin: ps exit 1 means Desktop exited; inspection errors remain unknown", async () => {
  assert.deepEqual(await processChain(123, { platform: "darwin", exec: async () => {
    throw Object.assign(new Error("process not found"), { code: 1, stdout: "" });
  } }), []);
  await assert.rejects(processChain(123, { platform: "darwin", exec: async () => {
    throw Object.assign(new Error("inspection denied"), { code: "EACCES" });
  } }), /inspection denied/);
  await assert.rejects(processChain(123, { platform: "darwin", exec: async () => {
    throw Object.assign(new Error("ps failed"), { code: 1, stdout: "", stderr: "Permission denied" });
  } }), /ps failed/);
});

test("native owned-child shutdown ends a stalled adapter without waiting for its task", { timeout: 10000 }, async t => {
  const child = spawn(process.execPath, ["-e", 'process.stdin.resume();process.stdin.on("end",()=>{});process.on("SIGTERM",()=>{});setInterval(()=>{},1000);process.stdout.write("ready\\n");'],
    { stdio: ["pipe", "pipe", "ignore"], windowsHide: true });
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) { const done = once(child, "exit"); child.kill("SIGKILL"); await done; }
  });
  await once(child.stdout, "data");
  await stopOwnedChild(child, { graceMs: 50 });
  assert.ok(child.exitCode !== null || child.signalCode !== null);
  await stopOwnedChild(child, { graceMs: 50 }); // Already exited is harmless.
});
