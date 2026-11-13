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
    const token = tokenFile ? (await readFile(expandHome(String(tokenFile)), "utf8")).trim() : config.token;
    const name = identity.name ?? settings.name ?? config.machine;
    const display = identity.display ?? config.bridgeName ?? name;
    const abort = new AbortController(), signal = abort.signal;
    const stopped = new Error("router_runtime_closed");
    let closed = false, closing, pending = 0, chain = Promise.resolve(), fallbackNotifier;
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
      token, name, version, heartbeatMs: settings.heartbeatMs, subscriptions: normalizeSubscriptionConfig(config.subscriptions),
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
      return observe(() => manager.read(threadId, manager.state.sessions[threadId]?.hostId, { turnLimit: 30 }));
    }
    async function waitTurn({ threadId, messageId, marker, deadline, initialTurnId = null }) {
      let turnId = initialTurnId, misses = 0;
      const watch = new ProgressWatch({ deadline, timeoutMs: noProgressTimeoutMs(config) });
      for (;;) {
        check();
        const saved = await receipt(messageId);
        if (saved?.error) throw new Error(`${saved.error.error}: ${saved.error.detail ?? "dispatch rejected"}`);
        const snapshot = await read(threadId);
        const expected = turnId ?? saved?.nativeTurnId;
        const found = expected ? snapshot.turns?.find(turn => turn.id === expected) : findRouterTurn(snapshot, marker);
        if (found) {
          turnId = found.id; misses = 0;
          if (TERMINAL_TURN.has(found.status)) {
            await observe(() => manager.captureTaskQuestions(threadId, found));
            return { turn: found, status: found.status };
          }
        } else if (turnId && ++misses > 20) throw new Error(`turn ${turnId} left the readable window before it completed`);
        watch.observe(found ?? snapshot.turns);
        if (found?.waitingForInput) watch.touch();
        const failure = watch.failure();
        if (failure) return { turn: found, status: found?.status, timedOut: true, ...failure };
        await sleep(Math.max(1, Math.min(500, watch.remainingMs())));
      }
    }
    async function deliver({ messageId, threadId, previous, text, origin }) {
      const request = { protocolVersion: PROTOCOL, messageId, source: "hermes", origin,
        text, targetThreadId: threadId, delivery: "steer" };
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
      let threadId = null, turnId = null, status = "failed", summary = "", failure = "";
      client.setActivity({ state: "running", taskId });
      try {
        if (!goal) throw new Error("router task has an empty goal");
        const destination = await target(messageId); threadId = destination.threadId;
        if (!destination.previous && (!manager.transcriptEnabled || dryRun())) await board({ taskId, phase: "start", goal, threadId });
        const cwd = manager.state.sessions[threadId].cwd;
        if (task.workspace && cwd && path.resolve(expandHome(String(task.workspace))) !== path.resolve(cwd))
          log("warn", "router_task_workspace_ignored", { taskId, threadId, sessionCwd: cwd });
        await deliver({ ...destination, messageId, origin: "router", text: routerPrompt({ taskId, goal, context: task.context }) });
        let waited = await waitTurn({ threadId, marker, messageId, deadline });
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
        failure = error.message; summary = `task runner error: ${failure}`;
        log("error", "router_task_failed", { taskId, threadId, message: failure });
      }
      try {
        check();
        const phase = taskBoardTerminalPhase(status);
        if (phase) await board({ taskId, phase, summary, failure, threadId });
        await observe(() => client.result({ taskId, status, summary: summary.slice(0, 20_000), artifacts: [],
          evidence: [{ type: "codex_thread", thread_id: threadId, turn_id: turnId }] }));
        log("info", "router_result_sent", { taskId, status, threadId, turnId });
      } finally { client.setActivity({ state: "idle", taskId: null }); }
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
      pending++;
      chain = chain.then(() => { check(); return run(value); }).catch(error => {
        if (!closed) log("error", "router_handler_failed", { message: error.message });
      }).finally(() => { pending--; client.setActivity({ state: "idle", taskId: null }); });
      return chain;
    }
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
      if (!closed) log("warn", "worker_message_poll_pending", { code: error.code ?? "pending" });
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
      onEvent: (event, payload) => { if (event === "worker_message") void messages.receive(payload).catch(error => {
        if (!closed) log("warn", "worker_message_pending", { code: error.code ?? "pending", messageId: payload?.message_id });
      }); },
      onConnect: info => { log("info", "router_connected", info); pollMessages(); }, onDisconnect: info => log("warn", "router_disconnected", info) });
    return service;
  }
}
