// Read-only, authenticated audit used by the app's external heartbeat. Never
// launches a writer, sends a message, or prints configuration/native payloads.
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { desktopOwner, processChain } from "../desktop-lifecycle.mjs";
import { configuredBridgeIdentity, matchesBridgeIdentity, resolveWatchdogStateDir } from "./watchdog-context.mjs";
import { auditInstallation } from "./installation-recovery.mjs";

export async function auditBridge({ stateDir, fetchImpl = fetch, readChain = processChain, clock = Date.now, installationAudit = auditInstallation }) {
  const report = (status, code, extra = {}) => ({ checkedAt: new Date(clock()).toISOString(), status, code,
    fingerprint: status + ":" + code, ...extra });
  let lifecycle, config;
  try {
    lifecycle = JSON.parse(await readFile(path.join(stateDir, "desktop-lifecycle-status.json"), "utf8"));
    config = JSON.parse(await readFile(path.join(stateDir, "desktop-config.local.json"), "utf8"));
  } catch { return report("fault", "local_health_config_unavailable"); }
  let owner;
  try { owner = desktopOwner(await readChain(lifecycle.desktopPid)); }
  catch { return report("unknown", "desktop_owner_check_pending"); }
  if (!owner || owner.pid !== lifecycle.desktopPid || owner.startedAt !== lifecycle.desktopStartedAt) {
    return report("inactive", "desktop_closed");
  }
  const expected = configuredBridgeIdentity(config), port = config.port ?? 8903;
  if ((config.host ?? "127.0.0.1") !== "127.0.0.1" || !expected || !Number.isInteger(port) || port < 1 || port > 65535) {
    return report("fault", "local_health_config_invalid");
  }
  const installation = await installationAudit({ stateDir, pluginRoot: path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..") });
  if (["installation_business_corrupt", "installation_business_drift"].includes(installation.code)) {
    return report("fault", installation.code, { corruptFiles: installation.corruptFiles });
  }
  // Corrupt state is never auto-restored from an older snapshot: that could
  // replay a native write whose receipt was lost. A review marker stays visible.
  try {
    const pending = JSON.parse(await readFile(path.join(stateDir, "state-recovery-required.local.json"), "utf8"));
    if (pending.required === true) return report("fault", "manager_state_recovery_required", {
      ...(/^[a-f\d-]{36}$/i.test(pending.incidentId ?? "") ? { recoveryIncidentId: pending.incidentId } : {}) });
  } catch (error) { if (error.code !== "ENOENT") return report("fault", "manager_state_recovery_required"); }
  let health;
  try {
    const token = (await readFile(config.tokenFile, "utf8")).trim();
    if (token.length < 24) return report("fault", "local_health_credential_invalid");
    const response = await fetchImpl(`http://127.0.0.1:${config.port ?? 8903}/health`, {
      headers: { authorization: "Bearer " + token }, signal: AbortSignal.timeout(3000) });
    if (!response.ok) return report("fault", [401, 403].includes(response.status) ? "local_health_auth_rejected" : "bridge_http_unavailable");
    health = await response.json();
  } catch {
    try { JSON.parse(await readFile(path.join(stateDir, "desktop-manager-state.json"), "utf8")); }
    catch (error) { if (error.code !== "ENOENT") return report("fault", "manager_state_corrupt"); }
    return report("fault", "bridge_unreachable", { lifecycleStage: lifecycle.stage, installationCode: installation.code });
  }
  if (health.integrationMode !== "desktop-native" || health.name !== expected.name || health.machine !== expected.machine) {
    return report("fault", "unexpected_bridge_identity");
  }
  const extra = { version: health.version, operatingMode: health.operatingMode,
    writerPid: health.integrations?.pid, watchdogVersion: health.capabilities?.businessHotReload?.desktopWatchdogVersion ?? null };
  if (!health.alive || !health.ready) return report("fault", "native_bridge_unready", extra);
  if (!matchesBridgeIdentity(health, expected)) return report("fault", "operating_mode_mismatch", extra);
  const handoff = health.capabilities?.businessHotReload?.handoff;
  if (handoff?.paused && clock() - Date.parse(handoff.startedAt) < (handoff.timeoutMs ?? 45000) + 15000) {
    return report("maintenance", "business_handoff_in_progress", extra);
  }
  const feishu = health.integrations?.feishu, connection = feishu?.connection;
  if (!feishu?.enabled || !feishu.inboundEnabled) return report("fault", "feishu_monitor_disabled", extra);
  if (!feishu.connected || connection?.phase !== "connected") return report("fault", "feishu_disconnected", extra);
  if (!connection.watchdog?.enabled) return report("fault", "feishu_watchdog_unavailable", extra);
  const received = Date.parse(connection.lastFrameAt), checked = Date.parse(connection.watchdog.lastCheckedAt);
  const budget = Math.max(connection.pingIntervalMs ?? 0, connection.watchdog.intervalMs ?? 180000) + (connection.pongTimeoutMs ?? 15000) + 5000;
  if (!Number.isFinite(received) || clock() - received > budget) return report("fault", "feishu_heartbeat_stale", extra);
  if (Number.isFinite(checked) && clock() - checked > 2 * connection.watchdog.intervalMs + 15000) {
    return report("fault", "feishu_watchdog_stale", extra);
  }
  if (extra.watchdogVersion !== "0.2.27") return report("fault", "desktop_watchdog_upgrade_pending", extra);
  let relay = feishu.relay;
  if (!relay && health.integrations?.selectedThreadId) {
    // Older business versions omitted selected-session errors from /health.
    // Read only the needed metadata; never expose persisted chat contents.
    try {
      const state = JSON.parse(await readFile(path.join(stateDir, "desktop-manager-state.json"), "utf8"));
      const session = state.sessions?.[health.integrations.selectedThreadId];
      if (!session) return report("fault", "native_relay_health_unavailable", extra);
      relay = { sessionState: session.state, ...session.relayStatus };
    } catch { return report("fault", "native_relay_health_unavailable", extra); }
  }
  if (relay?.lastError === "native_single_turn_too_large") return report("degraded", "native_single_turn_too_large", {
    ...extra, relayMode: relay.mode ?? "unavailable", lastProgressAt: relay.lastProgressAt ?? null,
    latestTextDelivery: relay.latestTextDelivery ?? null });
  if (relay?.lastError === "feishu_text_send_unconfirmed") return report("fault", "feishu_text_send_unconfirmed", extra);
  if (relay?.lastError === "feishu_public_send_unconfirmed") return report("fault", "feishu_public_send_unconfirmed", extra);
  // This is a verified local state/identity problem, not a lost transport.
  // Reloading cannot invent a current turn; leave it to bounded escalation.
  if (relay?.lastError === "self_task_journal_unavailable") return report("fault", "self_task_journal_unavailable", extra);
  if (relay?.mode === "desktop-public-journal" && relay.journal?.caughtUp === false &&
      clock() - Date.parse(relay.lastSuccessAt) < 120000) return report("maintenance", "public_journal_catching_up", {
        ...extra, pendingBytes: relay.journal.pendingBytes });
  if (relay?.sessionState === "error" || relay?.lastError) return report("fault", "native_relay_read_failed", {
    ...extra, relayError: relay.lastError ?? "selected_session_error" });
  if (relay?.startedAt) {
    const lastSync = Math.max(Date.parse(relay.startedAt) || 0, Date.parse(relay.lastSuccessAt) || 0);
    if (clock() - lastSync > 120000) return report("fault", "native_relay_stalled", extra);
  }
  return report("healthy", "bridge_and_feishu_responsive", { ...extra, lastFeishuFrameAt: connection.lastFrameAt,
    relayMode: relay?.mode ?? "full-history",
    lastRelaySyncAt: relay?.lastSuccessAt ?? null,
    watchdog: { checks: connection.watchdog.checks, repairs: connection.watchdog.repairs, lastCheckedAt: connection.watchdog.lastCheckedAt } });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { process.stdout.write(JSON.stringify(await auditBridge({ stateDir: resolveWatchdogStateDir() })) + "\n"); }
  catch { process.stdout.write(JSON.stringify({ status: "unknown", code: "audit_unavailable" }) + "\n"); }
}
