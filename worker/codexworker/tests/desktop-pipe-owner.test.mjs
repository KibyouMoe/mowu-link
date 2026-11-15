import test from "node:test";
import assert from "node:assert/strict";
import { resolveDesktopPipeOwner, resolveDesktopOwner, ensureDesktopSupervisor } from "../desktop-lifecycle.mjs";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const owner = { pid: 700, executable: "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT", startedAt: "stamp", name: "ChatGPT" };
const options = { platform: "darwin", uid: 501, inspect: async () => ({ isSocket: () => true, uid: 501 }),
  exec: async () => ({ stdout: "p700\nu501\nf104\nn/host.sock\n" }), readChain: async () => [owner] };

test("Mac shared-daemon owner comes from the exact host-supplied socket and authoritative process", async () => {
  const calls = [];
  assert.deepEqual(await resolveDesktopPipeOwner("/host.sock", { ...options, exec: async (command, args) => {
    calls.push([command, args]); return options.exec();
  } }), owner);
  assert.deepEqual(calls, [["/usr/sbin/lsof", ["-nP", "-a", "-U", "-Fpun", "/host.sock"]]]);
});
test("Windows and invalid paths do not use the Mac owner inspector", async () => {
  const inspect = async () => assert.fail("must not inspect");
  for (const value of [null, "", "relative.sock", "/host\0.sock"])
    assert.equal(await resolveDesktopPipeOwner(value, { ...options, inspect }), null);
  assert.equal(await resolveDesktopPipeOwner("/host.sock", { ...options, platform: "win32", inspect }), null);
});
test("wrong UID, symlink/non-socket, non-Desktop owner and ambiguous owners fail closed", async () => {
  for (const extra of [
    { inspect: async () => ({ isSocket: () => false, uid: 501 }) },
    { inspect: async () => ({ isSocket: () => true, uid: 0 }) },
    { exec: async () => ({ stdout: "p700\nu0\n" }) },
    { readChain: async () => [{ pid: 700, executable: "/usr/bin/node", name: "node", startedAt: "stamp" }] },
    { exec: async () => ({ stdout: "p701\nu501\n" }), readChain: async () => [{ pid: 701, name: "node" }, owner] },
    { exec: async () => ({ stdout: "p700\nu501\np701\nu501\n" }), readChain: async pid => [{ ...owner, pid }] },
  ]) assert.equal(await resolveDesktopPipeOwner("/host.sock", { ...options, ...extra }), null);
});
test("an inspection refusal never falls through to another ownership mechanism", async () => {
  let pipeCalls = 0;
  assert.equal(await resolveDesktopOwner(10, { readChain: async () => { throw new Error("denied"); },
    pipePath: "/host.sock", readPipeOwner: async () => { pipeCalls++; return owner; }, delay: async () => {} }), null);
  assert.equal(pipeCalls, 0);
});
test("real handoff chooses the verified pipe owner before launching; Windows keeps ancestor-only behavior", async t => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "cw-pipe-owner-"));
  t.after(() => rm(stateDir, { recursive: true, force: true }));
  for (const name of ["desktop-config.local.json", "manager-binding.json"]) await writeFile(path.join(stateDir, name), "{}");
  let selected, ready = false, pipeCalls = 0;
  const config = { stateDir, pluginRoot: stateDir, env: { CODEX_APP_TOOLS_PIPE_PATH: "/host.sock" },
    readChain: async () => [{ pid: 10, executable: "/usr/bin/daemon", startedAt: "stamp" }],
    pipeOwner: async supplied => { assert.equal(supplied, "/host.sock"); pipeCalls++; return owner; },
    ready: async () => ready, launchImpl: async value => { selected = value.owner; ready = true; },
    ensureWatchdog: async () => {}, delay: async () => {} };
  assert.equal(await ensureDesktopSupervisor({ ...config, platform: "darwin" }), true);
  assert.deepEqual(selected, owner); assert.equal(pipeCalls, 1);
  pipeCalls = 0;
  assert.equal(await ensureDesktopSupervisor({ ...config, platform: "win32" }), false);
  assert.equal(pipeCalls, 0);
});
