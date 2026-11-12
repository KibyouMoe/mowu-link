import { createHash, randomUUID } from "node:crypto";
import { readFile, writeFile, rename } from "node:fs/promises";
import path from "node:path";
import { questionInitiator } from "./question-origin.mjs";
import { getActiveRouterClient } from "./router-client.mjs";
import { routerQuestionNotice } from "./router-question-channel.mjs";
import { questionCard, parseCardAction, QUESTION_CARD_LAYOUT_VERSION } from "./question-cards.mjs";

const fingerprint = value => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export class QuestionRelayError extends Error {
  constructor(status, error, detail) { super(detail); this.status = status; this.body = { error, detail, retryable: false }; }
}
const reject = (status, error, detail) => { throw new QuestionRelayError(status, error, detail); };

// Native async questions may be answered after their own turn completes,
// but must not deliver into a newer task, an interrupted turn or another chat.
export function questionSourceTurn(snapshot, allowCompleted = false) {
  const live = snapshot?.deliveryObservation;
  if (live) return live.turnId && (["inProgress", "in_progress", "running", "started", "pending"].includes(live.turnStatus) ||
    allowCompleted && live.turnStatus === "completed") ? { id: live.turnId } : null;
  const turns = snapshot?.turns ?? [];
  const active = turns.find(t => ["inProgress", "in_progress", "running", "started"].includes(t.status));
  if (active || !allowCompleted) return active ?? null;
  const latest = snapshot?.page?.order === "newest_first" ? turns[0] : turns.at(-1);
  return latest?.status === "completed" ? latest : null;
}

export function normalizeQuestion(request) {
  if (!uuid.test(request.questionId ?? "") || typeof request.threadId !== "string" || !request.threadId ||
    !Array.isArray(request.questions) || request.questions.length < 1 || request.questions.length > 3) {
    reject(400, "invalid_question", "questionId UUIDv4, threadId and one to three public questions are required.");
  }
  const questions = request.questions.map(q => {
    if (typeof q?.title !== "string" || !q.title.trim() || q.title.length > 8000 ||
      (q.options != null && (!Array.isArray(q.options) || q.options.length < 2 || q.options.length > 6 ||
        q.options.some(s => typeof s !== "string" || !s.trim() || s.length > 1000)))) {
      reject(400, "invalid_question", "Each public question needs a nonempty title and optional two to six text options.");
    }
    return { title: q.title.trim(), ...(q.options ? { options: q.options.map(s => s.trim()) } : {}) };
  });
  if (request.sourceTurnId != null && (typeof request.sourceTurnId !== "string" || !request.sourceTurnId || request.sourceTurnId.length > 200)) reject(400, "invalid_question", "Invalid source turn.");
  if (request.sourceQuestionItemIds != null && (!Array.isArray(request.sourceQuestionItemIds) || request.sourceQuestionItemIds.length !== questions.length || request.sourceQuestionItemIds.some(id => {
    try { const parts = JSON.parse(id); return !Array.isArray(parts) || parts.length !== 3 || parts[0] !== "request_user_input_async" || typeof parts[1] !== "string" || !Number.isSafeInteger(parts[2]) || parts[2] < 0; } catch { return true; }
  }))) reject(400, "invalid_question", "Only explicit public async clarification IDs may be bound, not approval IDs.");
  if (request.delivery != null && (request.delivery !== "async" || !request.sourceTurnId || !request.sourceQuestionItemIds))
    reject(400, "invalid_question", "Async public questions require exact native source identities.");
  return { questionId: request.questionId, threadId: request.threadId, questions,
    ...(request.delivery === "async" ? { delivery: "async" } : {}),
    ...(request.sourceTurnId ? { sourceTurnId: request.sourceTurnId } : {}),
    ...(request.sourceQuestionItemIds ? { sourceQuestionItemIds: request.sourceQuestionItemIds } : {}) };
}

export function questionText(request, bridgeName) {
  const lines = [`${bridgeName}提问 · ${request.questionId.slice(0, 8)}`];
  request.questions.forEach((q, index) => {
    lines.push("", request.questions.length > 1 ? `${index + 1}. ${q.title}` : q.title);
    q.options?.forEach((option, i) => lines.push(`${i + 1}）${option}`));
  });
  lines.push("", request.recipient?.kind === "hermes"
    ? "这条提问由溪流回答，答复会更新到原卡片。"
    : "可以直接在飞书回复选项编号或答案文字，无需打开 Codex 提问面板。");
  return lines.join("\n");
}

// Accept only explicit public questions from a publisher or native projection.
// Routing and card state are separate from private tool arguments and reasoning.
export class QuestionRelay {
  constructor(manager) {
    this.manager = manager; this.inflight = new Map(); this.tail = Promise.resolve();
    this.path = path.join(manager.stateDir, "question-actions.json"); this.taskChecks = new Map();
    this.state = { version: 1, entries: {} }; this.running = null;
  }

  async initialize() {
    try { this.state = JSON.parse(await readFile(this.path, "utf8")); } catch (e) { if (e.code !== "ENOENT") throw e; }
    if (this.state.version !== 1 || !this.state.entries || Array.isArray(this.state.entries)) reject(503, "question_state_invalid", "Question actions were not discarded.");
    for (const entry of Object.values(this.state.entries)) {
      if (entry.status === "dispatching") {
        const receipt = this.manager.state.receipts?.[entry.nativeMessageId];
        if (receipt?.nativeDispatch?.status === "accepted") entry.status = "answered";
        else if (receipt?.response?.action === "message_queued" && !receipt.nativeDispatch) {
          entry.status = "answer_pending"; entry.answerQueued = true;
        } else { entry.status = "answer_unknown"; entry.reason = "dispatch_outcome_unknown"; }
        entry.revision++;
      }
      if (entry.routerDelivery?.status === "sending") {
        entry.routerDelivery.status = "unknown"; entry.revision++;
      }
    }
    await this.save();
  }

  serial(fn) {
    // Independent of the native message lock: a slow task must not delay card ACK.
    const task = this.tail.then(async () => { if (this.storageFault) await this.save(); return fn(); });
    this.tail = task.catch(() => {}); return task;
  }
  async save() {
    try { await writeFile(this.path + ".tmp", JSON.stringify(this.state), { mode: 0o600 }); await rename(this.path + ".tmp", this.path); this.storageFault = false; }
    catch (error) { this.storageFault = true; throw error; }
  }
  get(id) {
    const q = this.manager.state.publicQuestions?.[id]; const s = this.state.entries[id];
    if (!q) reject(404, "question_not_found", "No exact public question receipt was found.");
    return { questionId: id, threadId: q.threadId, questions: q.questions, status: s?.status ?? "legacy",
      sourceTurnId: s?.sourceTurnId ?? null, answers: s?.answers ?? null,
      recipient: q.recipient ?? { kind: "trainer" }, routerDelivery: s?.routerDelivery?.status ?? null,
      reason: s?.reason ?? null, cardUpdated: Boolean(s?.messageId && s.patchedRevision === s.revision),
      feishu: q.feishuSent ? "sent" : "pending" };
  }
  status() {
    const entries = Object.values(this.state.entries);
    return { policy: "source-turn-no-independent-ttl", routingPolicy: "turn-initiator", answerMirror: "original-question-card", cardLayoutVersion: QUESTION_CARD_LAYOUT_VERSION, open: entries.filter(s => s.status === "open").length,
      pending: entries.filter(s => ["answer_pending", "dispatching"].includes(s.status)).length,
      unknown: entries.filter(s => s.status === "answer_unknown").length,
      updatesPending: entries.filter(s => s.messageId && s.patchedRevision !== s.revision).length,
      callbacks: this.state.callbackStats ?? { received: 0, accepted: 0, rejected: 0, lastOutcome: null } };
  }

  async publish(input, { existingCard = null } = {}) {
    const request = normalizeQuestion(input); const hash = fingerprint(request);
    const active = this.inflight.get(request.questionId);
    if (active) {
      if (active.hash !== hash) reject(409, "question_conflict", "The same questionId cannot identify different question text or targets.");
      return { ...await active.task, duplicate: true };
    }
    const task = this.deliver(request, hash, existingCard);
    this.inflight.set(request.questionId, { hash, task });
    try { return await task; } finally { this.inflight.delete(request.questionId); }
  }

  async deliver(request, hash, existingCard = null) {
    const m = this.manager; let record; let duplicate;
    if (existingCard && !Object.values(m.state.publicTextOutbox ?? {}).some(e =>
        e.uuid === existingCard.uuid && e.feishuMessageId === existingCard.messageId &&
        e.threadId === request.threadId && existingCard.threadId === request.threadId &&
        e.publicQuestion?.questionId === request.questionId))
      reject(409, "question_card_conflict", "Reuse only this exact persisted question output card.");
    await m.serial(async () => {
      m.session(request.threadId);
      if (m.state.selectedThreadId !== request.threadId) reject(409, "selected_target_changed", "Questions must target the current Feishu-selected chat; no fallback was used.");
      m.state.publicQuestions ??= {};
      record = m.state.publicQuestions[request.questionId]; duplicate = Boolean(record);
      if (record && record.hash !== hash) reject(409, "question_conflict", "The same questionId cannot identify different question text or targets.");
      let sourceTurnId;
      if (!record) {
        const snapshot = m.deliveryState ? await m.deliveryState(m.session(request.threadId)) : await m.read(request.threadId, m.session(request.threadId).hostId);
        sourceTurnId = questionSourceTurn(snapshot, request.delivery === "async")?.id ?? null;
        if (request.sourceTurnId && request.sourceTurnId !== sourceTurnId) {
          if (request.delivery === "async") sourceTurnId = null; // Historical question: render closed, never answer into a newer task.
          else reject(409, "source_turn_ended", "The explicit source turn is no longer active.");
        }
      }
      const recipient = record?.recipient ?? await questionInitiator(m, request.threadId, request.sourceTurnId ?? sourceTurnId);
      record ??= m.state.publicQuestions[request.questionId] = { ...request, recipient, hash, createdAt: new Date().toISOString(),
        boundSourceTurnId: sourceTurnId, bindingEpoch: m.state.selectionEpoch,
        chatId: m.session(request.threadId).feishuChatId ?? m.config.feishu.chatId ?? null,
        text: questionText({ ...request, recipient }, m.config.bridgeName), feishuSent: false };
      record.recipient ??= recipient;
      await m.save();
    });
    let action = this.state.entries[request.questionId];
    if (!action) {
      await this.serial(async () => {
        action = this.state.entries[request.questionId] ??= { status: record.boundSourceTurnId ? "open" : "expired", reason: record.boundSourceTurnId ? null : "source_turn_ended",
          sourceTurnId: record.boundSourceTurnId ?? null, bindingEpoch: record.bindingEpoch ?? m.state.selectionEpoch, nonce: randomUUID(), selected: {},
          messageUuid: existingCard?.uuid ?? randomUUID(), ...(existingCard ? { messageId: existingCard.messageId } : {}), revision: 1, patchedRevision: 0 };
        await this.save();
      });
    }
    await this.forwardToInitiator(record, action);
    if (!record.feishuSent && m.notifier && record.chatId) {
      // No unbounded POST retries: after the first possible send the UUID's safe
      // dedup window is fixed, not extended on retries or a business restart.
      if (action.firstSendAt && Date.now() - action.firstSendAt > 50 * 60000) reject(409, "question_send_window_ended", "Inspect the original card; no new question message was sent.");
      await this.serial(async () => { action.firstSendAt ??= Date.now(); await this.save(); });
      const sentRevision = action.revision;
      const sent = await (action.messageId && m.notifier.updateQuestionCard
        ? m.notifier.updateQuestionCard(action.messageId, questionCard(record, action, m.config.bridgeName), { threadId: request.threadId })
        : m.notifier.sendQuestionCard ? m.notifier.sendQuestionCard(questionCard(record, action, m.config.bridgeName), {
        threadId: request.threadId, chatId: record.chatId, uuid: action.messageUuid }) :
        m.notifier.sendMessage(record.text, { title: `${m.config.bridgeName}-提问`, threadId: request.threadId, chatId: record.chatId, uuid: action.messageUuid })).catch(() => null);
      if (sent?.code === 0) {
        await this.serial(async () => { action.messageId = sent.data?.message_id ?? action.messageId ?? null; action.patchedRevision = sentRevision; action.cardLayoutVersion = QUESTION_CARD_LAYOUT_VERSION; await this.save(); });
        await m.serial(async () => { record.feishuSent = true; await m.save(); });
      }
    }
    return { questionId: request.questionId, threadId: request.threadId, duplicate,
      feishu: record.feishuSent ? "sent" : m.notifier && record.chatId ? "pending" : "unavailable",
      detail: "Only explicit public question text/options were published. Channel acceptance is not a human/agent read receipt; plain-text answers arrive through the normal native message ingress." };
  }

  async resolve(id, input) {
    const m = this.manager; const q = m.state.publicQuestions?.[id];
    if (!q || input.threadId !== q.threadId || q.threadId !== m.state.selectedThreadId) reject(409, "selected_target_changed", "Resolve only an exact question in the currently selected source chat.");
    if (!["answered", "expired", "canceled"].includes(input.status)) reject(400, "invalid_question_resolution", "Use answered, expired or canceled.");
    const answers = input.status === "answered" ? input.answers : null;
    if (answers && (!Array.isArray(answers) || answers.length !== q.questions.length || answers.some(s => typeof s !== "string" || !s.trim() || s.length > 8000)) || input.status === "answered" && !answers) reject(400, "invalid_question_resolution", "Supply the complete public answers in question order.");
    await this.serial(async () => {
      const s = this.state.entries[id];
      if (!s) reject(409, "legacy_question", "An old text-only question has no editable card receipt.");
      if (s.status === input.status && JSON.stringify(s.answers ?? null) === JSON.stringify(answers)) return;
      if (["dispatching", "answer_unknown", "answered", "expired", "canceled", "rejected"].includes(s.status)) reject(409, "question_closed", "Question already resolved; no answer was resent.");
      s.status = input.status; s.answers = answers; s.reason = input.status === "canceled" ? "canceled_by_owner" : input.status === "expired" ? "source_turn_ended" : null;
      s.revision++; await this.save();
    });
    // Metadata only: normal text/native-panel replies are NEVER sent again.
    this.kick(); return this.get(id);
  }

  router() { return this.manager.routerClient ?? getActiveRouterClient(); }

  async forwardToInitiator(question, action) {
    if (question.recipient?.kind !== "hermes" || action.status !== "open" ||
        ["sent", "sending", "unknown"].includes(action.routerDelivery?.status) ||
        Date.now() < (action.routerRetryAt ?? 0)) return;
    const client = this.router();
    if (!client?.notice) {
      await this.serial(async () => { action.routerDelivery = { status: "unavailable" }; action.routerRetryAt = Date.now()+5000; action.revision++; await this.save(); });
      return;
    }
    if (question.recipient.taskId && client.getTask) {
      let task;
      try { task = await client.getTask(question.recipient.taskId); }
      catch {
        await this.serial(async () => { action.routerDelivery = { status: "unavailable" }; action.routerRetryAt = Date.now()+5000; action.revision++; await this.save(); });
        return;
      }
      if (["cancelled","canceled","failed","done"].includes(task?.state)) {
        await this.serial(async () => { action.status = "expired"; action.reason = "source_task_ended"; action.revision++; await this.save(); });
        return;
      }
    }
    await this.serial(async () => { action.routerDelivery = { status: "sending" }; action.revision++; await this.save(); });
    let result, status;
    try {
      result = await client.notice({ kind: "worker.question", urgency: "urgent",
        text: routerQuestionNotice(question, action, this.manager) });
      status = result?.ok === true ? "sent" : "unavailable";
    } catch { status = "unknown"; } // A lost response may already have reached Hermes: never blindly notify twice.
    await this.serial(async () => {
      action.routerDelivery = { status, ...(result?.event_id ? { eventId: result.event_id } : {}) };
      if (status === "unavailable") action.routerRetryAt = Date.now()+5000;
      action.revision++; await this.save();
    });
    console.error("[bridge] question_forwarded " + JSON.stringify({ questionId: question.questionId, recipient: "hermes", taskId: question.recipient.taskId ?? null, status }));
  }

  async acceptAgentAnswer(id, input) {
    const q = this.manager.state.publicQuestions?.[id], s = this.state.entries[id];
    if (!q || !s) reject(404, "question_not_found", "No exact question exists.");
    if (q.recipient?.kind !== "hermes" || input.actor !== "hermes" || input.threadId !== q.threadId ||
        (input.taskId ?? null) !== (q.recipient.taskId ?? null) || input.nonce !== s.nonce)
      reject(403, "question_recipient_mismatch", "Only the bound originating agent/task may answer this question.");
    if (["answer_pending","dispatching","answered"].includes(s.status) && JSON.stringify(s.answers)===JSON.stringify(input.answers))
      return { ...this.get(id), duplicate: true };
    if (q.threadId !== this.manager.state.selectedThreadId || s.bindingEpoch !== this.manager.state.selectionEpoch)
      reject(409, "selected_target_changed", "Question binding changed; no answer was dispatched.");
    if (q.recipient.taskId && this.router()?.getTask) {
      const task = await this.router().getTask(q.recipient.taskId);
      if (["cancelled","canceled","failed","done"].includes(task?.state))
        reject(409, "source_task_ended", "Original task ended; no answer was dispatched.");
    }
    if (!Array.isArray(input.answers) || input.answers.length !== q.questions.length ||
        input.answers.some(a => typeof a !== "string" || !a.trim() || a.length>8000 || a.trim()==="<填写回答>"))
      reject(400, "invalid_question_answer", "Provide one real nonempty answer per question.");
    return this.serial(async () => {
      if (["answer_pending","dispatching","answered"].includes(s.status) && JSON.stringify(s.answers)===JSON.stringify(input.answers))
        return { ...this.get(id), duplicate: true };
      if (s.status !== "open") reject(409, "question_closed", "Question is already resolved; no answer was repeated.");
      s.answers = [...input.answers]; s.answerActor = "hermes"; s.status = "answer_pending";
      s.nativeMessageId = randomUUID(); s.revision++; await this.save();
      return { ...this.get(id), duplicate: false };
    });
  }

  async acceptCard(payload) {
    const event = parseCardAction(payload); const m = this.manager;
    const toast = (content, type = "info") => ({ toast: { type, content } });
    if (!event || typeof event.eventId !== "string" || !event.eventId || event.eventId.length > 200) {
      await this.serial(async () => {
        const stats = this.state.callbackStats ??= { received: 0, accepted: 0, rejected: 0 };
        stats.received++; stats.rejected++; stats.lastOutcome = "invalid_callback_shape"; await this.save();
      });
      return toast("无效的提问操作。", "error");
    }
    const { bridgeQuestion: id, nonce, action, questionIndex: qi, optionIndex: oi } = event.value;
    const result = await this.serial(async () => {
      const q = m.state.publicQuestions?.[id]; const s = this.state.entries[id];
      if (!q || !s || s.nonce !== nonce || s.messageId !== event.messageId || q.chatId !== event.chatId ||
          !m.config.feishu.trainerOpenIdWhitelist?.includes(event.openId)) return toast("无权操作此提问卡片。", "error");
      if (s.callbackEvents?.includes(event.eventId)) return toast("此操作已保存，不会重复处理。", "info");
      if (q.threadId !== m.state.selectedThreadId || s.bindingEpoch !== m.state.selectionEpoch) {
        if (s.status === "open" || s.status === "answer_pending") { s.status = "expired"; s.reason = "selected_target_changed"; s.revision++; await this.save(); }
        return toast("接入会话已切换，此提问已过期。", "warning");
      }
      if (q.recipient?.kind === "hermes") return toast("这条提问由溪流回答。", "info");
      if (s.status !== "open") return toast(s.status === "answered" ? "此提问已经回答，不会重复投递。" : "此提问已关闭或正在处理，不会重复投递。", "info");
      if (!s.sourceTurnId || !q.questions.every(q => q.options)) return toast("请直接回复完整答案。", "info");
      if (action === "select") {
        if (!Number.isInteger(qi) || !Number.isInteger(oi) || !q.questions[qi]?.options?.[oi]) return toast("无效选项。", "error");
        s.selected[qi] = oi;
      } else if (action !== "submit") return toast("无效操作。", "error");
      const complete = q.questions.every((_, i) => Number.isInteger(s.selected[i]));
      if (action === "submit" && !complete) return toast("请先为每一道题选择答案。", "warning");
      if (complete && (q.questions.length === 1 || action === "submit")) {
        s.answers = q.questions.map((q, i) => q.options[s.selected[i]]); s.answerActor = "trainer";
        s.status = "answer_pending"; s.nativeMessageId = randomUUID();
      }
      s.callbackEvents = [...(s.callbackEvents ?? []), event.eventId].slice(-256);
      s.revision++; await this.save();
      return toast(s.status === "answer_pending" ? "答案已保存，正在送回原会话。" : "选择已保存，请选完后提交。", "success");
    });
    await this.serial(async () => {
      const stats = this.state.callbackStats ??= { received: 0, accepted: 0, rejected: 0 };
      stats.received++;
      if (result.toast.type === "success") stats.accepted++; else stats.rejected++;
      stats.lastOutcome = result.toast.type === "success" ? "durably_accepted" : "ignored_or_rejected";
      await this.save();
    });
    // Return the WS response first; the timer, not this callback, performs I/O.
    return result;
  }

  start() { this.timer = setInterval(() => this.kick(), 1000); this.timer.unref(); }
  kick() {
    if (this.running || this.manager.closed || this.manager.handoffPaused) return;
    this.running = this.manager.track(this.process()).catch(() => { console.error("[bridge] Question synchronization pending; no unknown native answer is retried."); })
      .finally(() => { this.running = null; });
  }
  async close() { clearInterval(this.timer); await this.running; await this.tail; }

  async sourceState(q, s) {
    if (q.threadId !== this.manager.state.selectedThreadId || s.bindingEpoch !== this.manager.state.selectionEpoch) return { expired: true, reason: "selected_target_changed" };
    let snapshot;
    if (q.recipient?.kind === "hermes" && q.recipient.taskId && this.router()?.getTask) {
      try {
        let cached = this.taskChecks.get(q.recipient.taskId);
        if (!cached || s.status === "answer_pending" || Date.now()-cached.at >= 5000) {
          cached = { task: await this.router().getTask(q.recipient.taskId), at: Date.now() };
          this.taskChecks.set(q.recipient.taskId, cached);
        }
        if (["cancelled","canceled","failed","done"].includes(cached.task?.state)) return { expired: true, reason: "source_task_ended" };
      } catch { return { unavailable: true }; }
    }
    if (q.delivery === "async") {
      try {
        const snapshot = await this.manager.deliveryState(this.manager.session(q.threadId));
        const source = questionSourceTurn(snapshot, true);
        return source?.id === s.sourceTurnId ? { active: true } : { expired: true, reason: "source_turn_ended" };
      } catch { return { unavailable: true }; }
    }
    if (this.manager.isSelfTarget?.(this.manager.session(q.threadId))) {
      try {
        const live = (await this.manager.deliveryState(this.manager.session(q.threadId))).deliveryObservation;
        if (!live) return { unavailable: true };
        return live.turnId === s.sourceTurnId && ["inProgress", "in_progress", "running", "started", "pending"].includes(live.turnStatus)
          ? { active: true } : { expired: true, reason: "source_turn_ended" };
      } catch { return { unavailable: true }; }
    }
    try { snapshot = await this.manager.read(q.threadId, this.manager.session(q.threadId).hostId); }
    catch {
      try {
        const live = (await this.manager.deliveryState(this.manager.session(q.threadId))).deliveryObservation;
        if (!live) return { unavailable: true };
        if (live.turnId !== s.sourceTurnId || ["completed", "failed", "interrupted", "cancelled", "canceled"].includes(live.turnStatus)) return { expired: true, reason: "source_turn_ended" };
        return { active: true }; // No native panel answers are invented from status.
      } catch { return { unavailable: true }; }
    }
    const turn = snapshot.turns?.find(t => t.id === s.sourceTurnId);
    if (turn && q.sourceQuestionItemIds) {
      for (const item of turn.items ?? []) {
        if (item.type !== "userMessage") continue;
        const text = item.text ?? item.content?.map(c => c.text ?? "").join("\n") ?? "";
        const marker = text.match(/<send_user_message_question_reply>\s*([\s\S]*?)\s*<\/send_user_message_question_reply>/);
        if (!marker) continue;
        try {
          const replies = JSON.parse(marker[1]);
          const answers = q.sourceQuestionItemIds.map(id => replies.find(r => r.questionItemId === id)?.answer);
          if (answers.every(a => typeof a === "string" && a.trim())) return { answers };
        } catch { /* Not a complete public reply; do not infer from arbitrary text. */ }
      }
    }
    if (!turn || ["completed", "failed", "interrupted", "cancelled", "canceled"].includes(turn.status)) return { expired: true, reason: "source_turn_ended" };
    if (!["inProgress", "in_progress", "running", "started"].includes(turn.status)) return { unavailable: true };
    return { active: true };
  }

  async linkAnswerCard(id, action) {
    const m = this.manager;
    if (action.answerDeliveryLinked || !action.nativeMessageId || !action.messageId ||
        action.patchedRevision !== action.revision || !m.messageJournal) return;
    const recordId = m.state.latestTranscript?.["input:" + action.nativeMessageId];
    if (!recordId) return;
    const receipt = await m.messageJournal.receipt(recordId);
    if (receipt.plannedPages?.includes("question:" + id))
      await m.messageJournal.delivered(recordId, "question:" + id, "sent", action.messageId);
    await this.serial(async () => { action.answerDeliveryLinked = recordId; await this.save(); });
  }

  async process() {
    const m = this.manager;
    if (this.storageFault) await this.serial(() => this.save());
    for (const [id, s] of Object.entries(this.state.entries)) {
      if (m.closed || m.handoffPaused) return;
      const q = m.state.publicQuestions?.[id]; if (!q) continue;
      // Refresh only exact known cards from this current binding. No new message
      // is posted, no question ID/nonce/answer/native receipt is replaced.
      if (s.messageId && q.threadId === m.state.selectedThreadId && s.bindingEpoch === m.state.selectionEpoch &&
          (s.cardLayoutVersion ?? 1) < QUESTION_CARD_LAYOUT_VERSION) await this.serial(async () => {
        s.cardLayoutVersion = QUESTION_CARD_LAYOUT_VERSION; s.revision++; await this.save();
      });
      if (["open", "answer_pending"].includes(s.status)) {
        const source = await this.sourceState(q, s);
        await this.serial(async () => {
          if (!["open", "answer_pending"].includes(s.status)) return;
          if (source.answers) { s.status = "answered"; s.answers = source.answers; s.reason = null; s.revision++; await this.save(); }
          else if (source.expired) { s.status = "expired"; s.reason = source.reason; s.revision++; await this.save(); }
        });
        if (s.status === "open" && source.active) await this.forwardToInitiator(q, s);
        if (s.status === "answer_pending" && s.answerQueued) {
          const receipt = m.state.receipts?.[s.nativeMessageId] ?? await m.receiptArchive?.get(s.nativeMessageId);
          if (receipt?.nativeDispatch?.status === "accepted") await this.serial(async () => { s.status = "answered"; s.revision++; await this.save(); });
          else if (receipt?.error) await this.serial(async () => { s.status = "answer_unknown"; s.reason = receipt.error.error; s.revision++; await this.save(); });
        }
        if (s.status === "answer_pending" && !s.answerQueued && source.active && !m.handoffPaused && !m.closed &&
            !m.compacting?.(m.session(q.threadId))) {
          const answers = s.answers;
          const text = `提问 ${id.slice(0, 8)} 的回答：\n` + q.questions.map((q, i) => `${i + 1}）${q.title}\n答案：${answers[i]}`).join("\n\n");
          await this.serial(async () => { if (s.status === "answer_pending") { s.status = "dispatching"; s.revision++; await this.save(); } });
          if (s.status === "dispatching") {
            try {
              const agent = s.answerActor === "hermes";
              const accepted = await m.message({ protocolVersion: "1.1.1-draft", messageId: s.nativeMessageId,
                source: agent ? "hermes" : "trainer", origin: agent ? "router-question" : "feishu",
                ...(agent && q.recipient.taskId ? { taskId: q.recipient.taskId } : {}),
                text: agent ? text : `飞书：${text}`, targetThreadId: q.threadId, delivery: "steer" },
                { requireSelectedTarget: true, requireSelectionEpoch: s.bindingEpoch, requireSourceTurnId: s.sourceTurnId, allowCompletedSourceTurn: q.delivery === "async" });
              await this.serial(async () => {
                s.answerQueued = accepted?.action === "message_queued";
                s.status = s.answerQueued ? "answer_pending" : "answered";
                s.reason = null; s.revision++; await this.save();
              });
            } catch (e) {
              await this.serial(async () => {
                const safeReject = ["source_turn_ended", "selected_target_changed", "writer_conflict"].includes(e.body?.error);
                s.status = e.body?.error === "writer_conflict" ? "rejected" : safeReject ? "expired" : "answer_unknown";
                s.reason = safeReject ? e.body.error : "dispatch_outcome_unknown"; s.revision++; await this.save();
              });
            }
          }
        }
      }
      if (!m.closed && !m.handoffPaused && s.messageId && s.patchedRevision !== s.revision && m.notifier?.updateQuestionCard && Date.now() >= (s.retryPatchAt ?? 0)) {
        const revision = s.revision;
        const result = await m.notifier.updateQuestionCard(s.messageId, questionCard(q, s, m.config.bridgeName), { threadId: q.threadId }).catch(() => null);
        await this.serial(async () => { if (result?.code === 0) { s.patchedRevision = revision; s.retryPatchAt = null; } else s.retryPatchAt = Date.now() + 10000; await this.save(); });
      }
      await this.linkAnswerCard(id, s);
    }
  }
}
