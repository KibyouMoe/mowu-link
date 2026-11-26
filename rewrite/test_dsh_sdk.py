"""Optional real DSH SDK in an empty home. Only the agent and model events are scripted."""
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from . import worker_commands
from .devloop import Loop
from .dsh_port import DshHost, tool_listener
from .dsh_display import DshDisplay
from .feishu_inbound import Inbound
from .feishu_output import Views
from .host_wire import RemoteAdapter, serve_adapter
from .native_files import NativeFiles
from .storage import Fault, encode, guid
from .test_mainline import Clock
from .test_feishu_inbound import PNG
from .test_feishu_output import PlatformFixture
from .wire import Client
from .worker import Worker
from .worker_service import WorkerService

APP = Path("/Applications/DeepSeek Harness.app/Contents")
EXE = APP / "MacOS/DeepSeek Harness"
DRIVER = Path(__file__).parent / "integrations/dsh_host/sdk-fixture.mjs"


@unittest.skipUnless(EXE.is_file(),"installed macOS DSH SDK required")
class DshSdk(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-")))
        self.session = "isolated-dsh-session"
        env = {"ELECTRON_RUN_AS_NODE":"1","HOME":str(self.directory),"TMPDIR":str(self.directory),
               "XDG_CONFIG_HOME":str(self.directory),"PATH":os.defpath}
        self.error_log = self.enterContext((self.directory/"sdk.stderr").open("w+"))
        self.proc = subprocess.Popen([EXE,DRIVER.resolve(),APP/"Resources/app.asar/dsh",self.directory,self.session],
                                     env=env,cwd=self.directory,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                     stderr=self.error_log,text=True)
        self.addCleanup(self.stop_driver)
        self.ready = self.read()
        self.assertTrue(self.ready["ready"])
        self.assertEqual(set(self.ready["names"]),{"worker_report","worker_ask","worker_send_peer",
                                                   "worker_peer_status","worker_submit_material"})
        self.assertEqual(self.ready["globalNames"],[])
        self.clock = Clock()
        def host(_path,_name):
            port = DshHost(self.directory)
            port.session = self.session
            return port
        self.loop = self.enterContext(Loop.isolated(self.clock,host_factory=host))
        self.w,self.m = self.loop.worker,self.loop.hermes
        token = guid()
        platform = guid()
        self.service = WorkerService(self.w,self.clock)
        server = self.enterContext(self.service.listen({token:"adapter/oguri",platform:"platform/oguri"},("127.0.0.1",0),None))
        runner = Thread(target=server.serve_forever,kwargs={"poll_interval":0.02})
        runner.start()
        self.addCleanup(runner.join)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:"+str(server.server_port)
        worker_client = Client(url,token)
        self.loop.adapter.media_store = NativeFiles(self.directory/"media",worker_client)
        self.display = DshDisplay(self.loop.adapter,worker_client,"oguri")
        self.enterContext(tool_listener(self.directory,self.loop.adapter,worker_client))
        self.inbound = Inbound(self.directory/"feishu.db",Client(url,platform),None,"app","trainer","chat")

    def stop_driver(self):
        if self.proc.poll() is None:
            self.proc.stdin.write('{"action":"exit"}\n')
            self.proc.stdin.flush()
            self.proc.stdin.close()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
                self.fail("isolated DSH plugin did not shut down")
        if not self.proc.stdin.closed:
            self.proc.stdin.close()
        self.proc.stdout.close()
        self.assertEqual(self.proc.returncode,0,"isolated DSH SDK exited abnormally")

    def read(self):
        self.assertTrue(select.select([self.proc.stdout],[],[],10)[0],"SDK timed out")
        line = self.proc.stdout.readline()
        if not line:
            self.error_log.seek(0)
            self.fail(self.error_log.read())
        result = json.loads(line)
        self.assertNotIn("error",result)
        return result

    def drive(self, action, **body):
        self.proc.stdin.write(encode({"action":action,**body})+"\n")
        self.proc.stdin.flush()
        return self.read()

    def assign(self):
        tid = self.m.create(guid(),"DSH 隔离单","提交测试说明","oguri")["task_id"]
        self.w.connect()
        self.w.poll()
        self.clock.advance(10)
        self.w.poll()
        self.w.poll()
        aid = self.w.hold()["held_assignment_id"]
        self.assertTrue(aid)
        with self.loop.adapter.db.transaction() as db:
            row = db.execute("SELECT * FROM intents").fetchone()
        return tid,aid,row

    def call(self, name, call_id, args, append=True):
        return self.drive("tool",name=name,call_id=call_id,args=args,append=append)["result"]

    def test_native_input_written_before_failure_recovers_from_the_same_record(self):
        self.drive("lose-admission")
        mid = self.inbound.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":"om_lost",
                                  "message_type":"text","content":encode({"text":"核对回执"})})
        with self.assertRaisesRegex(Fault,"native_write_unconfirmed") as lost:
            self.inbound.deliver(mid)
        self.assertEqual(lost.exception.status,503)
        with self.loop.adapter.db.transaction() as db:
            pending = db.execute("SELECT * FROM intents").fetchone()
        native_id = self.loop.native.delivery_native_id(self.session,pending["delivery_id"],pending["digest"])
        with self.assertRaisesRegex(Fault,"native_turn_unconfirmed"):
            self.loop.adapter.prove_native_input(self.session,"wrong-turn",native_id)
        with self.loop.adapter.db.transaction() as db:
            self.assertIsNone(db.execute("SELECT receipt FROM intents").fetchone()[0])
        receipt = self.inbound.deliver(mid)
        self.assertEqual((receipt["session"],receipt["delivery_id"]),(self.session,"chat-"+mid))
        self.assertEqual(self.drive("count")["received"],1)
        self.assertFalse(self.loop.worker.hold()["held_task_id"])

    def remote_adapter(self, protected_read):
        credential, other = guid(),guid()
        listener = self.enterContext(serve_adapter(self.loop.adapter,"oguri",{credential:"worker/oguri",other:"worker/other"},
                                                   ("127.0.0.1",0),None))
        thread = Thread(target=listener.serve_forever,kwargs={"poll_interval":.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(listener.shutdown)
        url = "http://127.0.0.1:"+str(listener.server_port)
        with self.assertRaises(Fault) as denied:
            Client(url,other).request("GET","host/"+protected_read)
        self.assertEqual(denied.exception.status,403)
        self.w.adapter = RemoteAdapter(Client(url,credential))
        self.service.step()

    def platform_command(self,mid,command):
        self.inbound.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":mid,
            "message_type":"text","content":encode({"text":command})})
        self.inbound.deliver(mid)
        events = self.inbound.worker.request("GET","worker/display")["items"]
        return next(x["event"]["text"] for x in events if x["event"]["event_id"]=="command-"+mid)

    def test_context_command_reads_only_committed_usage_for_the_same_route(self):
        self.remote_adapter("context")
        def command(mid):
            return self.platform_command(mid,"/context")
        self.assertIn("还没有模型回包",command("om_empty"))
        self.drive("manual")
        self.drive("context",model="fixture",window=32768)
        self.drive("output",id="sample-one",content=[{"type":"text","text":"private body not for the card"}],
                   usage={"inputTokens":46,"outputTokens":5,"totalTokens":51})
        first = command("om_original")
        for part in ("fixture/fixture","输入 46","本次总用量 51","模型窗口 32768"):
            self.assertIn(part,first)
        self.assertNotIn("private body",first)
        self.drive("output",id="sample-two",model="other",content=[{"type":"text","text":"another answer"}])
        second = command("om_next")
        self.assertIn("fixture/other",second)
        self.assertEqual(second.count("未记录"),3)
        self.assertEqual(command("om_original"),first)
        self.assertEqual(self.drive("count")["received"],1)
        self.assertIsNone(self.w.hold()["held_task_id"])
        self.assertIn("暂未接入目标模式",self.platform_command("om_goal_unsupported","/goal"))
        self.assertIn("暂未接入目标模式",self.platform_command("om_pause_unsupported","/goal pause"))

    def test_model_and_effort_distinguish_live_selection_from_recorded_request(self):
        self.remote_adapter("models")
        with self.assertRaisesRegex(Fault,"wrong_native_session"):
            DshHost(self.directory).model_view("different-session","models")
        self.drive("manual")
        self.drive("request-header",model="route-1",effort="medium")
        self.drive("end")
        self.drive("select-model",model="fixture",effort="high")
        original = self.drive("model-records")
        self.assertEqual(original["records"],["request/header","model/selection"])
        model = self.platform_command("om_models","/model")
        effort = self.platform_command("om_efforts","/effort")
        for card in (model,effort):
            self.assertIn("会话当前选择 fixture/fixture · high",card)
            self.assertIn("最近记录的请求配置 fixture/route-1 · medium",card)
            self.assertNotIn("private provider details",card)
        self.assertIn("部分目录暂时无法读取",model)
        self.assertIn("第 1/2 页",model)
        self.assertIn("12. fixture/route-10",self.platform_command("om_more","/model page 2"))
        self.assertIn("模型默认思考强度 low",effort)
        self.assertIn("2. high",effort)
        sink = PlatformFixture()
        views = Views(self.directory/"selector-views.db",self.inbound.worker,sink,"chat","玉藻")
        views.poll()
        cards = [row[2] for row in sink.messages.values()]
        self.assertTrue(any(card["header"]["title"]["content"]=="玉藻-输出"
                            and card["elements"][0]["text"]["content"]==effort for card in cards))
        self.assertEqual(original,self.drive("model-records"))
        self.drive("select-text-only")
        self.assertEqual(effort,self.platform_command("om_efforts","/effort"))
        current = self.platform_command("om_efforts_next","/effort")
        self.assertIn("会话当前选择 fixture/text-only",current)
        self.assertIn("宿主未列出可选项",current)
        self.assertEqual(self.drive("count")["received"],1)

    def test_bound_model_change_persists_selection_and_does_not_replay_after_lost_reply(self):
        self.remote_adapter("models")
        self.drive("manual")
        self.drive("request-header",model="fixture",effort="low")
        self.assertIn("会话正在工作",self.platform_command("om_busy","/effort high"))
        self.drive("end")
        self.assertIn("该选项暂不可用",self.platform_command("om_invalid","/effort invalid"))
        self.assertEqual(self.drive("model-records")["records"],["request/header"])
        self.assertIn("本会话已切换为 fixture/fixture · high",
                      self.platform_command("om_high","/effort high"))
        proof = self.inbound.deliver("om_high")["model_receipt"]
        self.assertEqual(proof["session"],self.session)
        self.assertEqual(proof["selected"],{"provider":"fixture","model":"fixture","effort":"high"})
        self.assertIsInstance(proof["event_seq"],int)
        self.assertIn("本会话当前已选择 fixture/fixture · high",
                      self.platform_command("om_high_again","/effort high"))
        self.assertEqual(self.drive("model-records")["records"],["request/header","model/selection"])
        remote = self.w.adapter.model_change
        def lose_reply(*args):
            remote(*args)
            raise OSError("lost model selection receipt")
        self.w.adapter.model_change = lose_reply
        self.assertIn("切换结果待核",self.platform_command("om_lost","/model fixture/route-1"))
        self.w.adapter.model_change = remote
        self.assertEqual(self.drive("model-records")["selected"]["model"],"route-1")
        self.drive("select-model",model="fixture",effort="low")
        self.assertIn("切换结果待核",self.platform_command("om_lost","/model fixture/route-1"))
        final = self.drive("model-records")
        self.assertEqual(final["selected"],{"provider":"fixture","model":"fixture","reasoningEffort":"low"})
        self.assertEqual(final["records"].count("model/selection"),3)
        self.assertEqual(self.drive("count")["received"],1)
        self.assertIsNone(self.w.hold()["held_task_id"])

    def test_model_change_intent_survives_interruption_before_worker_records_receipt(self):
        self.remote_adapter("models")
        command = {"message_id":"om_restart_model","text":"/model fixture/route-3"}
        with patch.object(worker_commands,"record",side_effect=RuntimeError("worker interrupted")):
            with self.assertRaisesRegex(RuntimeError,"worker interrupted"):
                worker_commands.handle(self.service,command)
        self.assertEqual(self.drive("model-records")["selected"]["model"],"route-3")
        with self.assertRaisesRegex(Fault,"message_previously_command"):
            self.w.chat(command["message_id"],"same message, different path")
        with self.assertRaisesRegex(Fault,"platform_command_conflict"):
            worker_commands.handle(self.service,{**command,"text":"/status"})
        recovered = WorkerService(self.w,self.clock)
        self.drive("select-model",model="fixture",effort="high")
        pending = worker_commands.handle(recovered,command)
        self.assertEqual(worker_commands.handle(recovered,command),pending)
        self.assertNotIn("model_receipt",pending)
        self.assertEqual(self.drive("model-records")["selected"]["model"],"fixture")
        with self.w.db.transaction() as db:
            text = json.loads(db.execute(
                "SELECT payload FROM display_events WHERE event_id='command-om_restart_model'").fetchone()[0])["text"]
        self.assertIn("切换结果待核",text)

    def test_numbered_model_choices_use_the_last_published_page_and_effort_checks_its_route(self):
        self.remote_adapter("models")
        sink = PlatformFixture()
        views = Views(self.directory/"menu-views.db",self.inbound.worker,sink,"chat","玉藻")
        self.assertIn("没有该编号",self.platform_command("om_model_before","/model 12"))
        self.assertIn("第 1/2 页",self.platform_command("om_model_first","/model"))
        views.poll()
        second = self.platform_command("om_model_forward","/model next")
        self.assertIn("12. fixture/route-10",second)
        original = views.worker.request
        def lose_menu_receipt(method,path,*a,**kw):
            if path=="worker/model-menu-receipt":
                raise ConnectionError("Worker unavailable after Feishu accepted the card")
            return original(method,path,*a,**kw)
        with patch.object(views.worker,"request",side_effect=lose_menu_receipt):
            with self.assertRaises(ConnectionError):
                views.poll()
        card_id = sink.calls[-1][1]
        self.assertIn("尚未确认送达",self.platform_command("om_menu_unknown","/model 2"))
        views.poll()
        self.assertEqual(sink.calls.count(("send",card_id)),2)
        self.assertEqual([row[0] for row in sink.messages.values()].count(card_id),1)
        self.assertIn("第 1/2 页",self.platform_command("om_model_back","/model prev"))
        views.poll()
        self.inbound.worker.request("POST","worker/command",{"message_id":"om_model_forward","text":"/model next"})
        self.assertIn("没有该编号",self.platform_command("om_old_page","/model 12"))
        self.assertEqual(self.platform_command("om_model_forward_new","/model next"),second)
        views.poll()
        self.assertIn("本会话已切换为 fixture/route-10",self.platform_command("om_model_12","/model 12"))
        self.drive("select-model",model="fixture",effort="low")
        self.assertIn("2. high",self.platform_command("om_effort_old_menu","/effort"))
        views.poll()
        self.drive("select-model",model="route-1",effort="low")
        self.assertIn("打开菜单后模型已切换",self.platform_command("om_effort_stale","/effort 2"))
        self.assertEqual(self.drive("model-records")["selected"]["reasoningEffort"],"low")
        self.assertIn("2. high",self.platform_command("om_effort_new_menu","/effort"))
        views.poll()
        self.assertIn("已切换为 fixture/route-1 · high",self.platform_command("om_effort_2","/effort 2"))
        self.assertEqual(self.drive("count")["received"],0)

    def test_next_turn_queue_recovers_receipt_without_steering_or_replaying(self):
        self.drive("manual")
        self.service.step()
        mids = ("om_queue1","om_queue2")
        for mid in mids:
            self.inbound.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":mid,
                "message_type":"text","content":encode({"text":"/queue 下一回合\n"+mid})})
            self.assertEqual(self.inbound.deliver(mid)["message_id"],mid)
        self.clock.advance(5)
        self.service.step()
        self.assertEqual(self.drive("count")["received"],1)
        self.drive("end")
        reopened = Worker(self.w.db.path,"oguri",self.w.router,self.loop.adapter)
        service = WorkerService(reopened,self.clock)
        send = reopened.adapter.deliver
        def lost(did,body):
            send(did,body)
            raise OSError("worker missed native proof")
        reopened.adapter.deliver = lost
        service.step()
        self.assertEqual(self.drive("count")["received"],2)
        reopened.adapter.deliver = send
        self.clock.advance(5)
        service.step()
        self.assertEqual(self.drive("count")["received"],2)
        self.drive("end")
        self.clock.advance(5)
        service.step()
        with reopened.db.transaction() as db:
            rows = db.execute("SELECT * FROM queued_chats ORDER BY rowid").fetchall()
            recovered = db.execute("SELECT COUNT(*) FROM display_events WHERE event_id LIKE 'queue-recovered-%'").fetchone()[0]
        self.assertEqual(recovered,1)
        self.assertEqual([row["text"] for row in rows],["下一回合\n"+mid for mid in mids])
        proofs = [json.loads(row["receipt"]) for row in rows]
        self.assertEqual([p["delivery_id"] for p in proofs],["queued-chat-"+mid for mid in mids])
        self.assertEqual(len({p["turn_id"] for p in proofs}),2)
        self.assertTrue(all(p["session"]==self.session for p in proofs))
        self.assertEqual(self.drive("count")["received"],3)
        self.assertFalse(self.m.tasks()["items"])

    def test_queued_delivery_rejects_another_session_before_writing(self):
        with self.assertRaisesRegex(Fault,"queued_session_mismatch"):
            self.loop.adapter.deliver("queued-chat-1",{"kind":"queued_chat","session":"other","text":"no"})
        self.assertEqual(self.drive("count")["received"],0)

    def test_sdk_keeps_caption_image_and_generic_file_in_native_content(self):
        raw = b"# Independent DSH file\n"*16000
        self.inbound.resources = lambda mid,key,kind,limit: PNG if kind=="image" else raw
        def receive(mid,kind,content):
            self.inbound.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":mid,
                                "message_type":kind,"content":encode(content)})
            return self.inbound.deliver(mid)
        caption = "保留这一行和原图。"
        proof = receive("om_pic","post",{"content":[[{"tag":"text","text":caption},{"tag":"img","image_key":"img_x"}]]})
        self.assertEqual(proof["media_sha256"],[hashlib.sha256(PNG).hexdigest()])
        view = self.drive("inspect-media")
        self.assertEqual(view["id"],proof["native_id"])
        text,image = view["content"]
        self.assertEqual(text,{"type":"text","text":caption})
        self.assertEqual((image["type"],image["ref"]["width"],image["ref"]["height"]),("image",1,1))
        self.assertEqual(image["ref"]["attachmentId"],"sha256:"+image["sha256"])
        with self.loop.adapter.db.transaction() as db:
            db.execute("UPDATE intents SET receipt=NULL,native_id=NULL")
            payload = json.loads(db.execute("SELECT payload FROM intents").fetchone()[0])
        (self.directory/"media"/proof["media_sha256"][0]).unlink()
        self.assertEqual(self.loop.adapter.deliver("chat-om_pic",payload),proof)
        self.assertEqual(self.drive("count")["received"],1)
        second = receive("om_doc","file",{"file_name":"交接.md","file_key":"file_x"})
        file, = self.drive("inspect-media")["content"]
        self.assertEqual((file["ref"]["name"],file["type"],file["sha256"]),("交接.md","file",hashlib.sha256(raw).hexdigest()))
        self.assertEqual(second["media_sha256"],[file["sha256"]])

    def test_invalid_raster_and_text_only_model_do_not_receive_an_input(self):
        bad = PNG[:32]
        def receive(mid,raw):
            self.inbound.resources = lambda *args:raw
            self.inbound.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":mid,
                "message_type":"image","content":encode({"image_key":"img_"+mid})})
            return self.inbound.deliver(mid)
        with self.assertRaises(Fault):
            receive("om_broken",bad)
        self.drive("select-text-only")
        with self.assertRaisesRegex(Fault,"model_does_not_support_images"):
            receive("om_textmodel",PNG)
        self.assertEqual(self.drive("count")["received"],0)

    def test_public_log_to_feishu_cards_preserves_final_and_replays_lost_worker_ack(self):
        self.drive("manual")
        self.drive("output",id="msg1",content=[{"type":"text","text":"我先检查。"},
                   {"type":"tool-call","id":"call-1","name":"terminal","arguments":'{"command":"git status --private-example"}'}])
        self.drive("observed-tool",id="call-1",name="terminal",args={"command":"git status --private-example"})
        self.drive("output",id="msg2",content=[{"type":"text","text":"检查好了。"}])
        self.display.collect()
        send = self.display.worker.request
        def lose(*args,**kw):
            send(*args,**kw)
            raise OSError("display acknowledgement lost")
        self.display.worker.request = lose
        with self.assertRaises(OSError):
            self.display.flush()
        self.display.worker.request = send
        restored = DshDisplay(self.loop.adapter,self.display.worker,"oguri")
        restored.collect()
        restored.flush()
        sink = PlatformFixture()
        views = Views(self.directory/"views.db",self.inbound.worker,sink,"chat","玉藻")
        self.assertEqual(views.poll(),3)
        self.drive("end")
        restored.collect()
        restored.flush()
        self.assertEqual(views.poll(),1)
        self.assertEqual([r[2]["header"]["title"]["content"] for r in sink.messages.values()],
                         ["玉藻·原生输入","玉藻-输出","玉藻-工具","玉藻-回复"])
        bodies = [r[2]["elements"][0]["text"]["content"] for r in sink.messages.values()]
        self.assertEqual(bodies,["manual follow-up","我先检查。","terminal\ngit status","检查好了。"])
        self.assertEqual(views.poll(),0)
        self.assertFalse(self.m.tasks()["items"])

    def test_aborted_native_turn_and_protocol_input_never_become_a_final_or_a_report(self):
        self.w.chat("plain","在吗")
        self.drive("output",id="draft",content=[{"type":"text","text":"尚在处理"}])
        self.drive("end",aborted=True)
        tid,_,_ = self.assign()
        self.drive("output",id="unreported",content=[{"type":"text","text":"写在聊天里。"}])
        self.drive("end")
        for _ in range(3):
            self.display.collect()
            self.display.flush()
        rows = self.inbound.worker.request("GET","worker/display")["items"]
        observed = [(r["event"]["kind"],r["event"]["text"]) for r in rows if r["event"]["event_id"].startswith("host-dsh-")]
        self.assertEqual(observed,[("native_input","在吗"),("progress","尚在处理"),("final","写在聊天里。")])
        self.assertEqual(self.w.hold()["held_task_id"],tid)

    def test_committed_native_image_preserves_order_and_is_bound_to_the_public_record(self):
        self.inbound.resources = lambda *args:PNG
        self.inbound.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":"om_ref",
            "message_type":"image","content":encode({"image_key":"img_ref"})})
        self.inbound.deliver("om_ref")
        original, = self.drive("inspect-media")["content"]
        self.drive("output",id="with-image",content=[{"type":"text","text":"图前"},
                   {"type":"image","attachment":original["ref"]},{"type":"text","text":"图后"}])
        self.drive("end")
        self.display.collect()
        with self.display.db.transaction() as db:
            media = json.loads(db.execute("SELECT event FROM dsh_public_outbox WHERE event_id LIKE '%-image'").fetchone()[0])["image"]
        with self.assertRaisesRegex(Fault,"invalid_display_image"):
            self.loop.native.request("display-image",self.session,{"seq":media["seq"],"anchor":media["anchor"],
                               "index":0,"offset":0})
        with self.assertRaisesRegex(Fault,"display_history_changed"):
            self.loop.native.request("display-image",self.session,{"seq":media["seq"],"anchor":"mismatch",
                               "index":media["index"],"offset":0})
        self.display.flush()
        platform = PlatformFixture()
        views = Views(self.directory/"views.db",self.inbound.worker,platform,"chat","玉藻")
        self.assertEqual(views.poll(),3)
        before,image,after = platform.messages.values()
        self.assertEqual([before[1],image[1],after[1]],["interactive","image","interactive"])
        self.assertEqual(before[2]["elements"][0]["text"]["content"],"图前")
        self.assertEqual(after[2]["elements"][0]["text"]["content"],"图后")
        self.assertEqual(hashlib.sha256(platform.images[0]).hexdigest(),original["sha256"])
        self.display.collect()
        self.display.flush()
        self.assertEqual(views.poll(),0)

    def test_formal_receipt_recovered_from_sdk_log_and_registered_reports(self):
        tid,aid,row = self.assign()
        proof = json.loads(row["receipt"])
        self.assertEqual(proof["session"],self.session)
        self.assertEqual(self.drive("count")["received"],1)
        self.assertEqual(self.m.inspect(tid)["task"]["state"],"working")
        with self.loop.adapter.db.transaction() as db:
            db.execute("UPDATE intents SET receipt=NULL,native_id=NULL")
        self.assertEqual(self.loop.adapter.deliver(row["delivery_id"],json.loads(row["payload"])),proof)
        self.assertEqual(self.drive("count")["received"],1)
        ask = self.call("worker_ask","sdk-ask",{"text":"请核对","options":[]})
        self.assertFalse(ask["isError"],ask)
        args = {"task_id":tid,"assignment_id":aid,"summary":"测试驱动结果"}
        report = self.call("worker_report","sdk-report",args)
        self.assertFalse(report["isError"],report)
        self.assertEqual(report["value"]["release_assignment_id"],aid)
        self.assertEqual(self.call("worker_report","sdk-report",args,False)["value"],report["value"])
        self.assertIsNone(self.w.hold()["held_task_id"])
        self.assertEqual(self.m.inspect(tid)["task"]["state"],"completed_reported")
        frames = self.loop.manager_wire.frames(0)
        self.assertEqual(sum(f["payload"].get("text") == args["summary"] for f in frames),1)

    def test_cancel_stops_reporting_at_missing_native_cleanup_even_after_an_aborted_turn(self):
        tid,aid,_ = self.assign()
        self.drive("end",aborted=True)
        self.m.act(guid(),tid,self.m.inspect(tid)["task"]["revision"],"cancel",reason="隔离取消")
        self.w.poll()
        self.assertEqual(self.drive("count")["received"],2)
        params = {"task_id":tid,"assignment_id":aid,"outcome":"cancel_confirmed","summary":"测试停止"}
        for suffix in ("first","retry"):
            reply = self.call("worker_report","cleanup-"+suffix,params)
            self.assertTrue(reply["isError"],reply)
            self.assertIn("native_cleanup_unconfirmed",str(reply))
        details = self.m.inspect(tid)
        self.assertEqual((details["task"]["state"],details["pending_cleanup"][0]["assignment_id"]),("working",aid))
        self.assertIsNone(details["last_report"])
        self.assertEqual(self.w.hold()["held_assignment_id"],aid)
        sink = PlatformFixture()
        views = Views(self.directory/"cleanup-views.db",self.inbound.worker,sink,"chat","玉藻")
        views.poll()
        texts = [card["elements"][0]["text"]["content"] for _,_,card in sink.messages.values()]
        self.assertEqual(sum("原派发停止与清理仍待确认" in text for text in texts),1)
        self.assertFalse(any("Router 已收到原派发的清理确认" in text for text in texts))

    def test_native_stop_request_targets_only_the_admitted_assignment_turn(self):
        tid,aid,row = self.assign()
        original_turn = json.loads(row["receipt"])["turn_id"]
        self.m.act(guid(),tid,self.m.inspect(tid)["task"]["revision"],"cancel",reason="隔离取消")
        self.w.poll()
        self.w.poll()
        self.assertEqual(self.drive("stop-attempts")["stops"],[
            {"turn":1,"cause":{"kind":"user"},"options":{"keepInbox":True}}])
        with self.loop.adapter.db.transaction() as db:
            control = db.execute("SELECT * FROM intents WHERE json_extract(payload,'$.kind')='task.cancel'").fetchone()
        proof = json.loads(control["receipt"])
        self.assertNotEqual(proof["turn_id"],original_turn)
        with self.loop.adapter.db.transaction() as db:
            db.execute("UPDATE intents SET receipt=NULL,native_id=NULL WHERE delivery_id=?",(control["delivery_id"],))
        self.assertEqual(self.loop.adapter.deliver(control["delivery_id"],json.loads(control["payload"])),proof)
        self.assertEqual(len(self.drive("stop-attempts")["stops"]),1)
        self.assertEqual(self.w.hold()["held_assignment_id"],aid)

    def test_native_stop_does_not_interrupt_private_chat_after_assignment_turn(self):
        tid,aid,_ = self.assign()
        self.drive("end")
        self.w.chat("ordinary-side-chat","保留普通聊天")
        self.m.act(guid(),tid,self.m.inspect(tid)["task"]["revision"],"cancel",reason="隔离取消")
        self.w.poll()
        self.assertEqual(self.drive("stop-attempts")["stops"],[])
        self.assertEqual(self.w.hold()["held_assignment_id"],aid)

    def test_private_chat_and_mixed_input_cannot_report(self):
        tid,aid,_ = self.assign()
        self.drive("end")
        self.w.chat("dsh-chat","私聊")
        args = {"task_id":tid,"assignment_id":aid,"summary":"不可登记"}
        denied = self.call("worker_report","chat-tool",args)
        self.assertTrue(denied["isError"])
        self.assertIn("wrong_report_origin",str(denied))
        self.drive("manual")
        denied = self.call("worker_report","mixed-tool",args)
        self.assertTrue(denied["isError"])
        self.assertIn("ambiguous_native_turn",str(denied))
        self.assertEqual(self.w.hold()["held_assignment_id"],aid)
        self.assertIsNone(self.m.inspect(tid)["last_report"])
        with self.assertRaises(Fault) as denied:
            self.loop.native.busy("other")
        self.assertEqual(denied.exception.code,"wrong_native_session")
