import { createHash, randomUUID } from "node:crypto";
import { readFile, writeFile, rename } from "node:fs/promises";
import path from "node:path";
import { questionCard, parseCardAction, QUESTION_CARD_LAYOUT_VERSION } from "./question-cards.mjs";

const fingerprint = value => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export class QuestionRelayError extends Error {
  constructor(status, error, detail) { super(detail); this.status = status; this.body = { error, detail, retryable: false }; }
}
const reject = (status, error, detail) => { throw new QuestionRelayError(status, error, detail); };

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
  return { questionId: request.questionId, threadId: request.threadId, questions,
    ...(request.sourceTurnId ? { sourceTurnId: request.sourceTurnId } : {}),
    ...(request.sourceQuestionItemIds ? { sourceQuestionItemIds: request.sourceQuestionItemIds } : {}) };
}

export function questionText(request, bridgeName) {
  const lines = [`${bridgeName}提问 · ${request.questionId.slice(0, 8)}`];
  request.questions.forEach((q, index) => {
    lines.push("", request.questions.length > 1 ? `${index + 1}. ${q.title}` : q.title);
    q.options?.forEach((option, i) => lines.push(`${i + 1}）${option}`));
  });
  lines.push("", "可以直接在飞书或溪流回复选项编号或答案文字，无需打开 Codex 提问面板。");
  return lines.join("\n");
}

// The Desktop history API does not return its question panels. This is an
// explicit public-question publisher, not a raw tool-argument/history exporter.
export class QuestionRelay {
  constructor(manager) {
    this.manager = manager; this.inflight = new Map(); this.tail = Promise.resolve();
    this.path = path.join(manager.stateDir, "question-actions.json");
    this.state = { version: 1, entries: {} }; this.running = null;
  }

  async initialize() {
    try { this.state = JSON.parse(await readFile(this.path, "utf8")); } catch (e) { if (e.code !== "ENOENT") throw e; }
    if (this.state.version !== 1 || !this.state.entries || Array.isArray(this.state.entries)) reject(503, "question_state_invalid", "Question actions were not discarded.");
    for (const entry of Object.values(this.state.entries)) if (entry.status === "dispatching") {
      entry.status = "answer_unknown"; entry.reason = "dispatch_outcome_unknown"; entry.revision++;
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
      reason: s?.reason ?? null, cardUpdated: Boolean(s?.messageId && s.patchedRevision === s.revision),
      feishu: q.feishuSent ? "sent" : "pending", hermes: q.hermesStored ? "stored" : q.hermesQueued ? "queued" : "unavailable" };
  }
  status() {
    const entries = Object.values(this.state.entries);
    return { policy: "source-turn-no-independent-ttl", open: entries.filter(s => s.status === "open").length,
      pending: entries.filter(s => ["answer_pending", "dispatching"].includes(s.status)).length,
      unknown: entries.filter(s => s.status === "answer_unknown").length,
      updatesPending: entries.filter(s => s.messageId && s.patchedRevision !== s.revision).length,
      callbacks: this.state.callbackStats ?? { received: 0, accepted: 0, rejected: 0, lastOutcome: null } };
  }

  async publish(input) {
    const request = normalizeQuestion(input); const hash = fingerprint(request);
    const active = this.inflight.get(request.questionId);
    if (active) {
      if (active.hash !== hash) reject(409, "question_conflict", "The same questionId cannot identify different question text or targets.");
      return { ...await active.task, duplicate: true };
    }
    const task = this.deliver(request, hash);
    this.inflight.set(request.questionId, { hash, task });
    try { return await task; } finally { this.inflight.delete(request.questionId); }
  }

  async deliver(request, hash) {
    const m = this.manager; let record; let duplicate;
    await m.serial(async () => {
      m.session(request.threadId);
      if (m.state.selectedThreadId !== request.threadId) reject(409, "selected_target_changed", "Questions must target the current Feishu-selected chat; no fallback was used.");
      m.state.publicQuestions ??= {};
      record = m.state.publicQuestions[request.questionId]; duplicate = Boolean(record);
      if (record && record.hash !== hash) reject(409, "question_conflict", "The same questionId cannot identify different question text or targets.");
      let sourceTurnId;
      if (!record) {
        const snapshot = m.deliveryState ? await m.deliveryState(m.session(request.threadId)) : await m.read(request.threadId, m.session(request.threadId).hostId);
        sourceTurnId = snapshot.deliveryObservation
          ? (["inProgress", "in_progress", "running", "started"].includes(snapshot.deliveryObservation.turnStatus) ? snapshot.deliveryObservation.turnId : null)
          : snapshot.turns?.find(t => ["inProgress", "in_progress", "running", "started"].includes(t.status))?.id ?? null;
        if (request.sourceTurnId && request.sourceTurnId !== sourceTurnId) reject(409, "source_turn_ended", "The explicit source turn is no longer active.");
      }
      record ??= m.state.publicQuestions[request.questionId] = { ...request, hash, createdAt: new Date().toISOString(),
        boundSourceTurnId: sourceTurnId, bindingEpoch: m.state.selectionEpoch,
        chatId: m.session(request.threadId).feishuChatId ?? m.config.feishu.chatId ?? null,
        text: questionText(request, m.config.bridgeName), feishuSent: false, hermesQueued: false, hermesStored: false };
      if (m.config.xiliu?.enabled && m.config.xiliu.url && !record.hermesQueued) {
        // Keep the original four-field report contract. Queue and receipt share
        // one durable state write; exact retries cannot enqueue a second report.
        m.state.outbox.push({ machine: m.config.bridgeName, thread_id: request.threadId, type: "progress", content: record.text });
        record.hermesQueued = true;
      }
      await m.save();
    });
    let action = this.state.entries[request.questionId];
    if (!action) {
      await this.serial(async () => {
        action = this.state.entries[request.questionId] ??= { status: record.boundSourceTurnId ? "open" : "expired", reason: record.boundSourceTurnId ? null : "source_turn_ended",
          sourceTurnId: record.boundSourceTurnId ?? null, bindingEpoch: record.bindingEpoch ?? m.state.selectionEpoch, nonce: randomUUID(), selected: {},
          messageUuid: randomUUID(), revision: 1, patchedRevision: 0 };
        await this.save();
      });
    }
    if (!record.feishuSent && m.notifier && record.chatId) {
      // No unbounded POST retries: after the first possible send the UUID's safe
      // dedup window is fixed, not extended on retries or a business restart.
      if (action.firstSendAt && Date.now() - action.firstSendAt > 50 * 60000) reject(409, "question_send_window_ended", "Inspect the original card; no new question message was sent.");
      await this.serial(async () => { action.firstSendAt ??= Date.now(); await this.save(); });
      const sentRevision = action.revision;
      const sent = await (m.notifier.sendQuestionCard ? m.notifier.sendQuestionCard(questionCard(record, action, m.config.bridgeName), {
        threadId: request.threadId, chatId: record.chatId, uuid: action.messageUuid }) :
        m.notifier.sendMessage(record.text, { title: `${m.config.bridgeName}-提问`, threadId: request.threadId, chatId: record.chatId, uuid: action.messageUuid })).catch(() => null);
      if (sent?.code === 0) {
        await this.serial(async () => { action.messageId = sent.data?.message_id ?? null; action.patchedRevision = sentRevision; action.cardLayoutVersion = QUESTION_CARD_LAYOUT_VERSION; await this.save(); });
        await m.serial(async () => { record.feishuSent = true; await m.save(); });
      }
    }
    await m.reportTask;
    await m.flushReports();
    return { questionId: request.questionId, threadId: request.threadId, duplicate,
      feishu: record.feishuSent ? "sent" : m.notifier && record.chatId ? "pending" : "unavailable",
      hermes: record.hermesStored ? "stored" : record.hermesQueued ? "queued" : "unavailable",
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
      if (s.status !== "open") return toast(s.status === "answered" ? "此提问已经回答，不会重复投递。" : "此提问已关闭或正在处理，不会重复投递。", "info");
      if (!s.sourceTurnId || !q.questions.every(q => q.options)) return toast("请直接回复完整答案。", "info");
      if (action === "select") {
        if (!Number.isInteger(qi) || !Number.isInteger(oi) || !q.questions[qi]?.options?.[oi]) return toast("无效选项。", "error");
        s.selected[qi] = oi;
      } else if (action !== "submit") return toast("无效操作。", "error");
      const complete = q.questions.every((_, i) => Number.isInteger(s.selected[i]));
      if (action === "submit" && !complete) return toast("请先为每一道题选择答案。", "warning");
      if (complete && (q.questions.length === 1 || action === "submit")) {
        s.answers = q.questions.map((q, i) => q.options[s.selected[i]]);
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
        if (s.status === "answer_pending" && source.active && !m.handoffPaused && !m.closed) {
          const answers = s.answers;
          const text = `提问 ${id.slice(0, 8)} 的回答：\n` + q.questions.map((q, i) => `${i + 1}）${q.title}\n答案：${answers[i]}`).join("\n\n");
          await this.serial(async () => { if (s.status === "answer_pending") { s.status = "dispatching"; s.revision++; await this.save(); } });
          if (s.status === "dispatching") {
            try {
              await m.message({ protocolVersion: "1.1.1-draft", messageId: s.nativeMessageId, source: "trainer",
                text: `飞书：${text}`, targetThreadId: q.threadId, delivery: "steer" },
                { requireSelectedTarget: true, requireSelectionEpoch: s.bindingEpoch, requireSourceTurnId: s.sourceTurnId });
              await this.serial(async () => { s.status = "answered"; s.reason = null; s.revision++; await this.save(); });
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
      if (!m.closed && !m.handoffPaused && !["open", "answer_pending", "dispatching"].includes(s.status) && m.config.xiliu?.enabled && m.config.xiliu.url) {
        await m.serial(async () => {
          if (q.lifecycleReportedStatus === s.status) return;
          const label = { answered: "已回答", expired: "已过期", canceled: "已取消", rejected: "答案未投递", answer_unknown: "投递结果待核实，不自动重发" }[s.status];
          const content = `${m.config.bridgeName}提问 ${id.slice(0, 8)} · ${label}\n` + q.questions.map((q, i) =>
            `${i + 1}）${q.title}${q.options ? "\n" + q.options.map((o, j) => `${j + 1}）${o}`).join("\n") : ""}${s.answers?.[i] ? "\n所选答案：" + s.answers[i] : ""}`).join("\n\n");
          // Same durable outbox/receipt write, same existing four-field contract.
          m.state.outbox.push({ machine: m.config.bridgeName, thread_id: q.threadId, type: "progress", content });
          q.lifecycleReportedStatus = s.status; await m.save();
        });
      }
    }
  }
}
