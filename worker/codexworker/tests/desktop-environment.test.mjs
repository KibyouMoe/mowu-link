import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { ENVIRONMENT_LABEL, installDesktopEnvironment } from "../interface/desktop-connection/install-environment.mjs";

async function fixture(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "Desktop 环境 '&-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  const installed = { launcher: path.join(home, "immutable connection", "codex"), stateDir: path.join(home, "state") };
  const calls = [];
  let selected = "previous-native-cli", fail = null, metadata = true;
  const exec = async (file, args) => {
    calls.push({ file, args });
    if (file === installed.launcher) {
      if (args.includes("exec-server")) {
        if (fail === "executor") throw Error("native auxiliary delegation failed");
        return { stdout: "native exec-server help" };
      }
      return { stdout: JSON.stringify(metadata
        ? { appServerVersion: "0.167", configRequirements: { requirements: null } } : {}) };
    }
    if (file === "/usr/bin/plutil") { if (fail === "lint") throw Error("lint failed"); return { stdout: "OK" }; }
    if (args[0] === "getenv") return { stdout: selected + "\n" };
    if (args[0] === "unsetenv") selected = "";
    if (args[0] === "setenv") {
      selected = args[2];
      if (fail === "setenv" && selected === installed.launcher) throw Error("unknown command outcome");
    }
    return { stdout: "" };
  };
  const options = { platform: "darwin", home, prepare: async value => {
    assert.equal(value.mode, "native"); return installed;
  }, exec };
  return { home, installed, calls, options, selected: () => selected,
    fail: value => { fail = value; }, invalidMetadata: () => { metadata = false; } };
}

test("Windows and Linux never receive the macOS login environment installation", async () => {
  for (const platform of ["win32", "linux"]) await assert.rejects(installDesktopEnvironment({ platform,
    prepare: () => assert.fail("must not inspect files"), exec: () => assert.fail("must not change environment") }),
  /platform_unsupported/);
});

test("one-shot login item only selects the checked Desktop launcher, with Unicode/XML paths preserved", async t => {
  const f = await fixture(t), result = await installDesktopEnvironment(f.options);
  const plist = await readFile(result.environmentFile, "utf8");
  assert.ok(plist.includes(ENVIRONMENT_LABEL)); assert.match(plist, /<key>RunAtLoad<\/key><true\/>/);
  assert.ok(plist.includes("<string>/bin/launchctl</string><string>setenv</string>"));
  assert.equal(plist.includes("KeepAlive"), false); assert.equal(plist.includes("cli.mjs"), false);
  assert.match(plist, /&apos;&amp;/);
  assert.equal(f.selected(), f.installed.launcher);
  assert.deepEqual(f.calls[0].args.slice(0, 5), ["-c", "features.code_mode_host=true", "app-server",
    "--analytics-default-enabled", "--print-startup-config"]);
  assert.equal(f.calls.some(c => c.args.includes("--startup-sign-out")), false);
  assert.equal(result.startupPolicyVerified, true);
  assert.equal(result.nativeAuxiliaryVerified, true);
  assert.deepEqual(f.calls[1].args, ["-c", 'forced_login_method="chatgpt"', "exec-server", "--help"]);
});

test("invalid native startup metadata prevents persistent settings or environment changes", async t => {
  const f = await fixture(t); f.invalidMetadata();
  await assert.rejects(installDesktopEnvironment(f.options), /startup_policy_invalid/);
  assert.equal(f.calls.length, 1); assert.equal(f.selected(), "previous-native-cli");
});

test("failed Desktop auxiliary delegation prevents selecting an otherwise valid launcher", async t => {
  const f = await fixture(t); f.fail("executor");
  await assert.rejects(installDesktopEnvironment(f.options), /auxiliary delegation failed/);
  assert.equal(f.calls.length, 2); assert.equal(f.selected(), "previous-native-cli");
  await assert.rejects(readFile(path.join(f.home, "Library", "LaunchAgents", ENVIRONMENT_LABEL + ".plist")),
    { code: "ENOENT" });
});

test("failed lint or uncertain environment write restores existing bytes and prior selection", async t => {
  for (const failure of ["lint", "setenv"]) {
    const f = await fixture(t);
    const directory = path.join(f.home, "Library", "LaunchAgents"); await mkdir(directory, { recursive: true });
    const file = path.join(directory, ENVIRONMENT_LABEL + ".plist"), previous = Buffer.from("prior item bytes\n");
    await writeFile(file, previous); f.fail(failure);
    await assert.rejects(installDesktopEnvironment(f.options));
    assert.deepEqual(await readFile(file), previous); assert.equal(f.selected(), "previous-native-cli");
    const backups = await readdir(path.join(f.installed.stateDir, "desktop-environment-backups"));
    assert.equal(backups.length, 1);
    assert.deepEqual(await readFile(path.join(f.installed.stateDir, "desktop-environment-backups", backups[0])), previous);
    assert.equal((await readdir(directory)).some(name => name.endsWith(".tmp")), false);
  }
});
