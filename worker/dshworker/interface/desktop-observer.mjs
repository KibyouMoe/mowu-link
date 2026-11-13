// Observe native Desktop turns in explicitly bound sessions. Never create an
// agent, replay history, or subscribe other Desktop conversations to Feishu.
import { projectPublicQuestion, nativeQuestionReply, SKIPPED_QUESTION_ANSWER } from "./public-questions.mjs";

export class DshDesktopObserver {
  constructor(transport) {
    this.transport = transport;
    this.cursors = new WeakMap();
    this.inputs = new Map();
    this.stops = [
      transport.ctx.on("session/event", (session, event) => this.guard(() => this.event(session, event)), { global: true }),
      transport.ctx.on("agent/assistant-stream", ({ agent, frame }) => this.guard(() => {
        const owner = this.owner(agent.session);
        const turn = owner && this.active(owner.thread);
        if (turn?.nativeDesktop && owner.agent === agent) {
          transport.publicEvents.stream(owner.thread, turn, frame);
          if (frame?.type === "chunk" && frame.chunk?.type !== "heartbeat") turn.eventSeq++;
        }
      })),
    ];
  }
  guard(fn) {
    if (this.transport.closing) return;
    try { fn(); } catch (error) { this.transport.log("warn", "desktop_observation_failed", { message: error.message }); }
  }
  baseline(agent) { this.cursors.set(agent.session, agent.session.seq - 1); }
  sent(message, turn) { this.inputs.set(message.id, turn?.id); return message; }
  release(turn) { for (const [id, turnId] of this.inputs) if (turnId === turn.id) this.inputs.delete(id); }
  active(thread) { return thread.turns.find(t => t.status === "inProgress" && !thread.queue.includes(t.id)); }
  owner(session) {
    const t = this.transport;
    for (const thread of t.threads.values()) {
      if (!thread.sessionId) continue;
      let agent = t.live.get(thread.id)?.agent;
      if (!agent || agent.status === "disposed") {
        agent = t.ctx.get("agents")?.list?.().find(a => a.session?.id === thread.sessionId && a.status !== "disposed");
        // A Desktop-resumed agent is borrowed, not owned by the plugin.
        if (agent) t.live.set(thread.id, { agent, handle: { dispose: async () => {} } });
      }
      if (agent && t.agentsForTurn(agent).some(a => a.session === session)) return { thread, agent };
    }
    return null;
  }
  event(session, event) {
    const owner = this.owner(session);
    if (!owner) return;
    const { thread, agent } = owner, t = this.transport;
    if (Number.isSafeInteger(event?.seq)) {
      if (event.seq <= (this.cursors.get(session) ?? -1)) return;
      this.cursors.set(session, event.seq);
    }
    const root = session === agent.session;
    let turn = this.active(thread);
    if (root && event.type === "turn/start" && !turn) {
      turn = { id: `dsh-desktop-${session.id}-${event.seq}`, nativeDesktop: true,
        prompt: "", text: "", status: "inProgress", eventSeq: 0,
        startedAt: new Date().toISOString(), completedAt: null, error: null };
      thread.turns.push(turn);
      if (thread.turns.length > 200) thread.turns.splice(0, thread.turns.length - 200);
      thread.running = true; thread.status = "inProgress"; thread.updatedAt = turn.startedAt;
      t.log("info", "desktop_turn_observed", { threadId: thread.id, sessionId: session.id, turnId: turn.id });
      void t.save();
    }
    if (!turn) return;
    if (root && event.type === "user/message") {
      const message = event.data;
      if (this.inputs.delete(message?.id)) return;
      // Runtime context, wakeups, tool messages and synthetic prompts stay native.
      if (message?.source?.kind === "user") {
        const text = (message.content ?? []).map(p => p.type === "text" ? p.text
          : p.type === "image" ? "[DSH 桌面图片附件]" : "").filter(Boolean).join("\n");
        if (text) {
          if (turn.nativeDesktop && !turn.prompt) turn.prompt = text;
          else (turn.inputs ??= []).push({ id: `dsh-input-${message.id ?? event.seq}`, text });
          turn.eventSeq = (turn.eventSeq ?? 0) + 1;
          void t.save();
        }
      } else if (message?.source?.kind === "user-question-reply" && !turn.nativeReply) {
        // A continued question answered in Desktop is a native input in a new
        // turn. Convert only a previously observed question, never arbitrary JSON.
        const callId = message.source.callId;
        const sourceTurn = thread.turns.find(candidate => candidate.nativeQuestions?.some(q => q.dshQuestion.callId === callId));
        const item = sourceTurn?.nativeQuestions.find(q => q.dshQuestion.callId === callId);
        if (item && !turn.inputs?.some(input => input.id === `dsh-answer-${callId}`)) {
          const batch = JSON.parse(message.content.find(p => p.type === "text")?.text ?? "null");
          if (batch?.kind === "answer_to_pending_question" && batch.callId === callId &&
              Array.isArray(batch.answers) && item.dshQuestion.ids.every(id => batch.answers.some(a => a.id === id))) {
            const question = projectPublicQuestion(item, { threadId: thread.id, turnId: sourceTurn.id });
            const answers = item.dshQuestion.ids.map(id => {
              const answer = batch.answers.find(a => a.id === id);
              return answer.custom || answer.selected?.join("、") || SKIPPED_QUESTION_ANSWER;
            });
            const text = nativeQuestionReply(question, answers);
            if (turn.nativeDesktop && !turn.prompt) turn.prompt = text;
            else (turn.inputs ??= []).push({ id: `dsh-answer-${callId}`, text });
            void t.save();
          }
        }
      }
    }
    // Worker turns already have per-turn observers. Public text/tool projection
    // belongs to exactly one listener, including Desktop steering a Worker turn.
    if (!turn.nativeDesktop) return;
    turn.eventSeq++;
    t.publicEvents.event(thread, turn, agent, session, event);
    if (root && event.type === "turn/end") {
      turn.nativeEnd = event.data?.reason ?? { kind: "completed" };
      if (!turn.settling) {
        turn.settling = true;
        void this.settle(thread, turn, agent);
      }
    }
  }
  async settle(thread, turn, agent) {
    const t = this.transport;
    try {
      for (;;) {
        await agent.whenIdle();
        if (await t.ctx.get("subagents")?.waitForChildren?.(agent)) continue;
        if (agent.status === "idle" || agent.status === "disposed") break;
      }
      if (t.closing || turn.status !== "inProgress") return;
      const reason = turn.nativeEnd;
      t.finishTurn(thread, turn, { text: turn.publicMessages?.at(-1)?.text ?? "",
        reasonKind: reason.kind, error: reason.kind === "completed" ? null : reason.error?.message ?? reason.kind });
      t.publicEvents.finish(thread, turn);
      await t.ctx.get("sessions")?.flush?.(agent.session);
      t.log("info", "desktop_turn_completed", { threadId: thread.id, turnId: turn.id, status: turn.status });
    } catch (error) {
      if (!t.closing && turn.status === "inProgress") {
        t.finishTurn(thread, turn, { error: error.message, reasonKind: "error" });
        t.publicEvents.finish(thread, turn);
      }
      t.log("warn", "desktop_turn_settle_failed", { message: error.message });
    } finally { delete turn.settling; this.release(turn); }
  }
  close() { for (const stop of this.stops) stop?.(); this.inputs.clear(); }
}
