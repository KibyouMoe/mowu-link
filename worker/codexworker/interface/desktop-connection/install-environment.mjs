// macOS restores this one-shot environment setting at login. The app-server
// and Worker processes are still created by their actual Desktop parent.
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { prepareDesktopConnection } from "./prepare.mjs";

export const ENVIRONMENT_LABEL = "com.kibyou.codexworker-desktop-environment";
const execFileAsync = promisify(execFile);
const xml = value => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&apos;");

export async function installDesktopEnvironment({ platform = process.platform, home = os.homedir(),
  env = process.env, prepare = prepareDesktopConnection, exec = execFileAsync } = {}) {
  if (platform !== "darwin") throw new Error("desktop_environment_platform_unsupported");
  const installed = await prepare({ platform, env, mode: "native" });
  // Exercise the exact startup-policy argv before selecting a new launcher.
  const { stdout } = await exec(installed.launcher, ["-c", "features.code_mode_host=true", "app-server",
    "--analytics-default-enabled", "--print-startup-config", "--startup-client-info",
    JSON.stringify({ name: "codex_desktop", title: "Codex Desktop", version: "worker-install-check" })],
  { env, timeout: 30000, maxBuffer: 4 * 1024 * 1024 });
  const metadata = JSON.parse(stdout);
  if (typeof metadata.appServerVersion !== "string" || !metadata.configRequirements ||
      !Object.hasOwn(metadata.configRequirements, "requirements")) throw new Error("desktop_startup_policy_invalid");
  const file = path.join(home, "Library", "LaunchAgents", ENVIRONMENT_LABEL + ".plist");
  let previous;
  try { previous = await readFile(file); } catch (error) { if (error.code !== "ENOENT") throw error; }
  await mkdir(path.dirname(file), { recursive: true });
  if (previous) {
    const backup = path.join(installed.stateDir, "desktop-environment-backups");
    await mkdir(backup, { recursive: true, mode: 0o700 });
    await writeFile(path.join(backup, randomUUID() + ".plist"), previous, { flag: "wx", mode: 0o600 });
  }
  const content = '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n' +
    '<plist version="1.0"><dict>\n<key>Label</key><string>' + ENVIRONMENT_LABEL + '</string>\n' +
    '<key>ProgramArguments</key><array><string>/bin/launchctl</string><string>setenv</string>' +
    '<string>CODEX_CLI_PATH</string><string>' + xml(installed.launcher) + '</string></array>\n' +
    '<key>RunAtLoad</key><true/><key>LimitLoadToSessionType</key><string>Aqua</string>\n</dict></plist>\n';
  const temporary = file + "." + randomUUID() + ".tmp";
  const oldEnvironment = (await exec("/bin/launchctl", ["getenv", "CODEX_CLI_PATH"])).stdout.trim();
  await writeFile(temporary, content, { mode: 0o600 });
  try {
    await exec("/usr/bin/plutil", ["-lint", temporary]);
    await rename(temporary, file);
    await exec("/bin/launchctl", ["setenv", "CODEX_CLI_PATH", installed.launcher]);
  } catch (error) {
    await unlink(temporary).catch(() => {});
    if (previous) {
      await writeFile(temporary, previous, { flag: "wx", mode: 0o600 });
      await rename(temporary, file);
    }
    else await unlink(file).catch(() => {});
    const current = (await exec("/bin/launchctl", ["getenv", "CODEX_CLI_PATH"])).stdout.trim();
    if (current === installed.launcher) await exec("/bin/launchctl", oldEnvironment
      ? ["setenv", "CODEX_CLI_PATH", oldEnvironment] : ["unsetenv", "CODEX_CLI_PATH"]);
    throw error;
  }
  return { ...installed, environmentFile: file, label: ENVIRONMENT_LABEL,
    startupPolicyVerified: true, desktopRestartRequired: true };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  installDesktopEnvironment().then(result => console.log(JSON.stringify(result)), error => {
    process.stderr.write("[desktop-environment] installation failed (" + (error.code ?? "validation") + ")\n");
    process.exitCode = 1;
  });
}
