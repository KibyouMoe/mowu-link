import assert from "node:assert/strict";
import test from "node:test";
import { FeishuNotifier } from "../feishu.mjs";
import { clipBlock, clipOneLine, findRouterTurn, lastAssistantText, routerMarker,
  routerMessageId, routerPrompt, taskBoardText } from "../router-tasks.mjs";

test("router message ids are deterministic UUIDv4 values per task", () => {
  const first = routerMessageId("t-9df6613a5c");
  assert.match(first, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(first, routerMessageId("t-9df6613a5c"));
  assert.notEqual(first, routerMessageId("t-9df6613a5d"));
});

test("router prompt carries the marker and optional context", () => {
  assert.equal(routerPrompt({ taskId: "t-1", goal: " 查一下天气 " }), "[router t-1] 查一下天气");
  assert.equal(routerPrompt({ taskId: "t-1", goal: "g", context: { a: 1 } }),
    "[router t-1] g\n\n[router context]\n{\"a\":1}");
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

test("task board messages are exactly the three lifecycle lines", () => {
  assert.equal(taskBoardText({ phase: "start", taskId: "t-1", goal: "a\nb   c" }), "任务 t-1 开始 · a b c");
  assert.equal(taskBoardText({ phase: "done", taskId: "t-1", summary: "全部完成" }), "完成 · t-1 · 全部完成");
  assert.equal(taskBoardText({ phase: "failed", taskId: "t-1", failure: "boom" }), "失败 · t-1 · boom");
  assert.throws(() => taskBoardText({ phase: "progress", taskId: "t-1" }), /unknown task board phase/);
});

test("task board clipping stays inside the agreed budgets", () => {
  const longGoal = "目".repeat(200);
  const start = taskBoardText({ phase: "start", taskId: "t-1", goal: longGoal });
  assert.equal(start, `任务 t-1 开始 · ${"目".repeat(60)}…`);
  const longSummary = "结".repeat(2000);
  const done = taskBoardText({ phase: "done", taskId: "t-1", summary: longSummary });
  assert.equal(done, `完成 · t-1 · ${"结".repeat(800)}…`);
  assert.equal(clipOneLine("a\n\nb", 10), "a b");
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
