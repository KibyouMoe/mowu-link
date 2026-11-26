"""Pass a decision to its live, bound App Server request only once."""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import re
from sqlite3 import Error as SQLiteError
from threading import Event, Lock, Thread
import time

from .approvals import DECISIONS, TTL_SECONDS, validate_file_changes, validate_request
from .storage import Database, Fault, encode, guid, require


SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_approval_intents (
    approval_id TEXT PRIMARY KEY, session TEXT NOT NULL, turn_id TEXT NOT NULL,
    item_id TEXT NOT NULL, request_digest TEXT NOT NULL, router_receipt_digest TEXT
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


class CodexApprovals:
    def __init__(self,rpc,session,*,commands=True,files=False,adapter_db=None):
        self.rpc,self.session = rpc,session
        self.commands,self.files = commands,files
        self.clock = time.monotonic
        self.lock = Lock()
        self.live = {}
        self.file_changes = {}
        self.ledger = Database(adapter_db,SCHEMA) if adapter_db is not None else None
        self.cold_receipts = deque()
        if self.ledger is not None:
            with self.ledger.transaction() as db:
                self.cold_receipts.extend(tuple(row) for row in db.execute("""SELECT approval_id,request_digest
                    FROM codex_approval_intents WHERE session=? AND router_receipt_digest IS NULL ORDER BY rowid""",
                    (session,)))

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
            if rid in self.live:
                self.live[rid].closed = True

    def retire_turn(self,turn):
        if self.files:
            self.file_changes = {key:changes for key,changes in self.file_changes.items() if key[0]!=turn}
        with self.lock:
            for item in self.live.values():
                if item.request["turn_id"]==turn:
                    item.retired = True

    def answer(self,item,response):
        item.attempted_response = response
        self.rpc.send({"id":item.request_id,"result":{"decision":response["decision"]}})

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

    def recover_receipt(self,worker,approval_id,request_digest):
        original = {"session":self.session,"request_digest":request_digest}
        path = "worker/approvals/"+approval_id+"/receipt"
        reply = worker.request("POST",path,original)
        require(isinstance(reply,dict),502,"wrong_approval_closure")
        digest = reply.get("receipt_digest")
        require(isinstance(digest,str) and re.fullmatch("[0-9a-f]{64}",digest) and
                reply==dict(original,approval_id=approval_id,state="native_closed",receipt_digest=digest),
                502,"wrong_approval_closure")
        self.save_receipt(approval_id,request_digest,digest)

    def confirm(self,worker,route,item,attempted):
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
            # Previous-instance entries can only look up a stored closure, never answer or submit one.
            while self.cold_receipts and not stop.is_set():
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
