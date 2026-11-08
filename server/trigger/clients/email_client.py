#!/usr/bin/env python3
"""email_client — IMAP inbox → mowu-trigger inbox.

One source, one client. Credentials are read from
``~/.hermes/secrets/email.env`` (never from argv, never logged):

    EMAIL_HOST=imap.example.com
    EMAIL_PORT=993
    EMAIL_USER=you@example.com
    EMAIL_PASS=<app-specific password / 授权码>

Cursor: ``clients/state/email_cursor.json`` keyed by UIDVALIDITY, holding the
last handled UID and the advertised UIDNEXT. First run on a mailbox records a
baseline (UIDNEXT-1) so history is not replayed; UIDVALIDITY changing resets
the baseline.

**L1 only** (0.6.0): the only business rule here is "read this one folder"
(``--folder``).  ``noreply``/newsletter denies, rate limits and dedupe windows
moved to the server's L2 rules (``~/.hermes/trigger-rules.json``); ``--rules``
is accepted but ignored.  Every envelope is sent as ``level=info``: mail is
delivered to worker subscriptions but never wakes the agent by itself.

Polling: every 120 s by default. IMAP IDLE is deliberately **not** used —
polling works on every server (including the ones that reject IDLE) and the
cursor makes a poll idempotent, so IDLE would only save latency, not
correctness.

Usage:
    python3 email_client.py --once          # one poll, then exit
    python3 email_client.py                 # poll forever (120 s)
    python3 email_client.py --stub          # inject the built-in test mails
    python3 email_client.py --stub --dry-run

Exit codes: 0 ok, 1 runtime error, 2 credentials missing/unusable.

Standard library only. Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import time
from email import policy as email_policy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from filterwall import business_filter, log_skip, schema_problem
from localbus import LocalBus

HERE = Path(__file__).resolve().parent
DEFAULT_SECRETS = Path.home() / ".hermes" / "secrets" / "email.env"
DEFAULT_CURSOR = HERE / "state" / "email_cursor.json"
DEFAULT_LOG = HERE / "email_client.log"
DEFAULT_BUS_PORT = 8793
DEFAULT_DROPS = HERE / "drops.log"
DEFAULT_TRIGGER_URL = "http://127.0.0.1:8791/trigger"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
RETRY_DELAYS = [0.5, 1.0, 2.0, 4.0]
SOURCE_NAME = "email"
BODY_HEAD_CHARS = 200
HEADER_FIELDS = ("FROM SUBJECT DATE CONTENT-TYPE CONTENT-TRANSFER-ENCODING")

_OPENER = build_opener(ProxyHandler({}))

log = logging.getLogger("email_client")

STUBS: Dict[str, Dict[str, str]] = {
    "noreply": {
        "actor": "NoReply <noreply@shop.example.com>",
        "subject": "本周新品 Weekly newsletter",
        "body": "限时折扣 5 折起，点此退订 unsubscribe。",
    },
    "interview": {
        "actor": "HR <hr@acme.example.com>",
        "subject": "面试邀请：后端工程师",
        "body": "你好，我们看了你的简历，想约你本周三下午面试，方便吗？",
    },
}

MISSING_CREDS_MESSAGE = """\
没有找到可用的邮箱凭据，未发起任何网络请求。

需要文件：{path}
文件格式（每行 KEY=VALUE，# 开头为注释）：

    EMAIL_HOST=imap.example.com
    EMAIL_PORT=993
    EMAIL_USER=you@example.com
    EMAIL_PASS=<IMAP 授权码，不是登录密码>

说明：Gmail / QQ 邮箱 / 163 等都需要先在网页端开启 IMAP 并生成「授权码」。
建好文件后重新运行：python3 {script}
"""


class MissingCredentials(Exception):
    """Raised when the secrets file is absent or incomplete."""


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
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(sh)


# --------------------------------------------------------------------------- #
# config / cursor
# --------------------------------------------------------------------------- #

def load_credentials(path: Path) -> Dict[str, str]:
    """Parse a KEY=VALUE env file. Raises MissingCredentials with a hint."""
    if not path.exists():
        raise MissingCredentials("file not found: %s" % path)
    values: Dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    except OSError as exc:
        raise MissingCredentials("cannot read %s: %s" % (path, exc))

    missing = [k for k in ("EMAIL_HOST", "EMAIL_USER", "EMAIL_PASS")
               if not values.get(k)]
    if missing:
        raise MissingCredentials("missing field(s) in %s: %s"
                                 % (path, ", ".join(missing)))
    if not values.get("EMAIL_PORT"):
        values["EMAIL_PORT"] = "993"
    return values


def load_cursor(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_cursor(path: Path, cursor: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(cursor)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(str(tmp), str(path))


# --------------------------------------------------------------------------- #
# message parsing
# --------------------------------------------------------------------------- #

def _flatten_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return value


def _body_head(msg: Any, limit: int = BODY_HEAD_CHARS) -> str:
    content = ""
    try:
        if msg.is_multipart():
            part = msg.get_body(preferencelist=("plain", "html"))
            content = "" if part is None else part.get_content()
        else:
            content = msg.get_content()
    except Exception:  # noqa: BLE001 - broken charsets are common
        try:
            raw = msg.get_payload(decode=True) or b""
            content = raw.decode("utf-8", "replace")
        except Exception:
            content = ""
    if not isinstance(content, str):
        content = str(content)
    if "<html" in content.lower() or "<body" in content.lower():
        content = _flatten_html(content)
    return " ".join(content.split())[:limit]


def parse_message(raw: bytes) -> Tuple[str, str, str, str]:
    """Return (sender, subject, body_head, date) from raw RFC822 bytes."""
    msg = email.message_from_bytes(raw, policy=email_policy.default)
    sender = str(msg.get("From", "") or "").strip()
    subject = str(msg.get("Subject", "") or "").strip()
    date = str(msg.get("Date", "") or "").strip()
    return sender, subject, _body_head(msg), date


def compose_text(subject: str, body_head: str) -> str:
    subject = subject.strip()
    body_head = body_head.strip()
    if subject and body_head:
        return "%s — %s" % (subject, body_head)
    return subject or body_head or "(empty message)"


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #

class EmailClient:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.trigger_url = args.trigger_url
        self.dry_run = bool(args.dry_run)
        self.poll_seconds = float(args.poll_seconds)
        self.folder = args.folder
        self.cursor_path = Path(os.path.expanduser(args.cursor))
        self.stop = False
        self.drops_log = args.drops_log
        if getattr(args, "rules", None):
            log.warning("--rules %s ignored: L2 rules moved to the trigger "
                        "server (~/.hermes/trigger-rules.json)", args.rules)
        self.bus: Optional[LocalBus] = None
        if not args.no_bus:
            self.bus = LocalBus(SOURCE_NAME, port=args.listen_port)
        self.imap: Optional[imaplib.IMAP4_SSL] = None
        self.posted = 0
        self.blocked = 0
        self.duplicates = 0

    # -- credential handling --------------------------------------------- #
    def credentials(self) -> Dict[str, str]:
        return load_credentials(Path(os.path.expanduser(self.args.secrets)))

    # -- imap -------------------------------------------------------------- #
    def connect(self, creds: Dict[str, str]) -> imaplib.IMAP4_SSL:
        host = creds["EMAIL_HOST"]
        port = int(creds.get("EMAIL_PORT") or 993)
        log.info("connecting to %s:%d as %s", host, port, creds["EMAIL_USER"])
        imap = imaplib.IMAP4_SSL(host, port, timeout=30)
        imap.login(creds["EMAIL_USER"], creds["EMAIL_PASS"])
        typ, _ = imap.select(self.folder, readonly=True)
        if typ != "OK":
            raise RuntimeError("SELECT %s failed: %s" % (self.folder, typ))
        return imap

    def logout(self) -> None:
        if self.imap is None:
            return
        try:
            self.imap.close()
        except Exception:
            pass
        try:
            self.imap.logout()
        except Exception:
            pass
        self.imap = None

    @staticmethod
    def _uidvalidity(imap: imaplib.IMAP4_SSL) -> int:
        try:
            _, data = imap.response("UIDVALIDITY")
            value = data[0]
            if isinstance(value, bytes):
                value = value.decode("ascii", "ignore")
            return int(str(value).strip())
        except (TypeError, ValueError, IndexError, AttributeError):
            return 0

    def _uidnext(self, imap: imaplib.IMAP4_SSL) -> int:
        try:
            typ, data = imap.status(self.folder, "(UIDNEXT)")
            if typ == "OK" and data and data[0]:
                match = re.search(r"UIDNEXT\s+(\d+)", str(data[0]))
                if match:
                    return int(match.group(1))
        except Exception:
            pass
        return 0

    def _fetch(self, imap: imaplib.IMAP4_SSL, uid: int) -> bytes:
        typ, data = imap.uid(
            "FETCH", str(uid),
            "(BODY.PEEK[HEADER.FIELDS (%s)] BODY.PEEK[TEXT])" % HEADER_FIELDS)
        if typ != "OK" or not data:
            return b""
        chunks = []
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and item[1]:
                chunks.append(item[1])
        return b"".join(chunks)

    def _search_new(self, imap: imaplib.IMAP4_SSL, last_uid: int) -> List[int]:
        typ, data = imap.uid("SEARCH", None, "UID", "%d:*" % (last_uid + 1))
        if typ != "OK" or not data or not data[0]:
            return []
        uids = []
        for token in data[0].split():
            try:
                value = int(token)
            except ValueError:
                continue
            if value > last_uid:
                uids.append(value)
        return sorted(set(uids))

    # -- one poll ---------------------------------------------------------- #
    def poll_once(self) -> Dict[str, Any]:
        creds = self.credentials()
        summary = {"checked": 0, "passed": 0, "blocked": 0, "queued": 0}
        imap = self.connect(creds)
        self.imap = imap
        try:
            uidvalidity = self._uidvalidity(imap)
            uidnext = self._uidnext(imap)
            cursor = load_cursor(self.cursor_path)
            last_uid = int(cursor.get("last_uid") or 0)
            same_mailbox = (int(cursor.get("uidvalidity") or 0) == uidvalidity
                            and uidvalidity > 0)

            if not cursor or not same_mailbox:
                baseline = max(0, uidnext - 1)
                save_cursor(self.cursor_path, {
                    "folder": self.folder, "uidvalidity": uidvalidity,
                    "uidnext": uidnext, "last_uid": baseline})
                log.info("baseline for %s: uidvalidity=%s uidnext=%s "
                         "last_uid=%d (history is not replayed)",
                         self.folder, uidvalidity, uidnext, baseline)
                return summary
            if uidnext and int(cursor.get("uidnext") or 0) != uidnext:
                log.debug("uidnext moved %s -> %s", cursor.get("uidnext"), uidnext)

            for uid in self._search_new(imap, last_uid):
                summary["checked"] += 1
                raw = self._fetch(imap, uid)
                if not raw:
                    log.warning("uid %d: empty fetch, skipped", uid)
                else:
                    sender, subject, body_head, date = parse_message(raw)
                    outcome = self.handle(sender, subject, body_head, date,
                                          uid, uidvalidity)
                    summary[outcome] = summary.get(outcome, 0) + 1
                last_uid = uid
                save_cursor(self.cursor_path, {
                    "folder": self.folder, "uidvalidity": uidvalidity,
                    "uidnext": uidnext, "last_uid": last_uid})

            if summary["checked"] == 0:
                log.debug("no new mail (last_uid=%d uidnext=%s)", last_uid, uidnext)
            return summary
        finally:
            self.logout()

    # -- L1 business filter ---------------------------------------------- #
    def business_filter(self, envelope: Dict[str, Any],
                        folder: str) -> Tuple[bool, str]:
        """Only mail from the watched folder is a business event."""
        keep, reason = business_filter(envelope)
        if not keep:
            log.debug("envelope rejected: %s", schema_problem(envelope))
            return False, reason
        if folder != self.folder:
            return False, "folder_not_watched"
        return True, ""

    # -- shared delivery path (also used by --stub) ------------------------ #
    def handle(self, sender: str, subject: str, body_head: str, date: str,
               uid: int, uidvalidity: int,
               folder: Optional[str] = None) -> str:
        folder = self.folder if folder is None else folder
        text = compose_text(subject, body_head)
        envelope = {
            "source": SOURCE_NAME, "kind": "email", "level": "info",
            "actor": sender,
            "subject": subject, "body": body_head, "text": text,
            "priority": 1, "folder": folder,
            "event_id": "%s:%s:%s" % (SOURCE_NAME, uidvalidity or 0, uid),
        }
        keep, reason = self.business_filter(envelope, folder)
        if not keep:
            self.blocked += 1
            log.info("SKIP [%s] folder=%s uid=%s from=%r subject=%r",
                     reason, folder, uid, sender, subject[:80])
            log_skip(self.drops_log, SOURCE_NAME, reason, text, sender)
            return "blocked"

        priority = int(envelope["priority"])
        envelope["payload"] = {"subject": subject, "uid": uid,
                               "uidvalidity": uidvalidity, "from": sender,
                               "date": date, "folder": folder}
        if self.bus is not None and not self.dry_run:
            published = self.bus.publish(envelope)
            log.debug("bus seq=%d event_id=%s", published["seq"],
                      envelope["event_id"])
        if self.dry_run:
            log.info("[dry-run] would POST uid=%s prio=%d from=%r subject=%r",
                     uid, priority, sender, subject[:80])
            return "queued"
        result = self.post(envelope)
        if result is None:
            log.error("DROP after retries: uid=%s subject=%r", uid, subject[:80])
            return "failed"
        if result.get("duplicate"):
            self.duplicates += 1
            log.info("delivered (duplicate) id=%s uid=%s", result.get("id"), uid)
            return "queued"
        self.posted += 1
        log.info("delivered id=%s prio=%d from=%r subject=%r",
                 result.get("id"), priority, sender, subject[:80])
        return "passed"

    def post(self, envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            request = Request(self.trigger_url, data=body, method="POST",
                              headers={"Content-Type":
                                       "application/json; charset=utf-8"})
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
                time.sleep(delay)
        return None

    # -- stubs -------------------------------------------------------------- #
    def run_stub(self, which: str) -> int:
        names = list(STUBS) if which in ("", "all", None) else [which]
        unknown = [n for n in names if n not in STUBS]
        if unknown:
            print("unknown stub(s): %s (available: %s)"
                  % (", ".join(unknown), ", ".join(STUBS)), file=sys.stderr)
            return 2
        results = []
        for index, name in enumerate(names, start=1):
            stub = STUBS[name]
            outcome = self.handle(stub["actor"], stub["subject"], stub["body"],
                                  date="(stub)", uid=900000 + index,
                                  uidvalidity=0)
            results.append((name, outcome))
            print("stub %-10s -> %s" % (name, outcome))
        stats = {"posted": self.posted, "duplicates": self.duplicates,
                 "l1_blocked": self.blocked}
        print(json.dumps({"stubs": len(results),
                          "passed": sum(1 for _, o in results
                                        if o in ("passed", "queued")),
                          "blocked": sum(1 for _, o in results
                                         if o == "blocked"),
                          "client": stats}, ensure_ascii=False))
        print("drops.log 追加记录见：%s" % self.args.drops_log)
        return 0

    # -- loops -------------------------------------------------------------- #
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
        self.start_bus()
        try:
            return self._run()
        finally:
            if self.bus is not None:
                self.bus.stop()

    def _run(self) -> int:
        if self.args.stub is not None:
            return self.run_stub(self.args.stub)

        try:
            self.credentials()
        except MissingCredentials as exc:
            print(MISSING_CREDS_MESSAGE.format(
                path=self.args.secrets,
                script=os.path.basename(__file__)), file=sys.stderr)
            print("detail: %s" % exc, file=sys.stderr)
            return 2

        if self.args.once:
            summary = self.poll_once()
            print(json.dumps(summary, ensure_ascii=False))
            return 0

        log.info("polling every %.0fs (IMAP IDLE not used)", self.poll_seconds)
        while not self.stop:
            try:
                summary = self.poll_once()
                if summary.get("checked"):
                    log.info("poll: %s", json.dumps(summary, ensure_ascii=False))
            except MissingCredentials:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("poll failed: %s: %s", type(exc).__name__, exc)
                self.logout()
            for _ in range(int(max(1, self.poll_seconds))):
                if self.stop:
                    break
                time.sleep(1)
        return 0


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IMAP source client: poll -> L1 business filter -> POST /trigger")
    parser.add_argument("--secrets", default=str(DEFAULT_SECRETS),
                        help="credentials env file (default %(default)s)")
    parser.add_argument("--rules", default=None,
                        help="IGNORED since 0.6.0: L2 rules live on the server "
                             "(~/.hermes/trigger-rules.json); the flag is "
                             "accepted for old scripts")
    parser.add_argument("--cursor", default=str(DEFAULT_CURSOR),
                        help="UID cursor file (default %(default)s)")
    parser.add_argument("--drops-log", default=str(DEFAULT_DROPS),
                        help="blocked-message log (default %(default)s)")
    parser.add_argument("--log", default=str(DEFAULT_LOG),
                        help="client log file (default %(default)s)")
    parser.add_argument("--trigger-url", default=DEFAULT_TRIGGER_URL,
                        help="trigger inbox URL (default %(default)s)")
    parser.add_argument("--folder", default="INBOX",
                        help="IMAP folder (default %(default)s)")
    parser.add_argument("--poll-seconds", type=float, default=120.0,
                        help="poll interval in seconds (default %(default)s)")
    parser.add_argument("--once", action="store_true",
                        help="run exactly one poll, then exit")
    parser.add_argument("--listen-port", type=int, default=DEFAULT_BUS_PORT,
                        help="local bus port on 127.0.0.1 (default %(default)s)")
    parser.add_argument("--no-bus", action="store_true",
                        help="do not expose the local bus at all")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate + log, never POST or publish")
    parser.add_argument("--stub", nargs="?", const="all", default=None,
                        metavar="NAME",
                        help="inject built-in test mail(s) instead of IMAP; "
                             "NAME is 'noreply' or 'interview' "
                             "(no value = both)")
    parser.add_argument("--verbose", action="store_true",
                        help="DEBUG level in log file and stderr")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(Path(os.path.expanduser(args.log)), args.verbose)
    client = EmailClient(args)

    def _shutdown(signum, _frame):
        log.info("signal %s received; stopping", signum)
        client.stop = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        return client.run()
    except MissingCredentials as exc:
        print(MISSING_CREDS_MESSAGE.format(
            path=args.secrets, script=os.path.basename(__file__)),
            file=sys.stderr)
        print("detail: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("fatal: %s: %s", type(exc).__name__, exc)
        print("error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
