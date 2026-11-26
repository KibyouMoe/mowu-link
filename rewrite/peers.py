"""Ordinary peer messages do not hold, create or complete a formal task."""
from . import attachments, delivery, peer_files
from .messages import valid_guid
from .storage import require, string, timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS peer_messages (
 message_id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL, text TEXT NOT NULL,
 reply_to TEXT, accepted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS peer_files (
 message_id TEXT NOT NULL, position INTEGER NOT NULL, attachment_id TEXT NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(message_id,position)
);
"""


def submit(db, sender, target, body, now):
    require(set(body) <= {"message_id","text","reply_to","session_id","attachment_ids","attachment_kinds"},422,"invalid_peer_message")
    mid,text = string(body,"message_id"),body.get("text")
    ids,kinds = peer_files.request_files(body)
    require(isinstance(text,str) and (text.strip() or ids),422,"invalid_peer_message")
    require(valid_guid(mid) and target != sender,422,"invalid_peer_message")
    require(db.execute("SELECT 1 FROM workers WHERE worker_id=?", (target,)).fetchone(),404,"peer_not_registered")
    parent = body.get("reply_to")
    if parent is not None:
        require(valid_guid(parent) and db.execute(
            "SELECT 1 FROM peer_messages WHERE message_id=? AND sender=? AND recipient=?",
            (parent,target,sender)).fetchone(),409,"unknown_peer_parent")
    previous = db.execute("SELECT * FROM peer_messages WHERE message_id=?", (mid,)).fetchone()
    if previous:
        stored = db.execute("SELECT attachment_id,kind FROM peer_files WHERE message_id=? ORDER BY position",(mid,)).fetchall()
        require([tuple(r) for r in stored]==list(zip(ids,kinds)),409,"peer_message_conflict")
        require((previous["sender"],previous["recipient"],previous["text"],previous["reply_to"]) ==
                (sender,target,text,parent),409,"peer_message_conflict")
        accepted = previous["accepted_at"]
    else:
        accepted = timestamp(now)
        db.execute("INSERT INTO peer_messages VALUES (?,?,?,?,?,?)",(mid,sender,target,text,parent,accepted))
        payload = {"message_id":mid,"from_worker":sender,"to_worker":target,"text":text,"reply_to":parent}
        if ids:
            items = peer_files.file_list(db,"worker/"+sender,ids,kinds,owned=True)
            attachments.grant(db,"worker/"+sender,"worker/"+target,ids)
            db.executemany("INSERT INTO peer_files VALUES (?,?,?,?)",((mid,i,fid,kinds[i]) for i,fid in enumerate(ids)))
            payload["attachments"] = items
        delivery.enqueue(db,"worker/"+target,"peer.message",payload)
    return {"message_id":mid,"accepted_at":accepted,"status":"accepted"}
