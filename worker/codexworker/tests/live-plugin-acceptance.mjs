// Explicitly authorized native test chats only. This checks the actual plugin
// stdio process and Desktop-injected pipe, not a separate model/session host.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

if (!process.env.CODEX_APP_TOOLS_PIPE_PATH) throw new Error("This acceptance requires the Desktop-injected native tools pipe.");
const threadId = process.argv[2];
if (!threadId) throw new Error("Provide the authorized native acceptance task id.");
const pluginRoot = process.env.HERMES_ACCEPTANCE_PLUGIN_ROOT
  ? path.resolve(process.env.HERMES_ACCEPTANCE_PLUGIN_ROOT)
  : path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const child = spawn(process.execPath, [path.join(pluginRoot, "server.mjs")], {
  cwd: pluginRoot, env: process.env, stdio: ["pipe", "pipe", "pipe"], windowsHide: true,
});
const pending = new Map(); let sequence = 0;
const lines = readline.createInterface({ input: child.stdout });
child.stderr.on("data", chunk => process.stderr.write(chunk));
lines.on("line", line => {
  const response = JSON.parse(line); const request = pending.get(response.id);
  if (!request) return;
  pending.delete(response.id); clearTimeout(request.timer);
  if (response.error) request.reject(new Error(response.error.message)); else request.resolve(response.result);
});
function request(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`MCP acceptance timed out: ${method}`)); }, 60000);
    pending.set(id, { resolve, reject, timer });
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
  });
}
const call = (name, args = {}) => request("tools/call", { name, arguments: args });
child.once("exit", () => {
  for (const { reject, timer } of pending.values()) {
    clearTimeout(timer); reject(new Error("Acceptance plugin exited before replying."));
  }
  pending.clear();
});

try {
  const initialized = await request("initialize", { protocolVersion: "2025-06-18", capabilities: {},
    clientInfo: { name: "codexworker-live-plugin-acceptance", version: "1.0.0" } });
  child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })}\n`);
  assert.equal(initialized.serverInfo.name, "codexworker");
  const probe = await call("bridge_verify_manager_binding");
  assert.equal(probe.structuredContent.nativeReadProbeSucceeded, true, JSON.stringify(probe.structuredContent));
  const start = await call("bridge_start_local_manager");
  assert.equal(start.structuredContent.started, true, "plugin HTTP worker must start");
  const endpoint = start.structuredContent.endpoint;
  const configPath = path.join(process.env.USERPROFILE ?? process.env.HOME, ".codex", "state", "codexworker", "desktop-config.local.json");
  let config = {};
  try { config = JSON.parse(await readFile(configPath, "utf8")); } catch (e) { if (e.code !== "ENOENT") throw e; }
  const tokenFile = config.tokenFile ?? path.join(process.env.USERPROFILE ?? process.env.HOME, ".codex", "secrets", "codex-bridge-codexworker.token");
  const token = (await readFile(tokenFile, "utf8")).trim();
  const http = async (route, body) => {
    const response = await fetch(endpoint + route, { method: body ? "POST" : "GET",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(45000) });
    return { status: response.status, body: await response.json() };
  };
  const health = (await http("/health")).body;
  assert.equal(health.integrationMode, "desktop-native");
  assert.equal(health.ready, true); assert.equal(health.managerReady, false);
  const existing = await http(`/manager/sessions/${threadId}`);
  if (existing.status === 404) {
    const adopted = await http("/manager/sessions/adopt", { protocolVersion: "1.1.1-draft", messageId: randomUUID(), source: "trainer", threadId });
    assert.equal(adopted.status, 200);
  } else assert.equal(existing.status, 200);
  let messageId = randomUUID(); let marker = `HERMES_REAL_PLUGIN_${randomUUID()}`;
  let body = { protocolVersion: "1.1.1-draft", messageId, source: "hermes", targetThreadId: threadId,
    text: `${marker}\n这是CodexWorker插件真实管道验收。请只回复 NATIVE_MCP_HTTP_OK；不要调用工具、不要修改文件。` };
  const recoveryId = process.argv[3];
  if (recoveryId) {
    const state = JSON.parse(await readFile(path.join(path.dirname(configPath), "desktop-manager-state.json"), "utf8"));
    body = state.receipts[recoveryId]?.request;
    if (body?.targetThreadId !== threadId || !body.text.startsWith("HERMES_REAL_PLUGIN_")) throw new Error("Recovery may only reuse this explicit acceptance message.");
    messageId = body.messageId; marker = body.text.split("\n")[0];
  }
  const accepted = await http("/manager/messages", body); assert.equal(accepted.status, 200);
  assert.equal((await http("/manager/messages", body)).body.duplicate, true);
  let events = [];
  for (let attempt = 0; attempt < 120; attempt++) {
    events = (await http(`/manager/events?after=${accepted.body.eventSeq - 1}&stream=0`)).body.events.filter(e => e.messageId === messageId);
    if (events.some(e => e.type === "turn.completed")) break;
    if (attempt > 0 && attempt % 20 === 0) process.stdout.write(`${JSON.stringify({ stage: "waiting_for_native_turn", seconds: attempt, threadId, messageId })}\n`);
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  assert.equal(events.filter(e => e.type === "turn.started").length, 1);
  assert.equal(events.filter(e => e.type === "turn.completed").length, 1);
  assert.ok(events.some(e => e.type === "item.completed" && e.payload.snapshot.includes("NATIVE_MCP_HTTP_OK")));
  process.stdout.write(`${JSON.stringify({ passed: true, actualStdioPlugin: true, actualInjectedPipe: true,
    runtimeVersion: initialized.serverInfo.version, threadId, messageId, marker, endpoint,
    eventTypes: events.map(e => e.type), managerReady: health.managerReady })}\n`);
} finally {
  child.stdin.end();
  if (child.exitCode === null) await once(child, "exit");
  lines.close();
}
