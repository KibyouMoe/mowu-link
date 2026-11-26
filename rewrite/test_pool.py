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
