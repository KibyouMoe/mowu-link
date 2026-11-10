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
  const subscriptions = new Map();
  const pending = [];
  let subSeq = 0;
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      const url = new URL(req.url, "http://127.0.0.1");
      const sendJson = (value) => {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify(value));
      };
      if (req.method === "POST") {
        const parsed = body ? JSON.parse(body) : null;
        calls.push({ path: url.pathname, body: parsed });
        if (url.pathname === "/subscriptions") {
          const id = ++subSeq;
          const record = {
            id, worker: parsed.worker, source: parsed.source, kinds: parsed.kinds ?? [],
            active: true, trigger_id: `router:${parsed.worker}${id > 1 ? `:${id}` : ""}`,
            trigger_since: 10,
          };
          subscriptions.set(id, record);
          sendJson({ ok: true, ...record, trigger_forwarded: true });
          return;
        }
        sendJson({ ok: true, new: true, queued: 0, state: "done" });
        return;
      }
      if (req.method === "DELETE" && url.pathname.startsWith("/subscriptions/")) {
        const id = Number(url.pathname.split("/").at(-1));
        calls.push({ path: url.pathname, method: "DELETE", id });
        const found = subscriptions.get(id) ?? null;
        subscriptions.delete(id);
        sendJson(found ? { ok: true, id, deleted: true } : { ok: false, error: "unknown_subscription" });
        return;
      }
      if (req.method === "GET" && url.pathname === "/subscriptions") {
        calls.push({ path: "/subscriptions", method: "GET", query: url.search });
        sendJson({ ok: true, count: subscriptions.size, subscriptions: [...subscriptions.values()] });
        return;
      }
      if (req.method === "GET" && url.pathname === "/subscriptions/pending") {
        const events = pending.splice(0, pending.length);
        calls.push({ path: "/subscriptions/pending", method: "GET", query: url.search, delivered: events.length });
        sendJson({ ok: true, worker: url.searchParams.get("worker"), count: events.length,
          events: events.map((event, index) => ({ id: event.id ?? index + 1, ...event })), acked: events.length });
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
        calls, streams, subscriptions, pending,
        pushEvent: (event) => pending.push(event),
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
    assert.equal(router.calls.filter((call) => call.path === "/subscriptions").length, 0);
    assert.equal(router.calls.filter((call) => call.path === "/subscriptions/pending").length, 0);
    assert.equal(client.status().subscriptions.length, 0);
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

test("declares configured sources, polls pending events, and runs them one at a time", async () => {
  const router = await startFakeRouter();
  const logs = [];
  const client = new RouterClient({
    url: router.url, token: "t".repeat(32), name: "SubWorker",
    heartbeatMs: 1000, minBackoffMs: 100, maxBackoffMs: 200,
    subscriptionPollMs: 40,
    subscriptions: [{ source: "test", kinds: ["chat"] }, { source: "mail" }],
    logger: (level, event, fields) => logs.push({ level, event, fields }),
  });
  const seen = [];
  let concurrent = 0;
  let maxConcurrent = 0;
  try {
    router.pushEvent({ id: 7, source: "test", kind: "chat", text: "hello" });
    router.pushEvent({ id: 8, source: "test", kind: "chat", text: "world" });
    client.start({
      onTask: () => {},
      onSubscriptionEvent: async (event) => {
        concurrent += 1;
        maxConcurrent = Math.max(maxConcurrent, concurrent);
        await sleep(60);
        seen.push(event.text);
        concurrent -= 1;
      },
    });
    await waitFor(() => seen.length === 2, 4000, "both events");

    const created = router.calls.filter((call) => call.path === "/subscriptions" && call.body);
    assert.deepEqual(created.map((call) => call.body), [
      { worker: "SubWorker", source: "test", kinds: ["chat"] },
      { worker: "SubWorker", source: "mail" },
    ]);
    assert.equal(maxConcurrent, 1);
    assert.deepEqual(seen, ["hello", "world"]);
    await waitFor(() => logs.some((line) => line.event === "subscription_poll"), 3000, "poll log");
    assert.ok(logs.some((line) => line.event === "subscription_declared" && line.fields.source === "test"));
    assert.ok(logs.some((line) => line.event === "subscription_event" && line.fields.id === 7));
    const polled = router.calls.filter((call) => call.path === "/subscriptions/pending");
    assert.match(polled[0].query, /worker=SubWorker/);
    assert.match(polled[0].query, /limit=20/);
    assert.deepEqual(client.status().subscriptions.map((sub) => sub.source), ["test", "mail"]);
    assert.equal(client.status().subscriptionStats.events, 2);

    await client.stop();
    const deleted = router.calls.filter((call) => call.method === "DELETE").map((call) => call.id);
    assert.deepEqual(deleted.sort((a, b) => a - b), [1, 2]);
    assert.equal(client.status().subscriptions.length, 0);
  } finally {
    await client.stop();
    await router.close();
  }
});

test("re-declares subscriptions after a reconnect, dropping stale rows first", async () => {
  const router = await startFakeRouter();
  const client = new RouterClient({
    url: router.url, token: "t".repeat(32), name: "ReconnectSubWorker",
    heartbeatMs: 1000, minBackoffMs: 50, maxBackoffMs: 100,
    subscriptionPollMs: 500,
    subscriptions: [{ source: "test" }],
    logger: () => {},
  });
  try {
    client.start({ onTask: () => {}, onSubscriptionEvent: () => {} });
    await waitFor(() => router.calls.filter((call) => call.path === "/subscriptions" && call.body).length === 1, 3000, "first declare");
    const firstId = router.calls.filter((call) => call.path === "/subscriptions" && call.body).at(-1).body;
    assert.equal(firstId.source, "test");

    router.streams[0].end();
    await waitFor(() => router.streams.length === 2 && client.connected, 3000, "second stream");
    await waitFor(() => router.calls.filter((call) => call.path === "/subscriptions" && call.body).length === 2, 3000, "second declare");
    const staleDeletes = router.calls.filter((call) => call.method === "DELETE");
    assert.ok(staleDeletes.some((call) => call.id === 1), "old subscription id was deleted before re-declaring");
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
