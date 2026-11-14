// Wrap only the app-server that Desktop itself starts. No independent Worker
// receiver or second session owner is created by this connection adapter.
import { spawn } from "node:child_process";
import { once } from "node:events";
import { lstat, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { desktopOwner, processChain } from "../../desktop-lifecycle.mjs";
import { runDesktopConnection, runDesktopServer } from "./entry.mjs";

export async function requireNativeDesktopParent({ platform = process.platform, readChain = processChain } = {}) {
  if (!["darwin", "win32"].includes(platform)) throw new Error("desktop_connection_platform_unsupported");
  const owner = desktopOwner(await readChain(process.ppid));
  if (!owner) throw new Error("desktop_connection_requires_desktop_parent");
  return owner;
}

export async function loadNativeConnection(env = process.env) {
  const home = env.CODEX_HOME || path.join(env.USERPROFILE || os.homedir(), ".codex");
  const file = path.join(home, "state/codexworker/desktop-native-connection.json");
  const stat = await lstat(file);
  if (!stat.isFile() || stat.isSymbolicLink() ||
      typeof process.getuid === "function" && (stat.uid !== process.getuid() || stat.mode & 0o077))
    throw new Error("desktop_native_connection_not_private");
  const settings = JSON.parse(await readFile(file, "utf8"));
  if (settings.version !== 1 || !path.isAbsolute(settings.realCli ?? ""))
    throw new Error("desktop_native_connection_invalid");
  return settings;
}

export async function runNativeConnection(argv, { env = process.env, spawnImpl = spawn,
  verifyParent = requireNativeDesktopParent, load = loadNativeConnection, review,
  stdin = process.stdin, stdout = process.stdout, stderr = process.stderr } = {}) {
  return runDesktopConnection(argv, { env, spawnImpl, verifyParent, load,
    serve: settings => runDesktopServer(settings, { config: {} }, { stdin, stdout, stderr, review,
      connect: async () => {
        const childEnv = { ...env }; delete childEnv.CODEX_CLI_PATH;
        const child = spawnImpl(settings.realCli, argv, { env: childEnv, stdio: ["pipe", "pipe", "pipe"] });
        await once(child, "spawn");
        child.stderr.pipe(stderr, { end: false });
        child.once("exit", () => child.stderr.unpipe(stderr));
        return child;
      } }) });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runNativeConnection(process.argv.slice(2)).then(code => { process.exitCode = code; }, () => {
    process.stderr.write("[desktop-connection] native connection failed\n"); process.exitCode = 1;
  });
}
