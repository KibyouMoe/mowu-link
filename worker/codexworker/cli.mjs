#!/usr/bin/env node
// Worker CLI entry: one native transport + the shared HTTP + Feishu manager.
//
// This is the "no Desktop" launcher. It drives exactly one implementation from
// interface/transport/ (chosen by identity.transport, then
// CODEXWORKER_NATIVE_TRANSPORT), persists a CLI session, writes the manager
// binding + local desktop config that mcp-business.mjs already reads, and then
// starts the existing HTTP + Feishu manager unchanged.
//
// Nothing here reimplements business logic. The sequence is:
//   identity.json → cli-session.json → manager-binding.json →
//   desktop-config.local.json → createBusiness(...).activate()
//
// Identity is mandatory: name is the internal id (health.name, state), display
// is what a human sees (Feishu titles, health.display), and transport names the
// native implementation. No identity → exit 2.
//
// The manager's writer lease lives under the state directory declared by the
// identity (or CODEXWORKER_STATE_DIR); this process is the sole writer, so it
// sets RUDOLF_BRIDGE_LIFECYCLE_CHILD=1 the same way the Desktop lifetime
// supervisor does.
import { appendFile, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import RouterClient, { setActiveRouterClient } from "./router-client.mjs";
import { FeishuNotifier } from "./feishu.mjs";
import { PROTOCOL } from "./desktop-manager.mjs";
import { ROUTER_TURN_LIMIT_MS, TERMINAL_TURN, findRouterTurn, lastAssistantText,
  normalizeSubscriptionConfig, routerMarker, routerMessageId, routerPrompt,
  subscriptionMarker, subscriptionMessageId, subscriptionNoticeText,
  taskBoardTerminalPhase, taskBoardText, taskBoardTitle } from "./router-tasks.mjs";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PROGRAM = path.basename(ROOT);
// Keep in step with BUSINESS_VERSION in mcp-business.mjs.
const VERSION = "0.2.38";
// Neutral fallback only: each identity declares its own stateDir so an existing
// install keeps its writer lease exactly where it was.
const DEFAULT_STATE_DIR = path.join(os.homedir(), ".hermes", "state", PROGRAM);

const HELP = `${PROGRAM} ${VERSION}

用法:
  ${PROGRAM} [选项]            启动原生传输 + HTTP + 飞书 worker

选项:
  --identity <file>     实例身份文件（默认 ./identity.json：name/display/port/configFile/transport/stateDir）
  --config <file.json>  覆盖 identity.configFile 的配置文件
  --host <addr>         监听地址（默认 127.0.0.1）
  --port <n>            监听端口（默认取 identity.port，其次 config.port，最后 8903）
  --log <file>          追加 JSONL 日志（支持 {name}/{display}/{port} 占位符；默认只写 stderr）
  -h, --help            显示本帮助
  -v, --version         显示版本

环境变量:
  CODEXWORKER_IDENTITY, CODEXWORKER_CONFIG, CODEXWORKER_HOST, CODEXWORKER_PORT,
  CODEXWORKER_LOG, CODEXWORKER_STATE_DIR, CODEXWORKER_TOKEN_FILE,
  CODEXWORKER_NATIVE_TRANSPORT（identity 未声明 transport 时使用）,
  CODEXWORKER_ROUTER (0 关闭 router 连接), CODEXWORKER_ROUTER_URL,
  以及各 transport 自己的变量（见 interface/transport/*.mjs）

说明:
  1. 原生传输由 interface/transport/index.mjs 选择：identity.json 的 transport 字段优先，
     其次 CODEXWORKER_NATIVE_TRANSPORT，两者都没有则拒绝启动。
  2. 主会话保存在 <stateDir>/cli-session.json，重启复用；读不到时重建。
  3. 写入 <stateDir>/manager-binding.json 与 desktop-config.local.json，
     再把该会话登记到 manager（飞书可直接发消息）。
  4. SIGINT/SIGTERM 会同时关掉 manager 与原生传输子进程。
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
    const transport = typeof value.transport === "string" && value.transport.trim() ? value.transport.trim() : null;
    const stateDir = typeof value.stateDir === "string" && value.stateDir.trim() ? expandHome(value.stateDir.trim()) : null;
    return { file, name, display, transport, stateDir,
      bridge: typeof value.bridge === "string" && value.bridge.trim() ? value.bridge.trim() : PROGRAM,
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
  // 把用户配置里的模型与思考强度带给 app-server transport：
  // thread/start 用 CODEXWORKER_MODEL，turn/start 用 CODEXWORKER_REASONING_EFFORT。
  const codexConfig = config?.codex ?? {};
  if (typeof codexConfig.model === "string" && codexConfig.model.trim()) {
    process.env.CODEXWORKER_MODEL = codexConfig.model.trim();
  }
  if (typeof codexConfig.reasoningEffort === "string" && codexConfig.reasoningEffort.trim()) {
    process.env.CODEXWORKER_REASONING_EFFORT = codexConfig.reasoningEffort.trim();
  }
  const host = args.host ?? process.env.CODEXWORKER_HOST ?? config.host ?? "127.0.0.1";
  const port = resolvePort(args.port ?? process.env.CODEXWORKER_PORT ?? identity.port ?? config.port, 8903);
  const stateDir = path.resolve(expandHome(process.env.CODEXWORKER_STATE_DIR ?? identity.stateDir ?? DEFAULT_STATE_DIR));
  // mcp-business reads the binding/config/lease from this same directory; pin it
  // so an isolated instance (test port) never touches the production writer.
  process.env.CODEXWORKER_STATE_DIR = stateDir;
  const bindingStatePath = path.join(stateDir, "manager-binding.json");
  const desktopConfigPath = path.join(stateDir, "desktop-config.local.json");
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
  // 任务看板：worker 用自己的飞书身份给训练员发「开始」与「失败」两条生命周期
  // 消息；成功任务由 manager 的正常回复收尾，不再重复发「完成」。
  // 目标 open_id 必须在**本应用**下有效（跨应用会 open_id cross app）。
  const taskBoardOpenId = String(config.feishu?.taskBoardOpenId ?? "").trim()
    || whitelist.ids[0] || String(feishu.value.open_id ?? "").trim();

  log("info", "identity_loaded", { name: identity.name, display: identity.display,
    identityFile: identity.file, configFile });
  // The transport selector reads this at resolution time; set it before any
  // business import can call resolveNativeTransport(). The identity owns the
  // default so one CLI serves every engine; the env var is the escape hatch.
  const transportName = identity.transport || process.env.CODEXWORKER_NATIVE_TRANSPORT?.trim();
  if (!transportName) fail("identity 未声明 transport，且未设置 CODEXWORKER_NATIVE_TRANSPORT");
  process.env.CODEXWORKER_NATIVE_TRANSPORT = transportName;
  // This process owns the manager lifetime; no Desktop supervisor exists.
  process.env.RUDOLF_BRIDGE_LIFECYCLE_CHILD = "1";
  process.env.RUDOLF_BRIDGE_LIFECYCLE_READY ??= "1";

  const transportIndex = await import("./interface/transport/index.mjs");
  const transport = transportIndex.resolveNativeTransport();
  log("info", "start", { version: VERSION, host, port, cwd, stateDir, transport: transport.name });

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
  await writeJsonAtomic(bindingStatePath, {
    formatVersion: 1,
    integrationMode: "desktop-native",
    managerThreadId: session.threadId,
    managerTurnId: null,
    boundAt: new Date().toISOString(),
  });
  await writeJsonAtomic(desktopConfigPath, {
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
      taskBoardOpenId: taskBoardOpenId || null,
      taskBoardEnabled: config.feishu?.taskBoardEnabled !== false,
      onlineNotificationEnabled: config.feishu?.onlineNotificationEnabled ?? false,
      streamEnabled: config.feishu?.streamEnabled ?? true,
      defaultDelivery: config.feishu?.defaultDelivery ?? "steer",
      autoImageOutput: config.feishu?.autoImageOutput ?? true,
    },
  });
  log("info", "manager_config_written", { binding: bindingStatePath, config: desktopConfigPath,
    whitelistCount: whitelist.ids.length, whitelistSource: whitelist.source,
    feishuInbound: whitelist.ids.length > 0 ? "enabled" : "disabled_no_whitelist" });

  // ---- business / manager --------------------------------------------------
  const { createBusiness } = await import("./mcp-business.mjs");
  const startedAt = new Date().toISOString();
  const runtimeControl = {
    transition: false,
    status: () => ({ available: true, scope: `cli-${transport.name}`, featureVersion: null, sha256: null,
      loadedAt: startedAt, loadCount: 0, activeImageSends: 0, reloadAvailable: false,
      autoReloadOnUse: false, lastReloadError: null }),
    reload: async () => ({ available: true, scope: `cli-${transport.name}`, reloadAvailable: false,
      lastReloadError: "business hot reload is only available inside the plugin host" }),
  };
  const business = createBusiness(runtimeControl);
  let routerClient = null;
  let closing = false;
  const shutdown = async (signal) => {
    if (closing) return;
    closing = true;
    log("info", "shutdown", { signal });
    await routerClient?.stop().catch(() => {});
    await business.close().catch(() => {});
    await transport.shutdown().catch(() => {});
    await logFile.chain.catch(() => {});
    process.exit(0);
  };
  process.on("SIGINT", () => { void shutdown("SIGINT"); });
  process.on("SIGTERM", () => { void shutdown("SIGTERM"); });
  process.on("exit", () => { transport.killSync(); });
  process.on("uncaughtException", (error) => {
    log("error", "uncaught_exception", { message: error?.message ?? String(error) });
    void shutdown("uncaughtException");
  });
  process.on("unhandledRejection", (error) => {
    log("error", "unhandled_rejection", { message: error?.message ?? String(error) });
  });

  await business.activate();
  log("info", "manager_started", { url: `http://${host}:${port}` });

  // ---- router integration --------------------------------------------------
  // The router owns task dispatch. A router task is NOT a new native thread: it
  // is delivered into the manager's selected session — the same conversation the
  // Feishu chat uses — so both channels share context AND the manager's single
  // writer serializes them. Disable with CODEXWORKER_ROUTER=0 or
  // config.router.enabled=false (the settings UI and tests use that path).
  const routerConfig = config.router ?? {};
  const routerEnabled = process.env.CODEXWORKER_ROUTER !== "0" && routerConfig.enabled !== false;
  const routerUrl = String(process.env.CODEXWORKER_ROUTER_URL ?? routerConfig.url ?? "http://127.0.0.1:8790");
  const ROUTER_QUEUE_POLL_MS = 1_500;

  // ---- task board: this worker's own Feishu identity ------------------------
  // Only two lifecycle lines go to the trainer's p2p chat with THIS worker
  // (start / failed). A successful task ends with the manager's own reply in
  // the same chat, so a "完成" line would duplicate it. Result relaying to the
  // requester stays on the router → trigger path and is untouched.
  const taskBoardEnabled = config.feishu?.taskBoardEnabled !== false && Boolean(taskBoardOpenId);
  let fallbackNotifier = null;
  function taskBoardNotifier() {
    if (!taskBoardEnabled) return null;
    const managerNotifier = business.manager?.()?.notifier;
    if (managerNotifier?.enabled) return managerNotifier;
    fallbackNotifier ??= new FeishuNotifier({ appId: feishu.value.app_id, appSecret: feishu.value.app_secret,
      bridgeName: identity.display });
    return fallbackNotifier.enabled ? fallbackNotifier : null;
  }
  async function taskBoardNotify(taskId, phase, fields) {
    const text = taskBoardText({ phase, taskId, ...fields });
    const title = taskBoardTitle({ phase, taskId }) ?? `${identity.display} · 任务看板`;
    // Dry run exists for isolated test instances: the payload is logged (never
    // sent) so a test can never post into a real trainer chat.
    if (process.env.CODEXWORKER_FEISHU_DRY_RUN === "1") {
      log("info", "task_board_dry_run", { taskId, phase, title, text: text.slice(0, 200),
        textChars: text.length, targetOpenIdSha256: createHash("sha256").update(String(taskBoardOpenId)).digest("hex").slice(0, 12) });
      return "dry-run";
    }
    if (!taskBoardEnabled) { log("warn", "task_board_skipped", { taskId, phase, reason: "no_target" }); return null; }
    const notifier = taskBoardNotifier();
    if (!notifier) { log("warn", "task_board_skipped", { taskId, phase, reason: "feishu_disabled" }); return null; }
    let delivery = null;
    try {
      const response = await notifier.sendMessage(text, { openId: taskBoardOpenId, receiveIdType: "open_id",
        title, onDelivery: (entry) => { delivery = entry; } });
      const messageId = response?.data?.message_id ?? delivery?.messageId ?? null;
      log(messageId ? "info" : "warn", "task_board_sent", { taskId, phase, messageId,
        apiCode: Number.isInteger(response?.code) ? response.code : null, deliveryPhase: delivery?.phase ?? null });
      return messageId;
    } catch (error) {
      log("warn", "task_board_failed", { taskId, phase, message: error?.message ?? String(error) });
      return null;
    }
  }

  // The manager's `queue` is not drained for a CLI self-target: the scanner takes
  // the Desktop public-journal branch and `self_task_journal_unavailable` skips
  // its dispatcher, so a queued entry would never run. Serialization therefore
  // happens here — wait until the shared thread is idle — and the write itself
  // uses the same single-writer path (and delivery mode) the Feishu chat uses.
  async function waitForIdleThread(threadId, deadline) {
    for (;;) {
      const wait = await nativeCall("wait_threads", { targets: [{ threadId, hostId: "local" }], timeoutMs: 0 }, 20_000)
        .catch((error) => { log("warn", "router_idle_check_failed", { threadId, message: error?.message ?? String(error) }); return null; });
      const turn = wait?.polls?.[0]?.latestTurn ?? null;
      if (!turn || TERMINAL_TURN.has(turn.status)) return true;
      if (Date.now() >= deadline) return false;
      await sleep(1_000);
    }
  }

  // Poll the shared thread for our own turn (identified by the marker) and wait
  // for it to reach a terminal status. The manager keeps the writer; this only
  // reads native state, so it never competes for the writer.
  async function waitForRouterTurn({ threadId, marker, messageId, manager, deadline }) {
    let turn = null;
    let status = null;
    let misses = 0;
    while (Date.now() < deadline) {
      const rejection = manager.state.receipts?.[messageId]?.error;
      if (rejection) throw new Error(`${rejection.error}: ${rejection.detail ?? "dispatch rejected"}`);
      const snapshot = await nativeCall("read_thread", { threadId, turnLimit: 30, includeOutputs: false }, 60_000);
      const found = turn ? (snapshot?.turns ?? []).find((item) => item.id === turn.id) ?? null
        : findRouterTurn(snapshot, marker);
      if (!turn && found) turn = found;
      if (found) {
        misses = 0;
        status = found.status;
        if (TERMINAL_TURN.has(status)) return { turn: found, status, timedOut: false };
      } else if (turn && ++misses > 20) {
        throw new Error(`turn ${turn.id} left the readable window before it completed`);
      }
      const remaining = Math.max(1_000, Math.min(180_000, deadline - Date.now()));
      await nativeCall("wait_threads", { targets: [{ threadId, hostId: "local" }], timeoutMs: remaining }, remaining + 15_000)
        .catch((error) => log("warn", "router_wait_threads_failed", { messageId, message: error?.message ?? String(error) }));
      if (!found && turn) await sleep(ROUTER_QUEUE_POLL_MS);
    }
    return { turn, status, timedOut: true };
  }

  async function routerTurnReply(threadId) {
    try {
      const snapshot = await nativeCall("read_thread", { threadId, turnLimit: 1, includeOutputs: true }, 60_000);
      const turn = Array.isArray(snapshot?.turns) ? snapshot.turns.at(-1) : null;
      return lastAssistantText(turn);
    } catch (error) {
      log("warn", "router_read_thread_failed", { threadId, message: error?.message ?? String(error) });
      return "";
    }
  }

  async function runRouterTask(task) {
    const taskId = typeof task?.task_id === "string" ? task.task_id.trim() : "";
    if (!taskId) { log("warn", "router_task_without_id"); return; }
    const goal = typeof task?.goal === "string" ? task.goal.trim() : "";
    const startedAt = Date.now();
    const messageId = routerMessageId(taskId);
    const marker = routerMarker(taskId);
    let threadId = null;
    let turnId = null;
    let status = "failed";
    let summary = "";
    let failure = "task runner error";
    routerClient.setActivity({ state: "running", taskId });
    log("info", "router_task_start", { taskId, urgency: task.urgency ?? null, messageId });
    await taskBoardNotify(taskId, "start", { goal });
    try {
      if (!goal) throw new Error("router task has an empty goal");
      const manager = business.manager?.();
      if (!manager) throw new Error("manager is not active");
      const selected = manager.state.selectedThreadId;
      if (!selected || !manager.state.sessions[selected]) throw new Error("manager has no selected session");
      threadId = selected;
      const sessionCwd = manager.state.sessions[selected].cwd ?? session.cwd;
      if (task.workspace) {
        const requested = path.resolve(expandHome(String(task.workspace)));
        if (path.resolve(sessionCwd) !== requested) {
          log("warn", "router_task_workspace_ignored", { taskId, threadId,
            requested, sessionCwd: path.resolve(sessionCwd) });
        }
      }
      const declaredMs = Number(task.deadline_s);
      const budgetMs = Number.isFinite(declaredMs) && declaredMs > 0 ? declaredMs * 1000 : ROUTER_TURN_LIMIT_MS;
      const deadline = startedAt + budgetMs;
      if (!await waitForIdleThread(selected, deadline)) {
        throw new Error(`shared thread stayed busy past the ${Math.round(budgetMs / 1000)}s budget`);
      }
      const enqueued = await manager.message({
        protocolVersion: PROTOCOL, messageId, source: "hermes", origin: "router",
        text: routerPrompt({ taskId, goal, context: task.context }), targetThreadId: selected, delivery: "steer",
      });
      log("info", "router_task_sent", { taskId, threadId: selected, messageId,
        action: enqueued?.action ?? null, duplicate: enqueued?.duplicate === true });

      const waited = await waitForRouterTurn({ threadId: selected, marker, messageId, manager, deadline });
      const turn = waited.turn;
      turnId = turn?.id ?? null;
      const turnStatus = waited.status ?? "unknown";
      let text = lastAssistantText(turn);
      if (!text.trim() && turnId) text = await routerTurnReply(selected);
      text = text.trim();
      const durationS = Math.round((Date.now() - startedAt) / 1000);
      if (turnStatus === "completed") {
        status = "done";
        summary = text || "(completed; no assistant message)";
      } else {
        status = "failed";
        const reason = waited.timedOut
          ? `turn ${turnStatus} after ${durationS}s (deadline hit)`
          : `turn ${turnStatus}`;
        summary = text ? `${reason}\n\n${text}` : reason;
        failure = reason;
      }
      log(status === "done" ? "info" : "warn", "router_task_finished", {
        taskId, status, turnStatus, threadId, turnId, durationS, summaryChars: summary.length,
      });
    } catch (error) {
      status = "failed";
      failure = error?.message ?? String(error);
      summary = `task runner error: ${failure}`;
      log("error", "router_task_failed", { taskId, message: failure, threadId });
    } finally {
      routerClient.setActivity({ state: "idle", taskId: null });
    }

    // A successful task is already announced by the manager's reply in this
    // chat; only a failure needs a board line of its own.
    const boardPhase = taskBoardTerminalPhase(status);
    if (boardPhase) await taskBoardNotify(taskId, boardPhase, { summary, failure });

    try {
      const receipt = await routerClient.result({
        taskId, status, summary: summary.slice(0, 20_000),
        artifacts: [], evidence: [{ type: "codex_thread", thread_id: threadId, turn_id: turnId }],
      });
      log("info", "router_result_sent", {
        taskId, status, state: receipt?.state ?? null, duplicate: receipt?.duplicate === true,
      });
    } catch (error) {
      // At-least-once: the router requeues a task whose result never arrived.
      log("error", "router_result_failed", {
        taskId, status, code: error?.code ?? null, message: error?.message ?? String(error),
      });
    }
  }

  // The router dispatches one running task per worker, but serialize here too:
  // a duplicate delivery after a reconnect must never start two turns at once.
  let routerChain = Promise.resolve();
  function enqueueRouterTask(task) {
    routerChain = routerChain
      .then(() => runRouterTask(task))
      .catch((error) => log("error", "router_task_chain_error", {
        message: error?.message ?? String(error),
      }));
    return routerChain;
  }

  // ---- event subscriptions --------------------------------------------------
  // config.subscriptions = [{source, kinds?}] (top level; absent = none).
  // Realtime only: the router deletes the subscription when the stream drops,
  // and the client redeclares it on the next connect. Each event becomes one
  // manager message in the selected session, plus one Feishu line from this
  // worker's own identity. The router client already runs handler calls
  // strictly one at a time, so two events can never write the same thread
  // concurrently.
  const subscriptionConfig = normalizeSubscriptionConfig(config.subscriptions);
  const declaredTurnLimitMs = Number(config.subscriptionTurnLimitMs);
  const subscriptionTurnLimitMs = Number.isFinite(declaredTurnLimitMs) && declaredTurnLimitMs > 0
    ? declaredTurnLimitMs : ROUTER_TURN_LIMIT_MS;

  async function subscriptionNotify({ source, kind, id, text }) {
    const body = subscriptionNoticeText({ source, text });
    const title = `${identity.display} · 订阅事件`;
    // Same dry-run switch as the task board: an isolated test instance must
    // never post into a real trainer chat.
    if (process.env.CODEXWORKER_FEISHU_DRY_RUN === "1") {
      log("info", "subscription_notify_dry_run", { source, kind, id, title, text: body,
        textChars: body.length,
        targetOpenIdSha256: createHash("sha256").update(String(taskBoardOpenId)).digest("hex").slice(0, 12) });
      return "dry-run";
    }
    if (!taskBoardEnabled) { log("warn", "subscription_notify_skipped", { source, id, reason: "no_target" }); return null; }
    const notifier = taskBoardNotifier();
    if (!notifier) { log("warn", "subscription_notify_skipped", { source, id, reason: "feishu_disabled" }); return null; }
    try {
      const response = await notifier.sendMessage(body, { openId: taskBoardOpenId, receiveIdType: "open_id", title });
      const messageId = response?.data?.message_id ?? null;
      log(messageId ? "info" : "warn", "subscription_notified", { source, kind, id, messageId,
        apiCode: Number.isInteger(response?.code) ? response.code : null });
      return messageId;
    } catch (error) {
      log("warn", "subscription_notify_failed", { source, id, message: error?.message ?? String(error) });
      return null;
    }
  }

  async function runSubscriptionEvent(event) {
    const source = String(event?.source ?? "").trim() || "(unknown)";
    const kind = String(event?.kind ?? "").trim() || "event";
    const id = event?.id ?? null;
    const body = typeof event?.text === "string" ? event.text : String(event?.text ?? "");
    const startedAt = Date.now();
    const marker = subscriptionMarker({ source, kind, id });
    const messageId = subscriptionMessageId({ source, id });
    let status = "failed";
    let threadId = null;
    let turnId = null;
    let reply = "";
    let failure = "";
    log("info", "subscription_event_start", { source, kind, id, chars: body.length, messageId,
      subscriptionId: event?.subscription_id ?? null });
    await subscriptionNotify({ source, kind, id, text: body });
    try {
      const manager = business.manager?.();
      if (!manager) throw new Error("manager is not active");
      const selected = manager.state.selectedThreadId;
      if (!selected || !manager.state.sessions[selected]) throw new Error("manager has no selected session");
      threadId = selected;
      if (!await waitForIdleThread(selected, startedAt + subscriptionTurnLimitMs)) {
        throw new Error(`shared thread stayed busy past the ${Math.round(subscriptionTurnLimitMs / 1000)}s budget`);
      }
      const enqueued = await manager.message({
        protocolVersion: PROTOCOL, messageId, source: "hermes", origin: "subscription",
        text: `${marker}\n${body}`, targetThreadId: selected, delivery: "steer",
      });
      log("info", "subscription_message_sent", { source, kind, id, threadId: selected,
        action: enqueued?.action ?? null, duplicate: enqueued?.duplicate === true });
      const waited = await waitForRouterTurn({ threadId: selected, marker, messageId, manager,
        deadline: startedAt + subscriptionTurnLimitMs });
      turnId = waited.turn?.id ?? null;
      status = waited.status === "completed" ? "done" : "failed";
      reply = lastAssistantText(waited.turn);
      if (status !== "done") failure = waited.timedOut ? "deadline hit" : `turn ${waited.status ?? "unknown"}`;
    } catch (error) {
      failure = error?.message ?? String(error);
      log("error", "subscription_event_failed", { source, kind, id, message: failure });
    }
    log(status === "done" ? "info" : "warn", "subscription_event_finished", {
      source, kind, id, status, threadId, turnId,
      durationS: Math.round((Date.now() - startedAt) / 1000),
      replyChars: reply.length, reply: reply.slice(0, 200),
      ...(failure ? { failure } : {}),
    });
  }

  let subscriptionChain = Promise.resolve();
  function enqueueSubscriptionEvent(event) {
    // Belt and braces: the client serializes handler calls, this chain keeps
    // the ordering even if a caller invokes us directly.
    subscriptionChain = subscriptionChain
      .then(() => runSubscriptionEvent(event))
      .catch((error) => log("error", "subscription_chain_error", { message: error?.message ?? String(error) }));
    return subscriptionChain;
  }

  if (routerEnabled) {
    routerClient = new RouterClient({
      url: routerUrl,
      token,
      name: identity.name,
      version: VERSION,
      heartbeatMs: routerConfig.heartbeatMs,
      subscriptions: subscriptionConfig,
      subscriptionPollMs: config.subscriptionPollMs ?? routerConfig.subscriptionPollMs,
      logger: (level, event, fields = {}) => log(level, event, { component: "router", ...fields }),
    });
    setActiveRouterClient(routerClient);
    routerClient.start({
      onTask: enqueueRouterTask,
      onSubscriptionEvent: enqueueSubscriptionEvent,
      onConnect: ({ attempt, reconnects }) => log("info", "router_connected", { attempt, reconnects }),
      onDisconnect: ({ reason, delayMs, reconnects }) => log("warn", "router_disconnected", {
        reason, delayMs, reconnects,
      }),
    });
    log("info", "router_client_started", { url: routerUrl, name: identity.name,
      subscriptions: subscriptionConfig.map((item) => item.source) });
  } else {
    log("info", "router_client_disabled", { url: routerUrl });
  }

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
    `(transport=${transport.name}, name=${identity.name}, thread=${session.threadId}, ` +
    `feishu=${status?.connected ? "connected" : "disconnected"})\n`);

  await new Promise(() => {});
}

main().catch((error) => {
  log("error", "fatal", { message: error?.message ?? String(error) });
  process.stderr.write(`${label}: ${error?.stack ?? error}\n`);
  process.exit(1);
});
