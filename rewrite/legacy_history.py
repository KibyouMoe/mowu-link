"""Manager-only, historical projection of a verified legacy snapshot. No work-order writes."""
import hashlib
import json
import re
from uuid import UUID, uuid5

from .messages import valid_guid
from .storage import encode, require

REVIEWED_ARCHIVE = "reviewed_on_rewrite"
CANCELLED_ARCHIVE = "cancelled_on_rewrite"


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
CREATE TABLE IF NOT EXISTS history_request_keys (
 actor TEXT, request_key TEXT, source_id TEXT NOT NULL, legacy_task_id TEXT NOT NULL,
 PRIMARY KEY(actor,request_key)
);
"""

# This optional table lives in the restored copy. The legacy HTTP server does not expose it.
ROLLBACK_KEYS_SCHEMA = """CREATE TABLE IF NOT EXISTS rewrite_archive_request_keys (
    legacy_task_id TEXT PRIMARY KEY REFERENCES task_info(task_id),
    request_key TEXT NOT NULL UNIQUE,
    trial_source_id TEXT NOT NULL
)"""
QUEUE_REPLAY_SCHEMA = """CREATE TABLE IF NOT EXISTS rewrite_queue_replay_keys (
    legacy_request_id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    trial_source_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL
)"""


def rollback_request_id(source_id, key):
    # Preserve the request UUID mapping used by existing queue replay plans.
    return key if valid_guid(key) else str(uuid5(UUID(source_id),"queue-rollback/"+key))


def rollback_archive_id(source_id, trial_task_id):
    return "t-" + uuid5(UUID(source_id),"archive-rollback/"+trial_task_id).hex[:10]


def legacy_request_fingerprint(request):
    # Match the legacy HTTP server's exact request fingerprint, omitting its UUID.
    payload = {name:value for name,value in request.items() if name!="message_id"}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=True,
                                     separators=(",",":")).encode("ascii")).hexdigest()


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
        if row["request_id"] and row["actor"]=="hermes":
            db.execute("INSERT INTO history_request_keys VALUES ('hermes',?,?,?)",(row["request_id"],source,tid))
        db.execute("INSERT INTO history_tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (source,tid,mapping.get(tid),row["name"],row["description"],row["owner"],row["actor"],row["at"],
                    encode(archive) if archive is not None else None,encode(report) if report is not None else None))
    preserve_rollback_keys(db,old,source,mapping)
    preserve_queue_keys(db,old,source,mapping)


def has_rollback_table(old, name):
    table = old.execute("SELECT type FROM sqlite_master WHERE name=?",(name,)).fetchone()
    if table is None:
        return False
    if table["type"]!="table":
        raise ValueError("invalid rewrite rollback request-key table")
    return True


def preserve_rollback_keys(db, old, source, mapping):
    if not has_rollback_table(old,"rewrite_archive_request_keys"):
        return
    for row in old.execute("""SELECT a.*, t.actor, t.request_id, f.data AS archive_data
        FROM rewrite_archive_request_keys a LEFT JOIN task_info t ON t.task_id=a.legacy_task_id
        LEFT JOIN task_facts f ON f.task_id=a.legacy_task_id AND f.kind='archive' AND f.event_key='archive'"""):
        key, namespace, tid = row["request_key"],row["trial_source_id"],row["legacy_task_id"]
        archive = json.loads(row["archive_data"] or "null")
        trial_id = archive.get("recovery_reference") if isinstance(archive,dict) else None
        if (row["actor"]!="hermes" or not isinstance(key,str) or not re.fullmatch(r"hermes-[0-9a-f]{64}",key)
                or not valid_guid(namespace) or not valid_guid(trial_id)
                or archive.get("reason") not in (REVIEWED_ARCHIVE,CANCELLED_ARCHIVE)
                or rollback_request_id(namespace,key)!=row["request_id"] or rollback_archive_id(namespace,trial_id)!=tid
                or tid in mapping):
            raise ValueError("rewrite archive request key does not match its legacy archive")
        db.execute("INSERT INTO history_request_keys VALUES ('hermes',?,?,?)",(key,source,tid))


def preserve_queue_keys(db, old, source, mapping):
    if not has_rollback_table(old,"rewrite_queue_replay_keys"):
        return
    for row in old.execute("""SELECT r.*, t.task_id, t.actor, t.fingerprint AS recorded_fingerprint
        FROM rewrite_queue_replay_keys r LEFT JOIN task_info t ON t.request_id=r.legacy_request_id"""):
        key, namespace, legacy_key = row["request_key"],row["trial_source_id"],row["legacy_request_id"]
        if (not isinstance(key,str) or not (valid_guid(key) or re.fullmatch(r"hermes-[0-9a-f]{64}",key))
                or not valid_guid(namespace) or rollback_request_id(namespace,key)!=legacy_key
                or not isinstance(row["fingerprint"],str) or not re.fullmatch(r"[0-9a-f]{64}",row["fingerprint"])):
            raise ValueError("invalid rewrite queue replay request mapping")
        tid = row["task_id"]
        if not tid or row["actor"]!="hermes" or row["recorded_fingerprint"]!=row["fingerprint"]:
            raise ValueError("legacy queue replay receipt is missing or mismatched")
        if key==legacy_key:
            continue  # The normal legacy import already recorded the same key.
        if tid in mapping:
            inserted = db.execute("""INSERT INTO operations(actor,key,request,response)
                SELECT actor,?,request,response FROM operations WHERE actor='hermes' AND key=?""",
                (key,legacy_key)).rowcount
            if inserted!=1:
                raise ValueError("legacy queued order is missing its migrated creation receipt")
        db.execute("INSERT INTO history_request_keys VALUES ('hermes',?,?,?)",(key,source,tid))


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
