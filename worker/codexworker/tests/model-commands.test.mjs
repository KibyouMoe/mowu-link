import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { ModelCommands } from "../model-commands.mjs";

async function fixture(t) {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "worker-model-"));
  t.after(() => rm(stateDir, { recursive: true, force: true }));
  const efforts = Array.from({ length: 13 }, (_, index) => ({ id: "effort-" + index, name: "力度 " + index }));
  const models = Array.from({ length: 23 }, (_, index) => ({ id: "model-" + index, name: "模型 " + index,
    efforts, defaultEffort: "effort-0" }));
  const current = { main: { model: "model-0", effort: "effort-1" }, old: { model: "model-3", effort: "effort-2" } };
  const calls = [];
  const manager = { stateDir, state: { sessions: { main: {}, old: {} }, selectedThreadId: "main" },
    async call(name, args) {
      assert.equal(name, "model_settings"); calls.push(args);
      if (args.action === "update") {
        if (manager.rejectUpdate) throw new Error("known native rejection");
        current[args.threadId] = { model: args.model, effort: args.effort };
        if (manager.loseResponse) { manager.loseResponse = false; throw new Error("response lost"); }
      }
      return { models, current: structuredClone(current[args.threadId]), defaultSelection: { model: "model-1", effort: "effort-0" } };
    } };
  const service = new ModelCommands(manager);
  const input = fields => ({ kind: "model", action: "list", threadId: "main", messageId: randomUUID(), ...fields });
  return { service, manager, current, calls, input, stateDir,
    stored: async () => JSON.parse(await readFile(service.file, "utf8")) };
}

test("model catalog uses 10 rows, exact current/config tags, and persistent defaults", async t => {
  const f = await fixture(t);
  const first = await f.service.run(f.input());
  assert.equal(first.items.length, 10); assert.equal(first.pages, 3);
  assert.deepEqual(first.items[0].sources, ["current"]);
  assert.deepEqual(first.items[1].sources, ["config"]);
  const nextInput = f.input({ direction: 1 });
  assert.equal((await f.service.run(nextInput)).page, 1);
  assert.equal((await f.service.run(nextInput)).page, 1);
  assert.equal((await f.service.run(f.input({ direction: 1 }))).items.length, 3);
  assert.equal((await f.service.run(f.input({ direction: -1 }))).page, 1);
  assert.deepEqual((await f.stored()).defaults, { model: "model-1", effort: "effort-0" });
  assert.deepEqual(f.current.old, { model: "model-3", effort: "effort-2" });
  assert.equal(f.calls.some(call => call.action === "update"), false);
});

test("model and effort setters update the exact session and worker defaults", async t => {
  const f = await fixture(t);
  await f.service.run(f.input({ action: "set", value: "模型 4" }));
  assert.deepEqual(f.current.main, { model: "model-4", effort: "effort-1" });
  assert.deepEqual((await f.stored()).defaults, { model: "model-4", effort: "effort-0" });
  await f.service.run(f.input({ kind: "effort", action: "set", value: "effort-2" }));
  assert.deepEqual((await f.stored()).defaults, { model: "model-4", effort: "effort-2" });
  const listed = await f.service.run(f.input({ kind: "effort" }));
  assert.deepEqual(listed.items[2].sources, ["current", "config"]);
  assert.equal((await f.service.run(f.input({ kind: "effort", direction: 1 }))).items.length, 3);
  assert.deepEqual(f.current.old, { model: "model-3", effort: "effort-2" });
});

test("serialized commands preserve both settings through concurrent callers", async t => {
  const f = await fixture(t);
  await Promise.all([f.service.run(f.input({ action: "set", value: "model-6" })),
    f.service.run(f.input({ kind: "effort", action: "set", value: "effort-3" }))]);
  assert.deepEqual((await f.stored()).defaults, { model: "model-6", effort: "effort-3" });
  assert.deepEqual((await f.stored()).defaults, f.current.main);
});

test("unavailable values and conflicting retries reject without changing native selection", async t => {
  const f = await fixture(t), request = f.input();
  await f.service.run(request);
  await assert.rejects(f.service.run({ ...request, kind: "effort" }), error => error.status === 409);
  await assert.rejects(f.service.run(f.input({ action: "set", value: "missing" })), error => error.status === 422);
  await assert.rejects(f.service.run(f.input({ page: 90 })), error => error.status === 400);
  assert.equal(f.calls.some(call => call.action === "update"), false);
});

test("known native rejection clears its intent and preserves previous defaults", async t => {
  const f = await fixture(t); await f.service.run(f.input());
  f.manager.rejectUpdate = true;
  await assert.rejects(f.service.run(f.input({ action: "set", value: "model-7" })), /known native rejection/);
  assert.equal((await f.stored()).pending, undefined);
  assert.deepEqual((await f.stored()).defaults, { model: "model-1", effort: "effort-0" });
});

test("a lost native response is reconciled after service restart before new defaults are used", async t => {
  const f = await fixture(t); f.manager.loseResponse = true;
  await assert.rejects(f.service.run(f.input({ action: "set", value: "model-8" })), /response lost/);
  assert.equal((await f.stored()).pending.selection.model, "model-8");
  const restarted = new ModelCommands(f.manager);
  assert.deepEqual(await restarted.defaults("main"), { model: "model-8", effort: "effort-0" });
  assert.equal((await f.stored()).pending, undefined);
  assert.equal(f.calls.filter(call => call.action === "update").length, 1);
});

test("invalid existing worker configuration is preserved and reported", async t => {
  const f = await fixture(t); await writeFile(f.service.file, "{bad json");
  await assert.rejects(f.service.run(f.input()), error => error.status === 503);
  assert.equal(await readFile(f.service.file, "utf8"), "{bad json");
});

test("recovery preserves a later Desktop model change after a lost response", async t => {
  const f = await fixture(t); f.manager.loseResponse = true;
  await assert.rejects(f.service.run(f.input({ action: "set", value: "model-8" })), /response lost/);
  f.current.main = { model: "model-9", effort: "effort-3" };
  const restarted = new ModelCommands(f.manager);
  await assert.rejects(restarted.defaults("main"), error => error.status === 409);
  assert.deepEqual(f.current.main, { model: "model-9", effort: "effort-3" });
  assert.equal((await f.stored()).pending, undefined);
  assert.equal(f.calls.filter(call => call.action === "update").length, 1);
});
