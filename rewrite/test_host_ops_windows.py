"""Native Windows service maintenance with private, disposable SCM registrations."""
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
from .release_check import config, serve
from .storage import Fault, encode, guid
from .test_host_ops import HeldQueue
from .wire import Client


if sys.platform=="win32":
    from .host_ops_windows import WindowsJob, WindowsSCM, image_digest

PACKAGE = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform=="win32" and os.environ.get("MOWULINK_TEST_WINDOWS_SCM")=="1",
                     "isolated Windows SCM test runner required")
class WindowsMaintenance(unittest.TestCase):
    def wait(self,read,seconds=24):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                result = read()
            except (URLError,ConnectionError):
                result = None
            if result:
                return result
            time.sleep(.12)
        self.fail("isolated Windows SCM observation timed out")

    def sc(self,*args,check=True):
        sc = Path(os.environ["SystemRoot"])/"System32/sc.exe"
        result = subprocess.run([sc,*args],capture_output=True,text=True,errors="replace",timeout=35)
        if check:
            self.assertEqual(result.returncode,0,f"SCM {args[0]}: {result.stdout}{result.stderr}")
        return result

    @contextmanager
    def service(self,exe,root,name,manager,cert,key,dependency=None):
        tag = "mowulink-ci-"+guid()
        directory = root/name
        directory.mkdir()
        config(directory,"identity-seed.json",{manager:"hermes"})
        arguments = ("-B","-S","-m","rewrite.service","router","--db",directory/"router.db",
                     "--identities",directory/"router-identities.json","--cert",cert,"--key",key)
        (directory/"args.txt").write_text(subprocess.list2cmdline(list(map(str,arguments))),encoding="utf-8")
        (directory/"workdir.txt").write_text(str(PACKAGE),encoding="utf-8")
        argv = list(map(str,(exe,tag,directory,sys.executable)))
        opts = ("depend=",dependency) if dependency else ()
        self.sc("create",tag,"binPath=",subprocess.list2cmdline(argv),"start=","demand","obj=","LocalSystem",*opts)
        item = {"scm_service":tag,"argv":argv,"account":"LocalSystem","sha256":image_digest(exe)}
        try:
            yield item,directory
        finally:
            self.sc("stop",tag,check=False)
            self.wait(lambda:"STOPPED" in self.sc("query",tag,check=False).stdout)
            self.sc("delete",tag)

    def test_windows_scm_keeps_tasks_and_recovers_the_original_receipt(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="mowulink-ops-状态-"))).resolve()
        image = root/"service fixture.exe"
        compiler = Path(os.environ["SystemRoot"])/"Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        tool = subprocess.run([compiler,"/nologo","/target:exe","/reference:System.ServiceProcess.dll",
                               "/out:"+str(image),PACKAGE/"rewrite/testdata/windows_host_service.cs"],
                              capture_output=True,text=True,errors="replace",timeout=45)
        self.assertEqual(tool.returncode,0,tool.stdout+tool.stderr)
        cert,key = root/"tls.pem",root/"tls.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-keyout",key.name,"-out",cert.name],
                       cwd=root,check=True,capture_output=True,timeout=18)
        tokens = {name:guid() for name in ("router","neighbor","stopped","dependent","reader")}

        manager = guid()
        router,router_dir = self.enterContext(self.service(image,root,"router",manager,cert,key))
        neighbor,_ = self.enterContext(self.service(image,root,"neighbor",guid(),cert,key))
        stopped,_ = self.enterContext(self.service(image,root,"stopped",guid(),cert,key))
        dependent,_ = self.enterContext(self.service(image,root,"dependent",guid(),cert,key,
                                                      dependency=neighbor["scm_service"]))
        definitions = {"router":router,"neighbor":neighbor,"stopped":stopped,"dependent":dependent}
        self.sc("start",neighbor["scm_service"])
        self.sc("start",router["scm_service"])
        roles = config(root,"ops-identities.json",{tokens[name]:"host-maintainer/"+name for name in definitions}
                       | {tokens["reader"]:"host-observer"})
        manifest = config(root,"ops-manifest.json",definitions)
        state = root/"maintainer.db"
        native,job = WindowsSCM(),WindowsJob.read(router)
        with self.assertRaisesRegex(Fault,"windows_service_image_digest_mismatch"):
            WindowsJob.read(dict(router,sha256="0"*64))
        wrong = WindowsJob.read(dict(router,argv=[*router["argv"],"--different"]))
        self.assertEqual(native.observe(wrong)["reason"],"windows_service_configuration_mismatch")
        env = {**os.environ,"PYTHONDONTWRITEBYTECODE":"1"}
        extra = ("--manifest",manifest)
        base = "ops/services/router"

        def router_client(run_number):
            log = router_dir/"router.stdout"
            if not log.exists() or len(lines:=log.read_text().splitlines())<run_number:
                return None
            ready = json.loads(lines[-1])
            client = Client("https://127.0.0.1:"+str(ready["port"]),manager,cert)
            client.request("GET","tasks")
            return client

        def receipt(api,action,route=base,*,settled=True):
            try:
                result = api.request("GET",route+"/restarts/"+action["request_id"])
            except Fault as error:
                if error.code=="unknown_restart":
                    return None
                raise
            return result if not settled or result["state"]!="accepted" else None

        with serve(PACKAGE,"host-ops",state,roles,cert,key,env,extra) as url:
            reader,owner = Client(url,tokens["reader"],cert),Client(url,tokens["router"],cert)
            before = owner.request("GET",base)
            self.assertEqual(before["state"],"running",before)
            nearby = reader.request("GET","ops/services/neighbor")
            self.assertEqual(nearby["state"],"running",nearby)
            unsafe = Client(url,tokens["dependent"],cert)
            self.assertEqual(unsafe.request("GET","ops/services/dependent")["reason"],"windows_service_has_side_effects")
            request = {"request_id":guid(),"expected_generation":before["generation"],"reason":"test native Windows restart"}
            with self.assertRaisesRegex(Fault,"service_generation_unconfirmed"):
                unsafe.request("POST","ops/services/dependent/restart",request)
            with self.assertRaisesRegex(Fault,"forbidden"):
                reader.request("POST",base+"/restart",request)
            with self.assertRaisesRegex(Fault,"stale_service_generation"):
                owner.request("POST",base+"/restart",dict(request,expected_generation="0"*64))
            first = self.wait(lambda:router_client(1))
            child_before = int((router_dir/"child.pid").read_text())
            message = {"title":"Keep the isolated task","instructions":"Read after the Windows SCM restart"}
            creation_key = guid()
            order = first.request("POST","tasks",message,creation_key)
            target = urlsplit(url)
            delivery = HTTPSConnection(target.hostname,target.port,timeout=6,
                                       context=ssl.create_default_context(cafile=cert))
            try:
                delivery.request("POST","/v1/"+base+"/restart",body=encode(request).encode(),
                                 headers={"Authorization":"Bearer "+tokens["router"],"Content-Type":"application/json"})
                # Keep the request alive until an independent reader proves it reached the ledger.
                self.wait(lambda:receipt(reader,request,settled=False),seconds=12)
            finally:
                delivery.close()
            completed = self.wait(lambda:receipt(reader,request),seconds=45)
            self.assertEqual(completed["state"],"process_replaced",completed)
            after = completed["after"]
            self.assertNotEqual(after["pid"],before["pid"])
            self.assertEqual(native.birth(before["pid"]),"")
            self.assertEqual(owner.request("POST",base+"/restart",request),completed)
            recovered = self.wait(lambda:router_client(2))
            child_after = int((router_dir/"child.pid").read_text())
            self.assertNotEqual(child_before,child_after)
            self.assertEqual(native.birth(child_before),"")
            self.assertEqual(recovered.request("POST","tasks",message,creation_key),order)
            self.assertEqual(len(recovered.request("GET","tasks")["items"]),1)
            self.assertEqual(reader.request("GET","ops/services/neighbor"),nearby)

            standby = Client(url,tokens["stopped"],cert)
            standby_base = "ops/services/stopped"
            stopped_before = standby.request("GET",standby_base)
            self.assertEqual(stopped_before["state"],"stopped",stopped_before)
            resume = dict(request,request_id=guid(),expected_generation=stopped_before["generation"])
            standby.request("POST",standby_base+"/restart",resume)
            started = self.wait(lambda:receipt(standby,resume,standby_base))
            self.assertEqual(started["state"],"process_started",started)

        queue = HeldQueue()
        interrupted = HostOps(state,{"router":job},native,queue)
        pending = dict(request,request_id=guid(),expected_generation=after["generation"])
        interrupted.begin("host-maintainer/router","router",pending)
        self.assertEqual(len(queue.calls),1)
        original = base+"/restarts/"+pending["request_id"]
        with serve(PACKAGE,"host-ops",state,roles,cert,key,env,extra) as url:
            owner,reader = Client(url,tokens["router"],cert),Client(url,tokens["reader"],cert)
            self.assertEqual(owner.request("GET",original)["state"],"unconfirmed")
            with self.assertRaisesRegex(Fault,"service_restart_unconfirmed"):
                owner.request("POST",base+"/restart",dict(pending,request_id=guid()))
            manual = native.restart(job,after)
            self.assertIsNotNone(manual)
            settled = owner.request("POST",original+"/reconcile",{"expected_generation":manual["generation"]})
            self.assertEqual(settled["state"],"unconfirmed")
            self.assertEqual(settled["reconciliation"]["after"]["pid"],manual["pid"])
            self.assertEqual(owner.request("POST",base+"/restart",pending),settled)
            self.assertEqual(native.observe(job),manual)
            self.assertEqual(reader.request("GET","ops/services/neighbor"),nearby)
            third = self.wait(lambda:router_client(3))
            self.assertEqual(third.request("GET","tasks/"+order["task_id"])["task"],order)
        with serve(PACKAGE,"host-ops",state,roles,cert,key,env,extra) as url:
            owner = Client(url,tokens["router"],cert)
            self.assertEqual(owner.request("GET",original),settled)
            self.assertEqual(owner.request("POST",base+"/restart",pending),settled)
            self.assertEqual(native.observe(job),manual)
        if output := os.environ.get("MOWULINK_WINDOWS_MAINTENANCE_EVIDENCE"):
            Path(output).write_text(json.dumps({
                "runtime":sys.getwindowsversion().build,"task_id":order["task_id"],"router_task_count":1,
                "old_router_pid":child_before,"new_router_pid":child_after,"scm_restart":completed,
                "stopped_service":started,"uncertain_reconciliation":settled,"neighbor_pid_unchanged":nearby["pid"],
                "router_runs":3,"persisted_receipt":True,"original_post_response_read":False,
                "scope":"Actual Windows SCM and HTTPS, temporary SCM wrapper and real isolated Router.",
            },indent=2)+"\n")

    def test_packaged_router_service_installs_upgrades_and_rolls_back(self):
        from .release import build, materialize
        from .storage import state_writer

        repository = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
        target = os.environ["GITHUB_SHA"]
        previous = "1dc8ab382ea8d03fe99c97fb8bd3d9da88a17b38"
        self.assertNotEqual(previous,target)
        root = Path(self.enterContext(TemporaryDirectory(prefix="mowulink-scm-release-"))).resolve()
        cert,key = root/"tls.pem",root/"tls.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-keyout",key.name,"-out",cert.name],
                       cwd=root,check=True,capture_output=True,timeout=18)
        manager = guid()
        identities = config(root,"router-identities.json",{manager:"hermes"})
        icacls = Path(os.environ["SystemRoot"])/"System32/icacls.exe"
        subprocess.run([icacls,identities,"/grant:r","*S-1-5-18:F","*S-1-5-32-544:F","/inheritancelevel:r"],
                       check=True,capture_output=True,timeout=10)
        subprocess.run([icacls,identities,"/setowner","*S-1-5-18"],check=True,capture_output=True,timeout=10)
        db = root/"router.db"
        tag = "mowulink-ci-"+guid()
        packages = []
        for i,revision in enumerate((previous,target)):
            release = build(repository,revision,root/f"archive-{i}")
            archive,checksum = Path(release["archive"]),release["sha256"]
            installed = root/f"package-{i}"
            materialize(archive,revision,checksum,installed,install=True)
            launcher = root/f"launcher-{i}"
            args = ["--archive",archive,"--revision",revision,"--sha256",checksum,"--package",installed,
                    "--destination",launcher,"--service",tag,"--account","LocalSystem","--python",sys.executable,
                    "--db",db,"--identities",identities,"--cert",cert,"--key",key]
            command = [sys.executable,"-B","-S","-m","rewrite.windows_router_service"]
            if i==0:
                wrong = args.copy()
                wrong[wrong.index("--sha256")+1] = "0"*64
                rejected = subprocess.run([*command,*wrong],cwd=PACKAGE,capture_output=True,text=True,timeout=20)
                self.assertNotEqual(rejected.returncode,0)
                self.assertIn("release checksum mismatch",rejected.stderr)
                self.assertFalse(launcher.exists())
            staged = subprocess.run([*command,*args],cwd=PACKAGE,capture_output=True,text=True,timeout=65)
            self.assertEqual(staged.returncode,0,staged.stderr)
            plan = json.loads(staged.stdout)
            job = WindowsJob.read(plan["maintenance"])
            packages.append((archive,revision,checksum,installed,plan,job))

        old,new = packages
        native = WindowsSCM()
        ready_path = Path(old[4]["readiness"])
        self.assertEqual(new[4]["readiness"],str(ready_path))
        self.assertFalse(db.exists())
        self.sc("create",*old[4]["scm_create_args"])
        try:
            counts,runs = [],[]
            original_key = guid()
            message = {"title":"Original Windows service queue head","instructions":"Read this after the version switch"}

            def start_and_observe(job):
                def ready():
                    service = native.observe(job)
                    if service["state"]!="running" or not ready_path.exists():
                        return None
                    return service,json.loads(ready_path.read_text(encoding="utf-8"))

                self.sc("start",tag)
                return self.wait(ready)

            def wait_stopped(job,service,router):
                return self.wait(lambda:native.observe(job)["state"]=="stopped" and native.birth(service["pid"])==""
                                 and native.birth(router["pid"])=="" and not ready_path.exists())

            with state_writer(db):
                self.sc("start",tag,check=False)
                self.wait(lambda:"STOPPED" in self.sc("query",tag).stdout)
                self.assertFalse(ready_path.exists())
            for name,release in (("previous",old),("trial",new),("rollback",old)):
                _,revision,_,_,plan,job = release
                if name!="previous":
                    self.sc("config",*plan["scm_create_args"][:3])
                self.assertEqual(native.observe(job)["state"],"stopped")
                service,router = start_and_observe(job)
                client = Client("https://127.0.0.1:"+str(router["port"]),manager,cert)
                task = client.request("POST","tasks",message,original_key)
                if name=="previous":
                    original_task = task
                self.assertEqual(task,original_task)
                self.assertEqual(client.request("GET","tasks/"+task["task_id"])["task"],original_task)
                count = len(client.request("GET","tasks")["items"])
                self.assertEqual(count,1)
                counts.append(count)
                self.assertTrue(native.birth(router["pid"]))
                self.sc("stop",tag)
                wait_stopped(job,service,router)
                runs.append({"phase":name,"revision":revision,"service_pid":service["pid"],
                             "router_pid":router["pid"],"launcher_sha256":job.digest,"process_exits_observed":True})
            service,router = start_and_observe(job)
            kill = Path(os.environ["SystemRoot"])/"System32/taskkill.exe"
            subprocess.run([kill,"/F","/PID",str(router["pid"])],check=True,capture_output=True,timeout=12)
            wait_stopped(job,service,router)
            recovered_service,recovered_router = start_and_observe(job)
            recovered = Client("https://127.0.0.1:"+str(recovered_router["port"]),manager,cert)
            self.assertEqual(recovered.request("POST","tasks",message,original_key),original_task)
            self.assertEqual(len(recovered.request("GET","tasks")["items"]),1)
            self.sc("stop",tag)
            wait_stopped(job,recovered_service,recovered_router)
        finally:
            self.sc("stop",tag,check=False)
            self.wait(lambda:"STOPPED" in self.sc("query",tag,check=False).stdout)
            self.sc("delete",tag)
        self.assertNotEqual(self.sc("query",tag,check=False).returncode,0)
        for archive,revision,checksum,installed,_,_ in packages:
            materialize(archive,revision,checksum,installed,install=False)
        if output := os.environ.get("MOWULINK_WINDOWS_ROUTER_RELEASE_EVIDENCE"):
            Path(output).write_text(json.dumps({"windows_build":sys.getwindowsversion().build,"service":tag,
                "runs":runs,"archive_sha256":[old[2],new[2]],"task_id":original_task["task_id"],
                "task_counts":counts,"refused_bad_checksum":True,"refused_second_writer":True,"service_removed":True,
                "unexpected_exit":{"service_pid":service["pid"],"router_pid":router["pid"],
                    "reported_stopped":True,"recovered_pid":recovered_router["pid"],"original_task_preserved":True},
                "scope":"Actual disposable Windows SCM, installed releases, HTTPS Router and SQLite."},
                indent=2)+"\n",encoding="utf-8")
