// Incremental, read-only projection of Desktop's real item_completed events.
// No model/runtime, synthetic history, tool-output mining or native DB writes.
import { open, readdir, realpath } from "node:fs/promises";
import path from "node:path";
import { publicJournalItem, journalInputPaths } from "./interface/journal-items.mjs";
export { publicJournalItem } from "./interface/journal-items.mjs";
import { projectJournalContext } from "./interface/context-events.mjs";

const bad = code => { const e = new Error(code); e.code = code; throw e; };
const inside = (root, file) => { const r = path.relative(root, file); return r === "" || !r.startsWith(".." + path.sep) && r !== ".." && !path.isAbsolute(r); };
const samePath = (a, b) => path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase();

// Skip huge string values (image base64, command output) WITHOUT allocating
// them. Preserve JSON structure and small fields, including savedPath after a
// multi-megabyte result. UTF-8/escapes and incomplete lines span read blocks.
export class CompactJsonLines {
  constructor({ stringLimit = 262144, recordLimit = 4194304 } = {}) {
    Object.assign(this, { stringLimit, recordLimit }); this.reset();
  }
  reset() { this.parts = []; this.size = 0; this.inString = false; this.escaped = false; this.token = []; this.tokenSize = 0; this.dropped = false; }
  append(bytes) { this.size += bytes.length; if (this.size > this.recordLimit) bad("public_journal_record_too_large"); this.parts.push(Buffer.from(bytes)); }
  string(bytes) {
    this.tokenSize += bytes.length;
    if (this.tokenSize > this.stringLimit) { this.dropped = true; this.token = []; }
    else if (!this.dropped) this.token.push(Buffer.from(bytes));
  }
  feed(bytes, start, consume) {
    let i = 0;
    while (i < bytes.length) {
      if (this.inString) {
        if (this.escaped) { this.string(bytes.subarray(i, i + 1)); i++; this.escaped = false; continue; }
        const q = bytes.indexOf(34, i), slash = bytes.indexOf(92, i);
        const end = Math.min(q < 0 ? bytes.length : q, slash < 0 ? bytes.length : slash);
        this.string(bytes.subarray(i, end)); i = end;
        if (i === bytes.length) break;
        this.string(bytes.subarray(i, i + 1));
        if (bytes[i++] === 92) this.escaped = true;
        else {
          this.append(this.dropped ? Buffer.from("null") : Buffer.concat(this.token));
          this.inString = false; this.token = []; this.tokenSize = 0; this.dropped = false;
        }
      } else {
        const q = bytes.indexOf(34, i), nl = bytes.indexOf(10, i);
        const end = Math.min(q < 0 ? bytes.length : q, nl < 0 ? bytes.length : nl);
        this.append(bytes.subarray(i, end)); i = end;
        if (i === bytes.length) break;
        if (bytes[i++] === 34) { this.inString = true; this.string(Buffer.from('"')); }
        else {
          const line = Buffer.concat(this.parts).toString("utf8").trim();
          let value; try { value = line ? JSON.parse(line) : null; } catch { bad("public_journal_invalid_record"); }
          this.reset(); consume(value, start + i);
        }
      }
    }
  }
}

// Lifecycle events describe execution, unlike sidebar focus/open state. An old
// turn's delayed completion must never finish a newer turn in this same file.
export function journalTaskState(record, threadId, previous = null) {
  if (record?.type !== "event_msg" || !["task_started", "task_complete", "turn_aborted"].includes(record.payload?.type)) return previous;
  const p = record.payload;
  if ((p.thread_id != null && p.thread_id !== threadId) || typeof p.turn_id !== "string" || !p.turn_id) bad("public_journal_identity_mismatch");
  if (p.type !== "task_started" && previous && previous.turnId !== p.turn_id) return previous;
  return { turnId: p.turn_id, turnStatus: p.type === "task_started" ? "inProgress" : p.type === "turn_aborted" ? "interrupted" : "completed" };
}

export class LocalPublicJournal {
  constructor(root, { byteBudget = 8 * 1024 * 1024, chunkBytes = 64 * 1024 } = {}) {
    Object.assign(this, { root: path.resolve(root), byteBudget, chunkBytes }); this.readers = new Map();
  }
  async locate(id, saved) {
    if (!/^[a-f0-9-]{36}$/i.test(id)) bad("public_journal_unavailable");
    const root = await realpath(this.root);
    if (saved) {
      const file = await realpath(path.resolve(root, saved));
      if (!inside(root, file) || !path.basename(file).endsWith(`-${id}.jsonl`)) bad("public_journal_identity_mismatch");
      return { root, file };
    }
    const matches = []; let dirs = 0;
    const visit = async (dir, depth) => {
      if (++dirs > 4096) bad("public_journal_discovery_limit");
      for (const e of await readdir(dir, { withFileTypes: true })) {
        if (e.isSymbolicLink()) continue;
        const f = path.join(dir, e.name);
        if (e.isFile() && e.name.endsWith(`-${id}.jsonl`)) matches.push(f);
        else if (e.isDirectory() && depth < 3 && /^\d{2,4}$/.test(e.name)) await visit(f, depth + 1);
      }
    };
    await visit(root, 0);
    if (matches.length !== 1) bad(matches.length ? "public_journal_ambiguous" : "public_journal_unavailable");
    const file = await realpath(matches[0]);
    if (!inside(root, file)) bad("public_journal_identity_mismatch");
    return { root, file };
  }
  async read(session, checkpoint, { turnId, epoch, startAt = null, stateOnly = false, includeInputs = false, includeTools = false, includeContext = false }) {
    const saved = checkpoint?.epoch === epoch ? checkpoint : null;
    const location = await this.locate(session.threadId, saved?.file ?? this.readers.get(session.threadId)?.file);
    const handle = await open(location.file, "r");
    try {
      const st = await handle.stat(), identity = `${st.dev}:${st.ino}:${st.birthtimeMs}`;
      if (!st.isFile() || saved && (saved.identity !== identity || st.size < saved.offset)) bad("public_journal_replaced");
      let reader = this.readers.get(session.threadId);
      if (!reader || reader.identity !== identity || reader.expected !== (saved?.offset ?? 0) || reader.epoch !== epoch) {
        const header = new CompactJsonLines(); let meta, position = 0;
        while (!meta && position < Math.min(st.size, 4 * 1024 * 1024)) {
          const buf = Buffer.alloc(Math.min(this.chunkBytes, st.size - position));
          const { bytesRead } = await handle.read(buf, 0, buf.length, position);
          if (!bytesRead) break;
          header.feed(buf.subarray(0, bytesRead), position, value => { meta ??= value; }); position += bytesRead;
        }
        if (meta?.type !== "session_meta" || meta.payload?.id !== session.threadId || typeof meta.payload.cwd !== "string" || !samePath(meta.payload.cwd, session.cwd)) bad("public_journal_identity_mismatch");
        reader = { file: path.relative(location.root, location.file), identity, epoch, offset: saved?.offset ?? 0,
          expected: saved?.offset ?? 0, parser: new CompactJsonLines(), bootstrapTurnId: saved?.bootstrapTurnId ?? (!saved ? turnId : null),
          bootstrapEnd: saved?.bootstrapEnd ?? st.size,
          bootstrapReached: saved?.bootstrapReached ?? false,
          taskState: saved?.taskState ?? null,
          startAt: saved?.startAt ?? startAt, inputPaths: saved?.inputPaths ?? [] };
        this.readers.set(session.threadId, reader);
      }
      const events = []; let bytes = 0, completeOffset = reader.expected;
      while (reader.offset < st.size && bytes < this.byteBudget && events.length < 128) {
        const buf = Buffer.alloc(Math.min(this.chunkBytes, st.size - reader.offset));
        const { bytesRead } = await handle.read(buf, 0, buf.length, reader.offset);
        if (!bytesRead) break;
        reader.parser.feed(buf.subarray(0, bytesRead), reader.offset, (record, end) => {
          completeOffset = end;
          reader.taskState = journalTaskState(record, session.threadId, reader.taskState);
          if (stateOnly) return;
          if (record?.type === "turn_context" && record.payload?.turn_id === reader.bootstrapTurnId ||
              record?.type === "event_msg" && record.payload?.type === "task_started" && record.payload.turn_id === reader.bootstrapTurnId) reader.bootstrapReached = true;
          for (const imagePath of journalInputPaths(record, session.threadId)) {
            if (!reader.inputPaths.includes(imagePath)) reader.inputPaths.push(imagePath);
          }
          if (reader.inputPaths.length > 1024) bad("public_journal_input_image_limit");
          const context = includeContext ? projectJournalContext(record, session.threadId) : null;
          const event = context ? { turnId: context.turnId, item: { type: "contextEvent", event: context }, timestamp: context.timestamp }
            : publicJournalItem(record, session.threadId, { includeInputs, includeTools });
          if (event?.turnId === reader.bootstrapTurnId) reader.bootstrapReached = true;
          if (!event || end <= reader.bootstrapEnd && reader.bootstrapTurnId && !reader.bootstrapReached || reader.startAt && !(Date.parse(event.timestamp) >= Date.parse(reader.startAt))) return;
          if (event.item.type === "imageView" && reader.inputPaths.some(p => samePath(p, event.item.path))) return;
          events.push(event);
        });
        reader.offset += bytesRead; bytes += bytesRead;
      }
      const caughtUp = reader.offset === st.size;
      // Keep a bootstrap turn filter until the initial file size has been
      // traversed; all subsequent completed events follow the current binding.
      if (caughtUp) reader.bootstrapTurnId = null;
      reader.expected = completeOffset;
      return { events, bytesRead: bytes, caughtUp, pendingBytes: st.size - completeOffset, taskState: reader.taskState,
        checkpoint: { file: reader.file, identity, offset: completeOffset, epoch, bootstrapTurnId: reader.bootstrapTurnId,
          bootstrapEnd: reader.bootstrapEnd, bootstrapReached: reader.bootstrapReached, taskState: reader.taskState, startAt: reader.startAt, inputPaths: [...reader.inputPaths] } };
    } catch (e) { this.readers.delete(session.threadId); throw e; }
    finally { await handle.close(); }
  }
}
