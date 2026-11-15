import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { modelSettings } from "../interface/desktop-connection/model-settings.mjs";
import { createNativeInput } from "../interface/desktop-connection/native-input.mjs";
import { sendNativeInput } from "../interface/native-input-client.mjs";
import { randomUUID } from "node:crypto";

test("native model control uses catalog and thread settings RPCs on the owning thread", async t => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "native-model-"));
  t.after(() => rm(stateDir, { recursive: true, force: true }));
  await writeFile(path.join(stateDir, "desktop-manager-state.json"), JSON.stringify({ sessions: { original: {} } }));
  const calls = []; let current = { model: "native-a", reasoningEffort: "high" };
  const rpc = async (method, params) => {
    calls.push({ method, params });
    if (method === "model/list") return { data: [{ model: "native-a", displayName: "Native A", isDefault: true,
      defaultReasoningEffort: "high", supportedReasoningEfforts: [{ reasoningEffort: "high" }, { reasoningEffort: "ultra" }] }] };
    if (method === "thread/settings/update") { current = { model: params.model, reasoningEffort: params.effort }; return {}; }
    if (method === "thread/read") return { thread: current };
    assert.fail(method);
  };
  const result = await modelSettings({ rpc, stateDir, request: { action: "update", threadId: "original", model: "native-a", effort: "ultra" } });
  assert.deepEqual(result.current, { model: "native-a", effort: "ultra" });
  assert.deepEqual(calls[1], { method: "thread/settings/update", params: { threadId: "original", model: "native-a", effort: "ultra" } });
  await assert.rejects(modelSettings({ rpc, stateDir, request: { action: "read", threadId: "foreign" } }), /thread_unavailable/);
});

test("Desktop model capability crosses the real local socket with owner validation", async t => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "native-model-wire-"));
  await writeFile(path.join(stateDir, "desktop-manager-state.json"), JSON.stringify({ sessions: { original: {} } }));
  const owner = { pid: process.pid, startedAt: "isolated-desktop-lifetime" };
  let controller;
  controller = await createNativeInput({ stateDir, owner, send: message => {
    const result = message.method === "model/list" ? { data: [{ model: "model-a", displayName: "A", isDefault: true,
      defaultReasoningEffort: "high", supportedReasoningEfforts: [{ reasoningEffort: "high" }] }] }
      : { thread: { model: "model-a", reasoningEffort: "high" } };
    setImmediate(() => controller.server({ id: message.id, result }));
  } });
  t.after(async () => { await controller.close(); await rm(stateDir, { recursive: true, force: true }); });
  controller.client({ method: "initialized" });
  const env = { ...controller.env, CODEXWORKER_DESKTOP_PID: String(owner.pid), RUDOLF_BRIDGE_DESKTOP_STARTED_AT: owner.startedAt };
  const result = await sendNativeInput({ operation: "modelSettings", messageId: randomUUID(), threadId: "original", action: "read" }, { env });
  assert.deepEqual(JSON.parse(result.contentItems[0].text).current, { model: "model-a", effort: "high" });
  await assert.rejects(sendNativeInput({ operation: "modelSettings", messageId: randomUUID(), threadId: "original", action: "read" },
    { env: { ...env, CODEXWORKER_DESKTOP_PID: "foreign" } }), /owner_mismatch/);
});
