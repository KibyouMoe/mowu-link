"""DSH plugin transport, scoped to an owner-private Unix directory and a single session."""
import base64
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

from . import peer_files
from .agent_tools import TOOLSET
from .dsh_approvals import DshApprovals
from .messages import valid_guid
from .storage import Fault, encode, require, string

NATIVE_TOOLS = frozenset(tool["name"] for tool in TOOLSET)
MAX_WORKSPACE_ARTIFACT = 16 * 1024 * 1024


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
    rejects_busy_before_answer_write = True

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

    def handoff_activity(self, session):
        result = self.request("activity",session)
        require(type(result.get("busy")) is bool and type(result.get("workspace_pending",False)) is bool,
                502,"invalid_host_observation")
        return result

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

    def scoped_cleanup_evidence(self, session, assignment_id, kind, bindings):
        return self.request("cleanup-evidence",session,
                            {"assignment_id":assignment_id,"kind":kind,"bindings":bindings}).get("proof")

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


def stage_workspace_artifact(bound, callback, body, source, size):
    fields = {"session","turn_id","native_id","call_id","name","size_bytes","sha256"}
    require(set(body)==fields and type(body["size_bytes"]) is int and body["size_bytes"]==size,
            422,"invalid_workspace_artifact")
    session,turn,native = (string(body,key) for key in ("session","turn_id","native_id"))
    call = string(body,"call_id")
    bound.prove_native_input(session,turn,native)
    origin = bound.origin(native,session=session)
    require(origin["kind"] in ("task","task_update","task_probe","question_answer"),403,"wrong_artifact_origin")
    scope = {"session":session,"task_id":origin["task_id"],"assignment_id":origin["assignment_id"]}
    require(callback.request("POST","worker/workspace-state",scope)=={**scope,"state":"active"},
            409,"assignment_workspace_inactive")
    data = source.read(size)
    meta = peer_files.metadata(body)
    require(len(data)==size and hashlib.sha256(data).hexdigest()==meta["sha256"],422,"digest_mismatch")
    key = "dsh-artifact-"+hashlib.sha256(encode([scope,meta]).encode()).hexdigest()
    result = callback.request("POST","worker/media/uploads",meta,key)
    route = "worker/media/uploads/"+result["upload_id"]
    while not result["complete"]:
        offset = result["next_offset"]
        require(type(offset) is int and 0<=offset<size,409,"invalid_upload_offset")
        chunk = data[offset:offset+peer_files.CHUNK]
        result = callback.request("PATCH",route,chunk,headers={"Upload-Offset":str(offset)})
        require(result["next_offset"]==offset+len(chunk),409,"invalid_upload_ack")
    require(result["next_offset"]==size and valid_guid(result.get("attachment_id")),409,"unconfirmed_workspace_upload")
    return callback.request("POST","worker/native-tool",{"session":session,"turn_id":turn,"call_id":call,
        "tool":"worker_stage_artifacts","arguments":{"attachment_ids":[result["attachment_id"]]}})


@contextmanager
def tool_listener(directory, adapter, worker, *, family=None):
    from socketserver import UnixStreamServer
    endpoint = private_directory(directory) / "worker.sock"
    approvals = DshApprovals(adapter,worker) if family is None else None
    stop = Event()

    def recover_approvals():
        while not stop.wait(.5):
            try:
                candidates = (port.approvals for port in family.enrolled_ports()) if family else (approvals,)
                for queue in candidates:
                    queue.settle(stop)
            except OperationalError:
                continue

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def do_POST(self):
            self.connection.settimeout(8)
            try:
                workspace = self.path=="/workspace-artifact"
                require(self.path in ("/tool","/approval","/approval/attempt","/workspace-state","/workspace-artifact"),
                        404,"unknown_route")
                size = int(self.headers.get("Content-Length","-1"))
                if workspace:
                    header = self.headers.get("X-Mowu-Workspace","")
                    require(0<=size<=MAX_WORKSPACE_ARTIFACT and 0<len(header)<=4096 and
                            self.headers.get("Content-Type")=="application/octet-stream",422,"invalid_workspace_artifact")
                    body = json.loads(base64.b64decode(header+"="*(-len(header)%4),altchars=b"-_",validate=True))
                else:
                    require(0<size<=1048576,422,"invalid_envelope")
                    body = json.loads(self.rfile.read(size))
                require(isinstance(body,dict),422,"invalid_envelope")
                if workspace or self.path in ("/tool","/workspace-state"):
                    session = string(body,"session")
                    selected = family.port(session) if family else None
                    bound,callback = (selected.adapter,selected.callback) if selected else (adapter,worker)
                if workspace:
                    result = stage_workspace_artifact(bound,callback,body,self.rfile,size)
                elif self.path=="/workspace-state":
                    result = callback.request("POST","worker/workspace-state",body)
                elif self.path=="/tool":
                    fields = {"session","turn_id","native_id","tool","call_id","arguments"}
                    require(set(body) in (fields,fields|{"native_direct"})
                            and string(body,"tool") in NATIVE_TOOLS,422,"invalid_envelope")
                    if "native_direct" in body:
                        require(body["tool"]=="worker_ask_trainer" and body["native_direct"] is True
                                and not string(body,"native_id").startswith("mowulink-")
                                and session==bound.bound_session(),403,"wrong_native_session")
                    else:
                        bound.prove_native_input(session,string(body,"turn_id"),string(body,"native_id"))
                    del body["native_id"]
                    result = callback.request("POST","worker/native-tool",body)
                else:
                    require(set(body)=={"approval_id","data"} and valid_guid(body["approval_id"])
                            and isinstance(body["data"],dict),422,"invalid_approval_envelope")
                    session = string(body["data"],"session")
                    selected = family.port(session) if family else None
                    bound,queue = (selected.adapter,selected.approvals) if selected else (adapter,approvals)
                    require(session==bound.bound_session(),403,"wrong_native_session")
                    if self.path=="/approval":
                        result = queue.offer(body["approval_id"],body["data"])
                    else:
                        result = queue.attempt(body["approval_id"],body["data"])
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
