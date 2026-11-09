#!/usr/bin/env node
// CodexWorker CLI entry.
//
// Two modes:
//   serve (default) — the CLI-native bridge: /health, /version plus the
//                     DSH-compatible /threads, /turn and /manager/* surface,
//                     backed by `codex exec` through native-cli.mjs.
//   mcp             — sets the resolved environment and imports the original
//                     server.mjs stdio MCP entry unchanged (Desktop path).
//
// Backend selection: when CODEX_APP_TOOLS_PIPE_PATH exists the Desktop backend
// is selected, exactly like the plugin host. A standalone CLI process cannot
// attach that pipe, so it refuses to pretend: /health reports the Desktop
// backend as not attached and points at `codexworker mcp` inside the plugin
// host. Without the pipe the native-cli backend is used. `"backend": "cli"` in
// the config forces native-cli even if a pipe variable leaks into the shell.
import http from "node:http";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { randomUUID } from "node:crypto";
import { appendFile, mkdir, readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { CliNativeAdapter, detectCodexVersion, httpError, probeNativeBackend, resolveCodexBinary } from "./native-cli.mjs";
import { SessionStore } from "./session-store.mjs";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const NAME = "CodexWorker";
process.title = NAME;

const pkg = JSON.parse(await readFile(path.join(ROOT, "package.json"), "utf8"));
const VERSION = pkg.version;

const HELP = `CodexWorker ${VERSION}

用法:
  codexworker [serve] [选项]        启动 CLI 原生 Bridge（/health /version /threads /turn /manager/*）
  codexworker mcp [选项]            透传启动原 server.mjs（MCP stdio，Desktop 插件宿主）

选项:
  --config <file.json>  读取配置（backend/workspace/codex/host/port/bridgeName/tokenFile）
  --host <addr>         监听地址（默认 127.0.0.1）
  --port <n>            监听端口（默认 8903；0 表示随机空闲端口）
  --backend <cli|desktop|auto>
                        原生后端（默认 auto：有 CODEX_APP_TOOLS_PIPE_PATH 走 Desktop，否则 CLI）
  --log <file>          追加 JSONL 日志（默认写 stderr）
  --stdio               mcp 的别名
  -h, --help            显示本帮助
  -v, --version         显示版本

环境变量:
  CODEXWORKER_CONFIG, CODEXWORKER_HOST, CODEXWORKER_PORT, CODEXWORKER_LOG,
  CODEXWORKER_STATE_DIR, CODEXWORKER_CODEX_BIN, CODEX_HOME

CLI 模式示例:
  ./bin/codexworker --config ./codexworker.config.example.json --port 8904
  curl -sS -X POST http://127.0.0.1:8904/threads -d '{}'
  curl -sS -X POST http://127.0.0.1:8904/turn -d '{"threadId":"<id>","text":"只回复两个字母：OK"}'

注意: CLI 模式用 \`codex exec\` 代替 Codex Desktop 的注入管道；工具审批、图片和
      MCP 工具行为与 Desktop 不同，详见 README「CLI 模式（native-cli）」。
`;

const logFile = { path: null, chain: Promise.resolve() };
function stamp() { return new Date().toISOString(); }
function log(level, event, fields = {}) {
  const line = `${stamp()} [${NAME}] ${level} ${event}` +
    (Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : "");
  process.stderr.write(line + "\n");
  if (!logFile.path) return;
  const payload = JSON.stringify({ ts: stamp(), level, event, pid: process.pid, ...fields }) + "\n";
  logFile.chain = logFile.chain.then(() => appendFile(logFile.path, payload)).catch(() => {});
}

function fail(message, code = 2) {
  process.stderr.write(`${NAME}: ${message}\n`);
  process.exit(code);
}

function expandHome(value) {
  if (typeof value !== "string") return value;
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

function parseArgs(argv) {
  const out = { command: "serve", config: null, host: null, port: null, log: null, backend: null };
  const take = (i, flag) => {
    const value = argv[i];
    if (value == null || value.startsWith("--")) fail(`${flag} 需要一个值`);
    return value;
  };
  for (let i = 0; i < argv.length; i++) {
    let arg = argv[i];
    if (arg === "-h" || arg === "--help") { process.stdout.write(HELP); process.exit(0); }
    if (arg === "-v" || arg === "--version") { process.stdout.write(`${NAME} ${VERSION}\n`); process.exit(0); }
    if (arg === "serve" || arg === "mcp") { out.command = arg; continue; }
    if (arg === "--stdio") { out.command = "mcp"; continue; }
    let inline = null;
    const eq = arg.indexOf("=");
    if (arg.startsWith("--") && eq > 2) { inline = arg.slice(eq + 1); arg = arg.slice(0, eq); }
    const value = inline ?? null;
    switch (arg) {
      case "--config": out.config = expandHome(value ?? take(++i, arg)); break;
      case "--host": out.host = value ?? take(++i, arg); break;
      case "--port": out.port = value ?? take(++i, arg); break;
      case "--log": out.log = expandHome(value ?? take(++i, arg)); break;
      case "--backend": out.backend = value ?? take(++i, arg); break;
      default: fail(`未知参数: ${arg}（用 --help 查看用法）`);
    }
  }
  if (out.backend && !["cli", "desktop", "auto"].includes(out.backend)) fail(`--backend 只接受 cli|desktop|auto: ${out.backend}`);
  return out;
}

async function loadConfig(file) {
  if (!file) return {};
  let raw;
  try { raw = await readFile(file, "utf8"); }
  catch (error) { fail(`无法读取配置文件 ${file}: ${error.code ?? error.message}`); }
  let value;
  try { value = JSON.parse(raw); }
  catch (error) { fail(`配置文件不是合法 JSON (${file}): ${error.message}`); }
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`配置必须是 JSON 对象: ${file}`);
  return value;
}

function resolvePort(value, fallback) {
  if (value == null || value === "") return fallback;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 0 || port > 65535) fail(`端口不合法: ${value}`);
  return port;
}

const args = parseArgs(process.argv.slice(2));
const config = await loadConfig(args.config ? path.resolve(args.config) : null);
if (args.backend) config.backend = args.backend === "auto" ? undefined : args.backend;

const host = args.host ?? process.env.CODEXWORKER_HOST ?? config.host ?? "127.0.0.1";
const port = resolvePort(args.port ?? process.env.CODEXWORKER_PORT ?? config.port, 8903);
const stateDir = expandHome(process.env.CODEXWORKER_STATE_DIR ??
  path.join(os.homedir(), ".codex", "state", "codexworker"));
const resolvedLog = args.log ?? process.env.CODEXWORKER_LOG ?? null;
if (resolvedLog) {
  logFile.path = path.resolve(expandHome(resolvedLog));
  await mkdir(path.dirname(logFile.path), { recursive: true });
}

// The copied modules read these variables; set them before importing server.mjs.
process.env.CODEXWORKER_CONFIG = args.config ? path.resolve(args.config) : (process.env.CODEXWORKER_CONFIG ?? "");
process.env.CODEXWORKER_HOST = host;
process.env.CODEXWORKER_PORT = String(port);
process.env.CODEXWORKER_STATE_DIR = stateDir;
if (logFile.path) process.env.CODEXWORKER_LOG = logFile.path;

log("info", "start", { mode: args.command, version: VERSION, host, port, stateDir,
  config: args.config ? path.resolve(args.config) : null });

function loopback(address) {
  return address === "127.0.0.1" || address === "::1" || address === "localhost";
}

async function loadToken(settings) {
  if (typeof settings.token === "string" && settings.token.length >= 24) return settings.token;
  const file = settings.tokenFile ? expandHome(settings.tokenFile) : null;
  if (!file) return null;
  try {
    const value = (await readFile(file, "utf8")).trim();
    if (value.length < 24) fail(`tokenFile 中的 token 少于 24 字符: ${file}`);
    return value;
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

if (args.command === "mcp") {
  if (!process.env.CODEX_APP_TOOLS_PIPE_PATH) {
    log("warn", "desktop_pipe_missing", {
      detail: "CODEX_APP_TOOLS_PIPE_PATH is not set; the MCP entry will start but native task calls cannot succeed. Use `codexworker serve` for CLI turns." });
  }
  // Original stdio entry, imported after the environment is resolved.
  await import("./server.mjs");
} else {
  const probe = probeNativeBackend(process.env, config);
  const startedAt = new Date().toISOString();
  const startedMs = Date.now();
  let requests = 0, at = 0;

  if (probe.backend === "desktop") {
    // The standalone CLI cannot attach the Desktop pipe. Keep the honest
    // liveness-only surface instead of silently switching to another backend.
    log("warn", "desktop_backend_unattached", { pipeConfigured: probe.pipeConfigured });
    const limitations = [
      "backend=desktop was selected by CODEX_APP_TOOLS_PIPE_PATH (or config), but only the Codex Desktop plugin host can attach that pipe.",
      "Start `codexworker mcp` from the Desktop plugin host for the Desktop backend, or set \"backend\": \"cli\" / unset CODEX_APP_TOOLS_PIPE_PATH for native-cli.",
      "This process exposes liveness only: GET /health and GET /version. All other routes return 503/404."
    ];
    const health = () => ({
      name: config.bridgeName ?? NAME, version: VERSION, alive: true, ready: false, mode: "cli",
      backend: "desktop", operatingMode: config.operatingMode ?? "desktop-bridge", integrationMode: "cli-entry",
      host, port: server.address()?.port ?? port, pid: process.pid, startedAt,
      uptimeSec: Number(((Date.now() - startedMs) / 1000).toFixed(3)), requests,
      desktopNative: { available: true, attached: false, backend: "desktop",
        pipeConfigured: probe.pipeConfigured, reason: probe.reason },
      nativeCli: { available: false, backend: "cli", reason: "Not selected while backend=desktop." },
      managerReady: false, nativeTaskReady: false,
      capabilities: { health: true, version: true, mcpStdio: true, managerHttp: false, desktopNative: false, cliNative: false },
      limits: limitations
    });
    const send = (res, status, body, method = "GET") => {
      const text = JSON.stringify(body, null, 2) + "\n";
      res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store",
        "content-length": Buffer.byteLength(text) });
      if (method === "HEAD") res.end(); else res.end(text);
    };
    const server = http.createServer((req, res) => {
      requests++;
      const method = req.method ?? "GET";
      let pathname;
      try { pathname = new URL(req.url ?? "/", `http://${host}`).pathname; }
      catch { return send(res, 400, { error: "bad_request" }, method); }
      if (method === "GET" && pathname === "/health") return send(res, 200, health(), method);
      if (method === "GET" && pathname === "/version") return send(res, 200, { name: NAME, version: VERSION, mode: "cli", backend: "desktop" }, method);
      if (pathname === "/") return send(res, 200, { name: NAME, version: VERSION, backend: "desktop", endpoints: ["/health", "/version"] }, method);
      return send(res, 503, { error: "desktop_pipe_unattached", detail: probe.reason,
        hint: "Run `codexworker mcp` inside the Codex Desktop plugin host, or force the CLI backend with --backend cli.", path: pathname }, method);
    });
    server.on("error", error => { log("error", "listen_failed", { code: error.code, message: error.message, host, port }); process.exit(1); });
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(port, host, resolve); })
      .catch(error => { log("error", "listen_failed", { code: error.code, message: error.message, host, port }); process.exit(1); });
    const actualPort = server.address().port;
    log("info", "listening", { url: `http://${host}:${actualPort}`, backend: "desktop" });
    process.stdout.write(`${NAME} listening on http://${host}:${actualPort} (backend=desktop, liveness only)\n`);
    let closing = false;
    const shutdown = signal => {
      if (closing) return; closing = true;
      log("info", "shutdown", { signal });
      server.closeIdleConnections?.();
      server.close(() => { void logFile.chain.then(() => process.exit(0)); });
      const timer = setTimeout(() => process.exit(0), 3000); timer.unref();
    };
    process.on("SIGINT", () => shutdown("SIGINT"));
    process.on("SIGTERM", () => shutdown("SIGTERM"));
  } else {
    const token = await loadToken(config);
    if (!loopback(host) && !token) fail("监听非回环地址时必须配置 token 或 tokenFile（≥24 字符）", 2);

    const codex = config.codex ?? {};
    const codexBin = await resolveCodexBinary({
      bin: codex.bin ?? process.env.CODEXWORKER_CODEX_BIN ?? null }, process.env);
    const adapter = new CliNativeAdapter({
      codexBin,
      codexHome: codex.home ?? process.env.CODEX_HOME ?? null,
      stateDir,
      defaultCwd: config.workspace ?? config.cwd ?? process.cwd(),
      turnTimeoutMs: config.turnTimeoutMs ?? codex.turnTimeoutMs ?? 600000,
      sandbox: codex.sandbox,
      approvalPolicy: codex.approvalPolicy,
      model: codex.model ?? null,
      extraArgs: codex.extraArgs ?? [],
      proxy: codex.proxy ?? null,
      env: process.env,
      logger: (level, event, fields) => log(level, event, fields)
    });
    await adapter.init();
    const codexVersion = adapter.codexVersion ?? await detectCodexVersion(codexBin, process.env);

    // Bridge-owned history: /threads, transcripts and card history read from
    // here. The codex rollout jsonl is only the process stream.
    const history = await new SessionStore(path.join(stateDir, "history"), { logger: log }).init();

    // Feishu long connection (CLI mode). Optional: a missing/broken credential
    // file must never take the HTTP bridge down, so failures are logged and the
    // channel stays disabled.
    let feishuChannel = null;
    if (config.feishu?.inboundEnabled) {
      try {
        const { startCliFeishuChannel } = await import("./feishu-cli.mjs");
        feishuChannel = await startCliFeishuChannel({
          config, adapter, stateDir, bridgeName: config.bridgeName ?? NAME, sessionStore: history,
          logger: (level, event, fields) => log(level, event, fields ?? {})
        });
        log("info", "feishu_channel_ready", feishuChannel.status().connection ? {
          phase: feishuChannel.status().phase, whitelistCount: feishuChannel.status().whitelistCount
        } : {});
      } catch (error) {
        log("error", "feishu_channel_unavailable", { message: error?.message ?? String(error) });
      }
    }

    const limitations = [
      "Turns run `codex exec` as a child process; there is no Desktop session except the one Codex itself records under CODEX_HOME/sessions.",
      "exec --json emits message-level events, not token deltas: no streaming text, no interactive tool-approval prompt. approval_policy is fixed by config (default never).",
      "Images cannot be attached yet (only text is passed; codex exec --image is not wired to /turn).",
      "MCP tools follow the codex CLI config; Desktop-only native tools (read_thread/send_message_to_thread/...) do not exist in this backend.",
      "fork/compact/stop/unsubscribe are not implemented (release/unsubscribe answer ok as no-ops).",
      "Feishu inbound (long connection) runs only when feishu.inboundEnabled; online announcements, public image output, question cards, slash commands and the Desktop writer lease are not started by the CLI.",
      "The event stream (GET /manager/events) is in-process only; transcripts are rebuilt from the bridge-owned session store (~/.codex/state/codexworker/history), not from codex rollouts."
    ];

    const health = () => ({
      name: config.bridgeName ?? NAME, version: VERSION, alive: true,
      ready: Boolean(adapter.status().available), mode: "cli", backend: "cli",
      operatingMode: config.operatingMode ?? "cli-native", integrationMode: "cli-native", machine: config.machine ?? "codexworker",
      host, port: server.address()?.port ?? port, pid: process.pid, startedAt,
      uptimeSec: Number(((Date.now() - startedMs) / 1000).toFixed(3)), requests,
      eventSeq: adapter.state.seq,
      workspace: adapter.defaultCwd, workspaces: config.workspaceRoots ?? [adapter.defaultCwd],
      activeTurns: adapter.running.size, poolSize: Object.keys(adapter.state.threads).length,
      managerReady: true, nativeTaskReady: true,
      desktopNative: { available: true, attached: false, backend: "cli", pipeConfigured: probe.pipeConfigured,
        reason: probe.reason },
      nativeCli: { ...adapter.status(), backend: "cli", codexBinPresent: Boolean(codexBin), codexVersion },
      history: { root: history.root, impl: "jsonl-per-session", authoritative: true },
      feishu: feishuChannel ? feishuChannel.status()
        : { inboundEnabled: false, enabled: false, reason: config.feishu?.inboundEnabled ? "start_failed" : "disabled" },
      capabilities: { health: true, version: true, mcpStdio: true, managerHttp: true, desktopNative: false,
        cliNative: true, turn: true, threads: true, resume: true, history: true, tokenStreaming: false,
        feishuInbound: Boolean(feishuChannel) },
      limits: limitations
    });

    const send = (res, status, body, method = "GET") => {
      const text = JSON.stringify(body, null, 2) + "\n";
      res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store",
        "content-length": Buffer.byteLength(text) });
      if (method === "HEAD") res.end(); else res.end(text);
    };

    const readBody = async req => {
      let size = 0; const chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 2 * 1024 * 1024) throw httpError(413, "request_too_large", "Request body exceeds 2 MB.");
        chunks.push(chunk);
      }
      if (!chunks.length) return {};
      let value;
      try { value = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
      catch { throw httpError(400, "invalid_json", "Request body must be JSON."); }
      if (!value || typeof value !== "object" || Array.isArray(value)) throw httpError(400, "invalid_request", "Request body must be a JSON object.");
      return value;
    };

    const authorized = req => {
      if (!token) return true;
      const actual = Buffer.from(req.headers.authorization ?? "");
      const expected = Buffer.from(`Bearer ${token}`);
      return actual.length === expected.length && actual.equals(expected);
    };

    // Every turn is recorded in the bridge-owned history before it is dispatched
    // and after it replies; /threads and transcripts never read codex rollouts.
    const runTurn = async (body, { source = "http" } = {}) => {
      const text = body.text ?? body.message ?? body.prompt;
      if (typeof text !== "string" || !text.trim()) throw httpError(400, "invalid_request", "text is required.");
      const messageId = typeof body.messageId === "string" && body.messageId ? body.messageId : randomUUID();
      if (body.threadId != null && typeof body.threadId !== "string") throw httpError(400, "invalid_request", "threadId must be a string.");
      let threadId = body.threadId ?? null;
      const title = text.replace(/\s+/g, " ").slice(0, 80);
      if (threadId) {
        await adapter.adopt(threadId).catch(error => {
          if (error.status !== 404) throw error;
          // A caller-chosen id that is not a recorded session becomes a fresh
          // conversation key; it is replaced by the real session id afterwards.
        });
        await history.ensure({ threadId, cwd: body.cwd ?? null, source, title });
      } else {
        const thread = await adapter.register({ cwd: body.cwd ?? null, title });
        threadId = thread.id;
        await history.create({ threadId, cwd: thread.cwd ?? null, source, title: thread.title ?? title });
      }
      await history.appendMessage(threadId, { role: "user", text, messageId, source });
      const result = await adapter.dispatch({ threadId, text, cwd: body.cwd ?? null,
        messageId, onEvent: event => pushStream(event) });
      if (result.resultText && String(result.resultText).trim()) {
        await history.appendMessage(threadId, { role: "assistant", text: result.resultText,
          messageId: result.turnId ?? null, turnId: result.turnId ?? null, source });
      }
      await history.setMeta(threadId, { sessionId: result.sessionId ?? null, updatedAt: new Date().toISOString() });
      return { ...result, messageId };
    };

    // One-time import for sessions that only exist as codex rollouts (adopt).
    // After this the store is authoritative; rollouts are never read again.
    const importTranscript = async threadId => {
      const existing = await history.read(threadId, { limit: 1 });
      if (existing.ok && existing.totalMessages > 0) return 0;
      let snapshot;
      try { snapshot = await adapter.readThread(threadId, { turnLimit: 50 }); }
      catch { return 0; }
      const messages = [];
      for (const turn of snapshot.turns ?? []) {
        for (const item of turn.items ?? []) {
          if (!item?.text) continue;
          if (item.type === "userMessage") messages.push({ role: "user", text: item.text,
            messageId: item.id ?? null, ts: turn.createdAt ?? null, source: "cli" });
          else if (item.type === "agentMessage") messages.push({ role: "assistant", text: item.text,
            messageId: item.id ?? null, turnId: turn.id ?? null,
            ts: turn.completedAt ?? turn.createdAt ?? null, source: "cli" });
        }
      }
      if (messages.length) await history.appendMessages(threadId, messages);
      return messages.length;
    };

    const streams = new Set();
    const pushStream = event => {
      for (const res of streams) {
        try { res.write(`id: ${event.seq}\nevent: bridge\ndata: ${JSON.stringify(event)}\n\n`); }
        catch { /* closed below */ }
      }
    };

    const server = http.createServer(async (req, res) => {
      requests++;
      const method = req.method ?? "GET";
      let url, body = {};
      try {
        url = new URL(req.url ?? "/", `http://${host}`);
      } catch { return send(res, 400, { error: "bad_request" }, method); }
      try {
        const publicRoute = method === "GET" && ["/", "/health", "/version"].includes(url.pathname);
        if (!publicRoute && !authorized(req)) throw httpError(401, "unauthorized", "Valid Bearer token required.");
        if (method === "GET" && url.pathname === "/") {
          return send(res, 200, { name: NAME, version: VERSION, mode: "cli", backend: "cli",
            endpoints: ["GET /health", "GET /version", "GET /threads", "POST /threads", "POST /turn",
              "GET /threads/:id", "GET /threads/:id/last-turn", "POST /threads/:id/resume",
              "GET /manager/sessions", "POST /manager/messages", "GET /manager/events", "POST /manager/sessions/adopt"] }, method);
        }
        if (method === "GET" && url.pathname === "/health") return send(res, 200, health(), method);
        if (method === "GET" && url.pathname === "/version") {
          return send(res, 200, { name: NAME, version: VERSION, mode: "cli", backend: "cli", codexVersion }, method);
        }
        if (method === "GET" && url.pathname === "/threads") {
          const limit = Number(url.searchParams.get("limit") ?? 50);
          return send(res, 200, { threads: await history.list({ limit }) }, method);
        }
        if (method === "POST" && url.pathname === "/threads") {
          body = await readBody(req);
          const thread = await adapter.register({ cwd: body.cwd ?? null, title: body.title ?? null });
          await history.create({ threadId: thread.id, cwd: thread.cwd ?? null, source: "http",
            title: body.title ?? null, createdAt: thread.createdAt });
          return send(res, 200, { thread: { id: thread.id, cwd: thread.cwd, createdAt: thread.createdAt } }, method);
        }
        if (method === "POST" && url.pathname === "/turn") {
          body = await readBody(req);
          const wait = body.wait !== false;
          if (!wait) {
            // Give an id-less caller a stable handle before the async turn runs.
            const threadId = body.threadId ?? (await adapter.register({ cwd: body.cwd ?? null })).id;
            const messageId = body.messageId ?? randomUUID();
            void runTurn({ ...body, threadId, messageId })
              .catch(error => log("warn", "turn_failed", { threadId, error: error.body?.error ?? error.message }));
            return send(res, 202, { threadId, turnId: null, messageId, action: "dispatch_pending",
              status: "started", nativeTurnPending: true }, method);
          }
          const result = await runTurn(body);
          return send(res, 200, { threadId: result.threadId, sessionId: result.sessionId ?? null,
            turnId: result.turnId, messageId: result.messageId ?? null, status: result.status,
            resultText: result.resultText, reason: result.reason, toolCalls: result.toolCalls ?? [],
            usage: result.usage ?? null, durationMs: result.durationMs ?? null }, method);
        }
        if (method === "GET" && url.pathname === "/manager/events") {
          const after = Number(url.searchParams.get("after") ?? req.headers["last-event-id"] ?? 0);
          const page = adapter.eventsAfter(after, Number(url.searchParams.get("limit") ?? 500));
          if (url.searchParams.get("stream") === "0") return send(res, 200, page, method);
          res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
          for (const event of page.events) res.write(`id: ${event.seq}\nevent: bridge\ndata: ${JSON.stringify(event)}\n\n`);
          res.write(": connected\n\n");
          streams.add(res);
          const timer = setInterval(() => res.write(": heartbeat\n\n"), 15000); timer.unref();
          res.on("close", () => { clearInterval(timer); streams.delete(res); });
          return;
        }
        if (method === "GET" && url.pathname === "/manager/sessions") {
          const threads = await history.list({ limit: Number(url.searchParams.get("limit") ?? 50) });
          const sessions = threads.map(thread => ({ threadId: thread.id, sessionId: thread.sessionId,
            cwd: thread.cwd, createdAt: thread.createdAt, updatedAt: thread.updatedAt,
            alias: thread.title, hostId: "local", live: thread.live, turns: thread.turns }));
          return send(res, 200, { sessions, selectedThreadId: sessions[0]?.threadId ?? null }, method);
        }
        if (method === "POST" && url.pathname === "/manager/messages") {
          body = await readBody(req);
          const target = body.targetThreadId ?? body.threadId ?? null;
          const result = await runTurn({ ...body, threadId: target });
          return send(res, 200, { threadId: result.threadId, sessionId: result.sessionId ?? null,
            messageId: result.messageId ?? null, action: "completed", status: result.status,
            resultText: result.resultText, reason: result.reason }, method);
        }
        const adopt = url.pathname.match(/^\/manager\/sessions\/([^/]+)\/adopt$|^\/manager\/sessions\/adopt$/);
        if (method === "POST" && adopt) {
          body = await readBody(req);
          const id = adopt[1] ? decodeURIComponent(adopt[1]) : (body.threadId ?? body.id);
          if (typeof id !== "string" || !id) throw httpError(400, "invalid_request", "threadId is required.");
          const thread = await adapter.adopt(id);
          await history.ensure({ threadId: thread.id, cwd: thread.cwd ?? null, source: "http",
            title: thread.title ?? null, sessionId: thread.sessionId ?? null });
          if (thread.sessionId) await history.setMeta(thread.id, { sessionId: thread.sessionId });
          const imported = await importTranscript(thread.id);
          return send(res, 200, { thread: { id: thread.id, cwd: thread.cwd, createdAt: thread.createdAt,
            turns: thread.turns }, history: { messages: imported } }, method);
        }
        const match = url.pathname.match(/^\/(?:manager\/sessions|threads)\/([^/]+)(?:\/([a-z-]+))?$/);
        if (match) {
          const id = decodeURIComponent(match[1]);
          const operation = match[2];
          if (method === "GET" && !operation) {
            return send(res, 200, await history.readTurns(id, { turnLimit: Number(url.searchParams.get("turnLimit") ?? 10) }), method);
          }
          if (method === "GET" && operation === "last-turn") {
            const snapshot = await history.readTurns(id, { turnLimit: 1 });
            return send(res, 200, { threadId: id, turn: snapshot.turns.at(-1) ?? null,
              budget: { requestedTurns: 1, returnedTurns: snapshot.turns.length } }, method);
          }
          if (method === "GET" && operation === "progress") {
            return send(res, 200, adapter.progress(id), method);
          }
          if (method === "POST" && operation === "resume") {
            body = await readBody(req);
            if (typeof body.text === "string" && body.text.trim()) {
              const result = await runTurn({ ...body, threadId: id });
              return send(res, 200, { threadId: result.threadId, sessionId: result.sessionId,
                turnId: result.turnId, status: result.status, resultText: result.resultText, reason: result.reason }, method);
            }
            const thread = await adapter.adopt(id);
            return send(res, 200, { thread: { id: thread.id, live: true, cwd: thread.cwd, turns: thread.turns } }, method);
          }
          if (method === "POST" && ["release", "unsubscribe"].includes(operation)) {
            return send(res, 200, { ok: true, status: "notSubscribed",
              detail: "The CLI backend owns child processes per turn; there is no app-server subscription." }, method);
          }
          if (method === "POST" && ["stop", "compact", "fork"].includes(operation)) {
            throw httpError(501, "capability_unavailable", `The native-cli backend does not implement ${operation}.`);
          }
        }
        throw httpError(404, "not_found", "Unknown route.");
      } catch (error) {
        const mapped = error?.body ? error : httpError(500, "bridge_error", "CLI bridge request failed; inspect local logs.");
        if (!error?.body) log("error", "request_failed", { path: url.pathname, message: error?.message ?? String(error) });
        if (res.headersSent) { res.destroy(); return; }
        send(res, mapped.status ?? 500, { ...mapped.body, ...(body?.messageId ? { messageId: body.messageId } : {}) }, method);
      }
    });

    server.on("error", error => { log("error", "listen_failed", { code: error.code, message: error.message, host, port }); process.exit(1); });
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(port, host, resolve); })
      .catch(error => { log("error", "listen_failed", { code: error.code, message: error.message, host, port }); process.exit(1); });

    const actualPort = server.address().port;
    log("info", "listening", { url: `http://${host}:${actualPort}`, backend: "cli", codexBin, codexVersion,
      auth: token ? "bearer" : "loopback-open" });
    process.stdout.write(`${NAME} listening on http://${host}:${actualPort} (backend=cli, codexVersion=${codexVersion ?? "unavailable"})\n`);

    let closing = false;
    const shutdown = signal => {
      if (closing) return;
      closing = true;
      log("info", "shutdown", { signal });
      for (const stream of streams) { try { stream.end(); } catch { /* already closed */ } }
      streams.clear();
      server.closeIdleConnections?.();
      void Promise.allSettled([feishuChannel?.close(), adapter.close()]).finally(() => {
        server.close(() => { void logFile.chain.then(() => process.exit(0)); });
        const timer = setTimeout(() => process.exit(0), 3000); timer.unref();
      });
    };
    process.on("SIGINT", () => shutdown("SIGINT"));
    process.on("SIGTERM", () => shutdown("SIGTERM"));
    process.on("uncaughtException", error => {
      log("error", "uncaught_exception", { message: error?.message ?? String(error) });
      shutdown("uncaughtException");
    });
    process.on("unhandledRejection", error => {
      log("error", "unhandled_rejection", { message: error?.message ?? String(error) });
    });
  }
}
