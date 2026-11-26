"""Independent real gateway, real native session, and a loopback scripted model."""
from contextlib import closing, contextmanager
from hashlib import sha256
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest

from .devloop import Loop
from .hermes_check import GATEWAY_ENTRY, GATEWAY_GUARD, tracked_runtime
from .release_check import config
from .storage import encode, guid
from .trigger import Trigger


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
    def __init__(self):
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
                    call = ("call_create_manager_task","tool_call",{"calls":[{"name":"router_create_task","arguments":{
                        "title":"隔离 Hermes 原生建单","instructions":"Fixture-only, remain unassigned"}}]})
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


@unittest.skipUnless(os.environ.get("MOWULINK_HERMES_TEST_PYTHON") and
                     os.environ.get("MOWULINK_HERMES_TEST_SOURCE") and os.name=="posix",
                     "independent Hermes test runtime with a private local socket required")
class HermesGateway(unittest.TestCase):
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

    def test_trigger_to_real_gateway_and_model_tool_with_cold_receipt_recovery(self):
        root = self.root = Path(self.enterContext(TemporaryDirectory(prefix="mlhgw-",dir="/tmp"))).resolve()
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
        source = Trigger(root/"trigger.db","mail")
        loop = self.enterContext(Loop.isolated(trigger_ids=[source.instance_id]))
        model = ScriptedModel()
        base_url = self.enterContext(model.serve())
        manager_db = root/"manager.db"
        options = {"url":loop.manager_wire.url,"token":loop.manager_wire.token,
                   "database":str(manager_db),"session_id":"awaiting-test-session"}
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

        original = b"An isolated trigger attachment.\n"
        path = root/"trigger.txt"
        path.write_bytes(original)
        message_ids = {}
        with self.gateway(env,python,entry,"native",port,key) as pid:
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
        with self.gateway(env,python,entry,"recovered",port,key) as new_pid:
            self.assertNotEqual(pid,new_pid)
            again = self.wait(lambda:receipt(message_ids["Event"]))
            self.assertEqual(again,first)
            self.assertEqual(self.wait(native_inputs),[event_input,notify_input])
            self.assertEqual(len(model.requests),5)
            self.assertEqual(loop.hermes.tasks(unassigned="true")["items"],[created_task])

        if output := os.environ.get("MOWULINK_HERMES_GATEWAY_EVIDENCE"):
            evidence = {"hermes_source_revision":revision,"session_id":session,"route":route["session_key"],
                "native_pids":[pid,new_pid],"message_ids":message_ids,"event_native_admission":first["native_id"],
                "notify_native_admission":second["native_id"],"notify_after_event_release_seconds":
                round(notify["time"]-released,2),"info_native_inputs":0,"native_admission_count":2,
                "model_http_calls":5,"tool_discovery":"native tool_describe and tool_call","native_tool_calls":native_calls,
                "router_task":{"task_id":created_task["task_id"],"state":created_task["state"],
                               "unassigned_task_count_after_restart":1},
                "original_attachment_sha256":sha256(original).hexdigest(),"cold_recovery_reused_admission":True,
                "boundary":"Real Router, Trigger, Hermes gateway, private LOCAL session, tool execution and ledger. "
                           "Scripted OpenAI-compatible localhost model. No cloud credentials or Feishu."}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")
