import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { DesktopManager, PROTOCOL } from "../desktop-manager.mjs";
import { PluginRuntime } from "../plugin-runtime.mjs";
import { getActiveRouterClient, setActiveRouterClient } from "../router-client.mjs";
import { routerOperations } from "../router-operations.mjs";
import { startRouterRuntime } from "../router-runtime.mjs";
import { routerMessageId, routerPrompt } from "../router-tasks.mjs";
import { resetNativeTransportCache } from "../interface/transport/index.mjs";

// Run this identical file from either worker tree with Node >= 22. Python is
// stdlib-only; CODEXWORKER_TEST_PYTHON may name an interpreter (including spaces).
// All HTTP/SSE, SQLite, writer leases, framed native IPC and business-worker IPC
// are real. Only Desktop tool results and the trigger notification sink are
// fixtures: no model, Desktop process, Feishu account or installed config is used.
const pluginRoot = fileURLToPath(new URL("..", import.meta.url));
const repositoryRouter = fileURLToPath(new URL("../../../server/router/server.py", import.meta.url));
const routerSource = existsSync(repositoryRouter) ? repositoryRouter
  : fileURLToPath(new URL("../../router-server.py", import.meta.url));
const routerVersion = (await readFile(routerSource, "utf8")).match(/^VERSION = "([^"]+)"/m)?.[1];
assert.ok(routerVersion, "The exact router source must accompany this package.");
const toolNames = ["read_thread", "send_message_to_thread", "create_thread", "fork_thread",
  "set_thread_title", "list_threads", "list_projects", "wait_threads"];
const python = process.env.CODEXWORKER_TEST_PYTHON || (process.platform === "win32" ? "python" : "python3");

// Instantiate the actual server.py classes with port 0, avoiding a free-port
// probe race and the production CLI's platform-specific singleton lock. Its
// unconditional fcntl import needs this narrow Windows compatibility adapter;
// no router request, database, dispatch or ACK implementation is replaced.
const pythonBootstrap = String.raw`
import faulthandler, json, os, runpy, sys, threading, types
from pathlib import Path
faulthandler.dump_traceback_later(8, file=sys.stderr)
if sys.platform == "win32":
    import msvcrt
    compat = types.ModuleType("fcntl")
    compat.LOCK_EX, compat.LOCK_NB, compat.LOCK_UN = 2, 4, 8
    def flock(fd, operation):
        mode = msvcrt.LK_UNLCK if operation & 8 else (msvcrt.LK_NBLCK if operation & 4 else msvcrt.LK_LOCK)
        msvcrt.locking(fd, mode, 1)
    compat.flock = flock
    sys.modules["fcntl"] = compat
source, db, token_file, trigger_url = sys.argv[1:]
sys.path.insert(0, str(Path(source).parent))
api = runpy.run_path(source, run_name="router_e2e_server")
store = api["RouterStore"](db)
store.reset_runtime()
trigger = api["TriggerClient"](trigger_url, token_file)
server = api["RouterServer"](("127.0.0.1", 0), store, Path(token_file).read_text().strip(),
    api["TriggerNotifier"](trigger), api["TriggerSubscriptionBridge"](trigger), ping_interval=0.2)
def stop_on_eof():
    sys.stdin.buffer.read(1)
    server.shutdown()
threading.Thread(target=stop_on_eof, daemon=True).start()
print(json.dumps({"url": "http://127.0.0.1:%d" % server.server_address[1],
                  "version": api["VERSION"], "pid": os.getpid()}), flush=True)
faulthandler.cancel_dump_traceback_later()
try:
    server.serve_forever(poll_interval=0.05)
finally:
    with server.conn_lock:
        connections = list(server.conns.values())
    for connection in connections:
        connection.close("test_shutdown")
    server.server_close()
    store.close()
`;

async function bounded(operation, label, timeoutMs = 15000) {
  let timer;
  try {
    return await Promise.race([operation, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`timed out: ${label}`)), timeoutMs);
    })]);
  } finally { clearTimeout(timer); }
}

async function until(predicate, label, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  let last;
  do {
    last = await predicate();
    if (last) return last;
    await delay(25);
  } while (Date.now() < deadline);
  assert.fail(`timed out: ${label}; last value: ${JSON.stringify(last)}`);
}

async function jsonRequest(base, route, { body, token, router = false, status = 200 } = {}) {
  const response = await fetch(base + route, { method: body === undefined ? "GET" : "POST",
    headers: { "content-type": "application/json", ...(token ? router
      ? { "x-router-token": token } : { authorization: `Bearer ${token}` } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(5000) });
  const result = await response.json();
  assert.equal(response.status, status, `${route}: ${JSON.stringify(result)}`);
  return result;
}

function childEnvironment(root) {
  // An allowlist prevents inherited native pipes, router URLs, proxy settings,
  // NODE_OPTIONS and credentials from escaping this loopback-only fixture.
  const env = {};
  for (const key of ["PATH", "Path", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"]) if (process.env[key] !== undefined) env[key] = process.env[key];
  return { ...env, HOME: root, USERPROFILE: root, CODEX_HOME: path.join(root, ".codex"),
    PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1", PYTHONUTF8: "1", NO_PROXY: "127.0.0.1,localhost" };
}

async function listen(server, address = 0) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    if (typeof address === "number") server.listen(address, "127.0.0.1", resolve);
    else server.listen(address, resolve);
  });
}

function desktopFixture(label, root) {
  const control = `${label}-control`, target = `${label}-existing-chat`, sends = [], calls = [];
  const threads = new Map([control, target].map(id => [id, {
    thread: { id, cwd: root, title: id, hostId: "local", status: { type: "idle" } },
    turns: [{ id: `${id}-baseline`, status: "completed", items: [] }],
  }]));
  const native = async (name, args, context) => {
    calls.push({ name, args: structuredClone(args), context: structuredClone(context) });
    if (name === "read_thread") {
      assert.ok(threads.has(args.threadId), "read only the fixture's existing threads");
      const snapshot = structuredClone(threads.get(args.threadId));
      snapshot.turns = snapshot.turns.slice(-(args.turnLimit ?? 10)).reverse();
      return { ...snapshot, page: { order: "newest_first", nextCursor: null } };
    }
    if (name === "list_projects") return { projects: [{ projectId: `${label}-project`, path: root, hostId: "local" }] };
    if (name === "list_threads") return { pinnedThreads: [], threads: [...threads.values()].map(value => value.thread) };
    if (name === "wait_threads") return { polls: args.targets.map(({ threadId }) => {
      assert.equal(threadId, target, "native status must never self-target the control thread");
      const latest = threads.get(threadId).turns.at(-1);
      return { thread: { id: threadId, hostId: "local" }, latestTurn: { id: latest.id, status: latest.status } };
    }) };
    assert.equal(name, "send_message_to_thread", "never create/fork a thread or call a model");
    assert.equal(args.threadId, target);
    assert.equal(context.sourceThreadId, target);
    const turnId = `${label}-delivery-${sends.length + 1}`, summary = `${turnId}: native fixture completed`;
    sends.push({ ...structuredClone(args), turnId, summary });
    threads.get(target).turns.push({ id: turnId, status: "completed", items: [
      { id: `${turnId}-input`, type: "functionCallOutput", name, output:
        `<source_thread_id>${context.sourceThreadId}</source_thread_id><input>${args.prompt}</input>` },
      { id: `${turnId}-final`, type: "agentMessage", phase: "final_answer", text: summary },
    ] });
    return { threadId: target, turnId };
  };
  return { control, target, sends, calls, native };
}

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "cw-e2e-"));
  const disposers = [], children = [], sockets = new Set(), runtimes = [], managers = [], logs = [];
  const routerToken = `router-test-${randomUUID()}`, tokenFile = path.join(root, "router.token");
  let router, cleaned = false;
  const previousClient = getActiveRouterClient();
  const keys = ["CODEXWORKER_ROUTER", "CODEXWORKER_ROUTER_URL", "CODEXWORKER_ROUTER_TOKEN_FILE",
    "CODEXWORKER_FEISHU_DRY_RUN", "CODEXWORKER_NATIVE_TRANSPORT"];
  const saved = keys.map(key => [key, process.env[key]]);
  for (const key of keys) delete process.env[key];
  process.env.CODEXWORKER_NATIVE_TRANSPORT = "desktop-pipe";
  resetNativeTransportCache();
  const cleanup = async () => {
    if (cleaned) return;
    cleaned = true;
    const errors = [];
    for (const dispose of disposers.reverse()) {
      try { await dispose(); } catch (error) { errors.push(error); }
    }
    for (const [key, value] of saved) value === undefined ? delete process.env[key] : process.env[key] = value;
    resetNativeTransportCache(); setActiveRouterClient(previousClient);
    try { await rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 }); }
    catch (error) { errors.push(error); }
    if (errors.length) throw new AggregateError(errors, "isolated router fixture cleanup failed");
  };
  t.after(cleanup);
  await writeFile(tokenFile, routerToken, { mode: 0o600 });
  const notifications = [];
  const trigger = http.createServer(async (req, res) => {
    let raw = ""; for await (const chunk of req) raw += chunk;
    notifications.push({ method: req.method, path: req.url, token: req.headers["x-trigger-token"], body: JSON.parse(raw) });
    res.writeHead(200, { "content-type": "application/json" }); res.end('{"ok":true,"id":1}');
  });
  disposers.push(async () => { trigger.closeAllConnections(); await new Promise(resolve => trigger.close(resolve)); });
  await listen(trigger);
  const triggerURL = `http://127.0.0.1:${trigger.address().port}/trigger`;

  async function startRouter() {
    let stderr = "", output = "", ready, failure;
    const child = spawn(python, ["-u", "-c", pythonBootstrap, routerSource, path.join(root, "router.db"), tokenFile, triggerURL],
      { cwd: root, env: { ...childEnvironment(root), ROUTER_APPROVAL_POLICY: path.join(root, "approval-policy.json") },
        stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
    children.push(child);
    const exited = new Promise(resolve => {
      child.once("error", error => { failure = error; resolve(); });
      child.once("exit", (code, signal) => resolve({ code, signal }));
    });
    child.stdin.on("error", () => {});
    child.stderr.on("data", chunk => { stderr = (stderr + chunk).slice(-16000); });
    child.stdout.on("data", chunk => {
      output += chunk;
      const newline = output.indexOf("\n");
      if (!ready && newline >= 0) {
        try { ready = JSON.parse(output.slice(0, newline)); } catch (error) { failure = error; }
      }
    });
    let stopped = false;
    const stop = async () => {
      if (stopped) return;
      stopped = true;
      child.stdin.end();
      let result;
      try { result = await bounded(exited, "Python router graceful shutdown", 5000); }
      catch (error) { child.kill("SIGKILL"); await bounded(exited, "owned Python child exit", 5000); throw error; }
      if (failure) throw failure;
      assert.deepEqual(result, { code: 0, signal: null }, stderr);
    };
    disposers.push(stop);
    try {
      await until(() => {
        if (failure || child.exitCode !== null) throw new Error(`Cannot start ${python}: ${failure?.message ?? stderr}`);
        return ready;
      }, "real Python router readiness");
    } catch (error) { throw new Error(`${error.message}\nPython startup stderr:\n${stderr}\nstdout:\n${output}`); }
    assert.match(ready.url, /^http:\/\/127\.0\.0\.1:\d+$/);
    assert.equal(ready.pid, child.pid);
    assert.equal(ready.version, routerVersion);
    router = { ...ready, stop };
    return router;
  }
  const routerHTTP = (route, options = {}) => jsonRequest(router.url, route, { token: routerToken, router: true, ...options });
  async function startRuntime(peer) {
    peer.manager.config.router.url = router.url;
    peer.runtime = await startRouterRuntime({ manager: peer.manager, nativeCall: peer.native.native,
      identity: { name: peer.name, display: peer.name }, version: "0.3.0",
      log: (level, event, fields) => logs.push({ level, event, fields }) });
    const runtime = peer.runtime;
    disposers.push(() => runtime.close());
    await until(() => runtime.client?.connected, `${peer.name} SSE connected`);
    return runtime;
  }
  async function peer(label, native = desktopFixture(label, root)) {
    const stateDir = path.join(root, label, "writer"), name = `e2e-${label}`, httpCalls = [];
    const manager = new DesktopManager({ native: native.native, listNativeTools: async () => toolNames.map(name => ({ name })),
      stateDir, config: { port: 0, bridgeName: name, machine: name, token: `writer-test-${randomUUID()}`,
        workspaceRoots: [root], codexHome: path.join(root, ".codex"), publicJournalRoot: path.join(root, "public-journal"),
        pollMs: 40, nativeSenderThreadId: native.control, nativeSendFromTarget: true,
        nativeBusyDeliveryVerified: true, nativeStateIndependentDeliveryVerified: true, messageSourceLabels: false,
        router: { url: router.url, tokenFile }, feishu: { transcriptEnabled: true, autoImageOutput: false, taskBoardEnabled: false } } });
    managers.push(manager);
    let closed = false;
    const result = { name, manager, native, httpCalls, stateDir, close: async () => {
      if (closed) return;
      closed = true;
      await result.runtime?.close();
      clearInterval(manager.pollTimer);
      await until(() => !manager.pollRunning, `${name} native scanner drained`);
      manager.server?.closeAllConnections();
      await bounded(manager.close(), `${name} manager close`);
    } };
    disposers.push(result.close);
    await manager.initialize();
    result.url = await manager.listen();
    manager.server.on("request", req => httpCalls.push({ method: req.method, route: req.url }));
    if (!manager.state.selectedThreadId) await manager.register({ protocolVersion: PROTOCOL, source: "trainer",
      messageId: randomUUID(), threadId: native.target, hostId: "local" });
    assert.equal(manager.state.selectedThreadId, native.target);
    assert.notEqual(manager.config.nativeSenderThreadId, native.target);
    assert.equal(manager.isSelfTarget(manager.state.sessions[native.target]), false);
    await startRuntime(result);
    return result;
  }
  async function proxy(peer) {
    const stateDir = path.join(root, peer.name, "proxy");
    await mkdir(stateDir, { recursive: true });
    // Short Unix socket paths also work under macOS's small sockaddr_un limit.
    const pipe = process.platform === "win32" ? `\\\\.\\pipe\\cw-e2e-${randomUUID()}` : path.join(root, `${peer.name}.sock`);
    const requests = [], failures = [], pipeSockets = new Set();
    const nativePipe = net.createServer(socket => {
      sockets.add(socket); pipeSockets.add(socket);
      socket.once("close", () => { sockets.delete(socket); pipeSockets.delete(socket); }); socket.on("error", () => {});
      let buffer = Buffer.alloc(0), handled = false;
      socket.on("data", async chunk => {
        if (handled) return;
        buffer = Buffer.concat([buffer, chunk]);
        if (buffer.length < 4) return;
        const size = buffer.readUInt32LE(0);
        if (size > 1024 * 1024) { failures.push("oversized native request"); socket.destroy(); return; }
        if (buffer.length < size + 4) return;
        handled = true;
        let request, response;
        try {
          request = JSON.parse(buffer.subarray(4, size + 4).toString("utf8")); requests.push(request);
          let result;
          if (request.method === "tools/list") result = { tools: toolNames.map(name => ({ name, namespace: "codex_app" })) };
          else {
            assert.equal(request.method, "tools/call");
            assert.equal(request.params.threadId, peer.native.control);
            assert.ok(["read_thread", "list_projects"].includes(request.params.tool), "MCP proxy native pipe is read-only preflight");
            if (request.params.tool === "read_thread") assert.equal(request.params.arguments.threadId, peer.native.control);
            result = { success: true, contentItems: [{ type: "inputText", text:
              JSON.stringify(await peer.native.native(request.params.tool, request.params.arguments)) }] };
          }
          response = { jsonrpc: "2.0", id: request.id, result };
        } catch (error) {
          failures.push(error.message); response = { jsonrpc: "2.0", id: request?.id, error: { code: -32603, message: error.message } };
        }
        const bytes = Buffer.from(JSON.stringify(response)), frame = Buffer.alloc(4 + bytes.length);
        frame.writeUInt32LE(bytes.length); bytes.copy(frame, 4); socket.end(frame);
      });
    });
    let pipeClosed = false;
    const closePipe = async () => {
      if (pipeClosed) return;
      pipeClosed = true;
      for (const socket of pipeSockets) socket.destroy();
      await new Promise(resolve => nativePipe.close(resolve));
    };
    disposers.push(closePipe);
    await listen(nativePipe, pipe);
    await writeFile(path.join(stateDir, "manager-binding.json"), JSON.stringify({ managerThreadId: peer.native.control, managerTurnId: "fixture-control-turn" }));
    const writerTokenFile = path.join(stateDir, "writer.token"); await writeFile(writerTokenFile, peer.manager.config.token, { mode: 0o600 });
    await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ tokenFile: writerTokenFile,
      port: peer.manager.config.port, bridgeName: peer.name, machine: peer.name, workspaceRoots: [root], managerProxyWaitMs: 500 }));
    const runtime = new PluginRuntime({ pluginRoot, stateDir, pollMs: 1000000, env: { ...childEnvironment(root),
      CODEXWORKER_STATE_DIR: stateDir, CODEXWORKER_NATIVE_TRANSPORT: "desktop-pipe", CODEX_APP_TOOLS_PIPE_PATH: pipe,
      RUDOLF_BRIDGE_LIFECYCLE_CHILD: "0", RUDOLF_BRIDGE_LIFECYCLE_READY: "1" } });
    runtimes.push(runtime);
    const close = async () => {
      try { await bounded(runtime.close(), "MCP proxy close", 5000); }
      catch (error) {
        await Promise.all([...runtime.children].map(({ child }) => bounded(new Promise(resolve => {
          if (child.exitCode !== null || child.signalCode !== null) return resolve();
          child.once("exit", resolve); child.kill("SIGKILL");
        }), "owned MCP child exit", 5000)));
        throw error;
      }
      finally { await closePipe(); }
    };
    disposers.push(close);
    const mcp = (method, params = {}) => bounded(runtime.call(method, params), method);
    const call = async (name, args = {}) => {
      const result = await mcp("tools/call", { name, arguments: args });
      assert.equal(result.isError, false, JSON.stringify(result));
      return result.structuredContent;
    };
    const initialized = await mcp("initialize", { __bridgeAutoStart: false });
    assert.equal(initialized.serverInfo.version, "0.3.0");
    return { runtime, requests, failures, call, mcp, close };
  }
  await startRouter();
  return { root, children, runtimes, managers, sockets, notifications, logs, cleanup, startRouter, routerHTTP,
    startRuntime, peer, proxy, router: () => router };
}

async function accepted(f, id) {
  return until(async () => {
    const result = await f.routerHTTP(`/messages/${id}`);
    return result.status === "accepted" && result;
  }, `router ACK accepted for ${id}`);
}

async function delivered(peer, id) {
  await until(() => peer.manager.state.receipts[id]?.nativeDispatch?.status === "accepted", `native delivery ${id}`);
  await until(() => !peer.runtime.busy() && !peer.manager.pollRunning, "durable receipt settled");
  const disk = JSON.parse(await readFile(peer.manager.statePath, "utf8"));
  assert.equal(disk.selectedThreadId, peer.native.target);
  assert.equal(disk.receipts[id].response.accepted, true);
  assert.equal(disk.receipts[id].nativeDispatch.status, "accepted");
  assert.equal(disk.receipts[id].request.peerMessageId, id);
  assert.equal(disk.receipts[id].request.origin, "worker-chat");
  assert.equal(disk.receipts[id].request.targetThreadId, peer.native.target);
  return disk.receipts[id];
}

test("PluginRuntime MCP -> DesktopManager writer HTTP -> real Python router (isolated loopback)", { timeout: 180000 }, async t => {
  const f = await fixture(t);
  let a, b, proxyA, proxyB;
  await t.test("two real managers adopt existing threads and MCP proxies use authenticated writer routes", async () => {
    a = await f.peer("A"); b = await f.peer("B");
    proxyA = await f.proxy(a); proxyB = await f.proxy(b);
    const health = await f.routerHTTP("/health");
    assert.equal(health.db_path, path.join(f.root, "router.db"));
    assert.equal(health.online, 2); assert.equal(health.tasks, 0);
    await f.routerHTTP("/workers", { token: "wrong-test-token", status: 401 });
    await jsonRequest(a.url, "/health", { token: "wrong-test-token", status: 401 });
    const inventory = await proxyA.mcp("tools/list");
    for (const name of ["bridge_list_workers", "bridge_send_worker_message", "bridge_worker_inbox", "bridge_create_work_order", "bridge_work_order_status"])
      assert.ok(inventory.tools.some(tool => tool.name === name), name);
    const listed = await proxyA.call("bridge_list_workers");
    assert.deepEqual(listed.workers.map(worker => worker.name).sort(), [a.name, b.name]);
    assert.ok(listed.workers.every(worker => worker.online && worker.version === "0.3.0"));
    assert.ok(a.httpCalls.some(call => call.route === "/bridge/workers"));
    assert.deepEqual(proxyA.requests.filter(req => req.method === "tools/call").map(req => req.params.tool), ["read_thread", "list_projects"]);
    assert.equal(proxyA.runtime.children.size, 1);
    t.diagnostic(`Python ${python}; router ${health.version}; Node ${process.version}; native platform ${process.platform}`);
  });

  let first, reply;
  await t.test("MCP peer A -> B -> A preserves UUID/text/reply correlation and ACKs without any task rows", async () => {
    first = { messageId: randomUUID(), to: b.name, text: "普通消息 A→B\n保留原文、换行和 emoji 🍚" };
    const sent = await proxyA.call("bridge_send_worker_message", first);
    assert.equal(sent.message_id, first.messageId); assert.equal(sent.status, "queued");
    const receipt = await accepted(f, first.messageId), saved = await delivered(b, first.messageId);
    assert.deepEqual({ from: receipt.message.from, to: receipt.message.to, text: receipt.message.text, reply_to: receipt.message.reply_to },
      { from: a.name, to: b.name, text: first.text, reply_to: null });
    assert.equal(receipt.message_id, first.messageId); assert.equal(saved.request.reply_to, null);
    assert.ok(b.native.sends[0].prompt.endsWith(first.text));
    assert.ok(b.native.sends[0].prompt.startsWith(`[worker ${a.name} message ${first.messageId}]`));
    reply = { messageId: randomUUID(), to: a.name, text: "回复 B→A\n已收到完整内容。", replyTo: first.messageId };
    const response = await proxyB.call("bridge_send_worker_message", reply);
    assert.equal(response.message_id, reply.messageId);
    const replyReceipt = await accepted(f, reply.messageId), replySaved = await delivered(a, reply.messageId);
    assert.equal(replyReceipt.message.text, reply.text); assert.equal(replyReceipt.message.from, b.name);
    assert.equal(replyReceipt.message.to, a.name); assert.equal(replyReceipt.message.reply_to, first.messageId);
    assert.equal(replySaved.request.reply_to, first.messageId);
    assert.ok(a.native.sends[0].prompt.startsWith(`[worker ${b.name} message ${reply.messageId} reply_to ${first.messageId}]`));
    assert.ok(a.native.sends[0].prompt.endsWith(reply.text));
    assert.equal((await proxyA.call("bridge_send_worker_message", first)).duplicate, true);
    const conflict = await proxyA.mcp("tools/call", { name: "bridge_send_worker_message", arguments: { ...first, text: "changed" } });
    assert.equal(conflict.isError, true); assert.equal(conflict.structuredContent.error, "message_id_conflict");
    assert.deepEqual((await f.routerHTTP("/tasks")).tasks, []);
    assert.equal((await f.routerHTTP("/health")).tasks, 0);
    assert.equal(f.notifications.length, 0);
    assert.equal(a.native.sends.length, 1); assert.equal(b.native.sends.length, 1);
    assert.deepEqual((await proxyA.call("bridge_worker_inbox")).messages, []);
    assert.ok(a.httpCalls.some(call => call.method === "POST" && call.route === "/bridge/worker-messages"));
    assert.ok(b.httpCalls.some(call => call.method === "POST" && call.route === "/bridge/worker-messages"));
  });

  await t.test("POST /messages queues offline; SQLite/router and recipient-manager restart replay exactly once", async () => {
    const bNative = b.native;
    await proxyB.close(); await b.close();
    await until(async () => (await f.routerHTTP("/workers")).workers.find(worker => worker.name === b.name)?.online === false, "B offline");
    const payload = { message_id: randomUUID(), from: a.name, to: b.name, text: "离线积压\n重启后仍使用同一个 UUID。" };
    const queued = await f.routerHTTP("/messages", { body: payload, status: 202 });
    assert.equal(queued.status, "queued"); assert.equal(queued.message_id, payload.message_id);
    assert.equal((await f.routerHTTP(`/messages/${payload.message_id}`)).status, "queued");
    const pending = await f.routerHTTP(`/messages?worker=${b.name}`);
    assert.equal(pending.count, 1); assert.equal(pending.messages[0].text, payload.text);
    await f.routerHTTP(`/messages/${payload.message_id}/ack`, { body: { worker: a.name }, status: 403 });
    const duplicate = await f.routerHTTP("/messages", { body: payload });
    assert.equal(duplicate.duplicate, true); assert.equal(duplicate.message.seq, queued.message.seq);
    await f.routerHTTP("/messages", { body: { ...payload, text: "different" }, status: 409 });
    assert.equal(bNative.sends.length, 1, "no delivery while recipient is offline");
    await a.runtime.close(); await f.router().stop();
    await f.startRouter();
    const restored = await f.routerHTTP(`/messages/${payload.message_id}`);
    assert.equal(restored.status, "queued"); assert.deepEqual(restored.message, queued.message);
    assert.equal((await f.routerHTTP("/health")).online, 0);
    await f.startRuntime(a); b = await f.peer("B", bNative); proxyB = await f.proxy(b);
    assert.equal((await accepted(f, payload.message_id)).message.text, payload.text);
    const disk = await delivered(b, payload.message_id);
    assert.equal(disk.request.messageId, payload.message_id);
    assert.equal(bNative.sends.length, 2);
    assert.ok(bNative.sends[1].prompt.endsWith(payload.text));
    assert.equal((await f.routerHTTP(`/messages?worker=${b.name}`)).count, 0);
    const ackAgain = await f.routerHTTP(`/messages/${payload.message_id}/ack`, { body: { worker: b.name } });
    assert.equal(ackAgain.already, true); assert.equal(ackAgain.status, "accepted");
    const replay = await b.runtime.messages.receive(restored.message);
    assert.equal(replay.messageId, payload.message_id); assert.equal(replay.duplicate, true); assert.equal(replay.acked, true);
    await b.runtime.messages.poll();
    assert.equal((await proxyA.call("bridge_send_worker_message", first)).duplicate, true, "sender receipt survives router runtime restart");
    assert.equal(bNative.sends.length, 2, "ACK replay/reconnect cannot repeat a native write");
    assert.deepEqual((await f.routerHTTP("/tasks")).tasks, []);
  });

  await t.test("MCP work order and routerOperations retry share one ID, native goal prompt and exact terminal result", async () => {
    const request = { messageId: randomUUID(), worker: b.name, goal: "完成隔离插件路由验收", context: "只验证本工单，补充内容归同一个目标。" };
    const order = await proxyA.call("bridge_create_work_order", request);
    assert.match(order.task_id, /^t-/); assert.equal(order.execution_mode, "goal");
    const retry = await routerOperations(a.manager).createWorkOrder(request);
    assert.equal(retry.task_id, order.task_id); assert.equal(retry.duplicate, true);
    const mcpRetry = await proxyA.call("bridge_create_work_order", request);
    assert.equal(mcpRetry.task_id, order.task_id); assert.equal(mcpRetry.duplicate, true);
    const finished = await until(async () => {
      const value = await proxyA.call("bridge_work_order_status", { taskId: order.task_id });
      return value.state === "done" && value;
    }, "formal native turn result persisted at router");
    const delivery = b.native.sends.find(send => send.prompt.startsWith(`[router ${order.task_id}]`));
    assert.ok(delivery); assert.equal(delivery.threadId, b.native.target);
    assert.equal(delivery.prompt, routerPrompt({ taskId: order.task_id, goal: request.goal, context: request.context }));
    for (const text of ["正式工单", "get_goal", "create_goal", "已有目标", "不能擅自替换", "不自设 token 预算"])
      assert.ok(delivery.prompt.includes(text), text);
    assert.equal(finished.task_id, order.task_id); assert.equal(finished.task.task_id, order.task_id);
    assert.equal(finished.task.worker, b.name); assert.equal(finished.task.goal, request.goal);
    assert.equal(finished.task.context, request.context); assert.equal(finished.task.execution_mode, "goal");
    assert.equal(finished.task.deadline_s, null);
    assert.equal(finished.task.result.status, "done"); assert.equal(finished.task.result.summary, delivery.summary);
    assert.deepEqual(finished.task.result.evidence, [{ type: "codex_thread", thread_id: b.native.target, turn_id: delivery.turnId }]);
    const receipt = JSON.parse(await readFile(b.manager.statePath, "utf8")).receipts[routerMessageId(order.task_id)];
    assert.equal(receipt.request.origin, "router"); assert.equal(receipt.nativeTurnId, delivery.turnId);
    assert.equal(receipt.nativeDispatch.status, "accepted");
    const conflict = await proxyA.mcp("tools/call", { name: "bridge_create_work_order", arguments: { ...request, goal: "changed goal" } });
    assert.equal(conflict.isError, true); assert.equal(conflict.structuredContent.error, "message_id_conflict");
    const tasks = await f.routerHTTP("/tasks");
    assert.equal(tasks.count, 1); assert.equal(tasks.tasks[0].task_id, order.task_id);
    assert.equal(b.native.sends.filter(send => send.prompt.startsWith(`[router ${order.task_id}]`)).length, 1);
    await until(() => f.notifications.length === 1 && !b.runtime.busy(), "one real loopback result notification");
    assert.equal(f.notifications[0].path, "/trigger"); assert.equal(f.notifications[0].method, "POST");
    assert.equal(f.notifications[0].body.kind, "task.result");
    assert.deepEqual(f.notifications[0].body.payload, { task_id: order.task_id, worker: b.name, status: "done" });
    assert.ok(a.httpCalls.some(call => call.method === "POST" && call.route === "/bridge/work-orders"));
    assert.ok(a.httpCalls.some(call => call.route === `/bridge/work-orders/${order.task_id}`));
    assert.deepEqual(proxyA.failures, []); assert.deepEqual(proxyB.failures, []);
    assert.ok(b.native.calls.some(call => call.name === "wait_threads"), "exercise Desktop-shaped native status, including queue dispatch checks");
    assert.equal(f.logs.filter(row => row.event === "router_handler_failed").length, 0, JSON.stringify(f.logs));
  });

  await t.test("cleanup exits only owned children and releases HTTP listeners, native sockets and temporary state", async () => {
    await f.cleanup();
    assert.ok(f.children.every(child => child.exitCode === 0));
    assert.ok(f.runtimes.every(runtime => runtime.children.size === 0));
    assert.ok(f.managers.every(manager => !manager.server.listening && !manager.writerLease.listening));
    assert.equal(f.sockets.size, 0);
    await assert.rejects(stat(f.root), { code: "ENOENT" });
  });
});
