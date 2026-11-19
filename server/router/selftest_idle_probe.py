"""Live HTTP, SSE and SQLite with deterministic time at the production thresholds."""

import json
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import Mock

import server as router
from selftest_router import http


class ProbeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = router.RouterStore(str(Path(temp.name) / "router.db"))
        self.addCleanup(self.store.close)
        self.events = []
        trigger = Mock()
        trigger.post_trigger.side_effect = lambda payload: (
            self.events.append(payload) or True,
            {"id": len(self.events)},
        )
        self.api = router.RouterServer(
            ("127.0.0.1", 0),
            self.store,
            "fixture",
            router.TriggerNotifier(trigger, self.store),
            Mock(),
            verdict_token="reviewer",
        )
        self.addCleanup(self.api.server_close)
        thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.api.shutdown)
        self.base = "http://127.0.0.1:%d" % self.api.server_port
        self.now = time.time()
        self.api.lifecycle.clock = lambda: self.now
        self.store.orders.clock = lambda: self.now
        self.post("/register", {"name": "test-worker"})
        self.conn = self.api.attach("test-worker")
        self.task = self.post("/tasks", {"worker": "test-worker", "goal": "验收"})[1][
            "task_id"
        ]
        self.assertEqual(self.conn.queue.get_nowait()["event"], "task")

    def post(self, path, body):
        return http("POST", self.base + path, body, "fixture")

    def beat(self, state="idle", **kw):
        code, _ = self.post(
            "/heartbeat",
            {
                "name": "test-worker",
                "bBusy": state != "idle",
                "TaskID": self.task,
                **kw,
            },
        )
        self.assertEqual(code, 200)

    def advance(self, seconds, state="idle"):
        while seconds:
            step = min(5, seconds)
            seconds -= step
            self.now += step
            self.beat(state)
            self.api.lifecycle.sweep()

    def take(self):
        event = self.conn.queue.get_nowait()
        self.assertEqual(event["event"], "deliverable_request")
        return event["data"]

    def first(self):
        self.beat()
        self.advance(59)
        self.assertTrue(self.conn.queue.empty())
        self.advance(1)
        return self.take()

    def report(self, q, **fields):
        return self.post(
            "/tasks/" + self.task + "/deliverable",
            {
                "worker": "test-worker",
                "request_id": q["request_id"],
                "summary": "跑完三项检查",
                "artifacts": ["/tmp/report"],
                "evidence": ["git rev-parse: fixture"],
                "risks": [],
                **fields,
            },
        )

    def saved(self):
        return http("GET", self.base + "/tasks/" + self.task, token="fixture")[1][
            "task"
        ]

    def test_zero_heartbeat_archives_only_the_dropped_task_and_advances_fifo(self):
        self.beat()
        _, queued = self.post("/tasks", {"worker": "test-worker", "goal": "next"})
        self.post("/register", {"name": "other-worker"})
        self.api.attach("other-worker")
        _, unrelated = self.post("/tasks", {"worker": "other-worker", "goal": "unrelated"})
        generation = self.conn.generation
        zero = {"name": "test-worker", "TaskID": 0, "bBusy": False, "seq": 1, "stream": generation}
        with self.assertLogs("router", "INFO") as logs:
            self.assertEqual(self.post("/heartbeat", zero)[0], 200)
        self.assertEqual(self.saved()["archive"]["reason"], "worker_dropped_task")
        self.assertFalse(self.saved()["in_running_tasks"])
        self.assertFalse(any(f["kind"] == "summary_request" for f in self.store.orders.facts(self.task)))
        self.assertEqual(self.store.orders.held("other-worker"), unrelated["task_id"])
        self.assertEqual(self.conn.queue.get_nowait()["data"]["task_id"], queued["task_id"])
        self.assertTrue(self.post("/heartbeat", zero)[1]["duplicate"])
        self.api.lifecycle.sweep()
        self.api.lifecycle.sweep()
        self.assertEqual([event["kind"] for event in self.events], ["task.worker_dropped_task"])
        self.assertEqual(self.events[0]["payload"]["reason"], "worker_dropped_task")
        self.assertEqual(self.store.orders.held("test-worker"), queued["task_id"])
        print(next(line for line in logs.output if "worker_dropped_task" in line),
              "notice=1, summaries=0, next FIFO dispatched, unrelated task unchanged")

    def test_zero_heartbeat_requeues_unoffered_active_task_behind_waiting_work(self):
        self.api.detach(self.conn)
        self.assertTrue(self.post("/tasks/" + self.task + "/cancel", {})[1]["archived"])
        self.conn = self.api.attach("test-worker")
        orders = self.store.orders
        ghost = orders.create("missing offer", "missing offer", "test-worker", {}, "hermes", None, None)
        waiting = orders.create("queued next", "queued next", "test-worker", {}, "hermes", None, None)
        # Reproduce the durable state left by the pre-fix claim/send split.
        with orders.atomic():
            orders.db.execute("INSERT INTO active_tasks(task_id,worker) VALUES(?,?)", (ghost, "test-worker"))
            orders.db.execute("DELETE FROM dispatch_queue WHERE task_id=?", (ghost,))
        self.assertEqual(orders.facts(ghost), [])
        self.conn.offered_task = ghost
        self.conn.task_sent_at = self.now
        with self.assertLogs("router", "INFO") as logs:
            self.beat(TaskID=0)
        frame = self.conn.queue.get_nowait()
        self.assertEqual((frame["event"], frame["data"]["task_id"]), ("task", waiting))
        self.assertEqual(orders.held("test-worker"), waiting)
        self.assertFalse(orders.get(ghost)["in_running_tasks"])
        self.assertIsNone(orders.get(ghost)["archive"])
        self.assertEqual(orders.facts(ghost), [])
        with self.store._lock:
            rows = orders.db.execute("SELECT task_id FROM dispatch_queue WHERE worker=?", ("test-worker",)).fetchall()
        self.assertEqual([row["task_id"] for row in rows], [ghost])
        self.api.lifecycle.sweep()
        self.assertEqual(self.events, [])
        self.assertTrue(self.conn.queue.empty())
        print(next(line for line in logs.output if "dispatch_requeued" in line),
              "preserved original TaskID; previous FIFO head dispatched on same heartbeat; archives=0")

    def test_failed_enqueue_leaves_task_in_fifo_without_active_membership(self):
        from unittest.mock import patch

        self.api.detach(self.conn)
        self.assertTrue(self.post("/tasks/" + self.task + "/cancel", {})[1]["archived"])
        self.conn = self.api.attach("test-worker")
        with patch.object(self.conn, "send_task", return_value=False):
            status, first = self.post("/tasks", {"worker": "test-worker", "goal": "send failed"})
            self.assertEqual((status, first["dispatched"], first["in_running_tasks"]), (202, False, False))
            _, second = self.post("/tasks", {"worker": "test-worker", "goal": "later"})
        orders = self.store.orders
        self.assertIsNone(orders.held("test-worker"))
        self.assertEqual(orders.facts(first["task_id"]), [])
        self.assertEqual(orders.facts(second["task_id"]), [])
        self.beat(TaskID=0)
        self.assertEqual(self.conn.queue.get_nowait()["data"]["task_id"], first["task_id"])
        self.assertEqual(orders.held("test-worker"), first["task_id"])
        self.assertEqual([f["kind"] for f in orders.facts(first["task_id"])], ["dispatch"])
        print("SSE enqueue refused -> queued with original FIFO position and no active task; retry commits dispatch")

    def test_clearing_stale_id_reoffers_sent_task_instead_of_archiving_it(self):
        old_id = "t-stale-archived"
        for _ in range(3):
            self.beat(TaskID=old_id)
        self.conn.queued_task_offer = None
        self.conn.offered_task = self.task
        self.conn.task_sent_at = self.now - 20
        self.beat(TaskID=0)
        frame = self.conn.queue.get_nowait()
        self.assertEqual((frame["event"], frame["data"]["task_id"]), ("task", self.task))
        self.assertIsNone(self.saved()["archive"])
        self.assertIsNone(self.conn.offered_task)
        self.assertEqual(self.conn.queued_task_offer, self.task)
        self.beat(TaskID=0)
        self.assertTrue(self.conn.queue.empty())
        self.assertIsNone(self.saved()["archive"])
        self.beat()
        self.assertEqual(self.store.get_worker("test-worker")["TaskID"], self.task)
        print("old TaskID cleared after 3 corrections -> exact sent task reoffered once, archived=0")

    def test_dispatch_logs_wire_send_and_worker_ownership_separately(self):
        with closing(HTTPConnection("127.0.0.1", self.api.server_port, timeout=4)) as stream:
            with self.assertLogs("router", "INFO") as logs:
                stream.request("GET", "/stream?name=test-worker", headers={"X-Router-Token": "fixture"})
                with closing(stream.getresponse()) as wire:
                    self.assertEqual(wire.status, 200)
                    while True:
                        line = wire.readline()
                        self.assertTrue(line)
                        if line == b"event: task\n":
                            self.assertEqual(json.loads(wire.readline().removeprefix(b"data: "))["task_id"], self.task)
                            break
                    self.beat()
                    self.assertEqual(self.store.get_worker("test-worker")["TaskID"], self.task)
        for event in ("dispatch_queued", "dispatch_sent", "dispatch_confirmed"):
            print(next(line for line in logs.output if f":{event} " in line and self.task in line))

    def test_queued_and_just_sent_offers_survive_racing_zero_heartbeats(self):
        self.beat(TaskID=0)
        self.assertTrue(self.saved()["in_running_tasks"])
        self.conn.queued_task_offer = None
        self.conn.offered_task = self.task
        self.conn.task_sent_at = self.now
        self.now += 4.9
        self.beat(TaskID=0)
        self.assertTrue(self.saved()["in_running_tasks"])
        self.now += 0.2
        self.beat(TaskID=0)
        self.assertEqual(self.saved()["archive"]["reason"], "worker_dropped_task")
        print("unconfirmed offer survives pre-send and early zero; after one heartbeat interval, zero archives it")

    def test_cancelling_zero_heartbeat_archives_and_dispatches_next(self):
        self.beat()
        _, queued = self.post("/tasks", {"worker": "test-worker", "goal": "next"})
        self.post("/tasks/" + self.task + "/cancel", {})
        cancel = self.take()
        self.beat(TaskID=0)
        self.assertEqual(self.saved()["archive"]["reason"], "cancelled_without_ack")
        self.assertEqual(self.conn.queue.get_nowait()["data"]["task_id"], queued["task_id"])
        self.api.lifecycle.sweep()
        self.assertEqual(self.events, [])
        self.assertEqual(self.report(cancel, action="cancel")[0], 409)
        print("cancelling + zero TaskID -> cancelled_without_ack; next task dispatched in same heartbeat")

    def test_cancel_unheld_archives_immediately_without_sending_cancel(self):
        _, queued = self.post("/tasks", {"worker": "test-worker", "goal": "next"})
        code, reply = self.post("/tasks/" + self.task + "/cancel", {})
        self.assertEqual((code, reply["reason"]), (200, "cancelled_without_ack"))
        self.assertEqual(self.saved()["archive"]["reason"], "cancelled_without_ack")
        self.assertFalse(any(f["kind"] == "cancel_request" for f in self.store.orders.facts(self.task)))
        self.assertEqual(self.conn.queue.get_nowait()["data"]["task_id"], queued["task_id"])
        self.assertTrue(self.conn.queue.empty())
        print("cancel unheld HTTP 200; cancel requests=0; next FIFO task dispatched")

    def test_cancel_hard_deadline_releases_fifo_even_without_heartbeats(self):
        from summary_requests import SummaryRequests

        self.beat("busy")
        _, queued = self.post("/tasks", {"worker": "test-worker", "goal": "next"})
        route = "/tasks/" + self.task + "/cancel"
        self.assertEqual(self.post(route, {})[0], 202)
        first = self.take()
        # Rebuild the scheduler with the same DB and no in-memory idle samples.
        self.api.lifecycle = SummaryRequests(self.api, None)
        self.api.lifecycle.clock = lambda: self.now
        self.now += 179
        self.api.lifecycle.sweep()
        self.assertEqual(self.saved()["state"], "cancelling")
        self.assertTrue(self.post(route, {})[1]["duplicate"])
        self.now += 1
        self.api.lifecycle.sweep()
        self.assertEqual(self.saved()["archive"]["reason"], "archive_unconfirmed")
        self.assertEqual(self.conn.queue.get_nowait()["data"]["task_id"], queued["task_id"])
        self.assertEqual(self.events[0]["payload"]["reason"], "archive_unconfirmed")
        self.assertEqual(self.report(first, action="cancel")[0], 409)
        # The next offer may have reached a worker that still held the old ID.
        # Clearing that ID must re-offer the same task, without dropping it.
        stale = self.post("/heartbeat", {"name": "test-worker", "bBusy": True, "TaskID": self.task})[1]
        self.assertEqual(stale["clear_task_id"], self.task)
        self.conn.queued_task_offer = None
        self.conn.offered_task = queued["task_id"]
        self.conn.task_sent_at = self.now
        self.now += 6
        self.beat(TaskID=0)
        self.assertEqual(self.conn.queue.get_nowait()["data"]["task_id"], queued["task_id"])
        self.beat(TaskID=queued["task_id"])
        self.api.lifecycle.sweep()
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.store.orders.held("test-worker"), queued["task_id"])
        print("hard deadline: at 179s cancelling; at 180s archive_unconfirmed + FIFO dispatch; clear replays same offer")

    def test_cancelling_different_task_id_archives_even_while_busy(self):
        self.beat()
        self.post("/tasks/" + self.task + "/cancel", {})
        self.take()
        self.beat("busy", TaskID="t-another-id")
        self.assertEqual(self.saved()["archive"]["reason"], "cancelled_without_ack")
        self.assertTrue(self.conn.queue.empty())

    def test_idle_archive_and_summary_notification(self):
        q = self.first()
        self.assertEqual(q["purpose"], "summary")
        self.assertEqual(self.report(q)[0], 200)
        task = self.saved()
        self.assertFalse(task["in_running_tasks"])
        self.assertEqual(task["task_info"]["TaskOwner"], "test-worker")
        self.assertEqual(self.store.get_worker("test-worker")["TaskID"], 0)
        self.assertEqual(self.events[0]["payload"]["task_info"], task["task_info"])
        self.assertEqual(self.events[0]["payload"]["deliverable"], task["deliverable"])
        print(
            "idle 59s=0 requests; 60s=1; summary HTTP 200; TaskID=0; active=false; package=TaskInfo+summary"
        )

    def test_summary_and_cancel_attempts(self):
        with self.assertLogs("router", level="INFO") as log:
            first = self.first()
        self.assertIn(
            f"probe_queued task={self.task} request={first['request_id']} attempt=1 via=sse purpose=summary",
            log.output[-1],
        )
        for attempt in (2, 3):
            self.advance(60)
            self.assertEqual(self.take()["attempt"], attempt)
        self.advance(180)
        self.assertTrue(self.conn.queue.empty())
        self.assertTrue(self.saved()["in_running_tasks"])
        self.assertIsNone(self.saved()["archive"])
        self.assertEqual([e["kind"] for e in self.events], ["task.summary_missing"])
        self.assertFalse(any(f["kind"] == "cancel_request" for f in self.store.orders.facts(self.task)))
        # Exercise the retry cap independently from the 180-second deadline
        # and from a more permissive summary configuration.
        self.api.lifecycle.interval = 30
        self.api.lifecycle.max_attempts = 5
        self.assertEqual(self.post("/tasks/" + self.task + "/cancel", {})[0], 202)
        for attempt in (1, 2, 3):
            q = self.take()
            self.assertEqual((q["purpose"], q["attempt"]), ("cancel", attempt))
            with self.assertLogs("router", level="WARNING") as logs:
                self.advance(30)
            self.assertTrue(any("cancel_timeout" in line and q["request_id"] in line for line in logs.output))
        self.assertEqual(
            self.saved()["archive"]["reason"], "archive_unconfirmed"
        )
        self.assertEqual(
            [e["kind"] for e in self.events],
            ["task.summary_missing", "task.offline_archive"],
        )
        self.assertEqual(self.events[-1]["payload"]["reason"], "archive_unconfirmed")
        self.assertFalse(self.saved()["archive"]["confirmed_by_worker"])
        self.assertNotIn("state", self.saved())
        self.assertEqual(self.store.get_worker("test-worker")["TaskID"], 0)
        print(next(line for line in logs.output if "cancel_timeout" in line),
              "archive reason=archive_unconfirmed; offline_archive event=1")
        print(
            "summary attempts=1,2,3; summary_missing event=1; still active; automatic cancellations=0; manual cancel attempts=1,2,3; archived without worker confirmation; offline_archive event=1"
        )

    def test_same_idle_trigger_retries_cancel_on_wire_and_rejects_summary(self):
        # Pause the SSE writer after connecting so the old summary request is
        # still queued when a cancel arrives.
        from unittest.mock import patch

        connected = threading.Event()
        unblock = threading.Event()
        write = router.RouterHandler._write

        def hold_connected(handler, chunk):
            write(handler, chunk)
            if chunk == b": connected\n\n":
                connected.set()
                unblock.wait(5)

        with patch.object(router.RouterHandler, "_write", hold_connected):
            with closing(HTTPConnection("127.0.0.1", self.api.server_port, timeout=4)) as stream:
                stream.request("GET", "/stream?name=test-worker", headers={"X-Router-Token": "fixture"})
                with closing(stream.getresponse()) as wire, self.assertLogs("router", "INFO") as logs:
                    try:
                        self.assertTrue(connected.wait(2))
                        self.conn = self.api.current_conn("test-worker")
                        self.beat()
                        self.advance(60)
                        summary_id = self.saved()["probe_id"]
                        self.assertEqual(self.saved()["state"], "running")
                        code, cancelled = self.post("/tasks/" + self.task + "/cancel", {})
                        self.assertEqual((code, cancelled["state"]), (202, "cancelling"))
                        task = self.saved()
                        self.assertEqual(task["state"], "cancelling")
                        self.assertIsNone(task["probe_id"])
                        _, listing = http("GET", self.base + "/tasks", token="fixture")
                        self.assertEqual(next(t["state"] for t in listing["tasks"] if t["task_id"] == self.task),
                                         "cancelling")
                        self.assertEqual(self.report({"request_id": summary_id})[0], 409)
                    finally:
                        unblock.set()

                    def read_cancel():
                        while True:
                            line = wire.readline()
                            self.assertTrue(line, "SSE disconnected before cancellation")
                            if line == b"event: deliverable_request\n":
                                payload = json.loads(wire.readline().removeprefix(b"data: "))
                                self.assertEqual(payload["purpose"], "cancel")
                                return payload

                    first = read_cancel()
                    self.advance(60, "busy")
                    self.beat()
                    self.advance(59)
                    self.assertEqual(self.saved()["request_id"], first["request_id"])
                    self.advance(1)
                    retry = read_cancel()
                    self.assertEqual((first["attempt"], retry["attempt"]), (1, 2))
                    self.assertEqual(self.report(retry, action="cancel")[0], 200)
                    self.assertEqual(self.report(retry, action="cancel")[1]["duplicate"], True)
                    self.assertEqual(self.store.get_worker("test-worker")["TaskID"], 0)
                    self.assertEqual(self.saved()["archive"]["reason"], "worker_cancel")
                    self.assertNotIn("state", self.saved())
                    self.assertEqual([f["request_id"] for f in self.store.orders.facts(self.task)
                                      if f["kind"] == "summary_request"], [summary_id])
        self.assertEqual(self.events, [])
        for event, rid in (("cancel_sent", first["request_id"]), ("cancel_timeout", first["request_id"]),
                           ("cancel_retry", retry["request_id"]), ("cancel_acked", retry["request_id"])):
            entry = next(line for line in logs.output if event in line and rid in line)
            print(entry)
        print("wire summary frames after cancel=0; late summary HTTP 409; archive reason=worker_cancel; TaskID=0")

    def test_deferred_cancel_keeps_ownership_until_final_receipt(self):
        _, registration = self.post("/register", {"name": "test-worker", "TaskID": self.task})
        self.assertTrue(registration["cancel_deferred"])
        self.beat("busy")
        self.post("/tasks/" + self.task + "/cancel", {})
        q = self.take()
        with self.assertLogs("router", level="INFO") as logs:
            code, interim = self.report(q, action="cancel", deferred=True, reason="native_task_cancel_unavailable")
            self.assertEqual((code, interim["deferred"], interim["archived"]), (202, True, False))
            self.assertTrue(self.report(q, action="cancel", deferred=True,
                                        reason="native_task_cancel_unavailable")[1]["duplicate"])
            self.assertEqual(self.saved()["state"], "cancelling")
            self.assertEqual(self.store.get_worker("test-worker")["TaskID"], self.task)
            self.advance(179, "busy")
            self.assertEqual(self.events, [])
            self.assertTrue(self.conn.queue.empty())
            code, final = self.report(q, action="cancel", reason="user_cancel")
            self.assertEqual((code, final["worker_task_id"]), (200, 0))
        self.assertEqual(self.saved()["archive"]["reason"], "worker_cancel")
        self.assertEqual(self.saved()["archive"]["worker_reason"], "user_cancel")
        self.assertEqual(sum(f["kind"] == "worker_cancel_deferred" for f in self.store.orders.facts(self.task)), 1)
        for event in ("cancel_deferred", "cancel_acked"):
            matches = [line for line in logs.output if f":{event} " in line]
            self.assertEqual(len(matches), 1)
            print(matches[0])

    def test_online_cancel_requires_worker_and_offline_reconciliation(self):
        route = "/tasks/" + self.task + "/cancel"
        self.beat()
        self.assertEqual(self.post(route, {})[0], 202)
        q = self.take()
        self.assertTrue(self.saved()["in_running_tasks"])
        self.assertEqual(self.report(q, worker="wrong", action="cancel")[0], 409)
        self.assertEqual(self.report(q, action="cancel")[0], 200)
        self.assertTrue(self.saved()["archive"]["confirmed_by_worker"])
        self.assertEqual(self.saved()["task_info"]["TaskOwner"], "test-worker")
        self.beat(TaskID=0)
        _, second = self.post("/tasks", {"worker": "test-worker", "goal": "离线归档"})
        self.api.detach(self.conn)
        tid = second["task_id"]
        self.assertEqual(
            self.post("/tasks/" + tid + "/cancel", {})[1]["archived"], True
        )
        reply = self.post("/register", {"name": "test-worker", "TaskID": tid})[1]
        self.assertEqual((reply["clear_task_id"], reply["reason"]), (tid, "已取消"))
        self.assertTrue(reply["cancel_prompt"])
        print(
            "online cancel greeting=202; wrong worker=409; worker cancel=200, archived; offline cancel=200; register=已取消 + cancel_prompt"
        )

    def test_register_resumes_only_active_work_and_replays_pending_cancel(self):
        status, reply = self.post("/register", {"name": "test-worker", "TaskID": self.task})
        self.assertEqual(status, 200)
        self.assertEqual(reply["resume_task_id"], self.task)
        self.assertIn(self.task, reply["resume_prompt"])
        self.beat("busy")
        self.post("/tasks/" + self.task + "/cancel", {})
        first = self.take()
        _, reply = self.post("/register", {"name": "test-worker", "TaskID": self.task})
        self.assertNotIn("resume_prompt", reply)
        self.assertEqual(reply["cancel_request_id"], first["request_id"])
        self.conn = self.api.attach("test-worker")
        replay = self.take()
        self.assertEqual((replay["purpose"], replay["request_id"], replay["attempt"]),
                         ("cancel", first["request_id"], 1))
        with self.assertLogs("router", level="INFO") as logs:
            self.assertEqual(self.report(replay, action="cancel")[0], 200)
        self.assertTrue(any("cancel_acked" in line and first["request_id"] in line for line in logs.output))
        _, reply = self.post("/register", {"name": "test-worker", "TaskID": self.task})
        self.assertEqual(reply["clear_task_id"], self.task)
        self.assertNotIn("resume_prompt", reply)

    def test_first_registration_clears_offline_cancel_before_heartbeat(self):
        self.beat("busy")
        self.api.detach(self.conn)
        self.assertTrue(self.post("/tasks/" + self.task + "/cancel", {})[1]["archived"])
        with self.assertLogs("router", level="INFO") as logs:
            status, reply = self.post("/register", {"name": "test-worker", "TaskID": self.task})
        self.assertEqual(status, 200)
        self.assertEqual(reply["clear_task_id"], self.task)
        self.assertIn("这单已取消", reply["cancel_prompt"])
        self.assertEqual(self.store.get_worker("test-worker")["TaskID"], 0)
        entry = next(line for line in logs.output if "source=register" in line)
        print(entry, "register HTTP 200; Router TaskID=0; heartbeats since reconnect=0")

    def test_third_stale_report_releases_fifo_and_warns_once(self):
        self.api.detach(self.conn)
        self.post("/tasks/" + self.task + "/cancel", {})
        self.conn = self.api.attach("test-worker")
        stale = "t-no-longer-owned"

        def stale_beat(seq):
            return self.post("/heartbeat", {
                "name": "test-worker", "bBusy": False, "TaskID": stale,
                "seq": seq, "stream": self.conn.generation,
            })

        self.assertEqual(stale_beat(1)[1]["clear_task_id"], stale)
        code, queued = self.post("/tasks", {"worker": "test-worker", "goal": "FIFO after clear"})
        self.assertEqual(code, 202)
        self.assertTrue(stale_beat(1)[1]["duplicate"])
        self.assertEqual(stale_beat(2)[1]["clear_task_id"], stale)
        self.assertTrue(self.conn.queue.empty())
        # Replacing the SSE stream must preserve the correction budget.
        self.conn = self.api.attach("test-worker")
        self.assertTrue(self.conn.queue.empty())
        with self.assertLogs("router", level="WARNING") as logs:
            self.assertEqual(stale_beat(1)[1]["clear_task_id"], stale)
        self.assertEqual(self.conn.queue.get_nowait()["data"]["task_id"], queued["task_id"])
        self.assertNotIn("clear_task_id", stale_beat(2)[1])
        self.api.lifecycle.sweep()
        self.api.lifecycle.sweep()
        alerts = [e for e in self.events if e["kind"] == "worker.task_id_correction_limit"]
        self.assertEqual(
            [(e["level"], e["payload"]["TaskID"], e["payload"]["attempts"]) for e in alerts],
            [("event", stale, 3)],
        )
        self.assertTrue(self.conn.queue.empty())
        self.assertEqual(self.store.get_worker("test-worker")["TaskID"], 0)
        print(logs.output[-1], "FIFO dispatched; alerts=1; fourth stale heartbeat: no new correction")
        # Accepting the new offer preserves FIFO ownership.
        self.beat(TaskID=queued["task_id"])
        self.assertEqual(self.post("/tasks", {"worker": "test-worker", "goal": "later"})[0], 202)
        self.assertTrue(self.conn.queue.empty())

    def test_history_and_different_ids_do_not_spend_live_corrections(self):
        stale = "t-stale"
        self.beat(TaskID="t-other")
        self.beat(TaskID=stale)
        _, issued = self.post("/workers/test-worker/request-status", {})
        self.conn.queue.get_nowait()
        samples = [{"seq": i, "TaskID": stale, "bBusy": False} for i in range(1, 4)]
        reply = self.post("/workers/test-worker/status-history", {
            "request_id": issued["request_id"], "stream": self.conn.generation, "samples": samples,
        })
        self.assertEqual(reply, (200, {"ok": True, "through": 3}))
        self.beat(TaskID=stale)
        self.api.lifecycle.sweep()
        self.assertTrue(self.conn.awaiting_clear)
        self.assertEqual(self.events, [])
        self.beat(TaskID=self.task)
        self.assertFalse(self.conn.awaiting_clear)
        self.beat(TaskID=stale)
        self.assertEqual(self.api.lifecycle.open_correction("test-worker")["attempts"], 1)

    def test_duplicate_single_notification_and_late_receipt(self):
        q = self.first()
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda _: self.report(q), range(12)))
        self.assertEqual(sum(not r[1]["duplicate"] for r in replies), 1)
        self.assertEqual(len(self.events), 1)
        self.beat(TaskID=0)
        self.task = self.post("/tasks", {"worker": "test-worker", "goal": "迟到回执"})[
            1
        ]["task_id"]
        self.conn.queue.get_nowait()
        q = self.first()
        self.post("/tasks/" + self.task + "/cancel", {})
        cancel = self.take()
        self.report(cancel, action="cancel")
        for _ in range(2):
            self.assertEqual(self.report(q)[1]["detail"], "迟到回执已忽略")
        task = self.store.orders.get(self.task)
        self.assertFalse(task["in_running_tasks"])
        self.assertEqual(sum(f["kind"] == "late_receipt" for f in task["facts"]), 1)
        self.assertEqual(len(self.events), 1)
        print(
            "12 simultaneous reports: accepted=1 duplicate=11 notices=1; 2 late summaries: archived, ignored facts=1, extra notices=0"
        )

    def test_older_summary_after_archive_can_only_record_one_ignored_receipt(self):
        older = self.first()
        self.advance(60)
        accepted = self.take()
        self.assertNotEqual(older["request_id"], accepted["request_id"])
        self.assertEqual(self.report(accepted)[0], 200)
        for _ in range(2):
            self.assertEqual(
                self.report(older),
                (409, {"error": "delivery_request_closed", "detail": "迟到回执已忽略"}),
            )
        task = self.store.orders.get(self.task)
        self.assertFalse(task["in_running_tasks"])
        self.assertEqual(task["archive"]["reason"], "summary_received")
        self.assertEqual(sum(f["kind"] == "late_receipt" for f in task["facts"]), 1)
        self.assertEqual(len(self.events), 1)

    def test_durable_notification_dedup_survives_restart_and_rotates_cache(self):
        self.report(self.first())
        reopened = router.RouterStore(self.store.path)
        self.addCleanup(reopened.close)
        trigger = Mock()
        notifier = router.TriggerNotifier(trigger, reopened)
        accepted, receipt = notifier.post_task_notice(
            self.task, "deliverable", self.events[0]
        )
        self.assertTrue(accepted and receipt["duplicate"])
        trigger.post_trigger.assert_not_called()
        for i in range(501):
            reopened.remember_task_notice(str(i), "deliverable", {"id": i})
        self.assertIsNone(reopened.task_notice_receipt("0", "deliverable"))
        self.assertEqual(
            reopened.task_notice_receipt("500", "deliverable"), {"id": 500}
        )
        reopened._conn.execute(
            "UPDATE task_notice_receipts SET delivered_at=0 WHERE task_id='500'"
        )
        self.assertIsNone(reopened.task_notice_receipt("500", "deliverable"))
        # Pruning the short-term delivery cache must not resurrect the durable outbox.
        self.api.lifecycle.sweep()
        self.assertEqual(len(self.events), 1)

    def test_dispatch_retry_concurrency_and_registration_input_contract(self):
        for origin in (
            "http://example.com:80",
            "http://127.0.0.1:80/path",
            "http://user:password@127.0.0.1:80",
            "file:///tmp/x",
        ):
            self.assertEqual(
                self.post("/register", {"name": "test-worker", "control_url": origin})[
                    0
                ],
                400,
            )
        uid = str(uuid.uuid4())
        body = {"worker": "test-worker", "goal": "concurrent", "message_id": uid}
        for invalid in (None, "", "not-a-uuid", 123, {}, []):
            self.assertEqual(
                self.post("/tasks", {**body, "message_id": invalid})[0], 400
            )
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: self.post("/tasks", body)[1], range(8)))
        self.assertEqual(sum(item["duplicate"] for item in replies), 7)
        self.assertEqual(len({item["task_id"] for item in replies}), 1)
        retry = self.post(
            "/tasks",
            {"message_id": uid.upper(), "goal": "concurrent", "worker": "test-worker"},
        )[1]
        self.assertTrue(retry["duplicate"])
        task = self.store.orders.get(retry["task_id"])
        self.assertEqual(task["execution_mode"], "goal")
        self.assertIsNone(task.get("deadline_at"))
        first = self.post("/tasks", {"worker": "test-worker", "goal": "without UUID"})[
            1
        ]
        second = self.post("/tasks", {"worker": "test-worker", "goal": "without UUID"})[
            1
        ]
        self.assertNotEqual(first["task_id"], second["task_id"])

    def test_status_pair_and_ghost_notice(self):
        self.beat()
        self.api.lifecycle.sweep()
        self.assertEqual(self.events, [])
        self.post("/register", {"name": "chat-only"})
        for _ in range(3):
            self.assertEqual(self.post("/heartbeat", {"name": "chat-only", "bBusy": True, "TaskID": 0})[0], 200)
            self.api.lifecycle.sweep()
        self.assertEqual(self.events, [])
        code, body = self.post(
            "/notice",
            {
                "worker": "test-worker",
                "text": "legacy",
                "task_id": self.task,
                "kind": "task.result",
            },
        )
        self.assertEqual((code, body["notified"]), (409, False))
        self.assertEqual(
            self.post("/result", {"name": "test-worker", "task_id": self.task})[0], 409
        )
        print(
            "idle+TaskID anomaly=0; busy+0 repeated 3 times: anomaly events=0; legacy completion notifications=0"
        )

    def test_fifo_uses_only_task_id_and_survives_busy_and_queue(self):
        self.beat("busy")
        queued = [
            self.post("/tasks", {"worker": "test-worker", "goal": "next" + str(i)})
            for i in range(2)
        ]
        self.assertEqual([r[0] for r in queued], [202, 202])
        q = self.first()
        self.report(q)
        self.beat("busy", TaskID=0)
        self.assertEqual(
            self.conn.queue.get_nowait()["data"]["task_id"], queued[0][1]["task_id"]
        )
        self.assertTrue(self.conn.queue.empty())
        print(
            "TaskID occupied -> FIFO HTTP 202,202; busy=true + TaskID=0 -> dispatched first queued task"
        )

    def test_full_status_history_and_busy_resets_idle(self):
        stream = self.conn.generation

        def hb(seq, busy):
            return self.post(
                "/heartbeat",
                {
                    "name": "test-worker",
                    "stream": stream,
                    "seq": seq,
                    "bBusy": busy,
                    "TaskID": self.task,
                },
            )

        hb(1, False)
        self.now += 59
        self.assertEqual(hb(3, False)[0], 202)
        event = self.conn.queue.get_nowait()
        self.assertEqual(event["event"], "worker_status_request")
        reply = self.post(
            "/workers/test-worker/status-history",
            {
                **event["data"],
                "samples": [
                    {"seq": n, "bBusy": busy, "TaskID": self.task}
                    for n, busy in ((1, False), (2, True), (3, False))
                ],
            },
        )
        self.assertEqual(reply[1]["through"], 3)
        self.now += 1
        hb(4, False)
        self.assertTrue(self.conn.queue.empty())
        for seq in range(5, 17):
            self.now += 5 if seq != 16 else 4
            hb(seq, False)
        if self.conn.queue.queue[0]["event"] == "worker_status_request":
            self.conn.queue.get_nowait()
        self.assertEqual(self.take()["attempt"], 1)
        print(
            "history 1..3 replayed, missed busy reset idle clock, fresh uninterrupted 60s triggers first request"
        )

    def test_limits_raw_and_idempotent_dispatch(self):
        q = self.first()
        self.assertEqual(self.report(q, summary="甲" * 201)[0], 400)
        self.assertEqual(self.report(q, request_id=str(uuid.uuid4()))[0], 409)
        self.assertTrue(self.saved()["in_running_tasks"])
        self.assertEqual(self.report(q, summary="甲" * 200)[0], 200)
        body = {
            "worker": "test-worker",
            "goal": "dedup",
            "message_id": str(uuid.uuid4()),
        }
        replies = [self.post("/tasks", body)[1] for _ in range(2)]
        self.assertEqual(replies[0]["task_id"], replies[1]["task_id"])
        self.assertTrue(replies[1]["duplicate"])
        self.assertEqual(self.post("/tasks", {**body, "goal": "conflict"})[0], 409)

    def test_config_changes_live_thresholds(self):
        from summary_requests import SummaryRequests

        p = Path(self.store.path).with_suffix(".json")
        p.write_text(
            json.dumps(
                {"idle_task_end_minutes": 0.5, "summary_request_max_attempts": 2}
            )
        )
        api = self.api.lifecycle = SummaryRequests(self.api, p)
        api.clock = lambda: self.now
        self.beat()
        self.advance(29)
        self.assertTrue(self.conn.queue.empty())
        self.advance(1)
        self.assertEqual(self.take()["attempt"], 1)
        self.advance(30)
        second = self.take()
        self.assertEqual(second["attempt"], 2)
        self.advance(30)
        self.assertTrue(self.conn.queue.empty())
        self.assertTrue(self.saved()["in_running_tasks"])
        self.assertEqual([e["kind"] for e in self.events], ["task.summary_missing"])
        self.assertEqual(self.report(second)[0], 200)
        self.assertTrue(self.saved()["deliverable"])
        print(
            "config idle_task_end_minutes=0.5 -> 30s; summary_request_max_attempts=2 -> exactly 2 requests"
        )

    def test_restart_preserves_pending_summary_and_archived_result(self):
        q = self.first()
        db = router.RouterStore(self.store.path)
        self.addCleanup(db.close)
        self.assertEqual(db.orders.get(self.task)["probe_id"], q["request_id"])
        self.assertEqual(db.orders.held("test-worker"), self.task)
        qid = self.post("/tasks", {"worker": "test-worker", "goal": "FIFO on restart"})[
            1
        ]["task_id"]
        self.assertEqual(db.orders.next_dispatch("test-worker"), self.task)
        self.assertEqual(self.report(q)[0], 200)
        self.assertEqual(
            db.orders.get(self.task)["deliverable"]["summary"], "跑完三项检查"
        )
        self.assertEqual(db.orders.next_dispatch("test-worker"), qid)

    def test_router_shutdown_preserves_unanswered_cancel_without_offline_alert(self):
        self.beat()
        self.assertEqual(self.post("/tasks/" + self.task + "/cancel", {})[0], 202)
        self.assertEqual(self.take()["purpose"], "cancel")
        self.api.shutdown()
        self.api.server_close()
        self.api.detach(self.conn)
        self.api.lifecycle.sweep()
        task = self.store.orders.get(self.task)
        self.assertTrue(task["in_running_tasks"])
        self.assertIsNone(task["archive"])
        self.assertEqual(self.events, [])

    def test_unavailable_trigger_replays_only_committed_package(self):
        q = self.first()

        def offline(payload):
            return False, {"error": "unavailable"}

        trigger = self.api.notifier.client
        trigger.post_trigger.side_effect = offline
        self.assertEqual(self.report(q)[0], 200)
        self.assertFalse(self.saved()["in_running_tasks"])
        trigger.post_trigger.side_effect = lambda payload: (
            self.events.append(payload) or True,
            {"id": 1},
        )
        self.api.lifecycle.sweep()
        self.api.lifecycle.sweep()
        self.assertEqual([e["kind"] for e in self.events], ["task.deliverable"])
        self.assertEqual(self.report(q)[1]["duplicate"], True)

    def test_legacy_table_read_without_writes_and_two_live_states_derived_from_facts(
        self,
    ):
        import sqlite3

        p = Path(self.store.path).with_name("legacy.db")
        with closing(sqlite3.connect(p)) as old, old:
            old.execute(
                "CREATE TABLE tasks(task_id TEXT, worker TEXT, goal TEXT, created_at REAL, state TEXT, result_json TEXT)"
            )
            old.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?)",
                ("t-legacy", "w", "old", 1, "obsolete", '{"summary":"old receipt"}'),
            )
        db = router.RouterStore(str(p))
        self.addCleanup(db.close)
        legacy = db.orders.listing()[0]
        self.assertFalse(legacy["in_running_tasks"])
        self.assertNotIn("state", legacy)
        self.assertEqual(legacy["historical_summary"], "old receipt")
        self.assertEqual(
            db._conn.execute("SELECT state FROM tasks").fetchone()[0], "obsolete"
        )
        task = self.saved()
        self.assertEqual(
            set(task["task_info"]), {"TaskID", "TaskName", "TaskDesc", "TaskOwner"}
        )
        self.assertEqual(task["state"], "running")
        self.assertFalse({"status", "bRunning", "review_state"} & task.keys())
        cols = [
            r["name"] for r in self.store._conn.execute("PRAGMA table_info(task_info)")
        ]
        self.assertFalse({"state", "status", "bRunning"} & set(cols))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute("UPDATE task_info SET owner='hermes'")
        self.assertEqual(
            self.post("/tasks/" + self.task + "/verdict", {"status": "done"})[0], 404
        )
        print(
            "TaskInfo fields=TaskID,TaskName,TaskDesc,TaskOwner; state=running (derived); verdict HTTP 404; historical row unchanged"
        )

    def test_idle_requires_fresh_samples(self):
        self.beat()
        self.now += 65
        self.api.lifecycle.sweep()
        self.assertTrue(self.conn.queue.empty())
        self.beat()
        self.advance(55)
        self.assertTrue(self.conn.queue.empty())
        self.advance(5)
        self.assertEqual(self.take()["attempt"], 1)

    def test_approval_progress_survives_worker_archive(self):
        self.beat()
        _, resp = self.post(
            "/approvals",
            {
                "worker": "test-worker",
                "task_id": self.task,
                "kind": "command",
                "title": "review",
                "ttl_s": 60,
                "auto": False,
            },
        )
        aid = resp["approval_id"]
        self.assertEqual(self.saved()["progress"]["approval_id"], aid)
        self.assertTrue(self.saved()["progress"]["waiting_for_approval"])
        self.post("/tasks/" + self.task + "/cancel", {})
        self.report(self.take(), action="cancel")
        self.assertFalse(self.saved()["progress"]["waiting_for_approval"])
        self.assertEqual(self.saved()["progress"]["state"], "denied")

    def test_peer_ack_retry_budget_and_late_ack(self):
        self.post("/register", {"name": "sender"})
        mid = str(uuid.uuid4())
        _, receipt = self.post(
            "/messages",
            {
                "from": "sender",
                "to": "test-worker",
                "message_id": mid,
                "text": "ack test",
            },
        )
        start = receipt["message"]["created_at"]
        self.assertEqual(self.conn.queue.get_nowait()["data"]["message_id"], mid)
        for attempt in range(1, 4):
            self.api.sweep_peer_acks(start + 30 * attempt)
            frame = self.conn.queue.get_nowait()["data"]
            self.assertEqual(
                (frame["message_id"], frame["ack_attempt"]), (mid, attempt)
            )
            self.api.sweep_peer_acks(start + 30 * attempt)
            self.assertTrue(self.conn.queue.empty())
        self.api.sweep_peer_acks(start + 120)
        self.assertEqual(self.store.get_peer_message(mid)["status"], "abandoned")
        self.assertEqual(
            self.post("/messages/" + mid + "/ack", {"worker": "test-worker"})[0], 409
        )


if __name__ == "__main__":
    unittest.main()
