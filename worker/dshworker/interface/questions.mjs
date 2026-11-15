import { dshQuestionIdentity, parseDshIdentity, projectPublicQuestion, nativeQuestionReply, SKIPPED_QUESTION_ANSWER } from "./public-questions.mjs";

// Bind public clarification cards to exact DSH requests. Approval/plan intents
// remain with their native handler, never the clarification answer channel.
export class DshQuestions {
  constructor(transport) {
    this.transport = transport; this.pending = new Map();
    this.stop = transport.ctx.on("user-questions/request", (request, next) => this.request(request, next), { prepend: true });
  }
  async request(request, next) {
    const t = this.transport;
    const owned = [...t.live.entries()].find(([, entry]) => entry.agent === request.agent);
    const thread = owned && t.threads.get(owned[0]);
    const turn = thread?.turns.find(x => x.status === "inProgress" && !thread.queue.includes(x.id));
    if (!turn || !request.questions?.length || request.questions.length > 3 || request.questions.some(q => q.intent)) return next();
    const agent = request.agent, sessionId = agent.session.id;
    let callId = request.wait?.callId;
    if (!callId) for (let n = agent.session.seq - 1; n >= 0; n--) {
      const event = agent.session.eventAt(n);
      if (event?.type === "tool/call" && event.data.name === "ask_user_question") { callId = event.data.callId; break; }
    }
    if (!callId) return next();
    const item = { type: "agentMessage", kind: "assistant", id: `dsh-question-${sessionId}-${callId}`,
      phase: "commentary", delivery: "async", text: request.questions.map(q => q.question).join("\n"),
      questions: request.questions.map(q => ({ title: q.question,
        ...(!q.multiSelect && q.options?.length >= 2 && q.options.length <= 6 ? { options: q.options.map(o => o.label) } : {}) })),
      dshQuestion: { sessionId, callId, ids: request.questions.map(q => q.id) } };
    (turn.nativeQuestions ??= []).push(item);
    const key = `${sessionId}:${callId}`;
    let resolve, reject;
    const answered = new Promise((yes, no) => { resolve = yes; reject = no; });
    const entry = { request, thread, turn, resolve, reject, item };
    this.pending.set(key, entry);
    turn.waitingForInput = true; turn.eventSeq = (turn.eventSeq ?? 0) + 1;
    const abort = () => reject(request.signal.reason ?? new Error("question cancelled"));
    request.signal?.addEventListener("abort", abort, { once: true });
    if (request.signal?.aborted) abort();
    // Keep the native Desktop answerer active too. A missing Desktop provider
    // does not cancel a question which the Feishu/router channel can answer.
    Promise.resolve().then(() => next()).then(answer => {
      if (entry.relayAnswered) return;
      entry.nativeChosen = true; resolve(answer);
    }, error => {
      if (error?.code !== "NO_PROVIDER") reject(error);
    });
    try {
      const answer = await answered;
      item.dshQuestionAnswers = request.questions.map(q => {
        const a = answer.answers.find(a => a.id === q.id);
        return { questionItemId: dshQuestionIdentity(sessionId, callId, q.id),
          answer: a?.custom || a?.selected?.join("、") || SKIPPED_QUESTION_ANSWER };
      });
      if (!entry.relayAnswered) {
        const question = projectPublicQuestion(item, { threadId: thread.id, turnId: turn.id });
        (turn.inputs ??= []).push({ id: `dsh-answer-${callId}`,
          text: nativeQuestionReply(question, item.dshQuestionAnswers.map(a => a.answer)) });
      }
      return answer;
    } finally {
      request.signal?.removeEventListener("abort", abort);
      this.pending.delete(key); turn.waitingForInput = false;
      turn.eventSeq = (turn.eventSeq ?? 0) + 1;
      await t.save();
    }
  }
  prepare(thread, replies) {
    const identities = replies.map(r => parseDshIdentity(r.questionItemId));
    const [, sessionId, callId] = identities[0] ?? [];
    if (sessionId !== thread.sessionId || identities.some(p => !p || p[1] !== sessionId || p[2] !== callId))
      throw new Error("question_owner_mismatch");
    const entry = this.pending.get(`${sessionId}:${callId}`);
    const agent = this.transport.live.get(thread.id)?.agent;
    const service = agent?.ctx?.get("userQuestions") ?? this.transport.ctx.get("userQuestions");
    const continued = !entry && agent && service?.continued?.(agent).find(q => q.callId === callId);
    const questions = entry?.request.questions ?? continued?.questions;
    if (!questions || questions.length !== replies.length || !questions.every(q => replies.some((r, i) => identities[i][3] === q.id && r.question === q.question)))
      throw new Error("question_no_longer_waiting");
    const answer = { answers: questions.map(q => {
      const text = replies.find((r, i) => identities[i][3] === q.id).answer;
      return { id: q.id, selected: text === SKIPPED_QUESTION_ANSWER ? [] : q.options?.some(o => o.label === text) ? [text] : [],
        ...(text !== SKIPPED_QUESTION_ANSWER && !q.options?.some(o => o.label === text) ? { custom: text } : {}) };
    }) };
    if (entry) {
      if (entry.request.signal?.aborted || entry.nativeChosen || entry.relayAnswered) throw new Error("question_no_longer_waiting");
      entry.relayAnswered = true;
      entry.resolve(answer);
      return { turnId: entry.turn.id, answered: true };
    }
    const owner = thread.turns.find(t => t.nativeQuestions?.some(q => q.dshQuestion?.callId === callId));
    const taskId = owner?.taskId ?? /^\[router (t-[^\]\s]+)\]/m.exec(owner?.prompt ?? "")?.[1] ?? null;
    return { sessionId, callId, answer, taskId };
  }
  continue(agent, reply) {
    const service = agent.ctx?.get("userQuestions") ?? this.transport.ctx.get("userQuestions");
    if (agent.session.id !== reply.sessionId || !service?.answer(agent, reply.callId, reply.answer))
      throw new Error("question_no_longer_waiting");
  }
  close() { this.stop?.(); for (const p of this.pending.values()) p.reject(new Error("plugin closed")); }
}
