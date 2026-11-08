#!/usr/bin/env python3
"""selftest_levels.py — four-level acceptance for mowu-trigger 0.7.0.

Levels (highest first): ``event`` / ``notify`` / ``info`` / ``log``, default
``info``.  Legacy ``super`` and ``super_event`` are input aliases for ``event``
and must never be echoed or stored.

Part 1 (HTTP): a throwaway server on its own port + DB.  Four levels are
posted and every subscriber default / explicit override is asserted, plus the
alias folding and the 400 for an unknown level.

Part 2 (plugin, function level): the trigger-bus hooks are called directly —
no gateway, no poll thread.  Three top-level events must be inserted mid-turn
exactly once each by ``transform_tool_result``; ``notify`` must never
interrupt; ``pre_llm_call`` must never hand an inserted event out again and
must never inject ``info``.

    python3 selftest_levels.py
    PORT=18896 python3 selftest_levels.py

Never touches production 8791. Standard library only.
"""

from __future__ import annotations

import importlib
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
PORT = int(os.environ.get("PORT", "18796"))
BASE = "http://127.0.0.1:%d" % PORT
TOKEN_PATH = Path(os.path.expanduser("~/.hermes/secrets/trigger.token"))
TOKEN = TOKEN_PATH.read_text(encoding="utf-8").strip() if TOKEN_PATH.exists() else ""
OPENER = build_opener(ProxyHandler({}))
PLUGIN_PARENT = str(Path.home() / ".hermes" / "plugins")
PLUGIN_VERSION = "0.5.0"

RESULTS: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print("%s  %-62s %s" % ("PASS" if ok else "FAIL", name, detail))


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

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(2)  # SIGINT -> clean shutdown
            try:
                self.proc.wait(8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(3)


def db_rows(db: Path, sql: str, params: Tuple[Any, ...] = ()) -> List[Any]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def event_by_id(payload: Any, event_id: str) -> Optional[Dict[str, Any]]:
    for event in payload.get("events", []):
        if event.get("event_id") == event_id:
            return event
    return None


def ids_of(payload: Any) -> List[str]:
    return [str(e.get("event_id")) for e in payload.get("events", [])]


# --------------------------------------------------------------------------- #
# part 1: HTTP delivery matrix
# --------------------------------------------------------------------------- #

def http_checks(db: Path, tmp: Path) -> None:
    status, health = get("/health", token=False)
    check("/health levels == [event,notify,info,log]",
          status == 200
          and health.get("levels") == ["event", "notify", "info", "log"],
          "levels=%r" % (health.get("levels"),))
    check("/health version == 0.7.0", health.get("version") == "0.7.0",
          "version=%r" % health.get("version"))

    status, body = post("/trigger", {
        "source": "selftest", "kind": "chat", "actor": "probe",
        "text": "EVENT payload", "level": "event",
        "event_id": "lvl-event"})
    check("POST level=event -> 200, wake=true, priority 0",
          status == 200 and body.get("level") == "event"
          and body.get("wake") is True and body.get("delivered") is True
          and body.get("priority") == 0,
          "level=%r wake=%r priority=%r"
          % (body.get("level"), body.get("wake"), body.get("priority")))

    status, body = post("/trigger", {
        "source": "selftest", "kind": "chat", "text": "NOTIFY payload",
        "level": "notify", "event_id": "lvl-notify"})
    check("POST level=notify -> 200, delivered, wake-eligible",
          status == 200 and body.get("level") == "notify"
          and body.get("delivered") is True and body.get("wake") is True,
          "level=%r wake=%r" % (body.get("level"), body.get("wake")))

    status, body = post("/trigger", {
        "source": "selftest", "kind": "chat", "text": "INFO payload",
        "level": "info", "event_id": "lvl-info"})
    check("POST level=info -> 200, wake=false, priority=2",
          status == 200 and body.get("level") == "info"
          and body.get("wake") is False and body.get("priority") == 2,
          "level=%r" % body.get("level"))

    status, body = post("/trigger", {
        "source": "selftest", "kind": "chat", "text": "LOG payload",
        "level": "log", "event_id": "lvl-log"})
    check("POST level=log -> 200, delivered=false",
          status == 200 and body.get("level") == "log"
          and body.get("delivered") is False
          and body.get("wake") is False, "level=%r" % body.get("level"))

    status, body = post("/trigger", {
        "source": "selftest", "kind": "chat", "text": "default payload",
        "event_id": "lvl-default"})
    check("POST without level -> default info",
          status == 200 and body.get("level") == "info",
          "level=%r" % body.get("level"))

    status, body = post("/trigger", {
        "source": "selftest", "kind": "chat", "text": "alias super_event",
        "level": "super_event", "event_id": "lvl-alias-super-event"})
    row = db_rows(db, "SELECT level FROM events WHERE event_id=?",
                  ("lvl-alias-super-event",))
    check("POST level=super_event -> echo+store event",
          status == 200 and body.get("level") == "event"
          and bool(row) and row[0]["level"] == "event",
          "echo=%r stored=%r"
          % (body.get("level"), row[0]["level"] if row else None))

    status, body = post("/trigger", {
        "source": "selftest", "kind": "chat", "text": "alias super",
        "level": "super", "event_id": "lvl-alias-super"})
    row = db_rows(db, "SELECT level FROM events WHERE event_id=?",
                  ("lvl-alias-super",))
    check("POST level=super -> echo+store event",
          status == 200 and body.get("level") == "event"
          and bool(row) and row[0]["level"] == "event",
          "echo=%r stored=%r"
          % (body.get("level"), row[0]["level"] if row else None))

    status, body = post("/trigger", {
        "source": "selftest", "kind": "chat", "text": "bad level",
        "level": "warn", "event_id": "lvl-bad"})
    check("POST level=warn -> 400 error=bad_level",
          status == 400 and body.get("error") == "bad_level",
          "status=%s body=%r" % (status, body))

    status, hermes = get("/events?subscriber=hermes-main&since=0&limit=100")
    check("hermes default -> event+notify, no info/log",
          status == 200
          and event_by_id(hermes, "lvl-event") is not None
          and event_by_id(hermes, "lvl-notify") is not None
          and event_by_id(hermes, "lvl-alias-super") is not None
          and event_by_id(hermes, "lvl-alias-super-event") is not None
          and event_by_id(hermes, "lvl-info") is None
          and event_by_id(hermes, "lvl-log") is None,
          "ids=%s" % ids_of(hermes))
    check("/events echoes levels and every event carries level",
          hermes.get("levels") == ["event", "notify"]
          and all(e.get("level") for e in hermes.get("events", [])),
          "levels=%r" % (hermes.get("levels"),))

    status, worker = get("/events?subscriber=worker-levels&since=0&limit=100")
    check("worker default -> event+notify+info, no log",
          status == 200
          and event_by_id(worker, "lvl-event") is not None
          and event_by_id(worker, "lvl-notify") is not None
          and event_by_id(worker, "lvl-info") is not None
          and event_by_id(worker, "lvl-default") is not None
          and event_by_id(worker, "lvl-log") is None,
          "ids=%s" % ids_of(worker))
    check("worker /events levels == [event,info,notify]",
          worker.get("levels") == ["event", "info", "notify"],
          "levels=%r" % (worker.get("levels"),))

    status, only_info = get(
        "/events?subscriber=worker-levels&since=0&levels=info&limit=100")
    check("?levels=info -> only info",
          status == 200
          and all(str(e.get("level")) == "info"
                  for e in only_info.get("events", []))
          and event_by_id(only_info, "lvl-info") is not None
          and event_by_id(only_info, "lvl-event") is None,
          "levels=%r ids=%s" % (only_info.get("levels"),
                                ids_of(only_info)))

    status, event_info = get(
        "/events?subscriber=worker-levels&since=0&levels=event,info&limit=100")
    check("?levels=event,info -> exactly those two levels",
          status == 200
          and event_info.get("levels") == ["event", "info"]
          and {str(e.get("level")) for e in event_info.get("events", [])}
          == {"event", "info"},
          "levels=%r ids=%s" % (event_info.get("levels"),
                                ids_of(event_info)))

    status, alias_levels = get(
        "/events?subscriber=worker-levels&since=0&levels=super_event&limit=100")
    check("?levels=super_event -> folded to event",
          status == 200 and alias_levels.get("levels") == ["event"]
          and {str(e.get("level")) for e in alias_levels.get("events", [])}
          == {"event"},
          "levels=%r ids=%s" % (alias_levels.get("levels"),
                                ids_of(alias_levels)))

    status, body = get(
        "/events?subscriber=worker-levels&since=0&levels=log&limit=10")
    check("?levels=log -> 400 (log is never deliverable)",
          status == 400, "status=%s body=%r" % (status, body))

    status, stats = get("/stats?subscriber=hermes-main")
    check("/stats carries levels + by_level (event counted)",
          status == 200
          and stats.get("levels") == ["event", "notify", "info", "log"]
          and int(stats.get("by_level", {}).get("event") or 0) >= 3
          and int(stats.get("by_level", {}).get("log") or 0) >= 1,
          "by_level=%r" % (stats.get("by_level"),))

    # Migration is idempotent: re-running TriggerStore over an already
    # migrated DB must not raise and must not rewrite levels.
    sys.path.insert(0, str(ROOT))
    import server as server_mod  # noqa: E402
    store = server_mod.TriggerStore(str(db))
    store.close()
    levels = sorted({str(r["level"]) for r in
                     db_rows(db, "SELECT level FROM events")})
    check("migration idempotent; stored levels all canonical",
          levels == ["event", "info", "log", "notify"],
          "levels=%s" % levels)


# --------------------------------------------------------------------------- #
# part 2: plugin hooks (no gateway)
# --------------------------------------------------------------------------- #

def ev(event_id: str, level: str, text: str, ident: int,
       actor: str = "") -> Dict[str, Any]:
    return {"id": ident, "event_id": event_id, "source": "selftest",
            "kind": "chat", "level": level, "priority": 0, "actor": actor,
            "text": text, "ts": "2026-11-08T19:00:00+08:00",
            "disposition": "delivered"}


def install_gateway_stubs() -> None:
    """Minimal stand-ins for gateway.config / gateway.platforms.event.

    The plugin imports MessageEvent lazily inside the wake path.  A stub with
    the real field names (``internal``, ``inherit_channel_pin``, ``metadata``)
    lets us assert the constructed event without booting a gateway.
    """
    import dataclasses
    import types

    if "gateway.platforms.event" in sys.modules:
        return
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    config = types.ModuleType("gateway.config")

    class Platform:
        FEISHU = "feishu"

    config.Platform = Platform
    platforms = types.ModuleType("gateway.platforms")
    platforms.__path__ = []
    event_mod = types.ModuleType("gateway.platforms.event")

    class MessageType:
        TEXT = "text"

    @dataclasses.dataclass
    class MessageEvent:
        text: str
        message_type: Any = None
        source: Any = None
        internal: bool = False
        inherit_channel_pin: bool = False
        metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)

    event_mod.MessageEvent = MessageEvent
    event_mod.MessageType = MessageType
    gateway.config = config
    gateway.platforms = platforms
    platforms.event = event_mod
    sys.modules["gateway"] = gateway
    sys.modules["gateway.config"] = config
    sys.modules["gateway.platforms"] = platforms
    sys.modules["gateway.platforms.event"] = event_mod


def plugin_checks(tmp: Path) -> None:
    os.environ["TRIGGER_BUS_URL"] = BASE
    os.environ["TRIGGER_BUS_SUBSCRIBER"] = "selftest-levels"
    os.environ["TRIGGER_BUS_CURSOR"] = str(tmp / "plugin-cursor.json")
    os.environ["TRIGGER_BUS_LOCK"] = str(tmp / "plugin.lock")
    os.environ.pop("TRIGGER_BUS_DISABLED", None)
    sys.path.insert(0, PLUGIN_PARENT)
    mod = importlib.import_module("trigger-bus")
    check("plugin imports at version %s" % PLUGIN_VERSION,
          mod.PLUGIN_VERSION == PLUGIN_VERSION,
          "version=%r" % mod.PLUGIN_VERSION)

    state = mod._BusState()
    mod._STATE = state

    # ---- three top-level events: mid-turn insert, exactly once each ------ #
    for index, text in enumerate(("EVENT-ONE", "EVENT-TWO", "EVENT-THREE")):
        state.events.append(ev("event-%d" % (index + 1), "event",
                               text, 101 + index))
    state.high_water = 103

    results = []
    for _ in range(5):
        results.append(mod._on_transform_tool_result(
            tool_name="terminal", args={}, result="TOOL RESULT"))
    hits = [r for r in results if r is not None]
    check("transform_tool_result: 5 calls -> exactly 3 inserts",
          len(hits) == 3 and results[3] is None and results[4] is None,
          "non-None=%d" % len(hits))
    check("transform: each insert starts from the original tool output",
          all(r.startswith("TOOL RESULT") for r in hits), "")
    check("transform: every insert carries [EVENT] + the interrupt wording",
          all("[EVENT] 立刻停下当前任务" in r
              and "处理完再回到刚才的事。" in r for r in hits), "")
    check("transform: prefix is [EVENT] and never [SUPER]",
          all("[SUPER]" not in r for r in hits), "")
    check("transform: three distinct events, no text duplicated",
          [r.count("[EVENT]") for r in hits] == [1, 1, 1]
          and all(sum(1 for h in hits if marker in h) == 1
                  for marker in ("EVENT-ONE", "EVENT-TWO", "EVENT-THREE")),
          "markers=%s" % [[m in h for m in
                           ("EVENT-ONE", "EVENT-TWO", "EVENT-THREE")]
                          for h in hits])
    check("transform: queue drained, marked set has 3 keys",
          not state.events and len(state.interrupt_done) == 3,
          "queued=%d done=%d" % (len(state.events),
                                 len(state.interrupt_done)))
    check("transform: at-most-once cursor reached the last event id",
          state.interrupt_cursor == 103,
          "interrupt_cursor=%s" % state.interrupt_cursor)

    check("pre_llm_call after mid-turn inserts -> nothing re-injected",
          mod._on_pre_llm_call(session_id="t") is None, "")

    # ---- notify never interrupts, but is injected at the boundary -------- #
    state.events.append(ev("notify-1", "notify", "NOTIFY-BOUNDARY", 201))
    check("transform: notify is NOT inserted mid-turn",
          mod._on_transform_tool_result(result="X") is None, "")
    context = mod._on_pre_llm_call(session_id="t")
    text = (context or {}).get("context", "")
    check("pre_llm_call: notify injected at the turn boundary",
          "NOTIFY-BOUNDARY" in text, "context=%r" % text[:120])

    # ---- notify queued before an event: event jumps the queue ------------ #
    state.events.append(ev("notify-2", "notify", "NOTIFY-WAIT", 202))
    state.events.append(ev("event-4", "event", "EVENT-NOW", 203))
    inserted = mod._on_transform_tool_result(result="R")
    check("transform: skips the older notify, takes the top-level event",
          inserted is not None and "EVENT-NOW" in inserted
          and "NOTIFY-WAIT" not in inserted
          and len(state.events) == 1
          and state.events[0]["event_id"] == "notify-2",
          "queued=%d" % len(state.events))
    context = mod._on_pre_llm_call(session_id="t")
    text = (context or {}).get("context", "")
    check("pre_llm_call: the skipped notify is delivered afterwards",
          "NOTIFY-WAIT" in text and "EVENT-NOW" not in text,
          "context=%r" % text[:120])

    # ---- info never reaches the session ---------------------------------- #
    state.events.append(ev("info-1", "info", "INFO-ONLY", 210))
    check("pre_llm_call: info-only queue -> None (info never injected)",
          mod._on_pre_llm_call(session_id="t") is None, "")

    state.events.append(ev("notify-3", "notify", "MIXED-NOTIFY", 211))
    state.events.append(ev("info-2", "info", "INFO-SKIPPED", 212))
    context = mod._on_pre_llm_call(session_id="t")
    text = (context or {}).get("context", "")
    check("pre_llm_call: notify injected, info in the same batch discarded",
          "MIXED-NOTIFY" in text and "INFO-SKIPPED" not in text,
          "context=%r" % text[:120])

    # ---- a fresh top-level event at the boundary is injected ------------- #
    state.events.append(ev("event-5", "event", "EVENT-AT-BOUNDARY", 213))
    context = mod._on_pre_llm_call(session_id="t")
    text = (context or {}).get("context", "")
    check("pre_llm_call: a fresh event is injected at the boundary",
          "EVENT-AT-BOUNDARY" in text, "context=%r" % text[:120])

    check("transform: non-str result -> None",
          mod._on_transform_tool_result(result={"not": "a string"}) is None, "")
    state.events.append(ev("event-disabled", "event", "NOPE", 214))
    os.environ["TRIGGER_BUS_DISABLED"] = "1"
    check("transform: TRIGGER_BUS_DISABLED=1 -> None",
          mod._on_transform_tool_result(result="X") is None, "")
    os.environ.pop("TRIGGER_BUS_DISABLED", None)

    check("plugin: legacy 'super' / 'super_event' fold to event",
          mod._level({"level": "super"}) == "event"
          and mod._level({"level": "super_event"}) == "event"
          and mod._level({"level": "notify"}) == "notify", "")

    # ---- wake path: event = true interrupt, notify = idle-only ---------- #
    install_gateway_stubs()
    event_batch = [ev("wake-e1", "event", "WAKE-EVENT", 301, actor="Steve")]
    event_text = mod._wake_text(event_batch)
    built = mod._build_wake_event(event_text, mod._wake_internal(event_batch),
                                  "SRC", mod._interrupt_origin(event_batch[0]))
    check("wake: text starts with [EVENT source/kind ts]",
          event_text.startswith(
              "[EVENT selftest/chat 2026-11-08T19:00:00+08:00] WAKE-EVENT"),
          "text=%r" % event_text[:100])
    check("wake: MessageEvent(internal=False) for level=event",
          built.internal is False, "internal=%r" % built.internal)
    check("wake: metadata.interrupt_origin == 'selftest · Steve'",
          built.metadata.get("interrupt_origin") == "selftest · Steve",
          "metadata=%r" % (built.metadata,))

    notify_batch = [ev("wake-n1", "notify", "WAKE-NOTIFY", 302, actor="Mail")]
    built_n = mod._build_wake_event(
        mod._wake_text(notify_batch), mod._wake_internal(notify_batch),
        "SRC", mod._interrupt_origin(notify_batch[0]))
    check("wake: MessageEvent(internal=True) for level=notify",
          built_n.internal is True, "internal=%r" % built_n.internal)
    check("wake: notify also carries a non-empty interrupt_origin",
          bool(built_n.metadata.get("interrupt_origin")),
          "metadata=%r" % (built_n.metadata,))

    # End-to-end through _wake with a fake adapter (still no real gateway).
    import asyncio
    import threading
    import types

    class FakeAdapter:
        def __init__(self) -> None:
            self.events: List[Any] = []

        async def handle_message(self, event: Any) -> None:
            self.events.append(event)

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    adapter = FakeAdapter()
    state.gateway = types.SimpleNamespace(adapters={"feishu": adapter})
    state.source = "feishu-dm"
    state.loop = loop
    state.last_wake = float("-inf")
    mod._wake(state, event_batch)
    deadline = time.time() + 2
    while not adapter.events and time.time() < deadline:
        time.sleep(0.02)
    sent = adapter.events[0] if adapter.events else None
    check("wake path: adapter receives MessageEvent(internal=False)",
          sent is not None and sent.internal is False
          and sent.metadata.get("interrupt_origin") == "selftest · Steve",
          "sent=%d internal=%r"
          % (len(adapter.events), sent.internal if sent else None))
    adapter.events.clear()
    mod._wake(state, notify_batch)
    deadline = time.time() + 2
    while not adapter.events and time.time() < deadline:
        time.sleep(0.02)
    sent_n = adapter.events[0] if adapter.events else None
    check("wake path: adapter receives MessageEvent(internal=True) for notify",
          sent_n is not None and sent_n.internal is True,
          "sent=%d internal=%r"
          % (len(adapter.events), sent_n.internal if sent_n else None))
    loop.call_soon_threadsafe(loop.stop)

    # ---- 5 s coalescing: 3 events -> 1 message, 另有 2 条 ---------------- #
    with state.lock:
        state.wake_pending = []
        state.wake_due = None
    batch = [ev("co-1", "event", "CO-1", 311),
             ev("co-2", "event", "CO-2", 312),
             ev("co-3", "event", "CO-3", 313)]
    mod._buffer_wake(state, batch[:1])
    mod._buffer_wake(state, batch[1:])
    check("coalesce: window still open -> no message yet",
          mod._take_due_wake(state) is None, "")
    state.wake_due = time.monotonic() - 0.01
    merged_batch = mod._take_due_wake(state)
    check("coalesce: 3 events in 5 s -> exactly 1 message",
          merged_batch is not None and len(merged_batch) == 3,
          "batch=%r" % (len(merged_batch) if merged_batch else None))
    merged_text = mod._wake_text(merged_batch or [])
    check("coalesce: text lists 1 event + 「另有 2 条」",
          merged_text.count("[EVENT ") == 1 and "另有 2 条" in merged_text,
          "text=%r" % merged_text)
    check("coalesce: second flush emits nothing",
          mod._take_due_wake(state) is None, "")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mowu-levels-"))
    db = tmp / "levels.db"
    rules = tmp / "no-rules.json"
    blocked = tmp / "blocked.jsonl"
    log_path = tmp / "server.log"
    server = Server(db, rules, blocked, log_path)
    try:
        http_checks(db, tmp)
        plugin_checks(tmp)
    finally:
        server.stop()

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
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("-" * 78)
    print("%d/%d PASS, %d FAIL   (temp was %s)"
          % (passed, len(RESULTS), failed, tmp))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
