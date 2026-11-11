import assert from "node:assert/strict";
import test from "node:test";
import { InProcessTransport } from "../interface/inprocess.mjs";

async function fixture(t, opts = {}) {
  t.mock.timers.enable({ apis: ["Date", "setInterval"] });
  const handlers = new Map(), records = [];
  const ctx = { on(name, fn) { handlers.set(name, fn); return () => handlers.delete(name); }, get() { return null; } };
  const transport = new InProcessTransport({ ctx, selection: {}, cwd: "/tmp",
    stateDir: "/tmp/nonexistent-dsh-progress-test", log: () => {}, ...opts });
  transport.save = async () => {};
  const turn = { id: "turn", prompt: "[router t-test] hello", status: "inProgress", ...opts.turn };
  if (opts.deadlineMs) turn.deadlineAt = Date.now() + opts.deadlineMs;
  const thread = { id: "thread", turns: [turn], queue: [] };
  let idle;
  let cancels = 0;
  const agent = { session: { seq: 0, eventAt: n => records[n] }, status: "busy",
    followup() { records.push({ type: "turn/start" }); this.session.seq++; },
    whenIdle: () => new Promise(resolve => { idle = resolve; }),
    cancel() { cancels++; } };
  transport.ensureAgent = async () => agent;
  transport.threads.set("thread", thread);
  const running = transport.startTurn(thread, turn);
  await Promise.resolve(); await Promise.resolve();
  return { transport, ctx, agent, handlers, turn, running, cancels: () => cancels,
    complete() { records.push({ type: "turn/end", data: { reason: { kind: "completed" } } });
      agent.session.seq++; agent.status = "idle"; idle(); } };
}
test("in-process default survives 600s and 1200s with no implicit wall-clock kill", async t => {
  const f = await fixture(t, { turnTimeoutMs: 1 });
  t.mock.timers.tick(1200001);
  assert.equal(f.cancels(), 0); assert.equal(f.turn.status, "inProgress");
  f.complete(); await f.running; assert.equal(f.turn.status, "completed");
});
test("in-process tool events keep a turn alive for more than 30 minutes", async t => {
  const f = await fixture(t);
  for (let n = 0; n < 8; n++) { f.agent.session.seq++; t.mock.timers.tick(600000); }
  assert.equal(f.cancels(), 0);
  assert.ok(f.transport.readThread({ threadId: "thread" }).turns[0].eventSeq > 0);
  f.complete(); await f.running; assert.equal(f.turn.status, "completed");
});
test("in-process stream deltas are progress even before any durable session event", async t => {
  const f = await fixture(t);
  for (let n = 0; n < 8; n++) {
    f.handlers.get("agent/assistant-stream")({ agent: f.agent,
      frame: { type: "chunk", chunk: { type: "reasoning-delta", text: "x" } } });
    t.mock.timers.tick(600000);
  }
  assert.equal(f.cancels(), 0);
  f.complete(); await f.running; assert.equal(f.turn.thinking, "xxxxxxxx");
});
test("in-process no-progress watchdog cancels and resolves even when whenIdle never returns", async t => {
  const f = await fixture(t, { noProgressTimeoutMs: 1000 });
  t.mock.timers.tick(1000); await Promise.resolve();
  t.mock.timers.tick(1000); await f.running;
  assert.equal(f.cancels(), 1); assert.equal(f.turn.reasonKind, "stuck");
  assert.match(f.turn.error, /无进展超时/);
});
test("in-process explicit task deadline wins over live events", async t => {
  const f = await fixture(t, { deadlineMs: 100 });
  t.mock.timers.tick(100); await f.running;
  assert.equal(f.cancels(), 1); assert.equal(f.turn.reasonKind, "deadline");
});

test("descendant tool events and streams keep the parent task alive; unrelated agents cannot", async t => {
  const f = await fixture(t);
  f.agent.id = "root";
  const child = { id: "child", session: { id: "child-session", seq: 0 } };
  const unrelated = { id: "foreign", session: { id: "foreign-session", seq: 0 } };
  const registry = { list: () => [f.agent, child, unrelated],
    isOwnedBy: (id, parent) => id === "child" && parent === f.agent };
  f.ctx.get = name => name === "agents" ? registry : null;
  assert.deepEqual(f.transport.agentsForTurn(f.agent), [f.agent, child]);
  for (let n = 0; n < 8; n++) { child.session.seq++; t.mock.timers.tick(600000); }
  assert.equal(f.cancels(), 0);
  f.handlers.get("agent/assistant-stream")({ agent: child,
    frame: { type: "chunk", chunk: { type: "reasoning-delta", text: "child output" } } });
  t.mock.timers.tick(600000);
  assert.equal(f.cancels(), 0); assert.equal(f.turn.thinking, undefined);
  f.complete(); await f.running; assert.equal(f.turn.status, "completed");
});
