import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { watchDesktopWorker } from "../interface/desktop-connection/worker-lifecycle.mjs";
import { ensureDesktopSupervisor } from "../desktop-lifecycle.mjs";

async function fixture(t) {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "desktop-home-lifecycle-"));
  t.after(() => rm(stateDir, { recursive: true, force: true }));
  await writeFile(path.join(stateDir, "desktop-config.local.json"), "{}");
  await writeFile(path.join(stateDir, "manager-binding.json"), '{"managerThreadId":"original-chat"}');
  return { stateDir, pluginRoot: stateDir, env: { CODEX_APP_TOOLS_PIPE_PATH: "actual-host-pipe" }, intervalMs: 60000 };
}

test("the primary connection keeps checking ownership after inventory MCP retirement without an open chat", async t => {
  const f = await fixture(t), calls = [];
  const watcher = watchDesktopWorker({ ...f, ensure: async options => { calls.push(options); return true; } });
  t.after(() => watcher.close());
  assert.equal(await watcher.check(), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].stateDir, f.stateDir);
  assert.equal(calls[0].env, f.env);
  assert.equal(await watcher.check(), true);
  assert.equal(calls.length, 2);
  await watcher.close();
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(await watcher.check(), false);
  assert.equal(calls.length, 2);
});

test("concurrent checks share one startup and closing aborts the in-flight startup", async t => {
  const f = await fixture(t); let calls = 0;
  let started;
  const entering = new Promise(resolve => { started = resolve; });
  const watcher = watchDesktopWorker({ ...f, ensure: options => {
    calls++;
    started();
    return new Promise(resolve => options.signal.addEventListener("abort", () => resolve(false), { once: true }));
  } });
  t.after(() => watcher.close());
  const first = watcher.check(), second = watcher.check();
  await entering;
  assert.equal(calls, 1);
  await watcher.close();
  assert.deepEqual(await Promise.all([first, second]), [false, false]);
});

test("an unbound Desktop remains available and startup resumes after binding is configured", async t => {
  const f = await fixture(t); await rm(path.join(f.stateDir, "manager-binding.json"));
  let calls = 0;
  const watcher = watchDesktopWorker({ ...f, ensure: async () => { calls++; return true; } });
  t.after(() => watcher.close());
  assert.equal(await watcher.check(), false); assert.equal(calls, 0);
  await writeFile(path.join(f.stateDir, "manager-binding.json"), '{"managerThreadId":"original-chat"}');
  assert.equal(await watcher.check(), true); assert.equal(calls, 1);
});

test("autoStart false prevents launch on both platform branches", async t => {
  const f = await fixture(t);
  await writeFile(path.join(f.stateDir, "desktop-config.local.json"), '{"autoStart":false}');
  for (const platform of ["darwin", "win32"]) {
    const watcher = watchDesktopWorker({ ...f, ensure: options => ensureDesktopSupervisor({ ...options, platform,
      readChain: () => assert.fail("disabled Worker must not inspect processes"),
      launchImpl: () => assert.fail("disabled Worker must not start a supervisor") }) });
    assert.equal(await watcher.check(), false); await watcher.close();
  }
});
