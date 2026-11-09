// Recover only confirmed all-zero business files from an explicitly pinned
// release. Never fetch code, change fixed startup, touch state, or kill a writer.
import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdir, open, readFile, readdir, realpath, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { configuredBridgeIdentity } from "./watchdog-context.mjs";

const exec = promisify(execFile);
const fixed = new Set(["server.mjs", "plugin-runtime.mjs", "desktop-lifecycle.mjs", "business-runtime.mjs"]);
const filename = "installation-recovery.local.json";
const sha = bytes => createHash("sha256").update(bytes).digest("hex");
const zero = bytes => bytes.length > 0 && bytes.every(byte => byte === 0);
const safeName = name => /^[a-z][a-z0-9-]*\.mjs$/i.test(name) && !fixed.has(name);
const json = async file => JSON.parse(await readFile(file, "utf8"));
const ownedRoot = async root => {
  if (typeof root !== "string" || !path.isAbsolute(root)) throw new Error();
  const actual = await realpath(root);
  if (/[\\/]\.codex[\\/]plugins[\\/]cache(?:[\\/]|$)|[\\/]WindowsApps(?:[\\/]|$)/i.test(actual)) throw new Error();
  return actual;
};
const exactFile = async (root, name) => {
  const file = path.join(root, name);
  if (await realpath(file) !== file) throw new Error();
  return file;
};
const names = async root => (await readdir(root)).filter(name => name.endsWith(".mjs") && !fixed.has(name)).sort();
async function atomicWrite(file, bytes) {
  const temp = file + "." + randomUUID() + ".tmp";
  try {
    const handle = await open(temp, "wx", 0o600);
    try { await handle.writeFile(bytes); await handle.sync(); } finally { await handle.close(); }
    await rename(temp, file);
  } finally { await unlink(temp).catch(error => { if (error.code !== "ENOENT") throw error; }); }
}

// Explicit maintenance preparation, not a periodic action or trust-on-first-use.
export async function prepareInstallationRecovery({ sourceRoot, pluginRoot, stateDir }) {
  const installedRoot = await ownedRoot(pluginRoot), source = await ownedRoot(sourceRoot);
  if (installedRoot === source) throw new Error("installation_recovery_source_invalid");
  const sidecar = await json(path.join(stateDir, "runtime-source.local.json"));
  if (await realpath(sidecar.sourceRoot) !== installedRoot) throw new Error("installation_recovery_target_invalid");
  const identity = configuredBridgeIdentity(await json(path.join(stateDir, "desktop-config.local.json")));
  if (!identity) throw new Error("installation_recovery_identity_invalid");
  const manifest = await readFile(await exactFile(installedRoot, ".codex-plugin/plugin.json"));
  const sourceManifest = await readFile(await exactFile(source, ".codex-plugin/plugin.json"));
  const metadata = JSON.parse(manifest), sourceMetadata = JSON.parse(sourceManifest);
  if (metadata.version.split("+")[0] !== sourceMetadata.version.split("+")[0]) throw new Error("installation_recovery_release_mismatch");
  const files = [];
  for (const name of await names(source)) {
    if (!safeName(name)) throw new Error("installation_recovery_file_invalid");
    const file = await exactFile(source, name), bytes = await readFile(file);
    if (!bytes.length || bytes.length > 1024 * 1024 || bytes.includes(0)) throw new Error("installation_recovery_source_invalid");
    await exec(process.execPath, ["--check", file], { windowsHide: true, timeout: 10000 });
    files.push({ name, size: bytes.length, sha256: sha(bytes) });
  }
  if (!files.some(file => file.name === "mcp-business.mjs") || files.length > 128) throw new Error("installation_recovery_source_invalid");
  const plan = { version: 1, enabled: true, installedRoot, sourceRoot: source, identity,
    installedManifestSha256: sha(manifest), sourceManifestSha256: sha(sourceManifest),
    mcpSha256: sha(await readFile(await exactFile(installedRoot, ".mcp.json"))), files };
  await atomicWrite(path.join(stateDir, filename), JSON.stringify(plan, null, 2) + "\n");
  return { code: "installation_recovery_prepared", files: files.length };
}

async function inspect({ pluginRoot, stateDir }) {
  let plan;
  try { plan = await json(path.join(stateDir, filename)); }
  catch (error) { if (error.code === "ENOENT") return { code: "installation_recovery_unconfigured" }; throw error; }
  if (plan.version !== 1 || plan.enabled !== true || !Array.isArray(plan.files) || !plan.files.length || plan.files.length > 128) throw new Error();
  const root = await ownedRoot(pluginRoot), source = await ownedRoot(plan.sourceRoot);
  if (root !== await ownedRoot(plan.installedRoot) || root === source) throw new Error();
  if (await realpath((await json(path.join(stateDir, "runtime-source.local.json"))).sourceRoot) !== root) throw new Error();
  if (JSON.stringify(configuredBridgeIdentity(await json(path.join(stateDir, "desktop-config.local.json")))) !== JSON.stringify(plan.identity)) throw new Error();
  for (const [base, name, expected] of [[root, ".codex-plugin/plugin.json", plan.installedManifestSha256],
    [root, ".mcp.json", plan.mcpSha256], [source, ".codex-plugin/plugin.json", plan.sourceManifestSha256]]) {
    if (sha(await readFile(await exactFile(base, name))) !== expected) throw new Error();
  }
  const declared = plan.files.map(file => file.name).sort();
  if (new Set(declared).size !== declared.length || JSON.stringify(await names(root)) !== JSON.stringify(declared) ||
      JSON.stringify(await names(source)) !== JSON.stringify(declared)) throw new Error();
  const corrupt = []; let drift = false;
  for (const item of plan.files) {
    if (!safeName(item.name) || !Number.isSafeInteger(item.size) || item.size < 1 || item.size > 1024 * 1024 || !/^[a-f0-9]{64}$/.test(item.sha256)) throw new Error();
    const sourceBytes = await readFile(await exactFile(source, item.name));
    if (sourceBytes.length !== item.size || sourceBytes.includes(0) || sha(sourceBytes) !== item.sha256) throw new Error();
    const target = await exactFile(root, item.name), bytes = await readFile(target);
    if (sha(bytes) === item.sha256) continue;
    if (bytes.length === item.size && zero(bytes)) corrupt.push({ ...item, target, sourceBytes, damaged: bytes });
    else drift = true; // A real edit, truncation, or another release is NOT auto-replaced.
  }
  return { code: drift ? "installation_business_drift" : corrupt.length ? "installation_business_corrupt" : "installation_business_intact", corrupt };
}

export async function auditInstallation(options) {
  try { const result = await inspect(options); return { code: result.code, corruptFiles: result.corrupt?.length ?? 0 }; }
  catch { return { code: "installation_recovery_rejected", corruptFiles: 0 }; }
}

export async function restoreZeroedBusinessFiles(options) {
  let result;
  try { result = await inspect(options); } catch { return "installation_recovery_rejected"; }
  if (result.code !== "installation_business_corrupt") return result.code;
  const backup = path.join(options.stateDir, "plugin-backups", "zeroed-business-" + randomUUID());
  await mkdir(backup, { recursive: true });
  // Preserve every damaged original before any repair. No state/queue replay.
  for (const file of result.corrupt) await writeFile(path.join(backup, file.name), file.damaged, { flag: "wx", mode: 0o600 });
  for (const file of result.corrupt) {
    const now = await readFile(file.target);
    if (now.length !== file.size || !zero(now)) return "installation_changed_during_repair";
  }
  for (const file of result.corrupt) {
    if (!zero(await readFile(file.target))) return "installation_changed_during_repair";
    await atomicWrite(file.target, file.sourceBytes);
    if (sha(await readFile(file.target)) !== file.sha256) return "installation_restore_verify_failed";
  }
  return "installation_restored";
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv[2] !== "--prepare" || process.argv.length !== 5) throw new Error("Use --prepare <verified-source-root> <state-dir>");
  const stateDir = path.resolve(process.argv[4]);
  const pluginRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  console.log(JSON.stringify(await prepareInstallationRecovery({ sourceRoot: path.resolve(process.argv[3]), stateDir, pluginRoot })));
}
