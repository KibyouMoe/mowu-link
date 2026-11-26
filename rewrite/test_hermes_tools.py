import json
import unittest

from .devloop import Loop
from .hermes_tools import ManagerTools, TOOLS
from .storage import guid
from .test_mainline import Clock


class ManagerBinding(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock))
        self.binding = ManagerTools(self.loop.hermes, "test-manager")

    def invoke(self, name, args, call="call-1", session="test-manager"):
        return json.loads(self.binding.around_call(name, lambda a: self.binding.invoke(name,a),
                          args, session_id=session, turn_id="turn-1", tool_call_id=call))

    def test_same_call_survives_restart_and_foreign_session_cannot_manage(self):
        name = "router_create_task"
        args = {"title":"First","instructions":"Local only"}
        self.assertEqual(self.invoke(name,args,session="unrelated")["status"],403)
        self.assertEqual(json.loads(self.binding.invoke(name,args))["status"],403)
        self.assertEqual(self.invoke(name,args,call="")["error"],"missing_host_call_identity")
        self.assertEqual(self.invoke(name,{**args,"session_id":"test-manager"})["status"],422)
        self.assertEqual(self.loop.hermes.tasks()["items"],[])
        task = self.invoke(name,args)
        self.binding = ManagerTools(self.loop.hermes,"test-manager")
        self.assertEqual(task,self.invoke(name,args))
        self.assertEqual(self.invoke(name,{**args,"title":"Changed"})["error"],"idempotency_conflict")
        self.assertEqual([task],self.invoke("router_tasks",{"unassigned":True})["items"])
        self.assertEqual(self.invoke("router_tasks",{"task_id":"../workers"})["status"],422)

    def test_all_management_tools_through_isolated_router(self):
        self.assertEqual(len(TOOLS),8)
        worker = self.loop.worker
        worker.connect()
        worker.poll()
        self.clock.advance(10)
        worker.poll()
        self.assertEqual(self.invoke("router_workers",{"online":True})["items"][0]["worker_id"],"oguri")
        task = self.invoke("router_create_task",{"title":"Original","instructions":"test"})
        tid = task["task_id"]
        update = self.invoke("router_update_task",{"task_id":tid,"expected_revision":task["revision"],
                             "changes":{"instructions":"New instruction"}},call="edit")
        self.invoke("router_act_on_task",{"task_id":tid,"expected_revision":update["revision"],
                    "action":"assign","worker_id":"oguri"},call="assign")
        worker.poll()
        question = worker.ask(self.loop.native.entries()[0][1]["native_id"],"fixture-call","Fixture question?",[])
        qid = question["question_id"]
        self.assertEqual(self.invoke("router_questions",{"pending":True})["items"][0]["question_id"],qid)
        reply = {"task_id":tid,"question_id":qid,"answer":{"mode":"text","text":"Fixture answer"}}
        self.assertEqual(self.invoke("router_answer",reply,call="answer")["answer"],reply["answer"])
        self.assertEqual(self.invoke("router_tasks",{"task_id":tid})["task"]["state"],"working")
        self.assertEqual(self.invoke("router_info",{"level":"Info"})["items"],[])

    def test_close_adopts_an_explicit_candidate_without_creating_a_formal_report(self):
        worker, router = self.loop.worker,self.loop.hermes
        tid = router.create(guid(),"Candidate review","Isolated task","oguri")["task_id"]
        worker.connect()
        worker.poll()
        self.clock.advance(10)
        worker.poll()
        worker.poll()
        aid = worker.hold()["held_assignment_id"]
        native = self.loop.native.entries()[0][1]["native_id"]
        blocked = worker.report(native,tid,aid,"Cannot complete formal assignment","blocked")
        self.assertEqual(blocked["task_state"],"blocked")
        body = {"session_id":worker.session,"assignment_id":aid,"text":"Candidate for manager review"}
        material = self.loop.wire.request("POST",f"tasks/{tid}/materials",body,guid())
        before = router.inspect(tid)
        self.assertEqual(before["task"]["state"],"blocked")
        self.assertEqual(before["task"]["holder"],{"kind":"router"})
        self.assertEqual(TOOLS["router_act_on_task"][1]["properties"]["material_id"]["format"],"uuid")
        close = {"task_id":tid,"expected_revision":before["task"]["revision"],"action":"close",
                 "material_id":material["material_id"],"note":"Read and accepted this candidate"}
        tool = "router_act_on_task"
        self.assertEqual(self.invoke(tool,close,session="other")["error"],"wrong_host_session")
        self.assertEqual(self.invoke(tool,{**close,"material_id":"invalid"})["error"],"invalid_material_id")
        self.assertEqual(self.invoke(tool,{**close,"material_id":guid()})["error"],"unknown_material")
        self.assertEqual(self.invoke(tool,{**close,"action":"cancel","reason":"invalid use of a material"})["error"],
                         "material_requires_pool_close")
        self.assertEqual(router.inspect(tid),before)
        receipt = self.invoke(tool,close)
        self.assertEqual(receipt["task"]["state"],"closed")
        self.assertEqual(self.invoke(tool,close),receipt)
        self.assertEqual(self.invoke(tool,{**close,"note":"Different adoption"})["error"],"idempotency_conflict")
        detail = router.inspect(tid)
        self.assertEqual(detail["last_report"]["outcome"],"blocked")
        self.assertEqual(detail["materials"],before["materials"])
        with self.loop.orders.db.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reports WHERE task_id=?",(tid,)).fetchone()[0],0)
            adopted, = db.execute("SELECT request FROM operations WHERE actor='hermes' AND json_extract(request,'$[1].material_id')=?",
                                  (material["material_id"],)).fetchall()
            self.assertEqual(json.loads(adopted[0])[1],{key:value for key,value in close.items() if key!="task_id"})

        formal = router.create(guid(),"Formal work","Isolated task","oguri")["task_id"]
        for seconds in (1,10,1):
            self.clock.advance(seconds)
            worker.poll()
        current = worker.hold()["held_assignment_id"]
        native = self.loop.native.entries()[-1][1]["native_id"]
        worker.report(native,formal,current,"A formal deliverable")
        report = router.inspect(formal)
        bad_close = {**close,"task_id":formal,"expected_revision":report["task"]["revision"]}
        self.assertEqual(self.invoke(tool,bad_close,call="formal")["error"],"material_requires_pool_close")
        self.assertEqual(router.inspect(formal),report)
