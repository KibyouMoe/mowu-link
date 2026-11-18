"""Isolated Router HTTP and SSE acceptance for the two state layers."""

from http import client as http_client
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

    def versioned_stream(self):
        port = self.server.server_address[1]
        connection = http_client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/stream?name=%s&state_layers=2" % self.name,
                           headers={"X-Router-Token": self.server.token})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.addCleanup(connection.close)
        self.addCleanup(response.close)
        return response, response.getheader("X-Router-Generation")

    def stream_event(self, response):
        while True:
            raw = response.readline()
            self.assertTrue(raw, "SSE closed before the expected event")
            line = raw.decode("utf-8").strip()
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
                data = response.readline().decode("utf-8").partition(":")[2]
                return event, json.loads(data)

    def test_versioned_reconnect_pauses_then_resumes_with_current_execution_epoch(self):
        self.close_worker()
        _, registration = self.call("/register", {"name": self.name})
        self.assertEqual(registration["state_layers"], 2)
        initial, old_generation = self.versioned_stream()
        _, task = self.create("hold work across reconnect")
        task_id = task["task_id"]
        event, payload = self.stream_event(initial)
        self.assertEqual(event, "task")
        epoch = payload["execution_epoch"]
        self.assertRegex(epoch, r"^[a-f0-9]{32}$")
        route = "/tasks/" + task_id
        idle_before_result = {"name": self.name, "state": "idle", "connection_generation": old_generation}
        self.call("/heartbeat", idle_before_result)
        self.call("/heartbeat", {**idle_before_result, "finished_execution_epoch": epoch})
        self.assertEqual(self.store.get_task(task_id)["state"], "running")
        self.assertEqual(self.sent, [])
        self.server.current_conn(self.name).close("test Worker exited")
        self.assertEqual(initial.read(), b"\n")
        deadline = time.monotonic() + 2
        while self.server.online(self.name) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.server.online(self.name))
        self.store.reset_runtime()
        self.assertEqual(self.store.get_task(task_id)["state"], "running")

        recovered, generation = self.versioned_stream()
        event, payload = self.stream_event(recovered)
        self.assertEqual((event, payload["task_id"], payload["execution_epoch"]), ("task_recovery", task_id, epoch))
        paused = {"name": self.name, "state": "paused", "pause_reason": "session_unavailable",
                  "expected_epoch": epoch, "connection_generation": generation}
        code, old = self.call(route + "/execution", {**paused, "connection_generation": old_generation})
        self.assertEqual((code, old["error"]), (409, "stale_connection_generation"))
        code, state = self.call(route + "/execution", paused)
        self.assertEqual((code, state["state"], state["pause_reason"]), (200, "paused", "session_unavailable"))
        self.assertTrue(self.call(route + "/execution", paused)[1]["duplicate"])
        code, refused = self.create("cannot run while paused")
        self.assertEqual((code, refused["error"], refused["pause_reason"]), (409, "worker_paused", "session_unavailable"))
        idle = {"name": self.name, "state": "idle", "connection_generation": generation}
        self.call("/heartbeat", {**idle, "finished_execution_epoch": epoch})
        self.assertEqual(self.call("/result", {"name": self.name, "task_id": task_id,
                         "status": "done", "summary": "original result"})[0], 202)
        self.assertEqual(self.sent, [])

        resumed = {**paused, "state": "running", "execution_epoch": "b" * 32,
                   "evidence": {"thread_id": "old-thread", "turn_id": "old-turn", "message_id": "original-receipt"}}
        code, running = self.call(route + "/execution", resumed)
        self.assertEqual((code, running["state"], running["execution_epoch"], running["pause_reason"]),
                         (200, "running", "b" * 32, None))
        self.assertTrue(self.call(route + "/execution", resumed)[1]["duplicate"])
        self.assertEqual(self.call(route + "/execution", paused)[0], 409)
        self.call("/heartbeat", {**idle, "finished_execution_epoch": epoch})
        self.assertEqual(self.sent, [])
        self.call("/heartbeat", {**idle, "finished_execution_epoch": running["execution_epoch"]})
        self.assertEqual(self.store.get_task(task_id)["state"], "done")
        self.assertEqual(len(self.sent), 1)
        print("versioned SSE task -> task_recovery -> paused HTTP 200 -> worker_paused HTTP 409 -> running new epoch -> done, one notice", flush=True)

    def test_versioned_samples_reject_old_connection_and_ignore_reordered_sequence(self):
        self.close_worker()
        _, generation = self.versioned_stream()
        base = {"name": self.name, "connection_generation": generation, "state": "idle"}
        for phase, sequence, expected in (("thinking", 1, 1), ("outputting", 2, 2),
                                          ("idle", 3, 3), ("thinking", 2, 3)):
            view = {"phase": phase, "turnId": None if phase == "idle" else "observed",
                    "observedAt": 1700000000, "seq": sequence, "generation": generation, "fresh": True}
            self.assertEqual(self.call("/heartbeat", {**base, "turn_views": {"thread": view}})[0], 200)
            saved = json.loads(self.stored_views())["thread"]
            self.assertEqual(saved["seq"], expected)
        self.assertEqual(saved["phase"], "idle")
        status, error = self.call("/heartbeat", {**base, "connection_generation": "stale"})
        self.assertEqual((status, error["error"]), (409, "stale_connection_generation"))
        print("turn_views thinking -> outputting -> idle, stale generation HTTP 409, reordered sample ignored", flush=True)


if __name__ == "__main__":
    unittest.main()
