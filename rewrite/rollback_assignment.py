"""Verify the manager's revisions and assignment decisions before the first offer."""
import hashlib
import json

from .messages import valid_guid
from .storage import encode


def nonblank(value):
    return isinstance(value,str) and bool(value.strip())


def initial_creation(tid, operation):
    request = json.loads(operation["request"])
    (method,path),body = request
    if not isinstance(body,dict):
        return None
    content = {key:body.get(key) for key in ("title","instructions")}
    owner = body.get("assignee_id")
    initial = dict(content,task_id=tid,state="ready",revision=1,assignee_id=owner,assignment_id=None,
                   holder={"kind":"router"})
    expected = dict(content,assignee_id=owner) if "assignee_id" in body else content
    if (method!="POST" or path!="/v1/tasks" or not all(map(nonblank,content.values()))
            or (owner is not None and not nonblank(owner)) or body!=expected
            or encode(request)!=operation["request"] or encode(initial)!=operation["response"]):
        return None
    return initial


def revise_assignment(current, body):
    action,worker = body.get("action"),body.get("worker_id")
    former = current["assignee_id"]
    revision = current["revision"]
    if not nonblank(worker) or type(body.get("expected_revision")) is not int:
        return None
    expected = {"action":action,"expected_revision":revision,"worker_id":worker}
    if action=="transfer":
        if worker==former or not nonblank(body.get("reason")):
            return None
        expected["reason"] = body["reason"]
    elif former not in (None,worker):
        return None
    if body!=expected:
        return None
    return dict(current,assignee_id=worker,revision=revision+(worker!=former))


def verify(db, row, creation, first_offer, *, allow_edit_only=False):
    initial = initial_creation(row["task_id"],creation)
    if initial is None:
        return None
    proof = verify_prefix(db,row,initial,creation["seq"],first_offer,allow_edit_only=allow_edit_only)
    if proof is not None:
        return dict(proof,original_creation=dict(creation),
                    original_request_sha256=hashlib.sha256(creation["request"].encode()).hexdigest())
    return None


def verify_prefix(db, row, current, after_seq, first_offer=None, *, allow_edit_only=False):
    tid = row["task_id"]
    offered = json.loads(first_offer["payload"]) if first_offer else None
    if first_offer and (not isinstance(offered,dict) or type(offered.get("revision")) is not int
                        or not valid_guid(offered.get("assignment_id"))):
        return None
    final_revision = offered["revision"]-1 if offered else row["revision"]-(row["state"]=="cancelled")
    base = f"/v1/tasks/{tid}"
    events = []
    assignment_request = None
    for event in db.execute("""SELECT rowid AS seq,actor,key,request,response FROM main.operations
        WHERE rowid>?
        AND (json_extract(request,'$[0][1]')=? OR json_extract(request,'$[0][1]') GLOB ?)
        ORDER BY rowid""",(after_seq,base,base+"/*")):
        (method,path),body = json.loads(event["request"])
        if not isinstance(body,dict):
            return None
        revision = current["revision"]
        if (path==base+"/actions" and body.get("action") in ("assign","transfer")
                and type(body.get("expected_revision")) is int and body["expected_revision"]==revision):
            revised = revise_assignment(current,body)
            if method!="POST" or event["actor"]!="hermes" or revised is None:
                return None
            replies = [{"task":revised,"delivery_state":"pending"}]
            immediate = offered and revised["revision"]==final_revision
            if immediate:
                replies.append({"task":dict(revised,revision=offered["revision"],
                                            assignment_id=offered["assignment_id"]),"delivery_state":"pending"})
            if event["response"] not in map(encode,replies):
                return None
            if assignment_request is None or revised["assignee_id"]!=current["assignee_id"]:
                assignment_request = event["key"]
        elif path==base and method=="PATCH" and revision<final_revision:
            changes = body.get("changes")
            if (event["actor"]!="hermes" or not isinstance(changes,dict) or not changes
                    or set(changes)-{"title","instructions"} or not all(map(nonblank,changes.values()))
                    or type(body.get("expected_revision")) is not int
                    or body!={"expected_revision":revision,"changes":changes}):
                return None
            revised = dict(current,**changes,revision=revision+1)
            if event["response"]!=encode(revised):
                return None
        else:
            break
        if revised["revision"]>final_revision:
            return None
        events.append(dict(event))
        current = revised
        if json.loads(event["response"]).get("task",{}).get("assignment_id"):
            break
    if (not current["assignee_id"] or current["revision"]!=final_revision or not events
            or (assignment_request is None and not allow_edit_only)):
        return None
    content = {key:current[key] for key in ("title","instructions")}
    if offered:
        if (first_offer["consumer"]!="worker/"+current["assignee_id"]
                or offered!={"task_id":tid,"assignment_id":offered["assignment_id"],
                             "revision":final_revision+1,**content}):
            return None
    elif any(current[key]!=row[key] for key in ("title","instructions","assignee_id")):
        return None
    return {"worker_id":current["assignee_id"],"content":content,"ready_revision":final_revision,
            "assignment_request_id":assignment_request,"events":events}
