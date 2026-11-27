"""Manager-only, historical projection of a verified legacy snapshot. No work-order writes."""
import hashlib
import json
import re
from uuid import UUID, uuid5

from .messages import valid_guid
from .storage import encode, require

REVIEWED_ARCHIVE = "reviewed_on_rewrite"
CANCELLED_ARCHIVE = "cancelled_on_rewrite"
POOL_CLOSED_ARCHIVE = "pool_closed_on_rewrite"
FAILED_ARCHIVE = "failed_on_rewrite"
QUEUE_SUPERSEDED = "queue_replaced_on_rewrite"


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
ASSIGNED_REPLAY_SCHEMA = """CREATE TABLE IF NOT EXISTS rewrite_assigned_replay_keys (
    legacy_request_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
    trial_source_id TEXT NOT NULL, trial_task_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
    request_sha256 TEXT NOT NULL, retired_keys TEXT NOT NULL, archived INTEGER NOT NULL
)"""
ASSIGNED_HISTORY_SCHEMA = """CREATE TABLE IF NOT EXISTS history_assigned_creations (
    request_key TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, response TEXT NOT NULL
)"""


def rollback_request_id(source_id, key):
    # Preserve the request UUID mapping used by existing queue replay plans.
    return key if valid_guid(key) else str(uuid5(UUID(source_id),"queue-rollback/"+key))


def assigned_request_id(source_id, key):
    return str(uuid5(UUID(source_id),"assigned-rollback/"+key))


def rollback_archive_id(source_id, trial_task_id):
    return "t-" + uuid5(UUID(source_id),"archive-rollback/"+trial_task_id).hex[:10]


def queue_successor_ids(source_id, trial_task_id):
    namespace = UUID(source_id)
    identifier = uuid5(namespace,"legacy-queue-replacement/"+trial_task_id)
    return "t-"+identifier.hex[:10],str(uuid5(namespace,"legacy-queue-request/"+trial_task_id))


def queue_successor_request(request_id, title, description, worker, metadata):
    body = {"message_id":request_id,"worker":worker,"TaskName":title,"goal":description,
            "execution_mode":"goal","queue_if_offline":True}
    if metadata.get("context") is not None:
        body["context"] = metadata["context"]
    return body


def legacy_request_fingerprint(request):
    # Match the legacy HTTP server's exact request fingerprint, omitting its UUID.
    payload = {name:value for name,value in request.items() if name!="message_id"}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=True,
                                     separators=(",",":")).encode("ascii")).hexdigest()


def populate(db, old, source, sha, mapping):
    check_assigned_key_overlap(old)
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
    preserve_assigned_keys(db,old,source,mapping)
    preserve_queue_successors(db,old,source)


def preserve_queue_successors(db, old, source):
    rows = old.execute("""SELECT a.task_id,a.data,t.data AS key_data,i.metadata
        FROM task_facts a LEFT JOIN task_facts t ON t.task_id=a.task_id
          AND t.kind='rewrite_queue_revisions' AND t.event_key='rewrite_queue_revisions'
        LEFT JOIN task_info i ON i.task_id=a.task_id
        WHERE a.kind='archive' AND json_extract(a.data,'$.reason')=?""",(QUEUE_SUPERSEDED,))
    for row in rows:
        archive = json.loads(row["data"])
        namespace,trial_id,replacement = (archive.get(field) for field in
            ("rollback_source_id","recovery_reference","replacement_task_id"))
        recorded = json.loads(row["key_data"] or "{}")
        keys = recorded.get("keys") if isinstance(recorded,dict) else None
        if (not valid_guid(namespace) or not valid_guid(trial_id) or not isinstance(keys,list) or not keys
                or any(not isinstance(key,str) or not key.strip() for key in keys) or len(set(keys))!=len(keys)
                or replacement==row["task_id"] or row["metadata"] is None):
            raise ValueError("invalid legacy queue replacement history")
        expected_id,expected_request = queue_successor_ids(namespace,trial_id)
        successor = old.execute("SELECT * FROM task_info WHERE task_id=?",(expected_id,)).fetchone()
        if (replacement!=expected_id or not successor or successor["actor"]!="hermes"
                or successor["request_id"]!=expected_request or successor["metadata"]!=row["metadata"]):
            raise ValueError("legacy queue replacement does not match its successor")
        request = queue_successor_request(expected_request,successor["name"],successor["description"],
                                          successor["owner"],json.loads(successor["metadata"]))
        if successor["fingerprint"]!=legacy_request_fingerprint(request):
            raise ValueError("legacy queue replacement fingerprint changed")
        for key in keys:
            inserted = db.execute("""INSERT INTO history_request_keys VALUES ('hermes',?,?,?)
                                    ON CONFLICT(actor,request_key) DO NOTHING""",(key,source,row["task_id"])).rowcount
            if inserted!=1:
                raise ValueError("legacy queue revision key conflicts with another recorded request")


def has_rollback_table(old, name):
    table = old.execute("SELECT type FROM sqlite_master WHERE name=?",(name,)).fetchone()
    if table is None:
        return False
    if table[0]!="table":
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
                or archive.get("reason") not in (REVIEWED_ARCHIVE,CANCELLED_ARCHIVE,POOL_CLOSED_ARCHIVE,FAILED_ARCHIVE)
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


def remember_assigned_replay(db, source, trial_id, request, proof, *, archived=False):
    key = proof["original_creation"]["key"]
    legacy_key = request["message_id"]
    retired = [event["key"] for event in proof["events"]]
    if legacy_key!=assigned_request_id(source,key) or len(set([key,legacy_key,*retired]))!=len(retired)+2:
        raise ValueError("assigned rollback request keys overlap")
    db.execute(ASSIGNED_REPLAY_SCHEMA)
    db.execute("INSERT INTO rewrite_assigned_replay_keys VALUES (?,?,?,?,?,?,?,?)",
               (legacy_key,key,source,trial_id,legacy_request_fingerprint(request),
                proof["original_request_sha256"],encode(retired),int(archived)))


def check_assigned_key_overlap(db):
    if not has_rollback_table(db,"rewrite_assigned_replay_keys"):
        return
    rows = list(db.execute("SELECT legacy_request_id,request_key,retired_keys FROM rewrite_assigned_replay_keys"))
    projections = {row[0] for row in rows}
    aliases = set()
    for _,key,raw in rows:
        retired = json.loads(raw)
        if not isinstance(retired,list) or not retired or any(not isinstance(alias,str) or not alias.strip() for alias in retired):
            raise ValueError("invalid assigned rollback action keys")
        keys = [key,*retired]
        if len(set(keys))!=len(keys) or aliases.intersection(keys) or projections.intersection(keys):
            raise ValueError("assigned rollback request keys overlap")
        aliases.update(keys)
    keys_json = encode(sorted(aliases))
    if db.execute("SELECT 1 FROM task_info WHERE request_id IN (SELECT value FROM json_each(?))",(keys_json,)).fetchone():
        raise ValueError("assigned rollback key overlaps a legacy request")
    archive_keys = "rewrite_archive_request_keys"
    if has_rollback_table(db,archive_keys) and db.execute(
            "SELECT 1 FROM rewrite_archive_request_keys WHERE request_key IN (SELECT value FROM json_each(?))",
            (keys_json,)).fetchone():
        raise ValueError("assigned rollback key overlaps an archived request")
    if has_rollback_table(db,"rewrite_queue_replay_keys") and db.execute("""
            SELECT 1 FROM rewrite_queue_replay_keys
            WHERE request_key IN (SELECT value FROM json_each(?))
            OR legacy_request_id IN (SELECT value FROM json_each(?))""",
            (keys_json,encode(sorted(aliases|projections)))).fetchone():
        raise ValueError("assigned rollback key overlaps a queued request")


def preserve_assigned_keys(db, old, source, mapping):
    if not has_rollback_table(old,"rewrite_assigned_replay_keys"):
        return
    for row in old.execute("""SELECT r.*,t.task_id,t.actor,t.fingerprint AS recorded_fingerprint,f.data AS archive
        FROM rewrite_assigned_replay_keys r LEFT JOIN task_info t ON t.request_id=r.legacy_request_id
        LEFT JOIN task_facts f ON f.task_id=t.task_id AND f.kind='archive' AND f.event_key='archive'"""):
        key, namespace, legacy_key = row["request_key"],row["trial_source_id"],row["legacy_request_id"]
        tid,trial_id = row["task_id"],row["trial_task_id"]
        retired = json.loads(row["retired_keys"])
        if (not isinstance(key,str) or not (valid_guid(key) or re.fullmatch(r"hermes-[0-9a-f]{64}",key))
                or not valid_guid(namespace) or not valid_guid(trial_id) or assigned_request_id(namespace,key)!=legacy_key
                or row["archived"] not in (0,1)
                or not isinstance(row["fingerprint"],str) or not re.fullmatch(r"[0-9a-f]{64}",row["fingerprint"])
                or not isinstance(row["request_sha256"],str) or not re.fullmatch(r"[0-9a-f]{64}",row["request_sha256"])
                or not tid or row["actor"]!="hermes" or row["recorded_fingerprint"]!=row["fingerprint"]):
            raise ValueError("assigned rollback request receipt is missing or mismatched")
        if row["archived"]:
            archive = json.loads(row["archive"] or "null")
            if (tid in mapping or not isinstance(archive,dict) or archive.get("recovery_reference")!=trial_id
                    or archive.get("reason") not in (REVIEWED_ARCHIVE,CANCELLED_ARCHIVE,POOL_CLOSED_ARCHIVE,FAILED_ARCHIVE)
                    or rollback_archive_id(namespace,trial_id)!=tid):
                raise ValueError("assigned rollback request does not match its legacy archive")
        if tid in mapping:
            receipt = db.execute("SELECT response FROM operations WHERE actor='hermes' AND key=?",(legacy_key,)).fetchone()
            if not receipt:
                raise ValueError("assigned rollback is missing the queue replay receipt")
            db.execute(ASSIGNED_HISTORY_SCHEMA)
            db.execute("INSERT INTO history_assigned_creations VALUES (?,?,?)",(key,row["request_sha256"],receipt[0]))
        db.executemany("INSERT INTO history_request_keys VALUES ('hermes',?,?,?)",
                       [(alias,source,tid) for alias in [key,*retired]])


def replay_assigned_creation(db, key, request):
    if not has_rollback_table(db,"history_assigned_creations"):
        return None
    row = db.execute("SELECT * FROM history_assigned_creations WHERE request_key=?",(key,)).fetchone()
    if row:
        require(row["request_sha256"]==hashlib.sha256(request.encode()).hexdigest(),409,"idempotency_conflict")
        return json.loads(row["response"])
    return None


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
