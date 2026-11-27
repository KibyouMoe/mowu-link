"""Restricted operator decisions for individual native requests."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from . import delivery
from .messages import valid_guid
from .storage import Fault, encode, require, string


TTL_SECONDS = 600
DECISIONS = ("accept","cancel")
REPORT_APPROVAL = "worker_report"
REPORT_SERVER = re.compile(r"[A-Za-z0-9_-]{1,64}")
DSH_NON_GRANT_OUTCOMES = ("rejected","cancelled","unavailable")
SCHEMA = """
CREATE TABLE IF NOT EXISTS native_approvals (
 worker_id TEXT NOT NULL, approval_id TEXT NOT NULL, worker_session TEXT NOT NULL,
 request TEXT NOT NULL, request_digest TEXT NOT NULL, expires_at REAL NOT NULL,
 decision TEXT, closed_at REAL, attempted_response TEXT, native_proof TEXT,
 PRIMARY KEY(worker_id,approval_id)
);
"""


def upgrade(db):
    if "native_proof" not in {column["name"] for column in db.execute("PRAGMA table_info(native_approvals)")}:
        db.execute("ALTER TABLE native_approvals ADD COLUMN native_proof TEXT")


def validate_file_changes(changes):
    def absolute_path(value):
        return (isinstance(value,str) and 0<len(value)<=4096
                and not any(c in value for c in "\0\r\n")
                and (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()))

    require(isinstance(changes,list) and 0<len(changes)<=64,422,"invalid_approval_file_changes")
    for change in changes:
        require(isinstance(change,dict) and set(change)=={"path","kind","diff"}
                and absolute_path(change["path"]) and isinstance(change["diff"],str)
                and "\0" not in change["diff"],422,"invalid_approval_file_change")
        kind = change["kind"]
        require(isinstance(kind,dict),422,"invalid_approval_file_change")
        if kind.get("type")=="update":
            require(set(kind)<={"type","move_path"} and (kind.get("move_path") is None
                    or absolute_path(kind["move_path"])),422,"invalid_approval_file_change")
        else:
            require(set(kind)=={"type"} and kind["type"] in ("add","delete"),422,"invalid_approval_file_change")
    require(len({change["path"] for change in changes})==len(changes) and len(encode(changes))<=65536,
            422,"invalid_approval_file_changes")


def validate_request(request):
    require(isinstance(request,dict),422,"invalid_approval_request")
    common = {"session","turn_id","item_id","decisions"}
    if request.get("kind")==REPORT_APPROVAL:
        require(set(request)==common|{"kind","server","task_id","assignment_id","arguments_digest"},
                422,"invalid_approval_request")
        for field in common-{"decisions"}:
            require(len(string(request,field))<=200 and "\0" not in request[field],422,"invalid_approval_request")
        require(REPORT_SERVER.fullmatch(string(request,"server")) and valid_guid(request["task_id"])
                and valid_guid(request["assignment_id"]) and
                re.fullmatch(r"[0-9a-f]{64}",string(request,"arguments_digest"))
                and request["decisions"]==list(DECISIONS),422,"invalid_report_approval_request")
        return
    if request.get("kind")=="file_change":
        require(common|{"kind","changes"}<=set(request)<=common|{"kind","changes","reason"},
                422,"invalid_approval_request")
        for field in common-{"decisions"}:
            require(len(string(request,field))<=200 and "\0" not in request[field],422,"invalid_approval_request")
        validate_file_changes(request["changes"])
        validate_choices(request)
        return
    fields = {"session","turn_id","item_id","command","cwd","environment_id","decisions"}
    require(fields<=set(request)<=fields|{"arguments","reason"},422,"invalid_approval_request")
    for field in fields-{"decisions","environment_id"}:
        limit = 8192 if field in ("command","cwd") else 200
        require(len(string(request,field))<=limit and "\0" not in request[field],422,"invalid_approval_request")
    environment = request["environment_id"]
    require(environment is None or isinstance(environment,str) and 0<len(environment)<=200
            and "\0" not in environment,422,"invalid_approval_environment")
    if "arguments" in request:
        require(isinstance(request["arguments"],dict) and len(encode(request["arguments"]))<=32768,
                422,"invalid_approval_arguments")
    validate_choices(request)


def validate_choices(request):
    decisions = request["decisions"]
    require(isinstance(decisions,list) and decisions and all(value in DECISIONS for value in decisions)
            and len(decisions)==len(set(decisions)),422,"invalid_approval_choices")
    if "reason" in request:
        require(isinstance(request["reason"],str) and len(request["reason"])<=2000,422,"invalid_approval_reason")


def require_private_store(path):
    if os.name=="posix":
        file = Path(path)
        location = file.resolve().parent.stat()
        if file.is_symlink() or location.st_uid!=os.getuid() or location.st_mode&0o077:
            raise ValueError("native approvals require an owner-private Router state directory")


def record(row,worker_session,now):
    if row["closed_at"] is not None:
        state = "native_closed"
    elif row["worker_session"]!=worker_session:
        state = "disconnected"
    elif now>=row["expires_at"]:
        state = "expired"
    else:
        state = "decided" if row["decision"] is not None else "pending"
    attempted = json.loads(row["attempted_response"]) if row["attempted_response"] is not None else None
    result = {"worker_id":row["worker_id"],"approval_id":row["approval_id"],"state":state,
              "request":json.loads(row["request"]),"request_digest":row["request_digest"],
              "decision":row["decision"],"expires_at":row["expires_at"],"closed_at":row["closed_at"],
              "attempted_response":attempted}
    if row["native_proof"] is not None:
        result["native_proof"] = json.loads(row["native_proof"])
    return result


def report_approval_active(db,orders,wid,session,request):
    if request.get("kind")!=REPORT_APPROVAL:
        return True
    status = orders.assignment_status(db,wid,request["task_id"],
                                      {"session_id":session,"assignment_id":request["assignment_id"]})
    return status["state"]=="active"


def approval_view(db,orders,row,session,now):
    result = record(row,session,now)
    if result["state"] in ("pending","decided") and not report_approval_active(
            db,orders,row["worker_id"],session,result["request"]):
        result["state"] = "assignment_inactive"
    return result


def validate_dsh_proof(proof,aid,turn,outcomes):
    require(isinstance(proof,dict) and set(proof)=={"approval_id","turn_id","asked_seq","decided_seq","outcome"}
            and proof["approval_id"]==aid and proof["turn_id"]==turn
            and type(proof["asked_seq"]) is int and type(proof["decided_seq"]) is int
            and 0<=proof["asked_seq"]<proof["decided_seq"] and proof["outcome"] in outcomes,
            409,"native_approval_proof_unconfirmed")


def dsh_outcomes(attempted):
    if attempted=={"source":"operator","decision":"accept"}:
        return (*DSH_NON_GRANT_OUTCOMES,"allowed-once")
    return DSH_NON_GRANT_OUTCOMES


def close_native(db,orders,wid,aid,row,session,now,body):
    fields = {"session_id","request_digest","attempted_response"}
    require(fields<=set(body)<=fields|{"request","native_proof"},422,"invalid_approval_closure")
    orders.active_session(db,wid,body)
    attempted = body["attempted_response"]
    if attempted is not None:
        require(isinstance(attempted,dict) and set(attempted)=={"source","decision"},422,"invalid_approval_closure")
    stored = encode(attempted)
    native_deadline = attempted=={"source":"native_deadline","decision":"cancel"}
    dsh_proof = "native_proof" in body
    dsh_bootstrap = dsh_proof and "request" in body
    require(("request" in body)==(native_deadline or dsh_bootstrap) and (not dsh_bootstrap or attempted is None)
            and not (native_deadline and dsh_proof),
            422,"invalid_approval_closure")
    stored_proof = None
    if native_deadline or dsh_bootstrap:
        request = body["request"]
        validate_request(request)
        if dsh_bootstrap:
            require(request["item_id"]==aid and "command" in request,409,"approval_request_conflict")
            validate_dsh_proof(body["native_proof"],aid,request["turn_id"],DSH_NON_GRANT_OUTCOMES)
            stored_proof = encode(body["native_proof"])
        raw = encode(request)
        require("cancel" in request["decisions"] and hashlib.sha256(raw.encode()).hexdigest()==body["request_digest"],
                409,"approval_request_conflict")
        if not row:
            # A spent native request can arrive before any acknowledged registration. Never notify an approver here.
            db.execute("""INSERT INTO native_approvals(worker_id,approval_id,worker_session,request,request_digest,
                          expires_at,closed_at,attempted_response,native_proof) VALUES (?,?,?,?,?,?,?,?,?)""",
                       (wid,aid,session,raw,body["request_digest"],now,now,stored,stored_proof))
            return
        require(row["request"]==raw,409,"approval_request_conflict")
    require(row,404,"approval_not_found")
    require(session==row["worker_session"] and body["request_digest"]==row["request_digest"],
            409,"approval_request_conflict")
    if dsh_proof and not dsh_bootstrap:
        request = json.loads(row["request"])
        require(request["item_id"]==aid and "command" in request,409,"approval_request_conflict")
        validate_dsh_proof(body["native_proof"],aid,request["turn_id"],dsh_outcomes(attempted))
        stored_proof = encode(body["native_proof"])
    if row["closed_at"] is not None:
        prior_published_closure = dsh_proof and not dsh_bootstrap and row["native_proof"] is None
        require(row["attempted_response"]==stored and
                (stored_proof is None or row["native_proof"]==stored_proof or prior_published_closure),
                409,"approval_closure_conflict")
        return
    if attempted is not None and not native_deadline:
        decision,source = attempted["decision"],attempted["source"]
        inactive = (source=="assignment_inactive" and decision=="cancel"
                    and not report_approval_active(db,orders,wid,session,json.loads(row["request"])))
        require((source=="operator" and decision in DECISIONS and decision==row["decision"])
                or (source=="expiry" and decision=="cancel" and now>=row["expires_at"]
                    and "cancel" in json.loads(row["request"])["decisions"])
                or inactive,409,"approval_response_conflict")
    db.execute("""UPDATE native_approvals SET closed_at=?,attempted_response=?,native_proof=?
                  WHERE worker_id=? AND approval_id=?""",(now,stored,stored_proof,wid,aid))


def closure_receipt(row,session,now):
    receipt = record(row,session,now)
    return {"approval_id":receipt["approval_id"],"session":receipt["request"]["session"],
            "request_digest":receipt["request_digest"],"state":"native_closed",
            "receipt_digest":hashlib.sha256(encode(receipt).encode()).hexdigest()}


def recover_codex_closure(db,orders,wid,aid,row,session,now,body):
    fields = {"session_id","session","turn_id","item_id","request_digest","request_id","attempted_response"}
    require(set(body)==fields,422,"invalid_codex_resolution")
    orders.active_session(db,wid,body)
    require(row,404,"approval_not_found")
    rid = body["request_id"]
    require(type(rid) is int or (isinstance(rid,str) and 0<len(rid)<=200 and "\0" not in rid),
            422,"invalid_codex_resolution")
    request = json.loads(row["request"])
    require(all(request[key]==string(body,key) for key in ("session","turn_id","item_id"))
            and body["request_digest"]==row["request_digest"],409,"approval_request_conflict")
    proof = {key:body[key] for key in ("session_id","request_digest","attempted_response")}
    if proof["attempted_response"]=={"source":"native_deadline","decision":"cancel"}:
        # Closure of an existing request can reuse Router's original body, including private fields.
        proof["request"] = request
    close_native(db,orders,wid,aid,row,session,now,proof)
    current = db.execute("SELECT * FROM native_approvals WHERE worker_id=? AND approval_id=?",(wid,aid)).fetchone()
    return closure_receipt(current,session,now)


def route(orders,actor,method,parts,body):
    require(len(parts) in (2,3,4),404,"unknown_approval_route")
    wid = parts[1]
    operator, worker = "approver/"+wid, "worker/"+wid
    require(actor in (operator,worker),403,"forbidden")
    with orders.db.transaction() as db:
        now = orders.clock()
        current = db.execute("SELECT session_id FROM workers WHERE worker_id=?",(wid,)).fetchone()
        session = current["session_id"] if current else None
        if len(parts)==2:
            require(method=="GET" and actor==operator,403,"forbidden")
            rows = db.execute("SELECT * FROM native_approvals WHERE worker_id=? ORDER BY rowid DESC LIMIT 100",(wid,))
            return {"items":[approval_view(db,orders,row,session,now) for row in rows]}
        aid = parts[2]
        require(valid_guid(aid),422,"invalid_approval_id")
        row = db.execute("SELECT * FROM native_approvals WHERE worker_id=? AND approval_id=?",(wid,aid)).fetchone()
        if method=="POST" and len(parts)==3:
            require(actor==worker,403,"forbidden")
            require(set(body)=={"session_id","request"},422,"invalid_approval_request")
            orders.active_session(db,wid,body)
            request = body["request"]
            validate_request(request)
            raw = encode(request)
            if row:
                require(row["worker_session"]==session and row["request"]==raw,409,"approval_request_conflict")
                return approval_view(db,orders,row,session,now)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            db.execute("""INSERT INTO native_approvals(worker_id,approval_id,worker_session,request,request_digest,expires_at)
                          VALUES (?,?,?,?,?,?)""",(wid,aid,session,raw,digest,now+TTL_SECONDS))
            if report_approval_active(db,orders,wid,session,request):
                delivery.enqueue(db,operator,"approval.requested",{"approval_id":aid,"worker_id":wid})
        elif method=="GET" and len(parts)==3:
            require(actor==operator,403,"forbidden")
            require(row,404,"approval_not_found")
            return approval_view(db,orders,row,session,now)
        elif method=="PUT" and len(parts)==4 and parts[3]=="decision":
            require(actor==operator,403,"forbidden")
            require(row,404,"approval_not_found")
            require(set(body)=={"request_digest","decision"},422,"invalid_approval_decision")
            require(body["request_digest"]==row["request_digest"],409,"approval_view_changed")
            decision = body["decision"]
            require(decision in json.loads(row["request"])["decisions"],422,"unsupported_approval_decision")
            if row["decision"] is not None:
                require(row["decision"]==decision,409,"approval_decision_conflict")
                return approval_view(db,orders,row,session,now)
            state = approval_view(db,orders,row,session,now)["state"]
            require(state!="assignment_inactive",409,"stale_assignment")
            require(state=="pending",409,"approval_not_pending")
            db.execute("UPDATE native_approvals SET decision=? WHERE worker_id=? AND approval_id=?",(decision,wid,aid))
        elif method=="POST" and len(parts)==4 and parts[3]=="receipt":
            require(actor==worker,403,"forbidden")
            fields = {"session_id","session","request_digest"}
            require(fields<=set(body)<=fields|{"attempted_response"},422,"invalid_approval_receipt_lookup")
            orders.active_session(db,wid,body)
            native_session = string(body,"session")
            request_digest = string(body,"request_digest")
            require(row,404,"approval_not_found")
            require(row["worker_session"]==session and row["request_digest"]==request_digest
                    and json.loads(row["request"])["session"]==native_session,409,"approval_request_conflict")
            require(row["closed_at"] is not None,409,"native_approval_closure_pending")
            if "attempted_response" in body:
                require(encode(body["attempted_response"])==row["attempted_response"],409,"approval_closure_conflict")
            return closure_receipt(row,session,now)
        elif method=="PUT" and len(parts)==4 and parts[3]=="recover-codex-closure":
            require(actor==worker,403,"forbidden")
            return recover_codex_closure(db,orders,wid,aid,row,session,now,body)
        elif method=="PUT" and len(parts)==4 and parts[3]=="closed":
            require(actor==worker,403,"forbidden")
            close_native(db,orders,wid,aid,row,session,now,body)
        else:
            raise Fault(404,"unknown_approval_route")
        updated = db.execute("SELECT * FROM native_approvals WHERE worker_id=? AND approval_id=?",(wid,aid)).fetchone()
        return approval_view(db,orders,updated,session,now)
