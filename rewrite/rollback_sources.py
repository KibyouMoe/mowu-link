"""Bind source return plans to the exact stopped Router being rolled back."""
from contextlib import closing
import hashlib
import json
from pathlib import Path

from . import feed_return, rollback_peers, state_bundle, trigger_return
from .cutover import digest, readonly
from .messages import valid_guid
from .storage import encode


PUBLISHED = {"native_received","reviewed_info_read","archive_only","not_sent_to_host","router_archived"}
INFO = {"reviewed_info_read","archive_only","router_archived"}
PLAN_FIELDS = {"bundle","bundle_sha256","manager_instance","host_session","sources"}


class SourceReturns:
    def __init__(self, plan_path, router_sha256):
        self.plan_path = plan_path
        self.base = plan_path.parent
        self.plan, self.sha256 = feed_return.load_file(plan_path)
        if (not isinstance(self.plan,dict) or self.plan.keys()!=PLAN_FIELDS
                or not isinstance(self.plan["sources"],list) or not self.plan["sources"]
                or any(not isinstance(self.plan[k],str) or not self.plan[k].strip() for k in PLAN_FIELDS-{"sources"})):
            raise ValueError("invalid source return plan")
        self.bundle = self.path(self.plan["bundle"])
        self.snapshot = state_bundle.verify(self.bundle,self.plan["bundle_sha256"])
        router, = (item for label,item in self.snapshot["files"].items() if label.startswith("router:"))
        if router["sha256"]!=router_sha256:
            raise ValueError("source proof and trial Router must use the same stopped database")
        self.events, self.frames, self.uploads = {},{},{}
        self.grants, self.operations = set(),{}
        self.actors, self.frame_sources, self.intake = set(),{},{}
        self.reports, self.consumer_hashes = {},{}

    def path(self, value):
        if not isinstance(value,str) or not value.strip():
            raise ValueError("invalid source return path")
        return self.base/Path(value)

    def verify_bundle(self):
        if state_bundle.verify(self.bundle,self.plan["bundle_sha256"])!=self.snapshot:
            raise ValueError("source return bundle changed during staging")

    def stage(self, db, folder):
        folder.mkdir(mode=0o700)
        frozen_plan = folder/"plan.json"
        state_bundle.copy_private(self.plan_path,frozen_plan)
        if digest(frozen_plan)!=self.sha256:
            raise ValueError("source return plan changed during staging")
        for role in ("hermes","hermes-host"):
            label = role+":"+self.plan["manager_instance"]
            path = self.copy_ledger(label,folder/(role+".sqlite3"))
            self.consumer_hashes[path.name] = self.snapshot["files"][label]["sha256"]
        manager = folder/"hermes.sqlite3"
        destinations, resume_targets = set(), set()
        for spec in self.plan["sources"]:
            if not isinstance(spec,dict) or not isinstance(spec.get("trigger_instance"),str):
                raise ValueError("invalid source return recipe")
            label = "trigger:"+spec["trigger_instance"]
            _, name = state_bundle.component(label)
            if label not in self.snapshot["files"] or name.casefold() in destinations:
                raise ValueError("source recipe must name a unique Trigger ledger in the bundle")
            destination = folder/name
            destinations.add(name.casefold())
            target, status, manifest = self.prepare(spec,destination)
            if target in resume_targets:
                raise ValueError("source return recipes repeat the same legacy restoration target")
            resume_targets.add(target)
            checksum = self.snapshot["files"][label]["sha256"]
            archive = destination/"trial-archive.sqlite3"
            self.copy_ledger(label,archive)
            with closing(readonly(archive)) as local, closing(readonly(manager)) as manager_db:
                count = self.bind(db,local,manager_db,status)
            self.reports[name] = {"kind":spec["kind"],"manifest":manifest,"manifest_sha256":digest(destination/manifest),
                                  "trial_archive_sha256":checksum,"published_messages":count}
        self.verify_bundle()

    def copy_ledger(self, label, path):
        if label not in self.snapshot["files"]:
            raise ValueError("source return bundle is missing a consumer ledger")
        if not path.exists():
            state_bundle.copy_private(self.bundle/state_bundle.filename(label),path)
        if digest(path)!=self.snapshot["files"][label]["sha256"]:
            raise ValueError("source return archive differs from the approved bundle")
        return path

    def prepare(self, spec, destination):
        common = {"bundle":self.bundle,"manager_instance":self.plan["manager_instance"],
                  "host_session":self.plan["host_session"]}
        keys = spec.keys()-{"kind","trigger_instance"}
        if spec.get("kind")=="feed" and keys=={"legacy_cursor","review","review_sha256"}:
            cursor = self.path(spec["legacy_cursor"])
            status = feed_return.stage(destination,expected=self.plan["bundle_sha256"],legacy_cursor=cursor,
                                       review=self.path(spec["review"]),feed_instance=spec["trigger_instance"],**common)
            if status["review_sha256"]!=spec["review_sha256"]:
                raise ValueError("collector review differs from the approved recipe")
            return ("feed",cursor.resolve()),status,"return.json"
        required = {"legacy_snapshot","legacy_snapshot_sha256","journal","journal_sha256"}
        info = {"info_review","info_review_sha256"}
        if spec.get("kind")=="subscription" and keys in (required,required|info):
            status = trigger_return.stage_return(
                self.path(spec["legacy_snapshot"]),spec["legacy_snapshot_sha256"],destination,
                journal=self.path(spec["journal"]),journal_sha256=spec["journal_sha256"],
                bundle_sha256=self.plan["bundle_sha256"],trigger_instance=spec["trigger_instance"],
                info_review=self.path(spec["info_review"]) if "info_review" in spec else None,**common)
            if status["info_review_sha256"]!=spec.get("info_review_sha256"):
                raise ValueError("Info review differs from the approved recipe")
            return ("subscription",spec["legacy_snapshot_sha256"]),status,"ack-replay.json"
        raise ValueError("invalid source return recipe")

    def bind(self, db, local, manager, status):
        instance, = local.execute("SELECT id FROM instance WHERE slot=1").fetchone()
        if not valid_guid(instance) or status.get("instance_id",instance)!=instance:
            raise ValueError("confirmed source instance does not match the trial archive")
        actor = "trigger/"+instance
        if actor in self.actors:
            raise ValueError("source return recipes repeat the same Trigger instance")
        self.actors.add(actor)
        staged = {}
        for entry in status["events"]:
            mid, state = entry["message_id"],entry["state"]
            if state in PUBLISHED:
                if not valid_guid(mid) or mid in staged or mid in self.events:
                    raise ValueError("invalid or repeated source message identifier")
                staged[mid] = state
        rows = db.execute("SELECT * FROM messages WHERE owner=?",(actor,)).fetchall()
        if {row["message_id"] for row in rows}!=staged.keys():
            raise ValueError("reviewed source return does not cover exactly its published Router messages")
        for row in rows:
            mid = row["message_id"]
            state = staged[mid]
            original = local.execute("SELECT body,receipt FROM archive WHERE id=?",(mid,)).fetchone()
            receipt = {"message_id":mid,"received_at":row["received_at"],"duplicate":False}
            if not original or original["body"]!=row["payload"] or original["receipt"]!=encode(receipt):
                raise ValueError("source T04 does not match the frozen Router")
            body = json.loads(row["payload"])
            self.events[mid] = actor
            self.operations[actor,"message-"+mid] = body,receipt
            if state in INFO:
                if row["level"]!="Info":
                    raise ValueError("approved Info is not an Info in the frozen Router")
            else:
                self.bind_frame(db,row,body,state)
                notice = manager.execute("SELECT first_try,platform_id,error FROM manager_notices WHERE message_id=?",
                                         (mid,)).fetchone()
                if state=="native_received" and notice and (
                        notice["first_try"] is None or not str(notice["platform_id"]).startswith("om_")
                        or notice["error"] is not None):
                    raise ValueError("source Feishu notice needs reconciliation before combined rollback")
            media = local.execute("SELECT * FROM files WHERE message_id=? ORDER BY position",(mid,)).fetchall()
            if [file["file_id"] for file in media]!=body["attachment_ids"]:
                raise ValueError("approved source attachment order differs")
            for file in media:
                self.bind_file(db,local,file,actor)
        return len(rows)

    def bind_frame(self, db, row, body, state):
        frames = db.execute("""SELECT * FROM deliveries WHERE kind='message.deliver' AND consumer='hermes'
                               AND json_extract(payload,'$.message_id')=?""",(row["message_id"],)).fetchall()
        if (len(frames)!=1 or row["level"] not in ("Event","Notify")
                or frames[0]["payload"]!=encode({**body,"source":row["source"]})
                or frames[0]["ack"] not in (0,1) or state=="native_received" and frames[0]["ack"]!=1):
            raise ValueError("approved source delivery differs from the frozen Router")
        frame = frames[0]
        if not valid_guid(frame["delivery_id"]):
            raise ValueError("approved source has an invalid delivery identifier")
        self.frames[frame["delivery_id"]] = frame["consumer"],frame["cursor"],frame["ack"]
        self.frame_sources[frame["delivery_id"]] = row["message_id"]

    def bind_file(self, db, local, file, actor):
        fid = file["file_id"]
        row = db.execute("SELECT * FROM uploads WHERE attachment_id=? AND owner=?",(fid,actor)).fetchone()
        if (not row or row["upload_id"]!=file["upload_id"]
                or (row["name"],row["size_bytes"],row["sha256"])!=(file["name"],file["size"],file["sha"])
                or not db.execute("SELECT 1 FROM attachment_grants WHERE attachment_id=? AND actor='hermes'",(fid,)).fetchone()):
            raise ValueError("approved source attachment differs from the frozen Router")
        if row["upload_id"] not in self.uploads:
            rollback_peers.copy_chunks(db,row,lambda _offset,_data:None)
            self.uploads[row["upload_id"]] = row
            self.grants.add((fid,"hermes"))
        checksum,offset = hashlib.sha256(),0
        for start,data in local.execute("SELECT start,bytes FROM pieces WHERE id=? ORDER BY start",(file["id"],)):
            if start!=offset or not data:
                raise ValueError("source archive attachment contains a gap")
            checksum.update(data)
            offset += len(data)
        if offset!=file["size"] or checksum.hexdigest()!=file["sha"]:
            raise ValueError("source archive attachment checksum differs")

    def record(self, actor, key, body, reply, seq):
        if reply.get("duplicate") is not False or self.operations.pop((actor,key),None)!=(body,reply):
            raise ValueError("trial contains a T04 outside the approved source returns")
        self.intake[body["message_id"]] = seq

    def finish(self, acked):
        if self.operations:
            raise ValueError("approved source T04 operations are missing from the Router ledger")
        for delivery_id,(_,_,confirmed) in self.frames.items():
            if confirmed and acked.get(delivery_id,0)<=self.intake[self.frame_sources[delivery_id]]:
                raise ValueError("source transport receipt does not follow its T04")
