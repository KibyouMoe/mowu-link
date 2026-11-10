import assert from "node:assert/strict";
import test from "node:test";
import { resumeCandidates } from "../resume-candidates.mjs";
const task = (id, updatedAt = 1, extra = {}) => ({ id, kind: "codex", hostId: "local", cwd: "allowed", title: id, updatedAt, ...extra });
const allowed = value => value.cwd === "allowed";

test("reactivated old conversations use recency, not creation time or assistant background updates", async () => {
  const listing = { threads: [task("old", 900, { createdAt: 1, recencyAt: 300 }), task("new", 1000, { createdAt: 200, recencyAt: 200 })] };
  const options = { allowed, readThread: async () => { throw new Error("no history needed"); } };
  assert.deepEqual((await resumeCandidates(listing, options)).tasks.map(t => t.id), ["old", "new"]);
  listing.threads[1].updatedAt = 999999;
  assert.equal((await resumeCandidates(listing, options)).tasks[0].id, "old");
  assert.equal((await resumeCandidates(listing, { ...options, activity: { new: { at: 400 } } })).tasks[0].id, "new");
});

test("local catalog supplies section metadata without reading an oversized turn and preserves native titles", async () => {
  let reads = 0;
  const result = await resumeCandidates({ threads: [task("visible", 900, { title: "原生标题" })], sections: [{ itemKeys: ["codex:thread:local:huge"] }] },
    { allowed, localTasks: [task("visible", 1, { title: "本地旧标题", recencyAt: 10 }), task("huge", 1, { recencyAt: 20 })],
      readThread: async () => { reads++; throw new Error("oversized"); } });
  assert.equal(reads, 0); assert.deepEqual(result.tasks.map(t => t.id), ["huge", "visible"]);
  assert.equal(result.tasks[1].title, "原生标题"); assert.equal(result.discovery.partial, false);
});

test("schema v4 section-only chats are resolved and sorted with top-level chats", async () => {
  const reads = [];
  const listing = { threads: [task("drawing", 10)], sections: [{ itemKeys: [
    "codex:thread:local:drawing", "codex:thread:local:game", "codex:thread:local:wiki", "codex:thread:local:game"] }] };
  const result = await resumeCandidates(listing, { allowed, readThread: async id => {
    reads.push(id); return { thread: task(id, id === "game" ? 30 : 20) };
  } });
  assert.deepEqual(result.tasks.map(t => t.id), ["game", "wiki", "drawing"]);
  assert.deepEqual(reads.sort(), ["game", "wiki"]); assert.equal(result.discovery.partial, false);
});

test("remote, cloud and project references never become local resume candidates", async () => {
  const reads = [];
  const listing = { threads: [task("remote", 100, { hostId: "other" }), task("cloud", 99, { kind: "chatgpt" })],
    sections: [{ itemKeys: ["codex:thread:local:remote", "codex:thread:local:cloud", "codex:thread:other:x",
      "codex:project:project", "chatgpt:conversation:chat", "codex:thread:local:outside", "codex:thread:local:wrong-host"] }] };
  const result = await resumeCandidates(listing, { allowed, readThread: async id => {
    reads.push(id); return { thread: task(id, 1, id === "outside" ? { cwd: "elsewhere" } : { hostId: "other" }) };
  } });
  assert.deepEqual(result.tasks, []); assert.deepEqual(reads.sort(), ["outside", "wrong-host"]);
});

test("unreadable or wrong-identity references report partial coverage without fabricated fallbacks", async () => {
  const result = await resumeCandidates({ threads: [task("current")], sections: [{ itemKeys: [
    "codex:thread:local:failed", "codex:thread:local:mismatch"] }] }, { allowed,
    readThread: async id => { if (id === "failed") throw new Error("private source error"); return { thread: task("different") }; } });
  assert.deepEqual(result.tasks.map(t => t.id), ["current"]); assert.equal(result.discovery.unavailable, 2);
  assert.equal(result.discovery.partial, true); assert.ok(!JSON.stringify(result).includes("private source error"));
});

test("top-level incomplete metadata is verified and timestamp units are comparable", async () => {
  const listing = { pinnedThreads: [task("seconds", 1700000002)], threads: [
    task("iso", "2023-11-14T22:13:21Z"), task("milliseconds", 1700000000000), task("missing-cwd", 1, { cwd: undefined })] };
  const result = await resumeCandidates(listing, { allowed, readThread: async id => ({ thread: task(id, 1700000003) }) });
  assert.deepEqual(result.tasks.map(t => t.id), ["missing-cwd", "seconds", "iso", "milliseconds"]);
});

test("large sections have bounded discovery and expose the deferred count", async () => {
  let reads = 0, active = 0, peak = 0;
  const listing = { sections: [{ itemKeys: Array.from({ length: 30 }, (_, i) => "codex:thread:local:chat-" + i) }] };
  const result = await resumeCandidates(listing, { allowed, readThread: async id => {
    reads++; peak = Math.max(peak, ++active); await new Promise(resolve => setImmediate(resolve)); active--; return { thread: task(id) };
  } });
  assert.equal(reads, 12); assert.ok(peak <= 4); assert.equal(result.discovery.deferred, 18); assert.equal(result.discovery.partial, true);
});
