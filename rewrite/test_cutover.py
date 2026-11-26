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
from .pool import REMINDER_NOTICE
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
                approval = loop.hermes.act(approval_key,tid,received["revision"],"close",note="已核原附件")
                self.assertEqual(approval["task"]["state"],"closed")
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
        self.assertEqual(saved["review_note"],"已核原附件")
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

    def test_reviewed_transfers_keep_formal_result_and_candidate_files_separate_on_rollback(self):
        self.exercise_reviewed_transfers(migrated=True)

    def test_reviewed_trial_born_transfer_uses_original_legacy_owner_and_request(self):
        self.exercise_reviewed_transfers(migrated=False)

    def exercise_reviewed_transfers(self, *, migrated):
        legacy_id = self.old("""
store = RouterStore(sys.argv[1])
for name in ("oguri","tama","rice"):
    store.upsert_worker(name)
if sys.argv[2]=="yes":
    print(store.orders.create("Original order","An isolated request","oguri",{},"fixture",None,None))
store.close()
""",self.legacy,"yes" if migrated else "no")
        baseline_source = digest(self.legacy)
        backup(self.legacy,self.attachments,self.snapshot)
        baseline = prepare(self.snapshot,self.trial,self.source_id)
        clock = Clock()
        loop = Loop(self.trial,clock,FixtureHost,extra_workers=["tama","rice"])
        manager = loop.hermes
        original_key = guid()
        review = self.root/"transfer-review.json"

        def register(worker):
            worker.connect()
            worker.poll()
            clock.advance(10)
            worker.poll()
            worker.poll()
            return worker.hold()["held_assignment_id"]

        def station(name):
            host = FixtureHost(self.trial/f"{name}-native.db",f"review-{name}")
            worker = Worker(self.trial/f"{name}-worker.db",name,loop.extra_clients[name],
                            HostAdapter(self.trial/f"{name}-adapter.db",host,host.session))
            return worker,host

        def upload(client,name,body):
            checksum = hashlib.sha256(body).hexdigest()
            ticket = client.request("POST","uploads",{"name":name,"size_bytes":len(body),"sha256":checksum},guid())
            item = client.request("PATCH","uploads/"+ticket["upload_id"],body,headers={"Upload-Offset":"0"})
            return item["attachment_id"],checksum

        def transfer(worker):
            return manager.act(guid(),tid,manager.inspect(tid)["task"]["revision"],"transfer",
                               worker_id=worker,reason="移交给下一工位")

        def finish_cleanup(worker,host,assignment):
            self.assertIn(assignment,[row["assignment_id"] for row in worker.connect()["pending_cleanup"]])
            worker.poll()
            control,receipt = next((p,r) for p,r in host.entries() if p["kind"]=="task.transfer_out")
            self.assertEqual(control["assignment_id"],assignment)
            host.cleanup_evidence = lambda session,aid,kind: {
                "session":session,"assignment_id":aid,"kind":kind,"stopped":True,"cleaned":True,
                "receipt_id":"fixture-verified-transfer"}
            late = worker.report(receipt["native_id"],tid,assignment,"cleaned","transfer_confirmed")
            self.assertEqual(late,{"recorded":True,"release_assignment_id":assignment,"task_state":"closed"})
            self.assertEqual(worker.report(receipt["native_id"],tid,assignment,"cleaned","transfer_confirmed"),late)
            return late

        private_files = []
        with loop.server:
            loop.thread.start()
            try:
                tid = (baseline["queued_id_map"][legacy_id] if migrated else
                       manager.create(original_key,"Original order","An isolated request","oguri")["task_id"])
                former = register(loop.worker)
                self.assertTrue(former)
                if migrated:
                    clock.advance(630)
                    self.assertEqual(manager.inspect(tid)["task"]["state"],"blocked")
                    current = manager.inspect(tid)["task"]
                    transfer_snooze = guid()
                    manager.act(transfer_snooze,tid,current["revision"],"snooze",remind_after_hours=0.25)
                    clock.advance(900)
                    self.assertIsNone(manager.inspect(tid)["task"]["remind_at"])
                else:
                    for seconds in (1,10,1,1,1,1):
                        clock.advance(seconds)
                        loop.worker.poll()
                        loop.worker.poll()
                    self.assertEqual(manager.inspect(tid)["task"]["state"],"blocked")
                    self.assertIsNone(loop.worker.hold()["held_assignment_id"])
                original = b"old material bytes"
                first,first_sha = upload(loop.wire,"material.bin",original)
                private_files.append((first,first_sha,original))
                material_key = guid()
                material = {"session_id":loop.worker.session,"assignment_id":former,"text":"旧派发的候选",
                            "artifact_ids":[first]}
                if migrated:
                    record = loop.wire.request("POST",f"tasks/{tid}/materials",material,material_key)
                else:
                    shelved, = [receipt for payload,receipt in loop.native.entries() if payload["kind"]=="task.shelved"]
                    record = loop.worker.submit_material(loop.native.session,shelved["native_id"],material_key,
                        {**material,"task_id":tid})
                transfer("tama")
                tama,host2 = station("tama")
                second = register(tama)
                if migrated:
                    original = b"second worker blockage"
                    blocked_file,blocked_sha = upload(tama.router,"blocking.bin",original)
                    private_files.append((blocked_file,blocked_sha,original))
                    blocked = tama.report(host2.entries()[0][1]["native_id"],tid,second,"工位受阻","blocked",[blocked_file])
                    self.assertEqual(blocked["task_state"],"blocked")
                rice,host3 = station("rice")
                if migrated:
                    transfer("rice")
                    final = register(rice)
                else:
                    self.assertIsNone(register(rice))
                    offered = transfer("rice")["task"]
                    self.assertTrue(offered["assignment_id"])
                    rice.poll()
                    final = rice.hold()["held_assignment_id"]
                    self.assertEqual(final,offered["assignment_id"])
                original = b"final result bytes"
                result_file,result_sha = upload(rice.router,"final.bin",original)
                private_files.append((result_file,result_sha,original))
                result = agent_report(rice,host3.entries()[0][1]["native_id"],
                        {"task_id":tid,"assignment_id":final,"summary":"第三工位交回的正式成果","artifact_ids":[result_file]})
                self.assertFalse(result["isError"],result)
                detail = manager.inspect(tid)
                self.assertEqual(detail["last_report"]["assignment_id"],final)
                close_key = guid()
                closed = manager.approve(close_key,tid,detail["task"]["revision"])
                self.assertEqual(closed["task"]["state"],"closed")
                manager.poll()
                review.write_text(json.dumps({"trial_sha256":digest(self.trial/"router.db"),"closed_assignments":{tid:final}}))
                with self.assertRaisesRegex(ValueError,"cleanup is pending"):
                    stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
                self.assertFalse(self.restore.exists())

                late = finish_cleanup(loop.worker,loop.native,former)
                if not migrated:
                    later = finish_cleanup(tama,host2,second)
                self.assertEqual(loop.wire.request("POST",f"tasks/{tid}/materials",material,material_key),record)
                for worker in (loop.worker,tama,rice):
                    worker.poll()
                self.assertEqual(manager.inspect(tid)["task"],closed["task"])
            finally:
                loop.server.shutdown()
                loop.thread.join()
        trial_digest = digest(self.trial/"router.db")
        review.write_text(json.dumps({"trial_sha256":trial_digest,"closed_assignments":{tid:final}}))
        staged = stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
        self.assertEqual((staged["reviewed_orders"],staged["private_artifacts"]),(1,len(private_files)))
        private = self.restore/"private-review"
        manifest = json.loads((private/"manifest.json").read_text())
        archived = manifest["orders"][tid]
        self.assertEqual((archived["original_worker"],archived["worker_id"],archived["review_key"]),("oguri","rice",close_key))
        orig,intermediate,completed = archived["assignment_history"]
        self.assertEqual([step["assignment_id"] for step in (orig,intermediate,completed)],[former,second,final])
        self.assertEqual(orig["cleanup"]["receipt"],late)
        self.assertEqual(orig["materials"][0]["material_id"],record["material_id"])
        self.assertEqual(orig["materials"][0]["receipt_key"],material_key)
        if migrated:
            self.assertEqual(intermediate["pool"]["artifact_ids"],[blocked_file])
            schedule, = orig["pool"]["snoozes"]
            self.assertEqual(schedule["action_key"],transfer_snooze)
            self.assertIn(schedule["reminder_message_id"],
                          {notice["payload"]["message_id"] for notice in archived["pool_notices"]})
        else:
            self.assertEqual(intermediate["cleanup"]["receipt"],later)
            self.assertEqual(orig["pool"]["source"],"unanswered")
            self.assertEqual(len(orig["probe_delivery_ids"]),4)
            self.assertTrue(orig["shelved_delivery_id"])
        self.assertEqual(archived["artifact_ids"],[result_file])
        for file,_,body in private_files:
            self.assertEqual((private/"files"/file).read_bytes(),body)
        restored_id = archived["legacy_task_id"]
        if migrated:
            self.assertEqual(restored_id,legacy_id)
        else:
            self.assertEqual(archived["legacy_creation_request"]["message_id"],original_key)
        observed = json.loads(self.old("""
from server import RouterServer
from selftest_router import http
from threading import Thread
from unittest.mock import Mock
store = RouterStore(sys.argv[1])
token = "isolated-legacy-readback"
with RouterServer(("127.0.0.1",0),store,token,Mock(),Mock()) as server:
    runner = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
    runner.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        status, item = http("GET",url+"/tasks/"+sys.argv[2],token=token)
        request = json.loads(sys.argv[3])
        files = [http("GET",url+"/attachments/"+sha,token=token)[0] for sha in sys.argv[4:]]
        replay = http("POST",url+"/tasks",request,token) if request else None
        print(json.dumps({"status":status,"archive":item["task"]["archive"],
                          "owner":item["task"]["worker"],"deliverable":item["task"]["deliverable"],
                          "queues":[store.orders.next_dispatch(name) for name in ("oguri","tama","rice","two")],
                          "attachment_statuses":files,"replay":replay}))
    finally:
        server.shutdown()
        runner.join()
store.close()
""",self.restore/"legacy.db",restored_id,json.dumps(archived.get("legacy_creation_request")),
           *(checksum for _,checksum,_ in private_files)))
        self.assertEqual((observed["status"],observed["owner"],observed["deliverable"]),(200,"oguri",None))
        self.assertEqual(observed["archive"]["recovery_reference"],tid)
        self.assertEqual(observed["queues"],[None,None,None,self.queued])
        self.assertEqual(observed["attachment_statuses"],[404]*len(private_files))
        if not migrated:
            self.assertEqual((observed["replay"][0],observed["replay"][1]["task_id"]),(200,restored_id))
        self.assertEqual((digest(self.legacy),digest(self.trial/"router.db")),(baseline_source,trial_digest))
        inconsistent = self.root/"inconsistent-transfer"
        inconsistent.mkdir()
        shutil.copyfile(self.trial/"baseline.json",inconsistent/"baseline.json")
        shutil.copyfile(self.trial/"router.db",inconsistent/"router.db")
        with closing(sqlite3.connect(inconsistent/"router.db")) as db,db:
            changed = db.execute("UPDATE operations SET response=? WHERE actor='worker/oguri' AND key=?",
                                 (json.dumps(dict(late,task_state="working")),orig["cleanup"]["report_key"]))
            self.assertEqual(changed.rowcount,1)
        review.write_text(json.dumps({"trial_sha256":digest(inconsistent/"router.db"),"closed_assignments":{tid:final}}))
        refused = self.root/"unverified-transfer"
        with self.assertRaisesRegex(ValueError,"changes the state of a different assignment"):
            stage_reviewed_rollback(self.snapshot,inconsistent,review,refused)
        self.assertFalse(refused.exists())
        if not migrated:
            with closing(sqlite3.connect(inconsistent/"router.db")) as db,db:
                db.execute("UPDATE operations SET response=? WHERE actor='worker/oguri' AND key=?",
                           (json.dumps(late),orig["cleanup"]["report_key"]))
                db.execute("DELETE FROM deliveries WHERE delivery_id=?",(orig["probe_delivery_ids"][0],))
            review.write_text(json.dumps({"trial_sha256":digest(inconsistent/"router.db"),"closed_assignments":{tid:final}}))
            with self.assertRaisesRegex(ValueError,"inquiry deliveries do not match"):
                stage_reviewed_rollback(self.snapshot,inconsistent,review,refused)
            self.assertFalse(refused.exists())
        if output := os.environ.get("MOWULINK_TRANSFER_ROLLBACK_EVIDENCE"):
            path = Path(output)
            proofs = json.loads(path.read_text()) if path.exists() else {}
            proofs["migrated" if migrated else "trial_born"] = {
                "task_id":tid,"assignment_ids":[former,second,final],"material_id":record["material_id"],
                "late_cleanups":[late] if migrated else [late,later],
                "entry_pool_source":orig["pool"]["source"],
                "probe_delivery_ids":orig.get("probe_delivery_ids",[]),
                "shelved_delivery_id":orig.get("shelved_delivery_id"),
                "retained_files_sha256":[checksum for _,checksum,_ in private_files],
                "legacy":observed,"trial_sha256":trial_digest,"snapshot_sha256":baseline_source,
                "pending_cleanup_refused":True,"inconsistent_late_cleanup_refused":True,
                "boundary":"Actual old and new HTTP Routers, SQLite, Worker W03/W04 and "
                "candidate upload. Scripted host and agent, controlled clock."}
            if not migrated:
                proofs["trial_born"]["missing_inquiry_refused"] = True
            else:
                proofs["migrated"]["original_snooze"] = schedule
            path.write_text(json.dumps(proofs,ensure_ascii=False,indent=2)+"\n")

    def test_cancelled_orders_roll_back_without_replaying_work_or_inventing_results(self):
        old_id = self.old("""
store = RouterStore(sys.argv[1])
for name in ("oguri","tama"):
    store.upsert_worker(name)
print(store.orders.create("Migrated cancellation","Isolated trial","oguri",{},"fixture",None,None))
store.close()
""",self.legacy)
        original = digest(self.legacy)
        backup(self.legacy,self.attachments,self.snapshot)
        baseline = prepare(self.snapshot,self.trial,self.source_id)
        clock = Clock()
        loop = Loop(self.trial,clock,FixtureHost,extra_workers=["tama"])
        review = self.root/"cancel-review.json"
        ids, records = {}, {}

        def write_review():
            review.write_text(json.dumps({"trial_sha256":digest(self.trial/"router.db"),"closed_assignments":{},
                                         "cancelled_assignments":ids}))

        def cancel(tid, aid, phase):
            before = loop.hermes.inspect(tid)["task"]
            key = guid()
            reason = "隔离取消演练 "+phase
            receipt = loop.hermes.act(key,tid,before["revision"],"cancel",reason=reason)
            self.assertEqual(loop.hermes.act(key,tid,before["revision"],"cancel",reason=reason),receipt)
            ids[tid] = aid
            records[phase] = {"task_id":tid,"assignment_id":aid,"action":key,
                              "manager_reply":receipt["delivery_state"],"before":before["state"]}
            return receipt

        def create(phase):
            manager = ManagerTools(loop.hermes,"isolated-cancel-manager")
            order = json.loads(manager.around_call("router_create_task",
                lambda args:manager.invoke("router_create_task",args),
                {"title":"Cancellation "+phase,"instructions":"Isolated trial","assignee_id":"oguri"},
                session_id="isolated-cancel-manager",turn_id="cancel-rehearsal",tool_call_id=phase))
            return order["task_id"]

        def offer(tid):
            for _ in range(3):
                item = loop.hermes.inspect(tid)["task"]
                self.assertEqual(item["state"],"ready")
                if offer_id := item["assignment_id"]:
                    return offer_id
                loop.worker.poll()
                clock.advance(10)
            item = loop.hermes.inspect(tid)["task"]
            self.assertEqual(item["state"],"ready")
            self.assertTrue(item["assignment_id"],"idle worker did not receive an offer")
            return item["assignment_id"]

        def accept(tid):
            aid = loop.hermes.inspect(tid)["task"]["assignment_id"] or offer(tid)
            loop.worker.poll()
            self.assertEqual(loop.worker.hold()["held_assignment_id"],aid)
            native, = [receipt["native_id"] for frame,receipt in loop.native.entries()
                       if frame.get("assignment_id")==aid and frame["kind"]=="task"]
            return aid,native

        def finish(tid, aid, phase):
            loop.worker.poll()
            native = next(receipt["native_id"] for frame,receipt in loop.native.entries()
                          if frame.get("assignment_id")==aid and frame["kind"]=="task.cancel")
            def report():
                return loop.worker.report(native,tid,aid,"cleaned","cancel_confirmed")
            with self.assertRaises(Fault) as error:
                report()
            self.assertEqual(error.exception.code,"native_cleanup_unconfirmed")
            loop.native.cleanup_evidence = lambda session,assignment,kind: {
                "session":session,"assignment_id":assignment,"kind":kind,"stopped":True,
                "cleaned":True,"receipt_id":"isolated-verified-"+aid}
            confirmed = report()
            self.assertEqual(report(),confirmed)
            del loop.native.cleanup_evidence
            self.assertEqual(confirmed,{"recorded":True,"release_assignment_id":aid,"task_state":"cancelled"})
            self.assertIsNone(loop.worker.hold()["held_assignment_id"])
            self.assertIsNone(loop.hermes.inspect(tid)["last_report"])
            records[phase]["worker_reply"] = confirmed
            loop.worker.poll()

        with loop.server:
            loop.thread.start()
            try:
                tid = baseline["queued_id_map"][old_id]
                self.assertEqual(cancel(tid,None,"unoffered")["task"]["state"],"cancelled")
                loop.worker.connect()
                tid = create("offered")
                aid = offer(tid)
                frame, = [frame for frame in loop.wire.frames(0) if frame["kind"]=="task.dispatch"]
                self.assertEqual(frame["payload"]["assignment_id"],aid)
                self.assertEqual(cancel(tid,aid,"offered")["task"]["state"],"cancelled")
                for query in loop.wire.frames(0):
                    if query["kind"]=="worker.status_query":
                        loop.worker.observe(query["payload"]["query_id"])
                        loop.wire.ack(query)
                write_review()
                with self.assertRaisesRegex(ValueError,"no consumer acknowledgement"):
                    stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
                self.assertFalse(self.restore.exists())
                loop.wire.ack(frame)
                self.assertEqual(loop.native.entries(),[])

                tid = create("active")
                aid,_ = accept(tid)
                self.assertEqual(cancel(tid,aid,"active")["delivery_state"],"pending")
                self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"working")
                write_review()
                with self.assertRaisesRegex(ValueError,"outside the supported reviewed orders"):
                    stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
                self.assertFalse(self.restore.exists())
                for seconds in (1,10,1):
                    clock.advance(seconds)
                    loop.worker.poll()
                finish(tid,aid,"active")

                tid = create("worker_pool")
                aid,native = accept(tid)
                blocked = loop.worker.report(native,tid,aid,"已主动释放工单","blocked")
                self.assertEqual(blocked["task_state"],"blocked")
                candidate = loop.wire.request("POST",f"tasks/{tid}/materials",
                    {"session_id":loop.worker.session,"assignment_id":aid,"text":"仅候选材料"},guid())
                self.assertEqual(cancel(tid,aid,"worker_pool")["task"]["state"],"cancelled")
                records["worker_pool"]["material_id"] = candidate["material_id"]

                tid = create("offline_pool")
                aid,_ = accept(tid)
                clock.advance(630)
                self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"blocked")
                current = loop.hermes.inspect(tid)["task"]
                cancel_snooze = guid()
                loop.hermes.act(cancel_snooze,tid,current["revision"],"snooze",remind_after_hours=0.25)
                clock.advance(900)
                queued_cancel = cancel(tid,aid,"offline_pool")
                self.assertEqual((queued_cancel["task"]["state"],queued_cancel["delivery_state"]),("snoozed","pending"))
                self.assertIsNone(queued_cancel["task"]["remind_at"])
                loop.worker.connect()
                finish(tid,aid,"offline_pool")

                tid = create("transferred_offer")
                former,_ = accept(tid)
                tama_host = FixtureHost(self.trial/"tama-host.db","isolated-tama")
                tama = Worker(self.trial/"tama.db","tama",loop.extra_clients["tama"],
                              HostAdapter(self.trial/"tama-adapter.db",tama_host,tama_host.session))
                tama.connect()
                for seconds in (1,10,1):
                    clock.advance(seconds)
                    loop.worker.poll()
                    tama.poll()
                detail = loop.hermes.inspect(tid)["task"]
                self.assertEqual(detail["state"],"working")
                offer_after_transfer = loop.hermes.act(guid(),tid,detail["revision"],"transfer",worker_id="tama",
                                                       reason="交给另一工位")
                final = offer_after_transfer["task"]["assignment_id"]
                self.assertTrue(final)
                frame, = [frame for frame in tama.router.frames(0) if frame["kind"]=="task.dispatch"]
                tama.router.ack(frame)
                self.assertEqual(cancel(tid,final,"transferred_offer")["task"]["state"],"cancelled")
                self.assertEqual(tama_host.entries(),[])
                loop.worker.poll()
                native, = [receipt["native_id"] for frame,receipt in loop.native.entries()
                           if frame.get("assignment_id")==former and frame["kind"]=="task.transfer_out"]
                loop.native.cleanup_evidence = lambda session,assignment,kind: {
                    "session":session,"assignment_id":assignment,"kind":kind,"stopped":True,
                    "cleaned":True,"receipt_id":"isolated-transfer-"+former}
                late = loop.worker.report(native,tid,former,"cleaned","transfer_confirmed")
                self.assertEqual(late,{"recorded":True,"release_assignment_id":former,"task_state":"cancelled"})
                self.assertEqual(loop.worker.report(native,tid,former,"cleaned","transfer_confirmed"),late)
                records["transferred_offer"]["older_cleanup"] = late
                loop.hermes.poll()
                loop.worker.poll()
                tama.poll()
            finally:
                loop.server.shutdown()
                loop.thread.join()

        write_review()
        reviewed = stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
        self.assertEqual((reviewed["reviewed_orders"],reviewed["cancelled_orders"]),(6,6))
        private = self.restore/"private-review"
        saved = json.loads((private/"manifest.json").read_text())["orders"]
        for phase,record in records.items():
            entry = saved[record["task_id"]]
            record["legacy_id"] = entry["legacy_task_id"]
            self.assertEqual((entry["terminal_state"],entry["cancel_key"]),("cancelled",record["action"]))
            self.assertNotIn("summary",entry)
            self.assertEqual("cancel_w04_key" in entry,"worker_reply" in record)
            if "worker_reply" in record:
                cleanup = entry["assignment_history"][-1]["cleanup"]
                record["cancel_deliveries"] = cleanup["delivery_ids"]
                record["w04_key"] = entry["cancel_w04_key"]
                self.assertEqual(cleanup["receipt"],record["worker_reply"])
        self.assertEqual(records["unoffered"]["legacy_id"],old_id)
        self.assertGreater(len(records["active"]["cancel_deliveries"]),1)
        blocked = saved[records["worker_pool"]["task_id"]]["assignment_history"][-1]
        self.assertEqual(blocked["materials"][0]["material_id"],records["worker_pool"]["material_id"])
        self.assertTrue(blocked["pool"]["report_key"])
        transferred = saved[records["transferred_offer"]["task_id"]]["assignment_history"]
        self.assertEqual(transferred[0]["cleanup"]["receipt"],late)
        self.assertNotIn("accept_key",transferred[1])
        cancelled_pool = saved[records["offline_pool"]["task_id"]]["assignment_history"][-1]["pool"]
        schedule, = cancelled_pool["snoozes"]
        self.assertEqual(schedule["action_key"],cancel_snooze)
        records["offline_pool"]["snooze"] = schedule
        replay = {row["legacy_task_id"]:row.get("legacy_creation_request") for row in saved.values()}
        observed = json.loads(self.old("""
from server import RouterServer
from selftest_router import http
from threading import Thread
from unittest.mock import Mock
store = RouterStore(sys.argv[1])
token = "isolated-legacy-cancellation"
with RouterServer(("127.0.0.1",0),store,token,Mock(),Mock()) as server:
    runner = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
    runner.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        archives, duplicates = {}, {}
        for old_id,request in json.loads(sys.argv[2]).items():
            status, item = http("GET",url+"/tasks/"+old_id,token=token)
            assert status==200 and item["task"]["deliverable"] is None, item
            archives[old_id] = item["task"]["archive"]
            if request:
                code, reply = http("POST",url+"/tasks",request,token)
                assert code==200 and reply["duplicate"] and reply["task_id"]==old_id, reply
                duplicates[old_id] = code
        print(json.dumps({"archives":archives,"next":store.orders.next_dispatch("oguri"),
                          "held":store.orders.held("oguri"),"duplicates":duplicates,
                          "empty_deliverables":len(archives)}))
    finally:
        server.shutdown()
        runner.join()
store.close()
""",self.restore/"legacy.db",json.dumps(replay)))
        self.assertEqual((observed["next"],observed["held"]),(None,None))
        self.assertEqual((observed["empty_deliverables"],len(observed["duplicates"])),(6,5))
        for record in records.values():
            archive = observed["archives"][record["legacy_id"]]
            self.assertEqual(archive["reason"],"cancelled_on_rewrite")
            self.assertEqual(archive["recovery_reference"],record["task_id"])
            self.assertIs(archive["confirmed_by_worker"],"worker_reply" in record)

        again_snapshot,again = self.root/"cancel-again-snapshot",self.root/"cancel-again"
        backup(self.restore/"legacy.db",self.restore/"legacy.db.attachments",again_snapshot)
        next_source = guid()
        baselines = prepare(again_snapshot,again,next_source)
        self.assertTrue(set(replay).isdisjoint(baselines["queued_id_map"]))
        token = guid()
        with serve_loopback(Orders(again/"router.db",Clock()),{token:"hermes"}) as server:
            runner = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
            runner.start()
            try:
                client = Client(f"http://127.0.0.1:{server.server_port}",token)
                for entry in saved.values():
                    request = entry.get("legacy_creation_request")
                    if not request:
                        continue
                    body = {"title":request["TaskName"],"instructions":request["goal"],"assignee_id":request["worker"]}
                    for key in (entry["creation_key"],request["message_id"]):
                        with self.assertRaises(Fault) as duplicate:
                            client.request("POST","tasks",body,key)
                        self.assertEqual((duplicate.exception.status,duplicate.exception.code),
                                         (409,"legacy_request_recorded"))
                        self.assertEqual(duplicate.exception.details,
                                         {"source_id":next_source,"legacy_task_id":entry["legacy_task_id"]})
            finally:
                server.shutdown()
                runner.join()

        damaged = self.root/"damaged-cancel-ledger"
        damaged.mkdir()
        shutil.copyfile(self.trial/"baseline.json",damaged/"baseline.json")
        shutil.copyfile(self.trial/"router.db",damaged/"router.db")
        with closing(sqlite3.connect(damaged/"router.db")) as db,db:
            deleted = db.execute("DELETE FROM operations WHERE actor='worker/oguri' AND key=?",
                                 (records["active"]["w04_key"],))
            self.assertEqual(deleted.rowcount,1)
        approval = json.loads(review.read_text())
        review.write_text(json.dumps(dict(approval,trial_sha256=digest(damaged/"router.db"))))
        rejected = self.root/"rejected"
        with self.assertRaisesRegex(ValueError,"missing or conflicting receipt.*report"):
            stage_reviewed_rollback(self.snapshot,damaged,review,rejected)
        self.assertFalse(rejected.exists())
        self.assertEqual((digest(self.legacy),digest(self.trial/"router.db")),(original,reviewed["trial_sha256"]))
        if output := os.environ.get("MOWULINK_CANCEL_ROLLBACK_EVIDENCE"):
            evidence = {"cancelled":records,"legacy":observed,"trial_sha256":reviewed["trial_sha256"],
                        "legacy_snapshot_sha256":original,"pending_and_missing_w04_refused":True,
                        "unacknowledged_offer_refused":True,"remigrated_key_duplicates_rejected":True,
                        "boundary":"Actual old and new HTTP Routers, temporary SQLite databases, scripted host and clock."}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")

    def test_pool_archives_only_return_after_the_worker_released_the_assignment(self):
        old_id = self.old("""
store = RouterStore(sys.argv[1])
store.upsert_worker("oguri")
print(store.orders.create("Review candidate","Isolated order","oguri",{},"fixture",None,None))
store.close()
""",self.legacy)
        source_sha = digest(self.legacy)
        backup(self.legacy,self.attachments,self.snapshot)
        baseline = prepare(self.snapshot,self.trial,self.source_id)
        clock = Clock()
        loop = Loop(self.trial,clock,FixtureHost)
        manager,worker = loop.hermes,loop.worker
        review = self.root/"pool-review.json"
        approvals = {}
        content = b"isolated source material"
        checksum = hashlib.sha256(content).hexdigest()

        def take(tid):
            for _ in range(5):
                worker.poll()
                if worker.hold()["held_task_id"]==tid:
                    aid = worker.hold()["held_assignment_id"]
                    native = loop.native.entries()[-1][1]["native_id"]
                    return aid,native
                clock.advance(10)
            self.fail("worker never accepted test order")

        def resolve(tid, aid, action, **argument):
            detail = manager.inspect(tid)["task"]
            args = (guid(),tid,detail["revision"],action)
            receipt = manager.act(*args,**argument)
            self.assertEqual(manager.act(*args,**argument),receipt)
            approvals[tid] = {"assignment_id":aid,"state":receipt["task"]["state"]}
            return args[0]

        def stage(destination):
            review.write_text(json.dumps({"trial_sha256":digest(self.trial/"router.db"),
                                         "closed_assignments":{},"pool_archives":approvals}))
            return stage_reviewed_rollback(self.snapshot,self.trial,review,destination)

        def postpone(tid, hours):
            key = guid()
            revision = manager.inspect(tid)["task"]["revision"]
            reply = manager.act(key,tid,revision,"snooze",remind_after_hours=hours)
            self.assertEqual(manager.act(key,tid,revision,"snooze",remind_after_hours=hours),reply)
            self.assertEqual(reply["task"]["state"],"snoozed")
            return key,reply["task"]["remind_at"]

        def reminders(tid):
            return [frame["payload"]["message_id"] for frame in loop.manager_wire.frames(0)
                    if frame["payload"].get("task_id")==tid and frame["payload"].get("text")==REMINDER_NOTICE]

        with loop.server:
            loop.thread.start()
            try:
                worker.connect()
                closed = baseline["queued_id_map"][old_id]
                aid,native = take(closed)
                clock.advance(0.000002)
                self.assertEqual(worker.report(native,closed,aid,"Needs review","blocked")["task_state"],"blocked")
                upload = loop.wire.request("POST","uploads",
                    {"name":"candidate.txt","size_bytes":len(content),"sha256":checksum},guid())["upload_id"]
                attachment = loop.wire.request("PATCH","uploads/"+upload,content,
                                                headers={"Upload-Offset":"0"})["attachment_id"]
                material = loop.wire.request("POST",f"tasks/{closed}/materials",
                    {"session_id":worker.session,"assignment_id":aid,"text":"候选资料","artifact_ids":[attachment]},guid())
                far_future,far_deadline = postpone(closed,60_000_000)
                clock.advance(1)
                superseded,old_deadline = postpone(closed,0.25)
                clock.advance(60)
                first,first_deadline = postpone(closed,0.5)
                clock.advance(840)
                self.assertEqual(reminders(closed),[])
                clock.advance(960)
                first_notice, = reminders(closed)
                second,second_deadline = postpone(closed,0.25)
                clock.advance(900)
                first_notice_again, second_notice = reminders(closed)
                self.assertEqual(first_notice_again,first_notice)
                cancelled_timer,last_deadline = postpone(closed,0.25)
                close_key = resolve(closed,aid,"close",material_id=material["material_id"],note="原材料已核验")
                clock.advance(900)
                self.assertEqual(reminders(closed),[first_notice,second_notice])

                tools = ManagerTools(manager,"pool-archive-manager")
                created = tools.around_call("router_create_task",lambda args:tools.invoke("router_create_task",args),
                    {"title":"Archive failure","instructions":"Isolated order","assignee_id":"oguri"},
                    session_id="pool-archive-manager",turn_id="failure-archive",tool_call_id="create")
                failed = json.loads(created)["task_id"]
                fail_aid,native = take(failed)
                self.assertEqual(worker.report(native,failed,fail_aid,"Cannot continue","blocked")["task_state"],"blocked")
                clock.advance(3600)
                default_notice, = reminders(failed)
                fail_key = resolve(failed,fail_aid,"archive_failed",reason="原任务缺必要条件")
                worker.poll()
                without_ack = self.root/"pool-reminder-unacknowledged"
                with self.assertRaisesRegex(ValueError,"no consumer acknowledgement"):
                    stage(without_ack)
                self.assertFalse(without_ack.exists())
                manager.poll()
                worker.poll()
                self.assertEqual(manager.inspect(closed)["last_report"]["outcome"],"blocked")
                self.assertEqual(stage(self.restore)["pool_archived_orders"],2)
                settled_trial = digest(self.trial/"router.db")

                unsafe = manager.create(guid(),"Host may still be working","Isolated order","oguri")["task_id"]
                unsafe_aid,_ = take(unsafe)
                loop.native.is_busy = True
                clock.advance(630)
                self.assertEqual(manager.inspect(unsafe)["task"]["state"],"blocked")
                resolve(unsafe,unsafe_aid,"archive_failed",reason="业务状态可以归档，原生仍待核")
                manager.poll()
                self.assertEqual(worker.hold()["held_assignment_id"],unsafe_aid)
                self.assertTrue(loop.native.is_busy)
                rejected = self.root/"rejected-pool-archive"
                with self.assertRaisesRegex(ValueError,"final worker released"):
                    stage(rejected)
                self.assertFalse(rejected.exists())
            finally:
                loop.server.shutdown()
                loop.thread.join()

        private = self.restore/"private-review"
        manifest = json.loads((private/"manifest.json").read_text())
        self.assertEqual(digest(private/"trial-router.db"),settled_trial)
        self.assertEqual(digest(self.legacy),source_sha)
        self.assertEqual((private/"files"/attachment).read_bytes(),content)
        for tid,key,action in ((closed,close_key,"close"),(failed,fail_key,"archive_failed")):
            item = manifest["orders"][tid]
            self.assertNotIn("summary",item)
            self.assertTrue(item["assignment_history"][-1]["pool"]["report_key"])
            self.assertEqual((item["pool_archive"]["action_key"],item["pool_archive"]["request"]["action"]),(key,action))
        self.assertEqual(manifest["orders"][closed]["pool_archive"]["request"]["material_id"],material["material_id"])
        closed_pool = manifest["orders"][closed]["assignment_history"][-1]["pool"]
        schedules = closed_pool["snoozes"]
        self.assertEqual([(rec["action_key"],rec["remind_at"],rec.get("reminder_message_id")) for rec in schedules],
                         [(far_future,far_deadline,None),(superseded,old_deadline,None),(first,first_deadline,first_notice),
                          (second,second_deadline,second_notice),(cancelled_timer,last_deadline,None)])
        initial = manifest["orders"][failed]["assignment_history"][-1]["pool"]["initial_reminder"]
        self.assertEqual(initial["reminder_message_id"],default_notice)
        damaged = self.root/"premature-pool-reminder"
        damaged.mkdir()
        shutil.copyfile(self.trial/"baseline.json",damaged/"baseline.json")
        shutil.copyfile(private/"trial-router.db",damaged/"router.db")
        with closing(sqlite3.connect(damaged/"router.db")) as db,db:
            changed = db.execute("UPDATE messages SET received_at=? WHERE message_id=?",(old_deadline,first_notice))
            self.assertEqual(changed.rowcount,1)
        review.write_text(json.dumps({"trial_sha256":digest(damaged/"router.db"),"closed_assignments":{},
                                     "pool_archives":manifest["pool_archives"]}))
        cannot_restore = self.root/"premature-rollback"
        with self.assertRaisesRegex(ValueError,"reminder does not match an outstanding deadline"):
            stage_reviewed_rollback(self.snapshot,damaged,review,cannot_restore)
        self.assertFalse(cannot_restore.exists())
        fail_id = manifest["orders"][failed]["legacy_task_id"]
        observed = json.loads(self.old("""
from server import RouterServer
from selftest_router import http
from threading import Thread
from unittest.mock import Mock
store = RouterStore(sys.argv[1])
token = "isolated-legacy-pool-archive"
with RouterServer(("127.0.0.1",0),store,token,Mock(),Mock()) as server:
    runner = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
    runner.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        records = {}
        for tid in sys.argv[2:4]:
            status,item = http("GET",url+"/tasks/"+tid,token=token)
            assert status==200 and item["task"]["deliverable"] is None, item
            records[tid] = item["task"]["archive"]["reason"]
        replay_status,replay = http("POST",url+"/tasks",json.loads(sys.argv[4]),token)
        assert replay_status==200 and replay["duplicate"] and replay["task_id"]==sys.argv[3], replay
        file_status,_ = http("GET",url+"/attachments/"+sys.argv[5],token=token)
        print(json.dumps({"archives":records,"file_status":file_status,"replay_status":replay_status,
                          "next":store.orders.next_dispatch("oguri"),"held":store.orders.held("oguri")}))
    finally:
        server.shutdown()
        runner.join()
store.close()
""",self.restore/"legacy.db",old_id,fail_id,json.dumps(manifest["orders"][failed]["legacy_creation_request"]),checksum))
        self.assertEqual(observed["archives"],{old_id:"pool_closed_on_rewrite",fail_id:"failed_on_rewrite"})
        self.assertEqual((observed["file_status"],observed["next"],observed["held"]),(404,None,None))

        snapshot,trial = self.root/"pool-archive-snapshot",self.root/"pool-archive-remigration"
        backup(self.restore/"legacy.db",self.restore/"legacy.db.attachments",snapshot)
        self.assertNotIn(fail_id,prepare(snapshot,trial,guid())["queued_id_map"])
        entry = manifest["orders"][failed]
        with closing(sqlite3.connect(trial/"router.db")) as db:
            keys = db.execute("SELECT request_key FROM history_request_keys WHERE actor='hermes' AND legacy_task_id=?",
                              (fail_id,)).fetchall()
        self.assertEqual({row[0] for row in keys},{entry["creation_key"],entry["legacy_creation_request"]["message_id"]})
        if output := os.environ.get("MOWULINK_POOL_ARCHIVE_EVIDENCE"):
            evidence = {"adopted":{"task_id":closed,"assignment_id":aid,"action_key":close_key,
                                   "material_id":material["material_id"],"file_sha256":checksum},
                        "failed":{"task_id":failed,"assignment_id":fail_aid,"action_key":fail_key},
                        "unsafe":{"task_id":unsafe,"assignment_id":unsafe_aid,"restoration_rejected":True},
                        "legacy":observed,"trial_sha256":settled_trial,"legacy_snapshot_sha256":source_sha,
                        "remigrated_failure_request_ids":True,
                        "snoozes":schedules,"initial_reminder":initial,
                        "premature_and_unacknowledged_reminders_refused":True,
                        "boundary":"Real isolated old and new HTTP Routers, private SQLite copies, scripted host and clock."}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")

    def test_resumed_orders_archive_the_original_request_and_all_dispatch_versions(self):
        old_id = self.old("""
store = RouterStore(sys.argv[1])
store.upsert_worker("oguri")
print(store.orders.create("Migrated resume","Original migrated instructions","oguri",{},"fixture",None,None))
store.close()
""",self.legacy)
        source_sha = digest(self.legacy)
        backup(self.legacy,self.attachments,self.snapshot)
        original = prepare(self.snapshot,self.trial,self.source_id)
        clock = Clock()
        loop = Loop(self.trial,clock,FixtureHost)
        manager,worker = loop.hermes,loop.worker
        review = self.root/"resume-review.json"
        approvals,records = {},{}

        def accept(tid):
            for _ in range(5):
                worker.poll()
                if worker.hold()["held_task_id"]==tid:
                    aid = worker.hold()["held_assignment_id"]
                    return next((p,r) for p,r in loop.native.entries()
                                if p.get("assignment_id")==aid and p["kind"]=="task")
                clock.advance(10)
            self.fail("worker did not take the resumed order")

        def resume(tid, solution):
            before = manager.inspect(tid)["task"]
            key = guid()
            reply = manager.act(key,tid,before["revision"],"resume",resolution=solution)
            self.assertEqual(manager.act(key,tid,before["revision"],"resume",resolution=solution),reply)
            self.assertEqual(reply["delivery_state"],"pending")
            self.assertNotIn("remind_at",reply["task"])
            return key,reply

        def finish(tid, delivery, native):
            aid = delivery["assignment_id"]
            self.assertEqual(worker.report(native["native_id"],tid,aid,"Resolved")["task_state"],"completed_reported")
            current = manager.inspect(tid)["task"]
            self.assertEqual(manager.approve(guid(),tid,current["revision"])["task"]["state"],"closed")
            approvals[tid] = aid
            manager.poll()
            worker.poll()

        def stage(trial,destination,approved):
            review.write_text(json.dumps({"trial_sha256":digest(trial/"router.db"),"closed_assignments":approved}))
            return stage_reviewed_rollback(self.snapshot,trial,review,destination)

        with loop.server:
            loop.thread.start()
            try:
                worker.connect()
                migrated = original["queued_id_map"][old_id]
                born = manager.create(guid(),"Trial-born resume","Original trial instructions","oguri")["task_id"]
                for label,tid in (("deferred",migrated),("immediate",born)):
                    first,native = accept(tid)
                    aid = first["assignment_id"]
                    self.assertEqual(worker.report(native["native_id"],tid,aid,"Needs a decision","blocked")["task_state"],
                                     "blocked")
                    self.assertIsNone(worker.hold()["held_assignment_id"])
                    current = manager.inspect(tid)["task"]
                    manager.act(guid(),tid,current["revision"],"snooze",remind_after_hours=1)
                    if label=="immediate":
                        clock.advance(1)
                        worker.poll()
                        clock.advance(10)
                        worker.poll()
                    solution = "Use the approved input "+label
                    key,reply = resume(tid,solution)
                    second,new_native = accept(tid)
                    self.assertNotEqual(second["assignment_id"],aid)
                    self.assertEqual(reply["task"]["state"],"ready" if label=="immediate" else "resuming")
                    expected = first["instructions"]+"\n\n继续推进："+solution
                    self.assertEqual((reply["task"]["instructions"],second["instructions"]),(expected,expected))
                    self.assertEqual([p["assignment_id"] for p,_ in loop.native.entries()
                                      if p.get("task_id")==tid and p["kind"]=="task"],[aid,second["assignment_id"]])
                    finish(tid,second,new_native)
                    records[label] = {"task_id":tid,"assignments":[aid,second["assignment_id"]],
                                      "resume":key,"state_at_resume":reply["task"]["state"],
                                      "original":first["instructions"],"resumed":expected}
                settled = stage(self.trial,self.restore,approvals)
                public_facts = json.dumps(reconcile(self.snapshot,self.trial))
                for record in records.values():
                    self.assertNotIn(record["original"],public_facts)

                bad = manager.create(guid(),"Offline resume","Original unsafe instructions","oguri")["task_id"]
                former,_ = accept(bad)
                clock.advance(630)
                self.assertEqual(manager.inspect(bad)["task"]["state"],"blocked")
                resume(bad,"Proceed after disconnection")
                worker.connect()
                second,native = accept(bad)
                finish(bad,second,native)
                rejected = self.root/"unverified-offline-resume"
                with self.assertRaisesRegex(ValueError,"original worker released"):
                    stage(self.trial,rejected,approvals)
                self.assertFalse(rejected.exists())
            finally:
                loop.server.shutdown()
                loop.thread.join()

        private = self.restore/"private-review"
        manifest = json.loads((private/"manifest.json").read_text())
        self.assertEqual(set(manifest["orders"]),{migrated,born})
        self.assertEqual(digest(private/"trial-router.db"),settled["trial_sha256"])
        self.assertEqual(digest(self.legacy),source_sha)
        self.assertEqual(manifest["orders"][migrated]["legacy_task_id"],old_id)
        for record in records.values():
            history = manifest["orders"][record["task_id"]]["assignment_history"]
            self.assertEqual([step["assignment_id"] for step in history],record["assignments"])
            self.assertEqual([step["content"]["instructions"] for step in history],[record["original"],record["resumed"]])
            self.assertEqual(history[0]["resume"]["action_key"],record["resume"])
        born_entry = manifest["orders"][born]
        request = born_entry["legacy_creation_request"]
        self.assertEqual(request["goal"],records["immediate"]["original"])
        observed = json.loads(self.old("""
from server import RouterServer
from selftest_router import http
from threading import Thread
from unittest.mock import Mock
store = RouterStore(sys.argv[1])
token = "isolated-legacy-resume"
with RouterServer(("127.0.0.1",0),store,token,Mock(),Mock()) as server:
    runner = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
    runner.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        request = json.loads(sys.argv[4])
        replay,ret = http("POST",url+"/tasks",request,token)
        edited,_ = http("POST",url+"/tasks",dict(request,goal=sys.argv[5]),token)
        assert ret["duplicate"] and ret["task_id"]==sys.argv[3],ret
        archives = [http("GET",url+"/tasks/"+tid,token=token) for tid in sys.argv[2:4]]
        assert all(code==200 and item["task"]["deliverable"] is None for code,item in archives)
        print(json.dumps({"replay":replay,"changed_request":edited,"next":store.orders.next_dispatch("oguri"),
                          "reasons":[item["task"]["archive"]["reason"] for _,item in archives]}))
    finally:
        server.shutdown()
        runner.join()
store.close()
""",self.restore/"legacy.db",old_id,born_entry["legacy_task_id"],json.dumps(request),records["immediate"]["resumed"]))
        self.assertEqual(observed,{"replay":200,"changed_request":409,"next":None,
                                   "reasons":["reviewed_on_rewrite","reviewed_on_rewrite"]})

        changed = self.root/"changed-resumed-delivery"
        changed.mkdir()
        shutil.copyfile(self.trial/"baseline.json",changed/"baseline.json")
        shutil.copyfile(private/"trial-router.db",changed/"router.db")
        with closing(sqlite3.connect(changed/"router.db")) as db,db:
            frame = born_entry["assignment_history"][-1]["delivery_id"]
            updated = db.execute("UPDATE deliveries SET payload=json_set(payload,'$.instructions','mismatched') WHERE delivery_id=?",
                                 (frame,))
            self.assertEqual(updated.rowcount,1)
        refused = self.root/"refused-resumed-delivery"
        with self.assertRaisesRegex(ValueError,"dispatch does not match"):
            stage(changed,refused,manifest["closed_assignments"])
        self.assertFalse(refused.exists())
        if output := os.environ.get("MOWULINK_RESUMED_ROLLBACK_EVIDENCE"):
            evidence = {"orders":records,"legacy":observed,"trial_sha256":settled["trial_sha256"],
                        "legacy_snapshot_sha256":source_sha,"offline_resume_refused":former["assignment_id"],
                        "mismatched_dispatch_refused":True,
                        "boundary":"Actual isolated legacy and rewrite Router HTTP; scripted Worker host and clock."}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")

    def test_trial_born_closed_orders_return_as_private_archives_without_a_new_dispatch(self):
        self.old("""
store = RouterStore(sys.argv[1])
store.upsert_worker("oguri")
store.upsert_worker("two")
store.close()
""",self.legacy)
        original = digest(self.legacy)
        backup(self.legacy,self.attachments,self.snapshot)
        prepare(self.snapshot,self.trial,self.source_id)
        clock = Clock()
        loop = Loop(self.trial,clock,FixtureHost)
        direct_key = guid()
        blob = b"an isolated trial-born result\0"
        checksum = hashlib.sha256(blob).hexdigest()
        evidence = {}
        with loop.server:
            loop.thread.start()
            try:
                before_online = loop.hermes.create(direct_key,"新单私有标题","正文不得进入旧的共享库。","oguri")
                self.assertEqual(before_online["revision"],1)
                loop.worker.connect()
                loop.worker.poll()
                clock.advance(10)
                loop.worker.poll()
                loop.worker.poll()

                def finish(order, artifacts):
                    tid = order["task_id"]
                    hold = loop.worker.hold()
                    self.assertEqual(hold["held_task_id"],tid)
                    aid = hold["held_assignment_id"]
                    native = loop.native.entries()[-1][1]["native_id"]
                    report = agent_report(loop.worker,native,{"task_id":tid,"assignment_id":aid,
                        "summary":"新端私有成果","artifact_ids":artifacts})
                    self.assertFalse(report["isError"],report)
                    received = loop.hermes.inspect(tid)["task"]
                    self.assertEqual(received["state"],"completed_reported")
                    close = loop.hermes.approve(guid(),tid,received["revision"])
                    self.assertEqual(close["task"]["state"],"closed")
                    loop.hermes.poll()
                    loop.worker.poll()
                    evidence[tid] = aid

                upload = loop.wire.request("POST","uploads",{"name":"trial-private.bin","size_bytes":len(blob),
                                           "sha256":checksum},guid())["upload_id"]
                file_id = loop.wire.request("PATCH","uploads/"+upload,blob,headers={"Upload-Offset":"0"})["attachment_id"]
                finish(before_online,[file_id])
                clock.advance(1)
                loop.worker.poll()
                clock.advance(10)
                loop.worker.poll()
                tools = ManagerTools(loop.hermes,"isolated-trial-manager")
                while_online = json.loads(tools.around_call("router_create_task",
                    lambda args:tools.invoke("router_create_task",args),
                    {"title":"第二条私有标题","instructions":"第二条私有任务。\n保留全角符号。","assignee_id":"oguri"},
                    session_id="isolated-trial-manager",turn_id="isolated-create-turn",tool_call_id="create"))
                self.assertEqual(while_online["revision"],2)
                loop.worker.poll()
                finish(while_online,[])
                queued_params = {"title":"复原后可调度","instructions":"等旧队首处理后再派","assignee_id":"two"}
                queued = json.loads(tools.around_call("router_create_task",
                    lambda args:tools.invoke("router_create_task",args),queued_params,
                    session_id="isolated-trial-manager",turn_id="isolated-create-turn",tool_call_id="create-queued"))
                self.assertIsNone(queued["assignment_id"])
            finally:
                loop.server.shutdown()
                loop.thread.join()
        trial_digest = digest(self.trial/"router.db")
        facts = reconcile(self.snapshot,self.trial)
        self.assertEqual(facts["blocked_new_tasks"],[])
        self.assertEqual({row["task_id"] for row in facts["closed_new_tasks"]},evidence.keys())
        self.assertEqual([row["task_id"] for row in facts["queue_candidates"]],[queued["task_id"]])
        review = self.root/"return-created-review.json"
        decision = {"trial_sha256":trial_digest,"closed_assignments":evidence,
                    "execution_modes":{queued["task_id"]:"goal"}}
        review.write_text(json.dumps({**decision,"closed_assignments":{before_online["task_id"]:evidence[before_online["task_id"]]}}))
        with self.assertRaisesRegex(ValueError,"all closed assignments"):
            stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
        self.assertFalse(self.restore.exists())
        review.write_text(json.dumps(decision))
        exported = stage_reviewed_rollback(self.snapshot,self.trial,review,self.restore)
        self.assertEqual((exported["reviewed_orders"],exported["private_artifacts"],exported["queued_requests"]),(2,1,1))
        private = self.restore/"private-review"
        manifest = json.loads((private/"manifest.json").read_text())
        archived = manifest["orders"]
        self.assertEqual((private/"files"/file_id).read_bytes(),blob)
        self.assertEqual(digest(private/"trial-router.db"),trial_digest)
        self.assertEqual(archived[before_online["task_id"]]["legacy_creation_request"]["message_id"],direct_key)
        second = archived[while_online["task_id"]]
        self.assertRegex(second["creation_key"],r"^hermes-[0-9a-f]{64}$")
        self.assertNotEqual(second["legacy_creation_request"]["message_id"],second["creation_key"])
        queued_plan = json.loads((self.restore/"queue-replay.json").read_text())["requests"][0]
        self.assertRegex(queued_plan["trial_request_id"],r"^hermes-[0-9a-f]{64}$")
        missing_replay, unpublished = self.root/"before-replay-snapshot", self.root/"before-replay-trial"
        backup(self.restore/"legacy.db",self.restore/"legacy.db.attachments",missing_replay)
        with self.assertRaisesRegex(ValueError,"queue replay receipt is missing"):
            prepare(missing_replay,unpublished,guid())
        self.assertFalse(unpublished.exists())

        old_observation = """
from server import RouterServer
from selftest_router import http
from threading import Thread
from unittest.mock import Mock
operator = "isolated-legacy-operator"
root = Path(sys.argv[1])
orders = json.loads((root/"private-review/manifest.json").read_text())["orders"]
queue = json.loads((root/"queue-replay.json").read_text())["requests"][0]
store = RouterStore(root/"legacy.db")
result = {"archives":{}, "head":store.orders.next_dispatch("two")}
with RouterServer(("127.0.0.1",0),store,operator,Mock(),Mock()) as srv:
    thread = Thread(target=srv.serve_forever,kwargs={"poll_interval":.02})
    thread.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        for trial, row in orders.items():
            old, body = row["legacy_task_id"],row["legacy_creation_request"]
            assert old.startswith("t-") and len(old)==12
            status, item = http("GET",url+"/tasks/"+old,token=operator)
            replay, duplicate = http("POST",url+"/tasks",body,operator)
            clash, error = http("POST",url+"/tasks",dict(body,goal="other body"),operator)
            assert (status,replay,clash)==(200,200,409)
            assert error["error"]=="message_id_conflict"
            assert duplicate["duplicate"] and duplicate["task_id"]==old
            assert item["task"]["archive"]["recovery_reference"]==trial
            assert item["task"]["task_info"]["TaskName"]=="重做版已验收工单"
            assert item["task"]["goal"]=="完整记录见私有回退档案。"
            assert not item["task"]["in_running_tasks"] and item["task"]["deliverable"] is None
            result["archives"][trial]={"legacy_task_id":old,"replay":replay,"conflict":clash,
                                       "reason":item["task"]["archive"]["reason"]}
        result["artifact_status"],_ = http("GET",url+"/attachments/"+sys.argv[2],token=operator)
        result["queued_status"], revived = http("POST",url+"/tasks",queue["request"],operator)
        assert not revived.get("dispatched")
        result["held"] = store.orders.held("oguri")
        result["new_queue_for_oguri"] = store.orders.next_dispatch("oguri")
        result["queue_order"] = [row[0] for row in store._conn.execute(
            "SELECT task_id FROM dispatch_queue WHERE worker='two' ORDER BY seq")]
        result["replayed_queue_id"] = revived["task_id"]
    finally:
        srv.shutdown()
        thread.join()
store.close()
print(json.dumps(result))
"""
        before_restart = json.loads(self.old(old_observation,self.restore,checksum))
        after_restart = json.loads(self.old(old_observation,self.restore,checksum))
        self.assertEqual(before_restart["archives"],after_restart["archives"])
        self.assertEqual((before_restart["queued_status"],after_restart["queued_status"]),(202,200))
        self.assertEqual(before_restart["head"],self.queued)
        self.assertEqual(after_restart["queue_order"],[self.queued,before_restart["replayed_queue_id"]])
        self.assertEqual((after_restart["held"],after_restart["new_queue_for_oguri"]),(None,None))
        self.assertEqual(after_restart["artifact_status"],404)
        self.assertTrue(all(row["reason"]=="reviewed_on_rewrite" for row in after_restart["archives"].values()))
        self.assertEqual((digest(self.legacy),digest(self.trial/"router.db")),(original,trial_digest))

        # A later cutover must also remember the manager's original opaque request key.
        again_snapshot, again = self.root/"reentry-snapshot", self.root/"reentry-trial"
        backup(self.restore/"legacy.db",self.restore/"legacy.db.attachments",again_snapshot)
        source_id = guid()
        remigration = prepare(again_snapshot,again,source_id)
        token = guid()
        with serve_loopback(Orders(again/"router.db",clock),{token:"hermes"}) as srv:
            thread = Thread(target=srv.serve_forever,kwargs={"poll_interval":.02})
            thread.start()
            try:
                client = Client(f"http://127.0.0.1:{srv.server_port}",token)
                for trial_id, entry in archived.items():
                    request = entry["legacy_creation_request"]
                    keys = {entry["creation_key"],request["message_id"]}
                    for key in keys:
                        with self.assertRaises(Fault) as duplicate:
                            client.request("POST","tasks",{"title":request["TaskName"],"instructions":request["goal"],
                                                           "assignee_id":request["worker"]},key)
                        self.assertEqual((duplicate.exception.status,duplicate.exception.code),(409,"legacy_request_recorded"))
                        self.assertEqual(duplicate.exception.details,
                            {"source_id":source_id,"legacy_task_id":after_restart["archives"][trial_id]["legacy_task_id"]})
                alias = queued_plan["trial_request_id"]
                migrated = client.request("POST","tasks",queued_params,queued_plan["request"]["message_id"])
                self.assertEqual(migrated["task_id"],remigration["queued_id_map"][after_restart["replayed_queue_id"]])
                self.assertEqual(client.request("POST","tasks",queued_params,alias),migrated)
                with self.assertRaises(Fault) as changed:
                    client.request("POST","tasks",{**queued_params,"instructions":"改过的试用工单"},alias)
                self.assertEqual(changed.exception.code,"idempotency_conflict")
                self.assertEqual(len(client.request("GET","tasks")["items"]),2)
            finally:
                srv.shutdown()
                thread.join()
        inconsistent_old = self.root/"inconsistent-old.db"
        shutil.copyfile(self.restore/"legacy.db",inconsistent_old)
        with closing(sqlite3.connect(inconsistent_old)) as db, db:
            updated = db.execute("UPDATE rewrite_archive_request_keys SET trial_source_id=?",(guid(),))
            self.assertEqual(updated.rowcount,1)
        inconsistent_snapshot = self.root/"inconsistent-snapshot"
        backup(inconsistent_old,self.restore/"legacy.db.attachments",inconsistent_snapshot)
        refused = self.root/"cannot-reenter"
        with self.assertRaisesRegex(ValueError,"does not match its legacy archive"):
            prepare(inconsistent_snapshot,refused,guid())
        self.assertFalse(refused.exists())

        # The old endpoint has one UUID namespace even when two Rewrite keys differ.
        collision_trial = self.root/"collision-trial"
        shutil.copytree(self.trial,collision_trial)
        collision_key = second["legacy_creation_request"]["message_id"]
        token = guid()
        with serve_loopback(Orders(collision_trial/"router.db",clock),{token:"hermes"}) as srv:
            thread = Thread(target=srv.serve_forever,kwargs={"poll_interval":.02})
            thread.start()
            try:
                client = Client(f"http://127.0.0.1:{srv.server_port}",token)
                collision = client.request("POST","tasks",
                    {"title":"Colliding trial request","instructions":"Keep both source records","assignee_id":"two"},
                    collision_key)
            finally:
                srv.shutdown()
                thread.join()
        collision_digest = digest(collision_trial/"router.db")
        collision_target = self.root/"collision-cannot-publish"
        review.write_text(json.dumps({**decision,"trial_sha256":collision_digest,
            "execution_modes":{**decision["execution_modes"],collision["task_id"]:"goal"}}))
        with self.assertRaisesRegex(ValueError,"creation request already exists"):
            stage_reviewed_rollback(self.snapshot,collision_trial,review,collision_target)
        self.assertFalse(collision_target.exists())
        self.assertEqual(digest(collision_trial/"router.db"),collision_digest)
        if output := os.environ.get("MOWULINK_TRIAL_BORN_ROLLBACK_EVIDENCE"):
            Path(output).write_text(json.dumps({"trial_sha256":trial_digest,"legacy_snapshot_sha256":original,
                "closed_assignments":evidence,"public_legacy_readback":after_restart,
                "initial_queue_status":before_restart["queued_status"],"private_artifact_sha256":checksum,
                "incomplete_review_rejected":True,"mapped_legacy_request_collision_rejected":True,
                "later_cutover_rejects_original_and_mapped_request_keys":True,
                "queued_request_retry_keeps_the_same_task_after_a_second_cutover":True,
                "unreplayed_queue_prevents_a_second_cutover":True,
                "mismatched_archived_request_mapping_rejected":True,
                "both_inputs_unchanged":True,
                "boundary":"Actual old and new Router over local HTTP, SQLite and attachment bytes. Scripted Worker Agent."},
                ensure_ascii=False,indent=2)+"\n")

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
        with self.assertRaisesRegex(ValueError,"trial order still has an assignment"):
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
        self.assertEqual(plan["restored_legacy_sha256"],digest(self.restore/"legacy.db"))
        self.assertEqual(plan["restored_legacy_sha256"],staged["restored_legacy_sha256"])
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
