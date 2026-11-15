// Dispatch/control provenance, never inferred from arbitrary user text alone.
export function requestInitiator(request) {
  if (request?.source === "hermes") {
    const taskId = typeof request.taskId === "string" && /^t-[a-zA-Z0-9_-]+$/.test(request.taskId) ? request.taskId
      : request.origin === "router" ? /^\[router (t-[^\]\s]+)\]/m.exec(request.text ?? "")?.[1] ?? null : null;
    return { kind: "hermes", taskId, messageId: request.messageId ?? null };
  }
  return { kind: "trainer", messageId: request?.messageId ?? null };
}
export function rememberTurnInitiator(session, turnId, request) {
  if (!turnId || !request) return;
  (session.turnInitiators ??= {})[turnId] ??= requestInitiator(request);
}
export async function questionInitiator(manager, threadId, turnId) {
  const session = manager.session(threadId);
  if (session.turnInitiators?.[turnId]) return structuredClone(session.turnInitiators[turnId]);
  for (const id of session.turnMessages?.[turnId] ?? []) {
    const receipt = manager.state.receipts?.[id] ?? await manager.receiptArchive?.get(id);
    if (receipt?.request) {
      const value = requestInitiator(receipt.request);
      (session.turnInitiators ??= {})[turnId] = value;
      return structuredClone(value);
    }
  }
  return { kind: "trainer" };
}
