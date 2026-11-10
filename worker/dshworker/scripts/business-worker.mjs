// Fixed loader-side IPC, never an independent Codex/session/model host.
import { readdir } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { pathToFileURL } from "node:url";
let service, active = 0, status = {}, controls = new Map(), nextControl = 1, stopping = false;
const send = value => { if (process.connected) process.send(value); };
process.on("message", async message => {
  if (message.op === "runtime-status") { status = message.status; return; }
  if (message.op === "control-result") {
    const resolve = controls.get(message.id); controls.delete(message.id); resolve?.(message.result); return;
  }
  if (message.op === "load") {
    try {
      status = message.status;
      // Parse/import every business file before touching existing IO/leases.
      for (const file of await readdir(message.snapshot)) if (file.endsWith(".mjs")) {
        await import(pathToFileURL(path.join(message.snapshot, file)).href);
      }
      const module = await import(pathToFileURL(path.join(message.snapshot, "mcp-business.mjs")).href);
      if (module.BUSINESS_API_VERSION !== 1 || module.BUSINESS_STATE_VERSION !== 1 || typeof module.createBusiness !== "function") throw new Error();
      service = module.createBusiness({ transition: Boolean(message.transition), status: () => status,
        reload: () => new Promise(resolve => { const id = nextControl++; controls.set(id, resolve); send({ op: "reload-request", id }); }) });
      for (const key of ["handleRequest", "busy", "activate", "quiesce", "close"]) if (typeof service[key] !== "function") throw new Error();
      const listed = await service.handleRequest("tools/list", {});
      if (!Array.isArray(listed.tools) || new Set(listed.tools.map(tool => tool.name)).size !== listed.tools.length ||
          listed.tools.some(tool => typeof tool.name !== "string" || tool.inputSchema?.type !== "object")) throw new Error();
      send({ id: message.id, result: { ready: true, version: module.BUSINESS_VERSION ?? null,
        toolsHash: createHash("sha256").update(JSON.stringify(listed)).digest("hex") } });
    } catch { send({ id: message.id, error: "business_validation_failed" }); }
    return;
  }
  try {
    if (message.op === "inspect") { send({ id: message.id, result: { busy: Boolean(active || service.busy()) } }); return; }
    if (message.op === "quiesce") {
      send({ id: message.id, result: active ? { busy: true } : await service.quiesce() }); return;
    }
    if (message.op === "stop") {
      stopping = true; await service?.close(); send({ id: message.id, result: {} }); process.disconnect(); process.exit(0);
    }
    active++;
    try {
      const result = message.op === "activate" ? await service.activate() : await service.handleRequest(message.method, message.params);
      send({ id: message.id, result });
    } finally { active--; }
  } catch { send({ id: message.id, error: "business_operation_failed" }); }
});
process.on("disconnect", () => {
  if (!stopping) { stopping = true; void service?.close().finally(() => process.exit(0)); }
});
