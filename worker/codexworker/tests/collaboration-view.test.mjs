import assert from "node:assert/strict";
import test from "node:test";
import http from "node:http";
import vm from "node:vm";
import { DesktopManager } from "../desktop-manager.mjs";
import { collaborationToken, collaborationUrl, collaborationSummary, peerPresentation, WORKER_CHAT_POLICY } from "../collaboration-view.mjs";
import { collaborationScript } from "../collaboration-assets.mjs";

test("read-only collaboration capability cannot read other manager routes or write messages", async t => {
  const manager = new DesktopManager({ native: async () => assert.fail("view must not invoke the model"),
    stateDir: ".", config: { token: "private-router-token", host: "127.0.0.1", port: 0 } });
  const png = Buffer.from("89504e470d0a1a0a00000000", "hex");
  let reads = 0;
  manager.workerMessages = {
    history: async () => { reads++; return [{ ...peerPresentation({ from: "玉藻", to: "小栗帽", text: "<script>bad()</script>" }, "2026-11-15T08:00:00Z"), attachments: [] }]; },
    readImage: async id => id === "a".repeat(64) ? { bytes: png, mediaType: "image/png" } : null,
  };
  const server = http.createServer((req, res) => { void manager.handle(req, res); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  manager.config.port = server.address().port;
  const base = `http://127.0.0.1:${server.address().port}`;
  const token = collaborationToken(manager);
  const headers = { authorization: `Bearer ${token}` };
  assert.equal(new URL(collaborationUrl(manager)).hash.slice(1), token);
  assert.notEqual(token, manager.config.token);
  assert.equal((await fetch(base + "/collaboration/records")).status, 401);
  assert.equal((await fetch(base + "/health", { headers })).status, 401);
  assert.equal((await fetch(base + "/manager/messages", { method: "POST", headers, body: "{}" })).status, 401);
  assert.equal((await fetch(base + "/collaboration/records", { method: "POST", headers })).status, 405);
  assert.equal(reads, 0);
  const page = await fetch(base + "/collaboration/");
  assert.equal(page.status, 200);
  assert.match(page.headers.get("content-security-policy"), /connect-src 'self'/);
  assert.ok(!(await page.text()).includes(manager.config.token));
  const records = await (await fetch(base + "/collaboration/records", { headers })).json();
  assert.equal(records.records[0].displayText, "来自 玉藻：<script>bad()</script>");
  const summary = await collaborationSummary(manager);
  assert.equal(summary.count, 1);
  assert.ok(!JSON.stringify(summary).includes("<script>"), "opening the client view must not replay public message bodies to the model");
  const image = await fetch(base + "/collaboration/images/" + "a".repeat(64), { headers });
  assert.equal(image.headers.get("content-type"), "image/png");
  assert.deepEqual(Buffer.from(await image.arrayBuffer()), png);
  assert.equal((await fetch(base + "/collaboration/images/" + "b".repeat(64), { headers })).status, 404);
});

test("collaboration renderer uses text nodes and preserves model-only loop policy", () => {
  assert.doesNotThrow(() => new vm.Script(collaborationScript));
  assert.match(collaborationScript, /text\.textContent = record\.displayText/);
  assert.ok(!collaborationScript.includes("innerHTML"));
  assert.match(WORKER_CHAT_POLICY, /Do not automatically acknowledge or reply to a reply/);
});
