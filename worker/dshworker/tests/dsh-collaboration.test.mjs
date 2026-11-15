import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { dshFixture } from "./fixtures/dsh-harness.mjs";
import { registerCollaborationHost } from "../interface/collaboration-host.mjs";

test("workbench plugin registers a read-only collaboration tab and header entry", async () => {
  let module;
  const components = [], effects = [];
  let state;
  const React = { createElement: (type, props, ...children) => ({ type, props, children }),
    useState: initial => [state ?? initial, value => { state = value; }], useEffect: effect => effects.push(effect) };
  const source = await readFile(new URL("../interface/collaboration-client.js", import.meta.url), "utf8");
  vm.runInNewContext(source, { AbortController, window: { __ModuleLoader__: { load(value) { module = value; } } } });
  const plugin = module.factory(name => name === "react" ? React : {});
  const tabs = [];
  const ctx = { effect: fn => fn(),
    connection: { rpc: { call: async (_path, endpoint) => {
      assert.equal(endpoint, "workerCollaboration/view");
      return { ok: true, value: { url: "http://127.0.0.1:8906/collaboration/#read-key" } };
    } } },
    sidebarRightTabs: { register: tab => { tabs.push(tab); } },
    slots: { inject: (_slot, register) => register(), register: (definition, component) => components.push({ definition, component }) } };
  plugin.apply(ctx);
  assert.equal(tabs[0].title(), "协作记录");
  assert.equal(components.find(value => value.definition.name === "sidebar.panellist").definition.label(), "协作记录");
  assert.equal(components.find(value => value.definition.name === "main").component().type, "p");
  assert.ok(!components.some(value => value.definition.name === "conversation.session.header.corner"));
  effects[0]();
  await Promise.resolve();
  const frame = components.find(value => value.definition.name === "sidebar.right.pane.tab").component();
  assert.equal(frame.type, "iframe");
  assert.equal(frame.props.title, "协作记录");
  assert.equal(frame.props.referrerPolicy, "no-referrer");
  assert.equal(frame.props.src, "http://127.0.0.1:8906/collaboration/#read-key");
});

test("Harness host exposes only a zero-argument read endpoint over its normal Remote transport", async () => {
  const declarations = [];
  const sdk = { TypertRemoteService: class { constructor(ctx, key) { ctx[key] = this; } },
    Remote(_method, context) { declarations.push(context.name); context.addInitializer(function () { this.marked = true; }); } };
  const ctx = {};
  const service = await registerCollaborationHost(ctx, () => ({ url: "read-only" }), sdk);
  assert.equal(ctx.workerCollaboration, service);
  assert.equal(service.marked, true);
  assert.deepEqual(declarations, ["view"]);
  assert.equal(service.view.length, 0);
  assert.deepEqual(service.view(), { url: "read-only" });
});

test("DSH peer protocol is model-readable system metadata and leaves the visible native input clean", async t => {
  const f = await dshFixture(t);
  const messageId = "e5f9ed6d-a4d6-4d10-84f8-f8ce55db50b4";
  await f.send("来自 OguriCap：系统元数据验收", { source: "hermes", origin: "worker-chat",
    metadata: { workerChat: { messageId, from: "OguriCap", replyTo: null } } });
  const callback = [...f.listeners.get("agent/pre-step")][0];
  const decision = await callback({ agent: f.agent }, async () => ({ kind: "request", messages: [] }));
  assert.equal(decision.messages.at(-1).role, "system");
  assert.match(decision.messages.at(-1).content[0].text, /avoid automatic reply loops/);
  assert.ok(decision.messages.at(-1).content[0].text.includes(messageId));
  const original = { id: "original-system", role: "system", content: [{ type: "text", text: "Keep original engine instructions" }], source: { kind: "system-prompt" } };
  const combined = await callback({ agent: f.agent }, async () => ({ kind: "request", messages: [original] }));
  assert.equal(combined.messages.length, 1);
  assert.equal(combined.messages[0].content[0].text, original.content[0].text);
  assert.equal(original.content.length, 1);
  const repeated = await callback({ agent: f.agent }, async () => combined);
  assert.equal(repeated.messages[0].content.length, 2);
  const visible = f.records.find(event => event.type === "user/message").data;
  assert.equal(visible.content[0].text, "来自 OguriCap：系统元数据验收");
  assert.ok(!JSON.stringify(visible).includes(messageId));
  const other = await callback({ agent: {} }, async () => ({ kind: "request", messages: [] }));
  assert.equal(other.messages.length, 0);
  await f.finish();
});
