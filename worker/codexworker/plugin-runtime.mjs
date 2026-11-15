// Fixed, bounded generation loader. Retiring a child also retires its entire ESM
// graph, so reloads do not accumulate module-cache entries in the Desktop host.
import { fork } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, readdir, realpath, rm, writeFile } from "node:fs/promises";
import path from "node:path";

const FIXED = new Set(["server.mjs", "plugin-runtime.mjs", "desktop-lifecycle.mjs", "business-runtime.mjs"]);
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
export class PluginRuntime {
  constructor({ pluginRoot, stateDir, onToolsChanged = () => {}, pollMs = 1000, env = process.env }) {
    Object.assign(this, { pluginRoot: path.resolve(pluginRoot), stateDir, onToolsChanged, pollMs, env });
    this.current = null; this.children = new Set(); this.generation = 0; this.closed = false;
    this.lastReloadError = null; this.pendingReload = false; this.activeCalls = 0;
    this.nextId = 1; this.sessionDir = path.join(stateDir, "business-generations", String(process.pid) + "-" + randomUUID());
  }
  status() { return { available: Boolean(this.current), scope: "all-business", featureVersion: this.version ?? null,
    bootstrapVersion: "0.3.0", desktopWatchdogVersion: "0.2.27", desktopLifetimeLaunchMode: this.env.RUDOLF_BRIDGE_LIFETIME_LAUNCH ?? null,
    desktopOwner: { pid: Number(this.env.CODEXWORKER_DESKTOP_PID) || null, startedAt: this.env.RUDOLF_BRIDGE_DESKTOP_STARTED_AT ?? null },
    sha256: this.current?.sha ?? null, loadedAt: this.loadedAt ?? null, generation: this.generation,
    loadCount: this.generation, reloadAvailable: true, autoReloadOnUse: this.autoReloadEnabled !== false, automaticSourcePolling: true,
    reloadPaused: this.autoReloadEnabled === false,
    reloadMode: "business-worker-handoff", connectionBehavior: "brief-reconnect", bootstrapPid: process.pid,
    businessPid: this.current?.child.pid ?? null, reloadPending: this.pendingReload, lastReloadError: this.lastReloadError }; }
  async source(packaged = false) {
    let root = this.pluginRoot, autoReload = true;
    try { const value = packaged ? {} : JSON.parse(await readFile(path.join(this.stateDir, "runtime-source.local.json"), "utf8"));
      if (packaged) { /* Startup recovery uses only this installed package. */ }
      else {
      if (typeof value.sourceRoot !== "string" || !path.isAbsolute(value.sourceRoot)) throw new Error(); root = value.sourceRoot;
      autoReload = value.autoReload !== false;
      }
    } catch (error) { if (error.code !== "ENOENT") throw new Error("runtime_source_unavailable"); }
    root = await realpath(root);
    const read = async () => {
      const files = new Map();
      const collect = async (relative = "") => {
        const directory = path.join(root, relative);
        for (const entry of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name, "en"))) {
          const name = relative ? relative + "/" + entry.name : entry.name;
          if (entry.isDirectory() && (relative || name === "interface")) {
            await collect(name);
            continue;
          }
          if (!name.endsWith(".mjs") || FIXED.has(name)) continue;
          const expected = path.join(root, name);
          if (!entry.isFile() || await realpath(expected) !== expected) throw new Error("runtime_source_escape");
          const bytes = await readFile(expected);
          if (bytes.length > 1024 * 1024) throw new Error("runtime_source_too_large");
          files.set(name, bytes);
        }
      };
      await collect();
      if (!files.has("mcp-business.mjs") || files.size > 128) throw new Error("runtime_source_invalid");
      return { files, sha: hash(JSON.stringify([...files].map(([name, bytes]) => [name, hash(bytes)]))) };
    };
    const first = await read(), second = await read();
    if (first.sha !== second.sha) throw new Error("runtime_source_changing");
    // Machine config is private; its hash is used only for change detection and
    // is never exposed as a public business-source fingerprint.
    let config = ""; try {
      const bytes = await readFile(path.join(this.stateDir, "desktop-config.local.json")), values = JSON.parse(bytes);
      const hashes = [hash(bytes)];
      for (const filename of [values.tokenFile, values.feishu?.configFile]) if (filename) {
        hashes.push(hash(await readFile(filename).catch(error => { if (error.code === "ENOENT") return Buffer.from("missing"); throw error; })));
      }
      config = hash(JSON.stringify(hashes));
    }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    return { ...second, config, root, autoReload };
  }
  async start() {
    if (this.starting) return this.starting;
    this.starting = (async () => {
      let candidate;
      try { const source = await this.source(); this.autoReloadEnabled = source.autoReload;
        candidate = await this.prepare(source.autoReload ? source : await this.source(true), false); }
      catch {
        candidate = await this.prepare(await this.source(true), false);
        this.lastReloadError = "runtime_startup_source_rejected";
      }
      this.current = candidate; this.version = candidate.version; this.generation = 1; this.loadedAt = new Date().toISOString();
      this.publish();
      this.timer = setInterval(() => { void this.check().catch(() => {}); }, this.pollMs); this.timer.unref();
    })();
    try { await this.starting; } catch (error) { this.starting = null; throw error; }
  }
  publish() { for (const item of this.children) if (item.child.connected) item.child.send({ op: "runtime-status", status: this.status() }); }
  async prepare(source, transition) {
    await mkdir(this.sessionDir, { recursive: true });
    const snapshot = path.join(this.sessionDir, randomUUID()); await mkdir(snapshot);
    try {
      for (const [name, bytes] of source.files) {
        const destination = path.join(snapshot, name);
        await mkdir(path.dirname(destination), { recursive: true });
        await writeFile(destination, bytes, { flag: "wx", mode: 0o600 });
      }
      // This compatibility loader is immutable in a business generation.
      await writeFile(path.join(snapshot, "business-runtime.mjs"), await readFile(path.join(this.pluginRoot, "business-runtime.mjs")), { flag: "wx", mode: 0o600 });
      const child = fork(path.join(this.pluginRoot, "scripts", "business-worker.mjs"), [], {
        execPath: process.execPath, cwd: this.pluginRoot, windowsHide: true,
        env: { ...this.env, CODEXWORKER_STATE_DIR: path.resolve(this.stateDir),
          RUDOLF_BRIDGE_RUNTIME_HOST_PID: String(process.pid) }, stdio: ["ignore", "ignore", "ignore", "ipc"] });
      const item = { child, snapshot, sha: source.sha, config: source.config, pending: new Map() }; this.children.add(item);
      child.on("message", message => {
        if (message.op === "reload-request") {
          this.pendingReload = true; this.publish();
          child.send({ op: "control-result", id: message.id, result: { ...this.status(), reloadScheduled: true } });
          return;
        }
        const request = item.pending.get(message.id); if (!request) return;
        item.pending.delete(message.id); clearTimeout(request.timer);
        message.error ? request.reject(new Error(message.error)) : request.resolve(message.result);
      });
      const lost = () => { for (const request of item.pending.values()) { clearTimeout(request.timer); request.reject(new Error("business_worker_unavailable")); } item.pending.clear(); };
      child.once("exit", lost); child.once("error", lost);
      try { const loaded = await this.rpc(item, { op: "load", snapshot, transition, status: this.status() });
        item.version = loaded.version;
        item.toolsHash = loaded.toolsHash;
        const init = await this.rpc(item, { op: "call", method: "ping", params: {} });
        if (!init || typeof init !== "object") throw new Error("business_validation_failed");
        return item;
      } catch (error) { await this.retire(item); throw error; }
    } catch (error) { await this.discard(snapshot); throw error; }
  }
  rpc(item, message, timeoutMs = 180000) {
    if (!item.child.connected) return Promise.reject(new Error("business_worker_unavailable"));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { item.pending.delete(id); reject(new Error("business_operation_unknown")); }, timeoutMs); timer.unref();
      item.pending.set(id, { resolve, reject, timer }); item.child.send({ ...message, id }, error => {
        if (error) { item.pending.delete(id); clearTimeout(timer); reject(new Error("business_worker_unavailable")); }
      });
    });
  }
  async call(method, params = {}) {
    await this.start(); if (this.closed) throw new Error("runtime_closed");
    if (!this.activeCalls && !this.checking) await this.check();
    if (this.switching) await this.switching;
    this.activeCalls++;
    try {
      const result = await this.rpc(this.current, { op: "call", method, params });
      if (method === "initialize") this.version = result.serverInfo?.version;
      return result;
    } finally { this.activeCalls--; }
  }
  async check() {
    if (this.closed || !this.current || this.checking) return;
    this.checking = true;
    let source;
    try {
      source = await this.source();
      this.autoReloadEnabled = source.autoReload;
      if (!source.autoReload && !this.pendingReload) { this.publish(); return this.status(); }
      if (this.failedKey === source.sha + source.config && !this.pendingReload) return this.status();
      if (source.sha === this.current.sha && source.config === this.current.config && !this.pendingReload) {
        this.lastReloadError = null; this.publish(); return { ...this.status(), duplicate: true };
      }
      if (this.activeCalls || (await this.rpc(this.current, { op: "inspect" })).busy) { this.pendingReload = true; this.publish(); return; }
      const candidate = await this.prepare(source, true);
      if (this.closed) { await this.retire(candidate); return; }
      // The switch barrier blocks new MCP work. HTTP/WS quiesce refuses busy
      // operations rather than cancelling or replaying an uncertain write.
      this.switching = this.swap(candidate); await this.switching;
    } catch (error) {
      this.lastReloadError = ["runtime_source_unavailable", "runtime_source_escape", "runtime_source_changing"].includes(error.message)
        ? error.message : "runtime_reload_failed";
      if (source) this.failedKey = source.sha + source.config;
      this.pendingReload = false;
      this.publish();
    } finally { this.switching = null; this.checking = false; }
  }
  async swap(candidate) {
    const old = this.current;
    if (this.activeCalls) { this.pendingReload = true; await this.retire(candidate); return; }
    const drained = await this.rpc(old, { op: "quiesce" });
    if (drained.busy) { this.pendingReload = true; await this.retire(candidate); return; }
    try {
      if (drained.hadManager) await this.rpc(candidate, { op: "activate" });
    } catch (error) {
      // Dispose a failed candidate before reacquiring the original writer lease.
      await this.retire(candidate);
      if (drained.hadManager) await this.rpc(old, { op: "activate" });
      throw error;
    }
    // Activation is the commit point. Notification/cleanup failures must never
    // retire the now-current worker or reactivate an already-retired old one.
    this.current = candidate; this.version = candidate.version; this.generation++; this.loadedAt = new Date().toISOString();
    this.lastReloadError = null; this.pendingReload = false; this.failedKey = null; this.publish();
    try { if (candidate.toolsHash !== old.toolsHash) this.onToolsChanged(); }
    catch { this.lastReloadError = "runtime_tools_notification_failed"; }
    try { await this.retire(old); }
    catch { this.lastReloadError = "runtime_cleanup_failed"; }
    this.publish();
  }
  async retire(item) {
    if (!item) return;
    item.retiring ??= this.retireWorker(item);
    return item.retiring;
  }
  async retireWorker(item) {
    if (item.child.connected) {
      // No force-kill timeout: an unknown write must not become an auto replay.
      await this.rpc(item, { op: "stop" }).catch(() => {});
    }
    if (item.child.exitCode === null) await new Promise(resolve => item.child.once("exit", resolve));
    this.children.delete(item); await this.discard(item.snapshot);
  }
  async discard(directory) {
    // Delete only this host's generated snapshots, never state/workspace roots.
    let root, target;
    try { root = await realpath(path.join(this.stateDir, "business-generations")); target = await realpath(directory); }
    catch (error) { if (error.code === "ENOENT") return; throw error; }
    const relative = path.relative(root, target);
    if (!relative || relative === ".." || relative.startsWith(".." + path.sep) || path.isAbsolute(relative)) throw new Error("runtime_cleanup_scope");
    await rm(target, { recursive: true, force: true });
  }
  async close() {
    if (this.closed) return; this.closed = true; clearInterval(this.timer);
    await this.starting?.catch(() => {}); await this.switching?.catch(() => {});
    clearInterval(this.timer);
    for (const item of [...this.children]) await this.retire(item);
    this.current = null;
    await this.discard(this.sessionDir);
  }
}
