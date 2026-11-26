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

from .cutover import digest, output_directory, readonly
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
}
AGENT_NATIVE_PENDING = {
    "adapter_deliveries": ({"intents"},"SELECT COUNT(*) FROM intents WHERE receipt IS NULL"),
    "adapter_questions": ({"question_resolutions"},"SELECT COUNT(*) FROM question_resolutions WHERE proof IS NULL"),
}


def component(label):
    role,sep,name = label.partition(":")
    if not sep or role not in TABLES or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{0,63}",name):
        raise ValueError("invalid role or instance label")
    return role,name


def filename(label):
    role,name = component(label)
    return role+"--"+name+".sqlite3"


def validate_set(labels):
    labels = list(labels)
    roles = [component(label)[0] for label in labels]
    if not REQUIRED <= set(roles) or roles.count("router") != 1:
        raise ValueError("expected one Router and all five component roles")
    if len({filename(label).casefold() for label in labels})!=len(labels):
        raise ValueError("component filenames must also be distinct on case-insensitive filesystems")
    agents = {component(label)[1] for label in labels if component(label)[0]=="agent"}
    workers = {component(label)[1] for label in labels if component(label)[0]=="worker"}
    if agents != workers:
        raise ValueError("each Worker must have its named adapter")


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
def locked(sources):
    validate_set(sources)
    with ExitStack() as stack:
        identities = set()
        for label in sorted(sources):
            path = Path(sources[label])
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
            health(db,component(label)[0])
        yield


def capture(sources,revision,destination):
    check_revision(revision)
    manifest = {"format":1,"snapshot_id":guid(),"source_revision":revision,"files":{}}
    with output_directory(destination) as stage, locked(sources):
        for label,source in sorted(sources.items()):
            path = Path(source)
            dest = stage/filename(label)
            copy_private(path,dest)
            checksum = digest(dest)
            if checksum != digest(path):
                raise ValueError("state file changed during backup")
            manifest["files"][label] = {"size":dest.stat().st_size,"sha256":checksum}
        metadata = stage/"manifest.json"
        metadata.write_text(encode(manifest),encoding="utf-8")
        fingerprint = digest(metadata)
    return {"snapshot_id":manifest["snapshot_id"],"source_revision":revision,"components":len(sources),
            "manifest_sha256":fingerprint}


def verify(snapshot,expected):
    manifest_file = snapshot/"manifest.json"
    if (snapshot.is_symlink() or manifest_file.is_symlink() or not manifest_file.is_file()
            or digest(manifest_file) != expected):
        raise ValueError("snapshot manifest does not match the recorded digest")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"),dict):
        raise ValueError("unsupported snapshot")
    check_revision(manifest["source_revision"])
    validate_set(manifest["files"])
    expected_files = {"manifest.json"} | {filename(label) for label in manifest["files"]}
    if {p.name for p in snapshot.iterdir()} != expected_files:
        raise ValueError("missing or unexpected snapshot member")
    for label,entry in manifest["files"].items():
        path = snapshot/filename(label)
        with closing(readonly(path)) as db:
            if path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
                raise ValueError("snapshot bytes do not match: "+label)
            health(db,component(label)[0])
    return manifest


def restore(snapshot,expected,destination):
    manifest = verify(snapshot,expected)
    with output_directory(destination) as stage:
        for label,entry in manifest["files"].items():
            name = filename(label)
            copy_private(snapshot/name,stage/name)
            if digest(stage/name) != entry["sha256"]:
                raise ValueError("restored bytes do not match: "+label)
        if verify(snapshot,expected) != manifest:
            raise ValueError("snapshot changed during restore")
    return {label:str(destination/filename(label)) for label in manifest["files"]}


def pending_counts(db,checks):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    return {name:db.execute(query).fetchone()[0] if required<=tables else None
            for name,(required,query) in checks.items()}


def audit_handoff(snapshot,expected):
    manifest = verify(snapshot,expected)
    labels = manifest["files"]
    router_label, = (label for label in labels if component(label)[0]=="router")
    with closing(readonly(snapshot/filename(router_label))) as db:
        current = {row["assignee_id"]:dict(row) for row in db.execute("""SELECT assignee_id,task_id,assignment_id
                                                                    FROM tasks WHERE state='working' ORDER BY assignee_id""")}
        sessions = dict(db.execute("SELECT worker_id,session_id FROM workers"))
        reserved = dict(db.execute("""SELECT assignee_id,COUNT(*) FROM tasks WHERE state='ready'
                                    AND assignment_id IS NOT NULL GROUP BY assignee_id"""))
        cleanup = [dict(row) for row in db.execute("""SELECT worker_id,task_id,assignment_id,kind FROM cleanups
                                                    WHERE done=0 ORDER BY worker_id,assignment_id""")]
    workers,issues = [],[]
    for label in sorted(labels):
        role,wid = component(label)
        if role!="worker":
            continue
        remote = current.pop(wid,None)
        with closing(readonly(snapshot/filename(label))) as db:
            local = db.execute("""SELECT task_id,assignment_id,delivery_id,host_state FROM jobs
                                  WHERE stage='accepted'""").fetchone()
            sid = db.execute("SELECT session FROM connection WHERE slot=1").fetchone()
            pending = db.execute("SELECT COUNT(*) FROM outbox WHERE receipt IS NULL").fetchone()[0]
            session_pending = pending_counts(db,WORKER_NATIVE_PENDING)
            bound = None
            if local and db.execute("SELECT 1 FROM sqlite_schema WHERE name='assignment_sessions'").fetchone():
                bound = db.execute("SELECT session FROM assignment_sessions WHERE assignment_id=?",
                                   (local["assignment_id"],)).fetchone()
        local_ids = {key:local[key] for key in ("task_id","assignment_id")} if local else None
        remote_ids = {key:remote[key] for key in ("task_id","assignment_id")} if remote else None
        problems = []
        if local_ids!=remote_ids:
            problems.append("assignment_mismatch")
        if (wid in sessions and (not sid or sid[0]!=sessions[wid])) or (local and wid not in sessions):
            problems.append("worker_session_mismatch")
        if pending:
            problems.append("report_receipt_pending")
        if local and not adapter_has_input(snapshot,wid,local,bound[0] if bound else None):
            problems.append("adapter_receipt_unconfirmed")
        with closing(readonly(snapshot/filename("agent:"+wid))) as db:
            session_pending.update(pending_counts(db,AGENT_NATIVE_PENDING))
        session_pending["report_receipts"] = pending
        session_pending["held_assignments"] = int(bool(local or remote))
        session_pending["router_reserved_assignments"] = reserved.get(wid,0)
        session_pending["router_cleanup"] = sum(row["worker_id"]==wid for row in cleanup)
        workers.append({"worker_id":wid,"worker_assignment":local_ids,"router_assignment":remote_ids,
                        "unconfirmed_reports":pending,"issues":problems,"native_session_pending":session_pending,
                        "native_session_ledger_clear":not problems and all(count==0 for count in session_pending.values())})
        issues.extend({"worker_id":wid,"issue":problem} for problem in problems)
    issues.extend({"worker_id":wid,"issue":"active_worker_state_missing","task_id":row["task_id"],
                   "assignment_id":row["assignment_id"]} for wid,row in current.items())
    if verify(snapshot,expected)!=manifest:
        raise ValueError("snapshot changed during handoff audit")
    return {"snapshot_id":manifest["snapshot_id"],"workers":workers,"pending_cleanup":cleanup,
            "issues":issues,"needs_review":bool(issues or cleanup)}


def adapter_has_input(snapshot,wid,job,session):
    if job["host_state"]!="received" or not session:
        return False
    with closing(readonly(snapshot/filename("agent:"+wid))) as db:
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


def rollback(snapshot,expected,trial,destination):
    manifest = verify(snapshot,expected)
    if set(trial) != set(manifest["files"]):
        raise ValueError("trial component list differs from snapshot")
    with locked(trial):
        for label,path in trial.items():
            if digest(Path(path)) != manifest["files"][label]["sha256"]:
                raise ValueError("trial state changed; reconcile before rollback: "+label)
        return restore(snapshot,expected,destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    ops = parser.add_subparsers(dest="op",required=True)
    backup = ops.add_parser("capture")
    backup.add_argument("--sources",type=Path,required=True,help="role:instance to absolute database path, JSON")
    backup.add_argument("--revision",required=True)
    backup.add_argument("--destination",type=Path,required=True)
    for op in ("verify","restore","rollback","audit-handoff"):
        p = ops.add_parser(op)
        p.add_argument("--snapshot",type=Path,required=True)
        p.add_argument("--manifest-sha256",required=True)
        if op=="rollback":
            p.add_argument("--trial-sources",type=Path,required=True)
        if op in ("restore","rollback"):
            p.add_argument("--destination",type=Path,required=True)
    args = parser.parse_args()
    if args.op=="capture":
        result = capture(json.loads(args.sources.read_text()),args.revision,args.destination)
    elif args.op=="verify":
        manifest = verify(args.snapshot,args.manifest_sha256)
        result = {"snapshot_id":manifest["snapshot_id"],"components":len(manifest["files"])}
    elif args.op=="audit-handoff":
        result = audit_handoff(args.snapshot,args.manifest_sha256)
    elif args.op=="rollback":
        result = rollback(args.snapshot,args.manifest_sha256,json.loads(args.trial_sources.read_text()),args.destination)
    else:
        result = restore(args.snapshot,args.manifest_sha256,args.destination)
    print(encode(result))


if __name__ == "__main__":
    main()
