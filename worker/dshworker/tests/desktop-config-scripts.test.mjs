import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

const run = promisify(execFile), root = fileURLToPath(new URL("..", import.meta.url));
async function fixture(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "cw-config-scripts-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  const state = path.join(home, ".codex", "state", "codexworker"), selected = path.join(home, "existing state 中文");
  await mkdir(state, { recursive: true }); await mkdir(selected);
  await writeFile(path.join(state, "worker-instance.local.json"), JSON.stringify({ stateDir: selected }));
  await writeFile(path.join(selected, "desktop-config.local.json"), JSON.stringify({ autoStart: false, router: { enabled: false } }));
  const env = { ...process.env, HOME: home, USERPROFILE: home };
  for (const key of ["CODEX_APP_TOOLS_PIPE_PATH", "CODEXWORKER_STATE_DIR", "CODEXWORKER_NATIVE_TRANSPORT", "CODEXWORKER_ROUTER_URL", "CODEXWORKER_ROUTER_TOKEN_FILE"]) delete env[key];
  const execute = (name, args = [], extra = {}) => run(process.execPath, [path.join(root, "scripts", name), ...args],
    { env: { ...env, ...extra }, timeout: 10000, windowsHide: true });
  return { home, selected, execute };
}
test("image maintenance command reads Desktop state pointer with spaces and Unicode; it starts no Worker", async t => {
  const f = await fixture(t);
  await assert.rejects(f.execute("send-router-image.mjs", ["--upload-only", "--image", path.join(f.home, "never-upload.png")]), error => {
    const reply = JSON.parse(error.stderr); assert.equal(reply.detail, "Router is disabled."); return error.code === 1;
  });
});
test("explicit Desktop state overrides a stale state pointer for maintenance commands", async t => {
  const f = await fixture(t), state = path.join(f.home, "explicit state"); await mkdir(state);
  await writeFile(path.join(state, "desktop-config.local.json"), JSON.stringify({ router: { enabled: false } }));
  await writeFile(path.join(f.home, ".codex", "state", "codexworker", "worker-instance.local.json"), "invalid");
  await assert.rejects(f.execute("send-router-image.mjs", ["--upload-only", "--image", "never.png"], { CODEXWORKER_STATE_DIR: state }),
    error => JSON.parse(error.stderr).detail === "Router is disabled.");
});
test("selfcheck reports real selected state and disabled autoStart without claiming a native Desktop test", async t => {
  const f = await fixture(t), reply = await f.execute("selfcheck.mjs", ["--json"]), result = JSON.parse(reply.stdout);
  assert.equal(result.nativeProbed, false);
  const expected = await realpath(f.selected);
  assert.ok(result.checks.some(c => c.name === "状态目录" && path.resolve(c.detail) === expected));
  assert.ok(result.checks.some(c => c.name === "autoStart" && c.detail === "false"));
  assert.ok(result.checks.some(c => c.name === "CODEX_APP_TOOLS_PIPE_PATH" && c.status === "warn"));
});
