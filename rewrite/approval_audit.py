"""Reconcile Codex approval closure receipts in a stopped component snapshot."""
from contextlib import closing
import hashlib
import json

from . import approvals
from .cutover import readonly


def has_table(db,name):
    return db.execute("SELECT 1 FROM sqlite_schema WHERE name=? AND type='table'",(name,)).fetchone() is not None


def reconcile(router_path,worker_id,adapter_paths):
    intents = {}
    issues = []
    for path in adapter_paths:
        with closing(readonly(path)) as db:
            if not has_table(db,"codex_approval_intents"):
                continue
            for row in db.execute("SELECT * FROM codex_approval_intents"):
                approval_id = row["approval_id"]
                if approval_id in intents:
                    issues.append({"approval_id":approval_id,"code":"codex_approval_intent_duplicated"})
                intents[approval_id] = dict(row)
    with closing(readonly(router_path)) as db:
        remote = {row["approval_id"]:row for row in db.execute(
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

    # Report approvals have always required a durable Codex intent. The older
    # command rows on Router can also belong to other hosts.
    for approval_id,row in sorted(remote.items()):
        if row["closed_at"] is None or approval_id in intents:
            continue
        try:
            request = json.loads(row["request"])
        except (ValueError,TypeError):
            request = None
        if isinstance(request,dict) and request.get("kind")==approvals.REPORT_APPROVAL:
            issues.append({"approval_id":approval_id,"code":"codex_report_approval_intent_missing"})
    return {"confirmed":confirmed,"issues":issues}
