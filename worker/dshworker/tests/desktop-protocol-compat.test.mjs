import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, appendFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { LocalPublicJournal, publicJournalItem } from "../local-public-journal.mjs";
import { defaultPublicJournalRoot } from "../interface/journal-paths.mjs";
import { nativeQuestionReply, nativeAnswersInSnapshot } from "../interface/public-questions.mjs";
import { findRouterTurn, lastAssistantText } from "../router-tasks.mjs";
import { DesktopManager } from "../desktop-manager.mjs";

const threadId = "01a17e53-76b1-7e63-bd85-2161ef913ce8";
const record = (item, turnId = "turn-case") => ({ type: "event_msg", timestamp: "2026-11-13T00:00:00Z",
  payload: { type: "item_completed", thread_id: threadId, turn_id: turnId, item } });
const project = item => publicJournalItem(record(item), threadId, { includeInputs: true });
const line = value => JSON.stringify(value) + "\n";

test("both Desktop content tag versions preserve input and public output byte-for-byte", () => {
  const value = 'MiXeD /Work/Aa 中文 😀\n[router t-MiXeD]';
  for (const [outer, expected] of [["UserMessage", "userMessage"], ["userMessage", "userMessage"],
    ["AgentMessage", "agentMessage"], ["agentMessage", "agentMessage"]]) {
    for (const type of ["Text", "text"]) {
      const source = { type: outer, id: "Message-MiXeD", phase: "final", content: [{ type, text: value }] };
      const before = structuredClone(source), got = project(source);
      assert.equal(got.item.type, expected); assert.equal(got.item.text, value);
      assert.equal(got.item.id, source.id); assert.deepEqual(source, before);
    }
  }
});

test("mixed text and image input produces markers, never loses lowercase image blocks", () => {
  for (const type of ["Image", "image", "LocalImage", "localImage"]) {
    assert.equal(project({ type: "UserMessage", id: "input", content: [
      { type: "Text", text: "Before" }, { type, path: "/Mixed/Pic.PNG" }, { type: "text", text: "After" },
    ] }).item.text, "Before[图片输入]After");
  }
});

test("malformed and unsupported content fails explicitly instead of producing empty messages", () => {
  for (const outer of ["UserMessage", "AgentMessage"]) {
    for (const content of [null, {}, [null], [{ type: "text", text: null }],
      [{ type: "Text", text: 12 }], [{ type: "TEXT", text: "unknown" }], [{ type: "futureType", text: "unknown" }]]) {
      assert.throws(() => project({ type: outer, id: "bad", phase: "final", content }), /public_journal_(public_text_incomplete|content_unsupported)/);
    }
  }
});

test("case compatibility never publishes analysis, reasoning, tool output or mismatched identities", () => {
  for (const type of ["Reasoning", "reasoning", "CommandExecution", "commandExecution", "McpToolCall", "mcpToolCall"]) {
    assert.equal(project({ type, id: "private", content: [{ type: "text", text: "private" }] }), null);
  }
  for (const type of ["AgentMessage", "agentMessage"]) {
    assert.equal(project({ type, id: "private", phase: "analysis", content: [{ type: "text", text: "private" }] }), null);
  }
  const wrong = record({ type: "userMessage", id: "m", content: [{ type: "text", text: "public" }] });
  assert.throws(() => publicJournalItem(wrong, threadId.toUpperCase(), { includeInputs: true }), /identity/);
});

test("lowercase async question and native answer retain exact source IDs", () => {
  const asked = project({ type: "agentMessage", id: "Call-MiXeD", phase: "commentary", delivery: "async",
    questions: [{ title: "Write where?", options: ["A", "B"] }], content: [{ type: "text", text: "Write where?" }] });
  const q = asked.item.publicQuestion;
  assert.ok(q); assert.equal(JSON.parse(q.sourceQuestionItemIds[0])[1], "Call-MiXeD");
  const reply = nativeQuestionReply(q, ["Custom MiXeD Answer"]);
  const answered = project({ type: "userMessage", id: "reply", content: [{ type: "text", text: reply }] });
  assert.deepEqual(nativeAnswersInSnapshot(q, { turns: [{ items: [answered.item] }] }), ["Custom MiXeD Answer"]);
});

test("router matches PascalCase or camelCase messages and reads typed final content", () => {
  const marker = "[router t-MiXeD]";
  for (const [input, output, block] of [["UserMessage", "AgentMessage", "Text"], ["UserMessage", "AgentMessage", "text"],
    ["userMessage", "agentMessage", "text"]]) {
    const turn = { id: "target", items: [{ type: input, content: [{ type: block, text: marker }] },
      { type: output, phase: "final", content: [{ type: block, text: "Actual MiXeD result" }] },
      { type: output, phase: "analysis", text: "private" }] };
    assert.equal(findRouterTurn({ turns: [turn] }, marker), turn);
    assert.equal(lastAssistantText(turn), "Actual MiXeD result");
    assert.equal(findRouterTurn({ turns: [turn] }, marker.toLowerCase()), null);
  }
  const older = { id: "old", items: [{ type: "userMessage", text: marker }] };
  const newer = { id: "new", items: [{ type: "userMessage", text: marker }] };
  assert.equal(findRouterTurn({ page: { order: "newest_first" }, turns: [newer, older] }, marker).id, "new");
  assert.equal(findRouterTurn({ turns: [{ items: [{ type: "agentMessage", text: marker }] }] }, marker), null);
});

test("incremental lowercase records survive partial writes and restart without echoing input images", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "desktop-case-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const dir = path.join(root, "2026", "11", "13"); await mkdir(dir, { recursive: true });
  const file = path.join(dir, `rollout-${threadId}.jsonl`), session = { threadId, cwd: root };
  const ctx = { epoch: 1, turnId: null, includeInputs: true, includeTools: true };
  const pic = path.join(root, "MiXeD.PNG");
  await writeFile(file, line({ type: "session_meta", payload: { id: threadId, cwd: root } }) +
    line(record({ type: "userMessage", id: "input", content: [{ type: "text", text: "[router t-case]" }, { type: "localImage", path: pic }] })) +
    line(record({ type: "imageView", id: "view", path: pic })) +
    line(record({ type: "commandExecution", id: "tool", command: "echo test", status: "completed", exitCode: 0 })));
  const reader = new LocalPublicJournal(root), first = await reader.read(session, null, ctx);
  assert.deepEqual(first.events.map(e => e.item.type), ["userMessage", "toolActivity"]);
  assert.equal(first.events[0].item.text, "[router t-case][图片输入]");
  const final = line(record({ type: "agentMessage", id: "reply", phase: "final", content: [{ type: "text", text: "MiXeD reply" }] }));
  await appendFile(file, final.slice(0, -1));
  const partial = await reader.read(session, first.checkpoint, ctx); assert.equal(partial.events.length, 0);
  const restarted = new LocalPublicJournal(root); await appendFile(file, "\n");
  const complete = await restarted.read(session, partial.checkpoint, ctx);
  assert.deepEqual(complete.events.map(e => e.item.text), ["MiXeD reply"]);
  assert.equal((await restarted.read(session, complete.checkpoint, ctx)).events.length, 0);
});

test("journal root follows Codex home, independent of arbitrarily nested worker state", () => {
  const home = path.resolve("/test/MiXeD-home");
  assert.equal(defaultPublicJournalRoot({}, home), path.join(home, ".codex", "sessions"));
  assert.equal(defaultPublicJournalRoot({ CODEX_HOME: "~/Custom-Codex" }, home), path.join(home, "Custom-Codex", "sessions"));
  const manager = new DesktopManager({ stateDir: path.join(os.tmpdir(), "worker", "state", "extra", "cli") });
  assert.equal(manager.publicJournal.root, defaultPublicJournalRoot());
  const explicit = path.join(os.tmpdir(), "Custom-Logs");
  const configured = new DesktopManager({ stateDir: os.tmpdir(), config: { publicJournalRoot: explicit } });
  assert.equal(configured.publicJournal.root, explicit); assert.equal(configured.taskJournal.root, explicit);
});
