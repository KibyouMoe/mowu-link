#!/usr/bin/env python3
"""selftest_router.py — real end-to-end test for mowu-router.

Starts real processes with throwaway state:

  * server/trigger/server.py on a free port with a throwaway DB, so the
    notify path is verified against the real trigger service (not a mock and
    not the production ~/.hermes/trigger.db);
  * server/router/server.py on 127.0.0.1:8790 (override with
    ROUTER_TEST_PORT) with a throwaway DB/token/lock/log.

Then drives a fake worker over a real SSE connection and exercises:

  register → online → immediate dispatch → result → idle → done → trigger event
  offline 409 → queue_if_offline 202 → reconnect → one-at-a-time FIFO
  running task → worker_busy → retry after terminal result
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
  default one waiting slot with queue TTL, concurrent admission and recovery;
  explicit zero backlog: busy trainer/router turns, running/cancelling tasks,
    immediate idle dispatch, concurrent winners, reusable refused UUIDs and recovery
  deterministic connection-race injection over HTTP checks unaccepted-row cleanup
    and FIFO admission while accepted queued work awaits recovery
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


def worker_http(method, url, body=None, token=None, timeout=5.0):
    """Admission fixtures finish a fake turn using result + real idle heartbeats.

    Return the terminal exchange receipt. Completion-specific tests import the
    raw http() client instead and assert the individual 202/heartbeat responses.
    """
    code, response = http(method, url, body, token, timeout)
    report = isinstance(body, dict) and (url.endswith("/result") or
        url.endswith("/notice") and body.get("kind") == "task.result")
    if method != "POST" or not report or code != 202 or not response.get("candidate"):
        return code, response
    worker = body.get("name", body.get("worker"))
    origin = url.rsplit("/", 1)[0]
    http("POST", origin + "/heartbeat", {"name": worker, "state": "running",
        "turn_source": "router", "task_id": body["task_id"]}, token)
    _, idle = http("POST", origin + "/heartbeat", {"name": worker, "state": "idle",
        "turn_source": None, "task_id": body["task_id"]}, token)
    settled = next((item for item in idle.get("settlements", []) if item["task_id"] == body["task_id"]), None)
    if settled is None:
        _, view = http("GET", origin + "/tasks/" + body["task_id"], token=token)
        if view.get("state") not in ("done", "failed", "cancelled"):
            return code, response
        settled = {"task_id": body["task_id"], "state": view["state"], "notified": False, "duplicate": True}
    else:
        settled["duplicate"] = False
    return 200, {"ok": True, **settled,
                 "dispatched_next": idle.get("dispatched_next"),
                 **({"error": "trigger_unavailable"} if url.endswith("/notice")
                    and not settled["notified"] and not settled["duplicate"] else {})}


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

        concurrent_body = payload(to="溪流", text="甲🟦" * 150)
        concurrent = []
        writers = [threading.Thread(target=lambda: concurrent.append(
            call("POST", "/messages", concurrent_body))) for _ in range(6)]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(10)
        concurrent_peer = call("GET", "/messages/" + concurrent_body["message_id"])[1]["message"]
        checker.check("peer concurrent retries emit one notify with a 200-character Unicode preview",
                      len(concurrent) == 6 and all(code == 200 for code, _ in concurrent)
                      and concurrent_peer["acked_at"] is not None
                      and len(wake_rows(concurrent_peer)) == 1
                      and wake_rows(concurrent_peer)[0]["text"] ==
                      "溪流，%s 给你发了消息（seq %s）：%s" %
                      (a, concurrent_peer["seq"], concurrent_body["text"][:200]))

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
        call("POST", "/result", {"name": "OguriCap", "task_id": task["task_id"],
                                   "status": "done", "summary": "完成测试"})
        result_event = event(task["task_id"] + ":done")
        checker.check("worker names: task result uses Chinese actor/text and English routing metadata",
                      result_event["actor"] == "小栗帽"
                      and result_event["text"].startswith("小栗帽 完成 " + task["task_id"])
                      and json.loads(result_event["payload_json"])["worker"] == "OguriCap")
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


def task_notice_selftest(checker, work, trigger_url, trigger_token, trigger_db):
    """Exercise both real HTTP entry points against a real trigger and durable store."""
    import server as router_module
    from concurrent.futures import ThreadPoolExecutor

    db_path = str(work / "task-notices.db")
    store = router_module.RouterStore(db_path)
    client = router_module.TriggerClient(trigger_url + "/trigger", str(trigger_token))
    notifier = router_module.TriggerNotifier(client, store)
    server = router_module.RouterServer(("127.0.0.1", 0), store, "notice-test-token",
                                        notifier, mock.Mock())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    worker = "notice-test-worker"
    store.upsert_worker(worker)
    server.attach(worker)

    def call(route, body):
        return worker_http("POST", base + route, body, server.token)

    def new_task():
        code, result = call("/tasks", {"worker": worker, "goal": "task notice fixture"})
        if code != 200:
            raise RuntimeError("notice fixture task failed: %s" % result)
        return result["task_id"]

    def finish(task_id):
        return call("/result", {"name": worker, "task_id": task_id,
                                "status": "done", "summary": "internal result first"})

    def notice(task_id, state="done", text="worker result", **extra):
        return call("/notice", {"worker": worker, "kind": "task.result",
                                "task_id": task_id, "status": state, "text": text, **extra})

    def events(task_id):
        with sqlite3.connect(str(trigger_db)) as saved:
            saved.row_factory = sqlite3.Row
            return [dict(row) for row in saved.execute(
                "SELECT * FROM events WHERE event_id LIKE ? ORDER BY id", (task_id + ":%",))]

    try:
        # Subscribe before creating the fixtures so this checks delivery as well as storage.
        _, bus = client.request("GET", "/bus/health")
        since = bus["last_id"]
        for state, expected, urgency in (("done", "event", "normal"),
                                         ("failed", "event", "normal"),
                                         ("cancelled", "notify", "urgent")):
            task_id = new_task()
            if state == "cancelled":
                call("/tasks/" + task_id + "/cancel", None)
                code, settled = call("/tasks/" + task_id + "/cancel-ack", {
                    "name": worker, "outcome": {"ok": True, "task_id": task_id, "state": state}})
            else:
                # Check a candidate receipt before any event is delivered.
                pending_code, pending = http("POST", base + "/notice", {
                    "worker": worker, "kind": "task.result", "task_id": task_id,
                    "status": state, "text": "pending result", "urgency": urgency}, server.token)
                checker.check("task terminal level: %s candidate reports event without delivery" % state,
                              pending_code == 202 and pending.get("level") == expected
                              and pending.get("event_id") == task_id + ":" + state
                              and pending.get("notified") is False and not events(task_id))
                code, settled = call("/result", {"name": worker, "task_id": task_id,
                                                  "status": state, "summary": "settled result"})
            _, receipt = notice(task_id, state, urgency=urgency)
            trigger_status, fetched = client.request("GET", "/events", params={
                "subscriber": "hermes-terminal-level-test", "since": since, "levels": "event,notify"})
            event_id = task_id + ":" + state
            deliveries = [event for event in fetched.get("events", []) if event["event_id"] == event_id]
            observed = [event["level"] for event in deliveries]
            checker.check("task terminal level: %s -> %s" % (state, expected),
                          code == 200 and settled.get("state") == state
                          and receipt.get("ok") is True and receipt.get("level") == expected
                          and receipt.get("event_id") == event_id
                          and trigger_status == 200 and observed == [expected]
                          and deliveries[0]["kind"] == "task.result"
                          and deliveries[0]["payload"]["status"] == state,
                          "urgency=%s receipt=%s trigger=%s" % (urgency, receipt.get("level"), observed))

        oversized = new_task()
        full_summary = "完整正文😀" * 5000
        code, rejected = call("/result", {"name": worker, "task_id": oversized,
                                          "status": "done", "summary": full_summary})
        checker.check("result summary: oversized legacy input is rejected before terminal state or notify",
                      code == 400 and "300" in rejected.get("error", "")
                      and "summary_short" in rejected.get("error", "")
                      and store.get_task(oversized)["state"] == "running" and not events(oversized))
        for invalid in ["长" * 301, 123, None]:
            code, rejected = call("/result", {"name": worker, "task_id": oversized,
                                              "status": "done", "summary": full_summary,
                                              "summary_short": invalid})
            checker.check("result summary: invalid summary_short is rejected without finishing (%r)" % type(invalid).__name__,
                          code == 400 and "summary_short" in rejected.get("error", "")
                          and store.get_task(oversized)["state"] == "running" and not events(oversized))
        short = "短" * 299 + "😀"
        code, accepted = call("/result", {"name": worker, "task_id": oversized,
                                         "status": "done", "summary": full_summary, "summary_short": short})
        stored = store.get_task(oversized)
        checker.check("result summary: 300 Unicode characters notify intact while the full body remains readable",
                      code == 200 and accepted["state"] == "done"
                      and stored["result"]["summary"] == full_summary
                      and stored["result"]["summary_short"] == short
                      and events(oversized)[0]["text"].endswith(short))
        boundary = new_task()
        code, _ = call("/result", {"name": worker, "task_id": boundary,
                                   "status": "done", "summary": "字" * 300})
        checker.check("result summary: a 300-character legacy summary is accepted without truncation",
                      code == 200 and events(boundary)[0]["text"].endswith("字" * 300))
        explicit = new_task()
        code, rejected = notice(explicit, text="长" * 301)
        checker.check("task notice: explicit completion uses the same summary validation",
                      code == 400 and not events(explicit))
        code, _ = notice(explicit, text=full_summary[:5000], summary_short="完整交付见任务结果")
        code_result, _ = call("/result", {"name": worker, "task_id": explicit,
                                          "status": "done", "summary": full_summary,
                                          "summary_short": "完整交付见任务结果"})
        checker.check("task notice: explicit and automatic completion share one normalized result envelope",
                      code == 200 and code_result == 200 and len(events(explicit)) == 1
                      and events(explicit)[0]["text"].endswith("完整交付见任务结果"))

        with mock.patch.object(client, "post_trigger", wraps=client.post_trigger) as sent:
            first = new_task()
            finish(first)
            result_code, result_retry = finish(first)
            code, duplicate = notice(first, " DONE ", "different wording", urgency="urgent")
            first_events = events(first)
            checker.check("task terminal dedup: repeated result and worker notice send one event",
                          result_code == 200 and result_retry.get("duplicate") is True
                          and code == 200 and duplicate["duplicate"] is True and sent.call_count == 1
                          and duplicate["level"] == "event" and len(first_events) == 1
                          and first_events[0]["source"] == "任务完成"
                          and first_events[0]["text"].endswith("internal result first"),
                          "trigger_posts=%d trigger_rows=%d" % (sent.call_count, len(first_events)))

            second = new_task()
            _, original = notice(second, text="worker result first")
            finish(second)
            checker.check("task notice: worker notice then internal result preserves first arrival",
                          sent.call_count == 2 and not original.get("duplicate", False)
                          and events(second)[0]["text"].endswith("worker result first")
                          and store.get_task(second)["state"] == "done")

            third = new_task()
            notice(third, text="worker result first")
            finish(third)
            checker.check("task notice: identical text on a different task is delivered separately",
                          sent.call_count == 3 and len(events(third)) == 1
                          and events(third)[0]["id"] != events(second)[0]["id"])

            notice(first, "failed", "later failure")
            _, cancelled = notice(first, " CANCELED ", "later cancellation")
            _, cancel_again = notice(first, "cancelled", "changed cancellation wording")
            checker.check("task notice: late failure or cancellation preserves the first terminal result",
                          sent.call_count == 3 and len(events(first)) == 1
                          and [row["source"] for row in events(first)]
                          == ["任务完成"]
                          and router_module.terminal_notice_state(" CANCELED ") == "cancelled"
                          and cancelled["event_id"] == first + ":done"
                          and cancel_again["duplicate"] is True)

            _, inferred = call("/notice", {"worker": worker, "kind": "task.result",
                                           "task_id": first, "text": "legacy terminal notice"})
            checker.check("task notice: omitted status reads a terminal task and uses the same key",
                          inferred["duplicate"] is True and sent.call_count == 3)
            _, progress = notice(first, "unverified", "explicit progress remains ordinary")
            with sqlite3.connect(str(trigger_db)) as saved:
                progress_source = saved.execute("SELECT source FROM events WHERE event_id=?",
                                                (progress["event_id"],)).fetchone()[0]
            checker.check("task notice: explicit nonterminal progress keeps its ordinary source",
                          progress_source == "router" and progress["event_id"].startswith("notice:"))

            concurrent = new_task()
            before = sent.call_count
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(finish, concurrent)]
                futures.extend(pool.submit(notice, concurrent, "done", "race %d" % index)
                               for index in range(7))
                responses = [future.result() for future in futures]
            checker.check("task notice: concurrent result and worker notices send exactly one trigger request",
                          all(code == 200 and body.get("ok") for code, body in responses)
                          and sent.call_count == before + 1 and len(events(concurrent)) == 1)

            retry_task = new_task()
            with mock.patch.object(client, "post_trigger", return_value=(False, {"detail": "offline"})):
                finish(retry_task)
                code, failed = notice(retry_task)
            checker.check("task notice: trigger failure returns HTTP 200 and does not consume the key",
                          code == 200 and failed["error"] == "trigger_unavailable"
                          and failed.get("level") == "event"
                          and store.task_notice_receipt(retry_task, "done") is None)
            _, retried = notice(retry_task)
            checker.check("task notice: same key is retryable after trigger recovery",
                          retried["ok"] is True and retried["duplicate"] is False
                          and len(events(retry_task)) == 1)

        server.shutdown()
        thread.join(2)
        store.close()
        store = router_module.RouterStore(db_path)
        restarted = router_module.TriggerNotifier(client, store)
        with mock.patch.object(client, "post_trigger") as sent, \
                mock.patch.object(router_module.log, "info") as logged:
            accepted = restarted.notify_result({"task_id": first, "worker": worker}, "done", "restart retry")
        checker.check("task notice: persisted receipt survives a new store/notifier and logs the dedup key",
                      accepted and sent.call_count == 0
                      and mock.call("notice dedup task=%s state=%s", first, "done") in logged.call_args_list)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        store.close()

    bounded = router_module.RouterStore(str(work / "notice-bounds.db"))
    try:
        for index in range(501):
            bounded.remember_task_notice("bounded-%d" % index, "done", {"id": index})
        count = bounded._conn.execute("SELECT COUNT(*) FROM task_notice_dedup").fetchone()[0]
        checker.check("task notice: receipt table holds at most 500 and evicts its oldest entry",
                      count == 500 and bounded.task_notice_receipt("bounded-0", "done") is None
                      and bounded.task_notice_receipt("bounded-500", "done") == {"id": 500})
        stamp = time.time()
        bounded._conn.execute("UPDATE task_notice_dedup SET delivered_at=? WHERE task_id='bounded-1'",
                              (stamp - router_module.TASK_NOTICE_TTL_S,))
        with mock.patch.object(router_module.time, "time", return_value=stamp):
            expired = bounded.task_notice_receipt("bounded-1", "done")
        checker.check("task notice: 24-hour boundary expires without removing a fresh receipt",
                      expired is None and bounded.task_notice_receipt("bounded-500", "done") == {"id": 500}
                      and bounded._conn.execute("SELECT COUNT(*) FROM task_notice_dedup").fetchone()[0] == 499)
    finally:
        bounded.close()


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
            return worker_http(method, base + route, body, token)

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
        delivered = stream.wait_task(3)
        checker.check("workorders SSE emits formal goal mode and holds the second queued task",
                      delivered is not None and delivered.get("task_id") == task_id
                      and delivered.get("execution_mode") == "goal"
                      and delivered.get("deadline_s") is None and delivered.get("deadline_at") is None
                      and get_task(explicit_id).get("state") == "queued")
        checker.check("workorders live retry returns original task_id without another SSE dispatch",
                      receipt(*call("POST", "/tasks", body), task_id) and stream.wait_task(0.3) is None)
        stream.stop()
        checker.check("workorders offline retry after disconnect retains the same queued task",
                      online(False) and receipt(*call("POST", "/tasks", body), task_id)
                      and get_task(task_id).get("state") == "queued")
        reconnected = connect()
        replayed = reconnected.wait_task(3)
        checker.check("workorders reconnect replays original IDs once; HTTP retries add no dispatch",
                      replayed is not None and replayed.get("task_id") == task_id
                      and receipt(*call("POST", "/tasks", body), task_id)
                      and receipt(*call("POST", "/tasks", explicit_body), explicit_id)
                      and reconnected.wait_task(0.3) is None)
        finish_code, _ = http("POST", base + "/result", {"name": name, "task_id": task_id,
                              "status": "done", "summary": "first task business result"}, token)
        code, completed = call("POST", "/tasks", body)
        checker.check("workorders retry after a candidate preserves the same running task until idle",
                      finish_code == 202 and receipt(code, completed, task_id)
                      and completed.get("state") == "running" and reconnected.wait_task(0.2) is None)

        # Crash this test process with a running candidate and a waiting task.
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
                      and get_task(task_id).get("state") == "queued"
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
        first = restarted.wait_task(3)
        finish(task_id)
        second = restarted.wait_task(3)
        checker.check("workorders restart replays the two unsettled original IDs, retaining the candidate",
                      [task and task.get("task_id") for task in (first, second)] == [task_id, explicit_id]
                      and all(task.get("execution_mode") == "goal" for task in (first, second))
                      and second.get("deadline_s") == 7.5
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
                                         "task_id": body["task_id"], "state": "cancelled", "cancelled": True}).encode())
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
            code, _ = worker_http("POST", BASE + "/register",
                           {"name": name, "control_url": bad}, token)
            checker.check("control callback rejects unsafe origin " + bad, code == 400)
        code, _ = worker_http("POST", BASE + "/register",
                       {"name": name, "control_url": origin}, token)
        _, queued = worker_http("POST", BASE + "/tasks", {
            "worker": name, "goal": "queued", "queue_if_offline": True}, token)
        _, cancelled_q = worker_http("POST", BASE + "/tasks/%s/cancel" % queued["task_id"], None, token)
        checker.check("queued cancel never calls the worker",
                      not calls and cancelled_q.get("cancel_delivery", {}).get("skipped") == "queued_task")
        worker = FakeWorker(name, token)
        workers.append(worker)
        worker.start()
        checker.check("control worker registers loopback callback and connects",
                      code == 200 and wait_online(name, token, True))
        _, active = worker_http("POST", BASE + "/tasks", {"worker": name, "goal": "active"}, token)
        worker.wait_task(3)
        _, cancelled = worker_http("POST", BASE + "/tasks/%s/cancel" % active["task_id"], None, token)
        checker.check("running cancel reaches exact worker task with authenticated POST",
                      cancelled.get("cancel_delivery", {}).get("ok") is True
                      and calls == [("/manager/tasks/%s/cancel" % active["task_id"],
                                     "Bearer " + token, {"task_id": active["task_id"]})])
        _, saved = worker_http("GET", BASE + "/tasks/" + active["task_id"], token=token)
        checker.check("cancel forwarding receipt persists on the task",
                      saved["task"]["result"]["cancel_delivery"]["ok"] is True)
        _, failing = worker_http("POST", BASE + "/tasks", {"worker": name, "goal": "fail"}, token)
        worker.wait_task(3)
        response_status[0] = 503
        _, failed = worker_http("POST", BASE + "/tasks/%s/cancel" % failing["task_id"], None, token)
        checker.check("worker refusal is explicit rather than a silent cancel",
                      failed.get("cancel_delivery", {}).get("ok") is False)
        response_status[0] = 200
        _, retry = worker_http("POST", BASE + "/tasks/%s/cancel" % failing["task_id"], None, token)
        checker.check("an unconfirmed cancel can be retried safely by task id",
                      retry.get("already") is True and retry.get("cancel_delivery", {}).get("ok") is True)

        # An actual running task plus a real router approval must expose public progress.
        _, visible = worker_http("POST", BASE + "/tasks", {"worker": name, "goal": "approval progress"}, token)
        worker.wait_task(3)
        _, asked = worker_http("POST", BASE + "/approvals", {
            "worker": name, "task_id": visible["task_id"], "kind": "approval",
            "title": "write outside workspace", "ttl_s": 60, "auto": False}, token)
        aid = asked["approval_id"]
        _, snapshot = worker_http("GET", BASE + "/tasks/" + visible["task_id"], token=token)
        progress = snapshot["task"].get("progress") or {}
        checker.check("task progress exposes waiting approval id and readable reason",
                      progress.get("state") == "waiting_approval"
                      and progress.get("approval_id") == aid
                      and ("等待审批 " + aid) in progress.get("text", ""))
        _, listing = worker_http("GET", BASE + "/tasks?worker=" + name, token=token)
        row = next(t for t in listing["tasks"] if t["task_id"] == visible["task_id"])
        checker.check("task listing exposes the same approval progress",
                      row.get("progress", {}).get("approval_id") == aid)
        worker_http("POST", BASE + "/approvals/" + aid + "/decide",
             {"decision": "approved"}, token)
        worker_http("POST", BASE + "/result", {"name": name, "task_id": visible["task_id"],
             "status": "done", "summary": "done"}, token)
        _, finished = worker_http("GET", BASE + "/tasks/" + visible["task_id"], token=token)
        checker.check("finished task result retains approval id without a waiting state",
                      finished["task"]["result"].get("approval_id") == aid
                      and finished["task"]["progress"]["waiting_for_approval"] is False)
        _, aborting = worker_http("POST", BASE + "/tasks", {"worker": name, "goal": "cancel approval"}, token)
        worker.wait_task(3)
        _, asked = worker_http("POST", BASE + "/approvals", {
            "worker": name, "task_id": aborting["task_id"], "kind": "approval",
            "title": "cancel this approval", "ttl_s": 60, "auto": False}, token)
        worker_http("POST", BASE + "/tasks/" + aborting["task_id"] + "/cancel", None, token)
        _, decision = worker_http("GET", BASE + "/approvals/" + asked["approval_id"], token=token)
        checker.check("task cancellation closes its outstanding router approval",
                      decision["approval"]["status"] == "denied")
        _, late = worker_http("POST", BASE + "/approvals", {
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


def activity_selftest(checker, token, workers):
    name = "trainer-state-test"
    worker_http("POST", BASE + "/register", {"name": name, "control_sse": True}, token)
    client = FakeWorker(name, token)
    workers.append(client)
    client.start()
    checker.check("activity worker connects", wait_online(name, token, True))
    worker_http("POST", BASE + "/heartbeat", {"name": name, "state": "running",
         "turn_source": "trainer", "task_id": None, "progress_at": time.time()}, token)
    row = worker_row(name, token)
    code, task = worker_http("POST", BASE + "/tasks", {"worker": name, "goal": "task alongside trainer turn"}, token)
    checker.check("trainer turn stays visible without making the task slot busy",
                  not row["busy"] and row["turn_source"] == "trainer" and code == 200
                  and task["state"] == "running")
    dispatched = client.wait_task(3)
    checker.check("trainer turn alone does not prevent task delivery",
                  dispatched and dispatched["task_id"] == task["task_id"])
    worker_http("POST", BASE + "/heartbeat", {"name": name, "state": "running",
         "turn_source": "router", "task_id": task["task_id"]}, token)
    worker_http("POST", BASE + "/result", {"name": name, "task_id": task["task_id"],
         "status": "done", "summary": "actual completion"}, token)
    _, saved = worker_http("GET", BASE + "/tasks/" + task["task_id"], token=token)
    row = worker_row(name, token)
    checker.check("completion clears task identity and writes the exact finish time to progress",
                  row["task_id"] is None and row["progress_at"] == saved["task"]["finished_at"])
    worker_http("POST", BASE + "/heartbeat", {"name": name, "state": "idle",
         "task_id": task["task_id"], "progress_at": 1}, token)
    row = worker_row(name, token)
    checker.check("a late heartbeat cannot restore a terminal task or move progress backwards",
                  row["task_id"] is None and row["progress_at"] == saved["task"]["finished_at"])
    _, cancel = worker_http("POST", BASE + "/tasks", {"worker": name, "goal": "SSE cancellation"}, token)
    client.wait_task(3)
    code, pending = worker_http("POST", BASE + "/tasks/" + cancel["task_id"] + "/cancel", None, token)
    try:
        command = client.cancellations.get(timeout=3)
    except queue.Empty:
        command = None
    checker.check("remote worker cancellation uses the existing authenticated SSE channel",
                  code == 202 and pending["state"] == "cancelling"
                  and command == {"task_id": cancel["task_id"]})
    bad, _ = worker_http("POST", BASE + "/tasks/" + cancel["task_id"] + "/cancel-ack", {
        "name": "another-worker", "outcome": {"task_id": cancel["task_id"], "ok": True, "state": "cancelled"}}, token)
    _, acknowledged = worker_http("POST", BASE + "/tasks/" + cancel["task_id"] + "/cancel-ack", {
        "name": name, "outcome": {"task_id": cancel["task_id"], "ok": True, "state": "cancelled"}}, token)
    checker.check("only the assigned execution owner can confirm cancellation",
                  bad == 400 and acknowledged["state"] == "cancelled" and not worker_row(name, token)["busy"])


def zero_backlog_selftest(checker, work, trigger_url, trigger_token):
    """Exercise the default admission policy through real HTTP and SSE."""
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    db = work / "zero-backlog.db"
    token_path = work / "zero-backlog.token"
    log_path = work / "zero-backlog.log"
    argv = [PY, str(ROUTER), "--port", str(port), "--db", str(db),
            "--token-file", str(token_path), "--lock", str(work / "zero-backlog.lock"),
            "--log", str(log_path), "--trigger-url", trigger_url + "/trigger",
            "--trigger-token-file", str(trigger_token), "--ping-interval", "0.2",
            "--backlog-max", "0"]
    proc, output = start_process(argv, work / "zero-backlog.out")
    clients = []
    name = "zero-wait-worker"
    try:
        if not wait_http(base + "/health"):
            raise RuntimeError("zero-backlog router did not start: " + tail(log_path))
        token = token_path.read_text(encoding="utf-8").strip()

        def call(method, route, body=None):
            return worker_http(method, base + route, body, token)

        def row():
            return next(w for w in call("GET", "/workers")[1]["workers"] if w["name"] == name)

        def connect():
            client = FakeWorker(name, token, port=port)
            clients.append(client)
            client.start()
            if not client.connected.wait(5) or client.error or not row()["online"]:
                raise RuntimeError("zero-backlog SSE did not connect")
            return client

        def payload(goal):
            return {"worker": name, "goal": goal, "message_id": str(uuid.uuid4())}

        def heartbeat(state="idle", source=None, task_id=None):
            return call("POST", "/heartbeat", {"name": name, "state": state,
                        "turn_source": source, "task_id": task_id})

        def saved():
            with sqlite3.connect(str(db)) as connection:
                return connection.execute(
                    "SELECT task_id, state, request_id FROM tasks ORDER BY rowid").fetchall()

        def busy(body, task_id, source):
            before = saved()
            code, result = call("POST", "/tasks", body)
            return (code == 409 and result.get("error") == "worker_busy"
                    and result.get("busy") is True and "task_id" in result
                    and result.get("task_id") == task_id and "turn_source" in result
                    and result.get("turn_source") == source
                    and result.get("retry_when") == "current_task_accepted_and_worker_idle"
                    and "outstanding task" in result.get("message", "") and saved() == before)

        def duplicate(body, task_id, state):
            code, result = call("POST", "/tasks", body)
            return (code == 200 and result.get("duplicate") is True
                    and result.get("task_id") == task_id and result.get("state") == state
                    and result.get("dispatched") is False)

        def finish(task_id):
            return call("POST", "/result", {"name": name, "task_id": task_id,
                        "status": "done", "summary": "zero-backlog completion"})

        checker.check("zero backlog: explicit --backlog-max 0 refuses waiting work",
                      call("GET", "/health")[1].get("backlog_max") == 0)
        negative = subprocess.run([PY, str(ROUTER), "--backlog-max", "-1",
                                   "--log", str(work / "negative-backlog.log")],
                                  capture_output=True, text=True, timeout=10)
        checker.check("zero backlog: negative capacity rejected before startup",
                      negative.returncode == 2 and "must be >= 0" in negative.stderr)
        call("POST", "/register", {"name": name, "control_sse": True})
        offline_bodies = [dict(payload("offline-%s" % value), queue_if_offline=value)
                          for value in (False, True)]
        refusals = [call("POST", "/tasks", body) for body in offline_bodies]
        checker.check("zero backlog: offline refuses even queue_if_offline=true; no task or UUID saved",
                      all(code == 409 and result.get("error") == "worker_offline"
                          for code, result in refusals) and saved() == []
                      and row()["queued"] == 0)

        client = connect()
        trainer_body = payload("admit during trainer conversation")
        for state in ("running", "inProgress", "waiting_input", "waiting_approval"):
            heartbeat(state, "trainer")
            checker.check("zero backlog: trainer %s keeps the empty task slot free" % state,
                          not row()["busy"] and saved() == [])
        heartbeat("running", "trainer", "t-missing")
        checker.check("zero backlog: stale trainer task metadata does not occupy the task slot",
                      not row()["busy"] and saved() == [])
        heartbeat("running", "router", "t-missing")
        checker.check("zero backlog: stale router task metadata alone does not occupy a slot",
                      not row()["busy"] and saved() == [] and client.wait_task(0.1) is None)
        free = row()
        code, accepted = call("POST", "/tasks", trainer_body)
        task_id = accepted.get("task_id")
        delivered = client.wait_task(3)
        checker.check("zero backlog: an empty task slot dispatches immediately during native activity",
                      not free["busy"] and code == 200 and accepted.get("dispatched") is True
                      and delivered is not None and delivered.get("task_id") == task_id
                      and len(saved()) == 1 and saved()[0][2] == trainer_body["message_id"])
        next_body = dict(payload("retry after cancellation"), queue_if_offline=True)
        checker.check("zero backlog: running task refuses before any router heartbeat",
                      busy(next_body, task_id, "router") and client.wait_task(0.1) is None)
        heartbeat("running", "router", "t-missing")
        checker.check("zero backlog: stale heartbeat task_id falls back to the actual running work order",
                      busy(next_body, task_id, "router"))
        with sqlite3.connect(str(db)) as connection:
            connection.execute("UPDATE workers SET busy=0 WHERE name=?", (name,))
        checker.check("zero backlog: running row independently refuses when cached busy=false",
                      not row()["busy"] and busy(next_body, task_id, "router"))
        heartbeat("running", "router", task_id)
        checker.check("zero backlog: accepted exact retry bypasses busy and changed payload conflicts",
                      duplicate(trainer_body, task_id, "running")
                      and call("POST", "/tasks", dict(trainer_body, goal="changed"))[1].get("error")
                      == "message_id_conflict" and client.wait_task(0.1) is None)

        code, pending = call("POST", "/tasks/" + task_id + "/cancel")
        command = client.cancellations.get(timeout=3)
        checker.check("zero backlog: cancellation remains occupied and refuses new requests",
                      code == 202 and pending.get("state") == "cancelling"
                      and command == {"task_id": task_id} and busy(next_body, task_id, "router")
                      and duplicate(trainer_body, task_id, "cancelling")
                      and http("POST", base + "/result", {"name": name, "task_id": task_id,
                              "status": "done", "summary": "candidate during cancellation"}, token)[1].get("state") == "cancelling"
                      and client.wait_task(0.1) is None)
        with sqlite3.connect(str(db)) as connection:
            connection.execute("UPDATE workers SET busy=0 WHERE name=?", (name,))
        checker.check("zero backlog: cancelling row independently refuses when cached busy=false",
                      not row()["busy"] and busy(next_body, task_id, "router"))
        _, cancelled = call("POST", "/tasks/" + task_id + "/cancel-ack", {
            "name": name, "outcome": {"task_id": task_id, "ok": True, "state": "cancelled"}})
        idle = row()
        code, next_task = call("POST", "/tasks", next_body)
        delivered = client.wait_task(3)
        checker.check("zero backlog: cancellation ACK releases worker; refused UUID can be accepted",
                      cancelled.get("state") == "cancelled" and not idle["busy"]
                      and code == 200 and next_task.get("dispatched") is True
                      and delivered is not None and delivered.get("task_id") == next_task.get("task_id")
                      and len(saved()) == 2)
        finish(next_task["task_id"])
        heartbeat("waiting_input", "trainer")
        checker.check("zero backlog: terminal exact retry remains idempotent during a trainer turn",
                      duplicate(next_body, next_task["task_id"], "done")
                      and duplicate(trainer_body, task_id, "cancelled")
                      and client.wait_task(0.1) is None)
        heartbeat()
        code, offline_retry = call("POST", "/tasks", offline_bodies[1])
        delivered = client.wait_task(3)
        checker.check("zero backlog: offline-refused UUID remains reusable when worker is idle",
                      code == 200 and delivered is not None
                      and delivered.get("task_id") == offline_retry.get("task_id"))
        finish(offline_retry["task_id"])

        bodies = [payload("concurrent-%d" % index) for index in range(8)]
        gate = threading.Barrier(len(bodies))
        results = []

        def create(body):
            gate.wait(timeout=5)
            results.append((body, *call("POST", "/tasks", body)))

        writers = [threading.Thread(target=create, args=(body,), daemon=True) for body in bodies]
        before_count = len(saved())
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(6)
        winners = [(body, result) for body, code, result in results if code == 200]
        delivered = client.wait_task(3)
        if not checker.check("zero backlog: eight distinct concurrent UUIDs have one dispatch winner",
                             all(not writer.is_alive() for writer in writers) and len(results) == 8
                             and len(winners) == 1 and len(saved()) == before_count + 1
                             and all(code == 200 or (code == 409 and result.get("error") == "worker_busy"
                                     and result.get("task_id") == winners[0][1].get("task_id")
                                     and result.get("turn_source") == "router"
                                     and result.get("retry_when") == "current_task_accepted_and_worker_idle")
                                     for _, code, result in results)
                             and delivered is not None
                             and delivered.get("task_id") == winners[0][1].get("task_id")
                             and client.wait_task(0.2) is None,
                             "statuses=%s" % [code for _, code, _ in results]):
            raise RuntimeError("zero-backlog concurrent admission failed")
        keys = {key for _, _, key in saved()}
        losers = [body for body, code, _ in results if code == 409]
        checker.check("zero backlog: all concurrent losers leave UUIDs free and queued count at zero",
                      all(body["message_id"] not in keys for body in losers)
                      and row()["queued"] == 0
                      and call("GET", "/tasks?state=queued")[1].get("count") == 0)
        winner_body, winner = winners[0]
        checker.check("zero backlog: concurrent winner exact retry returns original without redispatch",
                      duplicate(winner_body, winner["task_id"], "running")
                      and client.wait_task(0.1) is None)
        finish(winner["task_id"])
        code, reused = call("POST", "/tasks", losers[0])
        delivered = client.wait_task(3)
        checker.check("zero backlog: concurrent loser UUID succeeds after current task completes",
                      code == 200 and delivered is not None
                      and delivered.get("task_id") == reused.get("task_id"))
        client.stop()
        deadline = time.time() + 5
        while row()["online"] and time.time() < deadline:
            time.sleep(0.05)
        checker.check("zero backlog: disconnect retains accepted queued task and exact retry identity",
                      not row()["online"] and duplicate(losers[0], reused["task_id"], "queued")
                      and row()["queued"] == 1)
        before = saved()
        fresh = dict(payload("offline with recovered backlog"), queue_if_offline=True)
        checker.check("zero backlog: offline recovered backlog refuses with original task_id and retry_when",
                      busy(fresh, reused["task_id"], "router") and saved() == before)

        proc.terminate()
        proc.wait(timeout=5)
        output.close()
        proc, output = start_process(argv + ["--backlog-max", "0"], work / "zero-backlog-restarted.out")
        if not wait_http(base + "/health"):
            raise RuntimeError("explicit-zero router did not start: " + tail(log_path))
        checker.check("zero backlog: explicit --backlog-max 0 starts and preserves accepted queued identity",
                      call("GET", "/health")[1].get("backlog_max") == 0
                      and duplicate(losers[0], reused["task_id"], "queued") and saved() == before)
        recovered = connect()
        replay = recovered.wait_task(3)
        checker.check("zero backlog: reconnect delivers only previously accepted task; no refused task appears",
                      replay is not None and replay.get("task_id") == reused["task_id"]
                      and recovered.wait_task(0.2) is None and saved()[-1][0] == reused["task_id"]
                      and all(key != fresh["message_id"] for _, _, key in saved()))
        finish(reused["task_id"])
        checker.check("zero backlog: final worker is idle with no queued/running/cancelling tasks",
                      not row()["busy"] and row()["queued"] == 0
                      and all(state in ("done", "cancelled") for _, state, _ in saved()))
    finally:
        for client in clients:
            client.stop()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        output.close()
        if checker.failed:
            print("\n--- zero-backlog log (tail) ---\n" + tail(log_path), flush=True)


def dispatch_race_selftest(checker, work):
    """Inject connection loss deterministically into the real HTTP handler."""
    import server as router_module

    store = router_module.RouterStore(str(work / "dispatch-race.db"))
    notifier = mock.Mock()
    notifier.notify_result.return_value = False
    notifier.post_task_notice.return_value = (False, {})
    server = router_module.RouterServer(("127.0.0.1", 0), store, "race-test-token",
                                        notifier, mock.Mock(), backlog_max=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    name = "dispatch-race"
    store.upsert_worker(name)
    conn = server.attach(name)
    try:
        checker.check("zero backlog: RouterServer explicit capacity 0",
                      server.backlog_max == 0)
        for queue_if_offline in (False, True):
            body = {"worker": name, "goal": "push race", "message_id": str(uuid.uuid4()),
                    "queue_if_offline": queue_if_offline}
            with mock.patch.object(conn, "send_task", return_value=False):
                code, result = worker_http("POST", base + "/tasks", body, server.token)
            checker.check("zero backlog: failed push (queue_if_offline=%s) deletes task and UUID" % queue_if_offline,
                          code == 409 and result.get("error") == "worker_offline"
                          and store.list_tasks() == [] and store.get_task_request(body["message_id"]) is None
                          and not store.get_worker(name)["busy"] and store.queued_count(name) == 0)
        with mock.patch.object(server, "current_conn", side_effect=[conn, None]):
            code, result = worker_http("POST", base + "/tasks", body, server.token)
        checker.check("zero backlog: vanished connection before push also leaves no task or UUID",
                      code == 409 and result.get("error") == "worker_offline"
                      and store.list_tasks() == [] and store.get_task_request(body["message_id"]) is None)
        code, accepted = worker_http("POST", base + "/tasks", body, server.token)
        event = conn.queue.get(timeout=3)
        checker.check("zero backlog: UUID refused by push race can be accepted on retry",
                      code == 200 and accepted.get("dispatched") is True
                      and event.get("data", {}).get("task_id") == accepted.get("task_id")
                      and len(store.list_tasks()) == 1 and store.queued_count(name) == 0)

        worker_http("POST", base + "/result", {"name": name, "task_id": accepted["task_id"],
             "status": "done", "summary": "prepare queued recovery race"}, server.token)
        server.backlog_max = 2
        server.detach(conn, "selftest offline")
        old_bodies = [{"worker": name, "goal": "accepted queued work %d" % index,
                       "message_id": str(uuid.uuid4()), "queue_if_offline": True} for index in range(2)]
        old_results = [worker_http("POST", base + "/tasks", old_body, server.token)
                       for old_body in old_bodies]
        old_ids = [task.get("task_id") for _, task in old_results]
        checker.check("queued recovery: explicit positive capacity accepts two legitimate old tasks",
                      all(code == 202 for code, _ in old_results)
                      and None not in old_ids and store.queued_count(name) == 2)
        with mock.patch.object(server, "push", return_value=False):
            conn = server.attach(name)
        store.heartbeat(name, state="idle", turn_source=None, task_id=old_ids[1])
        with mock.patch.object(conn, "send_task", return_value=False):
            dispatched = server.dispatch_one(name)
        checker.check("queued recovery: failed recovery push leaves old work queued on an idle live connection",
                      dispatched is None and server.online(name) and store.get_worker(name)["busy"]
                      and store.queued_count(name) == 2 and store.blocking_task(name) is None)
        new_body = {"worker": name, "goal": "new work must follow accepted work",
                    "message_id": str(uuid.uuid4())}
        code, full = worker_http("POST", base + "/tasks", new_body, server.token)
        checker.check("queued recovery: positive full capacity also prevents immediate FIFO bypass",
                      code == 429 and full.get("error") == "backlog_full"
                      and store.get_task_request(new_body["message_id"]) is None and conn.queue.empty())

        server.backlog_max = 0
        store.heartbeat(name, state="running", turn_source="trainer", task_id=None)
        before = store.list_tasks()
        contenders = [dict(new_body, message_id=str(uuid.uuid4())) for _ in range(8)]
        gate = threading.Barrier(len(contenders))
        results = []

        def create(request):
            gate.wait(timeout=5)
            results.append(worker_http("POST", base + "/tasks", request, server.token))

        writers = [threading.Thread(target=create, args=(request,), daemon=True) for request in contenders]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(6)
        checker.check("queued recovery: concurrent new requests refuse with oldest accepted task_id and retry_when",
                      all(not writer.is_alive() for writer in writers) and len(results) == len(contenders)
                      and all(code == 409 and result.get("error") == "worker_busy"
                              and result.get("task_id") == old_ids[0] and result.get("turn_source") == "router"
                              and result.get("retry_when") == "current_task_accepted_and_worker_idle"
                              for code, result in results)
                      and store.list_tasks() == before and conn.queue.empty()
                      and all(store.get_task_request(request["message_id"]) is None for request in contenders))
        code, duplicate = worker_http("POST", base + "/tasks", old_bodies[0], server.token)
        checker.check("queued recovery: exact accepted UUID retry bypasses admission refusal without dispatch",
                      code == 200 and duplicate.get("duplicate") is True
                      and duplicate.get("task_id") == old_ids[0] and duplicate.get("state") == "queued"
                      and conn.queue.empty())
        recovered = server.dispatch_one(name)
        dispatched = store.get_task(old_ids[0])
        event = conn.queue.get(timeout=3)
        checker.check("queued recovery: oldest accepted task resumes before any new work",
                      recovered["task_id"] == old_ids[0] and dispatched["state"] == "running"
                      and store.get_task(old_ids[1])["state"] == "queued"
                      and event.get("data", {}).get("task_id") == old_ids[0]
                      and conn.queue.empty())
        code, result = worker_http("POST", base + "/result", {
            "name": name, "task_id": old_ids[0], "status": "done", "summary": "oldest completed"}, server.token)
        event = conn.queue.get(timeout=3)
        checker.check("queued recovery: completing oldest task automatically dispatches the next accepted task",
                      code == 200 and result.get("dispatched_next") == old_ids[1]
                      and event.get("data", {}).get("task_id") == old_ids[1])
        worker_http("POST", base + "/result", {"name": name, "task_id": old_ids[1],
             "status": "done", "summary": "old queue drained"}, server.token)
        code, admitted = worker_http("POST", base + "/tasks", contenders[0], server.token)
        event = conn.queue.get(timeout=3)
        checker.check("queued recovery: refused UUID succeeds once accepted old work completes",
                      code == 200 and admitted.get("dispatched") is True
                      and event.get("data", {}).get("task_id") == admitted.get("task_id")
                      and store.queued_count(name) == 0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        store._conn.close()


def one_backlog_selftest(checker, work):
    """Real HTTP/curl admission, TTL and heartbeat recovery for the default slot."""
    import server as router_module

    store = router_module.RouterStore(str(work / "one-backlog.db"))
    notifier = mock.Mock()
    notifier.notify_result.return_value = True
    notifier.post_task_notice.return_value = (True, {"id": "isolated-notice"})
    server = router_module.RouterServer(("127.0.0.1", 0), store, "one-backlog-test-token",
                                        notifier, mock.Mock())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    name = "one-wait-worker"
    store.upsert_worker(name)
    conn = server.attach(name)

    def call(method, route, body=None):
        return worker_http(method, base + route, body, server.token)

    def payload(goal, **extra):
        return dict(worker=name, goal=goal, message_id=str(uuid.uuid4()), **extra)

    def finish(task_id):
        return call("POST", "/result", {"name": name, "task_id": task_id,
                    "status": "done", "summary": "one-slot test complete"})

    def curl_create(body):
        cmd = ["curl", "--silent", "--show-error", "--max-time", "5",
               "--request", "POST", base + "/tasks", "--header",
               "X-Router-Token: " + server.token, "--header", "Content-Type: application/json",
               "--data-binary", json.dumps(body), "--write-out", "\nHTTP_STATUS=%{http_code}\n"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True)
        print("CURL " + " ".join(cmd) + "\n" + result.stdout, flush=True)
        raw, status = result.stdout.strip().rsplit("\nHTTP_STATUS=", 1)
        return int(status), json.loads(raw)

    try:
        args = router_module.parse_args([])
        health = call("GET", "/health")[1]
        checker.check("one backlog: CLI and constructor default to one slot and six-hour queue TTL",
                      args.backlog_max == server.backlog_max == health["backlog_max"] == 1
                      and args.queue_ttl_s == server.queue_ttl_s == health["queue_ttl_s"] == 21600)
        active_body = payload("running task has no queue timeout", queue_ttl_s=0.05)
        code, active = call("POST", "/tasks", active_body)
        active_id = active["task_id"]
        checker.check("one backlog: idle starts one task immediately",
                      code == 200 and active["state"] == "running"
                      and conn.queue.get(timeout=3)["data"]["task_id"] == active_id)
        second_body = payload("second waiter is refused", queue_if_offline=True)
        code, second = curl_create(second_body)
        checker.check("one backlog: real curl refuses a new task while the current task is running",
                      code == 409 and second["error"] == "worker_busy"
                      and second["task_id"] == active_id
                      and second["retry_when"] == "current_task_accepted_and_worker_idle"
                      and store.get_task_request(second_body["message_id"]) is None
                      and store.queued_count(name) == 0)
        finish(active_id)
        server.detach(conn, "selftest offline")
        first_body = payload("watcher occupies the only waiting slot", queue_if_offline=True)
        code, first = curl_create(first_body)
        checker.check("one backlog: real curl accepts one offline waiting task",
                      code == 200 and first["queued"] is True and first["state"] == "queued"
                      and first["queued_ahead"] == 0 and store.queued_count(name) == 1)
        code, duplicate = call("POST", "/tasks", first_body)
        checker.check("one backlog: full-slot exact retry returns the same queued task",
                      code == 200 and duplicate["duplicate"] and duplicate["task_id"] == first["task_id"])
        conn = server.attach(name)
        checker.check("one backlog: reconnect dispatches the sole watcher exactly once",
                      conn.queue.get(timeout=3)["data"]["task_id"] == first["task_id"]
                      and store.get_task(first["task_id"])["state"] == "running" and store.queued_count(name) == 0
                      and conn.queue.empty())
        finish(first["task_id"])

        server.detach(conn, "selftest offline")
        expiring_body = payload("unstarted watcher expires", queue_ttl_s=0.08, queue_if_offline=True)
        code, expiring = call("POST", "/tasks", expiring_body)
        time.sleep(0.12)
        sweep = router_module.ApprovalSweeper(store, interval=1, server=server)
        sweep.start()
        try:
            until = time.monotonic() + 3
            while store.get_task(expiring["task_id"])["state"] != "failed" and time.monotonic() < until:
                time.sleep(0.02)
            expired = store.get_task(expiring["task_id"])["state"] == "failed"
        finally:
            sweep.stop()
            sweep.join(3)
        task = store.get_task(expiring["task_id"])
        checker.check("queue TTL: sweeper expires an unstarted task and clears busy",
                      code == 200 and expired and task["result"]["error"] == "queue_expired"
                      and task["started_at"] is None and store.queued_count(name) == 0
                      and not store.get_worker(name)["busy"])
        checker.check("queue TTL: expiration sends exactly one failure notification",
                      sum(c.args[0]["task_id"] == task["task_id"] for c in notifier.notify_result.call_args_list) == 1
                      and server.expire_queued_tasks() == 0)
        code, duplicate = call("POST", "/tasks", expiring_body)
        checker.check("queue TTL: retry of expired UUID returns its terminal task without replay",
                      code == 200 and duplicate["duplicate"] and duplicate["state"] == "failed")
        code, replacement = call("POST", "/tasks", second_body)
        checker.check("queue TTL: earlier refused UUID can fill the freed slot",
                      code == 200 and replacement["state"] == "queued")
        conn = server.attach(name)
        conn.queue.get(timeout=3)
        finish(replacement["task_id"])

        for invalid in (0, -1, True, None, "bad", "nan", "inf"):
            code, _ = call("POST", "/tasks", payload("invalid queue TTL", queue_ttl_s=invalid))
            checker.check("queue TTL: invalid per-task value %r is rejected" % invalid, code == 400)
        for invalid in ("0", "-1", "nan", "inf"):
            proc = subprocess.run([PY, str(ROUTER), "--queue-ttl-s", invalid,
                                   "--log", str(work / "bad-ttl.log")],
                                  capture_output=True, text=True, timeout=10)
            checker.check("queue TTL: invalid service value %s is rejected before startup" % invalid,
                          proc.returncode == 2 and "--queue-ttl-s must be finite and > 0" in proc.stderr)

        code, active = call("POST", "/tasks", payload("long native task", queue_ttl_s=0.01))
        active_id = active["task_id"]
        conn.queue.get(timeout=3)
        time.sleep(0.02)
        server.expire_queued_tasks()
        checker.check("queue TTL: executing task remains running after its queue TTL",
                      store.get_task(active_id)["state"] == "running")
        watcher = store.create_task(name, "previously accepted watcher survives router recovery")
        store.reset_runtime()
        server.expire_queued_tasks()
        checker.check("queue TTL: previously dispatched recovery task cannot expire",
                      store.get_task(active_id)["state"] == "queued")
        call("POST", "/heartbeat", {"name": name, "state": "inProgress",
             "turn_source": "router", "task_id": active_id})
        checker.check("one backlog: matching native heartbeat restores running task, watcher stays queued",
                      store.get_task(active_id)["state"] == "running"
                      and store.get_task(watcher["task_id"])["state"] == "queued"
                      and store.blocking_task(name)["task_id"] == active_id and store.queued_count(name) == 1
                      and conn.queue.empty())
        call("POST", "/heartbeat", {"name": name, "state": "inProgress",
             "turn_source": "router", "task_id": watcher["task_id"]})
        checker.check("one backlog: heartbeat cannot promote a never-dispatched watcher",
                      store.get_task(watcher["task_id"])["state"] == "queued")
        call("POST", "/heartbeat", {"name": name, "state": "inProgress",
             "turn_source": "router", "task_id": active_id})
        call("POST", "/tasks/%s/cancel" % watcher["task_id"])
        finish(active_id)
        server.detach(conn, "selftest offline")

        gate = threading.Barrier(8)
        results = []

        def create():
            gate.wait(timeout=5)
            results.append(call("POST", "/tasks", payload("concurrent waiter", queue_if_offline=True)))

        writers = [threading.Thread(target=create, daemon=True) for _ in range(8)]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(6)
        checker.check("one backlog: concurrent eight waiters have exactly one queued winner and seven 409s",
                      len(results) == 8 and sum(code == 200 for code, _ in results) == 1
                      and sum(code == 409 and data["error"] == "worker_busy" for code, data in results) == 7
                      and store.queued_count(name) == 1)
        winner = next(data for code, data in results if code == 200)
        conn = server.attach(name)
        conn.queue.get(timeout=3)
        finish(winner["task_id"])

        expired = store.create_task(name, "expires before dispatch", queue_ttl_s=0.01)
        time.sleep(0.02)
        call("POST", "/heartbeat", {"name": name, "state": "idle", "turn_source": None})
        checker.check("queue TTL: dispatch boundary expires old waiter instead of executing it",
                      store.get_task(expired["task_id"])["state"] == "failed"
                      and conn.queue.empty() and not store.get_worker(name)["busy"])
        legacy = store.create_task(name, "old unstarted row")
        with store._lock:
            store._conn.execute("UPDATE tasks SET queue_expires_at=NULL, created_at=? WHERE task_id=?",
                                (time.time() - server.queue_ttl_s - 1, legacy["task_id"]))
        with mock.patch.object(conn, "send_task", return_value=False):
            code, admitted = call("POST", "/tasks", payload("admission sweeps old queue"))
        checker.check("queue TTL: admission expires legacy unstarted row and admits replacement",
                      code == 200 and admitted["state"] == "queued"
                      and store.get_task(legacy["task_id"])["result"]["error"] == "queue_expired"
                      and store.queued_count(name) == 1 and store.get_worker(name)["busy"])
        rejected = store.get_task(admitted["task_id"])
        expired = store.expire_queued_tasks(server.queue_ttl_s, now=rejected["queue_expires_at"] + 1)
        checker.check("queue TTL: known failed SSE delivery retains TTL eligibility",
                      rejected["state"] == "queued" and rejected["dispatch_attempted"] is False
                      and any(task["task_id"] == rejected["task_id"] for task in expired))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        store.close()


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
        connections[name] = server.attach(name)
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
        status, hb = worker_http("POST", BASE + "/heartbeat",
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
        status, result = worker_http("POST", BASE + "/result", {
            "name": "波旁", "task_id": task1, "status": "done",
            "summary": "hello printed", "artifacts": ["/tmp/hello.txt"],
            "evidence": ["exit=0"],
        }, token)
        checker.check(
            "worker result + idle -> done, notified=true",
            status == 200 and result.get("state") == "done"
            and result.get("notified") is True,
            "status=%s body=%s" % (status, result))

        status, got = worker_http("GET", BASE + "/tasks/" + task1, token=token)
        checker.check(
            "GET /tasks/{id} -> state done + summary",
            status == 200 and got.get("state") == "done"
            and (got.get("task") or {}).get("result", {}).get("summary")
            == "hello printed",
            "status=%s body=%s" % (status, got))

        status, listing = worker_http(
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
            "SSE-dispatched task result reached trigger: source=任务完成 kind=task.result "
            "level=event actor=波旁 event_id=<id>:done",
            event_row is not None and event_row[1] == "任务完成"
            and event_row[2] == "task.result" and event_row[3] == "event"
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

        status, refused = worker_http("POST", BASE + "/tasks",
                               {"worker": "波旁", "goal": "offline-1"}, token)
        checker.check(
            "offline + queue_if_offline=false -> 409 worker_offline",
            status == 409 and refused.get("error") == "worker_offline",
            "status=%s body=%s" % (status, refused))

        status1, off1 = worker_http("POST", BASE + "/tasks", {
            "worker": "波旁", "goal": "offline-1", "queue_if_offline": True,
        }, token)
        status2, off2 = worker_http("POST", BASE + "/tasks", {
            "worker": "波旁", "goal": "offline-2", "queue_if_offline": True,
        }, token)
        checker.check(
            "offline + queue_if_offline=true -> 202 queued with queued_ahead",
            status1 == 202 and off1.get("queued") is True
            and off1.get("queued_ahead") == 0
            and status2 == 202 and off2.get("queued_ahead") == 1,
            "first=%s/%s second=%s/%s" % (status1, off1, status2, off2))

        status, pending = worker_http("GET", BASE + "/tasks?worker=%s&state=queued"
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
        early = worker2.wait_task(0.2)
        status, _ = worker_http("POST", BASE + "/result", {
            "name": "波旁", "task_id": off1["task_id"], "status": "done",
            "summary": "offline-1 done"}, token)
        second = worker2.wait_task(5.0)
        checker.check(
            "reconnect: backlog delivered one at a time in FIFO order",
            first is not None and early is None and second is not None
            and first.get("task_id") == off1.get("task_id")
            and second.get("task_id") == off2.get("task_id")
            and first.get("goal") == "offline-1"
            and second.get("goal") == "offline-2",
            "first=%s second=%s" % (first and first.get("goal"),
                                    second and second.get("goal")))

        status2, _ = worker_http("POST", BASE + "/result", {
            "name": "波旁", "task_id": off2["task_id"], "status": "done",
            "summary": "offline-2 done"}, token)
        checker.check("backlog tasks report done", status == 200
                      and status2 == 200)

        # ---------------------------------------------------------------- #
        # 4. running task blocks new work until the first result
        # ---------------------------------------------------------------- #
        _, a = worker_http("POST", BASE + "/tasks",
                    {"worker": "波旁", "goal": "busy-A"}, token)
        next_body = {"worker": "波旁", "goal": "busy-B", "message_id": str(uuid.uuid4())}
        status_b, denied = worker_http("POST", BASE + "/tasks", next_body, token)
        got_a = worker2.wait_task(3.0)
        noise_b = worker2.wait_task(1.2)
        checker.check(
            "busy: second task gets HTTP 409, no SSE delivery while first runs",
            a.get("dispatched") is True and status_b == 409
            and denied.get("error") == "worker_busy" and denied.get("task_id") == a.get("task_id")
            and got_a is not None and got_a.get("task_id") == a.get("task_id")
            and noise_b is None,
            "A=%s B=%s got_a=%s early_b=%s"
            % (a.get("state"), denied.get("error"),
               got_a and got_a.get("goal"), noise_b and noise_b.get("goal")))

        _, body_a = worker_http("POST", BASE + "/result", {
            "name": "波旁", "task_id": a["task_id"], "status": "done",
            "summary": "A done"}, token)
        code_b, b = worker_http("POST", BASE + "/tasks", next_body, token)
        dispatched_b = worker2.wait_task(5.0)
        checker.check(
            "busy: the refused UUID dispatches after the first result",
            body_a.get("state") == "done" and code_b == 200 and b.get("dispatched") is True
            and dispatched_b is not None
            and dispatched_b.get("task_id") == b.get("task_id"),
            "result=%s delivered=%s" % (body_a.get("state"),
                                        dispatched_b and dispatched_b.get("goal")))
        worker_http("POST", BASE + "/result", {
            "name": "波旁", "task_id": b["task_id"], "status": "done",
            "summary": "B done"}, token)
        worker2.stop()
        if not wait_online("波旁", token, False):
            raise RuntimeError("worker did not go offline before queued cancellation")

        # ---------------------------------------------------------------- #
        # 5. cancel a queued task: cancelled and never delivered
        # ---------------------------------------------------------------- #
        _, c = worker_http("POST", BASE + "/tasks",
                    {"worker": "波旁", "goal": "cancel-C", "queue_if_offline": True}, token)
        status, cancelled = worker_http(
            "POST", BASE + "/tasks/%s/cancel" % c["task_id"], None, token)
        worker2 = FakeWorker("波旁", token)
        workers.append(worker2)
        worker2.start()
        if not wait_online("波旁", token, True):
            raise RuntimeError("worker did not reconnect after queued cancellation")
        stray = worker2.wait_task(0.8)
        status_get, got_c = worker_http("GET", BASE + "/tasks/" + c["task_id"],
                                 token=token)
        checker.check(
            "cancel queued -> cancelled, never delivered",
            c.get("queued") is True and status == 200
            and cancelled.get("state") == "cancelled" and stray is None
            and status_get == 200 and got_c.get("state") == "cancelled",
            "cancel=%s stray=%s get=%s" % (status, stray and stray.get("goal"),
                                           got_c.get("state")))

        # ---------------------------------------------------------------- #
        # 6. cancel a running task; a late result is ignored
        # ---------------------------------------------------------------- #
        _, d = worker_http("POST", BASE + "/tasks",
                    {"worker": "波旁", "goal": "cancel-D"}, token)
        got_d = worker2.wait_task(3.0)
        status, cancel_d = worker_http(
            "POST", BASE + "/tasks/%s/cancel" % d["task_id"], None, token)
        pending_row = worker_row("波旁", token)
        _, retained = worker_http("POST", BASE + "/tasks", {"worker": "波旁", "goal": "blocked by cancel"}, token)
        checker.check("undelivered cancel retains task occupancy through the cancellation gate",
                      status == 202 and cancel_d.get("state") == "cancelling"
                      and cancel_d.get("ok") is False
                      and pending_row.get("busy") is False and retained.get("error") == "worker_busy")
        _, acknowledged = worker_http("POST", BASE + "/tasks/%s/cancel-ack" % d["task_id"], {
            "name": "波旁", "outcome": {"ok": True, "task_id": d["task_id"], "state": "cancelled"}}, token)
        status_late, late = worker_http("POST", BASE + "/result", {
            "name": "波旁", "task_id": d["task_id"], "status": "done",
            "summary": "late"}, token)
        row = worker_row("波旁", token)
        checker.check(
            "cancel running -> cancelled, busy freed, late result ignored",
            acknowledged.get("state") == "cancelled"
            and got_d is not None and got_d.get("task_id") == d["task_id"]
            and status_late == 200 and late.get("duplicate") is True
            and row is not None and row.get("busy") is False,
            "cancel=%s late=%s busy=%s" % (status, late, row and row.get("busy")))

        # ---------------------------------------------------------------- #
        # 7. backlog cap 128 for an offline worker
        # ---------------------------------------------------------------- #
        status, _ = worker_http("POST", BASE + "/register", {"name": "积压工"}, token)
        accepted = 0
        last_ahead = None
        first_bad = None
        for index in range(128):
            code, body = worker_http("POST", BASE + "/tasks", {
                "worker": "积压工", "goal": "backlog-%03d" % index,
                "queue_if_offline": True,
            }, token)
            if code == 202:
                accepted += 1
                last_ahead = body.get("queued_ahead")
            else:
                first_bad = (code, body)
                break
        code, overflow = worker_http("POST", BASE + "/tasks", {
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

        status, reg2 = worker_http("POST", BASE + "/register",
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

        worker2.stop()
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
        # 9. final shape / counts
        # ---------------------------------------------------------------- #
        status, health = worker_http("GET", BASE + "/health")
        checker.check(
            "GET /health final: workers=2, tasks=135 (queued 128)",
            status == 200 and health.get("workers") == 2
            and health.get("tasks") == 135
            and health.get("tasks_by_state", {}).get("queued") == 128,
            "status=%s body=%s" % (status, health))

        conn = sqlite3.connect(str(trigger_db))
        try:
            terminal_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE source IN ('任务完成','任务失败','任务取消')"
                " AND kind='task.result' AND level=CASE WHEN source='任务取消'"
                " THEN 'notify' ELSE 'event' END").fetchone()[0]
        finally:
            conn.close()
        checker.check(
            "trigger has exactly 5 task.result rows at the expected levels",
            terminal_count == 5, "count=%s" % terminal_count)

        _, tasks = worker_http("GET", BASE + "/tasks?worker=%s&limit=500"
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

        control_selftest(checker, token, workers)
        activity_selftest(checker, token, workers)
        peer_selftest(checker, work, trigger_url, trigger_token, trigger_db)
        worker_names_selftest(checker, work, trigger_url, trigger_token, trigger_db)
        task_notice_selftest(checker, work, trigger_url, trigger_token, trigger_db)
        workorders_selftest(checker, work, trigger_url, trigger_token)
        zero_backlog_selftest(checker, work, trigger_url, trigger_token)
        dispatch_race_selftest(checker, work)
        one_backlog_selftest(checker, work)
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
