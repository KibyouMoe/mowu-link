import test from "node:test";
import assert from "node:assert/strict";
import { dshModelSettings, dshSelection } from "../interface/model-settings.mjs";

test("DSH changes only the selected native agent and returns native effort IDs", async () => {
  let selected = { provider: "provider-a", model: "same-id", reasoningEffort: "high" };
  const calls = [];
  const catalog = { default: { ...selected }, groups: ["provider-a", "provider-b"].map(id => ({ id,
    models: [{ id: "same-id", name: "Native Model", reasoning: { efforts: [{ id: "low", name: "低" }, { id: "high", name: "高" }], defaultEffort: "high" } }] })) };
  const llm = { resolveCallConfig: async value => { calls.push(value); } };
  const services = { sessionController: { modelCatalog: async () => catalog }, llm,
    sessions: { flush: async () => calls.push("flushed") } };
  const agent = { session: {}, ctx: { get: name => services[name] },
    model: { current: () => selected, select: value => { selected = value; } } };
  const thread = {};
  const transport = { ctx: agent.ctx, requireThread: () => thread, ensureAgent: async () => agent,
    save: async () => calls.push("saved") };
  const result = await dshModelSettings(transport, { threadId: "original", action: "update", model: "provider-b/same-id", effort: "low" });
  assert.deepEqual(result.current, { model: "provider-b/same-id", effort: "low" });
  assert.deepEqual(catalog.default, { provider: "provider-a", model: "same-id", reasoningEffort: "high" });
  assert.deepEqual(thread.modelSelection, selected);
  assert.deepEqual(calls.at(-2), "flushed"); assert.equal(calls.at(-1), "saved");
  await assert.rejects(dshSelection(transport, "provider-b/same-id", "invented"), /unavailable/);
});
