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
CREATE TABLE IF NOT EXISTS peer_host_receipts (
 message_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL UNIQUE, receipt_id TEXT UNIQUE NOT NULL, recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS peer_delivery_lookup ON deliveries(consumer,json_extract(payload,'$.message_id'))
 WHERE kind='peer.message';
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


def status(db,worker,mid):
    require(valid_guid(mid),422,"invalid_peer_message")
    row = db.execute("""SELECT sender,recipient,accepted_at FROM peer_messages WHERE message_id=?
                        AND (sender=? OR recipient=?)""",(mid,worker,worker)).fetchone()
    require(row,404,"peer_message_not_found")
    frame = db.execute("""SELECT delivery_id,ack FROM deliveries WHERE consumer=? AND kind='peer.message'
                        AND json_extract(payload,'$.message_id')=?""",("worker/"+row["recipient"],mid)).fetchone()
    require(frame,409,"peer_delivery_unconfirmed")
    native = db.execute("SELECT receipt_id,recorded_at FROM peer_host_receipts WHERE message_id=?",(mid,)).fetchone()
    return {"message_id":mid,**dict(row),"delivery_id":frame["delivery_id"],"worker_received":bool(frame["ack"]),
            "host_receipt_id":native["receipt_id"] if native else None,
            "host_receipt_recorded_at":native["recorded_at"] if native else None}


def confirm(db,worker,mid,body,now):
    require(set(body)=={"session_id","delivery_id","receipt_id"} and valid_guid(body.get("receipt_id")),
            422,"invalid_peer_receipt")
    observed = status(db,worker,mid)
    require(observed["recipient"]==worker,403,"wrong_peer_recipient")
    require(observed["worker_received"] and body["delivery_id"]==observed["delivery_id"],409,"peer_delivery_unconfirmed")
    previous = observed["host_receipt_id"]
    if previous:
        require(previous==body["receipt_id"],409,"peer_receipt_conflict")
    else:
        inserted = db.execute("INSERT OR IGNORE INTO peer_host_receipts VALUES (?,?,?,?)",
                              (mid,body["delivery_id"],body["receipt_id"],timestamp(now))).rowcount
        require(inserted,409,"peer_receipt_conflict")
    return status(db,worker,mid)
