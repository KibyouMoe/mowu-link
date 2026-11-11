// Live acceptance for the app-server native transport.
//
// Requires a logged-in Codex CLI (`codex login status`) and the local http
// proxy used by codex. This test talks to a real `codex app-server` process;
// it is skipped only when the codex binary is missing.
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test, { after } from "node:test";
import * as appServer from "../interface/transport/app-server.mjs";
import { resetNativeTransportCache, resolveNativeTransport } from "../interface/transport/index.mjs";

process.env.CODEXWORKER_NATIVE_TRANSPORT = "app-server";
resetNativeTransportCache();
const transport = resolveNativeTransport();

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const invocation = { threadId: "test-harness", turnId: "test-harness-turn", callId: "test-harness-call" };
const EXPECTED_TOOLS = [
  "compact_thread",
  "create_thread",
  "fork_thread",
  "list_projects",
  "list_threads",
  "navigate_to_codex_page",
  "read_thread",
  "send_message_to_thread",
  "set_thread_title",
  "wait_threads",
];

function jsonPayload(response) {
  const text = response?.contentItems?.[0]?.text;
  assert.equal(typeof text, "string", "transport returned no text content item");
  return JSON.parse(text);
}

function payload(response) {
  assert.ok(response, "transport returned no response");
  assert.equal(response.success, true, `transport returned success=false: ${JSON.stringify(response)}`);
  return jsonPayload(response);
}

after(async () => {
  await appServer.shutdown();
});

test("app-server transport exposes ten native tools", async () => {
  assert.equal(transport.name, "app-server");
  assert.equal(appServer.available(), true, appServer.unavailableReason());
  const tools = await transport.listTools();
  const names = tools.map((tool) => tool.name).sort();
  assert.equal(tools.length, 10);
  assert.deepEqual(names, [...EXPECTED_TOOLS].sort());
  for (const tool of tools) {
    assert.equal(tool.namespace, "app-server");
    assert.equal(typeof tool.description, "string");
  }
  console.log("[listTools]", names.join(", "));
});

test("navigate_to_codex_page returns an explicit unsupported response", async () => {
  const response = await transport.callTool("navigate_to_codex_page", { page: "settings" }, invocation);
  assert.equal(response.success, false);
  const parsed = jsonPayload(response);
  assert.equal(parsed.error, "unsupported_tool");
  assert.match(parsed.reason, /app-server/);
  console.log("[unsupported]", JSON.stringify(parsed));
});

test("create → send → wait → read → title against live app-server", { timeout: 180_000 }, async () => {
  // 1. create_thread
  const created = payload(await transport.callTool("create_thread", {
    cwd: repoRoot,
    title: "CodexWorker app-server 验收",
  }, invocation, 30_000));
  assert.equal(typeof created.threadId, "string");
  assert.ok(created.threadId.length >= 20, `unexpected threadId: ${created.threadId}`);
  console.log("[create_thread]", created.threadId, "title=", created.title);

  // 2. send_message_to_thread
  const prompt = "只回复 OK，不要用工具";
  const sent = payload(await transport.callTool("send_message_to_thread", {
    threadId: created.threadId,
    prompt,
  }, invocation, 30_000));
  assert.equal(sent.threadId, created.threadId);
  assert.equal(typeof sent.turnId, "string");
  assert.equal(sent.accepted, true);
  console.log("[send_message_to_thread]", sent.turnId, sent.turn.status);

  // 3. wait_threads — bounded by the turn/completed notification.
  const waited = payload(await transport.callTool("wait_threads", {
    targets: [{ threadId: created.threadId, hostId: "local" }],
    timeoutMs: 120_000,
  }, invocation, 30_000));
  const poll = waited.polls.find((entry) => entry.thread.id === created.threadId);
  assert.ok(poll, "wait_threads returned no poll for the created thread");
  assert.equal(typeof poll.latestTurn?.id, "string");
  assert.equal(poll.latestTurn.id, sent.turnId);
  assert.equal(waited.timedOut, false, `wait_threads timed out: ${JSON.stringify(waited)}`);
  assert.equal(poll.latestTurn.status, "completed");
  console.log("[wait_threads]", JSON.stringify(poll.latestTurn), "timedOut=", waited.timedOut);

  // 4. read_thread — must contain the agent's reply, not just the user input.
  const snapshot = payload(await transport.callTool("read_thread", {
    threadId: created.threadId,
    turnLimit: 10,
    includeOutputs: false,
  }, invocation, 60_000));
  assert.equal(snapshot.thread.id, created.threadId);
  assert.equal(snapshot.thread.kind, "codex");
  assert.equal(snapshot.thread.hostId, "local");
  const turns = snapshot.turns ?? [];
  assert.ok(turns.length >= 1, "read_thread returned no turns");
  const items = turns.flatMap((turn) => turn.items ?? []);
  const agentMessages = items.filter((item) => item.type === "agentMessage" && typeof item.text === "string");
  assert.ok(agentMessages.length >= 1, `read_thread items: ${JSON.stringify(items).slice(0, 800)}`);
  assert.ok(agentMessages.some((item) => item.text.includes("OK")), `no OK reply in ${JSON.stringify(agentMessages)}`);
  console.log("[read_thread] turns=", turns.length, "items=", items.length,
    "reply=", JSON.stringify(agentMessages.at(-1)?.text));

  // 5. set_thread_title — verify by reading the thread metadata back.
  const titled = payload(await transport.callTool("set_thread_title", {
    threadId: created.threadId,
    title: "app-server 验收标题",
  }, invocation, 30_000));
  assert.equal(titled.updated, true);
  const reread = payload(await transport.callTool("read_thread", {
    threadId: created.threadId,
    turnLimit: 1,
    includeOutputs: false,
  }, invocation, 60_000));
  assert.equal(reread.thread.title, "app-server 验收标题");
  console.log("[set_thread_title]", reread.thread.title);
});

test("list_threads returns the created thread in desktop shape", async () => {
  // Runs after the end-to-end test; the singleton client keeps the thread id.
  const listing = payload(await transport.callTool("list_threads", { limit: 10 }, invocation, 30_000));
  assert.equal(listing.schemaVersion, 4);
  assert.ok(Array.isArray(listing.threads));
  assert.ok(listing.threads.length >= 1);
  for (const thread of listing.threads) {
    assert.equal(thread.kind, "codex");
    assert.equal(thread.hostId, "local");
    assert.equal(typeof thread.id, "string");
  }
  console.log("[list_threads]", listing.threads.length, "threads");
});

test("list_projects returns the workspace in desktop shape", async () => {
  const result = payload(await transport.callTool("list_projects", {}, invocation, 30_000));
  assert.equal(result.schemaVersion, 1);
  assert.equal(result.source, "app-server");
  assert.ok(Array.isArray(result.projects));
  assert.ok(result.projects.length >= 1, "list_projects returned no projects");
  const project = result.projects.find((entry) => entry.path === repoRoot);
  assert.ok(project, `no project for ${repoRoot}: ${JSON.stringify(result.projects)}`);
  assert.equal(project.hostId, "local");
  assert.equal(project.kind, "local");
  assert.equal(typeof project.projectId, "string");
  assert.ok(project.projectId.length >= 16);
  console.log("[list_projects]", result.projects.length, "projects;", project.projectId, project.path);
});
