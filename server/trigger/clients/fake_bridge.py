#!/usr/bin/env python3
"""fake_bridge.py — controllable stand-in for mowu_bot_bridge (:8765).

Only the subset ``mc_chat_client.py`` needs, plus test hooks:

    GET  /state     -> {"ok":true,"events":{"lastSeq":N,"buffered":N},
                        "player":{"name":"SuperCreek"}}
    GET  /stream    -> SSE; honours ``?since=`` and ``Last-Event-ID``
                       (unless ignore_since is on, which simulates a bridge
                       that always replays its whole buffer)
    POST /emit      {"text": "...", "player": "Steve"} -> new seq
    POST /reset     -> seq back to 0, buffer cleared (simulates game restart)
    POST /config    {"ignore_since": true|false}
    POST /trigger   -> records the envelope, returns {"ok":true,...}
    GET  /records   -> every envelope POSTed to /trigger, in order

    python3 fake_bridge.py --port 8801

Standard library only. For self-tests; never point it at production.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit


class Store:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.seq = 0
        self.events: List[Dict[str, Any]] = []
        self.triggers: List[Dict[str, Any]] = []
        self.ignore_since = False
        self.frames_sent = 0
        self.stream_sinces: List[Optional[str]] = []

    def emit(self, text: str, player: str = "Steve",
             ts: Optional[float] = None) -> Dict[str, Any]:
        with self.cond:
            self.seq += 1
            event = {"seq": self.seq, "text": text, "player": player,
                     "ts": time.time() if ts is None else float(ts)}
            self.events.append(event)
            self.cond.notify_all()
            return dict(event)

    def reset(self) -> None:
        with self.cond:
            self.seq = 0
            self.events = []
            self.cond.notify_all()

    def snapshot_after(self, since: int, honour_since: bool) -> List[Dict[str, Any]]:
        with self.cond:
            floor = since if honour_since else 0
            return [dict(e) for e in self.events if e["seq"] > floor]

    def state(self) -> Dict[str, Any]:
        with self.cond:
            return {"ok": True,
                    "events": {"lastSeq": self.seq, "buffered": len(self.events)},
                    "player": {"name": "SuperCreek"}}


STORE = Store()


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-bridge/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------- #
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt, *args):
        pass

    # -- routes ---------------------------------------------------------- #
    def do_GET(self):  # noqa: N802 - stdlib signature
        parts = urlsplit(self.path)
        if parts.path == "/state":
            self._json(STORE.state())
            return
        if parts.path == "/records":
            with STORE.cond:
                self._json({"ok": True, "records": list(STORE.triggers),
                            "frames_sent": STORE.frames_sent,
                            "stream_sinces": list(STORE.stream_sinces)})
            return
        if parts.path == "/stream":
            self._stream(parts.query)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):  # noqa: N802 - stdlib signature
        parts = urlsplit(self.path)
        if parts.path == "/emit":
            body = self._body()
            event = STORE.emit(str(body.get("text") or ""),
                               str(body.get("player") or "Steve"),
                               body.get("ts"))
            self._json({"ok": True, "event": event})
            return
        if parts.path == "/reset":
            STORE.reset()
            self._json({"ok": True, "lastSeq": 0})
            return
        if parts.path == "/config":
            body = self._body()
            with STORE.cond:
                if "ignore_since" in body:
                    STORE.ignore_since = bool(body["ignore_since"])
                ignore = STORE.ignore_since
            self._json({"ok": True, "ignore_since": ignore})
            return
        if parts.path == "/trigger":
            body = self._body()
            with STORE.cond:
                STORE.triggers.append(body)
                count = len(STORE.triggers)
            self._json({"ok": True, "id": count, "duplicate": False})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    # -- SSE ------------------------------------------------------------- #
    def _stream(self, query: str) -> None:
        params = parse_qs(query, keep_blank_values=True)
        raw = (params.get("since") or [None])[0]
        if raw is None:
            raw = self.headers.get("Last-Event-ID")
        try:
            since = int(str(raw)) if raw is not None else 0
        except (TypeError, ValueError):
            since = 0
        with STORE.cond:
            STORE.stream_sinces.append(raw)
            del STORE.stream_sinces[:-50]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        cursor = since
        last_ping = time.time()
        try:
            while True:
                honour = not STORE.ignore_since
                pending = STORE.snapshot_after(cursor, honour)
                for event in pending:
                    frame = "id: %d\ndata: %s\n\n" % (
                        event["seq"],
                        json.dumps({"text": event["text"],
                                    "player": event["player"],
                                    "ts": event["ts"]}, ensure_ascii=False))
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                    with STORE.cond:
                        STORE.frames_sent += 1
                    cursor = event["seq"]
                if time.time() - last_ping >= 5:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="fake MC bridge for tests")
    parser.add_argument("--port", type=int, default=8801)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print("fake bridge on http://127.0.0.1:%d (state=/state stream=/stream)" %
          args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
