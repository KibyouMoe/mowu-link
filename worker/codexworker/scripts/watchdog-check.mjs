// Fresh process per audit: repaired code/rules are loaded on the next check.
import { readFile, writeFile, rename } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { auditBridge } from "./check-desktop-health.mjs";
import { WatchdogEngine } from "./watchdog-engine.mjs";
import { remedyFor, repairKnown } from "./watchdog-remedies.mjs";
import { escalateIncident } from "./watchdog-native.mjs";
import { desktopOwner, processChain } from "../desktop-lifecycle.mjs";
import { resolveWatchdogStateDir, WATCHDOG_RULESET_VERSION } from "./watchdog-context.mjs";

const pluginRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const stateDir = resolveWatchdogStateDir();
if (!process.send || !process.env.CODEX_APP_TOOLS_PIPE_PATH) throw new Error("Requires the Desktop-owned script watchdog");
process.on("disconnect", () => process.exit(0));
const statePath = path.join(stateDir, "script-watchdog-state.json");
try {
  const owner = desktopOwner(await processChain(Number(process.env.RUDOLF_SCRIPT_WATCHDOG_OWNER_PID)));
  if (!owner || owner.startedAt !== process.env.CODEXWORKER_DESKTOP_STARTED_AT) throw new Error("desktop_lifetime_changed");
  const config = JSON.parse(await readFile(path.join(stateDir, "script-watchdog.local.json"), "utf8"));
  if (config.enabled !== true || !/^[a-f\d-]{36}$/i.test(config.maintenanceThreadId ?? "")) throw new Error("script_watchdog_disabled");
  let state;
  try { state = JSON.parse(await readFile(statePath, "utf8")); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  if (state && state.version !== 1) throw new Error("script_watchdog_state_invalid");
  const save = async value => {
    const temp = statePath + "." + process.pid + ".tmp";
    await writeFile(temp, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 }); await rename(temp, statePath);
  };
  const engine = new WatchdogEngine({ state, save, remedyFor,
    intervalMs: Math.max(15000, Math.min(300000, Number(config.intervalMs) || 300000)),
    repair: (remedy, report) => repairKnown(remedy, report, { stateDir, pluginRoot }),
    escalate: (incident, prompt) => escalateIncident({ threadId: config.maintenanceThreadId, incident, prompt }) });
  const outcome = await engine.tick(await auditBridge({ stateDir }));
  process.send({ ...outcome, checkedAt: engine.state.updatedAt, checks: engine.state.checks,
    rulesetVersion: WATCHDOG_RULESET_VERSION,
    health: engine.state.lastAudit.status, code: engine.state.lastAudit.code,
    incidentId: engine.state.incident?.id ?? null, escalation: engine.state.incident?.alert?.status ?? null });
} catch {
  process.send({ phase: "check_failed", code: "script_check_unavailable", nextMs: 60000 });
} finally { process.disconnect(); }
