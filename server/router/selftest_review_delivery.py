"""Loopback acceptance of mechanical settlement, review, and private deliverables."""
import hashlib
import importlib.util
import json
import sys
import unittest
import uuid
from pathlib import Path
from urllib import error, request
from unittest import mock

import selftest_two_layer_state as layer
from selftest_router import fixture_binding, fixture_end
import task_review


class ReviewTests(unittest.TestCase):
    call = layer.TwoLayerStateTests.call
    close_worker = layer.TwoLayerStateTests.close_worker
    versioned_stream = layer.TwoLayerStateTests.versioned_stream
    stream_event = layer.TwoLayerStateTests.stream_event

    def setUp(self):
        layer.TwoLayerStateTests.setUp(self)
        self.close_worker()
        self.stream, self.gen = self.versioned_stream()
        self.sender = {"name": self.name, "connection_generation": self.gen}
        self.server.verdict_token = "reviewer-fixture"
        self.context = {"card_id": "card-甲", "board_id": "board-乙"}

    def begin(self):
        code, task = self.call("/tasks", {"worker": self.name, "goal": "fixture", "context": self.context})
        self.assertEqual(code, 200, task)
        event, dispatch = self.stream_event(self.stream)
        self.assertEqual(event, "task")
        self.task_id = task["task_id"]
        self.route = "/tasks/" + self.task_id
        self.binding = fixture_binding(dispatch)
        self.assertEqual(self.call(self.route + "/turn-binding", {**self.sender, **self.binding})[0], 200)

    def end(self, wire="turn_ended", **fields):
        return self.call("/result", {**self.sender, "task_id": self.task_id, "status": wire,
                        "native_completion": fixture_end(self.binding), "summary": "worker 原文", **fields})

    def ready(self, wire="turn_ended", **fields):
        self.begin()
        # A pre-upgrade, already settled task may still be awaiting its original
        # deliverable. Exercise that retained workflow without using the new /result.
        self.store.finish_task(self.task_id, "done", {"summary": "worker 原文"},
                               task_review.initial_review(task_review.time.time()))
        self.server.reviews.tick(self.task_id)
        event, inquiry = self.stream_event(self.stream)
        self.assertEqual(event, "deliverable_request")
        self.assertEqual(inquiry["prompt"], task_review.PROMPT)
        self.assertEqual((inquiry["thread_id"], inquiry["task_id"]), (self.binding["thread_id"], self.task_id))
        return inquiry

    def saved(self):
        return self.call(self.route, method="GET")[1]["task"]

    def reviewer(self, endpoint="", body=None):
        req = request.Request(self.base + self.route + endpoint,
                              data=json.dumps(body).encode() if body is not None else None,
                              headers={"X-Review-Token": self.server.verdict_token, "X-Router-Token": self.server.token,
                                       "Content-Type": "application/json"})
        try:
            resp = request.urlopen(req, timeout=3)
        except error.HTTPError as exc:
            resp = exc
        with resp:
            return resp.status, json.load(resp)

    def report(self, q, summary):
        report = {"summary": summary, "artifacts": ["/tmp/artifact"], "evidence": ["readback OK"], "risks": []}
        return self.call(self.route + "/deliverable", {"name": self.name, "request_id": q["request_id"], "deliverable": report})

    def tick_due(self, key):
        due = self.server.reviews.load(self.task_id)[key]
        with mock.patch.object(task_review.time, "time", return_value=due + 0.001):
            self.server.reviews.sweep()

    def test_busy_and_unsolicited_legacy_result_do_not_settle(self):
        self.begin()
        self.call("/heartbeat", {**self.sender,"state":"running"})
        self.assertEqual(self.end()[0],409)
        self.assertEqual(self.saved()["state"],"running")
        self.assertIsNone(self.saved()["deliverable"])

    def test_human_verdict_and_goal_independence(self):
        for goal in ("active", "paused", None):
            for status in ("done", "failed"):
                self.ready(**({"goal_status": goal} if goal else {}))
                self.assertEqual(self.saved()["review_state"], "reviewing")
                verdict = {"status": status, "reason": "已检查", "evidence": ["git readback"]}
                self.assertEqual(self.call(self.route + "/verdict", verdict)[0], 403)
                code, reply = self.reviewer("/verdict", verdict)
                self.assertEqual((code,reply["review_state"]), (200,status))
                self.assertTrue(self.reviewer("/verdict", verdict)[1]["duplicate"])
                self.assertEqual(self.saved()["verdict"], verdict)
        print("② verdict(done)=200/done; ③ verdict(failed)=200/failed; ④ active/paused/absent × 2 verdicts: 6/6")

    def test_legacy_failure_without_open_probe_is_rejected(self):
        self.begin()
        self.assertEqual(self.end("failed")[1]["error"],"deliverable_request_required")
        self.assertEqual(self.saved()["state"],"running")

    def test_rewrite_reports_and_card_link(self):
        q = self.ready()
        original = "测" * 200 + "。"
        code, reply = self.report(q, original)
        self.assertEqual((code,reply["error"],reply["detail"]), (400,"deliverable_summary_too_long","当前 201 字，上限 200，请重新组织"))
        self.assertEqual(self.report(q, original), (code,reply))
        self.assertIsNone(self.saved()["deliverable"])
        self.assertEqual(self.reviewer()[1]["task"]["deliverable"]["raw"], [original])
        event, rewrite = self.stream_event(self.stream)
        self.assertEqual((event,rewrite["rewrite"]), ("deliverable_request",1))
        self.assertNotEqual(rewrite["request_id"], q["request_id"])
        self.assertEqual(self.report(rewrite, "🐴"*200)[0], 200)
        self.assertTrue(self.report(rewrite, "🐴"*200)[1]["duplicate"])
        self.assertEqual(self.saved()["deliverable"]["summary"], "🐴"*200)
        notices = [x for x in self.sent if x["kind"] == "task.deliverable"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["payload"]["artifacts"], ["/tmp/artifact"])
        listed = self.call("/tasks", method="GET")[1]["tasks"][0]
        for key,value in self.context.items():
            self.assertEqual((notices[0]["payload"][key], listed[key]),(value,value))
        print("⑧ deliverable=received notices=1 card_id=card-甲 board_id=board-乙; ⑩ HTTP 400",reply,
              "; ⑪ rewrite 200 codepoints -> HTTP 200")

    def test_rewrite_exhaustion_preserves_originals(self):
        q = self.ready()
        originals = []
        for attempt in range(4):
            originals.append(str(attempt) + "测"*200)
            self.assertEqual(self.report(q, originals[-1])[0], 400)
            if attempt < 3:
                _,q = self.stream_event(self.stream)
        self.assertEqual(self.saved()["deliverable_state"], "summary_oversized")
        self.assertEqual(self.reviewer()[1]["task"]["deliverable"]["raw"], originals)
        self.assertEqual(sum(n["kind"]=="task.summary_oversized" for n in self.sent), 1)

    def test_missing_and_overdue_remind_only(self):
        q = self.ready()
        for i in range(2,5):
            self.tick_due("delivery_due")
            _,retry = self.stream_event(self.stream)
            self.assertEqual((retry["attempt"],retry["request_id"]), (i,q["request_id"]))
        self.tick_due("delivery_due")
        self.assertEqual(self.saved()["deliverable_state"], "deliverable_missing")
        self.tick_due("review_due")
        self.assertEqual((self.saved()["review_state"],self.saved()["verdict"]),("reviewing",None))
        for name in ("deliverable_missing","review_overdue"):
            self.assertEqual(sum(x["kind"]=="task."+name for x in self.sent),1)
        print("⑨ attempts=1+3, deliverable_missing notices=1; overdue review reminder=1 verdict=null")

    def test_lossless_long_peer_through_reviewer_format_and_task_lookup(self):
        text = ("甲🐴，\n "*10000)
        body = {"message_id":str(uuid.uuid4()),"from":self.name,"to":"Hermes","text":text,"task_id":"t-correlated"}
        self.assertEqual(len(text),50000)
        code, result = self.call("/messages", body)
        self.assertEqual((code,result["status"]), (200,"accepted"))
        saved = self.call("/messages/"+body["message_id"],method="GET")[1]["message"]
        self.assertEqual(saved["text"],text)
        self.assertTrue(self.call("/messages",body)[1]["duplicate"])
        notices = [n for n in self.sent if n["kind"]=="peer.message"]
        self.assertEqual(len(notices),25)
        for i,n in enumerate(notices,1):
            self.assertIn(f"{i}/25 · {body['message_id']} · ", n["text"])
        with self.store._lock:
            sizes = [r[0] for r in self.store._conn.execute("SELECT length(data) FROM peer_text_chunks ORDER BY part")]
        self.assertTrue(all(x<=65536 for x in sizes))
        self.assertEqual(sum(sizes),len(text.encode()))
        plugin_dir = Path(__file__).resolve().parents[1]/"hermes-plugin"/"trigger-bus"
        spec = importlib.util.spec_from_file_location("peer_renderer_test",plugin_dir/"__init__.py")
        renderer = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = renderer
        self.addCleanup(sys.modules.pop, spec.name)
        spec.loader.exec_module(renderer)
        cards = renderer._format_events(notices)
        pos = 0
        for n in notices:
            at = cards.index(n["text"],pos)
            pos = at + len(n["text"])
        pieces = [n["text"].split(body["message_id"]+" · ",1)[1] for n in notices]
        self.assertEqual("".join(pieces),text)
        self.call("/messages",{**body,"message_id":str(uuid.uuid4()),"task_id":"t-another","text":"other"})
        result = self.call("/messages?task_id=t-correlated",method="GET")[1]
        self.assertEqual([m["message_id"] for m in result["messages"]],[body["message_id"]])
        digest = hashlib.sha256(text.encode()).hexdigest()
        print(f"⑫ chars=50000 bytes={len(text.encode())} DB chunks={sizes} rendered=25/25 SHA256={digest}; "
              "⑬ GET /messages?task_id=t-correlated matched=1 unrelated=0")

    def test_partial_root_forward_resumes_before_next_peer(self):
        ids = [str(uuid.uuid4()) for _ in range(2)]
        first = {"message_id": ids[0], "from": self.name, "to": "Hermes", "text": "文" * 4001}
        accepted = []
        def trigger(payload):
            if len(accepted) == 1:
                return False, {"error": "test offline"}
            accepted.append(payload["event_id"])
            return True, {"id": len(accepted)}
        self.server.trigger_client.post_trigger.side_effect = trigger
        _, pending = self.call("/messages", first)
        seq = pending["message"]["seq"]
        self.assertEqual((pending["status"], pending["message"]["acked_at"]), ("queued", None))
        self.server.trigger_client.post_trigger.side_effect = lambda p: (accepted.append(p["event_id"]) or True, {})
        _, following = self.call("/messages", {**first, "message_id": ids[1], "text": "下一条"})
        self.assertEqual(accepted, [f"peer:{seq}:1", f"peer:{seq}:2", f"peer:{seq}:3",
                                   "peer:%s" % following["message"]["seq"]])
        retried = self.call("/messages", first)[1]
        self.assertEqual((retried["status"], retried["duplicate"], len(accepted)), ("accepted", True, 4))
        self.assertEqual(self.call("/messages", {**first, "text":"冲突"})[0], 409)

    def test_peer_larger_than_generic_http_body_survives_sse_and_ack(self):
        msg = {"message_id": str(uuid.uuid4()), "from":"Hermes", "to":self.name, "text":"甲"*400000}
        code, queued = self.call("/messages", msg)
        self.assertEqual((code, queued["status"]), (202,"queued"))
        self.assertEqual(self.stream_event(self.stream), ("worker_message_ref", {"message_id":msg["message_id"]}))
        inbox = self.call("/messages?worker="+self.name, method="GET")[1]["messages"]
        self.assertEqual([m["text"] for m in inbox], [msg["text"]])
        ack = self.call("/messages/"+msg["message_id"]+"/ack", {"worker":self.name})[1]
        self.assertEqual(ack["status"],"accepted")
        self.assertEqual(self.call("/messages",msg)[1]["message"]["acked_at"], ack["message"]["acked_at"])
