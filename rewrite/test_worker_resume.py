"""Platform-command handoff, using the real Router and separate native receipt ledgers."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import unittest

from .adapter import HostAdapter
from .devloop import FixtureHost, Loop
from .feishu_client import receive
from .feishu_inbound import Inbound
from .feishu_output import Views
from .host_wire import RemoteAdapter, serve_adapter
from .storage import Fault, encode, guid
from .test_feishu_output import PlatformFixture
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

    def test_number_uses_the_confirmed_catalog_and_old_card_cannot_redirect_it(self):
        source_host = self.hosts[self.source]
        entries = [{"id":self.source,"title":"Shared title"},
                   *({"id":f"cold-{i}","title":"Shared title" if i==0 else f"Cold record {i}"} for i in range(9)),
                   {"id":self.target,"title":"Selected  test target"}]
        source_host.capabilities = lambda session: {"session_catalog":True}
        source_host.session_catalog = lambda session: {"session":session,"items":list(entries),"unavailable":0}
        self.clock.advance(5)
        self.service.step()
        platform = self.clients["platform/oguri"]
        path = self.loop.root/"platform.db"
        inbound = Inbound(path,platform,None,"app","trainer","chat",clock=self.clock)
        sink = PlatformFixture()
        views = Views(path,platform,sink,"chat","小栗帽",self.clock)

        def event(mid,text,parent=None):
            return SimpleNamespace(header=SimpleNamespace(app_id="app"),event=SimpleNamespace(
                sender=SimpleNamespace(sender_type="user",sender_id=SimpleNamespace(open_id="trainer")),
                message=SimpleNamespace(chat_id="chat",chat_type="p2p",message_id=mid,message_type="text",
                                        content=encode({"text":text}),parent_id=parent)))

        def send(mid,text,parent=None):
            return inbound.deliver(receive(inbound,event(mid,text,parent)))

        def view(receipt):
            with views.db.transaction() as db:
                row = db.execute("SELECT mid,event FROM view_attempt WHERE seq=?",(receipt["output"]["seq"],)).fetchone()
            return row["mid"],json.loads(row["event"])["text"]

        def output(receipt):
            entries = platform.request("GET","worker/display")["items"]
            return next(item["event"]["text"] for item in entries if item["seq"]==receipt["output"]["seq"])

        first = send("om_catalog_start","/resume")
        self.assertIn("0. Shared title",output(first))
        route = platform.request
        delay = True

        def lose_ack(method,path,*args,**kwargs):
            nonlocal delay
            if path=="worker/session-menu-receipt" and delay:
                delay = False
                raise ConnectionError("card created but Worker did not receive the menu acknowledgement")
            return route(method,path,*args,**kwargs)

        platform.request = lose_ack
        with self.assertRaises(ConnectionError):
            views.poll()
        self.assertIn("尚未确认送达",output(send("om_unconfirmed_menu","/resume 1")))
        self.assertEqual(self.status(self.clients)["host_session"],self.source)
        views.poll()
        first_id,_ = view(first)
        self.assertEqual(sink.calls.count(("send",first_id)),1)
        self.assertEqual(sum(value[0]==first_id for value in sink.messages.values()),1)
        self.assertIn("仅可浏览",output(first))
        self.assertIn("暂未配置",output(send("om_browsable_only","/resume 1",first_id)))
        self.assertIn("没有该编号",output(send("om_hidden_number","/resume 10")))
        self.assertIn("同名会话",output(send("om_ambiguous_title","/resume Shared title")))

        entries.insert(0,entries.pop())
        second = send("om_catalog_next","/resume next")
        self.assertIn("10. Selected  test target",output(second))
        views.poll()
        second_id,_ = view(second)
        self.assertIn("没有该编号",output(send("om_reply_wrong_page","/resume 10",first_id)))
        refreshed = send("om_catalog_refresh","/resume")
        self.assertIn("0. Selected  test target",output(refreshed))
        self.assertEqual(route("POST","worker/session-menu-receipt",{
            "seq":second["output"]["seq"],"platform_message_id":second_id}),{"recorded":True,"seq":second["output"]["seq"]})
        self.assertIn("已失效",output(send("om_stale_catalog","/resume 10",second_id)))
        self.assertIn("尚未确认送达",output(send("om_unacked_refresh","/resume 0")))
        self.assertEqual(self.status(self.clients)["host_session"],self.source)
        self.assertEqual(self.hosts[self.target].entries(),[])
        views.poll()
        new_id,_ = view(refreshed)
        result = send("om_number_switch","/resume 0",new_id)
        self.assertEqual(result["binding_receipt"]["session"],self.target)
        with self.assertRaisesRegex(Fault,"platform_message_conflict"):
            receive(inbound,event("om_number_switch","/resume 0",second_id))
        self.assertEqual(self.switch(self.clients,"om_back_after_number",self.source)["binding_receipt"]["session"],self.source)
        self.assertEqual(send("om_number_switch","/resume 0",new_id),result)
        self.assertIn("请先",output(send("om_number_after_return","/resume 0")))
        self.assertEqual(self.status(self.clients)["host_session"],self.source)
        title = send("om_title_switch",'/resume "Selected  test target"')
        self.assertEqual(title["binding_receipt"]["session"],self.target)
        self.assertEqual(self.hosts[self.source].entries(),[])
        self.assertEqual(self.hosts[self.target].entries(),[])
        self.assertEqual(self.message(self.clients,guid())["session"],self.target)
        self.evidence = {"first_page":first_id,"second_page":second_id,"refreshed_page":new_id,
                         "first_card_send_attempts":2,"unique_first_card":1,"old_card_selection_refused":True,
                         "number_receipt":result["binding_receipt"],"title_receipt":title["binding_receipt"]}

    def test_cold_target_with_active_goal_keeps_source_even_when_release_reply_is_lost(self):
        target = self.ports[self.target]
        describe = target.session_info
        target.session_info = lambda: dict(describe(),native_load_state="cold")
        activations = []

        def reject(session,operation_id):
            self.assertEqual(session,self.target)
            self.assertTrue(self.loop.hermes.workers()["items"][0]["admission_paused"])
            activations.append(operation_id)
            raise Fault(409,"native_cold_goal_active")

        target.activate = reject
        router = self.loop.wire.request
        lose = True

        def lose_release(method,path,*args,**kwargs):
            nonlocal lose
            result = router(method,path,*args,**kwargs)
            if lose and "/admission-handoffs/" in path and args[0]["action"]=="end":
                lose = False
                raise ConnectionError("release accepted but its reply was lost")
            return result

        self.loop.wire.request = lose_release
        with self.assertRaisesRegex(Fault,"native_switch_unconfirmed"):
            self.switch(self.clients,"om_refused_cold_goal",self.target)
        self.assertEqual(self.status(self.clients)["native_switch"]["phase"],"goal_active")
        self.assertEqual(self.worker.native_session_pin,self.source)
        self.assertFalse(self.loop.hermes.workers()["items"][0].get("admission_paused",False))
        self.service.step()
        receipt = self.switch(self.clients,"om_refused_cold_goal",self.target)
        self.assertNotIn("binding_receipt",receipt)
        events = self.clients["platform/oguri"].request("GET","worker/display")["items"]
        refusal, = [item["event"]["text"] for item in events if item["seq"]==receipt["output"]["seq"]]
        self.assertIn("正在进行的原生目标",refusal)
        self.assertEqual(len(activations),1)
        self.assertEqual(self.status(self.clients)["host_session"],self.source)
        self.assertEqual(self.message(self.clients,guid())["session"],self.source)
        self.assertEqual(self.hosts[self.target].entries(),[])
