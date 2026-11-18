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
from selftest_router import fixture_binding, fixture_end, http


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
        self.conn = self.server.attach(self.worker, native_settlement=True)
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
                                     "summary": "reported business success", "connection_generation": self.conn.generation, **fields})

    def finish(self, task, **fields):
        binding = fixture_binding(self.store.get_task(task))
        code, _ = self.call("/tasks/" + task + "/turn-binding", {"name": self.worker, "connection_generation": self.conn.generation, **binding})
        self.assertEqual(code, 200)
        saved = self.server.result_candidates.get(task)
        candidate = dict(saved["result"]["candidate"]) if saved else {}
        candidate.update(fields)
        return self.report(task, **{**candidate, "native_completion": fixture_end(binding)})

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

    def test_01_bound_native_turn_finishes_after_early_report(self):
        task = self.task()
        self.assertEqual(self.report(task)[0], 202)
        self.assertEqual(self.heartbeat()[1]["settlements"], [])
        anchor = fixture_binding(self.store.get_task(task))
        path = "/tasks/" + task + "/turn-binding"
        self.assertEqual(self.call(path, {"name": self.worker, "connection_generation": self.conn.generation, **anchor})[0], 200)
        self.assertEqual(self.call(path, {"name": self.worker, "connection_generation": self.conn.generation, **anchor, "turn_id": "unrelated"})[0], 409)
        for key in anchor:
            wrong = fixture_end({**anchor, key: "unrelated"})
            self.assertEqual(self.report(task, native_completion=wrong)[0], 409)
        for wrong in (anchor, {**fixture_end(anchor), "native_status": "inProgress"},
                      {**fixture_end(anchor), "cleanup_complete": False}):
            self.assertEqual(self.report(task, native_completion=wrong)[0], 409)
        self.assertEqual(self.notices(), [])
        code, outcome = self.finish(task)
        self.assertEqual((code, outcome["state"], len(self.notices())), (200, "done", 1))
        print("bound thread+turn+epoch, native ended -> HTTP 200 done; notifications=1", flush=True)

    def test_02_idle_without_result_keeps_a_nonterminal_inspection(self):
        task = self.task()
        self.heartbeat()
        self.assertEqual(self.store.get_task(task)["result"]["outcome"], "needs_inspection")
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.assertEqual(self.notices(), [])

    def test_03_unanswered_question_defers_until_answer_and_next_idle(self):
        task = self.task()
        question = self.question(task)
        self.report(task)
        _, beat = self.heartbeat(task_id=task)
        self.assertEqual(beat["settlements"], [])
        self.assertEqual(self.finish(task)[0], 202)
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.assertEqual(self.notices(), [])
        code, _ = self.call("/questions/%s/%s/answer" % (self.worker, question), {"actor": "hermes", "answers": ["yes"]})
        self.assertEqual(code, 200)
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.finish(task)
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.assertEqual(len(self.notices()), 1)

    def test_04_repeated_idle_does_not_repeat_settlement_or_notification(self):
        task = self.task()
        self.report(task)
        self.heartbeat(task_id=task)
        self.finish(task)
        finished_at = self.store.get_task(task)["finished_at"]
        for _ in range(3):
            _, beat = self.heartbeat(task_id=task)
            self.assertEqual(beat["settlements"], [])
        self.assertEqual(self.store.get_task(task)["finished_at"], finished_at)
        self.assertEqual(len(self.notices()), 1)
        code, duplicate = self.report(task, status="failed")
        self.assertEqual((code, duplicate["state"], duplicate["duplicate"]), (200, "done", True))

    def test_05_cancelling_idle_keeps_the_slot_with_or_without_open_question(self):
        task = self.task()
        self.question(task)
        self.store.cancel_task(task, pending=True)
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "cancelling")
        self.assertEqual(self.notices(), [])

    def test_06_old_worker_result_is_saved_without_observer(self):
        task = self.task()
        self.call("/register", {"name": self.worker, "control_sse": False, "version": "legacy"})
        self.assertEqual(self.report(task)[0], 202)
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.assertEqual(self.requests, [])

    def test_legacy_task_scoped_idle_and_explicit_result_both_orders(self):
        self.conn.native_settlement = False
        self.call("/register", {"name": self.worker, "control_sse": False})
        for idle_first in (False, True):
            task = self.task()
            if idle_first:
                self.heartbeat()
            code, _ = self.call("/result", {"name": self.worker, "task_id": task,
                                             "summary": "legacy business report", "status": "done",
                                             "evidence": [{"type": "codex_thread", "thread_id": "legacy", "turn_id": task}]})
            self.assertEqual(code, 200 if idle_first else 202)
            self.heartbeat()
            result = self.store.get_task(task)
            self.assertEqual(result["state"], "done")
            self.assertEqual(result["result"]["completion"]["signal"], "legacy_task_idle")
        self.assertEqual(len(self.notices()), 2)

    def test_legacy_idle_without_native_receipt_stays_pending(self):
        self.conn.native_settlement = False
        self.call("/register", {"name": self.worker, "control_sse": False})
        task = self.task()
        self.heartbeat()
        code, _ = self.call("/result", {"name": self.worker, "task_id": task, "status": "done", "summary": "early"})
        self.assertEqual((code, self.store.get_task(task)["state"], self.notices()), (202, "running", []))

    def test_legacy_observer_confirms_task_on_original_stream(self):
        self.conn.native_settlement = False
        task = self.task()
        def legacy_answer(worker, event, data, kind, timeout):
            self.assertEqual((event, kind, data["task_id"]), ("task_result_check", "completion", task))
            return 200, {"ready": True, "generation": self.conn.generation, "task_id": task,
                         "cleanup_complete": True, "followups_pending": False, "thread_id": "legacy-thread",
                         "turn_id": "tail", "turn_ids": ["root", "tail"], "native_status": "completed",
                         "result": data["candidate"]}
        with mock.patch.object(self.server, "worker_request", side_effect=legacy_answer):
            code, result = self.call("/result", {"name": self.worker, "task_id": task, "summary": "legacy", "status": "done"})
        self.assertEqual((code, result["state"], len(self.notices())), (200, "done", 1))
        view = self.store.get_task(task)
        self.assertEqual(view["turn_binding"], {"thread_id": "legacy-thread", "turn_id": "root",
                                                "execution_epoch": view["execution_epoch"]})

    def test_reported_failure_is_preserved(self):
        task = self.task()
        self.report(task, status="failed", error_code="fixture_error")
        self.heartbeat(task_id=task)
        self.finish(task)
        self.assertEqual(self.store.get_task(task)["result"]["error_code"], "fixture_error")
        self.assertEqual(self.store.get_task(task)["state"], "failed")

    def test_needs_inspection_is_done_with_outcome_and_full_summary(self):
        task = self.task()
        full = "完整报告" * 100
        self.report(task, status="failed", outcome="needs_inspection", error_code="native_state_unconfirmed",
                    summary=full, summary_short="未确认，请核对客户端")
        self.heartbeat(task_id=task)
        self.finish(task)
        view = self.store.get_task(task)
        self.assertEqual(view["state"], "done")
        self.assertEqual(view["result"]["outcome"], "needs_inspection")
        self.assertEqual(view["result"]["summary"], full)
        self.assertTrue(self.notices()[0]["text"].endswith("未确认，请核对客户端"))
        self.assertEqual(self.notices()[0]["payload"]["outcome"], "needs_inspection")

    def test_old_generation_and_missing_binding_cannot_finish_task(self):
        task = self.task()
        self.server.result_candidates.put(task, {"candidate": {"status": "done", "summary": "old revision"},
                                                  "generation": "old", "attachments": []})
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.assertEqual(len(self.notices()), 0)

    def test_reconnect_accepts_a_fresh_idle_pulse_after_a_question_was_answered(self):
        task = self.task()
        question = self.question(task)
        self.report(task)
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(task)["state"], "running")
        self.questions[question]["status"] = "answered"
        self.conn = self.server.attach(self.worker, native_settlement=True)
        self.finish(task)
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.assertEqual(len(self.notices()), 1)

    def test_a_question_bound_to_another_task_does_not_exempt_this_one(self):
        older = self.store.create_task(self.worker, "older", state="failed")["task_id"]
        self.question(older)
        task = self.task()
        self.heartbeat(task_id=task)
        self.assertEqual(self.finish(task)[1]["state"], "done")

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
        self.finish(task)
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.assertIsNone(self.server.result_candidates.get(task))

    def test_idle_before_report_preserves_late_business_result(self):
        task = self.task()
        self.heartbeat(task_id=task)
        self.assertEqual(self.report(task)[0], 202)
        self.assertEqual(self.finish(task)[1]["state"], "done")
        self.assertTrue(self.report(task)[1]["duplicate"])
        self.assertEqual(len(self.notices()), 1)

    def test_result_and_idle_race_share_one_terminal_winner(self):
        task = self.task()
        barrier = threading.Barrier(2)
        def result():
            barrier.wait()
            return self.finish(task)
        def idle():
            barrier.wait()
            return self.heartbeat(task_id=task)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(result)
            second = pool.submit(idle)
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.assertEqual(len(self.notices()), 1)
        self.assertTrue(self.report(task)[1]["duplicate"])

    def test_repeated_idle_pulse_cannot_finish_the_next_queued_task(self):
        task = self.task()
        queued = self.store.create_task(self.worker, "previously accepted next task")
        self.report(task)
        self.finish(task)
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(queued["task_id"])["state"], "running")
        self.heartbeat(task_id=task)
        self.assertEqual(self.store.get_task(queued["task_id"])["state"], "running")
        self.heartbeat("running", queued["task_id"])
        self.heartbeat(task_id=queued["task_id"])
        self.assertEqual(self.store.get_task(queued["task_id"])["state"], "running")

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
        self.finish(task)
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.store.get_task(task)["state"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
