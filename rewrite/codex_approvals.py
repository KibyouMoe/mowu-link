"""Pass a decision to its live, bound App Server request only once."""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import re
from sqlite3 import Error as SQLiteError
from threading import Event, Lock, Thread
import time

from .agent_tools import formal_report_origin
from .approvals import DECISIONS, REPORT_APPROVAL, REPORT_SERVER, TTL_SECONDS, validate_file_changes, validate_request
from .messages import valid_guid
from .storage import Database, Fault, encode, guid, require, string


SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_approval_intents (
    approval_id TEXT PRIMARY KEY, session TEXT NOT NULL, turn_id TEXT NOT NULL,
    item_id TEXT NOT NULL, request_digest TEXT NOT NULL, router_receipt_digest TEXT
);
CREATE TABLE IF NOT EXISTS codex_approval_resolutions (
    approval_id TEXT PRIMARY KEY REFERENCES codex_approval_intents(approval_id),
    resolution TEXT NOT NULL
);
"""


@dataclass
class NativeApproval:
    id: str
    request_id: int | str
    request: dict
    digest: str
    deadline: float
    attempted_response: dict | None = None
    closed: bool = False
    retired: bool = False
    expired: bool = False
    resolution_saved: bool = False


class CodexApprovals:
    def __init__(self,rpc,session,*,commands=True,files=False,report_server=None,adapter_db=None):
        self.rpc,self.session = rpc,session
        self.commands,self.files = commands,files
        require(report_server is None or (isinstance(report_server,str) and REPORT_SERVER.fullmatch(report_server)
                                          and adapter_db is not None),422,"invalid_report_approval_server")
        self.report_server = report_server
        self.clock = time.monotonic
        self.lock = Lock()
        self.live = {}
        self.file_changes = {}
        self.ledger = Database(adapter_db,SCHEMA) if adapter_db is not None else None
        self.cold_receipts = deque()
        if self.ledger is not None:
            with self.ledger.transaction() as db:
                self.cold_receipts.extend(tuple(row) for row in db.execute("""SELECT approval_id,request_digest,
                    turn_id,item_id,resolution FROM codex_approval_intents i
                    LEFT JOIN codex_approval_resolutions USING(approval_id)
                    WHERE session=? AND router_receipt_digest IS NULL ORDER BY i.rowid""",(session,)))

    def observe_item(self,method,params):
        if not isinstance(params,dict) or params.get("threadId")!=self.session:
            return
        turn,item = params.get("turnId"),params.get("item")
        if (not isinstance(turn,str) or not isinstance(item,dict) or item.get("type")!="fileChange"
                or not isinstance(item.get("id"),str)):
            return
        key = (turn,item["id"])
        if method=="item/completed":
            self.file_changes.pop(key,None)
            return
        changes = item.get("changes")
        try:
            require(item.get("status")=="inProgress",422,"invalid_native_file_change")
            validate_file_changes(changes)
        except Fault:
            changes = None
        if key in self.file_changes and self.file_changes[key]!=changes:
            changes = None
        self.file_changes[key] = changes

    def offer_file(self,packet):
        params,rid = self.request_binding(packet)
        turn,item = params.get("turnId"),params.get("itemId")
        require(isinstance(turn,str) and isinstance(item,str),422,"invalid_native_file_request")
        changes = self.file_changes.get((turn,item))
        require(changes is not None and params.get("grantRoot") is None,422,"native_file_change_unconfirmed")
        data = {"kind":"file_change","session":self.session,"turn_id":turn,"item_id":item,
                "changes":changes,"decisions":list(DECISIONS)}
        if params.get("reason") is not None:
            data["reason"] = params["reason"]
        self.remember(rid,data)

    def request_binding(self,packet):
        params,rid = packet.get("params"),packet.get("id")
        require(type(rid) in (int,str) and isinstance(params,dict) and params.get("threadId")==self.session,
                403,"wrong_native_approval_source")
        return params,rid

    def offer(self,packet):
        params,rid = self.request_binding(packet)
        choices = params.get("availableDecisions")
        require(isinstance(choices,list) and not params.get("additionalPermissions")
                and not params.get("networkApprovalContext"),422,"unsupported_native_approval")
        data = {"session":self.session,"turn_id":params.get("turnId"),"item_id":params.get("itemId"),
                "command":params.get("command"),"cwd":params.get("cwd"),
                "environment_id":params.get("environmentId"),
                "decisions":[choice for choice in DECISIONS if choice in choices]}
        self.remember(rid,data)

    def report_request(self,packet):
        params = packet.get("params")
        if self.report_server is None or not isinstance(params,dict) or params.get("serverName")!=self.report_server:
            return False
        metadata = params.get("_meta")
        return (isinstance(metadata,dict) and metadata.get("codex_approval_kind")=="mcp_tool_call"
                and metadata.get("tool_name")==REPORT_APPROVAL)

    def offer_report(self,packet,adapter):
        params,rid = self.request_binding(packet)
        require(adapter is not None and self.report_request(packet) and params.get("environmentId") is None
                and params.get("mode")=="form" and params.get("requestedSchema")=={"properties":{},"type":"object"},
                422,"unsupported_native_report_approval")
        args = params["_meta"].get("tool_params")
        fields = {"task_id","assignment_id","summary"}
        require(isinstance(args,dict) and fields<=set(args)<=fields|{"outcome","artifact_ids"}
                and args.get("outcome","completed")=="completed" and valid_guid(args["task_id"])
                and valid_guid(args["assignment_id"]) and bool(string(args,"summary")),
                422,"invalid_report_approval_arguments")
        artifacts = args.get("artifact_ids",[])
        require(isinstance(artifacts,list) and all(valid_guid(item) for item in artifacts),
                422,"invalid_report_approval_arguments")
        turn = string(params,"turnId")
        try:
            native = adapter.input_for_turn(self.session,turn)
        except Fault as error:
            if error.code!="native_turn_unconfirmed":
                raise
            # The tool request can arrive before the Worker has fetched the host receipt.
            adapter.control_bindings(args)
            native = adapter.input_for_turn(self.session,turn)
        origin = adapter.origin(native,session=self.session)
        require(formal_report_origin(origin,args["task_id"],args["assignment_id"]),403,"wrong_report_origin")
        self.remember(rid,{"kind":REPORT_APPROVAL,"session":self.session,"turn_id":turn,"item_id":"mcp:"+encode(rid),
                           "server":self.report_server,"task_id":args["task_id"],"assignment_id":args["assignment_id"],
                           "arguments_digest":hashlib.sha256(encode(args).encode()).hexdigest(),"decisions":list(DECISIONS)})

    def remember(self,rid,data):
        validate_request(data)
        with self.lock:
            current = self.live.get(rid)
            if current:
                require(current.request==data,409,"native_approval_conflict")
                return
            digest = hashlib.sha256(encode(data).encode()).hexdigest()
            item = NativeApproval(guid(),rid,data,digest,self.clock()+TTL_SECONDS)
            if self.ledger is not None:
                # A disconnected Worker may never receive this request. Keep correlation data only.
                try:
                    with self.ledger.transaction() as db:
                        db.execute("INSERT INTO codex_approval_intents VALUES (?,?,?,?,?,NULL)",
                                   (item.id,self.session,data["turn_id"],data["item_id"],digest))
                except SQLiteError:
                    raise Fault(503,"native_approval_journal_unconfirmed",True) from None
            self.live[rid] = item

    def closed(self,params):
        if not isinstance(params,dict) or params.get("threadId")!=self.session:
            return
        rid = params.get("requestId")
        if type(rid) not in (int,str):
            return
        with self.lock:
            item = self.live.get(rid)
            if item:
                item.closed = True
                try:
                    self.journal_resolution(item)
                except SQLiteError:
                    # The live relay will retry the same write before forwarding the closure.
                    pass

    def journal_resolution(self,item):
        if self.ledger is not None and not item.resolution_saved:
            resolution = {"request_id":item.request_id,"attempted_response":item.attempted_response}
            with self.ledger.transaction() as db:
                db.execute("INSERT INTO codex_approval_resolutions VALUES (?,?) ON CONFLICT(approval_id) DO NOTHING",
                           (item.id,encode(resolution)))
            item.resolution_saved = True

    def retire_turn(self,turn):
        if self.files:
            self.file_changes = {key:changes for key,changes in self.file_changes.items() if key[0]!=turn}
        with self.lock:
            for item in self.live.values():
                if item.request["turn_id"]==turn:
                    item.retired = True

    def answer(self,item,response):
        item.attempted_response = response
        if item.request.get("kind")==REPORT_APPROVAL:
            self.send_report_decision(item.request_id,response["decision"])
        else:
            self.rpc.send({"id":item.request_id,"result":{"decision":response["decision"]}})

    def send_report_decision(self,rid,decision):
        # No form input, stored approval, session grant, or tool result is sent here.
        result = {"action":"accept","content":{}} if decision=="accept" else {"action":"decline"}
        self.rpc.send({"id":rid,"result":result})

    def expire(self,item):
        # Both the native timer and a newly arrived Router response consult the same deadline under the lock.
        if (item.attempted_response is None and not (item.closed or item.retired)
                and self.clock()>=item.deadline and "cancel" in item.request["decisions"]):
            item.expired = True
            self.answer(item,{"source":"native_deadline","decision":"cancel"})

    def save_receipt(self,approval_id,request_digest,receipt_digest):
        with self.ledger.transaction() as db:
            changed = db.execute("""UPDATE codex_approval_intents SET router_receipt_digest=?
                WHERE approval_id=? AND session=? AND request_digest=?
                AND (router_receipt_digest IS NULL OR router_receipt_digest=?)""",
                (receipt_digest,approval_id,self.session,request_digest,receipt_digest)).rowcount
            require(changed==1,503,"native_approval_journal_unconfirmed")

    def recover_receipt(self,worker,approval_id,request_digest,turn_id,item_id,resolution):
        original = {"session":self.session,"request_digest":request_digest}
        path = "worker/approvals/"+approval_id
        saved = None
        if resolution is not None:
            try:
                saved = json.loads(resolution)
            except (ValueError,TypeError):
                raise Fault(503,"native_approval_journal_unconfirmed") from None
            require(isinstance(saved,dict) and set(saved)=={"request_id","attempted_response"},
                    503,"native_approval_journal_unconfirmed")
        lookup = original if saved is None else dict(original,attempted_response=saved["attempted_response"])
        try:
            reply = worker.request("POST",path+"/receipt",lookup)
        except Fault as error:
            if error.code!="native_approval_closure_pending" or saved is None:
                raise
            proof = dict(original,turn_id=turn_id,item_id=item_id,**saved)
            reply = worker.request("PUT",path+"/recover-codex-closure",proof)
        require(isinstance(reply,dict),502,"wrong_approval_closure")
        digest = reply.get("receipt_digest")
        require(isinstance(digest,str) and re.fullmatch("[0-9a-f]{64}",digest) and
                reply==dict(original,approval_id=approval_id,state="native_closed",receipt_digest=digest),
                502,"wrong_approval_closure")
        self.save_receipt(approval_id,request_digest,digest)

    def confirm(self,worker,route,item,attempted):
        with self.lock:
            self.journal_resolution(item)
        proof = {"session":self.session,"request_digest":item.digest,"attempted_response":attempted}
        if attempted and attempted["source"]=="native_deadline":
            proof["request"] = item.request
        receipt = worker.request("PUT",route+"/closed",proof)
        require(isinstance(receipt,dict) and receipt.get("approval_id")==item.id and receipt.get("state")=="native_closed"
                and receipt.get("attempted_response")==attempted and receipt.get("request_digest")==item.digest,
                502,"wrong_approval_closure")
        with self.lock:
            if self.live.get(item.request_id) is item:
                if self.ledger is not None:
                    self.save_receipt(item.id,item.digest,hashlib.sha256(encode(receipt).encode()).hexdigest())
                del self.live[item.request_id]

    def advance(self,worker,item):
        route = "worker/approvals/"+item.id
        with self.lock:
            self.expire(item)
            closed,attempted = item.closed,item.attempted_response
            native_timeout = attempted and attempted["source"]=="native_deadline"
        if native_timeout:
            if closed:
                self.confirm(worker,route,item,attempted)
            return

        reply = worker.request("POST",route,item.request)
        require(isinstance(reply,dict) and reply.get("approval_id")==item.id and reply.get("request")==item.request
                and reply.get("request_digest")==item.digest,502,"wrong_approval_receipt")
        with self.lock:
            self.expire(item)
            closed,attempted = item.closed,item.attempted_response
            state = reply.get("state")
            if state=="expired":
                item.expired = True
            if not closed and attempted is None and not item.retired:
                response = None
                if state=="decided":
                    decision = reply.get("decision")
                    expiry = reply.get("expires_at")
                    require(decision in item.request["decisions"] and type(expiry) in (float,int),502,"invalid_approval_receipt")
                    if time.time()<expiry and self.clock()<item.deadline:
                        response = {"source":"operator","decision":decision}
                elif state=="expired" and "cancel" in item.request["decisions"]:
                    response = {"source":"expiry","decision":"cancel"}
                elif state=="assignment_inactive" and item.request.get("kind")==REPORT_APPROVAL:
                    response = {"source":"assignment_inactive","decision":"cancel"}
                if response:
                    self.answer(item,response)
        if closed:
            self.confirm(worker,route,item,attempted)

    @contextmanager
    def run(self,worker):
        stop = Event()
        def enforce_deadlines():
            while not stop.wait(.2):
                with self.lock:
                    for item in self.live.values():
                        try:
                            self.expire(item)
                        except Fault:
                            # An attempted native response is never resent, even if the transport fails.
                            continue

        def pump():
            while not stop.wait(.25):
                with self.lock:
                    live = [item for item in self.live.values() if item.closed or not (item.retired or item.expired)]
                for item in live:
                    if stop.is_set():
                        return
                    try:
                        self.advance(worker,item)
                    except (Fault,OSError,SQLiteError):
                        # Keep the original request available for lookup; the native response is never resent.
                        continue
        def reconcile_cold_receipts():
            while self.cold_receipts and not stop.is_set():
                for _ in range(len(self.cold_receipts)):
                    if stop.is_set():
                        return
                    try:
                        self.recover_receipt(worker,*self.cold_receipts[0])
                    except (Fault,OSError,SQLiteError):
                        self.cold_receipts.rotate(-1)
                    else:
                        self.cold_receipts.popleft()
                if self.cold_receipts:
                    stop.wait(5)

        relay,deadlines = Thread(target=pump,daemon=True),Thread(target=enforce_deadlines,daemon=True)
        recovery = Thread(target=reconcile_cold_receipts,daemon=True)
        relay.start()
        deadlines.start()
        recovery.start()
        try:
            yield
        finally:
            stop.set()
            deadlines.join(timeout=3)
            relay.join(timeout=11)
            recovery.join(timeout=6)
