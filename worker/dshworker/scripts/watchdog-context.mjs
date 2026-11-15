import path from "node:path";

export const WATCHDOG_RULESET_VERSION = "20261102-text-receipt-review-v1";

// Audit CLI, scheduler and fresh checks must resolve the same local instance.
// Explicit stateDir is useful for isolated tests and machine-specific audits.
export function resolveWatchdogStateDir({ stateDir, env = process.env } = {}) {
  if (stateDir !== undefined) {
    if (typeof stateDir !== "string" || !path.isAbsolute(stateDir)) throw new Error("watchdog_state_directory_invalid");
    return path.resolve(stateDir);
  }
  // Match server.mjs and the installed writer; CODEX_HOME is not yet a writer
  // state-root override. Honoring it here alone would silently audit a different
  // instance. Non-default read-only audits must pass their verified stateDir.
  const codexHome = (env.USERPROFILE || env.HOME) && path.join(env.USERPROFILE || env.HOME, ".codex");
  if (!codexHome || !path.isAbsolute(codexHome)) throw new Error("watchdog_state_directory_unavailable");
  return path.join(codexHome, "state", "codexworker");
}

export function configuredBridgeIdentity(config) {
  const operatingMode = config.operatingMode ?? "desktop-bridge";
  const name = config.bridgeName ?? "CodexWorker", machine = config.machine ?? "codexworker";
  if (!["feishu-local", "desktop-bridge"].includes(operatingMode) ||
      typeof name !== "string" || !name.trim() || typeof machine !== "string" ||
      !/^[a-z][a-z0-9-]{0,63}$/.test(machine)) return null;
  return { name, machine, operatingMode };
}

export function matchesBridgeIdentity(health, expected) {
  return Boolean(expected && health?.integrationMode === "desktop-native" &&
    health.name === expected.name && health.machine === expected.machine &&
    health.operatingMode === expected.operatingMode &&
    (health.integrations?.operatingMode === undefined || health.integrations.operatingMode === expected.operatingMode));
}
