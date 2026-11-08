"""Source adapter base class.

A source is anything that has to be *pulled* — an SSE stream, an IMAP mailbox,
a polling HTTP API — rather than pushing into ``POST /trigger``.

Contract for subclasses:

  * set ``name`` (must match the key in ``sources.json``),
  * implement ``run()`` as a long-lived loop that stops when ``self._stop``
    is set (use ``self._stop.wait(seconds)`` for backoff so shutdown is fast),
  * feed events through ``self.emit(...)``, which calls the server's single
    ingest path (same dedupe as HTTP),
  * keep ``self._status`` one of the documented strings for ``/health``.

``start()`` / ``stop()`` are called by the server; the thread is daemon so a
stuck network call can never keep the process alive.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional


class BaseSource:
    name = "base"
    #: string reported by /health when the source is configured but off
    disabled_status = "disabled"

    def __init__(self, config: Dict[str, Any],
                 ingest: Callable[..., Dict[str, Any]],
                 logger: logging.Logger):
        self.config: Dict[str, Any] = dict(config or {})
        self.enabled = bool(self.config.get("enabled", False))
        self.ingest = ingest
        self.log = logger.getChild(self.name)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status_lock = threading.Lock()
        self._status = "error" if self.enabled else self.disabled_status
        self._detail = "not started" if self.enabled else "disabled by config"
        self._fail_count = 0

    # -- lifecycle ------------------------------------------------------ #
    def start(self) -> None:
        if not self.enabled:
            self.log.info("source disabled by config")
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_guarded, name="source-%s" % self.name, daemon=True)
        self._thread.start()
        self.log.info("source thread started")

    def _run_guarded(self) -> None:
        try:
            self.run()
        except Exception as exc:  # a source must never kill the server
            if not self._stop.is_set():
                self.log.exception("source crashed: %s", exc)
                self.set_status("error", "crashed: %s" % exc)

    def run(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def stop(self) -> None:
        self._stop.set()
        try:
            self.on_stop()
        except Exception:
            self.log.exception("on_stop failed")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self.log.info("source stopped")

    def on_stop(self) -> None:
        """Optional hook to close sockets / abort blocking reads."""

    # -- reporting ------------------------------------------------------ #
    def healthy(self) -> str:
        with self._status_lock:
            return self._status

    def status_detail(self) -> str:
        with self._status_lock:
            return self._detail

    def set_status(self, status: str, detail: str = "") -> None:
        with self._status_lock:
            changed = (status != self._status)
            self._status = status
            self._detail = detail
        if changed:
            self.log.info("status -> %s (%s)", status, detail)

    # -- ingest --------------------------------------------------------- #
    def emit(self, *, kind: str = "event", priority: int = 3, actor: str = "",
             text: str = "", payload: Optional[Any] = None,
             event_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not text or not str(text).strip():
            return None
        try:
            return self.ingest(
                source=self.name, kind=kind, priority=priority, actor=actor,
                text=str(text), payload=payload, event_id=event_id)
        except Exception as exc:  # DB hiccup must not kill the source thread
            self.log.warning("ingest failed: %s: %s", type(exc).__name__, exc)
            return None

    # -- helpers -------------------------------------------------------- #
    @staticmethod
    def _env_file(path: Path) -> Dict[str, str]:
        values: Dict[str, str] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                values[key.strip()] = val
        except OSError:
            return {}
        return values
