"""Provider-independent Trigger. The local SQLite archive survives process restarts."""
import hashlib
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid5

from .messages import valid_guid
from .storage import Database, Fault, encode, guid, require, timestamp


def archive_status(path):
    require(path.is_file(),404,"trigger_archive_missing")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=5)) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT id FROM instance WHERE slot=1").fetchone()
            require(row,409,"trigger_instance_missing")
            archived,waiting,latest,acknowledged = db.execute("""
                SELECT COUNT(*),COUNT(*) FILTER (WHERE receipt IS NULL),
                       MAX(json_extract(body,'$.generated_at')),MAX(json_extract(receipt,'$.received_at'))
                FROM archive""").fetchone()
    except sqlite3.DatabaseError as exc:
        raise Fault(409,"trigger_archive_unavailable") from exc
    return {"instance_id":row[0],"archived":archived,"awaiting_router_receipt":waiting,
            "latest_generated_at":latest,"latest_router_received_at":acknowledged}


class Trigger:
    def __init__(self, path, name, clock=time.time, *, instance_id=None):
        require(instance_id is None or valid_guid(instance_id),422,"invalid_trigger_instance")
        self.name, self.clock = name, clock
        self.db = Database(path, """
          CREATE TABLE IF NOT EXISTS instance (slot INTEGER PRIMARY KEY CHECK(slot=1),id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS archive (id TEXT PRIMARY KEY,body TEXT NOT NULL,receipt TEXT);
          CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY,message_id TEXT,position INTEGER,name TEXT,
             size INTEGER,sha TEXT,upload_id TEXT,file_id TEXT,UNIQUE(message_id,position));
          CREATE TABLE IF NOT EXISTS pieces (id TEXT,start INTEGER,bytes BLOB NOT NULL,PRIMARY KEY(id,start));
        """)
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO instance VALUES (1,?)", (instance_id or guid(),))
            self.instance_id = db.execute("SELECT id FROM instance").fetchone()[0]
            require(instance_id is None or self.instance_id==instance_id,409,"trigger_instance_mismatch")

    def record(self, level, text, paths=()):
        with ExitStack() as stack, self.db.transaction() as db:
            streams = [(p.name,stack.enter_context(p.open("rb"))) for p in map(Path,paths)]
            return self.archive(db,level,text,streams)

    def archive(self, db, level, text, streams, *, origin=None):
        """The source may commit its cursor in this same transaction."""
        require(level in ("Event","Notify","Info") and isinstance(text,str) and len(text)<=200
                and (text.strip() or streams),422,"invalid_trigger_message")
        mid = origin["message_id"] if origin else guid()
        body = {"message_id":mid,"trigger_name":self.name,"instance_id":self.instance_id,
                "generated_at":timestamp(self.clock()),"level":level,"text":text,"attachment_ids":[]}
        if origin:
            body["trigger_name"] = origin["trigger_name"]
            body["generated_at"] = origin["generated_at"]
        db.execute("INSERT INTO archive VALUES (?,?,NULL)",(mid,encode(body)))
        for position,(name,stream) in enumerate(streams):
            fid = str(uuid5(UUID(mid),f"attachment/{position}")) if origin else guid()
            offset,digest = 0,hashlib.sha256()
            while chunk := stream.read(256*1024):
                db.execute("INSERT INTO pieces VALUES (?,?,?)",(fid,offset,chunk))
                offset += len(chunk)
                digest.update(chunk)
            db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,NULL,NULL)",
                       (fid,mid,position,name,offset,digest.hexdigest()))
        return mid

    def upload(self, router, file, *, stop=None):
        if stop is not None and stop.is_set():
            raise InterruptedError
        fid = file["id"]
        if file["file_id"]:
            return file["file_id"]
        if file["upload_id"]:
            uid = file["upload_id"]
            status = router.request("GET", f"uploads/{uid}")
        else:
            status = router.request("POST", "uploads", {"name":file["name"], "size_bytes":file["size"],
                                    "sha256":file["sha"]}, "upload-" + fid)
            uid = status["upload_id"]
            with self.db.transaction() as db:
                db.execute("UPDATE files SET upload_id=? WHERE id=?", (uid, fid))
        while not status["complete"]:
            if stop is not None and stop.is_set():
                raise InterruptedError
            pos = status["next_offset"]
            with self.db.transaction() as db:
                chunk = db.execute("SELECT bytes FROM pieces WHERE id=? AND start=?", (fid, pos)).fetchone()
            require(chunk, 409, "unrecognized_upload_offset")
            status = router.request("PATCH", f"uploads/{uid}", chunk[0], headers={"Upload-Offset":str(pos)})
            require(status["next_offset"] > pos, 409, "upload_did_not_advance")
        require(status["next_offset"] == file["size"] and status.get("attachment_id"), 409, "unconfirmed_upload")
        with self.db.transaction() as db:
            db.execute("UPDATE files SET file_id=? WHERE id=?", (status["attachment_id"], fid))
        return status["attachment_id"]

    def send(self, router, mid, *, stop=None):
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM archive WHERE id=?", (mid,)).fetchone()
            require(row, 404, "local_message_not_found")
            if row["receipt"]:
                return json.loads(row["receipt"])
            files = db.execute("SELECT * FROM files WHERE message_id=? ORDER BY position", (mid,)).fetchall()
        body = json.loads(row["body"])
        body["attachment_ids"] = [self.upload(router,file,stop=stop) for file in files]
        if stop is not None and stop.is_set():
            raise InterruptedError
        receipt = router.request("POST", "messages", body, "message-" + mid)
        require(receipt.get("message_id") == mid and receipt.get("received_at"), 409, "missing_message_receipt")
        with self.db.transaction() as db:
            db.execute("UPDATE archive SET body=?,receipt=? WHERE id=?", (encode(body), encode(receipt), mid))
        return receipt

    def pending(self, *, limit=-1):
        with self.db.transaction() as db:
            return [row[0] for row in db.execute("SELECT id FROM archive WHERE receipt IS NULL ORDER BY rowid LIMIT ?",
                                                 (limit,))]
