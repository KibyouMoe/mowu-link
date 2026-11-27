"""Review and stage missing source records at or below a legacy collector cursor."""
import argparse
from contextlib import closing
from datetime import datetime
from email import policy
from email.parser import BytesParser
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
from uuid import UUID, uuid5

from . import trigger_archive
from .cutover import digest, output_directory, readonly
from .feed_return import load_file
from .feeds import Feed, mail_text, mc_frames, mc_text, source_text_digest
from .messages import valid_guid
from .storage import Fault, encode
from .trigger_handoff import approved_level


DATABASE = "feed.sqlite3"
COMMON = {"kind","scope","epoch","after","through","source_id","observed_at",
          "continuity_evidence","legacy_cursor_sha256","records"}


def integer(value):
    return type(value) is int and 0<=value<2**63


def valid_digest(value):
    return isinstance(value,str) and re.fullmatch(r"[0-9a-f]{64}",value)


def identity(inventory, cursor, cursor_sha):
    kind = inventory.get("kind")
    expected_fields = COMMON | ({"player"} if kind=="mc_chat" else set())
    if inventory.keys()!=expected_fields:
        raise ValueError("wrong source inventory fields")
    if (not valid_guid(inventory["source_id"]) or inventory["legacy_cursor_sha256"]!=cursor_sha
            or not all(integer(inventory[key]) for key in ("after","through"))
            or inventory["after"]>inventory["through"]
            or not isinstance(inventory["continuity_evidence"],str) or not inventory["continuity_evidence"].strip()
            or not isinstance(inventory["observed_at"],str)
            or datetime.fromisoformat(inventory["observed_at"]).tzinfo is None):
        raise ValueError("source inventory needs the exact cursor and a confirmed observation")
    scope,epoch = inventory["scope"],inventory["epoch"]
    if not isinstance(scope,list):
        raise ValueError("invalid source scope")
    if kind=="email":
        valid = (len(scope)==4 and integer(scope[1]) and 0<scope[1]<65536
                 and all(isinstance(scope[i],str) and scope[i].strip() for i in (0,2,3))
                 and integer(cursor.get("uidvalidity")) and cursor["uidvalidity"]>0
                 and epoch==str(cursor["uidvalidity"]) and cursor.get("folder")==scope[3]
                 and integer(cursor.get("last_uid")) and cursor["last_uid"]==inventory["through"])
    else:
        valid = (kind=="mc_chat" and len(scope)==1 and isinstance(scope[0],str) and scope[0].strip()
                 and isinstance(cursor.get("bridge"),str) and cursor["bridge"].rstrip("/")==scope[0]
                 and cursor.get("source")=="mc_chat" and epoch=="game"
                 and integer(cursor.get("last_seq")) and cursor["last_seq"]==inventory["through"]
                 and isinstance(inventory["player"],str) and inventory["player"].strip())
    if not valid:
        raise ValueError("source inventory does not match the legacy mailbox or bridge cursor")


def positions(inventory):
    entries = inventory["records"]
    lo,hi = inventory["after"],inventory["through"]
    if not isinstance(entries,dict) or any(
            not re.fullmatch(r"[1-9][0-9]{0,18}",key) or not lo<int(key)<=hi or not valid_digest(value)
            for key,value in entries.items()):
        raise ValueError("source positions or record digests are outside the verified interval")
    result = sorted((int(key),value) for key,value in entries.items())
    if inventory["kind"]=="mc_chat" and len(result)!=hi-lo:
        raise ValueError("game source interval has an unreadable sequence; recover the original frame first")
    return result


def record(directory,position,checksum,inventory):
    kind,epoch = inventory["kind"],inventory["epoch"]
    path = directory/(str(position)+(".eml" if kind=="email" else ".sse"))
    if directory.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("original source record is missing")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=checksum:
        raise ValueError("original source record digest mismatch")
    if kind=="email":
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        legacy_id = f"email:{epoch}:{position}"
        expected = {"uid":position,"uidvalidity":int(epoch),"folder":inventory["scope"][-1],
                    "from":str(msg.get("From","") or "").strip(),"subject":str(msg.get("Subject","") or "").strip()}
        return legacy_id,expected,"Info",mail_text(raw,message=msg)
    frames = list(mc_frames(BytesIO(raw)))
    if len(frames)!=1 or frames[0][0]!=str(position):
        raise ValueError("original game frame does not have the required numeric SSE id")
    try:
        payload = json.loads(frames[0][1])
    except ValueError:
        payload = frames[0][1]
    if not isinstance(payload,(str,dict)):
        raise ValueError("unsupported original game frame payload")
    level,text = mc_text(payload,inventory["player"])
    return f"mc_chat:sse:{position}",payload,level,text


def prior_state(db,legacy_id,expected,level,kind):
    row = db.execute("SELECT id,source,payload_json,disposition FROM events WHERE event_id=?",(legacy_id,)).fetchone()
    if row is None:
        return ("filtered" if level is None else "missing"),None
    try:
        payload = json.loads(row["payload_json"]) if row["payload_json"] is not None else None
    except ValueError:
        return "conflicting_legacy_event",row["id"]
    if kind=="email":
        matches = isinstance(payload,dict) and all(payload.get(key)==value for key,value in expected.items())
    else:
        matches = encode(payload)==encode(expected)
    if row["source"]!=kind or not matches:
        return "conflicting_legacy_event",row["id"]
    disposition = row["disposition"] or "delivered"
    if disposition not in {"delivered","downgraded","dropped"}:
        return "conflicting_legacy_event",row["id"]
    return ("legacy_dropped" if disposition=="dropped" else "legacy_stored"),row["id"]


def inspect(snapshot,expected,legacy_cursor,source_manifest,source_sha256):
    manifest = trigger_archive.verify(snapshot,expected)
    cursor,cursor_sha = load_file(legacy_cursor)
    inventory,source_sha = load_file(source_manifest)
    if source_sha!=source_sha256 or not isinstance(cursor,dict) or not isinstance(inventory,dict):
        raise ValueError("source inventory differs from its trusted digest")
    identity(inventory,cursor,cursor_sha)
    kind,epoch = inventory["kind"],inventory["epoch"]
    namespace = UUID(inventory["source_id"])
    directory = source_manifest.parent/"records"
    rows,missing = [],{}
    with closing(readonly(snapshot/trigger_archive.DATABASE)) as db:
        for position,checksum in positions(inventory):
            legacy_id,projection,level,text = record(directory,position,checksum,inventory)
            status,row_id = prior_state(db,legacy_id,projection,level,kind)
            item = {"position":position,"legacy_event_id":legacy_id,"state":status,"legacy_row_id":row_id}
            if status=="missing":
                item["message_id"] = str(uuid5(namespace,f"legacy-feed/{kind}/{epoch}/{position}"))
                item["allowed_levels"] = ["Event","Notify","Info"] if level=="Event" else ["Info"]
                missing[position] = text
            rows.append(item)
    if trigger_archive.verify(snapshot,expected)!=manifest:
        raise ValueError("legacy Trigger snapshot changed during source audit")
    report = {"source":kind,"epoch":epoch,"after":inventory["after"],"through":inventory["through"],
              "source_id":str(namespace),"instance_id":str(uuid5(namespace,"legacy-feed/"+kind)),
              "legacy_trigger_sha256":expected,"legacy_cursor_sha256":cursor_sha,"source_sha256":source_sha,"events":rows}
    return {"plan":report,"plan_sha256":hashlib.sha256(encode(report).encode()).hexdigest()},inventory,missing


def audit(**kwargs):
    return inspect(**kwargs)[0]


def stage(destination,review,**kwargs):
    result,inventory,missing = inspect(**kwargs)
    plan = result["plan"]
    if any(row["state"]=="conflicting_legacy_event" for row in plan["events"]):
        raise ValueError("old source identity conflicts with records in the legacy Trigger archive")
    approval,review_sha = load_file(review)
    if (not isinstance(approval,dict) or approval.keys()!={"plan_sha256","events"}
            or approval["plan_sha256"]!=result["plan_sha256"] or not isinstance(approval["events"],dict)
            or approval["events"].keys()!={str(position) for position in missing}):
        raise ValueError("approve every missing position in the exact audited plan")
    selected = [(row,approved_level(approval["events"][str(row["position"])],{**row,"seq":row["position"]}))
                for row in plan["events"] if row["state"]=="missing"]
    with output_directory(destination) as output:
        database = output/DATABASE
        feed = Feed(database,plan["source"],inventory["scope"],instance_id=plan["instance_id"])
        imported = feed.import_cursor(kwargs["legacy_cursor"],mailbox_confirmed=True)
        if imported["legacy_sha256"]!=plan["legacy_cursor_sha256"]:
            raise ValueError("legacy cursor changed during recovery staging")
        with feed.db.transaction() as db:
            db.execute("""CREATE TABLE feed_recovered (
                position INTEGER PRIMARY KEY, digest TEXT NOT NULL, mid TEXT NOT NULL UNIQUE, plan_sha256 TEXT NOT NULL
            )""")
            for row,level in selected:
                if level is None:
                    continue
                position = row["position"]
                text = missing[position]
                origin = {"message_id":row["message_id"],"trigger_name":plan["source"],
                          "generated_at":inventory["observed_at"]}
                mid = feed.archive_source(db,level,text,origin=origin)
                db.execute("INSERT INTO feed_recovered VALUES (?,?,?,?)",
                           (position,source_text_digest(level,text),mid,result["plan_sha256"]))
        database.chmod(0o600)
        outcome = {**result,"review_sha256":review_sha,"database_sha256":digest(database),
                   "staged":sum(level is not None for _,level in selected),
                   "already_received":sum(level is None for _,level in selected)}
        note = output/"recovery.json"
        with open(note,"x",encoding="utf-8",opener=lambda name,flags:os.open(name,flags,0o600)) as handle:
            handle.write(encode({**outcome,"review":approval}))
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation",required=True)
    read,write = commands.add_parser("audit"),commands.add_parser("stage")
    for command in (read,write):
        for name in ("snapshot","legacy-cursor","source-manifest"):
            command.add_argument("--"+name,type=Path,required=True)
        command.add_argument("--trigger-sha256",dest="expected",required=True)
        command.add_argument("--source-sha256",required=True)
    for name in ("review","destination"):
        write.add_argument("--"+name,type=Path,required=True)
    args = vars(parser.parse_args())
    try:
        print(encode({"audit":audit,"stage":stage}[args.pop("operation")](**args)))
    except (OSError,ValueError,Fault) as exc:
        parser.exit(1,str(exc)+"\n")


if __name__=="__main__":
    main()
