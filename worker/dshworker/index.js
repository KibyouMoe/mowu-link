// DSHWorker · cordis 插件入口（进程内 agents）
//
// 与 cli.mjs 的关系：
//   cli.mjs      独立进程入口（spawn/headless 调试用），保持不变；
//   index.js     常驻 DSH profile 的插件形态，直接用 ctx.agents / ctx.sessions
//                在**本进程内**建 agent、跑多轮，不再起 dsh 子进程。
//
// 两种形态共用同一套业务层（desktop-manager.mjs / mcp-business.mjs /
// router-client.mjs / feishu.mjs）：插件把自己实现的原生传输注册成
// `dsh-inprocess`，业务层照旧通过 interface/transport/index.mjs 的
// resolveNativeTransport() 取用，工具名与返回结构与 interface/transport/dsh.mjs
// 逐字段一致，所以业务层一行都不用改。
//
// 身份仍然来自 identity.json（bridge/name/display/port/configFile/stateDir），
// 端口/飞书名来自 ~/.hermes/secrets/dshworker.config.json。插件默认**不**启动
// HTTP + 飞书业务层（8906 通常被 CLI 常驻进程占着），要启用就显式配置
// `business: true`（或 DSHWORKER_BUSINESS=1）；启动前会探测端口，占用则跳过。
//
// 2026-11-10 实测结论（写在这里，免得下次又绕回 spawn）：
//   进程内 agents 报 `DeepSeek Messages transport failed / TRANSPORT` 的根因
//   不是 provider 选择，而是进程继承了 ~/.zprofile 里那台局域网代理
//   （HTTPS_PROXY=http://192.168.3.102:10808，EHOSTDOWN）。dsh 的 HTTP 客户端
//   在请求时读环境变量，所以整轮只会重试 5 次后 TRANSPORT 失败。用回环代理
//   （或直接不设代理）就能正常跑通多轮。见 config.proxy：只在显式配置时才
//   改写进程代理，避免悄悄动宿主进程的环境。
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { createHash, randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

import {
  registerNativeTransport,
  resetNativeTransportCache,
} from "./interface/transport/index.mjs";
import { ProgressWatch, noProgressTimeoutMs, taskDeadline } from "./progress-watch.mjs";
import { normalizeSubscriptionConfig, taskBoardTerminalPhase, taskBoardText, taskBoardTitle } from "./router-tasks.mjs";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
export const name = "dshworker";
// 进程内驱动一轮对话只需要这三个服务；agent-loop 由 profile 负责提供
// factory（缺了会在 agents.create 时报 "no agent factory registered"）。
export const inject = ["agents", "sessions", "agentDefaultModel"];

import {
  VERSION,
  TRANSPORT_NAME,
  NAMESPACE,
  SELF_TEST_FILE,
  DEFAULT_WORKSPACE,
  DEFAULT_STATE_DIR,
  DEFAULT_TURN_TIMEOUT_MS,
  MAX_WAIT_MS,
  sleep,
  expandHome,
  stringArg,
  readJsonSync,
  writeJsonAtomic,
  createLogger,
  loadIdentity,
  loadConfig,
  InProcessTransport
} from "./interface/inprocess.mjs";

// ---------------------------------------------------------------------------
// 插件入口
// ---------------------------------------------------------------------------

export function apply(ctx, config = {}) {
  const log = createLogger(config.logFile ?? "~/.hermes/logs/dshworker-plugin.log");
  const identity = loadIdentity(config, log);
  const { file: configFile, value: workerConfig } = loadConfig(identity, config, log);
  const stateDir = path.resolve(expandHome(
    stringArg(config.stateDir) ?? stringArg(process.env.CODEXWORKER_STATE_DIR)
      ?? identity.stateDir ?? DEFAULT_STATE_DIR));

  // dsh 的 HTTP 代理策略由启动器在任何插件加载前就装好了（dsh-http-proxy：
  // "the launcher resolves and installs the policy for every profile before the
  // first plugin loads"）。所以插件里再改 process.env 已经晚了——这里只做诊断，
  // 不改宿主环境。
  const proxyEnv = {
    httpsProxy: process.env.https_proxy ?? process.env.HTTPS_PROXY ?? null,
    allProxy: process.env.all_proxy ?? process.env.ALL_PROXY ?? null,
  };
  const proxyHost = (() => {
    const value = proxyEnv.httpsProxy ?? proxyEnv.allProxy;
    if (!value) return null;
    try { return new URL(value).hostname; } catch { return value; }
  })();
  const loopbackProxy = !proxyHost || /^(127\.|localhost$|::1$|\[::1\]$)/i.test(proxyHost);
  if (!loopbackProxy) {
    log("warn", "proxy_not_loopback", {
      httpsProxy: proxyEnv.httpsProxy, allProxy: proxyEnv.allProxy,
      hint: "DSH 在插件加载前就固定了代理；这台主机不可达时每轮都会 TRANSPORT 失败。用回环代理或清空代理再启动 dsh。",
    });
  }

  const defaultSelection = ctx.get("agentDefaultModel")?.currentSelection?.() ?? {};
  const selection = {
    provider: stringArg(config.provider) ?? defaultSelection.provider,
    model: stringArg(config.model) ?? defaultSelection.model,
  };
  if (stringArg(config.reasoningEffort) ?? defaultSelection.reasoningEffort) {
    selection.reasoningEffort = stringArg(config.reasoningEffort) ?? defaultSelection.reasoningEffort;
  }
  const requestedWorkspace = path.resolve(expandHome(
    stringArg(config.workspace) ?? stringArg(workerConfig.dsh?.workspace) ?? DEFAULT_WORKSPACE));
  const isDir = (candidate) => {
    try { return fs.statSync(candidate).isDirectory(); } catch { return false; }
  };
  // 身份文件里的 workspace 可能属于别的机器/还没建；退到 ~/projects，再退到 $HOME。
  const fallbackWorkspace = [DEFAULT_WORKSPACE, os.homedir()].find(isDir) ?? os.homedir();
  const workspace = isDir(requestedWorkspace) ? requestedWorkspace : fallbackWorkspace;
  if (workspace !== requestedWorkspace) {
    log("warn", "workspace_fallback", { requested: requestedWorkspace, using: workspace });
  }
  const idleTimeoutMs = noProgressTimeoutMs({ noProgressTimeoutMs: config.noProgressTimeoutMs ?? workerConfig.noProgressTimeoutMs });

  const transport = new InProcessTransport({
    ctx, selection, cwd: workspace, stateDir, noProgressTimeoutMs: idleTimeoutMs,
    proxyHint: loopbackProxy ? null : (proxyEnv.httpsProxy ?? proxyEnv.allProxy),
    log: (level, event, fields) => log(level, event, fields),
  });

  // 业务层通过这个选择器取传输；注册后立刻刷新缓存，保证 mcp-business
  // 首次 import 时拿到的就是进程内实现。
  registerNativeTransport(TRANSPORT_NAME, {
    name: TRANSPORT_NAME,
    capabilities: Object.freeze({ fork: false, desktopJournal: false, callerTurn: false }),
    listTools: () => transport.listTools(),
    callTool: async (toolName, args, _invocation, timeoutMs) => {
      try {
        return { success: true, tool: toolName, namespace: NAMESPACE,
          contentItems: [{ type: "text", text: JSON.stringify(await transport.execute(toolName, args ?? {}, timeoutMs)) }] };
      } catch (error) {
        return { success: false, tool: toolName, namespace: NAMESPACE,
          contentItems: [{ type: "text", text: JSON.stringify({ success: false, error: "tool_failed", tool: toolName,
            reason: error?.message ?? String(error) }) }] };
      }
    },
    shutdown: () => transport.close("transport shutdown"),
  });
  process.env.CODEXWORKER_NATIVE_TRANSPORT = TRANSPORT_NAME;
  process.env.CODEXWORKER_STATE_DIR = stateDir;
  resetNativeTransportCache();

  let business = null;
  let routerClient = null;
  const api = {
    version: VERSION,
    transport: TRANSPORT_NAME,
    identity: { name: identity.name, display: identity.display, configFile },
    status: () => ({ ready: true, identity: identity.display, business: business !== null,
      router: routerClient !== null, ...transport.status() }),
    listTools: () => transport.listTools(),
    callTool: (toolName, args, timeoutMs) => transport.execute(toolName, args ?? {}, timeoutMs),
    threads: () => transport.listThreads({ limit: 100 }).threads,
    /** 便捷入口：一轮对话跑完再返回（给脚本/自检用）。 */
    async run(prompt, { threadId = null, cwd = null, timeoutMs = DEFAULT_TURN_TIMEOUT_MS } = {}) {
      const recent = [...transport.threads.values()]
        .sort((a, b) => (Date.parse(b.updatedAt ?? 0) || 0) - (Date.parse(a.updatedAt ?? 0) || 0))[0] ?? null;
      const thread = threadId
        ? transport.requireThread("run", { threadId })
        : recent ?? transport.threads.get(
          transport.createThread({ title: `${identity.display} · plugin`, cwd: cwd ?? workspace }).threadId);
      const sent = transport.sendMessage({ threadId: thread.id, prompt });
      const until = Number(timeoutMs) > 0 ? Date.now() + Number(timeoutMs) : Infinity;
      let turn;
      for (;;) {
        turn = thread.turns.find((candidate) => candidate.id === sent.turnId) ?? null;
        if (!turn || ["completed", "failed", "interrupted"].includes(turn.status) || Date.now() >= until) break;
        await transport.waitThreads({ targets: [{ threadId: thread.id }],
          timeoutMs: Math.min(MAX_WAIT_MS, until - Date.now()) });
      }
      return { threadId: thread.id, turnId: sent.turnId,
        timedOut: turn?.status === "inProgress" && Date.now() >= until,
        status: turn?.status ?? null, text: turn?.text ?? "", error: turn?.error ?? null,
        thinking: turn?.thinking ?? "" };
    },
    async close(reason) {
      transport.approvals.close();
      try { await routerClient?.stop(); } catch { /* ignore */ }
      routerClient = null;
      try { await business?.close(); } catch { /* ignore */ }
      business = null;
      await transport.close(reason ?? "plugin disposed");
    },
  };

  ctx.provide("dshworker", api);
  ctx.effect(() => () => { void api.close("plugin disposed").catch(() => {}); });

  const businessEnabled = config.business === true
    || process.env.DSHWORKER_BUSINESS === "1"
    || workerConfig.plugin?.business === true;
  const selfTestPrompt = stringArg(config.selfTest)
    ?? (process.env.DSHWORKER_SELFTEST === "1" ? "Reply with exactly: SELF-TEST-OK" : null);

  log("info", "plugin_loaded", {
    version: VERSION, identity: identity.display, configFile, stateDir,
    provider: selection.provider ?? null, model: selection.model ?? null,
    workspace, business: businessEnabled, transport: TRANSPORT_NAME,
  });

  void (async () => {
    await ctx.get("loader")?.await();
    if (selfTestPrompt) {
      await runSelfTest({ transport, identity, stateDir, workspace, prompt: selfTestPrompt, log })
        .catch((error) => log("error", "self_test_failed", { message: error?.message ?? String(error) }));
    }
    if (businessEnabled) {
      try {
        const started = await startBusiness({ ctx, transport, identity, workerConfig, config, stateDir,
          workspace, log });
        business = started.business;
        routerClient = started.routerClient;
      } catch (error) {
        log("error", "business_start_failed", { message: error?.message ?? String(error),
          code: error?.code ?? null });
      }
    }
  })().catch((error) => log("error", "boot_failed", { message: error?.message ?? String(error) }));
}

// ---------------------------------------------------------------------------
// 自检：用业务层同款工具跑两轮，第二轮必须能引用第一轮的答案
// ---------------------------------------------------------------------------

async function runSelfTest({ transport, identity, stateDir, workspace, prompt, log }) {
  const startedAt = Date.now();
  const created = transport.createThread({ title: `${identity.display} · self-test`, cwd: workspace });
  const threadId = created.threadId;
  log("info", "self_test_start", { threadId, prompt });

  const first = transport.sendMessage({ threadId, prompt });
  await transport.waitThreads({ targets: [{ threadId }], timeoutMs: 120_000 });
  const turn1 = transport.readThread({ threadId, turnLimit: 1, includeOutputs: true }).turns.at(-1) ?? null;

  const second = transport.sendMessage({
    threadId,
    prompt: "Repeat the exact token you replied with in my previous message. Answer with that token only.",
  });
  await transport.waitThreads({ targets: [{ threadId }], timeoutMs: 120_000 });
  const turn2 = transport.readThread({ threadId, turnLimit: 1, includeOutputs: true }).turns.at(-1) ?? null;

  const result = {
    ok: turn1?.status === "completed" && turn2?.status === "completed"
      && Boolean(turn1?.text) && String(turn2?.text ?? "").includes(String(turn1?.text ?? "").trim()),
    threadId, sessionId: transport.threads.get(threadId)?.sessionId ?? null,
    turn1: { id: first.turnId, status: turn1?.status ?? null, text: turn1?.text ?? "", error: turn1?.error ?? null },
    turn2: { id: second.turnId, status: turn2?.status ?? null, text: turn2?.text ?? "", error: turn2?.error ?? null },
    durationMs: Date.now() - startedAt,
    provider: transport.selection.provider ?? null, model: transport.selection.model ?? null,
  };
  await writeJsonAtomic(path.join(stateDir, SELF_TEST_FILE), result);
  log(result.ok ? "info" : "error", "self_test_done", {
    ok: result.ok, threadId, sessionId: result.sessionId, durationMs: result.durationMs,
    turn1: result.turn1.status, turn2: result.turn2.status,
    turn1Chars: result.turn1.text.length, turn2Chars: result.turn2.text.length,
  });
  return result;
}

// ---------------------------------------------------------------------------
// 可选：在本进程里跑完整业务层（HTTP + 飞书 + router）
// ---------------------------------------------------------------------------

function resolveFeishuConfig(value) {
  const configured = value.feishu?.configFile
    ?? path.join(os.homedir(), ".hermes", "secrets", "feishu-oguri.json");
  const file = path.resolve(expandHome(configured));
  const parsed = readJsonSync(file);
  if (!parsed?.app_id || !parsed?.app_secret) return { file, value: null };
  return { file, value: parsed };
}

function resolveWhitelist(value, feishuValue) {
  // 显式给的数组就是不等于"没配"：空数组表示明确关掉飞书入站，绝不回退到
  // 凭据文件里的 open_id（否则测试实例会悄悄连上真实飞书）。
  if (Array.isArray(value.feishu?.trainerOpenIdWhitelist)) {
    const ids = value.feishu.trainerOpenIdWhitelist
      .filter((id) => typeof id === "string" && id.trim()).map((id) => id.trim());
    return { ids, source: ids.length ? "config" : "config_empty" };
  }
  const trainer = stringArg(feishuValue?.trainer_open_id);
  const openId = stringArg(feishuValue?.open_id);
  const derived = trainer ?? openId;
  return derived ? { ids: [derived], source: trainer ? "feishu.trainer_open_id" : "feishu.open_id" } : { ids: [], source: "none" };
}

function portFree(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.once("listening", () => server.close(() => resolve(true)));
    try { server.listen(port, host); } catch { resolve(false); }
  });
}

async function startBusiness({ ctx, transport, identity, workerConfig, config, stateDir, workspace, log }) {
  const host = stringArg(config.host) ?? stringArg(workerConfig.host) ?? "127.0.0.1";
  const port = Number(config.port ?? identity.port ?? workerConfig.port ?? 8906);
  if (!await portFree(port, host)) {
    log("warn", "business_port_busy", { host, port,
      reason: "another worker owns this port; 插件不抢生产进程的 8906" });
    return { business: null, routerClient: null };
  }

  const tokenFile = path.resolve(expandHome(
    process.env.CODEXWORKER_TOKEN_FILE ?? workerConfig.tokenFile
      ?? path.join(os.homedir(), ".hermes", "secrets", "router.token")));
  const token = (() => {
    try { return fs.readFileSync(tokenFile, "utf8").trim().replace(/^Bearer\s+/i, ""); }
    catch { return ""; }
  })();
  const feishu = resolveFeishuConfig(workerConfig);
  if (!feishu.value) log("warn", "feishu_config_missing", { file: feishu.file });
  const whitelist = resolveWhitelist(workerConfig, feishu.value);

  // 任务看板目标：训练员与这个 worker 的私聊。显式 taskBoardOpenId 优先，其次
  // 入站白名单（就是训练员本人），最后才是凭据文件里的 open_id。和 cli.mjs 的
  // 表达式保持一致，写进 desktop-config 的那份也复用这里的值。
  const taskBoardOpenId = stringArg(workerConfig.feishu?.taskBoardOpenId) ?? whitelist.ids[0]
    ?? stringArg(feishu.value?.open_id) ?? null;
  const taskBoardEnabled = workerConfig.feishu?.taskBoardEnabled !== false && Boolean(taskBoardOpenId);

  const thread = transport.threads.values().next().value
    ?? transport.threads.get(transport.createThread({ title: `${identity.display} · DSH`, cwd: workspace }).threadId);
  const bindingPath = path.join(stateDir, "manager-binding.json");
  const desktopConfigPath = path.join(stateDir, "desktop-config.local.json");
  await writeJsonAtomic(bindingPath, {
    formatVersion: 1, integrationMode: "desktop-native",
    managerThreadId: thread.id, managerTurnId: null, boundAt: new Date().toISOString(),
  });
  const projects = await transport.execute("list_projects", {}, 5_000).catch(() => ({ projects: [] }));
  const project = (projects.projects ?? []).find((entry) => entry.path === thread.cwd && entry.hostId === "local");
  await writeJsonAtomic(desktopConfigPath, {
    host, port, bridgeName: identity.display, operatingMode: "feishu-local",
    machine: identity.name, tokenFile, workspaceRoots: [thread.cwd],
    ...(project?.projectId ? { projectId: project.projectId } : {}),
    pollMs: 1000,
    noProgressTimeoutMs: transport.noProgressTimeoutMs,
    feishu: {
      configFile: feishu.file,
      inboundEnabled: true,
      trainerOpenIdWhitelist: whitelist.ids,
      taskBoardOpenId,
      taskBoardEnabled: workerConfig.feishu?.taskBoardEnabled !== false,
      onlineNotificationEnabled: workerConfig.feishu?.onlineNotificationEnabled ?? false,
      streamEnabled: workerConfig.feishu?.streamEnabled ?? true,
      defaultDelivery: workerConfig.feishu?.defaultDelivery ?? "steer",
      autoImageOutput: workerConfig.feishu?.autoImageOutput ?? true,
    },
  });
  log("info", "manager_config_written", { binding: bindingPath, config: desktopConfigPath,
    whitelistCount: whitelist.ids.length, whitelistSource: whitelist.source });

  process.env.RUDOLF_BRIDGE_LIFECYCLE_CHILD = "1";
  process.env.RUDOLF_BRIDGE_LIFECYCLE_READY ??= "1";
  const { createBusiness } = await import("./mcp-business.mjs");
  const startedAt = new Date().toISOString();
  const runtimeControl = {
    transition: false,
    status: () => ({ available: true, scope: "plugin-inprocess", featureVersion: VERSION, sha256: null,
      loadedAt: startedAt, loadCount: 0, activeImageSends: 0, reloadAvailable: false,
      autoReloadOnStart: false, lastReloadError: null }),
    reload: async () => ({ available: true, scope: "plugin-inprocess", reloadAvailable: false,
      lastReloadError: "business hot reload is not wired in plugin mode" }),
  };
  const business = createBusiness(runtimeControl);
  await business.activate();
  log("info", "business_started", { url: `http://${host}:${port}`, threadId: thread.id });

  // 飞书凭据是懒加载的：desktop-config 里只写 configFile，不写 appId/appSecret，
  // 所以 activate() 时 manager 的 notifier 还是 null——思考/正文的飞书投递全被
  // 静默跳过。reload 一次才会去读凭据、建 notifier、连 ws。
  try {
    const reloaded = await business.manager?.()?.reloadIntegrations?.();
    const ok = Boolean(reloaded?.feishu?.enabled);
    log(ok ? "info" : "warn", "business_integrations_reloaded", {
      feishuEnabled: ok, connected: Boolean(reloaded?.feishu?.connected) });
  } catch (error) {
    log("warn", "business_reload_failed", { message: error?.message ?? String(error) });
  }

  transport.control.attachManager(business.manager?.());

  // 把绑定线程登记进 manager 的会话池：飞书/HTTP 才有明确的投递目标。
  // 与 cli.mjs 的 adoptSession 同一条路径（409 adopt_conflict 视为已登记）。
  try {
    const response = await fetch(`http://${host}:${port}/manager/sessions/adopt`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify({ protocolVersion: "1.1.1-draft", messageId: randomUUID(),
        source: "hermes", threadId: thread.id, hostId: "local" }),
      signal: AbortSignal.timeout(35_000),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok) log("info", "session_adopted", { threadId: thread.id, action: body.action ?? null });
    else log("warn", "session_adopt_failed", { threadId: thread.id, status: response.status,
      error: body.error ?? null, detail: body.detail ?? null });
  } catch (error) {
    log("warn", "session_adopt_failed", { threadId: thread.id, message: error?.message ?? String(error) });
  }

  // ---- 任务看板：本 worker 自己的飞书身份 -----------------------------------
  // 只把两条生命周期消息发到训练员与这个 worker 的私聊（start / failed）：
  // 成功后 manager 自己在这条会话里的回复已经承载了正文，再发一条「完成」只是
  // 重复。router 的回执（router → trigger）不受影响。
  // 优先借 manager 已经建好的 notifier（它带着绑定的 token / canSend 守卫），
  // manager 不可用时才用凭据文件自建一个，避免插件形态下多一条长连接。
  const taskBoardDryRun = process.env.DSHWORKER_FEISHU_DRY_RUN === "1"
    || process.env.CODEXWORKER_FEISHU_DRY_RUN === "1";
  let taskBoardFallback = null;
  async function taskBoardNotifier() {
    const managerNotifier = business.manager?.()?.notifier;
    if (managerNotifier?.enabled) return managerNotifier;
    if (!feishu.value) return null;
    if (!taskBoardFallback) {
      const { FeishuNotifier } = await import("./feishu.mjs");
      taskBoardFallback = new FeishuNotifier({ appId: feishu.value.app_id,
        appSecret: feishu.value.app_secret, bridgeName: identity.display });
    }
    return taskBoardFallback.enabled ? taskBoardFallback : null;
  }
  async function taskBoardNotify(taskId, phase, fields = {}) {
    let text;
    let title;
    try {
      text = taskBoardText({ phase, taskId, ...fields });
      title = taskBoardTitle({ phase, taskId }) ?? `${identity.display} · 任务看板`;
    } catch (error) {
      log("warn", "task_board_failed", { taskId, phase, message: error?.message ?? String(error) });
      return null;
    }
    // 干跑只打日志不发，隔离实例的测试就永远不会投到训练员私聊里。
    if (taskBoardDryRun) {
      log("info", "task_board_dry_run", { taskId, phase, title, text: text.slice(0, 200),
        textChars: text.length,
        targetOpenIdSha256: createHash("sha256").update(String(taskBoardOpenId)).digest("hex").slice(0, 12) });
      return "dry-run";
    }
    if (!taskBoardEnabled) { log("warn", "task_board_skipped", { taskId, phase, reason: "no_target" }); return null; }
    const notifier = await taskBoardNotifier().catch(() => null);
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

  // router（可选）：任务进入 manager 选中的同一个会话，和飞书共享上下文。
  const routerConfig = workerConfig.router ?? {};
  const routerEnabled = config.router !== false && process.env.CODEXWORKER_ROUTER !== "0"
    && routerConfig.enabled !== false;
  let routerClient = null;
  if (routerEnabled) {
    if (token.length < 24) {
      log("warn", "router_token_unavailable", { tokenFile, reason: "missing or too short" });
    } else {
      const { RouterClient } = await import("./router-client.mjs");
      const { PROTOCOL } = await import("./desktop-manager.mjs");
      const { routerMarker, routerMessageId, lastAssistantText, findRouterTurn, TERMINAL_TURN } =
        await import("./router-tasks.mjs");
      const routerUrl = String(process.env.CODEXWORKER_ROUTER_URL ?? routerConfig.url ?? "http://127.0.0.1:8790");
      let chain = Promise.resolve();
      const runTask = async (task) => {
        const taskId = stringArg(task?.task_id);
        const goal = stringArg(task?.goal);
        if (!taskId) return;
        const startedTask = Date.now();
        const marker = routerMarker(taskId);
        const messageId = routerMessageId(taskId);
        let status = "failed";
        let summary = "";
        let failure = "task runner error";
        log("info", "router_task_start", { taskId, messageId });
        // 开始行：训练员私聊里要能看见「来自超级溪流的指令」+ 目标原文。
        await taskBoardNotify(taskId, "start", { goal });
        try {
          if (!goal) throw new Error("router task has an empty goal");
          const manager = business.manager?.();
          if (!manager?.message) throw new Error("manager is not active");
          const selected = manager.state?.selectedThreadId && manager.state.sessions?.[manager.state.selectedThreadId]
            ? manager.state.selectedThreadId : thread.id;
          const deadline = taskDeadline(task, startedTask);
          transport.taskDeadlines.set(taskId, Number.isFinite(deadline) ? deadline : null);
          await manager.message({ protocolVersion: PROTOCOL, messageId, source: "hermes", origin: "router",
            text: `${marker}\n${goal}`, targetThreadId: selected, delivery: "steer" });
          const watch = new ProgressWatch({ deadline, timeoutMs: transport.noProgressTimeoutMs });
          let turn = null;
          for (;;) {
            const rejection = manager.state.receipts?.[messageId]?.error;
            if (rejection) throw new Error(`${rejection.error}: ${rejection.detail ?? "dispatch rejected"}`);
            const snapshot = await transport.execute("read_thread", { threadId: selected, turnLimit: 30, includeOutputs: false });
            const found = turn ? snapshot.turns.find((item) => item.id === turn.id) ?? null : findRouterTurn(snapshot, marker);
            if (found) turn = found;
            if (found && TERMINAL_TURN.has(found.status)) break;
            watch.observe(found ?? snapshot.turns);
            const failure = watch.failure();
            if (failure) {
              await transport.control.cancelTask(taskId).catch(error =>
                log("error", "router_task_cancel_failed", { taskId, message: error.message }));
              throw new Error(failure.message);
            }
            await sleep(Math.max(1, Math.min(1_000, watch.remainingMs())));
          }
          status = turn?.status === "completed" ? "done" : "failed";
          summary = lastAssistantText(turn) || (turn ? "" : "task turn never appeared");
          if (status !== "done") {
            failure = turn?.error || (turn ? `turn ${turn.status}` : "task turn never appeared");
            summary = summary || failure;
          }
        } catch (error) {
          summary = error?.message ?? String(error);
          failure = summary;
          log("error", "router_task_failed", { taskId, message: summary });
        }
        transport.taskDeadlines.delete(taskId);
        // 成功不单独发：manager 在这条会话里的回复已经到训练员私聊了。
        const boardPhase = taskBoardTerminalPhase(status);
        if (boardPhase) await taskBoardNotify(taskId, boardPhase, { summary, failure });
        await routerClient.result({ taskId, status, summary }).catch((error) => {
          log("error", "router_result_failed", { taskId, message: error?.message ?? String(error) });
        });
      };
      routerClient = new RouterClient({
        url: routerUrl, token, name: identity.name, version: VERSION,
        subscriptions: normalizeSubscriptionConfig(workerConfig.subscriptions),
        logger: (level, event, fields = {}) => log(level, event, { component: "router", ...fields }),
      });
      transport.control.bindRouter(routerClient, `http://${host}:${port}`);
      routerClient.start({
        onTask: (task) => { chain = chain.then(() => runTask(task)).catch((error) =>
          log("error", "router_chain_error", { message: error?.message ?? String(error) })); return chain; },
        onConnect: ({ attempt, reconnects }) => log("info", "router_connected", { attempt, reconnects }),
        onDisconnect: ({ reason, delayMs, reconnects }) => log("warn", "router_disconnected", { reason, delayMs, reconnects }),
      });
      log("info", "router_client_started", { url: routerUrl, name: identity.name });
    }
  }
  return { business, routerClient };
}
