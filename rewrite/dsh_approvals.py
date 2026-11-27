"""Recover DSH approval closures from the original adapter and native session."""
import hashlib
import json
import time
from urllib.error import URLError

from .approvals import dsh_outcomes, validate_dsh_proof, validate_request
from .storage import Fault, encode, require


class DshApprovals:
    def __init__(self,adapter,worker):
        self.adapter,self.worker = adapter,worker
        with adapter.db.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS dsh_approvals (
                approval_id TEXT PRIMARY KEY, request TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0,
                attempted_response TEXT, closure TEXT
            )""")

    def offer(self,aid,request):
        validate_request(request)
        require(request["item_id"]==aid,409,"native_approval_request_mismatch")
        raw = encode(request)
        with self.adapter.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO dsh_approvals(approval_id,request) VALUES (?,?)",(aid,raw))
            row = db.execute("SELECT request,closure FROM dsh_approvals WHERE approval_id=?",(aid,)).fetchone()
            require(row["request"]==raw,409,"native_approval_request_mismatch")
            if row["closure"]:
                return json.loads(row["closure"])["router"]
        try:
            return self.publish(aid,request)
        except Fault as error:
            if error.code=="approval_relay_unavailable":
                with self.adapter.db.transaction() as db:
                    db.execute("DELETE FROM dsh_approvals WHERE approval_id=? AND published=0",(aid,))
            raise

    def publish(self,aid,request):
        reply = self.worker.request("POST","worker/approvals/"+aid,request)
        digest = hashlib.sha256(encode(request).encode()).hexdigest()
        require(isinstance(reply,dict) and reply.get("approval_id")==aid and reply.get("request")==request
                and reply.get("request_digest")==digest,502,"wrong_approval_receipt")
        with self.adapter.db.transaction() as db:
            db.execute("UPDATE dsh_approvals SET published=1 WHERE approval_id=?",(aid,))
        return reply

    def attempt(self,aid,body):
        require(set(body)=={"session","request_digest","attempted_response"},422,"invalid_approval_response")
        with self.adapter.db.transaction() as db:
            row = db.execute("SELECT * FROM dsh_approvals WHERE approval_id=?",(aid,)).fetchone()
        require(row and row["closure"] is None and row["attempted_response"] is None,409,"approval_response_already_attempted")
        offer = json.loads(row["request"])
        require(offer["session"]==body["session"],403,"wrong_native_session")
        reply = self.publish(aid,offer)
        require(reply["request_digest"]==body["request_digest"],409,"approval_view_changed")
        response = body["attempted_response"]
        require(isinstance(response,dict) and set(response)=={"source","decision"},422,"invalid_approval_response")
        if response["source"]=="operator":
            deadline = reply.get("expires_at")
            require(reply["state"]=="decided" and type(deadline) in (int,float) and time.time()<deadline
                    and reply["decision"]==response["decision"] and response["decision"] in offer["decisions"],
                    409,"approval_response_conflict")
        else:
            require(response=={"source":"expiry","decision":"cancel"} and reply["state"]=="expired"
                    and "cancel" in offer["decisions"],409,"approval_response_conflict")
        with self.adapter.db.transaction() as db:
            saved = db.execute("""UPDATE dsh_approvals SET attempted_response=? WHERE approval_id=?
                                  AND attempted_response IS NULL AND closure IS NULL""",(encode(response),aid)).rowcount
            require(saved,409,"approval_response_already_attempted")
        return {"approval_id":aid,"attempted_response":response}

    def settle(self,stop):
        with self.adapter.db.transaction() as db:
            pending = db.execute("SELECT * FROM dsh_approvals WHERE closure IS NULL").fetchall()
        for row in pending:
            if stop.is_set():
                return
            try:
                self.check_closure(row)
            except (Fault,URLError,OSError):
                continue

    def check_closure(self,row):
        aid = row["approval_id"]
        request = json.loads(row["request"])
        session,turn = request["session"],request["turn_id"]
        require(session==self.adapter.bound_session(),403,"wrong_native_session")
        proof = self.adapter.host.request("approval-proof",session,{"approval_id":aid,"turn_id":turn}).get("proof")
        if proof is None:
            return
        with self.adapter.db.transaction() as db:
            row = db.execute("SELECT * FROM dsh_approvals WHERE approval_id=?",(aid,)).fetchone()
        if not row or row["closure"] is not None:
            return
        digest = hashlib.sha256(row["request"].encode()).hexdigest()
        response = json.loads(row["attempted_response"]) if row["attempted_response"] else None
        validate_dsh_proof(proof,aid,turn,dsh_outcomes(response))
        body = {"session":session,"request_digest":digest,"attempted_response":response,"native_proof":proof}
        if not row["published"]:
            require(response is None,409,"native_approval_proof_unconfirmed")
            # An already settled native request has nothing for an operator to decide.
            body["request"] = request
        receipt = self.worker.request("PUT","worker/approvals/"+aid+"/closed",body)
        require(isinstance(receipt,dict) and receipt.get("approval_id")==aid and receipt.get("state")=="native_closed"
                and receipt.get("request")==request and receipt.get("request_digest")==digest
                and receipt.get("attempted_response")==response,502,"wrong_approval_closure")
        # The previous rewrite may already have closed a published approval before a lost reply.
        if not row["published"] or "native_proof" in receipt:
            require(receipt.get("native_proof")==proof,502,"wrong_approval_closure")
        with self.adapter.db.transaction() as db:
            db.execute("UPDATE dsh_approvals SET closure=? WHERE approval_id=?",
                       (encode({"native":proof,"router":receipt}),aid))
