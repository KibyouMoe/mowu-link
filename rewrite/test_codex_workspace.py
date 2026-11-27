"""Run an installed Codex App Server against disposable Linux assignment workspaces."""
from contextlib import ExitStack, closing
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest

from . import test_dsh_native_loop as dsh_checks
from .adapter import HostAdapter
from .devloop import FixtureHost, Loop
from .dsh_workspace_audit import inspect, journal
from .host_wire import RemoteAdapter
from .release import build, materialize
from .release_check import config, serve
from .state_bundle import capture, restore, rollback, verify
from .storage import Fault, encode, guid
from .test_codex_app_server import CODEX, LINK_OUTAGE_FIXTURE, Provider, running
from .test_mainline import Clock
from .test_workspace_engine import WINDOWS_DOCKER_PIPE, windows_docker_options, windows_real_docker
from .trigger import Trigger
from .wire import Client
from .worker import Worker
from .worker_service import WorkerService


DATA = bytes(range(256))*2700


class WorkspaceProvider(Provider):
    def link_outage_item(self,payload,after):
        outputs = {entry.get("call_id"):entry["output"] for entry in after if entry.get("type")=="function_call_output"}
        if "workspace-shell" not in outputs:
            command = "python3 -c 'from pathlib import Path; Path(\"output.bin\").write_bytes(bytes(range(256))*2700)'"
            if self.server.action!="complete":
                command = "set -e; "+command+"; setsid /bin/sleep 120 </dev/null >/dev/null 2>&1 & echo $! > /workspace/pid; sleep 20"
            call,tool,args = "workspace-shell","worker_workspace_shell",{"command":command}
        elif "workspace-export" not in outputs:
            call,tool,args = "workspace-export","worker_workspace_export",{"path":"output.bin"}
        elif "workspace-export-again" not in outputs:
            call,tool,args = "workspace-export-again","worker_workspace_export",{"path":"output.bin"}
        elif "workspace-report" not in outputs:
            try:
                staged = json.loads(outputs["workspace-export"])
                self.server.exports = [staged,json.loads(outputs["workspace-export-again"])]
            except ValueError:
                self.server.workspace_error = outputs
                self.server.report_returned.set()
                return {"type":"message","id":"workspace-error","role":"assistant","status":"completed",
                        "content":[{"type":"output_text","text":"Workspace export failed."}]}
            call,tool,args = "workspace-report","worker_report",{"task_id":payload["task_id"],
                "assignment_id":payload["assignment_id"],"summary":"Isolated Codex artifact",
                "artifact_ids":staged["artifact_ids"]}
        else:
            self.server.report_returned.set()
            return {"type":"message","id":"workspace-final","role":"assistant","status":"completed",
                    "content":[{"type":"output_text","text":"Workspace tool calls finished."}]}
        return {"type":"function_call","id":"fc_"+call,"name":tool,"call_id":call,"arguments":encode(args)}


def eventually(check):
    until = time.monotonic()+10
    while time.monotonic()<until:
        result = check()
        if result:
            return result
        time.sleep(.04)
    raise AssertionError("the isolated Codex workspace did not reach its expected state")


def boot_previous(root,revision,recovered,sid,profile,project,cert,key,env,dispatch,payload):
    repository = Path(__file__).resolve().parents[1]
    release = build(repository,revision,root/"previous-release")
    package = root/"previous-package"
    materialize(Path(release["archive"]),revision,release["sha256"],package,install=True)
    worker_key,manager_key,adapter_key,callback_key,platform_key = (guid() for _ in range(5))
    router_roles = config(root,"old-router-roles.json",{worker_key:"worker/oguri",manager_key:"hermes"})
    adapter_roles = config(root,"old-adapter-roles.json",{adapter_key:"worker/oguri"})
    worker_roles = config(root,"old-worker-roles.json",{callback_key:"adapter/oguri",platform_key:"platform/oguri"})
    with ExitStack() as stack:
        router_url = stack.enter_context(serve(package,"router",recovered["router:main"],router_roles,cert,key,env))
        manage = Client(router_url,manager_key,cert)
        def connection(name,url,token):
            return config(root,name+".json",{"url":url,"token":token,"ca_file":str(cert)})
        to_router = connection("old-router",router_url,worker_key)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1",0))
            port = reservation.getsockname()[1]
        to_worker = connection("old-callback",f"https://127.0.0.1:{port}",callback_key)
        adapter_options = ("--worker-id","oguri","--session",sid,"--worker-connection",to_worker,"--codex-binary",CODEX,
                           "--codex-profile",profile,"--cwd",project,"--read-only-assignments")
        adapter_url = stack.enter_context(serve(package,"codex-adapter",recovered["agent:oguri"],adapter_roles,cert,key,env,
                                               adapter_options,shutdown_timeout=15))
        to_adapter = connection("old-adapter",adapter_url,adapter_key)
        worker_options = ("--worker-id","oguri","--port",str(port),"--router-connection",to_router,
                          "--adapter-session",sid,"--adapter-connection",to_adapter)
        worker_url = stack.enter_context(serve(package,"worker",recovered["worker:oguri"],worker_roles,cert,key,env,
                                              worker_options,shutdown_timeout=20))
        adapter = RemoteAdapter(Client(adapter_url,adapter_key,cert),sid)
        original = adapter.lookup_delivery(dispatch["delivery_id"],payload,session=sid)
        platform = Client(worker_url,platform_key,cert)
        chat = {"message_id":guid(),"text":"Isolated previous-version private chat"}
        def receive():
            try:
                return platform.request("POST","worker/chat",chat)["receipt"]
            except Fault as error:
                if error.status not in (409,503):
                    raise
                return None
        first = eventually(receive)
        repeated = receive()
        eventually(lambda:not adapter.busy(session=sid))
        task = manage.request("GET","tasks/"+payload["task_id"])
        artifact, = task["last_report"]["artifact_ids"]
        contents = manage.request("GET","attachments/"+artifact)["bytes"]
        return {"revision":revision,"package_sha256":release["sha256"],"original_input":original,
                "private_chat":first,"retry_chat":repeated,"task":task,"artifact_sha256":hashlib.sha256(contents).hexdigest()}


@unittest.skipUnless(CODEX.is_file() and shutil.which("node") and shutil.which("openssl"),
                     "bundled Codex, Node and openssl required")
class CodexWorkspaceTest(unittest.TestCase):
    def test_native_workspace_artifact_release_cancel_and_transfer(self):
        docker_options = {}
        engine = None
        if os.name=="nt":
            if os.environ.get("MOWULINK_WINDOWS_WORKSPACE_TEST")!="1":
                self.skipTest("opt-in Windows workspace test with Docker Desktop required")
            image,cli,engine = windows_real_docker(self)
            docker_options = windows_docker_options(cli)
            docker,workspace_socket = cli[0],WINDOWS_DOCKER_PIPE
        else:
            image,cli = dsh_checks.NativeLoop.container_engine(self)
            docker,_,endpoint = cli
            workspace_socket = endpoint.removeprefix("unix://")
        cases = []
        for action in ("complete","cancel","transfer"):
            with self.subTest(action=action), ExitStack() as resources:
                root = Path(resources.enter_context(TemporaryDirectory(
                    dir=None if os.name=="nt" else "/tmp",prefix="ml-codex-ws-"))).resolve()
                profile,project,runtime = (root/name for name in ("profile","project","workspace"))
                for private in (profile,project,runtime):
                    private.mkdir(mode=0o700)
                provider = ThreadingHTTPServer(("127.0.0.1",0),WorkspaceProvider)
                provider.observed,provider.media,provider.action = [],[],action
                provider.report_returned = Event()
                control = provider.cleanup_fixture = {"late_tool":False,"response":Queue(),"release_control":Event()}
                origin = resources.enter_context(running(provider))
                resources.callback(control["release_control"].set)
                (profile/"config.toml").write_text('model="gpt-5.4"\nmodel_provider="test_wire"\nweb_search="disabled"\n'
                    '[model_providers.test_wire]\nname="Local test wire"\nbase_url='+encode(origin+'/v1')+'\nwire_api="responses"\n'
                    'request_max_retries=0\nstream_max_retries=0\n',encoding="utf-8")
                env = {"HOME":str(root),"TMPDIR":str(root),"PATH":os.defpath,"PYTHONNOUSERSITE":"1"}
                if os.name=="nt":
                    env.update(SystemRoot=os.environ["SystemRoot"],USERPROFILE=str(root),TEMP=str(root),TMP=str(root))
                tools = ("worker_report","worker_workspace_shell","worker_workspace_export")
                result = subprocess.run([sys.executable,"-B","-m","rewrite.service","codex-provision",
                    "--codex-binary",CODEX,"--codex-profile",profile,"--cwd",project,
                    *(arg for tool in tools for arg in ("--tool",tool))],capture_output=True,text=True,timeout=20,env=env)
                self.assertEqual(result.returncode,0,result.stderr)
                sid = json.loads(result.stdout)["thread_id"]
                resources.callback(dsh_checks.NativeLoop.clean_fixture_containers,self,runtime,sid,cli,
                                   expected_daemon=engine,**docker_options)
                clock = Clock()
                loop = resources.enter_context(Loop.isolated(clock,extra_workers=("tama",)))
                worker,manager = loop.worker,loop.hermes
                service = WorkerService(worker,clock)
                callback_key,adapter_key = guid(),guid()
                roles = config(root,"adapter-roles.json",{adapter_key:"worker/oguri"})
                configuration = config(root,"assignment-workspace.json",{"runtime":str(runtime),"node":shutil.which("node"),
                    "image":image,"docker":docker,"socket":workspace_socket})
                cert,key = root/"test.crt",root/"test.key"
                subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                    "-addext","subjectAltName=IP:127.0.0.1","-keyout",key,"-out",cert],check=True,capture_output=True,timeout=10)
                adapter_db = root/"adapter.db"
                args = ("--worker-id","oguri","--session",sid,"--codex-binary",CODEX,
                        "--codex-profile",profile,"--cwd",project,"--assignment-workspace",configuration)
                with ExitStack() as station:
                    def start(current,stage):
                        callback_url = station.enter_context(running(current.listen(
                            {callback_key:"adapter/oguri"},("127.0.0.1",0),None)))
                        callback = config(root,stage+"-callback.json",{"url":callback_url,"token":callback_key})
                        target = station.enter_context(serve(Path(__file__).resolve().parent.parent,"codex-adapter",
                            adapter_db,roles,cert,key,env,(*args,"--worker-connection",callback),shutdown_timeout=15))
                        return RemoteAdapter(Client(target,adapter_key,cert),sid)

                    remote = worker.adapter = start(service,"before")
                    if action=="complete":
                        normal_router = loop.wire.request
                        attempts = []

                        def observe_report(method,path,body=None,key=None,*extra,**kwargs):
                            response = normal_router(method,path,body,key,*extra,**kwargs)
                            if method=="POST" and path.endswith("/report"):
                                attempts.append({"body":body,"key":key,"receipt":response})
                                if len(attempts)==1:
                                    raise Fault(503,"isolated_workspace_report_unconfirmed",True)
                            return response

                        loop.wire.request = observe_report
                    other_host = FixtureHost(root/"other-native.db","fixture-next-owner")
                    other = Worker(root/"other-worker.db","tama",loop.extra_clients["tama"],
                                   HostAdapter(root/"other-adapter.db",other_host,other_host.session),clock=clock)
                    other.connect()
                    other.poll()
                    service.step()
                    clock.advance(10)
                    other.poll()
                    service.step()
                    task = manager.create(guid(),"Native Codex workspace",LINK_OUTAGE_FIXTURE,"oguri")["task_id"]
                    service.step()
                    assignment = worker.hold()["held_assignment_id"]
                    self.assertIsNotNone(assignment)

                    def active_workspace():
                        for path in (runtime/"assignment-containers").glob("*.json"):
                            value = json.loads(path.read_text())
                            if value["assignment_id"]==assignment and value["phase"]=="active":
                                return path,value
                    lease,record = eventually(active_workspace)
                    if engine is not None:
                        self.assertEqual((record["daemon"],record["image"]),(engine,image))
                    cid = record["container_id"]
                    identity = journal(runtime)[1]
                    self.assertEqual(inspect(runtime,{identity}),(None,"dsh_workspace_host_still_running"))
                    stopped_pid = None
                    if action=="complete":
                        self.assertTrue(provider.report_returned.wait(10))
                        self.assertIsNone(getattr(provider,"workspace_error",None))
                        self.assertEqual(len(attempts),1)
                        self.assertEqual(manager.inspect(task)["task"]["state"],"completed_reported")
                        self.assertEqual(worker.hold()["held_assignment_id"],assignment)
                        self.assertTrue(remote.handoff_activity(session=sid)["workspace_pending"])
                        exported, again = provider.exports
                        self.assertEqual(exported,again)
                        artifact, = exported["artifact_ids"]
                        self.assertEqual(loop.manager_wire.request("GET","attachments/"+artifact)["bytes"],DATA)
                        eventually(lambda:not remote.busy(session=sid))
                        model_requests = len(provider.observed)
                        station.close()
                        pending,error = inspect(runtime,{identity})
                        self.assertIsNone(error)
                        self.assertEqual(pending["pending"],[{"session":sid,"task_id":task,"assignment_id":assignment,
                                                              "phase":"active"}])
                        worker = Worker(loop.root/"worker.db","oguri",loop.wire,remote,clock=clock)
                        self.assertEqual(worker.hold()["held_assignment_id"],assignment)
                        worker.flush_reports()
                        self.assertEqual(attempts,[attempts[0],attempts[0]])
                        self.assertIsNone(worker.hold()["held_assignment_id"])
                        trigger = Trigger(root/"trigger.db","workspace-recovery",clock)
                        snapshot = root/"snapshot"
                        sources = {"router:main":loop.orders.db.path,"worker:oguri":worker.db.path,"agent:oguri":adapter_db,
                                   "hermes:main":manager.db.path,"trigger:workspace-recovery":trigger.db.path}
                        manifest_id = capture(sources,"a"*40,snapshot)["manifest_sha256"]
                        self.assertEqual(verify(snapshot,manifest_id)["format"],3)
                        trial = restore(snapshot,manifest_id,root/"trial")
                        rollback_args = (snapshot,manifest_id,trial,root/"rollback")
                        with self.assertRaisesRegex(ValueError,"dsh_assignment_workspace_pending"):
                            rollback(*rollback_args,dsh_runtimes={"oguri":runtime})
                        self.assertFalse((root/"rollback").exists())
                        self.assertEqual(json.loads(lease.read_text()),record)
                        service = WorkerService(worker,clock)
                        remote = worker.adapter = start(service,"after")
                        self.assertEqual(remote.bound_session(),sid)
                        service.step()
                        notice, = manager.messages(source="oguri")["items"]
                        self.assertEqual(len(provider.observed),model_requests)
                        proof = {"w04":attempts[0]["receipt"],"w04_idempotency_key":attempts[0]["key"],
                                 "artifact_id":artifact,"bytes":len(DATA),"sha256":hashlib.sha256(DATA).hexdigest(),
                                 "notify_id":notice["message_id"],"workspace_alive_before_w04_receipt":True,
                                 "pending_while_station_stopped":pending,"model_requests_after_restart":0}
                    else:
                        def detached_pid():
                            result = subprocess.run([*cli,"exec",cid,"/bin/sh","-c",
                                'p=$(cat /workspace/pid) && kill -0 "$p" && printf "%s" "$p"'],
                                text=True,capture_output=True,timeout=5,**docker_options)
                            return result.stdout if result.returncode==0 else None
                        stopped_pid = eventually(detached_pid)
                        self.assertIn("held_assignment_id",service.status())
                        before = manager.inspect(task)["task"]
                        manager.act(guid(),task,before["revision"],action,reason="Stop the isolated Codex task",
                                    **({"worker_id":"tama"} if action=="transfer" else {}))
                        if action=="transfer":
                            other.poll()
                            next_task = manager.inspect(task)["task"]
                            self.assertNotEqual(assignment,next_task["assignment_id"])
                            self.assertEqual(other.hold()["held_assignment_id"],next_task["assignment_id"])
                        def wait_for_control():
                            service.step()
                            return None if control["response"].empty() else control["response"].get_nowait()
                        self.assertEqual(eventually(wait_for_control),"native_cleanup_waiting")
                        self.assertTrue(manager.inspect(task)["pending_cleanup"])
                        control["release_control"].set()
                        def wait_for_w04():
                            service.step()
                            return not manager.inspect(task)["pending_cleanup"]
                        eventually(wait_for_w04)
                        if action=="transfer":
                            self.assertEqual(manager.inspect(task)["task"],next_task)
                        else:
                            self.assertEqual(manager.inspect(task)["task"]["state"],"cancelled")
                        with worker.db.transaction() as db:
                            row = db.execute("SELECT proof,receipt FROM cleanup_reports WHERE assignment_id=?",(assignment,)).fetchone()
                        receipt = json.loads(row["receipt"])
                        native_proof = json.loads(row["proof"])
                        self.assertEqual(native_proof["scope"],"assignment_container")
                        self.assertEqual(native_proof["workspace"]["container_id"],cid)
                        self.assertEqual(receipt["release_assignment_id"],assignment)
                        proof = {"w04":receipt,"native_cleanup":native_proof}
                    self.assertIsNone(worker.hold()["held_assignment_id"])
                    self.assertFalse((project/"output.bin").exists())
                    eventually(lambda:json.loads(lease.read_text())["phase"]=="sealed")
                    remaining = subprocess.check_output([*cli,"container","ls","-aq","--filter","id="+cid],
                                                        text=True,timeout=5,**docker_options)
                    self.assertEqual(remaining.strip(),"")
                    with closing(sqlite3.connect(adapter_db)) as db:
                        commands = db.execute("SELECT turn_id,call_id FROM codex_workspace_commands").fetchall()
                        self.assertEqual(len(commands),1)
                        inputs = db.execute("SELECT receipt,payload FROM intents WHERE json_extract(payload,'$.kind')='task'").fetchall()
                        (receipt,payload), = inputs
                        dispatch,payload = json.loads(receipt),json.loads(payload)
                        self.assertEqual(commands[0],(dispatch["turn_id"],"workspace-shell"))
                audit,error = inspect(runtime,{identity})
                self.assertIsNone(error)
                self.assertEqual(audit["pending"],[])
                if action=="complete":
                    recovered = rollback(*rollback_args,dsh_runtimes={"oguri":runtime})
                    self.assertEqual(recovered.keys(),sources.keys())
                    for label,path in recovered.items():
                        self.assertEqual(Path(path).read_bytes(),Path(trial[label]).read_bytes())
                    proof.update(snapshot_format=3,rollback_before_reap="dsh_assignment_workspace_pending",
                                 restored_components=sorted(recovered))
                    if previous := os.environ.get("MOWULINK_CODEX_WORKSPACE_PREVIOUS"):
                        count = len(provider.observed)
                        confirmation = boot_previous(root,previous,recovered,sid,profile,project,cert,key,env,dispatch,payload)
                        self.assertEqual(confirmation["original_input"],dispatch)
                        self.assertEqual(confirmation["private_chat"],confirmation["retry_chat"])
                        self.assertEqual(confirmation["private_chat"]["session"],sid)
                        self.assertEqual(confirmation["task"]["task"],manager.inspect(task)["task"])
                        self.assertEqual(confirmation["task"]["last_report"]["artifact_ids"],[artifact])
                        self.assertEqual(confirmation["artifact_sha256"],proof["sha256"])
                        eventually(lambda:len(provider.observed)>count)
                        self.assertEqual(len(provider.observed)-count,1)
                        proof.update(previous_release=confirmation,previous_native_model_requests=1)
                cases.append({"action":action,"task_id":task,"assignment_id":assignment,"original_input":dispatch,
                    "container_id":cid,"detached_child_pid_before_control":stopped_pid,"offline_workspace_audit":audit,
                    "final_task":manager.inspect(task)["task"],**proof})
        if output := os.environ.get("MOWULINK_CODEX_WORKSPACE_EVIDENCE"):
            evidence = {"recorded_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                "boundary":"Bundled Codex App Server, independent TLS adapter and Linux Docker. Local scripted model and replacement host.",
                "cases":cases}
            if engine is not None:
                evidence.update(docker_engine=engine,docker_image=image,docker_pipe=workspace_socket,operating_system="Windows")
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
