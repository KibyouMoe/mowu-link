#!/usr/bin/env python3
"""selftest_mc_cursor.py — end-to-end tests for the mc_chat source cursor.

Starts ``fake_bridge.py`` and real ``mc_chat_client.py`` subprocesses; no
Minecraft, no 8791, no production state touched (dedupe/drops/cursor/log all
live under a temp dir).

    /usr/bin/python3 selftest_mc_cursor.py

Cases:
  01 first run: no cursor -> baseline 0, then 3 events processed, last_seq=3
  02 restart: resumes at 3, only event 4 is processed
  03 bridge replays its buffer anyway: client-side skip still prevents dupes
  04 cursor deleted: WARNING + baseline at current lastSeq, no replay
  05 cursor corrupt '{bad json': same, no replay
  06 bridge seq goes backwards (game restart): re-baseline, no replay
  07 --lookback 5 accepts a boundary event by ts, rejects an old one
  08 --no-cursor: old behaviour, no cursor file written
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.error import URLError
from urllib.request import HTTPError, ProxyHandler, Request, build_opener

HERE = Path(__file__).resolve().parent
CLIENT = HERE / "mc_chat_client.py"
BRIDGE = HERE / "fake_bridge.py"
PY = sys.executable or "/usr/bin/python3"
OPENER = build_opener(ProxyHandler({}))
TZ_CST = timezone(timedelta(hours=8))

RESULTS: List[Dict[str, Any]] = []


def now_iso() -> str:
    return datetime.now(TZ_CST).isoformat(timespec="seconds")


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None,
              timeout: float = 5.0) -> Dict[str, Any]:
    data = None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, method=method, headers=headers)
    with OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_until(predicate: Callable[[], Any], timeout: float = 10.0,
               interval: float = 0.05, what: str = "condition") -> Any:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError("timeout waiting for %s (last=%r)" % (what, last))


class Bridge:
    def __init__(self) -> None:
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = subprocess.Popen(
            [PY, str(BRIDGE), "--port", str(self.port)],
            cwd=str(HERE), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        wait_until(lambda: self.state() is not None, 10, what="fake bridge up")

    def state(self) -> Optional[Dict[str, Any]]:
        try:
            return http_json("GET", self.base + "/state", timeout=1)
        except (URLError, HTTPError, OSError, ValueError):
            return None

    def emit(self, text: str, player: str = "Steve",
             ts: Optional[float] = None) -> int:
        body: Dict[str, Any] = {"text": text, "player": player}
        if ts is not None:
            body["ts"] = ts
        return int(http_json("POST", self.base + "/emit", body)["event"]["seq"])

    def reset(self) -> None:
        http_json("POST", self.base + "/reset", {})

    def config(self, **kwargs: Any) -> None:
        http_json("POST", self.base + "/config", kwargs)

    def records(self) -> List[Dict[str, Any]]:
        return http_json("GET", self.base + "/records")["records"]

    def meta(self) -> Dict[str, Any]:
        return http_json("GET", self.base + "/records")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Client:
    def __init__(self, cursor: Path, log: Path, rules: Path,
                 extra: Optional[List[str]] = None) -> None:
        self.log = log
        args = [PY, str(CLIENT),
                "--url", BRIDGE_URL + "/stream",
                "--trigger-url", BRIDGE_URL + "/trigger",
                "--cursor-file", str(cursor),
                "--rules", str(rules),
                "--drops-log", str(cursor.parent / "drops.log"),
                "--log", str(log),
                "--no-bus",
                "--reconnect-seconds", "0.5"]
        args.extend(extra or [])
        self.proc = subprocess.Popen(args, cwd=str(HERE),
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)

    def text(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def wait_log(self, needle: str, timeout: float = 10.0) -> str:
        wait_until(lambda: needle in self.text(), timeout,
                   what="log line %r in %s" % (needle, self.log.name))
        return self.text()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(6)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(3)


def read_cursor(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def texts(records: List[Dict[str, Any]]) -> List[str]:
    return [str(r.get("text") or "") for r in records]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mc-cursor-selftest-"))
    cursor = tmp / "mc_chat_cursor.json"
    rules = tmp / "mc_chat.json"
    rules.write_text(json.dumps({
        "default_priority": 2,
        "rate_limit": {"max": 500, "per_seconds": 60},
        "dedupe_window_seconds": 3,
        "dedupe_state": str(tmp / "dedupe.json"),
    }), encoding="utf-8")

    global BRIDGE_URL
    bridge = Bridge()
    BRIDGE_URL = bridge.base
    print("fake bridge: %s   temp: %s" % (bridge.base, tmp), flush=True)

    try:
        # ---------------------------------------------------------------- #
        # 01 — first run, no cursor: baseline 0, then process 3 events
        # ---------------------------------------------------------------- #
        log1 = tmp / "client-01.log"
        client = Client(cursor, log1, rules)
        client.wait_log("connected to")
        client.wait_log("cursor missing/corrupt")
        wait_until(lambda: cursor.exists() and read_cursor(cursor)["last_seq"] == 0,
                   5, what="baseline cursor 0")
        base = len(bridge.records())
        for text in ("one", "two", "three"):
            bridge.emit(text)
        wait_until(lambda: len(bridge.records()) == base + 3, 10,
                   what="3 deliveries")
        wait_until(lambda: read_cursor(cursor)["last_seq"] == 3, 5,
                   what="cursor last_seq=3")
        client.stop()
        saved = read_cursor(cursor)
        check("01 baseline at 0 on first run",
              "baselined at 0 (no replay)" in log1.read_text(encoding="utf-8"),
              "log has 'baselined at 0'")
        check("01b 3 events delivered, cursor last_seq=3",
              texts(bridge.records())[-3:] == ["one", "two", "three"]
              and saved["last_seq"] == 3,
              "last_seq=%s texts=%s" % (saved["last_seq"],
                                        texts(bridge.records())[-3:]))
        check("01c cursor file shape",
              saved.get("source") == "mc_chat"
              and saved.get("bridge") == bridge.base
              and str(saved.get("saved_at", "")).endswith("+08:00"),
              json.dumps(saved, ensure_ascii=False))

        # ---------------------------------------------------------------- #
        # 02 — restart: resume at 3, only event 4 is delivered
        # ---------------------------------------------------------------- #
        log2 = tmp / "client-02.log"
        base = len(bridge.records())
        client = Client(cursor, log2, rules)
        client.wait_log("resuming from cursor: last_seq=3")
        since_at_resume = bridge.meta()["stream_sinces"][-1]
        bridge.emit("four")
        wait_until(lambda: len(bridge.records()) == base + 1, 10,
                   what="event 4 delivered")
        time.sleep(0.3)
        client.stop()
        delivered = texts(bridge.records())[base:]
        check("02 restart resumes at 3, only event 4 delivered",
              delivered == ["four"] and read_cursor(cursor)["last_seq"] == 4
              and since_at_resume == "3",
              "delivered=%s last_seq=%s since=%s" %
              (delivered, read_cursor(cursor)["last_seq"], since_at_resume))

        # ---------------------------------------------------------------- #
        # 03 — bridge ignores since and replays everything; client must skip
        # ---------------------------------------------------------------- #
        bridge.config(ignore_since=True)
        log3 = tmp / "client-03.log"
        base = len(bridge.records())
        frames_before = bridge.meta()["frames_sent"]
        client = Client(cursor, log3, rules)
        client.wait_log("resuming from cursor: last_seq=4")
        time.sleep(1.5)  # bridge has replayed seq 1..4 by now
        replayed = len(bridge.records()) - base
        frames_replayed = bridge.meta()["frames_sent"] - frames_before
        bridge.emit("five")
        wait_until(lambda: len(bridge.records()) == base + 1, 10,
                   what="event 5 delivered")
        time.sleep(0.3)
        client.stop()
        delivered = texts(bridge.records())[base:]
        check("03 buffered replay skipped (seq <= cursor)",
              replayed == 0 and delivered == ["five"] and frames_replayed >= 4,
              "replayed=%d frames_resent=%d delivered=%s" %
              (replayed, frames_replayed, delivered))

        # ---------------------------------------------------------------- #
        # 04 — cursor deleted: warn + baseline, no replay
        # ---------------------------------------------------------------- #
        log4 = tmp / "client-04.log"
        cursor.unlink()
        base = len(bridge.records())
        frames_before = bridge.meta()["frames_sent"]
        client = Client(cursor, log4, rules)
        client.wait_log("cursor missing/corrupt")
        client.wait_log("baselined at 5 (no replay)")
        time.sleep(1.2)  # bridge replays 1..5, all must be skipped
        replayed = len(bridge.records()) - base
        frames_replayed = bridge.meta()["frames_sent"] - frames_before
        bridge.emit("six")
        wait_until(lambda: len(bridge.records()) == base + 1, 10,
                   what="event 6 delivered")
        time.sleep(0.3)
        client.stop()
        delivered = texts(bridge.records())[base:]
        check("04 deleted cursor: baselined, no replay",
              replayed == 0 and delivered == ["six"] and frames_replayed >= 5
              and read_cursor(cursor)["last_seq"] == 6,
              "replayed=%d frames_resent=%d delivered=%s" %
              (replayed, frames_replayed, delivered))

        # ---------------------------------------------------------------- #
        # 05 — corrupt cursor: warn + baseline, no replay
        # ---------------------------------------------------------------- #
        log5 = tmp / "client-05.log"
        cursor.write_text("{bad json", encoding="utf-8")
        base = len(bridge.records())
        client = Client(cursor, log5, rules)
        client.wait_log("corrupt json")
        client.wait_log("baselined at 6 (no replay)")
        time.sleep(1.2)
        replayed = len(bridge.records()) - base
        bridge.emit("seven")
        wait_until(lambda: len(bridge.records()) == base + 1, 10,
                   what="event 7 delivered")
        time.sleep(0.3)
        client.stop()
        delivered = texts(bridge.records())[base:]
        check("05 corrupt cursor: baselined, no replay",
              replayed == 0 and delivered == ["seven"]
              and read_cursor(cursor)["last_seq"] == 7,
              "replayed=%d delivered=%s" % (replayed, delivered))

        # ---------------------------------------------------------------- #
        # 06 — bridge seq goes backwards: new game session, no replay
        # ---------------------------------------------------------------- #
        log6 = tmp / "client-06.log"
        bridge.reset()                      # game restarted, seq back to 0
        bridge.emit("stale-before-restart")  # seq=1, remains in the buffer
        base = len(bridge.records())
        client = Client(cursor, log6, rules)
        client.wait_log("new bridge session detected (seq went backwards 7 -> 1)")
        client.wait_log("re-baselined at 1, no replay")
        time.sleep(1.0)
        replayed = len(bridge.records()) - base
        bridge.emit("after-restart")         # seq=2
        wait_until(lambda: len(bridge.records()) == base + 1, 10,
                   what="new-session event delivered")
        time.sleep(0.3)
        client.stop()
        delivered = texts(bridge.records())[base:]
        check("06 new bridge session: re-baselined, only new session processed",
              replayed == 0 and delivered == ["after-restart"]
              and read_cursor(cursor)["last_seq"] == 2,
              "replayed=%d delivered=%s last_seq=%s" %
              (replayed, delivered, read_cursor(cursor)["last_seq"]))

        # ---------------------------------------------------------------- #
        # 07 — --lookback 5: accept by ts just behind the cursor, reject old
        # ---------------------------------------------------------------- #
        log7 = tmp / "client-07.log"
        bridge.reset()
        bridge.config(ignore_since=True)
        now = time.time()
        bridge.emit("lookback-in", ts=now - 2)    # within 5s of saved_at
        bridge.emit("lookback-out", ts=now - 30)  # far older, must be skipped
        cursor.write_text(json.dumps({
            "source": "mc_chat", "last_seq": 2, "saved_at": now_iso(),
            "bridge": bridge.base}), encoding="utf-8")
        base = len(bridge.records())
        client = Client(cursor, log7, rules, ["--lookback", "5"])
        client.wait_log("lookback=5.0s")
        time.sleep(1.5)
        delivered = texts(bridge.records())[base:]
        client.stop()
        check("07 --lookback 5 accepts boundary event, rejects old one",
              set(delivered) == {"lookback-in"},
              "delivered=%s (duplicates are the server's job since 0.6.0)"
              % (delivered,))

        # ---------------------------------------------------------------- #
        # 08 — --no-cursor: old behaviour, no file written
        # ---------------------------------------------------------------- #
        log8 = tmp / "client-08.log"
        if cursor.exists():
            cursor.unlink()
        bridge.config(ignore_since=False)
        base = len(bridge.records())
        client = Client(cursor, log8, rules, ["--no-cursor"])
        client.wait_log("cursor disabled")
        client.wait_log("connected to")
        bridge.emit("legacy-mode")
        wait_until(lambda: "legacy-mode" in texts(bridge.records())[base:], 10,
                   what="legacy delivery")
        client.stop()
        check("08 --no-cursor writes no cursor file",
              not cursor.exists(),
              "cursor_exists=%s" % cursor.exists())
    finally:
        bridge.stop()

    passed = sum(1 for r in RESULTS if r["ok"])
    for result in RESULTS:
        print("%s  %-58s %s" % ("PASS" if result["ok"] else "FAIL",
                                result["name"], result["detail"]))
    print("-" * 100)
    print("%d/%d PASS, %d FAIL   (logs: %s)" %
          (passed, len(RESULTS), len(RESULTS) - passed, tmp))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
