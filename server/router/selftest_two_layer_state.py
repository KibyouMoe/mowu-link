"""Isolated Router HTTP and SSE acceptance for state separation phase 1."""

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import server as router
from selftest_router import FakeWorker, http, worker_http


class TwoLayerStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="router-state-p1-")
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "router.db"
        with closing(sqlite3.connect(path)) as legacy:
            legacy.execute("""CREATE TABLE workers (
                name TEXT PRIMARY KEY, online INTEGER DEFAULT 0, busy INTEGER DEFAULT 0,
                queued INTEGER DEFAULT 0, last_seen REAL, connected_at REAL,
                version TEXT, note TEXT, state TEXT, progress_at REAL, task_id TEXT)""")
            legacy.execute("INSERT INTO workers(name, busy) VALUES ('legacy-worker', 1)")
            legacy.commit()
        self.name = "legacy-worker"
        self.store = router.RouterStore(str(path))
        self.addCleanup(self.store.close)
        self.sent = []
        trigger = mock.Mock()
        trigger.post_trigger.side_effect = lambda payload: (self.sent.append(payload) or True, {"id": len(self.sent)})
        self.server = router.RouterServer(("127.0.0.1", 0), self.store, "phase1-token",
                                          router.TriggerNotifier(trigger, self.store), mock.Mock(), ping_interval=0.2)
        self.addCleanup(self.server.server_close)
        port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.shutdown)
        self.call("/register", {"name": self.name, "version": "legacy"})
        self.client = FakeWorker(self.name, self.server.token, port=port)
        self.client.start()
        self.addCleanup(self.close_worker)
        self.assertTrue(self.client.connected.wait(3))
        self.assertIsNone(self.client.error)

    def close_worker(self):
        conn = self.server.current_conn(self.name)
        if conn is not None:
            conn.close("selftest complete")
        self.client.stop()
        until = time.monotonic() + 2
        while self.server.online(self.name) and time.monotonic() < until:
            time.sleep(0.02)
        self.assertFalse(self.server.online(self.name))

    def call(self, route, body=None, method="POST"):
        return http(method, self.base + route, body, self.server.token)

    def create(self, goal):
        return self.call("/tasks", {"worker": self.name, "goal": goal})

    def worker(self):
        return self.call("/workers", method="GET")[1]["workers"][0]

    def stored_views(self):
        with self.store._lock:
            return self.store._conn.execute("SELECT turn_views_json FROM workers WHERE name=?",
                                            (self.name,)).fetchone()[0]

    def test_01_cancel_timeout_releases_occupancy(self):
        real_timeout = os.environ.get("ROUTER_TEST_REAL_CANCEL_TIMEOUT") == "1"
        timeout = router.CANCEL_TIMEOUT_S if real_timeout else 0.25
        with mock.patch.object(router, "CANCEL_TIMEOUT_S", timeout):
            sweep = router.ApprovalSweeper(self.store, server=self.server)
            sweep.start()
            try:
                _, task = self.create("execution never acknowledges cancellation")
                task_id = task["task_id"]
                self.assertEqual(self.client.wait_task(3)["task_id"], task_id)
                start = time.monotonic()
                code, cancel = self.call("/tasks/%s/cancel" % task_id)
                self.assertEqual((code, cancel["state"]), (202, "cancelling"))
                self.assertEqual(self.create("must wait for cancellation")[1]["error"], "worker_busy")
                deadline = start + timeout + 2
                while self.store.get_task(task_id)["state"] == "cancelling" and time.monotonic() < deadline:
                    time.sleep(0.02)
                elapsed = time.monotonic() - start
                saved = self.call("/tasks/" + task_id, method="GET")[1]["task"]
                self.assertEqual((saved["state"], saved["result"]["error_code"],
                                  saved["result"]["cancel_confirmed"]), ("cancelled", "worker_cancel_timeout", False))
                self.assertGreaterEqual(saved["finished_at"] - saved["result"]["cancel_requested_at"], timeout)
                self.assertLess(elapsed, timeout + 2)
                self.assertFalse(self.worker()["busy"])
                code, next_task = self.create("accepted after timeout")
                self.assertEqual((code, next_task.get("dispatched")), (200, True), next_task)
                self.assertEqual(self.client.wait_task(3)["task_id"], next_task["task_id"])
                late_code, late = self.call("/result", {"name": self.name, "task_id": task_id,
                                                      "status": "done", "summary": "late"})
                self.assertEqual((late_code, late["state"], late["duplicate"]), (200, "cancelled", True))
                sweep.stop()
                sweep.join(3)
                self.assertEqual([(n["event_id"], n["level"]) for n in self.sent], [(task_id + ":cancelled", "notify")])
                print("cancel timeout %.3fs -> cancelled, confirmed=false, next dispatch HTTP %d"
                      % (saved["finished_at"] - saved["result"]["cancel_requested_at"], code), flush=True)
            finally:
                sweep.stop()
                sweep.join(3)

    def test_02_empty_worker_keeps_busy_false_on_idle_and_trainer_heartbeats(self):
        for state in ("idle", "running", "idle"):
            code, pulse = self.call("/heartbeat", {"name": self.name, "state": state, "turn_source": "trainer"})
            self.assertEqual((code, pulse["busy"], self.worker()["busy"]), (200, False, False))
        print("no-task heartbeats idle/running/idle -> busy=false on all three", flush=True)

    def test_03_running_task_blocks_dispatch_even_with_waiting_capacity(self):
        self.server.backlog_max = 128
        code, running = self.create("first")
        self.assertEqual(code, 200)
        self.assertEqual(self.client.wait_task(3)["task_id"], running["task_id"])
        code, second = self.create("overlap")
        self.assertEqual((code, second["error"], second["task_id"]), (409, "worker_busy", running["task_id"]))
        self.assertEqual(self.worker()["queued"], 0)
        self.assertIsNone(self.client.wait_task(0.1))
        print("running + backlog capacity 128 -> HTTP 409 worker_busy, second SSE frame absent", flush=True)

    def test_04_legacy_worker_migrates_and_completes_without_turn_samples(self):
        self.assertIsNone(self.stored_views())
        code, task = self.create("legacy flow")
        self.assertEqual(code, 200)
        task_id = task["task_id"]
        self.assertEqual(self.client.wait_task(3)["task_id"], task_id)
        code, terminal = worker_http("POST", self.base + "/result", {
            "name": self.name, "task_id": task_id, "status": "done", "summary": "legacy done"}, self.server.token)
        self.assertEqual((code, terminal["state"], len(self.sent)), (200, "done", 1))
        self.assertIsNone(self.stored_views())
        self.assertFalse(self.worker()["busy"])
        print("legacy register -> SSE dispatch -> HTTP 200 done, one notice, turn_views_json=NULL", flush=True)

    def test_paused_task_blocks_and_turn_view_is_write_only(self):
        paused = self.store.create_task(self.name, "paused fixture", state="paused")
        queued = self.store.create_task(self.name, "already queued")
        view = {"thread": {"phase": "idle", "turnId": None, "observedAt": 1.0,
                           "seq": 1, "generation": "generation", "fresh": True}}
        code, _ = self.call("/heartbeat", {"name": self.name, "state": "idle", "turn_views": view})
        self.assertEqual((code, json.loads(self.stored_views())), (200, view))
        self.assertNotIn("turn_views_json", self.worker())
        self.assertEqual(self.store.get_task(queued["task_id"])["state"], "queued")
        code, denied = self.create("not yet")
        self.assertEqual((code, denied["error"], denied["task_id"]), (409, "worker_paused", paused["task_id"]))
        self.assertTrue(self.worker()["busy"])
        invalid = {"thread": {**view["thread"], "seq": True}}
        self.assertEqual(self.call("/heartbeat", {"name": self.name, "turn_views": invalid})[0], 400)
        self.assertEqual(json.loads(self.stored_views()), view)


if __name__ == "__main__":
    unittest.main()
