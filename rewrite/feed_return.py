"""Stage an old source cursor after accounting for the new collector's trial interval."""
import argparse
from contextlib import ExitStack, closing
import hashlib
import json
from pathlib import Path
import re

from . import source_history, state_bundle
from .cutover import digest, output_directory, readonly
from .messages import valid_guid
from .storage import encode
from .trigger_return import publication_state


def load_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("supply original regular offline cursor and review files")
    with path.open("rb") as stream:
        raw = stream.read(65_537)
    if len(raw)>65_536:
        raise ValueError("cursor or review file is too large")
    return json.loads(raw),hashlib.sha256(raw).hexdigest()


def source_baseline(db, cursor, old_hash):
    current = db.execute("SELECT * FROM feed_cursor WHERE slot=1").fetchone()
    anchor = db.execute("SELECT * FROM feed_handoff WHERE slot=1").fetchone()
    if (not current or not anchor or not {"generation","continuity_lost"}<=set(anchor.keys())
            or not anchor["verified"] or anchor["continuity_lost"]
            or not valid_guid(anchor["generation"]) or anchor["generation"]!=current["generation"]
            or anchor["epoch"]!=current["epoch"] or anchor["sha256"]!=old_hash
            or current["position"]<anchor["position"]):
        raise ValueError("original imported generation could not be verified")
    kind,*scope = json.loads(current["scope"])
    if kind=="email":
        compatible = (len(scope)==4 and cursor.get("folder")==scope[-1]
                      and type(cursor.get("uidvalidity")) is int and str(cursor["uidvalidity"])==anchor["epoch"])
        previous = cursor.get("last_uid")
    else:
        compatible = (kind=="mc_chat" and scope==[str(cursor.get("bridge","")).rstrip("/")]
                      and cursor.get("source")=="mc_chat" and anchor["epoch"]=="game")
        previous = cursor.get("last_seq")
    if not compatible or type(previous) is not int or previous!=anchor["position"]:
        raise ValueError("original cursor differs from the imported source baseline")
    overlap = None
    if kind=="mc_chat" and db.execute("SELECT 1 FROM sqlite_master WHERE name='feed_game_overlap'").fetchone():
        overlap = db.execute("SELECT position,checksum,verified FROM feed_game_overlap WHERE generation=? AND position=?",
                             (current["generation"],current["position"])).fetchone()
        if overlap and overlap["verified"]!=1:
            raise ValueError("previous game source frame still needs readback")
    reads = db.execute("SELECT * FROM feed_reads ORDER BY position").fetchall()
    positions = [row["position"] for row in reads]
    if (any(row["generation"]!=anchor["generation"] for row in reads)
            or any(not previous<pos<=current["position"] for pos in positions)
            or (positions[-1] if positions else previous)!=current["position"]
            or (kind=="mc_chat" and len(positions)!=current["position"]-previous)):
        raise ValueError("source ledger has a generation change or an unaccounted game sequence")
    return kind,anchor,current,reads,overlap


def review_interval(review, snapshot_sha, old_hash, anchor, current, reads):
    if (set(review)!={"snapshot_sha256","legacy_cursor_sha256","source","filtered_positions"}
            or review["snapshot_sha256"]!=snapshot_sha or review["legacy_cursor_sha256"]!=old_hash):
        raise ValueError("review must identify this verified snapshot and original source cursor")
    source = review["source"]
    if (not isinstance(source,dict) or set(source)!={"epoch","head","evidence"}
            or source["epoch"]!=anchor["epoch"] or type(source["head"]) is not int
            or not current["position"]<=source["head"]<2**63-1
            or not isinstance(source["evidence"],str) or not source["evidence"].strip()):
        raise ValueError("review must confirm the same source epoch and a current head at or above the trial cursor")
    filtered = review["filtered_positions"]
    if (not isinstance(filtered,dict) or filtered.keys()!={str(row["position"]) for row in reads if not row["mid"]}
            or any(not isinstance(reason,str) or not reason.strip() for reason in filtered.values())):
        raise ValueError("review must explain every filtered trial source position")
    return source["head"]


def captured_text(db, file):
    offset = 0
    content = bytearray()
    for piece in db.execute("SELECT start,bytes FROM pieces WHERE id=? ORDER BY start",(file["id"],)):
        if piece["start"]!=offset or not piece["bytes"]:
            raise ValueError("source text has a missing archive segment")
        content.extend(piece["bytes"])
        offset += len(piece["bytes"])
    if file["size"]!=offset or file["sha"]!=hashlib.sha256(content).hexdigest():
        raise ValueError("source text differs from its archive checksum")
    return content.decode("utf-8")


def recovered_records(db,previous):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='feed_recovered'").fetchone():
        return [],None
    records = db.execute("SELECT * FROM feed_recovered ORDER BY position").fetchall()
    if not records:
        return [],None
    plans = {row["plan_sha256"] for row in records}
    if (len(plans)!=1 or not isinstance(records[0]["plan_sha256"],str)
            or not re.fullmatch(r"[a-f0-9]{64}",records[0]["plan_sha256"])
            or any(type(row["position"]) is not int or not 0<row["position"]<=previous
                   or not valid_guid(row["mid"]) for row in records)):
        raise ValueError("invalid recovered source records below the legacy cursor")
    return records,records[0]["plan_sha256"]


def inspect_capture(ledgers, kind, instance_id, read, host_session, *, recovered=False):
    db = ledgers[3]
    mid = read["mid"]
    local = db.execute("SELECT * FROM archive WHERE id=?",(mid,)).fetchone()
    if not local:
        raise ValueError("source capture is missing from the local archive")
    body = json.loads(local["body"])
    levels = {"Info"} if kind=="email" else {"Event","Info"}
    if recovered and kind=="mc_chat":
        levels.add("Notify")
    if (body["message_id"]!=mid or body["trigger_name"]!=kind or body["instance_id"]!=instance_id
            or body["level"] not in levels):
        raise ValueError("source capture and its Trigger archive disagree")
    files = db.execute("SELECT * FROM files WHERE message_id=? ORDER BY position",(mid,)).fetchall()
    if files:
        if (len(files)!=1 or files[0]["name"]!="message.txt" or files[0]["position"]!=0
                or body["text"]!=kind+" 来源长文，全文见附件。"):
            raise ValueError("unexpected source archive attachments")
        text = captured_text(db,files[0])
    else:
        text = body["text"]
    if read["digest"]!=hashlib.sha256(encode([body["level"],text]).encode("utf-8")).hexdigest():
        raise ValueError("source capture digest differs from the archived text")
    return publication_state(ledgers,local,body,files,"trigger/"+instance_id,host_session)


def inspect_return(bundle, expected, legacy_cursor, review, feed_instance, manager_instance, host_session):
    original,old_hash = load_file(legacy_cursor)
    approval,approval_hash = load_file(review)
    manifest = state_bundle.verify(bundle,expected)
    router, = (label for label in manifest["files"] if label.startswith("router:"))
    labels = [router,"hermes:"+manager_instance,"hermes-host:"+manager_instance,"trigger:"+feed_instance]
    if not set(labels)<=manifest["files"].keys():
        raise ValueError("snapshot is missing the selected source and consumer ledgers")
    source_file = bundle/state_bundle.filename(labels[3])
    with ExitStack() as stack:
        ledgers = [stack.enter_context(closing(readonly(bundle/state_bundle.filename(label)))) for label in labels]
        r,_,host,feed = ledgers
        kind,anchor,current,reads,overlap = source_baseline(feed,original,old_hash)
        head = review_interval(approval,expected,old_hash,anchor,current,reads)
        if not host.execute("SELECT 1 FROM adapter_native_binding WHERE slot=1 AND session=?",
                            (host_session,)).fetchone():
            raise ValueError("the configured native consumer does not match the snapshot")
        instance, = feed.execute("SELECT id FROM instance WHERE slot=1").fetchone()
        recovered, recovery_plan = recovered_records(feed,anchor["position"])
        captured = [row["mid"] for row in [*recovered,*reads] if row["mid"]]
        stored = {row[0] for row in feed.execute("SELECT id FROM archive")}
        published = {row["message_id"] for row in source_history.live_messages(r,actor="trigger/"+instance)}
        if (not valid_guid(instance) or len(captured)!=len(set(captured)) or set(captured)!=stored
                or not published<=stored):
            raise ValueError("trial archive does not match the exact source interval")
        states = [{"position":row["position"],"message_id":row["mid"],"recovered":True,
                   "state":inspect_capture(ledgers,kind,instance,row,host_session,recovered=True)} for row in recovered]
        states.extend({"position":row["position"],"message_id":row["mid"],
                       "state":inspect_capture(ledgers,kind,instance,row,host_session) if row["mid"] else "reviewed_filter"}
                      for row in reads)
    if state_bundle.verify(bundle,expected)!=manifest:
        raise ValueError("snapshot changed during source audit")
    complete = all(row["state"] in {"router_archived","native_received","reviewed_filter"} for row in states)
    report = {"source":kind,"instance_id":instance,"source_epoch":anchor["epoch"],"initial_position":anchor["position"],
              "trial_position":current["position"],"observed_head":head,"safe_to_stage":complete,"events":states,
              "snapshot_sha256":expected,"legacy_cursor_sha256":old_hash,"review_sha256":approval_hash}
    if recovery_plan:
        report["recovery_plan_sha256"] = recovery_plan
    if overlap:
        report["game_overlap"] = {"position":overlap["position"],"sha256":overlap["checksum"]}
    return report,original,source_file,manifest["files"][labels[3]]["sha256"]


def audit(**kwargs):
    return inspect_return(**kwargs)[0]


def stage(destination, **kwargs):
    report,cursor,source_file,checksum = inspect_return(**kwargs)
    if not report["safe_to_stage"]:
        raise ValueError("one or more trial captures still need a Router or native receipt")
    with output_directory(destination) as folder:
        kind = report["source"]
        if kind=="email":
            cursor.update(last_uid=report["trial_position"],uidnext=report["observed_head"]+1)
        else:
            cursor["last_seq"] = report["trial_position"]
        cursor_file = folder/(kind+"_cursor.json")
        cursor_file.write_text(encode(cursor),encoding="utf-8")
        cursor_file.chmod(0o600)
        archive = folder/"trial-archive.sqlite3"
        state_bundle.copy_private(source_file,archive)
        if digest(archive)!=checksum:
            raise ValueError("source archive changed while staging the return")
        note = folder/"return.json"
        note.write_text(encode({**report,"cursor_sha256":digest(cursor_file),"trial_archive_sha256":checksum}),
                        encoding="utf-8")
        note.chmod(0o600)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation",required=True)
    read, write = commands.add_parser("audit"), commands.add_parser("stage")
    for command in (read,write):
        for name in ("bundle","legacy-cursor","review"):
            command.add_argument("--"+name,type=Path,required=True)
        command.add_argument("--manifest-sha256",dest="expected",required=True)
        for name in ("feed-instance","manager-instance","host-session"):
            command.add_argument("--"+name,required=True)
    write.add_argument("--destination",type=Path,required=True)
    args = vars(parser.parse_args())
    try:
        result = {"audit":audit,"stage":stage}[args.pop("operation")](**args)
    except (ValueError,KeyError,TypeError) as exc:
        parser.exit(1,str(exc)+"\n")
    print(encode(result))


if __name__=="__main__":
    main()
