import test from "node:test";
import assert from "node:assert/strict";
import { DesktopManager } from "../desktop-manager.mjs";

for (const projectId of ["project", "stale-project"]) for (const feishu of [false, true]) test(`new chat uses a current project and applies only Feishu defaults, project=${projectId}, feishu=${feishu}`, async () => {
  const manager = new DesktopManager({ stateDir: "/unused-state", config: { projectId, workspaceRoots: ["/test"] } });
  const calls = [], request = { messageId: "new-request", source: "trainer", targetThreadId: "main" };
  let defaultsRead = 0;
  manager.modelCommands.defaults = async () => { defaultsRead++; return { model: "selected-model", effort: "high" }; };
  manager.save = async () => {};
  manager.emit = async () => {};
  manager.read = async () => ({ thread: { id: "new-chat", cwd: "/test",
    title: calls.find(call => call.name === "create_thread").args.title }, turns: [] });
  manager.call = async (name, args) => {
    calls.push({ name, args });
    return name === "list_projects"
      ? { projects: [{ projectId: "project", path: "/test", hostId: "local" }] } : { threadId: "new-chat" };
  };
  manager.state.selectedThreadId = "main";
  manager.state.sessions.main = manager.makeSession({ id: "main" }, { cwd: "/test" });
  if (feishu) manager.state.feishuReceipts[request.messageId] = { incomingMessageId: "om_actual_input" };
  await manager.command(request, "/new");
  assert.equal(defaultsRead, feishu ? 1 : 0);
  const created = calls.find(call => call.name === "create_thread").args;
  assert.equal(created.target.projectId, "project");
  assert.equal(created.model, feishu ? "selected-model" : undefined);
  assert.equal(created.thinking, feishu ? "high" : undefined);
  assert.equal(manager.state.sessions.main.threadId, "main");
  assert.equal(calls.some(call => call.name === "set_thread_title"), false);
  assert.equal(manager.state.sessions["new-chat"].titlePending, undefined);
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

for (const unavailable of ["missing", "remote"]) test(`new chat refuses an unavailable local project before creation, ${unavailable}`, async () => {
  const manager = new DesktopManager({ stateDir: "/unused-state", config: { projectId: "stale-project", workspaceRoots: ["/test"] } });
  const calls = [];
  manager.save = async () => {};
  manager.state.selectedThreadId = "main";
  manager.state.sessions.main = manager.makeSession({ id: "main" }, { cwd: "/test" });
  manager.call = async (name) => {
    calls.push(name);
    return { projects: unavailable === "missing" ? [] : [{ projectId: "remote-project", path: "/test", hostId: "other-machine" }] };
  };
  await assert.rejects(manager.command({ messageId: "new-request", targetThreadId: "main" }, "/new"),
    error => error.status === 503 && error.body.error === "bridge_not_ready");
  assert.deepEqual(calls, ["list_projects"]);
});
