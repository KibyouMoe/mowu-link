"""Audit one legacy Worker subscription using frozen Router, Trigger and Worker records."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re

from . import cutover, trigger_archive
from .messages import valid_guid
from .storage import encode


BOUNDARY = "legacy-subscription-boundary.json"


def checked_object(path, expected):
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular offline JSON file")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected:
        raise ValueError("offline JSON digest mismatch")
    document = json.loads(raw)
    if not isinstance(document,dict):
        raise ValueError("expected an offline JSON object")
    return document


def sequence(db):
    row = db.execute("SELECT seq FROM sqlite_sequence WHERE name='subscriptions'").fetchone()
    last = row[0] if row else 0
    if type(last) is not int or last<0:
        raise ValueError("invalid legacy subscription allocation counter")
    return last


def before_stop(snapshot, expected):
    manifest = cutover.verified(snapshot)
    if manifest["legacy_db_sha256"]!=expected:
        raise ValueError("pre-stop Router snapshot differs from the approved digest")
    with closing(cutover.readonly(snapshot/"legacy.db")) as old:
        proof = {"pre_stop_sha256":expected,"sequence":sequence(old),
                 "active":manifest["blockers"]["active_subscriptions"]}
    if cutover.digest(snapshot/"legacy.db")!=expected:
        raise ValueError("pre-stop Router snapshot changed during subscription audit")
    return proof


def require_boundary(db, stopped_sha, proof):
    current = sequence(db)
    if proof is None:
        if current:
            raise ValueError("legacy subscription history requires a verified pre-stop Router snapshot")
        return
    if (set(proof)!={"pre_stop_sha256","stopped_sha256","sequence","active"}
            or not isinstance(proof["pre_stop_sha256"],str)
            or not re.fullmatch(r"[0-9a-f]{64}",proof["pre_stop_sha256"])
            or proof["stopped_sha256"]!=stopped_sha
            or type(proof["sequence"]) is not int or type(proof["active"]) is not int or proof["active"]<0):
        raise ValueError("pre-stop subscription boundary does not match this stopped Router")
    if proof["active"]:
        raise ValueError("active_subscriptions in the pre-stop Router require reconciliation")
    if proof["sequence"]!=current:
        raise ValueError("legacy subscription allocation changed after the pre-stop snapshot")


def message_id(source, sequence):
    """Matches subscriptionMessageId in the old Codex and DSH adapters."""
    fingerprint = hashlib.sha256(f"subscription:{source}:{sequence}".encode()).hexdigest()
    return f"{fingerprint[:8]}-{fingerprint[8:12]}-4{fingerprint[13:16]}-8{fingerprint[17:20]}-{fingerprint[20:32]}"


def json_text_hash(text):
    return hashlib.sha256(json.dumps(text,ensure_ascii=False).encode()).hexdigest()


def observation(state, receipt, mid, event):
    if receipt is None:
        return {"state":"receipt_missing"}
    if not isinstance(receipt,dict):
        return {"state":"receipt_mismatch"}
    request = receipt.get("request")
    if not isinstance(request,dict):
        return {"state":"receipt_mismatch"}
    source,kind,sequence = event["source"].strip() or "(unknown)",event["kind"],event["id"]
    text = f"[订阅事件 source={source} kind={kind} id={sequence}]\n{event['text']}"
    fields = {"messageId":mid,"source":"hermes","origin":"subscription","delivery":"steer","text":text}
    if any(request.get(key)!=value for key,value in fields.items()):
        return {"state":"receipt_mismatch"}
    thread = request.get("targetThreadId")
    dispatch = receipt.get("nativeDispatch")
    if dispatch is None:
        return {"state":"readback_unknown"}
    if (not isinstance(dispatch,dict) or not isinstance(thread,str) or not thread
            or (receipt.get("nativeTargetThreadId"),dispatch.get("threadId"),receipt.get("nativeInputHash"))
               !=(thread,thread,json_text_hash(text))):
        return {"state":"receipt_mismatch"}
    turn = receipt.get("nativeTurnId")
    if dispatch.get("status")!="accepted" or receipt.get("pending") or not isinstance(turn,str) or not turn:
        return {"state":"readback_unknown"}
    session = state["sessions"].get(thread)
    if not isinstance(session,dict) or session.get("threadId")!=thread:
        return {"state":"readback_unknown"}
    inputs, seen_items, turns = (session.get(field,{}) for field in ("inputMessages","seen","turnMessages"))
    if not all(isinstance(index,dict) for index in (inputs,seen_items,turns)):
        return {"state":"receipt_mismatch"}
    mapped = [item for item,owner in inputs.items() if owner==mid]
    if not mapped:
        return {"state":"readback_unknown"}
    if len(mapped)!=1 or not mapped[0].startswith(turn+"/") or receipt.get("transcriptNativeKey",mapped[0])!=mapped[0]:
        return {"state":"receipt_mismatch"}
    key = mapped[0]
    seen = seen_items.get(key)
    turn_messages = turns.get(turn,[])
    if not isinstance(seen,dict) or not isinstance(turn_messages,list) or mid not in turn_messages:
        return {"state":"readback_unknown"}
    if seen.get("hash")!=receipt["nativeInputHash"]:
        return {"state":"receipt_mismatch"}
    return {"state":"input_observed","session_id":thread,"turn_id":turn,"item_key":key}


def receipt_for(state, root, checksums, mid):
    current = state["receipts"].get(mid)
    if mid not in checksums:
        return current,None
    if root.is_symlink() or not root.is_dir():
        raise ValueError("expected the frozen receipt-archive directory beside its index")
    saved = checked_object(root/(mid+".json"),checksums[mid])
    return ((current if current is not None else saved),
            "receipt_archive_conflict" if current is not None and current!=saved else None)


def subscription(router, sub_id, worker):
    saved = router.execute("""SELECT s.*,c.last_id AS router_cursor FROM subscriptions s
                            LEFT JOIN subscription_cursors c ON c.trigger_name=s.trigger_id
                            WHERE s.active=1 AND s.id=? AND s.worker=?""",(sub_id,worker)).fetchone()
    if not saved:
        raise ValueError("active legacy subscription does not match the requested worker")
    base = "router:"+worker
    if (saved["trigger_id"] not in (base,f"{base}:{sub_id}")
            or router.execute("SELECT COUNT(*) FROM subscriptions WHERE active=1 AND trigger_id=?",
                              (saved["trigger_id"],)).fetchone()[0]!=1):
        raise ValueError("legacy Trigger subscriber is missing or shared")
    kinds = json.loads(saved["kinds"] or "[]")
    if not isinstance(kinds,list) or any(not isinstance(kind,str) or not kind for kind in kinds):
        raise ValueError("invalid legacy Worker subscription filter")
    return saved,kinds


def audit(router_snapshot,router_sha256,trigger_snapshot,trigger_sha256,worker,subscription_id,
          worker_state,worker_state_sha256,*,receipt_index=None,receipt_index_sha256=None):
    if (receipt_index is None)!=(receipt_index_sha256 is None):
        raise ValueError("supply both the receipt index and its trusted SHA-256")
    verified = cutover.verified(router_snapshot)
    if verified["legacy_db_sha256"]!=router_sha256:
        raise ValueError("legacy Router snapshot differs from the approved digest")
    trigger_manifest = trigger_archive.verify(trigger_snapshot,trigger_sha256)
    state = checked_object(worker_state,worker_state_sha256)
    if (type(state.get("version")) is not int or state["version"]!=1 or not isinstance(state.get("sessions"),dict)
            or not isinstance(state.get("receipts"),dict)):
        raise ValueError("unsupported legacy Worker receipt ledger")
    checksums = checked_object(receipt_index,receipt_index_sha256) if receipt_index else {}
    archive = receipt_index.parent/"receipt-archive" if receipt_index else None
    if any(not valid_guid(mid) or not isinstance(sha,str)
           or not re.fullmatch(r"[0-9a-f]{64}",sha) for mid,sha in checksums.items()):
        raise ValueError("invalid frozen receipt index")

    with closing(cutover.readonly(router_snapshot/"legacy.db")) as router:
        saved,kinds = subscription(router,subscription_id,worker)
    name,baseline,router_cursor = (saved[k] for k in ("trigger_id","trigger_since","router_cursor"))
    with closing(cutover.readonly(trigger_snapshot/trigger_archive.DATABASE)) as trigger:
        view = trigger.execute("""SELECT s.since,s.status,c.last_id FROM subscriptions s
                                 LEFT JOIN cursors c ON c.subscriber=s.name WHERE s.name=?""",(name,)).fetchone()
        head = trigger.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        if not view or view["since"]!=baseline or type(baseline) is not int or type(router_cursor) is not int:
            raise ValueError("legacy Router and Trigger subscription baselines disagree")
        trigger_cursor = baseline if view["last_id"] is None else view["last_id"]
        if type(trigger_cursor) is not int or not 0<=baseline<=min(router_cursor,trigger_cursor)<=max(router_cursor,trigger_cursor)<=head:
            raise ValueError("legacy subscription cursors lie outside the frozen event range")
        rows = trigger.execute("""SELECT e.id,e.event_id,e.source,e.kind,e.level,e.text,
                                EXISTS(SELECT 1 FROM receipts r WHERE r.event_id=e.event_id
                                       AND r.subscriber=?) AS trigger_ack
                                FROM events e WHERE e.id>? AND e.source=?
                                  AND COALESCE(e.level,'info') IN ('event','notify','info')
                                  AND COALESCE(e.disposition,'delivered')!='dropped'
                                ORDER BY e.id""",(name,baseline,saved["source"]))
        events = []
        for event in rows:
            if kinds and event["kind"] not in kinds:
                continue
            mid = message_id(event["source"].strip() or "(unknown)",event["id"])
            receipt,conflict = receipt_for(state,archive,checksums,mid)
            proof = {"state":conflict} if conflict else observation(state,receipt,mid,event)
            events.append({"sequence":event["id"],"event_id":event["event_id"],"message_id":mid,
                           "kind":event["kind"],"level":event["level"] or "info",
                           "router_cursor_passed":event["id"]<=router_cursor,
                           "trigger_ack":bool(event["trigger_ack"]),"native":proof})
    if cutover.verified(router_snapshot)!=verified or trigger_archive.verify(trigger_snapshot,trigger_sha256)!=trigger_manifest:
        raise ValueError("offline subscription snapshots changed during audit")
    return {"worker":worker,"subscription_id":subscription_id,"subscriber":name,
            "source":saved["source"],"kinds":kinds,"trigger_status":view["status"],"since":baseline,
            "router_cursor":router_cursor,"trigger_cursor":trigger_cursor,"source_head":head,
            "router_sha256":router_sha256,"trigger_sha256":trigger_sha256,
            "worker_state_sha256":worker_state_sha256,"receipt_index_sha256":receipt_index_sha256,"events":events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("router-snapshot","trigger-snapshot","worker-state"):
        parser.add_argument("--"+field,type=Path,required=True)
    for field in ("router-sha256","trigger-sha256","worker","worker-state-sha256"):
        parser.add_argument("--"+field,required=True)
    parser.add_argument("--subscription-id",type=int,required=True)
    parser.add_argument("--receipt-index",type=Path)
    parser.add_argument("--receipt-index-sha256")
    try:
        print(encode(audit(**vars(parser.parse_args()))))
    except (OSError,ValueError) as exc:
        parser.exit(1,str(exc)+"\n")


if __name__=="__main__":
    main()
