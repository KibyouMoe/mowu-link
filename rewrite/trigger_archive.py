"""Preserve an offline legacy Trigger archive and its receipt ledger."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re

from .cutover import digest, output_directory, readonly
from .state_bundle import copy_private
from .storage import encode, guid


DATABASE = "legacy-trigger.sqlite3"
OPTIONAL = {"blocked.jsonl", "drops.jsonl"}
TABLES = {"events", "cursors", "receipts", "subscriptions", "rules"}


def inventory(path):
    with closing(readonly(path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        fields = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        if not TABLES <= tables or not {"event_id","level","disposition","payload_json"} <= fields:
            raise ValueError("expected an offline legacy Trigger 0.7 event archive")
        if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("legacy Trigger integrity check failed")
        events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        levels = dict(db.execute("SELECT COALESCE(level,'info'), COUNT(*) FROM events GROUP BY 1"))
        dispositions = dict(db.execute("SELECT COALESCE(disposition,'delivered'), COUNT(*) FROM events GROUP BY 1"))
        subscriptions = dict(db.execute("SELECT status,COUNT(*) FROM subscriptions GROUP BY 1"))
        unfinished = db.execute("""SELECT COUNT(*) FROM subscriptions s
            LEFT JOIN cursors c ON c.subscriber=s.name WHERE s.status='active' AND EXISTS (
              SELECT 1 FROM events e WHERE e.id>COALESCE(c.last_id,0)
                AND COALESCE(e.level,'info') IN ('event','notify','info')
                AND COALESCE(e.disposition,'delivered')!='dropped'
                AND NOT EXISTS (SELECT 1 FROM receipts r WHERE r.event_id=e.event_id AND r.subscriber=s.name)
            )""").fetchone()[0]
        counts = {name:db.execute("SELECT COUNT(*) FROM "+name).fetchone()[0]
                  for name in ("cursors","receipts","rules")}
    return {"events":events,"levels":levels,"dispositions":dispositions,"subscriptions":subscriptions,
            "active_with_unacknowledged_visible_events":unfinished,**counts}


def fingerprint(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("offline archive inputs must be regular files")
    return {"bytes":path.stat().st_size,"sha256":digest(path)}


def capture(database,destination,*,blocked=None,drops=None):
    sources = {DATABASE:database}
    if blocked is not None:
        sources["blocked.jsonl"] = blocked
    if drops is not None:
        sources["drops.jsonl"] = drops
    initial = {name:fingerprint(path) for name,path in sources.items()}
    records = inventory(database)
    manifest = {"format":1,"archive_id":guid(),"members":initial,"inventory":records}
    with output_directory(destination) as stage:
        for name,source in sources.items():
            copy_private(source,stage/name)
            if fingerprint(stage/name)!=initial[name] or fingerprint(source)!=initial[name]:
                raise ValueError("source changed while copying: "+name)
        if inventory(stage/DATABASE)!=records:
            raise ValueError("copied Trigger inventory differs")
        # An opened WAL source is rejected even if its main database file did not change.
        with closing(readonly(database)):
            if fingerprint(database)!=initial[DATABASE]:
                raise ValueError("source changed during inventory")
        index = stage/"manifest.json"
        index.write_text(encode(manifest),encoding="utf-8")
        checksum = digest(index)
    return {"archive_id":manifest["archive_id"],"manifest_sha256":checksum,"inventory":records}


def verify(snapshot,expected):
    index = snapshot/"manifest.json"
    if (snapshot.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}",expected)
            or fingerprint(index)["sha256"]!=expected):
        raise ValueError("archive manifest differs from the recorded digest")
    manifest = json.loads(index.read_text(encoding="utf-8"))
    names = set(manifest["members"])
    if (manifest["format"]!=1 or DATABASE not in names or names-{DATABASE}-OPTIONAL
            or {path.name for path in snapshot.iterdir()} != names | {"manifest.json"}):
        raise ValueError("unexpected archive members")
    for name,checksum in manifest["members"].items():
        if fingerprint(snapshot/name)!=checksum:
            raise ValueError("archive bytes differ: "+name)
    if inventory(snapshot/DATABASE)!=manifest["inventory"]:
        raise ValueError("archive inventory differs")
    return manifest


def restore(snapshot,expected,destination):
    manifest = verify(snapshot,expected)
    with output_directory(destination) as stage:
        for name,checksum in manifest["members"].items():
            copy_private(snapshot/name,stage/name)
            if fingerprint(stage/name)!=checksum:
                raise ValueError("restored bytes differ: "+name)
        if verify(snapshot,expected)!=manifest:
            raise ValueError("archive changed during restore")
    return {"archive_id":manifest["archive_id"],"files":len(manifest["members"]),"inventory":manifest["inventory"]}


def event(snapshot,expected,event_id):
    verify(snapshot,expected)
    with closing(readonly(snapshot/DATABASE)) as db:
        row = db.execute("SELECT * FROM events WHERE event_id=?",(event_id,)).fetchone()
    if row is None:
        raise ValueError("event not present in this archive")
    return dict(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action",required=True)
    save = actions.add_parser("capture")
    save.add_argument("--database",type=Path,required=True)
    save.add_argument("--destination",type=Path,required=True)
    for name in ("blocked","drops"):
        save.add_argument("--"+name,type=Path)
    for action in ("verify","restore","event"):
        cmd = actions.add_parser(action)
        cmd.add_argument("--snapshot",type=Path,required=True)
        cmd.add_argument("--manifest-sha256",required=True)
        if action=="restore":
            cmd.add_argument("--destination",type=Path,required=True)
        if action=="event":
            cmd.add_argument("--event-id",required=True)
    args = parser.parse_args()
    if args.action=="capture":
        result = capture(args.database,args.destination,blocked=args.blocked,drops=args.drops)
    elif args.action=="restore":
        result = restore(args.snapshot,args.manifest_sha256,args.destination)
    elif args.action=="event":
        result = event(args.snapshot,args.manifest_sha256,args.event_id)
    else:
        result = verify(args.snapshot,args.manifest_sha256)
    print(encode(result))


if __name__=="__main__":
    main()
