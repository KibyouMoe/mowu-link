// The transport/report contracts are adapted from @dsh/hermes-bridge (MIT).
// The only conversation backend is the tool pipe injected by Codex Desktop.
import { createHash, randomUUID, timingSafeEqual } from "node:crypto";
import { mkdir, open, readFile, rename, unlink, writeFile } from "node:fs/promises";
import http from "node:http";
import net from "node:net";
import path from "node:path";
import os from "node:os";
import { FeishuNotifier, FeishuStreamBeat, FeishuWebsocketClient } from "./feishu.mjs";
import { localHost, nativeSendSource } from "./codex/native-dispatch.mjs";
import { QuestionRelay, QuestionRelayError } from "./question-relay.mjs";
import { cacheFeishuImage, imagePrompt } from "./feishu-images.mjs";
import { FeishuOnlineAnnouncement } from "./online-announcement.mjs";
import { BusinessRuntime, BusinessRuntimeError } from "./business-runtime.mjs";
import { PublicImageRelay, splitPublicMarkdown } from "./public-image-relay.mjs";
import { hydrateNativeInputs, readNativeWindow } from "./codex/native-history.mjs";
import { operatingConfig } from "./integration-profile.mjs";
import { resumeCandidates } from "./resume-candidates.mjs";
import { publicProgress, nativeTaskState } from "./codex/native-progress.mjs";
import { LocalPublicJournal } from "./local-public-journal.mjs";
import { BRIDGE_HELP, goalPrompt } from "./feishu-commands.mjs";
import { readLocalResumeCatalog } from "./local-resume-catalog.mjs";
import { routerStatus } from "./router-client.mjs";
import { resumePage, resumeListText } from "./resume-list.mjs";
import { isAbandonedText, isUnconfirmedText } from "./text-receipt-review.mjs";

export const PROTOCOL = "1.1.1-draft";
const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const digest = value => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const orderedJson = value => Array.isArray(value) ? value.map(orderedJson)
  : value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().map(key => [key, orderedJson(value[key])])) : value;
const requestFingerprint = request => digest(orderedJson(request));
const clone = value => structuredClone(value);
const terminal = status => ["completed", "interrupted", "failed", "cancelled", "canceled"].includes(status);

export class BridgeError extends Error {
  constructor(status, error, detail, retryable = false, extra = {}) {
    super(detail); this.status = status; this.body = { error, detail, retryable, ...extra };
  }
}
const fail = (status, error, detail, retryable = false, extra) => {
  throw new BridgeError(status, error, detail, retryable, extra);
};

export function publicItems(snapshot) {
  const result = [];
  const turns = snapshot.page?.order === "newest_first" ? [...(snapshot.turns ?? [])].reverse() : snapshot.turns ?? [];
  for (const turn of turns) {
    for (const [index, item] of (turn.items ?? []).entries()) {
      const key = `${turn.id}/${item.id ?? index}`;
      if (item.type === "agentMessage" && typeof item.text === "string") {
        result.push({ key, turnId: turn.id, kind: "assistant", text: item.text,
          final: item.phase === "final_answer" || item.phase === "final" });
      } else if (item.type === "userMessage") {
        const text = item.text ?? (item.content ?? []).map(c => c.text ?? "").join("\n");
        result.push({ key, turnId: turn.id, kind: "trainer", text });
      } else if (item.type === "functionCallOutput" && ["create_thread", "send_message_to_thread"].includes(item.name)) {
        const raw = typeof item.output === "string" ? item.output : item.output?.text;
        const start = raw?.indexOf("<input>") ?? -1;
        const end = raw?.lastIndexOf("</input>") ?? -1;
        const input = start >= 0 && end > start ? raw.slice(start + 7, end) : null;
        if (input) result.push({ key, turnId: turn.id, kind: "delegated", text: input,
          nativeTool: item.name, sourceThreadId: raw.slice(0, start).match(/<source_thread_id>([^<]+)<\/source_thread_id>/)?.[1] ?? null });
      }
      // Never export private reasoning, raw tool arguments/results, or auth metadata.
    }
  }
  return result;
}

function activeTurn(snapshot) {
  if (snapshot.deliveryObservation) return terminal(snapshot.deliveryObservation.turnStatus) ? null
    : { id: snapshot.deliveryObservation.turnId, status: snapshot.deliveryObservation.turnStatus };
  return (snapshot.turns ?? []).find(t => !terminal(t.status) && ["inProgress", "in_progress", "running", "started", "pending"].includes(t.status));
}

export class DesktopManager {
  constructor({ native, listNativeTools, stateDir, config = {}, fetchImpl = fetch }) {
    this.native = native; this.listNativeTools = listNativeTools; this.stateDir = stateDir;
    this.fetch = fetchImpl;
    this.config = operatingConfig({ host: "127.0.0.1", port: 8903, bridgeName: "CodexWorker", machine: "codexworker",
      pollMs: 1000, turnTimeoutMs: 600000, workspaceRoots: [], projectId: null,
      feishu: {}, ...config });
    this.statePath = path.join(stateDir, "desktop-manager-state.json");
    this.state = { version: 1, seq: 0, replayFloor: 0, nextName: 1, selectedThreadId: null,
      sessions: {}, receipts: {}, events: [], feishuReceipts: {}, selectionEpoch: 0, autoImages: {} };
    this.tail = Promise.resolve(); this.clients = new Set(); this.connected = false;
    this.nativeInputCache = new Map();
    this.nativeHistoryWindows = new Map();
    this.nativeProgressPolls = new Map();
    this.publicJournal = new LocalPublicJournal(config.publicJournalRoot ?? path.join(path.dirname(path.dirname(stateDir)), "sessions"));
    this.taskJournal = new LocalPublicJournal(this.publicJournal.root);
    this.taskJournalTails = new Map();
    this.resumeMetadata = () => readLocalResumeCatalog(config.codexHome ?? path.dirname(path.dirname(stateDir)));
    this.closed = false; this.pollRunning = false;
    this.relayStartedAt = new Date().toISOString();
    this.backgroundTasks = new Set(); this.serialPending = 0; this.nativeOperations = 0; this.httpOperations = 0;
    this.handoffPaused = false;
    this.handoff = { phase: "running", error: null, startedAt: null };
    this.inboxPath = path.join(stateDir, "feishu-inbox.json"); this.inbox = { version: 1, entries: {} };
    this.inboxTail = Promise.resolve(); this.inboxRunning = new Set();
    this.tools = []; this.notifier = null; this.ws = null; this.feishuBeats = new Map();
    this.feishuInputs = new Map(); this.feishuReactionTasks = new Map();
    this.questions = new QuestionRelay(this);
    this.onlineAnnouncement = new FeishuOnlineAnnouncement(this);
    this.businessRuntime = new BusinessRuntime({ stateDir });
    this.publicImageRelay = new PublicImageRelay(this);
  }

  serial(fn) {
    this.serialPending++;
    const promise = this.tail.then(fn).finally(() => { this.serialPending--; });
    this.tail = promise.catch(() => {}); return promise;
  }

  track(task) {
    this.backgroundTasks.add(task);
    void task.then(() => this.backgroundTasks.delete(task), () => this.backgroundTasks.delete(task));
    return task;
  }

  hotBusy() {
    return Object.values(this.hotBlockers()).some(Boolean);
  }

  hotBlockers() {
    return { poll: Number(this.pollRunning), serial: this.serialPending,
      native: this.nativeOperations, http: this.httpOperations, images: this.imageOperations ?? 0,
      background: this.backgroundTasks.size, inputs: this.feishuInputs.size,
      reactions: this.feishuReactionTasks.size };
  }

  hotHandoffStatus() {
    return { policy: "bounded-active-drain", ...this.handoff, paused: this.handoffPaused,
      blockers: this.hotBlockers() };
  }

  // The existing fixed loader calls business.busy ONLY for a requested source
  // handoff. Treat that inspect as intent: stop creating background work before
  // waiting for in-flight operations. No loader/stdio restart is required.
  requestHotHandoff(readRuntimeStatus = () => ({})) {
    if (this.closed) return true;
    if (!this.handoffPaused) {
      if (performance.now() < (this.handoffRetryAt ?? 0)) return true;
      const configured = this.config.hotHandoffTimeoutMs;
      const timeoutMs = Number.isFinite(configured) ? Math.max(50, Math.min(configured, 45000)) : 45000;
      const initialStatus = readRuntimeStatus();
      const started = performance.now();
      this.handoffPaused = true;
      this.handoff = { phase: "draining", error: null, startedAt: new Date().toISOString(), timeoutMs };
      this.onlineAnnouncement.pause();
      this.handoffTimer = setInterval(() => {
        try {
          const status = readRuntimeStatus();
          // IPC replaces the status object on every publish. This detects even
          // a repeated validation error without exposing source/config hashes.
          if (status !== initialStatus && status.lastReloadError && !status.reloadPending) {
            this.resumeHotHandoff("candidate_rejected"); return;
          }
          if (performance.now() - started >= timeoutMs) this.resumeHotHandoff("handoff_timeout");
        } catch { this.resumeHotHandoff("runtime_status_unavailable"); }
      }, 25);
      this.handoffTimer.unref();
    }
    const busy = this.hotBusy();
    this.handoff.phase = busy ? "draining" : "ready";
    return busy;
  }

  resumeHotHandoff(error = null) {
    clearInterval(this.handoffTimer); this.handoffTimer = null;
    if (!this.handoffPaused || this.closed) return;
    this.handoffPaused = false;
    this.handoff = { ...this.handoff, phase: error ? "blocked" : "running", error,
      ...(error ? { blockedAt: new Date().toISOString(), lastBlockers: this.hotBlockers() } : {}) };
    const configured = this.config.hotHandoffRetryMs;
    const retryMs = Number.isFinite(configured) ? Math.max(50, Math.min(configured, 30000)) : 30000;
    this.handoffRetryAt = error ? performance.now() + retryMs : 0;
    this.onlineAnnouncement.resume();
    for (const event of Object.values(this.inbox.entries)) this.runInbox(event);
    // Existing intervals resume naturally. Do not replay uncertain writes or
    // eagerly duplicate side-channel sends merely because a handoff failed.
  }

  commitHotHandoff() {
    clearInterval(this.handoffTimer); this.handoffTimer = null;
    this.handoff.phase = "committing";
  }

  async initialize() {
    if (!this.config.token || this.config.token.length < 24) {
      fail(503, "bridge_not_ready", "A machine-local Bearer token of at least 24 characters is required.");
    }
    if (!Array.isArray(this.config.workspaceRoots) || !this.config.workspaceRoots.length) {
      fail(503, "bridge_not_ready", "Bind an allowed Codex Desktop workspace before starting the bridge.");
    }
    await mkdir(this.stateDir, { recursive: true });
    // Windows releases a named pipe when its owner exits, even after a crash.
    // Acquire this OS lease before examining a stale diagnostic lock file so
    // simultaneous MCP restarts cannot both remove it and become writers.
    const leaseId = digest(path.resolve(this.stateDir).toLowerCase()).slice(0, 24);
    const leaseAddress = process.platform === "win32" ? `\\\\.\\pipe\\codexworker-manager-${leaseId}` : path.join(this.stateDir, "desktop-manager.sock");
    this.writerLease = net.createServer(socket => socket.end());
    try {
      await new Promise((resolve, reject) => { this.writerLease.once("error", reject); this.writerLease.listen(leaseAddress, resolve); });
    } catch (error) {
      if (error.code === "EADDRINUSE") fail(503, "manager_instance_active", "A live plugin instance owns the machine-level Desktop manager.", true);
      throw error;
    }
    this.leasePath = path.join(this.stateDir, "desktop-manager.lock");
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const handle = await open(this.leasePath, "wx", 0o600);
        this.leaseNonce = randomUUID();
        try { await handle.writeFile(JSON.stringify({ pid: process.pid, nonce: this.leaseNonce, startedAt: new Date().toISOString() })); }
        finally { await handle.close(); }
        break;
      } catch (error) {
        if (error.code !== "EEXIST") throw error;
        if (process.platform === "win32" && attempt === 0) {
          // The OS lease above is authoritative. After a reboot Windows may
          // reuse yesterday's diagnostic PID for an unrelated live process.
          // No other compliant writer can own this file while we hold the pipe.
          await unlink(this.leasePath); continue;
        }
        let owner;
        try { owner = JSON.parse(await readFile(this.leasePath, "utf8")); }
        catch { fail(503, "manager_instance_active", "Another plugin instance is initializing the shared Desktop manager.", true); }
        try { process.kill(owner.pid, 0); }
        catch (probe) {
          if (probe.code === "ESRCH" && attempt === 0) { await unlink(this.leasePath); continue; }
        }
        fail(503, "manager_instance_active", "A live plugin instance owns this machine's Desktop manager. Use its authenticated HTTP endpoint.", true);
      }
    }
    try { this.state = JSON.parse(await readFile(this.statePath, "utf8")); }
    catch (error) {
      if (error.code !== "ENOENT") fail(503, "manager_state_corrupt", "Durable Bridge state cannot be read; the original file is preserved. Review recovery without replaying unknown messages.", false);
    }
    if (this.state?.version !== 1 || !this.state.sessions || !this.state.receipts ||
        typeof this.state.sessions !== "object" || Array.isArray(this.state.sessions) ||
        typeof this.state.receipts !== "object" || Array.isArray(this.state.receipts) ||
        !Array.isArray(this.state.events)) {
      fail(503, "manager_state_corrupt", "Durable Bridge state failed validation; the original file is preserved.", false);
    }
    this.stateLoaded = true;
    this.state.selectionEpoch ??= 0; this.state.autoImages ??= {};
    this.tools = await this.listNativeTools();
    for (const name of ["read_thread", "send_message_to_thread", "create_thread", "fork_thread", "set_thread_title", "list_threads"]) {
      if (!this.tools.some(t => t.name === name)) fail(503, "desktop_integration_unavailable", `Desktop tool missing: ${name}`, true);
    }
    this.connected = true;
    for (const session of Object.values(this.state.sessions)) {
      session.pendingInputs ??= [];
      // Upgrade the old single readback slot without resending its native write.
      if (session.dispatchCorrelation && !session.pendingInputs.some(p => p.messageId === session.dispatchCorrelation.messageId)) {
        session.pendingInputs.push({ ...session.dispatchCorrelation, dispatchedAt: session.dispatchedAt ?? Date.now() });
      }
      delete session.dispatchPending; delete session.dispatchCorrelation; delete session.dispatchedAt;
    }
    for (const receipt of Object.values(this.state.receipts)) {
      if (receipt.pending) {
        // A crash may occur after the Desktop accepted a write. Never resend it blindly.
        receipt.pending = false;
        for (const session of Object.values(this.state.sessions)) {
          const input = session.pendingInputs.find(p => p.messageId === receipt.request.messageId);
          if (input) input.unknownOutcome = true;
        }
        receipt.error = { error: "dispatch_outcome_unknown", detail: "Bridge restarted during dispatch; read the native thread before submitting a new message.", retryable: false };
        await this.emit("message.rejected", receipt.request, receipt.error);
      }
    }
    await this.save();
    this.state.feishuReceipts ??= {};
    try { this.inbox = JSON.parse(await readFile(this.inboxPath, "utf8")); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    if (this.inbox.version !== 1 || !this.inbox.entries || typeof this.inbox.entries !== "object") fail(503, "feishu_inbox_invalid", "Durable Feishu ingress cannot be read; no entries were discarded.");
    await this.questions.initialize();
    this.configureIntegrations();
    await this.businessRuntime.initialize().catch(() => {
      console.error("[bridge] Image business module unavailable; native text ingress remains active.");
    });
    for (const event of Object.values(this.inbox.entries)) this.runInbox(event);
    return this;
  }

  configureIntegrations() {
    this.onlineAnnouncement.reset();
    this.ws?.stop(); this.ws = null; this.notifier = null;
    const f = this.config.feishu;
    if (f?.appId && f?.appSecret) {
      this.notifier = new FeishuNotifier({ ...f, bridgeName: this.config.bridgeName,
        canSend: ({ threadId }) => !this.closed && (!threadId || threadId === this.state.selectedThreadId) });
      for (const name of ["sendMessage", "sendQuestionCard", "updateQuestionCard", "downloadImage", "addReaction", "removeReaction", "warmup"]) {
        const method = this.notifier[name].bind(this.notifier);
        this.notifier[name] = (...args) => this.track(method(...args));
      }
      if (f.inboundEnabled && f.trainerOpenIdWhitelist?.length) {
        const socket = new FeishuWebsocketClient(this.notifier, { onMessage: event => {
          if (this.ws !== socket || this.closed) return Promise.reject(new Error("ingress_quiescing"));
          return this.track(this.enqueueFeishu(event));
        }, onCardAction: payload => {
          if (this.ws !== socket || this.closed) return Promise.reject(new Error("ingress_quiescing"));
          return this.track(this.questions.acceptCard(payload));
        }, onConnected: ({ connectionId }) => this.onlineAnnouncement.connected(socket, connectionId) });
        this.ws = socket;
        this.ws.start();
      }
    }
  }

  integrationStatus() {
    const selected = this.state.sessions[this.state.selectedThreadId];
    return { pid: process.pid, runtimeHostPid: Number(process.env.RUDOLF_BRIDGE_RUNTIME_HOST_PID) || null,
      operatingMode: this.config.operatingMode,
      selectedThreadId: this.state.selectedThreadId,
      feishu: { enabled: Boolean(this.notifier?.enabled), inboundEnabled: Boolean(this.ws),
        connected: this.ws?._ws?.readyState === 1,
        connection: this.ws?.status?.() ?? null,
        relay: selected ? { threadId: selected.threadId, sessionState: selected.state,
          startedAt: this.relayStartedAt, lastSuccessAt: selected.relayStatus?.lastSuccessAt ?? null,
          lastAttemptAt: selected.relayStatus?.lastAttemptAt ?? null,
          lastError: selected.relayStatus?.lastError ?? null, failures: selected.relayStatus?.failures ?? 0,
          mode: selected.relayStatus?.mode ?? "full-history", lastProgressAt: selected.relayStatus?.lastProgressAt ?? null,
          latestTextDelivery: selected.publicTextDelivery ?? selected.limitedTextDelivery ?? null,
          journal: selected.relayStatus?.journal ?? null, journalError: selected.relayStatus?.journalError ?? null,
          selfTarget: this.isSelfTarget(selected), taskStateSource: this.isSelfTarget(selected) ? "desktop-task-journal" : "native-status",
          taskJournal: selected.taskJournalStatus ?? null,
          unconfirmedTexts: Object.values(this.state.publicTextOutbox ?? {}).filter(e => e.threadId === selected.threadId && e.bindingEpoch === this.state.selectionEpoch && isUnconfirmedText(e)).length,
          abandonedUnknownTexts: Object.values(this.state.publicTextOutbox ?? {}).filter(e => e.threadId === selected.threadId && isAbandonedText(e)).length,
          unconfirmedImages: Object.values(this.state.autoImages ?? {}).filter(e => e.threadId === selected.threadId && e.bindingEpoch === this.state.selectionEpoch && /unknown/.test(e.error ?? "")).length,
          historyWindow: selected.historyWindow ?? null } : null,
        inboxPending: Object.keys(this.inbox.entries).length,
        trainerWhitelistCount: this.config.feishu.trainerOpenIdWhitelist?.length ?? 0,
        onlineNotice: this.onlineAnnouncement.status(), publicImages: this.publicImageRelay.status(), questions: this.questions.status() },
      router: routerStatus() };
  }

  imageTarget(threadId) {
    const session = this.session(threadId);
    if (session.threadId !== this.state.selectedThreadId) fail(409, "selected_target_changed", "Image output follows the Feishu-selected chat only; no fallback.");
    if (!localHost(session.hostId)) fail(422, "capability_unavailable", "Image output uses local workspace files only.");
    const chatId = session.feishuChatId ?? this.notifier?.chatId;
    if (!chatId || !this.notifier?.enabled) fail(503, "feishu_unavailable", "Current chat has no configured Feishu output.");
    return { threadId: session.threadId, chatId };
  }

  generatedImageRoot(threadId) {
    // Native imageGeneration.savedPath lives under this exact chat directory.
    // Do not grant the whole Codex home or other chats' generated-image storage.
    if (typeof threadId !== "string" || !/^[a-z\d-]+$/i.test(threadId)) return [];
    return [path.join(process.env.CODEX_HOME || path.join(os.homedir(), ".codex"), "generated_images", threadId)];
  }

  async sendImage(body, validateBinding = () => {}, publicEntry = null) {
    if (!body || typeof body !== "object" || Object.keys(body).some(key => !["imagePath", "threadId", "messageId"].includes(key))) fail(400, "invalid_request", "Only imagePath, threadId and messageId are accepted.");
    if (publicEntry && (!Object.values(this.state.autoImages ?? {}).includes(publicEntry) || publicEntry.status !== "pending" ||
        publicEntry.threadId !== this.state.selectedThreadId || publicEntry.bindingEpoch !== this.state.selectionEpoch ||
        publicEntry.imagePath !== body.imagePath || publicEntry.messageId !== body.messageId || publicEntry.threadId !== body.threadId)) {
      fail(403, "image_display_not_authorized", "No matching current public image queue entry; no file was uploaded.");
    }
    const bindingEpoch = this.state.selectionEpoch;
    const validate = () => {
      validateBinding();
      if (bindingEpoch !== this.state.selectionEpoch) fail(409, "selected_target_changed", "Feishu selection changed during image output; no fallback.");
    };
    return this.businessRuntime.sendImage(body, { stateDir: this.stateDir, notifier: this.notifier,
      fetchImpl: this.fetch, allowedRoots: [...this.config.workspaceRoots, path.join(this.stateDir, "feishu-images"),
        ...this.generatedImageRoot(body.threadId ?? this.state.selectedThreadId)],
      // The trainer authorized manual sends from any local directory as well.
      // Both routes grant one exact requested/displayed file, not a directory.
      displayedFiles: [body.imagePath],
      excludedRoots: publicEntry ? [path.join(this.stateDir, "feishu-images")] : [],
      getTarget: threadId => { validate(); return this.imageTarget(threadId); },
      validateTarget: expected => {
        validate();
        const current = this.imageTarget(expected.threadId);
        if (current.chatId !== expected.chatId) fail(409, "selected_target_changed", "Feishu destination changed; image was not sent.");
      } });
  }

  async recoverImage(body) {
    if (!body || typeof body !== "object" || Object.keys(body).some(key => !["imagePath", "threadId", "messageId"].includes(key))) fail(400, "invalid_request", "Only imagePath, threadId and original messageId are accepted.");
    const entry = Object.values(this.state.autoImages ?? {}).find(e => e.messageId === body.messageId && e.threadId === body.threadId);
    const epoch = this.state.selectionEpoch;
    const validate = () => {
      if (!entry || !["failed", "sent"].includes(entry.status) || (entry.status === "failed" && entry.error !== "image_send_outcome_unknown") ||
          entry.threadId !== this.state.selectedThreadId || entry.bindingEpoch !== this.state.selectionEpoch || epoch !== this.state.selectionEpoch) {
        fail(409, "image_recovery_not_authorized", "Recovery requires the original unknown public-image receipt in the unchanged current binding.");
      }
    };
    validate();
    const result = await this.businessRuntime.sendImage(body, { stateDir: this.stateDir, notifier: this.notifier,
      fetchImpl: this.fetch, allowedRoots: this.config.workspaceRoots, displayedFiles: [body.imagePath],
      excludedRoots: [path.join(this.stateDir, "feishu-images")], recoveryRequested: true,
      getTarget: id => { validate(); return this.imageTarget(id); },
      validateTarget: target => { validate(); if (this.imageTarget(target.threadId).chatId !== target.chatId) fail(409, "selected_target_changed", "Feishu destination changed; no fallback."); } });
    await this.serial(async () => {
      entry.previousFailure ??= { error: entry.error, completedAt: entry.completedAt };
      entry.status = "sent"; entry.feishuMessageId = result.feishuMessageId; entry.recoveredAt = Date.now();
      delete entry.error; await this.save();
    });
    return result;
  }

  async reloadIntegrations() {
    const configured = operatingConfig(JSON.parse(await readFile(path.join(this.stateDir, "desktop-config.local.json"), "utf8")));
    if (configured.operatingMode !== this.config.operatingMode) {
      fail(409, "runtime_reload_required", "Operating mode changes require the normal business handoff.", true);
    }
    const f = configured.feishu ?? {};
    let secret = {};
    if (f.configFile) secret = JSON.parse(await readFile(f.configFile, "utf8"));
    this.config.feishu = { appId: secret.app_id, appSecret: secret.app_secret,
      chatId: secret.chat_id ?? secret.trainer_chat_id,
      trainerOpenIdWhitelist: secret.trainer_open_id ? [secret.trainer_open_id] : [], ...f };
    this.configureIntegrations();
    return this.integrationStatus();
  }

  async save() {
    const temp = `${this.statePath}.${randomUUID()}.tmp`;
    await writeFile(temp, `${JSON.stringify(this.state)}\n`, { encoding: "utf8", mode: 0o600 });
    for (let attempt = 0; ; attempt++) {
      try { await rename(temp, this.statePath); break; }
      catch (error) {
        if (!["EPERM", "EBUSY", "EACCES"].includes(error.code) || attempt >= 7) throw error;
        // Windows antivirus/readers can briefly hold the destination open. Never
        // unlink the old state to work around this; preserve atomic replacement.
        await new Promise(resolve => setTimeout(resolve, 20 * (attempt + 1)));
      }
    }
  }

  health() {
    // Never claim the full 1.1.1 manager contract before native interruption,
    // exact active-turn steering and the ingress/fork acceptance gates are proven.
    return { name: this.config.bridgeName, version: "0.2.38", alive: !this.closed, ready: this.connected,
      operatingMode: this.config.operatingMode,
      integrationMode: "desktop-native", machine: this.config.machine,
      managerProtocolVersion: PROTOCOL, managerMinCompatibleVersion: PROTOCOL,
      managerReady: false, nativeTaskReady: false,
      managerUnavailableReason: "Staged desktop-native DSH-compatible adapter; local busy delivery is verified, but hard interrupt and atomic expected-turn control are unavailable. Do not hand off as the full manager.",
      workspace: this.config.workspaceRoots[0], workspaces: this.config.workspaceRoots,
      workspaceProjectId: this.config.projectId, eventSeq: this.state.seq,
      activeTurns: Object.values(this.state.sessions).filter(s => s.activeTurnId).length,
      poolSize: Object.keys(this.state.sessions).length, maxPool: 50,
      desktopBuild: "Codex Desktop injected native task pipe", runtimeBuild: "codexworker 0.2.38",
      integrations: this.integrationStatus(),
      appServerSchemaFile: "desktop-native-tools.json", appServerSchemaSha256: digest(this.tools),
      capabilities: { existingThreadSend: this.connected, new: this.connected, fork: this.connected,
        trainerInputSync: "native-transcript-poll", outputSync: "native-transcript-poll",
        pollIntervalMs: this.config.pollMs, queue: true, nativeBusyDelivery: Boolean(this.config.nativeBusyDeliveryVerified),
        localMessageOrigin: this.config.nativeSendFromTarget ? "target-task" : "bound-manager-task",
        remoteViaDesktopDevices: true, feishuImageOutput: Boolean(this.businessRuntime.module && this.notifier?.enabled),
        feishuOutputRouting: "selected-thread-only",
        feishuReconnectPolicy: "bounded-handshake-heartbeat",
        feishuQuestionCards: "durable-callback-source-turn",
        feishuPublicImageSync: this.config.feishu.autoImageOutput === false ? "disabled" : "selected-thread-public-images",
        feishuPublicImagePathPolicy: "exact-public-display",
        feishuImageRecovery: "bounded-same-uuid-50m",
        desktopStartupHandoff: { requiredBootstrap: "0.2.23", requiredLaunchMode: "windows-desktop-parent",
          active: this.businessRuntime.status().bootstrapVersion === "0.2.23" &&
            this.businessRuntime.status().desktopLifetimeLaunchMode === "windows-desktop-parent" },
        businessHotReload: this.businessRuntime.status(),
        stop: false, compact: false, perTurnSafetyOverride: false, tokenStreaming: false } };
  }

  async call(name, args, deliveryContext) {
    if (this.closed) fail(503, "desktop_integration_unavailable", "Desktop bridge is closed.", true);
    this.nativeOperations++;
    try { const value = await this.native(name, args, deliveryContext); this.connected = true; return value; }
    catch (error) {
      if (error?.nativeStatusPreflight) fail(503, "native_status_context_unavailable", "The bound manager's real lifecycle context is not available; no native status request was sent.", true);
      // A task-specific rejection (for example an oversized snapshot) is an
      // answer from a live native channel. Only transport loss makes it unready;
      // otherwise the supervisor would recycle healthy Feishu/HTTP every 45s.
      this.connected = error?.nativeReachable === true;
      if (error instanceof BridgeError) throw error;
      if (error?.nativeErrorCode === "native_snapshot_too_large") {
        fail(413, "native_snapshot_too_large", "Native history response exceeded Desktop's size limit.", false);
      }
      if (name === "read_thread" && error?.nativeErrorCode === "native_read_rejected") {
        fail(503, "native_read_rejected", "Desktop rejected this history page; a smaller read-only window may be available.", true);
      }
      // A tool timeout can have an unknown write outcome. Never automatically retry a write.
      fail(503, "desktop_integration_unavailable", `Native ${name} failed; inspect the Desktop task before retrying.`, false);
    } finally { this.nativeOperations--; }
  }

  checkWorkspace(snapshot, adopting = false) {
    const cwd = snapshot.thread?.cwd;
    const allowed = typeof cwd === "string" && this.config.workspaceRoots.some(root => {
      const relative = path.relative(path.resolve(root), path.resolve(cwd));
      return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
    });
    if (!allowed) fail(adopting ? 422 : 400, adopting ? "adopt_failed" : "invalid_request",
      "Task is outside the allowed workspace; 该任务仍未受 Bridge 管理。", false);
    return cwd;
  }

  async read(threadId, hostId, { turnLimit = 10 } = {}) {
    // Public assistant/trainer messages and imageView/imageGeneration nodes are
    // returned without raw tool outputs. Those outputs can overflow Desktop's
    // response limit in long chats and are not part of the public relay.
    const args = { threadId, turnLimit, includeOutputs: false, maxOutputCharsPerItem: 20000 };
    if (hostId) args.hostId = hostId;
    let snapshot;
    for (let attempt = 0; ; attempt++) {
      try { snapshot = await readNativeWindow(pageArgs => this.call("read_thread", pageArgs), args, this.nativeHistoryWindows); break; }
      catch (error) {
        if (error.body?.error === "native_single_turn_too_large") {
          if (turnLimit === 10 && this.state.sessions[threadId]) this.state.sessions[threadId].historyWindow = clone(error.nativeHistoryWindow);
          fail(413, error.body.error, error.body.detail, true);
        }
        // Read-only snapshots can fail transiently while Desktop changes turns.
        // Never apply this retry policy to send/create/fork or an unknown write.
        if (error.body?.error !== "desktop_integration_unavailable" || attempt >= 2) throw error;
        await new Promise(resolve => setTimeout(resolve, 100 * (attempt + 1)));
      }
    }
    if (!snapshot?.thread?.id || snapshot.thread.id !== threadId) fail(404, "thread_not_found", "Native task was not found.", false, { threadId });
    if (turnLimit === 10 && this.state.sessions[threadId]) this.state.sessions[threadId].historyWindow = clone(snapshot.nativeHistoryWindow);
    try {
      return await hydrateNativeInputs(snapshot, pageArgs => this.call("read_thread", { ...pageArgs, ...(hostId ? { hostId } : {}) }), this.nativeInputCache);
    } catch (error) {
      if (error.body?.error === "native_snapshot_incomplete") fail(503, error.body.error, error.body.detail, true);
      throw error;
    }
  }

  isSelfTarget(session) {
    return localHost(session.hostId) && session.threadId === this.config.nativeSenderThreadId;
  }

  async journalDeliveryState(session) {
    const id = session.threadId, epoch = this.state.selectionEpoch;
    const work = (this.taskJournalTails.get(id) ?? Promise.resolve()).then(async () => {
      const batch = await this.taskJournal.read(session, session.taskJournalCheckpoint,
        { turnId: null, epoch, stateOnly: true });
      if (epoch !== this.state.selectionEpoch) fail(409, "selected_target_changed", "The binding changed while observing local task state.");
      session.taskJournalCheckpoint = batch.checkpoint;
      session.taskJournalStatus = { caughtUp: batch.caughtUp && batch.pendingBytes === 0,
        pendingBytes: batch.pendingBytes, bytesRead: batch.bytesRead };
      if (!session.taskJournalStatus.caughtUp || !batch.taskState) {
        fail(503, "local_task_state_pending", "Verified local lifecycle events are not caught up; execution state is unknown.", true);
      }
      return { deliveryObservation: { ...batch.taskState, source: "desktop-task-journal" } };
    });
    const tail = work.catch(() => {});
    this.taskJournalTails.set(id, tail);
    try { return await work; }
    finally { if (this.taskJournalTails.get(id) === tail) this.taskJournalTails.delete(id); }
  }

  async deliveryState(session) {
    // The native host explicitly forbids wait_threads on its caller. Keep the
    // real caller/binding and use verified, bounded local lifecycle events.
    if (this.isSelfTarget(session)) return this.journalDeliveryState(session);
    if (localHost(session.hostId) && this.tools.some(t => t.name === "wait_threads")) {
      let progress;
      try {
        progress = nativeTaskState(await this.call("wait_threads", {
          targets: [{ threadId: session.threadId, hostId: "local" }], timeoutMs: 0
        }), session.threadId);
      } catch (error) {
        if (!["desktop_integration_unavailable", "native_status_context_unavailable"].includes(error.body?.error)) throw error;
        // This was a read-only failure. An independently verified target
        // lifecycle can still provide real state, never an assumed idle value.
        return this.journalDeliveryState(session);
      }
      if (!["completed", "interrupted", "failed", "cancelled", "canceled", "inProgress", "in_progress", "running", "started", "pending"].includes(progress.turnStatus)) {
        fail(503, "native_status_unavailable", "Native task state is not recognized; no message was sent.", true);
      }
      // Explicitly a live status observation, NEVER a fabricated history page.
      return { deliveryObservation: progress };
    }
    return this.read(session.threadId, session.hostId);
  }

  async selectionState(candidate) {
    try { return await this.read(candidate.threadId, candidate.hostId, { turnLimit: 1 }); }
    catch (error) {
      if (error.body?.error !== "native_single_turn_too_large" || !localHost(candidate.hostId)) throw error;
      const known = this.state.sessions[candidate.threadId] ?? candidate;
      this.checkWorkspace({ thread: { cwd: known.cwd } }, true);
      const live = await this.deliveryState(known);
      if (!live.deliveryObservation) throw error;
      // Verify the exact local journal's header/cwd; this is an explicit empty
      // baseline on selection, not a claim to have recovered native history.
      await this.publicJournal.read(known, null, { turnId: live.deliveryObservation.turnId,
        epoch: this.state.selectionEpoch + 1, startAt: new Date().toISOString() });
      return { ...live, thread: { id: candidate.threadId, cwd: known.cwd, title: candidate.alias ?? known.alias },
        turns: [], publicJournalBaseline: true };
    }
  }

  async emit(type, request = {}, payload = {}, extra = {}) {
    if (type === "message.rejected") this.finishFeishuInput(request.messageId);
    if (type === "turn.completed") {
      for (const id of payload.messageIds ?? [request.messageId]) this.finishFeishuInput(id);
    }
    const event = { protocolVersion: PROTOCOL, seq: ++this.state.seq, type,
      bridge: this.config.bridgeName, machine: this.config.machine, timestamp: new Date().toISOString(),
      threadId: request.targetThreadId ?? request.threadId ?? null,
      messageId: request.messageId ?? null, source: request.source ?? "bridge", payload, ...extra };
    this.state.events.push(event);
    const now = Date.now();
    const retained = this.state.events.filter(e => now - Date.parse(e.timestamp) <= (e.type === "item.delta" ? 3600000 : 30 * 86400000));
    // A single persisted journal/registry snapshot has a hard 256MiB limit.
    let bytes = Buffer.byteLength(JSON.stringify(retained));
    while (bytes > 256 * 1024 * 1024 && retained.length) bytes -= Buffer.byteLength(JSON.stringify(retained.shift())) + 1;
    if (retained.length !== this.state.events.length) {
      const retainedSeqs = new Set(retained.map(e => e.seq));
      for (const old of this.state.events) if (!retainedSeqs.has(old.seq)) this.state.replayFloor = Math.max(this.state.replayFloor, old.seq);
    }
    this.state.events = retained;
    if (event.threadId && this.state.sessions[event.threadId]) this.state.sessions[event.threadId].lastEventSeq = event.seq;
    await this.save();
    for (const client of this.clients) {
      if (client.writableLength > 1024 * 1024) { client.destroy(); this.clients.delete(client); continue; }
      client.write(`id: ${event.seq}\nevent: bridge\ndata: ${JSON.stringify(event)}\n\n`);
    }
    return event;
  }

  session(threadId) {
    const id = threadId ?? this.state.selectedThreadId;
    if (!id) fail(409, "no_selected_session", "Use /new or register/select an existing task first.");
    const session = this.state.sessions[id];
    if (!session) fail(404, "thread_not_found", "Explicit target is not registered; no fallback was used.", false, { threadId: id });
    return session;
  }

  validate(request) {
    if (!UUID4.test(request.messageId ?? "")) fail(400, "invalid_request", "messageId must be UUIDv4.");
    if (request.protocolVersion !== PROTOCOL) fail(426, "protocol_version_unsupported", `Use ${PROTOCOL}.`, false,
      { managerProtocolVersion: PROTOCOL, managerMinCompatibleVersion: PROTOCOL });
    if (!["trainer", "hermes"].includes(request.source)) fail(400, "invalid_request", "source must be trainer or hermes.");
    if (request.delivery != null && !["queue", "steer"].includes(request.delivery)) fail(400, "invalid_request", "delivery must be steer or queue.");
    if (request.safetyMode != null) fail(422, "capability_unavailable", "Native task tools cannot change per-turn safety policy. Configure policy in Codex Desktop.");
  }

  async once(request, operation) {
    this.validate(request);
    return this.serial(async () => {
      const fingerprint = requestFingerprint(request);
      const old = this.state.receipts[request.messageId];
      if (old) {
        // Existing receipts used insertion-order hashes; compare their saved JSON
        // semantically so retries remain valid across upgrades and key reordering.
        if ((old.request ? requestFingerprint(old.request) : old.fingerprint) !== fingerprint) fail(409, "message_id_conflict", "messageId was already used with different content.", false, { messageId: request.messageId });
        if (old.error) throw new BridgeError(old.status ?? 409, old.error.error, old.error.detail, old.error.retryable, { ...old.error, messageId: request.messageId });
        return { ...clone(old.response), duplicate: true };
      }
      const receipt = { fingerprint, request: clone(request), pending: true };
      this.state.receipts[request.messageId] = receipt;
      await this.save();
      let accepted = false;
      try {
        const event = await this.emit("message.accepted", request, { text: request.text ?? "/adopt", delivery: request.delivery ?? "steer" });
        accepted = true;
        const result = await operation();
        receipt.pending = false;
        receipt.response = { accepted: true, duplicate: false, messageId: request.messageId, eventSeq: event.seq, ...result };
        await this.save(); return clone(receipt.response);
      } catch (error) {
        const mapped = error instanceof BridgeError ? error : new BridgeError(502, "codex_app_server_error", "Desktop operation failed; inspect the native task before retrying.");
        receipt.pending = false; receipt.error = { ...mapped.body, messageId: request.messageId }; receipt.status = mapped.status;
        if (accepted) await this.emit("message.rejected", request, receipt.error);
        await this.save(); throw new BridgeError(mapped.status, mapped.body.error, mapped.body.detail, mapped.body.retryable, { ...mapped.body, messageId: request.messageId });
      }
    });
  }

  async register(request, { requireFork = false } = {}) {
    return this.once(request, async () => {
      if (!request.threadId) fail(400, "invalid_request", "threadId is required.");
      if (this.state.sessions[request.threadId]) fail(409, "adopt_conflict", "Task is already registered.", false, { threadId: request.threadId });
      const snapshot = await this.read(request.threadId, request.hostId);
      const cwd = this.checkWorkspace(snapshot, true);
      const parent = snapshot.thread.forkedFromId ?? snapshot.thread.forked_from_id ?? null;
      if (requireFork && !parent) fail(422, "adopt_failed", "Native fork parent is not verifiable; 该子任务仍未受 Bridge 管理。", false, { threadId: request.threadId });
      const session = this.makeSession(snapshot.thread, { hostId: request.hostId, cwd, alias: snapshot.thread.title ?? "Hermes 已有会话", parent });
      this.baseline(session, snapshot);
      this.state.sessions[session.threadId] = session; this.selectSession(session.threadId);
      await this.emit("session.adopted", { ...request, threadId: session.threadId }, { session: clone(session) });
      return { action: "session_adopted", session: clone(session) };
    });
  }

  makeSession(thread, { alias, cwd, hostId, parent = null } = {}) {
    return { threadId: thread.id, alias, cwd, hostId: hostId ?? null, bridge: this.config.bridgeName,
      machine: this.config.machine, owner: "hermes", sourceThreadId: parent, forkedFromId: parent,
      state: "idle", activeTurnId: null, createdAt: new Date().toISOString(), queue: [], pendingInputs: [], seen: {}, observedTurns: {}, turnMessages: {}, lastEventSeq: this.state.seq };
  }

  baseline(session, snapshot) {
    this.publicImageRelay.baseline(session, snapshot);
    session.seen = Object.fromEntries(publicItems(snapshot).map(i => [i.key, { text: i.text, hash: digest(i.text) }]));
    session.observedTurns = Object.fromEntries((snapshot.turns ?? []).map(t => [t.id, t.status]));
    this.markHistoryBaseline(session, snapshot);
    session.historyWindow = clone(snapshot.nativeHistoryWindow ?? null);
    const active = activeTurn(snapshot);
    session.activeTurnId = active?.id ?? null; session.state = active ? "running" : "idle";
  }

  touchSession(threadId, at = new Date().toISOString()) {
    const activity = this.state.resumeActivity ??= {};
    if (!activity[threadId] || Date.parse(at) > Date.parse(activity[threadId].at)) activity[threadId] = { at };
  }

  selectSession(threadId, { touch = false } = {}) {
    if (this.state.selectedThreadId !== threadId) {
      this.state.selectionEpoch++;
      if (this.state.sessions[threadId]) this.state.sessions[threadId].publicJournalStartAt = new Date().toISOString();
    }
    this.state.selectedThreadId = threadId;
    if (touch) this.touchSession(threadId);
  }

  markHistoryBaseline(session, snapshot) {
    session.historyBaselineAnchor = (snapshot.page?.order === "newest_first" ? snapshot.turns?.[0] : snapshot.turns?.at(-1))?.id ?? null;
    session.historyBaselineSeededTurns = Object.fromEntries((snapshot.turns ?? []).map(turn => [turn.id, true]));
  }

  seedOlderHistory(session, snapshot) {
    const turns = snapshot.page?.order === "newest_first" ? snapshot.turns ?? [] : [...(snapshot.turns ?? [])].reverse();
    const index = turns.findIndex(turn => turn.id === session.historyBaselineAnchor);
    if (index < 0) return;
    const seeded = session.historyBaselineSeededTurns ??= {};
    const older = turns.slice(index + 1).filter(turn => !seeded[turn.id]);
    // An adaptive window can widen later. History preceding the selection's
    // anchor is baseline, never a newly received reply/image to replay.
    const oldSnapshot = { ...snapshot, turns: older };
    const imageSeed = {}; this.publicImageRelay.baseline(imageSeed, oldSnapshot);
    Object.assign(session.seenImages ??= {}, imageSeed.seenImages);
    for (const item of publicItems(oldSnapshot)) session.seen[item.key] ??= { text: item.text, hash: digest(item.text) };
    for (const turn of older) { session.observedTurns[turn.id] ??= turn.status; seeded[turn.id] = true; }
  }

  async message(request, { requireSelectedTarget = false, requireSelectionEpoch, requireSourceTurnId } = {}) {
    if (typeof request.text !== "string" || !request.text.trim()) fail(400, "invalid_request", "text is required.");
    return this.once(request, async () => {
      if ((requireSelectedTarget && request.targetThreadId !== this.state.selectedThreadId) ||
          (requireSelectionEpoch != null && requireSelectionEpoch !== this.state.selectionEpoch)) {
        fail(409, "selected_target_changed", "Refresh /threads and use the Feishu-selected native chat; no fallback was used.", false,
          { threadId: request.targetThreadId, selectedThreadId: this.state.selectedThreadId });
      }
      let text = request.text.trim(), delivery = request.delivery, transformed = false;
      if (/^\/goal(?:\s|$)/.test(text)) {
        transformed = true;
        text = goalPrompt(text);
        if (!text) return { action: "help", text: BRIDGE_HELP };
      } else if (/^\/queue\s+/.test(text)) { text = text.replace(/^\/queue\s+/, ""); delivery = "queue"; transformed = true; }
      else if (text.startsWith("/") && !text.startsWith("//")) return this.command(request, text);
      const session = this.session(request.targetThreadId);
      const normalized = { ...request, delivery, targetThreadId: session.threadId, text: text.startsWith("//") ? text.slice(1) : transformed ? text : request.text,
        sourceTurnGuard: requireSourceTurnId ?? null,
        ...(request.source === "trainer" ? { trainerActivityAt: new Date().toISOString() } : {}) };
      const selectedAtStart = session.threadId === this.state.selectedThreadId, epoch = this.state.selectionEpoch;
      // Queue acceptance needs no guess about idle state. Only the scanner with
      // a fresh lifecycle observation may dispatch it.
      if (delivery === "queue" && !requireSourceTurnId) {
        session.queue.push(normalized);
        if (request.source === "trainer") this.touchSession(session.threadId, normalized.trainerActivityAt);
        await this.emit("message.queued", normalized, { position: session.queue.length, stateRequiredBeforeDispatch: true });
        return { action: "message_queued", session: clone(session) };
      }
      const independent = localHost(session.hostId) && this.config.nativeBusyDeliveryVerified &&
        (this.config.nativeStateIndependentDeliveryVerified || this.isSelfTarget(session)) && !requireSourceTurnId;
      const snapshot = independent ? { deliveryContract: { idleAndBusy: true } } : await this.deliveryState(session);
      if (selectedAtStart && (epoch !== this.state.selectionEpoch || session.threadId !== this.state.selectedThreadId)) fail(409, "selected_target_changed", "The selected chat changed during delivery validation; no message was sent.");
      const active = activeTurn(snapshot);
      // Native tools have no atomic expectedTurnId. Check immediately before
      // dispatch, reject stale questions; do not claim a native atomic guarantee.
      if (requireSourceTurnId && active?.id !== requireSourceTurnId) fail(409, "source_turn_ended", "The original question turn is no longer active; no answer was sent.");
      if (requireSelectionEpoch != null && (requireSelectionEpoch !== this.state.selectionEpoch || session.threadId !== this.state.selectedThreadId)) fail(409, "selected_target_changed", "The question binding changed during source validation; no answer was sent.");
      if (active || session.pendingInputs?.length || session.queue.length || session.steerQueue?.length) {
        if (delivery === "queue") {
          session.queue.push(normalized);
          if (request.source === "trainer") this.touchSession(session.threadId, normalized.trainerActivityAt);
          await this.emit("message.queued", normalized, { position: session.queue.length });
          return { action: "message_queued", session: clone(session) };
        }
        if (!this.config.nativeBusyDeliveryVerified || !localHost(session.hostId)) {
          fail(409, "writer_conflict", "Busy native delivery is not verified for this host; choose delivery:queue.", false,
            { threadId: session.threadId, activeTurnId: active?.id ?? null });
        }
        // Native acceptance, not transcript readback, releases the next write.
        // Drain an upgrade's unsent backlog before a newly arriving interjection.
        while (session.steerQueue?.length) {
          await this.dispatchQueued(session, session.steerQueue.shift(), snapshot);
        }
      }
      return this.dispatch(session, normalized, snapshot);
    });
  }

  async dispatch(session, request, beforeSnapshot) {
    const dispatchEpoch = this.state.selectionEpoch, selectedAtStart = session.threadId === this.state.selectedThreadId;
    const sourceThreadId = nativeSendSource(this.config, session);
    if (!sourceThreadId) fail(503, "bridge_not_ready", "The native sender task must be bound before plain-text delivery.");
    const snapshot = beforeSnapshot ?? await this.deliveryState(session);
    if (snapshot.deliveryContract && (!localHost(session.hostId) || !this.config.nativeBusyDeliveryVerified ||
        !(this.config.nativeStateIndependentDeliveryVerified || this.isSelfTarget(session)))) {
      fail(409, "writer_conflict", "State-independent native follow-up is not verified for this binding.");
    }
    const acceptedWithoutHistory = Boolean(snapshot.deliveryObservation || snapshot.deliveryContract);
    const active = activeTurn(snapshot);
    if (active && (!this.config.nativeBusyDeliveryVerified || !localHost(session.hostId))) {
      fail(409, "writer_conflict", "Busy native delivery is not verified for this host; choose delivery:queue.", false,
        { threadId: session.threadId, activeTurnId: active.id });
    }
    // Trainer-authorized, visible channel labels; original receipts/reports stay
    // untouched. Slash commands are handled before reaching this presentation.
    const label = this.config.messageSourceLabels === false ? null
      : request.source === "trainer" && this.state.feishuReceipts[request.messageId] ? "飞书"
      : request.source === "hermes" && request.origin !== "router" ? "超级溪流" : null;
    const prompt = label ? `${label}：${request.text}` : request.text;
    const pending = { messageId: request.messageId, inputHash: digest(prompt), dispatchedAt: Date.now(),
      sourceThreadId, kind: active ? "steer" : snapshot.deliveryContract ? "followup" : "start", expectedTurnId: active?.id ?? null,
      beforeItemKeys: publicItems(snapshot).map(item => item.key),
      beforeTurnIds: [...new Set([...Object.keys(session.observedTurns), ...(snapshot.turns ?? []).map(t => t.id)])] };
    if (!acceptedWithoutHistory) (session.pendingInputs ??= []).push(pending);
    const receipt = this.state.receipts[request.messageId];
    if (receipt) { receipt.nativeSourceThreadId = sourceThreadId; receipt.nativeInputHash = digest(prompt);
      receipt.nativeDispatch = { status: "dispatching", at: new Date().toISOString(), threadId: session.threadId }; }
    const feishuReceipt = this.state.feishuReceipts[request.messageId];
    if (acceptedWithoutHistory && feishuReceipt && !feishuReceipt.cleanupRequested) {
      // Native acceptance releases ingress, not the trainer's Typing indicator.
      // Persist a separate wait which never blocks queue/state-independent sends.
      feishuReceipt.awaitingPublicOutput = { threadId: session.threadId, bindingEpoch: dispatchEpoch,
        after: new Date(pending.dispatchedAt).toISOString(), turnId: active?.id ?? null };
    }
    // Persist correlation before the write; UUID/source metadata never enters the prompt.
    await this.save();
    try {
      if (request.delivery === "queue" && activeTurn(await this.deliveryState(session))) {
        fail(409, "queue_target_busy", "The queued target is running; no native write was attempted.", true);
      }
      if (request.sourceTurnGuard) {
        const latest = activeTurn(await this.deliveryState(session));
        if (latest?.id !== request.sourceTurnGuard) fail(409, "source_turn_ended", "The question turn ended before native dispatch; no answer was sent.");
      }
      if (selectedAtStart && (dispatchEpoch !== this.state.selectionEpoch || session.threadId !== this.state.selectedThreadId)) {
        fail(409, "selected_target_changed", "The binding changed before native dispatch; no message was sent.");
      }
    } catch (error) {
      if (receipt) receipt.nativeDispatch.status = "canceled";
      session.pendingInputs = (session.pendingInputs ?? []).filter(item => item !== pending);
      await this.save(); throw error;
    }
    const args = { threadId: session.threadId, prompt };
    if (session.hostId) args.hostId = session.hostId;
    const turns = snapshot.page?.order === "newest_first" ? snapshot.turns : [...(snapshot.turns ?? [])].reverse();
    try { await this.call("send_message_to_thread", args, { sourceThreadId, turnId: active?.id ?? snapshot.deliveryObservation?.turnId ?? turns?.[0]?.id }); }
    catch (error) {
      // A timeout may have inserted the input. Keep its correlation slot so a
      // later identical message cannot accidentally claim this uncertain write.
      pending.unknownOutcome = true;
      if (receipt) receipt.nativeDispatch.status = "unknown";
      await this.save(); throw error;
    }
    session.state = "running"; pending.dispatchedAt = Date.now();
    if (receipt) receipt.nativeDispatch.status = "accepted";
    if (request.source === "trainer") this.touchSession(session.threadId, request.trainerActivityAt);
    await this.emit("message.dispatched", request, { delivery: active ? "native-busy-followup" : snapshot.deliveryContract ? "native-followup-state-independent" : "native-followup", pendingTurnReadback: !acceptedWithoutHistory });
    if (this.notifier && request.source === "hermes" && request.origin !== "router" && session.threadId === this.state.selectedThreadId) void this.notifier.sendMessage(request.text,
      { title: `${this.config.bridgeName}-任务`, threadId: session.threadId, chatId: session.feishuChatId }).catch(() => {});
    return { action: "dispatch_pending", session: clone(session) };
  }

  async correlatePlainInput(session, items) {
    const groups = new Map();
    for (const pending of session.pendingInputs ?? []) {
      const key = JSON.stringify([pending.sourceThreadId, pending.inputHash]);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(pending);
    }
    const resolved = new Set(); const paired = [];
    const eligible = (pending, item) => !session.inputMessages?.[item.key] && item.kind === "delegated" &&
      item.nativeTool === "send_message_to_thread" && item.sourceThreadId === pending.sourceThreadId &&
      digest(item.text) === pending.inputHash && (pending.kind === "steer"
        ? !(pending.beforeItemKeys ?? []).includes(item.key) : !(pending.beforeTurnIds ?? []).includes(item.turnId));
    for (const group of groups.values()) {
      const matches = items.filter(item => group.some(pending => eligible(pending, item)));
      // Identical bursts are paired in native history FIFO only when the whole
      // visible cohort has an unambiguous one-to-one match. This never gates writes.
      if (matches.length < group.length) continue;
      if (matches.length > group.length || group.some((pending, index) => !eligible(pending, matches[index]))) {
        for (const pending of group) {
          resolved.add(pending.messageId);
          if (!pending.unknownOutcome) await this.emit("message.rejected", { targetThreadId: session.threadId, messageId: pending.messageId },
            { error: "dispatch_outcome_unknown", detail: "Native input readback is ambiguous. Read the Desktop task to recover; no write is automatically retried.", retryable: false });
        }
        continue;
      }
      for (const [index, pending] of group.entries()) {
        const item = matches[index]; resolved.add(pending.messageId);
        (session.inputMessages ??= {})[item.key] = pending.messageId;
        if (!pending.unknownOutcome) paired.push({ pending, item });
      }
    }
    for (const { pending, item } of paired.sort((a, b) => items.indexOf(a.item) - items.indexOf(b.item))) {
      session.turnMessages[item.turnId] ??= [];
      if (!session.turnMessages[item.turnId].includes(pending.messageId)) {
        session.turnMessages[item.turnId].push(pending.messageId);
        const receipt = this.state.receipts[pending.messageId];
        const steered = pending.kind === "steer" && item.turnId === pending.expectedTurnId;
        await this.emit(steered ? "turn.steered" : "turn.started",
          { ...receipt?.request, targetThreadId: session.threadId, messageId: pending.messageId },
          { delivery: steered ? "native-busy-followup" : "native-followup", expectedTurnId: pending.expectedTurnId ?? null }, { turnId: item.turnId });
      }
    }
    session.pendingInputs = (session.pendingInputs ?? []).filter(p => !resolved.has(p.messageId));
    await this.save();
  }

  async dispatchQueued(session, request, snapshot) {
    const receipt = this.state.receipts[request.messageId];
    // A crash after removing the queue entry must never silently lose/repeat it.
    if (receipt) receipt.pending = true;
    await this.save();
    try { await this.dispatch(session, request, snapshot); }
    catch (error) {
      if (request.delivery === "queue" && receipt?.nativeDispatch?.status === "canceled" &&
          ["queue_target_busy", "local_task_state_pending"].includes(error.body?.error)) {
        session.queue.unshift(request); receipt.nativeDispatch.status = "deferred";
        return; // Pre-write deferral only; unknown native writes never requeue.
      }
      const body = error.body ?? { error: "dispatch_failed", detail: "Queue dispatch failed.", retryable: false };
      if (receipt) { receipt.error = body; receipt.status = error.status ?? 502; }
      await this.emit("message.rejected", request, body);
    } finally { if (receipt) receipt.pending = false; await this.save(); }
  }

  async collectResumeCandidates() {
    const local = await this.resumeMetadata();
    let listing;
    try { listing = await this.call("list_threads", { limit: 50 }); }
    catch (error) { if (!local.available) throw error; listing = {}; }
    const result = await resumeCandidates(listing, {
      localTasks: local.tasks, activity: this.state.resumeActivity,
      readThread: threadId => this.call("read_thread", { threadId, hostId: "local", turnLimit: 1,
        includeOutputs: false, maxOutputCharsPerItem: 1 }),
      allowed: task => { try { this.checkWorkspace({ thread: task }); return true; } catch { return false; } }
    });
    result.discovery.localCatalogAvailable = local.available;
    return { candidates: result.tasks.map((task, index) => ({ index, threadId: task.id,
      alias: task.title ?? task.id, cwd: task.cwd, hostId: task.hostId ?? "local", updatedAt: task.updatedAt,
      lastActivityAt: task.lastActivityAt, metadataSource: task.metadataSource })), discovery: result.discovery };
  }

  async showResumeCandidates(catalog, query = "") {
    const lower = query.toLocaleLowerCase();
    this.state.resumeCandidates = catalog.candidates.filter(c => !query || c.alias.toLocaleLowerCase().includes(lower) || c.threadId.toLowerCase().includes(lower))
      .map((c, index) => ({ ...c, index }));
    this.state.resumeSnapshot = { version: 2, page: 0, query, discovery: catalog.discovery, createdAt: new Date().toISOString() };
    return this.showResumePage(0);
  }

  async showResumePage(page) {
    if (!this.state.resumeSnapshot || !Array.isArray(this.state.resumeCandidates)) fail(400, "resume_list_required", "请先发送 /resume 查看列表。");
    let visible;
    try { visible = resumePage(this.state.resumeCandidates, page); }
    catch { fail(400, "resume_page_out_of_range", "没有这一页；用 /resume 刷新或 /resume prev 返回上一页。"); }
    this.state.resumeSnapshot.page = page;
    const selected = this.state.sessions[this.state.selectedThreadId];
    const current = this.state.resumeCandidates.find(c => c.threadId === this.state.selectedThreadId);
    await this.save();
    return { action: "sessions_listed", sessions: clone(visible.sessions), resumeList: true, resumePage: visible.page,
      resumeQuery: this.state.resumeSnapshot.query, resumeDiscovery: clone(this.state.resumeSnapshot.discovery),
      currentSession: selected ? { threadId: selected.threadId, title: current?.alias ?? selected.alias ?? null, index: current?.index ?? null } : null };
  }

  async command(request, text) {
    const [rawCommand, ...parts] = text.split(/\s+/);
    const command = rawCommand === "/clear" ? "/new" : rawCommand;
    if (["/help", "/start", "/commands"].includes(command)) return { action: "help", text: BRIDGE_HELP };
    if (["/new", "/fork", "/stop", "/list", "/status"].includes(command) && parts.length) {
      fail(422, "command_arguments_not_supported", `${command} does not accept arguments.`);
    }
    if (command === "/list") return { action: "sessions_listed", sessions: Object.values(this.state.sessions).map(clone) };
    if (command === "/resume") {
      if (!parts.length) return this.showResumeCandidates(await this.collectResumeCandidates());
      const operation = parts[0].toLowerCase();
      if (["next", "prev", "page"].includes(operation)) {
        if (operation === "page" ? parts.length !== 2 || !/^[1-9]\d*$/.test(parts[1]) : parts.length !== 1) fail(400, "invalid_request", "使用 /resume next、/resume prev 或 /resume page 2。");
        return this.showResumePage(operation === "page" ? Number(parts[1]) - 1 : (this.state.resumeSnapshot?.page ?? 0) + (operation === "next" ? 1 : -1));
      }
      if (operation === "search") {
        const query = parts.slice(1).join(" ").trim();
        if (!query) fail(400, "invalid_request", "使用 /resume search 关键词。");
        return this.showResumeCandidates(await this.collectResumeCandidates(), query);
      }
      let candidate;
      if (parts.length === 1 && /^\d+$/.test(parts[0])) {
        const index = Number(parts[0]);
        if (Number.isSafeInteger(index)) candidate = this.state.resumeCandidates?.[index];
      } else {
        const name = parts.join(" ").replace(/^(["'])(.*)\1$/, "$2"), catalog = await this.collectResumeCandidates();
        const exact = catalog.candidates.filter(c => c.threadId === name || c.alias === name);
        if (exact.length === 1) candidate = exact[0];
        else return this.showResumeCandidates(catalog, name);
      }
      if (!candidate) fail(404, "thread_not_found", "该编号不在上次列表中；请发送 /resume 或 /resume search 关键词。");
      const snapshot = await this.selectionState(candidate);
      const cwd = this.checkWorkspace(snapshot, true);
      if (!this.state.sessions[candidate.threadId]) {
        const adopted = this.makeSession(snapshot.thread, { alias: candidate.alias, cwd, hostId: candidate.hostId,
          parent: snapshot.thread.forkedFromId ?? null });
        this.baseline(adopted, snapshot); this.state.sessions[candidate.threadId] = adopted;
        await this.emit("session.adopted", { ...request, threadId: candidate.threadId }, { session: clone(adopted) });
      }
      const session = this.session(candidate.threadId);
      if (this.state.selectedThreadId !== candidate.threadId) {
        this.publicImageRelay.baseline(session, snapshot);
        this.markHistoryBaseline(session, snapshot);
      }
      this.selectSession(candidate.threadId, { touch: true });
      await this.emit("session.selected", { ...request, threadId: candidate.threadId }, { session: clone(session) });
      return { action: "session_selected", session: clone(session) };
    }
    if (command === "/switch") {
      if (parts.length !== 1) fail(400, "invalid_request", "Use /switch <threadId or number>.");
      const list = Object.values(this.state.sessions);
      const id = list.find(s => s.threadId === parts[0])?.threadId ?? (/^\d+$/.test(parts[0]) ? list[Number(parts[0]) - 1]?.threadId : null);
      if (!id) fail(404, "thread_not_found", "Switch target was not found.");
      const session = this.session(id);
      if (this.state.selectedThreadId !== id) {
        const snapshot = await this.selectionState(session); this.checkWorkspace(snapshot, true);
        this.publicImageRelay.baseline(session, snapshot);
        this.markHistoryBaseline(session, snapshot);
      }
      this.selectSession(id, { touch: true });
      await this.emit("session.selected", { ...request, threadId: id }, { session: clone(session) });
      return { action: "session_selected", session: clone(session) };
    }
    if (command === "/status") return { action: "session_status", session: clone(this.session(request.targetThreadId)), health: this.health() };
    if (command === "/stop") {
      const session = this.session(request.targetThreadId);
      const snapshot = await this.read(session.threadId, session.hostId);
      if (!activeTurn(snapshot)) return { action: "turn_already_idle", session: clone(session) };
      fail(501, "capability_unavailable", "Native interrupt is not exposed by the Desktop task tools. Use the Stop button in that Codex Desktop chat; no stop was simulated.", false, { threadId: session.threadId, activeTurnId: activeTurn(snapshot).id });
    }
    if (!["/new", "/fork"].includes(command)) fail(422, "unknown_command", `Unsupported Bridge command: ${command}`);
    const alias = `Hermes 会话 #${String(this.state.nextName++).padStart(3, "0")}`;
    await this.save();
    let response; let parent = null; let hostId = null;
    if (command === "/new") {
      if (!this.config.projectId) fail(503, "bridge_not_ready", "A Desktop projectId is required for /new.");
      response = await this.call("create_thread", { title: alias,
        prompt: "这是 Hermes Bridge 创建的 Codex 原生会话。请只回复 HERMES_READY，等待后续任务；不要调用工具或修改文件。",
        target: { type: "project", projectId: this.config.projectId, environment: { type: "local" } } });
    } else {
      parent = request.sourceThreadId ?? this.session(request.targetThreadId).threadId;
      hostId = this.state.sessions[parent]?.hostId ?? null;
      if (hostId && hostId !== "local") fail(501, "capability_unavailable", "The native fork tool has no remote hostId contract.");
      const source = await this.read(parent, hostId); this.checkWorkspace(source);
      const active = activeTurn(source);
      if (active) fail(409, "source_turn_active", "Wait for the source task to finish or stop it in Codex Desktop before /fork.", true, { threadId: parent, activeTurnId: active.id });
      response = await this.call("fork_thread", { threadId: parent, environment: { type: "same-directory" } });
    }
    if (!response?.threadId) fail(502, "codex_app_server_error", "Desktop did not return a native threadId.");
    const snapshot = await this.read(response.threadId, response.hostId);
    const cwd = this.checkWorkspace(snapshot, true);
    const session = this.makeSession(snapshot.thread, { alias, cwd, parent, hostId: response.hostId });
    this.baseline(session, snapshot);
    this.state.sessions[session.threadId] = session; this.selectSession(session.threadId, { touch: true });
    try { await this.call("set_thread_title", { threadId: session.threadId, title: alias }); }
    catch { session.state = "error"; session.titlePending = true; }
    const type = command === "/new" ? "session.created" : "session.forked";
    await this.emit(type, { ...request, threadId: session.threadId }, { session: clone(session) });
    return { action: command === "/new" ? "session_created" : "session_forked", session: clone(session) };
  }

  async scan() {
    if (this.pollRunning || this.closed || this.handoffPaused) return;
    this.pollRunning = true;
    try {
      const ids = Object.keys(this.state.sessions);
      if (this.config.operatingMode === "feishu-local") ids.sort((a, b) => Number(b === this.state.selectedThreadId) - Number(a === this.state.selectedThreadId));
      for (const id of ids) {
        if (this.closed || this.handoffPaused) break;
        const registered = this.session(id);
        if (this.config.operatingMode === "feishu-local" && id !== this.state.selectedThreadId &&
            !registered.queue?.length && !registered.steerQueue?.length && !registered.pendingInputs?.length) continue;
        // A slow read-only poll must not hold the native ingress writer lock.
        const hostId = this.state.sessions[id]?.hostId;
        const imageBindingEpoch = this.state.selectionEpoch;
        let snapshot;
        (this.session(id).relayStatus ??= {}).lastAttemptAt = new Date().toISOString();
        if (id === this.state.selectedThreadId && localHost(hostId) && (this.isSelfTarget(registered) || this.tools.some(t => t.name === "wait_threads"))) {
          try { await this.scanPublicJournal(id, imageBindingEpoch); continue; }
          catch (error) {
            if (this.isSelfTarget(registered)) {
              await this.serial(async () => {
                const pending = error.body?.error === "local_task_state_pending" && registered.taskJournalStatus?.pendingBytes > 0;
                const unconfirmed = Object.values(this.state.publicTextOutbox ?? {}).some(e => e.threadId === id && e.bindingEpoch === imageBindingEpoch && isUnconfirmedText(e));
                const unconfirmedImage = Object.values(this.state.autoImages ?? {}).some(e => e.threadId === id && e.bindingEpoch === imageBindingEpoch && /unknown/.test(e.error ?? ""));
                registered.relayStatus = { ...registered.relayStatus, mode: "desktop-public-journal",
                  lastError: unconfirmed ? "feishu_text_send_unconfirmed" : unconfirmedImage ? "feishu_public_send_unconfirmed" : pending ? null : "self_task_journal_unavailable", journalError: pending ? null : "self_task_journal_unavailable",
                  lastSuccessAt: pending ? new Date().toISOString() : registered.relayStatus.lastSuccessAt,
                  journal: { ...registered.taskJournalStatus, caughtUp: false } };
                await this.save();
              });
              continue; // Never replace unknown self state with a huge history read.
            }
            this.session(id).relayStatus.journalError = /^[a-z_]+$/.test(error.code ?? "") ? error.code : "public_journal_unavailable";
          }
        }
        try { snapshot = await this.read(id, hostId); }
        catch (error) {
          if (error.body?.error === "native_single_turn_too_large" && id === this.state.selectedThreadId &&
              localHost(hostId) && this.tools.some(tool => tool.name === "wait_threads")) {
            try { await this.scanLimitedProgress(id, imageBindingEpoch); continue; }
            catch { /* Keep the real history error visible if progress is also unavailable. */ }
          }
          await this.relayFailed(id, error); continue;
        }
        try { await this.serial(async () => {
          const session = this.session(id);
          this.seedOlderHistory(session, snapshot);
          const items = publicItems(snapshot);
          this.publicImageRelay.observe(session, snapshot, imageBindingEpoch);
          await this.correlatePlainInput(session, items);
          for (const [index, item] of items.entries()) {
            const messageId = session.inputMessages?.[item.key];
            if (messageId && items.slice(index + 1).some(later => later.turnId === item.turnId &&
              later.kind === "assistant" && session.deliveredProgress?.[later.key])) this.finishFeishuInput(messageId);
          }
          for (const item of items) {
            const old = session.seen[item.key];
            if (old?.hash === digest(item.text)) continue;
            const correlation = item.text.match(/^\[Hermes Bridge messageId=([0-9a-f-]+) source=(trainer|hermes)\]\n/i);
            const known = correlation && this.state.receipts[correlation[1]];
            const boundPlain = item.kind === "delegated" && [session.inputMessages?.[item.key]].some(messageId => {
              const receipt = this.state.receipts[messageId];
              const text = receipt?.request.text;
              return text != null && item.sourceThreadId === (receipt.nativeSourceThreadId ?? this.config.nativeSenderThreadId) &&
                item.text === (text.trim().startsWith("//") ? text.trim().slice(1) : text);
            });
            const unconfirmedPlain = (session.pendingInputs ?? []).some(pending => item.kind === "delegated" &&
              item.nativeTool === "send_message_to_thread" && item.sourceThreadId === pending.sourceThreadId &&
              digest(item.text) === pending.inputHash && (pending.kind === "steer"
                ? !(pending.beforeItemKeys ?? []).includes(item.key) : !(pending.beforeTurnIds ?? []).includes(item.turnId)));
            const legacy = !boundPlain && known && item.kind === "delegated" && item.nativeTool === "send_message_to_thread" &&
              item.sourceThreadId === this.config.nativeSenderThreadId && !session.pendingInputs?.length &&
              (known.response?.session?.threadId ?? known.request.targetThreadId) === id &&
              item.text === `[Hermes Bridge messageId=${correlation[1]} source=${known.request.source}]\n${known.request.text}` &&
              !Object.entries(session.turnMessages).some(([turnId, ids]) => turnId !== item.turnId && ids.includes(correlation[1]));
            if (legacy) {
              const messageId = correlation[1];
              session.turnMessages[item.turnId] ??= [];
              if (!session.turnMessages[item.turnId].includes(messageId)) {
                session.turnMessages[item.turnId].push(messageId);
                const receipt = this.state.receipts[messageId];
                await this.emit("turn.started", { ...receipt?.request, targetThreadId: id, messageId }, {}, { turnId: item.turnId });
              }
            } else if (!boundPlain && !unconfirmedPlain && (item.kind === "trainer" || item.kind === "delegated")) {
              const messageId = randomUUID();
              await this.emit("message.observed", { targetThreadId: id, messageId, source: "trainer" }, { text: item.text, origin: "desktop-native-composer", alreadyDispatched: true }, { turnId: item.turnId });
            } else if (item.kind === "assistant") {
              const delta = old && item.text.startsWith(old.text) ? item.text.slice(old.text.length) : item.text;
              const ids = session.turnMessages[item.turnId] ?? [];
              await this.emit(item.final ? "item.completed" : "item.delta", { targetThreadId: id, source: "codex", messageId: ids[0] ?? null },
                { text: delta, snapshot: item.text, phase: item.final ? "final_answer" : "commentary", messageIds: ids }, { turnId: item.turnId });
              const inlineMedia = splitPublicMarkdown(item.final ? item.text : delta).some(part => part.type === "image");
              if (inlineMedia && this.notifier && id === this.state.selectedThreadId) {
                this.enqueuePublicText(session, item.turnId, item.key, item.final ? item.text : delta, imageBindingEpoch, item.final, [...ids]);
              } else if (item.final) {
                if (this.notifier && id === this.state.selectedThreadId) void this.notifier.sendMessage(item.text,
                  { title: `${this.config.bridgeName}-回复`, threadId: id, chatId: session.feishuChatId }).catch(() => {});
              } else if (this.notifier && id === this.state.selectedThreadId && this.config.feishu.streamEnabled !== false) {
                if (!this.feishuBeats.has(id)) this.feishuBeats.set(id, new FeishuStreamBeat(this.notifier,
                  { threadId: id, chatId: session.feishuChatId, bridgeName: this.config.bridgeName }));
                const beat = this.feishuBeats.get(id); beat.chatId = session.feishuChatId;
                const messageIds = [...ids];
                beat.feedChunk({ type: "text-delta", text: delta });
                beat.flush(() => this.serial(async () => {
                  (session.deliveredProgress ??= {})[item.key] = true;
                  for (const messageId of messageIds) this.finishFeishuInput(messageId);
                  await this.save();
                }));
              }
            }
            session.seen[item.key] = { text: item.text, hash: digest(item.text) };
          }
          for (const turn of snapshot.turns ?? []) {
            const ids = session.turnMessages[turn.id] ?? [];
            const uncompleted = ids.filter(messageId => !session.completedMessages?.[messageId]);
            if (terminal(turn.status) && (session.observedTurns[turn.id] !== turn.status || uncompleted.length)) {
              await this.emit("turn.completed", { targetThreadId: id, source: "codex", messageId: session.turnMessages[turn.id]?.[0] ?? null },
                { status: turn.status, messageIds: session.turnMessages[turn.id] ?? [] }, { turnId: turn.id });
              session.completedMessages ??= {};
              for (const messageId of ids) session.completedMessages[messageId] = true;
            }
            session.observedTurns[turn.id] = turn.status;
          }
          const active = activeTurn(snapshot);
          session.activeTurnId = active?.id ?? null; session.state = active || session.pendingInputs?.length ? "running" : "idle";
          for (const pending of [...(session.pendingInputs ?? [])]) {
            if (Date.now() - pending.dispatchedAt <= this.config.turnTimeoutMs) continue;
            session.pendingInputs = session.pendingInputs.filter(p => p !== pending);
            if (!pending.unknownOutcome) await this.emit("message.rejected", { targetThreadId: id, messageId: pending.messageId }, { error: "dispatch_outcome_unknown", detail: "Native follow-up was accepted but its turn could not be confirmed; inspect the Desktop task. No automatic retry.", retryable: false });
          }
          const readySteer = session.steerQueue?.length && (!active || (this.config.nativeBusyDeliveryVerified && localHost(session.hostId)));
          if (!this.handoffPaused && readySteer) {
            while (session.steerQueue.length && !this.handoffPaused) await this.dispatchQueued(session, session.steerQueue.shift(), snapshot);
          } else if (!this.handoffPaused && !active && !session.pendingInputs?.length && session.queue.length) {
            await this.dispatchQueued(session, session.queue.shift(), snapshot);
          }
          session.relayStatus = { ...session.relayStatus, mode: "full-history", lastSuccessAt: new Date().toISOString(), lastError: null, failures: 0 };
          await this.save();
        }); } catch (error) { await this.relayFailed(id, error); }
      }
      this.publicImageRelay.kick();
    } finally { this.pollRunning = false; }
  }

  async relayFailed(id, error) {
    return this.serial(async () => {
      const session = this.session(id), status = session.relayStatus ??= {};
      session.state = "error"; status.failures = (status.failures ?? 0) + 1;
      status.lastError = ["native_snapshot_too_large", "native_single_turn_too_large", "native_read_rejected", "native_snapshot_incomplete", "desktop_integration_unavailable"]
        .includes(error?.body?.error) ? error.body.error : "native_relay_failed";
      status.lastFailureAt = new Date().toISOString(); await this.save();
    });
  }

  async scanPublicJournal(id, epoch) {
    const session = this.session(id);
    await this.publicJournal.locate(id, session.publicJournalCheckpoint?.file);
    const observation = await this.deliveryState(session);
    const progress = observation.deliveryObservation;
    if (!progress) throw new Error("public_journal_status_unavailable");
    const batch = await this.publicJournal.read(session, session.publicJournalCheckpoint,
      { turnId: progress.turnId, epoch, startAt: session.publicJournalStartAt });
    await this.serial(async () => {
      if (this.closed || this.handoffPaused || id !== this.state.selectedThreadId || epoch !== this.state.selectionEpoch) return;
      for (const { turnId, item, timestamp } of batch.events) {
        // This projection is consumed ONLY by the public-image filter. It is
        // never used as history, input correlation, or a turn-control snapshot.
        if (item.type !== "agentMessage") {
          this.publicImageRelay.observe(session, { turns: [{ id: turnId, items: [item] }] }, epoch); continue;
        }
        const key = `${turnId}/${item.id}`, old = session.seen[key];
        if (old?.hash === digest(item.text)) continue;
        const final = ["final", "final_answer"].includes(item.phase);
        const delta = old && item.text.startsWith(old.text) ? item.text.slice(old.text.length) : item.text;
        session.seen[key] = { text: item.text, hash: digest(item.text) };
        if (this.notifier && (final || this.config.feishu.streamEnabled !== false)) {
          this.enqueuePublicText(session, turnId, key, final ? item.text : delta, epoch, final,
            this.typingInputsForOutput(session, timestamp));
        }
        await this.emit(final ? "item.completed" : "item.delta", { targetThreadId: id, source: "codex" },
          { text: delta, snapshot: item.text, phase: item.phase, readSource: "desktop-public-journal" }, { turnId });
      }
      session.publicJournalCheckpoint = batch.checkpoint;
      session.activeTurnId = activeTurn(observation)?.id ?? null;
      session.state = session.activeTurnId ? "running" : "idle";
      this.finishTerminalTyping(session, progress);
      const unknownText = Object.values(this.state.publicTextOutbox ?? {}).some(e => e.threadId === id && e.bindingEpoch === epoch && isUnconfirmedText(e) && (e.status === "unknown" || !this.publicTextRunning));
      const unknownImage = Object.values(this.state.autoImages ?? {}).some(e => e.threadId === id && e.bindingEpoch === epoch && /unknown/.test(e.error ?? ""));
      session.relayStatus = { ...session.relayStatus, mode: "desktop-public-journal", journalError: null,
        lastSuccessAt: new Date().toISOString(), lastError: unknownText ? "feishu_text_send_unconfirmed" : unknownImage ? "feishu_public_send_unconfirmed" : null, failures: 0,
        journal: { caughtUp: batch.caughtUp, offset: batch.checkpoint.offset, pendingBytes: batch.pendingBytes, bytesRead: batch.bytesRead } };
      const readySteer = session.steerQueue?.length && (!session.activeTurnId || this.config.nativeBusyDeliveryVerified);
      if (readySteer && !this.handoffPaused) await this.dispatchQueued(session, session.steerQueue.shift(), observation);
      else if (!session.activeTurnId && session.queue.length && !this.handoffPaused) await this.dispatchQueued(session, session.queue.shift(), observation);
      await this.save(); // Public cursor + UUID outbox, before any network write.
    });
  }

  nextPublicOutputOrder() { return this.state.publicOutputSequence = (this.state.publicOutputSequence ?? 0) + 1; }

  typingInputsForOutput(session, timestamp) {
    const at = Date.parse(timestamp);
    if (!Number.isFinite(at)) return []; // Unknown event age cannot dismiss a newer input.
    return Object.entries(this.state.feishuReceipts).filter(([messageId, receipt]) => {
      const wait = receipt.awaitingPublicOutput;
      return !receipt.cleanupRequested && wait?.threadId === session.threadId &&
        wait.bindingEpoch === this.state.selectionEpoch && at >= Date.parse(wait.after) &&
        this.state.receipts[messageId]?.nativeDispatch?.status === "accepted";
    }).map(([messageId]) => messageId);
  }

  finishTerminalTyping(session, progress) {
    for (const [messageId, receipt] of Object.entries(this.state.feishuReceipts)) {
      const wait = receipt.awaitingPublicOutput;
      if (receipt.cleanupRequested || wait?.threadId !== session.threadId || wait.bindingEpoch !== this.state.selectionEpoch) continue;
      if (!terminal(progress.turnStatus)) { wait.turnId ??= progress.turnId; continue; }
      if (wait.turnId !== progress.turnId) continue; // A previous idle turn is not this input's completion.
      const unsent = Object.values(this.state.publicTextOutbox ?? {}).some(entry => entry.threadId === session.threadId &&
        entry.bindingEpoch === wait.bindingEpoch && entry.messageIds?.includes(messageId) && (entry.status === "pending" || isUnconfirmedText(entry)));
      if (!unsent) this.finishFeishuInput(messageId, "native_terminal");
    }
  }

  enqueuePublicText(session, turnId, key, text, epoch, final, messageIds = []) {
    const inlineSeen = new Set();
    for (const [index, part] of splitPublicMarkdown(text).entries()) {
      if (part.type === "image") {
        const identity = /^[a-z]:[\\/]/i.test(part.imagePath) ? path.win32.normalize(part.imagePath).toLowerCase() : path.normalize(part.imagePath);
        if (inlineSeen.has(identity)) continue; inlineSeen.add(identity);
        this.publicImageRelay.observe(session, { turns: [{ id: turnId, items: [{ type: "imageView", id: `${key}:inline:${index}`, path: part.imagePath }] }] }, epoch, { inline: true });
      } else {
        const uuid = randomUUID();
        (this.state.publicTextOutbox ??= {})[uuid] = { uuid, key, part: index, threadId: session.threadId,
          bindingEpoch: epoch, chatId: session.feishuChatId, text: part.text, order: this.nextPublicOutputOrder(),
          title: `${this.config.bridgeName}-${final ? "回复" : "输出"}`, messageIds, status: "pending", at: new Date().toISOString() };
      }
    }
    // 入队即触发发送。CLI 模式下没有图片中继来 kick，纯文字会永远停在 pending。
    if (!this.closed && !this.handoffPaused) void this.drainPublicTexts();
  }

  async drainPublicTexts() {
    if (this.publicTextRunning) return this.publicTextTask;
    if (this.closed || this.handoffPaused || !this.notifier) return;
    if (this.publicImageRelay.running) { await this.publicImageRelay.task; if (this.closed || this.handoffPaused) return; }
    this.publicTextRunning = true;
    const work = (async () => {
      for (let count = 0; count < 8 && !this.closed && !this.handoffPaused; count++) {
        let entry, imageEntry; const notifier = this.notifier;
        await this.serial(async () => {
          const current = e => e.threadId === this.state.selectedThreadId && e.bindingEpoch === this.state.selectionEpoch;
          for (const old of Object.values(this.state.publicTextOutbox ?? {})) {
            if (old.status === "pending" && !current(old)) { old.status = "canceled"; delete old.text; }
          }
          const texts = Object.values(this.state.publicTextOutbox ?? {}).filter(e => e.status === "pending" || current(e) && isUnconfirmedText(e)).map(e => ({ e, image: false }));
          const images = Object.values(this.state.autoImages ?? {}).filter(e => e.status === "pending" || current(e) && e.status === "failed" && /unknown/.test(e.error ?? "")).map(e => ({ e, image: true }));
          const next = [...texts, ...images].sort((a, b) => (a.e.order ?? -1) - (b.e.order ?? -1))[0];
          if (next?.image) imageEntry = next.e;
          else if (next?.e.status === "pending") entry = next.e;
          if (entry) { entry.status = "dispatching"; await this.save(); }
        });
        if (imageEntry) {
          if (imageEntry.status !== "pending") return; // Never pass an unknown send barrier.
          await this.publicImageRelay.drain(imageEntry);
          if (imageEntry.status === "pending" || /unknown/.test(imageEntry.error ?? "")) return;
          continue;
        }
        if (!entry) return;
        if (this.closed || this.handoffPaused) {
          // No send began. Preserve the same pending UUID across a handoff,
          // instead of treating a temporary pause as a binding cancellation.
          await this.serial(async () => { entry.status = "pending"; await this.save(); });
          return;
        }
        const valid = () => !this.closed && !this.handoffPaused && this.notifier === notifier &&
          entry.threadId === this.state.selectedThreadId && entry.bindingEpoch === this.state.selectionEpoch;
        let status = "canceled", sentMessageId = null;
        if (valid()) {
          try {
            const result = await notifier.sendMessage(entry.text, { threadId: entry.threadId, chatId: entry.chatId,
              title: entry.title, uuid: entry.uuid, validateTarget: valid,
              onDelivery: diagnostic => this.serial(async () => {
                (entry.deliveryEvidence ??= []).push(diagnostic);
                if (entry.deliveryEvidence.length > 64) entry.deliveryEvidence.shift();
                if (diagnostic.phase === "dispatching") entry.firstAttemptAt ??= diagnostic.at;
                await this.save();
              }) });
            status = result?.code === 0 ? "sent" : "unknown";
            if (status === "sent") sentMessageId = result.data?.message_id ?? null;
          }
          catch { status = "unknown"; }
        }
        await this.serial(async () => {
          entry.status = status; entry.at = new Date().toISOString();
          if (status !== "unknown") delete entry.text;
          if (sentMessageId) entry.feishuMessageId = sentMessageId;
          this.session(entry.threadId).publicTextDelivery = { key: entry.key, uuid: entry.uuid, status, at: entry.at };
          if (status === "sent") {
            (this.session(entry.threadId).deliveredProgress ??= {})[entry.key] = true;
            for (const id of entry.messageIds ?? []) this.finishFeishuInput(id, "public_output_delivered");
          }
          if (status === "unknown") this.session(entry.threadId).relayStatus.lastError = "feishu_text_send_unconfirmed";
          const settled = Object.values(this.state.publicTextOutbox).filter(e => ["sent", "canceled"].includes(e.status));
          for (const old of settled.slice(0, Math.max(0, settled.length - 128))) delete this.state.publicTextOutbox[old.uuid];
          await this.save();
        });
      }
    })();
    this.publicTextTask = work;
    try { await this.track(work); }
    catch { console.error("[bridge] Public text outbox persistence failed; unknown sends are not replayed."); }
    finally { this.publicTextRunning = false; this.publicTextTask = null; }
  }

  async abandonTextReceipt(body) {
    if (!body || Object.keys(body).some(k => !["uuid", "threadId", "bindingEpoch", "reason"].includes(k)) ||
        !UUID4.test(body.uuid ?? "") || !Number.isSafeInteger(body.bindingEpoch) ||
        typeof body.reason !== "string" || !body.reason.trim() || body.reason.length > 500) {
      fail(400, "invalid_request", "An exact original UUID, binding epoch and trainer decision reason are required.");
    }
    return this.serial(async () => {
      const entry = this.state.publicTextOutbox?.[body.uuid];
      if (!entry || entry.status !== "unknown" || entry.threadId !== body.threadId ||
          entry.threadId !== this.state.selectedThreadId || entry.bindingEpoch !== body.bindingEpoch ||
          entry.bindingEpoch !== this.state.selectionEpoch) {
        fail(409, "receipt_review_conflict", "Review only an unknown text in the unchanged current binding; no send was attempted.");
      }
      const duplicate = isAbandonedText(entry), previous = entry.review;
      if (!duplicate) {
        entry.review = { decision: "abandon", actor: "trainer", uuid: entry.uuid, threadId: entry.threadId,
          bindingEpoch: entry.bindingEpoch, reason: body.reason, at: new Date().toISOString() };
        try { await this.save(); }
        catch (error) { if (previous === undefined) delete entry.review; else entry.review = previous; throw error; }
      }
      return { uuid: entry.uuid, originalStatus: entry.status, resolution: "abandoned_by_trainer", duplicate,
        replayed: false, markedDelivered: false };
    });
  }

  async scanLimitedProgress(id, epoch) {
    const previous = this.nativeProgressPolls.get(id);
    if (previous && Date.now() - previous.at < 5000) return;
    this.nativeProgressPolls.set(id, { at: Date.now(), cursor: previous?.cursor ?? null });
    const inspect = async cursor => publicProgress(await this.call("wait_threads", { targets: [{ threadId: id, hostId: "local",
      ...(cursor ? { afterCursor: cursor } : {}) }], timeoutMs: 0 }), id);
    let progress;
    try { progress = await inspect(previous?.cursor); }
    catch (error) {
      if (!previous?.cursor) throw error;
      this.nativeProgressPolls.set(id, { at: Date.now(), cursor: null });
      progress = await inspect(null); // Read-only reset after a stale native cursor.
    }
    let delivery = null, accepted = false;
    await this.serial(async () => {
      if (this.closed || this.handoffPaused || this.state.selectedThreadId !== id || this.state.selectionEpoch !== epoch) return;
      accepted = true;
      const session = this.session(id), status = session.relayStatus ??= {};
      session.state = "error"; // Full-history/input/image synchronization is still unavailable.
      session.activeTurnId = terminal(progress.turnStatus) ? null : progress.turnId;
      Object.assign(status, { mode: "latest-public-message", lastProgressAt: new Date().toISOString(), lastError: "native_single_turn_too_large" });
      const item = progress.message, key = item && `${item.turnId}/${item.id}`;
      if (item?.text.trim() && session.seen[key]?.hash !== digest(item.text)) {
        const old = session.seen[key];
        const delta = old && item.text.startsWith(old.text) ? item.text.slice(old.text.length) : item.text;
        const ids = session.turnMessages[item.turnId] ?? [];
        await this.emit(item.final ? "item.completed" : "item.delta", { targetThreadId: id, source: "codex", messageId: ids[0] ?? null },
          { text: delta, snapshot: item.text, phase: item.final ? "final_answer" : "commentary", messageIds: ids,
            readSource: "native-progress", historyLimited: true }, { turnId: item.turnId });
        session.seen[key] = { text: item.text, hash: digest(item.text) };
        if (this.notifier && (item.final || this.config.feishu.streamEnabled !== false)) {
          delivery = { key, uuid: randomUUID(), text: item.final ? item.text : delta, threadId: id, chatId: session.feishuChatId,
            title: `${this.config.bridgeName}-${item.final ? "回复" : "输出"}（历史受限）`, notifier: this.notifier };
          session.limitedTextDelivery = { key, uuid: delivery.uuid, status: "dispatching", at: new Date().toISOString() };
          (session.limitedTextReceipts ??= {})[delivery.uuid] = session.limitedTextDelivery;
          const settled = Object.values(session.limitedTextReceipts).filter(receipt => ["sent", "canceled"].includes(receipt.status));
          for (const receipt of settled.slice(0, Math.max(0, settled.length - 128))) delete session.limitedTextReceipts[receipt.uuid];
        }
      }
      // Save the seen item and intent BEFORE sending. Unknown sends never replay.
      await this.save();
    });
    if (!accepted) return;
    this.nativeProgressPolls.set(id, { at: Date.now(), cursor: progress.cursor });
    if (!delivery) return;
    let outcome = "canceled";
    if (!this.closed && !this.handoffPaused && this.state.selectedThreadId === id && this.state.selectionEpoch === epoch && this.notifier === delivery.notifier) {
      try { outcome = (await delivery.notifier.sendMessage(delivery.text, { threadId: delivery.threadId,
        chatId: delivery.chatId, title: delivery.title, uuid: delivery.uuid,
        validateTarget: () => !this.closed && !this.handoffPaused && this.state.selectedThreadId === id &&
          this.state.selectionEpoch === epoch && this.notifier === delivery.notifier }))?.code === 0 ? "sent" : "unknown"; }
      catch { outcome = "unknown"; }
    }
    await this.serial(async () => {
      const session = this.session(id), receipt = session.limitedTextReceipts?.[delivery.uuid];
      if (receipt) {
        receipt.status = outcome; receipt.at = new Date().toISOString();
        if (session.limitedTextDelivery?.uuid === delivery.uuid) session.limitedTextDelivery = receipt;
        await this.save();
      }
    });
    // Do not correlate/replay inputs, dispatch queues, mark turns completed or
    // discover images from this intentionally incomplete native snapshot.
  }

  async receiveFeishu(event) {
    if (event.chat_type !== "p2p" || event.sender_type !== "user" || !["text", "image"].includes(event.message_type) || !event.content?.trim()) return;
    if (!this.config.feishu.trainerOpenIdWhitelist?.includes(event.open_id)) return;
    // Stable UUIDv4-shaped digest makes duplicate WS delivery idempotent across restart.
    const hex = createHash("sha256").update(`feishu:${event.message_id}`).digest("hex");
    const messageId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
    if (this.feishuInputs.has(messageId)) return { ...await this.feishuInputs.get(messageId), duplicate: true };
    const task = this.handleFeishuInput(event, messageId);
    this.feishuInputs.set(messageId, task);
    try { return await task; } finally { this.feishuInputs.delete(messageId); }
  }

  inboxSerial(fn) {
    const task = this.inboxTail.then(fn); this.inboxTail = task.catch(() => {}); return task;
  }

  async saveInbox() {
    const temp = `${this.inboxPath}.${randomUUID()}.tmp`;
    await writeFile(temp, JSON.stringify(this.inbox), { mode: 0o600 });
    for (let attempt = 0; ; attempt++) {
      try { await rename(temp, this.inboxPath); return; }
      catch (error) {
        if (!["EPERM", "EBUSY", "EACCES"].includes(error.code) || attempt >= 7) throw error;
        await new Promise(resolve => setTimeout(resolve, 20 * (attempt + 1)));
      }
    }
  }

  async enqueueFeishu(event) {
    if (this.closed) throw new Error("ingress_quiescing");
    if (event.chat_type !== "p2p" || event.sender_type !== "user" || !["text", "image"].includes(event.message_type) ||
        !event.message_id || !event.content?.trim() || !this.config.feishu.trainerOpenIdWhitelist?.includes(event.open_id)) return;
    // Separate from the native writer tail: a slow native send must not prevent
    // a later WS event from being durably received and acknowledged promptly.
    await this.inboxSerial(async () => {
      if (!this.inbox.entries[event.message_id]) {
        this.inbox.entries[event.message_id] = { ...structuredClone(event),
          ...(event.message_type === "image" ? { bridgeImageThreadId: this.state.selectedThreadId } : {}) };
      }
      // Even an in-memory duplicate may follow a failed previous disk write.
      await this.saveInbox();
    });
    this.runInbox(this.inbox.entries[event.message_id]);
  }

  runInbox(event) {
    if (!event || this.closed || this.handoffPaused || this.inboxRunning.has(event.message_id)) return;
    this.inboxRunning.add(event.message_id);
    const task = this.receiveFeishu(event).catch(() => {
      console.error("[bridge] Durable Feishu input rejected; inspect message terminal events.");
      return null;
    }).then(async result => {
      if (!result) {
        // Never discard a received input on an IO/early handling failure. Only
        // a saved logical terminal (including unknown native outcome) closes it.
        const hex = createHash("sha256").update(`feishu:${event.message_id}`).digest("hex");
        const id = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
        const persisted = JSON.parse(await readFile(this.statePath, "utf8"));
        if (!persisted.receipts?.[id]?.error && !persisted.receipts?.[id]?.response) return;
      }
      return this.inboxSerial(async () => {
        delete this.inbox.entries[event.message_id]; await this.saveInbox();
      });
    }).finally(() => this.inboxRunning.delete(event.message_id));
    void this.track(task).catch(() => console.error("[bridge] Feishu inbox completion could not be saved; its UUID must not be replaced."));
  }

  async handleFeishuInput(event, messageId) {
    const imageThreadId = event.message_type === "image"
      ? this.state.feishuReceipts[messageId]?.imageThreadId ?? event.bridgeImageThreadId ?? this.session().threadId : null;
    if (!this.state.feishuReceipts[messageId]) {
      await this.serial(async () => {
        this.state.feishuReceipts[messageId] = { incomingMessageId: event.message_id,
          chatId: event.chat_id, cleanupRequested: false, reactionId: null,
          ...(imageThreadId ? { imageThreadId } : {}) };
        await this.save();
      });
      if (this.notifier) {
        const notifier = this.notifier;
        const task = (async () => {
          const reactionId = await notifier.addReaction(event.message_id).catch(() => null);
          await this.serial(async () => { this.state.feishuReceipts[messageId].reactionId = reactionId; await this.save(); });
          if (this.state.feishuReceipts[messageId].cleanupRequested) await this.removeFeishuReaction(messageId, notifier);
        })().catch(() => console.error("[bridge] Feishu typing reaction failed."));
        this.feishuReactionTasks.set(messageId, task);
        void task.finally(() => this.feishuReactionTasks.delete(messageId));
      }
    }
    try {
      if (imageThreadId && imageThreadId !== this.state.selectedThreadId) fail(409, "selected_target_changed", "Image ingress follows its selected chat at durable acceptance; no fallback.");
      if (imageThreadId && !localHost(this.session(imageThreadId).hostId)) fail(422, "capability_unavailable", "飞书图片缓存位于CodexWorker本机，暂不能把本机路径传给其他设备的聊天。");
      const text = event.message_type === "image" ? imagePrompt(await cacheFeishuImage(this, event, messageId)) : event.content;
      const result = await this.message({ protocolVersion: PROTOCOL, messageId, source: "trainer", text,
        delivery: this.config.feishu.defaultDelivery ?? "steer",
        ...(imageThreadId ? { targetThreadId: imageThreadId } : {}) }, { requireSelectedTarget: Boolean(imageThreadId) });
      if (result.session) await this.serial(async () => { this.session(result.session.threadId).feishuChatId = event.chat_id; await this.save(); });
      if (!result.duplicate && this.notifier && !["dispatch_pending", "message_queued"].includes(result.action)) {
        const text = result.text ?? (result.resumeList ? resumeListText(result)
          : result.action === "session_status" ? `当前会话：${result.session.alias}\n状态：${result.session.state}\n同步：${result.session.relayStatus?.mode ?? "native-history"}\n飞书：${result.health.integrations.feishu.connected ? "已连接" : "未连接"}`
          : result.session ? `已切换到：${result.session.alias}` : `已完成：${result.action}`);
        await this.notifier.sendMessage(text, { chatId: event.chat_id, threadId: result.session?.threadId ?? result.currentSession?.threadId });
      }
      if (!["dispatch_pending", "message_queued"].includes(result.action)) this.finishFeishuInput(messageId);
      return result;
    } catch (error) {
      this.finishFeishuInput(messageId);
      const unknown = this.state.receipts[messageId]?.nativeDispatch?.status === "unknown";
      if (this.notifier) await this.notifier.sendMessage(unknown
        ? "这条消息的原生发送结果尚未确认。请先在 Codex 查看，避免重复发送；Bridge 已保留原始回执。"
        : `未接收这条消息：${error.body?.error ?? "bridge_error"}\n${error.body?.detail ?? "请在 Codex 检查桥状态。"}`, { chatId: event.chat_id }).catch(() => {});
      throw error;
    }
  }

  finishFeishuInput(messageId, reason = "native_terminal") {
    const receipt = this.state.feishuReceipts[messageId];
    if (!receipt || receipt.cleanupRequested) return;
    receipt.cleanupRequested = true;
    receipt.cleanupRequestedAt = new Date().toISOString(); receipt.cleanupReason = reason;
    delete receipt.awaitingPublicOutput;
    if (receipt.reactionId && this.notifier) void this.removeFeishuReaction(messageId, this.notifier).catch(() => {});
  }

  async removeFeishuReaction(messageId, notifier) {
    const receipt = this.state.feishuReceipts[messageId];
    if (!receipt?.reactionId) return;
    if (await notifier.removeReaction(receipt.incomingMessageId, receipt.reactionId)) {
      await this.serial(async () => { receipt.reactionId = null; await this.save(); });
    }
  }

  pull(after, limit = 500) {
    if (!Number.isSafeInteger(after) || after < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 1000) fail(400, "invalid_request", "after must be nonnegative and limit must be 1..1000.");
    if (after < this.state.replayFloor) fail(409, "replay_too_old", "Event replay window expired. Read the native thread to recover, then continue at latestSeq.", false, { latestSeq: this.state.seq });
    const matching = this.state.events.filter(e => e.seq > after);
    return { events: clone(matching.slice(0, limit)), hasMore: matching.length > limit, latestSeq: this.state.seq };
  }

  async listen() {
    this.server = http.createServer((req, res) => { void this.handle(req, res); });
    await new Promise((resolve, reject) => {
      this.server.once("error", reject); this.server.listen(this.config.port, this.config.host, resolve);
    });
    this.config.port = this.server.address().port;
    this.pollTimer = setInterval(() => { void this.scan().catch(() => console.error("[bridge] Native synchronization failed.")); }, this.config.pollMs);
    this.pollTimer.unref();
    this.questions.start();
    return `http://${this.config.host}:${this.config.port}`;
  }

  async handle(req, res, context = {}) {
    let body, counted = false;
    try {
      const actual = Buffer.from(req.headers.authorization ?? "");
      const expected = Buffer.from(`Bearer ${context.token ?? this.config.token}`);
      if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) fail(401, "unauthorized", "Valid Bearer token required.");
      const url = new URL(req.url, "http://bridge.invalid");
      // Snapshot/control reads remain observable during drain. New work is
      // rejected BEFORE any native write, so an explicit retry is safe. Already
      // admitted HTTP requests still finish and remain counted until then.
      const snapshot = req.method === "GET" && (["/health", "/bridge/runtime", "/bridge/integrations",
        "/manager/events", "/manager/sessions", "/threads"].includes(url.pathname) ||
        /^\/manager\/sessions\/[^/]+$/.test(url.pathname));
      const reload = req.method === "POST" && url.pathname === "/bridge/runtime/reload";
      if (this.handoffPaused && !snapshot && !reload) {
        fail(503, "bridge_reloading", "Business handoff in progress; this request was not dispatched. Retry with the same messageId.", true);
      }
      if (!snapshot && !reload) { this.httpOperations++; counted = true; }
      if (req.method === "GET" && url.pathname === "/health") return json(res, 200, this.health());
      if (req.method === "POST" && url.pathname === "/bridge/text-receipts/abandon") {
        const peer = req.socket.remoteAddress?.replace(/^::ffff:/, "");
        if (!["127.0.0.1", "::1"].includes(peer)) fail(403, "loopback_required", "Receipt review is local-only.");
        return json(res, 200, await this.abandonTextReceipt(await readBody(req)));
      }
      if (["/bridge/images", "/bridge/images/recover", "/bridge/runtime", "/bridge/runtime/reload"].includes(url.pathname)) {
        const peer = req.socket.remoteAddress?.replace(/^::ffff:/, "");
        if (!["127.0.0.1", "::1"].includes(peer)) fail(403, "loopback_required", "Image output and code reload are local-only.");
        if (req.method === "GET" && url.pathname === "/bridge/runtime") return json(res, 200, this.businessRuntime.status());
        if (req.method === "POST" && url.pathname === "/bridge/runtime/reload") {
          body = await readBody(req);
          if (Object.keys(body).length) fail(400, "invalid_request", "Reload takes no caller-supplied code or path.");
          return json(res, 200, await this.businessRuntime.reload());
        }
        if (req.method === "POST" && url.pathname === "/bridge/images") { body = await readBody(req); return json(res, 200, await this.sendImage(body)); }
        if (req.method === "POST" && url.pathname === "/bridge/images/recover") { body = await readBody(req); return json(res, 200, await this.recoverImage(body)); }
        fail(404, "not_found", "Unsupported image/runtime operation.");
      }
      if (req.method === "GET" && url.pathname === "/bridge/integrations") return json(res, 200, this.integrationStatus());
      if (req.method === "POST" && url.pathname === "/bridge/integrations/reload") return json(res, 200, await this.reloadIntegrations());
      if (req.method === "POST" && url.pathname === "/bridge/questions") return json(res, 200, await this.questions.publish(await readBody(req)));
      const questionRoute = url.pathname.match(/^\/bridge\/questions\/([^/]+)(?:\/(resolve))?$/);
      if (questionRoute) {
        const peer = req.socket.remoteAddress?.replace(/^::ffff:/, "");
        if (!["127.0.0.1", "::1"].includes(peer)) fail(403, "loopback_required", "Question lifecycle updates are local-only.");
        const id = decodeURIComponent(questionRoute[1]);
        if (req.method === "GET" && !questionRoute[2]) return json(res, 200, this.questions.get(id));
        if (req.method === "POST" && questionRoute[2] === "resolve") return json(res, 200, await this.questions.resolve(id, await readBody(req)));
      }
      if (req.method === "GET" && url.pathname === "/manager/events") {
        const after = Number(url.searchParams.get("after") ?? req.headers["last-event-id"] ?? "0");
        const page = this.pull(after, Number(url.searchParams.get("limit") ?? 500));
        if (url.searchParams.get("stream") === "0") return json(res, 200, page);
        res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
        // No await between replay and subscribe: a live event cannot fall through the gap.
        for (const event of this.state.events.filter(e => e.seq > after)) res.write(`id: ${event.seq}\nevent: bridge\ndata: ${JSON.stringify(event)}\n\n`);
        res.write(": connected\n\n"); this.clients.add(res);
        const timer = setInterval(() => res.write(": heartbeat\n\n"), 15000); timer.unref();
        res.on("close", () => { clearInterval(timer); this.clients.delete(res); }); return;
      }
      if (req.method === "GET" && url.pathname === "/manager/sessions") return json(res, 200, { sessions: Object.values(this.state.sessions).map(clone), selectedThreadId: this.state.selectedThreadId });
      if (req.method === "GET" && url.pathname === "/threads") return json(res, 200, { threads: Object.values(this.state.sessions).slice(0, Math.max(1, Math.min(50, Number(url.searchParams.get("limit") ?? 50)))).map(s => ({ id: s.threadId, cwd: s.cwd, createdAt: s.createdAt, live: true, hostId: s.hostId })) });
      if (["POST", "PATCH"].includes(req.method)) body = await readBody(req);
      if (req.method === "POST" && url.pathname === "/manager/messages") return json(res, 200, await this.message(body));
      if (req.method === "POST" && url.pathname === "/manager/sessions/adopt") return json(res, 200, await this.register(body, { requireFork: body.command === "/fork" }));
      if (req.method === "POST" && url.pathname === "/threads") {
        const result = await this.message(legacyRequest({ text: "/new", messageId: body.messageId, safetyMode: body.safetyMode }));
        return json(res, 200, { thread: { id: result.session.threadId, cwd: result.session.cwd, createdAt: result.session.createdAt } });
      }
      if (req.method === "POST" && url.pathname === "/turn") {
        if (typeof body.threadId !== "string" || !body.threadId) fail(400, "invalid_request", "threadId is required.");
        this.session(body.threadId);
        const result = await this.message(legacyRequest({ ...body, targetThreadId: body.threadId }));
        if (body.wait !== false && ["dispatch_pending", "message_queued"].includes(result.action)) {
          return json(res, 200, await this.waitResult(result.messageId, body.threadId));
        }
        return json(res, 200, { threadId: result.session?.threadId ?? body.threadId, turnId: null, messageId: result.messageId,
          duplicate: result.duplicate, eventSeq: result.eventSeq, action: result.action,
          status: result.action === "dispatch_pending" ? "started" : result.action, nativeTurnPending: result.action === "dispatch_pending",
          ...(result.sessions ? { sessions: result.sessions } : {}),
          ...(result.resumeList ? { currentSession: result.currentSession, resumePage: result.resumePage, resumeQuery: result.resumeQuery } : {}) });
      }
      const match = url.pathname.match(/^\/(manager\/sessions|threads)\/([^/]+)(?:\/(select|last-turn|resume|release|unsubscribe|stop|compact))?$/);
      if (match) {
        const [, surface, encoded, operation] = match; const id = decodeURIComponent(encoded); const session = this.session(id);
        if (req.method === "POST" && operation === "select") return json(res, 200, await this.message(legacyRequest({ text: `/switch ${id}`, source: "trainer" })));
        if (req.method === "PATCH" && surface === "manager/sessions" && !operation) {
          if (typeof body.alias !== "string" || !body.alias.trim()) fail(400, "invalid_request", "alias is required.");
          await this.serial(async () => {
            await this.call("set_thread_title", { threadId: id, title: body.alias.trim() }); session.alias = body.alias.trim();
            await this.emit("session.renamed", { threadId: id }, { alias: session.alias });
          });
          return json(res, 200, { session: clone(session) });
        }
        if (req.method === "GET") {
          if (surface === "manager/sessions") return json(res, 200, { session: clone(session) });
          const snapshot = await this.read(id, session.hostId); const turns = transcriptTurns(snapshot);
          const last = turns.at(-1); const details = { lastUserText: last?.text ?? "", lastResult: last?.result ?? "", hasTurns: turns.length > 0 };
          return json(res, 200, operation === "last-turn" ? { threadId: id, ...details } : { thread: { id, cwd: session.cwd, live: true, ...details, turns, preview: details.lastResult.slice(0, 120) } });
        }
        if (req.method === "POST" && ["release", "unsubscribe"].includes(operation)) {
          return json(res, 200, { ok: true, status: "notSubscribed", detail: "Desktop owns the task; this adapter owns no app-server subscription." });
        }
        if (req.method === "POST" && operation === "resume") { await this.read(id, session.hostId); return json(res, 200, { thread: { id, live: true, cwd: session.cwd } }); }
        if (req.method === "POST" && operation === "stop") {
          const result = await this.message(legacyRequest({ text: "/stop", targetThreadId: id }));
          return json(res, 200, { idle: result.action === "turn_already_idle" });
        }
        if (operation === "compact") fail(501, "capability_unavailable", "The Desktop native tool pipe does not expose compact; use the client's native control.");
      }
      fail(404, "not_found", "Unknown route.");
    } catch (error) {
      const mapped = error instanceof BridgeError || error instanceof QuestionRelayError || error instanceof BusinessRuntimeError || error?.name === "ImageOutputError" ? error : new BridgeError(500, "bridge_error", "Bridge request failed; inspect local plugin diagnostics.");
      if (res.headersSent) { res.destroy(); return; }
      json(res, mapped.status, { ...mapped.body, ...(body?.messageId ? { messageId: body.messageId } : {}) });
    } finally { if (counted) this.httpOperations--; }
  }

  async waitResult(messageId, threadId) {
    const until = Date.now() + this.config.turnTimeoutMs;
    while (Date.now() < until && !this.closed) {
      const events = this.state.events.filter(e => e.messageId === messageId ||
        (e.threadId === threadId && e.type === "turn.completed" && e.payload.messageIds?.includes(messageId)));
      const rejected = events.find(e => e.type === "message.rejected");
      if (rejected) return { threadId, messageId, status: "error", resultText: "", reason: rejected.payload.detail };
      const done = events.find(e => e.type === "turn.completed");
      if (done) {
        const output = this.state.events.filter(e => e.threadId === threadId && e.turnId === done.turnId && e.type === "item.completed").map(e => e.payload.snapshot).join("\n");
        return { threadId, turnId: done.turnId, messageId, status: done.payload.status === "completed" ? "completed" : "error", resultText: output, reason: done.payload.status };
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    return { threadId, messageId, turnId: null, status: "timeout", resultText: "", reason: "Native task is not stopped by an HTTP timeout; inspect it in Codex Desktop." };
  }

  async close() {
    clearInterval(this.handoffTimer); this.handoffTimer = null;
    this.closed = true; clearInterval(this.pollTimer); this.ws?.stop();
    await this.questions.close();
    this.onlineAnnouncement.reset(); await this.onlineAnnouncement.task;
    for (const client of this.clients) client.end(); this.clients.clear();
    if (this.server) await new Promise(resolve => this.server.close(resolve));
    await Promise.allSettled([...this.backgroundTasks, ...this.feishuInputs.values(), ...this.feishuReactionTasks.values()]);
    await this.tail;
    await this.inboxTail;
    // initialize() may fail after acquiring the lease but before loading state.
    // Never overwrite an unreadable ledger with constructor defaults on cleanup.
    if (this.leaseNonce && this.stateLoaded) await this.save().catch(error => { if (error.code !== "ENOENT") throw error; });
    if (this.leaseNonce) {
      try {
        const lease = JSON.parse(await readFile(this.leasePath, "utf8"));
        if (lease.nonce === this.leaseNonce && lease.pid === process.pid) await unlink(this.leasePath);
      } catch (error) { if (error.code !== "ENOENT") throw error; }
      this.leaseNonce = null;
    }
    if (this.writerLease?.listening) await new Promise(resolve => this.writerLease.close(resolve));
  }
}

function legacyRequest(body) {
  return { protocolVersion: PROTOCOL, messageId: body.messageId ?? randomUUID(), source: body.source ?? "hermes",
    text: body.text, targetThreadId: body.targetThreadId ?? null, delivery: body.delivery ?? "queue", safetyMode: body.safetyMode ?? null };
}

function transcriptTurns(snapshot) {
  const turns = snapshot.page?.order === "newest_first" ? [...(snapshot.turns ?? [])].reverse() : snapshot.turns ?? [];
  return turns.map(turn => {
    const items = publicItems({ turns: [turn] });
    return { text: items.filter(i => i.kind !== "assistant").map(i => i.text.replace(/^\[Hermes Bridge[^\n]+\]\n/, "")).join("\n"),
      result: items.filter(i => i.kind === "assistant" && i.final).map(i => i.text).join("\n") };
  });
}

async function readBody(req) {
  const chunks = []; let size = 0;
  for await (const chunk of req) {
    size += chunk.length; if (size > 1024 * 1024) fail(413, "invalid_request", "Request exceeds 1MiB."); chunks.push(chunk);
  }
  const body = Buffer.concat(chunks).toString("utf8");
  try { const parsed = JSON.parse(body || "{}"); if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(); return parsed; }
  catch { fail(400, "invalid_request", "Body must be a JSON object."); }
}

function json(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "content-length": Buffer.byteLength(text) }); res.end(text);
}
