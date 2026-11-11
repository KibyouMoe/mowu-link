import assert from "node:assert/strict";
import test from "node:test";
import { ProgressWatch, DEFAULT_NO_PROGRESS_TIMEOUT_MS, noProgressTimeoutMs, taskDeadline } from "../progress-watch.mjs";
import { ROUTER_TURN_LIMIT_MS } from "../router-tasks.mjs";
import { DesktopManager } from "../desktop-manager.mjs";

test("no implicit task deadline, including missing/invalid/zero deadlines", () => {
  for (const deadline_s of [undefined, null, 0, -1, "oops"])
    assert.equal(taskDeadline({ deadline_s }, 0), Infinity);
  assert.equal(ROUTER_TURN_LIMIT_MS, Infinity);
  assert.equal(taskDeadline({ deadline_s: 4 }, 100), 4100);
});
test("default no-progress window is 30 minutes and can be configured", () => {
  assert.equal(DEFAULT_NO_PROGRESS_TIMEOUT_MS, 1800000);
  assert.equal(noProgressTimeoutMs({ noProgressTimeoutMs: 123 }), 123);
});
test("continued native events keep a task alive for days beyond old wall-clock limits", () => {
  let now = 0;
  const watch = new ProgressWatch({ now: () => now });
  for (let seq = 0; seq < 3000; seq++) {
    watch.observe({ id: "turn", eventSeq: seq });
    now += 60000;
    assert.equal(watch.failure(), null);
  }
  assert.ok(now > 24 * 60 * 60000);
});
test("identical polls with new observation ages do not reset no-progress time", () => {
  let now = 0;
  const watch = new ProgressWatch({ timeoutMs: 120, now: () => now });
  watch.observe({ id: "turn", status: "inProgress", eventSeq: 1, updatedAt: "old", durationMs: 0 });
  now = 121;
  watch.observe({ id: "turn", status: "inProgress", eventSeq: 1, updatedAt: "new", durationMs: 121 });
  assert.equal(watch.failure().reason, "stuck");
  assert.match(watch.failure().message, /无进展超时/);
});
test("tool-only and stream-only changes reset liveness without an assistant reply", () => {
  let now = 0;
  const watch = new ProgressWatch({ timeoutMs: 100, now: () => now });
  watch.observe({ id: "turn", eventSeq: 1 });
  now = 90; watch.observe({ id: "turn", eventSeq: 2 });
  now = 180; assert.equal(watch.failure(), null);
  now = 191; assert.equal(watch.failure().reason, "stuck");
});
test("explicit deadlines remain absolute even with continued progress", () => {
  let now = 0;
  const watch = new ProgressWatch({ deadline: 100, timeoutMs: 500, now: () => now });
  watch.observe({ eventSeq: 1 });
  now = 101; watch.observe({ eventSeq: 2 });
  assert.equal(watch.failure().reason, "deadline");
});
function manager() {
  return new DesktopManager({ native: async () => {}, listNativeTools: async () => [],
    stateDir: "/tmp/unused-progress-test", config: { noProgressTimeoutMs: 500 } });
}
test("manager HTTP wait survives the old 600s limit when real events continue", async t => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"] });
  const m = manager();
  m.config.turnTimeoutMs = 1; // Old setting must not truncate a task.
  const waiting = m.waitResult("message", "thread");
  for (let seq = 1; seq <= 6010; seq++) {
    m.observeTaskProgress("thread", { id: "turn", eventSeq: seq });
    t.mock.timers.tick(100);
    await Promise.resolve();
  }
  m.state.events.push({ seq: 6011, threadId: "thread", messageId: "message",
    type: "turn.completed", turnId: "turn", payload: { status: "completed", messageIds: ["message"] } });
  t.mock.timers.tick(100);
  assert.equal((await waiting).status, "completed");
});
test("manager HTTP wait reports stuck only after no new progress", async t => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"] });
  const m = manager(), waiting = m.waitResult("message", "thread");
  for (let n = 0; n < 8; n++) { t.mock.timers.tick(100); await Promise.resolve(); }
  const result = await waiting;
  assert.equal(result.status, "stuck"); assert.match(result.reason, /无进展超时/);
});
test("manager explicit deadline expires even while task events continue", async t => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"] });
  const m = manager(), waiting = m.waitResult("message", "thread", { deadline_s: 0.1 });
  t.mock.timers.tick(100);
  assert.equal((await waiting).status, "timeout");
});

test("app-server reasoning/tool notifications are observable progress; status polls are not", async () => {
  const { AppServerClient } = await import("../interface/transport/app-server.mjs");
  const client = new AppServerClient();
  client.ensureStarted = async () => {};
  const turn = { id: "turn", status: "inProgress", items: [{ id: "input", type: "userMessage", content: [{ type: "text", text: "hi" }] }] };
  client.request = async () => ({ thread: { id: "thread", turns: [turn] } });
  client.handleNotification("item/reasoning/textDelta", { threadId: "thread", turnId: "turn", delta: "thinking" });
  client.handleNotification("item/commandExecution/outputDelta", { threadId: "thread", turnId: "turn", delta: "tool output" });
  const read = () => client.readThread({ threadId: "thread", includeOutputs: false });
  assert.equal((await read()).turns[0].eventSeq, 2);
  client.handleNotification("thread/status/changed", { threadId: "thread", status: "active" });
  client.handleNotification("item/reasoning/textDelta", { threadId: "thread", turnId: "turn", delta: "" });
  assert.equal((await read()).turns[0].eventSeq, 2);
  assert.equal((await read()).turns[0].eventSeq, 2);
});
