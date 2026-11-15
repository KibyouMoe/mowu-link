import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { startDshRouterRuntime } from "../interface/router-runtime.mjs";
import { getActiveRouterClient, setActiveRouterClient } from "../router-client.mjs";

async function until(predicate) {
  for (let attempt = 0; attempt < 300; attempt++) {
    if (predicate()) return;
    await delay(10);
  }
  assert.fail("DSH router fixture did not settle");
}

test("DSH joins the shared peer runtime with native approvals, control and explicit deadlines", async t => {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "dsh-peer-runtime-"));
  const tokenFile = path.join(stateDir, "router.token");
  await writeFile(tokenFile, "isolated-router-token");
  const keys = ["CODEXWORKER_ROUTER", "CODEXWORKER_ROUTER_URL", "CODEXWORKER_ROUTER_TOKEN_FILE"];
  const env = keys.map(key => [key, process.env[key]]);
  for (const key of keys) delete process.env[key];
  const previous = getActiveRouterClient();
  t.after(() => { setActiveRouterClient(previous); for (const [key, value] of env)
    value === undefined ? delete process.env[key] : process.env[key] = value; });
  let stream, registered, result, boundClient, deadlineAtDispatch;
  const server = http.createServer(async (req, res) => {
    let raw = ""; for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : {};
    const pathname = new URL(req.url, "http://fixture").pathname;
    if (pathname === "/stream") {
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write(": connected\n\n"); stream = res; return;
    }
    if (pathname === "/register") { assert.ok(boundClient); registered = body; }
    if (pathname === "/result") result = body;
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(pathname === "/messages"
      ? { ok: true, worker: "TamamoCross", messages: [], has_more: false, next_after: 0 }
      : { ok: true, state: body.status }));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  let runtime;
  t.after(async () => {
    await runtime?.close(); stream?.destroy(); server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await rm(stateDir, { recursive: true, force: true });
  });
  const transport = { taskDeadlines: new Map(), control: { bindRouter(client, origin) {
    boundClient = client; assert.equal(origin, "http://127.0.0.1:8906");
    const request = client._request.bind(client);
    client._request = (method, endpoint, body, options) => request(method, endpoint,
      endpoint === "/register" ? { ...body, control_url: origin } : body, options);
  } } };
  const manager = { stateDir, server: { listening: true }, config: { token: "fixture", machine: "TamamoCross", feishu: {} },
    state: { selectedThreadId: "existing-thread", sessions: { "existing-thread": {} }, receipts: {} },
    turns: [], receiptArchive: { get: async () => null },
    async mirrorInput() {}, async publicReply() {}, async captureTaskQuestions() {},
    async message(request) {
      deadlineAtDispatch = transport.taskDeadlines.get("t-deadline");
      this.state.receipts[request.messageId] = { nativeTurnId: "existing-turn" };
      this.turns.push({ id: "existing-turn", status: "completed", items: [
        { type: "userMessage", text: request.text }, { type: "agentMessage", text: "DSH shared runtime OK" }] });
    },
    async read() { return { turns: this.turns }; } };
  runtime = await startDshRouterRuntime({ manager, transport, workerConfig: {
    router: { url: `http://127.0.0.1:${server.address().port}` } }, pluginConfig: {},
    identity: { name: "TamamoCross", display: "玉藻十字" }, host: "127.0.0.1", port: 8906,
    tokenFile, log() {}, version: "test" });
  await until(() => stream && registered);
  assert.equal(registered.control_url, "http://127.0.0.1:8906");
  assert.equal(manager.routerClient, boundClient);
  assert.ok(manager.workerMessages);
  assert.equal(getActiveRouterClient(), boundClient);
  stream.write('event: task\ndata: {"task_id":"t-deadline","goal":"test","deadline_s":60}\n\n');
  await until(() => result && !runtime.busy());
  assert.equal(result.status, "done");
  assert.equal(result.summary, "DSH shared runtime OK");
  assert.ok(deadlineAtDispatch > Date.now());
  assert.equal(transport.taskDeadlines.size, 0);
  assert.equal(manager.state.selectedThreadId, "existing-thread");
  await runtime.close();
  assert.equal(manager.workerMessages, null);
  assert.equal(manager.routerClient, null);
});
