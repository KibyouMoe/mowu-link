import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { WatchdogEngine } from "../scripts/watchdog-engine.mjs";
import { escalateIncident } from "../scripts/watchdog-native.mjs";
import { remedyFor, repairKnown } from "../scripts/watchdog-remedies.mjs";
import { ensureScriptWatchdog } from "../desktop-lifecycle.mjs";

const healthy = { status: "healthy", code: "bridge_and_feishu_responsive" };
const fault = { status: "fault", code: "feishu_disconnected" };
function fixture(state) {
  let now = 1000000;
  const saved = [], repairs = [], prompts = [];
  const engine = new WatchdogEngine({ state, clock: () => now, save: async data => saved.push(structuredClone(data)),
    remedyFor, repair: async remedy => { repairs.push(remedy); return "integrations_reloaded"; },
    escalate: async (incident, prompt) => { prompts.push({ incident: structuredClone(incident), prompt }); return "sent"; } });
  return { engine, saved, repairs, prompts, advance: ms => { now += ms; } };
}
test("healthy scheduled audits never call a model or native task write", async () => {
  const f = fixture(); for (let i = 0; i < 100; i++) await f.engine.tick(healthy);
  assert.equal(f.engine.state.checks, 100); assert.deepEqual(f.repairs, []); assert.deepEqual(f.prompts, []);
});
test("one transient failure followed by stable recovery is silent", async () => {
  const f = fixture(); assert.equal((await f.engine.tick(fault)).nextMs, 15000);
  await f.engine.tick(healthy); await f.engine.tick(healthy);
  assert.equal(f.engine.state.incident, null); assert.deepEqual(f.repairs, []); assert.deepEqual(f.prompts, []);
});

test("a verified unreconciled-state marker reopens the same incident without resending its recorded diagnosis", async () => {
  const f = fixture(); for (let i = 0; i < 3; i++) await f.engine.tick(fault);
  const id = f.engine.state.incident.id; await f.engine.tick(healthy); await f.engine.tick(healthy);
  const previousAlert = structuredClone(f.engine.state.lastIncident.alert);
  for (let i = 0; i < 5; i++) await f.engine.tick({ status: "fault", code: "manager_state_recovery_required", recoveryIncidentId: id });
  assert.equal(f.engine.state.incident.id, id);
  assert.deepEqual(f.engine.state.incident.alert, previousAlert);
  assert.equal(f.engine.state.incident.resolvedAt, undefined); assert.equal(f.prompts.length, 1);
});

test("restored installation gets bounded supervisor startup grace; state corruption has no automatic rollback remedy", async () => {
  const f = fixture(); f.engine.repair = async () => "installation_restored";
  const broken = { status: "fault", code: "installation_business_corrupt" };
  await f.engine.tick(broken);
  assert.equal((await f.engine.tick(broken)).nextMs, 90000);
  assert.equal(remedyFor("manager_state_corrupt"), null);
  assert.equal(remedyFor("manager_state_recovery_required"), null);
  await f.engine.tick(healthy); await f.engine.tick(healthy);
  assert.equal(f.engine.state.incident, null); assert.equal(f.prompts.length, 0);
});
test("a bounded business handoff waits without repairing, escalating or erasing an existing incident", async () => {
  const f = fixture(); await f.engine.tick(fault); const incident = f.engine.state.incident.id;
  for (let i = 0; i < 4; i++) assert.equal((await f.engine.tick({ status: "maintenance", code: "business_handoff_in_progress" })).nextMs, 15000);
  assert.equal(f.engine.state.incident.id, incident); assert.equal(f.engine.state.incident.failures, 1);
  assert.deepEqual(f.repairs, []); assert.deepEqual(f.prompts, []);
});
test("an oversized single turn never triggers a futile business reload and escalates once with its limits", async () => {
  const f = fixture(); const report = { status: "degraded", code: "native_single_turn_too_large" };
  for (let i = 0; i < 6; i++) await f.engine.tick(report);
  assert.deepEqual(f.repairs, []); assert.equal(f.prompts.length, 1);
  assert.match(f.prompts[0].prompt, /业务重载不能缩小/); assert.match(f.prompts[0].prompt, /不擅自中断/);
});
test("a known repair is persisted before execution, verified afterward and never wakes the model on recovery", async () => {
  const f = fixture(); await f.engine.tick(fault); const outcome = await f.engine.tick(fault);
  assert.equal(outcome.phase, "verifying_repair"); assert.equal(outcome.nextMs, 20000);
  assert.equal(f.saved.at(-2).incident.repairResult, "dispatching");
  await f.engine.tick(healthy); await f.engine.tick(healthy);
  assert.deepEqual(f.repairs, ["reload_feishu"]); assert.deepEqual(f.prompts, []);
});
test("an unresolved incident raises exactly one diagnosis task with mandatory repair-to-script instructions", async () => {
  const f = fixture(); for (let i = 0; i < 20; i++) await f.engine.tick(fault);
  assert.deepEqual(f.repairs, ["reload_feishu"]); assert.equal(f.prompts.length, 1);
  assert.match(f.prompts[0].prompt, /watchdog-remedies\.mjs/); assert.match(f.prompts[0].prompt, /能复现问题的测试/);
  assert.equal(f.engine.state.incident.alert.status, "sent");
  const restarted = fixture(structuredClone(f.engine.state)); await restarted.engine.tick(fault);
  assert.deepEqual(restarted.prompts, []); assert.deepEqual(restarted.repairs, []);
});
test("authorization or identity faults have no automatic repair and still escalate once", async () => {
  for (const code of ["local_health_auth_rejected", "unexpected_bridge_identity", "operating_mode_mismatch", "local_health_config_invalid"]) {
    const f = fixture(); await f.engine.tick({ status: "fault", code }); await f.engine.tick({ status: "fault", code });
    assert.deepEqual(f.repairs, []); assert.equal(f.prompts.length, 1);
  }
});
test("uncertain alerts and crashes after durable dispatch intent never resend a task", async () => {
  const f = fixture(); f.engine.escalate = async () => { throw new Error("private native error"); };
  for (let i = 0; i < 3; i++) await f.engine.tick(fault);
  assert.equal(f.engine.state.incident.alert.status, "unknown");
  const state = structuredClone(f.engine.state); state.incident.alert.status = "dispatching";
  const restarted = fixture(state); await restarted.engine.tick(fault); assert.deepEqual(restarted.prompts, []);
});
test("failed native discovery retries at most three times before dispatch, without retrying unknown writes", async () => {
  const f = fixture(); let attempts = 0;
  f.engine.escalate = async () => { attempts++; return "unavailable"; };
  for (let i = 0; i < 3; i++) await f.engine.tick(fault);
  assert.equal(attempts, 1); await f.engine.tick(fault); assert.equal(attempts, 1);
  for (let i = 0; i < 5; i++) { f.advance(300000); await f.engine.tick(fault); }
  assert.equal(attempts, 3); assert.equal(f.engine.state.incident.alert.attempts, 3);
});
test("disk failure prevents both a repair and an incident prompt", async () => {
  const f = fixture(); await f.engine.tick(fault);
  f.engine.save = async () => { throw new Error("disk unavailable"); };
  await assert.rejects(f.engine.tick(fault)); assert.deepEqual(f.repairs, []); assert.deepEqual(f.prompts, []);
});
test("flapping errors cannot spend tokens repeatedly; a new confirmed incident observes a one-hour cooldown", async () => {
  const f = fixture(); for (let i = 0; i < 3; i++) await f.engine.tick(fault);
  await f.engine.tick(healthy); await f.engine.tick(healthy);
  for (let i = 0; i < 4; i++) await f.engine.tick({ status: "fault", code: "native_bridge_unready" });
  assert.equal(f.prompts.length, 1); f.advance(3600000); await f.engine.tick({ status: "fault", code: "native_bridge_unready" });
  assert.equal(f.prompts.length, 2);
});
test("native escalation targets only the configured maintenance chat with the recorded incident call ID", async () => {
  const threadId = "11111111-1111-4111-8111-111111111111", calls = [];
  const incident = { alert: { callId: "recorded-call" } };
  const outcome = await escalateIncident({ pipe: "private-test-pipe", threadId, incident, prompt: "test diagnosis", request: async (_pipe, method, params) => {
    calls.push({ method, params });
    return method === "tools/list" ? { tools: [{ name: "send_message_to_thread", namespace: "codex_app" }] } : { success: true };
  } });
  assert.equal(outcome, "sent"); assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].params.arguments, { threadId, prompt: "test diagnosis" });
  assert.equal(calls[1].params.threadId, threadId); assert.equal(calls[1].params.callerSource, "codex");
  assert.equal(calls[1].params.callId, "recorded-call");
  let writes = 0;
  assert.equal(await escalateIncident({ pipe: "test", threadId, incident, prompt: "test", request: async (_p, method) => {
    if (method === "tools/list") return { tools: [{ name: "send_message_to_thread", namespace: "codex_app" }] };
    writes++; throw new Error("response lost after acceptance");
  } }), "unknown"); assert.equal(writes, 1);
});
test("repair rechecks identity before POST and delegates missing supervisor recovery to existing startup gates", async t => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "script-remedy-")); t.after(() => rm(stateDir, { recursive: true, force: true }));
  const tokenFile = path.join(stateDir, "test.token"); await writeFile(tokenFile, "isolated-script-watchdog-token-123456789");
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ tokenFile, operatingMode: "feishu-local", bridgeName: "test" }));
  const requests = []; let identity = "other";
  const options = { stateDir, pluginRoot: "unused", fetchImpl: async (url, request) => {
    requests.push({ url, method: request.method ?? "GET" });
    return { ok: true, json: async () => ({ name: identity, machine: "codexworker", integrationMode: "desktop-native", operatingMode: "feishu-local" }) };
  } };
  assert.equal(await repairKnown("reload_feishu", fault, options), "repair_identity_rejected"); assert.equal(requests.length, 1);
  identity = "test"; assert.equal(await repairKnown("reload_feishu", fault, options), "integrations_reloaded");
  assert.equal(requests.at(-1).method, "POST");
  assert.equal(await repairKnown("reload_business", { code: "native_relay_read_failed" }, options), "business_reload_requested");
  assert.ok(requests.at(-1).url.endsWith("/bridge/runtime/reload"));
  await writeFile(path.join(stateDir, "desktop-lifecycle-status.json"), '{"supervisorPid":42}');
  let starts = 0;
  const lifecycle = { ...options, ensure: async () => { starts++; return true; }, alive: () => true };
  const missing = { status: "fault", code: "bridge_unreachable" };
  assert.equal(await repairKnown("ensure_desktop_lifecycle", missing, lifecycle), "existing_supervisor_recovering"); assert.equal(starts, 0);
  assert.equal(await repairKnown("ensure_desktop_lifecycle", missing, { ...lifecycle, alive: () => false }), "desktop_lifecycle_ready"); assert.equal(starts, 1);
  assert.equal(await repairKnown("reload_feishu", { code: "local_health_auth_rejected" }, options), "remedy_not_applicable");
});

test("desktop-bridge remedies require exact name, machine and mode and never disable Hermes", async t => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "desktop-bridge-remedy-"));
  t.after(() => rm(stateDir, { recursive: true, force: true }));
  const tokenFile = path.join(stateDir, "test.token");
  await writeFile(tokenFile, "isolated-desktop-bridge-test-token-123456789");
  const config = { tokenFile, bridgeName: "CodexWorker", machine: "codexworker" };
  const configPath = path.join(stateDir, "desktop-config.local.json");
  await writeFile(configPath, JSON.stringify(config));
  const health = { name: "CodexWorker", machine: "codexworker", integrationMode: "desktop-native", operatingMode: "desktop-bridge" };
  const calls = [];
  const options = { stateDir, pluginRoot: "unused", fetchImpl: async (url, request) => {
    calls.push({ url, method: request.method ?? "GET" });
    assert.equal(request.body, undefined); // No replacement integration config or mode switch.
    return { ok: true, json: async () => health };
  } };
  for (const [key, invalid] of [["name", "气槽"], ["machine", "qicao"], ["operatingMode", "feishu-local"], ["integrationMode", "cli"]]) {
    const original = health[key]; health[key] = invalid; calls.length = 0;
    assert.equal(await repairKnown("reload_feishu", fault, options), "repair_identity_rejected");
    assert.equal(calls.length, 1); health[key] = original;
  }
  calls.length = 0;
  assert.equal(await repairKnown("reload_feishu", fault, options), "integrations_reloaded");
  assert.equal(calls.at(-1).method, "POST");
  assert.ok(calls.at(-1).url.endsWith("/bridge/integrations/reload"));
  assert.equal(await repairKnown("reload_business", { code: "native_relay_stalled" }, options), "business_reload_requested");
  assert.deepEqual(JSON.parse(await readFile(configPath, "utf8")), config);
  calls.length = 0;
  assert.equal(await repairKnown("reload_feishu", fault, { ...options, fetchImpl: async (_url, request) => {
    calls.push(request); return { ok: false, status: 403 };
  } }), "repair_health_rejected");
  assert.equal(calls.length, 1); assert.equal(calls[0].method, undefined);
  await writeFile(path.join(stateDir, "desktop-lifecycle-status.json"), '{"supervisorPid":42}');
  let starts = 0;
  const lifecycle = { ...options, alive: () => false, ensure: async input => {
    starts++; assert.equal(input.stateDir, stateDir); assert.equal(input.pluginRoot, "unused"); return true;
  } };
  assert.equal(await repairKnown("ensure_desktop_lifecycle", { code: "bridge_unreachable" }, lifecycle), "desktop_lifecycle_ready");
  assert.equal(starts, 1);
  assert.equal(await repairKnown("ensure_desktop_lifecycle", { code: "bridge_unreachable" }, { ...lifecycle, alive: () => true }), "existing_supervisor_recovering");
  assert.equal(starts, 1);
  await writeFile(configPath, JSON.stringify({ ...config, operatingMode: "unknown-profile" }));
  calls.length = 0;
  assert.equal(await repairKnown("reload_feishu", fault, options), "repair_config_rejected");
  assert.equal(calls.length, 0);
});

test("script lifecycle is opt-in, checks exact process lifetime and starts only the watchdog role", async t => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "script-watchdog-gate-")); t.after(() => rm(stateDir, { recursive: true, force: true }));
  const owner = { pid: 700, startedAt: "owner-lifetime" }; let launches = 0;
  const status = { version: 1, stage: "running", pid: 800, processStartedAt: "watchdog-lifetime", desktopPid: 700, desktopStartedAt: owner.startedAt };
  const statusPath = path.join(stateDir, "script-watchdog-status.json");
  const options = { pluginRoot: "test", stateDir, owner, delay: async () => {},
    readChain: async () => [{ pid: 800, startedAt: "watchdog-lifetime" }, owner],
    launch: async input => { launches++; assert.equal(input.role, "watchdog"); await writeFile(statusPath, JSON.stringify(status)); } };
  assert.equal(await ensureScriptWatchdog(options), false); assert.equal(launches, 0);
  await writeFile(path.join(stateDir, "script-watchdog.local.json"), JSON.stringify({ enabled: true, maintenanceThreadId: "11111111-1111-4111-8111-111111111111" }));
  assert.equal(await ensureScriptWatchdog(options), true); assert.equal(launches, 1);
  assert.equal(await ensureScriptWatchdog(options), true); assert.equal(launches, 1);
  await writeFile(statusPath, JSON.stringify({ ...status, processStartedAt: "reused-old-pid" }));
  assert.equal(await ensureScriptWatchdog(options), true); assert.equal(launches, 2);
});
