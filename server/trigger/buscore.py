#!/usr/bin/env python3
"""buscore.py — the subscription lifecycle shared by both buses.

Two buses expose the same semantics:

  * ``clients/localbus.py``  — in-process bus inside each source client;
  * ``server.py``            — the remote bus on 8791 for other machines.

Everything that decides *who gets what, and when to hang up* lives here:

  liveness   只有 SSE 连接本身：每 15s 一发 ``: ping``；连续 5 次写失败、或单次
             写阻塞超过 10s → 立刻断开（reason=write_failures）；对端 EOF/RST
             由 ``probe`` 探测到 → 立刻清理（reason=client_eof）。
  backpressure 每订阅者一个 128 槽有界队列，P0/P1/P2 共用容量。满了**不丢最新**：
             找队列里最低的那一档（priority 数值最大者），丢掉那一档里最早的一条；
             没有 P2 就丢最早的 P1，只剩 P0 才丢最早的 P0（``victim_index``）。
             每次丢弃只写 ``~/.hermes/trigger-drops.jsonl`` + 内存计数，**不发任何
             事件通知**；消费者靠 ``seq`` 不连续自行判断丢失。发布路径永不阻塞。
  resume     worker 带 ``since=<最后收到的 seq>`` 重连即可；缓冲/数据库里还有的
             事件会补发，早于最旧缓冲的部分用 gap 帧说明，绝不沉默。
  quota      ``max_events=N``（默认 0=不限）；投满 N 条发 ``event: exhausted``
             + ``{"last_seq": N}`` 后断开（reason=quota），带 since=N 续订即可继续。
  ack        ``POST /ack {name, up_to_seq}`` 只推进游标，不参与存活判断。

订阅周期（budget，不是存活）：

  * 每个 name 有一条订阅记录，``expires_at`` 默认 now+3600s；
  * 剩余时间进入 ``renew_window``（默认 600s）时，SSE 发一帧
    ``event: subscription`` + ``{"name","expires_in","renew_required":true}``，
    客户端自己决定要不要 ``POST /renew``；
  * ``expires_at`` 到点仍未续 → 停止投递（``GET /events`` 返回空数组），SSE 发
    ``event: expired`` + ``{"name"}`` 后断开（reason=expired）。记录保留 24h，
    期间 ``/renew`` 仍可恢复；
  * 周期与连接解耦：写失败/EOF 只结束连接，不改 ``expires_at``；反过来，
    连接活着也不会自动续期。``/ack`` 两者都不是。

Standard library only. Python 3.9 compatible.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import (Any, Callable, Deque, Dict, List, Optional, Sequence,
                    Tuple)

try:  # POSIX only; the buses themselves stay importable elsewhere
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("buscore")

QUEUE_MAX = 128
BATCH_MAX = 50
PING_INTERVAL = 15.0
WRITE_TIMEOUT = 10.0
MAX_WRITE_FAILURES = 5

#: P0/P1/P2 are the only tiers the drop policy knows.  Anything above P2
#: (notably the legacy default ``priority=3``) is treated as P2, anything
#: below 0 as P0 — the rule never sees a fourth level.
PRIORITY_MAX = 2
DEFAULT_DROP_LOG = "~/.hermes/trigger-drops.jsonl"

_UIDS = itertools.count(1)


def normalise_priority(raw: Any) -> int:
    """Map any incoming priority onto the three delivery tiers (0/1/2)."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = PRIORITY_MAX
    return max(0, min(PRIORITY_MAX, value))


def victim_index(priorities: Sequence[Any]) -> int:
    """Index of the entry a full queue must evict; -1 for an empty queue.

    Rule (fixed by the trainer, shared by both buses): take the *lowest*
    priority tier present — the largest priority number — and evict the
    *earliest* entry of that tier.  So any P2 wins, else any P1, else the
    earliest P0.  The newest message always gets in.
    """
    levels = [normalise_priority(p) for p in priorities]
    if not levels:
        return -1
    return levels.index(max(levels))


class DropLog:
    """Append-only JSONL audit of queue drops.

    One ``os.write`` per line on an ``O_APPEND`` descriptor, guarded by
    ``fcntl.flock`` so concurrent processes append whole lines instead of
    interleaving.  The file is never rewritten, and a failing log must never
    break the publish path: errors are logged and swallowed.
    """

    def __init__(self, path: str = DEFAULT_DROP_LOG) -> None:
        self.path = Path(os.path.expanduser(str(path)))
        self._lock = threading.Lock()

    def record(self, subscriber: str, priority: int, dropped_event_id: str,
               dropped_seq: int, queue_len: int, dropped_total: int,
               reason: str = "queue_full") -> Dict[str, Any]:
        entry = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "subscriber": str(subscriber),
            "priority": normalise_priority(priority),
            "dropped_event_id": str(dropped_event_id),
            "dropped_seq": int(dropped_seq),
            "reason": str(reason),
            "queue_len": int(queue_len),
            "dropped_total": int(dropped_total),
        }
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                fd = os.open(str(self.path),
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                    os.write(fd, line)
                finally:
                    try:
                        if fcntl is not None:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
        except OSError as exc:  # disk full, read-only home, ...
            log.warning("cannot append to drop log %s: %s", self.path, exc)
        return entry


_DEFAULT_DROP_LOG: Optional[DropLog] = None


def default_drop_log() -> DropLog:
    """One process-wide writer for the default ``trigger-drops.jsonl``."""
    global _DEFAULT_DROP_LOG
    if _DEFAULT_DROP_LOG is None:
        _DEFAULT_DROP_LOG = DropLog()
    return _DEFAULT_DROP_LOG


def event_identity(payload: Dict[str, Any], seq: int) -> str:
    """Best available stable id for a dropped event, for the audit line."""
    for field in ("event_id", "id"):
        value = payload.get(field)
        if value is not None and str(value).strip():
            return str(value)
    return "%s:%d" % (payload.get("source") or "?", int(seq))


def parse_drop_since(raw: Any) -> Optional[float]:
    """Accept epoch seconds or ISO-8601 for ``/drops?since=``; None if junk."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def read_drops(path: str = DEFAULT_DROP_LOG, subscriber: Optional[str] = None,
               since: Optional[Any] = None, limit: int = 100
               ) -> List[Dict[str, Any]]:
    """Newest-first slice of the drop log, read from the tail only.

    The file is scanned backwards in 64 KiB blocks, so a query never loads
    the whole JSONL into memory: we stop as soon as ``limit`` matching lines
    are collected.  ``since`` is an inclusive lower bound (epoch or ISO) on
    the ``at`` field; ``subscriber`` is an exact match.
    """
    limit = max(1, min(int(limit), 1000))
    wanted = str(subscriber).strip() if subscriber is not None else ""
    cutoff = parse_drop_since(since) if since is not None else None
    out: List[Dict[str, Any]] = []
    target = Path(os.path.expanduser(str(path)))
    try:
        fh = open(target, "rb")
    except OSError:
        return out
    with fh:
        try:
            pos = fh.seek(0, os.SEEK_END)
        except OSError:
            return out
        remainder = b""
        while pos > 0 and len(out) < limit:
            step = min(65536, pos)
            pos -= step
            fh.seek(pos)
            chunk = fh.read(step) + remainder
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
                if not isinstance(entry, dict):
                    continue
                if wanted and str(entry.get("subscriber") or "") != wanted:
                    continue
                if cutoff is not None:
                    stamp = parse_drop_since(entry.get("at"))
                    if stamp is None or stamp < cutoff:
                        continue
                out.append(entry)
                if len(out) >= limit:
                    break
        if len(out) < limit and remainder.strip():
            try:
                entry = json.loads(remainder.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                entry = None
            if (isinstance(entry, dict)
                    and (not wanted
                         or str(entry.get("subscriber") or "") == wanted)):
                stamp = parse_drop_since(entry.get("at"))
                if cutoff is None or (stamp is not None and stamp >= cutoff):
                    out.append(entry)
    return out


class Subscriber:
    """One live subscription."""

    def __init__(self, name: str, since: int, max_events: int = 0,
                 queue_max: int = QUEUE_MAX,
                 drop_log: Optional[DropLog] = None,
                 levels: Optional[Any] = None) -> None:
        self.uid = next(_UIDS)
        self.name = str(name)
        self.since = int(since)
        self.delivered = 0
        self.connected_at = time.time()
        self.blocked_reason = ""
        self.closed = False
        self.max_events = max(0, int(max_events or 0))
        self.queue_max = max(1, int(queue_max))
        # Which envelope levels this subscriber wants.  `log` is archive-only
        # and must never be streamed, so the default is event+info; Hermes
        # itself subscribes with event only (see server.parse_levels).
        self.levels = set(levels) if levels else {"event", "info"}
        self.drop_log = drop_log if drop_log is not None else default_drop_log()
        self.events: Deque[Tuple[int, Dict[str, Any]]] = deque()
        self.lost_from: Optional[int] = None
        self.lost_to: Optional[int] = None
        # queue-full drops are audit-only: the JSONL log is the source of
        # truth, the counter is just for /health.  Downstream learns about
        # loss from a discontinuity in ``seq``.
        self.dropped_total = 0
        self.cond = threading.Condition()

    # -- producer side (never blocks) ------------------------------------ #
    def push(self, seq: int, payload: Dict[str, Any]) -> bool:
        """Append one event, evicting by priority when the queue is full.

        The publisher is never refused: when the queue is at capacity the
        victim chosen by :func:`victim_index` leaves and the new event enters.
        The audit line is written after the lock is released so a consumer
        waiting on this condition is not held up by file I/O.
        """
        seq = int(seq)
        level = str(payload.get("level") or "info")
        if level not in self.levels:
            # Accepted by this bus, but this subscriber did not ask for it.
            return True
        drop: Optional[Dict[str, Any]] = None
        with self.cond:
            if self.closed:
                return False
            if len(self.events) >= self.queue_max:
                index = victim_index([p.get("priority")
                                      for _, p in self.events])
                victim_seq, victim = self.events[index]
                del self.events[index]
                self.dropped_total += 1
                drop = {
                    "subscriber": self.name,
                    "priority": normalise_priority(victim.get("priority")),
                    "dropped_event_id": event_identity(victim, victim_seq),
                    "dropped_seq": int(victim_seq),
                    "queue_len": self.queue_max,
                    "dropped_total": self.dropped_total,
                }
            self.events.append((seq, payload))
            self.cond.notify_all()
        if drop is not None:
            self.drop_log.record(**drop)
        return True

    # -- consumer side --------------------------------------------------- #
    def take(self, timeout: float
             ) -> Tuple[List[Tuple[int, Dict[str, Any]]],
                        Optional[Tuple[int, int]]]:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                if self.closed:
                    return [], None
                if self.events or self.lost_from is not None:
                    items = list(self.events)[:BATCH_MAX]
                    for _ in items:
                        self.events.popleft()
                    gap = None
                    if self.lost_from is not None:
                        gap = (self.lost_from, self.lost_to)
                        self.lost_from = self.lost_to = None
                    return items, gap
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], None
                self.cond.wait(min(remaining, 1.0))

    def mark_delivered(self, seq: int) -> None:
        with self.cond:
            self.since = max(self.since, int(seq))
            self.delivered += 1

    def quota_exhausted(self) -> bool:
        return self.max_events > 0 and self.delivered >= self.max_events

    def ack(self, up_to_seq: Optional[int] = None) -> Dict[str, Any]:
        """Cursor only — liveness is the socket, not this call."""
        with self.cond:
            if up_to_seq is not None:
                try:
                    self.since = max(self.since, int(up_to_seq))
                except (TypeError, ValueError):
                    pass
            return {"ok": True, "name": self.name, "up_to_seq": self.since}

    def close(self, reason: str) -> None:
        with self.cond:
            if not self.closed:
                self.closed = True
                self.blocked_reason = reason
            self.cond.notify_all()

    # -- reporting -------------------------------------------------------- #
    def health(self) -> Dict[str, Any]:
        with self.cond:
            return {"name": self.name,
                    "since": self.since,
                    "delivered": self.delivered,
                    "queue": len(self.events),
                    "queue_max": self.queue_max,
                    "dropped_total": self.dropped_total,
                    "blocked_reason": self.blocked_reason,
                    "connected_s": round(time.time() - self.connected_at, 1)}


class SubscriptionManager:
    """Registry of subscribers + fan-out."""

    def __init__(self, queue_max: int = QUEUE_MAX,
                 drop_log: Optional[DropLog] = None) -> None:
        self.queue_max = int(queue_max)
        self.drop_log = drop_log if drop_log is not None else default_drop_log()
        self._lock = threading.RLock()
        self._subs: Dict[str, Subscriber] = {}

    # -- lifecycle -------------------------------------------------------- #
    def register(self, name: str, since: int, max_events: int = 0,
                 levels: Optional[Any] = None) -> Subscriber:
        """Create (or replace) the subscription for ``name``."""
        with self._lock:
            old = self._subs.get(name)
            if old is not None:
                old.close("superseded")
            sub = Subscriber(name, since, max_events, self.queue_max,
                             self.drop_log, levels=levels)
            self._subs[name] = sub
            return sub

    def get(self, name: str) -> Optional[Subscriber]:
        with self._lock:
            return self._subs.get(name)

    def remove(self, sub: Optional[Subscriber]) -> None:
        """Drop a subscriber the moment its connection ends."""
        if sub is None:
            return
        with self._lock:
            if self._subs.get(sub.name) is sub:
                del self._subs[sub.name]
        sub.close(sub.blocked_reason or "client_eof")

    def ack(self, name: str, up_to_seq: Optional[int] = None
            ) -> Optional[Dict[str, Any]]:
        sub = self.get(name)
        if sub is None or sub.closed:
            return None
        return sub.ack(up_to_seq)

    # -- fan-out ---------------------------------------------------------- #
    def broadcast(self, seq: int, payload: Dict[str, Any]) -> int:
        with self._lock:
            targets = list(self._subs.values())
        taken = 0
        for sub in targets:
            if sub.push(seq, payload):
                taken += 1
        return taken

    def health(self) -> List[Dict[str, Any]]:
        with self._lock:
            subs = list(self._subs.values())
        return [s.health() for s in sorted(subs, key=lambda s: s.name)]

    def active_count(self) -> int:
        with self._lock:
            return len(self._subs)


def subscription_snapshot(name: str, expires_at: float, renew_count: int,
                          status: str, renew_window: float,
                          now: Optional[float] = None) -> Dict[str, Any]:
    """The wire shape shared by ``/events``, ``/subscriptions`` and SSE frames.

    ``expired`` means "delivery must stop": the clock ran out, or an operator
    closed the subscription. ``renew_required`` is only true while the record
    is still active and the remaining time is inside the renew window.
    """
    now = time.time() if now is None else now
    if status != "active":
        expires_in = 0.0
    else:
        expires_in = max(0.0, float(expires_at) - now)
    expired = status != "active" or expires_in <= 0.0
    return {
        "name": name,
        "expires_in": round(expires_in, 1),
        "renew_required": (not expired) and expires_in <= float(renew_window),
        "expired": bool(expired),
        "renew_count": int(renew_count or 0),
        "status": status,
    }


class LeaseWatch:
    """Turns a subscription snapshot into SSE control frames.

    Returns ``("subscription", payload)`` once each time the record enters the
    renew window, and ``("expired", payload)`` when delivery must stop. A
    renewal (``renew_count`` changes) re-arms the warning, so the next window
    warns again. It never extends anything itself: the client's AI decides.
    """

    def __init__(self, snapshot: Callable[[], Optional[Dict[str, Any]]],
                 name: str = "") -> None:
        self._snapshot = snapshot
        self._name = name
        self._renew_count: Optional[int] = None
        self._warned = False

    def __call__(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        snap = self._snapshot()
        if not snap:
            return ("expired", {"name": self._name})
        name = snap.get("name") or self._name
        if snap.get("expired"):
            return ("expired", {"name": name})
        count = snap.get("renew_count")
        if count != self._renew_count:
            self._renew_count = count
            self._warned = False
        if snap.get("renew_required") and not self._warned:
            self._warned = True
            return ("subscription", {"name": name,
                                     "expires_in": snap.get("expires_in"),
                                     "renew_required": True})
        return None


# --------------------------------------------------------------------------- #
# SSE pump
# --------------------------------------------------------------------------- #

def _write(write: Callable[[bytes], None], data: str) -> Any:
    """True on success, False on OSError, "timeout" on a blocked write."""
    try:
        write(data.encode("utf-8"))
        return True
    except socket.timeout:
        return "timeout"
    except (BrokenPipeError, ConnectionResetError):
        return False
    except OSError:
        return False


def pump(sub: Subscriber, write: Callable[[bytes], None],
         probe: Optional[Callable[[], bool]] = None,
         ping_interval: float = PING_INTERVAL,
         max_failures: int = MAX_WRITE_FAILURES,
         lease: Optional[Callable[[], Optional[Tuple[str, Dict[str, Any]]]]] = None
         ) -> str:
    """Stream one subscriber until it ends. Returns the close reason.

    ``write`` must raise on failure (a plain ``wfile.write`` does).
    ``probe`` returns True when the peer has gone away (EOF/RST).
    ``lease`` returns an optional control frame; ``("expired", ...)`` ends the
    stream with ``reason=expired``. It is consulted before every event write,
    so an expired lease can never leak a queued event.
    """
    failures = 0
    last_ping = time.monotonic()

    def emit(event: str, payload: Dict[str, Any]) -> Any:
        return _write(write, "event: %s\ndata: %s\n\n" % (
            event, json.dumps(payload, ensure_ascii=False)))

    def lease_frame() -> Optional[str]:
        """Write a due control frame; return a close reason or None."""
        nonlocal failures
        if lease is None:
            return None
        action = lease()
        if action is None:
            return None
        event, payload = action
        result = emit(event, payload)
        if result == "timeout":
            return "write_failures"
        if result is False:
            failures += 1
            if failures >= max_failures:
                return "write_failures"
            return None
        failures = 0
        return "expired" if event == "expired" else None

    while True:
        if sub.closed:
            return sub.blocked_reason or "closed"
        if probe is not None and probe():
            return "client_eof"

        ending = lease_frame()
        if ending:
            return ending

        items, gap = sub.take(1.0)
        if gap is not None:
            frame = "event: gap\ndata: %s\n\n" % json.dumps(
                {"from": gap[0], "to": gap[1]}, ensure_ascii=False)
            result = _write(write, frame)
            if result == "timeout":
                return "write_failures"
            if result is False:
                failures += 1
                if failures >= max_failures:
                    return "write_failures"
            else:
                failures = 0

        for seq, payload in items:
            if sub.closed:
                return sub.blocked_reason or "closed"
            ending = lease_frame()
            if ending:
                return ending
            result = _write(write, "data: %s\n\n" % json.dumps(
                payload, ensure_ascii=False))
            if result == "timeout":
                return "write_failures"
            if result is False:
                failures += 1
                if failures >= max_failures:
                    return "write_failures"
                continue
            failures = 0
            sub.mark_delivered(seq)
            if sub.quota_exhausted():
                _write(write, "event: exhausted\ndata: %s\n\n" % json.dumps(
                    {"last_seq": sub.since}, ensure_ascii=False))
                return "quota"

        now = time.monotonic()
        if now - last_ping >= ping_interval:
            last_ping = now
            result = _write(write, ": ping\n\n")
            if result == "timeout":
                return "write_failures"
            if result is False:
                failures += 1
                if failures >= max_failures:
                    return "write_failures"
            else:
                failures = 0
