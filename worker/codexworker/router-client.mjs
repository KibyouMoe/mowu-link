// Client for mowu-router (server/router/server.py).
//
// One worker == one SSE stream. The router pushes `event: task` frames; this
// client registers, keeps a 30s heartbeat, reports results/notices, and
// reconnects with 1s→2s→4s→…→30s backoff. Registration is idempotent, so every
// reconnect re-registers before opening the stream.
//
// No dependency on the manager or Feishu: this module is intentionally thin so
// the CLI can own the connection and desktop-manager can only read `status()`.
//
// Endpoint notes (verified against server.py 0.3.0):
//   POST /register  {name, version?, note?}        -> {ok, queued, new, ...}
//   POST /heartbeat {name, state?, task_id?}       -> 404 unknown_worker if not registered
//   POST /result    {name, task_id, status, summary, artifacts?, evidence?}
//   POST /notice    {worker, text, kind?, urgency?}
//   GET  /stream?name=<name>[&version=<v>]         -> text/event-stream
//   There is no worker-facing /ack: the router owns task claim/requeue, and a
//   reconnect re-delivers anything still marked running (at-least-once).
import process from "node:process";

const DEFAULT_HEARTBEAT_MS = 30_000;
const MIN_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;
const REQUEST_TIMEOUT_MS = 8_000;

let activeClient = null;

export function setActiveRouterClient(client) {
  activeClient = client ?? null;
}

/** Snapshot for GET /health `integrations.router`. Never contains the token. */
export function routerStatus() {
  if (activeClient) return activeClient.status();
  return {
    enabled: false, registered: false, connected: false, phase: "disabled",
    url: null, name: null, reconnects: 0, attempts: 0,
    connectedAt: null, disconnectedAt: null, lastError: null,
  };
}

export class RouterError extends Error {
  constructor(message, { status = null, code = null, body = null } = {}) {
    super(message);
    this.name = "RouterError";
    this.status = status;
    this.code = code ?? (status ? `http_${status}` : "transport_error");
    this.body = body;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function trimBase(url) {
  return String(url ?? "").replace(/\/+$/, "");
}

export class RouterClient {
  constructor({
    url = "http://127.0.0.1:8790",
    token,
    name,
    version = null,
    heartbeatMs = DEFAULT_HEARTBEAT_MS,
    minBackoffMs = MIN_BACKOFF_MS,
    maxBackoffMs = MAX_BACKOFF_MS,
    requestTimeoutMs = REQUEST_TIMEOUT_MS,
    logger = null,
  } = {}) {
    if (!name || typeof name !== "string") throw new Error("router client needs a worker name");
    if (!token || typeof token !== "string") throw new Error("router client needs a token");
    this.url = trimBase(url);
    this.token = token;
    this.name = name;
    this.version = version;
    this.heartbeatMs = Math.max(1000, Number(heartbeatMs) || DEFAULT_HEARTBEAT_MS);
    this.minBackoffMs = Math.max(100, Number(minBackoffMs) || MIN_BACKOFF_MS);
    this.maxBackoffMs = Math.max(this.minBackoffMs, Number(maxBackoffMs) || MAX_BACKOFF_MS);
    this.requestTimeoutMs = Math.max(500, Number(requestTimeoutMs) || REQUEST_TIMEOUT_MS);
    this.logger = logger ?? ((level, event, fields) => {
      const line = `${new Date().toISOString()} [router] ${level} ${event}` +
        (fields && Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : "");
      process.stderr.write(line + "\n");
    });

    this.running = false;
    this.registered = false;
    this.connected = false;
    this.phase = "idle";
    this.attempts = 0;          // stream connections established, ever
    this.reconnects = 0;        // attempts after the first successful one
    this.connectedAt = null;
    this.disconnectedAt = null;
    this.lastError = null;
    this.lastDisconnectReason = null;
    this.activity = { state: "idle", taskId: null };
    this.loopPromise = null;
    this.abort = null;
    this.sleepTimer = null;
    this.heartbeatTimer = null;
    this.heartbeatInFlight = false;
    this.handlers = {};
  }

  status() {
    return {
      enabled: this.running,
      registered: this.registered,
      connected: this.connected,
      phase: this.phase,
      url: this.url,
      name: this.name,
      reconnects: this.reconnects,
      attempts: this.attempts,
      connectedAt: this.connectedAt,
      disconnectedAt: this.disconnectedAt,
      lastDisconnectReason: this.lastDisconnectReason,
      lastError: this.lastError,
      activity: { ...this.activity },
    };
  }

  log(level, event, fields = {}) {
    try { this.logger(level, event, fields); } catch { /* logging must never break the loop */ }
  }

  headers() {
    return { "X-Router-Token": this.token, accept: "application/json, text/event-stream" };
  }

  async _request(method, path, body = null, { timeoutMs = this.requestTimeoutMs } = {}) {
    const init = {
      method,
      headers: this.headers(),
      signal: AbortSignal.timeout(timeoutMs),
    };
    if (body !== null) {
      init.headers["content-type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(this.url + path, init);
    } catch (error) {
      throw new RouterError(`router ${method} ${path} failed: ${error?.message ?? error}`,
        { code: "transport_error" });
    }
    const text = await response.text().catch(() => "");
    let parsed = null;
    if (text) {
      try { parsed = JSON.parse(text); } catch { parsed = { raw: text }; }
    }
    if (!response.ok) {
      const code = parsed?.error ?? `http_${response.status}`;
      throw new RouterError(
        `router ${method} ${path} -> ${response.status} ${code}`,
        { status: response.status, code, body: parsed });
    }
    return parsed ?? {};
  }

  async register() {
    const body = await this._request("POST", "/register", {
      name: this.name,
      ...(this.version ? { version: this.version } : {}),
      note: "codexworker router client",
    });
    this.registered = true;
    this.log("info", "router_register_ok", {
      name: this.name, new: body.new === true, queued: body.queued ?? null,
    });
    return body;
  }

  /** Send one heartbeat. `state` defaults to the activity the task runner set. */
  async heartbeat({ state, taskId, progressAt } = {}) {
    const body = await this._request("POST", "/heartbeat", {
      name: this.name,
      state: state ?? this.activity.state ?? "idle",
      ...((taskId ?? this.activity.taskId) ? { task_id: taskId ?? this.activity.taskId } : {}),
      ...(progressAt != null ? { progress_at: progressAt } : {}),
    });
    return body;
  }

  setActivity({ state = "idle", taskId = null } = {}) {
    this.activity = { state, taskId };
  }

  async result({ taskId, status, summary, artifacts = [], evidence = [] }) {
    if (!taskId) throw new Error("result needs a taskId");
    if (status !== "done" && status !== "failed") throw new Error("result status must be done or failed");
    return this._request("POST", "/result", {
      name: this.name,
      task_id: taskId,
      status,
      summary: String(summary ?? ""),
      artifacts: Array.isArray(artifacts) ? artifacts : [],
      evidence: Array.isArray(evidence) ? evidence : [],
    });
  }

  async notice({ text, worker = this.name, kind = "worker.notice", urgency = "normal" }) {
    if (!text || !String(text).trim()) throw new Error("notice needs text");
    return this._request("POST", "/notice", {
      worker, text: String(text), kind, urgency,
    });
  }

  /**
   * Connect and stay connected until stop().
   * `onTask(task)` is called for each `event: task` payload; it may return a
   * promise and is never awaited by the reader, so a long turn cannot stall pings.
   */
  start({ onTask, onConnect, onDisconnect, onEvent, onError } = {}) {
    if (this.running) return this;
    if (typeof onTask !== "function") throw new Error("router client start needs onTask");
    this.handlers = { onTask, onConnect, onDisconnect, onEvent, onError };
    this.running = true;
    this.phase = "connecting";
    this.loopPromise = this._loop().catch((error) => {
      this.log("error", "router_loop_crashed", { message: error?.message ?? String(error) });
      this.lastError = error?.message ?? String(error);
    });
    return this;
  }

  async stop({ timeoutMs = 3000 } = {}) {
    if (!this.running) return;
    this.running = false;
    this.phase = "stopping";
    this._clearHeartbeat();
    if (this.sleepTimer) { clearTimeout(this.sleepTimer); this.sleepTimer = null; }
    if (this.abort) { try { this.abort.abort(); } catch { /* already gone */ } }
    if (this.loopPromise) {
      await Promise.race([this.loopPromise, sleep(timeoutMs)]).catch(() => {});
    }
    this.connected = false;
    this.registered = false;
    this.phase = "stopped";
    this.log("info", "router_client_stopped", { name: this.name, reconnects: this.reconnects });
  }

  _wait(ms) {
    return new Promise((resolve) => {
      this.sleepTimer = setTimeout(() => { this.sleepTimer = null; resolve(); }, ms);
    });
  }

  async _loop() {
    let backoff = this.minBackoffMs;
    while (this.running) {
      let reason = null;
      const attemptStartedAt = Date.now();
      let streamed = false;
      this.phase = "connecting";
      try {
        await this.register();
        await this._consumeStream();  // resolves when the stream ends
        streamed = true;
        reason = this.lastDisconnectReason ?? "stream_ended";
      } catch (error) {
        reason = error?.message ?? String(error);
        this.lastError = reason;
        this.registered = false;
        this.log("warn", "router_stream_failed", {
          reason, code: error?.code ?? null, status: error?.status ?? null,
        });
      }
      this.connected = false;
      this.registered = false;
      this.disconnectedAt = new Date().toISOString();
      this._clearHeartbeat();
      if (!this.running) break;

      const delay = streamed && Date.now() - attemptStartedAt >= this.heartbeatMs
        ? this.minBackoffMs : backoff;
      // A stream that stayed healthy for a full heartbeat interval resets the
      // ladder; repeated short-lived failures keep backing off.
      backoff = Math.min(delay * 2, this.maxBackoffMs);
      this.phase = "reconnecting";
      this.log("warn", "router_reconnect_scheduled", {
        reason, delayMs: delay, attempts: this.attempts, reconnects: this.reconnects,
      });
      try { this.handlers.onDisconnect?.({ reason, delayMs: delay, reconnects: this.reconnects }); }
      catch (error) { this.log("warn", "router_on_disconnect_failed", { message: error?.message ?? String(error) }); }
      await this._wait(delay);
    }
    this.phase = this.running ? this.phase : "stopped";
  }

  streamUrl() {
    const params = new URLSearchParams({ name: this.name });
    if (this.version) params.set("version", this.version);
    return `${this.url}/stream?${params.toString()}`;
  }

  async _consumeStream() {
    const controller = new AbortController();
    this.abort = controller;
    let response;
    try {
      response = await fetch(this.streamUrl(), {
        headers: { ...this.headers(), accept: "text/event-stream" },
        signal: controller.signal,
      });
    } catch (error) {
      if (!this.running) return;
      throw new RouterError(`router stream connect failed: ${error?.message ?? error}`, { code: "stream_connect_failed" });
    }
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new RouterError(`router stream -> ${response.status} ${text.slice(0, 200)}`,
        { status: response.status, code: `http_${response.status}` });
    }
    if (!response.body) throw new RouterError("router stream has no body", { code: "stream_no_body" });

    this.attempts += 1;
    if (this.attempts > 1) this.reconnects += 1;
    this.connected = true;
    this.registered = true;
    this.connectedAt = new Date().toISOString();
    this.lastError = null;
    this.phase = "streaming";
    this.log("info", "router_stream_connected", {
      attempt: this.attempts, reconnects: this.reconnects, version: this.version,
    });
    try { this.handlers.onConnect?.({ attempt: this.attempts, reconnects: this.reconnects }); }
    catch (error) { this.log("warn", "router_on_connect_failed", { message: error?.message ?? String(error) }); }

    this._startHeartbeat();

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let reason = "eof";
    try {
      while (this.running) {
        let chunk;
        try {
          chunk = await reader.read();
        } catch (error) {
          if (!this.running) { reason = "client_stopped"; break; }
          reason = `read_error: ${error?.message ?? error}`;
          break;
        }
        if (chunk.done) { reason = "server_closed"; break; }
        buffer += decoder.decode(chunk.value, { stream: true });
        let frame;
        while ((frame = takeFrame(buffer)) !== null) {
          buffer = frame.rest;
          if (frame.text === "") continue;
          this._handleFrame(frame.text);
        }
        if (buffer.length > 1_000_000) { reason = "frame_too_large"; break; }
      }
    } finally {
      this._clearHeartbeat();
      try { await reader.cancel(); } catch { /* already closed */ }
      if (this.abort === controller) this.abort = null;
      this.lastDisconnectReason = reason;
      this.log("warn", "router_stream_closed", { reason, attempts: this.attempts });
    }
  }

  _handleFrame(raw) {
    let event = "message";
    const dataLines = [];
    for (const line of raw.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue;    // `: connected` / `: ping`
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") dataLines.push(value);
    }
    if (!dataLines.length) return;
    const data = dataLines.join("\n");
    let payload;
    try { payload = JSON.parse(data); }
    catch (error) {
      this.log("warn", "router_frame_bad_json", { event, message: error?.message ?? String(error) });
      return;
    }
    if (event === "task") {
      const task = payload?.task ?? payload;
      this.log("info", "router_task_received", {
        task_id: task?.task_id ?? null, urgency: task?.urgency ?? null,
      });
      try { this.handlers.onEvent?.(event, payload); } catch { /* observer only */ }
      try {
        const result = this.handlers.onTask(task);
        if (result && typeof result.catch === "function") {
          result.catch((error) => {
            this.log("error", "router_task_handler_failed", {
              task_id: task?.task_id ?? null, message: error?.message ?? String(error),
            });
            this.handlers.onError?.(error, task);
          });
        }
      } catch (error) {
        this.log("error", "router_task_handler_threw", {
          task_id: task?.task_id ?? null, message: error?.message ?? String(error),
        });
        try { this.handlers.onError?.(error, task); } catch { /* ignore */ }
      }
      return;
    }
    this.log("debug", "router_sse_event", { event });
    try { this.handlers.onEvent?.(event, payload); } catch { /* observer only */ }
  }

  _startHeartbeat() {
    this._clearHeartbeat();
    const beat = () => { void this._beat(); };
    beat();
    this.heartbeatTimer = setInterval(beat, this.heartbeatMs);
    this.heartbeatTimer.unref?.();
  }

  _clearHeartbeat() {
    if (this.heartbeatTimer) { clearInterval(this.heartbeatTimer); this.heartbeatTimer = null; }
  }

  async _beat() {
    if (this.heartbeatInFlight || !this.running || !this.connected) return;
    this.heartbeatInFlight = true;
    try {
      await this.heartbeat();
    } catch (error) {
      this.log("warn", "router_heartbeat_failed", {
        status: error?.status ?? null, code: error?.code ?? null, message: error?.message ?? String(error),
      });
      if (error?.status === 404 && error?.code === "unknown_worker") {
        // The router DB was reset while our stream stayed open; re-register so
        // the next beat lands. Failure here is retried by the timer.
        this.registered = false;
        try { await this.register(); } catch { /* backoff loop owns reconnects */ }
      }
    } finally {
      this.heartbeatInFlight = false;
    }
  }
}

/** Split one SSE frame off `buffer`; returns {text, rest} or null. */
export function takeFrame(buffer) {
  const lf = buffer.indexOf("\n\n");
  const crlf = buffer.indexOf("\r\n\r\n");
  if (lf === -1 && crlf === -1) return null;
  if (crlf !== -1 && (lf === -1 || crlf < lf)) {
    return { text: buffer.slice(0, crlf), rest: buffer.slice(crlf + 4) };
  }
  return { text: buffer.slice(0, lf), rest: buffer.slice(lf + 2) };
}

export default RouterClient;
