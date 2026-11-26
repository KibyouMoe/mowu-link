"""Shared SQLite transaction and wire primitives. Each component owns its database."""
import json
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def guid():
    return str(uuid4())


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class Fault(Exception):
    def __init__(self, status, code, retryable=False, details=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


def require(condition, status, code):
    if not condition:
        raise Fault(status, code)


def string(data, name):
    value = data.get(name)
    require(isinstance(value, str) and value.strip(), 422, "invalid_" + name)
    return value


class StateWriterBusy(RuntimeError):
    pass


@contextmanager
def state_writer(state_file, *, existing=False):
    """Coordinate a process lifetime with other writers and offline snapshots."""
    path = Path(state_file).resolve()
    lock = path.with_name(path.name+".writer")
    flags = os.O_RDWR | getattr(os,"O_NOFOLLOW",0)
    if not existing:
        flags |= os.O_CREAT
    with os.fdopen(os.open(lock,flags,0o600),"rb") as guard:
        info = os.fstat(guard.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name=="posix" and
                (info.st_uid!=os.getuid() or info.st_mode & 0o077)):
            raise ValueError("expected an owner-private state lock")
        with closing(sqlite3.connect(lock.as_uri()+"?mode=rw",uri=True,timeout=0)) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if (exc.sqlite_errorcode & 255) in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                    raise StateWriterBusy("state_writer_already_running") from None
                raise
            yield


class Database:
    def __init__(self, path, schema):
        self.path = path
        with self.transaction() as db:
            db.executescript(schema)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
