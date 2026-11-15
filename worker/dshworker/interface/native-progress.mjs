// A real native progress snapshot, not a synthetic history page. It can expose
// only the latest public text; intermediate messages/images/inputs are unknown.
export function nativeTaskState(response, threadId) {
  const poll = response?.polls?.find(value => value?.thread?.id === threadId && (value.thread.hostId ?? "local") === "local");
  if (!poll || typeof poll.latestTurn?.id !== "string" || !poll.latestTurn.id || typeof poll.latestTurn.status !== "string") throw new Error("native_progress_unavailable");
  return { turnId: poll.latestTurn.id, turnStatus: poll.latestTurn.status };
}
export function publicProgress(response, threadId) {
  const state = nativeTaskState(response, threadId);
  const poll = response.polls.find(value => value?.thread?.id === threadId && (value.thread.hostId ?? "local") === "local");
  let message = poll.latestAssistantMessage;
  if (message && (typeof message.id !== "string" || !message.id || typeof message.turnId !== "string" || !message.turnId ||
      message.id !== poll.latestAssistantMessageId || typeof message.text !== "string" || message.truncated === true)) {
    throw new Error("native_progress_incomplete");
  }
  // The latest public message may be the previous turn's final while a new
  // turn has no text yet. Keep its real turnId; never assign it to latestTurn.
  if (message && !["commentary", "final", "final_answer"].includes(message.phase)) message = null;
  return { cursor: typeof poll.cursor === "string" ? poll.cursor : null,
    ...state,
    message: message ? { id: message.id, turnId: message.turnId, text: message.text,
      final: ["final", "final_answer"].includes(message.phase) } : null };
}
