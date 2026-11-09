import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { CliNativeAdapter, probeNativeBackend, resolveCodexBinary } from "../native-cli.mjs";
import { publicProgress } from "../native-progress.mjs";
import { randomUUID } from "node:crypto";

// A stand-in for `codex exec --json`. It records argv, writes a rollout file in
// the real codex layout, and streams the same NDJSON event types.
const FAKE = `#!/usr/bin/env node
import { createInterface } from "node:readline";
import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const argv = process.argv.slice(2);
if (argv[0] === "--version") { console.log("codex-cli 0.0.0-fake"); process.exit(0); }
appendFileSync(process.env.FAKE_ARGS_FILE, JSON.stringify(argv) + "\\n");
let prompt = "";
for await (const line of createInterface({ input: process.stdin })) prompt += line + "\\n";
prompt = prompt.trim();
const resume = argv[0] === "exec" && argv[1] === "resume" ? argv[2] : null;
const sessionId = resume ?? process.env.FAKE_SESSION_ID;
const turnId = resume ? "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeee02" : "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeee01";
const rollouts = path.join(process.env.CODEX_HOME, "sessions", "2026", "11", "09");
mkdirSync(rollouts, { recursive: true });
const file = path.join(rollouts, "rollout-2026-11-09T00-00-00-" + sessionId + ".jsonl");
const ts = new Date().toISOString();
const records = [
  ...(resume ? [] : [{ timestamp: ts, type: "session_meta", payload: { session_id: sessionId, id: sessionId, timestamp: ts, cwd: process.cwd() } }]),
  { timestamp: ts, type: "turn_context", payload: { turn_id: turnId, cwd: process.cwd() } },
  { timestamp: ts, type: "response_item", payload: { type: "message", id: "msg_u_" + turnId, role: "user", content: [{ type: "input_text", text: prompt }] } },
  { timestamp: ts, type: "event_msg", payload: { type: "task_started", turn_attribution: { turn_id: turnId } } },
  { timestamp: ts, type: "response_item", payload: { type: "message", id: "msg_a_" + turnId, role: "assistant", phase: "final", content: [{ type: "output_text", text: resume ? "上一轮是 OK" : "OK" }] } },
  { timestamp: ts, type: "event_msg", payload: { type: "task_complete", turn_id: turnId, last_agent_message: resume ? "上一轮是 OK" : "OK" } },
];
writeFileSync(file, records.map(record => JSON.stringify(record)).join("\\n") + "\\n", { flag: "a" });
console.log(JSON.stringify({ type: "thread.started", thread_id: sessionId }));
console.log(JSON.stringify({ type: "turn.started" }));
console.log(JSON.stringify({ type: "item.completed", item: { id: "item_1", type: "command_execution", command: "printf ok", aggregated_output: "ok", exit_code: 0, status: "completed" } }));
console.log(JSON.stringify({ type: "item.completed", item: { id: "item_2", type: "agent_message", text: resume ? "上一轮是 OK" : "OK" } }));
console.log(JSON.stringify({ type: "turn.completed", usage: { input_tokens: 10, output_tokens: 2 } }));
`;

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-native-cli-"));
  const bin = path.join(root, "fake-codex.mjs");
  await writeFile(bin, FAKE, { mode: 0o755 });
  const codexHome = path.join(root, "codex-home");
  const stateDir = path.join(root, "state");
  await mkdir(codexHome, { recursive: true });
  const argsFile = path.join(root, "argv.jsonl");
  const sessionId = randomUUID();
  const adapter = new CliNativeAdapter({ codexBin: bin, codexHome, stateDir, defaultCwd: root,
    turnTimeoutMs: 20000, sandbox: "read-only", env: { ...process.env, FAKE_ARGS_FILE: argsFile,
      FAKE_SESSION_ID: sessionId, CODEX_HOME: codexHome }, logger: () => {} });
  await adapter.init();
  return { root, adapter, argsFile, sessionId, codexHome };
}

const argvLines = async file => (await readFile(file, "utf8")).trim().split("\n").map(line => JSON.parse(line));

test("backend probe prefers cli without a Desktop pipe and honours an explicit override", () => {
  const cli = probeNativeBackend({}, {});
  assert.equal(cli.backend, "cli"); assert.equal(cli.available, true); assert.equal(cli.attached, true);
  const desktop = probeNativeBackend({ CODEX_APP_TOOLS_PIPE_PATH: "C:/pipe" }, {});
  assert.equal(desktop.backend, "desktop"); assert.equal(desktop.pipeConfigured, true);
  assert.equal(probeNativeBackend({ CODEX_APP_TOOLS_PIPE_PATH: "C:/pipe" }, { backend: "cli" }).backend, "cli");
});

test("resolveCodexBinary finds an executable on PATH and ignores missing config bins", async () => {
  const found = await resolveCodexBinary({ bin: "/definitely/missing/codex" }, process.env);
  assert.ok(found === null || typeof found === "string");
  const node = await resolveCodexBinary({ bin: process.execPath }, process.env);
  assert.equal(node, process.execPath);
});

test("first turn creates a session, second turn resumes it, progress/history/transcript read back", async () => {
  const { adapter, argsFile, sessionId } = await fixture();
  const first = await adapter.dispatch({ text: "只回复两个字母：OK" });
  assert.equal(first.status, "completed");
  assert.equal(first.resultText, "OK");
  assert.equal(first.sessionId, sessionId);
  assert.equal(first.threadId, sessionId);
  assert.equal(first.turnId, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeee01");
  assert.equal(first.toolCalls.length, 1);
  assert.equal(first.toolCalls[0].name, "shell");
  assert.equal(first.toolCalls[0].exitCode, 0);

  const second = await adapter.dispatch({ threadId: first.threadId, text: "刚才我让你回什么？两个字" });
  assert.equal(second.status, "completed");
  assert.equal(second.resultText, "上一轮是 OK");
  assert.equal(second.sessionId, sessionId);

  const calls = await argvLines(argsFile);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0].slice(0, 2), ["exec", "--skip-git-repo-check"]);
  assert.deepEqual(calls[1].slice(0, 4), ["exec", "resume", sessionId, "--skip-git-repo-check"]);
  for (const call of calls) assert.equal(call.at(-1), "-", "prompt must travel over stdin");

  const progress = adapter.progress(first.threadId);
  const publicView = publicProgress(progress, first.threadId);
  assert.equal(publicView.message.text, "上一轮是 OK");
  assert.equal(publicView.turnStatus, "completed");

  const history = await adapter.history({ limit: 10 });
  const listed = history.find(thread => thread.id === first.threadId);
  assert.ok(listed);
  assert.equal(listed.turns, 2);
  assert.equal(listed.title, "只回复两个字母：OK");
  assert.equal(listed.source, "cli");

  const snapshot = await adapter.readThread(first.threadId, { turnLimit: 10 });
  assert.equal(snapshot.turns.length, 2);
  assert.equal(snapshot.turns[0].items[0].type, "userMessage");
  assert.equal(snapshot.turns[0].items[0].text, "只回复两个字母：OK");
  assert.equal(snapshot.turns[1].items.at(-1).text, "上一轮是 OK");
  await adapter.close();
});

test("unmanaged codex sessions appear in history and can be adopted", async () => {
  const { adapter, codexHome } = await fixture();
  const id = randomUUID();
  const dir = path.join(codexHome, "sessions", "2026", "11", "09");
  await mkdir(dir, { recursive: true });
  const file = path.join(dir, `rollout-2026-11-09T01-00-00-${id}.jsonl`);
  const ts = "2026-11-09T01:00:00.000Z";
  await writeFile(file, [
    { timestamp: ts, type: "session_meta", payload: { session_id: id, timestamp: ts, cwd: "/tmp/other" } },
    { timestamp: ts, type: "response_item", payload: { type: "message", id: "u1", role: "user", content: [{ type: "input_text", text: "外部会话的问题" }] } },
    { timestamp: ts, type: "response_item", payload: { type: "message", id: "a1", role: "assistant", content: [{ type: "output_text", text: "外部回答" }] } },
    { timestamp: ts, type: "event_msg", payload: { type: "task_complete", turn_id: "turn-x", last_agent_message: "外部回答" } },
  ].map(record => JSON.stringify(record)).join("\n") + "\n");

  const history = await adapter.history({ limit: 10 });
  const row = history.find(thread => thread.id === id);
  assert.ok(row); assert.equal(row.source, "codex-session"); assert.equal(row.title, "外部会话的问题");
  assert.equal(row.lastText, "外部回答");

  const adopted = await adapter.adopt(id);
  assert.equal(adopted.sessionId, id); assert.equal(adopted.cwd, "/tmp/other");
  assert.equal((await adapter.history({ limit: 10 })).find(thread => thread.id === id).source, "cli");
  await assert.rejects(adapter.adopt(randomUUID()), error => error.status === 404);
  await adapter.close();
});
