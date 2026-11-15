import { startWorkerMessaging } from "./worker-messages.mjs";
import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import RouterClient, { RouterError, getActiveRouterClient, setActiveRouterClient } from "./router-client.mjs";
import { createApprovalChannel } from "./approval-channel.mjs";
import { FeishuNotifier } from "./feishu.mjs";
import { PROTOCOL } from "./desktop-manager.mjs";
import { waitForTaskClarification } from "./router-question-wait.mjs";
import { ProgressWatch, noProgressTimeoutMs, taskDeadline } from "./progress-watch.mjs";
import { TERMINAL_TURN, findRouterTurn, lastAssistantText, normalizeSubscriptionConfig,
  routerMarker, routerMessageId, routerPrompt, subscriptionMarker, subscriptionMessageId,
  subscriptionNoticeText, taskBoardTerminalPhase, taskBoardText, taskBoardTitle } from "./router-tasks.mjs";

const owners = new WeakMap(), approvalOwners = new WeakMap();
const expandHome = value => value === "~" ? os.homedir()
  : value.startsWith("~/") ? path.join(os.homedir(), value.slice(2)) : value;
const inactive = () => ({ client: null, busy: () => false, close: async () => {} });

// One service per actual writer, never an inventory MCP/proxy or session host.
// nativeCall is accepted for the host contract; reads deliberately use the
// manager's hydrated history, not caller-turn-scoped native wait_threads.
export async function startRouterRuntime({ manager, nativeCall, transport = {}, config = manager?.config ?? {},
  identity = {}, log = () => {}, version = null } = {}) {
  if (!manager || manager.proxy || manager.closed || !manager.server?.listening) return inactive();
  if (owners.has(manager)) return owners.get(manager);
  const starting = start();
  owners.set(manager, starting);
  try { const service = await starting; if (!service.client) owners.delete(manager); return service; }
  catch (error) { if (owners.get(manager) === starting) owners.delete(manager); throw error; }

  async function start() {
    config = { ...manager.config, ...config, router: { ...manager.config?.router, ...config.router },
      feishu: { ...manager.config?.feishu, ...config.feishu } };
    const settings = config.router;
    if (process.env.CODEXWORKER_ROUTER === "0" || settings.enabled === false) return inactive();
    const tokenFile = process.env.CODEXWORKER_ROUTER_TOKEN_FILE ?? settings.tokenFile;
    let token;
    try { token = tokenFile ? (await readFile(expandHome(String(tokenFile)), "utf8")).trim() : config.token; }
    catch (error) {
      // Optional router credentials must not close the shared Feishu writer.
      log("warn", "router_credentials_unavailable", { worker: identity.name ?? settings.name ?? config.machine,
        code: error.code ?? "read_failed", message: "Router credentials could not be read; check the configured token file." });
      return inactive();
    }
    const name = identity.name ?? settings.name ?? config.machine;
    const display = identity.display ?? config.bridgeName ?? name;
    const abort = new AbortController(), signal = abort.signal;
    const stopped = new Error("router_runtime_closed");
    let closed = false, closing, pending = 0, chain = Promise.resolve(), fallbackNotifier, activeTask;
    const receivedTasks = new Set(), cancelledTasks = new Set(), cancellations = new Map();
    const check = () => { if (signal.aborted) throw stopped; };
    const sleep = ms => delay(ms, undefined, { signal });
    // Native writes cannot be canceled safely. Stop observing them on shutdown;
    // their manager-owned receipts remain authoritative, including unknowns.
    function observe(operation) {
      check();
      return new Promise((resolve, reject) => {
        const cancel = () => reject(stopped);
        signal.addEventListener("abort", cancel, { once: true });
        Promise.resolve().then(() => { check(); return operation(); }).then(resolve, reject)
          .finally(() => signal.removeEventListener("abort", cancel));
      });
    }

    // The base client has bounded requests but no lifetime signal. Scope its
    // HTTP requests and reconnect sleeps to this generation, including approval
    // polls. Closing the SSE stream removes realtime subscriptions on the router.
    class LifetimeClient extends RouterClient {
      async _request(method, endpoint, body = null, { timeoutMs = this.requestTimeoutMs } = {}) {
        check();
        const headers = this.headers();
        if (body !== null) headers["content-type"] = "application/json";
        const response = await fetch(this.url + endpoint, { method, headers,
          ...(body !== null ? { body: JSON.stringify(body) } : {}),
          signal: AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) });
        const raw = await response.text();
        let value = {};
        try { value = raw ? JSON.parse(raw) : {}; } catch { value = { raw }; }
        if (!response.ok) throw new RouterError(`router ${method} ${endpoint} -> ${response.status}`,
          { status: response.status, code: value.error, body: value });
        check();
        return value;
      }
      async _wait(ms) { await sleep(ms).catch(() => {}); }
      async _consumeStream() { if (!closed && this.running) return super._consumeStream(); }
      _dispatchSubscriptionEvent(event) {
        // Our queue counts every event immediately; the base client's extra
        // promise queue would hide waiting events from the handoff busy check.
        this.subscriptionChain = this.handlers.onSubscriptionEvent(event);
        return this.subscriptionChain;
      }
      async unregisterSubscriptions() {
        if (closed) { this.subscriptions = []; return; }
        return super.unregisterSubscriptions();
      }
    }
    const client = new LifetimeClient({ url: process.env.CODEXWORKER_ROUTER_URL ?? settings.url ?? "http://127.0.0.1:8790",
      token, name, version, heartbeatMs: settings.heartbeatMs ?? 5000,
      controlSse: typeof manager.cancelRouterTask === "function",
      activityReader: () => {
        const session = manager.state.sessions[activeTask?.threadId ?? manager.state.selectedThreadId];
        const active = session?.activeTurnId;
        const initiator = session?.turnInitiators?.[active];
        const progressAt = manager.taskProgress?.get(session?.threadId)?.lastProgressAt;
        const routerTurn = activeTask && (activeTask.turnId === active ||
          session?.turnMessages?.[active]?.includes(activeTask.messageId));
        if (active && !routerTurn) {
          return { state: "running", taskId: initiator?.kind === "hermes" ? initiator.taskId : null,
            turnSource: initiator?.kind === "hermes" && initiator.taskId ? "router" : "trainer",
            ...(progressAt != null ? { progressAt: progressAt / 1000 } : {}) };
        }
        return { ...client.activity, turnSource: activeTask ? "router" : null,
          ...(activeTask && progressAt != null ? { progressAt: progressAt / 1000 } : {}) };
      }, subscriptions: normalizeSubscriptionConfig(config.subscriptions),
      subscriptionPollMs: config.subscriptionPollMs ?? settings.subscriptionPollMs,
      logger: (level, event, fields) => log(level, event, { component: "router", ...fields }) });
    const dryRun = () => process.env.CODEXWORKER_FEISHU_DRY_RUN === "1";
    const openId = config.feishu.taskBoardOpenId || config.feishu.trainerOpenIdWhitelist?.[0] || "";
    const boardEnabled = config.feishu.taskBoardEnabled !== false && Boolean(openId);
    function notifier() {
      if (manager.notifier?.enabled) return manager.notifier;
      fallbackNotifier ??= new FeishuNotifier({ ...config.feishu, bridgeName: display });
      return fallbackNotifier.enabled ? fallbackNotifier : null;
    }
    async function board({ taskId, phase, source, kind, id, goal, summary, failure, text, threadId }) {
      check();
      const subscription = source != null;
      const body = subscription ? subscriptionNoticeText({ source, text }) : taskBoardText({ phase, taskId, goal, summary, failure });
      const title = subscription ? `${display} · 订阅事件` : taskBoardTitle({ phase, taskId }) ?? `${display} · 任务看板`;
      try {
        if (dryRun()) {
          log("info", subscription ? "subscription_notify_dry_run" : "task_board_dry_run", { taskId, phase, source, kind, id,
            title, text: body.slice(0, 200), textChars: body.length,
            targetOpenIdSha256: createHash("sha256").update(String(openId)).digest("hex").slice(0, 12) });
        } else if (!subscription && phase !== "start" && manager.transcriptEnabled) {
          await observe(() => manager.publicReply({ messageId: routerMessageId(taskId), source: "bridge",
            targetThreadId: threadId }, body, "task-" + phase));
        } else if (boardEnabled && notifier()) {
          await observe(() => notifier().sendMessage(body, { openId, receiveIdType: "open_id", title }));
        }
      } catch (error) {
        check();
        log("warn", "router_board_failed", { taskId, source, message: error.message });
      }
    }
    async function receipt(messageId) {
      return manager.state.receipts?.[messageId] ?? await observe(() => manager.receiptArchive?.get(messageId));
    }
    async function target(messageId) {
      const previous = await receipt(messageId);
      // A reconnect after /switch must follow the original receipt's thread.
      const threadId = previous?.request?.targetThreadId ?? previous?.nativeTargetThreadId
        ?? previous?.nativeDispatch?.threadId ?? manager.state.selectedThreadId;
      if (!threadId || !manager.state.sessions[threadId]) throw new Error("manager has no selected session");
      return { threadId, previous };
    }
    async function read(threadId) {
      return observe(() => manager.read(threadId, manager.state.sessions[threadId]?.hostId,
        { turnLimit: 30, requireCompleteInputs: false }));
    }
    async function waitTurn({ threadId, messageId, marker, deadline, initialTurnId = null }) {
      let turnId = initialTurnId, deadlineNotified = false, lastReadError;
      const watch = new ProgressWatch({ deadline, timeoutMs: noProgressTimeoutMs(config) });
      for (;;) {
        check();
        if (activeTask && cancelledTasks.has(activeTask.taskId)) return { status: "cancelled" };
        const saved = await receipt(messageId);
        const nativeProgress = manager.taskProgress?.get(threadId)?.lastProgressAt;
        if (nativeProgress > watch.lastProgressAt) watch.lastProgressAt = nativeProgress;
        if (saved?.error && !["accepted", "unknown"].includes(saved.nativeDispatch?.status)) {
          log("warn", "router_task_input_rejected", { threadId, messageId,
            code: saved.error.error, detail: saved.error.detail });
          throw new Error("客户端暂未接收任务，请保持客户端打开并检查原会话的连接后重试当前工单。");
        }
        const session = manager.state.sessions[threadId];
        if (session?.queue?.some(request => request.messageId === messageId)) {
          watch.touch(); await sleep(500); continue;
        }
        let snapshot;
        try { snapshot = await read(threadId); }
        catch (error) {
          if (!error.body?.retryable && !error.nativeReachable &&
              !["EPIPE", "ECONNREFUSED", "ETIMEDOUT"].includes(error.code)) throw error;
          const code = error.body?.error ?? error.code;
          if (lastReadError !== code) log("warn", "router_task_read_pending", { threadId, turnId, code,
            message: "客户端读回暂不可用，保留原任务等待连接恢复" });
          lastReadError = code;
          const failure = watch.failure();
          if (failure) return { status: "unknown", timedOut: true, ...failure };
          await sleep(Math.max(1, Math.min(1000, watch.remainingMs())));
          continue;
        }
        lastReadError = null;
        const expected = turnId ?? saved?.nativeTurnId;
        const found = expected ? snapshot.turns?.find(turn => turn.id === expected) : findRouterTurn(snapshot, marker);
        if (found) {
          turnId = found.id;
          if (activeTask?.messageId === messageId) activeTask.turnId = turnId;
          if (activeTask?.messageId === messageId &&
              (saved?.nativeTurnId !== turnId || saved?.nativeDispatch?.status !== "accepted"))
            await observe(() => manager.confirmRouterTaskTurn?.(activeTask.taskId, threadId, found));
          if (TERMINAL_TURN.has(found.status)) {
            await observe(() => manager.captureTaskQuestions(threadId, found));
            return { turn: found, status: found.status };
          }
        }
        watch.observe(found ?? snapshot.turns);
        if (found?.waitingForInput) watch.touch();
        if (!deadlineNotified && Date.now() >= deadline) {
          deadlineNotified = true;
          log("warn", "router_task_deadline_notice", { threadId, turnId, deadline,
            message: "预计完成时间已到，继续等待任务进展" });
        }
        const failure = watch.failure();
        if (failure) return { turn: found, status: found?.status, timedOut: true, ...failure };
        await sleep(Math.max(1, Math.min(500, watch.remainingMs())));
      }
    }
    async function deliver({ messageId, threadId, previous, text, origin }) {
      const request = previous?.request ? structuredClone(previous.request) : {
        protocolVersion: PROTOCOL, messageId, source: "hermes", origin,
        text, targetThreadId: threadId, delivery: origin === "router" ? "queue" : "steer" };
      if (!previous && !dryRun()) await observe(() => manager.mirrorInput(request));
      // Always let the manager resolve durable receipt semantics; never retry a
      // native write directly, including accepted/queued/unknown dispatches.
      return observe(() => manager.message(request));
    }
    async function runTask(task) {
      const taskId = String(task?.task_id ?? "").trim();
      if (!taskId) { log("warn", "router_task_without_id"); return; }
      const messageId = routerMessageId(taskId), marker = routerMarker(taskId);
      const goal = String(task.goal ?? "").trim(), deadline = taskDeadline(task);
      let threadId = null, turnId = null, status = "failed", summary = "", failure = "", finishedAt;
      try {
        if (manager.state.routerCancellations?.[taskId]) { await cancelTask(taskId); return; }
        transport.taskStarted?.(task, deadline);
        client.setActivity({ state: "running", taskId });
        try {
          if (!goal) throw new Error("router task has an empty goal");
          const destination = await target(messageId); threadId = destination.threadId;
          activeTask = { taskId, threadId, messageId, turnId: null };
          if (cancelledTasks.has(taskId)) return;
          if (!destination.previous && (!manager.transcriptEnabled || dryRun())) await board({ taskId, phase: "start", goal, threadId });
          const cwd = manager.state.sessions[threadId].cwd;
          if (task.workspace && cwd && path.resolve(expandHome(String(task.workspace))) !== path.resolve(cwd))
            log("warn", "router_task_workspace_ignored", { taskId, threadId, sessionCwd: cwd });
          try {
            await deliver({ ...destination, messageId, origin: "router", text: routerPrompt({ taskId, goal, context: task.context }) });
          } catch (error) {
            const saved = await receipt(messageId);
            if (!["accepted", "unknown"].includes(saved?.nativeDispatch?.status)) {
              throw new Error("客户端尚未确认接收工单，请检查原会话的连接状态后重试当前工单。");
            }
            log("warn", "router_task_dispatch_unconfirmed", { taskId, threadId,
              message: "原生投递结果正在核对，保留原消息回执继续等待" });
          }
          let waited = await waitTurn({ threadId, marker, messageId, deadline });
          if (cancelledTasks.has(taskId)) return;
          const consumed = new Set();
          while (waited.status === "completed") {
            const answer = await observe(() => waitForTaskClarification({ manager, router: client, taskId, threadId, consumed,
              deadline, log, sleep }));
            if (!answer) break;
            consumed.add(answer.questionId);
            waited = await waitTurn({ threadId, marker, messageId: answer.messageId, initialTurnId: answer.turnId, deadline });
          }
          turnId = waited.turn?.id ?? null;
          let text = lastAssistantText(waited.turn);
          if (!text && turnId) text = lastAssistantText((await read(threadId)).turns?.find(turn => turn.id === turnId));
          status = waited.status === "completed" && !waited.timedOut ? "done" : "failed";
          failure = waited.timedOut ? waited.message : (typeof waited.turn?.error === "string" ? waited.turn.error
            : waited.turn?.error?.message) || `turn ${waited.status ?? "unknown"}`;
          summary = status === "done" ? text || "(completed; no assistant message)" : [failure, text].filter(Boolean).join("\n\n");
        } catch (error) {
          if (closed) return;
          failure = error.message; summary = failure;
          log("error", "router_task_failed", { taskId, threadId, message: failure });
        }
        try {
          check();
          const phase = taskBoardTerminalPhase(status);
          if (phase) await board({ taskId, phase, summary, failure, threadId });
          const result = await observe(() => client.result({ taskId, status, summary: summary.slice(0, 20_000), artifacts: [],
            evidence: [{ type: "codex_thread", thread_id: threadId, turn_id: turnId }] }));
          finishedAt = result.finished_at;
          log("info", "router_result_sent", { taskId, status, threadId, turnId });
        } finally {
          activeTask = null;
          client.setActivity({ state: "idle", taskId: null, progressAt: finishedAt ?? Date.now() / 1000 });
          await observe(() => client.heartbeat());
        }
      } finally {
        if (activeTask?.taskId === taskId) activeTask = null;
        transport.taskFinished?.(taskId);
      }
    }
    async function runSubscription(event) {
      const source = String(event?.source ?? "").trim() || "(unknown)", kind = String(event?.kind ?? "event"), id = event?.id ?? null;
      const text = String(event?.text ?? ""), messageId = subscriptionMessageId({ source, id });
      const marker = subscriptionMarker({ source, kind, id });
      const limit = Number(config.subscriptionTurnLimitMs);
      const deadline = Number.isFinite(limit) && limit > 0 ? Date.now() + limit : Infinity;
      const destination = await target(messageId);
      if (!destination.previous && (!manager.transcriptEnabled || dryRun())) await board({ source, kind, id, text });
      await deliver({ ...destination, messageId, origin: "subscription", text: marker + "\n" + text });
      const waited = await waitTurn({ ...destination, marker, messageId, deadline });
      log(waited.status === "completed" && !waited.timedOut ? "info" : "warn", "subscription_event_finished",
        { source, kind, id, threadId: destination.threadId, turnId: waited.turn?.id,
          status: waited.timedOut ? "failed" : waited.status, reason: waited.message });
    }
    // Count queued work synchronously, before yielding to the SSE reader. Both
    // origins share one chain so task approval attribution cannot interleave.
    function enqueue(run, value) {
      if (closed) return Promise.resolve();
      if (run === runTask) receivedTasks.add(value.task_id);
      pending++;
      chain = chain.then(() => { check(); return run(value); }).catch(error => {
        if (!closed) log("error", "router_handler_failed", { worker: name, taskId: value?.task_id ?? null,
          source: value?.source ?? null, message: error.message });
      }).finally(() => { pending--; client.setActivity({ state: "idle", taskId: null }); });
      return chain;
    }

    async function cancelTask(taskId) {
      if (!/^t-[a-zA-Z0-9_-]+$/.test(taskId ?? "")) return;
      if (cancellations.has(taskId)) return cancellations.get(taskId);
      cancelledTasks.add(taskId);
      const action = (async () => {
        let outcome;
        try {
          const saved = await receipt(routerMessageId(taskId));
          outcome = await observe(() => manager.cancelRouterTask(taskId,
            { notDispatched: !saved && receivedTasks.has(taskId) }));
        } catch (error) {
          outcome = { ok: false, task_id: taskId, state: "cancelling", error: error.body?.error ?? "worker_cancel_failed" };
        }
        log(outcome.state === "cancelled" ? "info" : "warn", "router_cancel_outcome", { taskId, ...outcome });
        await client._request("POST", `/tasks/${encodeURIComponent(taskId)}/cancel-ack`, { name, outcome });
      })().finally(() => cancellations.delete(taskId));
      cancellations.set(taskId, action);
      return action;
    }
    async function questionRequest(payload) {
      if (typeof payload?.request_id !== "string" || typeof payload.question_id !== "string") return;
      let status = 200, body;
      try {
        if (payload.operation === "get") body = manager.questions.get(payload.question_id);
        else if (payload.operation === "answer") body = await manager.questions.acceptAgentAnswer(payload.question_id, payload.input);
        else throw Object.assign(new Error("Unknown question operation."), { status: 400, body: { error: "invalid_question_operation" } });
      } catch (error) {
        status = error.status ?? 503;
        body = error.body ?? { error: "question_worker_unavailable", detail: "Question state could not be read or answered." };
      }
      await client._request("POST", "/questions/responses", { request_id: payload.request_id, worker: name, status, body });
    }
    transport.bindRouter?.(client, `http://${config.host ?? "127.0.0.1"}:${config.port}`);
    let approvalChannel, approvalHandler;
    if (typeof transport.setApprovalHandler === "function") {
      approvalChannel = createApprovalChannel({ router: client, taskId: () => client.activity.taskId, sleepImpl: sleep,
        log: (level, event, fields) => log(level, event, { component: "approval", ...fields }) });
      approvalHandler = async request => {
        try { return await observe(() => approvalChannel.handler(request)); }
        catch (error) { return { decision: "denied", reason: closed ? "router runtime closed" : error.message, approvalId: null }; }
      };
      transport.setApprovalHandler(approvalHandler);
      approvalOwners.set(transport, approvalHandler);
    }
    const messages = startWorkerMessaging({ manager, client, worker: name, log });
    manager.workerMessages = messages;
    const pollMessages = () => { if (!closed && client.connected) void messages.poll().catch(error => {
      if (!closed) log("warn", "worker_message_poll_pending", { worker: name, code: error.code ?? "pending",
        message: "Worker inbox polling is pending; check the router connection." });
    }); };
    const peerPollTimer = setInterval(pollMessages, 5000); peerPollTimer.unref();
    const service = { client, messages, busy: () => pending > 0 || messages.busy() || Boolean(approvalChannel?.active()), close() {
      if (closing) return closing;
      closed = true; abort.abort(stopped); clearInterval(peerPollTimer);
      if (manager.workerMessages === messages) manager.workerMessages = null;
      if (manager.routerClient === client) manager.routerClient = null;
      if (getActiveRouterClient() === client) setActiveRouterClient(null);
      if (approvalHandler && approvalOwners.get(transport) === approvalHandler) {
        transport.setApprovalHandler(null); approvalOwners.delete(transport);
      }
      closing = (async () => {
        await messages.close();
        await client.stop({ timeoutMs: 50 });
        await chain;
        if (owners.get(manager) === starting) owners.delete(manager);
      })();
      return closing;
    } };
    manager.routerClient = client; // QuestionRelay.router() reads this instance.
    setActiveRouterClient(client);
    client.start({ onTask: task => enqueue(runTask, task), onSubscriptionEvent: event => enqueue(runSubscription, event),
      onEvent: (event, payload) => {
        if (event === "worker_command") void (async () => {
          let status = 200, body;
          try { body = await manager.modelCommands.run(payload.command); }
          catch (error) { status = error.status ?? 503; body = error.body ?? { error: "worker_command_failed", detail: error.message }; }
          await client._request("POST", "/commands/responses", { worker: name,
            request_id: payload.request_id, status, body });
        })().catch(error => { if (!closed) log("warn", "worker_command_response_pending", { code: error.code ?? "transport_error" }); });
        if (event === "question_request") void questionRequest(payload).catch(error => {
          if (!closed) log("warn", "question_response_pending", { questionId: payload?.question_id, code: error.code ?? "transport_error" });
        });
        if (event === "task_cancel") void cancelTask(payload?.task_id).catch(error => {
          if (!closed) log("warn", "router_cancel_ack_pending", { taskId: payload?.task_id, message: error.message });
        });
        if (event === "worker_message") void messages.receive(payload).catch(error => {
        if (!closed) log("warn", "worker_message_pending", { code: error.code ?? "pending", messageId: payload?.message_id });
      }); },
      onConnect: info => { log("info", "router_connected", info); pollMessages(); }, onDisconnect: info => log("warn", "router_disconnected", info) });
    return service;
  }
}
