// Pure helpers for router tasks.
//
// A router task is not a new Codex thread: the CLI enqueues it into the manager's
// selected session (the same conversation the Feishu chat uses) and then finds
// its own turn again by a marker. Everything here is side-effect free so the
// queue/format rules are unit-testable without a native transport.
import { createHash } from "node:crypto";

export const ROUTER_TURN_LIMIT_MS = 20 * 60 * 1000;
export const TERMINAL_TURN = new Set(["completed", "interrupted", "failed", "cancelled", "canceled"]);

// Deterministic per taskId: a router retry (at-least-once delivery) reuses the
// same manager messageId, so the queue can never run the same goal twice.
export function routerMessageId(taskId) {
  const hex = createHash("sha256").update(`router:${taskId}`).digest("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

// Marks the enqueued turn in the shared conversation so the runner can find its
// own turn even when a Feishu message interleaves in the same thread.
export function routerMarker(taskId) {
  return `[router ${taskId}]`;
}

export function routerPrompt({ taskId, goal, context }) {
  const body = (context == null ? ""
    : typeof context === "string" ? context : JSON.stringify(context)).trim();
  return `${routerMarker(taskId)} ${String(goal ?? "").trim()}` + (body ? `\n\n[router context]\n${body}` : "");
}

export function clipOneLine(value, limit) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export function clipBlock(value, limit) {
  const text = String(value ?? "").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export function itemText(item) {
  if (typeof item?.text === "string") return item.text;
  if (Array.isArray(item?.content)) return item.content.map((part) => part?.text ?? "").join("\n");
  return "";
}

// Newest turn whose input is this task's prompt.
export function findRouterTurn(snapshot, marker) {
  const turns = snapshot?.turns ?? [];
  for (let index = turns.length - 1; index >= 0; index--) {
    const turn = turns[index];
    if ((turn.items ?? []).some((item) => item?.type === "userMessage" && itemText(item).includes(marker))) return turn;
  }
  return null;
}

// Prefer the announced final answer; fall back to the last assistant message.
export function lastAssistantText(turn) {
  const messages = (turn?.items ?? [])
    .filter((item) => item?.type === "agentMessage" && typeof item.text === "string" && item.text.trim());
  const final = [...messages].reverse().find((item) => item.phase === "final_answer" || item.phase === "final");
  return (final ?? messages.at(-1))?.text?.trim() ?? "";
}

// Scheme A: the trainer's p2p chat with this worker gets exactly three
// lifecycle lines — start, done, failed — and never intermediate output.
export function taskBoardText({ phase, taskId, goal = "", summary = "", failure = "" }) {
  if (phase === "start") return `任务 ${taskId} 开始 · ${clipOneLine(goal, 60)}`;
  if (phase === "done") return `完成 · ${taskId} · ${clipBlock(summary, 800)}`;
  if (phase === "failed") return `失败 · ${taskId} · ${clipBlock(failure || summary, 800)}`;
  throw new Error(`unknown task board phase: ${phase}`);
}
