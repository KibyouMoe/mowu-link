from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
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
from .codex_approvals import CodexApprovals
from .codex_thread_preparation import SCHEMA as CODEX_PREPARATION_SCHEMA
from .managed_sessions import FAMILY_SCHEMA, derive_callback, read_enrollment, token_digest, write_enrollment
from .orders import Orders
from .release_check import config
from .state_bundle import audit_handoff, audit_trigger, capture, filename, restore, rollback, stage_native_handoff, validate_set, verify
from .storage import Database, Fault, StateWriterBusy, encode, guid, state_writer
from .test_feishu_inbound import PNG
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .trigger import Trigger
from .wire import Client, serve_loopback
from .worker import Worker
from . import display, dsh_workspace_audit, state_bundle, worker_commands
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
    def stack(self,paths,*,pin_session=False):
        source = Trigger(paths["trigger:mail"],"mail",self.clock)
        router = Orders(paths["router:main"],self.clock,1)
        ids = {"mgr":"hermes","w":"worker/oguri","mail":"trigger/"+source.instance_id}
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
            "local_cursor":0,"queued_without_native_receipt":1,"issues":[]})
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
