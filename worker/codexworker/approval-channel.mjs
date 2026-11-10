// 审批通道：app-server 的 server→client 审批请求 → router /approvals → 溪流
// 批准/拒绝 → 回写 app-server。
//
// 这里只做三件事：
//   1. 把 `{method, kind, title, detail, params}` 变成一张 router 审批单；
//   2. 轮询 `GET /approvals/{id}` 直到 approved / denied / expired；
//   3. 返回统一口径 `{decision: "approved" | "denied", note, approvalId, reason}`。
//
// 失败一律返回 denied，绝不抛给 transport：把异常留给上层只会让 turn 挂着。
// 超时时间默认 9 分钟，落在 router 的 10 分钟 TTL 之内，这样 worker 等到的
// 一定是一个明确结论（approved / denied / expired），而不是永远 pending。
import process from "node:process";

export const DEFAULT_TIMEOUT_MS = 540_000;
export const DEFAULT_POLL_MS = 1_500;
const MIN_TIMEOUT_MS = 5_000;
const MAX_TIMEOUT_MS = 600_000;
const MIN_POLL_MS = 200;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));

export function approvalTimeoutMs() {
  const raw = Number(process.env.CODEXWORKER_APPROVAL_TIMEOUT_MS);
  if (!Number.isFinite(raw) || raw <= 0) return DEFAULT_TIMEOUT_MS;
  return Math.min(MAX_TIMEOUT_MS, Math.max(MIN_TIMEOUT_MS, Math.trunc(raw)));
}

export function approvalPollMs() {
  const raw = Number(process.env.CODEXWORKER_APPROVAL_POLL_MS);
  if (!Number.isFinite(raw) || raw <= 0) return DEFAULT_POLL_MS;
  return Math.max(MIN_POLL_MS, Math.trunc(raw));
}

// Computer Use 的 app 授权走 elicitation，它在 cua_repl 的 js kernel 里同步等待，
// 人工审批（哪怕 20 秒）就会让 kernel 超时重置。这条是 app 级 GUI 访问、riskLevel=low，
// 所以默认自动放行；命令执行/改文件仍然照常提单问人。
const AUTO_APPROVE_KINDS = new Set(
  String(process.env.CODEXWORKER_AUTO_APPROVE_KINDS ?? "elicitation")
    .split(",").map(s => s.trim()).filter(Boolean),
);

export function shouldAutoApprove(request) {
  const kind = String(request?.kind ?? "");
  if (!AUTO_APPROVE_KINDS.has(kind)) return false;
  if (kind === "elicitation" && process.env.CODEXWORKER_AUTO_APPROVE_ELICITATION === "0") return false;
  return true;
}

function denied(approvalId, reason, extra = {}) {
  return { decision: "denied", approvalId: approvalId ?? null, reason, ...extra };
}

/**
 * @param {object} options
 * @param {{requestApproval: Function, getApproval: Function}} options.router
 * @param {(level: string, event: string, fields?: object) => void} [options.log]
 * @param {() => string|null} [options.taskId]
 * @param {number} [options.timeoutMs]
 * @param {number} [options.pollMs]
 */
export function createApprovalChannel({
  router,
  log = () => {},
  taskId = () => null,
  timeoutMs = approvalTimeoutMs(),
  pollMs = approvalPollMs(),
  sleepImpl = sleep,
  now = Date.now,
} = {}) {
  if (!router || typeof router.requestApproval !== "function" || typeof router.getApproval !== "function") {
    throw new Error("approval channel needs a router client with requestApproval/getApproval");
  }
  const stats = { requested: 0, approved: 0, denied: 0, expired: 0, failed: 0, timedOut: 0, polls: 0 };
  let active = 0;

  async function submit(request) {
    const startedAt = now();
    const deadline = startedAt + Math.max(MIN_TIMEOUT_MS, Number(timeoutMs) || DEFAULT_TIMEOUT_MS);
    const taskIdValue = typeof taskId === "function" ? taskId() : taskId;
    let posted;
    try {
      posted = await router.requestApproval({
        taskId: taskIdValue,
        kind: request.kind ?? "approval",
        title: request.title ?? `app-server 审批：${request.method}`,
        detail: request.detail || null,
        ttlMs: Math.max(MIN_TIMEOUT_MS, deadline - now()),
      });
    } catch (error) {
      stats.failed += 1;
      log("warn", "approval_submit_failed", {
        method: request.method, kind: request.kind ?? null,
        message: error?.message ?? String(error),
      });
      return denied(null, `approval channel unavailable: ${error?.message ?? error}`);
    }
    const approvalId = posted?.approval_id;
    if (!approvalId) {
      stats.failed += 1;
      log("warn", "approval_submit_empty", { method: request.method, body: posted ?? null });
      return denied(null, "router did not return an approval id");
    }
    log("info", "approval_submitted", {
      approvalId, method: request.method, kind: request.kind ?? null,
      taskId: taskIdValue ?? null, title: (request.title ?? "").slice(0, 160),
    });

    let lastError = null;
    while (now() < deadline) {
      let approval = null;
      try {
        approval = await router.getApproval(approvalId);
        stats.polls += 1;
        lastError = null;
      } catch (error) {
        if (error?.status === 404) {
          stats.failed += 1;
          log("warn", "approval_missing", { approvalId, message: error?.message ?? String(error) });
          return denied(approvalId, "approval record disappeared from the router");
        }
        lastError = error?.message ?? String(error);
        log("warn", "approval_poll_failed", { approvalId, message: lastError });
      }
      const status = approval?.status;
      if (status === "approved") {
        stats.approved += 1;
        log("info", "approval_granted", {
          approvalId, method: request.method, note: approval?.note ?? null,
          waitedMs: now() - startedAt,
        });
        return { decision: "approved", approvalId, note: approval?.note ?? null, reason: "approved" };
      }
      if (status === "denied" || status === "expired") {
        stats[status === "denied" ? "denied" : "expired"] += 1;
        log("info", status === "denied" ? "approval_denied" : "approval_expired", {
          approvalId, method: request.method, note: approval?.note ?? null,
          waitedMs: now() - startedAt,
        });
        return denied(approvalId, status === "expired" ? "approval expired" : "approval denied",
          { note: approval?.note ?? null, expired: status === "expired" });
      }
      const remaining = deadline - now();
      if (remaining <= 0) break;
      await sleepImpl(Math.min(pollMs, remaining));
    }
    stats.timedOut += 1;
    log("warn", "approval_timeout", {
      approvalId, method: request.method, waitedMs: now() - startedAt,
      lastError,
    });
    return denied(approvalId, "approval timed out before a human answered");
  }

  async function handler(request) {
    stats.requested += 1;
    const incoming = request ?? {};
    if (shouldAutoApprove(incoming)) {
      stats.approved += 1;
      log("info", "approval_auto_approved", {
        method: incoming.method, kind: incoming.kind ?? null,
        title: String(incoming.title ?? "").slice(0, 160),
      });
      return { decision: "approved", approvalId: null, note: "auto-approved (app access)", reason: "auto" };
    }
    active += 1;
    try {
      return await submit(incoming);
    } finally {
      active -= 1;
    }
  }

  return {
    handler,
    stats: () => ({ ...stats }),
    active: () => active,
    timeoutMs,
    pollMs,
  };
}

export default createApprovalChannel;
