"""Provider-independent Trigger. The local SQLite archive survives process restarts."""
import hashlib
import json
from pathlib import Path
import time

from .storage import Database, encode, guid, require, timestamp


class Trigger:
    def __init__(self, path, name, clock=time.time):
        self.name, self.clock = name, clock
        self.db = Database(path, """
          CREATE TABLE IF NOT EXISTS instance (slot INTEGER PRIMARY KEY CHECK(slot=1),id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS archive (id TEXT PRIMARY KEY,body TEXT NOT NULL,receipt TEXT);
          CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY,message_id TEXT,position INTEGER,name TEXT,
             size INTEGER,sha TEXT,upload_id TEXT,file_id TEXT,UNIQUE(message_id,position));
          CREATE TABLE IF NOT EXISTS pieces (id TEXT,start INTEGER,bytes BLOB NOT NULL,PRIMARY KEY(id,start));
        """)
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO instance VALUES (1,?)", (guid(),))
            self.instance_id = db.execute("SELECT id FROM instance").fetchone()[0]

    def record(self, level, text, paths=()):
        require(level in ("Event","Notify","Info") and isinstance(text,str) and len(text)<=200
                and (text.strip() or paths), 422, "invalid_trigger_message")
        mid = guid()
        with self.db.transaction() as db:
            body = {"message_id":mid, "trigger_name":self.name, "instance_id":self.instance_id,
                    "generated_at":timestamp(self.clock()), "level":level, "text":text, "attachment_ids":[]}
            db.execute("INSERT INTO archive VALUES (?,?,NULL)", (mid, encode(body)))
            for position, path in enumerate(map(Path, paths)):
                fid, offset, digest = guid(), 0, hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(256*1024):
                        db.execute("INSERT INTO pieces VALUES (?,?,?)", (fid, offset, chunk))
                        offset += len(chunk)
                        digest.update(chunk)
                db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,NULL,NULL)",
                           (fid, mid, position, path.name, offset, digest.hexdigest()))
        return mid

    def upload(self, router, file):
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

    def send(self, router, mid):
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM archive WHERE id=?", (mid,)).fetchone()
            require(row, 404, "local_message_not_found")
            if row["receipt"]:
                return json.loads(row["receipt"])
            files = db.execute("SELECT * FROM files WHERE message_id=? ORDER BY position", (mid,)).fetchall()
        body = json.loads(row["body"])
        body["attachment_ids"] = [self.upload(router, file) for file in files]
        receipt = router.request("POST", "messages", body, "message-" + mid)
        require(receipt.get("message_id") == mid and receipt.get("received_at"), 409, "missing_message_receipt")
        with self.db.transaction() as db:
            db.execute("UPDATE archive SET body=?,receipt=? WHERE id=?", (encode(body), encode(receipt), mid))
        return receipt

    def pending(self):
        with self.db.transaction() as db:
            return [row[0] for row in db.execute("SELECT id FROM archive WHERE receipt IS NULL ORDER BY rowid")]
