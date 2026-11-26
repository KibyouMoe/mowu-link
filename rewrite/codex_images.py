"""Copy only explicitly approved, bounded native image views into the Worker media store."""
from contextlib import ExitStack
import errno
import hashlib
import os
from pathlib import Path
import stat

from .feishu_inbound import image_format
from .storage import Database, Fault, require, string


LIMIT = 10 * 1024 * 1024
CHUNK = 256 * 1024


class CodexImages:
    def __init__(self,root,db_path):
        root = Path(root)
        require(os.name=="posix" and root.is_absolute() and root.is_dir() and not root.is_symlink(),
                422,"private_public_image_directory_required")
        owner = root.stat()
        require(owner.st_uid==os.getuid() and not owner.st_mode&0o077,
                422,"private_public_image_directory_required")
        self.configured,self.root = root,root.resolve()
        self.db = Database(db_path,"""
            CREATE TABLE IF NOT EXISTS codex_image_binding (slot INTEGER PRIMARY KEY CHECK(slot=1), root TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS codex_image_exports (
                event_id TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT, size INTEGER, hash TEXT,
                bytes BLOB, attachment_id TEXT
            );
        """)
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO codex_image_binding VALUES (1,?)",(str(self.root),))
            require(db.execute("SELECT root FROM codex_image_binding WHERE slot=1").fetchone()[0]==str(self.root),
                    409,"native_image_root_changed")

    def reference(self,filename):
        if not isinstance(filename,str):
            return None
        path = Path(filename)
        for root in (self.configured,self.root):
            if path.is_relative_to(root):
                relative = path.relative_to(root)
                if relative.parts and ".." not in relative.parts:
                    return relative.as_posix()
        return None

    def capture(self,path):
        parts = Path(path).parts
        require(parts and not Path(path).is_absolute() and ".." not in parts,409,"invalid_native_image_reference")
        directory_flags = os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
        try:
            with ExitStack() as descriptors:
                folder = os.open(self.root,directory_flags)
                descriptors.callback(os.close,folder)
                for part in parts[:-1]:
                    folder = os.open(part,directory_flags,dir_fd=folder)
                    descriptors.callback(os.close,folder)
                fd = os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=folder)
                with os.fdopen(fd,"rb") as image:
                    before = os.fstat(image.fileno())
                    if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=LIMIT:
                        return None
                    raw = image.read(LIMIT+1)
                    after = os.fstat(image.fileno())
        except OSError as error:
            if error.errno in (errno.ELOOP,errno.ENOTDIR,errno.EACCES,errno.EPERM):
                return None
            raise Fault(503,"native_public_image_unconfirmed",True) from None
        if (before.st_size,before.st_mtime_ns)!=(len(raw),after.st_mtime_ns):
            raise Fault(503,"native_public_image_unconfirmed",True)
        try:
            return "codex-image."+image_format(raw),raw
        except Fault:
            return None

    def stage(self,event_id,path,worker):
        with self.db.transaction() as db:
            prior = db.execute("SELECT * FROM codex_image_exports WHERE event_id=?",(event_id,)).fetchone()
            if not prior:
                snapshot = self.capture(path)
                if not snapshot:
                    db.execute("INSERT INTO codex_image_exports(event_id,path) VALUES (?,?)",(event_id,path))
                    return None
                name,raw = snapshot
                db.execute("INSERT INTO codex_image_exports VALUES (?,?,?,?,?,?,NULL)",
                           (event_id,path,name,len(raw),hashlib.sha256(raw).hexdigest(),raw))
                prior = db.execute("SELECT * FROM codex_image_exports WHERE event_id=?",(event_id,)).fetchone()
            require(prior["path"]==path,409,"native_image_reference_conflict")
        if not prior["name"] or prior["attachment_id"]:
            return prior["attachment_id"]
        route = "worker/media/uploads"
        metadata = {"name":prior["name"],"size_bytes":prior["size"],"sha256":prior["hash"]}
        status = worker.request("POST",route,metadata,event_id)
        upload_id = string(status,"upload_id")
        raw = prior["bytes"]
        while status.get("complete") is False:
            start = status.get("next_offset")
            require(type(start) is int and 0<=start<len(raw),409,"invalid_native_image_offset")
            piece = raw[start:start+CHUNK]
            status = worker.request("PATCH",route+"/"+upload_id,piece,headers={"Upload-Offset":str(start)})
            require(status.get("upload_id")==upload_id and status.get("next_offset")==start+len(piece),
                    409,"native_image_upload_unconfirmed")
        require(status.get("complete") is True and status.get("next_offset")==len(raw),502,"native_image_upload_unconfirmed")
        attachment_id = string(status,"attachment_id")
        with self.db.transaction() as db:
            db.execute("UPDATE codex_image_exports SET attachment_id=?,bytes=NULL WHERE event_id=?",(attachment_id,event_id))
        return attachment_id
