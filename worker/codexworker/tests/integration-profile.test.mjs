import assert from "node:assert/strict";
import test from "node:test";
import { operatingConfig } from "../integration-profile.mjs";

test("local Feishu mode keeps Feishu credentials and never mutates the original configuration", () => {
  const config = { operatingMode: "feishu-local", tokenFile: "local.token", feishu: { configFile: "feishu.json" } };
  const before = structuredClone(config), local = operatingConfig(config);
  assert.deepEqual(local.feishu, config.feishu); assert.equal(local.tokenFile, config.tokenFile);
  assert.equal(local.operatingMode, "feishu-local");
  assert.deepEqual(config, before);
});

test("Desktop mode preserves explicit settings and invalid profiles do not silently fall back", () => {
  const config = { port: 8903, feishu: { configFile: "feishu.json" } };
  assert.deepEqual(operatingConfig(config), { ...config, operatingMode: "desktop-bridge" });
  assert.throws(() => operatingConfig({ operatingMode: "unknown" }), /unsupported_operating_mode/);
});
