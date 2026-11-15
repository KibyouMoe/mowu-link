// Project only the native public async question fields. Never infer questions
// from prose, reasoning, approvals, tool arguments or tool outputs.
import { createHash } from "node:crypto";
// An explicit user skip is not an option selection or a permission decision.
export const SKIPPED_QUESTION_ANSWER = "（用户选择跳过此问题，未提供答案）";
function codexprojectPublicQuestion(item, { threadId, turnId }) {
  if (!["AgentMessage", "agentMessage"].includes(item?.type) || item.delivery !== "async" ||
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

// Desktop derives answered-question state from this exact public reply envelope.
// Keep the original IDs and real answers; prose alone cannot close the panel.
function codexnativeQuestionReply(question, answers) {
  if (question.delivery !== "async") return null;
  if (!Array.isArray(question.sourceQuestionItemIds) ||
      question.sourceQuestionItemIds.length !== question.questions.length ||
      !Array.isArray(answers) || answers.length !== question.questions.length)
    throw new Error("invalid_native_question_reply");
  const replies = question.questions.map((q, i) => ({
    questionItemId: question.sourceQuestionItemIds[i], question: q.title, answer: answers[i],
  }));
  const text = "<send_user_message_question_reply>\n" + JSON.stringify(replies) +
    "\n</send_user_message_question_reply>";
  if (!isNativeQuestionReply(text)) throw new Error("invalid_native_question_reply");
  return text;
}

function codexparseNativeQuestionReply(text) {
  const start = "<send_user_message_question_reply>", end = "</send_user_message_question_reply>";
  if (typeof text !== "string") return null;
  text = text.trim();
  if (!text.startsWith(start) || !text.endsWith(end)) return null;
  try {
    const parsed = JSON.parse(text.slice(start.length, -end.length));
    const replies = Array.isArray(parsed) ? parsed : [parsed], ids = new Set();
    if (!replies.length || replies.length > 3) return null;
    for (const reply of replies) {
      if (typeof reply?.question !== "string" || !reply.question.trim() ||
          typeof reply.answer !== "string" || !reply.answer.trim() ||
          ids.has(reply.questionItemId)) return null;
      const id = JSON.parse(reply.questionItemId);
      if (!Array.isArray(id) || id.length !== 3 || id[0] !== "request_user_input_async" ||
          typeof id[1] !== "string" || !id[1] || !Number.isSafeInteger(id[2]) || id[2] < 0) return null;
      ids.add(reply.questionItemId);
    }
    return replies.map(({questionItemId, question, answer}) => ({questionItemId, question, answer}));
  } catch { return null; }
}
export function isNativeQuestionReply(text) { return parseNativeQuestionReply(text) !== null; }

function codexnativeAnswersInSnapshot(question, snapshot) {
  if (!question.sourceQuestionItemIds) return null;
  const parts = new Map();
  const turns = snapshot.page?.order === "newest_first" ? [...(snapshot.turns ?? [])].reverse() : snapshot.turns ?? [];
  for (const turn of turns) for (const item of turn.items ?? []) {
    if (item.type !== "userMessage") continue;
    const text = item.text ?? item.content?.map(c => c.text ?? "").join("\n") ?? "";
    for (const reply of parseNativeQuestionReply(text) ?? []) {
      const i = question.sourceQuestionItemIds.indexOf(reply.questionItemId);
      if (i >= 0 && reply.question.trim() === question.questions[i].title.trim())
        parts.set(reply.questionItemId, reply.answer);
    }
  }
  const answers = question.sourceQuestionItemIds.map(id => parts.get(id));
  return answers.every(a => typeof a === "string" && a.trim()) ? answers : null;
}

// Validation belongs to the engine adapter; shared relay stores opaque identities.
function codexvalidNativeQuestionId(value) {
  try { const p = JSON.parse(value); return Array.isArray(p) && p.length === 3 &&
    p[0] === "request_user_input_async" && typeof p[1] === "string" && Number.isSafeInteger(p[2]) && p[2] >= 0;
  } catch { return false; }
}

export const dshQuestionIdentity = (sessionId, callId, id) => JSON.stringify(["dsh-user-question", sessionId, callId, id]);
export function parseDshIdentity(value) {
  try { const p = JSON.parse(value); return Array.isArray(p) && p.length === 4 && p[0] === "dsh-user-question" && p.slice(1).every(x => typeof x === "string" && x) ? p : null; }
  catch { return null; }
}
export function validNativeQuestionId(id) { return !!parseDshIdentity(id) || codexvalidNativeQuestionId(id); }
export function projectPublicQuestion(item, context) {
  if (!item?.dshQuestion) return codexprojectPublicQuestion(item, context);
  const projected = codexprojectPublicQuestion(item, context);
  if (!projected) return null;
  const { sessionId, callId, ids } = item.dshQuestion;
  if (!sessionId || !callId || ids?.length !== item.questions.length) return null;
  return { ...projected, sourceQuestionItemIds: ids.map(id => dshQuestionIdentity(sessionId, callId, id)) };
}
const START = "<dsh_user_question_reply>", END = "</dsh_user_question_reply>";
export function parseDshQuestionReply(text) {
  if (typeof text !== "string" || !text.trim().startsWith(START) || !text.trim().endsWith(END)) return null;
  try {
    const replies = JSON.parse(text.trim().slice(START.length, -END.length));
    if (!Array.isArray(replies) || !replies.length || replies.length > 3) return null;
    const ids = new Set();
    for (const r of replies) {
      if (!parseDshIdentity(r.questionItemId) || ids.has(r.questionItemId) || typeof r.question !== "string" ||
          typeof r.answer !== "string" || !r.answer.trim()) return null;
      ids.add(r.questionItemId);
    }
    return replies;
  } catch { return null; }
}
export function parseNativeQuestionReply(text) { return parseDshQuestionReply(text) ?? codexparseNativeQuestionReply(text); }
export function nativeQuestionReply(question, answers) {
  if (!question.sourceQuestionItemIds?.every(id => parseDshIdentity(id))) return codexnativeQuestionReply(question, answers);
  if (answers.length !== question.questions.length) throw new Error("invalid_native_question_reply");
  const text = START + "\n" + JSON.stringify(question.questions.map((q, i) => ({
    questionItemId: question.sourceQuestionItemIds[i], question: q.title, answer: answers[i],
  }))) + "\n" + END;
  if (!parseDshQuestionReply(text)) throw new Error("invalid_native_question_reply");
  return text;
}
export function nativeAnswersInSnapshot(question, snapshot) {
  if (!question.sourceQuestionItemIds?.every(id => parseDshIdentity(id))) return codexnativeAnswersInSnapshot(question, snapshot);
  const answers = new Map();
  for (const turn of snapshot.turns ?? []) for (const item of turn.items ?? [])
    for (const r of item.dshQuestionAnswers ?? []) answers.set(r.questionItemId, r.answer);
  const result = question.sourceQuestionItemIds.map(id => answers.get(id));
  return result.every(x => typeof x === "string" && x) ? result : null;
}
