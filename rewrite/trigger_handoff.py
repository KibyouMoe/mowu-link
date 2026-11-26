"""Stage reviewed unread legacy Trigger events for a new, authenticated Trigger instance."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from uuid import UUID, uuid5

from .cutover import digest, output_directory, readonly
from .messages import valid_guid
from .storage import encode
from .trigger import Trigger
from .trigger_archive import DATABASE, verify


LEVELS = {"event", "notify", "info"}
HANDOFF_DB = "trigger.sqlite3"
LONG_TEXT_SUMMARY = "旧 Trigger 迁入，原文见附件。"
LONG_TEXT_NAME = "legacy-trigger-original.txt"


def plan(snapshot, expected, subscriber, consumer_cursor, levels, source_id):
    verify(snapshot,expected)
    return read_plan(snapshot,expected,subscriber,consumer_cursor,levels,source_id)[0]


def read_plan(snapshot, expected, subscriber, consumer_cursor, levels, source_id):
    if (not isinstance(subscriber,str) or not subscriber or len(subscriber)>128
            or type(consumer_cursor) is not int or not 0<=consumer_cursor<2**63
            or not isinstance(levels,list) or not levels or any(level not in LEVELS for level in levels)
            or len(set(levels))!=len(levels) or not valid_guid(source_id)):
        raise ValueError("supply the exact subscriber, consumer cursor, effective levels and stable source UUID")
    namespace = UUID(source_id)
    levels = sorted(levels)
    with closing(readonly(snapshot/DATABASE)) as db:
        active = dict(db.execute("SELECT name,since FROM subscriptions WHERE status='active' ORDER BY name"))
        if subscriber not in active:
            raise ValueError("subscriber was not active in the legacy archive")
        row = db.execute("SELECT last_id FROM cursors WHERE subscriber=?",(subscriber,)).fetchone()
        legacy_cursor = row[0] if row else active[subscriber]
        tail = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        if consumer_cursor!=legacy_cursor or not active[subscriber]<=legacy_cursor<=tail:
            raise ValueError("consumer cursor and legacy receipt ledger disagree")
        marks = ",".join("?" for _ in levels)
        rows = db.execute(f"""SELECT e.* FROM events e WHERE e.id>?
            AND COALESCE(e.level,'info') IN ({marks}) AND COALESCE(e.disposition,'delivered')!='dropped'
            AND NOT EXISTS (SELECT 1 FROM receipts r WHERE r.subscriber=? AND r.event_id=e.event_id)
            ORDER BY e.id""",(legacy_cursor,*levels,subscriber)).fetchall()
    items = []
    for row in rows:
        level = row["level"] or "info"
        allowed = ["Info"]
        if level in ("event","notify"):
            allowed.insert(0,"Notify")
        if level=="event" and type(row["priority"]) is int and row["priority"]<=0:
            allowed.insert(0,"Event")
        items.append({"seq":row["id"],"legacy_event_id":row["event_id"],"source":row["source"],
                      "legacy_level":level,"allowed_levels":allowed,
                      "message_id":str(uuid5(namespace,"legacy-event/"+row["event_id"]))})
    contents = {"archive_sha256":expected,"subscriber":subscriber,"consumer_cursor":legacy_cursor,"levels":levels,
                "source_id":source_id,"instance_id":str(uuid5(namespace,"legacy-trigger-handoff")),
                "other_active_subscribers":sorted(active.keys()-{subscriber}),"snapshot_tail":tail,"candidates":items}
    return {"plan":contents,"plan_sha256":hashlib.sha256(encode(contents).encode("utf-8")).hexdigest()},rows


def approved_level(selection, candidate):
    if isinstance(selection,dict):
        if set(selection)=={"level","evidence"} and selection["level"] in candidate["allowed_levels"]:
            if isinstance(selection["evidence"],str) and selection["evidence"].strip():
                return selection["level"]
        if set(selection)=={"already_received"} and isinstance(selection["already_received"],str):
            if selection["already_received"].strip():
                return None
    raise ValueError("event needs original consumer evidence and a permitted disposition: "+str(candidate["seq"]))


def reviewed_levels(checked, review_data):
    items = checked["plan"]["candidates"]
    decisions = review_data.get("events")
    if (set(review_data)!={"plan_sha256","events"} or review_data["plan_sha256"]!=checked["plan_sha256"]
            or not isinstance(decisions,dict) or decisions.keys()!={str(item["seq"]) for item in items}):
        raise ValueError("review must cover each unread event in the exact plan")
    return [approved_level(decisions[str(item["seq"])],item) for item in items]


def legacy_attachment_refs(row):
    payload = json.loads(row["payload_json"]) if row["payload_json"] is not None else None
    if not isinstance(payload,dict) or "attachments" not in payload:
        return []
    metadata = payload["attachments"]
    if not isinstance(metadata,list) or not 1<=len(metadata)<=10:
        raise ValueError("invalid legacy attachment list")
    references = []
    for item in metadata:
        checksum = item.get("id") if isinstance(item,dict) else None
        if not isinstance(checksum,str) or not re.fullmatch("[0-9a-f]{64}",checksum):
            raise ValueError("invalid legacy attachment identifier")
        name,size = item.get("name"),item.get("size")
        if (item.get("path")!="/attachments/"+checksum or item.get("sha256")!=checksum
                or type(size) is not int or size<=0 or not isinstance(name,str) or not name.strip()
                or name in (".","..") or any(c in name for c in ("/","\\","\x00","\n","\r"))):
            raise ValueError("invalid legacy attachment metadata")
        references.append((name,size,checksum))
    return references


def legacy_files(row, directory, stack):
    references = legacy_attachment_refs(row)
    if references and (directory is None or directory.is_symlink()):
        raise ValueError("legacy attachment references require an offline content store")
    streams,checksums = [],[]
    for name,size,checksum in references:
        source = directory/checksum
        if source.is_symlink() or not source.is_file():
            raise ValueError("legacy attachment bytes are missing")
        streams.append((name,stack.enter_context(source.open("rb"))))
        checksums.append((size,checksum))
    return streams,checksums


def stage_event(db, trigger, row, item, level, attachments):
    generated = row["ts"]
    if (not isinstance(generated,str) or datetime.fromisoformat(generated).tzinfo is None
            or not row["source"].strip()):
        raise ValueError("legacy event time or source cannot be represented")
    origin = {"trigger_name":row["source"],"generated_at":generated,"message_id":item["message_id"]}
    with ExitStack() as stack:
        files,expected = legacy_files(row,attachments,stack)
        text = row["text"]
        if len(text)>200:
            files.append((LONG_TEXT_NAME,BytesIO(text.encode("utf-8"))))
            text = LONG_TEXT_SUMMARY
        mid = trigger.archive(db,level,text,files,origin=origin)
        copied = db.execute("SELECT size,sha FROM files WHERE message_id=? ORDER BY position",(mid,)).fetchall()
        if [tuple(part) for part in copied[:len(expected)]]!=expected:
            raise ValueError("legacy attachment bytes do not match the archived reference")


def stage(snapshot, expected, saved, review, destination, *, attachments=None):
    manifest = verify(snapshot,expected)
    selection = json.loads(saved.read_text(encoding="utf-8"))
    supplied = selection["plan"]
    checked,rows = read_plan(snapshot,expected,*(supplied[key] for key in (
        "subscriber","consumer_cursor","levels","source_id")))
    if checked!=selection:
        raise ValueError("handoff plan differs from the verified legacy archive")
    review_data = json.loads(review.read_text(encoding="utf-8"))
    items = checked["plan"]["candidates"]
    levels = reviewed_levels(checked,review_data)
    destination_id = checked["plan"]["instance_id"]
    with output_directory(destination) as folder:
        path = folder/HANDOFF_DB
        trigger = Trigger(path,"legacy-handoff",instance_id=destination_id)
        with trigger.db.transaction() as db:
            for row,item,level in zip(rows,items,levels,strict=True):
                if level is not None:
                    stage_event(db,trigger,row,item,level,attachments)
        if verify(snapshot,expected)!=manifest:
            raise ValueError("legacy archive changed during staging")
        path.chmod(0o600)
        journal = folder/"handoff.json"
        journal.write_text(encode({**checked,"review":review_data,"staged_sha256":digest(path)}),encoding="utf-8")
        journal.chmod(0o600)
        journal_sha256 = digest(journal)
    count = sum(level is not None for level in levels)
    return {"instance_id":destination_id,"pending":count,"already_received":len(items)-count,
            "plan_sha256":checked["plan_sha256"],"journal_sha256":journal_sha256}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation",required=True)
    read = operations.add_parser("plan")
    write = operations.add_parser("stage")
    for cmd in (read,write):
        cmd.add_argument("--snapshot",type=Path,required=True)
        cmd.add_argument("--manifest-sha256",dest="expected",required=True)
    read.add_argument("--subscriber",required=True)
    read.add_argument("--consumer-cursor",type=int,required=True)
    read.add_argument("--level",dest="levels",choices=sorted(LEVELS),action="append",required=True)
    read.add_argument("--source-id",required=True)
    for key in ("saved","review","destination"):
        write.add_argument("--"+key,type=Path,required=True)
    write.add_argument("--attachments",type=Path)
    args = vars(parser.parse_args())
    try:
        result = {"plan":plan,"stage":stage}[args.pop("operation")](**args)
    except (ValueError,KeyError,TypeError) as exc:
        parser.exit(1,str(exc)+"\n")
    print(encode(result))


if __name__=="__main__":
    main()
