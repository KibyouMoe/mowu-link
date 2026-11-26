"""02A loopback development transport. No ambient proxies, credentials or live services."""
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from . import delivery, pool
from .orders import task, worker_actor
from .storage import Fault, encode, require


def route(orders, actor, method, path, body, headers):
    url = urlsplit(path)
    parts = url.path.removeprefix("/v1/").split("/")
    require(path.startswith("/v1/"), 404, "unknown_route")
    orders.tick()
    if method == "GET" and parts in (["tasks"], ["workers"]):
        require(actor == "hermes", 403, "forbidden")
        query = parse_qs(url.query, keep_blank_values=True)
        require(all(len(v) == 1 for v in query.values()), 422, "duplicate_filter")
        with orders.db.transaction() as db:
            page = orders.list_records(db, parts[0], {k: v[0] for k, v in query.items()})
        return 200, "application/json", page
    if method == "GET" and parts == ["deliveries", "stream"]:
        cursor = headers.get("Last-Event-ID", "0")
        require(re.fullmatch(r"[0-9]{1,15}", cursor), 422, "invalid_cursor")
        with orders.db.transaction() as db:
            if actor.startswith("worker/"):
                wid = actor.split("/", 1)[1]
                row = db.execute("SELECT * FROM workers WHERE worker_id=?", (wid,)).fetchone()
                require(row, 409, "missing_session")
                orders.query(db, row)
            frames = delivery.read(db, actor, int(cursor))
        return 200, "text/event-stream", "".join(
            f"id: {frame['cursor']}\ndata: {encode(frame)}\n\n" for frame in frames).encode()
    if method == "GET" and len(parts) == 2 and parts[0] == "tasks":
        require(actor == "hermes", 403, "forbidden")
        with orders.db.transaction() as db:
            return 200, "application/json", orders.details(db, parts[1])

    status = 200
    match method, parts:
        case "POST", ["deliveries", did, "ack"]:
            def execute(db):
                return delivery.acknowledge(db, actor, did, body.get("cursor"))
        case "POST", ["tasks"]:
            require(actor == "hermes", 403, "forbidden")
            status = 201
            execute = lambda db: orders.create(db, body)
        case "POST", ["tasks", tid, "actions"]:
            require(actor == "hermes", 403, "forbidden")
            execute = lambda db: orders.act(db, tid, body)
        case "PATCH", ["tasks", tid]:
            require(actor == "hermes", 403, "forbidden")
            execute = lambda db: orders.update(db, tid, body)
        case "PUT", ["workers", wid, op] if op in ("session", "presence"):
            require(actor == worker_actor(wid), 403, "forbidden")
            handler = orders.session if op == "session" else orders.presence
            execute = lambda db: handler(db, wid, body)
        case "POST", ["tasks", tid, "materials"]:
            require(actor.startswith("worker/"), 403, "forbidden")
            def execute(db):
                wid = actor.split("/", 1)[1]
                orders.active_session(db, wid, body)
                return pool.candidate(db, task(db, tid), wid, body, orders.clock())
        case "POST", ["tasks", tid, op] if op in ("accept", "report"):
            require(actor.startswith("worker/"), 403, "forbidden")
            wid = actor.split("/", 1)[1]
            handler = orders.accept if op == "accept" else orders.report
            execute = lambda db: handler(db, wid, tid, body)
        case _:
            raise Fault(404, "unknown_route")
    result = orders.write(actor, headers.get("Idempotency-Key"), [method, path], body, execute)
    return status, "application/json", result


def serve_loopback(orders, identities):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            try:
                auth = self.headers.get("Authorization", "")
                actor = identities.get(auth.removeprefix("Bearer ")) if auth.startswith("Bearer ") else None
                require(actor, 401, "unauthenticated")
                body = {}
                if self.command != "GET":
                    length = self.headers.get("Content-Length", "")
                    require(length.isdecimal() and 0 < int(length) <= 1024 * 1024, 422, "invalid_length")
                    body = json.loads(self.rfile.read(int(length)))
                    require(isinstance(body, dict), 422, "invalid_body")
                status, content_type, response = route(orders, actor, self.command, self.path, body, self.headers)
            except (ValueError, UnicodeError):
                error = Fault(422, "invalid_json")
            except Fault as exc:
                error = exc
            else:
                error = None
            if error:
                status, content_type = error.status, "application/json"
                response = {"error": {"code": error.code, "message": str(error), "retryable": error.retryable}}
            raw = response if isinstance(response, bytes) else encode(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_PATCH = handle_request

    class Server(ThreadingHTTPServer):
        next_tick = 0

        def service_actions(self):
            super().service_actions()
            if time.monotonic() >= self.next_tick:
                orders.tick()
                self.next_tick = time.monotonic() + 1

    return Server(("127.0.0.1", 0), Handler)


class Client:
    def __init__(self, url, token):
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.path:
            raise ValueError("development Router must be loopback")
        self.url, self.token = url, token
        self.http = build_opener(ProxyHandler({}))

    def request(self, method, path, body=None, key=None, headers=None):
        hdr = {"Authorization": "Bearer " + self.token, **(headers or {})}
        raw = None
        if body is not None:
            hdr["Content-Type"] = "application/json"
            raw = encode(body).encode()
        if key:
            hdr["Idempotency-Key"] = key
        req = Request(self.url + "/v1/" + path, data=raw, method=method, headers=hdr)
        try:
            with self.http.open(req, timeout=5) as response:
                raw = response.read()
                if response.headers.get_content_type() == "text/event-stream":
                    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b"data: ")]
                return json.loads(raw)
        except HTTPError as exc:
            with exc:
                error = json.load(exc)["error"]
            raise Fault(exc.code, error["code"], error["retryable"]) from exc

    def frames(self, cursor):
        return self.request("GET", "deliveries/stream", headers={"Accept": "text/event-stream",
                                                                "Last-Event-ID": str(cursor)})

    def ack(self, frame):
        return self.request("POST", f"deliveries/{frame['delivery_id']}/ack", {"cursor": frame["cursor"]},
                            "ack-" + frame["delivery_id"])
