#!/usr/bin/env python3
"""Isolated Router and trigger integration checks for messaging, dispatch and subscriptions.
Lifecycle, retry and replay acceptance runs in selftest_idle_probe.py."""

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
from unittest import mock

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
         timeout: float = 5.0, *, reviewer=None):
    """Small JSON client; returns (status, payload). Never raises on 4xx/5xx."""
    data = None
    if body is not None:
        # Escaped Unicode also lets validation tests send lone surrogates as
        # legal JSON bytes; the server must reject those invalid text values.
        data = json.dumps(body, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    if reviewer:
        req.add_header("X-Review-Token", reviewer)
    if token:
        req.add_header("X-Router-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return int(resp.status), (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        with exc:
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
        with exc:
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


def fixture_attach(server, name):
    return server.attach(name)


worker_http = http


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
        self.cancellations: "queue.Queue[dict]" = queue.Queue()
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
                    if event in ("task", "worker_message", "task_cancel") and data:
                        try:
                            payload = json.loads("\n".join(data))
                        except ValueError:
                            payload = None
                        if isinstance(payload, dict):
                            target = (self.tasks if event == "task" else self.cancellations
                                      if event == "task_cancel" else self.messages)
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



def peer_selftest(checker, work, trigger_url, trigger_token, trigger_db):
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
            return worker_http(method, base + route, body, token)

        def payload(**changes):
            body = {"message_id": str(uuid.uuid4()), "from": a, "to": b,
                    "text": marker}
            body.update(changes)
            return body

        def wake_rows(message):
            with sqlite3.connect(str(trigger_db)) as saved:
                saved.row_factory = sqlite3.Row
                return [dict(row) for row in saved.execute(
                    "SELECT source, kind, level, actor, text, event_id FROM events WHERE event_id=?",
                    ("peer:%s" % message["seq"],))]

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
                       ("GET", "/messages?worker=%s&view=history" % quote(b), None),
                       ("GET", "/messages/" + mid, None),
                       ("GET", "/messages?id=" + mid, None),
                       ("POST", "/messages/" + mid + "/ack", {"worker": b}),
                       ("GET", "/stream?name=" + quote(b), None)]
        checker.check("peer auth: create/inbox/id/ACK/SSE require the router token",
                      all(worker_http(method, base + route, body, given)[0] == 401
                          for given in (None, "invalid-token")
                          for method, route, body in auth_routes))

        invalid = [{"message_id": "not-a-uuid"}, {"message_id": 123},
                   {"from": []}, {"to": ""}, {"text": " \n"}, {"text": 123},
                   {"task_id": "not-a-task-id"}, {"text": "\ud800"},
                   {"reply_to": "not-a-uuid"}, {"deadline_s": 30}]
        checker.check("peer validation: UUID, worker types, Unicode text and optional task identity",
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
                          "/messages?worker=%s&after=9223372036854775808" % quote(b),
                          "/messages?worker=%s&view=invalid" % quote(b),
                          "/messages?worker=%s&before=1" % quote(b),
                          "/messages?worker=%s&view=history&after=1" % quote(b),
                          "/messages?worker=%s&view=history&before=0" % quote(b),
                          "/messages?worker=%s&view=history&before=9223372036854775808" % quote(b))))
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
        checker.check("peer worker recipients stay queued without a trigger wake",
                      wake_rows(first_message) == [])
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

        code, root_message = call("POST", "/messages", payload(**{
            "from": "Hermes", "to": c, "text": "溪流普通私聊"}))
        root_reply_body = payload(**{
            "from": c, "to": "Hermes", "text": "普通回复", "reply_to": root_message.get("message_id")})
        root_reply_code, root_reply = call("POST", "/messages", root_reply_body)
        root_history = call("GET", "/messages?worker=Hermes&view=history")[1]
        root_inbox = call("GET", "/messages?worker=Hermes")[1]
        checker.check("peer Hermes identity: bidirectional chat needs no worker registration or task",
                      code == 202 and root_reply_code == 200
                      and len(root_history.get("messages", [])) == 2
                      and root_inbox.get("messages") == []
                      and not any(row["name"] == "Hermes" for row in call("GET", "/workers")[1]["workers"]))
        root_peer = root_reply["message"]
        checker.check("peer secretary wake: one 私聊 notify and a durable delivery ACK",
                      root_reply["status"] == "accepted" and root_peer["acked_at"] is not None
                      and root_peer["status"] == "delivered"
                      and call("GET", "/messages/" + root_peer["message_id"])[1]["status"] == "delivered"
                      and wake_rows(root_peer) == [{
                          "source": "私聊", "kind": "peer.message", "level": "notify", "actor": c,
                          "text": "溪流，%s 给你发了消息（seq %s）：普通回复" % (c, root_peer["seq"]),
                          "event_id": "peer:%s" % root_peer["seq"]}])
        retry_code, root_retry = call("POST", "/messages", root_reply_body)
        checker.check("peer Hermes exact retry keeps one notify and the original delivery timestamp",
                      retry_code == 200 and root_retry.get("duplicate") is True
                      and root_retry["message"] == root_peer and len(wake_rows(root_peer)) == 1)

        aliases = []
        for alias in ("溪流", "hermes", "HeRmEs"):
            alias_code, sent = call("POST", "/messages", payload(to=alias))
            aliases.append(sent["message"])
            checker.check("peer root alias %s wakes without worker registration" % alias,
                          alias_code == 200 and sent["status"] == "accepted"
                          and len(wake_rows(sent["message"])) == 1)
        root_alias_history = call("GET", "/messages?worker=%s&view=history" % quote("溪流"))[1]
        checker.check("peer root aliases share history while retaining the original sender and recipient",
                      set(ids(root_alias_history["messages"])) ==
                      {root_message["message_id"], root_peer["message_id"], *ids(aliases)})

        long_text = "溪流🐴，\n" * 10000
        body = payload(to="Hermes", text=long_text, task_id="t-peer-50000")
        code, posted = call("POST", "/messages", body)
        seq = posted["message"]["seq"]
        with sqlite3.connect(str(trigger_db)) as saved:
            events = saved.execute("SELECT text FROM events WHERE event_id LIKE ? ORDER BY id",
                                   ("peer:%s:%%" % seq,)).fetchall()
        received = "".join(row[0].split(body["message_id"] + " · ", 1)[1] for row in events)
        found = call("GET", "/messages?task_id=t-peer-50000")[1]["messages"]
        checker.check("50000-character private message through actual trigger HTTP and SQLite",
                      code == 200 and len(long_text) == 50000 and received == long_text and len(events) == 25
                      and [m["message_id"] for m in found] == [body["message_id"]]
                      and found[0]["acked_at"] is not None)

        concurrent_body = payload(to="溪流", text="甲🟦" * 150)
        concurrent = []
        writers = [threading.Thread(target=lambda: concurrent.append(
            call("POST", "/messages", concurrent_body))) for _ in range(6)]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(10)
        concurrent_peer = call("GET", "/messages/" + concurrent_body["message_id"])[1]["message"]
        checker.check("peer concurrent retries emit one notify with the complete Unicode text",
                      len(concurrent) == 6 and all(code == 200 for code, _ in concurrent)
                      and concurrent_peer["acked_at"] is not None
                      and len(wake_rows(concurrent_peer)) == 1
                      and wake_rows(concurrent_peer)[0]["text"] ==
                      "溪流，%s 给你发了消息（seq %s）：%s" %
                      (a, concurrent_peer["seq"], concurrent_body["text"]))

        unavailable_body = payload(to="Hermes", text="recover wake with the same UUID")
        original_token = trigger_token.read_bytes()
        try:
            trigger_token.write_bytes(b"")
            unavailable_code, unavailable = call("POST", "/messages", unavailable_body)
        finally:
            trigger_token.write_bytes(original_token)
        checker.check("peer unavailable trigger preserves HTTP 200 and the queued durable message",
                      unavailable_code == 200 and unavailable["ok"] is True
                      and unavailable["status"] == "queued"
                      and unavailable["message"]["acked_at"] is None
                      and wake_rows(unavailable["message"]) == [])
        recovered_code, recovered = call("POST", "/messages", unavailable_body)
        checker.check("peer exact retry repairs a failed wake without creating another message",
                      recovered_code == 200 and recovered["duplicate"] is True
                      and recovered["message"]["seq"] == unavailable["message"]["seq"]
                      and recovered["status"] == "accepted"
                      and recovered["message"]["acked_at"] is not None
                      and len(wake_rows(recovered["message"])) == 1)
        call("POST", "/messages/" + root_message["message_id"] + "/ack", {"worker": c})
        call("POST", "/messages/" + root_reply["message_id"] + "/ack", {"worker": "Hermes"})

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

        def history(name, **params):
            query = "&".join("%s=%s" % (key, value) for key, value in params.items())
            return call("GET", "/messages?worker=%s&view=history&%s" % (quote(name), query))[1]

        records, before = [], 9223372036854775807
        while True:
            page = history(b, before=before, limit=7)
            records.extend(page["messages"])
            if not page["has_more"]:
                break
            if not 0 < page["next_before"] < before:
                raise RuntimeError("peer history cursor did not descend")
            before = page["next_before"]
        checker.check("peer history: both directions include accepted and queued messages newest first",
                      mid in ids(records) and reply["message_id"] in ids(records)
                      and {row["status"] for row in records} == {"queued", "accepted"}
                      and [row["seq"] for row in records] == sorted((row["seq"] for row in records), reverse=True)
                      and len(ids(records)) == len(set(ids(records))))
        checker.check("peer history: worker scope, byte/count limits and attachment metadata are preserved",
                      set(ids(history(c)["messages"])) == {root_message["message_id"], root_reply["message_id"]}
                      and history(b, limit=1)["count"] == 1
                      and len(json.dumps(history(b), ensure_ascii=False).encode()) <= 256 * 1024
                      and next(row for row in records if row["message_id"] == attached["message_id"])["attachments"] == [image_meta])
        checker.check("peer history: reads do not ACK, replay SSE, create tasks or change pending inbox",
                      ids(inbox()) == [row["message_id"] for row in all_pending if row["message_id"] != mid] + [live["message_id"]]
                      and stream.wait_message(0.2) is None and call("GET", "/tasks")[1].get("count") == 0)

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
        _, restarted_root = call("POST", "/messages", root_reply_body)
        checker.check("peer root delivery survives router restart without another wake",
                      restarted_root.get("message") == root_peer and len(wake_rows(root_peer)) == 1)
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


def worker_names_selftest(checker, work, trigger_url, trigger_token, trigger_db):
    """Real HTTP checks with an isolated, live-read display-name configuration."""
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    names_file = work / "worker-names.json"
    token_file = work / "names.token"
    env = {**os.environ, "ROUTER_WORKER_NAMES_FILE": str(names_file)}
    proc, output = start_process([
        PY, str(ROUTER), "--port", str(port), "--db", str(work / "names.db"),
        "--token-file", str(token_file), "--lock", str(work / "names.lock"),
        "--log", str(work / "names.log"), "--trigger-url", trigger_url + "/trigger",
        "--trigger-token-file", str(trigger_token)], work / "names.out", env=env)
    worker = None
    try:
        if not wait_http(base + "/health"):
            raise RuntimeError("worker-name router did not start")
        token = token_file.read_text().strip()

        def call(method, route, body=None):
            return worker_http(method, base + route, body, token)

        def workers():
            return {row["name"]: row for row in call("GET", "/workers")[1]["workers"]}

        def event(event_id):
            with sqlite3.connect(str(trigger_db)) as saved:
                saved.row_factory = sqlite3.Row
                return dict(saved.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone())

        names = {"OguriCap": "小栗帽", "TamamoCross": "玉藻十字",
                 "qicao-codexworker": "气槽", "Rudolf": "鲁道夫象征", "Hermes": "溪流"}
        for name in names:
            if name != "Hermes":
                call("POST", "/register", {"name": name})
        checker.check("worker names: missing configuration falls back to unchanged English IDs",
                      all(row["display_name"] == name for name, row in workers().items()))
        names_file.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
        checker.check("worker names: workers expose configured Chinese display_name and stable name",
                      all(workers()[name]["display_name"] == display
                          for name, display in names.items() if name != "Hermes"))

        body = {"message_id": str(uuid.uuid4()), "from": "小栗帽", "to": "玉藻十字", "text": "中文互聊"}
        code, sent = call("POST", "/messages", body)
        stored = call("GET", "/messages/" + body["message_id"])[1]["message"]
        with sqlite3.connect(str(work / "names.db")) as saved:
            identities = saved.execute("SELECT sender, recipient FROM peer_messages WHERE message_id=?",
                                       (body["message_id"],)).fetchone()
        checker.check("worker names: Chinese peers persist English IDs and echo sender receipt spellings",
                      code == 202 and sent["message"]["from"] == "小栗帽"
                      and sent["message"]["to"] == "玉藻十字"
                      and sent["message"]["sender_id"] == "OguriCap"
                      and sent["message"]["recipient_id"] == "TamamoCross"
                      and identities == ("OguriCap", "TamamoCross")
                      and stored["from"] == "OguriCap" and stored["to"] == "TamamoCross")
        chinese_inbox = call("GET", "/messages?worker=" + quote("玉藻十字"))[1]
        english_inbox = call("GET", "/messages?worker=TamamoCross")[1]
        _, duplicate = call("POST", "/messages", body)
        checker.check("worker names: Chinese and English inboxes, ACK and retries address the same record",
                      chinese_inbox == english_inbox and duplicate["duplicate"] is True
                      and call("POST", "/messages/" + body["message_id"] + "/ack", {"worker": "玉藻十字"})[0] == 200)

        _, peer = call("POST", "/messages", {"message_id": str(uuid.uuid4()), "from": "OguriCap",
                                               "to": "溪流", "text": "显示名真实协议"})
        wake = event("peer:%s" % peer["message"]["seq"])
        checker.check("worker names: secretary notify reads 来自 私聊 · 小栗帽 with stable peer.message kind",
                      wake["source"] == "私聊" and wake["actor"] == "小栗帽"
                      and wake["kind"] == "peer.message" and wake["level"] == "notify"
                      and wake["text"].startswith("溪流，小栗帽 给你发了消息")
                      and json.loads(wake["payload_json"])["from"] == "OguriCap"
                      and peer["message"]["status"] == "delivered")

        worker = FakeWorker("OguriCap", token, port=port)
        worker.start()
        if not worker.connected.wait(5):
            raise RuntimeError("worker-name task fixture did not connect")
        _, task = call("POST", "/tasks", {"worker": "OguriCap", "goal": "显示名结果"})
        if worker.wait_task(5) is None:
            raise RuntimeError("worker-name task fixture did not receive task")
        code, response = call("POST", "/notice", {"worker": "OguriCap", "task_id":task["task_id"],
                                          "kind":"task.result", "text":"old completion"})
        checker.check("old completion notice cannot add a second task notification",
                      code == 409 and not response["notified"])
        _, approval = call("POST", "/approvals", {"worker": "OguriCap", "kind": "command", "title": "测试审批"})
        approval_event = event("approval:" + approval["approval_id"])
        checker.check("worker names: approval uses Chinese actor/title and English worker ID",
                      approval_event["actor"] == "小栗帽" and "小栗帽：测试审批" in approval_event["text"]
                      and json.loads(approval_event["payload_json"])["worker"] == "OguriCap")
        _, notice = call("POST", "/notice", {"worker": "OguriCap", "text": "显示名通知"})
        checker.check("worker names: ordinary notice uses the configured display actor",
                      event(notice["event_id"])["actor"] == "小栗帽")

        names_file.write_text(json.dumps({"OguriCap": "小栗帽"}), encoding="utf-8")
        checker.check("worker names: missing key falls back to its unchanged English ID",
                      workers()["TamamoCross"]["display_name"] == "TamamoCross")
        names_file.write_text("{broken", encoding="utf-8")
        checker.check("worker names: invalid configuration remains available with English fallbacks",
                      all(row["display_name"] == name for name, row in workers().items()))
    finally:
        if worker is not None:
            worker.stop()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        output.close()


def question_proxy_selftest(checker, work):
    """Cross-worker question RPC on the existing stream, with real HTTP clients."""
    import server as router_module
    from concurrent.futures import ThreadPoolExecutor

    store = router_module.RouterStore(str(work / "question-proxy.db"))
    server = router_module.RouterServer(("127.0.0.1", 0), store, "question-proxy-test-token",
                                        mock.Mock(), mock.Mock())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    names = ("QuestionMac", "QuestionWindows")
    connections = {}
    for name in names:
        store.upsert_worker(name, control_sse=True)
        connections[name] = fixture_attach(server, name)
    pool = ThreadPoolExecutor(max_workers=2)
    question_id = str(uuid.uuid4())

    def call(method, route, body=None, token=server.token):
        return worker_http(method, base + route, body, token, timeout=8)

    def respond(request, worker, status=200, body=None):
        return call("POST", "/questions/responses", {"request_id": request["request_id"],
                    "worker": worker, "status": status, "body": body or {"questionId": question_id, "worker": worker}})

    try:
        checker.check("question proxy: unauthenticated read is rejected",
                      call("GET", "/questions/QuestionMac/" + question_id, token=None)[0] == 401)
        checker.check("question proxy: unknown worker and invalid question ID are rejected",
                      call("GET", "/questions/Missing/" + question_id)[0] == 404
                      and call("GET", "/questions/QuestionMac/bad-id")[0] == 400)
        reads = {name: pool.submit(call, "GET", "/questions/" + name + "/" + question_id) for name in names}
        requests = {name: connections[name].queue.get(timeout=3) for name in names}
        checker.check("question proxy: two workers with the same question UUID receive separate requests",
                      all(event["event"] == "question_request" and event["data"]["question_id"] == question_id
                          and event["data"]["operation"] == "get" for event in requests.values())
                      and len({event["data"]["request_id"] for event in requests.values()}) == 2)
        wrong = respond(requests[names[0]]["data"], names[1])
        checker.check("question proxy: another worker cannot complete a pending request",
                      wrong[0] == 403 and wrong[1]["error"] == "question_worker_mismatch"
                      and not reads[names[0]].done())
        for name in reversed(names):
            respond(requests[name]["data"], name)
        values = {name: future.result(timeout=3) for name, future in reads.items()}
        checker.check("question proxy: out-of-order responses return to the correct worker URL",
                      all(code == 200 and data["worker"] == name for name, (code, data) in values.items())
                      and store.counts()["total"] == 0)
        checker.check("question proxy: ended response IDs cannot be reused",
                      respond(requests[names[0]]["data"], names[0])[0] == 404)
        for action in ({"answers": ["选项甲"]}, {"action": "skip"}, {"answers": ["自由文本"]}):
            body = {"actor": "Hermes", "threadId": "original-thread", "nonce": str(uuid.uuid4()), **action}
            future = pool.submit(call, "POST", "/questions/QuestionMac/" + question_id + "/answer", body)
            event = connections[names[0]].queue.get(timeout=3)
            request = event["data"]
            respond(request, names[0], body={"questionId": question_id, "status": "answer_pending", **action})
            code, answer = future.result(timeout=3)
            checker.check("question proxy: exact %s body reaches only its worker" % (action.get("action") or action["answers"][0]),
                          event["event"] == "question_request" and request["operation"] == "answer"
                          and request["input"] == body and code == 200 and answer["status"] == "answer_pending"
                          and connections[names[1]].queue.empty() and store.counts()["total"] == 0)
        for text, expected in [("/model", {"action": "list"}),
                               ("/model next", {"action": "list", "direction": 1}),
                               ("/model page 3", {"action": "list", "page": 2}),
                               ("/model Native Model", {"action": "set", "value": "Native Model"}),
                               ("/effort high", {"action": "set", "value": "high"})]:
            body = {"worker": names[0], "message_id": str(uuid.uuid4()), "thread_id": "original", "text": text}
            future = pool.submit(call, "POST", "/commands", body)
            event = connections[names[0]].queue.get(timeout=3)
            request = event["data"]
            wrong = respond(request, names[0])
            response = call("POST", "/commands/responses", {"worker": names[0],
                "request_id": request["request_id"], "status": 200, "body": {"text": text}})
            code, value = future.result(timeout=3)
            checker.check("model command: router parses and routes %s" % text,
                          event["event"] == "worker_command" and wrong[0] == 403
                          and response[0] == 200 and code == 200 and value["text"] == text
                          and all(request["command"][key] == val for key, val in expected.items())
                          and request["command"]["messageId"] == body["message_id"]
                          and store.counts()["total"] == 0)
        checker.check("model command: invalid pagination and missing request identity reject",
                      call("POST", "/commands", {"worker": names[0], "thread_id": "original", "text": "/model page 0"})[0] == 400
                      and call("POST", "/commands", {"worker": names[0], "thread_id": "original", "text": "/effort"})[0] == 400)
        server.detach(connections[names[1]])
        checker.check("question proxy: offline worker returns a recoverable error without closing a question",
                      call("GET", "/questions/QuestionWindows/" + question_id)[0] == 503)
        code, body = call("GET", "/questions/QuestionMac/" + question_id)
        checker.check("question proxy: missing worker response times out and releases the pending slot",
                      code == 504 and body["error"] == "question_worker_timeout"
                      and not server.question_requests and store.counts()["total"] == 0)
    finally:
        pool.shutdown(wait=True)
        server.shutdown()
        server.server_close()
        thread.join(5)
        store.close()


def start_process(argv, log_path: Path, env=None):
    handle = open(str(log_path), "wb")
    proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT, env=env)
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
            "--backlog-max", "128",
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

        status, health = worker_http("GET", BASE + "/health")
        checker.check(
            "GET /health (no token): ok, version, workers, tasks",
            status == 200 and health.get("ok") is True
            and health.get("version") == "0.4.0"
            and health.get("capabilities", {}).get("workOrders") == "idempotent-goal-v1"
            and health.get("workers") == 0 and health.get("tasks") == 0,
            "status=%s body=%s" % (status, health))

        status, payload = worker_http("GET", BASE + "/workers")
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
        status, reg = worker_http("POST", BASE + "/register",
                           {"name": "波旁", "version": "fake-1"}, token)
        checker.check(
            "POST /register {name} -> ok, queued=0, new=true",
            status == 200 and reg.get("ok") is True
            and reg.get("queued") == 0 and reg.get("new") is True,
            "status=%s body=%s" % (status, reg))

        status, payload = worker_http("POST", BASE + "/tasks",
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

        status, created = worker_http("POST", BASE + "/tasks",
                               {"worker": "波旁", "goal": "打印 hello",
                                "context": "selftest", "urgency": "urgent",
                                "workspace": "/tmp", "deadline_s": 60}, token)
        task1 = created.get("task_id")
        checker.check(
            "online+idle: POST /tasks -> 200 dispatched:true",
            status == 200 and created.get("dispatched") is True
            and created.get("in_running_tasks") is True,
            "status=%s body=%s" % (status, created))
        received = worker.wait_task(5.0)
        checker.check(
            "SSE delivered event:task with matching task_id/goal",
            received is not None and received.get("task_id") == task1
            and received.get("goal") == "打印 hello"
            and received.get("type") == "task",
            "received=%s" % (received,))

        # 8. subscriptions: real trigger registration + pending + ACK + delete
        # ---------------------------------------------------------------- #
        t_token = trigger_token.read_text(encoding="utf-8").strip()

        def tcall(method: str, path: str, body=None):
            return trigger_http(method, trigger_url + path, body, t_token)

        def trigger_subs():
            code, payload = tcall("GET", "/subscriptions")
            return payload.get("subscriptions", []) if code == 200 else []

        status, sub = worker_http("POST", BASE + "/subscriptions", {
            "worker": "波旁", "source": "mc_chat",
            "kinds": ["chat", "whisper"],
        }, token)
        sub_id = sub.get("id")
        trigger_id = sub.get("trigger_id") or ""
        status_list, subs = worker_http(
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
        status_p1, pending1 = worker_http(
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
        status_p2, pending2 = worker_http(
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
        status_pf, pending_f = worker_http(
            "GET", BASE + "/subscriptions/pending?worker=" + quote("波旁"),
            token=token)
        tcall("POST", "/trigger", {
            "source": "mc_chat", "kind": "chat", "level": "notify",
            "text": "selftest chat 2", "actor": "mc", "event_id": "sub-2",
        })
        status_pc, pending_c = worker_http(
            "GET", BASE + "/subscriptions/pending?worker=" + quote("波旁"),
            token=token)
        checker.check(
            "pending: kinds filter drops non-matching kinds, keeps matching",
            status_pf == 200 and pending_f.get("count") == 0
            and status_pc == 200 and pending_c.get("count") == 1
            and (pending_c.get("events") or [{}])[0].get("kind") == "chat",
            "filtered=%s matched=%s" % (pending_f.get("count"),
                                        pending_c.get("count")))

        status_del, deleted = worker_http(
            "DELETE", BASE + "/subscriptions/%s" % sub_id, None, token)
        status_active, active = worker_http(
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
        status_n, notice = worker_http("POST", BASE + "/notice",
                                {"worker": "波旁", "text": notice_text}, token)
        for _attempt in range(3):
            expected_id = notice_event_id("波旁", notice_text)
            if notice.get("event_id") == expected_id:
                break
            status_n, notice = worker_http("POST", BASE + "/notice",
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
            _sn, again = worker_http("POST", BASE + "/notice",
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

        status_u, urgent = worker_http("POST", BASE + "/notice", {
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
        status_a, asked = worker_http("POST", BASE + "/approvals", {
            "worker": "波旁", "taskId": "t-approval-1", "kind": "command",
            "title": "运行 rm -rf ./tmp/build",
            "detail": "cwd=/tmp/build\n命令需要 escalated 权限",
            "options": ["approved", "denied"],
        }, token)
        approval_id = asked.get("approval_id") or ""
        status_g, fetched = worker_http("GET", BASE + "/approvals/" + approval_id,
                                 token=token)
        approval_row = fetched.get("approval") or {}
        status_l, listing = worker_http(
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

        status_d, denied = worker_http(
            "POST", BASE + "/approvals/%s/decide" % approval_id,
            {"decision": "denied", "note": "不要在共享机上删目录"}, token)
        status_d2, again = worker_http(
            "POST", BASE + "/approvals/%s/decide" % approval_id,
            {"decision": "approved"}, token)
        status_g2, fetched2 = worker_http("GET", BASE + "/approvals/" + approval_id,
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

        status_e, expiring = worker_http("POST", BASE + "/approvals", {
            "worker": "波旁", "kind": "fileChange",
            "title": "写入 /etc/hosts", "ttl_s": 1,
        }, token)
        expiring_id = expiring.get("approval_id") or ""
        time.sleep(1.2)
        status_ge, expired = worker_http("GET", BASE + "/approvals/" + expiring_id,
                                  token=token)
        status_de, late_decide = worker_http(
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

        status_ua, missing = worker_http("GET", BASE + "/approvals/ap_nope",
                                  token=token)
        status_ud, missing_d = worker_http(
            "POST", BASE + "/approvals/ap_nope/decide",
            {"decision": "approved"}, token)
        status_bad_d, bad_d = worker_http(
            "POST", BASE + "/approvals/%s/decide" % approval_id,
            {"decision": "maybe"}, token)
        status_bad_a, bad_a = worker_http("POST", BASE + "/approvals",
                                   {"worker": "波旁"}, token)
        status_nauth, _ = worker_http("GET", BASE + "/approvals/ap_nope")
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

        status_h, health_a = worker_http("GET", BASE + "/health")
        checker.check(
            "GET /health exposes approvals_pending (0 after decide/expire)",
            status_h == 200 and health_a.get("approvals_pending") == 0,
            "status=%s pending=%s" % (status_h, health_a.get("approvals_pending")))

        # ---------------------------------------------------------------- #
        # 8c. re-declare uses a fresh name; disconnect purges everything
        # ---------------------------------------------------------------- #
        status, sub2 = worker_http("POST", BASE + "/subscriptions", {
            "worker": "波旁", "source": "mc_chat", "kinds": ["chat"],
        }, token)
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
        status_pn, pending_n = worker_http(
            "GET", BASE + "/subscriptions/pending?worker=" + quote("波旁"),
            token=token)
        checker.check(
            "pending: new generation queue delivers its own events",
            status_pn == 200 and pending_n.get("count") == 1
            and (pending_n.get("events") or [{}])[0].get("trigger_id")
            == trigger_id2,
            "pending=%s" % (pending_n,))

        worker.stop()
        deadline = time.time() + 6.0
        router_subs = None
        t_after = []
        while time.time() < deadline:
            _s, router_subs = worker_http(
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
        _s, after_reconnect = worker_http(
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
            status_c, logged = worker_http("POST", BASE + "/chat/log", payload, token)
            chat_ids.append(logged.get("id"))
            chat_ok = chat_ok and status_c == 200 and logged.get("ok") is True \
                and logged.get("duplicate") is False and isinstance(
                    logged.get("id"), int)
            chat_detail.append((status_c, logged))
        checker.check(
            "POST /chat/log writes in/out rows, ids ascending, duplicate:false",
            chat_ok and all(a < b for a, b in zip(chat_ids, chat_ids[1:])),
            "ids=%s detail=%s" % (chat_ids, chat_detail))

        status_m, listing = worker_http(
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

        status_d, dup = worker_http("POST", BASE + "/chat/log", {
            "worker": chat_worker, "direction": "out", "text": "worker：在",
            "session_id": "s-1", "ts": chat_now - 595, "message_id": "cm-2",
        }, token)
        _, listing2 = worker_http(
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

        status_x, cross = worker_http("POST", BASE + "/chat/log", {
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

        status_lim, limited = worker_http(
            "GET", BASE + "/chat/%s/messages?limit=2" % quote(chat_worker),
            token=token)
        status_dir, only_out = worker_http(
            "GET", BASE + "/chat/%s/messages?direction=out"
            % quote(chat_worker), token=token)
        status_sin, since_two = worker_http(
            "GET", BASE + "/chat/%s/messages?since=%s&limit=2"
            % (quote(chat_worker), chat_ids[0]), token=token)
        status_sid, since_dir = worker_http(
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

        status_ts, ts_filtered = worker_http(
            "GET", BASE + "/chat/%s/messages?since=%.3f"
            % (quote(chat_worker), chat_now - 1000), token=token)
        checker.check(
            "GET messages: since=<epoch ts> keeps only messages in the window",
            status_ts == 200 and ts_filtered.get("count") == 4
            and chat_ids[3] not in [m["id"] for m in ts_filtered["messages"]],
            "status=%s count=%s ids=%s"
            % (status_ts, ts_filtered.get("count"),
               [m["id"] for m in ts_filtered.get("messages", [])]))

        status_sum, summary = worker_http(
            "GET", BASE + "/chat/%s/summary?minutes=60" % quote(chat_worker),
            token=token)
        status_sum2, summary2 = worker_http(
            "GET", BASE + "/chat/%s/summary?minutes=180" % quote(chat_worker),
            token=token)
        status_none, empty_sum = worker_http(
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

        status_bad, bad_dir = worker_http(
            "GET", BASE + "/chat/%s/messages?direction=sideways"
            % quote(chat_worker), token=token)
        status_badlog, bad_log = worker_http("POST", BASE + "/chat/log", {
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
        _, health = worker_http("GET", BASE + "/health")
        checker.check("health counts tasks and active memberships",health["tasks"] >= health["active_tasks"])
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
            worker_http("POST", dead_base + "/register", {"name": "断链工"}, dtok)
            status_s, fail_sub = worker_http(
                "POST", dead_base + "/subscriptions",
                {"worker": "断链工", "source": "mc_chat"}, dtok)
            status_l, fail_list = worker_http(
                "GET", dead_base + "/subscriptions?worker=" + quote("断链工"),
                token=dtok)
            status_n, fail_notice = worker_http(
                "POST", dead_base + "/notice",
                {"worker": "断链工", "text": "trigger is down"}, dtok)
            status_ap, fail_approval = worker_http(
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
            status_peer, fail_peer = worker_http("POST", dead_base + "/messages", {
                "message_id": str(uuid.uuid4()), "from": "断链工", "to": "溪流",
                "text": "peer trigger unavailable"}, dtok)
            checker.check("trigger down: root peer stays queued with HTTP 200 and a warning",
                          status_peer == 200 and fail_peer.get("ok") is True
                          and fail_peer.get("status") == "queued"
                          and fail_peer.get("message", {}).get("acked_at") is None
                          and "peer wake failed" in (work / "dead.log").read_text())
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

        peer_selftest(checker, work, trigger_url, trigger_token, trigger_db)
        worker_names_selftest(checker, work, trigger_url, trigger_token, trigger_db)
        question_proxy_selftest(checker, work)

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
    os.environ.setdefault("ROUTER_WORKER_NAMES_FILE", str(work / "missing-worker-names.json"))
    os.environ.setdefault("ROUTER_APPROVAL_POLICY", str(work / "missing-approval-policy.json"))
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
