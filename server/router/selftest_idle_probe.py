"""Live HTTP, SSE and SQLite with deterministic time at the production thresholds."""

import json
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
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
        for attempt in (1, 2, 3):
            self.advance(60)
            q = self.take()
            self.assertEqual((q["purpose"], q["attempt"]), ("cancel", attempt))
        self.advance(60)
        self.assertEqual(
            self.saved()["archive"]["reason"], "cancel_greetings_unanswered"
        )
        self.assertEqual(
            [e["kind"] for e in self.events],
            ["task.summary_missing", "task.offline_archive"],
        )
        print(
            "summary attempts=1,2,3; summary_missing event=1; cancel attempts=1,2,3; archived without worker confirmation; offline_archive event=1"
        )

    def test_online_cancel_requires_worker_and_offline_reconciliation(self):
        route = "/tasks/" + self.task + "/cancel"
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
        for _ in range(3):
            self.beat("busy", TaskID=0)
            self.api.lifecycle.sweep()
        self.assertEqual([e["kind"] for e in self.events], ["worker.anomaly"])
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
            "idle+TaskID anomaly=0; busy+0 repeated 3 times: anomaly events=1; legacy completion notifications=0"
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
        self.assertEqual(self.take()["attempt"], 2)
        self.advance(30)
        self.assertEqual(self.take()["purpose"], "cancel")
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

    def test_legacy_table_read_without_writes_and_no_lifecycle_state_in_schema_or_wire(
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
        self.assertFalse({"state", "status", "bRunning", "review_state"} & task.keys())
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
            "TaskInfo fields=TaskID,TaskName,TaskDesc,TaskOwner; external task state fields=0; verdict HTTP 404; historical row unchanged"
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
