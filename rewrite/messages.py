"""Trigger admission and manager message archive, written by the Router."""
from datetime import datetime
import json
from uuid import UUID

from . import attachments, delivery
from .storage import encode, guid, require, string, timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
 message_id TEXT PRIMARY KEY, owner TEXT NOT NULL, source TEXT NOT NULL, level TEXT NOT NULL,
 received_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_versions (version INTEGER PRIMARY KEY);
-- Old delivery frames prove that the Router queued the message, but have no observation time.
INSERT OR IGNORE INTO messages(message_id,owner,source,level,received_at,payload)
 SELECT json_extract(payload,'$.message_id'),'router',json_extract(payload,'$.source'),
        json_extract(payload,'$.level'),'',payload
 FROM deliveries WHERE consumer='hermes' AND kind='message.deliver'
 AND NOT EXISTS (SELECT 1 FROM archive_versions WHERE version=1);
INSERT OR IGNORE INTO archive_versions VALUES (1);
"""


def valid_guid(value):
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def submit(db, actor, data, now):
    fields = {"message_id","instance_id","trigger_name","level","text","generated_at","attachment_ids"}
    require(set(data) == fields, 422, "invalid_message")
    mid, instance = data["message_id"], data["instance_id"]
    require(valid_guid(mid) and valid_guid(instance), 422, "invalid_identifier")
    require(actor == "trigger/" + instance, 403, "wrong_instance")
    source = string(data, "trigger_name")
    text = data["text"]
    require(isinstance(text, str) and len(text) <= 200 and (text.strip() or data["attachment_ids"]),
            422, "invalid_message_text")
    level = data["level"]
    require(level in ("Event", "Notify", "Info"), 422, "invalid_level")
    try:
        when = datetime.fromisoformat(string(data, "generated_at").replace("Z", "+00:00"))
    except ValueError:
        require(False, 422, "invalid_generated_at")
    require(when.tzinfo is not None, 422, "invalid_generated_at")
    previous = db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone()
    if previous:
        require(previous["owner"] == actor and previous["payload"] == encode(data), 409, "message_conflict")
        return {"message_id": mid, "received_at": previous["received_at"], "duplicate": True}
    attachments.grant(db, actor, "hermes", data["attachment_ids"])
    received = timestamp(now)
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", (mid, actor, source, level, received, encode(data)))
    if level != "Info":
        delivery.enqueue(db, "hermes", "message.deliver", {**data, "source": source})
    return {"message_id": mid, "received_at": received, "duplicate": False}


def notify(db, now, tid, source, text, ids=(), **details):
    mid = guid()
    data = {"message_id":mid,"level":"Notify","task_id":tid,"source":source,"text":text,
            "attachment_ids":list(ids),**details}
    db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
               (mid,"router",source,"Notify",timestamp(now),encode(data)))
    delivery.enqueue(db,"hermes","message.deliver",data)


def public(row):
    return dict(json.loads(row["payload"]), source=row["source"], received_at=row["received_at"] or None)
