"""Offline component state snapshots. Keep revision and manifest digest outside the snapshot."""
import argparse
from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory

from . import delivery, dsh_workspace_audit, feishu_audit, trigger_audit
from . import state_bundle_managed as managed
from .cutover import digest, output_directory, readonly
from .messages import valid_guid
from .storage import encode, guid, state_writer

TABLES = {
    "router":{"tasks","workers","reports","operations","deliveries"},
    "worker":{"connection","jobs","inbox","outbox","questions","chats"},
    "agent":{"intents","question_resolutions"},
    "hermes":{"progress","queue"},
    "hermes-host":{"intents","question_resolutions"},
    "trigger":{"instance","archive","files","pieces"},
    "feishu":{"binding","platform_events","view_cursor","view_attempt"},
}
REQUIRED = {"router","worker","agent","hermes","trigger"}
WORKER_NATIVE_PENDING = {
    "inbox": ({"inbox"},"SELECT COUNT(*) FROM inbox WHERE done=0"),
    "private_chat": ({"chats","chat_deliveries"},"""SELECT COUNT(*) FROM chats c
        LEFT JOIN chat_deliveries n USING(message_id) WHERE n.receipt IS NULL"""),
    "queued_chat": ({"queued_chats"},"SELECT COUNT(*) FROM queued_chats WHERE receipt IS NULL"),
    "peer_ingress": ({"peers","peer_receipt_acks"},"""SELECT COUNT(*) FROM peers p
        LEFT JOIN peer_receipt_acks a USING(message_id) WHERE p.direction='in' AND a.ack IS NULL"""),
    "trainer_questions": ({"trainer_questions"},"SELECT COUNT(*) FROM trainer_questions WHERE proof IS NULL"),
    "compactions": ({"platform_compactions"},"SELECT COUNT(*) FROM platform_compactions WHERE native_receipt IS NULL"),
    "cleanup_reports": ({"cleanup_reports"},"SELECT COUNT(*) FROM cleanup_reports WHERE receipt IS NULL"),
    "manager_questions": ({"questions"},"SELECT COUNT(*) FROM questions WHERE answer IS NULL"),
    "native_command_writes": ({"platform_model_intents","platform_commands"},"""SELECT COUNT(*)
        FROM platform_model_intents i LEFT JOIN platform_commands c USING(message_id) WHERE c.message_id IS NULL"""),
    "outbound_peers": ({"peers","peer_retry"},"""SELECT COUNT(*) FROM peers p LEFT JOIN peer_retry r
        ON r.operation_id=p.message_id AND r.phase='send_rejected'
        WHERE p.direction='out' AND p.receipt IS NULL AND r.operation_id IS NULL"""),
}
AGENT_NATIVE_PENDING = {
    "adapter_deliveries": ({"intents"},"SELECT COUNT(*) FROM intents WHERE receipt IS NULL"),
    "adapter_questions": ({"question_resolutions"},"SELECT COUNT(*) FROM question_resolutions WHERE proof IS NULL"),
}
OPTIONAL_AGENT_PENDING = {
    "dsh_public_events": ({"dsh_public_outbox"},"SELECT COUNT(*) FROM dsh_public_outbox WHERE ack IS NULL"),
    "dsh_approval_closures": ({"dsh_approvals"},"SELECT COUNT(*) FROM dsh_approvals WHERE closure IS NULL"),
    "dsh_session_preparations": ({"dsh_thread_preparations"},"SELECT COUNT(*) FROM dsh_thread_preparations WHERE state<>'prepared'"),
    "codex_approval_closures": ({"codex_approval_intents"},
                              "SELECT COUNT(*) FROM codex_approval_intents WHERE router_receipt_digest IS NULL"),
    "codex_public_events": ({"codex_display_live"},"SELECT COUNT(*) FROM codex_display_live WHERE sent=0"),
    "codex_public_turns": ({"codex_display_turns"},"SELECT COUNT(*) FROM codex_display_turns WHERE sent=0"),
    "codex_session_preparations": ({"codex_thread_preparations"},
                                  "SELECT COUNT(*) FROM codex_thread_preparations WHERE state<>'prepared'"),
}
WORKER_ADMISSION_PENDING = {
    "cleanup_waits": ({"cleanup_waits"},"SELECT COUNT(*) FROM cleanup_waits"),
    "scoped_cleanup_reports": ({"scoped_cleanup_reports"},"SELECT COUNT(*) FROM scoped_cleanup_reports WHERE receipt IS NULL"),
    "local_admission_handoffs": ({"native_handoff_admission"},"""SELECT COUNT(*) FROM native_handoff_admission
        WHERE state IN ('beginning','frozen','ending')"""),
    "native_resume_operations": ({"native_resume"},"SELECT COUNT(*) FROM native_resume WHERE phase<>'done'"),
}
FEISHU_NATIVE_PENDING = {
    "platform_inputs": ({"platform_events"},"SELECT COUNT(*) FROM platform_events WHERE state='pending'"),
    "platform_answers": ({"card_answers"},"SELECT COUNT(*) FROM card_answers WHERE state='pending'"),
    "platform_display_attempts": ({"view_attempt"},"SELECT COUNT(*) FROM view_attempt WHERE mid IS NULL"),
    "platform_input_notices": ({"input_notices"},"SELECT COUNT(*) FROM input_notices WHERE outcome<>'sent'"),
}
FEISHU_OPTIONAL_PENDING = {
    "platform_known_receipts_uncommitted": ({"view_attempt","view_writes"},"""SELECT COUNT(*) FROM view_attempt
        JOIN view_writes USING(seq) WHERE view_attempt.mid IS NULL AND view_writes.platform_mid IS NOT NULL"""),
    "platform_image_uploads_unconfirmed": ({"view_image_uploads"},"SELECT COUNT(*) FROM view_image_uploads"),
}


def component(label):
    role,sep,name = label.partition(":")
    if not sep or role not in TABLES or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{0,63}",name):
        raise ValueError("invalid role or instance label")
    return role,name


def filename(label):
    role,name = component(label)
    return role+"--"+name+".sqlite3"


def validate_set(labels,*,partial=False):
    labels = list(labels)
    roles = [component(label)[0] for label in labels]
    if len({filename(label).casefold() for label in labels})!=len(labels):
        raise ValueError("component filenames must also be distinct on case-insensitive filesystems")
    if partial:
        if not roles or roles.count("router")>1:
            raise ValueError("a host snapshot needs at least one component and at most one Router")
        return
    if not REQUIRED <= set(roles) or roles.count("router") != 1:
        raise ValueError("expected one Router and all five component roles")
    agents = {component(label)[1] for label in labels if component(label)[0]=="agent"}
    workers = {component(label)[1] for label in labels if component(label)[0]=="worker"}
    if agents != workers:
        raise ValueError("each Worker must have its named adapter")
    platforms = {component(label)[1] for label in labels if component(label)[0]=="feishu"}
    if not platforms <= workers:
        raise ValueError("each Feishu ledger must have its named Worker")


def health(db,role):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    if not TABLES[role] <= tables or list(x[0] for x in db.execute("PRAGMA integrity_check")) != ["ok"]:
        raise ValueError("wrong component schema or corrupt database: "+role)


def check_revision(revision):
    if not isinstance(revision,str) or not re.fullmatch("[a-f0-9]{40}",revision):
        raise ValueError("expected the full source revision")


def copy_private(src,dst):
    with src.open("rb") as reader, open(dst,"xb",opener=lambda p,f:os.open(p,f,0o600)) as writer:
        shutil.copyfileobj(reader,writer)
        writer.flush()
        os.fsync(writer.fileno())


@contextmanager
def locked(sources,registries=None,*,partial=False):
    validate_set(sources,partial=partial)
    with ExitStack() as stack:
        identities = set()
        def lock_database(path,role):
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise ValueError("expected absolute regular component database paths")
            stat = path.stat()
            identity = (stat.st_dev,stat.st_ino)
            if identity in identities:
                raise ValueError("two roles point to the same database")
            identities.add(identity)
            stack.enter_context(state_writer(path))
            if any(path.with_name(path.name+s).exists() for s in ("-wal","-journal","-shm")):
                raise ValueError("checkpoint and stop all isolated writers before snapshot")
            db = stack.enter_context(closing(sqlite3.connect(path.resolve().as_uri()+"?mode=rw",uri=True,timeout=0)))
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ValueError("expected closed rollback-journal databases")
            # Lock all selected files before copying the first. A single busy owner aborts staging.
            db.execute("BEGIN EXCLUSIVE")
            health(db,role)
        for label in sorted(sources):
            lock_database(Path(sources[label]),component(label)[0])
        yield managed.collect(sources,registries or {},stack,lock_database,partial=partial)


def agent_ledger_paths(directory,manifest):
    managed_native = manifest.get("managed_native",{})
    for label in manifest["files"]:
        role,wid = component(label)
        if role=="agent":
            group = managed_native.get(wid,{"children":{}})
            children = [directory/managed.registry_name(wid)/(sid+".sqlite3") for sid in group["children"]]
            yield wid,[directory/filename(label),*children]


def has_workspace_journal(directory,manifest):
    return any(dsh_workspace_audit.identities(paths) for _wid,paths in agent_ledger_paths(directory,manifest))


def workspace_audits(snapshot,manifest,dsh_runtimes,*,reservations=None):
    dsh_runtimes = dsh_runtimes or {}
    ledgers = dict(agent_ledger_paths(snapshot,manifest))
    if set(dsh_runtimes)-ledgers.keys():
        raise ValueError("DSH runtime does not belong to a snapshot Worker")
    audits = {}
    for wid,paths in ledgers.items():
        workspace,issue = dsh_workspace_audit.inspect(dsh_runtimes.get(wid),dsh_workspace_audit.identities(paths),
                                                     reservations=reservations)
        if workspace is not None and workspace["pending"]:
            issue = "dsh_assignment_workspace_pending"
        if workspace is not None or issue:
            audits[wid] = (workspace,issue)
    return audits


def host_metadata(batch_id,host_id):
    if not valid_guid(batch_id) or not isinstance(host_id,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",host_id):
        raise ValueError("expected a cutover batch UUID and an inventory host ID")
    return {"batch_id":batch_id,"host_id":host_id}


def capture(sources,revision,destination,*,registries=None,batch_id=None,host_id=None):
    check_revision(revision)
    if not isinstance(sources,dict):
        raise ValueError("expected a map of component database paths")
    part = host_metadata(batch_id,host_id) if batch_id is not None or host_id is not None else None
    manifest = {"format":1,"snapshot_id":guid(),"source_revision":revision,"files":{}}
    if part is not None:
        manifest["host_part"] = part
    with output_directory(destination) as stage, locked(sources,registries,partial=part is not None) as inventory:
        for label,source in sorted(sources.items()):
            path = Path(source)
            dest = stage/filename(label)
            copy_private(path,dest)
            checksum = digest(dest)
            if checksum != digest(path):
                raise ValueError("state file changed during backup")
            manifest["files"][label] = {"size":dest.stat().st_size,"sha256":checksum}
        if inventory:
            managed.copy_ledgers(registries,inventory,stage,copy_private)
            manifest.update(format=2,managed_native=inventory)
        if has_workspace_journal(stage,manifest):
            manifest["format"] = 3
        metadata = stage/"manifest.json"
        metadata.write_text(encode(manifest),encoding="utf-8")
        fingerprint = digest(metadata)
    result = {"snapshot_id":manifest["snapshot_id"],"source_revision":revision,"components":len(sources),
              "manifest_sha256":fingerprint}
    if part is not None:
        result["host_part"] = part
    return result


def verify(snapshot,expected,*,partial=False):
    manifest_file = snapshot/"manifest.json"
    if snapshot.is_symlink() or manifest_file.is_symlink() or not manifest_file.is_file():
        raise ValueError("snapshot manifest does not match the recorded digest")
    raw = manifest_file.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected:
        raise ValueError("snapshot manifest does not match the recorded digest")
    manifest = json.loads(raw)
    if not isinstance(manifest,dict):
        raise ValueError("unsupported snapshot")
    if partial != ("host_part" in manifest):
        raise ValueError("host fragments must be assembled before ordinary verification or restore")
    if partial:
        part = manifest["host_part"]
        if not isinstance(part,dict) or set(part)!={"batch_id","host_id"}:
            raise ValueError("invalid host fragment metadata")
        host_metadata(part["batch_id"],part["host_id"])
    version = manifest.get("format")
    if version not in (1,2,3) or not isinstance(manifest.get("files"),dict):
        raise ValueError("unsupported snapshot")
    check_revision(manifest["source_revision"])
    validate_set(manifest["files"],partial=partial)
    inventory = manifest.get("managed_native",{})
    if (not isinstance(inventory,dict) or version in (1,2) and bool(inventory)!=(version==2)
            or any("agent:"+wid not in manifest["files"] for wid in inventory)):
        raise ValueError("invalid managed snapshot members")
    names = {wid:managed.registry_name(wid) for wid in inventory}
    expected_files = {"manifest.json"} | {filename(label) for label in manifest["files"]} | set(names.values())
    if {p.name for p in snapshot.iterdir()} != expected_files:
        raise ValueError("missing or unexpected snapshot member")
    sources = {label:snapshot/filename(label) for label in manifest["files"]}
    for label,entry in manifest["files"].items():
        path = sources[label]
        with closing(readonly(path)) as db:
            if path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
                raise ValueError("snapshot bytes do not match: "+label)
            health(db,component(label)[0])
    directories = {wid:managed.private_registry((snapshot/name).absolute()) for wid,name in names.items()}
    managed.validate(sources,inventory,directories,health=health,partial=partial)
    if has_workspace_journal(snapshot,manifest)!=(version==3):
        raise ValueError("snapshot version does not match the DSH workspace registry")
    return manifest


def assemble_hosts(plan,destination):
    if (not isinstance(plan,dict) or set(plan)!={"batch_id","source_revision","hosts"}
            or not isinstance(plan["hosts"],dict) or not plan["hosts"]):
        raise ValueError("expected a batch, source revision and roster of host fragments")
    revision = plan["source_revision"]
    check_revision(revision)
    batch = plan["batch_id"]
    manifests,files,host_digests = {},{},{}
    host_names = set()
    locations,inventory = {},{}

    for host,settings in sorted(plan["hosts"].items()):
        identity = host_metadata(batch,host)
        if (not isinstance(settings,dict) or set(settings)!={"snapshot","manifest_sha256","components"}
                or not isinstance(settings["snapshot"],str) or not Path(settings["snapshot"]).is_absolute()
                or not isinstance(settings["components"],list)):
            raise ValueError("expected an absolute fragment path, verified manifest digest and component roster")
        validate_set(settings["components"],partial=True)
        path,digest_expected = Path(settings["snapshot"]),settings["manifest_sha256"]
        fragment = verify(path,digest_expected,partial=True)
        if fragment["host_part"]!=identity or fragment["source_revision"]!=revision:
            raise ValueError("host fragment belongs to a different batch, host or revision")
        roster = set(settings["components"])
        if roster!=set(fragment["files"]):
            raise ValueError("host fragment does not match its component roster: "+host)
        if roster & files.keys() or host.casefold() in host_names:
            raise ValueError("duplicate host or component in assembly")
        host_names.add(host.casefold())
        host_digests[host] = digest_expected
        manifests[path] = fragment
        files.update(fragment["files"])
        group = fragment.get("managed_native",{})
        locations.update({wid:path/managed.registry_name(wid) for wid in group})
        inventory.update(group)

    validate_set(files)
    manifest = {"format":2 if inventory else 1,"snapshot_id":guid(),"source_revision":revision,"files":files,
                "assembled_hosts":{"batch_id":batch,"manifest_sha256_by_host":host_digests}}
    if inventory:
        manifest["managed_native"] = inventory
    if any(fragment["format"]==3 for fragment in manifests.values()):
        manifest["format"] = 3
    with output_directory(destination) as stage:
        for path,fragment in manifests.items():
            for label in fragment["files"]:
                name = filename(label)
                copy_private(path/name,stage/name)
        managed.copy_ledgers(locations,inventory,stage,copy_private)
        metadata = stage/"manifest.json"
        metadata.write_text(encode(manifest),encoding="utf-8")
        fingerprint = digest(metadata)
        # Recheck bytes in the destination and compare both sides of any
        # managed enrollment before atomically publishing a complete snapshot.
        verify(stage,fingerprint)
    return {"snapshot_id":manifest["snapshot_id"],"source_revision":revision,"components":len(files),
            "manifest_sha256":fingerprint,"assembled_hosts":manifest["assembled_hosts"]}


def restore_files(snapshot,expected,manifest,stage,*,destination=None,managed_keys=None):
    for label,entry in manifest["files"].items():
        name = filename(label)
        copy_private(snapshot/name,stage/name)
        if digest(stage/name) != entry["sha256"]:
            raise ValueError("restored bytes do not match: "+label)
    inventory = manifest.get("managed_native",{})
    paths = {wid:snapshot/managed.registry_name(wid) for wid in inventory}
    managed.copy_ledgers(paths,inventory,stage,copy_private)
    if verify(snapshot,expected) != manifest:
        raise ValueError("snapshot changed during restore")
    managed.restore_enrollments(stage,destination,inventory,managed_keys or {},
                                lambda wid:stage/filename("agent:"+wid))


def restore(snapshot,expected,destination,*,managed_keys=None):
    manifest = verify(snapshot,expected)
    with output_directory(destination) as stage:
        restore_files(snapshot,expected,manifest,stage,destination=destination,managed_keys=managed_keys)
    return {label:str(destination/filename(label)) for label in manifest["files"]}


def pending_counts(db,checks):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    return {name:db.execute(query).fetchone()[0] if required<=tables else None
            for name,(required,query) in checks.items()}


def agent_pending(paths):
    combined = {}
    for path in paths:
        with closing(readonly(path)) as db:
            totals = pending_counts(db,AGENT_NATIVE_PENDING)
            excluded = pending_counts(db,{"adapter_deliveries":({"intents","assignment_withdrawals"},"""
                SELECT COUNT(*) FROM intents LEFT JOIN assignment_withdrawals USING(delivery_id)
                WHERE receipt IS NULL AND discard_seq IS NULL""")})
            totals.update({k:v for k,v in excluded.items() if v is not None})
            totals.update({k:v for k,v in pending_counts(db,OPTIONAL_AGENT_PENDING).items() if v is not None})
        for key,value in totals.items():
            previous = combined.get(key,0)
            combined[key] = None if value is None or previous is None else previous+value
    return combined


def feishu_display_backlog(db,platform_path):
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='display_events' AND type='table'").fetchone():
        return None,"platform_display_ledger_unconfirmed"
    db.execute("ATTACH DATABASE ? AS feishu",(platform_path.resolve().as_uri()+"?mode=ro&immutable=1",))
    slots = db.execute("SELECT slot,cursor FROM feishu.view_cursor LIMIT 2").fetchall()
    if len(slots)!=1 or slots[0]["slot"]!=1 or type(slots[0]["cursor"]) is not int or slots[0]["cursor"]<0:
        return None,"platform_display_cursor_unconfirmed"
    cursor = slots[0]["cursor"]
    # A cursor is meaningful only against the identical, contiguous Worker event prefix.
    matched = db.execute("""SELECT COUNT(*) FROM main.display_events e JOIN feishu.view_attempt v USING(seq)
        WHERE e.seq BETWEEN 1 AND ? AND v.event=e.payload AND substr(v.mid,1,3)='om_'""",(cursor,)).fetchone()[0]
    diverged = db.execute("""SELECT 1 FROM feishu.view_attempt v LEFT JOIN main.display_events e USING(seq)
        WHERE v.seq<1 OR (v.seq>? AND (v.seq>?+1 OR v.mid IS NOT NULL OR e.seq IS NULL OR v.event<>e.payload))
        LIMIT 1""",(cursor,cursor)).fetchone()
    unseen = db.execute("""SELECT COUNT(*) FROM main.display_events e LEFT JOIN feishu.view_attempt v USING(seq)
        WHERE e.seq>? AND v.seq IS NULL""",(cursor,)).fetchone()[0]
    return unseen,"platform_display_ledger_mismatch" if matched!=cursor or diverged else None


def hermes_delivery_audit(snapshot,labels,router_path,confirmed):
    managers = sorted(label for label in labels if component(label)[0]=="hermes")
    results = []
    for label in managers:
        with closing(readonly(snapshot/filename(label))) as db:
            rows = db.execute("SELECT cursor FROM progress LIMIT 2").fetchall()
            cursor = rows[0]["cursor"] if len(rows)==1 else None
            queued,pending = db.execute("SELECT COUNT(*),COUNT(*) FILTER (WHERE receipt IS NULL) FROM queue").fetchone()
            issues = []
            if type(cursor) is not int or cursor<0:
                issues.append("hermes_cursor_unconfirmed")
            elif cursor>confirmed:
                issues.append("hermes_cursor_ahead_of_router")

            db.execute("ATTACH DATABASE ? AS router",(router_path.resolve().as_uri()+"?mode=ro&immutable=1",))
            prefix,matched = db.execute("""SELECT
                COUNT(*) FILTER (WHERE d.cursor BETWEEN 1 AND ? AND d.ack=1),
                COUNT(DISTINCT q.message_id)
                FROM router.deliveries d JOIN main.queue q
                  ON q.message_id=json_extract(d.payload,'$.message_id') AND q.payload=d.payload
                WHERE d.consumer='hermes' AND d.kind='message.deliver'""",(cursor,)).fetchone()
            if not issues and prefix!=cursor:
                issues.append("hermes_confirmed_prefix_missing")
            if matched!=queued:
                issues.append("hermes_queue_router_mismatch")
            if len(managers)>1:
                issues.append("hermes_receiver_ambiguous")
        results.append({"instance":component(label)[1],"router_confirmed_cursor":confirmed,"local_cursor":cursor,
                        "queued_without_native_receipt":pending,"issues":issues})
    return results


def worker_delivery_audit(db,router_path,worker_id,confirmed):
    db.execute("ATTACH DATABASE ? AS router",(router_path.resolve().as_uri()+"?mode=ro&immutable=1",))
    position = db.execute("SELECT cursor FROM connection WHERE slot=1").fetchone()
    cursor = position[0] if position else None
    actor = "worker/"+worker_id
    prefix,matched = db.execute("""SELECT
        COUNT(*) FILTER (WHERE d.cursor BETWEEN 1 AND ? AND d.ack=1),
        COUNT(*)
        FROM router.deliveries d JOIN main.inbox i USING(delivery_id)
        WHERE d.consumer=? AND json_extract(i.frame,'$.delivery_id')=d.delivery_id
          AND json_extract(i.frame,'$.cursor')=d.cursor AND json_extract(i.frame,'$.kind')=d.kind
          AND json_extract(i.frame,'$.payload')=d.payload""",(confirmed,actor)).fetchone()
    inbox_count = db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
    issues = []
    if type(cursor) is not int or cursor<0 or cursor>confirmed:
        issues.append("worker_cursor_ahead_of_router")
    if prefix!=confirmed:
        issues.append("worker_confirmed_prefix_missing")
    if matched!=inbox_count:
        issues.append("worker_inbox_router_mismatch")
    # An accepted W03 can still be replayed after its assignment has ended.
    stale_dispatch = db.execute("""WITH pending_dispatches AS (
        SELECT json_extract(frame,'$.payload.task_id') AS task_id,
               json_extract(frame,'$.payload.assignment_id') AS assignment_id
        FROM main.inbox WHERE done=0 AND json_extract(frame,'$.kind')='task.dispatch'
    )
    SELECT 1 FROM pending_dispatches p
      JOIN router.operations o ON o.actor=? AND o.key='accept-'||p.assignment_id
      LEFT JOIN main.jobs j ON j.assignment_id=p.assignment_id
      LEFT JOIN main.controls c ON c.assignment_id=p.assignment_id
    WHERE json_extract(o.response,'$.accepted')=1
      AND json_extract(o.response,'$.assignment_id')=p.assignment_id
      AND COALESCE(j.stage,'offered')='offered' AND c.assignment_id IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM router.tasks t WHERE t.task_id=p.task_id AND t.assignment_id=p.assignment_id
          AND t.assignee_id=? AND t.state='working'
      )
    LIMIT 1""",(actor,worker_id)).fetchone()
    if stale_dispatch:
        issues.append("worker_stale_dispatch_receipt")
    return {"router_confirmed_cursor":confirmed,"local_cursor":cursor,"matched_frames":matched},issues


def audit_trigger(snapshot,expected):
    manifest = verify(snapshot,expected)
    router, = (label for label in manifest["files"] if component(label)[0]=="router")
    triggers = {label:snapshot/filename(label) for label in manifest["files"] if component(label)[0]=="trigger"}
    result = trigger_audit.reconcile(snapshot/filename(router),triggers)
    if verify(snapshot,expected)!=manifest:
        raise ValueError("snapshot changed during Trigger audit")
    return {"snapshot_id":manifest["snapshot_id"],"source_revision":manifest["source_revision"],**result}


def audit_handoff(snapshot,expected,*,dsh_runtimes=None):
    manifest = verify(snapshot,expected)
    labels = manifest["files"]
    workspaces = workspace_audits(snapshot,manifest,dsh_runtimes)
    router_label, = (label for label in labels if component(label)[0]=="router")
    router_path = snapshot/filename(router_label)
    worker_ids = sorted(component(label)[1] for label in labels if component(label)[0]=="worker")
    with closing(readonly(router_path)) as db:
        hermes_confirmed = delivery.confirmed(db,"hermes")
        worker_confirmed = {wid:delivery.confirmed(db,"worker/"+wid) for wid in worker_ids}
        current = {row["assignee_id"]:dict(row) for row in db.execute("""SELECT assignee_id,task_id,assignment_id
                                                                    FROM tasks WHERE state='working' ORDER BY assignee_id""")}
        sessions = dict(db.execute("SELECT worker_id,session_id FROM workers"))
        reserved = dict(db.execute("""SELECT assignee_id,COUNT(*) FROM tasks WHERE state='ready'
                                    AND assignment_id IS NOT NULL GROUP BY assignee_id"""))
        cleanup = [dict(row) for row in db.execute("""SELECT worker_id,task_id,assignment_id,kind FROM cleanups
                                                    WHERE done=0 ORDER BY worker_id,assignment_id""")]
        frames = dict(db.execute("""SELECT consumer,COUNT(*) FROM deliveries
            WHERE ack=0 AND kind<>'worker.status_query' GROUP BY consumer"""))
        has_questions = db.execute("SELECT 1 FROM sqlite_schema WHERE name='questions' AND type='table'").fetchone()
        questions = dict(db.execute("""SELECT asked_by,COUNT(*) FROM questions
            WHERE answer IS NULL GROUP BY asked_by""")) if has_questions else None
        has_approvals = db.execute("SELECT 1 FROM sqlite_schema WHERE name='native_approvals' AND type='table'").fetchone()
        open_approvals = dict(db.execute("""SELECT worker_id,COUNT(*) FROM native_approvals WHERE closed_at IS NULL
                                         GROUP BY worker_id""")) if has_approvals else None
        has_handoffs = db.execute("SELECT 1 FROM sqlite_schema WHERE name='admission_handoffs' AND type='table'").fetchone()
        handoffs = dict(db.execute("""SELECT worker_id,COUNT(*) FROM admission_handoffs
            WHERE released_at IS NULL GROUP BY worker_id""")) if has_handoffs else {}
    workers,issues = [],[]
    for wid in worker_ids:
        remote = current.pop(wid,None)
        platform_label = "feishu:"+wid
        platform_issue = None
        platform_proof = None
        with closing(readonly(snapshot/filename("worker:"+wid))) as db:
            local = db.execute("""SELECT task_id,assignment_id,delivery_id,host_state FROM jobs
                                  WHERE stage='accepted'""").fetchone()
            sid = db.execute("SELECT session FROM connection WHERE slot=1").fetchone()
            pending = db.execute("SELECT COUNT(*) FROM outbox WHERE receipt IS NULL").fetchone()[0]
            session_pending = pending_counts(db,WORKER_NATIVE_PENDING)
            session_pending.update({k:v for k,v in pending_counts(db,WORKER_ADMISSION_PENDING).items() if v is not None})
            transport,problems = worker_delivery_audit(db,router_path,wid,worker_confirmed[wid])
            if platform_label in labels:
                unseen,platform_issue = feishu_display_backlog(db,snapshot/filename(platform_label))
                session_pending["platform_display_unseen"] = unseen
                if platform_issue is None:
                    platform_proof = feishu_audit.reconcile(db)
            bound = None
            if local and db.execute("SELECT 1 FROM sqlite_schema WHERE name='assignment_sessions'").fetchone():
                bound = db.execute("SELECT session FROM assignment_sessions WHERE assignment_id=?",
                                   (local["assignment_id"],)).fetchone()
        local_ids = {key:local[key] for key in ("task_id","assignment_id")} if local else None
        remote_ids = {key:remote[key] for key in ("task_id","assignment_id")} if remote else None
        if platform_issue:
            problems.append(platform_issue)
        if platform_proof is not None:
            problems.extend(sorted({row["code"] for row in platform_proof["issues"]}))
        if local_ids!=remote_ids:
            problems.append("assignment_mismatch")
        if (wid in sessions and (not sid or sid[0]!=sessions[wid])) or (local and wid not in sessions):
            problems.append("worker_session_mismatch")
        if pending:
            problems.append("report_receipt_pending")
        root = snapshot/filename("agent:"+wid)
        group = manifest.get("managed_native",{}).get(wid,{"children":{}})
        child_ledgers = {sid:snapshot/managed.registry_name(wid)/(sid+".sqlite3") for sid in group["children"]}
        bound_session = bound[0] if bound else None
        if local and not adapter_has_input(child_ledgers.get(bound_session,root),local,bound_session):
            problems.append("adapter_receipt_unconfirmed")
        agent_paths = [root,*child_ledgers.values()]
        session_pending.update(agent_pending(agent_paths))
        workspace,workspace_issue = workspaces.get(wid,(None,None))
        if workspace_issue:
            problems.append(workspace_issue)
        if wid in workspaces:
            session_pending["dsh_assignment_workspaces"] = len(workspace["pending"]) if workspace is not None else None
        if platform_label in labels:
            with closing(readonly(snapshot/filename(platform_label))) as db:
                session_pending.update(pending_counts(db,FEISHU_NATIVE_PENDING))
                session_pending.update({k:v for k,v in pending_counts(db,FEISHU_OPTIONAL_PENDING).items()
                                        if v is not None})
        session_pending["report_receipts"] = pending
        session_pending["held_assignments"] = int(bool(local or remote))
        session_pending["router_reserved_assignments"] = reserved.get(wid,0)
        session_pending["router_cleanup"] = sum(row["worker_id"]==wid for row in cleanup)
        session_pending["router_approvals"] = open_approvals.get(wid,0) if open_approvals is not None else None
        session_pending["router_waiting_deliveries"] = frames.get("worker/"+wid,0)
        session_pending["router_waiting_questions"] = questions.get(wid,0) if questions is not None else None
        session_pending["router_registration_unconfirmed"] = int(wid not in sessions)
        session_pending["router_admission_handoffs"] = handoffs.get(wid,0)
        workers.append({"worker_id":wid,"worker_assignment":local_ids,"router_assignment":remote_ids,
                        "unconfirmed_reports":pending,"issues":problems,"native_session_pending":session_pending,
                        "inbox_transport":transport,
                        "native_session_ledger_clear":not problems and all(count==0 for count in session_pending.values()),
                        **({"feishu_receipts":platform_proof} if platform_proof is not None else {}),
                        **({"dsh_workspace_audit":workspace} if workspace is not None else {})})
        issues.extend({"worker_id":wid,"issue":problem} for problem in problems)
    issues.extend({"worker_id":wid,"issue":"active_worker_state_missing","task_id":row["task_id"],
                   "assignment_id":row["assignment_id"]} for wid,row in current.items())
    hermes = hermes_delivery_audit(snapshot,labels,router_path,hermes_confirmed)
    issues.extend({"hermes_instance":row["instance"],"issue":issue} for row in hermes for issue in row["issues"])
    if verify(snapshot,expected)!=manifest:
        raise ValueError("snapshot changed during handoff audit")
    return {"snapshot_id":manifest["snapshot_id"],"workers":workers,"hermes":hermes,"pending_cleanup":cleanup,
            "issues":issues,"needs_review":bool(issues or cleanup)}


def adapter_has_input(path,job,session):
    if job["host_state"]!="received" or not session:
        return False
    with closing(readonly(path)) as db:
        row = db.execute("SELECT payload,digest,native_id,receipt FROM intents WHERE delivery_id=? AND session=?",
                         (job["delivery_id"],session)).fetchone()
        if not row or not row["receipt"] or not row["native_id"]:
            return False
    try:
        proof,payload = json.loads(row["receipt"]),json.loads(row["payload"])
    except (ValueError,TypeError):
        return False
    return (isinstance(proof,dict) and isinstance(payload,dict)
            and proof.get("delivery_id")==job["delivery_id"] and proof.get("session")==session
            and proof.get("digest")==row["digest"] and proof.get("native_id")==row["native_id"]
            and row["digest"]==hashlib.sha256(row["payload"].encode()).hexdigest() and bool(proof.get("turn_id"))
            and payload.get("kind")=="task" and payload.get("task_id")==job["task_id"]
            and payload.get("assignment_id")==job["assignment_id"])


def stage_native_handoff(snapshot,expected,worker_id,source_session,target_session,destination,*,
                         managed_keys=None,dsh_runtimes=None):
    worker_label,adapter_label = "worker:"+worker_id,"agent:"+worker_id
    component(worker_label)
    if source_session==target_session or not all(
            re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}",session) for session in (source_session,target_session)):
        raise ValueError("expected two distinct native session identifiers")
    manifest = verify(snapshot,expected)
    if worker_id in manifest.get("managed_native",{}):
        raise ValueError("restore managed sessions first and use the Worker native handoff")
    if worker_label not in manifest["files"]:
        raise ValueError("worker absent from snapshot")
    audit = audit_handoff(snapshot,expected,dsh_runtimes=dsh_runtimes)
    worker, = (row for row in audit["workers"] if row["worker_id"]==worker_id)
    if not worker["native_session_ledger_clear"]:
        pending = [key for key,count in worker["native_session_pending"].items() if count!=0]
        raise ValueError("native handoff still pending: "+", ".join(pending+worker["issues"]))
    for label,table in ((worker_label,"native_binding"),(adapter_label,"adapter_native_binding")):
        with closing(readonly(snapshot/filename(label))) as db:
            exists = db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",(table,)).fetchone()
            pin = db.execute(f"SELECT session FROM {table} WHERE slot=1").fetchone() if exists else None
            if not pin or pin[0]!=source_session:
                raise ValueError("original native binding unconfirmed: "+label)
    with ExitStack() as reservations, output_directory(destination) as stage:
        restore_files(snapshot,expected,manifest,stage,destination=destination,managed_keys=managed_keys)
        worker_db = stage/filename(worker_label)
        with closing(sqlite3.connect(worker_db,timeout=0)) as db, db:
            updated = db.execute("UPDATE native_binding SET session=? WHERE slot=1 AND session=?",
                                 (target_session,source_session)).rowcount
            if updated!=1:
                raise ValueError("original native binding changed")
        result = {"snapshot_id":manifest["snapshot_id"],"worker_id":worker_id,"previous_session":source_session,
                  "expected_session":target_session,"worker_db":str(destination/worker_db.name),
                  "previous_adapter_db":str(destination/filename(adapter_label)),
                  "target_adapter_db":str(destination/("target-"+filename(adapter_label)))}
        if workspace := worker.get("dsh_workspace_audit"):
            identity = dsh_workspace_audit.identities([snapshot/filename(adapter_label)])
            if dsh_workspace_audit.inspect(dsh_runtimes[worker_id],identity,reservations=reservations)!=(workspace,None):
                raise ValueError("DSH workspace journal changed during handoff")
            result["dsh_workspace_journal_sha256"] = workspace["fingerprint"]
        (stage/"native-handoff.json").write_text(encode(result),encoding="utf-8")
    return result


def rollback(snapshot,expected,trial,destination,*,registries=None,managed_keys=None,dsh_runtimes=None):
    manifest = verify(snapshot,expected)
    if set(trial) != set(manifest["files"]):
        raise ValueError("trial component list differs from snapshot")
    with locked(trial,registries) as inventory, TemporaryDirectory(prefix="mowulink-rollback-check-") as tmp:
        if inventory!=manifest.get("managed_native",{}):
            raise ValueError("trial state changed; reconcile managed native sessions before rollback")
        for label,path in trial.items():
            expected_digest = manifest["files"][label]["sha256"]
            wid = label.removeprefix("agent:")
            if label.startswith("agent:") and wid in inventory:
                staged = Path(tmp)/filename(label)
                copy_private(snapshot/filename(label),staged)
                managed.relocate_root(staged,Path(registries[wid]).resolve())
                expected_digest = digest(staged)
            if digest(Path(path)) != expected_digest:
                raise ValueError("trial state changed; reconcile before rollback: "+label)
        workspaces = workspace_audits(snapshot,manifest,dsh_runtimes)
        for wid,(_workspace,issue) in workspaces.items():
            if issue:
                raise ValueError(f"version rollback still pending for {wid}: {issue}")
        with ExitStack() as reservations, output_directory(destination) as stage:
            restore_files(snapshot,expected,manifest,stage,destination=destination,managed_keys=managed_keys)
            if workspace_audits(snapshot,manifest,dsh_runtimes,reservations=reservations)!=workspaces:
                raise ValueError("DSH workspace journal changed during rollback")
    return {label:str(destination/filename(label)) for label in manifest["files"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    ops = parser.add_subparsers(dest="op",required=True)
    for op in ("capture","capture-part"):
        backup = ops.add_parser(op)
        backup.add_argument("--sources",type=Path,required=True,help="role:instance to absolute database path, JSON")
        backup.add_argument("--managed-registries",type=Path,help="Worker ID to private native registry directory, JSON")
        backup.add_argument("--revision",required=True)
        backup.add_argument("--destination",type=Path,required=True)
        if op=="capture-part":
            backup.add_argument("--batch-id",required=True,help="shared UUID for this stopped cutover window")
            backup.add_argument("--host-id",required=True)
    assembly = ops.add_parser("assemble")
    assembly.add_argument("--plan",type=Path,required=True,help="the expected host and component roster with recorded digests")
    assembly.add_argument("--destination",type=Path,required=True)
    for op in ("verify","restore","rollback","audit-trigger","audit-handoff","stage-native-handoff"):
        p = ops.add_parser(op)
        p.add_argument("--snapshot",type=Path,required=True)
        p.add_argument("--manifest-sha256",required=True)
        if op=="rollback":
            p.add_argument("--trial-sources",type=Path,required=True)
            p.add_argument("--managed-registries",type=Path,help="Worker ID to the trial's native registry directory, JSON")
        if op in ("restore","rollback","stage-native-handoff"):
            p.add_argument("--managed-keys",type=Path,help="Worker ID to the original private callback key file, JSON")
        if op in ("audit-handoff","stage-native-handoff","rollback"):
            p.add_argument("--dsh-runtimes",type=Path,help="Worker ID to original stopped DSH runtime, JSON")
        if op=="stage-native-handoff":
            for field in ("worker-id","source-session","target-session"):
                p.add_argument("--"+field,required=True)
        if op in ("restore","rollback","stage-native-handoff"):
            p.add_argument("--destination",type=Path,required=True)
    args = parser.parse_args()
    def optional_map(flag):
        path = getattr(args,flag,None)
        data = json.loads(path.read_text()) if path is not None else {}
        if not isinstance(data,dict) or not all(isinstance(value,str) and Path(value).is_absolute() for value in data.values()):
            parser.error("expected a JSON map to absolute paths")
        return data

    if args.op in ("capture","capture-part"):
        result = capture(json.loads(args.sources.read_text()),args.revision,args.destination,
                         registries=optional_map("managed_registries"),batch_id=getattr(args,"batch_id",None),
                         host_id=getattr(args,"host_id",None))
    elif args.op=="assemble":
        result = assemble_hosts(json.loads(args.plan.read_text()),args.destination)
    elif args.op=="verify":
        manifest = verify(args.snapshot,args.manifest_sha256)
        result = {"snapshot_id":manifest["snapshot_id"],"components":len(manifest["files"])}
    elif args.op=="audit-trigger":
        result = audit_trigger(args.snapshot,args.manifest_sha256)
    elif args.op=="audit-handoff":
        result = audit_handoff(args.snapshot,args.manifest_sha256,dsh_runtimes=optional_map("dsh_runtimes"))
    elif args.op=="stage-native-handoff":
        result = stage_native_handoff(args.snapshot,args.manifest_sha256,args.worker_id,
                                      args.source_session,args.target_session,args.destination,
                                      managed_keys=optional_map("managed_keys"),dsh_runtimes=optional_map("dsh_runtimes"))
    elif args.op=="rollback":
        result = rollback(args.snapshot,args.manifest_sha256,json.loads(args.trial_sources.read_text()),args.destination,
                          registries=optional_map("managed_registries"),managed_keys=optional_map("managed_keys"),
                          dsh_runtimes=optional_map("dsh_runtimes"))
    else:
        result = restore(args.snapshot,args.manifest_sha256,args.destination,managed_keys=optional_map("managed_keys"))
    print(encode(result))
    if args.op=="audit-trigger" and not result["ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
