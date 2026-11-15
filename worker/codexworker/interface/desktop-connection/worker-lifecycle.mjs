// The primary native connection lasts as long as Desktop, including its home
// screen. Discovery MCP processes can retire without taking this owner down.
import { access, lstat, readFile } from "node:fs/promises";
import path from "node:path";
import { ensureDesktopSupervisor } from "../../desktop-lifecycle.mjs";

export function watchDesktopWorker({ stateDir, env, pluginRoot, ensure = ensureDesktopSupervisor,
  intervalMs = 5000, onError = () => {} } = {}) {
  const controller = new AbortController();
  let running, lastError;
  function check() {
    if (controller.signal.aborted) return Promise.resolve(false);
    if (running) return running;
    running = (async () => {
      try {
        await access(path.join(stateDir, "desktop-config.local.json"));
        await access(path.join(stateDir, "manager-binding.json"));
        if (!pluginRoot) {
          const file = new URL("./worker-root.json", import.meta.url), stat = await lstat(file);
          if (!stat.isFile() || stat.isSymbolicLink() || typeof process.getuid === "function" &&
              (stat.uid !== process.getuid() || stat.mode & 0o077)) throw Error("desktop_worker_root_not_private");
          const value = JSON.parse(await readFile(file, "utf8"));
          if (value.version !== 1 || !path.isAbsolute(value.pluginRoot ?? "")) throw Error("desktop_worker_root_invalid");
          pluginRoot = value.pluginRoot;
        }
        if (controller.signal.aborted) return false;
        const ready = await ensure({ pluginRoot, stateDir, env, signal: controller.signal });
        lastError = null;
        return ready;
      } catch (error) {
        if (!controller.signal.aborted && error.code !== "ENOENT" && error.message !== lastError) {
          lastError = error.message; onError(error);
        }
        return false;
      }
    })().finally(() => { running = null; });
    return running;
  }
  const timer = setInterval(() => { void check(); }, intervalMs); timer.unref();
  void check();
  return { check, async close() { clearInterval(timer); controller.abort(); await running; } };
}
