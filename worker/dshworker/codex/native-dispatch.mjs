// Routing metadata is private to the adapter, never part of the model prompt.
// Business-generation handoff must preserve these native source/target rules.
import path from "node:path";
export const localHost = hostId => hostId == null || hostId === "local";

export function nativeToolRejection(name, response) {
  const error = new Error(`Desktop rejected ${name}`);
  // Only a complete native tool rejection proves the transport answered. Never
  // carry its raw body (history, paths or credentials) into diagnostics.
  error.nativeReachable = response?.success === false;
  if (name === "read_thread" && response?.success === false && response.contentItems?.some(item =>
    typeof item.text === "string" && /^(?:Tool response exceeds the size limit|Thread history exceeds the read size limit)\./.test(item.text))) {
    error.nativeErrorCode = "native_snapshot_too_large";
  }
  if (name === "read_thread" && response?.success === false && !error.nativeErrorCode &&
      response.contentItems?.some(item => item.text?.trim() === "Codex app tool request failed")) {
    // Newer Desktop builds can reject a bulk page without exposing size/error
    // details. A smaller read-only window may still work; this is not pipe loss.
    error.nativeErrorCode = "native_read_rejected";
  }
  return error;
}

export function nativeSendSource(config, session) {
  return config.nativeSendFromTarget && localHost(session.hostId)
    ? session.threadId : config.nativeSenderThreadId;
}

export function nativeToolInvocation(name, args, binding, deliveryContext = {}) {
  if (name !== "send_message_to_thread" || !localHost(args.hostId)) {
    return { threadId: binding.managerThreadId, turnId: binding.managerTurnId ?? undefined };
  }
  if (!args.threadId || deliveryContext.sourceThreadId !== args.threadId) {
    throw new Error("Native local delivery must originate from its registered target task.");
  }
  return { threadId: args.threadId, turnId: deliveryContext.turnId ?? undefined };
}

// wait_threads is scoped to the caller's current real turn, even when the
// target is another chat. A saved binding turn is not a live invocation.
export async function nativeStatusInvocation(binding, manager, callerSession) {
  if (!manager || callerSession?.threadId !== binding.managerThreadId) throw new Error("native_status_caller_unavailable");
  const session = manager.state.sessions[binding.managerThreadId] ?? (manager.state.nativeStatusCaller ??= callerSession);
  if (session.threadId !== binding.managerThreadId || typeof session.cwd !== "string" || typeof callerSession.cwd !== "string" ||
      path.resolve(session.cwd).toLowerCase() !== path.resolve(callerSession.cwd).toLowerCase()) throw new Error("native_status_caller_mismatch");
  const snapshot = await manager.journalDeliveryState(session);
  const turnId = snapshot.deliveryObservation?.turnId;
  if (typeof turnId !== "string" || !turnId) throw new Error("native_status_caller_unavailable");
  return { threadId: binding.managerThreadId, turnId };
}
