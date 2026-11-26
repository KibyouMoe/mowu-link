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
