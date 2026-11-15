import test from "node:test";
import assert from "node:assert/strict";
import { InProcessTransport } from "../interface/inprocess.mjs";
import { installSessionRoleRecovery, skipInvalidMessageRoles } from "../interface/session-recovery.mjs";
import { createRequire } from "node:module";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { dshFixture } from "./fixtures/dsh-harness.mjs";

const bad = (seq, role = "system") => ({ type: "user/message", seq, time: 1000 + seq,
  data: { id: "message-" + seq, role, content: [{ type: "text", text: "private policy" }] } });
const good = { ...bad(0, "user"), data: { ...bad(0, "user").data, content: [{ type: "text", text: "human" }] } };

test("DSH skips a system-role user/message and warns while retaining event coordinates", () => {
  const input = [good, { ...bad(1), surfaceOp: { type: "append" } }, { type: "turn/end", seq: 2, time: 1002, data: {} }];
  const warnings = [];
  const events = skipInvalidMessageRoles("original", input, (...args) => warnings.push(args));
  assert.equal(events.length, input.length);
  assert.equal(events[0], good);
  assert.equal(events[2], input[2]);
  assert.equal(input[1].type, "user/message");
  assert.deepEqual(events[1], { type: "dshworker/recovery-skipped", seq: 1, time: 1001,
    ignorable: true, data: { originalType: "user/message", expectedRole: "user", actualRole: "system" } });
  assert.deepEqual(warnings, [["warn", "session_role_skipped", {
    sessionId: "original", seq: 1, eventType: "user/message", expectedRole: "user", actualRole: "system" }]]);
  assert.doesNotMatch(JSON.stringify(events) + JSON.stringify(warnings), /private policy/);
});

test("unknown and missing roles are skipped, healthy inputs retain identity", () => {
  const warnings = [];
  const healthy = [good];
  assert.equal(skipInvalidMessageRoles("s", healthy, assert.fail), healthy);
  const records = [bad(0, "future-role"), bad(1, null)];
  assert.equal(skipInvalidMessageRoles("s", records, (...args) => warnings.push(args))
    .filter(event => event.ignorable).length, 2);
  assert.equal(warnings.length, 2);
});

test("native resume succeeds with a mixed-role log and warns before strict adoption", async () => {
  const warnings = [];
  const parsed = { meta: { id: "original" }, events: [good, bad(1)], recoveredTail: [bad(2, "alien")], tornTruncateTo: 8192 };
  const persistence = { async readZstdPrefix() { return parsed; } };
  const sessions = { prepare(id, { seed }) {
    for (const event of seed) if (event.type === "user/message") assert.equal(event.data.role, "user");
    return { id, events: seed };
  } };
  const registry = { async resume({ resumeSessionId }) {
    const decoded = await persistence.readZstdPrefix();
    assert.equal(decoded.tornTruncateTo, 8192);
    assert.equal(decoded.recoveredTail[0].ignorable, true);
    return { agent: { session: sessions.prepare(resumeSessionId, { seed: decoded.events }) } };
  } };
  const ctx = { get: name => ({ sessions, sessionPersistence: persistence, agents: registry })[name] };
  const restore = installSessionRoleRecovery(ctx, (...args) => warnings.push(args));
  try {
    const transport = Object.create(InProcessTransport.prototype);
    Object.assign(transport, { ctx, live: new Map(), selection: {}, log() {}, presetSetup: async () => null });
    const agent = await transport.ensureAgent({ id: "main", sessionId: "original" });
    assert.equal(agent.session.id, "original");
    assert.equal(agent.session.events[1].ignorable, true);
    assert.equal(warnings.length, 2);
    assert.equal(parsed.events[1].data.role, "system");
  } finally { restore(); }
});

test("prepare recovery and overlapping plugin unload preserve the native methods", () => {
  const original = function (id, options) { return options.seed; };
  const sessions = { prepare: original }, ctx = { get: name => name === "sessions" ? sessions : null };
  const first = [], second = [];
  const stop1 = installSessionRoleRecovery(ctx, (...args) => first.push(args));
  const stop2 = installSessionRoleRecovery(ctx, (...args) => second.push(args));
  stop1();
  assert.equal(sessions.prepare("s", { seed: [bad(0)] })[0].ignorable, true);
  assert.equal(first.length, 0); assert.equal(second.length, 1);
  stop2();
  assert.equal(sessions.prepare, original);
});

test("peer guidance uses native system assembly without appending a user message", async t => {
  const f = await dshFixture(t);
  await f.send("human input");
  f.transport.threads.get(f.threadId).turns[0].nativePeerMessage = { from: "Hermes", messageId: "peer-test" };
  assert.equal(f.listeners.has("agent/pre-step"), false);
  const [assemble] = f.listeners.get("system-prompt/assemble");
  const before = f.records.length;
  const initial = { sections: [{ name: "existing", text: "keep" }] };
  const first = await assemble(null, { agent: f.agent }, async () => initial);
  const second = await assemble(null, { agent: f.agent }, async () => first);
  assert.equal(second.sections.filter(section => section.name === "worker-chat-policy").length, 1);
  assert.equal(second.sections[0].text, "keep");
  assert.match(second.sections[1].text, /Hermes/);
  assert.equal(f.records.length, before);
  await f.finish();
});

test("native V4 persistence restores mixed system and unknown roles with warnings", {
  skip: !process.env.DSH_NATIVE_TEST_ROOT && "Set DSH_NATIVE_TEST_ROOT to the installed DSH package root",
}, async t => {
  const require = createRequire(path.join(process.env.DSH_NATIVE_TEST_ROOT, "native-test.mjs"));
  const { Context } = await import(require.resolve("@deepseek-ai/cordis"));
  const { SessionStore } = await import(require.resolve("@deepseek-ai/dsh-session"));
  const persistencePlugin = await import(require.resolve("@deepseek-ai/dsh-session-persistence-jsonl"));
  const { sessionFormatCatalog } = await import(require.resolve("@deepseek-ai/dsh-session-format-catalog"));
  const { zstdCompressSync } = await import("node:zlib");
  const root = await mkdtemp(path.join(os.tmpdir(), "dsh-native-role-test-"));
  const ctx = new Context(), store = ctx.plugin(SessionStore);
  const fiber = ctx.plugin(persistencePlugin, { root });
  let stop;
  t.after(async () => { stop?.(); await fiber.dispose(); await store.dispose(); await rm(root, { recursive: true, force: true }); });
  await fiber.await();
  const id = "session-role-test", dir = path.join(root, "_no-cwd", id);
  await mkdir(dir, { recursive: true });
  const header = { type: "session", version: 4, id, createdAt: 1, isSeeded: false, delegationDepth: 0 };
  const events = [good, bad(1), bad(2, "future-role"), { ...good, seq: 3, data: { ...good.data, id: "last-human" } }]
    .map(event => sessionFormatCatalog.encodeCurrentEvent({ ...event, surfaceOp: "append",
      data: { ...event.data, source: { kind: "user" } } }));
  const bytes = Buffer.concat([zstdCompressSync(JSON.stringify(header) + "\n"),
    zstdCompressSync(events.map(event => JSON.stringify(event) + "\n").join(""))]);
  const filename = path.join(dir, "session.v4.jsonl.zstd");
  await writeFile(filename, bytes);
  const persistence = ctx.get("sessionPersistence");
  await assert.rejects(async () => {
    const handle = await persistence.open(id, "read");
    try { await handle.read(0); } finally { await handle.close(); }
  }, /must have role "user"/);
  const warnings = [];
  stop = installSessionRoleRecovery(ctx, (...args) => warnings.push(args));
  const handle = await persistence.open(id, "read");
  try {
    const decoded = await handle.read(0);
    const session = ctx.get("sessions").prepare(id, { seed: decoded.events,
      meta: structuredClone(handle.header), inheritedEventCount: handle.inheritedEventCount,
      eventState: decoded.eventState });
    assert.equal(session.id, id); assert.equal(decoded.events.length, 4);
    assert.equal(session.eventAt(3).data.id, "last-human");
    assert.equal(session.eventAt(1).type, "dshworker/recovery-skipped");
    assert.equal(session.eventAt(2).type, "dshworker/recovery-skipped");
    assert.deepEqual(warnings.map(([, , fields]) => fields.actualRole), ["system", "future-role"]);
    assert.deepEqual(await readFile(filename), bytes);
  } finally { await handle.close(); }
  for (const [index, type] of ["user/message", "system/message"].entries()) {
    const nextId = "early-role-" + index, nextDir = path.join(root, "_no-cwd", nextId);
    await mkdir(nextDir, { recursive: true });
    const raw = { ...events[0], seq: 0,
      data: { ...events[0].data, role: "developer" } };
    if (type === "system/message") {
      raw.type = type;
      raw.data = { turn: 1, step: 1, message: { ...events[0].data, role: "future-role" } };
    }
    const image = Buffer.concat([zstdCompressSync(JSON.stringify({ ...header, id: nextId }) + "\n"),
      zstdCompressSync(JSON.stringify(raw) + "\n")]);
    const nextFile = path.join(nextDir, "session.v4.jsonl.zstd");
    await writeFile(nextFile, image);
    const nextHandle = await persistence.open(nextId, "read");
    try {
      const decoded = await nextHandle.read(0);
      const session = ctx.get("sessions").prepare(nextId, { seed: decoded.events,
        meta: structuredClone(nextHandle.header), inheritedEventCount: nextHandle.inheritedEventCount,
        eventState: decoded.eventState });
      assert.equal(session.id, nextId);
      assert.equal(session.eventAt(0).type, "dshworker/recovery-skipped");
      assert.equal(decoded.events[0].data.originalType, type);
      assert.equal(decoded.tornTruncateTo, undefined);
      assert.deepEqual(await readFile(nextFile), image);
    } finally { await nextHandle.close(); }
  }
  assert.deepEqual(warnings.slice(2).map(([, , fields]) => fields.actualRole), ["developer", "future-role"]);
});
