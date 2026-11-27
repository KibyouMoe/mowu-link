"""Retain reviewed trial peer messages without handing their contents to the legacy Router."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from . import attachments, legacy_history, legacy_peers, peer_files, peers
from .cutover import digest, readonly
from .messages import valid_guid
from .rollback_lifecycle import utc_time
from .storage import Fault, encode, require
from .wire import serve_authenticated


NAME = "peer-history.db"
TABLES = {"peer_messages", "peer_files", "peer_host_receipts"}
ARCHIVE_TABLES = ("history_sources", "history_peers", "history_peer_text", "history_peer_files",
                  "history_peer_proofs", "uploads", "chunks", "attachment_grants")


def review_rows(db):
    return [dict(row) for row in db.execute("""
        SELECT p.message_id,p.sender,p.recipient,p.reply_to,d.delivery_id,d.ack AS worker_received,
               r.receipt_id AS host_receipt_id
        FROM peer_messages p LEFT JOIN peer_host_receipts r USING(message_id)
        LEFT JOIN deliveries d ON d.kind='peer.message' AND d.consumer='worker/'||p.recipient
          AND json_extract(d.payload,'$.message_id')=p.message_id
        ORDER BY p.rowid
    """)]


def reviewed_peers(db, deltas):
    proofs, frames, uploads, grants = {}, {}, {}, set()
    file_count, prior_frame = 0, 0
    for row in db.execute("SELECT rowid AS seq,* FROM peer_messages ORDER BY rowid"):
        mid, sender, recipient = row["message_id"], row["sender"], row["recipient"]
        try:
            observation = peers.status(db,sender,mid)
        except Fault as error:
            raise ValueError("trial peer has no confirmed delivery") from error
        did, receipt = observation["delivery_id"], observation["host_receipt_id"]
        native = db.execute("SELECT * FROM peer_host_receipts WHERE message_id=?",(mid,)).fetchone()
        frame = db.execute("SELECT rowid AS seq,* FROM deliveries WHERE delivery_id=?",(did,)).fetchone()
        if (not all(valid_guid(v) for v in (mid,did,receipt)) or not native or native["delivery_id"]!=did
                or not frame or frame["ack"]!=1 or frame["consumer"]!="worker/"+recipient or frame["seq"]<=prior_frame
                or sender==recipient or utc_time(native["recorded_at"])<utc_time(row["accepted_at"])):
            raise ValueError("trial peer needs a confirmed recipient and native host receipt")
        parent = row["reply_to"]
        if parent is not None:
            prior = proofs.get(parent)
            if not valid_guid(parent) or not peers.reply_parent(db,parent,sender,recipient) or (
                    not prior and db.execute("SELECT 1 FROM peer_messages WHERE message_id=?",(parent,)).fetchone()):
                raise ValueError("trial peer has an invalid reply relationship")
        linked = db.execute("SELECT position,attachment_id,kind FROM peer_files WHERE message_id=? ORDER BY position",
                            (mid,)).fetchall()
        ids, kinds = peer_files.request_files({"attachment_ids":[item["attachment_id"] for item in linked],
                                             "attachment_kinds":[item["kind"] for item in linked]})
        if [item["position"] for item in linked]!=list(range(len(ids))):
            raise ValueError("trial peer attachment order has a gap")
        owner, reader = "worker/"+sender, "worker/"+recipient
        files = peer_files.file_list(db,owner,ids,kinds,owned=True)
        file_count += len(files)
        for item in files:
            fid = item["attachment_id"]
            upload = attachments.authorize(db,reader,fid)
            grants.add((fid,reader))
            uploads[upload["upload_id"]] = upload
        payload = {"message_id":mid,"from_worker":sender,"to_worker":recipient,"text":row["text"],"reply_to":parent}
        if files:
            payload["attachments"] = files
        if frame["kind"]!="peer.message" or frame["payload"]!=encode(payload):
            raise ValueError("trial peer delivery does not match the original message")
        frames[did] = frame["consumer"],frame["cursor"]
        prior_frame = frame["seq"]
        proofs[mid] = {"row":dict(row),"observation":observation,"files":files}
    for table, count in (("peer_messages",len(proofs)),("peer_files",file_count),("peer_host_receipts",len(proofs))):
        if deltas.get(table,{}).get("trial_only",0)!=count:
            raise ValueError("trial has unrelated peer data")
    return proofs,frames,uploads,grants


class ReceiptLedger:
    def __init__(self, proofs):
        self.proofs = proofs
        self.sent = {}
        self.confirmed = set()

    def match(self, seq, actor, parts, body, reply, sessions, acked):
        if len(parts)==5 and parts[:3]==["","v1","peers"] and parts[4]=="messages":
            mid = body.get("message_id")
            proof = self.proofs.get(mid)
            if not proof:
                raise ValueError("trial has an unreviewed peer send")
            item = proof["row"]
            ids, kinds = peer_files.request_files(body)
            if (mid in self.sent or actor!="worker/"+item["sender"] or parts[3]!=item["recipient"]
                    or actor not in sessions or body.get("session_id")!=sessions[actor]
                    or body.get("text")!=item["text"] or body.get("reply_to")!=item["reply_to"]
                    or list(zip(ids,kinds))!=[(f["attachment_id"],f["kind"]) for f in proof["files"]]
                    or reply!={"status":"accepted","message_id":mid,"accepted_at":item["accepted_at"]}):
                raise ValueError("trial peer send receipt does not match its message")
            self.sent[mid] = seq
            return True
        if len(parts)==6 and parts[:4]==["","v1","peers","messages"] and parts[5]=="receipt":
            mid = parts[4]
            proof = self.proofs.get(mid)
            if not proof:
                raise ValueError("trial has an unreviewed native peer receipt")
            saved = proof["observation"]
            if (mid not in self.sent or mid in self.confirmed or actor!="worker/"+saved["recipient"]
                    or acked.get(saved["delivery_id"],0)<=self.sent[mid] or actor not in sessions
                    or body!={"session_id":sessions[actor],"delivery_id":saved["delivery_id"],
                              "receipt_id":saved["host_receipt_id"]} or reply!=saved):
                raise ValueError("trial peer native receipt does not match its acknowledged delivery")
            self.confirmed.add(mid)
            return True
        return False

    def verify(self):
        if self.confirmed!=self.proofs.keys():
            raise ValueError("trial peer operation ledger is incomplete")


def copy_chunks(db, row, write):
    checksum, offset = hashlib.sha256(), 0
    for start, data in db.execute("SELECT start,bytes FROM chunks WHERE upload_id=? ORDER BY start",(row["upload_id"],)):
        if start!=offset or not data:
            raise ValueError("reviewed attachment contains a gap")
        write(start,data)
        checksum.update(data)
        offset += len(data)
    if offset!=row["size_bytes"] or offset!=row["next_offset"] or checksum.hexdigest()!=row["sha256"]:
        raise ValueError("reviewed attachment digest mismatch")


def freeze(source, expected, destination):
    if not isinstance(expected,str) or not re.fullmatch(r"[a-f0-9]{64}",expected):
        raise ValueError("expected a trusted peer archive SHA-256")
    with closing(readonly(source)), source.open("rb") as original, open(
            destination,"xb",opener=lambda name,flags:os.open(name,flags,0o600)) as output:
        shutil.copyfileobj(original,output)
    if digest(destination)!=expected:
        raise ValueError("peer archive SHA-256 mismatch")


def archive_from_trial(trial, parent, expected):
    if expected is None:
        return None
    target = parent/"private-review"/NAME
    target.parent.mkdir(mode=0o700,exist_ok=True)
    freeze(trial/NAME,expected,target)
    return target


def import_archive(target, path):
    with closing(readonly(path)) as source:
        if [r[0] for r in source.execute("PRAGMA integrity_check")]!=["ok"]:
            raise ValueError("peer archive database integrity check failed")
        target.execute(legacy_peers.TRIAL_SCHEMA)
        if any(target.execute("SELECT 1 FROM history_peers WHERE message_id=?",(r[0],)).fetchone()
               for r in source.execute("SELECT message_id FROM history_peers")):
            raise ValueError("peer archive message ID already exists in history")
        for table in ARCHIVE_TABLES:
            rows = source.execute(f"SELECT * FROM {table} ORDER BY rowid")
            values = ",".join("?" for _ in rows.description)
            target.executemany(f"INSERT INTO {table} VALUES ({values})",rows)


def write_archive(db, archive, previous, source_id, trial_sha, proofs, uploads):
    if not proofs:
        if previous is None:
            return None
        previous.rename(archive)
        return digest(archive)
    fd = os.open(archive,os.O_CREAT | os.O_EXCL | os.O_WRONLY,0o600)
    os.close(fd)
    with closing(sqlite3.connect(archive)) as target, target:
        target.executescript(legacy_history.SCHEMA+legacy_peers.SCHEMA+legacy_peers.TRIAL_SCHEMA+attachments.SCHEMA)
        if previous is not None:
            import_archive(target,previous)
        if proofs:
            src = str(uuid5(UUID(source_id),"peer-trial/"+trial_sha))
            target.execute("INSERT INTO history_sources VALUES (?,?)",(src,trial_sha))
            for uid,row in uploads.items():
                target.execute("INSERT INTO uploads VALUES (?,?,?,?,?,?,?)",
                               (uid,"history/"+src,row["name"],row["size_bytes"],row["sha256"],row["next_offset"],
                                row["attachment_id"]))
                copy_chunks(db,row,lambda start,data:target.execute("INSERT INTO chunks VALUES (?,?,?)",(uid,start,data)))
            readers = {(item["attachment_id"],"worker/"+proof["row"][actor])
                       for proof in proofs.values() for item in proof["files"]
                       for actor in ("sender","recipient")}
            target.executemany("INSERT INTO attachment_grants VALUES (?,?)",sorted(readers))
            for mid, proof in proofs.items():
                row, observed = proof["row"], proof["observation"]
                text = row["text"]
                parts = [text[i:i+16384] for i in range(0,len(text),16384)]
                target.execute("INSERT INTO history_peers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (src,mid,row["seq"],row["sender"],row["recipient"],"worker/"+row["sender"],
                                "worker/"+row["recipient"],row["reply_to"],None,utc_time(row["accepted_at"]).timestamp(),
                                None,None,len(parts)))
                target.executemany("INSERT INTO history_peer_text VALUES (?,?,?,?)",
                                   ((src,mid,position,part) for position,part in enumerate(parts)))
                target.executemany("INSERT INTO history_peer_files VALUES (?,?,?,?,?,NULL)",
                                   ((src,mid,pos,item["attachment_id"],item["name"])
                                    for pos,item in enumerate(proof["files"])))
                target.execute("INSERT INTO history_peer_proofs VALUES (?,?,?,?,?,?,?)",
                               (src,mid,observed["delivery_id"],observed["host_receipt_id"],row["accepted_at"],
                                observed["host_receipt_recorded_at"],encode([f["kind"] for f in proof["files"]])))
    return digest(archive)


class ReadOnlyArchive:
    def __init__(self, path):
        self.path = path

    def transaction(self):
        return closing(readonly(self.path))

    def handle(self, actor, method, path, _body, headers):
        require(method=="GET",405,"read_only_history")
        url = urlsplit(path)
        require(url.path.startswith("/v1/"),404,"unknown_route")
        parts = url.path.removeprefix("/v1/").split("/")
        if legacy_peers.history_route(parts):
            return 200,"application/json",legacy_peers.read(self,actor,method,parts,url.query)
        if len(parts)==2 and parts[0]=="attachments" and not url.query:
            with self.transaction() as db:
                file = attachments.authorize(db,actor,parts[1])
            download = attachments.Download(self,file,headers.get("Range"))
            return (206 if "Content-Range" in download.headers else 200),"application/octet-stream",download
        raise Fault(404,"unknown_route")


@contextmanager
def listen(path, sha, identities, address, tls_context):
    with tempfile.TemporaryDirectory(prefix="mowulink-peer-archive-") as temp:
        frozen = Path(temp)/NAME
        freeze(path,sha,frozen)
        with serve_authenticated(identities,address,tls_context,ReadOnlyArchive(frozen).handle) as server:
            yield server
