"""Opt-in macOS Codex adapter and Linux VM Worker rehearsal over private TLS."""
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import ssl
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from urllib.error import URLError

from .adapter import HostAdapter
from .agent_tools import REPORT_TOOL
from .codex_app_server import AppServerHost, worker_callbacks
from .devloop import FixtureHost
from .hermes import HermesAdapter
from .host_wire import serve_adapter
from .orders import Orders
from .release_check import config
from .storage import Fault, encode, guid
from .test_codex_app_server import CODEX, LINK_OUTAGE_FIXTURE, Provider, isolated_app_server, running
from .test_mainline import Clock
from .wire import Client, serve_router


@contextmanager
def guest_worker(docker,image,state,session,port=0):
    name = "mowulink-test-worker-"+guid()
    repo = Path(__file__).resolve().parent.parent
    process = subprocess.Popen([docker,"run","--rm","--pull","never","--platform","linux/arm64",
        "--network","host","--cap-drop","ALL","--user",f"{os.getuid()}:{os.getgid()}","--name",name,
        "-v",f"{repo}:/repo:ro","-v",f"{state}:/state","-w","/repo",image,
        "python3","-B","-m","rewrite.service","worker","--worker-id","oguri","--adapter-session",session,
        "--router-connection","/state/to-router.json","--adapter-connection","/state/to-adapter.json",
        "--db","/state/worker.db","--identities","/state/roles.json","--cert","/state/test.crt",
        "--key","/state/test.key","--port",str(port)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        lines = Queue()
        Thread(target=lambda:lines.put(process.stdout.readline()),daemon=True).start()
        line = lines.get(timeout=15)
        if not line:
            raise RuntimeError("isolated VM Worker failed to start: "+process.stderr.read()[-900:])
        ready = json.loads(line)
        if ready["component"]!="worker" or ready["tls"] is not True:
            raise RuntimeError("unexpected guest process")
        container = subprocess.check_output([docker,"inspect","--format","{{.Id}}",name],text=True,timeout=6).strip()
        yield ready["port"],container
    finally:
        if process.poll() is None:
            subprocess.run([docker,"stop","--time","3",name],check=False,capture_output=True,timeout=8)
        try:
            process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            subprocess.run([docker,"rm","--force",name],check=False,capture_output=True,timeout=8)
            process.kill()
            process.communicate(timeout=8)
            raise


def eventual(read,timeout=14):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            result = read()
            if result:
                return result
        except OSError:
            pass
        time.sleep(.04)
    raise AssertionError("timed out waiting for the isolated Worker or original native turn")


@unittest.skipUnless(CODEX.is_file() and os.environ.get("MOWULINK_CODEX_CROSS_GUEST_IMAGE") and shutil.which("openssl"),
                     "opt-in Linux VM image and bundled macOS App Server required")
class CrossGuest(unittest.TestCase):
    def test_linux_worker_restarts_without_resending_the_mac_native_order(self):
        image = os.environ["MOWULINK_CODEX_CROSS_GUEST_IMAGE"]
        docker = os.environ.get("MOWULINK_DSH_TEST_DOCKER") or shutil.which("docker")
        self.assertIsNotNone(docker,"local Docker executable is required")
        shared = Path(os.environ.get("MOWULINK_CODEX_CROSS_GUEST_SHARED",str(Path.home())))
        host = os.environ.get("MOWULINK_CODEX_CROSS_GUEST_HOST","host.lima.internal")
        self.assertRegex(host,r"^[a-zA-Z0-9][a-zA-Z0-9.-]+$")
        root = Path(self.enterContext(TemporaryDirectory(prefix="mowulink-cross-guest-")))
        state = Path(self.enterContext(TemporaryDirectory(dir=shared,prefix=".mowulink-guest-worker-")))
        cert,key = state/"test.crt",state/"test.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
            "-addext",f"subjectAltName=IP:127.0.0.1,DNS:{host}","-keyout",key,"-out",cert],
            check=True,capture_output=True,timeout=10)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(cert,key)
        provider = ThreadingHTTPServer(("127.0.0.1",0),Provider)
        provider.observed,provider.media = [],[]
        fixture = provider.link_outage = {"first":Queue(),"retry":Queue(),"resume":Event()}
        rpc = self.enterContext(isolated_app_server(root,self.enterContext(running(provider))))
        self.assertIsNone(rpc.request("account/read",{"refreshToken":False})["account"])
        started = rpc.request("thread/start",{"cwd":str(root),"ephemeral":False,"sandbox":"workspace-write",
            "approvalPolicy":"never","dynamicTools":[REPORT_TOOL]})["thread"]
        session = started["id"]
        native = AppServerHost(rpc,session)
        clock = Clock()
        orders = Orders(root/"router.db",clock,poll_seconds=1)
        worker_key,manager_key,adapter_key,callback_key,ops_key,platform_key = (guid() for _ in range(6))
        router = serve_router(orders,{worker_key:"worker/oguri",manager_key:"hermes"},("127.0.0.1",0),tls)
        self.enterContext(running(router))
        router_url = f"https://127.0.0.1:{router.server_port}"
        manager_host = FixtureHost(root/"manager-native.db","test-manager")
        manager = HermesAdapter(root/"manager.db",Client(router_url,manager_key,cert),
            HostAdapter(root/"manager-host.db",manager_host,manager_host.session),clock)
        adapter = HostAdapter(root/"codex.db",native,session)
        adapter_http = serve_adapter(adapter,"oguri",{adapter_key:"worker/oguri"},("127.0.0.1",0),tls)
        self.enterContext(running(adapter_http))
        config(state,"to-router.json",{"url":f"https://{host}:{router.server_port}","token":worker_key,"ca_file":"/state/test.crt"})
        config(state,"to-adapter.json",{"url":f"https://{host}:{adapter_http.server_port}","token":adapter_key,"ca_file":"/state/test.crt"})
        config(state,"roles.json",{callback_key:"adapter/oguri",ops_key:"ops/oguri",platform_key:"platform/oguri"})
        terminal = Queue()

        with guest_worker(docker,image,state,session) as (port,first_instance):
            endpoint = f"https://127.0.0.1:{port}"
            callback,ops = Client(endpoint,callback_key,cert),Client(endpoint,ops_key,cert)
            platform = Client(endpoint,platform_key,cert)
            def status():
                return ops.request("GET","worker/status")

            def bound_status():
                observed = status()
                if observed["host_session"]==session and observed["router_connected"]:
                    return observed
                return None

            initial = eventual(bound_status)
            eventual(lambda:any(row["online"] and row["busy"] is False
                                  for row in manager.workers(worker_id="oguri")["items"]))
            with self.assertRaises(URLError):
                Client(endpoint,ops_key).request("GET","worker/status")
            with self.assertRaises(Fault) as unauthorized:
                platform.request("GET","worker/status")
            self.assertEqual(unauthorized.exception.status,403)
            self.enterContext(worker_callbacks(native,callback,lambda:None,
                                               notify_terminal=lambda turn,state:terminal.put((turn,state))))
            self.addCleanup(fixture["resume"].set)
            self.addCleanup((root/".link-continue").touch)
            tid = manager.create(guid(),"Cross-VM native task",LINK_OUTAGE_FIXTURE,"oguri")["task_id"]
            clock.advance(10)
            eventual((root/".link-started").exists)
            order = manager.inspect(tid)["task"]
            aid = order["assignment_id"]
            self.assertEqual(status()["held_assignment_id"],aid)
            router_client = Client(router_url,worker_key,cert)
            dispatch, = [frame for frame in router_client.frames(0) if frame["kind"]=="task.dispatch"]
            payload = {"kind":"task",**dispatch["payload"]}
            receipt = adapter.lookup_delivery(dispatch["delivery_id"],payload,session=session)
            self.assertEqual(receipt["session"],session)

        (root/".link-continue").touch()
        first_response = fixture["first"].get(timeout=12)
        self.assertEqual(first_response,"worker_tool_unconfirmed")
        self.assertEqual((root/".link-result").read_text(),"continued")
        self.assertTrue(native.busy(session))
        self.assertEqual(manager.inspect(tid)["task"],order)
        self.assertIsNone(manager.inspect(tid)["last_report"])
        with guest_worker(docker,image,state,session,port) as (new_port,restarted_instance):
            self.assertEqual(new_port,port)
            self.assertNotEqual(restarted_instance,first_instance)
            reconnected = eventual(bound_status)
            self.assertEqual(reconnected["held_assignment_id"],aid)
            self.assertIs(reconnected["host_busy"],True)
            fixture["resume"].set()
            result = json.loads(fixture["retry"].get(timeout=12))
            self.assertEqual(result,{"recorded":True,"release_assignment_id":aid,"task_state":"completed_reported"})
            self.assertEqual(terminal.get(timeout=12),(receipt["turn_id"],"completed"))
            after = status()
            self.assertIsNone(after["held_assignment_id"])
            turn, = native.thread(session,include_turns=True)["turns"]
            input_ids = [item["id"] for item in turn["items"] if item["type"]=="userMessage"]
            self.assertEqual(input_ids,[receipt["native_id"]])
            call_ids = [item["id"] for item in turn["items"] if item["type"]=="dynamicToolCall"]
            self.assertEqual(call_ids,["link-report-first","link-report-retry"])
            detail = manager.inspect(tid)
            self.assertEqual(detail["task"]["state"],"completed_reported")
            self.assertEqual(detail["last_report"]["assignment_id"],aid)
            notice, = manager.messages(source="oguri")["items"]
            self.assertEqual(notice["task_id"],tid)
        if output := os.environ.get("MOWULINK_CODEX_CROSS_GUEST_EVIDENCE"):
            Path(output).write_text(encode({"observed_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                "boundary":"Native App Server on macOS, real Worker process in a Linux VM, TLS on both directions."
                " Model is a local fixture; no Desktop identity or cloud platform.","codex_version":started["cliVersion"],
                "task_id":tid,"assignment_id":aid,"worker_container_ids":[first_instance,restarted_instance],
                "initial":initial,"reconnected":reconnected,"after":after,"native_receipt":receipt,
                "native_input_ids":input_ids,"native_report_call_ids":call_ids,"first_response":first_response,
                "w04":result,"notify_id":notice["message_id"],"command_result":"continued"})+"\n",encoding="utf-8")
