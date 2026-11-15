import assert from "node:assert/strict";
import test from "node:test";
import os from "node:os";
import path from "node:path";
import { configuredBridgeIdentity, matchesBridgeIdentity, resolveWatchdogStateDir } from "../scripts/watchdog-context.mjs";

test("audit and resident watchdog share the writer's CodexWorker state, not an unrelated CODEX_HOME", () => {
  const user = path.join(os.tmpdir(), "watchdog-user");
  assert.equal(resolveWatchdogStateDir({ env: { USERPROFILE: user } }), path.join(user, ".codex", "state", "codexworker"));
  const codex = path.join(os.tmpdir(), "watchdog-custom-codex");
  assert.equal(resolveWatchdogStateDir({ env: { CODEX_HOME: codex, USERPROFILE: user } }), path.join(user, ".codex", "state", "codexworker"));
  assert.equal(resolveWatchdogStateDir({ stateDir: codex, env: {} }), codex);
  assert.throws(() => resolveWatchdogStateDir({ env: {} }));
  assert.throws(() => resolveWatchdogStateDir({ stateDir: "relative" }));
});

test("both supported profiles retain exact configured identity, never accept another host", () => {
  for (const operatingMode of ["desktop-bridge", "feishu-local"]) {
    const expected = configuredBridgeIdentity({ operatingMode, bridgeName: "气槽", machine: "qicao" });
    const health = { name: "气槽", machine: "qicao", operatingMode, integrationMode: "desktop-native" };
    assert.equal(matchesBridgeIdentity(health, expected), true);
    assert.equal(matchesBridgeIdentity({ ...health, machine: "codexworker" }, expected), false);
    assert.equal(matchesBridgeIdentity({ ...health, integrations: { operatingMode: "different" } }, expected), false);
  }
  assert.equal(configuredBridgeIdentity({}).operatingMode, "desktop-bridge");
  assert.equal(configuredBridgeIdentity({ operatingMode: "guess" }), null);
  assert.equal(configuredBridgeIdentity({ bridgeName: "" }), null);
});
