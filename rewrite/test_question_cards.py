import json
import unittest
from threading import Thread

from .feishu_output import Views
from .host_wire import RemoteAdapter, serve_adapter
from .question_cards import Actions
from .storage import Fault, guid
from .test_feishu_inbound import PlatformCase
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .wire import Client


class QuestionCards(PlatformCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.loop.orders.clock = self.clock
        self.sink = PlatformFixture()
        self.views = Views(self.loop.root/"views.db", self.platform,self.sink,"private-chat","小栗帽",self.clock)
        self.actions = Actions(self.views,self.inbound)
        self.options = [{"id":"yes","label":"第一项"},{"id":"no","label":"第二项"}]
        self.native = self.loop.native
        self.question = {"session":self.native.session,"turn_id":"turn","native_question_id":"question",
                         "text":"选择或填写？","options":self.options}
        self.closed = {}
        self.executions = 0
        self.native.resolve_question = self.fixture_close
        self.native.lookup_answer = lambda *key: self.closed.get(key)
        # Put the host boundary on its own authenticated HTTP connection.
        server = self.enterContext(serve_adapter(self.loop.adapter,"oguri",{"test-worker":"worker/oguri"},("127.0.0.1",0),None))
        thread = Thread(target=server.serve_forever,kwargs={"poll_interval":0.01},daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.loop.worker.adapter = RemoteAdapter(Client("http://127.0.0.1:"+str(server.server_port),"test-worker"))

    def fixture_close(self, original,answer):
        self.assertEqual(original,self.question)
        self.executions += 1
        key = tuple(original[k] for k in ("session","turn_id","native_question_id"))
        self.assertNotIn(key,self.closed)
        proof = dict(zip(("session","turn_id","native_question_id"),key),answer=answer,receipt_id=guid(),closed=True)
        self.closed[key] = proof
        return proof

    def open(self):
        reply = self.adapter.request("POST","worker/native-question",self.question)
        self.assertEqual(self.adapter.request("POST","worker/native-question",self.question),reply)
        self.views.poll()
        card = list(self.sink.messages.values())[-1]
        return reply["question_id"],card

    def click(self,qid,mid,mode,form=None,actor="trainer",**extra):
        return self.actions.record("app",actor,"private-chat",mid,{"question_id":qid,"mode":mode,**extra},form)

    def test_trainer_answers_only_original_card_and_original_native_prompt(self):
        for mode,fields,form,answer_text in [("choice",{"option_id":"no"},None,"第二项"),
                                               ("text",{},{"reply":"手写答案"},"手写答案"),("skip",{},None,"已跳过")]:
            self.question["turn_id"] = "turn-"+mode
            qid,card = self.open()
            mid,_,body = card
            self.assertLess(json.dumps(body,ensure_ascii=False).index("第一项"),json.dumps(body,ensure_ascii=False).index("第二项"))
            self.assertIn("form",[e["tag"] for e in body["body"]["elements"]])
            for actor,msg in (("other",mid),("trainer","om_other")):
                with self.assertRaises(Fault):
                    self.click(qid,msg,mode,form,actor,**fields)
            self.assertEqual(self.click(qid,mid,mode,form,**fields),"pending")
            self.assertEqual(self.click(qid,mid,mode,form,**fields),"pending")
            self.actions.flush()
            self.views.poll()
            self.assertEqual(self.click(qid,mid,mode,form,**fields),"confirmed")
            self.assertEqual(card[2]["header"]["title"]["content"],"小栗帽-提问 · 已回答")
            self.assertNotIn("form",[e["tag"] for e in card[2]["body"]["elements"]])
            self.assertIn("训练员的回答："+answer_text,card[2]["body"]["elements"][0]["text"]["content"])
            self.assertEqual(self.sink.calls[-1],("patch",mid))
        self.assertEqual(self.executions,3)
        self.assertFalse(self.loop.hermes.tasks()["items"])

    def test_unknown_close_is_queried_and_original_action_is_never_repeated(self):
        qid,card = self.open()
        def ambiguous(original,answer):
            self.fixture_close(original,answer)
            return None
        self.native.resolve_question = ambiguous
        self.native.lookup_answer = lambda *args: None
        self.click(qid,card[0],"skip")
        self.actions.flush()
        self.clock.advance(16)
        self.actions.flush()
        self.assertEqual(self.executions,1)
        self.assertFalse(self.views.poll())
        self.native.lookup_answer = lambda *args: self.closed[args]
        self.clock.advance(16)
        self.actions.flush()
        self.assertEqual(self.views.poll(),1)
        self.assertEqual(self.executions,1)
        self.assertEqual(self.sink.calls[-1],("patch",card[0]))

    def test_batch_submit_skips_blanks_and_keeps_original_field_options(self):
        self.question.pop("text")
        self.question.pop("options")
        self.question["questions"] = [
            {"id":"first","text":"选一项","options":self.options,"multiple":False,"custom":False},
            {"id":"colors","text":"可选任意项","options":self.options,"multiple":True,"custom":True},
            {"id":"note","text":"仅自填","options":[],"multiple":False,"custom":True},
        ]
        qid,card = self.open()
        mid = card[0]
        form = {"options_1":"2","options_2":"2,1"}
        for invalid in (dict(form,options_1="1,2"),dict(form,options_2="1,1"),dict(form,options_2="3"),
                        dict(form,reply_1="不接受"),dict(form,options_3="1")):
            with self.assertRaises(Fault):
                self.click(qid,mid,"batch",invalid)
        self.assertEqual(self.executions,0)
        self.assertEqual(self.click(qid,mid,"batch",form),"pending")
        self.assertEqual(self.click(qid,mid,"batch",dict(form,options_2="1,2")),"pending")
        with self.assertRaisesRegex(Fault,"answer_conflict"):
            self.click(qid,mid,"skip")
        self.actions.flush()
        self.views.poll()
        proof, = self.closed.values()
        self.assertEqual(proof["answer"],{"mode":"batch","answers":[
            {"id":"first","option_ids":["no"]},{"id":"colors","option_ids":["yes","no"]},{"id":"note","option_ids":[]},
        ]})
        self.assertEqual(self.sink.calls.count(("patch",mid)),1)
        fields = [e["text"]["content"] for e in card[2]["body"]["elements"][:3]]
        self.assertEqual([field.splitlines()[-1] for field in fields],
                         ["训练员的回答：第二项","训练员的回答：第一项、第二项","训练员的回答：已跳过"])
        self.assertEqual(self.executions,1)
        self.assertFalse(self.loop.hermes.tasks()["items"])

    def test_manager_is_read_only_and_original_card_updates_after_answer(self):
        manager,w = self.loop.hermes,self.loop.worker
        tid = manager.create(guid(),"题卡","找溪流","oguri")["task_id"]
        w.connect()
        w.poll()
        self.clock.advance(10)
        w.poll()
        w.poll()
        native = self.native.entries()[0][1]["native_id"]
        qid = w.ask(native,"manager-call","问溪流",self.options)["question_id"]
        self.views.poll()
        card = list(self.sink.messages.values())[-1]
        self.assertNotIn("form",[e["tag"] for e in card[2]["body"]["elements"]])
        with self.assertRaises(Fault):
            self.click(qid,card[0],"skip")
        with self.assertRaises(Fault):
            self.platform.request("POST","worker/trainer-answer",{"question_id":qid,"answer":{"mode":"skip"}})
        manager.answer(guid(),tid,qid,{"mode":"text","text":"继续"})
        w.poll()
        self.assertEqual(self.views.poll(),1)
        self.assertEqual(self.sink.calls[-1],("patch",card[0]))
        self.assertIn("溪流的回答：继续",card[2]["body"]["elements"][0]["text"]["content"])
        self.assertEqual(w.hold()["held_task_id"],tid)
        self.assertEqual(self.executions,0)

    def test_proof_for_another_turn_and_foreign_question_source_are_rejected(self):
        with self.assertRaises(Fault):
            self.other.request("POST","worker/native-question",self.question)
        with self.assertRaises(Fault):
            self.adapter.request("POST","worker/native-question",dict(self.question,session="wrong"))
        qid,card = self.open()
        self.native.resolve_question = lambda *args: dict(self.fixture_close(*args),turn_id="unrelated")
        self.click(qid,card[0],"skip")
        self.actions.flush()
        self.assertFalse(self.views.poll())
        with self.assertRaises(Fault):
            self.click(qid,card[0],"text",{"reply":"changed"})
