import unittest

from .devloop import Loop
from .storage import guid
from .test_mainline import Clock


class Inquiries(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock))
        self.w, self.m = self.loop.worker, self.loop.hermes
        self.tid = self.m.create(guid(), "Idle", "Report explicitly", "oguri")["task_id"]
        self.w.connect()
        self.w.poll()
        self.advance(10)
        self.aid = self.w.hold()["held_assignment_id"]

    def advance(self, seconds):
        self.clock.advance(seconds)
        self.w.poll()
        self.w.poll()

    def count(self, kind):
        return sum(frame["kind"] == kind for frame in self.loop.wire.frames(0))

    def test_busy_breaks_idle_window_and_continue_retains_work(self):
        self.advance(1)
        self.advance(9)
        self.assertEqual(self.count("task.probe"), 0)
        self.loop.native.is_busy = True
        self.advance(1)
        self.loop.native.is_busy = False
        self.advance(1)
        self.advance(9)
        self.assertEqual(self.count("task.probe"), 0)
        self.advance(1)
        self.assertEqual(self.count("task.probe"), 1)
        p, receipt = self.loop.native.entries()[-1]
        self.assertEqual(p["kind"], "task_probe")
        result = self.w.report(receipt["native_id"], self.tid, self.aid, "more time", "continue_requested")
        self.assertNotIn("release_assignment_id", result)
        self.assertEqual(self.w.hold()["held_assignment_id"], self.aid)
        self.advance(1)
        self.assertEqual(self.count("task.probe"), 1)

    def test_three_retries_shelve_without_claiming_cancellation(self):
        before = self.m.inspect(self.tid)["task"]
        self.m.act(guid(), self.tid, before["revision"], "cancel", reason="cancel")
        self.loop.native.is_busy = True
        self.advance(1)
        self.advance(15)
        self.assertEqual(self.count("task.cancel"), 1)
        self.loop.native.is_busy = False
        self.advance(1)
        self.advance(10)
        self.advance(1)
        self.advance(1)
        self.assertEqual(self.count("task.cancel"), 4)
        self.assertEqual(self.m.inspect(self.tid)["task"]["state"], "working")
        self.advance(1)
        self.assertEqual(self.count("task.probe"), 0)
        self.assertEqual(self.count("task.shelved"), 1)
        self.assertIsNone(self.w.hold()["held_assignment_id"])
        self.assertEqual(self.m.inspect(self.tid)["task"]["state"], "blocked")
        self.assertIsNone(self.m.inspect(self.tid)["last_report"])

    def test_unanswered_inquiry_never_becomes_cancel(self):
        self.advance(1)
        self.advance(10)
        for _ in range(4):
            self.advance(1)
        self.assertEqual(self.count("task.probe"), 4)
        self.assertEqual(self.count("task.cancel"), 0)
        self.assertEqual(self.count("task.shelved"), 1)
        self.assertEqual(len(self.loop.manager_wire.frames(0)), 1)
