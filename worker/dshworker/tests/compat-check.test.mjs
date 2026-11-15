import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { compareSharedModules, pythonCommands, runCompatibility, testEnvironment, testFailureDetails } from "../../check-compat.mjs";

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "worker-hash-check-"));
  const roots = ["codexworker", "dshworker"].map(worker => path.join(root, "worker", worker));
  await Promise.all(roots.map(directory => mkdir(directory, { recursive: true })));
  t.after(() => rm(root, { recursive: true, force: true }));
  return { root, roots };
}

test("compatibility checks compare bytes and include newly added public root modules", async t => {
  const f = await fixture(t);
  await Promise.all(f.roots.map(directory => writeFile(path.join(directory, "shared.mjs"), "export const value = 1;\n")));
  const [module] = await compareSharedModules(f.root);
  assert.equal(module.match, true);
  assert.match(module.codexSha256, /^[a-f0-9]{64}$/);
  assert.equal(module.codexSha256, module.dshSha256);
  await writeFile(path.join(f.roots[1], "shared.mjs"), "export const value = 2;\n");
  await writeFile(path.join(f.roots[1], "added.mjs"), "export {};\n");
  assert.deepEqual((await compareSharedModules(f.root)).map(module => [module.file, module.match]),
    [["added.mjs", false], ["shared.mjs", false]]);
});

test("module names remain case-sensitive on macOS and Windows filesystems", async t => {
  const f = await fixture(t);
  await writeFile(path.join(f.roots[0], "Shared.mjs"), "export {};\n");
  await writeFile(path.join(f.roots[1], "shared.mjs"), "export {};\n");
  const modules = await compareSharedModules(f.root);
  assert.equal(modules.length, 2);
  assert.ok(modules.every(module => !module.match));
});

test("a hash mismatch fails the automatic check before any test process is launched", async t => {
  const f = await fixture(t), lines = [];
  await writeFile(path.join(f.roots[0], "only.mjs"), "export {};\n");
  assert.equal(await runCompatibility({ root: f.root, report: line => lines.push(line) }), false);
  assert.ok(lines.some(line => line.startsWith("HASH_MISMATCH")));
  assert.ok(!lines.some(line => line.startsWith("codexworker passed=")));
});

test("Python selection covers native Windows launchers, macOS and executable paths containing spaces", () => {
  assert.deepEqual(pythonCommands("win32"), [["py", "-3"], ["python"], ["python3"]]);
  assert.deepEqual(pythonCommands("darwin"), [["python3"], ["python"]]);
  assert.deepEqual(pythonCommands("win32", "C:\\Program Files\\Python\\python.exe"),
    [["C:\\Program Files\\Python\\python.exe"]]);
});

test("test isolation handles Windows environment key casing and preserves user homes and PATH", () => {
  assert.deepEqual(testEnvironment({ CODEXWORKER_STATE_DIR: "live", dshworker_config: "live",
    Rudolf_Bridge_Token: "live", Codex_App_Tools_Pipe_Path: "live", CODEXWORKER_TEST_PYTHON: "test-python",
    PATH: "keep", CODEX_HOME: "keep", USERPROFILE: "keep" }),
  { CODEXWORKER_TEST_PYTHON: "test-python", PATH: "keep", CODEX_HOME: "keep", USERPROFILE: "keep" });
});

test("CI diagnostics retain the failing TAP assertion even when later passing tests fill the log tail", () => {
  const failure = ["# Subtest: native compatibility", "not ok 1 - native compatibility", "  ---",
    "  expected: 1", "  actual: 0", "  ..."].join("\n");
  const output = testFailureDetails(failure + "\n" + "ok 2 - later passing test\n".repeat(200));
  assert.match(output, /not ok 1 - native compatibility/);
  assert.match(output, /expected: 1/);
  assert.match(output, /actual: 0/);
  assert.ok(!output.includes("later passing test"));
});
