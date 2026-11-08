#!/usr/bin/env python3
"""mowu-trigger — pure inbox service for Hermes.

A single long-lived, loopback-only HTTP service that does exactly four
things:

  * accepts events from anything that can POST (``POST /trigger``),
  * applies the cross-source L2 rules (``~/.hermes/trigger-rules.json``:
    downgrade / rate_limit / dedupe / quiet), hot-reloaded on mtime,
  * persists every event once in SQLite (dedupe by ``event_id``) and keeps
    blocked ones with ``disposition='dropped'``,
  * serves unread events to subscribers with cursor/ack semantics and
    long-polling (``GET /events``, ``POST /ack``).

It knows **nothing** about how a source talks. What it does read is each
source's *declaration* (``sources/<name>/source.json``): the envelope fields
that source promises (``schema``), its priority/wake policy, the dedupe field
list its client uses, and ``health.max_silence_s``. From that the server only
lists (``GET /sources``), validates those fields on ingest and judges
staleness. There are no adapters, no per-source branches: every source ships
its own client (connect → normalise to an envelope → L1 business filter →
POST), so adding a source is a directory + a declaration + a client script,
never a change here. Undeclared sources are accepted and merely logged, so a
missing declaration can never cut a feed.

L1 (client-side) only knows this source's own business semantics (an MC client
does not echo its own chat, an email client reads one folder). L2 (this file)
only knows envelope fields and ``thread_key`` — never a protocol.

Every event carries a ``level``, one of four tiers (highest first):

  ``event``   interrupt now — delivered, injected, and inserted mid-turn;
  ``notify``  next turn — delivered and injected at the next turn boundary;
  ``info``    worker-only — delivered to worker subscriptions, never injected;
  ``log``     archive-only (never queued, never streamed, never delivered).

``super_event`` and ``super`` are accepted on input as aliases for ``event``;
they are never stored or echoed.


The companion Hermes plugin (``~/.hermes/plugins/trigger-bus``) is the only
intended consumer of ``GET /events``; Feishu private chat keeps using the
native Hermes channel and never goes through this service.

Standard library only. Binds 127.0.0.1 on purpose — never 0.0.0.0.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import select
import signal
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from buscore import (DEFAULT_DROP_LOG, QUEUE_MAX, LeaseWatch, SubscriptionManager,
                     parse_drop_since, pump, read_drops, subscription_snapshot)

VERSION = "0.7.0"
HOST = "127.0.0.1"
DEFAULT_PORT = 8791
DEFAULT_DB = "~/.hermes/trigger.db"
DEFAULT_RULES_FILE = "~/.hermes/trigger-rules.json"
DEFAULT_BLOCKED_LOG = "~/.hermes/trigger-blocked.jsonl"
BLOCKED_TEXT_CHARS = 120
LEVELS = ("event", "notify", "info", "log")
DEFAULT_LEVEL = "info"
# Legacy input names, folded to the top level before validation/storage.
LEVEL_ALIASES = {"super": "event", "super_event": "event"}
LEVEL_DEFAULT_PRIORITY = {"event": 0, "notify": 0, "info": 2, "log": 3}
WAKE_LEVELS = ("event", "notify")
DELIVERY_LEVELS = ("event", "notify", "info")  # log is archive-only
WAKE_PRIORITY_MAX = 0  # priority <= this is a wake (matches trigger-bus default)
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
MAX_BODY_BYTES = 1 * 1024 * 1024
DEFAULT_SUBSCRIPTION_TTL = 3600
DEFAULT_RENEW_WINDOW = 600
SUBSCRIPTION_GRACE = 24 * 3600  # expired records stay renewable this long
JANITOR_INTERVAL = 3600.0

HERE = Path(__file__).resolve().parent
DEFAULT_LOG = HERE / "trigger-server.log"
DEFAULT_SOURCES_DIR = HERE / "sources"
DEFAULT_TOKEN_PATH = "~/.hermes/secrets/trigger.token"
DEFAULT_ALLOW_CIDR = "192.168.3.0/24"
SSE_WAIT_SECONDS = 15.0
SSE_PING = b": ping\n\n"

log = logging.getLogger("trigger")


def canonical_level(value: Any) -> str:
    """Normalise a level for storage/echo; legacy names fold to ``event``."""
    level = str(value or DEFAULT_LEVEL).strip().lower()
    return LEVEL_ALIASES.get(level, level)


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #

def setup_logging(log_path: Path, verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            str(log_path), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        fh.setLevel(logging.DEBUG)
        log.addHandler(fh)
    except OSError as exc:  # cannot open log file -> keep running, stderr only
        print("warning: cannot open log file %s: %s" % (log_path, exc),
              file=sys.stderr)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    sh.setLevel(logging.INFO if verbose else logging.WARNING)
    log.addHandler(sh)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT UNIQUE NOT NULL,
    source       TEXT NOT NULL,
    kind         TEXT,
    level        TEXT DEFAULT 'info',
    priority     INTEGER,
    actor        TEXT,
    text         TEXT NOT NULL,
    payload_json TEXT,
    ts           TEXT,
    dedupe_key   TEXT,
    disposition  TEXT DEFAULT 'delivered',
    created_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS cursors (
    subscriber TEXT PRIMARY KEY,
    last_id    INTEGER NOT NULL DEFAULT 0,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS receipts (
    event_id   TEXT NOT NULL,
    subscriber TEXT NOT NULL,
    read_at    REAL,
    PRIMARY KEY (event_id, subscriber)
);
CREATE INDEX IF NOT EXISTS idx_receipts_sub ON receipts(subscriber);

CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT,
    match_json TEXT,
    action     TEXT,
    priority   INTEGER,
    enabled    INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS subscriptions (
    name        TEXT PRIMARY KEY,
    since       INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    renew_count INTEGER NOT NULL DEFAULT 0,
    last_seen   REAL,
    status      TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
"""


class TriggerStore:
    """SQLite-backed event store. One connection + one lock (low volume)."""

    def __init__(self, db_path: str):
        self.path = os.path.abspath(os.path.expanduser(db_path))
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._conn = sqlite3.connect(self.path, timeout=10,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None  # autocommit; explicit BEGIN where needed
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        log.info("store ready: %s", self.path)

    def _migrate(self) -> None:
        """Idempotent in-place upgrades: no framework, only pragma + ALTER."""
        cols = {str(row["name"]) for row in
                self._conn.execute("PRAGMA table_info(events)").fetchall()}
        if "dedupe_key" not in cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN dedupe_key TEXT")
            log.info("store migration: added events.dedupe_key")
            cur = self._conn.execute(
                "UPDATE events SET dedupe_key = source || ':' || event_id"
                " WHERE dedupe_key IS NULL")
            log.info("store migration: backfilled %d dedupe_key value(s)",
                     int(cur.rowcount or 0))
        if "disposition" not in cols:
            # Existing rows become 'delivered' through the column default;
            # no UPDATE is issued, so history is never rewritten.
            self._conn.execute(
                "ALTER TABLE events ADD COLUMN disposition TEXT"
                " DEFAULT 'delivered'")
            log.info("store migration: added events.disposition")
        if "level" not in cols:
            # Old events are ordinary traffic: 'info'.
            self._conn.execute(
                "ALTER TABLE events ADD COLUMN level TEXT DEFAULT 'info'")
            log.info("store migration: added events.level")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe"
            " ON events(dedupe_key, source)")

    # -- writes --------------------------------------------------------- #
    def add_event(self, source: str, text: str, kind: str = "event",
                  level: str = DEFAULT_LEVEL,
                  priority: int = 3, actor: str = "",
                  payload: Optional[Any] = None,
                  event_id: Optional[str] = None,
                  ts: Optional[str] = None,
                  dedupe_key: Optional[str] = None,
                  disposition: str = "delivered") -> Dict[str, Any]:
        """Single ingest path: every adapter and POST /trigger ends up here."""
        ts = ts or now_iso()
        if not event_id:
            event_id = hashlib.sha256(
                ("%s%s%s" % (source, text, ts)).encode("utf-8")).hexdigest()
        dedupe_key = (str(dedupe_key).strip() if dedupe_key is not None else "")
        if not dedupe_key:
            dedupe_key = "%s:%s" % (source, event_id)
        if disposition not in ("delivered", "downgraded", "dropped"):
            disposition = "delivered"
        level = canonical_level(level)
        if level not in LEVELS:
            level = DEFAULT_LEVEL
        payload_json = (json.dumps(payload, ensure_ascii=False)
                        if payload is not None else None)
        created_at = time.time()
        with self._cond:
            try:
                cur = self._conn.execute(
                    "INSERT INTO events (event_id, source, kind, level,"
                    " priority, actor, text, payload_json, ts, dedupe_key,"
                    " disposition, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_id, source, kind, level, int(priority), actor, text,
                     payload_json, ts, dedupe_key, disposition, created_at),
                )
                row_id = int(cur.lastrowid)
                self._cond.notify_all()
                log.info("event #%d source=%s kind=%s level=%s actor=%r"
                         " chars=%d dedupe=%r disposition=%s",
                         row_id, source, kind, level, actor, len(text),
                         dedupe_key, disposition)
                return {"ok": True, "id": row_id, "event_id": event_id,
                        "dedupe_key": dedupe_key, "duplicate": False,
                        "level": level, "disposition": disposition,
                        "wake": (level in WAKE_LEVELS and disposition != "dropped"
                                 and int(priority) <= WAKE_PRIORITY_MAX)}
            except sqlite3.IntegrityError:
                # Either event_id or (dedupe_key, source) collided; report the
                # row that already exists so a retry is idempotent either way.
                row = self._conn.execute(
                    "SELECT id, event_id, dedupe_key, level, priority,"
                    " disposition FROM events WHERE event_id=?",
                    (event_id,)).fetchone()
                if row is None:
                    row = self._conn.execute(
                        "SELECT id, event_id, dedupe_key, level, priority,"
                        " disposition FROM events"
                        " WHERE dedupe_key=? AND source=?",
                        (dedupe_key, source)).fetchone()
                if row is None:  # extremely unlikely: unique violation elsewhere
                    raise
                log.debug("duplicate source=%s event_id=%s dedupe_key=%s"
                          " (existing #%d)", source, event_id, dedupe_key,
                          row["id"])
                return {"ok": True, "id": int(row["id"]),
                        "event_id": row["event_id"],
                        "dedupe_key": row["dedupe_key"], "duplicate": True,
                        "level": row["level"] or DEFAULT_LEVEL,
                        "disposition": row["disposition"] or "delivered",
                        "wake": ((row["level"] or DEFAULT_LEVEL) in WAKE_LEVELS
                                 and (row["disposition"] or "delivered")
                                 != "dropped"
                                 and row["priority"] is not None
                                 and int(row["priority"]) <= WAKE_PRIORITY_MAX)}

    def ack(self, subscriber: str, up_to_id: int) -> Dict[str, Any]:
        up_to_id = max(0, int(up_to_id))
        with self._cond:
            row = self._conn.execute(
                "SELECT last_id FROM cursors WHERE subscriber=?",
                (subscriber,)).fetchone()
            previous = int(row["last_id"]) if row else 0
            new_last = max(previous, up_to_id)
            acked = 0
            if new_last > previous:
                rows = self._conn.execute(
                    "SELECT event_id FROM events WHERE id > ? AND id <= ?",
                    (previous, new_last)).fetchall()
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for r in rows:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO receipts"
                            " (event_id, subscriber, read_at) VALUES (?, ?, ?)",
                            (r["event_id"], subscriber, time.time()))
                    self._conn.execute(
                        "INSERT INTO cursors (subscriber, last_id, updated_at)"
                        " VALUES (?, ?, ?)"
                        " ON CONFLICT(subscriber) DO UPDATE SET"
                        " last_id=excluded.last_id, updated_at=excluded.updated_at",
                        (subscriber, new_last, time.time()))
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                acked = len(rows)
            if new_last != previous or row is None:
                log.info("ack subscriber=%s %d -> %d (%d receipts)",
                         subscriber, previous, new_last, acked)
            return {"ok": True, "subscriber": subscriber,
                    "last_id": new_last, "acked": acked}

    # -- subscriptions: budget cycle, not connection liveness ------------ #
    @staticmethod
    def view_of(row: sqlite3.Row, renew_window: float) -> Dict[str, Any]:
        return subscription_snapshot(
            row["name"], row["expires_at"], row["renew_count"],
            row["status"], renew_window, now=time.time())

    def get_subscription(self, name: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM subscriptions WHERE name=?", (name,)).fetchone()

    def ensure_subscription(self, name: str, since: int,
                            ttl: float) -> sqlite3.Row:
        """Create on first sight; always refresh ``last_seen``.

        A closed record is never resurrected here — otherwise a worker whose
        subscription was cancelled would silently come back on its next poll.
        """
        now = time.time()
        with self._cond:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE name=?", (name,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO subscriptions (name, since, created_at,"
                    " expires_at, renew_count, last_seen, status)"
                    " VALUES (?, ?, ?, ?, 0, ?, 'active')",
                    (name, max(0, int(since)), now, now + float(ttl), now))
                log.info("subscription created name=%s ttl=%.0fs", name, ttl)
            else:
                self._conn.execute(
                    "UPDATE subscriptions SET last_seen=? WHERE name=?",
                    (now, name))
            return self._conn.execute(
                "SELECT * FROM subscriptions WHERE name=?", (name,)).fetchone()

    def subscription_view(self, name: str,
                          renew_window: float) -> Optional[Dict[str, Any]]:
        row = self.get_subscription(name)
        return self.view_of(row, renew_window) if row else None

    def renew_subscription(self, name: str,
                           ttl: float) -> Optional[sqlite3.Row]:
        """Extend an *active* record. Returns None for unknown names.

        A record whose ``status`` is closed/expired comes back unchanged; the
        caller turns that into 410 (it exists, but is not renewable).
        """
        now = time.time()
        with self._cond:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE name=?", (name,)).fetchone()
            if row is None:
                return None
            if row["status"] != "active":
                return row
            self._conn.execute(
                "UPDATE subscriptions SET expires_at=?, renew_count=renew_count+1,"
                " last_seen=? WHERE name=?",
                (now + float(ttl), now, name))
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE name=?", (name,)).fetchone()
            log.info("subscription renewed name=%s renew_count=%d expires_in=%.0fs",
                     name, int(row["renew_count"]), float(ttl))
            return row

    def close_subscription(self, name: str) -> Optional[sqlite3.Row]:
        with self._cond:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE name=?", (name,)).fetchone()
            if row is None:
                return None
            if row["status"] != "closed":
                self._conn.execute(
                    "UPDATE subscriptions SET status='closed' WHERE name=?",
                    (name,))
                log.info("subscription closed name=%s", name)
            return self.get_subscription(name)

    def list_subscriptions(self, renew_window: float) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subscriptions ORDER BY name").fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            entry = self.view_of(row, renew_window)
            entry["since"] = int(row["since"])
            entry["created_at"] = row["created_at"]
            entry["expires_at"] = row["expires_at"]
            entry["last_seen"] = row["last_seen"]
            out.append(entry)
        return out

    def subscription_counts(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS c FROM subscriptions"
                " GROUP BY status").fetchall()
        counts = {"active": 0, "expired": 0, "closed": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["c"])
        counts["total"] = sum(int(row["c"]) for row in rows)
        return counts

    def expire_stale_subscriptions(self,
                                   grace: float = SUBSCRIPTION_GRACE) -> int:
        cutoff = time.time() - float(grace)
        with self._cond:
            cur = self._conn.execute(
                "UPDATE subscriptions SET status='expired'"
                " WHERE status='active' AND expires_at < ?", (cutoff,))
            changed = int(cur.rowcount or 0)
        if changed:
            log.info("subscription janitor: %d record(s) past the %.0fh grace "
                     "window marked expired", changed, grace / 3600.0)
        return changed

    # -- reads ---------------------------------------------------------- #
    def _unread_rows(self, subscriber: str, since: int, limit: int,
                     levels: Optional[Any] = None):
        wanted = sorted(set(levels) if levels else set(DELIVERY_LEVELS))
        marks = ",".join("?" for _ in wanted)
        sql = ("SELECT e.* FROM events e"
               " WHERE e.id > ?"
               "   AND COALESCE(e.disposition, 'delivered') != 'dropped'"
               "   AND COALESCE(e.level, 'info') IN (%s)" % marks)
        params: List[Any] = [since] + wanted
        sql += ("   AND NOT EXISTS (SELECT 1 FROM receipts r"
                "                   WHERE r.event_id=e.event_id"
                "                     AND r.subscriber=?)"
                " ORDER BY e.id ASC LIMIT ?")
        params.extend([subscriber, limit])
        return self._conn.execute(sql, params).fetchall()

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            payload = (json.loads(row["payload_json"])
                       if row["payload_json"] else None)
        except (ValueError, TypeError):
            payload = None
        return {
            "id": int(row["id"]),
            "event_id": row["event_id"],
            "source": row["source"],
            "kind": row["kind"],
            "priority": row["priority"],
            "actor": row["actor"],
            "text": row["text"],
            "payload": payload,
            "ts": row["ts"],
            "level": row["level"] or DEFAULT_LEVEL,
            "dedupe_key": row["dedupe_key"],
            "disposition": row["disposition"] or "delivered",
            "wake": ((row["level"] or DEFAULT_LEVEL) in WAKE_LEVELS
                     and (row["disposition"] or "delivered") != "dropped"
                     and row["priority"] is not None
                     and int(row["priority"]) <= WAKE_PRIORITY_MAX),
            "created_at": row["created_at"],
        }

    def list_events(self, subscriber: str, since: Optional[int],
                    limit: int = 50, wait: float = 0.0,
                    levels: Optional[Any] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._cond:
            if since is None:
                row = self._conn.execute(
                    "SELECT last_id FROM cursors WHERE subscriber=?",
                    (subscriber,)).fetchone()
                cursor = int(row["last_id"]) if row else 0
            else:
                cursor = max(0, int(since))
            rows = self._unread_rows(subscriber, cursor, limit, levels)
            if rows or wait <= 0:
                return [self._row_to_event(r) for r in rows]
            deadline = time.monotonic() + min(float(wait), 300.0)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)
                rows = self._unread_rows(subscriber, cursor, limit, levels)
                if rows:
                    return [self._row_to_event(r) for r in rows]

    def get_event(self, row_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE id=?", (int(row_id),)).fetchone()
        return self._row_to_event(row) if row else None

    def recent_since(self, since: int, limit: int = 200,
                     levels: Optional[Any] = None
                     ) -> List[Dict[str, Any]]:
        """Newest ``limit`` events with id > since, returned oldest-first."""
        limit = max(1, int(limit))
        wanted = sorted(set(levels) if levels else set(DELIVERY_LEVELS))
        marks = ",".join("?" for _ in wanted)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM events WHERE id > ?"
                " AND COALESCE(disposition, 'delivered') != 'dropped'"
                " AND COALESCE(level, 'info') IN (%s)"
                " ORDER BY id DESC LIMIT ?) ORDER BY id ASC" % marks,
                tuple([max(0, int(since))] + wanted + [limit])).fetchall()
        return [self._row_to_event(r) for r in rows]

    def max_id(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
            return int(row["m"])

    def count_since(self, since: int, levels: Optional[Any] = None) -> int:
        """Visible events with id > since (gap detection with level filters)."""
        wanted = sorted(set(levels) if levels else set(DELIVERY_LEVELS))
        marks = ",".join("?" for _ in wanted)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE id > ?"
                " AND COALESCE(disposition, 'delivered') != 'dropped'"
                " AND COALESCE(level, 'info') IN (%s)" % marks,
                tuple([max(0, int(since))] + wanted)).fetchone()
        return int(row["c"])

    def cursor_of(self, subscriber: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_id FROM cursors WHERE subscriber=?",
                (subscriber,)).fetchone()
            return int(row["last_id"]) if row else 0

    def tail_events(self, since: int, limit: int = 100,
                    wait: float = 0.0,
                    levels: Optional[Any] = None
                    ) -> List[Dict[str, Any]]:
        """Id-ordered tail for SSE subscribers (no receipt filtering)."""
        limit = max(1, min(int(limit), 500))
        since = max(0, int(since))
        wanted = sorted(set(levels) if levels else set(DELIVERY_LEVELS))
        marks = ",".join("?" for _ in wanted)

        def query():
            return self._conn.execute(
                "SELECT * FROM events WHERE id > ?"
                " AND COALESCE(disposition, 'delivered') != 'dropped'"
                " AND COALESCE(level, 'info') IN (%s)"
                " ORDER BY id ASC LIMIT ?" % marks,
                tuple([since] + wanted + [limit])).fetchall()

        with self._cond:
            rows = query()
            if rows or wait <= 0:
                return [self._row_to_event(r) for r in rows]
            deadline = time.monotonic() + min(float(wait), 300.0)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)
                rows = query()
                if rows:
                    return [self._row_to_event(r) for r in rows]

    def stats(self, subscriber: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_id FROM cursors WHERE subscriber=?",
                (subscriber,)).fetchone()
            cursor = int(row["last_id"]) if row else 0
            unread = self._conn.execute(
                "SELECT COUNT(*) AS c FROM events e WHERE e.id > ?"
                " AND COALESCE(e.disposition, 'delivered') != 'dropped'"
                " AND NOT EXISTS (SELECT 1 FROM receipts r"
                "                 WHERE r.event_id=e.event_id"
                "                   AND r.subscriber=?)",
                (cursor, subscriber)).fetchone()["c"]
            return {"unread": int(unread), "last_id": cursor}

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) AS c FROM events").fetchone()["c"])

    def level_counts(self) -> Dict[str, int]:
        """Visible (non-dropped) events per level; every known level is present."""
        counts = {level: 0 for level in LEVELS}
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(level, 'info') AS lvl, COUNT(*) AS c"
                " FROM events"
                " WHERE COALESCE(disposition, 'delivered') != 'dropped'"
                " GROUP BY lvl").fetchall()
        for row in rows:
            counts[str(row["lvl"])] = int(row["c"])
        return counts

    def recent_events(self, since_epoch: float,
                      limit: int = 20000) -> List[Dict[str, Any]]:
        """Rows created since ``since_epoch``, oldest first (rule backfill)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE created_at >= ?"
                " ORDER BY id ASC LIMIT ?",
                (float(since_epoch), max(1, int(limit)))).fetchall()
        return [self._row_to_event(r) for r in rows]

    def last_event_ts_by_source(self) -> Dict[str, Dict[str, Any]]:
        """Newest ``ts`` per source, with its ``created_at`` as a fallback.

        In SQLite a bare column next to MAX() comes from the row that won,
        so ``created_at`` belongs to the newest row even when ``ts`` is junk.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, MAX(ts) AS ts, created_at FROM events"
                " GROUP BY source").fetchall()
        return {str(r["source"]): {"ts": r["ts"], "created_at": r["created_at"]}
                for r in rows}

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# L2 rule engine (cross-source, config file, hot reload)
# --------------------------------------------------------------------------- #

RULE_ACTIONS = ("downgrade", "rate_limit", "dedupe", "quiet")
RULE_MATCH_KEYS = ("source", "kind", "actor_regex", "text_regex",
                   "thread_key_regex", "priority_max")
RULE_FINGERPRINT_FIELDS = ("source", "kind", "actor", "text", "thread_key",
                           "subject")


def _glob_match(pattern: str, value: str) -> bool:
    """Case-insensitive glob (``*`` / ``mc_*``); no regex here."""
    return fnmatch.fnmatchcase(str(value).lower(), str(pattern).lower())


class RuleEngine:
    """L2 filtering: one JSON file, one engine, every source.

    Rules are evaluated in file order against an envelope
    ``{source, kind, level, actor, text, priority, thread_key, subject}``.  A
    rule whose ``match`` has no fields matches everything.  A blocking
    outcome (rate_limit overflow or dedupe window) stops evaluation;
    ``downgrade`` / ``quiet`` may lower the priority and let the next rule
    see the new value.

    The file is re-read on the next ``POST /trigger`` whenever its mtime
    changed.  A broken file never takes the service down: the previous rules
    stay active and one WARNING is logged (the bad mtime is remembered so the
    same broken file is not re-parsed on every request).
    """

    def __init__(self, path: Any, blocked_path: Any,
                 store: Optional[TriggerStore] = None) -> None:
        self.path = Path(os.path.expanduser(str(path)))
        self.blocked_path = Path(os.path.expanduser(str(blocked_path)))
        self.store = store
        self._lock = threading.RLock()
        self._rules: List[Dict[str, Any]] = []
        self._mtime_ns: Optional[int] = None
        self._mtime_iso: Optional[str] = None
        self._last_error = ""
        self._rate_state: Dict[int, List[float]] = {}
        self._dedupe_state: Dict[Tuple[int, str], float] = {}
        self._quiet_state: Dict[Tuple[int, str], float] = {}
        self.reload(force=True)

    # -- loading -------------------------------------------------------- #
    def reload(self, force: bool = False) -> bool:
        """Re-read the rule file when its mtime changed. Never raises."""
        with self._lock:
            try:
                stat = self.path.stat()
            except FileNotFoundError:
                stat = None
            except OSError as exc:
                self._last_error = "stat failed: %s" % exc
                log.warning("rules: cannot stat %s (%s); keeping %d rule(s)",
                            self.path, exc, len(self._rules))
                return False

            mtime_ns = stat.st_mtime_ns if stat is not None else None
            if not force and mtime_ns == self._mtime_ns:
                return False

            if stat is None:
                had_rules = bool(self._rules)
                self._rules = []
                self._mtime_ns = None
                self._mtime_iso = None
                self._last_error = ""
                self._reset_state()
                if had_rules or force:
                    log.info("rules: %s not found; 0 rules loaded "
                             "(every event passes)", self.path)
                return True

            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                rules = self._compile_file(raw)
            except (OSError, ValueError, TypeError) as exc:
                # Keep the rules that were working; remember the bad mtime so
                # the same broken file is not re-parsed on every POST.
                self._mtime_ns = mtime_ns
                self._last_error = str(exc)
                log.warning("rules: cannot load %s (%s); keeping %d previous "
                            "rule(s)", self.path, exc, len(self._rules))
                return False

            self._rules = rules
            self._mtime_ns = mtime_ns
            self._mtime_iso = datetime.fromtimestamp(
                stat.st_mtime).astimezone().isoformat(timespec="seconds")
            self._last_error = ""
            self._reset_state()
            backfilled = self._backfill_rate()
            log.info("rules: loaded %d rule(s) from %s (mtime=%s, "
                     "rate state backfilled=%d)",
                     len(rules), self.path, self._mtime_iso, backfilled)
            return True

    def _reset_state(self) -> None:
        self._rate_state = {}
        self._dedupe_state = {}
        self._quiet_state = {}

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("%s must be a positive integer" % label)
        return int(value)

    @staticmethod
    def _positive_float(value: Any, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("%s must be a positive number" % label)
        value = float(value)
        if value <= 0:
            raise ValueError("%s must be a positive number" % label)
        return value

    @staticmethod
    def _int_value(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("%s must be an integer" % label)
        return int(value)

    @staticmethod
    def _by_fields(raw: Any) -> Tuple[str, ...]:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("action.by must be a non-empty string")
        fields = tuple(part.strip() for part in re.split(r"[+,]", raw)
                       if part.strip())
        if not fields:
            raise ValueError("action.by must name at least one field")
        unknown = [f for f in fields if f not in RULE_FINGERPRINT_FIELDS]
        if unknown:
            raise ValueError("action.by has unknown field(s): %s"
                             % ", ".join(unknown))
        return fields

    def _compile_file(self, raw: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw, dict):
            raise ValueError("root must be a JSON object")
        version = raw.get("version")
        if version is not None and version != 1:
            log.warning("rules: unknown version %r (expected 1); loading anyway",
                        version)
        items = raw.get("rules")
        if not isinstance(items, list):
            raise ValueError("rules must be a list")
        compiled: List[Dict[str, Any]] = []
        for index, item in enumerate(items):
            try:
                compiled.append(self._compile_rule(item, index))
            except (ValueError, TypeError, re.error) as exc:
                log.warning("rules: skipping rule #%d: %s", index + 1, exc)
        return compiled

    def _compile_rule(self, raw: Any, index: int) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("rule must be an object")
        name = str(raw.get("name") or "").strip() or "rule#%d" % (index + 1)
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        match_raw = raw.get("match") or {}
        if not isinstance(match_raw, dict):
            raise ValueError("match must be an object")
        unknown = sorted(set(match_raw) - set(RULE_MATCH_KEYS))
        if unknown:
            raise ValueError("unknown match field(s): %s" % ", ".join(unknown))

        match: Dict[str, Any] = {}
        for key in ("source", "kind"):
            value = match_raw.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError("match.%s must be a string" % key)
            value = value.strip()
            if not value:
                continue
            match[key] = value
        for key, slot in (("actor_regex", "actor_re"),
                          ("text_regex", "text_re"),
                          ("thread_key_regex", "thread_re")):
            pattern = match_raw.get(key)
            if pattern is None or pattern == "":
                continue
            if not isinstance(pattern, str):
                raise ValueError("match.%s must be a string" % key)
            if pattern.strip() == "*":
                continue  # explicit wildcard == no constraint
            match[slot] = re.compile(pattern, re.IGNORECASE)
        if match_raw.get("priority_max") is not None:
            match["priority_max"] = self._int_value(
                match_raw["priority_max"], "match.priority_max")

        action_raw = raw.get("action") or {}
        if not isinstance(action_raw, dict):
            raise ValueError("action must be an object")
        action_type = str(action_raw.get("type") or "").strip().lower()
        if action_type not in RULE_ACTIONS:
            raise ValueError("action.type must be one of %s"
                             % ", ".join(RULE_ACTIONS))
        action: Dict[str, Any] = {"type": action_type}
        if action_type == "downgrade":
            action["to"] = self._int_value(action_raw.get("to", 3),
                                           "action.to")
        elif action_type == "rate_limit":
            action["max"] = self._positive_int(action_raw.get("max"),
                                               "action.max")
            action["window_s"] = self._positive_float(
                action_raw.get("window_s"), "action.window_s")
            on_exceed = str(action_raw.get("on_exceed") or "drop").lower()
            if on_exceed not in ("drop", "downgrade"):
                raise ValueError("action.on_exceed must be drop or downgrade")
            action["on_exceed"] = on_exceed
            action["to"] = self._int_value(action_raw.get("to", 3),
                                           "action.to")
        elif action_type == "dedupe":
            action["window_s"] = self._positive_float(
                action_raw.get("window_s"), "action.window_s")
            action["by"] = self._by_fields(action_raw.get("by") or "text")
        elif action_type == "quiet":
            action["window_s"] = self._positive_float(
                action_raw.get("window_s"), "action.window_s")
            action["by"] = self._by_fields(
                action_raw.get("by") or "thread_key")
            effect = str(action_raw.get("effect") or "downgrade").lower()
            if effect != "downgrade":
                raise ValueError("action.effect must be downgrade "
                                 "(quiet never drops)")
            action["to"] = self._int_value(action_raw.get("to", 2),
                                           "action.to")
        return {"id": index, "name": name, "enabled": enabled,
                "match": match, "action": action}

    # -- matching / fingerprints ---------------------------------------- #
    @staticmethod
    def _matches(rule: Dict[str, Any], env: Dict[str, Any],
                 priority: int) -> bool:
        match = rule["match"]
        if "source" in match and not _glob_match(
                match["source"], str(env.get("source") or "")):
            return False
        if "kind" in match and not _glob_match(
                match["kind"], str(env.get("kind") or "")):
            return False
        if "actor_re" in match and not match["actor_re"].search(
                str(env.get("actor") or "")):
            return False
        if "text_re" in match and not match["text_re"].search(
                str(env.get("text") or "")):
            return False
        if "thread_re" in match and not match["thread_re"].search(
                str(env.get("thread_key") or "")):
            return False
        if "priority_max" in match and priority > match["priority_max"]:
            return False
        return True

    @staticmethod
    def _fingerprint(fields: Tuple[str, ...],
                     env: Dict[str, Any]) -> Optional[str]:
        if "thread_key" in fields and not str(
                env.get("thread_key") or "").strip():
            # A thread-scoped rule cannot fire on a message that has no
            # thread: an empty key must not collapse every event into one.
            return None
        parts = [str(env.get(field) or "") for field in fields]
        return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()

    # -- evaluation ----------------------------------------------------- #
    def evaluate(self, env: Dict[str, Any]) -> Dict[str, Any]:
        """Return disposition/priority/matched rules for one envelope."""
        self.reload()
        with self._lock:
            priority = int(env.get("priority", 3))
            original = priority
            matched: List[str] = []
            blocked: Optional[Dict[str, Any]] = None
            reason = ""
            for rule in self._rules:
                if not rule["enabled"]:
                    continue
                if not self._matches(rule, env, priority):
                    continue
                matched.append(rule["name"])
                action = rule["action"]
                kind = action["type"]

                if kind == "downgrade":
                    new_priority = action["to"]
                    if new_priority != priority:
                        log.info("rule=%s downgrade source=%s actor=%r "
                                 "priority %d -> %d", rule["name"],
                                 env.get("source"), env.get("actor"),
                                 priority, new_priority)
                        priority = new_priority
                    continue

                if kind == "rate_limit":
                    now = time.time()
                    hits = self._rate_state.setdefault(rule["id"], [])
                    cutoff = now - action["window_s"]
                    if hits and hits[0] < cutoff:
                        hits[:] = [hit for hit in hits if hit >= cutoff]
                    if len(hits) >= action["max"]:
                        if action["on_exceed"] == "downgrade":
                            if action["to"] != priority:
                                log.info("rule=%s rate exceeded; downgrade "
                                         "priority %d -> %d", rule["name"],
                                         priority, action["to"])
                                priority = action["to"]
                        else:
                            blocked, reason = rule, "rate_limit"
                            break
                    else:
                        hits.append(now)
                    continue

                if kind == "dedupe":
                    fingerprint = self._fingerprint(action["by"], env)
                    if fingerprint is None:
                        continue
                    key = (rule["id"], fingerprint)
                    now = time.time()
                    seen = self._dedupe_state.get(key)
                    if seen is not None and now - seen < action["window_s"]:
                        blocked, reason = rule, "dedupe_window"
                        break
                    self._dedupe_state[key] = now
                    continue

                if kind == "quiet":
                    fingerprint = self._fingerprint(action["by"], env)
                    if fingerprint is None:
                        continue
                    key = (rule["id"], fingerprint)
                    now = time.time()
                    last_woken = self._quiet_state.get(key)
                    if last_woken is not None and \
                            now - last_woken < action["window_s"]:
                        if action["to"] != priority:
                            log.info("rule=%s thread already woke at %.0f; "
                                     "downgrade priority %d -> %d",
                                     rule["name"], last_woken, priority,
                                     action["to"])
                            priority = action["to"]
                    elif (priority <= WAKE_PRIORITY_MAX
                          and str(env.get("level") or DEFAULT_LEVEL)
                          in WAKE_LEVELS):
                        # Only an event/notify message that actually wakes
                        # starts the quiet window; info never wakes.
                        self._quiet_state[key] = now

            if blocked is not None:
                disposition = "dropped"
            elif priority != original:
                disposition = "downgraded"
            else:
                disposition = "delivered"
            return {
                "disposition": disposition,
                "priority": priority,
                "original_priority": original,
                "matched": matched,
                "rule": blocked["name"] if blocked is not None else "",
                "reason": reason,
            }

    # -- audit ---------------------------------------------------------- #
    def log_blocked(self, source: str, kind: str, actor: str, event_id: str,
                    rule: str, reason: str, text: str,
                    level: str = DEFAULT_LEVEL) -> None:
        """One JSON line per rule-blocked event; failures warn, never propagate."""
        entry = {
            "at": now_iso(),
            "source": str(source),
            "kind": str(kind),
            "level": str(level),
            "actor": str(actor),
            "event_id": str(event_id),
            "rule": str(rule),
            "reason": str(reason),
            "text": " ".join(str(text or "").split())[:BLOCKED_TEXT_CHARS],
        }
        with self._lock:
            try:
                self.blocked_path.parent.mkdir(parents=True, exist_ok=True)
                with self.blocked_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as exc:
                log.warning("cannot write blocked log %s: %s",
                            self.blocked_path, exc)

    # -- startup backfill ----------------------------------------------- #
    def _backfill_rate(self) -> int:
        """Seed rate_limit windows from recent events so a restart is not a
        free pass: the last ``window_s`` of accepted traffic still counts."""
        if self.store is None:
            return 0
        total = 0
        now = time.time()
        for rule in self._rules:
            action = rule["action"]
            if action["type"] != "rate_limit":
                continue
            cutoff = now - action["window_s"]
            try:
                rows = self.store.recent_events(cutoff)
            except Exception as exc:  # noqa: BLE001 - never block startup
                log.warning("rules: rate backfill failed for %s: %s",
                            rule["name"], exc)
                continue
            hits: List[float] = []
            for row in rows:
                if (row.get("disposition") or "delivered") == "dropped":
                    continue
                if (row.get("level") or DEFAULT_LEVEL) == "log":
                    continue  # archive-only traffic never consumes rate quota
                env = {
                    "source": row.get("source"),
                    "kind": row.get("kind"),
                    "actor": row.get("actor"),
                    "text": row.get("text"),
                    "priority": row.get("priority") or 0,
                    "thread_key": "",
                }
                payload = row.get("payload")
                if isinstance(payload, dict) and payload.get("thread_key"):
                    env["thread_key"] = str(payload["thread_key"])
                if self._matches(rule, env, int(env["priority"])):
                    hits.append(float(row.get("created_at") or 0.0))
            self._rate_state[rule["id"]] = sorted(hits)
            total += len(hits)
            log.info("rules: rule=%s rate window backfilled %d hit(s) "
                     "from db", rule["name"], len(hits))
        return total

    # -- introspection --------------------------------------------------- #
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"rules": len(self._rules),
                    "rules_mtime": self._mtime_iso,
                    "rules_file": str(self.path),
                    "rules_error": self._last_error or None}


def read_blocked_log(path: Any, source: Optional[str] = None,
                     since: Optional[Any] = None,
                     limit: int = 100) -> List[Dict[str, Any]]:
    """Newest-first slice of the blocked audit log, read from the tail.

    ``source`` is an exact match, ``since`` an inclusive lower bound (ISO or
    epoch) on ``at``.  Mirrors ``buscore.read_drops`` but with L2 fields.
    """
    limit = max(1, min(int(limit), 1000))
    wanted = str(source).strip() if source else ""
    cutoff = parse_drop_since(since) if since is not None else None
    out: List[Dict[str, Any]] = []
    target = Path(os.path.expanduser(str(path)))
    try:
        handle = open(target, "rb")
    except OSError:
        return out

    def wanted_entry(entry: Dict[str, Any]) -> bool:
        if wanted and str(entry.get("source") or "") != wanted:
            return False
        if cutoff is not None:
            stamp = parse_drop_since(entry.get("at"))
            if stamp is None or stamp < cutoff:
                return False
        return True

    with handle:
        try:
            pos = handle.seek(0, os.SEEK_END)
        except OSError:
            return out
        remainder = b""
        while pos > 0 and len(out) < limit:
            step = min(65536, pos)
            pos -= step
            handle.seek(pos)
            chunk = handle.read(step) + remainder
            lines = chunk.split(b"\n")
            remainder = lines.pop(0)
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(entry, dict) or not wanted_entry(entry):
                    continue
                out.append(entry)
                if len(out) >= limit:
                    break
        if len(out) < limit and remainder.strip():
            try:
                entry = json.loads(remainder.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                entry = None
            if isinstance(entry, dict) and wanted_entry(entry):
                out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# source declarations
# --------------------------------------------------------------------------- #

SCHEMA_TYPES = ("string", "int", "bool", "list", "dict")
_PY_TYPES = {"string": str, "int": int, "bool": bool, "list": list, "dict": dict}


def _is_type(value: Any, declared: str) -> bool:
    if declared == "int":  # bool is an int subclass; never let True pass as int
        return isinstance(value, int) and not isinstance(value, bool)
    expected = _PY_TYPES.get(declared)
    return expected is not None and isinstance(value, expected)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_ts_epoch(value: Any) -> Optional[float]:
    """ISO-8601 (with offset or local naive) → epoch seconds; None if junk."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


class SourceRegistry:
    """The only knowledge this service has about sources: their declarations.

    It never imports or calls a client, never knows a wire protocol.  A source
    is a directory ``sources/<name>/source.json``; adding one needs no change
    to this file.  Undeclared sources are accepted and warned about once.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._decls: Dict[str, Dict[str, Any]] = {}
        self._warned: set = set()

    # -- loading -------------------------------------------------------- #
    @staticmethod
    def _normalise(raw: Any, path: Path) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("root must be a JSON object")
        name = str(raw.get("name") or path.parent.name).strip()
        if (not name or len(name) > 64 or "/" in name or "\\" in name
                or any(ch.isspace() for ch in name)):
            raise ValueError("bad source name %r" % name)
        title = raw.get("title")
        title = str(title).strip() if title is not None else name
        mode = raw.get("mode")
        mode = str(mode).strip() if mode is not None else "push"
        if not mode:
            raise ValueError("mode must be a non-empty string")
        schema = raw.get("schema") or {}
        if not isinstance(schema, dict):
            raise ValueError("schema must be an object")
        clean_schema: Dict[str, str] = {}
        for field, declared in schema.items():
            field = str(field).strip()
            if not field:
                raise ValueError("schema has an empty field name")
            if declared not in SCHEMA_TYPES:
                raise ValueError("schema[%r]=%r is not one of %s"
                                 % (field, declared, list(SCHEMA_TYPES)))
            clean_schema[field] = declared
        policy = raw.get("policy") or {}
        if not isinstance(policy, dict):
            raise ValueError("policy must be an object")
        health = raw.get("health") or {}
        if not isinstance(health, dict):
            raise ValueError("health must be an object")
        max_silence = health.get("max_silence_s")
        if max_silence is not None:
            if isinstance(max_silence, bool):
                raise ValueError("health.max_silence_s must be > 0 or null")
            try:
                max_silence = float(max_silence)
            except (TypeError, ValueError):
                raise ValueError("health.max_silence_s must be > 0 or null")
            if max_silence <= 0:
                raise ValueError("health.max_silence_s must be > 0 or null")
            if max_silence.is_integer():
                max_silence = int(max_silence)
        dedupe = raw.get("dedupe") or []
        if not isinstance(dedupe, list) or not all(
                isinstance(item, str) for item in dedupe):
            raise ValueError("dedupe must be a list of field names")
        return {
            "name": name, "title": title, "mode": mode,
            "schema": clean_schema, "policy": dict(policy),
            "max_silence_s": max_silence,
            "dedupe": [str(item) for item in dedupe],
            "path": str(path),
        }

    def load(self) -> int:
        """Scan ``*/source.json``. A bad file is skipped, never fatal."""
        found: Dict[str, Dict[str, Any]] = {}
        if self.directory.is_dir():
            try:
                paths = sorted(self.directory.glob("*/source.json"))
            except OSError as exc:
                log.warning("sources: cannot scan %s: %s", self.directory, exc)
                paths = []
        else:
            log.warning("sources: %s is not a directory; no declarations loaded",
                        self.directory)
            paths = []
        for path in paths:
            try:
                decl = self._normalise(
                    json.loads(path.read_text(encoding="utf-8")), path)
            except (OSError, ValueError) as exc:
                log.warning("sources: skipping %s: %s", path, exc)
                continue
            existing = found.get(decl["name"])
            if existing is not None:
                log.warning("sources: duplicate name %r — %s ignored, keeping %s",
                            decl["name"], path, existing["path"])
                continue
            found[decl["name"]] = decl
        with self._lock:
            self._decls = found
            self._warned = set()
        log.info("sources: %d declaration(s) from %s: %s", len(found),
                 self.directory, ", ".join(sorted(found)) or "(none)")
        return len(found)

    # -- lookups -------------------------------------------------------- #
    def __len__(self) -> int:
        with self._lock:
            return len(self._decls)

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._decls.get(name)

    def note_unknown(self, name: str) -> None:
        """Warn once per source; the event itself is still accepted."""
        with self._lock:
            if name in self._warned:
                return
            self._warned.add(name)
        log.warning("sources: undeclared source %r is posting events — accepted "
                    "anyway (declare it as sources/%s/source.json)",
                    name, name)

    @staticmethod
    def validate(decl: Dict[str, Any], body: Dict[str, Any]) -> Optional[str]:
        """Smallest useful check: declared fields must be present, non-empty
        and of the declared JSON type. Returns the first offending field."""
        for field, declared in decl["schema"].items():
            if field not in body:
                return field
            value = body[field]
            if _is_empty(value) or not _is_type(value, declared):
                return field
        return None

    # -- health --------------------------------------------------------- #
    @staticmethod
    def _health(max_silence: Optional[int], row: Optional[Dict[str, Any]],
                now: float) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "max_silence_s": max_silence, "last_event_at": None,
            "silence_s": None, "stale": False, "last_error": None,
        }
        if not row:
            return entry  # nothing ever arrived: not stale, just unknown
        if row.get("ts"):
            entry["last_event_at"] = row["ts"]
        epoch = parse_ts_epoch(row.get("ts"))
        if epoch is None and row.get("created_at") is not None:
            try:
                epoch = float(row["created_at"])
            except (TypeError, ValueError):
                epoch = None
        if epoch is not None:
            silence = max(0.0, now - epoch)
            entry["silence_s"] = round(silence, 1)
            if max_silence is not None:
                entry["stale"] = silence > float(max_silence)
        return entry

    def entries(self, store: TriggerStore,
                include_undeclared: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            decls = dict(self._decls)
        observed = store.last_event_ts_by_source()
        now = time.time()
        out: List[Dict[str, Any]] = []
        for name in sorted(decls):
            decl = decls[name]
            out.append({
                "name": name, "title": decl["title"], "mode": decl["mode"],
                "schema": decl["schema"], "policy": decl["policy"],
                "dedupe": decl["dedupe"], "declared": True,
                "health": self._health(decl["max_silence_s"],
                                       observed.get(name), now),
            })
        if include_undeclared:
            for name in sorted(set(observed) - set(decls)):
                out.append({
                    "name": name, "title": name, "mode": None,
                    "schema": {}, "policy": {}, "dedupe": [],
                    "declared": False,
                    "health": self._health(None, observed.get(name), now),
                })
        return out


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class BadRequest(Exception):
    pass


class SchemaMismatch(BadRequest):
    """A declared source sent a field its own declaration forbids."""

    def __init__(self, field: str):
        super().__init__("schema_mismatch")
        self.field = field


class Unauthorized(Exception):
    pass


def parse_levels(qs: Dict[str, List[str]], subscriber: str) -> set:
    """Which envelope levels one subscriber wants.

    Defaults by role:

      * ``hermes*`` (the agent's own poller, default ``hermes-main``):
        ``event`` + ``notify`` — ``info`` must never be injected or wake a
        session, and ``log`` is never delivered at all;
      * everything else (workers): ``event`` + ``notify`` + ``info``.

    ``?levels=event,info`` overrides either default with exactly the named
    set; legacy ``super`` / ``super_event`` fold to ``event``.  ``log`` is not
    a deliverable level: naming it is a 400, and no subscriber can ever
    receive a ``log`` row (archive reads use ``include_log=1`` on a direct
    ``GET /events`` query).
    """
    raw = (qs.get("levels", [""])[0] or "").strip()
    if raw:
        levels = {canonical_level(part) for part in raw.split(",") if part.strip()}
        unknown = levels - set(DELIVERY_LEVELS)
        if unknown:
            raise BadRequest("levels must be a subset of %s (got %s)"
                             % (",".join(DELIVERY_LEVELS),
                                ",".join(sorted(unknown))))
        if not levels:
            raise BadRequest("levels must name %s" % " and/or ".join(
                DELIVERY_LEVELS))
        return levels
    if subscriber.startswith("hermes"):
        return {"event", "notify"}
    return set(DELIVERY_LEVELS)



class TriggerHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, addr, handler, once: bool = False):
        self.once = once
        # In --once mode wait for the handler thread so the response is flushed
        # before the process exits; in server mode long polls must not block exit.
        self.daemon_threads = not once
        super().__init__(addr, handler)

    def handle_error(self, request, client_address):
        log.exception("unhandled error for %s", client_address)


class TriggerHandler(BaseHTTPRequestHandler):
    server_version = "mowu-trigger/" + VERSION
    sys_version = ""

    # -- helpers -------------------------------------------------------- #
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

    def _int_param(self, qs: Dict[str, List[str]], name: str,
                   default: Optional[int] = None) -> Optional[int]:
        raw = qs.get(name, [None])[0]
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise BadRequest("parameter %r must be an integer" % name)

    def _float_param(self, qs: Dict[str, List[str]], name: str,
                     default: float = 0.0) -> float:
        raw = qs.get(name, [None])[0]
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            raise BadRequest("parameter %r must be a number" % name)

    @staticmethod
    def _subscriber(qs: Dict[str, List[str]]) -> str:
        sub = (qs.get("subscriber", [""])[0] or "").strip()
        if not sub:
            raise BadRequest("subscriber is required")
        if len(sub) > 128:
            raise BadRequest("subscriber too long")
        return sub

    def _require_token(self) -> None:
        """401 for a missing/wrong X-Trigger-Token; no event data leaks."""
        expected = getattr(self.server, "token", "") or ""
        if not expected:
            return
        given = self.headers.get("X-Trigger-Token") or ""
        try:
            ok = hmac.compare_digest(given.encode("latin-1", "ignore"),
                                     expected.encode("utf-8"))
        except Exception:
            ok = False
        if not ok:
            raise Unauthorized()

    def _serve_subscribe(self, qs: Dict[str, List[str]]) -> None:
        """SSE tail of the inbox for remote workers (id-ordered).

        Lifecycle (gap / quota / fast-fail) is implemented in
        ``buscore.pump``; this method only does HTTP + the backlog.
        """
        try:
            self._require_token()
            subscriber = ((qs.get("name", [""])[0]
                           or qs.get("subscriber", [""])[0]) or "").strip()
            if not subscriber:
                raise BadRequest("name is required")
            if len(subscriber) > 128:
                raise BadRequest("name too long")
            since = self._int_param(qs, "since")
            max_events = self._int_param(qs, "max_events", 0) or 0
            levels = parse_levels(qs, subscriber)
        except Unauthorized:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            log.info("%s GET /subscribe -> 401", self.address_string())
            return
        except BadRequest as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if since is None:
            stored = self.server.store.cursor_of(subscriber)
            since = stored if stored > 0 else self.server.store.max_id()
        since = max(0, int(since))

        store = self.server.store
        row = store.ensure_subscription(subscriber, since,
                                        self.server.subscription_ttl)
        view = store.view_of(row, self.server.renew_window)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.connection.settimeout(10.0)
        except OSError:
            pass

        if view["expired"]:
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.write(("event: expired\ndata: %s\n\n" % json.dumps(
                    {"name": subscriber}, ensure_ascii=False)).encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            log.info("SSE subscribe name=%s refused: subscription status=%s "
                     "expired=%s", subscriber, view["status"], view["expired"])
            return

        manager = self.server.manager
        sub = manager.register(subscriber, since, max_events, levels=levels)

        backlog = self.server.store.recent_since(since, 200, levels)
        visible = self.server.store.count_since(since, levels)
        if backlog and visible > len(backlog):
            sub.lost_from = since + 1
            sub.lost_to = int(backlog[0]["id"]) - 1
        for event in backlog:
            event["seq"] = event["id"]
            sub.push(int(event["id"]), event)

        def snapshot() -> Optional[Dict[str, Any]]:
            return store.subscription_view(subscriber,
                                           self.server.renew_window)

        lease = LeaseWatch(snapshot, subscriber)
        log.info("SSE subscribe name=%s since=%d backlog=%d max_events=%d "
                 "expires_in=%.1fs from=%s", subscriber, since, len(backlog),
                 max_events, view["expires_in"], self.address_string())
        reason = "client_closed"
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            reason = pump(sub, self._write, probe=self._peer_gone, lease=lease)
        except (BrokenPipeError, ConnectionResetError):
            reason = "write_failures"
        except Exception as exc:  # noqa: BLE001
            log.info("SSE stream for %s failed: %s: %s", subscriber,
                     type(exc).__name__, exc)
            reason = "write_failures"
        finally:
            sub.close(reason)
            manager.remove(sub)
            log.info("SSE subscription %s ended reason=%s delivered=%d since=%d",
                     subscriber, reason, sub.delivered, sub.since)

    def _write(self, chunk: bytes) -> None:
        self.wfile.write(chunk)
        self.wfile.flush()

    def _peer_gone(self) -> bool:
        """True when the worker closed its end (EOF/RST) — detected in ~1s."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    # -- routes --------------------------------------------------------- #
    def do_GET(self):
        started = time.monotonic()
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        if not self.server.client_allowed(self.client_address[0]):
            self._send_json(403, {"ok": False, "error": "forbidden"})
            log.info("%s GET %s -> 403 (cidr)", self.address_string(), self.path)
            return
        if parsed.path == "/subscribe":
            self._serve_subscribe(qs)
            return
        status = 200
        try:
            if parsed.path == "/health":
                status, payload = 200, self._health()
            elif parsed.path == "/sources":
                # Declaration metadata only (no event data), so like /health
                # it stays readable without the bus token.
                status, payload = 200, self.server.sources.entries(
                    self.server.store)
            elif parsed.path == "/events":
                self._require_token()
                status, payload = 200, self._events(qs)
            elif parsed.path == "/drops":
                self._require_token()
                status, payload = 200, self._drops(qs)
            elif parsed.path == "/blocked":
                self._require_token()
                status, payload = 200, self._blocked(qs)
            elif parsed.path == "/stats":
                self._require_token()
                status, payload = 200, self._stats(qs)
            elif parsed.path == "/subscriptions":
                self._require_token()
                status, payload = 200, self._subscriptions()
            elif parsed.path == "/bus/health":
                status, payload = 200, self.server.bus_health()
            elif parsed.path == "/":
                status, payload = 200, {
                    "ok": True, "service": "mowu-trigger",
                    "version": VERSION,
                    "endpoints": ["/trigger", "/sources", "/events", "/drops",
                                  "/blocked", "/ack", "/renew", "/subscriptions",
                                  "/unsubscribe", "/health", "/stats",
                                  "/subscribe", "/bus/health"],
                }
            else:
                status, payload = 404, {"ok": False, "error": "not found"}
        except Unauthorized:
            status, payload = 401, {"ok": False, "error": "unauthorized"}
        except BadRequest as exc:
            status, payload = 400, {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("GET %s failed", self.path)
            status, payload = 500, {"ok": False,
                                    "error": "%s: %s" % (type(exc).__name__, exc)}
        self._send_json(status, payload)
        log.info("%s %s %s -> %d %.1fms", self.address_string(),
                 self.command, self.path, status,
                 (time.monotonic() - started) * 1000)

    def do_POST(self):
        started = time.monotonic()
        status = 200
        try:
            parsed = urlparse(self.path)
            if not self.server.client_allowed(self.client_address[0]):
                status, payload = 403, {"ok": False, "error": "forbidden"}
            elif parsed.path == "/trigger":
                if self.server.require_token_for_trigger:
                    self._require_token()
                status, payload = 200, self._trigger(self._read_json())
            elif parsed.path == "/ack":
                self._require_token()
                status, payload = 200, self._ack(self._read_json())
            elif parsed.path == "/renew":
                self._require_token()
                status, payload = self._renew(self._read_json())
            elif parsed.path == "/unsubscribe":
                self._require_token()
                status, payload = self._unsubscribe(self._read_json())
            else:
                status, payload = 404, {"ok": False, "error": "not found"}
        except Unauthorized:
            status, payload = 401, {"ok": False, "error": "unauthorized"}
        except SchemaMismatch as exc:
            status, payload = 400, {"error": "schema_mismatch", "field": exc.field,
                                    "ok": False}
        except BadRequest as exc:
            status, payload = 400, {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("POST %s failed", self.path)
            status, payload = 500, {"ok": False,
                                    "error": "%s: %s" % (type(exc).__name__, exc)}
        self._send_json(status, payload)
        log.info("%s %s %s -> %d %.1fms", self.address_string(),
                 self.command, self.path, status,
                 (time.monotonic() - started) * 1000)

    def do_DELETE(self):
        started = time.monotonic()
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        status = 200
        try:
            if not self.server.client_allowed(self.client_address[0]):
                status, payload = 403, {"ok": False, "error": "forbidden"}
            elif parsed.path == "/subscriptions":
                self._require_token()
                name = (qs.get("name", [""])[0] or "").strip()
                if not name:
                    raise BadRequest("name is required")
                status, payload = self._close_subscription(name)
            else:
                status, payload = 404, {"ok": False, "error": "not found"}
        except Unauthorized:
            status, payload = 401, {"ok": False, "error": "unauthorized"}
        except BadRequest as exc:
            status, payload = 400, {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("DELETE %s failed", self.path)
            status, payload = 500, {"ok": False,
                                    "error": "%s: %s" % (type(exc).__name__, exc)}
        self._send_json(status, payload)
        log.info("%s %s %s -> %d %.1fms", self.address_string(),
                 self.command, self.path, status,
                 (time.monotonic() - started) * 1000)

    # -- endpoint implementations --------------------------------------- #
    def _health(self) -> Dict[str, Any]:
        server = self.server
        snapshot = server.rules.snapshot()
        return {
            "ok": True,
            "version": VERSION,
            "levels": list(LEVELS),
            "events": server.store.count(),
            "sources": len(server.sources),
            "port": server.server_address[1],
            "db_path": server.store.path,
            "uptime_s": round(max(0.0, time.time() - server.started_at), 1),
            "subscribers": server.manager.health(),
            "queue_max": server.queue_max,
            "drops_log": os.path.expanduser(DEFAULT_DROP_LOG),
            "rules": snapshot["rules"],
            "rules_mtime": snapshot["rules_mtime"],
            "rules_file": snapshot["rules_file"],
            "rules_error": snapshot["rules_error"],
            "blocked_log": str(server.rules.blocked_path),
            "subscription_ttl": server.subscription_ttl,
            "renew_window": server.renew_window,
            "subscriptions": server.store.subscription_counts(),
        }

    def _drops(self, qs: Dict[str, List[str]]) -> Dict[str, Any]:
        """Queue-drop audit, newest first — the log is the only notification.

        ``subscriber`` filters to one name (omitted = all), ``since`` is an
        inclusive ISO-8601 or epoch lower bound on ``at``, ``limit`` caps the
        slice (default 100, hard max 1000).  Read from the file tail; the
        whole log is never loaded.
        """
        subscriber = (qs.get("subscriber", [""])[0] or "").strip() or None
        since_raw = qs.get("since", [None])[0]
        limit = self._int_param(qs, "limit", 100)
        if limit is None:
            limit = 100
        if limit < 1:
            raise BadRequest("limit must be >= 1")
        if since_raw not in (None, "") and parse_drop_since(since_raw) is None:
            raise BadRequest("since must be ISO-8601 or epoch seconds")
        drops = read_drops(DEFAULT_DROP_LOG, subscriber=subscriber,
                           since=since_raw, limit=limit)
        return {"drops": drops, "count": len(drops)}

    def _blocked(self, qs: Dict[str, List[str]]) -> Dict[str, Any]:
        """L2 blocked audit, newest first, read from the log tail."""
        source = (qs.get("source", [""])[0] or "").strip() or None
        since_raw = qs.get("since", [None])[0]
        limit = self._int_param(qs, "limit", 100)
        if limit is None:
            limit = 100
        if limit < 1:
            raise BadRequest("limit must be >= 1")
        if since_raw not in (None, "") and parse_drop_since(since_raw) is None:
            raise BadRequest("since must be ISO-8601 or epoch seconds")
        entries = read_blocked_log(self.server.rules.blocked_path,
                                   source=source, since=since_raw, limit=limit)
        return {"blocked": entries, "count": len(entries)}

    def _stats(self, qs: Dict[str, List[str]]) -> Dict[str, Any]:
        payload = self.server.store.stats(self._subscriber(qs))
        payload["levels"] = list(LEVELS)
        payload["by_level"] = self.server.store.level_counts()
        payload["sources"] = self.server.sources.entries(
            self.server.store, include_undeclared=True)
        return payload

    def _trigger(self, body: Dict[str, Any]) -> Dict[str, Any]:
        source = str(body.get("source") or "").strip()
        if not source:
            raise BadRequest("source is required")
        decl = self.server.sources.get(source)
        if decl is not None:
            bad_field = SourceRegistry.validate(decl, body)
            if bad_field is not None:
                log.warning("schema_mismatch source=%s field=%s declared=%s",
                            source, bad_field, decl["schema"].get(bad_field))
                raise SchemaMismatch(bad_field)
        else:
            self.server.sources.note_unknown(source)
        text = body.get("text")
        if text is None or not str(text).strip():
            raise BadRequest("text is required")
        text = str(text)
        kind = str(body.get("kind") or "event")
        actor = str(body.get("actor") or "")
        level = canonical_level(body.get("level"))
        if level not in LEVELS:
            raise BadRequest("bad_level")
        raw_priority = body.get("priority")
        if raw_priority is None:
            priority = LEVEL_DEFAULT_PRIORITY[level]
        else:
            try:
                priority = int(raw_priority)
            except (TypeError, ValueError):
                raise BadRequest("priority must be an integer")
        event_id = body.get("event_id")
        if event_id is not None:
            event_id = str(event_id).strip() or None
        dedupe_key = body.get("dedupe_key")
        if dedupe_key is not None:
            dedupe_key = str(dedupe_key).strip() or None
            if dedupe_key is not None and len(dedupe_key) > 512:
                raise BadRequest("dedupe_key too long (max 512)")

        payload = body.get("payload")
        thread_key = str(body.get("thread_key") or "").strip()
        if not thread_key and isinstance(payload, dict):
            thread_key = str(payload.get("thread_key") or "").strip()

        # L2: cross-source rules live here, once, for every source.  Reload
        # (mtime check) happens inside evaluate, before the decision.  A
        # `log` event is archive-only: it bypasses L2 entirely and is never
        # queued for a subscriber.
        if level == "log":
            outcome = {
                "disposition": "delivered",
                "priority": priority,
                "original_priority": priority,
                "matched": [],
                "rule": "",
                "reason": "",
            }
        else:
            outcome = self.server.rules.evaluate({
                "source": source, "kind": kind, "level": level, "actor": actor,
                "text": text, "priority": priority,
                "thread_key": thread_key,
                "subject": str(body.get("subject") or ""),
            })
        result = self.server.store.add_event(
            source=source, text=text, kind=kind, level=level,
            priority=outcome["priority"], actor=actor, payload=payload,
            event_id=event_id, dedupe_key=dedupe_key,
            disposition=outcome["disposition"])

        if result.get("duplicate"):
            # A retry must report what is actually stored, not a fresh
            # evaluation that may disagree after a rule change.
            stored = self.server.store.get_event(int(result["id"]))
            if stored is not None:
                outcome["disposition"] = (stored.get("disposition")
                                          or "delivered")
                outcome["priority"] = stored.get("priority")
                outcome["rule"] = ""
                outcome["reason"] = "duplicate"
                level = stored.get("level") or DEFAULT_LEVEL

        if outcome["disposition"] == "dropped":
            if not result.get("duplicate"):
                self.server.rules.log_blocked(
                    source=source, kind=kind, actor=actor,
                    event_id=str(result.get("event_id") or ""),
                    rule=outcome["rule"] or "rule", reason=outcome["reason"],
                    text=text, level=level)
            log.info("blocked id=%s source=%s level=%s rule=%s reason=%s",
                     result.get("id"), source, level, outcome["rule"],
                     outcome["reason"])
        elif level != "log" and not result.get("duplicate"):
            stored = self.server.store.get_event(int(result["id"]))
            if stored is not None:
                pushed = self.server.broadcast(stored)
                log.debug("broadcast id=%s to %d subscriber(s)",
                          result["id"], pushed)

        result["priority"] = outcome["priority"]
        result["disposition"] = outcome["disposition"]
        result["level"] = level
        result["rule"] = outcome["rule"]
        result["reason"] = outcome["reason"]
        result["rules_matched"] = outcome["matched"]
        result["wake"] = (level in WAKE_LEVELS
                          and outcome["disposition"] != "dropped"
                          and outcome["priority"] is not None
                          and int(outcome["priority"]) <= WAKE_PRIORITY_MAX)
        result["delivered"] = (level != "log"
                               and outcome["disposition"] != "dropped")
        return result

    def _events(self, qs: Dict[str, List[str]]) -> Dict[str, Any]:
        """Snapshot + subscription snapshot. An expired lease returns no events."""
        subscriber = self._subscriber(qs)
        since = self._int_param(qs, "since")
        limit = self._int_param(qs, "limit", 50) or 50
        wait = self._float_param(qs, "wait", 0.0)
        include_log = (qs.get("include_log", [""])[0] or "").strip().lower() \
            in ("1", "true", "yes", "on")
        levels = parse_levels(qs, subscriber)
        if include_log:
            levels.add("log")
        store = self.server.store
        row = store.ensure_subscription(subscriber, since or 0,
                                        self.server.subscription_ttl)
        view = store.view_of(row, self.server.renew_window)
        if view["expired"]:
            return {"events": [], "levels": sorted(levels), "subscription": view}

        # Never wait past the lease: an expiring subscription must wake up
        # with an empty array instead of handing out events it no longer owns.
        if wait > 0:
            wait = min(wait, max(0.0, view["expires_in"]))
        events = store.list_events(subscriber, since, limit, wait, levels)

        fresh = store.subscription_view(subscriber, self.server.renew_window)
        if fresh is None:
            fresh = view
        if fresh["expired"]:
            return {"events": [], "levels": sorted(levels),
                    "subscription": fresh}
        return {"events": events, "levels": sorted(levels),
                "subscription": fresh}

    def _renew(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        name = str(body.get("name") or body.get("subscriber") or "").strip()
        if not name:
            raise BadRequest("name is required")
        server = self.server
        row = server.store.renew_subscription(name, server.subscription_ttl)
        if row is None:
            return 404, {"ok": False, "error": "unknown subscription",
                         "name": name}
        if row["status"] != "active":
            return 410, {"ok": False, "error": "subscription %s" % row["status"],
                         "name": name}
        view = server.store.view_of(row, server.renew_window)
        return 200, {"ok": True, "name": name,
                     "expires_in": view["expires_in"],
                     "renew_count": view["renew_count"]}

    def _subscriptions(self) -> Dict[str, Any]:
        server = self.server
        entries = server.store.list_subscriptions(server.renew_window)
        return {"ok": True, "count": len(entries),
                "subscription_ttl": server.subscription_ttl,
                "renew_window": server.renew_window,
                "subscriptions": entries}

    def _close_subscription(self, name: str) -> Tuple[int, Dict[str, Any]]:
        """Stop delivery immediately: DB status + any live SSE connection."""
        row = self.server.store.close_subscription(name)
        if row is None:
            return 404, {"ok": False, "error": "unknown subscription",
                         "name": name}
        sub = self.server.manager.get(name)
        if sub is not None:
            sub.close("unsubscribed")
            self.server.manager.remove(sub)
        return 200, {"ok": True, "name": name, "status": "closed"}

    def _unsubscribe(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        name = str(body.get("name") or body.get("subscriber") or "").strip()
        if not name:
            raise BadRequest("name is required")
        return self._close_subscription(name)

    def _ack(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Cursor only. Liveness is the SSE connection, not this call."""
        subscriber = str(body.get("subscriber") or body.get("name") or "").strip()
        if not subscriber:
            raise BadRequest("subscriber (or name) is required")
        raw = body.get("up_to_id", body.get("up_to_seq"))
        if raw is None:
            raise BadRequest("up_to_id (or up_to_seq) is required")
        try:
            up_to = int(raw)
        except (TypeError, ValueError):
            raise BadRequest("up_to_id must be an integer")
        result = self.server.store.ack(subscriber, up_to)
        cursor = self.server.manager.ack(subscriber, up_to)
        if cursor:
            result["up_to_seq"] = cursor["up_to_seq"]
        return result

# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #

class TriggerServer(TriggerHTTPServer):
    """HTTP shell around TriggerStore. Contains zero source/protocol logic."""

    def __init__(self, addr, store: TriggerStore, once: bool = False,
                 token: str = "", require_token_for_trigger: bool = False,
                 allow_nets: Optional[List[Any]] = None,
                 subscription_ttl: float = DEFAULT_SUBSCRIPTION_TTL,
                 renew_window: float = DEFAULT_RENEW_WINDOW,
                 queue_max: int = QUEUE_MAX,
                 sources: Optional[SourceRegistry] = None,
                 rules_path: str = DEFAULT_RULES_FILE,
                 blocked_log: str = DEFAULT_BLOCKED_LOG):
        self.store = store
        self.started_at = time.time()
        self.token = token or ""
        self.require_token_for_trigger = bool(require_token_for_trigger)
        self.allow_nets = list(allow_nets or [])
        self.subscription_ttl = int(subscription_ttl)
        self.renew_window = int(renew_window)
        self.queue_max = max(1, int(queue_max))
        self.sources = (sources if sources is not None
                        else SourceRegistry(DEFAULT_SOURCES_DIR))
        self.rules = RuleEngine(rules_path, blocked_log, store=store)
        self.manager = SubscriptionManager(queue_max=self.queue_max)
        super().__init__(addr, TriggerHandler, once=once)

    # -- remote-bus bookkeeping ------------------------------------------ #
    def broadcast(self, event: Dict[str, Any]) -> int:
        """Fan a freshly stored event out to live subscribers."""
        payload = dict(event)
        payload["seq"] = payload["id"]
        return self.manager.broadcast(int(event["id"]), payload)

    def bus_health(self) -> Dict[str, Any]:
        return {"ok": True, "subscribers": self.manager.health(),
                "events": self.store.count(), "last_id": self.store.max_id()}

    def client_allowed(self, ip: str) -> bool:
        if not self.allow_nets:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if addr.is_loopback:
            return True
        return any(addr in net for net in self.allow_nets)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def load_or_create_token(path: Path) -> Tuple[str, bool]:
    """Read the shared bus token, generating a 32-byte hex one if absent."""
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


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="mowu-trigger: pure event inbox. Loopback by default, "
                    "one explicit LAN address on request. Sources are "
                    "separate clients; this process has none.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="listen port on 127.0.0.1 (default %(default)s)")
    p.add_argument("--host", default=HOST,
                   help="bind address on this machine (default %(default)s); "
                        "a LAN IP is allowed, 0.0.0.0 is refused")
    p.add_argument("--db", default=DEFAULT_DB,
                   help="SQLite path (default %(default)s)")
    p.add_argument("--sources-dir", default=str(DEFAULT_SOURCES_DIR),
                   metavar="DIR",
                   help="directory of <name>/source.json declarations "
                        "(default %(default)s)")
    p.add_argument("--token-file", default=DEFAULT_TOKEN_PATH,
                   help="shared bus token file (default %(default)s)")
    p.add_argument("--rules-file", default=DEFAULT_RULES_FILE,
                   metavar="PATH",
                   help="L2 rule file, hot-reloaded on mtime change "
                        "(default %(default)s; missing = no rules)")
    p.add_argument("--blocked-log", default=DEFAULT_BLOCKED_LOG,
                   metavar="PATH",
                   help="JSONL audit of events dropped by L2 rules "
                        "(default %(default)s)")
    p.add_argument("--allow-cidr", action="append", default=None,
                   metavar="CIDR",
                   help="CIDR allowed to connect when bound to a LAN address "
                        "(repeatable; default %s)" % DEFAULT_ALLOW_CIDR)
    p.add_argument("--require-token-for-trigger", action="store_true",
                   help="also require X-Trigger-Token on POST /trigger")
    p.add_argument("--subscription-ttl", type=int, default=None,
                   metavar="SECONDS",
                   help="how long a subscription may receive events before it "
                        "must be renewed (default: $TRIGGER_SUBSCRIPTION_TTL "
                        "or %d)" % DEFAULT_SUBSCRIPTION_TTL)
    p.add_argument("--renew-window", type=int, default=None,
                   metavar="SECONDS",
                   help="warn clients this many seconds before expiry "
                        "(default: $TRIGGER_RENEW_WINDOW or %d)"
                        % DEFAULT_RENEW_WINDOW)
    p.add_argument("--queue-max", type=int, default=None, metavar="N",
                   help="slots in each subscriber's fan-out queue, shared by "
                        "P0/P1/P2 (default: $TRIGGER_QUEUE_MAX or %d)"
                        % QUEUE_MAX)
    p.add_argument("--log", default=str(DEFAULT_LOG),
                   help="log file (default %(default)s)")
    p.add_argument("--once", action="store_true",
                   help="serve exactly one HTTP request, then exit (self-test)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=VERSION)
    return p.parse_args(argv)


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print("warning: %s=%r is not an integer; using %d"
              % (name, raw, default), file=sys.stderr)
        return default


def _janitor_loop(store: TriggerStore, stop_event: threading.Event) -> None:
    """Hourly: age out records that stayed unused past the 24h grace window."""
    while True:
        try:
            store.expire_stale_subscriptions(SUBSCRIPTION_GRACE)
        except Exception:  # noqa: BLE001 - janitor must never kill the server
            log.exception("subscription janitor failed")
        if stop_event.wait(JANITOR_INTERVAL):
            return


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(Path(os.path.expanduser(args.log)), args.verbose)

    host = (args.host or HOST).strip()
    if host in ("0.0.0.0", "::", "::0", "*", ""):
        print("error: refusing to bind %r — mowu-trigger must never listen on "
              "every interface. Use 127.0.0.1 or one specific LAN address."
              % host, file=sys.stderr)
        return 2
    try:
        bind_addr = ipaddress.ip_address(host)
    except ValueError:
        print("error: --host must be an IP literal (got %r)" % host,
              file=sys.stderr)
        return 2

    token_path = Path(os.path.expanduser(args.token_file))
    token, generated = load_or_create_token(token_path)

    ttl = (args.subscription_ttl if args.subscription_ttl is not None
           else _env_int("TRIGGER_SUBSCRIPTION_TTL", DEFAULT_SUBSCRIPTION_TTL))
    renew_window = (args.renew_window if args.renew_window is not None
                    else _env_int("TRIGGER_RENEW_WINDOW", DEFAULT_RENEW_WINDOW))
    if ttl < 1:
        print("error: --subscription-ttl must be >= 1 (got %d)" % ttl,
              file=sys.stderr)
        return 2
    if renew_window < 0:
        print("error: --renew-window must be >= 0 (got %d)" % renew_window,
              file=sys.stderr)
        return 2
    if renew_window >= ttl:
        # Otherwise renew_required is true forever and a dutiful client would
        # renew on every single poll — warning + clamp, never silent.
        clamped = max(0, ttl - 1)
        print("warning: --renew-window %d >= --subscription-ttl %d; clamping "
              "renew window to %d" % (renew_window, ttl, clamped),
              file=sys.stderr)
        renew_window = clamped

    queue_max = (args.queue_max if args.queue_max is not None
                 else _env_int("TRIGGER_QUEUE_MAX", QUEUE_MAX))
    if queue_max < 1:
        print("error: --queue-max must be >= 1 (got %d)" % queue_max,
              file=sys.stderr)
        return 2

    allow_nets: List[Any] = []
    if not bind_addr.is_loopback:
        for cidr in (args.allow_cidr or [DEFAULT_ALLOW_CIDR]):
            try:
                allow_nets.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                print("error: bad --allow-cidr %r" % cidr, file=sys.stderr)
                return 2

    store = TriggerStore(args.db)
    registry = SourceRegistry(Path(os.path.expanduser(args.sources_dir)))
    registry.load()
    try:
        server = TriggerServer((host, args.port), store, once=args.once,
                               token=token,
                               require_token_for_trigger=args.require_token_for_trigger,
                               allow_nets=allow_nets,
                               subscription_ttl=ttl,
                               renew_window=renew_window,
                               queue_max=queue_max,
                               sources=registry,
                               rules_path=args.rules_file,
                               blocked_log=args.blocked_log)
    except OSError as exc:
        log.error("cannot bind %s:%d: %s", host, args.port, exc)
        print("error: cannot bind %s:%d: %s" % (host, args.port, exc),
              file=sys.stderr)
        store.close()
        return 1

    log.info("mowu-trigger %s listening on http://%s:%d (db=%s, inbox-only)",
             VERSION, host, args.port, store.path)
    snapshot = server.rules.snapshot()
    log.info("L2 rules: %d loaded from %s (mtime=%s); blocked log=%s",
             snapshot["rules"], snapshot["rules_file"],
             snapshot["rules_mtime"] or "none", args.blocked_log)
    log.info("subscriptions: ttl=%ds renew_window=%ds grace=%.0fh",
             ttl, renew_window, SUBSCRIPTION_GRACE / 3600.0)
    log.info("fan-out queue: %d slots/subscriber shared by P0/P1/P2; when "
             "full, drop the earliest of the lowest tier present; drops log=%s",
             queue_max, os.path.expanduser(DEFAULT_DROP_LOG))
    janitor_stop = threading.Event()
    threading.Thread(target=_janitor_loop, args=(store, janitor_stop),
                     name="subscription-janitor", daemon=True).start()
    if generated:
        log.info("generated new bus token at %s (mode 600)", token_path)
    log.info("bus token file: %s — remote /subscribe, /events, /ack require "
             "the X-Trigger-Token header", token_path)
    if allow_nets:
        log.info("remote bus: clients allowed from %s (loopback always ok)",
                 ", ".join(str(n) for n in allow_nets))
    print("mowu-trigger %s listening on http://%s:%d  [remote subscribe needs "
          "X-Trigger-Token from %s]" % (VERSION, host, args.port, token_path),
          flush=True)

    if args.once:
        try:
            server.handle_request()
        finally:
            janitor_stop.set()
            server.server_close()
            store.close()
        return 0

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
        janitor_stop.set()
        server.server_close()
        store.close()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
