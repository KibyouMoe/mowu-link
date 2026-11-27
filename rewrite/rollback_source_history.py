"""Preserve audited Trigger messages through fallback and subsequent re-entry."""
from contextlib import closing
import json
import os
import sqlite3

from . import attachments, rollback_peers, source_history
from .cutover import digest, readonly


NAME = "source-history.db"
TABLES = ("messages","history_source_messages","history_source_operations",
          "history_source_uploads","uploads","chunks","attachment_grants")


def expected_for_legacy(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rewrite_source_history'").fetchone():
        row, = db.execute("SELECT archive_sha256 FROM rewrite_source_history")
        return row[0]
    return None


def bind_to_legacy(path, checksum):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS rewrite_source_history
                       (slot INTEGER PRIMARY KEY CHECK(slot=1), archive_sha256 TEXT NOT NULL)""")
        db.execute("INSERT OR REPLACE INTO rewrite_source_history VALUES (1,?)",(checksum,))


def freeze(source, expected, destination):
    rollback_peers.freeze(source,expected,destination,label="source archive")


def archive_from_trial(trial, parent, expected):
    if expected is None:
        return None
    path = parent/"private-review"/NAME
    path.parent.mkdir(mode=0o700,exist_ok=True)
    freeze(trial/NAME,expected,path)
    return path


def import_archive(target, path):
    for statement in source_history.SCHEMA:
        target.execute(statement)
    with closing(readonly(path)) as original:
        if [row[0] for row in original.execute("PRAGMA integrity_check")]!=["ok"]:
            raise ValueError("source archive database integrity check failed")
        for table in TABLES:
            rows = original.execute(f"SELECT * FROM {table} ORDER BY rowid")
            values = ",".join("?" for _ in rows.description)
            target.executemany(f"INSERT INTO {table} VALUES ({values})",rows)


def write_archive(db, archive, previous, trial_sha, sources):
    if sources is None or not sources.events:
        if previous is None:
            return None
        previous.rename(archive)
        return digest(archive)

    frames = {mid:(did,sources.frames[did][2]) for did,mid in sources.frame_sources.items()}
    fd = os.open(archive,os.O_CREAT | os.O_EXCL | os.O_WRONLY,0o600)
    os.close(fd)
    with closing(sqlite3.connect(archive)) as target, target:
        target.executescript(source_history.MESSAGE_TABLE+attachments.SCHEMA)
        for statement in source_history.SCHEMA:
            target.execute(statement)
        if previous is not None:
            import_archive(target,previous)
        with closing(readonly(archive.parent/"source-returns"/"hermes-host.sqlite3")) as host:
            for row in source_history.live_messages(db,trigger_only=True):
                mid = row["message_id"]
                if mid not in sources.events:
                    continue
                state = sources.dispositions[mid]
                did,ack = frames.get(mid,(None,None))
                native_id,turn_id = None,None
                if state=="native_received":
                    proof = json.loads(host.execute("SELECT receipt FROM intents WHERE delivery_id=?",
                                                    (mid,)).fetchone()[0])
                    native_id,turn_id = proof["native_id"],proof.get("turn_id")
                target.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",tuple(row))
                target.execute("INSERT INTO history_source_messages VALUES (?,?,?,?,?,?,?)",
                               (mid,trial_sha,state,did,ack,native_id,turn_id))
        for uid,row in sources.uploads.items():
            actor,fid = row["owner"],row["attachment_id"]
            target.execute("INSERT INTO uploads VALUES (?,?,?,?,?,?,?)",
                           (uid,"history/source-"+trial_sha,row["name"],row["size_bytes"],row["sha256"],
                            row["next_offset"],fid))
            target.execute("INSERT INTO history_source_uploads VALUES (?,?)",(uid,actor))
            target.executemany("INSERT INTO attachment_grants VALUES (?,?)",((fid,actor),(fid,"hermes")))
            rollback_peers.copy_chunks(db,row,lambda start,data:target.execute(
                "INSERT INTO chunks VALUES (?,?,?)",(uid,start,data)))
        for row in db.execute("SELECT actor,key,request,response FROM operations WHERE actor LIKE 'trigger/%' ORDER BY rowid"):
            operation,body = json.loads(row["request"])
            if ((operation==["POST","/v1/messages"] and sources.events.get(body.get("message_id"))==row["actor"])
                    or (operation==["POST","/v1/uploads"] and json.loads(row["response"]).get("upload_id") in sources.uploads)):
                target.execute("INSERT INTO history_source_operations VALUES (?,?,?,?)",tuple(row))
    return digest(archive)
