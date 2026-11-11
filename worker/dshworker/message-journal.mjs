// Durable public conversation archive, independent of the bounded SSE replay window.
// Records are immutable; delivery receipts and a tiny ordered index are separate.
import { mkdir, readFile, writeFile, rename, readdir, open } from "node:fs/promises";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";
const hash = value => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const idPattern = /^[a-f0-9]{64}$/;
const cursorPattern = /^\d{16}-[a-f0-9]{64}\.json$/;
async function json(file, fallback) {
  try { return JSON.parse(await readFile(file, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return fallback; throw error; }
}
async function atomic(file, value) {
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const temp = file + "." + randomUUID() + ".tmp";
  const handle = await open(temp, "wx", 0o600);
  try { await handle.writeFile(JSON.stringify(value) + "\n"); await handle.sync(); }
  finally { await handle.close(); }
  await rename(temp, file);
}
export function publicRecordId(logicalId, text, phase = null, previousId = null) {
  return hash(previousId ? [logicalId, text, phase, previousId] : [logicalId, text, phase]);
}
export function nativeMessageId(threadId, itemKey) {
  const h = hash(["native-input", threadId, itemKey]);
  return h.slice(0, 8) + "-" + h.slice(8, 12) + "-4" + h.slice(13, 16) + "-8" + h.slice(17, 20) + "-" + h.slice(20, 32);
}
export class MessageJournal {
  constructor(stateDir) { this.root = path.join(stateDir, "message-journal"); this.tail = Promise.resolve(); }
  serial(fn) { const work = this.tail.then(fn); this.tail = work.catch(() => {}); return work; }
  async record(input) {
    return this.serial(async () => {
      if (!["input", "output"].includes(input.direction) || typeof input.logicalId !== "string"
          || typeof input.text !== "string" || !Number.isSafeInteger(input.seq) || input.seq < 0) throw Error("invalid_public_record");
      let previousId = input.previousId ?? null;
      if (previousId && !idPattern.test(previousId)) throw Error("invalid_public_previous_id");
      const previous = previousId ? await json(path.join(this.root, "records", previousId + ".json"), null) : null;
      const same = previous && previous.logicalId === input.logicalId && previous.text === input.text &&
        previous.phase === (input.phase ?? null);
      const id = same ? previous.id : publicRecordId(input.logicalId, input.text, input.phase ?? null, previousId);
      if (same) previousId = previous.previousId ?? null;
      const file = path.join(this.root, "records", id + ".json");
      let record = await json(file, null);
      if (record && (record.id !== id || record.logicalId !== input.logicalId || record.text !== input.text || record.threadId !== (input.threadId ?? null) || record.direction !== input.direction)) throw Error("public_record_conflict");
      if (!record) {
        record = { version: 1, id, previousId, seq: input.seq, logicalId: input.logicalId,
          direction: input.direction, source: input.source, threadId: input.threadId ?? null,
          turnId: input.turnId ?? null, messageId: input.messageId ?? null, taskId: input.taskId ?? null,
          kind: ["image", "tool_progress", "tool_event"].includes(input.kind) ? input.kind : "text",
          ...(input.tool ? { tool: { toolName: input.tool.toolName, itemId: input.tool.itemId, status: input.tool.status, exitCode: input.tool.exitCode, durationMs: input.tool.durationMs } } : {}),
          mirror: input.mirror !== false, complete: input.complete !== false, historyLimited: input.historyLimited === true,
          phase: input.phase ?? null, text: input.text, timestamp: input.timestamp ?? new Date().toISOString() };
        await atomic(file, record);
      }
      const cursor = String(record.seq).padStart(16, "0") + "-" + id + ".json";
      await mkdir(path.join(this.root, "ordered"), { recursive: true, mode: 0o700 });
      await writeFile(path.join(this.root, "ordered", cursor), "", { flag: "wx", mode: 0o600 })
        .catch(error => { if (error.code !== "EEXIST") throw error; });
      return { ...record, cursor };
    });
  }
  // Repair an interrupted record/index commit before serving the archive.
  async recover() {
    let files;
    try { files = await readdir(path.join(this.root, "records")); }
    catch (error) { if (error.code === "ENOENT") return 0; throw error; }
    let seq = 0;
    for (const name of files.filter(n => /^[a-f0-9]{64}\.json$/.test(n))) {
      const record = await json(path.join(this.root, "records", name), null);
      if (!record || record.id + ".json" !== name || publicRecordId(record.logicalId, record.text, record.phase, record.previousId) !== record.id) throw Error("public_record_corrupt");
      await this.record(record);
      seq = Math.max(seq, record.seq);
    }
    return seq;
  }
  async receipt(id) {
    if (!idPattern.test(id)) throw Error("invalid_public_record_id");
    return json(path.join(this.root, "delivery", id + ".json"), { state: "pending", pages: {} });
  }
  async plan(id, pageIds) {
    if (!idPattern.test(id) || !Array.isArray(pageIds)) throw Error("invalid_public_delivery_plan");
    return this.serial(async () => {
      const receipt = await this.receipt(id);
      receipt.plannedPages = [...new Set(pageIds)];
      receipt.expectedPages = receipt.plannedPages.length;
      if (!pageIds.length && receipt.state === "pending") receipt.state = "local_only";
      await atomic(path.join(this.root, "delivery", id + ".json"), receipt);
    });
  }
  async supersede(id, by) {
    return this.serial(async () => {
      const receipt = await this.receipt(id);
      if (!["pending", "retrying"].includes(receipt.state)) return;
      receipt.state = "superseded"; receipt.supersededBy = by;
      await atomic(path.join(this.root, "delivery", id + ".json"), receipt);
    });
  }
  async delivered(id, pageId, state, messageId, expectedPages = 1) {
    if (!idPattern.test(id)) throw Error("invalid_public_record_id");
    return this.serial(async () => {
      const receipt = await this.receipt(id);
      receipt.pages ??= {};
      receipt.pages[pageId] = { state, messageId: messageId ?? null, at: new Date().toISOString() };
      receipt.expectedPages = receipt.plannedPages?.length ?? Math.max(expectedPages, receipt.expectedPages ?? 0);
      if (receipt.supersededBy) return receipt;
      const pages = receipt.plannedPages ? receipt.plannedPages.map(id => receipt.pages[id] ?? { state: "pending" }) : Object.values(receipt.pages);
      receipt.state = pages.some(p => p.state === "unknown") ? "unknown"
        : pages.length >= receipt.expectedPages && pages.every(p => ["sent", "received", "superseded"].includes(p.state)) && pages.some(p => p.state === "superseded") ? "superseded"
        : pages.length >= receipt.expectedPages && pages.every(p => ["sent", "received"].includes(p.state)) ? "sent"
        : pages.some(p => p.state === "retrying") ? "retrying" : "pending";
      receipt.updatedAt = new Date().toISOString();
      await atomic(path.join(this.root, "delivery", id + ".json"), receipt);
      return receipt;
    });
  }
  async list({ after = "", limit = 50, threadId, direction } = {}) {
    if (after && !cursorPattern.test(after)) throw Error("invalid_transcript_cursor");
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200) throw Error("invalid_transcript_limit");
    if (direction && !["input", "output"].includes(direction)) throw Error("invalid_transcript_direction");
    let names;
    try { names = await readdir(path.join(this.root, "ordered")); }
    catch (error) { if (error.code === "ENOENT") return { records: [], nextCursor: after, hasMore: false }; throw error; }
    names = names.filter(n => cursorPattern.test(n) && n > after).sort();
    const records = []; let scanned = 0, cursor = after;
    for (const name of names) {
      const id = name.slice(17, -5), record = await json(path.join(this.root, "records", id + ".json"), null);
      if (!record || record.id !== id || publicRecordId(record.logicalId, record.text, record.phase, record.previousId) !== id) throw Error("public_record_missing");
      cursor = name; scanned++;
      if ((!threadId || record.threadId === threadId) && (!direction || record.direction === direction))
        records.push({ ...record, cursor: name, delivery: await this.receipt(id) });
      if (records.length >= limit || scanned >= 1000) break;
    }
    return { records, nextCursor: cursor, hasMore: scanned < names.length };
  }
}
