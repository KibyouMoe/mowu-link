import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { auditTextReceipts, matchingTextReceipt } from "../scripts/audit-text-receipts.mjs";
import { FeishuNotifier, markdownPayload } from "../feishu.mjs";

const criteria = { appId: "app", chatId: "chat", title: "回复", threadId: "thread", text: "exact public output", startMs: 1000, endMs: 5000 };
const item = () => ({ message_id: "om_test", msg_type: "interactive", chat_id: "chat", create_time: "3000",
  sender: { sender_type: "app", id_type: "app_id", id: "app" }, body: { content: markdownPayload(criteria.text, criteria.title, criteria.threadId) } });
test("receipt matching requires exact bot, target, public body, thread note and bounded time", () => {
  assert.equal(matchingTextReceipt(item(), criteria), true);
  for (const changed of [{ ...item(), chat_id: "other" }, { ...item(), create_time: "8000" }, { ...item(), sender: { ...item().sender, id: "other" } }, { ...item(), deleted: true }]) assert.equal(matchingTextReceipt(changed, criteria), false);
  assert.equal(matchingTextReceipt(item(), { ...criteria, text: "different" }), false);
});

async function fixture(t, items = []) {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "text-receipt-audit-")); t.after(() => rm(stateDir, { recursive: true, force: true }));
  const tokenFile = path.join(stateDir, "token"); await writeFile(tokenFile, "test-only-health-token-at-least-24-chars");
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ tokenFile, bridgeName: "test", machine: "codexworker", operatingMode: "feishu-local" }));
  const state = { selectedThreadId: "thread", selectionEpoch: 1, sessions: { thread: { seen: { key: { text: criteria.text } } } }, publicTextOutbox: {
    original: { uuid: "original", key: "key", threadId: "thread", bindingEpoch: 1, chatId: "chat", title: "回复", part: 0, status: "unknown", at: new Date(3000).toISOString() } } };
  const filename = path.join(stateDir, "desktop-manager-state.json"); await writeFile(filename, JSON.stringify(state)); const before = await readFile(filename);
  let lists = 0, now = 10000;
  const fetchImpl = async (url, opts) => {
    if (String(url).startsWith("http://127.0.0.1:")) return { ok: true, json: async () => ({ name: "test", machine: "codexworker", operatingMode: "feishu-local", integrationMode: "desktop-native" }) };
    assert.equal(opts.method, "GET"); assert.equal(new URL(url).searchParams.get("page_size"), "50"); lists++;
    return { ok: true, json: async () => ({ code: 0, data: { has_more: false, items } }) };
  };
  return { stateDir, filename, before, options: { stateDir, fetchImpl, notifier: { appId: "app", getToken: async () => "test-only-feishu-token" }, clock: () => now }, lists: () => lists, advance: () => { now += 300001; } };
}
test("empty history is not proof of non-delivery; audit never changes UUID/status and backs off", async t => {
  const f = await fixture(t); const first = await auditTextReceipts(f.options);
  assert.equal(first.reviews[0].outcome, "no_receipt_found_not_proof_of_failure"); assert.equal(first.reviews[0].status, "needs_review");
  await auditTextReceipts(f.options); assert.equal(f.lists(), 1);
  f.advance(); await auditTextReceipts(f.options); assert.equal(f.lists(), 2);
  assert.deepEqual(await readFile(f.filename), f.before); assert.equal(first.messagePosts, 0);
});
test("matching content cannot by itself mark the original UUID sent", async t => {
  const f = await fixture(t, [item()]); const result = await auditTextReceipts(f.options);
  assert.equal(result.reviews[0].outcome, "matching_content_requires_review"); assert.deepEqual(result.reviews[0].matches, ["om_test"]);
  assert.deepEqual(await readFile(f.filename), f.before);
});
test("trainer-abandoned unknown receipt stays unchanged and is excluded from repeated audits", async t => {
  const f = await fixture(t); const state = JSON.parse(await readFile(f.filename, "utf8"));
  state.publicTextOutbox.original.review = { decision: "abandon", actor: "trainer", uuid: "original", threadId: "thread", bindingEpoch: 1 };
  await writeFile(f.filename, JSON.stringify(state)); const before = await readFile(f.filename);
  assert.equal((await auditTextReceipts(f.options)).reviews.length, 0); assert.equal(f.lists(), 0);
  assert.deepEqual(await readFile(f.filename), before);
});
test("an existing original message ID can be checked directly without altering its unknown ledger", async t => {
  const f = await fixture(t); const state = JSON.parse(await readFile(f.filename, "utf8"));
  state.publicTextOutbox.original.feishuMessageId = "om_test"; await writeFile(f.filename, JSON.stringify(state)); const before = await readFile(f.filename);
  const originalFetch = f.options.fetchImpl;
  f.options.fetchImpl = async (url, opts) => {
    if (String(url).startsWith("http://127.0.0.1:")) return originalFetch(url, opts);
    assert.equal(opts.method, "GET"); assert.equal(new URL(url).pathname, "/open-apis/im/v1/messages/om_test");
    return { ok: true, json: async () => ({ code: 0, data: { items: [item()] } }) };
  };
  assert.equal((await auditTextReceipts(f.options)).reviews[0].outcome, "original_receipt_verified");
  assert.deepEqual(await readFile(f.filename), before);
});

function notifier() { const n = new FeishuNotifier({ appId: "app", appSecret: "secret", chatId: "chat" }); n.getToken = async () => "secret-token"; return n; }
test("new attempts persist bounded evidence and distinguish API rejection from missing transport response", async () => {
  for (const response of [{ code: 12345, msg: "PRIVATE RAW ERROR" }, { code: -1, msg: "PRIVATE RAW NETWORK ERROR" }, null]) {
    const n = notifier(), events = []; let writes = 0;
    n._postChunk = async () => { assert.equal(events.at(-1).phase, "dispatching"); writes++; return response; };
    assert.equal(await n.sendMessage("public", { uuid: "original", onDelivery: async e => events.push(e) }), null);
    assert.equal(writes, 1); assert.equal(events.at(-1).phase, response?.code === 12345 ? "rejected" : "unknown");
    assert.doesNotMatch(JSON.stringify(events), /PRIVATE|secret-token|public/);
  }
});
test("no-token outcome proves no message attempt and durable evidence failure prevents sending", async () => {
  const n = notifier(), events = []; let writes = 0; n._postChunk = async () => { writes++; return { code: 0 }; };
  n.getToken = async () => ""; await n.sendMessage("public", { onDelivery: async e => events.push(e) });
  assert.equal(writes, 0); assert.equal(events[0].reason, "token_unavailable");
  n.getToken = async () => "secret-token";
  await assert.rejects(n.sendMessage("public", { onDelivery: async () => { throw new Error("disk failure"); } }), /disk failure/);
  assert.equal(writes, 0);
});
test("successful chunk receipts retain original chunk UUID and real Feishu message ID", async () => {
  const n = notifier(), events = []; n._postChunk = async () => ({ code: 0, data: { message_id: "om_real_receipt" } });
  await n.sendMessage("public", { uuid: "original", onDelivery: async e => events.push(e) });
  assert.equal(events.at(-1).phase, "accepted"); assert.equal(events.at(-1).uuid, "original"); assert.equal(events.at(-1).messageId, "om_real_receipt");
});
