import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";
import http from "node:http";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
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

async function fixture(options = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-native-cli-"));
  const bin = path.join(root, "fake-codex.mjs");
  await writeFile(bin, FAKE, { mode: 0o755 });
  const codexHome = path.join(root, "codex-home");
  const stateDir = path.join(root, "state");
  await mkdir(codexHome, { recursive: true });
  const argsFile = path.join(root, "argv.jsonl");
  const sessionId = randomUUID();
  const adapter = new CliNativeAdapter({ codexBin: bin, codexHome, stateDir, defaultCwd: root,
    turnTimeoutMs: 20000, sandbox: "read-only", model: options.model ?? null,
    reasoningEffort: options.reasoningEffort,
    env: { ...process.env, FAKE_ARGS_FILE: argsFile,
      FAKE_SESSION_ID: sessionId, CODEX_HOME: codexHome }, logger: options.logger ?? (() => {}) });
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


// ---------------------------------------------------------------------------
// reasoningEffort: `codex exec` has no flag for it, so it must ride as
// `-c model_reasoning_effort="<value>"` right after `-m`, on both paths.
// ---------------------------------------------------------------------------

const CLI_ENTRY = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "cli.mjs");

const hasEffortArg = argv => argv.some(arg => String(arg).includes("model_reasoning_effort"));

function requestJson(base, pathname, { method = "GET", body = null } = {}) {
  return new Promise((resolve, reject) => {
    const payload = body == null ? null : Buffer.from(JSON.stringify(body), "utf8");
    const req = http.request(`${base}${pathname}`, { method,
      headers: payload ? { "content-type": "application/json", "content-length": payload.length } : {} }, res => {
      const chunks = [];
      res.on("data", chunk => chunks.push(chunk));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        let json = null;
        try { json = text ? JSON.parse(text) : null; } catch { /* non-JSON body */ }
        resolve({ status: res.statusCode, json, text });
      });
    });
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
}

// Boots the real cli.mjs against the fake codex binary so the config -> adapter
// -> argv chain is exercised end to end, not just the adapter in isolation.
async function startBridge({ reasoningEffort, sessionId }) {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-effort-"));
  const bin = path.join(root, "fake-codex.mjs");
  await writeFile(bin, FAKE, { mode: 0o755 });
  const codexHome = path.join(root, "codex-home");
  const stateDir = path.join(root, "state");
  await mkdir(codexHome, { recursive: true });
  const argsFile = path.join(root, "argv.jsonl");
  const logPath = path.join(root, "bridge.log");
  const configPath = path.join(root, "config.json");
  const codex = { bin, home: codexHome, sandbox: "read-only", model: "gpt-6.1-sol" };
  if (reasoningEffort !== undefined) codex.reasoningEffort = reasoningEffort;
  await writeFile(configPath, JSON.stringify({ backend: "cli", host: "127.0.0.1", port: 0, workspace: root,
    codex, feishu: { inboundEnabled: false } }, null, 2));

  const env = { ...process.env, CODEXWORKER_STATE_DIR: stateDir, CODEX_HOME: codexHome,
    FAKE_ARGS_FILE: argsFile, FAKE_SESSION_ID: sessionId,
    NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" };
  delete env.CODEX_APP_TOOLS_PIPE_PATH;

  const child = spawn(process.execPath, [CLI_ENTRY, "--config", configPath, "--port", "0", "--log", logPath],
    { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "", stderr = "";
  child.stdout.setEncoding("utf8"); child.stderr.setEncoding("utf8");
  child.stdout.on("data", chunk => { stdout += chunk; });
  child.stderr.on("data", chunk => { stderr += chunk; });

  const base = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`bridge did not listen in time.\nstdout: ${stdout}\nstderr: ${stderr}`)), 20000);
    const poll = setInterval(() => {
      const match = stdout.match(/listening on (http:\/\/[^\s]+)/);
      if (!match) return;
      clearInterval(poll); clearTimeout(timer);
      resolve(match[1]);
    }, 25);
    child.once("exit", code => {
      clearInterval(poll); clearTimeout(timer);
      reject(new Error(`bridge exited before listening (${code}).\nstdout: ${stdout}\nstderr: ${stderr}`));
    });
  });

  const stop = async () => {
    if (child.exitCode === null) {
      const exited = new Promise(resolve => child.once("exit", resolve));
      child.kill("SIGTERM");
      await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 5000))]);
      if (child.exitCode === null) child.kill("SIGKILL");
    }
    await rm(root, { recursive: true, force: true });
  };
  return { base, argsFile, logPath, stop, stderr: () => stderr };
}

async function waitForLog(file, needle) {
  for (let attempt = 0; attempt < 100; attempt++) {
    try { const text = await readFile(file, "utf8"); if (text.includes(needle)) return text; }
    catch { /* log file not flushed yet */ }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`log ${file} never mentioned ${needle}`);
}

test("reasoningEffort=max adds -c model_reasoning_effort after -m on the new and resume paths", async () => {
  const { adapter, argsFile } = await fixture({ model: "gpt-6.1-sol", reasoningEffort: "max" });
  const first = await adapter.dispatch({ text: "只回复两个字母：OK" });
  const second = await adapter.dispatch({ threadId: first.threadId, text: "再来一次" });
  assert.equal(second.sessionId, first.sessionId);

  const calls = await argvLines(argsFile);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0].slice(0, 2), ["exec", "--skip-git-repo-check"], "new session path");
  assert.deepEqual(calls[1].slice(0, 3), ["exec", "resume", first.sessionId], "resume path");
  for (const call of calls) {
    const at = call.indexOf("-m");
    assert.ok(at > 0, `expected -m in ${JSON.stringify(call)}`);
    assert.deepEqual(call.slice(at, at + 4), ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="max"']);
  }
  assert.equal(adapter.status().reasoningEffort, "max");
  await adapter.close();
});

test("no reasoningEffort (unset, null or blank) adds no model_reasoning_effort", async () => {
  for (const reasoningEffort of [undefined, null, "", "   "]) {
    const { adapter, argsFile } = await fixture({ model: "gpt-6.1-sol", reasoningEffort });
    await adapter.dispatch({ text: "只回复两个字母：OK" });
    const [call] = await argvLines(argsFile);
    assert.equal(hasEffortArg(call), false, `unexpected override for ${JSON.stringify(reasoningEffort)}`);
    assert.equal(adapter.status().reasoningEffort, null);
    await adapter.close();
  }
});

test("an unknown reasoningEffort warns once, is ignored, and never reaches argv", async () => {
  const logs = [];
  const { adapter, argsFile } = await fixture({ model: "gpt-6.1-sol", reasoningEffort: "turbo",
    logger: (level, event, fields = {}) => logs.push({ level, event, ...fields }) });
  await adapter.dispatch({ text: "只回复两个字母：OK" });
  const [call] = await argvLines(argsFile);
  assert.equal(hasEffortArg(call), false);
  assert.equal(adapter.status().reasoningEffort, null);
  const warns = logs.filter(entry => entry.event === "reasoning_effort_invalid");
  assert.equal(warns.length, 1);
  assert.equal(warns[0].level, "warn");
  assert.equal(warns[0].value, "turbo");
  await adapter.close();
});

test("cli.mjs reads codex.reasoningEffort, reports it in /health, and passes it on both real argv paths", async () => {
  const bridge = await startBridge({ reasoningEffort: "max", sessionId: randomUUID() });
  try {
    const health = await requestJson(bridge.base, "/health");
    assert.equal(health.status, 200);
    assert.equal(health.json.ready, true);
    assert.equal(health.json.nativeCli.model, "gpt-6.1-sol");
    assert.equal(health.json.nativeCli.reasoningEffort, "max");

    const first = await requestJson(bridge.base, "/turn", { method: "POST", body: { text: "只回复两个字母：OK" } });
    assert.equal(first.json.status, "completed");
    const second = await requestJson(bridge.base, "/turn", { method: "POST",
      body: { threadId: first.json.threadId, text: "再来一次" } });
    assert.equal(second.json.sessionId, first.json.sessionId);

    const calls = await argvLines(bridge.argsFile);
    assert.equal(calls.length, 2);
    assert.equal(calls[0][1], "--skip-git-repo-check");
    assert.deepEqual(calls[1].slice(0, 3), ["exec", "resume", first.json.sessionId]);
    for (const call of calls) {
      const at = call.indexOf("-m");
      assert.deepEqual(call.slice(at, at + 4), ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="max"']);
    }
  } finally { await bridge.stop(); }
});

test("cli.mjs stays up on an invalid reasoningEffort: warn logged, no override, family keeps working", async () => {
  const bridge = await startBridge({ reasoningEffort: "turbo", sessionId: randomUUID() });
  try {
    const health = await requestJson(bridge.base, "/health");
    assert.equal(health.status, 200);
    assert.equal(health.json.ready, true);
    assert.equal(health.json.nativeCli.reasoningEffort, null);
    assert.match(bridge.stderr(), /warn reasoning_effort_invalid/);
    assert.match(bridge.stderr(), /"value":"turbo"/);

    const turn = await requestJson(bridge.base, "/turn", { method: "POST", body: { text: "只回复两个字母：OK" } });
    assert.equal(turn.json.status, "completed");
    const [call] = await argvLines(bridge.argsFile);
    assert.equal(hasEffortArg(call), false);

    const logText = await waitForLog(bridge.logPath, "reasoning_effort_invalid");
    assert.match(logText, /"level":"warn"/);
  } finally { await bridge.stop(); }
});
