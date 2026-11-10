import assert from "node:assert/strict";
import test from "node:test";
import { hydrateNativeInputs, readNativeWindow } from "../interface/native-history.mjs";
import { publicItems } from "../desktop-manager.mjs";
import { nativeToolRejection } from "../interface/native-dispatch.mjs";

const text = "飞书：第一段\n\n第二段完整保留";
const input = (id, name = "send_message_to_thread") => ({ type: "functionCallOutput", id, name });
const output = body => ({ text: `<codex_delegation><source_thread_id>source-chat</source_thread_id><input>${body}</input></codex_delegation>`, truncated: false });
const snapshot = (turns, nextCursor) => ({ thread: { id: "target-chat" }, page: { order: "newest_first", ...(nextCursor ? { nextCursor } : {}) }, turns });

test("opaque Desktop history rejection uses a bounded smaller read without pretending it is a size error", async () => {
  const rejection = nativeToolRejection("read_thread", { success: false, contentItems: [{ text: "Codex app tool request failed" }] });
  assert.equal(rejection.nativeErrorCode, "native_read_rejected"); assert.equal(rejection.nativeReachable, true);
  assert.equal(nativeToolRejection("send_message_to_thread", { success: false, contentItems: [{ text: "Codex app tool request failed" }] }).nativeErrorCode, undefined);
  const calls = [], windows = new Map(); let now = 0;
  const read = async args => {
    calls.push(args.turnLimit);
    if (args.turnLimit > 1) throw { body: { error: "native_read_rejected" } };
    return snapshot([{ id: "current", items: [{ type: "agentMessage", id: "reply", text: "完整的当前回复" }] }]);
  };
  const args = { threadId: "target-chat", turnLimit: 10, includeOutputs: false };
  const value = await readNativeWindow(read, args, windows, () => now);
  assert.deepEqual(calls, [10, 5, 2, 1]); assert.equal(value.nativeHistoryWindow.fallbackReason, "native_read_rejected");
  assert.equal(value.nativeHistoryWindow.limitedBySize, false); assert.equal(value.nativeHistoryWindow.degraded, true);
  assert.equal(publicItems(value)[0].text, "完整的当前回复");
  calls.length = 0; now = 1000; await readNativeWindow(read, args, windows, () => now); assert.deepEqual(calls, [1]);
  await assert.rejects(readNativeWindow(async () => { throw { body: { error: "native_read_rejected" } }; }, { ...args, turnLimit: 1 }));
});

test("history size windows shrink within a bound, cache failures, and retry widening after the deadline or a new head", async () => {
  const calls = [], windows = new Map(); let now = 0, allowed = 1, head = "head";
  const args = { threadId: "target-chat", turnLimit: 10, includeOutputs: false };
  const readPage = async request => {
    calls.push(request.turnLimit); assert.equal(request.includeOutputs, false);
    if (request.turnLimit > allowed) throw { body: { error: "native_snapshot_too_large" } };
    return snapshot([{ id: head, items: [] }]);
  };
  const first = await readNativeWindow(readPage, args, windows, () => now);
  assert.deepEqual(calls, [10, 5, 2, 1]); assert.equal(first.nativeHistoryWindow.limitedBySize, true);
  calls.length = 0; now = 10000; await readNativeWindow(readPage, args, windows, () => now);
  assert.deepEqual(calls, [1]);
  calls.length = 0; now = 31000; await readNativeWindow(readPage, args, windows, () => now);
  assert.deepEqual(calls, [10, 5, 2, 1]);
  calls.length = 0; now = 32000; allowed = 5; head = "new-head";
  const expanded = await readNativeWindow(readPage, args, windows, () => now);
  assert.deepEqual(calls, [1, 10, 5]); assert.equal(expanded.nativeHistoryWindow.readLimit, 5);
});

test("a single unreadable turn and actual transport failures are returned without inventing an empty history", async () => {
  const args = { threadId: "target-chat", turnLimit: 10, includeOutputs: false };
  let reads = 0;
  await assert.rejects(readNativeWindow(async () => { reads++; throw new Error("transport"); }, args));
  assert.equal(reads, 1); reads = 0;
  await assert.rejects(readNativeWindow(async () => { reads++; throw { body: { error: "native_snapshot_too_large" } }; }, args),
    error => error.body.error === "native_single_turn_too_large" && error.nativeHistoryWindow.blocked === true);
  assert.equal(reads, 4);
});

test("an oversized single turn backs off, probes one turn, and recovers when a new head is readable", async () => {
  const windows = new Map(), calls = []; let now = 0, readable = false;
  const read = async args => { calls.push(args.turnLimit); if (!readable || args.turnLimit > 1) throw { body: { error: "native_snapshot_too_large" } };
    return snapshot([{ id: "new-readable-turn", items: [] }]); };
  const args = { threadId: "target-chat", turnLimit: 10, includeOutputs: false };
  await assert.rejects(readNativeWindow(read, args, windows, () => now)); assert.deepEqual(calls, [10, 5, 2, 1]);
  now = 10000; await assert.rejects(readNativeWindow(read, args, windows, () => now)); assert.equal(calls.length, 4);
  now = 30000; await assert.rejects(readNativeWindow(read, args, windows, () => now)); assert.deepEqual(calls, [10, 5, 2, 1, 1]);
  now = 60000; readable = true; const recovered = await readNativeWindow(read, args, windows, () => now);
  assert.equal(recovered.turns[0].id, "new-readable-turn"); assert.equal(recovered.nativeHistoryWindow.blocked, undefined);
});

test("both Desktop history-size rejection formats retain reachable-channel status and a size-only failure code", () => {
  for (const message of ["Tool response exceeds the size limit. Request fewer turns or omit outputs.",
    "Thread history exceeds the read size limit. Try fewer turns. A single oversized turn cannot be read by this tool."]) {
    const failure = nativeToolRejection("read_thread", { success: false, contentItems: [{ text: message }] });
    assert.equal(failure.nativeReachable, true); assert.equal(failure.nativeErrorCode, "native_snapshot_too_large");
    assert.ok(!JSON.stringify(failure).includes(message));
  }
});

test("metadata hydration retains complete delegated inputs with native cursors and caches only public input outputs", async () => {
  const turns = [{ id: "new", items: [input("new-input"), { type: "agentMessage", id: "reply", text: "公开回复", phase: "final_answer" }] },
    { id: "old", items: [input("old-input", "create_thread")] }];
  const raw = structuredClone(turns); raw[0].items[0].output = output(text); raw[1].items[0].output = output("原始创建正文");
  raw[0].items.push({ type: "mcpToolCall", output: { text: "private tool output" } });
  const calls = [], cache = new Map();
  const readPage = async args => {
    calls.push(args); assert.equal(args.threadId, "target-chat"); assert.equal(args.turnLimit, 1); assert.equal(args.includeOutputs, true);
    assert.ok(args.cursor == null || args.cursor === "opaque-native-cursor");
    return args.cursor ? snapshot([raw[1]]) : snapshot([raw[0]], "opaque-native-cursor");
  };
  const hydrated = await hydrateNativeInputs(snapshot(structuredClone(turns)), readPage, cache);
  assert.deepEqual(publicItems(hydrated), publicItems(snapshot(raw)));
  assert.ok(!JSON.stringify(hydrated).includes("private tool output"));
  assert.ok(!JSON.stringify([...cache]).includes("private tool output"));
  assert.equal(calls.length, 2);
  const repeated = await hydrateNativeInputs(snapshot(structuredClone(turns)), readPage, cache);
  assert.deepEqual(publicItems(repeated), publicItems(hydrated)); assert.equal(calls.length, 2);
});

test("single-turn size fallback is bounded and keeps every paragraph rather than using truncated input", async () => {
  const calls = [], item = input("input");
  const hydrated = await hydrateNativeInputs(snapshot([{ id: "turn", items: [item] }]), async args => {
    calls.push(args.maxOutputCharsPerItem);
    if (args.maxOutputCharsPerItem === 20000) throw { body: { error: "native_snapshot_too_large" } };
    return snapshot([{ id: "turn", items: [{ ...item, output: output(text) }] }]);
  });
  assert.deepEqual(calls, [20000, 4000]);
  assert.equal(publicItems(hydrated)[0].text, text);
  await assert.rejects(hydrateNativeInputs(snapshot([{ id: "turn", items: [input("truncated")] }]), async () =>
    snapshot([{ id: "turn", items: [{ ...input("truncated"), output: { text: "incomplete", truncated: true } }] }])),
    error => error.body.error === "native_snapshot_incomplete");
});

test("hydration rejects another chat and repeated cursors without guessing input or issuing writes", async () => {
  const base = () => snapshot([{ id: "turn", items: [input("pending")] }]);
  await assert.rejects(hydrateNativeInputs(base(), async () => ({ thread: { id: "wrong-chat" }, turns: [] })),
    error => error.body.error === "native_snapshot_incomplete");
  let reads = 0;
  await assert.rejects(hydrateNativeInputs(base(), async () => { reads++; return snapshot([], "same-cursor"); }),
    error => error.body.error === "native_snapshot_incomplete");
  assert.equal(reads, 2);
});

test("request rejection and real transport errors are not multiplied by the size-only fallback", async () => {
  for (const failure of [new Error("transport unavailable"), { body: { error: "desktop_integration_unavailable" } }]) {
    let reads = 0;
    await assert.rejects(hydrateNativeInputs(snapshot([{ id: "turn", items: [input("pending")] }]), async () => { reads++; throw failure; }));
    assert.equal(reads, 1);
  }
});

test("hydration cache is bounded and ordinary tool results are never retained as public inputs", async () => {
  const items = Array.from({ length: 514 }, (_, i) => input(`input-${i}`));
  const cache = new Map();
  const hydrated = await hydrateNativeInputs(snapshot([{ id: "turn", items: structuredClone(items) }]), async () =>
    snapshot([{ id: "turn", items: items.map((item, i) => ({ ...item, output: i ? output("public") : { text: "private ordinary tool result", truncated: false } })) }]), cache);
  assert.equal(cache.size, 512); assert.equal(publicItems(hydrated).length, 513);
  assert.ok(!JSON.stringify(hydrated).includes("private ordinary tool result"));
});
