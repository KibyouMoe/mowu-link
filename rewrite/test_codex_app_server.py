"""Run Desktop's bundled native App Server against a scripted loopback model."""
import base64
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
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
from .codex_app_server import AppServerHost, worker_callbacks
from .codex_display import CodexDisplay
from .codex_provision import SEED
from .codex_rpc import NativeRpcRejected, start_app_server
from .devloop import Loop
from .feishu_inbound import Inbound
from .feishu_output import Views
from .host_wire import RemoteAdapter
from .native_files import NativeFiles
from .question_cards import Actions
from .storage import Fault, encode, guid
from .test_feishu_output import PlatformFixture
from .test_feishu_inbound import PNG
from .test_mainline import Clock
from . import test_service
from .wire import Client
from .worker_service import WorkerService

CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
GOAL_FIXTURE = "Finish the isolated goal check"


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
        calls = sum(item.get("type")=="function_call_output" for item in items[user_index+1:])
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
        elif formal_goal is not None and (goal_continuation or payload and payload.get("kind")=="task"):
            item = self.formal_goal_item(formal_goal,payload,items[user_index+1:],names,goal_continuation)
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
            item = {"type":"function_call","id":"fc_trainer_"+trainer_prompt,"name":"request_user_input",
                    "call_id":"native_trainer_"+trainer_prompt,"arguments":encode({"questions":[native_q]})}
        elif peer and not calls:
            item = {"type":"function_call","id":"fc_peer","name":"worker_send_peer","call_id":"native_peer_fixture",
                    "arguments":peer.split("\n",1)[1]}
        elif question and not calls:
            arguments = {"text":"Use the first route?","options":[{"id":"go","label":"继续"}]}
            item = {"type":"function_call","id":"fc_question","name":"worker_ask","call_id":"native_question",
                    "arguments":encode(arguments)}
        elif payload and payload.get("kind") in ("task","task_update","task_probe","question_answer") and not question and calls<2:
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
                self.assertIn(receipt["state"],("running","completed"))
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
                ("skip",{"mode":"skip"},None,{})):
            turn,qid,mid,card = open_card(mode)
            action = {"question_id":qid,**value}
            with self.assertRaisesRegex(Fault,"wrong_card_actor"):
                actions.record("app","another","private-chat",mid,action,form)
            with self.assertRaisesRegex(Fault,"not_original_trainer_card"):
                actions.record("app","trainer","private-chat","om_wrong",action,form)
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
        secret = send("private")
        wait_for(lambda: any(name=="private" for name,_ in self.provider.trainer_responses))
        self.assertFalse([item for item in events() if item["turn_id"]==secret and item["kind"]=="question_open"])
        with self.assertRaisesRegex(Fault,"native_question_already_offered"):
            host.questions.offer({"id":900,"params":{"threadId":sid,"turnId":turn,
                "itemId":"native_trainer_unconfirmed","autoResolutionMs":None,
                "questions":[{"id":"route","question":"选择路线 · unconfirmed","isOther":True,"isSecret":False,
                              "options":[{"label":"第一项"},{"label":"第二项"}]}]}})
        self.assertFalse(loop.hermes.tasks()["items"])
        self.evidence = {"thread_id":sid,"answered":delivered,"unconfirmed_card_id":mid,"unconfirmed_untouched":True,
                         "secret_question_projected":False,
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
