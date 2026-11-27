"""Opt-in native macOS adapter and Linux VM Worker rehearsal over private TLS."""
from contextlib import ExitStack, contextmanager
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from urllib.error import URLError

from .adapter import HostAdapter
from .agent_tools import REPORT_TOOL
from .codex_app_server import AppServerHost, worker_callbacks
from .devloop import FixtureHost
from .dsh_local_link import LocalLink
from .dsh_port import DshHost, tool_listener
from .hermes import HermesAdapter
from .host_wire import serve_adapter
from .orders import Orders
from .release_check import config
from .state_bundle import audit_handoff, audit_trigger, filename, restore, verify
from .storage import Fault, encode, guid, state_writer
from .test_codex_app_server import CODEX, LINK_OUTAGE_FIXTURE, Provider, isolated_app_server, running
from .test_dsh_native_loop import NativeProcess
from .test_dsh_sdk import EXE as DSH
from .test_mainline import Clock
from .trigger import Trigger
from .wire import Client, route, serve_authenticated, serve_router


@contextmanager
def guest_worker(docker,image,state,session,port=0,*,package=None):
    name = "mowulink-test-worker-"+guid()
    command = [docker] if isinstance(docker,str) else docker
    package = package or Path(__file__).resolve().parent.parent
    process = subprocess.Popen([*command,"run","--rm","--pull","never","--platform","linux/arm64",
        "--network","host","--cap-drop","ALL","--user",f"{os.getuid()}:{os.getgid()}","--name",name,
        "--read-only","--security-opt","no-new-privileges","--tmpfs","/tmp",
        "-v",f"{package}:/repo:ro","-v",f"{state}:/state","-w","/repo",image,
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
        container = subprocess.check_output([*command,"inspect","--format","{{.Id}}",name],text=True,timeout=6).strip()
        yield ready["port"],container
    finally:
        if process.poll() is None:
            subprocess.run([*command,"stop","--time","3",name],check=False,capture_output=True,timeout=8)
        try:
            process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            subprocess.run([*command,"rm","--force",name],check=False,capture_output=True,timeout=8)
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


def guest_tls(state,host):
    cert,key = state/"test.crt",state/"test.key"
    subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
        "-addext",f"subjectAltName=IP:127.0.0.1,DNS:{host}","-keyout",key,"-out",cert],
        check=True,capture_output=True,timeout=10)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert,key)
    return cert,tls


@unittest.skipUnless(sys.platform=="darwin" and os.environ.get("MOWULINK_CROSS_HOST_STATE_IMAGE")
                     and os.environ.get("MOWULINK_CROSS_HOST_STATE_SOCKET")
                     and all(shutil.which(command) for command in ("colima","docker","openssl")),
                     "isolated Colima Linux guest and pinned Worker image required")
class CrossHostState(unittest.TestCase):
    def test_offline_fragments_restore_original_cross_host_w04(self):
        image = os.environ["MOWULINK_CROSS_HOST_STATE_IMAGE"]
        socket = os.environ["MOWULINK_CROSS_HOST_STATE_SOCKET"]
        engine = [shutil.which("docker"),"--host","unix://"+socket]
        colima = [shutil.which("colima"),"ssh","--"]
        shared = Path(os.environ.get("MOWULINK_CODEX_CROSS_GUEST_SHARED",str(Path.home())))
        package = Path(__file__).resolve().parent.parent
        test_root = Path(self.enterContext(TemporaryDirectory(dir=shared,prefix=".mowulink-host-backup-"))).resolve()
        revision = os.environ.get("MOWULINK_TEST_RELEASE_REVISION","a"*40)

        def guest(*arguments,check=True):
            return subprocess.run([*colima,*map(str,arguments)],check=check,cwd=package,
                                  text=True,capture_output=True,timeout=20)

        guest_root = Path(guest("mktemp","-d","/tmp/mowulink-host-backup-XXXXXXXX").stdout.strip())
        self.assertRegex(str(guest_root),r"^/tmp/mowulink-host-backup-[A-Za-z0-9]+$")
        self.addCleanup(guest,"python3","-c","import shutil,sys; shutil.rmtree(sys.argv[1])",guest_root)
        fs = guest("findmnt","-n","-T",guest_root,"-o","FSTYPE").stdout.strip()
        self.assertEqual(fs,"ext4")
        original, recovered = guest_root/"original",guest_root/"restored"
        guest("mkdir","-m","700",original,recovered)
        exchange = test_root/"exchange"
        exchange.mkdir(mode=0o700)
        host = "host.lima.internal"
        cert,tls = guest_tls(test_root,host)
        clock = Clock()
        native = FixtureHost(test_root/"native-fixture.db","snapshot-test-session")
        manager_host = FixtureHost(test_root/"manager-fixture.db","snapshot-manager-session")
        local = {label:test_root/filename(label) for label in
                 ("router:main","agent:oguri","hermes:main","hermes-host:main","trigger:source")}
        Trigger(local["trigger:source"],"fixture",clock)
        worker_key,manager_key,adapter_key,callback_key,ops_key = (guid() for _ in range(5))
        first_reply_lost = Event()
        submissions = []
        batch = guid()

        @contextmanager
        def services(paths,*,lose_report=False):
            with ExitStack() as stack:
                for label in ("router:main","agent:oguri","hermes:main","hermes-host:main"):
                    stack.enter_context(state_writer(paths[label]))
                orders = Orders(paths["router:main"],clock,1)

                def router_request(actor,method,path,body,headers):
                    result = route(orders,actor,method,path,body,headers)
                    if method=="POST" and path.endswith("/report"):
                        submissions.append((body["assignment_id"],headers["Idempotency-Key"],result[2]))
                        if lose_report:
                            first_reply_lost.set()
                            raise ConnectionResetError("fixture dropped the committed W04 response")
                    return result

                server = serve_authenticated({worker_key:"worker/oguri",manager_key:"hermes"},
                    ("127.0.0.1",0),tls,router_request,orders.tick)
                stack.enter_context(running(server))
                router_client = Client(f"https://127.0.0.1:{server.server_port}",manager_key,cert)
                manager = HermesAdapter(paths["hermes:main"],router_client,
                    HostAdapter(paths["hermes-host:main"],manager_host,manager_host.session),clock)
                adapter = HostAdapter(paths["agent:oguri"],native,native.session)
                adapter_server = serve_adapter(adapter,"oguri",{adapter_key:"worker/oguri"},("127.0.0.1",0),tls)
                stack.enter_context(running(adapter_server))
                yield manager,server.server_port,adapter_server.server_port

        def configure(directory,folder,router_port,adapter_port):
            folder.mkdir(mode=0o700)
            config(folder,"to-router.json",{"url":f"https://{host}:{router_port}","token":worker_key,
                                            "ca_file":"/state/test.crt"})
            config(folder,"to-adapter.json",{"url":f"https://{host}:{adapter_port}","token":adapter_key,
                                             "ca_file":"/state/test.crt"})
            config(folder,"roles.json",{callback_key:"adapter/oguri",ops_key:"ops/oguri"})
            guest("cp","-p",*sorted(folder.iterdir()),cert,test_root/"test.key",directory)

        # Drive the fixture callback and ops probe from the guest loopback.
        # The Worker itself connects directly to the Mac Router and adapter.
        # This avoids racing the VM's automatic localhost port forwarding.
        probe = """import json,sys
from pathlib import Path
from rewrite.storage import Fault,encode
from rewrite.wire import Client
directory,port,role,method,path,raw = sys.argv[1:]
root = Path(directory)
roles = json.loads((root/'roles.json').read_text())
token, = [token for token,actor in roles.items() if actor==role+'/oguri']
try:
    reply = Client('https://127.0.0.1:'+port,token,root/'test.crt').request(method,path,json.loads(raw))
except Fault as fault:
    print(encode({'error':fault.code,'status':fault.status}))
else:
    print(encode({'result':reply}))
"""

        def live_request(directory,port,role,method,path,body=None):
            result = json.loads(guest("python3","-B","-c",probe,directory,port,role,method,path,encode(body)).stdout)
            if "error" in result:
                raise Fault(result["status"],result["error"])
            return result["result"]

        worker_ref = config(exchange,"worker-sources.json",{"worker:oguri":str(original/"worker.db")})
        remote_part = guest_root/"sealed-worker"
        guest_capture = ("python3","-B","-m","rewrite.state_bundle","capture-part","--sources",worker_ref,
                         "--revision",revision,"--batch-id",batch,"--host-id","linux-worker","--destination",remote_part)

        with services(local,lose_report=True) as (manager,router_port,adapter_port):
            configure(original,test_root/"original-settings",router_port,adapter_port)
            with guest_worker(engine,image,original,native.session,package=package) as (port,old_process):
                def status():
                    return live_request(original,port,"ops","GET","worker/status")

                try:
                    eventual(lambda:any(w["online"] and w["busy"] is False for w in manager.workers()["items"]))
                except AssertionError as error:
                    error.add_note("Router: "+encode(manager.workers()["items"]))
                    error.add_note("Worker: "+encode(status()))
                    raise
                tid = manager.create(guid(),"Cutover replay","Formal W04 with a dropped receipt","oguri")["task_id"]
                clock.advance(10)
                payload,receipt = eventual(native.entries)[0]
                assignment = payload["assignment_id"]
                self.assertEqual(status()["held_assignment_id"],assignment)
                arguments = {"task_id":tid,"assignment_id":assignment,"summary":"Original report before capture"}
                request = {"session":native.session,"turn_id":receipt["turn_id"],
                           "tool":"worker_report","call_id":"fixture-w04","arguments":arguments}
                with self.assertRaisesRegex(Fault,"worker_report_unconfirmed"):
                    live_request(original,port,"adapter","POST","worker/native-tool",request)
                self.assertTrue(first_reply_lost.is_set())
                detail = manager.inspect(tid)
                self.assertEqual(detail["task"]["state"],"completed_reported")
                self.assertEqual(status()["held_assignment_id"],assignment)
                blocked = guest(*guest_capture,check=False)
                self.assertNotEqual(blocked.returncode,0)
                self.assertIn("state_writer_already_running",blocked.stderr)
                self.assertNotEqual(guest("test","-e",remote_part,check=False).returncode,0)
        original_reports = len(submissions)
        worker_fragment = json.loads(guest(*guest_capture).stdout)
        imported_worker = exchange/"worker-part"
        guest("cp","-pR",remote_part,imported_worker)
        local_config = config(test_root,"local-sources.json",{key:str(path) for key,path in local.items()})
        local_part = test_root/"mac-part"

        def local_cli(*arguments):
            result = subprocess.run([sys.executable,"-B","-m","rewrite.state_bundle",*map(str,arguments)],
                                    cwd=package,check=True,text=True,capture_output=True,timeout=20)
            return json.loads(result.stdout)

        mac_fragment = local_cli("capture-part","--sources",local_config,"--revision",revision,"--batch-id",batch,
                                 "--host-id","mac-router","--destination",local_part)
        records = {host:{"snapshot":str(path),"manifest_sha256":part["manifest_sha256"],"components":labels}
                   for host,path,part,labels in (("linux-worker",imported_worker,worker_fragment,["worker:oguri"]),
                                               ("mac-router",local_part,mac_fragment,list(local)))}
        plan = config(test_root,"combine.json",{"batch_id":batch,"source_revision":revision,"hosts":records})
        full = test_root/"combined"
        sealed = local_cli("assemble","--plan",plan,"--destination",full)
        digest = sealed["manifest_sha256"]
        before = audit_handoff(full,digest)
        ledger, = before["workers"]
        self.assertTrue(before["needs_review"])
        self.assertIn("report_receipt_pending",ledger["issues"])
        self.assertTrue(audit_trigger(full,digest)["ready"])
        paths = restore(full,digest,test_root/"restored")
        guest("cp","-p",paths["worker:oguri"],recovered/"worker.db")

        with services(paths) as (manager,router_port,adapter_port):
            configure(recovered,test_root/"restored-settings",router_port,adapter_port)
            with guest_worker(engine,image,recovered,native.session,package=package) as (port,new_process):
                eventual(lambda:(state:=live_request(recovered,port,"ops","GET","worker/status"))["router_connected"]
                         and state["held_assignment_id"] is None and len(submissions)>original_reports)
                ack = live_request(recovered,port,"adapter","POST","worker/native-tool",request)
                self.assertEqual(ack,{"recorded":True,"release_assignment_id":assignment,"task_state":"completed_reported"})
                self.assertEqual(manager.inspect(tid)["last_report"],detail["last_report"])
                notice, = [row for row in manager.messages(source="oguri")["items"] if row.get("task_id")==tid]
                self.assertEqual(native.entries(),[(payload,receipt)])

        self.assertNotEqual(old_process,new_process)
        self.assertGreater(len(submissions),original_reports)
        self.assertTrue(all(row==submissions[0] for row in submissions))
        self.assertEqual(audit_handoff(full,digest),before)
        for record in records.values():
            verify(Path(record["snapshot"]),record["manifest_sha256"],partial=True)
        if output := os.environ.get("MOWULINK_CROSS_HOST_STATE_EVIDENCE"):
            Path(output).write_text(encode({"capture":sealed,"worker_device_fs":fs,"image":image,
                "worker_containers":[old_process,new_process],"active_guest_refused_snapshot":True,
                "full_backup_unchanged":True,"task_id":tid,"assignment_id":assignment,"native_receipt":receipt,
                "audit_before":ledger,"probe_submissions":submissions,"w04_recovered":ack,
                "native_inputs":1,"report_notices":1,"notice_id":notice["message_id"],
                "boundary":"macOS Router and adapter, Linux guest ext4 Worker, actual HTTPS. Agent and manager are fixtures."})
                +"\n",encoding="utf-8")


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
        cert,tls = guest_tls(state,host)
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


@unittest.skipUnless(DSH.is_file() and os.environ.get("MOWULINK_DSH_CROSS_GUEST_IMAGE") and shutil.which("openssl"),
                     "opt-in Linux VM image and bundled macOS DSH SDK required")
class DshCrossGuest(NativeProcess):
    def test_dsh_reports_from_the_original_turn_after_its_linux_worker_restarts(self):
        package = Path(__file__).resolve().parents[1]
        self.assertFalse((package/"server").exists(),"run from an installed rewrite-only release")
        self.assertFalse((package/"sources").exists(),"run from an installed rewrite-only release")
        image = os.environ["MOWULINK_DSH_CROSS_GUEST_IMAGE"]
        self.assertRegex(image,r"^sha256:[a-f0-9]{64}$")
        socket = Path(os.environ["MOWULINK_DSH_CROSS_GUEST_SOCKET"])
        self.assertTrue(socket.is_absolute() and socket.is_socket())
        docker = shutil.which("docker")
        self.assertIsNotNone(docker)
        engine = [docker,"--host","unix://"+str(socket)]
        host = os.environ.get("MOWULINK_CROSS_GUEST_HOST","host.lima.internal")
        self.assertRegex(host,r"^[a-zA-Z0-9][a-zA-Z0-9.-]+$")
        shared = Path(os.environ.get("MOWULINK_CROSS_GUEST_SHARED",str(Path.home())))
        state = Path(self.enterContext(TemporaryDirectory(dir=shared,prefix=".mowulink-dsh-guest-")))
        runtime = self.local_tls_directory()
        cert,tls = guest_tls(state,host)
        worker_key,manager_key,adapter_key,callback_key,ops_key,platform_key = (guid() for _ in range(6))
        clock = Clock()
        orders = Orders(runtime/"router.db",clock,poll_seconds=1)
        router = serve_router(orders,{worker_key:"worker/oguri",manager_key:"hermes"},("127.0.0.1",0),tls)
        self.enterContext(running(router))
        router_url = f"https://127.0.0.1:{router.server_port}"
        manager_host = FixtureHost(runtime/"manager-native.db","isolated-manager")
        manager = HermesAdapter(runtime/"manager.db",Client(router_url,manager_key,cert),
            HostAdapter(runtime/"manager-host.db",manager_host,manager_host.session),clock)
        session = "dsh-isolated-linux-worker-"+guid()
        link = LocalLink(runtime,use_tls=True)
        adapter = HostAdapter(runtime/"adapter.db",DshHost(runtime,link=link),session)
        adapter_http = serve_adapter(adapter,"oguri",{adapter_key:"worker/oguri"},("127.0.0.1",0),tls)
        self.enterContext(running(adapter_http))
        config(state,"to-router.json",{"url":f"https://{host}:{router.server_port}","token":worker_key,
                                      "ca_file":"/state/test.crt"})
        config(state,"to-adapter.json",{"url":f"https://{host}:{adapter_http.server_port}","token":adapter_key,
                                       "ca_file":"/state/test.crt"})
        config(state,"roles.json",{callback_key:"adapter/oguri",ops_key:"ops/oguri",platform_key:"platform/oguri"})
        arrival,observations = Queue(),Queue()
        release = [Event(),Event()]
        callback_attempts = 0

        with ExitStack() as first_worker:
            port,first = first_worker.enter_context(guest_worker(engine,image,state,session,package=package))
            endpoint = f"https://127.0.0.1:{port}"
            callback,ops = Client(endpoint,callback_key,cert),Client(endpoint,ops_key,cert)
            forward = callback.request

            def request(method,path,body=None,*args,**kwargs):
                nonlocal callback_attempts
                if path!="worker/native-tool" or body.get("tool")!="worker_report":
                    return forward(method,path,body,*args,**kwargs)
                attempt = callback_attempts
                callback_attempts += 1
                arrival.put((body["session"],body["turn_id"],body["call_id"]))
                if attempt>=len(release) or not release[attempt].wait(10):
                    raise Fault(503,"fixture_report_gate_not_released")
                try:
                    proof = forward(method,path,body,*args,**kwargs)
                except (URLError,OSError):
                    observations.put("worker_connection_unconfirmed")
                    raise
                observations.put(proof)
                return proof

            callback.request = request
            self.enterContext(tool_listener(runtime,adapter,callback,link=link))
            native = self.enterContext(self.start_native(runtime,session,"formal",use_tls=True))
            self.addCleanup(release[0].set)
            self.addCleanup(release[1].set)

            def status():
                return ops.request("GET","worker/status")

            def bound():
                result = status()
                return result if result["host_session"]==session and result["router_connected"] and result["adapter_connected"] else None

            initial = eventual(bound)
            eventual(lambda:any(row["online"] and row["busy"] is False for row in manager.workers(worker_id="oguri")["items"]))
            with self.assertRaises(URLError):
                Client(endpoint,ops_key).request("GET","worker/status")
            with self.assertRaises(Fault) as denied:
                Client(endpoint,platform_key,cert).request("GET","worker/status")
            self.assertEqual(denied.exception.status,403)

            tid = manager.create(guid(),"DSH cross-guest recovery","Report from the original isolated turn","oguri")["task_id"]
            clock.advance(10)
            first_call = arrival.get(timeout=15)
            working = manager.inspect(tid)["task"]
            aid = working["assignment_id"]
            self.assertEqual(status()["held_assignment_id"],aid)
            frames = Client(router_url,worker_key,cert).frames(0)
            dispatch, = [frame for frame in frames if frame["kind"]=="task.dispatch"]
            receipt = adapter.lookup_delivery(dispatch["delivery_id"],{"kind":"task",**dispatch["payload"]},session=session)
            self.assertEqual(first_call,(session,receipt["turn_id"],"native-w04-fixture-0"))

        release[0].set()
        failed = observations.get(timeout=10)
        self.assertEqual(failed,"worker_connection_unconfirmed")
        self.assertIsNone(manager.inspect(tid)["last_report"])
        self.assertEqual(manager.inspect(tid)["task"],working)
        with guest_worker(engine,image,state,session,port,package=package) as (reused,second):
            self.assertEqual(reused,port)
            self.assertNotEqual(second,first)
            restored = eventual(bound)
            self.assertEqual(restored["held_assignment_id"],aid)
            self.assertTrue(restored["host_busy"])
            second_call = arrival.get(timeout=10)
            self.assertEqual(second_call,(session,receipt["turn_id"],"native-w04-fixture-1"))
            release[1].set()
            returned = observations.get(timeout=10)
            observed = self.read_native(native,"read")
            self.assertEqual(observed["inputs"],[receipt["native_id"]])
            self.assertEqual([result["isError"] for result in observed["results"]],[True,False])
            self.assertIn("worker_unconfirmed",encode(observed["results"][0]))
            w04 = json.loads(observed["results"][1]["content"][0]["text"])
            self.assertEqual(w04,{"recorded":True,"release_assignment_id":aid,"task_state":"completed_reported"})
            self.assertEqual(returned,w04)
            self.assertIsNone(status()["held_assignment_id"])
            detail = manager.inspect(tid)
            self.assertEqual(detail["last_report"]["assignment_id"],aid)
            self.assertEqual(detail["task"]["state"],"completed_reported")
            notices = manager.messages(source="oguri")["items"]
            notice, = [item for item in notices if item.get("task_id")==tid]
            self.assertEqual(notice["text"],detail["last_report"]["summary"])

        if output := os.environ.get("MOWULINK_DSH_CROSS_GUEST_EVIDENCE"):
            Path(output).write_text(encode({"evidence_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                "boundary":"Native macOS DSH SDK, real Linux VM Worker, TLS in both directions. Local model and manager are test fixtures.",
                "guest_image":image,"worker_containers":[first,second],"initial":initial,"reopened":restored,
                "task_id":tid,"assignment_id":aid,"native_receipt":receipt,"native_inputs":observed["inputs"],
                "report_calls":[first_call,second_call],"first_callback":failed,"w04":w04,
                "notify_id":notice["message_id"],
                "other_notices":[{"message_id":item["message_id"],"task_id":item.get("task_id"),"text":item["text"]}
                                 for item in notices if item["message_id"]!=notice["message_id"]]})+"\n",encoding="utf-8")
