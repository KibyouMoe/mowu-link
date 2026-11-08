#!/usr/bin/env python3
"""selftest_bus.py — end-to-end check of a client's local bus.

Starts the fake SSE source, starts ``mc_chat_client.py`` against it, then
subscribes twice to the client's local bus:

  * ``worker-sse``  — GET /subscribe (SSE, live push)
  * ``worker-poll`` — GET /events?since=0 (plain JSON)

and finally a late joiner ``worker-late`` with ``since=2`` to prove the
per-subscriber cursor works.

Asserts: same count, same event_ids, same seqs; late joiner starts at seq 3;
/bus/health lists the subscribers.

    python3 selftest_bus.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

HERE = Path(__file__).resolve().parent
OPENER = build_opener(ProxyHandler({}))
FAKE_PORT = 8799
BUS_PORT = 8792
EXPECTED = 4  # 5 frames minus 1 deduped duplicate
BASE = "http://127.0.0.1:%d" % BUS_PORT


def get_json(url: str, timeout: float = 5.0):
    with OPENER.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_for(url: str, seconds: float = 20.0):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            return get_json(url)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.2)
    raise RuntimeError("timed out waiting for %s (%s)" % (url, last))


def sse_reader(url: str, sink: list, errors: list, limit: int,
               gaps: list) -> None:
    try:
        request = Request(url, headers={"Accept": "text/event-stream"})
        with OPENER.open(request, timeout=45) as resp:
            current_event = None
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    payload = json.loads(line[5:].strip())
                    if current_event == "gap":
                        gaps.append(payload)
                    else:
                        sink.append(payload)
                        if len(sink) >= limit:
                            return
                elif line == "":
                    current_event = None
    except Exception as exc:  # noqa: BLE001
        errors.append("%s: %s" % (type(exc).__name__, exc))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bus-selftest-"))
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    fake = subprocess.Popen(
        [sys.executable, "fake_sse_server.py", "--port", str(FAKE_PORT),
         "--delay", "1.0"],
        cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(0.6)
    client = subprocess.Popen(
        [sys.executable, "mc_chat_client.py",
         "--url", "http://127.0.0.1:%d/stream" % FAKE_PORT,
         "--once-seconds", "22",
         "--listen-port", str(BUS_PORT),
         "--log", str(tmp / "mc.log"),
         "--drops-log", str(tmp / "drops.log"),
         "--verbose"],
        cwd=str(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    try:
        health0 = wait_for(BASE + "/bus/health")
        print("bus health up:", json.dumps(health0, ensure_ascii=False))

        sse_events: list = []
        sse_errors: list = []
        sse_gaps: list = []
        reader = threading.Thread(
            target=sse_reader,
            args=(BASE + "/subscribe?name=worker-sse&since=0", sse_events,
                  sse_errors, EXPECTED, sse_gaps))
        reader.start()
        reader.join(40)

        poll_events = get_json(BASE + "/events?since=0&limit=50")
        late_events = get_json(BASE + "/events?since=2&limit=50")
        health = get_json(BASE + "/bus/health")
    finally:
        for proc in (client, fake):
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("\n--- worker-sse (GET /subscribe) ---")
    for event in sse_events:
        print("seq=%s event_id=%s kind=%s prio=%s %r" % (
            event["seq"], event["event_id"], event["kind"],
            event["priority"], event["text"]))
    print("errors:", sse_errors or "none", " gaps:", sse_gaps or "none")

    print("\n--- worker-poll (GET /events?since=0) ---")
    for event in poll_events:
        print("seq=%s event_id=%s kind=%s prio=%s %r" % (
            event["seq"], event["event_id"], event["kind"],
            event["priority"], event["text"]))

    print("\n--- worker-late (GET /events?since=2) ---")
    for event in late_events:
        print("seq=%s event_id=%s %r" % (event["seq"], event["event_id"],
                                        event["text"]))

    print("\n--- /bus/health ---")
    print(json.dumps(health, ensure_ascii=False, indent=2))

    check("SSE subscriber received %d events" % EXPECTED,
          len(sse_events) == EXPECTED, "got %d" % len(sse_events))
    check("poll subscriber received %d events" % EXPECTED,
          len(poll_events) == EXPECTED, "got %d" % len(poll_events))
    sse_ids = [e["event_id"] for e in sse_events]
    poll_ids = [e["event_id"] for e in poll_events]
    check("event_id sets identical", sse_ids == poll_ids,
          "sse=%s poll=%s" % (sse_ids, poll_ids))
    check("seqs are 1..N on both sides",
          [e["seq"] for e in sse_events] == list(range(1, EXPECTED + 1))
          and [e["seq"] for e in poll_events] == list(range(1, EXPECTED + 1)),
          "sse=%s poll=%s" % ([e["seq"] for e in sse_events],
                              [e["seq"] for e in poll_events]))
    check("late subscriber since=2 starts at seq 3",
          [e["seq"] for e in late_events] == list(range(3, EXPECTED + 1)),
          "got %s" % [e["seq"] for e in late_events])
    check("envelope keys match the /trigger envelope",
          all(set(e) >= {"seq", "source", "kind", "priority", "actor", "text",
                         "payload", "event_id"} for e in poll_events),
          "")
    check("/bus/health reports buffer + last_seq",
          health.get("last_seq") == EXPECTED
          and health.get("buffered") == EXPECTED,
          json.dumps(health, ensure_ascii=False))

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "-" * 70)
    for name, ok, detail in results:
        print("%s  %-46s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("%d/%d PASS" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
