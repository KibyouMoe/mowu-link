"""Real isolated HTTP tests for idle settlement; production state stays untouched."""

import json
import os
import queue
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import server as router
from selftest_router import http


class IdleSettlementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="router-idle-settlement-")
        self.database = str(Path(self.temporary.name) / "router.db")
        self.sent = []
        self.questions = {}
        self.requests = []
        self.open()

    def open(self):
        self.store = router.RouterStore(self.database)
        client = mock.Mock()
        client.post_trigger.side_effect = lambda payload: (self.sent.append(payload) or True, {"id": len(self.sent)})
        self.server = router.RouterServer(("127.0.0.1", 0), self.store, "isolated-token",
                                         router.TriggerNotifier(client, self.store), mock.Mock())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.worker = "idle-test-worker"
        self.call("/register", {"name": self.worker, "control_sse": True})
        self.conn = self.server.attach(self.worker)
        self.stopping = threading.Event()
        self.reader = threading.Thread(target=self.respond, daemon=True)
        self.reader.start()

    def respond(self):
        while not self.stopping.is_set():
            try:
                event = self.conn.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if event is None:
                continue
            if event["event"] != "question_request":
                if event["event"] == "task_result_check":
                    self.requests.append("unexpected_native_gate")
                continue
            data = event["data"]
            self.requests.append(data)
            question = self.questions[data["question_id"]]
            if data["operation"] == "answer":
                question["status"] = "answered"
            self.call("/questions/responses", {"worker": self.worker, "request_id": data["request_id"],
                                               "status": 200, "body": question})

    def close(self):
        self.stopping.set()
        self.reader.join(2)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.store.close()

    def tearDown(self):
        self.close()
        self.temporary.cleanup()

    def call(self, route, body=None, method="POST"):
        return http(method, self.base + route, body, self.server.token)

    def heartbeat(self, state="idle", task_id=None):
        return self.call("/heartbeat", {"name": self.worker, "state": state,
                                       "task_id": task_id, "turn_source": "router" if state != "idle" else None})

    def task(self):
        code, task = self.call("/tasks", {"worker": self.worker, "goal": "isolated idle settlement"})
        self.assertEqual(code, 200)
        self.assertTrue(task["dispatched"])
        self.heartbeat("running", task["task_id"])
        return task["task_id"]

    def report(self, task, **fields):
        return self.call("/result", {"name": self.worker, "task_id": task, "status": "done",
                                     "summary": "reported business success", **fields})

    def notices(self):
        return [item for item in self.sent if item["kind"] == "task.result"]

    def question(self, task):
        question_id = str(uuid.uuid4())
        self.questions[question_id] = {"questionId": question_id, "status": "open",
                                       "recipient": {"kind": "hermes", "taskId": task}}
        code, _ = self.call("/notice", {"worker": self.worker, "kind": "worker.question",
            "text": "提问，请 GET http://unused.invalid/questions/%s/%s" % (self.worker, question_id)})
        self.assertEqual(code, 200)
        return question_id

    def test_01_result_then_idle_finishes_and_notifies_once(self):
        task = self.task()
        code, candidate = self.report(task)
        self.assertEqual(code, 202)
        self.assertTrue(candidate["candidate"])
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.assertEqual(self.notices(), [])
        code, beat = self.heartbeat(task_id=task)
        self.assertEqual(code, 200)
        self.assertEqual(beat["settlements"][0]["state"], "done")
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.requests, [])

    def test_02_idle_without_result_is_failed_with_real_http_receipt(self):
        task = self.task()
        code, heartbeat = self.heartbeat(task_id=task)
        get_code, view = self.call("/tasks/" + task, method="GET")
        result = view["task"]["result"]
        self.assertEqual((code, get_code, view["state"]), (200, 200, "failed"))
        self.assertEqual(result["error_code"], "worker_idle_without_result")
        self.assertEqual(len(self.notices()), 1)
        receipt = {"heartbeat_http": code, "heartbeat": heartbeat, "task_http": get_code,
                   "task_id": task, "state": view["state"], "result": result,
                   "notification_count": len(self.notices())}
        if os.environ.get("ROUTER_TEST_IDLE_RECEIPT"):
            Path(os.environ["ROUTER_TEST_IDLE_RECEIPT"]).write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("IDLE_WITHOUT_RESULT_RECEIPT " + json.dumps(receipt, ensure_ascii=False), flush=True)

    def test_03_unanswered_question_defers_until_answer_and_next_idle(self):
        task = self.task()
        question = self.question(task)
        self.report(task)
        _, beat = self.heartbeat(task_id=task)
        self.assertEqual(beat["settlements"], [])
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.assertEqual(self.notices(), [])
        code, _ = self.call("/questions/%s/%s/answer" % (self.worker, question), {"actor": "hermes", "answers": ["yes"]})
        self.assertEqual(code, 200)
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.assertEqual(len(self.notices()), 1)

    def test_04_repeated_idle_does_not_repeat_settlement_or_notification(self):
        task = self.task()
        self.report(task)
        self.heartbeat(task_id=task)
        finished_at = self.store.get_task(task)["finished_at"]
        for _ in range(3):
            _, beat = self.heartbeat(task_id=task)
            self.assertEqual(beat["settlements"], [])
        self.assertEqual(self.store.get_task(task)["finished_at"], finished_at)
        self.assertEqual(len(self.notices()), 1)
        code, duplicate = self.report(task, status="failed")
        self.assertEqual((code, duplicate["state"], duplicate["duplicate"]), (200, "done", True))

    def test_05_cancelling_idle_finishes_cancelled_even_with_open_question(self):
        task = self.task()
        self.question(task)
        self.store.cancel_task(task, pending=True)
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "cancelled")
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.notices()[0]["source"], "任务取消")
        self.assertEqual(self.notices()[0]["level"], "notify")

    def test_06_old_worker_needs_no_observer_or_candidate_capability(self):
        task = self.task()
        self.call("/register", {"name": self.worker, "control_sse": False, "version": "legacy"})
        self.assertEqual(self.report(task)[0], 202)
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.assertEqual(self.requests, [])

    def test_reported_failure_is_preserved(self):
        task = self.task()
        self.report(task, status="failed", error_code="fixture_error")
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["result"]["error_code"], "fixture_error")
        self.assertEqual(self.store.get_task(task)["state"], "failed")

    def test_needs_inspection_is_done_with_outcome_and_full_summary(self):
        task = self.task()
        full = "完整报告" * 100
        self.report(task, status="failed", outcome="needs_inspection", error_code="native_state_unconfirmed",
                    summary=full, summary_short="未确认，请核对客户端")
        self.heartbeat(task_id=task)
        view = self.store.get_task(task)
        self.assertEqual(view["state"], "done")
        self.assertEqual(view["result"]["outcome"], "needs_inspection")
        self.assertEqual(view["result"]["summary"], full)
        self.assertTrue(self.notices()[0]["text"].endswith("未确认，请核对客户端"))
        self.assertEqual(self.notices()[0]["payload"]["outcome"], "needs_inspection")

    def test_old_generation_and_missing_binding_are_only_consistency_metadata(self):
        task = self.task()
        self.server.result_candidates.put(task, {"candidate": {"status": "done", "summary": "old revision"},
                                                  "generation": "old", "attachments": []})
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.assertFalse(self.store.get_task(task)["result"]["completion"]["generation_matches"])

    def test_reconnect_accepts_a_fresh_idle_pulse_after_a_question_was_answered(self):
        task = self.task()
        question = self.question(task)
        self.report(task)
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.questions[question]["status"] = "answered"
        self.conn = self.server.attach(self.worker)
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.assertEqual(len(self.notices()), 1)

    def test_a_question_bound_to_another_task_does_not_exempt_this_one(self):
        older = self.store.create_task(self.worker, "older", state="failed")["task_id"]
        self.question(older)
        task = self.task()
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["result"]["error_code"], "worker_idle_without_result")

    def test_candidate_and_question_survive_router_restart(self):
        task = self.task()
        question = self.question(task)
        self.report(task)
        self.close()
        self.open()
        self.heartbeat("running", task)
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.questions[question]["status"] = "answered"  # Native/Feishu answer, outside Router's answer endpoint.
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.assertIsNone(self.server.result_candidates.get(task))

    def test_idle_before_result_wins_and_late_notice_cannot_change_terminal_kind(self):
        task = self.task()
        self.heartbeat(task_id=task)
        code, duplicate = self.report(task)
        self.assertEqual((code, duplicate["state"], duplicate["duplicate"]), (200, "failed", True))
        self.call("/notice", {"worker": self.worker, "kind": "task.result", "task_id": task,
                              "status": "done", "text": "late success"})
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.notices()[0]["source"], "任务失败")

    def test_result_and_idle_race_share_one_terminal_winner(self):
        task = self.task()
        barrier = threading.Barrier(2)
        def result():
            barrier.wait()
            return self.report(task)
        def idle():
            barrier.wait()
            return self.heartbeat(task_id=task)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(result)
            second = pool.submit(idle)
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertIn(self.store.get_task(task)["state"], ("done", "failed"))
        self.assertEqual(len(self.notices()), 1)
        self.assertTrue(self.report(task)[1]["duplicate"])

    def test_repeated_idle_pulse_cannot_finish_the_next_queued_task(self):
        task = self.task()
        queued = self.store.create_task(self.worker, "previously accepted next task")
        self.report(task)
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(queued["task_id"])["state"], "running")
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(queued["task_id"])["state"], "running")
        self.heartbeat("running", queued["task_id"])
        self.heartbeat(task_id=queued["task_id"])
        self.assertEqual(self.store.get_task(queued["task_id"])["result"]["error_code"], "worker_idle_without_result")

    def test_invalid_large_summary_does_not_create_candidate(self):
        task = self.task()
        code, error = self.report(task, summary="x" * 301)
        self.assertEqual(code, 400)
        self.assertIn("summary_short", error["error"])
        self.assertIsNone(self.server.result_candidates.get(task))

    def test_explicit_notice_and_automatic_result_use_one_exit(self):
        task = self.task()
        code, _ = self.call("/notice", {"worker": self.worker, "kind": "task.result", "task_id": task,
                                       "status": "done", "text": "explicit outcome"})
        self.assertEqual(code, 202)
        self.report(task)
        self.heartbeat(task_id=task)
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.store.get_task(task)["state"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
