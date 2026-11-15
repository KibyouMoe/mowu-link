// Desktop-owned script scheduler. It runs no model, MCP host or Feishu receiver.
// Each check is a fresh bounded child so repaired rules take effect next time.
import { fork } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile, writeFile, rename } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { desktopOwner, processChain, resolveDesktopOwner } from "../desktop-lifecycle.mjs";
import { WatchdogEngine } from "./watchdog-engine.mjs";
import { escalateIncident } from "./watchdog-native.mjs";
import { resolveWatchdogStateDir } from "./watchdog-context.mjs";

const args = process.argv.slice(2), desktopPid = Number(args[1]);
if (args[0] !== "--desktop-pid" || !Number.isSafeInteger(desktopPid) || desktopPid < 1 || !process.env.CODEX_APP_TOOLS_PIPE_PATH) throw new Error("Fresh Desktop environment required");
const owner = await resolveDesktopOwner(desktopPid);
if (!owner || owner.pid !== desktopPid || owner.startedAt !== process.env.RUDOLF_BRIDGE_DESKTOP_STARTED_AT) throw new Error("desktop_lifetime_changed");
const ownProcess = (await processChain(process.pid))[0];
if (ownProcess?.pid !== process.pid) throw new Error("script_watchdog_identity_unavailable");
const pluginRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const stateDir = resolveWatchdogStateDir();
const configPath = path.join(stateDir, "script-watchdog.local.json");
const config = JSON.parse(await readFile(configPath, "utf8"));
if (config.enabled !== true || !/^[a-f\d-]{36}$/i.test(config.maintenanceThreadId ?? "")) throw new Error("script_watchdog_disabled");
const intervalMs = Math.max(15000, Math.min(300000, Number(config.intervalMs) || 300000));
const key = createHash("sha256").update(JSON.stringify([path.resolve(stateDir).toLowerCase(), desktopPid, owner.startedAt])).digest("hex").slice(0, 24);
const lease = net.createServer(socket => socket.end());
try { await new Promise((resolve, reject) => { lease.once("error", reject); lease.listen("\\\\.\\pipe\\codexworker-script-watchdog-" + key, resolve); }); }
catch (error) { if (error.code === "EADDRINUSE") process.exit(0); throw error; }

let stopping = false, child = null, checkTimer, ownerTimer, ownerChecking = false, completed = 0, lastResult = null, nextCheckAt = null;
const statusPath = path.join(stateDir, "script-watchdog-status.json");
let writes = Promise.resolve();
const persist = (filename, value) => {
  writes = writes.catch(() => {}).then(async () => {
    const temp = filename + "." + process.pid + ".tmp";
    await writeFile(temp, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 }); await rename(temp, filename);
  });
  return writes;
};
const status = stage => persist(statusPath, { version: 1, stage, pid: process.pid, desktopPid,
  processStartedAt: ownProcess.startedAt, desktopStartedAt: owner.startedAt, intervalMs, completed, lastResult, nextCheckAt, updatedAt: new Date().toISOString() });
const fallbackPath = path.join(stateDir, "script-watchdog-runner-state.json");
let fallbackState;
try { fallbackState = JSON.parse(await readFile(fallbackPath, "utf8")); } catch (error) { if (error.code !== "ENOENT") throw error; }
const fallback = new WatchdogEngine({ state: fallbackState, intervalMs, save: value => persist(fallbackPath, value),
  remedyFor: () => null, repair: async () => "unavailable",
  escalate: (incident, prompt) => escalateIncident({ threadId: config.maintenanceThreadId, incident, prompt }) });

async function shutdown() {
  if (stopping) return; stopping = true; clearTimeout(checkTimer); clearInterval(ownerTimer);
  if (child) { child.kill(); child = null; }
  await status("stopped").catch(() => {});
  await new Promise(resolve => lease.close(resolve));
}
async function checkOwner() {
  const current = desktopOwner(await processChain(desktopPid));
  if (!current || current.startedAt !== owner.startedAt || current.executable !== owner.executable) return false;
  const settings = JSON.parse(await readFile(configPath, "utf8"));
  return settings.enabled === true && settings.maintenanceThreadId === config.maintenanceThreadId;
}
async function run() {
  if (stopping || child) return;
  try {
    if (!await checkOwner()) { await shutdown(); return; }
  } catch { checkTimer = setTimeout(() => void run(), 30000); return; }
  const audit = fork(path.join(pluginRoot, "scripts", "watchdog-check.mjs"), [], { cwd: pluginRoot, windowsHide: true,
    env: { ...process.env, RUDOLF_SCRIPT_WATCHDOG_OWNER_PID: String(desktopPid) }, stdio: ["ignore", "ignore", "ignore", "ipc"] });
  child = audit; let result = null, done = false;
  const timeout = setTimeout(() => audit.kill(), 75000);
  audit.on("message", message => { if (message && typeof message.phase === "string") result = message; });
  const finish = async () => {
    if (done) return; done = true; clearTimeout(timeout); if (child === audit) child = null;
    if (stopping) return;
    completed++;
    const failed = !result || result.phase === "check_failed";
    lastResult = result ?? { phase: "check_failed", code: "script_check_unavailable" };
    try {
      const fallbackResult = await fallback.tick({ status: failed ? "fault" : "healthy", code: failed ? "script_check_unavailable" : "script_check_completed" });
      const next = failed ? fallbackResult.nextMs : result.nextMs;
      const delay = Math.max(15000, Math.min(300000, Number(next) || 300000));
      nextCheckAt = new Date(Date.now() + delay).toISOString();
      await status("running");
      checkTimer = setTimeout(() => void run(), delay);
    } catch { checkTimer = setTimeout(() => void run(), 60000); }
  };
  audit.once("exit", () => void finish()); audit.once("error", () => void finish());
}
ownerTimer = setInterval(() => {
  if (stopping || ownerChecking) return; ownerChecking = true;
  void checkOwner().then(alive => { if (!alive) return shutdown(); }).catch(() => {}).finally(() => { ownerChecking = false; });
}, 30000);
process.on("SIGINT", () => void shutdown()); process.on("SIGTERM", () => void shutdown());
await status("running"); await run();
