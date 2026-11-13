#!/usr/bin/env python3
"""selftest_router.py — real end-to-end test for mowu-router 0.3.0.

Starts two real processes, no in-process shortcuts:

  * server/trigger/server.py on a free port with a throwaway DB, so the
    notify path is verified against the real trigger service (not a mock and
    not the production ~/.hermes/trigger.db);
  * server/router/server.py on 127.0.0.1:8790 (override with
    ROUTER_TEST_PORT) with a throwaway DB/token/lock/log.

Then drives a fake worker over a real SSE connection and exercises:

  register → online → immediate dispatch → result → done → trigger notify
  offline 409 → queue_if_offline 202 → reconnect → ordered backlog flush
  busy → queued → first result → second auto-dispatched
  cancel queued → cancelled (never delivered)
  cancel running → cancelled, late result ignored
  backlog cap 128 → 129th refused with backlog_full
  POST /notice normal/urgent → trigger notify/event, minute-hash idempotence
  subscriptions → trigger registration → pending pull → ACK → delete closes
  chat: POST /chat/log → GET messages (dedupe/filters) → summary counts
  peer chat: authenticated /messages, exact retries/conflicts, recipient ACK,
    bounded inbox/SSE replay, image references, offline queue and process restart
  formal tasks: optional creation UUID, atomic persistence, exact retries across
    reconnect/restart, goal-only execution and explicit deadlines, legacy clients
  SSE disconnect → subscriptions purged locally and at trigger
  trigger down → POST /subscriptions 502 + rollback, /notice ok:false (no 5xx)
  single-instance flock, token file generation/permissions, auth

Standard library only, Python 3.9.  Prints one PASS/FAIL line per check and
a total; exits non-zero on any failure.
"""

from __future__ import annotations

import http.client as http_client
import base64
import hashlib
import json
import os
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PY = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
ROUTER = HERE / "server.py"
TRIGGER = REPO / "server" / "trigger" / "server.py"
PORT = int(os.environ.get("ROUTER_TEST_PORT", "8790"))
BASE = "http://127.0.0.1:%d" % PORT


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print("PASS %02d %s" % (self.passed + self.failed, label), flush=True)
        else:
            self.failed += 1
            self.failures.append(label)
            print("FAIL %02d %s%s" % (self.passed + self.failed, label,
                                      ("  [" + detail + "]") if detail else ""),
                  flush=True)
        return bool(ok)

    @property
    def total(self) -> int:
        return self.passed + self.failed


def http(method: str, url: str, body=None, token=None,
         timeout: float = 5.0):
    """Small JSON client; returns (status, payload). Never raises on 4xx/5xx."""
    data = None
    if body is not None:
        # Escaped Unicode also lets validation tests send lone surrogates as
        # legal JSON bytes; the server must reject those invalid text values.
        data = json.dumps(body, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("X-Router-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return int(resp.status), (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
        return int(exc.code), payload
    except urllib.error.URLError as exc:
        return 0, {"error": "unreachable", "detail": str(exc)}


def trigger_http(method: str, url: str, body=None, token=None,
                 timeout: float = 5.0):
    """JSON client for trigger (X-Trigger-Token instead of X-Router-Token)."""
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("X-Trigger-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return int(resp.status), (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw}
        return int(exc.code), payload
    except urllib.error.URLError as exc:
        return 0, {"error": "unreachable", "detail": str(exc)}


def notice_event_id(worker: str, text: str,
                    minute: Optional[int] = None) -> str:
    if minute is None:
        minute = int(time.time() // 60)
    digest = hashlib.sha256(
        ("%s\n%s\n%d" % (worker, text, minute)).encode("utf-8")).hexdigest()[:24]
    return "notice:" + digest


def wait_http(url: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http("GET", url, timeout=0.5)
        if status == 200:
            return True
        time.sleep(0.1)
    return False


def wait_online(name: str, token: str, want: bool,
                timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = http("GET", BASE + "/workers", token=token)
        if status == 200:
            for worker in payload.get("workers", []):
                if worker["name"] == name and worker["online"] is want:
                    return True
        time.sleep(0.1)
    return False


def worker_row(name: str, token: str):
    status, payload = http("GET", BASE + "/workers", token=token)
    if status != 200:
        return None
    for worker in payload.get("workers", []):
        if worker["name"] == name:
            return worker
    return None


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class FakeWorker(threading.Thread):
    """A real SSE client: one HTTP GET streaming until stop()."""

    def __init__(self, name: str, token: str, version: str = "fake-1",
                 port: int = PORT) -> None:
        super().__init__(daemon=True, name="fake-worker-" + name)
        self.worker_name = name
        self.token = token
        self.version = version
        self.port = port
        self.tasks: "queue.Queue[dict]" = queue.Queue()
        self.messages: "queue.Queue[dict]" = queue.Queue()
        self.ping_times = []
        self.connected = threading.Event()
        self.error = None
        self._stop_event = threading.Event()
        self._conn = None
        self._resp = None

    def run(self) -> None:
        path = "/stream?name=%s&version=%s" % (
            quote(self.worker_name), quote(self.version))
        try:
            conn = http_client.HTTPConnection("127.0.0.1", self.port, timeout=5.0)
            conn.request("GET", path, headers={
                "X-Router-Token": self.token,
                "Accept": "text/event-stream",
            })
            resp = conn.getresponse()
            self._conn, self._resp = conn, resp
            if resp.status != 200:
                raw = resp.read(500).decode("utf-8", "replace")
                self.error = "HTTP %d: %s" % (resp.status, raw)
                return
            self.connected.set()
            if conn.sock is not None:
                conn.sock.settimeout(1.0)
            event = None
            data = []
            while not self._stop_event.is_set():
                try:
                    raw = resp.readline()
                except socket.timeout:
                    continue
                except (OSError, ValueError) as exc:
                    if not self._stop_event.is_set():
                        self.error = repr(exc)
                    break
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if event in ("task", "worker_message") and data:
                        try:
                            payload = json.loads("\n".join(data))
                        except ValueError:
                            payload = None
                        if isinstance(payload, dict):
                            target = self.tasks if event == "task" else self.messages
                            target.put(payload)
                    event = None
                    data = []
                elif line.startswith(":"):
                    if "ping" in line:
                        self.ping_times.append(time.time())
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
        except Exception as exc:  # noqa: BLE001 - reported by the test
            self.error = repr(exc)
        finally:
            self.connected.set()
            try:
                if self._resp is not None:
                    self._resp.close()
            except Exception:
                pass
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass

    def wait_task(self, timeout: float = 5.0):
        try:
            return self.tasks.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_message(self, timeout: float = 5.0):
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_ping(self, timeout: float = 4.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ping_times:
                return True
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=3.0)



def peer_selftest(checker, work, trigger_url, trigger_token):
    """A separate real router: crash/restart never touches the main test router."""
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    db = work / "peer.db"
    token_path = work / "peer.token"
    log_path = work / "peer.log"
    argv = [PY, str(ROUTER), "--port", str(port), "--db", str(db),
            "--token-file", str(token_path), "--lock", str(work / "peer.lock"),
            "--log", str(log_path), "--trigger-url", trigger_url + "/trigger",
            "--trigger-token-file", str(trigger_token), "--ping-interval", "0.2"]
    proc, output = start_process(argv, work / "peer.out")
    clients = []
    a, b, c = "peer-发送", "peer-接收", "peer-third"
    marker = "PEER_BODY_MUST_STAY_OUT_OF_LOGS"
    try:
        if not wait_http(base + "/health"):
            raise RuntimeError("peer router did not start: " + tail(log_path))
        token = token_path.read_text(encoding="utf-8").strip()

        def call(method, route, body=None):
            return http(method, base + route, body, token)

        def payload(**changes):
            body = {"message_id": str(uuid.uuid4()), "from": a, "to": b,
                    "text": marker}
            body.update(changes)
            return body

        def inbox():
            messages = []
            after = 0
            while True:
                status, page = call("GET", "/messages?worker=%s&after=%d" % (quote(b), after))
                if status != 200:
                    raise RuntimeError("peer inbox returned %d" % status)
                messages.extend(page["messages"])
                if not page["has_more"]:
                    return messages
                next_after = page["next_after"]
                if not next_after or next_after <= after:
                    raise RuntimeError("peer inbox cursor did not advance")
                after = next_after

        def online(want):
            deadline = time.time() + 5
            while time.time() < deadline:
                _, rows = call("GET", "/workers")
                if any(w["name"] == b and w["online"] is want for w in rows.get("workers", [])):
                    return True
                time.sleep(0.05)
            return False

        def connect():
            client = FakeWorker(b, token, port=port)
            clients.append(client)
            client.start()
            if not client.connected.wait(5) or not online(True) or client.error:
                raise RuntimeError("peer SSE did not connect")
            return client

        def receive(client, count):
            return [client.wait_message(3) for _ in range(count)]

        def ids(messages):
            return [m.get("message_id") if isinstance(m, dict) else None for m in messages]

        for name in (a, b, c):
            call("POST", "/register", {"name": name})
        first_body = payload()
        mid = first_body["message_id"]
        auth_routes = [("POST", "/messages", first_body),
                       ("GET", "/messages?worker=" + quote(b), None),
                       ("GET", "/messages/" + mid, None),
                       ("GET", "/messages?id=" + mid, None),
                       ("POST", "/messages/" + mid + "/ack", {"worker": b}),
                       ("GET", "/stream?name=" + quote(b), None)]
        checker.check("peer auth: create/inbox/id/ACK/SSE require the router token",
                      all(http(method, base + route, body, given)[0] == 401
                          for given in (None, "invalid-token")
                          for method, route, body in auth_routes))

        invalid = [{"message_id": "not-a-uuid"}, {"message_id": 123},
                   {"from": []}, {"to": ""}, {"text": " \n"}, {"text": 123},
                   {"text": "x" * 20001}, {"text": "\ud800"},
                   {"reply_to": "not-a-uuid"}, {"deadline_s": 30}]
        checker.check("peer validation: UUID, worker types, text/Unicode/20000 cap and no task fields",
                      all(call("POST", "/messages", payload(**edits))[0] == 400 for edits in invalid))
        checker.check("peer validation: sender and recipient must already be known workers",
                      all(call("POST", "/messages", payload(**edits))[0] == 404
                          for edits in ({"from": "unknown"}, {"to": "unknown"})))
        checker.check("peer validation: missing/bad inbox worker, limit and cursor are rejected",
                      all(call("GET", route)[0] == 400 for route in (
                          "/messages", "/messages?worker=", "/messages/not-a-uuid",
                          "/messages?worker=%s&limit=0" % quote(b),
                          "/messages?worker=%s&limit=101" % quote(b),
                          "/messages?worker=%s&after=-1" % quote(b),
                          "/messages?worker=%s&after=9223372036854775808" % quote(b))))
        checker.check("peer validation: unknown inbox/message/ACK/reply returns 404",
                      call("GET", "/messages?worker=unknown")[0] == 404
                      and call("GET", "/messages/" + mid)[0] == 404
                      and call("POST", "/messages/" + mid + "/ack", {"worker": b})[0] == 404
                      and call("POST", "/messages", payload(reply_to=mid))[0] == 404)

        code, first = call("POST", "/messages", first_body)
        first_message = first.get("message", {})
        checker.check("peer offline create: 202 queued with explicit from/to, no deadline",
                      code == 202 and first.get("status") == "queued"
                      and first.get("duplicate") is False
                      and first_message.get("from") == a and first_message.get("to") == b
                      and first_message.get("text") == marker
                      and first_message.get("acked_at") is None
                      and not ({"task_id", "deadline_s", "result"} & set(first_message)))
        code, duplicate = call("POST", "/messages", dict(reversed(list(first_body.items()))))
        checker.check("peer exact retry: key order ignored, one durable message and stable receipt",
                      code == 200 and duplicate.get("duplicate") is True
                      and duplicate.get("message") == first_message and len(inbox()) == 1)
        conflicts = [{"text": marker + " "}, {"from": c}, {"to": c},
                     {"reply_to": None}, {"attachments": []}]
        checker.check("peer reused UUID: changed text/from/to/optional fields -> 409, original unchanged",
                      all(call("POST", "/messages", dict(first_body, **edits))[0] == 409
                          for edits in conflicts)
                      and inbox() == [first_message])
        checker.check("peer ACK: sender or other worker cannot acknowledge the recipient's message",
                      all(call("POST", "/messages/" + mid + "/ack", {"worker": w})[0] == 403
                          for w in (a, c)) and inbox() == [first_message])

        code, reply = call("POST", "/messages", payload(**{
            "from": b, "to": a, "text": "explicit reply", "reply_to": mid}))
        checker.check("peer reply_to: reverse direction of the same pair is allowed",
                      code == 202 and reply.get("message", {}).get("reply_to") == mid)
        call("POST", "/messages/" + reply["message_id"] + "/ack", {"worker": a})
        checker.check("peer reply_to: third-party and self-conversation pairs are rejected",
                      all(call("POST", "/messages", payload(**edits))[0] == 400
                          for edits in ({"to": c, "reply_to": mid},
                                        {"to": a, "reply_to": mid})))

        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        sha = hashlib.sha256(image).hexdigest()
        req = urllib.request.Request(base + "/attachments", data=image, method="POST",
                                     headers={"X-Router-Token": token, "X-Attachment-SHA256": sha})
        with urllib.request.urlopen(req, timeout=5) as response:
            uploaded = json.loads(response.read())["attachment"]
        attached_body = payload(attachments=[{"id": sha, "name": "../../peer.png"}])
        code, attached = call("POST", "/messages", attached_body)
        attached_message = attached.get("message", {})
        image_meta = attached_message.get("attachments", [{}])[0]
        bare_code, bare = call("POST", "/messages", payload(attachments=[sha]))
        checker.check("peer attachments: uploaded ids resolve to trusted metadata, display name sanitized",
                      code == bare_code == 202 and image_meta == dict(uploaded, name="peer.png")
                      and image_meta.get("path") == "/attachments/" + sha
                      and bare.get("message", {}).get("attachments") == [uploaded])
        bad_attachments = [None, {}, ["0" * 64], ["../secret"], [{"image_path": "/tmp/no.png"}],
                           [{"id": sha, "path": "/etc/passwd"}], [{"id": sha, "size": 1}],
                           [{"id": sha, "name": 1}], [{"id": sha, "name": "\ud800"}],
                           [sha] * 11]
        checker.check("peer attachments: unknown ids, arbitrary paths, forged metadata and invalid lists rejected",
                      all(call("POST", "/messages", payload(attachments=value))[0] == 400
                          for value in bad_attachments))
        checker.check("peer attachment payload changes conflict instead of mutating stored metadata",
                      call("POST", "/messages", dict(attached_body, attachments=[sha]))[0] == 409)

        # Exceed both the HTTP byte budget and the stream's 32-event buffer.
        for _ in range(5):
            call("POST", "/messages", payload(text="\U0001f40e" * 20000))
        for index in range(40):
            call("POST", "/messages", payload(text="backlog-%02d" % index))
        all_pending = inbox()
        _, page = call("GET", "/messages?worker=" + quote(b))
        _, one = call("GET", "/messages?worker=%s&limit=1" % quote(b))
        checker.check("peer inbox: Unicode boundary accepted, bounded byte/count pages lose no queued rows",
                      len(all_pending) == 48 and len(json.dumps(page, ensure_ascii=False).encode()) <= 256 * 1024
                      and page.get("has_more") is True and one.get("count") == 1
                      and one.get("next_after") == first_message.get("seq")
                      and [m["seq"] for m in all_pending] == sorted(m["seq"] for m in all_pending))
        stream = connect()
        received = receive(stream, len(all_pending))
        checker.check("peer SSE attach: offline inbox replays in seq order beyond the prefetch buffer",
                      received == all_pending and not stream.error)
        checker.check("peer SSE delivery remains queued until explicit ACK; no automatic reply/task",
                      inbox() == all_pending and stream.wait_task(0.2) is None
                      and call("GET", "/tasks")[1].get("count") == 0)
        code, _ = call("POST", "/messages", first_body)
        checker.check("peer exact retry while connected emits no extra worker_message",
                      code == 200 and stream.wait_message(0.3) is None)
        live_body = payload(text="live peer message")
        code, live = call("POST", "/messages", live_body)
        checker.check("peer live create: worker_message carries full sender/recipient and stays queued",
                      code == 202 and stream.wait_message(3) == live.get("message")
                      and call("GET", "/messages/" + live["message_id"])[1].get("status") == "queued")

        code, accepted = call("POST", "/messages/" + mid + "/ack", {"worker": b})
        code2, again = call("POST", "/messages/" + mid + "/ack", {"worker": b})
        checker.check("peer recipient ACK: accepted is idempotent with an unchanged receipt timestamp",
                      code == code2 == 200 and accepted.get("status") == "accepted"
                      and accepted.get("already") is False and again.get("already") is True
                      and accepted.get("message") == again.get("message")
                      and accepted.get("message", {}).get("acked_at") is not None
                      and mid not in ids(inbox()))
        checker.check("peer accepted message: wrong worker still forbidden, sender can query by path or id",
                      call("POST", "/messages/" + mid + "/ack", {"worker": a})[0] == 403
                      and call("GET", "/messages/" + mid)[1].get("message") == accepted["message"]
                      and call("GET", "/messages?id=" + mid)[1].get("message") == accepted["message"])

        # Replace a live stream while new creates race with attach/replay.
        race_bodies = [payload(text="attach-race-%d" % n) for n in range(8)]
        gate = threading.Barrier(2)
        race_codes = []

        def race_create():
            gate.wait(timeout=5)
            for body in race_bodies:
                race_codes.append(call("POST", "/messages", body)[0])

        writer = threading.Thread(target=race_create, daemon=True)
        writer.start()
        gate.wait(timeout=5)
        replacement = connect()
        writer.join(5)
        stream.join(3)
        expected = inbox()
        replayed = receive(replacement, len(expected))
        checker.check("peer SSE replacement/create race: complete ordered replay, no duplicates or stale detach",
                      not writer.is_alive() and race_codes == [202] * 8
                      and replayed == expected and not stream.is_alive() and online(True)
                      and replacement.wait_message(0.2) is None and mid not in ids(replayed))
        replacement.stop()
        checker.check("peer disconnect: pending inbox persists independently of stream/subscription lifetime",
                      online(False) and inbox() == expected
                      and call("GET", "/subscriptions")[1].get("count") == 0)
        offline_body = payload(text="offline before process restart")
        code, offline = call("POST", "/messages", offline_body)
        before_restart = inbox()
        checker.check("peer offline enqueue is immediately durable in SQLite",
                      code == 202 and offline.get("status") == "queued"
                      and ids(before_restart)[-1] == offline_body["message_id"])
        with sqlite3.connect(str(db)) as saved:
            row = saved.execute("SELECT acked_at, attachments_json FROM peer_messages WHERE message_id=?",
                                (attached["message_id"],)).fetchone()
            checker.check("peer SQLite stores pending image metadata without source paths",
                          row is not None and row[0] is None and json.loads(row[1]) == [image_meta])

        # Kill only this throwaway process to prove committed rows survive a
        # process crash, not just reconnect or an orderly store close.
        proc.kill()
        proc.wait(timeout=5)
        output.close()
        proc, output = start_process(argv, work / "peer-restarted.out")
        if not wait_http(base + "/health"):
            raise RuntimeError("peer router restart failed: " + tail(log_path))
        checker.check("peer process restart: queued inbox and accepted rows survive with the same identities",
                      inbox() == before_restart
                      and call("GET", "/messages/" + mid)[1].get("message") == accepted["message"])
        code, retried = call("POST", "/messages", first_body)
        checker.check("peer idempotency survives restart and returns current accepted status",
                      code == 200 and retried.get("duplicate") is True
                      and retried.get("status") == "accepted"
                      and call("POST", "/messages", dict(first_body, text="changed after restart"))[0] == 409
                      and call("POST", "/messages", attached_body)[1].get("message") == attached_message)
        restarted = connect()
        checker.check("peer restart SSE replay: every unacked message once, accepted message excluded",
                      receive(restarted, len(before_restart)) == before_restart
                      and restarted.wait_message(0.2) is None)
        req = urllib.request.Request(base + image_meta["path"], headers={"X-Router-Token": token})
        with urllib.request.urlopen(req, timeout=5) as response:
            downloaded = response.read()
        checker.check("peer uploaded image bytes and trusted metadata survive restart",
                      downloaded == image
                      and call("GET", "/messages/" + attached["message_id"])[1].get("message") == attached_message)
        ack_codes = [call("POST", "/messages/" + m["message_id"] + "/ack", {"worker": b})[0]
                     for m in before_restart]
        empty_stream = connect()
        checker.check("peer ACK drains inbox durably; reconnect produces no messages or implicit task results",
                      all(code == 200 for code in ack_codes) and inbox() == []
                      and empty_stream.wait_message(0.3) is None and empty_stream.wait_task(0.2) is None
                      and call("GET", "/tasks")[1].get("count") == 0
                      and call("GET", "/subscriptions")[1].get("count") == 0)
        logs = log_path.read_text(encoding="utf-8")
        checker.check("peer logging: message bodies and authentication tokens never appear",
                      marker not in logs and token not in logs
                      and max(map(len, logs.splitlines()), default=0) < 2000)
    finally:
        for client in clients:
            client.stop()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        output.close()


def workorders_selftest(checker, work, trigger_url, trigger_token):
    """Use a legacy DB and a throwaway process; leave all other routers alone."""
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    db = work / "workorders.db"
    token_path = work / "workorders.token"
    log_path = work / "workorders.log"
    with sqlite3.connect(str(db)) as saved:
        saved.executescript("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, worker TEXT NOT NULL, goal TEXT NOT NULL,
                context TEXT, workspace TEXT, urgency TEXT DEFAULT 'normal',
                state TEXT DEFAULT 'queued', created_at REAL, started_at REAL,
                finished_at REAL, result_json TEXT
            );
            INSERT INTO tasks (task_id, worker, goal, state, created_at)
                VALUES ('t-legacy', 'workorders', 'legacy formal task', 'done', 1);
        """)
    argv = [PY, str(ROUTER), "--port", str(port), "--db", str(db),
            "--token-file", str(token_path), "--lock", str(work / "workorders.lock"),
            "--log", str(log_path), "--trigger-url", trigger_url + "/trigger",
            "--trigger-token-file", str(trigger_token), "--ping-interval", "0.2",
            "--backlog-max", "2"]
    proc, output = start_process(argv, work / "workorders.out")
    clients = []
    name = "workorders"
    try:
        if not wait_http(base + "/health"):
            raise RuntimeError("workorders router did not start: " + tail(log_path))
        token = token_path.read_text(encoding="utf-8").strip()

        def call(method, route, body=None):
            return http(method, base + route, body, token)

        def online(want):
            deadline = time.time() + 5
            while time.time() < deadline:
                _, rows = call("GET", "/workers")
                if any(w["name"] == name and w["online"] is want
                       for w in rows.get("workers", [])):
                    return True
                time.sleep(0.05)
            return False

        def connect():
            client = FakeWorker(name, token, port=port)
            clients.append(client)
            client.start()
            if not client.connected.wait(5) or not online(True) or client.error:
                raise RuntimeError("workorders SSE did not connect")
            return client

        def get_task(task_id):
            return call("GET", "/tasks/" + task_id)[1].get("task", {})

        def finish(task_id):
            return call("POST", "/result", {"name": name, "task_id": task_id,
                        "status": "done", "summary": "formal task completed"})

        def receipt(code, result, task_id):
            return (code == 200 and result.get("ok") is True
                    and result.get("task_id") == task_id
                    and result.get("duplicate") is True
                    and result.get("dispatched") is False
                    and result.get("execution_mode") == "goal")

        call("POST", "/register", {"name": name})
        call("POST", "/register", {"name": "other-workorders"})
        with sqlite3.connect(str(db)) as saved:
            columns = {row[1] for row in saved.execute("PRAGMA table_info(tasks)")}
            indexes = {row[1]: row[2] for row in saved.execute("PRAGMA index_list(tasks)")}
        legacy = get_task("t-legacy")
        checker.check("workorders migration: legacy task survives, nullable keys and unique index added",
                      {"deadline_s", "request_id", "request_fingerprint"} <= columns
                      and indexes.get("idx_tasks_request_id") == 1
                      and legacy.get("goal") == "legacy formal task"
                      and legacy.get("execution_mode") == "goal"
                      and legacy.get("deadline_s") is None
                      and legacy.get("deadline_at") is None)

        plain = {"worker": name, "goal": "legacy client", "queue_if_offline": True}
        checker.check("workorders reject chat and all non-goal execution modes (including null)",
                      all(call("POST", "/tasks", dict(plain, execution_mode=mode))[0] == 400
                          for mode in ("chat", "Goal", " goal ", "", None, False, 1, {}, [])))
        checker.check("workorders optional message_id must be a UUID when present",
                      all(call("POST", "/tasks", dict(plain, message_id=value))[0] == 400
                          for value in (None, "", "not-a-uuid", 123, {}, [], uuid.uuid4().hex))
                      and call("GET", "/tasks")[1].get("count") == 1)
        old1_code, old1 = call("POST", "/tasks", plain)
        old2_code, old2 = call("POST", "/tasks", plain)
        with sqlite3.connect(str(db)) as saved:
            old_keys = saved.execute(
                "SELECT request_id, request_fingerprint FROM tasks WHERE task_id IN (?, ?)",
                (old1.get("task_id"), old2.get("task_id"))).fetchall()
        checker.check("workorders old clients: identical requests without UUID remain separate tasks",
                      old1_code == old2_code == 202 and old1.get("task_id") != old2.get("task_id")
                      and old1.get("execution_mode") == old2.get("execution_mode") == "goal"
                      and old_keys == [(None, None), (None, None)])
        for task in (old1, old2):
            call("POST", "/tasks/" + task["task_id"] + "/cancel")

        refused_body = {"worker": name, "goal": "online only", "message_id": str(uuid.uuid4())}
        refused_code, refused = call("POST", "/tasks", refused_body)
        with sqlite3.connect(str(db)) as saved:
            reserved = saved.execute("SELECT COUNT(*) FROM tasks WHERE request_id=?",
                                     (refused_body["message_id"],)).fetchone()[0]
        checker.check("workorders rejected offline creation does not reserve its UUID",
                      refused_code == 409 and refused.get("error") == "worker_offline" and reserved == 0)

        body = {"worker": name, "goal": "definite project", "message_id": str(uuid.uuid4()),
                "context": {"project": "router", "criteria": ["persist", "dispatch once"]},
                "workspace": "/tmp/workorders", "queue_if_offline": True, "urgency": "normal"}
        gate = threading.Barrier(8)
        results = []

        def create():
            gate.wait(timeout=5)
            results.append(call("POST", "/tasks", body))

        writers = [threading.Thread(target=create, daemon=True) for _ in range(8)]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(6)
        task_ids = {result.get("task_id") for _, result in results}
        if not checker.check("workorders concurrent UUID retries: one create, seven duplicates, one task_id",
                             all(not writer.is_alive() for writer in writers)
                             and sorted(code for code, _ in results) == [200] * 7 + [202]
                             and len(task_ids) == 1 and None not in task_ids,
                             "results=%s" % results):
            raise RuntimeError("workorder concurrent creation failed")
        task_id = next(iter(task_ids))
        reordered = dict(reversed(list(body.items())))
        reordered["context"] = dict(reversed(list(body["context"].items())))
        checker.check("workorders JSON key order and UUID letter case do not change identity",
                      receipt(*call("POST", "/tasks", reordered), task_id)
                      and receipt(*call("POST", "/tasks", dict(body, message_id=body["message_id"].upper())), task_id))
        changes = ({"goal": "changed"}, {"worker": "other-workorders"}, {"context": {}},
                   {"workspace": "/elsewhere"}, {"urgency": "urgent"},
                   {"queue_if_offline": False}, {"execution_mode": "goal"},
                   {"execution_mode": "chat"}, {"deadline_s": 30}, {"extra": True})
        checker.check("workorders changed payload under the same UUID always conflicts (409)",
                      all(call("POST", "/tasks", dict(body, **edits))[0] == 409 for edits in changes)
                      and get_task(task_id).get("goal") == body["goal"])

        explicit_body = {"worker": name, "goal": "explicit deadline", "message_id": str(uuid.uuid4()),
                         "execution_mode": "goal", "deadline_s": 7.5, "queue_if_offline": True}
        code, explicit = call("POST", "/tasks", explicit_body)
        explicit_id = explicit.get("task_id")
        overflow_code, overflow = call("POST", "/tasks", plain)
        checker.check("workorders duplicate bypasses full backlog without reserving another slot",
                      code == 202 and explicit.get("execution_mode") == "goal"
                      and overflow_code == 429 and overflow.get("error") == "backlog_full"
                      and receipt(*call("POST", "/tasks", body), task_id)
                      and call("GET", "/tasks?state=queued")[1].get("count") == 2)
        with sqlite3.connect(str(db)) as saved:
            persisted = saved.execute(
                "SELECT task_id, request_id, request_fingerprint, deadline_s FROM tasks"
                " WHERE request_id IS NOT NULL ORDER BY rowid").fetchall()
        checker.check("workorders SQLite commits UUID and fingerprint with each task row",
                      len(persisted) == 2 and [row[0] for row in persisted] == [task_id, explicit_id]
                      and [row[1] for row in persisted] == [body["message_id"], explicit_body["message_id"]]
                      and all(isinstance(row[2], str) and len(row[2]) == 64 for row in persisted)
                      and persisted[0][3] is None and persisted[1][3] == 7.5)
        default_task, timed_task = get_task(task_id), get_task(explicit_id)
        _, listing = call("GET", "/tasks")
        checker.check("workorders no wall-time default; only an explicit deadline_s creates deadline_at",
                      default_task.get("deadline_s") is None and default_task.get("deadline_at") is None
                      and timed_task.get("deadline_s") == 7.5
                      and timed_task.get("deadline_at") == timed_task.get("created_at") + 7.5
                      and all(task.get("execution_mode") == "goal" for task in listing.get("tasks", [])))

        stream = connect()
        delivered = [stream.wait_task(3), stream.wait_task(3)]
        checker.check("workorders SSE emits formal goal mode and preserves explicit/absent deadlines",
                      [task and task.get("task_id") for task in delivered] == [task_id, explicit_id]
                      and all(task and task.get("execution_mode") == "goal" for task in delivered)
                      and delivered[0].get("deadline_s") is None and delivered[0].get("deadline_at") is None
                      and delivered[1].get("deadline_s") == 7.5)
        checker.check("workorders live retry returns original task_id without another SSE dispatch",
                      receipt(*call("POST", "/tasks", body), task_id) and stream.wait_task(0.3) is None)
        stream.stop()
        checker.check("workorders offline retry after disconnect retains the same queued task",
                      online(False) and receipt(*call("POST", "/tasks", body), task_id)
                      and get_task(task_id).get("state") == "queued")
        reconnected = connect()
        replayed = [reconnected.wait_task(3), reconnected.wait_task(3)]
        checker.check("workorders reconnect replays original IDs once; HTTP retries add no dispatch",
                      [task and task.get("task_id") for task in replayed] == [task_id, explicit_id]
                      and receipt(*call("POST", "/tasks", body), task_id)
                      and receipt(*call("POST", "/tasks", explicit_body), explicit_id)
                      and reconnected.wait_task(0.3) is None)
        finish_code, _ = finish(task_id)
        code, completed = call("POST", "/tasks", body)
        checker.check("workorders retry after completion returns the same terminal task",
                      finish_code == 200 and receipt(code, completed, task_id)
                      and completed.get("state") == "done" and reconnected.wait_task(0.2) is None)

        # Crash this test process while the second task is running. The task
        # row and creation identity must survive together, before reconnect.
        proc.kill()
        proc.wait(timeout=5)
        reconnected.stop()
        output.close()
        proc, output = start_process(argv, work / "workorders-restarted.out")
        if not wait_http(base + "/health"):
            raise RuntimeError("workorders router restart failed: " + tail(log_path))
        checker.check("workorders process restart preserves UUIDs, fingerprints and original task IDs",
                      receipt(*call("POST", "/tasks", body), task_id)
                      and receipt(*call("POST", "/tasks", explicit_body), explicit_id)
                      and get_task(task_id).get("state") == "done"
                      and get_task(explicit_id).get("state") == "queued"
                      and call("POST", "/tasks", dict(body, goal="changed after restart"))[0] == 409
                      and call("GET", "/tasks")[1].get("count") == 5)
        with sqlite3.connect(str(db)) as saved:
            after_restart = saved.execute(
                "SELECT task_id, request_id, request_fingerprint, deadline_s FROM tasks"
                " WHERE request_id IS NOT NULL ORDER BY rowid").fetchall()
        checker.check("workorders migration is repeatable and restart does not change persisted creation data",
                      after_restart == persisted and get_task("t-legacy") == legacy)
        restarted = connect()
        replay = restarted.wait_task(3)
        checker.check("workorders restart dispatches only the original unfinished task, never its completed peer",
                      replay is not None and replay.get("task_id") == explicit_id
                      and replay.get("execution_mode") == "goal"
                      and receipt(*call("POST", "/tasks", explicit_body), explicit_id)
                      and restarted.wait_task(0.3) is None)
        finish(explicit_id)
        code, accepted = call("POST", "/tasks", refused_body)
        accepted_task = restarted.wait_task(3)
        checker.check("workorders UUID from a refused request remains usable for immediate online dispatch",
                      code == 200 and accepted.get("dispatched") is True
                      and accepted_task is not None and accepted_task.get("task_id") == accepted.get("task_id")
                      and accepted_task.get("execution_mode") == "goal"
                      and accepted_task.get("deadline_s") is None and accepted_task.get("deadline_at") is None)
        restarted.stop()
        checker.check("workorders exact retry bypasses offline refusal even without queue_if_offline",
                      online(False) and receipt(*call("POST", "/tasks", refused_body), accepted["task_id"])
                      and get_task(accepted["task_id"]).get("state") == "queued")
    finally:
        for client in clients:
            client.stop()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        output.close()
        if checker.failed:
            print("\n--- workorders log (tail) ---\n" + tail(log_path), flush=True)


def control_selftest(checker, token, workers):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []
    response_status = [200]

    class ControlHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            calls.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(response_status[0])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": response_status[0] == 200,
                                         "task_id": body["task_id"], "cancelled": True}).encode())
        def log_message(self, *args):
            pass

    callback = ThreadingHTTPServer(("127.0.0.1", 0), ControlHandler)
    thread = threading.Thread(target=callback.serve_forever, daemon=True)
    thread.start()
    name = "control-test"
    worker = None
    try:
        origin = "http://127.0.0.1:%d" % callback.server_address[1]
        for bad in ("http://example.com:80", "http://127.0.0.1:80/path",
                    "http://user:password@127.0.0.1:80", "file:///tmp/x"):
            code, _ = http("POST", BASE + "/register",
                           {"name": name, "control_url": bad}, token)
            checker.check("control callback rejects unsafe origin " + bad, code == 400)
        code, _ = http("POST", BASE + "/register",
                       {"name": name, "control_url": origin}, token)
        worker = FakeWorker(name, token)
        workers.append(worker)
        worker.start()
        checker.check("control worker registers loopback callback and connects",
                      code == 200 and wait_online(name, token, True))
        _, active = http("POST", BASE + "/tasks", {"worker": name, "goal": "active"}, token)
        worker.wait_task(3)
        _, queued = http("POST", BASE + "/tasks", {"worker": name, "goal": "queued"}, token)
        _, cancelled_q = http("POST", BASE + "/tasks/%s/cancel" % queued["task_id"], None, token)
        checker.check("queued cancel never calls the worker",
                      not calls and cancelled_q.get("cancel_delivery", {}).get("skipped") == "queued_task")
        _, cancelled = http("POST", BASE + "/tasks/%s/cancel" % active["task_id"], None, token)
        checker.check("running cancel reaches exact worker task with authenticated POST",
                      cancelled.get("cancel_delivery", {}).get("ok") is True
                      and calls == [("/manager/tasks/%s/cancel" % active["task_id"],
                                     "Bearer " + token, {"task_id": active["task_id"]})])
        _, saved = http("GET", BASE + "/tasks/" + active["task_id"], token=token)
        checker.check("cancel forwarding receipt persists on the task",
                      saved["task"]["result"]["cancel_delivery"]["ok"] is True)
        _, failing = http("POST", BASE + "/tasks", {"worker": name, "goal": "fail"}, token)
        worker.wait_task(3)
        response_status[0] = 503
        _, failed = http("POST", BASE + "/tasks/%s/cancel" % failing["task_id"], None, token)
        checker.check("worker refusal is explicit rather than a silent cancel",
                      failed.get("cancel_delivery", {}).get("ok") is False)
        response_status[0] = 200
        _, retry = http("POST", BASE + "/tasks/%s/cancel" % failing["task_id"], None, token)
        checker.check("an unconfirmed cancel can be retried safely by task id",
                      retry.get("already") is True and retry.get("cancel_delivery", {}).get("ok") is True)

        # An actual running task plus a real router approval must expose public progress.
        _, visible = http("POST", BASE + "/tasks", {"worker": name, "goal": "approval progress"}, token)
        worker.wait_task(3)
        _, asked = http("POST", BASE + "/approvals", {
            "worker": name, "task_id": visible["task_id"], "kind": "approval",
            "title": "write outside workspace", "ttl_s": 60, "auto": False}, token)
        aid = asked["approval_id"]
        _, snapshot = http("GET", BASE + "/tasks/" + visible["task_id"], token=token)
        progress = snapshot["task"].get("progress") or {}
        checker.check("task progress exposes waiting approval id and readable reason",
                      progress.get("state") == "waiting_approval"
                      and progress.get("approval_id") == aid
                      and ("等待审批 " + aid) in progress.get("text", ""))
        _, listing = http("GET", BASE + "/tasks?worker=" + name, token=token)
        row = next(t for t in listing["tasks"] if t["task_id"] == visible["task_id"])
        checker.check("task listing exposes the same approval progress",
                      row.get("progress", {}).get("approval_id") == aid)
        http("POST", BASE + "/approvals/" + aid + "/decide",
             {"decision": "approved"}, token)
        http("POST", BASE + "/result", {"name": name, "task_id": visible["task_id"],
             "status": "done", "summary": "done"}, token)
        _, finished = http("GET", BASE + "/tasks/" + visible["task_id"], token=token)
        checker.check("finished task result retains approval id without a waiting state",
                      finished["task"]["result"].get("approval_id") == aid
                      and finished["task"]["progress"]["waiting_for_approval"] is False)
        _, aborting = http("POST", BASE + "/tasks", {"worker": name, "goal": "cancel approval"}, token)
        worker.wait_task(3)
        _, asked = http("POST", BASE + "/approvals", {
            "worker": name, "task_id": aborting["task_id"], "kind": "approval",
            "title": "cancel this approval", "ttl_s": 60, "auto": False}, token)
        http("POST", BASE + "/tasks/" + aborting["task_id"] + "/cancel", None, token)
        _, decision = http("GET", BASE + "/approvals/" + asked["approval_id"], token=token)
        checker.check("task cancellation closes its outstanding router approval",
                      decision["approval"]["status"] == "denied")
        _, late = http("POST", BASE + "/approvals", {
            "worker": name, "task_id": aborting["task_id"], "kind": "approval",
            "title": "late approval after cancellation"}, token)
        checker.check("late approval submissions for cancelled tasks fail closed",
                      late.get("status") == "denied" and late.get("notified") is False)


    finally:
        if worker is not None:
            worker.stop()
        callback.shutdown()
        callback.server_close()
        thread.join(2)


def start_process(argv, log_path: Path):
    handle = open(str(log_path), "wb")
    proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT)
    return proc, handle


def tail(path: Path, lines: int = 40) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except OSError:
        return "(no log)"


def run(work: Path, checker: Checker) -> None:
    router_db = work / "router.db"
    router_token = work / "router.token"
    router_lock = work / "router.lock"
    router_log = work / "router.log"
    trigger_db = work / "trigger.db"
    trigger_token = work / "trigger.token"
    trigger_log = work / "trigger.log"
    trigger_port = free_port()
    trigger_url = "http://127.0.0.1:%d" % trigger_port

    trigger_proc = None
    router_proc = None
    trigger_out = None
    router_out = None
    workers = []
    try:
        refused = subprocess.run([
            PY, str(ROUTER), "--host", "0.0.0.0",
            "--log", str(work / "bind-refused.log"),
        ], capture_output=True, text=True, timeout=10)
        checker.check("remote bind requires explicit opt-in",
                      refused.returncode == 2 and "--allow-remote" in refused.stderr)
        # ---------------------------------------------------------------- #
        # 0. processes (real trigger + real router)
        # ---------------------------------------------------------------- #
        trigger_proc, trigger_out = start_process([
            PY, str(TRIGGER),
            "--port", str(trigger_port),
            "--db", str(trigger_db),
            "--token-file", str(trigger_token),
            "--rules-file", str(work / "no-rules.json"),
            "--blocked-log", str(work / "trigger-blocked.jsonl"),
            "--log", str(trigger_log),
        ], work / "trigger.out")
        if not wait_http(trigger_url + "/health"):
            raise RuntimeError("trigger did not start: " + tail(trigger_log))

        router_proc, router_out = start_process([
            PY, str(ROUTER),
            "--host", "0.0.0.0", "--allow-remote",
            "--port", str(PORT),
            "--db", str(router_db),
            "--token-file", str(router_token),
            "--lock", str(router_lock),
            "--log", str(router_log),
            "--trigger-url", trigger_url + "/trigger",
            "--trigger-token-file", str(trigger_token),
            "--ping-interval", "1",
        ], work / "router.out")
        if not wait_http(BASE + "/health", timeout=10.0):
            raise RuntimeError("router did not start: " + tail(router_log))

        token = router_token.read_text(encoding="utf-8").strip()
        checker.check(
            "token file generated: 64 hex chars, mode 0600",
            len(token) == 64 and all(c in "0123456789abcdef" for c in token)
            and (os.stat(str(router_token)).st_mode & 0o777) == 0o600,
            "len=%d mode=%o" % (len(token),
                                os.stat(str(router_token)).st_mode & 0o777))

        status, health = http("GET", BASE + "/health")
        checker.check(
            "GET /health (no token): ok, version, workers, tasks",
            status == 200 and health.get("ok") is True
            and health.get("version") == "0.4.0"
            and health.get("capabilities", {}).get("workOrders") == "idempotent-goal-v1"
            and health.get("workers") == 0 and health.get("tasks") == 0,
            "status=%s body=%s" % (status, health))

        status, payload = http("GET", BASE + "/workers")
        checker.check("auth: GET /workers without token -> 401",
                      status == 401 and payload.get("error") == "unauthorized",
                      "status=%s" % status)

        # Single instance: a second process on the same lock must refuse.
        second = subprocess.run([
            PY, str(ROUTER),
            "--port", str(free_port()),
            "--db", str(work / "second.db"),
            "--token-file", str(router_token),
            "--lock", str(router_lock),
            "--log", str(work / "second.log"),
        ], capture_output=True, text=True, timeout=10)
        checker.check(
            "single instance: second process refused by flock",
            second.returncode == 1 and "another mowu-router" in second.stderr,
            "rc=%s stderr=%r" % (second.returncode, second.stderr[-200:]))

        # ---------------------------------------------------------------- #
        # 1. register + stream + immediate dispatch
        # ---------------------------------------------------------------- #
        status, reg = http("POST", BASE + "/register",
                           {"name": "波旁", "version": "fake-1"}, token)
        checker.check(
            "POST /register {name} -> ok, queued=0, new=true",
            status == 200 and reg.get("ok") is True
            and reg.get("queued") == 0 and reg.get("new") is True,
            "status=%s body=%s" % (status, reg))

        status, payload = http("POST", BASE + "/tasks",
                               {"worker": "不存在", "goal": "x"}, token)
        checker.check("POST /tasks unknown worker -> 400 unknown_worker",
                      status == 400 and payload.get("error") == "unknown_worker",
                      "status=%s body=%s" % (status, payload))

        worker = FakeWorker("波旁", token)
        workers.append(worker)
        worker.start()
        online = wait_online("波旁", token, True)
        checker.check("GET /stream: worker becomes online", online,
                      "last row=%s" % (worker_row("波旁", token),))

        pingged = worker.wait_ping(4.0)
        checker.check("SSE heartbeat comment line (: ping) received",
                      pingged, "pings=%d" % len(worker.ping_times))

        status, created = http("POST", BASE + "/tasks",
                               {"worker": "波旁", "goal": "打印 hello",
                                "context": "selftest", "urgency": "urgent",
                                "workspace": "/tmp", "deadline_s": 60}, token)
        task1 = created.get("task_id")
        checker.check(
            "online+idle: POST /tasks -> 200 dispatched:true",
            status == 200 and created.get("dispatched") is True
            and created.get("state") == "running",
            "status=%s body=%s" % (status, created))
        received = worker.wait_task(5.0)
        checker.check(
            "SSE delivered event:task with matching task_id/goal",
            received is not None and received.get("task_id") == task1
            and received.get("goal") == "打印 hello"
            and received.get("type") == "task",
            "received=%s" % (received,))

        progress_ts = round(time.time() - 30, 3)
        status, hb = http("POST", BASE + "/heartbeat",
                          {"name": "波旁", "state": "working",
                           "progress_at": progress_ts,
                           "task_id": task1}, token)
        row = worker_row("波旁", token)
        checker.check(
            "POST /heartbeat stores state, last_seen refreshed",
            status == 200 and hb.get("ok") is True
            and row is not None and row.get("state") == "working"
            and row.get("busy") is True,
            "status=%s body=%s row=%s" % (status, hb, row))
        checker.check(
            "GET /workers exposes last_progress_at from heartbeat.progress_at",
            row is not None
            and abs(float(row.get("last_progress_at") or 0) - progress_ts) < 0.01
            and row.get("last_progress_at_iso") is not None,
            "row=%s" % (row,))

        # ---------------------------------------------------------------- #
        # 2. result -> done -> trigger notify
        # ---------------------------------------------------------------- #
        status, result = http("POST", BASE + "/result", {
            "name": "波旁", "task_id": task1, "status": "done",
            "summary": "hello printed", "artifacts": ["/tmp/hello.txt"],
            "evidence": ["exit=0"],
        }, token)
        checker.check(
            "POST /result done -> 200, notified=true",
            status == 200 and result.get("state") == "done"
            and result.get("notified") is True,
            "status=%s body=%s" % (status, result))

        status, got = http("GET", BASE + "/tasks/" + task1, token=token)
        checker.check(
            "GET /tasks/{id} -> state done + summary",
            status == 200 and got.get("state") == "done"
            and (got.get("task") or {}).get("result", {}).get("summary")
            == "hello printed",
            "status=%s body=%s" % (status, got))

        status, listing = http(
            "GET", BASE + "/tasks?worker=%s&state=done" % quote("波旁"),
            token=token)
        checker.check(
            "GET /tasks?worker=&state= filters",
            status == 200 and any(t["task_id"] == task1
                                  for t in listing.get("tasks", [])),
            "status=%s count=%s" % (status, listing.get("count")))

        event_row = None
        deadline = time.time() + 5.0
        while time.time() < deadline:
            conn = sqlite3.connect(str(trigger_db))
            try:
                event_row = conn.execute(
                    "SELECT event_id, source, kind, level, actor, text,"
                    " payload_json FROM events WHERE event_id=?",
                    (task1 + ":done",)).fetchone()
            finally:
                conn.close()
            if event_row:
                break
            time.sleep(0.1)
        payload_ok = False
        if event_row:
            try:
                payload_ok = json.loads(event_row[6] or "{}") == {
                    "task_id": task1, "worker": "波旁", "status": "done"}
            except ValueError:
                payload_ok = False
        checker.check(
            "trigger received notify: source=router kind=task.result "
            "level=notify actor=波旁 event_id=<id>:done",
            event_row is not None and event_row[1] == "router"
            and event_row[2] == "task.result" and event_row[3] == "notify"
            and event_row[4] == "波旁" and "完成" in (event_row[5] or "")
            and payload_ok,
            "row=%s" % (event_row,))

        # ---------------------------------------------------------------- #
        # 3. offline -> 409 / 202 -> reconnect -> ordered flush
        # ---------------------------------------------------------------- #
        worker.stop()
        checker.check("worker disconnect -> offline, name kept",
                      wait_online("波旁", token, False)
                      and worker_row("波旁", token) is not None)

        status, refused = http("POST", BASE + "/tasks",
                               {"worker": "波旁", "goal": "offline-1"}, token)
        checker.check(
            "offline + queue_if_offline=false -> 409 worker_offline",
            status == 409 and refused.get("error") == "worker_offline",
            "status=%s body=%s" % (status, refused))

        status1, off1 = http("POST", BASE + "/tasks", {
            "worker": "波旁", "goal": "offline-1", "queue_if_offline": True,
        }, token)
        status2, off2 = http("POST", BASE + "/tasks", {
            "worker": "波旁", "goal": "offline-2", "queue_if_offline": True,
        }, token)
        checker.check(
            "offline + queue_if_offline=true -> 202 queued with queued_ahead",
            status1 == 202 and off1.get("queued") is True
            and off1.get("queued_ahead") == 0
            and status2 == 202 and off2.get("queued_ahead") == 1,
            "first=%s/%s second=%s/%s" % (status1, off1, status2, off2))

        status, pending = http("GET", BASE + "/tasks?worker=%s&state=queued"
                               % quote("波旁"), token=token)
        checker.check(
            "backlog is visible via GET /tasks?state=queued",
            status == 200 and pending.get("count") == 2,
            "status=%s body=%s" % (status, pending.get("count")))

        worker2 = FakeWorker("波旁", token)
        workers.append(worker2)
        worker2.start()
        checker.check("reconnect: worker online again",
                      wait_online("波旁", token, True))
        first = worker2.wait_task(5.0)
        second = worker2.wait_task(5.0)
        checker.check(
            "reconnect: backlog flushed in order (offline-1 then offline-2)",
            first is not None and second is not None
            and first.get("task_id") == off1.get("task_id")
            and second.get("task_id") == off2.get("task_id")
            and first.get("goal") == "offline-1"
            and second.get("goal") == "offline-2",
            "first=%s second=%s" % (first and first.get("goal"),
                                    second and second.get("goal")))

        status, _ = http("POST", BASE + "/result", {
            "name": "波旁", "task_id": off1["task_id"], "status": "done",
            "summary": "offline-1 done"}, token)
        status2, _ = http("POST", BASE + "/result", {
            "name": "波旁", "task_id": off2["task_id"], "status": "done",
            "summary": "offline-2 done"}, token)
        checker.check("backlog tasks report done", status == 200
                      and status2 == 200)

        # ---------------------------------------------------------------- #
        # 4. busy -> queued -> auto-dispatch after the first result
        # ---------------------------------------------------------------- #
        _, a = http("POST", BASE + "/tasks",
                    {"worker": "波旁", "goal": "busy-A"}, token)
        status_b, b = http("POST", BASE + "/tasks",
                           {"worker": "波旁", "goal": "busy-B"}, token)
        got_a = worker2.wait_task(3.0)
        noise_b = worker2.wait_task(1.2)
        checker.check(
            "busy: second task queued (202), not pushed while first runs",
            a.get("dispatched") is True and status_b == 202
            and b.get("queued") is True
            and b.get("queued_ahead") == 1
            and got_a is not None and got_a.get("task_id") == a.get("task_id")
            and noise_b is None,
            "A=%s B=%s got_a=%s early_b=%s"
            % (a.get("state"), b.get("state"),
               got_a and got_a.get("goal"), noise_b and noise_b.get("goal")))

        body_b = http("POST", BASE + "/result", {
            "name": "波旁", "task_id": a["task_id"], "status": "done",
            "summary": "A done"}, token)[1]
        dispatched_b = worker2.wait_task(5.0)
        checker.check(
            "busy: first result auto-dispatches the queued second task",
            body_b.get("dispatched_next") == b.get("task_id")
            and dispatched_b is not None
            and dispatched_b.get("task_id") == b.get("task_id"),
            "result=%s delivered=%s" % (body_b.get("dispatched_next"),
                                        dispatched_b and dispatched_b.get("goal")))

        # ---------------------------------------------------------------- #
        # 5. cancel a queued task: cancelled and never delivered
        # ---------------------------------------------------------------- #
        _, c = http("POST", BASE + "/tasks",
                    {"worker": "波旁", "goal": "cancel-C"}, token)
        status, cancelled = http(
            "POST", BASE + "/tasks/%s/cancel" % c["task_id"], None, token)
        stray = worker2.wait_task(0.8)
        status_get, got_c = http("GET", BASE + "/tasks/" + c["task_id"],
                                 token=token)
        checker.check(
            "cancel queued -> cancelled, never delivered",
            c.get("queued") is True and status == 200
            and cancelled.get("state") == "cancelled" and stray is None
            and status_get == 200 and got_c.get("state") == "cancelled",
            "cancel=%s stray=%s get=%s" % (status, stray and stray.get("goal"),
                                           got_c.get("state")))

        # Finish B so the worker is idle for the running-cancel case.
        http("POST", BASE + "/result", {
            "name": "波旁", "task_id": b["task_id"], "status": "done",
            "summary": "B done"}, token)
        time.sleep(0.2)

        # ---------------------------------------------------------------- #
        # 6. cancel a running task; a late result is ignored
        # ---------------------------------------------------------------- #
        _, d = http("POST", BASE + "/tasks",
                    {"worker": "波旁", "goal": "cancel-D"}, token)
        got_d = worker2.wait_task(3.0)
        status, cancel_d = http(
            "POST", BASE + "/tasks/%s/cancel" % d["task_id"], None, token)
        status_late, late = http("POST", BASE + "/result", {
            "name": "波旁", "task_id": d["task_id"], "status": "done",
            "summary": "late"}, token)
        row = worker_row("波旁", token)
        checker.check(
            "cancel running -> cancelled, busy freed, late result ignored",
            status == 200 and cancel_d.get("state") == "cancelled"
            and got_d is not None and got_d.get("task_id") == d["task_id"]
            and status_late == 200 and late.get("duplicate") is True
            and row is not None and row.get("busy") is False,
            "cancel=%s late=%s busy=%s" % (status, late, row and row.get("busy")))

        # ---------------------------------------------------------------- #
        # 7. backlog cap 128 for an offline worker
        # ---------------------------------------------------------------- #
        status, _ = http("POST", BASE + "/register", {"name": "积压工"}, token)
        accepted = 0
        last_ahead = None
        first_bad = None
        for index in range(128):
            code, body = http("POST", BASE + "/tasks", {
                "worker": "积压工", "goal": "backlog-%03d" % index,
                "queue_if_offline": True,
            }, token)
            if code == 202:
                accepted += 1
                last_ahead = body.get("queued_ahead")
            else:
                first_bad = (code, body)
                break
        code, overflow = http("POST", BASE + "/tasks", {
            "worker": "积压工", "goal": "backlog-overflow",
            "queue_if_offline": True,
        }, token)
        row = worker_row("积压工", token)
        checker.check(
            "backlog cap: 128 accepted, 129th -> 429 backlog_full",
            status == 200 and accepted == 128 and last_ahead == 127
            and first_bad is None and code == 429
            and overflow.get("error") == "backlog_full"
            and overflow.get("limit") == 128,
            "register=%s accepted=%d last_ahead=%s overflow=%s/%s"
            % (status, accepted, last_ahead, code, overflow))

        status, reg2 = http("POST", BASE + "/register",
                            {"name": "积压工"}, token)
        checker.check(
            "backlog survives disconnect: name kept, queued=128 reported",
            row is not None and row.get("online") is False
            and row.get("queued") == 128
            and status == 200 and reg2.get("queued") == 128,
            "row=%s register=%s" % (row, reg2))

        # ---------------------------------------------------------------- #
        # 8. subscriptions: real trigger registration + pending + ACK + delete
        # ---------------------------------------------------------------- #
        t_token = trigger_token.read_text(encoding="utf-8").strip()

        def tcall(method: str, path: str, body=None):
            return trigger_http(method, trigger_url + path, body, t_token)

        def trigger_subs():
            code, payload = tcall("GET", "/subscriptions")
            return payload.get("subscriptions", []) if code == 200 else []

        status, sub = http("POST", BASE + "/subscriptions", {
            "worker": "波旁", "source": "mc_chat",
            "kinds": ["chat", "whisper"],
        }, token)
        sub_id = sub.get("id")
        trigger_id = sub.get("trigger_id") or ""
        status_list, subs = http(
            "GET", BASE + "/subscriptions?worker=%s" % quote("波旁"),
            token=token)
        listed = [s for s in subs.get("subscriptions", [])
                  if s.get("id") == sub_id]
        tsub = [s for s in trigger_subs() if s.get("name") == trigger_id]
        checker.check(
            "subscriptions: POST 201 -> row has trigger_id -> trigger active",
            status == 201 and sub.get("active") is True
            and sub.get("trigger_forwarded") is True
            and trigger_id == "router:波旁"
            and status_list == 200 and len(listed) == 1
            and listed[0].get("kinds") == ["chat", "whisper"]
            and listed[0].get("trigger_id") == trigger_id
            and len(tsub) == 1 and tsub[0].get("status") == "active",
            "create=%s/%s list=%s trigger=%s"
            % (status, sub, status_list, tsub))

        status_ev, ev1 = tcall("POST", "/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "notify",
            "text": "selftest chat 1", "actor": "mc", "event_id": "sub-1",
        })
        ev1_id = ev1.get("id")
        status_p1, pending1 = http(
            "GET", BASE + "/subscriptions/pending?worker=" + quote("波旁"),
            token=token)
        first = (pending1.get("events") or [{}])[0]
        checker.check(
            "pending: returns the trigger-queued event + subscription_id",
            status_ev == 200 and status_p1 == 200
            and pending1.get("count") == 1
            and first.get("id") == ev1_id
            and first.get("text") == "selftest chat 1"
            and first.get("subscription_id") == sub_id,
            "post=%s/%s pending=%s" % (status_ev, ev1, pending1))
        status_p2, pending2 = http(
            "GET", BASE + "/subscriptions/pending?worker=" + quote("波旁"),
            token=token)
        status_c, cursor_now = tcall(
            "GET", "/events?subscriber=%s&since=%s"
            % (quote(trigger_id, safe=""), ev1_id))
        checker.check(
            "pending: ACK empties the next poll and advances trigger cursor",
            status_p2 == 200 and pending2.get("count") == 0
            and status_c == 200 and cursor_now.get("events") == [],
            "pending2=%s trigger=%s" % (pending2, cursor_now))

        tcall("POST", "/trigger", {
            "source": "mc_chat", "kind": "system", "level": "notify",
            "text": "filtered out", "actor": "mc", "event_id": "sub-filter",
        })
        status_pf, pending_f = http(
            "GET", BASE + "/subscriptions/pending?worker=" + quote("波旁"),
            token=token)
        tcall("POST", "/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "notify",
            "text": "selftest chat 2", "actor": "mc", "event_id": "sub-2",
        })
        status_pc, pending_c = http(
            "GET", BASE + "/subscriptions/pending?worker=" + quote("波旁"),
            token=token)
        checker.check(
            "pending: kinds filter drops non-matching kinds, keeps matching",
            status_pf == 200 and pending_f.get("count") == 0
            and status_pc == 200 and pending_c.get("count") == 1
            and (pending_c.get("events") or [{}])[0].get("kind") == "chat",
            "filtered=%s matched=%s" % (pending_f.get("count"),
                                        pending_c.get("count")))

        status_del, deleted = http(
            "DELETE", BASE + "/subscriptions/%s" % sub_id, None, token)
        status_active, active = http(
            "GET", BASE + "/subscriptions?worker=%s&active=1" % quote("波旁"),
            token=token)
        tsub_after = [s for s in trigger_subs() if s.get("name") == trigger_id]
        checker.check(
            "subscriptions: DELETE removes row + closes trigger subscription",
            status_del == 200 and deleted.get("deleted") is True
            and deleted.get("trigger_closed") is True
            and all(s.get("id") != sub_id
                    for s in active.get("subscriptions", []))
            and len(tsub_after) == 1
            and tsub_after[0].get("status") == "closed",
            "delete=%s/%s list=%s trigger=%s"
            % (status_del, deleted, status_active, tsub_after))

        # ---------------------------------------------------------------- #
        # 8b. notice: worker -> Hermes, normal/urgent + minute hash
        # ---------------------------------------------------------------- #
        notice_text = "selftest notice normal"
        expected_id = notice_event_id("波旁", notice_text)
        status_n, notice = http("POST", BASE + "/notice",
                                {"worker": "波旁", "text": notice_text}, token)
        for _attempt in range(3):
            expected_id = notice_event_id("波旁", notice_text)
            if notice.get("event_id") == expected_id:
                break
            status_n, notice = http("POST", BASE + "/notice",
                                    {"worker": "波旁", "text": notice_text},
                                    token)
        conn = sqlite3.connect(str(trigger_db))
        try:
            nrow = conn.execute(
                "SELECT source, kind, level, actor, text FROM events"
                " WHERE event_id=?", (expected_id,)).fetchone()
        finally:
            conn.close()
        checker.check(
            "POST /notice normal -> ok, level notify, trigger worker.notice",
            status_n == 200 and notice.get("ok") is True
            and notice.get("level") == "notify"
            and isinstance(notice.get("trigger_id"), int)
            and notice.get("event_id") == expected_id
            and nrow is not None and nrow[0] == "router"
            and nrow[1] == "worker.notice" and nrow[2] == "notify"
            and nrow[3] == "波旁" and nrow[4] == notice_text,
            "status=%s body=%s row=%s" % (status_n, notice, nrow))

        dup_ok = False
        last = notice
        for _attempt in range(3):
            _sn, again = http("POST", BASE + "/notice",
                              {"worker": "波旁", "text": notice_text}, token)
            if again.get("event_id") == last.get("event_id"):
                dup_ok = again.get("duplicate") is True
                last = again
                break
            last = again
        conn = sqlite3.connect(str(trigger_db))
        try:
            dup_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_id=?",
                (last.get("event_id"),)).fetchone()[0]
        finally:
            conn.close()
        checker.check(
            "notice: same worker+text+minute -> one trigger row, duplicate:true",
            dup_ok and dup_count == 1,
            "dup_ok=%s count=%s body=%s" % (dup_ok, dup_count, last))

        status_u, urgent = http("POST", BASE + "/notice", {
            "worker": "波旁", "text": "selftest notice urgent",
            "urgency": "urgent",
        }, token)
        conn = sqlite3.connect(str(trigger_db))
        try:
            urow = conn.execute(
                "SELECT level FROM events WHERE event_id=?",
                (urgent.get("event_id"),)).fetchone()
        finally:
            conn.close()
        checker.check(
            "POST /notice urgency=urgent -> level event at trigger",
            status_u == 200 and urgent.get("ok") is True
            and urgent.get("level") == "event"
            and urow is not None and urow[0] == "event",
            "status=%s body=%s row=%s" % (status_u, urgent, urow))

        # ---------------------------------------------------------------- #
        # 8b2. approvals: worker asks, human decides, worker polls
        # ---------------------------------------------------------------- #
        status_a, asked = http("POST", BASE + "/approvals", {
            "worker": "波旁", "taskId": "t-approval-1", "kind": "command",
            "title": "运行 rm -rf ./tmp/build",
            "detail": "cwd=/tmp/build\n命令需要 escalated 权限",
            "options": ["approved", "denied"],
        }, token)
        approval_id = asked.get("approval_id") or ""
        status_g, fetched = http("GET", BASE + "/approvals/" + approval_id,
                                 token=token)
        approval_row = fetched.get("approval") or {}
        status_l, listing = http(
            "GET", BASE + "/approvals?status=pending&worker=%s"
            % quote("波旁"), token=token)
        conn = sqlite3.connect(str(trigger_db))
        try:
            arow = conn.execute(
                "SELECT kind, level, actor, payload_json FROM events"
                " WHERE event_id=?", ("approval:" + approval_id,)).fetchone()
        finally:
            conn.close()
        checker.check(
            "POST /approvals -> pending, camelCase taskId stored, "
            "GET + list agree, trigger approval.request/event",
            status_a == 200 and asked.get("ok") is True
            and asked.get("status") == "pending"
            and approval_id.startswith("ap_")
            and asked.get("notified") is True
            and 590 < float(asked.get("remaining_s") or 0) <= 600
            and status_g == 200 and approval_row.get("status") == "pending"
            and approval_row.get("task_id") == "t-approval-1"
            and approval_row.get("worker") == "波旁" and approval_row.get("kind") == "command"
            and approval_row.get("title") == "运行 rm -rf ./tmp/build"
            and "escalated" in (approval_row.get("detail") or "")
            and approval_row.get("options") == ["approved", "denied"]
            and status_l == 200
            and approval_id in [a.get("approval_id")
                                for a in listing.get("approvals", [])]
            and arow is not None and arow[0] == "approval.request"
            and arow[1] == "event" and arow[2] == "波旁"
            and approval_id in (arow[3] or ""),
            "ask=%s get=%s list=%s trigger=%s"
            % (status_a, approval_row.get("status"), status_l, arow))

        status_d, denied = http(
            "POST", BASE + "/approvals/%s/decide" % approval_id,
            {"decision": "denied", "note": "不要在共享机上删目录"}, token)
        status_d2, again = http(
            "POST", BASE + "/approvals/%s/decide" % approval_id,
            {"decision": "approved"}, token)
        status_g2, fetched2 = http("GET", BASE + "/approvals/" + approval_id,
                                   token=token)
        checker.check(
            "POST /approvals/{id}/decide -> denied + note, second decide "
            "409 already_decided, GET reflects it",
            status_d == 200 and denied.get("status") == "denied"
            and denied.get("decision") == "denied"
            and denied.get("note") == "不要在共享机上删目录"
            and denied.get("already") is False
            and status_d2 == 409 and again.get("error") == "already_decided"
            and again.get("decision") == "denied"
            and status_g2 == 200
            and (fetched2.get("approval") or {}).get("status") == "denied",
            "decide=%s/%s again=%s/%s"
            % (status_d, denied.get("status"), status_d2, again.get("error")))

        status_e, expiring = http("POST", BASE + "/approvals", {
            "worker": "波旁", "kind": "fileChange",
            "title": "写入 /etc/hosts", "ttl_s": 1,
        }, token)
        expiring_id = expiring.get("approval_id") or ""
        time.sleep(1.2)
        status_ge, expired = http("GET", BASE + "/approvals/" + expiring_id,
                                  token=token)
        status_de, late_decide = http(
            "POST", BASE + "/approvals/%s/decide" % expiring_id,
            {"decision": "approved"}, token)
        checker.check(
            "approval ttl_s=1 -> pending becomes expired; decide -> 409 "
            "approval_expired",
            status_e == 200 and status_ge == 200
            and (expired.get("approval") or {}).get("status") == "expired"
            and status_de == 409
            and late_decide.get("error") == "approval_expired",
            "get=%s decide=%s/%s"
            % ((expired.get("approval") or {}).get("status"), status_de,
               late_decide.get("error")))

        status_ua, missing = http("GET", BASE + "/approvals/ap_nope",
                                  token=token)
        status_ud, missing_d = http(
            "POST", BASE + "/approvals/ap_nope/decide",
            {"decision": "approved"}, token)
        status_bad_d, bad_d = http(
            "POST", BASE + "/approvals/%s/decide" % approval_id,
            {"decision": "maybe"}, token)
        status_bad_a, bad_a = http("POST", BASE + "/approvals",
                                   {"worker": "波旁"}, token)
        status_nauth, _ = http("GET", BASE + "/approvals/ap_nope")
        checker.check(
            "approval validation: unknown id 404, bad decision/body 400, "
            "missing token 401",
            status_ua == 404 and missing.get("error") == "unknown_approval"
            and status_ud == 404 and missing_d.get("error") == "unknown_approval"
            and status_bad_d == 400 and "decision" in (bad_d.get("error") or "")
            and status_bad_a == 400 and "title" in (bad_a.get("error") or "")
            and status_nauth == 401,
            "get=%s decide=%s bad_d=%s bad_a=%s auth=%s"
            % (status_ua, status_ud, status_bad_d, status_bad_a, status_nauth))

        status_h, health_a = http("GET", BASE + "/health")
        checker.check(
            "GET /health exposes approvals_pending (0 after decide/expire)",
            status_h == 200 and health_a.get("approvals_pending") == 0,
            "status=%s pending=%s" % (status_h, health_a.get("approvals_pending")))

        # ---------------------------------------------------------------- #
        # 8c. re-declare uses a fresh name; disconnect purges everything
        # ---------------------------------------------------------------- #
        status, sub2 = http("POST", BASE + "/subscriptions", {
            "worker": "波旁", "source": "mc_chat", "kinds": ["chat"],
        }, token)
        sub2_id = sub2.get("id")
        trigger_id2 = sub2.get("trigger_id") or ""
        tsub2 = [s for s in trigger_subs() if s.get("name") == trigger_id2]
        checker.check(
            "subscriptions: re-declare after delete gets a fresh trigger name",
            status == 201 and trigger_id2.startswith("router:波旁:")
            and len(tsub2) == 1 and tsub2[0].get("status") == "active",
            "create=%s trigger=%s" % (status, sub2))

        tcall("POST", "/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "notify",
            "text": "after redeclare", "actor": "mc", "event_id": "sub-3",
        })
        status_pn, pending_n = http(
            "GET", BASE + "/subscriptions/pending?worker=" + quote("波旁"),
            token=token)
        checker.check(
            "pending: new generation queue delivers its own events",
            status_pn == 200 and pending_n.get("count") == 1
            and (pending_n.get("events") or [{}])[0].get("trigger_id")
            == trigger_id2,
            "pending=%s" % (pending_n,))

        worker2.stop()
        deadline = time.time() + 6.0
        router_subs = None
        t_after = []
        while time.time() < deadline:
            _s, router_subs = http(
                "GET", BASE + "/subscriptions?worker=%s" % quote("波旁"),
                token=token)
            t_after = [s for s in trigger_subs()
                       if s.get("name") == trigger_id2]
            if (router_subs.get("count") == 0 and t_after
                    and t_after[0].get("status") == "closed"):
                break
            time.sleep(0.1)
        checker.check(
            "SSE disconnect: router purges all subscriptions, trigger closed",
            router_subs is not None and router_subs.get("count") == 0
            and len(t_after) == 1 and t_after[0].get("status") == "closed",
            "router=%s trigger=%s" % (router_subs, t_after))

        worker3 = FakeWorker("波旁", token)
        workers.append(worker3)
        worker3.start()
        wait_online("波旁", token, True)
        time.sleep(0.3)
        _s, after_reconnect = http(
            "GET", BASE + "/subscriptions?worker=%s" % quote("波旁"),
            token=token)
        checker.check(
            "reconnect: subscriptions are NOT rebuilt automatically",
            after_reconnect.get("count") == 0,
            "subscriptions=%s" % (after_reconnect,))
        worker3.stop()

        # ---------------------------------------------------------------- #
        # 8d. chat eavesdropping: store-only log, dedupe, filters, summary
        # ---------------------------------------------------------------- #
        chat_worker = "旁听工"
        chat_now = time.time()
        base_msgs = [
            ("in", "训练员：在吗", chat_now - 600, "s-1", "cm-1",
             {"from": "trainer"}),
            ("out", "worker：在", chat_now - 595, "s-1", "cm-2", None),
            ("in", "训练员：看下日志", chat_now - 590, "s-1", "cm-3", None),
            ("out", "worker：两小时前的旧话", chat_now - 7200, "s-0",
             "cm-4", None),
        ]
        chat_ids = []
        chat_ok = True
        chat_detail = []
        for direction, text, ts, session_id, message_id, meta in base_msgs:
            payload = {"worker": chat_worker, "direction": direction,
                       "text": text, "session_id": session_id,
                       "ts": ts, "message_id": message_id}
            if meta is not None:
                payload["meta"] = meta
            status_c, logged = http("POST", BASE + "/chat/log", payload, token)
            chat_ids.append(logged.get("id"))
            chat_ok = chat_ok and status_c == 200 and logged.get("ok") is True \
                and logged.get("duplicate") is False and isinstance(
                    logged.get("id"), int)
            chat_detail.append((status_c, logged))
        checker.check(
            "POST /chat/log writes in/out rows, ids ascending, duplicate:false",
            chat_ok and all(a < b for a, b in zip(chat_ids, chat_ids[1:])),
            "ids=%s detail=%s" % (chat_ids, chat_detail))

        status_m, listing = http(
            "GET", BASE + "/chat/%s/messages" % quote(chat_worker), token=token)
        msgs = listing.get("messages") or []
        first = msgs[0] if msgs else {}
        expected_keys = {"id", "worker", "direction", "text", "session_id",
                         "ts", "message_id"}
        conn = sqlite3.connect(str(router_db))
        try:
            meta_row = conn.execute(
                "SELECT meta FROM chat_messages WHERE id=?",
                (chat_ids[0],)).fetchone()
        finally:
            conn.close()
        meta_ok = False
        try:
            meta_ok = json.loads(meta_row[0] or "{}") == {"from": "trainer"}
        except (TypeError, ValueError):
            meta_ok = False
        checker.check(
            "GET /chat/{worker}/messages: write/read consistent, exact fields",
            status_m == 200 and listing.get("count") == 4 and len(msgs) == 4
            and [m["id"] for m in msgs] == chat_ids
            and set(first.keys()) == expected_keys
            and first.get("worker") == chat_worker
            and first.get("direction") == "in"
            and first.get("text") == "训练员：在吗"
            and first.get("session_id") == "s-1"
            and abs(float(first.get("ts") or 0) - (chat_now - 600)) < 0.01
            and first.get("message_id") == "cm-1" and meta_ok,
            "status=%s listing=%s meta=%s" % (status_m, listing, meta_row))

        status_d, dup = http("POST", BASE + "/chat/log", {
            "worker": chat_worker, "direction": "out", "text": "worker：在",
            "session_id": "s-1", "ts": chat_now - 595, "message_id": "cm-2",
        }, token)
        status_l2, listing2 = http(
            "GET", BASE + "/chat/%s/messages" % quote(chat_worker), token=token)
        checker.check(
            "POST /chat/log duplicate (worker+message_id+direction): id reused, "
            "no new row",
            status_d == 200 and dup.get("duplicate") is True
            and dup.get("id") == chat_ids[1]
            and listing2.get("count") == 4
            and [m["id"] for m in listing2.get("messages", [])] == chat_ids,
            "status=%s dup=%s count=%s" % (status_d, dup,
                                           listing2.get("count")))

        status_x, cross = http("POST", BASE + "/chat/log", {
            "worker": chat_worker, "direction": "in",
            "text": "方向不同但 message_id 相同", "ts": chat_now - 300,
            "message_id": "cm-2",
        }, token)
        chat_ids.append(cross.get("id"))
        checker.check(
            "POST /chat/log same message_id but other direction -> new row",
            status_x == 200 and cross.get("duplicate") is False
            and cross.get("id") not in chat_ids[:-1],
            "status=%s body=%s ids=%s" % (status_x, cross, chat_ids))

        status_lim, limited = http(
            "GET", BASE + "/chat/%s/messages?limit=2" % quote(chat_worker),
            token=token)
        status_dir, only_out = http(
            "GET", BASE + "/chat/%s/messages?direction=out"
            % quote(chat_worker), token=token)
        status_sin, since_two = http(
            "GET", BASE + "/chat/%s/messages?since=%s&limit=2"
            % (quote(chat_worker), chat_ids[0]), token=token)
        status_sid, since_dir = http(
            "GET", BASE + "/chat/%s/messages?since=%s&direction=out"
            % (quote(chat_worker), chat_ids[2]), token=token)
        dup_ids = [m["id"] for m in limited.get("messages") or []]
        checker.check(
            "GET messages: limit (newest N, ascending) + direction filter",
            status_lim == 200 and dup_ids == chat_ids[3:]
            and status_dir == 200 and only_out.get("count") == 2
            and [m["id"] for m in only_out.get("messages", [])]
            == [chat_ids[1], chat_ids[3]]
            and all(m["direction"] == "out"
                    for m in only_out.get("messages", [])),
            "limit=%s dir=%s" % (limited, only_out))
        checker.check(
            "GET messages: since=<id> cursor + since & direction combined",
            status_sin == 200 and [m["id"] for m in since_two.get("messages", [])]
            == [chat_ids[1], chat_ids[2]]
            and status_sid == 200
            and [m["id"] for m in since_dir.get("messages", [])]
            == [chat_ids[3]],
            "since=%s since_out=%s" % (since_two, since_dir))

        status_ts, ts_filtered = http(
            "GET", BASE + "/chat/%s/messages?since=%.3f"
            % (quote(chat_worker), chat_now - 1000), token=token)
        checker.check(
            "GET messages: since=<epoch ts> keeps only messages in the window",
            status_ts == 200 and ts_filtered.get("count") == 4
            and chat_ids[3] not in [m["id"] for m in ts_filtered["messages"]],
            "status=%s count=%s ids=%s"
            % (status_ts, ts_filtered.get("count"),
               [m["id"] for m in ts_filtered.get("messages", [])]))

        status_sum, summary = http(
            "GET", BASE + "/chat/%s/summary?minutes=60" % quote(chat_worker),
            token=token)
        status_sum2, summary2 = http(
            "GET", BASE + "/chat/%s/summary?minutes=180" % quote(chat_worker),
            token=token)
        status_none, empty_sum = http(
            "GET", BASE + "/chat/%s/summary?minutes=60" % quote("没人"),
            token=token)
        checker.check(
            "GET /chat/{worker}/summary: in/out counts + last, minutes window",
            status_sum == 200 and summary.get("minutes") == 60
            and summary.get("count") == 4 and summary.get("in") == 3
            and summary.get("out") == 1
            and (summary.get("last") or {}).get("id") == chat_ids[-1]
            and status_sum2 == 200 and summary2.get("count") == 5
            and summary2.get("in") == 3 and summary2.get("out") == 2,
            "60=%s 180=%s" % (summary, summary2))
        checker.check(
            "GET /chat/{worker}/summary: unknown worker -> zeros, last null",
            status_none == 200 and empty_sum.get("count") == 0
            and empty_sum.get("in") == 0 and empty_sum.get("out") == 0
            and empty_sum.get("last") is None,
            "status=%s body=%s" % (status_none, empty_sum))

        status_bad, bad_dir = http(
            "GET", BASE + "/chat/%s/messages?direction=sideways"
            % quote(chat_worker), token=token)
        status_badlog, bad_log = http("POST", BASE + "/chat/log", {
            "worker": chat_worker, "direction": "sideways", "text": "x",
        }, token)
        checker.check(
            "chat validation: bad direction -> 400 on log and messages",
            status_bad == 400 and "direction" in (bad_dir.get("error") or "")
            and status_badlog == 400
            and "direction" in (bad_log.get("error") or ""),
            "get=%s/%s post=%s/%s"
            % (status_bad, bad_dir, status_badlog, bad_log))

        # ---------------------------------------------------------------- #
        # 9. final shape / counts
        # ---------------------------------------------------------------- #
        status, health = http("GET", BASE + "/health")
        checker.check(
            "GET /health final: workers=2, tasks=135 (queued 128)",
            status == 200 and health.get("workers") == 2
            and health.get("tasks") == 135
            and health.get("tasks_by_state", {}).get("queued") == 128,
            "status=%s body=%s" % (status, health))

        conn = sqlite3.connect(str(trigger_db))
        try:
            notify_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE source='router'"
                " AND kind='task.result' AND level='notify'").fetchone()[0]
        finally:
            conn.close()
        checker.check(
            "trigger has exactly 5 task.result notify rows",
            notify_count == 5, "count=%s" % notify_count)

        _, tasks = http("GET", BASE + "/tasks?worker=%s&limit=500"
                        % quote("积压工"), token=token)
        checker.check(
            "GET /tasks?worker= returns the 128 backlog rows",
            tasks.get("count") == 128,
            "count=%s" % tasks.get("count"))

        # ---------------------------------------------------------------- #
        # 10. trigger down: subscription 502 + rollback, notice ok:false
        # ---------------------------------------------------------------- #
        dead_port = free_port()
        dead_trigger_port = free_port()
        while dead_trigger_port == dead_port:
            dead_trigger_port = free_port()
        dead_base = "http://127.0.0.1:%d" % dead_port
        dead_token = work / "dead.token"
        dead_proc, dead_out = start_process([
            PY, str(ROUTER),
            "--port", str(dead_port),
            "--db", str(work / "dead.db"),
            "--token-file", str(dead_token),
            "--lock", str(work / "dead.lock"),
            "--log", str(work / "dead.log"),
            "--trigger-url", "http://127.0.0.1:%d/trigger" % dead_trigger_port,
            "--trigger-token-file", str(trigger_token),
        ], work / "dead.out")
        try:
            if not wait_http(dead_base + "/health", timeout=8.0):
                raise RuntimeError("dead-trigger router did not start: "
                                   + tail(work / "dead.log"))
            dtok = dead_token.read_text(encoding="utf-8").strip()
            http("POST", dead_base + "/register", {"name": "断链工"}, dtok)
            status_s, fail_sub = http(
                "POST", dead_base + "/subscriptions",
                {"worker": "断链工", "source": "mc_chat"}, dtok)
            status_l, fail_list = http(
                "GET", dead_base + "/subscriptions?worker=" + quote("断链工"),
                token=dtok)
            status_n, fail_notice = http(
                "POST", dead_base + "/notice",
                {"worker": "断链工", "text": "trigger is down"}, dtok)
            status_ap, fail_approval = http(
                "POST", dead_base + "/approvals",
                {"worker": "断链工", "kind": "command",
                 "title": "trigger down approval"}, dtok)
            checker.check(
                "trigger down: POST /subscriptions -> 502, table rolled back",
                status_s == 502 and fail_sub.get("error") == "trigger_unavailable"
                and status_l == 200 and fail_list.get("count") == 0,
                "status=%s/%s list=%s" % (status_s, fail_sub, fail_list))
            checker.check(
                "trigger down: POST /notice -> 200 ok:false (no 5xx)",
                status_n == 200 and fail_notice.get("ok") is False
                and fail_notice.get("error") == "trigger_unavailable",
                "status=%s body=%s" % (status_n, fail_notice))
            checker.check(
                "trigger down: POST /approvals -> 200 pending, notified:false",
                status_ap == 200 and fail_approval.get("ok") is True
                and fail_approval.get("status") == "pending"
                and fail_approval.get("notified") is False,
                "status=%s body=%s" % (status_ap, fail_approval))
        finally:
            try:
                dead_proc.terminate()
                dead_proc.wait(timeout=5)
            except Exception:
                try:
                    dead_proc.kill()
                except Exception:
                    pass
            try:
                dead_out.close()
            except Exception:
                pass

        control_selftest(checker, token, workers)
        peer_selftest(checker, work, trigger_url, trigger_token)
        workorders_selftest(checker, work, trigger_url, trigger_token)

        for worker_obj in workers:
            worker_obj.stop()
        workers = []

    finally:
        for worker_obj in workers:
            try:
                worker_obj.stop()
            except Exception:
                pass
        for proc, handle in ((router_proc, router_out),
                             (trigger_proc, trigger_out)):
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    if checker.failed:
        print("\n--- router log (tail) ---", flush=True)
        print(tail(router_log), flush=True)
        print("\n--- router stdout (tail) ---", flush=True)
        print(tail(work / "router.out"), flush=True)
        print("\n--- trigger log (tail) ---", flush=True)
        print(tail(trigger_log), flush=True)


def main() -> int:
    if not ROUTER.exists():
        print("missing %s" % ROUTER)
        return 2
    if not TRIGGER.exists():
        print("missing %s" % TRIGGER)
        return 2
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", PORT))
    except OSError:
        print("port %d is already in use; stop the running router or set "
              "ROUTER_TEST_PORT" % PORT)
        return 2
    finally:
        probe.close()

    work = Path(tempfile.mkdtemp(prefix="router-selftest-"))
    print("workdir: %s" % work, flush=True)
    checker = Checker()
    try:
        run(work, checker)
    except Exception:
        checker.failed += 1
        traceback.print_exc()
    finally:
        print("\nTOTAL %d/%d PASS" % (checker.passed, checker.total),
              flush=True)
        if not checker.failed:
            shutil.rmtree(str(work), ignore_errors=True)
        else:
            print("workdir kept for inspection: %s" % work, flush=True)
    return 0 if checker.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
