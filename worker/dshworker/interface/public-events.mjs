// DSH native events -> the shared manager's public message/tool contract.
// Only display text and allowlisted tool previews leave this boundary.
import { projectToolActivity } from "./tool-activity.mjs";

export class DshPublicEvents {
  constructor(transport) { this.transport = transport; this.manager = null; this.calls = new Map(); }
  attach(manager) { this.manager = manager; if (manager) manager.toolActivitySource = "native-notifications"; }
  publish(method, event) {
    try { Promise.resolve(this.manager?.[method]?.(event)).catch(() => this.transport.log("warn", "public_event_delivery_failed", { method })); }
    catch { this.transport.log("warn", "public_event_delivery_failed", { method }); }
  }
  stream(thread, turn, frame) {
    if (frame?.type === "start") { turn.streamAttempt = frame.attemptId; return; }
    if (frame?.type === "end" && frame.outcome?.kind === "abandoned") {
      turn.publicMessages = (turn.publicMessages ?? []).filter(item => item.id !== `dsh-text-${frame.attemptId}`);
      return;
    }
    if (frame?.type !== "chunk" || frame.chunk?.type !== "text-delta" || typeof frame.chunk.text !== "string") return;
    const id = `dsh-text-${frame.attemptId ?? turn.streamAttempt ?? turn.id}`;
    turn.publicMessages ??= [];
    let item = turn.publicMessages.find(item => item.id === id);
    if (!item) { item = { id, text: "" }; turn.publicMessages.push(item); }
    item.text += frame.chunk.text;
  }
  event(thread, turn, agent, session, event) {
    if (!this.transport.agentsForTurn(agent).some(subject => subject.session === session)) return;
    const data = event?.data ?? {};
    if (event?.type === "assistant/message" && session === agent.session) {
      const text = (Array.isArray(data.message?.content) ? data.message.content : []).filter(block => block?.type === "text" && typeof block.text === "string").map(block => block.text).join("");
      if (text) {
        turn.publicMessages ??= [];
        const id = `dsh-text-${turn.streamAttempt ?? data.message.id ?? event.seq}`;
        let item = turn.publicMessages.find(item => item.id === id);
        if (!item) { item = { id, text }; turn.publicMessages.push(item); }
        else item.text = text;
      }
    }
    const context = { threadId: thread.id, turnId: turn.id };
    if (event?.type === "tool/call") {
      let args = data.arguments;
      if (typeof args === "string") { try { args = JSON.parse(args); } catch { args = {}; } }
      const terminal = ["bash", "terminal", "pwsh"].includes(data.name);
      const item = { id: `${session.id}:${data.callId}`, type: terminal ? "commandExecution" : "dynamicToolCall",
        name: data.name, command: args?.command ?? args?.cmd, arguments: args, status: "inProgress" };
      const activity = projectToolActivity(item, context);
      if (activity) { this.calls.set(item.id, activity); this.publish("observeToolActivity", activity); }
    }
    if (event?.type === "tool/result") {
      const id = `${session.id}:${data.message?.toolCallId ?? data.message?.source?.callId}`, prior = this.calls.get(id);
      if (prior) {
        this.calls.delete(id);
        this.publish("observeToolActivity", { ...prior, status: data.message?.isError ? "failed" : "completed", timestamp: new Date().toISOString() });
      }
    }
    if (["compaction/start", "compaction/end"].includes(event?.type))
      this.publish("observeContextEvent", { ...context, kind: "compaction", itemId: `dsh-compaction-${data.compactionId ?? data.id ?? turn.id}`,
        status: event.type === "compaction/start" ? "in_progress" : data.error ? "failed" : "completed", timestamp: new Date().toISOString() });
  }
  finish(thread, turn) {
    this.publish("observeToolActivity", { kind: "turn_end", threadId: thread.id, turnId: turn.id, status: turn.status });
    this.publish("observeContextEvent", { kind: "turn_end", threadId: thread.id, turnId: turn.id, status: turn.status });
    for (const [id, item] of this.calls) if (item.turnId === turn.id) this.calls.delete(id);
  }
}
