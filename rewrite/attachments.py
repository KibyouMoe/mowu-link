"""Router-owned immutable upload chunks and per-recipient attachment grants."""
import hashlib
import re

from .storage import Fault, guid, require, string

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
 upload_id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL, next_offset INTEGER NOT NULL, attachment_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS chunks (
 upload_id TEXT NOT NULL, start INTEGER NOT NULL, bytes BLOB NOT NULL, PRIMARY KEY(upload_id,start)
);
CREATE TABLE IF NOT EXISTS attachment_grants (
 attachment_id TEXT NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(attachment_id,actor)
);
CREATE TABLE IF NOT EXISTS business_files (
 kind TEXT NOT NULL, reference_id TEXT NOT NULL, position INTEGER NOT NULL, attachment_id TEXT NOT NULL,
 PRIMARY KEY(kind,reference_id,position)
);
"""


def progress(row):
    result = {"upload_id": row["upload_id"], "next_offset": row["next_offset"], "complete": bool(row["attachment_id"])}
    if row["attachment_id"]:
        result["attachment_id"] = row["attachment_id"]
    return result


def owned(db, actor, upload_id):
    row = db.execute("SELECT * FROM uploads WHERE owner=? AND upload_id=?", (actor, upload_id)).fetchone()
    require(row, 404, "upload_not_found")
    return row


def begin(db, actor, body):
    require(set(body) == {"name","size_bytes","sha256"}, 422, "invalid_upload")
    size, digest = body["size_bytes"], string(body, "sha256")
    require(type(size) is int and 0 <= size <= 2**63-1 and re.fullmatch(r"[0-9a-f]{64}", digest),
            422, "invalid_upload")
    if size == 0:
        require(digest == hashlib.sha256(b"").hexdigest(), 422, "digest_mismatch")
    upload_id = guid()
    db.execute("INSERT INTO uploads VALUES (?,?,?,?,?,0,?)",
               (upload_id, actor, string(body, "name"), size, digest, guid() if size == 0 else None))
    return progress(owned(db, actor, upload_id))


def append(db, actor, upload_id, offset, data):
    row = owned(db, actor, upload_id)
    require(re.fullmatch(r"[0-9]{1,19}", offset or ""), 422, "invalid_offset")
    offset = int(offset)
    known = row["next_offset"]
    if offset != known:
        previous = db.execute("SELECT bytes FROM chunks WHERE upload_id=? AND start=?", (upload_id, offset)).fetchone() if offset < known else None
        if previous and previous[0] == data:
            return progress(row)
        raise Fault(409, "offset_conflict", details={"next_offset": known})
    require(data and not row["attachment_id"] and len(data) <= row["size_bytes"] - known, 422, "invalid_chunk")
    db.execute("INSERT INTO chunks VALUES (?,?,?)", (upload_id, offset, data))
    end = offset + len(data)
    file_id = None
    if end == row["size_bytes"]:
        digest = hashlib.sha256()
        for piece in db.execute("SELECT bytes FROM chunks WHERE upload_id=? ORDER BY start", (upload_id,)):
            digest.update(piece[0])
        require(digest.hexdigest() == row["sha256"], 422, "digest_mismatch")
        file_id = guid()
    db.execute("UPDATE uploads SET next_offset=?,attachment_id=? WHERE upload_id=?", (end, file_id, upload_id))
    return progress(owned(db, actor, upload_id))


def authorize(db, actor, file_id):
    row = db.execute("""SELECT * FROM uploads WHERE attachment_id=? AND
           (owner=? OR EXISTS(SELECT 1 FROM attachment_grants WHERE attachment_id=? AND actor=?))""",
                     (file_id, actor, file_id, actor)).fetchone()
    require(row, 404, "attachment_not_found")
    return row


def grant(db, owner, recipient, ids):
    require(isinstance(ids, list) and all(isinstance(i, str) and i for i in ids), 422, "invalid_attachment_ids")
    for file_id in ids:
        authorize(db, owner, file_id)
        db.execute("INSERT OR IGNORE INTO attachment_grants VALUES (?,?)", (file_id, recipient))


def link(db, owner, kind, reference, ids):
    grant(db, owner, "hermes", ids)
    db.executemany("INSERT INTO business_files VALUES (?,?,?,?)",
                   ((kind,reference,pos,file_id) for pos,file_id in enumerate(ids)))


def linked(db, kind, reference):
    return [r[0] for r in db.execute("SELECT attachment_id FROM business_files WHERE kind=? AND reference_id=? ORDER BY position",
                                    (kind,reference))]


class Download:
    def __init__(self, store, row, requested):
        self.store, self.upload = store, row["upload_id"]
        size = row["size_bytes"]
        self.start, self.end = 0, size
        self.headers = {"Accept-Ranges":"bytes", "X-Content-SHA256":row["sha256"]}
        if requested is not None:
            match = re.fullmatch(r"bytes=([0-9]{0,19})-([0-9]{0,19})", requested)
            require(match and any(match.groups()) and size > 0, 416, "invalid_range")
            left, right = match.groups()
            if left:
                self.start = int(left)
                self.end = min(size, int(right)+1) if right else size
            else:
                self.start = max(0, size-int(right))
            require(self.start < self.end, 416, "invalid_range")
            self.headers["Content-Range"] = f"bytes {self.start}-{self.end-1}/{size}"
        self.length = self.end-self.start

    def write_to(self, stream):
        # Completed chunks are immutable. Release the DB before waiting on a slow reader.
        pos = self.start
        while pos < self.end:
            with self.store.transaction() as db:
                row = db.execute("""SELECT start,bytes FROM chunks WHERE upload_id=? AND start<=?
                                 ORDER BY start DESC LIMIT 1""", (self.upload, pos)).fetchone()
            if not row or row["start"] + len(row["bytes"]) <= pos:
                raise OSError("missing immutable attachment chunk")
            part = row["bytes"][pos-row["start"]:self.end-row["start"]]
            stream.write(part)
            pos += len(part)
