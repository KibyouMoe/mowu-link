"""DSH plugin transport, scoped to an owner-private Unix directory and a single session."""
from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler
import json
import os
import socket
from socketserver import ThreadingMixIn
from sqlite3 import OperationalError
from threading import Event, Thread
from urllib.error import URLError

from .agent_tools import TOOLSET
from .dsh_approvals import DshApprovals
from .messages import valid_guid
from .storage import Fault, encode, require, string

NATIVE_TOOLS = frozenset(tool["name"] for tool in TOOLSET)


def private_directory(path):
    info = path.lstat()
    require(path.is_absolute() and path.is_dir() and not path.is_symlink()
            and info.st_uid == os.getuid() and not info.st_mode & 0o077, 403, "private_runtime_required")
    return path


class UnixConnection(HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost",timeout=8)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class DshActivation:
    def __init__(self,host,session):
        self.host,self.session = host,session

    @property
    def state(self):
        proof = self.host.request("load-state",self.session)
        require(proof.get("session")==self.session and proof.get("state") in ("cold","loaded"),
                502,"invalid_host_observation")
        return proof["state"]

    def activate(self,session,operation_id):
        require(session==self.session,409,"wrong_native_session")
        proof = self.host.request("activate",session,{"operation_id":operation_id})
        require(proof=={"session":session,"operation_id":operation_id,"state":"loaded"},502,"invalid_native_activation")
        return proof


class DshHost:
    # DSH Agent.followup places an identified input at the next-turn boundary.
    next_turn = True
    rejects_busy_before_task_write = True

    def __init__(self, directory):
        self.directory = private_directory(directory)

    def request(self, operation, session, fields=None):
        client = UnixConnection(private_directory(self.directory) / "dsh.sock")
        try:
            body = encode({"operation":operation,"session":session,**(fields or {})}).encode()
            client.request("POST","/host",body=body,headers={"Content-Type":"application/json"})
            response = client.getresponse()
            raw = response.read(1048577)
            require(len(raw) <= 1048576,502,"invalid_host_observation")
            result = json.loads(raw)
            require(isinstance(result,dict),502,"invalid_host_observation")
            if response.status != 200:
                raise Fault(response.status,result.get("error","dsh_host_unconfirmed"),response.status>=500)
            return result
        except (HTTPException,OSError,ValueError):
            raise Fault(503,"adapter_connection_unconfirmed",True) from None
        finally:
            client.close()

    def busy(self, session):
        value = self.request("activity",session).get("busy")
        require(type(value) is bool,502,"invalid_host_observation")
        return value

    def context_usage(self, session):
        return self.request("context-usage",session)

    def capabilities(self, session):
        try:
            return self.request("capabilities",session)
        except Fault as error:
            if error.code == "unsupported_operation":
                return {}
            raise

    def session_catalog(self,session):
        return self.request("session-catalog",session)

    def stop_private(self,session):
        return self.request("stop-private",session)

    def compact(self, session, command_id, *, start):
        return self.request("compact-start" if start else "compact-status",session,{"command_id":command_id})

    def lookup_answer(self,session,turn,native):
        return self.request("question-proof",session,{"turn_id":turn,"native_question_id":native})["proof"]

    def resolve_question(self,original,answer):
        fields = {key:original[key] for key in ("turn_id","native_question_id")}
        return self.request("question-answer",original["session"],{**fields,"original":original,"answer":answer})["proof"]

    def goal_view(self, session):
        return self.request("goal-view",session)

    def goal_change(self, session, action, *, objective=None):
        fields = {"action":action}
        if objective is not None:
            fields["objective"] = objective
        return self.request("goal-change",session,fields)

    def model_view(self, session, kind):
        return self.request("model-view",session,{"kind":kind})

    def model_change(self, session, kind, value, *, expected_route=None):
        fields = {"kind":kind,"value":value}
        if expected_route is None:
            return self.request("model-change",session,fields)
        return self.request("model-change-if-route",session,{**fields,"expected_route":expected_route})

    @staticmethod
    def delivery_native_id(session, did, digest):
        parts = encode([session,did,digest])
        return "mowulink-"+hashlib.sha256(parts.encode()).hexdigest()

    def delivery(self, operation, session, did, payload, digest, **fields):
        result = self.request(operation,session,{"delivery_id":did,"payload_json":encode(payload),"digest":digest,**fields})
        require("receipt" in result,502,"invalid_host_observation")
        return result["receipt"]

    def deliver(self, session, did, payload, digest):
        return self.delivery("deliver",session,did,payload,digest)

    def deliver_control(self, session, did, payload, digest, bindings):
        return self.delivery("deliver-control",session,did,payload,digest,bindings=bindings)

    def lookup_expected(self, session, did, payload, digest):
        return self.delivery("lookup",session,did,payload,digest)

    def media_operation(self, operation, session, did, payload, digest, files):
        expected = [self.directory/"media"/item["sha256"] for item in payload["attachments"]]
        require(files==expected,409,"dsh_media_cache_mismatch")
        return self.delivery(operation,session,did,payload,digest)

    def deliver_media(self, session, did, payload, digest, files):
        return self.media_operation("deliver",session,did,payload,digest,files)

    def lookup_media(self, session, did, payload, digest, files):
        return self.media_operation("lookup",session,did,payload,digest,files)


@contextmanager
def tool_listener(directory, adapter, worker):
    from socketserver import UnixStreamServer
    endpoint = private_directory(directory) / "worker.sock"
    approvals = DshApprovals(adapter,worker)
    stop = Event()

    def recover_approvals():
        while not stop.wait(.5):
            try:
                approvals.settle(stop)
            except OperationalError:
                continue

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def do_POST(self):
            self.connection.settimeout(8)
            try:
                require(self.path in ("/tool","/approval","/approval/attempt"),404,"unknown_route")
                size = int(self.headers.get("Content-Length","0"))
                require(0 < size <= 1048576,422,"invalid_envelope")
                body = json.loads(self.rfile.read(size))
                require(isinstance(body,dict),422,"invalid_envelope")
                if self.path=="/tool":
                    require(set(body)=={"session","turn_id","native_id","tool","call_id","arguments"}
                            and string(body,"tool") in NATIVE_TOOLS,422,"invalid_envelope")
                    adapter.prove_native_input(string(body,"session"),string(body,"turn_id"),string(body,"native_id"))
                    del body["native_id"]
                    result = worker.request("POST","worker/native-tool",body)
                else:
                    require(set(body)=={"approval_id","data"} and valid_guid(body["approval_id"])
                            and isinstance(body["data"],dict),422,"invalid_approval_envelope")
                    require(string(body["data"],"session")==adapter.bound_session(),403,"wrong_native_session")
                    if self.path=="/approval":
                        result = approvals.offer(body["approval_id"],body["data"])
                    else:
                        result = approvals.attempt(body["approval_id"],body["data"])
                status = 200
            except Fault as error:
                status, result = error.status, {"error":error.code}
            except (ValueError,TypeError):
                status, result = 422, {"error":"invalid_envelope"}
            except (OSError,URLError):
                status, result = 503, {"error":"worker_unconfirmed"}
            raw = encode(result).encode()
            self.send_response(status)
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    class Server(ThreadingMixIn,UnixStreamServer):
        daemon_threads = True

    with Server(str(endpoint),Handler) as server:
        info = endpoint.stat()
        runner = Thread(target=server.serve_forever,kwargs={"poll_interval":0.05},daemon=True)
        recovery = Thread(target=recover_approvals,daemon=True)
        runner.start()
        recovery.start()
        try:
            yield
        finally:
            stop.set()
            server.shutdown()
            runner.join()
            recovery.join(timeout=16)
            if endpoint.exists() and os.path.samestat(info,endpoint.lstat()):
                endpoint.unlink()
