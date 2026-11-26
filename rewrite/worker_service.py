"""Long-lived Worker state and narrow authenticated callbacks from its bound services."""
from threading import Event, Thread
from urllib.error import URLError

from . import worker_media
from .storage import Fault, require, string
from .wire import serve_authenticated


class WorkerService:
    def __init__(self, worker):
        self.worker = worker
        self.stop = Event()
        self.thread = Thread(target=self.poll, daemon=True)
        self.router_connected = False
        self.last_error = None

    def poll(self):
        while not self.stop.is_set():
            try:
                if not self.router_connected:
                    self.worker.connect()
                    self.router_connected = True
                self.worker.poll()
                self.last_error = None
            except (URLError,OSError):
                self.router_connected = False
                self.last_error = "router_connection_unconfirmed"
            except Fault as err:
                if err.code in ("stale_session","missing_session"):
                    self.router_connected = False
                self.last_error = err.code
            self.stop.wait(0.2)

    def handle(self, actor, method, path, body, _headers):
        w = self.worker
        if path.startswith("/v1/worker/media/"):
            return worker_media.route(w,actor,method,path,body,_headers)
        if actor == "adapter/" + w.worker_id and method == "POST" and path == "/v1/worker/native-tool":
            native = w.adapter.input_for_turn(string(body,"session"),string(body,"turn_id"))
            args = body.get("arguments")
            require(isinstance(args,dict),422,"invalid_arguments")
            if body.get("tool") == "worker_ask":
                require(set(args) == {"text","options"},422,"invalid_arguments")
                result = w.ask(native, string(body,"call_id"), string(args,"text"), args["options"])
            else:
                require(body.get("tool") == "worker_report" and set(args) <= {"task_id","assignment_id","summary","outcome","artifact_ids"},
                        422,"unsupported_native_tool")
                result = w.report(native,string(args,"task_id"),string(args,"assignment_id"),
                                  string(args,"summary"),args.get("outcome","completed"),args.get("artifact_ids"))
        elif actor == "adapter/" + w.worker_id and method == "POST" and path == "/v1/worker/peer":
            result = w.send_peer(string(body,"message_id"),string(body,"to_worker"),string(body,"text"),body.get("reply_to"))
        elif actor == "adapter/" + w.worker_id and method == "GET" and path == "/v1/worker/peers":
            result = {"items":w.peer_history()}
        elif actor == "platform/" + w.worker_id and method == "POST" and path == "/v1/worker/chat":
            require({"message_id","text"} <= body.keys() <= {"message_id","text","attachment_ids"},422,"invalid_chat")
            result = {"receipt":w.chat(string(body,"message_id"),body["text"],body.get("attachment_ids"))}
        elif actor == "ops/" + w.worker_id and method == "GET" and path == "/v1/worker/status":
            result = {"worker_id":w.worker_id,"router_connected":self.router_connected,
                      "last_error":self.last_error,**w.hold()}
        else:
            raise Fault(403,"forbidden")
        return 200,"application/json",result

    def listen(self, identities, address, ctx):
        return serve_authenticated(identities,address,ctx,self.handle)
