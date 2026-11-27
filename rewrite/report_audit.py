"""Reconcile terminal Worker outbox reports with the sealed Router ledger."""
from contextlib import closing
import json

from . import attachments
from .cutover import readonly
from .messages import valid_guid
from .storage import Fault
from .trigger_audit import bytes_match, json_object


OUTCOMES = {"completed":("complete-","completed_reported"),"blocked":("blocked-","blocked")}


def router_result(router,outcome,assignment_id):
    if outcome=="completed":
        return router.execute("SELECT task_id,worker_id,summary FROM reports WHERE assignment_id=?",
                              (assignment_id,)).fetchone()
    return router.execute("""SELECT task_id,worker_id,reason FROM pool_entries
                             WHERE assignment_id=? AND source='worker'""",(assignment_id,)).fetchone()


def artifact_valid(router,actor,file_id):
    try:
        row = attachments.authorize(router,actor,file_id)
        attachments.authorize(router,"hermes",file_id)
    except Fault:
        return False
    pieces = router.execute("SELECT start,bytes FROM chunks WHERE upload_id=? ORDER BY start",(row["upload_id"],))
    return row["next_offset"]==row["size_bytes"] and bytes_match(pieces,row["size_bytes"],row["sha256"])


def verify_report(router,worker_id,row):
    aid,tid = row["assignment_id"],row["task_id"]
    body = json_object(row["body"])
    receipt = json_object(row["receipt"])
    if (not body or not valid_guid(aid) or not valid_guid(tid) or body.get("assignment_id")!=aid
            or not isinstance(body.get("outcome"),str) or body["outcome"] not in OUTCOMES
            or not isinstance(body.get("summary"),str)):
        return "unverified","report_outbox_mismatch",0
    outcome = body["outcome"]
    prefix,task_state = OUTCOMES[outcome]
    actor = "worker/"+worker_id
    operation = router.execute("SELECT request,response FROM operations WHERE actor=? AND key=?",
                               (actor,prefix+aid)).fetchone()
    result = router_result(router,outcome,aid)
    if not operation:
        if not result and row["receipt"] is None:
            return "pending",None,0
        if not result and receipt=={"error":"stale_assignment"}:
            return "stale",None,0
        return "unverified","report_router_receipt_missing",0
    try:
        request = json.loads(operation["request"])
    except (TypeError,ValueError):
        request = None
    if request!=[["POST",f"/v1/tasks/{tid}/report"],body]:
        return "unverified","report_router_request_mismatch",0
    response = json_object(operation["response"])
    if (not response or response.get("recorded") is not True or response.get("release_assignment_id")!=aid
            or response.get("task_state")!=task_state
            or (row["receipt"] is not None and (receipt!=response or row["stage"]!="released"))):
        return "unverified","report_receipt_mismatch",0
    if not result or tuple(result)!=(tid,worker_id,body["summary"]):
        return "unverified","report_router_result_mismatch",0
    ids = body.get("artifact_ids",[])
    if (not isinstance(ids,list) or not all(valid_guid(fid) for fid in ids)
            or attachments.linked(router,"report",aid)!=ids):
        return "unverified","report_artifact_links_mismatch",0
    if not all(artifact_valid(router,actor,fid) for fid in ids):
        return "unverified","report_artifact_unverified",0
    state = "awaiting_worker_commit" if row["receipt"] is None else "confirmed"
    return state,None,len(ids)


def reconcile(worker,router_path,worker_id):
    counts = dict.fromkeys(("pending","awaiting_worker_commit","confirmed","stale","unverified","verified_artifacts"),0)
    issues,local = [],set()
    with closing(readonly(router_path)) as router:
        for row in worker.execute("SELECT assignment_id,task_id,stage,body,receipt FROM outbox "
                                  "LEFT JOIN jobs USING(assignment_id) ORDER BY assignment_id"):
            aid = row["assignment_id"]
            local.add(aid)
            state,issue,files = verify_report(router,worker_id,row)
            counts[state] += 1
            counts["verified_artifacts"] += files
            if issue:
                issues.append({"task_id":row["task_id"],"assignment_id":aid,"code":issue})
        # These are current Worker actions. Imported history has no matching Worker write requirement.
        operations = router.execute("""SELECT key,json_extract(request,'$[0][1]') AS route FROM operations
            WHERE actor=? AND (key GLOB 'complete-*' OR key GLOB 'blocked-*')
            AND json_valid(request) AND json_extract(request,'$[0][0]')='POST'
            ORDER BY key""",("worker/"+worker_id,))
        for key,route in operations:
            aid = key.split("-",1)[1]
            if aid in local or not valid_guid(aid) or not isinstance(route,str):
                continue
            parts = route.split("/")
            if len(parts)==5 and parts[1:3]==["v1","tasks"] and valid_guid(parts[3]) and parts[4]=="report":
                issues.append({"task_id":parts[3],"assignment_id":aid,"code":"report_worker_outbox_missing"})
                counts["unverified"] += 1
    return {**counts,"issues":issues}
