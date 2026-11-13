// HTTP stays alive across short MCP connections, not across Desktop lifetimes.
import { once } from "node:events";
import { execFile, spawn } from "node:child_process";
import { access, readFile, realpath, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
export const DESKTOP_BOOTSTRAP_VERSION = "0.3.0";
export const desktopLaunchMode = platform => platform === "win32" ? "windows-desktop-parent" : "desktop-owned";

// Only the child handle created by this supervisor is eligible for shutdown.
// The grace period belongs to adapter shutdown, never to a model task.
export async function stopOwnedChild(child, { graceMs = 5000 } = {}) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  await new Promise(resolve => {
    const limit = setTimeout(() => { child.kill("SIGKILL"); }, graceMs);
    child.once("exit", () => { clearTimeout(limit); resolve(); });
    child.stdin?.end();
  });
}

export async function resolveDesktopState(defaultState, env = process.env) {
  if (env.CODEXWORKER_STATE_DIR) return path.resolve(env.CODEXWORKER_STATE_DIR);
  let settings;
  try { settings = JSON.parse(await readFile(path.join(defaultState, "worker-instance.local.json"), "utf8")); }
  catch (error) { if (error.code === "ENOENT") return defaultState; throw new Error("desktop_state_invalid"); }
  if (typeof settings?.stateDir !== "string" || !path.isAbsolute(settings.stateDir)) throw new Error("desktop_state_invalid");
  return realpath(settings.stateDir);
}

export async function readDesktopSettings(stateDir) {
  let config;
  try { config = JSON.parse(await readFile(path.join(stateDir, "desktop-config.local.json"), "utf8")); }
  catch (error) { if (error.code === "ENOENT") return { autoStart: true }; throw new Error("desktop_settings_invalid"); }
  if (!config || typeof config !== "object" || Array.isArray(config) ||
      Object.hasOwn(config, "autoStart") && typeof config.autoStart !== "boolean") throw new Error("desktop_autostart_invalid");
  return { autoStart: config.autoStart !== false };
}

export async function resolveLifecycleRoot(pluginRoot, stateDir) {
  if (!stateDir) return pluginRoot;
  let sidecar;
  try { sidecar = JSON.parse(await readFile(path.join(stateDir, "runtime-source.local.json"), "utf8")); }
  catch (error) { if (error.code === "ENOENT") return pluginRoot; throw new Error("desktop_lifecycle_source_invalid"); }
  if (typeof sidecar.sourceRoot !== "string" || !path.isAbsolute(sidecar.sourceRoot)) throw new Error("desktop_lifecycle_source_invalid");
  // The existing trusted machine sidecar names the user-owned install source.
  // Keep the lifetime supervisor and its children outside disposable app caches.
  const root = await realpath(sidecar.sourceRoot);
  for (const entry of ["server.mjs", "desktop-lifecycle.mjs", "plugin-runtime.mjs", "business-runtime.mjs",
    "scripts/run-desktop-bridge.mjs", "scripts/business-worker.mjs", "scripts/launch-desktop-lifetime.ps1"]) {
    const actual = await realpath(path.join(root, entry));
    if (actual !== path.join(root, entry)) throw new Error("desktop_lifecycle_source_escape");
  }
  return root;
}

export async function launchDesktopSupervisor({ pluginRoot, stateDir, owner, env, role = "bridge", exec = execFileAsync, platform = process.platform, spawnImpl = spawn }) {
  if (!["bridge", "watchdog"].includes(role)) throw new Error("desktop_lifetime_role_invalid");
  pluginRoot = await resolveLifecycleRoot(pluginRoot, stateDir);
  if (platform === "darwin") {
    const child = spawnImpl(process.execPath, [path.join(pluginRoot, "scripts", role === "watchdog" ? "run-script-watchdog.mjs" : "run-desktop-bridge.mjs"), "--desktop-pid", String(owner.pid)], {
      cwd: pluginRoot, env: { ...env, RUDOLF_BRIDGE_DESKTOP_STARTED_AT: owner.startedAt,
        RUDOLF_BRIDGE_LIFETIME_LAUNCH: desktopLaunchMode(platform) }, detached: true, stdio: "ignore" });
    await once(child, "spawn"); child.unref();
    return { supervisorPid: child.pid, launchMode: desktopLaunchMode(platform) };
  }
  if (platform !== "win32") throw new Error("desktop_platform_unsupported");
  let stdout;
  try { ({ stdout } = await exec(path.join(env.SystemRoot ?? "C:/Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
    ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned", "-File", path.join(pluginRoot, "scripts", "launch-desktop-lifetime.ps1"),
      process.execPath, String(owner.pid), role], {
      cwd: pluginRoot, env: { ...env, RUDOLF_BRIDGE_DESKTOP_STARTED_AT: owner.startedAt },
      windowsHide: true, timeout: 12000, maxBuffer: 65536 })); }
  catch (error) {
    const denied = /desktop_lifetime_launch_denied:(\d{1,5})/.exec(error.stderr ?? "");
    throw new Error(denied ? denied[0] : "desktop_lifetime_launch_unavailable");
  }
  const launched = JSON.parse(stdout.trim());
  if (!Number.isSafeInteger(launched.supervisorPid) || launched.supervisorPid < 1 || launched.launchMode !== "windows-desktop-parent") {
    throw new Error("desktop_lifetime_launch_invalid");
  }
  return launched;
}

export async function ensureScriptWatchdog({ pluginRoot, stateDir, owner, env = process.env,
  launch = launchDesktopSupervisor, readChain = processChain, delay = ms => new Promise(resolve => setTimeout(resolve, ms)) }) {
  let config;
  try { config = JSON.parse(await readFile(path.join(stateDir, "script-watchdog.local.json"), "utf8")); }
  catch (error) { if (error.code === "ENOENT") return false; throw new Error("script_watchdog_config_invalid"); }
  if (config.enabled !== true) return false;
  if (!/^[a-f\d-]{36}$/i.test(config.maintenanceThreadId ?? "")) throw new Error("script_watchdog_target_invalid");
  const ready = async () => {
    try {
      const status = JSON.parse(await readFile(path.join(stateDir, "script-watchdog-status.json"), "utf8"));
      if (status.desktopPid !== owner.pid || status.desktopStartedAt !== owner.startedAt || status.stage !== "running" || status.version !== 1) return false;
      if (!Number.isSafeInteger(status.pid) || status.pid < 1) return false;
      const chain = await readChain(status.pid);
      return chain[0]?.pid === status.pid && chain[0]?.startedAt === status.processStartedAt &&
        chain[1]?.pid === owner.pid && chain[1]?.startedAt === owner.startedAt;
    } catch { return false; }
  };
  if (await ready()) return true;
  await launch({ pluginRoot, stateDir, owner, env, role: "watchdog" });
  for (let attempt = 0; attempt < 30; attempt++) { if (await ready()) return true; await delay(200); }
  throw new Error("script_watchdog_startup_pending");
}
export function desktopOwner(chain) {
  return [...chain].reverse().find(p => Number.isSafeInteger(p.pid) && p.pid > 0 && typeof p.startedAt === "string" && (
    p.name?.toLowerCase() === "chatgpt.exe" && /[\\/]OpenAI\.Codex_[^\\/]+[\\/]app[\\/]ChatGPT\.exe$/i.test(p.executable ?? "") ||
    /\/(Codex|ChatGPT)\.app\/Contents\/MacOS\/(Codex|ChatGPT)$/.test(p.executable ?? "")
  )) ?? null;
}

export async function processChain(pid, { exec = execFileAsync, platform = process.platform } = {}) {
  if (!Number.isSafeInteger(pid) || pid < 1) return [];
  if (platform === "darwin") {
    const chain = [], seen = new Set();
    for (let depth = 0; depth < 16 && pid > 0 && !seen.has(pid); depth++) {
      seen.add(pid);
      let stdout;
      try { ({ stdout } = await exec("/bin/ps", ["-p", String(pid), "-o", "pid=,ppid=,lstart=,comm="], { timeout: 8000, maxBuffer: 65536 })); }
      catch (error) {
        if (error.code === 1 && !error.stdout?.trim() && !error.stderr?.trim()) break; // ps uses exit 1 for a process which has exited.
        throw error; // A permission/inspection failure is still unknown, not proof of exit.
      }
      const match = stdout.trim().match(/^(\d+)\s+(\d+)\s+(.{24})\s+(.+)$/);
      if (!match) break;
      chain.push({ pid: Number(match[1]), startedAt: match[3], executable: match[4], name: path.basename(match[4]) });
      pid = Number(match[2]);
    }
    return chain;
  }
  if (platform !== "win32") return [];
  // Only a validated integer enters this fixed read-only command.
  const script = "$ErrorActionPreference='Stop'; $bridgeInspectPid=" + pid + "; $bridgeSeen=@{}; $bridgeChain=@(); " +
    "for($bridgeDepth=0;$bridgeDepth -lt 16 -and $bridgeInspectPid -gt 0;$bridgeDepth++){ " +
    "if($bridgeSeen.ContainsKey($bridgeInspectPid)){break}; $bridgeSeen[$bridgeInspectPid]=$true; " +
    "$bridgeProcess=Get-CimInstance Win32_Process -Filter ('ProcessId='+$bridgeInspectPid); " +
    "if(!$bridgeProcess){break}; $bridgeChain+=@{pid=[int]$bridgeProcess.ProcessId;name=$bridgeProcess.Name;" +
    "executable=$bridgeProcess.ExecutablePath;startedAt=$bridgeProcess.CreationDate.ToUniversalTime().ToString('o')}; " +
    "$bridgeInspectPid=[int]$bridgeProcess.ParentProcessId }; ConvertTo-Json -Compress -InputObject @($bridgeChain)";
  const { stdout } = await exec(path.join(process.env.SystemRoot ?? "C:/Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
    ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], { windowsHide: true, timeout: 8000, maxBuffer: 65536 });
  return JSON.parse(stdout.trim() || "[]");
}

export async function resolveDesktopOwner(pid, { readChain = processChain,
  delay = ms => new Promise(resolve => setTimeout(resolve, ms)) } = {}) {
  for (let attempt = 0; attempt < 3; attempt++) {
    try { const owner = desktopOwner(await readChain(pid)); if (owner) return owner; }
    catch { /* Cold Windows process queries may fail transiently. Never guess an owner. */ }
    if (attempt < 2) await delay(200);
  }
  return null;
}

export async function desktopSupervisorReady(stateDir, owner, { platform = process.platform, fetchImpl = fetch, alive = pid => {
  try { process.kill(pid, 0); return true; } catch { return false; }
} } = {}) {
  try {
    const status = JSON.parse(await readFile(path.join(stateDir, "desktop-lifecycle-status.json"), "utf8"));
    if (status.desktopPid !== owner.pid || status.desktopStartedAt !== owner.startedAt ||
        status.version !== DESKTOP_BOOTSTRAP_VERSION || status.launchMode !== desktopLaunchMode(platform) ||
        !Number.isSafeInteger(status.supervisorPid) || status.supervisorPid < 1 || !alive(status.supervisorPid) ||
        !["desktop_lifetime_bridge_active", "observing_managed_writer"].includes(status.stage)) return false;
    const config = JSON.parse(await readFile(path.join(stateDir, "desktop-config.local.json"), "utf8"));
    if ((config.host ?? "127.0.0.1") !== "127.0.0.1") return false;
    const token = (await readFile(config.tokenFile, "utf8")).trim();
    if (token.length < 24) return false;
    const response = await fetchImpl("http://127.0.0.1:" + (config.port ?? 8903) + "/health", {
      headers: { authorization: "Bearer " + token }, signal: AbortSignal.timeout(1500) });
    if (!response.ok) return false;
    const health = await response.json();
    return health.ready === true && health.integrationMode === "desktop-native" &&
      health.name === (config.bridgeName ?? "CodexWorker") && health.integrations?.pid === status.writerPid &&
      health.capabilities?.desktopStartupHandoff?.active === true &&
      health.capabilities?.businessHotReload?.bootstrapVersion === DESKTOP_BOOTSTRAP_VERSION &&
      health.capabilities?.businessHotReload?.desktopLifetimeLaunchMode === desktopLaunchMode(platform);
  } catch { return false; } // Stale metadata or a socket alone is never readiness.
}

export async function ensureDesktopSupervisor({ pluginRoot, stateDir, env = process.env,
  platform = process.platform, readChain = processChain, launchImpl = launchDesktopSupervisor,
  ready = desktopSupervisorReady, clock = Date.now, ensureWatchdog = ensureScriptWatchdog,
  delay = ms => new Promise(resolve => setTimeout(resolve, ms)), timeoutMs = 40000 } = {}) {
  if (!(await readDesktopSettings(stateDir)).autoStart) return false;
  if (!["win32", "darwin"].includes(platform) || !env.CODEX_APP_TOOLS_PIPE_PATH || env.RUDOLF_BRIDGE_LIFECYCLE_CHILD === "1") return false;
  // Never persist/reconstruct a pipe or register an OS-login job with old handles.
  await access(path.join(stateDir, "desktop-config.local.json"));
  await access(path.join(stateDir, "manager-binding.json"));
  const deadline = clock() + timeoutMs;
  const owner = await resolveDesktopOwner(process.pid, { readChain, delay });
  if (!owner) return false;
  // The independent audit scheduler must not depend on HTTP/startup readiness:
  // otherwise a slow startup can launch a writer but skip its watchdog forever.
  if (platform === "win32") await ensureWatchdog({ pluginRoot, stateDir, owner, env }).catch(() => {});
  if (await ready(stateDir, owner, { platform })) {
    await resumeStartupMaintenance(stateDir);
    return true;
  }
  // Even an allowed breakaway can remain in an outer inventory cleanup job.
  // Create the supervisor under the verified Desktop itself, retaining its job
  // and token. OS denial has no alternate-parent or cold-MCP writer fallback.
  await launchImpl({ pluginRoot, stateDir, owner, env, platform });
  // Inventory clients may tear down the MCP process immediately after its
  // initialize response. Finish the durable supervisor handoff BEFORE replying,
  // and before this short-lived process can become the HTTP/Feishu writer.
  while (clock() < deadline) {
    if (await ready(stateDir, owner, { platform })) {
      await resumeStartupMaintenance(stateDir);
      return true;
    }
    await delay(200);
  }
  throw new Error("desktop_lifecycle_not_ready");
}

async function resumeStartupMaintenance(stateDir) {
  // A deployment may keep an older ordinary-MCP writer serving while the new
  // fixed files wait for normal loading. Restore ONLY its explicit maintenance
  // marker after authenticated, owner-matched startup acknowledgement above.
  const target = path.join(stateDir, "runtime-source.local.json");
  let tmp;
  try {
    const original = await readFile(target, "utf8"), value = JSON.parse(original);
    if (value.autoReload !== false || value.resumeAutoReloadAfterBootstrap !== "0.2.23") return;
    value.autoReload = true; delete value.resumeAutoReloadAfterBootstrap;
    tmp = target + ".startup-" + process.pid + "-" + Date.now() + ".tmp";
    await writeFile(tmp, JSON.stringify(value, null, 2) + "\n", { flag: "wx", mode: 0o600 });
    if (await readFile(target, "utf8") !== original) return; // Preserve a concurrent user edit.
    await rename(tmp, target); tmp = null;
  } catch { /* A sidecar write failure must not stop the acknowledged writer. */ }
  finally { if (tmp) await unlink(tmp).catch(() => {}); }
}

export class DesktopLifecycle {
  constructor({ desktopAlive, health, launch, stopChild, enabled = async () => true, clock = Date.now, log = () => {} }) {
    Object.assign(this, { desktopAlive, health, launch, stopChild, enabled, clock, log });
    this.child = null; this.closed = false; this.retryAt = 0; this.backoff = 1000;
    this.stage = null; this.tickRunning = false;
  }
  report(stage, detail = {}) {
    const key = JSON.stringify({ stage, ...detail });
    if (key === this.stageKey) return;
    this.stage = stage; this.stageKey = key; this.log({ stage, ...detail });
  }
  async tick() {
    if (this.closed || this.tickRunning) return;
    this.tickRunning = true;
    try {
      let ownerAlive;
      try { ownerAlive = await this.desktopAlive(); }
      catch {
        // A transient CIM timeout is not evidence that Desktop exited. Do not
        // stop a healthy owned adapter (or launch another) on an unknown result.
        this.report("desktop_owner_check_pending"); return;
      }
      if (!ownerAlive) { await this.close(); return; }
      if (!await this.enabled()) {
        if (this.child) { const owned = this.child; this.child = null; await this.stopChild(owned); }
        this.report("desktop_autostart_disabled"); return;
      }
      let live = null;
      try { live = await this.health(); } catch { /* Never retry native message writes. */ }
      if (live?.ready && live.integrationMode === "desktop-native") {
        this.unhealthySince = null;
        this.backoff = 1000;
        const pid = live.integrations?.pid;
        if (this.child && pid && pid !== this.child.pid && live.integrations?.runtimeHostPid !== this.child.pid) {
          const proxy = this.child; this.child = null; await this.stopChild(proxy);
        }
        this.report(this.child ? "desktop_lifetime_bridge_active" : "observing_managed_writer", { writerPid: pid });
        return;
      }
      if (this.child && this.unhealthySince == null) this.unhealthySince = this.clock();
      // A brief business handoff after hours of uptime gets the same continuous
      // grace as startup; process age is not evidence of a sustained outage.
      if (this.child && this.clock() - this.unhealthySince > 45000) {
        const expired = this.child; this.child = null; await this.stopChild(expired);
        this.retryAt = this.clock() + this.backoff; this.backoff = Math.min(30000, this.backoff * 2);
      }
      if (!this.child && this.clock() >= this.retryAt) {
        this.report("starting_desktop_adapter");
        const child = this.launch(); this.child = child; this.launchedAt = this.clock();
        this.unhealthySince = this.launchedAt;
        const exited = (code, signal) => {
          if (this.child !== child) return;
          this.child = null; this.retryAt = this.clock() + this.backoff;
          this.backoff = Math.min(30000, this.backoff * 2);
          this.report("adapter_retry_pending", { adapterPid: child.pid,
            exitCode: Number.isInteger(code) ? code : null,
            signal: typeof signal === "string" && /^SIG[A-Z]+$/.test(signal) ? signal : null });
        };
        child.once("exit", exited); child.once("error", exited);
      }
    } finally { this.tickRunning = false; }
  }
  async close() {
    if (this.closed) return;
    this.closed = true;
    if (this.child) { const child = this.child; this.child = null; await this.stopChild(child); }
    this.report("desktop_lifecycle_stopped");
  }
}
