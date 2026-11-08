#!/usr/bin/env python3
"""mowu-router — the worker / task / subscription registry for Hermes.

Implements docs/ROUTER.md ① (workers) ② (tasks + result + notify) ③
(subscription table, trigger forwarding stubbed) at 127.0.0.1:8790.

The router is deliberately dumb:

  * it knows *who is connected*, *who is busy* and *how many tasks each
    worker has queued*;
  * it knows nothing about capabilities, task meaning or worker internals;
  * the only outbound thing it does is write one ``level=notify`` event to
    trigger when a worker reports a result.

Liveness is the SSE connection itself, never a heartbeat: ``POST /heartbeat``
only records the worker's own state/progress.  When a stream ends the worker
goes offline, its name and backlog stay, and its in-flight task goes back to
``queued`` so the next connection receives it again (at-least-once; workers
dedupe by ``task_id``).

Standard library only.  Python 3.9.  Binds loopback only.
"""

from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import logging
import os
import queue
import secrets
import select
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

VERSION = "0.1.0"
HOST = "127.0.0.1"
DEFAULT_PORT = 8790
DEFAULT_DB = "~/.hermes/router.db"
DEFAULT_TOKEN_PATH = "~/.hermes/secrets/router.token"
DEFAULT_LOCK_PATH = "~/.hermes/router.lock"
DEFAULT_LOG = "~/.hermes/router.log"
DEFAULT_TRIGGER_URL = "http://127.0.0.1:8791/trigger"
DEFAULT_TRIGGER_TOKEN_PATH = "~/.hermes/secrets/trigger.token"
BACKLOG_MAX = 128
MAX_BODY_BYTES = 1 * 1024 * 1024
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

TASK_STATES = ("queued", "running", "done", "failed", "cancelled")
TERMINAL_STATES = ("done", "failed", "cancelled")
URGENCIES = ("urgent", "normal")
PING_INTERVAL = 15.0

log = logging.getLogger("router")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def now_iso(ts: Optional[float] = None) -> str:
    stamp = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(stamp).astimezone().isoformat(
        timespec="seconds")


def parse_epoch(value: Any, label: str) -> float:
    """Epoch seconds from a number or an ISO-8601 string."""
    if isinstance(value, bool):
        raise BadRequest("%s must be a timestamp" % label)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text:
            if text[-1:] in ("Z", "z"):
                text = text[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(text).timestamp()
            except ValueError:
                pass
    raise BadRequest("%s must be epoch seconds or ISO-8601" % label)


class BadRequest(Exception):
    pass


class Unauthorized(Exception):
    pass


class NotFound(Exception):
    pass


def setup_logging(log_path: Path, verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            str(log_path), maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        fh.setLevel(logging.DEBUG)
        log.addHandler(fh)
    except OSError as exc:
        print("warning: cannot open log file %s: %s" % (log_path, exc),
              file=sys.stderr)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s"))
    sh.setLevel(logging.INFO)
    log.addHandler(sh)


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    name         TEXT PRIMARY KEY,
    online       INTEGER DEFAULT 0,
    busy         INTEGER DEFAULT 0,
    queued       INTEGER DEFAULT 0,
    last_seen    REAL,
    connected_at REAL,
    version      TEXT,
    note         TEXT,
    state        TEXT,
    progress_at  REAL,
    task_id      TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    worker      TEXT NOT NULL,
    goal        TEXT NOT NULL,
    context     TEXT,
    workspace   TEXT,
    urgency     TEXT DEFAULT 'normal',
    state       TEXT DEFAULT 'queued',
    created_at  REAL,
    started_at  REAL,
    finished_at REAL,
    result_json TEXT,
    deadline_s  REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_worker_state ON tasks(worker, state);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    worker     TEXT NOT NULL,
    source     TEXT NOT NULL,
    kinds      TEXT,
    active     INTEGER DEFAULT 1,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_worker ON subscriptions(worker);
"""


class RouterStore:
    """SQLite store. One connection, one RLock, autocommit."""

    def __init__(self, db_path: str):
        self.path = os.path.abspath(os.path.expanduser(db_path))
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        log.info("store ready: %s", self.path)

    def _migrate(self) -> None:
        cols = {str(row["name"]) for row in
                self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "deadline_s" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN deadline_s REAL")
            log.info("store migration: added tasks.deadline_s")
        wcols = {str(row["name"]) for row in
                 self._conn.execute("PRAGMA table_info(workers)").fetchall()}
        for column, decl in (("state", "TEXT"), ("progress_at", "REAL"),
                             ("task_id", "TEXT")):
            if column not in wcols:
                self._conn.execute(
                    "ALTER TABLE workers ADD COLUMN %s %s" % (column, decl))
                log.info("store migration: added workers.%s", column)

    # -- startup -------------------------------------------------------- #
    def reset_runtime(self) -> int:
        """Fresh process: nobody is online; in-flight tasks go back to queue."""
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET online=0, busy=0, queued=0")
            cur = self._conn.execute(
                "UPDATE tasks SET state='queued', started_at=NULL"
                " WHERE state='running'")
            changed = int(cur.rowcount or 0)
            for row in self._conn.execute("SELECT name FROM workers").fetchall():
                self.refresh_counts(str(row["name"]))
        if changed:
            log.info("startup: %d in-flight task(s) requeued", changed)
        return changed

    # -- workers -------------------------------------------------------- #
    @staticmethod
    def _worker_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "name": row["name"],
            "online": bool(row["online"]),
            "busy": bool(row["busy"]),
            "queued": int(row["queued"] or 0),
            "last_seen": row["last_seen"],
            "last_seen_iso": now_iso(row["last_seen"]) if row["last_seen"] else None,
            "connected_at": row["connected_at"],
            "version": row["version"],
            "note": row["note"],
            "state": row["state"],
            "progress_at": row["progress_at"],
            "task_id": row["task_id"],
        }

    def get_worker(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workers WHERE name=?", (name,)).fetchone()
        return self._worker_dict(row) if row else None

    def list_workers(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workers ORDER BY name").fetchall()
        return [self._worker_dict(row) for row in rows]

    def upsert_worker(self, name: str, version: Optional[str] = None,
                      note: Optional[str] = None) -> Tuple[Dict[str, Any], bool]:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM workers WHERE name=?", (name,)).fetchone()
            created = row is None
            if created:
                self._conn.execute(
                    "INSERT INTO workers (name, online, busy, queued,"
                    " last_seen, connected_at, version, note)"
                    " VALUES (?, 0, 0, 0, ?, NULL, ?, ?)",
                    (name, now, version, note))
                log.info("worker registered name=%s version=%s",
                         name, version or "-")
            else:
                self._conn.execute(
                    "UPDATE workers SET last_seen=?,"
                    " version=COALESCE(?, version),"
                    " note=COALESCE(?, note) WHERE name=?",
                    (now, version, note, name))
                log.info("worker re-registered name=%s version=%s",
                         name, version or "-")
            self.refresh_counts(name)
        worker = self.get_worker(name)
        assert worker is not None
        return worker, created

    def set_online(self, name: str, version: Optional[str] = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET online=1, last_seen=?, connected_at=?,"
                " version=COALESCE(?, version) WHERE name=?",
                (now, now, version, name))
        log.info("worker online name=%s version=%s", name, version or "-")

    def set_offline(self, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET online=0, last_seen=? WHERE name=?",
                (time.time(), name))
        log.info("worker offline name=%s (name and backlog kept)", name)

    def heartbeat(self, name: str, state: Optional[str] = None,
                  progress_at: Optional[float] = None,
                  task_id: Optional[str] = None) -> bool:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM workers WHERE name=?", (name,)).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE workers SET last_seen=?,"
                " state=COALESCE(?, state),"
                " progress_at=COALESCE(?, progress_at),"
                " task_id=COALESCE(?, task_id) WHERE name=?",
                (now, state, progress_at, task_id, name))
        return True

    def refresh_counts(self, name: str) -> None:
        with self._lock:
            busy = self._conn.execute(
                "SELECT COUNT(*) AS c FROM tasks"
                " WHERE worker=? AND state='running'", (name,)).fetchone()["c"]
            queued = self._conn.execute(
                "SELECT COUNT(*) AS c FROM tasks"
                " WHERE worker=? AND state='queued'", (name,)).fetchone()["c"]
            self._conn.execute(
                "UPDATE workers SET busy=?, queued=? WHERE name=?",
                (int(busy), int(queued), name))

    def running_count(self, name: str) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) AS c FROM tasks"
                " WHERE worker=? AND state='running'", (name,)).fetchone()["c"])

    def queued_count(self, name: str) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) AS c FROM tasks"
                " WHERE worker=? AND state='queued'", (name,)).fetchone()["c"])

    # -- tasks ---------------------------------------------------------- #
    @staticmethod
    def _task_dict(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            result = (json.loads(row["result_json"])
                      if row["result_json"] else None)
        except (ValueError, TypeError):
            result = None
        deadline_at = None
        if row["deadline_s"] is not None and row["created_at"] is not None:
            deadline_at = float(row["created_at"]) + float(row["deadline_s"])
        return {
            "task_id": row["task_id"],
            "worker": row["worker"],
            "goal": row["goal"],
            "context": row["context"],
            "workspace": row["workspace"],
            "urgency": row["urgency"],
            "state": row["state"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "deadline_s": row["deadline_s"],
            "deadline_at": deadline_at,
            "result": result,
        }

    def create_task(self, worker: str, goal: str,
                    context: Optional[str] = None,
                    workspace: Optional[str] = None,
                    urgency: str = "normal",
                    state: str = "queued",
                    deadline_s: Optional[float] = None) -> Dict[str, Any]:
        task_id = "t-" + secrets.token_hex(5)
        now = time.time()
        started = now if state == "running" else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (task_id, worker, goal, context, workspace,"
                " urgency, state, created_at, started_at, finished_at,"
                " result_json, deadline_s)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                (task_id, worker, goal, context, workspace, urgency, state,
                 now, started, deadline_s))
            self.refresh_counts(worker)
        log.info("task created id=%s worker=%s state=%s urgency=%s",
                 task_id, worker, state, urgency)
        task = self.get_task(task_id)
        assert task is not None
        return task

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._task_dict(row) if row else None

    def delete_task(self, task_id: str) -> None:
        """Drop a task that was never handed to a worker (dispatch raced)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM tasks WHERE task_id=?", (task_id,))

    def list_tasks(self, worker: Optional[str] = None,
                   state: Optional[str] = None,
                   limit: int = 100) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        where: List[str] = []
        params: List[Any] = []
        if worker:
            where.append("worker=?")
            params.append(worker)
        if state:
            where.append("state=?")
            params.append(state)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._task_dict(row) for row in rows]

    def claim_next_queued(self, worker: str) -> Optional[Dict[str, Any]]:
        """Move this worker's oldest queued task to running; return it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE worker=? AND state='queued'"
                " ORDER BY rowid LIMIT 1", (worker,)).fetchone()
            if row is None:
                return None
            task_id = str(row["task_id"])
            self._conn.execute(
                "UPDATE tasks SET state='running', started_at=?"
                " WHERE task_id=? AND state='queued'",
                (time.time(), task_id))
            self.refresh_counts(worker)
            task = self.get_task(task_id)
        if task is not None:
            log.info("task dispatched id=%s worker=%s", task_id, worker)
        return task

    def requeue_task(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET state='queued', started_at=NULL"
                " WHERE task_id=? AND state='running'", (task_id,))

    def requeue_running(self, worker: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET state='queued', started_at=NULL"
                " WHERE worker=? AND state='running'", (worker,))
            changed = int(cur.rowcount or 0)
            self.refresh_counts(worker)
        if changed:
            log.info("worker=%s: %d running task(s) requeued", worker, changed)
        return changed

    def finish_task(self, task_id: str, status: str,
                    result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET state=?, finished_at=?, result_json=?"
                " WHERE task_id=?",
                (status, time.time(),
                 json.dumps(result, ensure_ascii=False), task_id))
        return self.get_task(task_id)

    def cancel_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        result = {"status": "cancelled", "summary": "cancelled via API",
                  "cancelled_at": time.time()}
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET state='cancelled', finished_at=?,"
                " result_json=? WHERE task_id=?",
                (time.time(), json.dumps(result, ensure_ascii=False),
                 task_id))
        return self.get_task(task_id)

    def counts(self) -> Dict[str, int]:
        counts = {"total": 0, "queued": 0, "running": 0, "done": 0,
                  "failed": 0, "cancelled": 0}
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS c FROM tasks GROUP BY state"
            ).fetchall()
        for row in rows:
            state = str(row["state"])
            counts[state] = int(row["c"])
            counts["total"] += int(row["c"])
        return counts

    # -- subscriptions (table only; forwarding is stubbed in 0.1.0) ------ #
    @staticmethod
    def _subscription_dict(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            kinds = json.loads(row["kinds"]) if row["kinds"] else []
        except (ValueError, TypeError):
            kinds = []
        return {
            "id": int(row["id"]),
            "worker": row["worker"],
            "source": row["source"],
            "kinds": kinds if isinstance(kinds, list) else [],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
        }

    def add_subscription(self, worker: str, source: str,
                         kinds: List[str]) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO subscriptions (worker, source, kinds, active,"
                " created_at) VALUES (?, ?, ?, 1, ?)",
                (worker, source,
                 json.dumps(kinds, ensure_ascii=False), now))
            sub_id = int(cur.lastrowid)
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        log.info("subscription added id=%d worker=%s source=%s kinds=%s",
                 sub_id, worker, source, kinds or "all")
        return self._subscription_dict(row)

    def get_subscription(self, sub_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE id=?", (int(sub_id),)
            ).fetchone()
        return self._subscription_dict(row) if row else None

    def deactivate_subscription(self, sub_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE id=?", (int(sub_id),)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE subscriptions SET active=0 WHERE id=?", (int(sub_id),))
        log.info("subscription deactivated id=%d", int(sub_id))
        return self.get_subscription(sub_id)

    def list_subscriptions(self, worker: Optional[str] = None,
                           active_only: bool = False
                           ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM subscriptions"
        where: List[str] = []
        params: List[Any] = []
        if worker:
            where.append("worker=?")
            params.append(worker)
        if active_only:
            where.append("active=1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._subscription_dict(row) for row in rows]

    def active_subscriptions(self, worker: str) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) AS c FROM subscriptions"
                " WHERE worker=? AND active=1", (worker,)).fetchone()["c"])

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# trigger side (result notify is real; subscription forwarding is a stub)
# --------------------------------------------------------------------------- #

class TriggerNotifier:
    """Writes exactly one ``task.result`` notify per finished task."""

    def __init__(self, url: str, token_path: str):
        self.url = url
        self.token_path = Path(os.path.expanduser(token_path))

    def _token(self) -> str:
        try:
            return self.token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def notify_result(self, task: Dict[str, Any], status: str,
                      summary: str) -> bool:
        token = self._token()
        if not token:
            log.warning("trigger notify skipped: no token at %s",
                        self.token_path)
            return False
        label = "完成" if status == "done" else "失败"
        one_line = " ".join(str(summary or "").split())
        if len(one_line) > 300:
            one_line = one_line[:297] + "..."
        text = "%s %s：%s" % (task["task_id"], label, one_line or "(无摘要)")
        payload = {
            "source": "router",
            "kind": "task.result",
            "level": "notify",
            "text": text,
            "actor": task["worker"],
            "event_id": "%s:%s" % (task["task_id"], status),
            "payload": {
                "task_id": task["task_id"],
                "worker": task["worker"],
                "status": status,
            },
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("X-Trigger-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                code = int(resp.status)
                resp.read(4096)
        except Exception as exc:  # trigger down must never break /result
            log.warning("trigger notify failed task=%s status=%s: %s",
                        task["task_id"], status, exc)
            return False
        log.info("trigger notify task=%s status=%s -> HTTP %d",
                 task["task_id"], status, code)
        return code == 200


class TriggerSubscriptionBridge:
    """0.1.0 placeholder for ROUTER.md §四 trigger-side forwarding.

    The subscription rows are the source of truth here; pushing them into
    trigger (and receiving matched messages from trigger) is the next step.
    """

    enabled = False

    def register(self, worker: str, source: str, kinds: List[str]) -> bool:
        log.info("subscription stub: would register worker=%s source=%s "
                 "kinds=%s at trigger", worker, source, kinds or "all")
        return False

    def unregister(self, sub: Dict[str, Any]) -> bool:
        log.info("subscription stub: would deactivate id=%s worker=%s at "
                 "trigger", sub.get("id"), sub.get("worker"))
        return False

    def worker_offline(self, worker: str, active: int) -> None:
        if active:
            log.info("subscription stub: worker=%s offline, would drop %d "
                     "trigger subscription(s)", worker, active)


# --------------------------------------------------------------------------- #
# SSE connection registry
# --------------------------------------------------------------------------- #

class Connection:
    """One live SSE stream. Tasks are queued here for its writer thread."""

    def __init__(self, name: str, version: Optional[str] = None):
        self.name = name
        self.version = version
        self.queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.closed = False
        self.reason = ""
        self.connected_at = time.time()

    def send_task(self, task: Dict[str, Any]) -> bool:
        if self.closed:
            return False
        payload = dict(task)
        payload["type"] = "task"
        self.queue.put({"event": "task", "data": payload})
        return True

    def close(self, reason: str = "closed") -> None:
        if self.closed:
            return
        self.closed = True
        self.reason = reason
        self.queue.put(None)


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class RouterServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, addr, store: RouterStore, token: str,
                 notifier: TriggerNotifier, bridge: TriggerSubscriptionBridge,
                 ping_interval: float = PING_INTERVAL,
                 backlog_max: int = BACKLOG_MAX):
        self.store = store
        self.started_at = time.time()
        self.token = token or ""
        self.notifier = notifier
        self.trigger_bridge = bridge
        self.ping_interval = max(0.2, float(ping_interval))
        self.backlog_max = max(1, int(backlog_max))
        self.conns: Dict[str, Connection] = {}
        self.conn_lock = threading.Lock()
        self.dispatch_lock = threading.RLock()
        super().__init__(addr, RouterHandler)

    # -- connections ---------------------------------------------------- #
    def current_conn(self, name: str) -> Optional[Connection]:
        with self.conn_lock:
            return self.conns.get(name)

    def online(self, name: str) -> bool:
        return self.current_conn(name) is not None

    def attach(self, name: str, version: Optional[str] = None) -> Connection:
        conn = Connection(name, version)
        with self.conn_lock:
            old = self.conns.get(name)
            self.conns[name] = conn
            if old is not None:
                old.close("replaced")
        with self.dispatch_lock:
            self.store.set_online(name, version)
            # A reconnect is at-least-once: anything still marked running was
            # sent over the previous stream, so hand it out again.
            self.store.requeue_running(name)
            self.flush_backlog_locked(name)
        log.info("stream attached name=%s version=%s (queued=%d)",
                 name, version or "-", self.store.queued_count(name))
        return conn

    def detach(self, conn: Connection, reason: str = "closed") -> None:
        with self.conn_lock:
            current = self.conns.get(conn.name) is conn
            if current:
                self.conns.pop(conn.name, None)
        with self.dispatch_lock:
            if current:
                self.store.set_offline(conn.name)
                self.store.requeue_running(conn.name)
                active_subs = self.store.active_subscriptions(conn.name)
                self.trigger_bridge.worker_offline(conn.name, active_subs)
            self.store.refresh_counts(conn.name)
        log.info("stream detached name=%s reason=%s current=%s queued=%d",
                 conn.name, reason, current, self.store.queued_count(conn.name))

    # -- dispatch ------------------------------------------------------- #
    def push(self, conn: Connection, task: Dict[str, Any]) -> bool:
        return conn.send_task(task)

    def dispatch_one(self, worker: str) -> Optional[Dict[str, Any]]:
        """Push the next queued task when the worker has nothing running."""
        with self.dispatch_lock:
            conn = self.current_conn(worker)
            if conn is None or conn.closed:
                return None
            if self.store.running_count(worker) > 0:
                return None
            task = self.store.claim_next_queued(worker)
            if task is None:
                return None
            if not self.push(conn, task):
                self.store.requeue_task(task["task_id"])
                self.store.refresh_counts(worker)
                return None
            return task

    def flush_backlog_locked(self, worker: str) -> int:
        """Send every queued task, in creation order (caller holds lock)."""
        conn = self.current_conn(worker)
        if conn is None or conn.closed:
            return 0
        sent = 0
        while True:
            task = self.store.claim_next_queued(worker)
            if task is None:
                break
            if not self.push(conn, task):
                self.store.requeue_task(task["task_id"])
                break
            sent += 1
        self.store.refresh_counts(worker)
        if sent:
            log.info("backlog flushed name=%s tasks=%d", worker, sent)
        return sent


class RouterHandler(BaseHTTPRequestHandler):
    server_version = "mowu-router/" + VERSION
    sys_version = ""

    # -- plumbing ------------------------------------------------------- #
    def log_message(self, fmt, *args):  # noqa: D401 - stdlib signature
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("invalid Content-Length")
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise BadRequest("body too large")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest("invalid JSON: %s" % exc)
        if not isinstance(data, dict):
            raise BadRequest("JSON body must be an object")
        return data

    def _require_token(self) -> None:
        expected = getattr(self.server, "token", "") or ""
        if not expected:
            return
        given = self.headers.get("X-Router-Token") or ""
        try:
            ok = hmac.compare_digest(given.encode("latin-1", "ignore"),
                                     expected.encode("utf-8"))
        except Exception:
            ok = False
        if not ok:
            raise Unauthorized()

    @staticmethod
    def _name_param(qs: Dict[str, List[str]]) -> str:
        name = (qs.get("name", [""])[0] or "").strip()
        if not name:
            raise BadRequest("name is required")
        if len(name) > 128:
            raise BadRequest("name too long")
        return name

    def _int_param(self, qs: Dict[str, List[str]], name: str,
                   default: Optional[int] = None) -> Optional[int]:
        raw = qs.get(name, [None])[0]
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise BadRequest("parameter %r must be an integer" % name)

    @staticmethod
    def _path_parts(path: str) -> List[str]:
        return [part for part in path.split("/") if part]

    # -- routes --------------------------------------------------------- #
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        parts = self._path_parts(parsed.path)
        try:
            if parts == ["health"]:
                return self._send_json(200, self._health())
            if not parts:
                return self._send_json(200, {
                    "ok": True, "service": "mowu-router", "version": VERSION,
                    "endpoints": ["/health", "/workers", "/tasks", "/stream",
                                  "/register", "/heartbeat", "/result",
                                  "/subscriptions"],
                })
            if parts == ["stream"]:
                return self._serve_stream(qs)
            self._require_token()
            if parts == ["workers"]:
                return self._send_json(200, self._workers())
            if parts == ["tasks"]:
                return self._send_json(200, self._tasks_list(qs))
            if len(parts) == 2 and parts[0] == "tasks":
                return self._task_get(parts[1])
            if parts == ["subscriptions"]:
                return self._send_json(200, self._subscriptions_list(qs))
            self._send_json(404, {"ok": False, "error": "not found"})
        except Unauthorized:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
        except BadRequest as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except NotFound:
            self._send_json(404, {"ok": False, "error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("GET %s failed", self.path)
            self._send_json(500, {"ok": False, "error": "%s: %s"
                                  % (type(exc).__name__, exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = self._path_parts(parsed.path)
        try:
            self._require_token()
            body = self._read_json()
            if parts == ["register"]:
                return self._send_json(200, self._register(body))
            if parts == ["heartbeat"]:
                return self._send_json(*self._heartbeat(body))
            if parts == ["result"]:
                return self._send_json(*self._result(body))
            if parts == ["tasks"]:
                return self._send_json(*self._task_create(body))
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
                return self._send_json(*self._task_cancel(parts[1]))
            if parts == ["subscriptions"]:
                return self._send_json(*self._subscription_create(body))
            self._send_json(404, {"ok": False, "error": "not found"})
        except Unauthorized:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
        except BadRequest as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except NotFound:
            self._send_json(404, {"ok": False, "error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s failed", self.path)
            self._send_json(500, {"ok": False, "error": "%s: %s"
                                  % (type(exc).__name__, exc)})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        parts = self._path_parts(parsed.path)
        try:
            self._require_token()
            if len(parts) == 2 and parts[0] == "subscriptions":
                return self._send_json(*self._subscription_delete(parts[1]))
            self._send_json(404, {"ok": False, "error": "not found"})
        except Unauthorized:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
        except BadRequest as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except NotFound:
            self._send_json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001
            log.exception("DELETE %s failed", self.path)
            self._send_json(500, {"ok": False, "error": "%s: %s"
                                  % (type(exc).__name__, exc)})

    # -- GET endpoints -------------------------------------------------- #
    def _health(self) -> Dict[str, Any]:
        counts = self.server.store.counts()
        workers = self.server.store.list_workers()
        with self.server.conn_lock:
            live = set(self.server.conns)
        online = [w for w in workers if w["name"] in live]
        return {
            "ok": True,
            "version": VERSION,
            "workers": len(workers),
            "online": len(online),
            "tasks": counts["total"],
            "tasks_by_state": counts,
            "subscriptions": len(
                self.server.store.list_subscriptions(active_only=True)),
            "port": self.server.server_address[1],
            "db_path": self.server.store.path,
            "backlog_max": self.server.backlog_max,
            "uptime_s": round(max(0.0, time.time() - self.server.started_at), 1),
        }

    def _workers(self) -> Dict[str, Any]:
        live = set()
        with self.server.conn_lock:
            live = set(self.server.conns)
        workers = self.server.store.list_workers()
        for worker in workers:
            worker["online"] = worker["name"] in live
        return {"ok": True, "count": len(workers), "workers": workers}

    def _tasks_list(self, qs: Dict[str, List[str]]) -> Dict[str, Any]:
        worker = (qs.get("worker", [""])[0] or "").strip() or None
        state = (qs.get("state", [""])[0] or "").strip() or None
        if state is not None and state not in TASK_STATES:
            raise BadRequest("state must be one of %s" % ", ".join(TASK_STATES))
        limit = self._int_param(qs, "limit", 100)
        if limit is None:
            limit = 100
        if limit < 1:
            raise BadRequest("limit must be >= 1")
        tasks = self.server.store.list_tasks(worker, state, limit)
        return {"ok": True, "count": len(tasks), "tasks": tasks}

    def _task_get(self, task_id: str) -> None:
        task = self.server.store.get_task(task_id)
        if task is None:
            return self._send_json(404, {"ok": False, "error": "unknown_task",
                                         "task_id": task_id})
        self._send_json(200, {"ok": True, "task_id": task_id,
                              "state": task["state"], "task": task})

    def _subscriptions_list(self, qs: Dict[str, List[str]]) -> Dict[str, Any]:
        worker = (qs.get("worker", [""])[0] or "").strip() or None
        active_only = (qs.get("active", [""])[0] or "").strip() in (
            "1", "true", "yes")
        subs = self.server.store.list_subscriptions(worker, active_only)
        return {"ok": True, "count": len(subs), "subscriptions": subs}

    # -- POST endpoints -------------------------------------------------- #
    def _register(self, body: Dict[str, Any]) -> Dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise BadRequest("name is required")
        if len(name) > 128:
            raise BadRequest("name too long")
        version = body.get("version")
        version = str(version).strip() if version is not None else None
        note = body.get("note")
        note = str(note).strip() if note is not None else None
        worker, created = self.server.store.upsert_worker(name, version, note)
        return {
            "ok": True,
            "name": name,
            "queued": worker["queued"],
            "online": self.server.online(name),
            "new": created,
            "version": worker["version"],
        }

    def _heartbeat(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise BadRequest("name is required")
        state = body.get("state")
        if state is not None:
            state = str(state).strip() or None
            if state is not None and len(state) > 64:
                raise BadRequest("state too long")
        progress_at = body.get("progress_at")
        if progress_at is not None:
            progress_at = parse_epoch(progress_at, "progress_at")
        task_id = body.get("task_id")
        task_id = str(task_id).strip() if task_id is not None else None
        if not self.server.store.heartbeat(name, state, progress_at, task_id):
            return 404, {"ok": False, "error": "unknown_worker", "worker": name}
        worker = self.server.store.get_worker(name)
        assert worker is not None
        return 200, {
            "ok": True, "name": name,
            "online": self.server.online(name),
            "busy": worker["busy"],
            "queued": worker["queued"],
            "last_seen": worker["last_seen"],
        }

    def _result(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        name = str(body.get("name") or "").strip()
        task_id = str(body.get("task_id") or "").strip()
        if not name:
            raise BadRequest("name is required")
        if not task_id:
            raise BadRequest("task_id is required")
        status = str(body.get("status") or "").strip().lower()
        if status not in ("done", "failed"):
            raise BadRequest("status must be done or failed")
        summary = str(body.get("summary") or "")
        artifacts = body.get("artifacts") or []
        evidence = body.get("evidence") or []
        if not isinstance(artifacts, list):
            raise BadRequest("artifacts must be a list")
        if not isinstance(evidence, list):
            raise BadRequest("evidence must be a list")

        with self.server.dispatch_lock:
            task = self.server.store.get_task(task_id)
            if task is None:
                return 404, {"ok": False, "error": "unknown_task",
                             "task_id": task_id}
            if task["worker"] != name:
                return 400, {"ok": False, "error": "worker_mismatch",
                             "task_id": task_id, "worker": task["worker"],
                             "reported_by": name}
            if task["state"] in TERMINAL_STATES:
                log.info("result ignored (already %s) task=%s worker=%s",
                         task["state"], task_id, name)
                return 200, {"ok": True, "task_id": task_id,
                             "state": task["state"], "duplicate": True,
                             "notified": False, "dispatched_next": None}
            result = {
                "status": status,
                "summary": summary,
                "artifacts": artifacts,
                "evidence": evidence,
                "reported_at": time.time(),
            }
            task = self.server.store.finish_task(task_id, status, result)
            assert task is not None
            self.server.store.refresh_counts(name)
            dispatched = self.server.dispatch_one(name)

        notified = self.server.notifier.notify_result(task, status, summary)
        log.info("task finished id=%s worker=%s status=%s notified=%s",
                 task_id, name, status, notified)
        return 200, {
            "ok": True,
            "task_id": task_id,
            "state": status,
            "notified": notified,
            "dispatched_next": dispatched["task_id"] if dispatched else None,
        }

    def _task_create(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        worker_name = str(body.get("worker") or "").strip()
        if not worker_name:
            raise BadRequest("worker is required")
        goal = body.get("goal")
        if goal is None or not str(goal).strip():
            raise BadRequest("goal is required")
        goal = str(goal)
        if len(goal) > 20000:
            raise BadRequest("goal too long (max 20000 chars)")
        context = body.get("context")
        if context is not None and not isinstance(context, str):
            context = json.dumps(context, ensure_ascii=False)
        workspace = body.get("workspace")
        workspace = str(workspace) if workspace is not None else None
        urgency = str(body.get("urgency") or "normal").strip().lower()
        if urgency not in URGENCIES:
            raise BadRequest("urgency must be urgent or normal")
        queue_if_offline = body.get("queue_if_offline", False)
        if not isinstance(queue_if_offline, bool):
            raise BadRequest("queue_if_offline must be a boolean")
        deadline_s = body.get("deadline_s")
        if deadline_s is not None:
            try:
                deadline_s = float(deadline_s)
            except (TypeError, ValueError):
                raise BadRequest("deadline_s must be a number")
            if deadline_s <= 0:
                raise BadRequest("deadline_s must be > 0")

        store = self.server.store
        if store.get_worker(worker_name) is None:
            return 400, {"ok": False, "error": "unknown_worker",
                         "worker": worker_name}

        with self.server.dispatch_lock:
            online = self.server.online(worker_name)
            if online and store.running_count(worker_name) == 0:
                task = store.create_task(
                    worker_name, goal, context, workspace, urgency,
                    state="running", deadline_s=deadline_s)
                self.server.store.refresh_counts(worker_name)
                conn = self.server.current_conn(worker_name)
                if conn is not None and self.server.push(conn, task):
                    return 200, {
                        "ok": True, "task_id": task["task_id"],
                        "worker": worker_name, "state": "running",
                        "dispatched": True, "queued": False,
                        "queued_ahead": 0,
                    }
                # Stream vanished between the check and the push.
                store.requeue_task(task["task_id"])
                store.refresh_counts(worker_name)
                online = self.server.online(worker_name)
                if not online and not queue_if_offline:
                    store.delete_task(task["task_id"])
                    store.refresh_counts(worker_name)
                    return 409, {
                        "ok": False, "error": "worker_offline",
                        "worker": worker_name, "online": False,
                        "queue_if_offline": False,
                        "message": "worker is offline; retry with "
                                   "queue_if_offline=true to queue it",
                    }
                # The task is already queued; fall through to the queued path
                # without creating a second one by reusing this row.
                task = store.get_task(task["task_id"])
                assert task is not None
                ahead = store.queued_count(worker_name) - 1
                if ahead < 0:
                    ahead = 0
                if online:
                    self.server.dispatch_one(worker_name)
                    task = store.get_task(task["task_id"]) or task
                store.refresh_counts(worker_name)
                status = 200 if task["state"] == "running" else 202
                state = task["state"]
                log.info("task queued id=%s worker=%s online=%s ahead=%d",
                         task["task_id"], worker_name, online, ahead)
                return status, {
                    "ok": True, "task_id": task["task_id"],
                    "worker": worker_name, "state": state,
                    "dispatched": state == "running",
                    "queued": state == "queued", "queued_ahead": ahead,
                }

            if not online and not queue_if_offline:
                log.info("refused task for offline worker=%s", worker_name)
                return 409, {
                    "ok": False, "error": "worker_offline",
                    "worker": worker_name, "online": False,
                    "queue_if_offline": False,
                    "message": "worker is offline; retry with "
                               "queue_if_offline=true to queue it",
                }

            queued = store.queued_count(worker_name)
            if queued >= self.server.backlog_max:
                log.warning("backlog full worker=%s queued=%d limit=%d",
                            worker_name, queued, self.server.backlog_max)
                return 429, {
                    "ok": False, "error": "backlog_full",
                    "worker": worker_name, "queued": queued,
                    "limit": self.server.backlog_max,
                    "message": "worker backlog is full (%d); wait for it to "
                               "drain or pick another worker"
                               % self.server.backlog_max,
                }
            ahead = queued + store.running_count(worker_name)
            task = store.create_task(
                worker_name, goal, context, workspace, urgency,
                state="queued", deadline_s=deadline_s)
            store.refresh_counts(worker_name)
            status = 202
            log.info("task queued id=%s worker=%s online=%s ahead=%d",
                     task["task_id"], worker_name, online, ahead)
            return status, {
                "ok": True, "task_id": task["task_id"],
                "worker": worker_name, "state": "queued",
                "dispatched": False, "queued": True,
                "queued_ahead": ahead,
            }

    def _task_cancel(self, task_id: str) -> Tuple[int, Dict[str, Any]]:
        with self.server.dispatch_lock:
            task = self.server.store.get_task(task_id)
            if task is None:
                return 404, {"ok": False, "error": "unknown_task",
                             "task_id": task_id}
            if task["state"] == "cancelled":
                return 200, {"ok": True, "task_id": task_id,
                             "state": "cancelled", "already": True,
                             "dispatched_next": None}
            if task["state"] in TERMINAL_STATES:
                return 409, {"ok": False, "error": "not_cancellable",
                             "task_id": task_id, "state": task["state"]}
            task = self.server.store.cancel_task(task_id)
            assert task is not None
            self.server.store.refresh_counts(task["worker"])
            dispatched = self.server.dispatch_one(task["worker"])
        log.info("task cancelled id=%s worker=%s", task_id, task["worker"])
        return 200, {"ok": True, "task_id": task_id, "state": "cancelled",
                     "already": False,
                     "dispatched_next": dispatched["task_id"]
                     if dispatched else None}

    def _subscription_create(self, body: Dict[str, Any]
                             ) -> Tuple[int, Dict[str, Any]]:
        worker = str(body.get("worker") or "").strip()
        source = str(body.get("source") or "").strip()
        if not worker:
            raise BadRequest("worker is required")
        if not source:
            raise BadRequest("source is required")
        if self.server.store.get_worker(worker) is None:
            return 400, {"ok": False, "error": "unknown_worker",
                         "worker": worker}
        raw_kinds = body.get("kinds") or []
        if isinstance(raw_kinds, str):
            kinds = [part.strip() for part in raw_kinds.split(",")
                     if part.strip()]
        elif isinstance(raw_kinds, list):
            kinds = [str(part).strip() for part in raw_kinds if str(part).strip()]
        else:
            raise BadRequest("kinds must be a list of strings")
        sub = self.server.store.add_subscription(worker, source, kinds)
        forwarded = self.server.trigger_bridge.register(worker, source, kinds)
        return 201, {
            "ok": True, "id": sub["id"], "worker": worker, "source": source,
            "kinds": kinds, "active": True,
            "trigger_forwarded": forwarded,
            "note": "subscription stored; trigger-side forwarding is a stub "
                    "in 0.1.0 (see server/router/README.md)",
        }

    def _subscription_delete(self, raw_id: str) -> Tuple[int, Dict[str, Any]]:
        try:
            sub_id = int(raw_id)
        except ValueError:
            raise BadRequest("subscription id must be an integer")
        sub = self.server.store.get_subscription(sub_id)
        if sub is None:
            return 404, {"ok": False, "error": "unknown_subscription",
                         "id": sub_id}
        self.server.trigger_bridge.unregister(sub)
        sub = self.server.store.deactivate_subscription(sub_id)
        return 200, {"ok": True, "id": sub_id,
                     "active": bool(sub["active"]) if sub else False}

    # -- SSE ------------------------------------------------------------ #
    def _write(self, chunk: bytes) -> None:
        self.wfile.write(chunk)
        self.wfile.flush()

    def _peer_gone(self) -> bool:
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _serve_stream(self, qs: Dict[str, List[str]]) -> None:
        try:
            self._require_token()
            name = self._name_param(qs)
            if self.server.store.get_worker(name) is None:
                self._send_json(404, {"ok": False, "error": "unknown_worker",
                                      "worker": name})
                log.info("stream refused: unknown worker %r", name)
                return
            version = (qs.get("version", [""])[0] or "").strip() or None
        except Unauthorized:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        except BadRequest as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        conn = self.server.attach(name, version)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            self.connection.settimeout(10.0)
        except OSError:
            pass

        reason = "client_closed"
        last_ping = time.monotonic()
        try:
            self._write(b": connected\n\n")
            # attach() already flushed the backlog into this stream's queue.
            while True:
                if conn.closed:
                    reason = conn.reason or "closed"
                    break
                if self._peer_gone():
                    reason = "client_eof"
                    break
                try:
                    item = conn.queue.get(timeout=0.5)
                except queue.Empty:
                    if time.monotonic() - last_ping >= self.server.ping_interval:
                        self._write(b": ping\n\n")
                        last_ping = time.monotonic()
                    continue
                if item is None:
                    reason = conn.reason or "closed"
                    break
                frame = ("event: %s\ndata: %s\n\n" % (
                    item["event"],
                    json.dumps(item["data"], ensure_ascii=False))).encode("utf-8")
                self._write(frame)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            reason = "write_failures"
        finally:
            self.server.detach(conn, reason)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def load_or_create_token(path: Path) -> Tuple[str, bool]:
    """Read the router token; generate a 32-byte hex one if absent."""
    try:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                try:
                    os.chmod(str(path), 0o600)
                except OSError:
                    pass
                return token, False
    except OSError as exc:
        log.warning("cannot read token file %s: %s", path, exc)

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)  # 32 bytes -> 64 hex chars
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(str(tmp), 0o600)
    except OSError:
        pass
    os.replace(str(tmp), str(path))
    return token, True


def acquire_lock(path: Path) -> Optional[int]:
    """Exclusive non-blocking flock; returns the fd or None if held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, ("%d\n" % os.getpid()).encode("utf-8"))
    os.fsync(fd)
    return fd


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="mowu-router: worker / task / subscription registry "
                    "for Hermes. Loopback only, stdlib only.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="listen port on 127.0.0.1 (default %(default)s)")
    p.add_argument("--host", default=HOST,
                   help="loopback address to bind (default %(default)s)")
    p.add_argument("--db", default=DEFAULT_DB,
                   help="SQLite path (default %(default)s)")
    p.add_argument("--token-file", default=DEFAULT_TOKEN_PATH,
                   help="router token file (default %(default)s)")
    p.add_argument("--lock", default=DEFAULT_LOCK_PATH,
                   help="single-instance lock file (default %(default)s)")
    p.add_argument("--log", default=DEFAULT_LOG,
                   help="log file, mirrored to stderr (default %(default)s)")
    p.add_argument("--trigger-url", default=DEFAULT_TRIGGER_URL,
                   help="trigger ingest URL (default %(default)s)")
    p.add_argument("--trigger-token-file", default=DEFAULT_TRIGGER_TOKEN_PATH,
                   help="trigger token file (default %(default)s)")
    p.add_argument("--backlog-max", type=int, default=BACKLOG_MAX,
                   help="max queued/backlog tasks per worker "
                        "(default %(default)s)")
    p.add_argument("--ping-interval", type=float, default=PING_INTERVAL,
                   help="SSE comment heartbeat seconds (default %(default)s)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=VERSION)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(Path(os.path.expanduser(args.log)), args.verbose)

    host = (args.host or HOST).strip()
    if host not in ("127.0.0.1", "localhost"):
        print("error: mowu-router is loopback-only; refusing to bind %r"
              % host, file=sys.stderr)
        return 2
    if args.backlog_max < 1:
        print("error: --backlog-max must be >= 1", file=sys.stderr)
        return 2
    if args.ping_interval <= 0:
        print("error: --ping-interval must be > 0", file=sys.stderr)
        return 2

    lock_fd = acquire_lock(Path(os.path.expanduser(args.lock)))
    if lock_fd is None:
        print("error: another mowu-router already holds %s"
              % os.path.expanduser(args.lock), file=sys.stderr)
        log.error("lock held: %s", os.path.expanduser(args.lock))
        return 1

    token_path = Path(os.path.expanduser(args.token_file))
    token, generated = load_or_create_token(token_path)

    store = RouterStore(args.db)
    store.reset_runtime()
    notifier = TriggerNotifier(args.trigger_url, args.trigger_token_file)
    bridge = TriggerSubscriptionBridge()
    try:
        server = RouterServer((host, args.port), store, token, notifier,
                              bridge, ping_interval=args.ping_interval,
                              backlog_max=args.backlog_max)
    except OSError as exc:
        print("error: cannot bind %s:%d: %s" % (host, args.port, exc),
              file=sys.stderr)
        log.error("cannot bind %s:%d: %s", host, args.port, exc)
        store.close()
        os.close(lock_fd)
        return 1

    log.info("mowu-router %s listening on http://%s:%d (db=%s, "
             "backlog_max=%d, ping=%.1fs)", VERSION, host, args.port,
             store.path, args.backlog_max, args.ping_interval)
    log.info("router token: %s%s; every endpoint except /health needs "
             "X-Router-Token", token_path,
             " (generated)" if generated else "")
    log.info("result notify -> %s (token %s)", args.trigger_url,
             args.trigger_token_file)
    log.info("subscription forwarding to trigger is a stub in %s", VERSION)
    print("mowu-router %s listening on http://%s:%d  [X-Router-Token from %s]"
          % (VERSION, host, args.port, token_path), flush=True)

    def _shutdown(signum, _frame):
        log.info("signal %s received; shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
    finally:
        server.server_close()
        store.close()
        try:
            os.close(lock_fd)
        except OSError:
            pass
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
