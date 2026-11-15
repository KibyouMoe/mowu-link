import test from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import { DshPublicEvents } from "../interface/public-events.mjs";
import { InProcessTransport, loadIdentity } from "../interface/inprocess.mjs";

function fixture() {
  const activities = [], context = [], session = { id: "native-original" }, agent = { session };
  const transport = { agentsForTurn: () => [agent], log() {} };
  const events = new DshPublicEvents(transport);
  events.attach({ observeToolActivity: event => activities.push(event), observeContextEvent: event => context.push(event) });
  const thread = { id: "thread" }, turn = { id: "turn", status: "inProgress" };
  return { events, activities, context, session, agent, thread, turn,
    send: (type, data, subject = session) => events.event(thread, turn, agent, subject, { type, data, seq: 1 }) };
}
test("DSH streams public text under a stable item ID and settles without duplicating it", () => {
  const f = fixture();
  f.events.stream(f.thread, f.turn, { type: "start", attemptId: "attempt" });
  for (const text of ["第一", "句话"]) f.events.stream(f.thread, f.turn,
    { type: "chunk", attemptId: "attempt", chunk: { type: "text-delta", text } });
  assert.equal(f.turn.publicMessages.length, 1); const id = f.turn.publicMessages[0].id;
  assert.equal(f.turn.publicMessages[0].text, "第一句话");
  f.send("assistant/message", { message: { id: "native-message", content: [{ type: "text", text: "第一句话。" }] } });
  assert.deepEqual(f.turn.publicMessages, [{ id, text: "第一句话。" }]);
  f.events.stream(f.thread, f.turn, { type: "chunk", chunk: { type: "reasoning-delta", text: "private" } });
  assert.ok(!JSON.stringify(f.turn.publicMessages).includes("private"));
});
test("DSH tool start/result use one ID, hide raw output and redact preview secrets", () => {
  const f = fixture();
  f.send("tool/call", { callId: "tool", name: "bash", arguments: JSON.stringify({ command: "curl --token private-value" }) });
  f.send("tool/result", { message: { toolCallId: "tool", isError: true, content: [{ text: "secret output" }] } });
  assert.deepEqual(f.activities.map(x => x.status), ["running", "failed"]);
  assert.equal(f.activities[0].itemId, f.activities[1].itemId);
  assert.equal(f.activities[0].category, "terminal");
  assert.ok(!JSON.stringify(f.activities).includes("private-value"));
  assert.ok(!JSON.stringify(f.activities).includes("secret output"));
});
test("unrelated DSH sessions cannot publish through this Worker", () => {
  const f = fixture();
  f.send("tool/call", { callId: "foreign", name: "bash", arguments: "{}" }, { id: "other" });
  f.send("assistant/message", { message: { content: [{ type: "text", text: "other" }] } }, { id: "other" });
  assert.deepEqual(f.activities, []); assert.equal(f.turn.publicMessages, undefined);
});
test("DSH compaction emits lifecycle only, never its summary", () => {
  const f = fixture();
  f.send("compaction/start", { compactionId: "compact", summary: "private" });
  f.send("compaction/end", { compactionId: "compact", summary: "private" });
  assert.deepEqual(f.context.map(x => x.status), ["in_progress", "completed"]);
  assert.ok(!JSON.stringify(f.context).includes("private"));
});
test("failed native resume preserves session identity and never creates a replacement", async () => {
  let created = 0;
  const c = Object.create(InProcessTransport.prototype);
  c.live = new Map(); c.selection = {}; c.log = () => {}; c.presetSetup = async () => null;
  c.ctx = { get: () => ({ resume: async () => { throw Error("not found"); }, create: async () => { created++; } }) };
  const thread = { id: "thread", sessionId: "months-of-history" };
  await assert.rejects(c.ensureAgent(thread), e => e.code === "dsh_session_resume_failed");
  assert.equal(thread.sessionId, "months-of-history"); assert.equal(created, 0);
});
test("a new native session identity is saved before it can accept any prompt", async () => {
  const c = Object.create(InProcessTransport.prototype), writes = [];
  c.live = new Map(); c.selection = {}; c.log = () => {}; c.presetSetup = async () => null;
  const thread = { id: "thread", cwd: "/work" }, agent = { session: { id: "native-new", seq: 0 } };
  c.ctx = { get: () => ({ create: async () => ({ agent }) }) };
  c.save = async () => { writes.push(thread.sessionId); };
  assert.equal(await c.ensureAgent(thread), agent);
  assert.deepEqual(writes, ["native-new"]);
});
test("native compaction retains the same agent and exports no summary", async () => {
  const c = Object.create(InProcessTransport.prototype);
  const thread = { id: "thread", sessionId: "original", running: false }, agent = { session: { id: "original" } };
  let called = 0, stopped = 0;
  c.threads = new Map([[thread.id, thread]]); c.ensureAgent = async () => agent; c.save = async () => {};
  c.ctx = { on: () => () => stopped++, get: name => name === "compaction" ? {
    async compactNow(subject, signal) { assert.equal(subject, agent); assert.equal(signal.aborted, false); called++; return { summary: "private" }; },
  } : null };
  c.publicEvents = { event() {} };
  assert.ok((await c.listTools()).some(t => t.name === "compact_thread"));
  assert.deepEqual(await c.execute("compact_thread", { threadId: "thread" }), { threadId: "thread", sessionId: "original", compacted: true });
  assert.equal(called, 1); assert.equal(stopped, 1); assert.equal(thread.compacting, false);
  thread.running = true;
  await assert.rejects(c.compactThread({ threadId: "thread" }), /requires_idle/); assert.equal(called, 1);
});
test("a failed first checkpoint cannot admit input through the live-agent cache", async () => {
  const c = Object.create(InProcessTransport.prototype);
  c.live = new Map(); c.selection = {}; c.log = () => {}; c.presetSetup = async () => null;
  const thread = { id: "thread", cwd: "/work" }; let disposed = false;
  c.ctx = { get: name => name === "agents" ? { create: async () => ({ agent: { session: { id: "native", seq: 0 } }, dispose: async () => { disposed = true; } }) } : null };
  c.save = async () => { throw Error("disk full"); };
  await assert.rejects(c.ensureAgent(thread), /disk full/);
  assert.equal(c.live.size, 0); assert.equal(disposed, true); assert.equal(thread.sessionId, "native");
});
test("Desktop compaction resolves through the preset's native command registry", async () => {
  const c = Object.create(InProcessTransport.prototype);
  const thread = { id: "thread", sessionId: "original" }, agent = { session: { id: "original" }, ctx: { get: () => null } };
  const calls = [];
  c.live = new Map([[thread.id, { agent }]]); c.threads = new Map([[thread.id, thread]]);
  c.ensureAgent = async () => agent; c.save = async () => {}; c.publicEvents = { event() {} };
  const commands = { find: (a, name) => a === agent && name === "compact" ? {} : null,
    execute: async (...args) => { calls.push(args); return { commandId: "native-command", result: { kind: "success", sourceEventSeq: 42 } }; } };
  c.ctx = { get: name => name === "commands" ? commands : null, on: () => () => {} };
  assert.ok((await c.listTools()).some(t => t.name === "compact_thread"));
  assert.equal((await c.compactThread({ threadId: "thread" })).compacted, true);
  assert.equal(calls[0][0], agent); assert.equal(calls[0][1], "/compact"); assert.deepEqual(calls[0][2], []);
  commands.execute = async () => ({ result: { kind: "success", text: "No compactable history yet." } });
  await assert.rejects(c.compactThread({ threadId: "thread" }), /没有可压缩历史/);
  assert.equal(thread.compacting, false);
});
test("a DSH plugin opened from Codex keeps its own identity and state directory", t => {
  const names = ["CODEXWORKER_IDENTITY", "CODEXWORKER_STATE_DIR", "DSHWORKER_IDENTITY"];
  const before = names.map(name => process.env[name]);
  t.after(() => names.forEach((name, i) => { if (before[i] == null) delete process.env[name]; else process.env[name] = before[i]; }));
  process.env.CODEXWORKER_IDENTITY = "/nonexistent/foreign-identity.json";
  process.env.CODEXWORKER_STATE_DIR = "/tmp/foreign-codex-state";
  delete process.env.DSHWORKER_IDENTITY;
  const identity = loadIdentity({}, () => {});
  assert.equal(identity.name, "TamamoCross");
  assert.equal(identity.stateDir, path.join(os.homedir(), ".hermes", "state", "dshworker"));
});
