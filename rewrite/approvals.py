"""Restricted operator decisions for individual native requests."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import delivery
from .messages import valid_guid
from .storage import Fault, encode, require, string


TTL_SECONDS = 600
DECISIONS = ("accept","cancel")
SCHEMA = """
CREATE TABLE IF NOT EXISTS native_approvals (
 worker_id TEXT NOT NULL, approval_id TEXT NOT NULL, worker_session TEXT NOT NULL,
 request TEXT NOT NULL, request_digest TEXT NOT NULL, expires_at REAL NOT NULL,
 decision TEXT, closed_at REAL, attempted_response TEXT,
 PRIMARY KEY(worker_id,approval_id)
);
"""


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
    return {"worker_id":row["worker_id"],"approval_id":row["approval_id"],"state":state,
            "request":json.loads(row["request"]),"request_digest":row["request_digest"],
            "decision":row["decision"],"expires_at":row["expires_at"],"closed_at":row["closed_at"],
            "attempted_response":attempted}


def close_native(db,orders,wid,aid,row,session,now,body):
    fields = {"session_id","request_digest","attempted_response"}
    require(fields<=set(body)<=fields|{"request"},422,"invalid_approval_closure")
    orders.active_session(db,wid,body)
    attempted = body["attempted_response"]
    if attempted is not None:
        require(isinstance(attempted,dict) and set(attempted)=={"source","decision"},422,"invalid_approval_closure")
    stored = encode(attempted)
    native_deadline = attempted=={"source":"native_deadline","decision":"cancel"}
    require(("request" in body)==native_deadline,422,"invalid_approval_closure")
    if native_deadline:
        request = body["request"]
        validate_request(request)
        raw = encode(request)
        require("cancel" in request["decisions"] and hashlib.sha256(raw.encode()).hexdigest()==body["request_digest"],
                409,"approval_request_conflict")
        if not row:
            # A spent native request can arrive before any acknowledged registration. Never notify an approver here.
            db.execute("""INSERT INTO native_approvals(worker_id,approval_id,worker_session,request,request_digest,
                          expires_at,closed_at,attempted_response) VALUES (?,?,?,?,?,?,?,?)""",
                       (wid,aid,session,raw,body["request_digest"],now,now,stored))
            return
        require(row["request"]==raw,409,"approval_request_conflict")
    require(row,404,"approval_not_found")
    require(session==row["worker_session"] and body["request_digest"]==row["request_digest"],
            409,"approval_request_conflict")
    if row["closed_at"] is not None:
        require(row["attempted_response"]==stored,409,"approval_closure_conflict")
        return
    if attempted is not None and not native_deadline:
        decision,source = attempted["decision"],attempted["source"]
        require((source=="operator" and decision in DECISIONS and decision==row["decision"])
                or (source=="expiry" and decision=="cancel" and now>=row["expires_at"]
                    and "cancel" in json.loads(row["request"])["decisions"]),409,"approval_response_conflict")
    db.execute("""UPDATE native_approvals SET closed_at=?,attempted_response=? WHERE worker_id=? AND approval_id=?""",
               (now,stored,wid,aid))


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
            return {"items":[record(row,session,now) for row in rows]}
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
                return record(row,session,now)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            db.execute("""INSERT INTO native_approvals(worker_id,approval_id,worker_session,request,request_digest,expires_at)
                          VALUES (?,?,?,?,?,?)""",(wid,aid,session,raw,digest,now+TTL_SECONDS))
            delivery.enqueue(db,operator,"approval.requested",{"approval_id":aid,"worker_id":wid})
        elif method=="GET" and len(parts)==3:
            require(actor==operator,403,"forbidden")
            require(row,404,"approval_not_found")
            return record(row,session,now)
        elif method=="PUT" and len(parts)==4 and parts[3]=="decision":
            require(actor==operator,403,"forbidden")
            require(row,404,"approval_not_found")
            require(set(body)=={"request_digest","decision"},422,"invalid_approval_decision")
            require(body["request_digest"]==row["request_digest"],409,"approval_view_changed")
            decision = body["decision"]
            require(decision in json.loads(row["request"])["decisions"],422,"unsupported_approval_decision")
            if row["decision"] is not None:
                require(row["decision"]==decision,409,"approval_decision_conflict")
                return record(row,session,now)
            require(record(row,session,now)["state"]=="pending",409,"approval_not_pending")
            db.execute("UPDATE native_approvals SET decision=? WHERE worker_id=? AND approval_id=?",(decision,wid,aid))
        elif method=="PUT" and len(parts)==4 and parts[3]=="closed":
            require(actor==worker,403,"forbidden")
            close_native(db,orders,wid,aid,row,session,now,body)
        else:
            raise Fault(404,"unknown_approval_route")
        updated = db.execute("SELECT * FROM native_approvals WHERE worker_id=? AND approval_id=?",(wid,aid)).fetchone()
        return record(updated,session,now)
