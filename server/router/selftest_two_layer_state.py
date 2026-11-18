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
from selftest_router import FakeWorker, fixture_binding, fixture_end, http


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

    def test_01_cancel_timeout_pauses_and_retains_occupancy(self):
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
                self.assertEqual((saved["state"], saved["pause_reason"], saved["result"]["cancel_confirmed"]),
                                 ("paused", "cancel_unconfirmed", False))
                self.assertIsNone(saved["finished_at"])
                self.assertGreaterEqual(elapsed, timeout)
                self.assertLess(elapsed, timeout + 2)
                self.assertTrue(self.worker()["busy"])
                code, refused = self.create("must not overlap unknown execution")
                self.assertEqual((code, refused["error"]), (409, "worker_paused"))
                self.assertEqual(self.sent, [])
                self.assertEqual(self.call("/result", {"name": self.name, "task_id": task_id,
                                      "status": "done", "summary": "late"})[0], 202)
                release = {"confirm": True, "reason": "operator independently verified stop",
                           "execution_epoch": saved["execution_epoch"]}
                path = "/tasks/" + task_id + "/cancel-release"
                self.assertEqual(self.call(path, {**release, "execution_epoch": "old"})[0], 409)
                self.assertEqual(self.call(path, release)[1]["state"], "cancelled")
                self.assertTrue(self.store.get_task(task_id)["result"]["manual_release"])
                self.assertFalse(self.store.get_task(task_id)["result"]["cancel_confirmed"])
                self.assertEqual(self.create("next after manual release")[0], 200)
                print("cancel timeout %.3fs -> paused/cancel_unconfirmed; next HTTP %d %s; manual release HTTP 200"
                      % (elapsed, code, refused["error"]), flush=True)
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

    def test_04_legacy_ordinary_turn_ending_cannot_settle_the_new_order(self):
        self.call("/heartbeat", {"name": self.name, "state": "running", "turn_source": "trainer"})
        _, task = self.create("new task waiting behind an ordinary turn")
        task_id = task["task_id"]
        self.assertEqual(self.client.wait_task(3)["task_id"], task_id)
        _, idle = self.call("/heartbeat", {"name": self.name, "state": "idle", "task_id": None})
        saved = self.store.get_task(task_id)
        self.assertEqual((saved["state"], saved["finished_at"], saved["result"]["outcome"]),
                         ("running", None, "needs_inspection"))
        code, candidate = self.call("/result", {"name": self.name, "task_id": task_id,
                                                "status": "done", "summary": "late actual result"})
        self.assertEqual((code, candidate["candidate"], idle["settlements"], len(self.sent)), (202, True, [], 0))
        self.assertIsNone(self.stored_views())
        print("legacy trainer idle -> running/needs_inspection; late result HTTP 202; notifications=0", flush=True)

    def test_matching_stop_releases_paused_slot_and_rejects_old_ack(self):
        _, task = self.create("stop on the original turn")
        task_id = task["task_id"]
        native = fixture_binding(self.client.wait_task(3))
        self.call("/tasks/" + task_id + "/turn-binding", {"name": self.name, "connection_generation": self.server.current_conn(self.name).generation, **native})
        self.call("/tasks/" + task_id + "/cancel")
        with mock.patch.object(router, "CANCEL_TIMEOUT_S", 0):
            self.server.sweep_cancellations()
        conn = self.server.current_conn(self.name)
        outcome = {"task_id": task_id, "state": "cancelled", "ok": True, "executionStopped": True,
                   "pendingCommands": 0, "connection_generation": conn.generation, **native}
        def ack(**changes):
            return self.call("/tasks/" + task_id + "/cancel-ack", {"name": self.name, "outcome": {**outcome, **changes}})[1]
        self.assertEqual(ack(connection_generation="old")["state"], "paused")
        self.assertEqual(ack(execution_epoch="old")["state"], "paused")
        self.assertEqual(ack(turn_id="other")["state"], "paused")
        self.assertEqual(ack(executionStopped=False)["state"], "paused")
        self.assertEqual(ack()["state"], "cancelled")
        self.assertTrue(self.store.get_task(task_id)["result"]["cancel_confirmed"])
        self.assertEqual(ack()["state"], "cancelled")
        self.assertEqual(self.create("next after verified stop")[0], 200)
        print("stale or unconfirmed ACK -> paused; matching stop -> cancelled, cancel_confirmed=true, next HTTP 200", flush=True)

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
        self.assertEqual(self.store.get_task(task_id)["state"], "running")
        native = self.store.get_task(task_id)["turn_binding"]
        code, _ = self.call("/result", {"name": self.name, "task_id": task_id, "status": "done",
                                       "connection_generation": generation, "native_completion": fixture_end(native), "summary": "original result"})
        self.assertEqual(code, 200)
        self.assertEqual(self.store.get_task(task_id)["state"], "done")
        self.assertEqual(len(self.sent), 1)
        print("versioned SSE task -> task_recovery -> paused HTTP 200 -> worker_paused HTTP 409 -> running new epoch -> done, one notice", flush=True)

    def test_pause_and_resume_on_same_connection_preserve_native_binding(self):
        self.close_worker()
        stream, generation = self.versioned_stream()
        _, task = self.create("same-connection pause")
        _, dispatch = self.stream_event(stream)
        task_id = task["task_id"]
        path = "/tasks/" + task_id
        identity = fixture_binding(dispatch)
        sender = {"name": self.name, "connection_generation": generation}
        self.assertEqual(self.call(path + "/turn-binding", {**identity, **sender})[0], 200)
        pause = {**sender, "state": "paused", "pause_reason": "resume_unconfirmed",
                 "expected_epoch": dispatch["execution_epoch"]}
        self.assertEqual(self.call(path + "/execution", pause)[1]["state"], "paused")
        self.assertEqual(self.create("blocked")[1]["error"], "worker_paused")
        evidence = {"thread_id": identity["thread_id"], "turn_id": identity["turn_id"], "message_id": "receipt"}
        resume = {**pause, "state": "running", "evidence": evidence, "execution_epoch": "a" * 32}
        self.assertEqual(self.call(path + "/execution", {**resume, "evidence": {**evidence, "turn_id": "other"}})[0], 409)
        self.assertEqual(self.call(path + "/execution", resume)[1]["state"], "running")
        self.call(path + "/cancel")
        with mock.patch.object(router, "CANCEL_TIMEOUT_S", 0):
            self.server.sweep_cancellations()
        stale_resume = {**resume, "expected_epoch": resume["execution_epoch"], "execution_epoch": "b" * 32}
        self.assertEqual(self.call(path + "/execution", stale_resume)[1]["error"], "cancellation_pending")
        self.assertEqual(self.store.get_task(task_id)["pause_reason"], "cancel_unconfirmed")
        print("pause -> HTTP 409 worker_paused; exact binding resumes; unconfirmed stop blocks resume", flush=True)

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
