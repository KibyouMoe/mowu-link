from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from .adapter import HostAdapter
from .devloop import FixtureHost
from .feishu_inbound import Inbound
from .feishu_output import Views
from .hermes import HermesAdapter
from .manager_notices import ManagerNotices
from .question_cards import Actions
from .codex_approvals import CodexApprovals
from .codex_thread_preparation import SCHEMA as CODEX_PREPARATION_SCHEMA
from .managed_sessions import FAMILY_SCHEMA, derive_callback, read_enrollment, token_digest, write_enrollment
from .orders import Orders
from .release_check import config
from .state_bundle import (assemble_hosts, audit_handoff, audit_trigger, capture, filename, restore, rollback,
                           stage_native_handoff, validate_set, verify)
from .storage import Database, Fault, StateWriterBusy, encode, guid, state_writer
from .test_feishu_inbound import PNG
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .trigger import Trigger
from .wire import Client, serve_loopback
from .worker import Worker
from . import display, dsh_workspace_audit, state_bundle, user_questions, worker_commands
from .worker_service import WorkerService


@contextmanager
def running(server):
    with server:
        thread = Thread(target=server.serve_forever,kwargs={"poll_interval":0.01})
        thread.start()
        try:
            yield "http://127.0.0.1:"+str(server.server_port)
        finally:
            server.shutdown()
            thread.join()


class Bundle(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        labels = ("router:main","worker:oguri","agent:oguri","hermes:main","hermes-host:main","trigger:mail","feishu:oguri")
        self.sources = {label:self.root/filename(label) for label in labels}
        self.snapshot = self.root/"backup"
        self.dest = self.root/"restored"
        self.clock = Clock()
        self.native = FixtureHost(self.root/"separate-native.db","isolated-agent")
        self.hermes_host = FixtureHost(self.root/"separate-manager.db","isolated-manager")
        self.platform = PlatformFixture()
        self.revision = "a"*40

    @contextmanager
    def stack(self,paths,*,pin_session=False,extra_workers=(),approval_operator=False):
        source = Trigger(paths["trigger:mail"],"mail",self.clock)
        router = Orders(paths["router:main"],self.clock,1)
        ids = {"mgr":"hermes","w":"worker/oguri","mail":"trigger/"+source.instance_id,
               **{wid:"worker/"+wid for wid in extra_workers}}
        if approval_operator:
            ids["operator"] = "approver/oguri"
        with running(serve_loopback(router,ids)) as url:
            manager = HermesAdapter(paths["hermes:main"],Client(url,"mgr"),
                      HostAdapter(paths["hermes-host:main"],self.hermes_host,self.hermes_host.session),self.clock)
            agent = HostAdapter(paths["agent:oguri"],self.native,self.native.session)
            if pin_session:
                agent.expected_session = self.native.session
            worker = Worker(paths["worker:oguri"],"oguri",Client(url,"w"),agent)
            server = WorkerService(worker).listen({"platform":"platform/oguri"},("127.0.0.1",0),None)
            with running(server) as worker_url:
                client = Client(worker_url,"platform")
                platform = Inbound(paths["feishu:oguri"],client,None,"app","trainer","chat",clock=self.clock)
                views = Views(paths["feishu:oguri"],client,self.platform,"chat","工位",clock=self.clock)
                yield manager,worker,source,Client(url,"mail"),platform,views

    def test_trigger_audit_distinguishes_outbox_from_lost_receipt_and_reads_recovered_t04(self):
        attachment = self.root/"fixture.bin"
        content = bytes(range(256))*1300
        attachment.write_bytes(content)
        with self.stack(self.sources) as (manager,_,source,wire,_,_):
            event = source.record("Event","Unacknowledged source event",[attachment])
            info = source.record("Info","Still only in the source outbox")
            request = wire.request

            def lose_t04(method,path,*args,**kwargs):
                result = request(method,path,*args,**kwargs)
                if method=="POST" and path=="messages":
                    raise ConnectionError("lost T04 after commit")
                return result

            wire.request = lose_t04
            with self.assertRaisesRegex(ConnectionError,"lost T04 after commit"):
                source.send(wire,event)
            archived, = manager.messages(message_id=event)["items"]
            saved_time = archived["received_at"]
            self.assertEqual(len(manager.router.frames(0)),1)
        first = capture(self.sources,self.revision,self.snapshot)
        waiting = audit_trigger(self.snapshot,first["manifest_sha256"])
        self.assertFalse(waiting["ready"])
        self.assertEqual({row["message_id"]:row["code"] for row in waiting["issues"]},
                         {event:"awaiting_local_receipt",info:"awaiting_router_message"})
        self.assertEqual(waiting["sources"],[{"component":"trigger:mail","instance_id":source.instance_id,
                                             "archived":2,"settled":0}])

        recovered = restore(self.snapshot,first["manifest_sha256"],self.dest)
        with self.stack({key:Path(path) for key,path in recovered.items()}) as (manager,_,source,wire,_,_):
            self.assertEqual(source.send(wire,event)["received_at"],saved_time)
            source.send(wire,info)
            same, = manager.messages(message_id=event)["items"]
            self.assertEqual(same,archived)
            file, = same["attachment_ids"]
            self.assertEqual(manager.router.request("GET","attachments/"+file)["bytes"],content)
            self.assertEqual(len(manager.router.frames(0)),1)
        final = self.root/"settled"
        second = capture(recovered,self.revision,final)
        clean = audit_trigger(final,second["manifest_sha256"])
        self.assertTrue(clean["ready"])
        self.assertEqual(clean["issues"],[])
        self.assertEqual(clean["sources"][0]["settled"],2)
        for folder,snapshot,expected_exit,expected in ((self.snapshot,first,1,waiting),(final,second,0,clean)):
            cli = [sys.executable,"-B","-m","rewrite.state_bundle","audit-trigger","--snapshot",str(folder),
                   "--manifest-sha256",snapshot["manifest_sha256"]]
            result = subprocess.run(cli,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,expected_exit,result.stderr)
            self.assertEqual(json.loads(result.stdout),expected)
            self.assertEqual(verify(folder,snapshot["manifest_sha256"])["snapshot_id"],expected["snapshot_id"])
        if evidence := os.environ.get("MOWULINK_TRIGGER_AUDIT_EVIDENCE"):
            Path(evidence).write_text(json.dumps({"before":waiting,"after":clean,"original_router_received_at":saved_time,
                "attachment_id":file,"attachment_sha256":hashlib.sha256(content).hexdigest(),
                "attachment_bytes":len(content),"router_notifications":1,
                "boundary":"Real isolated Router HTTP and SQLite snapshots. Synthetic Trigger inputs."},
                ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_trigger_audit_rejects_individually_valid_but_inconsistent_ledgers(self):
        attachment = self.root/"fixture.bin"
        attachment.write_bytes(b"verified payload")
        with self.stack(self.sources) as (_,_,source,wire,_,_):
            broken_file = source.record("Info","Archive file",[attachment])
            no_router = source.record("Info","Router is behind")
            no_source = source.record("Info","Trigger is behind")
            false_receipt = source.record("Info","T04 timestamp differs")
            false_body = source.record("Info","Source text differs")
            no_grant = source.record("Info","Lost attachment permission",[attachment])
            for mid in (broken_file,no_router,no_source,false_receipt,false_body,no_grant):
                source.send(wire,mid)

        with closing(sqlite3.connect(self.sources["router:main"])) as router, router:
            router.execute("UPDATE chunks SET bytes=zeroblob(length(bytes)) WHERE upload_id=(SELECT u.upload_id FROM uploads u "
                           "JOIN messages m ON u.attachment_id=json_extract(m.payload,'$.attachment_ids[0]') WHERE m.message_id=?)",
                           (broken_file,))
            router.execute("DELETE FROM messages WHERE message_id=?",(no_router,))
            router.execute("UPDATE messages SET payload=json_set(payload,'$.text','different') WHERE message_id=?",(false_body,))
            router.execute("""DELETE FROM attachment_grants WHERE attachment_id=(SELECT json_extract(payload,'$.attachment_ids[0]')
                              FROM messages WHERE message_id=?) AND actor='hermes'""",(no_grant,))
        with source.db.transaction() as db:
            db.execute("DELETE FROM archive WHERE id=?",(no_source,))
            db.execute("UPDATE archive SET receipt=json_set(receipt,'$.received_at','2025-01-01T00:00:00+00:00') WHERE id=?",
                       (false_receipt,))
        seal = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        audit = audit_trigger(self.snapshot,seal)
        self.assertFalse(audit["ready"])
        self.assertEqual({row["message_id"]:row["code"] for row in audit["issues"]},
                         {broken_file:"attachment_mismatch", no_router:"receipt_without_router_message",
                          no_source:"router_message_without_source_archive", false_receipt:"router_receipt_mismatch",
                          false_body:"router_payload_mismatch", no_grant:"attachment_mismatch"})

        other = self.root/"other-trigger.sqlite3"
        Trigger(other,"another-source",self.clock)
        inputs = {label:path for label,path in self.sources.items() if label!="trigger:mail"}
        unlisted = self.root/"source-omitted"
        seal = capture({**inputs,"trigger:other":other},self.revision,unlisted)["manifest_sha256"]
        audit = audit_trigger(unlisted,seal)
        self.assertFalse(audit["ready"])
        self.assertEqual({row["message_id"]:row["code"] for row in audit["issues"]},
                         {mid:"router_source_missing" for mid in (broken_file,no_source,false_receipt,false_body,no_grant)})

    def test_prepare_worker_chat_archive_after_both_receipts_recover(self):
        from .feishu_history import prepare as prepare_history

        def record(platform,mid,text):
            return platform.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat",
                                   "message_id":mid,"message_type":"text","content":encode({"text":text})})

        def card_event(worker,kind,event_id,turn_id,text):
            display.native(worker,{"event_id":event_id,"session":self.native.session,"turn_id":turn_id,
                                   "kind":kind,"text":text,**({"tool_name":"fixture"} if kind=="tool" else {})})

        def report(snapshot,proof,destination):
            return prepare_history(snapshot,proof["manifest_sha256"],destination,"oguri")

        with self.stack(self.sources) as (_,worker,_,_,platform,views):
            mid = record(platform,"om_first_chat","first trainer message")
            request = platform.worker.request

            def lose_reply(method,path,*args,**kwargs):
                request(method,path,*args,**kwargs)
                raise ConnectionError("native write succeeded but its reply was not received")

            platform.worker.request = lose_reply
            try:
                with self.assertRaises(ConnectionError):
                    platform.deliver(mid)
            finally:
                platform.worker.request = request
            native = self.native.entries()[0][1]
            card_event(worker,"tool","tool-preview",native["turn_id"],"tool preview")
            card_event(worker,"final","chat-reply",native["turn_id"],"first public reply")
            card_event(worker,"final","other-final",guid(),"not a Feishu chat reply")
            self.assertEqual(views.poll(),3)
            with platform.db.transaction() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM chat_history_index").fetchone()[0],0)
            self.clock.advance(6)
            self.assertEqual(platform.deliver(record(platform,mid,"first trainer message")),native)
            self.assertEqual(len(self.native.entries()),1)

            legacy_mid = record(platform,"om_old_chat","old input with no time")
            with platform.db.transaction() as db:
                db.execute("DELETE FROM chat_arrivals WHERE mid=?",(legacy_mid,))
            platform.deliver(legacy_mid)

            mixed = platform.deliver(record(platform,"om_mixed_chat","input to a mixed turn"))
            # The private reply exporter refuses a turn with more than one recorded origin.
            with worker.adapter.db.transaction() as db:
                db.execute("INSERT INTO intents(delivery_id,session,payload,digest,receipt,native_id) VALUES (?,?,?,?,?,?)",
                           ("fixture-mixed",mixed["session"],encode({"kind":"task"}),mixed["digest"],
                            encode(mixed),guid()))
            card_event(worker,"final","mixed-final",mixed["turn_id"],"mixed native output")
            self.assertEqual(views.poll(),1)
            second_mid = record(platform,"om_second_chat","second trainer message")
            second = platform.deliver(second_mid)
            card_event(worker,"final","second-reply",second["turn_id"],"second public reply")
            self.platform.lose_next = True
            with self.assertRaises(ConnectionError):
                views.poll()

        original = capture(self.sources,self.revision,self.snapshot)
        partial = report(self.snapshot,original,self.root/"partial-chat")
        self.assertEqual((partial["inbound"],partial["outbound"],partial["inbound_without_index"],
                          partial["finals_without_chat_proof"]),(3,1,1,2))
        paths = restore(self.snapshot,original["manifest_sha256"],self.dest)
        posted = len(self.platform.messages)
        with self.stack(paths) as (_,_,_,_,platform,views):
            self.assertEqual(views.poll(),1)
            self.assertEqual(platform.deliver(second_mid),second)
            self.assertEqual(len(self.platform.messages),posted)
            self.assertEqual(views.poll(),0)
        snapshot = self.root/"second-backup"
        proof = capture(paths,self.revision,snapshot)
        archive_dir = self.root/"chat-result"
        tool = subprocess.run([sys.executable,"-B","-S","-m","rewrite.cutover","prepare-worker-chat-archive",
                               "--snapshot",snapshot,"--manifest-sha256",proof["manifest_sha256"],
                               "--destination",archive_dir,"--worker","oguri"],
                              cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=15)
        self.assertEqual(tool.returncode,0,tool.stderr)
        final = json.loads(tool.stdout)
        self.assertEqual((final["inbound"],final["outbound"],final["inbound_without_index"],
                          final["finals_without_chat_proof"]),(3,2,1,2))
        with self.assertRaisesRegex(ValueError,"no Feishu state belongs"):
            prepare_history(snapshot,proof["manifest_sha256"],self.root/"wrong-chat","tamamo")
        partial_archive = self.root/"partial-chat"/"chat-history.db"
        with closing(sqlite3.connect(partial_archive.as_uri()+"?mode=ro",uri=True)) as state:
            partial_ids = [row[0] for row in state.execute("SELECT id FROM messages ORDER BY id")]
        archive = archive_dir/"chat-history.db"
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),final["archive_sha256"])
        with closing(sqlite3.connect(archive.as_uri()+"?mode=ro",uri=True)) as state:
            state.row_factory = sqlite3.Row
            records = state.execute("SELECT id,text,worker,message_id FROM messages ORDER BY id").fetchall()
            self.assertEqual([row["text"] for row in records],["first trainer message","first public reply",
                "input to a mixed turn","second trainer message","second public reply"])
            self.assertEqual([row["id"] for row in records[:-1]],partial_ids)
            self.assertEqual({row["worker"] for row in records},{"oguri"})
            self.assertEqual(state.execute("SELECT id,text,worker,message_id FROM messages WHERE id>? ORDER BY id",
                                           (partial_ids[-1],)).fetchall(),records[-1:])
        if path := os.environ.get("MOWULINK_WORKER_CHAT_EVIDENCE"):
            Path(path).write_text(json.dumps({"before_restore":{"rows":partial["count"],"inbound":partial["inbound"],
                "outbound":partial["outbound"]},"after_restore":final,"copied_history_cursor":partial_ids[-1],
                "ids":[row["id"] for row in records],"last_feishu_message_id":records[-1]["message_id"],
                "native_retry_extra_inputs":0,"platform_retry_extra_messages":0,"archive_workers":["oguri"],
                "scope":"Local Worker HTTP, read-only chat archive and component snapshots. Native input and Feishu use fixtures."},
                indent=2)+"\n",encoding="utf-8")

    def test_five_component_restore_resumes_hold_attachment_notifications_and_platform_input(self):
        with self.stack(self.sources) as (m,w,source,mail,platform,views):
            tid = m.create(guid(),"重开后继续","报告一次","oguri")["task_id"]
            w.connect()
            w.poll()
            self.clock.advance(10)
            w.poll()
            w.poll()
            hold = w.hold()
            self.assertEqual(hold["held_task_id"],tid)
            origin,receipt = self.native.entries()[0]
            # Router and Hermes adapter have accepted this Notify, but the host is busy.
            self.hermes_host.is_busy = True
            mid = source.record("Notify","归队通知")
            source.send(mail,mid)
            m.poll()
            self.assertEqual(self.hermes_host.entries(),[])
            data = bytes(range(256))*1300
            raw_file = self.root/"input.bin"
            raw_file.write_bytes(data)
            pending = source.record("Info","离线原件",[raw_file])
            raw_file.unlink()
            platform.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":"om_offline",
                             "message_type":"text","content":encode({"text":"恢复后打招呼"})})
            views.poll()
            already_sent = list(self.platform.calls)
            source_id,session = source.instance_id,w.session
        snapshot = capture(self.sources,self.revision,self.snapshot)
        self.assertEqual(snapshot["components"],7)
        audit = audit_handoff(self.snapshot,snapshot["manifest_sha256"])
        self.assertFalse(audit["needs_review"])
        self.assertEqual(audit["workers"][0]["router_assignment"],{"task_id":tid,"assignment_id":hold["held_assignment_id"]})
        paths = restore(self.snapshot,snapshot["manifest_sha256"],self.dest)
        with self.stack(paths) as (m,w,source,mail,platform,views):
            self.assertEqual((source.instance_id,w.session),(source_id,session))
            w.connect()
            w.poll()
            self.assertEqual(w.hold(),hold)
            self.assertEqual(w.adapter.deliver(receipt["delivery_id"],origin),receipt)
            self.assertEqual(len(self.native.entries()),1)
            self.assertEqual(views.poll(),0)
            self.assertEqual(self.platform.calls,already_sent)
            self.assertEqual(platform.pending(),["om_offline"])
            chat = platform.deliver("om_offline")
            self.assertEqual(platform.deliver("om_offline"),chat)
            self.assertEqual(len(self.native.entries()),2)
            self.assertEqual(source.pending(),[pending])
            source.send(mail,pending)
            file_id, = m.messages(message_id=pending)["items"][0]["attachment_ids"]
            self.assertEqual(m.router.request("GET","attachments/"+file_id)["bytes"],data)
            self.hermes_host.is_busy = False
            m.poll()
            self.clock.advance(10)
            m.poll()
            m.poll()
            self.assertEqual([p["message_id"] for p,_ in self.hermes_host.entries()],[mid])
            closed = w.report(receipt["native_id"],tid,hold["held_assignment_id"],"恢复后交件")
            self.assertEqual(w.report(receipt["native_id"],tid,hold["held_assignment_id"],"恢复后交件"),closed)
            self.assertEqual(m.inspect(tid)["task"]["state"],"completed_reported")
            self.assertIsNone(w.hold()["held_task_id"])
        self.assertEqual(verify(self.snapshot,snapshot["manifest_sha256"])["source_revision"],self.revision)

    def test_hermes_snapshot_checks_the_router_receipt_prefix_without_treating_lag_as_loss(self):
        router = self.sources["router:main"]
        before = self.root/"router-before-c02.db"
        with self.stack(self.sources) as (_manager,_worker,source,client,_platform,_views):
            mid = source.record("Event","Retain the original Hermes delivery")
            source.send(client,mid)
        before.write_bytes(router.read_bytes())
        with self.stack(self.sources) as (manager,*_):
            acknowledge = manager.router.ack

            def lose_reply(frame):
                acknowledge(frame)
                raise ConnectionError("Router recorded C02 but did not return its reply")

            with patch.object(manager.router,"ack",side_effect=lose_reply), self.assertRaises(ConnectionError):
                manager.receive()

        prefix = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        lagging = audit_handoff(self.snapshot,prefix)
        self.assertFalse(lagging["needs_review"])
        self.assertEqual(lagging["hermes"][0],{"instance":"main","router_confirmed_cursor":1,
            "local_cursor":0,"queued_without_native_receipt":1,"issues":[],"native_receipt_issues":[],
            "native_receipts":{"not_attempted":1,"intent_unconfirmed":0,"manager_commit_pending":0,
                               "confirmed":0,"unverified":0}})
        with self.stack(self.sources) as (manager,*_):
            manager.receive()
            self.assertEqual(manager.router.request("GET","deliveries/status")["confirmed_cursor"],1)

        manager_db = self.sources["hermes:main"]
        saved = self.root/"manager-missing-queue-item.db"
        saved.write_bytes(manager_db.read_bytes())
        with closing(sqlite3.connect(saved)) as db,db:
            db.execute("DELETE FROM queue WHERE message_id=?",(mid,))
        for variant,paths,reason in (
            ("older-router",{**self.sources,"router:main":before},"hermes_cursor_ahead_of_router"),
            ("lost-prefix",{**self.sources,"hermes:main":saved},"hermes_confirmed_prefix_missing"),
        ):
            with self.subTest(variant=variant):
                target = self.root/variant
                expected = capture(paths,self.revision,target)["manifest_sha256"]
                detail = audit_handoff(target,expected)
                self.assertTrue(detail["needs_review"])
                self.assertIn({"hermes_instance":"main","issue":reason},detail["issues"])

    def test_hermes_snapshot_checks_the_native_receipt_attachment_and_cached_payload(self):
        file = self.root/"attachment.txt"
        file.write_text("Isolated source attachment",encoding="utf-8")
        with self.stack(self.sources) as (manager,_worker,source,client,_platform,_views):
            mid = source.record("Event","Private manager notification text",[file])
            source.send(client,mid)
            manager.receive()
            with manager.db.transaction() as db:
                queued = db.execute("SELECT * FROM queue WHERE message_id=?",(mid,)).fetchone()
            payload = manager.delivery_payload(queued)
            native = manager.host.deliver(mid,payload)
            # Stop after the adapter committed the receipt and before Hermes saved it.
            self.assertEqual(len(self.hermes_host.entries()),1)
        check = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        pending = audit_handoff(self.snapshot,check)
        item, = pending["hermes"]
        self.assertTrue(pending["needs_review"])
        self.assertEqual(item["native_receipts"]["manager_commit_pending"],1)
        self.assertEqual(item["native_receipt_issues"],[{"message_id":mid,"code":"hermes_native_receipt_pending"}])
        self.assertNotIn(payload["text"],encode(pending))
        paths = restore(self.snapshot,check,self.dest)
        with self.stack(paths) as (manager,*_):
            self.assertEqual(manager.poll(),native)
            self.assertEqual(len(self.hermes_host.entries()),1)

        ready = self.root/"ready"
        seal = capture(paths,self.revision,ready)["manifest_sha256"]
        reconciled = audit_handoff(ready,seal)
        self.assertFalse(reconciled["needs_review"])
        self.assertEqual(reconciled["hermes"][0]["native_receipts"]["confirmed"],1)
        self.assertEqual(audit_handoff(self.snapshot,check),pending)

        plan = (
            ("corrupt-receipt","hermes:main","UPDATE queue SET receipt='{}'","hermes_native_receipt_mismatch"),
            ("wrong-host","hermes-host:main","UPDATE adapter_native_binding SET session='swapped'","hermes_native_intent_mismatch"),
            ("orphan","hermes:main","DELETE FROM queue","hermes_orphan_host_intent"),
            ("wrong-payload","hermes:main","UPDATE queue SET host_payload='{}'","hermes_cached_host_payload_mismatch"),
            ("corrupt-artifact","router:main","UPDATE chunks SET bytes=X'00'","hermes_attachment_unverified"),
            ("loss-of-authorization","router:main","DELETE FROM attachment_grants WHERE actor='hermes'","hermes_attachment_unverified"),
        )
        for variant,label,sql,error in plan:
            with self.subTest(variant=variant):
                copy = self.root/(variant+".db")
                shutil.copyfile(paths[label],copy)
                with closing(sqlite3.connect(copy)) as db,db:
                    db.execute(sql)
                dest = self.root/variant
                bad = capture(dict(paths,**{label:copy}),self.revision,dest)["manifest_sha256"]
                result = audit_handoff(dest,bad)
                self.assertTrue(result["needs_review"])
                self.assertIn({"message_id":mid,"code":error},result["hermes"][0]["native_receipt_issues"])
                self.assertEqual(result["hermes"][0]["native_receipts"]["confirmed"],0)
        omitted = self.root/"omitted"
        digest = capture({k:v for k,v in paths.items() if k!="hermes-host:main"},self.revision,omitted)["manifest_sha256"]
        missing = audit_handoff(omitted,digest)
        self.assertTrue(missing["needs_review"])
        self.assertEqual(missing["hermes"][0]["native_receipt_issues"],[{"code":"hermes_host_ledger_missing","count":1}])

    def test_hermes_snapshot_separately_reconciles_manager_platform_receipts(self):
        notices = ManagerNotices(self.platform,"oc_test_manager","test-app",self.clock)

        def audit(name, paths):
            snapshot = self.root/name
            seal = capture(paths,self.revision,snapshot)["manifest_sha256"]
            result = audit_handoff(snapshot,seal)
            self.assertNotIn("source-only text",encode(result))
            return snapshot,seal,result

        with self.stack(self.sources) as (manager,_worker,source,client,_platform,_views):
            manager.notices = notices
            first = source.record("Event","source-only text")
            source.send(client,first)
            self.assertEqual(manager.poll()["delivery_id"],first)
            self.platform.lose_next = True
            self.assertIsNone(notices.flush(manager.db))
            sent, = self.platform.messages.values()
        snapshot,seal,pending = audit("notice-pending",self.sources)
        item, = pending["hermes"]
        self.assertEqual(item["native_receipts"]["confirmed"],1)
        self.assertTrue(pending["needs_review"])
        self.assertEqual(item["platform_notices"]["awaiting_receipt"],1)
        self.assertEqual(item["platform_notices"]["issues"],
                         [{"code":"hermes_platform_notice_unconfirmed","message_id":first}])
        restored = restore(snapshot,seal,self.dest)
        self.clock.advance(5)
        with self.stack(restored) as (manager,*_):
            self.assertEqual(notices.flush(manager.db),sent[0])
            self.assertEqual(self.platform.calls,[("send",sent[0])]*2)
            self.assertIsNone(manager.poll())
            self.assertEqual(len(self.hermes_host.entries()),1)
        _,_,confirmed = audit("notice-confirmed",restored)
        self.assertFalse(confirmed["needs_review"])
        self.assertEqual(confirmed["hermes"][0]["platform_notices"]["confirmed"],1)

        with self.stack(restored) as (manager,_worker,source,client,_platform,_views):
            manager.notices = notices
            later = source.record("Event","A different event")
            source.send(client,later)
            self.assertEqual(manager.poll()["delivery_id"],later)
            self.platform.lose_next = True
            self.assertIsNone(notices.flush(manager.db))
            self.clock.advance(3600)
            self.assertIsNone(notices.flush(manager.db))

        _,_,settled = audit("notice-expired",restored)
        proof = settled["hermes"][0]["platform_notices"]
        self.assertEqual((proof["confirmed"],proof["receipt_unknown"]),(1,1))
        self.assertEqual(proof["issues"],[{"code":"hermes_platform_notice_receipt_unknown","message_id":later}])
        self.assertTrue(settled["needs_review"])
        self.assertEqual(len(self.platform.calls),3)
        self.assertEqual(audit_handoff(snapshot,seal),pending)

        with closing(sqlite3.connect(restored["hermes:main"])) as db,db:
            db.execute("UPDATE manager_notices SET text='mismatched notice' WHERE message_id=?",(first,))
        _,_,mismatch = audit("notice-mismatch",restored)
        self.assertIn({"code":"hermes_platform_notice_mismatch","message_id":first},
                      mismatch["hermes"][0]["platform_notices"]["issues"])

        with closing(sqlite3.connect(restored["hermes:main"])) as db,db:
            db.execute("UPDATE manager_notices SET text=?,platform_id=?,error=NULL WHERE message_id=?",
                       (sent[2]["text"],sent[0],first))
            db.execute("UPDATE manager_notices SET platform_id=?,error=NULL WHERE message_id=?",(sent[0],later))
        _,_,duplicate = audit("notice-duplicate",restored)
        self.assertEqual(duplicate["hermes"][0]["platform_notices"]["issues"],
                         [{"code":"hermes_platform_notice_mismatch","message_id":mid} for mid in (first,later)])

    def test_worker_snapshot_compares_original_confirmed_frame_in_both_ledgers(self):
        with self.stack(self.sources) as (_manager,w,*_):
            w.connect()
            w.poll()
        checksum = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        good = audit_handoff(self.snapshot,checksum)
        self.assertFalse(good["needs_review"])
        self.assertEqual(good["workers"][0]["inbox_transport"],
                         {"router_confirmed_cursor":1,"local_cursor":1,"matched_frames":1})

        worker_db = self.sources["worker:oguri"]
        for name,sql in (("missing","DELETE FROM inbox"),
                         ("changed","UPDATE inbox SET frame=json_set(frame,'$.payload.query_id','different')")):
            with self.subTest(name=name):
                wrong_worker = self.root/(name+".db")
                wrong_worker.write_bytes(worker_db.read_bytes())
                with closing(sqlite3.connect(wrong_worker)) as db,db:
                    db.execute(sql)
                other = self.root/name
                seal = capture({**self.sources,"worker:oguri":wrong_worker},self.revision,other)["manifest_sha256"]
                result = audit_handoff(other,seal)
                self.assertTrue(result["needs_review"])
                self.assertIn("worker_confirmed_prefix_missing",result["workers"][0]["issues"])
                self.assertEqual(result["workers"][0]["inbox_transport"]["matched_frames"],0)
                if name=="changed":
                    self.assertIn("worker_inbox_router_mismatch",result["workers"][0]["issues"])

    def test_worker_snapshot_flags_old_w03_receipt_even_when_transport_prefix_matches(self):
        stale = self.root/"worker-after-c02-before-w03.db"
        with self.stack(self.sources) as (manager,w,*_):
            task_id = manager.create(guid(),"One execution only","Report before taking the old copy forward.","oguri")["task_id"]
            w.connect()
            w.poll()
            self.clock.advance(10)
            w.poll()
            process = w.process

            def stop_before_w03(frame):
                if frame["kind"]=="task.dispatch":
                    raise ConnectionError("stopped after C02 and before W03")
                return process(frame)

            with patch.object(w,"process",side_effect=stop_before_w03), self.assertRaises(ConnectionError):
                w.poll()
            with closing(sqlite3.connect(w.db.path)) as original, closing(sqlite3.connect(stale)) as copied:
                original.backup(copied)
            w.poll()
            (payload,receipt), = self.native.entries()
            result = w.report(receipt["native_id"],task_id,payload["assignment_id"],"Only one formal result")
            self.assertEqual(result["task_state"],"completed_reported")
            assignment = payload["assignment_id"]
            historical = w.router.request("POST",f"tasks/{task_id}/accept",
                {"session_id":w.session,"assignment_id":assignment,"revision":payload["revision"]},
                "accept-"+assignment)
            self.assertTrue(historical["accepted"])
            self.assertEqual(len(self.native.entries()),1)

        checksum = capture({**self.sources,"worker:oguri":stale},self.revision,self.snapshot)["manifest_sha256"]
        check = audit_handoff(self.snapshot,checksum)
        worker, = check["workers"]
        transport = worker["inbox_transport"]
        self.assertEqual(transport["local_cursor"],transport["router_confirmed_cursor"])
        self.assertGreater(transport["matched_frames"],0)
        self.assertTrue(check["needs_review"])
        self.assertIn("worker_stale_dispatch_receipt",worker["issues"])
        destination = self.root/"stale-handoff"
        with self.assertRaisesRegex(ValueError,"worker_stale_dispatch_receipt"):
            stage_native_handoff(self.snapshot,checksum,"oguri",self.native.session,"new-session",destination)
        self.assertFalse(destination.exists())

    def test_host_assembly_requires_its_pinned_roster_and_rejects_changed_bytes(self):
        with self.stack(self.sources):
            pass
        batch = guid()
        label = "worker:oguri"
        sources = {label:self.sources[label]}
        worker_part = self.root/"worker-part"
        with state_writer(self.sources[label]),self.assertRaises(StateWriterBusy):
            capture(sources,self.revision,worker_part,batch_id=batch,host_id="worker")
        self.assertFalse(worker_part.exists())

        hosts = {}
        for host,ledgers in (("worker",sources),("router",{k:v for k,v in self.sources.items() if k!=label})):
            path = self.root/(host+"-part")
            fragment = capture(ledgers,self.revision,path,batch_id=batch,host_id=host)
            hosts[host] = {"snapshot":str(path),"manifest_sha256":fragment["manifest_sha256"],"components":list(ledgers)}
        plan = {"batch_id":batch,"source_revision":self.revision,"hosts":hosts}
        with self.assertRaisesRegex(ValueError,"must be assembled"):
            restore(worker_part,hosts["worker"]["manifest_sha256"],self.dest)
        for name,invalid in (
            ("old-batch",dict(plan,batch_id=guid())),
            ("old-revision",dict(plan,source_revision="b"*40)),
            ("wrong-host",dict(plan,hosts={"other":hosts["worker"],"router":hosts["router"]})),
            ("missing-worker",dict(plan,hosts={"router":hosts["router"]})),
            ("wrong-roster",dict(plan,hosts=dict(hosts,worker=dict(hosts["worker"],components=["worker:other"])))),
            ("wrong-digest",dict(plan,hosts=dict(hosts,worker=dict(hosts["worker"],manifest_sha256="0"*64)))),
        ):
            with self.subTest(name=name),self.assertRaises(ValueError):
                assemble_hosts(invalid,self.dest)
            self.assertFalse(self.dest.exists())
        path = worker_part/filename(label)
        original = path.read_bytes()
        with path.open("ab") as stream:
            stream.write(b"unrecorded bytes")
        with self.assertRaisesRegex(ValueError,"snapshot bytes do not match"):
            assemble_hosts(plan,self.dest)
        self.assertFalse(self.dest.exists())
        path.write_bytes(original)
        merged = assemble_hosts(plan,self.dest)
        manifest = verify(self.dest,merged["manifest_sha256"])
        self.assertEqual(manifest["assembled_hosts"],{"batch_id":batch,
                         "manifest_sha256_by_host":{host:part["manifest_sha256"] for host,part in hosts.items()}})
        self.assertEqual(set(manifest["files"]),set(self.sources))
        self.assertEqual((self.dest/filename(label)).read_bytes(),original)
        self.assertEqual(path.read_bytes(),original)

    def test_feishu_audit_keeps_a_held_receipt_in_scope_while_other_output_recovers(self):
        with self.stack(self.sources) as (_m,w,_source,_wire,_platform,views):
            with w.db.transaction() as db:
                common = {"session":self.native.session,"turn_id":"first","kind":"progress","text":"Wait here"}
                first = display.append(db,{**common,"event_id":"unknown"})["seq"]
                second = display.append(db,{**common,"event_id":"related"})["seq"]
                display.append(db,{**common,"event_id":"independent","turn_id":"other"})
            self.platform.lose_next = True
            with self.assertRaises(ConnectionError):
                views.poll()
        old = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        recovered = restore(self.snapshot,old,self.dest)
        with self.stack(recovered) as (_m,_w,_source,_wire,_platform,views):
            self.clock.advance(3601)
            with self.assertLogs(level="WARNING"):
                self.assertEqual(views.poll(),1)
            self.assertEqual(views.poll(),0)
        current = self.root/"after-review"
        seal = capture(recovered,self.revision,current)["manifest_sha256"]
        audited = audit_handoff(current,seal)
        self.assertTrue(audited["needs_review"])
        worker, = audited["workers"]
        self.assertFalse(worker["native_session_ledger_clear"])
        self.assertEqual(worker["feishu_receipts"]["confirmed_outputs"],1)
        self.assertEqual(worker["feishu_receipts"]["issues"],
                         [{"code":"platform_receipt_held","display_seq":seq,"root_seq":first} for seq in (first,second)])
        self.assertEqual(worker["native_session_pending"]["platform_display_unseen"],0)
        self.assertEqual(self.platform.calls,[("send","om_1"),("send","om_2")])
        with closing(sqlite3.connect(recovered["feishu:oguri"])) as db,db:
            db.execute("UPDATE view_receipt_review SET scope='unrelated' WHERE seq=?",(second,))
        changed = self.root/"bad-review"
        seal = capture(recovered,self.revision,changed)["manifest_sha256"]
        self.assertIn({"code":"platform_output_receipt_mismatch","display_seq":second,"root_seq":first},
                      audit_handoff(changed,seal)["workers"][0]["feishu_receipts"]["issues"])

    def test_handoff_audit_detects_an_omitted_idle_peers_recipient_before_and_after_c02(self):
        other_labels = ("worker:tama","agent:tama")
        sources = {**self.sources,**{label:self.root/filename(label) for label in other_labels}}
        host = FixtureHost(self.root/"receiver-native.db","receiver-session")
        message_id = guid()
        text = "The recipient was omitted from the backup"
        reports = {}

        def receiver(paths, router_url):
            adapter = HostAdapter(paths["agent:tama"],host,host.session)
            return Worker(paths["worker:tama"],"tama",Client(router_url,"tama"),adapter,clock=self.clock)

        def report(name, inputs):
            path = self.root/name
            saved = capture(inputs,self.revision,path)
            result = audit_handoff(path,saved["manifest_sha256"])
            self.assertNotIn(text,encode(result))
            reports[name] = result
            return saved["manifest_sha256"]

        with self.stack(sources,extra_workers=("tama",)) as (_manager,sender,_source,router,*_):
            other = receiver(sources,router.url)
            sender.connect()
            other.connect()
        report("only_status_queries",self.sources)
        self.assertFalse(reports["only_status_queries"]["needs_review"])

        with self.stack(sources,extra_workers=("tama",)) as (_manager,sender,_source,router,*_):
            sender.send_peer(message_id,"tama",text)
        report("before_c02",self.sources)
        self.assertTrue(reports["before_c02"]["needs_review"])
        self.assertEqual(reports["before_c02"]["missing_worker_ledgers"],[{
            "worker_id":"tama","router_waiting_deliveries":1,"router_waiting_peer_receipts":1,
        }])

        with self.stack(sources,extra_workers=("tama",)) as (manager,sender,_source,router,*_):
            other = receiver(sources,router.url)
            original = other.adapter.deliver

            def lose_native_receipt(*args,**kwargs):
                original(*args,**kwargs)
                return None

            with patch.object(other.adapter,"deliver",side_effect=lose_native_receipt):
                other.poll()
            status = sender.router.request("GET","peers/messages/"+message_id)
            self.assertTrue(status["worker_received"])
            self.assertIsNone(status["host_receipt_id"])
            self.assertEqual(manager.tasks()["items"],[])
            self.assertEqual(len(host.entries()),1)
        report("after_c02",self.sources)
        self.assertTrue(reports["after_c02"]["needs_review"])
        self.assertEqual(reports["after_c02"]["missing_worker_ledgers"],[{
            "worker_id":"tama","router_waiting_peer_receipts":1,
        }])

        seal = report("all_components",sources)
        restored = restore(self.root/"all_components",seal,self.dest)
        with self.stack(restored,extra_workers=("tama",)) as (_manager,sender,_source,router,*_):
            other = receiver(restored,router.url)
            self.clock.advance(5)
            other.poll()
            other.poll()
            receipts = sender.router.request("GET","peers/messages/"+message_id)
            self.assertTrue(receipts["host_receipt_id"])
            (payload, _), = host.entries()
            self.assertEqual(payload["message_id"],message_id)
        report("reconciled",restored)
        self.assertFalse(reports["reconciled"]["needs_review"])
        self.assertEqual(reports["reconciled"]["missing_worker_ledgers"],[])

    def test_handoff_audit_checks_independent_roster_even_for_empty_ledgers(self):
        with self.stack(self.sources):
            pass
        self.sources["trigger:spare"] = self.root/"spare.db"
        Trigger(self.sources["trigger:spare"],"quiet-source",self.clock)
        roster = {"source_revision":self.revision,"components":sorted(self.sources)}
        full = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        missing = ["feishu:oguri","hermes-host:main","trigger:spare"]
        omit = {label:path for label,path in self.sources.items() if label not in missing}
        partial = self.root/"incomplete-snapshot"
        omitted = capture(omit,self.revision,partial)["manifest_sha256"]
        for snapshot, digest_, expected_roster, returncode, wanted in (
                (self.snapshot,full,roster,0,[]),
                (partial,omitted,roster,1,["snapshot_components_missing"]),
                (self.snapshot,full,{**roster,"source_revision":"b"*40},1,["snapshot_revision_mismatch"]),
                (self.snapshot,full,{**roster,"components":sorted(omit)},1,["snapshot_components_unexpected"])):
            with self.subTest(snapshot=snapshot.name,issues=wanted):
                roster_file = self.root/"roster.json"
                roster_file.write_text(encode(expected_roster),encoding="utf-8")
                cli = [sys.executable,"-B","-m","rewrite.state_bundle","audit-handoff","--snapshot",str(snapshot),
                       "--manifest-sha256",digest_,"--roster",str(roster_file)]
                output = subprocess.run(cli,capture_output=True,text=True,timeout=15)
                self.assertEqual(output.returncode,returncode,output.stderr)
                result = json.loads(output.stdout)
                self.assertEqual([issue["issue"] for issue in result["issues"]],wanted)
                if wanted==["snapshot_components_missing"]:
                    self.assertEqual(result["issues"][0]["components"],missing)
                self.assertEqual(verify(snapshot,digest_)["source_revision"],self.revision)
        with self.assertRaisesRegex(ValueError,"duplicate"):
            audit_handoff(self.snapshot,full,roster={**roster,"components":roster["components"]+["worker:oguri"]})

    def test_handoff_reconciles_both_w04_ledgers_and_formal_attachment_bytes(self):
        content = bytes(range(256))*1300
        secret_summary = "Private work report for the two-ledger test"
        with self.stack(self.sources) as (manager,worker,*_):
            tid = manager.create(guid(),"Formal file","Finish the isolated task","oguri")["task_id"]
            worker.connect()
            worker.poll()
            self.clock.advance(10)
            worker.poll()
            worker.poll()
            aid = worker.hold()["held_assignment_id"]
            native = self.native.entries()[0][1]["native_id"]
            upload = worker.router.request("POST","uploads",{"name":"result.bin","size_bytes":len(content),
                                                  "sha256":hashlib.sha256(content).hexdigest()},guid())
            for offset in range(0,len(content),256*1024):
                result = worker.router.request("PATCH","uploads/"+upload["upload_id"],content[offset:offset+256*1024],
                                               headers={"Upload-Offset":str(offset)})
            fid = result["attachment_id"]
            expected = worker.report(native,tid,aid,secret_summary,artifact_ids=[fid])
            self.assertEqual(manager.router.request("GET","attachments/"+fid)["bytes"],content)
            pending = manager.inspect(tid)["task"]
            manager.approve(guid(),tid,pending["revision"])
            self.assertEqual(manager.inspect(tid)["task"]["state"],"closed")

            blocked = manager.create(guid(),"Blocked task","Explain why","oguri")["task_id"]
            worker.connect()
            worker.poll()
            self.clock.advance(10)
            worker.poll()
            worker.poll()
            blocked_aid = worker.hold()["held_assignment_id"]
            native_blocked = self.native.entries()[-1][1]["native_id"]
            self.assertEqual(worker.report(native_blocked,blocked,blocked_aid,"No test input",outcome="blocked")["task_state"],"blocked")
            self.assertEqual(expected["release_assignment_id"],aid)

        proof = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        clean = audit_handoff(self.snapshot,proof)
        self.assertFalse(clean["needs_review"])
        cases = (
            ("receipt","worker:oguri","UPDATE outbox SET receipt=json_set(receipt,'$.task_state','closed') WHERE assignment_id=?",
             (aid,),"report_receipt_mismatch"),
            ("missing","worker:oguri","DELETE FROM outbox WHERE assignment_id=?",(aid,),"report_worker_outbox_missing"),
            ("operation","router:main","DELETE FROM operations WHERE key=?",("complete-"+aid,),"report_router_receipt_missing"),
            ("summary","router:main","UPDATE reports SET summary='different' WHERE assignment_id=?",(aid,),"report_router_result_mismatch"),
            ("blocked","router:main","UPDATE pool_entries SET reason='different' WHERE assignment_id=?",
             (blocked_aid,),"report_router_result_mismatch"),
            ("link","router:main","DELETE FROM business_files WHERE kind='report' AND reference_id=?",
             (aid,),"report_artifact_links_mismatch"),
            ("chunks","router:main","UPDATE chunks SET bytes=zeroblob(length(bytes)) WHERE upload_id=? AND start>0",
             (upload["upload_id"],),"report_artifact_unverified"),
            ("grant","router:main","DELETE FROM attachment_grants WHERE actor='hermes' AND attachment_id=?",
             (fid,),"report_artifact_unverified"),
        )
        for name,label,sql,parameters,code in cases:
            with self.subTest(name=name):
                db_path = self.root/(name+".db")
                shutil.copyfile(self.sources[label],db_path)
                with closing(sqlite3.connect(db_path)) as db,db:
                    self.assertEqual(db.execute(sql,parameters).rowcount,1)
                snapshot = self.root/name
                seal = capture({**self.sources,label:db_path},self.revision,snapshot)["manifest_sha256"]
                audit = audit_handoff(snapshot,seal)
                self.assertTrue(audit["needs_review"])
                self.assertIn(code,[issue["issue"] for issue in audit["issues"]])
                worker_issues = audit["workers"][0]["report_receipts"]["issues"]
                self.assertIn(code,[issue["code"] for issue in worker_issues])
                self.assertNotIn(secret_summary,encode(audit))
        evidence = clean["workers"][0]["report_receipts"]
        self.assertEqual(evidence["confirmed"],2)
        self.assertEqual(evidence["verified_artifacts"],1)
        self.assertEqual(evidence["issues"],[])

    def test_feishu_restore_reuses_original_card_and_keeps_known_provider_receipt_after_deadline(self):
        message = {"chat_type":"p2p","chat_id":"chat","message_id":"om_trial_input","message_type":"text",
                   "content":encode({"text":"隔离收件"})}
        event = {"event_id":"trial-progress","kind":"progress","session":self.native.session,
                 "turn_id":"trial-turn","text":"第一次输出"}
        with self.stack(self.sources) as (_m,w,_source,_mail,inbound,views):
            w.connect()
            w.poll()
            inbound.record("app","user","trainer",message)
            receipt = inbound.deliver(message["message_id"])
            with w.db.transaction() as db:
                original = display.append(db,event)
            self.platform.lose_next = True
            with self.assertRaisesRegex(ConnectionError,"platform accepted"):
                views.poll()
            key, = self.platform.messages
            provider_mid,_,card = self.platform.messages[key]
            self.assertEqual(card["header"]["title"]["content"],"工位-输出")
        checksum = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        pending, = audit_handoff(self.snapshot,checksum)["workers"]
        self.assertEqual(pending["native_session_pending"]["platform_display_attempts"],1)
        self.assertEqual(pending["native_session_pending"]["platform_known_receipts_uncommitted"],0)
        self.assertFalse(pending["native_session_ledger_clear"])
        with self.assertRaisesRegex(ValueError,"platform_display_attempts"):
            stage_native_handoff(self.snapshot,checksum,"oguri",self.native.session,"other",self.dest)
        recovered = restore(self.snapshot,checksum,self.dest)
        self.clock.advance(6)

        with self.stack(recovered) as (_m,w,_source,_mail,inbound,views):
            views.name = "恢复后的名称"
            self.assertEqual(views.poll(),1)
            self.assertEqual(views.deliver(original["seq"],event),provider_mid)
            self.assertEqual(self.platform.calls,[("send",provider_mid),("send",provider_mid)])
            inbound.record("app","user","trainer",message)
            self.assertEqual(inbound.deliver(message["message_id"]),receipt)
            self.assertEqual(len(self.native.entries()),1)
            with w.db.transaction() as db:
                command = worker_commands.record(db,"om_menu_fixture","/model","/model",self.native.session,"模型列表",
                    menu=(self.native.session,"models",1,encode({"1":"fixture/model"}),None))
            route = views.worker.request

            def lose_worker_ack(method,path,*args,**kwargs):
                result = route(method,path,*args,**kwargs)
                if path=="worker/model-menu-receipt":
                    raise ConnectionError("fixture lost Worker menu acknowledgement")
                return result

            views.worker.request = lose_worker_ack
            with self.assertRaisesRegex(ConnectionError,"lost Worker menu acknowledgement"):
                views.poll()
            menu_seq = command["output"]["seq"]
            with views.db.transaction() as db:
                mid, = db.execute("SELECT platform_mid FROM view_writes WHERE seq=?",(menu_seq,)).fetchone()
                self.assertIsNone(db.execute("SELECT mid FROM view_attempt WHERE seq=?",(menu_seq,)).fetchone()[0])
            self.assertEqual(self.platform.calls[-1],("send",mid))
            sends = list(self.platform.calls)

        with_receipt = self.root/"provider-receipt"
        sha = capture(recovered,self.revision,with_receipt)["manifest_sha256"]
        waiting, = audit_handoff(with_receipt,sha)["workers"]
        self.assertEqual(waiting["native_session_pending"]["platform_known_receipts_uncommitted"],1)
        self.assertEqual(waiting["feishu_receipts"]["provider_outputs_awaiting_commit"],1)
        self.assertEqual(waiting["feishu_receipts"]["issues"],[])
        final = restore(with_receipt,sha,self.root/"after-known-receipt")
        self.clock.advance(3601)
        with self.stack(final) as (_m,w,_source,_mail,_inbound,views):
            self.assertEqual(views.poll(),1)
            self.assertEqual(views.poll(),0)
            with w.db.transaction() as db:
                worker_mid, = db.execute("SELECT platform_mid FROM platform_model_menu WHERE display_seq=?",
                                         (menu_seq,)).fetchone()
            self.assertEqual(worker_mid,mid)
            self.assertEqual(self.platform.calls,sends)
        settled = self.root/"settled-platform"
        seal = capture(final,self.revision,settled)["manifest_sha256"]
        finished, = audit_handoff(settled,seal)["workers"]
        self.assertTrue(finished["native_session_ledger_clear"],finished)
        self.feishu_evidence = {"snapshot_with_unconfirmed_send":checksum,"snapshot_with_known_receipt":sha,
                                "settled_snapshot":seal,"initial_request_key":key,"initial_platform_mid":provider_mid,
                                "menu_platform_mid":mid,"worker_bound_menu_mid":worker_mid,"native_inputs":1,
                                "remote_operations":self.platform.calls,"original_payload_reused":True,
                                "known_platform_receipt_reused_after_seconds":3601,
                                "boundary":"Actual Worker HTTP and SQLite, simulated Feishu provider and native host."}

    def test_feishu_image_upload_backoff_survives_snapshot_without_reordering_messages(self):
        digest = hashlib.sha256(PNG).hexdigest()
        with self.stack(self.sources) as (_m,w,_source,_mail,_inbound,views):
            w.connect()
            w.poll()
            upload = views.worker.request("POST","worker/media/uploads",
                {"name":"fixture.png","size_bytes":len(PNG),"sha256":digest},"test-image")
            image = views.worker.request("PATCH","worker/media/uploads/"+upload["upload_id"],PNG,
                                         headers={"Upload-Offset":"0"})
            frame = {"session":self.native.session,"turn_id":"test-turn"}
            with w.db.transaction() as db:
                display.append(db,{**frame,"event_id":"before","kind":"progress","text":"图前"})
                middle = display.append(db,{**frame,"event_id":"image","kind":"image",
                                            "attachment_id":image["attachment_id"]})
                display.append(db,{**frame,"event_id":"after","kind":"progress","text":"图后"})
            self.platform.lose_upload = True
            with self.assertRaisesRegex(ConnectionError,"upload acknowledgement lost"):
                views.poll()
            self.assertEqual(self.platform.images,[PNG])
            self.assertEqual(len(self.platform.messages),1)

        checksum = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        pending, = audit_handoff(self.snapshot,checksum)["workers"]
        self.assertEqual(pending["native_session_pending"]["platform_image_uploads_unconfirmed"],1)
        self.assertFalse(pending["native_session_ledger_clear"])
        recovered = restore(self.snapshot,checksum,self.dest)
        with self.stack(recovered) as (_m,_w,_source,_mail,_inbound,views):
            self.clock.advance(4)
            self.assertEqual(views.poll(),0)
            self.assertEqual(self.platform.images,[PNG])
            self.clock.advance(1)
            self.platform.lose_upload = True
            with self.assertRaisesRegex(ConnectionError,"upload acknowledgement lost"):
                views.poll()
            self.clock.advance(9)
            self.assertEqual(views.poll(),0)
            self.assertEqual(self.platform.images,[PNG,PNG])
            self.assertEqual(len(self.platform.messages),1)
            self.clock.advance(1)
            self.assertEqual(views.poll(),2)
            self.assertEqual(views.poll(),0)
            with views.db.transaction() as db:
                stored, = db.execute("SELECT image_key FROM view_images WHERE seq=?",(middle["seq"],)).fetchone()
            self.assertEqual(stored,"img_fixture_3")
            before,image,after = self.platform.messages.values()
            self.assertEqual([before[1],image[1],after[1]],["interactive","image","interactive"])
            self.assertEqual(image[2],{"image_key":stored})
            self.assertEqual([card[2]["elements"][0]["text"]["content"] for card in (before,after)],["图前","图后"])
            self.assertEqual(self.platform.images,[PNG]*3)
        settled = self.root/"settled-image"
        seal = capture(recovered,self.revision,settled)["manifest_sha256"]
        finished, = audit_handoff(settled,seal)["workers"]
        self.assertEqual(finished["native_session_pending"]["platform_image_uploads_unconfirmed"],0)
        self.assertTrue(finished["native_session_ledger_clear"],finished)
        self.image_evidence = {"snapshot_with_unconfirmed_upload":checksum,"settled_snapshot":seal,
                               "worker_attachment_sha256":digest,"platform_assets":len(self.platform.images),
                               "platform_operations":self.platform.calls,"confirmed_asset_key":stored,
                               "platform_message_kinds":[message[1] for message in self.platform.messages.values()],
                               "pending_uploads":[pending["native_session_pending"]["platform_image_uploads_unconfirmed"],
                                                  finished["native_session_pending"]["platform_image_uploads_unconfirmed"]],
                               "boundary":"Actual Worker HTTP and SQLite, simulated Feishu provider, native host and clock."}

    def test_wrong_role_active_writer_or_incomplete_inventory_preserves_destination(self):
        with self.stack(self.sources):
            pass
        def backup(sources):
            return capture(sources,self.revision,self.snapshot)
        with self.assertRaisesRegex(ValueError,"five component roles"):
            backup({k:v for k,v in self.sources.items() if k!="worker:oguri"})
        with self.assertRaisesRegex(ValueError,"Feishu ledger must have its named Worker"):
            backup({"feishu:other" if key=="feishu:oguri" else key:value for key,value in self.sources.items()})
        validate_set([k.replace(":oguri",":OguriCap") for k in self.sources])
        with self.assertRaisesRegex(ValueError,"case-insensitive"):
            validate_set([*self.sources,"worker:Oguri","agent:Oguri"])
        with self.assertRaisesRegex(ValueError,"schema"):
            backup(dict(self.sources,**{"worker:oguri":self.sources["trigger:mail"],"trigger:mail":self.sources["worker:oguri"]}))
        with state_writer(self.sources["worker:oguri"]):
            with self.assertRaisesRegex(StateWriterBusy,"state_writer_already_running"):
                backup(self.sources)
        self.assertFalse(self.snapshot.exists())
        with closing(sqlite3.connect(self.sources["worker:oguri"])) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                backup(self.sources)
        self.assertFalse(self.snapshot.exists())
        result = backup(self.sources)
        checksum = result["manifest_sha256"]
        with self.assertRaisesRegex(ValueError,"manifest"):
            restore(self.snapshot,"0"*64,self.dest)
        file = self.snapshot/filename("trigger:mail")
        with file.open("ab") as out:
            out.write(b"changed")
        with self.assertRaisesRegex(ValueError,"bytes"):
            restore(self.snapshot,checksum,self.dest)
        self.assertFalse(self.dest.exists())

    def test_restore_previous_release_only_before_trial_changes(self):
        with self.stack(self.sources) as (_,_,source,_,_,_):
            source.record("Info","快照之前")
        checksum = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        trial = restore(self.snapshot,checksum,self.root/"trial")
        previous = rollback(self.snapshot,checksum,trial,self.dest)
        with self.stack(previous) as (_,_,source,_,_,_):
            self.assertEqual(len(source.pending()),1)
        with self.stack(trial) as (_,_,source,_,_,_):
            source.record("Info","快照之后")
        with self.assertRaisesRegex(ValueError,"trial state changed"):
            rollback(self.snapshot,checksum,trial,self.root/"refused")
        self.assertFalse((self.root/"refused").exists())

    def test_offline_handoff_audit_flags_lost_w04_receipt_without_changing_snapshot(self):
        with self.stack(self.sources) as (m,w,_source,_mail,_platform,_views):
            tid = m.create(guid(),"Handoff review","Report under test","oguri")["task_id"]
            w.connect()
            w.poll()
            self.clock.advance(10)
            w.poll()
            w.poll()
            aid = w.hold()["held_assignment_id"]
            native_id = self.native.entries()[0][1]["native_id"]
            transport = w.router.request
            def lose_report(method,path,*args,**kwargs):
                response = transport(method,path,*args,**kwargs)
                if path==f"tasks/{tid}/report":
                    raise ConnectionError("isolated report acknowledgement lost")
                return response
            w.router.request = lose_report
            with self.assertRaises(ConnectionError):
                w.report(native_id,tid,aid,"Private test result")
            self.assertEqual(m.inspect(tid)["task"]["state"],"completed_reported")
            self.assertEqual(w.hold()["held_assignment_id"],aid)
        checksum = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        seen = audit_handoff(self.snapshot,checksum)
        self.assertTrue(seen["needs_review"])
        self.assertEqual([row["issue"] for row in seen["issues"]],["assignment_mismatch","report_receipt_pending"])
        self.assertEqual(seen["workers"][0]["report_receipts"]["awaiting_worker_commit"],1)
        self.assertNotIn("Private test result",encode(seen))
        self.assertEqual(seen["workers"][0]["worker_assignment"],{"task_id":tid,"assignment_id":aid})
        self.assertIsNone(seen["workers"][0]["router_assignment"])
        self.assertEqual(audit_handoff(self.snapshot,checksum),seen)
        restored = restore(self.snapshot,checksum,self.dest)
        with self.stack(restored) as (m,w,_source,_mail,_platform,_views):
            w.connect()
            w.poll()
            self.assertIsNone(w.hold()["held_task_id"])
            self.assertEqual(w.report(native_id,tid,aid,"Private test result")["task_state"],"completed_reported")
            notice, = m.router.frames(0)
            self.assertEqual(notice["payload"]["task_id"],tid)

    def test_offline_handoff_audit_detects_a_swapped_adapter_with_no_original_receipt(self):
        with self.stack(self.sources) as (m,w,_source,_mail,_platform,_views):
            tid = m.create(guid(),"Bound receipt check","Preserve the original receipt","oguri")["task_id"]
            w.connect()
            w.poll()
            self.clock.advance(10)
            w.poll()
            w.poll()
            self.assertEqual(w.hold()["held_task_id"],tid)
        alternate = self.root/"separate-ledger.db"
        HostAdapter(alternate,self.native,self.native.session)
        checksum = capture({**self.sources,"agent:oguri":alternate},self.revision,self.snapshot)["manifest_sha256"]
        seen = audit_handoff(self.snapshot,checksum)
        self.assertTrue(seen["needs_review"])
        self.assertEqual(seen["issues"],[{"worker_id":"oguri","issue":"adapter_receipt_unconfirmed"}])
        self.assertEqual(seen["workers"][0]["worker_assignment"],seen["workers"][0]["router_assignment"])

    def test_handoff_checks_the_router_and_platform_before_writing_a_copy(self):
        with self.stack(self.sources) as (manager,worker,_source,_mail,platform,_views):
            worker.adapter.expected_session = self.native.session
            pinned = Worker(worker.db.path,worker.worker_id,worker.router,worker.adapter)
            service = WorkerService(pinned,self.clock)
            service.step()
            self.assertEqual(service.host_session,self.native.session)
            receiver_id = "om_pending_for_old_session"
            platform.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":receiver_id,
                "message_type":"text","content":encode({"text":"Awaiting original delivery"})})
            pinned.poll()
            self.clock.advance(10)
            manager.create(guid(),"Await original Worker","Stay on the original side","oguri")
            manager.tasks()
        checksum = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        before = audit_handoff(self.snapshot,checksum)
        worker, = before["workers"]
        self.assertEqual(worker["native_session_pending"]["platform_inputs"],1)
        self.assertEqual(worker["native_session_pending"]["router_reserved_assignments"],1)
        self.assertEqual(worker["native_session_pending"]["router_waiting_deliveries"],1)
        with self.assertRaisesRegex(ValueError,"native handoff still pending"):
            stage_native_handoff(self.snapshot,checksum,"oguri",self.native.session,"next",self.dest)
        self.assertFalse(self.dest.exists())
        self.assertEqual(audit_handoff(self.snapshot,checksum),before)
        self.assertEqual(self.native.entries(),[])

    def test_handoff_waits_for_feishu_to_read_the_worker_display_tail(self):
        original = self.native.session
        with self.stack(self.sources,pin_session=True) as (_m,w,_source,_mail,_inbound,views):
            WorkerService(w,self.clock).step()
            views.poll()
            with w.db.transaction() as db:
                last = display.append(db,{"event_id":"private-tail","kind":"final","session":original,
                                          "turn_id":"fixture-turn","text":"Unpublished private reply"})

        checksum = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        pending, = audit_handoff(self.snapshot,checksum)["workers"]
        self.assertFalse(pending["native_session_ledger_clear"],pending)
        self.assertEqual(pending["native_session_pending"]["platform_display_unseen"],1)
        self.assertEqual(pending["native_session_pending"]["platform_display_attempts"],0)
        self.assertNotIn("Unpublished private reply",encode(pending))
        with self.assertRaisesRegex(ValueError,"platform_display_unseen"):
            stage_native_handoff(self.snapshot,checksum,"oguri",original,"next",self.dest)
        self.assertFalse(self.dest.exists())

        restored = restore(self.snapshot,checksum,self.root/"display-retry")
        with self.stack(restored,pin_session=True) as (_m,_w,_source,_mail,_inbound,views):
            self.assertEqual(views.poll(),1)
            self.assertEqual(views.poll(),0)
            with views.db.transaction() as db:
                mid, = db.execute("SELECT mid FROM view_attempt WHERE seq=?",(last["seq"],)).fetchone()
            self.assertEqual(self.platform.calls.count(("send",mid)),1)
        settled = self.root/"display-settled"
        seal = capture(restored,self.revision,settled)["manifest_sha256"]
        cleared, = audit_handoff(settled,seal)["workers"]
        self.assertTrue(cleared["native_session_ledger_clear"],cleared)
        self.assertEqual(cleared["native_session_pending"]["platform_display_unseen"],0)
        result = stage_native_handoff(settled,seal,"oguri",original,"next",self.dest)
        self.assertEqual(result["expected_session"],"next")
        self.display_evidence = {"pending_snapshot":checksum,"settled_snapshot":seal,"display_seq":last["seq"],
                                 "fixture_platform_mid":mid,"unseen_before_and_after":[1,0],
                                 "platform_writes_for_event":1,"handoff_blocked_before_drain":True,
                                 "handoff_staged_after_drain":True,
                                 "boundary":"Actual Worker HTTP and SQLite, simulated Feishu provider and native host."}

    @unittest.skipUnless(os.name=="posix","DSH private Unix runtime required")
    def test_handoff_rechecks_dsh_workspace_journal_before_publishing(self):
        runtime = self.root/"dsh-runtime"
        runtime.mkdir(mode=0o700)
        (runtime/"assignment-containers").mkdir(mode=0o700)
        journal,identity = dsh_workspace_audit.journal(runtime)
        original = self.native.session
        with self.stack(self.sources,pin_session=True) as (_m,w,_source,_mail,_platform,views):
            WorkerService(w,self.clock).step()
            with w.adapter.db.transaction() as db:
                dsh_workspace_audit.marker(db,original,identity)
            views.poll()
        seal = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        self.assertEqual(verify(self.snapshot,seal)["format"],3)
        runtimes = {"oguri":runtime}
        ready, = audit_handoff(self.snapshot,seal,dsh_runtimes=runtimes)["workers"]
        self.assertTrue(ready["native_session_ledger_clear"],ready)
        aid = guid()
        record = {"session":original,"task_id":guid(),"assignment_id":aid,
                  "container_id":"a"*64,"phase":"active","token":guid()}
        name = hashlib.sha256(encode([original,aid]).encode()).hexdigest()+".json"
        original_restore = state_bundle.restore_files
        def changed(*args,**kwargs):
            original_restore(*args,**kwargs)
            with open(journal/name,"x",opener=lambda p,flags:os.open(p,flags,0o600)) as stream:
                stream.write(encode(record))
        trial = restore(self.snapshot,seal,self.root/"trial")
        operations = {
            "handoff":lambda:stage_native_handoff(self.snapshot,seal,"oguri",original,"next",self.dest,
                                                  dsh_runtimes=runtimes),
            "rollback":lambda:rollback(self.snapshot,seal,trial,self.dest,dsh_runtimes=runtimes),
        }
        for label,operation in operations.items():
            with self.subTest(operation=label), patch.object(state_bundle,"restore_files",side_effect=changed):
                with self.assertRaisesRegex(ValueError,"workspace journal changed during "+label):
                    operation()
                self.assertFalse(self.dest.exists())
                pending, = audit_handoff(self.snapshot,seal,dsh_runtimes=runtimes)["workers"]
                self.assertEqual(pending["native_session_pending"]["dsh_assignment_workspaces"],1)
                self.assertNotIn(record["token"],encode(pending))
            (journal/name).unlink()

    def test_offline_handoff_and_rollback_reserve_the_codex_workspace_writer(self):
        runtime = self.root/"codex-runtime"
        runtime.mkdir(mode=0o700)
        (runtime/"assignment-containers").mkdir(mode=0o700)
        _,identity = dsh_workspace_audit.journal(runtime)
        session = self.native.session
        with self.stack(self.sources,pin_session=True) as (_m,w,_source,_mail,_platform,views):
            WorkerService(w,self.clock).step()
            with w.adapter.db.transaction() as db:
                dsh_workspace_audit.marker(db,session,identity)
            views.poll()
        seal = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        trial = restore(self.snapshot,seal,self.root/"trial")
        runtimes = {"oguri":runtime}
        writer = runtime/"codex-workspace"
        handoff = self.root/"handoff"
        operations = (
            (handoff,lambda:stage_native_handoff(self.snapshot,seal,"oguri",session,"next",handoff,
                                                 dsh_runtimes=runtimes)),
            (self.dest,lambda:rollback(self.snapshot,seal,trial,self.dest,dsh_runtimes=runtimes)),
        )
        # Codex takes this lock before its Node child publishes the health socket.
        with state_writer(writer):
            self.assertFalse((runtime/"dsh.sock").exists())
            report = audit_handoff(self.snapshot,seal,dsh_runtimes=runtimes)
            self.assertIn({"worker_id":"oguri","issue":"dsh_workspace_host_still_running"},report["issues"])
            for destination,operation in operations:
                with self.assertRaisesRegex(ValueError,"dsh_workspace_host_still_running"):
                    operation()
                self.assertFalse(destination.exists())

        original_output = state_bundle.output_directory
        publications = []

        @contextmanager
        def check_publication(destination):
            with original_output(destination) as stage:
                yield stage
                with self.assertRaises(StateWriterBusy), state_writer(writer,existing=True):
                    pass
                publications.append(destination)

        with patch.object(state_bundle,"output_directory",check_publication):
            for destination,operation in operations:
                operation()
                self.assertTrue(destination.is_dir())
        self.assertEqual(publications,[handoff,self.dest])
        with state_writer(writer,existing=True):
            self.assertFalse((runtime/"dsh.sock").exists())

    def test_workspace_audit_checks_private_file_and_original_journal_identity(self):
        runtime = self.root/"workspace-runtime"
        runtime.mkdir(mode=0o700)
        directory = runtime/"assignment-containers"
        directory.mkdir(mode=0o700)
        _,identity = dsh_workspace_audit.journal(runtime)
        if os.name=="nt":
            self.assertEqual(dsh_workspace_audit.inspect(runtime,{identity}),
                             (None,"dsh_workspace_journal_unconfirmed"))
        with state_writer(runtime/"dsh-workspace"):
            pass
        session,task,assignment = "isolated-workspace-ledger",guid(),guid()
        record = {"session":session,"task_id":task,"assignment_id":assignment,"phase":"sealed",
                  "token":guid(),"container_id":"a"*64}
        filename = hashlib.sha256(encode([session,assignment]).encode()).hexdigest()+".json"
        path = directory/filename
        with open(path,"x",opener=lambda name,flags:os.open(name,flags,0o600)) as stream:
            stream.write(encode(record))
        observed,issue = dsh_workspace_audit.inspect(runtime,{identity})
        self.assertIsNone(issue)
        self.assertEqual(observed["pending"],[])
        self.assertRegex(observed["fingerprint"],r"^[0-9a-f]{64}$")

        foreign = self.root/"foreign-workspace-runtime"
        foreign.mkdir(mode=0o700)
        (foreign/"assignment-containers").mkdir(mode=0o700)
        self.assertEqual(dsh_workspace_audit.inspect(foreign,{identity}),
                         (None,"dsh_workspace_journal_mismatch"))

        if os.name=="nt":
            icacls = Path(os.environ["SystemRoot"])/"System32/icacls.exe"
            change = subprocess.run([icacls,path,"/grant","*S-1-1-0:(R)"],capture_output=True,timeout=15)
            self.assertEqual(change.returncode,0,change.stderr)
        else:
            path.chmod(0o644)
        self.assertEqual(dsh_workspace_audit.inspect(runtime,{identity}),
                         (None,"dsh_workspace_journal_unconfirmed"))

    def test_handoff_detects_different_display_payload_or_cursor_ahead_of_worker(self):
        with self.stack(self.sources) as (_m,w,_source,_mail,_inbound,views):
            w.connect()
            w.poll()
            event = {"event_id":"display-origin","kind":"final","session":self.native.session,
                     "turn_id":"fixture-turn","text":"Original private display"}
            with w.db.transaction() as db:
                receipt = display.append(db,event)
            views.poll()

        worker_db = Database(self.sources["worker:oguri"],"")
        changes = {"foreign-payload":("UPDATE display_events SET payload=? WHERE seq=?",
                                      (encode(dict(event,text="Different display")),receipt["seq"])),
                   "older-worker":("DELETE FROM display_events WHERE seq=?",(receipt["seq"],))}
        for name,(query,args) in changes.items():
            with self.subTest(name=name):
                with worker_db.transaction() as db:
                    db.execute(query,args)
                path = self.root/name
                seal = capture(self.sources,self.revision,path)["manifest_sha256"]
                audited = audit_handoff(path,seal)
                worker, = audited["workers"]
                self.assertTrue(audited["needs_review"])
                self.assertIn("platform_display_ledger_mismatch",worker["issues"])
                self.assertFalse(worker["native_session_ledger_clear"])
                self.assertNotIn("Different display",encode(audited))

    def test_handoff_reconciles_feishu_receipts_even_when_display_prefix_matches(self):
        session = self.native.session
        incoming = "om_private_audit"
        with self.stack(self.sources,pin_session=True) as (_m,worker,_source,_mail,inbound,views):
            WorkerService(worker,self.clock).step()
            for mid,text in ((incoming,"Original private message"),("om_command_audit","/status")):
                inbound.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":mid,
                    "message_type":"text","content":encode({"text":text})})
            inbound.deliver("om_command_audit")
            request = inbound.worker.request

            def lose_private_ack(method,path,*args,**kwargs):
                reply = request(method,path,*args,**kwargs)
                if path=="worker/chat":
                    raise ConnectionError("lost the Worker reply after native receipt")
                return reply

            inbound.worker.request = lose_private_ack
            with self.assertRaisesRegex(ConnectionError,"lost the Worker reply after native receipt"):
                inbound.deliver(incoming)
            inbound.worker.request = request
            with worker.db.transaction() as db:
                menu = worker_commands.record(db,"om_audit_menu","/model","/model",session,"Choices",
                    menu=(session,"models",1,encode({"1":"fixture/model"}),None))
                for idx,text in enumerate(("first","second")):
                    display.append(db,{"event_id":f"audit-progress-{idx}","kind":"progress","session":session,
                                       "turn_id":"original-turn","text":text})
                final = display.append(db,{"event_id":"audit-final","kind":"final","session":session,
                                           "turn_id":"original-turn","text":"Final private text"})
            views.poll()
        waiting = self.root/"receipt-lag"
        seal = capture(self.sources,self.revision,waiting)["manifest_sha256"]
        pending, = audit_handoff(waiting,seal)["workers"]
        self.assertFalse(pending["native_session_ledger_clear"])
        self.assertEqual(pending["feishu_receipts"]["issues"],[])
        self.assertEqual(pending["feishu_receipts"]["worker_inputs_awaiting_platform"],1)
        sends = list(self.platform.calls)
        resumed = restore(waiting,seal,self.root/"resumed-receipt")
        with self.stack(resumed,pin_session=True) as (_m,_w,_source,_mail,inbound,views):
            self.assertEqual(inbound.deliver(incoming),self.native.entries()[0][1])
            self.assertEqual(views.poll(),0)
        self.assertEqual(self.platform.calls,sends)
        seal = capture(resumed,self.revision,self.snapshot)["manifest_sha256"]
        baseline, = audit_handoff(self.snapshot,seal)["workers"]
        self.assertTrue(baseline["native_session_ledger_clear"],baseline)
        self.assertEqual(baseline["feishu_receipts"],{"confirmed_inputs":2,"worker_inputs_awaiting_platform":0,
            "confirmed_outputs":final["seq"],"provider_outputs_awaiting_commit":0,"issues":[]})
        sends = list(self.platform.calls)
        cases = (
            ("inbound","feishu","UPDATE platform_events SET receipt=json_set(receipt,'$.native_id','other') WHERE mid=?",
             (incoming,),{"code":"platform_input_receipt_mismatch","message_id":incoming}),
            ("provider","feishu","UPDATE view_writes SET platform_mid='om_wrong' WHERE seq=?",
             (final["seq"],),{"code":"platform_output_receipt_mismatch","display_seq":final["seq"]}),
            ("menu","worker","UPDATE platform_model_menu SET platform_mid='om_wrong' WHERE display_seq=?",
             (menu["output"]["seq"],),{"code":"platform_worker_menu_receipt_mismatch","display_seq":menu["output"]["seq"]}),
            ("patch","feishu","UPDATE view_writes SET request=json_set(request,'$.target','om_wrong') WHERE seq=?",
             (final["seq"]-1,),{"code":"platform_output_receipt_mismatch","display_seq":final["seq"]-1}),
            ("binding","feishu","UPDATE binding SET value=json_set(value,'$[2]','wrong-chat')",(),
             {"code":"platform_receipt_binding_mismatch"}),
        )
        observed = {}
        for name,role,query,args,expected in cases:
            with self.subTest(name=name):
                restored = restore(self.snapshot,seal,self.root/(name+"-test-state"))
                with closing(sqlite3.connect(restored[role+":oguri"])) as db,db:
                    db.execute(query,args)
                snapshot = self.root/(name+"-test-snapshot")
                digest = capture(restored,self.revision,snapshot)["manifest_sha256"]
                result = audit_handoff(snapshot,digest)
                row, = result["workers"]
                self.assertTrue(result["needs_review"])
                self.assertEqual(row["feishu_receipts"]["issues"],[expected])
                self.assertEqual(row["native_session_pending"]["platform_display_unseen"],0)
                self.assertNotIn("Final private text",encode(result))
                destination = self.root/(name+"-would-publish")
                with self.assertRaisesRegex(ValueError,expected["code"]):
                    stage_native_handoff(snapshot,digest,"oguri",session,"next",destination)
                self.assertFalse(destination.exists())
                observed[name] = row["feishu_receipts"]["issues"]
        self.assertEqual(self.platform.calls,sends)
        self.assertEqual(len(self.native.entries()),1)
        if evidence := os.environ.get("MOWULINK_FEISHU_RECEIPT_AUDIT_EVIDENCE"):
            Path(evidence).write_text(json.dumps({"first_reply_lost":pending["feishu_receipts"],
                "valid":baseline["feishu_receipts"],"rejected":observed,
                "native_input_count":len(self.native.entries()),"additional_platform_calls":0,
                "handoff_refused_for_every_mismatch":True,"boundary":"Actual SQLite and Worker HTTP. Feishu and native host are fixtures."},
                ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_handoff_reconciles_the_original_feishu_trainer_answer(self):
        closes = []
        submitted = "Do not expose this trainer answer"
        original = {"session":self.native.session,"turn_id":"question-turn","native_question_id":"fixture-question",
                    "text":"Choose a test answer","options":[]}

        def close(question,answer):
            self.assertEqual(question,original)
            self.assertEqual(answer,{"mode":"text","text":submitted})
            scope = {key:question[key] for key in ("session","turn_id","native_question_id")}
            proof = {**scope,"answer":answer,"closed":True,"receipt_id":guid()}
            closes.append(proof)
            return proof

        def audit(paths,name):
            path = self.root/name
            seal = capture(paths,self.revision,path)["manifest_sha256"]
            result = audit_handoff(path,seal)
            self.assertNotIn(submitted,encode(result))
            return path,seal,result

        self.native.resolve_question = close
        with self.stack(self.sources,pin_session=True) as (_m,worker,_source,_mail,inbound,views):
            WorkerService(worker,self.clock).step()
            qid = user_questions.open_native(worker,original)["question_id"]
            views.poll()
            with views.db.transaction() as db:
                mid, = db.execute("SELECT mid FROM question_cards WHERE qid=?",(qid,)).fetchone()
            sends = list(self.platform.calls)
            actions = Actions(views,inbound)
            self.assertEqual(actions.record("app","trainer","chat",mid,{"question_id":qid,"mode":"text"},
                                            {"reply":submitted}),"pending")
            request = views.worker.request

            def lose_answer_ack(method,path,*args,**kwargs):
                result = request(method,path,*args,**kwargs)
                if path=="worker/trainer-answer":
                    raise ConnectionError("The Worker saved the native proof but the platform lost the reply")
                return result

            with patch.object(views.worker,"request",side_effect=lose_answer_ack):
                actions.flush()
            self.assertEqual(len(closes),1)
        pending,seal,missing = audit(self.sources,"lost-trainer-answer-receipt")
        self.assertTrue(missing["needs_review"])
        received = missing["workers"][0]["feishu_receipts"]
        self.assertEqual(received["trainer_answers"]["awaiting_platform_commit"],1)
        self.assertIn({"code":"platform_trainer_answer_receipt_pending","question_id":qid},received["issues"])

        restored = restore(pending,seal,self.dest)
        self.clock.advance(16)
        with self.stack(restored,pin_session=True) as (_m,_worker,_source,_mail,inbound,views):
            Actions(views,inbound).flush()
            self.assertEqual(views.poll(),1)
            self.assertEqual(views.poll(),0)
        self.assertEqual(len(closes),1)
        self.assertEqual(self.platform.calls,[*sends,("patch",mid)])
        ready,digest,settled = audit(restored,"confirmed-trainer-answer")
        self.assertFalse(settled["needs_review"],settled)
        self.assertEqual(settled["workers"][0]["feishu_receipts"]["trainer_answers"]["confirmed"],1)

        for role,change in (
            ("feishu","UPDATE card_answers SET mid='om_different_card'"),
            ("feishu","UPDATE card_answers SET answer=json_set(answer,'$.text','other')"),
            ("feishu","UPDATE question_cards SET question=json_set(question,'$.turn_id','different-turn')"),
            ("worker","UPDATE trainer_questions SET proof=NULL"),
            ("worker","UPDATE trainer_questions SET proof=json_set(proof,'$.turn_id','different-turn')"),
        ):
            with self.subTest(change=change):
                use = self.root/("tampered-state-"+guid())
                bad = restore(ready,digest,use)
                with closing(sqlite3.connect(bad[role+":oguri"])) as db,db:
                    db.execute(change)
                snapshot,bad_seal,result = audit(bad,"tampered-snapshot-"+guid())
                self.assertTrue(result["needs_review"])
                self.assertIn({"code":"platform_trainer_answer_receipt_mismatch","question_id":qid},
                              result["workers"][0]["feishu_receipts"]["issues"])
                with self.assertRaisesRegex(ValueError,"platform_trainer_answer_receipt_mismatch"):
                    stage_native_handoff(snapshot,bad_seal,"oguri",self.native.session,"next-session",use/"published")

        with self.stack(restored,pin_session=True) as (_m,worker,_source,_mail,inbound,views):
            other = dict(original,turn_id="ended-question-turn")
            ended = user_questions.open_native(worker,other)["question_id"]
            views.poll()
            with views.db.transaction() as db:
                other_mid, = db.execute("SELECT mid FROM question_cards WHERE qid=?",(ended,)).fetchone()
            actions = Actions(views,inbound)
            self.assertEqual(actions.record("app","trainer","chat",other_mid,{"question_id":ended,"mode":"skip"},None),
                             "pending")
            scope = {k:other[k] for k in ("session","turn_id","native_question_id")}
            user_questions.observe_proof(worker,{"question_id":ended,"proof":{
                **scope,"receipt_id":guid(),"answer":None,"closed":True,"state":"ended_unanswered"}})
            actions.flush()
            views.poll()
        self.assertEqual(len(closes),1)
        _,_,ended_snapshot = audit(restored,"closed-without-trainer-answer")
        self.assertFalse(ended_snapshot["needs_review"],ended_snapshot)
        self.assertEqual(ended_snapshot["workers"][0]["feishu_receipts"]["trainer_answers"]["rejected"],1)

    def test_handoff_refuses_codex_approval_never_registered_with_router(self):
        session = self.native.session
        with self.stack(self.sources,pin_session=True) as (_manager,worker,_source,_mail,_platform,_views):
            worker.connect()
            approvals = CodexApprovals(None,session,adapter_db=self.sources["agent:oguri"])
            request = {"session":session,"turn_id":"original-turn","item_id":"original-item",
                       "command":"pwd","cwd":"/private-sandbox","environment_id":None,"decisions":["cancel"]}
            approvals.remember(41,request)
            approvals.remember(41,request)
        # Reopening the adapter does not recover a live request or discard its pending identity.
        reopened = CodexApprovals(None,session,adapter_db=self.sources["agent:oguri"])
        self.assertFalse(reopened.live)
        seal = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        worker, = audit_handoff(self.snapshot,seal)["workers"]
        self.assertEqual(worker["native_session_pending"]["router_approvals"],0)
        self.assertEqual(worker["native_session_pending"]["codex_approval_closures"],1)
        self.assertFalse(worker["native_session_ledger_clear"])
        with self.assertRaisesRegex(ValueError,"codex_approval_closures"):
            stage_native_handoff(self.snapshot,seal,"oguri",session,"next",self.dest)
        self.assertFalse(self.dest.exists())
        db = self.snapshot/filename("agent:oguri")
        self.assertNotIn(b"/private-sandbox",db.read_bytes())

    def test_audit_matches_codex_approval_closure_to_original_router_row(self):
        session = self.native.session
        relay = CodexApprovals(None,session,adapter_db=self.sources["agent:oguri"])
        request = {"kind":"worker_report","server":"test-mcp","session":session,"turn_id":guid(),"item_id":"mcp:0",
                   "task_id":guid(),"assignment_id":guid(),"arguments_digest":"1"*64,"decisions":["accept","cancel"]}
        relay.remember(0,request)
        original = relay.live[0]
        digest = original.digest
        route = "approvals/oguri/"+original.id
        with self.stack(self.sources,pin_session=True,approval_operator=True) as (_manager,worker,*_):
            worker.connect()
            reply = worker.router.request("POST",route,{"session_id":worker.session,"request":request})
            self.assertEqual(reply["state"],"assignment_inactive")
            closed = worker.router.request("PUT",route+"/closed",{"session_id":worker.session,"request_digest":digest,
                "attempted_response":{"source":"assignment_inactive","decision":"cancel"}})
        receipt = hashlib.sha256(encode(closed).encode()).hexdigest()
        relay.save_receipt(original.id,digest,receipt)

        def audit(name):
            path = self.root/name
            seal = capture(self.sources,self.revision,path)["manifest_sha256"]
            return audit_handoff(path,seal)

        clean = audit("confirmed-approval")
        self.assertFalse(clean["needs_review"],clean)
        self.assertEqual(clean["workers"][0]["approval_receipts"],{"confirmed":1,"issues":[]})
        with relay.ledger.transaction() as db:
            db.execute("UPDATE codex_approval_intents SET router_receipt_digest=?",("0"*64,))
        mismatch = audit("incorrect-approval-digest")
        self.assertTrue(mismatch["needs_review"])
        self.assertEqual(mismatch["workers"][0]["approval_receipts"],{"confirmed":0,"issues":[
            {"approval_id":original.id,"code":"codex_approval_closure_mismatch"}]})
        with relay.ledger.transaction() as db:
            db.execute("DELETE FROM codex_approval_intents WHERE approval_id=?",(original.id,))
        missing = audit("missing-approval-intent")
        self.assertTrue(missing["needs_review"])
        self.assertEqual(missing["workers"][0]["approval_receipts"],{"confirmed":0,"issues":[
            {"approval_id":original.id,"code":"codex_report_approval_intent_missing"}]})

    def test_handoff_refuses_to_copy_an_unconfirmed_native_thread_preparation(self):
        with self.stack(self.sources) as (_manager,worker,_source,_mail,_platform,_views):
            worker.adapter.expected_session = self.native.session
            pinned = Worker(worker.db.path,worker.worker_id,worker.router,worker.adapter)
            WorkerService(pinned,self.clock).step()
            ledger = Database(self.sources["agent:oguri"],CODEX_PREPARATION_SCHEMA)
            with ledger.transaction() as db:
                db.execute("INSERT INTO codex_thread_preparations VALUES (?,?,?,?,?,'issuing',NULL,NULL)",
                           (guid(),self.native.session,"new","Test pending preparation","{}"))
                db.execute("INSERT INTO codex_thread_preparations VALUES (?,?,?,?,?,'prepared',?,?)",
                           (guid(),self.native.session,"fork","Prior preparation","{}",guid(),"{}"))
        checksum = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        worker, = audit_handoff(self.snapshot,checksum)["workers"]
        self.assertFalse(worker["native_session_ledger_clear"])
        self.assertEqual(worker["native_session_pending"]["codex_session_preparations"],1)
        with self.assertRaisesRegex(ValueError,"codex_session_preparations"):
            stage_native_handoff(self.snapshot,checksum,"oguri",self.native.session,"next",self.dest)
        self.assertFalse(self.dest.exists())

    def test_capture_refuses_to_leave_native_session_registry_outside_the_backup(self):
        with self.stack(self.sources):
            root = Database(self.sources["agent:oguri"],FAMILY_SCHEMA)
            with root.transaction() as db:
                db.execute("INSERT INTO codex_managed_session_root VALUES (1,?,?)",
                           (guid(),str(self.root/"private-session-directory")))
        with self.assertRaisesRegex(ValueError,"external native session registry"):
            capture(self.sources,self.revision,self.snapshot)
        self.assertFalse(self.snapshot.exists())

    def test_managed_bundle_preserves_lineage_and_can_only_rollback_unchanged_ledgers(self):
        def command(*args):
            result = subprocess.run([sys.executable,"-B","-m","rewrite.state_bundle",*args],
                                    cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,
                                    check=True,timeout=15)
            return json.loads(result.stdout)

        with self.stack(self.sources):
            pass
        anchor,child,op = guid(),guid(),guid()
        key = os.urandom(32)
        token = derive_callback(key,"oguri",child,op)
        keys = {"oguri":config(self.root,"original-key.json",{"key":key.hex()})}
        wrong = {"oguri":config(self.root,"unrelated-key.json",{"key":os.urandom(32).hex()})}
        directory = self.root/"registry"
        directory.mkdir(mode=0o700)
        root = self.root/"managed-agent.sqlite3"
        child_db = directory/(child+".sqlite3")
        registry = {"oguri":directory}
        sources = dict(self.sources,**{"agent:oguri":root})
        HostAdapter(root,None,anchor)
        HostAdapter(child_db,None,child)
        with Database(root,FAMILY_SCHEMA+CODEX_PREPARATION_SCHEMA).transaction() as db:
            db.execute("INSERT INTO codex_managed_session_root VALUES (1,?,?)",(anchor,str(directory.resolve())))
            proof = {"state":"prepared","source_session":anchor,"operation_id":op,"kind":"new",
                     "session":child,"parent_session":None}
            db.execute("INSERT INTO codex_thread_preparations VALUES (?,?,?,?,?,'prepared',?,?)",
                       (op,anchor,"new","New isolated session","{}",child,encode(proof)))
        with Database(sources["worker:oguri"],"").transaction() as db:
            db.execute("INSERT INTO native_resume VALUES (?,?,?,?,?,?,'done')",("test","/new","/new",op,anchor,child))
            db.execute("INSERT INTO managed_native_sessions VALUES (?,?,?,?)",(child,anchor,op,token_digest(token)))
        enrollment = directory/(child+".json")
        recorded = {"source":anchor,"operation_id":op,"session":child,"callback_token":token}
        write_enrollment(enrollment,recorded)
        with state_writer(directory/"session-registry"):
            with self.assertRaises(StateWriterBusy):
                capture(sources,self.revision,self.snapshot,registries=registry)
        self.assertFalse(self.snapshot.exists())
        with self.assertRaisesRegex(ValueError,"native registry path differs"):
            capture(sources,self.revision,self.snapshot,registries={"oguri":self.root})
        with state_writer(child_db):
            with self.assertRaises(StateWriterBusy):
                capture(sources,self.revision,self.snapshot,registries=registry)
        write_enrollment(enrollment,dict(recorded,source=guid()))
        with self.assertRaisesRegex(ValueError,"no enrolled parent"):
            capture(sources,self.revision,self.snapshot,registries=registry)
        write_enrollment(enrollment,recorded)
        source_map = config(self.root,"sources.json",{k:str(v) for k,v in sources.items()})
        registry_map = config(self.root,"registries.json",{"oguri":str(directory)})
        key_map = config(self.root,"keys.json",{"oguri":str(keys["oguri"])})
        checksum = command("capture","--sources",source_map,"--managed-registries",registry_map,
                           "--revision",self.revision,"--destination",self.snapshot)["manifest_sha256"]
        manifest = verify(self.snapshot,checksum)
        self.assertEqual(manifest["format"],2)
        self.assertEqual(set(manifest["managed_native"]["oguri"]["children"]),{child})
        self.assertEqual([p.name for p in (self.snapshot/"native--oguri").iterdir()],[child_db.name])
        for private in (token,key.hex(),"callback_token"):
            self.assertNotIn(private,(self.snapshot/"manifest.json").read_text())
        batch = guid()
        hosts = {}
        for host,part in (("adapter",{"agent:oguri":root}),
                          ("router",{k:v for k,v in sources.items() if k!="agent:oguri"})):
            path = self.root/(host+"-part")
            fragment = capture(part,self.revision,path,registries=registry if host=="adapter" else {},
                               batch_id=batch,host_id=host)
            hosts[host] = {"snapshot":str(path),"manifest_sha256":fragment["manifest_sha256"],"components":list(part)}
        plan = {"batch_id":batch,"source_revision":self.revision,"hosts":hosts}
        combined = self.root/"assembled"
        assembled = assemble_hosts(plan,combined)
        assembled_manifest = verify(combined,assembled["manifest_sha256"])
        self.assertEqual((assembled_manifest["format"],assembled_manifest["managed_native"]),
                         (2,manifest["managed_native"]))
        self.assertEqual(list((combined/"native--oguri").iterdir()),[combined/"native--oguri"/child_db.name])
        with Database(sources["worker:oguri"],"").transaction() as db:
            db.execute("UPDATE managed_native_sessions SET callback_digest=?",("0"*64,))
        bad_part = self.root/"mismatched-worker"
        worker_only = {"worker:oguri":sources["worker:oguri"]}
        mismatch = capture(worker_only,self.revision,bad_part,batch_id=batch,host_id="worker")
        router_part = {k:v for k,v in sources.items() if k not in ("worker:oguri","agent:oguri")}
        router_only = self.root/"router-only"
        router_proof = capture(router_part,self.revision,router_only,batch_id=batch,host_id="router")
        bad_hosts = {**hosts,
                     "worker":{"snapshot":str(bad_part),"manifest_sha256":mismatch["manifest_sha256"],
                               "components":list(worker_only)},
                     "router":{"snapshot":str(router_only),"manifest_sha256":router_proof["manifest_sha256"],
                               "components":list(router_part)}}
        with self.assertRaisesRegex(ValueError,"enrollment has no matching native ledger"):
            assemble_hosts(dict(plan,hosts=bad_hosts),self.root/"bad-assembly")
        self.assertFalse((self.root/"bad-assembly").exists())
        with Database(sources["worker:oguri"],"").transaction() as db:
            db.execute("UPDATE managed_native_sessions SET callback_digest=?",(token_digest(token),))
        with self.assertRaisesRegex(ValueError,"supply the original callback key"):
            restore(self.snapshot,checksum,self.dest)
        with self.assertRaisesRegex(ValueError,"key does not match"):
            restore(self.snapshot,checksum,self.dest,managed_keys=wrong)
        self.assertFalse(self.dest.exists())

        trial_dir = self.root/"trial"
        trial = command("restore","--snapshot",self.snapshot,"--manifest-sha256",checksum,
                        "--destination",trial_dir,"--managed-keys",key_map)
        trial_registry = {"oguri":trial_dir/"native--oguri"}
        trial_db = trial_registry["oguri"]/child_db.name
        self.assertEqual(trial_db.read_bytes(),child_db.read_bytes())
        self.assertEqual(read_enrollment(trial_registry["oguri"]/enrollment.name),recorded)
        if os.name=="posix":
            self.assertEqual((trial_registry["oguri"]/enrollment.name).stat().st_mode & 0o777,0o600)
        previous = rollback(self.snapshot,checksum,trial,self.dest,registries=trial_registry,managed_keys=keys)
        with closing(sqlite3.connect(previous["agent:oguri"])) as db:
            self.assertEqual(db.execute("SELECT session,directory FROM codex_managed_session_root").fetchone(),
                             (anchor,str((self.dest/"native--oguri").resolve())))
        with Database(trial_db,"").transaction() as db:
            db.execute("INSERT INTO intents(delivery_id,session,payload,digest) VALUES (?,?,?,?)",
                       (guid(),child,"{}","unconfirmed"))
        refused = self.root/"refused"
        with self.assertRaisesRegex(ValueError,"reconcile managed native sessions"):
            rollback(self.snapshot,checksum,trial,refused,registries=trial_registry,managed_keys=keys)
        self.assertFalse(refused.exists())
        with self.assertRaisesRegex(ValueError,"Worker native handoff"):
            stage_native_handoff(self.snapshot,checksum,"oguri",anchor,child,refused)
        packed = self.snapshot/"native--oguri"/child_db.name
        with Database(packed,"").transaction() as db:
            db.execute("INSERT INTO intents(delivery_id,session,payload,digest) VALUES (?,?,?,?)",
                       (guid(),child,"{}","unconfirmed"))
        with self.assertRaisesRegex(ValueError,"native ledger bytes do not match"):
            verify(self.snapshot,checksum)

    def test_native_session_audit_keeps_unknown_private_and_queued_inputs_for_the_original_host(self):
        self.native.next_turn = True
        self.native.is_busy = True
        private_mid,queue_mid = guid(),guid()
        private_text,queue_text = "Private unknown fixture","Private queued fixture"
        with self.stack(self.sources) as (m,w,_source,_mail,_platform,_views):
            service = WorkerService(w,self.clock)
            service.step()
            original_deliver = self.native.deliver
            def lose_host_receipt(*args,**kwargs):
                original_deliver(*args,**kwargs)
                return None
            self.native.deliver = lose_host_receipt
            self.assertIsNone(w.chat(private_mid,private_text))
            self.native.deliver = original_deliver
            service.handle("platform/oguri","POST","/v1/worker/command",
                           {"message_id":queue_mid,"text":"/queue "+queue_text},{})
            ((_,original),) = self.native.entries()
            self.assertEqual(m.tasks()["items"],[])

        checksum = capture(self.sources,self.revision,self.snapshot)["manifest_sha256"]
        seen = audit_handoff(self.snapshot,checksum)
        self.assertFalse(seen["needs_review"])
        worker, = seen["workers"]
        self.assertFalse(worker["native_session_ledger_clear"])
        self.assertEqual({name:count for name,count in worker["native_session_pending"].items() if count},
                         {"private_chat":1,"queued_chat":1,"adapter_deliveries":1,"platform_display_unseen":2})
        self.assertNotIn(private_text,encode(seen))
        self.assertNotIn(queue_text,encode(seen))
        self.assertEqual(audit_handoff(self.snapshot,checksum),seen)
        with self.assertRaisesRegex(ValueError,"private_chat"):
            stage_native_handoff(self.snapshot,checksum,"oguri",self.native.session,"next",self.dest)
        self.assertFalse(self.dest.exists())

        restored = restore(self.snapshot,checksum,self.dest)
        self.native.is_busy = False
        with self.stack(restored) as (_m,w,_source,_mail,_platform,views):
            self.assertEqual(w.chat(private_mid,private_text),original)
            service = WorkerService(w,self.clock)
            service.step()
            service.step()
            self.assertEqual([payload["text"] for payload,_ in self.native.entries()],[private_text,queue_text])
            views.poll()
        resolved = self.root/"resolved"
        later = capture(restored,self.revision,resolved)["manifest_sha256"]
        clean, = audit_handoff(resolved,later)["workers"]
        self.assertTrue(clean["native_session_ledger_clear"],clean)
        self.assertTrue(all(count==0 for count in clean["native_session_pending"].values()))
