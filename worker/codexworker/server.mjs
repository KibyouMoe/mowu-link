// Fixed Desktop/MCP transport. All application behavior lives in a disposable
// business worker; only this bootstrap and the generation loader need reinstall.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ensureDesktopSupervisor } from "./desktop-lifecycle.mjs";
import { PluginRuntime } from "./plugin-runtime.mjs";

const pluginRoot = path.dirname(fileURLToPath(import.meta.url));
// CodexWorker copy: the state directory is configurable so a CLI run never
// shares the Desktop plugin's durable writer state by accident.
const stateDir = process.env.CODEXWORKER_STATE_DIR
  ?? path.join(process.env.USERPROFILE ?? process.env.HOME ?? process.cwd(), ".codex", "state", "codexworker");
// Keep the OS process table attributable to this copy.
process.title = "CodexWorker";
let initialized = false, ended = false, pending = 0, buffer = "", closing = false, lifecycleStartup;
const write = message => process.stdout.write(JSON.stringify(message) + "\n");
const runtime = new PluginRuntime({ pluginRoot, stateDir, onToolsChanged: () => {
  if (initialized && !ended) write({ jsonrpc: "2.0", method: "notifications/tools/list_changed" });
} });
async function finish() {
  if (!ended || pending || closing) return;
  closing = true; await lifecycleStartup; await runtime.close(); process.exit(0);
}
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
        lifecycleStartup ??= ensureDesktopSupervisor({ pluginRoot, stateDir }).catch(error => {
          const detail = /^desktop_lifetime_launch_denied:\d{1,5}$/.test(error.message) ? error.message : "desktop_lifecycle_unavailable";
          process.stderr.write("[CodexWorker] " + detail + "\n");
          return false;
        });
        const guarded = await lifecycleStartup;
        const ready = guarded === true || process.env.CODEXWORKER_LIFECYCLE_CHILD === "1";
        process.env.CODEXWORKER_LIFECYCLE_READY = ready ? "1" : "0";
        request.params = { ...(request.params ?? {}), __bridgeLifecycleReady: ready };
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
