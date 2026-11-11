import { setTimeout as delay } from "node:timers/promises";

export const APPROVAL_TIMEOUT_MS = 540_000;
export const APPROVAL_POLL_MS = 1_500;

// The answerer must settle even if a caller-supplied router method never does.
export function abortable(work, signal) {
  if (signal.aborted) return Promise.reject(signal.reason ?? new Error("cancelled"));
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason ?? new Error("cancelled"));
    signal.addEventListener("abort", abort, { once: true });
    Promise.resolve(work).then(resolve, reject).finally(() =>
      signal.removeEventListener("abort", abort));
  });
}

export function createDshApprovalBridge({
  router = null, log = () => {}, timeoutMs = APPROVAL_TIMEOUT_MS,
  pollMs = APPROVAL_POLL_MS, sleep = (ms, signal) => delay(ms, null, { signal }),
} = {}) {
  const pending = new Set();
  const emit = (...args) => { try { log(...args); } catch { /* logging cannot strand a turn */ } };
  let closed = false;
  return {
    setRouter(value) { router = value; },
    close() { closed = true; for (const c of pending) c.abort(new Error("plugin closed")); },
    async request(request, { taskId = null, threadId = null, turnId = null,
      progress = () => {} } = {}) {
      const controller = new AbortController();
      pending.add(controller);
      const signal = request.signal
        ? AbortSignal.any([request.signal, controller.signal]) : controller.signal;
      const budget = Math.min(APPROVAL_TIMEOUT_MS, Math.max(1, Number(timeoutMs) || APPROVAL_TIMEOUT_MS));
      const timer = setTimeout(() => controller.abort(new Error("approval timeout")), budget);
      const reason = String(request.reason ?? "DSH tool approval");
      const record = { approval_id: null, state: "submitting", toolName: request.toolName,
        reason, session_id: request.agent?.session?.id ?? null,
        task_id: taskId, thread_id: threadId, turn_id: turnId, call_id: request.callId ?? null };
      let outcome = "rejected";
      const report = () => { try { progress({ ...record }); } catch (error) {
        emit("warn", "approval_progress_failed", { message: error.message }); } };
      try {
        emit("info", "approval_request", { ...record });
        report();
        if (closed || !router?.requestApproval || !router?.getApproval)
          throw new Error("router approval channel unavailable");
        if (signal.aborted) throw signal.reason;
        const posted = await abortable(router.requestApproval({
          taskId, kind: "approval", title: reason.slice(0, 160),
          detail: JSON.stringify({ ...record, native_approval_id: request.id ?? null }),
          ttlMs: budget,
        }), signal);
        if (!posted?.approval_id) throw new Error("router returned no approval id");
        record.approval_id = posted.approval_id;
        record.state = "waiting_approval";
        record.text = `等待审批 ${record.approval_id}（${reason}）`;
        report();
        let approval = posted;
        for (;;) {
          if (signal.aborted) throw signal.reason;
          if (approval?.status === "approved") {
            record.state = "approved"; outcome = "allowed-once"; break;
          }
          if (approval?.status === "denied" || approval?.status === "expired") {
            record.state = approval.status; break;
          }
          if (approval?.status !== "pending") throw new Error("invalid router approval status");
          await abortable(sleep(pollMs, signal), signal);
          approval = await abortable(router.getApproval(record.approval_id), signal);
        }
      } catch (error) {
        record.state = signal.aborted
          ? (controller.signal.reason?.message === "approval timeout" ? "timeout" : "cancelled") : "error";
        record.error = error?.message ?? String(error);
      } finally {
        clearTimeout(timer);
        pending.delete(controller);
        report();
        emit("info", "approval_resolved", { ...record, outcome });
      }
      return outcome;
    },
  };
}
