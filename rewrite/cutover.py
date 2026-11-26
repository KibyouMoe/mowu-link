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
from . import legacy_history, legacy_peers
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


def prepare(snapshot, destination, source_id):
    namespace = UUID(source_id)
    manifest = verified(snapshot)
    with closing(readonly(snapshot/"legacy.db")) as old:
        blockers = {key:n for key,n in inventory(old).items() if n}
    if blockers:
        raise ValueError("legacy work requires reconciliation: " + encode(blockers))
    with output_directory(destination) as stage, closing(readonly(snapshot/"legacy.db")) as old:
        router = Orders(stage/"router.db")
        mapping = {}
        with router.db.transaction() as db:
            for row in old.execute("""SELECT q.task_id,i.name,i.description,i.metadata,i.request_id,i.actor,q.worker FROM dispatch_queue q
                                      JOIN task_info i USING(task_id) ORDER BY q.seq"""):
                tid = str(uuid5(namespace,row["task_id"]))
                body = row["description"]
                meta = json.loads(row["metadata"])
                context = (meta.get("context") or "").strip()
                if context:
                    body += "\n\n[router context]\n"+context
                instructions = LEGACY_GOAL+"\n\n"+body if meta else body
                db.execute("INSERT INTO tasks(task_id,title,instructions,assignee_id) VALUES (?,?,?,?)",
                           (tid,row["name"],instructions,row["worker"]))
                if row["actor"]=="hermes" and row["request_id"]:
                    original = {"title":row["name"],"instructions":body,"assignee_id":row["worker"]}
                    db.execute("INSERT INTO operations VALUES ('hermes',?,?,?)",
                               (row["request_id"],encode([["POST","/v1/tasks"],original]),encode(task(db,tid))))
                mapping[row["task_id"]] = tid
            legacy_history.populate(db,old,str(namespace),manifest["legacy_db_sha256"],mapping)
            legacy_peers.populate(db,old,str(namespace),snapshot/"attachments")
        baseline = {"snapshot_sha256":manifest["legacy_db_sha256"],"source_id":str(namespace),
                    "router_db_sha256":digest(stage/"router.db"),"queued_id_map":mapping}
        (stage/"baseline.json").write_text(encode(baseline))
    return baseline


def rollback(snapshot, trial, destination):
    manifest = verified(snapshot)
    baseline = json.loads((trial/"baseline.json").read_text())
    with closing(readonly(trial/"router.db")):
        if baseline["snapshot_sha256"] != manifest["legacy_db_sha256"]:
            raise ValueError("snapshot does not belong to this rehearsal")
        if digest(trial/"router.db") != baseline["router_db_sha256"]:
            raise ValueError("new Router changed; reconcile new writes before rollback")
    with output_directory(destination) as stage:
        shutil.copyfile(snapshot/"legacy.db",stage/"legacy.db")
        shutil.copytree(snapshot/"attachments",stage/"attachments")
        if digest(stage/"legacy.db") != manifest["legacy_db_sha256"] or attachment_index(stage/"attachments") != manifest["attachments"]:
            raise ValueError("restored bytes failed verification")
    return {"restored_db_sha256":manifest["legacy_db_sha256"],"attachments":len(manifest["attachments"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="op", required=True)
    for op, names in (("backup",("database","attachments","destination")),
                      ("verify",("snapshot",)),("prepare",("snapshot","destination")),
                      ("rollback",("snapshot","trial","destination"))):
        command = commands.add_parser(op)
        for name in names:
            command.add_argument("--"+name,type=Path,required=True)
        if op == "prepare":
            command.add_argument("--source-id",required=True)
    args = vars(parser.parse_args())
    command = {"backup":backup,"verify":verified,"prepare":prepare,"rollback":rollback}[args.pop("op")]
    print(encode(command(**args)))


if __name__ == "__main__":
    main()
