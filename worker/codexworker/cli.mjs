#!/usr/bin/env node
// CodexWorker CLI entry.
//
// This is the "no Codex Desktop" launcher: it owns one `codex app-server`
// child through transport/app-server.mjs, persists a CLI session, writes the
// manager binding + local desktop config that mcp-business.mjs already reads,
// and then starts the existing HTTP + Feishu manager unchanged.
//
// Nothing here reimplements business logic. The sequence is:
//   identity.json → cli-session.json → manager-binding.json →
//   desktop-config.local.json → createBusiness(...).activate()
//
// Identity is mandatory: name is the internal id (health.name, state), display
// is what a human sees (Feishu titles, health.display). No identity → exit 2.
//
// The manager's writer lease lives under ~/.codex/state/codexworker; this
// process is the sole writer, so it sets RUDOLF_BRIDGE_LIFECYCLE_CHILD=1 the
// same way the Desktop lifetime supervisor does.
import { appendFile, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PROGRAM = "codexworker";
// Keep in step with BUSINESS_VERSION in mcp-business.mjs.
const VERSION = "0.2.38";
const BINDING_STATE_DIR = path.join(os.homedir(), ".codex", "state", "codexworker");
const BINDING_STATE_PATH = path.join(BINDING_STATE_DIR, "manager-binding.json");
const DESKTOP_CONFIG_PATH = path.join(BINDING_STATE_DIR, "desktop-config.local.json");

const HELP = `codexworker ${VERSION}

用法:
  codexworker [选项]                 启动 app-server 后端（HTTP + 飞书）

选项:
  --identity <file>     实例身份文件（默认 ./identity.json：name/display/port/configFile）
  --config <file.json>  覆盖 identity.configFile 的配置文件
  --host <addr>         监听地址（默认 127.0.0.1）
  --port <n>            监听端口（默认取 identity.port，其次 config.port，最后 8903）
  --log <file>          追加 JSONL 日志（支持 {name}/{display}/{port} 占位符；默认只写 stderr）
  -h, --help            显示本帮助
  -v, --version         显示版本

环境变量:
  CODEXWORKER_IDENTITY, CODEXWORKER_CONFIG, CODEXWORKER_HOST, CODEXWORKER_PORT,
  CODEXWORKER_LOG, CODEXWORKER_STATE_DIR, CODEXWORKER_TOKEN_FILE,
  CODEXWORKER_CODEX_BIN, CODEXWORKER_APP_SERVER_PROXY

说明:
  1. 本命令固定使用 transport/app-server.mjs（codex app-server JSON-RPC）。
  2. 主会话保存在 <stateDir>/cli-session.json，重启复用；读不到时重建。
  3. 写入 ~/.codex/state/codexworker/manager-binding.json 与
     desktop-config.local.json，再把该会话登记到 manager（飞书可直接发消息）。
  4. SIGINT/SIGTERM 会同时关掉 manager 与 codex app-server 子进程。
`;

const logFile = { path: null, chain: Promise.resolve() };
let label = PROGRAM;
const stamp = () => new Date().toISOString();

function log(level, event, fields = {}) {
  const line = `${stamp()} [${label}] ${level} ${event}` +
    (Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : "");
  process.stderr.write(line + "\n");
  if (!logFile.path) return;
  const payload = JSON.stringify({ ts: stamp(), level, event, pid: process.pid, ...fields }) + "\n";
  logFile.chain = logFile.chain.then(() => appendFile(logFile.path, payload)).catch(() => {});
}

function fail(message, code = 2) {
  process.stderr.write(`${label}: ${message}\n`);
  process.exit(code);
}

function expandHome(value) {
  if (typeof value !== "string" || !value) return value;
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function parseArgs(argv) {
  const out = { identity: null, config: null, host: null, port: null, log: null };
  const take = (index, flag) => {
    const value = argv[index];
    if (value == null || value.startsWith("--")) fail(`${flag} 需要一个值`);
    return value;
  };
  for (let i = 0; i < argv.length; i++) {
    let arg = argv[i];
    if (arg === "-h" || arg === "--help") { process.stdout.write(HELP); process.exit(0); }
    if (arg === "-v" || arg === "--version") { process.stdout.write(`${PROGRAM} ${VERSION}\n`); process.exit(0); }
    let inline = null;
    const eq = arg.indexOf("=");
    if (arg.startsWith("--") && eq > 2) { inline = arg.slice(eq + 1); arg = arg.slice(0, eq); }
    const value = inline;
    switch (arg) {
      case "--identity": out.identity = path.resolve(expandHome(value ?? take(++i, arg))); break;
      case "--config": out.config = path.resolve(expandHome(value ?? take(++i, arg))); break;
      case "--host": out.host = value ?? take(++i, arg); break;
      case "--port": out.port = value ?? take(++i, arg); break;
      case "--log": out.log = expandHome(value ?? take(++i, arg)); break;
      default: fail(`未知参数: ${arg}（用 --help 查看用法）`);
    }
  }
  return out;
}

async function readJson(file) {
  const raw = await readFile(file, "utf8");
  try { return JSON.parse(raw); }
  catch (error) { throw new Error(`不是合法 JSON (${file}): ${error.message}`); }
}

async function readJsonIfExists(file) {
  try { return await readJson(file); }
  catch (error) { if (error.code === "ENOENT") return null; throw error; }
}

async function writeJsonAtomic(file, value, mode = 0o600) {
  await mkdir(path.dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}.tmp`;
  await writeFile(temp, JSON.stringify(value, null, 2) + "\n", { mode });
  await rename(temp, file);
}

function resolvePort(value, fallback) {
  if (value == null || value === "") return fallback;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) fail(`端口不合法: ${value}`);
  return port;
}

async function loadIdentity(explicit) {
  const envFile = process.env.CODEXWORKER_IDENTITY
    ? path.resolve(expandHome(process.env.CODEXWORKER_IDENTITY)) : null;
  const pinned = explicit ?? envFile;
  const candidates = pinned
    ? [pinned]
    : [path.resolve("identity.json"), path.join(ROOT, "identity.json")];
  for (const file of candidates) {
    let value;
    try { value = await readJson(file); }
    catch (error) {
      if (error.code === "ENOENT") {
        if (pinned) fail(`缺少实例身份：找不到 identity 文件 ${file}（需包含 name 与 display）`);
        continue;
      }
      fail(`无法读取 identity 文件 ${file}: ${error.message}`);
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) fail(`identity 文件必须是 JSON 对象: ${file}`);
    const name = typeof value.name === "string" ? value.name.trim() : "";
    const display = typeof value.display === "string" ? value.display.trim() : "";
    if (!name || !display) fail(`缺少实例身份：${file} 需同时包含非空的 name 与 display`);
    if (/[\\/]/.test(name) || name === "." || name === "..") fail(`identity.name 不能包含路径分隔符: ${name}`);
    const port = value.port == null || value.port === "" ? null : resolvePort(value.port, null);
    return { file, name, display, bridge: typeof value.bridge === "string" && value.bridge.trim() ? value.bridge.trim() : "CodexWorker",
      port, configFile: typeof value.configFile === "string" && value.configFile.trim() ? expandHome(value.configFile.trim()) : null };
  }
  fail(`缺少实例身份：identity.json 需包含 name 与 display（默认路径 ${candidates[0]}，可用 --identity 指定）`);
}

async function loadConfig(explicit, identity) {
  const file = explicit ?? identity.configFile;
  if (!file) return { file: null, value: {} };
  let value;
  try { value = await readJson(file); }
  catch (error) {
    if (error.code === "ENOENT") {
      if (explicit) fail(`无法读取配置文件 ${file}: ENOENT`);
      log("warn", "config_missing", { file });
      return { file, value: {} };
    }
    fail(`无法读取配置文件 ${file}: ${error.message}`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`配置必须是 JSON 对象: ${file}`);
  return { file, value };
}

function resolveTokenFile(config) {
  const file = path.resolve(expandHome(process.env.CODEXWORKER_TOKEN_FILE
    || config.tokenFile || path.join(os.homedir(), ".hermes", "secrets", "router.token")));
  return file;
}

async function assertTokenFile(file) {
  let value;
  try { value = (await readFile(file, "utf8")).trim(); }
  catch (error) { fail(`无法读取 tokenFile ${file}: ${error.code ?? error.message}`); }
  if (value.length < 24) fail(`tokenFile 中的 token 少于 24 字符: ${file}`);
  return value;
}

async function resolveFeishuConfigFile(config) {
  const configured = config.feishu?.configFile ?? path.join(os.homedir(), ".hermes", "secrets", "feishu-oguri.json");
  const file = path.resolve(expandHome(configured));
  let value;
  try { value = await readJson(file); }
  catch (error) { fail(`无法读取飞书配置 ${file}: ${error.code ?? error.message}`); }
  if (!value?.app_id || !value?.app_secret) fail(`飞书配置缺少 app_id/app_secret: ${file}`);
  return { file, value };
}

// The manager starts Feishu inbound only when the whitelist is non-empty.
// Prefer the explicit config whitelist; otherwise use the trainer open_id that
// already lives in the Feishu credential file (never printed, count only).
function resolveWhitelist(config, feishuValue) {
  const configured = Array.isArray(config.feishu?.trainerOpenIdWhitelist)
    ? config.feishu.trainerOpenIdWhitelist.filter((id) => typeof id === "string" && id.trim()).map((id) => id.trim())
    : [];
  if (configured.length) return { ids: configured, source: "config" };
  const trainer = typeof feishuValue.trainer_open_id === "string" ? feishuValue.trainer_open_id.trim() : "";
  const openId = typeof feishuValue.open_id === "string" ? feishuValue.open_id.trim() : "";
  const derived = trainer || openId;
  return derived ? { ids: [derived], source: trainer ? "feishu.trainer_open_id" : "feishu.open_id" } : { ids: [], source: "none" };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const identity = await loadIdentity(args.identity);
  label = identity.display;
  process.title = identity.name;

  const { file: configFile, value: config } = await loadConfig(args.config, identity);
  const host = args.host ?? process.env.CODEXWORKER_HOST ?? config.host ?? "127.0.0.1";
  const port = resolvePort(args.port ?? process.env.CODEXWORKER_PORT ?? identity.port ?? config.port, 8903);
  const stateDir = path.resolve(expandHome(process.env.CODEXWORKER_STATE_DIR ?? BINDING_STATE_DIR));
  const cwd = process.cwd();

  const resolvedLog = args.log ?? process.env.CODEXWORKER_LOG ?? null;
  if (resolvedLog) {
    logFile.path = path.resolve(expandHome(String(resolvedLog)
      .replaceAll("{name}", identity.name).replaceAll("{display}", identity.display).replaceAll("{port}", String(port))));
    await mkdir(path.dirname(logFile.path), { recursive: true });
  }
  await mkdir(stateDir, { recursive: true });

  const tokenFile = resolveTokenFile(config);
  const token = await assertTokenFile(tokenFile);
  const feishu = await resolveFeishuConfigFile(config);
  const whitelist = resolveWhitelist(config, feishu.value);

  log("info", "identity_loaded", { name: identity.name, display: identity.display,
    identityFile: identity.file, configFile });
  log("info", "start", { version: VERSION, host, port, cwd, stateDir, transport: "app-server" });

  // The transport selector reads this at resolution time; set it before any
  // business import can call resolveNativeTransport().
  process.env.CODEXWORKER_NATIVE_TRANSPORT = "app-server";
  // This process owns the manager lifetime; no Desktop supervisor exists.
  process.env.RUDOLF_BRIDGE_LIFECYCLE_CHILD = "1";
  process.env.RUDOLF_BRIDGE_LIFECYCLE_READY ??= "1";

  const transportIndex = await import("./transport/index.mjs");
  const appServer = await import("./transport/app-server.mjs");
  const transport = transportIndex.resolveNativeTransport();

  async function nativeCall(name, params = {}, timeoutMs = 30_000) {
    const response = await transport.callTool(name, params, {}, timeoutMs);
    const text = response?.contentItems?.[0]?.text;
    if (response?.success !== true) {
      let detail = text ?? "no detail";
      try { const parsed = JSON.parse(text); detail = parsed.reason ?? parsed.error ?? detail; } catch { /* plain text */ }
      throw new Error(`${name} failed: ${detail}`);
    }
    return text ? JSON.parse(text) : null;
  }

  // ---- 主会话 ---------------------------------------------------------------
  const sessionPath = path.join(stateDir, "cli-session.json");
  let session = null;
  const saved = await readJsonIfExists(sessionPath).catch((error) => {
    log("warn", "cli_session_unreadable", { file: sessionPath, message: error.message });
    return null;
  });
  if (saved?.threadId && typeof saved.threadId === "string") {
    try {
      const snapshot = await nativeCall("read_thread",
        { threadId: saved.threadId, turnLimit: 1, includeOutputs: false }, 30_000);
      const found = snapshot?.thread;
      if (found?.id === saved.threadId &&
          path.resolve(found.cwd ?? saved.cwd ?? cwd) === path.resolve(saved.cwd ?? cwd)) {
        session = { threadId: saved.threadId, cwd: found.cwd ?? saved.cwd ?? cwd };
        log("info", "cli_session_reused", { threadId: session.threadId, cwd: session.cwd });
      } else {
        log("warn", "cli_session_stale", { threadId: saved.threadId,
          reason: found?.id !== saved.threadId ? "thread_mismatch" : "cwd_changed" });
      }
    } catch (error) {
      log("warn", "cli_session_unavailable", { threadId: saved.threadId, message: error.message });
    }
  }
  if (!session) {
    const created = await nativeCall("create_thread",
      { cwd, title: `${identity.display} · CLI` }, 30_000);
    if (typeof created?.threadId !== "string" || !created.threadId) {
      throw new Error("create_thread returned no threadId");
    }
    session = { threadId: created.threadId, cwd: created.thread?.cwd ?? cwd };
    await writeJsonAtomic(sessionPath, session);
    log("info", "cli_session_created", { threadId: session.threadId, cwd: session.cwd });
  }

  const projects = await nativeCall("list_projects", {}, 30_000).catch((error) => {
    log("warn", "list_projects_failed", { message: error.message });
    return { projects: [] };
  });
  const project = (projects.projects ?? []).find((entry) => entry.path === session.cwd && entry.hostId === "local");
  log("info", "projects_loaded", { count: projects.projects?.length ?? 0, projectId: project?.projectId ?? null });

  // ---- manager binding + local config --------------------------------------
  await writeJsonAtomic(BINDING_STATE_PATH, {
    formatVersion: 1,
    integrationMode: "desktop-native",
    managerThreadId: session.threadId,
    managerTurnId: null,
    boundAt: new Date().toISOString(),
  });
  await writeJsonAtomic(DESKTOP_CONFIG_PATH, {
    host,
    port,
    bridgeName: identity.display,
    operatingMode: "feishu-local",
    machine: identity.name,
    tokenFile,
    workspaceRoots: [session.cwd],
    ...(project?.projectId ? { projectId: project.projectId } : {}),
    pollMs: 1000,
    feishu: {
      configFile: feishu.file,
      inboundEnabled: true,
      trainerOpenIdWhitelist: whitelist.ids,
      onlineNotificationEnabled: config.feishu?.onlineNotificationEnabled ?? false,
      streamEnabled: config.feishu?.streamEnabled ?? true,
      defaultDelivery: config.feishu?.defaultDelivery ?? "steer",
      autoImageOutput: config.feishu?.autoImageOutput ?? true,
    },
  });
  log("info", "manager_config_written", { binding: BINDING_STATE_PATH, config: DESKTOP_CONFIG_PATH,
    whitelistCount: whitelist.ids.length, whitelistSource: whitelist.source,
    feishuInbound: whitelist.ids.length > 0 ? "enabled" : "disabled_no_whitelist" });

  // ---- business / manager --------------------------------------------------
  const { createBusiness } = await import("./mcp-business.mjs");
  const startedAt = new Date().toISOString();
  const runtimeControl = {
    transition: false,
    status: () => ({ available: true, scope: "cli-app-server", featureVersion: null, sha256: null,
      loadedAt: startedAt, loadCount: 0, activeImageSends: 0, reloadAvailable: false,
      autoReloadOnUse: false, lastReloadError: null }),
    reload: async () => ({ available: true, scope: "cli-app-server", reloadAvailable: false,
      lastReloadError: "business hot reload is only available inside the Codex plugin host" }),
  };
  const business = createBusiness(runtimeControl);
  let closing = false;
  const shutdown = async (signal) => {
    if (closing) return;
    closing = true;
    log("info", "shutdown", { signal });
    await business.close().catch(() => {});
    await appServer.shutdown().catch(() => {});
    await logFile.chain.catch(() => {});
    process.exit(0);
  };
  process.on("SIGINT", () => { void shutdown("SIGINT"); });
  process.on("SIGTERM", () => { void shutdown("SIGTERM"); });
  process.on("exit", () => { appServer.killSync(); });
  process.on("uncaughtException", (error) => {
    log("error", "uncaught_exception", { message: error?.message ?? String(error) });
    void shutdown("uncaughtException");
  });
  process.on("unhandledRejection", (error) => {
    log("error", "unhandled_rejection", { message: error?.message ?? String(error) });
  });

  await business.activate();
  log("info", "manager_started", { url: `http://${host}:${port}` });

  // Register the CLI session so trainer messages have a selected target.
  async function adoptSession() {
    const response = await fetch(`http://${host}:${port}/manager/sessions/adopt`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify({ protocolVersion: "1.1.1-draft", messageId: randomUUID(),
        source: "hermes", threadId: session.threadId, hostId: "local" }),
      signal: AbortSignal.timeout(35_000),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok) return { adopted: true, action: body.action ?? null };
    if (response.status === 409 && body.error === "adopt_conflict") return { adopted: false, alreadyRegistered: true };
    throw new Error(`${response.status} ${body.error ?? "adopt_failed"}: ${body.detail ?? ""}`.trim());
  }
  try {
    const adoption = await adoptSession();
    log("info", "session_adopted", { threadId: session.threadId, ...adoption });
  } catch (error) {
    log("warn", "session_adopt_failed", { threadId: session.threadId, message: error.message });
  }

  async function waitForFeishu(timeoutMs = 25_000) {
    const deadline = Date.now() + timeoutMs;
    let health = null;
    while (Date.now() < deadline) {
      try {
        const response = await fetch(`http://${host}:${port}/health`, {
          headers: { authorization: `Bearer ${token}` },
          signal: AbortSignal.timeout(3_000),
        });
        if (response.ok) {
          health = await response.json();
          if (health.integrations?.feishu?.connected) return health;
        }
      } catch { /* manager still starting */ }
      await sleep(500);
    }
    return health;
  }
  const health = await waitForFeishu() ?? await business.activate();
  const status = health?.integrations?.feishu ?? null;
  log("info", "manager_ready", {
    ready: Boolean(health?.ready), name: health?.name, integrationMode: health?.integrationMode,
    selectedThreadId: health?.integrations?.selectedThreadId,
    feishu: status ? { enabled: status.enabled, inboundEnabled: status.inboundEnabled,
      connected: status.connected, phase: status.connection?.phase ?? null,
      trainerWhitelistCount: status.trainerWhitelistCount } : null,
  });
  process.stdout.write(`${identity.display} listening on http://${host}:${port} ` +
    `(transport=app-server, name=${identity.name}, thread=${session.threadId}, ` +
    `feishu=${status?.connected ? "connected" : "disconnected"})\n`);

  await new Promise(() => {});
}

main().catch((error) => {
  log("error", "fatal", { message: error?.message ?? String(error) });
  process.stderr.write(`${label}: ${error?.stack ?? error}\n`);
  process.exit(1);
});
