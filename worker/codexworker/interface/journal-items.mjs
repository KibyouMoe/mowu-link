import { projectPublicQuestion } from "./public-questions.mjs";
import { projectToolActivity } from "./tool-activity.mjs";
import { isNativeType, nativeContentText } from "./native-content.mjs";

const bad = code => { throw Object.assign(new Error(code), { code }); };
export function publicJournalItem(record, threadId, { includeInputs = false, includeTools = false } = {}) {
  if (record?.type !== "event_msg" || record.payload?.type !== "item_completed") return null;
  const p = record.payload, x = p.item;
  if (p.thread_id !== threadId || typeof p.turn_id !== "string" || !p.turn_id || typeof x?.id !== "string" || !x.id) bad("public_journal_identity_mismatch");
  let item;
  if (includeTools) {
    const activity = projectToolActivity(x, { threadId, turnId: p.turn_id, completed: true, timestamp: record.timestamp });
    if (activity) return { turnId: p.turn_id, item: { type: "toolActivity", id: x.id, activity }, timestamp: record.timestamp };
  }
  if (isNativeType(x.type, "AgentMessage") && ["commentary", "final", "final_answer"].includes(x.phase)) {
    const text = nativeContentText(x.content);
    const publicQuestion = projectPublicQuestion(x, { threadId, turnId: p.turn_id });
    item = { type: "agentMessage", id: x.id, text, phase: x.phase, ...(publicQuestion ? { publicQuestion } : {}) };
  } else if (includeInputs && isNativeType(x.type, "UserMessage")) {
    item = { type: "userMessage", id: x.id, text: nativeContentText(x.content, { input: true }) };
  } else if (isNativeType(x.type, "ImageView") && typeof x.path === "string") item = { type: "imageView", id: x.id, path: x.path };
  else if (isNativeType(x.type, "Extension") && x.kind === "image_gen.generation" && x.status === "completed" && typeof x.savedPath === "string" && !x.failure) {
    item = { type: "imageGeneration", id: x.id, status: "completed", savedPath: x.savedPath };
  }
  return item ? { turnId: p.turn_id, item, timestamp: record.timestamp } : null;
}

export function journalInputPaths(record, threadId) {
  if (record?.type !== "event_msg" || record.payload?.type !== "item_completed" || !isNativeType(record.payload?.item?.type, "UserMessage")) return [];
  if (record.payload.thread_id !== threadId) bad("public_journal_identity_mismatch");
  const content = record.payload.item.content;
  if (!Array.isArray(content)) bad("public_journal_public_text_incomplete");
  return content.filter(c => isNativeType(c?.type, "LocalImage") && typeof c.path === "string").map(c => c.path);
}
