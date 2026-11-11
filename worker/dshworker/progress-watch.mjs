// Shared task liveness policy. Polls/heartbeats are observations, not progress.
import { createHash } from "node:crypto";

export const DEFAULT_NO_PROGRESS_TIMEOUT_MS = 30 * 60 * 1000;

export function noProgressTimeoutMs(config = {}) {
  const value = Number(config.noProgressTimeoutMs ?? process.env.CODEXWORKER_NO_PROGRESS_TIMEOUT_MS);
  return Number.isFinite(value) && value > 0 ? value : DEFAULT_NO_PROGRESS_TIMEOUT_MS;
}

export function taskDeadline(task, startedAt = Date.now()) {
  const seconds = Number(task?.deadline_s);
  return Number.isFinite(seconds) && seconds > 0 ? startedAt + seconds * 1000 : Infinity;
}

// Computed ages and read timestamps must never keep a stalled task alive.
const volatile = new Set(["updatedAt", "recencyAt", "durationMs", "elapsedMs", "elapsed_s",
  "lastPolledAt", "lastSuccessAt", "lastAttemptAt"]);
export function progressToken(value) {
  return createHash("sha256").update(JSON.stringify(value, (key, item) =>
    volatile.has(key) ? undefined : item) ?? "null").digest("hex");
}

export class ProgressWatch {
  constructor({ timeoutMs = noProgressTimeoutMs(), deadline = Infinity, now = Date.now,
    startedAt = now() } = {}) {
    this.timeoutMs = timeoutMs; this.deadline = deadline; this.now = now;
    this.lastProgressAt = startedAt; this.token = undefined;
  }
  touch() { this.lastProgressAt = this.now(); }
  observe(value) {
    if (value == null) return;
    const token = progressToken(value);
    if (token !== this.token) { this.token = token; this.touch(); }
  }
  remainingMs() {
    return Math.max(0, Math.min(this.deadline, this.lastProgressAt + this.timeoutMs) - this.now());
  }
  failure() {
    if (this.now() >= this.deadline) return { reason: "deadline", message: "显式 deadline_s 已到期" };
    if (this.now() - this.lastProgressAt >= this.timeoutMs)
      return { reason: "stuck", message: `无进展超时：连续 ${this.timeoutMs / 60000} 分钟没有新事件` };
    return null;
  }
}
