"""Run the installed DSH AgentLoop with a local deterministic provider and a fresh profile."""
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
from .storage import guid
from .test_mainline import Clock
from .test_dsh_sdk import APP, EXE
from .wire import Client
from .worker_service import WorkerService


FIXTURE = Path(__file__).parent/"integrations/dsh_host/native-loop-fixture.mjs"


def environment(directory):
    return {"ELECTRON_RUN_AS_NODE":"1","HOME":directory,"TMPDIR":directory,
            "XDG_CONFIG_HOME":directory,"PATH":os.defpath}


def observe():
    with tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-loop-") as directory:
        result = subprocess.run([EXE,FIXTURE.resolve(),APP/"Resources/app.asar/dsh",directory,"dsh-isolated-real-loop"],
                                env=environment(directory),cwd=directory,timeout=12,text=True,capture_output=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)


@unittest.skipUnless(EXE.is_file(),"installed macOS DSH SDK required")
class NativeLoop(unittest.TestCase):
    def test_original_turn_stops_queued_chat_survives_and_native_request_uses_new_effort(self):
        observed = observe()
        self.assertEqual(observed["turns"],[[1,"aborted","user"],[2,"completed",None],[3,"completed",None]])
        original, control = observed["original"],observed["control"]
        self.assertEqual(original["session"],control["session"])
        self.assertNotEqual(original["turn_id"],control["turn_id"])
        self.assertEqual(observed["admitted"],[original["native_id"],"keep-private",control["native_id"]])
        self.assertEqual([(r["command"],r["reasoningEffort"]) for r in observed["modelRequests"]],
                         [("task","high"),("private","high"),("task.cancel","high")])

    def test_native_tool_executor_files_w04_through_the_worker(self):
        session = "dsh-real-loop-formal"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-formal-"))
        class LateDeliveryReceipt(DshHost):
            def deliver(self,actual_session,did,payload,digest):
                proof = super().deliver(actual_session,did,payload,digest)
                return None if payload["kind"]=="task" else proof
        def host(_path,_name):
            port = LateDeliveryReceipt(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        credential = guid()
        service = WorkerService(loop.worker,clock)
        server = self.enterContext(service.listen({credential:"adapter/oguri"},("127.0.0.1",0),None))
        thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        client = Client("http://127.0.0.1:"+str(server.server_port),credential)
        self.enterContext(tool_listener(Path(directory),loop.adapter,client))
        router_requests = []
        with subprocess.Popen([EXE,FIXTURE.resolve(),APP/"Resources/app.asar/dsh",directory,session,"formal"],
                              env=environment(directory),cwd=directory,stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as native:
            try:
                self.assertTrue(select.select([native.stdout],[],[],10)[0],"DSH did not start")
                self.assertEqual(json.loads(native.stdout.readline()),{"ready":True,"session":session})
                tid = loop.hermes.create(guid(),"Native pipeline test","Report a local fixture result","oguri")["task_id"]
                send = loop.worker.router.request
                def observe_report(method,path,*args,**kwargs):
                    if path!=f"tasks/{tid}/report":
                        return send(method,path,*args,**kwargs)
                    before = {"router":loop.hermes.inspect(tid)["task"],"worker":loop.worker.hold()}
                    proof = send(method,path,*args,**kwargs)
                    router_requests.append({"before":before,"receipt":proof})
                    return proof
                loop.worker.router.request = observe_report
                loop.worker.connect()
                loop.worker.poll()
                clock.advance(10)
                loop.worker.poll()
                loop.worker.poll()
                native.stdin.write("read\n")
                native.stdin.flush()
                self.assertTrue(select.select([native.stdout],[],[],10)[0],"DSH native turn did not settle")
                observed = json.loads(native.stdout.readline())
                with loop.adapter.db.transaction() as db:
                    intent = db.execute("SELECT * FROM intents WHERE json_extract(payload,'$.task_id')=?",(tid,)).fetchone()
                receipt = json.loads(intent["receipt"])
                # Reconstruct the host receipt after dropping the adapter's copy. DSH must not insert a new input.
                with loop.adapter.db.transaction() as db:
                    db.execute("UPDATE intents SET receipt=NULL,native_id=NULL")
                self.assertEqual(loop.adapter.deliver(intent["delivery_id"],json.loads(intent["payload"])),receipt)
                native.stdin.write("read\n")
                native.stdin.flush()
                self.assertTrue(select.select([native.stdout],[],[],10)[0],"DSH replay state unavailable")
                self.assertEqual(json.loads(native.stdout.readline()),observed)
            finally:
                try:
                    tail,errors = native.communicate("exit\n",timeout=6)
                except subprocess.TimeoutExpired:
                    native.kill()
                    native.communicate()
                    raise
                self.assertEqual(native.returncode,0,tail+errors)
        details = loop.hermes.inspect(tid)
        self.assertEqual(details["task"]["state"],"completed_reported")
        self.assertEqual(observed["inputs"],[receipt["native_id"]])
        report = details["last_report"]
        aid = report["assignment_id"]
        self.assertEqual(json.loads(intent["payload"])["assignment_id"],aid)
        self.assertEqual([r["name"] for r in observed["calls"]],["worker_report","worker_report"])
        self.assertEqual([json.loads(r["arguments"])["assignment_id"] for r in observed["calls"]],[aid,aid])
        expected = {"recorded":True,"task_state":"completed_reported","release_assignment_id":aid}
        request, = router_requests
        self.assertEqual(request["before"]["router"]["state"],"working")
        self.assertEqual(request["before"]["worker"],{"held_task_id":tid,"held_assignment_id":aid})
        self.assertEqual(request["receipt"],expected)
        self.assertEqual([r["toolCallId"] for r in observed["results"]],[c["callId"] for c in observed["calls"]])
        self.assertEqual([r["isError"] for r in observed["results"]],[False,False],observed["results"])
        self.assertEqual([json.loads(r["content"][0]["text"]) for r in observed["results"]],[expected,expected])
        self.assertEqual(loop.worker.hold(),{"held_task_id":None,"held_assignment_id":None})
        notified = [f["delivery_id"] for f in loop.manager_wire.frames(0) if f["payload"].get("text")==report["summary"]]
        self.assertEqual(len(notified),1)
        self.evidence = {"native":observed,"input_receipt":receipt,"router":details,
                         "router_w04":request,"notification_delivery_ids":notified,"worker_after":loop.worker.hold(),
                         "boundary":"Native DSH AgentLoop, tool execution and real isolated Router/Worker; scripted local model"}
