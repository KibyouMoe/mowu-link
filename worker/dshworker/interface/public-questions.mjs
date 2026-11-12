// Project only the native public async question fields. Never infer questions
// from prose, reasoning, approvals, tool arguments or tool outputs.
import { createHash } from "node:crypto";
export function projectPublicQuestion(item, { threadId, turnId }) {
  if (item?.type !== "AgentMessage" || item.delivery !== "async" ||
      !["commentary", "final", "final_answer"].includes(item.phase) ||
      typeof item.id !== "string" || !item.id || typeof threadId !== "string" ||
      typeof turnId !== "string" || !Array.isArray(item.questions) ||
      item.questions.length < 1 || item.questions.length > 3) return null;
  const questions = [];
  for (const q of item.questions) {
    if (typeof q?.title !== "string" || !q.title.trim() || q.title.length > 8000 ||
        q.options != null && (!Array.isArray(q.options) || q.options.length < 2 || q.options.length > 6 ||
        q.options.some(s => typeof s !== "string" || !s.trim() || s.length > 1000))) return null;
    questions.push({ title: q.title.trim(), ...(q.options ? { options: q.options.map(s => s.trim()) } : {}) });
  }
  const h = createHash("sha256").update(JSON.stringify(["public-question", threadId, turnId, item.id])).digest("hex");
  const questionId = h.slice(0,8)+"-"+h.slice(8,12)+"-4"+h.slice(13,16)+"-8"+h.slice(17,20)+"-"+h.slice(20,32);
  return { questionId, threadId, sourceTurnId: turnId, delivery: "async", questions,
    sourceQuestionItemIds: questions.map((_, i) => JSON.stringify(["request_user_input_async", item.id, i])) };
}
