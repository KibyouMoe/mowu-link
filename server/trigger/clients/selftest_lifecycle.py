#!/usr/bin/env python3
"""selftest_lifecycle.py — subscription lifecycle tests for either bus.

    python3 selftest_lifecycle.py --mode local     # in-process bus on 127.0.0.1:8792
    python3 selftest_lifecycle.py --mode server    # spawns server.py on 127.0.0.1:8795
                                                   # with a throwaway DB

Identical semantics on both buses:

  a) slow reader  — subscribe and do not read, publish 300 events: the publisher
                    never blocks, the 128-slot queue overflows, drops are only
                    logged (no `gap`/`dropped` frame) and the stream shows a
                    discontinuity in `seq`;
  b) EOF cleanup  — subscribe, hang up, keep publishing: the subscriber is gone
                    from /bus/health within ~1 s and the log records why;
  c) resume       — reconnect with since=<last seq>: no duplicates, no loss,
                    and no gap frame for the missed window;
  d) quota        — max_events=3 with 5 events: 3 delivered, `event: exhausted`,
                    close, then re-subscribe with since=N for the rest.

There is no lease/renew test: liveness is the SSE connection itself.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import ProxyHandler, Request, build_opener

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OPENER = build_opener(ProxyHandler({}))
HOST = "127.0.0.1"
LOCAL_PORT = 8792
SERVER_PORT = 8795

RESULTS: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print("%s  %-50s %s" % ("PASS" if ok else "FAIL", name, detail))


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


# --------------------------------------------------------------------------- #
# SSE reader over a raw socket (can start reading late = slow consumer)
# --------------------------------------------------------------------------- #

class SSE:
    def __init__(self, port: int, name: str, since: int = 0,
                 max_events: Optional[int] = None, token: str = "",
                 autoread: bool = True) -> None:
        query = ["name=%s" % name, "since=%d" % since]
        if max_events is not None:
            query.append("max_events=%d" % max_events)
        lines = ["GET /subscribe?%s HTTP/1.0" % "&".join(query),
                 "Host: %s:%d" % (HOST, port),
                 "Accept: text/event-stream"]
        if token:
            lines.append("X-Trigger-Token: %s" % token)
        self.sock = socket.create_connection((HOST, port), timeout=60)
        self.sock.settimeout(60)
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        self.events: List[Dict[str, Any]] = []
        self.controls: List[Dict[str, Any]] = []
        self.closed_at: Optional[float] = None
        self.error = ""
        self._buf = b""
        self._thread: Optional[threading.Thread] = None
        if autoread:
            self.start_reading()

    def start_reading(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        frame_event = None
        data_lines: List[str] = []
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._buf += chunk
                while b"\n" in self._buf:
                    raw, self._buf = self._buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    if line == "":
                        if data_lines:
                            payload = json.loads("\n".join(data_lines))
                            if frame_event in ("gap", "expired", "exhausted"):
                                payload["_event"] = frame_event
                                self.controls.append(payload)
                            else:
                                self.events.append(payload)
                        frame_event, data_lines = None, []
                    elif line.startswith(":"):
                        continue
                    elif line.startswith("event:"):
                        frame_event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
        except (socket.timeout, OSError) as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            self.closed_at = time.time()

    def wait_for(self, predicate, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# two backends
# --------------------------------------------------------------------------- #

class LocalBackend:
    mode = "local"

    def __init__(self) -> None:
        sys.path.insert(0, str(HERE))
        from localbus import LocalBus
        self.capture = _Capture()
        logger = logging.getLogger("localbus")
        logger.addHandler(self.capture)
        logger.setLevel(logging.INFO)
        self.bus = LocalBus("selftest", port=LOCAL_PORT)
        self.port = self.bus.start()
        self.token = ""
        self._n = 0

    def publish(self, text: str, payload: Optional[dict] = None) -> float:
        started = time.time()
        self._n += 1
        self.bus.publish({"source": "selftest", "kind": "test", "priority": 2,
                          "actor": "tester", "text": text,
                          "payload": payload or {},
                          "event_id": "life-%d" % self._n})
        return (time.time() - started) * 1000

    def last_seq(self) -> int:
        return self.bus.last_seq

    def get_json(self, path: str) -> Any:
        with OPENER.open("http://%s:%d%s" % (HOST, self.port, path),
                         timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def log_text(self) -> str:
        return "\n".join(self.capture.lines)

    def stop(self) -> None:
        self.bus.stop()


class ServerBackend:
    mode = "server"

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="lifecycle-"))
        self.log_path = self.tmp / "life.log"
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"),
             "--port", str(SERVER_PORT),
             "--db", str(self.tmp / "life.db"),
             "--log", str(self.log_path)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        self.port = SERVER_PORT
        self.token = (Path.home() / ".hermes" / "secrets" /
                      "trigger.token").read_text(encoding="utf-8").strip()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                self.get_json("/health")
                break
            except Exception:
                time.sleep(0.2)
        self._n = 0

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request("http://%s:%d%s" % (HOST, self.port, path), data=data,
                          method="POST",
                          headers={"Content-Type": "application/json",
                                   "X-Trigger-Token": self.token})
        with OPENER.open(request, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def publish(self, text: str, payload: Optional[dict] = None) -> float:
        started = time.time()
        self._n += 1
        self._post("/trigger", {"source": "selftest", "kind": "test",
                                "priority": 2, "actor": "tester", "text": text,
                                "payload": payload or {},
                                "event_id": "life-%d" % self._n})
        return (time.time() - started) * 1000

    def last_seq(self) -> int:
        return int(self.get_json("/bus/health")["last_id"])

    def get_json(self, path: str) -> Any:
        headers = {"X-Trigger-Token": self.token} if self.token else {}
        request = Request("http://%s:%d%s" % (HOST, self.port, path),
                          headers=headers)
        with OPENER.open(request, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #

def find_sub(backend, name: str) -> Optional[dict]:
    for entry in backend.get_json("/bus/health")["subscribers"]:
        if entry["name"] == name:
            return entry
    return None


def test_slow_consumer(backend) -> None:
    print("\n--- a) slow reader: subscribe, never read, publish 300 ---")
    base = backend.last_seq()
    stream = SSE(backend.port, "slow", since=base, token=backend.token,
                 autoread=False)
    time.sleep(0.4)  # registered, but nothing is drained from the socket
    print("health while stalled:", json.dumps(find_sub(backend, "slow")))

    payload = {"blob": "x" * 32768}  # big enough to fill the socket buffer
    durations = [backend.publish("slow message %03d" % i, payload)
                 for i in range(300)]
    total = sum(durations) / 1000.0
    print("publisher: 300 events in %.2fs (avg %.2f ms, max %.2f ms)"
          % (total, sum(durations) / len(durations), max(durations)))
    check("publisher not blocked by a stalled subscriber",
          total < 15.0 and max(durations) < 2000,
          "300 events in %.2fs, max %.1f ms" % (total, max(durations)))

    stream.start_reading()
    stream.wait_for(lambda: len(stream.events) > 0, 30)
    health = find_sub(backend, "slow")
    print("subscriber: %d data frames, controls=%s, health=%s"
          % (len(stream.events), [c.get("_event") for c in stream.controls],
             json.dumps(health)))
    check("slow reader's queue overflowed (dropped_total > 0)",
          bool(health) and health.get("dropped_total", 0) > 0,
          "dropped_total=%s" % (health or {}).get("dropped_total"))
    seqs = [e.get("seq") for e in stream.events if e.get("seq") is not None]
    holes = [(a, b) for a, b in zip(seqs, seqs[1:]) if b - a > 1]
    print("seq holes:", holes[:5], "(%d total)" % len(holes))
    check("loss is visible as a seq discontinuity", bool(holes),
          str(holes[:3]) if holes else "no discontinuity")
    check("no loss-notification frame is sent",
          not any(c.get("_event") in ("gap", "dropped")
                  for c in stream.controls),
          str([c.get("_event") for c in stream.controls]))
    check("subscriber buffer stayed bounded",
          len(stream.events) <= 200,
          "%d data frames out of 300" % len(stream.events))
    stream.close()
    time.sleep(0.3)


def test_eof_cleanup(backend) -> None:
    print("\n--- b) EOF: subscribe, kill the client, keep publishing ---")
    base = backend.last_seq()
    query = "name=eof-test&since=%d" % base
    lines = ["GET /subscribe?%s HTTP/1.0" % query,
             "Host: %s:%d" % (HOST, backend.port)]
    if backend.token:
        lines.append("X-Trigger-Token: %s" % backend.token)
    sock = socket.create_connection((HOST, backend.port), timeout=5)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
    time.sleep(0.3)
    check("subscriber visible before the hang-up",
          find_sub(backend, "eof-test") is not None,
          json.dumps(find_sub(backend, "eof-test")))
    sock.close()  # FIN/RST — the server must notice without any write
    for i in range(6):
        backend.publish("eof message %d" % i)
        time.sleep(0.1)
    gone = False
    deadline = time.time() + 5
    while time.time() < deadline:
        if find_sub(backend, "eof-test") is None:
            gone = True
            break
        time.sleep(0.1)
    log_text = backend.log_text()
    reason_ok = ("reason=client_eof" in log_text
                 or "reason=write_failures" in log_text)
    detail = [ln for ln in log_text.splitlines() if "eof-test" in ln][-1:]
    check("subscriber cleaned up after EOF/RST", gone, "gone=%s" % gone)
    check("cleanup logged with a kill reason", reason_ok,
          detail[0] if detail else "no log line")


def test_resume(backend) -> None:
    print("\n--- c) resume: reconnect with since=last seq ---")
    base = backend.last_seq()
    first = SSE(backend.port, "resume", since=base, token=backend.token)
    backend.publish("resume message 1")
    backend.publish("resume message 2")
    first.wait_for(lambda: len(first.events) >= 2, 10)
    got = [e["seq"] for e in first.events]
    last_seen = got[-1] if got else base
    print("first connection received:", got)
    first.close()
    time.sleep(0.4)

    backend.publish("resume message 3")
    backend.publish("resume message 4")
    second = SSE(backend.port, "resume", since=last_seen, token=backend.token)
    second.wait_for(lambda: len(second.events) >= 2, 10)
    seqs = [e["seq"] for e in second.events]
    ids = [e["event_id"] for e in second.events]
    print("resumed connection received:", seqs, ids)
    check("resume delivers exactly the missed events",
          seqs == [last_seen + 1, last_seen + 2], str(seqs))
    check("resume has no duplicates", len(set(seqs)) == len(seqs), str(seqs))
    check("resume emits no gap frame",
          not any(c.get("_event") == "gap" for c in second.controls),
          str([c.get("_event") for c in second.controls]))
    second.close()


def test_quota(backend) -> None:
    print("\n--- d) quota: max_events=3 with 5 events, then resume ---")
    base = backend.last_seq()
    stream = SSE(backend.port, "quota", since=base, max_events=3,
                 token=backend.token)
    time.sleep(0.3)
    for i in range(5):
        backend.publish("quota message %d" % i)
    stream.wait_for(lambda: stream.closed_at is not None, 15)
    exhausted = next((c for c in stream.controls
                      if c.get("_event") == "exhausted"), None)
    print("delivered=%d exhausted=%s closed=%s"
          % (len(stream.events), json.dumps(exhausted), stream.closed_at))
    check("exactly 3 events delivered before the quota", len(stream.events) == 3,
          "%d" % len(stream.events))
    check("event: exhausted carries last_seq",
          bool(exhausted) and exhausted.get("last_seq") == base + 3,
          json.dumps(exhausted))
    check("stream closed after exhausted", stream.closed_at is not None, "")
    stream.close()
    time.sleep(0.5)

    resumed = SSE(backend.port, "quota", since=base + 3, max_events=3,
                  token=backend.token)
    resumed.wait_for(lambda: len(resumed.events) >= 2, 10)
    seqs = [e.get("seq") for e in resumed.events]
    print("resumed events:", seqs)
    check("re-subscribe with since=N delivers the rest",
          seqs == [base + 4, base + 5], str(seqs))
    resumed.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("local", "server"), required=True)
    args = parser.parse_args()

    backend = LocalBackend() if args.mode == "local" else ServerBackend()
    print("backend=%s port=%d last_seq=%d" %
          (backend.mode, backend.port, backend.last_seq()))
    try:
        test_slow_consumer(backend)
        test_eof_cleanup(backend)
        test_resume(backend)
        test_quota(backend)
    finally:
        backend.stop()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "-" * 78)
    print("%d/%d PASS" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
