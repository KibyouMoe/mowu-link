"""Live HTTP, SSE and SQLite with deterministic time at the production thresholds."""
import json
import queue
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
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
        trigger.post_trigger.side_effect = lambda payload: (self.events.append(payload) or True, {"id": len(self.events)})
        self.api = router.RouterServer(("127.0.0.1", 0), self.store, "fixture",
                                       router.TriggerNotifier(trigger, self.store), Mock(), verdict_token="reviewer")
        self.addCleanup(self.api.server_close)
        thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.api.shutdown)
        self.base = "http://127.0.0.1:%d" % self.api.server_port
        self.now = time.time()
        self.api.watchdog.clock = lambda: self.now
        self.post("/register", {"name":"test-worker"})
        self.conn = self.api.attach("test-worker")
        self.task = self.post("/tasks", {"worker":"test-worker","goal":"验收"})[1]["task_id"]
        self.assertEqual(self.conn.queue.get_nowait()["event"], "task")

    def post(self, path, body):
        return http("POST", self.base + path, body, "fixture")

    def beat(self, state="idle", **kw):
        code, _ = self.post("/heartbeat", {"name":"test-worker", "state":state, **kw})
        self.assertEqual(code, 200)

    def advance(self, seconds, state="idle"):
        while seconds:
            step = min(60,seconds)
            seconds -= step
            self.now += step
            self.beat(state)
            self.api.watchdog.sweep()

    def take(self):
        event = self.conn.queue.get_nowait()
        self.assertEqual(event["event"], "deliverable_request")
        return event["data"]

    def first(self):
        self.beat()
        self.advance(119)
        self.assertTrue(self.conn.queue.empty())
        self.advance(1)
        return self.take()

    def report(self, q, **fields):
        return self.post("/tasks/"+self.task+"/deliverable", {
            "worker":"test-worker", "request_id":q["request_id"], "summary":"跑完三项检查",
            "artifacts":["/tmp/report"], "evidence":["git rev-parse: fixture"], "risks":[], **fields})

    def saved(self):
        return http("GET",self.base+"/tasks/"+self.task,token="fixture")[1]["task"]

    def test_01_idle_120s_sends_fixed_prompt(self):
        probe = self.first()
        expected = f"当前 task_id = {self.task}。请在当前回合的最终答复中只写 JSON，插件将代交成果：summary（≤200 字）、artifacts（分支/SHA/文件绝对路径）、evidence（跑过的命令与关键输出）、risks（未完成项与风险）。后三项用字符串数组，无则填 []。私信仅用于额外说明。"
        self.assertEqual(probe["template"],expected)
        self.assertEqual(set(probe), {"template", "task_id", "request_id", "issued_at"})
        uuid.UUID(probe["request_id"])
        watch = http("GET",self.base+"/workers", token="fixture")[1]["workers"][0]["watchdog"]
        self.assertEqual((watch["connected"],watch["state"],watch["task_id"],watch["probe_attempts"]),
                         (True,"idle",self.task,1))
        self.assertGreater(watch["checks"], 1)
        print("① idle=119s frames=0; idle=120s attempt=1:", probe["template"])

    def test_02_result_stores_structured_review_and_only_reviewer_finishes(self):
        q = self.first()
        self.assertEqual(self.report(q,request_id="wrong")[0],409)
        code, reply = self.report(q)
        task = self.saved()
        self.assertEqual((code, reply["state"],task["deliverable"]["summary"],task["finished_at"]),
                         (200,"reviewing","跑完三项检查",None))
        self.assertTrue(self.report(q)[1]["duplicate"])
        self.assertTrue(self.report(q,summary="不同的第二份")[1]["duplicate"])
        self.assertEqual(self.saved()["deliverable"]["summary"], "跑完三项检查")
        self.assertEqual([e["kind"] for e in self.events],["task.deliverable"])
        body = {"status":"done","reason":"复核通过","evidence":["回读原件"]}
        endpoint = "/tasks/"+self.task+"/verdict"
        self.assertEqual(self.post(endpoint,body)[0],403)
        from urllib.request import Request, urlopen
        request = Request(self.base+endpoint,data=json.dumps(body).encode(),
                          headers={"Content-Type":"application/json","X-Router-Token":"fixture","X-Review-Token":"reviewer"})
        for _ in range(2):
            with urlopen(request) as r:
                self.assertEqual(r.status,200)
        self.assertEqual(self.saved()["state"],"done")
        self.assertEqual([e["kind"] for e in self.events],["task.deliverable","task.result"])
        print("②⑰ POST /tasks/{id}/deliverable HTTP 200; summary=跑完三项检查; state=reviewing; verdict -> done")

    def test_03_missing_at_300s_releases_probe_occupancy(self):
        self.beat(busy=False)
        self.advance(120)
        q = self.take()
        self.assertTrue(self.store.get_worker("test-worker")["busy"])
        self.advance(299)
        self.assertEqual(self.events,[])
        self.advance(1)
        self.assertEqual(self.saved()["deliverable_state"],"deliverable_missing")
        self.assertFalse(self.store.get_worker("test-worker")["busy"])
        self.assertEqual(self.store.get_worker("test-worker")["task_id"], self.task)
        self.advance(300)
        self.assertTrue(self.conn.queue.empty())
        self.assertEqual([(e["kind"],e["level"]) for e in self.events],[("task.deliverable_missing","event")])
        self.assertEqual(self.report(q)[1]["state"],"reviewing")
        print("探测 +299s busy=true; +300s deliverable_missing,busy=false; event=1")

    def test_04_concurrent_ticks_and_answers_deduplicate(self):
        self.beat()
        self.advance(60)
        self.now += 60
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.api.watchdog.sweep(), range(16)))
        q = self.take()
        self.assertTrue(self.conn.queue.empty())
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda _: self.report(q),range(12)))
        self.assertEqual(sum(not r[1]["duplicate"] for r in responses),1)
        self.assertEqual(len(self.events),1)
        print("④⑳ 16 concurrent checks -> 1 probe; 12 replies -> 1 stored, 11 duplicate")

    def test_05_busy_restarts_continuous_idle_clock(self):
        self.beat()
        self.advance(119)
        self.beat("idle",busy=True)
        self.advance(240,state="running")
        self.assertTrue(self.conn.queue.empty())
        self.first()
        print("⑤ busy=240s probes=0; fresh idle=119s probes=0; +1s probes=1")

    def test_offline_gap_and_new_connection_require_new_idle(self):
        self.beat()
        self.advance(119)
        self.api.detach(self.conn)
        self.now += 180
        self.api.watchdog.sweep()
        self.assertFalse(self.store.get_worker("test-worker")["watchdog"]["connected"])
        self.conn = self.api.attach("test-worker")
        self.assertEqual(self.conn.queue.get_nowait()["event"], "task")
        self.first()

    def test_unsolicited_result_and_oversized_do_not_close(self):
        self.assertEqual(self.report({"request_id":"absent"})[0],409)
        q = self.first()
        code, rejected = self.report(q, summary="测"*201, summary_short="短稿")
        self.assertEqual((code,rejected["error"]), (400,"deliverable_summary_too_long"))
        self.assertEqual(self.report(q, summary_short="测"*201)[0],400)
        self.assertIsNone(self.saved()["deliverable"])
        self.assertEqual(self.saved()["state"],"running")
        self.assertEqual(self.report(q, summary="测"*200)[1]["state"],"reviewing")
        print("⑱ 201 字 -> HTTP 400 deliverable_summary_too_long; 同一 request_id 重写 200 字 -> reviewing")

    def test_wrong_id_worker_peer_and_result_do_not_submit(self):
        probe = self.first()
        code, resp = self.report(probe, request_id=str(uuid.uuid4()))
        self.assertEqual((code,resp["error"]), (409,"delivery_request_mismatch"))
        self.assertEqual(self.report(probe, worker="other")[0],400)
        text = json.dumps({"task_id":self.task,"summary":"相同文字","artifacts":[],"evidence":[],"risks":[]})
        code, peer = self.post("/messages", {"message_id":str(uuid.uuid4()), "from":"test-worker",
                             "to":"Hermes", "task_id":self.task, "text":text})
        self.assertEqual(code,200,peer)
        self.assertEqual(peer["message"]["task_id"],self.task)
        self.assertEqual(self.post("/result", {"task_id":self.task, "name":"test-worker","summary":text})[0],409)
        self.assertIsNone(self.saved()["deliverable"])
        self.assertEqual(self.saved()["state"],"running")
        self.assertFalse(any(e["kind"] == "task.deliverable" for e in self.events))
        print("⑲ wrong request_id -> HTTP 409 delivery_request_mismatch; peer -> linked; deliverable=null; state=running")

    def test_restart_preserves_request_identity_and_first_accepted_report(self):
        original = self.first()
        self.api.detach(self.conn)
        db_path = self.store.path
        restored = router.RouterStore(db_path)
        self.addCleanup(restored.close)
        restored.reset_runtime()
        restored_api = router.RouterServer(("127.0.0.1", 0), restored, "fixture", self.api.notifier, Mock())
        self.addCleanup(restored_api.server_close)
        restored_api.watchdog.clock = lambda: self.now
        conn = restored_api.attach("test-worker")
        self.now += 300
        restored_api.watchdog.observe(conn, "idle")
        self.assertTrue(conn.queue.empty())
        self.assertEqual(restored.get_task(self.task)["deliverable_state"], "deliverable_missing")
        body = {"worker":"test-worker", "request_id":original["request_id"], "summary":"首次提交",
                "artifacts":[], "evidence":["回读"], "risks":[]}
        self.assertEqual(restored_api.reviews.receive(self.task,body)[1]["state"], "reviewing")
        _, duplicate = self.api.reviews.receive(self.task, {**body, "summary":"重连后的另一份"})
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(restored.get_task(self.task)["deliverable"]["summary"],"首次提交")
        self.assertEqual([e["kind"] for e in self.events],["task.deliverable_missing","task.deliverable"])

    def test_peer_ack_retry_budget_and_late_ack(self):
        self.post("/register", {"name":"sender"})
        self.api.attach("sender")
        mid = str(uuid.uuid4())
        _, receipt = self.post("/messages", {"from":"sender", "to":"test-worker", "message_id":mid, "text":"ack test"})
        self.post("/heartbeat", {"name":"sender", "state":"idle", "busy":False})
        self.assertTrue(self.store.get_worker("sender")["busy"])
        self.assertEqual(self.post("/tasks", {"worker":"sender", "goal":"隔离验证"})[0], 409)
        start = receipt["message"]["created_at"]
        self.assertEqual(self.conn.queue.get_nowait()["data"]["message_id"],mid)
        for attempt in range(1,4):
            self.api.sweep_peer_acks(start + 30*attempt)
            frame = self.conn.queue.get_nowait()["data"]
            self.assertEqual((frame["message_id"],frame["ack_attempt"]), (mid,attempt))
            self.api.sweep_peer_acks(start+30*attempt)
            self.assertTrue(self.conn.queue.empty())
        self.api.sweep_peer_acks(start+120)
        self.assertEqual(self.store.get_peer_message(mid)["status"], "abandoned")
        self.assertEqual(self.store.peer_messages("test-worker")[0],[])
        self.assertFalse(self.store.get_worker("sender")["busy"])
        self.assertEqual(self.post("/tasks", {"worker":"sender", "goal":"恢复派单"})[0], 200)
        self.assertEqual(self.post("/messages/"+mid+"/ack", {"worker":"test-worker"})[0],409)
        self.assertIsNone(self.store.ack_peer_message(mid)["acked_at"])
        print("私信 +30/+60/+90s 各重发一次; +120s abandoned; pending=0")

    def test_busy_retry_is_suppressed_and_timeout_still_alerts(self):
        self.first()
        self.advance(600, state="running")
        self.assertTrue(self.conn.queue.empty())
        self.assertEqual(self.saved()["probe_attempts"],1)
        self.assertEqual(self.events[0]["level"],"event")
