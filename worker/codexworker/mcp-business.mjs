import { randomUUID } from "node:crypto";
import { mkdir, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { DesktopManager, PROTOCOL } from "./desktop-manager.mjs";
import { nativeToolInvocation, nativeToolRejection, nativeStatusInvocation } from "./codex/native-dispatch.mjs";
import { sendImage } from "./feishu-image-output.mjs";
import { operatingConfig } from "./integration-profile.mjs";
import { resolveNativeTransport } from "./codex/transport/index.mjs";

export const BUSINESS_API_VERSION = 1;
export const BUSINESS_STATE_VERSION = 1;
export const BUSINESS_VERSION = "0.2.38";
export function createBusiness(runtimeControl) {
let transition = Boolean(runtimeControl.transition);

const SERVER_NAME = "codexworker";
const SERVER_VERSION = "0.2.38";
const PIPE_ENV = "CODEX_APP_TOOLS_PIPE_PATH";
const CODEX_TURN_METADATA_KEY = "x-codex-turn-metadata";
const LOCAL_MANAGER_HOST = "127.0.0.1";
const LOCAL_MANAGER_PORT = 8903;
const NATIVE_CONTRACT_TOOLS = new Set([
  "create_thread",
  "fork_thread",
  "list_threads",
  "navigate_to_codex_page",
  "read_thread",
  "send_message_to_thread",
  "set_thread_title",
  "wait_threads",
]);
const BINDING_STATE_DIR = path.join(
  process.env.USERPROFILE ?? process.env.HOME ?? process.cwd(),
  ".codex",
  "state",
  "codexworker",
);
const BINDING_STATE_PATH = path.join(BINDING_STATE_DIR, "manager-binding.json");

let desktopManager = null;
let startingManager = null;

async function handleRequest(method, params) {
  if (desktopManager?.handoffPaused && method === "tools/call" &&
      !["bridge_binding_status", "bridge_reload_business_modules"].includes(params.name)) {
    return toolResult({ error: "bridge_reloading", detail: "Business handoff in progress; no native operation was dispatched. Retry with the same messageId.", retryable: true }, true);
  }
  switch (method) {
    case "initialize":
      if (params.__bridgeLifecycleReady !== false) void ensureLocalManagerServer().catch(() => process.stderr.write("[codexworker] HTTP adapter awaits a valid local binding/config/token.\n"));
      return {
        protocolVersion: params.protocolVersion ?? "2025-06-18",
        capabilities: { tools: { listChanged: true } },
        serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
        instructions: "Probe and operate the CodexWorker desktop-native adapter. The trainer authorized automatic Feishu relay of public displayed output images in its selected chat; do not separately bridge_send_image those same previews. Arbitrary files still need explicit send authorization. In a Bridge-connected selected chat, publish every trainer clarification through bridge_publish_question (complete public title/options). Do not additionally open request_user_input_async or another native panel for the same clarification: external replies continue this chat but cannot fill that separate panel. Never publish private reasoning, arbitrary tool arguments or secrets.",
      };
    case "ping":
      return {};
    case "tools/list":
      return {
        tools: [
          {
            name: "bridge_probe",
            description: "Verify that this plugin can reach Codex Desktop native task tools without revealing the pipe path.",
            inputSchema: {
              type: "object",
              properties: {},
              additionalProperties: false,
            },
            annotations: { readOnlyHint: true },
          },
          {
            name: "bridge_inspect_native_contract",
            description: "Show the input contracts for the Codex Desktop native task tools required by the Hermes Bridge. This is read-only.",
            inputSchema: {
              type: "object",
              properties: {},
              additionalProperties: false,
            },
            annotations: { readOnlyHint: true },
          },
          {
            name: "bridge_bind_manager",
            description: "Bind this Codex Desktop task as the local Hermes manager context and verify a read-only native list_threads call. It does not create, edit, or send any task.",
            inputSchema: {
              type: "object",
              properties: {},
              additionalProperties: false,
            },
            annotations: { readOnlyHint: true },
          },
          {
            name: "bridge_binding_status",
            description: "Read the machine-local Hermes manager binding status without contacting Codex Desktop.",
            inputSchema: {
              type: "object",
              properties: {},
              additionalProperties: false,
            },
            annotations: { readOnlyHint: true },
          },
          {
            name: "bridge_verify_manager_binding",
            description: "Verify that the persisted Hermes manager binding can still perform a read-only Codex Desktop list_threads call after the original binding turn has completed.",
            inputSchema: {
              type: "object",
              properties: {},
              additionalProperties: false,
            },
            annotations: { readOnlyHint: true },
          },
          {
            name: "bridge_start_local_manager",
            description: "Start the authenticated desktop-native DSH-compatible Bridge adapter. It never starts a separate Codex session runtime. Full manager readiness remains false until native control acceptance passes.",
            inputSchema: {
              type: "object",
              properties: {},
              additionalProperties: false,
            },
          },
          {
            name: "bridge_register_current_task",
            description: "Register the current native Codex chat, or an explicitly authorized thread, for Hermes Bridge message delivery and public transcript synchronization. Does not fork or send a prompt.",
            inputSchema: { type: "object", properties: { threadId: { type: "string" }, hostId: { type: "string" } }, additionalProperties: false },
          },
          {
            name: "bridge_publish_question",
            description: "Publish an authorized public clarification and its options to Feishu and the existing Hermes report service, with per-channel delivery status. Does not send a prompt or start a native task. Use for Bridge-connected selected chats instead of a Codex-only question panel; external plain-text replies continue the chat.",
            inputSchema: { type: "object", properties: { questionId: { type: "string" }, threadId: { type: "string" },
              sourceTurnId: { type: "string" }, sourceQuestionItemIds: { type: "array", items: { type: "string" } },
              questions: { type: "array", minItems: 1, maxItems: 3, items: { type: "object", properties: {
                title: { type: "string", minLength: 1 }, options: { type: "array", minItems: 2, maxItems: 6, items: { type: "string" } }
              }, required: ["title"], additionalProperties: false } } }, required: ["questions"], additionalProperties: false },
          },
          {
            name: "bridge_message",
            description: "Send an authorized message or /new /clear /resume /fork /list /switch /status /stop through the desktop-native Bridge. Never use to message another chat without the trainer's authorization. Native stop and exact steer may be unavailable; queue is supported.",
            inputSchema: { type: "object", properties: { text: { type: "string", minLength: 1 }, threadId: { type: "string" }, messageId: { type: "string" }, delivery: { type: "string", enum: ["queue", "steer"], default: "steer" } }, required: ["text"], additionalProperties: false },
          },
          {
            name: "bridge_send_image",
            description: "Send a trainer-authorized local raster image to the current Feishu-selected chat. Uploads a real image, returns a durable receipt, never sends a native prompt. Reuse messageId on retry; unknown outcomes are not resent.",
            inputSchema: { type: "object", properties: { imagePath: { type: "string", minLength: 1 }, threadId: { type: "string" }, messageId: { type: "string", format: "uuid" } }, required: ["imagePath"], additionalProperties: false },
          },
          {
            name: "bridge_reload_business_modules",
            description: "Request an atomic reload of all business code from trusted local source. Busy writes defer the handoff; invalid code retains the old generation. Native tasks and durable receipts/selection are preserved. Transport connections may reconnect; bootstrap/loader stay fixed. Inspect /bridge/runtime for the applied generation.",
            inputSchema: { type: "object", properties: {}, additionalProperties: false },
          },
          {
            name: "bridge_verify_native_write_path",
            description: "Create one visible Codex Desktop task, wait for it to become idle, send a unique follow-up through the native desktop tool, and read the task back. This is a deliberate write-path acceptance test.",
            inputSchema: {
              type: "object",
              properties: {
                projectId: {
                  type: "string",
                  minLength: 1,
                  description: "Codex Desktop project id in which the visible acceptance task will be created.",
                },
              },
              required: ["projectId"],
              additionalProperties: false,
            },
          },
        ],
      };
    case "tools/call":
      switch (params.name) {
        case "bridge_probe":
          return probeDesktopPipe();
        case "bridge_inspect_native_contract":
          return inspectNativeContract();
        case "bridge_bind_manager":
          return bindManagerContext(params);
        case "bridge_binding_status":
          return bindingStatus();
        case "bridge_verify_manager_binding":
          return verifyManagerBinding();
        case "bridge_start_local_manager":
          return startLocalManager();
        case "bridge_register_current_task":
          return registerCurrentTask(params);
        case "bridge_message":
          return bridgeMessage(params);
        case "bridge_publish_question":
          return publishQuestion(params);
        case "bridge_send_image":
          return bridgeImage(params);
        case "bridge_reload_business_modules":
          return reloadBusinessModules();
        case "bridge_verify_native_write_path":
          return verifyNativeWritePath(params);
        default:
          throw new Error(`Unknown tool: ${params.name}`);
      }
    default:
      throw new Error(`Unsupported MCP method: ${method}`);
  }
}

async function probeDesktopPipe() {
  if (!desktopPipePath()) {
    return toolResult({
      desktopNative: false,
      pipeInjected: false,
      error: `${PIPE_ENV} was not injected by Codex Desktop`,
    }, true);
  }

  try {
    const tools = await nativeTools();
    return toolResult({
      desktopNative: true,
      pipeInjected: true,
      nativeToolCount: tools.length,
      nativeTools: tools.map((tool) => tool?.name).filter((name) => typeof name === "string").sort(),
    });
  } catch (error) {
    return toolResult({
      desktopNative: false,
      pipeInjected: true,
      error: error instanceof Error ? error.message : String(error),
    }, true);
  }
}

async function inspectNativeContract() {
  if (!desktopPipePath()) {
    return toolResult({
      desktopNative: false,
      pipeInjected: false,
      error: `${PIPE_ENV} was not injected by Codex Desktop`,
    }, true);
  }

  try {
    const tools = await nativeTools();
    const requiredTools = tools
      .filter((tool) => NATIVE_CONTRACT_TOOLS.has(tool?.name))
      .map((tool) => ({
        name: tool.name,
        namespace: tool.namespace,
        inputSchema: tool.inputSchema,
      }));
    const missingTools = [...NATIVE_CONTRACT_TOOLS].filter(
      (name) => !tools.some((tool) => tool?.name === name),
    );
    return toolResult({
      desktopNative: true,
      pipeInjected: true,
      requiredTools,
      missingTools,
    }, missingTools.length > 0);
  } catch (error) {
    return toolResult({
      desktopNative: false,
      pipeInjected: true,
      error: error instanceof Error ? error.message : String(error),
    }, true);
  }
}

async function bindManagerContext(params) {
  const invocation = invocationContext(params);
  if (!invocation.threadId) {
    return toolResult({
      desktopNative: true,
      pipeInjected: Boolean(desktopPipePath()),
      error: "Codex Desktop did not provide the current task metadata; no manager binding was written.",
    }, true);
  }

  try {
    const response = await callNativeTool("list_threads", { limit: 1 }, invocation);
    if (response?.success !== true) {
      return toolResult({
        desktopNative: true,
        pipeInjected: true,
        managerThreadId: invocation.threadId,
        nativeTaskReady: false,
        nativeReadProbeSucceeded: false,
        error: "Codex Desktop rejected the read-only list_threads verification call; no manager binding was written.",
        nativeCall: summarizeNativeCall("list_threads", response),
      }, true);
    }

    const binding = {
      formatVersion: 1,
      integrationMode: "desktop-native",
      managerThreadId: invocation.threadId,
      managerTurnId: invocation.turnId ?? null,
      boundAt: new Date().toISOString(),
    };
    await writeBinding(binding);
    return toolResult({
      desktopNative: true,
      pipeInjected: true,
      nativeTaskReady: false,
      nativeReadProbeSucceeded: true,
      binding,
      nativeCall: summarizeNativeCall("list_threads", response),
    });
  } catch (error) {
    return toolResult({
      desktopNative: true,
      pipeInjected: Boolean(desktopPipePath()),
      managerThreadId: invocation.threadId,
      nativeTaskReady: false,
      error: error instanceof Error ? error.message : String(error),
    }, true);
  }
}

async function bindingStatus() {
  try {
    const binding = await readBinding();
    return toolResult({
      desktopNative: Boolean(desktopPipePath()),
      pipeInjected: Boolean(desktopPipePath()),
      binding,
      stateLocation: "machine-local Codex state",
    });
  } catch (error) {
    if (error?.code === "ENOENT") {
      return toolResult({
        desktopNative: Boolean(desktopPipePath()),
        pipeInjected: Boolean(desktopPipePath()),
        binding: null,
        stateLocation: "machine-local Codex state",
      });
    }
    return toolResult({
      error: error instanceof Error ? error.message : String(error),
    }, true);
  }
}

async function verifyManagerBinding() {
  let binding;
  try {
    binding = await readBinding();
  } catch (error) {
    if (error?.code === "ENOENT") {
      return toolResult({
        desktopNative: Boolean(desktopPipePath()),
        pipeInjected: Boolean(desktopPipePath()),
        nativeTaskReady: false,
        nativeReadProbeSucceeded: false,
        error: "No persisted Hermes manager binding exists. Run bridge_bind_manager first.",
      }, true);
    }
    return toolResult({
      error: error instanceof Error ? error.message : String(error),
    }, true);
  }

  try {
    const response = await callNativeTool("list_threads", { limit: 1 }, {
      threadId: binding.managerThreadId,
      turnId: binding.managerTurnId ?? undefined,
    });
    const success = response?.success === true;
    return toolResult({
      desktopNative: true,
      pipeInjected: true,
      nativeTaskReady: false,
      nativeReadProbeSucceeded: success,
      binding,
      nativeCall: summarizeNativeCall("list_threads", response),
    }, !success);
  } catch (error) {
    return toolResult({
      desktopNative: Boolean(desktopPipePath()),
      pipeInjected: Boolean(desktopPipePath()),
      nativeTaskReady: false,
      nativeReadProbeSucceeded: false,
      binding,
      error: error instanceof Error ? error.message : String(error),
    }, true);
  }
}

async function startLocalManager() {
  let binding;
  try {
    binding = await readBinding();
  } catch (error) {
    return toolResult({
      started: false,
      error: error?.code === "ENOENT"
        ? "No persisted Hermes manager binding exists. Run bridge_bind_manager first."
        : (error instanceof Error ? error.message : String(error)),
    }, true);
  }

  try {
    await ensureLocalManagerServer();
    return toolResult({
      started: true,
      endpoint: `http://${desktopManager.config.host}:${desktopManager.config.port}`,
      loopbackOnly: desktopManager.config.host === "127.0.0.1",
      binding: {
        managerThreadId: binding.managerThreadId,
        boundAt: binding.boundAt,
      },
      managerReady: false,
      health: desktopManager.health(),
      detail: "Authenticated DSH-compatible HTTP adapter is running on the Desktop native task tools. Token stays in the machine-local secret file; full manager acceptance is still pending.",
    });
  } catch (error) {
    return toolResult({
      started: false,
      error: error instanceof Error ? error.message : String(error),
      ...(error.body ?? {}),
    }, true);
  }
}

async function verifyNativeWritePath(params) {
  const projectId = params?.arguments?.projectId?.trim();
  if (!projectId) {
    return toolResult({
      nativeWriteVerified: false,
      error: "projectId is required",
    }, true);
  }

  let binding;
  try {
    binding = await readBinding();
  } catch (error) {
    return toolResult({
      nativeWriteVerified: false,
      error: error?.code === "ENOENT"
        ? "No persisted Hermes manager binding exists. Run bridge_bind_manager first."
        : (error instanceof Error ? error.message : String(error)),
    }, true);
  }

  const invocation = {
    threadId: binding.managerThreadId,
    turnId: binding.managerTurnId ?? undefined,
  };
  const probeId = randomUUID();
  const marker = `HERMES_NATIVE_WRITE_PROBE_${probeId}`;
  let createdThreadId = null;

  try {
    const createResponse = await callNativeTool("create_thread", {
      prompt: "这是 Hermes Bridge 的原生桌面写入验收任务。请只回复 NATIVE_CREATE_OK，不要调用工具，不要修改文件。",
      title: "Hermes Bridge · 原生写入验收",
      target: {
        type: "project",
        projectId,
        environment: { type: "local" },
      },
    }, invocation, 30000);
    const createPayload = nativeJsonPayload(createResponse);
    createdThreadId = typeof createPayload?.threadId === "string" ? createPayload.threadId : null;
    if (createResponse?.success !== true || !createdThreadId) {
      return toolResult({
        nativeWriteVerified: false,
        stage: "create_thread",
        nativeCall: summarizeNativeCall("create_thread", createResponse),
        nativePayload: createPayload,
        error: "Codex Desktop did not return a native threadId.",
      }, true);
    }

    await waitForNativeTask(createdThreadId, invocation);
    const beforeSend = await readNativeTask(createdThreadId, invocation);
    if (beforeSend?.thread?.status?.type !== "idle") {
      return toolResult({
        nativeWriteVerified: false,
        createdThreadId,
        stage: "wait_for_created_task",
        threadStatus: beforeSend?.thread?.status ?? null,
        error: "The created desktop task did not become idle; the follow-up was not sent to avoid steering an active turn.",
      }, true);
    }

    const sendResponse = await callNativeTool("send_message_to_thread", {
      threadId: createdThreadId,
      prompt: `${marker}\n请只回复 NATIVE_SEND_OK，不要调用工具，不要修改文件。`,
    }, invocation, 30000);
    if (sendResponse?.success !== true) {
      return toolResult({
        nativeWriteVerified: false,
        createdThreadId,
        marker,
        stage: "send_message_to_thread",
        nativeCall: summarizeNativeCall("send_message_to_thread", sendResponse),
        error: "Codex Desktop rejected the native follow-up message.",
      }, true);
    }

    await waitForNativeTask(createdThreadId, invocation);
    const afterSend = await readNativeTask(createdThreadId, invocation);
    const transcript = JSON.stringify(afterSend);
    const markerReadBack = transcript.includes(marker);
    const finalIdle = afterSend?.thread?.status?.type === "idle";
    return toolResult({
      nativeWriteVerified: markerReadBack && finalIdle,
      nativeBindingReady: true,
      createdThreadId,
      marker,
      createAccepted: true,
      sendAccepted: true,
      markerReadBack,
      finalThreadStatus: afterSend?.thread?.status ?? null,
      managerReady: false,
      detail: markerReadBack && finalIdle
        ? "Native create, idle follow-up send, and transcript read-back are verified. Manager readiness remains false until interrupt and trainer-ingress semantics are implemented."
        : "The native calls returned, but the marker was not confirmed in an idle desktop transcript.",
    }, !(markerReadBack && finalIdle));
  } catch (error) {
    return toolResult({
      nativeWriteVerified: false,
      createdThreadId,
      marker,
      error: error instanceof Error ? error.message : String(error),
    }, true);
  }
}

async function waitForNativeTask(threadId, invocation) {
  return callNativeTool("wait_threads", {
    targets: [{ threadId }],
    timeoutMs: 120000,
  }, invocation, 135000);
}

async function readNativeTask(threadId, invocation) {
  const response = await callNativeTool("read_thread", {
    threadId,
    turnLimit: 10,
    includeOutputs: true,
    maxOutputCharsPerItem: 4000,
  }, invocation, 30000);
  if (response?.success !== true) {
    throw new Error("Codex Desktop rejected native read_thread during write-path verification");
  }
  const payload = nativeJsonPayload(response);
  if (!payload) throw new Error("Codex Desktop returned no readable task payload");
  return payload;
}

async function ensureLocalManagerServer() {
  if (desktopManager?.server?.listening || desktopManager?.proxy) return;
  const lifecycleUnavailable = process.env.RUDOLF_BRIDGE_LIFECYCLE_READY === "0";
  // A failed cold initialize is a writer-start guard, not a permanent ban on
  // reconnecting to an authenticated, already healthy machine writer later.
  // Never promote a proxy or a contradictory child/ready=0 into a new writer.
  if (lifecycleUnavailable && process.env.RUDOLF_BRIDGE_LIFECYCLE_CHILD === "1") throw new Error("desktop_lifetime_unavailable");
  if (startingManager) return startingManager;
  startingManager = (async () => {
    const { readFile } = await import("node:fs/promises");
    const binding = await readBinding();
    let configured = {};
    try { configured = JSON.parse(await readFile(path.join(BINDING_STATE_DIR, "desktop-config.local.json"), "utf8")); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    let manager, callerSession;
    const native = async (name, args, deliveryContext) => {
      let requestInvocation;
      if (name === "wait_threads") {
        try { requestInvocation = await nativeStatusInvocation(binding, manager, callerSession); }
        catch (error) { error.nativeStatusPreflight = true; throw error; }
      } else requestInvocation = nativeToolInvocation(name, args, binding, deliveryContext);
      const response = await callNativeTool(name, args, requestInvocation, name === "list_threads" ? 60000 : 30000);
      if (response?.success !== true) throw nativeToolRejection(name, response);
      const payload = nativeJsonPayload(response);
      if (!payload) throw new Error(`Desktop returned no JSON for ${name}`);
      return payload;
    };
    const current = await native("read_thread", { threadId: binding.managerThreadId, turnLimit: 1 });
    callerSession = { threadId: binding.managerThreadId, cwd: current.thread?.cwd, hostId: "local" };
    const projects = await native("list_projects", {});
    const project = projects.projects?.find(p => p.path === current.thread?.cwd && p.hostId === "local");
    const tokenFile = configured.tokenFile ?? path.join(process.env.USERPROFILE ?? process.env.HOME, ".codex", "secrets", "codex-bridge-codexworker.token");
    const config = operatingConfig({ host: LOCAL_MANAGER_HOST, port: LOCAL_MANAGER_PORT,
      workspaceRoots: current.thread?.cwd ? [current.thread.cwd] : [], projectId: project?.projectId,
      ...configured, nativeSenderThreadId: binding.managerThreadId, nativeSendFromTarget: true,
      nativeBusyDeliveryVerified: true, nativeStateIndependentDeliveryVerified: true,
      desktopLifetimeStartedAt: process.env.RUDOLF_BRIDGE_DESKTOP_STARTED_AT ?? null,
      token: (await readFile(tokenFile, "utf8")).trim() });
    // Legacy Feishu config files are reusable, but never loaded from synchronized source.
    if (config.feishu?.configFile) {
      const f = JSON.parse(await readFile(config.feishu.configFile, "utf8"));
      config.feishu = { appId: f.app_id, appSecret: f.app_secret, chatId: f.chat_id ?? f.trainer_chat_id,
        trainerOpenIdWhitelist: f.trainer_open_id ? [f.trainer_open_id] : [], ...config.feishu };
    }
    // Only the supervisor-owned child may become an HTTP/Feishu writer. A
    // chat/inventory MCP can otherwise race into the lease gap during handoff.
    if (process.env.RUDOLF_BRIDGE_LIFECYCLE_CHILD !== "1") {
      try { desktopManager = await connectManagerProxy(config, !lifecycleUnavailable); }
      catch (error) {
        if (!lifecycleUnavailable) throw error;
        const failure = new Error("desktop_lifetime_unavailable");
        failure.body = { error: failure.message, detail: "No authenticated healthy Desktop-lifetime writer is available; no alternate writer was started.", retryable: true };
        throw failure;
      }
      return;
    }
    manager = new DesktopManager({ native, listNativeTools: nativeTools, stateDir: BINDING_STATE_DIR,
      config: { ...config, hotReloadTransition: transition } });
    transition = false;
    // All handlers and their imports belong to one validated generation. No
    // independent leaf loader may mix image code from a different generation.
    manager.businessRuntime = { module: true, initialize: async () => runtimeControl.status(),
      status: () => ({ ...runtimeControl.status(), activeImageSends: manager.imageOperations ?? 0,
        handoff: manager.hotHandoffStatus() }),
      reload: () => runtimeControl.reload(), sendImage: async (request, context) => {
        manager.imageOperations = (manager.imageOperations ?? 0) + 1;
        try { return await sendImage(request, context); } finally { manager.imageOperations--; }
      } };
    try { await manager.initialize(); await manager.listen(); desktopManager = manager; }
    catch (error) {
      await manager.close();
      if (error?.body?.error !== "manager_instance_active") throw error;
      // Codex can host several copies of this MCP server in different chats.
      // They share one machine-level writer and never rewrite the registry in parallel.
      desktopManager = await connectManagerProxy(config, false);
    }
  })();
  try { await startingManager; } finally { startingManager = null; }
}

async function connectManagerProxy(config, wait) {
  const endpoint = `http://127.0.0.1:${config.port}`;
  const request = async (route, body, timeoutMs = 35000) => {
    const response = await fetch(endpoint + route, { method: body ? "POST" : "GET",
      headers: { authorization: `Bearer ${config.token}`, "content-type": "application/json" },
      ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(timeoutMs) });
    const result = await response.json();
    if (!response.ok) { const failure = new Error(result.detail); failure.body = result; throw failure; }
    return result;
  };
  const configured = config.managerProxyWaitMs;
  const budget = Number.isFinite(configured) ? Math.max(50, Math.min(configured, 35000)) : 35000;
  const deadline = performance.now() + budget;
  let health;
  do {
    try {
      health = await request("/health", undefined, Math.max(1, Math.ceil(deadline - performance.now())));
      if (health.ready && health.integrationMode === "desktop-native" && health.name === (config.bridgeName ?? "CodexWorker")) break;
      health = null;
    } catch { health = null; }
    if (!wait || performance.now() >= deadline) break;
    await new Promise(resolve => setTimeout(resolve, Math.min(100, Math.max(1, deadline - performance.now()))));
  } while (performance.now() < deadline);
  if (!health) {
    const failure = new Error("desktop_lifetime_writer_unavailable");
    failure.body = { error: failure.message, detail: "The Desktop-lifetime writer is not ready; no alternate writer was started.", retryable: true };
    throw failure;
  }
  // This is connectivity, NOT ownership. No listening-server sentinel and no
  // manager activation/lease acquisition during this MCP's business reload.
  return { config, proxy: true, health: () => health, register: body => request("/manager/sessions/adopt", body),
    message: body => request("/manager/messages", body), sendImage: body => request("/bridge/images", body),
    businessRuntime: { reload: () => request("/bridge/runtime/reload", {}) },
    questions: { publish: body => request("/bridge/questions", body) }, close: async () => {} };
}

async function registerCurrentTask(params) {
  try {
    await ensureLocalManagerServer();
    const invocation = invocationContext(params);
    const args = params.arguments ?? {};
    const threadId = args.threadId ?? invocation.threadId;
    if (!threadId) throw new Error("Current Codex task metadata is missing.");
    const result = await desktopManager.register({ protocolVersion: PROTOCOL, messageId: randomUUID(), source: "trainer", threadId,
      ...(args.hostId ? { hostId: args.hostId } : {}) });
    return toolResult(result);
  } catch (error) { return toolResult(error.body ?? { error: error.message }, true); }
}

async function bridgeMessage(params) {
  try {
    await ensureLocalManagerServer();
    const args = params.arguments ?? {};
    const result = await desktopManager.message({ protocolVersion: PROTOCOL, messageId: args.messageId ?? randomUUID(),
      source: "trainer", text: args.text, delivery: args.delivery ?? "steer", targetThreadId: args.threadId ?? null,
      sourceThreadId: invocationContext(params).threadId ?? null });
    return toolResult(result);
  } catch (error) { return toolResult(error.body ?? { error: error.message }, true); }
}

async function publishQuestion(params) {
  try {
    await ensureLocalManagerServer();
    const args = params.arguments ?? {};
    const threadId = args.threadId ?? invocationContext(params).threadId;
    if (!threadId) throw new Error("Current Codex task metadata is missing.");
    return toolResult(await desktopManager.questions.publish({ threadId, questionId: args.questionId ?? randomUUID(), questions: args.questions,
      ...(args.sourceTurnId ? { sourceTurnId: args.sourceTurnId } : {}), ...(args.sourceQuestionItemIds ? { sourceQuestionItemIds: args.sourceQuestionItemIds } : {}) }));
  } catch (error) { return toolResult(error.body ?? { error: error.message }, true); }
}

async function bridgeImage(params) {
  try {
    await ensureLocalManagerServer();
    const args = params.arguments ?? {};
    const threadId = args.threadId ?? invocationContext(params).threadId;
    if (!threadId) throw new Error("Current Codex task metadata is missing; no image was sent.");
    return toolResult(await desktopManager.sendImage({ ...args, threadId, messageId: args.messageId ?? randomUUID() }));
  } catch (error) { return toolResult(error.body ?? { error: error.message }, true); }
}

async function reloadBusinessModules() {
  try { await ensureLocalManagerServer(); return toolResult(await desktopManager.businessRuntime.reload()); }
  catch (error) { return toolResult(error.body ?? { error: error.message }, true); }
}

function desktopPipePath() {
  return process.env[PIPE_ENV]?.trim() || null;
}

async function nativeTools() {
  return await resolveNativeTransport().listTools();
}

async function callNativeTool(name, arguments_, invocation, timeoutMs = 5000) {
  return await resolveNativeTransport().callTool(name, arguments_, invocation, timeoutMs);
}

function nativeJsonPayload(response) {
  const contentItems = Array.isArray(response?.contentItems) ? response.contentItems : [];
  for (const item of contentItems) {
    if (typeof item?.text !== "string") continue;
    try {
      return JSON.parse(item.text);
    } catch {
      // Native task tools normally return JSON text; ignore non-JSON annotations.
    }
  }
  return null;
}

function summarizeNativeCall(tool, response) {
  const contentItems = Array.isArray(response?.contentItems) ? response.contentItems : [];
  return {
    tool,
    success: response?.success === true,
    contentItemTypes: contentItems.map((item) => item?.type).filter(Boolean),
    contentItemCount: contentItems.length,
  };
}

function invocationContext(params) {
  const metadata = isObject(params?._meta) ? params._meta : {};
  const turnMetadata = parseTurnMetadata(metadata[CODEX_TURN_METADATA_KEY]);
  return {
    threadId: stringMetadata(metadata, [
      "openai/threadId",
      "openai/thread_id",
      "codexThreadId",
      "codex_thread_id",
      "threadId",
      "thread_id",
    ]) ?? turnMetadata?.thread_id ?? nestedMetadataId(metadata, "thread"),
    turnId: stringMetadata(metadata, [
      "openai/turnId",
      "openai/turn_id",
      "codexTurnId",
      "codex_turn_id",
      "turnId",
      "turn_id",
    ]) ?? turnMetadata?.turn_id ?? nestedMetadataId(metadata, "turn"),
    callId: stringMetadata(metadata, [
      "openai/toolCallId",
      "openai/tool_call_id",
      "codexCallId",
      "codex_call_id",
      "callId",
      "call_id",
    ]) ?? nestedMetadataId(metadata, "call"),
  };
}

function stringMetadata(metadata, keys) {
  for (const key of keys) {
    const value = metadata?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function nestedMetadataId(metadata, key) {
  const value = metadata?.[key];
  if (typeof value === "string" && value.trim()) return value.trim();
  if (isObject(value)) {
    for (const candidate of ["id", `${key}Id`, `${key}_id`]) {
      if (typeof value[candidate] === "string" && value[candidate].trim()) {
        return value[candidate].trim();
      }
    }
  }
  return null;
}

function parseTurnMetadata(value) {
  if (isObject(value)) return value;
  if (typeof value !== "string") return null;
  try {
    const parsed = JSON.parse(value);
    return isObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function isObject(value) {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

async function writeBinding(binding) {
  await mkdir(BINDING_STATE_DIR, { recursive: true });
  const temporaryPath = `${BINDING_STATE_PATH}.${process.pid}.${randomUUID()}.tmp`;
  await writeFile(temporaryPath, `${JSON.stringify(binding, null, 2)}\n`, "utf8");
  await rename(temporaryPath, BINDING_STATE_PATH);
}

async function readBinding() {
  const { readFile } = await import("node:fs/promises");
  return JSON.parse(await readFile(BINDING_STATE_PATH, "utf8"));
}

function toolResult(payload, isError = false) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    isError,
    structuredContent: payload,
  };
}


return { handleRequest,
  busy: () => Boolean(startingManager || desktopManager?.requestHotHandoff?.(runtimeControl.status)),
  activate: async () => { await ensureLocalManagerServer(); return desktopManager.health(); },
  quiesce: async () => {
    if (startingManager || desktopManager?.requestHotHandoff?.(runtimeControl.status)) return { busy: true };
    const hadManager = desktopManager instanceof DesktopManager && Boolean(desktopManager.server?.listening);
    transition = true;
    desktopManager?.commitHotHandoff?.();
    await desktopManager?.close(); desktopManager = null;
    return { busy: false, hadManager };
  },
  close: async () => { await startingManager?.catch(() => {}); await desktopManager?.close(); desktopManager = null; }
};
}
