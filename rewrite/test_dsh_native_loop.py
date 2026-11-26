"""Run the installed DSH AgentLoop with a local deterministic provider and a fresh profile."""
import hashlib
import json
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import select
import subprocess
import tempfile
from threading import Event, Thread
import time
import unittest

from . import test_codex_app_server as codex_driver, worker_commands
from .adapter import HostAdapter
from .codex_app_server import AppServerHost, worker_callbacks
from .devloop import FixtureHost, Loop
from .dsh_display import DshDisplay
from .dsh_port import DshHost, tool_listener
from .feishu_inbound import Inbound
from .feishu_output import Views
from .host_wire import RemoteAdapter, serve_adapter
from .native_files import NativeFiles
from .question_cards import Actions
from .storage import Fault, encode, guid
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .test_dsh_sdk import APP, EXE
from .test_feishu_inbound import PNG
from .wire import Client
from .worker import Worker
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

    def test_closed_native_binding_never_resumes_or_sends_to_a_new_agent(self):
        session = "dsh-binding-close"
        with tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-binding-") as directory:
            with self.start_native(directory,session,"binding-close") as native:
                host = DshHost(Path(directory))
                self.assertFalse(host.busy(session))
                closed = self.read_native(native,"close")
                self.assertEqual(closed,{"controllerResolutions":1,"live":False,"sameAgent":False,"inputs":0})
                for operation in ("activity","display-start","session-catalog"):
                    with self.assertRaisesRegex(Fault,"native_agent_unavailable") as error:
                        host.request(operation,session)
                    self.assertEqual(error.exception.status,503)
                self.assertEqual(self.read_native(native,"status"),closed)
                reopened = self.read_native(native,"reopen")
                self.assertEqual(reopened,{"controllerResolutions":2,"live":True,"sameAgent":False,"inputs":0})
                payload = {"kind":"chat","text":"Must not reach a replacement agent","message_id":"closed-test"}
                digest = hashlib.sha256(encode(payload).encode()).hexdigest()
                with self.assertRaisesRegex(Fault,"native_agent_unavailable"):
                    host.deliver(session,"chat-closed-test",payload,digest)
                self.assertEqual(self.read_native(native,"status"),reopened)

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

    def goal_worker(self,session,extra_workers=()):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-goals-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host,extra_workers=extra_workers))
        service = WorkerService(loop.worker,clock)
        key = guid()
        platform = Client(self.open_worker(service,{key:"platform/oguri"}),key)
        def command(mid,text):
            receipt = platform.request("POST","worker/command",{"message_id":mid,"text":text})["receipt"]
            items = platform.request("GET","worker/display")["items"]
            event = next(item["event"] for item in items if item["seq"]==receipt["output"]["seq"])
            return receipt,event["text"]
        return directory,loop,service,command

    def test_native_w04_stages_private_artifact_before_sharing_it_with_manager(self):
        session = "dsh-formal-artifact"
        directory,loop,service,_command = self.goal_worker(session)
        token = guid()
        client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
        self.enterContext(tool_listener(Path(directory),loop.adapter,client))
        raw = bytes(range(256))*2200
        digest = hashlib.sha256(raw).hexdigest()
        route = "worker/media/uploads"
        uploaded = client.request("POST",route,{"name":"sample.bin","sha256":digest,"size_bytes":len(raw)},guid())
        local = client.request("PATCH",route+"/"+uploaded["upload_id"],raw,headers={"Upload-Offset":"0"})["attachment_id"]
        transfer = loop.worker.router.request
        events = []
        with self.start_native(directory,session,"formal-artifacts") as native:
            tid = loop.hermes.create(guid(),"Native artifact test",local,"oguri")["task_id"]
            def fail_final_file_ack(method,path,*args,**kwargs):
                if path==f"tasks/{tid}/report":
                    artifact, = args[0]["artifact_ids"]
                    with self.assertRaisesRegex(Fault,"attachment_not_found"):
                        loop.manager_wire.request("GET","attachments/"+artifact)
                    self.assertEqual(transfer("GET","attachments/"+artifact)["bytes"],raw)
                    self.assertEqual(loop.worker.hold()["held_task_id"],tid)
                    events.append(("report",artifact))
                result = transfer(method,path,*args,**kwargs)
                if method=="PATCH" and path.startswith("uploads/"):
                    events.append(("chunk",result["next_offset"]))
                    if result["complete"]:
                        raise ConnectionError("isolated final file receipt lost")
                return result
            loop.worker.router.request = fail_final_file_ack
            loop.worker.connect()
            loop.worker.poll()
            service.clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            observed = self.read_native(native,"read")
        self.assertEqual([call["name"] for call in observed["calls"]],
                         ["worker_stage_artifacts","worker_stage_artifacts","worker_report"])
        self.assertEqual([result["isError"] for result in observed["results"]],[True,False,False])
        self.assertEqual(len(observed["inputs"]),1)
        _stage, staged, report = observed["results"]
        artifact, = json.loads(staged["content"][0]["text"])["artifact_ids"]
        self.assertEqual(events,[("chunk",256*1024),("chunk",512*1024),("chunk",len(raw)),("report",artifact)])
        details = loop.hermes.inspect(tid)
        self.assertEqual(details["last_report"]["artifact_ids"],[artifact])
        self.assertEqual(details["task"]["state"],"completed_reported")
        self.assertIsNone(loop.worker.hold()["held_assignment_id"])
        self.assertEqual(loop.manager_wire.request("GET","attachments/"+artifact)["bytes"],raw)
        notify, = loop.manager_wire.frames(0)
        self.assertEqual(notify["payload"]["attachment_ids"],[artifact])
        self.evidence = {"session":session,"native_input":observed["inputs"][0],"calls":observed["calls"],
                         "report":json.loads(report["content"][0]["text"]),"task_id":tid,"artifact_id":artifact,
                         "local_artifact_id":local,"sha256":digest,"bytes":len(raw),"transfer":events,
                         "manager_notification":notify["delivery_id"],
                         "boundary":"Actual DSH AgentLoop and isolated Router, scripted model, no Feishu"}

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

    def test_dsh_native_timed_question_closes_the_original_feishu_test_card(self):
        session = "dsh-real-timed-question"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-questions-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        service = WorkerService(loop.worker,clock)
        platform_key,adapter_key = guid(),guid()
        url = self.open_worker(service,{platform_key:"platform/oguri",adapter_key:"adapter/oguri"})
        platform = Client(url,platform_key)
        relay = DshDisplay(loop.adapter,Client(url,adapter_key),"oguri")
        inbound = Inbound(loop.root/"feishu.db",platform,None,"app","trainer","test-chat")
        sink = PlatformFixture()
        views = Views(loop.root/"views.db",platform,sink,"test-chat","小栗帽",clock)
        actions = Actions(views,inbound)
        completed = []
        writes = []
        with self.start_native(directory,session,"questions") as native:
            relay.collect()
            resolve = loop.native.resolve_question
            def lose(original,answer):
                proof = resolve(original,answer)
                writes.append(original["native_question_id"])
                if answer["mode"]=="choice":
                    raise Fault(503,"adapter_connection_unconfirmed",True)
                return proof
            loop.native.resolve_question = lose
            for mode,form,options in (
                ("choice",None,{"option_id":"2"}),("text",{"reply":"C"},{}),("skip",None,{})):
                loop.worker.chat(guid(),"Question fixture "+mode)
                opened = self.read_native(native,"read")
                call_id = "native-question-"+mode
                self.assertIn(call_id,opened["continued"])
                for _ in range(4):
                    relay.collect()
                    relay.flush()
                views.poll()
                with views.db.transaction() as db:
                    row = db.execute("""SELECT qid,mid,question FROM question_cards
                                      WHERE json_extract(question,'$.text')=?""",("Pick "+mode+"?",)).fetchone()
                self.assertIsNotNone(row)
                qid,mid,raw = row
                original = json.loads(raw)
                self.assertIsNone(loop.native.lookup_answer(session,"0/0",call_id))
                value = {"question_id":qid,"mode":mode,**options}
                with self.assertRaisesRegex(Fault,"wrong_card_actor"):
                    actions.record("app","other","test-chat",mid,value,form)
                with self.assertRaisesRegex(Fault,"not_original_trainer_card"):
                    actions.record("app","trainer","test-chat","om_other",value,form)
                self.assertEqual(actions.record("app","trainer","test-chat",mid,value,form),"pending")
                actions.flush()
                observed = self.read_native(native,"read")
                self.assertNotIn(call_id,observed["continued"])
                matching = [row for row in observed["replies"] if row["call"]==call_id]
                self.assertEqual(len(matching),1)
                found, = matching
                if mode=="choice":
                    self.assertEqual(actions.record("app","trainer","test-chat",mid,value,form),"pending")
                    clock.advance(16)
                    actions.flush()
                views.poll()
                self.assertEqual(sink.calls[-1],("patch",mid))
                self.assertEqual(actions.record("app","trainer","test-chat",mid,value,form),"confirmed")
                with loop.worker.db.transaction() as db:
                    proof = json.loads(db.execute("SELECT proof FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()[0])
                self.assertEqual((proof["receipt_id"],proof["turn_id"]),(found["id"],original["turn_id"]))
                completed.append({"platform_message":mid,"question_id":qid,"proof":proof,"native_reply":found})
            loop.worker.chat(guid(),"Question fixture multi")
            rejected = self.read_native(native,"read")
            self.assertIn("native-question-multi",rejected["continued"])
            for _ in range(4):
                relay.collect()
                relay.flush()
            views.poll()
            with views.db.transaction() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM question_cards").fetchone()[0],3)
            loop.worker.chat(guid(),"Question fixture native")
            self.assertIn("native-question-native",self.read_native(native,"read")["continued"])
            original_request = relay.worker.request
            dropped = []
            def lose_open(*args):
                result = original_request(*args)
                if args[1]=="worker/native-question":
                    dropped.append(result["question_id"])
                    raise Fault(503,"adapter_connection_unconfirmed",True)
                return result
            relay.worker.request = lose_open
            with self.assertRaisesRegex(Fault,"adapter_connection_unconfirmed"):
                for _ in range(4):
                    relay.collect()
                    relay.flush()
            views.poll()
            with views.db.transaction() as db:
                qid,mid = db.execute("SELECT qid,mid FROM question_cards WHERE qid=?",(dropped[0],)).fetchone()
            pending = {"question_id":qid,"mode":"choice","option_id":"2"}
            self.assertEqual(actions.record("app","trainer","test-chat",mid,pending,None),"pending")
            won = self.read_native(native,"answer-native")
            self.assertNotIn("native-question-native",won["continued"])
            relay.worker.request = original_request
            for _ in range(4):
                relay.collect()
                relay.flush()
            actions.flush()
            views.poll()
            self.assertEqual(sink.calls[-1],("patch",mid))
            self.assertEqual(actions.record("app","trainer","test-chat",mid,pending,None),"rejected")
            with loop.worker.db.transaction() as db:
                row = db.execute("SELECT proof FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()
            native_proof = json.loads(row[0])
            self.assertEqual(native_proof["answer"],{"mode":"choice","option_id":"1"})
            self.assertEqual([item["id"] for item in won["replies"] if item["call"]=="native-question-native"],
                             [native_proof["receipt_id"]])
            self.assertEqual(writes,["native-question-choice","native-question-text","native-question-skip"])
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.evidence = {"session":session,"questions":completed,"native_answer_writes":writes,
                             "native_won_conflict":{"platform_message":mid,"question_id":qid,"proof":native_proof,
                                                    "late_platform_choice":"2","platform_state":"rejected"},
                             "unbridged_multi":rejected["continued"],"router_tasks":0,
                             "boundary":"Native DSH timed question tool and reply admission; local scripted provider, Feishu fixture"}

    def test_resume_browses_only_configured_native_cold_sessions_without_activating_them(self):
        session = "dsh-native-catalog-bound"
        directory,loop,service,command = self.goal_worker(session)
        with self.start_native(directory,session,"catalog") as native:
            allowed,foreign = guid(),guid()
            server = self.enterContext(serve_adapter(loop.adapter,"oguri",
                {allowed:"worker/oguri",foreign:"worker/other"},("127.0.0.1",0),None))
            thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
            url = "http://127.0.0.1:"+str(server.server_port)
            loop.worker.adapter = RemoteAdapter(Client(url,allowed))
            with self.assertRaisesRegex(Fault,"wrong_adapter_owner"):
                Client(url,foreign).request("GET","host/session-catalog")
            service.step()
            self.assertTrue(service.host_session_catalog)
            catalog = loop.worker.adapter.session_catalog()
            ids = {item["id"] for item in catalog["items"]}
            self.assertEqual(ids,{session,*("mowulink-test-catalog-"+str(i) for i in range(11))})
            self.assertEqual(catalog["unavailable"],1)
            page1,text = command("om_catalog","/resume")
            self.assertIn("第 1/2 页",text)
            _,text = command("om_page_next","/resume next")
            self.assertIn("第 2/2 页",text)
            self.assertIn("1 个会话的记录暂不可读",text)
            self.assertIn("第 1/2 页",command("om_prev","/resume prev")[1])
            self.assertIn("第 2/2 页",command("om_page_two","/resume page 2")[1])
            _,text = command("om_title_search","/resume search 目录中的会话 10")
            self.assertIn("mowulink-test-catalog-10",text)
            self.assertNotIn("mowulink-test-catalog-0",text)
            self.assertIn("当前绑定",command("om_binding","/resume search dsh-native-catalog-bound")[1])
            self.assertIn("没有符合",command("om_hidden","/resume search 仅宿主可见")[1])
            self.assertIn("敬请期待",command("om_no_select","/resume 1")[1])
            self.assertIn("敬请期待",command("om_no_foreign_select","/resume mowulink-test-catalog-10")[1])
            self.assertEqual(command("om_catalog","/resume")[0],page1)
            result = self.read_native(native,"read")
            self.assertEqual(result["liveSessions"],[session])
            self.assertEqual(result["inputs"],[])
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.evidence = {"session":session,"allowed_catalog_entries":len(ids),"unreadable":catalog["unavailable"],
                "excluded_unconfigured_session":True,"denied_other_worker_http":403,"live_sessions_after":result["liveSessions"],
                "native_input_count":len(result["inputs"]),"search_and_pagination_confirmed":True,
                "boundary":"Native DSH SessionQueryEngine and persisted session files; isolated Worker HTTP"}

    def test_stop_targets_only_current_private_turn_and_never_replays_a_lost_result(self):
        session = "dsh-native-private-stop"
        directory,loop,service,command = self.goal_worker(session)
        with self.start_native(directory,session,"private-stop") as native:
            service.step()
            self.assertIn("已空闲",command("om_idle","/stop")[1])
            first = loop.worker.chat(guid(),"Hold private one")
            self.assertTrue(self.read_native(native,"wait-private one")["running"])
            loop.worker.chat(guid(),"Keep queued input after abort")
            receipt,card = command("om_stop_first","/stop")
            self.assertIn("回合已停止",card)
            self.assertEqual(receipt["stop_receipt"]["turn_id"],first["turn_id"])
            stopped = self.read_native(native,"read")
            queued, = stopped["inbox"]
            self.assertEqual(len(stopped["inputs"]),1)
            continued = self.read_native(native,"wake-private-queue")
            self.assertEqual(continued["inputs"],[first["native_id"],queued,"fixture-next-private"])
            self.assertEqual(continued["inbox"],[])
            loop.worker.chat(guid(),"Hold private two")
            self.assertTrue(self.read_native(native,"wait-private two")["running"])
            original = loop.adapter.stop_private
            def lose(session):
                proof = original(session)
                self.assertEqual(proof["state"],"stopped")
                raise Fault(503,"adapter_connection_unconfirmed",True)
            loop.adapter.stop_private = lose
            pending,card = command("om_stop_second","/stop")
            self.assertIn("待核",card)
            self.assertNotIn("stop_receipt",pending)
            loop.adapter.stop_private = original
            loop.worker.chat(guid(),"Hold private three")
            self.assertTrue(self.read_native(native,"wait-private three")["running"])
            self.assertEqual(command("om_stop_first","/stop")[0],receipt)
            self.assertEqual(command("om_stop_second","/stop")[0],pending)
            self.assertTrue(loop.adapter.busy())
            self.assertIn("回合已停止",command("om_stop_third","/stop")[1])
            observed = self.read_native(native,"read")
            self.assertEqual([turn["reason"]["kind"] for turn in observed["turns"]],
                             ["aborted","completed","completed","aborted","aborted"])
            self.assertEqual(len(observed["inputs"]),5)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            tid = loop.hermes.create(guid(),"Protected dispatch","Wait for Router cancel","oguri")["task_id"]
            loop.worker.poll()
            service.clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            working = loop.hermes.inspect(tid)["task"]
            self.assertEqual(working["state"],"working")
            self.assertEqual(loop.worker.hold()["held_task_id"],tid)
            self.assertIn("取消请走工单流程",command("om_stop_formal","/stop")[1])
            with self.assertRaisesRegex(Fault,"native_stop_not_private"):
                loop.adapter.stop_private(session)
            self.assertTrue(loop.adapter.busy())
            self.assertEqual(loop.hermes.inspect(tid)["task"],working)
            loop.hermes.act(guid(),tid,working["revision"],"cancel",reason="End isolated formal case")
            loop.worker.poll()
            formal = self.read_native(native,"read")
            self.assertEqual(formal["turns"][-2]["reason"]["kind"],"aborted")
            self.assertEqual(loop.worker.hold()["held_task_id"],tid)
            self.evidence = {"session":session,"first_stop":receipt["stop_receipt"],
                "turns":observed["turns"],"input_count":len(observed["inputs"]),"preserved_native_queue_item":queued,
                "lost_reply_status":"unknown","replay_did_not_stop_later_turn":True,
                "formal_stop_rejected":tid,"formal_held_pending_cleanup":True,
                "boundary":"Native DSH AgentLoop and abort records; scripted local provider"}

    def test_native_formal_question_returns_to_its_assignment_and_keeps_ownership_until_completion(self):
        session = "dsh-native-w05"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-w05-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        credential,platform_key = guid(),guid()
        service = WorkerService(loop.worker,clock)
        url = self.open_worker(service,{credential:"adapter/oguri",platform_key:"platform/oguri"})
        self.enterContext(tool_listener(Path(directory),loop.adapter,Client(url,credential)))
        platform = Client(url,platform_key)
        with self.start_native(directory,session,"work-question") as native:
            tid = loop.hermes.create(guid(),"Formal native Q&A","Ask before continuing","oguri")["task_id"]
            loop.worker.connect()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            asked = self.read_native(native,"read")
            self.assertEqual([call["name"] for call in asked["calls"]],["worker_ask"],asked["results"])
            self.assertFalse(asked["results"][0]["isError"])
            question, = loop.hermes.questions(task_id=tid)["items"]
            qid,aid = question["question_id"],question["assignment_id"]
            self.assertEqual(json.loads(asked["results"][0]["content"][0]["text"])["question_id"],qid)
            self.assertEqual(loop.worker.hold(),{"held_task_id":tid,"held_assignment_id":aid})
            self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"working")
            self.assertIsNone(loop.hermes.inspect(tid)["last_report"])
            choice = {"mode":"choice","option_id":"go"}
            key = guid()
            answer = loop.hermes.answer(key,tid,qid,choice)
            self.assertEqual(loop.hermes.answer(key,tid,qid,choice),answer)
            dropped = []
            native_deliver = loop.native.deliver
            def miss(session,delivery_id,payload,digest):
                result = native_deliver(session,delivery_id,payload,digest)
                if payload["kind"]=="question_answer":
                    dropped.append((delivery_id,payload))
                    return None
                return result
            loop.native.deliver = miss
            loop.worker.poll()
            continued = self.read_native(native,"read")
            self.assertEqual(len(dropped),1)
            did,payload = dropped[0]
            self.assertEqual((payload["task_id"],payload["assignment_id"],payload["question_id"],payload["answer"]),
                             (tid,aid,qid,choice))
            self.assertEqual(loop.worker.hold(),{"held_task_id":tid,"held_assignment_id":aid})
            self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"working")
            continuation = json.loads(continued["results"][-1]["content"][0]["text"])
            self.assertEqual(continuation,{"recorded":True,"task_state":"working"},continued["results"])
            loop.worker.poll()
            self.assertEqual(self.read_native(native,"read")["inputs"],continued["inputs"])
            task = loop.hermes.inspect(tid)["task"]
            loop.hermes.update(guid(),tid,task["revision"],{"instructions":"Use the confirmed choice and report."})
            loop.worker.poll()
            finished = self.read_native(native,"read")
            detail = loop.hermes.inspect(tid)
            self.assertEqual(detail["task"]["state"],"completed_reported")
            self.assertEqual(len(finished["inputs"]),3)
            self.assertEqual([call["name"] for call in finished["calls"]],["worker_ask","worker_report","worker_report"])
            self.assertFalse(any(item["isError"] for item in finished["results"]))
            self.assertIsNone(loop.worker.hold()["held_assignment_id"])
            with loop.adapter.db.transaction() as db:
                input_receipt = json.loads(db.execute("SELECT receipt FROM intents WHERE delivery_id=?",(did,)).fetchone()[0])
            self.assertEqual(input_receipt["native_id"],finished["inputs"][1])
            items = platform.request("GET","worker/display")["items"]
            cards = [row["event"] for row in items if row["event"].get("question_id")==qid]
            self.assertEqual([(card["kind"],card["recipient"]) for card in cards],
                             [("question_open","manager"),("question_answered","manager")])
            notices = [frame["payload"] for frame in loop.manager_wire.frames(0) if frame["payload"].get("task_id")==tid]
            self.assertEqual([item["text"] for item in notices],["Use the first route?","Finished after the confirmed answer"])
            self.evidence = {"session":session,"task_id":tid,"assignment_id":aid,"question_id":qid,
                "question_call":asked["calls"][0],"answer":choice,"answer_receipt":input_receipt,
                "continue_receipt":continuation,"reports":[json.loads(item["content"][0]["text"]) for item in finished["results"]],
                "native_inputs":finished["inputs"],"manager_notice_count":len(notices),"router_after":detail["task"]["state"],
                "boundary":"Real DSH tool executor and Router/Worker HTTP; scripted local model and manager input"}

    def test_native_goal_finishing_releases_formal_work_only_after_its_router_probe_w04(self):
        session = "dsh-native-formal-goal"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-formal-goal-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        credential = guid()
        service = WorkerService(loop.worker,clock)
        url = self.open_worker(service,{credential:"adapter/oguri"})
        self.enterContext(tool_listener(Path(directory),loop.adapter,Client(url,credential)))
        with self.start_native(directory,session,"formal-goal") as native:
            tid = loop.hermes.create(guid(),"Native long work","Run a native goal and report when asked","oguri")["task_id"]
            loop.worker.connect()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            completed = self.read_native(native,"formal-goal-round")
            self.assertEqual([x["name"] for x in completed["calls"]],["create_goal","update_goal"],completed["results"])
            self.assertTrue(all(not row["isError"] for row in completed["results"]),completed["results"])
            goal = loop.native.goal_view(session)["goal"]
            self.assertEqual(goal["phase"],"complete")
            self.assertEqual([row["id"] for row in completed["rounds"]],[goal["id"]])
            aid = loop.worker.hold()["held_assignment_id"]
            self.assertTrue(aid)
            self.assertEqual(loop.worker.hold()["held_task_id"],tid)
            before = loop.hermes.inspect(tid)
            self.assertEqual(before["task"]["state"],"working")
            self.assertIsNone(before["last_report"])
            clock.advance(1)
            loop.worker.poll()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            after = self.read_native(native,"read")
            self.assertEqual([x["name"] for x in after["calls"]],["create_goal","update_goal","worker_report"])
            self.assertFalse(after["results"][-1]["isError"])
            result = json.loads(after["results"][-1]["content"][0]["text"])
            self.assertEqual(result,{"recorded":True,"task_state":"completed_reported","release_assignment_id":aid})
            self.assertEqual(len(after["inputs"]),2)
            with loop.adapter.db.transaction() as db:
                inputs = [(json.loads(p),json.loads(r)) for p,r in db.execute("SELECT payload,receipt FROM intents ORDER BY rowid")]
            self.assertEqual([item[0]["kind"] for item in inputs],["task","task_probe"])
            self.assertTrue(all(item[0]["assignment_id"]==aid for item in inputs))
            detail = loop.hermes.inspect(tid)
            self.assertEqual(detail["task"]["state"],"completed_reported")
            self.assertIsNone(loop.worker.hold()["held_assignment_id"])
            self.evidence = {"session":session,"task_id":tid,"assignment_id":aid,"native_goal":goal,
                "goal_rounds":completed["rounds"],"native_before_probe":before["task"]["state"],
                "probe_receipt":inputs[-1][1],"w04":result,"router_final":detail["task"]["state"],
                "boundary":"Native DSH goal tools, actual scheduled continuation and Router probes; scripted local model"}

    def test_actual_dsh_loop_gives_the_same_feishu_caption_and_original_image_to_its_provider(self):
        session = "dsh-native-media"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-media-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        service = WorkerService(loop.worker,clock)
        platform_key,adapter_key = guid(),guid()
        url = self.open_worker(service,{platform_key:"platform/oguri",adapter_key:"adapter/oguri"})
        platform,adapter = Client(url,platform_key),Client(url,adapter_key)
        loop.adapter.media_store = NativeFiles(Path(directory)/"media",adapter)
        relay = DshDisplay(loop.adapter,adapter,"oguri")
        downloads = []
        def fetch(mid,key,kind,limit):
            downloads.append((mid,key,kind))
            return PNG
        inbound = Inbound(loop.root/"feishu.db",platform,fetch,"app","trainer","test-chat")
        sink = PlatformFixture()
        views = Views(loop.root/"views.db",platform,sink,"test-chat","小栗帽",clock)
        caption = "  图上有哪些细节？\n同一条原文。 "
        post = {"zh_cn":{"content":[[{"tag":"text","text":caption},{"tag":"img","image_key":"img_fixture"}]]}}
        def record(mid,kind,content):
            return inbound.record("app","user","trainer",{"chat_id":"test-chat","chat_type":"p2p","message_type":kind,
                "message_id":mid,"content":json.dumps(content,ensure_ascii=False)})
        with self.start_native(directory,session,"media") as native:
            relay.collect()
            one = record("om_dsh_caption","post",post)
            proof = inbound.deliver(one)
            self.assertEqual(inbound.deliver(record(one,"post",post)),proof)
            self.read_native(native,"read")
            two = record("om_dsh_image","image",{"image_key":"img_fixture"})
            pure = inbound.deliver(two)
            self.assertEqual(inbound.deliver(two),pure)
            observed = self.read_native(native,"read")
            sha = hashlib.sha256(PNG).hexdigest()
            self.assertEqual(observed["mediaRequests"],[
                {"caption":caption,"images":[{"sha256":sha,"size":len(PNG)}],"partTypes":["text","image"]},
                {"caption":"","images":[{"sha256":sha,"size":len(PNG)}],"partTypes":["image"]},
            ])
            self.assertEqual(observed["inputs"],[proof["native_id"],pure["native_id"]])
            self.assertEqual(proof["media_sha256"],[sha])
            self.assertEqual(pure["media_sha256"],[sha])
            for _ in range(8):
                relay.collect()
                relay.flush()
            views.poll()
            rows = [item[2] for item in sink.messages.values()]
            finals = [r for r in rows if r["header"]["title"]["content"]=="小栗帽-回复"]
            self.assertEqual(len(finals),2)
            self.assertTrue(all("Local fixture received 1 image(s)." in json.dumps(f) for f in finals))
            platform_items = platform.request("GET","worker/display")["items"]
            self.assertEqual([item["event"]["text"] for item in platform_items if item["event"]["kind"]=="native_input"],[caption])
            self.assertEqual(len(downloads),2)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.evidence = {"session":session,"captioned_receipt":proof,"image_only_receipt":pure,
                "provider_messages":observed["mediaRequests"],"download_count":len(downloads),
                "original_input_count":len(observed["inputs"]),"reply_cards":len(finals),
                "boundary":"Actual DSH run loop and attachment storage; Feishu and model provider are fixtures"}

    def test_dsh_peer_image_and_native_reply_travel_on_the_same_private_chain(self):
        session = "dsh-native-peer"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-peer-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host,extra_workers=["tama","foreign"]))
        loop.worker.clock = clock
        sender_host = FixtureHost(loop.root/"sender-host.db","test-sending-host")
        sender = Worker(loop.root/"sender.db","tama",loop.extra_clients["tama"],
                        HostAdapter(loop.root/"sender-adapter.db",sender_host,sender_host.session))
        sender_key,recipient_key = guid(),guid()
        sender_api = Client(self.open_worker(WorkerService(sender,clock),{sender_key:"adapter/tama"}),sender_key)
        receiver = Client(self.open_worker(WorkerService(loop.worker,clock),{recipient_key:"adapter/oguri"}),recipient_key)
        loop.adapter.media_store = NativeFiles(Path(directory)/"media",receiver)
        self.enterContext(tool_listener(Path(directory),loop.adapter,receiver))
        sha = hashlib.sha256(PNG).hexdigest()
        upload = sender_api.request("POST","worker/media/uploads",{"name":"peer.png","sha256":sha,"size_bytes":len(PNG)},guid())
        path = "worker/media/uploads/"+upload["upload_id"]
        uploaded = sender_api.request("PATCH",path,PNG,headers={"Upload-Offset":"0"})
        mid,caption = guid(),"请回这条原消息。原图随信。"
        body = {"message_id":mid,"to_worker":"oguri","text":caption,"attachment_ids":[uploaded["attachment_id"]],
                "attachment_kinds":["image"]}
        with self.start_native(directory,session,"peer-media") as native:
            sender.connect()
            loop.worker.connect()
            accepted = sender_api.request("POST","worker/peer",body)
            self.assertEqual(sender_api.request("POST","worker/peer",body),accepted)
            status = "peers/messages/"+mid
            self.assertFalse(sender.router.request("GET",status)["worker_received"])
            original_delivery = loop.native.deliver_media
            def drop_host_reply(*args):
                original_delivery(*args)
                return None
            loop.native.deliver_media = drop_host_reply
            loop.worker.poll()
            first = self.read_native(native,"read")
            pending = sender.router.request("GET",status)
            self.assertTrue(pending["worker_received"])
            self.assertIsNone(pending["host_receipt_id"])
            loop.native.deliver_media = original_delivery
            clock.advance(5)
            loop.worker.poll()
            observed = self.read_native(native,"read")
            self.assertEqual(len(first["inputs"]),1)
            self.assertEqual(observed["inputs"],first["inputs"])
            routed = sender.router.request("GET",status)
            self.assertTrue(routed["host_receipt_id"])
            image = observed["mediaRequests"][0]
            original = json.loads(image["caption"].split("\n")[1])
            self.assertEqual((original["message_id"],original["text"]),(mid,caption))
            self.assertEqual(image["images"],[{"sha256":sha,"size":len(PNG)}])
            history_call,listed,call,lookup = observed["calls"]
            self.assertEqual(history_call["name"],"worker_peer_inbox")
            self.assertEqual(listed["name"],"worker_list_peers")
            self.assertEqual(call["name"],"worker_send_peer")
            self.assertEqual(lookup["name"],"worker_peer_status")
            args = json.loads(call["arguments"])
            self.assertEqual((args["to_worker"],args["reply_to"]),("tama",mid))
            history,directory_result,result,native_status = (
                json.loads(entry["content"][0]["text"]) for entry in observed["results"])
            received, = history["items"]
            self.assertEqual((received["message_id"],received["peer_id"],received["direction"]),(mid,"tama","in"))
            self.assertEqual(set(received),{"message_id","direction","peer_id","reply_to"})
            self.assertEqual(directory_result,{"items":[{"worker_id":"tama","online":True}],"next_cursor":None})
            self.assertEqual(result["status"],"accepted")
            self.assertEqual((native_status["message_id"],native_status["status"]),(mid,"accepted"))
            self.assertTrue(native_status["worker_received"])
            self.assertIsNone(native_status["host_receipt_id"])
            with loop.adapter.db.transaction() as db:
                proof = json.loads(db.execute("SELECT receipt FROM intents WHERE delivery_id=?",
                                              (routed["delivery_id"],)).fetchone()[0])
            replay = {"session":session,"turn_id":proof["turn_id"],"call_id":call["callId"],
                      "tool":"worker_send_peer","arguments":args}
            self.assertEqual(receiver.request("POST","worker/native-tool",replay),result)
            with self.assertRaisesRegex(Fault,"peer_message_conflict"):
                receiver.request("POST","worker/native-tool",dict(replay,arguments=dict(args,text="changed body")))
            sender.poll()
            (reply,receipt), = sender_host.entries()
            self.assertEqual((reply["message_id"],reply["reply_to"],reply["text"]),(result["message_id"],mid,args["text"]))
            with self.assertRaisesRegex(Fault,"peer_message_not_found"):
                loop.extra_clients["foreign"].request("GET",status)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.assertIsNone(loop.worker.hold()["held_task_id"])
            self.evidence = {"letter":mid,"host_input":proof,"peer_reception":routed,"actual_tool_call":call["callId"],
                "inbox_call":history_call["callId"],"native_inbox":history,
                "directory_call":listed["callId"],"native_directory":directory_result,
                "native_status_call":lookup["callId"],"native_status_before_host_receipt":native_status,
                "reply":result,"reply_to":reply["reply_to"],"reply_recipient_receipt":receipt,
                "image":image["images"][0],"native_inputs":len(observed["inputs"]),"reply_inputs":len(sender_host.entries()),
                "clock":"controlled test clock",
                "boundary":"Actual DSH native tool executor and independent Router; scripted local model and sending host fixture"}

    def test_two_real_dsh_sessions_roundtrip_a_linked_peer_message(self):
        recipient_dir,loop,service,_ = self.goal_worker("dsh-peer-destination",extra_workers=["tama"])
        sender_dir = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-peer-src-"))
        source_session = "dsh-peer-source"
        target_session = loop.adapter.bound_session()
        sender_port = DshHost(Path(sender_dir))
        sender_adapter = HostAdapter(loop.root/"ds-source-adapter.db",sender_port,source_session)
        sender = Worker(loop.root/"dsh-peer-source.db","tama",loop.extra_clients["tama"],sender_adapter)
        a,b = guid(),guid()
        source = Client(self.open_worker(WorkerService(sender,service.clock),{a:"adapter/tama"}),a)
        target = Client(self.open_worker(service,{b:"adapter/oguri"}),b)
        loop.adapter.media_store = NativeFiles(Path(recipient_dir)/"media",target)
        self.enterContext(tool_listener(Path(sender_dir),sender_adapter,source))
        self.enterContext(tool_listener(Path(recipient_dir),loop.adapter,target))
        sha = hashlib.sha256(PNG).hexdigest()
        upload = source.request("POST","worker/media/uploads",{"name":"native-peer.png","sha256":sha,
                                "size_bytes":len(PNG)},guid())
        file = source.request("PATCH","worker/media/uploads/"+upload["upload_id"],PNG,headers={"Upload-Offset":"0"})
        args = {"to_worker":"oguri","text":"One isolated Agent to another","attachment_ids":[file["attachment_id"]],
                "attachment_kinds":["image"]}
        with (self.start_native(sender_dir,source_session,"peer-source") as sender_host,
              self.start_native(recipient_dir,target_session,"peer-media") as recipient_host):
            sender.connect()
            loop.worker.connect()
            prompt = sender.chat(guid(),"Send peer fixture\n"+encode(args))
            original = self.read_native(sender_host,"read")
            sent_call, = original["calls"]
            self.assertEqual((sent_call["name"],json.loads(sent_call["arguments"])),("worker_send_peer",args))
            sent = json.loads(original["results"][0]["content"][0]["text"])
            self.assertEqual(sent["status"],"accepted")
            loop.worker.poll()
            delivered = self.read_native(recipient_host,"read")
            loop.worker.poll()
            loop.worker.poll()
            self.assertEqual(self.read_native(recipient_host,"read")["inputs"],delivered["inputs"])
            self.assertEqual(len(delivered["inputs"]),1)
            self.assertEqual(delivered["mediaRequests"][0]["images"],[{"sha256":sha,"size":len(PNG)}])
            _,_,reply_call,_ = delivered["calls"]
            reply_args = json.loads(reply_call["arguments"])
            self.assertEqual((reply_call["name"],reply_args["reply_to"]),("worker_send_peer",sent["message_id"]))
            reply = json.loads(delivered["results"][2]["content"][0]["text"])
            sender.poll()
            answered = self.read_native(sender_host,"read")
            sender.poll()
            self.assertEqual(self.read_native(sender_host,"read")["inputs"],answered["inputs"])
            self.assertEqual(len(answered["inputs"]),2)
            self.assertEqual(answered["inputs"][0],prompt["native_id"])
            self.assertEqual([call["name"] for call in answered["calls"]],["worker_send_peer"])
            self.assertEqual([request["command"] for request in answered["modelRequests"]],["private","private","peer"])
            receipts = [sender.router.request("GET","peers/messages/"+mid) for mid in
                        (sent["message_id"],reply["message_id"])]
            self.assertTrue(all(receipt["host_receipt_id"] for receipt in receipts))
            with sender_adapter.db.transaction() as db:
                native_reply = json.loads(db.execute("SELECT payload FROM intents WHERE native_id=?",
                                                     (answered["inputs"][-1],)).fetchone()[0])
            self.assertEqual((native_reply["text"],native_reply["reply_to"]),(reply_args["text"],sent["message_id"]))
            self.assertFalse(loop.hermes.tasks()["items"])
            self.assertEqual((sender.hold()["held_task_id"],loop.worker.hold()["held_task_id"]),(None,None))
            self.evidence = {"source_session":source_session,"destination_session":target_session,
                "source_tool_call":sent_call["callId"],"destination_tool_call":reply_call["callId"],
                "destination_input":delivered["inputs"][0],"source_reply_input":answered["inputs"][-1],
                "reply_to":native_reply["reply_to"],"router_receipts":receipts,"image_sha256":sha,
                "formal_task_count":0,"clock":"controlled test clock",
                "boundary":"Two actual DSH AgentLoop processes with scripted local providers"}

    @unittest.skipUnless(codex_driver.CODEX.is_file(),"bundled Codex App Server required")
    def test_codex_and_dsh_roundtrip_native_peer_image_and_linked_reply(self):
        directory,loop,service,_ = self.goal_worker("dsh-cross-peer",extra_workers=["tama"])
        target_session,clock = loop.adapter.bound_session(),service.clock
        loop.worker.clock = clock
        profile = loop.root/"codex-peer-profile"
        profile.mkdir(mode=0o700)
        model = ThreadingHTTPServer(("127.0.0.1",0),codex_driver.Provider)
        model.observed,model.media = [],[]
        url = self.enterContext(codex_driver.running(model))
        rpc = self.enterContext(codex_driver.isolated_app_server(str(profile),url))
        tools = [tool for tool in codex_driver.TOOLSET if tool["name"]=="worker_send_peer"]
        sid = rpc.request("thread/start",{"cwd":str(profile),"ephemeral":False,"dynamicTools":tools})["thread"]["id"]
        codex = AppServerHost(rpc,sid)
        codex_adapter = HostAdapter(loop.root/"codex-peer-adapter.db",codex,sid)
        sender = Worker(loop.root/"codex-peer-worker.db","tama",loop.extra_clients["tama"],codex_adapter,clock=clock)
        a,b = guid(),guid()
        source = Client(self.open_worker(WorkerService(sender,clock),{a:"adapter/tama"}),a)
        target = Client(self.open_worker(service,{b:"adapter/oguri"}),b)
        loop.adapter.media_store = NativeFiles(Path(directory)/"media",target)
        self.enterContext(tool_listener(Path(directory),loop.adapter,target))
        settled = Event()
        self.enterContext(worker_callbacks(codex,source,settled.set))
        digest = hashlib.sha256(PNG).hexdigest()
        upload = source.request("POST","worker/media/uploads",{"name":"codex-to-dsh.png","sha256":digest,
                                "size_bytes":len(PNG)},guid())
        file = source.request("PATCH","worker/media/uploads/"+upload["upload_id"],PNG,headers={"Upload-Offset":"0"})
        args = {"to_worker":"oguri","text":"Cross-host original image","attachment_ids":[file["attachment_id"]],
                "attachment_kinds":["image"]}

        def wait_for_codex():
            until = time.monotonic()+10
            while not settled.wait(.02) and time.monotonic()<until:
                sender.poll()
            self.assertTrue(settled.is_set())
            settled.clear()

        with self.start_native(directory,target_session,"peer-media") as dsh:
            sender.connect()
            loop.worker.connect()
            mid = guid()
            prompt = "Send peer fixture\n"+encode(args)
            prompt_receipt = sender.chat(mid,prompt)
            until = time.monotonic()+4
            while prompt_receipt is None and time.monotonic()<until:
                prompt_receipt = sender.chat(mid,prompt)
                time.sleep(.02)
            self.assertIsNotNone(prompt_receipt)
            wait_for_codex()
            first, = codex.thread(sid,include_turns=True)["turns"]
            sent, = [item for item in first["items"] if item.get("type")=="dynamicToolCall"]
            self.assertEqual((sent["tool"],sent["arguments"],sent["success"]),("worker_send_peer",args,True))
            posted = json.loads(sent["contentItems"][0]["text"])
            loop.worker.poll()
            dsh_result = self.read_native(dsh,"read")
            loop.worker.poll()
            loop.worker.poll()
            self.assertEqual(self.read_native(dsh,"read")["inputs"],dsh_result["inputs"])
            self.assertEqual(len(dsh_result["inputs"]),1)
            self.assertEqual(dsh_result["mediaRequests"][0]["images"],[{"sha256":digest,"size":len(PNG)}])
            _,_,call,_ = dsh_result["calls"]
            self.assertEqual(json.loads(call["arguments"])["reply_to"],posted["message_id"])
            reply = json.loads(dsh_result["results"][2]["content"][0]["text"])
            sender.poll()
            wait_for_codex()
            clock.advance(5)
            sender.poll()
            loop.worker.poll()
            first,second = codex.thread(sid,include_turns=True)["turns"]
            received, = [item for item in second["items"] if item.get("type")=="userMessage"]
            inbound = json.loads(received["content"][0]["text"].split("\n",1)[1])["payload"]
            self.assertEqual((inbound["message_id"],inbound["reply_to"]),(reply["message_id"],posted["message_id"]))
            receipts = [sender.router.request("GET","peers/messages/"+value) for value in
                        (posted["message_id"],reply["message_id"])]
            self.assertTrue(all(receipt["host_receipt_id"] for receipt in receipts),receipts)
            self.assertFalse(loop.hermes.tasks()["items"])
            self.evidence = {"codex_session":sid,"dsh_session":target_session,"source_tool":sent["id"],
                             "dsh_reply_tool":call["callId"],"source_input":prompt_receipt["native_id"],
                             "dsh_input":dsh_result["inputs"][0],"codex_reply_input":received["id"],
                             "reply_to":inbound["reply_to"],"image_sha256":digest,"router_receipts":receipts,
                             "boundary":"Actual Codex App Server and DSH AgentLoop on one Mac with scripted providers"}

    def test_shelved_dsh_assignment_submits_candidate_without_changing_ownership(self):
        directory,loop,service,_ = self.goal_worker("dsh-native-material",extra_workers=["tama"])
        session,clock = loop.adapter.bound_session(),service.clock
        token = guid()
        client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
        self.enterContext(tool_listener(Path(directory),loop.adapter,client))
        raw = b"evidence from native shelved assignment\n" * 16000
        digest = hashlib.sha256(raw).hexdigest()
        upload = client.request("POST","worker/media/uploads",
                                {"name":"candidate.txt","sha256":digest,"size_bytes":len(raw)},guid())
        source = client.request("PATCH","worker/media/uploads/"+upload["upload_id"],raw,
                                headers={"Upload-Offset":"0"})["attachment_id"]
        next_host = FixtureHost(loop.root/"new-holder-native.db","new-holder-session")
        next_worker = Worker(loop.root/"new-holder.db","tama",loop.extra_clients["tama"],
                             HostAdapter(loop.root/"new-holder-adapter.db",next_host,next_host.session))
        next_worker.connect()
        next_worker.poll()
        with self.start_native(directory,session,"material") as native:
            tid = loop.hermes.create(guid(),"Candidate",source,"oguri")["task_id"]
            router_request = loop.worker.router.request
            checked_scope = []

            def confirm_material_scope(method,path,*args,**kwargs):
                if method=="POST" and path==f"tasks/{tid}/materials" and not checked_scope:
                    artifact, = args[0]["artifact_ids"]
                    with self.assertRaisesRegex(Fault,"attachment_not_found"):
                        loop.manager_wire.request("GET","attachments/"+artifact)
                    self.assertEqual(router_request("GET","attachments/"+artifact)["bytes"],raw)
                    self.assertIsNone(loop.worker.hold()["held_assignment_id"])
                    checked_scope.append(artifact)
                return router_request(method,path,*args,**kwargs)

            loop.worker.router.request = confirm_material_scope
            loop.worker.connect()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            initial = self.read_native(native,"read")
            aid = loop.worker.hold()["held_assignment_id"]
            self.assertEqual(len(initial["inputs"]),1)
            for seconds in (1,10,1,1,1,1):
                clock.advance(seconds)
                loop.worker.poll()
                loop.worker.poll()
                result = self.read_native(native,"read")
            stage,call = result["calls"]
            self.assertEqual([stage["name"],call["name"]],["worker_stage_artifacts","worker_submit_material"])
            args = json.loads(call["arguments"])
            self.assertEqual((args["task_id"],args["assignment_id"]),(tid,aid))
            self.assertEqual(set(args),{"task_id","assignment_id","artifact_ids"})
            self.assertEqual(args["artifact_ids"],checked_scope)
            artifact, = checked_scope
            self.assertEqual([item["isError"] for item in result["results"]],[False,False])
            material = json.loads(result["results"][1]["content"][0]["text"])
            self.assertEqual(set(material),{"received","material_id"})
            self.assertTrue(material["received"])
            details = loop.hermes.inspect(tid)
            self.assertEqual((details["task"]["state"],details["task"]["holder"]),("blocked",{"kind":"router"}))
            self.assertIsNone(details["last_report"])
            self.assertEqual([item["material_id"] for item in details["materials"]],[material["material_id"]])
            self.assertEqual(details["materials"][0]["artifact_ids"],[artifact])
            self.assertEqual(loop.manager_wire.request("GET","attachments/"+artifact)["bytes"],raw)
            with self.assertRaisesRegex(Fault,"attachment_not_found"):
                loop.extra_clients["tama"].request("GET","attachments/"+artifact)
            self.assertIsNone(loop.worker.hold()["held_assignment_id"])
            with self.assertRaisesRegex(Fault,"stale_assignment") as stale:
                loop.wire.request("POST",f"tasks/{tid}/report",{"session_id":loop.worker.session,
                                  "assignment_id":aid,"outcome":"completed","summary":"Late W04"},guid())
            self.assertEqual(stale.exception.status,409)
            with loop.adapter.db.transaction() as db:
                proof = json.loads(db.execute("""SELECT receipt FROM intents
                    WHERE json_extract(payload,'$.kind')='task.shelved'""").fetchone()[0])
            request = {"session":session,"turn_id":proof["turn_id"],"call_id":call["callId"],
                       "tool":"worker_submit_material","arguments":args}
            self.assertEqual(client.request("POST","worker/native-tool",request),material)
            with self.assertRaisesRegex(Fault,"idempotency_conflict"):
                client.request("POST","worker/native-tool",dict(request,arguments=dict(args,text="changed")))
            loop.hermes.act(guid(),tid,details["task"]["revision"],"transfer",worker_id="tama",reason="Assign again")
            next_worker.poll()
            next_worker.poll()
            new_aid = next_worker.hold()["held_assignment_id"]
            self.assertTrue(new_aid and new_aid!=aid)
            self.assertEqual(len(next_host.entries()),1)
            self.assertEqual(client.request("POST","worker/native-tool",request),material)
            with self.assertRaisesRegex(Fault,"ineligible_material"):
                client.request("POST","worker/native-tool",dict(request,call_id="new-candidate-attempt"))
            final = loop.hermes.inspect(tid)
            self.assertEqual(final["task"]["assignment_id"],new_aid)
            self.assertEqual(final["task"]["holder"],{"kind":"worker","worker_id":"tama"})
            self.assertEqual([row["assignment_id"] for row in final["pending_cleanup"]],[aid])
            self.assertEqual(len(final["materials"]),1)
            self.assertIsNone(final["last_report"])
            self.assertFalse([frame for frame in loop.manager_wire.frames(0)
                              if artifact in frame["payload"].get("attachment_ids",[])])
            self.evidence = {"session":session,"task_id":tid,"old_assignment_id":aid,"shelved_receipt":proof,
                "native_tool":call["name"],"native_call_id":call["callId"],"first_receipt":material,
                "stage_tool_call_id":stage["callId"],"local_artifact":source,"router_artifact":artifact,
                "artifact_sha256":digest,"artifact_bytes":len(raw),"manager_read_only_after_submission":True,
                "first_w04":stale.exception.status,"before_new_assignment":details["task"]["holder"],"new_assignment_id":new_aid,
                "new_holder":final["task"]["holder"],"pending_cleanup":aid,
                "final_material_count":len(final["materials"]),"final_report":final["last_report"],
                "boundary":"Actual DSH native tool and Router, scripted local model and test clock"}

    def test_native_race_after_w03_never_prequeues_a_formal_task(self):
        facts = []
        for cancelled in (False,True):
            with self.subTest(cancelled=cancelled):
                session = "dsh-admission-race-"+str(cancelled).lower()
                directory,loop,service,_ = self.goal_worker(session)
                with self.start_native(directory,session,"admission-race") as native:
                    tid = loop.hermes.create(guid(),"Check admission race","Only admit when idle","oguri")["task_id"]
                    original = loop.worker.router.request
                    during_accept = []
                    def accept(method,path,*args,**kwargs):
                        response = original(method,path,*args,**kwargs)
                        if path==f"tasks/{tid}/accept":
                            private = {"kind":"chat","text":"Hold private one","message_id":guid()}
                            during_accept.append(loop.adapter.deliver(guid(),private))
                            self.assertTrue(self.read_native(native,"wait-private one")["running"])
                        return response
                    loop.worker.router.request = accept
                    loop.worker.connect()
                    loop.worker.poll()
                    service.clock.advance(10)
                    loop.worker.poll()
                    loop.worker.poll()
                    self.assertEqual(len(during_accept),1)
                    self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"working")
                    self.assertEqual(loop.worker.hold()["held_task_id"],tid)
                    self.assertEqual(self.read_native(native,"wait-private one")["inbox"],[])
                    with loop.adapter.db.transaction() as db:
                        count = db.execute("SELECT COUNT(*) FROM intents WHERE json_extract(payload,'$.kind')='task'").fetchone()[0]
                    self.assertEqual(count,0)
                    if cancelled:
                        task = loop.hermes.inspect(tid)["task"]
                        loop.hermes.act(guid(),tid,task["revision"],"cancel",reason="Cancel before native admission")
                        loop.worker.poll()
                        # The cancellation must preserve the unrelated user's live turn.
                        self.assertTrue(self.read_native(native,"wait-private one")["running"])
                    self.read_native(native,"end-race-private")
                    loop.worker.poll()
                    observed = self.read_native(native,"read")
                    loop.worker.poll()
                    self.assertEqual(self.read_native(native,"read")["inputs"],observed["inputs"])
                    with loop.adapter.db.transaction() as db:
                        entries = [json.loads(row[0])["kind"] for row in db.execute("SELECT payload FROM intents ORDER BY rowid")]
                    self.assertEqual(entries,["chat","task.cancel" if cancelled else "task"])
                    self.assertEqual(len(observed["inputs"]),2)
                    facts.append({"session":session,"task_id":tid,"held_after_w03":True,
                        "formal_inbox_while_private_active":0,"native_input_kinds":entries,
                        "native_input_count_after_replay":len(observed["inputs"]),"cancelled_before_admission":cancelled})
        self.evidence = {"cases":facts,"boundary":"Real DSH AgentLoop and isolated Router acceptance; local scripted provider"}
