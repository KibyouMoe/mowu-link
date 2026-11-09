#!/usr/bin/env node
// CodexWorker CLI entry.
//
// This file is the only new runtime code in this copy. It parses
// --config/--host/--port/--log, then either:
//   serve (default) — starts a small liveness HTTP surface (/health, /version);
//   mcp            — sets the resolved environment and imports the original
//                    server.mjs stdio MCP entry unchanged.
//
// The full manager HTTP API lives in desktop-manager.mjs and needs the native
// task pipe that Codex Desktop injects into the plugin host. That pipe does not
// exist for a plain CLI process, so this entry deliberately does not pretend to
// be the manager: /health reports ready:false and lists the Desktop-only pieces.
import http from "node:http";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { appendFile, mkdir, readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const NAME = "CodexWorker";
process.title = NAME;

const pkg = JSON.parse(await readFile(path.join(ROOT, "package.json"), "utf8"));
const VERSION = pkg.version;

const HELP = `CodexWorker ${VERSION}

用法:
  codexworker [serve] [选项]        启动 CLI 存活服务（GET /health, /version）
  codexworker mcp [选项]            透传启动原 server.mjs（MCP stdio，需 Desktop 注入管道）

选项:
  --config <file.json>  读取最小配置（host/port/bridgeName/operatingMode/tokenFile）
  --host <addr>         监听地址（默认 127.0.0.1）
  --port <n>            监听端口（默认 8903；0 表示随机空闲端口）
  --log <file>          追加 JSONL 日志（默认写 stderr）
  --stdio               mcp 的别名
  -h, --help            显示本帮助
  -v, --version         显示版本

环境变量:
  CODEXWORKER_CONFIG, CODEXWORKER_HOST, CODEXWORKER_PORT,
  CODEXWORKER_LOG, CODEXWORKER_STATE_DIR

最小配置示例:
  {"host":"127.0.0.1","port":8903,"bridgeName":"CodexWorker"}

注意: 不设置 CODEX_APP_TOOLS_PIPE_PATH 时，Desktop 原生任务相关能力不可用，
      /health 会返回 ready:false。详见 README「当前限制」。
`;

const logFile = { path: null, chain: Promise.resolve() };
function stamp() { return new Date().toISOString(); }
function log(level, event, fields = {}) {
  const line = `${stamp()} [${NAME}] ${level} ${event}` +
    (Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : "");
  process.stderr.write(line + "\n");
  if (!logFile.path) return;
  const payload = JSON.stringify({ ts: stamp(), level, event, pid: process.pid, ...fields }) + "\n";
  logFile.chain = logFile.chain.then(() => appendFile(logFile.path, payload)).catch(() => {});
}

function fail(message, code = 2) {
  process.stderr.write(`${NAME}: ${message}\n`);
  process.exit(code);
}

function expandHome(value) {
  if (typeof value !== "string") return value;
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

function parseArgs(argv) {
  const out = { command: "serve", config: null, host: null, port: null, log: null };
  const take = (i, flag) => {
    const value = argv[i];
    if (value == null || value.startsWith("--")) fail(`${flag} 需要一个值`);
    return value;
  };
  for (let i = 0; i < argv.length; i++) {
    let arg = argv[i];
    if (arg === "-h" || arg === "--help") { process.stdout.write(HELP); process.exit(0); }
    if (arg === "-v" || arg === "--version") { process.stdout.write(`${NAME} ${VERSION}\n`); process.exit(0); }
    if (arg === "serve" || arg === "mcp") { out.command = arg; continue; }
    if (arg === "--stdio") { out.command = "mcp"; continue; }
    let inline = null;
    const eq = arg.indexOf("=");
    if (arg.startsWith("--") && eq > 2) { inline = arg.slice(eq + 1); arg = arg.slice(0, eq); }
    const value = inline ?? null;
    switch (arg) {
      case "--config": out.config = expandHome(value ?? take(++i, arg)); break;
      case "--host": out.host = value ?? take(++i, arg); break;
      case "--port": out.port = value ?? take(++i, arg); break;
      case "--log": out.log = expandHome(value ?? take(++i, arg)); break;
      default: fail(`未知参数: ${arg}（用 --help 查看用法）`);
    }
  }
  return out;
}

async function loadConfig(file) {
  if (!file) return {};
  let raw;
  try { raw = await readFile(file, "utf8"); }
  catch (error) { fail(`无法读取配置文件 ${file}: ${error.code ?? error.message}`); }
  let value;
  try { value = JSON.parse(raw); }
  catch (error) { fail(`配置文件不是合法 JSON (${file}): ${error.message}`); }
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`配置必须是 JSON 对象: ${file}`);
  return value;
}

function resolvePort(value, fallback) {
  if (value == null || value === "") return fallback;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 0 || port > 65535) fail(`端口不合法: ${value}`);
  return port;
}

const args = parseArgs(process.argv.slice(2));
const config = await loadConfig(args.config ? path.resolve(args.config) : null);

const host = args.host ?? process.env.CODEXWORKER_HOST ?? config.host ?? "127.0.0.1";
const port = resolvePort(args.port ?? process.env.CODEXWORKER_PORT ?? config.port, 8903);
const stateDir = expandHome(process.env.CODEXWORKER_STATE_DIR ??
  path.join(os.homedir(), ".codex", "state", "codexworker"));
const resolvedLog = args.log ?? process.env.CODEXWORKER_LOG ?? null;
if (resolvedLog) {
  logFile.path = path.resolve(expandHome(resolvedLog));
  await mkdir(path.dirname(logFile.path), { recursive: true });
}

// The copied modules read these variables; set them before importing server.mjs.
process.env.CODEXWORKER_CONFIG = args.config ? path.resolve(args.config) : (process.env.CODEXWORKER_CONFIG ?? "");
process.env.CODEXWORKER_HOST = host;
process.env.CODEXWORKER_PORT = String(port);
process.env.CODEXWORKER_STATE_DIR = stateDir;
if (logFile.path) process.env.CODEXWORKER_LOG = logFile.path;

log("info", "start", { mode: args.command, version: VERSION, host, port, stateDir,
  config: args.config ? path.resolve(args.config) : null });

if (args.command === "mcp") {
  if (!process.env.CODEX_APP_TOOLS_PIPE_PATH) {
    log("warn", "desktop_pipe_missing", {
      detail: "CODEX_APP_TOOLS_PIPE_PATH is not set; the MCP entry will start but native task calls cannot succeed." });
  }
  // Original stdio entry, imported after the environment is resolved.
  await import("./server.mjs");
} else {
  const startedAt = new Date().toISOString();
  const startedMs = Date.now();
  let requests = 0;

  const limitations = [
    "Desktop native tasks (/threads, /turn, /manager/*) need the Codex Desktop plugin host and its injected CODEX_APP_TOOLS_PIPE_PATH; the CLI does not attach it.",
    "managerReady/nativeTaskReady stay false without that native pipe.",
    "Feishu relay, online announcements and public image output are not started by the CLI.",
    "This CLI exposes liveness only: GET /health and GET /version. All other routes return 404."
  ];

  const health = () => ({
    name: config.bridgeName ?? NAME,
    version: VERSION,
    alive: true,
    ready: false,
    mode: "cli",
    operatingMode: config.operatingMode ?? "desktop-bridge",
    integrationMode: "cli-entry",
    host,
    port: server.address()?.port ?? port,
    pid: process.pid,
    startedAt,
    uptimeSec: Number(((Date.now() - startedMs) / 1000).toFixed(3)),
    requests,
    desktopNative: {
      available: Boolean(process.env.CODEX_APP_TOOLS_PIPE_PATH),
      attached: false,
      reason: process.env.CODEX_APP_TOOLS_PIPE_PATH
        ? "Native pipe is present in the environment but the CLI entry does not attach it; run inside the Desktop plugin host for native tasks."
        : "CODEX_APP_TOOLS_PIPE_PATH is not set; start from the Codex Desktop plugin host."
    },
    capabilities: { health: true, version: true, mcpStdio: true, managerHttp: false, desktopNative: false },
    limits: limitations
  });

  const send = (res, status, body, method = "GET") => {
    const text = JSON.stringify(body, null, 2) + "\n";
    res.writeHead(status, {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "content-length": Buffer.byteLength(text)
    });
    if (method === "HEAD") res.end(); else res.end(text);
  };

  const server = http.createServer((req, res) => {
    requests++;
    const method = req.method ?? "GET";
    if (method !== "GET" && method !== "HEAD") {
      return send(res, 405, { error: "method_not_allowed", detail: "CLI 只提供 GET /health 和 GET /version。", method }, method);
    }
    let pathname;
    try { pathname = new URL(req.url ?? "/", `http://${host}`).pathname; }
    catch { return send(res, 400, { error: "bad_request" }, method); }
    if (pathname === "/health") return send(res, 200, health(), method);
    if (pathname === "/version") return send(res, 200, { name: NAME, version: VERSION, mode: "cli" }, method);
    if (pathname === "/") return send(res, 200, { name: NAME, version: VERSION, endpoints: ["/health", "/version"] }, method);
    return send(res, 404, {
      error: "not_found",
      detail: "CLI 入口只暴露 /health 与 /version；Desktop 管理 API 未在此进程中启动。",
      path: pathname
    }, method);
  });

  server.on("error", (error) => {
    log("error", "listen_failed", { code: error.code, message: error.message, host, port });
    process.exit(1);
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, resolve);
  }).catch((error) => {
    log("error", "listen_failed", { code: error.code, message: error.message, host, port });
    process.exit(1);
  });

  const actualPort = server.address().port;
  log("info", "listening", { url: `http://${host}:${actualPort}`, health: `http://${host}:${actualPort}/health` });
  process.stdout.write(`${NAME} listening on http://${host}:${actualPort} (GET /health)\n`);

  let closing = false;
  const shutdown = (signal) => {
    if (closing) return;
    closing = true;
    log("info", "shutdown", { signal });
    server.closeIdleConnections?.();
    server.close(() => { void logFile.chain.then(() => process.exit(0)); });
    const timer = setTimeout(() => process.exit(0), 3000);
    timer.unref();
  };
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("uncaughtException", (error) => {
    log("error", "uncaught_exception", { message: error?.message ?? String(error) });
    process.exit(1);
  });
  process.on("unhandledRejection", (error) => {
    log("error", "unhandled_rejection", { message: error?.message ?? String(error) });
    process.exit(1);
  });
}
