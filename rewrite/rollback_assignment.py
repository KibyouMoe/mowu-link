"""Verify the manager's first assignment of a trial order created without a worker."""
import hashlib
import json

from .messages import valid_guid
from .storage import encode


def nonblank(value):
    return isinstance(value,str) and bool(value.strip())


def unassigned_creation(tid, operation):
    request = json.loads(operation["request"])
    (_,path),body = request
    if not isinstance(body,dict):
        return None
    content = {key:body.get(key) for key in ("title","instructions")}
    initial = dict(content,task_id=tid,state="ready",revision=1,assignee_id=None,assignment_id=None,
                   holder={"kind":"router"})
    if (path!="/v1/tasks" or not all(map(nonblank,content.values()))
            or body not in (content,dict(content,assignee_id=None))
            or encode(request)!=operation["request"] or encode(initial)!=operation["response"]):
        return None
    return initial


def verify(db, row, creation, first_offer):
    tid = row["task_id"]
    current = unassigned_creation(tid,creation)
    if current is None:
        return None
    offered = json.loads(first_offer["payload"]) if first_offer else None
    if first_offer and (not isinstance(offered,dict) or type(offered.get("revision")) is not int
                        or not valid_guid(offered.get("assignment_id"))):
        return None
    final_revision = offered["revision"]-1 if offered else row["revision"]-(row["state"]=="cancelled")
    base = f"/v1/tasks/{tid}"
    events = []
    first_assignment = None
    for event in db.execute("""SELECT rowid AS seq,actor,key,request,response FROM main.operations
        WHERE rowid>?
        AND (json_extract(request,'$[0][1]')=? OR json_extract(request,'$[0][1]') GLOB ?)
        ORDER BY rowid""",(creation["seq"],base,base+"/*")):
        (method,path),body = json.loads(event["request"])
        if not isinstance(body,dict):
            return None
        revision = current["revision"]
        if (path.endswith("/actions") and body.get("action")=="assign"
                and type(body.get("expected_revision")) is int and body["expected_revision"]==revision):
            worker = body.get("worker_id")
            if (method!="POST" or event["actor"]!="hermes" or not nonblank(worker)
                    or current["assignee_id"] not in (None,worker)
                    or body!={"action":"assign","expected_revision":revision,"worker_id":worker}):
                return None
            revised = dict(current,assignee_id=worker,revision=revision+(current["assignee_id"] is None))
            replies = [{"task":revised,"delivery_state":"pending"}]
            immediate = offered and revised["revision"]==final_revision
            if immediate:
                replies.append({"task":dict(revised,revision=offered["revision"],
                                            assignment_id=offered["assignment_id"]),"delivery_state":"pending"})
            if event["response"] not in map(encode,replies):
                return None
            if first_assignment is None:
                first_assignment = event["key"]
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
    if not current["assignee_id"] or current["revision"]!=final_revision:
        return None
    content = {key:current[key] for key in ("title","instructions")}
    if offered:
        if (first_offer["consumer"]!="worker/"+current["assignee_id"]
                or offered!={"task_id":tid,"assignment_id":offered["assignment_id"],
                             "revision":final_revision+1,**content}):
            return None
    elif any(current[key]!=row[key] for key in ("title","instructions","assignee_id")):
        return None
    return {"original_creation":dict(creation),"original_request_sha256":hashlib.sha256(creation["request"].encode()).hexdigest(),
            "worker_id":current["assignee_id"],"content":content,"ready_revision":final_revision,
            "assignment_request_id":first_assignment,"events":events}
