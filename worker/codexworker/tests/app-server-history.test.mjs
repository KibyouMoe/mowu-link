import test from "node:test";
import assert from "node:assert/strict";
import {AppServerClient} from "../interface/transport/app-server.mjs";

test("large historical threads read only the requested turns and follow item cursors", async () => {
  const c = new AppServerClient(), calls = [];
  c.ensureStarted = async () => {};
  c.request = async (method, params) => {
    calls.push({method, params: {...params}});
    if (method === "thread/read") {
      assert.equal(params.includeTurns, false, "never download full image-bearing history");
      return {thread: {id: "original", cwd: "/work"}};
    }
    if (method === "thread/turns/list") {
      assert.equal(params.limit, 1); assert.equal(params.itemsView, "notLoaded");
      return {data: [{id: "latest", status: "completed", startedAt: 2}], nextCursor: "older-turns"};
    }
    assert.equal(method, "thread/items/list"); assert.equal(params.turnId, "latest");
    assert.equal(params.sortDirection, "asc"); assert.equal(params.limit, 25);
    return params.cursor ? {data: [{turnId: "latest", item: {id: "reply", type: "agentMessage", text: "restored"}}]} :
      {data: [{turnId: "latest", item: {id: "tool", type: "functionCallOutput", output: "private result"}}], nextCursor: "next-items"};
  };
  c.applyTurn("original", {id: "old", status: "completed", startedAt: 1, items: [{id: "old-image", result: "old image data"}]});
  const r = await c.readThread({threadId: "original", turnLimit: 1, includeOutputs: false});
  assert.equal(r.thread.id, "original"); assert.equal(r.page.nextCursor, "older-turns");
  assert.deepEqual(r.turns.map(t => t.id), ["latest"]);
  assert.equal(r.turns[0].items.at(-1).text, "restored");
  assert.equal(r.turns[0].items[0].output, undefined);
  assert.equal(calls.length, 4);
});

test("turn pagination keeps the native cursor and never reinserts unrelated cached turns", async () => {
  const c = new AppServerClient(); c.ensureStarted = async () => {};
  c.request = async (method, params) => {
    if (method === "thread/read") return {thread: {id: "original"}};
    if (method === "thread/turns/list") {
      assert.equal(params.cursor, "older-turns");
      return {data: [{id: "old", status: "completed", startedAt: 1}]};
    }
    return {data: []};
  };
  c.applyTurn("original", {id: "new", status: "inProgress", startedAt: 3});
  const r = await c.readThread({threadId: "original", turnLimit: 1, cursor: "older-turns"});
  assert.deepEqual(r.turns.map(t => t.id), ["old"]);
});

test("item pagination rejects repeated cursors instead of looping forever", async () => {
  const c = new AppServerClient(); let reads = 0;
  c.request = async () => {reads++; return {data: [], nextCursor: "repeat"};};
  await assert.rejects(c.readItemsFallback("original", 1000, "turn"), /repeated its cursor/);
  assert.equal(reads, 2);
});

test("item pages cannot silently merge another turn", async () => {
  const c = new AppServerClient();
  c.request = async () => ({data: [{turnId: "different", item: {id: "item"}}]});
  await assert.rejects(c.readItemsFallback("original", 1000, "turn"), /different turn/);
});

test("shared connection failures preserve the actual cause for recovery logs", async () => {
  const c = new AppServerClient(), child = {stdin: {write() {}}}; c.child = child;
  const pending = c.request("thread/read", {}, 1000);
  const cause = Object.assign(new Error("shared_app_server_message_too_large"), {code: "app_server_connection_failed"});
  c.handleExit(child, null, null, cause);
  await assert.rejects(pending, e => e.code === "app_server_connection_failed" && /message_too_large/.test(e.message));
});
