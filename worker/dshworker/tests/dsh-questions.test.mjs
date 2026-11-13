import test from "node:test";
import assert from "node:assert/strict";
import { InProcessTransport } from "../interface/inprocess.mjs";
import { projectPublicQuestion, nativeQuestionReply, nativeAnswersInSnapshot, SKIPPED_QUESTION_ANSWER } from "../interface/public-questions.mjs";
import { normalizeQuestion } from "../question-relay.mjs";

function fixture() {
  const handlers = new Map(), steers = [];
  const service = { continued: () => [], answer: () => false };
  const ctx = { on(name, fn) { handlers.set(name, fn); return () => handlers.delete(name); }, get(name) { return name === "userQuestions" ? service : null; } };
  const transport = new InProcessTransport({ ctx, selection: {}, cwd: "/tmp", stateDir: "/tmp/dsh-question-nonexistent-fixture", log() {} });
  transport.save = async () => {};
  const agent = { session: { id: "session-original", seq: 0 }, steer: m => steers.push(m) };
  const turn = { id: "turn", prompt: "test", status: "inProgress" };
  const thread = { id: "thread", sessionId: agent.session.id, running: true, queue: [], turns: [turn] };
  transport.live.set(thread.id, { agent }); transport.threads.set(thread.id, thread);
  const controller = new AbortController();
  const request = { agent, signal: controller.signal, wait: { callId: "call", timed: true },
    questions: [{ id: "where", question: "写到哪里？", options: [{ label: "文件" }, { label: "只回复" }] }] };
  const next = () => Promise.reject(Object.assign(Error("no desktop"), { code: "NO_PROVIDER" }));
  return { transport, handlers, steers, service, agent, turn, thread, request, controller, next,
    ask: () => handlers.get("user-questions/request")(request, next),
    question: () => normalizeQuestion(projectPublicQuestion(turn.nativeQuestions[0], { threadId: thread.id, turnId: turn.id })) };
}
for (const choice of ["文件", SKIPPED_QUESTION_ANSWER, "自由回答"]) test(`DSH native question round trip: ${choice}`, async () => {
  const f = fixture(); const answer = f.ask(); const q = f.question();
  assert.match(q.sourceQuestionItemIds[0], /dsh-user-question/);
  const sent = f.transport.sendMessage({ threadId: "thread", prompt: nativeQuestionReply(q, [choice]) });
  assert.equal(sent.turnId, "turn"); assert.equal(f.thread.turns.length, 1); assert.equal(f.steers.length, 0);
  const actual = await answer;
  assert.deepEqual(actual.answers[0], choice === "文件" ? { id: "where", selected: ["文件"] } :
    choice === SKIPPED_QUESTION_ANSWER ? { id: "where", selected: [] } : { id: "where", selected: [], custom: choice });
  assert.deepEqual(nativeAnswersInSnapshot(q, f.transport.readThread({ threadId: "thread" })), [choice]);
  assert.equal(f.turn.waitingForInput, false);
});
test("DSH Desktop answers settle the same public question", async () => {
  const f = fixture();
  const pending = f.handlers.get("user-questions/request")(f.request, async () => ({ answers: [{ id: "where", selected: ["只回复"] }] }));
  const q = f.question(); await pending;
  assert.deepEqual(nativeAnswersInSnapshot(q, f.transport.readThread({ threadId: "thread" })), ["只回复"]);
});
test("DSH leaves approval intents and foreign agents with the native answerer", async () => {
  const f = fixture(); let calls = 0;
  f.request.questions[0].intent = { kind: "approval" };
  assert.equal(await f.handlers.get("user-questions/request")(f.request, () => { calls++; return "native"; }), "native");
  delete f.request.questions[0].intent; f.request.agent = { session: { id: "other" } };
  assert.equal(await f.handlers.get("user-questions/request")(f.request, () => { calls++; return "native"; }), "native");
  assert.equal(calls, 2); assert.equal(f.turn.nativeQuestions, undefined);
});
test("DSH rejects cross-session answers and abort releases the native waiter", async () => {
  const f = fixture(); const pending = f.ask(); const q = f.question();
  const foreign = nativeQuestionReply(q, ["文件"]).replaceAll("session-original", "foreign");
  assert.throws(() => f.transport.sendMessage({ threadId: "thread", prompt: foreign }), /question_owner_mismatch/);
  f.controller.abort(new Error("cancelled by router")); await assert.rejects(pending, /cancelled by router/);
  assert.equal(f.transport.questions.pending.size, 0);
  assert.throws(() => f.transport.sendMessage({ threadId: "thread", prompt: nativeQuestionReply(q, ["文件"]) }), /no_longer_waiting/);
});
test("DSH timed-out clarification continues through the native question service", async () => {
  const f = fixture(); const pending = f.ask(); const q = f.question();
  f.controller.abort(new Error("ASK_TIMED_OUT")); await assert.rejects(pending);
  f.turn.status = "completed"; f.thread.running = false;
  f.service.continued = () => [{ callId: "call", questions: f.request.questions }];
  let nextTurn;
  f.transport.startTurn = async (thread, turn) => { nextTurn = turn; };
  const sent = f.transport.sendMessage({ threadId: "thread", prompt: nativeQuestionReply(q, ["只回复"]) });
  assert.notEqual(sent.turnId, "turn"); assert.equal(nextTurn.nativeReply.callId, "call");
  let accepted;
  f.service.answer = (agent, call, answer) => { accepted = { agent, call, answer }; return true; };
  f.transport.questions.continue(f.agent, nextTurn.nativeReply);
  assert.equal(accepted.agent, f.agent); assert.equal(accepted.call, "call");
  assert.deepEqual(accepted.answer.answers, [{ id: "where", selected: ["只回复"] }]);
});
test("busy messages enter native steer and stay in the original turn transcript", () => {
  const f = fixture();
  const sent = f.transport.sendMessage({ threadId: "thread", prompt: "new instruction" });
  assert.equal(sent.turnId, "turn"); assert.equal(f.thread.turns.length, 1); assert.equal(f.steers.length, 1);
  assert.ok(f.transport.readThread({ threadId: "thread" }).turns[0].items.some(i => i.text === "new instruction"));
});
