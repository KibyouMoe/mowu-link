"""Offline component state snapshots. Keep revision and manifest digest outside the snapshot."""
import argparse
from contextlib import ExitStack, closing, contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

from .cutover import digest, output_directory, readonly
from .storage import encode, guid

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


def component(label):
    role,sep,name = label.partition(":")
    if not sep or role not in TABLES or not re.fullmatch("[a-z0-9][a-z0-9_-]{0,63}",name):
        raise ValueError("invalid role or instance label")
    return role,name


def filename(label):
    role,name = component(label)
    return role+"--"+name+".sqlite3"


def validate_set(labels):
    roles = [component(label)[0] for label in labels]
    if not REQUIRED <= set(roles) or roles.count("router") != 1:
        raise ValueError("expected one Router and all five component roles")
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
    for op in ("verify","restore","rollback"):
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
    elif args.op=="rollback":
        result = rollback(args.snapshot,args.manifest_sha256,json.loads(args.trial_sources.read_text()),args.destination)
    else:
        result = restore(args.snapshot,args.manifest_sha256,args.destination)
    print(encode(result))


if __name__ == "__main__":
    main()
