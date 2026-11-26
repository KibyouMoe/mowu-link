"""Verify a Trigger handoff's contiguous receipt prefix before staging a legacy return."""
import argparse
from contextlib import ExitStack, closing
import hashlib
import json
from pathlib import Path

from . import state_bundle, trigger_archive
from .cutover import output_directory, readonly
from .storage import encode
from .trigger_handoff import LONG_TEXT_NAME, LONG_TEXT_SUMMARY, legacy_attachment_refs, read_plan, reviewed_levels


CONSUMED = {"legacy_received","native_received","reviewed_info_read"}
PENDING = {"not_published","not_sent_to_host","archive_only"}


def matches_hash(path, expected):
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular offline evidence file")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest()!=expected:
        raise ValueError("handoff journal digest differs from the recorded value")
    return json.loads(content)


def require_ledger(db, sql, params=()):
    if not db.execute(sql,params).fetchone():
        raise ValueError("required receipt ledger record is missing")


def router_files(db, files, actor):
    result = []
    for file in files:
        metadata = db.execute("""SELECT attachment_id,name,sha256,size_bytes FROM uploads
             JOIN attachment_grants USING(attachment_id) WHERE attachment_id=? AND owner=?
             AND actor='hermes' AND next_offset=size_bytes""",(file["file_id"],actor)).fetchone()
        if not metadata or (metadata["name"],metadata["size_bytes"],metadata["sha256"])!=(
                file["name"],file["size"],file["sha"]):
            raise ValueError("attachment binding differs across receipt ledgers")
        result.append(dict(metadata))
    return result


def receipt_state(ledgers, item, legacy, level, actor, session, read_info):
    router,manager,host,trigger = ledgers
    mid = item["message_id"]
    local = trigger.execute("SELECT * FROM archive WHERE id=?",(mid,)).fetchone()
    message = router.execute("SELECT * FROM messages WHERE message_id=?",(mid,)).fetchone()
    frames = router.execute("""SELECT * FROM deliveries WHERE consumer='hermes' AND kind='message.deliver'
                             AND json_extract(payload,'$.message_id')=?""",(mid,)).fetchall()
    queued = manager.execute("SELECT * FROM queue WHERE message_id=?",(mid,)).fetchone()
    intent = host.execute("SELECT * FROM intents WHERE delivery_id=?",(mid,)).fetchone()
    if level is None:
        return "receipt_mismatch" if any((local,message,frames,queued,intent)) else "legacy_received"
    if not local:
        return "receipt_mismatch"
    body = json.loads(local["body"])
    text = legacy["text"]
    expected_files = legacy_attachment_refs(legacy)
    if len(text)>200:
        full = text.encode("utf-8")
        expected_files.append((LONG_TEXT_NAME,len(full),hashlib.sha256(full).hexdigest()))
        text = LONG_TEXT_SUMMARY
    if (body.get("message_id")!=mid or "trigger/"+str(body.get("instance_id"))!=actor
            or body.get("trigger_name")!=item["source"] or body.get("level")!=level
            or body.get("generated_at")!=legacy["ts"] or body.get("text")!=text):
        return "receipt_mismatch"
    stored_files = trigger.execute("SELECT * FROM files WHERE message_id=? ORDER BY position",(mid,)).fetchall()
    if [(row["name"],row["size"],row["sha"]) for row in stored_files]!=expected_files:
        return "receipt_mismatch"
    if not message:
        return "receipt_mismatch" if any((local["receipt"],frames,queued,intent)) else "not_published"
    if (message["owner"]!=actor or message["source"]!=item["source"] or message["level"]!=level
            or not message["received_at"]):
        return "receipt_mismatch"
    if not local["receipt"]:
        return "trigger_receipt_pending"
    t04 = json.loads(local["receipt"])
    if (message["payload"]!=encode(body) or t04.get("message_id")!=mid
            or t04.get("received_at")!=message["received_at"]):
        return "receipt_mismatch"
    if [row["file_id"] for row in stored_files]!=body["attachment_ids"]:
        return "receipt_mismatch"
    files = router_files(router,stored_files,actor)
    if level=="Info":
        if any((frames,queued,intent)):
            return "receipt_mismatch"
        if item["legacy_level"]!="info":
            return "archive_only"
        return "reviewed_info_read" if read_info else "info_read_unknown"

    expected = {**body,"source":item["source"]}
    if len(frames)!=1 or frames[0]["payload"]!=encode(expected):
        return "receipt_mismatch"
    frame = frames[0]
    if queued and queued["payload"]!=frame["payload"]:
        return "receipt_mismatch"
    if not intent:
        if (frame["ack"] and not queued) or (queued and queued["receipt"]):
            return "receipt_mismatch"
        notice = manager.execute("SELECT first_try FROM manager_notices WHERE message_id=?",(mid,)).fetchone()
        return "platform_receipt_needs_review" if notice and notice[0] is not None else "not_sent_to_host"
    native_payload = encode({**expected,"attachments":files} if files else expected)
    expected_digest = hashlib.sha256(native_payload.encode("utf-8")).hexdigest()
    if (not frame["ack"] or not queued or intent["session"]!=session
            or intent["payload"]!=native_payload or intent["digest"]!=expected_digest):
        return "receipt_mismatch"
    if not intent["receipt"]:
        return "receipt_mismatch" if queued["receipt"] else "host_receipt_unknown"
    proof = json.loads(intent["receipt"])
    if (proof.get("delivery_id")!=mid or proof.get("session")!=session or proof.get("digest")!=expected_digest
            or not intent["native_id"] or proof.get("native_id")!=intent["native_id"]
            or (files and proof.get("media_sha256")!=[file["sha256"] for file in files])):
        return "receipt_mismatch"
    if not queued["receipt"]:
        return "hermes_receipt_pending"
    return "native_received" if json.loads(queued["receipt"])==proof else "receipt_mismatch"


def collect_info_reads(info_review, checked, levels):
    if info_review is None:
        return {},None
    if info_review.is_symlink() or not info_review.is_file():
        raise ValueError("expected a regular Info read review")
    content = info_review.read_bytes()
    review = json.loads(content)
    reads = review.get("archive_reads")
    options = {str(item["seq"]) for item,level in zip(checked["plan"]["candidates"],levels,strict=True)
               if level=="Info" and item["legacy_level"]=="info"}
    if (set(review)!={"plan_sha256","archive_reads"} or review["plan_sha256"]!=checked["plan_sha256"]
            or not isinstance(reads,dict) or not reads.keys()<=options
            or any(not isinstance(value,str) or not value.strip() for value in reads.values())):
        raise ValueError("Info read review must match this plan and confirm only the original Info items")
    return reads,hashlib.sha256(content).hexdigest()


def audit(snapshot, expected, journal, journal_sha256, bundle, bundle_sha256,
          trigger_instance, manager_instance, host_session, *, info_review=None):
    record = matches_hash(journal,journal_sha256)
    old = record["plan"]
    legacy_manifest = trigger_archive.verify(snapshot,expected)
    checked,legacy = read_plan(snapshot,expected,*(old[key] for key in (
        "subscriber","consumer_cursor","levels","source_id")))
    if any(record[key]!=value for key,value in checked.items()):
        raise ValueError("handoff plan no longer matches the verified legacy archive")
    levels = reviewed_levels(checked,record["review"])
    reads,info_sha256 = collect_info_reads(info_review,checked,levels)
    manifest = state_bundle.verify(bundle,bundle_sha256)
    router_label, = (label for label in manifest["files"] if label.startswith("router:"))
    labels = [router_label,"hermes:"+manager_instance,"hermes-host:"+manager_instance,"trigger:"+trigger_instance]
    if not set(labels)<=manifest["files"].keys():
        raise ValueError("snapshot must include the exact Router, handoff Trigger, Hermes and host ledgers")
    actor = "trigger/"+old["instance_id"]
    items = old["candidates"]
    outgoing = {item["message_id"] for item,level in zip(items,levels,strict=True) if level is not None}
    with ExitStack() as stack:
        ledgers = [
            stack.enter_context(closing(readonly(bundle/state_bundle.filename(label)))) for label in labels]
        router,_,host,trigger = ledgers
        require_ledger(host,"SELECT 1 FROM adapter_native_binding WHERE slot=1 AND session=?",(host_session,))
        require_ledger(trigger,"SELECT 1 FROM instance WHERE slot=1 AND id=?",(old["instance_id"],))
        stored = {row[0] for row in trigger.execute("SELECT id FROM archive")}
        submitted = {row[0] for row in router.execute("SELECT message_id FROM messages WHERE owner=?",(actor,))}
        if stored!=outgoing or not submitted<=outgoing:
            raise ValueError("handoff instance contains missing or additional events")
        states = [receipt_state(ledgers,item,row,level,actor,host_session,str(item["seq"]) in reads)
                  for item,row,level in zip(items,legacy,levels,strict=True)]
    if state_bundle.verify(bundle,bundle_sha256)!=manifest:
        raise ValueError("the new receipt bundle changed during audit")
    if trigger_archive.verify(snapshot,expected)!=legacy_manifest:
        raise ValueError("legacy archive changed during audit")
    result = []
    cursor = old["consumer_cursor"]
    gap = False
    for item,state in zip(items,states,strict=True):
        row = {key:item[key] for key in ("seq","legacy_event_id","message_id")}
        row.update(state=state,received_beyond_gap=gap and state in CONSUMED)
        result.append(row)
        gap = gap or state not in CONSUMED
        if not gap:
            cursor = item["seq"]
    safe = all(row["state"] in CONSUMED|PENDING and not row["received_beyond_gap"] for row in result)
    return {"subscriber":old["subscriber"],"levels":old["levels"],"initial_cursor":old["consumer_cursor"],
            "verified_cursor":cursor,"safe_to_stage":safe,"events":result,"plan_sha256":checked["plan_sha256"],
            "new_snapshot_sha256":bundle_sha256,"legacy_archive_sha256":expected,
            "reviewed_info_sequences":sorted(map(int,reads)),"info_review_sha256":info_sha256}


def stage_return(snapshot, expected, destination, **kwargs):
    status = audit(snapshot,expected,**kwargs)
    if not status["safe_to_stage"]:
        raise ValueError("an unknown receipt or a received item beyond a gap prevents legacy resume")
    with output_directory(destination) as folder:
        trigger_archive.restore(snapshot,expected,folder/"legacy")
        upto = status["verified_cursor"]
        request = {"subscriber":status["subscriber"],"up_to_id":upto} if upto>status["initial_cursor"] else None
        pending = [row["legacy_event_id"] for row in status["events"] if row["seq"]>upto]
        path = folder/"ack-replay.json"
        path.write_text(encode({**status,"request":request,"remaining_event_ids":pending}),encoding="utf-8")
        path.chmod(0o600)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation",required=True)
    read, write = operations.add_parser("audit"), operations.add_parser("stage")
    for command in (read,write):
        for key in ("snapshot","journal","bundle"):
            command.add_argument("--"+key,type=Path,required=True)
        command.add_argument("--manifest-sha256",dest="expected",required=True)
        for key in ("journal-sha256","bundle-sha256","trigger-instance","manager-instance","host-session"):
            command.add_argument("--"+key,required=True)
        command.add_argument("--info-review",type=Path)
    write.add_argument("--destination",type=Path,required=True)
    args = vars(parser.parse_args())
    try:
        result = {"audit":audit,"stage":stage_return}[args.pop("operation")](**args)
    except (ValueError,KeyError,TypeError) as exc:
        parser.exit(1,str(exc)+"\n")
    print(encode(result))


if __name__=="__main__":
    main()
