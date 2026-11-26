"""Long-lived Worker state and narrow authenticated callbacks from its bound services."""
from threading import Event, Thread
import time
from urllib.error import URLError

from . import display, links, queued_chat, user_questions, worker_commands, worker_media
from .storage import Fault, require, string
from .wire import serve_authenticated


class WorkerService:
    def __init__(self, worker, clock=time.monotonic, probe_interval=5):
        self.worker = worker
        self.stop = Event()
        self.thread = Thread(target=self.poll, daemon=True)
        self.router_connected = False
        self.router_observed = False
        self.adapter_connected = None
        self.host_busy = None
        self.host_queue = False
        self.host_session = ""
        self.clock, self.probe_interval = clock, probe_interval
        self.next_probe = 0
        self.next_router_attempt = 0
        self.started = False
        self.last_error = None

    def step(self):
        w = self.worker
        if not self.started:
            # A new process must establish both legs before reporting a fresh online session.
            with w.db.transaction() as db:
                db.execute("DELETE FROM link_state WHERE up=1")
            self.started = True
        probe = self.clock() >= self.next_probe
        if probe:
            try:
                describe = getattr(w.adapter,"session_info",None)
                info = describe() if describe else {"session":w.adapter.bound_session()}
                session = info["session"]
                busy = w.adapter.busy()
                require(isinstance(session,str) and session and type(busy) is bool,
                        502,"invalid_host_observation")
                self.host_session, self.host_busy, self.adapter_connected = session,busy,True
                self.host_queue = info.get("next_turn") is True
            except (Fault,URLError,OSError):
                self.host_busy = None
                self.adapter_connected = False
            links.observe(w,"adapter",self.adapter_connected,self.host_session)
            self.next_probe = self.clock()+self.probe_interval
            queued_chat.advance(self)
        try:
            if not self.router_connected:
                if self.clock() < self.next_router_attempt:
                    return
                w.connect()
                self.router_connected = True
                self.router_observed = True
                links.observe(w,"router",True,self.host_session)
            links.publish(w,heartbeat=probe and not self.adapter_connected)
            w.poll()
            self.last_error = None if self.adapter_connected else "adapter_connection_unconfirmed"
        except (URLError,OSError):
            self.router_observed = True
            self.router_connected = False
            self.next_router_attempt = self.clock() + 20*60
            self.last_error = "router_connection_unconfirmed"
            links.observe(w,"router",False,self.host_session)
        except Fault as err:
            if err.code in ("stale_session","missing_session","unauthenticated"):
                self.router_observed = True
                self.router_connected = False
                self.next_router_attempt = self.clock() + 20*60
                links.observe(w,"router",False,self.host_session)
            if err.code == "adapter_connection_unconfirmed":
                self.adapter_connected = False
                links.observe(w,"adapter",False,self.host_session)
            self.last_error = err.code

    def poll(self):
        while not self.stop.is_set():
            self.step()
            self.stop.wait(0.2)

    def handle(self, actor, method, path, body, _headers):
        w = self.worker
        if path.startswith("/v1/worker/media/"):
            return worker_media.route(w,actor,method,path,body,_headers)
        if path.partition("?")[0] == "/v1/worker/display":
            if method == "POST" and actor == "adapter/"+w.worker_id:
                return 200,"application/json",display.native(w,body)
            if method == "GET" and actor == "platform/"+w.worker_id:
                return 200,"application/json",display.read(w,path)
            raise Fault(403,"forbidden")
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
        elif actor == "adapter/" + w.worker_id and method == "POST" and path == "/v1/worker/native-question":
            result = user_questions.open_native(w,body)
        elif actor == "platform/" + w.worker_id and method == "POST" and path == "/v1/worker/trainer-answer":
            result = user_questions.resolve(w,body)
        elif actor == "adapter/" + w.worker_id and method == "POST" and path == "/v1/worker/peer":
            result = w.send_peer(string(body,"message_id"),string(body,"to_worker"),string(body,"text"),body.get("reply_to"))
        elif actor == "adapter/" + w.worker_id and method == "GET" and path == "/v1/worker/peers":
            result = {"items":w.peer_history()}
        elif actor == "platform/" + w.worker_id and method == "POST" and path == "/v1/worker/command":
            result = {"receipt":worker_commands.handle(self,body)}
        elif actor == "platform/" + w.worker_id and method == "POST" and path == "/v1/worker/chat":
            require({"message_id","text"} <= body.keys() <= {"message_id","text","attachment_ids"},422,"invalid_chat")
            result = {"receipt":w.chat(string(body,"message_id"),body["text"],body.get("attachment_ids"))}
        elif actor == "ops/" + w.worker_id and method == "GET" and path == "/v1/worker/status":
            result = {"worker_id":w.worker_id,"router_connected":self.router_connected,
                      "adapter_connected":self.adapter_connected,"last_error":self.last_error,**w.hold()}
        else:
            raise Fault(403,"forbidden")
        return 200,"application/json",result

    def listen(self, identities, address, ctx):
        return serve_authenticated(identities,address,ctx,self.handle)
