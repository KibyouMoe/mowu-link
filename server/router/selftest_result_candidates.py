"""Isolated HTTP/SSE-RPC completion gate tests. Never opens production state."""

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import server as router
from selftest_router import completion_proof, http


class ResultGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="router-result-gate-")
        self.database = str(Path(self.temporary.name) / "router.db")
        self.sent = []
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
        self.worker = "test-worker"
        self.store.upsert_worker(self.worker)
        self.conn = self.server.attach(self.worker)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.store.close()

    def tearDown(self):
        self.close()
        self.temporary.cleanup()

    def call(self, route, body=None, method="POST"):
        return http(method, self.base + route, body, self.server.token)

    def task(self):
        return self.store.create_task(self.worker, "real native turn fixture", state="running")["task_id"]

    def candidate(self, task_id, **fields):
        return dict(name=self.worker, task_id=task_id, status="done", summary="delivered report", **fields)

    def request(self, route, body, proof=None, before_reply=None):
        # The real worker receives this event over SSE. Here the test owns that
        # connection's queue and replies through the same authenticated endpoint.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.call, route, body)
            while True:
                event = self.conn.queue.get(timeout=3)
                if event["event"] == "task_result_check":
                    break
            data = event["data"]
            observed = completion_proof(self.worker, data["task_id"], data["generation"], data["candidate"])
            if proof:
                observed.update(proof)
            if before_reply:
                before_reply(data)
            reply = self.call("/result/responses", {"worker": self.worker, "request_id": data["request_id"],
                                                   "status": 200, "body": observed})
            return pending.result(timeout=4), reply

    def test_early_result_and_notice_wait_for_native_and_cleanup_then_notify_once(self):
        task = self.task()
        for route, body in [("/result", self.candidate(task)),
                            ("/notice", dict(worker=self.worker, task_id=task, kind="task.result", status="done", text="same work"))]:
            (code, result), _ = self.request(route, body, {"ready": False, "reason": "native_turn_active"})
            self.assertEqual(code, 202)
            self.assertTrue(result["candidate"])
            self.assertEqual(self.store.get_task(task)["state"], "running")
            self.assertEqual(self.sent, [])
        (code, _), _ = self.request("/result", self.candidate(task), {"cleanup_complete": False})
        self.assertEqual(code, 202)
        (code, _), _ = self.request("/result", self.candidate(task))
        self.assertEqual(code, 200)
        self.assertIsNone(self.server.result_candidates.get(task))
        self.assertEqual(self.store.get_task(task)["state"], "done")
        self.call("/notice", dict(worker=self.worker, task_id=task, kind="task.result", status="done", text="different wording"))
        self.call("/result", self.candidate(task))
        self.assertEqual(len(self.sent), 1)

    def test_restart_keeps_candidate_but_requires_fresh_native_observation(self):
        task = self.task()
        self.request("/result", self.candidate(task), {"ready": False})
        old = self.server.result_candidates.get(task)
        self.close()
        self.open()
        self.assertEqual(self.server.result_candidates.get(task), old)
        code, view = self.call("/tasks/" + task, method="GET")
        self.assertEqual(code, 200)
        self.assertEqual(view["result_candidate"], old)
        self.assertEqual(self.sent, [])
        (code, _), _ = self.request("/result", self.candidate(task))
        self.assertEqual(code, 200)
        self.assertEqual(len(self.sent), 1)

    def test_replaced_connection_cannot_settle_even_with_old_ready_proof(self):
        task = self.task()
        old = self.conn

        def replace(_):
            self.conn = self.server.attach(self.worker)

        (code, _), (reply_code, _) = self.request("/result", self.candidate(task), before_reply=replace)
        self.assertEqual(code, 202)
        self.assertEqual(reply_code, 403)
        self.assertTrue(old.closed)
        self.assertNotIn(self.store.get_task(task)["state"], router.TERMINAL_STATES)
        self.assertEqual(self.sent, [])
        (code, _), _ = self.request("/result", self.candidate(task))
        self.assertEqual(code, 200)

    def test_cancellation_during_observation_uses_original_ack_path(self):
        task = self.task()

        def cancel(_):
            self.store.cancel_task(task, pending=True)

        (code, _), _ = self.request("/result", self.candidate(task), before_reply=cancel)
        self.assertEqual(code, 202)
        self.assertEqual(self.store.get_task(task)["state"], "cancelling")
        self.assertEqual(self.sent, [])
        code, _ = self.call("/tasks/" + task + "/cancel-ack", {"name": self.worker,
            "outcome": {"ok": True, "task_id": task, "state": "cancelled"}})
        self.assertEqual(code, 200)
        self.assertEqual(self.store.get_task(task)["state"], "cancelled")
        self.assertIsNone(self.server.result_candidates.get(task))

    def test_unconfirmed_outcome_never_becomes_done_without_observed_replacement(self):
        task = self.task()
        candidate = self.candidate(task, outcome="needs_inspection", error_code="native_state_unconfirmed")
        (code, _), _ = self.request("/result", candidate)
        self.assertEqual(code, 202)
        self.assertEqual(self.server.result_candidates.get(task)["result"]["candidate"]["outcome"], "needs_inspection")
        self.assertEqual(self.sent, [])
        (code, _), _ = self.request("/result", candidate, {"result": self.candidate(task)})
        self.assertEqual(code, 200)
        self.assertNotIn("outcome", self.store.get_task(task)["result"])

    def test_changed_candidate_revision_rejects_inflight_old_response(self):
        task = self.task()

        def replace(_):
            with self.server.dispatch_lock:
                self.server.result_candidates.put(task, {"candidate": self.candidate(task, outcome="needs_inspection"),
                                                         "attachments": [], "generation": self.conn.generation})

        (code, _), _ = self.request("/result", self.candidate(task), before_reply=replace)
        self.assertEqual(code, 202)
        self.assertEqual(self.sent, [])

    def test_proof_requires_exact_identity_terminal_turn_and_no_followups(self):
        task = self.task()
        for invalid in [{"generation": "old"}, {"task_id": "t-other"}, {"turn_ids": []},
                        {"native_status": "inProgress"}, {"followups_pending": True}, {"cleanup_complete": False},
                        {"result": {**self.candidate(task), "name": "someone-else"}}]:
            with self.subTest(proof=invalid):
                (code, _), _ = self.request("/result", self.candidate(task), invalid)
                self.assertEqual(code, 202)
        self.assertEqual(self.sent, [])

    def test_caller_readiness_flags_are_ignored_and_large_body_needs_short_summary(self):
        task = self.task()
        candidate = self.candidate(task)
        candidate.update(ready=True, cleanup_complete=True, followups_pending=False)
        self.request("/result", candidate, {"ready": False})
        self.assertEqual(self.sent, [])
        candidate.update(summary="完整正文" * 300)
        code, error = self.call("/result", candidate)
        self.assertEqual(code, 400)
        self.assertIn("summary_short", error["error"])
        candidate["summary_short"] = "完整正文见任务"
        (code, _), _ = self.request("/result", candidate)
        self.assertEqual(code, 200)
        self.assertEqual(self.store.get_task(task)["result"]["summary"], candidate["summary"])
        self.assertTrue(self.sent[0]["text"].endswith(candidate["summary_short"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
