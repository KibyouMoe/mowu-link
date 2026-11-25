"""Shared SQLite transaction and wire primitives. Each component owns its database."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


def guid():
    return str(uuid4())


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class Fault(Exception):
    def __init__(self, status, code, retryable=False):
        super().__init__(code)
        self.status = status
        self.code = code
        self.retryable = retryable


def require(condition, status, code):
    if not condition:
        raise Fault(status, code)


def string(data, name):
    value = data.get(name)
    require(isinstance(value, str) and value.strip(), 422, "invalid_" + name)
    return value


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
