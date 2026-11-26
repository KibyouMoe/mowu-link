import unittest

from .devloop import Loop
from .storage import Fault, guid
from .test_mainline import Clock


class Management(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock))
        self.m, self.w = self.loop.hermes, self.loop.worker
        self.w.connect()
        self.w.poll()
        self.clock.advance(10)
        self.w.poll()

    def test_unassigned_list_pagination_and_authority(self):
        tasks = [self.m.create(guid(), name, "instructions") for name in ("First", "Second", "Third")]
        page = self.m.tasks(unassigned="true", limit=2)
        rest = self.m.tasks(unassigned="true", cursor=page["next_cursor"], limit=2)
        self.assertEqual(page["items"] + rest["items"], tasks)
        self.assertIsNone(rest["next_cursor"])
        self.assertFalse(self.m.tasks(pool="blocked")["items"])
        for path in ("tasks?limit=1&limit=2", "tasks?cursor=-1", "tasks?unassigned=no", "workers?unsupported=a"):
            with self.assertRaises(Fault) as failure:
                self.loop.manager_wire.request("GET", path)
            self.assertEqual(failure.exception.status, 422)
        with self.assertRaises(Fault) as failure:
            self.loop.wire.request("GET", "workers")
        self.assertEqual(failure.exception.status, 403)
        self.assertFalse(self.m.workers(online="false")["items"])
        self.clock.advance(31)
        self.assertEqual(self.m.workers(online="false", worker_id="oguri")["items"][0]["online"], False)

    def test_assign_working_update_dedup_and_version_conflict(self):
        task = self.m.create(guid(), "Title", "First instruction")
        tid = task["task_id"]
        assigned = self.m.act(guid(), tid, task["revision"], "assign", worker_id="oguri")
        self.assertEqual(assigned["delivery_state"], "pending")
        self.w.poll()
        original = self.m.inspect(tid)["task"]
        key, changes = guid(), {"instructions": "New instruction"}
        updated = self.m.update(key, tid, original["revision"], changes)
        self.assertEqual(updated, self.m.update(key, tid, original["revision"], changes))
        self.assertEqual(updated["assignment_id"], original["assignment_id"])
        with self.assertRaises(Fault) as failure:
            self.m.update(guid(), tid, original["revision"], {"title": "Stale edit"})
        self.assertEqual(failure.exception.code, "revision_conflict")
        with self.assertRaises(Fault) as failure:
            self.m.update(guid(), tid, updated["revision"], {"assignee_id": "someone"})
        self.assertEqual(failure.exception.status, 422)
        self.w.poll()
        frame, = [f for f in self.loop.wire.frames(0) if f["kind"] == "task.updated"]
        self.w.process(frame)
        entries = self.loop.native.entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1][0]["changes"], changes)
        self.assertEqual(self.w.hold()["held_assignment_id"], original["assignment_id"])
        self.assertIsNone(self.m.inspect(tid)["last_report"])

    def test_update_outstanding_offer_fences_stale_input(self):
        first = self.m.create(guid(), "Title", "Old instruction", "oguri")
        second = self.m.update(guid(), first["task_id"], first["revision"], {"instructions": "New instruction"})
        self.assertNotEqual(first["assignment_id"], second["assignment_id"])
        self.w.poll()
        entries = self.loop.native.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0]["instructions"], "New instruction")
        self.assertEqual(self.w.hold()["held_assignment_id"], second["assignment_id"])

    def test_unavailable_target_holds_at_router(self):
        task = self.m.create(guid(), "Unassigned", "Wait for registration")
        receipt = self.m.act(guid(), task["task_id"], task["revision"], "assign", worker_id="offline")
        self.assertEqual(receipt["task"]["holder"], {"kind": "router"})
        self.assertIsNone(receipt["task"]["assignment_id"])
        self.assertFalse(self.loop.native.entries())
