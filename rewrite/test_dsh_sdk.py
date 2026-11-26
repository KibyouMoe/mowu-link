"""Optional real DSH SDK in an empty home. Only the agent and model events are scripted."""
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
from .storage import Fault, encode, guid
from .test_mainline import Clock
from .wire import Client
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
        server = self.enterContext(WorkerService(self.w).listen({token:"adapter/oguri"},("127.0.0.1",0),None))
        runner = Thread(target=server.serve_forever,kwargs={"poll_interval":0.02})
        runner.start()
        self.addCleanup(runner.join)
        self.addCleanup(server.shutdown)
        self.enterContext(tool_listener(self.directory,self.loop.adapter,Client("http://127.0.0.1:"+str(server.server_port),token)))

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
