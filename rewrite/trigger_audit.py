"""Reconcile the Trigger and Router ledgers in a sealed rewrite snapshot."""
from contextlib import closing
from datetime import datetime
import hashlib
import json

from . import source_history
from .cutover import readonly
from .messages import valid_guid


def receipt_time(value):
    try:
        return isinstance(value,str) and datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def json_object(value):
    try:
        document = json.loads(value)
    except (TypeError,ValueError):
        return None
    return document if isinstance(document,dict) else None


def bytes_match(rows,size,sha256):
    offset = 0
    digest = hashlib.sha256()
    for start,chunk in rows:
        if start!=offset or not isinstance(chunk,bytes) or not chunk:
            return False
        digest.update(chunk)
        offset += len(chunk)
    return offset==size and digest.hexdigest()==sha256


def same_files(local,router,files,owner):
    for index,file in enumerate(files):
        if file["position"]!=index or not valid_guid(file["file_id"]):
            return False
        remote = router.execute("""SELECT u.* FROM uploads u JOIN attachment_grants g USING(attachment_id)
                                   WHERE u.attachment_id=? AND g.actor='hermes'""",(file["file_id"],)).fetchone()
        if not remote or (remote["owner"],remote["upload_id"],remote["name"],remote["size_bytes"],
                          remote["next_offset"],remote["sha256"]) != (
                              owner,file["upload_id"],file["name"],file["size"],file["size"],file["sha"]):
            return False
        pieces = local.execute("SELECT start,bytes FROM pieces WHERE id=? ORDER BY start",(file["id"],))
        chunks = router.execute("SELECT start,bytes FROM chunks WHERE upload_id=? ORDER BY start",(remote["upload_id"],))
        if not (bytes_match(pieces,file["size"],file["sha"]) and bytes_match(chunks,file["size"],file["sha"])):
            return False
    return True


def message_state(local,router,row,remote,owner):
    mid = row["id"]
    body = json_object(row["body"])
    if (not body or body.get("message_id")!=mid or not valid_guid(mid)
            or body.get("instance_id")!=owner.removeprefix("trigger/")):
        return "local_archive_mismatch"
    receipt = None
    if row["receipt"] is not None:
        receipt = json_object(row["receipt"])
        if (not receipt or receipt.get("message_id")!=mid
                or not receipt_time(receipt.get("received_at"))):
            return "local_receipt_invalid"
    if not remote:
        return "receipt_without_router_message" if receipt else "awaiting_router_message"
    if remote["owner"]!=owner:
        return "router_source_identity_mismatch"
    files = local.execute("SELECT * FROM files WHERE message_id=? ORDER BY position",(mid,)).fetchall()
    attachment_ids = [file["file_id"] for file in files]
    stored_ids = body.get("attachment_ids")
    if stored_ids!=(attachment_ids if receipt else []) or not same_files(local,router,files,owner):
        return "attachment_mismatch"
    if (json_object(remote["payload"])!={**body,"attachment_ids":attachment_ids}
            or remote["source"]!=body.get("trigger_name") or remote["level"]!=body.get("level")):
        return "router_payload_mismatch"
    if not receipt_time(remote["received_at"]) or (receipt and receipt["received_at"]!=remote["received_at"]):
        return "router_receipt_mismatch"
    return None if receipt else "awaiting_local_receipt"


def reconcile(router_path,triggers):
    with closing(readonly(router_path)) as router:
        remote_sources = {row["message_id"]:row["owner"]
                          for row in source_history.live_messages(router,trigger_only=True)}
        instances,matched,sources,issues = set(),set(),[],[]
        for component,path in sorted(triggers.items()):
            with closing(readonly(path)) as local:
                instance = local.execute("SELECT id FROM instance WHERE slot=1").fetchone()
                if not instance or not valid_guid(instance[0]) or instance[0] in instances:
                    raise ValueError("missing, invalid or duplicate Trigger identity: "+component)
                instance_id = instance[0]
                instances.add(instance_id)
                owner = "trigger/"+instance_id
                total = settled = 0
                for row in local.execute("SELECT id,body,receipt FROM archive ORDER BY id"):
                    mid = row["id"]
                    total += 1
                    remote = router.execute("SELECT * FROM messages WHERE message_id=?",(mid,)).fetchone()
                    if remote and mid not in remote_sources and remote["owner"].startswith("trigger/"):
                        # Recorded legacy history does not supply a live T04 receipt.
                        remote = None
                    if remote and remote["owner"]==owner:
                        matched.add(mid)
                    state = message_state(local,router,row,remote,owner)
                    if state:
                        issues.append({"code":state,"instance_id":instance_id,"message_id":mid})
                    else:
                        settled += 1
                sources.append({"component":component,"instance_id":instance_id,"archived":total,"settled":settled})
        for mid,owner in remote_sources.items():
            if mid not in matched:
                instance_id = owner.removeprefix("trigger/")
                code = "router_message_without_source_archive" if instance_id in instances else "router_source_missing"
                issues.append({"code":code,"instance_id":instance_id,"message_id":mid})
    return {"ready":not issues,"sources":sources,"issues":issues}
