"""mc_chat source — SSE client for the Minecraft chat bridge.

Talks to a local SSE endpoint (default ``http://127.0.0.1:8765/stream``),
turning each chat payload into an event with ``source=mc_chat``.

Wire format tolerated per ``data:`` frame (JSON object):
    {"player": "alice", "message": "hi"}          # or text/content/actor
    {"id": "...", "player": "...", "text": "..."}
    "plain string"

Reconnect policy: on any failure wait ``reconnect_seconds`` (default 5) and
retry. Only the first failure of an outage logs at WARNING; repeats stay at
DEBUG, so a missing bridge cannot flood the log. Startup never blocks on this
source — the HTTP server is already serving before the thread connects.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

from .base import BaseSource

# Never send loopback traffic through HTTP(S)_PROXY from the environment.
_OPENER = build_opener(ProxyHandler({}))


class MCChatSource(BaseSource):
    name = "mc_chat"

    def __init__(self, config, ingest, logger):
        super().__init__(config, ingest, logger)
        self.url = str(config.get("url") or "http://127.0.0.1:8765/stream")
        self.reconnect_seconds = float(config.get("reconnect_seconds", 5) or 5)
        self.read_timeout = float(config.get("read_timeout", 65) or 65)
        self._response = None
        self._response_lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------ #
    def on_stop(self) -> None:
        with self._response_lock:
            resp = self._response
        try:
            if resp is not None:
                resp.close()
        except Exception:
            pass

    # -- main loop ------------------------------------------------------ #
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._consume_stream()
                if not self._stop.is_set():
                    self._note_failure("stream closed by server")
            except Exception as exc:
                if not self._stop.is_set():
                    self._note_failure("%s: %s" % (type(exc).__name__, exc))
            if self._stop.wait(self.reconnect_seconds):
                break

    def _consume_stream(self) -> None:
        req = Request(self.url, headers={
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "User-Agent": "mowu-trigger/0.2",
        })
        resp = _OPENER.open(req, timeout=self.read_timeout)
        with self._response_lock:
            self._response = resp
        try:
            if self._fail_count:
                self.log.info("reconnected to %s after %d failed attempt(s)",
                              self.url, self._fail_count)
            else:
                self.log.info("connected to %s", self.url)
            self._fail_count = 0
            self.set_status("connected", self.url)
            data_lines = []
            event_name = "message"
            event_id = None
            while not self._stop.is_set():
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        self._dispatch("\n".join(data_lines), event_name,
                                       event_id)
                    data_lines, event_name, event_id = [], "message", None
                elif line.startswith(":"):
                    continue  # comment / heartbeat
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
                elif line.startswith("event:"):
                    event_name = line[6:].strip() or "message"
                elif line.startswith("id:"):
                    event_id = line[3:].strip() or None
        finally:
            with self._response_lock:
                self._response = None
            try:
                resp.close()
            except Exception:
                pass

    def _dispatch(self, data: str, event_name: str, event_id: Any) -> None:
        try:
            payload = json.loads(data)
        except ValueError:
            payload = data
        if isinstance(payload, dict):
            text = (payload.get("text") or payload.get("message")
                    or payload.get("content") or "")
            actor = (payload.get("actor") or payload.get("player")
                     or payload.get("username") or payload.get("user") or "")
            kind = str(payload.get("kind") or payload.get("type")
                       or ("chat" if event_name in ("message", "chat")
                           else event_name))
            try:
                priority = int(payload.get("priority", 3))
            except (TypeError, ValueError):
                priority = 3
            ext_id = (str(event_id) if event_id else
                      (str(payload["id"]) if payload.get("id") is not None
                       else (str(payload["event_id"])
                             if payload.get("event_id") else None)))
        else:
            text, actor, kind, priority, ext_id = str(payload), "", "chat", 3, event_id
        if not str(text).strip():
            self.log.debug("ignoring empty chat frame: %r", payload)
            return
        dedupe_id = ("mc_chat:sse:%s" % ext_id) if ext_id else None
        self.emit(kind=kind, priority=priority, actor=str(actor),
                  text=str(text), payload=payload, event_id=dedupe_id)

    def _note_failure(self, detail: str) -> None:
        first = self._fail_count == 0
        self._fail_count += 1
        self.set_status("error", detail)
        if first:
            self.log.warning("mc_chat unavailable (%s); retrying every %.0fs",
                             detail, self.reconnect_seconds)
        elif self._fail_count % 60 == 0:
            self.log.info("mc_chat still unavailable after %d attempts (%s)",
                          self._fail_count, detail)
        else:
            self.log.debug("mc_chat retry #%d: %s", self._fail_count, detail)


SOURCE = MCChatSource
