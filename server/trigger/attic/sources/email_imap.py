"""email source — IMAP polling adapter.

Credentials come from ``~/.hermes/secrets/email.env`` (override with
``TRIGGER_EMAIL_ENV``), simple ``KEY=VALUE`` lines::

    EMAIL_USER=me@example.com
    EMAIL_PASSWORD=authorization-code

If the secrets file is missing or incomplete the source logs one INFO line and
stays disabled — it never raises, never exits, and never blocks startup.

Only headers are fetched (``BODY.PEEK[HEADER]``) with the mailbox opened
read-only, so the adapter does not mark mail as read. The first successful
poll records the current highest UID as a baseline: existing mail is not
replayed, only messages that arrive afterwards are ingested. State lives in
``state/email.json`` (override with ``TRIGGER_EMAIL_STATE``).

Set ``"dry_run": true`` in the source config (or ``TRIGGER_EMAIL_DRY_RUN=1``)
to exercise the loop without opening a connection.
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import time
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .base import BaseSource

DEFAULT_SECRETS = "~/.hermes/secrets/email.env"
DEFAULT_STATE = str(Path(__file__).resolve().parent.parent / "state" / "email.json")


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


class EmailIMAPSource(BaseSource):
    name = "email"

    def __init__(self, config, ingest, logger):
        super().__init__(config, ingest, logger)
        self.host = str(config.get("host") or "")
        self.port = int(config.get("port", 993) or 993)
        self.use_ssl = bool(config.get("ssl", True))
        self.folder = str(config.get("folder") or "INBOX")
        self.poll_seconds = float(config.get("poll_seconds", 120) or 120)
        self.max_per_poll = int(config.get("max_per_poll", 20) or 20)
        self.dry_run = bool(config.get("dry_run", False)) or \
            os.environ.get("TRIGGER_EMAIL_DRY_RUN") == "1"
        self.secrets_path = Path(os.path.expanduser(
            config.get("secrets_file") or os.environ.get("TRIGGER_EMAIL_ENV")
            or DEFAULT_SECRETS))
        self.state_path = Path(os.path.expanduser(
            os.environ.get("TRIGGER_EMAIL_STATE") or DEFAULT_STATE))
        self._state: Dict[str, Any] = {}

    # -- main loop ------------------------------------------------------ #
    def run(self) -> None:
        creds = self._credentials()
        if creds is None:
            self.set_status("disabled", "no credentials in %s" % self.secrets_path)
            self.log.info("email source skipped: %s missing or lacks "
                          "EMAIL_USER/EMAIL_PASSWORD", self.secrets_path)
            return
        user, password = creds
        if self.dry_run:
            self.log.info("email dry-run: would poll %s:%s as %s every %.0fs",
                          self.host or "<unset host>", self.port, user,
                          self.poll_seconds)
        if not self.host:
            self.set_status("disabled", "no IMAP host configured")
            self.log.info("email source disabled: no host in config")
            return

        while not self._stop.is_set():
            try:
                count = self._poll_once(user, password)
                self._fail_count = 0
                self.set_status("ok", "%d new message(s)" % count)
                if count:
                    self.log.info("email poll: ingested %d message(s)", count)
            except Exception as exc:
                self._note_failure("%s: %s" % (type(exc).__name__, exc))
            if self._stop.wait(self.poll_seconds):
                break

    # -- credentials ---------------------------------------------------- #
    def _credentials(self) -> Optional[Tuple[str, str]]:
        env = self._env_file(self.secrets_path)
        user = (env.get("EMAIL_USER") or env.get("EMAIL_ACCOUNT")
                or str(self.config.get("user") or "")).strip()
        password = (env.get("EMAIL_PASSWORD") or env.get("EMAIL_PASS")
                    or env.get("EMAIL_AUTH_CODE") or "").strip()
        if not user or not password:
            return None
        return user, password

    # -- polling -------------------------------------------------------- #
    def _poll_once(self, user: str, password: str) -> int:
        if self.dry_run:
            self.log.info("email dry-run: connection to %s:%d skipped",
                          self.host, self.port)
            return 0
        conn = self._connect()
        try:
            typ, data = conn.select(self.folder, readonly=True)
            if typ != "OK":
                raise RuntimeError("SELECT %s failed: %r" % (self.folder, data))
            uidvalidity = self._uidvalidity(conn)
            uids = self._all_uids(conn)
            if not uids:
                return 0
            state = self._load_state()
            if (not state.get("last_uid")
                    or (uidvalidity and state.get("uidvalidity")
                        and uidvalidity != state.get("uidvalidity"))):
                state = {"last_uid": max(uids), "uidvalidity": uidvalidity,
                         "folder": self.folder, "updated_at": time.time()}
                self._save_state(state)
                self.log.info("email baseline set at UID %d (%d message(s) "
                              "already present, not replayed)",
                              max(uids), len(uids))
                return 0
            last_uid = int(state["last_uid"])
            new_uids = [u for u in uids if u > last_uid][: self.max_per_poll]
            ingested = 0
            for uid in new_uids:
                if self._stop.is_set():
                    break
                if self._ingest_uid(conn, uid):
                    ingested += 1
                last_uid = max(last_uid, uid)
            if new_uids:
                self._state = {"last_uid": last_uid, "uidvalidity": uidvalidity,
                               "folder": self.folder, "updated_at": time.time()}
                self._save_state(self._state)
            return ingested
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _connect(self):
        if self.use_ssl:
            return imaplib.IMAP4_SSL(self.host, self.port, timeout=30)
        return imaplib.IMAP4(self.host, self.port, timeout=30)

    @staticmethod
    def _uidvalidity(conn) -> Optional[int]:
        try:
            typ, data = conn.response("UIDVALIDITY")
            if typ == "UIDVALIDITY" and data and data[0]:
                return int(data[0])
        except Exception:
            pass
        return None

    @staticmethod
    def _all_uids(conn):
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        out = []
        for chunk in data[0].split():
            try:
                out.append(int(chunk))
            except ValueError:
                continue
        return sorted(out)

    def _ingest_uid(self, conn, uid: int) -> bool:
        typ, data = conn.uid("fetch", str(uid), "(BODY.PEEK[HEADER])")
        if typ != "OK" or not data:
            self.log.debug("email UID %d fetch returned %r", uid, typ)
            return False
        raw = b""
        for part in data:
            if isinstance(part, tuple) and len(part) >= 2:
                raw = part[1]
                break
        if not raw:
            return False
        msg = email.message_from_bytes(raw)
        subject = _decode(msg.get("Subject")).strip() or "(no subject)"
        sender_name, sender_addr = parseaddr(_decode(msg.get("From")))
        actor = sender_addr or sender_name
        date = _decode(msg.get("Date"))
        message_id = (msg.get("Message-ID") or "").strip()
        event_id = ("email:%s" % message_id if message_id
                    else "email:%s:%s:%d" % (self.host, self.folder, uid))
        payload = {
            "from": _decode(msg.get("From")),
            "from_addr": sender_addr,
            "to": _decode(msg.get("To")),
            "subject": subject,
            "date": date,
            "message_id": message_id,
            "uid": uid,
            "folder": self.folder,
            "host": self.host,
        }
        result = self.emit(kind="mail", priority=2, actor=actor, text=subject,
                           payload=payload, event_id=event_id)
        return bool(result) and not result.get("duplicate")

    # -- state ---------------------------------------------------------- #
    def _load_state(self) -> Dict[str, Any]:
        if self._state:
            return self._state
        try:
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(self._state, dict):
                self._state = {}
        except (OSError, ValueError):
            self._state = {}
        return self._state

    def _save_state(self, state: Dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            self.log.warning("cannot write email state %s: %s",
                             self.state_path, exc)

    # -- failure logging ------------------------------------------------ #
    def _note_failure(self, detail: str) -> None:
        first = self._fail_count == 0
        self._fail_count += 1
        self.set_status("error", detail)
        if first:
            self.log.warning("email poll failed (%s); retrying every %.0fs",
                             detail, self.poll_seconds)
        elif self._fail_count % 10 == 0:
            self.log.info("email still failing after %d attempts (%s)",
                          self._fail_count, detail)
        else:
            self.log.debug("email retry #%d: %s", self._fail_count, detail)


SOURCE = EmailIMAPSource
