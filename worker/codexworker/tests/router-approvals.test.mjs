// RouterClient 的审批方法：URL、鉴权头、请求体形状（对真 http server）。
import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import RouterClient from "../router-client.mjs";

function startServer() {
  const calls = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      const url = new URL(req.url, "http://127.0.0.1");
      calls.push({
        method: req.method, path: url.pathname, query: url.search,
        token: req.headers["x-router-token"], body: body ? JSON.parse(body) : null,
      });
      const send = (status, value) => {
        res.writeHead(status, { "content-type": "application/json" });
        res.end(JSON.stringify(value));
      };
      if (req.method === "POST" && url.pathname === "/approvals") {
        return send(200, { ok: true, approval_id: "ap_abc123", status: "pending", notified: true });
      }
      if (req.method === "GET" && /^\/approvals\/ap_[^/]+$/.test(url.pathname)) {
        return send(200, { ok: true, approval: { approval_id: url.pathname.split("/").at(-1), status: "approved", note: "可以" } });
      }
      if (req.method === "POST" && /^\/approvals\/ap_[^/]+\/decide$/.test(url.pathname)) {
        return send(200, { ok: true, approval_id: url.pathname.split("/")[2], status: "denied", decision: "denied" });
      }
      if (req.method === "GET" && url.pathname === "/approvals") {
        return send(200, { ok: true, count: 1, approvals: [{ approval_id: "ap_abc123", status: "pending" }] });
      }
      return send(404, { ok: false, error: "not found" });
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, calls, url: `http://127.0.0.1:${server.address().port}` }));
  });
}

test("approval client methods hit the documented paths with the token", async () => {
  const { server, calls, url } = await startServer();
  const client = new RouterClient({ url, token: "secret-token", name: "codexworker" });
  try {
    const asked = await client.requestApproval({
      taskId: "t-1", kind: "command", title: "执行命令：ls",
      detail: "cwd: /tmp", ttlMs: 600_000,
    });
    assert.equal(asked.approval_id, "ap_abc123");
    const submitted = calls.at(-1);
    assert.equal(submitted.path, "/approvals");
    assert.equal(submitted.token, "secret-token");
    assert.deepEqual(submitted.body, {
      worker: "codexworker", task_id: "t-1", kind: "command",
      title: "执行命令：ls", detail: "cwd: /tmp", ttl_s: 600,
    });

    const approval = await client.getApproval("ap_abc123");
    assert.equal(approval.status, "approved");
    assert.equal(approval.note, "可以");
    assert.equal(calls.at(-1).path, "/approvals/ap_abc123");
    assert.equal(calls.at(-1).method, "GET");

    const decided = await client.decideApproval("ap_abc123", "denied", "不行");
    assert.equal(decided.status, "denied");
    assert.equal(calls.at(-1).path, "/approvals/ap_abc123/decide");
    assert.deepEqual(calls.at(-1).body, { decision: "denied", note: "不行" });

    const listing = await client.approvals({ status: "pending", limit: 5 });
    assert.equal(listing.length, 1);
    assert.match(calls.at(-1).query, /status=pending/);
    assert.match(calls.at(-1).query, /limit=5/);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("approval client validates inputs before sending", async () => {
  const client = new RouterClient({ url: "http://127.0.0.1:1", token: "x", name: "codexworker" });
  await assert.rejects(() => client.requestApproval({ title: "  " }), /title/);
  await assert.rejects(() => client.decideApproval("ap_1", "maybe"), /approved or denied/);
  await assert.rejects(() => client.getApproval(""), /approval id/);
});
