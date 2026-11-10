// Read only Desktop's lightweight catalog; never load a turn to list a chat.
// The database is opened read-only and no native state is updated by Bridge.
import path from "node:path";

export const normalizeDesktopPath = value => typeof value === "string"
  ? value.replace(/^\\\\\?\\(?=[a-z]:[\\/])/i, "") : value;

export async function readLocalResumeCatalog(codexHome) {
  let db;
  try {
    // Optional on older Node versions: native listing remains the fallback.
    const { DatabaseSync } = await import("node:sqlite");
    db = new DatabaseSync(path.join(codexHome, "state_5.sqlite"), { readOnly: true });
    const columns = new Set(db.prepare("PRAGMA table_info(threads)").all().map(r => r.name));
    if (!["id", "cwd", "title", "source", "originator", "archived"].every(k => columns.has(k))) throw new Error("unsupported_catalog");
    const fields = ["id", "cwd", "title", "name", "created_at", "created_at_ms", "updated_at", "updated_at_ms", "recency_at", "recency_at_ms"]
      .filter(k => columns.has(k));
    const rows = db.prepare(`SELECT ${fields.join(",")} FROM threads WHERE archived = 0 AND source = 'vscode' AND originator = 'Codex Desktop'`).all();
    const tasks = rows.filter(r => /^[a-f\d-]{36}$/i.test(r.id) && typeof r.cwd === "string").map(r => ({
      id: r.id, kind: "codex", hostId: "local", cwd: normalizeDesktopPath(r.cwd),
      title: r.name?.trim() ? r.name : r.title,
      createdAt: r.created_at_ms ?? (r.created_at == null ? null : r.created_at * 1000),
      updatedAt: r.updated_at_ms ?? (r.updated_at == null ? null : r.updated_at * 1000),
      recencyAt: r.recency_at_ms ?? (r.recency_at == null ? null : r.recency_at * 1000),
      metadataSource: "desktop-local-catalog"
    }));
    return { available: true, tasks };
  } catch { return { available: false, tasks: [] }; }
  finally { db?.close(); }
}
