"""Independent real gateway, real native session, and a loopback scripted model."""
from contextlib import closing, contextmanager
from hashlib import sha256
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from .devloop import FixtureHost, Loop, agent_report
from .adapter import HostAdapter
from .bundle_release_check import private_reply_id, publish_reply
from .dsh_display import DshDisplay
from .dsh_local_link import LocalLink
from .dsh_port import DshHost, tool_listener
from .feishu_inbound import Inbound
from .feishu_output import Views
from .hermes_check import GATEWAY_ENTRY, GATEWAY_GUARD, tracked_runtime
from .hermes_host import HermesLocalHost, input_text
from .orders import Orders
from .release_check import config
from .storage import Fault, encode, guid
from .test_dsh_native_loop import NativeProcess
from .test_dsh_sdk import APP as DSH_APP, EXE as DSH_EXE
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .test_state_bundle import running
from .trigger import Trigger
from .wire import Client, serve_router
from .worker import Worker
from .worker_service import WorkerService


PACKAGE = Path(__file__).resolve().parent.parent
CREATE_SESSION = GATEWAY_GUARD + """from hermes_cli.gateway_runtime import discover_gateway_endpoint
from hermes_cli.gateway_client import connect_gateway

async def main():
    home=Path(os.environ["HERMES_HOME"])
    async with asyncio.timeout(15):
        while True:
            owner=await asyncio.to_thread(discover_gateway_endpoint,home)
            if owner.state=="ready" and owner.endpoint is not None:
                break
            if owner.state not in {"starting","absent"}:
                raise RuntimeError("isolated gateway discovery: "+str(owner))
            await asyncio.sleep(.1)
    async with connect_gateway(owner.endpoint) as cli:
        result=await cli.rpc("session.create",source="cli",request_id=os.environ["TEST_SESSION_REQUEST"],
                             toolsets=["mowulink_manager"],title="MowuLink isolated gateway test")
        session=result["stored_session_id"]
        reply=await cli.rpc("session.detach",session_id=session,subscription_id=result["subscription_id"])
        assert reply["detached"]
        print(json.dumps({"session":session,"model":result["info"]["model"]}))

asyncio.run(main())
"""


class ScriptedModel:
    def __init__(self, assignee=None):
        self.assignee = assignee
        self.requests = []
        self.release_event = Event()
        self.event_waiting = Event()

    @contextmanager
    def serve(self):
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass

            def send_json(self,payload):
                body = encode(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path!="/v1/models":
                    return self.send_error(404)
                self.send_json({"object":"list","data":[{"id":"mowulink-fixture","object":"model",
                                    "context_length":131072,"owned_by":"isolated-test"}]})

            def do_POST(self):
                if self.path!="/v1/chat/completions":
                    return self.send_error(404)
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                user = [m["content"] for m in request["messages"] if m["role"]=="user"][-1]
                _, marker, data = user.partition("魔物链接消息\n") if isinstance(user,str) else ("","","")
                if not marker:
                    return self.send_error(422,"Expected an isolated MowuLink input")
                body = json.JSONDecoder().raw_decode(data)[0]
                tool_result = request["messages"][-1] if request["messages"][-1]["role"]=="tool" else None
                model.requests.append({"input":body,"time":time.monotonic(),"tool_result":tool_result,
                                       "tools":[tool["function"]["name"] for tool in request.get("tools",[])]})
                message = {"role":"assistant","content":"隔离网关已处理 "+body["message"]["level"]}
                finish = "stop"
                if body["message"]["level"]=="Event" and tool_result is None:
                    model.event_waiting.set()
                    if not model.release_event.wait(30):
                        return self.send_error(503,"Event test release timed out")
                    call = ("call_describe_router_tools","tool_describe",
                            {"names":["router_info","router_create_task"]})
                elif tool_result and tool_result.get("tool_call_id")=="call_describe_router_tools":
                    file, = body["message"]["attachments"]
                    call = ("call_read_trigger_original","tool_call",{"calls":[{"name":"router_info","arguments":{
                        "message_id":body["message_id"],"attachment_id":file["attachment_id"]}}]})
                elif tool_result and tool_result.get("tool_call_id")=="call_read_trigger_original":
                    assignment = {"assignee_id":model.assignee} if model.assignee else {}
                    instructions = "Run the isolated cutover task" if model.assignee else "Fixture-only, remain unassigned"
                    call = ("call_create_manager_task","tool_call",{"calls":[{"name":"router_create_task","arguments":{
                        "title":"隔离 Hermes 原生建单","instructions":instructions,**assignment}}]})
                elif model.assignee and body["message"].get("task_id") and not tool_result:
                    call = ("describe_review_tools","tool_describe",
                            {"names":["router_tasks","router_act_on_task"]})
                elif tool_result and tool_result.get("tool_call_id")=="describe_review_tools":
                    call = ("read_reported_task","tool_call",{"calls":[{"name":"router_tasks","arguments":{
                        "task_id":body["message"]["task_id"]}}]})
                elif tool_result and tool_result.get("tool_call_id")=="read_reported_task":
                    task = json.loads(tool_result["content"])["task"]
                    call = ("accept_reported_task","tool_call",{"calls":[{"name":"router_act_on_task","arguments":{
                        "task_id":task["task_id"],"expected_revision":task["revision"],"action":"close"}}]})
                else:
                    call = None
                if call:
                    call_id,name,arguments = call
                    message = {"role":"assistant","content":None,"tool_calls":[{"id":call_id,
                        "type":"function","function":{"name":name,"arguments":encode(arguments)}}]}
                    finish = "tool_calls"
                identity = {"id":"chatcmpl-"+guid(),"model":"mowulink-fixture","created":int(time.time())}
                if not request.get("stream"):
                    self.send_json({**identity,"object":"chat.completion",
                        "choices":[{"index":0,"message":message,"finish_reason":finish}],
                        "usage":{"prompt_tokens":150,"completion_tokens":15,"total_tokens":165}})
                    return
                self.send_response(200)
                self.send_header("Content-Type","text/event-stream")
                self.end_headers()
                if finish=="tool_calls":
                    message["tool_calls"][0]["index"] = 0
                for delta,reason in ((message,None),({},finish)):
                    chunk = {**identity,"object":"chat.completion.chunk",
                             "choices":[{"index":0,"delta":delta,"finish_reason":reason}]}
                    self.wfile.write(b"data: "+encode(chunk).encode()+b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")

        with ThreadingHTTPServer(("127.0.0.1",0),Handler) as server:
            runner = Thread(target=server.serve_forever,kwargs={"poll_interval":.05})
            runner.start()
            try:
                yield "http://127.0.0.1:"+str(server.server_port)+"/v1"
            finally:
                self.release_event.set()
                server.shutdown()
                runner.join()


def rows(database,statement,*args):
    with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(statement,args)]


class HermesAdmissionProof(unittest.TestCase):
    def test_authorized_native_message_with_same_id_and_other_payload_blocks_enqueue(self):
        context = SimpleNamespace(plugin_id="mowulink_manager",inject_message=Mock())
        session,route,mid = "session","test-local-route",guid()
        host = HermesLocalHost(context,session,route)
        producer = encode(["plugin",context.plugin_id,route,"a"*32])
        row = {"principal_id":"automation:"+route,"request_id":producer,"admission_id":"previous-admission",
               "payload":{"text":input_text(session,mid,{"text":"original"},"original-digest"),
                          "local_automation_v1":{"identity":producer,"owner":session,"route":route,"target":session}}}
        runtime = SimpleNamespace(list_session_admissions=lambda *_args,**_kwargs:[row])
        with patch.dict(sys.modules,{"hermes_state_runtime":runtime}), patch.object(host,"binding") as binding:
            binding.return_value = SimpleNamespace(db=object())
            with self.assertRaisesRegex(Fault,"hermes_input_conflict"):
                host.deliver(session,mid,{"text":"changed"},"changed-digest")
        context.inject_message.assert_not_called()


@unittest.skipUnless(os.environ.get("MOWULINK_HERMES_TEST_PYTHON") and
                     os.environ.get("MOWULINK_HERMES_TEST_SOURCE") and os.name=="posix",
                     "independent Hermes test runtime with a private local socket required")
class HermesGateway(NativeProcess):
    def wait(self, predicate, *, timeout=22):
        cutoff = time.monotonic()+timeout
        while time.monotonic()<cutoff:
            if result := predicate():
                return result
            time.sleep(.05)
        recent_logs = "\n".join(p.read_text(errors="replace")[-3800:] for p in self.root.glob("gateway-*.log"))
        self.fail(f"Timed out waiting for {predicate.__name__}. Scratch gateway logs:\n{recent_logs}")

    @contextmanager
    def gateway(self, environment, python, entry, name, port, key):
        with (self.root/f"gateway-{name}.log").open("w") as log:
            process = subprocess.Popen([python,"-B",entry],env=environment,cwd=self.root,stdout=log,stderr=log)
            try:
                def readiness():
                    self.assertIsNone(process.poll(),(self.root/f"gateway-{name}.log").read_text()[-3800:])
                    try:
                        with closing(HTTPConnection("127.0.0.1",port,timeout=1)) as native:
                            native.request("GET","/health/detailed",headers={"Authorization":"Bearer "+key})
                            response = native.getresponse()
                            health = json.load(response)
                    except (OSError,ValueError):
                        return None
                    if response.status==200 and health["pid"]==process.pid and health["gateway_state"]=="running":
                        return health
                self.wait(readiness)
                yield process.pid
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    def prepare_gateway(self, model, router, manager_db):
        root = self.root
        python = Path(os.environ["MOWULINK_HERMES_TEST_PYTHON"]).absolute()
        revision = tracked_runtime(Path(os.environ["MOWULINK_HERMES_TEST_SOURCE"]),root/"source")
        profile = root/"profile"
        profile.mkdir(mode=0o700)
        shutil.copytree(PACKAGE/"rewrite/integrations/hermes_manager",profile/"plugins/mowulink_manager",
                        ignore=shutil.ignore_patterns("__pycache__"))
        entry = root/"start.py"
        entry.write_text(GATEWAY_ENTRY)
        key = guid()+guid()
        config(root,"gateway-auth.json",{"token":key})
        with closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1",0))
            port = probe.getsockname()[1]
        env = {"PATH":os.defpath,"HOME":str(root),"HERMES_HOME":str(profile),"TMPDIR":str(root),
               "HERMES_GATEWAY_LOCK_DIR":str(root/"locks"),"XDG_CACHE_HOME":str(root/"cache"),
               "XDG_STATE_HOME":str(root/"state"),"PYTHONDONTWRITEBYTECODE":"1","PYTHONNOUSERSITE":"1",
               "PYTHONPATH":os.pathsep.join((str(PACKAGE),str(root/"source"))),"TEST_SESSION_REQUEST":guid()}
        base_url = self.enterContext(model.serve())
        options = {"url":router.url,"token":router.token,
                   "database":str(manager_db),"session_id":"awaiting-test-session"}
        if router.ca_file:
            options["ca_file"] = str(router.ca_file)
        private_file = config(root,"connection.json",options)
        config(profile,"config.yaml",{"gateway":{"api_server":{"enabled":True,"host":"127.0.0.1","port":port}},
            "model":{"provider":"custom","default":"mowulink-fixture","base_url":base_url,"api_key":"fixture"},
            "plugins":{"enabled":["mowulink_manager"],"entries":{"mowulink_manager":{
                "settings":{"connection_file":str(private_file)},"allow_gateway_injection":True}}}})

        native_db = profile/"state.db"
        with self.gateway(env,python,entry,"bootstrap",port,key):
            result = subprocess.run([python,"-B","-c",CREATE_SESSION],cwd=root,env=env,capture_output=True,
                                    text=True,timeout=23)
            self.assertEqual(result.returncode,0,result.stderr+(root/"gateway-bootstrap.log").read_text()[-4200:])
            created = json.loads(result.stdout.splitlines()[-1])
        session = created["session"]
        self.assertEqual(created["model"],"mowulink-fixture")
        route, = rows(native_db,"SELECT session_key,created_source FROM sessions WHERE id=?",session)
        self.assertEqual(route["created_source"],"cli")
        options.update(session_id=session,input_session_key=route["session_key"])
        private_file.write_text(encode(options))
        return revision,session,route["session_key"],lambda name:self.gateway(env,python,entry,name,port,key)

    @unittest.skipUnless(shutil.which("openssl"),"loopback TLS certificate fixture unavailable")
    def test_rewrite_cold_start_trigger_to_native_manager_to_closed_order(self):
        self.exercise_cold_cutover(native_dsh=False)

    @unittest.skipUnless(DSH_EXE.is_file() and shutil.which("openssl"),"isolated macOS DSH SDK and TLS required")
    def test_rewrite_cold_start_hermes_and_dsh_native_tools(self):
        self.exercise_cold_cutover(native_dsh=True)

    def exercise_cold_cutover(self, *, native_dsh):
        root = self.root = Path(self.enterContext(TemporaryDirectory(prefix="mlhgw-",dir="/tmp"))).resolve()
        cert,key = root/"cert.pem",root/"key.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-keyout",key,"-out",cert],cwd=root,
                       check=True,capture_output=True,timeout=15)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert,key)
        clock = Clock()
        source = Trigger(root/"source.db","clean-source",clock)
        router = Orders(root/"router.db",clock,poll_seconds=1)
        manager_key,source_key,worker_key,platform_key = (guid() for _ in range(4))
        roles = {manager_key:"hermes",source_key:"trigger/"+source.instance_id,worker_key:"worker/trial"}
        router_url = self.enterContext(running(serve_router(router,roles,("127.0.0.1",0),tls)))
        router_url = router_url.replace("http://","https://",1)
        manager = Client(router_url,manager_key,cert)
        source_link = Client(router_url,source_key,cert)
        worker_session = "clean-worker-session"
        if native_dsh:
            dsh_root = self.local_tls_directory()
            dsh_link = LocalLink(dsh_root,use_tls=True)
            host = DshHost(dsh_root,link=dsh_link)
        else:
            host = FixtureHost(root/"native-worker.db",worker_session)
        adapter = HostAdapter(root/"adapter.db",host,worker_session)
        worker = Worker(root/"worker.db","trial",Client(router_url,worker_key,cert),adapter,clock=clock)
        callback_key = guid()
        listener = WorkerService(worker).listen({platform_key:"platform/trial",callback_key:"adapter/trial"},
                                                ("127.0.0.1",0),tls)
        platform_url = self.enterContext(running(listener)).replace("http://","https://",1)
        platform_link = Client(platform_url,platform_key,cert)
        if native_dsh:
            callback = Client(platform_url,callback_key,cert)
            self.enterContext(tool_listener(dsh_root,adapter,callback,link=dsh_link))
            native = self.enterContext(self.start_native(dsh_root,worker_session,"formal",use_tls=True))
            relay = DshDisplay(adapter,callback,"trial")
            relay.collect()
            self.assertEqual(self.read_native(native,"read")["inputs"],[])
        else:
            self.assertEqual(host.entries(),[])
        feishu_db = root/"feishu.db"
        platform = PlatformFixture()
        inbound = Inbound(feishu_db,platform_link,None,"app","trainer","cutover-chat",clock=clock)
        views = Views(feishu_db,platform_link,platform,"cutover-chat","Trial Worker",clock=clock)
        manager_db = root/"manager.db"
        model = ScriptedModel(assignee="trial")
        revision,session,_route,gateway = self.prepare_gateway(model,manager,manager_db)
        self.assertEqual(manager.request("GET","tasks")["items"],[])
        self.assertEqual(source.pending(),[])
        with gateway("clean-start") as gateway_pid:
            attachment = root/"original.txt"
            attachment.write_bytes(b"Fresh Trigger attachment for the cutover rehearsal.\n")
            mid = source.record("Event","Create one test order.",[attachment])
            t04 = source.send(source_link,mid)
            self.assertEqual(source.send(source_link,mid),t04)
            self.wait(model.event_waiting.is_set)
            model.release_event.set()
            [task] = self.wait(lambda:manager.request("GET","tasks")["items"])
            tid = task["task_id"]
            self.assertEqual(task["assignee_id"],"trial")
            self.assertEqual(task["state"],"ready")
            worker.connect()
            worker.poll()
            clock.advance(10)
            worker.poll()
            worker.poll()
            with adapter.db.transaction() as db:
                [first] = db.execute("SELECT payload,receipt FROM intents WHERE json_extract(payload,'$.kind')='task'")
                body,formal = json.loads(first["payload"]),json.loads(first["receipt"])
            aid = body["assignment_id"]
            self.assertEqual((body["task_id"],formal["session"]),(tid,worker_session))
            if native_dsh:
                formal_observation = self.read_native(native,"read")
                self.assertEqual(formal_observation["inputs"],[formal["native_id"]])
                calls,results = formal_observation["calls"],formal_observation["results"]
                self.assertEqual([c["name"] for c in calls],["worker_report","worker_report"])
                self.assertEqual([json.loads(c["arguments"])["assignment_id"] for c in calls],[aid,aid])
                self.assertEqual([r["toolCallId"] for r in results],[c["callId"] for c in calls])
                self.assertEqual([r["isError"] for r in results],[False,False],results)
                receipts = [json.loads(r["content"][0]["text"]) for r in results]
                self.assertEqual(receipts,[receipts[0]]*2)
                w04 = receipts[0]
                args,retry = (json.loads(c["arguments"]) for c in calls)
                self.assertEqual(retry,args)
            else:
                self.assertEqual(worker.hold()["held_assignment_id"],aid)
                args = {"task_id":tid,"assignment_id":aid,"summary":"Cutover fixture result."}

            chat_id = "om_cutover_private"
            mid_chat = inbound.record("app","user","trainer",{"chat_id":"cutover-chat","chat_type":"p2p",
                "message_id":chat_id,"message_type":"text","content":encode({"text":"Independent private chat."})})
            chat = inbound.deliver(mid_chat)
            self.assertEqual(inbound.deliver(mid_chat),chat)
            self.assertEqual(agent_report(worker,chat["native_id"],args)["content"][0]["text"],"wrong_report_origin")
            if native_dsh:
                end = self.read_native(native,"read")
                self.assertEqual(end["inputs"],[formal["native_id"],chat["native_id"]])
                self.assertEqual(end["calls"],calls)
                def private_reply_recorded():
                    relay.collect()
                    relay.flush()
                    views.poll()
                    with views.db.transaction() as db:
                        return db.execute("""SELECT 1 FROM chat_history_index WHERE direction='out'
                            AND session=? AND turn=?""",(chat["session"],chat["turn_id"])).fetchone()

                self.wait(private_reply_recorded)
                card = private_reply_id(views,chat_id,chat)
                self.assertEqual(views.poll(),0)
                [reply] = [item[2] for item in platform.messages.values() if item[0]==card]
                self.assertEqual(reply["elements"][0]["text"]["content"],"isolated fixture response")
            else:
                card = publish_reply(worker,views,platform,chat_id,chat,"One private reply.")
                worker.turn_finished(formal["native_id"],"The ordinary turn ended.")
                self.assertEqual(manager.request("GET","tasks/"+tid)["task"]["state"],"working")
                report = agent_report(worker,formal["native_id"],args)
                self.assertFalse(report["isError"],report)
                self.assertEqual(agent_report(worker,formal["native_id"],args),report)
                w04 = json.loads(report["content"][0]["text"])
                self.assertEqual([p["kind"] for p,_r in host.entries()],["task","chat"])
            self.assertEqual((w04["release_assignment_id"],w04["task_state"]),(aid,"completed_reported"))
            self.assertIsNone(worker.hold()["held_assignment_id"])
            [finished] = self.wait(lambda:[t for t in manager.request("GET","tasks")["items"]
                                          if t["state"]=="closed"],timeout=35)
            self.assertEqual(finished["task_id"],tid)
            self.assertEqual(manager.request("GET","tasks")["items"],[finished])
            native_db = root/"profile/state.db"
            tools = rows(native_db,"SELECT tool_name,tool_call_id FROM messages WHERE session_id=? AND role='tool' "
                         "AND tool_name IN ('router_info','router_create_task','router_tasks','router_act_on_task') ORDER BY id",
                         session)
            self.assertEqual([row["tool_call_id"] for row in tools],
                             ["call_read_trigger_original","call_create_manager_task","read_reported_task","accept_reported_task"])
            [notice] = manager.request("GET","messages?source=trial")["items"]
            self.assertEqual(notice["task_id"],tid)
            [review] = [call["tool_result"] for call in model.requests
                        if call["tool_result"] and call["tool_result"].get("tool_call_id")=="read_reported_task"]
            reviewed = json.loads(review["content"])
            self.assertEqual(reviewed["task"]["state"],"completed_reported")
            self.assertEqual((reviewed["last_report"]["assignment_id"],reviewed["last_report"]["summary"]),
                             (aid,args["summary"]))
            # The real manager created the order only after downloading the original Trigger attachment.
            [download] = [call["tool_result"] for call in model.requests
                          if call["tool_result"] and call["tool_result"].get("tool_call_id")=="call_read_trigger_original"]
            self.assertEqual(Path(json.loads(download["content"])["local_path"]).read_bytes(),attachment.read_bytes())

        if output := os.environ.get("MOWULINK_HERMES_COLD_CUTOVER_EVIDENCE"):
            evidence = {"hermes_revision":revision,"gateway_pid":gateway_pid,"manager_session":session,
                "source_instance":source.instance_id,"t04":t04,"task_id":tid,"assignment_id":aid,
                "source_attachment_sha256":sha256(attachment.read_bytes()).hexdigest(),
                "native_worker_receipt":formal,"w04":w04,"manager_notify_id":notice["message_id"],
                "manager_tools":tools,"final_state":finished["state"],"platform_reply_id":card,
                "private_input_id":chat["native_id"],"formal_worker_inputs":1,"private_worker_inputs":1,
                "boundary":"New stores; real local Hermes with a scripted model; real Router/Worker TLS and Worker MCP; "
                           "Worker host and Feishu provider are fixtures."}
            if native_dsh:
                with (DSH_APP/"Info.plist").open("rb") as bundle:
                    dsh_version = plistlib.load(bundle)["CFBundleShortVersionString"]
                evidence.update(dsh_version=dsh_version,worker_host="Installed DSH AgentLoop and native tool executor",
                    dsh_tool_calls=[{"call_id":c["callId"],"name":c["name"]} for c in calls],
                    dsh_w04_receipts=receipts,dsh_private_reply=reply["elements"][0]["text"]["content"],
                    boundary="Real local Hermes and native DSH AgentLoop with scripted models; real Router/Worker TLS "
                             "and DSH mutual TLS; Feishu provider is a fixture.")
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")

    def test_trigger_to_real_gateway_and_model_tool_with_cold_receipt_recovery(self):
        root = self.root = Path(self.enterContext(TemporaryDirectory(prefix="mlhgw-",dir="/tmp"))).resolve()
        source = Trigger(root/"trigger.db","mail")
        loop = self.enterContext(Loop.isolated(trigger_ids=[source.instance_id]))
        model = ScriptedModel()
        manager_db = root/"manager.db"
        revision,session,route,gateway = self.prepare_gateway(model,loop.manager_wire,manager_db)
        native_db = root/"profile/state.db"
        original = b"An isolated trigger attachment.\n"
        path = root/"trigger.txt"
        path.write_bytes(original)
        message_ids = {}
        with gateway("native") as pid:
            # Publish real Trigger records while only the isolated gateway can consume them.
            for level in ("Info","Event","Notify"):
                mid = message_ids[level] = source.record(level,"Gateway test "+level,[path] if level=="Event" else [])
                source.send(loop.trigger_clients[source.instance_id],mid)
            self.wait(model.event_waiting.is_set)
            self.assertEqual([r["input"]["message_id"] for r in model.requests],[message_ids["Event"]])
            time.sleep(10.2)
            self.assertEqual(len(model.requests),1)
            released = time.monotonic()
            model.release_event.set()

            def native_inputs():
                found = rows(native_db,"SELECT admission_id,status,outcome,payload_json FROM session_admissions "
                             "WHERE target_session_id=? ORDER BY seq",session)
                if len(found)==2 and all(row["status"]=="terminal" for row in found):
                    return found
            event_input, notify_input = self.wait(native_inputs,timeout=35)
            for record,level in ((event_input,"Event"),(notify_input,"Notify")):
                self.assertEqual(record["outcome"],"completed")
                native_body = json.loads(record["payload_json"])["text"].split("\n",1)[1]
                self.assertEqual(json.loads(native_body)["message_id"],message_ids[level])
            event_first, event_description, event_tool, creation, notify = model.requests
            self.assertEqual([r["input"]["message_id"] for r in model.requests],
                             [message_ids["Event"]]*4+[message_ids["Notify"]])
            self.assertTrue(all(r["input"]["session"]==session for r in model.requests))
            self.assertIn("tool_describe",event_first["tools"])
            self.assertIn("tool_call",event_first["tools"])
            description = json.loads(event_description["tool_result"]["content"])
            self.assertEqual(set(description["tools"]),{"router_info","router_create_task"})
            response = json.loads(event_tool["tool_result"]["content"])
            self.assertEqual(event_tool["tool_result"]["tool_call_id"],"call_read_trigger_original")
            self.assertEqual(sha256(Path(response["local_path"]).read_bytes()).hexdigest(),sha256(original).hexdigest())
            created_task = json.loads(creation["tool_result"]["content"])
            self.assertEqual(creation["tool_result"]["tool_call_id"],"call_create_manager_task")
            self.assertEqual(loop.hermes.tasks(unassigned="true")["items"],[created_task])
            observed_task = loop.hermes.inspect(created_task["task_id"])["task"]
            self.assertEqual(observed_task,created_task)
            self.assertEqual(observed_task["holder"],{"kind":"router"})
            self.assertIsNone(observed_task["assignment_id"])
            self.assertGreaterEqual(notify["time"]-released,10)

            def receipt(mid):
                found = rows(manager_db,"SELECT receipt FROM queue WHERE message_id=?",mid)
                return json.loads(found[0]["receipt"]) if found and found[0]["receipt"] else None
            self.assertFalse(rows(manager_db,"SELECT receipt FROM queue WHERE message_id=?",message_ids["Info"]))
            info, = loop.hermes.messages(message_id=message_ids["Info"])["items"]
            self.assertEqual(info["level"],"Info")
            native_calls = {row["tool_name"]:row for row in rows(native_db,
                "SELECT id,tool_name,tool_call_id FROM messages WHERE session_id=? AND role='tool' "
                "AND tool_name IN ('router_info','router_create_task')",session)}
            self.assertEqual(native_calls["router_info"]["tool_call_id"],"call_read_trigger_original")
            self.assertEqual(native_calls["router_create_task"]["tool_call_id"],"call_create_manager_task")
            first = self.wait(lambda:receipt(message_ids["Event"]))
            second = self.wait(lambda:receipt(message_ids["Notify"]))
            self.assertEqual(first["native_id"],event_input["admission_id"])
            self.assertEqual(second["native_id"],notify_input["admission_id"])
            self.assertIn("隔离网关已处理 Notify",[row["content"] for row in rows(native_db,
                "SELECT content FROM messages WHERE session_id=? AND role='assistant'",session)])

        # Keep the native ledger intact, but simulate a lost receipt on both sides of our adapter.
        with closing(sqlite3.connect(manager_db)) as conn, conn:
            conn.execute("UPDATE queue SET receipt=NULL WHERE message_id=?",(message_ids["Event"],))
        with closing(sqlite3.connect(str(manager_db)+".host")) as conn, conn:
            conn.execute("UPDATE intents SET receipt=NULL,native_id=NULL WHERE delivery_id=?",(message_ids["Event"],))
        with gateway("recovered") as new_pid:
            self.assertNotEqual(pid,new_pid)
            again = self.wait(lambda:receipt(message_ids["Event"]))
            self.assertEqual(again,first)
            self.assertEqual(self.wait(native_inputs),[event_input,notify_input])
            self.assertEqual(len(model.requests),5)
            self.assertEqual(loop.hermes.tasks(unassigned="true")["items"],[created_task])

        # Restore the two relay ledgers behind the original native admission.
        with closing(sqlite3.connect(manager_db)) as conn, conn:
            conn.execute("DELETE FROM queue WHERE message_id=?",(message_ids["Event"],))
            conn.execute("UPDATE progress SET cursor=0")
        with closing(sqlite3.connect(str(manager_db)+".host")) as conn, conn:
            conn.execute("DELETE FROM intents WHERE delivery_id=?",(message_ids["Event"],))
        with gateway("restored-before-event") as restored_pid:
            def recovered_or_duplicated():
                saved = receipt(message_ids["Event"])
                inputs = rows(native_db,"SELECT admission_id FROM session_admissions WHERE target_session_id=? ORDER BY seq",
                              session)
                if saved or len(inputs)>2:
                    return saved,len(inputs)
            recovered = self.wait(recovered_or_duplicated)
            self.assertEqual(recovered,(first,2))
            self.assertEqual(rows(native_db,"SELECT admission_id,status,outcome,payload_json FROM session_admissions "
                                  "WHERE target_session_id=? ORDER BY seq",session),[event_input,notify_input])
            self.assertEqual(len(model.requests),5)
            self.assertEqual(loop.hermes.tasks(unassigned="true")["items"],[created_task])

        if output := os.environ.get("MOWULINK_HERMES_GATEWAY_EVIDENCE"):
            evidence = {"hermes_source_revision":revision,"session_id":session,"route":route,
                "native_pids":[pid,new_pid,restored_pid],"message_ids":message_ids,"event_native_admission":first["native_id"],
                "notify_native_admission":second["native_id"],"notify_after_event_release_seconds":
                round(notify["time"]-released,2),"info_native_inputs":0,"native_admission_count":2,
                "model_http_calls":5,"tool_discovery":"native tool_describe and tool_call","native_tool_calls":native_calls,
                "router_task":{"task_id":created_task["task_id"],"state":created_task["state"],
                               "unassigned_task_count_after_restart":1},
                "original_attachment_sha256":sha256(original).hexdigest(),"cold_recovery_reused_admission":True,
                "missing_queue_and_intent_recovered":{"same_admission":recovered[0]["native_id"],
                                                      "native_admission_count":recovered[1],"extra_model_calls":0},
                "boundary":"Real Router, Trigger, Hermes gateway, private LOCAL session, tool execution and ledger. "
                           "Scripted OpenAI-compatible localhost model. No cloud credentials or Feishu."}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")
