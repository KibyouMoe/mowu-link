import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { auditBridge } from "../scripts/check-desktop-health.mjs";
import { remedyFor } from "../scripts/watchdog-remedies.mjs";

test("an unknown image barrier remains actionable and is never repaired by replay", async t => {
  const f = await fixture(t);
  f.health.integrations.feishu.relay = { mode: "desktop-public-journal", lastError: "feishu_public_send_unconfirmed" };
  assert.equal((await auditBridge(f.options)).code, "feishu_public_send_unconfirmed");
  assert.equal(remedyFor("feishu_public_send_unconfirmed"), null);
});

test("unknown self lifecycle state escalates without a futile reload remedy", async t => {
  const f = await fixture(t);
  f.health.integrations.feishu.relay = { mode: "desktop-public-journal", lastError: "self_task_journal_unavailable" };
  assert.equal((await auditBridge(f.options)).code, "self_task_journal_unavailable");
  assert.equal(remedyFor("self_task_journal_unavailable"), null);
});

async function fixture(t) {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "bridge-audit-"));
  t.after(() => rm(stateDir, { recursive: true, force: true }));
  const tokenFile = path.join(stateDir, "test.token");
  await writeFile(tokenFile, "isolated-audit-token-never-a-production-secret");
  const owner = { pid: 700, name: "ChatGPT.exe", startedAt: "test-desktop-lifetime",
    executable: "C:/Program Files/WindowsApps/OpenAI.Codex_test/app/ChatGPT.exe" };
  const config = { tokenFile, operatingMode: "feishu-local", bridgeName: "test-bridge" };
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify(config));
  await writeFile(path.join(stateDir, "desktop-lifecycle-status.json"), JSON.stringify({ desktopPid: owner.pid,
    desktopStartedAt: owner.startedAt, stage: "adapter_retry_pending" }));
  const now = 1000000;
  const connection = { phase: "connected", lastFrameAt: new Date(now - 1000).toISOString(), pingIntervalMs: 90000,
    pongTimeoutMs: 15000, watchdog: { enabled: true, checks: 4, repairs: 1, intervalMs: 180000, lastCheckedAt: new Date(now - 30000).toISOString() } };
  const health = { name: "test-bridge", machine: "codexworker", version: "0.2.27", alive: true, ready: true, integrationMode: "desktop-native",
    operatingMode: "feishu-local", capabilities: { businessHotReload: { desktopWatchdogVersion: "0.2.27" } },
    integrations: { pid: 900, feishu: { enabled: true, inboundEnabled: true, connected: true, connection } } };
  const options = { stateDir, clock: () => now, readChain: async () => [owner], fetchImpl: async (url, request) => {
    assert.equal(url, "http://127.0.0.1:8903/health"); assert.match(request.headers.authorization, /^Bearer /);
    return { ok: true, json: async () => health };
  } };
  return { options, config, owner, health, connection, now };
}

test("audit verifies an authenticated live native writer and recent Feishu heartbeat without leaking secrets", async t => {
  const f = await fixture(t), result = await auditBridge(f.options);
  assert.equal(result.status, "healthy"); assert.equal(result.fingerprint, "healthy:bridge_and_feishu_responsive");
  assert.equal(result.watchdog.repairs, 1);
  assert.doesNotMatch(JSON.stringify(result), /Bearer|isolated-audit-token|test\.token/);
});

test("incremental public journal is healthy when caught up, catches up silently, and exposes uncertain sends", async t => {
  const f = await fixture(t);
  f.health.integrations.feishu.relay = { mode: "desktop-public-journal", sessionState: "running", lastError: null,
    lastSuccessAt: new Date(f.now).toISOString(), startedAt: new Date(f.now).toISOString(), journal: { caughtUp: true } };
  assert.equal((await auditBridge(f.options)).status, "healthy");
  f.health.integrations.feishu.relay.journal = { caughtUp: false, pendingBytes: 210000000 };
  assert.equal((await auditBridge(f.options)).code, "public_journal_catching_up");
  f.health.integrations.feishu.relay.lastError = "feishu_text_send_unconfirmed";
  assert.equal((await auditBridge(f.options)).code, "feishu_text_send_unconfirmed");
});
test("a dead or reused Desktop PID is inactive and never triggers an HTTP request", async t => {
  const f = await fixture(t); let calls = 0;
  const options = { ...f.options, fetchImpl: async () => { calls++; throw new Error(); } };
  assert.equal((await auditBridge({ ...options, readChain: async () => [] })).code, "desktop_closed");
  assert.equal((await auditBridge({ ...options, readChain: async () => [{ ...f.owner, startedAt: "reused" }] })).status, "inactive");
  assert.equal(calls, 0);
});
test("transient process lookup failure is unknown, not evidence that Desktop closed", async t => {
  const f = await fixture(t);
  assert.equal((await auditBridge({ ...f.options, readChain: async () => { throw new Error("private raw text"); } })).status, "unknown");
});
test("unreachable, rejected auth and wrong identity remain distinct actionable faults", async t => {
  const f = await fixture(t);
  const missing = await auditBridge({ ...f.options, fetchImpl: async () => { throw new Error("private signed endpoint"); } });
  assert.equal(missing.code, "bridge_unreachable"); assert.equal(missing.lifecycleStage, "adapter_retry_pending");
  const denied = await auditBridge({ ...f.options, fetchImpl: async () => ({ ok: false, status: 403 }) });
  assert.equal(denied.code, "local_health_auth_rejected");
  f.health.name = "other"; assert.equal((await auditBridge(f.options)).code, "unexpected_bridge_identity");
  assert.doesNotMatch(JSON.stringify(missing), /private signed endpoint/);
});
test("OPEN alone cannot pass the audit when heartbeats or the watchdog are stale", async t => {
  const f = await fixture(t);
  f.connection.lastFrameAt = null; assert.equal((await auditBridge(f.options)).code, "feishu_heartbeat_stale");
  f.connection.lastFrameAt = new Date(f.now - 210000).toISOString();
  assert.equal((await auditBridge(f.options)).code, "feishu_heartbeat_stale");
  f.connection.lastFrameAt = new Date(f.now).toISOString();
  f.connection.watchdog.lastCheckedAt = new Date(f.now - 400000).toISOString();
  assert.equal((await auditBridge(f.options)).code, "feishu_watchdog_stale");
  f.connection.watchdog.enabled = false; assert.equal((await auditBridge(f.options)).code, "feishu_watchdog_unavailable");
});
test("healthy HTTP cannot hide disconnected Feishu or an unintended operating mode", async t => {
  const f = await fixture(t); f.health.integrations.feishu.connected = false;
  assert.equal((await auditBridge(f.options)).code, "feishu_disconnected");
  f.health.operatingMode = "desktop-bridge"; assert.equal((await auditBridge(f.options)).code, "operating_mode_mismatch");
});
test("the audit rejects non-loopback configuration before sending its bearer credential", async t => {
  const f = await fixture(t); let calls = 0;
  await writeFile(path.join(f.options.stateDir, "desktop-config.local.json"), JSON.stringify({ ...f.config, host: "192.0.2.1" }));
  const result = await auditBridge({ ...f.options, fetchImpl: async () => { calls++; } });
  assert.equal(result.code, "local_health_config_invalid"); assert.equal(calls, 0);
});
test("a new business version alone cannot claim the fixed watchdog startup layer is loaded", async t => {
  const f = await fixture(t); delete f.health.capabilities.businessHotReload.desktopWatchdogVersion;
  assert.equal((await auditBridge(f.options)).code, "desktop_watchdog_upgrade_pending");
});

test("zeroed code is a distinct actionable fault; damaged or unreconciled state never looks healthy", async t => {
  const f = await fixture(t); let requests = 0;
  const corrupt = await auditBridge({ ...f.options, installationAudit: async () => ({ code: "installation_business_corrupt", corruptFiles: 20 }),
    fetchImpl: async () => { requests++; assert.fail("Code corruption is known before HTTP"); } });
  assert.equal(corrupt.code, "installation_business_corrupt"); assert.equal(corrupt.corruptFiles, 20); assert.equal(requests, 0);
  await writeFile(path.join(f.options.stateDir, "desktop-manager-state.json"), Buffer.alloc(1024));
  const unavailable = await auditBridge({ ...f.options, fetchImpl: async () => { throw new Error(); } });
  assert.equal(unavailable.code, "manager_state_corrupt");
  await writeFile(path.join(f.options.stateDir, "state-recovery-required.local.json"), '{"required":true}');
  assert.equal((await auditBridge(f.options)).code, "manager_state_recovery_required");
});

test("desktop-bridge audit preserves Hermes mode and rejects another machine or profile", async t => {
  const f = await fixture(t);
  delete f.config.operatingMode;
  await writeFile(path.join(f.options.stateDir, "desktop-config.local.json"), JSON.stringify(f.config));
  f.health.operatingMode = "desktop-bridge";
  assert.equal((await auditBridge(f.options)).status, "healthy");
  f.health.machine = "qicao";
  assert.equal((await auditBridge(f.options)).code, "unexpected_bridge_identity");
  f.health.machine = "rudolf"; f.health.operatingMode = "feishu-local";
  assert.equal((await auditBridge(f.options)).code, "operating_mode_mismatch");
});

test("live Feishu heartbeat cannot hide a failed or stalled selected chat relay", async t => {
  const f = await fixture(t);
  f.health.integrations.feishu.relay = { sessionState: "error", lastError: "native_read_rejected", startedAt: new Date(f.now).toISOString() };
  let result = await auditBridge(f.options);
  assert.equal(result.code, "native_relay_read_failed"); assert.equal(result.relayError, "native_read_rejected");
  f.health.integrations.feishu.relay = { sessionState: "running", lastError: null, startedAt: new Date(f.now - 180000).toISOString(),
    lastSuccessAt: new Date(f.now - 180000).toISOString() };
  assert.equal((await auditBridge(f.options)).code, "native_relay_stalled");
  f.health.integrations.feishu.relay.lastSuccessAt = new Date(f.now).toISOString();
  assert.equal((await auditBridge(f.options)).status, "healthy");
});

test("legacy health is checked against selected-session state without returning private content", async t => {
  const f = await fixture(t); f.health.integrations.selectedThreadId = "selected";
  await writeFile(path.join(f.options.stateDir, "desktop-manager-state.json"), JSON.stringify({ sessions: { selected: {
    state: "error", seen: { private: "private transcript never returned" } } } }));
  const result = await auditBridge(f.options);
  assert.equal(result.code, "native_relay_read_failed"); assert.doesNotMatch(JSON.stringify(result), /private transcript/);
});
test("only a bounded active handoff defers relay alerts", async t => {
  const f = await fixture(t); f.health.integrations.feishu.connected = false;
  f.health.capabilities.businessHotReload.handoff = { paused: true, startedAt: new Date(f.now - 1000).toISOString(), timeoutMs: 45000 };
  assert.equal((await auditBridge(f.options)).status, "maintenance");
  f.health.capabilities.businessHotReload.handoff.startedAt = new Date(f.now - 61000).toISOString();
  assert.equal((await auditBridge(f.options)).code, "feishu_disconnected");
});
test("limited native progress is reported as degraded rather than full-history health", async t => {
  const f = await fixture(t);
  f.health.integrations.feishu.relay = { sessionState: "error", lastError: "native_single_turn_too_large", mode: "latest-public-message", lastProgressAt: new Date(f.now).toISOString() };
  const result = await auditBridge(f.options); assert.equal(result.status, "degraded");
  assert.equal(result.code, "native_single_turn_too_large"); assert.equal(result.relayMode, "latest-public-message");
});
