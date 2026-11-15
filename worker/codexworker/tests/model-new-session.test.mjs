import test from "node:test";
import assert from "node:assert/strict";
import { DesktopManager } from "../desktop-manager.mjs";

for (const feishu of [false, true]) test(`new chat applies worker defaults only to a Feishu command, feishu=${feishu}`, async () => {
  const manager = new DesktopManager({ stateDir: "/unused-state", config: { projectId: "project", workspaceRoots: ["/test"] } });
  const calls = [], request = { messageId: "new-request", source: "trainer", targetThreadId: "main" };
  let defaultsRead = 0;
  manager.modelCommands.defaults = async () => { defaultsRead++; return { model: "selected-model", effort: "high" }; };
  manager.save = async () => {};
  manager.emit = async () => {};
  manager.read = async () => ({ thread: { id: "new-chat", cwd: "/test" }, turns: [] });
  manager.call = async (name, args) => { calls.push({ name, args }); return { threadId: "new-chat" }; };
  manager.state.selectedThreadId = "main";
  manager.state.sessions.main = manager.makeSession({ id: "main" }, { cwd: "/test" });
  if (feishu) manager.state.feishuReceipts[request.messageId] = { incomingMessageId: "om_actual_input" };
  await manager.command(request, "/new");
  assert.equal(defaultsRead, feishu ? 1 : 0);
  assert.equal(calls[0].args.model, feishu ? "selected-model" : undefined);
  assert.equal(calls[0].args.thinking, feishu ? "high" : undefined);
  assert.equal(manager.state.sessions.main.threadId, "main");
});

test("Feishu model commands pass raw text and identity to the router parser", async () => {
  const manager = new DesktopManager({ stateDir: "/unused-state", config: { machine: "WorkerName" } });
  const calls = [];
  manager.routerClient = { _request: async (...args) => { calls.push(args); return { text: "native list" }; } };
  const result = await manager.command({ messageId: "stable-id", targetThreadId: "original" }, "/model next");
  assert.equal(result.text, "native list");
  assert.deepEqual(calls[0].slice(0, 3), ["POST", "/commands", { worker: "WorkerName", message_id: "stable-id", thread_id: "original", text: "/model next" }]);
  manager.routerClient._request = async () => { throw { status: 422, body: { error: "bad_model", detail: "模型不可用" } }; };
  await assert.rejects(manager.command({ messageId: "next-id" }, "/model missing"), error => error.status === 422 && error.body.detail === "模型不可用");
});
