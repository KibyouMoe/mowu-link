import assert from "node:assert/strict";
import test from "node:test";
import path from "node:path";
import os from "node:os";
import { mkdtemp, mkdir, readFile, writeFile, rename, rm } from "node:fs/promises";
import { recoverPrimarySession } from "../primary-session.mjs";

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "worker-primary-session-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const sessionPath = path.join(root, "cli-session.json"), calls = [], logs = [], waits = [];
  const write = async (file, value) => {
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file + ".tmp", JSON.stringify(value) + "\n", { mode: 0o600 });
    await rename(file + ".tmp", file);
  };
  const options = { sessionPath, cwd: root, display: "test", workerName: "test-worker",
    writeJsonAtomic: write, log: (...args) => logs.push(args),
    nativeCall: async (name, args) => { calls.push({ name, args });
      if (name === "read_thread") return { thread: { id: args.threadId, cwd: root } };
      return { threadId: "first-thread", thread: { cwd: root } };
    }, sleep: async ms => { waits.push(ms); } };
  const saved = { threadId: "original-thread", cwd: root };
  const save = async value => writeFile(sessionPath, JSON.stringify(value ?? saved));
  const run = extra => recoverPrimarySession({ ...options, ...extra });
  const status = async () => JSON.parse(await readFile(path.join(root, "session-recovery.json"), "utf8"));
  return { root, sessionPath, calls, logs, waits, options, saved, save, run, status, write };
}
test("existing primary thread is reused without changing the primary state bytes", async t => {
  const f = await fixture(t); await f.save();
  const before = await readFile(f.sessionPath);
  assert.deepEqual(await f.run(), f.saved);
  assert.deepEqual(await readFile(f.sessionPath), before);
  assert.deepEqual(f.calls.map(c => c.name), ["read_thread"]);
  assert.equal((await f.status()).state, "ready");
});
test("temporary read failures back off against only the original id, then recover", async t => {
  const f = await fixture(t); await f.save(); let tries = 0;
  const result = await f.run({ nativeCall: async (name, args) => {
    f.calls.push({ name, args });
    if (++tries <= 5) throw Object.assign(new Error("temporary"), { code: "ECONNRESET" });
    return { thread: { id: args.threadId, cwd: f.root } };
  } });
  assert.equal(result.threadId, f.saved.threadId);
  assert.deepEqual(f.waits, [1000, 2000, 4000, 8000, 16000]);
  assert.ok(f.calls.every(c => c.name === "read_thread" && c.args.threadId === "original-thread"));
  assert.equal(f.logs.filter(x => x[1] === "primary_session_retrying").length, 5);
});
test("retry delay is capped and does not create a new thread after prolonged recovery", async t => {
  const f = await fixture(t); await f.save(); let attempts = 0;
  await f.run({ retryInitialMs: 10, retryMaxMs: 25, nativeCall: async (name, args) => {
    assert.equal(name, "read_thread"); assert.equal(args.threadId, f.saved.threadId);
    if (++attempts <= 6) throw Error("offline");
    return { thread: { id: args.threadId, cwd: f.root } };
  } });
  assert.deepEqual(f.waits, [10, 20, 25, 25, 25, 25]);
});
for (const invalid of ["{broken", "", "null", "{}", '{"threadId":""}', '{"threadId":" x "}', '{"threadId":42}']) {
  test("invalid primary state is preserved and cannot trigger create: " + JSON.stringify(invalid), async t => {
    const f = await fixture(t); await writeFile(f.sessionPath, invalid);
    await assert.rejects(f.run(), e => e.code === "primary_session_corrupt");
    assert.equal(await readFile(f.sessionPath, "utf8"), invalid); assert.equal(f.calls.length, 0);
    assert.equal((await f.status()).state, "blocked");
  });
}
test("a primary-state read error is not mistaken for a missing first-install file", async t => {
  const f = await fixture(t); await mkdir(f.sessionPath);
  await assert.rejects(f.run(), e => e.code === "primary_session_unreadable");
  assert.equal(f.calls.length, 0);
});
for (const artifact of ["manager-binding.json", "desktop-manager-state.json"]) {
  test("missing primary file with existing " + artifact + " blocks fresh creation", async t => {
    const f = await fixture(t); await writeFile(path.join(f.root, artifact), "{}");
    await assert.rejects(f.run(), e => e.code === "primary_session_missing");
    assert.equal(f.calls.length, 0);
  });
}
test("mismatching native thread id never replaces the saved id", async t => {
  const f = await fixture(t); await f.save();
  await assert.rejects(f.run({ nativeCall: async () => ({ thread: { id: "foreign-thread", cwd: f.root } }) }),
    e => e.code === "primary_session_mismatch");
  assert.deepEqual(JSON.parse(await readFile(f.sessionPath, "utf8")), f.saved);
});
test("changed native cwd fails closed instead of creating a thread in another workspace", async t => {
  const f = await fixture(t); await f.save();
  await assert.rejects(f.run({ nativeCall: async () => ({ thread: { id: f.saved.threadId, cwd: f.root + "-other" } }) }),
    e => e.code === "primary_session_cwd_mismatch");
  assert.deepEqual(JSON.parse(await readFile(f.sessionPath, "utf8")), f.saved);
});
test("launch cwd may change while the saved primary workspace remains unchanged", async t => {
  const f = await fixture(t); await f.save();
  assert.deepEqual(await f.run({ cwd: f.root + "-new-launch-dir" }), f.saved);
});
test("permanently missing native thread is reported and never auto-replaced", async t => {
  const f = await fixture(t); await f.save();
  await assert.rejects(f.run({ nativeCall: async () => { throw Object.assign(Error("gone"), { code: "thread_not_found" }); } }),
    e => e.code === "primary_session_unavailable");
  assert.deepEqual(f.waits, []); assert.equal((await f.status()).threadId, "original-thread");
});
test("repeated process starts continue the same durable session", async t => {
  const f = await fixture(t); await f.save();
  for (let i = 0; i < 3; i++) assert.deepEqual(await f.run(), f.saved);
  assert.ok(f.calls.every(c => c.name === "read_thread"));
});
test("only a truly fresh installation creates and saves one primary thread", async t => {
  const f = await fixture(t);
  const session = await f.run();
  assert.equal(session.threadId, "first-thread");
  assert.equal(f.calls.filter(c => c.name === "create_thread").length, 1);
  assert.deepEqual(await f.run(), session);
  assert.equal(f.calls.filter(c => c.name === "create_thread").length, 1);
});
test("unconfirmed first creation cannot create again on the next process start", async t => {
  const f = await fixture(t); let writes = 0;
  const nativeCall = async name => { assert.equal(name, "create_thread"); writes++; throw Error("lost response"); };
  await assert.rejects(f.run({ nativeCall }), e => e.code === "primary_session_create_unconfirmed");
  await assert.rejects(f.run({ nativeCall }), e => e.code === "primary_session_missing");
  assert.equal(writes, 1);
});
test("failed primary-file save recovers the exact created thread from its durable receipt", async t => {
  const f = await fixture(t); let failOnce = true;
  await assert.rejects(f.run({ writeJsonAtomic: async (file, value) => {
    if (file === f.sessionPath && failOnce) { failOnce = false; throw Error("disk full"); }
    await f.write(file, value);
  } }), e => e.code === "primary_session_persist_failed");
  assert.equal((await f.status()).threadId, "first-thread");
  assert.equal((await f.run()).threadId, "first-thread");
  assert.equal(f.calls.filter(c => c.name === "create_thread").length, 1);
});
test("missing primary file is restored from a verified exact-worker recovery receipt", async t => {
  const f = await fixture(t); await f.save(); await f.run(); await rm(f.sessionPath);
  assert.deepEqual(await f.run(), f.saved);
  assert.deepEqual(JSON.parse(await readFile(f.sessionPath, "utf8")), f.saved);
  assert.ok(f.calls.every(c => c.name === "read_thread"));
});
test("another worker's recovery receipt never seeds a new primary thread", async t => {
  const f = await fixture(t);
  await f.write(path.join(f.root, "session-recovery.json"), { ...f.saved, workerName: "other-worker" });
  await assert.rejects(f.run(), e => e.code === "primary_session_missing"); assert.equal(f.calls.length, 0);
});
test("corrupt primary-file reporting preserves the last known recovery id", async t => {
  const f = await fixture(t); await f.save(); await f.run();
  await writeFile(f.sessionPath, "{broken");
  await assert.rejects(f.run(), e => e.code === "primary_session_corrupt");
  assert.equal((await f.status()).threadId, f.saved.threadId);
});
test("shutdown interrupts an in-flight native read and keeps the primary file intact", async t => {
  const f = await fixture(t); await f.save(); const abort = new AbortController(); let entered;
  const started = new Promise(resolve => { entered = resolve; });
  const running = f.run({ signal: abort.signal, nativeCall: async () => {
    entered(); return new Promise(() => {});
  } });
  await started; abort.abort(new Error("shutdown"));
  await assert.rejects(running, /shutdown/);
  assert.equal((await f.status()).state, "interrupted");
  assert.deepEqual(JSON.parse(await readFile(f.sessionPath, "utf8")), f.saved);
});
test("shutdown during retry never reaches a fresh-create path", async t => {
  const f = await fixture(t); await f.save(); const abort = new AbortController();
  await assert.rejects(f.run({ signal: abort.signal, nativeCall: async () => { throw Error("offline"); },
    sleep: async () => { abort.abort(new Error("shutdown")); } }), /shutdown/);
  assert.equal((await f.status()).state, "interrupted");
  assert.deepEqual(JSON.parse(await readFile(f.sessionPath, "utf8")), f.saved);
});
