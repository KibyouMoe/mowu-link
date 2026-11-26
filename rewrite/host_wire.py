"""Authenticated Worker-to-adapter port. One server is bound to one worker and native session."""
from urllib.error import URLError

from .storage import Fault, require, string
from .wire import serve_authenticated


class RemoteAdapter:
    def __init__(self, client):
        self.client = client

    def request(self, *args):
        try:
            return self.client.request(*args)
        except Fault as err:
            if err.status in (401,403) and err.code in ("unauthenticated","wrong_adapter_owner"):
                raise Fault(err.status,"adapter_identity_unconfirmed") from err
            raise
        except (URLError,OSError):
            raise Fault(503,"adapter_connection_unconfirmed",True) from None

    def busy(self):
        reply = self.request("GET", "host/activity")
        require(type(reply.get("busy")) is bool, 502, "invalid_host_observation")
        return reply["busy"]

    def bound_session(self):
        return self.session_info()["session"]

    def session_info(self):
        info = self.request("GET","host/session")
        return {"session":string(info,"session"),"next_turn":info.get("next_turn") is True,
                "context_usage":info.get("context_usage") is True,"model_view":info.get("model_view") is True,
                "model_change":info.get("model_change") is True,"goal_control":info.get("goal_control") is True,
                "compaction":info.get("compaction") is True,"session_catalog":info.get("session_catalog") is True}

    def context_usage(self):
        return self.request("GET","host/context")

    def session_catalog(self):
        return self.request("GET","host/session-catalog")

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

    def deliver(self, did, payload):
        return self.request("POST", "host/deliver", {"delivery_id":did, "payload":payload})["receipt"]

    def origin(self, native_id):
        return self.request("POST", "host/origin", {"native_id":native_id})["origin"]

    def input_for_turn(self, session, turn):
        return self.request("POST", "host/turn", {"session":session, "turn_id":turn})["native_id"]

    def cleanup_evidence(self, assignment_id, kind):
        return self.request("POST", "host/cleanup-evidence", {"assignment_id":assignment_id,"kind":kind})["proof"]

    def resolve_question(self, original, answer):
        return self.request("POST","host/answer-question",{"original":original,"answer":answer})["proof"]


def serve_adapter(adapter, worker_id, identities, address, context):
    def handle(actor, method, path, body, _headers):
        require(actor == "worker/" + worker_id, 403, "wrong_adapter_owner")
        match method,path:
            case "GET","/v1/host/session":
                info = getattr(adapter,"session_info",None)
                result = info() if info else {"session":adapter.bound_session(),"next_turn":False}
            case "GET","/v1/host/activity":
                result = {"busy":adapter.busy()}
            case "GET","/v1/host/context":
                result = adapter.context_usage()
            case "GET","/v1/host/session-catalog":
                result = adapter.session_catalog()
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
                require(isinstance(payload,dict) and payload.get("kind") in (
                    "task","task_update","task_probe","task.shelved","task.cancel","task.transfer_out",
                    "question_answer","chat","queued_chat","peer"), 422, "invalid_host_input")
                result = {"receipt": adapter.deliver(string(body,"delivery_id"),payload)}
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
