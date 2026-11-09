import assert from "node:assert/strict";
import test from "node:test";
import { operatingConfig, optionalIngressFailure } from "../integration-profile.mjs";

test("local Feishu mode removes dormant Hermes credentials/endpoints without mutating the original configuration", () => {
  const config = { operatingMode: "feishu-local", tokenFile: "local.token", feishu: { configFile: "feishu.json" },
    hermesIngress: { enabled: true, host: "192.168.3.106", tokenFile: "absent-hermes.token" },
    xiliu: { enabled: true, url: "http://unavailable-hermes.test" } };
  const before = structuredClone(config), local = operatingConfig(config);
  assert.deepEqual(local.hermesIngress, { enabled: false }); assert.deepEqual(local.xiliu, { enabled: false });
  assert.deepEqual(local.feishu, config.feishu); assert.equal(local.tokenFile, config.tokenFile);
  assert.deepEqual(config, before); assert.ok(!JSON.stringify(local).includes("absent-hermes"));
});

test("legacy Desktop mode preserves explicit Hermes settings and invalid profiles do not silently fall back", () => {
  const config = { hermesIngress: { enabled: true, host: "127.0.0.1" }, xiliu: { enabled: true } };
  assert.deepEqual(operatingConfig(config), { ...config, operatingMode: "desktop-bridge" });
  assert.throws(() => operatingConfig({ operatingMode: "unknown" }), /unsupported_operating_mode/);
});

test("only recognized optional socket failures are isolated; diagnostics contain a code rather than raw exceptions", () => {
  for (const code of ["EADDRNOTAVAIL", "EADDRINUSE", "ENETUNREACH", "EACCES"]) {
    assert.equal(optionalIngressFailure({ code, message: "private diagnostic" }), code);
  }
  assert.equal(optionalIngressFailure(new Error("configuration invalid")), null);
});
