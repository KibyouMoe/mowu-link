// Run only with an explicitly authorized Desktop test task. Native calls are
// relayed by the calling Codex turn, not by a separate Codex runtime.
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import { randomUUID } from "node:crypto";
import { DesktopManager, PROTOCOL } from "../desktop-manager.mjs";

const [threadId, workspace, relayFile, nativeSenderThreadId] = process.argv.slice(2);
if (!threadId || !workspace || !nativeSenderThreadId) throw new Error("Usage: live-desktop-acceptance.mjs <authorized-test-threadId> <workspace> <relayFile> <actual-native-sender-threadId>");
const root = await mkdtemp(path.join(os.tmpdir(), "rudolf-live-acceptance-"));
const pending = new Map(); let sequence = 0;
if (process.stdin.isTTY) process.stdin.setRawMode(true);
const lines = readline.createInterface({ input: process.stdin });
lines.on("line", line => {
  const result = JSON.parse(line); const handler = pending.get(result.id); pending.delete(result.id);
  if (!handler) return;
  clearTimeout(handler.timer);
  if (result.error) handler.reject(new Error(result.error)); else handler.resolve(result.result);
});
const native = (name, args) => new Promise((resolve, reject) => {
  const id = ++sequence;
  const timer = setTimeout(() => { pending.delete(id); reject(new Error("Desktop acceptance relay timed out")); }, 60000);
  pending.set(id, { resolve, reject, timer });
  const request = JSON.stringify({ kind: "native", id, name, args });
  if (relayFile) void writeFile(relayFile, request, "utf8").then(() => process.stdout.write(`NATIVE ${id} ${name}\n`), reject);
  else process.stdout.write(`${request}\n`);
});
let manager;
try {
  manager = await new DesktopManager({ native,
    listNativeTools: async () => ["read_thread", "send_message_to_thread", "create_thread", "fork_thread", "set_thread_title", "list_threads"].map(name => ({ name })),
    stateDir: root, config: { token: randomUUID(), workspaceRoots: [workspace], port: 0, pollMs: 1000000, nativeSenderThreadId } }).initialize();
  const url = await manager.listen();
  await manager.register({ protocolVersion: PROTOCOL, messageId: randomUUID(), source: "trainer", threadId });
  const messageId = randomUUID(); const marker = `HERMES_DESKTOP_ADAPTER_${randomUUID()}`;
  const body = { protocolVersion: PROTOCOL, messageId, source: "hermes", targetThreadId: threadId,
    text: `${marker}\n这是鲁道夫 Bridge 的 HTTP→Codex Desktop 原生会话验收。请只回复 DESKTOP_ADAPTER_OK；不要调用工具、不要修改文件。` };
  const send = async () => {
    const response = await fetch(`${url}/manager/messages`, { method: "POST", headers: { authorization: `Bearer ${manager.config.token}`, "content-type": "application/json" }, body: JSON.stringify(body) });
    return { status: response.status, body: await response.json() };
  };
  const accepted = await send(); assert.equal(accepted.status, 200);
  const duplicate = await send(); assert.equal(duplicate.body.duplicate, true);
  for (let attempt = 0; attempt < 30; attempt++) {
    await manager.scan();
    if (manager.state.events.some(e => e.type === "turn.completed" && e.messageId === messageId)) break;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  const events = manager.state.events.filter(e => e.messageId === messageId);
  assert.equal(events.filter(e => e.type === "turn.started").length, 1);
  assert.equal(events.filter(e => e.type === "turn.completed").length, 1);
  assert.ok(events.some(e => e.type === "item.completed" && e.payload.snapshot.includes("DESKTOP_ADAPTER_OK")));
  const result = { kind: "result", passed: true, threadId, messageId, marker, eventTypes: events.map(e => e.type), managerReady: manager.health().managerReady };
  if (relayFile) { await writeFile(relayFile, JSON.stringify(result), "utf8"); process.stdout.write("RESULT passed\n"); }
  else process.stdout.write(`${JSON.stringify(result)}\n`);
} finally {
  await manager?.close(); lines.close(); process.stdin.destroy();
  assert.ok(root.startsWith(os.tmpdir())); await rm(root, { recursive: true, force: true });
}
