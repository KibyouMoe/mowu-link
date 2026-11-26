from concurrent.futures import ThreadPoolExecutor
import unittest

from .devloop import Loop, agent_report
from .storage import Fault, guid
from .test_mainline import Clock


class Pool(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock))
        self.w, self.m = self.loop.worker, self.loop.hermes
        self.tid = self.m.create(guid(), "Pool test", "Try once", "oguri")["task_id"]
        self.w.connect()
        self.w.poll()
        self.clock.advance(10)
        self.w.poll()
        self.w.poll()
        self.aid = self.w.hold()["held_assignment_id"]
        self.native = self.loop.native.entries()[0][1]["native_id"]

    def details(self):
        return self.m.inspect(self.tid)

    def report(self, outcome):
        args = {"task_id": self.tid, "assignment_id": self.aid, "outcome": outcome, "summary": outcome}
        reply = agent_report(self.w, self.native, args)
        self.assertFalse(reply["isError"])

    def fall_offline(self):
        self.clock.advance(629)
        self.assertEqual(self.details()["task"]["state"], "working")
        self.clock.advance(1)
        self.assertEqual(self.details()["task"]["state"], "blocked")
        self.assertIsNone(self.details()["last_report"])

    def material(self, key, text="Found result"):
        return self.loop.wire.request("POST", f"tasks/{self.tid}/materials", {
            "session_id": self.w.session, "assignment_id": self.aid, "text": text}, key)

    def test_continue_then_block_then_snooze_then_close(self):
        self.report("continue_requested")
        self.assertEqual(self.w.hold()["held_assignment_id"], self.aid)
        self.report("blocked")
        self.report("blocked")
        self.assertIsNone(self.w.hold()["held_assignment_id"])
        details = self.details()
        self.assertEqual(details["last_report"]["outcome"], "blocked")
        self.assertEqual(self.m.tasks(pool="blocked")["items"][0]["task_id"], self.tid)
        self.m.act(guid(), self.tid, details["task"]["revision"], "snooze", remind_after_hours=2)
        self.clock.advance(3600)
        self.assertEqual(len(self.loop.manager_wire.frames(0)), 1)
        self.clock.advance(3600)
        self.assertEqual(len(self.loop.manager_wire.frames(0)), 2)
        self.assertEqual(self.details()["task"]["state"], "snoozed")
        candidate = self.material(guid())
        closed = self.m.act(guid(), self.tid, self.details()["task"]["revision"], "close",
                            material_id=candidate["material_id"])
        self.assertEqual(closed["task"]["state"], "closed")
        self.assertEqual(self.details()["last_report"]["outcome"], "blocked")

    def test_reconnect_resets_offline_interval(self):
        self.clock.advance(610)
        self.assertEqual(self.w.connect()["reconcile"], "continue")
        self.fall_offline()
        self.assertEqual(self.w.connect()["release_assignment_ids"], [self.aid])
        self.assertIsNone(self.w.hold()["held_assignment_id"])

    def cancel_pooled(self):
        before = self.details()["task"]
        result = self.m.act(guid(),self.tid,before["revision"],"cancel",reason="Stop the original work")
        self.assertEqual((result["delivery_state"],result["task"]["state"]),("pending","blocked"))
        pending, = self.details()["pending_cleanup"]
        self.assertEqual((pending["assignment_id"],pending["kind"]),(self.aid,"task.cancel"))

    def test_pooled_cancel_waits_for_the_original_w04(self):
        self.fall_offline()
        self.cancel_pooled()
        registration = self.w.connect()
        self.assertEqual(registration["reconcile"],"confirm_cancel")
        self.assertEqual(registration["pending_cleanup"],self.details()["pending_cleanup"])
        self.w.poll()
        controls = [body for body,_ in self.loop.native.entries() if body["kind"]=="task.cancel"]
        self.assertEqual([body["assignment_id"] for body in controls],[self.aid])
        self.assertEqual(self.details()["task"]["state"],"blocked")
        key = guid()
        body = {"session_id":self.w.session,"assignment_id":self.aid,"outcome":"cancel_confirmed"}
        wrong = dict(body,assignment_id=guid())
        route = f"tasks/{self.tid}/report"
        with self.assertRaisesRegex(Fault,"stale_assignment"):
            self.loop.wire.request("POST",route,wrong,guid())
        receipt = self.loop.wire.request("POST",route,body,key)
        self.assertEqual(receipt,{"recorded":True,"release_assignment_id":self.aid,"task_state":"cancelled"})
        self.assertEqual(self.loop.wire.request("POST",route,body,key),receipt)
        self.assertEqual(self.details()["task"]["state"],"cancelled")
        self.assertEqual(self.details()["pending_cleanup"],[])
        self.assertEqual(self.m.tasks(pool="blocked")["items"],[])
        self.assertEqual(len(self.m.messages(source="oguri")["items"]),1)

    def test_late_pooled_cancel_receipt_cannot_cancel_a_new_assignment(self):
        self.fall_offline()
        self.cancel_pooled()
        self.w.connect()
        self.m.act(guid(),self.tid,self.details()["task"]["revision"],"resume",resolution="Try again")
        self.w.poll()
        self.clock.advance(10)
        self.w.poll()
        self.w.poll()
        current = self.details()["task"]
        self.assertEqual(current["state"],"working")
        self.assertNotEqual(current["assignment_id"],self.aid)
        receipt = self.loop.wire.request("POST",f"tasks/{self.tid}/report",
            {"session_id":self.w.session,"assignment_id":self.aid,"outcome":"cancel_confirmed"},guid())
        self.assertEqual(receipt,{"recorded":True,"release_assignment_id":self.aid,"task_state":"working"})
        self.assertEqual(self.details()["task"],current)
        self.assertEqual(self.m.workers()["items"][0]["held_assignment_id"],current["assignment_id"])
        self.assertEqual(self.details()["pending_cleanup"],[])
        notice, = self.m.messages(source="oguri")["items"]
        self.assertEqual(notice["text"],"原处理人已确认停止并清理旧派发。")

    def test_stale_completed_is_not_material(self):
        self.fall_offline()
        with self.assertRaises(Fault) as failure:
            self.w.report(self.native, self.tid, self.aid, "late output")
        self.assertEqual((failure.exception.status, failure.exception.code), (409, "stale_assignment"))
        self.assertFalse(self.details()["materials"])
        self.w.connect()
        key = guid()
        first = self.material(key)
        self.assertEqual(first, self.material(key))
        self.assertNotIn("release_assignment_id", first)
        self.assertEqual(len(self.details()["materials"]), 1)
        with self.assertRaises(Fault) as failure:
            self.material(key, "different")
        self.assertEqual(failure.exception.code, "idempotency_conflict")
        self.assertEqual(self.details()["task"]["holder"], {"kind": "router"})
        self.assertIsNone(self.details()["last_report"])

    def test_new_dispatch_permanently_excludes_new_old_material(self):
        self.fall_offline()
        key = guid()
        first = self.material(key)
        self.w.connect()
        self.m.act(guid(), self.tid, self.details()["task"]["revision"], "resume", resolution="Try again")
        self.w.poll()
        self.clock.advance(10)
        self.w.poll()
        self.w.poll()
        new_aid = self.w.hold()["held_assignment_id"]
        self.assertTrue(new_aid and new_aid != self.aid)
        self.assertEqual(first, self.material(key))
        with self.assertRaises(Fault):
            self.material(guid())
        self.fall_offline()
        with self.assertRaises(Fault):
            self.material(guid())
        self.assertEqual(len(self.details()["materials"]), 1)

    def test_concurrent_material_and_resume(self):
        self.fall_offline()
        rev = self.details()["task"]["revision"]
        def submit():
            try:
                return self.material(guid())
            except Fault as exc:
                self.assertEqual(exc.code, "ineligible_material")
        with ThreadPoolExecutor(2) as pool:
            material = pool.submit(submit)
            resume = pool.submit(self.m.act, guid(), self.tid, rev, "resume", resolution="fixed")
        self.assertEqual(resume.result()["task"]["state"], "resuming")
        self.assertEqual(len(self.details()["materials"]), int(material.result() is not None))
        self.assertIsNone(self.details()["last_report"])
