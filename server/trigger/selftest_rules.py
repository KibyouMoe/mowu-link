#!/usr/bin/env python3
"""selftest_rules.py — end-to-end acceptance for mowu-trigger 0.7.0 (L2).

Starts a throwaway server (port 18795, temp DB), exercises the four envelope
levels and every rule action with real HTTP POSTs, restarts it to prove
rate-limit backfill, then breaks the rule file on purpose to prove the service
keeps the old rules.

Levels (0.7.0, highest first):
  * event  -> delivered, wake-eligible, mid-turn interrupt
  * notify -> delivered, wake-eligible, injected at the next turn boundary
  * info   -> delivered to worker subscriptions, never wakes
  * log    -> archive only: not delivered, not queued, still counted

    python3 selftest_rules.py
    PORT=18895 python3 selftest_rules.py

Never touches production 8791: own port, own DB, own rules and blocked log.
Standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "18795"))
BASE = "http://127.0.0.1:%d" % PORT
TOKEN_PATH = Path(os.path.expanduser("~/.hermes/secrets/trigger.token"))
TOKEN = TOKEN_PATH.read_text(encoding="utf-8").strip() if TOKEN_PATH.exists() else ""
OPENER = build_opener(ProxyHandler({}))

RESULTS: List[Tuple[str, bool, str]] = []

RULES: Dict[str, Any] = {
    "version": 1,
    "rules": [
        {"name": "email-downgrade", "enabled": True,
         "match": {"source": "email"},
         "action": {"type": "downgrade", "to": 2}},
        {"name": "mc-rate", "enabled": True,
         "match": {"source": "mc_chat", "kind": "chat",
                   "actor_regex": "^Ratey$"},
         "action": {"type": "rate_limit", "max": 30, "window_s": 60,
                    "on_exceed": "drop"}},
        {"name": "dup-window", "enabled": True,
         "match": {"source": "*"},
         "action": {"type": "dedupe", "window_s": 30,
                    "by": "text+actor"}},
        {"name": "quiet-thread", "enabled": True,
         "match": {"source": "*"},
         "action": {"type": "quiet", "window_s": 300, "by": "thread_key",
                    "to": 2}},
    ],
}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print("%s  %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


def request(path: str, body: Optional[Dict[str, Any]] = None,
            token: bool = False) -> Tuple[int, Any]:
    data = None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if token and TOKEN:
        headers["X-Trigger-Token"] = TOKEN
    req = Request(BASE + path, data=data, headers=headers,
                  method="POST" if body is not None else "GET")
    try:
        with OPENER.open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
        return exc.code, payload


def post(path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
    return request(path, body)


def get(path: str, token: bool = True) -> Tuple[int, Any]:
    return request(path, None, token=token)


class Server:
    def __init__(self, db: Path, rules: Path, blocked: Path, log: Path) -> None:
        self.log_path = log
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"),
             "--port", str(PORT), "--db", str(db), "--log", str(log),
             "--rules-file", str(rules), "--blocked-log", str(blocked)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                status, _ = get("/health", token=False)
                if status == 200:
                    return
            except Exception:
                pass
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError("server did not come up on %s (log: %s)"
                           % (BASE, self.log_path))

    def text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(2)  # SIGINT -> clean shutdown
            try:
                self.proc.wait(8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(3)


def db_query(db: Path, sql: str, params: Tuple[Any, ...] = ()) -> List[Any]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def row_for(db: Path, event_id: str):
    rows = db_query(db, "SELECT * FROM events WHERE event_id=?", (event_id,))
    return rows[0] if rows else None


def event_by_id(events: Any, event_id: str) -> Optional[Dict[str, Any]]:
    for event in events.get("events", []):
        if event.get("event_id") == event_id:
            return event
    return None


def legacy_migration_check(tmp: Path) -> None:
    """A 0.5.x DB gains level/disposition; old rows are not rewritten."""
    legacy = tmp / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " event_id TEXT UNIQUE NOT NULL, source TEXT NOT NULL, kind TEXT,"
        " priority INTEGER, actor TEXT, text TEXT NOT NULL,"
        " payload_json TEXT, ts TEXT, dedupe_key TEXT, created_at REAL);"
        "INSERT INTO events (event_id, source, kind, priority, actor, text,"
        " dedupe_key, created_at) VALUES ('old-1','mc_chat','chat',2,'Steve',"
        " 'old row','mc_chat:old-1', 1.0);")
    conn.commit()
    conn.close()
    server = Server(legacy, tmp / "missing-rules.json",
                    tmp / "legacy-blocked.jsonl", tmp / "legacy.log")
    try:
        cols = [r["name"] for r in db_query(
            legacy, "PRAGMA table_info(events)")]
        row = row_for(legacy, "old-1")
        check("migration: events.disposition added", "disposition" in cols,
              "columns=%s" % ",".join(cols))
        check("migration: events.level added", "level" in cols,
              "columns=%s" % ",".join(cols))
        check("migration: existing row reads delivered/info",
              row is not None and row["disposition"] == "delivered"
              and row["level"] == "info",
              "disposition=%r level=%r"
              % (row["disposition"] if row else None,
                 row["level"] if row else None))
        check("migration: existing row text untouched",
              row is not None and row["text"] == "old row",
              "text=%r" % (row["text"] if row else None))
    finally:
        server.stop()


def main() -> int:
    if not TOKEN:
        print("error: %s is missing; start server.py once to create it"
              % TOKEN_PATH, file=sys.stderr)
        return 2
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
            print("error: port %d already in use; use PORT=..." % PORT,
                  file=sys.stderr)
            return 2
    except OSError:
        pass

    tmp = Path(tempfile.mkdtemp(prefix="trigger-rules-selftest-"))
    db = tmp / "t6.db"
    rules_path = tmp / "trigger-rules.json"
    blocked_path = tmp / "trigger-blocked.jsonl"
    print("temp: %s   port: %d" % (tmp, PORT), flush=True)

    legacy_migration_check(tmp)

    server = Server(db, rules_path, blocked_path, tmp / "server-1.log")
    try:
        # ------------------------------------------------------------ #
        # 1. no rule file -> everything passes; levels decide delivery
        # ------------------------------------------------------------ #
        status, health = get("/health", token=False)
        check("no rules: GET /health 200 version=0.7.0", status == 200
              and health.get("version") == "0.7.0",
              "version=%r" % health.get("version"))
        check("no rules: rules=0, rules_mtime=null",
              health.get("rules") == 0 and health.get("rules_mtime") is None,
              "rules=%r mtime=%r" % (health.get("rules"),
                                     health.get("rules_mtime")))

        status, body = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "actor": "Steve",
            "text": "level event message", "level": "event",
            "event_id": "r-00-event"})
        check("level=event: delivered, wake=true, default priority 0",
              status == 200 and body.get("level") == "event"
              and body.get("delivered") is True and body.get("wake") is True
              and body.get("priority") == 0,
              "level=%r delivered=%r wake=%r priority=%r"
              % (body.get("level"), body.get("delivered"),
                 body.get("wake"), body.get("priority")))

        status, body = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "actor": "Steve",
            "text": "level info message", "level": "info",
            "event_id": "r-00-info"})
        check("level=info: delivered, wake=false, default priority 2",
              status == 200 and body.get("level") == "info"
              and body.get("delivered") is True and body.get("wake") is False
              and body.get("priority") == 2,
              "level=%r delivered=%r wake=%r priority=%r"
              % (body.get("level"), body.get("delivered"),
                 body.get("wake"), body.get("priority")))

        _, stats_before = get("/stats?subscriber=stats-probe")
        status, body = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "actor": "Steve",
            "text": "level log message", "level": "log",
            "event_id": "r-00-log"})
        check("level=log: stored, delivered=false, wake=false", status == 200
              and body.get("level") == "log"
              and body.get("delivered") is False
              and body.get("wake") is False,
              "level=%r delivered=%r wake=%r"
              % (body.get("level"), body.get("delivered"),
                 body.get("wake")))
        log_row = row_for(db, "r-00-log")
        check("level=log: row exists in the events table",
              log_row is not None and log_row["level"] == "log",
              "level=%r" % (log_row["level"] if log_row else None))
        _, stats_after = get("/stats?subscriber=stats-probe")
        check("level=log: /stats unread +1",
              stats_after.get("unread") == stats_before.get("unread", 0) + 1,
              "before=%r after=%r" % (stats_before.get("unread"),
                                      stats_after.get("unread")))

        status, denied = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "actor": "Steve",
            "text": "bad level", "level": "warn", "event_id": "r-00-bad"})
        check("level=warn -> 400", status == 400, "status=%s" % status)

        status, hermes = get("/events?subscriber=hermes-main&since=0&limit=50")
        check("levels: hermes default sees event, not info/log",
              status == 200 and event_by_id(hermes, "r-00-event") is not None
              and event_by_id(hermes, "r-00-info") is None
              and event_by_id(hermes, "r-00-log") is None,
              "ids=%s" % [e.get("event_id") for e in hermes.get("events", [])])

        status, worker = get("/events?subscriber=worker-a&since=0&limit=50")
        check("levels: worker default sees event+info, not log",
              status == 200 and event_by_id(worker, "r-00-event") is not None
              and event_by_id(worker, "r-00-info") is not None
              and event_by_id(worker, "r-00-log") is None,
              "ids=%s" % [e.get("event_id") for e in worker.get("events", [])])

        status, worker_event = get(
            "/events?subscriber=worker-a&since=0&levels=event&limit=50")
        check("levels: worker with levels=event gets only event",
              status == 200
              and event_by_id(worker_event, "r-00-event") is not None
              and event_by_id(worker_event, "r-00-info") is None
              and event_by_id(worker_event, "r-00-log") is None,
              "ids=%s" % [e.get("event_id")
                          for e in worker_event.get("events", [])])

        status, archive = get(
            "/events?subscriber=hermes-main&since=0&include_log=1&limit=50")
        check("levels: include_log=1 exposes the archived log row",
              status == 200
              and event_by_id(archive, "r-00-log") is not None,
              "ids=%s" % [e.get("event_id")
                          for e in archive.get("events", [])])

        # ------------------------------------------------------------ #
        # 2. write rules -> the very next POST reloads and downgrades
        # ------------------------------------------------------------ #
        rules_path.write_text(json.dumps(RULES, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        status, body = post("/trigger", {
            "source": "email", "kind": "email", "level": "info",
            "actor": "hr@acme.example.com", "text": "offer 面试",
            "priority": 3, "event_id": "r-01-downgrade"})
        check("hot reload: next POST uses new rules (downgraded 3 -> 2)",
              status == 200 and body.get("disposition") == "downgraded"
              and body.get("priority") == 2,
              "disposition=%r priority=%r" % (body.get("disposition"),
                                              body.get("priority")))
        _, health = get("/health", token=False)
        check("hot reload: /health rules=4 + mtime set",
              health.get("rules") == 4 and health.get("rules_mtime"),
              "rules=%r mtime=%r" % (health.get("rules"),
                                     health.get("rules_mtime")))
        check("hot reload: log says 4 rules loaded",
              "rules: loaded 4 rule(s)" in server.text(), "")
        row = row_for(db, "r-01-downgrade")
        check("downgrade: stored priority=2 disposition=downgraded",
              row is not None and row["priority"] == 2
              and row["disposition"] == "downgraded",
              "priority=%r disposition=%r"
              % (row["priority"] if row else None,
                 row["disposition"] if row else None))

        # ------------------------------------------------------------ #
        # 3. rate_limit: 30 pass, the 31st is dropped; not delivered
        # ------------------------------------------------------------ #
        for index in range(30):
            status, body = post("/trigger", {
                "source": "mc_chat", "kind": "chat", "level": "info",
                "actor": "Ratey", "text": "rate-%02d" % index,
                "priority": 2, "event_id": "r-02-rate-%02d" % index})
            if status != 200 or body.get("disposition") != "delivered":
                break
        status, body = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "info",
            "actor": "Ratey", "text": "rate-30", "priority": 2,
            "event_id": "r-02-rate-30"})
        check("rate_limit: 31st POST dropped by mc-rate", status == 200
              and body.get("disposition") == "dropped"
              and body.get("rule") == "mc-rate"
              and body.get("reason") == "rate_limit"
              and body.get("delivered") is False,
              "disposition=%r rule=%r reason=%r"
              % (body.get("disposition"), body.get("rule"),
                 body.get("reason")))
        dropped_id = int(body["id"])
        row = row_for(db, "r-02-rate-30")
        check("rate_limit: dropped event is stored, not delivered",
              row is not None and row["disposition"] == "dropped",
              "disposition=%r" % (row["disposition"] if row else None))
        status, events = get("/events?subscriber=e2e&since=0&limit=200")
        ids = [e["id"] for e in events.get("events", [])]
        check("rate_limit: dropped id absent from GET /events",
              status == 200 and dropped_id not in ids,
              "dropped_id=%d" % dropped_id)
        delivered = db_query(db, "SELECT COUNT(*) AS c FROM events"
                                " WHERE actor='Ratey'"
                                " AND disposition='delivered'")[0]["c"]
        dropped = db_query(db, "SELECT COUNT(*) AS c FROM events"
                               " WHERE actor='Ratey'"
                               " AND disposition='dropped'")[0]["c"]
        check("rate_limit: exactly 30 delivered / 1 dropped",
              delivered == 30 and dropped == 1,
              "delivered=%d dropped=%d" % (delivered, dropped))

        # ------------------------------------------------------------ #
        # 4. dedupe: same text+actor twice in 30 s -> second dropped
        # ------------------------------------------------------------ #
        _, first = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "info",
            "actor": "Dup", "text": "same text", "priority": 2,
            "event_id": "r-03-dup-1"})
        status, second = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "info",
            "actor": "Dup", "text": "same text", "priority": 2,
            "event_id": "r-03-dup-2"})
        check("dedupe: first passes, second dropped", first.get("disposition")
              == "delivered" and second.get("disposition") == "dropped"
              and second.get("reason") == "dedupe_window",
              "first=%r second=%r reason=%r"
              % (first.get("disposition"), second.get("disposition"),
                 second.get("reason")))

        # ------------------------------------------------------------ #
        # 5. quiet: second P0 on the same thread_key becomes P2
        # ------------------------------------------------------------ #
        _, q1 = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "event",
            "actor": "Quiet", "text": "thread first", "priority": 0,
            "thread_key": "thread-1", "event_id": "r-04-quiet-1"})
        status, q2 = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "event",
            "actor": "Quiet", "text": "thread second", "priority": 0,
            "thread_key": "thread-1", "event_id": "r-04-quiet-2"})
        check("quiet: first P0 stays P0", q1.get("priority") == 0
              and q1.get("disposition") == "delivered"
              and q1.get("wake") is True,
              "priority=%r disposition=%r"
              % (q1.get("priority"), q1.get("disposition")))
        check("quiet: second P0 downgraded to P2 (no wake)",
              status == 200 and q2.get("priority") == 2
              and q2.get("disposition") == "downgraded"
              and q2.get("wake") is False,
              "priority=%r disposition=%r wake=%r"
              % (q2.get("priority"), q2.get("disposition"),
                 q2.get("wake")))
        row = row_for(db, "r-04-quiet-2")
        check("quiet: second stored as priority=2", row is not None
              and row["priority"] == 2,
              "priority=%r" % (row["priority"] if row else None))

        # ------------------------------------------------------------ #
        # 6. /blocked: rule-blocked records only, newest first
        # ------------------------------------------------------------ #
        status, blocked = get("/blocked?limit=100")
        entries = blocked.get("blocked", [])
        check("/blocked: 2 records newest first (dedupe, rate)",
              status == 200 and blocked.get("count") == 2
              and [e["reason"] for e in entries]
              == ["dedupe_window", "rate_limit"],
              "count=%r reasons=%s" % (blocked.get("count"),
                                       [e.get("reason") for e in entries]))
        required = {"at", "source", "kind", "level", "actor", "event_id",
                    "rule", "reason", "text"}
        check("/blocked: every entry has the documented fields",
              all(required <= set(e) for e in entries),
              "keys=%s" % sorted(entries[0]) if entries else "no entries")
        status, mine = get("/blocked?source=mc_chat&limit=100")
        check("/blocked?source=mc_chat: only mc_chat",
              status == 200 and mine.get("count") == 2
              and all(e["source"] == "mc_chat" for e in mine.get("blocked", [])),
              "count=%r" % mine.get("count"))
        status, other = get("/blocked?source=email")
        check("/blocked?source=email: 0 (downgrade is not a block)",
              status == 200 and other.get("count") == 0,
              "count=%r" % other.get("count"))
        status, limited = get("/blocked?limit=1")
        check("/blocked?limit=1: exactly 1", status == 200
              and limited.get("count") == 1,
              "count=%r" % limited.get("count"))
        status, denied = get("/blocked", token=False)
        check("/blocked without token -> 401", status == 401,
              "status=%s" % status)
        lines = blocked_path.read_text(encoding="utf-8").strip().splitlines()
        check("blocked JSONL: one line per block",
              len(lines) == 2 and all(json.loads(line) for line in lines),
              "lines=%d" % len(lines))

        # ------------------------------------------------------------ #
        # 7. restart: rate window is backfilled from the DB
        # ------------------------------------------------------------ #
        server.stop()
        server = Server(db, rules_path, blocked_path, tmp / "server-2.log")
        _, health = get("/health", token=False)
        check("restart: rules still 4", health.get("rules") == 4,
              "rules=%r" % health.get("rules"))
        check("restart: log reports 30 backfilled hits",
              "rate window backfilled 30 hit(s)" in server.text(), "")
        status, body = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "info",
            "actor": "Ratey", "text": "rate-after-restart", "priority": 2,
            "event_id": "r-05-rate-restart"})
        check("restart: next Ratey POST still rate-limited", status == 200
              and body.get("disposition") == "dropped"
              and body.get("reason") == "rate_limit",
              "disposition=%r reason=%r"
              % (body.get("disposition"), body.get("reason")))

        # ------------------------------------------------------------ #
        # 8. hot reload changes behaviour (disable email-downgrade)
        # ------------------------------------------------------------ #
        changed = json.loads(json.dumps(RULES))
        changed["rules"][0]["enabled"] = False
        rules_path.write_text(json.dumps(changed, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        status, body = post("/trigger", {
            "source": "email", "kind": "email", "level": "info",
            "actor": "hr@acme.example.com", "text": "no downgrade now",
            "priority": 3, "event_id": "r-06-reload-off"})
        check("reload: disabled rule no longer downgrades", status == 200
              and body.get("disposition") == "delivered"
              and body.get("priority") == 3,
              "disposition=%r priority=%r"
              % (body.get("disposition"), body.get("priority")))
        check("reload: second 'loaded 4 rule(s)' log line",
              server.text().count("rules: loaded 4 rule(s)") >= 2, "")

        # ------------------------------------------------------------ #
        # 9. broken JSON: service survives, old rules stay, one WARN
        # ------------------------------------------------------------ #
        rules_path.write_text("{ this is not json", encoding="utf-8")
        status, body = post("/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "info",
            "actor": "Bystander", "text": "bad rules but alive",
            "priority": 2, "event_id": "r-07-bad-json"})
        check("bad JSON: POST still 200 + delivered (old rules)", status == 200
              and body.get("disposition") == "delivered",
              "status=%s disposition=%r" % (status, body.get("disposition")))
        _, health = get("/health", token=False)
        check("bad JSON: /health still 4 rules + rules_error set",
              health.get("rules") == 4 and health.get("rules_error"),
              "rules=%r error=%r" % (health.get("rules"),
                                     health.get("rules_error")))
        check("bad JSON: WARN keeps previous rules",
              "cannot load" in server.text()
              and "keeping 4 previous rule(s)" in server.text(), "")
        check("bad JSON: server did not exit", server.proc.poll() is None, "")
    finally:
        server.stop()

    # port released?
    released = False
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.2):
                time.sleep(0.1)
        except OSError:
            released = True
            break
    check("shutdown: port %d released" % PORT, released, "")
    shutil.rmtree(tmp, ignore_errors=True)
    check("temp DB discarded (tmp dir removed)",
          not db.exists() and not tmp.exists(), str(tmp))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("-" * 78)
    print("%d/%d PASS, %d FAIL   (temp was %s)" %
          (passed, len(RESULTS), failed, tmp))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
