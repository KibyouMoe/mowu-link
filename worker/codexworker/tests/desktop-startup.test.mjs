import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, realpath, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { ensureDesktopStartup, reopenDesktop } from "../interface/desktop-startup.mjs";
import { loadNativeConnection } from "../interface/desktop-connection/native-entry.mjs";
import { prepareDesktopConnection } from "../interface/desktop-connection/prepare.mjs";

const boot = "94856DEE-EF12-4D33-BB03-B8665EC7C02D";
async function fixture(t) {
  const stateDir = await realpath(await mkdtemp(path.join(os.tmpdir(), "native-startup-空格-")));
  t.after(() => rm(stateDir, { recursive: true, force: true }));
  const owner = { pid: 700, startedAt: "Sat Nov 14 17:31:31 2026",
    executable: "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT" };
  const engine = { pid: 699, startedAt: "original engine lifetime", executable: "/native/codex" };
  const creator = { pid: 698, startedAt: "original MCP lifetime", executable: "/native/node" };
  let engineExited = false;
  const readChain = async pid => pid === 699 ? engineExited ? [] : [engine, owner]
    : pid === 700 ? [owner] : [creator, engine, owner];
  const env = { CODEXWORKER_NATIVE_BOOTSTRAP: "1", CODEX_APP_TOOLS_PIPE_PATH: "test-native-pipe" };
  const file = path.join(stateDir, "native-startup", boot + "-v1.json"), installs = [], reopens = [];
  const launcher = path.join(stateDir, "desktop-connection", "a".repeat(64), "codex");
  await writeFile(path.join(stateDir, "manager-binding.json"), JSON.stringify({ managerThreadId: "original-chat" }));
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ autoStart: true }));
  await writeFile(path.join(stateDir, "desktop-native-connection.json"), JSON.stringify({ version: 1, realCli: "/original/native/codex" }), { mode: 0o600 });
  const options = { stateDir, env, platform: "darwin", readChain, bootSession: async () => boot,
    install: async value => { installs.push(value); return { launcher }; }, reopen: async value => { reopens.push(value); } };
  return { stateDir, owner, env, file, installs, reopens, launcher, options,
    engineExited: () => { engineExited = true; },
    run: extra => ensureDesktopStartup({ ...options, ...extra }),
    record: async () => JSON.parse(await readFile(file, "utf8")) };
}

test("the real plugin cold-start contract enables native setup without a separate launcher", async () => {
  const manifest = JSON.parse(await readFile(new URL("../.mcp.json", import.meta.url), "utf8"));
  assert.equal(manifest.mcpServers.codexworker.env.CODEXWORKER_NATIVE_BOOTSTRAP, "1");
  assert.ok(manifest.mcpServers.codexworker.env_vars.includes("CODEX_HOME"));
});

test("Windows, Linux, isolated diagnostics, disabled workers and unbound inventories never repair the host", async t => {
  const f = await fixture(t);
  for (const platform of ["win32", "linux"]) assert.equal((await f.run({ platform })).restarting, false);
  assert.equal((await f.run({ env: {} })).restarting, false);
  assert.equal((await f.run({ env: { ...f.env, RUDOLF_BRIDGE_LIFECYCLE_CHILD: "1" } })).restarting, false);
  await writeFile(path.join(f.stateDir, "desktop-config.local.json"), JSON.stringify({ autoStart: false }));
  assert.equal((await f.run()).restarting, false);
  await writeFile(path.join(f.stateDir, "desktop-config.local.json"), JSON.stringify({ autoStart: true }));
  await rm(path.join(f.stateDir, "manager-binding.json"));
  assert.equal((await f.run()).restarting, false);
  assert.equal(f.installs.length, 0); assert.equal(f.reopens.length, 0);
});

test("concurrent inventories claim one setup and one normal reopen, preserving the original binding", async t => {
  const f = await fixture(t), binding = await readFile(path.join(f.stateDir, "manager-binding.json"));
  const results = await Promise.all(Array.from({ length: 8 }, () => f.run()));
  assert.ok(results.every(r => r.restarting));
  assert.equal(f.installs.length, 1); assert.deepEqual(f.reopens, [f.file]);
  assert.equal((await f.record()).status, "scheduled");
  assert.equal((await f.record()).threadId, "original-chat");
  assert.ok((await readFile(path.join(f.stateDir, "manager-binding.json"))).equals(binding));
  assert.equal((await f.run()).restarting, true); assert.equal(f.reopens.length, 1);
});

test("failed setup is terminal for this boot and a reopened host without native input cannot loop", async t => {
  const f = await fixture(t);
  await assert.rejects(f.run({ install: async () => { throw Error("native startup metadata invalid"); } }), /metadata invalid/);
  assert.equal((await f.run()).restarting, false); assert.equal(f.reopens.length, 0);
  assert.equal((await f.record()).status, "failed");
  const other = await fixture(t); await other.run();
  const result = await other.run({ readChain: async () => [{ ...other.owner, pid: 701 }] });
  assert.equal(result.restarting, false); assert.equal(result.error, "desktop_startup_reopen_unconfirmed");
  assert.equal(other.reopens.length, 1);
});

test("an exited repair actor releases startup, while an inspection failure keeps the same live attempt", async t => {
  const f = await fixture(t); await f.run();
  const waiting = await f.run({ readChain: async pid => { if (pid === 698) throw Error("temporary inspection failure"); return [f.owner]; } });
  assert.equal(waiting.restarting, true);
  assert.equal((await f.record()).status, "scheduled");
  const stopped = await f.run({ readChain: async pid => pid === 698 ? [] : [f.owner] });
  assert.equal(stopped.restarting, false); assert.equal(stopped.error, "desktop_startup_actor_exited");
  assert.equal((await f.record()).status, "failed"); assert.equal(f.reopens.length, 1);
});

test("only a current primary native controller confirms restoration after normal reopening", async t => {
  const f = await fixture(t); await f.run();
  const owner = { ...f.owner, pid: 701, startedAt: "new Desktop lifetime" };
  const descriptor = path.join(f.stateDir, "descriptor.json");
  await writeFile(descriptor, JSON.stringify({ version: 1, desktopOwner: owner, controllerPid: 702 }), { mode: 0o600 });
  const env = { ...f.env, CODEXWORKER_NATIVE_IMAGE_INPUT: "1", CODEXWORKER_NATIVE_QUESTION_INPUT: "1",
    CODEXWORKER_NATIVE_INPUT_DESCRIPTOR: descriptor };
  const result = await f.run({ env, readChain: async pid => pid === 702 ? [{ pid: 702 }, owner] : [owner] });
  assert.equal(result.restarting, false); assert.equal((await f.record()).status, "restored");
  assert.equal((await f.record()).restoredDesktopPid, 701); assert.equal(f.reopens.length, 1);
});

test("normal reopening checks the exact owner, stops on declined quit and launches the same app with explicit environment", async t => {
  const f = await fixture(t); await f.run(); const calls = [];
  await reopenDesktop(f.file, { platform: "darwin", readChain: f.options.readChain,
    execute: async (command, args) => { calls.push({ command, args }); f.engineExited(); return {}; } });
  assert.deepEqual(calls[1], { command: "/usr/bin/open", args: ["-a", "/Applications/ChatGPT.app", "--env", "CODEX_CLI_PATH=" + f.launcher,
    "--env", "CODEXWORKER_STATE_DIR=" + f.stateDir] });
  assert.equal((await f.record()).status, "opened");
  const declined = await fixture(t); await declined.run(); let writes = 0;
  await assert.rejects(reopenDesktop(declined.file, { platform: "darwin", readChain: declined.options.readChain,
    execute: async () => { writes++; throw Error("normal quit declined"); } }), /declined/);
  assert.equal(writes, 1); assert.equal((await declined.record()).status, "failed");
  assert.equal((await declined.record()).detail, "normal quit declined");
  const changed = await fixture(t); await changed.run();
  await assert.rejects(reopenDesktop(changed.file, { platform: "darwin", readChain: async () => [],
    execute: () => assert.fail("owner mismatch must not operate an app") }), /owner_changed/);
});

test("disabling during setup or before normal reopen cancels the host operation", async t => {
  const f = await fixture(t);
  const result = await f.run({ install: async () => {
    await writeFile(path.join(f.stateDir, "desktop-config.local.json"), JSON.stringify({ autoStart: false }));
    return { launcher: f.launcher };
  } });
  assert.equal(result.restarting, false); assert.equal(f.reopens.length, 0);
  assert.equal((await f.record()).status, "disabled");
  const next = await fixture(t); await next.run();
  await writeFile(path.join(next.stateDir, "desktop-config.local.json"), JSON.stringify({ autoStart: false }));
  await reopenDesktop(next.file, { platform: "darwin", readChain: next.options.readChain,
    execute: () => assert.fail("disabled Worker must not operate the Desktop app") });
  assert.equal((await next.record()).status, "disabled");
});

test("the actual AppKit script requests normal quit and never force-terminates the host", async t => {
  const f = await fixture(t); await f.run(); let script;
  await reopenDesktop(f.file, { platform: "darwin", readChain: f.options.readChain,
    execute: async (command, args) => { if (command === "/usr/bin/osascript") script = args[3]; f.engineExited(); } });
  let quit = 0;
  const app = { isNil: () => false, bundleURL: { path: "/Applications/ChatGPT.app" },
    get terminate() { quit++; this.terminated = true; return true; }, terminated: false };
  const sandbox = { ObjC: Object.assign(() => {}, { import() {}, unwrap: x => x }),
    $: { NSRunningApplication: { runningApplicationWithProcessIdentifier: () => app } } };
  assert.equal(vm.runInNewContext(script + '\nrun(["700", "/Applications/ChatGPT.app"])', sandbox), "terminated");
  assert.equal(quit, 1); assert.equal(script.includes("forceTerminate"), false);
});

test("the real macOS AppKit bridge handles an already absent PID without operating an app", { skip: process.platform !== "darwin" }, async t => {
  const f = await fixture(t); await f.run();
  await reopenDesktop(f.file, { platform: "darwin", readChain: f.options.readChain,
    execute: async (command, args, options) => {
      if (command === "/usr/bin/osascript") {
        const safeArgs = [...args]; safeArgs[4] = "2147483647";
        const result = await promisify(execFile)(command, safeArgs, options);
        assert.equal(result.stdout.trim(), "already-exited"); f.engineExited();
      } else assert.equal(command, "/usr/bin/open"); // The test never opens a real app.
    } });
});

test("native settings follow CODEX_HOME and a relocated existing Worker state on both operating systems", async t => {
  const f = await fixture(t), home = path.join(f.stateDir, "custom Codex home"), defaultState = path.join(home, "state/codexworker");
  await mkdir(defaultState, { recursive: true });
  await writeFile(path.join(defaultState, "worker-instance.local.json"), JSON.stringify({ stateDir: f.stateDir }));
  const settings = await loadNativeConnection({ CODEX_HOME: home });
  assert.equal(settings.version, 1); assert.equal(settings.realCli, "/original/native/codex");
});

test("Mac immutable preparation stays in the relocated Worker state", { skip: process.platform !== "darwin" }, async t => {
  const f = await fixture(t), home = path.join(f.stateDir, "custom home"), defaultState = path.join(home, "state/codexworker");
  await mkdir(defaultState, { recursive: true });
  await writeFile(path.join(defaultState, "worker-instance.local.json"), JSON.stringify({ stateDir: f.stateDir }));
  const prepared = await prepareDesktopConnection({ platform: "darwin", env: { CODEX_HOME: home }, mode: "native" });
  assert.equal(prepared.stateDir, f.stateDir);
  assert.equal(path.dirname(path.dirname(prepared.launcher)), path.join(f.stateDir, "desktop-connection"));
});
