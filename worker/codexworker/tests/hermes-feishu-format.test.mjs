import test from "node:test";
import assert from "node:assert/strict";
import { hermesPostRows, hermesOutbound, hermesChunks, splitForFeishu,
  fenceStateAfter, FEISHU_MAX_MESSAGE_CHARS } from "../hermes-feishu-format.mjs";
import { goalPrompt, BRIDGE_HELP } from "../feishu-commands.mjs";
import { FeishuNotifier, cardPayload } from "../feishu.mjs";

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

// ---------------------------------------------------------------------------
// Long replies: Hermes `truncate_message` behaviour at Feishu's 4096-char cap.
// ---------------------------------------------------------------------------

const squash = text => String(text).replace(/\s+/g, "");

test("a reply under the cap is one message", () => {
  assert.deepEqual(splitForFeishu("短回复"), ["短回复"]);
  const exact = "字".repeat(FEISHU_MAX_MESSAGE_CHARS);
  assert.deepEqual(splitForFeishu(exact), [exact]);
  assert.equal(FEISHU_MAX_MESSAGE_CHARS, 4096);
});

test("a long prose reply splits on paragraph boundaries and loses nothing", () => {
  const paragraph = "第%d段：这里的文字用来把回复撑到限制以上，验证切分不会落在句子中间而是段落边界。";
  const text = Array.from({ length: 240 }, (_, i) => paragraph.replace("%d", String(i + 1))).join("\n\n");
  assert.ok(text.length > FEISHU_MAX_MESSAGE_CHARS * 2);
  const chunks = splitForFeishu(text);
  assert.ok(chunks.length >= 3);
  for (const chunk of chunks) {
    assert.ok(chunk.length <= FEISHU_MAX_MESSAGE_CHARS, `chunk ${chunk.length} over cap`);
    assert.ok(chunk.trim().length > 0);
  }
  // Paragraph-first: every chunk but the last ends right after a paragraph.
  for (const chunk of chunks.slice(0, -1)) assert.match(chunk, /\n$/, "split kept the newline");
  assert.equal(squash(chunks.join("")), squash(text));
});

test("a fenced code block is closed per chunk and reopened with the same language", () => {
  const code = "const 训练员 = '😀';\n";
  const text = `说明文字\n\n\`\`\`js\n${code.repeat(900)}\`\`\`\n结尾`;
  const chunks = splitForFeishu(text);
  assert.ok(chunks.length >= 3);
  for (const [index, chunk] of chunks.entries()) {
    assert.ok(chunk.length <= FEISHU_MAX_MESSAGE_CHARS);
    assert.equal(fenceStateAfter(chunk)[0], false, `chunk ${index + 1} leaves a fence open`);
    assert.equal(chunk.split("```").length % 2, 1);
    if (index > 0) assert.match(chunk, /^```js\n/, "the continuation reopens the fence");
  }
  // No code line was dropped or duplicated by the close/reopen dance.
  const strip = value => String(value).replace(/```js/g, "").replace(/```/g, "");
  assert.equal(squash(strip(chunks.join(""))), squash(strip(text)));
});

test("a long inline code span is not cut in half", () => {
  const span = "`" + "x".repeat(3000) + "`";
  const text = `${span}\n\n${"后文".repeat(1200)}`;
  const chunks = splitForFeishu(text);
  for (const chunk of chunks) {
    const ticks = (chunk.match(/`/g) ?? []).length;
    assert.equal(ticks % 2, 0, "every chunk carries a balanced inline code span");
  }
});

test("the final reply card is Hermes' _card() structure and keeps code blocks verbatim", () => {
  const text = "**结论**\n\n```py\ndef run():\n    return 1\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |";
  const card = JSON.parse(cardPayload(text, { title: "CodexWorker-回复", threadId: "cli-123" }));
  assert.deepEqual(card.config, { wide_screen_mode: true });
  assert.deepEqual(card.header, { title: { content: "CodexWorker-回复", tag: "plain_text" }, template: "indigo" });
  assert.equal(card.elements.length, 2);
  assert.equal(card.elements[0].tag, "markdown");
  assert.equal(card.elements[0].content, text, "fenced code and tables ride the markdown element untouched");
  assert.deepEqual(card.elements[1], { tag: "note", elements: [{ tag: "plain_text", content: "会话 cli-123" }] });
  // No session note when the thread is not known yet.
  assert.equal(JSON.parse(cardPayload("hi", { title: "t" })).elements.length, 1);
});
