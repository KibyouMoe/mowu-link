// One Node transport supervisor per real Desktop lifetime, never a model host.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { appendFile, mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DesktopLifecycle, desktopOwner, processChain, resolveDesktopOwner } from "../desktop-lifecycle.mjs";

const args = process.argv.slice(2);
if (args[0] !== "--desktop-pid" || !/^\d+$/.test(args[1] ?? "") || !process.env.CODEX_APP_TOOLS_PIPE_PATH) {
  throw new Error("Requires the actual Desktop PID and its already injected native pipe.");
}
const desktopPid = Number(args[1]);
const owner = await resolveDesktopOwner(desktopPid);
if (!owner || owner.pid !== desktopPid ||
    process.env.RUDOLF_BRIDGE_DESKTOP_STARTED_AT && process.env.RUDOLF_BRIDGE_DESKTOP_STARTED_AT !== owner.startedAt) {
  throw new Error("The native pipe must belong to the verified live Codex Desktop.");
}
const pluginRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const stateRoot = path.join(process.env.USERPROFILE ?? process.env.HOME ?? os.homedir(), ".codex", "state", "codexworker");
const config = JSON.parse(await readFile(path.join(stateRoot, "desktop-config.local.json"), "utf8"));
if ((config.host ?? "127.0.0.1") !== "127.0.0.1") throw new Error("This activation only supports loopback.");
const token = (await readFile(config.tokenFile, "utf8")).trim();
if (token.length < 24) throw new Error("A configured strong machine-local credential is required.");
const endpoint = "http://127.0.0.1:" + (config.port ?? 8903);
const leaseKey = createHash("sha256").update(JSON.stringify([path.resolve(stateRoot).toLowerCase(), desktopPid, owner.startedAt])).digest("hex").slice(0, 24);
const lease = net.createServer(socket => socket.end());
try {
  await new Promise((resolve, reject) => { lease.once("error", reject);
    lease.listen("\\\\.\\pipe\\codexworker-desktop-lifecycle-" + leaseKey, resolve); });
} catch (error) { if (error.code === "EADDRINUSE") process.exit(0); throw error; }
await mkdir(stateRoot, { recursive: true });
let statusWrite = Promise.resolve();
const log = value => {
  const status = { ...value, supervisorPid: process.pid, desktopPid, desktopStartedAt: owner.startedAt,
    version: "0.2.23", watchdogVersion: "0.2.27", launchMode: process.env.RUDOLF_BRIDGE_LIFETIME_LAUNCH ?? "legacy-node-detached", updatedAt: new Date().toISOString() };
  // Bounded metadata only; no raw stderr, pipe, credentials or request payloads.
  statusWrite = statusWrite.then(async () => {
    const target = path.join(stateRoot, "desktop-lifecycle-status.json");
    const tmp = target + "." + process.pid + ".tmp";
    await writeFile(tmp, JSON.stringify(status), { mode: 0o600 }); await rename(tmp, target);
    const journal = path.join(stateRoot, "desktop-lifecycle-events.jsonl");
    // Keep bounded lifecycle evidence across short inventory connections. No
    // payloads, credentials, pipe handles or raw exceptions enter this journal.
    if (await stat(journal).then(s => s.size >= 65536, () => false)) {
      await rename(journal, journal + ".previous");
    }
    await appendFile(journal, JSON.stringify(status) + "\n", { mode: 0o600 });
  }).catch(() => {});
  // The launcher exits before this long-lived process; its stdout is not a
  // lifecycle channel. Persist diagnostics without writing a retired pipe.
};
async function stopChild(child) {
  if (child.exitCode !== null) return;
  await new Promise(resolve => {
    const limit = setTimeout(() => { child.kill(); resolve(); }, 5000);
    child.once("exit", () => { clearTimeout(limit); resolve(); }); child.stdin.end();
  });
}
const lifecycle = new DesktopLifecycle({
  desktopAlive: async () => {
    const now = desktopOwner(await processChain(desktopPid));
    return now?.pid === desktopPid && now.startedAt === owner.startedAt && now.executable === owner.executable;
  },
  health: async () => {
    const response = await fetch(endpoint + "/health", {
      headers: { authorization: "Bearer " + token }, signal: AbortSignal.timeout(3000) });
    if (!response.ok) return null;
    const result = await response.json();
    return result.name === (config.bridgeName ?? "CodexWorker") ? result : null;
  },
  launch: () => {
    const child = spawn(process.execPath, [path.join(pluginRoot, "server.mjs")], {
      cwd: pluginRoot, env: { ...process.env, RUDOLF_BRIDGE_LIFECYCLE_CHILD: "1" },
      stdio: ["pipe", "pipe", "ignore"], windowsHide: true });
    let output = "";
    child.stdout.setEncoding("utf8"); child.stdout.on("data", chunk => {
      output += chunk;
      if (output.length > 1024 * 1024) { child.kill(); return; }
      for (;;) {
        const end = output.indexOf("\n"); if (end < 0) break;
        const line = output.slice(0, end); output = output.slice(end + 1);
        try { const response = JSON.parse(line); if (response.error) child.kill(); }
        catch { child.kill(); }
      }
    });
    child.stdin.on("error", () => child.kill());
    child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {
      protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "desktop-bridge-lifecycle", version: "0.2.23" } } }) + "\n");
    child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
    return child;
  }, stopChild, log });
let stopping = false;
async function shutdown() {
  if (stopping) return; stopping = true; clearInterval(watchdog);
  await lifecycle.close(); await statusWrite;
  await new Promise(resolve => lease.close(resolve));
}
const watchdog = setInterval(() => { void lifecycle.tick().then(() => {
  if (lifecycle.closed) return shutdown();
}).catch(() => { log({ stage: "supervisor_tick_failed" }); }); }, 2000);
process.on("SIGINT", () => { void shutdown(); });
process.on("SIGTERM", () => { void shutdown(); });
await lifecycle.tick().catch(() => { log({ stage: "supervisor_tick_failed" }); });
