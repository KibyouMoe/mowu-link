import assert from "node:assert/strict";
import test from "node:test";
import { FeishuNotifier } from "../feishu.mjs";
import { clipBlock, findRouterTurn, lastAssistantText, normalizeSubscriptionConfig,
  routerMarker, routerMessageId, routerPrompt, subscriptionMarker,
  subscriptionMessageId, subscriptionNoticeText, taskBoardTerminalPhase,
  taskBoardText, taskBoardTitle } from "../router-tasks.mjs";

test("router message ids are deterministic UUIDv4 values per task", () => {
  const first = routerMessageId("t-9df6613a5c");
  assert.match(first, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(first, routerMessageId("t-9df6613a5c"));
  assert.notEqual(first, routerMessageId("t-9df6613a5d"));
});

test("router prompt carries the marker, 溪流's origin and optional context", () => {
  const prompt = routerPrompt({ taskId: "t-1", goal: " 查一下天气 " });
  assert.match(prompt, /^\[router t-1\] 来自超级溪流的指令（任务 t-1）\n/);
  assert.match(prompt, /不是训练员本人；回复时称呼「溪流」/);
  assert.ok(prompt.endsWith("\n\n查一下天气"));
  assert.equal(routerPrompt({ taskId: "t-1", goal: "g", context: { a: 1 } }),
    routerPrompt({ taskId: "t-1", goal: "g" }) + "\n\n[router context]\n{\"a\":1}");
});

test("findRouterTurn picks the newest turn whose input is this task", () => {
  const marker = routerMarker("t-abc");
  const snapshot = { turns: [
    { id: "turn-1", status: "completed", items: [{ type: "userMessage", text: "飞书：随便聊聊" }] },
    { id: "turn-2", status: "completed", items: [{ type: "userMessage", text: `${marker} do it` }] },
    { id: "turn-3", status: "completed", items: [{ type: "userMessage", text: "飞书：又说一句" }] },
  ] };
  assert.equal(findRouterTurn(snapshot, marker)?.id, "turn-2");
  assert.equal(findRouterTurn({ turns: [{ id: "x", items: [{ type: "userMessage", content: [{ text: `${marker} c` }] }] }] }, marker)?.id, "x");
  assert.equal(findRouterTurn({ turns: [{ id: "x", status: "running", items: [{ type: "agentMessage", text: marker }] }] }, marker), null);
  assert.equal(findRouterTurn({}, marker), null);
});

test("lastAssistantText prefers the announced final answer", () => {
  const turn = { items: [
    { type: "userMessage", text: "[router t] g" },
    { type: "agentMessage", text: "中间说明", phase: "commentary" },
    { type: "agentMessage", text: "最终答复", phase: "final_answer" },
  ] };
  assert.equal(lastAssistantText(turn), "最终答复");
  assert.equal(lastAssistantText({ items: [{ type: "agentMessage", text: "只有一条" }] }), "只有一条");
  assert.equal(lastAssistantText(null), "");
});

test("task board speaks only for start and failure", () => {
  assert.equal(taskBoardText({ phase: "start", taskId: "t-1", goal: "a\nb   c" }), "a\nb   c");
  assert.equal(taskBoardText({ phase: "failed", taskId: "t-1", failure: "boom" }), "失败 · t-1 · boom");
  // A successful task ends with the manager's reply in the same chat; a second
  // message carrying the same body is what the trainer rejected.
  assert.throws(() => taskBoardText({ phase: "done", taskId: "t-1", summary: "全部完成" }),
    /unknown task board phase/);
  assert.throws(() => taskBoardText({ phase: "progress", taskId: "t-1" }), /unknown task board phase/);
});

test("the start line is 溪流's instruction with the task id in the title", () => {
  assert.equal(taskBoardTitle({ phase: "start", taskId: "t-1" }), "来自超级溪流的指令\n（任务 t-1）");
  assert.equal(taskBoardTitle({ phase: "failed", taskId: "t-1" }), null);
  // Body has no "任务 t-xxx 开始" prefix; it is the goal itself.
  const text = taskBoardText({ phase: "start", taskId: "t-1", goal: "查一下天气" });
  assert.equal(text, "查一下天气");
  assert.doesNotMatch(text, /开始|t-1/);
  assert.equal(taskBoardText({ phase: "start", taskId: "t-1", goal: "  " }), "（无目标文本）");
});

test("a done task sends no terminal board line", () => {
  assert.equal(taskBoardTerminalPhase("done"), null);
  assert.equal(taskBoardTerminalPhase("failed"), "failed");
  // Any non-done terminal state still gets the failure line.
  assert.equal(taskBoardTerminalPhase("interrupted"), "failed");
});

test("task board clipping stays inside the agreed budgets", () => {
  // The goal is shown verbatim; only a pathological body hits the 4000 guard.
  const goal = "目".repeat(3900);
  assert.equal(taskBoardText({ phase: "start", taskId: "t-1", goal }), goal);
  const huge = "目".repeat(4100);
  assert.equal(taskBoardText({ phase: "start", taskId: "t-1", goal: huge }), `${"目".repeat(4000)}…（已截断）`);
  const longSummary = "结".repeat(2000);
  const failed = taskBoardText({ phase: "failed", taskId: "t-1", summary: longSummary });
  assert.equal(failed, `失败 · t-1 · ${"结".repeat(800)}…`);
  assert.equal(clipBlock("  x  ", 10), "x");
});

test("FeishuNotifier can address the trainer p2p chat by open_id", async () => {
  const notifier = new FeishuNotifier({ appId: "app", appSecret: "secret", bridgeName: "小栗帽" });
  const calls = [];
  notifier.getToken = async () => "tenant-token";
  notifier._postChunk = async (...args) => { calls.push(args); return { code: 0, data: { message_id: "om_task_board" } }; };
  const response = await notifier.sendMessage("完成 · t-1 · ok",
    { openId: "ou_trainer", receiveIdType: "open_id", title: "小栗帽 · 任务看板" });
  assert.equal(response?.data?.message_id, "om_task_board");
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "ou_trainer");
  assert.equal(calls[0][6], "open_id");
});

test("FeishuNotifier keeps chat_id delivery as the default", async () => {
  const notifier = new FeishuNotifier({ appId: "app", appSecret: "secret", bridgeName: "小栗帽", chatId: "oc_chat" });
  const calls = [];
  notifier.getToken = async () => "tenant-token";
  notifier._postChunk = async (...args) => { calls.push(args); return { code: 0, data: { message_id: "om_chat" } }; };
  await notifier.sendMessage("普通消息", { title: "小栗帽" });
  assert.equal(calls[0][0], "oc_chat");
  assert.equal(calls[0][6], "chat_id");
});

test("subscription events get a deterministic message id keyed by source and id", () => {
  const first = subscriptionMessageId({ source: "test", id: 7 });
  assert.match(first, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(first, subscriptionMessageId({ source: "test", id: 7 }));
  assert.notEqual(first, subscriptionMessageId({ source: "test", id: 8 }));
  assert.notEqual(first, subscriptionMessageId({ source: "mail", id: 7 }));
});

test("the subscription marker is the exact message prefix", () => {
  assert.equal(subscriptionMarker({ source: "test", kind: "chat", id: 12 }),
    "[订阅事件 source=test kind=chat id=12]");
});

test("the Feishu line is one row: 订阅事件 · source · first 80 chars", () => {
  assert.equal(subscriptionNoticeText({ source: "test", text: "  你好\n  世界  " }), "订阅事件 · test · 你好 世界");
  const long = "字".repeat(120);
  const line = subscriptionNoticeText({ source: "test", text: long });
  assert.equal(line, `订阅事件 · test · ${"字".repeat(80)}…`);
  assert.equal(subscriptionNoticeText({ source: "", text: "" }), "订阅事件 · (unknown) · ");
});

test("subscription config accepts objects, drops junk and splits string kinds", () => {
  assert.deepEqual(normalizeSubscriptionConfig([
    { source: " test ", kinds: [" chat ", ""] },
    { source: "mail" },
    { source: "  " },
    null,
    "nope",
    { kinds: ["chat"] },
  ]), [
    { source: "test", kinds: ["chat"] },
    { source: "mail", kinds: [] },
  ]);
  assert.deepEqual(normalizeSubscriptionConfig({ source: "test" }), []);
  assert.deepEqual(normalizeSubscriptionConfig(undefined), []);
});
