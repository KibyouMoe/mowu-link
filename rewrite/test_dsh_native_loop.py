"""Run the installed DSH AgentLoop with a local deterministic provider and a fresh profile."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import select
import subprocess
import tempfile
from threading import Thread
import unittest

from . import worker_commands
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
    def read_native(self,native,command=None):
        if command is not None:
            native.stdin.write(command+"\n")
            native.stdin.flush()
        self.assertTrue(select.select([native.stdout],[],[],10)[0],"DSH did not return a native observation")
        return json.loads(native.stdout.readline())

    @contextmanager
    def start_native(self,directory,session,mode):
        with subprocess.Popen([EXE,FIXTURE.resolve(),APP/"Resources/app.asar/dsh",directory,session,mode],
                              env=environment(directory),cwd=directory,stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as native:
            try:
                self.assertEqual(self.read_native(native),{"ready":True,"session":session})
                yield native
            finally:
                try:
                    tail,errors = native.communicate("exit\n",timeout=6)
                except subprocess.TimeoutExpired:
                    native.kill()
                    native.communicate()
                    raise
                self.assertEqual(native.returncode,0,tail+errors)

    def open_worker(self,service,identities):
        server = self.enterContext(service.listen(identities,("127.0.0.1",0),None))
        thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:"+str(server.server_port)

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
        client = Client(self.open_worker(service,{credential:"adapter/oguri"}),credential)
        self.enterContext(tool_listener(Path(directory),loop.adapter,client))
        router_requests = []
        with self.start_native(directory,session,"formal") as native:
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
            observed = self.read_native(native,"read")
            with loop.adapter.db.transaction() as db:
                intent = db.execute("SELECT * FROM intents WHERE json_extract(payload,'$.task_id')=?",(tid,)).fetchone()
            receipt = json.loads(intent["receipt"])
            # Reconstruct the host receipt after dropping the adapter's copy. DSH must not insert a new input.
            with loop.adapter.db.transaction() as db:
                db.execute("UPDATE intents SET receipt=NULL,native_id=NULL")
            self.assertEqual(loop.adapter.deliver(intent["delivery_id"],json.loads(intent["payload"])),receipt)
            self.assertEqual(self.read_native(native,"read"),observed)
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
        with loop.worker.db.transaction() as db:
            notice, = db.execute("SELECT payload FROM display_events WHERE event_id=?",
                                 ("task-"+receipt["delivery_id"],)).fetchall()
        self.assertIn(tid,json.loads(notice["payload"])["text"])
        notified = [f["delivery_id"] for f in loop.manager_wire.frames(0) if f["payload"].get("text")==report["summary"]]
        self.assertEqual(len(notified),1)
        self.evidence = {"native":observed,"input_receipt":receipt,"router":details,
                         "router_w04":request,"notification_delivery_ids":notified,"worker_after":loop.worker.hold(),
                         "boundary":"Native DSH AgentLoop, tool execution and real isolated Router/Worker; scripted local model"}

    def goal_worker(self,session):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-goals-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        service = WorkerService(loop.worker,clock)
        key = guid()
        platform = Client(self.open_worker(service,{key:"platform/oguri"}),key)
        def command(mid,text):
            receipt = platform.request("POST","worker/command",{"message_id":mid,"text":text})["receipt"]
            items = platform.request("GET","worker/display")["items"]
            event = next(item["event"] for item in items if item["seq"]==receipt["output"]["seq"])
            return receipt,event["text"]
        return directory,loop,service,command

    def test_goal_query_pause_and_resume_use_the_bound_native_goal(self):
        session = "dsh-real-loop-goals"
        directory,loop,service,command = self.goal_worker(session)
        with self.start_native(directory,session,"goals") as native:
            service.step()
            self.assertIn("没有目标",command("om_no_goal","/goal 状态")[1])
            self.assertIn("没有目标",command("om_no_pause","/goal 暂停")[1])
            self.assertIsNone(loop.native.goal_view(session)["goal"])
            request = "/goal 隔离持久目标\n保留 第二行"
            created,_ = command("om_create_goal",request)
            original = loop.native.goal_view(session)["goal"]
            self.assertEqual(original["objective"],"隔离持久目标\n保留 第二行")
            self.assertEqual(created["goal_receipt"]["id"],original["id"])
            self.assertEqual(command("om_create_goal",request)[0],created)
            self.assertIn("已有未完成",command("om_replace_goal","/goal 不能替换")[1])
            self.assertEqual(loop.native.goal_view(session)["goal"],original)
            current = command("om_goal","/goal")[1]
            self.assertIn(f"进行中 · 0/{original['limit']} 轮",current)
            self.assertIn("隔离持久目标",current)
            paused,text = command("om_pause","/goal 暂停")
            self.assertIn("已暂停",text)
            self.assertEqual(paused["goal_receipt"]["id"],original["id"])
            self.assertEqual(paused["goal_receipt"]["revision"],original["revision"]+1)
            change = loop.adapter.goal_change
            def lost(*args):
                change(*args)
                raise ConnectionError("lost native goal receipt")
            loop.adapter.goal_change = lost
            pending,text = command("om_resume","/goal 继续")
            self.assertIn("目标操作待核",text)
            self.assertNotIn("goal_receipt",pending)
            loop.adapter.goal_change = change
            self.assertEqual(command("om_resume","/goal 继续")[0],pending)
            self.assertEqual(command("om_pause","/goal 暂停")[0],paused)
            self.assertIn("续跑授权 已启用",command("om_goal_now","/goal")[1])
            self.assertEqual(loop.native.goal_view(session)["goal"]["revision"],original["revision"]+2)
            observed = self.read_native(native,"read")
            self.assertEqual(observed["inputs"],[])
            self.assertEqual(loop.hermes.tasks()["items"],[])

    def test_native_goal_rounds_pause_and_resume_without_becoming_router_orders(self):
        session = "dsh-real-goal-rounds"
        directory,loop,service,command = self.goal_worker(session)
        with self.start_native(directory,session,"goal-driver") as native:
            service.step()
            creation,_ = command("om_create_live_goal","/goal 推进测试目标")
            created = creation["goal_receipt"]
            self.assertTrue(self.read_native(native,"first-round")["running"])
            paused,card = command("om_pause_live_goal","/goal pause")
            self.assertIn("已暂停 · 1/2 轮",card)
            before = self.read_native(native,"read")
            self.assertEqual([row["round"] for row in before["rounds"]],[1])
            self.assertEqual([row["reason"]["kind"] for row in before["turns"]],["aborted"])
            resumed,card = command("om_resume_live_goal","/goal resume")
            self.assertIn("进行中",card)
            after = self.read_native(native,"second-round")
            self.assertEqual([row["id"] for row in after["rounds"]],[created["id"]]*2)
            self.assertEqual([row["round"] for row in after["rounds"]],[1,2])
            self.assertEqual([row["revision"] for row in after["rounds"]],
                             [created["revision"],resumed["goal_receipt"]["revision"]])
            self.assertEqual([row["reason"]["kind"] for row in after["turns"]],["aborted","completed"])
            self.assertEqual(command("om_pause_live_goal","/goal pause")[0],paused)
            self.assertEqual(command("om_resume_live_goal","/goal resume")[0],resumed)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.assertIsNone(loop.worker.hold()["held_task_id"])
            self.assertEqual(loop.manager_wire.frames(0),[])
            self.evidence = {"created":created,"paused":paused["goal_receipt"],"resumed":resumed["goal_receipt"],
                             "rounds":after["rounds"],"turns":after["turns"],
                             "router_tasks":0,"router_notifications":0,"worker_hold":loop.worker.hold(),
                             "boundary":"Native DSH goal service and continuation driver; local scripted provider"}

    def test_compact_reconciles_a_lost_start_reply_against_the_native_session(self):
        session = "dsh-real-compact"
        directory,loop,service,command = self.goal_worker(session)
        with self.start_native(directory,session,"compact") as native:
            service.step()
            self.assertTrue(service.host_compaction)
            command("om_empty","/compact")
            original = loop.adapter.compact
            def find_result(message_id):
                with loop.worker.db.transaction() as db:
                    row = db.execute("SELECT * FROM platform_compactions WHERE message_id=?",(message_id,)).fetchone()
                    notices = db.execute("SELECT payload FROM display_events WHERE event_id=?",
                                         ("compact-result-"+message_id,)).fetchall()
                return dict(row),[json.loads(item[0])["text"] for item in notices]
            worker_commands.advance_compact(service)
            empty,_ = find_result("om_empty")
            self.assertEqual(json.loads(empty["native_receipt"]),{"state":"empty"})
            for index in range(3):
                loop.worker.chat(guid(),f"Fixture paragraph {index}. "+"Many words to compress. "*120)
                self.read_native(native,"read")
            attempts = []
            def lost(session,command_id,*,start):
                result = original(session,command_id,start=start)
                if start:
                    attempts.append(command_id)
                    raise ConnectionError("native compact started, reply lost")
                return result
            loop.adapter.compact = lost
            receipt,text = command("om_compact","/compact")
            self.assertIn("等待宿主",text)
            in_progress = self.read_native(native,"first-compact")
            self.assertTrue(in_progress["compacting"])
            in_progress["adapter_busy"] = loop.adapter.busy()
            self.assertTrue(in_progress["adapter_busy"])
            worker_commands.advance_compact(service)
            self.assertEqual(find_result("om_compact")[1],[])
            self.assertEqual(command("om_compact","/compact")[0],receipt)
            self.assertIn("仍待宿主确认",command("om_compact_again","/compact")[1])
            observed = self.read_native(native,"finish-compact")
            self.assertFalse(loop.adapter.busy())
            recovered_service = WorkerService(loop.worker,service.clock)
            recovered_service.step()
            service.clock.advance(5)
            recovered_service.step()
            row,notices = find_result("om_compact")
            self.assertEqual(len(notices),1)
            self.assertIn("宿主已压缩",notices[0])
            self.assertEqual(attempts,[row["command_id"]])
            proof = json.loads(row["native_receipt"])
            events = observed["compactEvents"]
            self.assertEqual([entry["kind"] for entry in events],
                             ["compaction/start","compaction/summary","user/message","compaction/end"])
            self.assertEqual({entry["sourceCommandId"] for entry in events},set(attempts))
            self.assertEqual({entry["compactionId"] for entry in events},{proof["compaction_id"]})
            self.assertEqual(events[-1]["seq"],proof["end_seq"])
            self.assertEqual([item["command"] for item in observed["modelRequests"]],["private"]*3+["compaction"])
            self.assertEqual(len(observed["inputs"]),3)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.evidence = {"session":session,"compaction":proof,"events":events,"result_count":len(notices),
                             "start_attempts":len(attempts),"native_in_progress":in_progress,
                             "native_model_requests":observed["modelRequests"],"router_tasks":0,
                             "boundary":"Native DSH basic compaction engine and persisted surface, scripted local provider"}
