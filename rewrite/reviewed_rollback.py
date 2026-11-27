"""Return terminal work to an offline legacy copy, keeping trial records private."""
from contextlib import closing
import json
import os
import shutil
import sqlite3
import time

from . import legacy_history, rollback_idle, rollback_lifecycle, rollback_migrated_queue, rollback_peers, rollback_questions
from .rollback_sources import SourceReturns
from .cutover import (digest, legacy_creation_request, output_directory, queue_execution_modes,
                      readonly, reconcile, restore_snapshot, stage_queue_requests, verified)
from .messages import valid_guid
from .rollback_lifecycle import is_reviewed_completion
from .storage import encode


TABLES = {"tasks","reports","messages","deliveries","operations","workers","worker_adapter_links",
          "uploads","chunks","attachment_grants","business_files","pool_entries","cleanups","materials","questions"
          } | rollback_peers.TABLES


def reviewed_orders(facts, original_tasks, replacements, *, with_sources=False):
    queued = queue_execution_modes(facts["queue_candidates"])
    new = {item["task_id"]:item for field in ("closed_new_tasks","cancelled_new_tasks","archived_pool_new_tasks")
           for item in facts[field]}
    changes = []
    for row in facts["work_orders"]:
        if row["task_id"] in replacements:
            continue
        if row["task_id"] in queued:
            if row["before"] or row["completed_reports"] or row["candidate_materials"]:
                raise ValueError("new queued order already has a report or candidate material")
        else:
            changes.append(row)
    deltas = facts["changed_tables"]
    if (not (changes or replacements or facts["trial_peers"] or "workers" in deltas or with_sources)
            or facts["blocked_new_tasks"] or set(deltas)-TABLES
            or any(value["baseline_only"] for name,value in deltas.items() if name!="tasks")):
        raise ValueError("trial has changes outside the supported reviewed orders")
    if deltas.get("tasks",{"baseline_only":0,"trial_only":0})!={
            "baseline_only":len(changes)-len(new)+len(replacements),
            "trial_only":len(changes)+len(queued)+len(replacements)}:
        raise ValueError("trial contains other task changes")
    entries = {}
    for row in changes:
        tid = row["task_id"]
        before,after,reports = row["before"],row["trial"],row["completed_reports"]
        if tid not in new and (not row["legacy_task_id"] or not before
                or before["state"]!="ready" or before["assignment_id"] is not None or before["revision"]!=1):
            raise ValueError("reviewed rollback needs a migrated or trial-created order")
        if not after or after["assignment_id"] is not None:
            raise ValueError("trial order still has an assignment")
        state = after["state"]
        if state=="closed" and len(reports)==1 and reports[0]["worker_id"]==after["assignee_id"]:
            entry = dict(reports[0])
        elif state=="cancelled" and not reports:
            entry = {"assignment_id":row["cancelled_assignment_id"],"worker_id":after["assignee_id"]}
        elif not reports and row.get("pool_archive_assignment_id"):
            entry = {"assignment_id":row["pool_archive_assignment_id"],"worker_id":after["assignee_id"],
                     "pool_archive":True}
        else:
            raise ValueError("reviewed rollback needs unchanged migrated orders closed after one W04")
        entry["terminal_state"] = state
        entry["original_content"] = original_tasks[tid]
        if tid in new:
            entry.update(creation_key=new[tid]["request_id"],original_worker=new[tid]["assignee_id"])
        else:
            entry.update(legacy_task_id=row["legacy_task_id"],original_worker=before["assignee_id"])
        entries[tid] = entry
    return entries


def reviewed_files(entries):
    for entry in entries.values():
        if is_reviewed_completion(entry):
            yield "report",entry["assignment_id"],entry["worker_id"],entry["artifact_ids"]
        for step in entry["assignment_history"]:
            if pool := step.get("pool"):
                yield "report",step["assignment_id"],step["worker_id"],pool["artifact_ids"]
            for material in step.get("materials",[]):
                yield "material",material["material_id"],step["worker_id"],material["artifact_ids"]


def copy_reviewed_files(db,entries,deltas,folder,audited_uploads,audited_grants):
    files = {}
    uploads = dict(audited_uploads)
    grants = set(audited_grants)
    links = 0
    for kind,reference,worker,ids in reviewed_files(entries):
        links += len(ids)
        for index,file_id in enumerate(ids):
            if not valid_guid(file_id):
                raise ValueError("invalid reviewed attachment identifier")
            binding = db.execute("""SELECT attachment_id FROM business_files WHERE kind=?
                                    AND reference_id=? AND position=?""",(kind,reference,index)).fetchone()
            if not binding or binding["attachment_id"]!=file_id:
                raise ValueError("reviewed attachment order changed")
            row = db.execute("SELECT * FROM uploads WHERE attachment_id=?",(file_id,)).fetchone()
            if (not row or row["owner"]!="worker/"+worker or row["size_bytes"]!=row["next_offset"]
                    or db.execute("SELECT 1 FROM attachment_grants WHERE attachment_id=? AND actor='hermes'",
                                  (file_id,)).fetchone() is None):
                raise ValueError("reviewed attachment is incomplete or outside its former access scope")
            if file_id in files:
                continue
            with open(folder/file_id,"xb",opener=private_open) as output:
                rollback_peers.copy_chunks(db,row,lambda _offset,data:output.write(data))
            files[file_id] = {key:row[key] for key in ("attachment_id","name","size_bytes","sha256","owner")}
            uploads[row["upload_id"]] = row
            grants.add((file_id,"hermes"))
    if grants!={tuple(row) for row in db.execute("SELECT * FROM attachment_grants WHERE attachment_id IN "
                                                 "(SELECT attachment_id FROM uploads WHERE owner NOT LIKE 'history/%')")}:
        raise ValueError("trial has grants outside the reviewed assignments, peers or sources")
    chunks = sum(db.execute("SELECT COUNT(*) FROM chunks WHERE upload_id=?",(uid,)).fetchone()[0] for uid in uploads)
    for table,count in (("uploads",len(uploads)),("chunks",chunks),("attachment_grants",len(grants)),
                        ("business_files",links)):
        if deltas.get(table,{}).get("trial_only",0)!=count:
            raise ValueError("trial has files or grants outside the reviewed assignments")
    return uploads,files


def private_open(name,flags):
    return os.open(name,flags,0o600)


def archive_labels(entry):
    if entry["terminal_state"]=="cancelled":
        return "重做版已取消工单",legacy_history.CANCELLED_ARCHIVE
    if entry["terminal_state"]=="failed_archived":
        return "重做版失败归档工单",legacy_history.FAILED_ARCHIVE
    if entry.get("pool_archive"):
        return "重做版池中结单",legacy_history.POOL_CLOSED_ARCHIVE
    return "重做版已验收工单",legacy_history.REVIEWED_ARCHIVE


def archive_in_legacy(path, entries, source_id, at):
    with closing(sqlite3.connect(path)) as db, db:
        for tid,entry in entries.items():
            title,reason = archive_labels(entry)
            if "creation_key" in entry:
                legacy = legacy_history.rollback_archive_id(source_id,tid)
                request = entry["legacy_creation_request"]
                request_id = request["message_id"]
                if db.execute("SELECT 1 FROM task_info WHERE task_id=? OR request_id=?",(legacy,request_id)).fetchone():
                    raise ValueError("archived order collides with a legacy task or request identifier")
                fingerprint = legacy_history.legacy_request_fingerprint(request)
                db.execute("""INSERT INTO task_info(task_id,name,description,owner,actor,at,metadata,request_id,fingerprint)
                              VALUES (?,?,?,?,?,?,?,?,?)""",
                           (legacy,title,"完整记录见私有回退档案。",entry["original_worker"],"hermes",
                            at,"{}",request_id,fingerprint))
                if proof := entry.get("initial_assignment"):
                    legacy_history.remember_assigned_replay(db,source_id,tid,request,proof,archived=True)
                elif request_id!=entry["creation_key"]:
                    db.execute(legacy_history.ROLLBACK_KEYS_SCHEMA)
                    db.execute("INSERT INTO rewrite_archive_request_keys VALUES (?,?,?)",
                               (legacy,entry["creation_key"],source_id))
                entry["legacy_task_id"] = legacy
            else:
                legacy = entry["legacy_task_id"]
                removed = db.execute("DELETE FROM dispatch_queue WHERE task_id=? AND worker=?",(legacy,entry["original_worker"]))
                if removed.rowcount!=1:
                    raise ValueError("legacy queue no longer contains the reviewed original")
            # Keep only the routing outcome and private reference in the shared legacy database.
            metadata = {"reason":reason,"recovery_reference":tid}
            if reason==legacy_history.CANCELLED_ARCHIVE:
                metadata["confirmed_by_worker"] = "cancel_w04_key" in entry
            db.execute("INSERT INTO task_facts(task_id,kind,event_key,at,data) VALUES (?,'archive','archive',?,?)",
                       (legacy,at,encode(metadata)))
        if db.execute("PRAGMA integrity_check").fetchall()!=[("ok",)]:
            raise ValueError("staged legacy database integrity check failed")


def stage_reviewed_rollback(snapshot,trial,review,destination,*,source_plan=None):
    """Prepare an offline fallback without re-enqueueing terminal work."""
    original_tasks = {}
    initial_assignments = {}
    facts = reconcile(snapshot,trial,original_tasks=original_tasks,initial_assignments=initial_assignments)
    source = trial/"router.db"
    try:
        approval = json.loads(review.read_text())
    except FileNotFoundError:
        # Keep the existing readiness diagnostic while the operator has no review yet.
        reviewed_orders(facts,original_tasks,{},with_sources=source_plan is not None)
        raise
    if not isinstance(approval,dict):
        raise ValueError("review must be an object with the trial digest")
    selected = approval.get("replace_migrated_queue",{})
    if not isinstance(selected,dict):
        raise ValueError("replace_migrated_queue must list reviewed orders")
    with closing(readonly(source)) as db:
        replacements = rollback_migrated_queue.reviewed_proofs(db,facts["work_orders"],original_tasks,selected)
    entries = reviewed_orders(facts,original_tasks,replacements,with_sources=source_plan is not None)
    for tid,entry in entries.items():
        if tid in initial_assignments:
            entry["initial_assignment"] = initial_assignments[tid]
    decision = {"trial_sha256":facts["trial_sha256"],
                "closed_assignments":{tid:row["assignment_id"] for tid,row in entries.items()
                                      if is_reviewed_completion(row)}}
    cancelled = {tid:row["assignment_id"] for tid,row in entries.items() if row["terminal_state"]=="cancelled"}
    if cancelled:
        decision["cancelled_assignments"] = cancelled
    pool_archives = {tid:{"assignment_id":row["assignment_id"],"state":row["terminal_state"]}
                     for tid,row in entries.items() if row.get("pool_archive")}
    if pool_archives:
        decision["pool_archives"] = pool_archives
    if facts["queue_candidates"]:
        decision["execution_modes"] = queue_execution_modes(facts["queue_candidates"])
    if replacements:
        decision["replace_migrated_queue"] = {tid:{"legacy_task_id":proof["legacy_task_id"],
                                                  "revision":proof["ready_revision"],"execution_mode":"goal"}
                                              for tid,proof in replacements.items()}
    peer_receipts = {r["message_id"]:r["host_receipt_id"] for r in facts["trial_peers"]}
    if peer_receipts:
        if any(not valid_guid(receipt) for receipt in peer_receipts.values()):
            raise ValueError("trial peer needs a confirmed recipient and native host receipt")
        decision["peer_receipts"] = peer_receipts
    sources = SourceReturns(source_plan,facts["trial_sha256"]) if source_plan is not None else None
    if sources is not None:
        decision["source_plan_sha256"] = sources.sha256
    if approval!=decision:
        raise ValueError("review must match the trial digest, all closed assignments and any new queue modes, cancellations, pool archives, peer receipts or source returns")
    manifest = verified(snapshot)
    if manifest["legacy_db_sha256"]!=facts["snapshot_sha256"]:
        raise ValueError("legacy snapshot changed during review")
    with output_directory(destination) as stage:
        restore_snapshot(snapshot,stage,manifest)
        with closing(readonly(stage/"legacy.db")) as old:
            idle = rollback_idle.IdleObservers({row[0] for row in old.execute("SELECT name FROM workers")})
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
            if sources is not None:
                sources.stage(db,private/"source-returns")
            for tid,entry in entries.items():
                if not is_reviewed_completion(entry):
                    continue
                result = db.execute("SELECT summary FROM reports WHERE task_id=? AND assignment_id=?",
                                    (tid,entry["assignment_id"])).fetchone()
                if not result or not result["summary"].strip():
                    raise ValueError("missing reviewed report")
                entry["summary"] = result["summary"]
            preparation = {**initial_assignments,**replacements}
            rollback_lifecycle.histories(db,entries,facts["changed_tables"],preparation)
            questions = rollback_questions.retain(db,entries,facts["changed_tables"].get("questions",{}).get("trial_only",0))
            proofs,peer_frames,peer_uploads,peer_grants = rollback_peers.reviewed_peers(db,facts["changed_tables"])
            optional_workers = {worker for proof in replacements.values()
                                for worker in (proof["initial"]["assignee_id"],proof["worker_id"])}
            optional_workers.update(worker for proof in proofs.values()
                                    for worker in (proof["row"]["sender"],proof["row"]["recipient"]))
            workers,frames,inquiries = rollback_lifecycle.check_transport(
                db,entries,questions,optional_workers,peer_frames,idle,sources)
            audited_uploads, audited_grants = peer_uploads,peer_grants
            if sources is not None:
                audited_uploads = {**peer_uploads,**sources.uploads}
                audited_grants = peer_grants | sources.grants
            uploads,files = copy_reviewed_files(db,entries,facts["changed_tables"],private/"files",
                                                audited_uploads,audited_grants)
            rollback_lifecycle.check_operations(db,entries,workers,frames,uploads,
                                                facts["changed_tables"].get("operations",{}).get("trial_only",0),
                                                facts["queue_candidates"],questions,inquiries,preparation,
                                                rollback_peers.ReceiptLedger(proofs),idle,sources)
            observers = idle.finish(db)
            previous_sha = facts.get("peer_archive_sha256")
            previous = None
            if previous_sha:
                previous = private/"previous-peer-history.db"
                rollback_peers.freeze(trial/rollback_peers.NAME,previous_sha,previous)
            peer_sha = rollback_peers.write_archive(db,private/rollback_peers.NAME,previous,facts["source_id"],
                                                     facts["trial_sha256"],proofs,peer_uploads)
            if previous is not None:
                previous.unlink(missing_ok=True)
            for tid,entry in entries.items():
                if "creation_key" in entry:
                    original = dict(entry["original_content"],assignee_id=entry["original_worker"])
                    entry["legacy_creation_request"] = legacy_creation_request(facts["source_id"],
                        entry["creation_key"],original,assigned="initial_assignment" in entry)
            at = time.time()
            archive_in_legacy(stage/"legacy.db",entries,facts["source_id"],at)
            applied = rollback_migrated_queue.replace_orders(stage/"legacy.db",facts["source_id"],replacements,at) if replacements else {}
            plan = stage_queue_requests(db,stage/"legacy.db",facts,initial_assignments)
            if not plan["requests"]:
                plan = None
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
            if peer_sha:
                recovery["peer_archive_sha256"] = peer_sha
            if observers:
                recovery["idle_observers"] = observers
            if sources is not None:
                recovery["source_returns"] = sources.reports
                recovery["source_consumer_sha256"] = sources.consumer_hashes
            if applied:
                recovery["migrated_queue_replacements"] = applied
            output.write(encode(recovery))
        with closing(readonly(source)):
            if digest(source)!=digest(frozen):
                raise ValueError("trial database changed while staging rollback")
    result = {"reviewed_orders":len(entries),"cancelled_orders":len(cancelled),"pool_archived_orders":len(pool_archives),
              "private_artifacts":len(files),"trial_sha256":facts["trial_sha256"],
              "restored_legacy_sha256":restored_sha,"queued_requests":len(facts["queue_candidates"]),
              "queue_replay_sha256":queue_manifest_sha}
    if applied:
        fields = ("legacy_task_id","replacement_task_id","queue_position","worker_id")
        result["migrated_queue_replacements"] = {tid:{key:row[key] for key in fields} for tid,row in applied.items()}
    if peer_sha:
        result.update(peer_archive_sha256=peer_sha,reviewed_peers=len(proofs))
    if observers:
        result["idle_observers"] = len(observers)
    if sources is not None:
        result["source_returns"] = sources.reports
    return result
