// Primary conversation identity is durable. Recovery may retry, never replace it.
import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

export class SessionRecoveryError extends Error {
  constructor(code, message) { super(message); this.name = "SessionRecoveryError"; this.code = code; }
}
const fail = (code, message) => { throw new SessionRecoveryError(code, message); };
async function readState(file) {
  let raw;
  try { raw = await readFile(file, "utf8"); }
  catch (error) {
    if (error.code === "ENOENT") return { exists: false, value: null };
    fail("primary_session_unreadable", "主会话状态暂不可读，已保留原文件，禁止自动新建会话。");
  }
  try { return { exists: true, value: JSON.parse(raw) }; }
  catch { fail("primary_session_corrupt", "主会话状态损坏，已保留原文件，禁止自动新建会话。"); }
}
function validSession(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    && typeof value.threadId === "string" && value.threadId.trim() === value.threadId && value.threadId
    && (value.cwd == null || typeof value.cwd === "string" && value.cwd.trim());
}
async function exists(file) {
  try { await stat(file); return true; }
  catch (error) { if (error.code === "ENOENT") return false; throw error; }
}
async function abortable(operation, signal) {
  signal?.throwIfAborted();
  if (!signal) return operation();
  let cancel;
  const aborted = new Promise((_, reject) => {
    cancel = () => reject(signal.reason ?? new Error("session recovery interrupted"));
    signal.addEventListener("abort", cancel, { once: true });
  });
  try { return await Promise.race([Promise.resolve().then(operation), aborted]); }
  finally { signal.removeEventListener("abort", cancel); }
}
const permanent = new Set(["thread_not_found", "session_not_found", "not_found", "modelProviderMismatch",
  "model_provider_mismatch", "unauthorized", "permission_denied"]);

/**
 * Only an untouched installation may create its primary thread. A bootstrap
 * receipt also prevents duplicate creation after a lost response or failed save.
 */
export async function recoverPrimarySession({
  sessionPath, cwd, display, workerName, nativeCall, writeJsonAtomic, log = () => {},
  signal, retryInitialMs = 1000, retryMaxMs = 30000,
  sleep = ms => delay(ms, undefined, { signal }),
}) {
  const statusPath = path.join(path.dirname(sessionPath), "session-recovery.json");
  let saved, restoreFile = false, attempt = 0;
  const report = async (state, fields = {}) => {
    let previous;
    try { previous = JSON.parse(await readFile(statusPath, "utf8")); } catch { /* no usable prior receipt */ }
    if (previous?.workerName !== workerName) previous = null;
    const status = { state, workerName, threadId: saved?.threadId ?? previous?.threadId ?? null,
      cwd: saved?.cwd ?? previous?.cwd ?? null, attempt, updatedAt: new Date().toISOString(), ...fields };
    await writeJsonAtomic(statusPath, status);
    log(state === "ready" ? "info" : "warn", "primary_session_" + state, status);
  };
  try {
    const record = await readState(sessionPath);
    if (record.exists) {
      if (!validSession(record.value)) fail("primary_session_corrupt", "主会话状态缺少有效标识，禁止自动新建会话。");
      saved = record.value;
    } else {
      const prior = await readState(statusPath);
      if (prior.exists) {
        if (prior.value?.workerName !== workerName || !validSession(prior.value))
          fail("primary_session_missing", "主会话文件缺失且恢复记录不足以确认原会话，禁止自动新建。");
        saved = { threadId: prior.value.threadId, cwd: prior.value.cwd };
        restoreFile = true;
      } else {
        for (const name of ["manager-binding.json", "desktop-manager-state.json"]) {
          if (await exists(path.join(path.dirname(sessionPath), name)))
            fail("primary_session_missing", "主会话文件缺失，但已有运行记录，禁止把旧安装当成首次启动。");
        }
        signal?.throwIfAborted();
        await report("creating");
        let created;
        try { created = await abortable(() => nativeCall("create_thread", { cwd, title: `${display} · CLI` }, 30000), signal); }
        catch { fail("primary_session_create_unconfirmed", "首次创建会话的结果未确认，已停止自动重建，请核对原会话。"); }
        if (!validSession({ threadId: created?.threadId, cwd: created?.thread?.cwd ?? cwd }))
          fail("primary_session_create_unconfirmed", "引擎未返回有效会话标识，已停止自动重建。");
        saved = { threadId: created.threadId, cwd: created.thread?.cwd ?? cwd };
        // Persist the exact returned id before saving the main file, so a failed
        // primary-file write can be recovered without another create_thread.
        await report("created");
        signal?.throwIfAborted();
        try { await writeJsonAtomic(sessionPath, saved); }
        catch { fail("primary_session_persist_failed", "会话已创建但主会话文件保存失败，恢复记录已保留原 ID。"); }
        await report("ready");
        log("info", "cli_session_created", saved);
        return saved;
      }
    }
    for (;;) {
      signal?.throwIfAborted();
      attempt++;
      await report("recovering");
      let snapshot;
      try {
        snapshot = await abortable(() => nativeCall("read_thread", {
          threadId: saved.threadId, turnLimit: 1, includeOutputs: false,
        }, 30000), signal);
      } catch (error) {
        signal?.throwIfAborted();
        if (permanent.has(error?.code))
          fail("primary_session_unavailable", "原会话恢复被引擎拒绝，已保留原 ID，禁止自动新建。");
        const initial = Number.isFinite(Number(retryInitialMs)) && Number(retryInitialMs) > 0 ? Number(retryInitialMs) : 1000;
        const maximum = Number.isFinite(Number(retryMaxMs)) && Number(retryMaxMs) >= initial ? Number(retryMaxMs) : Math.max(initial, 30000);
        const retryInMs = Math.min(maximum, initial * 2 ** Math.min(attempt - 1, 16));
        await report("retrying", { code: error?.code ?? "native_read_failed", retryInMs,
          nextRetryAt: new Date(Date.now() + retryInMs).toISOString() });
        await sleep(retryInMs);
        continue;
      }
      signal?.throwIfAborted();
      const found = snapshot?.thread;
      if (found?.id !== saved.threadId)
        fail("primary_session_mismatch", "引擎返回的会话 ID 与原会话不符，已停止恢复，禁止自动新建。");
      const expectedCwd = saved.cwd ?? found.cwd ?? cwd;
      if (found.cwd != null && (typeof found.cwd !== "string" || path.resolve(found.cwd) !== path.resolve(expectedCwd)))
        fail("primary_session_cwd_mismatch", "原会话的工作目录与保存记录不符，已停止恢复，禁止自动新建。");
      const session = { threadId: saved.threadId, cwd: found.cwd ?? expectedCwd };
      if (restoreFile) await writeJsonAtomic(sessionPath, session);
      await report("ready");
      log("info", "cli_session_reused", session);
      return session;
    }
  } catch (error) {
    const aborted = signal?.aborted;
    await report(aborted ? "interrupted" : "blocked", {
      code: aborted ? "recovery_interrupted" : error?.code ?? "recovery_failed",
    }).catch(() => {});
    throw error;
  }
}
