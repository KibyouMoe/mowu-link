"""Manager-only, historical projection of a verified legacy snapshot. No work-order writes."""
import json
import re

from .messages import valid_guid
from .storage import encode, require


SCHEMA = """
CREATE TABLE IF NOT EXISTS history_sources (
    source_id TEXT PRIMARY KEY, legacy_db_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_tasks (
    source_id TEXT NOT NULL, legacy_task_id TEXT NOT NULL, mapped_task_id TEXT,
    title TEXT NOT NULL, instructions TEXT NOT NULL, worker TEXT NOT NULL, actor TEXT NOT NULL,
    created_at REAL NOT NULL, archive TEXT, deliverable TEXT,
    PRIMARY KEY(source_id,legacy_task_id)
);
"""


def populate(db, old, source, sha, mapping):
    db.execute("INSERT INTO history_sources VALUES (?,?)",(source,sha))
    for row in old.execute("SELECT * FROM task_info ORDER BY rowid"):
        tid = row["task_id"]
        facts = {}
        for fact in old.execute("""SELECT kind,at,data FROM task_facts
                           WHERE task_id=? AND kind IN ('summary','archive') ORDER BY seq""",(tid,)):
            data = json.loads(fact["data"])
            facts[fact["kind"]] = dict(data,at=fact["at"]) if fact["kind"]=="archive" else data.get("report")
        archive,report = facts.get("archive"),facts.get("summary")
        db.execute("INSERT INTO history_tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (source,tid,mapping.get(tid),row["name"],row["description"],row["owner"],row["actor"],row["at"],
                    encode(archive) if archive is not None else None,encode(report) if report is not None else None))


def query(db, source, filters):
    require(valid_guid(source),422,"invalid_legacy_source")
    row = db.execute("SELECT legacy_db_sha256 FROM history_sources WHERE source_id=?",(source,)).fetchone()
    require(row,404,"legacy_source_not_found")
    result = {"source_id":source,"legacy_db_sha256":row[0]}
    if "legacy_task_id" in filters:
        require(len(filters)==1,422,"detail_with_list_filters")
        row = db.execute("SELECT * FROM history_tasks WHERE source_id=? AND legacy_task_id=?",
                         (source,filters["legacy_task_id"])).fetchone()
        require(row,404,"legacy_task_not_found")
        detail = dict(row)
        for name in ("archive","deliverable"):
            if detail[name] is not None:
                detail[name] = json.loads(detail[name])
        return {**result,"historical_task":detail}

    require(set(filters)<={"cursor","limit"},422,"invalid_filter")
    cursor,limit = filters.get("cursor","0"),filters.get("limit","50")
    require(re.fullmatch("[0-9]{1,18}",cursor) and re.fullmatch("[0-9]{1,3}",limit)
            and 1<=int(limit)<=200,422,"invalid_pagination")
    rows = db.execute("""SELECT rowid AS cursor_id,legacy_task_id,title,worker,mapped_task_id,
                     json_extract(archive,'$.reason') AS archive_reason
                     FROM history_tasks WHERE source_id=? AND rowid>? ORDER BY rowid LIMIT ?""",
                      (source,int(cursor),int(limit)+1)).fetchall()
    page = [dict(r) for r in rows[:int(limit)]]
    next_cursor = str(page[-1]["cursor_id"]) if len(rows)>len(page) else None
    for item in page:
        del item["cursor_id"]
    return {**result,"items":page,"next_cursor":next_cursor}
