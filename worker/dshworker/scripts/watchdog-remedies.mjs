// Add each verified recurring repair here with a reproducing test. Unknown
// failures escalate once; never turn a broad restart into a guessed remedy.
import { readFile } from "node:fs/promises";
import path from "node:path";
import { ensureDesktopSupervisor } from "../desktop-lifecycle.mjs";
import { configuredBridgeIdentity, matchesBridgeIdentity } from "./watchdog-context.mjs";
import { auditTextReceipts } from "./audit-text-receipts.mjs";
import { restoreZeroedBusinessFiles } from "./installation-recovery.mjs";

export const REMEDIES = Object.freeze({
  feishu_disconnected: "reload_feishu", feishu_heartbeat_stale: "reload_feishu",
  feishu_watchdog_stale: "reload_feishu", feishu_watchdog_unavailable: "reload_feishu",
  bridge_unreachable: "ensure_desktop_lifecycle",
  installation_business_corrupt: "restore_business_files",
  native_relay_read_failed: "reload_business", native_relay_stalled: "reload_business",
  feishu_text_send_unconfirmed: "audit_text_receipts"
});
export const remedyFor = code => REMEDIES[code] ?? null;

export async function repairKnown(remedy, report, { stateDir, pluginRoot, fetchImpl = fetch,
  ensure = ensureDesktopSupervisor, alive = pid => { try { process.kill(pid, 0); return true; } catch { return false; } } }) {
  if (remedyFor(report.code) !== remedy) return "remedy_not_applicable";
  const config = JSON.parse(await readFile(path.join(stateDir, "desktop-config.local.json"), "utf8"));
  const port = config.port ?? 8903;
  const expected = configuredBridgeIdentity(config);
  if ((config.host ?? "127.0.0.1") !== "127.0.0.1" || !expected ||
      !Number.isInteger(port) || port < 1 || port > 65535) return "repair_config_rejected";
  if (remedy === "restore_business_files") return restoreZeroedBusinessFiles({ pluginRoot, stateDir });
  if (remedy === "ensure_desktop_lifecycle") {
    const restored = await restoreZeroedBusinessFiles({ pluginRoot, stateDir });
    if (restored === "installation_restored") return restored;
    const status = JSON.parse(await readFile(path.join(stateDir, "desktop-lifecycle-status.json"), "utf8"));
    // Existing live supervisor already owns bounded child recovery. Do not
    // compete with it or terminate healthy/unknown processes from a timer.
    if (Number.isSafeInteger(status.supervisorPid) && alive(status.supervisorPid)) return "existing_supervisor_recovering";
    return await ensure({ pluginRoot, stateDir }) ? "desktop_lifecycle_ready" : "desktop_lifecycle_unavailable";
  }
  const token = (await readFile(config.tokenFile, "utf8")).trim();
  if (token.length < 24) return "repair_credential_rejected";
  const endpoint = `http://127.0.0.1:${port}`;
  const headers = { authorization: "Bearer " + token };
  const probe = await fetchImpl(endpoint + "/health", { headers, signal: AbortSignal.timeout(3000) });
  if (!probe.ok) return "repair_health_rejected";
  const health = await probe.json();
  if (!matchesBridgeIdentity(health, expected)) return "repair_identity_rejected";
  if (remedy === "audit_text_receipts") {
    await auditTextReceipts({ stateDir, fetchImpl });
    return "text_receipts_audited_no_replay";
  }
  const route = remedy === "reload_business" ? "/bridge/runtime/reload" : "/bridge/integrations/reload";
  const response = await fetchImpl(endpoint + route, { method: "POST", headers, signal: AbortSignal.timeout(15000) });
  return response.ok ? (remedy === "reload_business" ? "business_reload_requested" : "integrations_reloaded") : "reload_rejected";
}
