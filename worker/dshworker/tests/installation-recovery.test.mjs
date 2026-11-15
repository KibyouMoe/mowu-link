import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { auditInstallation, prepareInstallationRecovery, restoreZeroedBusinessFiles } from "../scripts/installation-recovery.mjs";
import { repairKnown } from "../scripts/watchdog-remedies.mjs";

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "bridge-code-integrity-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const sourceRoot = path.join(root, "source"), pluginRoot = path.join(root, "installed"), stateDir = path.join(root, "state");
  await mkdir(stateDir);
  for (const dir of [sourceRoot, pluginRoot]) {
    await mkdir(path.join(dir, ".codex-plugin"), { recursive: true });
    await writeFile(path.join(dir, ".codex-plugin", "plugin.json"), JSON.stringify({ name: "test-bridge", version: "1.0.0" }));
    await writeFile(path.join(dir, ".mcp.json"), '{"mcpServers":{"test":{}}}');
    await writeFile(path.join(dir, "mcp-business.mjs"), 'export const BUSINESS_VERSION = "1.0.0";\n');
    await writeFile(path.join(dir, "example.mjs"), "export const value = 1;\n");
    await writeFile(path.join(dir, "server.mjs"), "// Fixed startup must not change.\n");
  }
  await writeFile(path.join(stateDir, "runtime-source.local.json"), JSON.stringify({ sourceRoot: pluginRoot }));
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ bridgeName: "test", machine: "codexworker" }));
  await writeFile(path.join(stateDir, "desktop-manager-state.json"), "unreadable state must not be touched");
  const options = { sourceRoot, pluginRoot, stateDir };
  await prepareInstallationRecovery(options);
  const damage = async name => {
    const original = await readFile(path.join(sourceRoot, name));
    await writeFile(path.join(pluginRoot, name), Buffer.alloc(original.length));
  };
  return { ...options, damage };
}

test("pinned all-zero business corruption restores exact bytes, backs up originals and never touches state or fixed startup", async t => {
  const f = await fixture(t); await f.damage("mcp-business.mjs"); await f.damage("example.mjs");
  assert.deepEqual(await auditInstallation(f), { code: "installation_business_corrupt", corruptFiles: 2 });
  assert.equal(await restoreZeroedBusinessFiles(f), "installation_restored");
  for (const name of ["mcp-business.mjs", "example.mjs", "server.mjs"]) assert.deepEqual(await readFile(path.join(f.pluginRoot, name)), await readFile(path.join(f.sourceRoot, name)));
  assert.equal(await readFile(path.join(f.stateDir, "desktop-manager-state.json"), "utf8"), "unreadable state must not be touched");
  const backup = (await readdir(path.join(f.stateDir, "plugin-backups")))[0];
  const bytes = await readFile(path.join(f.stateDir, "plugin-backups", backup, "example.mjs"));
  assert.equal(bytes.every(byte => byte === 0), true);
  assert.equal((await auditInstallation(f)).code, "installation_business_intact");
  assert.equal(await restoreZeroedBusinessFiles(f), "installation_business_intact");
});

test("a legitimate edit or truncation blocks the whole repair rather than overwriting user changes", async t => {
  const f = await fixture(t); await f.damage("mcp-business.mjs");
  await writeFile(path.join(f.pluginRoot, "example.mjs"), "export const edited = true;\n");
  assert.equal(await restoreZeroedBusinessFiles(f), "installation_business_drift");
  assert.equal((await readFile(path.join(f.pluginRoot, "mcp-business.mjs"))).every(b => b === 0), true);
});

test("changed source release, installed identity and machine identity all fail closed", async t => {
  for (const mutation of ["source", "manifest", "machine"]) {
    const f = await fixture(t); await f.damage("example.mjs");
    if (mutation === "source") await writeFile(path.join(f.sourceRoot, "example.mjs"), "export const changed = 2;\n");
    if (mutation === "manifest") await writeFile(path.join(f.pluginRoot, ".codex-plugin", "plugin.json"), '{"name":"another-bridge","version":"1.0.0"}');
    if (mutation === "machine") await writeFile(path.join(f.stateDir, "desktop-config.local.json"), '{"bridgeName":"test","machine":"qicao"}');
    assert.equal(await restoreZeroedBusinessFiles(f), "installation_recovery_rejected");
    assert.equal((await readFile(path.join(f.pluginRoot, "example.mjs"))).every(b => b === 0), true);
  }
});

test("missing opt-in is silent; path escapes, fixed-file entries and disposable cache targets cannot be repaired", async t => {
  const f = await fixture(t), filename = path.join(f.stateDir, "installation-recovery.local.json");
  const plan = JSON.parse(await readFile(filename, "utf8"));
  for (const name of ["../outside.mjs", "server.mjs"]) {
    await writeFile(filename, JSON.stringify({ ...plan, files: [{ ...plan.files[0], name }] }));
    assert.equal(await restoreZeroedBusinessFiles(f), "installation_recovery_rejected");
  }
  await rm(filename);
  assert.equal((await auditInstallation(f)).code, "installation_recovery_unconfigured");
  const cache = path.join(f.stateDir, ".codex", "plugins", "cache", "test"); await mkdir(cache, { recursive: true });
  await assert.rejects(prepareInstallationRecovery({ ...f, pluginRoot: cache }));
});

test("watchdog repairs corrupt files through the allowlist without model/native calls or stopping a live supervisor", async t => {
  const f = await fixture(t); await f.damage("example.mjs");
  const options = { ...f, fetchImpl: () => { assert.fail("No HTTP write for local file repair"); },
    ensure: () => { assert.fail("Existing supervisor must recover its own child"); } };
  assert.equal(await repairKnown("restore_business_files", { code: "installation_business_corrupt" }, options), "installation_restored");
  await f.damage("example.mjs");
  assert.equal(await repairKnown("ensure_desktop_lifecycle", { code: "bridge_unreachable" }, options), "installation_restored");
});
