"""Run Desktop's bundled native App Server against a scripted loopback model."""
import base64
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest

from .agent_tools import REPORT_TOOL
from .codex_app_server import AppServerHost
from .devloop import Loop
from .feishu_inbound import Inbound
from .feishu_output import Views
from .native_files import NativeFiles
from .storage import Fault, encode, guid
from .test_feishu_output import PlatformFixture
from .test_feishu_inbound import PNG
from .test_mainline import Clock
from .wire import Client
from .worker_service import WorkerService

CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")


class Stdio:
    def __init__(self, process):
        self.process = process
        self.counter = 0
        self.incoming, self.events = Queue(),deque()
        self.reader = Thread(target=self.read,daemon=True)
        self.reader.start()

    def read(self):
        for line in self.process.stdout:
            self.incoming.put(json.loads(line))
        self.incoming.put(None)

    def send(self, packet):
        self.process.stdin.write(encode(packet)+"\n")
        self.process.stdin.flush()

    def receive(self, deadline):
        packet = self.incoming.get(timeout=max(0,deadline-time.monotonic()))
        if packet is None:
            raise RuntimeError("isolated App Server closed its transport")
        return packet

    def request(self,method,params):
        self.counter += 1
        self.send({"method":method,"params":params,"id":self.counter})
        until = time.monotonic()+12
        while True:
            packet = self.receive(until)
            if "method" not in packet and packet.get("id")==self.counter:
                if "error" in packet:
                    error = packet["error"]
                    if (method=="thread/read" and params["includeTurns"] and error["code"]==-32601
                            and error["message"]=="list_turns is not supported yet"):
                        raise Fault(503,"native_history_unconfirmed",True)
                    raise RuntimeError("App Server "+method+" error "+encode(error))
                return packet["result"]
            self.events.append(packet)

    def event(self,deadline):
        return self.events.popleft() if self.events else self.receive(deadline)


class Provider(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def do_POST(self):
        if self.path!="/v1/responses":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        items = body["input"]
        latest = next(item for item in reversed(items) if item.get("role")=="user")["content"]
        pictures = [part for part in latest if part.get("type")=="input_image"]
        if pictures:
            captions = [part["text"] for part in latest if part.get("type")=="input_text"
                        and not part["text"].startswith("<image name=[Image #") and part["text"]!="</image>"]
            images = [hashlib.sha256(base64.b64decode(part["image_url"].split(",",1)[1],validate=True)).hexdigest()
                      for part in pictures]
            self.server.media.append({"caption":captions,"image_sha256":images})
        markers = [block["text"] for item in items if item.get("role")=="user"
                   for block in item.get("content",[]) if block.get("text","").startswith("魔物链接投递\n")]
        payload = json.loads(markers[-1].split("\n",1)[1])["payload"] if markers else None
        calls = sum(item.get("type")=="function_call_output" for item in items)
        names = [tool.get("name") for tool in body.get("tools",[])]
        self.server.observed.append(("worker_report" in names,calls))
        response_id = "resp_fixture_"+str(len(self.server.observed))
        if payload and calls<2:
            arguments = {"task_id":payload["task_id"],"assignment_id":payload["assignment_id"],
                         "summary":"Isolated App Server result"}
            item = {"type":"function_call","id":"fc_fixture_"+str(calls),"name":"worker_report",
                    "call_id":"native_fixture_"+str(calls),"arguments":encode(arguments)}
        else:
            item = {"id":"msg_fixture","type":"message","role":"assistant","status":"completed",
                    "content":[{"type":"output_text","text":"Local model fixture accepted the receipt."}]}
        stream = [
            {"type":"response.created","response":{"id":response_id}},
            {"type":"response.output_item.added","output_index":0,"item":item},
            {"type":"response.output_item.done","output_index":0,"item":item},
            {"type":"response.completed","response":{"id":response_id,
             "usage":{"input_tokens":11,"output_tokens":5,"total_tokens":16}}},
        ]
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
def isolated_app_server(root, url):
    settings = [
        'model="gpt-5.4"', 'model_provider="test_wire"', 'model_providers.test_wire.name="Local test wire"',
        'model_providers.test_wire.base_url='+encode(url+"/v1"), 'model_providers.test_wire.wire_api="responses"',
        'model_providers.test_wire.request_max_retries=0','model_providers.test_wire.stream_max_retries=0',
        'web_search="disabled"',
    ]
    argv = [CODEX,"app-server","--stdio",*(value for setting in settings for value in ("-c",setting))]
    with Path(root,"native.stderr").open("w+") as errors:
        with subprocess.Popen(argv,cwd=root,env={"CODEX_HOME":root,"PATH":os.defpath},
                              stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,text=True) as process:
            rpc = Stdio(process)
            try:
                rpc.request("initialize",{"clientInfo":{"name":"mowulink_local_check","title":"Isolated protocol test",
                    "version":"1.0"},"capabilities":{"experimentalApi":True}})
                rpc.send({"method":"initialized","params":{}})
                yield rpc
            finally:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
                rpc.reader.join(timeout=4)


@unittest.skipUnless(CODEX.is_file(),"bundled Codex App Server required")
class CodexAppServer(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(TemporaryDirectory(prefix="mowulink-codex-protocol-"))
        self.provider = ThreadingHTTPServer(("127.0.0.1",0),Provider)
        self.provider.observed = []
        self.provider.media = []
        self.origin = self.enterContext(running(self.provider))

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
                        host.report_call(dict(event,params={**event["params"],"threadId":guid()}),callback)
                    with self.assertRaisesRegex(Fault,"native_turn_unconfirmed"):
                        host.report_call(dict(event,params={**event["params"],"turnId":guid()}),callback)
                proof = host.report_call(event,callback)
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
