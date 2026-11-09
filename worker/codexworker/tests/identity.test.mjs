import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";
import http from "node:http";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { IDENTITY_REQUIRED_MESSAGE, resolveIdentity } from "../identity.mjs";
import { CliFeishuChannel } from "../feishu-cli.mjs";
import { SessionStore } from "../session-store.mjs";

// ---------------------------------------------------------------------------
// Instance identity: identity.json (name/display/port) is what makes a process
// an instance. A missing or half-written identity must fail the boot, never
// fall back to the bridge's shell name.
// ---------------------------------------------------------------------------

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CLI_ENTRY = path.join(HERE, "..", "cli.mjs");
const SHIPPED = JSON.parse(await readFile(path.join(HERE, "..", "identity.json"), "utf8"));

const FAKE_CODEX = `#!/usr/bin/env node
if (process.argv[2] === "--version") { console.log("codex-cli 0.0.0-identity"); process.exit(0); }
process.exit(0);
`;

function requestJson(base, pathname) {
  return new Promise((resolve, reject) => {
    const req = http.request(`${base}${pathname}`, { method: "GET" }, res => {
      const chunks = [];
      res.on("data", chunk => chunks.push(chunk));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        let json = null;
        try { json = text ? JSON.parse(text) : null; } catch { /* non-JSON */ }
        resolve({ status: res.statusCode, json, text });
      });
    });
    req.on("error", reject);
    req.end();
  });
}

function childEnv(root, codexHome) {
  const env = { ...process.env, HOME: root, CODEX_HOME: codexHome,
    NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" };
  for (const key of ["CODEXWORKER_CONFIG", "CODEXWORKER_IDENTITY", "CODEXWORKER_STATE_DIR",
    "CODEXWORKER_LOG", "CODEXWORKER_PORT", "CODEXWORKER_HOST", "CODEX_APP_TOOLS_PIPE_PATH"]) delete env[key];
  return env;
}

async function sandbox({ config = {} } = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-identity-"));
  const bin = path.join(root, "fake-codex.mjs");
  await writeFile(bin, FAKE_CODEX, { mode: 0o755 });
  const codexHome = path.join(root, "codex-home");
  await mkdir(codexHome, { recursive: true });
  const configPath = path.join(root, "config.json");
  await writeFile(configPath, JSON.stringify({ backend: "cli", host: "127.0.0.1", workspace: root,
    codex: { bin, home: codexHome, sandbox: "read-only" }, feishu: { inboundEnabled: false }, ...config }));
  return { root, configPath, env: childEnv(root, codexHome) };
}

async function startBridge({ box, args = [] }) {
  const child = spawn(process.execPath, [CLI_ENTRY, "--config", box.configPath, "--port", "0", ...args],
    { cwd: box.root, env: box.env, stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "", stderr = "";
  child.stdout.setEncoding("utf8"); child.stderr.setEncoding("utf8");
  child.stdout.on("data", chunk => { stdout += chunk; });
  child.stderr.on("data", chunk => { stderr += chunk; });
  const base = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`bridge did not listen.\nstdout: ${stdout}\nstderr: ${stderr}`)), 20000);
    const poll = setInterval(() => {
      const match = stdout.match(/listening on (http:\/\/[^\s]+)/);
      if (!match) return;
      clearInterval(poll); clearTimeout(timer); resolve(match[1]);
    }, 25);
    child.once("exit", code => {
      clearInterval(poll); clearTimeout(timer);
      reject(new Error(`bridge exited before listening (${code}).\nstdout: ${stdout}\nstderr: ${stderr}`));
    });
  });
  const stop = async () => {
    if (child.exitCode === null) {
      const exited = new Promise(resolve => child.once("exit", resolve));
      child.kill("SIGTERM");
      await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 5000))]);
      if (child.exitCode === null) child.kill("SIGKILL");
    }
    await rm(box.root, { recursive: true, force: true });
  };
  return { base, stdout: () => stdout, stderr: () => stderr, stop };
}

async function runToExit(box, args) {
  const child = spawn(process.execPath, [CLI_ENTRY, ...args], { cwd: box.root, env: box.env,
    stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "", stderr = "";
  child.stdout.setEncoding("utf8"); child.stderr.setEncoding("utf8");
  child.stdout.on("data", chunk => { stdout += chunk; });
  child.stderr.on("data", chunk => { stderr += chunk; });
  const code = await new Promise(resolve => {
    const timer = setTimeout(() => { child.kill("SIGKILL"); resolve("timeout"); }, 15000);
    child.once("exit", value => { clearTimeout(timer); resolve(value); });
  });
  return { code, stdout, stderr };
}

async function waitForLog(file, needle) {
  for (let attempt = 0; attempt < 100; attempt++) {
    try { const text = await readFile(file, "utf8"); if (text.includes(needle)) return text; }
    catch { /* not flushed yet */ }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`log ${file} never mentioned ${needle}`);
}

// ---------------------------------------------------------------------------
// Resolution rules (unit level: no process, no network)
// ---------------------------------------------------------------------------

test("identity.json wins over config.bridgeName and supplies name/display/port", () => {
  const identity = resolveIdentity({
    identity: { file: "/tmp/identity.json", value: { bridge: "CodexWorker", name: "OguriCap", display: "小栗帽", port: 8905 } },
    config: { bridgeName: "旧配置名" },
  });
  assert.equal(identity.name, "OguriCap");
  assert.equal(identity.display, "小栗帽");
  assert.equal(identity.bridge, "CodexWorker");
  assert.equal(identity.port, 8905);
  assert.equal(identity.source, "identity.json");
});

test("without identity.json the legacy config.bridgeName still boots an instance", () => {
  const identity = resolveIdentity({ config: { bridgeName: "鲁道夫" } });
  assert.equal(identity.name, "鲁道夫");
  assert.equal(identity.display, "鲁道夫");
  assert.equal(identity.source, "config.bridgeName");
});

test("no identity anywhere refuses to start (and a shell name is not an identity)", () => {
  for (const config of [{}, { bridgeName: "   " }, { bridgeName: "CodexWorker" }, { bridgeName: "codex" }]) {
    assert.throws(() => resolveIdentity({ config }), error => {
      assert.equal(error.message, IDENTITY_REQUIRED_MESSAGE, JSON.stringify(config));
      return true;
    });
  }
});

test("an identity file without display is an error, not a fall-through", () => {
  assert.throws(() => resolveIdentity({ identity: { file: "/tmp/identity.json", value: { name: "OguriCap" } },
    config: { bridgeName: "旧配置名" } }), error => {
    assert.match(error.message, /缺少实例身份/);
    assert.match(error.message, /display/);
    return true;
  });
});

// ---------------------------------------------------------------------------
// Card titles are the display name: the channel titles every card with the
// bridgeName it is constructed with, and the CLI hands it identity.display.
// ---------------------------------------------------------------------------

test("the Feishu channel carries the instance display name into card titles", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "codexworker-identity-title-"));
  const store = await new SessionStore(path.join(root, "history"), { logger: () => {} }).init();
  try {
    const channel = new CliFeishuChannel({ adapter: {}, sessionStore: store, sessionsPath: path.join(root, "sessions.json"),
      bridgeName: SHIPPED.display, feishu: {} });
    assert.equal(channel.status().displayName, SHIPPED.display);
    assert.equal(channel.notifier.bridgeName, SHIPPED.display);
  } finally { await rm(root, { recursive: true, force: true }); }
});

// ---------------------------------------------------------------------------
// a) the shipped identity.json drives health, log prefix, state dir and log file
// ---------------------------------------------------------------------------

test("identity.json drives /health (name/display/port), the log prefix, the state dir and the log file name", async () => {
  const box = await sandbox({ config: { bridgeName: "旧配置名" } });
  const logTemplate = path.join(box.root, "logs", "{name}.jsonl");
  const bridge = await startBridge({ box, args: ["--log", logTemplate] });
  try {
    const health = await requestJson(bridge.base, "/health");
    assert.equal(health.status, 200);
    assert.equal(health.json.name, SHIPPED.name);
    assert.equal(health.json.display, SHIPPED.display);
    assert.equal(health.json.bridge, SHIPPED.bridge);
    assert.equal(health.json.port, Number(new URL(bridge.base).port));
    assert.equal(health.json.machine, SHIPPED.name);

    // Internal paths follow `name`, never the shell name or the display.
    assert.equal(health.json.history.root, path.join(box.root, ".codex", "state", SHIPPED.name, "history"));
    assert.equal(path.basename(path.dirname(path.dirname(health.json.history.root))), "state");

    const logPath = path.join(box.root, "logs", `${SHIPPED.name}.jsonl`);
    await waitForLog(logPath, "identity_loaded");
    const line = (await readFile(logPath, "utf8")).split("\n").filter(Boolean)
      .map(text => JSON.parse(text)).find(entry => entry.event === "identity_loaded");
    assert.equal(line.name, SHIPPED.name);
    assert.equal(line.display, SHIPPED.display);
    assert.equal(line.source, "identity.json");

    // bridgeName in the bridge config is superseded, and said so once.
    const text = await readFile(logPath, "utf8");
    assert.match(text, /identity_config_bridge_ignored/);
    assert.match(text, new RegExp(`"bridgeName":"旧配置名"`));

    // User-facing surfaces carry the display name; the shell name appears nowhere.
    assert.match(bridge.stdout(), new RegExp(`^${SHIPPED.display} listening on http`));
    assert.match(bridge.stderr(), new RegExp(`\\[${SHIPPED.display}\\] info identity_loaded`));
    assert.doesNotMatch(bridge.stdout(), /CodexWorker listening/);
    assert.doesNotMatch(bridge.stderr(), /\[CodexWorker\]/);
    assert.doesNotMatch(JSON.stringify(health.json), /"name":"CodexWorker"/);
  } finally { await bridge.stop(); }
});

// ---------------------------------------------------------------------------
// b) an identity file that exists but lacks display fails the boot with exit 2
// ---------------------------------------------------------------------------

test("an identity file missing display fails the boot with exit 2 and 缺少实例身份", async () => {
  const box = await sandbox();
  const file = path.join(box.root, "broken-identity.json");
  await writeFile(file, JSON.stringify({ bridge: "CodexWorker", name: "NoDisplay" }));
  try {
    const result = await runToExit(box, ["--identity", file, "--port", "0"]);
    assert.equal(result.code, 2);
    assert.match(result.stderr, /缺少实例身份/);
    assert.match(result.stderr, /display/);
    assert.doesNotMatch(result.stdout, /listening on/);
  } finally { await rm(box.root, { recursive: true, force: true }); }
});

test("a missing --identity file fails the boot with exit 2 instead of falling back", async () => {
  const box = await sandbox({ config: { bridgeName: "旧配置名" } });
  try {
    const result = await runToExit(box, ["--identity", path.join(box.root, "nope.json"), "--port", "0"]);
    assert.equal(result.code, 2);
    assert.match(result.stderr, /缺少实例身份/);
  } finally { await rm(box.root, { recursive: true, force: true }); }
});

// ---------------------------------------------------------------------------
// c) --identity <path> points the instance at another identity file
// ---------------------------------------------------------------------------

test("--identity <path> overrides the shipped identity for name/display/state", async () => {
  const box = await sandbox({ config: { bridgeName: "旧配置名" } });
  const identityPath = path.join(box.root, "identity.json");
  const identity = { bridge: "CodexWorker", name: `Temp${randomUUID().slice(0, 8)}`, display: "临时实例", port: 0 };
  await writeFile(identityPath, JSON.stringify(identity));
  const bridge = await startBridge({ box, args: ["--identity", identityPath] });
  try {
    const health = await requestJson(bridge.base, "/health");
    assert.equal(health.json.name, identity.name);
    assert.equal(health.json.display, "临时实例");
    assert.equal(health.json.history.root, path.join(box.root, ".codex", "state", identity.name, "history"));
    assert.match(bridge.stdout(), /^临时实例 listening on http/);
  } finally { await bridge.stop(); }
});
