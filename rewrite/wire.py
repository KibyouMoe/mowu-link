"""02A loopback development transport. No ambient proxies, credentials or live services."""
import json
import re
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from . import attachments, delivery, links, messages, peers, pool, questions
from .orders import task, worker_actor
from .storage import Fault, encode, require


def route(orders, actor, method, path, body, headers):
    url = urlsplit(path)
    parts = url.path.removeprefix("/v1/").split("/")
    require(path.startswith("/v1/"), 404, "unknown_route")
    orders.tick()
    if parts[0] == "uploads":
        require(actor.startswith(("trigger/", "worker/")), 403, "forbidden")
        if parts == ["uploads"] and method == "POST":
            result = orders.write(actor, headers.get("Idempotency-Key"), [method, path], body,
                                  lambda db: attachments.begin(db, actor, body))
            return 201, "application/json", result
        if len(parts) == 2 and method in ("GET", "PATCH"):
            with orders.db.transaction() as db:
                if method == "GET":
                    result = attachments.progress(attachments.owned(db, actor, parts[1]))
                else:
                    result = attachments.append(db, actor, parts[1], headers.get("Upload-Offset"), body)
            return 200, "application/json", result
    if len(parts) == 2 and parts[0] == "attachments" and method == "GET":
        with orders.db.transaction() as db:
            row = attachments.authorize(db, actor, parts[1])
            result = attachments.Download(orders.db, row, headers.get("Range"))
        return (206 if "Content-Range" in result.headers else 200), "application/octet-stream", result
    if method == "GET" and parts in (["tasks"], ["workers"], ["questions"], ["messages"]):
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
        case "POST", ["messages"]:
            require(actor.startswith("trigger/"), 403, "forbidden")
            execute = lambda db: messages.submit(db, actor, body, orders.clock())
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
        case "PUT", ["workers", wid, "links", "adapter"]:
            require(actor == worker_actor(wid),403,"forbidden")
            def execute(db):
                orders.active_session(db,wid,body)
                return links.report(db,wid,body,orders.clock())
        case "POST", ["tasks", tid, "materials"]:
            require(actor.startswith("worker/"), 403, "forbidden")
            def execute(db):
                wid = actor.split("/", 1)[1]
                orders.active_session(db, wid, body)
                return pool.candidate(db, task(db, tid), wid, body, orders.clock())
        case "POST", ["peers", target, "messages"]:
            require(actor.startswith("worker/"), 403, "forbidden")
            def execute(db):
                sender = actor.split("/", 1)[1]
                orders.active_session(db,sender,body)
                return peers.submit(db,sender,target,body,orders.clock())
        case "POST", ["tasks", tid, "questions"]:
            require(actor.startswith("worker/"), 403, "forbidden")
            def execute(db):
                wid = actor.split("/", 1)[1]
                orders.active_session(db, wid, body)
                return questions.ask(db, task(db, tid), wid, body, orders.clock())
        case "PUT", ["questions", qid, "answer"]:
            require(actor == "hermes", 403, "forbidden")
            execute = lambda db: questions.answer(db, qid, body)
        case "POST", ["tasks", tid, op] if op in ("accept", "report"):
            require(actor.startswith("worker/"), 403, "forbidden")
            wid = actor.split("/", 1)[1]
            handler = orders.accept if op == "accept" else orders.report
            execute = lambda db: handler(db, wid, tid, body)
        case _:
            raise Fault(404, "unknown_route")
    result = orders.write(actor, headers.get("Idempotency-Key"), [method, path], body, execute)
    return status, "application/json", result


def serve_router(orders, identities, address, tls_context=None):
    def handle(actor, method, path, body, headers):
        return route(orders, actor, method, path, body, headers)

    return serve_authenticated(identities, address, tls_context, handle, orders.tick)


def serve_authenticated(identities, address, tls_context, handle, tick=None):
    if address[0] != "127.0.0.1" and tls_context is None:
        raise ValueError("a network listener requires TLS")
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
                    data = self.rfile.read(int(length))
                    if self.command == "PATCH" and urlsplit(self.path).path.startswith(("/v1/uploads/","/v1/worker/media/uploads/")):
                        require(self.headers.get_content_type() == "application/octet-stream", 415, "invalid_media_type")
                        body = data
                    else:
                        body = json.loads(data)
                        require(isinstance(body, dict), 422, "invalid_body")
                status, content_type, response = handle(actor, self.command, self.path, body, self.headers)
            except (ValueError, UnicodeError):
                error = Fault(422, "invalid_json")
            except Fault as exc:
                error = exc
            else:
                error = None
            if error:
                status, content_type = error.status, "application/json"
                response = {"error": {"code": error.code, "message": str(error), "retryable": error.retryable, **error.details}}
            if isinstance(response, attachments.Download):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(response.length))
                self.end_headers()
                try:
                    response.write_to(self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            raw = response if isinstance(response, bytes) else encode(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_PATCH = handle_request

    class Server(ThreadingHTTPServer):
        next_tick = 0

        def finish_request(self, request, client_address):
            request.settimeout(15)
            if tls_context is None:
                return super().finish_request(request, client_address)
            try:
                with tls_context.wrap_socket(request, server_side=True) as secure:
                    super().finish_request(secure, client_address)
            except (ssl.SSLError, TimeoutError, ConnectionResetError):
                return

        def service_actions(self):
            super().service_actions()
            if tick is not None and time.monotonic() >= self.next_tick:
                tick()
                self.next_tick = time.monotonic() + 1

    return Server(address, Handler)


def serve_loopback(orders, identities):
    return serve_router(orders, identities, ("127.0.0.1", 0))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, url, token, ca_file=None):
        parts = urlsplit(url)
        if (not parts.hostname or parts.path or parts.query or parts.fragment or parts.username is not None
                or parts.password is not None or parts.scheme not in ("http","https")
                or (parts.scheme == "http" and parts.hostname != "127.0.0.1")):
            raise ValueError("expected a single HTTPS origin or an isolated HTTP loopback")
        self.url, self.token = url, token
        handlers = [ProxyHandler({}), NoRedirect()]
        if parts.scheme == "https":
            handlers.append(HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
        self.http = build_opener(*handlers)

    def request(self, method, path, body=None, key=None, headers=None, *, max_bytes=None):
        hdr = {"Authorization": "Bearer " + self.token, **(headers or {})}
        raw = None
        if isinstance(body, bytes):
            hdr["Content-Type"] = "application/octet-stream"
            raw = body
        elif body is not None:
            hdr["Content-Type"] = "application/json"
            raw = encode(body).encode()
        if key:
            hdr["Idempotency-Key"] = key
        req = Request(self.url + "/v1/" + path, data=raw, method=method, headers=hdr)
        try:
            with self.http.open(req, timeout=5) as response:
                raw = response.read() if max_bytes is None else response.read(max_bytes+1)
                if max_bytes is not None and len(raw)>max_bytes:
                    raise Fault(409, "oversized_file_response")
                if response.headers.get_content_type() == "application/octet-stream":
                    return {"bytes": raw, "status": response.status, "headers": dict(response.headers)}
                if response.headers.get_content_type() == "text/event-stream":
                    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b"data: ")]
                return json.loads(raw)
        except HTTPError as exc:
            with exc:
                try:
                    error = json.load(exc)["error"]
                except (ValueError, KeyError, TypeError):
                    raise Fault(exc.code, "unexpected_http_response") from exc
            raise Fault(exc.code, error["code"], error["retryable"],
                        {k:v for k,v in error.items() if k not in ("code","message","retryable")}) from exc

    def frames(self, cursor):
        return self.request("GET", "deliveries/stream", headers={"Accept": "text/event-stream",
                                                                "Last-Event-ID": str(cursor)})

    def ack(self, frame):
        return self.request("POST", f"deliveries/{frame['delivery_id']}/ack", {"cursor": frame["cursor"]},
                            "ack-" + frame["delivery_id"])
