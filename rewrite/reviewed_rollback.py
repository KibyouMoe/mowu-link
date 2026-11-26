"""Return reviewed migrated work to an offline legacy copy, keeping trial results private."""
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import time

from .cutover import (digest, output_directory, queue_execution_modes, queue_replay_plan,
                      readonly, reconcile, restore_snapshot, verified)
from .messages import valid_guid
from .orders import task
from .storage import encode


TABLES = {"tasks","reports","messages","deliveries","operations","workers","worker_adapter_links",
          "uploads","chunks","attachment_grants","business_files"}


def reviewed_orders(facts):
    queued = queue_execution_modes(facts["queue_candidates"])
    changes = []
    for row in facts["work_orders"]:
        if row["task_id"] in queued:
            if row["before"] or row["completed_reports"] or row["candidate_materials"]:
                raise ValueError("new queued order already has a report or candidate material")
        else:
            changes.append(row)
    deltas = facts["changed_tables"]
    if (not changes or facts["blocked_new_tasks"] or set(deltas)-TABLES
            or any(value["baseline_only"] for name,value in deltas.items() if name!="tasks")):
        raise ValueError("trial has changes outside the supported reviewed orders")
    if deltas.get("tasks")!={"baseline_only":len(changes),"trial_only":len(changes)+len(queued)}:
        raise ValueError("trial contains other task changes")
    entries = {}
    for row in changes:
        before,after,reports = row["before"],row["trial"],row["completed_reports"]
        if (not row["legacy_task_id"] or row["same_content"] is not True or not before or not after or before["state"]!="ready"
                or after["state"]!="closed" or before["assignment_id"] is not None or after["assignment_id"] is not None
                or before["revision"]!=1 or after["revision"]!=5
                or before["assignee_id"]!=after["assignee_id"] or row["candidate_materials"] or len(reports)!=1
                or reports[0]["worker_id"]!=after["assignee_id"]):
            raise ValueError("reviewed rollback needs unchanged migrated orders closed after one W04")
        entries[row["task_id"]] = {**reports[0],"legacy_task_id":row["legacy_task_id"]}
    return entries


def check_transport(db,entries):
    workers = {row["worker_id"] for row in entries.values()}
    registered = db.execute("SELECT * FROM workers").fetchall()
    if ({row["worker_id"] for row in registered} != workers or any(row["busy"] or row["held_task_id"] is not None
            or row["held_assignment_id"] is not None or row["query_id"] for row in registered)):
        raise ValueError("trial workers or their outstanding queries need reconciliation")
    links = db.execute("SELECT worker_id FROM worker_adapter_links").fetchall()
    if any(row["worker_id"] not in workers for row in links):
        raise ValueError("trial contains an unrelated adapter link")

    notices = {}
    for row in db.execute("SELECT * FROM messages"):
        data = json.loads(row["payload"])
        tid = data.get("task_id")
        entry = entries.get(tid)
        if (not entry or tid in notices or row["owner"]!="router" or row["level"]!="Notify"
                or row["source"]!=entry["worker_id"] or not row["received_at"] or data!={
                    "message_id":row["message_id"],"level":"Notify","task_id":tid,"source":entry["worker_id"],
                    "text":entry["summary"],"attachment_ids":entry["artifact_ids"]}):
            raise ValueError("trial has a notice outside the reviewed W04 results")
        notices[tid] = {"payload":data,"received_at":row["received_at"]}
    if notices.keys()!=entries.keys():
        raise ValueError("missing W04 notice")
    sent,dispatches,frames = set(),set(),{}
    for row in db.execute("SELECT * FROM deliveries"):
        data = json.loads(row["payload"])
        tid = data.get("task_id")
        entry = entries.get(tid)
        kind,consumer = row["kind"],row["consumer"]
        if not row["ack"]:
            raise ValueError("trial delivery has no consumer acknowledgement")
        if kind=="worker.status_query":
            if set(data)!={"worker_id","query_id"} or data["worker_id"] not in workers or consumer!="worker/"+data["worker_id"]:
                raise ValueError("unexpected worker status query")
        elif kind=="task.dispatch" and entry and tid not in dispatches:
            expected = task(db,tid)
            if consumer!="worker/"+entry["worker_id"] or data!={"task_id":tid,
                    "assignment_id":entry["assignment_id"],"revision":2,
                    "title":expected["title"],"instructions":expected["instructions"]}:
                raise ValueError("trial dispatch does not match the reviewed order")
            dispatches.add(tid)
        elif kind=="message.deliver" and entry and tid not in sent:
            if consumer!="hermes" or data!=notices[tid]["payload"]:
                raise ValueError("trial notification does not match its archived message")
            sent.add(tid)
            notices[tid]["delivery_id"] = row["delivery_id"]
        else:
            raise ValueError("trial has a transport frame outside the reviewed orders")
        frames[row["delivery_id"]] = (consumer,row["cursor"])
    if sent!=entries.keys() or dispatches!=entries.keys():
        raise ValueError("trial is missing a dispatch or W04 notification delivery")
    return workers,frames,notices


def check_operations(db,entries,workers,frames,uploads,expected_count,queue_candidates):
    mapped,applicable,acked = {},0,set()
    report_sessions = {}
    queued_keys = {item["request_id"] for item in queue_candidates}
    for row in db.execute("SELECT * FROM operations"):
        (method,path),body = json.loads(row["request"])
        parts = path.split("/")
        actor,reply = row["actor"],json.loads(row["response"])
        if method=="POST" and path=="/v1/tasks":
            # The delta count excludes unchanged baseline rows and includes reviewed queue additions.
            if actor=="hermes" and row["key"] in queued_keys:
                queued_keys.remove(row["key"])
                applicable += 1
            continue
        applicable += 1
        if method=="POST" and len(parts)==5 and parts[:3]==["","v1","tasks"] and parts[3] in entries:
            tid,op = parts[3:]
            entry = entries[tid]
            aid,worker = entry["assignment_id"],entry["worker_id"]
            if op=="accept":
                valid = (actor=="worker/"+worker and set(body)=={"session_id","assignment_id","revision"}
                         and bool(body["session_id"]) and body["assignment_id"]==aid and body["revision"]==2
                         and reply=={"accepted":True,"holder":{"kind":"worker","worker_id":worker},"assignment_id":aid})
            elif op=="report":
                expected = {"assignment_id":aid,"outcome":"completed","summary":entry["summary"],
                            "artifact_ids":entry["artifact_ids"]}
                valid = (actor=="worker/"+worker and {**body,"artifact_ids":body.get("artifact_ids",[])}==
                         {**expected,"session_id":body.get("session_id")} and bool(body.get("session_id"))
                         and reply=={"recorded":True,"release_assignment_id":aid,"task_state":"completed_reported"})
            elif op=="actions":
                valid = (actor=="hermes" and body=={"action":"close","expected_revision":4}
                         and reply=={"task":task(db,tid),"delivery_state":"received"})
            else:
                valid = False
            if not valid or (tid,op) in mapped:
                raise ValueError("missing or conflicting acceptance, W04, or manager-close receipt")
            mapped[tid,op] = row["key"]
            if op in ("accept","report"):
                report_sessions[tid,op] = body["session_id"]
        elif method=="POST" and len(parts)==5 and parts[:3]==["","v1","deliveries"] and parts[4]=="ack":
            did = parts[3]
            if frames.get(did)!=(actor,body.get("cursor")) or did in acked or reply.get("recorded") is not True:
                raise ValueError("trial has an unrecognized delivery acknowledgement")
            acked.add(did)
        elif method=="PUT" and parts[:3]==["","v1","workers"] and len(parts)>=5 and parts[3] in workers:
            if actor!="worker/"+parts[3] or parts[4:] not in (["session"],["presence"],["links","adapter"]):
                raise ValueError("trial has an unrecognized worker operation")
        elif method=="POST" and path=="/v1/uploads" and reply.get("upload_id") in uploads:
            upload = uploads[reply["upload_id"]]
            if actor!=upload["owner"] or body!={key:upload[key] for key in ("name","size_bytes","sha256")}:
                raise ValueError("trial upload registration does not match the reviewed attachment")
        else:
            raise ValueError("trial has an operation outside the reviewed orders")
    if queued_keys or applicable!=expected_count or acked!=frames.keys() or set(mapped)!={(tid,op)
            for tid in entries for op in ("accept","report","actions")}:
        raise ValueError("trial operation ledger is incomplete or has unrelated writes")
    if any(report_sessions[tid,"accept"]!=report_sessions[tid,"report"] for tid in entries):
        raise ValueError("assignment acceptance and report use different worker sessions")
    return mapped


def copy_report_files(db,entries,deltas,folder):
    files = {}
    uploads = {}
    chunks = 0
    for entry in entries.values():
        aid = entry["assignment_id"]
        for index,file_id in enumerate(entry["artifact_ids"]):
            if not valid_guid(file_id):
                raise ValueError("invalid report attachment identifier")
            binding = db.execute("""SELECT attachment_id FROM business_files WHERE kind='report'
                                    AND reference_id=? AND position=?""",(aid,index)).fetchone()
            if not binding or binding["attachment_id"]!=file_id:
                raise ValueError("report attachment order changed")
            row = db.execute("SELECT * FROM uploads WHERE attachment_id=?",(file_id,)).fetchone()
            if (not row or row["owner"]!="worker/"+entry["worker_id"] or row["size_bytes"]!=row["next_offset"]
                    or db.execute("SELECT 1 FROM attachment_grants WHERE attachment_id=? AND actor='hermes'",
                                  (file_id,)).fetchone() is None):
                raise ValueError("report attachment is incomplete or outside its former access scope")
            if file_id in files:
                continue
            target = folder/file_id
            current = 0
            checksum = hashlib.sha256()
            with open(target,"xb",opener=private_open) as output:
                for part in db.execute("SELECT start,bytes FROM chunks WHERE upload_id=? ORDER BY start",(row["upload_id"],)):
                    if part["start"]!=current or not part["bytes"]:
                        raise ValueError("report attachment contains a gap")
                    output.write(part["bytes"])
                    current += len(part["bytes"])
                    checksum.update(part["bytes"])
                    chunks += 1
            if current!=row["size_bytes"] or checksum.hexdigest()!=row["sha256"]:
                raise ValueError("report attachment digest mismatch")
            files[file_id] = {key:row[key] for key in ("attachment_id","name","size_bytes","sha256","owner")}
            uploads[row["upload_id"]] = row
    for table,count in (("uploads",len(uploads)),("chunks",chunks),("attachment_grants",len(files)),
                        ("business_files",sum(len(entry["artifact_ids"]) for entry in entries.values()))):
        if deltas.get(table,{}).get("trial_only",0)!=count:
            raise ValueError("trial has files or grants outside the reviewed reports")
    return uploads,files


def private_open(name,flags):
    return os.open(name,flags,0o600)


def remove_from_legacy_queue(path,entries,at):
    with closing(sqlite3.connect(path)) as db, db:
        for tid,entry in entries.items():
            legacy = entry["legacy_task_id"]
            removed = db.execute("DELETE FROM dispatch_queue WHERE task_id=? AND worker=?",(legacy,entry["worker_id"]))
            if removed.rowcount!=1:
                raise ValueError("legacy queue no longer contains the reviewed original")
            # Keep only the routing outcome and private reference in the shared legacy database.
            metadata = {"reason":"reviewed_on_rewrite","recovery_reference":tid}
            db.execute("INSERT INTO task_facts(task_id,kind,event_key,at,data) VALUES (?,'archive','archive',?,?)",
                       (legacy,at,encode(metadata)))
        if db.execute("PRAGMA integrity_check").fetchall()!=[("ok",)]:
            raise ValueError("staged legacy database integrity check failed")


def stage_reviewed_rollback(snapshot,trial,review,destination):
    """Prepare an offline fallback without re-enqueueing completed migrated work."""
    facts = reconcile(snapshot,trial)
    entries = reviewed_orders(facts)
    decision = {"trial_sha256":facts["trial_sha256"],
                "closed_assignments":{tid:row["assignment_id"] for tid,row in entries.items()}}
    if facts["queue_candidates"]:
        decision["execution_modes"] = queue_execution_modes(facts["queue_candidates"])
    if json.loads(review.read_text())!=decision:
        raise ValueError("review must match the trial digest, all closed assignments and any new queue modes")
    manifest = verified(snapshot)
    source = trial/"router.db"
    if manifest["legacy_db_sha256"]!=facts["snapshot_sha256"]:
        raise ValueError("legacy snapshot changed during review")
    with output_directory(destination) as stage:
        restore_snapshot(snapshot,stage,manifest)
        # RouterServer derives its serving directory from the restored database path.
        (stage/"attachments").rename(stage/"legacy.db.attachments")
        private = stage/"private-review"
        private.mkdir(mode=0o700)
        (private/"files").mkdir(mode=0o700)
        frozen = private/"trial-router.db"
        with closing(readonly(source)), source.open("rb") as previous, open(frozen,"xb",opener=private_open) as output:
            shutil.copyfileobj(previous,output)
        if digest(frozen)!=facts["trial_sha256"]:
            raise ValueError("trial database changed during review")
        with closing(readonly(frozen)) as db:
            for tid,entry in entries.items():
                result = db.execute("SELECT summary FROM reports WHERE task_id=? AND assignment_id=?",
                                    (tid,entry["assignment_id"])).fetchone()
                if not result or not result["summary"].strip():
                    raise ValueError("missing reviewed report")
                entry["summary"] = result["summary"]
            workers,frames,notices = check_transport(db,entries)
            uploads,files = copy_report_files(db,entries,facts["changed_tables"],private/"files")
            operations = check_operations(db,entries,workers,frames,uploads,
                                          facts["changed_tables"].get("operations",{}).get("trial_only",0),
                                          facts["queue_candidates"])
            for tid,entry in entries.items():
                entry.update(notice=notices[tid],w04_key=operations[tid,"report"],review_key=operations[tid,"actions"])
            plan = queue_replay_plan(db,stage/"legacy.db",facts) if facts["queue_candidates"] else None
        at = time.time()
        remove_from_legacy_queue(stage/"legacy.db",entries,at)
        restored_sha = digest(stage/"legacy.db")
        queue_manifest_sha = None
        if plan:
            path = stage/"queue-replay.json"
            with open(path,"x",encoding="utf-8",opener=private_open) as output:
                output.write(encode({**plan,"restored_legacy_sha256":restored_sha}))
            queue_manifest_sha = digest(path)
        with open(private/"manifest.json","x",encoding="utf-8",opener=private_open) as output:
            recovery = {**decision,"source_id":facts["source_id"],"legacy_sha256":facts["snapshot_sha256"],
                        "restored_sha256":restored_sha,"staged_at":at,"orders":entries,"files":files}
            if queue_manifest_sha:
                recovery["queue_replay_sha256"] = queue_manifest_sha
            output.write(encode(recovery))
        with closing(readonly(source)):
            if digest(source)!=digest(frozen):
                raise ValueError("trial database changed while staging rollback")
    return {"reviewed_orders":len(entries),"private_artifacts":len(files),"trial_sha256":facts["trial_sha256"],
            "restored_legacy_sha256":restored_sha,"queued_requests":len(facts["queue_candidates"]),
            "queue_replay_sha256":queue_manifest_sha}
