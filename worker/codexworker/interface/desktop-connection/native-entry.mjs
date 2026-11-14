// Wrap only the app-server that Desktop itself starts. No independent Worker
// receiver or second session owner is created by this connection adapter.
import { spawn } from "node:child_process";
import { once } from "node:events";
import { lstat, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { desktopOwner, processChain, resolveDesktopState, stopOwnedChild } from "../../desktop-lifecycle.mjs";
import { runDesktopConnection, runDesktopServer } from "./entry.mjs";
import { createNativeInput } from "./native-input.mjs";
import { normalizeDesktopEnvironment } from "../desktop-environment.mjs";

export async function requireNativeDesktopParent({ platform = process.platform, readChain = processChain } = {}) {
  if (!["darwin", "win32"].includes(platform)) throw new Error("desktop_connection_platform_unsupported");
  const owner = desktopOwner(await readChain(process.ppid));
  if (!owner) throw new Error("desktop_connection_requires_desktop_parent");
  return owner;
}

export async function loadNativeConnection(env = process.env) {
  env = normalizeDesktopEnvironment({ ...env });
  const home = env.CODEX_HOME || path.join(env.USERPROFILE || os.homedir(), ".codex");
  const stateDir = await resolveDesktopState(path.join(home, "state/codexworker"), env);
  const file = path.join(stateDir, "desktop-native-connection.json");
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
  verifyParent = requireNativeDesktopParent, load = loadNativeConnection, review, createInput = createNativeInput,
  stdin = process.stdin, stdout = process.stdout, stderr = process.stderr } = {}) {
  env = normalizeDesktopEnvironment({ ...env });
  let owner, input;
  try {
    return await runDesktopConnection(argv, { env, spawnImpl,
    verifyParent: async () => { owner = await verifyParent(); return owner; }, load, nativePassthrough: true,
    serve: settings => runDesktopServer(settings, { config: {}, nativePassthrough: true }, { stdin, stdout, stderr, review,
      terminationEvent: "close", closeChannel: async child => {
        await input?.close().catch(() => stderr.write("[desktop-connection] image input cleanup failed\n"));
        await stopOwnedChild(child);
      },
      observeClient: message => input?.client(message), observeServer: message => input?.server(message),
      connect: async () => {
        const childEnv = { ...env }; delete childEnv.CODEX_CLI_PATH;
        let child;
        // Only the primary Desktop child publishes the input endpoint. Internal
        // tool subprocesses and metadata checks retain their normal behavior.
        if (process.platform === "darwin" && owner.pid === process.ppid) {
          try {
            const home = env.CODEX_HOME || path.join(os.homedir(), ".codex");
            const stateDir = await resolveDesktopState(path.join(home, "state/codexworker"), env);
            input = await createInput({ stateDir, owner,
              send: message => child.stdin.write(JSON.stringify(message) + "\n") });
            Object.assign(childEnv, input.env);
          } catch {
            delete childEnv.CODEXWORKER_NATIVE_IMAGE_INPUT; delete childEnv.CODEXWORKER_NATIVE_INPUT_DESCRIPTOR;
            delete childEnv.CODEXWORKER_NATIVE_QUESTION_INPUT;
            stderr.write("[desktop-connection] native image input unavailable\n");
          }
        }
        child = spawnImpl(settings.realCli, argv, { env: childEnv, stdio: ["pipe", "pipe", "pipe"] });
        await once(child, "spawn");
        child.stderr.pipe(stderr, { end: false });
        child.once("close", () => child.stderr.unpipe(stderr));
        return child;
      } }) });
  } finally {
    await input?.close().catch(() => stderr.write("[desktop-connection] image input cleanup failed\n"));
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runNativeConnection(process.argv.slice(2)).then(code => { process.exitCode = code; }, () => {
    process.stderr.write("[desktop-connection] native connection failed\n"); process.exitCode = 1;
  });
}
