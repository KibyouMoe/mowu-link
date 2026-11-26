"""Check the committed assignment chain before publishing an offline rollback."""
from datetime import datetime, timedelta, timezone
import json
import math

from . import attachments, rollback_inquiries, rollback_questions
from .cleanup import CANCEL_CONFIRMED_NOTICE
from .messages import valid_guid
from .orders import task
from .pool import CANCELLATION_EXHAUSTED, INITIAL_REMINDER_SECONDS, PROGRESS_EXHAUSTED, REMINDER_NOTICE, RESUME_INSTRUCTION_PREFIX
from .storage import timestamp


def is_reviewed_completion(entry):
    return entry["terminal_state"]=="closed" and not entry.get("pool_archive")


def order_state(order, step, state, revision, assignment_id=None):
    holder = {"kind":"worker","worker_id":step["worker_id"]} if state=="working" else {"kind":"router"}
    return dict(order,**step["content"],state=state,revision=revision,assignment_id=assignment_id,
                assignee_id=step["worker_id"],holder=holder)


def edit_requests(db):
    edits = {}
    for request, in db.execute("""SELECT request FROM operations
        WHERE json_extract(request,'$[0][0]')='PATCH'
        AND json_extract(request,'$[0][1]') GLOB '/v1/tasks/*'"""):
        (_,path),body = json.loads(request)
        index = path.split("/")[3],body.get("expected_revision")
        if index in edits:
            raise ValueError("duplicate task edit revision")
        edits[index] = body.get("changes")
    return edits


def retain_edit(edits, order, worker, content, state, revision, assignment_id=None):
    changes = edits.pop((order["task_id"],revision))
    if (not isinstance(changes,dict) or not changes or set(changes)-{"title","instructions"}
            or any(not isinstance(value,str) or not value.strip() for value in changes.values())):
        raise ValueError("invalid changes in the reviewed task edit")
    content = dict(content,**changes)
    receipt = order_state(order,{"worker_id":worker,"content":content},state,revision+1,assignment_id)
    return content,{"expected_revision":revision,"changes":changes,"expected":[receipt]}


def retain_edits(edits, order, worker, content, state, revision, assignment_id=None):
    history = []
    while (order["task_id"],revision) in edits:
        content,edit = retain_edit(edits,order,worker,content,state,revision,assignment_id)
        history.append(edit)
        revision += 1
    return revision,content,history


def inquiry_frames(db, tid):
    by_assignment = {}
    for row in db.execute("""SELECT rowid AS seq,kind,payload,delivery_id FROM deliveries
        WHERE kind IN ('task.probe','task.shelved','task.cancel') AND json_extract(payload,'$.task_id')=?
        ORDER BY rowid""",(tid,)):
        aid = json.loads(row["payload"]).get("assignment_id")
        by_assignment.setdefault(aid,[]).append(row)
    return by_assignment


def retain_inquiries(step, frames, dispatch_seq, next_dispatch_seq):
    source = step.get("pool",{}).get("source")
    unanswered = source=="unanswered"
    kinds = [frame["kind"] for frame in frames]
    count = kinds.count("task.probe")
    expired = step.get("expired_cancel")
    cleanup = step.get("cleanup")
    cancel_cleanup = cleanup and cleanup["kind"]=="task.cancel"
    total_cancellations = kinds.count("task.cancel")
    expired_count = 0
    if expired:
        expired_count = 4 if unanswered else total_cancellations-int(bool(cancel_cleanup))
        if not 1<=expired_count<=4 or source not in ("unanswered","offline"):
            raise ValueError("invalid number of cancellation deliveries before pooling")
    cancellations = total_cancellations-expired_count
    expected = ["task.probe"]*count + ["task.cancel"]*expired_count + (["task.shelved"] if unanswered else [])
    expected += ["task.cancel"]*cancellations
    if (kinds!=expected or not 0<=cancellations<=(1 if step.get("pool") else 4)
            or bool(cancellations)!=bool(cancel_cleanup)
            or any(frame["seq"]<=dispatch_seq or next_dispatch_seq is not None and frame["seq"]>=next_dispatch_seq
                   for frame in frames)):
        raise ValueError("trial inquiry deliveries do not match their original assignment")
    if count:
        step["probe_delivery_ids"] = [frame["delivery_id"] for frame in frames[:count]]
    if unanswered:
        step["shelved_delivery_id"] = frames[count+expired_count]["delivery_id"]
    if expired:
        expired["delivery_ids"] = [frame["delivery_id"] for frame in frames[count:count+expired_count]]
    if cancel_cleanup:
        cleanup["delivery_ids"] = [frame["delivery_id"] for frame in frames[-cancellations:]]


def utc_time(value):
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo==timezone.utc and value==instant.isoformat():
            return instant
    except (TypeError, ValueError):
        pass
    raise ValueError("invalid timestamp in the reviewed pool history")


def clock_precision(instant):
    # The Router computes the deadline as a float, then serializes microseconds.
    return timedelta(microseconds=1+math.ceil(math.ulp(instant.timestamp())*1_000_000))


def pool_actions(db):
    actions = {"snooze":{},"resume":{},"cancel":{}}
    for request,reply in db.execute("""SELECT request,response FROM operations
        WHERE json_extract(request,'$[1].action') IN ('snooze','resume','cancel')
        AND json_extract(request,'$[0][0]')='POST'
        AND json_extract(request,'$[0][1]') GLOB '/v1/tasks/*/actions'"""):
        (_,path),body = json.loads(request)
        index = path.split("/")[3],body.get("expected_revision")
        group = actions[body["action"]]
        if index in group:
            raise ValueError("duplicate pool action revision")
        group[index] = body,json.loads(reply)
    return actions


def retain_pool_changes(pool, order, worker, content, revision, edits, snoozes):
    # The rolled back copy contains ISO microsecond receipts, rather than the
    # execution timestamp. Recover the schedule with the same duration.
    started = utc_time(timestamp(pool["entered"]))
    earliest = started-clock_precision(started)
    state = "blocked"
    tid = order["task_id"]
    while (tid,revision) in snoozes or (tid,revision) in edits:
        if (tid,revision) in edits:
            content,edit = retain_edit(edits,order,worker,content,state,revision)
            pool.setdefault("edits",[]).append(edit)
            revision += 1
            continue
        body,reply = snoozes.pop((tid,revision))
        hours,value = body.get("remind_after_hours"),reply.get("task",{}).get("remind_at")
        if type(hours) not in (int,float) or not 0<hours<1e100 or not math.isfinite(hours):
            raise ValueError("invalid duration in a reviewed pool snooze")
        deadline = utc_time(value)
        try:
            since = deadline-timedelta(seconds=hours*3600)
        except OverflowError as exc:
            raise ValueError("pool snooze deadline cannot be reconciled") from exc
        precision = clock_precision(deadline)
        if since+precision < earliest:
            raise ValueError("pool snooze is outside its assignment history")
        pool.setdefault("snoozes",[]).append({
            "expected_revision":revision,"remind_after_hours":hours,"remind_at":value,
            "scheduled_at":since.isoformat(),"content":content})
        state = "snoozed"
        revision += 1
        earliest = max(earliest,since-precision)
    return revision,content


def histories(db, entries, deltas):
    used = dict.fromkeys(("pool_entries","cleanups","materials"),0)
    dispatches = set()
    actions = pool_actions(db)
    snoozes,resumes = actions["snooze"],actions["resume"]
    edits = edit_requests(db)
    for tid, entry in entries.items():
        order = task(db,tid)
        content = entry["original_content"]
        cancelled = entry["terminal_state"]=="cancelled"
        steps = []
        revision,content,initial_edits = retain_edits(edits,order,entry["original_worker"],content,"ready",1)
        if initial_edits:
            entry["initial_edits"] = initial_edits
        revision += 1  # First offer, or cancellation of a task that was never offered.
        pools = {row["assignment_id"]:dict(row) for row in db.execute("SELECT * FROM pool_entries WHERE task_id=?",(tid,))}
        cleanups = {row["assignment_id"]:dict(row) for row in db.execute("SELECT * FROM cleanups WHERE task_id=?",(tid,))}
        materials = {}
        for row in db.execute("SELECT * FROM materials WHERE task_id=? ORDER BY rowid",(tid,)):
            material = dict(row,artifact_ids=attachments.linked(db,"material",row["material_id"]))
            materials.setdefault(row["assignment_id"],[]).append(material)
        inquiries = inquiry_frames(db,tid)
        offers = db.execute("""SELECT rowid AS seq,* FROM deliveries WHERE kind='task.dispatch'
                             AND json_extract(payload,'$.task_id')=? ORDER BY rowid""",(tid,)).fetchall()
        for frame,next_frame in zip(offers,offers[1:]+[None]):
            payload = json.loads(frame["payload"])
            aid, worker = payload.get("assignment_id"), frame["consumer"].removeprefix("worker/")
            if (not frame["consumer"].startswith("worker/") or not worker or not valid_guid(aid)
                    or aid in dispatches or payload!={"task_id":tid,"assignment_id":aid,"revision":revision,**content}):
                raise ValueError("trial dispatch does not match the reviewed order")
            dispatches.add(aid)
            step = {"assignment_id":aid,"worker_id":worker,"revision":revision,"delivery_id":frame["delivery_id"],
                    "dispatch_seq":frame["seq"],"content":content}
            steps.append(step)
            if (tid,revision) in edits:
                # A05 replaces an unaccepted offer. The old offer may still have
                # a transport acknowledgement, but is never a W03 obligation.
                if (next_frame and next_frame["consumer"]!=frame["consumer"]) or (not next_frame and not cancelled):
                    raise ValueError("edited offer has no verifiable replacement or cancellation")
                revised,edit = retain_edit(edits,order,worker,content,"ready",revision)
                revision,content,waiting = retain_edits(edits,order,worker,revised,"ready",revision+1)
                revision += 1  # Replacement offer, or cancellation.
                if next_frame and not waiting:
                    replacement = json.loads(next_frame["payload"]).get("assignment_id")
                    edit["expected"].append(order_state(order,{"worker_id":worker,"content":revised},
                                                        "ready",revision,replacement))
                step["fenced_edits"] = [edit,*waiting]
                continue
            if next_frame is None and cancelled and aid not in pools and aid not in cleanups:
                step["unaccepted"] = True
                revision += 1
                continue

            revision,content,working = retain_edits(edits,order,worker,content,"working",revision+1,aid)
            if working:
                step["working_edits"] = working
            if pool := pools.pop(aid,None):
                if (pool["worker_id"]!=worker or pool["active"]!=0 or pool["remind_at"] is not None
                        or pool["source"] not in ("worker","offline","unanswered") or not pool["reason"].strip()):
                    raise ValueError("trial has an unresolved or unsupported blocked assignment")
                pool["artifact_ids"] = attachments.linked(db,"report",aid)
                if pool["source"]!="worker" and pool["artifact_ids"]:
                    raise ValueError("Router pool notification has unexpected files")
                exhausted = pool["source"]=="unanswered" and pool["reason"]==CANCELLATION_EXHAUSTED
                if pool["source"]=="unanswered" and not exhausted and pool["reason"]!=PROGRESS_EXHAUSTED:
                    raise ValueError("trial has an unsupported exhausted inquiry")
                original = actions["cancel"].get((tid,revision))
                if exhausted or pool["source"]=="offline" and original:
                    reason = original[0].get("reason") if original else None
                    if not isinstance(reason,str) or not reason.strip():
                        raise ValueError("exhausted cancellation is missing its original action")
                    expired = {"expected_revision":revision,"reason":reason,"content":content}
                    revision,content,working = retain_edits(edits,order,worker,content,"working",revision+1,aid)
                    if working:
                        expired["working_edits"] = working
                    step["expired_cancel"] = expired
                revision,content = retain_pool_changes(pool,order,worker,content,revision+1,edits,snoozes)
                step["pool"] = pool
                used["pool_entries"] += 1
            if cleanup := cleanups.pop(aid,None):
                expected_kind = "task.cancel" if cancelled and next_frame is None else "task.transfer_out"
                if (cleanup["worker_id"]!=worker or cleanup["kind"]!=expected_kind
                        or expected_kind=="task.cancel" and cleanup["new_assignee_id"] is not None
                        or cleanup["done"]!=1 or not cleanup["reason"].strip()):
                    raise ValueError("original assignment cleanup is pending or cannot be verified")
                step["cleanup"] = cleanup
                used["cleanups"] += 1
            elif step.get("expired_cancel"):
                raise ValueError("exhausted cancellation needs the original assignment's final cleanup")
            if candidates := materials.pop(aid,None):
                if not pool or any(m["submitted_by"]!=worker or not m["received_at"]
                                   or not (m["text"].strip() or m["artifact_ids"]) for m in candidates):
                    raise ValueError("candidate material does not belong to its original blocked assignment")
                step["materials"] = candidates
                used["materials"] += len(candidates)
            retain_inquiries(step,inquiries.pop(aid,[]),frame["seq"],next_frame["seq"] if next_frame else None)
            if next_frame:
                wait_state = "ready"
                if resume := resumes.pop((tid,revision),None):
                    resolution = resume[0].get("resolution")
                    if (step.get("pool",{}).get("source")!="worker" or "cleanup" in step
                            or not isinstance(resolution,str) or not resolution.strip()):
                        raise ValueError("resume needs evidence that the original worker released its assignment")
                    step["resume"] = {"resolution":resolution}
                    content = dict(content,instructions=content["instructions"]+RESUME_INSTRUCTION_PREFIX+resolution)
                    wait_state = "resuming"
                step["transition"] = {"expected_revision":revision,"content":content,"wait_state":wait_state}
                next_worker = next_frame["consumer"].removeprefix("worker/")
                revision,content,waiting = retain_edits(edits,order,next_worker,content,wait_state,revision+1)
                if waiting:
                    step["transition"]["waiting_edits"] = waiting
                revision += 1  # Next offer after the transition.
            elif is_reviewed_completion(entry):
                if set(step) & {"pool","cleanup","materials"}:
                    raise ValueError("trial is missing a complete assignment chain for the reviewed result")
                revision,content,completed = retain_edits(edits,order,worker,content,"completed_reported",revision+1)
                if completed:
                    entry["completed_edits"] = completed
                revision += 1  # Manager close.
            else:
                revision += 1 + ("cleanup" in step)  # Terminal action, plus cancellation W04 if needed.
        if pools or cleanups or materials or inquiries:
            raise ValueError("trial is missing a complete assignment chain for the reviewed result")
        if any(order[field]!=value for field,value in content.items()):
            raise ValueError("trial contents differ from the verified history")
        if not steps:
            if (not cancelled or entry["assignment_id"] is not None or order["revision"]!=revision
                    or entry["worker_id"]!=entry["original_worker"]):
                raise ValueError("trial order has no verifiable dispatch")
            entry["assignment_history"] = []
            continue
        final = steps[-1]
        if (steps[0]["worker_id"]!=entry["original_worker"] or final["assignment_id"]!=entry["assignment_id"]
                or final["worker_id"]!=entry["worker_id"]):
            raise ValueError("trial is missing a complete assignment chain for the reviewed result")
        if cancelled:
            pool,cleanup = final.get("pool"),final.get("cleanup")
            if pool and bool(cleanup)==(pool["source"]=="worker"):
                raise ValueError("original assignment cleanup is pending or cannot be verified")
        elif entry.get("pool_archive"):
            if final.get("pool",{}).get("source")!="worker" or "cleanup" in final:
                raise ValueError("pool archive needs evidence that the final worker released its assignment")
        if order["revision"]!=revision:
            raise ValueError("terminal action does not match the assignment revision")
        for step,target in zip(steps,steps[1:]):
            if "fenced_edits" in step:
                continue
            if "resume" in step:
                if step["worker_id"]!=target["worker_id"]:
                    raise ValueError("resume changed the original assignee")
                continue
            cleanup = step.get("cleanup")
            needs_cleanup = step.get("pool",{}).get("source")!="worker"
            if (step["worker_id"]==target["worker_id"] or bool(cleanup)!=needs_cleanup
                    or cleanup and cleanup["new_assignee_id"]!=target["worker_id"]):
                raise ValueError("transfer does not match the former assignment and its cleanup")
        entry["assignment_history"] = steps
    if snoozes or resumes or edits:
        raise ValueError("trial contains a snooze, resume, or edit outside the reviewed assignment history")
    if any(deltas.get(table,{}).get("trial_only",0)!=count for table,count in used.items()):
        raise ValueError("trial contains unrelated pool, cleanup, or material rows")


def retain_reminder(pool, message_id, observed):
    initial = pool.setdefault("initial_reminder",{"remind_at":timestamp(pool["entered"]+INITIAL_REMINDER_SECONDS)})
    schedule = [initial,*pool.get("snoozes",[])]
    for slot,replacement in zip(schedule,schedule[1:]+[None]):
        if ("reminder_message_id" not in slot and observed>=utc_time(slot["remind_at"])
                and (replacement is None or observed<=utc_time(replacement["scheduled_at"])
                     +clock_precision(utc_time(replacement["remind_at"])))):
            slot["reminder_message_id"] = message_id
            return
    raise ValueError("pool reminder does not match an outstanding deadline")


def check_transport(db, entries, reviewed_questions):
    workers, optional_workers = set(), set()
    expected_frames = {}
    expected_inquiries = {}
    expected_notices = {}
    notices = {tid:[] for tid in entries}
    cleanup_frames = {}
    expired_assignments,expired_retry_ids = set(),set()
    update_frames = {}
    terminal_notices = set()
    pool_bounds = {}
    pool_messages = {}
    terminal_messages = {}
    active_pools = {}
    consumed = dict.fromkeys(entries,0)
    for tid, entry in entries.items():
        expected_notices[tid] = []
        if not entry["assignment_history"]:
            optional_workers.add(entry["original_worker"])
        history = entry["assignment_history"]
        for step,target in zip(history,history[1:]+[None]):
            aid, worker = step["assignment_id"], step["worker_id"]
            workers.add(worker)
            expected_frames["task.dispatch",tid,aid] = ("worker/"+worker,step["delivery_id"],
                {"task_id":tid,"assignment_id":aid,"revision":step["revision"],**step["content"]})
            expired = step.get("expired_cancel",{})
            for edit in (*step.get("working_edits",[]),*expired.get("working_edits",[])):
                revision = edit["expected_revision"]+1
                update_frames[tid,aid,revision] = (edit,"worker/"+worker,step["dispatch_seq"],
                                                  target["dispatch_seq"] if target else None)
            for did in step.get("probe_delivery_ids",[]):
                expected_inquiries[did] = ("task.probe","worker/"+worker,{"task_id":tid,"assignment_id":aid})
            if shelved := step.get("shelved_delivery_id"):
                expected_inquiries[shelved] = ("task.shelved","worker/"+worker,{"task_id":tid,
                    "assignment_id":aid,"reason":step["pool"]["reason"]})
            for did in expired.get("delivery_ids",[]):
                expected_inquiries[did] = ("task.cancel","worker/"+worker,
                    {"task_id":tid,"assignment_id":aid,"reason":expired["reason"]})
            expired_retry_ids.update(expired.get("delivery_ids",[])[1:])
            if expired and step.get("shelved_delivery_id"):
                expired_assignments.add(aid)
            if cleanup := step.get("cleanup"):
                payload = {name:cleanup[name] for name in ("task_id","assignment_id","reason")}
                if cleanup["kind"]=="task.cancel":
                    for did in cleanup["delivery_ids"]:
                        expected_inquiries[did] = ("task.cancel","worker/"+worker,payload)
                else:
                    key = ("task.transfer_out",tid,aid)
                    cleanup_frames[key] = cleanup
                    expected_frames[key] = ("worker/"+worker,None,
                                            dict(payload,new_assignee_id=cleanup["new_assignee_id"]))
            if pool := step.get("pool"):
                source = worker if pool["source"]=="worker" else "router"
                expected_notices[tid].append((source,pool["reason"],pool["artifact_ids"],pool))
                pool_bounds[aid] = (step["dispatch_seq"],target["dispatch_seq"] if target else None)
        if is_reviewed_completion(entry):
            expected_notices[tid].append((entry["worker_id"],entry["summary"],entry["artifact_ids"],None))
            terminal_notices.add(tid)
        elif entry["terminal_state"]=="cancelled" and entry["assignment_history"] and entry["assignment_history"][-1].get("cleanup"):
            expected_notices[tid].append((entry["worker_id"],CANCEL_CONFIRMED_NOTICE,[],None))
            terminal_notices.add(tid)
    registered = db.execute("SELECT * FROM workers").fetchall()
    registered_workers = {row["worker_id"] for row in registered}
    if (not workers <= registered_workers <= (workers | optional_workers)
            or any(row["busy"] or row["held_task_id"] is not None
            or row["held_assignment_id"] is not None or row["query_id"] for row in registered)):
        raise ValueError("trial workers or their outstanding queries need reconciliation")
    workers = registered_workers
    if any(row[0] not in workers for row in db.execute("SELECT worker_id FROM worker_adapter_links")):
        raise ValueError("trial contains an unrelated adapter link")

    messages = {}
    for row in db.execute("SELECT * FROM messages ORDER BY rowid"):
        body = json.loads(row["payload"])
        tid, mid = body.get("task_id"), row["message_id"]
        if tid not in entries or row["owner"]!="router" or row["level"]!="Notify" or not row["received_at"]:
            raise ValueError("trial has a notice outside the reviewed W04 results")
        if "question_id" in body:
            messages[mid] = rollback_questions.record_notice(reviewed_questions,row,body)
            continue
        reminder = row["source"]=="router" and body.get("text")==REMINDER_NOTICE
        if reminder:
            source,text,files = "router",REMINDER_NOTICE,[]
            pool = active_pools.get(tid)
            if not pool:
                raise ValueError("pool reminder has no reviewed pool entry")
            retain_reminder(pool,mid,utc_time(row["received_at"]))
        else:
            index = consumed[tid]
            if index>=len(expected_notices[tid]):
                raise ValueError("trial has a notice outside the reviewed W04 results")
            source,text,files,pool = expected_notices[tid][index]
            consumed[tid] += 1
            active_pools[tid] = pool
            if pool and row["received_at"]!=timestamp(pool["entered"]):
                raise ValueError("pool entry notification time differs from the assignment")
        if row["source"]!=source or body!={"message_id":mid,"level":"Notify","task_id":tid,
                                          "source":source,"text":text,"attachment_ids":files}:
            raise ValueError("trial has a notice outside the reviewed W04 results")
        notice = {"payload":body,"received_at":row["received_at"]}
        notices[tid].append(notice)
        messages[mid] = notice
        if pool:
            pool_messages[mid] = pool["assignment_id"],reminder
        elif tid in terminal_notices:
            terminal_messages[mid] = entries[tid]["assignment_id"]

    frames = {}
    pool_start_frames = {}
    latest_update = {}
    work_stopped = set()
    shelved_assignments = set()
    latest_query,inquiry_sources = {},{}
    for row in db.execute("SELECT rowid AS seq,* FROM deliveries ORDER BY rowid"):
        data = json.loads(row["payload"])
        kind,consumer,did = row["kind"],row["consumer"],row["delivery_id"]
        tid, aid = data.get("task_id"),data.get("assignment_id")
        if not row["ack"]:
            raise ValueError("trial delivery has no consumer acknowledgement")
        if kind=="worker.status_query":
            if set(data)!={"worker_id","query_id"} or data["worker_id"] not in workers or consumer!="worker/"+data["worker_id"]:
                raise ValueError("unexpected worker status query")
            latest_query[consumer] = data["query_id"]
        elif kind=="message.deliver":
            mid = data.get("message_id")
            saved = messages.pop(mid,None)
            if not saved or consumer!="hermes" or data!=saved["payload"]:
                raise ValueError("trial notification does not match its archived message")
            saved["delivery_id"] = did
            if "question_id" in data:
                rollback_questions.prove_notice_delivery(reviewed_questions,data,row["seq"],pool_start_frames,work_stopped)
            if mid in terminal_messages:
                work_stopped.add(terminal_messages[mid])
            if mid in pool_messages:
                origin,is_reminder = pool_messages[mid]
                start,end = pool_bounds[origin]
                if is_reminder:
                    start = pool_start_frames.get(origin)
                if start is None or not start < row["seq"] or end is not None and row["seq"]>=end:
                    raise ValueError("pool notification delivery is outside its assignment")
                if not is_reminder:
                    pool_start_frames[origin] = row["seq"]
        elif kind in ("task.probe","task.shelved","task.cancel"):
            if expected_inquiries.pop(did,None)!=(kind,consumer,data):
                raise ValueError("trial inquiry delivery does not match the reviewed assignment")
            if kind=="task.cancel":
                work_stopped.add(aid)
            elif kind=="task.shelved":
                shelved_assignments.add(aid)
            if kind!="task.cancel" or did in expired_retry_ids:
                inquiry_sources[did] = rollback_inquiries.source(latest_query,consumer,tid,aid)
        elif kind=="question.resolved":
            rollback_questions.record_answer_delivery(reviewed_questions,data,consumer,did,row["seq"])
        elif kind=="task.updated":
            revision = data.get("revision")
            match = update_frames.pop((tid,aid,revision),None)
            if not match:
                raise ValueError("unexpected task update delivery")
            edit,owner,start,end = match
            if (consumer!=owner or not start<row["seq"] or end is not None and row["seq"]>=end
                    or aid in pool_start_frames or revision<=latest_update.get(aid,0)
                    or data!={"task_id":tid,"assignment_id":aid,"revision":revision,"changes":edit["changes"]}):
                raise ValueError("task update delivery does not match its active assignment")
            edit["delivery_id"] = did
            latest_update[aid] = revision
        else:
            key = (kind,tid,aid)
            expected = expected_frames.pop(key,None)
            if not expected or consumer!=expected[0] or expected[1] not in (None,did) or data!=expected[2]:
                raise ValueError("trial has a transport frame outside the reviewed orders")
            if key in cleanup_frames:
                if aid in expired_assignments and aid not in shelved_assignments:
                    raise ValueError("transfer cleanup predates the exhausted cancellation")
                cleanup_frames[key]["delivery_id"] = did
                work_stopped.add(aid)
        frames[did] = (consumer,row["cursor"])
    if (messages or expected_frames or expected_inquiries or update_frames
            or any(consumed[tid]!=len(items) for tid,items in expected_notices.items())):
        raise ValueError("trial is missing a dispatch, cleanup delivery, or W04 notification")
    for tid, items in notices.items():
        if tid in terminal_notices:
            entries[tid]["notice"] = items[-1]
            items = items[:-1]
        if items:
            entries[tid]["pool_notices"] = items
    return workers,frames,inquiry_sources


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


def verify_edits(tid, edits, operations, after):
    for edit in edits:
        revision = edit["expected_revision"]
        receipt = take(operations,(tid,"edit",revision),"hermes",
                       {"expected_revision":revision,"changes":edit["changes"]},edit.pop("expected"))
        if receipt["seq"]<=after:
            raise ValueError("task edit is out of order")
        edit.update(action_key=receipt["key"],receipt=receipt["reply"])
        after = receipt["seq"]
    return after


def optional_close_note(actual, request):
    if "note" in actual:
        note = actual["note"]
        if not isinstance(note,str):
            raise ValueError("close note is invalid")
        request["note"] = note


def verify_cancelled(tid, entry, order, step, operations, after, states):
    cleanup = step.get("cleanup")
    expected_revision = order["revision"]-(2 if cleanup else 1)
    key = (tid,"action","cancel",expected_revision)
    action = operations.get(key)
    reason = action["body"].get("reason") if action else None
    if not isinstance(reason,str) or not reason.strip() or cleanup and cleanup["reason"]!=reason:
        raise ValueError("missing or conflicting cancellation reason")
    replies = [{"task":order,"delivery_state":"received"}]
    if cleanup:
        pool = step.get("pool")
        snoozes = pool.get("snoozes",[]) if pool else []
        state = "snoozed" if snoozes else "blocked" if pool else "working"
        final = dict(step,content={field:order[field] for field in ("title","instructions")})
        pending = order_state(order,final,state,order["revision"]-1,
                              None if pool else step["assignment_id"])
        if pool:
            initial = {"remind_at":timestamp(pool["entered"]+INITIAL_REMINDER_SECONDS),**pool.get("initial_reminder",{})}
            schedule = snoozes[-1] if snoozes else initial
            pending.update(blocked_since=timestamp(pool["entered"]),remind_at=schedule["remind_at"])
        replies = [{"task":pending,"delivery_state":"pending"}]
        if pool and "reminder_message_id" in schedule:
            replies.append({"task":dict(pending,remind_at=None),"delivery_state":"pending"})
    action = take(operations,key,"hermes",{"action":"cancel","expected_revision":expected_revision,"reason":reason},replies)
    if action["seq"]<=after:
        raise ValueError("cancellation predates the original assignment records")
    entry.update(cancel_key=action["key"],cancel_reason=reason)
    terminal_seq = action["seq"]
    if cleanup:
        aid = step["assignment_id"]
        receipt = take(operations,(tid,"report",aid,"cancel_confirmed"),"worker/"+step["worker_id"],
                       {"assignment_id":aid,"outcome":"cancel_confirmed"},
                       [{"recorded":True,"release_assignment_id":aid,"task_state":"cancelled"}],session=True)
        if receipt["seq"]<=terminal_seq:
            raise ValueError("cleanup receipt predates the cancellation")
        states.append((terminal_seq,{pending["state"]}))
        terminal_seq = receipt["seq"]
        cleanup.update(report_key=receipt["key"],receipt=receipt["reply"])
        if step.get("expired_cancel"):
            cleanup.update(action_seq=action["seq"],report_seq=receipt["seq"])
        entry["cancel_w04_key"] = receipt["key"]
    states.append((terminal_seq,{"cancelled"}))
    return action["seq"]


def verify_pool_archive(tid, entry, order, operations, after, states):
    state = order["state"]
    action = "archive_failed" if state=="failed_archived" else "close"
    revision = order["revision"]-1
    key = (tid,"action",action,revision)
    operation = operations.get(key)
    actual = operation["body"] if operation else {}
    request = {"action":action,"expected_revision":revision}
    if action=="archive_failed":
        reason = actual.get("reason")
        if not isinstance(reason,str) or not reason.strip():
            raise ValueError("pool failure archive is missing its reason")
        request["reason"] = reason
    else:
        optional_close_note(actual,request)
        if "material_id" in actual:
            mid = actual["material_id"]
            if mid is not None and not any(material["material_id"]==mid for step in entry["assignment_history"]
                                           for material in step.get("materials",[])):
                raise ValueError("selected material is missing from the reviewed order")
            request["material_id"] = mid
    archive = take(operations,key,"hermes",request,[{"task":order,"delivery_state":"received"}])
    if archive["seq"]<=after:
        raise ValueError("pool archive predates the release or selected material")
    entry["pool_archive"] = {"action_key":archive["key"],"request":request}
    states.append((archive["seq"],{state}))


def verify_pool_activity(tid, step, order, operations, after, states):
    pool = step["pool"]
    activity = []
    position = after
    first_snooze = None
    revisions = [(item["expected_revision"],"edit",item) for item in pool.get("edits",[])]
    revisions.extend((item["expected_revision"],"snooze",item) for item in pool.get("snoozes",[]))
    for _,kind,snooze in sorted(revisions):
        if kind=="edit":
            position = verify_edits(tid,[snooze],operations,position)
            activity.append((position,{snooze["receipt"]["state"]}))
            continue
        revision = snooze["expected_revision"]
        expected = order_state(order,dict(step,content=snooze["content"]),"snoozed",revision+1)
        expected.update(blocked_since=timestamp(pool["entered"]),remind_at=snooze["remind_at"])
        action = take(operations,(tid,"action","snooze",revision),"hermes",
                      {"action":"snooze","expected_revision":revision,"remind_after_hours":snooze["remind_after_hours"]},
                      [{"task":expected,"delivery_state":"received"}])
        if action["seq"]<=position:
            raise ValueError("snooze predates pool entry or a previous action")
        snooze["action_key"] = action["key"]
        position = action["seq"]
        if first_snooze is None:
            first_snooze = position
        activity.append((position,{"snoozed"}))
    for material in step.get("materials",[]):
        mid = material["material_id"]
        receipt = take(operations,(tid,"material",mid),"worker/"+step["worker_id"],
                       {"assignment_id":step["assignment_id"],"text":material["text"],"artifact_ids":material["artifact_ids"]},
                       [{"material_id":mid,"received":True}],session=True,files=True)
        if receipt["seq"]<=after:
            raise ValueError("candidate material predates its blocked assignment")
        material["receipt_key"] = receipt["key"]
        after = receipt["seq"]
        activity.append((after,{"snoozed" if first_snooze is not None and after>first_snooze else "blocked"}))
    activity.sort()
    if activity and (expired := step.get("expired_cancel")):
        expired["pool_action_seq"] = activity[0][0]
    states.extend(activity)
    return max(after,position)


def transition_replies(order, step, target):
    transition = step["transition"]
    waiting = dict(target,content=transition["content"])
    early = order_state(order,waiting,transition["wait_state"],transition["expected_revision"]+1)
    replies = [{"task":early,"delivery_state":"pending"}]
    if not transition.get("waiting_edits"):
        offered = order_state(order,target,"ready",target["revision"],target["assignment_id"])
        replies.append({"task":offered,"delivery_state":"pending"})
    return replies


def verify_expired_cancel(tid, step, order, operations, after, states):
    expired = step["expired_cancel"]
    revision = expired["expected_revision"]
    pending = order_state(order,dict(step,content=expired["content"]),"working",revision+1,step["assignment_id"])
    action = take(operations,(tid,"action","cancel",revision),"hermes",
                  {"action":"cancel","expected_revision":revision,"reason":expired["reason"]},
                  [{"task":pending,"delivery_state":"pending"}])
    if action["seq"]<=after:
        raise ValueError("exhausted cancellation predates its original assignment")
    expired.update(action_key=action["key"],receipt=action["reply"],action_seq=action["seq"])
    after = verify_edits(tid,expired.get("working_edits",[]),operations,action["seq"])
    expired["working_end_seq"] = after
    states.append((action["seq"],{"working","blocked"}))
    return after


def verify_order(db, tid, entry, operations, windows):
    order = task(db,tid)
    history = entry["assignment_history"]
    adjacent_states, cleanup_receipts = [],[]
    after = verify_edits(tid,entry.get("initial_edits",[]),operations,0)
    cancelled = entry["terminal_state"]=="cancelled"
    if not history:
        verify_cancelled(tid,entry,order,{},operations,after,adjacent_states)
    for step,target in zip(history,history[1:]+[None]):
        aid, worker, revision = (step[key] for key in ("assignment_id","worker_id","revision"))
        actor = "worker/"+worker
        if "fenced_edits" in step or step.get("unaccepted"):
            after = verify_edits(tid,step.get("fenced_edits",[]),operations,after)
            if target is None:
                verify_cancelled(tid,entry,order,step,operations,after,adjacent_states)
            continue
        accept = take(operations,(tid,"accept",aid),actor,{"assignment_id":aid,"revision":revision},
                      [{"accepted":True,"holder":{"kind":"worker","worker_id":worker},"assignment_id":aid}],session=True)
        if accept["seq"]<=after:
            raise ValueError("assignment was accepted before its transfer")
        step["accept_key"] = accept["key"]
        after = accept["seq"]
        # The offline timer does not write an operation. Between W03 and the
        # next action, an admitted assignment can have reached its saved pool.
        source = step.get("pool",{}).get("source")
        possible = {"working","blocked"} if source in ("offline","unanswered") else {"working"}
        adjacent_states.append((after,possible))
        after = verify_edits(tid,step.get("working_edits",[]),operations,after)
        if expired := step.get("expired_cancel"):
            after = verify_expired_cancel(tid,step,order,operations,after,adjacent_states)
            windows[aid] = (accept["seq"],expired["action_seq"])
        if target is None and is_reviewed_completion(entry):
            report = take(operations,(tid,"report",aid,"completed"),actor,{
                "assignment_id":aid,"outcome":"completed","summary":entry["summary"],"artifact_ids":entry["artifact_ids"]},
                [{"recorded":True,"release_assignment_id":aid,"task_state":"completed_reported"}],session=True,files=True)
            key = (tid,"action","close",order["revision"]-1)
            request = {"action":"close","expected_revision":order["revision"]-1}
            optional_close_note(operations.get(key,{}).get("body",{}),request)
            close = take(operations,key,"hermes",request,[{"task":order,"delivery_state":"received"}])
            if report["seq"]<=after:
                raise ValueError("completion predates acceptance or edits")
            windows[aid] = (accept["seq"],report["seq"])
            after = verify_edits(tid,entry.get("completed_edits",[]),operations,report["seq"])
            if close["seq"]<=after:
                raise ValueError("acceptance, report, and manager review are out of order")
            adjacent_states.extend([(report["seq"],{"completed_reported"}),(close["seq"],{"closed"})])
            entry.update(w04_key=report["key"],review_key=close["key"])
            if "note" in request:
                entry["review_note"] = request["note"]
            continue
        pool = step.get("pool")
        if pool and pool["source"]=="worker":
            blocked = take(operations,(tid,"report",aid,"blocked"),actor,{
                "assignment_id":aid,"outcome":"blocked","summary":pool["reason"],"artifact_ids":pool["artifact_ids"]},
                [{"recorded":True,"release_assignment_id":aid,"task_state":"blocked"}],session=True,files=True)
            if blocked["seq"]<=after:
                raise ValueError("blocked report predates acceptance")
            pool["report_key"] = blocked["key"]
            windows[aid] = (accept["seq"],blocked["seq"])
            after = blocked["seq"]
            adjacent_states.append((after,{"blocked"}))
        if pool:
            after = verify_pool_activity(tid,step,order,operations,after,adjacent_states)
        if target is None:
            if cancelled:
                end = verify_cancelled(tid,entry,order,step,operations,after,adjacent_states)
                windows.setdefault(aid,(accept["seq"],end))
                if expired:
                    expired.setdefault("pool_action_seq",end)
            else:
                verify_pool_archive(tid,entry,order,operations,after,adjacent_states)
            continue
        transition = step["transition"]
        expected_revision = transition["expected_revision"]
        replies = transition_replies(order,step,target)
        if resume := step.get("resume"):
            action = take(operations,(tid,"action","resume",expected_revision),"hermes",
                          {"action":"resume","expected_revision":expected_revision,"resolution":resume["resolution"]},replies)
            if action["seq"]<=after:
                raise ValueError("resume predates the original assignment release")
            resume.update(action_key=action["key"],receipt=action["reply"])
            state = action["reply"]["task"]["state"]
            adjacent_states.append((action["seq"],{state,"ready"}))
            after = verify_edits(tid,transition.get("waiting_edits",[]),operations,action["seq"])
            continue
        key = (tid,"action","transfer",expected_revision)
        transfer = operations.get(key)
        reason = transfer["body"].get("reason") if transfer else None
        if not isinstance(reason,str) or not reason.strip():
            raise ValueError("missing transfer reason")
        cleanup = step.get("cleanup")
        if cleanup and cleanup["reason"]!=reason:
            raise ValueError("cleanup reason differs from the transfer")
        transfer = take(operations,key,"hermes",{"action":"transfer","expected_revision":expected_revision,
                                                "worker_id":target["worker_id"],"reason":reason},replies)
        if transfer["seq"]<=after:
            raise ValueError("transfer predates the original assignment records")
        windows.setdefault(aid,(accept["seq"],transfer["seq"]))
        if expired:
            expired.setdefault("pool_action_seq",transfer["seq"])
        step.update(transfer_key=transfer["key"],transfer_reason=reason)
        adjacent_states.append((transfer["seq"],{"ready"}))
        after = verify_edits(tid,transition.get("waiting_edits",[]),operations,transfer["seq"])
        if cleanup:
            receipt = take(operations,(tid,"report",aid,"transfer_confirmed"),actor,
                           {"assignment_id":aid,"outcome":"transfer_confirmed"},[
                               {"recorded":True,"release_assignment_id":aid,"task_state":state}
                               for state in ("ready","resuming","working","blocked","snoozed","completed_reported",
                                             "closed","cancelled","failed_archived")],
                           session=True)
            if receipt["seq"]<=transfer["seq"]:
                raise ValueError("cleanup receipt predates the transfer")
            if expired:
                cleanup.update(action_seq=transfer["seq"],report_seq=receipt["seq"])
            cleanup_receipts.append((cleanup,receipt))
    for cleanup,receipt in cleanup_receipts:
        _, states = next((item for item in reversed(adjacent_states) if item[0]<receipt["seq"]))
        if receipt["reply"]["task_state"] not in states:
            raise ValueError("cleanup receipt changes the state of a different assignment")
        cleanup.update(report_key=receipt["key"],receipt=receipt["reply"])


def check_operations(db,entries,workers,frames,uploads,expected_count,queue_candidates,reviewed_questions,inquiry_sources):
    operations, applicable, acked, sessions = {},0,{},{}
    asks,answers = {},{}
    samples,continued = {},{}
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
        if method=="POST" and len(parts)==5 and parts[:3]==["","v1","tasks"] and parts[4]=="questions":
            if actor not in sessions or body.get("session_id")!=sessions[actor]:
                raise ValueError("question receipt uses an unregistered worker session")
            asks.setdefault(body.get("question_id"),[]).append(
                {"seq":row["seq"],"actor":actor,"key":row["key"],"body":body,"reply":reply,"task_id":parts[3]})
        elif method=="PUT" and len(parts)==5 and parts[:3]==["","v1","questions"] and parts[4]=="answer":
            answers.setdefault(parts[3],[]).append({"seq":row["seq"],"actor":actor,"key":row["key"],"body":body,"reply":reply})
        elif method=="PATCH" and len(parts)==4 and parts[:3]==["","v1","tasks"] and parts[3] in entries:
            key = (parts[3],"edit",body.get("expected_revision"))
            if key in operations:
                raise ValueError("duplicate task edit receipt")
            operations[key] = {"seq":row["seq"],"actor":actor,"key":row["key"],"body":body,"reply":reply}
        elif method=="POST" and len(parts)==5 and parts[:3]==["","v1","tasks"] and parts[3] in entries:
            tid,op = parts[3:]
            if op=="report" and body.get("outcome")=="continue_requested":
                if actor not in sessions or body.get("session_id")!=sessions[actor]:
                    raise ValueError("continue receipt uses an unregistered worker session")
                continued.setdefault(body.get("assignment_id"),[]).append(
                    {"seq":row["seq"],"actor":actor,"key":row["key"],"body":body,"reply":reply,"task_id":tid})
                continue
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
            acked[did] = row["seq"]
        elif method=="PUT" and parts[:3]==["","v1","workers"] and len(parts)>=5 and parts[3] in workers:
            if actor!="worker/"+parts[3] or parts[4:] not in (["session"],["presence"],["links","adapter"]):
                raise ValueError("trial has an unrecognized worker operation")
            if parts[4:]==["session"]:
                session = body.get("session_id")
                if not isinstance(session,str) or not session.strip():
                    raise ValueError("trial worker registration is missing its session")
                sessions[actor] = session
            elif parts[4:]==["presence"]:
                query = body.get("query_id")
                if not valid_guid(query) or query in samples or actor not in sessions or body.get("session_id")!=sessions[actor]:
                    raise ValueError("worker presence receipt does not match its observation")
                samples[query] = {"seq":row["seq"],"actor":actor,"key":row["key"],"body":body,"reply":reply}
        elif method=="POST" and path=="/v1/uploads" and reply.get("upload_id") in uploads:
            upload = uploads[reply["upload_id"]]
            if actor!=upload["owner"] or body!={key:upload[key] for key in ("name","size_bytes","sha256")}:
                raise ValueError("trial upload registration does not match the reviewed attachment")
        else:
            raise ValueError("trial has an operation outside the reviewed orders")
    windows = {}
    for tid,entry in entries.items():
        verify_order(db,tid,entry,operations,windows)
    rollback_questions.verify_receipts(reviewed_questions,asks,answers,windows,acked)
    rollback_inquiries.verify_rounds(entries,inquiry_sources,samples,continued,windows,acked)
    if creation_keys or applicable!=expected_count or acked.keys()!=frames.keys() or operations:
        raise ValueError("trial operation ledger is incomplete or has unrelated writes")
