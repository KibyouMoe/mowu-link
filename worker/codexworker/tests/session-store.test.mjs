// Bridge-owned history: no network, no codex process, temp state only.
import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, readdir, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { SessionStore, groupTurns } from "../session-store.mjs";

const THREAD = "cli-11111111-2222-4333-8444-555555555555";

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-history-"));
  const store = await new SessionStore(path.join(root, "history"), { logger: () => {} }).init();
  return { root, store };
}

test("50 messages in one session stay complete through list and transcript reads", async t => {
  const { store } = await fixture(t);
  await store.create({ threadId: THREAD, cwd: "/tmp/workspace", source: "feishu", title: "长对话" });
  const messages = [];
  for (let i = 0; i < 25; i++) {
    messages.push({ role: "user", text: `问题 ${i}`, messageId: `m-u-${i}`, source: "feishu" });
    messages.push({ role: "assistant", text: `回答 ${i}`, messageId: `m-a-${i}`, turnId: `turn-${i}`, source: "feishu" });
  }
  await store.appendMessages(THREAD, messages);

  const snapshot = await store.read(THREAD, { limit: 1000 });
  assert.equal(snapshot.ok, true);
  assert.equal(snapshot.totalMessages, 50);
  assert.equal(snapshot.messages[0].text, "问题 0");
  assert.equal(snapshot.messages.at(-1).text, "回答 24");
  assert.equal(snapshot.messages[0].role, "user");
  assert.equal(snapshot.messages[1].role, "assistant");
  assert.equal(snapshot.messages[0].messageId, "m-u-0");

  const transcript = await store.readTurns(THREAD, { turnLimit: 100 });
  assert.equal(transcript.turns.length, 25);
  assert.equal(transcript.turns[0].items.length, 2);
  assert.equal(transcript.turns.at(-1).items.at(-1).text, "回答 24");
  assert.equal(transcript.thread.messageCount, 50);
  assert.equal(transcript.thread.sessionId, null);

  // This is the /threads source.
  const list = await store.list({ limit: 10 });
  assert.equal(list.length, 1);
  assert.equal(list[0].id, THREAD);
  assert.equal(list[0].turns, 25);
  assert.equal(list[0].messageCount, 50);
  assert.equal(list[0].lastText, "回答 24");
  assert.equal(list[0].source, "bridge-store");
});

test("a new store instance on the same directory reads the same history after restart", async t => {
  const { root, store } = await fixture(t);
  await store.create({ threadId: THREAD, cwd: "/tmp/workspace", source: "http", title: "重启" });
  for (let i = 0; i < 5; i++) {
    await store.appendMessage(THREAD, { role: "user", text: `u${i}`, source: "http" });
    await store.appendMessage(THREAD, { role: "assistant", text: `a${i}`, turnId: `t${i}`, source: "http" });
  }
  await store.setMeta(THREAD, { sessionId: "99999999-8888-4777-8666-555555555555" });

  const reopened = await new SessionStore(path.join(root, "history"), { logger: () => {} }).init();
  const snapshot = await reopened.read(THREAD, { limit: 100 });
  assert.equal(snapshot.ok, true);
  assert.equal(snapshot.totalMessages, 10);
  assert.equal(snapshot.meta.sessionId, "99999999-8888-4777-8666-555555555555");
  assert.equal((await reopened.list()).length, 1);
  assert.equal((await reopened.readTurns(THREAD)).turns.length, 5);
});

test("store files are mode 600 and live under the bridge state dir", async t => {
  const { root, store } = await fixture(t);
  await store.create({ threadId: THREAD, source: "feishu" });
  await store.appendMessage(THREAD, { role: "user", text: "hi", source: "feishu" });
  const file = path.join(root, "history", `${THREAD}.jsonl`);
  assert.equal((await stat(file)).mode & 0o777, 0o600);
  const text = await readFile(file, "utf8");
  assert.equal(text.split("\n").filter(Boolean).length, 2);
  assert.equal((await readdir(path.join(root, "history"))).some(name => name.endsWith(".tmp")), false);
});

test("missing, empty and corrupt stores report a precise reason instead of fake history", async t => {
  const { root, store } = await fixture(t);
  assert.equal((await store.read("cli-00000000-0000-4000-8000-000000000000")).reason, "session_store_missing");

  await store.create({ threadId: THREAD, source: "feishu" });
  const file = path.join(root, "history", `${THREAD}.jsonl`);
  await writeFile(file, "not json\n{broken\n");
  const corrupt = await store.read(THREAD);
  assert.equal(corrupt.ok, false);
  assert.match(corrupt.reason, /^session_store_corrupt:/);

  await writeFile(file, "");
  assert.equal((await store.read(THREAD)).reason, "session_store_corrupt:meta_missing");
});

test("appendMessage creates the session on demand without deadlocking", async t => {
  const { store } = await fixture(t);
  await store.appendMessage(THREAD, { role: "user", text: "first", source: "feishu" });
  const snapshot = await store.read(THREAD);
  assert.equal(snapshot.ok, true);
  assert.equal(snapshot.totalMessages, 1);
  assert.equal(snapshot.meta.source, "feishu");
});

test("groupTurns keeps a user message and its assistant replies in one turn", () => {
  const turns = groupTurns([
    { role: "user", text: "q1", ts: "t1", messageId: "u1" },
    { role: "assistant", text: "a1", ts: "t2", messageId: "a1", turnId: "T1" },
    { role: "assistant", text: "a1b", ts: "t3", messageId: "a1b", turnId: "T1" },
    { role: "user", text: "q2", ts: "t4", messageId: "u2" },
  ]);
  assert.equal(turns.length, 2);
  assert.equal(turns[0].id, "T1");
  assert.equal(turns[0].items.length, 3);
  assert.equal(turns[1].items.length, 1);
});

test("invalid thread ids are rejected before touching the filesystem", async t => {
  const { root, store } = await fixture(t);
  await assert.rejects(() => store.create({ threadId: "../escape", source: "http" }), /invalid thread id/);
  const files = await readdir(path.join(root, "history"));
  assert.deepEqual(files, []);
});
