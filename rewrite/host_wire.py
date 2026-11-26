"""Authenticated Worker-to-adapter port with explicit native session pins."""
import re
from urllib.error import URLError

from .messages import valid_guid
from .storage import Fault, require, string
from .wire import serve_authenticated


SESSION_HEADER = "X-Mowu-Host-Session"
TASK_INPUTS = {"task","task_update","task_probe","task.shelved","task.cancel","task.transfer_out","question_answer"}


class RemoteAdapter:
    def __init__(self, client, expected_session=None):
        self.client = client
        if expected_session is not None and (not isinstance(expected_session,str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}",expected_session)):
            raise ValueError("expected a native session identifier")
        self.expected_session = expected_session

    def request(self, method, path, *args, native_session=None):
        try:
            require(native_session is None or self.expected_session in (None,native_session),409,"wrong_native_session")
            session = self.expected_session or native_session
            if session:
                route = "host/bound/"+path.removeprefix("host/")
                return self.client.request(method,route,*args,headers={SESSION_HEADER:session})
            return self.client.request(method,path,*args)
        except Fault as err:
            if err.status in (401,403) and err.code in ("unauthenticated","wrong_adapter_owner"):
                raise Fault(err.status,"adapter_identity_unconfirmed") from err
            raise
        except (URLError,OSError):
            raise Fault(503,"adapter_connection_unconfirmed",True) from None

    def busy(self, *, session=None):
        reply = self.request("GET","host/activity",native_session=session)
        require(isinstance(reply,dict) and type(reply.get("busy")) is bool, 502, "invalid_host_observation")
        return reply["busy"]

    def handoff_activity(self, *, session):
        try:
            reply = self.request("GET","host/handoff-activity",native_session=session)
        except Fault as error:
            if error.status==404 and error.code=="unknown_host_route":
                raise Fault(409,"handoff_adapter_upgrade_required") from None
            raise
        require(isinstance(reply,dict) and reply.get("session")==session
                and type(reply.get("busy")) is bool and type(reply.get("approval_pending")) is bool
                and type(reply.get("workspace_pending",False)) is bool,
                502,"invalid_handoff_observation")
        return reply

    def bound_session(self):
        return self.session_info()["session"]

    def session_info(self):
        info = self.request("GET","host/session")
        require(isinstance(info,dict) and isinstance(info.get("session"),str) and info["session"].strip(),
                502,"invalid_host_observation")
        session = info["session"]
        require(self.expected_session is None or session==self.expected_session,409,"wrong_native_session")
        state = info.get("native_load_state")
        require(state in (None,"cold","loaded"),502,"invalid_host_observation")
        return {**({"native_load_state":state} if state is not None else {}),
                **({"session_preparation":True} if info.get("session_preparation") is True else {}),
                **({"managed_session_enrollment":True} if info.get("managed_session_enrollment") is True else {}),
                "session":session,"next_turn":info.get("next_turn") is True,
                "context_usage":info.get("context_usage") is True,"model_view":info.get("model_view") is True,
                "model_change":info.get("model_change") is True,"goal_control":info.get("goal_control") is True,
                "compaction":info.get("compaction") is True,"session_catalog":info.get("session_catalog") is True,
                "stop_private":info.get("stop_private") is True}

    def activate(self,session,operation_id):
        return self.request("POST","host/activate",{"operation_id":operation_id},native_session=session)

    def prepare_thread(self,session,operation_id,kind):
        return self.request("POST","host/prepare-thread",{"operation_id":operation_id,"kind":kind},
                            native_session=session)

    def enroll_thread(self,source,operation_id,session,callback_token):
        return self.request("POST","host/enroll-thread",
                            {"operation_id":operation_id,"session":session,"callback_token":callback_token},
                            native_session=source)

    def for_session(self,session):
        return RemoteAdapter(self.client,session)

    def context_usage(self):
        return self.request("GET","host/context")

    def session_catalog(self):
        return self.request("GET","host/session-catalog")

    def stop_private(self,session):
        return self.request("POST","host/stop-private",{"session":session})

    def compact(self, session, command_id, *, start):
        return self.request("POST","host/compact",{"session":session,"command_id":command_id,"start":start})

    def goal_view(self):
        return self.request("GET","host/goal")

    def goal_change(self, session, action, *, objective=None):
        body = {"session":session,"action":action}
        if objective is not None:
            body["objective"] = objective
        return self.request("POST","host/goal",body)

    def model_view(self, kind):
        return self.request("GET","host/"+kind)

    def model_change(self, session, kind, value, *, expected_route=None):
        body = {"session":session,"kind":kind,"value":value}
        if expected_route is not None:
            body["expected_route"] = expected_route
        return self.request("POST","host/model-selection",body)

    def deliver(self, did, payload, *, session=None):
        result = self.request("POST","host/deliver",{"delivery_id":did,"payload":payload},native_session=session)
        require(isinstance(result,dict) and "receipt" in result,502,"host_receipt_unconfirmed")
        return result["receipt"]

    def lookup_delivery(self,did,payload,*,session):
        kind = payload["kind"]
        route = "host/find-peer" if kind=="peer" else "host/find-chat"
        if kind in TASK_INPUTS:
            route = "host/find-task"
        if kind=="trainer_answer":
            route = "host/find-trainer-answer"
        result = self.request("POST",route,{"delivery_id":did,"payload":payload},native_session=session)
        require(isinstance(result,dict) and "receipt" in result,502,"host_receipt_unconfirmed")
        return result["receipt"]

    def origin(self, native_id, *, session=None):
        return self.request("POST","host/origin",{"native_id":native_id},native_session=session)["origin"]

    def input_for_turn(self, session, turn):
        return self.request("POST", "host/turn", {"session":session, "turn_id":turn})["native_id"]

    def cleanup_evidence(self, assignment_id, kind):
        return self.request("POST", "host/cleanup-evidence", {"assignment_id":assignment_id,"kind":kind})["proof"]

    def resolve_question(self, original, answer):
        return self.request("POST","host/answer-question",{"original":original,"answer":answer})["proof"]


def serve_adapter(adapter, worker_id, identities, address, context, *, activation=None, preparation=None, family=None):
    def handle(actor, method, path, body, headers):
        require(actor == "worker/" + worker_id, 403, "wrong_adapter_owner")
        pinned = path.startswith("/v1/host/bound/")
        current = adapter,activation,preparation
        if pinned:
            session = headers.get(SESSION_HEADER)
            if session!=adapter.bound_session():
                require(family is not None,409,"wrong_native_session")
                port = family.port(session)
                current = port.adapter,port.activation,port.preparation
            path = "/v1/host/"+path.removeprefix("/v1/host/bound/")
        return route(*current,method,path,body,pinned)

    def route(adapter,activation,preparation,method,path,body,pinned):
        if activation is not None and path not in ("/v1/host/session","/v1/host/activate"):
            if activation.state!="loaded":
                raise Fault(503,"native_session_cold",True)
        match method,path:
            case "GET","/v1/host/session":
                info = getattr(adapter,"session_info",None)
                result = info() if info else {"session":adapter.bound_session(),"next_turn":False}
                if activation is not None:
                    result["native_load_state"] = activation.state
                if preparation is not None:
                    result["session_preparation"] = True
                if family is not None:
                    result["managed_session_enrollment"] = True
            case "POST","/v1/host/activate":
                require(pinned and activation is not None,403,"native_activation_unavailable")
                require(set(body)=={"operation_id"} and valid_guid(body.get("operation_id")),422,"invalid_native_activation")
                result = activation.activate(adapter.bound_session(),body["operation_id"])
            case "POST","/v1/host/prepare-thread":
                require(pinned and preparation is not None,403,"native_preparation_unavailable")
                require(set(body)=={"operation_id","kind"},422,"invalid_native_preparation")
                result = preparation.prepare(adapter.bound_session(),body["operation_id"],body["kind"])
            case "POST","/v1/host/enroll-thread":
                require(pinned and family is not None,403,"native_enrollment_unavailable")
                require(set(body)=={"operation_id","session","callback_token"},422,"invalid_native_enrollment")
                result = family.enroll(adapter.bound_session(),body["operation_id"],body["session"],body["callback_token"])
            case "GET","/v1/host/activity":
                result = {"busy":adapter.busy()}
            case "GET","/v1/host/handoff-activity":
                require(pinned,403,"native_handoff_observation_requires_pin")
                result = adapter.handoff_activity()
            case "GET","/v1/host/context":
                result = adapter.context_usage()
            case "GET","/v1/host/session-catalog":
                result = adapter.session_catalog()
            case "POST","/v1/host/stop-private":
                require(set(body)=={"session"},422,"invalid_stop_request")
                result = adapter.stop_private(string(body,"session"))
            case "POST","/v1/host/compact":
                require(set(body)=={"session","command_id","start"} and type(body["start"]) is bool,422,"invalid_compact_request")
                result = adapter.compact(string(body,"session"),string(body,"command_id"),start=body["start"])
            case "GET","/v1/host/goal":
                result = adapter.goal_view()
            case "POST","/v1/host/goal":
                action = body.get("action")
                if action=="create":
                    require(set(body)=={"session","action","objective"},422,"invalid_goal_action")
                    result = adapter.goal_change(string(body,"session"),action,objective=string(body,"objective"))
                else:
                    require(set(body)=={"session","action"} and action in ("pause","resume"),422,"invalid_goal_action")
                    result = adapter.goal_change(string(body,"session"),action)
            case "GET",("/v1/host/models"|"/v1/host/efforts"):
                result = adapter.model_view(path.rsplit("/",1)[1])
            case "POST","/v1/host/model-selection":
                require({"session","kind","value"}<=set(body)<={"session","kind","value","expected_route"}
                        and body["kind"] in ("models","efforts"),
                        422,"invalid_model_change")
                args = (string(body,"session"),body["kind"],string(body,"value"))
                if "expected_route" in body:
                    require(body["kind"]=="efforts",422,"invalid_model_change")
                    result = adapter.model_change(*args,expected_route=string(body,"expected_route"))
                else:
                    result = adapter.model_change(*args)
            case "POST","/v1/host/deliver":
                payload = body.get("payload")
                require(isinstance(payload,dict) and isinstance(payload.get("kind"),str)
                        and payload["kind"] in TASK_INPUTS | {"chat","queued_chat","peer","trainer_answer"},
                        422,"invalid_host_input")
                did = string(body,"delivery_id")
                if payload["kind"]=="trainer_answer":
                    trainer_answer(did,payload,adapter.bound_session(),pinned)
                result = {"receipt": adapter.deliver(did,payload)}
            case "POST","/v1/host/find-chat":
                payload = body.get("payload")
                did = string(body,"delivery_id")
                require(isinstance(payload,dict) and payload.get("kind") in ("chat","queued_chat"),422,"invalid_host_input")
                session = adapter.bound_session()
                queued = payload["kind"]=="queued_chat"
                require(did==("queued-chat-" if queued else "chat-")+string(payload,"message_id")
                        and (not queued or payload.get("session")==session),422,"invalid_host_input")
                result = {"receipt":adapter.lookup_delivery(did,payload,session=session)}
            case "POST","/v1/host/find-peer":
                payload = body.get("payload")
                did = string(body,"delivery_id")
                require(valid_guid(did) and isinstance(payload,dict) and payload.get("kind")=="peer"
                        and payload.get("to_worker")==worker_id and valid_guid(payload.get("message_id")),
                        422,"invalid_host_input")
                result = {"receipt":adapter.lookup_delivery(did,payload,session=adapter.bound_session())}
            case "POST","/v1/host/find-task":
                payload = body.get("payload")
                did = string(body,"delivery_id")
                require(isinstance(payload,dict) and isinstance(payload.get("kind"),str)
                        and payload["kind"] in TASK_INPUTS and valid_guid(did)
                        and valid_guid(payload.get("task_id")) and valid_guid(payload.get("assignment_id")),
                        422,"invalid_host_input")
                result = {"receipt":adapter.lookup_delivery(did,payload,session=adapter.bound_session())}
            case "POST","/v1/host/find-trainer-answer":
                payload = body.get("payload")
                did,session = string(body,"delivery_id"),adapter.bound_session()
                trainer_answer(did,payload,session,pinned)
                result = {"receipt":adapter.lookup_delivery(did,payload,session=session)}
            case "POST","/v1/host/origin":
                result = {"origin":adapter.origin(string(body,"native_id"))}
            case "POST","/v1/host/turn":
                result = {"native_id":adapter.input_for_turn(string(body,"session"),string(body,"turn_id"))}
            case "POST","/v1/host/cleanup-evidence":
                result = {"proof":adapter.cleanup_evidence(string(body,"assignment_id"),string(body,"kind"))}
            case "POST","/v1/host/answer-question":
                require(isinstance(body.get("original"),dict) and isinstance(body.get("answer"),dict),422,"invalid_question")
                result = {"proof":adapter.resolve_question(body["original"],body["answer"])}
            case _:
                raise Fault(404, "unknown_host_route")
        return 200, "application/json", result
    return serve_authenticated(identities,address,context,handle)


def trainer_answer(did,payload,session,pinned):
    from .question_forms import validate_form, validate_reply

    require(pinned and isinstance(payload,dict)
            and set(payload)=={"kind","session","source_turn_id","question_id","questions","answer"}
            and payload["kind"]=="trainer_answer" and payload["session"]==session
            and valid_guid(payload["question_id"]) and did=="trainer-answer-"+payload["question_id"],
            422,"invalid_trainer_answer_delivery")
    string(payload,"source_turn_id")
    validate_form(payload)
    validate_reply(payload["answer"],payload)
