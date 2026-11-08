#!/usr/bin/env python3
"""localbus.py — in-process "local bus" that every source client exposes.

The trigger server is the durable inbox; the local bus is the *fast path*. A
worker on the same machine can subscribe straight to the client and skip the
round trip through ``/trigger``:

    GET  /subscribe?name=<subscriber>&since=<seq>[&max_events=N]
                                                   SSE; one `data: {...}` line
                                                   per event, `: ping` every 15 s
    GET  /events?name=<subscriber>&since=<seq>&limit=50
                                                   {events, subscription} object
    POST /ack   {"name": "...", "up_to_seq": N}    advance the cursor only
    POST /renew {"name": "..."}                    restart the subscription clock
    POST /unsubscribe {"name": "..."}              stop delivery for good
    GET  /subscriptions                            every record + expiry state
    GET  /drops?subscriber=<name>&since=<ISO|epoch>&limit=100
                                                   queue-drop audit (tail read)
    GET  /bus/health                               subscribers + counters

Lifecycle (identical to the remote bus on 8791, shared through ``buscore``):

  * each subscriber has its own 128-slot queue (P0/P1/P2 share it); when full,
    the earliest entry of the lowest tier present is evicted (P2 first, then
    P1, then P0) and every eviction is appended to
    ``~/.hermes/trigger-drops.jsonl``.  No event is pushed to announce the
    drop: consumers detect loss from a discontinuity in ``seq``.  Publishers
    never block;
  * 5 consecutive write failures (or one 10 s write) closes the stream with
    ``reason=write_failures``; peer EOF/RST -> ``reason=client_eof``;
  * ``max_events=N`` caps a subscription; when spent the stream sends
    ``event: exhausted`` + ``{"last_seq": N}`` and closes (``reason=quota``).
    Re-subscribe with the same name and ``since=N`` to continue.

Subscription cycle — a *budget*, not connection liveness:

  * the first time a name is seen a record is created with
    ``expires_at = now + subscription_ttl`` (default 3600 s);
  * when the remaining time drops into ``renew_window`` (default 600 s) the SSE
    stream emits one ``event: subscription`` frame (``renew_required: true``)
    and ``GET /events`` reports ``subscription.renew_required=true``; the
    client decides whether to ``POST /renew``;
  * once ``expires_at`` passes, delivery stops: ``GET /events`` returns an
    empty ``events`` array even for ``since=0``, and the SSE stream emits
    ``event: expired`` then closes (``reason=expired``). The record is kept
    for 24 h, during which ``POST /renew`` still revives it;
  * the cycle and the connection are independent: write failures/EOF only end
    the connection and never extend ``expires_at``; a live connection never
    renews by itself; ``/ack`` is neither a heartbeat nor a renewal.

The ring buffer holds the last 500 events **in memory only**. A client restart
drops them; a worker that needs history must fall back to
``GET /events`` on the trigger server.

Standard library only. Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import select
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from buscore import (  # noqa: E402
    DEFAULT_DROP_LOG, QUEUE_MAX, LeaseWatch, SubscriptionManager,
    parse_drop_since, pump, read_drops, subscription_snapshot)

VERSION = "0.6.0"
DEFAULT_MAXLEN = 500
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
DEFAULT_SUBSCRIPTION_TTL = 3600
DEFAULT_RENEW_WINDOW = 600
SUBSCRIPTION_GRACE = 24 * 3600   # expired records stay renewable this long
JANITOR_INTERVAL = 60.0

log = logging.getLogger("localbus")


class SubscriptionLedger:
    """In-memory subscription records — the local twin of the server's table.

    A record is *not* a connection: it survives disconnects, and its clock is
    only reset by ``renew()``. That is the whole point of the cycle.
    """

    def __init__(self, ttl: float = DEFAULT_SUBSCRIPTION_TTL,
                 renew_window: float = DEFAULT_RENEW_WINDOW,
                 grace: float = SUBSCRIPTION_GRACE) -> None:
        self.ttl = float(ttl)
        self.renew_window = float(renew_window)
        self.grace = float(grace)
        self._lock = threading.RLock()
        self._rows: Dict[str, Dict[str, Any]] = {}

    # -- lifecycle -------------------------------------------------------- #
    def ensure(self, name: str, since: int = 0) -> Dict[str, Any]:
        """Create on first sight; always refresh ``last_seen``.

        A closed record is never resurrected here — otherwise a worker whose
        subscription was cancelled would silently come back on its next poll.
        """
        now = time.time()
        with self._lock:
            row = self._rows.get(name)
            if row is None:
                row = {"name": str(name),
                       "since": max(0, int(since)),
                       "created_at": now,
                       "expires_at": now + self.ttl,
                       "renew_count": 0,
                       "last_seen": now,
                       "status": "active"}
                self._rows[name] = row
                log.info("subscription created name=%s ttl=%.0fs",
                         name, self.ttl)
            else:
                row["last_seen"] = now
            return dict(row)

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._rows.get(name)
            return dict(row) if row else None

    def view(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._rows.get(name)
            if row is None:
                return None
            return subscription_snapshot(row["name"], row["expires_at"],
                                         row["renew_count"], row["status"],
                                         self.renew_window, now=time.time())

    def renew(self, name: str) -> Optional[Dict[str, Any]]:
        """Extend an *active* record. None for unknown names.

        A record whose ``status`` is closed/expired comes back unchanged; the
        caller turns that into 410 (it exists, but is not renewable).
        """
        now = time.time()
        with self._lock:
            row = self._rows.get(name)
            if row is None:
                return None
            if row["status"] != "active":
                return subscription_snapshot(row["name"], row["expires_at"],
                                             row["renew_count"], row["status"],
                                             self.renew_window, now=now)
            row["expires_at"] = now + self.ttl
            row["renew_count"] = int(row["renew_count"]) + 1
            row["last_seen"] = now
            log.info("subscription renewed name=%s renew_count=%d ttl=%.0fs",
                     name, row["renew_count"], self.ttl)
            return subscription_snapshot(row["name"], row["expires_at"],
                                         row["renew_count"], row["status"],
                                         self.renew_window, now=now)

    def close(self, name: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            row = self._rows.get(name)
            if row is None:
                return None
            if row["status"] != "closed":
                row["status"] = "closed"
                log.info("subscription closed name=%s", name)
            return subscription_snapshot(row["name"], row["expires_at"],
                                         row["renew_count"], row["status"],
                                         self.renew_window, now=now)

    def listing(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            rows = [dict(r) for r in self._rows.values()]
        out: List[Dict[str, Any]] = []
        for row in sorted(rows, key=lambda r: r["name"]):
            entry = subscription_snapshot(row["name"], row["expires_at"],
                                          row["renew_count"], row["status"],
                                          self.renew_window, now=now)
            entry["since"] = int(row["since"])
            entry["created_at"] = row["created_at"]
            entry["expires_at"] = row["expires_at"]
            entry["last_seen"] = row["last_seen"]
            out.append(entry)
        return out

    def counts(self) -> Dict[str, int]:
        with self._lock:
            rows = list(self._rows.values())
        counts = {"active": 0, "expired": 0, "closed": 0}
        for row in rows:
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
        counts["total"] = len(rows)
        return counts

    def expire_stale(self, grace: Optional[float] = None) -> int:
        """Mark records that stayed unused past the grace window as expired."""
        cutoff = time.time() - float(self.grace if grace is None else grace)
        changed = 0
        with self._lock:
            for row in self._rows.values():
                if row["status"] == "active" and row["expires_at"] < cutoff:
                    row["status"] = "expired"
                    changed += 1
        if changed:
            log.info("subscription janitor: %d record(s) past the %.0fh grace "
                     "window marked expired", changed, self.grace / 3600.0)
        return changed


class LocalBus:
    """Ring buffer + fan-out. Thread-safe; one instance per client process."""

    def __init__(self, source_name: str, host: str = "127.0.0.1",
                 port: int = 0, maxlen: int = DEFAULT_MAXLEN,
                 queue_max: int = QUEUE_MAX,
                 subscription_ttl: float = DEFAULT_SUBSCRIPTION_TTL,
                 renew_window: float = DEFAULT_RENEW_WINDOW) -> None:
        self.source = str(source_name)
        self.host = host
        self.maxlen = int(maxlen)
        self.queue_max = max(1, int(queue_max))
        self._buf: Deque[Dict[str, Any]] = deque(maxlen=self.maxlen)
        self._seq = 0
        # RLock: since()/has_gap()/oldest_seq() nest lock acquisition.
        self._lock = threading.RLock()
        self._manager = SubscriptionManager(queue_max=self.queue_max)
        self.subscription_ttl = int(subscription_ttl)
        self.renew_window = int(renew_window)
        self.ledger = SubscriptionLedger(self.subscription_ttl,
                                         self.renew_window)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._janitor: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._port = int(port)

    # -- publishing ------------------------------------------------------- #
    def publish(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """Append one envelope, assign ``seq``, fan out. Never blocks."""
        with self._lock:
            self._seq += 1
            event = dict(envelope)
            event["seq"] = self._seq
            self._buf.append(event)
            seq = self._seq
        self._manager.broadcast(seq, event)
        return event

    # -- reading ---------------------------------------------------------- #
    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def oldest_seq(self) -> int:
        with self._lock:
            if not self._buf:
                return self._seq + 1
            return int(self._buf[0]["seq"])

    def has_gap(self, since: int) -> bool:
        if since <= 0:
            return False
        return since < self.oldest_seq() - 1

    def since(self, since: int, limit: int = DEFAULT_LIMIT
              ) -> Tuple[List[Dict[str, Any]], bool]:
        """Return ``(events, gap)`` for ``seq > since``."""
        limit = max(1, min(int(limit), MAX_LIMIT, self.maxlen))
        with self._lock:
            gap = self.has_gap(int(since))
            events = [e for e in self._buf
                      if int(e["seq"]) > int(since)
                      and str(e.get("level") or "info") != "log"][:limit]
            return events, gap

    def backfill(self, sub: Any, since: int) -> None:
        """Push the ring buffer into a fresh subscription's queue."""
        with self._lock:
            buffered = list(self._buf)
            oldest = self.oldest_seq()
        if since > 0 and since < oldest - 1:
            sub.lost_from = since + 1
            sub.lost_to = oldest - 1
        for event in buffered:
            if int(event["seq"]) > since:
                sub.push(int(event["seq"]), event)

    # -- health ------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        with self._lock:
            buffered = len(self._buf)
            last = self._seq
        return {"ok": True, "source": self.source,
                "version": VERSION,
                "subscription_ttl": self.subscription_ttl,
                "renew_window": self.renew_window,
                "queue_max": self.queue_max,
                "drops_log": os.path.expanduser(DEFAULT_DROP_LOG),
                "subscriptions": self.ledger.counts(),
                "subscribers": self._manager.health(),
                "buffered": buffered, "last_seq": last}

    # -- http -------------------------------------------------------------- #
    def bind(self) -> int:
        """Create the HTTP server (no thread yet). Returns the actual port."""
        if self._httpd is not None:
            return self._port
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self._port), handler)
        self._httpd.daemon_threads = True
        self._port = self._httpd.server_address[1]
        return self._port

    def start(self) -> int:
        """Bind and serve in a daemon thread. Returns the actual port."""
        port = self.bind()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="localbus",
            kwargs={"poll_interval": 0.2}, daemon=True)
        self._thread.start()
        self._janitor = threading.Thread(
            target=self._janitor_loop, name="localbus-janitor", daemon=True)
        self._janitor.start()
        log.info("local bus on http://%s:%d (buffer=%d, queue_max=%d, ttl=%ds,"
                 " window=%ds)", self.host, self._port, self.maxlen,
                 self.queue_max, self.subscription_ttl, self.renew_window)
        return port

    def serve_once(self) -> None:
        """Serve exactly one HTTP request (self-test), then return."""
        self.bind()
        done = threading.Event()
        base = self._httpd.RequestHandlerClass

        class _OneShot(base):  # type: ignore[misc, valid-type]
            def finish(self) -> None:
                try:
                    super().finish()
                finally:
                    done.set()

        self._httpd.RequestHandlerClass = _OneShot
        self._httpd.handle_request()
        done.wait(10.0)

    def _janitor_loop(self) -> None:
        while not self._stop.wait(JANITOR_INTERVAL):
            try:
                self.ledger.expire_stale()
            except Exception:  # noqa: BLE001 - janitor must never kill the bus
                log.exception("subscription janitor failed")

    def stop(self) -> None:
        self._stop.set()
        httpd = self._httpd
        if httpd is None:
            return
        # shutdown() only returns while serve_forever() is running; bind-only
        # instances (--once) would hang forever waiting for an event that is
        # never set, so only call it when the serve thread is alive.
        if self._thread is not None and self._thread.is_alive():
            try:
                httpd.shutdown()
            except Exception:
                pass
        try:
            httpd.server_close()
        except Exception:
            pass
        self._httpd = None


def _make_handler(bus: LocalBus):
    class BusHandler(BaseHTTPRequestHandler):
        server_version = "mowu-localbus/" + VERSION
        sys_version = ""

        def log_message(self, fmt, *args):
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
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _read_json(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if length <= 0 or length > 65536:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        def do_GET(self):  # noqa: N802 - stdlib signature
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query, keep_blank_values=True)
            try:
                if parsed.path == "/subscribe":
                    self._subscribe(qs)
                elif parsed.path == "/events":
                    self._events(qs)
                elif parsed.path == "/subscriptions":
                    self._subscriptions()
                elif parsed.path == "/drops":
                    self._drops(qs)
                elif parsed.path == "/bus/health":
                    self._send_json(200, bus.health())
                else:
                    self._send_json(404, {"ok": False, "error": "not found",
                                          "endpoints": ["/subscribe", "/events",
                                                        "/drops", "/ack",
                                                        "/renew",
                                                        "/unsubscribe",
                                                        "/subscriptions",
                                                        "/bus/health"]})
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning("bus request failed: %s: %s", type(exc).__name__, exc)
                try:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                except Exception:
                    pass

        def do_POST(self):  # noqa: N802 - stdlib signature
            parsed = urlparse(self.path)
            body = self._read_json()
            try:
                if parsed.path == "/ack":
                    self._ack(body)
                elif parsed.path == "/renew":
                    self._renew(body)
                elif parsed.path == "/unsubscribe":
                    self._unsubscribe(body)
                else:
                    self._send_json(404, {"ok": False, "error": "not found",
                                          "endpoints": ["/ack", "/renew",
                                                        "/unsubscribe"]})
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning("bus POST failed: %s: %s", type(exc).__name__, exc)
                try:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                except Exception:
                    pass

        # -- helpers ---------------------------------------------------- #
        @staticmethod
        def _int(qs: Dict[str, List[str]], name: str, default: int) -> int:
            raw = qs.get(name, [""])[0]
            if raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        @staticmethod
        def _float(qs: Dict[str, List[str]], name: str, default: float) -> float:
            raw = qs.get(name, [""])[0]
            if raw == "":
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        @staticmethod
        def _name(qs: Dict[str, List[str]]) -> Optional[str]:
            name = (qs.get("name", [""])[0]
                    or qs.get("subscriber", [""])[0] or "").strip()
            return name or None

        @staticmethod
        def _body_name(body: Dict[str, Any]) -> Optional[str]:
            name = str(body.get("name") or body.get("subscriber") or "").strip()
            return name or None

        # -- endpoints --------------------------------------------------- #
        def _events(self, qs: Dict[str, List[str]]) -> None:
            name = self._name(qs)
            if not name:
                self._send_json(400, {"ok": False,
                                      "error": "name is required"})
                return
            since = self._int(qs, "since", 0)
            limit = self._int(qs, "limit", DEFAULT_LIMIT)
            bus.ledger.ensure(name, since)
            view = bus.ledger.view(name)
            if view is not None and view["expired"]:
                # Expired: no events, even for since=0. The record survives
                # for 24h so POST /renew can revive it.
                self._send_json(200, {"events": [], "subscription": view})
                return
            events, gap = bus.since(since, limit)
            fresh = bus.ledger.view(name) or view
            if fresh is not None and fresh["expired"]:
                self._send_json(200, {"events": [], "subscription": fresh})
                return
            if gap:
                self._send_json(200, {
                    "gap": True,
                    "source": bus.source,
                    "since": since,
                    "oldest_seq": bus.oldest_seq(),
                    "last_seq": bus.last_seq,
                    "detail": "requested seq is older than the ring buffer; "
                              "history was lost (client restart?) — fetch it "
                              "from http://127.0.0.1:8791/events instead",
                    "count": len(events),
                    "events": events,
                    "subscription": fresh,
                })
                return
            self._send_json(200, {"events": events, "subscription": fresh})

        def _subscriptions(self) -> None:
            entries = bus.ledger.listing()
            self._send_json(200, {"ok": True, "count": len(entries),
                                  "subscription_ttl": bus.subscription_ttl,
                                  "renew_window": bus.renew_window,
                                  "queue_max": bus.queue_max,
                                  "subscriptions": entries})

        def _drops(self, qs: Dict[str, List[str]]) -> None:
            """Same contract as server.py /drops: newest first, tail-read."""
            subscriber = (qs.get("subscriber", [""])[0] or "").strip() or None
            since_raw = qs.get("since", [None])[0]
            try:
                limit = int(qs.get("limit", ["100"])[0] or 100)
            except ValueError:
                self._send_json(400, {"ok": False,
                                      "error": "limit must be an integer"})
                return
            if limit < 1:
                self._send_json(400, {"ok": False,
                                      "error": "limit must be >= 1"})
                return
            if since_raw not in (None, "") and parse_drop_since(since_raw) is None:
                self._send_json(400, {"ok": False,
                                      "error": "since must be ISO-8601 or "
                                               "epoch seconds"})
                return
            drops = read_drops(DEFAULT_DROP_LOG, subscriber=subscriber,
                               since=since_raw, limit=limit)
            self._send_json(200, {"drops": drops, "count": len(drops)})

        def _renew(self, body: Dict[str, Any]) -> None:
            name = self._body_name(body)
            if not name:
                self._send_json(400, {"ok": False,
                                      "error": "name is required"})
                return
            view = bus.ledger.renew(name)
            if view is None:
                self._send_json(404, {"ok": False,
                                      "error": "unknown subscription",
                                      "name": name})
                return
            if view["status"] != "active":
                self._send_json(410, {"ok": False,
                                      "error": "subscription %s"
                                               % view["status"],
                                      "name": name})
                return
            self._send_json(200, {"ok": True, "name": name,
                                  "expires_in": view["expires_in"],
                                  "renew_count": view["renew_count"]})

        def _unsubscribe(self, body: Dict[str, Any]) -> None:
            name = self._body_name(body)
            if not name:
                self._send_json(400, {"ok": False,
                                      "error": "name is required"})
                return
            view = bus.ledger.close(name)
            if view is None:
                self._send_json(404, {"ok": False,
                                      "error": "unknown subscription",
                                      "name": name})
                return
            sub = bus._manager.get(name)
            if sub is not None:
                sub.close("unsubscribed")
                bus._manager.remove(sub)
            self._send_json(200, {"ok": True, "name": name, "status": "closed"})

        def _ack(self, body: Dict[str, Any]) -> None:
            name = self._body_name(body)
            if not name:
                self._send_json(400, {"ok": False,
                                      "error": "name is required"})
                return
            raw = body.get("up_to_seq", body.get("up_to_id"))
            try:
                up_to = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False,
                                      "error": "up_to_seq must be an integer"})
                return
            result = bus._manager.ack(name, up_to)
            if result is None:
                self._send_json(404, {"ok": False,
                                      "error": "unknown subscriber",
                                      "name": name})
                return
            log.info("ack name=%s up_to_seq=%s", name, result.get("up_to_seq"))
            self._send_json(200, result)

        def _subscribe(self, qs: Dict[str, List[str]]) -> None:
            name = self._name(qs)
            if not name:
                self._send_json(400, {"ok": False,
                                      "error": "name is required"})
                return
            since = self._int(qs, "since", 0)
            max_events = self._int(qs, "max_events", 0)

            bus.ledger.ensure(name, since)
            view = bus.ledger.view(name)

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

            if view is not None and view["expired"]:
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.write(("event: expired\ndata: %s\n\n" %
                                      json.dumps({"name": name},
                                                 ensure_ascii=False)
                                      ).encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                log.info("subscribe name=%s refused: status=%s expired=%s",
                         name, view["status"], view["expired"])
                return

            sub = bus._manager.register(name, since, max_events)
            bus.backfill(sub, since)

            def snapshot() -> Optional[Dict[str, Any]]:
                return bus.ledger.view(name)

            lease = LeaseWatch(snapshot, name)
            log.info("subscribe name=%s since=%d max_events=%d expires_in=%s "
                     "from=%s", name, since, max_events,
                     view["expires_in"] if view else "?", self.address_string())

            reason = "client_closed"
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                reason = pump(sub, self._write, probe=self._peer_gone,
                              lease=lease)
            except (BrokenPipeError, ConnectionResetError):
                reason = "write_failures"
            except Exception as exc:  # noqa: BLE001
                log.warning("subscription %s failed: %s: %s", name,
                            type(exc).__name__, exc)
                reason = "write_failures"
            finally:
                sub.close(reason)
                bus._manager.remove(sub)
                log.info("subscription %s ended reason=%s delivered=%d since=%d",
                         name, reason, sub.delivered, sub.since)

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

    return BusHandler


# --------------------------------------------------------------------------- #
# standalone entry point (mainly for self-tests / a worker's fake source)
# --------------------------------------------------------------------------- #

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


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="standalone local bus (same semantics as server.py's)")
    p.add_argument("--source", default="localbus",
                   help="source name reported by /events and /bus/health")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8792)
    p.add_argument("--maxlen", type=int, default=DEFAULT_MAXLEN,
                   help="ring buffer size (default %(default)s)")
    p.add_argument("--queue-max", type=int, default=None, metavar="N",
                   help="slots in each subscriber's fan-out queue, shared by "
                        "P0/P1/P2 (default: $TRIGGER_QUEUE_MAX or %d)"
                        % QUEUE_MAX)
    p.add_argument("--subscription-ttl", type=int, default=None,
                   help="seconds until a subscription expires (default: "
                        "$TRIGGER_SUBSCRIPTION_TTL or %d)"
                        % DEFAULT_SUBSCRIPTION_TTL)
    p.add_argument("--renew-window", type=int, default=None,
                   help="seconds before expiry to emit renew_required "
                        "(default: $TRIGGER_RENEW_WINDOW or %d)"
                        % DEFAULT_RENEW_WINDOW)
    p.add_argument("--max-events", type=int, default=0,
                   help="publish N synthetic events right after binding "
                        "(self-test helper; 0 = none)")
    p.add_argument("--once", action="store_true",
                   help="serve exactly one HTTP request, then exit")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=VERSION)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr)

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

    bus = LocalBus(args.source, host=args.host, port=args.port,
                   maxlen=args.maxlen, queue_max=queue_max,
                   subscription_ttl=ttl, renew_window=renew_window)
    try:
        if args.once:
            bus.bind()
        else:
            bus.start()
            for _ in range(max(0, args.max_events)):
                bus.publish({"source": args.source, "kind": "seed",
                             "priority": 3, "text": "seed event",
                             "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    except OSError as exc:
        print("error: cannot bind %s:%d: %s" % (args.host, args.port, exc),
              file=sys.stderr)
        return 1

    print("mowu-localbus %s listening on http://%s:%d  [ttl=%ds "
          "renew_window=%ds buffer=%d queue_max=%d]"
          % (VERSION, args.host, bus._port, ttl, renew_window, args.maxlen,
             bus.queue_max), flush=True)
    try:
        if args.once:
            bus.serve_once()
            return 0
        bus._stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        bus.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
