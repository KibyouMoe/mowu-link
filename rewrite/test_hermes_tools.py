import json
import unittest

from .devloop import Loop
from .hermes_tools import ManagerTools, TOOLS
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
