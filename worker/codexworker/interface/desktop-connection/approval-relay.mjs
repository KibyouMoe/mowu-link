import { requestPermissionReview } from "../permission-client.mjs";
import { isToolApproval } from "../native-approval.mjs";

// This relay runs inside the actual Desktop-owned connection. It never
// resumes a thread or opens another client, and preserves the native prompt.
export function createApprovalRelay({ send, review = requestPermissionReview, log = () => {} }) {
  const pending = new Map();
  const answered = new Set();
  function resolved(id) {
    pending.get(id)?.abort();
    pending.delete(id);
  }
  return {
    server(message) {
      if (message?.method === "serverRequest/resolved") resolved(message.params?.requestId);
      if (!isToolApproval(message) || pending.has(message.id)) return;
      answered.delete(message.id);
      const controller = new AbortController();
      pending.set(message.id, controller);
      log("native_approval_received", { requestId: message.id, threadId: message.params.threadId });
      Promise.resolve().then(() => review({ type: "desktop-server-request", request: message },
        { signal: controller.signal })).then(result => {
        if (pending.get(message.id) !== controller) return;
        const response = result?.nativeResponse;
        if (!response || !["accept", "decline"].includes(response.action)) return;
        resolved(message.id);
        send({ id: message.id, result: { action: response.action,
          content: response.action === "accept" ? {} : null } });
        answered.add(message.id);
        if (answered.size > 1024) answered.delete(answered.values().next().value);
        log("native_approval_resolved", { requestId: message.id, approvalId: result.approvalId,
          action: response.action });
      }).catch(() => {
        // The native UI is still available if the local adapter cannot decide.
        if (!controller.signal.aborted) log("native_approval_review_failed", { requestId: message.id });
      }).finally(() => {
        if (pending.get(message.id) === controller) pending.delete(message.id);
      });
    },
    client(message) {
      if (message?.id !== undefined && !message.method && answered.has(message.id)) return false;
      if (message?.id !== undefined && !message.method) resolved(message.id);
      return true;
    },
    close() { for (const id of pending.keys()) resolved(id); answered.clear(); },
  };
}
