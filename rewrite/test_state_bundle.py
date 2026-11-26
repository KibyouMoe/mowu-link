from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
from threading import Thread
import unittest

from .adapter import HostAdapter
from .devloop import FixtureHost
from .feishu_inbound import Inbound
from .feishu_output import Views
from .hermes import HermesAdapter
from .orders import Orders
from .state_bundle import audit_handoff, capture, filename, restore, rollback, validate_set, verify
from .storage import StateWriterBusy, encode, guid, state_writer
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .trigger import Trigger
from .wire import Client, serve_loopback
from .worker import Worker
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
    def stack(self,paths):
        source = Trigger(paths["trigger:mail"],"mail",self.clock)
        router = Orders(paths["router:main"],self.clock,1)
        ids = {"mgr":"hermes","w":"worker/oguri","mail":"trigger/"+source.instance_id}
        with running(serve_loopback(router,ids)) as url:
            manager = HermesAdapter(paths["hermes:main"],Client(url,"mgr"),
                      HostAdapter(paths["hermes-host:main"],self.hermes_host,self.hermes_host.session),self.clock)
            agent = HostAdapter(paths["agent:oguri"],self.native,self.native.session)
            worker = Worker(paths["worker:oguri"],"oguri",Client(url,"w"),agent)
            server = WorkerService(worker).listen({"platform":"platform/oguri"},("127.0.0.1",0),None)
            with running(server) as worker_url:
                client = Client(worker_url,"platform")
                platform = Inbound(paths["feishu:oguri"],client,None,"app","trainer","chat",clock=self.clock)
                views = Views(paths["feishu:oguri"],client,self.platform,"chat","工位",clock=self.clock)
                yield manager,worker,source,Client(url,"mail"),platform,views

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

    def test_wrong_role_active_writer_or_incomplete_inventory_preserves_destination(self):
        with self.stack(self.sources):
            pass
        def backup(sources):
            return capture(sources,self.revision,self.snapshot)
        with self.assertRaisesRegex(ValueError,"five component roles"):
            backup({k:v for k,v in self.sources.items() if k!="worker:oguri"})
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
                         {"private_chat":1,"queued_chat":1,"adapter_deliveries":1})
        self.assertNotIn(private_text,encode(seen))
        self.assertNotIn(queue_text,encode(seen))
        self.assertEqual(audit_handoff(self.snapshot,checksum),seen)

        restored = restore(self.snapshot,checksum,self.dest)
        self.native.is_busy = False
        with self.stack(restored) as (_m,w,_source,_mail,_platform,_views):
            self.assertEqual(w.chat(private_mid,private_text),original)
            service = WorkerService(w,self.clock)
            service.step()
            service.step()
            self.assertEqual([payload["text"] for payload,_ in self.native.entries()],[private_text,queue_text])
        resolved = self.root/"resolved"
        later = capture(restored,self.revision,resolved)["manifest_sha256"]
        clean, = audit_handoff(resolved,later)["workers"]
        self.assertTrue(clean["native_session_ledger_clear"],clean)
        self.assertTrue(all(count==0 for count in clean["native_session_pending"].values()))
