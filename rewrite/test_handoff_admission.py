"""Exercise the Worker and Router handoff barrier over their authenticated HTTP ports."""
from concurrent.futures import ThreadPoolExecutor
import unittest

from .devloop import Loop
from .feishu_inbound import Inbound
from .storage import Fault, encode, guid
from .test_mainline import Clock
from .test_state_bundle import running
from .wire import Client
from .worker import Worker
from .worker_service import WorkerService


class HandoffAdmission(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock,extra_workers=["other"]))
        self.worker = self.loop.worker
        self.service = WorkerService(self.worker,self.clock)
        self.service.step()

    def router_handoff(self,handoff_id,action,session=None):
        return self.loop.wire.request("PUT","workers/oguri/admission-handoffs/"+handoff_id,
                                      {"session_id":session or self.worker.session,"action":action})

    def create(self):
        return self.loop.hermes.create(guid(),"Arrived during handoff","Write one result","oguri")["task_id"]

    def platform_port(self,service):
        config = {guid():role for role in ("handoff/oguri","platform/oguri","ops/oguri","handoff/other")}
        url = self.enterContext(running(service.listen(config,("127.0.0.1",0),None)))
        return [Client(url,key) for key in config]

    def assert_fault(self,code,call,*args):
        with self.assertRaises(Fault) as failure:
            call(*args)
        self.assertEqual(failure.exception.code,code)
        return failure.exception.status

    def test_router_admission_and_assignment_serialize_with_current_session(self):
        self.clock.advance(10)
        first = guid()

        def begin():
            try:
                return self.router_handoff(first,"begin")["state"]
            except Fault as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            begun = pool.submit(begin)
            task = pool.submit(self.create)
            state,tid = begun.result(),task.result()
        details = self.loop.hermes.inspect(tid)["task"]
        self.assertIn(state,("frozen","handoff_work_pending"))
        if state=="frozen":
            self.assertIsNone(details["assignment_id"])
            self.assertEqual(self.router_handoff(first,"end")["state"],"released")
            self.worker.poll()
            self.clock.advance(10)
            self.worker.poll()
            details = self.loop.hermes.inspect(tid)["task"]
        self.assertTrue(details["assignment_id"])
        self.assertEqual(self.assert_fault("handoff_work_pending",self.router_handoff,guid(),"begin"),409)
        operator,_platform,ops,_foreign = self.platform_port(self.service)
        request = {"handoff_id":guid(),"action":"begin"}
        self.assert_fault("handoff_work_pending",operator.request,"PUT","worker/handoff-admission",request)
        self.assertNotIn("handoff",ops.request("GET","worker/status"))
        self.assert_fault("handoff_work_pending",operator.request,"PUT","worker/handoff-admission",request)
        self.worker.poll()
        self.assertEqual(self.worker.hold()["held_assignment_id"],details["assignment_id"])
        self.assertEqual(self.assert_fault("handoff_work_pending",self.router_handoff,guid(),"begin"),409)
        self.assertEqual(len(self.loop.native.entries()),1)

    def test_router_release_tombstone_fences_late_begin_and_requires_fresh_idle(self):
        handoff_id,delayed = guid(),guid()
        self.assertEqual(self.router_handoff(delayed,"end")["state"],"released")
        self.assertEqual(self.router_handoff(delayed,"begin")["state"],"released")
        self.assertEqual(self.router_handoff(handoff_id,"begin")["state"],"frozen")
        self.assertEqual(self.assert_fault("another_handoff_active",self.router_handoff,guid(),"begin"),409)
        self.assert_fault("forbidden",self.loop.extra_clients["other"].request,"PUT",
                          "workers/oguri/admission-handoffs/"+handoff_id,
                          {"session_id":self.worker.session,"action":"end"})
        self.assert_fault("stale_session",self.router_handoff,handoff_id,"end",guid())
        self.assert_fault("worker_handoff_active",self.loop.wire.request,"PUT","workers/oguri/session",
                          {"session_id":guid()},guid())
        self.worker.connect()
        self.worker.poll()
        self.clock.advance(15)
        self.worker.poll()
        tid = self.create()
        self.assertTrue(self.loop.hermes.workers()["items"][0]["admission_paused"])
        self.assertEqual(self.router_handoff(delayed,"end")["state"],"released")
        self.assertIsNone(self.loop.hermes.inspect(tid)["task"]["assignment_id"])
        self.assertEqual(self.router_handoff(handoff_id,"end")["state"],"released")
        self.worker.poll()
        self.clock.advance(9)
        self.worker.poll()
        self.assertIsNone(self.loop.hermes.inspect(tid)["task"]["assignment_id"])
        self.clock.advance(1)
        self.worker.poll()
        self.worker.poll()
        self.assertEqual(self.worker.hold()["held_task_id"],tid)
        self.assertEqual(self.router_handoff(handoff_id,"begin")["state"],"released")
        self.assertEqual(len(self.loop.native.entries()),1)

    def test_worker_retains_paused_inputs_and_recovers_both_lost_router_receipts(self):
        operator,platform,ops,foreign = self.platform_port(self.service)
        handoff_id = guid()
        command = {"handoff_id":handoff_id,"action":"begin"}
        for client in (platform,ops,foreign):
            self.assert_fault("forbidden",client.request,"PUT","worker/handoff-admission",command)
        self.loop.native.is_busy = True
        self.assert_fault("handoff_native_busy",operator.request,"PUT","worker/handoff-admission",command)
        self.loop.native.is_busy = False
        router = self.loop.wire.request
        lost = set()

        def lose(method,path,*args,**kwargs):
            reply = router(method,path,*args,**kwargs)
            if path.endswith("/admission-handoffs/"+handoff_id):
                action = args[0]["action"]
                if action not in lost:
                    lost.add(action)
                    raise ConnectionError("lost handoff response after durable Router write")
            return reply

        self.loop.wire.request = lose
        self.assert_fault("handoff_receipt_unconfirmed",operator.request,"PUT","worker/handoff-admission",command)
        self.assertEqual(ops.request("GET","worker/status")["handoff"],{"handoff_id":handoff_id,"state":"beginning"})
        mid,peer_id = "om_held_by_platform",guid()
        attach = self.loop.root/"platform-handoff.db"
        platform_input = Inbound(attach,platform,None,"app","trainer","test-chat",clock=self.clock)
        platform_input.record("app","user","trainer",{"chat_type":"p2p","chat_id":"test-chat","message_id":mid,
            "message_type":"text","content":encode({"text":"Keep this input during handoff"})})
        self.assert_fault("native_handoff_in_progress",platform_input.deliver,mid)
        self.clock.advance(5)
        self.assertEqual(platform_input.pending(),[mid])
        peer,peer_session = self.loop.extra_clients["other"],guid()
        peer.request("PUT","workers/other/session",{"session_id":peer_session},guid())
        peer.request("POST","peers/oguri/messages",{"session_id":peer_session,"message_id":peer_id,"text":"Peer during handoff"},guid())
        tid = self.create()
        reopened = Worker(self.worker.db.path,"oguri",self.loop.wire,self.loop.adapter,clock=self.clock)
        service = WorkerService(reopened,self.clock)
        operator,platform,ops,_foreign = self.platform_port(service)
        service.step()
        self.assertEqual(ops.request("GET","worker/status")["handoff"]["state"],"frozen")
        self.assertEqual(operator.request("PUT","worker/handoff-admission",command)["state"],"frozen")
        self.assertTrue(self.loop.hermes.workers()["items"][0]["admission_paused"])
        self.assertIsNone(self.loop.hermes.inspect(tid)["task"]["assignment_id"])
        self.assertFalse(peer.request("GET","peers/messages/"+peer_id)["worker_received"])
        self.assertFalse(self.loop.native.entries())
        self.clock.advance(31)
        service.step()
        status, = [row for row in self.loop.hermes.workers()["items"] if row["worker_id"]=="oguri"]
        self.assertTrue(status["online"] and status["admission_paused"])
        self.assertIsNone(self.loop.hermes.inspect(tid)["task"]["assignment_id"])

        end = dict(command,action="end")
        self.assert_fault("handoff_receipt_unconfirmed",operator.request,"PUT","worker/handoff-admission",end)
        self.assertEqual(ops.request("GET","worker/status")["handoff"]["state"],"ending")
        self.assert_fault("native_handoff_in_progress",platform.request,"POST","worker/chat",{"message_id":mid,"text":"Keep this input during handoff"})
        service.step()
        self.assertNotIn("handoff",ops.request("GET","worker/status"))
        self.assertEqual(operator.request("PUT","worker/handoff-admission",end)["state"],"released")
        self.assertEqual(lost,{"begin","end"})
        reopened_platform = Inbound(attach,platform,None,"app","trainer","test-chat",clock=self.clock)
        receipt = reopened_platform.deliver(mid)
        self.assertEqual(reopened_platform.deliver(mid),receipt)
        self.assertEqual([p["kind"] for p,_ in self.loop.native.entries()],["peer","chat"])
        self.assertTrue(peer.request("GET","peers/messages/"+peer_id)["host_receipt_id"])
        self.clock.advance(10)
        service.step()
        service.step()
        self.assertEqual(reopened.hold()["held_task_id"],tid)
        self.assertEqual([p["kind"] for p,_ in self.loop.native.entries()],["peer","chat","task"])
        self.evidence = {"handoff_id":handoff_id,"recovered":sorted(lost),
            "online_during_pause":status["online"],
            "paused_private_delivery":mid,"native_inputs":[proof for _,proof in self.loop.native.entries()],
            "task_id":tid,"held_assignment_id":reopened.hold()["held_assignment_id"],
            "boundary":"Real isolated Router/Worker HTTP and SQLite; platform and native host fixtures"}
