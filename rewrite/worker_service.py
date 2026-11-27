"""Long-lived Worker state and narrow authenticated callbacks from its bound services."""
import json
import sqlite3
from threading import Event, Lock, Thread
import time
from urllib.parse import urlencode, urlsplit
from urllib.error import URLError
from uuid import NAMESPACE_URL, uuid5

from . import display, links, peer_files, peer_retry, queued_chat, user_questions, worker_catalog, worker_commands, worker_handoff, worker_media, worker_resume
from .messages import valid_guid
from .storage import Fault, encode, require, string
from .wire import serve_authenticated
from .worker import ADAPTER_OBSERVATION_ERRORS, ASSIGNMENT_STATUS_REQUIRED, TRANSPORT_RECONCILIATION_ERRORS


HOST_CAPABILITY_FLAGS = {
    "next_turn":"host_queue",
    "context_usage":"host_context",
    "compaction":"host_compaction",
    "session_catalog":"host_session_catalog",
    "stop_private":"host_stop_private",
    "model_view":"host_model_view",
    "model_change":"host_model_change",
    "goal_control":"host_goal_control",
}
OPTIONAL_HOST_CAPABILITY_FLAGS = {
    "session_preparation":"host_session_preparation",
    "managed_session_enrollment":"host_managed_session_enrollment",
}


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
        self.host_capabilities = None
        for flag in (HOST_CAPABILITY_FLAGS | OPTIONAL_HOST_CAPABILITY_FLAGS).values():
            setattr(self,flag,False)
        self.native_command_lock = Lock()
        self.platform_admission_lock = Lock()
        self.native_callback_lock = Lock()
        self.native_callbacks_in_flight = 0
        self.identities = {}
        self.host_session = ""
        self.clock, self.probe_interval = clock, probe_interval
        self.next_probe = 0
        self.next_router_attempt = 0
        self.started = False
        self.last_error = None

    def record_host_observation(self,info,busy):
        capabilities = {key:info.get(key) is True for key in HOST_CAPABILITY_FLAGS}
        for key,flag in HOST_CAPABILITY_FLAGS.items():
            setattr(self,flag,capabilities[key])
        for key,flag in OPTIONAL_HOST_CAPABILITY_FLAGS.items():
            value = info.get(key) is True
            setattr(self,flag,value)
            if value:
                capabilities[key] = value
        self.host_capabilities = capabilities
        self.host_session,self.host_busy,self.adapter_connected = info["session"],busy,True
        self.host_error = None
        self.host_observed_at = self.clock()

    def probe_adapter(self):
        w = self.worker
        try:
            describe = getattr(w.adapter,"session_info",None)
            info = describe() if describe else {"session":w.adapter.bound_session()}
            require(isinstance(info,dict),502,"invalid_host_observation")
            session = info.get("session")
            require(isinstance(session,str) and session,502,"invalid_host_observation")
            if info.get("native_load_state")=="cold":
                require(not worker_resume.blocks_admission(w) and not worker_handoff.active(w),503,"native_handoff_in_progress")
                info = worker_resume.restore_committed_binding(w,info)
            busy = w.adapter.busy(session=session)
            require(type(busy) is bool,502,"invalid_host_observation")
            w.observe_native_binding(session)
            self.record_host_observation(info,busy)
        except (Fault,URLError,OSError) as err:
            self.host_busy = None
            self.adapter_connected = False
            self.host_error = err.code if isinstance(err,Fault) else "adapter_connection_unconfirmed"
            self.host_observed_at = self.clock()
        links.observe(w,"adapter",self.adapter_connected,self.host_session)
        self.next_probe = self.clock()+self.probe_interval

    def step(self):
        w = self.worker
        if not self.started:
            # A new process must establish both legs before reporting a fresh online session.
            with w.db.transaction() as db:
                db.execute("DELETE FROM link_state WHERE up=1")
            self.started = True
        probe = self.clock() >= self.next_probe
        if probe:
            with self.platform_admission_lock:
                self.probe_adapter()
                if not worker_handoff.active(w) and not worker_resume.blocks_admission(w):
                    queued_chat.advance(self)
                    worker_commands.advance_compact(self)
        try:
            if not self.router_connected:
                if self.clock() < self.next_router_attempt:
                    return
                w.connect()
                links.observe(w,"router",True,self.host_session)
                self.router_connected = True
                self.router_observed = True
            with self.platform_admission_lock:
                switch_error = worker_resume.recover(self)
                paused = bool(worker_handoff.active(w) or worker_resume.blocks_admission(w))
            links.publish(w,heartbeat=probe and (not self.adapter_connected or paused))
            w.poll()
            self.last_error = switch_error or (None if self.adapter_connected else self.host_error)
        except (URLError,OSError):
            self.router_observed = True
            self.router_connected = False
            self.next_router_attempt = self.clock() + 20*60
            self.last_error = "router_connection_unconfirmed"
            links.observe(w,"router",False,self.host_session)
        except Fault as err:
            if (err.code in TRANSPORT_RECONCILIATION_ERRORS
                or err.code in (ASSIGNMENT_STATUS_REQUIRED,"stale_session","missing_session","unauthenticated")):
                self.router_observed = True
                self.router_connected = False
                self.next_router_attempt = self.clock() + (self.probe_interval if err.code==ASSIGNMENT_STATUS_REQUIRED else 20*60)
                links.observe(w,"router",False,self.host_session)
            if err.code in ADAPTER_OBSERVATION_ERRORS:
                self.adapter_connected = False
                self.host_error = err.code
                self.host_observed_at = self.clock()
                links.observe(w,"adapter",False,self.host_session)
            self.last_error = err.code

    def poll(self):
        while not self.stop.is_set():
            try:
                self.step()
            except sqlite3.OperationalError:
                self.last_error = "worker_storage_unconfirmed"
            self.stop.wait(0.2)

    def status(self):
        now = self.clock()
        connected = self.adapter_connected
        handoff = worker_handoff.active(self.worker)
        switch = worker_resume.pending(self.worker)
        return {"worker_id":self.worker.worker_id,"router_connected":self.router_connected,
                "router_observed":self.router_observed,"adapter_connected":connected,
                "last_error":self.last_error,"host_error":self.host_error,
                "host_session":self.host_session if connected else None,
                "expected_host_session":getattr(self.worker.adapter,"expected_session",None),
                "host_busy":self.host_busy if connected else None,
                "native_capabilities":self.host_capabilities if connected else None,
                "host_observation_age_seconds":None if self.host_observed_at is None else max(0,now-self.host_observed_at),
                "router_retry_in_seconds":None if self.router_connected or not self.router_observed
                else max(0,self.next_router_attempt-now),**self.worker.hold(),
                **({"handoff":handoff} if handoff else {}),
                **({"native_switch":{"source":switch["old_session"],"target":switch["target"],"phase":switch["phase"],
                                     "handoff_id":switch["handoff_id"]}}
                   if switch else {})}

    def handle(self, actor, method, path, body, _headers):
        w = self.worker
        adapter = "adapter/"+w.worker_id
        if w.resume_targets and (actor==adapter or actor.startswith(adapter+"/")):
            with self.native_callback_lock:
                require(actor!=adapter,403,"scoped_native_callback_required")
                if actor!=adapter+"/"+w.native_session_pin:
                    raise Fault(409,"native_callback_inactive",True)
                require(not worker_resume.blocks_admission(w),503,"native_handoff_in_progress")
                self.native_callbacks_in_flight += 1
            try:
                return self.route(adapter,method,path,body,_headers)
            finally:
                with self.native_callback_lock:
                    self.native_callbacks_in_flight -= 1
        if actor=="platform/"+w.worker_id and method=="POST" and path in (
                "/v1/worker/chat","/v1/worker/command","/v1/worker/trainer-answer"):
            with self.platform_admission_lock:
                text = body.get("text")
                resume = path=="/v1/worker/command" and w.resume_targets and isinstance(text,str) and (
                    worker_resume.selection(text) or (w.session_callback_key and worker_resume.creation(text)))
                if not resume and (worker_handoff.active(w) or worker_resume.blocks_admission(w)):
                    raise Fault(503,"native_handoff_in_progress",True)
                try:
                    return self.route(actor,method,path,body,_headers)
                except Fault as error:
                    if resume and error.code in ("stale_session","missing_session","unauthenticated"):
                        raise Fault(503,"native_switch_unconfirmed",True) from None
                    raise
                except (OSError,URLError):
                    if resume:
                        raise Fault(503,"native_switch_unconfirmed",True) from None
                    raise
        return self.route(actor,method,path,body,_headers)

    def route(self, actor, method, path, body, _headers):
        w = self.worker
        if path=="/v1/worker/handoff-admission":
            require(actor=="handoff/"+w.worker_id,403,"forbidden")
            require(method=="PUT",405,"invalid_handoff")
            try:
                with self.platform_admission_lock:
                    switch = worker_resume.pending(w)
                    reply = worker_resume.cancel(self,switch,body) if switch else worker_handoff.request(w,body)
            except (OSError,URLError):
                raise Fault(503,"handoff_receipt_unconfirmed",True) from None
            return 200,"application/json",reply
        if path=="/v1/worker/workspace-state":
            require(actor=="adapter/"+w.worker_id,403,"forbidden")
            require(method=="POST" and set(body)=={"session","task_id","assignment_id"}
                    and valid_guid(body["task_id"]) and valid_guid(body["assignment_id"]),422,"invalid_workspace")
            return 200,"application/json",w.workspace_state(string(body,"session"),body["task_id"],body["assignment_id"])
        if path.startswith("/v1/worker/media/"):
            return worker_media.route(w,actor,method,path,body,_headers)
        if path.startswith("/v1/worker/approvals/"):
            require(actor=="adapter/"+w.worker_id,403,"forbidden")
            parts = path.removeprefix("/v1/worker/approvals/").split("/")
            require(parts and valid_guid(parts[0]),422,"invalid_approval_id")
            session = string(body,"session")
            require(w.adapter.bound_session()==session,403,"wrong_native_session")
            target = "approvals/"+w.worker_id+"/"+parts[0]
            if method=="POST" and len(parts)==1:
                result = w.router.request(method,target,{"session_id":w.session,"request":body})
            elif method=="POST" and len(parts)==2 and parts[1]=="receipt":
                fields = {"session","request_digest"}
                require(fields<=set(body)<=fields|{"attempted_response"},422,"invalid_approval_receipt_lookup")
                result = w.router.request(method,target+"/receipt",dict(body,session_id=w.session))
            elif method=="PUT" and len(parts)==2 and parts[1]=="recover-codex-closure":
                fields = {"session","turn_id","item_id","request_digest","request_id","attempted_response"}
                require(set(body)==fields,422,"invalid_codex_resolution")
                result = w.router.request(method,target+"/recover-codex-closure",dict(body,session_id=w.session))
            elif method=="PUT" and len(parts)==2 and parts[1]=="closed":
                fields = {"session","request_digest","attempted_response"}
                require(fields<=set(body)<=fields|{"request","native_proof"},422,"invalid_approval_closure")
                if "request" in body:
                    require(isinstance(body["request"],dict) and body["request"].get("session")==session,
                            403,"wrong_native_session")
                proof = {key:value for key,value in body.items() if key!="session"}
                result = w.router.request(method,target+"/closed",dict(proof,session_id=w.session))
            else:
                raise Fault(404,"unknown_approval_route")
            return 200,"application/json",result
        if path.partition("?")[0] == "/v1/worker/display":
            if method == "POST" and actor == "adapter/"+w.worker_id:
                return 200,"application/json",display.native(w,body)
            if method == "GET" and actor == "platform/"+w.worker_id:
                return 200,"application/json",display.read(w,path)
            raise Fault(403,"forbidden")
        if actor == "adapter/" + w.worker_id and method == "POST" and path == "/v1/worker/native-tool":
            session,turn = string(body,"session"),string(body,"turn_id")
            args = body.get("arguments")
            require(isinstance(args,dict),422,"invalid_arguments")
            if body.get("tool")=="worker_ask_trainer":
                if "native_direct" in body:
                    require(body["native_direct"] is True,422,"invalid_native_question_origin")
                else:
                    native = w.adapter.input_for_turn(session,turn)
                    origin = w.adapter.origin(native,session=session)
                    require(origin["kind"] in ("chat","queued_chat","trainer_answer"),403,"wrong_trainer_question_origin")
                result = {**user_questions.open_standalone(w,session,turn,string(body,"call_id"),args),
                          "state":"queued_for_trainer"}
                return 200,"application/json",result
            require("native_direct" not in body,422,"invalid_native_question_origin")
            native = w.adapter.input_for_turn(session,turn)
            if body.get("tool") == "worker_ask":
                require(set(args) == {"text","options"},422,"invalid_arguments")
                result = w.ask(native, string(body,"call_id"), string(args,"text"), args["options"])
            elif body.get("tool") == "worker_submit_material":
                require({"task_id","assignment_id"}<=set(args)<={"task_id","assignment_id","text","artifact_ids"},
                        422,"invalid_arguments")
                call = string(body,"call_id")
                key = "candidate-"+str(uuid5(NAMESPACE_URL,encode([w.worker_id,session,turn,call])))
                result = w.submit_material(session,native,key,args)
            elif body.get("tool") == "worker_stage_artifacts":
                result = w.stage_work_artifacts(session,native,args)
            elif body.get("tool") in ("worker_send_peer","worker_peer_status","worker_list_peers","worker_peer_inbox"):
                self.require_peer_origin(session,native)
                if body["tool"] == "worker_send_peer":
                    result = self.send_native_peer(session,turn,string(body,"call_id"),args)
                elif body["tool"] == "worker_peer_status":
                    require(set(args)=={"message_id"},422,"invalid_arguments")
                    result = self.peer_status(string(args,"message_id"))
                    result.setdefault("status","accepted")
                elif body["tool"] == "worker_list_peers":
                    result = self.peer_directory(args)
                else:
                    result = self.peer_inbox(args)
            else:
                require(body.get("tool") == "worker_report" and set(args) <= {"task_id","assignment_id","summary","outcome","artifact_ids"},
                        422,"unsupported_native_tool")
                try:
                    result = w.report(native,string(args,"task_id"),string(args,"assignment_id"),
                                      string(args,"summary"),args.get("outcome","completed"),args.get("artifact_ids"))
                except OSError:
                    raise Fault(503,"worker_report_unconfirmed",True) from None
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
        elif actor == "platform/" + w.worker_id and method == "POST" and path == "/v1/worker/session-menu-receipt":
            result = worker_catalog.acknowledge(w,body)
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
        require(origin["kind"] in ("chat","queued_chat","trainer_answer","peer","task","task_update","task_probe","question_answer"),
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
                "status":"accepted" if ack else states.get(row["phase"],"unknown"),
                "accepted_at":ack["accepted_at"] if ack else None,"worker_received":None,
                "host_receipt_id":None,"local_failure":row["failure"]}

    def peer_inbox(self,args):
        view = args.get("view","pending")
        if view!="legacy":
            require(set(args)<={"view","after"},422,"invalid_arguments")
            return self.worker.peer_inbox(view,args.get("after",0))
        source,mid = args.get("source_id"),args.get("message_id")
        if "source_id" in args:
            require(valid_guid(source),422,"invalid_legacy_source")
        if "message_id" in args:
            require(source and valid_guid(mid) and set(args)<={"view","source_id","message_id","part"},
                    422,"invalid_arguments")
            part = args.get("part",0)
            require(type(part) is int and 0<=part<10**18,422,"invalid_history_part")
            return self.worker.router.request("GET",f"history/{source}/peers/{mid}?part={part}")
        require(set(args)<={"view","source_id","after","limit"},422,"invalid_arguments")
        after,limit = args.get("after",0),args.get("limit",50)
        require(type(after) is int and 0<=after<10**18 and type(limit) is int and 1<=limit<=100,
                422,"invalid_history_filter")
        route = f"history/{source}/peers" if source else "history/peers"
        return self.worker.router.request("GET",route+"?"+urlencode({"cursor":after,"limit":limit}))

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
        w = self.worker
        self.identities = dict(identities)
        if w.resume_targets:
            adapter = "adapter/"+w.worker_id
            roles = set(identities.values())
            configured = w.resume_targets.keys()-w.managed_native_sessions.keys()
            require(adapter not in roles and all(adapter+"/"+session in roles for session in configured),
                    422,"scoped_native_callbacks_required")
            for session,(_anchor,op) in w.managed_native_sessions.items():
                token = worker_resume.callback_token(w,session,op)
                self.check_managed_callback(session,token)
                self.add_managed_callback(session,token)
        return serve_authenticated(self.identities,address,ctx,self.handle)

    def check_managed_callback(self,session,token):
        role = "adapter/"+self.worker.worker_id+"/"+session
        require(token not in self.identities and role not in self.identities.values(),409,"managed_callback_identity_conflict")

    def add_managed_callback(self,session,token):
        self.identities[token] = "adapter/"+self.worker.worker_id+"/"+session
