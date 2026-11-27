"""Maintenance authorization, operation recovery, and an isolated real macOS LaunchAgent."""
from contextlib import closing, contextmanager, redirect_stdout
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
from io import StringIO
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import urlsplit

from . import host_ops_cli
from .hermes_check import GATEWAY_ENTRY, tracked_runtime
from .host_ops import HostOps, Job, Launchd, roles_allowed
from .release_check import config, serve
from .storage import Fault, encode, guid
from .wire import Client


PACKAGE = Path(__file__).resolve().parent.parent


@contextmanager
def launchd_job(root,program,stem,*,run_at_load=True,working_directory=PACKAGE,exit_timeout=3):
    domain = f"gui/{os.getuid()}"
    label = "org.mowulink.test."+stem+"-"+guid()
    target = domain+"/"+label
    plist = root/(stem+".plist")
    output = root/(stem+".stdout")
    plist.write_bytes(plistlib.dumps({"Label":label,"ProgramArguments":list(map(str,program)),"RunAtLoad":run_at_load,
        "WorkingDirectory":str(working_directory),"ThrottleInterval":1,"ExitTimeOut":exit_timeout,
        "StandardOutPath":str(output),"StandardErrorPath":str(root/(stem+".stderr")),
        "EnvironmentVariables":{"PYTHONDONTWRITEBYTECODE":"1","HOME":str(root)}}))
    plist.chmod(0o600)
    subprocess.run(["/bin/launchctl","bootstrap",domain,plist],check=True,capture_output=True,timeout=10)
    try:
        yield {"label":label,"plist":str(plist)},output
    finally:
        subprocess.run(["/bin/launchctl","bootout",target],check=True,capture_output=True,timeout=exit_timeout+10)
        deadline = time.monotonic()+12
        while time.monotonic()<deadline:
            if subprocess.run(["/bin/launchctl","print",target],capture_output=True,timeout=5).returncode!=0:
                break
            time.sleep(.1)
        else:
            raise AssertionError("temporary LaunchAgent did not unload")


class NoWriteBackend:
    def observe(self,_job):
        return {"state":"running","pid":123,"generation":"a"*64}

    def restart(self,*args):
        raise AssertionError("an interrupted operation was resubmitted")


class HeldQueue:
    def __init__(self):
        self.calls = []

    def submit(self,*args):
        self.calls.append(args)


class HostOpsState(unittest.TestCase):
    def test_external_cli_rejects_unencrypted_operator_connection(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        path = config(root,"plain.json",{"url":"http://127.0.0.1:8123","token":"fixture"})
        with self.assertRaisesRegex(ValueError,"private HTTPS maintenance connection"):
            host_ops_cli.connect(path)

    def test_external_cli_keeps_the_original_request_id_when_the_reply_is_lost(self):
        client = Mock()
        client.request.side_effect = ConnectionResetError("lost fixture response")
        output = StringIO()
        rid = guid()
        args = ["--connection","fixture.json","restart","gateway","--request-id",rid,
                "--expected-generation","a"*64,"--reason","isolated response loss"]
        with patch.object(host_ops_cli,"connect",return_value=client), redirect_stdout(output):
            self.assertEqual(host_ops_cli.main(args),1)
        client.request.assert_called_once_with("POST","ops/services/gateway/restart",
            {"request_id":rid,"expected_generation":"a"*64,"reason":"isolated response loss"})
        self.assertEqual(json.loads(output.getvalue()),{"error":"restart_receipt_unconfirmed","request_id":rid})

    def test_interrupted_restart_stays_unconfirmed_and_scoped(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        job = Job("test.router",root/"unused.plist","/bin/true","")
        jobs = {"router":job,"other":job}
        queue = HeldQueue()
        operations = HostOps(root/"ops.db",jobs,NoWriteBackend(),queue)
        writer, observer = "host-maintainer/router", "host-observer"
        request = {"request_id":guid(),"expected_generation":"a"*64,"reason":"isolated process recovery"}
        path = "/v1/ops/services/router/restart"
        rid = request["request_id"]
        accepted = operations.route(writer,"POST",path,request,{})
        self.assertEqual((accepted[0],accepted[2]["state"]),(202,"accepted"))
        self.assertEqual(operations.route(writer,"POST",path,request,{}),accepted)
        self.assertEqual(len(queue.calls),1)
        with self.assertRaisesRegex(Fault,"service_restart_in_progress"):
            operations.route(writer,"POST",path,dict(request,request_id=guid()),{})
        with self.assertRaisesRegex(Fault,"forbidden"):
            operations.route(observer,"POST",path,request,{})
        with self.assertRaisesRegex(Fault,"forbidden"):
            operations.route("host-maintainer/other","POST",path,request,{})
        queue = HeldQueue()
        operations = HostOps(root/"ops.db",jobs,NoWriteBackend(),queue)
        status,_,receipt = operations.route(observer,"GET",f"/v1/ops/services/router/restarts/{rid}",{}, {})
        self.assertEqual((status,receipt["state"],receipt["failure"]),(200,"unconfirmed","maintenance_process_interrupted"))
        self.assertEqual(operations.route(writer,"POST",path,request,{})[2],receipt)
        with self.assertRaisesRegex(Fault,"service_restart_unconfirmed") as rejected:
            operations.route(writer,"POST",path,dict(request,request_id=guid()),{})
        self.assertEqual(rejected.exception.details,{"blocking_request_id":rid})
        with self.assertRaisesRegex(Fault,"restart_request_conflict"):
            operations.route(writer,"POST",path,dict(request,reason="different"),{})
        self.assertEqual(queue.calls,[])
        self.assertTrue(roles_allowed({"fixture":observer},jobs))
        self.assertFalse(roles_allowed({"fixture":"hermes"},jobs))
        self.assertFalse(roles_allowed({"fixture":observer,"duplicate":observer},jobs))

    def test_generation_change_before_the_native_call_records_no_attempt(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        queue = HeldQueue()
        backend = NoWriteBackend()
        ops = HostOps(root/"ops.db",{"router":Job("test.router",root/"unused.plist","/bin/true","")},backend,queue)
        request = {"request_id":guid(),"expected_generation":"a"*64,"reason":"isolated generation race"}
        writer = "host-maintainer/router"
        ops.begin(writer,"router",request)
        backend.observe = lambda _job: {"state":"running","pid":567,"generation":"b"*64}
        ((execute,*args),) = queue.calls
        execute(*args)
        receipt = ops.result(writer,"router",request["request_id"])
        self.assertEqual((receipt["state"],receipt["before"]["pid"],receipt["after"]["pid"]),
                         ("not_attempted",123,567))

    def test_unconfirmed_reconciliation_requires_the_observed_new_process(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        state = root/"ops.db"
        job = Job("test.router",root/"unused.plist","/bin/true","")
        backend = NoWriteBackend()
        backend.birth = Mock(return_value="fixture-old-process-still-present")
        jobs = {"router":job,"companion":Job("test.neighbor",root/"neighbor.plist","/bin/true","")}
        operation = HostOps(state,jobs,backend,HeldQueue())
        request = {"request_id":guid(),"expected_generation":"a"*64,"reason":"native receipt lost"}
        writer,observer = "host-maintainer/router","host-observer"
        original = operation.begin(writer,"router",request)
        rid = original["request_id"]
        path = f"/v1/ops/services/router/restarts/{rid}/reconcile"
        before = {"expected_generation":"a"*64}
        with self.assertRaisesRegex(Fault,"restart_is_not_unconfirmed"):
            operation.route(writer,"POST",path,before,{})
        queue = HeldQueue()
        operation = HostOps(state,jobs,backend,queue)
        with self.assertRaisesRegex(Fault,"forbidden"):
            operation.route(observer,"POST",path,before,{})
        with self.assertRaisesRegex(Fault,"forbidden"):
            operation.route("host-maintainer/companion","POST",path,before,{})
        with self.assertRaisesRegex(Fault,"replacement_not_observed"):
            operation.route(writer,"POST",path,before,{})
        backend.observe = lambda _job: {"state":"running","pid":567,"generation":"b"*64}
        after = {"expected_generation":"b"*64}
        with self.assertRaisesRegex(Fault,"stale_service_generation"):
            operation.route(writer,"POST",path,before,{})
        with self.assertRaisesRegex(Fault,"prior_process_exit_unconfirmed"):
            operation.route(writer,"POST",path,after,{})
        with self.assertRaisesRegex(Fault,"service_restart_unconfirmed"):
            operation.begin(writer,"router",dict(request,request_id=guid(),expected_generation="b"*64))
        self.assertEqual(queue.calls,[])
        # Renaming a registration cannot bypass the lock on its original native label.
        renamed = HostOps(state,{"renamed":job},backend,queue)
        with self.assertRaisesRegex(Fault,"service_restart_unconfirmed") as denial:
            renamed.begin("host-maintainer/renamed","renamed",
                          dict(request,request_id=guid(),expected_generation="b"*64))
        self.assertEqual(denial.exception.details,{})

        backend.birth.return_value = ""
        settled = operation.route(writer,"POST",path,after,{})[2]
        self.assertEqual(settled["state"],"unconfirmed")
        self.assertEqual(settled["failure"],"maintenance_process_interrupted")
        self.assertEqual((settled["reconciliation"]["state"],settled["reconciliation"]["after"]["pid"]),
                         ("replacement_observed",567))
        self.assertEqual(operation.route(writer,"POST",path,after,{})[2],settled)
        with self.assertRaisesRegex(Fault,"restart_reconciliation_conflict"):
            operation.route(writer,"POST",path,before,{})
        self.assertEqual(queue.calls,[])
        reopened = HostOps(state,jobs,backend,queue)
        self.assertEqual(reopened.result(observer,"router",rid),settled)
        self.assertEqual(reopened.begin(writer,"router",request),settled)
        next_request = dict(request,request_id=guid(),expected_generation="b"*64)
        self.assertEqual(reopened.begin(writer,"router",next_request)["state"],"accepted")
        self.assertEqual(len(queue.calls),1)


@unittest.skipUnless(sys.platform=="darwin" and os.getuid()!=0 and shutil.which("openssl"), "macOS GUI launchd and OpenSSL required")
class LaunchdMaintenance(unittest.TestCase):
    def wait(self,observe, *, seconds=22):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                value = observe()
            except (URLError,OSError):
                value = None
            if value:
                return value
            time.sleep(.1)
        self.fail("isolated maintenance observation timed out")

    def test_interrupted_maintenance_blocks_new_requests_until_native_reconciliation(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="mowulink-restart-reconcile-"))).resolve()
        owner,observer = guid(),guid()
        identities = config(root,"identities.json",{owner:"host-maintainer/router",observer:"host-observer"})
        job,_ = self.enterContext(launchd_job(root,["/bin/sleep","120"],"reconcile"))
        raw = config(root,"manifest.json",{"router":job})
        state = root/"ops.db"
        cert,key = root/"certificate.pem",root/"key.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
            "-addext","subjectAltName=IP:127.0.0.1","-keyout",key,"-out",cert],check=True,capture_output=True,timeout=10)
        native,job = Launchd(),Job.read(job)
        before = self.wait(lambda:current if (current:=native.observe(job))["state"]=="running" else None)
        request = {"request_id":guid(),"expected_generation":before["generation"],"reason":"interrupted isolated operation"}
        queue = HeldQueue()
        fixture = HostOps(state,{"router":job},native,queue)
        fixture.begin("host-maintainer/router","router",request)
        self.assertEqual(len(queue.calls),1)
        env = {**os.environ,"PYTHONDONTWRITEBYTECODE":"1"}
        setup = ("--manifest",raw)
        base = "ops/services/router"
        lookup = base+"/restarts/"+request["request_id"]
        with serve(PACKAGE,"host-ops",state,identities,cert,key,env,setup) as url:
            maintainer,reader = Client(url,owner,cert),Client(url,observer,cert)
            original = reader.request("GET",lookup)
            self.assertEqual((original["state"],original["failure"]),("unconfirmed","maintenance_process_interrupted"))
            with self.assertRaisesRegex(Fault,"service_restart_unconfirmed") as conflict:
                maintainer.request("POST",base+"/restart",dict(request,request_id=guid()))
            self.assertEqual(conflict.exception.details["blocking_request_id"],request["request_id"])
            self.assertEqual(native.observe(job),before)
            with self.assertRaisesRegex(Fault,"replacement_not_observed"):
                maintainer.request("POST",lookup+"/reconcile",{"expected_generation":before["generation"]})
            # The test operator changes only the isolated job using the native supervisor.
            manual = native.restart(job,before)
            self.assertIsNotNone(manual)
            with self.assertRaisesRegex(Fault,"forbidden"):
                reader.request("POST",lookup+"/reconcile",{"expected_generation":manual["generation"]})
            connection = config(root,"connection.json",{"url":url,"token":owner,"ca_file":str(cert)})
            invoked = subprocess.run([sys.executable,"-B","-m","rewrite.host_ops_cli","--connection",connection,
                "reconcile","router",request["request_id"],"--expected-generation",manual["generation"]],
                cwd=PACKAGE,env=env,check=True,capture_output=True,text=True,timeout=10)
            report = json.loads(invoked.stdout)
            self.assertEqual({k:v for k,v in report.items() if k!="reconciliation"},original)
            self.assertEqual(report["reconciliation"]["after"]["pid"],manual["pid"])
            self.assertEqual(native.observe(job),manual)
        with serve(PACKAGE,"host-ops",state,identities,cert,key,env,setup) as url:
            maintainer = Client(url,owner,cert)
            self.assertEqual(maintainer.request("GET",lookup),report)
            self.assertEqual(maintainer.request("POST",base+"/restart",request),report)
            self.assertEqual(native.observe(job),manual)
            next_request = dict(request,request_id=guid(),expected_generation=manual["generation"])
            self.assertEqual(maintainer.request("POST",base+"/restart",next_request)["state"],"accepted")
            def applied():
                row = maintainer.request("GET",base+"/restarts/"+next_request["request_id"])
                return row if row["state"]!="accepted" else None
            replacement = self.wait(applied)
            self.assertEqual(replacement["state"],"process_replaced")
            self.assertEqual(replacement["before"]["pid"],manual["pid"])
            self.assertNotIn(replacement["after"]["pid"],(before["pid"],manual["pid"]))
            self.assertEqual(maintainer.request("POST",base+"/restart",next_request),replacement)
        if evidence := os.environ.get("MOWULINK_RESTART_RECONCILIATION_EVIDENCE"):
            Path(evidence).write_text(json.dumps({
                "blocked_fresh_uuid":True,"receipt_after_cold_restart":report,
                "independent_native_replacement":manual,"new_explicit_restart":replacement,
                "reconciliation_preserved_observed_pid":True,
                "scope":"Actual isolated macOS LaunchAgent and HTTPS; pending request seeded before the backend could run."
            },indent=2)+"\n")

    def test_isolated_router_restart_preserves_task_and_receipt_over_https(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="mowulink-ops-test-"))).resolve()
        owner,observer,alternate,restorer,manager = (guid() for _ in range(5))
        identities = config(root,"ops-identities.json",{owner:"host-maintainer/router",observer:"host-observer",
                                                        alternate:"host-maintainer/companion",
                                                        restorer:"host-maintainer/stopped"})
        router_identities = config(root,"router-identities.json",{manager:"hermes"})
        cert,key = root/"test.crt",root/"test.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
            "-addext","subjectAltName=IP:127.0.0.1","-keyout",key,"-out",cert],check=True,capture_output=True,timeout=10)
        router_program = [sys.executable,"-B","-S","-m","rewrite.service","router","--db",root/"router.db",
                          "--identities",router_identities,"--cert",cert,"--key",key]
        job,stdout = self.enterContext(launchd_job(root,router_program,"router"))
        companion,_ = self.enterContext(launchd_job(root,["/bin/sleep","120"],"companion"))
        stopped,_ = self.enterContext(launchd_job(root,["/bin/sleep","120"],"stopped",run_at_load=False))
        spec = config(root,"manifest.json",{"router":job,"companion":companion,"stopped":stopped})
        state = root/"host-ops.db"
        env = {**os.environ,"PYTHONDONTWRITEBYTECODE":"1"}
        extra = ("--manifest",spec)
        with serve(PACKAGE,"host-ops",state,identities,cert,key,env,extra) as url:
            writable,reader,other = (Client(url,token,cert) for token in (owner,observer,alternate))

            def running_service(client,name):
                observation = client.request("GET","ops/services/"+name)
                return observation if observation["state"]=="running" else None

            before = self.wait(lambda:running_service(reader,"router"))
            neighbor = self.wait(lambda:running_service(reader,"companion"))
            self.assertEqual([row["service"] for row in writable.request("GET","ops/services")["services"]],["router"])
            self.assertEqual(len(reader.request("GET","ops/services")["services"]),3)
            records = self.wait(lambda:stdout.read_text().splitlines())
            self.assertEqual(len(records),1)
            published = json.loads(records[0])
            old_router = Client(f"https://127.0.0.1:{published['port']}",manager,cert)
            create_key = guid()
            create = {"title":"Keep the isolated task","instructions":"Verify unchanged state"}
            task = old_router.request("POST","tasks",create,create_key)
            restart = "ops/services/router/restart"
            rid = guid()
            operation = {"request_id":rid,"expected_generation":before["generation"],"reason":"isolate restart acceptance"}
            for client in (reader,other):
                with self.assertRaisesRegex(Fault,"forbidden"):
                    client.request("POST",restart,operation)
            with self.assertRaisesRegex(Fault,"unauthenticated"):
                Client(url,manager,cert).request("GET","ops/services")
            with self.assertRaisesRegex(Fault,"stale_service_generation"):
                writable.request("POST",restart,dict(operation,expected_generation="0"*64))
            with self.assertRaisesRegex(Fault,"invalid_restart_request"):
                writable.request("POST",restart,dict(operation,command="ignored"))
            self.assertEqual(reader.request("GET","ops/services/router"),before)

            # Submit over real TLS and discard the HTTP response before reading it.
            target = urlsplit(url)
            transport = HTTPSConnection(target.hostname,target.port,context=ssl.create_default_context(cafile=cert),timeout=4)
            try:
                transport.request("POST","/v1/"+restart,body=encode(operation),
                                  headers={"Authorization":"Bearer "+owner,"Content-Type":"application/json"})
            finally:
                transport.close()

            lookup = "ops/services/router/restarts/"+rid
            def receipt_ready():
                try:
                    status = reader.request("GET",lookup)
                except Fault as error:
                    if error.code=="unknown_restart":
                        return None
                    raise
                return status if status["state"]!="accepted" else None

            result = self.wait(receipt_ready)
            self.assertEqual(result["state"],"process_replaced",result)
            after = result["after"]
            self.assertNotEqual(after["pid"],before["pid"])
            self.assertNotEqual(after["generation"],before["generation"])
            self.assertEqual(Launchd().birth(before["pid"]),"")
            self.assertEqual(writable.request("POST",restart,operation),result)
            self.assertEqual(reader.request("GET","ops/services/companion"),neighbor)
            companion_plist = Path(companion["plist"])
            original_plist = companion_plist.read_bytes()
            try:
                settings = plistlib.loads(original_plist)
                companion_plist.write_bytes(plistlib.dumps(dict(settings,RunAtLoad=False)))
                self.assertEqual(other.request("GET","ops/services/companion")["reason"],"service_plist_changed")
                with self.assertRaisesRegex(Fault,"service_generation_unconfirmed"):
                    other.request("POST","ops/services/companion/restart",
                                  dict(operation,request_id=guid(),expected_generation=neighbor["generation"]))
            finally:
                companion_plist.write_bytes(original_plist)
            self.assertEqual(other.request("GET","ops/services/companion"),neighbor)

            def router_ready():
                lines = stdout.read_text().splitlines()
                if len(lines)<2:
                    return None
                self.assertEqual(len(lines),2)
                ready = json.loads(lines[-1])
                client = Client(f"https://127.0.0.1:{ready['port']}",manager,cert)
                return client.request("GET","tasks/"+task["task_id"])["task"],client

            recovered,client = self.wait(router_ready)
            self.assertEqual(recovered,task)
            self.assertEqual(client.request("POST","tasks",create,create_key),task)
            self.assertEqual(len(client.request("GET","tasks")["items"]),1)
            stopped_before = reader.request("GET","ops/services/stopped")
            self.assertEqual((stopped_before["state"],stopped_before["pid"]),("stopped",None))
            restore = Client(url,restorer,cert)
            restart_stopped = {"request_id":guid(),"expected_generation":stopped_before["generation"],
                               "reason":"isolated recovery of a stopped job"}
            first = restore.request("POST","ops/services/stopped/restart",restart_stopped)
            self.assertEqual(first["state"],"accepted")
            def started():
                observed = restore.request("GET","ops/services/stopped/restarts/"+restart_stopped["request_id"])
                return observed if observed["state"]!="accepted" else None

            stopped_after = self.wait(started)
            self.assertEqual(stopped_after["state"],"process_started",stopped_after)
            self.assertEqual(restore.request("POST","ops/services/stopped/restart",restart_stopped),stopped_after)
        try:
            companion_plist.write_bytes(plistlib.dumps(dict(plistlib.loads(original_plist),
                                                          ProgramArguments=["/bin/sleep","110"])))
            with serve(PACKAGE,"host-ops",state,identities,cert,key,env,extra) as url:
                restored = Client(url,owner,cert)
                self.assertEqual(restored.request("GET",lookup),result)
                self.assertEqual(restored.request("POST",restart,operation),result)
                self.assertEqual(restored.request("GET","ops/services/router"),after)
                companion_ops = Client(url,alternate,cert)
                self.assertEqual(companion_ops.request("GET","ops/services/companion")["reason"],"launch_job_mismatch")
        finally:
            companion_plist.write_bytes(original_plist)
        self.evidence = {"operation":result,"router_task_id":task["task_id"],"unchanged_companion_pid":neighbor["pid"],
                         "old_pid_gone":True,"router_task_count":1,"maintenance_requests_applied":1,
                         "changed_plist_rejected":True,"different_loaded_arguments_rejected":True,"receipt_reopened":True,
                         "stopped_job_recovery":stopped_after,
                         "transport":"TLS loopback","fixture":"three isolated macOS user LaunchAgents"}

    @unittest.skipUnless(os.environ.get("MOWULINK_HERMES_TEST_PYTHON") and
                         os.environ.get("MOWULINK_HERMES_TEST_SOURCE"), "independent Hermes test runtime required")
    def test_isolated_hermes_gateway_matches_external_restart_and_native_health(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="mlhgw-",dir="/tmp"))).resolve()
        python = Path(os.environ["MOWULINK_HERMES_TEST_PYTHON"]).absolute()
        source = Path(os.environ["MOWULINK_HERMES_TEST_SOURCE"]).resolve()
        checkout = root/"source"
        revision = tracked_runtime(source,checkout)
        home = root/"profile"
        home.mkdir(mode=0o700)
        entry = root/"gateway-entry.py"
        entry.write_text(GATEWAY_ENTRY,encoding="utf-8")
        key = secrets.token_hex(32)
        config(root,"gateway-auth.json",{"token":key})
        with closing(socket.socket()) as station:
            station.bind(("127.0.0.1",0))
            port = station.getsockname()[1]
        config(home,"config.yaml",{"gateway":{"api_server":{"enabled":True,"host":"127.0.0.1","port":port}}})

        env = {"HOME":str(root),"PATH":os.defpath,"PYTHONDONTWRITEBYTECODE":"1","PYTHONNOUSERSITE":"1",
               "HERMES_HOME":str(home),"HERMES_GATEWAY_LOCK_DIR":str(root/"locks"),"PYTHONPATH":str(checkout),
               "XDG_CACHE_HOME":str(root/"cache"),"XDG_STATE_HOME":str(root/"state"),"TMPDIR":str(root)}
        program = ["/usr/bin/env","-i",*(name+"="+value for name,value in env.items()),python,"-B",entry]
        # The gateway's bootstrap resolves its own private socket paths relative to this directory.
        job,_ = self.enterContext(launchd_job(root,program,"gateway",working_directory=root))
        maintainer,observer = guid(),guid()
        roles = config(root,"ops-identities.json",{maintainer:"host-maintainer/gateway",observer:"host-observer"})
        manifest = config(root,"ops-manifest.json",{"gateway":job})
        cert,tls_key = root/"ops.crt",root/"ops.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
            "-subj","/CN=localhost","-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",tls_key],
            check=True,capture_output=True,timeout=10)
        clean_env = {"HOME":str(root),"PATH":os.defpath,"PYTHONNOUSERSITE":"1","PYTHONDONTWRITEBYTECODE":"1"}

        def native_health(pid):
            with closing(HTTPConnection("127.0.0.1",port,timeout=1)) as gateway:
                try:
                    gateway.request("GET","/health/detailed",headers={"Authorization":"Bearer "+key})
                    response = gateway.getresponse()
                    report = json.load(response)
                    native = json.loads((home/"gateway.pid").read_text())
                except (HTTPException,ValueError,FileNotFoundError):
                    return None
            if (response.status!=200 or report.get("gateway_state")!="running" or
                    report.get("platforms",{}).get("api_server",{}).get("state")!="connected" or
                    native.get("kind")!="hermes-gateway" or native.get("pid")!=pid or report.get("pid")!=pid):
                return None
            return {field:report[field] for field in ("pid","gateway_state","status","active_agents")}

        with serve(PACKAGE,"host-ops",root/"ops.db",roles,cert,tls_key,clean_env,("--manifest",manifest)) as address:
            admin = config(root,"ops-client.json",{"url":address,"token":maintainer,"ca_file":str(cert)})
            viewer = config(root,"ops-reader.json",{"url":address,"token":observer,"ca_file":str(cert)})

            def cli(cfg,*args,code=0):
                run = subprocess.run([sys.executable,"-B","-m","rewrite.host_ops_cli","--connection",cfg,*args],
                                     cwd=PACKAGE,env=clean_env,timeout=35,capture_output=True,text=True)
                self.assertEqual(run.returncode,code,run.stdout+run.stderr)
                return json.loads(run.stdout)

            self.assertEqual([row["service"] for row in cli(viewer,"list")["services"]],["gateway"])
            def ready():
                authority = cli(viewer,"show","gateway")
                if authority["state"]=="running" and (health := native_health(authority["pid"])):
                    return authority,health
                return None

            before,baseline = self.wait(ready,seconds=35)
            with closing(HTTPConnection("127.0.0.1",port,timeout=3)) as gateway:
                gateway.request("GET","/health/detailed")
                response = gateway.getresponse()
                self.assertEqual(response.status,401)
                response.read()
            rid = guid()
            start = ("restart","gateway","--request-id",rid,"--expected-generation",before["generation"],
                     "--reason","isolated Hermes gateway lifecycle proof")
            denied = cli(viewer,*start,code=1)
            self.assertEqual((denied["error"],denied["status"]),("forbidden",403))
            self.assertEqual(cli(viewer,"show","gateway"),before)
            self.assertEqual(native_health(before["pid"]),baseline)

            accepted = cli(admin,*start)
            self.assertEqual((accepted["state"],accepted["request_id"],accepted["before"]),("accepted",rid,before))
            replay = cli(admin,"receipt","gateway",rid,"--wait-seconds","26")
            self.assertEqual(replay["state"],"process_replaced",replay)
            after = replay["after"]
            self.assertNotEqual(after["pid"],before["pid"])
            self.assertNotEqual(after["generation"],before["generation"])
            self.assertEqual(Launchd().birth(before["pid"]),"")
            healthy = self.wait(lambda:native_health(after["pid"]),seconds=35)
            self.assertEqual((baseline["active_agents"],healthy["active_agents"]),(0,0))
            self.assertEqual(cli(admin,*start),replay)
            self.assertEqual(cli(viewer,"show","gateway"),after)
            self.assertEqual(native_health(after["pid"]),healthy)

        with serve(PACKAGE,"host-ops",root/"ops.db",roles,cert,tls_key,clean_env,("--manifest",manifest)) as address:
            recovered = config(root,"ops-reopened.json",{"url":address,"token":maintainer,"ca_file":str(cert)})
            self.assertEqual(cli(recovered,"receipt","gateway",rid),replay)
            self.assertEqual(cli(recovered,*start),replay)
            self.assertEqual(cli(recovered,"show","gateway"),after)
            self.assertEqual(native_health(after["pid"]),healthy)

        if output := os.environ.get("MOWULINK_HERMES_MAINTENANCE_EVIDENCE"):
            evidence = {"hermes_source_revision":revision,"operation":replay,"old_gateway_health":baseline,
                "new_gateway_health":healthy,"old_pid_exited":True,"native_pid_matches_launchd":True,
                "unauthorized_maintenance_status":denied["status"],"unauthenticated_gateway_status":401,
                "restart_submissions_idempotent":True,"receipt_after_maintenance_cold_restart":True,"boundary":"Real Hermes GatewayRunner with its local API adapter, isolated profile and no model or platform credentials."}
            Path(output).write_text(encode(evidence)+"\n",encoding="utf-8")
