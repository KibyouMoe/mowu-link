#!/usr/bin/env python3
"""mowu-router — the worker / task / subscription registry for Hermes.

Implements docs/ROUTER.md ① (workers) ② (tasks + result + notify) ③
(subscriptions with real trigger forwarding) and ⑨ (chat eavesdropping:
store-only, never forwarded) at 127.0.0.1:8790.

The router is deliberately dumb:

  * it knows *who is connected*, *who is busy* and *how many tasks each
    worker has queued*;
  * it knows nothing about capabilities, task meaning or worker internals;
  * it forwards trigger events (task results and ``/notice``), trigger
    subscription lifecycle calls, and durable worker-to-worker messages.

Liveness is the SSE connection itself, never a heartbeat: ``POST /heartbeat``
only records the worker's own state/progress.  When a stream ends the worker
goes offline, its name and backlog stay, and its in-flight task goes back to
``queued`` so the next connection receives it again (at-least-once; workers
dedupe by ``task_id``).

Standard library only.  Python 3.9.  Binds loopback unless explicitly opted in.
"""

from __future__ import annotations

from attachments import AttachmentStore, MAX_ATTACHMENTS

import argparse
import fcntl
import fnmatch
import hashlib
import hmac
import json
import logging
import os
import queue
import secrets
import select
import signal
import socket
import subprocess
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urlparse

VERSION = "0.4.0"
HOST = "127.0.0.1"
DEFAULT_PORT = 8790
DEFAULT_DB = "~/.hermes/router.db"
DEFAULT_TOKEN_PATH = "~/.hermes/secrets/router.token"
DEFAULT_LOCK_PATH = "~/.hermes/router.lock"
DEFAULT_LOG = "~/.hermes/router.log"
DEFAULT_TRIGGER_URL = "http://127.0.0.1:8791/trigger"
DEFAULT_TRIGGER_TOKEN_PATH = "~/.hermes/secrets/trigger.token"
BACKLOG_MAX = 128
MAX_GOAL_CHARS = 2000
MAX_BODY_BYTES = 1 * 1024 * 1024
PEER_MAX_TEXT = 20000
PEER_PAGE_LIMIT = 100
PEER_PAGE_BYTES = 256 * 1024
PEER_STREAM_BUFFER = 32
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

TASK_STATES = ("queued", "running", "done", "failed", "cancelled")
TERMINAL_STATES = ("done", "failed", "cancelled")
URGENCIES = ("urgent", "normal")
CHAT_DIRECTIONS = ("in", "out")
PING_INTERVAL = 15.0

# Approval channel (worker → Hermes): a worker asks, a human decides, the
# worker polls.  Nothing here executes anything; the router only stores the
# question and the verdict.
APPROVAL_STATUSES = ("pending", "approved", "denied", "expired")
APPROVAL_DECISIONS = ("approved", "denied")
APPROVAL_TTL_S = 600.0          # pending older than 10 min is expired
APPROVAL_SWEEP_S = 30.0
APPROVAL_MAX_TITLE = 300
APPROVAL_MAX_DETAIL = 4000
APPROVAL_MAX_OPTIONS = 8
APPROVAL_MAX_TTL_S = 600.0

# Router-side auto-approval. Rules live in a JSON file so the policy is one
# place for every worker (Codex / DSH / future ones) instead of each worker
# hard-coding its own shortcuts.
APPROVAL_POLICY_FILE = os.environ.get(
    "ROUTER_APPROVAL_POLICY",
    os.path.expanduser("~/.hermes/router-approval-policy.json"))


def load_approval_policy() -> List[Dict[str, Any]]:
    """Read autoApprove rules. Missing/corrupt file means 'no rules'."""
    try:
        with open(APPROVAL_POLICY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("approval policy unreadable: %s", exc)
        return []
    rules = data.get("autoApprove") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict)]


def _policy_rule_matches(rule: Dict[str, Any], worker: str, kind: str,
                         title: str) -> bool:
    want_worker = rule.get("worker")
    if want_worker and str(want_worker) != "*":
        if not fnmatch.fnmatchcase(str(worker), str(want_worker)):
            return False
    want_kind = rule.get("kind")
    if want_kind and str(want_kind) != "*":
        if str(kind) != str(want_kind):
            return False
    pattern = rule.get("titlePattern")
    if pattern:
        try:
            if not re.search(str(pattern), str(title or "")):
                return False
        except re.error:
            return False
    return True


def match_approval_policy(worker: str, kind: str,
                          title: str) -> Optional[Dict[str, Any]]:
    for rule in load_approval_policy():
        if _policy_rule_matches(rule, worker, kind, title):
            return rule
    return None


# -- host ops ------------------------------------------------------------- #
# Router 是独立 launchd 服务，不是网关的子进程，所以它能安全地去操作别的服务。
# 只认白名单，名字与 label 一对一，不接受任意 label 拼接。
OPS_SERVICES = {
    "gateway": "ai.hermes.gateway",
    "router": "ai.hermes.router",
    "trigger": "ai.hermes.trigger",
    "codexworker": "ai.hermes.codexworker",
    "dsh-web": "ai.hermes.dsh-web",
}


def ops_pid(label: str) -> Optional[int]:
    """从 launchctl list 里取某个 label 的 pid；没跑就是 None。"""
    try:
        out = subprocess.run(["/bin/launchctl", "list"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 3 and cols[2].strip() == label:
            try:
                return int(cols[0])
            except ValueError:
                return None
    return None


def ops_kickstart(label: str) -> Tuple[bool, str]:
    """launchctl kickstart -k：先停后起，返回 (成功, 错误文本)。"""
    target = "gui/%d/%s" % (os.getuid(), label)
    try:
        proc = subprocess.run(["/bin/launchctl", "kickstart", "-k", target],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:400]
    return True, ""


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
    deadline_s  REAL,
    request_id  TEXT,
    request_fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_worker_state ON tasks(worker, state);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    worker     TEXT NOT NULL,
    source     TEXT NOT NULL,
    kinds      TEXT,
    active     INTEGER DEFAULT 1,
    created_at REAL,
    trigger_id TEXT,
    trigger_since INTEGER
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_worker ON subscriptions(worker);

CREATE TABLE IF NOT EXISTS subscription_cursors (
    trigger_name TEXT PRIMARY KEY,
    last_id      INTEGER DEFAULT 0,
    updated_at   REAL
);

-- 聊天旁听 (0.3.0): 只存不发。Router never forwards these rows anywhere;
-- Hermes pulls /chat/{worker}/summary to see what was just said.
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    worker     TEXT NOT NULL,
    direction  TEXT NOT NULL,
    text       TEXT,
    session_id TEXT,
    ts         REAL,
    message_id TEXT,
    meta       TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_worker_id ON chat_messages(worker, id);
CREATE INDEX IF NOT EXISTS idx_chat_worker_ts ON chat_messages(worker, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_dedupe
    ON chat_messages(worker, message_id, direction)
    WHERE message_id IS NOT NULL AND message_id <> '';

-- Peer chat is independent of tasks and ephemeral trigger subscriptions.
-- Sending over SSE never consumes a message; only its recipient's ACK does.
CREATE TABLE IF NOT EXISTS peer_messages (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id       TEXT NOT NULL UNIQUE,
    sender           TEXT NOT NULL,
    recipient        TEXT NOT NULL,
    text             TEXT NOT NULL,
    reply_to         TEXT,
    attachments_json TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    created_at       REAL NOT NULL,
    acked_at         REAL
);
CREATE INDEX IF NOT EXISTS idx_peer_inbox
    ON peer_messages(recipient, seq) WHERE acked_at IS NULL;

-- approval channel: a worker asks a question, a human answers, the worker
-- polls the verdict.  `status` is the single source of truth; expiry is
-- applied both lazily on read and by a sweeper in the server process.
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    worker      TEXT NOT NULL,
    task_id     TEXT,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    detail      TEXT,
    options     TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    decision    TEXT,
    note        TEXT,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    decided_at  REAL,
    notified    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_approvals_worker ON approvals(worker, status);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
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
        for column, decl in (("deadline_s", "REAL"), ("request_id", "TEXT"),
                             ("request_fingerprint", "TEXT")):
            if column not in cols:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN %s %s" % (column, decl))
                log.info("store migration: added tasks.%s", column)
        # Build the index after migration so pre-idempotency databases open.
        # SQLite permits multiple NULL keys, preserving legacy task creation.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_request_id"
            " ON tasks(request_id)")
        wcols = {str(row["name"]) for row in
                 self._conn.execute("PRAGMA table_info(workers)").fetchall()}
        for column, decl in (("state", "TEXT"), ("progress_at", "REAL"),
                             ("task_id", "TEXT"), ("control_url", "TEXT")):
            if column not in wcols:
                self._conn.execute(
                    "ALTER TABLE workers ADD COLUMN %s %s" % (column, decl))
                log.info("store migration: added workers.%s", column)
        scols = {str(row["name"]) for row in
                 self._conn.execute("PRAGMA table_info(subscriptions)").fetchall()}
        for column, decl in (("trigger_id", "TEXT"),
                             ("trigger_since", "INTEGER")):
            if column not in scols:
                self._conn.execute(
                    "ALTER TABLE subscriptions ADD COLUMN %s %s" % (column, decl))
                log.info("store migration: added subscriptions.%s", column)

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
            "last_progress_at": row["progress_at"],
            "last_progress_at_iso": (now_iso(row["progress_at"])
                                     if row["progress_at"] else None),
            "task_id": row["task_id"],
            "control_url": row["control_url"],
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
                      note: Optional[str] = None,
                      control_url: Optional[str] = None) -> Tuple[Dict[str, Any], bool]:
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
            if control_url is not None:
                self._conn.execute("UPDATE workers SET control_url=? WHERE name=?",
                                   (control_url, name))
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

    # -- durable peer chat ---------------------------------------------- #
    @staticmethod
    def _peer_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {"seq": row["seq"], "message_id": row["message_id"],
                "from": row["sender"], "to": row["recipient"],
                "text": row["text"], "reply_to": row["reply_to"],
                "attachments": json.loads(row["attachments_json"]),
                "status": "queued" if row["acked_at"] is None else "accepted",
                "created_at": row["created_at"], "acked_at": row["acked_at"]}

    def get_peer_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM peer_messages WHERE message_id=?",
                (message_id,)).fetchone()
        return self._peer_dict(row) if row else None

    def peer_payload(self, message_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM peer_messages WHERE message_id=?",
                (message_id,)).fetchone()
        return row["payload_json"] if row else None

    def add_peer_message(self, message_id: str, sender: str, recipient: str,
                         text: str, reply_to: Optional[str],
                         attachments: List[Dict[str, Any]],
                         payload_json: str) -> Dict[str, Any]:
        # The dispatch lock serializes create/dedupe, ACK and stream replay.
        # SQLite autocommit completes before any SSE notification is queued.
        with self._lock:
            self._conn.execute(
                "INSERT INTO peer_messages (message_id, sender, recipient, text,"
                " reply_to, attachments_json, payload_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, sender, recipient, text, reply_to,
                 json.dumps(attachments, ensure_ascii=True), payload_json,
                 time.time()))
            return self.get_peer_message(message_id)

    def pending_peer_messages(self, worker: str, after: int = 0,
                              limit: int = PEER_PAGE_LIMIT
                              ) -> Tuple[List[Dict[str, Any]], bool]:
        """Bound both row count and encoded bytes; never load the whole inbox."""
        messages: List[Dict[str, Any]] = []
        size = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, message_id, sender, recipient, text, reply_to,"
                " attachments_json, created_at, acked_at FROM peer_messages"
                " WHERE recipient=? AND acked_at IS NULL AND seq>?"
                " ORDER BY seq LIMIT ?", (worker, after, limit + 1))
            try:
                for row in rows:
                    message = self._peer_dict(row)
                    encoded_size = len(json.dumps(
                        message, ensure_ascii=False).encode("utf-8")) + 2
                    if (len(messages) >= limit or
                            (messages and size + encoded_size > PEER_PAGE_BYTES - 1024)):
                        return messages, True
                    messages.append(message)
                    size += encoded_size
            finally:
                rows.close()
        return messages, False

    def ack_peer_message(self, message_id: str) -> Dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "UPDATE peer_messages SET acked_at=?"
                " WHERE message_id=? AND acked_at IS NULL",
                (time.time(), message_id))
            return self.get_peer_message(message_id)

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
            "execution_mode": "goal",
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
                    deadline_s: Optional[float] = None,
                    request_id: Optional[str] = None,
                    request_fingerprint: Optional[str] = None) -> Dict[str, Any]:
        task_id = "t-" + secrets.token_hex(5)
        now = time.time()
        started = now if state == "running" else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (task_id, worker, goal, context, workspace,"
                " urgency, state, created_at, started_at, finished_at,"
                " result_json, deadline_s, request_id, request_fingerprint)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
                (task_id, worker, goal, context, workspace, urgency, state,
                 now, started, deadline_s, request_id, request_fingerprint))
            self.refresh_counts(worker)
        log.info("task created id=%s worker=%s state=%s urgency=%s",
                 task_id, worker, state, urgency)
        task = self.get_task(task_id)
        assert task is not None
        return task

    def get_task_request(self, request_id: str
                         ) -> Optional[Tuple[Dict[str, Any], str]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
        return (self._task_dict(row), row["request_fingerprint"]) if row else None

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
        task = self.get_task(task_id)
        progress = self.approval_progress(task) if task else None
        if progress:
            result = {**result, "approval_id": progress["approval_id"],
                      "approval_status": progress["state"]}
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
                "UPDATE approvals SET status='denied', decision='denied', note='task cancelled', decided_at=?"
                " WHERE task_id=? AND status='pending'", (time.time(), task_id))
            task = self.get_task(task_id)
            progress = self.approval_progress(task) if task else None
            if progress:
                result["approval_id"] = progress["approval_id"]
                result["approval_status"] = progress["state"]
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

    # -- subscriptions --------------------------------------------------- #
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
            "trigger_id": row["trigger_id"],
            "trigger_since": row["trigger_since"],
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

    def set_subscription_trigger(self, sub_id: int, trigger_id: str,
                                 trigger_since: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._conn.execute(
                "UPDATE subscriptions SET trigger_id=?, trigger_since=?"
                " WHERE id=?", (trigger_id, int(trigger_since), int(sub_id)))
        log.info("subscription id=%d -> trigger=%s since=%d",
                 int(sub_id), trigger_id, int(trigger_since))
        return self.get_subscription(sub_id)

    def delete_subscription(self, sub_id: int) -> Optional[Dict[str, Any]]:
        """Hard delete one row (订阅不留存: no tombstones to leak)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE id=?", (int(sub_id),)
            ).fetchone()
            if row is None:
                return None
            sub = self._subscription_dict(row)
            self._conn.execute(
                "DELETE FROM subscriptions WHERE id=?", (int(sub_id),))
        log.info("subscription deleted id=%d worker=%s",
                 int(sub_id), sub["worker"])
        return sub

    def purge_subscriptions(self, worker: str) -> List[Dict[str, Any]]:
        """Drop every subscription of one worker; returns the rows."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subscriptions WHERE worker=? ORDER BY id",
                (worker,)).fetchall()
            subs = [self._subscription_dict(row) for row in rows]
            self._conn.execute(
                "DELETE FROM subscriptions WHERE worker=?", (worker,))
            for sub in subs:
                if sub.get("trigger_id"):
                    self._conn.execute(
                        "DELETE FROM subscription_cursors WHERE trigger_name=?",
                        (sub["trigger_id"],))
        if subs:
            log.info("subscriptions purged worker=%s count=%d", worker, len(subs))
        return subs

    def purge_all_subscriptions(self) -> List[Dict[str, Any]]:
        """Fresh process: nobody is connected, so nothing may stay subscribed."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subscriptions ORDER BY id").fetchall()
            subs = [self._subscription_dict(row) for row in rows]
            self._conn.execute("DELETE FROM subscriptions")
            self._conn.execute("DELETE FROM subscription_cursors")
        if subs:
            log.info("startup: purged %d stale subscription(s)", len(subs))
        return subs

    # -- subscription cursors (per trigger subscriber) ------------------- #
    def get_cursor(self, trigger_name: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_id FROM subscription_cursors WHERE trigger_name=?",
                (trigger_name,)).fetchone()
        return int(row["last_id"]) if row else 0

    def set_cursor(self, trigger_name: str, last_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO subscription_cursors (trigger_name, last_id,"
                " updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(trigger_name) DO UPDATE SET"
                " last_id=excluded.last_id, updated_at=excluded.updated_at",
                (trigger_name, int(last_id), time.time()))

    def delete_cursor(self, trigger_name: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM subscription_cursors WHERE trigger_name=?",
                (trigger_name,))

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

    # -- chat messages (store-only eavesdropping) ------------------------ #
    @staticmethod
    def _chat_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "worker": row["worker"],
            "direction": row["direction"],
            "text": row["text"],
            "session_id": row["session_id"],
            "ts": row["ts"],
            "message_id": row["message_id"],
        }

    def add_chat_message(self, worker: str, direction: str, text: str,
                         session_id: Optional[str] = None,
                         ts: Optional[float] = None,
                         message_id: Optional[str] = None,
                         meta: Optional[str] = None
                         ) -> Tuple[int, bool]:
        """Insert one row; same (worker, message_id, direction) is a no-op."""
        stamp = time.time() if ts is None else float(ts)
        with self._lock:
            if message_id:
                row = self._conn.execute(
                    "SELECT id FROM chat_messages WHERE worker=?"
                    " AND message_id=? AND direction=? ORDER BY id LIMIT 1",
                    (worker, message_id, direction)).fetchone()
                if row is not None:
                    return int(row["id"]), True
            cur = self._conn.execute(
                "INSERT INTO chat_messages (worker, direction, text,"
                " session_id, ts, message_id, meta)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (worker, direction, text, session_id, stamp, message_id, meta))
            return int(cur.lastrowid or 0), False

    def list_chat_messages(self, worker: str, since_id: Optional[int] = None,
                           since_ts: Optional[float] = None,
                           limit: int = 50,
                           direction: Optional[str] = None
                           ) -> List[Dict[str, Any]]:
        """Oldest-first window.  Without a cursor: the newest ``limit`` rows."""
        where = ["worker=?"]
        params: List[Any] = [worker]
        if since_id is not None:
            where.append("id > ?")
            params.append(int(since_id))
        if since_ts is not None:
            where.append("ts >= ?")
            params.append(float(since_ts))
        if direction:
            where.append("direction=?")
            params.append(direction)
        clause = " AND ".join(where)
        with self._lock:
            if since_id is None and since_ts is None:
                sql = ("SELECT * FROM (SELECT * FROM chat_messages"
                       " WHERE " + clause + " ORDER BY id DESC LIMIT ?)"
                       " ORDER BY id ASC")
            else:
                sql = ("SELECT * FROM chat_messages WHERE " + clause +
                       " ORDER BY id ASC LIMIT ?")
            rows = self._conn.execute(
                sql, tuple(params + [int(limit)])).fetchall()
        return [self._chat_dict(row) for row in rows]

    def chat_summary(self, worker: str, minutes: int) -> Dict[str, Any]:
        since_ts = time.time() - float(minutes) * 60.0
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total,"
                " COALESCE(SUM(direction='in'), 0) AS in_count,"
                " COALESCE(SUM(direction='out'), 0) AS out_count"
                " FROM chat_messages WHERE worker=? AND ts>=?",
                (worker, since_ts)).fetchone()
            last_row = self._conn.execute(
                "SELECT * FROM chat_messages WHERE worker=? AND ts>=?"
                " ORDER BY id DESC LIMIT 1", (worker, since_ts)).fetchone()
        last = self._chat_dict(last_row) if last_row is not None else None
        return {
            "since": since_ts,
            "count": int(row["total"] or 0),
            "in": int(row["in_count"] or 0),
            "out": int(row["out_count"] or 0),
            "last": last,
        }

    # -- approvals (worker asks, human decides) -------------------------- #

    @staticmethod
    def _approval_dict(row: sqlite3.Row, now: Optional[float] = None
                       ) -> Dict[str, Any]:
        stamp = time.time() if now is None else float(now)
        status = str(row["status"])
        expires_at = float(row["expires_at"] or 0)
        if status == "pending" and expires_at <= stamp:
            # Read-time truth even if the sweeper has not run yet.
            status = "expired"
        try:
            options = json.loads(row["options"]) if row["options"] else None
        except ValueError:
            options = None
        if not isinstance(options, list) or not options:
            options = ["approved", "denied"]
        decided_at = row["decided_at"]
        return {
            "approval_id": row["approval_id"],
            "worker": row["worker"],
            "task_id": row["task_id"],
            "kind": row["kind"],
            "title": row["title"],
            "detail": row["detail"],
            "options": options,
            "status": status,
            "decision": row["decision"] if status != "expired" else None,
            "note": row["note"],
            "created_at": row["created_at"],
            "created_at_iso": (now_iso(row["created_at"])
                               if row["created_at"] else None),
            "expires_at": expires_at,
            "expires_at_iso": now_iso(expires_at) if expires_at else None,
            "decided_at": decided_at,
            "decided_at_iso": now_iso(decided_at) if decided_at else None,
            "remaining_s": (round(max(0.0, expires_at - stamp), 1)
                            if status == "pending" else 0.0),
            "notified": bool(row["notified"]),
        }

    def _approval_row(self, approval_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?",
            (approval_id,)).fetchone()


    def approval_progress(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Approval is progress metadata, never a new task scheduling state."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE task_id=? AND worker=? ORDER BY rowid DESC",
                (task["task_id"], task["worker"])).fetchall()
        approvals = [self._approval_dict(row) for row in rows]
        if not approvals:
            return None
        pending = next((a for a in approvals if a["status"] == "pending"), None)
        latest = pending or approvals[0]
        waiting = task["state"] == "running" and pending is not None
        return {"state": "waiting_approval" if waiting else latest["status"],
                "waiting_for_approval": waiting,
                "approval_id": latest["approval_id"], "reason": latest["title"],
                "text": ("等待审批 %s（%s）" % (latest["approval_id"], latest["title"])
                         if waiting else "审批 %s：%s" % (latest["approval_id"], latest["status"])),
                "approvals": approvals}


    def create_approval(self, worker: str, kind: str, title: str,
                        task_id: Optional[str] = None,
                        detail: Optional[str] = None,
                        options: Optional[List[str]] = None,
                        ttl_s: float = APPROVAL_TTL_S,
                        auto: Optional[bool] = None,
                        ) -> Dict[str, Any]:
        now = time.time()
        ttl = min(APPROVAL_MAX_TTL_S, max(1.0, float(ttl_s)))
        payload = json.dumps(options or ["approved", "denied"],
                             ensure_ascii=False)
        with self._lock:
            for _attempt in range(4):
                approval_id = "ap_" + secrets.token_hex(8)
                try:
                    self._conn.execute(
                        "INSERT INTO approvals (approval_id, worker, task_id,"
                        " kind, title, detail, options, status, created_at,"
                        " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending',"
                        " ?, ?)",
                        (approval_id, worker, task_id, kind, title, detail,
                         payload, now, now + ttl))
                    break
                except sqlite3.IntegrityError:
                    continue
            else:  # pragma: no cover - 64 bits of entropy
                raise RuntimeError("could not allocate an approval id")
            task = self.get_task(task_id) if task_id else None
            if task and task["worker"] == worker and task["state"] == "cancelled":
                self._conn.execute(
                    "UPDATE approvals SET status='denied', decision='denied',"
                    " note='task cancelled', decided_at=? WHERE approval_id=?",
                    (now, approval_id))
            elif auto is not False:
                rule = match_approval_policy(worker, kind, title)
                if rule is not None:
                    label = str(rule.get("desc") or rule.get("kind")
                                or "policy")
                    self._conn.execute(
                        "UPDATE approvals SET status='approved',"
                        " decision='approved', note=?, decided_at=?"
                        " WHERE approval_id=? AND status='pending'",
                        ("溪流代批", now, approval_id))
                    log.info("approval auto-approved id=%s worker=%s kind=%s"
                             " rule=%s", approval_id, worker, kind, label)
            row = self._approval_row(approval_id)
        assert row is not None
        log.info("approval created id=%s worker=%s kind=%s ttl=%.0fs",
                 approval_id, worker, kind, ttl)
        return self._approval_dict(row, now)

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._approval_row(approval_id)
            if row is None:
                return None
            if (str(row["status"]) == "pending"
                    and float(row["expires_at"] or 0) <= time.time()):
                self._conn.execute(
                    "UPDATE approvals SET status='expired'"
                    " WHERE approval_id=? AND status='pending'",
                    (approval_id,))
                row = self._approval_row(approval_id)
        return self._approval_dict(row) if row is not None else None

    def list_approvals(self, worker: Optional[str] = None,
                       status: Optional[str] = None,
                       limit: int = 50) -> List[Dict[str, Any]]:
        # 不做写操作：_approval_dict 会把过期的 pending 显示成 expired，
        # 真正落库交给 decide 和清扫线程，避免每次读都触发一次 UPDATE。
        where = []
        params: List[Any] = []
        if worker:
            where.append("worker=?")
            params.append(worker)
        if status == "pending":
            where.append("(status='pending' AND expires_at>?)")
            params.append(time.time())
        elif status == "expired":
            # 过期是懒判定的：还没被清扫线程落库的 pending 也算 expired。
            where.append("(status='expired' OR (status='pending' AND expires_at<=?))")
            params.append(time.time())
        elif status:
            where.append("status=?")
            params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approvals" + clause +
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(params + [int(limit)])).fetchall()
        return [self._approval_dict(row) for row in rows]

    def decide_approval(self, approval_id: str, decision: str,
                        note: Optional[str] = None
                        ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Returns (outcome, approval).  Outcome: decided / already / expired
        / unknown / invalid."""
        if decision not in APPROVAL_DECISIONS:
            return "invalid", None
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status=?, decision=?, note=?,"
                " decided_at=? WHERE approval_id=? AND status='pending'"
                " AND expires_at>?",
                (decision, decision, note, now, approval_id, now))
            row = self._approval_row(approval_id)
            if row is None:
                return "unknown", None
            if cur.rowcount:
                log.info("approval decided id=%s decision=%s",
                         approval_id, decision)
                return "decided", self._approval_dict(row, now)
            status = str(row["status"])
            if status == "pending":
                # The deadline passed between the UPDATE and this read.
                self._conn.execute(
                    "UPDATE approvals SET status='expired'"
                    " WHERE approval_id=? AND status='pending'", (approval_id,))
                row = self._approval_row(approval_id)
                if row is None:
                    return "unknown", None
                status = "expired"
            if status == "expired":
                return "expired", self._approval_dict(row, now)
            return "already", self._approval_dict(row, now)

    def expire_approvals(self, now: Optional[float] = None) -> int:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET status='expired'"
                " WHERE status='pending' AND expires_at<=?", (stamp,))
            changed = int(cur.rowcount or 0)
        if changed:
            log.info("approvals expired: %d", changed)
        return changed

    def pending_approvals(self, worker: Optional[str] = None) -> int:
        clause = " AND worker=?" if worker else ""
        params: List[Any] = [time.time()] + ([worker] if worker else [])
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM approvals"
                " WHERE status='pending' AND expires_at>?" + clause,
                tuple(params)).fetchone()
        return int(row["n"] or 0)

    def mark_approval_notified(self, approval_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE approvals SET notified=1 WHERE approval_id=?",
                (approval_id,))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# trigger side
# --------------------------------------------------------------------------- #

class TriggerClient:
    """Small JSON client for trigger (stdlib urllib, 3s timeout per call)."""

    def __init__(self, trigger_url: str, token_path: str):
        self.trigger_url = trigger_url
        base = trigger_url.rstrip("/")
        if base.endswith("/trigger"):
            base = base[:-len("/trigger")]
        self.base_url = base or "http://127.0.0.1:8791"
        self.token_path = Path(os.path.expanduser(token_path))

    def _token(self) -> str:
        try:
            return self.token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def request(self, method: str, path: str, body: Optional[Any] = None,
                params: Optional[Dict[str, Any]] = None,
                timeout: float = 3.0) -> Tuple[int, Dict[str, Any]]:
        """Returns (status, payload); status 0 means "never reached trigger"."""
        token = self._token()
        if not token:
            return 0, {"error": "no_trigger_token",
                       "detail": "cannot read %s" % self.token_path}
        url = self.base_url + path
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        if clean:
            url += "?" + urlencode(clean)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method.upper())
        req.add_header("X-Trigger-Token", token)
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(65536).decode("utf-8", "replace")
                code = int(resp.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read(65536).decode("utf-8", "replace")
            code = int(exc.code)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"raw": raw[:500]}
        if not isinstance(payload, dict):
            payload = {"data": payload}
        return code, payload

    def post_trigger(self, payload: Dict[str, Any]
                     ) -> Tuple[bool, Dict[str, Any]]:
        status, data = self.request("POST", "/trigger", payload)
        if status == 200 and data.get("ok"):
            return True, data
        return False, {"status": status,
                       "error": data.get("error") or "trigger_rejected",
                       "detail": data}


class TriggerNotifier:
    """Writes exactly one ``task.result`` notify per finished task."""

    def __init__(self, client: TriggerClient):
        self.client = client

    def notify_result(self, task: Dict[str, Any], status: str,
                      summary: str) -> bool:
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
        ok, data = self.client.post_trigger(payload)
        if ok:
            log.info("trigger notify task=%s status=%s -> id=%s",
                     task["task_id"], status, data.get("id"))
        else:
            log.warning("trigger notify failed task=%s status=%s: %s",
                        task["task_id"], status, data.get("detail") or data)
        return ok


class TriggerSubscriptionBridge:
    """ROUTER.md §四 forwarding, implemented against trigger 0.7.x.

    trigger's public shape is a *cursor* bus: a subscriber's record is created
    lazily by ``GET /events?subscriber=<name>&since=<id>``, messages stay
    queued until ``POST /ack {subscriber, up_to_id}``, and the lease must be
    renewed with ``POST /renew``.  There is no ``POST /subscribe`` in 0.7.x;
    this bridge creates the same record by polling once at the current tail,
    so history is never replayed.

    A closed subscription name cannot be reopened by trigger, so a worker's
    first generation uses ``router:<worker>`` and a re-declaration after a
    disconnect uses ``router:<worker>:<subscription id>``.
    """

    def __init__(self, client: TriggerClient):
        self.client = client

    def last_event_id(self) -> Optional[int]:
        status, data = self.client.request("GET", "/bus/health")
        if status == 200 and isinstance(data.get("last_id"), int):
            return int(data["last_id"])
        log.warning("trigger /bus/health unusable: %.200s", data)
        return None

    def open(self, name: str, since: int) -> Tuple[bool, Dict[str, Any]]:
        """Create (or touch) the trigger-side subscription at ``since``."""
        status, data = self.client.request(
            "GET", "/events",
            params={"subscriber": name, "since": int(since), "limit": 1})
        if status != 200:
            return False, {"status": status,
                           "error": data.get("error") or "trigger_error",
                           "detail": data}
        view = data.get("subscription") or {}
        if view.get("status") != "active" or view.get("expired"):
            return False, {"error": "subscription_%s" % (
                "closed" if view.get("status") == "closed" else "expired"),
                "subscription": view}
        return True, view

    def renew(self, name: str) -> bool:
        status, data = self.client.request(
            "POST", "/renew", {"name": name})
        if status == 200:
            log.info("trigger subscription renewed name=%s expires_in=%s",
                     name, data.get("expires_in"))
            return True
        log.warning("trigger renew failed name=%s status=%s: %.200s",
                    name, status, data)
        return False

    def close(self, name: str) -> bool:
        """DELETE /subscriptions?name=, falling back to POST /unsubscribe."""
        status, data = self.client.request(
            "DELETE", "/subscriptions", params={"name": name})
        if status == 200:
            log.info("trigger subscription closed name=%s", name)
            return True
        status2, data2 = self.client.request(
            "POST", "/unsubscribe", {"name": name})
        if status2 == 200:
            log.info("trigger subscription closed name=%s (via /unsubscribe)",
                     name)
            return True
        log.warning("trigger unsubscribe failed name=%s status=%s/%s: %.200s",
                    name, status, status2, data2 or data)
        return False

    def fetch(self, name: str, since: int, limit: int
              ) -> Tuple[int, Dict[str, Any]]:
        return self.client.request(
            "GET", "/events",
            params={"subscriber": name, "since": int(since),
                    "limit": max(1, min(int(limit), 200))})

    def ack(self, name: str, up_to_id: int) -> bool:
        status, data = self.client.request(
            "POST", "/ack", {"subscriber": name, "up_to_id": int(up_to_id)})
        if status == 200:
            return True
        log.warning("trigger ack failed name=%s up_to=%s status=%s: %.200s",
                    name, up_to_id, status, data)
        return False


# --------------------------------------------------------------------------- #
# SSE connection registry
# --------------------------------------------------------------------------- #

class Connection:
    """One SSE writer; peer events are prefetched in bounded batches."""

    def __init__(self, name: str, version: Optional[str] = None):
        self.name = name
        self.version = version
        self.queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.closed = False
        self.reason = ""
        self.connected_at = time.time()
        self.peer_after = 0
        self.peer_buffered = 0

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


def worker_control_url(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip().rstrip("/")
    if not raw:
        return None
    parsed = urlparse(raw)
    try:
        port = parsed.port
    except ValueError:
        raise BadRequest("invalid control_url port")
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or not port or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise BadRequest("control_url must be a numeric loopback HTTP origin with an explicit port")
    return raw


class RouterServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, addr, store: RouterStore, token: str,
                 notifier: TriggerNotifier, bridge: TriggerSubscriptionBridge,
                 ping_interval: float = PING_INTERVAL,
                 backlog_max: int = BACKLOG_MAX):
        self.store = store
        self.attachments = AttachmentStore(store.path + ".attachments")
        self.started_at = time.time()
        self.token = token or ""
        self.notifier = notifier
        self.trigger_client = notifier.client
        self.trigger_bridge = bridge
        self.ping_interval = max(0.2, float(ping_interval))
        self.backlog_max = max(1, int(backlog_max))
        self.conns: Dict[str, Connection] = {}
        self.conn_lock = threading.Lock()
        self.dispatch_lock = threading.RLock()
        super().__init__(addr, RouterHandler)

    def server_bind(self) -> None:
        # HTTPServer resolves a decorative server_name with getfqdn(), which
        # can block macOS startup on unavailable reverse DNS. Bind the configured
        # address directly; authentication and remote-bind opt-in stay unchanged.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


    def cancel_execution(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Deliver only the exact task identity; never a selected-session stop."""
        worker = self.store.get_worker(task["worker"]) or {}
        base = worker.get("control_url")
        if not base:
            delivery = {"ok": False, "error": "worker_control_unavailable"}
        else:
            try:
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, headers, newurl):
                        return None
                req = urllib.request.Request(
                    base + "/manager/tasks/" + task["task_id"] + "/cancel",
                    data=json.dumps({"task_id": task["task_id"]}).encode("utf-8"),
                    headers={"Authorization": "Bearer " + self.token,
                             "Content-Type": "application/json"},
                    method="POST")
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), NoRedirect())
                with opener.open(req, timeout=3.0) as response:
                    payload = json.loads(response.read(65536).decode("utf-8"))
                    if not 200 <= response.status < 300 or payload.get("ok") is not True:
                        raise ValueError("worker rejected cancellation")
                    if payload.get("task_id") != task["task_id"]:
                        raise ValueError("worker acknowledged a different task")
                delivery = {"ok": True, "worker": payload}
            except Exception as exc:
                delivery = {"ok": False, "error": type(exc).__name__,
                            "detail": str(exc)[:300]}
        log.log(logging.INFO if delivery["ok"] else logging.ERROR,
                "cancel_forward_%s task=%s worker=%s delivery=%s",
                "ok" if delivery["ok"] else "failed", task["task_id"],
                task["worker"], delivery)
        with self.store._lock:
            current = self.store.get_task(task["task_id"])
            if current and current["state"] == "cancelled":
                result = current.get("result") or {}
                result["cancel_delivery"] = delivery
                self.store._conn.execute(
                    "UPDATE tasks SET result_json=? WHERE task_id=? AND state='cancelled'",
                    (json.dumps(result, ensure_ascii=False), task["task_id"]))
        return delivery

    # -- connections ---------------------------------------------------- #
    def current_conn(self, name: str) -> Optional[Connection]:
        with self.conn_lock:
            return self.conns.get(name)

    def online(self, name: str) -> bool:
        return self.current_conn(name) is not None

    def attach(self, name: str, version: Optional[str] = None) -> Connection:
        conn = Connection(name, version)
        with self.dispatch_lock:
            # Publish the connection and replay under the same lock as peer
            # creation/ACK. Otherwise a live create can overtake its replay.
            with self.conn_lock:
                old = self.conns.get(name)
                self.conns[name] = conn
                if old is not None:
                    old.close("replaced")
            self.store.set_online(name, version)
            # A reconnect is at-least-once: anything still marked running was
            # sent over the previous stream, so hand it out again.
            self.store.requeue_running(name)
            self.flush_backlog_locked(name)
            self.flush_peer_messages_locked(conn)
        log.info("stream attached name=%s version=%s (queued=%d)",
                 name, version or "-", self.store.queued_count(name))
        return conn

    def detach(self, conn: Connection, reason: str = "closed") -> None:
        purged: List[Dict[str, Any]] = []
        with self.dispatch_lock:
            with self.conn_lock:
                current = self.conns.get(conn.name) is conn
                if current:
                    self.conns.pop(conn.name, None)
            if current:
                self.store.set_offline(conn.name)
                self.store.requeue_running(conn.name)
                # 订阅不留存: a worker that is not listening keeps nothing.
                purged = self.store.purge_subscriptions(conn.name)
            self.store.refresh_counts(conn.name)
        for sub in purged:
            if sub.get("trigger_id"):
                self.trigger_bridge.close(sub["trigger_id"])
        if purged:
            log.info("stream detached name=%s: dropped %d subscription(s)",
                     conn.name, len(purged))
        log.info("stream detached name=%s reason=%s current=%s queued=%d",
                 conn.name, reason, current, self.store.queued_count(conn.name))

    # -- dispatch ------------------------------------------------------- #
    def flush_peer_messages_locked(self, conn: Connection) -> None:
        """Caller holds dispatch_lock; no socket I/O while replaying SQLite.

        peer_after is only a per-stream prefetch cursor, never a durable ACK.
        Draining the last buffered event refills the next bounded page. A new
        stream starts at zero, so all still-queued messages replay in order.
        """
        if conn.closed or self.current_conn(conn.name) is not conn:
            return
        available = PEER_STREAM_BUFFER - conn.peer_buffered
        if available <= 0:
            return
        messages, _ = self.store.pending_peer_messages(
            conn.name, conn.peer_after, available)
        for message in messages:
            conn.queue.put({"event": "worker_message", "data": message})
            conn.peer_buffered += 1
            conn.peer_after = message["seq"]

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


class ApprovalSweeper(threading.Thread):
    """Expire stale pending approvals even when nobody polls them."""

    def __init__(self, store: RouterStore,
                 interval: float = APPROVAL_SWEEP_S) -> None:
        super().__init__(daemon=True, name="approval-sweeper")
        self.store = store
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.store.expire_approvals()
            except Exception:  # noqa: BLE001
                log.exception("approval sweep failed")

    def stop(self) -> None:
        self._stop.set()


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

    @staticmethod
    def _worker_path_param(raw: str) -> str:
        name = unquote(raw).strip()
        if not name:
            raise BadRequest("worker is required")
        if len(name) > 128:
            raise BadRequest("worker too long")
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
                                  "/notice", "/messages", "/messages/{id}",
                                  "/messages/{id}/ack", "/subscriptions",
                                  "/subscriptions/pending", "/chat/log",
                                  "/chat/{worker}/messages",
                                  "/chat/{worker}/summary",
                                  "/approvals", "/approvals/{id}",
                                  "/approvals/{id}/decide",
                                  "/ops/services",
                                  "/ops/services/{name}/restart"],
                })
            if parts == ["stream"]:
                return self._serve_stream(qs)
            self._require_token()
            if len(parts) == 2 and parts[0] == "attachments":
                return self._attachment_get(parts[1])
            if parts == ["ops", "services"]:
                return self._send_json(*self._ops_list())
            if parts == ["workers"]:
                return self._send_json(200, self._workers())
            if parts == ["tasks"]:
                return self._send_json(200, self._tasks_list(qs))
            if len(parts) == 2 and parts[0] == "tasks":
                return self._task_get(parts[1])
            if parts == ["messages"]:
                return self._send_json(*self._peer_messages(qs))
            if len(parts) == 2 and parts[0] == "messages":
                return self._send_json(*self._peer_message_get(parts[1]))
            if parts == ["subscriptions", "pending"]:
                return self._send_json(*self._subscriptions_pending(qs))
            if parts == ["subscriptions"]:
                return self._send_json(200, self._subscriptions_list(qs))
            if (len(parts) == 3 and parts[0] == "chat"
                    and parts[2] in ("messages", "summary")):
                worker = self._worker_path_param(parts[1])
                if parts[2] == "messages":
                    return self._send_json(200, self._chat_messages(qs, worker))
                return self._send_json(200, self._chat_summary(qs, worker))
            if parts == ["approvals"]:
                return self._send_json(*self._approvals_list(qs))
            if len(parts) == 2 and parts[0] == "approvals":
                return self._send_json(*self._approval_get(
                    unquote(parts[1]).strip()))
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
            if parts == ["attachments"]:
                return self._send_json(*self._attachment_put())
            body = self._read_json()
            if (len(parts) == 4 and parts[0] == "ops"
                    and parts[1] == "services" and parts[3] == "restart"):
                return self._send_json(*self._ops_restart(
                    unquote(parts[2]).strip()))
            if parts == ["register"]:
                return self._send_json(200, self._register(body))
            if parts == ["heartbeat"]:
                return self._send_json(*self._heartbeat(body))
            if parts == ["result"]:
                return self._send_json(*self._result(body))
            if parts == ["notice"]:
                return self._send_json(*self._notice(body))
            if parts == ["chat", "log"]:
                return self._send_json(200, self._chat_log(body))
            if parts == ["messages"]:
                return self._send_json(*self._peer_message_create(body))
            if len(parts) == 3 and parts[0] == "messages" and parts[2] == "ack":
                return self._send_json(*self._peer_message_ack(parts[1], body))
            if parts == ["tasks"]:
                return self._send_json(*self._task_create(body))
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
                return self._send_json(*self._task_cancel(parts[1]))
            if parts == ["subscriptions"]:
                return self._send_json(*self._subscription_create(body))
            if parts == ["approvals"]:
                return self._send_json(*self._approval_create(body))
            if (len(parts) == 3 and parts[0] == "approvals"
                    and parts[2] == "decide"):
                return self._send_json(*self._approval_decide(
                    unquote(parts[1]).strip(), body))
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
            "approvals_pending": self.server.store.pending_approvals(),
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
        for task in tasks:
            task["progress"] = self.server.store.approval_progress(task)
        return {"ok": True, "count": len(tasks), "tasks": tasks}

    def _task_get(self, task_id: str) -> None:
        task = self.server.store.get_task(task_id)
        if task is None:
            return self._send_json(404, {"ok": False, "error": "unknown_task",
                                         "task_id": task_id})
        task["progress"] = self.server.store.approval_progress(task)
        self._send_json(200, {"ok": True, "task_id": task_id,
                              "state": task["state"], "task": task})

    def _subscriptions_list(self, qs: Dict[str, List[str]]) -> Dict[str, Any]:
        worker = (qs.get("worker", [""])[0] or "").strip() or None
        active_only = (qs.get("active", [""])[0] or "").strip() in (
            "1", "true", "yes")
        subs = self.server.store.list_subscriptions(worker, active_only)
        return {"ok": True, "count": len(subs), "subscriptions": subs}

    # -- peer messages (never create tasks/results or implicit replies) --- #
    # POST /messages -> 202 on create, 200 on an exact JSON payload retry,
    # 409 if that UUID is reused with any changed field. JSON key order is
    # immaterial; missing vs explicit optional fields remain distinct.
    # queued means committed to SQLite, even if already emitted over SSE.
    # Only POST /messages/<UUID>/ack {worker: <to>} changes it to accepted:
    # accepted means recipient receipt, not a reply or completed task.
    # GET /messages/<UUID> (or /messages?id=UUID) queries either state.
    # GET /messages?worker=NAME[&after=SEQ&limit=1..100] pages the pending
    # inbox in seq order, capped at 256 KiB. Follow next_after if has_more.
    # All routes use the existing shared X-Router-Token authentication;
    # worker identities are explicit assertions, not separate credentials.
    @staticmethod
    def _peer_uuid(value: Any, label: str = "message_id") -> str:
        if isinstance(value, str) and len(value) == 36:
            try:
                parsed = str(uuid.UUID(value))
                if parsed == value.lower():
                    return parsed
            except ValueError:
                pass
        raise BadRequest("%s must be a UUID" % label)

    @staticmethod
    def _peer_worker(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise BadRequest("%s must be a worker name (1..128 chars)" % label)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise BadRequest("%s must contain valid Unicode" % label)
        return value

    @staticmethod
    def _peer_receipt(message: Dict[str, Any], **extra) -> Dict[str, Any]:
        return {"ok": True, "message_id": message["message_id"],
                "status": message["status"], "message": message, **extra}

    def _peer_message_get(self, raw_id: str) -> Tuple[int, Dict[str, Any]]:
        message_id = self._peer_uuid(unquote(raw_id))
        message = self.server.store.get_peer_message(message_id)
        if message is None:
            return 404, {"ok": False, "error": "unknown_message"}
        return 200, self._peer_receipt(message)

    def _peer_messages(self, qs: Dict[str, List[str]]) -> Tuple[int, Dict[str, Any]]:
        if "id" in qs:
            if "worker" in qs or len(qs["id"]) != 1:
                raise BadRequest("use either id or worker")
            return self._peer_message_get(qs["id"][0])
        worker = self._peer_worker(qs.get("worker", [None])[0], "worker")
        if self.server.store.get_worker(worker) is None:
            return 404, {"ok": False, "error": "unknown_worker"}
        after = self._int_param(qs, "after", 0)
        limit = self._int_param(qs, "limit", PEER_PAGE_LIMIT)
        if after is None or not 0 <= after <= 9223372036854775807:
            raise BadRequest("after must be a nonnegative SQLite sequence")
        if limit is None or not 1 <= limit <= PEER_PAGE_LIMIT:
            raise BadRequest("limit must be 1..100")
        messages, more = self.server.store.pending_peer_messages(worker, after, limit)
        return 200, {"ok": True, "worker": worker, "count": len(messages),
                     "messages": messages, "has_more": more,
                     "next_after": messages[-1]["seq"] if more else None}

    def _peer_message_create(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        message_id = self._peer_uuid(body.get("message_id"))
        payload_json = json.dumps(body, sort_keys=True, ensure_ascii=True,
                                  separators=(",", ":"))
        with self.server.dispatch_lock:
            previous = self.server.store.peer_payload(message_id)
            if previous is not None:
                if previous != payload_json:
                    return 409, {"ok": False, "error": "message_id_conflict",
                                 "message_id": message_id}
                # Do not re-resolve images or re-emit an exact retry. The
                # original metadata and current ACK state are authoritative.
                message = self.server.store.get_peer_message(message_id)
                return 200, self._peer_receipt(message, duplicate=True)
            if set(body) - {"message_id", "from", "to", "text", "reply_to", "attachments"}:
                raise BadRequest("unsupported message fields")
            sender = self._peer_worker(body.get("from"), "from")
            recipient = self._peer_worker(body.get("to"), "to")
            for worker in (sender, recipient):
                if self.server.store.get_worker(worker) is None:
                    return 404, {"ok": False, "error": "unknown_worker"}
            text = body.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > PEER_MAX_TEXT:
                raise BadRequest("text must be nonempty (max 20000 chars)")
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                raise BadRequest("text must contain valid Unicode")
            reply_to = body.get("reply_to")
            if reply_to is not None:
                reply_to = self._peer_uuid(reply_to, "reply_to")
                parent = self.server.store.get_peer_message(reply_to)
                if parent is None:
                    return 404, {"ok": False, "error": "unknown_reply_to"}
                if {parent["from"], parent["to"]} != {sender, recipient}:
                    raise BadRequest("reply_to must belong to the same conversation pair")
            attachments = []
            if "attachments" in body:
                values = body["attachments"]
                if not isinstance(values, list) or len(values) > MAX_ATTACHMENTS:
                    raise BadRequest("attachments must contain at most 10 uploaded image ids")
                refs = []
                for value in values:
                    if isinstance(value, str):
                        value = {"id": value}
                    if not isinstance(value, dict) or set(value) - {"id", "name"}:
                        raise BadRequest("attachments accept only uploaded image ids and display names")
                    name = value.get("name")
                    if name is not None:
                        if not isinstance(name, str) or len(name) > 200:
                            raise BadRequest("attachment name must be at most 200 chars")
                        try:
                            name.encode("utf-8")
                        except UnicodeEncodeError:
                            raise BadRequest("attachment name must contain valid Unicode")
                    refs.append(value)
                if refs:
                    try:
                        attachments = self.server.attachments.resolve(refs)
                    except (ValueError, OSError):
                        raise BadRequest("attachments must reference existing uploaded images")
            message = self.server.store.add_peer_message(
                message_id, sender, recipient, text, reply_to, attachments, payload_json)
            conn = self.server.current_conn(recipient)
            if conn is not None:
                self.server.flush_peer_messages_locked(conn)
        log.info("peer queued id=%s", message_id)
        return 202, self._peer_receipt(message, duplicate=False)

    def _peer_message_ack(self, raw_id: str, body: Dict[str, Any]
                          ) -> Tuple[int, Dict[str, Any]]:
        message_id = self._peer_uuid(unquote(raw_id))
        if set(body) - {"worker"}:
            raise BadRequest("ACK accepts only worker")
        worker = self._peer_worker(body.get("worker"), "worker")
        with self.server.dispatch_lock:
            message = self.server.store.get_peer_message(message_id)
            if message is None:
                return 404, {"ok": False, "error": "unknown_message"}
            if worker != message["to"]:
                return 403, {"ok": False, "error": "wrong_recipient"}
            already = message["status"] == "accepted"
            message = self.server.store.ack_peer_message(message_id)
        log.info("peer accepted id=%s already=%s", message_id, already)
        return 200, self._peer_receipt(message, already=already)

    # -- chat endpoints (store only; nothing is forwarded) --------------- #
    def _chat_log(self, body: Dict[str, Any]) -> Dict[str, Any]:
        worker = str(body.get("worker") or "").strip()
        if not worker:
            raise BadRequest("worker is required")
        if len(worker) > 128:
            raise BadRequest("worker too long")
        direction = str(body.get("direction") or "").strip().lower()
        if direction not in CHAT_DIRECTIONS:
            raise BadRequest("direction must be in or out")
        text = body.get("text")
        if text is None or not str(text).strip():
            raise BadRequest("text is required")
        text = str(text)
        if len(text) > 100000:
            raise BadRequest("text too long (max 100000 chars)")
        session_id = body.get("session_id")
        if session_id is not None:
            session_id = str(session_id).strip() or None
            if session_id is not None and len(session_id) > 128:
                raise BadRequest("session_id too long")
        message_id = body.get("message_id")
        if message_id is not None:
            message_id = str(message_id).strip() or None
            if message_id is not None and len(message_id) > 200:
                raise BadRequest("message_id too long")
        ts = body.get("ts")
        if ts is not None:
            ts = parse_epoch(ts, "ts")
        meta = body.get("meta")
        if meta is not None:
            if isinstance(meta, (dict, list)):
                meta = json.dumps(meta, ensure_ascii=False)
            elif not isinstance(meta, str):
                raise BadRequest("meta must be an object, list or string")
            if len(meta) > 16000:
                raise BadRequest("meta too large (max 16000 chars)")
        msg_id, duplicate = self.server.store.add_chat_message(
            worker, direction, text, session_id, ts, message_id, meta)
        log.info("chat %s id=%d worker=%s direction=%s duplicate=%s",
                 "duplicate" if duplicate else "logged", msg_id, worker,
                 direction, duplicate)
        return {"ok": True, "id": msg_id, "duplicate": duplicate}

    def _chat_messages(self, qs: Dict[str, List[str]],
                       worker: str) -> Dict[str, Any]:
        since_raw = (qs.get("since", [""])[0] or "").strip()
        since_id: Optional[int] = None
        since_ts: Optional[float] = None
        if since_raw:
            try:
                since_val = float(since_raw)
            except ValueError:
                since_ts = parse_epoch(since_raw, "since")
            else:
                # Small integers are message ids (cursor); ISO/epoch-looking
                # values (>= 1e9 or a decimal point) are timestamps.
                if "." in since_raw or since_val >= 1e9:
                    since_ts = since_val
                else:
                    since_id = int(since_val)
        limit = self._int_param(qs, "limit", 50)
        if limit is None:
            limit = 50
        if limit < 1 or limit > 500:
            raise BadRequest("limit must be between 1 and 500")
        direction = (qs.get("direction", [""])[0] or "").strip().lower() or None
        if direction is not None and direction not in CHAT_DIRECTIONS:
            raise BadRequest("direction must be in or out")
        messages = self.server.store.list_chat_messages(
            worker, since_id=since_id, since_ts=since_ts, limit=limit,
            direction=direction)
        return {"ok": True, "worker": worker, "count": len(messages),
                "messages": messages}

    def _chat_summary(self, qs: Dict[str, List[str]],
                      worker: str) -> Dict[str, Any]:
        minutes = self._int_param(qs, "minutes", 60)
        if minutes is None:
            minutes = 60
        if minutes < 1 or minutes > 525600:
            raise BadRequest("minutes must be between 1 and 525600")
        summary = self.server.store.chat_summary(worker, minutes)
        return {"ok": True, "worker": worker, "minutes": minutes,
                "since": summary["since"], "count": summary["count"],
                "in": summary["in"], "out": summary["out"],
                "last": summary["last"]}

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
        control_url = worker_control_url(body.get("control_url"))
        worker, created = self.server.store.upsert_worker(name, version, note, control_url)
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
        # A UUID identifies one formal work order; ordinary chat uses /messages.
        # Compare the submitted JSON (key order is immaterial), before live
        # worker/backlog checks. Commit the key and fingerprint in the task's
        # INSERT, so a lost HTTP response or process restart cannot split them.
        request_id = (self._peer_uuid(body["message_id"])
                      if "message_id" in body else None)
        fingerprint = None
        if request_id is not None:
            payload = {key: value for key, value in body.items() if key != "message_id"}
            fingerprint = hashlib.sha256(json.dumps(
                payload, sort_keys=True, ensure_ascii=True,
                separators=(",", ":")).encode("ascii")).hexdigest()
        with self.server.dispatch_lock:
            if request_id is not None:
                previous = self.server.store.get_task_request(request_id)
                if previous is not None:
                    task, saved_fingerprint = previous
                    if saved_fingerprint != fingerprint:
                        return 409, {"ok": False, "error": "message_id_conflict",
                                     "message_id": request_id}
                    return 200, {
                        "ok": True, "task_id": task["task_id"],
                        "worker": task["worker"], "state": task["state"],
                        "execution_mode": "goal", "duplicate": True,
                        "dispatched": False, "queued": task["state"] == "queued",
                    }
            if "execution_mode" in body and body["execution_mode"] != "goal":
                raise BadRequest("execution_mode must be goal; use /messages for chat")
            worker_name = str(body.get("worker") or "").strip()
            if not worker_name:
                raise BadRequest("worker is required")
            goal = body.get("goal")
            if goal is None or not str(goal).strip():
                raise BadRequest("goal is required")
            goal = str(goal).strip()
            if len(goal) > MAX_GOAL_CHARS:
                raise BadRequest(
                    "goal too long (max %d chars, got %d)"
                    % (MAX_GOAL_CHARS, len(goal)))
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

            online = self.server.online(worker_name)
            if online and store.running_count(worker_name) == 0:
                task = store.create_task(
                    worker_name, goal, context, workspace, urgency,
                    state="running", deadline_s=deadline_s,
                    request_id=request_id, request_fingerprint=fingerprint)
                self.server.store.refresh_counts(worker_name)
                conn = self.server.current_conn(worker_name)
                if conn is not None and self.server.push(conn, task):
                    return 200, {
                        "ok": True, "task_id": task["task_id"],
                        "worker": worker_name, "state": "running",
                        "execution_mode": "goal",
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
                    "execution_mode": "goal",
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
                state="queued", deadline_s=deadline_s,
                request_id=request_id, request_fingerprint=fingerprint)
            store.refresh_counts(worker_name)
            status = 202
            log.info("task queued id=%s worker=%s online=%s ahead=%d",
                     task["task_id"], worker_name, online, ahead)
            return status, {
                "ok": True, "task_id": task["task_id"],
                "worker": worker_name, "state": "queued",
                "execution_mode": "goal",
                "dispatched": False, "queued": True,
                "queued_ahead": ahead,
            }

    def _task_cancel(self, task_id: str) -> Tuple[int, Dict[str, Any]]:
        with self.server.dispatch_lock:
            task = self.server.store.get_task(task_id)
            if task is None:
                return 404, {"ok": False, "error": "unknown_task", "task_id": task_id}
            already = task["state"] == "cancelled"
            if not already and task["state"] in TERMINAL_STATES:
                return 409, {"ok": False, "error": "not_cancellable",
                             "task_id": task_id, "state": task["state"]}
            was_running = task["state"] == "running"
            retry_delivery = already and (task.get("result") or {}).get(
                "cancel_delivery", {}).get("ok") is False
            if not already:
                task = self.server.store.cancel_task(task_id)
                self.server.store.refresh_counts(task["worker"])
        delivery = ((task.get("result") or {}).get("cancel_delivery")
                    if already else {"ok": True, "skipped": "queued_task"})
        if was_running or retry_delivery:
            delivery = self.server.cancel_execution(task)
        with self.server.dispatch_lock:
            dispatched = self.server.dispatch_one(task["worker"]) if not already else None
        log.info("task cancelled id=%s worker=%s delivery=%s", task_id, task["worker"], delivery)
        return 200, {"ok": True, "task_id": task_id, "state": "cancelled",
                     "already": already, "cancel_delivery": delivery,
                     "dispatched_next": dispatched["task_id"] if dispatched else None}

    def _attachment_put(self):
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("use a bounded Content-Length")
            size = int(self.headers.get("Content-Length") or 0)
            record = self.server.attachments.put(
                self.rfile, size, self.headers.get("X-Attachment-SHA256"))
            return 201, {"ok": True, "attachment": record}
        except (ValueError, OSError) as exc:
            self.close_connection = True
            raise BadRequest(str(exc))

    def _attachment_get(self, attachment_id):
        try:
            record = self.server.attachments.describe(attachment_id)
        except FileNotFoundError:
            raise NotFound()
        except ValueError as exc:
            raise BadRequest(str(exc))
        with (self.server.attachments.root / attachment_id).open("rb") as stream:
            self.send_response(200)
            self.send_header("Content-Type", record["media_type"])
            self.send_header("Content-Length", str(record["size"]))
            self.send_header("X-Attachment-SHA256", record["sha256"])
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _notice(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Worker → Hermes word path.  A trigger failure never becomes a 5xx."""
        worker = str(body.get("worker") or "").strip()
        text = body.get("text")
        if not worker:
            raise BadRequest("worker is required")
        if len(worker) > 128:
            raise BadRequest("worker too long")
        if text is None or not str(text).strip():
            raise BadRequest("text is required")
        text = str(text)
        if len(text) > 8000:
            raise BadRequest("text too long (max 8000 chars)")
        kind = str(body.get("kind") or "worker.notice").strip() or "worker.notice"
        if len(kind) > 128:
            raise BadRequest("kind too long")
        urgency = str(body.get("urgency") or "normal").strip().lower()
        if urgency not in URGENCIES:
            raise BadRequest("urgency must be urgent or normal")
        attachments = []
        if "attachments" in body:
            try:
                attachments = self.server.attachments.resolve(body["attachments"])
            except (ValueError, OSError) as exc:
                raise BadRequest("invalid attachments: %s" % exc)
        message_id = body.get("message_id")
        if message_id is not None and (not isinstance(message_id, str)
                or not message_id or len(message_id) > 128
                or any(not c.isalnum() and c not in "-_" for c in message_id)):
            raise BadRequest("invalid message_id")
        level = "event" if urgency == "urgent" else "notify"
        minute = int(time.time() // 60)
        digest = hashlib.sha256(
            ("%s\n%s\n%d" % (worker, text, minute)).encode("utf-8")
        ).hexdigest()[:24]
        if attachments or message_id:
            identity = json.dumps([worker, kind, text, attachments,
                                   message_id if message_id else minute],
                                  sort_keys=True, ensure_ascii=False)
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        event_id = "notice:" + digest
        payload = {
            "source": "router",
            "kind": kind,
            "level": level,
            "text": text,
            "actor": worker,
            "event_id": event_id,
            "payload": {"worker": worker, "urgency": urgency,
                        "notice": True},
        }
        if attachments:
            payload["payload"]["attachments"] = attachments
        ok, data = self.server.trigger_client.post_trigger(payload)
        if not ok:
            log.warning("notice failed worker=%s urgency=%s: %.300s",
                        worker, urgency, data.get("detail") or data)
            return 200, {
                "ok": False, "error": "trigger_unavailable",
                "detail": data.get("detail") or data,
                "event_id": event_id, "level": level,
            }
        log.info("notice worker=%s urgency=%s event_id=%s trigger_id=%s"
                 " duplicate=%s", worker, urgency, event_id, data.get("id"),
                 data.get("duplicate"))
        return 200, {
            "ok": True,
            "trigger_id": data.get("id"),
            "level": level,
            "event_id": event_id,
            "duplicate": bool(data.get("duplicate")),
        }

    # -- approval endpoints --------------------------------------------- #
    def _approval_notify(self, approval: Dict[str, Any]) -> bool:
        """One Feishu push per approval. Reuses the /notice trigger path."""
        one_line = " ".join(str(approval.get("detail") or "").split())
        if len(one_line) > 400:
            one_line = one_line[:397] + "..."
        text = "审批请求 [%s] %s：%s" % (
            approval["kind"], approval["worker"], approval["title"])
        if one_line:
            text += "\n" + one_line
        text += "\napproval_id: %s" % approval["approval_id"]
        payload = {
            "source": "router",
            "kind": "approval.request",
            "level": "event",
            "text": text,
            "actor": approval["worker"],
            "event_id": "approval:" + approval["approval_id"],
            "payload": {
                "approval_id": approval["approval_id"],
                "worker": approval["worker"],
                "task_id": approval.get("task_id"),
                "kind": approval["kind"],
                "options": approval.get("options"),
                "notice": True,
            },
        }
        ok, data = self.server.trigger_client.post_trigger(payload)
        if not ok:
            log.warning("approval notify failed id=%s: %.300s",
                        approval["approval_id"], data.get("detail") or data)
            return False
        log.info("approval notify id=%s trigger_id=%s duplicate=%s",
                 approval["approval_id"], data.get("id"), data.get("duplicate"))
        return True

    def _approval_create(self, body: Dict[str, Any]
                         ) -> Tuple[int, Dict[str, Any]]:
        worker = str(body.get("worker") or "").strip()
        if not worker:
            raise BadRequest("worker is required")
        if len(worker) > 128:
            raise BadRequest("worker too long")
        kind = str(body.get("kind") or "approval").strip() or "approval"
        if len(kind) > 64:
            raise BadRequest("kind too long")
        title = body.get("title")
        if title is None or not str(title).strip():
            raise BadRequest("title is required")
        title = str(title).strip()
        if len(title) > APPROVAL_MAX_TITLE:
            raise BadRequest("title too long (max %d chars)"
                             % APPROVAL_MAX_TITLE)
        detail = body.get("detail")
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)
        if detail is not None:
            detail = str(detail)
            if len(detail) > APPROVAL_MAX_DETAIL:
                detail = detail[:APPROVAL_MAX_DETAIL - 3] + "..."
        task_id = body.get("task_id", body.get("taskId"))
        task_id = str(task_id).strip() if task_id is not None else None
        if task_id == "":
            task_id = None
        options = body.get("options")
        if options is None:
            options = ["approved", "denied"]
        if not isinstance(options, list) or not options:
            raise BadRequest("options must be a non-empty array")
        if len(options) > APPROVAL_MAX_OPTIONS:
            raise BadRequest("options too long (max %d)"
                             % APPROVAL_MAX_OPTIONS)
        options = [str(item).strip() for item in options]
        if any(not item or len(item) > 64 for item in options):
            raise BadRequest("every option must be a non-empty string "
                             "of at most 64 chars")
        ttl_raw = body.get("ttl_s", body.get("ttlS", APPROVAL_TTL_S))
        try:
            ttl_s = float(ttl_raw)
        except (TypeError, ValueError):
            raise BadRequest("ttl_s must be a number")
        if ttl_s <= 0:
            raise BadRequest("ttl_s must be > 0")

        auto_raw = body.get("auto", body.get("autoApprove"))
        auto = None if auto_raw is None else bool(auto_raw)
        approval = self.server.store.create_approval(
            worker, kind, title, task_id=task_id, detail=detail,
            options=options, ttl_s=ttl_s, auto=auto)
        # 已经把结论给出去的（策略自动批准）不再推事件打扰训练员，
        # 只有真正等人拍板的 pending 单才发通知。
        notified = False
        if approval["status"] == "pending":
            notified = self._approval_notify(approval)
            if notified:
                self.server.store.mark_approval_notified(
                    approval["approval_id"])
                approval["notified"] = True
        log.info("approval requested id=%s worker=%s kind=%s notified=%s",
                 approval["approval_id"], worker, kind, notified)
        return 200, {
            "ok": True,
            "approval_id": approval["approval_id"],
            "status": approval["status"],
            "worker": approval["worker"],
            "task_id": approval["task_id"],
            "kind": approval["kind"],
            "expires_at": approval["expires_at"],
            "expires_at_iso": approval["expires_at_iso"],
            "remaining_s": approval["remaining_s"],
            "notified": notified,
        }

    # -- ops -------------------------------------------------------------- #

    def _ops_list(self) -> Tuple[int, Dict[str, Any]]:
        self._require_token()
        rows = []
        for name, label in OPS_SERVICES.items():
            pid = ops_pid(label)
            rows.append({"service": name, "label": label,
                         "pid": pid, "running": pid is not None})
        return 200, {"ok": True, "services": rows}

    def _ops_restart(self, name: str) -> Tuple[int, Dict[str, Any]]:
        self._require_token()
        label = OPS_SERVICES.get(name)
        if not label:
            raise NotFound()
        before = ops_pid(label)
        if label == OPS_SERVICES["router"]:
            # 自己重启自己：先把 202 发出去，再动手。
            def _later() -> None:
                time.sleep(0.5)
                ok, err = ops_kickstart(label)
                log.info("ops self-restart %s ok=%s err=%s", label, ok, err)
            threading.Thread(target=_later, daemon=True).start()
            return 202, {"ok": True, "service": name, "label": label,
                         "accepted": True, "before_pid": before,
                         "note": "router 会重启自己，本连接可能在响应后断开"}
        ok, err = ops_kickstart(label)
        if not ok:
            return 500, {"ok": False, "service": name, "label": label,
                         "error": err or "kickstart failed",
                         "before_pid": before}
        after = None
        for _ in range(40):
            time.sleep(0.25)
            after = ops_pid(label)
            if after and after != before:
                break
        log.info("ops restart %s (%s) pid %s -> %s", name, label, before, after)
        return 200, {"ok": True, "service": name, "label": label,
                     "before_pid": before, "after_pid": after,
                     "restarted": after is not None and after != before}

    @staticmethod
    def _approval_id_param(raw: str) -> str:
        approval_id = raw.strip()
        if not approval_id:
            raise BadRequest("approval id is required")
        if len(approval_id) > 128:
            raise BadRequest("approval id too long")
        return approval_id

    def _approval_get(self, raw_id: str) -> Tuple[int, Dict[str, Any]]:
        approval_id = self._approval_id_param(raw_id)
        approval = self.server.store.get_approval(approval_id)
        if approval is None:
            return 404, {"ok": False, "error": "unknown_approval",
                         "approval_id": approval_id}
        return 200, {"ok": True, "approval": approval}

    def _approvals_list(self, qs: Dict[str, List[str]]
                        ) -> Tuple[int, Dict[str, Any]]:
        worker = (qs.get("worker", [""])[0] or "").strip() or None
        status = (qs.get("status", [""])[0] or "").strip() or None
        if status is not None and status not in APPROVAL_STATUSES:
            raise BadRequest("status must be one of %s"
                             % ", ".join(APPROVAL_STATUSES))
        limit = self._int_param(qs, "limit", 50)
        if limit is None:
            limit = 50
        if limit < 1 or limit > 200:
            raise BadRequest("limit must be between 1 and 200")
        approvals = self.server.store.list_approvals(worker, status, limit)
        return 200, {"ok": True, "count": len(approvals),
                     "approvals": approvals}

    def _approval_decide(self, raw_id: str, body: Dict[str, Any]
                         ) -> Tuple[int, Dict[str, Any]]:
        approval_id = self._approval_id_param(raw_id)
        decision = str(body.get("decision") or "").strip().lower()
        # Accept the imperative spelling too; the stored value is canonical.
        if decision in ("approve", "accept"):
            decision = "approved"
        elif decision in ("deny", "decline", "reject"):
            decision = "denied"
        if decision not in APPROVAL_DECISIONS:
            raise BadRequest("decision must be approved or denied")
        note = body.get("note")
        if note is not None:
            note = str(note)
            if len(note) > 1000:
                raise BadRequest("note too long (max 1000 chars)")
        outcome, approval = self.server.store.decide_approval(
            approval_id, decision, note)
        if outcome == "unknown" or approval is None:
            return 404, {"ok": False, "error": "unknown_approval",
                         "approval_id": approval_id}
        if outcome == "expired":
            return 409, {"ok": False, "error": "approval_expired",
                         "approval_id": approval_id,
                         "status": approval["status"]}
        if outcome == "already":
            return 409, {"ok": False, "error": "already_decided",
                         "approval_id": approval_id,
                         "status": approval["status"],
                         "decision": approval["decision"]}
        log.info("approval decided id=%s decision=%s worker=%s",
                 approval_id, decision, approval["worker"])
        return 200, {
            "ok": True,
            "approval_id": approval_id,
            "status": approval["status"],
            "decision": approval["decision"],
            "note": approval["note"],
            "decided_at": approval["decided_at"],
            "decided_at_iso": approval["decided_at_iso"],
            "already": False,
        }

    def _subscriptions_pending(self, qs: Dict[str, List[str]]
                               ) -> Tuple[int, Dict[str, Any]]:
        """Pull this worker's trigger queues, ACK, return the matched events."""
        worker = (qs.get("worker", [""])[0] or "").strip()
        if not worker:
            raise BadRequest("worker is required")
        if len(worker) > 128:
            raise BadRequest("worker too long")
        limit = self._int_param(qs, "limit", 50)
        if limit is None:
            limit = 50
        if limit < 1 or limit > 200:
            raise BadRequest("limit must be between 1 and 200")

        store = self.server.store
        bridge = self.server.trigger_bridge
        subs = [sub for sub in store.list_subscriptions(worker, active_only=True)
                if sub.get("trigger_id")]
        if not subs:
            return 200, {"ok": True, "worker": worker, "count": 0,
                         "events": [], "acked": 0}

        merged: Dict[int, Dict[str, Any]] = {}
        acked = 0
        expired: List[str] = []
        errors: List[Dict[str, Any]] = []
        for sub in subs:
            name = str(sub["trigger_id"])
            since = store.get_cursor(name)
            status, data = bridge.fetch(name, since, limit)
            if status != 200:
                errors.append({"trigger_id": name, "status": status,
                               "error": data.get("error") or "trigger_error"})
                continue
            events = data.get("events") or []
            view = data.get("subscription") or {}
            if view.get("expired"):
                expired.append(name)
            elif view.get("renew_required"):
                bridge.renew(name)
            wanted = set(sub.get("kinds") or [])
            for event in events:
                if event.get("source") != sub["source"]:
                    continue
                if wanted and event.get("kind") not in wanted:
                    continue
                event = dict(event)
                event["subscription_id"] = sub["id"]
                event["trigger_id"] = name
                merged[int(event["id"])] = event
            if events:
                top = max(int(event["id"]) for event in events)
                if bridge.ack(name, top):
                    store.set_cursor(name, top)
                    acked += len(events)
                else:
                    errors.append({"trigger_id": name,
                                   "error": "ack_failed"})

        out = [merged[key] for key in sorted(merged)]
        if errors and not out:
            return 502, {"ok": False, "error": "trigger_unavailable",
                         "worker": worker, "detail": errors[:3]}
        body = {"ok": True, "worker": worker, "count": len(out),
                "events": out, "acked": acked}
        if expired:
            body["expired"] = expired
        if errors:
            body["errors"] = errors
        return 200, body

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

        store = self.server.store
        bridge = self.server.trigger_bridge
        sub = store.add_subscription(worker, source, kinds)
        sub_id = int(sub["id"])
        # First live subscription of a generation gets the plain name; later
        # ones must not share a trigger queue, so they carry the row id.
        only_one = store.active_subscriptions(worker) == 1
        base_name = "router:%s" % worker
        alt_name = "router:%s:%d" % (worker, sub_id)
        name = base_name if only_one else alt_name

        baseline = bridge.last_event_id()
        if baseline is None:
            store.delete_subscription(sub_id)
            return 502, {"ok": False, "error": "trigger_unavailable",
                         "worker": worker,
                         "detail": "trigger /bus/health unavailable"}
        ok, detail = bridge.open(name, baseline)
        if not ok and name != alt_name:
            # trigger 0.7.x cannot reopen a closed name: use a fresh one.
            name = alt_name
            ok, detail = bridge.open(name, baseline)
        if not ok:
            store.delete_subscription(sub_id)
            log.warning("subscription create failed worker=%s source=%s: %.300s",
                        worker, source, detail)
            return 502, {"ok": False, "error": "trigger_unavailable",
                         "worker": worker, "source": source,
                         "detail": detail}
        store.set_subscription_trigger(sub_id, name, baseline)
        store.set_cursor(name, baseline)
        sub = store.get_subscription(sub_id) or sub
        log.info("subscription id=%d worker=%s source=%s kinds=%s"
                 " trigger=%s since=%d", sub_id, worker, source,
                 kinds or "all", name, baseline)
        return 201, {
            "ok": True, "id": sub_id, "worker": worker, "source": source,
            "kinds": kinds, "active": True,
            "trigger_forwarded": True, "trigger_id": name,
            "trigger_since": baseline,
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
        closed = False
        if sub.get("trigger_id"):
            closed = self.server.trigger_bridge.close(sub["trigger_id"])
        self.server.store.delete_subscription(sub_id)
        if sub.get("trigger_id"):
            self.server.store.delete_cursor(sub["trigger_id"])
        log.info("subscription deleted id=%d trigger=%s trigger_closed=%s",
                 sub_id, sub.get("trigger_id"), closed)
        return 200, {"ok": True, "id": sub_id, "active": False,
                     "deleted": True, "trigger_id": sub.get("trigger_id"),
                     "trigger_closed": closed}

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
                if item["event"] == "worker_message":
                    with self.server.dispatch_lock:
                        conn.peer_buffered -= 1
                        pending = self.server.store.get_peer_message(
                            item["data"]["message_id"])
                        if conn.peer_buffered == 0:
                            self.server.flush_peer_messages_locked(conn)
                    # An HTTP inbox consumer may have ACKed a prefetched row.
                    if pending is None or pending["status"] != "queued":
                        continue
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
                    "for Hermes. Loopback by default, stdlib only.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="listen port (default %(default)s)")
    p.add_argument("--host", default=HOST,
                   help="address to bind (default %(default)s)")
    p.add_argument("--allow-remote", action="store_true",
                   help="allow non-loopback binding; token auth remains required")
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
    if host not in ("127.0.0.1", "localhost") and not args.allow_remote:
        print("error: non-loopback binding requires --allow-remote; refusing to bind %r"
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
    trigger_client = TriggerClient(args.trigger_url, args.trigger_token_file)
    notifier = TriggerNotifier(trigger_client)
    bridge = TriggerSubscriptionBridge(trigger_client)
    stale_subs = store.purge_all_subscriptions()
    for sub in stale_subs:
        if sub.get("trigger_id"):
            bridge.close(sub["trigger_id"])
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
    log.info("subscription forwarding -> %s (cursor bus: /events + /ack)",
             trigger_client.base_url)
    sweeper = ApprovalSweeper(store)
    sweeper.start()
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
        sweeper.stop()
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
