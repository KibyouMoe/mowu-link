"""Real isolated HTTP tests for idle settlement; production state stays untouched."""

import json
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

    def test_idle_without_result_or_native_binding_finishes(self):
        task = self.task()
        self.assertIsNone(self.store.get_task(task)["turn_binding"])
        _, beat = self.heartbeat()
        self.assertEqual(beat["settlements"][0]["state"], "done")
        saved = self.store.get_task(task)
        self.assertIsNone(saved["deliverable_candidate"])
        self.assertEqual((saved["review_state"],saved["deliverable_state"]), ("reviewing","pending"))
        self.assertEqual(len(self.notices()), 1)
        print("⑭ POST /result calls=0; idle -> done; deliverable=pending; terminal notices=1")

    def test_legacy_success_and_failure_are_only_references(self):
        self.conn.native_settlement = False
        task = self.task()
        for status in ("done","failed","turn_ended"):
            code, ack = self.report(task, status=status, evidence=["ref"], summary="参考")
            self.assertEqual((code,ack["ignored_as_reference"],ack["deprecated"],ack["state"]), (200,True,True,"running"))
            self.assertEqual(self.store.get_task(task)["deliverable_candidate"]["evidence"], ["ref"])
            self.assertIsNone(self.store.get_task(task)["result"])
            self.assertTrue(self.report(task,status=status,evidence=["ref"],summary="参考")[1]["duplicate"])
        self.assertEqual((len(self.notices()),len(self.requests)), (0,0))
        print("⑮ legacy done/failed -> HTTP 200 ignored_as_reference=true; state=running; notices=0")

    def test_references_with_stale_or_missing_proof_cannot_settle(self):
        task = self.task()
        anchor = fixture_binding(self.store.get_task(task))
        for proof in (None, fixture_end(anchor), fixture_end({**anchor,"execution_epoch":"stale"})):
            self.assertTrue(self.report(task,native_completion=proof)[1]["ignored_as_reference"])
        self.assertEqual(self.store.get_task(task)["state"],"running")
        self.assertIsNone(self.store.get_task(task)["turn_binding"])

    def test_wrong_owner_cannot_record_a_reference(self):
        task = self.task()
        self.assertEqual(self.report(task,name="other")[0],400)
        self.assertIsNone(self.store.get_task(task)["deliverable_candidate"])
        self.assertEqual(self.report("t-missing")[0],404)

    def test_repeated_idle_and_late_reference_do_not_repeat_notice(self):
        task = self.task()
        self.heartbeat()
        stamp = self.store.get_task(task)["finished_at"]
        self.assertTrue(self.report(task,status="failed")[1]["ignored_as_reference"])
        for _ in range(3):
            self.assertEqual(self.heartbeat()[1]["settlements"],[])
        self.assertEqual(self.store.get_task(task)["state"],"done")
        self.assertEqual(self.store.get_task(task)["finished_at"],stamp)
        self.assertEqual(len(self.notices()),1)

    def test_cancelling_retains_occupancy_despite_reference_and_idle(self):
        task = self.task()
        self.question(task)
        self.store.cancel_task(task,pending=True)
        self.heartbeat()
        self.assertEqual(self.report(task,status="failed")[1]["state"],"cancelling")
        self.assertEqual(self.notices(),[])

    def test_pending_question_does_not_classify_turns(self):
        task = self.task()
        self.question(task)
        self.assertEqual(self.heartbeat("running", task)[1]["settlements"],[])
        self.assertEqual(self.heartbeat()[1]["settlements"][0]["state"],"done")

    def test_offline_pulse_does_not_settle(self):
        task = self.task()
        self.conn.close("test")
        self.heartbeat()
        self.assertEqual(self.store.get_task(task)["state"],"running")
        self.conn = self.server.attach(self.worker)
        self.assertEqual(self.heartbeat()[1]["settlements"][0]["state"],"done")

    def test_native_failed_and_interrupted_are_visible_without_result(self):
        for native in ("failed","interrupted"):
            task = self.task()
            self.call("/heartbeat", {"name":self.worker,"state":"running","native_status":native})
            self.assertEqual(self.store.get_task(task)["state"],"running")
            self.call("/heartbeat", {"name":self.worker,"state":"idle","native_status":native})
            view = self.store.get_task(task)
            self.assertEqual((view["state"],view["result"]["completion"]["native_status"]), ("failed",native))
            self.assertEqual(view["deliverable_state"],"pending")

    def test_new_worker_rejects_old_connection_sample(self):
        task = self.task()
        self.conn = self.server.attach(self.worker,state_layers=True)
        self.assertEqual(self.call("/heartbeat", {"name":self.worker,"state":"idle","connection_generation":"stale"})[0],409)
        self.assertEqual(self.store.get_task(task)["state"],"running")

    def test_reference_and_question_survive_restart(self):
        task = self.task()
        self.question(task)
        self.report(task, summary="durable")
        self.close()
        self.open()
        self.assertEqual(self.store.get_task(task)["deliverable_candidate"]["summary"],"durable")
        self.assertEqual(self.store.pending_questions(self.worker)[0]["task_id"],task)
        self.heartbeat()
        self.assertEqual(self.store.get_task(task)["state"],"done")

    def test_staged_deliverable_survives_restart_and_needs_fresh_online_idle(self):
        task = self.task()
        self.heartbeat()
        request_id = self.server.reviews.load(task)["request_id"]
        report = {"summary":"重启前已保存","artifacts":[],"evidence":[],"risks":[]}
        code, _ = self.call("/tasks/" + task + "/deliverable",
                            {"name": self.worker, "request_id": request_id, "deliverable": report})
        self.assertEqual(code, 202)
        self.close()
        self.open()
        self.heartbeat("running")
        self.assertIsNone(self.store.get_task(task)["deliverable"])
        self.heartbeat()
        self.assertEqual(self.store.get_task(task)["deliverable"], report)
        self.assertEqual(sum(item["kind"] == "task.deliverable" for item in self.sent), 1)

    def test_full_legacy_summary_and_outcome_remain_reference_only(self):
        task = self.task()
        full = "正文" * 230
        self.report(task,summary=full,summary_short="未确认",outcome="needs_inspection")
        self.heartbeat()
        view = self.store.get_task(task)
        self.assertEqual(view["deliverable_candidate"]["summary"],full)
        self.assertEqual(view["deliverable_candidate"]["outcome"],"needs_inspection")
        self.assertNotIn("outcome",view["result"])
        self.assertIsNone(view["deliverable"])

    def test_reference_and_concurrent_idle_have_one_terminal_winner(self):
        task = self.task()
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(self.report,task),pool.submit(self.heartbeat),pool.submit(self.heartbeat)]
            replies = [j.result(timeout=5) for j in jobs]
        self.assertTrue(replies[0][1]["ignored_as_reference"])
        self.assertEqual(sum(len(r[1]["settlements"]) for r in replies[1:]),1)
        self.assertEqual(len(self.notices()),1)
        self.assertTrue(self.report(task)[1]["duplicate"])

    def test_replayed_finished_epoch_cannot_finish_next_task(self):
        task = self.task()
        epoch = self.store.get_task(task)["execution_epoch"]
        queued = self.store.create_task(self.worker,"next")["task_id"]
        pulse = {"name":self.worker,"state":"idle","finished_execution_epoch":epoch}
        self.call("/heartbeat",pulse)
        self.assertEqual(self.store.get_task(queued)["state"],"running")
        self.assertEqual(self.call("/heartbeat",pulse)[1]["settlements"],[])
        self.assertEqual(self.heartbeat()[1]["settlements"][0]["task_id"],queued)

    def test_invalid_large_summary_does_not_create_reference(self):
        task = self.task()
        code, error = self.report(task, summary="x" * 301)
        self.assertEqual(code,400)
        self.assertIn("summary_short",error["error"])
        self.assertIsNone(self.store.get_task(task)["deliverable_candidate"])

    def test_explicit_notice_and_reference_do_not_announce_completion(self):
        task = self.task()
        code, ack = self.call("/notice", {"worker":self.worker,"kind":"task.result","task_id":task,"text":"参考","status":"done"})
        self.assertEqual((code,ack["ignored_as_reference"],len(self.notices())),(200,True,0))
        self.report(task)
        self.heartbeat()
        self.assertEqual(len(self.notices()),1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
