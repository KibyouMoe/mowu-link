// session-store.mjs — the bridge's own conversation history.
//
// Why JSONL per session and not SQLite: this package has zero dependencies and
// `engines.node >= 20`, where `node:sqlite` is not available (it landed later
// and is still flagged on some 22.x builds). One append-only file per
// conversation is crash-safe (a torn line never invalidates earlier messages),
// keeps reads proportional to one conversation, and needs no schema migration.
// The codex rollout jsonl is now only a process stream; every `/threads`,
// transcript and card-history read comes from here.
//
// Layout: <stateDir>/sessions/<threadId>.jsonl
//   line 1: {"type":"meta","threadId":"cli-…","sessionId":"<codex uuid>","cwd":…,"title":…,
//            "source":"feishu|http|cli","createdAt":…,"updatedAt":…}
//   line n: {"type":"message","role":"user|assistant","text":…,"ts":…,"messageId":…,
//            "turnId":…,"source":…}
// Every file is mode 600; the directory is 700. Nothing here is committed.
import { chmod, mkdir, open, readFile, readdir, rename, writeFile } from "node:fs/promises";
import path from "node:path";

const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const MAX_TEXT = 200000;
const ROLES = new Set(["user", "assistant"]);

function nowIso() { return new Date().toISOString(); }

export class SessionStore {
  constructor(root, { logger = () => {} } = {}) {
    this.root = root;
    this.logger = logger;
    this.chains = new Map();
    this.ready = false;
  }

  async init() {
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    this.ready = true;
    return this;
  }

  pathFor(threadId) {
    if (typeof threadId !== "string" || !ID_RE.test(threadId)) {
      const error = new Error("invalid thread id");
      error.status = 400;
      error.body = { error: "invalid_thread_id", detail: "Thread ids must be uuid/cli-uuid shaped." };
      throw error;
    }
    return path.join(this.root, `${threadId}.jsonl`);
  }

  // Serialize writes to one file so concurrent turns cannot interleave lines.
  #serial(file, task) {
    const previous = this.chains.get(file) ?? Promise.resolve();
    const run = previous.then(task);
    this.chains.set(file, run.catch(() => {}));
    return run;
  }

  async readMeta(threadId) {
    const file = this.pathFor(threadId);
    let text;
    try { text = await readFile(file, "utf8"); }
    catch (error) { if (error.code === "ENOENT") return null; throw error; }
    const first = text.slice(0, text.indexOf("\n") === -1 ? text.length : text.indexOf("\n"));
    try {
      const value = JSON.parse(first);
      return value?.type === "meta" && value.threadId === threadId ? value : null;
    } catch { return null; }
  }

  async create({ threadId, cwd = null, source = "cli", title = null, sessionId = null, createdAt = nowIso() }) {
    const existing = await this.readMeta(threadId).catch(() => null);
    if (existing) return existing;
    const file = this.pathFor(threadId);
    const meta = { type: "meta", threadId, sessionId, cwd, title, source, createdAt, updatedAt: createdAt };
    // Lock-free creation: `wx` makes exactly one writer win. Never nest this in
    // #serial — appendMessages holds the file chain while calling ensure().
    try {
      await writeFile(file, JSON.stringify(meta) + "\n", { mode: 0o600, flag: "wx" });
      await chmod(file, 0o600).catch(() => {});
      return meta;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      return (await this.readMeta(threadId).catch(() => null)) ?? meta;
    }
  }

  async ensure(options) {
    const existing = await this.readMeta(options.threadId).catch(() => null);
    return existing ?? this.create(options);
  }

  async appendMessages(threadId, messages) {
    if (!Array.isArray(messages) || messages.length === 0) return [];
    const file = this.pathFor(threadId);
    return this.#serial(file, async () => {
      await this.ensure({ threadId, source: messages[0]?.source ?? "cli" });
      const lines = messages.map(message => {
        if (!ROLES.has(message.role)) throw new Error(`invalid role: ${message.role}`);
        return JSON.stringify({
          type: "message", role: message.role,
          text: String(message.text ?? "").slice(0, MAX_TEXT),
          ts: message.ts ?? nowIso(),
          messageId: message.messageId ?? null,
          turnId: message.turnId ?? null,
          source: message.source ?? "cli",
        });
      });
      const handle = await open(file, "a", 0o600);
      try { await handle.appendFile(lines.join("\n") + "\n", "utf8"); }
      finally { await handle.close(); }
      return lines.map(line => JSON.parse(line));
    });
  }

  appendMessage(threadId, message) {
    return this.appendMessages(threadId, [message]).then(written => written[0]);
  }

  async setMeta(threadId, patch) {
    const file = this.pathFor(threadId);
    return this.#serial(file, async () => {
      let text;
      try { text = await readFile(file, "utf8"); }
      catch (error) {
        if (error.code !== "ENOENT") throw error;
        await this.ensure({ threadId, source: patch.source ?? "cli" });
        text = await readFile(file, "utf8");
      }
      const lines = text.split("\n").filter(line => line.trim());
      const meta = JSON.parse(lines[0]);
      const next = { ...meta, ...patch, type: "meta", threadId, updatedAt: patch.updatedAt ?? nowIso() };
      lines[0] = JSON.stringify(next);
      const temporary = `${file}.${process.pid}.tmp`;
      await writeFile(temporary, lines.join("\n") + "\n", { mode: 0o600 });
      await rename(temporary, file);
      return next;
    });
  }

  async #readFile(threadId) {
    const file = this.pathFor(threadId);
    let text;
    try { text = await readFile(file, "utf8"); }
    catch (error) {
      if (error.code === "ENOENT") return null;
      return { error: `session_store_unreadable:${error.code ?? error.message}` };
    }
    let meta = null, malformed = 0, total = 0;
    const messages = [];
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      total += 1;
      let record;
      try { record = JSON.parse(line); } catch { malformed += 1; continue; }
      if (record?.type === "meta" && !meta) meta = record;
      else if (record?.type === "message" && ROLES.has(record.role)) {
        messages.push({ role: record.role, text: String(record.text ?? ""), ts: record.ts ?? null,
          messageId: record.messageId ?? null, turnId: record.turnId ?? null, source: record.source ?? "cli" });
      } else malformed += 1;
    }
    if (!meta) return { error: "session_store_corrupt:meta_missing" };
    if (malformed > Math.max(1, total / 2)) return { error: `session_store_corrupt:${malformed}/${total}` };
    return { meta, messages, malformed, lines: total };
  }

  // Public read used by /threads/:id, degradation checks and future card history.
  async read(threadId, { limit = 200 } = {}) {
    const parsed = await this.#readFile(threadId);
    if (!parsed) return { ok: false, reason: "session_store_missing" };
    if (parsed.error) return { ok: false, reason: parsed.error };
    const messages = parsed.messages.slice(-Math.max(1, Math.min(Number(limit) || 200, 1000)));
    return { ok: true, meta: parsed.meta, messages, malformed: parsed.malformed,
      totalMessages: parsed.messages.length, turns: groupTurns(parsed.messages).length };
  }

  // Same shape the CLI used to return from adapter.readThread, so /threads/:id
  // consumers keep working while the source moves to this store.
  async readTurns(threadId, { turnLimit = 20 } = {}) {
    const snapshot = await this.read(threadId, { limit: 10000 });
    if (!snapshot.ok) {
      const error = new Error(snapshot.reason);
      error.status = 404;
      error.body = { error: "thread_not_found", detail: snapshot.reason };
      throw error;
    }
    const turns = groupTurns(snapshot.messages).slice(-Math.max(1, Math.min(Number(turnLimit) || 20, 200)));
    return {
      thread: { id: threadId, sessionId: snapshot.meta.sessionId ?? null, cwd: snapshot.meta.cwd ?? null,
        createdAt: snapshot.meta.createdAt ?? null, updatedAt: snapshot.meta.updatedAt ?? null,
        title: snapshot.meta.title ?? null, hostId: "local", source: "bridge-store",
        messageCount: snapshot.totalMessages },
      page: { order: "oldest_first" },
      turns,
    };
  }

  async list({ limit = 50 } = {}) {
    let names = [];
    try { names = await readdir(this.root); }
    catch (error) { if (error.code === "ENOENT") return []; throw error; }
    const sessions = [];
    for (const name of names) {
      if (!name.endsWith(".jsonl")) continue;
      const threadId = name.slice(0, -".jsonl".length);
      if (!ID_RE.test(threadId)) continue;
      let parsed;
      try { parsed = await this.#readFile(threadId); } catch { continue; }
      if (!parsed || parsed.error) continue;
      const turns = groupTurns(parsed.messages);
      const last = parsed.messages.at(-1) ?? null;
      sessions.push({
        id: threadId, sessionId: parsed.meta.sessionId ?? null, cwd: parsed.meta.cwd ?? null,
        createdAt: parsed.meta.createdAt ?? null, updatedAt: parsed.meta.updatedAt ?? last?.ts ?? null,
        title: parsed.meta.title ?? null, turns: turns.length, lastText: last?.text ?? null,
        live: false, hostId: "local", source: "bridge-store",
        messageCount: parsed.messages.length,
      });
    }
    sessions.sort((a, b) => String(b.updatedAt ?? "").localeCompare(String(a.updatedAt ?? "")));
    return sessions.slice(0, Math.max(1, Math.min(Number(limit) || 50, 200)));
  }

  async status() {
    const list = await this.list({ limit: 200 }).catch(() => []);
    return { root: this.root, sessions: list.length,
      messages: list.reduce((sum, item) => sum + (item.messageCount ?? 0), 0) };
  }
}

// A user message opens a turn; assistant messages that follow belong to it.
// Linear conversations need no explicit turn id, and a missing turnId on old
// rows never breaks the grouping.
export function groupTurns(messages) {
  const turns = [];
  for (const message of messages) {
    if (message.role === "user") {
      turns.push({ id: message.turnId ?? `turn-${turns.length + 1}`, status: "completed",
        createdAt: message.ts ?? null, completedAt: null,
        items: [{ type: "userMessage", id: message.messageId ?? null, text: message.text }] });
      continue;
    }
    if (turns.length === 0) {
      turns.push({ id: message.turnId ?? "turn-1", status: "completed", createdAt: message.ts ?? null,
        completedAt: null, items: [] });
    }
    const turn = turns.at(-1);
    turn.items.push({ type: "agentMessage", id: message.messageId ?? null, text: message.text });
    turn.completedAt = message.ts ?? turn.completedAt;
    if (message.turnId) turn.id = message.turnId;
  }
  return turns;
}
