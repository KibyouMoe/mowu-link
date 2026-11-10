// Hermetic checks for the dsh native transport: tool contract + thread lifecycle
// that does NOT need a model round-trip (no dsh child process is spawned).
// The live create → send → wait → read chain is exercised manually against dsh.
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test, { after } from "node:test";

const stateDir = await mkdtemp(path.join(os.tmpdir(), "dshworker-transport-"));
process.env.CODEXWORKER_STATE_DIR = stateDir;
process.env.CODEXWORKER_NATIVE_TRANSPORT = "dsh";

const dsh = await import("../codex/transport/dsh.mjs");
const { resetNativeTransportCache, resolveNativeTransport } = await import("../codex/transport/index.mjs");
resetNativeTransportCache();
const transport = resolveNativeTransport();

const invocation = { threadId: "test-harness", turnId: "test-harness-turn", callId: "test-harness-call" };
const EXPECTED_TOOLS = [
  "create_thread",
  "list_projects",
  "list_threads",
  "read_thread",
  "send_message_to_thread",
  "set_thread_title",
  "wait_threads",
];

function payload(response) {
  assert.ok(response, "transport returned no response");
  assert.equal(response.success, true, `transport returned success=false: ${JSON.stringify(response)}`);
  const text = response?.contentItems?.[0]?.text;
  assert.equal(typeof text, "string", "transport returned no text content item");
  return JSON.parse(text);
}

after(async () => {
  await dsh.shutdown();
  await rm(stateDir, { recursive: true, force: true });
});

test("dsh transport is selectable and exposes the seven native tools", async () => {
  assert.equal(transport.name, "dsh");
  assert.equal(dsh.available(), true, dsh.unavailableReason());
  const tools = await transport.listTools();
  assert.equal(tools.length, 7);
  assert.deepEqual(tools.map((tool) => tool.name).sort(), [...EXPECTED_TOOLS].sort());
  for (const tool of tools) {
    assert.equal(tool.namespace, "dsh");
    assert.equal(typeof tool.description, "string");
    assert.equal(tool.inputSchema.type, "object");
  }
});

test("fork_thread / navigate_to_codex_page are explicit unsupported responses", async () => {
  for (const toolName of ["fork_thread", "navigate_to_codex_page"]) {
    const response = await transport.callTool(toolName, { threadId: "x" }, invocation);
    assert.equal(response.success, false);
    const parsed = payload0(response);
    assert.equal(parsed.error, "unsupported_tool");
    assert.match(parsed.reason, /dsh/);
  }
});

test("create → read → title → list stays local and desktop-shaped", async () => {
  const created = payload(await transport.callTool("create_thread", {
    cwd: os.tmpdir(),
    title: "dsh transport 单测",
  }, invocation, 30_000));
  assert.equal(typeof created.threadId, "string");
  assert.equal(created.thread.cwd, path.resolve(os.tmpdir()));
  assert.equal(created.thread.status, "idle");

  const snapshot = payload(await transport.callTool("read_thread", { threadId: created.threadId }, invocation, 30_000));
  assert.equal(snapshot.thread.id, created.threadId);
  assert.deepEqual(snapshot.turns, []);
  assert.equal(snapshot.page.returnedTurns, 0);

  // timeoutMs:0 必须立刻返回快照，不能挂住。
  const startedAt = Date.now();
  const waited = payload(await transport.callTool("wait_threads", {
    targets: [{ threadId: created.threadId }], timeoutMs: 0,
  }, invocation, 10_000));
  assert.ok(Date.now() - startedAt < 1_000, "wait_threads timeoutMs=0 must return immediately");
  assert.equal(waited.snapshot, true);
  assert.equal(waited.timedOut, false);
  assert.equal(waited.polls[0].thread.id, created.threadId);

  const titled = payload(await transport.callTool("set_thread_title", {
    threadId: created.threadId, title: "dsh 改名",
  }, invocation, 10_000));
  assert.equal(titled.updated, true);

  const listed = payload(await transport.callTool("list_threads", { limit: 10 }, invocation, 10_000));
  assert.equal(listed.source, "dsh");
  const found = listed.threads.find((thread) => thread.id === created.threadId);
  assert.equal(found?.title, "dsh 改名");

  const projects = payload(await transport.callTool("list_projects", {}, invocation, 10_000));
  assert.ok(projects.projects.some((project) => project.path === path.resolve(os.tmpdir()) && project.threadCount >= 1));
});

test("unknown thread ids fail loudly instead of fabricating state", async () => {
  await assert.rejects(
    () => transport.callTool("read_thread", { threadId: "does-not-exist" }, invocation, 5_000),
    /unknown dsh thread/,
  );
});

function payload0(response) {
  return JSON.parse(response.contentItems[0].text);
}
