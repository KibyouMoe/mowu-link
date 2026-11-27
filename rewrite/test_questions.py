import unittest
from unittest.mock import patch

from .devloop import Loop
from .storage import Fault, guid
from .test_mainline import Clock
from .worker import Worker


class Questions(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock))
        self.w, self.m = self.loop.worker, self.loop.hermes
        self.tid = self.m.create(guid(), "Questions", "Ask independently", "oguri")["task_id"]
        self.w.connect()
        self.w.poll()
        self.clock.advance(10)
        self.w.poll()
        self.w.poll()
        self.native = self.loop.native.entries()[0][1]["native_id"]
        self.options = [{"id":"y", "label":"Yes"}, {"id":"n", "label":"No"}]

    def test_two_questions_selected_and_skipped_delivery_once(self):
        a = self.w.ask(self.native, "call 1", "Proceed?", self.options)
        self.assertEqual(a, self.w.ask(self.native, "call 1", "Proceed?", self.options))
        b = self.w.ask(self.native, "call 2", "Any note?", [])
        self.assertNotEqual(a["question_id"], b["question_id"])
        for frame in self.loop.manager_wire.frames(0):
            self.assertEqual(frame["payload"]["level"], "Notify")
        self.assertEqual(len(self.loop.manager_wire.frames(0)), 2)
        self.assertEqual(len(self.m.questions(task_id=self.tid, pending="true")["items"]), 2)
        choice = {"mode":"choice", "option_id":"y"}
        self.m.answer(guid(), self.tid, a["question_id"], choice)
        self.assertEqual(len(self.m.questions(pending="true")["items"]), 1)
        self.m.answer(guid(), self.tid, b["question_id"], {"mode":"skip"})
        self.m.answer(guid(), self.tid, b["question_id"], {"mode":"skip"})
        self.w.poll()
        self.assertFalse(self.m.questions(pending="true")["items"])
        self.assertEqual([(p["question_id"],p["answer"]) for p,_ in self.loop.native.entries()[1:]],
                         [(a["question_id"],choice),(b["question_id"],{"mode":"skip"})])
        self.assertIsNone(self.m.inspect(self.tid)["last_report"])

    def test_invalid_answers_and_ordinary_chat_cannot_ask(self):
        chat = self.w.chat(guid(), "private")
        with self.assertRaises(Fault):
            self.w.ask(chat["native_id"], "private call", "ask?", [])
        qid = self.w.ask(self.native, "first", "Proceed?", self.options)["question_id"]
        for wrong in ({"mode":"choice","option_id":"bad"}, {"mode":"text","text":" "}, {"mode":"skip","text":"no"}):
            with self.assertRaises(Fault):
                self.m.answer(guid(), self.tid, qid, wrong)
        with self.assertRaises(Fault):
            self.m.answer(guid(), guid(), qid, {"mode":"skip"})
        with self.assertRaises(Fault) as failure:
            self.loop.wire.request("PUT", f"questions/{qid}/answer", {"task_id":self.tid, "answer":{"mode":"skip"}}, guid())
        self.assertEqual(failure.exception.status, 403)
        text = {"mode":"text","text":"A separate note"}
        self.m.answer(guid(), self.tid, qid, text)
        with self.assertRaises(Fault) as failure:
            self.m.answer(guid(), self.tid, qid, {"mode":"skip"})
        self.assertEqual(failure.exception.code, "answer_conflict")

    def test_old_question_after_transfer_remains_with_asker(self):
        qid = self.w.ask(self.native, "original", "Should I?", [])["question_id"]
        before = self.m.inspect(self.tid)["task"]
        self.m.act(guid(), self.tid, before["revision"], "transfer", worker_id="other", reason="move")
        self.m.answer(guid(), self.tid, qid, {"mode":"skip"})
        self.w.poll()
        answer, receipt = self.loop.native.entries()[-1]
        self.assertEqual(answer["kind"], "question_answer")
        with self.assertRaises(Fault):
            self.w.report(receipt["native_id"], self.tid, before["assignment_id"], "late answer")
        self.assertEqual(self.m.questions(question_id=qid)["items"][0]["asked_by"], "oguri")
        self.assertEqual(self.m.inspect(self.tid)["task"]["assignee_id"], "other")

    def test_lost_question_ack_replays_same_generated_guid(self):
        original = self.loop.wire.request
        def drop(method, path, *args):
            reply = original(method, path, *args)
            if path.endswith("/questions"):
                raise ConnectionError("lost after Router commit")
            return reply
        self.loop.wire.request = drop
        with self.assertRaises(ConnectionError):
            self.w.ask(self.native, "call", "same question", [])
        self.loop.wire.request = original
        self.w = Worker(self.w.db.path,self.w.worker_id,self.w.router,self.w.adapter,clock=self.clock)
        self.w.connect()
        with self.w.db.transaction() as db:
            receipt, = db.execute("SELECT receipt FROM questions").fetchone()
        self.assertIsNotNone(receipt)
        with patch.object(self.loop.wire,"request",side_effect=AssertionError("W05 already recovered")):
            receipt = self.w.ask(self.native, "call", "same question", [])
        self.assertEqual([q["question_id"] for q in self.m.questions()["items"]], [receipt["question_id"]])
        self.assertEqual(len(self.loop.manager_wire.frames(0)), 1)

    def test_question_outbox_recovers_before_commit_and_delivers_the_original_answer_once(self):
        request = self.loop.wire.request
        sent = []
        def lost(method,path,*args,**kwargs):
            if method=="POST" and path.endswith("/questions"):
                sent.append((method,path,args,kwargs))
                raise ConnectionError("question request never arrived")
            return request(method,path,*args,**kwargs)

        with patch.object(self.loop.wire,"request",side_effect=lost), self.assertRaises(ConnectionError):
            self.w.ask(self.native,"original call","Original text",self.options)
        self.assertEqual(self.m.questions()["items"],[])
        self.w = Worker(self.w.db.path,self.w.worker_id,self.w.router,self.w.adapter,clock=self.clock)
        with patch.object(self.loop.wire,"request",wraps=request) as replay:
            self.w.poll()
        attempt, = [call for call in replay.call_args_list if call.args[0]=="POST" and call.args[1].endswith("/questions")]
        method,path,args,kwargs = sent[0]
        self.assertEqual((attempt.args,attempt.kwargs),((method,path,*args),kwargs))
        q, = self.m.questions()["items"]
        self.assertEqual(q["question_id"],args[0]["question_id"])
        self.assertEqual(q["assignment_id"],self.w.hold()["held_assignment_id"])
        choice = {"mode":"choice","option_id":"y"}
        reply_key = guid()
        original = self.m.answer(reply_key,self.tid,q["question_id"],choice)
        self.assertEqual(self.m.answer(reply_key,self.tid,q["question_id"],choice),original)
        self.w.poll()
        self.w.poll()
        (payload,reply), = self.loop.native.entries()[1:]
        self.assertEqual((payload["assignment_id"],payload["question_id"],payload["answer"]),
                         (q["assignment_id"],q["question_id"],choice))
        self.assertIsNone(self.m.inspect(self.tid)["last_report"])
        with self.w.db.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM display_events WHERE event_id LIKE '%manager-%'").fetchone()[0],2)
        self.assertEqual(self.w.report(reply["native_id"],self.tid,q["assignment_id"],"After the answer")["task_state"],
                         "completed_reported")

    def test_uncommitted_question_from_a_transferred_assignment_stays_rejected(self):
        with patch.object(self.loop.wire,"request",side_effect=ConnectionError("before W05")),self.assertRaises(ConnectionError):
            self.w.ask(self.native,"original call","Original text",[])
        task = self.m.inspect(self.tid)["task"]
        self.m.act(guid(),self.tid,task["revision"],"transfer",worker_id="other",reason="move")
        self.w.poll()
        self.w.poll()
        with self.assertRaisesRegex(Fault,"stale_assignment"):
            self.w.ask(self.native,"original call","Original text",[])
        self.assertEqual(self.m.questions()["items"],[])
        self.assertEqual(self.m.inspect(self.tid)["task"]["assignee_id"],"other")
        with self.w.db.transaction() as db:
            question_id,receipt = db.execute("SELECT question_id,receipt FROM questions").fetchone()
            self.assertIsNone(receipt)
            self.assertEqual(tuple(db.execute("SELECT status,code FROM question_rejections WHERE question_id=?",
                                              (question_id,)).fetchone()),(409,"stale_assignment"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM display_events WHERE event_id LIKE '%manager-%'").fetchone()[0],0)
