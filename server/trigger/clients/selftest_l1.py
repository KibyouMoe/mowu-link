#!/usr/bin/env python3
"""selftest_l1.py — L1 client-side filtering, with a fake bridge and fake IMAP.

Proves the 0.6.0 split from the client side:

  * MC: a chat line from our own player is skipped (``self_message``) and is
    **not POSTed**; ordinary lines still are.
  * MC/email: the old generic rules (deny patterns, rate limit, dedupe window,
    priority map) are gone from the clients — a rules file that would have
    blocked everything in 0.5.x is now ignored, and all ordinary messages
    arrive.
  * email: only the watched folder is read; a message from another folder is
    rejected as ``folder_not_watched``.

Uses subprocesses + a temp dir; no production port, DB, or cursor is touched.

    python3 clients/selftest_l1.py
"""

from __future__ import annotations

import email.message
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import ProxyHandler, Request, build_opener

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
OPENER = build_opener(ProxyHandler({}))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "18801"))
RESULTS: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print("%s  %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


def wait_until(predicate, timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise RuntimeError("timeout waiting for " + what)


def http_json(url: str, body: Optional[Dict[str, Any]] = None) -> Any:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers,
                  method="POST" if body is not None else "GET")
    with OPENER.open(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def free_port(start: int) -> int:
    for port in range(start, start + 50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                continue
        except OSError:
            return port
    raise RuntimeError("no free port near %d" % start)


# --------------------------------------------------------------------------- #
# MC client against clients/fake_bridge.py
# --------------------------------------------------------------------------- #

def mc_test(tmp: Path) -> None:
    port = free_port(BRIDGE_PORT)
    bridge_url = "http://127.0.0.1:%d" % port
    bridge_log = tmp / "bridge.log"
    bridge = subprocess.Popen(
        [PY, str(HERE / "fake_bridge.py"), "--port", str(port)],
        cwd=str(HERE), stdout=open(bridge_log, "w"), stderr=subprocess.STDOUT)
    mc_log = tmp / "mc.log"
    drops = tmp / "mc-drops.log"
    cursor = tmp / "mc-cursor.json"
    # Deliberately hostile 0.5.x rules: deny noreply, 1 msg/min, 60 s dedupe,
    # priority map.  All of it must be ignored by the 0.6.0 client.
    old_rules = tmp / "old-mc-rules.json"
    old_rules.write_text(json.dumps({
        "default_priority": 0,
        "deny_patterns": ["noreply", "newsletter"],
        "rate_limit": {"max": 1, "per_seconds": 60},
        "dedupe_window_seconds": 60,
        "priority_map": {"hello": 0},
    }), encoding="utf-8")

    client = subprocess.Popen(
        [PY, str(HERE / "mc_chat_client.py"),
         "--url", bridge_url + "/stream",
         "--trigger-url", bridge_url + "/trigger",
         "--cursor-file", str(cursor),
         "--rules", str(old_rules),
         "--drops-log", str(drops),
         "--log", str(mc_log),
         "--no-bus", "--own-name", "SuperCreek",
         "--reconnect-seconds", "0.3"],
        cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def mc_text() -> str:
        try:
            return mc_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    try:
        wait_until(lambda: "connected to" in mc_text(), 10,
                   "mc client connected")
        check("mc: client connected to fake bridge", True, "")
        check("mc: old --rules file is ignored with a WARNING",
              "--rules" in mc_text() and "ignored" in mc_text(), "")

        http_json(bridge_url + "/emit",
                  {"text": "hello from Steve", "player": "Steve"})
        wait_until(lambda: len(http_json(bridge_url + "/records")["records"]) >= 1,
                   5, "first message delivered")

        http_json(bridge_url + "/emit",
                  {"text": "my own line", "player": "SuperCreek"})
        wait_until(lambda: "self_message" in (drops.read_text(
            encoding="utf-8", errors="replace")
            if drops.exists() else ""), 5, "self_message in drops.log")

        http_json(bridge_url + "/emit",
                  {"text": "repeat me", "player": "Steve"})
        http_json(bridge_url + "/emit",
                  {"text": "repeat me", "player": "Steve"})
        http_json(bridge_url + "/emit",
                  {"text": "noreply newsletter sale", "player": "Steve"})
        wait_until(lambda: len(http_json(bridge_url + "/records")["records"]) >= 4,
                   8, "four delivered messages")

        records = http_json(bridge_url + "/records")["records"]
        actors = [r.get("actor") for r in records]
        texts = [r.get("text") for r in records]
        check("mc: self message was NOT POSTed",
              "SuperCreek" not in actors and "my own line" not in texts,
              "actors=%s" % actors)
        check("mc: ordinary message WAS POSTed",
              texts[0] == "hello from Steve", "texts=%s" % texts)
        check("mc: duplicate text within 60 s both delivered (no dedupe)",
              texts.count("repeat me") == 2, "texts=%s" % texts)
        check("mc: deny_patterns no longer block (noreply delivered)",
              "noreply newsletter sale" in texts, "texts=%s" % texts)
        check("mc: rate_limit max=1 no longer blocks (4 delivered)",
              len(records) == 4, "records=%d" % len(records))
        check("mc: priority comes from the client, not the old map",
              records[0].get("priority") == 2,
              "priority=%r" % records[0].get("priority"))
        check("mc: ordinary chat is level=info (never wakes)",
              records[0].get("level") == "info",
              "level=%r" % records[0].get("level"))
        lines = drops.read_text(encoding="utf-8").strip().splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
        check("mc: exactly one L1 skip, reason=self_message",
              len(entries) == 1 and entries[0]["reason"] == "self_message",
              "drops=%s" % [e.get("reason") for e in entries])
    finally:
        for proc in (client, bridge):
            if proc.poll() is None:
                proc.send_signal(2)
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        bridge_log.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Email client against a fake IMAP object
# --------------------------------------------------------------------------- #

def _raw_mail(sender: str, subject: str, body: str) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Sun, 08 Nov 2026 10:00:00 +0800"
    msg.set_content(body)
    return msg.as_bytes()


RAW_MAILS = {
    101: _raw_mail("NoReply <noreply@shop.example.com>",
                   "Weekly newsletter", "unsubscribe here"),
    102: _raw_mail("HR <hr@acme.example.com>",
                   "面试邀请", "想约你本周三下午面试"),
}


class FakeIMAP:
    """Minimal IMAP4_SSL stand-in: one mailbox, UIDVALIDITY 42, UIDNEXT 103."""

    selected: List[str] = []
    fetched: List[int] = []

    def __init__(self, host: str, port: int, timeout: Optional[float] = None):
        self.host, self.port = host, port

    def login(self, user: str, password: str):
        return "OK", [b"logged in"]

    def select(self, folder: str, readonly: bool = True):
        FakeIMAP.selected.append(folder)
        return "OK", [b"2"]

    def response(self, key: str):
        return "UIDVALIDITY", [b"42"]

    def status(self, folder: str, what: str):
        return "OK", [b"INBOX (UIDNEXT 103)"]

    def uid(self, command: str, *args):
        if command.upper() == "SEARCH":
            return "OK", [b"101 102"]
        if command.upper() == "FETCH":
            uid = int(args[0])
            FakeIMAP.fetched.append(uid)
            raw = RAW_MAILS[uid]
            return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]
        return "OK", [b""]

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


def email_test(tmp: Path) -> None:
    import imaplib
    sys.path.insert(0, str(HERE))
    import email_client

    imaplib.IMAP4_SSL = FakeIMAP  # type: ignore[assignment]

    secrets = tmp / "email.env"
    secrets.write_text("EMAIL_HOST=imap.test\nEMAIL_USER=me@example.com\n"
                       "EMAIL_PASS=secret\nEMAIL_PORT=993\n", encoding="utf-8")
    cursor = tmp / "email_cursor.json"
    cursor.write_text(json.dumps({
        "folder": "INBOX", "uidvalidity": 42, "uidnext": 103,
        "last_uid": 100}), encoding="utf-8")
    log_path = tmp / "email.log"
    drops = tmp / "email-drops.log"

    args = email_client.parse_args([
        "--secrets", str(secrets), "--cursor", str(cursor),
        "--log", str(log_path), "--drops-log", str(drops), "--no-bus",
        "--folder", "INBOX", "--trigger-url", "http://127.0.0.1:1/trigger"])
    email_client.setup_logging(log_path, verbose=False)
    client = email_client.EmailClient(args)
    posted: List[Dict[str, Any]] = []

    def fake_post(envelope: Dict[str, Any]) -> Dict[str, Any]:
        posted.append(envelope)
        return {"ok": True, "id": len(posted), "duplicate": False}

    client.post = fake_post  # type: ignore[assignment]
    summary = client.poll_once()

    check("email: fake IMAP read exactly the watched folder",
          FakeIMAP.selected == ["INBOX"], "selected=%s" % FakeIMAP.selected)
    check("email: two mails checked, two passed",
          summary.get("checked") == 2 and summary.get("passed") == 2
          and summary.get("blocked") == 0, json.dumps(summary))
    check("email: both mails POSTed", len(posted) == 2,
          "posted=%d" % len(posted))
    check("email: noreply/newsletter delivered (deny_patterns gone)",
          any("noreply" in str(p.get("actor")) for p in posted),
          "actors=%s" % [p.get("actor") for p in posted])
    check("email: no generic priority map (both stay P1)",
          [p.get("priority") for p in posted] == [1, 1],
          "priorities=%s" % [p.get("priority") for p in posted])
    check("email: envelopes are level=info (never wake)",
          [p.get("level") for p in posted] == ["info", "info"],
          "levels=%s" % [p.get("level") for p in posted])
    check("email: no L1 skip logged for INBOX",
          not drops.exists() or not drops.read_text().strip(), "")

    # Direct call with a different folder proves the folder guard.
    outcome = client.handle("other@example.com", "hello", "body",
                            "(test)", 900001, 0, folder="Archive")
    entries = []
    if drops.exists():
        entries = [json.loads(line) for line in
                   drops.read_text(encoding="utf-8").splitlines() if line.strip()]
    check("email: other folder rejected, not POSTed",
          outcome == "blocked" and len(posted) == 2,
          "outcome=%r posted=%d" % (outcome, len(posted)))
    check("email: drops.log records folder_not_watched",
          len(entries) == 1 and entries[0]["reason"] == "folder_not_watched",
          "reasons=%s" % [e.get("reason") for e in entries])
    check("email: IMAP was never told to select the other folder",
          FakeIMAP.selected == ["INBOX"], "selected=%s" % FakeIMAP.selected)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="trigger-l1-selftest-"))
    print("temp: %s" % tmp, flush=True)
    try:
        mc_test(tmp)
        email_test(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("-" * 78)
    print("%d/%d PASS, %d FAIL" % (passed, len(RESULTS), failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
