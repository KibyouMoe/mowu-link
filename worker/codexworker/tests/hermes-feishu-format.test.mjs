import test from "node:test";
import assert from "node:assert/strict";
import { hermesPostRows, hermesOutbound, hermesChunks } from "../hermes-feishu-format.mjs";
import { goalPrompt, BRIDGE_HELP } from "../feishu-commands.mjs";
import { FeishuNotifier } from "../feishu.mjs";

test("ordinary rich text uses a card with title and session ID and retains code/table source", async t => {
  const original = globalThis.fetch; let body;
  globalThis.fetch = async (_url, options) => { body = JSON.parse(options.body); return { json: async () => ({ code: 0, data: { message_id: "card-id" } }) }; };
  t.after(() => { globalThis.fetch = original; });
  const text = '**说明**\n```js\nconsole.log(1)\n```\n| A | B |\n|---|---|\n| 1 | 2 |';
  await new FeishuNotifier({})._postChunk("chat", text, "助手-回复", "test-only-token", "session-id", "stable-uuid");
  assert.equal(body.msg_type, "interactive"); assert.equal(body.uuid, "stable-uuid");
  const card = JSON.parse(body.content);
  assert.equal(card.header.title.content, "助手-回复"); assert.equal(card.elements[0].content, text);
  assert.match(card.elements.at(-1).elements[0].content, /session-id/);
});
test("Hermes native post separates fenced code from trailing prose and preserves tables", () => {
  const text = "**进展**\n\n```js\nconsole.log(1)\n```\n后续文字\n| A | B |\n|---|---|\n| 1 | 2 |";
  assert.deepEqual(hermesPostRows(text).map(r => r[0].text), ["**进展**\n", "```js\nconsole.log(1)\n```", "后续文字\n| A | B |\n|---|---|\n| 1 | 2 |"]);
  assert.equal(hermesOutbound(text).msg_type, "post");
  assert.equal(hermesOutbound("普通文字").msg_type, "text");
  assert.equal(hermesOutbound("普通分段", true).msg_type, "post");
});
test("long code chunks stay bounded, valid Unicode and balanced fenced blocks", () => {
  const text = "```js\n" + "const 训练员 = '😀';\n".repeat(1800) + "```\n完成";
  const parts = hermesChunks(text); assert.ok(parts.length > 2);
  for (const part of parts) { assert.ok(part.length <= 8000); assert.equal(part.split("```").length % 2, 1); assert.equal(part.isWellFormed(), true); }
});
test("goal commands carry explicit user intent and never claim native success", () => {
  assert.equal(goalPrompt("/goal"), null);
  assert.match(goalPrompt("/goal 完成测试"), /create_goal/); assert.match(goalPrompt("/goal 完成测试"), /不要自设 token_budget/);
  assert.match(goalPrompt("/goal status"), /不要创建或修改/); assert.match(goalPrompt("/goal pause"), /paused/);
  assert.match(goalPrompt("/goal resume"), /不能谎报/); assert.match(BRIDGE_HELP, /发送成功不代表/);
});
