import { randomUUID } from "node:crypto";

export function incidentPrompt(incident) {
  return `训练员已授权的 Bridge 脚本自检发现持续故障，请处理这一次诊断任务。
事故编号：${incident.id}
当前原因码：${incident.code}
脚本恢复结果：${incident.repairResult ?? "no_safe_automatic_remedy"}
${incident.code === "native_single_turn_too_large" ? "已确认整轮历史接口超限。业务重载不能缩小这轮历史；首先检查 0.2.31 的本地公开事件增量读取、真实会话身份和持久游标，恢复文字与图片同步。大轮次是正常使用场景，不要求训练员拆任务或等待新轮次；不擅自中断、压缩、分叉或另建会话。" : ""}
${incident.code === "feishu_text_send_unconfirmed" ? "公开文字发送结果未知；先读本机 text-receipt-audit.json 的只读核查结果，再核对原 UUID 和飞书回执。无搜索结果不等于未送达，仅正文相同也不能证明原 UUID 已送达。不可自动重放、换 UUID 或将其伪标为已发送。" : ""}
先使用 operate-codex-bridge 技能复查真实状态；若已恢复，记录后结束。保留本机原有 operatingMode（desktop-bridge 或 feishu-local）、Hermes 通道、本机绑定及未知写入回执。
定位并修复后，必须把已验证、可安全自动执行的解决方法写入 scripts/watchdog-remedies.mjs 或对应运行脚本，并加入能复现问题的测试。无法自动解决的原因也要写入明确的识别、退避和升级规则。不要只记文档或只修本次进程。
验证源码、部署到既有用户安装源并确认下次脚本检查加载新规则；禁止修改受管缓存、重放未知消息、绕过权限或关闭训练员的其他任务。使用既有Desktop启动门禁。恢复后更新脚本事故记录；不要重新启用定时模型巡检。
只有这个未解决事故需要模型；正常检查和已被脚本恢复的故障保持静默。`;
}

export class WatchdogEngine {
  constructor({ state = { version: 1, checks: 0 }, save, remedyFor, repair, escalate, clock = Date.now, intervalMs = 300000 }) {
    Object.assign(this, { state, save, remedyFor, repair, escalate, clock, intervalMs });
  }
  async tick(report) {
    const state = this.state, now = this.clock();
    state.checks++; state.lastAudit = report; state.updatedAt = new Date(now).toISOString();
    const result = phase => ({ phase, nextMs: this.intervalMs });
    if (report.status === "inactive") { await this.save(state); return result("inactive"); }
    if (report.status === "maintenance") { await this.save(state); return { phase: "maintenance", nextMs: 15000 }; }
    if (report.status === "healthy") {
      state.lastHealthyAt = state.updatedAt;
      if (state.incident) {
        state.incident.healthyChecks = (state.incident.healthyChecks ?? 0) + 1;
        if (state.incident.healthyChecks >= 2) {
          state.lastIncident = { ...state.incident, resolvedAt: state.updatedAt };
          state.incident = null;
        }
      }
      await this.save(state);
      return state.incident ? { phase: "confirming_recovery", nextMs: 15000 } : result("healthy");
    }
    // A pre-existing review marker can prove that an apparent recovery did not
    // reconcile a damaged ledger. Reopen that same recorded incident, retaining
    // its sent/unknown alert intent; never spend another task on the same loss.
    if (!state.incident && report.code === "manager_state_recovery_required" &&
        report.recoveryIncidentId === state.lastIncident?.id) {
      state.incident = { ...state.lastIncident }; delete state.incident.resolvedAt;
    }
    state.incident ??= { id: randomUUID(), firstSeenAt: state.updatedAt, failures: 0, alert: null };
    const incident = state.incident;
    incident.code = /^[a-z_]{1,80}$/.test(report.code ?? "") ? report.code : "audit_unavailable";
    incident.failures++; incident.healthyChecks = 0;
    if (incident.failures < 2) { await this.save(state); return { phase: "confirming_fault", nextMs: 15000 }; }
    const remedy = this.remedyFor(incident.code);
    if (remedy && !incident.repairAttempted && !incident.alert) {
      incident.repairAttempted = true; incident.remedy = remedy; incident.repairResult = "dispatching";
      await this.save(state); // No side effect if durable intent cannot be written.
      try { incident.repairResult = await this.repair(remedy, report); }
      catch { incident.repairResult = "repair_outcome_unknown"; }
      await this.save(state);
      // The existing supervisor may be in its 30s backoff and must finish its
      // normal startup grace. Never launch/kill another writer to hurry it.
      return { phase: "verifying_repair", nextMs: incident.repairResult === "installation_restored" ? 90000 : 20000 };
    }
    // A dispatching intent surviving a crash is an unknown write, never retried.
    const preflightRetry = incident.alert?.status === "unavailable" && (incident.alert.attempts ?? 1) < 3 && now >= (incident.alert.retryAt ?? Infinity);
    if (preflightRetry || !incident.alert && (!state.lastAlertAt || now - Date.parse(state.lastAlertAt) >= 3600000)) {
      incident.alert ??= { callId: "bridge-watchdog-" + incident.id, attempts: 0 };
      incident.alert.status = "dispatching"; incident.alert.attempts++;
      state.lastAlertAt = state.updatedAt;
      await this.save(state);
      try { incident.alert.status = await this.escalate(incident, incidentPrompt(incident)); }
      catch { incident.alert.status = "unknown"; }
      if (!["sent", "rejected", "unknown", "unavailable"].includes(incident.alert.status)) incident.alert.status = "unknown";
      // unavailable means discovery failed before tools/call; no task was sent.
      // Only that proven pre-dispatch case gets bounded retries, never unknown.
      if (incident.alert.status === "unavailable") incident.alert.retryAt = now + 300000;
      await this.save(state);
    } else await this.save(state);
    return result(incident.alert ? "escalated" : "escalation_cooldown");
  }
}
