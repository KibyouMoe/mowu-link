import { createHash } from "node:crypto";
import { mkdir, readFile, realpath, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export class BusinessRuntimeError extends Error {
  constructor(status, error, detail, retryable = false) { super(detail); this.status = status; this.body = { error, detail, retryable }; }
}

// Only a fixed, builtin-only business leaf is replaceable. The manager/native
// pipe, MCP tool inventory, config, receipts and sole inbound reader stay intact.
export class BusinessRuntime {
  constructor({ stateDir, maxLoads = 32 }) {
    this.stateDir = stateDir; this.maxLoads = maxLoads; this.loads = 0; this.active = 0;
    this.reloading = false; this.module = null; this.sha256 = null;
    this.packageRoot = path.dirname(fileURLToPath(import.meta.url));
  }
  status() { return { available: Boolean(this.module), scope: "feishu-image-output", featureVersion: this.module?.FEATURE_VERSION ?? null,
    sha256: this.sha256, loadedAt: this.loadedAt ?? null, loadCount: this.loads, activeImageSends: this.active,
    reloadAvailable: true, autoReloadOnUse: true, lastReloadError: this.lastReloadError ?? null }; }
  async initialize() { return this.load(this.packageRoot); }
  async reload() {
    let settings;
    try { settings = JSON.parse(await readFile(path.join(this.stateDir, "runtime-source.local.json"), "utf8")); }
    catch { throw new BusinessRuntimeError(503, "runtime_source_unavailable", "Configure a trusted local sourceRoot in runtime-source.local.json before reloading."); }
    if (typeof settings.sourceRoot !== "string" || !path.isAbsolute(settings.sourceRoot)) throw new BusinessRuntimeError(400, "invalid_runtime_source", "sourceRoot must be an absolute user-owned plugin source directory.");
    return this.load(settings.sourceRoot);
  }
  async load(sourceRoot) {
    if (this.active || this.reloading) throw new BusinessRuntimeError(409, "runtime_busy", "Image operation/reload in progress; retry after it completes.", true);
    this.reloading = true;
    try {
      const root = await realpath(sourceRoot);
      const filename = await realpath(path.join(root, "feishu-image-output.mjs"));
      if (path.dirname(filename) !== root) throw new Error("Module escaped trusted source root");
      const bytes = await readFile(filename);
      if (bytes.length > 256 * 1024) throw new Error("Module too large");
      const sha256 = createHash("sha256").update(bytes).digest("hex");
      if (sha256 === this.sha256) return { ...this.status(), duplicate: true };
      if (this.loads >= this.maxLoads) throw new BusinessRuntimeError(409, "runtime_reload_limit", "Business module load limit reached; a normal plugin reload is required.");
      const cache = path.join(this.stateDir, "business-runtime"); await mkdir(cache, { recursive: true });
      const snapshot = path.join(cache, `${sha256}.mjs`);
      // Content-addressed URL avoids Node's ESM cache without unbounded timestamp
      // imports. Bad code cannot replace the last successfully validated module.
      await writeFile(snapshot, bytes, { flag: "wx", mode: 0o600 }).catch(error => { if (error.code !== "EEXIST") throw error; });
      if (!(await readFile(snapshot)).equals(bytes)) throw new Error("Runtime snapshot hash mismatch");
      this.loads++;
      const module = await import(pathToFileURL(snapshot).href);
      if (module.API_VERSION !== 1 || typeof module.sendImage !== "function" || typeof module.FEATURE_VERSION !== "string") throw new Error("Unsupported business module API");
      this.module = module; this.sha256 = sha256; this.loadedAt = new Date().toISOString();
      return { ...this.status(), duplicate: false };
    } catch (error) {
      if (error instanceof BusinessRuntimeError) throw error;
      throw new BusinessRuntimeError(422, "runtime_reload_failed", "Business module failed validation/loading; the previous module remains active.");
    } finally { this.reloading = false; }
  }
  async sendImage(request, context) {
    // Once this bootstrap is installed, edits at the trusted development source
    // apply on the next idle image operation, without another install or restart.
    if (!this.active && !this.reloading) {
      try { await this.reload(); this.lastReloadError = null; }
      catch (error) {
        this.lastReloadError = error.body?.error ?? "runtime_reload_failed";
        // A missing sidecar keeps the packaged implementation usable. A broken
        // update retains the previous implementation and is visible in status.
        if (!this.module) throw error;
      }
    }
    if (!this.module || this.reloading) throw new BusinessRuntimeError(503, "runtime_unavailable", "Image business module is unavailable; retry after reload.", true);
    this.active++;
    try { return await this.module.sendImage(request, context); }
    finally { this.active--; }
  }
}
