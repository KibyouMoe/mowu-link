"""Reconcile native approval closure receipts in a stopped component snapshot."""
from contextlib import closing
import hashlib
import json

from . import approvals
from .cutover import readonly
from .storage import Fault


def has_table(db,name):
    return db.execute("SELECT 1 FROM sqlite_schema WHERE name=? AND type='table'",(name,)).fetchone() is not None


def dsh_closure_matches(row,intent):
    try:
        saved = json.loads(intent["closure"])
        request = json.loads(intent["request"])
        attempted = json.loads(intent["attempted_response"]) if intent["attempted_response"] is not None else None
        approvals.validate_request(request)
        approvals.validate_dsh_proof(saved["native"],intent["approval_id"],request["turn_id"],approvals.dsh_outcomes(attempted))
        router = approvals.record(row,row["worker_session"],0)
        return (set(saved)=={"native","router"} and isinstance(saved["router"],dict)
                and request["item_id"]==intent["approval_id"] and "command" in request
                and row["request"]==intent["request"] and router["attempted_response"]==attempted
                and hashlib.sha256(intent["request"].encode()).hexdigest()==router["request_digest"]
                and saved["router"]==router
                and (router["native_proof"]==saved["native"] if "native_proof" in router else intent["published"]==1))
    except (AttributeError,Fault,KeyError,TypeError,ValueError):
        return False


def reconcile(router_path,worker_id,adapter_paths):
    intents = {}
    dsh = {}
    issues = []
    for path in adapter_paths:
        with closing(readonly(path)) as db:
            for table,records,code in (
                ("codex_approval_intents",intents,"codex_approval_intent_duplicated"),
                ("dsh_approvals",dsh,"dsh_approval_intent_duplicated"),
            ):
                if not has_table(db,table):
                    continue
                for row in db.execute(f"SELECT * FROM {table}"):
                    approval_id = row["approval_id"]
                    if approval_id in intents or approval_id in dsh:
                        issues.append({"approval_id":approval_id,"code":code})
                    records[approval_id] = dict(row)
    with closing(readonly(router_path)) as db:
        remote = {row["approval_id"]:{"native_proof":None,**dict(row)} for row in db.execute(
            "SELECT * FROM native_approvals WHERE worker_id=?",(worker_id,))} if has_table(db,"native_approvals") else {}
    confirmed = 0
    for approval_id,intent in sorted(intents.items()):
        digest = intent["router_receipt_digest"]
        if digest is None:
            continue
        row = remote.get(approval_id)
        if row is None or row["closed_at"] is None:
            issues.append({"approval_id":approval_id,"code":"codex_approval_router_closure_missing"})
            continue
        try:
            request = json.loads(row["request"])
            expected = approvals.closure_receipt(row,row["worker_session"],0)["receipt_digest"]
            valid = (all(request[key]==intent[key] for key in ("session","turn_id","item_id"))
                     and row["request_digest"]==intent["request_digest"]==
                     hashlib.sha256(row["request"].encode()).hexdigest()
                     and digest==expected)
        except (AttributeError,KeyError,TypeError,ValueError):
            valid = False
        if valid:
            confirmed += 1
        else:
            issues.append({"approval_id":approval_id,"code":"codex_approval_closure_mismatch"})

    dsh_confirmed = dsh_native_proofs = 0
    for approval_id,intent in sorted(dsh.items()):
        if intent["closure"] is None:
            continue
        row = remote.get(approval_id)
        if row is None or row["closed_at"] is None:
            issues.append({"approval_id":approval_id,"code":"dsh_approval_router_closure_missing"})
        elif dsh_closure_matches(row,intent):
            dsh_confirmed += 1
            dsh_native_proofs += int(row["native_proof"] is not None)
        else:
            issues.append({"approval_id":approval_id,"code":"dsh_approval_closure_mismatch"})

    # These Router records identify the original engine. Historical command
    # receipts without native proof can also belong to another engine.
    missing_dsh = False
    for approval_id,row in sorted(remote.items()):
        if row["closed_at"] is None:
            continue
        if row["native_proof"] is not None and approval_id not in dsh:
            issues.append({"approval_id":approval_id,"code":"dsh_approval_intent_missing"})
            missing_dsh = True
        if approval_id in intents:
            continue
        try:
            request = json.loads(row["request"])
        except (ValueError,TypeError):
            request = None
        if isinstance(request,dict) and request.get("kind")==approvals.REPORT_APPROVAL:
            issues.append({"approval_id":approval_id,"code":"codex_report_approval_intent_missing"})
    result = {"confirmed":confirmed,"issues":issues}
    if dsh or missing_dsh:
        result["dsh"] = {"confirmed":dsh_confirmed,"router_native_proofs_matched":dsh_native_proofs,
                         "earlier_receipts_without_router_native_proof":dsh_confirmed-dsh_native_proofs}
    return result
