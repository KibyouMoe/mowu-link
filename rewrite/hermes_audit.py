"""Reconcile Hermes queue receipts with the original native adapter ledger."""
from contextlib import closing
import hashlib

from . import attachments
from .cutover import readonly
from .storage import Fault, encode
from .trigger_audit import bytes_match, json_object


def native_payload(router, message_id, serialized):
    payload = json_object(serialized)
    if (not payload or payload.get("message_id")!=message_id or payload.get("level") not in ("Event","Notify")
            or not isinstance(payload.get("attachment_ids"),list)):
        return None,None,"hermes_queue_payload_unconfirmed"
    archived = router.execute("SELECT source,payload FROM messages WHERE message_id=?",(message_id,)).fetchone()
    body = json_object(archived["payload"]) if archived else None
    if not body or {**body,"source":archived["source"]}!=payload:
        return None,None,"hermes_router_archive_mismatch"
    files = []
    for file_id in payload["attachment_ids"]:
        if not isinstance(file_id,str) or not file_id:
            return None,None,"hermes_attachment_unverified"
        try:
            file = attachments.authorize(router,"hermes",file_id)
        except Fault:
            return None,None,"hermes_attachment_unverified"
        chunks = router.execute("SELECT start,bytes FROM chunks WHERE upload_id=? ORDER BY start",(file["upload_id"],))
        if file["next_offset"]!=file["size_bytes"] or not bytes_match(chunks,file["size_bytes"],file["sha256"]):
            return None,None,"hermes_attachment_unverified"
        files.append({key:file[key] for key in ("attachment_id","name","sha256","size_bytes")})
    expected = encode({**payload,"attachments":files}) if files else serialized
    return expected,files,None


def receipt_state(router,host,session,row):
    mid = row["message_id"]
    expected,files,issue = native_payload(router,mid,row["payload"])
    if issue:
        return "unverified",issue
    if row["host_payload"] is not None and row["host_payload"]!=expected:
        return "unverified","hermes_cached_host_payload_mismatch"
    intent = host.execute("SELECT * FROM intents WHERE delivery_id=?",(mid,)).fetchone()
    if not intent:
        if row["receipt"] is not None:
            return "unverified","hermes_native_receipt_mismatch"
        return "not_attempted",None
    digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    if not session or (intent["session"],intent["payload"],intent["digest"])!=(session,expected,digest):
        return "unverified","hermes_native_intent_mismatch"
    if intent["receipt"] is None and row["receipt"] is None and intent["native_id"] is None:
        return "intent_unconfirmed","hermes_native_receipt_unknown"
    proof = json_object(intent["receipt"])
    native_id = intent["native_id"]
    if (not proof or not isinstance(native_id,str) or not native_id
            or (proof.get("delivery_id"),proof.get("session"),proof.get("digest"),proof.get("native_id"))
                !=(mid,session,digest,native_id)
            or (files and proof.get("media_sha256")!=[file["sha256"] for file in files])):
        return "unverified","hermes_native_receipt_mismatch"
    if row["receipt"] is None:
        return "manager_commit_pending","hermes_native_receipt_pending"
    if json_object(row["receipt"])!=proof:
        return "unverified","hermes_native_receipt_mismatch"
    return "confirmed",None


def reconcile(manager,router_path,host_path):
    counts = dict.fromkeys(("not_attempted","intent_unconfirmed","manager_commit_pending","confirmed","unverified"),0)
    if host_path is None:
        queued = manager.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        counts["unverified"] = queued
        return counts,[{"code":"hermes_host_ledger_missing","count":queued}] if queued else []

    issues = []
    columns = {row[1] for row in manager.execute("PRAGMA table_info(queue)")}
    cached = "host_payload" if "host_payload" in columns else "NULL AS host_payload"
    with closing(readonly(host_path)) as host, closing(readonly(router_path)) as router:
        has_binding = host.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='adapter_native_binding'").fetchone()
        pin = host.execute("SELECT session FROM adapter_native_binding WHERE slot=1").fetchone() if has_binding else None
        session = pin[0] if pin else None
        queue_ids = set()
        for row in manager.execute(f"SELECT message_id,payload,receipt,{cached} FROM queue ORDER BY rowid"):
            mid = row["message_id"]
            queue_ids.add(mid)
            state,code = receipt_state(router,host,session,row)
            counts[state] += 1
            if code:
                issues.append({"message_id":mid,"code":code})
        for (mid,) in host.execute("SELECT delivery_id FROM intents ORDER BY delivery_id"):
            if mid not in queue_ids:
                issues.append({"message_id":mid,"code":"hermes_orphan_host_intent"})
    return counts,issues
