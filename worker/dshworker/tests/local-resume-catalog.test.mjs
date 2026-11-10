import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { readLocalResumeCatalog, normalizeDesktopPath } from "../local-resume-catalog.mjs";
import { resumePage, resumeListText } from "../resume-list.mjs";

test("local resume catalog is read-only, uses recency/name and excludes archived and agent tasks", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "resume-catalog-")); t.after(() => rm(root, { recursive: true, force: true }));
  const file = path.join(root, "state_5.sqlite"), db = new DatabaseSync(file);
  db.exec("CREATE TABLE threads(id TEXT,cwd TEXT,title TEXT,name TEXT,source TEXT,originator TEXT,archived INTEGER,created_at_ms INTEGER,updated_at_ms INTEGER,recency_at_ms INTEGER)");
  const insert = db.prepare("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)");
  for (let i = 0; i < 25; i++) insert.run(`11111111-1111-4111-8111-${String(i).padStart(12, "0")}`, "\\\\?\\C:\\work", "old first message", "标题 " + i,
    i === 23 ? '{"subagent":{}}' : "vscode", "Codex Desktop", i === 24 ? 1 : 0, i, 999999, i + 100);
  db.close(); const before = await readFile(file), result = await readLocalResumeCatalog(root);
  assert.equal(result.available, true); assert.equal(result.tasks.length, 23); assert.equal(result.tasks[0].title, "标题 0");
  assert.equal(result.tasks[0].cwd, "C:\\work"); assert.equal(result.tasks[0].recencyAt, 100); assert.deepEqual(await readFile(file), before);
});

test("missing or unsupported catalogs fall back without creating a native database", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "resume-catalog-empty-")); t.after(() => rm(root, { recursive: true, force: true }));
  assert.deepEqual(await readLocalResumeCatalog(root), { available: false, tasks: [] });
  await assert.rejects(readFile(path.join(root, "state_5.sqlite")), { code: "ENOENT" });
  assert.equal(normalizeDesktopPath("C:\\work"), "C:\\work");
});

test("ten-per-page lists keep global zero-based selectors and expose later pages", () => {
  const items = Array.from({ length: 26 }, (_, index) => ({ index, threadId: "id" + index, alias: "会话" + index }));
  const result = resumePage(items, 1); assert.equal(result.sessions[0].index, 10); assert.equal(result.page.pages, 3);
  const text = resumeListText({ ...result, resumePage: result.page, currentSession: { threadId: "id25", title: "会话25", index: 25 } });
  assert.match(text, /第 2\/3 页/); assert.match(text, /编号 10/); assert.match(text, /search/); assert.doesNotMatch(text, /0–4|五条/);
  assert.throws(() => resumePage(items, 3), /out_of_range/);
});
