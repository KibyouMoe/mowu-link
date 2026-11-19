"""Immutable task descriptions and evidence. Only membership of active_tasks is mutable."""

import contextlib
import json
import math
import time
import uuid
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS worker_task_corrections(
 id INTEGER PRIMARY KEY, worker TEXT NOT NULL, task_id TEXT NOT NULL, attempts INTEGER NOT NULL,
 cleared_at REAL, notified_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS worker_task_correction_open
 ON worker_task_corrections(worker) WHERE cleared_at IS NULL;
CREATE TABLE IF NOT EXISTS task_info(
 task_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, owner TEXT NOT NULL,
 actor TEXT NOT NULL, at REAL NOT NULL, metadata TEXT NOT NULL, request_id TEXT UNIQUE, fingerprint TEXT);
CREATE TABLE IF NOT EXISTS active_tasks(task_id TEXT PRIMARY KEY, worker TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS dispatch_queue(
 seq INTEGER PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, worker TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dispatch_queue_worker ON dispatch_queue(worker,seq);
CREATE TABLE IF NOT EXISTS task_facts(
 seq INTEGER PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, event_key TEXT NOT NULL,
 at REAL NOT NULL, data TEXT NOT NULL, UNIQUE(task_id,event_key));
CREATE INDEX IF NOT EXISTS task_facts_kind ON task_facts(kind,seq);
"""


def settings(path=None):
    data = json.loads(Path(path).expanduser().read_text()) if path else {}
    minutes = data.get("idle_task_end_minutes", 1)
    attempts = data.get("summary_request_max_attempts", 3)
    if type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes <= 0:
        raise ValueError("idle_task_end_minutes must be positive and finite")
    if type(attempts) is not int or attempts < 1:
        raise ValueError("summary_request_max_attempts must be a positive integer")
    return minutes * 60, attempts


class Ledger:
    def __init__(self, store):
        self.store, self.db = store, store._conn
        self.clock = time.time
        self.db.executescript(DDL)
        for table in ("task_info", "task_facts"):
            for op in ("UPDATE", "DELETE"):
                self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'task history is append-only'); END"
                )
        self.legacy = {r["name"] for r in self.db.execute("PRAGMA table_info(tasks)")}

    @contextlib.contextmanager
    def atomic(self):
        with self.store._lock:
            self.db.execute("SAVEPOINT task_change")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK TO task_change")
                raise
            finally:
                self.db.execute("RELEASE task_change")

    def add(self, tid, kind, key, **data):
        with self.store._lock:
            return (
                self.db.execute(
                    "INSERT INTO task_facts(task_id,kind,event_key,at,data) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(task_id,event_key) DO NOTHING",
                    (tid, kind, key, self.clock(), json.dumps(data)),
                ).rowcount
                == 1
            )

    def facts(self, tid):
        return [
            dict(json.loads(r["data"]), kind=r["kind"], at=r["at"])
            for r in self.db.execute("SELECT * FROM task_facts WHERE task_id=? ORDER BY seq", (tid,))
        ]

    def held(self, name):
        with self.store._lock:
            r = self.db.execute("SELECT task_id FROM active_tasks WHERE worker=?", (name,)).fetchone()
            return r[0] if r else None

    def create(self, name, desc, worker, meta, actor, request_id, fingerprint):
        tid = "t-" + uuid.uuid4().hex[:10]
        with self.atomic():
            self.db.execute(
                "INSERT INTO task_info VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    name,
                    desc,
                    worker,
                    actor,
                    time.time(),
                    json.dumps(meta, ensure_ascii=False),
                    request_id,
                    fingerprint,
                ),
            )
            self.db.execute("INSERT INTO dispatch_queue(task_id,worker) VALUES(?,?)", (tid, worker))
        return tid

    def next_dispatch(self, worker):
        with self.atomic():
            held = self.held(worker)
            if held:
                return held
            row = self.db.execute(
                "SELECT task_id FROM dispatch_queue WHERE worker=? ORDER BY seq LIMIT 1", (worker,)
            ).fetchone()
            if not row:
                return None
            tid = row["task_id"]
            self.db.execute("INSERT INTO active_tasks(task_id,worker) VALUES(?,?)", (tid, worker))
            self.db.execute("DELETE FROM dispatch_queue WHERE task_id=?", (tid,))
            return tid

    def pending_cancellations(self):
        with self.store._lock:
            return self.db.execute(
                "SELECT a.task_id, f.at FROM active_tasks a JOIN task_facts f "
                "ON f.task_id=a.task_id AND f.event_key='cancel:1'"
            ).fetchall()

    def archive(self, tid, why, **data):
        self.add(tid, "archive", "archive", reason=why, **data)
        self.db.execute(
            "UPDATE approvals SET status='denied', decision='denied', note=?, decided_at=? "
            "WHERE task_id=? AND status='pending'",
            (why, time.time(), tid),
        )
        self.db.execute("DELETE FROM dispatch_queue WHERE task_id=?", (tid,))
        self.db.execute("DELETE FROM active_tasks WHERE task_id=?", (tid,))
        self.db.execute("UPDATE workers SET task_id=NULL WHERE task_id=?", (tid,))

    def get(self, tid):
        with self.store._lock:
            r = self.db.execute(
                "SELECT i.*,a.task_id AS member FROM task_info i LEFT JOIN active_tasks a "
                "ON a.task_id=i.task_id WHERE i.task_id=?",
                (tid,),
            ).fetchone()
            if not r:
                return self.historical(tid)
            info = dict(TaskID=tid, TaskName=r["name"], TaskDesc=r["description"], TaskOwner=r["owner"])
            facts = self.facts(tid)
            latest = {f["kind"]: f for f in facts}
            report = latest.get("summary", {}).get("report")
            cancelling = "cancel_request" in latest
            return {
                **json.loads(r["metadata"]),
                "task_info": info,
                "task_id": tid,
                "worker": r["owner"],
                "goal": r["description"],
                "created_at": r["at"],
                "actor": r["actor"],
                "in_running_tasks": r["member"] is not None,
                **({"state": "cancelling" if cancelling else "running"} if r["member"] else {}),
                "facts": facts,
                "archive": latest.get("archive"),
                "probe_id": None if cancelling else latest.get("summary_request", {}).get("request_id"),
                "request_kind": "cancel" if cancelling else "summary",
                "request_id": (latest.get("cancel_request") or latest.get("summary_request") or {}).get(
                    "request_id"
                ),
                "deliverable": report,
            }

    def historical(self, tid):
        if not self.legacy:
            return None
        cols = sorted(
            self.legacy
            & {"task_id", "goal", "worker", "created_at", "context", "workflow_json", "result_json"}
        )
        row = self.db.execute(
            "SELECT " + ",".join(cols) + " FROM tasks WHERE task_id=?", (tid,)
        ).fetchone()
        if not row:
            return None

        def obj(raw):
            try:
                value = json.loads(raw or "{}")
            except (TypeError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}

        item = dict(row)
        flow = obj(item.pop("workflow_json", None))
        historic_result = obj(item.pop("result_json", None))
        return {
            **item,
            "task_info": {
                "TaskID": tid,
                "TaskOwner": item["worker"],
                "TaskName": item["goal"],
                "TaskDesc": item["goal"],
            },
            "legacy": True,
            "in_running_tasks": False,
            "facts": [],
            "deliverable": flow.get("deliverable"),
            "historical_summary": historic_result.get("summary"),
        }

    def listing(self, worker=None, active_only=False, limit=100):
        with self.store._lock:
            query = "SELECT i.task_id,i.owner AS worker,i.at FROM task_info i"
            if active_only:
                query += " JOIN active_tasks a ON a.task_id=i.task_id"
            elif self.legacy:
                query += " UNION ALL SELECT task_id,worker,created_at AS at FROM tasks"
            args = []
            query = "SELECT task_id FROM (" + query + ")"
            if worker:
                query += " WHERE worker=?"
                args.append(worker)
            rows = self.db.execute(
                query + " ORDER BY at DESC,task_id DESC LIMIT ?", args + [limit]
            ).fetchall()
            return [self.get(row[0]) for row in rows]
