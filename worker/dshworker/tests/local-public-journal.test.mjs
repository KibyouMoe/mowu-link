import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, appendFile, mkdir, rm, readFile } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { CompactJsonLines, LocalPublicJournal, publicJournalItem, journalTaskState } from "../local-public-journal.mjs";
import { journalInputPaths } from "../interface/journal-items.mjs";

const id = "01a17e53-76b1-7e63-bd85-2161ef913ce8";
const event = (item, turn = "turn-1", thread = id) => ({ timestamp: "2026-10-28T00:00:00.000Z", type: "event_msg", payload: { type: "item_completed", thread_id: thread, turn_id: turn, item } });
const text = (value, itemId = "message-1") => event({ type: "AgentMessage", id: itemId, phase: "commentary", content: [{ type: "Text", text: value }] });
const line = value => JSON.stringify(value) + "\n";
test("real lifecycle state ignores old completions, checks identity and never interprets sidebar state", () => {
  const state = journalTaskState({ type: "event_msg", payload: { type: "task_started", turn_id: "new" } }, id);
  assert.equal(state.turnStatus, "inProgress");
  assert.deepEqual(journalTaskState({ type: "event_msg", payload: { type: "task_complete", turn_id: "old" } }, id, state), state);
  assert.equal(journalTaskState({ type: "thread_settings_applied", payload: { status: "active" } }, id), null);
  assert.throws(() => journalTaskState({ type: "event_msg", payload: { type: "task_started", turn_id: "new", thread_id: "wrong" } }, id), /identity/);
});

test("task state has a durable bounded cursor across huge tool data, partial events and process restart", async t => {
  const f = await fixture(t, { byteBudget: 4096, chunkBytes: 1000 });
  await appendFile(f.file, line({ type: "event_msg", payload: { type: "task_started", turn_id: "turn-1" } }) +
    line({ type: "response_item", payload: { type: "function_call_output", output: "x".repeat(2 * 1024 * 1024) } }));
  let batch, checkpoint;
  do { batch = await f.reader.read(f.session, checkpoint, { ...f.ctx, stateOnly: true }); checkpoint = batch.checkpoint; } while (!batch.caughtUp);
  assert.equal(batch.taskState.turnStatus, "inProgress"); assert.deepEqual(batch.events, []);
  const restarted = new LocalPublicJournal(f.root);
  batch = await restarted.read(f.session, checkpoint, { ...f.ctx, stateOnly: true });
  assert.equal(batch.bytesRead, 0); assert.equal(batch.taskState.turnId, "turn-1");
  await appendFile(f.file, JSON.stringify({ type: "event_msg", payload: { type: "turn_aborted", turn_id: "turn-1" } }));
  const partial = await restarted.read(f.session, batch.checkpoint, { ...f.ctx, stateOnly: true });
  assert.ok(partial.pendingBytes > 0); assert.equal(partial.taskState.turnStatus, "inProgress");
  await appendFile(f.file, "\n");
  const complete = await restarted.read(f.session, partial.checkpoint, { ...f.ctx, stateOnly: true });
  assert.equal(complete.taskState.turnStatus, "interrupted"); assert.equal(complete.pendingBytes, 0);
});
async function fixture(t, options = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "public-journal-")); t.after(() => rm(root, { recursive: true, force: true }));
  const folder = path.join(root, "2026", "10", "28"); await mkdir(folder, { recursive: true });
  const file = path.join(folder, `rollout-2026-10-28T00-00-00-${id}.jsonl`);
  const session = { threadId: id, cwd: root };
  await writeFile(file, line({ type: "session_meta", payload: { id, cwd: root } }));
  return { root, file, session, reader: new LocalPublicJournal(root, options), ctx: { turnId: "turn-1", epoch: 1 } };
}

test("real snake_case image inputs preserve caption correlation and allow later public replies", async t => {
  for (const imagePath of ["/Users/Case Sensitive/图片.png", "C:\\Users\\Case Sensitive\\图片.png"]) {
    const f = await fixture(t);
    const input = event({ type: "UserMessage", id: "image-user", content: [
      { type: "text", text: "飞书：看这张图" }, { type: "local_image", path: imagePath }] });
    await appendFile(f.file, line(input) + line(text("图片后的公开回复")));
    const batch = await f.reader.read(f.session, null, { ...f.ctx, includeInputs: true });
    assert.equal(batch.caughtUp, true); assert.equal(batch.events.length, 2);
    assert.equal(batch.events[0].item.text, "飞书：看这张图[图片输入]");
    assert.equal(batch.events[0].item.nativeInputText, "飞书：看这张图");
    assert.deepEqual(journalInputPaths(input, id), [imagePath]);
    assert.equal(batch.events[1].item.text, "图片后的公开回复");
    assert.equal((await f.reader.read(f.session, batch.checkpoint, { ...f.ctx, includeInputs: true })).events.length, 0);
  }
});

test("compact parser handles escaped quotes, UTF-8 and every single-byte boundary", () => {
  const value = { escaped: '训练员😀\\\"\n', nested: [{ text: "ok" }] }, bytes = Buffer.from(line(value)), actual = [];
  const parser = new CompactJsonLines();
  for (let i = 0; i < bytes.length; i++) parser.feed(bytes.subarray(i, i + 1), i, (v, end) => actual.push({ v, end }));
  assert.deepEqual(actual, [{ v: value, end: bytes.length }]);
});
test("huge inline image is discarded while the real completed savedPath survives", () => {
  const parser = new CompactJsonLines({ stringLimit: 64 }), values = [];
  const raw = Buffer.from(line(event({ type: "Extension", id: "image", kind: "image_gen.generation", status: "completed", result: "a".repeat(5 * 1024 * 1024), savedPath: "C:/out/image.png", failure: null })));
  for (let i = 0; i < raw.length; i += 113) parser.feed(raw.subarray(i, i + 113), i, v => values.push(v));
  assert.equal(values[0].payload.item.result, null);
  assert.equal(publicJournalItem(values[0], id).item.savedPath, "C:/out/image.png");
});
test("reasoning, raw tool paths, user text and non-public phases never become output", () => {
  for (const type of ["Reasoning", "CommandExecution", "McpToolCall", "UserMessage"]) assert.equal(publicJournalItem(event({ type, id: type, text: "private", path: "C:/secret.png" }), id), null);
  assert.equal(publicJournalItem(event({ type: "AgentMessage", id: "analysis", phase: "analysis", content: [{ type: "Text", text: "private" }] }), id), null);
  assert.throws(() => publicJournalItem(text("wrong", "m", "wrong"), "wrong"), /identity/);
  const incomplete = text("large"); incomplete.payload.item.content[0].text = null;
  assert.throws(() => publicJournalItem(incomplete, id), /incomplete/);
});
test("incremental read passes huge records, retains native IDs, resumes after restart and never repeats", async t => {
  const f = await fixture(t, { byteBudget: 4000, chunkBytes: 997 });
  await appendFile(f.file, line(text("first")) + line(event({ type: "CommandExecution", id: "tool", aggregated_output: "x".repeat(2 * 1024 * 1024) })) + line(text("last", "message-2")));
  let checkpoint, events = [], reads = 0, batch;
  do { batch = await f.reader.read(f.session, checkpoint, f.ctx); checkpoint = batch.checkpoint; events.push(...batch.events); reads++; } while (!batch.caughtUp && reads < 600);
  assert.equal(batch.caughtUp, true); assert.equal(events.length, 2); assert.equal(events[1].item.id, "message-2"); assert.ok(reads > 400);
  const restarted = new LocalPublicJournal(f.root);
  assert.equal((await restarted.read(f.session, checkpoint, f.ctx)).events.length, 0);
  const partial = line(text("new 😀", "message-3"));
  await appendFile(f.file, partial.slice(0, -2));
  const unfinished = await restarted.read(f.session, checkpoint, f.ctx); assert.equal(unfinished.events.length, 0);
  await appendFile(f.file, partial.slice(-2));
  const finished = await restarted.read(f.session, unfinished.checkpoint, f.ctx);
  assert.equal(finished.events[0].item.text, "new 😀");
});
test("wrong cwd, replaced files and ambiguous identities fail closed", async t => {
  const f = await fixture(t); await appendFile(f.file, line(text("hello")));
  await assert.rejects(f.reader.read({ ...f.session, cwd: path.join(f.root, "other") }, null, f.ctx), /identity/);
  const batch = await f.reader.read(f.session, null, f.ctx);
  await writeFile(f.file, ""); await assert.rejects(f.reader.read(f.session, batch.checkpoint, f.ctx), /replaced/);
});
test("a mounted-device change preserves the verified cursor and only emits new events", async t => {
  const f = await fixture(t);
  await appendFile(f.file, line({ type: "event_msg", payload: { type: "task_started", turn_id: "turn-1" } }) + line(text("already delivered")));
  const first = await f.reader.read(f.session, null, f.ctx);
  const parts = first.checkpoint.identity.split(":"); parts[0] = String(Number(parts[0]) + 1);
  const saved = { ...first.checkpoint, identity: parts.join(":") };
  const appended = line(text("new public text", "message-after-reboot")) +
    line({ type: "event_msg", payload: { type: "task_complete", turn_id: "turn-1" } });
  await appendFile(f.file, appended);
  const resumed = await new LocalPublicJournal(f.root).read(f.session, saved, f.ctx);
  assert.deepEqual(resumed.events.map(event => event.item.text), ["new public text"]);
  assert.equal(resumed.bytesRead, Buffer.byteLength(appended));
  assert.equal(resumed.taskState.turnStatus, "completed");
  assert.equal(resumed.checkpoint.identity, first.checkpoint.identity);
  assert.ok(resumed.checkpoint.offset > first.checkpoint.offset);
});

test("device-renumbered cursors still reject inode/creation changes and truncation", async t => {
  const f = await fixture(t); await appendFile(f.file, line(text("public")));
  const { checkpoint } = await f.reader.read(f.session, null, f.ctx);
  for (const index of [1, 2]) {
    const parts = checkpoint.identity.split(":"); parts[0] = String(Number(parts[0]) + 1);
    // Windows file IDs can exceed Number.MAX_SAFE_INTEGER. Keep the persisted
    // inode as an exact decimal string when constructing a different identity.
    parts[index] = index === 1 ? String(BigInt(parts[index]) + 1n) : String(Number(parts[index]) + 1);
    await assert.rejects(new LocalPublicJournal(f.root).read(f.session, { ...checkpoint, identity: parts.join(":") }, f.ctx), /replaced/);
  }
  const parts = checkpoint.identity.split(":"); parts[0] = String(Number(parts[0]) + 1);
  await writeFile(f.file, "");
  await assert.rejects(new LocalPublicJournal(f.root).read(f.session, { ...checkpoint, identity: parts.join(":") }, f.ctx), /replaced/);
});

test("accepting a changed device requires session header verification even with a cached reader", async t => {
  const f = await fixture(t); await appendFile(f.file, line(text("public")));
  const { checkpoint } = await f.reader.read(f.session, null, f.ctx);
  const parts = checkpoint.identity.split(":"); parts[0] = String(Number(parts[0]) + 1);
  const contents = await readFile(f.file, "utf8");
  await writeFile(f.file, contents.replace('"id":"' + id + '"', '"id":"' + id.replace("01a", "02a") + '"'));
  await assert.rejects(f.reader.read(f.session, { ...checkpoint, identity: parts.join(":") }, f.ctx), /identity_mismatch/);
});
test("a turn starting between native status and initial file stat is not lost", async t => {
  const f = await fixture(t);
  const later = text("later", "later-message"); later.payload.turn_id = "newer-than-native-status";
  await appendFile(f.file, line({ type: "turn_context", payload: { turn_id: "turn-1" } }) + line(later));
  const batch = await f.reader.read(f.session, null, f.ctx);
  assert.equal(batch.events[0].item.text, "later");
});
test("old turns are baseline, new turns appended during catchup are retained, selections do not replay", async t => {
  const f = await fixture(t, { byteBudget: 100, chunkBytes: 100 });
  const old = text("old"); old.payload.turn_id = "previous";
  await appendFile(f.file, line(old) + line(text("current")));
  let batch = await f.reader.read(f.session, null, f.ctx), all = [...batch.events];
  const next = text("next", "next-message"); next.payload.turn_id = "next-turn"; await appendFile(f.file, line(next));
  while (!batch.caughtUp) { batch = await f.reader.read(f.session, batch.checkpoint, f.ctx); all.push(...batch.events); }
  assert.deepEqual(all.map(e => e.item.text), ["current", "next"]);
  const switched = new LocalPublicJournal(f.root);
  const quiet = await switched.read(f.session, batch.checkpoint, { ...f.ctx, epoch: 2, startAt: "2026-10-28T01:00:00Z" });
  assert.equal(quiet.events.length, 0);
});
