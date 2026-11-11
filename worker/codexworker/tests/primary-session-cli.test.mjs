// Full CLI lifecycle with a local JSON-RPC stub; no real model/router/Feishu calls.
import assert from "node:assert/strict";
import test from "node:test";
import path from "node:path";
import os from "node:os";
import net from "node:net";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { mkdtemp, mkdir, writeFile, readFile, rm } from "node:fs/promises";

const cli = fileURLToPath(new URL("../cli.mjs", import.meta.url));
const stub = `#!NODE
import fs from "node:fs";
import readline from "node:readline";
const log = process.env.TEST_ENGINE_LOG, cwd = process.env.TEST_ENGINE_CWD;
const previous = fs.existsSync(log) ? fs.readFileSync(log, "utf8").trim().split("\\n").filter(Boolean).map(JSON.parse) : [];
let reads = previous.filter(x => x.method === "thread/read").length;
readline.createInterface({ input: process.stdin }).on("line", line => {
  const request = JSON.parse(line); if (request.id == null) return;
  fs.appendFileSync(log, JSON.stringify({ method: request.method, threadId: request.params?.threadId }) + "\\n");
  const thread = { id: "original-thread", cwd, title: "test", status: "idle", turns: [] };
  if (request.method === "thread/read" && ++reads <= 2) {
    process.stdout.write(JSON.stringify({ id: request.id, error: { code: -32000, message: "temporary read failure" } }) + "\\n");
    return;
  }
  let result = {};
  if (request.method === "initialize") result = { userAgent: "isolated-session-test" };
  else if (request.method === "thread/read" || request.method === "thread/resume") result = { thread };
  else if (request.method === "thread/list") result = { data: [thread], nextCursor: null };
  else if (request.method === "thread/items/list") result = { data: [], nextCursor: null };
  else if (request.method === "thread/start") result = { thread: { ...thread, id: "wrong-new-thread" } };
  process.stdout.write(JSON.stringify({ id: request.id, result }) + "\\n");
});
`;
async function setup(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "worker-cli-continuity-"));
  const stateDir = path.join(root, "state"); await mkdir(stateDir);
  const token = "isolated-session-test-token-123456789";
  const binary = path.join(root, "engine.mjs"), engineLog = path.join(root, "engine.jsonl");
  await writeFile(binary, stub.replace("#!NODE", "#!" + process.execPath), { mode: 0o700 });
  const server = net.createServer(); server.listen(0, "127.0.0.1"); await once(server, "listening");
  const port = server.address().port; await new Promise(resolve => server.close(resolve));
  const tokenFile = path.join(root, "token"), feishuFile = path.join(root, "feishu.json");
  const configFile = path.join(root, "config.json"), identity = path.join(root, "identity.json");
  await writeFile(tokenFile, token); await writeFile(feishuFile, JSON.stringify({ app_id: "test-only", app_secret: "test-only" }));
  await writeFile(configFile, JSON.stringify({ tokenFile, port, sessionRecovery: { retryInitialMs: 5, retryMaxMs: 10 },
    feishu: { configFile: feishuFile, trainerOpenIdWhitelist: [], onlineNotificationEnabled: false },
    router: { enabled: false } }));
  await writeFile(identity, JSON.stringify({ name: "continuity-test", display: "test", port,
    configFile, transport: "app-server", stateDir }));
  const sessionPath = path.join(stateDir, "cli-session.json");
  await writeFile(sessionPath, JSON.stringify({ threadId: "original-thread", cwd: root }, null, 2));
  const guard = path.join(root, "local-fetch-only.mjs");
  await writeFile(guard, 'const original = globalThis.fetch; globalThis.fetch = (input, opts) => { const u = new URL(String(input)); if (u.hostname !== "127.0.0.1" && u.hostname !== "localhost") throw Error("external HTTP forbidden in test"); return original(input, opts); };');
  const children = [];
  const stop = async child => {
    if (child.exitCode != null || child.signalCode != null) return;
    const done = once(child, "exit"); child.kill("SIGTERM");
    const force = setTimeout(() => child.kill("SIGKILL"), 2000); force.unref();
    await done; clearTimeout(force);
  };
  t.after(async () => { await Promise.all(children.map(stop)); await rm(root, { recursive: true, force: true }); });
  const start = () => {
    const env = { ...process.env, CODEXWORKER_CODEX_BIN: binary, CODEXWORKER_STATE_DIR: stateDir,
      CODEXWORKER_TOKEN_FILE: tokenFile, CODEXWORKER_ROUTER: "0", CODEXWORKER_FEISHU_DRY_RUN: "1",
      TEST_ENGINE_LOG: engineLog, TEST_ENGINE_CWD: root };
    delete env.CODEX_APP_TOOLS_PIPE_PATH;
    const child = spawn(process.execPath, ["--import", guard, cli, "--identity", identity], { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
    let diagnostics = ""; child.stdout.on("data", c => { diagnostics += c; }); child.stderr.on("data", c => { diagnostics += c; });
    child.diagnostics = () => diagnostics; children.push(child); return child;
  };
  const ready = async child => {
    for (let i = 0; i < 120; i++) {
      if (child.exitCode != null) assert.fail(child.diagnostics());
      try {
        const response = await fetch("http://127.0.0.1:" + port + "/health",
          { headers: { authorization: "Bearer " + token }, signal: AbortSignal.timeout(300) });
        const health = await response.json();
        if (health.ready && health.integrations?.selectedThreadId === "original-thread") return health;
      } catch { /* private listener is still starting */ }
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    assert.fail("isolated worker did not recover: " + child.diagnostics());
  };
  return { root, sessionPath, engineLog, start, stop, ready };
}
test("CLI retries its real transport and survives a complete process restart on the original thread", { timeout: 15000 }, async t => {
  const f = await setup(t), original = await readFile(f.sessionPath);
  const first = f.start(); await f.ready(first);
  assert.deepEqual(await readFile(f.sessionPath), original);
  await f.stop(first);
  const second = f.start(); await f.ready(second);
  assert.deepEqual(await readFile(f.sessionPath), original);
  const calls = (await readFile(f.engineLog, "utf8")).trim().split("\n").map(JSON.parse);
  assert.equal(calls.filter(c => c.method === "thread/start").length, 0);
  assert.ok(calls.filter(c => c.method === "thread/read").length >= 4);
  assert.ok(calls.filter(c => c.method === "thread/read").every(c => c.threadId === "original-thread"));
});
test("CLI exits safely on corrupt primary state without starting a new engine session", { timeout: 10000 }, async t => {
  const f = await setup(t); await writeFile(f.sessionPath, "{broken");
  const child = f.start(); const [code] = await once(child, "exit");
  assert.equal(code, 1); assert.equal(await readFile(f.sessionPath, "utf8"), "{broken");
  assert.match(child.diagnostics(), /primary_session_blocked/);
  const calls = await readFile(f.engineLog, "utf8").catch(() => "");
  assert.equal(calls, "");
});
