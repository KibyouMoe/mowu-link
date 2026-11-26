"""Maintenance authorization, operation recovery, and an isolated real macOS LaunchAgent."""
from contextlib import contextmanager
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import plistlib
import shutil
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from urllib.error import URLError
from urllib.parse import urlsplit

from .host_ops import HostOps, Job, Launchd, roles_allowed
from .release_check import config, serve
from .storage import Fault, encode, guid
from .wire import Client


PACKAGE = Path(__file__).resolve().parent.parent


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

    @contextmanager
    def job(self,root,program,stem,*,run_at_load=True):
        domain = f"gui/{os.getuid()}"
        label = "org.mowulink.test."+stem+"-"+guid()
        target = domain+"/"+label
        plist = root/(stem+".plist")
        output = root/(stem+".stdout")
        plist.write_bytes(plistlib.dumps({"Label":label,"ProgramArguments":list(map(str,program)),"RunAtLoad":run_at_load,
            "WorkingDirectory":str(PACKAGE),"ThrottleInterval":1,"ExitTimeOut":3,
            "StandardOutPath":str(output),"StandardErrorPath":str(root/(stem+".stderr")),
            "EnvironmentVariables":{"PYTHONDONTWRITEBYTECODE":"1","HOME":str(root)}}))
        plist.chmod(0o600)
        subprocess.run(["/bin/launchctl","bootstrap",domain,plist],check=True,capture_output=True,timeout=10)
        try:
            yield {"label":label,"plist":str(plist)},output
        finally:
            subprocess.run(["/bin/launchctl","bootout",target],check=True,capture_output=True,timeout=10)
            self.wait(lambda:subprocess.run(["/bin/launchctl","print",target],capture_output=True,
                                            timeout=5).returncode!=0,seconds=12)

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
        job,stdout = self.enterContext(self.job(root,router_program,"router"))
        companion,_ = self.enterContext(self.job(root,["/bin/sleep","120"],"companion"))
        stopped,_ = self.enterContext(self.job(root,["/bin/sleep","120"],"stopped",run_at_load=False))
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
