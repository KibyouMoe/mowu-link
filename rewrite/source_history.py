"""Read-only source history and idempotency after an audited Router re-entry."""
import json

from .storage import Fault, encode, require


MESSAGE_TABLE = """CREATE TABLE IF NOT EXISTS messages (
 message_id TEXT PRIMARY KEY, owner TEXT NOT NULL, source TEXT NOT NULL, level TEXT NOT NULL,
 received_at TEXT NOT NULL, payload TEXT NOT NULL
);"""

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS history_source_messages (
 message_id TEXT PRIMARY KEY, trial_sha256 TEXT NOT NULL, disposition TEXT NOT NULL,
 transport_id TEXT, transport_received INTEGER, native_id TEXT, turn_id TEXT
);""",
    """CREATE TABLE IF NOT EXISTS history_source_operations (
 actor TEXT NOT NULL, key TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(actor,key)
);""",
    """CREATE TABLE IF NOT EXISTS history_source_uploads (
 upload_id TEXT PRIMARY KEY, actor TEXT NOT NULL
);""",
)
DEFERRED = {"archive_only","not_sent_to_host"}


def installed(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_source_messages'").fetchone()


def live_messages(db, *, actor=None, mid=None, trigger_only=False):
    conditions, values = [], []
    if installed(db):
        conditions.append("NOT EXISTS (SELECT 1 FROM history_source_messages h WHERE h.message_id=m.message_id)")
    if actor is not None:
        conditions.append("m.owner=?")
        values.append(actor)
    elif trigger_only:
        conditions.append("m.owner LIKE 'trigger/%'")
    if mid is not None:
        conditions.append("m.message_id=?")
        values.append(mid)
    where = " WHERE "+" AND ".join(conditions) if conditions else ""
    return db.execute("SELECT m.* FROM messages m"+where+" ORDER BY m.rowid",values)


def observation(db, mid):
    if installed(db):
        row = db.execute("""SELECT trial_sha256,disposition,transport_id,transport_received,native_id,turn_id
                            FROM history_source_messages WHERE message_id=?""",(mid,)).fetchone()
        if row:
            result = dict(row)
            if result["transport_received"] is not None:
                result["transport_received"] = bool(result["transport_received"])
            return result
    return None


def upload(db, actor, upload_id):
    if not actor.startswith("trigger/") or not installed(db):
        return None
    return db.execute("""SELECT u.* FROM uploads u JOIN history_source_uploads h USING(upload_id)
                         WHERE h.actor=? AND u.upload_id=?""",(actor,upload_id)).fetchone()


def replay(db, actor, key, operation, data):
    if not actor.startswith("trigger/") or not installed(db):
        return None
    row = db.execute("""SELECT request,response FROM history_source_operations WHERE actor=? AND key=?""",
                     (actor,key)).fetchone()
    if row:
        require(row["request"]==encode([operation,data]),409,"idempotency_conflict")
    if operation==["POST","/v1/messages"] and isinstance(data.get("message_id"),str):
        prior = db.execute("""SELECT m.owner,m.payload,m.received_at,h.disposition FROM messages m
                              JOIN history_source_messages h USING(message_id) WHERE message_id=?""",
                           (data["message_id"],)).fetchone()
        if prior:
            require(prior["owner"]==actor and prior["payload"]==encode(data),409,"message_conflict")
            if prior["disposition"] in DEFERRED:
                raise Fault(409,"source_returned_to_legacy")
            if not row:
                return {"message_id":data["message_id"],"received_at":prior["received_at"],"duplicate":True}
    return json.loads(row["response"]) if row else None
