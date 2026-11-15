import { isNativeType, nativeMessageText } from "./interface/native-content.mjs";
// Pure helpers for router tasks.
//
// A router task is not a new Codex thread: the CLI enqueues it into the manager's
// selected session (the same conversation the Feishu chat uses) and then finds
// its own turn again by a marker. Everything here is side-effect free so the
// queue/format rules are unit-testable without a native transport.
import { createHash } from "node:crypto";

export const ROUTER_TURN_LIMIT_MS = Infinity;
export const SUBSCRIPTION_NOTICE_LIMIT = 80;
export const TERMINAL_TURN = new Set(["completed", "interrupted", "failed", "cancelled", "canceled"]);

// Deterministic per taskId: a router retry (at-least-once delivery) reuses the
// same manager messageId, so the queue can never run the same goal twice.
export function routerMessageId(taskId) {
  const hex = createHash("sha256").update(`router:${taskId}`).digest("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

function sha256Uuid(seed) {
  const hex = createHash("sha256").update(seed).digest("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

// One subscription event == one manager message. The trigger event id is only
// unique per source, so the dedupe key is (source, id).
export function subscriptionMessageId({ source, id }) {
  return sha256Uuid(`subscription:${String(source ?? "")}:${String(id ?? "")}`);
}

// Same marker shape as the message prefix, so the runner can find its turn
// even when the trainer types into the same thread meanwhile.
export function subscriptionMarker({ source, kind, id }) {
  return `[订阅事件 source=${source} kind=${kind} id=${id}]`;
}

// The Feishu line is deliberately one glanceable row: 订阅事件 · source · body.
export function subscriptionNoticeText({ source, text, limit = SUBSCRIPTION_NOTICE_LIMIT }) {
  const body = String(text ?? "").replace(/\s+/g, " ").trim();
  const clipped = body.length > limit ? `${body.slice(0, limit)}…` : body;
  return `订阅事件 · ${String(source ?? "").trim() || "(unknown)"} · ${clipped}`;
}

// Config shape: [{source, kinds?}] — anything else is ignored, never fatal.
export function normalizeSubscriptionConfig(value) {
  if (!Array.isArray(value)) return [];
  const out = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const source = typeof item.source === "string" ? item.source.trim() : "";
    if (!source) continue;
    const rawKinds = item.kinds;
    const kinds = Array.isArray(rawKinds)
      ? rawKinds.map((kind) => String(kind).trim()).filter(Boolean)
      : typeof rawKinds === "string" ? rawKinds.split(",").map((kind) => kind.trim()).filter(Boolean) : [];
    out.push({ source, kinds });
  }
  return out;
}

// Marks the enqueued turn in the shared conversation so the runner can find its
// own turn even when a Feishu message interleaves in the same thread.
export function routerMarker(taskId) {
  return `[router ${taskId}]`;
}

// Router tasks are dispatched by 溪流 (the trainer's secretary), not by the
// trainer themself. The prompt has to say so: without it the agent answers
// "训练员" and the trainer sees a task that pretends to come from him.
export const ROUTER_ORIGIN = "这条指令来自超级溪流（训练员的秘书，负责向各 worker 派活），不是训练员本人；回复时称呼「溪流」。";

export const TASK_GOAL_INSTRUCTIONS = "[正式工单 · 目标模式]\n先核对原生目标状态（支持时调用 get_goal）；没有未完成目标才调用 create_goal 建立本工单目标，不自设 token 预算。已有目标时先关联本工单的补充或子项；若是独立冲突项目，保留原目标并说明冲突，不能擅自替换、完成或暂停。以原生工具实际返回为准，执行、验证后按真实完成状态回传。普通聊天不建立工单或目标。";

export function routerPrompt({ taskId, goal, context }) {
  const body = (context == null ? ""
    : typeof context === "string" ? context : JSON.stringify(context)).trim();
  return `${routerMarker(taskId)} 来自超级溪流的指令（任务 ${taskId}）\n${ROUTER_ORIGIN}\n\n${TASK_GOAL_INSTRUCTIONS}\n\n${String(goal ?? "").trim()}`
    + (body ? `\n\n[router context]\n${body}` : "");
}

export function clipBlock(value, limit) {
  const text = String(value ?? "").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export function itemText(item) {
  return nativeMessageText(item);
}

// Newest turn whose input is this task's prompt.
export function findRouterTurn(snapshot, marker) {
  const turns = snapshot?.page?.order === "newest_first" ? [...(snapshot.turns ?? [])].reverse() : snapshot?.turns ?? [];
  for (let index = turns.length - 1; index >= 0; index--) {
    const turn = turns[index];
    if ((turn.items ?? []).some((item) => isNativeType(item?.type, "UserMessage") && itemText(item).includes(marker))) return turn;
  }
  return null;
}

// Prefer the announced final answer; fall back to the last assistant message.
export function lastAssistantText(turn) {
  const messages = (turn?.items ?? [])
    .filter((item) => isNativeType(item?.type, "AgentMessage") &&
      (item.phase == null || ["commentary", "final", "final_answer"].includes(item.phase)))
    .map(item => ({ ...item, text: nativeMessageText(item) })).filter(item => item.text.trim());
  const final = [...messages].reverse().find((item) => item.phase === "final_answer" || item.phase === "final");
  return (final ?? messages.at(-1))?.text?.trim() ?? "";
}

// Scheme A: the trainer's p2p chat with this worker gets one start line, plus a
// failure line only when the task fails — never intermediate output, and never
// a "完成" line (the manager's own reply in the same chat already carries that
// body). The start line is published as 溪流's instruction: the card title is
// `来自超级溪流的指令（任务 <taskId>）` and the body is the goal verbatim (a
// 4000-char guard only exists to stay far away from Feishu's 30KB card limit).
export const TASK_BOARD_GOAL_LIMIT = 4000;

export function taskBoardTitle({ phase, taskId }) {
  return phase === "start" ? `来自超级溪流的指令\n（任务 ${taskId}）` : null;
}

export function taskBoardTerminalPhase(status) {
  return status === "done" ? null : "failed";
}

export function taskBoardText({ phase, taskId, goal = "", summary = "", failure = "" }) {
  if (phase === "start") {
    const text = String(goal ?? "").trim();
    if (!text) return "（无目标文本）";
    return text.length > TASK_BOARD_GOAL_LIMIT ? `${text.slice(0, TASK_BOARD_GOAL_LIMIT)}…（已截断）` : text;
  }
  if (phase === "failed") return `失败 · ${taskId} · ${clipBlock(failure || summary, 800)}`;
  throw new Error(`unknown task board phase: ${phase}`);
}
