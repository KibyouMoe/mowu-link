import json
import os
from pathlib import Path
import unittest

from .adapter import HostAdapter
from .devloop import FixtureHost, Loop
from .storage import Fault, guid
from .test_mainline import Clock
from .worker import Worker


class Cleanup(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock, extra_workers=["tama"]))
        self.old, self.manager = self.loop.worker, self.loop.hermes
        root = self.loop.root
        self.host2 = FixtureHost(root / "tama-host.db", "test-tama")
        self.new = Worker(root / "tama.db", "tama", self.loop.extra_clients["tama"],
                          HostAdapter(root / "tama-adapter.db", self.host2, self.host2.session))
        self.tid = self.manager.create(guid(), "Cleanup", "Work", "oguri")["task_id"]
        for w in (self.old, self.new):
            w.connect()
            w.poll()
        self.clock.advance(10)
        for w in (self.old, self.new):
            w.poll()
            w.poll()
        self.aid = self.old.hold()["held_assignment_id"]
        self.native = self.loop.native.entries()[0][1]["native_id"]

    def act(self, action, **params):
        return self.manager.act(guid(), self.tid, self.manager.inspect(self.tid)["task"]["revision"], action, **params)

    def finish_cleanup(self, kind, outcome):
        self.old.poll()
        p, receipt = next((p, r) for p, r in self.loop.native.entries() if p["kind"] == kind)
        def report():
            return self.old.report(receipt["native_id"], self.tid, self.aid, "cleaned", outcome)
        with self.assertRaises(Fault) as missing:
            report()
        self.assertEqual(missing.exception.code, "native_cleanup_unconfirmed")
        self.loop.native.cleanup_evidence = lambda session, aid, k: {
            "session": session, "assignment_id": aid, "kind": k, "stopped": True, "cleaned": True,
            "receipt_id": "fixture-scope-cleanup-" + self.aid}
        reply = report()
        self.assertEqual(reply, report())
        self.assertEqual(reply["release_assignment_id"], p["assignment_id"])
        return reply

    def test_active_transfer_late_report_then_scoped_cleanup(self):
        self.act("transfer", worker_id="tama", reason="Continue on the second worker")
        self.new.poll()
        current = self.manager.inspect(self.tid)
        aid2 = self.new.hold()["held_assignment_id"]
        self.assertTrue(aid2 and self.aid != aid2)
        self.assertEqual(current["task"]["holder"], {"kind": "worker", "worker_id": "tama"})
        self.assertEqual(current["pending_cleanup"][0]["assignment_id"], self.aid)
        self.assertEqual(self.old.connect()["reconcile"], "confirm_transfer")
        with self.assertRaises(Fault) as stale:
            self.old.report(self.native, self.tid, self.aid, "late")
        self.assertEqual(stale.exception.code, "stale_assignment")
        self.assertEqual(self.finish_cleanup("task.transfer_out", "transfer_confirmed")["task_state"], "working")
        self.assertEqual(self.new.hold()["held_assignment_id"], aid2)
        detail = self.manager.inspect(self.tid)
        self.assertEqual(detail["task"], current["task"])
        self.assertEqual(detail["pending_cleanup"], [])
        self.assertIsNone(detail["last_report"])

    def test_cancel_waits_for_cleanup_and_never_becomes_result(self):
        receipt = self.act("cancel", reason="stop work")
        self.assertEqual(receipt["delivery_state"], "pending")
        self.assertEqual(self.old.connect()["reconcile"], "confirm_cancel")
        self.assertEqual(self.manager.inspect(self.tid)["task"]["state"], "working")
        self.assertEqual(self.finish_cleanup("task.cancel", "cancel_confirmed")["task_state"], "cancelled")
        self.assertIsNone(self.old.hold()["held_task_id"])
        self.assertIsNone(self.manager.inspect(self.tid)["last_report"])
        with self.old.db.transaction() as db:
            events = db.execute("SELECT event_id FROM display_events WHERE event_id LIKE 'cleanup-%' ORDER BY seq").fetchall()
        self.assertEqual([row[0] for row in events],[
            f"cleanup-task.cancel-{self.aid}-pending",f"cleanup-task.cancel-{self.aid}-recorded"])

    def test_reissued_cancel_rejects_old_receipt_and_recovers_lost_new_w04(self):
        original = self.act("cancel",reason="Same reason across both requests")
        for seconds in (1,10,1,1,1):
            self.clock.advance(seconds)
            self.old.poll()
            self.old.poll()
        current = self.manager.inspect(self.tid)
        self.assertEqual(current["task"]["state"],"blocked")
        self.assertEqual(current["pending_cleanup"],[])
        controls = [r for p,r in self.loop.native.entries() if p["kind"]=="task.cancel"]
        self.assertEqual(len(controls),4)
        late = controls[-1]
        forwarded = []
        request = self.old.router.request

        def trace(method,path,body=None,key=None,**options):
            if method=="POST" and path==f"tasks/{self.tid}/report":
                forwarded.append((body,key))
            return request(method,path,body,key,**options)

        def report(worker, receipt):
            return worker.report(receipt["native_id"],self.tid,self.aid,"Stopped","cancel_confirmed")

        def proof(session,aid,kind):
            return {"session":session,"assignment_id":aid,"kind":kind,"stopped":True,"cleaned":True,"receipt_id":guid()}

        self.old.router.request = trace
        self.loop.native.cleanup_evidence = proof
        with self.assertRaises(Fault) as stale:
            report(self.old,late)
        self.assertEqual((stale.exception.status,stale.exception.code),(409,"stale_assignment"))
        (old_body,old_key), = forwarded
        self.assertNotIn("cleanup_id",old_body)
        second = self.act("cancel",reason="Same reason across both requests")
        self.assertEqual((original["delivery_state"],second["delivery_state"]),("pending","pending"))
        cleanup_id = self.manager.inspect(self.tid)["pending_cleanup"][0]["cleanup_id"]
        path = f"tasks/{self.tid}/report"
        for once,key in [(old_body,old_key),(old_body,guid()),(dict(old_body,cleanup_id=guid()),guid())]:
            with self.assertRaises(Fault) as rejected:
                request("POST",path,once,key)
            self.assertEqual((rejected.exception.status,rejected.exception.code),(409,"stale_assignment"))
        replay = self.old.connect()
        self.assertEqual(replay["pending_cleanup"][0]["cleanup_id"],cleanup_id)
        self.old.poll()
        payload, fresh = self.loop.native.entries()[-1]
        self.assertEqual((payload["kind"],payload["cleanup_id"]),("task.cancel",cleanup_id))
        self.assertNotEqual(fresh["native_id"],late["native_id"])
        with self.assertRaises(Fault) as denied:
            report(self.old,late)
        self.assertEqual((denied.exception.status,denied.exception.code),(409,"stale_assignment"))
        self.assertEqual(len(forwarded),1,"cached error must retain its original scope")
        self.assertEqual(self.manager.inspect(self.tid)["task"]["state"],"blocked")

        def lose_reply(method,path,body=None,key=None,**options):
            result = trace(method,path,body,key,**options)
            if method=="POST" and path.endswith("/report"):
                raise Fault(503,"isolated_w04_response_lost",True)
            return result

        self.old.router.request = lose_reply
        with self.assertRaises(Fault) as lost:
            report(self.old,fresh)
        self.assertEqual(lost.exception.code,"isolated_w04_response_lost")
        self.assertEqual(self.manager.inspect(self.tid)["task"]["state"],"cancelled")
        self.old.router.request = trace
        restarted = Worker(self.loop.root/"worker.db","oguri",self.loop.wire,self.loop.adapter)
        restarted.connect()
        restarted.poll()
        self.assertEqual(restarted.session,self.old.session)
        self.assertEqual(restarted.hold(),{"held_task_id":None,"held_assignment_id":None})
        fresh_body,fresh_key = forwarded[1]
        self.assertEqual(forwarded,[forwarded[0],forwarded[1],forwarded[1]])
        self.assertEqual(fresh_body["cleanup_id"],cleanup_id)
        self.assertEqual(fresh_key,"cancel_confirmed-"+cleanup_id)
        expected = {"recorded":True,"release_assignment_id":self.aid,"task_state":"cancelled"}
        self.assertEqual(report(restarted,fresh),expected)
        self.assertEqual(request("POST",path,fresh_body,fresh_key),expected)
        self.assertEqual(len(self.loop.native.entries()),7)
        state = self.manager.inspect(self.tid)
        self.assertEqual(state["pending_cleanup"],[])
        self.assertIsNone(state["last_report"])
        with self.loop.orders.db.transaction() as db:
            written, = db.execute("""SELECT key FROM operations
                WHERE json_extract(request,'$[1].outcome')='cancel_confirmed'""").fetchall()
            self.assertEqual(written["key"],fresh_key)
        notices = [frame for frame in self.loop.manager_wire.frames(0)
                   if frame["payload"].get("text")=="原处理人已确认取消并清理工单。"]
        self.assertEqual(len(notices),1)
        if output := os.environ.get("MOWULINK_REISSUED_CANCEL_EVIDENCE"):
            evidence = {"task_id":self.tid,"assignment_id":self.aid,"cleanup_id":cleanup_id,
                        "original_native_input":late["native_id"],"new_native_input":fresh["native_id"],
                        "old_w04_key":old_key,"new_w04_key":fresh_key,"old_replay_status":409,
                        "new_reply_lost_then_recovered":expected,"terminal_notice_count":len(notices),
                        "original_worker_held_after_reconnect":restarted.hold(),
                        "boundary":"Actual isolated HTTP Router, Worker and persisted Adapter. Scripted host, clock and response loss."}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")

    def test_retries_reuse_their_scope_and_later_explicit_cancel_replaces_it(self):
        self.act("cancel",reason="Same reason")
        second_key = guid()
        revision = self.manager.inspect(self.tid)["task"]["revision"]
        second = self.manager.act(second_key,self.tid,revision,"cancel",reason="Same reason")
        self.assertEqual(self.manager.act(second_key,self.tid,revision,"cancel",reason="Same reason"),second)
        first_scope = self.manager.inspect(self.tid)["pending_cleanup"][0]["cleanup_id"]
        for seconds in (1,10):
            self.clock.advance(seconds)
            self.old.poll()
            self.old.poll()
        commands = [(p,r) for p,r in self.loop.native.entries() if p["kind"]=="task.cancel"]
        self.assertGreater(len(commands),2)
        self.assertEqual([p.get("cleanup_id") for p,_ in commands],[None]+[first_scope]*(len(commands)-1))
        self.act("cancel",reason="Same reason")
        self.old.poll()
        payload, latest = self.loop.native.entries()[-1]
        self.assertNotEqual(payload["cleanup_id"],first_scope)

        def report(receipt):
            return self.old.report(receipt["native_id"],self.tid,self.aid,"Stopped","cancel_confirmed")

        host = self.loop.native

        def wait_for_control(*args):
            raise Fault(409,"native_cleanup_waiting",True)

        host.cleanup_evidence = wait_for_control
        with self.assertRaises(Fault) as waiting:
            report(latest)
        self.assertEqual(waiting.exception.code,"native_cleanup_waiting")
        host.cleanup_evidence = lambda session,aid,kind: {
            "session":session,"assignment_id":aid,"kind":kind,"stopped":True,"cleaned":True,"receipt_id":guid()}
        with self.assertRaises(Fault) as stale:
            report(commands[-1][1])
        self.assertEqual(stale.exception.code,"stale_assignment")
        self.assertEqual(self.manager.inspect(self.tid)["task"]["state"],"working")
        self.old.poll()
        self.assertEqual(report(latest)["task_state"],"cancelled")

    def test_material_before_transfer_survives_new_holder(self):
        self.clock.advance(630)
        self.assertEqual(self.manager.inspect(self.tid)["task"]["state"], "blocked")
        def post(key):
            return self.loop.wire.request("POST", f"tasks/{self.tid}/materials",
                  {"session_id": self.old.session, "assignment_id": self.aid, "text": "late material"}, key)
        key = guid()
        original = post(key)
        self.act("transfer", worker_id="tama", reason="new owner")
        self.new.connect()
        self.new.poll()
        self.clock.advance(10)
        self.new.poll()
        self.new.poll()
        aid2 = self.new.hold()["held_assignment_id"]
        self.assertIsNotNone(aid2)
        self.assertEqual(original, post(key))
        with self.assertRaises(Fault):
            post(guid())
        self.assertEqual(self.finish_cleanup("task.transfer_out", "transfer_confirmed")["task_state"], "working")
        self.assertEqual(self.manager.inspect(self.tid)["task"]["assignment_id"], aid2)
        self.assertEqual(len(self.manager.inspect(self.tid)["materials"]), 1)
