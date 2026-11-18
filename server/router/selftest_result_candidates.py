"""Real isolated HTTP tests for idle settlement; production state stays untouched."""

import json
import queue
import tempfile
import threading
import unittest
import uuid
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

    def test_legacy_proofs_cannot_settle_without_a_probe(self):
        task = self.task()
        anchor = fixture_binding(self.store.get_task(task))
        for proof in (None, fixture_end(anchor), fixture_end({**anchor,"turn_id":"other"})):
            code, reply = self.report(task,native_completion=proof)
            self.assertEqual((code,reply["error"],reply["state"]), (409,"deliverable_request_required","running"))
        self.assertEqual(self.notices(),[])

    def test_bound_turn_and_ended_goal_do_not_replace_human_verdict(self):
        task = self.task()
        anchor = fixture_binding(self.store.get_task(task))
        self.assertEqual(self.call("/tasks/"+task+"/turn-binding",
                         {"name":self.worker,"connection_generation":self.conn.generation,**anchor})[0],200)
        self.heartbeat()
        for goal in ("active","paused","completed",None):
            self.assertEqual(self.report(task,goal_status=goal,native_completion=fixture_end(anchor))[0],409)
        self.assertEqual(self.store.get_task(task)["state"],"running")

    def test_open_question_and_cancellation_preserve_task_state(self):
        task = self.task()
        self.question(task)
        self.store.cancel_task(task,pending=True)
        self.heartbeat()
        self.assertEqual(self.report(task,status="failed")[0],409)
        self.assertEqual(self.store.get_task(task)["state"],"cancelling")
        self.assertEqual(self.notices(),[])

    def test_legacy_receipts_cannot_create_probe_for_another_worker(self):
        task = self.task()
        self.assertEqual(self.report(task,name="other")[0],400)
        self.assertEqual(self.report("t-missing")[0],404)
        self.assertIsNone(self.store.get_task(task)["probe_id"])

    def test_explicit_terminal_notice_cannot_bypass_probe(self):
        task = self.task()
        code, reply = self.call("/notice",{"worker":self.worker,"task_id":task,"kind":"task.result",
                                          "status":"failed","text":"early"})
        self.assertEqual((code,reply["error"]), (409,"probe_response_required"))
        self.assertEqual(self.store.get_task(task)["state"],"running")
        self.assertEqual(self.notices(),[])

    def test_candidate_file_from_previous_version_does_not_finish_after_restart(self):
        task = self.task()
        self.server.result_candidates.put(task,{"candidate":{"status":"done","summary":"old"}})
        self.close()
        self.open()
        self.heartbeat()
        self.assertEqual(self.store.get_task(task)["state"],"running")
        self.assertIsNone(self.store.get_task(task)["deliverable"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
