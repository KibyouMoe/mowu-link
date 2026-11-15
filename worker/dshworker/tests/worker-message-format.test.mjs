import assert from "node:assert/strict";
import test from "node:test";
import http from "node:http";
import { DesktopManager } from "../desktop-manager.mjs";
import { peerPresentation, WORKER_CHAT_POLICY } from "../worker-message-format.mjs";

test("peer display retains sender and body while reply policy uses ordinary inbox metadata", () => {
  assert.equal(peerPresentation({ from: "玉藻", to: "小栗帽", text: "收到" }, "now").displayText, "来自 玉藻：\n收到");
  assert.match(WORKER_CHAT_POLICY, /view=history/);
  assert.match(WORKER_CHAT_POLICY, /Do not automatically acknowledge or reply to a reply/);
});

test("removed collaboration routes serve no client page or peer history", async t => {
  const manager = new DesktopManager({ native: async () => assert.fail("page removal must not invoke native tools"),
    stateDir: ".", config: { token: "test-manager-token", host: "127.0.0.1", port: 0 } });
  const server = http.createServer((req, res) => { void manager.handle(req, res); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  for (const route of ["/collaboration/", "/collaboration/records", "/bridge/collaboration"])
    assert.equal((await fetch(base + route, { headers: { authorization: "Bearer test-manager-token" } })).status, 404);
});
