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
import { createApprovalRelay } from "./approval-relay.mjs";
import { createNativeLineRelay } from "./native-lines.mjs";

export async function requireDesktopParent({ platform = process.platform, pid = process.ppid,
  readChain = processChain } = {}) {
  // Windows uses Desktop's native named pipe; do not install a Unix override.
  if (platform !== "darwin") throw new Error("desktop_connection_platform_unsupported");
  const owner = desktopOwner(await readChain(pid));
  if (!owner) throw new Error("desktop_connection_requires_desktop_parent");
  return owner;
}

export function desktopServerOptions(argv, { nativePassthrough = false } = {}) {
  const index = argv.indexOf("app-server");
  if (index < 0) return null;
  // Only these non-serving metadata operations may reach the native CLI.
  if (["generate-json-schema", "generate-ts"].includes(argv[index + 1]) ||
      argv[index + 1] === "daemon" && argv[index + 2] === "version" && argv.length === index + 3 ||
      argv.includes("--help") || argv.includes("-h") || argv.includes("--print-startup-config")) return null;
  if (nativePassthrough) {
    // Desktop's bundled CLI owns native option validation, including newly
    // added startup/config flags. This adapter only accepts its stdio server.
    const listen = argv.indexOf("--listen");
    if (argv[index + 1] === "daemon" || listen >= 0 && argv[listen + 1] !== "stdio://" ||
        argv.some(arg => arg.startsWith("--listen=") && arg !== "--listen=stdio://"))
      throw new Error("unsupported_native_desktop_listener");
    return { config: {} };
  }
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
  stderr = process.stderr, connect = connectUnixWebSocket, review, terminationEvent = "exit",
  closeChannel = channel => channel.kill() } = {}) {
  const channel = await connect(settings.socket);
  stderr.write("[desktop-connection] Desktop transport connected\n");
  let buffer = "", serverBuffer = "", failed = false, closing;
  const decoder = new StringDecoder("utf8");
  const serverDecoder = new StringDecoder("utf8");
  const relay = createApprovalRelay({ review,
    send: message => channel.stdin.write(JSON.stringify(message) + "\n"),
    log: (event, fields) => stderr.write("[desktop-connection] " + event + " " + JSON.stringify(fields) + "\n") });
  const nativeInput = options.nativePassthrough && createNativeLineRelay({
    write: bytes => channel.stdin.write(bytes), inspect: message => relay.client(message) });
  const nativeOutput = options.nativePassthrough && createNativeLineRelay({
    write: bytes => stdout.write(bytes), inspect: message => relay.server(message) });
  return new Promise(resolve => {
    const fail = () => {
      if (failed) return;
      failed = true;
      relay.close();
      stderr.write("[desktop-connection] forwarding failed; no fallback engine was started\n");
      channel.kill();
    };
    const input = chunk => {
      try {
        if (nativeInput) { nativeInput.write(chunk); return; }
        buffer += decoder.write(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
        if (Buffer.byteLength(buffer) > 16 * 1024 * 1024) throw new Error("shared_desktop_input_too_large");
        let at;
        while ((at = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, at); buffer = buffer.slice(at + 1);
          if (!line.trim()) continue;
          const message = JSON.parse(line);
          if (relay.client(message)) channel.stdin.write(JSON.stringify(desktopRequest(message, options.config)) + "\n");
        }
      } catch { fail(); }
    };
    const output = chunk => {
      try {
        if (nativeOutput) { nativeOutput.write(chunk); return; }
        serverBuffer += serverDecoder.write(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
        if (Buffer.byteLength(serverBuffer) > 16 * 1024 * 1024) throw new Error("shared_desktop_output_too_large");
        let at;
        while ((at = serverBuffer.indexOf("\n")) >= 0) {
          const line = serverBuffer.slice(0, at); serverBuffer = serverBuffer.slice(at + 1);
          if (!line.trim()) continue;
          const message = JSON.parse(line);
          stdout.write(line + "\n");
          relay.server(message);
        }
      } catch { fail(); }
    };
    const stop = () => {
      relay.close();
      closing ??= Promise.resolve().then(() => closeChannel(channel)).catch(fail);
    };
    const end = () => {
      try {
        nativeInput && nativeInput.end();
        if (buffer.trim() || decoder.lastNeed) fail(); else stop();
      } catch { fail(); }
    };
    const outputEnd = () => { try { nativeOutput && nativeOutput.end(); } catch { fail(); } };
    stdin.on("data", input); stdin.once("end", end); stdin.once("error", fail);
    channel.stdout.on("data", output);
    channel.stdout.once("end", outputEnd);
    channel.stdin.on("error", fail); stdout.on("error", fail);
    process.once("SIGTERM", stop); process.once("SIGINT", stop);
    channel.on("error", fail);
    channel.once(terminationEvent, code => {
      stdin.removeListener("data", input); stdin.removeListener("end", end); stdin.removeListener("error", fail); stdin.pause();
      channel.stdin.removeListener("error", fail); stdout.removeListener("error", fail); channel.removeListener("error", fail);
      relay.close();
      channel.stdout.removeListener("data", output);
      channel.stdout.removeListener("end", outputEnd);
      process.removeListener("SIGTERM", stop); process.removeListener("SIGINT", stop);
      resolve(failed ? 1 : code ?? 0);
    });
  });
}

function isDesktopAuxiliary(argv) {
  const appServer = argv.indexOf("app-server");
  return argv.length === 1 && ["--version", "-V", "--help", "-h"].includes(argv[0]) ||
    ["exec-server", "login", "logout"].includes(argv[0]) ||
    appServer >= 0 && (["generate-json-schema", "generate-ts"].includes(argv[appServer + 1]) ||
      argv.includes("--print-startup-config") || argv.includes("--help") || argv.includes("-h") ||
      argv.length === appServer + 3 && argv[appServer + 1] === "daemon" && argv[appServer + 2] === "version");
}

export async function runDesktopConnection(argv, { verifyParent = requireDesktopParent,
  load = loadDesktopConnection, serve = runDesktopServer, spawnImpl = spawn, env = process.env,
  nativePassthrough = false } = {}) {
  await verifyParent(); // Before settings, connection, or any native process.
  const options = desktopServerOptions(argv, { nativePassthrough });
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
