"""Check the committed assignment chain before publishing an offline rollback."""
import json

from . import attachments
from .messages import valid_guid
from .orders import task


def order_state(order, step, state, revision, assignment_id=None):
    holder = {"kind":"worker","worker_id":step["worker_id"]} if state=="working" else {"kind":"router"}
    return dict(order,state=state,revision=revision,assignment_id=assignment_id,
                assignee_id=step["worker_id"],holder=holder)


def histories(db, entries, deltas):
    used = dict.fromkeys(("pool_entries","cleanups","materials"),0)
    dispatches = set()
    for tid, entry in entries.items():
        order = task(db,tid)
        steps = []
        revision = 2
        pools = {row["assignment_id"]:dict(row) for row in db.execute("SELECT * FROM pool_entries WHERE task_id=?",(tid,))}
        cleanups = {row["assignment_id"]:dict(row) for row in db.execute("SELECT * FROM cleanups WHERE task_id=?",(tid,))}
        materials = {}
        for row in db.execute("SELECT * FROM materials WHERE task_id=? ORDER BY rowid",(tid,)):
            material = dict(row,artifact_ids=attachments.linked(db,"material",row["material_id"]))
            materials.setdefault(row["assignment_id"],[]).append(material)
        for frame in db.execute("""SELECT * FROM deliveries WHERE kind='task.dispatch'
                                  AND json_extract(payload,'$.task_id')=? ORDER BY rowid""",(tid,)):
            payload = json.loads(frame["payload"])
            aid, worker = payload.get("assignment_id"), frame["consumer"].removeprefix("worker/")
            if (not frame["consumer"].startswith("worker/") or not worker or not valid_guid(aid)
                    or aid in dispatches or payload!={"task_id":tid,"assignment_id":aid,"revision":revision,
                                                     "title":order["title"],"instructions":order["instructions"]}):
                raise ValueError("trial dispatch does not match the reviewed order")
            dispatches.add(aid)
            step = {"assignment_id":aid,"worker_id":worker,"revision":revision,"delivery_id":frame["delivery_id"]}
            if pool := pools.pop(aid,None):
                if (pool["worker_id"]!=worker or pool["active"]!=0 or pool["remind_at"] is not None
                        or pool["source"] not in ("worker","offline") or not pool["reason"].strip()):
                    raise ValueError("trial has an unresolved or unsupported blocked assignment")
                pool["artifact_ids"] = attachments.linked(db,"report",aid)
                if pool["source"]=="offline" and pool["artifact_ids"]:
                    raise ValueError("offline pool notification has unexpected files")
                step["pool"] = pool
                used["pool_entries"] += 1
            if cleanup := cleanups.pop(aid,None):
                if (cleanup["worker_id"]!=worker or cleanup["kind"]!="task.transfer_out"
                        or cleanup["done"]!=1 or not cleanup["reason"].strip()):
                    raise ValueError("original assignment cleanup is pending or cannot be verified")
                step["cleanup"] = cleanup
                used["cleanups"] += 1
            if candidates := materials.pop(aid,None):
                if not pool or any(m["submitted_by"]!=worker or not m["received_at"]
                                   or not (m["text"].strip() or m["artifact_ids"]) for m in candidates):
                    raise ValueError("candidate material does not belong to its original blocked assignment")
                step["materials"] = candidates
                used["materials"] += len(candidates)
            steps.append(step)
            revision += 3 + ("pool" in step)
        if (pools or cleanups or materials or not steps or steps[0]["worker_id"]!=entry["original_worker"]
                or steps[-1]["assignment_id"]!=entry["assignment_id"] or steps[-1]["worker_id"]!=entry["worker_id"]
                or order["revision"]!=revision or set(steps[-1]) & {"pool","cleanup","materials"}):
            raise ValueError("trial is missing a complete assignment chain for the reviewed result")
        for step,target in zip(steps,steps[1:]):
            cleanup = step.get("cleanup")
            released_by_worker = step.get("pool",{}).get("source")=="worker"
            if (step["worker_id"]==target["worker_id"] or bool(cleanup)==released_by_worker
                    or cleanup and cleanup["new_assignee_id"]!=target["worker_id"]):
                raise ValueError("transfer does not match the former assignment and its cleanup")
        entry["assignment_history"] = steps
    if any(deltas.get(table,{}).get("trial_only",0)!=count for table,count in used.items()):
        raise ValueError("trial contains unrelated pool, cleanup, or material rows")


def check_transport(db, entries):
    workers = set()
    expected_frames = {}
    expected_notices = {}
    notices = {tid:[] for tid in entries}
    cleanup_frames = {}
    for tid, entry in entries.items():
        expected_notices[tid] = []
        order = task(db,tid)
        for step in entry["assignment_history"]:
            aid, worker = step["assignment_id"], step["worker_id"]
            workers.add(worker)
            expected_frames["task.dispatch",tid,aid] = ("worker/"+worker,step["delivery_id"],
                {"task_id":tid,"assignment_id":aid,"revision":step["revision"],
                 "title":order["title"],"instructions":order["instructions"]})
            if cleanup := step.get("cleanup"):
                key = ("task.transfer_out",tid,aid)
                cleanup_frames[key] = cleanup
                expected_frames[key] = ("worker/"+worker,None,
                    {name:cleanup[name] for name in ("task_id","assignment_id","reason","new_assignee_id")})
            if pool := step.get("pool"):
                source = worker if pool["source"]=="worker" else "router"
                expected_notices[tid].append((source,pool["reason"],pool["artifact_ids"]))
        expected_notices[tid].append((entry["worker_id"],entry["summary"],entry["artifact_ids"]))
    registered = db.execute("SELECT * FROM workers").fetchall()
    if ({row["worker_id"] for row in registered} != workers or any(row["busy"] or row["held_task_id"] is not None
            or row["held_assignment_id"] is not None or row["query_id"] for row in registered)):
        raise ValueError("trial workers or their outstanding queries need reconciliation")
    if any(row[0] not in workers for row in db.execute("SELECT worker_id FROM worker_adapter_links")):
        raise ValueError("trial contains an unrelated adapter link")

    messages = {}
    for row in db.execute("SELECT * FROM messages ORDER BY rowid"):
        body = json.loads(row["payload"])
        tid, mid = body.get("task_id"), row["message_id"]
        index = len(notices[tid]) if tid in notices else 0
        expected = expected_notices.get(tid,[])
        if index>=len(expected) or row["owner"]!="router" or row["level"]!="Notify" or not row["received_at"]:
            raise ValueError("trial has a notice outside the reviewed W04 results")
        source,text,files = expected[index]
        if row["source"]!=source or body!={"message_id":mid,"level":"Notify","task_id":tid,
                                          "source":source,"text":text,"attachment_ids":files}:
            raise ValueError("trial has a notice outside the reviewed W04 results")
        notice = {"payload":body,"received_at":row["received_at"]}
        notices[tid].append(notice)
        messages[mid] = notice

    frames = {}
    for row in db.execute("SELECT * FROM deliveries ORDER BY rowid"):
        data = json.loads(row["payload"])
        kind,consumer,did = row["kind"],row["consumer"],row["delivery_id"]
        tid, aid = data.get("task_id"),data.get("assignment_id")
        if not row["ack"]:
            raise ValueError("trial delivery has no consumer acknowledgement")
        if kind=="worker.status_query":
            if set(data)!={"worker_id","query_id"} or data["worker_id"] not in workers or consumer!="worker/"+data["worker_id"]:
                raise ValueError("unexpected worker status query")
        elif kind=="message.deliver":
            saved = messages.pop(data.get("message_id"),None)
            if not saved or consumer!="hermes" or data!=saved["payload"]:
                raise ValueError("trial notification does not match its archived message")
            saved["delivery_id"] = did
        else:
            key = (kind,tid,aid)
            expected = expected_frames.pop(key,None)
            if not expected or consumer!=expected[0] or expected[1] not in (None,did) or data!=expected[2]:
                raise ValueError("trial has a transport frame outside the reviewed orders")
            if key in cleanup_frames:
                cleanup_frames[key]["delivery_id"] = did
        frames[did] = (consumer,row["cursor"])
    if messages or expected_frames or any(len(notices[tid])!=len(items) for tid,items in expected_notices.items()):
        raise ValueError("trial is missing a dispatch, cleanup delivery, or W04 notification")
    for tid, items in notices.items():
        entries[tid]["notice"] = items[-1]
        if len(items)>1:
            entries[tid]["pool_notices"] = items[:-1]
    return workers,frames


def take(operations, key, actor, body, replies=None, *, session=False, files=False):
    record = operations.pop(key,None)
    if record and record["actor"]==actor:
        actual = dict(record["body"])
        if session:
            actual.pop("session_id",None)
        if files:
            actual.setdefault("artifact_ids",[])
        if actual==body and (replies is None or record["reply"] in replies):
            return record
    raise ValueError("missing or conflicting receipt for the reviewed assignment: "+key[1])


def verify_order(db, tid, entry, operations):
    order = task(db,tid)
    history = entry["assignment_history"]
    adjacent_states, cleanup_receipts = [],[]
    after = 0
    for step,target in zip(history,history[1:]+[None]):
        aid, worker, revision = (step[key] for key in ("assignment_id","worker_id","revision"))
        actor = "worker/"+worker
        accept = take(operations,(tid,"accept",aid),actor,{"assignment_id":aid,"revision":revision},
                      [{"accepted":True,"holder":{"kind":"worker","worker_id":worker},"assignment_id":aid}],session=True)
        if accept["seq"]<=after:
            raise ValueError("assignment was accepted before its transfer")
        step["accept_key"] = accept["key"]
        after = accept["seq"]
        # The offline timer does not write an operation. Between W03 and the
        # next action, an admitted assignment can have reached its saved pool.
        possible = {"working","blocked"} if step.get("pool",{}).get("source")=="offline" else {"working"}
        adjacent_states.append((after,possible))
        if target is None:
            report = take(operations,(tid,"report",aid,"completed"),actor,{
                "assignment_id":aid,"outcome":"completed","summary":entry["summary"],"artifact_ids":entry["artifact_ids"]},
                [{"recorded":True,"release_assignment_id":aid,"task_state":"completed_reported"}],session=True,files=True)
            close = take(operations,(tid,"action","close",order["revision"]-1),"hermes",
                         {"action":"close","expected_revision":order["revision"]-1},
                         [{"task":order,"delivery_state":"received"}])
            if not after<report["seq"]<close["seq"]:
                raise ValueError("acceptance, report, and manager review are out of order")
            adjacent_states.extend([(report["seq"],{"completed_reported"}),(close["seq"],{"closed"})])
            entry.update(w04_key=report["key"],review_key=close["key"])
            continue
        pool = step.get("pool")
        if pool and pool["source"]=="worker":
            blocked = take(operations,(tid,"report",aid,"blocked"),actor,{
                "assignment_id":aid,"outcome":"blocked","summary":pool["reason"],"artifact_ids":pool["artifact_ids"]},
                [{"recorded":True,"release_assignment_id":aid,"task_state":"blocked"}],session=True,files=True)
            if blocked["seq"]<=after:
                raise ValueError("blocked report predates acceptance")
            pool["report_key"] = blocked["key"]
            after = blocked["seq"]
            adjacent_states.append((after,{"blocked"}))
        for material in step.get("materials",[]):
            mid = material["material_id"]
            receipt = take(operations,(tid,"material",mid),actor,
                           {"assignment_id":aid,"text":material["text"],"artifact_ids":material["artifact_ids"]},
                           [{"material_id":mid,"received":True}],session=True,files=True)
            if receipt["seq"]<=after:
                raise ValueError("candidate material predates its blocked assignment")
            material["receipt_key"] = receipt["key"]
            after = receipt["seq"]
            adjacent_states.append((after,{"blocked"}))
        expected_revision = target["revision"]-2
        key = (tid,"action","transfer",expected_revision)
        transfer = operations.get(key)
        reason = transfer["body"].get("reason") if transfer else None
        if not isinstance(reason,str) or not reason.strip():
            raise ValueError("missing transfer reason")
        cleanup = step.get("cleanup")
        if cleanup and cleanup["reason"]!=reason:
            raise ValueError("cleanup reason differs from the transfer")
        replies = [{"task":order_state(order,target,"ready",target["revision"]-1),"delivery_state":"pending"},
                   {"task":order_state(order,target,"ready",target["revision"],target["assignment_id"]),
                    "delivery_state":"pending"}]
        transfer = take(operations,key,"hermes",{"action":"transfer","expected_revision":expected_revision,
                                                "worker_id":target["worker_id"],"reason":reason},replies)
        if transfer["seq"]<=after:
            raise ValueError("transfer predates the original assignment records")
        step.update(transfer_key=transfer["key"],transfer_reason=reason)
        after = transfer["seq"]
        adjacent_states.append((after,{"ready"}))
        if cleanup:
            receipt = take(operations,(tid,"report",aid,"transfer_confirmed"),actor,
                           {"assignment_id":aid,"outcome":"transfer_confirmed"},[
                               {"recorded":True,"release_assignment_id":aid,"task_state":state}
                               for state in ("ready","working","blocked","completed_reported","closed")],
                           session=True)
            if receipt["seq"]<=after:
                raise ValueError("cleanup receipt predates the transfer")
            cleanup_receipts.append((cleanup,receipt))
    for cleanup,receipt in cleanup_receipts:
        _, states = next((item for item in reversed(adjacent_states) if item[0]<receipt["seq"]))
        if receipt["reply"]["task_state"] not in states:
            raise ValueError("cleanup receipt changes the state of a different assignment")
        cleanup.update(report_key=receipt["key"],receipt=receipt["reply"])


def check_operations(db,entries,workers,frames,uploads,expected_count,queue_candidates):
    operations, applicable, acked, sessions = {},0,set(),{}
    creation_keys = {item["request_id"] for item in queue_candidates}
    creation_keys.update(entry["creation_key"] for entry in entries.values() if "creation_key" in entry)
    for row in db.execute("SELECT rowid AS seq,* FROM operations ORDER BY rowid"):
        (method,path),body = json.loads(row["request"])
        parts = path.split("/")
        actor,reply = row["actor"],json.loads(row["response"])
        if method=="POST" and path=="/v1/tasks":
            if actor=="hermes" and row["key"] in creation_keys:
                creation_keys.remove(row["key"])
                applicable += 1
            continue
        applicable += 1
        if method=="POST" and len(parts)==5 and parts[:3]==["","v1","tasks"] and parts[3] in entries:
            tid,op = parts[3:]
            if op=="accept":
                key = (tid,op,body.get("assignment_id"))
            elif op=="report":
                key = (tid,op,body.get("assignment_id"),body.get("outcome"))
            elif op=="actions":
                key = (tid,"action",body.get("action"),body.get("expected_revision"))
            elif op=="materials":
                body.setdefault("text","")
                key = (tid,"material",reply.get("material_id"))
            else:
                raise ValueError("trial has an operation outside the reviewed orders")
            if (key in operations or op!="actions" and
                    (actor not in sessions or body.get("session_id")!=sessions[actor])):
                raise ValueError("trial receipt is duplicated or uses an unregistered worker session")
            operations[key] = {"seq":row["seq"],"actor":actor,"key":row["key"],"body":body,"reply":reply}
        elif method=="POST" and len(parts)==5 and parts[:3]==["","v1","deliveries"] and parts[4]=="ack":
            did = parts[3]
            if frames.get(did)!=(actor,body.get("cursor")) or did in acked or reply.get("recorded") is not True:
                raise ValueError("trial has an unrecognized delivery acknowledgement")
            acked.add(did)
        elif method=="PUT" and parts[:3]==["","v1","workers"] and len(parts)>=5 and parts[3] in workers:
            if actor!="worker/"+parts[3] or parts[4:] not in (["session"],["presence"],["links","adapter"]):
                raise ValueError("trial has an unrecognized worker operation")
            if parts[4:]==["session"]:
                session = body.get("session_id")
                if not isinstance(session,str) or not session.strip():
                    raise ValueError("trial worker registration is missing its session")
                sessions[actor] = session
        elif method=="POST" and path=="/v1/uploads" and reply.get("upload_id") in uploads:
            upload = uploads[reply["upload_id"]]
            if actor!=upload["owner"] or body!={key:upload[key] for key in ("name","size_bytes","sha256")}:
                raise ValueError("trial upload registration does not match the reviewed attachment")
        else:
            raise ValueError("trial has an operation outside the reviewed orders")
    for tid,entry in entries.items():
        verify_order(db,tid,entry,operations)
    if creation_keys or applicable!=expected_count or acked!=frames.keys() or operations:
        raise ValueError("trial operation ledger is incomplete or has unrelated writes")
