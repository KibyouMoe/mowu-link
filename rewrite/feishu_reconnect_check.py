"""Verify the installed Feishu SDK against a local long-connection fixture."""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import json
import os
from pathlib import Path
from queue import Empty, Queue
from socket import SHUT_RDWR
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic

from .adapter import HostAdapter
from .devloop import FixtureHost
from .feishu_client import receive, start_long_connection
from .feishu_inbound import Inbound
from .storage import encode, guid
from .wire import Client
from .worker import Worker
from .worker_service import WorkerService


APP, SECRET, TRAINER, CHAT = "fixture-app","fixture-secret","fixture-trainer","fixture-chat"


def sdk_receiver(folder, port, mode):
    import lark_oapi as lark

    inbox = Inbound(Path(folder)/"inbox.db",None,None,APP,TRAINER,CHAT)
    dispatcher = (lark.EventDispatcherHandler.builder("","")
                  .register_p2_im_message_receive_v1(lambda event:receive(inbox,event)).build())
    options = {"domain":f"http://127.0.0.1:{int(port)}","log_level":lark.LogLevel.ERROR}
    if mode=="raw":
        lark.ws.Client(APP,SECRET,event_handler=dispatcher,**options).start()
    else:
        start_long_connection(APP,SECRET,dispatcher,discovery_seconds=.5,**options)


def event_frame(sequence, label):
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame

    message = {"schema":"2.0","header":{"event_id":"event-"+label,"event_type":"im.message.receive_v1",
               "app_id":APP},"event":{"sender":{"sender_type":"user","sender_id":{"open_id":TRAINER}},
               "message":{"message_id":"om_"+label,"chat_id":CHAT,"chat_type":"p2p",
                          "message_type":"text","content":encode({"text":"isolated "+label})}}}
    frame = Frame(SeqID=sequence,LogID=sequence,service=1,method=1,payload=encode(message).encode())
    for key,value in {"message_id":"wire-"+label,"trace_id":"trace-"+label,"sum":"1","seq":"0","type":"event"}.items():
        item = frame.headers.add()
        item.key,item.value = key,value
    return frame.SerializeToString()


@contextmanager
def endpoint(ws_port,stall_discovery):
    calls = []
    blocked,release = Event(),Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path!="/callback/ws/endpoint" or data!={"AppID":APP,"AppSecret":SECRET}:
                self.send_error(400)
                return
            number = len(calls)+1
            calls.append(monotonic())
            if stall_discovery and number==2:
                blocked.set()
                # Keep the socket open and send no headers or bytes.
                release.wait()
                return
            url = f"ws://127.0.0.1:{ws_port}/?device_id=fixture-{number}&service_id=1"
            body = encode({"code":0,"msg":"ok","data":{"URL":url,"ClientConfig":{
                "ReconnectCount":3,"ReconnectInterval":0,"ReconnectNonce":0,"PingInterval":60}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1",0),Handler) as http:
        runner = Thread(target=http.serve_forever,kwargs={"poll_interval":.05})
        runner.start()
        try:
            yield http.server_port,calls,blocked
        finally:
            release.set()
            http.shutdown()
            runner.join()


def deliver_to_worker(folder):
    root = Path(folder)
    native = FixtureHost(root/"native.db","isolated-feishu-host")
    adapter = HostAdapter(root/"adapter.db",native,native.session)
    worker = Worker(root/"worker.db","test",None,adapter)
    token = guid()
    with WorkerService(worker).listen({token:"platform/test"},("127.0.0.1",0),None) as server:
        runner = Thread(target=server.serve_forever,kwargs={"poll_interval":.05})
        runner.start()
        try:
            wire = Client("http://127.0.0.1:"+str(server.server_port),token)
            inbox = Inbound(root/"inbox.db",wire,None,APP,TRAINER,CHAT)
            first,second = inbox.pending()
            receipt = inbox.deliver(first)
            inbox.deliver(second)
            if inbox.deliver(first)!=receipt:
                raise RuntimeError("replayed event returned a different Worker receipt")
        finally:
            server.shutdown()
            runner.join()
    entries = native.entries()
    if [body["message_id"] for body,_receipt in entries]!=["om_before","om_after"]:
        raise RuntimeError("unexpected input count or message IDs at the Worker host")
    return len(entries)


def run(*,stall_discovery=False,raw_sdk=False):
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import serve
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame

    connections,acks = [],[]
    finished = Queue()
    def feishu(socket):
        connections.append(socket.request.path)
        number = len(connections)
        labels = ["before"] if number==1 else ["before","after"]
        try:
            for label in labels:
                sequence = len(acks)+1
                socket.send(event_frame(sequence,label))
                deadline = monotonic()+4
                while True:
                    reply = Frame.FromString(socket.recv(timeout=max(0,deadline-monotonic())))
                    if reply.method==1 and reply.SeqID==sequence:
                        break
                    if monotonic()>=deadline:
                        raise TimeoutError("SDK acknowledgement did not arrive")
                status = json.loads(reply.payload)["code"]
                if status!=200:
                    raise RuntimeError("SDK did not acknowledge the fixture event")
                acks.append((label,status))
            if number==1:
                # Drop the test TCP connection without sending a WebSocket close frame.
                socket.socket.shutdown(SHUT_RDWR)
            else:
                finished.put(None)
                # Let the client stay online while the parent inspects the durable inbox.
                try:
                    socket.recv(timeout=3)
                except (ConnectionClosed,TimeoutError):
                    pass
        except Exception as error:
            finished.put(error)

    with TemporaryDirectory(prefix="mowulink-feishu-reconnect-") as folder, serve(feishu,"127.0.0.1",0) as ws:
        runner = Thread(target=ws.serve_forever)
        runner.start()
        try:
            with endpoint(ws.socket.getsockname()[1],stall_discovery) as (port,calls,blocked):
                env = {"HOME":folder,"TMPDIR":folder,"PATH":os.defpath,"PYTHONNOUSERSITE":"1"}
                command = [sys.executable,"-m","rewrite.feishu_reconnect_check","--fixture-receiver",folder,str(port),
                           "raw" if raw_sdk else "bounded"]
                with subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as child:
                    stalled = False
                    try:
                        if raw_sdk and not blocked.wait(timeout=10):
                            raise RuntimeError("raw SDK did not attempt endpoint discovery again")
                        error = finished.get(timeout=3 if raw_sdk else 12)
                        if error:
                            raise error
                        if child.poll() is not None:
                            raise RuntimeError("SDK receiver exited during reconnect")
                        inbox = Inbound(Path(folder)/"inbox.db",None,None,APP,TRAINER,CHAT)
                        messages = inbox.pending()
                    except Empty:
                        stalled = True
                        if raw_sdk:
                            if child.poll() is not None or not blocked.is_set() or len(calls)!=2 or acks!=[("before",200)]:
                                raise RuntimeError("raw SDK stall did not match the expected transport state")
                            return {"sdk_version":version("lark-oapi"),"endpoint_requests":len(calls),
                                    "websocket_connections":len(connections),"event_acknowledgements":acks,
                                    "blocked_observation_seconds":3,"receiver_alive":True,
                                    "recovery_observed":False,"cloud_platform":False}
                    finally:
                        child.terminate()
                        _stdout,stderr = child.communicate(timeout=5)
                    if stalled:
                        raise RuntimeError("SDK reconnect did not finish: "+stderr.decode(errors="replace")[-600:])
                inputs = deliver_to_worker(folder)
        finally:
            ws.shutdown()
            runner.join()
    if raw_sdk:
        raise RuntimeError("raw SDK recovered while the rediscovery response was withheld")
    if len(calls)!=(3 if stall_discovery else 2) or len(connections)!=2 or acks!=[("before",200),("before",200),("after",200)]:
        raise RuntimeError("expected two SDK connections and three acknowledged deliveries")
    if messages!=["om_before","om_after"]:
        raise RuntimeError("expected only one durable input for the replayed message")
    result = {"sdk_version":version("lark-oapi"),"websocket_connections":len(connections),
            "endpoint_requests":len(calls),"event_acknowledgements":acks,"durable_messages":messages,
            "worker_native_inputs":inputs,"host":"fixture","cloud_platform":False,
            "disconnect":"TCP closed without a WebSocket close frame",
            "network":"actual loopback HTTP and WebSocket"}
    if stall_discovery:
        result["discovery_retry_seconds"] = round(calls[2]-calls[1],3)
        result["discovery_timeout_seconds"] = .5
        result["same_receiver_process"] = True
    return result


if __name__=="__main__":
    if len(sys.argv)==5 and sys.argv[1]=="--fixture-receiver":
        sdk_receiver(sys.argv[2],sys.argv[3],sys.argv[4])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--stall-discovery",action="store_true")
        parser.add_argument("--raw-sdk",action="store_true",help="measure the SDK with no discovery timeout")
        args = parser.parse_args()
        if args.raw_sdk and not args.stall_discovery:
            parser.error("--raw-sdk requires --stall-discovery")
        print(encode(run(stall_discovery=args.stall_discovery,raw_sdk=args.raw_sdk)))
