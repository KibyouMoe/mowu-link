import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { chmod, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { PassThrough } from "node:stream";
import { pathToFileURL } from "node:url";
import test from "node:test";
import { desktopRequest, desktopServerOptions, loadDesktopConnection, requireDesktopParent,
  runDesktopConnection, runDesktopServer } from "../interface/desktop-connection/entry.mjs";
import { prepareDesktopConnection } from "../interface/desktop-connection/prepare.mjs";

const actualStartup = ["-c", "features.code_mode_host=true", "app-server", "--analytics-default-enabled",
  "-c", "plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true"];
const owner = { pid: 700, startedAt: "Fri Nov 13 10:20:30 2026", name: "ChatGPT",
  executable: "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT" };

test("actual Desktop extra startup arguments keep the existing thread and explicit overrides", () => {
  const options = desktopServerOptions(actualStartup);
  for (const method of ["thread/resume", "thread/start", "thread/fork"]) {
    const original = { id: 3, method, params: { threadId: "original", config: { "features.code_mode_host": false } } };
    const projected = desktopRequest(original, options.config);
    assert.equal(projected.params.threadId, "original");
    assert.equal(projected.params.config["features.code_mode_host"], false);
    assert.equal(projected.params.config["plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled"], true);
    assert.deepEqual(original.params.config, { "features.code_mode_host": false });
  }
  for (const method of ["initialize", "thread/read", "turn/start", "turn/steer", "config/read"]) {
    const request = { method, params: { input: [{ type: "text", text: "原文" }] } };
    assert.equal(desktopRequest(request, options.config), request);
  }
});

test("Mac requires a verified Desktop ancestor; Windows never inspects or connects Unix", async () => {
  assert.equal(await requireDesktopParent({ platform: "darwin", readChain: async () => [owner] }), owner);
  await assert.rejects(requireDesktopParent({ platform: "darwin", readChain: async () => [] }), /requires_desktop_parent/);
  await assert.rejects(requireDesktopParent({ platform: "darwin", readChain: async () => { throw Error("denied"); } }), /denied/);
  for (const platform of ["win32", "linux"]) {
    await assert.rejects(requireDesktopParent({ platform, readChain: () => assert.fail("must not inspect") }), /platform_unsupported/);
  }
});

test("manual/denied entry fails before settings, sockets or any native CLI", async () => {
  await assert.rejects(runDesktopConnection(actualStartup, {
    verifyParent: async () => { throw Error("not Desktop"); },
    load: () => assert.fail("must not read config"), serve: () => assert.fail("must not connect"),
    spawnImpl: () => assert.fail("must not start CLI"),
  }), /not Desktop/);
});

test("app-server only connects the existing daemon; failure cannot spawn a model", async () => {
  const settings = { socket: "/existing.sock" };
  let calls = 0;
  const deps = { verifyParent: async () => owner, load: async () => settings,
    spawnImpl: () => assert.fail("never start a model"), serve: async (value, options) => {
      calls++; assert.equal(value, settings); assert.deepEqual(options, desktopServerOptions(actualStartup)); throw Error("socket missing");
    } };
  await assert.rejects(runDesktopConnection(actualStartup, deps), /socket missing/);
  assert.equal(calls, 1);
  for (const args of [["exec", "prompt"], ["resume", "thread"], ["app-server", "daemon", "start"],
    ["app-server", "--listen", "ws://127.0.0.1:9000"], ["app-server", "--unknown"],
    ["app-server", "-c", "bad"], ["app-server", "-c", "key=unparsed"]]) {
    await assert.rejects(runDesktopConnection(args, deps));
  }
  assert.equal(calls, 1);
});

test("Desktop version and exec-server use its real binary without recursive override", async () => {
  const startupPolicy = [...actualStartup, "--print-startup-config", "--startup-client-info",
    JSON.stringify({ name: "codex_desktop", title: "Codex Desktop", version: "26.1106.51428" })];
  for (const args of [["--version"], ["exec-server"], ["app-server", "daemon", "version"],
    startupPolicy, [...startupPolicy, "--startup-sign-out"]]) {
    const child = new EventEmitter(); child.kill = () => {};
    const result = runDesktopConnection(args, { verifyParent: async () => owner,
      load: async () => ({ realCli: "/native/codex" }), env: { CODEX_CLI_PATH: "adapter", KEEP: "yes" },
      serve: () => assert.fail("not a model"), spawnImpl: (file, argv, opts) => {
        assert.equal(file, "/native/codex"); assert.equal(argv, args); assert.deepEqual(opts.env, { KEEP: "yes" });
        setImmediate(() => child.emit("exit", 0)); return child;
      } });
    assert.equal(await result, 0);
  }
});

test("Mac machine settings require private regular files, not symlinks", { skip: process.platform !== "darwin" }, async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "desktop connection 空格-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const file = path.join(root, "settings.json");
  await writeFile(file, JSON.stringify({ version: 1, realCli: "/native/codex", socket: "/existing.sock" }), { mode: 0o600 });
  const env = { CODEXWORKER_SHARED_DESKTOP_CONFIG: file };
  assert.equal((await loadDesktopConnection(env)).socket, "/existing.sock");
  await symlink(file, file + ".link");
  await assert.rejects(loadDesktopConnection({ CODEXWORKER_SHARED_DESKTOP_CONFIG: file + ".link" }), /not_private/);
  await chmod(file, 0o666); await assert.rejects(loadDesktopConnection(env), /not_private/);
});

test("Windows connector preparation fails before inspecting machine files", async () => {
  await assert.rejects(prepareDesktopConnection({ platform: "win32", pluginRoot: "Z:/must-not-read" }), /platform_unsupported/);
});

test("prepared Mac connector is relocatable, immutable and independent of deleted legacy source", { skip: process.platform !== "darwin" }, async t => {
  const home = await mkdtemp(path.join(os.tmpdir(), "Desktop's 连接-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  const state = path.join(home, "state/codexworker"); await mkdir(state, { recursive: true });
  await writeFile(path.join(state, "desktop-shared-connection.json"), JSON.stringify({
    version: 1, realCli: "/native/codex", socket: "/existing.sock",
  }), { mode: 0o600 });
  const opts = { env: { CODEX_HOME: home }, nodePath: process.execPath };
  const result = await prepareDesktopConnection(opts);
  assert.equal(result.fileCount, 8);
  const launcher = await readFile(result.launcher, "utf8");
  assert.ok(launcher.includes("'\\''")); assert.ok(launcher.includes('"$@"'));
  assert.equal(launcher.includes("/worker/codexworker/"), false);
  const root = path.dirname(result.launcher);
  const entry = await readFile(path.join(root, "interface/desktop-connection/entry.mjs"), "utf8");
  assert.match(entry, /requireDesktopParent/);
  assert.deepEqual(await prepareDesktopConnection(opts), result);
  await writeFile(path.join(root, "desktop-lifecycle.mjs"), "corrupt");
  await assert.rejects(prepareDesktopConnection(opts), /install_mismatch/);
});

test("prepared native connector contains its entire approval dependency tree", { skip: process.platform !== "darwin" }, async t => {
  const home = await mkdtemp(path.join(os.tmpdir(), "native approval 连接-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  const state = path.join(home, "state/codexworker"); await mkdir(state, { recursive: true });
  await writeFile(path.join(state, "desktop-native-connection.json"), JSON.stringify({
    version: 1, realCli: "/native/codex",
  }), { mode: 0o600 });
  const result = await prepareDesktopConnection({ env: { CODEX_HOME: home }, mode: "native" });
  assert.equal(result.fileCount, 9);
  assert.match(await readFile(result.launcher, "utf8"), /native-entry\.mjs/);
  const entry = await import(new URL("../interface/desktop-connection/native-entry.mjs", import.meta.url));
  const installed = await import(pathToFileURL(path.join(path.dirname(result.launcher), "interface/desktop-connection/native-entry.mjs")));
  assert.equal(typeof entry.runNativeConnection, typeof installed.runNativeConnection);
});

function streams() {
  const channel = new EventEmitter(); channel.stdin = new PassThrough(); channel.stdout = new PassThrough();
  let kills = 0;
  channel.kill = () => { if (++kills === 1) setImmediate(() => channel.emit("exit", 0)); };
  return { channel, input: new PassThrough(), output: new PassThrough(), errors: new PassThrough(), kills: () => kills };
}

test("JSONL stream preserves split Unicode, per-thread config and closes only its connection on EOF", async () => {
  const s = streams(), messages = []; let wire = "";
  s.channel.stdin.on("data", b => { wire += b; });
  const done = runDesktopServer({ socket: "existing" }, desktopServerOptions(actualStartup), {
    stdin: s.input, stdout: s.output, stderr: s.errors, connect: async file => { assert.equal(file, "existing"); return s.channel; },
  });
  await new Promise(resolve => setImmediate(resolve));
  const bytes = Buffer.from(JSON.stringify({ id: 1, method: "thread/resume", params: { threadId: "原会话" } }) + "\n");
  const at = bytes.indexOf(Buffer.from("原")) + 1;
  s.input.write(bytes.subarray(0, at)); s.input.write(bytes.subarray(at)); s.input.end();
  assert.equal(await done, 0); assert.equal(s.kills(), 1);
  messages.push(...wire.trim().split("\n").map(line => JSON.parse(line)));
  assert.equal(messages[0].params.threadId, "原会话");
  assert.equal(messages[0].params.config["features.code_mode_host"], true);
});

test("malformed/truncated JSONL and asynchronous write errors fail closed", async () => {
  for (const send of [s => s.input.end("{broken}\n"), s => s.input.end('{"incomplete":'),
    s => s.channel.stdin.emit("error", Error("write failed"))]) {
    const s = streams();
    const done = runDesktopServer({}, { config: {} }, { stdin: s.input, stdout: s.output, stderr: s.errors, connect: async () => s.channel });
    await new Promise(resolve => setImmediate(resolve)); send(s);
    assert.equal(await done, 1); assert.ok(s.kills() >= 1);
  }
});
