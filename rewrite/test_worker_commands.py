from .feishu_output import Views
from . import worker_commands
from .storage import Fault, guid
from .test_feishu_inbound import PlatformCase
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock


class WorkerCommands(PlatformCase):
    def command(self, mid, text):
        return self.inbound.deliver(self.record(mid,"text",{"text":text}))

    def outputs(self):
        return [row["event"] for row in self.platform.request("GET","worker/display")["items"]
                if row["event"]["kind"]=="command"]

    def test_status_uses_link_observations_and_held_assignment_without_native_input(self):
        self.command("om_before","/status")
        self.assertIn("Router 待确认",self.outputs()[0]["text"])
        clock = Clock()
        self.loop.orders.clock = clock
        self.service.clock = clock
        tid = self.loop.hermes.create(guid(),"Assigned","Check status","oguri")["task_id"]
        self.service.step()
        clock.advance(10)
        self.service.step()
        self.service.step()
        hold = self.loop.worker.hold()
        self.assertEqual(tid,hold["held_task_id"])
        self.loop.native.is_busy = True
        clock.advance(5)
        self.service.step()
        self.command("om_status","/status")
        answer = self.outputs()[-1]
        for fact in ("Router 已连接","宿主适配器 已连接","宿主 忙碌",tid,hold["held_assignment_id"]):
            self.assertIn(fact,answer["text"])
        self.assertEqual(len(self.loop.native.entries()),1)
        self.assertEqual(self.loop.worker.hold(),hold)

    def test_lost_command_ack_replays_original_card_and_plain_text_stays_chat(self):
        mid = self.record("om_help","text",{"text":"/help"})
        request = self.platform.request
        def lose(*args,**kw):
            result = request(*args,**kw)
            if args[1]=="worker/command":
                raise ConnectionError("test acknowledgement lost")
            return result
        self.platform.request = lose
        with self.assertRaises(ConnectionError):
            self.inbound.deliver(mid)
        self.platform.request = request
        self.assertEqual(self.inbound.deliver(mid)["output"],{"event_id":"command-om_help","seq":1})
        sink = PlatformFixture()
        views = Views(self.loop.root/"views.db",self.platform,sink,"private-chat","小栗帽")
        self.assertEqual((views.poll(),views.poll()),(1,0))
        record, = sink.messages.values()
        self.assertEqual(record[2]["header"]["title"]["content"],"小栗帽-输出")
        self.assertEqual(self.loop.native.entries(),[])
        with self.assertRaisesRegex(Fault,"platform_command_conflict"):
            self.platform.request("POST","worker/command",{"message_id":mid,"text":"/model"})
        with self.assertRaisesRegex(Fault,"message_previously_command"):
            self.platform.request("POST","worker/chat",{"message_id":mid,"text":"/help"})
        self.command("om_path","/Users/example/notes.md")
        self.inbound.deliver(self.record("om_rich","post",{"content":[[{"tag":"text","text":"/help"}]]}))
        self.assertEqual([p["text"] for p,_ in self.loop.native.entries()],["/Users/example/notes.md","/help"])
        self.assertFalse(self.loop.hermes.tasks()["items"])

    def test_fixed_session_and_unimplemented_commands_are_explicit_and_role_bound(self):
        self.service.step()
        for n,text in enumerate(("/resume","/list","/switch 1","/resume search 其他","/bogus","/model sample")):
            self.command("om_case"+str(n),text)
        results = [e["text"] for e in self.outputs()]
        self.assertEqual(len(set(results[:3])),1)
        self.assertIn(self.loop.native.session,results[0])
        self.assertIn("敬请期待",results[3])
        self.assertIn("暂不开放",results[4])
        self.assertIn("敬请期待",results[5])
        self.assertFalse(self.loop.native.entries())
        with self.assertRaises(Fault) as denied:
            self.adapter.request("POST","worker/command",{"message_id":"om_denied","text":"/status"})
        self.assertEqual(denied.exception.status,403)

    def test_pre_upgrade_command_sent_as_chat_recovers_same_native_receipt(self):
        mid = self.record("om_prior","text",{"text":"/status"})
        previous = self.loop.worker.chat(mid,"/status")
        self.assertEqual(self.inbound.deliver(mid),previous)
        self.assertEqual(len(self.loop.native.entries()),1)
        self.assertEqual(self.outputs(),[])

    def test_compaction_with_no_native_receipt_times_out_without_a_second_start(self):
        self.service.step()
        self.command("om_unsupported_compact","/compact")
        self.assertIn("敬请期待",self.outputs()[-1]["text"])
        self.service.host_compaction = True
        requests = []
        def missing(session,command_id,*,start):
            requests.append(start)
            if start:
                raise ConnectionError("unconfirmed")
            return {"session":session,"command_id":command_id,"state":"missing"}
        self.loop.adapter.compact = missing
        mid = "om_unconfirmed_compact"
        original = self.command(mid,"/compact")
        with self.loop.worker.db.transaction() as db:
            db.execute("UPDATE platform_compactions SET deadline=0 WHERE message_id=?",(mid,))
        worker_commands.advance_compact(self.service)
        worker_commands.advance_compact(self.service)
        self.assertEqual(self.command(mid,"/compact"),original)
        self.assertEqual(requests,[True,False])
        self.assertEqual(sum("压缩结果待核" in e["text"] for e in self.outputs()),1)
        self.assertEqual(self.loop.native.entries(),[])
