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
        return {"session":string(info,"session"),"next_turn":info.get("next_turn") is True}

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
