from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from threading import Thread

from .cutover import backup, digest, prepare, rollback, verified
from .devloop import FixtureHost, Loop
from .test_mainline import Clock
from .orders import Orders
from .storage import Fault, guid
from .hermes import HermesAdapter
from .hermes_tools import ManagerTools
from .wire import Client, serve_loopback
from .worker_service import WorkerService


@unittest.skipUnless(os.name == "posix","this version of the old Router imports fcntl")
class Cutover(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.legacy = self.root/"legacy.db"
        self.attachments = self.root/"attachments"
        self.snapshot, self.trial, self.restore = [self.root/n for n in ("backup","new","restored")]
        self.source_id = guid()
        ids = self.old("""
store = RouterStore(sys.argv[1])
a = store.orders.create("Archived","Earlier","one",{},"fixture",None,None)
store.orders.record_dispatch(a,"one","first")
store.orders.add(a,"summary","s",report={"summary":"Prior result"})
store.orders.archive(a,"summary_received")
q = store.orders.create("Queued","Wait until idle","two",{},"fixture",None,None)
image = b"GIF89a" + bytes(range(64))
att = AttachmentStore(Path(sys.argv[2])).put(io.BytesIO(image),len(image),hashlib.sha256(image).hexdigest())
store._conn.close()
print(json.dumps([a,q,att["id"]]))
""",self.legacy,self.attachments)
        self.archived,self.queued,self.sha = json.loads(ids)

    def old(self,code,*args):
        source = Path(__file__).resolve().parents[1]/"server/router"
        program = "from server import RouterStore, AttachmentStore\nfrom pathlib import Path\nimport sys,json,hashlib,io\n" + code
        output = subprocess.run([sys.executable,"-c",program,*map(str,args)],check=True,capture_output=True,
                                text=True,cwd=self.root,env={"PATH":os.defpath,"HOME":str(self.root),
                                "PYTHONPATH":str(source),"PYTHONDONTWRITEBYTECODE":"1"})
        return output.stdout.strip()

    def test_queue_crosswalk_attachment_bytes_and_real_legacy_reopen_after_rollback(self):
        original = digest(self.legacy)
        manifest = backup(self.legacy,self.attachments,self.snapshot)
        self.assertEqual(verified(self.snapshot),manifest)
        baseline = prepare(self.snapshot,self.trial,self.source_id)
        new_id = baseline["queued_id_map"][self.queued]
        self.assertNotIn(self.archived,baseline["queued_id_map"])
        self.assertEqual(prepare(self.snapshot,self.root/"again",self.source_id)["queued_id_map"],
                         baseline["queued_id_map"])
        self.assertEqual(rollback(self.snapshot,self.trial,self.restore)["restored_db_sha256"],original)
        with closing(sqlite3.connect(self.trial/"router.db")) as db:
            self.assertEqual(db.execute("SELECT task_id,state,assignment_id FROM tasks").fetchall(),[(new_id,"ready",None)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],0)
        history = self.old("""
store = RouterStore(sys.argv[1])
print(json.dumps({"next":store.orders.next_dispatch("two"),"report":store.orders.get(sys.argv[2])["archive"]["reason"]}))
store._conn.close()
""",self.restore/"legacy.db",self.archived)
        self.assertEqual(json.loads(history),{"next":self.queued,"report":"summary_received"})
        self.assertEqual((self.restore/"attachments"/self.sha).read_bytes(),(self.attachments/self.sha).read_bytes())
        self.assertEqual(digest(self.legacy),original)

    def test_old_http_default_order_migrates_with_goal_and_idempotency(self):
        queued_key,archived_key = guid(),guid()
        old_ids = json.loads(self.old("""
from server import RouterServer
from selftest_router import http
from threading import Thread
from unittest.mock import Mock
store = RouterStore(sys.argv[1])
store.upsert_worker("oguri")
with RouterServer(("127.0.0.1",0),store,"offline-test",Mock(),Mock()) as srv:
    thread = Thread(target=srv.serve_forever,kwargs={"poll_interval":.02})
    thread.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/tasks"
        ids = []
        for key in sys.argv[2:]:
            payload = {"message_id":key,"worker":"oguri","TaskName":"From HTTP","goal":"Keep this order",
                       "context":"关联材料"}
            status,reply = http("POST",url,payload,"offline-test")
            assert status==202 and reply["queued"] and reply["execution_mode"]=="goal", (status,reply)
            again,duplicate = http("POST",url,payload,"offline-test")
            assert again==200 and duplicate["duplicate"] and duplicate["task_id"]==reply["task_id"]
            ids.append(reply["task_id"])
        store.orders.record_dispatch(ids[1],"oguri","fixture")
        store.orders.archive(ids[1],"worker_cancel")
    finally:
        srv.shutdown()
        thread.join()
store.close()
print(json.dumps(ids))
""",self.legacy,queued_key,archived_key))
        manifest = backup(self.legacy,self.attachments,self.snapshot)
        # An old snapshot counted ordinary HTTP metadata as a blocker. Its original count stays verifiable.
        manifest["format"] = 1
        manifest["blockers"]["ambiguous_queue_rows"] = 1
        (self.snapshot/"manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(verified(self.snapshot),manifest)
        baseline = prepare(self.snapshot,self.trial,self.source_id)
        mapped = baseline["queued_id_map"][old_ids[0]]
        clock = Clock()
        loop = Loop(self.trial,clock,FixtureHost)
        self.addCleanup(loop.server.server_close)
        loop.thread.start()
        self.addCleanup(loop.thread.join)
        self.addCleanup(loop.server.shutdown)
        manager = loop.hermes
        expected_body = "Keep this order\n\n[router context]\n关联材料"
        self.assertEqual(manager.create(queued_key,"From HTTP",expected_body,"oguri")["task_id"],mapped)
        with self.assertRaisesRegex(Fault,"idempotency_conflict"):
            manager.create(queued_key,"From HTTP","Changed","oguri")
        with self.assertRaisesRegex(Fault,"legacy_request_recorded") as archived:
            manager.create(archived_key,"From HTTP","Keep this order","oguri")
        self.assertEqual(archived.exception.details["legacy_task_id"],old_ids[1])
        loop.worker.connect()
        loop.worker.poll()
        clock.advance(10)
        loop.worker.poll()
        loop.worker.poll()
        (payload,_receipt), = loop.native.entries()
        self.assertEqual((payload["task_id"],loop.worker.hold()["held_task_id"]),(mapped,mapped))
        self.assertIn("原生目标",payload["instructions"])
        self.assertTrue(payload["instructions"].endswith("\n\n"+expected_body))
        self.assertEqual(len(manager.tasks()["items"]),2)

    def test_inflight_or_unhandled_metadata_keeps_original_and_blocks_new_writer(self):
        self.old("""
store = RouterStore(sys.argv[1])
store.orders.record_dispatch(sys.argv[2],"two","in-flight")
store.orders.create("With file","Handle context","three",{"context":"extra"},"fixture",None,None)
store._conn.close()
""",self.legacy,self.queued)
        blockers = backup(self.legacy,self.attachments,self.snapshot)["blockers"]
        self.assertEqual(blockers["held_tasks"],1)
        self.assertEqual(blockers["ambiguous_queue_rows"],1)
        with self.assertRaisesRegex(ValueError,"reconciliation"):
            prepare(self.snapshot,self.trial,self.source_id)
        self.assertFalse(self.trial.exists())

    def test_changed_new_system_requires_reconciliation_before_restoring_old(self):
        backup(self.legacy,self.attachments,self.snapshot)
        prepare(self.snapshot,self.trial,self.source_id)
        router = Orders(self.trial/"router.db")
        with router.db.transaction() as db:
            router.create(db,{"title":"New work","instructions":"Preserve this"})
        with self.assertRaisesRegex(ValueError,"reconcile"):
            rollback(self.snapshot,self.trial,self.restore)
        self.assertFalse(self.restore.exists())

    def test_pending_peer_or_missing_middle_chunk_prevents_publishing_a_partial_migration(self):
        mid = guid()
        self.old("""
store = RouterStore(sys.argv[1])
store.add_peer_message(sys.argv[2],"tama","oguri","a"*240000,None,[],"fixture")
store.close()
""",self.legacy,mid)
        backup(self.legacy,self.attachments,self.snapshot)
        with self.assertRaisesRegex(ValueError,"unconfirmed_peer_messages"):
            prepare(self.snapshot,self.trial,self.source_id)
        self.old("""
store = RouterStore(sys.argv[1])
store.ack_peer_message(sys.argv[2])
assert store._conn.execute("DELETE FROM peer_text_chunks WHERE message_id=? AND part=1",(sys.argv[2],)).rowcount==1
store.close()
""",self.legacy,mid)
        second = self.root/"snapshot-with-gap"
        backup(self.legacy,self.attachments,second)
        with self.assertRaisesRegex(ValueError,"incomplete legacy peer text"):
            prepare(second,self.trial,self.source_id)
        self.assertFalse(self.trial.exists())

    def test_archived_peer_threads_files_and_permissions_survive_without_replay(self):
        first,reply,root_msg = guid(),guid(),guid()
        sample = "原样保留🟣"*13000
        result = json.loads(self.old("""
store = RouterStore(sys.argv[1])
src = AttachmentStore(Path(sys.argv[2]))
ref = src.describe(sys.argv[3],"附件.gif")
first,reply,root = sys.argv[4:7]
store.add_peer_message(first,"tama","oguri","原样保留🟣"*13000,None,[ref],"fixture",sys.argv[7])
store.ack_peer_message(first)
store.add_peer_message(reply,"oguri","tama","收到",first,[],"fixture")
store._conn.execute("UPDATE peer_messages SET abandoned_at=created_at WHERE message_id=?",(reply,))
store.add_peer_message(root,"Hermes","tama","one manager message",None,[],"fixture")
store.ack_peer_message(root)
print(json.dumps(store.get_peer_message(first)["acked_at"]))
store.close()
""",self.legacy,self.attachments,self.sha,first,reply,root_msg,self.archived))
        backup(self.legacy,self.attachments,self.snapshot)
        prepare(self.snapshot,self.trial,self.source_id)
        loop = Loop(self.trial,Clock(),FixtureHost,extra_workers=("tama","stranger"))
        loop.thread.start()
        self.addCleanup(loop.server.server_close)
        self.addCleanup(loop.thread.join)
        self.addCleanup(loop.server.shutdown)
        loop.worker.connect()
        self.assertFalse([f for f in loop.wire.frames(0) if f["kind"]=="peer.message"])
        route = "history/"+self.source_id+"/peers"
        first_page = loop.wire.request("GET",route+"?limit=1")
        self.assertEqual(first_page["items"][0]["message_id"],first)
        self.assertNotIn("text",first_page["items"][0])
        second, = loop.wire.request("GET",route+"?cursor="+str(first_page["next_cursor"]))["items"]
        self.assertEqual((second["message_id"],second["reply_to"],second["acked_at"]),(reply,first,None))
        self.assertIsNotNone(second["abandoned_at"])
        self.assertEqual([m["message_id"] for m in loop.manager_wire.request("GET",route)["items"]],[root_msg])
        for other in (loop.manager_wire,loop.extra_clients["stranger"]):
            with self.assertRaisesRegex(Fault,"legacy_peer_not_found"):
                other.request("GET",route+"/"+first)
        with self.assertRaisesRegex(Fault,"legacy_peer_recorded"):
            loop.worker.send_peer(reply,"tama","收到",first)
        key,platform = guid(),guid()
        listener = self.enterContext(WorkerService(loop.worker).listen({key:"adapter/oguri",platform:"platform/oguri"},
                                                                       ("127.0.0.1",0),None))
        thread = Thread(target=listener.serve_forever,kwargs={"poll_interval":.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(listener.shutdown)
        origin = "http://127.0.0.1:"+str(listener.server_port)
        native = Client(origin,key)
        detail = "worker/"+route+"/"+first
        item = native.request("GET",detail)
        self.assertEqual(item["message"]["acked_at"],result)
        self.assertEqual(item["message"]["task_id"],self.archived)
        self.assertNotIn("host_receipt_id",item["message"])
        self.assertGreater(item["message"]["text_parts"],1)
        pieces = [item["text"]]
        while item["next_part"] is not None:
            item = native.request("GET",detail+"?part="+str(item["next_part"]))
            pieces.append(item["text"])
        self.assertEqual("".join(pieces),sample)
        ref, = item["attachments"]
        for other in (loop.manager_wire,loop.extra_clients["stranger"]):
            with self.assertRaisesRegex(Fault,"attachment_not_found"):
                other.request("GET","attachments/"+ref["attachment_id"])
        with self.assertRaises(Fault):
            Client(origin,platform).request("GET",detail)
        fetched = native.request("POST",detail+"/file",{"position":0})
        self.assertEqual(native.request("POST",detail+"/file",{"position":0}),fetched)
        self.assertNotEqual(fetched["attachment_id"],ref["attachment_id"])
        self.assertEqual(native.request("GET","worker/media/files/"+fetched["attachment_id"])["bytes"],
                         (self.attachments/self.sha).read_bytes())
        self.assertEqual(loop.native.entries(),[])
        self.assertFalse(loop.worker.hold()["held_task_id"])
        clock = loop.orders.clock
        tid = loop.hermes.create(guid(),"New work","独立正式单","oguri")["task_id"]
        loop.worker.poll()
        clock.advance(10)
        loop.worker.poll()
        loop.worker.poll()
        held = loop.worker.hold()
        self.assertEqual(held["held_task_id"],tid)
        with self.assertRaisesRegex(Fault,"historical_attachment_read_only"):
            loop.wire.request("POST","tasks/"+tid+"/report",{
                "session_id":loop.worker.session,"assignment_id":held["held_assignment_id"],
                "outcome":"completed","summary":"private file reference","artifact_ids":[ref["attachment_id"]]},guid())
        self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"working")
        with self.assertRaisesRegex(Fault,"attachment_not_found"):
            loop.manager_wire.request("GET","attachments/"+ref["attachment_id"])

    def test_original_id_and_source_read_back_via_manager_without_reviving_archived_order(self):
        backup(self.legacy,self.attachments,self.snapshot)
        baseline = prepare(self.snapshot,self.trial,self.source_id)
        srv = serve_loopback(Orders(self.trial/"router.db"),{"manager":"hermes","worker":"worker/test"})
        self.addCleanup(srv.server_close)
        thread = Thread(target=srv.serve_forever,kwargs={"poll_interval":.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(srv.shutdown)
        url = "http://127.0.0.1:"+str(srv.server_port)
        manager = HermesAdapter(self.root/"reader.db",Client(url,"manager"),None)
        tools = ManagerTools(manager,"review")
        def query(**filters):
            return json.loads(tools.around_call("router_tasks",lambda a:tools.invoke("router_tasks",a),
                              filters,session_id="review"))
        source = {"legacy_source_id":self.source_id}
        first = query(**source,limit=1)
        second = query(**source,limit=1,cursor=int(first["next_cursor"]))
        self.assertEqual([first["items"][0]["legacy_task_id"],second["items"][0]["legacy_task_id"]],
                         [self.archived,self.queued])
        self.assertIsNone(second["next_cursor"])
        original = query(**source,legacy_task_id=self.archived)
        archived = original["historical_task"]
        self.assertEqual(original["legacy_db_sha256"],baseline["snapshot_sha256"])
        self.assertEqual(archived["archive"]["reason"],"summary_received")
        self.assertIsInstance(archived["archive"]["at"],float)
        self.assertEqual(archived["deliverable"],{"summary":"Prior result"})
        self.assertIsNone(archived["mapped_task_id"])
        self.assertEqual(second["items"][0]["mapped_task_id"],baseline["queued_id_map"][self.queued])
        self.assertEqual(query(legacy_source_id=guid(),legacy_task_id=self.archived)["status"],404)
        self.assertEqual(query(legacy_task_id=self.archived)["status"],422)
        self.assertEqual(query(**source,task_id=guid())["status"],422)
        route = f"history/{self.source_id}/tasks"
        with self.assertRaises(Fault) as denied:
            Client(url,"worker").request("GET",route)
        self.assertEqual(denied.exception.status,403)
        with self.assertRaises(Fault) as denied:
            manager.router.request("POST",route,{"action":"resume"},guid())
        self.assertEqual(denied.exception.status,405)
        self.assertEqual(len(manager.tasks()["items"]),1)
        self.assertEqual(manager.router.frames(0),[])
        self.assertEqual(digest(self.trial/"router.db"),baseline["router_db_sha256"])

    def test_corrupt_files_and_existing_destinations_are_rejected(self):
        backup(self.legacy,self.attachments,self.snapshot)
        with self.assertRaises(FileExistsError):
            backup(self.legacy,self.attachments,self.snapshot)
        (self.snapshot/"attachments"/self.sha).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError,"digest"):
            prepare(self.snapshot,self.trial,self.source_id)
        self.assertFalse(self.trial.exists())
