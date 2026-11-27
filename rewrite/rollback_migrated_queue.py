"""Stage approved replacements for old orders revised before their first offer."""
from contextlib import closing
import json
import sqlite3

from . import legacy_history, rollback_assignment
from .cutover import LEGACY_GOAL, first_dispatch, legacy_description_fits, queue_instructions
from .orders import task
from .storage import encode


def reviewed_proofs(db, rows, previous, selected):
    proofs = {}
    for row in rows:
        tid = row["task_id"]
        if tid not in selected:
            continue
        before,after = row["before"],row["trial"]
        if (not row["legacy_task_id"] or not before or not after
                or before["state"]!="ready" or before["assignment_id"] is not None or before["revision"]!=1
                or after["state"]!="ready" or after["assignment_id"] is not None
                or row["completed_reports"] or row["candidate_materials"] or first_dispatch(db,tid)):
            raise ValueError("migrated queue rollback requires an unchanged holding state and no offers")
        initial = dict(previous[tid],**before,task_id=tid,holder={"kind":"router"})
        proof = rollback_assignment.verify_prefix(db,task(db,tid),initial,0,allow_edit_only=True)
        if not proof:
            raise ValueError("migrated queue revision lacks its original management receipts")
        proofs[tid] = dict(proof,legacy_task_id=row["legacy_task_id"],initial=initial)
    if proofs.keys()!=selected.keys():
        raise ValueError("reviewed queue replacement does not match a revised migrated order")
    return proofs


def project_body(instructions, meta):
    header = LEGACY_GOAL+"\n\n" if meta else ""
    _,template = queue_instructions("",meta)
    suffix = template[len(header):]
    if (not instructions.startswith(header) or not instructions.endswith(suffix)
            or len(instructions)<=len(header)+len(suffix)):
        raise ValueError("edited instructions do not preserve the legacy execution mode and context")
    body = instructions[len(header):len(instructions)-len(suffix)]
    if not body or not legacy_description_fits({"instructions":body}):
        raise ValueError("edited instructions cannot be represented by the legacy queue")
    return body


def replace_order(db, source, tid, proof, at):
    old_id = proof["legacy_task_id"]
    old = db.execute("""SELECT i.*,q.seq,q.worker FROM task_info i JOIN dispatch_queue q USING(task_id)
                    WHERE task_id=?""",(old_id,)).fetchone()
    if (not old or old["worker"]!=proof["initial"]["assignee_id"] or old["owner"]!=old["worker"]
            or db.execute("SELECT 1 FROM task_facts WHERE task_id=?",(old_id,)).fetchone()
            or db.execute("SELECT 1 FROM active_tasks WHERE task_id=?",(old_id,)).fetchone()):
        raise ValueError("original legacy order is not in its reviewed queue position")
    worker = proof["worker_id"]
    if not db.execute("SELECT 1 FROM workers WHERE name=?",(worker,)).fetchone():
        raise ValueError("replacement worker is absent from the legacy snapshot")
    meta = json.loads(old["metadata"])
    initial = proof["initial"]
    if (old["name"]!=initial["title"]
            or queue_instructions(old["description"],meta)[1]!=initial["instructions"]):
        raise ValueError("original legacy order does not match the reviewed content")
    content = proof["content"]
    replacement,request_id = legacy_history.queue_successor_ids(source,tid)
    if db.execute("SELECT 1 FROM task_info WHERE task_id=? OR request_id=?",(replacement,request_id)).fetchone():
        raise ValueError("legacy queue replacement collides with an existing order")
    body = legacy_history.queue_successor_request(request_id,content["title"],
                                                 project_body(content["instructions"],meta),worker,meta)
    db.execute("""INSERT INTO task_info(task_id,name,description,owner,actor,at,metadata,request_id,fingerprint)
                  VALUES (?,?,?,?,?,?,?,?,?)""",
               (replacement,body["TaskName"],body["goal"],worker,"hermes",at,old["metadata"],request_id,
                legacy_history.legacy_request_fingerprint(body)))
    archive = {"reason":legacy_history.QUEUE_SUPERSEDED,"replacement_task_id":replacement,
               "recovery_reference":tid,"rollback_source_id":source}
    keys = [event["key"] for event in proof["events"]]
    db.executemany("INSERT INTO task_facts(task_id,kind,event_key,at,data) VALUES(?,?,?,?,?)",
                   [(old_id,"archive","archive",at,encode(archive)),
                    (old_id,"rewrite_queue_revisions","rewrite_queue_revisions",at,encode({"keys":keys}))])
    if db.execute("UPDATE dispatch_queue SET task_id=?,worker=? WHERE seq=? AND task_id=? AND worker=?",
                  (replacement,worker,old["seq"],old_id,old["worker"])).rowcount!=1:
        raise ValueError("original legacy queue position changed")
    return {"legacy_task_id":old_id,"replacement_task_id":replacement,"queue_position":old["seq"],
            "worker_id":worker,"legacy_request":body,"proof":proof}


def replace_orders(legacy, source, proofs, at):
    retired = {event["key"] for proof in proofs.values() for event in proof["events"]}
    if retired.intersection(legacy_history.queue_successor_ids(source,tid)[1] for tid in proofs):
        raise ValueError("replacement request overlaps a recorded trial operation")
    with closing(sqlite3.connect(legacy)) as old, old:
        old.row_factory = sqlite3.Row
        replacements = {tid:replace_order(old,source,tid,proof,at) for tid,proof in proofs.items()}
        if [row[0] for row in old.execute("PRAGMA integrity_check")]!=["ok"]:
            raise ValueError("legacy queue replacement failed its SQLite integrity check")
    return replacements
