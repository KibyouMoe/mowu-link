// Native input setup belongs to the Desktop plugin. A missed login environment
// may request one normal host reopen per boot; it never starts an engine/worker.
import { execFile, spawn } from "node:child_process";
import { once } from "node:events";
import { randomUUID } from "node:crypto";
import { access, link, lstat, mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { desktopOwner, processChain, readDesktopSettings } from "../desktop-lifecycle.mjs";
import { installDesktopEnvironment } from "./desktop-connection/install-environment.mjs";

const exec = promisify(execFile);
const modulePath = fileURLToPath(import.meta.url);
const bootPattern = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const pending = new Set(["preparing", "scheduled", "reopening"]);

async function readJson(file) {
  try { return JSON.parse(await readFile(file, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return null; throw error; }
}
async function save(file, record) {
  const temporary = file + "." + randomUUID() + ".tmp";
  await writeFile(temporary, JSON.stringify(record) + "\n", { flag: "wx", mode: 0o600 });
  await rename(temporary, file);
}
async function claim(file, record) {
  const temporary = file + "." + randomUUID() + ".tmp";
  await writeFile(temporary, JSON.stringify(record) + "\n", { flag: "wx", mode: 0o600 });
  try { await link(temporary, file); return true; }
  catch (error) { if (error.code === "EEXIST") return false; throw error; }
  finally { await unlink(temporary); }
}
async function privateRecord(file) {
  const stat = await lstat(file);
  if (!stat.isFile() || stat.isSymbolicLink() ||
      typeof process.getuid === "function" && (stat.uid !== process.getuid() || stat.mode & 0o077))
    throw Error("desktop_startup_record_not_private");
  return JSON.parse(await readFile(file, "utf8"));
}
async function inputReady(env, owner, readChain) {
  if (env.CODEXWORKER_NATIVE_IMAGE_INPUT !== "1" || env.CODEXWORKER_NATIVE_QUESTION_INPUT !== "1" ||
      !env.CODEXWORKER_NATIVE_INPUT_DESCRIPTOR) return false;
  try {
    const descriptor = await privateRecord(env.CODEXWORKER_NATIVE_INPUT_DESCRIPTOR);
    if (descriptor.version !== 1 || descriptor.desktopOwner?.pid !== owner.pid ||
        descriptor.desktopOwner.startedAt !== owner.startedAt) return false;
    const chain = await readChain(descriptor.controllerPid);
    return chain[0]?.pid === descriptor.controllerPid && chain[1]?.pid === owner.pid &&
      chain[1]?.startedAt === owner.startedAt;
  } catch { return false; }
}

export async function ensureDesktopStartup({ stateDir, env = process.env, platform = process.platform,
  readChain = processChain, bootSession = async () => (await exec("/usr/sbin/sysctl", ["-n", "kern.bootsessionuuid"])).stdout.trim(),
  install = installDesktopEnvironment, reopen = async file => {
    const child = spawn(process.execPath, [modulePath, "--reopen", file], {
      detached: true, stdio: "ignore", env });
    await once(child, "spawn"); child.unref();
  } } = {}) {
  if (platform !== "darwin" || env.CODEXWORKER_NATIVE_BOOTSTRAP !== "1" ||
      !env.CODEX_APP_TOOLS_PIPE_PATH || env.RUDOLF_BRIDGE_LIFECYCLE_CHILD === "1")
    return { restarting: false };
  if (!(await readDesktopSettings(stateDir)).autoStart) return { restarting: false };
  const binding = await readJson(path.join(stateDir, "manager-binding.json"));
  if (!binding?.managerThreadId) return { restarting: false };
  const caller = await readChain(process.pid);
  const owner = desktopOwner(caller);
  if (!owner) return { restarting: false };
  const boot = await bootSession();
  if (!bootPattern.test(boot)) throw Error("desktop_boot_identity_invalid");
  const directory = path.join(stateDir, "native-startup");
  const file = path.join(directory, boot + "-v1.json");
  let previous = await readJson(file);
  if (previous) previous = await privateRecord(file);
  if (await inputReady(env, owner, readChain)) {
    if (previous && previous.status !== "restored") await save(file, { ...previous,
      status: "restored", restoredDesktopPid: owner.pid, restoredAt: new Date().toISOString() });
    return { restarting: false, nativeInputReady: true };
  }
  if (previous) {
    if (previous.desktopPid !== owner.pid || previous.desktopStartedAt !== owner.startedAt)
      return { restarting: false, error: "desktop_startup_reopen_unconfirmed" };
    if (pending.has(previous.status)) {
      const pid = previous.status === "reopening" ? previous.helperPid : previous.creatorPid;
      const startedAt = previous.status === "reopening" ? previous.helperStartedAt : previous.creatorStartedAt;
      let actor;
      try { actor = (await readChain(pid))[0]; }
      catch { return { restarting: true, error: "desktop_startup_actor_inspection_unavailable" }; }
      if (actor?.pid !== pid || actor.startedAt !== startedAt) {
        await save(file, { ...previous, status: "failed", error: "desktop_startup_actor_exited" });
        return { restarting: false, error: "desktop_startup_actor_exited" };
      }
    }
    return { restarting: pending.has(previous.status), error: previous.error ?? null };
  }
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const directoryInfo = await lstat(directory);
  if (!directoryInfo.isDirectory() || directoryInfo.isSymbolicLink() || typeof process.getuid === "function" &&
      (directoryInfo.uid !== process.getuid() || directoryInfo.mode & 0o077)) throw Error("desktop_startup_directory_not_private");
  const appPath = path.posix.resolve(path.posix.dirname(owner.executable), "../..");
  const engine = caller.find(process => path.basename(process.executable ?? "") === "codex");
  if (!engine) return { restarting: false, error: "desktop_native_engine_identity_unknown" };
  const record = { version: 1, boot, stateDir, appPath, desktopPid: owner.pid,
    ...(env.CODEX_HOME ? { codexHome: path.resolve(env.CODEX_HOME) } : {}),
    desktopStartedAt: owner.startedAt, threadId: binding.managerThreadId,
    creatorPid: caller[0].pid, creatorStartedAt: caller[0].startedAt,
    enginePid: engine.pid, engineStartedAt: engine.startedAt,
    status: "preparing", createdAt: new Date().toISOString() };
  if (!await claim(file, record)) {
    previous = await privateRecord(file);
    return { restarting: pending.has(previous.status), error: previous.error ?? null };
  }
  try {
    const settings = path.join(stateDir, "desktop-native-connection.json");
    if (!await readJson(settings)) {
      let realCli;
      for (const relative of ["codex-cli/CodexCLI.app/Contents/MacOS/codex", "codex"]) {
        const candidate = path.join(appPath, "Contents/Resources", relative);
        try { await access(candidate); realCli = candidate; break; }
        catch (error) { if (error.code !== "ENOENT") throw error; }
      }
      if (!realCli) throw Error("desktop_native_cli_not_found");
      await claim(settings, { version: 1, realCli });
    }
    const result = await install({ env });
    if (!(await readDesktopSettings(stateDir)).autoStart) {
      await save(file, { ...record, status: "disabled" }); return { restarting: false };
    }
    await save(file, { ...record, status: "scheduled", launcher: result.launcher });
    await reopen(file);
    return { restarting: true };
  } catch (error) {
    await save(file, { ...record, status: "failed", error: "desktop_startup_setup_failed" });
    throw error;
  }
}

// AppKit requests ordinary termination, waits for the app to finish, and never
// force-kills a host which declines or needs the user's attention.
const quitScript = `ObjC.import('AppKit');
function run(args) {
  var pid = Number(args[0]), appPath = args[1];
  var app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(pid);
  if (!app || app.isNil()) return 'already-exited';
  if (ObjC.unwrap(app.bundleURL.path) !== appPath) throw Error('desktop_owner_changed');
  if (!app.terminate) throw Error('desktop_normal_quit_declined');
  var until = Date.now() + 30000;
  while (!app.terminated && Date.now() < until) {
    $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.1));
  }
  if (!app.terminated) throw Error('desktop_normal_quit_pending');
  return 'terminated';
}`;

export async function reopenDesktop(file, { platform = process.platform, readChain = processChain,
  execute = exec } = {}) {
  if (platform !== "darwin") throw Error("desktop_reopen_platform_unsupported");
  let record = await privateRecord(file);
  if (record.version !== 1 || record.status !== "scheduled" || !bootPattern.test(record.boot ?? "") ||
      !Number.isSafeInteger(record.desktopPid) || record.desktopPid < 1 ||
      !Number.isSafeInteger(record.enginePid) || record.enginePid < 1 || typeof record.engineStartedAt !== "string" ||
      !path.isAbsolute(record.launcher ?? "") || !path.isAbsolute(record.appPath ?? "") ||
      !path.isAbsolute(record.stateDir ?? "") || record.codexHome != null &&
      (typeof record.codexHome !== "string" || !path.isAbsolute(record.codexHome))) throw Error("desktop_reopen_record_invalid");
  if (path.resolve(file) !== path.join(record.stateDir, "native-startup", record.boot + "-v1.json"))
    throw Error("desktop_reopen_record_location_invalid");
  if (path.dirname(path.dirname(record.launcher)) !== path.join(record.stateDir, "desktop-connection") ||
      !/^[a-f0-9]{64}$/.test(path.basename(path.dirname(record.launcher))) || path.basename(record.launcher) !== "codex")
    throw Error("desktop_reopen_launcher_invalid");
  let operation = "verify_owner";
  try {
    const owner = desktopOwner(await readChain(record.desktopPid));
    if (owner?.pid !== record.desktopPid || owner.startedAt !== record.desktopStartedAt ||
        path.posix.resolve(path.posix.dirname(owner.executable), "../..") !== record.appPath)
      throw Error("desktop_reopen_owner_changed");
    if (!(await readDesktopSettings(record.stateDir)).autoStart) {
      await save(file, { ...record, status: "disabled" }); return;
    }
    const helper = (await readChain(process.pid))[0];
    record = { ...record, status: "reopening", helperPid: helper?.pid, helperStartedAt: helper?.startedAt };
    await save(file, record);
    operation = "normal_quit";
    await execute("/usr/bin/osascript", ["-l", "JavaScript", "-e", quitScript,
      String(record.desktopPid), record.appPath], { timeout: 35000, maxBuffer: 65536 });
    // The host may exit before its native backend releases the original chat.
    // Verify that exact process lifetime has ended before reopening Desktop.
    const deadline = Date.now() + 15000;
    operation = "wait_native_exit";
    for (;;) {
      const engine = (await readChain(record.enginePid))[0];
      if (engine?.pid !== record.enginePid || engine.startedAt !== record.engineStartedAt) break;
      if (Date.now() >= deadline) throw Error("desktop_native_engine_still_exiting");
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    operation = "open_desktop";
    await execute("/usr/bin/open", ["-a", record.appPath, "--env", "CODEX_CLI_PATH=" + record.launcher,
      "--env", "CODEXWORKER_STATE_DIR=" + record.stateDir,
      ...(record.codexHome ? ["--env", "CODEX_HOME=" + record.codexHome] : [])],
      { timeout: 15000, maxBuffer: 65536 });
    const latest = await privateRecord(file);
    if (latest.status !== "restored") await save(file, { ...latest, status: "opened", openedAt: new Date().toISOString() });
  } catch (error) {
    await save(file, { ...record, status: "failed", error: "desktop_normal_reopen_failed", operation,
      detail: String(error.stderr || error.message || error.code).slice(0, 2000) });
    throw error;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === modulePath && process.argv[2] === "--reopen") {
  reopenDesktop(process.argv[3]).catch(() => { process.exitCode = 1; });
}
