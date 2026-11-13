// Desktop JSONL -> the existing documented Unix daemon. This is a connection
// adapter, never a Worker entry or an independently launched model host.
import { spawn } from "node:child_process";
import { lstat, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { StringDecoder } from "node:string_decoder";
import { fileURLToPath } from "node:url";
import { desktopOwner, processChain } from "../../desktop-lifecycle.mjs";
import { connectUnixWebSocket } from "./unix-websocket.mjs";

export async function requireDesktopParent({ platform = process.platform, pid = process.ppid,
  readChain = processChain } = {}) {
  // Windows uses Desktop's native named pipe; do not install a Unix override.
  if (platform !== "darwin") throw new Error("desktop_connection_platform_unsupported");
  const owner = desktopOwner(await readChain(pid));
  if (!owner) throw new Error("desktop_connection_requires_desktop_parent");
  return owner;
}

export function desktopServerOptions(argv) {
  const index = argv.indexOf("app-server");
  if (index < 0) return null;
  // Only these non-serving metadata operations may reach the native CLI.
  if (["generate-json-schema", "generate-ts"].includes(argv[index + 1]) ||
      argv[index + 1] === "daemon" && argv[index + 2] === "version" && argv.length === index + 3 ||
      argv.includes("--help") || argv.includes("-h") || argv.includes("--print-startup-config")) return null;
  const config = {};
  const args = [...argv.slice(0, index), ...argv.slice(index + 1)];
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--analytics-default-enabled" || arg === "--stdio") continue;
    if (arg === "--listen") {
      if (args[++i] !== "stdio://") throw new Error("unsupported_shared_desktop_listener");
      continue;
    }
    let value;
    if (arg === "-c" || arg === "--config") value = args[++i];
    else if (arg.startsWith("--config=")) value = arg.slice(9);
    else throw new Error("unsupported_shared_desktop_option");
    const at = value?.indexOf("=") ?? -1;
    if (at < 1) throw new Error("invalid_shared_desktop_config");
    const key = value.slice(0, at).trim();
    if (!/^[A-Za-z0-9_.@-]+$/.test(key)) throw new Error("invalid_shared_desktop_config_key");
    try { config[key] = JSON.parse(value.slice(at + 1)); }
    catch { throw new Error("unsupported_shared_desktop_config_value"); }
  }
  return { config };
}

export function desktopRequest(message, config) {
  if (!["thread/start", "thread/resume", "thread/fork"].includes(message?.method) || !Object.keys(config).length) return message;
  const original = message.params?.config;
  if (original != null && (typeof original !== "object" || Array.isArray(original))) throw new Error("invalid_native_config");
  return { ...message, params: { ...message.params, config: { ...config, ...original } } };
}

export async function loadDesktopConnection(env = process.env) {
  const home = env.CODEX_HOME || path.join(os.homedir(), ".codex");
  const file = env.CODEXWORKER_SHARED_DESKTOP_CONFIG || path.join(home, "state/codexworker/desktop-shared-connection.json");
  const metadata = await lstat(file);
  if (!metadata.isFile() || metadata.isSymbolicLink() ||
      typeof process.getuid === "function" && metadata.uid !== process.getuid() || metadata.mode & 0o077)
    throw new Error("shared_desktop_config_not_private");
  const value = JSON.parse(await readFile(file, "utf8"));
  if (value?.version !== 1 || !path.isAbsolute(value.realCli ?? "") || !path.isAbsolute(value.socket ?? ""))
    throw new Error("invalid_shared_desktop_connection");
  return value;
}

export async function runDesktopServer(settings, options, { stdin = process.stdin, stdout = process.stdout,
  stderr = process.stderr, connect = connectUnixWebSocket } = {}) {
  const channel = await connect(settings.socket);
  stderr.write("[desktop-connection] connected to existing shared runtime\n");
  let buffer = "", failed = false;
  const decoder = new StringDecoder("utf8");
  channel.stdout.pipe(stdout, { end: false });
  return new Promise(resolve => {
    const fail = () => {
      failed = true;
      stderr.write("[desktop-connection] forwarding failed; no fallback engine was started\n");
      channel.kill();
    };
    const input = chunk => {
      try {
        buffer += decoder.write(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
        if (Buffer.byteLength(buffer) > 16 * 1024 * 1024) throw new Error("shared_desktop_input_too_large");
        let at;
        while ((at = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, at); buffer = buffer.slice(at + 1);
          if (!line.trim()) continue;
          channel.stdin.write(JSON.stringify(desktopRequest(JSON.parse(line), options.config)) + "\n");
        }
      } catch { fail(); }
    };
    const stop = () => channel.kill();
    const end = () => { if (buffer.trim() || decoder.lastNeed) fail(); else stop(); };
    stdin.on("data", input); stdin.once("end", end); stdin.once("error", fail);
    channel.stdin.on("error", fail); stdout.on("error", fail);
    process.once("SIGTERM", stop); process.once("SIGINT", stop);
    channel.on("error", fail);
    channel.once("exit", () => {
      stdin.removeListener("data", input); stdin.removeListener("end", end); stdin.removeListener("error", fail); stdin.pause();
      channel.stdin.removeListener("error", fail); stdout.removeListener("error", fail); channel.removeListener("error", fail);
      channel.stdout.unpipe(stdout);
      process.removeListener("SIGTERM", stop); process.removeListener("SIGINT", stop);
      resolve(failed ? 1 : 0);
    });
  });
}

function isDesktopAuxiliary(argv) {
  return argv.length === 1 && ["--version", "-V", "--help", "-h"].includes(argv[0]) ||
    ["exec-server", "login", "logout"].includes(argv[0]) ||
    argv[0] === "app-server" && (["generate-json-schema", "generate-ts", "--help", "-h", "--print-startup-config"].includes(argv[1]) ||
      argv.length === 3 && argv[1] === "daemon" && argv[2] === "version");
}

export async function runDesktopConnection(argv, { verifyParent = requireDesktopParent,
  load = loadDesktopConnection, serve = runDesktopServer, spawnImpl = spawn, env = process.env } = {}) {
  await verifyParent(); // Before settings, connection, or any native process.
  const options = desktopServerOptions(argv);
  if (!options && !isDesktopAuxiliary(argv)) throw new Error("unsupported_desktop_command");
  const settings = await load(env);
  if (options) return serve(settings, options);
  const childEnv = { ...env }; delete childEnv.CODEX_CLI_PATH;
  const child = spawnImpl(settings.realCli, argv, { stdio: "inherit", env: childEnv });
  const stop = () => child.kill("SIGTERM"); process.once("SIGTERM", stop);
  return new Promise((resolve, reject) => {
    child.once("error", error => { process.removeListener("SIGTERM", stop); reject(error); });
    child.once("exit", code => { process.removeListener("SIGTERM", stop); resolve(code ?? 1); });
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runDesktopConnection(process.argv.slice(2)).then(code => { process.exitCode = code; }, error => {
    process.stderr.write("[desktop-connection] " + error.message + "; no fallback engine was started\n");
    process.exitCode = 1;
  });
}
