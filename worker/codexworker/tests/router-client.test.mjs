import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { RouterClient, takeFrame } from "../router-client.mjs";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(predicate, timeoutMs = 3000, label = "condition") {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await sleep(10);
  }
  throw new Error(`timed out waiting for ${label}`);
}

function startFakeRouter() {
  const calls = [];
  const streams = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      const url = new URL(req.url, "http://127.0.0.1");
      if (req.method === "POST") {
        calls.push({ path: url.pathname, body: body ? JSON.parse(body) : null });
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ ok: true, new: true, queued: 0, state: "done" }));
        return;
      }
      if (url.pathname === "/stream") {
        calls.push({ path: "/stream", name: url.searchParams.get("name"), token: req.headers["x-router-token"] });
        res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
        res.write(": connected\n\n");
        streams.push(res);
        return;
      }
      res.writeHead(404, { "content-type": "application/json" });
      res.end("{}");
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      resolve({
        url: `http://127.0.0.1:${port}`,
        calls, streams, server,
        close: () => new Promise((done) => {
          for (const stream of streams) stream.destroy();
          server.close(done);
        }),
      });
    });
  });
}

test("registers, parses SSE tasks, beats, reports and notifies", async () => {
  const router = await startFakeRouter();
  const client = new RouterClient({
    url: router.url, token: "t".repeat(32), name: "TestWorker", version: "9.9.9",
    heartbeatMs: 1000, minBackoffMs: 100, maxBackoffMs: 200,
    logger: () => {},
  });
  const tasks = [];
  const connects = [];
  try {
    client.start({ onTask: (task) => { tasks.push(task); }, onConnect: (info) => connects.push(info) });
    await waitFor(() => router.streams.length === 1 && client.connected, 3000, "first stream");

    assert.deepEqual(router.calls[0], {
      path: "/register",
      body: { name: "TestWorker", version: "9.9.9", note: "codexworker router client" },
    });
    assert.equal(router.calls[1].path, "/stream");
    assert.equal(router.calls[1].name, "TestWorker");
    assert.equal(router.calls[1].token, "t".repeat(32));

    router.streams[0].write(": ping\n\n");
    router.streams[0].write(`event: task\ndata: ${JSON.stringify({ task_id: "t-1", goal: "x", type: "task" })}\n\n`);
    await waitFor(() => tasks.length === 1, 3000, "task frame");
    assert.equal(tasks[0].task_id, "t-1");
    assert.equal(tasks[0].goal, "x");

    client.setActivity({ state: "running", taskId: "t-1" });
    await client.heartbeat();
    const beat = router.calls.at(-1);
    assert.deepEqual(beat, { path: "/heartbeat", body: { name: "TestWorker", state: "running", task_id: "t-1" } });

    await client.result({ taskId: "t-1", status: "done", summary: "OK", evidence: [{ thread_id: "th" }] });
    assert.deepEqual(router.calls.at(-1).body, {
      name: "TestWorker", task_id: "t-1", status: "done", summary: "OK", artifacts: [], evidence: [{ thread_id: "th" }],
    });

    await client.notice({ text: "hello" });
    assert.deepEqual(router.calls.at(-1).body, {
      worker: "TestWorker", text: "hello", kind: "worker.notice", urgency: "normal",
    });

    assert.equal(client.status().connected, true);
    assert.equal(client.status().registered, true);
    assert.equal(connects.length, 1);
  } finally {
    await client.stop();
    await router.close();
  }
});

test("reconnects with backoff and re-registers before the second stream", async () => {
  const router = await startFakeRouter();
  const client = new RouterClient({
    url: router.url, token: "t".repeat(32), name: "RetryWorker",
    heartbeatMs: 1000, minBackoffMs: 50, maxBackoffMs: 100,
    logger: () => {},
  });
  try {
    client.start({ onTask: () => {} });
    await waitFor(() => router.streams.length === 1 && client.connected, 3000, "first stream");
    router.streams[0].end();  // server closes the stream
    await waitFor(() => router.streams.length === 2 && client.connected, 3000, "second stream");

    const registers = router.calls.filter((call) => call.path === "/register");
    assert.equal(registers.length, 2);
    assert.equal(router.calls.filter((call) => call.path === "/stream").length, 2);
    assert.equal(client.status().reconnects, 1);
    assert.equal(client.status().attempts, 2);
  } finally {
    await client.stop();
    await router.close();
  }
});

test("takeFrame handles flush and CRLF separators", () => {
  assert.deepEqual(takeFrame("event: task\ndata: {}\n\nrest"), { text: "event: task\ndata: {}", rest: "rest" });
  assert.equal(takeFrame("no separator"), null);
  assert.deepEqual(takeFrame("a\r\n\r\nb"), { text: "a", rest: "b" });
});
