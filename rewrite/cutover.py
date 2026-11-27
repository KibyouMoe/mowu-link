"""Rehearse a Router transition using offline database and attachment copies."""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from uuid import UUID, uuid5

from .orders import Orders, task
from . import attachments, cleanup, legacy_history, legacy_peers, rollback_assignment
from .messages import valid_guid
from .storage import encode


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def readonly(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("supply a regular offline database copy")
    for suffix in ("-wal","-journal"):
        if path.with_name(path.name + suffix).exists():
            raise ValueError("database has a live journal; close the isolated writer and checkpoint first")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    return db


@contextmanager
def output_directory(destination):
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    with tempfile.TemporaryDirectory(prefix=".mowulink-stage-",dir=destination.parent) as tmp:
        staging = Path(tmp) / "files"
        staging.mkdir(mode=0o700)
        yield staging
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        staging.rename(destination)


DEFAULT_QUEUE_META = {"context":None,"workspace":None,"urgency":"normal","deadline_s":None,"execution_mode":"goal"}
LEGACY_GOAL = ("[正式工单 · 目标模式]\n先核对宿主原生目标。支持目标工具且没有未完成目标时，为本单建立目标，"
               "不自设 token 预算。已有目标时关联同单补充或子项；遇到独立冲突目标，保留原目标并说明冲突。"
               "以原生工具返回、执行及验证结果为准。")


def queue_metadata_supported(raw,version):
    meta = json.loads(raw)
    if meta=={}:
        return True
    if version==1 or not isinstance(meta,dict) or not isinstance(meta.get("context"),(type(None),str)):
        return False
    return dict(meta,context=None)==DEFAULT_QUEUE_META and "context" in meta


def queue_instructions(description, meta):
    context = (meta.get("context") or "").strip()
    body = description + ("\n\n[router context]\n"+context if context else "")
    instructions = LEGACY_GOAL+"\n\n"+body if meta else body
    return body,instructions


def inventory(db, version=2):
    if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ValueError("database integrity check failed")
    required = {"workers","active_tasks","dispatch_queue","task_info","task_facts","peer_messages",
                "approvals","task_questions","subscriptions","worker_task_corrections","chat_messages"}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= tables:
        raise ValueError("unsupported legacy Router schema")
    db.create_function("simple_queue_metadata",1,lambda raw:queue_metadata_supported(raw,version))
    queries = {
        "held_tasks":"SELECT count(*) FROM active_tasks",
        "worker_holds":"SELECT count(*) FROM workers WHERE task_id IS NOT NULL AND task_id NOT IN ('','0')",
        "unconfirmed_peer_messages":"SELECT count(*) FROM peer_messages WHERE acked_at IS NULL AND abandoned_at IS NULL",
        "pending_approvals":"SELECT count(*) FROM approvals WHERE status='pending' OR notified=0",
        "open_questions":"SELECT count(*) FROM task_questions WHERE status NOT IN ('answered','skipped','expired','cancelled','withdrawn')",
        "active_subscriptions":"SELECT count(*) FROM subscriptions WHERE active=1",
        "ownership_corrections":"SELECT count(*) FROM worker_task_corrections WHERE cleared_at IS NULL",
        "ambiguous_queue_rows":"""SELECT count(*) FROM dispatch_queue q LEFT JOIN task_info i USING(task_id)
              WHERE i.task_id IS NULL OR i.owner<>q.worker OR trim(i.name)='' OR trim(i.description)=''
              OR trim(i.owner)='' OR NOT simple_queue_metadata(i.metadata) OR EXISTS
              (SELECT 1 FROM task_facts f WHERE f.task_id=q.task_id)""",
        "unclassified_tasks":"""SELECT count(*) FROM task_info i WHERE NOT EXISTS
              (SELECT 1 FROM dispatch_queue q WHERE q.task_id=i.task_id) AND NOT EXISTS
              (SELECT 1 FROM task_facts f WHERE f.task_id=i.task_id AND f.kind='archive')""",
    }
    if "tasks" in tables:
        # Pre-ledger task layouts need separate interpretation. Preserve them intact in the backup.
        queries["pre_ledger_tasks"] = "SELECT count(*) FROM tasks"
    return {key:db.execute(sql).fetchone()[0] for key,sql in queries.items()}


def attachment_index(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("expected an offline legacy attachment directory")
    result = {}
    for file in sorted(directory.iterdir()):
        if not re.fullmatch("[0-9a-f]{64}",file.name) or file.is_symlink() or not file.is_file():
            raise ValueError("unrecognized attachment entry or unfinished upload")
        if digest(file) != file.name:
            raise ValueError("legacy attachment digest mismatch")
        result[file.name] = file.stat().st_size
    return result


def verify_references(db, files):
    for (raw,) in db.execute("SELECT attachments_json FROM peer_messages"):
        references = json.loads(raw)
        if not isinstance(references,list):
            raise ValueError("invalid legacy peer attachment list")
        for entry in references:
            if not isinstance(entry,dict) or entry.get("id") not in files:
                raise ValueError("legacy peer attachment is missing from snapshot")


def backup(database, attachments, destination):
    with output_directory(destination) as stage:
        with closing(readonly(database)) as db:
            before = digest(database)
            blockers = inventory(db)
            files = attachment_index(attachments)
            verify_references(db,files)
        shutil.copyfile(database,stage/"legacy.db")
        shutil.copytree(attachments,stage/"attachments")
        if (digest(database) != before or digest(stage/"legacy.db") != before
                or attachment_index(stage/"attachments") != files or attachment_index(attachments) != files):
            raise ValueError("offline source changed during backup")
        with closing(readonly(database)):
            pass
        manifest = {"format":2,"legacy_db_sha256":before,"attachments":files,"blockers":blockers}
        (stage/"manifest.json").write_text(encode(manifest))
    return manifest


def verified(snapshot):
    manifest = json.loads((snapshot/"manifest.json").read_text())
    if (manifest.get("format") not in (1,2) or digest(snapshot/"legacy.db") != manifest["legacy_db_sha256"]
            or attachment_index(snapshot/"attachments") != manifest["attachments"]):
        raise ValueError("snapshot verification failed")
    with closing(readonly(snapshot/"legacy.db")) as db:
        verify_references(db,manifest["attachments"])
        if inventory(db,manifest["format"]) != manifest["blockers"]:
            raise ValueError("snapshot inventory changed")
    return manifest


def export_chat(snapshot, destination, worker, after=0, limit=500, direction=None):
    if (not isinstance(worker,str) or not worker.strip() or type(after) is not int or not 0<=after<2**63
            or type(limit) is not int or not 1<=limit<=5000 or direction not in (None,"in","out")):
        raise ValueError("supply an exact worker, a nonnegative cursor and a valid export range")
    manifest = verified(snapshot)
    with output_directory(destination) as stage, closing(readonly(snapshot/"legacy.db")) as db:
        rows = db.execute("""SELECT id,worker,direction,text,session_id,ts,message_id FROM chat_messages
            WHERE worker=? AND id>? AND (? IS NULL OR direction=?) ORDER BY id LIMIT ?""",
            (worker,after,direction,direction,limit+1)).fetchall()
        path = stage/"messages.jsonl"
        with open(path,"x",encoding="utf-8",opener=lambda name,flags:os.open(name,flags,0o600)) as stream:
            for row in rows[:limit]:
                stream.write(encode(dict(row))+"\n")
        result = {"snapshot_sha256":manifest["legacy_db_sha256"],"worker":worker,"direction":direction,
                  "after":after,"count":min(len(rows),limit),"next_cursor":rows[limit-1]["id"] if len(rows)>limit else None,
                  "messages_sha256":digest(path)}
        if digest(snapshot/"legacy.db")!=manifest["legacy_db_sha256"]:
            raise ValueError("snapshot changed during export")
        (stage/"manifest.json").write_text(encode(result),encoding="utf-8")
    return result


def prepare(snapshot, destination, source_id, peer_archive=None, peer_archive_sha256=None):
    from . import rollback_peers

    if (peer_archive is None)!=(peer_archive_sha256 is None):
        raise ValueError("peer archive path and trusted SHA-256 must be supplied together")
    namespace = UUID(source_id)
    manifest = verified(snapshot)
    with closing(readonly(snapshot/"legacy.db")) as old:
        blockers = {key:n for key,n in inventory(old).items() if n}
    if blockers:
        raise ValueError("legacy work requires reconciliation: " + encode(blockers))
    with output_directory(destination) as stage, closing(readonly(snapshot/"legacy.db")) as old:
        if peer_archive is not None:
            rollback_peers.freeze(peer_archive,peer_archive_sha256,stage/rollback_peers.NAME)
        router = Orders(stage/"router.db")
        mapping = {}
        with router.db.transaction() as db:
            for row in old.execute("""SELECT q.task_id,i.name,i.description,i.metadata,i.request_id,i.actor,q.worker FROM dispatch_queue q
                                      JOIN task_info i USING(task_id) ORDER BY q.seq"""):
                tid = str(uuid5(namespace,row["task_id"]))
                body,instructions = queue_instructions(row["description"],json.loads(row["metadata"]))
                db.execute("INSERT INTO tasks(task_id,title,instructions,assignee_id) VALUES (?,?,?,?)",
                           (tid,row["name"],instructions,row["worker"]))
                if row["actor"]=="hermes" and row["request_id"]:
                    original = {"title":row["name"],"instructions":body,"assignee_id":row["worker"]}
                    db.execute("INSERT INTO operations VALUES ('hermes',?,?,?)",
                               (row["request_id"],encode([["POST","/v1/tasks"],original]),encode(task(db,tid))))
                mapping[row["task_id"]] = tid
            legacy_history.populate(db,old,str(namespace),manifest["legacy_db_sha256"],mapping)
            legacy_peers.populate(db,old,str(namespace),snapshot/"attachments")
            if peer_archive is not None:
                rollback_peers.import_archive(db,stage/rollback_peers.NAME)
        baseline = {"snapshot_sha256":manifest["legacy_db_sha256"],"source_id":str(namespace),
                    "router_db_sha256":digest(stage/"router.db"),"queued_id_map":mapping}
        if peer_archive is not None:
            baseline["peer_archive_sha256"] = peer_archive_sha256
        (stage/"baseline.json").write_text(encode(baseline))
    return baseline


def rollback(snapshot, trial, destination):
    from . import rollback_peers

    manifest = verified(snapshot)
    baseline = json.loads((trial/"baseline.json").read_text())
    with closing(readonly(trial/"router.db")):
        if baseline["snapshot_sha256"] != manifest["legacy_db_sha256"]:
            raise ValueError("snapshot does not belong to this rehearsal")
        if digest(trial/"router.db") != baseline["router_db_sha256"]:
            raise ValueError("new Router changed; reconcile new writes before rollback")
    with output_directory(destination) as stage:
        restore_snapshot(snapshot,stage,manifest)
        peer_sha = baseline.get("peer_archive_sha256")
        rollback_peers.archive_from_trial(trial,stage,peer_sha)
    result = {"restored_db_sha256":manifest["legacy_db_sha256"],"attachments":len(manifest["attachments"])}
    if peer_sha is not None:
        result["peer_archive_sha256"] = peer_sha
    return result


def restore_snapshot(snapshot, stage, manifest):
    shutil.copyfile(snapshot/"legacy.db",stage/"legacy.db")
    shutil.copytree(snapshot/"attachments",stage/"attachments")
    if (digest(stage/"legacy.db")!=manifest["legacy_db_sha256"]
            or attachment_index(stage/"attachments")!=manifest["attachments"]):
        raise ValueError("restored bytes failed verification")


def schema(db, database):
    return db.execute(f"""SELECT type,name,tbl_name,sql FROM {database}.sqlite_master
                          WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name""").fetchall()


def row_difference(table, first, second):
    name = '"' + table.replace('"','""') + '"'
    return f"SELECT rowid,* FROM {first}.{name} EXCEPT SELECT rowid,* FROM {second}.{name}"


def row_delta(db, table, first, second):
    return db.execute(f"SELECT count(*) FROM ({row_difference(table,first,second)})").fetchone()[0]


def creation_receipt(db, row):
    recorded = db.execute("""SELECT rowid AS seq,actor,key,request,response FROM main.operations WHERE actor='hermes'
                             AND json_extract(request,'$[0][0]')='POST' AND json_extract(request,'$[0][1]')='/v1/tasks'
                             AND json_extract(response,'$.task_id')=?
                             AND key NOT IN (SELECT key FROM baseline.operations WHERE actor='hermes')""",
                          (row["task_id"],)).fetchall()
    if len(recorded)!=1:
        return None
    return recorded[0]


def portable_request_key(key):
    return isinstance(key,str) and (valid_guid(key) or re.fullmatch(r"hermes-[0-9a-f]{64}",key))


def portable_creation(row, operation):
    request = {field:row[field] for field in ("title","instructions","assignee_id")}
    return (portable_request_key(operation["key"])
            and operation["request"]==encode([["POST","/v1/tasks"],request]))


def legacy_description_fits(row):
    return row["instructions"]==row["instructions"].strip() and len(row["instructions"])<=2000


def queued_candidates(db, delta, legacy_workers, initial_assignments):
    candidates, blocked = [], []
    sql = """SELECT current.* FROM main.tasks current LEFT JOIN baseline.tasks old USING(task_id)
             WHERE old.task_id IS NULL ORDER BY current.rowid"""
    for row in db.execute(sql):
        tid = row["task_id"]
        if row["state"]!="ready" or row["assignment_id"] is not None:
            blocked.append({"task_id":tid,"reason":"assignment_or_lifecycle_recorded"})
            continue
        if not row["assignee_id"]:
            blocked.append({"task_id":tid,"reason":"legacy_requires_assignee"})
            continue
        if row["assignee_id"]!=row["assignee_id"].strip() or row["assignee_id"] not in legacy_workers:
            blocked.append({"task_id":tid,"reason":"assignee_not_registered_in_legacy_snapshot"})
            continue
        operation = creation_receipt(db,row)
        if operation is None:
            blocked.append({"task_id":tid,"reason":"missing_unique_manager_creation_receipt"})
            continue
        # Old /tasks trims the description and accepts at most 2,000 characters.
        if not legacy_description_fits(row):
            blocked.append({"task_id":tid,"reason":"legacy_description_would_change_or_be_rejected"})
            continue
        item = {"task_id":tid,"assignee_id":row["assignee_id"],"request_id":operation["key"]}
        proof = None
        if portable_request_key(operation["key"]) and first_dispatch(db,tid) is None:
            proof = rollback_assignment.verify(db,row,operation,None,allow_edit_only=True)
        if proof:
            if proof["assignment_request_id"] is not None:
                item["assignment_request_id"] = proof["assignment_request_id"]
            initial_assignments[tid] = proof
        elif row["revision"]==1:
            if not portable_creation(row,operation) or operation["response"]!=encode(dict(row,holder={"kind":"router"})):
                blocked.append({"task_id":tid,"reason":"creation_receipt_incompatible"})
                continue
        else:
            blocked.append({"task_id":tid,"reason":"assignment_or_lifecycle_recorded"})
            continue
        candidates.append(item)
    queued_operations = len(candidates)+sum(len(proof["events"]) for proof in initial_assignments.values())
    only_queue = (bool(candidates) and not blocked and set(delta)=={"tasks","operations"}
                  and delta["tasks"]=={"baseline_only":0,"trial_only":len(candidates)}
                  and delta["operations"]=={"baseline_only":0,"trial_only":queued_operations})
    return candidates,blocked,only_queue


def work_order_changes(db, delta, old_ids, original_tasks):
    """Locate changed formal records without exporting instructions, results or file bytes."""
    touched = set()
    for table in ("tasks","reports","materials"):
        if table not in delta:
            continue
        for first,second in (("main","baseline"),("baseline","main")):
            touched.update(row[0] for row in db.execute(
                f"SELECT task_id FROM ({row_difference(table,first,second)})"))
    records = []
    for tid in sorted(touched):
        before = db.execute("""SELECT assignee_id,assignment_id,revision,state FROM baseline.tasks
                              WHERE task_id=?""",(tid,)).fetchone()
        after = db.execute("""SELECT assignee_id,assignment_id,revision,state FROM main.tasks
                             WHERE task_id=?""",(tid,)).fetchone()
        same_content = None
        if before and after:
            same_content = bool(db.execute("""SELECT 1 FROM main.tasks t JOIN baseline.tasks b USING(task_id)
                 WHERE t.task_id=? AND t.title=b.title AND t.instructions=b.instructions""",(tid,)).fetchone())
            if original_tasks is not None:
                original_tasks[tid] = dict(db.execute("SELECT title,instructions FROM baseline.tasks WHERE task_id=?",
                                                      (tid,)).fetchone())
        reports = [dict(row,artifact_ids=attachments.linked(db,"report",row["assignment_id"]))
                   for row in db.execute("""SELECT assignment_id,worker_id FROM main.reports
                                           WHERE task_id=? ORDER BY rowid""",(tid,))]
        materials = [dict(row,artifact_ids=attachments.linked(db,"material",row["material_id"]))
                     for row in db.execute("""SELECT material_id,assignment_id,submitted_by FROM main.materials
                                              WHERE task_id=? ORDER BY rowid""",(tid,))]
        records.append({"task_id":tid,"legacy_task_id":old_ids.get(tid),"before":dict(before) if before else None,
                        "trial":dict(after) if after else None,"same_content":same_content,
                        "completed_reports":reports,"candidate_materials":materials})
        if after and (after["state"] in ("cancelled","failed_archived") or after["state"]=="closed" and not reports):
            offer = db.execute("""SELECT json_extract(payload,'$.assignment_id') FROM main.deliveries
                WHERE kind='task.dispatch' AND json_extract(payload,'$.task_id')=? ORDER BY rowid DESC LIMIT 1""",
                (tid,)).fetchone()
            field = "cancelled_assignment_id" if after["state"]=="cancelled" else "pool_archive_assignment_id"
            records[-1][field] = offer[0] if offer else None
    return records


def first_dispatch(db, tid):
    return db.execute("""SELECT consumer,payload FROM main.deliveries WHERE kind='task.dispatch'
        AND json_extract(payload,'$.task_id')=? ORDER BY rowid LIMIT 1""",(tid,)).fetchone()


def archived_new_candidates(db, changes, legacy_workers, original_tasks, initial_assignments):
    candidates = {"closed":[],"cancelled":[],"pool":[]}
    for change in changes:
        after, reports = change["trial"], change["completed_reports"]
        if (change["before"] is not None or change["legacy_task_id"] is not None or not after
                or after["assignment_id"] is not None):
            continue
        state = after["state"]
        if state=="closed" and len(reports)==1 and reports[0]["worker_id"]==after["assignee_id"]:
            category = "closed"
        elif state=="cancelled" and not reports:
            category = "cancelled"
        elif not reports and change.get("pool_archive_assignment_id"):
            category = "pool"
        else:
            continue
        row = task(db,change["task_id"])
        first = first_dispatch(db,row["task_id"])
        if ((first and not first["consumer"].startswith("worker/"))
                or (not first and category!="cancelled")):
            continue
        original_worker = first["consumer"].removeprefix("worker/") if first else row["assignee_id"]
        if original_worker not in legacy_workers:
            continue
        operation = creation_receipt(db,row)
        if not operation:
            continue
        creation = json.loads(operation["response"])
        if not all(isinstance(creation.get(field),str) and creation[field].strip() for field in ("title","instructions")):
            continue
        original = dict(row,state="ready",revision=1,assignee_id=original_worker,
                        title=creation["title"],instructions=creation["instructions"])
        if not legacy_description_fits(original):
            continue
        accepted = [encode(original)]
        if first:
            accepted.append(encode(dict(original,revision=2,assignment_id=json.loads(first["payload"])["assignment_id"])))
        item = {"task_id":row["task_id"],"assignee_id":original_worker,"request_id":operation["key"]}
        proof = rollback_assignment.verify(db,row,operation,first) if portable_request_key(operation["key"]) else None
        if proof:
            if proof["worker_id"]!=original_worker:
                continue
            initial_assignments[row["task_id"]] = proof
            item["assignment_request_id"] = proof["assignment_request_id"]
        elif not (portable_creation(original,operation) and operation["response"] in accepted):
            continue
        candidates[category].append(item)
        if original_tasks is not None:
            original_tasks[row["task_id"]] = {field:original[field] for field in ("title","instructions")}
    return candidates


def reconcile(snapshot, trial, *, original_tasks=None, initial_assignments=None):
    """Read the exact post-cutover difference. Original snapshots and trial databases stay intact."""
    from . import rollback_peers

    saved = json.loads((trial/"baseline.json").read_text())
    source = trial/"router.db"
    result = {}
    assignments = {} if initial_assignments is None else initial_assignments
    with tempfile.TemporaryDirectory(prefix="mowulink-reconcile-") as directory:
        reference = Path(directory)/"reference"
        peer_sha = saved.get("peer_archive_sha256")
        peer_archive = trial/rollback_peers.NAME if peer_sha is not None else None
        regenerated = prepare(snapshot,reference,saved["source_id"],peer_archive,peer_sha)
        if regenerated!=saved:
            raise ValueError("rehearsal baseline could not be reproduced by this release")
        with closing(readonly(snapshot/"legacy.db")) as old:
            legacy_workers = {row[0] for row in old.execute("SELECT name FROM workers")}
        with closing(readonly(source)) as db:
            before = digest(source)
            result.update(source_id=saved["source_id"],snapshot_sha256=saved["snapshot_sha256"],
                          baseline_sha256=saved["router_db_sha256"],
                          trial_sha256=before)
            if [row[0] for row in db.execute("PRAGMA integrity_check")]!=["ok"]:
                raise ValueError("trial database integrity check failed")
            if cleanup.has_scope_column(db):
                with closing(sqlite3.connect(reference/"router.db")) as copy,copy:
                    cleanup.enable_scopes(copy)
            db.execute("ATTACH DATABASE ? AS baseline",
                       ((reference/"router.db").resolve().as_uri()+"?mode=ro&immutable=1",))
            baseline_schema = schema(db,"baseline")
            if baseline_schema!=schema(db,"main"):
                raise ValueError("trial schema does not match this release")
            delta = {}
            for obj in baseline_schema:
                if obj["type"]!="table":
                    continue
                table = obj["name"]
                baseline_only = row_delta(db,table,"baseline","main")
                trial_only = row_delta(db,table,"main","baseline")
                if baseline_only or trial_only:
                    delta[table] = {"baseline_only":baseline_only,"trial_only":trial_only}
            candidates,blocked,only_queue = queued_candidates(db,delta,legacy_workers,assignments)
            old_ids = {new:old for old,new in saved["queued_id_map"].items()}
            changes = work_order_changes(db,delta,old_ids,original_tasks)
            archived = archived_new_candidates(db,changes,legacy_workers,original_tasks,assignments)
            archived_ids = {item["task_id"] for group in archived.values() for item in group}
            result.update(changed_tables=delta,queue_candidates=candidates,closed_new_tasks=archived["closed"],
                          cancelled_new_tasks=archived["cancelled"],
                          archived_pool_new_tasks=archived["pool"],
                          blocked_new_tasks=[item for item in blocked if item["task_id"] not in archived_ids],
                          work_orders=changes,trial_peers=rollback_peers.review_rows(db),
                          exact_rollback_allowed=before==saved["router_db_sha256"],only_new_queued_tasks=only_queue)
            if peer_sha is not None:
                result["peer_archive_sha256"] = peer_sha
        with closing(readonly(source)):
            if digest(source)!=before:
                raise ValueError("trial database changed during reconciliation")
    return result


def queue_execution_modes(candidates):
    return {item["task_id"]:"goal" for item in candidates}


def legacy_creation_request(source_id, key, row, *, assigned=False):
    request_id = legacy_history.assigned_request_id if assigned else legacy_history.rollback_request_id
    legacy_key = request_id(source_id,key)
    return {"message_id":legacy_key,"worker":row["assignee_id"],"TaskName":row["title"],
            "goal":row["instructions"],"execution_mode":"goal","queue_if_offline":True}


def stage_queue_requests(db, legacy, facts, initial_assignments):
    requests = []
    legacy_keys = set()
    with closing(readonly(legacy)) as old:
        for item in facts["queue_candidates"]:
            tid,key = item["task_id"],item["request_id"]
            row = db.execute("SELECT title,instructions,assignee_id FROM tasks WHERE task_id=?",(tid,)).fetchone()
            if row is None:
                raise ValueError("trial order disappeared after reconciliation")
            proof = initial_assignments.get(tid)
            body = legacy_creation_request(facts["source_id"],key,row,assigned=proof is not None)
            legacy_key = body["message_id"]
            if legacy_key in legacy_keys or old.execute("SELECT 1 FROM task_info WHERE request_id=?",(legacy_key,)).fetchone():
                raise ValueError("creation request already exists in the rollback queue")
            legacy_keys.add(legacy_key)
            request = {"trial_task_id":tid,"trial_request_id":key,"request":body}
            if proof:
                request["initial_assignment"] = proof
            requests.append(request)
    with closing(sqlite3.connect(legacy)) as old, old:
        for row in requests:
            if proof := row.get("initial_assignment"):
                legacy_history.remember_assigned_replay(old,facts["source_id"],row["trial_task_id"],row["request"],proof)
            else:
                old.execute(legacy_history.QUEUE_REPLAY_SCHEMA)
                old.execute("INSERT INTO rewrite_queue_replay_keys VALUES (?,?,?,?)",
                            (row["request"]["message_id"],row["trial_request_id"],facts["source_id"],
                             legacy_history.legacy_request_fingerprint(row["request"])))
        legacy_history.check_assigned_key_overlap(old)
    return {"source_id":facts["source_id"],"trial_sha256":facts["trial_sha256"],
            "legacy_sha256":facts["snapshot_sha256"],"requests":requests}


def stage_queue_rollback(snapshot, trial, review, destination):
    """Prepare a legacy sandbox and reviewed requests. No live API is called."""
    from . import rollback_peers

    initial_assignments = {}
    facts = reconcile(snapshot,trial,initial_assignments=initial_assignments)
    if not facts["only_new_queued_tasks"]:
        raise ValueError("queue replay requires only new unoffered orders")
    source = trial/"router.db"
    approval = json.loads(review.read_text())
    expected = queue_execution_modes(facts["queue_candidates"])
    if approval!={"trial_sha256":facts["trial_sha256"],"execution_modes":expected}:
        raise ValueError("review must match the trial digest and confirm goal mode for every order")
    manifest = verified(snapshot)
    if manifest["legacy_db_sha256"]!=facts["snapshot_sha256"]:
        raise ValueError("legacy snapshot changed after reconciliation")
    with output_directory(destination) as stage:
        restore_snapshot(snapshot,stage,manifest)
        peer_sha = facts.get("peer_archive_sha256")
        rollback_peers.archive_from_trial(trial,stage,peer_sha)
        with closing(readonly(source)) as db:
            plan = stage_queue_requests(db,stage/"legacy.db",facts,initial_assignments)
        with closing(readonly(source)):
            if digest(source)!=facts["trial_sha256"]:
                raise ValueError("trial database changed after reconciliation")
        restored_sha = digest(stage/"legacy.db")
        with open(stage/"queue-replay.json","x",encoding="utf-8",
                  opener=lambda name,flags:os.open(name,flags,0o600)) as output:
            output.write(encode({**plan,"restored_legacy_sha256":restored_sha}))
    result = {"trial_sha256":facts["trial_sha256"],"legacy_sha256":manifest["legacy_db_sha256"],
              "restored_legacy_sha256":restored_sha,"queued_requests":len(plan["requests"])}
    if peer_sha is not None:
        result["peer_archive_sha256"] = peer_sha
    return result


def main():
    from .reviewed_rollback import stage_reviewed_rollback

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="op", required=True)
    for op, names in (("backup",("database","attachments","destination")),
                      ("verify",("snapshot",)),("prepare",("snapshot","destination")),
                      ("reconcile",("snapshot","trial")),
                      ("stage-queue-rollback",("snapshot","trial","review","destination")),
                      ("stage-reviewed-rollback",("snapshot","trial","review","destination")),
                      ("rollback",("snapshot","trial","destination")),
                      ("export-chat",("snapshot","destination"))):
        command = commands.add_parser(op)
        for name in names:
            command.add_argument("--"+name,type=Path,required=True)
        if op == "prepare":
            command.add_argument("--source-id",required=True)
            command.add_argument("--peer-archive",type=Path)
            command.add_argument("--peer-archive-sha256")
        elif op == "export-chat":
            command.add_argument("--worker",required=True)
            command.add_argument("--after",type=int,default=0)
            command.add_argument("--limit",type=int,default=500)
            command.add_argument("--direction",choices=("in","out"))
    args = vars(parser.parse_args())
    command = {"backup":backup,"verify":verified,"prepare":prepare,
               "reconcile":reconcile,"rollback":rollback,"stage-queue-rollback":stage_queue_rollback,
               "stage-reviewed-rollback":stage_reviewed_rollback,
               "export-chat":export_chat}[args.pop("op")]
    print(encode(command(**args)))


if __name__ == "__main__":
    main()
