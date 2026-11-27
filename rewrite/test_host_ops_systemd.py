"""Native user-manager rehearsal on an isolated Linux guest."""
from contextlib import contextmanager
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from urllib.error import URLError
from urllib.parse import urlsplit

from .host_ops import HostOps
from .host_ops_systemd import Systemd, SystemdJob, unit_digest
from .release_check import config, serve
from .storage import Fault, encode, guid
from .test_host_ops import HeldQueue
from .wire import Client


PACKAGE = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform=="linux" and os.environ.get("MOWULINK_TEST_USER_SYSTEMD")=="1",
                     "isolated Linux systemd user manager required")
class LinuxMaintenance(unittest.TestCase):
    def require_idle_test_manager(self):
        for options in (("list-units","--type=service","--state=active,activating,deactivating","--plain"),
                        ("list-jobs",)):
            owned = subprocess.check_output(["/usr/bin/systemctl","--user",*options,"--no-legend"],timeout=6)
            self.assertFalse(owned.strip(),"the test requires a dedicated user manager without active services or jobs")

    def reload_test_manager(self):
        self.require_idle_test_manager()
        subprocess.run(["/usr/bin/systemctl","--user","daemon-reload"],check=True,capture_output=True,timeout=12)

    @contextmanager
    def inactive_unit(self):
        self.require_idle_test_manager()
        label = "mowulink-maintenance-stopped-"+guid()+".service"
        units = Path("/run/user")/str(os.getuid())/"systemd/user"
        created = not units.is_dir()
        units.mkdir(mode=0o700,exist_ok=True)
        path = units/label
        argv = ["/usr/bin/sleep","180"]
        with path.open("x",encoding="utf-8") as config_file:
            os.fchmod(config_file.fileno(),0o600)
            config_file.write("[Service]\nType=exec\nKillMode=control-group\nExecStart="+" ".join(argv)+"\n")
        try:
            self.reload_test_manager()
            yield {"unit":label,"file":str(path),"argv":argv,"sha256":unit_digest(path)}
        finally:
            try:
                subprocess.run(["/usr/bin/systemctl","--user","stop",label],check=True,capture_output=True,timeout=12)
            finally:
                path.unlink()
                if created and not any(units.iterdir()):
                    units.rmdir()
                self.reload_test_manager()

    def wait(self, read, seconds=15):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                result = read()
            except (URLError,ConnectionError):
                result = None
            if result:
                return result
            time.sleep(.1)
        self.fail("isolated systemd observation timed out")

    @contextmanager
    def unit(self, root, name, argv, *properties):
        label = "mowulink-maintenance-"+name+"-"+guid()+".service"
        log = root/(name+".stdout")
        subprocess.run(["/usr/bin/systemd-run","--user","--quiet","--unit="+label,"--property=Type=exec",
                        "--property=WorkingDirectory="+str(PACKAGE),"--property=StandardOutput=append:"+str(log),
                        "--property=StandardError=append:"+str(root/(name+".stderr")),
                        *("--property="+prop for prop in properties),"--",*map(str,argv)],
                       check=True,capture_output=True,timeout=12)
        try:
            result = subprocess.run(["/usr/bin/systemctl","--user","show","--value","--property=FragmentPath",label],
                                    check=True,text=True,capture_output=True,timeout=6)
            path = Path(result.stdout.strip())
            yield {"unit":label,"file":str(path),"argv":list(map(str,argv)),"sha256":unit_digest(path)},log
        finally:
            subprocess.run(["/usr/bin/systemctl","--user","stop",label],check=True,timeout=12,capture_output=True)

    def test_native_router_restart_readback_and_unconfirmed_recovery(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="mowulink-linux-maintenance-")))
        cert,key = root/"local.pem",root/"local.key"
        subprocess.run(["/usr/bin/openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                        "-subj","/CN=localhost","-addext","subjectAltName=IP:127.0.0.1","-keyout",key,"-out",cert],
                       check=True,capture_output=True,timeout=15)
        stopped = self.enterContext(self.inactive_unit())
        router_token,writer,reader,companion_token,restorer = (guid() for _ in range(5))
        identities = config(root,"router-identities.json",{router_token:"hermes"})
        argv = [sys.executable,"-B","-S","-m","rewrite.service","router","--db",root/"router.db",
                "--identities",identities,"--cert",cert,"--key",key]
        router,log = self.enterContext(self.unit(root,"router",argv))
        neighbor,_ = self.enterContext(self.unit(root,"neighbor",[sys.executable,"-c","import time; time.sleep(180)"]))
        fixture,_ = self.enterContext(self.unit(root,"dependency",["/usr/bin/sleep","180"]))
        dependent,_ = self.enterContext(self.unit(root,"dependent",["/usr/bin/sleep","180"],"Requires="+fixture["unit"]))
        definitions = {"router":router,"neighbor":neighbor,"dependent":dependent,"stopped":stopped}
        manifest = config(root,"maintenance.json",definitions)
        roles = config(root,"roles.json",{writer:"host-maintainer/router",reader:"host-observer",
                                           companion_token:"host-maintainer/dependent",restorer:"host-maintainer/stopped"})
        native = Systemd()
        job = SystemdJob.read(router)
        with self.assertRaisesRegex(Fault,"systemd_unit_digest_mismatch"):
            SystemdJob.read(dict(router,sha256="0"*64))
        mismatch = SystemdJob.read(dict(router,argv=[*router["argv"],"--drain-dispatch"]))
        self.assertEqual(native.observe(mismatch)["reason"],"systemd_command_mismatch")
        state = root/"maintenance.db"
        runtime = {**os.environ,"PYTHONDONTWRITEBYTECODE":"1"}
        extras = ("--manifest",manifest)
        base = "ops/services/router"

        def router_client(minimum):
            if not log.exists() or len(lines:=log.read_text().splitlines())<minimum:
                return None
            values = json.loads(lines[-1])
            client = Client("https://127.0.0.1:"+str(values["port"]),router_token,cert)
            client.request("GET","tasks")
            return client

        def receipt(api, request, route=base):
            try:
                row = api.request("GET",route+"/restarts/"+request["request_id"])
            except Fault as fault:
                if fault.code=="unknown_restart":
                    return None
                raise
            return row if row["state"]!="accepted" else None

        with serve(PACKAGE,"host-ops",state,roles,cert,key,runtime,extras) as url:
            owner,observer = Client(url,writer,cert),Client(url,reader,cert)
            unsafe = Client(url,companion_token,cert)
            before = owner.request("GET",base)
            self.assertEqual(before["state"],"running",before)
            nearby = observer.request("GET","ops/services/neighbor")
            self.assertEqual(nearby["state"],"running",nearby)
            self.assertEqual(unsafe.request("GET","ops/services/dependent")["reason"],"systemd_unit_has_side_effects")
            request = {"request_id":guid(),"expected_generation":before["generation"],"reason":"test Linux restart"}
            with self.assertRaisesRegex(Fault,"service_generation_unconfirmed"):
                unsafe.request("POST","ops/services/dependent/restart",request)
            with self.assertRaisesRegex(Fault,"forbidden"):
                observer.request("POST",base+"/restart",request)
            with self.assertRaisesRegex(Fault,"stale_service_generation"):
                owner.request("POST",base+"/restart",dict(request,expected_generation="0"*64))
            original_router = self.wait(lambda:router_client(1))
            message = {"title":"Retain the test task","instructions":"Use the existing offline queue"}
            creation_key = guid()
            task = original_router.request("POST","tasks",message,creation_key)
            endpoint = urlsplit(url)
            delivery = HTTPSConnection(endpoint.hostname,endpoint.port,
                                       context=ssl.create_default_context(cafile=cert),timeout=4)
            try:
                delivery.request("POST","/v1/"+base+"/restart",body=encode(request),
                                 headers={"Authorization":"Bearer "+writer,"Content-Type":"application/json"})
            finally:
                delivery.close()
            completed = self.wait(lambda:receipt(observer,request))
            self.assertEqual(completed["state"],"process_replaced",completed)
            after = completed["after"]
            self.assertNotEqual(after["pid"],before["pid"])
            self.assertEqual(native.birth(before["pid"]),"")
            self.assertEqual(owner.request("POST",base+"/restart",request),completed)
            self.assertEqual(observer.request("GET","ops/services/neighbor"),nearby)
            recovered = self.wait(lambda:router_client(2))
            self.assertEqual(recovered.request("GET","tasks/"+task["task_id"])["task"],task)
            self.assertEqual(recovered.request("POST","tasks",message,creation_key),task)
            self.assertEqual(len(recovered.request("GET","tasks")["items"]),1)
            recovery = Client(url,restorer,cert)
            stopped_path = "ops/services/stopped"
            before_stop = recovery.request("GET",stopped_path)
            self.assertEqual(before_stop["state"],"stopped",before_stop)
            recovery_request = dict(request,request_id=guid(),expected_generation=before_stop["generation"])
            recovery.request("POST",stopped_path+"/restart",recovery_request)
            restored = self.wait(lambda:receipt(recovery,recovery_request,stopped_path))
            self.assertEqual(restored["state"],"process_started",restored)
            self.assertEqual(recovery.request("POST",stopped_path+"/restart",recovery_request),restored)

        # Seed an accepted operation and stop before the supervisor receives it.
        queue = HeldQueue()
        interrupted = HostOps(state,{"router":job},native,queue)
        pending = dict(request,request_id=guid(),expected_generation=after["generation"])
        interrupted.begin("host-maintainer/router","router",pending)
        self.assertEqual(len(queue.calls),1)
        original_path = base+"/restarts/"+pending["request_id"]
        with serve(PACKAGE,"host-ops",state,roles,cert,key,runtime,extras) as url:
            owner,observer = Client(url,writer,cert),Client(url,reader,cert)
            self.assertEqual(observer.request("GET",original_path)["state"],"unconfirmed")
            with self.assertRaisesRegex(Fault,"service_restart_unconfirmed"):
                owner.request("POST",base+"/restart",dict(pending,request_id=guid()))
            with self.assertRaisesRegex(Fault,"replacement_not_observed"):
                owner.request("POST",original_path+"/reconcile",{"expected_generation":after["generation"]})
            manual = native.restart(job,after)
            self.assertIsNotNone(manual)
            settled = owner.request("POST",original_path+"/reconcile",{"expected_generation":manual["generation"]})
            self.assertEqual(settled["state"],"unconfirmed")
            self.assertEqual(settled["reconciliation"]["after"]["pid"],manual["pid"])
            self.assertEqual(owner.request("POST",base+"/restart",pending),settled)
            self.assertEqual(native.observe(job),manual)
            self.assertEqual(observer.request("GET","ops/services/neighbor"),nearby)
            recovered = self.wait(lambda:router_client(3))
            self.assertEqual(recovered.request("POST","tasks",message,creation_key),task)
            self.assertEqual(len(log.read_text().splitlines()),3)
        with serve(PACKAGE,"host-ops",state,roles,cert,key,runtime,extras) as url:
            owner = Client(url,writer,cert)
            self.assertEqual(owner.request("GET",original_path),settled)
            self.assertEqual(owner.request("POST",base+"/restart",pending),settled)
            self.assertEqual(native.observe(job),manual)
        if output := os.environ.get("MOWULINK_LINUX_MAINTENANCE_EVIDENCE"):
            Path(output).write_text(json.dumps({
                "systemd":subprocess.check_output(["/usr/bin/systemctl","--version"],text=True).splitlines()[0],
                "original_task":task["task_id"],"reported_restart":completed,"recovered_uncertain_restart":settled,
                "stopped_unit_receipt":restored,"same_receipt_after_daemon_restart":True,
                "other_unit_pid_unchanged":nearby["pid"],"policy":["wrong file digest and argv rejected","unit dependencies rejected",
                    "observer cannot restart","stale generation rejected","fresh key refused while outcome unknown"],
                "router_runs":3,"router_task_count":1,"scope":"Actual user systemd and loopback HTTPS on an isolated Linux guest.",
            },indent=2)+"\n")
