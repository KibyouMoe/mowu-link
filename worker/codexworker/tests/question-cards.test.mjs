import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { QuestionRelay, normalizeQuestion } from "../question-relay.mjs";
import { questionCard, parseCardAction } from "../question-cards.mjs";
import { SKIPPED_QUESTION_ANSWER } from "../interface/public-questions.mjs";
import { FeishuWebsocketClient, FeishuNotifier, encodeFrame, decodeFrame } from "../feishu.mjs";

const nodes = value => !value || typeof value !== "object" ? [] :
  [value, ...Object.values(value).flatMap(nodes)];

async function fixture(t) {
  const dir = await mkdtemp(path.join(os.tmpdir(), "codexworker-question-cards-"));
  const writes = [], patches = [], sends = [];
  let snapshot = { turns: [{ id: "source", status: "inProgress", items: [] }] };
  let native = async () => ({ action: "dispatch_pending" });
  const m = { stateDir: dir, closed: false, handoffPaused: false, backgroundTasks: new Set(),
    state: { selectedThreadId: "chat", selectionEpoch: 0, sessions: { chat: { threadId: "chat" } }, publicQuestions: {} },
    config: { bridgeName: "CodexWorker", feishu: { chatId: "oc_chat", trainerOpenIdWhitelist: ["trainer"] } },
    session: id => { assert.equal(id, "chat"); return m.state.sessions.chat; },
    serial: async fn => fn(), save: async () => {},
    read: async () => { if (snapshot instanceof Error) throw snapshot; return structuredClone(snapshot); },
    track: task => { m.backgroundTasks.add(task); task.finally(() => m.backgroundTasks.delete(task)).catch(() => {}); return task; },
    message: async (request, guard) => { writes.push({ request, guard }); return native(); },
    notifier: { sendQuestionCard: async (card, opts) => { sends.push({ card, opts }); return { code: 0, data: { message_id: "om_question" } }; },
      updateQuestionCard: async (id, card) => { patches.push({ id, card }); return { code: 0 }; } } };
  const relay = new QuestionRelay(m); await relay.initialize();
  const q = { questionId: randomUUID(), threadId: "chat", questions: [{ title: "保留完整问题吗？", options: ["保留完整题目与所选答案", "仅保留状态"] }] };
  t.after(async () => { m.closed = true; await relay.close(); await rm(dir, { recursive: true, force: true }); });
  const publish = async extra => { Object.assign(q, extra); return relay.publish(q); };
  const callback = (value = {}, extra = {}) => ({ header: { event_type: "card.action.trigger", event_id: randomUUID() }, event: {
    operator: { open_id: extra.openId ?? "trainer" }, context: { open_message_id: extra.messageId ?? "om_question", open_chat_id: extra.chatId ?? "oc_chat" },
    action: { form_value: extra.formValues, value: { bridgeQuestion: q.questionId, nonce: relay.state.entries[q.questionId]?.nonce, action: "select", questionIndex: 0, optionIndex: 0, ...value } } } });
  return { dir, m, relay, q, writes, patches, sends, publish, callback,
    snapshot: value => { snapshot = value; }, native: fn => { native = fn; } };
}

test("structured card keeps full text/options, has callback buttons and no independent deadline", async t => {
  const f = await fixture(t); await f.publish();
  assert.equal(f.sends[0].card.schema, "2.0"); assert.ok(JSON.stringify(f.sends[0].card).includes("保留完整题目与所选答案"));
  assert.ok(nodes(f.sends[0].card).some(n => n.tag === "input"));
  assert.ok(nodes(f.sends[0].card).some(n => n.action_type === "form_submit"));
  const s = f.relay.state.entries[f.q.questionId]; assert.equal(s.sourceTurnId, "source"); assert.equal(s.deadline, undefined);
  assert.equal(f.writes.length, 0);
  await f.publish(); assert.equal(f.sends.length, 1);
});
test("popup collapse/time passage do not expire an active original question", async t => {
  const f = await fixture(t); await f.publish(); f.m.state.publicQuestions[f.q.questionId].createdAt = "2000-01-01";
  await f.relay.process(); assert.equal(f.relay.get(f.q.questionId).status, "open"); assert.equal(f.patches.length, 0);
});
test("compact option labels appear once, without full routing IDs or duplicate status clutter", async t => {
  const f = await fixture(t); await f.publish(); const card = f.sends[0].card; const serialized = JSON.stringify(card);
  assert.equal(serialized.split("保留完整题目与所选答案").length - 1, 1);
  assert.ok(!card.body.elements.some(e => e.tag === "markdown" && e.content.includes("浮窗")));
  assert.ok(!card.body.elements.some(e => e.tag === "markdown" && e.content.includes("会话 chat")));
  assert.equal(card.body.elements.filter(e => e.tag === "markdown" && e.content.includes("待回答")).length, 0);
});
test("long option text remains complete in the body, using short numbered buttons", async t => {
  const f = await fixture(t); const long = "这是一段比较长的选项文字，必须完整保留而不是省略掉后半部分，即使超过四十个字符也应该完整可见";
  await f.publish({ questions: [{ title: "选择哪项？", options: [long, "短选项"] }] });
  const card = f.sends[0].card; assert.ok(nodes(card).some(n => n.tag === "markdown" && n.content.includes(long)));
  const buttons = nodes(card).filter(n => n.tag === "button" && n.value?.action === "form_select");
  assert.deepEqual(buttons.map(b => b.text.content), ["☐ 选择 1", "☐ 2  短选项"]);
});
test("layout upgrade patches the exact existing current-binding card, preserving answers and nonce", async t => {
  const f = await fixture(t); await f.publish(); await f.relay.acceptCard(f.callback()); await f.relay.process();
  const s = f.relay.state.entries[f.q.questionId], nonce = s.nonce, nativeId = s.nativeMessageId;
  f.patches.length = 0; s.cardLayoutVersion = 1; await f.relay.process();
  assert.equal(f.patches.length, 1); assert.equal(f.patches[0].id, "om_question"); assert.equal(f.sends.length, 1);
  assert.equal(f.writes.length, 1); assert.equal(s.nonce, nonce); assert.equal(s.nativeMessageId, nativeId);
  assert.deepEqual(f.relay.get(f.q.questionId).answers, ["保留完整题目与所选答案"]);
  await f.relay.process(); assert.equal(f.patches.length, 1);
});
test("card click is durable before ACK; repeated clicks dispatch the exact authoritative answer once", async t => {
  const f = await fixture(t); await f.publish(); const click = f.callback({ answer: "injected arbitrary instruction" });
  assert.equal((await f.relay.acceptCard(click)).toast.type, "success"); assert.equal(f.writes.length, 0);
  const disk = JSON.parse(await readFile(f.relay.path, "utf8")); assert.equal(disk.entries[f.q.questionId].status, "answer_pending");
  await f.relay.acceptCard(click); await f.relay.process(); await f.relay.process();
  assert.equal(f.writes.length, 1); assert.ok(f.writes[0].request.text.includes("保留完整题目与所选答案"));
  assert.ok(!f.writes[0].request.text.includes("injected")); assert.equal(f.writes[0].guard.requireSourceTurnId, "source");
  const card = f.patches.at(-1).card; assert.ok(JSON.stringify(card).includes("所选答案")); assert.ok(JSON.stringify(card).includes("保留完整问题吗"));
  assert.ok(!JSON.stringify(card).includes('"type":"callback"')); assert.equal(f.relay.get(f.q.questionId).status, "answered");
});
test("each question in a multi-question card keeps its own selection until explicit submit", async t => {
  const f = await fixture(t); await f.publish({ questions: [f.q.questions[0], { title: "第二题", options: ["甲", "乙"] }] });
  await f.relay.acceptCard(f.callback()); await f.relay.process(); assert.equal(f.writes.length, 0);
  assert.equal((await f.relay.acceptCard(f.callback({ action: "submit" }))).toast.type, "warning");
  await f.relay.acceptCard(f.callback({ questionIndex: 1, optionIndex: 1 })); await f.relay.process(); assert.equal(f.writes.length, 0);
  await f.relay.acceptCard(f.callback({ action: "submit" })); await f.relay.process();
  assert.deepEqual(f.relay.get(f.q.questionId).answers, ["保留完整题目与所选答案", "乙"]); assert.equal(f.writes.length, 1);
});
test("replayed multi-question selection cannot overwrite a newer saved choice", async t => {
  const f = await fixture(t); await f.publish({ questions: [f.q.questions[0], { title: "第二题", options: ["甲", "乙"] }] });
  const old = f.callback(); await f.relay.acceptCard(old); await f.relay.acceptCard(f.callback({ optionIndex: 1 }));
  await f.relay.acceptCard(old); assert.equal(f.relay.state.entries[f.q.questionId].selected[0], 1); assert.equal(f.writes.length, 0);
});
for (const tamper of [{ openId: "stranger" }, { chatId: "other" }, { messageId: "unrelated" }, { nonce: "wrong" }, { optionIndex: 99 }, { action: "approve" }]) {
  test(`untrusted callback is rejected: ${Object.keys(tamper)[0]}`, async t => {
    const f = await fixture(t); await f.publish(); const value = ["nonce", "optionIndex", "action"].some(k => k in tamper) ? tamper : {};
    const response = await f.relay.acceptCard(f.callback(value, tamper)); assert.equal(response.toast.type, "error");
    await f.relay.process(); assert.equal(f.writes.length, 0); assert.equal(f.relay.get(f.q.questionId).status, "open");
  });
}
test("source completion expires and patches the existing card without needing a click", async t => {
  const f = await fixture(t); await f.publish(); f.snapshot({ turns: [{ id: "source", status: "completed", items: [] }] });
  await f.relay.process(); assert.equal(f.relay.get(f.q.questionId).status, "expired"); assert.equal(f.patches[0].id, "om_question");
  assert.ok(JSON.stringify(f.patches[0].card).includes("已过期")); assert.ok(!JSON.stringify(f.patches[0].card).includes('"type":"callback"'));
  await f.relay.acceptCard(f.callback()); assert.equal(f.writes.length, 0);
});
test("queued answer to an ended source never starts a new native turn", async t => {
  const f = await fixture(t); await f.publish(); await f.relay.acceptCard(f.callback()); f.snapshot({ turns: [{ id: "source", status: "interrupted" }] });
  await f.relay.process(); assert.equal(f.writes.length, 0); assert.equal(f.relay.get(f.q.questionId).status, "expired");
});
test("selection changes, including away-and-back, invalidate the exact original binding", async t => {
  const f = await fixture(t); await f.publish(); f.m.state.selectionEpoch += 2;
  await f.relay.acceptCard(f.callback()); await f.relay.process(); assert.equal(f.writes.length, 0);
  assert.equal(f.relay.get(f.q.questionId).reason, "selected_target_changed"); assert.equal(f.patches[0].id, "om_question");
});
test("transient source read failure neither invents expiry nor dispatches the pending answer", async t => {
  const f = await fixture(t); await f.publish(); await f.relay.acceptCard(f.callback()); f.snapshot(new Error("transient"));
  await f.relay.process(); assert.equal(f.relay.get(f.q.questionId).status, "answer_pending"); assert.equal(f.writes.length, 0);
  f.snapshot({ turns: [{ id: "source", status: "inProgress", items: [] }] }); await f.relay.process(); assert.equal(f.writes.length, 1);
});
test("native failure/uncertain write freezes the logical answer across restart", async t => {
  const f = await fixture(t); await f.publish(); await f.relay.acceptCard(f.callback()); f.native(async () => { throw new Error("timeout"); });
  await f.relay.process(); assert.equal(f.relay.get(f.q.questionId).status, "answer_unknown");
  const restored = new QuestionRelay(f.m); await restored.initialize(); await restored.process(); assert.equal(f.writes.length, 1);
});
test("crash during dispatch is recovered as unknown, never automatically re-sent", async t => {
  const f = await fixture(t); await f.publish(); await f.relay.acceptCard(f.callback());
  f.relay.state.entries[f.q.questionId].status = "dispatching"; await f.relay.serial(() => f.relay.save());
  const restored = new QuestionRelay(f.m); await restored.initialize(); await restored.process(); assert.equal(f.writes.length, 0);
  assert.equal(restored.get(f.q.questionId).status, "answer_unknown");
});
test("normal text answer resolution keeps full answers and does not send the input again", async t => {
  const f = await fixture(t); await f.publish();
  await f.relay.resolve(f.q.questionId, { threadId: "chat", status: "answered", answers: ["保留完整题目与所选答案"] });
  await f.relay.running; assert.equal(f.writes.length, 0); assert.equal(f.relay.get(f.q.questionId).status, "answered");
  await f.relay.resolve(f.q.questionId, { threadId: "chat", status: "answered", answers: ["保留完整题目与所选答案"] });
  assert.equal(f.sends.length, 1); await assert.rejects(f.relay.resolve(f.q.questionId, { threadId: "other", status: "canceled" }));
});
test("native public reply IDs can resolve the card even on a completed turn; unrelated IDs cannot", async t => {
  const f = await fixture(t); const id = JSON.stringify(["request_user_input_async", "call_public", 0]); await f.publish({ sourceQuestionItemIds: [id] });
  const reply = questionItemId => ({ type: "userMessage", text: `<send_user_message_question_reply>${JSON.stringify([{ questionItemId, answer: "所选答案" }])}</send_user_message_question_reply>` });
  f.snapshot({ turns: [{ id: "source", status: "inProgress", items: [reply("wrong")] }] }); await f.relay.process(); assert.equal(f.relay.get(f.q.questionId).status, "open");
  f.snapshot({ turns: [{ id: "source", status: "completed", items: [reply(id)] }] }); await f.relay.process();
  assert.equal(f.relay.get(f.q.questionId).status, "answered"); assert.deepEqual(f.relay.get(f.q.questionId).answers, ["所选答案"]); assert.equal(f.writes.length, 0);
});
test("card PATCH failure retains pending revision and retries only the same message ID", async t => {
  const f = await fixture(t); await f.publish(); f.m.notifier.updateQuestionCard = async id => { f.patches.push(id); return null; };
  f.snapshot({ turns: [{ id: "source", status: "completed" }] }); await f.relay.process(); assert.equal(f.relay.get(f.q.questionId).cardUpdated, false);
  f.m.notifier.updateQuestionCard = async id => { f.patches.push(id); return { code: 0 }; }; f.relay.state.entries[f.q.questionId].retryPatchAt = 0;
  await f.relay.process(); assert.deepEqual(f.patches, ["om_question", "om_question"]); assert.equal(f.sends.length, 1); assert.equal(f.relay.get(f.q.questionId).cardUpdated, true);
});
test("a source ending while the first POST is in flight still patches the sent card to expired", async t => {
  const f = await fixture(t); let release;
  f.m.notifier.sendQuestionCard = async () => new Promise(resolve => { release = () => resolve({ code: 0, data: { message_id: "om_question" } }); });
  const publishing = f.publish(); while (!release) await new Promise(r => setTimeout(r, 1));
  f.snapshot({ turns: [{ id: "source", status: "completed" }] }); await f.relay.process(); release(); await publishing; await f.relay.process();
  assert.equal(f.relay.get(f.q.questionId).status, "expired"); assert.ok(JSON.stringify(f.patches.at(-1).card).includes("已过期"));
});
test("disk failure prevents acknowledgement or native write until the intent is really durable", async t => {
  const f = await fixture(t); await f.publish(); const properPath = f.relay.path; f.relay.path = path.join(f.dir, "missing", "actions.json");
  await assert.rejects(f.relay.acceptCard(f.callback())); await assert.rejects(f.relay.process()); assert.equal(f.writes.length, 0);
  f.relay.path = properPath; await f.relay.acceptCard(f.callback());
  assert.equal(JSON.parse(await readFile(properPath, "utf8")).entries[f.q.questionId].status, "answer_pending");
  await f.relay.process(); assert.equal(f.writes.length, 1);
});
test("business handoff pauses question processing, without cancelling or re-sending answers", async t => {
  const f = await fixture(t); await f.publish(); f.m.handoffPaused = true; await f.relay.acceptCard(f.callback()); await f.relay.process();
  assert.equal(f.writes.length, 0); f.m.handoffPaused = false; await f.relay.process(); assert.equal(f.writes.length, 1);
});
test("WS card response ACK echoes 64-bit IDs with base64 toast and does not await native dispatch", async t => {
  const f = await fixture(t); await f.publish();
  const client = new FeishuWebsocketClient({ enabled: true }, { onCardAction: payload => f.relay.acceptCard(payload) });
  const frame = decodeFrame(encodeFrame({ SeqID: 2n ** 60n + 1n, LogID: 2n ** 61n + 3n, method: 1, service: 4,
    payload_encoding: "json", payload_type: "json", LogIDNew: "test-log-id",
    headers: [["type", "card"]], payload: Buffer.from(JSON.stringify(f.callback())) }));
  let ack; await client._handleFrame({ send: bytes => { ack = decodeFrame(bytes); } }, frame, 4);
  assert.equal(ack.SeqID, frame.SeqID); assert.equal(ack.LogID, frame.LogID); assert.equal(f.writes.length, 0);
  assert.equal(ack.payload_encoding, "json"); assert.equal(ack.payload_type, "json"); assert.equal(ack.LogIDNew, "test-log-id");
  const envelope = JSON.parse(new TextDecoder().decode(ack.payload)); assert.equal(envelope.code, 200);
  assert.equal(JSON.parse(Buffer.from(envelope.data, "base64")).toast.type, "success");
});
test("malformed, ordinary message or permission callback is not a public question reply", () => {
  assert.equal(parseCardAction(Buffer.from("bad")), null); assert.equal(parseCardAction({ event: { action: { value: {} } } }), null);
  assert.throws(() => normalizeQuestion({ questionId: randomUUID(), threadId: "chat", questions: [{ title: "test" }], sourceQuestionItemIds: [JSON.stringify(["request_environment_input", "call_permission", 0])] }));
});
test("question notifier uses exact POST UUID, PATCH message ID and does not send old-target new cards", async t => {
  const previous = globalThis.fetch; t.after(() => { globalThis.fetch = previous; });
  const calls = []; globalThis.fetch = async (url, opts) => { calls.push({ url, opts }); return { json: async () => ({ code: 0, data: { message_id: "om_test" } }) }; };
  let selected = true; const n = new FeishuNotifier({ appId: "test", appSecret: "test", chatId: "oc_test", canSend: () => selected });
  n._token = "test-only-token"; n._tokenExpiresAt = Date.now() / 1000 + 100;
  await n.sendQuestionCard({ schema: "2.0" }, { uuid: "same-uuid", threadId: "chat" });
  assert.equal(JSON.parse(calls[0].opts.body).uuid, "same-uuid"); selected = false;
  await n.sendQuestionCard({}, { uuid: "same-uuid", threadId: "chat" }); assert.equal(calls.length, 1);
  await n.updateQuestionCard("om_test", {}, { threadId: "chat" }); assert.equal(calls[1].opts.method, "PATCH"); assert.ok(calls[1].url.endsWith("om_test"));
});

test("skip is durable before ACK, ignores injected answers and closes the original card once", async t => {
 const f=await fixture(t);await f.publish();
 const button=nodes(f.sends[0].card).find(e=>e.tag==="button"&&e.text?.content==="跳过");
 assert.equal(button.value.action,"form_skip");
 const click=f.callback({action:"skip",answer:"forged choice"});
 assert.equal((await f.relay.acceptCard(click)).toast.type,"success");
 const entry=JSON.parse(await readFile(f.relay.path,"utf8")).entries[f.q.questionId];
 assert.equal(entry.status,"answer_pending");assert.deepEqual(entry.skippedQuestionIndexes,[0]);
 assert.deepEqual(entry.answers,[SKIPPED_QUESTION_ANSWER]);assert.equal(f.writes.length,0);
 await f.relay.acceptCard(click);await f.relay.process();await f.relay.process();
 assert.equal(f.writes.length,1);assert.equal(f.relay.get(f.q.questionId).status,"skipped");
 assert.ok(!f.writes[0].request.text.includes("forged choice"));
 assert.ok(JSON.stringify(f.patches.at(-1).card).includes("已跳过"));
 assert.ok(!JSON.stringify(f.patches.at(-1).card).includes('"type":"callback"'));
 await f.relay.acceptCard(f.callback({action:"skip"}));await f.relay.process();
 assert.equal(f.writes.length,1);
});
test("skip preserves selected multi-question answers and skips only unanswered questions",async t=>{
 const f=await fixture(t);await f.publish({questions:[f.q.questions[0],{title:"第二题",options:["甲","乙"]}]});
 f.relay.state.entries[f.q.questionId].answers=[f.q.questions[0].options[0],null];
 await f.relay.acceptCard(f.callback({optionIndex:1}));await f.relay.process();
 assert.equal(f.writes.length,0);
 await f.relay.acceptCard(f.callback({action:"skip"}));await f.relay.process();
 assert.deepEqual(f.relay.get(f.q.questionId).answers,["仅保留状态",SKIPPED_QUESTION_ANSWER]);
 assert.deepEqual(f.relay.get(f.q.questionId).skippedQuestionIndexes,[1]);
 assert.equal(f.writes.length,1);assert.equal(f.relay.get(f.q.questionId).status,"skipped");
 assert.ok(JSON.stringify(f.patches.at(-1).card).includes("部分跳过"));
});
test("a free-text clarification without options still exposes a working skip button",async t=>{
 const f=await fixture(t);await f.publish({questions:[{title:"请补充说明"}]});
 assert.ok(nodes(f.sends[0].card).some(e=>e.tag==="button"&&e.text?.content==="跳过"));
 await f.relay.acceptCard(f.callback({action:"skip"}));await f.relay.process();
 assert.equal(f.relay.get(f.q.questionId).status,"skipped");
 assert.deepEqual(f.relay.get(f.q.questionId).answers,[SKIPPED_QUESTION_ANSWER]);
});
test("skip retains the same user, chat, message, nonce and source-binding checks",async t=>{
 const f=await fixture(t);await f.publish();
 for(const extra of [{openId:"stranger"},{chatId:"other"},{messageId:"wrong"}])
  assert.equal((await f.relay.acceptCard(f.callback({action:"skip"},extra))).toast.type,"error");
 assert.equal((await f.relay.acceptCard(f.callback({action:"skip",nonce:"wrong"}))).toast.type,"error");
 assert.equal(f.relay.get(f.q.questionId).status,"open");assert.equal(f.writes.length,0);
 f.m.state.selectionEpoch++;
 assert.equal((await f.relay.acceptCard(f.callback({action:"skip"}))).toast.type,"warning");
 assert.equal(f.relay.get(f.q.questionId).status,"expired");assert.equal(f.writes.length,0);
});
test("a queued skip remains pending through restart and finishes only on native acceptance",async t=>{
 const f=await fixture(t);await f.publish();f.native(async()=>({action:"message_queued"}));
 await f.relay.acceptCard(f.callback({action:"skip"}));await f.relay.process();
 assert.equal(f.relay.get(f.q.questionId).status,"answer_pending");assert.equal(f.writes.length,1);
 const restored=new QuestionRelay(f.m);await restored.initialize();await restored.process();
 assert.equal(restored.get(f.q.questionId).status,"answer_pending");assert.equal(f.writes.length,1);
 const id=restored.state.entries[f.q.questionId].nativeMessageId;
 f.m.state.receipts={[id]:{nativeDispatch:{status:"accepted"}}};
 await restored.process();await restored.process();
 assert.equal(restored.get(f.q.questionId).status,"skipped");assert.equal(f.writes.length,1);
 await restored.close();
});

test("mixed questions render numbered options and editable multiline fields with explicit submit", async t => {
  const f = await fixture(t); await f.publish({ questions: [f.q.questions[0], { title: "补充说明" }] });
  const all = nodes(f.sends[0].card), inputs = all.filter(n => n.tag === "input");
  assert.deepEqual(inputs.map(n => n.name), ["answer_0", "answer_1"]);
  assert.ok(inputs.every(n => n.input_type === "multiline_text" && n.required === false && n.max_length === 1000));
  assert.deepEqual(all.filter(n => n.tag === "button").map(n => n.value.action), ["form_select", "form_select", "form_skip", "form_submit"]);
});

test("form selection preserves other drafts across restart and never submits an answer", async t => {
  const f = await fixture(t); await f.publish({ questions: [f.q.questions[0], { title: "补充说明" }] });
  const click = f.callback({ action: "form_select", optionIndex: 1, answer: "forged" },
    { formValues: { answer_0: "旧输入", answer_1: "保留这一段\n第二行" } });
  assert.equal((await f.relay.acceptCard(click)).toast.type, "success");
  const restored = new QuestionRelay(f.m); await restored.initialize(); await restored.process();
  assert.equal(f.writes.length, 0); assert.equal(restored.get(f.q.questionId).status, "open");
  const inputs = nodes(f.patches.at(-1).card).filter(n => n.tag === "input");
  assert.deepEqual(inputs.map(n => n.default_value), ["仅保留状态", "保留这一段\n第二行"]);
  assert.equal(f.patches.at(-1).id, "om_question"); assert.equal(f.sends.length, 1);
  await restored.acceptCard(click); assert.deepEqual(restored.state.entries[f.q.questionId].draftAnswers, inputs.map(n => n.default_value));
  await restored.close();
});

test("single-question form selection still requires explicit submit", async t => {
  const f = await fixture(t); await f.publish();
  await f.relay.acceptCard(f.callback({ action: "form_select" }, { formValues: { answer_0: "" } }));
  await f.relay.process(); assert.equal(f.writes.length, 0); assert.equal(f.relay.get(f.q.questionId).status, "open");
  await f.relay.acceptCard(f.callback({ action: "form_submit" }, { formValues: { answer_0: f.q.questions[0].options[0] } }));
  await f.relay.process(); assert.equal(f.writes.length, 1);
});

test("custom form answers bind exact native question IDs and dispatch once after durable ACK", async t => {
  const f = await fixture(t);
  const ids = [0, 1].map(i => JSON.stringify(["request_user_input_async", "call_form", i]));
  f.m.deliveryState = async () => f.m.read();
  await f.publish({ questions: [f.q.questions[0], { title: "补充说明" }], delivery: "async", sourceTurnId: "source", sourceQuestionItemIds: ids });
  const answers = ["这是自定义答案，并非预设选项", "第一行\n第二行 🐎"];
  const click = f.callback({ action: "form_submit", answer: "forged" },
    { formValues: { answer_0: answers[0], answer_1: answers[1], threadId: "attacker-thread" } });
  assert.equal((await f.relay.acceptCard(click)).toast.type, "success");
  assert.equal(f.writes.length, 0);
  const disk = JSON.parse(await readFile(f.relay.path, "utf8")).entries[f.q.questionId];
  assert.deepEqual(disk.answers, answers); assert.equal(disk.status, "answer_pending");
  await f.relay.acceptCard(click); await f.relay.process(); await f.relay.process();
  assert.equal(f.writes.length, 1);
  const replies = JSON.parse(f.writes[0].request.text.match(/<send_user_message_question_reply>([\s\S]*)<\/send_user_message_question_reply>/)[1]);
  assert.deepEqual(replies.map(r => r.questionItemId), ids); assert.deepEqual(replies.map(r => r.answer), answers);
  assert.ok(!f.writes[0].request.text.includes("attacker-thread")); assert.ok(!f.writes[0].request.text.includes("forged"));
  assert.ok(!nodes(f.patches.at(-1).card).some(n => n.tag === "input" || n.tag === "button"));
  await f.relay.acceptCard(f.callback({ action: "form_submit" }, { formValues: { answer_0: "changed", answer_1: "changed" } }));
  await f.relay.process(); assert.equal(f.writes.length, 1);
});

for (const [label, formValues] of [["missing", undefined], ["array", []], ["number", { answer_0: 42 }],
  ["oversized", { answer_0: "x".repeat(1001) }], ["nested", { answer_0: { text: "nested" } }]]) {
  test(`malformed form is rejected before recording an answer: ${label}`, async t => {
    const f = await fixture(t); await f.publish({ questions: [{ title: "填写回答" }] });
    assert.equal((await f.relay.acceptCard(f.callback({ action: "form_submit" }, { formValues }))).toast.type, "error");
    assert.equal(f.relay.get(f.q.questionId).status, "open");
    assert.equal(f.relay.state.entries[f.q.questionId].answers, undefined);
    await f.relay.process(); assert.equal(f.writes.length, 0);
  });
}

test("blank submit does not commit a partial answer; explicit skip preserves only filled fields", async t => {
  const f = await fixture(t); await f.publish({ questions: [f.q.questions[0], { title: "第二题" }] });
  const formValues = { answer_0: "自定义补充", answer_1: "  \n " };
  assert.equal((await f.relay.acceptCard(f.callback({ action: "form_submit" }, { formValues }))).toast.type, "warning");
  assert.equal(f.relay.state.entries[f.q.questionId].answers, undefined); assert.equal(f.writes.length, 0);
  assert.equal((await f.relay.acceptCard(f.callback({ action: "form_skip" }, { formValues }))).toast.type, "success");
  await f.relay.process(); assert.deepEqual(f.relay.get(f.q.questionId).answers, ["自定义补充", SKIPPED_QUESTION_ANSWER]);
  assert.deepEqual(f.relay.get(f.q.questionId).skippedQuestionIndexes, [1]);
  assert.equal(f.writes.length, 1); assert.ok(JSON.stringify(f.patches.at(-1).card).includes("部分跳过"));
});

test("empty form can skip every question without inventing answers", async t => {
  const f = await fixture(t); await f.publish({ questions: [{ title: "填写回答" }] });
  await f.relay.acceptCard(f.callback({ action: "form_skip" }, { formValues: {} }));
  await f.relay.process(); assert.deepEqual(f.relay.get(f.q.questionId).answers, [SKIPPED_QUESTION_ANSWER]);
  assert.equal(f.relay.get(f.q.questionId).status, "skipped");
});

test("form submission keeps trainer/chat/message/nonce/epoch authorization", async t => {
  const f = await fixture(t); await f.publish();
  for (const extra of [{ openId: "stranger" }, { chatId: "other" }, { messageId: "other" }]) {
    assert.equal((await f.relay.acceptCard(f.callback({ action: "form_submit" }, { ...extra, formValues: { answer_0: "valid" } }))).toast.type, "error");
  }
  assert.equal((await f.relay.acceptCard(f.callback({ action: "form_submit", nonce: "wrong" }, { formValues: { answer_0: "valid" } }))).toast.type, "error");
  assert.equal(f.relay.state.entries[f.q.questionId].draftAnswers, undefined);
  f.m.state.selectionEpoch++;
  assert.equal((await f.relay.acceptCard(f.callback({ action: "form_submit" }, { formValues: { answer_0: "valid" } }))).toast.type, "warning");
  assert.equal(f.relay.get(f.q.questionId).status, "expired"); assert.equal(f.writes.length, 0);
});

test("form fields survive the actual websocket callback envelope and ACK before native I/O", async t => {
  const f = await fixture(t); await f.publish({ questions: [{ title: "填写回答" }] });
  const payload = Buffer.from(JSON.stringify(f.callback({ action: "form_submit" }, { formValues: { answer_0: "来自输入框" } })));
  assert.deepEqual(parseCardAction(payload).formValues, { answer_0: "来自输入框" });
  const client = new FeishuWebsocketClient({ enabled: true }, { onCardAction: p => f.relay.acceptCard(p) });
  const frame = decodeFrame(encodeFrame({ SeqID: 123n, LogID: 456n, method: 1, service: 4,
    headers: [["type", "card"]], payload }));
  let ack; await client._handleFrame({ send: bytes => { ack = decodeFrame(bytes); } }, frame, 4);
  const envelope = JSON.parse(new TextDecoder().decode(ack.payload));
  assert.equal(JSON.parse(Buffer.from(envelope.data, "base64")).toast.type, "success");
  assert.equal(f.writes.length, 0); await f.relay.process(); assert.equal(f.writes.length, 1);
  assert.deepEqual(f.relay.get(f.q.questionId).answers, ["来自输入框"]);
});
