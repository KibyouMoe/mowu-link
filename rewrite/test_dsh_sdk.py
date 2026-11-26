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

from .devloop import Loop
from .dsh_port import DshHost, tool_listener
from .feishu_inbound import Inbound
from .native_files import NativeFiles
from .storage import Fault, encode, guid
from .test_mainline import Clock
from .test_feishu_inbound import PNG
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
        self.assertEqual(set(self.ready["names"]),{"worker_report","worker_ask"})
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
