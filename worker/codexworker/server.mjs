// Fixed Desktop/MCP transport. All application behavior lives in a disposable
// business worker; only this bootstrap and the generation loader need reinstall.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ensureDesktopSupervisor, readDesktopSettings, resolveDesktopState } from "./desktop-lifecycle.mjs";
import { PluginRuntime } from "./plugin-runtime.mjs";
import { ensureDesktopStartup } from "./interface/desktop-startup.mjs";

const pluginRoot = path.dirname(fileURLToPath(import.meta.url));
const codexHome = path.resolve(process.env.CODEX_HOME ?? path.join(process.env.USERPROFILE ?? process.env.HOME ?? process.cwd(), ".codex"));
const stateDir = await resolveDesktopState(path.join(codexHome, "state", "codexworker"));
process.env.CODEXWORKER_STATE_DIR = stateDir;
let initialized = false, ended = false, pending = 0, buffer = "", closing = false, lifecycleStartup;
let refreshingLifecycle = false;
let startupRepair, startupRepairError;
async function repairingStartup() {
  startupRepair ??= ensureDesktopStartup({ stateDir }).catch(() => ({ restarting: false, error: "desktop_startup_setup_failed" }));
  const status = await startupRepair;
  if (!status.nativeInputReady) startupRepair = null;
  if (status.error && status.error !== startupRepairError) process.stderr.write("[bridge] " + status.error + "\n");
  startupRepairError = status.error ?? null;
  return status.restarting;
}
const write = message => process.stdout.write(JSON.stringify(message) + "\n");
const runtime = new PluginRuntime({ pluginRoot, stateDir, onToolsChanged: () => {
  if (initialized && !ended) write({ jsonrpc: "2.0", method: "notifications/tools/list_changed" });
} });
async function finish() {
  if (!ended || pending || closing) return;
  closing = true; clearInterval(lifecycleWatch); await lifecycleStartup; await runtime.close(); process.exit(0);
}
// A discovery MCP may retire after handing off its first writer. A live MCP
// rechecks ownership and takes over only through the normal Desktop launcher.
const lifecycleWatch = setInterval(() => {
  if (!initialized || ended || refreshingLifecycle || process.platform !== "darwin" ||
      process.env.RUDOLF_BRIDGE_LIFECYCLE_CHILD === "1") return;
  refreshingLifecycle = true;
  lifecycleStartup = null;
  lifecycleStartup = repairingStartup().then(repairing => repairing ? false : ensureDesktopSupervisor({ pluginRoot, stateDir })).catch(() => false)
    .finally(() => { refreshingLifecycle = false; });
}, 5000);
lifecycleWatch.unref();
async function dispatch(line) {
  pending++;
  try {
    let request; try { request = JSON.parse(line); } catch { return; }
    if (request.id == null) return;
    try {
      // Inventory requests can arrive in the same stdin chunk as initialize.
      // Fence every request before forking a worker so it inherits the final
      // lifecycle gate, never an undefined permission to become a cold writer.
      {
        const settings = await readDesktopSettings(stateDir);
        let guarded = false;
        if (!settings.autoStart) lifecycleStartup = null;
        else if (!await repairingStartup()) {
          lifecycleStartup ??= ensureDesktopSupervisor({ pluginRoot, stateDir }).catch(error => {
            const detail = /^desktop_lifetime_launch_denied:\d{1,5}$/.test(error.message) ? error.message : "desktop_lifecycle_unavailable";
            process.stderr.write("[bridge] " + detail + "\n");
            return false;
          });
          guarded = await lifecycleStartup;
          if (!guarded) lifecycleStartup = null;
        }
        const ready = settings.autoStart && (guarded === true || process.env.RUDOLF_BRIDGE_LIFECYCLE_CHILD === "1");
        process.env.RUDOLF_BRIDGE_LIFECYCLE_READY = ready ? "1" : "0";
        request.params = { ...(request.params ?? {}), __bridgeLifecycleReady: ready, __bridgeAutoStart: settings.autoStart };
      }
      const result = await runtime.call(request.method, request.params ?? {});
      if (request.method === "initialize") initialized = true;
      write({ jsonrpc: "2.0", id: request.id, result });
    } catch (error) { write({ jsonrpc: "2.0", id: request.id, error: { code: -32603, message: error.message } }); }
  } finally { pending--; void finish(); }
}
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => {
  buffer += chunk;
  if (buffer.length > 8 * 1024 * 1024) { process.stdin.destroy(); ended = true; void finish(); return; }
  for (;;) { const end = buffer.indexOf("\n"); if (end < 0) break;
    const line = buffer.slice(0, end).trim(); buffer = buffer.slice(end + 1); if (line) void dispatch(line); }
});
process.stdin.on("end", () => { ended = true; void finish(); });
process.on("SIGTERM", () => { ended = true; void finish(); });
process.on("SIGINT", () => { ended = true; void finish(); });
