"""Long-lived Worker state and narrow authenticated callbacks from its bound services."""
from threading import Event, Lock, Thread
import time
from urllib.parse import urlencode, urlsplit
from urllib.error import URLError
from uuid import NAMESPACE_URL, uuid5

from . import display, links, peer_files, peer_retry, queued_chat, user_questions, worker_commands, worker_media
from .messages import valid_guid
from .storage import Fault, encode, require, string
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
        self.host_error = None
        self.host_observed_at = None
        self.host_queue = False
        self.host_context = False
        self.host_compaction = False
        self.host_session_catalog = False
        self.host_stop_private = False
        self.host_model_view = False
        self.host_model_change = False
        self.host_goal_control = False
        self.native_command_lock = Lock()
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
                self.host_error = None
                self.host_queue = info.get("next_turn") is True
                self.host_context = info.get("context_usage") is True
                self.host_compaction = info.get("compaction") is True
                self.host_session_catalog = info.get("session_catalog") is True
                self.host_stop_private = info.get("stop_private") is True
                self.host_model_view = info.get("model_view") is True
                self.host_model_change = info.get("model_change") is True
                self.host_goal_control = info.get("goal_control") is True
            except (Fault,URLError,OSError) as err:
                self.host_busy = None
                self.adapter_connected = False
                self.host_error = err.code if isinstance(err,Fault) else "adapter_connection_unconfirmed"
            self.host_observed_at = self.clock()
            links.observe(w,"adapter",self.adapter_connected,self.host_session)
            self.next_probe = self.clock()+self.probe_interval
            queued_chat.advance(self)
            worker_commands.advance_compact(self)
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
            if err.code in ("adapter_connection_unconfirmed","wrong_native_session","native_agent_unavailable"):
                self.adapter_connected = False
                self.host_error = err.code
                self.host_observed_at = self.clock()
                links.observe(w,"adapter",False,self.host_session)
            self.last_error = err.code

    def poll(self):
        while not self.stop.is_set():
            self.step()
            self.stop.wait(0.2)

    def status(self):
        now = self.clock()
        connected = self.adapter_connected
        return {"worker_id":self.worker.worker_id,"router_connected":self.router_connected,
                "router_observed":self.router_observed,"adapter_connected":connected,
                "last_error":self.last_error,"host_error":self.host_error,
                "host_session":self.host_session if connected else None,
                "expected_host_session":getattr(self.worker.adapter,"expected_session",None),
                "host_busy":self.host_busy if connected else None,
                "host_observation_age_seconds":None if self.host_observed_at is None else max(0,now-self.host_observed_at),
                "router_retry_in_seconds":None if self.router_connected or not self.router_observed
                else max(0,self.next_router_attempt-now),**self.worker.hold()}

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
            session,turn = string(body,"session"),string(body,"turn_id")
            native = w.adapter.input_for_turn(session,turn)
            args = body.get("arguments")
            require(isinstance(args,dict),422,"invalid_arguments")
            if body.get("tool") == "worker_ask":
                require(set(args) == {"text","options"},422,"invalid_arguments")
                result = w.ask(native, string(body,"call_id"), string(args,"text"), args["options"])
            elif body.get("tool") == "worker_submit_material":
                require({"task_id","assignment_id"}<=set(args)<={"task_id","assignment_id","text","artifact_ids"},
                        422,"invalid_arguments")
                call = string(body,"call_id")
                key = "candidate-"+str(uuid5(NAMESPACE_URL,encode([w.worker_id,session,turn,call])))
                result = w.submit_material(session,native,key,args)
            elif body.get("tool") in ("worker_send_peer","worker_peer_status","worker_list_peers"):
                self.require_peer_origin(session,native)
                if body["tool"] == "worker_send_peer":
                    result = self.send_native_peer(session,turn,string(body,"call_id"),args)
                elif body["tool"] == "worker_peer_status":
                    require(set(args)=={"message_id"},422,"invalid_arguments")
                    result = self.peer_status(string(args,"message_id"))
                    result.setdefault("status","accepted")
                else:
                    result = self.peer_directory(args)
            else:
                require(body.get("tool") == "worker_report" and set(args) <= {"task_id","assignment_id","summary","outcome","artifact_ids"},
                        422,"unsupported_native_tool")
                result = w.report(native,string(args,"task_id"),string(args,"assignment_id"),
                                  string(args,"summary"),args.get("outcome","completed"),args.get("artifact_ids"))
        elif actor == "adapter/" + w.worker_id and method == "POST" and path == "/v1/worker/native-question":
            result = user_questions.open_native(w,body)
        elif actor == "adapter/" + w.worker_id and method == "POST" and path == "/v1/worker/native-question-proof":
            result = user_questions.observe_proof(w,body)
        elif actor == "platform/" + w.worker_id and method == "POST" and path == "/v1/worker/trainer-answer":
            result = user_questions.resolve(w,body)
        elif actor == "adapter/" + w.worker_id and method == "POST" and path == "/v1/worker/peer":
            result = w.send_peer(string(body,"message_id"),string(body,"to_worker"),body.get("text",""),body.get("reply_to"),
                                 body.get("attachment_ids"),body.get("attachment_kinds"))
        elif actor == "adapter/" + w.worker_id and method == "GET" and path == "/v1/worker/peers":
            result = {"items":w.peer_history()}
        elif actor=="adapter/"+w.worker_id and path.startswith("/v1/worker/history/"):
            url = urlsplit(path)
            parts = url.path.removeprefix("/v1/worker/history/").split("/")
            require(len(parts) in (2,3,4) and parts[1]=="peers" and valid_guid(parts[0]),404,"unknown_history_route")
            if len(parts)>2:
                require(valid_guid(parts[2]),422,"invalid_peer_message")
            route = "history/"+"/".join(parts[:3])
            if len(parts)==4:
                require(parts[3]=="file" and not url.query and method=="POST" and isinstance(body,dict)
                        and set(body)=={"position"} and type(body["position"]) is int,422,"invalid_history_file")
                files = w.router.request("GET",route)["attachments"]
                require(0<=body["position"]<len(files),404,"history_attachment_not_found")
                result = peer_files.receive(w,files[body["position"]])
            else:
                require(method=="GET",405,"read_only_history")
                result = w.router.request("GET",route+("?"+url.query if url.query else ""))
        elif actor == "adapter/"+w.worker_id and method=="GET" and path.startswith("/v1/worker/peers/"):
            result = self.peer_status(path.removeprefix("/v1/worker/peers/"))
        elif actor == "platform/" + w.worker_id and method == "POST" and path == "/v1/worker/command":
            result = {"receipt":worker_commands.handle(self,body)}
        elif actor == "platform/" + w.worker_id and method == "POST" and path == "/v1/worker/model-menu-receipt":
            result = worker_commands.acknowledge_menu(w,body)
        elif actor == "platform/" + w.worker_id and method == "POST" and path == "/v1/worker/chat":
            require({"message_id","text"} <= body.keys() <= {"message_id","text","attachment_ids","attachment_kinds"},
                    422,"invalid_chat")
            result = {"receipt":w.chat(string(body,"message_id"),body["text"],body.get("attachment_ids"),
                                       body.get("attachment_kinds"))}
        elif actor == "ops/" + w.worker_id and method == "GET" and path == "/v1/worker/status":
            result = self.status()
        else:
            raise Fault(403,"forbidden")
        return 200,"application/json",result

    def require_peer_origin(self,session,native):
        w = self.worker
        origin = w.adapter.origin(native,session=session)
        require(origin["kind"] in ("chat","queued_chat","peer","task","task_update","task_probe","question_answer"),
                403,"wrong_peer_origin")
        assignment = origin.get("assignment_id")
        require(not assignment or w.assignment_session(assignment) in (None,session),403,"wrong_assignment_session")

    def send_native_peer(self,session,turn,call,args):
        require({"to_worker"}<=set(args)<={"to_worker","text","reply_to","attachment_ids","attachment_kinds"},
                422,"invalid_arguments")
        w = self.worker
        mid = str(uuid5(NAMESPACE_URL,encode(["native-peer-message",w.worker_id,session,turn,call])))
        try:
            return w.send_peer(mid,string(args,"to_worker"),args.get("text",""),args.get("reply_to"),
                               args.get("attachment_ids"),args.get("attachment_kinds"))
        except (Fault,OSError) as error:
            if isinstance(error,Fault) and not peer_retry.send_retryable(error):
                raise
            pending = self.local_peer_status(mid)
            require(pending,503,"peer_exchange_unconfirmed")
            return pending

    def local_peer_status(self,mid):
        w = self.worker
        with w.db.transaction() as db:
            row = db.execute("""SELECT p.peer_id,p.receipt,r.phase,r.failure FROM peers p
                LEFT JOIN peer_retry r ON r.operation_id=p.message_id AND r.phase IN ('send','send_rejected')
                WHERE p.message_id=? AND p.direction='out'""",(mid,)).fetchone()
        if not row:
            return None
        ack = json.loads(row["receipt"]) if row["receipt"] else None
        states = {"send":"pending","send_rejected":"rejected"}
        return {"message_id":mid,"sender":w.worker_id,"recipient":row["peer_id"],
                "status":states.get(row["phase"],"unknown"),
                "accepted_at":ack["accepted_at"] if ack else None,"worker_received":None,
                "host_receipt_id":None,"local_failure":row["failure"]}

    def peer_status(self,mid):
        require(valid_guid(mid),422,"invalid_peer_message")
        w = self.worker
        try:
            result = w.router.request("GET","peers/messages/"+mid)
        except (Fault,OSError) as error:
            if isinstance(error,Fault) and error.code!="peer_message_not_found" and error.status<500:
                raise
            local = self.local_peer_status(mid)
            if local is None:
                raise
            return local
        if result["recipient"]==w.worker_id:
            result["local_pending"] = peer_retry.waiting(w,mid)
        return result

    def peer_directory(self,args):
        require(set(args)<={"online","cursor","limit"},422,"invalid_peer_filters")
        query = dict(args)
        if "online" in query:
            require(type(query["online"]) is bool,422,"invalid_peer_filters")
            query["online"] = str(query["online"]).lower()
        require(type(query.get("limit",1)) is int and isinstance(query.get("cursor","0"),str),
                422,"invalid_peer_filters")
        return self.worker.router.request("GET","peers/workers?"+urlencode(query))

    def listen(self, identities, address, ctx):
        return serve_authenticated(identities,address,ctx,self.handle)
