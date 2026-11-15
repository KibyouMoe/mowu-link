// DSHWorker · cordis 插件入口（进程内 agents）
//
// 注入 agents / sessions / agentDefaultModel，在宿主进程内管理多轮会话。
// 原生传输注册为 dsh-inprocess，由 interface/transport/index.mjs 接入
// 与 Codex 共用的 desktop-manager、mcp-business、router-client 和 feishu。
// identity.json 指定实例身份及配置位置。HTTP / 飞书业务层通过 business、
// DSHWORKER_BUSINESS 或 workerConfig.plugin.business 显式启用，启动前检查端口。
// HTTP 代理策略由 DSH 启动器安装，插件读取环境并记录连接诊断。
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

import {
  registerNativeTransport,
  resetNativeTransportCache,
} from "./interface/transport/index.mjs";
import { noProgressTimeoutMs } from "./progress-watch.mjs";
import { startDshRouterRuntime } from "./interface/router-runtime.mjs";
import { createInProcessAdapter } from "./interface/transport/inprocess.mjs";
import { registerCollaborationHost } from "./interface/collaboration-host.mjs";
import { collaborationUrl } from "./collaboration-view.mjs";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
export const name = "dshworker";
// 进程内驱动一轮对话只需要这三个服务；agent-loop 由 profile 负责提供
// factory（缺了会在 agents.create 时报 "no agent factory registered"）。
export const inject = ["agents", "sessions", "agentDefaultModel"];

import {
  VERSION,
  TRANSPORT_NAME,
  SELF_TEST_FILE,
  DEFAULT_WORKSPACE,
  DEFAULT_STATE_DIR,
  DEFAULT_TURN_TIMEOUT_MS,
  MAX_WAIT_MS,
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
    stringArg(config.stateDir) ?? stringArg(process.env.DSHWORKER_STATE_DIR)
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
  registerNativeTransport(TRANSPORT_NAME, createInProcessAdapter(transport));
  process.env.CODEXWORKER_NATIVE_TRANSPORT = TRANSPORT_NAME;
  process.env.CODEXWORKER_STATE_DIR = stateDir;
  resetNativeTransportCache();

  let business = null;
  let routerClient = null;
  let routerRuntime = null;
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
      const sent = await transport.execute("send_message_to_thread", { threadId: thread.id, prompt });
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
      try { await routerRuntime?.close(); } catch { /* ignore */ }
      routerRuntime = null;
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
    await registerCollaborationHost(ctx, () => {
      const local = readJsonSync(path.join(stateDir, "desktop-config.local.json"));
      const token = fs.readFileSync(expandHome(local.tokenFile), "utf8").trim().replace(/^Bearer\s+/i, "");
      return { url: collaborationUrl({ config: { token, port: local.port } }) };
    }).catch(error => log("warn", "collaboration_client_unavailable", { message: error.message }));
    if (selfTestPrompt) {
      await runSelfTest({ transport, identity, stateDir, workspace, prompt: selfTestPrompt, log })
        .catch((error) => log("error", "self_test_failed", { message: error?.message ?? String(error) }));
    }
    if (businessEnabled) {
      try {
        const started = await startBusiness({ transport, identity, workerConfig, config, stateDir,
          workspace, log });
        business = started.business;
        routerClient = started.routerClient;
        routerRuntime = started.routerRuntime;
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

async function startBusiness({ transport, identity, workerConfig, config, stateDir, workspace, log }) {
  const host = stringArg(config.host) ?? stringArg(workerConfig.host) ?? "127.0.0.1";
  const port = Number(config.port ?? identity.port ?? workerConfig.port ?? 8906);
  if (!await portFree(port, host)) {
    log("warn", "business_port_busy", { host, port,
      reason: "Business port is already in use; check the worker instance owning this address." });
    return { business: null, routerClient: null };
  }

  const tokenFile = path.resolve(expandHome(
    process.env.DSHWORKER_TOKEN_FILE ?? workerConfig.tokenFile
      ?? path.join(os.homedir(), ".hermes", "secrets", "router.token")));
  const token = (() => {
    try { return fs.readFileSync(tokenFile, "utf8").trim().replace(/^Bearer\s+/i, ""); }
    catch { return ""; }
  })();
  const feishu = resolveFeishuConfig(workerConfig);
  if (!feishu.value) log("warn", "feishu_config_missing", { file: feishu.file });
  const whitelist = resolveWhitelist(workerConfig, feishu.value);

  // 任务看板目标：训练员与这个 worker 的私聊。显式 taskBoardOpenId 优先，其次
  // 入站白名单（就是训练员本人），最后才是凭据文件里的 open_id。
  const taskBoardOpenId = stringArg(workerConfig.feishu?.taskBoardOpenId) ?? whitelist.ids[0]
    ?? stringArg(feishu.value?.open_id) ?? null;

  const thread = transport.threads.values().next().value
    ?? transport.threads.get(transport.createThread({ title: `${identity.display} · DSH`, cwd: workspace }).threadId);
  // Resolve the existing agent's scoped services before advertising tools. This
  // resumes context only; it does not submit a prompt or start a model turn.
  if (thread.sessionId) await transport.ensureAgent(thread).catch(error =>
    log("warn", "primary_resume_pending", { threadId: thread.id, message: error.message }));
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
    nativeBusyDeliveryVerified: true,
    noProgressTimeoutMs: transport.noProgressTimeoutMs,
    feishu: {
      configFile: feishu.file,
      inboundEnabled: true,
      trainerOpenIdWhitelist: whitelist.ids,
      taskBoardOpenId,
      taskBoardEnabled: workerConfig.feishu?.taskBoardEnabled !== false,
      onlineNotificationEnabled: workerConfig.feishu?.onlineNotificationEnabled ?? false,
      streamEnabled: workerConfig.feishu?.streamEnabled ?? true,
      transcriptEnabled: workerConfig.feishu?.transcriptEnabled ?? true,
      toolProgressEnabled: workerConfig.feishu?.toolProgressEnabled ?? true,
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
  log("info", "business_host_context", { pid: process.pid, pluginRoot: ROOT,
    lifecycleChild: process.env.RUDOLF_BRIDGE_LIFECYCLE_CHILD ?? null,
    lifecycleReady: process.env.RUDOLF_BRIDGE_LIFECYCLE_READY ?? null });
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
  // 与公共 manager.register 同一条路径（409 adopt_conflict 视为已登记）。
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

  const routerRuntime = await startDshRouterRuntime({ manager: business.manager(), transport,
    workerConfig, pluginConfig: config, identity, host, port, tokenFile, log, version: VERSION });
  return { business, routerClient: routerRuntime.client, routerRuntime };
}
