"""Platform-command handoff, using the real Router and separate native receipt ledgers."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest

from .adapter import HostAdapter
from .devloop import FixtureHost, Loop
from .host_wire import RemoteAdapter, serve_adapter
from .storage import Fault, guid
from .test_mainline import Clock
from .test_state_bundle import running
from .wire import Client
from .worker import Worker
from .worker_service import WorkerService


class WorkerResume(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock))
        self.source,self.target = ("resume-source","resume-target")
        self.hosts = {session:FixtureHost(self.loop.root/(session+".native.db"),session)
                      for session in (self.source,self.target)}
        self.ports = {}
        for session,native in self.hosts.items():
            adapter = HostAdapter(self.loop.root/(session+".adapter.db"),native,session)
            token = guid()
            addr = self.enterContext(running(serve_adapter(adapter,"oguri",{token:"worker/oguri"},("127.0.0.1",0),None)))
            self.ports[session] = RemoteAdapter(Client(addr,token),session)
        self.worker,self.service,self.clients = self.open_worker()

    def open_worker(self):
        worker = Worker(self.loop.worker.db.path,"oguri",self.loop.wire,self.ports[self.source],
                        clock=self.clock,resume_targets={self.target:self.ports[self.target]})
        service = WorkerService(worker,self.clock)
        service.step()
        roles = ["platform/oguri","ops/oguri","adapter/oguri/"+self.source,"adapter/oguri/"+self.target,"handoff/oguri"]
        tokens = {guid():role for role in roles}
        with self.assertRaisesRegex(Fault,"scoped_native_callbacks_required"):
            service.listen(dict(tokens,unscoped="adapter/oguri"),("127.0.0.1",0),None)
        url = self.enterContext(running(service.listen(tokens,("127.0.0.1",0),None)))
        return worker,service,{role:Client(url,token) for token,role in tokens.items()}

    def command(self,clients,mid,text):
        return clients["platform/oguri"].request("POST","worker/command",{"message_id":mid,"text":text})["receipt"]

    def switch(self,clients,mid,target):
        return self.command(clients,mid,"/resume "+target)

    def message(self,clients,mid):
        return clients["platform/oguri"].request("POST","worker/chat",{"message_id":mid,"text":"Same private message"})["receipt"]

    def native_display(self,client,session):
        return client.request("POST","worker/display",{"event_id":guid(),"kind":"final","session":session,
                              "text":"Native callback","turn_id":"fixture-turn"})

    def status(self,clients):
        return clients["ops/oguri"].request("GET","worker/status")

    def test_switch_preserves_both_host_receipts_and_rejects_old_callback(self):
        clients = self.clients
        source = clients["adapter/oguri/"+self.source]
        target = clients["adapter/oguri/"+self.target]
        before = guid()
        old = self.message(clients,before)
        with self.assertRaisesRegex(Fault,"native_callback_inactive"):
            self.native_display(target,self.source)
        receipt = self.switch(clients,"om_runtime_resume",self.target)
        binding = receipt["binding_receipt"]
        self.assertEqual((binding["previous_session"],binding["session"]),(self.source,self.target))
        self.assertEqual(self.status(clients)["host_session"],self.target)
        with self.assertRaisesRegex(Fault,"native_callback_inactive"):
            self.native_display(source,self.target)
        self.assertTrue(self.native_display(target,self.target)["event_id"])
        self.assertEqual(self.switch(clients,"om_runtime_resume",self.target),receipt)
        self.assertEqual(self.message(clients,before),old)
        new = self.message(clients,guid())
        self.assertEqual((new["session"],len(self.hosts[self.target].entries())),(self.target,1))
        self.assertFalse(self.loop.hermes.tasks()["items"])
        self.assertEqual(self.switch(clients,"om_runtime_back",self.source)["binding_receipt"]["session"],self.source)
        self.assertEqual(self.switch(clients,"om_runtime_resume",self.target),receipt)
        self.assertEqual(self.status(clients)["host_session"],self.source)
        self.assertEqual(len(self.hosts[self.source].entries()),1)
        self.assertEqual(len(self.hosts[self.target].entries()),1)

    def test_lost_begin_and_end_keep_operation_and_target_binding_across_restart(self):
        request = self.loop.wire.request
        lose = {"begin","end"}

        def drop(method,path,*args,**kwargs):
            reply = request(method,path,*args,**kwargs)
            if "/admission-handoffs/" in path and args[0]["action"] in lose:
                lose.remove(args[0]["action"])
                raise ConnectionError("receipt lost after committed write")
            return reply

        self.loop.wire.request = drop
        with self.assertRaisesRegex(Fault,"native_switch_unconfirmed"):
            self.switch(self.clients,"om_lost_switch",self.target)
        self.assertEqual(self.status(self.clients)["native_switch"]["phase"],"starting")
        with self.assertRaisesRegex(Fault,"native_handoff_in_progress"):
            self.message(self.clients,guid())
        self.assertTrue(self.loop.hermes.workers()["items"][0]["admission_paused"])
        with self.assertRaisesRegex(Fault,"pending_resume_targets_required"):
            Worker(self.worker.db.path,"oguri",self.loop.wire,self.ports[self.source],clock=self.clock)
        restarted,service,clients = self.open_worker()
        self.assertEqual(restarted.native_session_pin,self.target)
        self.assertEqual(self.status(clients)["native_switch"]["phase"],"bound")
        with self.assertRaisesRegex(Fault,"native_switch_manages_handoff"):
            clients["handoff/oguri"].request("PUT","worker/handoff-admission",
                {"action":"end","handoff_id":self.status(clients)["native_switch"]["handoff_id"]})
        with self.assertRaisesRegex(Fault,"native_handoff_in_progress"):
            self.message(clients,guid())
        task = self.loop.hermes.create(guid(),"After resume","Submit fixture", "oguri")["task_id"]
        self.assertIsNone(self.loop.hermes.inspect(task)["task"]["assignment_id"])
        finished,final_service,final_clients = self.open_worker()
        self.assertEqual(finished.native_session_pin,self.target)
        self.assertNotIn("native_switch",self.status(final_clients))
        receipt = self.switch(final_clients,"om_lost_switch",self.target)
        self.assertEqual(receipt["binding_receipt"]["session"],self.target)
        with self.assertRaisesRegex(Fault,"platform_command_conflict"):
            self.switch(final_clients,"om_lost_switch",self.source)
        self.assertEqual(self.hosts[self.source].entries(),[])
        seen = self.message(final_clients,guid())
        self.assertEqual(seen["session"],self.target)
        self.clock.advance(10)
        final_service.step()
        final_service.step()
        self.assertEqual(finished.hold()["held_task_id"],task)
        payload,formal = self.hosts[self.target].entries()[-1]
        self.assertEqual((payload["task_id"],formal["session"]),(task,self.target))
        self.assertEqual(lose,set())
        display = final_clients["platform/oguri"].request("GET","worker/display")["items"]
        self.assertEqual(sum(item["event"]["event_id"]=="command-om_lost_switch" for item in display),1)
        self.evidence = {"binding":receipt["binding_receipt"],"lost":["begin","end"],
                         "private_receipt":seen,"task_id":task,"formal_receipt":formal,
                         "source_inputs":0,"target_inputs":2}

    def test_in_flight_callback_prevents_cutover_and_busy_target_never_starts_it(self):
        clients = self.clients
        self.hosts[self.target].is_busy = True
        declined = self.switch(clients,"om_busy_target",self.target)
        self.assertNotIn("binding_receipt",declined)
        self.assertIn("目标会话正在运行",json.dumps(declined,ensure_ascii=False)
                      + json.dumps(clients["platform/oguri"].request("GET","worker/display"),ensure_ascii=False))
        self.hosts[self.target].is_busy = False
        arrive,release = Event(),Event()
        route = self.service.route

        def block(actor,method,path,body,headers):
            if path=="/v1/worker/display" and actor=="adapter/oguri":
                arrive.set()
                self.assertTrue(release.wait(4))
            return route(actor,method,path,body,headers)

        self.service.route = block
        with ThreadPoolExecutor(max_workers=1) as pool:
            in_flight = pool.submit(self.native_display,clients["adapter/oguri/"+self.source],self.source)
            try:
                self.assertTrue(arrive.wait(3))
                with self.assertRaisesRegex(Fault,"native_switch_callbacks_pending"):
                    self.switch(clients,"om_wait_callback",self.target)
            finally:
                release.set()
            self.assertTrue(in_flight.result()["event_id"])
        self.assertEqual(self.switch(clients,"om_wait_callback",self.target)["binding_receipt"]["session"],self.target)

    def test_lost_target_keeps_worker_online_and_operator_can_cancel_before_commit(self):
        clients = self.clients
        request = self.loop.wire.request
        failures = {"begin","end"}

        def drop(method,path,*args,**kwargs):
            reply = request(method,path,*args,**kwargs)
            if "/admission-handoffs/" in path and args[0]["action"] in failures:
                action = args[0]["action"]
                failures.remove(action)
                if action=="begin":
                    self.ports[self.target].busy = unavailable
                raise ConnectionError("reply lost after the Router write")
            return reply

        def unavailable(*args,**kwargs):
            raise ConnectionError("target is off")

        self.loop.wire.request = drop
        with self.assertRaisesRegex(Fault,"native_switch_unconfirmed"):
            self.switch(clients,"om_failed_switch",self.target)
        handoff = self.status(clients)["native_switch"]["handoff_id"]
        for _ in range(4):
            self.clock.advance(20)
            self.service.step()
            status = self.loop.hermes.workers()["items"][0]
            self.assertTrue(status["online"])
            self.assertTrue(status["admission_paused"])
        path = "worker/handoff-admission"
        auth = clients["handoff/oguri"]
        cancel = {"action":"end","handoff_id":handoff}
        with self.assertRaisesRegex(Fault,"forbidden"):
            clients["platform/oguri"].request("PUT",path,cancel)
        with self.assertRaisesRegex(Fault,"native_switch_manages_handoff"):
            auth.request("PUT",path,dict(cancel,handoff_id=guid()))
        with self.assertRaisesRegex(Fault,"handoff_receipt_unconfirmed"):
            auth.request("PUT",path,cancel)
        self.assertEqual(self.status(clients)["native_switch"]["phase"],"cancelling")
        self.assertEqual(self.worker.native_session_pin,self.source)
        reopened,_,after = self.open_worker()
        self.assertEqual(reopened.native_session_pin,self.source)
        self.assertNotIn("native_switch",self.status(after))
        self.assertEqual(after["handoff/oguri"].request("PUT",path,cancel),{"handoff_id":handoff,"state":"released"})
        late = request("PUT",f"workers/oguri/admission-handoffs/{handoff}",
                       {"action":"begin","session_id":reopened.session})
        self.assertEqual(late["state"],"released")
        self.assertNotIn("binding_receipt",self.switch(after,"om_failed_switch",self.target))
        display = after["platform/oguri"].request("GET","worker/display")["items"]
        cancelled, = [item["event"] for item in display if item["event"]["event_id"]=="command-om_failed_switch"]
        self.assertEqual(cancelled["text"],"已取消切换，继续使用原会话。")
        self.assertEqual(self.message(after,guid())["session"],self.source)
        self.assertEqual(self.hosts[self.target].entries(),[])
        self.assertEqual(failures,set())

    def test_stale_router_session_keeps_original_platform_command_retryable(self):
        request = self.loop.wire.request
        allowed = False

        def router(method,path,*args,**kwargs):
            if "/admission-handoffs/" in path and not allowed:
                raise Fault(409,"stale_session")
            return request(method,path,*args,**kwargs)

        self.loop.wire.request = router
        with self.assertRaises(Fault) as pending:
            self.switch(self.clients,"om_resume_before_w01",self.target)
        self.assertEqual((pending.exception.status,pending.exception.code,pending.exception.retryable),
                         (503,"native_switch_unconfirmed",True))
        self.assertEqual(self.worker.native_session_pin,self.source)
        valid_op = self.status(self.clients)["native_switch"]["handoff_id"]
        allowed = True
        receipt = self.switch(self.clients,"om_resume_before_w01",self.target)
        self.assertEqual(receipt["binding_receipt"]["handoff_id"],valid_op)
        self.assertEqual(self.status(self.clients)["host_session"],self.target)
