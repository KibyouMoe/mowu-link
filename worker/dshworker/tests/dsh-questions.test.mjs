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

test("a continued answer after plugin reload resumes its original session before native validation", async () => {
  const f = fixture();
  const pending = f.ask(), question = f.question();
  f.controller.abort(new Error("ASK_TIMED_OUT"));
  await assert.rejects(pending);
  f.turn.status = "completed"; f.thread.running = false;
  f.transport.live.clear();
  f.service.continued = () => [{ callId: "call", questions: f.request.questions }];
  let resumes = 0, continuation;
  f.transport.ensureAgent = async thread => {
    resumes++; assert.equal(thread.sessionId, "session-original");
    f.transport.live.set(thread.id, { agent: f.agent });
    return f.agent;
  };
  f.transport.startTurn = async (_thread, turn) => { continuation = turn.nativeReply; };
  await f.transport.execute("send_message_to_thread", { threadId: "thread",
    prompt: nativeQuestionReply(question, ["只回复"]) });
  assert.equal(resumes, 1);
  assert.equal(continuation.sessionId, "session-original");
  assert.equal(continuation.callId, "call");
});

test("settling one of two native clarification requests keeps the other wait visible", async () => {
  const f = fixture(), controller = new AbortController();
  const first = f.ask(), question = f.question();
  const second = f.transport.questions.request({ ...f.request, signal: controller.signal,
    wait: { callId: "second-call", timed: true } }, f.next);
  try {
    f.transport.sendMessage({ threadId: "thread", prompt: nativeQuestionReply(question, ["文件"]) });
    await first;
    assert.equal(f.transport.questions.pending.size, 1);
    assert.equal(f.turn.waitingForInput, true);
  } finally {
    controller.abort(new Error("test finished"));
    await assert.rejects(second);
  }
  assert.equal(f.turn.waitingForInput, false);
});

test("a displayed native question is checkpointed while its answer is still pending", async () => {
  const f = fixture(), writes = [];
  f.transport.save = async () => { writes.push(structuredClone(f.turn)); };
  const pending = f.ask();
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(writes.some(turn => turn.waitingForInput && turn.nativeQuestions?.length === 1));
  } finally {
    f.controller.abort(new Error("test finished"));
    await assert.rejects(pending);
  }
});

test("repeated native requests share the provider, answer and original source identity", async () => {
  const f = fixture(); let providers = 0;
  const ask = request => f.transport.questions.request(request, () => { providers++; return f.next(); });
  const pending = Array.from({ length: 20 }, () => ask(f.request));
  try {
    const item = f.turn.nativeQuestions[0], original = f.question();
    for (let index = 0; index < 20; index++)
      assert.deepEqual(projectPublicQuestion(item, { threadId: f.thread.id, turnId: "wrapper-" + index }), original);
    assert.equal(projectPublicQuestion(item, { threadId: "foreign", turnId: f.turn.id }), null);
    assert.equal(f.turn.nativeQuestions.length, 1);
    f.transport.sendMessage({ threadId: f.thread.id, prompt: nativeQuestionReply(original, ["文件"]) });
    const answers = await Promise.all(pending);
    assert.ok(answers.every(answer => JSON.stringify(answer) === JSON.stringify(answers[0])));
    assert.equal(providers, 1); assert.equal(f.transport.questions.pending.size, 0);
    assert.deepEqual(await ask(f.request), answers[0]);
    assert.equal(providers, 1); assert.equal(f.turn.nativeQuestions.length, 1);
  } finally {
    f.controller.abort(new Error("test finished")); await Promise.allSettled(pending);
  }
});

test("a reused native call cannot change its question, while a distinct call keeps a distinct identity", async () => {
  const f = fixture(), pending = f.ask(), original = f.question();
  try {
    const changed = { ...f.request, questions: [{ ...f.request.questions[0], question: "changed" }] };
    await assert.rejects(f.transport.questions.request(changed, f.next), /question_identity_conflict/);
    const second = f.transport.questions.request({ ...f.request, wait: { callId: "another-call" } }, f.next);
    second.catch(() => {});
    const projected = projectPublicQuestion(f.turn.nativeQuestions[1], { threadId: f.thread.id, turnId: f.turn.id });
    assert.notEqual(projected.questionId, original.questionId);
    f.transport.sendMessage({ threadId: f.thread.id, prompt: nativeQuestionReply(original, ["文件"]) });
    await pending;
    await assert.rejects(f.transport.questions.request(changed, f.next), /question_identity_conflict/);
    f.controller.abort(new Error("test finished")); await assert.rejects(second, /test finished/);
  } finally {
    f.controller.abort(new Error("test finished")); await Promise.allSettled([pending]);
  }
});

test("cancelling a repeated native request rejects every waiter without another public question", async () => {
  const f = fixture();
  const pending = Array.from({ length: 20 }, () => f.ask());
  const settled = Promise.allSettled(pending);
  f.controller.abort(new Error("cancelled by router"));
  assert.ok((await settled).every(result => result.status === "rejected" && result.reason.message === "cancelled by router"));
  assert.equal(f.turn.nativeQuestions.length, 1);
  assert.equal(f.transport.questions.pending.size, 0);
  assert.equal(f.turn.waitingForInput, false);
  await assert.rejects(f.ask(), /cancelled by router/);
  assert.equal(f.turn.nativeQuestions.length, 1);
});
