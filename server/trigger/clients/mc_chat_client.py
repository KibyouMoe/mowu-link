#!/usr/bin/env python3
"""mc_chat_client — Minecraft chat → mowu-trigger inbox.

One source, one client. This process owns *everything* about the MC source:

    GET :8765/stream (SSE) → normalise to envelope → L1 business filter
    → POST /trigger

The server knows nothing about SSE or Minecraft; it just stores what we send
and applies the cross-source L2 rules (``~/.hermes/trigger-rules.json``).

**L1 only** (0.6.0): this client filters exactly one business thing — a chat
line sent by our own player is not sent back to us (``self_message``), unless
``--include-self`` is given.  Rate limits, dedupe windows and allow/deny lists
are L2 and must be configured on the server, not here.

Priority is source-intrinsic here: a death message or 溪流/SuperCreek is P0
because of what it means in this game; those are sent as ``level=event``
(top level: wake-eligible and can interrupt mid-turn). Everything else is
``level=info``: delivered to worker subscriptions but never injected or
woken. L2 can only lower urgency (``downgrade``), never raise it.

Usage:
    python3 mc_chat_client.py                 # run forever, resuming from cursor
    python3 mc_chat_client.py --dry-run       # never POST, just log
    python3 mc_chat_client.py --once-seconds 10
    python3 mc_chat_client.py --url http://127.0.0.1:8799/stream
    python3 mc_chat_client.py --no-cursor     # old behaviour: no cursor file
    python3 mc_chat_client.py --lookback 5    # re-accept events newer than
                                              # (saved_at - 5s); default 0 for MC

Cursor (``clients/state/mc_chat_cursor.json``)::

    {"source": "mc_chat", "last_seq": 123, "saved_at": "<ISO+08:00>",
     "bridge": "http://127.0.0.1:8765"}

The bridge has no boot id, so the cursor doubles as a session detector: the
MC chat box is cleared on every login, therefore a new bridge session means we
must **not** replay.  On connect we read ``/state.events.lastSeq``:

    lastSeq <  stored last_seq  -> new session, re-baseline, no replay (INFO)
    lastSeq >= stored last_seq  -> same session, continue from last_seq

A missing/corrupt cursor is baselined at the current ``lastSeq`` with a
WARNING — never replay history ("宁漏不重").  ``--no-cursor`` disables all of
this and behaves exactly like the pre-cursor client.

Standard library only. Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from filterwall import business_filter, log_skip, schema_problem
from localbus import LocalBus

HERE = Path(__file__).resolve().parent
DEFAULT_STREAM_URL = "http://127.0.0.1:8765/stream"
DEFAULT_TRIGGER_URL = "http://127.0.0.1:8791/trigger"
DEFAULT_LOG = HERE / "mc_chat_client.log"
DEFAULT_CURSOR = HERE / "state" / "mc_chat_cursor.json"
DEFAULT_BUS_PORT = 8792
DEFAULT_DROPS = HERE / "drops.log"
DEFAULT_LOOKBACK = 0.0     # MC chat has no history to look back at
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
RETRY_DELAYS = [0.5, 1.0, 2.0, 4.0]
SOURCE_NAME = "mc_chat"
STATE_TIMEOUT = 2.0
TZ_CST = timezone(timedelta(hours=8))

# Loopback must never go through HTTP(S)_PROXY from the environment.
_OPENER = build_opener(ProxyHandler({}))

# 溪流 / SuperCreek -> highest priority
STREAM_RE = re.compile(r"溪流|supercreek", re.IGNORECASE)
# death messages
DEATH_RE = re.compile(
    r"slain by|was killed|fell from|drowned|burned|died", re.IGNORECASE)
SYSTEM_KINDS = {"system", "server", "info"}

log = logging.getLogger("mc_chat_client")


def setup_logging(log_path: Path, verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        str(log_path), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
        encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s"))
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(sh)


# --------------------------------------------------------------------------- #
# source-side cursor
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    """Local wall clock, always +08:00 regardless of the host TZ."""
    return datetime.now(TZ_CST).isoformat(timespec="seconds")


def parse_iso_ts(value: Any) -> Optional[float]:
    """Best-effort ISO-8601 -> epoch seconds; None when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def event_ts(payload: Any) -> Optional[float]:
    """Pull an epoch timestamp out of a bridge event, if it carries one."""
    if not isinstance(payload, dict):
        return None
    for key in ("ts", "time", "timestamp", "at"):
        raw = payload.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            value = float(raw)
            # Tolerate milliseconds since epoch.
            return value / 1000.0 if value > 1e11 else value
        parsed = parse_iso_ts(raw)
        if parsed is not None:
            return parsed
    return None


def load_cursor_file(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return ``(cursor, problem)``; ``cursor`` is None when unusable.

    Any missing/invalid field counts as corrupt: the caller re-baselines
    instead of replaying history.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, "unreadable (%s)" % exc
    try:
        raw = json.loads(raw_text)
    except ValueError as exc:
        return None, "corrupt json (%s)" % exc
    if not isinstance(raw, dict):
        return None, "corrupt (not a JSON object)"
    seq = raw.get("last_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return None, "invalid last_seq=%r" % (seq,)
    source = raw.get("source")
    if source is not None and source != SOURCE_NAME:
        return None, "invalid source=%r" % (source,)
    return raw, ""


def save_cursor_file(path: Path, seq: int, bridge: str) -> None:
    """Atomically persist the cursor (tmp + fsync + os.replace)."""
    payload = {
        "source": SOURCE_NAME,
        "last_seq": int(seq),
        "saved_at": now_iso(),
        "bridge": bridge,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(tmp), str(path))


def build_envelope(payload: Any, event_name: str = "message",
                   sse_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Turn one SSE frame into a normalised envelope (or None to ignore)."""
    if isinstance(payload, dict):
        text = (payload.get("text") or payload.get("message")
                or payload.get("content") or "")
        actor = (payload.get("actor") or payload.get("player")
                 or payload.get("username") or payload.get("user") or "")
        raw_kind = str(payload.get("kind") or payload.get("type") or "")
        ext_id = payload.get("id")
        if ext_id is None:
            ext_id = payload.get("event_id")
    else:
        text, actor, raw_kind, ext_id = str(payload), "", "", None

    text = str(text)
    if not text.strip():
        return None

    death = bool(DEATH_RE.search(text))
    stream = bool(STREAM_RE.search(text))
    if death:
        kind, priority = "death", 0
    elif stream:
        kind, priority = "chat", 0
    elif raw_kind.lower() in SYSTEM_KINDS or event_name in ("system", "server"):
        kind, priority = "system", 2
    else:
        kind, priority = "chat", 2

    stable = sse_id if sse_id else (str(ext_id) if ext_id is not None else None)
    event_id = ("%s:sse:%s" % (SOURCE_NAME, stable)) if stable else \
        ("%s:%s" % (SOURCE_NAME, uuid.uuid4().hex))

    return {
        "source": SOURCE_NAME,
        "kind": kind,
        "level": "event" if priority == 0 else "info",
        "priority": priority,
        "actor": str(actor),
        "text": text,
        "payload": payload,
        "event_id": event_id,
    }


class MCClient:
    def __init__(self, args: argparse.Namespace) -> None:
        self.stream_url = args.url
        self.trigger_url = args.trigger_url
        self.dry_run = bool(args.dry_run)
        self.reconnect_seconds = float(args.reconnect_seconds)
        self.read_timeout = float(args.read_timeout)
        self.once_seconds = args.once_seconds
        self.stop = threading.Event()
        self.fail_count = 0
        self.posted = 0
        self.blocked = 0
        self.duplicates = 0
        self.drops_log = args.drops_log
        self.include_self = bool(getattr(args, "include_self", False))
        self.own_name = (getattr(args, "own_name", "") or "").strip()
        if not self.own_name:
            self.own_name = self._detect_own_name()
        log.info("L1 business filter: include_self=%s own_name=%r "
                 "(L2 rules live on the server)", self.include_self,
                 self.own_name)
        if getattr(args, "rules", None):
            log.warning("--rules %s ignored: L2 rules moved to the trigger "
                        "server (~/.hermes/trigger-rules.json)", args.rules)
        self.bus: Optional[LocalBus] = None
        if not args.no_bus:
            self.bus = LocalBus(SOURCE_NAME, port=args.listen_port)
        # -- source-side cursor (see module docstring) ------------------- #
        self.use_cursor = not bool(getattr(args, "no_cursor", False))
        self.cursor_path = Path(os.path.expanduser(
            getattr(args, "cursor_file", str(DEFAULT_CURSOR))))
        self.lookback = max(0.0, float(getattr(args, "lookback", DEFAULT_LOOKBACK)))
        self.bridge = self.stream_url.split("/stream")[0].rstrip("/")
        self.last_seq = 0
        self.cursor_ready = False
        self._lookback_floor: Optional[float] = None
        self._lookback_degraded = False
        self._cursor_write_warned = False
        self._bridge_seq_warned = False
        self._nonnumeric_id_warned = False

    # -- lifecycle ------------------------------------------------------- #
    def _detect_own_name(self) -> str:
        """Read our player name from the bridge /state (best effort)."""
        base = self.stream_url.split("/stream")[0].rstrip("/")
        try:
            import urllib.request
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
            with opener.open(base + "/state", timeout=5) as resp:
                data = json.loads(resp.read(200000).decode("utf-8"))
            name = ((data or {}).get("player") or {}).get("name") or ""
            return str(name).strip()
        except Exception as exc:
            log.debug("own-name detect failed: %s", exc)
            return ""

    # -- source-side cursor ---------------------------------------------- #
    def _bridge_last_seq(self) -> Optional[int]:
        """``GET /state`` -> ``events.lastSeq``; None when unavailable."""
        try:
            with _OPENER.open(self.bridge + "/state",
                              timeout=STATE_TIMEOUT) as resp:
                data = json.loads(resp.read(200000).decode("utf-8"))
        except (URLError, HTTPError, OSError, ValueError) as exc:
            if not self._bridge_seq_warned:
                log.warning("bridge /state unavailable (%s); cannot check "
                            "bridge session", exc)
                self._bridge_seq_warned = True
            return None
        events = data.get("events") if isinstance(data, dict) else None
        raw = events.get("lastSeq") if isinstance(events, dict) else None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            if not self._bridge_seq_warned:
                log.warning("bridge /state has no usable events.lastSeq (%r); "
                            "cannot check bridge session", raw)
                self._bridge_seq_warned = True
            return None
        self._bridge_seq_warned = False
        return raw

    def _prepare_cursor(self) -> None:
        """Resume, re-baseline, or detect a new bridge session.

        Called once before the first connect and again on every reconnect:
        the MC chat box is cleared on each login, so a ``lastSeq`` that went
        backwards means the game (and the bridge) restarted.  We never replay
        in that case.
        """
        if not self.use_cursor:
            return

        if self.cursor_ready:
            bridge_seq = self._bridge_last_seq()
            if bridge_seq is not None and bridge_seq < self.last_seq:
                log.info("new bridge session detected (seq went backwards "
                         "%d -> %d), re-baselined at %d, no replay",
                         self.last_seq, bridge_seq, bridge_seq)
                self._baseline(bridge_seq)
            return

        bridge_seq = self._bridge_last_seq()
        cursor, problem = load_cursor_file(self.cursor_path)
        stored: Optional[int] = None
        foreign_bridge = ""
        if cursor is not None:
            foreign_bridge = str(cursor.get("bridge") or "").rstrip("/")
            if foreign_bridge and foreign_bridge != self.bridge:
                problem = "cursor belongs to another bridge (%s)" % foreign_bridge
                cursor = None
            else:
                stored = int(cursor["last_seq"])

        if stored is not None:
            if bridge_seq is not None and bridge_seq < stored:
                log.info("new bridge session detected (seq went backwards "
                         "%d -> %d), re-baselined at %d, no replay",
                         stored, bridge_seq, bridge_seq)
                self._baseline(bridge_seq)
                return
            self.last_seq = stored
            self.cursor_ready = True
            saved_at = parse_iso_ts((cursor or {}).get("saved_at"))
            if self.lookback > 0 and saved_at is not None:
                self._lookback_floor = saved_at - self.lookback
                log.info("resuming from cursor: last_seq=%d (bridge lastSeq=%s, "
                         "lookback=%.1fs)", stored, bridge_seq, self.lookback)
            elif self.lookback > 0:
                self._lookback_degraded = True
                log.info("resuming from cursor: last_seq=%d (bridge lastSeq=%s); "
                         "saved_at unparseable, lookback degraded to exact seq",
                         stored, bridge_seq)
            else:
                log.info("resuming from cursor: last_seq=%d (bridge lastSeq=%s)",
                         stored, bridge_seq)
            return

        if bridge_seq is None:
            log.warning("cursor missing/corrupt (%s: %s) and bridge /state "
                        "unavailable; will baseline at the first event",
                        self.cursor_path, problem or "unusable")
            return

        if foreign_bridge:
            log.info("cursor was for another bridge (%s); re-baselined at %d "
                     "(no replay)", foreign_bridge, bridge_seq)
        else:
            log.warning("cursor missing/corrupt (%s: %s); baselined at %d "
                        "(no replay)", self.cursor_path,
                        problem or "unusable", bridge_seq)
        self._baseline(bridge_seq)

    def _baseline(self, seq: int) -> None:
        """Treat everything up to ``seq`` as read, and persist that."""
        self.last_seq = int(seq)
        self.cursor_ready = True
        self._lookback_floor = None
        self._write_cursor()

    def _write_cursor(self) -> None:
        if not self.use_cursor:
            return
        try:
            save_cursor_file(self.cursor_path, self.last_seq, self.bridge)
            self._cursor_write_warned = False
        except OSError as exc:
            if not self._cursor_write_warned:
                log.warning("cannot write cursor %s (%s); continuing without "
                            "cursor persistence", self.cursor_path, exc)
                self._cursor_write_warned = True

    def _advance(self, seq: Optional[int]) -> None:
        """Move the read watermark forward after one event was handled."""
        if not self.use_cursor or seq is None:
            return
        if seq <= self.last_seq and self.cursor_ready:
            return
        self.last_seq = seq
        self.cursor_ready = True
        self._write_cursor()

    def _accept_seq(self, seq: int, payload: Any) -> bool:
        """False when the event is at/below the cursor (not replayed)."""
        if not self.cursor_ready or seq > self.last_seq:
            return True
        if self._lookback_floor is not None:
            ts = event_ts(payload)
            if ts is not None and ts >= self._lookback_floor:
                return True
            if ts is None and not self._lookback_degraded:
                log.info("lookback degraded to exact last_seq (event seq=%d "
                         "has no ts)", seq)
                self._lookback_degraded = True
        return False

    def _seq_from_frame(self, sse_id: Optional[str],
                        payload: Any) -> Optional[int]:
        candidates: List[Any] = [sse_id]
        if isinstance(payload, dict):
            candidates.append(payload.get("seq"))
            candidates.append(payload.get("lastSeq"))
        for raw in candidates:
            if raw is None or isinstance(raw, bool):
                continue
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
        if self.use_cursor and sse_id and not self._nonnumeric_id_warned:
            log.warning("stream ids are not numeric (%r); cursor cannot "
                        "advance on this connection", sse_id)
            self._nonnumeric_id_warned = True
        return None

    def request_stop(self) -> None:
        self.stop.set()

    def start_bus(self) -> None:
        if self.bus is None:
            log.info("local bus disabled (--no-bus)")
            return
        try:
            port = self.bus.start()
        except OSError as exc:
            log.warning("cannot bind local bus on %s:%s (%s); continuing "
                        "without it", self.bus.host, self.bus._port, exc)
            self.bus = None
            return
        log.info("local bus: http://127.0.0.1:%d/subscribe  /events  /bus/health",
                 port)

    def run(self) -> int:
        deadline = (time.time() + float(self.once_seconds)
                    if self.once_seconds else None)
        log.info("mc_chat_client starting: stream=%s trigger=%s dry_run=%s",
                 self.stream_url, self.trigger_url, self.dry_run)
        if self.use_cursor:
            log.info("cursor: %s (lookback=%gs, bridge=%s)",
                     self.cursor_path, self.lookback, self.bridge)
        else:
            log.info("cursor disabled (--no-cursor); legacy behaviour")
        self._prepare_cursor()
        self.start_bus()
        try:
            return self._run(deadline)
        finally:
            if self.bus is not None:
                self.bus.stop()

    def _run(self, deadline: Optional[float]) -> int:
        first_attempt = True
        while not self.stop.is_set():
            if deadline is not None and time.time() >= deadline:
                break
            try:
                if not first_attempt:
                    # Reconnect: same session continues, a restarted game is
                    # detected here (lastSeq went backwards) and re-baselined.
                    self._prepare_cursor()
                first_attempt = False
                self._consume(deadline)
                if not self.stop.is_set():
                    self._note_failure("stream closed by server")
            except Exception as exc:  # noqa: BLE001 - one bad frame must not kill us
                if not self.stop.is_set():
                    self._note_failure("%s: %s" % (type(exc).__name__, exc))
            if self.stop.is_set():
                break
            wait = self.reconnect_seconds
            if deadline is not None:
                wait = max(0.0, min(wait, deadline - time.time()))
            if self.stop.wait(wait) or (deadline is not None
                                        and time.time() >= deadline):
                break
        stats = {"posted": self.posted, "duplicates": self.duplicates,
                 "l1_blocked": self.blocked}
        log.info("stopping: posted=%d duplicates=%d l1_blocked=%d stats=%s",
                 self.posted, self.duplicates, self.blocked,
                 json.dumps(stats, ensure_ascii=False))
        return 0

    # -- stream ---------------------------------------------------------- #
    def _stream_url(self) -> str:
        """Append ``since=<cursor>`` when we have a position to resume from."""
        if not (self.use_cursor and self.cursor_ready):
            return self.stream_url
        parts = urlsplit(self.stream_url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k != "since"]
        query.append(("since", str(self.last_seq)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(query), parts.fragment))

    def _consume(self, deadline: Optional[float]) -> None:
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "User-Agent": "mowu-trigger-client/0.5",
        }
        url = self._stream_url()
        if self.use_cursor and self.cursor_ready:
            # Same value twice: some proxies drop query strings, some drop
            # headers; the bridge may honour either.
            headers["Last-Event-ID"] = str(self.last_seq)
        req = Request(url, headers=headers)
        timeout = self.read_timeout
        if deadline is not None:
            timeout = max(1.0, min(timeout, deadline - time.time()))
        resp = _OPENER.open(req, timeout=timeout)
        try:
            if self.fail_count:
                log.info("reconnected to %s after %d failed attempt(s)",
                         url, self.fail_count)
            else:
                log.info("connected to %s", url)
            self.fail_count = 0
            data_lines: List[str] = []
            event_name = "message"
            sse_id: Optional[str] = None
            while not self.stop.is_set():
                raw = resp.readline()
                if not raw:
                    break
                if deadline is not None and time.time() >= deadline:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        self._dispatch("\n".join(data_lines), event_name, sse_id)
                    data_lines, event_name, sse_id = [], "message", None
                elif line.startswith(":"):
                    continue  # heartbeat
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
                elif line.startswith("event:"):
                    event_name = line[6:].strip() or "message"
                elif line.startswith("id:"):
                    sse_id = line[3:].strip() or None
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _dispatch(self, data: str, event_name: str,
                  sse_id: Optional[str]) -> None:
        try:
            payload = json.loads(data)
        except ValueError:
            payload = data
        seq = self._seq_from_frame(sse_id, payload)
        if seq is not None and not self._accept_seq(seq, payload):
            log.debug("skip seq=%d <= cursor %d (buffered replay)", seq,
                      self.last_seq)
            return
        try:
            self._handle(payload, data, event_name, sse_id)
        finally:
            # Read position advances even when the message was filtered,
            # deduped, self-skipped or a dry run: the cursor is "where in the
            # source we are", not "what we delivered".
            self._advance(seq)

    # -- L1 business filter ---------------------------------------------- #
    def business_filter(self, envelope: Dict[str, Any],
                        sender: str) -> Tuple[bool, str]:
        """The only MC-specific rule: never send our own chat back to us."""
        keep, reason = business_filter(envelope)
        if not keep:
            log.debug("envelope rejected: %s", schema_problem(envelope))
            return False, reason
        if not self.include_self and self.own_name and sender == self.own_name:
            return False, "self_message"
        return True, ""

    def _handle(self, payload: Any, data: str, event_name: str,
                sse_id: Optional[str]) -> None:
        env = build_envelope(payload, event_name, sse_id)
        if env is None:
            log.debug("ignoring empty chat frame: %r", data[:120])
            return
        sender = str(env.get("actor") or "").strip()
        if not sender:
            # The bridge keeps the speaker inside the text ("<Name> ..."),
            # not in a separate field.
            m = re.match(r"^<([^>]{1,32})>\s*", str(env.get("text") or ""))
            sender = m.group(1).strip() if m else ""

        keep, reason = self.business_filter(env, sender)
        if not keep:
            self.blocked += 1
            log.info("SKIP [%s] actor=%r text=%r", reason, sender,
                     env["text"][:120])
            log_skip(self.drops_log, SOURCE_NAME, reason, env["text"], sender)
            return

        priority = int(env["priority"])
        if self.bus is not None and not self.dry_run:
            published = self.bus.publish(env)
            log.debug("bus seq=%d event_id=%s", published["seq"],
                      env["event_id"])
        if self.dry_run:
            log.info("[dry-run] would POST %s kind=%s prio=%d actor=%r text=%r",
                     env["event_id"], env["kind"], priority, env["actor"],
                     env["text"][:120])
            return
        result = self.post(env)
        if result is None:
            log.error("DROP after retries: %s %r", env["event_id"],
                      env["text"][:120])
            return
        if result.get("duplicate"):
            self.duplicates += 1
            log.info("delivered (duplicate) id=%s %s", result.get("id"),
                     env["event_id"])
        else:
            self.posted += 1
            log.info("delivered id=%s kind=%s prio=%d actor=%r text=%r",
                     result.get("id"), env["kind"], priority, env["actor"],
                     env["text"][:120])

    # -- delivery -------------------------------------------------------- #
    def post(self, envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            if self.stop.is_set():
                return None
            request = Request(self.trigger_url, data=body, method="POST",
                              headers={"Content-Type": "application/json; charset=utf-8"})
            try:
                with _OPENER.open(request, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                if isinstance(payload, dict) and payload.get("ok"):
                    return payload
                log.warning("trigger rejected (attempt %d/%d): %s",
                            attempt, attempts, payload)
            except (URLError, HTTPError, OSError, ValueError) as exc:
                if attempt >= attempts:
                    log.error("trigger POST failed after %d attempts: %s",
                              attempts, exc)
                    return None
                delay = RETRY_DELAYS[attempt - 1]
                log.debug("trigger POST attempt %d failed (%s); retry in %.1fs",
                          attempt, exc, delay)
                if self.stop.wait(delay):
                    return None
        return None

    # -- noise control ---------------------------------------------------- #
    def _note_failure(self, detail: str) -> None:
        first = self.fail_count == 0
        self.fail_count += 1
        if first:
            log.warning("stream unavailable (%s); retrying every %.0fs",
                        detail, self.reconnect_seconds)
        elif self.fail_count % 60 == 0:
            log.info("stream still unavailable after %d attempts (%s)",
                     self.fail_count, detail)
        else:
            log.debug("reconnect attempt #%d failed: %s", self.fail_count, detail)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Minecraft chat source client: SSE -> L1 business filter "
                    "-> POST /trigger")
    parser.add_argument("--url", default=DEFAULT_STREAM_URL,
                        help="SSE stream URL (default %(default)s)")
    parser.add_argument("--trigger-url", default=DEFAULT_TRIGGER_URL,
                        help="trigger inbox URL (default %(default)s)")
    parser.add_argument("--rules", default=None,
                        help="IGNORED since 0.6.0: L2 rules live on the "
                             "server (~/.hermes/trigger-rules.json); the flag "
                             "is accepted for old scripts")
    parser.add_argument("--drops-log", default=str(DEFAULT_DROPS),
                        help="blocked-message log (default %(default)s)")
    parser.add_argument("--cursor-file", default=str(DEFAULT_CURSOR),
                        help="source-side cursor JSON (default %(default)s)")
    parser.add_argument("--lookback", type=float, default=DEFAULT_LOOKBACK,
                        help="on resume, also accept events newer than "
                             "saved_at minus N seconds (default %(default)s "
                             "for MC chat: no history to look back at)")
    parser.add_argument("--no-cursor", action="store_true",
                        help="disable the cursor entirely (old behaviour: no "
                             "read, no write, no since= on the stream)")
    parser.add_argument("--log", default=str(DEFAULT_LOG),
                        help="client log file (default %(default)s)")
    parser.add_argument("--reconnect-seconds", type=float, default=5.0,
                        help="reconnect delay after a stream failure "
                             "(default %(default)s)")
    parser.add_argument("--read-timeout", type=float, default=65.0,
                        help="socket read timeout in seconds (default %(default)s)")
    parser.add_argument("--listen-port", type=int, default=DEFAULT_BUS_PORT,
                        help="local bus port on 127.0.0.1 (default %(default)s)")
    parser.add_argument("--no-bus", action="store_true",
                        help="do not expose the local bus at all")
    parser.add_argument("--include-self", action="store_true",
                        help="include chat lines sent by our own player "
                             "(default: filtered out to avoid self-trigger loops)")
    parser.add_argument("--own-name", default=os.environ.get("MOWU_NAME", ""),
                        help="player name to treat as self (default: read from /state)")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate + log, never POST or publish")
    parser.add_argument("--once-seconds", type=float, default=None,
                        help="run for N seconds then exit (self-test)")
    parser.add_argument("--verbose", action="store_true",
                        help="DEBUG level in log file and stderr")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(Path(os.path.expanduser(args.log)), args.verbose)
    client = MCClient(args)

    def _shutdown(signum, _frame):
        log.info("signal %s received; stopping", signum)
        client.request_stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    return client.run()


if __name__ == "__main__":
    raise SystemExit(main())
