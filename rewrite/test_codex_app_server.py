"""Run Desktop's bundled native App Server against a scripted loopback model."""
import base64
from contextlib import ExitStack, closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest

from .agent_tools import REPORT_TOOL, TOOLSET
from .adapter import HostAdapter
from .approvals import TTL_SECONDS
from .codex_activation import CodexActivation
from .codex_app_server import AppServerHost, worker_callbacks
from .codex_display import CodexDisplay
from .codex_provision import SEED
from .codex_rpc import NativeRpcRejected, start_app_server
from .codex_thread_preparation import CodexThreadPreparation
from .devloop import FixtureHost, Loop
from .feishu_inbound import Inbound
from .feishu_output import Views
from .hermes import HermesAdapter
from .host_wire import RemoteAdapter, serve_adapter
from .native_files import NativeFiles
from .orders import Orders
from .question_cards import Actions
from .release_check import config, serve
from .state_bundle import audit_handoff, capture, filename, restore, rollback, verify
from .storage import Fault, encode, guid
from .test_feishu_output import PlatformFixture
from .test_feishu_inbound import PNG
from .test_mainline import Clock
from .trigger import Trigger
from . import test_service
from .wire import Client, serve_loopback
from .worker import Worker
from .worker_service import WorkerService

CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
GOAL_FIXTURE = "Finish the isolated goal check"
KEEP_FORMAL_HELD = "Keep the isolated order held until an update"
TRAINER_CARD_FIXTURE = "Publish a standalone trainer test question"
TRAINER_CARD_QUESTIONS = [{"title":"Which isolated route?","options":["North","South"]},
                          {"title":"Leave a note"},{"title":"Optional note"}]


class Provider(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def formal_goal_item(self,run,payload,after,names,continuation):
        def call(tool,cid,args):
            return {"type":"function_call","id":"fc_"+cid,"name":tool,"call_id":cid,"arguments":encode(args)}

        returned = {item.get("call_id"):item.get("output") for item in after
                    if item.get("type")=="function_call_output"}
        if not continuation:
            run.setdefault("assignment",{name:payload[name] for name in ("task_id","assignment_id")})
            run.setdefault("native_tools",sorted(set(names)&{"create_goal","get_goal","update_goal","worker_report"}))
            if "fixture_goal_start" not in returned:
                return call("create_goal","fixture_goal_start",{"objective":GOAL_FIXTURE})
        elif "fixture_goal_unbound" not in returned:
            return call("worker_report","fixture_goal_unbound",dict(run["assignment"],summary="Unbound goal report"))
        elif "fixture_goal_finish" not in returned:
            run["continuation_report_reply"] = returned["fixture_goal_unbound"]
            return call("update_goal","fixture_goal_finish",{"status":"complete"})
        return {"type":"message","id":"msg_goal_continued" if continuation else "msg_goal_started",
                "role":"assistant","status":"completed","phase":"final_answer",
                "content":[{"type":"output_text","text":"Native goal fixture step."}]}

    def do_POST(self):
        if self.path!="/v1/responses":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if hasattr(self.server,"config_observations"):
            self.server.config_observations.append((body.get("model"),(body.get("reasoning") or {}).get("effort")))
        items = body["input"]
        user_index = next(i for i in range(len(items)-1,-1,-1) if items[i].get("role")=="user")
        latest = items[user_index]["content"]
        pictures = [part for part in latest if part.get("type")=="input_image"]
        if pictures:
            captions = [part["text"] for part in latest if part.get("type")=="input_text"
                        and not part["text"].startswith("<image name=[Image #") and part["text"]!="</image>"]
            images = [hashlib.sha256(base64.b64decode(part["image_url"].split(",",1)[1],validate=True)).hexdigest()
                      for part in pictures]
            self.server.media.append({"caption":captions,"image_sha256":images})
        markers = [block["text"] for block in latest if block.get("text","").startswith("魔物链接投递\n")]
        payload = json.loads(markers[-1].split("\n",1)[1])["payload"] if markers else None
        calls = sum(item.get("type") in ("function_call_output","custom_tool_call_output")
                    for item in items[user_index+1:])
        names = [tool.get("name") for tool in body.get("tools",[])]
        self.server.observed.append(("worker_report" in names,calls))
        if hasattr(self.server,"private_admissions"):
            held = next((item["text"] for item in latest if item.get("text","").startswith("Hold bound private ")),None)
            if held:
                self.server.private_admissions.put(held)
                self.server.release_private.wait(15)
                return
        response_id = "resp_fixture_"+str(len(self.server.observed))
        question = payload and payload.get("instructions")=="Ask the scripted manager before proceeding"
        peer = next((block["text"] for block in latest if block.get("text","").startswith("Send peer fixture\n")),None)
        public_preview = getattr(self.server,"public_preview",False) and any(
            block.get("text")=="Native tool preview fixture" for block in latest)
        approval_prompt = next((block["text"].removeprefix("Approval fixture ")
                                for block in latest if hasattr(self.server,"approval_commands")
                                and block.get("text","").startswith("Approval fixture ")),None)
        file_approval = next((block["text"].removeprefix("File approval fixture ")
                              for block in latest if hasattr(self.server,"approval_patches")
                              and block.get("text","").startswith("File approval fixture ")),None)
        trainer_prompt = next((block["text"].split(": ",1)[1] for block in latest
                              if hasattr(self.server,"trainer_responses")
                              and block.get("text","").startswith("Native trainer question: ")),None)
        image_prompt = next((block["text"].split(": ",1)[1] for block in latest
                            if hasattr(self.server,"public_image_paths")
                            and block.get("text","").startswith("Native output image: ")),None)

        def visible_part(name,text):
            return {"type":"message","id":name,"role":"assistant","status":"completed","phase":"commentary",
                    "content":[{"type":"output_text","text":text}]}

        prefix = []
        provision_action = getattr(self.server,"provision_calls_tool",False) and any(
            block.get("text")==SEED for block in latest)
        formal_goal = getattr(self.server,"formal_goal",None)
        goal_continuation = formal_goal is not None and any(
            part.get("text","").startswith('<codex_internal_context source="goal">') for part in latest)
        if provision_action and not calls:
            item = {"type":"function_call","id":"fc_seed","name":"worker_report","call_id":"native_seed_fixture",
                    "arguments":"{}"}
        elif any(block.get("text")==TRAINER_CARD_FIXTURE for block in latest) and not calls:
            item = {"type":"function_call","id":"fc_standalone_trainer","name":"worker_ask_trainer",
                    "call_id":"isolated-standalone-trainer","arguments":encode({"questions":TRAINER_CARD_QUESTIONS})}
        elif formal_goal is not None and (goal_continuation or payload and payload.get("kind")=="task"):
            item = self.formal_goal_item(formal_goal,payload,items[user_index+1:],names,goal_continuation)
        elif approval_prompt and not calls:
            item = {"type":"function_call","id":"fc_approval_"+approval_prompt,"name":"exec_command",
                    "call_id":"trial-approval-"+approval_prompt,
                    "arguments":encode({"cmd":self.server.approval_commands[approval_prompt],
                                        "sandbox_permissions":"require_escalated",
                                        "justification":"Read the isolated test directory."})}
        elif file_approval and not calls:
            item = {"type":"custom_tool_call","id":"ct_fixture_"+file_approval,
                    "call_id":"trial-file-"+file_approval,"name":"apply_patch",
                    "input":self.server.approval_patches[file_approval]}
        elif public_preview and not calls:
            commentary = {"id":"msg_preview_comment","type":"message","role":"assistant","status":"completed",
                          "phase":"commentary","content":[{"type":"output_text","text":"Checking the isolated workspace."}]}
            prefix = [commentary]
            item = {"type":"function_call","id":"fc_preview","name":"exec_command","call_id":"codex_preview_fixture",
                    "arguments":encode({"cmd":"pwd && while [ ! -f .release-preview ]; do sleep .05; done # PREVIEW_PRIVATE_MARKER",
                                        "yield_time_ms":9000})}
        elif image_prompt and not calls:
            prefix = [visible_part("native_pre_one_"+image_prompt,"Before image "),
                      visible_part("native_pre_two_"+image_prompt,"continued.")]
            item = {"type":"function_call","id":"fc_image_view","name":"view_image","call_id":"public_native_"+image_prompt,
                    "arguments":encode({"path":self.server.public_image_paths[image_prompt]})}
        elif trainer_prompt and not calls:
            native_q = {"id":"route","header":"路线","question":"选择路线 · "+trainer_prompt,
                        "isSecret":trainer_prompt=="private","isOther":True,
                        "options":[{"label":"第一项","description":"First"},{"label":"第二项","description":"Second"}]}
            native_questions = [native_q]
            if trainer_prompt in ("batch","mixed-private"):
                native_questions.extend([
                    {"id":"note","header":"备注","question":"留一句话","isSecret":trainer_prompt=="mixed-private",
                     "isOther":True,"options":[{"label":"有","description":"提供备注"},{"label":"无","description":"留空"}]},
                    {"id":"optional","header":"略过","question":"这题可以跳过","isSecret":False,"isOther":True,
                     "options":[{"label":"可选","description":"选填"}]},
                ])
            item = {"type":"function_call","id":"fc_trainer_"+trainer_prompt,"name":"request_user_input",
                    "call_id":"native_trainer_"+trainer_prompt,"arguments":encode({"questions":native_questions})}
        elif peer and not calls:
            item = {"type":"function_call","id":"fc_peer","name":"worker_send_peer","call_id":"native_peer_fixture",
                    "arguments":peer.split("\n",1)[1]}
        elif question and not calls:
            arguments = {"text":"Use the first route?","options":[{"id":"go","label":"继续"}]}
            item = {"type":"function_call","id":"fc_question","name":"worker_ask","call_id":"native_question",
                    "arguments":encode(arguments)}
        elif (payload and payload.get("kind") in ("task","task_update","task_probe","question_answer")
                and payload.get("instructions")!=KEEP_FORMAL_HELD and not question and calls<2):
            arguments = {"task_id":payload["task_id"],"assignment_id":payload["assignment_id"],
                         "summary":"Isolated App Server result"}
            item = {"type":"function_call","id":"fc_fixture_"+str(calls),"name":"worker_report",
                    "call_id":"native_fixture_"+str(calls),"arguments":encode(arguments)}
        else:
            item = {"id":"msg_fixture","type":"message","role":"assistant","status":"completed",
                    "content":[{"type":"output_text","text":"Local model fixture accepted the receipt."}]}
            if image_prompt and calls:
                prefix = [visible_part("native_post_one_"+image_prompt,"After image "),
                          visible_part("native_post_two_"+image_prompt,"continued.")]
                item.update(id="native_image_final_"+image_prompt,phase="final_answer")
            if trainer_prompt and calls:
                answer = next(entry["output"] for entry in reversed(items[user_index+1:])
                              if entry.get("type")=="function_call_output")
                self.server.trainer_responses.append((trainer_prompt,answer))
            if payload and payload.get("kind")=="trainer_answer":
                self.server.standalone_received.append(payload)
        stream = [{"type":"response.created","response":{"id":response_id}}]
        for index,entry in enumerate([*prefix,item]):
            stream.extend({"type":"response.output_item."+kind,"output_index":index,"item":entry}
                          for kind in ("added","done"))
        stream.append({"type":"response.completed","response":{"id":response_id,
                       "usage":{"input_tokens":11,"output_tokens":5,"total_tokens":16}}})
        raw = "".join("event: "+event["type"]+"\ndata: "+encode(event)+"\n\n" for event in stream).encode()
        self.send_response(200)
        self.send_header("Content-Type","text/event-stream")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@contextmanager
def running(server):
    with server:
        thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02},daemon=True)
        thread.start()
        try:
            yield "http://127.0.0.1:"+str(server.server_port)
        finally:
            server.shutdown()
            thread.join()


@contextmanager
def isolated_app_server(root, url, extra=()):
    settings = [
        'model="gpt-5.4"', 'model_provider="test_wire"', 'model_providers.test_wire.name="Local test wire"',
        'model_providers.test_wire.base_url='+encode(url+"/v1"), 'model_providers.test_wire.wire_api="responses"',
        'model_providers.test_wire.request_max_retries=0','model_providers.test_wire.stream_max_retries=0',
        'web_search="disabled"',
    ]
    with start_app_server(CODEX,root,root,[*settings,*extra]) as rpc:
        yield rpc


@contextmanager
def handoff_station(paths,clock,host,session,manager_host):
    orders = Orders(paths["router:main"],clock,1)
    Trigger(paths["trigger:source"],"fixture")
    adapter = HostAdapter(paths["agent:oguri"],host,session)
    adapter_token,worker_token,manager_token,platform_token,callback = (guid() for _ in range(5))
    with ExitStack() as resources:
        router = resources.enter_context(running(serve_loopback(orders,
            {worker_token:"worker/oguri",manager_token:"hermes"})))
        adapter_url = resources.enter_context(running(serve_adapter(adapter,"oguri",
            {adapter_token:"worker/oguri"},("127.0.0.1",0),None)))
        port = RemoteAdapter(Client(adapter_url,adapter_token),session)
        worker = Worker(paths["worker:oguri"],"oguri",Client(router,worker_token),port,clock=clock)
        service = WorkerService(worker,clock)
        service.step()
        if service.status()["host_session"]!=session:
            raise AssertionError(service.status())
        worker_url = resources.enter_context(running(service.listen(
            {platform_token:"platform/oguri",callback:"adapter/oguri"},("127.0.0.1",0),None)))
        manager = HermesAdapter(paths["hermes:main"],Client(router,manager_token),manager_host,clock)
        yield worker,manager,adapter,Client(worker_url,platform_token),Client(worker_url,callback)


def stage_handoff_cli(snapshot,fingerprint,source,target,destination):
    result = subprocess.run([sys.executable,"-B","-m","rewrite.state_bundle","stage-native-handoff",
        "--snapshot",snapshot,"--manifest-sha256",fingerprint,"--worker-id","oguri",
        "--source-session",source,"--target-session",target,"--destination",destination],
        cwd=Path(__file__).resolve().parent.parent,capture_output=True,text=True,check=True,timeout=12)
    return json.loads(result.stdout)


@unittest.skipUnless(CODEX.is_file(),"bundled Codex App Server required")
class CodexAppServer(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory(prefix="mowulink-codex-protocol-"))
        self.provider = ThreadingHTTPServer(("127.0.0.1",0),Provider)
        self.provider.observed = []
        self.provider.media = []
        self.origin = self.enterContext(running(self.provider))

    def provision_command(self,profile,project):
        return [sys.executable,"-m","rewrite.service","codex-provision","--codex-binary",CODEX,
                "--codex-profile",profile,"--cwd",project]

    def local_profile(self,profile):
        (profile/"config.toml").write_text('model = "gpt-5.4"\nmodel_provider = "test_wire"\n'
                                          'web_search = "disabled"\n'
                                          '[model_providers.test_wire]\nname = "Local test wire"\n'
                                          'base_url = '+encode(self.origin+"/v1")+'\nwire_api = "responses"\n'
                                          'request_max_retries = 0\nstream_max_retries = 0\n',encoding="utf-8")

    def test_provision_blocks_missing_identity_and_test_seed_tool_calls(self):
        root = Path(self.root)
        profile,project = root/"profile",root/"project"
        profile.mkdir(mode=0o700)
        project.mkdir(mode=0o700)
        command = self.provision_command(profile,project)
        repo = Path(__file__).resolve().parent.parent
        refusal = subprocess.run(command,cwd=repo,capture_output=True,text=True,timeout=25)
        self.assertEqual((refusal.returncode,json.loads(refusal.stderr)),
                         (1,{"error":"codex_test_account_required"}))
        with start_app_server(CODEX,profile,project) as review:
            self.assertFalse(review.request("thread/list",{"limit":1})["data"])
        self.assertEqual(self.provider.observed,[])

        self.local_profile(profile)
        self.provider.provision_calls_tool = True
        refused_tool = subprocess.run(command,cwd=repo,capture_output=True,text=True,timeout=25)
        failure = json.loads(refused_tool.stderr)
        self.assertEqual((refused_tool.returncode,failure["error"]),(1,"codex_provision_requested_action"))
        self.assertTrue(failure["thread_id"] and failure["turn_id"])
        self.assertFalse(refused_tool.stdout)
        self.assertEqual(self.provider.observed,[(True,0)])

    def test_app_server_compact_recovers_native_turn_without_restarting_uncertain_request(self):
        clock = Clock()
        ledger = Path(self.root)/"native-compaction.db"
        compact_calls = []

        def completed(rpc,sid):
            deadline = time.monotonic()+12
            while True:
                event = rpc.event(deadline)
                if event["method"]=="turn/completed" and event["params"]["threadId"]==sid:
                    state = event["params"]["turn"]
                    self.assertEqual(state["status"],"completed")
                    return state["id"]

        def instrument(rpc,lose_native_ack=False):
            actual = rpc.request
            def request(method,params):
                result = actual(method,params)
                if method=="thread/compact/start":
                    compact_calls.append(params["threadId"])
                    if lose_native_ack:
                        raise Fault(503,"isolated_native_ack_lost",True)
                return result
            rpc.request = request

        with isolated_app_server(self.root,self.origin) as initial:
            instrument(initial)
            sid = initial.request("thread/start",{"cwd":self.root,"ephemeral":False})["thread"]["id"]
            initial.request("turn/start",{"threadId":sid,"input":[{"type":"text","text":"Keep the native test history",
                                                                  "text_elements":[]}]})
            seed = completed(initial,sid)
            host = AppServerHost(initial,sid,ledger)
            host.session = sid
            loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:host))
            service = WorkerService(loop.worker,clock)
            token = guid()
            url = self.enterContext(running(service.listen({token:"platform/oguri"},("127.0.0.1",0),None)))
            platform = Client(url,token)
            service.step()

            def command(mid):
                return platform.request("POST","worker/command",{"message_id":mid,"text":"/compact"})["receipt"]

            def row(mid):
                with loop.worker.db.transaction() as db:
                    return db.execute("SELECT command_id,native_receipt FROM platform_compactions WHERE message_id=?",(mid,)).fetchone()

            def cards(mid):
                return [item["event"]["text"] for item in platform.request("GET","worker/display")["items"]
                        if item["event"]["event_id"]=="compact-result-"+mid]

            original = loop.adapter.compact
            def lose_worker_ack(session,command_id,*,start):
                receipt = original(session,command_id,start=start)
                self.assertEqual(receipt["command_id"],command_id)
                # Acceptance is durable even when App Server history still omits the original anchor.
                with host.compactions.db.transaction() as db:
                    row = db.execute("SELECT accepted,session FROM codex_compactions WHERE command_id=?",
                                     (command_id,)).fetchone()
                self.assertEqual(tuple(row),(1,sid))
                raise Fault(503,"isolated_worker_ack_lost",True)
            loop.adapter.compact = lose_worker_ack
            first = command("compact_first")
            self.assertIsNone(row("compact_first")["native_receipt"])
            old_compaction_turn = completed(initial,sid)
            self.assertNotEqual(old_compaction_turn,seed)
            self.assertEqual(cards("compact_first"),[])
            loop.adapter.compact = original

        with isolated_app_server(self.root,self.origin) as reopened:
            instrument(reopened,lose_native_ack=True)
            reopened.request("thread/resume",{"threadId":sid,"cwd":self.root})
            host = AppServerHost(reopened,sid,ledger)
            loop.adapter.host = host
            clock.advance(5)
            service.step()
            durable = row("compact_first")
            proof = json.loads(durable["native_receipt"])
            self.assertEqual((proof["state"],proof["turn_id"]),("completed",old_compaction_turn))
            self.assertEqual(cards("compact_first"),["宿主已确认完成历史压缩。"])
            self.assertEqual(command("compact_first"),first)
            self.assertEqual(loop.adapter.compact(sid,durable["command_id"],start=True),
                             {"session":sid,"command_id":durable["command_id"],**proof})
            self.assertEqual(compact_calls,[sid])

            second = command("compact_uncertain")
            unconfirmed_turn = completed(reopened,sid)
            later = row("compact_uncertain")
            self.assertIsNone(later["native_receipt"])
            self.assertEqual(loop.adapter.compact(sid,later["command_id"],start=True)["state"],"unknown")
            self.assertEqual(command("compact_uncertain"),second)
            self.assertEqual(compact_calls,[sid,sid])
            with loop.worker.db.transaction() as db:
                db.execute("UPDATE platform_compactions SET deadline=0 WHERE message_id='compact_uncertain'")
            clock.advance(5)
            service.step()
            self.assertEqual(cards("compact_uncertain"),["压缩结果待核。请先查看原生会话的历史记录。"])
            turns = host.thread(sid,include_turns=True)["turns"]
            self.assertEqual([turn["id"] for turn in turns],[seed,old_compaction_turn,unconfirmed_turn])
            self.assertEqual([turn["items"][0]["type"] for turn in turns],["userMessage","contextCompaction","contextCompaction"])
            self.assertEqual(proof["compaction_id"],turns[1]["items"][0]["id"])
        self.assertEqual(loop.hermes.tasks()["items"],[])
        self.evidence = {"thread_id":sid,"verified_original":proof,"uncertain_native_turn":unconfirmed_turn,
                         "native_start_requests":len(compact_calls),"replay_created_no_extra_turns":True,
                         "boundary":"Actual App Server compaction and cold history with local model and isolated platform"}

    def test_platform_goal_uses_native_user_origin_and_does_not_replay_after_lost_ack(self):
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False})["thread"]["id"]
        rpc.request("turn/start",{"threadId":sid,"input":[{"type":"text","text":"Seed the independent goal test thread",
                                                          "text_elements":[]}]})
        until = time.monotonic()+12
        while rpc.event(until)["method"]!="turn/completed":
            pass
        host = AppServerHost(rpc,sid,Path(self.root)/"goal-and-compact.db")
        host.session = sid
        loop = self.enterContext(Loop.isolated(host_factory=lambda _path,_name:host))
        service = WorkerService(loop.worker)
        token,adapter_token = guid(),guid()
        url = self.enterContext(running(service.listen({token:"platform/oguri",adapter_token:"adapter/oguri"},
                                                       ("127.0.0.1",0),None)))
        platform = Client(url,token)
        service.step()
        mutations = []
        request = rpc.request
        def record(method,params):
            result = request(method,params)
            if method=="thread/goal/set":
                mutations.append(params)
            return result
        rpc.request = record

        def command(mid,text):
            receipt = platform.request("POST","worker/command",{"message_id":mid,"text":text})["receipt"]
            card = next(row["event"]["text"] for row in platform.request("GET","worker/display")["items"]
                        if row["seq"]==receipt["output"]["seq"])
            return receipt,card
        self.assertIn("没有目标",command("no_goal","/goal")[1])
        with self.assertRaisesRegex(Fault,"forbidden"):
            Client(url,adapter_token).request("POST","worker/command",{"message_id":"not_trainer","text":"/goal forbidden"})
        objective = "Keep the isolated provider fixture available"
        created,card = command("goal_start","/goal "+objective)
        self.assertIn("进行中",card)
        self.assertIn("tokens",card)
        initial = rpc.request("thread/goal/get",{"threadId":sid})["goal"]
        self.assertEqual((initial["threadId"],initial["objective"],initial["status"],initial["tokenBudget"]),
                         (sid,objective,"active",None))
        self.assertEqual(created["goal_receipt"]["created_at"],initial["createdAt"])
        self.assertIn("已有未完成的目标",command("goal_replace","/goal Try replacing it")[1])
        self.assertEqual(loop.adapter.compact(sid,"goal-blocks-compaction",start=True)["state"],"busy")
        self.assertIn("已暂停",command("goal_pause","/goal pause")[1])

        original = loop.adapter.goal_change
        def drop(session,action,**kw):
            self.assertEqual(original(session,action,**kw)["goal"]["phase"],"active")
            raise Fault(503,"isolated_goal_ack_lost",True)
        loop.adapter.goal_change = drop
        pending,card = command("goal_resume","/goal resume")
        self.assertIn("目标操作待核",card)
        self.assertNotIn("goal_receipt",pending)
        loop.adapter.goal_change = original
        self.assertIn("进行中",command("goal_native_active","/goal")[1])
        self.assertIn("已暂停",command("goal_pause_again","/goal pause")[1])
        self.assertEqual(command("goal_resume","/goal resume")[0],pending)
        last = rpc.request("thread/goal/get",{"threadId":sid})["goal"]
        self.assertEqual((last["status"],last["objective"],last["createdAt"]),("paused",objective,initial["createdAt"]))
        self.assertEqual([item["status"] for item in mutations],["active","paused","active","paused"])
        self.assertTrue(all(event.get("origin")=="user" and event["threadId"]==sid and "tokenBudget" not in event
                            for event in mutations))
        self.assertFalse(loop.hermes.tasks()["items"])
        self.evidence = {"thread_id":sid,"native_goal_creation":created["goal_receipt"],"native_goal_last":last,
                         "mutation_phases":[item["status"] for item in mutations],"origin":"user",
                         "resume_receipt_lost":True,"replay_did_not_resume_paused_goal":True,
                         "boundary":"Actual App Server goal protocol, separately authorized test platform identity and local model fixture"}

    def test_native_app_server_dynamic_tool_keeps_ids_and_fires_one_w04(self):
        root,provider = self.root,self.provider
        rpc = self.enterContext(isolated_app_server(root,self.origin))
        account = rpc.request("account/read",{"refreshToken":False})
        self.assertIsNone(account["account"])
        started = rpc.request("thread/start",{"cwd":root,"ephemeral":False,"dynamicTools":[REPORT_TOOL]})["thread"]
        sid = started["id"]
        host = AppServerHost(rpc,sid)
        host.session = sid
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:host))
        token = guid()
        service = WorkerService(loop.worker,clock)
        source = self.enterContext(running(service.listen({token:"adapter/oguri"},("127.0.0.1",0),None)))
        callback = Client(source,token)
        tid = loop.hermes.create(guid(),"App Server test","Read the synthetic fixture","oguri")["task_id"]
        loop.worker.connect()
        loop.worker.poll()
        clock.advance(10)
        loop.worker.poll()
        loop.worker.poll()
        aid = loop.worker.hold()["held_assignment_id"]
        self.assertEqual(loop.worker.hold()["held_task_id"],tid)
        frame, = [frame for frame in loop.wire.frames(0) if frame["kind"]=="task.dispatch"]
        receipts, native_calls = [],[]
        last = time.monotonic()+16
        while True:
            event = rpc.event(last)
            if event.get("method")=="item/tool/call":
                args = event["params"]["arguments"]
                self.assertEqual((args["task_id"],args["assignment_id"]),(tid,aid))
                if not receipts:
                    # The active turn is suspended waiting for this request's answer, so its input is visible.
                    self.assertTrue(loop.worker.process(frame))
                    self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"working")
                    with self.assertRaisesRegex(Fault,"wrong_native_caller"):
                        host.worker_call(dict(event,params={**event["params"],"threadId":guid()}),callback)
                    with self.assertRaisesRegex(Fault,"native_turn_unconfirmed"):
                        host.worker_call(dict(event,params={**event["params"],"turnId":guid()}),callback)
                    with self.assertRaisesRegex(Fault,"unsupported_native_tool"):
                        host.worker_call(dict(event,params={**event["params"],"tool":"arbitrary_tool"}),callback)
                proof = host.worker_call(event,callback)
                receipts.append(proof)
                native_calls.append(event["params"])
                rpc.send({"id":event["id"],"result":{"success":True,
                          "contentItems":[{"type":"inputText","text":encode(proof)}]}})
            if event.get("method")=="turn/completed":
                self.assertEqual(event["params"]["turn"]["status"],"completed")
                break
        turn, = host.thread(sid,include_turns=True)["turns"]
        inputs = [item for item in turn["items"] if item.get("type")=="userMessage"]
        recorded = [item for item in turn["items"] if item.get("type")=="dynamicToolCall"]
        self.assertEqual(len(inputs),1)
        self.assertEqual(inputs[0]["clientId"],"mowulink-"+frame["delivery_id"])
        self.assertEqual([item["id"] for item in recorded],[call["callId"] for call in native_calls])
        self.assertEqual({call["turnId"] for call in native_calls},{turn["id"]})
        body = {"kind":"task",**frame["payload"]}
        original = loop.adapter.lookup_delivery(frame["delivery_id"],body,session=sid)
        self.assertEqual((original["native_id"],original["turn_id"]),(inputs[0]["id"],turn["id"]))
        expected = {"recorded":True,"task_state":"completed_reported","release_assignment_id":aid}
        self.assertEqual(receipts,[expected,expected])
        self.assertIsNone(loop.worker.hold()["held_assignment_id"])
        details = loop.hermes.inspect(tid)
        self.assertEqual(details["task"]["state"],"completed_reported")
        notice, = loop.manager_wire.frames(0)
        self.assertEqual(notice["payload"]["task_id"],tid)
        self.assertEqual(provider.observed,[(True,0),(True,1),(True,2)])
        public = host.public_turn(sid,turn["id"])
        self.assertEqual([(item["kind"],item["text"]) for item in public],
                         [("final","Local model fixture accepted the receipt.")])
        self.evidence = {"cli_version":started["cliVersion"],"thread_id":sid,"task_id":tid,"assignment_id":aid,"receipt":original,
                         "native_client_input_id":inputs[0]["clientId"],
                         "native_tool_calls":native_calls,"router_receipts":receipts,
                         "notification_delivery_id":notice["delivery_id"],"native_input_count":len(inputs),
                         "report_status_before_review":details["task"]["state"],"account_present":False,
                         "scripted_provider_requests":provider.observed,
                         "boundary":"Bundled native Codex App Server using a scripted local HTTP model. Desktop and authenticated model not exercised."}

    def test_native_goal_finishing_keeps_original_assignment_until_bound_app_server_report(self):
        self.provider.formal_goal = run = {}
        rpc = self.enterContext(isolated_app_server(self.root,self.origin,["features.goals=true"]))
        started = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False,"dynamicTools":[REPORT_TOOL]})["thread"]
        sid = started["id"]
        host = AppServerHost(rpc,sid)
        host.session = sid
        terminals = Queue()
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:host))
        token = guid()
        service = WorkerService(loop.worker,clock)
        url = self.enterContext(running(service.listen({token:"adapter/oguri"},("127.0.0.1",0),None)))
        self.enterContext(worker_callbacks(host,Client(url,token),lambda:None,
                                           notify_terminal=lambda tid,status:terminals.put((tid,status))))
        tid = loop.hermes.create(guid(),"Native formal goal","Set the native goal and report on the original order","oguri")["task_id"]
        loop.worker.connect()
        loop.worker.poll()
        clock.advance(10)
        loop.worker.poll()
        loop.worker.poll()
        dispatch, = [frame for frame in loop.wire.frames(0) if frame["kind"]=="task.dispatch"]
        aid = dispatch["payload"]["assignment_id"]

        def await_terminal():
            until = time.monotonic()+16
            while time.monotonic()<until:
                try:
                    return terminals.get(timeout=.05)
                except Empty:
                    loop.worker.poll()
            self.fail("isolated App Server did not finish the native turn")

        first,first_status = await_terminal()
        continuation,continuation_status = await_terminal()
        self.assertEqual((first_status,continuation_status),("completed","completed"))
        self.assertNotEqual(first,continuation)
        self.assertTrue(loop.worker.process(dispatch))
        initial = loop.adapter.lookup_delivery(dispatch["delivery_id"],{"kind":"task",**dispatch["payload"]},session=sid)
        self.assertEqual((initial["session"],initial["turn_id"]),(sid,first))
        goal = host.goal_view(sid)["goal"]
        self.assertEqual((goal["phase"],goal["objective"],goal["token_budget"]),("complete",GOAL_FIXTURE,None))
        self.assertEqual(run["native_tools"],["create_goal","get_goal","update_goal","worker_report"])
        self.assertIn("native_turn_unconfirmed",encode(run["continuation_report_reply"]))
        before = loop.hermes.inspect(tid)
        self.assertEqual(before["task"]["state"],"working")
        self.assertIsNone(before["last_report"])
        self.assertEqual(loop.worker.hold(),{"held_task_id":tid,"held_assignment_id":aid})
        self.assertEqual(loop.manager_wire.frames(0),[])
        independent = host.read_turn(sid,continuation)
        self.assertEqual(independent["rootTurnId"],first)
        self.assertFalse(any(item["type"]=="userMessage" for item in independent["items"]))
        self.assertFalse(any(event["kind"]=="native_input" for event in host.public_turn(sid,continuation)))

        clock.advance(1)
        loop.worker.poll()
        loop.worker.poll()
        clock.advance(10)
        loop.worker.poll()
        loop.worker.poll()
        reported,reported_status = await_terminal()
        self.assertEqual(reported_status,"completed")
        self.assertNotIn(reported,(first,continuation))
        probe, = [frame for frame in loop.wire.frames(0) if frame["kind"]=="task.probe"]
        self.assertTrue(loop.worker.process(probe))
        proof = loop.adapter.lookup_delivery(probe["delivery_id"],{"kind":"task_probe",**probe["payload"]},session=sid)
        self.assertEqual((proof["session"],proof["turn_id"],probe["payload"]["assignment_id"]),(sid,reported,aid))
        calls = [item for item in host.read_turn(sid,reported)["items"] if item["type"]=="dynamicToolCall"]
        self.assertEqual(loop.worker.hold(),{"held_task_id":None,"held_assignment_id":None},calls)
        w04_calls = [item["id"] for item in calls]
        self.assertEqual(w04_calls,["native_fixture_0","native_fixture_1"])
        expected = {"recorded":True,"task_state":"completed_reported","release_assignment_id":aid}
        self.assertTrue(all(item["success"] and json.loads(item["contentItems"][0]["text"])==expected
                            for item in calls),calls)
        detail = loop.hermes.inspect(tid)
        self.assertEqual(detail["task"]["state"],"completed_reported")
        notify, = loop.manager_wire.frames(0)
        self.assertEqual(notify["payload"]["task_id"],tid)
        turns = host.thread(sid,include_turns=True)["turns"]
        self.assertEqual([turn["id"] for turn in turns],[first,continuation,reported])
        inputs = [item["clientId"] for turn in turns for item in turn["items"] if item["type"]=="userMessage"]
        self.assertEqual(inputs,["mowulink-"+dispatch["delivery_id"],"mowulink-"+probe["delivery_id"]])
        self.evidence = {"thread_id":sid,"task_id":tid,"assignment_id":aid,"native_goal":goal,
                         "dispatch_receipt":initial,"continuation_turn_id":continuation,
                         "continuation_report_rejected":True,"status_before_probe":before["task"]["state"],
                         "probe_receipt":proof,"w04_calls":w04_calls,"same_w04_receipt":expected,
                         "notification_delivery_id":notify["delivery_id"],
                         "router_final":detail["task"]["state"],"native_input_count":len(inputs),
                         "boundary":"Actual Codex App Server goal continuation and W04; scripted local model, test Router clock, no account"}

    def pending_native_task(self,rpc):
        clock = Clock()
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False,"dynamicTools":[REPORT_TOOL]})["thread"]["id"]
        host = AppServerHost(rpc,sid)
        host.session = sid
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:host))
        tid = loop.hermes.create(guid(),"Pending native test","Original assignment content","oguri")["task_id"]
        loop.worker.connect()
        loop.worker.poll()
        clock.advance(10)
        loop.worker.poll()
        loop.worker.poll()
        deadline = time.monotonic()+14
        while True:
            event = rpc.event(deadline)
            if event.get("method")=="item/tool/call":
                pending_call = event["params"]
                break
        self.assertEqual(pending_call["threadId"],sid)
        frame, = [frame for frame in loop.wire.frames(0) if frame["kind"]=="task.dispatch"]
        self.assertTrue(loop.worker.process(frame))
        proof = loop.adapter.lookup_delivery(frame["delivery_id"],{"kind":"task",**frame["payload"]},session=sid)
        self.assertEqual(proof["turn_id"],pending_call["turnId"])
        self.assertIsNone(loop.hermes.inspect(tid)["last_report"])
        return loop,host,frame,proof,pending_call

    def test_native_fork_cannot_confirm_or_resend_the_parent_delivery(self):
        workspace = str(Path(self.root).resolve())
        old_mid,new_mid = guid(),guid()
        before_text,after_text = "Parent private fixture","Fork private fixture"
        old_did,new_did = "chat-"+old_mid,"chat-"+new_mid
        old_input = {"kind":"chat","message_id":old_mid,"text":before_text}
        old_digest = hashlib.sha256(encode(old_input).encode()).hexdigest()

        def settle(rpc,session):
            until = time.monotonic()+12
            while True:
                packet = rpc.event(until)
                if packet["method"]=="turn/completed" and packet["params"]["threadId"]==session:
                    self.assertEqual(packet["params"]["turn"]["status"],"completed")
                    return packet["params"]["turn"]["id"]

        def user_inputs(host):
            return [(turn["id"],item["id"],item["clientId"]) for turn in host.thread(host.thread_id,include_turns=True)["turns"]
                    for item in turn["items"] if item["type"]=="userMessage"]

        with isolated_app_server(self.root,self.origin) as rpc:
            parent = rpc.request("thread/start",{"cwd":workspace,"ephemeral":False,"dynamicTools":[REPORT_TOOL]})["thread"]["id"]
            host = AppServerHost(rpc,parent)
            host.session = parent
            loop = self.enterContext(Loop.isolated(host_factory=lambda _path,_name:host))
            loop.worker.chat(old_mid,before_text)
            settle(rpc,parent)
            original = loop.worker.chat(old_mid,before_text)
            old_inputs = user_inputs(host)
            self.assertEqual(old_inputs,[(original["turn_id"],original["native_id"],"mowulink-"+old_did)])
            fork = rpc.request("thread/fork",{"threadId":parent,"cwd":workspace,"ephemeral":False})["thread"]
            child = fork["id"]
            self.assertEqual(fork["forkedFromId"],parent)
            self.assertNotEqual(parent,child)

        child_db = loop.root/"isolated-fork-adapter.db"
        counts = len(self.provider.observed)
        with isolated_app_server(self.root,self.origin) as rpc:
            resumed = rpc.request("thread/resume",{"threadId":child,"cwd":workspace})["thread"]
            host = AppServerHost(rpc,child)
            host.bind_fork_history(child_db,resumed)
            adapter = HostAdapter(child_db,host,child)
            worker = Worker(loop.worker.db.path,loop.worker.worker_id,loop.wire,adapter)
            self.assertEqual(user_inputs(host),old_inputs)
            self.assertIsNone(host.lookup_expected(child,old_did,old_input,old_digest))
            self.assertEqual(worker.chat(old_mid,before_text),original)
            with worker.db.transaction() as db:
                db.execute("UPDATE chat_deliveries SET receipt=NULL WHERE message_id=?",(old_mid,))
            self.assertIsNone(worker.chat(old_mid,before_text))
            with self.assertRaisesRegex(Fault,"inherited_native_delivery"):
                adapter.deliver(old_did,old_input,session=child)
            self.assertIsNone(adapter.lookup_delivery(old_did,old_input,session=child))
            with self.assertRaisesRegex(Fault,"native_turn_unconfirmed"):
                adapter.input_for_turn(child,original["turn_id"])
            self.assertEqual(user_inputs(host),old_inputs)
            self.assertEqual(len(self.provider.observed),counts)
            worker.chat(new_mid,after_text)
            new_turn = settle(rpc,child)
            posted = worker.chat(new_mid,after_text)
            self.assertEqual((posted["session"],posted["turn_id"]),(child,new_turn))
            self.assertEqual(user_inputs(host),old_inputs+[(new_turn,posted["native_id"],"mowulink-"+new_did)])

        # Lose only the new receipt in the two test ledgers and reopen the same fork.
        with adapter.db.transaction() as db:
            db.execute("UPDATE intents SET receipt=NULL,native_id=NULL WHERE delivery_id=?",(new_did,))
        with worker.db.transaction() as db:
            db.execute("UPDATE chat_deliveries SET receipt=NULL WHERE message_id=?",(new_mid,))
        with isolated_app_server(self.root,self.origin) as rpc:
            resumed = rpc.request("thread/resume",{"threadId":child,"cwd":workspace})["thread"]
            host = AppServerHost(rpc,child)
            host.bind_fork_history(child_db,resumed)
            adapter = HostAdapter(child_db,host,child)
            worker = Worker(loop.worker.db.path,loop.worker.worker_id,loop.wire,adapter)
            self.assertIsNone(worker.chat(old_mid,before_text))
            self.assertEqual(worker.chat(new_mid,after_text),posted)
            self.assertIsNone(adapter.lookup_delivery(old_did,old_input,session=child))
            rpc.request("thread/resume",{"threadId":parent,"cwd":workspace})
            self.assertEqual(user_inputs(AppServerHost(rpc,parent)),old_inputs)
            self.assertEqual(len(user_inputs(host)),2)
        self.assertEqual(len(self.provider.observed),counts+1)
        self.assertEqual(loop.hermes.tasks()["items"],[])
        self.evidence = {"parent_thread":parent,"child_thread":child,"copied_parent_input":original,
                         "parent_delivery_confirmed_in_child":False,"parent_delivery_resent":False,
                         "child_receipt":posted,"child_receipt_recovered_after_cold_reopen":True,
                         "parent_input_count":1,"child_input_count_including_parent_history":2,
                         "new_model_requests":1,
                         "boundary":"Actual Codex App Server fork, manual isolated adapter selection, scripted local model"}

    def test_cold_switch_and_return_preserve_receipts_and_use_the_target_native_session(self):
        root = Path(self.root)
        state = root/"source-state"
        state.mkdir()
        labels = ("router:main","worker:oguri","agent:oguri","hermes:main","trigger:source")
        original_paths = {label:state/filename(label) for label in labels}
        revision = "a"*40
        clock = Clock()
        manager_native = FixtureHost(root/"manager-inputs.db","manager-test-session")
        manager_host = HostAdapter(root/"manager-host.db",manager_native,manager_native.session)
        old_message = {"message_id":guid(),"text":"Original isolated session"}
        new_message = {"message_id":guid(),"text":"New isolated session"}

        @contextmanager
        def station(paths,rpc,thread):
            sid = thread["id"]
            host = AppServerHost(rpc,sid)
            host.bind_fork_history(paths["agent:oguri"],thread)
            with handoff_station(paths,clock,host,sid,manager_host) as (worker,manager,_adapter,platform,callback):
                completed = Event()
                with worker_callbacks(host,callback,completed.set):
                    yield worker,manager,host,platform,completed

        def inputs(host):
            return [(turn["id"],item["id"],item["clientId"]) for turn in host.thread(host.thread_id,include_turns=True)["turns"]
                    for item in turn["items"] if item["type"]=="userMessage"]

        def chat(platform,msg):
            return platform.request("POST","worker/chat",msg)["receipt"]

        with isolated_app_server(root,self.origin) as rpc:
            parent = rpc.request("thread/start",{"cwd":str(root),"ephemeral":False,"dynamicTools":[REPORT_TOOL]})["thread"]
            with station(original_paths,rpc,parent) as (worker,_manager,host,platform,finished):
                chat(platform,old_message)
                self.assertTrue(finished.wait(12))
                old = chat(platform,old_message)
                original_inputs = inputs(host)
                connection_id = worker.session
                self.assertEqual(original_inputs,
                                 [(old["turn_id"],old["native_id"],"mowulink-chat-"+old_message["message_id"])])
            child = rpc.request("thread/fork",{"threadId":parent["id"],"cwd":str(root),"ephemeral":False})["thread"]
        source,target = parent["id"],child["id"]
        snapshot = root/"source-snapshot"
        fingerprint = capture(original_paths,revision,snapshot)["manifest_sha256"]
        staged = root/"trial"
        plan = stage_handoff_cli(snapshot,fingerprint,source,target,staged)
        self.assertEqual(json.loads((staged/"native-handoff.json").read_text()),plan)
        self.assertEqual(plan["expected_session"],target)
        trial_paths = {label:staged/filename(label) for label in labels}
        trial_paths["agent:oguri"] = Path(plan["target_adapter_db"])
        incorrect = RemoteAdapter(Client("http://127.0.0.1:1","unused"),source)
        with self.assertRaisesRegex(Fault,"native_binding_change_requires_handoff"):
            Worker(trial_paths["worker:oguri"],"oguri",None,incorrect)
        self.assertFalse(trial_paths["agent:oguri"].exists())

        with isolated_app_server(root,self.origin) as rpc:
            resumed = rpc.request("thread/resume",{"threadId":target,"cwd":str(root)})["thread"]
            self.assertEqual(resumed["forkedFromId"],source)
            with station(trial_paths,rpc,resumed) as (worker,manager,host,platform,finished):
                with self.assertRaisesRegex(Fault,"adapter_ledger_bound_to_another_session"):
                    HostAdapter(Path(plan["previous_adapter_db"]),host,target)
                self.assertEqual(worker.session,connection_id)
                self.assertEqual(chat(platform,old_message),old)
                self.assertEqual(inputs(host),original_inputs)
                chat(platform,new_message)
                self.assertTrue(finished.wait(12))
                new = chat(platform,new_message)
                self.assertEqual(new["session"],target)
                finished.clear()
                task_id = manager.create(guid(),"Clean handoff","Report from the child test session","oguri")["task_id"]
                worker.poll()
                clock.advance(10)
                worker.poll()
                deadline = time.monotonic()+12
                while not finished.wait(.02) and time.monotonic()<deadline:
                    worker.poll()
                self.assertTrue(finished.is_set())
                worker.poll()
                order = manager.inspect(task_id)
                task_turn = host.thread(target,include_turns=True)["turns"][-1]
                self.assertEqual(order["task"]["state"],"completed_reported",task_turn["items"])
                self.assertIsNone(worker.hold()["held_assignment_id"])
                records, = manager.router.frames(0)
                self.assertEqual(records["payload"]["task_id"],task_id)
                child_inputs = inputs(host)
                self.assertEqual(len(child_inputs),3)
                native_calls = [item for item in task_turn["items"] if item["type"]=="dynamicToolCall"]
                self.assertEqual(len(native_calls),2)
                self.assertTrue(all(call["success"] for call in native_calls),native_calls)
                expected = {"recorded":True,"task_state":"completed_reported",
                            "release_assignment_id":order["last_report"]["assignment_id"]}
                self.assertEqual([json.loads(call["contentItems"][0]["text"]) for call in native_calls],[expected]*2)
                self.assertEqual(order["last_report"]["summary"],"Isolated App Server result")
                self.assertEqual(child_inputs[1],(new["turn_id"],new["native_id"],"mowulink-chat-"+new_message["message_id"]))
        trial_snapshot = root/"trial-snapshot"
        trial_fingerprint = capture(trial_paths,revision,trial_snapshot)["manifest_sha256"]
        back = root/"return"
        stage_handoff_cli(trial_snapshot,trial_fingerprint,target,source,back)
        return_paths = {label:back/filename(label) for label in labels}
        return_paths["agent:oguri"] = Path(plan["previous_adapter_db"])
        before_return = len(self.provider.observed)
        with isolated_app_server(root,self.origin) as rpc:
            resumed = rpc.request("thread/resume",{"threadId":source,"cwd":str(root)})["thread"]
            with station(return_paths,rpc,resumed) as (worker,manager,host,platform,_):
                self.assertEqual(worker.session,connection_id)
                self.assertEqual(chat(platform,old_message),old)
                self.assertEqual(chat(platform,new_message),new)
                self.assertEqual(inputs(host),original_inputs)
                result = manager.inspect(task_id)
                self.assertEqual(result["last_report"],order["last_report"])
                self.assertEqual(manager.router.frames(0),[records])
        extra_model_requests = len(self.provider.observed)-before_return
        self.assertEqual(extra_model_requests,0)
        verify(snapshot,fingerprint)
        verify(trial_snapshot,trial_fingerprint)
        self.evidence = {"source_session":source,"target_session":target,"original_inputs":original_inputs,
            "target_inputs_including_fork_history":child_inputs,"original_receipt":old,"target_receipt":new,
            "task_id":task_id,"assignment_id":order["last_report"]["assignment_id"],
            "native_tool_calls":[call["id"] for call in native_calls],"native_tool_receipt":expected,
            "notification_id":records["delivery_id"],"report_state":result["task"]["state"],
            "return_preserved_both_receipts":True,"model_requests_added_on_return":extra_model_requests,
            "boundary":"Independent real App Server threads, isolated Router and Worker, local scripted model and platform"}

    def test_native_command_approval_cancels_exact_request_and_does_not_infer_missing_proof(self):
        self.provider.approval_commands = dict.fromkeys(("one","two","allow","expire"),"pwd")
        root = Path(self.root)
        rpc = self.enterContext(isolated_app_server(self.root,self.origin,
                                ['approval_policy="on-request"','sandbox_mode="read-only"']))
        session = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False})["thread"]["id"]
        host = AppServerHost(rpc,session,relay_command_approvals=True)
        terminals = Queue()
        worker_token,manager,operator,other,gateway,trainer = (guid() for _ in range(6))
        clock_offset = 0
        router = self.enterContext(running(serve_loopback(Orders(root/"approval-router.db",clock=lambda:time.time()+clock_offset),
                  {worker_token:"worker/dev",manager:"hermes",operator:"approver/dev",other:"approver/other"})))
        adapter = HostAdapter(root/"approval-adapter.db",host,session)
        worker = Worker(root/"approval-worker.db","dev",Client(router,worker_token),adapter)
        worker.connect()
        service = WorkerService(worker)
        url = self.enterContext(running(service.listen({gateway:"adapter/dev",trainer:"platform/dev"},("127.0.0.1",0),None)))
        approver, outsider, manager_wire = (Client(router,token) for token in (operator,other,manager))
        callback,platform = Client(url,gateway),Client(url,trainer)

        lost_router_reply,seen_closures,native_responses,native_offers,ignore_closure = Event(),[],[],[],Event()
        send,event,call = rpc.send,rpc.event,callback.request
        def record_response(packet):
            if "decision" in packet.get("result",{}):
                native_responses.append(packet)
            send(packet)
        def read_event(deadline):
            while True:
                packet = event(deadline)
                if packet.get("method")=="item/commandExecution/requestApproval":
                    native_offers.append(packet)
                if not (ignore_closure.is_set() and packet.get("method")=="serverRequest/resolved"):
                    return packet
        def drop_one_response(method,path,*args,**kwargs):
            receipt = call(method,path,*args,**kwargs)
            if method=="PUT" and path.endswith("/closed"):
                seen_closures.append(receipt)
                if not lost_router_reply.is_set():
                    lost_router_reply.set()
                    raise ConnectionError("lost isolated closure response")
            return receipt
        rpc.send,rpc.event,callback.request = record_response,read_event,drop_one_response
        self.enterContext(worker_callbacks(host,callback,lambda:None,
                          notify_terminal=lambda tid,status:terminals.put((tid,status))))

        def until(predicate):
            deadline = time.monotonic()+10
            while time.monotonic()<deadline:
                result = predicate()
                if result:
                    return result
                time.sleep(.05)
            self.fail("approval fixture did not settle")

        def open_one(variant):
            received = worker.chat(guid(),"Approval fixture "+variant)
            def matching():
                return next((row for row in approver.request("GET","approvals/dev")["items"]
                             if row["request"]["item_id"]=="trial-approval-"+variant),None)
            offered = until(matching)
            turn = offered["request"]["turn_id"]
            if received:
                self.assertEqual(received["turn_id"],turn)
            self.assertEqual(offered["state"],"pending")
            self.assertEqual(offered["request"]["session"],session)
            self.assertEqual(offered["request"]["decisions"],["accept","cancel"])
            native, = [offer["params"] for offer in native_offers if offer["params"]["turnId"]==turn]
            self.assertEqual(offered["request"]["command"],native["command"])
            self.assertTrue(native["command"].endswith(" pwd"),native["command"])
            self.assertEqual(offered["request"]["environment_id"],native.get("environmentId"))
            return offered,"approvals/dev/"+offered["approval_id"]

        first,route = open_one("one")
        decision = {"request_digest":first["request_digest"],"decision":"cancel"}
        with self.assertRaisesRegex(Fault,"forbidden"):
            outsider.request("PUT",route+"/decision",decision)
        with self.assertRaisesRegex(Fault,"forbidden"):
            platform.request("PUT","worker/"+route+"/decision",decision)
        self.assertEqual(native_responses,[])
        decided = approver.request("PUT",route+"/decision",decision)
        self.assertEqual(decided["state"],"decided")
        turn,status = terminals.get(timeout=10)
        self.assertEqual((turn,status),(first["request"]["turn_id"],"interrupted"))
        until(lambda:len(seen_closures)==2)
        closed = approver.request("GET",route)
        self.assertEqual(seen_closures,[closed,closed])
        self.assertEqual(closed["state"],"native_closed")
        self.assertEqual(closed["attempted_response"],{"source":"operator","decision":"cancel"})

        ignore_closure.set()
        second,route = open_one("two")
        decision = {"request_digest":second["request_digest"],"decision":"cancel"}
        decided = approver.request("PUT",route+"/decision",decision)
        turn,status = terminals.get(timeout=10)
        self.assertEqual((turn,status),(second["request"]["turn_id"],"interrupted"))
        self.assertEqual(approver.request("PUT",route+"/decision",decision),decided)
        until(lambda:len(native_responses)==2)
        time.sleep(.6)  # Allow additional pump cycles to expose any incorrect native resend.
        self.assertEqual(approver.request("GET",route),decided)
        self.assertIsNone(decided["attempted_response"])
        ignore_closure.clear()

        accepted,accept_route = open_one("allow")
        accepted_turn = accepted["request"]["turn_id"]
        accept = {"request_digest":accepted["request_digest"],"decision":"accept"}
        with self.assertRaisesRegex(Fault,"approval_view_changed"):
            approver.request("PUT",accept_route+"/decision",dict(accept,request_digest=first["request_digest"]))
        self.assertEqual(len(native_responses),2)
        approver.request("PUT",accept_route+"/decision",accept)
        self.assertEqual(terminals.get(timeout=10),(accepted_turn,"completed"))

        def accepted_receipt():
            result = approver.request("GET",accept_route)
            return result if result["state"]=="native_closed" else None

        native_accepted = until(accepted_receipt)
        self.assertEqual(native_accepted["attempted_response"],{"source":"operator","decision":"accept"})
        self.assertEqual(approver.request("PUT",accept_route+"/decision",accept),native_accepted)

        expired,expired_route = open_one("expire")
        clock_offset = 601  # Advance only the isolated Router clock.
        turn,status = terminals.get(timeout=10)
        self.assertEqual((turn,status),(expired["request"]["turn_id"],"interrupted"))
        def expired_receipt():
            result = approver.request("GET",expired_route)
            return result if result["state"]=="native_closed" else None
        confirmed_expiry = until(expired_receipt)
        self.assertEqual(confirmed_expiry["attempted_response"],{"source":"expiry","decision":"cancel"})
        self.assertIsNone(confirmed_expiry["decision"])
        self.assertEqual(native_responses,[{"id":offer["id"],"result":{"decision":decision}}
                         for offer,decision in zip(native_offers,("cancel","cancel","accept","cancel"),strict=True)])
        turns = host.thread(session,include_turns=True)["turns"]
        (executed_turn,command), = [(turn["id"],item) for turn in turns for item in turn["items"]
                                  if item["type"]=="commandExecution"]
        self.assertEqual(executed_turn,accepted_turn)
        self.assertEqual(command["command"],accepted["request"]["command"])
        self.assertEqual(command["status"],"completed")
        self.assertEqual(command["exitCode"],0)
        self.assertEqual(Path(command["aggregatedOutput"].strip()).resolve(),root.resolve())
        self.assertEqual(manager_wire.request("GET","tasks")["items"],[])
        self.assertEqual(platform.request("GET","worker/display")["items"],[])
        self.evidence = {"native_thread":session,"requests":[first["approval_id"],second["approval_id"],expired["approval_id"]],
                         "native_turn_ids":[offer["params"]["turnId"] for offer in native_offers],
                         "native_request_ids":[offer["id"] for offer in native_offers],
                         "native_decisions_sent_once":[packet["result"]["decision"] for packet in native_responses],
                         "first_router_state":closed["state"],
                         "first_closure_router_attempts":sum(receipt["approval_id"]==first["approval_id"] for receipt in seen_closures),
                         "second_router_state":decided["state"],"native_closure_deliberately_dropped":True,
                         "expired_approval_native_response":confirmed_expiry["attempted_response"],
                         "native_commands_executed":1,"ordinary_chat_and_tasks_unchanged":True,
                         "boundary":"Bundled Codex App Server, local scripted model, injected transport loss, isolated Router and Worker",
                         "one_time_acceptance":{"approval_id":accepted["approval_id"],"turn_id":accepted_turn,
                            "item_id":command["id"],"native_status":command["status"],"exit_code":command["exitCode"],
                            "output_matches_isolated_directory":True,"router_state":native_accepted["state"],
                            "attempted_response":native_accepted["attempted_response"],"same_decision_replay":True}}

    def test_native_file_approval_binds_to_exact_changes_and_rejects_missing_or_broad_requests(self):
        root = Path(self.root).resolve()
        modes = ("accept","cancel","missing","broad")
        paths = {mode:[root/(mode+".txt")] for mode in modes}
        paths["accept"].append(root/"another.txt")
        self.provider.approval_patches = {}
        for mode,targets in paths.items():
            for target in targets:
                target.write_text("before\n")
            updates = "".join(f"*** Update File: {target}\n@@\n-before\n+after\n" for target in targets)
            self.provider.approval_patches[mode] = "*** Begin Patch\n"+updates+"*** End Patch"
        rpc = self.enterContext(isolated_app_server(self.root,self.origin,
            ['model="gpt-5.5"','approval_policy="on-request"','sandbox_mode="read-only"']))
        session = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False})["thread"]["id"]
        host = AppServerHost(rpc,session,relay_file_approvals=True)
        terminals = Queue()
        worker_token,manager_token,operator_token,other_token,adapter_token,platform_token = (guid() for _ in range(6))
        router = self.enterContext(running(serve_loopback(Orders(root/"router.db"),{
            worker_token:"worker/dev",manager_token:"hermes",operator_token:"approver/dev",other_token:"approver/other"})))
        worker = Worker(root/"worker.db","dev",Client(router,worker_token),HostAdapter(root/"adapter.db",host,session))
        worker.connect()
        service = WorkerService(worker)
        worker_url = self.enterContext(running(service.listen({adapter_token:"adapter/dev",platform_token:"platform/dev"},
                                                              ("127.0.0.1",0),None)))
        operator,outsider,manager = (Client(router,token) for token in (operator_token,other_token,manager_token))
        platform = Client(worker_url,platform_token)
        callback = Client(worker_url,adapter_token)
        operator_file = config(root,"operator.json",{"url":router,"token":operator_token})
        cli = [sys.executable,"-m","rewrite.service","approvals","--connection",str(operator_file),"--worker-id","dev"]
        native_items,native_requests,native_decisions = {},{},[]
        event,send = rpc.event,rpc.send

        def record_event(deadline):
            while True:
                packet = event(deadline)
                method,params = packet.get("method"),packet.get("params",{})
                if method=="item/started" and params.get("item",{}).get("type")=="fileChange":
                    item = params["item"]
                    native_items[item["id"]] = params
                    if item["id"]=="trial-file-missing":
                        continue
                if method=="item/fileChange/requestApproval":
                    native_requests[params["itemId"]] = packet
                    if params["itemId"]=="trial-file-broad":
                        return dict(packet,params=dict(params,grantRoot=str(root)))
                return packet

        def record_decision(packet):
            if "decision" in packet.get("result",{}):
                native_decisions.append((packet["id"],packet["result"]["decision"]))
            send(packet)

        rpc.event,rpc.send = record_event,record_decision
        self.enterContext(worker_callbacks(host,callback,lambda:None,
                          notify_item=lambda item:host.publish_public_item(callback,item),
                          notify_terminal=lambda tid,status:terminals.put((tid,status))))

        def until(predicate):
            deadline = time.monotonic()+10
            while time.monotonic()<deadline:
                value = predicate()
                if value:
                    return value
                time.sleep(.05)
            self.fail("native file approval fixture did not settle")

        def records():
            return {row["request"]["item_id"]:row for row in operator.request("GET","approvals/dev")["items"]}

        receipts = {}
        for mode in modes:
            item_id = "trial-file-"+mode
            worker.chat(guid(),"File approval fixture "+mode)
            if mode in ("accept","cancel"):
                offered = until(lambda:records().get(item_id))
                approval_id = offered["approval_id"]
                route = "approvals/dev/"+approval_id
                native = native_requests[item_id]
                changes = native_items[item_id]["item"]["changes"]
                self.assertEqual({change["path"] for change in changes},{str(path) for path in paths[mode]})
                self.assertTrue(all(change["diff"]=="@@ -1 +1 @@\n-before\n+after\n" for change in changes))
                self.assertEqual(offered["request"],{"kind":"file_change","session":session,
                    "turn_id":native["params"]["turnId"],"item_id":item_id,"changes":changes,
                    "decisions":["accept","cancel"]})
                self.assertEqual(offered["request_digest"],hashlib.sha256(encode(offered["request"]).encode()).hexdigest())
                self.assertEqual(offered["state"],"pending")
                self.assertTrue(all(path.read_text()=="before\n" for path in paths[mode]))
                with self.assertRaisesRegex(Fault,"forbidden"):
                    outsider.request("GET",route)
                with self.assertRaisesRegex(Fault,"forbidden"):
                    platform.request("GET","worker/"+route)
                decision = {"request_digest":offered["request_digest"],"decision":mode}
                with self.assertRaisesRegex(Fault,"approval_view_changed"):
                    operator.request("PUT",route+"/decision",dict(decision,request_digest="wrong-diff-digest"))
                with self.assertRaisesRegex(Fault,"unsupported_approval_decision"):
                    operator.request("PUT",route+"/decision",dict(decision,decision="acceptForSession"))
                if mode=="accept":
                    shown = subprocess.check_output([*cli,"show",approval_id],text=True,timeout=10)
                    self.assertEqual(json.loads(shown),offered)
                    accepted = subprocess.check_output([*cli,"decide",approval_id,"--request-digest",
                        decision["request_digest"],"--decision","accept"],text=True,timeout=10)
                    self.assertEqual(json.loads(accepted)["decision"],"accept")
                else:
                    operator.request("PUT",route+"/decision",decision)
            turn,status = terminals.get(timeout=10)
            expected = "completed" if mode=="accept" else "interrupted"
            self.assertEqual((turn,status),(native_requests[item_id]["params"]["turnId"],expected))
            self.assertTrue(all(path.read_text()==("after\n" if mode=="accept" else "before\n") for path in paths[mode]))
            if mode in ("accept","cancel"):
                def closed():
                    row = records().get(item_id)
                    return row if row and row["state"]=="native_closed" else None
                receipt = until(closed)
                self.assertEqual(receipt["attempted_response"],{"decision":mode,"source":"operator"})
                self.assertEqual(operator.request("PUT",route+"/decision",decision),receipt)
                receipts[mode] = {"approval_id":approval_id,"request_digest":offered["request_digest"],"turn":turn,
                                  "closure":receipt["attempted_response"],"files":len(paths[mode])}

        self.assertEqual(native_decisions,[(native_requests["trial-file-"+mode]["id"],
                         "accept" if mode=="accept" else "cancel") for mode in modes])
        self.assertEqual(set(records()),{"trial-file-accept","trial-file-cancel"})
        notices = operator.frames(0)
        self.assertCountEqual([n["payload"] for n in notices],
                              [{"approval_id":r["approval_id"],"worker_id":"dev"} for r in receipts.values()])
        self.assertEqual(manager.frames(0),[])
        native_turns = host.thread(session,include_turns=True)["turns"]
        for turn in native_turns:
            host.publish_public_turn(session,turn["id"],callback,turn)
        display_rows = platform.request("GET","worker/display")["items"]
        self.assertTrue(display_rows)  # The approved turn has an ordinary public model reply.
        display_text = encode(display_rows)
        self.assertFalse(any(str(path) in display_text for targets in paths.values() for path in targets))
        self.assertNotIn("@@ -1 +1 @@",display_text)
        completed = [item for turn in native_turns for item in turn["items"] if item["type"]=="fileChange"]
        self.assertEqual([(item["id"],item["status"]) for item in completed],[("trial-file-accept","completed")])
        self.evidence = {"native_session":session,"receipts":receipts,
                         "native_requests":[{"item":item,"request_id":packet["id"],"turn":packet["params"]["turnId"]}
                                            for item,packet in native_requests.items()],
                         "only_completed_item":completed[0]["id"],"decisions_sent_once":native_decisions,
                         "only_approved_bytes_changed":True,"missing_diff_and_directory_grant_refused":True,
                         "operator_notice_count":len(notices),"other_notice_count":0,"operator_cli_verified":True,
                         "diff_excluded_from_display":True,
                         "boundary":"Actual bundled App Server, HTTP Router and Worker. Local model and operator fixtures. Missing diff and directory scope injected."}

    def test_native_approval_deadline_survives_lost_intake_and_late_router_accept(self):
        root = Path(self.root).resolve()
        target = root/"deadline.txt"
        target.write_bytes(b"before\n")
        self.provider.approval_patches = {"deadline":f"*** Begin Patch\n*** Update File: {target}\n"
                                                    "@@\n-before\n+after\n*** End Patch"}
        self.provider.approval_commands = {"deadline":"pwd"}
        rpc = self.enterContext(isolated_app_server(self.root,self.origin,
            ['model="gpt-5.5"','approval_policy="on-request"','sandbox_mode="read-only"']))
        session = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False})["thread"]["id"]
        host = AppServerHost(rpc,session,relay_command_approvals=True,relay_file_approvals=True)
        clock = Clock()
        host.approvals.clock = clock
        worker_token,operator_token,adapter_token = (guid() for _ in range(3))
        router = self.enterContext(running(serve_loopback(Orders(root/"router.db"),
                                         {worker_token:"worker/dev",operator_token:"approver/dev"})))
        worker_wire,operator = Client(router,worker_token),Client(router,operator_token)
        worker = Worker(root/"worker.db","dev",worker_wire,HostAdapter(root/"adapter.db",host,session))
        worker.connect()
        endpoint = self.enterContext(running(WorkerService(worker).listen({adapter_token:"adapter/dev"},
                                                                  ("127.0.0.1",0),None)))
        callback = Client(endpoint,adapter_token)
        holding = {name:Event() for name in ("file","command")}
        release = {name:Event() for name in holding}
        releases = []
        registrations,closures = [],[]
        request = worker_wire.request

        def obstruct(method,path,body=None,*args,**kwargs):
            if method=="POST" and path.startswith("approvals/dev/"):
                item = body["request"]["item_id"]
                registrations.append(item)
                if item=="trial-file-deadline" and not holding["file"].is_set():
                    holding["file"].set()
                    release["file"].wait(8)
                    raise Fault(503,"isolated_router_intake_unavailable",True)
                result = request(method,path,body,*args,**kwargs)
                if item=="trial-approval-deadline" and result["state"]=="decided" and not holding["command"].is_set():
                    holding["command"].set()
                    release["command"].wait(8)
                    releases.append(result)
                return result
            result = request(method,path,body,*args,**kwargs)
            if method=="PUT" and path.endswith("/closed"):
                closures.append(result)
                if len(closures)==1:
                    raise Fault(503,"isolated_deadline_closure_receipt_lost",True)
            return result

        worker_wire.request = obstruct
        packets,decisions,native_closed = {},[],[]
        outgoing,incoming = rpc.send,rpc.event
        terminals = Queue()

        def take(deadline):
            packet = incoming(deadline)
            if packet["method"] in ("item/fileChange/requestApproval","item/commandExecution/requestApproval"):
                packets[packet["params"]["itemId"]] = packet
            if packet["method"]=="serverRequest/resolved":
                native_closed.append(packet["params"]["requestId"])
            return packet

        def send(packet):
            if "decision" in packet.get("result",{}):
                decisions.append((packet["id"],packet["result"]["decision"]))
            outgoing(packet)

        rpc.event,rpc.send = take,send
        self.enterContext(worker_callbacks(host,callback,lambda:None,
                          notify_terminal=lambda tid,state:terminals.put((tid,state))))
        self.addCleanup(lambda:[gate.set() for gate in release.values()])

        def wait(condition):
            deadline = time.monotonic()+6
            while time.monotonic()<deadline:
                result = condition()
                if result:
                    return result
                time.sleep(.05)
            self.fail("original approval did not settle")

        def find(item,state):
            return next((row for row in operator.request("GET","approvals/dev")["items"]
                         if row["request"]["item_id"]==item and row["state"]==state),None)

        reports = {}
        for mode,item,prompt in [("file","trial-file-deadline","File approval fixture deadline"),
                                 ("command","trial-approval-deadline","Approval fixture deadline")]:
            worker.chat(guid(),prompt)
            if mode=="command":
                pending = wait(lambda:find(item,"pending"))
                path = "approvals/dev/"+pending["approval_id"]
                decision = {"request_digest":pending["request_digest"],"decision":"accept"}
                self.assertEqual(operator.request("PUT",path+"/decision",decision)["state"],"decided")
            self.assertTrue(holding[mode].wait(4))
            native = packets[item]
            self.assertNotIn((native["id"],"cancel"),decisions)
            if mode=="file":
                self.assertFalse(operator.request("GET","approvals/dev")["items"])
                self.assertEqual(operator.frames(0),[])
            start = time.monotonic()
            clock.advance(TTL_SECONDS)
            self.assertEqual(terminals.get(timeout=3),(native["params"]["turnId"],"interrupted"))
            self.assertIn(native["id"],native_closed)
            self.assertFalse(release[mode].is_set())
            self.assertEqual(target.read_bytes(),b"before\n")
            interrupted_in = round(time.monotonic()-start,3)
            release[mode].set()
            closed = wait(lambda:find(item,"native_closed"))
            self.assertEqual(closed["attempted_response"],{"source":"native_deadline","decision":"cancel"})
            self.assertEqual(closed["decision"],None if mode=="file" else "accept")
            reports[mode] = {"approval_id":closed["approval_id"],"native_id":native["id"],
                             "turn_id":native["params"]["turnId"],"decision":closed["decision"],
                             "source":closed["attempted_response"]["source"],"native_outcome":"interrupted",
                             "interrupted_in_seconds_after_deadline":interrupted_in}
            if mode=="file":
                wait(lambda:len(closures)>=2)
                self.assertEqual(closures[:2],[closed,closed])
                self.assertEqual(registrations.count(item),1)
                self.assertEqual(callback.request("POST","worker/approvals/"+closed["approval_id"],closed["request"]),closed)
                self.assertEqual(operator.frames(0),[])

        self.assertEqual(len(releases),1)
        self.assertEqual(releases[0]["decision"],"accept")
        self.assertEqual(decisions,[(reports[mode]["native_id"],"cancel") for mode in ("file","command")])
        notice, = operator.frames(0)
        self.assertEqual(notice["payload"],{"worker_id":"dev","approval_id":reports["command"]["approval_id"]})
        turns = host.thread(session,include_turns=True)["turns"]
        self.assertEqual(len(turns),2)
        self.assertFalse(any(item["type"] in ("commandExecution","fileChange") for turn in turns for item in turn["items"]))
        self.evidence = {"session_id":session,"requests":reports,"late_accept_recorded_but_not_sent":True,
                         "file_intake_not_registered_and_no_late_notice":True,"first_closure_attempts":2,
                         "on_time_native_replies":decisions,"file_sha256_after":hashlib.sha256(target.read_bytes()).hexdigest(),
                         "boundary":"Actual App Server, HTTP Router and Worker, local model. Router transport blocked with gates and native ten-minute clock advanced."}

    def test_resume_catalog_uses_only_current_app_server_workspace_without_loading_history(self):
        root = Path(self.root)
        work,elsewhere = root/"approved",root/"other"
        work.mkdir()
        elsewhere.mkdir()

        def create(rpc,folder,title):
            sid = rpc.request("thread/start",{"cwd":str(folder),"ephemeral":False})["thread"]["id"]
            rpc.request("turn/start",{"threadId":sid,"input":[{"type":"text","text":"Local catalog fixture",
                                                             "text_elements":[]}]})
            until = time.monotonic()+12
            while True:
                event = rpc.event(until)
                if event["method"]=="turn/completed" and event["params"]["threadId"]==sid:
                    self.assertEqual(event["params"]["turn"]["status"],"completed")
                    break
            rpc.request("thread/name/set",{"threadId":sid,"name":title})
            return sid

        with isolated_app_server(self.root,self.origin) as setup:
            sid = create(setup,work,"Bound demo")
            cold = create(setup,work,"Cold fixture")
            archived = create(setup,work,"Archived hidden")
            other = create(setup,elsewhere,"Other workspace hidden")
            setup.request("thread/archive",{"threadId":archived})
        self.assertEqual(len(self.provider.observed),4)

        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        self.assertEqual(rpc.request("thread/resume",{"threadId":sid,"cwd":str(work)})["thread"]["id"],sid)
        host = AppServerHost(rpc,sid)
        host.session = sid
        loop = self.enterContext(Loop.isolated(host_factory=lambda _path,_name:host))
        service = WorkerService(loop.worker)
        token = guid()
        url = self.enterContext(running(service.listen({token:"platform/oguri"},("127.0.0.1",0),None)))
        platform = Client(url,token)
        service.step()

        def render(mid,text):
            entry = platform.request("POST","worker/command",{"message_id":mid,"text":text})["receipt"]
            card = next(row["event"]["text"] for row in platform.request("GET","worker/display")["items"]
                        if row["seq"]==entry["output"]["seq"])
            return entry,card

        self.assertEqual(service.status()["host_session"],sid)
        self.assertIs(service.status()["native_capabilities"]["session_catalog"],True)
        prior = rpc.request("thread/read",{"threadId":cold,"includeTurns":False})["thread"]["status"]["type"]
        self.assertEqual(prior,"notLoaded")
        entries = host.session_catalog(sid)["items"]
        self.assertEqual({(row["id"],row["title"]) for row in entries},
                         {(sid,"Bound demo"),(cold,"Cold fixture")},entries)
        _,shown = render("list_native_catalog","/resume")
        self.assertIn("Bound demo（当前绑定）",shown)
        self.assertIn(cold,shown)
        self.assertNotIn(archived,shown)
        self.assertNotIn(other,shown)
        found,search = render("search_native_catalog","/resume search Cold fixture")
        self.assertIn(cold,search)
        self.assertNotIn(sid,search)
        self.assertEqual(render("search_native_catalog","/resume search Cold fixture")[0],found)
        self.assertIn("切换会话敬请期待",render("pick_native_catalog","/resume 1")[1])
        self.assertEqual(loop.adapter.bound_session(),sid)
        after = rpc.request("thread/read",{"threadId":cold,"includeTurns":False})["thread"]["status"]["type"]
        self.assertEqual(after,prior)
        self.assertEqual(len(host.thread(sid,include_turns=True)["turns"]),1)
        self.assertEqual(len(self.provider.observed),4)
        self.assertFalse(loop.hermes.tasks()["items"])
        self.evidence = {"thread_id":sid,"visible_entries":entries,"cold_before":prior,"cold_after":after,
                         "out_of_scope_ids":[archived,other],"additional_model_requests":0,
                         "replayed_catalog_receipt":found,
                         "boundary":"Actual App Server, fresh profile and native history. Fixture model seeds catalog and platform is isolated."}

    @unittest.skipUnless(shutil.which("openssl"),"isolated TLS certificate generator required")
    def test_cold_codex_activates_only_after_router_pauses_and_recovers_lost_reply(self):
        root = Path(self.root).resolve()
        profile,project = root/"cold-profile",root/"cold-project"
        profile.mkdir(mode=0o700)
        project.mkdir(mode=0o700)
        self.local_profile(profile)
        provisioned = subprocess.run(self.provision_command(profile,project),cwd=Path(__file__).resolve().parents[1],
                                     capture_output=True,text=True,timeout=30,check=True)
        seed = json.loads(provisioned.stdout)
        sid = seed["thread_id"]
        self.assertEqual(self.provider.observed,[(True,0)])
        with start_app_server(CODEX,profile,project) as review:
            native = review.request("thread/read",{"threadId":sid,"includeTurns":False})["thread"]
            self.assertEqual((native["id"],native["status"]["type"]),(sid,"notLoaded"))

        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock))
        source = loop.native.session
        source_token,cold_token,platform_token,source_callback,cold_callback = (guid() for _ in range(5))
        source_url = self.enterContext(running(serve_adapter(loop.adapter,"oguri",{source_token:"worker/oguri"},
                                                            ("127.0.0.1",0),None)))
        source_port = RemoteAdapter(Client(source_url,source_token),source)
        target = RemoteAdapter(None,sid)
        worker_db = root/"cold-worker.db"
        worker = Worker(worker_db,"oguri",loop.wire,source_port,clock=clock,resume_targets={sid:target})
        service = WorkerService(worker,clock)
        identities = {platform_token:"platform/oguri",source_callback:"adapter/oguri/"+source,
                      cold_callback:"adapter/oguri/"+sid}
        worker_url = self.enterContext(running(service.listen(identities,("127.0.0.1",0),None)))
        platform = Client(worker_url,platform_token)
        inbox = Inbound(root/"cold-platform.db",platform,None,"app","trainer","chat",clock=clock)
        cards = Views(root/"cold-platform.db",platform,PlatformFixture(),"chat","小栗帽",clock)
        foreign = guid()
        roles = config(root,"cold-adapter-roles",{cold_token:"worker/oguri",foreign:"worker/foreign"})
        to_worker = config(root,"cold-to-worker",{"url":worker_url,"token":cold_callback})
        cert,key = root/"tls.crt",root/"tls.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",key],
                       capture_output=True,timeout=10,check=True)
        adapter_db = root/"cold-adapter.db"
        extra = ("--worker-id","oguri","--session",sid,"--worker-connection",to_worker,"--codex-binary",CODEX,
                 "--codex-profile",profile,"--cwd",project,"--resume-on-handoff")
        package = Path(__file__).resolve().parents[1]

        def event(mid,text):
            return inbox.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":mid,
                "message_type":"text","content":encode({"text":text})})

        calls = []
        def install_transport(url,*,lose):
            transport = target.client = Client(url,cold_token,cert)
            request = transport.request

            def observe(method,path,*args,**kwargs):
                if path=="host/bound/activate":
                    calls.append(args[0]["operation_id"])
                    if lose:
                        self.assertTrue(loop.hermes.workers()["items"][0]["admission_paused"])
                        self.assertEqual(worker.native_session_pin,source)
                    reply = request(method,path,*args,**kwargs)
                    if lose:
                        raise ConnectionError("isolated adapter activation reply lost after native load")
                    return reply
                return request(method,path,*args,**kwargs)

            transport.request = observe

        with serve(package,"codex-adapter",adapter_db,roles,cert,key,dict(os.environ),extra) as url:
            install_transport(url,lose=True)
            service.step()
            with self.assertRaisesRegex(Fault,"native_activation_unavailable"):
                Client(url,cold_token,cert).request("POST","host/activate",{"operation_id":guid()})
            other_session = guid()
            with self.assertRaisesRegex(Fault,"wrong_native_session"):
                RemoteAdapter(Client(url,cold_token,cert),other_session).activate(other_session,guid())
            with self.assertRaisesRegex(Fault,"adapter_identity_unconfirmed"):
                RemoteAdapter(Client(url,foreign,cert),sid).activate(sid,guid())
            with self.assertRaisesRegex(Fault,"native_session_cold"):
                target.deliver("before-cutover",{"kind":"chat","text":"Must not reach the native session"})
            menu = inbox.deliver(event("om_cold_catalog","/resume"))
            cards.poll()
            self.assertGreater(menu["output"]["seq"],0)
            self.assertEqual(target.session_info()["native_load_state"],"cold")
            self.assertFalse(calls)
            cmd = event("om_load_exact_thread","/resume "+sid)
            with self.assertRaisesRegex(Fault,"native_switch_host_unconfirmed"):
                inbox.deliver(cmd)
            self.assertEqual(len(calls),1)
            self.assertEqual(target.session_info()["native_load_state"],"loaded")
            self.assertEqual(worker.native_session_pin,source)
            self.assertTrue(loop.hermes.workers()["items"][0]["admission_paused"])
            self.assertEqual(self.provider.observed,[(True,0)])
            task_id = loop.hermes.create(guid(),"Cold session rollout","Return a real worker_report callback","oguri")["task_id"]
            self.assertIsNone(loop.hermes.inspect(task_id)["task"]["assignment_id"])
            service.step()
            cutover = inbox.deliver(cmd)
            self.assertEqual((cutover["binding_receipt"]["session"],worker.native_session_pin),(sid,sid))
            self.assertEqual(calls,[cutover["binding_receipt"]["handoff_id"]])
            self.assertFalse(loop.hermes.workers()["items"][0].get("admission_paused",False))
            service.step()
            clock.advance(10)
            until = time.monotonic()+10
            while time.monotonic()<until:
                service.step()
                state = loop.hermes.inspect(task_id)
                with closing(sqlite3.connect(adapter_db)) as db:
                    projected = db.execute("SELECT COUNT(*) FROM codex_display_turns WHERE sent=1").fetchone()[0]
                if state["task"]["state"]=="completed_reported" and projected==2 and not target.busy():
                    break
                time.sleep(.035)
            self.assertEqual(state["task"]["state"],"completed_reported")
            self.assertEqual(projected,2)
            self.assertEqual(loop.native.entries(),[])
            self.assertIsNone(worker.hold()["held_assignment_id"])
            public = [entry["event"] for entry in platform.request("GET","worker/display")["items"]]
            self.assertFalse(any(event["turn_id"]==seed["turn_id"] for event in public))
            self.assertEqual(sum(event["kind"]=="final" and event["session"]==sid for event in public),1)
            outcome, = loop.manager_wire.frames(0)
            self.assertEqual(outcome["payload"]["task_id"],task_id)
            report = state["last_report"]
            self.assertEqual(worker.assignment_session(report["assignment_id"]),sid)
            before_restart = self.provider.observed[:]

        with serve(package,"codex-adapter",adapter_db,roles,cert,key,dict(os.environ),extra) as url:
            install_transport(url,lose=False)
            self.assertEqual(target.session_info()["native_load_state"],"cold")
            clock.advance(5)
            service.step()
            self.assertEqual(service.status()["host_session"],sid)
            self.assertEqual(calls,[cutover["binding_receipt"]["handoff_id"]]*2)
            self.assertEqual(inbox.deliver(cmd),cutover)
            self.assertEqual(loop.hermes.inspect(task_id)["last_report"],report)
        self.assertEqual(self.provider.observed,before_restart)
        self.assertEqual(before_restart,[(True,0),(True,0),(True,1),(True,2)])
        with start_app_server(CODEX,profile,project) as review:
            review.request("thread/resume",{"threadId":sid,"cwd":str(project)})
            turns = AppServerHost(review,sid).thread(sid,include_turns=True)["turns"]
            self.assertEqual(len(turns),2)
            self.assertEqual(turns[0]["id"],seed["turn_id"])
            native_inputs = [item["clientId"] for item in turns[1]["items"] if item["type"]=="userMessage"]
            formal_delivery, = [frame["delivery_id"] for frame in loop.wire.frames(0) if frame["kind"]=="task.dispatch"]
            self.assertEqual(native_inputs,["mowulink-"+formal_delivery])
            tool_calls = [item for item in turns[1]["items"] if item["type"]=="dynamicToolCall"]
            self.assertEqual([item["tool"] for item in tool_calls],["worker_report"]*2)
        self.evidence = {"cold_native_session":sid,"existing_turn":seed["turn_id"],"handoff":cutover["binding_receipt"],
            "activation_requests":calls,"replayed_without_second_activation_before_process_restart":True,
            "new_native_turn":turns[1]["id"],"native_client_input":native_inputs[0],
            "native_tool_calls":[item["id"] for item in tool_calls],"seed_republished":False,"new_model_calls_on_restore":0,
            "task_id":task_id,"assignment_id":report["assignment_id"],"notification_id":outcome["delivery_id"],
            "scope":"Actual independent Codex App Server process and temporary TLS adapter; scripted model and Feishu fixture"}

    def test_native_cold_activation_checks_goal_and_recovers_a_missing_app_server_reply(self):
        root = Path(self.root).resolve()
        with isolated_app_server(root,self.origin) as seed:
            sid = seed.request("thread/start",{"cwd":str(root),"ephemeral":False})["thread"]["id"]
            original = seed.request("turn/start",{"threadId":sid,
                "input":[{"type":"text","text":"Seed the independent cold target","text_elements":[]}]})["turn"]["id"]
            until = time.monotonic()+12
            while seed.event(until)["method"]!="turn/completed":
                pass
        with isolated_app_server(root,self.origin) as rpc:
            host = AppServerHost(rpc,sid)
            objective = "Keep the isolated target goal active"
            self.assertEqual(host.thread(sid,include_turns=False)["status"]["type"],"notLoaded")
            rpc.request("thread/goal/set",{"threadId":sid,"origin":"user","status":"active","objective":objective})
            initialized,loads = [],[]
            activation = CodexActivation(host,root,root/"cold-proof.db",
                                         lambda:initialized.append(host.thread(sid,include_turns=True)["turns"]))
            operation_id = guid()
            with self.assertRaisesRegex(Fault,"native_cold_goal_active"):
                activation.activate(sid,operation_id)
            self.assertEqual(activation.state,"cold")
            self.assertEqual(host.thread(sid,include_turns=False)["status"]["type"],"notLoaded")
            self.assertEqual(host.goal_view(sid)["goal"]["objective"],objective)
            rpc.request("thread/goal/set",{"threadId":sid,"origin":"user","status":"paused","objective":objective})
            request = host.request

            def drop(method,params):
                result = request(method,params)
                if method=="thread/resume":
                    loads.append(params["threadId"])
                    raise Fault(503,"isolated_native_resume_reply_lost",True)
                return result

            host.request = drop
            with self.assertRaisesRegex(Fault,"isolated_native_resume_reply_lost"):
                activation.activate(sid,operation_id)
            self.assertEqual(activation.state,"cold")
            self.assertEqual(host.thread(sid,include_turns=False)["status"]["type"],"idle")
            self.assertEqual(initialized,[])
            expected = {"session":sid,"operation_id":operation_id,"state":"loaded"}
            self.assertEqual(activation.activate(sid,operation_id),expected)
            self.assertEqual(activation.activate(sid,operation_id),expected)
            self.assertEqual(loads,[sid])
            self.assertEqual([[turn["id"] for turn in history] for history in initialized],[[original]])
            self.assertEqual(host.goal_view(sid)["goal"]["phase"],"paused")
        self.assertEqual(len(self.provider.observed),1)
        self.evidence = {"session":sid,"operation_id":operation_id,"existing_turn":original,
                         "goal_refused_while_cold":True,"goal_after_restore":"paused",
                         "native_resume_calls":len(loads),"additional_model_calls":0}

    def test_thread_preparation_retains_native_identity_and_never_retries_an_uncertain_creation(self):
        root = Path(self.root).resolve()
        with isolated_app_server(root,self.origin,['model_reasoning_effort="high"']) as rpc:
            parent = rpc.request("thread/start",{"cwd":str(root),"ephemeral":False,
                                                  "dynamicTools":[REPORT_TOOL]})["thread"]["id"]
            seeded = rpc.request("turn/start",{"threadId":parent,"clientUserMessageId":"mowulink-prior-parent",
                "input":[{"type":"text","text":"Only the existing parent's input","text_elements":[]}]})["turn"]["id"]
            until = time.monotonic()+12
            while rpc.event(until)["method"]!="turn/completed":
                pass
            host = AppServerHost(rpc,parent)
            db = root/"prepared.db"
            prepare = CodexThreadPreparation(host,root,db)
            native = host.request
            allocated = []
            lose_name = True
            lose_creation = False

            def lose(method,params):
                nonlocal lose_name
                result = native(method,params)
                if method in ("thread/start","thread/fork"):
                    allocated.append((method,result["thread"]["id"]))
                    if lose_creation:
                        raise Fault(503,"isolated_native_reply_lost",True)
                if method=="thread/name/set" and lose_name:
                    lose_name = False
                    raise Fault(503,"isolated_name_reply_lost",True)
                return result

            host.request = lose
            new_id,fork_id,unknown = (guid() for _ in range(3))
            with self.assertRaisesRegex(Fault,"isolated_name_reply_lost"):
                prepare.prepare(parent,new_id,"new")
            with prepare.db.transaction() as ledger:
                saved = ledger.execute("SELECT state,native_session FROM codex_thread_preparations WHERE operation_id=?",
                                       (new_id,)).fetchone()
            self.assertEqual(tuple(saved),("received",allocated[0][1]))
            reopened = CodexThreadPreparation(host,root,db)
            new = reopened.prepare(parent,new_id,"new")
            self.assertEqual(new["session"],allocated[0][1])
            self.assertEqual(reopened.prepare(parent,new_id,"new"),new)
            with self.assertRaisesRegex(Fault,"native_preparation_conflict"):
                reopened.prepare(parent,new_id,"fork")
            fork = reopened.prepare(parent,fork_id,"fork")
            self.assertEqual(fork["parent_session"],parent)
            self.assertEqual(fork["session"],allocated[1][1])
            self.assertEqual(reopened.prepare(parent,fork_id,"fork"),fork)
            self.assertEqual(new["selection"],fork["selection"])
            self.assertEqual(new["selection"],{"model":"gpt-5.4","provider":"test_wire","effort":"high"})
            self.assertIsNone(new["parent_session"])
            child = AppServerHost(rpc,fork["session"])
            child.bind_fork_history(root/"child-baseline.db",child.thread(fork["session"],include_turns=False))
            history = child.thread(fork["session"],include_turns=True)["turns"]
            self.assertEqual([turn["id"] for turn in history],[seeded])
            self.assertIn("prior-parent",child.inherited_delivery_ids)
            self.assertEqual([turn["id"] for turn in host.thread(parent,include_turns=True)["turns"]],[seeded])
            empty = native("thread/read",{"threadId":new["session"],"includeTurns":False})["thread"]
            self.assertEqual(empty["name"],new["title"])
            lose_creation = True
            for _ in range(3):
                with self.assertRaisesRegex(Fault,"native_creation_unconfirmed"):
                    reopened.prepare(parent,unknown,"new")
            self.assertEqual([method for method,_sid in allocated],["thread/start","thread/fork","thread/start"])
            self.assertEqual(len({sid for _method,sid in allocated}),3)
            self.assertEqual(len(self.provider.observed),1)

        with isolated_app_server(root,self.origin) as rpc:
            host = AppServerHost(rpc,parent)
            after_restart = CodexThreadPreparation(host,root,db)
            self.assertEqual(after_restart.prepare(parent,new_id,"new"),new)
            self.assertEqual(after_restart.prepare(parent,fork_id,"fork"),fork)
            with self.assertRaisesRegex(Fault,"native_creation_unconfirmed"):
                after_restart.prepare(parent,unknown,"new")
            observed = host.request("thread/read",{"threadId":new["session"],"includeTurns":False})["thread"]
            self.assertEqual((observed["id"],observed["status"]["type"]),(new["session"],"notLoaded"))
        self.assertEqual(len(self.provider.observed),1)
        self.evidence = {"parent":parent,"parent_turn":seeded,"new":new,"fork":fork,"native_creations":allocated,
            "unknown_receipt_operation":unknown,"unknown_retries":4,"automatic_model_calls":0,
            "scope":"Installed independent Codex App Server, loopback model fixture and temporary profile"}

    @unittest.skipUnless(shutil.which("openssl"),"isolated TLS certificate generator required")
    def test_prepare_sessions_cli_uses_authenticated_pinned_adapter_and_replays_after_restart(self):
        root = Path(self.root).resolve()
        profile,project = root/"prepare-profile",root/"prepare-workspace"
        profile.mkdir(mode=0o700)
        project.mkdir(mode=0o700)
        self.local_profile(profile)
        package = Path(__file__).resolve().parents[1]
        provisioned = subprocess.run(self.provision_command(profile,project),cwd=package,capture_output=True,text=True,
                                     timeout=30,check=True)
        parent = json.loads(provisioned.stdout)["thread_id"]
        self.assertEqual(len(self.provider.observed),1)
        worker,foreign = guid(),guid()
        roles = config(root,"prepare-adapter-roles.json",{worker:"worker/oguri",foreign:"worker/other"})
        # No model or public display event is expected during preparation.
        callback = config(root,"prepare-to-unused-test-worker.json",{"url":"http://127.0.0.1:1","token":guid()})
        cert,key = root/"prepare.crt",root/"prepare.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",key],
                       capture_output=True,timeout=10,check=True)
        flags = ("--worker-id","oguri","--session",parent,"--worker-connection",callback,"--codex-binary",CODEX,
                 "--codex-profile",profile,"--cwd",project)
        db = root/"preparation-adapter.db"
        op = guid()
        with serve(package,"codex-adapter",db,roles,cert,key,dict(os.environ),flags) as url:
            initial = RemoteAdapter(Client(url,worker,cert),parent)
            self.assertNotIn("session_preparation",initial.session_info())
            with self.assertRaisesRegex(Fault,"native_preparation_unavailable"):
                initial.prepare_thread(parent,op,"new")
        enabled = (*flags,"--prepare-sessions")
        with serve(package,"codex-adapter",db,roles,cert,key,dict(os.environ),enabled) as url:
            client = Client(url,worker,cert)
            remote = RemoteAdapter(client,parent)
            self.assertTrue(remote.session_info()["session_preparation"])
            with self.assertRaisesRegex(Fault,"native_preparation_unavailable"):
                client.request("POST","host/prepare-thread",{"operation_id":op,"kind":"new"})
            with self.assertRaisesRegex(Fault,"adapter_identity_unconfirmed"):
                RemoteAdapter(Client(url,foreign,cert),parent).prepare_thread(parent,op,"new")
            wrong = guid()
            with self.assertRaisesRegex(Fault,"wrong_native_session"):
                RemoteAdapter(client,wrong).prepare_thread(wrong,op,"new")
            with self.assertRaisesRegex(Fault,"invalid_native_preparation"):
                remote.prepare_thread(parent,op,"delete")
            new = remote.prepare_thread(parent,op,"new")
            self.assertEqual(remote.prepare_thread(parent,op,"new"),new)
            self.assertEqual(remote.session_info()["session"],parent)
        with serve(package,"codex-adapter",db,roles,cert,key,dict(os.environ),enabled) as url:
            remote = RemoteAdapter(Client(url,worker,cert),parent)
            self.assertEqual(remote.prepare_thread(parent,op,"new"),new)
            self.assertEqual(remote.session_info()["session"],parent)
        self.assertEqual(len(self.provider.observed),1)
        with closing(sqlite3.connect(db)) as ledger:
            self.assertEqual(ledger.execute("SELECT state,native_session FROM codex_thread_preparations").fetchall(),
                             [("prepared",new["session"])])

        # The prepared native ID can be enrolled as a separate test adapter with its own callback identity.
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock))
        initial_session,child = loop.native.session,new["session"]
        source_token,platform_token,cold_callback,warm_callback = (guid() for _ in range(4))
        initial_url = self.enterContext(running(serve_adapter(loop.adapter,"oguri",{source_token:"worker/oguri"},
                                                             ("127.0.0.1",0),None)))
        initial = RemoteAdapter(Client(initial_url,source_token),initial_session)
        target = RemoteAdapter(None,child)
        rebuilt = Worker(root/"enrolled-worker.db","oguri",loop.wire,initial,clock=clock,resume_targets={child:target})
        gateway = WorkerService(rebuilt,clock)
        gateway_url = self.enterContext(running(gateway.listen({platform_token:"platform/oguri",
            cold_callback:"adapter/oguri/"+child,warm_callback:"adapter/oguri/"+initial_session},("127.0.0.1",0),None)))
        incoming = Client(gateway_url,platform_token)
        enrolled = config(root,"enrolled-callback.json",{"url":gateway_url,"token":cold_callback})
        child_flags = ("--worker-id","oguri","--session",child,"--worker-connection",enrolled,"--codex-binary",CODEX,
                       "--codex-profile",profile,"--cwd",project,"--resume-on-handoff")
        with serve(package,"codex-adapter",root/"enrolled-adapter.db",roles,cert,key,dict(os.environ),child_flags) as url:
            target.client = Client(url,worker,cert)
            self.assertEqual(target.session_info()["native_load_state"],"cold")
            gateway.step()
            select = {"message_id":"om_choose_prepared_test_child","text":"/resume "+child}
            binding = incoming.request("POST","worker/command",select)
            self.assertEqual(binding["receipt"]["binding_receipt"]["session"],child)
            self.assertEqual(incoming.request("POST","worker/command",select),binding)
            self.assertEqual(len(self.provider.observed),1)
            tid = loop.hermes.create(guid(),"Adopted test child","Return one native result","oguri")["task_id"]
            gateway.step()
            clock.advance(10)
            deadline = time.monotonic()+10
            while time.monotonic()<deadline:
                gateway.step()
                order = loop.hermes.inspect(tid)
                if order["task"]["state"]=="completed_reported" and not target.busy():
                    break
                time.sleep(.04)
            self.assertEqual(order["task"]["state"],"completed_reported")
            aid = order["last_report"]["assignment_id"]
            self.assertEqual(rebuilt.assignment_session(aid),child)
            self.assertIsNone(rebuilt.hold()["held_assignment_id"])
            notice, = loop.manager_wire.frames(0)
            self.assertEqual(notice["payload"]["task_id"],tid)
        with start_app_server(CODEX,profile,project) as rpc:
            rpc.request("thread/resume",{"threadId":child,"cwd":str(project)})
            native = AppServerHost(rpc,child).thread(child,include_turns=True)["turns"]
            turn, = native
            calls = [item["id"] for item in turn["items"] if item["type"]=="dynamicToolCall" and item["tool"]=="worker_report"]
            self.assertEqual(len(calls),2)
        self.evidence = {"prepared":new,"adoption":binding["receipt"]["binding_receipt"],"native_turn":turn["id"],
            "native_worker_reports":calls,"task_id":tid,"assignment_id":aid,"notification_id":notice["delivery_id"],
            "scope":"Actual isolated App Server and TLS adapters. An explicit second adapter enrolls the prepared thread. Model is a fixture."}

    @unittest.skipUnless(shutil.which("openssl"),"isolated TLS certificate generator required")
    def test_managed_native_commands_bind_without_manual_enrollment_and_restore(self):
        root = Path(self.root).resolve()
        profile,project,registry = (root/name for name in ("managed-profile","managed-project","managed-sessions"))
        for directory in (profile,project,registry):
            directory.mkdir(mode=0o700)
        self.local_profile(profile)
        package = Path(__file__).resolve().parents[1]
        provisioned = subprocess.run(self.provision_command(profile,project),cwd=package,capture_output=True,
                                     text=True,timeout=30,check=True)
        parent = json.loads(provisioned.stdout)["thread_id"]
        tls_token,platform_token,parent_token = (guid() for _ in range(3))
        roles = config(root,"managed-adapter-roles.json",{tls_token:"worker/oguri"})
        worker_roles = {platform_token:"platform/oguri",parent_token:"adapter/oguri/"+parent}
        cert,key = root/"managed.crt",root/"managed.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",key],
                       capture_output=True,timeout=10,check=True)
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock))
        parent_port = RemoteAdapter(None,parent)
        worker_db,host_db = root/"managed-worker.db",root/"managed-adapter.db"
        callback_key = os.urandom(32)

        def rebuilt_worker():
            return Worker(worker_db,"oguri",loop.wire,parent_port,clock=clock,session_callback_key=callback_key)

        def command(mid,text,**extra):
            return client.request("POST","worker/command",{"message_id":"om_managed_"+mid,"text":text,**extra})["receipt"]

        def platform_input(inbox,suffix,message):
            mid = "om_managed_"+suffix
            inbox.record("app","user","trainer",{"message_id":mid,"chat_id":"test-chat","chat_type":"p2p",
                "message_type":"text","content":encode({"text":message})})
            deadline = time.monotonic()+10
            while True:
                try:
                    return inbox.deliver(mid)
                except Fault as error:
                    if (error.code not in ("native_switch_callbacks_pending","host_receipt_unconfirmed")
                            or time.monotonic()>=deadline):
                        raise
                    time.sleep(.04)

        def formal(label,gateway):
            result = loop.hermes.create(guid(),label,"Return one native result","oguri")
            task = result["task_id"]
            gateway.step()
            clock.advance(10)
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                gateway.step()
                detail = loop.hermes.inspect(task)
                adapter = gateway.worker.adapter
                if detail["task"]["state"]=="completed_reported" and not adapter.busy():
                    break
                time.sleep(.04)
            self.assertEqual(detail["task"]["state"],"completed_reported")
            assignment = detail["last_report"]["assignment_id"]
            self.assertEqual(gateway.worker.assignment_session(assignment),gateway.worker.native_session_pin)
            self.assertIsNone(gateway.worker.hold()["held_assignment_id"])
            with gateway.worker.db.transaction() as db:
                delivery = db.execute("SELECT delivery_id FROM jobs WHERE assignment_id=?",(assignment,)).fetchone()[0]
            return {"task_id":task,"assignment_id":assignment,"delivery_id":delivery}

        gateway = WorkerService(rebuilt_worker(),clock)
        old_enroll = parent_port.enroll_thread
        enrollment = []

        def lose_first_enrollment(source,operation,session,token):
            receipt = old_enroll(source,operation,session,token)
            enrollment.append(receipt)
            if len(enrollment)==1:
                raise Fault(503,"adapter_connection_unconfirmed",True)
            return receipt

        parent_port.enroll_thread = lose_first_enrollment
        with running(gateway.listen(worker_roles,("127.0.0.1",0),None)) as gateway_url:
            client = Client(gateway_url,platform_token)
            feishu_db = root/"managed-feishu.db"
            inbox = Inbound(feishu_db,client,None,"app","trainer","test-chat",clock)
            parent_callback = Client(gateway_url,parent_token)
            callback = config(root,"managed-callback.json",{"url":gateway_url,"token":parent_token})
            flags = ("--worker-id","oguri","--session",parent,"--worker-connection",callback,"--codex-binary",CODEX,
                     "--codex-profile",profile,"--cwd",project,"--prepare-sessions","--managed-sessions-dir",registry)
            with serve(package,"codex-adapter",host_db,roles,cert,key,dict(os.environ),flags) as url:
                parent_port.client = Client(url,tls_token,cert)
                gateway.step()
                self.assertTrue(gateway.status()["native_capabilities"]["managed_session_enrollment"])
                with self.assertRaisesRegex(Fault,"wrong_native_session"):
                    parent_port.for_session(guid()).session_info()
                with self.assertRaisesRegex(Fault,"native_enrollment_not_prepared"):
                    old_enroll(parent,guid(),guid(),"a"*64)
                with self.assertRaisesRegex(Fault,"adapter_connection_unconfirmed"):
                    platform_input(inbox,"new","/new")
                self.assertEqual(gateway.status()["handoff"]["state"],"frozen")
                self.assertEqual(gateway.worker.native_session_pin,parent)
                self.assertEqual(len(self.provider.observed),1)
            # Restart both the adapter and Worker after child enrollment but before the binding was recorded.
        endpoint = ("127.0.0.1",int(gateway_url.rsplit(":",1)[1]))
        gateway = WorkerService(rebuilt_worker(),clock)
        with running(gateway.listen(worker_roles,endpoint,None)) as reconnected:
            self.assertEqual(reconnected,gateway_url)
            with serve(package,"codex-adapter",host_db,roles,cert,key,dict(os.environ),flags) as url:
                parent_port.client = Client(url,tls_token,cert)
                gateway.step()
                created = platform_input(inbox,"new","/new")
                self.assertEqual(created,command("new","/new"))
                child = created["binding_receipt"]["session"]
                self.assertEqual(enrollment, [{"operation_id":created["binding_receipt"]["handoff_id"],
                                               "session":child,"state":"enrolled"}]*2)
                self.assertNotEqual(child,parent)
                with self.assertRaisesRegex(Fault,"native_enrollment_conflict"):
                    old_enroll(parent,enrollment[0]["operation_id"],child,"b"*64)
                with self.assertRaisesRegex(Fault,"native_callback_inactive"):
                    parent_callback.request("POST","worker/native-tool",{"session":child})
                first = formal("New session result",gateway)
                forked = platform_input(inbox,"fork","/fork")
                self.assertEqual(forked,command("fork","/fork"))
                fork = forked["binding_receipt"]["session"]
                self.assertEqual(forked["binding_receipt"]["previous_session"],child)
                self.assertNotIn(fork,(parent,child))
                second = formal("Forked session result",gateway)
                discarded = platform_input(inbox,"clear","/clear")
                fresh = discarded["binding_receipt"]["session"]
                self.assertNotIn(fresh,(parent,child,fork))
                self.assertEqual(discarded,command("clear","/clear"))
                self.assertEqual(gateway.worker.native_session_pin,fresh)
                self.assertEqual(command("back","/resume "+fork)["binding_receipt"]["session"],fork)
                notices = [frame for frame in loop.manager_wire.frames(0) if frame["payload"].get("task_id")]
                self.assertEqual([frame["payload"]["task_id"] for frame in notices],[first["task_id"],second["task_id"]])
        with self.assertRaisesRegex(Fault,"managed_session_key_changed"):
            Worker(worker_db,"oguri",loop.wire,parent_port,clock=clock,session_callback_key=os.urandom(32))
        with serve(package,"codex-adapter",host_db,roles,cert,key,dict(os.environ),flags) as url:
            parent_port.client = Client(url,tls_token,cert)
            worker = rebuilt_worker()
            self.assertEqual(worker.native_session_pin,fork)
            self.assertEqual(parent_port.session_info()["native_load_state"],"cold")
            self.assertEqual(worker.adapter.session_info()["native_load_state"],"cold")
            gateway = WorkerService(worker,clock)
            with running(gateway.listen(worker_roles,endpoint,None)):
                gateway.step()
                self.assertIs(gateway.status()["adapter_connected"],True)
                self.assertEqual(gateway.status()["host_session"],fork)
                self.assertEqual(parent_port.session_info()["native_load_state"],"cold")
                self.assertEqual(command("fork","/fork"),forked)
                private = platform_input(inbox,"private","Managed private chat after reconnect.")
                self.assertEqual(private["session"],fork)
                self.assertIsNone(worker.hold()["held_assignment_id"])
                deadline = time.monotonic()+10
                while time.monotonic()<deadline and worker.adapter.busy():
                    time.sleep(.04)
                self.assertFalse(worker.adapter.busy())
        with start_app_server(CODEX,profile,project) as rpc:
            def native_story(sid):
                rpc.request("thread/resume",{"threadId":sid,"cwd":str(project)})
                thread = AppServerHost(rpc,sid).thread(sid,include_turns=True)
                inputs = [item["clientId"] for turn in thread["turns"] for item in turn["items"]
                          if item["type"]=="userMessage"]
                return thread,inputs
            parent_thread,parent_inputs = native_story(parent)
            child_thread,child_inputs = native_story(child)
            fork_thread,fork_inputs = native_story(fork)
            self.assertEqual(len(parent_inputs),1)
            self.assertIsNone(child_thread["forkedFromId"])
            self.assertEqual(child_inputs,["mowulink-"+first["delivery_id"]])
            self.assertEqual(fork_thread["forkedFromId"],child)
            self.assertEqual(fork_inputs,child_inputs+["mowulink-"+second["delivery_id"],"mowulink-"+private["delivery_id"]])
            for result,turn in ((first,child_thread["turns"][-1]),(second,fork_thread["turns"][-2])):
                result["native_turn"] = turn["id"]
                result["native_calls"] = [item["id"] for item in turn["items"]
                    if item["type"]=="dynamicToolCall" and item["tool"]=="worker_report"]
                self.assertEqual(len(result["native_calls"]),2)
            third_thread = rpc.request("thread/read",{"threadId":fresh,"includeTurns":False})["thread"]
            self.assertIsNone(third_thread["forkedFromId"])
        self.assertEqual(len(self.provider.observed),8)
        self.evidence = {"original_session":parent_thread["id"],"new_binding":created["binding_receipt"],
            "fork_binding":forked["binding_receipt"],"clear_binding":discarded["binding_receipt"],
            "formals":[first,second],"manager_notifications":[notice["delivery_id"] for notice in notices],
            "restart_restored_session":fork,"enrollment_receipts":len(enrollment),
            "private_input":private["native_id"],"private_turn":fork_thread["turns"][-1]["id"],
            "scope":"Installed isolated Codex App Server and actual Router/Worker. Platform command fixture and local scripted model."}
        destination = os.environ.get("MOWULINK_MANAGED_SESSION_EVIDENCE")
        if destination:
            Path(destination).write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    @unittest.skipUnless(shutil.which("openssl"),"isolated TLS certificate generator required")
    def test_managed_bundle_restores_pending_enrollment_and_rolls_back_a_held_assignment(self):
        root = Path(self.root).resolve()
        profile,project,registry,source = (root/name for name in ("profile","workspace","sessions","source"))
        for directory in (profile,project,registry,source):
            directory.mkdir(mode=0o700)
        self.local_profile(profile)
        package = Path(__file__).resolve().parents[1]
        original_package = os.environ.get("MOWULINK_MANAGED_ROLLBACK_PACKAGE")
        provisioned = subprocess.run(self.provision_command(profile,project),cwd=package,capture_output=True,
                                     text=True,timeout=30,check=True)
        parent = json.loads(provisioned.stdout)["thread_id"]
        cert,key = root/"adapter.crt",root/"adapter.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",key],
                       capture_output=True,timeout=10,check=True)
        rt,mgr,worker_auth,platform,callback = (guid() for _ in range(5))
        roles = config(root,"adapter-roles.json",{worker_auth:"worker/oguri"})
        worker_roles = {platform:"platform/oguri",callback:"adapter/oguri/"+parent}
        callback_key = os.urandom(32)
        keys = {"oguri":config(root,"callback-key.json",{"key":callback_key.hex()})}
        labels = ("router:main","worker:oguri","agent:oguri","hermes:main","trigger:source")
        paths = {label:source/filename(label) for label in labels}
        Trigger(paths["trigger:source"],"snapshot-fixture")
        clock = Clock()
        manager_native = FixtureHost(root/"manager-native.db","isolated-manager")
        manager_host = HostAdapter(root/"manager-host.db",manager_native,manager_native.session)
        create = {"message_id":"om_snapshot_native_new","text":"/new"}
        enrollment = []

        @contextmanager
        def station(state,native_dir,stage,*,lose_reply=False,code=package):
            orders = Orders(state["router:main"],clock,1)
            with running(serve_loopback(orders,{rt:"worker/oguri",mgr:"hermes"})) as router_url:
                manager = HermesAdapter(state["hermes:main"],Client(router_url,mgr),manager_host,clock)
                port = RemoteAdapter(None,parent)
                worker = Worker(state["worker:oguri"],"oguri",Client(router_url,rt),port,clock=clock,
                                session_callback_key=callback_key)
                gateway = WorkerService(worker,clock)
                if lose_reply:
                    original = port.enroll_thread
                    def lose_after_write(*args):
                        enrollment.append(original(*args))
                        raise Fault(503,"adapter_connection_unconfirmed",True)
                    port.enroll_thread = lose_after_write
                with running(gateway.listen(worker_roles,("127.0.0.1",0),None)) as url:
                    receiver = config(root,"adapter-to-worker-"+stage+".json",{"url":url,"token":callback})
                    flags = ("--worker-id","oguri","--session",parent,"--worker-connection",receiver,"--codex-binary",CODEX,
                             "--codex-profile",profile,"--cwd",project,"--prepare-sessions","--managed-sessions-dir",native_dir)
                    with serve(code,"codex-adapter",state["agent:oguri"],roles,cert,key,dict(os.environ),flags) as address:
                        connection = Client(address,worker_auth,cert)
                        for target in worker.resume_targets.values():
                            target.client = connection
                        gateway.step()
                        yield gateway,manager,Client(url,platform)

        def wait(gateway,predicate):
            deadline = time.monotonic()+12
            while True:
                gateway.step()
                value = predicate()
                if value and not gateway.worker.adapter.busy():
                    return value
                if time.monotonic()>=deadline:
                    self.fail("timed out waiting for isolated state: "+str(gateway.status()))
                time.sleep(.04)

        with station(paths,registry,"source",lose_reply=True) as (gateway,_manager,incoming):
            with self.assertRaisesRegex(Fault,"adapter_connection_unconfirmed"):
                incoming.request("POST","worker/command",create)
            self.assertEqual(gateway.status()["handoff"]["state"],"frozen")
        recorded, = enrollment
        child = recorded["session"]
        pending = root/"pending-snapshot"
        sha = capture(paths,"a"*40,pending,registries={"oguri":registry})["manifest_sha256"]
        audit, = audit_handoff(pending,sha)["workers"]
        self.assertEqual(audit["native_session_pending"]["native_resume_operations"],1)
        self.assertEqual(audit["native_session_pending"]["router_admission_handoffs"],1)
        trial_dir = root/"restored-trial"
        trial = restore(pending,sha,trial_dir,managed_keys=keys)
        trial_registry = trial_dir/"native--oguri"
        with station(trial,trial_registry,"restored-trial") as (gateway,manager,incoming):
            self.assertEqual(gateway.worker.native_session_pin,child)
            receipt = incoming.request("POST","worker/command",create)["receipt"]["binding_receipt"]
            self.assertEqual(receipt,{"handoff_id":recorded["operation_id"],"previous_session":parent,
                                       "session":child,"state":"bound"})
            self.assertEqual(len(self.provider.observed),1)
            tid = manager.create(guid(),"Retain held order",KEEP_FORMAL_HELD,"oguri")["task_id"]
            gateway.step()
            clock.advance(10)
            def received_dispatch():
                with gateway.worker.db.transaction() as db:
                    row = db.execute("""SELECT frame FROM inbox i JOIN jobs j USING(delivery_id)
                                        WHERE j.task_id=? AND j.host_state='received'""",(tid,)).fetchone()
                return json.loads(row[0]) if row else None
            frame = wait(gateway,received_dispatch)
            aid = frame["payload"]["assignment_id"]
            original = gateway.worker.adapter.lookup_delivery(frame["delivery_id"],{"kind":"task",**frame["payload"]},
                                                               session=child)
            self.assertEqual(gateway.worker.hold(),{"held_task_id":tid,"held_assignment_id":aid})
            self.assertEqual(manager.inspect(tid)["task"]["state"],"working")
            self.assertFalse(any(n["payload"].get("task_id")==tid for n in manager.router.frames(0)))
        held_snapshot = root/"held-snapshot"
        held_sha = capture(trial,"b"*40,held_snapshot,registries={"oguri":trial_registry})["manifest_sha256"]
        held_audit, = audit_handoff(held_snapshot,held_sha)["workers"]
        self.assertEqual(held_audit["issues"],[])
        self.assertEqual(held_audit["native_session_pending"]["held_assignments"],1)
        untouched_dir = root/"untouched-candidate"
        untouched = restore(held_snapshot,held_sha,untouched_dir,managed_keys=keys)
        back_dir = root/"rollback"
        back = rollback(held_snapshot,held_sha,untouched,back_dir,
                        registries={"oguri":untouched_dir/"native--oguri"},managed_keys=keys)
        back_registry = back_dir/"native--oguri"
        with station(back,back_registry,"rollback",code=Path(original_package) if original_package else package) as (gateway,manager,incoming):
            self.assertEqual(gateway.worker.native_session_pin,child)
            self.assertEqual(gateway.worker.hold(),{"held_task_id":tid,"held_assignment_id":aid})
            self.assertEqual(incoming.request("POST","worker/command",create)["receipt"]["binding_receipt"],receipt)
            self.assertTrue(gateway.worker.process(frame))
            self.assertEqual(gateway.worker.adapter.lookup_delivery(frame["delivery_id"],{"kind":"task",**frame["payload"]},
                                                                    session=child),original)
            self.assertEqual(len(self.provider.observed),2)
            task = manager.inspect(tid)["task"]
            manager.update(guid(),tid,task["revision"],{"instructions":"Now return an isolated result."})
            def result_if_ready():
                detail = manager.inspect(tid)
                return detail if detail["task"]["state"]=="completed_reported" else None
            final = wait(gateway,result_if_ready)
            self.assertEqual(final["last_report"]["assignment_id"],aid)
            self.assertIsNone(gateway.worker.hold()["held_assignment_id"])
            notice, = [n for n in manager.router.frames(0) if n["payload"].get("task_id")==tid]
        refused = root/"refused-after-effect"
        with self.assertRaisesRegex(ValueError,"trial state changed"):
            rollback(held_snapshot,held_sha,back,refused,registries={"oguri":back_registry},managed_keys=keys)
        self.assertFalse(refused.exists())
        self.assertEqual(verify(held_snapshot,held_sha)["source_revision"],"b"*40)
        with start_app_server(CODEX,profile,project) as rpc:
            rpc.request("thread/resume",{"threadId":child,"cwd":str(project)})
            holds,reported = AppServerHost(rpc,child).thread(child,include_turns=True)["turns"]
        self.assertEqual(holds["id"],original["turn_id"])
        inputs = [item["clientId"] for item in holds["items"] if item["type"]=="userMessage"]
        self.assertEqual(inputs,["mowulink-"+frame["delivery_id"]])
        calls = [item for item in reported["items"] if item["type"]=="dynamicToolCall" and item["tool"]=="worker_report"]
        self.assertEqual(len(calls),2)
        self.assertTrue(all(item["success"] for item in calls))
        self.assertEqual(len(self.provider.observed),5)
        self.evidence = {"pending_snapshot_manifest_sha256":sha,"held_snapshot_manifest_sha256":held_sha,
            "recovered_enrollment":receipt,"task_id":tid,"assignment_id":aid,"original_native_receipt":original,
            "held_audit_issues":held_audit["issues"],"report_turn_id":reported["id"],"native_tool_calls":[c["id"] for c in calls],
            "manager_notice_id":notice["delivery_id"],"rollback_after_trial_effect_refused":not refused.exists(),
            "scope":"Actual isolated Codex App Server and Router/Worker. Same native profile, scripted model, no real Feishu.",
            "previous_adapter_package_used":bool(original_package),
            "previous_adapter_revision":os.environ.get("MOWULINK_MANAGED_ROLLBACK_REVISION") if original_package else None}
        output = os.environ.get("MOWULINK_MANAGED_BUNDLE_EVIDENCE")
        if output:
            Path(output).write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_native_app_server_reconnect_recovers_input_before_w04_without_new_model_call(self):
        with isolated_app_server(self.root,self.origin) as rpc:
            loop,host,frame,original,pending_call = self.pending_native_task(rpc)
            # Disconnect this isolated App Server before answering its pending tool request.
        self.assertIsNotNone(rpc.process.poll())
        sid = host.thread_id
        body = {"kind":"task",**frame["payload"]}
        did,tid = frame["delivery_id"],body["task_id"]
        with loop.adapter.db.transaction() as db:
            db.execute("UPDATE intents SET receipt=NULL,native_id=NULL WHERE delivery_id=?",(did,))
        with isolated_app_server(self.root,self.origin) as reopened:
            result = reopened.request("thread/resume",{"threadId":sid,"cwd":self.root})
            self.assertEqual(result["thread"]["id"],sid)
            host = AppServerHost(reopened,sid)
            loop.adapter.host = host
            self.assertTrue(loop.worker.process(frame))
            recovered = loop.adapter.lookup_delivery(did,body,session=sid)
            self.assertEqual(recovered,original)
            thread = host.thread(sid,include_turns=True)
            inputs = [item["id"] for turn in thread["turns"] for item in turn["items"] if item["type"]=="userMessage"]
            self.assertEqual(inputs,[original["native_id"]])
        self.assertEqual(self.provider.observed,[(True,0)])
        self.assertEqual(loop.worker.hold(),{"held_task_id":tid,"held_assignment_id":body["assignment_id"]})
        task = loop.hermes.inspect(tid)
        self.assertEqual(task["task"]["state"],"working")
        self.assertIsNone(task["last_report"])
        self.assertEqual(loop.manager_wire.frames(0),[])
        self.evidence = {"thread_id":sid,"task_id":tid,"assignment_id":body["assignment_id"],"original":original,
                         "recovered":recovered,"native_input_count":len(inputs),"router_state":task["task"]["state"],
                         "pending_native_call_id":pending_call["callId"],"model_requests":len(self.provider.observed),
                         "original_process_exited":rpc.process.returncode is not None,
                         "boundary":"Native App Server process restart with local scripted provider; no account or Desktop session"}

    def test_rejected_first_native_history_read_keeps_original_assignment_lookup_only(self):
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        original = rpc.request
        native_starts = []
        rejections = []
        def request(method,params):
            if method=="thread/read" and params.get("includeTurns") and native_starts and not rejections:
                rejections.append(params["threadId"])
                raise NativeRpcRejected("isolated initial history rejection")
            result = original(method,params)
            if method=="turn/start":
                native_starts.append(result["turn"]["id"])
            return result
        rpc.request = request
        loop,host,frame,proof,_call = self.pending_native_task(rpc)
        self.assertEqual(rejections,[host.thread_id])
        self.assertEqual(native_starts,[proof["turn_id"]])
        payload = {"kind":"task",**frame["payload"]}
        self.assertEqual(loop.adapter.lookup_delivery(frame["delivery_id"],payload,session=host.thread_id),proof)
        self.assertTrue(loop.worker.process(frame))
        self.assertEqual(len(native_starts),1)
        self.assertEqual(loop.hermes.inspect(payload["task_id"])["task"]["state"],"working")
        self.assertFalse(loop.manager_wire.frames(0))

    def test_native_app_server_interrupt_proves_exact_turn_without_releasing_assignment(self):
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        loop,host,frame,original,_pending_call = self.pending_native_task(rpc)
        sid,turn = original["session"],original["turn_id"]
        tid,aid = frame["payload"]["task_id"],frame["payload"]["assignment_id"]
        working = loop.hermes.inspect(tid)["task"]
        cancel = loop.hermes.act(guid(),tid,working["revision"],"cancel",reason="Isolated native stop proof")
        self.assertEqual(cancel["delivery_state"],"pending")
        self.assertTrue(host.busy(sid))
        with self.assertRaisesRegex(Fault,"native_turn_unconfirmed"):
            host.stop_turn(sid,dict(original,native_id=guid()),start=True)
        with self.assertRaisesRegex(RuntimeError,"expected active turn id"):
            rpc.request("turn/interrupt",{"threadId":sid,"turnId":guid()})
        self.assertTrue(host.busy(sid))
        self.assertIsNone(host.stop_turn(sid,original,start=False))
        proof = host.stop_turn(sid,original,start=True)
        until = time.monotonic()+12
        while True:
            event = rpc.event(until)
            if event.get("method")=="turn/completed":
                self.assertEqual(event["params"]["turn"]["id"],turn)
                self.assertEqual(event["params"]["turn"]["status"],"interrupted")
                break
        observed = host.stop_turn(sid,original,start=False)
        self.assertEqual(observed,{"session":sid,"turn_id":turn,"native_id":original["native_id"],"status":"interrupted"})
        if proof:
            self.assertEqual(proof,observed)
        self.assertEqual(host.stop_turn(sid,original,start=True),observed)
        self.assertFalse(host.busy(sid))
        self.assertEqual(loop.worker.hold(),{"held_task_id":tid,"held_assignment_id":aid})
        state = loop.hermes.inspect(tid)
        self.assertEqual(state["task"]["state"],"working")
        self.assertEqual([row["assignment_id"] for row in state["pending_cleanup"]],[aid])
        self.assertIsNone(state["last_report"])
        self.assertEqual(loop.manager_wire.frames(0),[])
        self.evidence = {"session":sid,"task_id":tid,"assignment_id":aid,"original_turn":original,
                         "native_stop":observed,"holder_after":state["task"]["holder"],
                         "state_after":state["task"]["state"],"wrong_turn_rejected":True,
                         "cleanup_pending":state["pending_cleanup"],
                         "boundary":"Native App Server, one scripted local model turn. Only native interruption is confirmed."}

    def test_platform_stop_interrupts_only_the_proven_private_app_server_turn(self):
        self.provider.private_admissions = Queue()
        self.provider.release_private = Event()
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        self.addCleanup(self.provider.release_private.set)
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False,"dynamicTools":[REPORT_TOOL]})["thread"]["id"]
        host = AppServerHost(rpc,sid)
        host.session = sid
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:host))
        service = WorkerService(loop.worker,clock)
        token = guid()
        url = self.enterContext(running(service.listen({token:"platform/oguri"},("127.0.0.1",0),None)))
        platform = Client(url,token)
        service.step()

        def command(mid):
            receipt = platform.request("POST","worker/command",{"message_id":mid,"text":"/stop"})["receipt"]
            card = next(row["event"]["text"] for row in platform.request("GET","worker/display")["items"]
                        if row["seq"]==receipt["output"]["seq"])
            return receipt,card

        def open_private(marker):
            text = "Hold bound private "+marker
            mid = guid()
            loop.worker.chat(mid,text)
            self.assertEqual(self.provider.private_admissions.get(timeout=8),text)
            return loop.worker.chat(mid,text)

        self.assertIn("已空闲",command("stop_idle")[1])
        first = open_private("one")
        saved,card = command("stop_first")
        self.assertIn("回合已停止",card)
        self.assertEqual(saved["stop_receipt"],{"session":sid,"turn_id":first["turn_id"],"native_input_id":first["native_id"]})
        self.assertEqual(host.thread(sid,include_turns=True)["turns"][-1]["status"],"interrupted")

        second = open_private("two")
        original_stop = loop.adapter.stop_private
        def lose_stop_ack(session):
            self.assertEqual(original_stop(session)["native_input_id"],second["native_id"])
            raise Fault(503,"isolated_stop_ack_lost",True)
        loop.adapter.stop_private = lose_stop_ack
        pending,card = command("stop_lost")
        loop.adapter.stop_private = original_stop
        self.assertIn("待核",card)
        self.assertNotIn("stop_receipt",pending)

        third = open_private("three")
        self.assertEqual(command("stop_first")[0],saved)
        self.assertEqual(command("stop_lost")[0],pending)
        self.assertTrue(host.busy(sid))
        self.assertEqual(command("stop_third")[0]["stop_receipt"]["turn_id"],third["turn_id"])
        private_turns = host.thread(sid,include_turns=True)["turns"]
        self.assertEqual([(turn["id"],turn["status"]) for turn in private_turns],
                         [(item["turn_id"],"interrupted") for item in (first,second,third)])

        task = loop.hermes.create(guid(),"Hold formal","No private stop permission","oguri")["task_id"]
        loop.worker.poll()
        clock.advance(10)
        loop.worker.poll()
        loop.worker.poll()
        deadline = time.monotonic()+12
        while rpc.event(deadline)["method"]!="item/tool/call":
            pass
        self.assertEqual(loop.worker.hold()["held_task_id"],task)
        self.assertIn("取消请走工单流程",command("stop_formal")[1])
        with self.assertRaisesRegex(Fault,"native_stop_not_private"):
            loop.adapter.stop_private(sid)
        self.assertTrue(host.busy(sid))
        self.assertEqual(loop.hermes.inspect(task)["task"]["state"],"working")
        self.assertFalse(loop.manager_wire.frames(0))
        self.evidence = {"thread_id":sid,"native_stop":saved["stop_receipt"],"lost_receipt_state":"unknown",
                         "later_turn_was_kept":third["turn_id"],"formal_task_kept":task,
                         "interrupted_private_turns":[turn["id"] for turn in private_turns],
                         "boundary":"Actual bundled App Server, scripted local model and isolated platform calls"}

    def test_app_server_next_turn_queue_never_steers_busy_turn_or_replays_lost_ack(self):
        self.provider.private_admissions = Queue()
        self.provider.release_private = Event()
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        self.addCleanup(self.provider.release_private.set)
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False})["thread"]["id"]
        host = AppServerHost(rpc,sid)
        host.session = sid
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:host))
        service = WorkerService(loop.worker,clock)
        token = guid()
        url = self.enterContext(running(service.listen({token:"platform/oguri"},("127.0.0.1",0),None)))
        platform = Client(url,token)
        service.step()

        def command(mid,text):
            return platform.request("POST","worker/command",{"message_id":mid,"text":text})["receipt"]

        def step():
            clock.advance(5)
            service.step()

        def queued():
            with loop.worker.db.transaction() as db:
                return [(row["message_id"],row["uncertain"],json.loads(row["receipt"]) if row["receipt"] else None)
                        for row in db.execute("SELECT * FROM queued_chats ORDER BY rowid")]

        first_text,second_text = "Hold bound private queued first","Queued second"
        first = command("queue_first","/queue "+first_text)
        command("queue_second","/queue "+second_text)
        self.assertEqual(command("queue_first","/queue "+first_text),first)
        original = loop.adapter.deliver
        before = []
        attempts = []
        def race_then_lose(did,payload,**kw):
            if did!="queued-chat-queue_first":
                return original(did,payload,**kw)
            attempts.append(did)
            if len(attempts)==1:
                msg = "Hold bound private race"
                mid = guid()
                loop.worker.chat(mid,msg)
                self.assertEqual(self.provider.private_admissions.get(timeout=8),msg)
                before.append(loop.worker.chat(mid,msg))
                return original(did,payload,**kw)
            original(did,payload,**kw)
            self.assertEqual(self.provider.private_admissions.get(timeout=8),first_text)
            proof = loop.adapter.lookup_delivery(did,payload,session=sid)
            self.assertIsNotNone(proof)
            self.assertEqual(proof["session"],sid)
            raise Fault(503,"isolated_queue_receipt_lost",True)
        loop.adapter.deliver = race_then_lose
        step()
        self.assertEqual(queued(),[("queue_first",0,None),("queue_second",0,None)])
        with loop.adapter.db.transaction() as db:
            self.assertFalse(db.execute("SELECT 1 FROM intents WHERE delivery_id LIKE 'queued-chat-%'").fetchone())
        active, = host.thread(sid,include_turns=True)["turns"]
        self.assertEqual(active["id"],before[0]["turn_id"])
        self.assertEqual(sum(item["type"]=="userMessage" for item in active["items"]),1)
        self.assertEqual(command("stop_race","/stop")["stop_receipt"]["turn_id"],active["id"])

        step()
        self.assertEqual(queued(),[("queue_first",1,None),("queue_second",0,None)])
        step()
        first_row,second_row = queued()
        self.assertIsNotNone(first_row[2])
        self.assertEqual(second_row,("queue_second",0,None))
        step()
        self.assertEqual(queued()[1],second_row)
        self.assertEqual(command("stop_queue","/stop")["stop_receipt"]["turn_id"],first_row[2]["turn_id"])
        step()
        until = time.monotonic()+8
        while (host.busy(sid) or queued()[1][2] is None) and time.monotonic()<until:
            step()
            time.sleep(.03)
        self.assertEqual(len(attempts),2)
        turns = host.thread(sid,include_turns=True)["turns"]
        texts = [[item["content"][0]["text"] for item in turn["items"] if item["type"]=="userMessage"] for turn in turns]
        self.assertEqual(texts,[["Hold bound private race"],[first_text],[second_text]])
        self.assertEqual([row[2]["turn_id"] for row in queued()],[turn["id"] for turn in turns[1:]])
        self.assertFalse(loop.hermes.tasks()["items"])
        self.evidence = {"thread_id":sid,"original_turn":before[0]["turn_id"],
                         "native_turns_ordered":[turn["id"] for turn in turns],"inputs_per_turn":[len(x) for x in texts],
                         "first_attempt_refused_before_native_write":True,"first_ack_found_by_lookup":True,
                         "write_attempts_for_first":len(attempts),
                         "boundary":"Actual App Server with a local model fixture, platform and transport fault driven in isolation"}

    def test_codex_tool_card_arrives_during_native_execution_and_final_stays_distinct(self):
        self.provider.public_preview = True
        release = Path(self.root)/".release-preview"
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False,"sandbox":"read-only",
                                         "approvalPolicy":"never"})["thread"]["id"]
        rpc.request("turn/start",{"threadId":sid,"input":[{"type":"text","text":"Seed preview fixture",
                                                          "text_elements":[]}]})
        until = time.monotonic()+12
        while rpc.event(until)["method"]!="turn/completed":
            pass
        host = AppServerHost(rpc,sid)
        host.session = sid
        loop = self.enterContext(Loop.isolated(host_factory=lambda _path,_name:host))
        tool_acks = []

        class LoseFirstToolReceipt(WorkerService):
            def handle(self,actor,method,path,body,headers):
                result = super().handle(actor,method,path,body,headers)
                if method=="POST" and path=="/v1/worker/display" and body["kind"]=="tool":
                    tool_acks.append(result[2])
                    if len(tool_acks)==1:
                        raise Fault(503,"isolated_tool_ack_lost",True)
                return result

        token,platform_token = guid(),guid()
        server = LoseFirstToolReceipt(loop.worker).listen({token:"adapter/oguri",platform_token:"platform/oguri"},
                                                         ("127.0.0.1",0),None)
        url = self.enterContext(running(server))
        adapter,platform = Client(url,token),Client(url,platform_token)
        sink = PlatformFixture()
        views = Views(loop.root/"live-codex-cards.db",platform,sink,"fixture-chat","小栗帽")
        output = CodexDisplay(loop.adapter,adapter)
        self.enterContext(output.run())
        self.enterContext(worker_callbacks(host,adapter,output.wake.set,output.observe,output.observe_terminal))

        def display_events():
            return [item["event"] for item in platform.request("GET","worker/display")["items"]]

        def card_titles():
            return [item[2]["header"]["title"]["content"] for item in sink.messages.values()]

        try:
            query = "Native tool preview fixture"
            turn = rpc.request("turn/start",{"threadId":sid,"input":[{"type":"text","text":query,
                                                                      "text_elements":[]}]})["turn"]["id"]
            until = time.monotonic()+10
            while len(tool_acks)<2 and time.monotonic()<until:
                time.sleep(.03)
            self.assertGreaterEqual(len(tool_acks),2)
            self.assertEqual(tool_acks[0],tool_acks[1])
            self.assertTrue(host.busy(sid))
            before = display_events()
            self.assertEqual([(item["kind"],item["text"]) for item in before],
                             [("native_input",query),("progress","Checking the isolated workspace."),("tool","pwd")])
            self.assertTrue(all(item["turn_id"]==turn for item in before))
            self.assertEqual(views.poll(),3)
            self.assertEqual(card_titles(),["小栗帽·原生输入","小栗帽-输出","小栗帽-工具"])
        finally:
            release.touch()
        until = time.monotonic()+12
        while time.monotonic()<until:
            events = display_events()
            if any(item["kind"]=="final" for item in events):
                break
            time.sleep(.04)
        self.assertEqual([item["kind"] for item in events],["native_input","progress","tool","final"])
        self.assertEqual(events[-1]["text"],"Local model fixture accepted the receipt.")
        self.assertNotIn("PREVIEW_PRIVATE_MARKER",encode(events))
        self.assertNotIn("sleep",encode(events))
        self.assertEqual(views.poll(),1)
        self.assertEqual(card_titles()[-1],"小栗帽-回复")
        self.assertFalse(loop.hermes.tasks()["items"])
        self.evidence = {"thread_id":sid,"turn_id":turn,"events":events,"tool_receipt_replayed":tool_acks[1],
                         "private_arguments_omitted":True,"early_cards":3,"card_titles":card_titles(),
                         "boundary":"Actual App Server command execution in an isolated test directory; model and Feishu fixtures"}

    def test_native_tool_publishes_standalone_question_and_accepts_answer_once(self):
        self.provider.standalone_received = []
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        tool, = [item for item in TOOLSET if item["name"]=="worker_ask_trainer"]
        session = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False,"dynamicTools":[tool]})["thread"]["id"]
        host = AppServerHost(rpc,session)
        host.session = session
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:host))
        adapter_key,platform_key,worker_key = guid(),guid(),guid()
        adapter_url = self.enterContext(running(serve_adapter(loop.adapter,"oguri",{worker_key:"worker/oguri"},
                                                      ("127.0.0.1",0),None)))
        loop.worker.adapter = RemoteAdapter(Client(adapter_url,worker_key),session)
        worker_server = WorkerService(loop.worker,clock).listen(
            {adapter_key:"adapter/oguri",platform_key:"platform/oguri"},("127.0.0.1",0),None)
        worker_url = self.enterContext(running(worker_server))
        platform,adapter = Client(worker_url,platform_key),Client(worker_url,adapter_key)
        sink = PlatformFixture()
        views = Views(loop.root/"standalone-codex-views.db",platform,sink,"test-chat","小栗帽",clock)
        actions = Actions(views,Inbound(loop.root/"standalone-inbound.db",platform,None,"app","trainer","test-chat"))
        sent = Event()
        self.enterContext(worker_callbacks(host,adapter,sent.set))
        source_turn = rpc.request("turn/start",{"threadId":session,"input":[
            {"type":"text","text":TRAINER_CARD_FIXTURE,"text_elements":[]}]})["turn"]["id"]
        self.assertTrue(sent.wait(12))
        sent.clear()
        before = host.read_turn(session,source_turn)
        native_tool, = [row for row in before["items"] if row["type"]=="dynamicToolCall"]
        self.assertEqual(native_tool["tool"],"worker_ask_trainer")
        views.poll()
        with views.db.transaction() as db:
            qid,mid = tuple(db.execute("SELECT qid,mid FROM question_cards").fetchone())
        self.assertEqual(self.provider.standalone_received,[])
        self.assertEqual([turn["id"] for turn in host.thread(session,include_turns=True)["turns"]],[source_turn])
        value = {"question_id":qid,"mode":"batch"}
        with self.assertRaisesRegex(Fault,"wrong_card_actor"):
            actions.record("app","other","test-chat",mid,value,{})
        forged = {"question_id":qid,"proof":{"closed":True}}
        with self.assertRaisesRegex(Fault,"standalone_question_requires_delivery_receipt"):
            adapter.request("POST","worker/native-question-proof",forged)
        data = ("app","trainer","test-chat",mid,value,{"options_1":"2","reply_2":"From the bound trainer"})
        self.assertEqual(actions.record(*data),"pending")
        original = host.deliver
        writes = []
        def lose(*args):
            original(*args)
            writes.append(args)
            raise Fault(503,"isolated_trainer_answer_ack_lost",True)
        host.deliver = lose
        actions.flush()
        self.assertTrue(sent.wait(12))
        self.assertEqual(actions.record(*data),"pending")
        views.poll()
        self.assertEqual(sink.calls,[("send",mid)])
        self.assertEqual(len(writes),1)
        # Resume the Worker using its durable state and the same adapter; its replay uses lookup.
        worker_server.shutdown()
        empty = HostAdapter(loop.root/"replacement-without-answer-intent.db",host,session)
        missing_url = self.enterContext(running(serve_adapter(empty,"oguri",{worker_key:"worker/oguri"},
                                                      ("127.0.0.1",0),None)))
        no_history = RemoteAdapter(Client(missing_url,worker_key),session)
        restarted = Worker(loop.worker.db.path,"oguri",loop.worker.router,no_history,clock=clock)
        recovered_url = self.enterContext(running(WorkerService(restarted,clock).listen(
            {platform_key:"platform/oguri"},("127.0.0.1",0),None)))
        views.worker = Client(recovered_url,platform_key)
        clock.advance(16)
        actions.flush()
        self.assertEqual(actions.record(*data),"pending")
        self.assertEqual(len(writes),1)
        restarted.adapter = loop.worker.adapter
        clock.advance(16)
        actions.flush()
        views.poll()
        self.assertEqual(actions.record(*data),"confirmed")
        self.assertEqual(sink.calls,[("send",mid),("patch",mid)])
        self.assertEqual(len(writes),1)
        reply, = self.provider.standalone_received
        self.assertEqual((reply["question_id"],reply["source_turn_id"],reply["session"]),(qid,source_turn,session))
        self.assertEqual(reply["answer"],{"mode":"batch","answers":[{"id":"1","option_ids":["2"]},
            {"id":"2","option_ids":[],"text":"From the bound trainer"},{"id":"3","option_ids":[]}]})
        native_receipt = host.lookup_expected(*writes[0])
        turns = host.thread(session,include_turns=True)["turns"]
        self.assertEqual([turn["id"] for turn in turns],[source_turn,native_receipt["turn_id"]])
        self.assertEqual(loop.hermes.tasks()["items"],[])
        with restarted.db.transaction() as db:
            proof = json.loads(db.execute("SELECT proof FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()[0])
        self.assertEqual((proof["receipt_id"],proof["answer_turn_id"]),(native_receipt["native_id"],native_receipt["turn_id"]))
        self.evidence = {"session":session,"source_turn_id":source_turn,"native_tool":native_tool["tool"],
                         "question_id":qid,"fixture_message_id":mid,"host_receipt":native_receipt,
                         "worker_proof":proof,"original_card_updates":1,"native_writes":1,
                         "retry_without_adapter_ledger":"pending",
                         "boundary":"Bundled App Server, isolated adapter and Worker HTTP, local model and Feishu fixture."}

    def test_native_trainer_question_closes_original_card_only_after_app_server_receipt(self):
        self.provider.trainer_responses = []
        rpc = self.enterContext(isolated_app_server(self.root,self.origin,
                                                    ["features.default_mode_request_user_input=true"]))
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False})["thread"]["id"]
        rpc.request("turn/start",{"threadId":sid,"input":[{"type":"text","text":"Seed the trainer question fixture",
                                                          "text_elements":[]}]})
        until = time.monotonic()+12
        while rpc.event(until)["method"]!="turn/completed":
            pass
        host = AppServerHost(rpc,sid,Path(self.root)/"native-questions.db")
        host.session = sid
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:host))
        service = WorkerService(loop.worker,clock)
        token,trainer_token = guid(),guid()
        url = self.enterContext(running(service.listen({token:"adapter/oguri",trainer_token:"platform/oguri"},
                                                       ("127.0.0.1",0),None)))
        adapter,platform = Client(url,token),Client(url,trainer_token)
        sink = PlatformFixture()
        platform_db = loop.root/"native-trainer-platform.db"
        inbound = Inbound(platform_db,platform,None,"app","trainer","private-chat",clock=clock)
        views = Views(platform_db,platform,sink,"private-chat","小栗帽",clock)
        actions = Actions(views,inbound)
        output = CodexDisplay(loop.adapter,adapter)
        self.enterContext(output.run())
        self.enterContext(worker_callbacks(host,adapter,output.wake.set,output.observe,output.observe_terminal))

        def events():
            return [item["event"] for item in platform.request("GET","worker/display")["items"]]

        def wait_for(observe):
            until = time.monotonic()+10
            while time.monotonic()<until:
                result = observe()
                if result:
                    return result
                time.sleep(.025)
            self.fail("native trainer question state did not arrive")

        def send(mode):
            self.assertFalse(host.busy(sid))
            return rpc.request("turn/start",{"threadId":sid,"input":[{"type":"text",
                               "text":"Native trainer question: "+mode,"text_elements":[]}]})["turn"]["id"]

        def open_card(mode):
            turn = send(mode)
            event = wait_for(lambda: next((e for e in events() if e["kind"]=="question_open" and e["turn_id"]==turn),None))
            qid = event["question_id"]
            if mode=="batch":
                fields = event["questions"]
                self.assertEqual([field["id"] for field in fields],["route","note","optional"])
                self.assertTrue(all(field["custom"] for field in fields))
                self.assertFalse(any(field["multiple"] for field in fields))
            else:
                self.assertEqual(event["text"],"选择路线 · "+mode)
                self.assertEqual([opt["label"] for opt in event["options"]],["第一项","第二项"])
            views.poll()
            with views.db.transaction() as db:
                mid = db.execute("SELECT mid FROM question_cards WHERE qid=?",(qid,)).fetchone()[0]
            card = next(item for item in sink.messages.values() if item[0]==mid)
            self.assertIn("form",[element["tag"] for element in card[2]["body"]["elements"]])
            return turn,qid,mid,card

        delivered = []
        for mode,value,form,wanted in (
                ("choice",{"mode":"choice","option_id":"2"},None,{"route":{"answers":["第二项"]}}),
                ("text",{"mode":"text"},{"reply":"  原样回答  "},{"route":{"answers":["  原样回答  "]}}),
                ("skip",{"mode":"skip"},None,{}),
                ("batch",{"mode":"batch"},{"options_1":"2","reply_2":"  留在原题  "},
                 {"route":{"answers":["第二项"]},"note":{"answers":["  留在原题  "]}})):
            turn,qid,mid,card = open_card(mode)
            action = {"question_id":qid,**value}
            with self.assertRaisesRegex(Fault,"wrong_card_actor"):
                actions.record("app","another","private-chat",mid,action,form)
            with self.assertRaisesRegex(Fault,"not_original_trainer_card"):
                actions.record("app","trainer","private-chat","om_wrong",action,form)
            if mode=="batch":
                self.assertIn("First",json.dumps(card[2],ensure_ascii=False))
                for invalid in (dict(form,options_1="1,2"),dict(form,reply_1="不能与选项同时提交")):
                    with self.assertRaises(Fault):
                        actions.record("app","trainer","private-chat",mid,action,invalid)
            self.assertEqual(actions.record("app","trainer","private-chat",mid,action,form),"pending")
            actions.flush()
            proof = host.questions.lookup(sid,turn,"native_trainer_"+mode)
            self.assertTrue(proof and proof["closed"])
            self.assertEqual(actions.record("app","trainer","private-chat",mid,action,form),"confirmed")
            wait_for(lambda: not host.busy(sid))
            observed = wait_for(lambda: next((json.loads(raw) for name,raw in self.provider.trainer_responses if name==mode),None))
            self.assertEqual(observed["answers"],wanted)
            views.poll()
            self.assertEqual(card[2]["header"]["title"]["content"],"小栗帽-提问 · 已回答")
            self.assertNotIn("form",[element["tag"] for element in card[2]["body"]["elements"]])
            self.assertEqual(sink.calls.count(("patch",mid)),1)
            delivered.append({"mode":mode,"message_id":mid,"question_id":qid,"proof":proof,"model_saw":observed})
        turn,qid,mid,card = open_card("unconfirmed")
        original_closed = host.questions.closed
        host.questions.closed = lambda _params: None
        try:
            action = {"question_id":qid,"mode":"choice","option_id":"1"}
            actions.record("app","trainer","private-chat",mid,action,None)
            actions.flush()
            self.assertIsNone(host.questions.lookup(sid,turn,"native_trainer_unconfirmed"))
            clock.advance(16)
            actions.flush()
        finally:
            host.questions.closed = original_closed
        views.poll()
        self.assertIn("form",[element["tag"] for element in card[2]["body"]["elements"]])
        self.assertEqual(actions.record("app","trainer","private-chat",mid,action,None),"pending")
        self.assertEqual(sum(name=="unconfirmed" for name,_ in self.provider.trainer_responses),1)
        wait_for(lambda: not host.busy(sid))
        for mode in ("private","mixed-private"):
            secret = send(mode)
            wait_for(lambda: any(name==mode for name,_ in self.provider.trainer_responses))
            self.assertFalse([item for item in events() if item["turn_id"]==secret and item["kind"]=="question_open"])
            wait_for(lambda: not host.busy(sid))
        with self.assertRaisesRegex(Fault,"native_question_already_offered"):
            host.questions.offer({"id":900,"params":{"threadId":sid,"turnId":turn,
                "itemId":"native_trainer_unconfirmed","autoResolutionMs":None,
                "questions":[{"id":"route","question":"选择路线 · unconfirmed","isOther":True,"isSecret":False,
                              "options":[{"label":"第一项"},{"label":"第二项"}]}]}})
        self.assertFalse(loop.hermes.tasks()["items"])
        self.evidence = {"thread_id":sid,"answered":delivered,"unconfirmed_card_id":mid,"unconfirmed_untouched":True,
                         "secret_question_projected":False,"mixed_secret_group_projected":False,
                         "boundary":"Actual App Server requestUserInput and fixture card actions, no Feishu identity or cloud model"}

    def test_native_approved_image_splits_the_platform_card_and_replays_the_original_upload(self):
        root = Path(self.root)
        public = root/"approved-images"
        public.mkdir(mode=0o700)
        visible,private,link = public/"share.png",root/"private.png",public/"linked.png"
        visible.write_bytes(PNG)
        private.write_bytes(PNG)
        link.symlink_to(private)
        self.provider.public_image_paths = {"approved":str(visible),"outside":str(private),"symlink":str(link)}
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False,"sandbox":"read-only",
                                         "approvalPolicy":"never"})["thread"]["id"]
        rpc.request("turn/start",{"threadId":sid,"input":[{"type":"text","text":"Seed native media fixture",
                                                          "text_elements":[]}]})
        until = time.monotonic()+12
        while rpc.event(until)["method"]!="turn/completed":
            pass
        ledger = root/"image-publication.db"
        host = AppServerHost(rpc,sid,ledger,public)
        host.session = sid
        read_turn = host.read_turn
        temporary_interrupt = []
        def early_history(session,turn):
            native = read_turn(session,turn)
            if (not temporary_interrupt and native["status"]=="completed"
                    and any(item["id"]=="native_image_final_symlink" for item in native["items"])):
                temporary_interrupt.append(turn)
                return dict(native,status="interrupted")
            return native
        host.read_turn = early_history
        with self.assertRaisesRegex(Fault,"native_image_root_changed"):
            AppServerHost(rpc,sid,ledger,root)
        loop = self.enterContext(Loop.isolated(host_factory=lambda _path,_name:host))
        upload_receipts = []

        class LoseImageReceipt(WorkerService):
            def handle(self,actor,method,path,body,headers):
                result = super().handle(actor,method,path,body,headers)
                if method=="PATCH" and path.startswith("/v1/worker/media/uploads/") and result[2]["complete"]:
                    upload_receipts.append(result[2])
                    if len(upload_receipts)==1:
                        visible.unlink()
                        raise Fault(503,"isolated_image_ack_lost",True)
                return result

        token,trainer_token = guid(),guid()
        url = self.enterContext(running(LoseImageReceipt(loop.worker).listen(
            {token:"adapter/oguri",trainer_token:"platform/oguri"},("127.0.0.1",0),None)))
        adapter,platform = Client(url,token),Client(url,trainer_token)
        sink = PlatformFixture()
        views = Views(loop.root/"native-output-image-views.db",platform,sink,"fixture-chat","小栗帽")
        display = CodexDisplay(loop.adapter,adapter)
        self.enterContext(display.run())
        self.enterContext(worker_callbacks(host,adapter,display.wake.set,display.observe,display.observe_terminal))

        def converse(mode):
            tid = rpc.request("turn/start",{"threadId":sid,"input":[{"type":"text",
                              "text":"Native output image: "+mode,"text_elements":[]}]})["turn"]["id"]
            until = time.monotonic()+10
            while time.monotonic()<until:
                events = [row["event"] for row in platform.request("GET","worker/display")["items"]
                          if row["event"]["turn_id"]==tid]
                with display.db.transaction() as db:
                    saved = db.execute("SELECT sent FROM codex_display_turns WHERE turn_id=?",(tid,)).fetchone()
                if saved and saved[0]:
                    events = [row["event"] for row in platform.request("GET","worker/display")["items"]
                              if row["event"]["turn_id"]==tid]
                    break
                time.sleep(.04)
            turns = host.thread(sid,include_turns=True)["turns"]
            native = next(turn for turn in turns if turn["id"]==tid)
            self.assertEqual(native["status"],"completed")
            source, = [item for item in native["items"] if item["type"]=="imageView"]
            self.assertEqual(source["path"],self.provider.public_image_paths[mode])
            self.assertEqual([row["kind"] for row in events].count("final"),1,
                             (mode,[(item.get("id"),item.get("type"),item.get("phase"),item.get("text"))
                                    for item in native["items"]],
                              [(item["kind"],item.get("text")) for item in events]))
            return events

        approved = converse("approved")
        self.assertEqual([item["kind"] for item in approved],
                         ["native_input","progress","progress","image","progress","progress","final"])
        self.assertEqual(approved[3]["attachment_id"],upload_receipts[0]["attachment_id"])
        self.assertNotIn("native_path",encode(approved))
        self.assertEqual(views.poll(),7)
        cards = list(sink.messages.values())
        self.assertEqual([card[1] for card in cards],["interactive","interactive","image","interactive","interactive"])
        left,right = (cards[n][2]["elements"][0]["text"]["content"] for n in (1,3))
        self.assertEqual((left,right),("Before image continued.","After image continued."))
        self.assertEqual(sink.images,[PNG])
        self.assertFalse(visible.exists())
        self.assertEqual(sink.calls,[("send","om_1"),("send","om_2"),("patch","om_2"),("send","om_3"),
                                     ("send","om_4"),("patch","om_4"),("send","om_5")])
        for mode in ("outside","symlink"):
            denied = converse(mode)
            self.assertEqual([item["kind"] for item in denied],
                             ["native_input","progress","progress","progress","progress","final"])
            views.poll()
        self.assertEqual(len(upload_receipts),1)
        self.assertEqual(sink.images,[PNG])
        self.assertEqual(len(temporary_interrupt),1)
        self.assertFalse(loop.hermes.tasks()["items"])
        with loop.worker.db.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM media_requests").fetchone()[0],1)
        self.evidence = {"thread_id":sid,"turn_id":approved[3]["turn_id"],"image_attachment_id":approved[3]["attachment_id"],
                         "sha256":hashlib.sha256(sink.images[0]).hexdigest(),"platform_card_sections":[left,right],
                         "first_platform_order":[card[1] for card in cards],"fixture_image_message_id":cards[2][0],
                         "file_uploads":len(upload_receipts),"source_gone_before_replay":True,
                         "outside_and_symlink_not_projected":True,
                         "provisional_interruption_did_not_downgrade_final":True,
                         "boundary":"Actual App Server view_image on approved fixture file; model and Feishu are fixtures"}

    def test_native_app_server_conversation_uses_standard_feishu_cards(self):
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False})["thread"]["id"]
        host = AppServerHost(rpc,sid)
        host.session = sid
        loop = self.enterContext(Loop.isolated(host_factory=lambda _path,_name:host))
        service = WorkerService(loop.worker)
        token,platform_token = guid(),guid()
        url = self.enterContext(running(service.listen({token:"adapter/oguri",platform_token:"platform/oguri"},
                                                       ("127.0.0.1",0),None)))
        callback = Client(url,token)
        trainer_text = "  这句从原生会话发出。\n保留换行。  "
        turn = rpc.request("turn/start",{"threadId":sid,"input":[{"type":"text","text":trainer_text,"text_elements":[]}],
                                        "clientUserMessageId":"trainer-test-input"})["turn"]["id"]
        until = time.monotonic()+14
        while True:
            event = rpc.event(until)
            if event.get("method")=="turn/completed":
                self.assertEqual(event["params"]["turn"]["status"],"completed")
                break
        first = host.publish_public_turn(sid,turn,callback)
        self.assertEqual(host.publish_public_turn(sid,turn,callback),first)
        platform = Client(url,platform_token)
        display = platform.request("GET","worker/display")["items"]
        self.assertEqual([(row["event"]["kind"],row["event"]["text"]) for row in display],
                         [("native_input",trainer_text),("final","Local model fixture accepted the receipt.")])
        sink = PlatformFixture()
        views = Views(loop.root/"codex-views.db",platform,sink,"isolated-private-chat","小栗帽")
        self.assertEqual((views.poll(),views.poll()),(2,0))
        messages = list(sink.messages.values())
        titles = [message[2]["header"]["title"]["content"] for message in messages]
        self.assertEqual(titles,["小栗帽·原生输入","小栗帽-回复"])
        self.assertEqual([message[1] for message in messages],["interactive","interactive"])
        self.assertEqual(self.provider.observed,[(False,0)])
        self.assertFalse(loop.hermes.tasks()["items"])
        self.evidence = {"thread_id":sid,"turn_id":turn,"worker_events":first,"card_titles":titles,
                         "fixture_card_ids":[row[0] for row in messages],"fixture_model_requests":1,
                         "boundary":"Native App Server history, synthetic user input and local model, Feishu fixture"}

    def test_native_app_server_admits_captioned_and_pure_images_with_original_bytes(self):
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False})["thread"]["id"]
        host = AppServerHost(rpc,sid)
        host.session = sid
        loop = self.enterContext(Loop.isolated(host_factory=lambda _path,_name:host))
        service = WorkerService(loop.worker)
        token,platform_token = guid(),guid()
        url = self.enterContext(running(service.listen({token:"adapter/oguri",platform_token:"platform/oguri"},
                                                       ("127.0.0.1",0),None)))
        loop.adapter.media_store = NativeFiles(loop.root/"codex-media",Client(url,token))
        platform = Client(url,platform_token)
        inbound = Inbound(loop.root/"codex-inbox.db",platform,lambda mid,key,kind,limit: PNG,"app","trainer","chat")
        text = "图和文字是一条。\n  保留空格。  "
        post = {"zh_cn":{"content":[[{"tag":"text","text":text},{"tag":"img","image_key":"img_fixture"}]]}}
        samples = (("om_caption","post",post),("om_image","image",{"image_key":"img_fixture"}))
        original_request = platform.request
        def lose_reply(method,path,*args,**kwargs):
            reply = original_request(method,path,*args,**kwargs)
            if path=="worker/chat":
                raise ConnectionError("isolated chat response lost")
            return reply
        proofs = []
        for mid,kind,body in samples:
            inbound.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":mid,
                           "message_type":kind,"content":encode(body)})
            platform.request = lose_reply
            with self.assertRaises(ConnectionError):
                inbound.deliver(mid)
            platform.request = original_request
            until = time.monotonic()+14
            while True:
                event = rpc.event(until)
                if event.get("method")=="turn/completed":
                    self.assertEqual(event["params"]["turn"]["status"],"completed")
                    break
            proof = inbound.deliver(mid)
            self.assertEqual(inbound.deliver(mid),proof)
            proofs.append(proof)
        sha = hashlib.sha256(PNG).hexdigest()
        self.assertEqual(self.provider.media,[{"caption":[text],"image_sha256":[sha]},
                                             {"caption":[],"image_sha256":[sha]}])
        turns = host.thread(sid,include_turns=True)["turns"]
        self.assertEqual(len(turns),len(samples))
        inputs = [item for turn in turns for item in turn["items"] if item["type"]=="userMessage"]
        self.assertEqual([item["id"] for item in inputs],[proof["native_id"] for proof in proofs])
        self.assertEqual([proof["media_sha256"] for proof in proofs],[[sha],[sha]])
        self.assertEqual((loop.root/"codex-media"/sha).read_bytes(),PNG)
        self.assertFalse(loop.hermes.tasks()["items"])
        self.evidence = {"thread_id":sid,"receipts":proofs,"model_input":self.provider.media,
                         "native_input_count":len(inputs),"router_tasks":0,
                         "boundary":"Native App Server receives Feishu fixture images; local model records the original PNG bytes"}

    @unittest.skipUnless(shutil.which("openssl"),"isolated TLS certificate generator required")
    def test_codex_adapter_process_handles_native_report_without_controller_dispatch(self):
        root = Path(self.root)
        profile,project = root/"codex-profile",root/"test-project"
        profile.mkdir(mode=0o700)
        project.mkdir(mode=0o700)
        self.local_profile(profile)
        seed_result = subprocess.run(self.provision_command(profile,project),
                                     cwd=Path(__file__).resolve().parent.parent,capture_output=True,text=True,timeout=30)
        self.assertEqual(seed_result.returncode,0,seed_result.stderr)
        seed = json.loads(seed_result.stdout)
        sid = seed["thread_id"]
        self.assertEqual(seed["requested_tools"],["worker_report"])
        self.assertEqual(seed["cwd"],str(project.resolve()))
        self.assertEqual(self.provider.observed,[(True,0)])
        self.provider.observed.clear()
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock))
        callback_token,adapter_token,platform_token = guid(),guid(),guid()
        display_acks = []
        class LostDisplayReceipt(WorkerService):
            def handle(self,actor,method,path,body,headers):
                result = super().handle(actor,method,path,body,headers)
                if method=="POST" and path=="/v1/worker/display":
                    display_acks.append(result[2])
                    if len(display_acks)==1:
                        raise Fault(503,"isolated_display_receipt_lost",True)
                return result
        callback_server = LostDisplayReceipt(loop.worker,clock).listen(
            {callback_token:"adapter/oguri",platform_token:"platform/oguri"},("127.0.0.1",0),None)
        callback_url = self.enterContext(running(callback_server))
        def write(name,body):
            dest = root/name
            dest.write_text(encode(body),encoding="utf-8")
            dest.chmod(0o600)
            return dest
        worker_conn = write("worker-connection.json",{"url":callback_url,"token":callback_token})
        roles = write("adapter-roles.json",{adapter_token:"worker/oguri"})
        cert,key = root/"test.crt",root/"test.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",key],
                       check=True,capture_output=True,timeout=10)
        adapter_db = root/"codex-adapter.db"
        command = [sys.executable,"-m","rewrite.service","codex-adapter","--db",adapter_db,"--identities",roles,
                   "--cert",cert,"--key",key,"--worker-id","oguri","--session",sid,"--worker-connection",worker_conn,
                   "--codex-binary",CODEX,"--codex-profile",profile,"--cwd",project]
        wrong_project = subprocess.run([*command[:-1],root],cwd=Path(__file__).resolve().parent.parent,
                                       capture_output=True,text=True,timeout=8)
        self.assertNotEqual(wrong_project.returncode,0)
        self.assertIn("wrong_native_workspace",wrong_project.stderr)
        process = subprocess.Popen(command,cwd=Path(__file__).resolve().parent.parent,text=True,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            ready = Queue()
            Thread(target=lambda:ready.put(process.stdout.readline()),daemon=True).start()
            line = ready.get(timeout=12)
            self.assertTrue(line,process.stderr.read() if process.poll() is not None else "no adapter readiness")
            info = json.loads(line)
            self.assertEqual(info["component"],"codex-adapter")
            self.assertTrue(info["tls"])
            other_db = [root/"alternate-adapter.db" if value==adapter_db else value for value in command]
            duplicate = subprocess.run(other_db,cwd=Path(__file__).resolve().parent.parent,capture_output=True,
                                       text=True,timeout=8)
            self.assertEqual((duplicate.returncode,duplicate.stderr),(1,"state_writer_already_running\n"))
            transport = Client("https://127.0.0.1:"+str(info["port"]),adapter_token,cert)
            with self.assertRaisesRegex(Fault,"wrong_native_session"):
                RemoteAdapter(transport,guid()).busy()
            remote = RemoteAdapter(transport,sid)
            loop.worker.adapter = remote
            tid = loop.hermes.create(guid(),"Standalone Codex adapter test","File an isolated result","oguri")["task_id"]
            loop.worker.connect()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            until = time.monotonic()+9
            while time.monotonic()<until:
                loop.worker.poll()
                with closing(sqlite3.connect(adapter_db)) as db:
                    pending_output = db.execute("SELECT COUNT(*) FROM codex_display_turns WHERE sent=0").fetchone()[0]
                if (len(display_acks)==2 and pending_output==0
                        and loop.hermes.inspect(tid)["task"]["state"]=="completed_reported" and not remote.busy()):
                    break
                time.sleep(.05)
            self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"completed_reported")
            self.assertIsNone(loop.worker.hold()["held_assignment_id"])
            self.assertFalse(loop.native.entries())
            self.assertEqual(len(display_acks),2)
            self.assertEqual(display_acks[0],display_acks[1])
            sink = PlatformFixture()
            views = Views(root/"codex-views.db",Client(callback_url,platform_token),sink,"fixture-chat","小栗帽")
            self.assertEqual((views.poll(),views.poll()),(2,0))
            titles = [card[2]["header"]["title"]["content"] for card in sink.messages.values()]
            self.assertEqual(titles,["小栗帽·工单","小栗帽-回复"])
        finally:
            test_service.Service.stop(process)
        self.assertEqual(self.provider.observed,[(True,0),(True,1),(True,2)])
        notify, = loop.manager_wire.frames(0)
        self.assertEqual(notify["payload"]["task_id"],tid)
        frame, = [frame for frame in loop.wire.frames(0) if frame["kind"]=="task.dispatch"]
        with start_app_server(CODEX,profile,project) as review:
            native = review.request("thread/resume",{"threadId":sid,"cwd":str(project)})["thread"]
            published = AppServerHost(review,sid)
            body = {"kind":"task",**frame["payload"]}
            did = frame["delivery_id"]
            proof = published.lookup_expected(sid,did,body,hashlib.sha256(encode(body).encode()).hexdigest())
            self.assertIsNotNone(proof)
            turns = published.thread(sid,include_turns=True)["turns"]
            self.assertEqual(len(turns),2)
            self.assertEqual(turns[0]["id"],seed["turn_id"])
            seed_inputs = [item for item in turns[0]["items"] if item.get("type")=="userMessage"]
            self.assertEqual([(item["id"],item["clientId"]) for item in seed_inputs],
                             [(seed["native_input_id"],seed["client_input_id"])])
            calls = [item["id"] for item in turns[1]["items"] if item.get("type")=="dynamicToolCall"]
            self.assertEqual(calls,["native_fixture_0","native_fixture_1"])
            restored_display = CodexDisplay(HostAdapter(adapter_db,published,sid),Client(callback_url,callback_token))
            restored_display.collect()
            restored_display.flush()
            self.assertEqual(len(display_acks),2)
        self.evidence = {"thread_id":sid,"task_id":tid,"assignment_id":body["assignment_id"],"native_receipt":proof,
                         "native_tool_call_ids":calls,"notification_delivery_id":notify["delivery_id"],
                         "cli_version":native["cliVersion"],"router_state":"completed_reported","tls":True,
                         "wrong_workspace_rejected":True,"second_profile_writer_rejected":True,
                         "provisioned_native_input_id":seed["native_input_id"],"provisioned_turn_id":seed["turn_id"],
                         "provisioned_client_input_id":seed["client_input_id"],"thread_seed_count":1,
                         "worker_display_receipts":display_acks,"fixture_card_titles":titles,
                         "boundary":"Separate codex-adapter process drove the native App Server with a local scripted model"}

    def test_native_question_returns_to_the_original_app_server_assignment(self):
        rpc = self.enterContext(isolated_app_server(self.root,self.origin))
        allowed = [tool for tool in TOOLSET if tool["name"] in ("worker_ask","worker_report")]
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False,"dynamicTools":allowed})["thread"]["id"]
        host = AppServerHost(rpc,sid)
        host.session = sid
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:host))
        token = guid()
        server = WorkerService(loop.worker,clock).listen({token:"adapter/oguri"},("127.0.0.1",0),None)
        callback = Client(self.enterContext(running(server)),token)
        settled = Event()
        self.enterContext(worker_callbacks(host,callback,settled.set))
        tid = loop.hermes.create(guid(),"Original native question","Ask the scripted manager before proceeding",
                                 "oguri")["task_id"]
        loop.worker.connect()
        loop.worker.poll()
        clock.advance(10)
        loop.worker.poll()
        loop.worker.poll()
        def wait_for_native_turn():
            until = time.monotonic()+8
            while not settled.wait(.02) and time.monotonic()<until:
                loop.worker.poll()
            self.assertTrue(settled.is_set())
            settled.clear()
        wait_for_native_turn()
        aid = loop.worker.hold()["held_assignment_id"]
        questions = loop.hermes.questions(task_id=tid,pending="true")["items"]
        native_turns = host.thread(sid,include_turns=True)["turns"]
        self.assertEqual(len(questions),1,native_turns)
        question, = questions
        qid = question["question_id"]
        self.assertEqual(question["assignment_id"],aid)
        self.assertEqual(question["text"],"Use the first route?")
        self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"working")
        self.assertIsNone(loop.hermes.inspect(tid)["last_report"])
        answer = {"mode":"choice","option_id":"go"}
        key = guid()
        reply = loop.hermes.answer(key,tid,qid,answer)
        self.assertEqual(loop.hermes.answer(key,tid,qid,answer),reply)
        loop.worker.poll()
        loop.worker.poll()
        wait_for_native_turn()
        thread = host.thread(sid,include_turns=True)
        first,second = thread["turns"]
        asks = [item for item in first["items"] if item.get("type")=="dynamicToolCall"]
        resumed_input, = [item for item in second["items"] if item.get("type")=="userMessage"]
        content = resumed_input["content"][0]["text"]
        resumed = json.loads(content.split("\n",1)[1])["payload"]
        self.assertEqual((resumed["kind"],resumed["task_id"],resumed["assignment_id"],resumed["question_id"],resumed["answer"]),
                         ("question_answer",tid,aid,qid,answer))
        ask, = asks
        self.assertEqual((ask["tool"],ask["id"],ask["success"]),("worker_ask","native_question",True))
        results = [item for item in second["items"] if item.get("type")=="dynamicToolCall"]
        self.assertEqual([item["tool"] for item in results],["worker_report","worker_report"])
        self.assertTrue(all(item["success"] for item in results))
        self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"completed_reported")
        self.assertIsNone(loop.worker.hold()["held_task_id"])
        question_notice,result_notice = loop.manager_wire.frames(0)
        self.assertEqual((question_notice["payload"]["task_id"],result_notice["payload"]["task_id"]),(tid,tid))
        self.evidence = {"session":sid,"task_id":tid,"assignment_id":aid,"question_id":qid,"original_question_turn":first["id"],
                         "original_tool_call":ask["id"],"answer_turn":second["id"],"answer_input":resumed_input["id"],
                         "manager_answer":answer,"report_tool_calls":[item["id"] for item in results],
                         "notification_ids":[question_notice["delivery_id"],result_notice["delivery_id"]],
                         "boundary":"Native App Server with scripted local model and an isolated manager test driver"}

    def test_router_cancel_interrupts_bound_app_server_turn_and_preserves_unrelated_turn(self):
        cases = []
        for unrelated in (False,True):
            with self.subTest(unrelated=unrelated), isolated_app_server(self.root,self.origin) as rpc:
                loop,host,frame,original,_call = self.pending_native_task(rpc)
                sid = original["session"]
                tid,aid = frame["payload"]["task_id"],frame["payload"]["assignment_id"]
                foreign = None
                if unrelated:
                    self.assertIsNotNone(host.stop_turn(sid,original,start=True))
                    foreign_id = guid()
                    other = {"kind":"task","task_id":guid(),"assignment_id":guid()}
                    digest = hashlib.sha256(encode(other).encode()).hexdigest()
                    host.deliver(sid,foreign_id,other,digest)
                    until = time.monotonic()+12
                    while True:
                        event = rpc.event(until)
                        if event.get("method")=="item/tool/call":
                            break
                    foreign = host.lookup_expected(sid,foreign_id,other,digest)
                    self.assertIsNotNone(foreign)
                    with self.assertRaisesRegex(Fault,"native_task_not_admitted_busy"):
                        host.deliver(sid,guid(),other,digest)
                current = loop.hermes.inspect(tid)["task"]
                loop.hermes.act(guid(),tid,current["revision"],"cancel",reason="Cancel original isolated work")
                loop.worker.poll()
                if foreign:
                    live = host.thread(sid,include_turns=True)["turns"][-1]
                    self.assertEqual((live["id"],live["status"]),(foreign["turn_id"],"inProgress"))
                    self.assertIsNotNone(host.stop_turn(sid,foreign,start=True))
                until = time.monotonic()+12
                while time.monotonic()<until:
                    loop.worker.poll()
                    turns = host.thread(sid,include_turns=True)["turns"]
                    if len(turns)==(3 if unrelated else 2) and turns[-1]["status"]=="completed":
                        break
                    time.sleep(.02)
                self.assertEqual([turn["status"] for turn in turns],["interrupted"]*(2 if unrelated else 1)+["completed"])
                control, = [item for item in loop.wire.frames(0) if item["kind"]=="task.cancel"]
                body = {"kind":"task.cancel",**control["payload"]}
                receipt = loop.adapter.lookup_delivery(control["delivery_id"],body,session=sid)
                self.assertEqual(receipt["turn_id"],turns[-1]["id"])
                with self.assertRaisesRegex(Fault,"native_cleanup_unconfirmed"):
                    loop.worker.report(receipt["native_id"],tid,aid,"No cleanup proof","cancel_confirmed")
                self.assertEqual(loop.worker.hold(),{"held_task_id":tid,"held_assignment_id":aid})
                result = loop.hermes.inspect(tid)
                self.assertIsNone(result["last_report"])
                self.assertEqual(result["task"]["state"],"working")
                self.assertEqual([row["assignment_id"] for row in result["pending_cleanup"]],[aid])
                self.assertEqual(loop.manager_wire.frames(0),[])
                cases.append({"task_id":tid,"assignment_id":aid,"original":original,"control_receipt":receipt,
                              "unrelated":foreign,"unrelated_preserved_at_cancel":unrelated,
                              "native_statuses":[turn["status"] for turn in turns],"cleanup_pending":True})
        self.evidence = {"cases":cases,"boundary":"Isolated Router and native App Server, scripted model. No cleanup success inferred."}

    def test_model_effort_and_context_read_native_app_server_observations(self):
        self.provider.config_observations = []
        rpc = self.enterContext(isolated_app_server(self.root,self.origin,['model_reasoning_effort="high"']))
        sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False})["thread"]["id"]
        host = AppServerHost(rpc,sid)
        host.session = sid
        loop = self.enterContext(Loop.isolated(host_factory=lambda _path,_name:host))
        service = WorkerService(loop.worker)
        platform_token,adapter_token = guid(),guid()
        server = service.listen({platform_token:"platform/oguri",adapter_token:"adapter/oguri"},("127.0.0.1",0),None)
        url = self.enterContext(running(server))
        platform = Client(url,platform_token)
        settled = Event()
        self.enterContext(worker_callbacks(host,Client(url,adapter_token),settled.set))
        service.step()

        def command(mid,text):
            receipt = platform.request("POST","worker/command",{"message_id":mid,"text":text})["receipt"]
            view = platform.request("GET","worker/display")["items"]
            return next(item["event"]["text"] for item in view if item["seq"]==receipt["output"]["seq"])

        current = command("om_model_native","/model")
        effort = command("om_effort_native","/effort")
        self.assertIn("会话当前选择 test_wire/gpt-5.4 · high",current)
        self.assertIn("最近记录的请求配置 未记录",effort)
        self.assertIn("该选项暂不可用",command("om_effort_change","/effort low"))
        self.assertIn("还没有模型回包",command("om_context_pending","/context"))
        rpc.request("turn/start",{"threadId":sid,"input":[{"type":"text","text":"Context fixture","text_elements":[]}]})
        self.assertTrue(settled.wait(8))
        usage = host.context_usage(sid)["latest"]
        self.assertEqual((usage["turn"],usage["input_tokens"],usage["total_tokens"]),(1,11,16))
        self.assertGreater(usage["window_tokens"],0)
        context = command("om_context_after","/context")
        self.assertIn("第 1 回合",context)
        self.assertNotIn("步",context)
        self.assertIn("输入 11 tokens",context)
        self.assertIn("模型窗口 "+str(usage["window_tokens"])+" tokens",context)
        self.assertEqual(self.provider.config_observations,[("gpt-5.4","high")])
        self.assertFalse(loop.hermes.tasks()["items"])
        self.evidence = {"thread_id":sid,"model_card":current,"effort_card":effort,"context_card":context,
                         "native_usage":usage,"scripted_provider_config":self.provider.config_observations,
                         "boundary":"Actual isolated App Server settings and usage events from a local model fixture"}

    def test_native_model_and_effort_commands_change_next_request_and_persist_cold(self):
        self.provider.config_observations = []
        writes = []

        def send(rpc,worker,phrase):
            worker.chat(guid(),phrase)
            until = time.monotonic()+12
            while True:
                event = rpc.event(until)
                if event["method"]=="turn/completed":
                    self.assertEqual(event["params"]["turn"]["status"],"completed")
                    return

        with isolated_app_server(self.root,self.origin) as rpc:
            sid = rpc.request("thread/start",{"cwd":self.root,"ephemeral":False,"model":"gpt-6.2-sol"})["thread"]["id"]
            actual = rpc.request
            def native(method,params):
                result = actual(method,params)
                if method=="thread/settings/update":
                    writes.append(params)
                return result
            rpc.request = native
            host = AppServerHost(rpc,sid)
            host.session = sid
            loop = self.enterContext(Loop.isolated(host_factory=lambda _path,_name:host))
            service = WorkerService(loop.worker)
            token = guid()
            server = service.listen({token:"platform/oguri"},("127.0.0.1",0),None)
            url = self.enterContext(running(server))
            platform = Client(url,token)
            platform_feed = Views(loop.root/"settings-views.db",platform,PlatformFixture(),"fixture-chat","小栗帽")
            service.step()

            def command(mid,text):
                receipt = platform.request("POST","worker/command",{"message_id":mid,"text":text})["receipt"]
                card = next(row["event"]["text"] for row in platform.request("GET","worker/display")["items"]
                            if row["seq"]==receipt["output"]["seq"])
                return receipt,card

            def option(card,part):
                return next(line.split(".",1)[0] for line in card.splitlines() if f" {part} · " in line)

            send(rpc,loop.worker,"Observe the original Sol request")
            _,models = command("models","/model")
            _,efforts = command("efforts_original","/effort")
            astra = option(models,"test_wire/gpt-6.2-astra")
            high = option(efforts,"high")
            self.assertIn("尚未确认送达",command("early_selection","/model "+astra)[1])
            platform_feed.poll()
            changed,card = command("model_astra","/model "+astra)
            self.assertIn("已切换为 test_wire/gpt-6.2-astra",card)
            self.assertEqual(changed["model_receipt"]["selected"],
                             {"provider":"test_wire","model":"gpt-6.2-astra","effort":None})
            self.assertIn("打开菜单后模型已切换",command("old_effort","/effort "+high)[1])
            self.assertIn("该选项暂不可用",command("other_provider","/model another/gpt-6.2-sol")[1])
            _,fresh = command("efforts_current","/effort")
            platform_feed.poll()
            maximum,card = command("effort_max","/effort "+option(fresh,"max"))
            self.assertIn("已切换为 test_wire/gpt-6.2-astra · max",card)
            self.assertEqual(maximum["model_receipt"]["selected"]["effort"],"max")
            send(rpc,loop.worker,"Observe the new Astra and max request")
            effort,card = command("effort_ultra","/effort "+option(fresh,"ultra"))
            self.assertIn("已切换为 test_wire/gpt-6.2-astra · ultra",card)
            self.assertIn("实际模型请求配置暂无回读",card)
            self.assertEqual(effort["model_receipt"]["selected"]["effort"],"ultra")
            send(rpc,loop.worker,"Observe the new Astra and ultra request")

            actual_change = loop.adapter.model_change
            def lose(session,kind,value,**kw):
                self.assertEqual(actual_change(session,kind,value,**kw)["selected"]["effort"],"high")
                raise Fault(503,"isolated_model_ack_lost",True)
            loop.adapter.model_change = lose
            pending,card = command("effort_lost","/effort high")
            self.assertIn("切换结果待核",card)
            self.assertNotIn("model_receipt",pending)
            loop.adapter.model_change = actual_change
            low,card = command("effort_low","/effort low")
            self.assertEqual(low["model_receipt"]["selected"]["effort"],"low")
            self.assertIn("· low",card)
            self.assertEqual(command("effort_lost","/effort high")[0],pending)
            self.assertEqual(len(writes),5)

        with isolated_app_server(self.root,self.origin) as reopened:
            host = AppServerHost(reopened,sid)
            loop.adapter.host = host
            selected = host.model_view(sid,"models")["current"]
            self.assertEqual(selected,{"provider":"test_wire","model":"gpt-6.2-astra","effort":"low"})
            self.assertEqual(reopened.request("thread/resume",{"threadId":sid,"cwd":self.root})["thread"]["id"],sid)
            send(reopened,loop.worker,"Observe the selection after cold resume")
        sol,maximum,ultra,cold = self.provider.config_observations
        self.assertEqual((sol,maximum,cold),
                         (("gpt-6.2-sol","low"),("gpt-6.2-astra","max"),("gpt-6.2-astra","low")))
        self.assertEqual(ultra[0],"gpt-6.2-astra")
        self.assertIn(ultra[1],("xhigh","ultra"))
        self.assertEqual(writes,[{"threadId":sid,"model":"gpt-6.2-astra"},*({"threadId":sid,"effort":item}
                                  for item in ("max","ultra","high","low"))])
        self.assertFalse(loop.hermes.tasks()["items"])
        self.evidence = {"thread_id":sid,"native_writes":writes,"cold_choice":selected,"request_settings":self.provider.config_observations,
                         "stale_effort_menu_refused":True,"unconfirmed_attempt_did_not_replay":True,
                         "boundary":"Actual App Server per-thread selection and local model requests; platform fixture"}
