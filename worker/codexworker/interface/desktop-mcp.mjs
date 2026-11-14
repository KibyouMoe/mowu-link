// Select the runtime supplied by Desktop before loading the MCP bootstrap.
// This file is an MCP launcher and does not perform native tool requests.
import { spawn } from "node:child_process";
import { realpath } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const serverUrl = new URL("../server.mjs", import.meta.url);

export async function launchDesktopMcp({ env = process.env, platform = process.platform,
  currentExecutable = process.execPath, canonical = realpath, replaceProcess = process.execve,
  spawnImpl = spawn, loadServer = () => import(serverUrl.href) } = {}) {
  const supplied = env.CODEX_MCP_NODE_PATH?.trim();
  if (!supplied) {
    // Preserve older hosts and isolated diagnostics without a runtime hint.
    await loadServer(); return;
  }
  const paths = platform === "win32" ? path.win32 : path.posix;
  if (!paths.isAbsolute(supplied)) throw new Error("desktop_node_runtime_not_absolute");
  const runtime = await canonical(supplied);
  if (runtime === await canonical(currentExecutable)) { await loadServer(); return; }
  const server = fileURLToPath(serverUrl);
  if (platform !== "win32" && typeof replaceProcess === "function") {
    replaceProcess(runtime, [runtime, server], env);
    return;
  }
  // Windows has no execve. Keep the ordinary MCP parent alive for the lifetime
  // of the selected runtime and forward its termination request.
  const child = spawnImpl(runtime, [server], { stdio: "inherit", env, windowsHide: true });
  const stop = () => child.kill("SIGTERM");
  process.once("SIGTERM", stop); process.once("SIGINT", stop);
  try {
    return await new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("exit", code => resolve(code ?? 1));
    });
  } finally {
    process.removeListener("SIGTERM", stop); process.removeListener("SIGINT", stop);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  launchDesktopMcp().then(code => { if (typeof code === "number") process.exitCode = code; }, error => {
    process.stderr.write("[codexworker] " + error.message + "\n"); process.exitCode = 1;
  });
}
