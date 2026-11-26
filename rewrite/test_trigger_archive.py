from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from .cutover import digest
from .devloop import FixtureHost, Loop
from .legacy_test_fixture import source_for
from .storage import guid
from .test_mainline import Clock
from .trigger import Trigger
from .trigger_archive import DATABASE, capture, event, restore, verify
from .trigger_handoff import HANDOFF_DB, plan, stage


class TriggerArchive(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.root/"old.sqlite3"
        self.snapshot = self.root/"prepared"
        self.restored = self.root/"restored"
        self.blocked = self.root/"blocked.jsonl"
        self.blocked.write_text('{"event_id":"blocked","cause":"fixture"}\n',encoding="utf-8")
        self.old("""
store = TriggerStore(sys.argv[1])
store.ensure_subscription('fixture-live',0,3600)
store.ensure_subscription('fixture-closed',0,3600)
store.close_subscription('fixture-closed')
store.add_event('email','旧原文',level='notify',event_id='heard',payload={'id':'原始标识'})
store.ack('fixture-live',1)
store.add_event('mc_chat','continue',level='event',priority=0,event_id='pending')
store.add_event('email','downgraded',level='info',event_id='info',disposition='downgraded')
store.add_event('email','drop',level='notify',event_id='blocked',disposition='dropped')
store.add_event('email','archive-only',level='log',event_id='kept')
store.close()
""",self.source)

    def old(self,body,database):
        code = "from server import TriggerStore\nimport json,sys\n"+body
        module = source_for("trigger")
        result = subprocess.run([sys.executable,"-c",code,str(database)],cwd=self.root,check=True,
                                env={"HOME":str(self.root),"PATH":os.defpath,"PYTHONPATH":str(module),
                                     "PYTHONDONTWRITEBYTECODE":"1"},capture_output=True,text=True)
        return result.stdout

    def test_archive_copies_actual_legacy_events_and_receipts_without_replaying_them(self):
        original = digest(self.source)
        metadata = capture(self.source,self.snapshot,blocked=self.blocked)
        sha = metadata["manifest_sha256"]
        counts = metadata["inventory"]
        self.assertEqual(counts["events"],5)
        self.assertEqual(counts["subscriptions"],{"active":1,"closed":1})
        self.assertEqual(counts["active_with_unacknowledged_visible_events"],1)
        self.assertEqual(counts["receipts"],1)
        self.assertEqual(counts["dispositions"],{"delivered":3,"downgraded":1,"dropped":1})
        self.assertEqual(verify(self.snapshot,sha)["inventory"],counts)
        old = event(self.snapshot,sha,"heard")
        self.assertEqual((old["text"],json.loads(old["payload_json"])),("旧原文",{"id":"原始标识"}))
        self.assertEqual(event(self.snapshot,sha,"blocked")["disposition"],"dropped")
        self.assertEqual(event(self.snapshot,sha,"kept")["level"],"log")
        self.assertEqual(restore(self.snapshot,sha,self.restored)["files"],2)
        self.assertEqual(digest(self.restored/DATABASE),original)
        self.assertEqual((self.restored/"blocked.jsonl").read_bytes(),self.blocked.read_bytes())
        readback = json.loads(self.old("""
store = TriggerStore(sys.argv[1])
print(json.dumps({
    'unread': [item['event_id'] for item in store.list_events('fixture-live', None)],
    'closed': store.get_subscription('fixture-closed')['status'],
    'count': store.count(),
}))
store.close()
""",self.restored/DATABASE))
        self.assertEqual(readback,{"unread":["pending","info"],"closed":"closed","count":5})
        self.assertEqual(digest(self.source),original)

    def test_rejects_active_journal_and_tampered_archive_member(self):
        db = sqlite3.connect(self.source)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE events SET priority=1")
        with self.assertRaisesRegex(ValueError,"live journal"):
            capture(self.source,self.snapshot)
        db.rollback()
        db.close()
        self.assertFalse(self.snapshot.exists())
        result = capture(self.source,self.snapshot,blocked=self.blocked)
        (self.snapshot/"blocked.jsonl").write_text("changed",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"bytes differ"):
            restore(self.snapshot,result["manifest_sha256"],self.restored)
        self.assertFalse(self.restored.exists())

    def test_reviewed_unread_handoff_retries_without_replay_and_preserves_attachments(self):
        files = self.root/"legacy-router-files"
        sample = b"GIF89a"+bytes(range(256))*1300
        checksum = hashlib.sha256(sample).hexdigest()
        self.old(f"""
sys.path.insert(0,{str(source_for("router"))!r})
from attachments import AttachmentStore
from io import BytesIO
sample = b"GIF89a"+bytes(range(256))*1300
attachment = AttachmentStore({str(files)!r}).put(BytesIO(sample),len(sample),{checksum!r})
store = TriggerStore(sys.argv[1])
store.add_event("email","Original receipt lost",priority=1,level="notify",event_id="already-seen")
store.add_event("router","原附件\\n"+"新↵"*300,priority=1,level="notify",event_id="with-files",
                payload={{"attachments":[attachment],"secret_old_metadata":"not published"}})
store.add_event("mc_chat","Old level will not wake",priority=3,level="event",event_id="quiet")
store.close()
""",self.source)
        original = digest(self.source)
        snap = capture(self.source,self.snapshot)["manifest_sha256"]
        namespace = guid()
        levels = ["event","notify","info"]
        with self.assertRaisesRegex(ValueError,"consumer cursor and legacy receipt ledger disagree"):
            plan(self.snapshot,snap,"fixture-live",0,levels,namespace)
        with self.assertRaisesRegex(ValueError,"subscriber was not active"):
            plan(self.snapshot,snap,"fixture-closed",0,levels,namespace)
        cli = [sys.executable,"-B","-m","rewrite.trigger_handoff"]
        root = Path(__file__).resolve().parents[1]
        archive_args = ["--snapshot",str(self.snapshot),"--manifest-sha256",snap]
        planned = subprocess.run([*cli,"plan",*archive_args,"--subscriber","fixture-live","--consumer-cursor","1",
                                  "--level","event","--level","notify","--level","info","--source-id",namespace],
                                 cwd=root,check=True,capture_output=True,text=True,timeout=10)
        report = json.loads(planned.stdout)
        entries = {item["legacy_event_id"]:item for item in report["plan"]["candidates"]}
        names = ["pending","info","already-seen","with-files","quiet"]
        old_pending = json.loads(self.old("""
store = TriggerStore(sys.argv[1])
print(json.dumps([item["event_id"] for item in store.list_events("fixture-live",None)]))
store.close()
""",self.source))
        self.assertEqual(list(entries),old_pending)
        self.assertEqual(old_pending,names)
        event_id,info_id,seen_id,file_id,quiet_id = [entries[name]["message_id"] for name in names]
        self.assertNotIn("Event",entries["quiet"]["allowed_levels"])
        saved,review = self.root/"plan.json",self.root/"review.json"
        saved.write_text(json.dumps(report),encoding="utf-8")
        decisions = {str(entries[name]["seq"]):{"level":level,"evidence":"Fixture consumer confirmed not received"}
                     for name,level in zip(names,("Event","Info","Notify","Notify","Info"),strict=True)}
        decisions[str(entries["already-seen"]["seq"])] = {"already_received":"fixture native receipt for old event"}
        approved = {"plan_sha256":report["plan_sha256"],"events":decisions}
        review.write_text(json.dumps({**approved,"events":{}}),encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"review must cover each unread event"):
            stage(self.snapshot,snap,saved,review,self.restored,attachments=files)
        self.assertFalse(self.restored.exists())
        review.write_text(json.dumps(approved),encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"offline content store"):
            stage(self.snapshot,snap,saved,review,self.restored)
        target = files/checksum
        target.write_bytes(b"x"+sample[1:])
        with self.assertRaisesRegex(ValueError,"bytes do not match"):
            stage(self.snapshot,snap,saved,review,self.restored,attachments=files)
        self.assertFalse(self.restored.exists())
        target.write_bytes(sample)
        staged = subprocess.run([*cli,"stage",*archive_args,"--saved",str(saved),"--review",str(review),
                                 "--destination",str(self.restored),"--attachments",str(files)],
                                cwd=root,check=True,capture_output=True,text=True,timeout=10)
        stage_result = json.loads(staged.stdout)
        repeat = self.root/"rebuilt"
        self.assertEqual(stage(self.snapshot,snap,saved,review,repeat,attachments=files),stage_result)
        target.unlink()
        self.assertEqual((stage_result["pending"],stage_result["already_received"]),(4,1))
        instance = stage_result["instance_id"]
        trigger = Trigger(self.restored/HANDOFF_DB,"legacy-handoff")
        self.assertEqual(trigger.instance_id,instance)
        self.assertEqual(trigger.pending(),[event_id,info_id,file_id,quiet_id])

        clock = Clock()
        with Loop.isolated(clock,FixtureHost,trigger_ids=(instance,)) as loop:
            wire = loop.trigger_clients[instance]
            for mid in (event_id,info_id):
                trigger.send(wire,mid)
            send = wire.request
            lost = []

            def drop_ack(method,path,data=None,key=None,**kwargs):
                result = send(method,path,data,key,**kwargs)
                if method=="POST" and path=="messages":
                    lost.append(result)
                    raise ConnectionError("durable T04 receipt was not delivered")
                return result

            wire.request = drop_ack
            with self.assertRaisesRegex(ConnectionError,"durable T04"):
                trigger.send(wire,file_id)
            self.assertEqual(trigger.pending(),[file_id,quiet_id])
            wire.request = send
            reopened = Trigger(self.restored/HANDOFF_DB,"legacy-handoff")
            retried = reopened.send(wire,file_id)
            self.assertEqual(lost,[retried])
            rebuilt = Trigger(repeat/HANDOFF_DB,"legacy-handoff")
            self.assertEqual(rebuilt.send(wire,file_id),retried)
            reopened.send(wire,quiet_id)
            self.assertEqual(reopened.pending(),[])
            old_notify, = loop.hermes.messages(message_id=file_id)["items"]
            self.assertEqual(old_notify["source"],"router")
            self.assertNotIn("secret_old_metadata",json.dumps(old_notify))
            image,fulltext = old_notify["attachment_ids"]
            self.assertEqual(loop.manager_wire.request("GET","attachments/"+image)["bytes"],sample)
            self.assertEqual(loop.manager_wire.request("GET","attachments/"+fulltext)["bytes"],
                             ("原附件\n"+"新↵"*300).encode())
            self.assertEqual({item["message_id"] for item in loop.hermes.messages()["items"]},
                             {event_id,info_id,file_id,quiet_id})
            self.assertNotIn(seen_id,[frame["payload"]["message_id"] for frame in loop.manager_wire.frames(0)])
            frames = loop.manager_wire.frames(0)
            self.assertEqual([frame["payload"]["message_id"] for frame in frames],[event_id,file_id])
            loop.hermes.poll()
            self.assertEqual([p["message_id"] for p,_ in loop.hermes_native.entries()],[event_id])
            loop.hermes.poll()
            self.assertEqual(len(loop.hermes_native.entries()),1)
            clock.advance(10)
            loop.hermes.poll()
            delivered = [p["message_id"] for p,_ in loop.hermes_native.entries()]
            self.assertEqual(delivered,[event_id,file_id])
            delivery = loop.manager_wire.request("GET","deliveries/status")
            self.assertEqual((delivery["latest_cursor"],delivery["confirmed_cursor"],
                              delivery["awaiting_consumer_ack"]),(2,2,0))
            loop.hermes.poll()
            self.assertEqual(len(loop.hermes_native.entries()),2)
            with closing(sqlite3.connect(loop.root/"router.db")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM uploads").fetchone()[0],2)
        self.assertEqual(digest(self.source),original)
        self.evidence = {"old_cursor":1,"old_unread_ids":old_pending,"mapped":dict(zip(names,
                         (event_id,info_id,seen_id,file_id,quiet_id),strict=True)),"trigger_instance_id":instance,
                         "receipt_after_retry":retried,"router_delivery_status":delivery,"manager_host_inputs":delivered,
                         "original_file_sha256":checksum,"both_artifacts_byte_verified":True,
                         "old_archive_unchanged":True,"information_did_not_wake":True,
                         "boundary":"Actual old TriggerStore and rewrite HTTP and SQLite; Hermes host and clock are fixtures."}
