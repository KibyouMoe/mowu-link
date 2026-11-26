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
import unittest
from threading import Thread

from .adapter import HostAdapter
from .cutover import backup, digest, export_chat, prepare, reconcile, rollback, stage_queue_rollback, verified
from .devloop import FixtureHost, Loop, agent_report
from .legacy_test_fixture import source_for
from .test_mainline import Clock
from .orders import Orders
from .reviewed_rollback import stage_reviewed_rollback
from .state_bundle import audit_handoff, capture, restore
from .storage import Fault, guid
from .hermes import HermesAdapter
from .hermes_tools import ManagerTools
from .trigger import Trigger
from .wire import Client, serve_loopback
from .worker import Worker
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
        source = source_for("router")
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

    def test_export_legacy_chat_keeps_original_fields_and_private_scope(self):
        conversation = json.loads(self.old("""
store = RouterStore(sys.argv[1])
store.add_chat_message("oguri","in","原问题", "old-session", 123, "om_same", meta="raw retained in snapshot")
store.add_chat_message("tamamo","out","other worker body", "other-session", 124, "om_other")
store.add_chat_message("oguri","out","原回复", "old-session", 125, "om_same")
store.add_chat_message("oguri","out","new session", "other-thread", 126, "om_next")
print(json.dumps(store.list_chat_messages("oguri",since_id=0)))
store.close()
""",self.legacy))
        backup(self.legacy,self.attachments,self.snapshot)
        before = digest(self.snapshot/"legacy.db")
        def data(folder):
            path = folder/"messages.jsonl"
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            return [json.loads(line) for line in path.read_text().splitlines()]
        first = self.root/"first-chat"
        initial = export_chat(self.snapshot,first,"oguri",limit=2)
        self.assertEqual((initial["count"],initial["next_cursor"]),(2,conversation[1]["id"]))
        self.assertEqual(initial["messages_sha256"],digest(first/"messages.jsonl"))
        self.assertEqual(data(first),conversation[:2])
        second = self.root/"second-chat"
        end = export_chat(self.snapshot,second,"oguri",after=initial["next_cursor"])
        self.assertIsNone(end["next_cursor"])
        self.assertEqual(data(second),conversation[2:])
        answers = self.root/"answers"
        export_chat(self.snapshot,answers,"oguri",direction="out")
        self.assertEqual(data(answers),conversation[1:])
        self.assertEqual(digest(self.snapshot/"legacy.db"),before)
        self.assertEqual(digest(self.legacy),before)
        (self.snapshot/"legacy.db").write_bytes(b"damaged snapshot")
        with self.assertRaisesRegex(ValueError,"snapshot verification failed"):
            export_chat(self.snapshot,self.root/"rejected","oguri")
        self.assertFalse((self.root/"rejected").exists())

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

    def test_migrated_formal_result_and_attachment_stop_exact_rollback_with_a_crosswalk(self):
        legacy_id = json.loads(self.old("""
store = RouterStore(sys.argv[1])
store.upsert_worker("oguri")
tid = store.orders.create("Migrated work","Reconciliation fixture private input","oguri",{},"fixture",None,None)
store.close()
print(json.dumps(tid))
""",self.legacy))
        backup(self.legacy,self.attachments,self.snapshot)
        base = prepare(self.snapshot,self.trial,self.source_id)
        tid = base["queued_id_map"][legacy_id]
        clock = Clock()
        loop = Loop(self.trial,clock,FixtureHost)
        blob = b"Private result binary"
        with loop.server:
            loop.thread.start()
            try:
                loop.worker.connect()
                loop.worker.poll()
                clock.advance(10)
                loop.worker.poll()
                loop.worker.poll()
                aid = loop.worker.hold()["held_assignment_id"]
                native = loop.native.entries()[0][1]["native_id"]
                declared = loop.wire.request("POST","uploads",{"name":"private-name.bin",
                            "size_bytes":len(blob),"sha256":hashlib.sha256(blob).hexdigest()},guid())
                artifact = loop.wire.request("PATCH","uploads/"+declared["upload_id"],blob,
                                             headers={"Upload-Offset":"0"})["attachment_id"]
                args = {"task_id":tid,"assignment_id":aid,"summary":"Reconciliation fixture private result",
                        "artifact_ids":[artifact]}
                report = agent_report(loop.worker,native,args)
                self.assertFalse(report["isError"])
                self.assertEqual(agent_report(loop.worker,native,args),report)
                self.assertIsNone(loop.worker.hold()["held_assignment_id"])
                notices = loop.manager_wire.frames(0)
                self.assertEqual(len(notices),1)
            finally:
                loop.server.shutdown()
                loop.thread.join()
        old_before = digest(self.snapshot/"legacy.db")
        trial_before = digest(self.trial/"router.db")
        facts = reconcile(self.snapshot,self.trial)
        change = next(row for row in facts["work_orders"] if row["task_id"]==tid)
        self.assertEqual(change["legacy_task_id"],legacy_id)
        self.assertEqual(change["before"],{"assignee_id":"oguri","assignment_id":None,"revision":1,"state":"ready"})
        self.assertEqual(change["trial"]["state"],"completed_reported")
        self.assertIsNone(change["trial"]["assignment_id"])
        self.assertEqual(change["completed_reports"],
                         [{"assignment_id":aid,"worker_id":"oguri","artifact_ids":[artifact]}])
        self.assertEqual(change["candidate_materials"],[])
        self.assertFalse(facts["only_new_queued_tasks"] or facts["exact_rollback_allowed"])
        seen = json.dumps(facts)
        for secret_text in (args["summary"],"Reconciliation fixture private input","private-name.bin",blob.decode()):
            self.assertNotIn(secret_text,seen)
        with self.assertRaisesRegex(ValueError,"reconcile"):
            rollback(self.snapshot,self.trial,self.restore)
        review = self.root/"pending-review.json"
        review.write_text(json.dumps({"trial_sha256":trial_before,"closed_assignments":{tid:aid}}))
        with self.assertRaisesRegex(ValueError,"closed after one W04"):
            stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
        self.assertFalse(self.restore.exists())
        self.assertEqual((digest(self.snapshot/"legacy.db"),digest(self.trial/"router.db")),(old_before,trial_before))
        self.evidence = {"work_order":change,"router_notification_count":len(notices),
                         "trial_sha256":facts["trial_sha256"],"legacy_snapshot_sha256":old_before,
                         "exact_rollback_allowed":facts["exact_rollback_allowed"],"both_databases_preserved":True,
                         "boundary":"Actual legacy and rewrite SQLite; isolated Router over HTTP and scripted Agent"}

    def test_reviewed_migrated_report_rolls_back_as_archive_without_redispatch_or_public_attachment(self):
        creation = {"message_id":guid(),"worker":"oguri","TaskName":"Complete while trial is live",
                    "goal":"Isolated migrated task"}
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
        status, reply = http("POST",f"http://127.0.0.1:{srv.server_port}/tasks",
                             json.loads(sys.argv[2]),"offline-test")
        assert status==202 and reply["queued"], (status,reply)
        first = reply["task_id"]
    finally:
        srv.shutdown()
        thread.join()
second = store.orders.create("Keep this queued","Do after recovery","oguri",{},"fixture",None,None)
store.close()
print(json.dumps([first,second]))
""",self.legacy,json.dumps(creation)))
        backup(self.legacy,self.attachments,self.snapshot)
        base = prepare(self.snapshot,self.trial,self.source_id)
        tid,waiting = (base["queued_id_map"][old] for old in old_ids)
        clock = Clock()
        loop = Loop(self.trial,clock,FixtureHost)
        blob = b"private archive bytes\0" * 20
        sha = hashlib.sha256(blob).hexdigest()
        summary = "试用中已验收。\n正文和附件只能在私有材料包里查看。"
        with loop.server:
            loop.thread.start()
            try:
                loop.worker.connect()
                loop.worker.poll()
                clock.advance(10)
                loop.worker.poll()
                loop.worker.poll()
                self.assertEqual(loop.worker.hold()["held_task_id"],tid)
                aid = loop.worker.hold()["held_assignment_id"]
                native = loop.native.entries()[0][1]["native_id"]
                upload = loop.wire.request("POST","uploads",{"name":"reviewed.bin","size_bytes":len(blob),
                                           "sha256":sha},guid())["upload_id"]
                loop.wire.request("PATCH","uploads/"+upload,blob[:31],headers={"Upload-Offset":"0"})
                artifact = loop.wire.request("PATCH","uploads/"+upload,blob[31:],
                                             headers={"Upload-Offset":"31"})["attachment_id"]
                call = {"task_id":tid,"assignment_id":aid,"summary":summary,"artifact_ids":[artifact]}
                report = agent_report(loop.worker,native,call)
                self.assertFalse(report["isError"])
                self.assertEqual(agent_report(loop.worker,native,call),report)
                received = loop.hermes.inspect(tid)["task"]
                self.assertEqual(received["state"],"completed_reported")
                approval_key = guid()
                self.assertEqual(loop.hermes.approve(approval_key,tid,received["revision"])["task"]["state"],"closed")
                notice, = loop.manager_wire.frames(0)
                self.assertEqual(loop.hermes.inspect(waiting)["task"]["state"],"ready")
            finally:
                loop.server.shutdown()
                loop.thread.join()
        review = self.root/"review-approved.json"
        approval = {"trial_sha256":digest(self.trial/"router.db"),"closed_assignments":{tid:aid}}
        review.write_text(json.dumps(approval))
        with self.assertRaisesRegex(ValueError,"no consumer acknowledgement"):
            stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
        self.assertFalse(self.restore.exists())

        token = guid()
        with serve_loopback(loop.orders,{token:"hermes"}) as server:
            thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
            thread.start()
            try:
                loop.hermes.router = Client("http://127.0.0.1:"+str(server.server_port),token)
                loop.hermes.poll()
                self.assertEqual(len(loop.hermes.router.frames(notice["cursor"])),0)
            finally:
                server.shutdown()
                thread.join()
        before = digest(self.legacy)
        trial_digest = digest(self.trial/"router.db")
        with self.assertRaisesRegex(ValueError,"review must match"):
            stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
        review.write_text(json.dumps({**approval,"trial_sha256":trial_digest}))
        result = stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
        self.assertEqual((result["reviewed_orders"],result["private_artifacts"]),(1,1))
        self.assertEqual(digest(self.restore/"legacy.db"),result["restored_legacy_sha256"])
        private = self.restore/"private-review"
        record = json.loads((private/"manifest.json").read_text())
        saved = record["orders"][tid]
        self.assertEqual((saved["legacy_task_id"],saved["assignment_id"],saved["summary"],saved["review_key"]),
                         (old_ids[0],aid,summary,approval_key))
        self.assertEqual(saved["notice"]["delivery_id"],notice["delivery_id"])
        self.assertEqual(record["files"][artifact]["sha256"],sha)
        self.assertEqual((private/"files"/artifact).read_bytes(),blob)
        self.assertEqual((private/"files"/artifact).stat().st_mode&0o777,0o600)
        self.assertEqual((private/"manifest.json").stat().st_mode&0o777,0o600)
        self.assertEqual(private.stat().st_mode&0o777,0o700)
        self.assertEqual(digest(private/"trial-router.db"),trial_digest)
        self.assertEqual(digest(self.legacy),before)
        self.assertEqual(digest(self.trial/"router.db"),trial_digest)

        observed = json.loads(self.old("""
from server import RouterServer
from selftest_router import http
from threading import Thread
from urllib.request import Request, urlopen
from unittest.mock import Mock
store = RouterStore(sys.argv[1])
with RouterServer(("127.0.0.1",0),store,"offline-operator-token",Mock(),Mock()) as srv:
    thread = Thread(target=srv.serve_forever,kwargs={"poll_interval":.02})
    thread.start()
    try:
        url = "http://127.0.0.1:"+str(srv.server_port)
        status, archive = http("GET",url+"/tasks/"+sys.argv[2],token="offline-operator-token")
        gone, _ = http("GET",url+"/attachments/"+sys.argv[3], token="offline-operator-token")
        repeated_status,repeated = http("POST",url+"/tasks",json.loads(sys.argv[5]),"offline-operator-token")
        assert repeated_status==200 and repeated["duplicate"] and repeated["task_id"]==sys.argv[2], repeated
        existing = Request(url+"/attachments/"+sys.argv[4],headers={"X-Router-Token":"offline-operator-token"})
        with urlopen(existing,timeout=5) as response:
            old_file_digest = hashlib.sha256(response.read()).hexdigest()
        print(json.dumps({"status":status,"archive":archive["task"]["archive"],
                          "deliverable":archive["task"]["deliverable"],"attachment_status":gone,
                          "previous_attachment_sha256":old_file_digest,
                          "legacy_creation_replayed":repeated["task_id"],
                          "head":store.orders.next_dispatch("oguri"),"active":store.orders.held("oguri")}))
    finally:
        srv.shutdown()
        thread.join()
store.close()
""",self.restore/"legacy.db",old_ids[0],sha,self.sha,json.dumps(creation)))
        self.assertEqual(observed["status"],200)
        self.assertEqual(observed["archive"]["reason"],"reviewed_on_rewrite")
        self.assertEqual(observed["archive"]["recovery_reference"],tid)
        self.assertEqual(observed["legacy_creation_replayed"],old_ids[0])
        self.assertIsNone(observed["deliverable"])
        self.assertEqual((observed["head"],observed["active"],observed["attachment_status"]),(old_ids[1],None,404))
        self.assertEqual(observed["previous_attachment_sha256"],self.sha)
        corrupt = self.root/"corrupt-trial"
        corrupt.mkdir()
        shutil.copyfile(self.trial/"baseline.json",corrupt/"baseline.json")
        shutil.copyfile(self.trial/"router.db",corrupt/"router.db")
        with closing(sqlite3.connect(corrupt/"router.db")) as db,db:
            db.execute("UPDATE chunks SET bytes=? WHERE upload_id=? AND start=31",(b"x"*len(blob[31:]),upload))
        review.write_text(json.dumps({**approval,"trial_sha256":digest(corrupt/"router.db")}))
        failed = self.root/"damaged-back"
        with self.assertRaisesRegex(ValueError,"attachment digest mismatch"):
            stage_reviewed_rollback(self.snapshot,corrupt,review,failed)
        self.assertFalse(failed.exists())
        shutil.copyfile(self.trial/"router.db",corrupt/"router.db")
        info_instance,info_key = guid(),guid()
        with serve_loopback(Orders(corrupt/"router.db",clock),{info_key:"trigger/"+info_instance}) as server:
            thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
            thread.start()
            try:
                trigger = Client("http://127.0.0.1:"+str(server.server_port),info_key)
                trigger.request("POST","messages",{"message_id":guid(),"instance_id":info_instance,
                    "trigger_name":"isolated-info","level":"Info","text":"New trial archival message",
                    "generated_at":"2026-01-01T00:00:00Z","attachment_ids":[]},guid())
            finally:
                server.shutdown()
                thread.join()
        review.write_text(json.dumps({**approval,"trial_sha256":digest(corrupt/"router.db")}))
        with self.assertRaisesRegex(ValueError,"outside the reviewed W04 results"):
            stage_reviewed_rollback(self.snapshot,corrupt,review,failed)
        self.assertFalse(failed.exists())
        self.evidence = {"source_legacy_task":old_ids[0],"trial_task":tid,"assignment":aid,"review_key":approval_key,
                         "notification_delivery":notice["delivery_id"],"artifact_id":artifact,"artifact_sha256":sha,
                         "trial_sha256":trial_digest,"legacy_original_sha256":before,"legacy_readback":observed,
                         "unacknowledged_notification_rejected":True,"corrupt_artifact_rejected":True,
                         "unrelated_trial_info_prevented_rollback":True,
                         "boundary":"Actual isolated rewrite and legacy Router HTTP and SQLite. Worker model is scripted."}
        self.mixed_evidence = self.exercise_mixed_reviewed_queue(clock,old_ids,tid,aid,artifact,sha)

    def exercise_mixed_reviewed_queue(self,clock,old_ids,tid,aid,artifact,sha):
        mixed = self.root/"trial-with-new-queue"
        mixed.mkdir()
        for name in ("baseline.json","router.db"):
            shutil.copyfile(self.trial/name,mixed/name)
        token,first_key = guid(),guid()
        clock.advance(31)
        with serve_loopback(Orders(mixed/"router.db",clock),{token:"hermes"}) as server:
            thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
            thread.start()
            try:
                manager = HermesAdapter(self.root/"mixed-hermes.db",
                                        Client("http://127.0.0.1:"+str(server.server_port),token),None)
                first = manager.create(first_key,"新工单一","保留换行\n第一份要求","oguri")
                tools = ManagerTools(manager,"mixed-rollback-hermes")
                second = json.loads(tools.around_call("router_create_task",
                    lambda args:tools.invoke("router_create_task",args),
                    {"title":"新工单二","instructions":"保留换行\n第二份要求","assignee_id":"oguri"},
                    session_id="mixed-rollback-hermes",turn_id="independent-turn",tool_call_id="create-second"))
                self.assertTrue(all(row["state"]=="ready" and row["assignment_id"] is None
                                    for row in (first,second)))
            finally:
                server.shutdown()
                thread.join()

        facts = reconcile(self.snapshot,mixed)
        new_ids = [first["task_id"],second["task_id"]]
        self.assertEqual([row["task_id"] for row in facts["queue_candidates"]],new_ids)
        self.assertFalse(facts["only_new_queued_tasks"])
        review = self.root/"mixed-review.json"
        decision = {"trial_sha256":facts["trial_sha256"],"closed_assignments":{tid:aid}}
        review.write_text(json.dumps({**decision,"execution_modes":{new_ids[0]:"goal"}}))
        target = self.root/"mixed-restore"
        with self.assertRaisesRegex(ValueError,"all closed assignments and any new queue modes"):
            stage_reviewed_rollback(self.snapshot,mixed,review,target)
        with self.assertRaisesRegex(ValueError,"only new unoffered orders"):
            stage_queue_rollback(self.snapshot,mixed,review,target)
        self.assertFalse(target.exists())
        review.write_text(json.dumps({**decision,"execution_modes":dict.fromkeys(new_ids,"goal")}))
        staged = stage_reviewed_rollback(self.snapshot,mixed,review,target)
        self.assertEqual((staged["reviewed_orders"],staged["private_artifacts"],staged["queued_requests"]),(1,1,2))
        queue = target/"queue-replay.json"
        readback = json.loads(queue.read_text())
        self.assertEqual(queue.stat().st_mode&0o777,0o600)
        self.assertEqual(readback["restored_legacy_sha256"],digest(target/"legacy.db"))
        self.assertEqual(readback["requests"][0]["request"]["message_id"],first_key)
        self.assertRegex(readback["requests"][1]["trial_request_id"],r"^hermes-[0-9a-f]{64}$")
        private = target/"private-review"
        self.assertEqual(staged["queue_replay_sha256"],digest(queue))
        self.assertEqual(json.loads((private/"manifest.json").read_text())["queue_replay_sha256"],digest(queue))
        self.assertEqual(digest(private/"files"/artifact),sha)
        self.assertEqual(digest(private/"trial-router.db"),facts["trial_sha256"])

        replay = """
from server import RouterServer
from selftest_router import http
from threading import Thread
from unittest.mock import Mock
store = RouterStore(sys.argv[1])
plan = json.loads(Path(sys.argv[2]).read_text())
auth = "offline-test"
replies = []
with RouterServer(("127.0.0.1",0),store,auth,Mock(),Mock()) as srv:
    thread = Thread(target=srv.serve_forever,kwargs={"poll_interval":.02})
    thread.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        status, archive = http("GET",url+"/tasks/"+sys.argv[3],token=auth)
        assert status==200 and archive["task"]["archive"]["reason"]=="reviewed_on_rewrite"
        for item in plan["requests"][:int(sys.argv[4])]:
            body = item["request"]
            status, reply = http("POST",url+"/tasks",body,auth)
            assert status in (200,202) and not reply.get("dispatched"), (status,reply)
            assert bool(reply.get("duplicate"))==(status==200), reply
            clash, _ = http("POST",url+"/tasks",dict(body,goal="changed during replay"),auth)
            assert clash==409
            replies.append({"status":status,"legacy_task_id":reply["task_id"],"trial_task_id":item["trial_task_id"]})
    finally:
        srv.shutdown()
        thread.join()
rows = store._conn.execute('''SELECT i.task_id,i.name,i.description,i.request_id,i.metadata FROM task_info i
                              JOIN dispatch_queue q USING(task_id) WHERE i.owner='oguri' ORDER BY q.seq''').fetchall()
print(json.dumps({"archive":archive["task"]["archive"],"replies":replies,"rows":[dict(r) for r in rows],
                  "active":store.orders.held("oguri")}))
store.close()
"""
        first_boot = json.loads(self.old(replay,target/"legacy.db",queue,old_ids[0],1))
        resumed = json.loads(self.old(replay,target/"legacy.db",queue,old_ids[0],2))
        self.assertEqual([x["status"] for x in first_boot["replies"]],[202])
        self.assertEqual([x["status"] for x in resumed["replies"]],[200,202])
        self.assertEqual(first_boot["replies"][0]["legacy_task_id"],resumed["replies"][0]["legacy_task_id"])
        self.assertEqual(first_boot["archive"],resumed["archive"])
        self.assertIsNone(resumed["active"])
        self.assertEqual([row["task_id"] for row in resumed["rows"]],
                         [old_ids[1]]+[row["legacy_task_id"] for row in resumed["replies"]])
        self.assertEqual([row["description"] for row in resumed["rows"][1:]],
                         ["保留换行\n第一份要求","保留换行\n第二份要求"])
        self.assertTrue(all(json.loads(row["metadata"])["execution_mode"]=="goal" for row in resumed["rows"][1:]))
        self.assertEqual(digest(mixed/"router.db"),facts["trial_sha256"])
        return {"source_legacy_task":old_ids[0],"next_legacy_task":old_ids[1],"reviewed_trial_task":tid,
                "assignment":aid,"trial_sha256":facts["trial_sha256"],"private_artifact_sha256":sha,
                "queue_plan_sha256":staged["queue_replay_sha256"],"incomplete_review_rejected":True,
                "first_boot":first_boot["replies"],"retry_after_legacy_restart":resumed["replies"],
                "legacy_queue_order":[row["task_id"] for row in resumed["rows"]],"legacy_active_task":resumed["active"],
                "changed_request_status":409,"reviewed_archive_reason":resumed["archive"]["reason"],
                "boundary":"Actual isolated rewrite and legacy Router HTTP and SQLite. Worker model is scripted."}

    def test_inflight_rollback_drains_same_assignment_before_old_router_can_resume(self):
        old_ids = json.loads(self.old("""
store = RouterStore(sys.argv[1])
store.upsert_worker("oguri")
first = store.orders.create("Finish in the trial","Keep the original holder","oguri",{},"fixture",None,None)
second = store.orders.create("Leave queued","The old Router will pick this up","oguri",{},"fixture",None,None)
store.close()
print(json.dumps([first,second]))
""",self.legacy))
        backup(self.legacy,self.attachments,self.snapshot)
        baseline = prepare(self.snapshot,self.trial,self.source_id)
        tid,waiting = (baseline["queued_id_map"][old] for old in old_ids)
        clock = Clock()
        native = FixtureHost(self.root/"native.db","drain-native-session")
        native_manager = FixtureHost(self.root/"native-admin.db","drain-manager-session")
        paths = {"router:main":self.trial/"router.db","worker:oguri":self.root/"worker.db",
                 "agent:oguri":self.root/"agent.db","hermes:main":self.root/"manager.db",
                 "hermes-host:main":self.root/"manager-host.db","trigger:mail":self.root/"trigger.db"}
        Trigger(paths["trigger:mail"],"mail",clock)

        @contextmanager
        def boot(files,*,drain):
            tokens = {"manager":"hermes","fixture-worker":"worker/oguri"}
            with serve_loopback(Orders(files["router:main"],clock,poll_seconds=1,drain_dispatch=drain),tokens) as server:
                thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
                thread.start()
                try:
                    url = "http://127.0.0.1:"+str(server.server_port)
                    worker = Worker(files["worker:oguri"],"oguri",Client(url,"fixture-worker"),
                                    HostAdapter(files["agent:oguri"],native,native.session))
                    manager = HermesAdapter(files["hermes:main"],Client(url,"manager"),
                        HostAdapter(files["hermes-host:main"],native_manager,native_manager.session),clock)
                    yield manager,worker
                finally:
                    server.shutdown()
                    thread.join()

        def recover(files,label):
            stored = self.root/(label+"-snapshot")
            seal = capture(files,"a"*40,stored)["manifest_sha256"]
            audit = audit_handoff(stored,seal)
            recovered = restore(stored,seal,self.root/(label+"-recovered"))
            return audit,{role:Path(path) for role,path in recovered.items()}

        def view(files,label):
            folder = self.root/label
            folder.mkdir()
            shutil.copyfile(files["router:main"],folder/"router.db")
            shutil.copyfile(self.trial/"baseline.json",folder/"baseline.json")
            return folder

        with boot(paths,drain=False) as (manager,worker):
            worker.connect()
            worker.poll()
            clock.advance(10)
            worker.poll()
            worker.poll()
            held = worker.hold()
            self.assertEqual(held["held_task_id"],tid)
            aid = held["held_assignment_id"]
            payload,native_receipt = native.entries()[0]
            self.assertEqual(worker.adapter.deliver(native_receipt["delivery_id"],payload),native_receipt)
            native.is_busy = True
        review = self.root/"drain-review.json"
        with self.assertRaisesRegex(ValueError,"closed after one W04"):
            stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
        self.assertFalse(self.restore.exists())
        audit,draining = recover(paths,"working")
        self.assertFalse(audit["needs_review"])
        self.assertEqual(audit["workers"][0]["router_assignment"],{"task_id":tid,"assignment_id":aid})

        attempts = []
        summary = "从原派发恢复并只报一次。"
        with boot(draining,drain=True) as (manager,worker):
            self.assertEqual(worker.connect()["reconcile"],"continue")
            worker.poll()
            self.assertEqual(worker.hold(),held)
            self.assertEqual(native.entries(),[(payload,native_receipt)])
            send = worker.router.request

            def lose_receipt(method,path,body=None,key=None,**kwargs):
                reply = send(method,path,body,key,**kwargs)
                if path==f"tasks/{tid}/report":
                    attempts.append((key,reply))
                    raise ConnectionError("simulated loss after durable W04")
                return reply

            worker.router.request = lose_receipt
            with self.assertRaisesRegex(ConnectionError,"after durable W04"):
                worker.report(native_receipt["native_id"],tid,aid,summary)
            self.assertEqual(worker.hold(),held)
            self.assertEqual(manager.inspect(tid)["task"]["state"],"completed_reported")

        pending_view = view(draining,"durable-report")
        with self.assertRaisesRegex(ValueError,"closed after one W04"):
            stage_reviewed_rollback(self.snapshot,pending_view,review,self.restore)
        self.assertFalse(self.restore.exists())
        pending,settling = recover(draining,"waiting-for-w04-receipt")
        self.assertTrue(pending["needs_review"])
        self.assertEqual([item["issue"] for item in pending["issues"]],
                         ["assignment_mismatch","report_receipt_pending"])

        with boot(settling,drain=True) as (manager,worker):
            send = worker.router.request

            def observe_retry(method,path,body=None,key=None,**kwargs):
                reply = send(method,path,body,key,**kwargs)
                if path==f"tasks/{tid}/report":
                    attempts.append((key,reply))
                return reply

            worker.router.request = observe_retry
            self.assertEqual(worker.connect()["release_assignment_ids"],[aid])
            self.assertEqual(len(attempts),2)
            self.assertEqual(attempts[0],attempts[1])
            self.assertEqual(attempts[0][1]["release_assignment_id"],aid)
            self.assertIsNone(worker.hold()["held_task_id"])
            native.is_busy = False
            worker.poll()
            manager.poll()
            clock.advance(12)
            worker.poll()
            worker.poll()
            manager.poll()
            state = manager.workers(worker_id="oguri")["items"][0]
            self.assertTrue(state["online"])
            self.assertFalse(state["busy"])
            self.assertIsNone(manager.inspect(waiting)["task"]["assignment_id"])
            self.assertEqual(native.entries(),[(payload,native_receipt)])
            notice, = native_manager.entries()
            self.assertEqual(notice[0]["task_id"],tid)
            delivered = manager.inspect(tid)["task"]
            self.assertEqual(delivered["state"],"completed_reported")
            self.assertEqual(manager.approve(guid(),tid,delivered["revision"])["task"]["state"],"closed")

        final_view = view(settling,"after-review")
        final_seal = capture(settling,"a"*40,self.root/"settled-snapshot")["manifest_sha256"]
        audited = audit_handoff(self.root/"settled-snapshot",final_seal)
        self.assertFalse(audited["needs_review"])
        self.assertTrue(audited["workers"][0]["native_session_ledger_clear"])
        with closing(sqlite3.connect(final_view/"router.db")) as db:
            count = db.execute("SELECT count(*) FROM deliveries WHERE kind='task.dispatch'").fetchone()[0]
        self.assertEqual(count,1)
        review.write_text(json.dumps({"trial_sha256":digest(final_view/"router.db"),"closed_assignments":{tid:aid}}))
        self.assertEqual(stage_reviewed_rollback(self.snapshot,final_view,review,self.restore)["reviewed_orders"],1)
        readback = json.loads(self.old("""
store = RouterStore(sys.argv[1])
tid = sys.argv[2]
print(json.dumps({"archived":store.orders.get(tid)["archive"]["reason"],"next":store.orders.next_dispatch("oguri"),
                  "held":store.orders.held("oguri")}))
store.close()
""",self.restore/"legacy.db",old_ids[0]))
        self.assertEqual(readback,{"archived":"reviewed_on_rewrite","next":old_ids[1],"held":None})
        self.drain_evidence = {"trial_task_id":tid,"assignment_id":aid,"native_receipt":native_receipt,
                               "lost_receipt_audit":pending["issues"],"w04_key":attempts[1][0],"w04_receipt":attempts[1][1],
                               "router_dispatches":count,"manager_native_notices":len(native_manager.entries()),
                               "next_trial_task_unassigned":waiting,"settled_snapshot_sha256":final_seal,
                               "legacy_readback":readback,"old_router_blocked_before_review":True,
                               "boundary":"Isolated rewrite HTTP and real legacy store; host, model and clock are fixtures."}

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

    def test_reconcile_distinguishes_unoffered_tasks_and_new_delivery_facts_without_writing(self):
        self.old("""
store = RouterStore(sys.argv[1])
store.upsert_worker("oguri")
store.close()
""",self.legacy)
        backup(self.legacy,self.attachments,self.snapshot)
        prepare(self.snapshot,self.trial,self.source_id)
        self.assertTrue(reconcile(self.snapshot,self.trial)["exact_rollback_allowed"])
        clock = Clock()
        @contextmanager
        def running():
            loop = Loop(self.trial,clock,FixtureHost)
            with loop.server:
                loop.thread.start()
                try:
                    yield loop
                finally:
                    loop.server.shutdown()
                    loop.thread.join()
        key = guid()
        with running() as loop:
            tid = loop.hermes.create(key,"Queued during trial","Preserve this instruction","oguri")["task_id"]
        before = digest(self.trial/"router.db")
        queued = reconcile(self.snapshot,self.trial)
        self.assertEqual(digest(self.trial/"router.db"),before)
        self.assertFalse(queued["exact_rollback_allowed"])
        self.assertTrue(queued["only_new_queued_tasks"])
        self.assertEqual(queued["queue_candidates"],[{"task_id":tid,"assignee_id":"oguri","request_id":key}])
        self.assertEqual(queued["changed_tables"],{
            "tasks":{"baseline_only":0,"trial_only":1},"operations":{"baseline_only":0,"trial_only":1}})
        with running() as loop:
            unknown = loop.hermes.create(guid(),"New identity","Pending","not-in-old")["task_id"]
            unassigned = loop.hermes.create(guid(),"Unassigned","Pending")["task_id"]
            loop.worker.connect()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            self.assertEqual(loop.worker.hold()["held_task_id"],tid)
            self.assertEqual(len(loop.native.entries()),1)
        before = digest(self.trial/"router.db")
        observed = reconcile(self.snapshot,self.trial)
        self.assertEqual(digest(self.trial/"router.db"),before)
        self.assertFalse(observed["only_new_queued_tasks"])
        self.assertEqual(observed["queue_candidates"],[])
        self.assertEqual(observed["blocked_new_tasks"],[
            {"task_id":tid,"reason":"assignment_or_lifecycle_recorded"},
            {"task_id":unknown,"reason":"assignee_not_registered_in_legacy_snapshot"},
            {"task_id":unassigned,"reason":"legacy_requires_assignee"}])
        self.assertIn("deliveries",observed["changed_tables"])
        self.assertIn("workers",observed["changed_tables"])
        with self.assertRaisesRegex(ValueError,"reconcile"):
            rollback(self.snapshot,self.trial,self.restore)
        self.assertFalse(self.restore.exists())
        baseline_path = self.trial/"baseline.json"
        wrong = json.loads(baseline_path.read_text())
        baseline_path.write_text(json.dumps({**wrong,"router_db_sha256":before}))
        with self.assertRaisesRegex(ValueError,"baseline could not be reproduced"):
            reconcile(self.snapshot,self.trial)

    def test_reviewed_new_orders_reenter_the_old_http_queue_once_and_preserve_its_head(self):
        self.old("""
store = RouterStore(sys.argv[1])
store.upsert_worker("two")
store.close()
""",self.legacy)
        backup(self.legacy,self.attachments,self.snapshot)
        prepare(self.snapshot,self.trial,self.source_id)
        loop = Loop(self.trial,Clock(),FixtureHost)
        direct_key = guid()
        with loop.server:
            loop.thread.start()
            try:
                first = loop.hermes.create(direct_key,"先入队","逐字保留\n第二行","two")
                tools = ManagerTools(loop.hermes,"isolated-hermes")
                params = {"title":"后入队","instructions":"逐字保留\n第二行","assignee_id":"two"}
                second = json.loads(tools.around_call("router_create_task",
                    lambda args:tools.invoke("router_create_task",args),params,
                    session_id="isolated-hermes",turn_id="runner-turn",tool_call_id="runner-call"))
                new_ids = [first["task_id"],second["task_id"]]
            finally:
                loop.server.shutdown()
                loop.thread.join()
        trial_sha = digest(self.trial/"router.db")
        review = self.root/"review.json"
        approval = {"trial_sha256":trial_sha,"execution_modes":dict.fromkeys(new_ids,"goal")}
        review.write_text(json.dumps({**approval,"trial_sha256":"0"*64}))
        with self.assertRaisesRegex(ValueError,"review must match"):
            stage_queue_rollback(self.snapshot,self.trial,review,self.restore)
        self.assertFalse(self.restore.exists())
        review.write_text(json.dumps(approval))
        staged = stage_queue_rollback(self.snapshot,self.trial,review,self.restore)
        self.assertEqual(staged["queued_requests"],2)
        self.assertEqual(staged["legacy_sha256"],digest(self.snapshot/"legacy.db"))
        plan = json.loads((self.restore/"queue-replay.json").read_text())
        example,converted = plan["requests"]
        self.assertEqual(example["trial_request_id"],direct_key)
        self.assertEqual(example["request"]["message_id"],direct_key)
        self.assertRegex(converted["trial_request_id"],r"^hermes-[0-9a-f]{64}$")
        other_stage = self.root/"same-replay-again"
        stage_queue_rollback(self.snapshot,self.trial,review,other_stage)
        self.assertEqual(json.loads((other_stage/"queue-replay.json").read_text()),plan)
        self.assertEqual((self.restore/"attachments"/self.sha).read_bytes(),(self.attachments/self.sha).read_bytes())
        observed = json.loads(self.old("""
from server import RouterServer
from selftest_router import http
from threading import Thread
from unittest.mock import Mock
store = RouterStore(sys.argv[1])
plan = json.loads(Path(sys.argv[2]).read_text())
mapping = {}
with RouterServer(("127.0.0.1",0),store,"offline-test",Mock(),Mock()) as srv:
    thread = Thread(target=srv.serve_forever,kwargs={"poll_interval":.02})
    thread.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/tasks"
        for item in plan["requests"]:
            body = item["request"]
            status,reply = http("POST",url,body,"offline-test")
            assert status==202 and reply["queued"] and not reply["dispatched"], (status,reply)
            again,duplicate = http("POST",url,body,"offline-test")
            assert again==200 and duplicate["duplicate"] and duplicate["task_id"]==reply["task_id"]
            clash,_ = http("POST",url,dict(body,goal="unexpected change"),"offline-test")
            assert clash==409
            mapping[item["trial_task_id"]] = reply["task_id"]
    finally:
        srv.shutdown()
        thread.join()
rows = store._conn.execute('''SELECT i.task_id,i.name,i.description,i.request_id,i.metadata FROM task_info i
                              JOIN dispatch_queue q USING(task_id) WHERE i.owner='two' ORDER BY q.seq''').fetchall()
print(json.dumps({"head":store.orders.next_dispatch("two"),"queue":[dict(r) for r in rows],"mapping":mapping}))
store.close()
""",self.restore/"legacy.db",self.restore/"queue-replay.json"))
        self.assertEqual(observed["head"],self.queued)
        inserted = observed["queue"][1:]
        self.assertEqual([row["task_id"] for row in inserted],[observed["mapping"][tid] for tid in new_ids])
        self.assertEqual([row["request_id"] for row in inserted],[row["request"]["message_id"] for row in plan["requests"]])
        self.assertEqual([row["name"] for row in inserted],["先入队","后入队"])
        self.assertTrue(all(row["description"]=="逐字保留\n第二行" and
                            json.loads(row["metadata"])["execution_mode"]=="goal" for row in inserted))
        self.assertEqual(digest(self.trial/"router.db"),trial_sha)

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
