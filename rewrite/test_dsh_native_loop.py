"""Run the installed DSH AgentLoop with a local deterministic provider and a fresh profile."""
import csv
import hashlib
from io import BytesIO
import json
from contextlib import closing, contextmanager
from http.server import ThreadingHTTPServer
import os
import shutil
from pathlib import Path
import select
from queue import Empty, Queue
import shlex
import socket
import sqlite3
import ssl
import subprocess
import tarfile
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from . import test_codex_app_server as codex_driver, worker_commands, worker_resume
from .adapter import HostAdapter
from .approvals import TTL_SECONDS
from .codex_app_server import AppServerHost, worker_callbacks
from .codex_display import CodexDisplay
from .devloop import FixtureHost, Loop
from .dsh_display import DshDisplay
from .dsh_local_link import LocalLink
from .dsh_port import DshActivation, DshHost, tool_listener
from .dsh_session_family import DshPreparation
from .feishu_inbound import Inbound
from .feishu_output import Views
from .host_wire import RemoteAdapter, serve_adapter
from .native_files import NativeFiles
from .orders import Orders
from .release_check import config, serve
from .question_cards import Actions
from .state_bundle import agent_pending, audit_handoff, capture, filename, restore, rollback, stage_native_handoff, verify
from .storage import Fault, encode, guid, state_writer
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .trigger import Trigger
from .test_dsh_sdk import APP, EXE
from .test_feishu_inbound import PNG
from .wire import Client, route, serve_authenticated, serve_loopback
from .worker import Worker
from .worker_service import WorkerService


FIXTURE = Path(__file__).parent/"integrations/dsh_host/native-loop-fixture.mjs"


def environment(directory):
    env = {"ELECTRON_RUN_AS_NODE":"1","HOME":directory,"TMPDIR":directory,
           "XDG_CONFIG_HOME":directory,"PATH":os.defpath}
    if os.name=="nt":
        standard = ("SystemRoot","WINDIR","PATH","COMSPEC","PATHEXT","ProgramFiles","ProgramData","SystemDrive",
                    "USERPROFILE","USERNAME","USERDOMAIN","HOMEDRIVE","HOMEPATH","LOCALAPPDATA","APPDATA","PSModulePath")
        env.update({key:os.environ[key] for key in standard if key in os.environ})
        env.update({"DSH_HOME":directory,"TEMP":directory,"TMP":directory})
    return env


def observe():
    with tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-loop-") as directory:
        result = subprocess.run([EXE,FIXTURE.resolve(),APP/"Resources/app.asar/dsh",directory,"dsh-isolated-real-loop"],
                                env=environment(directory),cwd=directory,timeout=12,text=True,capture_output=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)


class NativeProcess(unittest.TestCase):
    def read_native(self,native,command=None):
        if command is not None:
            native.stdin.write(command+"\n")
            native.stdin.flush()
        if os.name=="nt":
            output = Queue()
            reader = Thread(target=lambda:output.put(native.stdout.readline()),daemon=True)
            reader.start()
            try:
                line = output.get(timeout=30)
            except Empty:
                native.kill()
                reader.join(3)
                self.fail("DSH did not return a native observation")
        else:
            self.assertTrue(select.select([native.stdout],[],[],10)[0],"DSH did not return a native observation")
            line = native.stdout.readline()
        self.assertTrue(line,"DSH fixture exited without a native observation")
        return json.loads(line)

    @contextmanager
    def start_native(self,directory,session,mode,*,fixture=FIXTURE,use_tls=False):
        exe, root = EXE,APP/"Resources/app.asar/dsh"
        env = environment(str(directory))
        if os.name=="nt":
            exe = Path(os.environ["MOWULINK_TEST_DSH_BINARY"])
            root = exe.parent/"resources/app.asar/dsh"
        args = [exe,fixture.resolve(),root,directory,session,mode]
        if use_tls:
            args.append("tls")
        with subprocess.Popen(args,env=env,cwd=directory,stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8") as native:
            try:
                self.assertEqual(self.read_native(native),{"ready":True,"session":session})
                yield native
            finally:
                try:
                    tail,errors = native.communicate("exit\n" if native.poll() is None else None,timeout=6)
                except subprocess.TimeoutExpired:
                    native.kill()
                    native.communicate()
                    raise
                self.assertEqual(native.returncode,0,tail+errors)

    def open_worker(self,service,identities):
        server = self.enterContext(service.listen(identities,("127.0.0.1",0),None))
        thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:"+str(server.server_port)

    def formal_report(self,directory,*,use_tls=False):
        session = "dsh-real-loop-formal"
        link = LocalLink(directory,use_tls=use_tls)
        class LateDeliveryReceipt(DshHost):
            def deliver(self,actual_session,did,payload,digest):
                proof = super().deliver(actual_session,did,payload,digest)
                return None if payload["kind"]=="task" else proof
        def host(_path,_name):
            port = LateDeliveryReceipt(directory,link=link)
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        credential = guid()
        service = WorkerService(loop.worker,clock)
        client = Client(self.open_worker(service,{credential:"adapter/oguri"}),credential)
        self.enterContext(tool_listener(directory,loop.adapter,client,link=link))
        router_requests = []
        with self.start_native(directory,session,"formal",use_tls=use_tls) as native:
            if use_tls:
                self.assert_client_certificate_required(link)
                with self.assertRaisesRegex(Fault,"wrong_native_session"):
                    link_host = DshHost(directory,link=link)
                    link_host.request("activity","foreign-session")
            tid = loop.hermes.create(guid(),"Native pipeline test","Report a local fixture result","oguri")["task_id"]
            send = loop.worker.router.request
            def observe_report(method,path,*args,**kwargs):
                if path!=f"tasks/{tid}/report":
                    return send(method,path,*args,**kwargs)
                before = {"router":loop.hermes.inspect(tid)["task"],"worker":loop.worker.hold()}
                proof = send(method,path,*args,**kwargs)
                router_requests.append({"before":before,"receipt":proof})
                return proof
            loop.worker.router.request = observe_report
            loop.worker.connect()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            observed = self.read_native(native,"read")
            with loop.adapter.db.transaction() as db:
                intent = db.execute("SELECT * FROM intents WHERE json_extract(payload,'$.task_id')=?",(tid,)).fetchone()
            receipt = json.loads(intent["receipt"])
            # Reconstruct the host receipt after dropping the adapter's copy. DSH must not insert a new input.
            with loop.adapter.db.transaction() as db:
                db.execute("UPDATE intents SET receipt=NULL,native_id=NULL")
            self.assertEqual(loop.adapter.deliver(intent["delivery_id"],json.loads(intent["payload"])),receipt)
            self.assertEqual(self.read_native(native,"read"),observed)
        cold = None
        if use_tls:
            with loop.adapter.db.transaction() as db:
                db.execute("UPDATE intents SET receipt=NULL,native_id=NULL WHERE delivery_id=?",(receipt["delivery_id"],))
            payload = json.loads(intent["payload"])
            with self.assertRaisesRegex(Fault,"adapter_connection_unconfirmed"):
                loop.adapter.deliver(receipt["delivery_id"],payload)
            with loop.adapter.db.transaction() as db:
                pending = db.execute("SELECT receipt FROM intents WHERE delivery_id=?",(receipt["delivery_id"],)).fetchone()
            self.assertIsNone(pending["receipt"])
            with self.start_native(directory,session,"formal-reopen",use_tls=True) as reopened:
                self.assertEqual(loop.adapter.deliver(receipt["delivery_id"],payload),receipt)
                cold = self.read_native(reopened,"read")
                self.assertEqual({**cold,"modelRequests":observed["modelRequests"]},observed)
                self.assertEqual(cold["modelRequests"],[])
        details = loop.hermes.inspect(tid)
        self.assertEqual(details["task"]["state"],"completed_reported")
        self.assertEqual(observed["inputs"],[receipt["native_id"]])
        report = details["last_report"]
        aid = report["assignment_id"]
        self.assertEqual(json.loads(intent["payload"])["assignment_id"],aid)
        self.assertEqual([r["name"] for r in observed["calls"]],["worker_report","worker_report"])
        self.assertEqual([json.loads(r["arguments"])["assignment_id"] for r in observed["calls"]],[aid,aid])
        expected = {"recorded":True,"task_state":"completed_reported","release_assignment_id":aid}
        request, = router_requests
        self.assertEqual(request["before"]["router"]["state"],"working")
        self.assertEqual(request["before"]["worker"],{"held_task_id":tid,"held_assignment_id":aid})
        self.assertEqual(request["receipt"],expected)
        self.assertEqual([r["toolCallId"] for r in observed["results"]],[c["callId"] for c in observed["calls"]])
        self.assertEqual([r["isError"] for r in observed["results"]],[False,False],observed["results"])
        self.assertEqual([json.loads(r["content"][0]["text"]) for r in observed["results"]],[expected,expected])
        self.assertEqual(loop.worker.hold(),{"held_task_id":None,"held_assignment_id":None})
        with loop.worker.db.transaction() as db:
            notice, = db.execute("SELECT payload FROM display_events WHERE event_id=?",
                                 ("task-"+receipt["delivery_id"],)).fetchall()
        self.assertIn(tid,json.loads(notice["payload"])["text"])
        notified = [f["delivery_id"] for f in loop.manager_wire.frames(0) if f["payload"].get("text")==report["summary"]]
        self.assertEqual(len(notified),1)
        self.evidence = {"native":observed,"input_receipt":receipt,"router":details,
                         "router_w04":request,"notification_delivery_ids":notified,"worker_after":loop.worker.hold(),
                         "boundary":"Native DSH AgentLoop, tool execution and real isolated Router/Worker; scripted local model"}
        if cold is not None:
            self.evidence["cold_restart"] = {"offline_receipt_unconfirmed":True,"same_receipt":True,
                                             "native_inputs":len(cold["inputs"]),"new_model_calls":0}


    def assert_client_certificate_required(self,link):
        from http.client import HTTPSConnection

        trust = ssl.create_default_context(cafile=link.directory/"dsh-link.crt")
        for role,path in [("host","/host"),("worker","/tool")]:
            with self.subTest(role=role), closing(HTTPSConnection("127.0.0.1",link.ports[role+"_port"],
                                                                  context=trust,timeout=3)) as no_identity:
                with self.assertRaises(OSError):
                    no_identity.request("POST",path,body=b"{}")
                    no_identity.getresponse()

    def goal_worker(self,session,extra_workers=(),*,use_tls=False):
        directory = (self.local_tls_directory() if use_tls else
                     Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-goals-"))))
        link = LocalLink(directory,use_tls=use_tls)
        def host(_path,_name):
            port = DshHost(directory,link=link)
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host,extra_workers=extra_workers))
        service = WorkerService(loop.worker,clock)
        key = guid()
        platform = Client(self.open_worker(service,{key:"platform/oguri"}),key)
        def command(mid,text):
            receipt = platform.request("POST","worker/command",{"message_id":mid,"text":text})["receipt"]
            items = platform.request("GET","worker/display")["items"]
            event = next(item["event"] for item in items if item["seq"]==receipt["output"]["seq"])
            return receipt,event["text"]
        return str(directory),loop,service,command

    def read_only_cleanup(self,*,use_tls=False):
        cases = []
        for action in ("cancel","transfer"):
            with self.subTest(action=action):
                session = "dsh-read-only-"+action
                directory,loop,service,_command = self.goal_worker(session,extra_workers=("tama",),use_tls=use_tls)
                token = guid()
                client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
                w = loop.worker
                alternate = FixtureHost(loop.root/"alternate.db","alternate")
                other = Worker(loop.root/"second-worker.db","tama",loop.extra_clients["tama"],
                               HostAdapter(loop.root/"second-adapter.db",alternate,"alternate"),clock=service.clock)
                tid = loop.hermes.create(guid(),"Scope native stop","Preserve exact assignment scope","oguri")["task_id"]
                original_request = w.router.request
                cleanup_receipts = []
                def lose_cleanup_reply(method,route,*args,**kwargs):
                    reply = original_request(method,route,*args,**kwargs)
                    if method=="POST" and route==f"tasks/{tid}/report" and args[0].get("outcome")=="cancel_confirmed":
                        cleanup_receipts.append(reply)
                        if len(cleanup_receipts)==1:
                            raise Fault(503,"test_cleanup_reply_lost",True)
                    return reply
                w.router.request = lose_cleanup_reply
                runtime = Path(directory)
                workspace = runtime/"workspace-sentinel"
                workspace.write_text("preserve this file",encoding="utf-8")
                with tool_listener(runtime,loop.adapter,client,link=loop.native.link), \
                     self.start_native(directory,session,"cleanup-read-only",use_tls=use_tls) as native:
                    other.connect()
                    w.connect()
                    w.poll()
                    other.poll()
                    service.clock.advance(10)
                    w.poll()
                    w.poll()
                    self.assertTrue(self.read_native(native,"work-started")["running"])
                    before = loop.hermes.inspect(tid)["task"]
                    aid = before["assignment_id"]
                    self.assertEqual(w.hold()["held_assignment_id"],aid)
                    loop.hermes.update(guid(),tid,before["revision"],{"title":"Update queued on the old assignment"})
                    w.poll()
                    private_mid = guid()
                    self.assertIsNone(w.chat(private_mid,"Keep this unrelated queued input"))
                    with loop.adapter.db.transaction() as db:
                        pending_update = db.execute("""SELECT delivery_id,digest,receipt FROM intents
                            WHERE json_extract(payload,'$.assignment_id')=? AND json_extract(payload,'$.kind')='task_update'""",
                                                    (aid,)).fetchone()
                    self.assertIsNotNone(pending_update)
                    self.assertIsNone(pending_update["receipt"])
                    queued_native_id = loop.native.delivery_native_id(session,pending_update["delivery_id"],pending_update["digest"])
                    before = loop.hermes.inspect(tid)["task"]
                    fields = {"worker_id":"tama"} if action=="transfer" else {}
                    loop.hermes.act(guid(),tid,before["revision"],action,reason="End only the old assignment",**fields)
                    if action=="transfer":
                        other.poll()
                        new_holder = loop.hermes.inspect(tid)["task"]
                        self.assertNotEqual(new_holder["assignment_id"],aid)
                        self.assertEqual(other.hold()["held_assignment_id"],new_holder["assignment_id"])
                    w.poll()
                    native_end = self.read_native(native,"read")
                    self.assertEqual([turn["reason"]["kind"] for turn in native_end["turns"]],
                                     ["aborted","completed","completed"])
                    results = native_end["results"]
                    native_result, = results
                    with w.db.transaction() as db:
                        record = db.execute("SELECT proof,receipt FROM cleanup_reports WHERE assignment_id=?",(aid,)).fetchone()
                    self.assertIsNotNone(record,native_result)
                    proof = json.loads(record["proof"])
                    self.assertEqual((proof["assignment_id"],proof["scope"],proof["stopped"],proof["cleaned"]),
                                     (aid,"read_only",True,True))
                    self.assertEqual(proof["stopped_turns"][0]["reason"],"aborted")
                    withdrawal, = proof["withdrawn_inputs"]
                    self.assertEqual((withdrawal["delivery_id"],withdrawal["native_id"]),
                                     (pending_update["delivery_id"],queued_native_id))
                    self.assertNotIn(queued_native_id,native_end["inputs"])
                    private_receipt = w.chat(private_mid,"Keep this unrelated queued input")
                    self.assertEqual(native_end["inputs"].count(private_receipt["native_id"]),1)
                    with loop.adapter.db.transaction() as db:
                        discarded = db.execute("SELECT native_id,discard_seq FROM assignment_withdrawals WHERE delivery_id=?",
                                               (withdrawal["delivery_id"],)).fetchone()
                    self.assertEqual(tuple(discarded),(withdrawal["native_id"],withdrawal["discard_seq"]))
                    self.assertEqual(workspace.read_text(encoding="utf-8"),"preserve this file")
                    if action=="transfer":
                        self.assertFalse(native_result["isError"])
                        reply = json.loads(native_result["content"][0]["text"])
                        self.assertEqual(reply["release_assignment_id"],aid)
                        self.assertEqual(w.hold()["held_assignment_id"],None)
                        self.assertEqual(loop.hermes.inspect(tid)["task"],new_holder)
                        self.assertEqual(json.loads(record["receipt"]),reply)
                    else:
                        self.assertTrue(native_result["isError"])
                        self.assertIsNone(record["receipt"])
                        self.assertEqual(w.hold()["held_assignment_id"],aid)
                        self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"cancelled")
                if use_tls:
                    database = runtime/"dsh-admissions.sqlite3"
                    with closing(sqlite3.connect(database)) as db:
                        records = [json.loads(row[0]) for row in db.execute("SELECT envelope FROM admissions")]
                    self.assertCountEqual([row["kind"] for row in records],
                                          ["task","task_update","task.cancel" if action=="cancel" else "task.transfer_out"])
                    self.assertTrue(all((row["task_id"],row["session"],row["assignment_id"])==(tid,session,aid)
                                        for row in records))
                    with self.start_native(directory,session,"cleanup-read-only-reopen",use_tls=True) as native:
                        self.assertEqual(loop.adapter.cleanup_evidence(aid,proof["kind"]),proof)
                        cold = self.read_native(native,"read")
                        self.assertEqual(cold["inputs"],native_end["inputs"])
                        self.assertEqual(cold["modelRequests"],[])
                    self.restore_read_only_proof(runtime,loop.adapter,aid,proof)
                if action=="cancel":
                    reopened = Worker(w.db.path,"oguri",w.router,loop.adapter,clock=service.clock)
                    reopened.connect()
                    self.assertEqual(cleanup_receipts,[cleanup_receipts[0]]*2)
                    self.assertIsNone(reopened.hold()["held_assignment_id"])
                    with reopened.db.transaction() as db:
                        recovered = json.loads(db.execute("SELECT receipt FROM cleanup_reports WHERE assignment_id=?",
                                                          (aid,)).fetchone()[0])
                    self.assertEqual(recovered,cleanup_receipts[0])
                current = loop.hermes.inspect(tid)
                self.assertEqual(current["pending_cleanup"],[])
                self.assertIsNone(current["last_report"])
                self.assertEqual(agent_pending([loop.adapter.db.path])["adapter_deliveries"],0)
                cases.append({"action":action,"task_id":tid,"old_assignment_id":aid,
                    "native_inputs":native_end["inputs"],"proof":proof,"final_state":current["task"]["state"],
                    "retained_private_input":private_receipt["native_id"],
                    "new_assignment_id":current["task"]["assignment_id"] if action=="transfer" else None,
                    "dropped_router_w04_receipts":int(action=="cancel"),"recorded_w04_replays":len(cleanup_receipts),
                    **({"durable_admissions":len(records),"proof_recovered_after_cold_restart":True,
                        "new_model_requests_after_reopen":len(cold["modelRequests"]),
                        "copied_native_state_proof_matches":True} if use_tls else {})})
        output = os.environ.get("MOWULINK_DSH_CLEANUP_EVIDENCE")
        if output:
            Path(output).write_text(json.dumps({"cases":cases,
                "boundary":"Installed DSH SDK with opt-in read-only assignments, local model, real isolated Router."},
                ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def restore_read_only_proof(self,runtime,original,aid,proof):
        restored = self.local_tls_directory()
        self.assertFalse((runtime/"dsh-admissions.sqlite3-journal").exists())
        shutil.copy2(runtime/"dsh-admissions.sqlite3",restored/"dsh-admissions.sqlite3")
        shutil.copytree(runtime/"sessions",restored/"sessions")
        state = restored/"adapter.db"
        with closing(sqlite3.connect(original.db.path)) as source, closing(sqlite3.connect(state)) as target:
            source.backup(target)
        link = LocalLink(restored,use_tls=True)
        recovered = HostAdapter(state,DshHost(restored,link=link),original.session)
        with self.start_native(restored,original.session,"cleanup-read-only-reopen",use_tls=True) as native:
            self.assertEqual(recovered.cleanup_evidence(aid,proof["kind"]),proof)
            self.assertEqual(self.read_native(native,"read")["modelRequests"],[])

    def read_only_cleanup_refusals(self,*,use_tls=False):
        cases = []
        for mode in ("cleanup-unscoped","cleanup-forbidden-tool"):
            with self.subTest(mode=mode):
                sid = "dsh-"+mode
                directory,loop,service,_command = self.goal_worker(sid,use_tls=use_tls)
                token = guid()
                client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
                w = loop.worker
                runtime = Path(directory)
                with tool_listener(runtime,loop.adapter,client,link=loop.native.link), \
                     self.start_native(directory,sid,mode,use_tls=use_tls) as native:
                    tid = loop.hermes.create(guid(),"Unverified cleanup","Do not infer workspace cleanup","oguri")["task_id"]
                    w.connect()
                    w.poll()
                    service.clock.advance(10)
                    w.poll()
                    w.poll()
                    self.assertTrue(self.read_native(native,"work-started")["running"])
                    active = loop.hermes.inspect(tid)["task"]
                    aid = active["assignment_id"]
                    loop.hermes.act(guid(),tid,active["revision"],"cancel",reason="Native stop is only one witness")
                    w.poll()
                    observed = self.read_native(native,"read")
                    self.assertEqual(observed["turns"][0]["reason"]["kind"],"aborted")
                    report = next(row for row in observed["results"] if row["toolCallId"]=="cleanup-report-"+aid)
                    self.assertTrue(report["isError"])
                    self.assertIn("native_cleanup_unconfirmed",encode(report))
                    self.assertEqual(w.hold()["held_assignment_id"],aid)
                    self.assertEqual([item["assignment_id"] for item in loop.hermes.inspect(tid)["pending_cleanup"]],[aid])
                    self.assertFalse((runtime/"workspace-write.txt").exists())
                    if mode=="cleanup-forbidden-tool":
                        blocked = next(row for row in observed["results"] if row["toolCallId"]=="cleanup-forbidden-tool")
                        self.assertTrue(blocked["isError"])
                        self.assertIn("只读工位",encode(blocked))
                    cases.append({"mode":mode,"task_id":tid,"assignment_id":aid,"native_stop":"aborted",
                                  "native_report":"native_cleanup_unconfirmed",
                                  "workspace_write_observed":(runtime/"workspace-write.txt").exists()})
        output = os.environ.get("MOWULINK_DSH_CLEANUP_REFUSAL_EVIDENCE")
        if output:
            Path(output).write_text(json.dumps({"cases":cases},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


    def native_media(self,*,use_tls=False):
        session = "dsh-native-media"
        directory = (self.local_tls_directory() if use_tls else
                     Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-media-"))))
        link = LocalLink(directory,use_tls=use_tls)

        def host(_path,_name):
            port = DshHost(directory,link=link)
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        service = WorkerService(loop.worker,clock)
        platform_key,adapter_key = guid(),guid()
        url = self.open_worker(service,{platform_key:"platform/oguri",adapter_key:"adapter/oguri"})
        platform,adapter = Client(url,platform_key),Client(url,adapter_key)
        loop.adapter.media_store = NativeFiles(Path(directory)/"media",adapter)
        relay = DshDisplay(loop.adapter,adapter,"oguri")
        downloads = []
        sample_file = b"# DSH isolated handoff\n"*14000
        def fetch(mid,key,kind,limit):
            downloads.append((mid,key,kind))
            return sample_file if kind=="file" else PNG
        inbound = Inbound(loop.root/"feishu.db",platform,fetch,"app","trainer","test-chat")
        sink = PlatformFixture()
        views = Views(loop.root/"views.db",platform,sink,"test-chat","小栗帽",clock)
        caption = "  图上有哪些细节？\n同一条原文。 "
        post = {"zh_cn":{"content":[[{"tag":"text","text":caption},{"tag":"img","image_key":"img_fixture"}]]}}
        def record(mid,kind,content):
            return inbound.record("app","user","trainer",{"chat_id":"test-chat","chat_type":"p2p","message_type":kind,
                "message_id":mid,"content":json.dumps(content,ensure_ascii=False)})
        mode = "media-echo" if use_tls else "media"
        with self.start_native(directory,session,mode,use_tls=use_tls) as native:
            relay.collect()
            one = record("om_dsh_caption","post",post)
            proof = inbound.deliver(one)
            self.assertEqual(inbound.deliver(record(one,"post",post)),proof)
            self.read_native(native,"read")
            two = record("om_dsh_image","image",{"image_key":"img_fixture"})
            pure = inbound.deliver(two)
            self.assertEqual(inbound.deliver(two),pure)
            if use_tls:
                three = record("om_dsh_file","file",{"file_key":"file_fixture","file_name":"交接.md"})
                document = inbound.deliver(three)
                self.assertEqual(inbound.deliver(three),document)
                expected_file = hashlib.sha256(sample_file).hexdigest()
                self.assertEqual(document["media_sha256"],[expected_file])
            observed = self.read_native(native,"read")
            sha = hashlib.sha256(PNG).hexdigest()
            expected_requests = [
                {"caption":caption,"images":[{"sha256":sha,"size":len(PNG)}],"partTypes":["text","image"]},
                {"caption":"","images":[{"sha256":sha,"size":len(PNG)}],"partTypes":["image"]},
            ]
            inputs = [proof["native_id"],pure["native_id"]]
            if use_tls:
                inputs.append(document["native_id"])
                self.assertEqual(observed["nativeFiles"],[{"inputId":document["native_id"],"name":"交接.md",
                                                          "sha256":expected_file,"size":len(sample_file)}])
                materialized = observed["mediaRequests"][-1]
                self.assertIn(f'[File "交接.md" ({len(sample_file)} bytes, sha256:{expected_file[:8]})',materialized["caption"])
                self.assertEqual(materialized["partTypes"],["text"])
                self.assertEqual(materialized["images"],[])
                self.assertEqual(observed["mediaRequests"][:-1],expected_requests)
            else:
                self.assertEqual(observed["mediaRequests"],expected_requests)
            self.assertEqual(observed["inputs"],inputs)
            self.assertEqual(proof["media_sha256"],[sha])
            self.assertEqual(pure["media_sha256"],[sha])
            for _ in range(8):
                relay.collect()
                relay.flush()
            if use_tls:
                self.recover_media_receipt(directory,loop,proof,observed,native)
                send = sink.send
                sent_image = []
                def drop_image_ack(chat,kind,body,key):
                    mid = send(chat,kind,body,key)
                    if kind=="image":
                        sent_image.append(mid)
                        if len(sent_image)==1:
                            raise ConnectionError("isolated image send acknowledgement lost")
                    return mid
                sink.send = drop_image_ack
                with self.assertRaisesRegex(ConnectionError,"isolated image send acknowledgement lost"):
                    views.poll()
                clock.advance(5)
            views.poll()
            finals = [item[2] for item in sink.messages.values() if item[1]=="interactive"
                      and item[2]["header"]["title"]["content"]=="小栗帽-回复"]
            self.assertEqual(len(finals),3 if use_tls else 2)
            def body(card):
                return card["elements"][0]["text"]["content"]
            if use_tls:
                sent = list(sink.messages.values())
                self.assertEqual([r[1] for r in sent],["interactive","interactive","image",
                                                          "interactive","interactive","interactive"])
                self.assertEqual(sent_image,[sent[2][0],sent[2][0]])
                self.assertEqual([body(sent[i][2]) for i in (1,3)],["图前","图后"])
                self.assertEqual(body(finals[0]),"图后")
                self.assertEqual(body(finals[-1]),"Local fixture received 0 image(s).")
                self.assertEqual(sink.images,[PNG])
                self.assertEqual(views.poll(),0)
                self.assertTrue(len({r[0] for r in sent})==len(sent))
                self.reject_shared_media(directory,loop,proof,observed,native)
            else:
                self.assertEqual(sink.images,[])
            self.assertEqual(body(finals[1]),"Local fixture received 1 image(s).")
            platform_items = platform.request("GET","worker/display")["items"]
            self.assertEqual([item["event"]["text"] for item in platform_items if item["event"]["kind"]=="native_input"],[caption])
            self.assertEqual(len(downloads),3 if use_tls else 2)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.evidence = {"session":session,"captioned_receipt":proof,"image_only_receipt":pure,
                "provider_messages":expected_requests,"download_count":len(downloads),
                "original_input_count":len(observed["inputs"]),"reply_cards":len(finals),
                "boundary":"Actual DSH run loop and attachment storage; Feishu and model provider are fixtures",
                **({"ordered_feishu_fixture_messages":[{"message_id":r[0],"kind":r[1]} for r in sent],
                    "ordinary_file_receipt":document,"native_file_observations":observed["nativeFiles"],
                    "output_image_sha256":hashlib.sha256(sink.images[0]).hexdigest(),
                    "output_image_send_attempts":sent_image,"original_media_receipt_recovered_after_cache_loss":True,
                    "shared_media_rejected_by_both_sides":os.name=="nt",
                    "untrusted_cache_native_inputs_added":0} if use_tls else {})}
        output = os.environ.get("MOWULINK_TEST_DSH_MEDIA_EVIDENCE") if use_tls else None
        if output:
            Path(output).write_text(encode(self.evidence)+"\n",encoding="utf-8")

    def recover_media_receipt(self,directory,loop,proof,observed,native):
        did = proof["delivery_id"]
        with loop.adapter.db.transaction() as db:
            old = db.execute("SELECT payload FROM intents WHERE delivery_id=?",(did,)).fetchone()[0]
            db.execute("UPDATE intents SET receipt=NULL,native_id=NULL WHERE delivery_id=?",(did,))
        (directory/"media"/proof["media_sha256"][0]).unlink()
        self.assertEqual(loop.adapter.deliver(did,json.loads(old)),proof)
        self.assertEqual(self.read_native(native,"read"),observed)

    def reject_shared_media(self,directory,loop,proof,observed,native):
        with loop.adapter.db.transaction() as db:
            original = db.execute("SELECT payload FROM intents WHERE delivery_id=?",(proof["delivery_id"],)).fetchone()[0]
        payload = json.loads(original)
        cache = directory/"media"/proof["media_sha256"][0]
        if os.name=="nt":
            icacls = Path(os.environ["SystemRoot"])/"System32/icacls.exe"
            subprocess.run([icacls,cache,"/grant","*S-1-5-32-545:(R)"],check=True,capture_output=True)
            with self.assertRaisesRegex(Fault,"invalid_media_object"):
                loop.adapter.media_store.prepare(payload["attachments"])
        else:
            cache.chmod(0o644)
        payload["text"] = "隔离测试。共享缓存必须拒绝。"
        with self.assertRaisesRegex(Fault,"invalid_media_object"):
            loop.native.deliver(proof["session"],"disallowed-cache",payload,hashlib.sha256(encode(payload).encode()).hexdigest())
        self.assertEqual(self.read_native(native,"read")["inputs"],observed["inputs"])

    def managed_session_switches(self,*,use_tls=False):
        sid = "isolated-dsh-managed-root"
        root = (self.local_tls_directory(dynamic_callback=True) if use_tls else
                Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="ml-dsh-managed-"))).resolve())
        link = LocalLink(root,use_tls=True) if use_tls else None
        registry = root/"enrolled"
        registry.mkdir(mode=0o700)
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock))
        incoming_token,callback_token,tls_token,foreign = (guid() for _ in range(4))
        if use_tls:
            cert,key = root/"dsh-link.crt",root/"dsh-link.key"
        else:
            cert,key = root/"tls.crt",root/"tls.key"
            subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                            "-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",key],
                           capture_output=True,timeout=10,check=True)
        roles = config(root,"adapter-roles.json",{tls_token:"worker/oguri",foreign:"worker/other"})
        callback_key = os.urandom(32)
        package = Path(__file__).resolve().parents[1]
        enrollment = []
        callback_ports = []

        @contextmanager
        def station(mode,stage,*,lose_first=False):
            port = RemoteAdapter(None,sid)
            worker = Worker(root/"worker.db","oguri",loop.wire,port,clock=clock,session_callback_key=callback_key)
            gateway = WorkerService(worker,clock)
            worker_roles = {incoming_token:"platform/oguri", callback_token:"adapter/oguri/"+sid}
            with codex_driver.running(gateway.listen(worker_roles,("127.0.0.1",0),None)) as url:
                to_worker = config(root,"adapter-to-worker-"+stage+".json",{"url":url,"token":callback_token})
                extra = ("--worker-id","oguri","--session",sid,"--runtime-dir",root,"--worker-connection",to_worker,
                         "--managed-sessions-dir",registry)
                if use_tls:
                    extra += ("--local-tls",)
                with self.start_native(root,sid,mode,use_tls=use_tls) as native, \
                     serve(package,"dsh-adapter",root/"adapter.db",roles,cert,key,dict(os.environ),extra) as host_url:
                    actual = Client(host_url,tls_token,cert)
                    if use_tls:
                        callback_ports.append(json.loads((root/"dsh-worker-port.json").read_text())["port"])
                        if stage=="initial":
                            with self.assertRaisesRegex(RuntimeError,"state_writer_already_running"), \
                                 serve(package,"dsh-adapter",root/"duplicate.db",roles,cert,key,dict(os.environ),
                                       ("--worker-id","oguri","--session",sid,"--runtime-dir",root,
                                        "--worker-connection",to_worker,"--local-tls")):
                                self.fail("another adapter acquired the DSH callback runtime")
                    for target in worker.resume_targets.values():
                        target.client = actual
                    original = port.enroll_thread
                    if stage in ("initial","retry"):
                        def watch(source,operation,target,token):
                            if source==sid:
                                self.assertTrue(loop.hermes.workers()["items"][0]["admission_paused"])
                            reply = original(source,operation,target,token)
                            enrollment.append(reply)
                            if lose_first:
                                raise Fault(503,"adapter_connection_unconfirmed",True)
                            return reply
                        port.enroll_thread = watch
                    gateway.step()
                    yield gateway,native,Client(url,incoming_token),Client(url,callback_token),host_url

        def platform(incoming,mid,command):
            inbox = Inbound(root/"feishu.db",incoming,None,"app","trainer","chat",clock=clock)
            inbox.record("app","user","trainer",{"chat_id":"chat","chat_type":"p2p","message_id":mid,
                "message_type":"text","content":encode({"text":command})})
            deadline = time.monotonic()+8
            while True:
                try:
                    return inbox.deliver(mid)
                except Fault as error:
                    if error.code not in ("native_switch_callbacks_pending","host_receipt_unconfirmed") or time.monotonic()>=deadline:
                        raise
                    time.sleep(.04)

        def dispatch(gateway,title):
            tid = loop.hermes.create(guid(),title,"Report one DSH fixture result","oguri")["task_id"]
            gateway.step()
            clock.advance(10)
            started = time.monotonic()
            w = gateway.worker
            while True:
                detail = loop.hermes.inspect(tid)
                if detail["task"]["state"]=="completed_reported" and not w.adapter.busy():
                    break
                self.assertLess(time.monotonic()-started,15,gateway.status())
                gateway.step()
                time.sleep(.04)
            aid = detail["last_report"]["assignment_id"]
            with w.db.transaction() as db:
                raw = db.execute("SELECT frame FROM inbox JOIN jobs USING(delivery_id) WHERE assignment_id=?",(aid,)).fetchone()[0]
            frame = json.loads(raw)
            receipt = w.adapter.lookup_delivery(frame["delivery_id"],{"kind":"task",**frame["payload"]},session=w.native_session_pin)
            self.assertEqual(w.assignment_session(aid),w.native_session_pin)
            self.assertIsNone(w.hold()["held_assignment_id"])
            self.assertTrue(w.process(frame))
            return {"task_id":tid,"assignment_id":aid,"receipt":receipt}

        with station("managed-sessions","initial",lose_first=True) as (gateway,native,incoming,_callback,host_url):
            remote = gateway.worker.adapter
            self.assertTrue(remote.session_info()["managed_session_enrollment"])
            before = self.read_native(native,"status")
            with self.assertRaisesRegex(Fault,"adapter_identity_unconfirmed"):
                RemoteAdapter(Client(host_url,foreign,cert),sid).prepare_thread(sid,guid(),"new")
            with self.assertRaisesRegex(Fault,"adapter_connection_unconfirmed"):
                platform(incoming,"om_dsh_managed_new","/new")
            target = enrollment[0]["session"]
            if use_tls:
                self.check_tls_enrollment(registry,link,callback_ports[-1],target,remote)
            cold = remote.for_session(target)
            self.assertEqual(cold.session_info()["native_load_state"],"cold")
            self.assertEqual(self.read_native(native,"status"),before)
            self.assertEqual(gateway.worker.native_session_pin,sid)
            self.assertTrue(loop.hermes.workers()["items"][0]["admission_paused"])
        with station("managed-sessions-reopen","retry") as (gateway,native,incoming,old_callback,_host_url):
            created = platform(incoming,"om_dsh_managed_new","/new")["binding_receipt"]
            self.assertEqual((created["session"],created["previous_session"]),(target,sid))
            self.assertEqual(len(enrollment),2)
            self.assertEqual(enrollment[0],enrollment[1])
            self.assertEqual(self.read_native(native,"status")["liveSessions"],[sid,target])
            with self.assertRaisesRegex(Fault,"native_callback_inactive"):
                old_callback.request("POST","worker/native-tool",{"session":target})
            first = dispatch(gateway,"First managed DSH order")
            w = gateway.worker
            self.assertEqual(w.adapter.model_view("efforts")["current"]["effort"],"low")
            selected = w.adapter.model_change(target,"efforts","high")
            self.assertEqual(selected["selected"]["effort"],"high")
            forked = platform(incoming,"om_dsh_managed_fork","/fork")["binding_receipt"]
            branch = forked["session"]
            self.assertEqual(forked["previous_session"],target)
            self.assertEqual(w.adapter.model_view("efforts")["current"]["effort"],"high")
            second = dispatch(gateway,"Forked managed DSH order")
            history = self.read_native(native,"read "+branch)
            self.assertEqual(history["parent"],target)
            self.assertEqual(history["allInputs"],[first["receipt"]["native_id"],second["receipt"]["native_id"]])
            self.assertEqual(history["ownInputs"],[second["receipt"]["native_id"]])
            self.assertEqual(len(history["results"]),2)
            expected = {"recorded":True,"task_state":"completed_reported","release_assignment_id":second["assignment_id"]}
            self.assertTrue(all(not row["isError"] and json.loads(row["content"][0]["text"])==expected
                                for row in history["results"]))
            notes = [frame for frame in loop.manager_wire.frames(0) if frame["payload"].get("task_id")]
            self.assertEqual([n["payload"]["task_id"] for n in notes],[first["task_id"],second["task_id"]])
        with station("managed-sessions-reopen","ready") as (gateway,native,incoming,_callback,_host_url):
            restored = self.read_native(native,"status")
            self.assertEqual(restored["liveSessions"],[branch])
            self.assertEqual(restored["modelRequests"],[])
            self.assertEqual(gateway.worker.native_session_pin,branch)
            self.assertEqual(platform(incoming,"om_dsh_managed_fork","/fork")["binding_receipt"],forked)
            cleared = platform(incoming,"om_dsh_managed_clear","/clear")["binding_receipt"]
            self.assertNotIn(cleared["session"],(branch,target,sid))
            new = self.read_native(native,"read "+cleared["session"])
            self.assertEqual((new["allInputs"],new["parent"],new["selection"]["reasoningEffort"]),([],None,"low"))
            self.assertEqual(self.read_native(native,"status")["modelRequests"],[])
            if use_tls:
                resumed = platform(incoming,"om_dsh_managed_resume","/resume "+target)["binding_receipt"]
                self.assertEqual((resumed["session"],resumed["previous_session"],gateway.worker.native_session_pin),
                                 (target,cleared["session"],target))
                self.assertEqual(self.read_native(native,"read "+target)["ownInputs"],[first["receipt"]["native_id"]])
                self.assertEqual(platform(incoming,"om_dsh_managed_resume","/resume "+target)["binding_receipt"],resumed)
                self.assertEqual(self.read_native(native,"status")["modelRequests"],[])
                back = platform(incoming,"om_dsh_managed_resume_back","/resume "+cleared["session"])["binding_receipt"]
                self.assertEqual((back["session"],back["previous_session"]),(cleared["session"],target))

            def source_changed(*args):
                self.assertTrue(loop.hermes.workers()["items"][0]["admission_paused"])
                raise Fault(409,"native_preparation_source_changed")
            gateway.worker.adapter.prepare_thread = source_changed
            refusal = platform(incoming,"om_dsh_changed_source","/fork")
            seq = refusal["output"]["seq"]
            render, = [item["event"] for item in incoming.request("GET",f"worker/display?after={seq-1}")["items"]
                       if item["seq"]==seq]
            self.assertIn("原会话已变化",render["text"])
            self.assertEqual(gateway.worker.native_session_pin,cleared["session"])
            self.assertFalse(loop.hermes.workers()["items"][0].get("admission_paused",False))
        loop.server.shutdown()
        loop.thread.join()
        trigger_db = root/"trigger.db"
        Trigger(trigger_db,"isolated-managed-source")
        sources = {"router:main":loop.root/"router.db","worker:oguri":root/"worker.db","agent:oguri":root/"adapter.db",
                   "hermes:main":loop.root/"hermes-adapter.db","trigger:source":trigger_db}
        snapshot = root/"snapshot"
        recorded = capture(sources,"a"*40,snapshot,registries={"oguri":registry})
        checksum = recorded["manifest_sha256"]
        members = verify(snapshot,checksum)["managed_native"]["oguri"]["children"]
        self.assertEqual(set(members),{target,branch,cleared["session"]})
        keys = {"oguri":config(root,"managed-key.json",{"key":callback_key.hex()})}
        trial = root/"trial"
        restored_paths = restore(snapshot,checksum,trial,managed_keys=keys)
        rolled = root/"rollback"
        rollback(snapshot,checksum,restored_paths,rolled,registries={"oguri":trial/"native--oguri"},managed_keys=keys)
        for child in members:
            name = child+".sqlite3"
            self.assertEqual((rolled/"native--oguri"/name).read_bytes(),(registry/name).read_bytes())
        self.evidence = {"new_binding":created,"fork_binding":forked,"clear_binding":cleared,"formal":[first,second],
            "branch_parent":history["parent"],"native_inherited_count":history["inherited"],
            "native_own_inputs":history["ownInputs"],"branch_native_tool_results":len(history["results"]),
            "notice_ids":[note["delivery_id"] for note in notes],"recovered_enrollment_count":len(enrollment)-1,
            "managed_snapshot_sha256":checksum,"restored_native_ledgers":len(members),
            "scope":"Isolated installed DSH SDK, real TLS adapter and rewrite Router/Worker, fixture model and platform.",
            **({"resume_binding":resumed,"resume_back_binding":back,"registry_secret_excluded_from_host_reply":True,
                "shared_registry_refused":True,"foreign_session_refused":True,"resume_added_model_requests":0,
                "adapter_callback_ports":callback_ports,"second_callback_writer_refused":True}
               if use_tls else {})}
        output = os.environ.get("MOWULINK_DSH_MANAGED_EVIDENCE")
        if output:
            Path(output).write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def check_tls_enrollment(self,registry,link,worker_port,target,remote):
        from http.client import HTTPSConnection

        foreign = guid()
        for sid,status in ((foreign,409),(target,200)):
            with closing(HTTPSConnection("127.0.0.1",worker_port,timeout=4,context=link.client)) as local:
                local.request("POST","/managed-session",body=encode({"session":sid}).encode(),
                              headers={"Content-Type":"application/json"})
                response = local.getresponse()
                raw = json.load(response)
                self.assertEqual(response.status,status)
                if sid==target:
                    self.assertEqual(set(raw),{"session","source","operation_id"})
                    self.assertEqual(raw["session"],target)
                else:
                    self.assertEqual(raw,{"error":"wrong_native_session"})
            if sid==foreign:
                with self.assertRaisesRegex(Fault,"wrong_native_session"):
                    remote.for_session(sid).session_info()
        record = registry/(target+".json")
        if os.name=="nt":
            icacls = Path(os.environ["SystemRoot"])/"System32/icacls.exe"
            subprocess.run([icacls,record,"/grant","*S-1-5-32-545:(R)"],check=True,capture_output=True)
        else:
            record.chmod(0o644)
        try:
            with self.assertRaisesRegex(Fault,"private_session_registry_required"):
                remote.for_session(target).session_info()
        finally:
            if os.name=="nt":
                subprocess.run([icacls,record,"/remove:g","*S-1-5-32-545"],check=True,capture_output=True)
            else:
                record.chmod(0o600)

    def foreground_questions(self,*,use_tls):
        evidence = []
        for outcome in ("feishu-first","native-first","canceled","timed-out"):
            with self.subTest(outcome=outcome):
                evidence.append(self.foreground_relay(outcome,use_tls=use_tls))
        self.evidence = {"cases":evidence,
            "link":"IPv4 loopback with mutual TLS" if use_tls else "Unix socket",
            "boundary":"Packaged DSH AgentLoop, question SDK and persistence. Local model, native answer provider and Feishu test platform."}
        if use_tls and (output := os.environ.get("MOWULINK_DSH_FOREGROUND_TLS_EVIDENCE")):
            Path(output).write_text(encode(self.evidence)+"\n",encoding="utf-8")

    def foreground_relay(self,outcome,*,use_tls):
        session = "dsh-real-question-"+outcome
        directory = (self.local_tls_directory() if use_tls else
                     Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-live-q-"))))
        native = DshHost(directory,link=LocalLink(directory,use_tls=use_tls))
        native.session = session
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:native))
        adapter_key,platform_key = guid(),guid()
        url = self.open_worker(WorkerService(loop.worker,clock),
                               {adapter_key:"adapter/oguri",platform_key:"platform/oguri"})
        platform = Client(url,platform_key)
        relay = DshDisplay(loop.adapter,Client(url,adapter_key),"oguri")
        sink = PlatformFixture()
        views = Views(loop.root/"foreground-views.db",platform,sink,"test-chat","小栗帽",clock)
        actions = Actions(views,Inbound(loop.root/"feishu.db",platform,None,"app","trainer","test-chat"))

        def project():
            for _ in range(4):
                relay.collect()
                relay.flush()
            views.poll()

        fixture = "foreground-relay-timed" if outcome=="timed-out" else "foreground-relay"
        with self.start_native(directory,session,fixture,use_tls=use_tls) as process:
            project()
            loop.worker.chat(guid(),"Question fixture foreground")
            waiting = self.read_native(process,"foreground-question")
            self.assertEqual((waiting["status"],waiting["continued"]),("running",0))
            self.assertEqual(waiting["timed"],outcome=="timed-out")
            project()
            with views.db.transaction() as db:
                qid,mid = tuple(db.execute("SELECT qid,mid FROM question_cards").fetchone())
            self.assertIsNone(native.lookup_answer(session,"0/0",waiting["call_id"]))
            self.assertIsNone(native.lookup_answer(session,waiting["turn_id"],waiting["call_id"]))
            value = {"question_id":qid,"mode":"choice","option_id":"1"}
            keys = ("app","trainer","test-chat",mid,value,None)
            self.assertEqual(actions.record(*keys),"pending")
            if outcome=="feishu-first":
                accept = native.resolve_question
                attempts = []
                def lost_reply(original,answer):
                    proof = accept(original,answer)
                    attempts.append(proof["receipt_id"])
                    raise Fault(503,"adapter_connection_unconfirmed",True)
                native.resolve_question = lost_reply
                actions.flush()
                observed = self.read_native(process,"answer-foreground")
                self.assertEqual(actions.record(*keys),"pending")
                native.resolve_question = accept
                clock.advance(16)
                actions.flush()
                self.assertEqual(actions.record(*keys),"confirmed")
                self.assertEqual(attempts,[observed["results"][0]["id"]])
                self.assertGreaterEqual(observed["foregroundProviderCloses"],1)
            elif outcome=="native-first":
                observed = self.read_native(process,"answer-foreground")
                actions.flush()
                self.assertEqual(actions.record(*keys),"rejected")
            elif outcome=="canceled":
                observed = self.read_native(process,"cancel-foreground")
                actions.flush()
            else:
                timed = self.read_native(process,"read")
                self.assertIn(waiting["call_id"],timed["continued"])
                project()
                self.assertEqual(sink.calls.count(("send",mid)),1)
                actions.flush()
                observed = self.read_native(process,"read")
            project()
            project()
            with loop.worker.db.transaction() as db:
                proof = json.loads(db.execute("SELECT proof FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()[0])
                self.assertEqual(db.execute("SELECT count(*) FROM trainer_questions").fetchone()[0],1)
            self.assertEqual(native.lookup_answer(session,waiting["turn_id"],waiting["call_id"]),proof)
            self.assertIsNone(native.lookup_answer(session,"0/0",waiting["call_id"]))
            self.assertEqual([call["callId"] for call in observed["calls"]],[waiting["call_id"]])
            self.assertEqual((sink.calls.count(("send",mid)),sink.calls.count(("patch",mid))),(1,1))
            card = next(row[2] for row in sink.messages.values() if row[0]==mid)
            self.assertFalse(any(row["tag"]=="form" for row in card["body"]["elements"]))
            self.assertEqual(loop.hermes.tasks()["items"],[])
            if outcome=="canceled":
                self.assertEqual(proof["state"],"ended_unanswered")
                self.assertIsNone(proof["answer"])
                self.assertEqual(observed["turns"][-1]["reason"]["kind"],"aborted")
                with self.assertRaisesRegex(Fault,"native_question_ended"):
                    actions.record(*keys)
            else:
                selected,expected = ("B","2") if outcome=="native-first" else ("A","1")
                self.assertEqual(proof["answer"],{"mode":"choice","option_id":expected})
                if outcome=="timed-out":
                    reply, = observed["replies"]
                    self.assertEqual((proof["receipt_id"],reply["body"]["answers"][0]["selected"]),(reply["id"],[selected]))
                else:
                    result, = observed["results"]
                    self.assertEqual(proof["receipt_id"],result["id"])
                    self.assertEqual(json.loads(result["content"][0]["text"]),
                                     {"answers":[{"id":"primary","selected":[selected]}]})
                    self.assertEqual(observed["replies"],[])
            result = {"case":outcome,"opening":waiting,"question_id":qid,"fixture_message_id":mid,
                      "proof":proof,"native_calls":len(observed["calls"]),
                      "native_continued_replies":len(observed["replies"]),"original_card_updates":1}
        if use_tls:
            ledger = directory/"dsh-questions.sqlite3"
            with closing(sqlite3.connect(ledger)) as db:
                rows = db.execute("SELECT digest FROM foreground_questions").fetchall()
            self.assertRegex(rows[0][0] if len(rows)==1 else "",r"^[0-9a-f]{64}$")
            result["question_mark_count"] = len(rows)
        if outcome=="feishu-first":
            def check_reopened(runtime,host):
                with self.start_native(runtime,session,"foreground-relay-reopen",use_tls=use_tls) as process:
                    self.assertEqual(host.lookup_answer(session,waiting["turn_id"],waiting["call_id"]),proof)
                    readback = self.read_native(process,"read")
                    self.assertEqual(readback["results"],observed["results"])
                    self.assertEqual(readback["modelRequests"],[])
                    self.assertEqual(sink.calls.count(("patch",mid)),1)
                    return proof["receipt_id"]
            result["reopened_receipt_id"] = check_reopened(directory,native)
            if use_tls:
                restored = self.local_tls_directory()
                shutil.copytree(directory/"sessions",restored/"sessions")
                shutil.copyfile(ledger,restored/ledger.name)
                if os.name=="posix":
                    (restored/ledger.name).chmod(0o600)
                restored_host = DshHost(restored,link=LocalLink(restored,use_tls=True))
                result["copied_session_receipt_id"] = check_reopened(restored,restored_host)
                result["model_requests_after_restore"] = 0
        return result

    def native_command_decisions(self,*,use_tls):
        session = "isolated-dsh-bash-approvals"
        directory = (self.local_tls_directory() if use_tls else
                     Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-permission-"))))
        link = LocalLink(directory,use_tls=use_tls)
        command = "pwd -W" if os.name=="nt" else "pwd"
        worker_token,operator,manager,callback,platform = (guid() for _ in range(5))
        advance = 0
        orders = Orders(directory/"router.db",clock=lambda:time.time()+advance)
        router = self.enterContext(codex_driver.running(serve_loopback(orders,{
            worker_token:"worker/dev",operator:"approver/dev",manager:"hermes"})))
        host = DshHost(directory,link=link)
        adapter = HostAdapter(directory/"adapter.db",host,session)
        worker = Worker(directory/"worker.db","dev",Client(router,worker_token),adapter)
        service = WorkerService(worker)
        callback_url = self.open_worker(service,{callback:"adapter/dev",platform:"platform/dev"})
        callback_wire = Client(callback_url,callback)
        platform_wire = Client(callback_url,platform)
        observer = Client(router,operator)
        worker.connect()
        confirmations = []
        block_closure = Event()
        request = callback_wire.request
        def drop_once(method,path,*args,**kwargs):
            if method=="PUT" and path.endswith("/closed") and block_closure.is_set():
                raise ConnectionError("cannot forward closure during the isolated restart")
            receipt = request(method,path,*args,**kwargs)
            if method=="PUT" and path.endswith("/closed"):
                confirmations.append(receipt)
                if len(confirmations)==1:
                    raise ConnectionError("lost the isolated closure response")
            return receipt
        callback_wire.request = drop_once

        def wait_for(check):
            until = time.monotonic()+8
            while time.monotonic()<until:
                value = check()
                if value:
                    return value
                time.sleep(.05)
            self.fail("the isolated DSH approval did not settle: "+encode(self.read_native(native,"approval-state")))

        def open_offer(variant):
            received = worker.chat(guid(),"DSH approval fixture "+variant)
            seen = wait_for(lambda:next((row for row in observer.request("GET","approvals/dev")["items"]
                                         if row["state"]=="pending"),None))
            self.assertEqual((seen["request"]["session"],seen["request"]["command"]),(session,command))
            if received:
                self.assertEqual(received["turn_id"],seen["request"]["turn_id"])
            return seen,"approvals/dev/"+seen["approval_id"]

        def wait_closed(route):
            def closed():
                seen = observer.request("GET",route)
                return seen if seen["state"]=="native_closed" else None
            return wait_for(closed)

        def verify_native(events,offered,variant,outcome):
            aid = offered["approval_id"]
            asked,decided = [event for event in events if event["data"]["id"]==aid]
            self.assertEqual((asked["type"],asked["data"]["callId"]),("approval/asked","fixture-bash-"+variant))
            self.assertEqual((decided["type"],decided["data"]["outcome"]),("approval/decided",outcome))
            self.assertLess(asked["seq"],decided["seq"])
            return {"native_id":aid,"turn":offered["request"]["turn_id"],"asked_seq":asked["seq"],
                    "decided_seq":decided["seq"],"outcome":outcome}

        with tool_listener(directory,adapter,callback_wire,link=link), \
             self.start_native(directory,session,"command-approvals",use_tls=use_tls) as native:
            worker.chat(guid(),"DSH approval fixture unverifiable")
            local = self.read_native(native,"read")
            first_local,last_local = local["approvals"]
            self.assertEqual(first_local["data"]["callId"],"fixture-bash-unverifiable")
            self.assertEqual(last_local["data"],{"id":first_local["data"]["id"],"outcome":"rejected"})
            self.assertEqual(observer.request("GET","approvals/dev")["items"],[])
            denied,denied_route = open_offer("reject")
            self.assertEqual(denied["request"]["decisions"],["accept","cancel"])
            self.assertEqual(denied["request"]["cwd"],str(directory))
            decision = {"request_digest":denied["request_digest"],"decision":"cancel"}
            with self.assertRaisesRegex(Fault,"unsupported_approval_decision"):
                observer.request("PUT",denied_route+"/decision",dict(decision,decision="acceptForSession"))
            with self.assertRaisesRegex(Fault,"forbidden"):
                platform_wire.request("POST","worker/approvals/"+denied["approval_id"],denied["request"])
            observer.request("PUT",denied_route+"/decision",decision)
            actual = self.read_native(native,"read")
            rejected_native = verify_native(actual["approvals"],denied,"reject","rejected")
            rejected = wait_closed(denied_route)
            wait_for(lambda:len(confirmations)>=2)
            self.assertEqual(confirmations[:2],[rejected,rejected])
            self.assertEqual(rejected["attempted_response"],{"source":"operator","decision":"cancel"})
            self.assertEqual(sum(item.get("isError") is False for item in actual["results"]),0)

            granted,granted_route = open_offer("allow")
            self.assertEqual(granted["request"]["decisions"],["accept","cancel"])
            self.assertEqual(granted["request"]["arguments"]["workdir"],str(directory))
            decision = {"request_digest":granted["request_digest"],"decision":"accept"}
            observer.request("PUT",granted_route+"/decision",decision)
            actual = self.read_native(native,"read")
            allowed_native = verify_native(actual["approvals"],granted,"allow","allowed-once")
            allowed = wait_closed(granted_route)
            self.assertEqual(observer.request("PUT",granted_route+"/decision",decision),allowed)
            self.assertEqual(allowed["attempted_response"],{"source":"operator","decision":"accept"})
            result, = [item for item in actual["results"] if item.get("isError") is False]
            text = "\n".join(part["text"] for part in result["content"] if part["type"]=="text")
            expected = str(directory.resolve())
            if os.name=="nt":
                text,expected = text.replace("\\","/").casefold(),expected.replace("\\","/").casefold()
            self.assertIn(expected,text)

            expired,expired_route = open_offer("expire")
            advance = TTL_SECONDS+1
            actual = self.read_native(native,"read")
            expired_native = verify_native(actual["approvals"],expired,"expire","rejected")
            timed_out = wait_closed(expired_route)
            self.assertEqual((timed_out["decision"],timed_out["attempted_response"]),
                             (None,{"source":"expiry","decision":"cancel"}))
            self.assertEqual(sum(item.get("isError") is False for item in actual["results"]),1)
            cold,cold_route = open_offer("cold")
            block_closure.set()
            decision = {"request_digest":cold["request_digest"],"decision":"cancel"}
            observer.request("PUT",cold_route+"/decision",decision)
            before = self.read_native(native,"read")
            cold_native = verify_native(before["approvals"],cold,"cold","rejected")
            self.assertEqual(observer.request("GET",cold_route)["state"],"decided")
        reopened = HostAdapter(directory/"adapter.db",DshHost(directory,link=link),session)
        block_closure.clear()
        with tool_listener(directory,reopened,callback_wire,link=link), \
             self.start_native(directory,session,"command-approvals-reopen",use_tls=use_tls) as native:
            after = self.read_native(native,"read")
            recovered = wait_closed(cold_route)
            self.assertEqual(recovered["attempted_response"],{"source":"operator","decision":"cancel"})
            self.assertEqual((after["inputs"],after["approvals"],after["results"]),
                             (before["inputs"],before["approvals"],before["results"]))
        self.assertEqual(Client(router,manager).request("GET","tasks")["items"],[])
        notices = observer.frames(0)
        self.assertEqual([notice["payload"]["approval_id"] for notice in notices],
                         [denied["approval_id"],granted["approval_id"],expired["approval_id"],cold["approval_id"]])
        self.evidence = {"session":session,"native_records":[rejected_native,allowed_native,expired_native,cold_native],
                         "router_notifications":[notice["delivery_id"] for notice in notices],
                         "closure_retry_same_receipt":confirmations[0]==confirmations[1],
                         "cold_reopen_recovered_original_closure":True,
                         "unverifiable_directory_used_native_answerer":True,
                         "successful_bash_calls":1,
                         "boundary":"Installed DSH AgentLoop, ApprovalService and Bash provider; test model and operator identity"}
        if use_tls and (output := os.environ.get("MOWULINK_DSH_APPROVAL_TLS_EVIDENCE")):
            self.evidence["link"] = "IPv4 loopback with mutual TLS"
            Path(output).write_text(encode(self.evidence)+"\n",encoding="utf-8")

@unittest.skipUnless(EXE.is_file() or os.environ.get("MOWULINK_TEST_DSH_BINARY"),
                     "installed DSH SDK or verified packaged Windows executable required")
@unittest.skipUnless(shutil.which("openssl"),"test TLS certificate generator required")
class LocalTlsLoop(NativeProcess):
    def local_tls_directory(self,*,dynamic_callback=False):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="mldsh-tls-"))).resolve()
        if os.name=="nt":
            system32 = Path(os.environ["SystemRoot"])/"System32"
            current = subprocess.check_output([system32/"whoami.exe","/user","/fo","csv","/nh"],text=True)
            _,owner = next(csv.reader(current.splitlines()))
            # Python's private temporary directory need not give new files inheritable rights.
            subprocess.run([system32/"icacls.exe",directory,"/grant:r","*"+owner+":(OI)(CI)F"],
                           check=True,capture_output=True)
        with closing(socket.socket()) as host, closing(socket.socket()) as tools:
            host.bind(("127.0.0.1",0))
            tools.bind(("127.0.0.1",0))
            config(directory,"dsh-link.json",{"host_port":host.getsockname()[1],
                                              "worker_port":0 if dynamic_callback else tools.getsockname()[1]})
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                        "-subj","/CN=mowulink isolated DSH link","-addext","subjectAltName=IP:127.0.0.1",
                        "-addext","basicConstraints=critical,CA:FALSE","-addext","extendedKeyUsage=serverAuth,clientAuth",
                        "-out","dsh-link.crt","-keyout","dsh-link.key"],
                       cwd=directory,check=True,capture_output=True)
        if os.name=="posix":
            for name in ("dsh-link.crt","dsh-link.key"):
                (directory/name).chmod(0o600)
        return directory

    @unittest.skipUnless(os.name=="nt","Windows file share flags required")
    def test_dynamic_callback_port_survives_a_temporary_reader(self):
        runtime = self.local_tls_directory(dynamic_callback=True)
        link = LocalLink(runtime,use_tls=True)
        locator = runtime/"dsh-worker-port.json"
        first,second = [p for p in (32000,32001,32002) if p!=link.ports["host_port"]][:2]
        link.publish_callback_port(first)
        replace = os.replace
        conflicts = []
        with locator.open("rb") as reader:
            def release_reader(pending,target):
                try:
                    replace(pending,target)
                except PermissionError as error:
                    self.assertEqual(json.load(reader),{"port":first})
                    conflicts.append(error.winerror)
                    reader.close()
                    raise
            with patch("rewrite.dsh_local_link.os.replace",side_effect=release_reader):
                link.publish_callback_port(second)
        self.assertEqual(len(conflicts),1)
        self.assertIn(conflicts[0],(5,32,33))
        self.assertEqual(json.loads(locator.read_text()),{"port":second})
        self.assertEqual(list(runtime.glob(".dsh-port-*")),[])

    def test_fixed_session_w04_over_mutual_tls(self):
        directory = self.local_tls_directory()
        if os.name=="posix":
            (directory/"dsh-link.key").chmod(0o644)
            with self.assertRaisesRegex(Fault,"private_dsh_link_required"):
                LocalLink(directory,use_tls=True)
            (directory/"dsh-link.key").chmod(0o600)
        if os.name=="nt":
            powershell = Path(os.environ["SystemRoot"])/"System32/WindowsPowerShell/v1.0/powershell.exe"
            check = FIXTURE.with_name("check-local-link.ps1")
            observed = subprocess.run([powershell,"-NoProfile","-NonInteractive","-File",check,directory],
                                      env=environment(str(directory)),capture_output=True,timeout=20)
            self.assertEqual(observed.returncode,0,observed.stderr.decode("utf-8",errors="replace"))
        self.formal_report(directory,use_tls=True)
        self.evidence.update({"link":"IPv4 loopback with mutual TLS","missing_client_certificate_rejected_in_both_directions":True,
                              "foreign_session_rejected":True,"input_count_after_replay":1})
        output = os.environ.get("MOWULINK_TEST_DSH_EVIDENCE")
        if output:
            Path(output).write_text(encode(self.evidence)+"\n",encoding="utf-8")

    def test_captioned_image_and_reply_order_over_mutual_tls(self):
        self.native_media(use_tls=True)

    def test_dsh_adapter_process_delivers_media(self):
        session = "dsh-native-process-media"
        runtime = self.local_tls_directory()
        cert,key = runtime/"dsh-link.crt",runtime/"dsh-link.key"
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock))
        port = RemoteAdapter(None,session)
        worker = Worker(runtime/"worker.db","oguri",loop.wire,port,clock=clock)
        worker_service = WorkerService(worker,clock)
        platform_token,callback_token,adapter_token = (guid() for _ in range(3))
        worker_url = self.open_worker(worker_service,{platform_token:"platform/oguri",callback_token:"adapter/oguri"})
        platform = Client(worker_url,platform_token)
        roles = config(runtime,"adapter-roles.json",{adapter_token:"worker/oguri"})
        connection = config(runtime,"worker-connection.json",{"url":worker_url,"token":callback_token})
        flags = ("--worker-id","oguri","--session",session,"--runtime-dir",runtime,"--worker-connection",connection,
                 "--local-tls")
        package = Path(__file__).resolve().parents[1]
        with self.start_native(runtime,session,"media-echo",use_tls=True) as native, \
             serve(package,"dsh-adapter",runtime/"adapter.db",roles,cert,key,dict(os.environ),flags) as adapter_url:
            port.client = Client(adapter_url,adapter_token,cert)
            worker.connect()
            inbox = Inbound(runtime/"inbox.db",platform,lambda *_:PNG,"app","trainer","private-chat")
            text = "实际适配器进程与固定会话"
            mid = inbox.record("app","user","trainer",{"message_id":"om_dsh_process","chat_id":"private-chat",
                "chat_type":"p2p","message_type":"post",
                "content":encode({"content":[[{"tag":"text","text":text},{"tag":"img","image_key":"img_local_fixture"}]]})})
            receipt = inbox.deliver(mid)
            self.assertEqual(inbox.deliver(mid),receipt)
            observed = self.read_native(native,"read")
            sha = hashlib.sha256(PNG).hexdigest()
            self.assertEqual(observed["inputs"],[receipt["native_id"]])
            self.assertEqual(observed["mediaRequests"],[
                {"caption":text,"images":[{"sha256":sha,"size":len(PNG)}],"partTypes":["text","image"]}])
            self.assertEqual(receipt["media_sha256"],[sha])
            self.assertEqual((runtime/"media"/sha).read_bytes(),PNG)
            sink = PlatformFixture()
            views = Views(runtime/"views.db",platform,sink,"private-chat","小栗帽",clock)
            deadline = time.monotonic()+8
            while len(sink.messages)<4 and time.monotonic()<deadline:
                views.poll()
                time.sleep(.05)
            sent = list(sink.messages.values())
            self.assertEqual([message[1] for message in sent],["interactive","interactive","image","interactive"])
            self.assertEqual(sink.images,[PNG])
            self.assertFalse(loop.hermes.tasks()["items"])
        output = os.environ.get("MOWULINK_TEST_DSH_MEDIA_PROCESS_EVIDENCE")
        if output:
            Path(output).write_text(encode({"separate_adapter_process":True,"input_receipt":receipt,
                "native_input_count":len(observed["inputs"]),"caption":text,"image_sha256":sha,
                "ordered_feishu_fixture_messages":[{"message_id":r[0],"kind":r[1]} for r in sent]})+"\n",
                encoding="utf-8")

    def test_managed_sessions_switch_and_recover_over_mutual_tls(self):
        self.managed_session_switches(use_tls=True)

    def test_read_only_cancel_and_transfer_over_mutual_tls(self):
        self.read_only_cleanup(use_tls=True)

    def test_read_only_cleanup_over_mutual_tls_requires_proof(self):
        self.read_only_cleanup_refusals(use_tls=True)

    def test_command_approval_uses_native_decision_and_closure_over_mutual_tls(self):
        self.native_command_decisions(use_tls=True)

    def test_foreground_question_closes_exact_original_over_mutual_tls(self):
        self.foreground_questions(use_tls=True)

    def test_foreground_question_host_rejects_a_ledger_readable_by_other_users(self):
        self.assert_host_refuses_exposed_ledger(
            "dsh-questions.sqlite3","foreground-relay","native_question_unconfirmed")

    def test_read_only_host_rejects_an_admission_ledger_readable_by_other_users(self):
        self.assert_host_refuses_exposed_ledger(
            "dsh-admissions.sqlite3","cleanup-read-only","native_cleanup_unconfirmed")

    def assert_host_refuses_exposed_ledger(self,filename,mode,posix_error):
        directory = self.local_tls_directory()
        ledger = directory/filename
        ledger.touch(0o600)
        if os.name=="nt":
            icacls = Path(os.environ["SystemRoot"])/"System32/icacls.exe"
            subprocess.run([icacls,ledger,"/grant","*S-1-5-32-545:(R)"],check=True,capture_output=True)
            code = "private_dsh_link_required"
        else:
            ledger.chmod(0o644)
            code = posix_error
        with self.assertRaisesRegex(AssertionError,code), \
             self.start_native(directory,"dsh-insecure-ledger",mode,use_tls=True):
            self.fail("DSH accepted a non-private ledger")
        self.assertEqual(ledger.stat().st_size,0)


@unittest.skipUnless(EXE.is_file(),"installed macOS DSH SDK required")
class NativeLoop(NativeProcess):
    def test_original_turn_stops_queued_chat_survives_and_native_request_uses_new_effort(self):
        observed = observe()
        self.assertEqual(observed["turns"],[[1,"aborted","user"],[2,"completed",None],[3,"completed",None]])
        original, control = observed["original"],observed["control"]
        self.assertEqual(original["session"],control["session"])
        self.assertNotEqual(original["turn_id"],control["turn_id"])
        self.assertEqual(observed["admitted"],[original["native_id"],"keep-private",control["native_id"]])
        self.assertEqual([(r["command"],r["reasoningEffort"]) for r in observed["modelRequests"]],
                         [("task","high"),("private","high"),("task.cancel","high")])

    def test_dsh_native_command_decisions_match_persisted_approval_ids(self):
        self.native_command_decisions(use_tls=False)

    def test_dsh_approval_pins_implicit_and_relative_native_working_directory(self):
        session = "isolated-dsh-approval-working-directory"
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-approval-cwd-")))
        nested,physical = root/"approval-child",root/"physical-directory"
        nested.mkdir()
        (physical/"nested").mkdir(parents=True)
        (root/"approval-link").symlink_to(physical/"nested",target_is_directory=True)
        worker_token,operator_token,adapter_token = (guid() for _ in range(3))
        router = self.enterContext(codex_driver.running(serve_loopback(Orders(root/"router.db"),
                                         {worker_token:"worker/dev",operator_token:"approver/dev"})))
        host = DshHost(root)
        adapter = HostAdapter(root/"adapter.db",host,session)
        worker = Worker(root/"worker.db","dev",Client(router,worker_token),adapter)
        endpoint = self.open_worker(WorkerService(worker),{adapter_token:"adapter/dev"})
        worker.connect()
        callback,operator = Client(endpoint,adapter_token),Client(router,operator_token)
        scenarios = [
            ("implicit",None,root,"allowed-once",True),
            ("relative","approval-child",nested,"allowed-once",True),
            ("physical-parent","approval-link/..",physical,"allowed-once",True),
            ("waiting-change",None,root,"unavailable",False),
            ("dispatch-change",None,root,"allowed-once",False),
            ("after-pin-change",None,root,"allowed-once",True),
        ]
        outcomes = {}

        def wait(predicate):
            deadline = time.monotonic()+6
            while time.monotonic()<deadline:
                result = predicate()
                if result:
                    return result
                time.sleep(.05)
            self.fail("DSH did not produce the expected approval and cwd proof")

        def pending():
            return next((row for row in operator.request("GET","approvals/dev")["items"]
                         if row["state"]=="pending"),None)

        with tool_listener(root,adapter,callback), self.start_native(root,session,"command-approvals") as native:
            for mode,requested,effective,native_outcome,ran in scenarios:
                worker.chat(guid(),"DSH approval fixture "+mode)
                offered = wait(pending)
                request = offered["request"]
                self.assertEqual((request["session"],request["cwd"],request["command"]),
                                 (session,str(effective.resolve()),"pwd"))
                self.assertEqual(request["arguments"].get("workdir"),requested)
                if requested is None:
                    self.assertNotIn("workdir",request["arguments"])
                if mode=="waiting-change":
                    self.assertEqual(self.read_native(native,"set-approval-cwd "+str(nested))["cwd"],
                                     str(nested.resolve()))
                aid = offered["approval_id"]
                route = "approvals/dev/"+aid
                decision = {"request_digest":offered["request_digest"],"decision":"accept"}
                operator.request("PUT",route+"/decision",decision)
                history = self.read_native(native,"read")
                asked,decided = [event for event in history["approvals"] if event["data"]["id"]==aid]
                self.assertEqual(asked["data"]["callId"],"fixture-bash-"+mode)
                self.assertEqual(decided["data"]["outcome"],native_outcome)
                result, = [item for item in history["results"] if item["toolCallId"]==asked["data"]["callId"]]
                self.assertEqual(result["isError"],not ran)
                if ran:
                    self.assertIn(str(effective.resolve()),encode(result))
                if mode=="dispatch-change":
                    self.assertIn("DSH Bash approval no longer matches the working directory",encode(result))
                if mode in ("waiting-change","dispatch-change"):
                    self.assertEqual(self.read_native(native,"set-approval-cwd "+str(root))["cwd"],str(root.resolve()))

                def closed():
                    record = operator.request("GET",route)
                    return record if record["state"]=="native_closed" else None

                settled = wait(closed)
                expected = None if mode=="waiting-change" else {"source":"operator","decision":"accept"}
                self.assertEqual(settled["attempted_response"],expected)
                self.assertEqual(operator.request("PUT",route+"/decision",decision),settled)
                outcomes[mode] = {"approval_id":aid,"turn_id":request["turn_id"],"native_outcome":native_outcome,
                                  "bash_executed":ran,"asked_seq":asked["seq"],"decided_seq":decided["seq"],
                                  "effective_directory":effective.resolve().relative_to(root.resolve()).as_posix(),
                                  "original_workdir":requested,"router_attempted_response":expected}
            history = self.read_native(native,"read")
            self.assertEqual(sum(record["isError"] is False for record in history["results"]),4)
            notices = operator.frames(0)
            self.assertEqual([item["payload"]["approval_id"] for item in notices],
                             [record["approval_id"] for record in outcomes.values()])
        self.evidence = {"session":session,"outcomes":outcomes,"bash_success_count":4,
                         "boundary":"Installed DSH native AgentLoop, ApprovalService and Bash. Model, operator and directory changes are fixtures."}

    def test_dsh_unacknowledged_approval_recovers_without_a_late_notification(self):
        session = "isolated-dsh-unacknowledged-approval"
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-approval-outage-")))
        worker_token,operator_token,adapter_token,target_token,platform_token = (guid() for _ in range(5))
        router = self.enterContext(codex_driver.running(serve_loopback(Orders(root/"router.db"),
                                         {worker_token:"worker/dev",operator_token:"approver/dev"})))
        host = DshHost(root)
        adapter_path = root/"adapter.db"
        target = "isolated-dsh-approval-switch-target"
        target_host = FixtureHost(root/"target-native.db",target)
        ports = {}
        for sid,adapter in ((session,HostAdapter(adapter_path,host,session)),
                            (target,HostAdapter(root/"target.db",target_host,target))):
            token = guid()
            url = self.enterContext(codex_driver.running(serve_adapter(adapter,"dev",
                {token:"worker/dev"},("127.0.0.1",0),None)))
            ports[sid] = RemoteAdapter(Client(url,token),sid)
        worker = Worker(root/"worker.db","dev",Client(router,worker_token),ports[session],
                        resume_targets={target:ports[target]})
        service = WorkerService(worker)
        endpoint = self.open_worker(service,{adapter_token:"adapter/dev/"+session,
            target_token:"adapter/dev/"+target,platform_token:"platform/dev"})
        platform = Client(endpoint,platform_token)

        def switch(mid):
            return platform.request("POST","worker/command",
                {"message_id":mid,"text":"/resume "+target})["receipt"]
        worker.connect()
        operator = Client(router,operator_token)
        callback = Client(endpoint,adapter_token)
        send = callback.request
        cut = Event()
        blocked = Event()
        mode = "missing"
        offers,closure_replies = {},{}

        def fault(method,path,body=None,*args,**kwargs):
            if cut.is_set() and path.startswith("worker/approvals/"):
                if method=="POST":
                    aid = path.split("/")[-1]
                    if mode=="committed":
                        offers[aid] = send(method,path,body,*args,**kwargs)
                    blocked.set()
                raise ConnectionError("isolated approval transport interrupted")
            result = send(method,path,body,*args,**kwargs)
            if method=="PUT" and path.endswith("/closed"):
                receipts = closure_replies.setdefault(result["approval_id"],[])
                receipts.append(result)
                if len(receipts)==1:
                    raise ConnectionError("isolated close response lost after commit")
            return result

        callback.request = fault

        def until(check):
            deadline = time.monotonic()+8
            while time.monotonic()<deadline:
                value = check()
                if value:
                    return value
                time.sleep(.05)
            self.fail("DSH closure was not recovered")

        def lookup(aid):
            return next((r for r in operator.request("GET","approvals/dev")["items"] if r["approval_id"]==aid),None)

        evidence = {}
        notices = []
        for mode in ("missing","committed"):
            blocked.clear()
            cut.set()
            adapter = HostAdapter(adapter_path,host,session)
            fixture = "command-approvals" if mode=="missing" else "command-approvals-reopen"
            with tool_listener(root,adapter,callback), self.start_native(root,session,fixture) as native:
                service.probe_adapter()
                prompt = {"message_id":guid(),"text":"DSH approval fixture "+mode}
                worker.chat(prompt["message_id"],prompt["text"])
                self.assertTrue(blocked.wait(5))
                state = self.read_native(native,"approval-state")
                asked, = (event["data"] for event in state if event["type"]=="approval/asked"
                          and event["data"]["callId"]=="fixture-bash-"+mode)
                aid = asked["id"]
                self.assertFalse(any(event["type"]=="approval/decided" and event["data"]["id"]==aid for event in state))
                with adapter.db.transaction() as db:
                    original = db.execute("SELECT * FROM dsh_approvals WHERE approval_id=?",(aid,)).fetchone()
                self.assertEqual((original["published"],original["attempted_response"],original["closure"]),(0,None,None))
                if mode=="missing":
                    self.assertIsNone(lookup(aid))
                    self.assertEqual(operator.frames(0),[])
                else:
                    row = offers[aid]
                    decision = {"request_digest":row["request_digest"],"decision":"accept"}
                    self.assertEqual(operator.request("PUT","approvals/dev/"+aid+"/decision",decision)["state"],"decided")
                    notices = operator.frames(0)
                    self.assertEqual([notice["payload"]["approval_id"] for notice in notices],[aid])
                before = self.read_native(native,"cancel-approval")
                source,decision_event = [event for event in before["approvals"] if event["data"]["id"]==aid]
                outcome = decision_event["data"]["outcome"]
                self.assertEqual(source["type"],"approval/asked")
                self.assertEqual(decision_event["type"],"approval/decided")
                self.assertNotEqual(outcome,"allowed-once")
                self.assertTrue(all(result.get("isError") is not False for result in before["results"]))
                self.assertEqual(before["turns"][-1]["reason"]["kind"],"aborted")
                until(lambda:not ports[session].busy())
                self.assertTrue(ports[session].handoff_activity(session=session)["approval_pending"])
                refused = switch("om_isolated_dsh_approval_"+mode)
                self.assertNotIn("binding_receipt",refused)
                self.assertEqual(worker.native_session_pin,session)
                self.assertFalse(service.status().get("handoff"))
                self.assertEqual(target_host.entries(),[])
            cut.clear()
            reopened = HostAdapter(adapter_path,host,session)
            with tool_listener(root,reopened,callback), self.start_native(root,session,"command-approvals-reopen") as native:
                after = self.read_native(native,"read")
                until(lambda:len(closure_replies.get(aid,[]))>=2)
                receipt = lookup(aid)
                self.assertEqual(closure_replies[aid],[receipt,receipt])
                self.assertEqual(operator.frames(0),notices,"a closed DSH request emitted a fresh approval notice")
                self.assertEqual((after["inputs"],after["approvals"],after["results"]),
                                 (before["inputs"],before["approvals"],before["results"]))
                proof = {"approval_id":aid,"turn_id":receipt["request"]["turn_id"],"asked_seq":source["seq"],
                         "decided_seq":decision_event["seq"],"outcome":outcome}
                self.assertEqual(receipt["native_proof"],proof)
                self.assertEqual((receipt["state"],receipt["decision"],receipt["attempted_response"]),
                                 ("native_closed","accept" if mode=="committed" else None,None))
                rolled_in = send("POST","worker/approvals/"+aid,receipt["request"])
                self.assertEqual(rolled_in,receipt)
                self.assertEqual(operator.frames(0),notices)
                self.assertEqual(switch("om_isolated_dsh_approval_"+mode),refused)
                until(lambda:not ports[session].handoff_activity(session=session)["approval_pending"])
                evidence[mode] = {"proof":proof,"router_decision":receipt["decision"],"forwarded_decision":None,
                                  "same_closure_after_lost_reply":True,"original_router_notices":len(notices),
                                  "switch_refused_before_closure":refused}
                if mode=="committed":
                    self.assertTrue(worker.chat(prompt["message_id"],prompt["text"]))
                    try_switch = switch("om_isolated_dsh_approval_closed")
                    self.assertEqual(try_switch["binding_receipt"]["session"],target)
                    self.assertEqual(target_host.entries(),[])
        self.evidence = {"session":session,"recovered":evidence,"new_notices_after_recovery":0,
                         "binding_after_closure":try_switch["binding_receipt"],
                         "boundary":"Installed DSH AgentLoop and native event log, local model, target and operator fixtures; approval receipt failure injected."}
        if output := os.environ.get("MOWULINK_DSH_APPROVAL_HANDOFF_EVIDENCE"):
            Path(output).write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_closed_native_binding_never_resumes_or_sends_to_a_new_agent(self):
        session = "dsh-binding-close"
        with tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-binding-") as directory:
            with self.start_native(directory,session,"binding-close") as native:
                host = DshHost(Path(directory))
                self.assertFalse(host.busy(session))
                closed = self.read_native(native,"close")
                self.assertEqual(closed,{"controllerResolutions":1,"live":False,"sameAgent":False,"inputs":0})
                for operation in ("activity","display-start","session-catalog"):
                    with self.assertRaisesRegex(Fault,"native_agent_unavailable") as error:
                        host.request(operation,session)
                    self.assertEqual(error.exception.status,503)
                self.assertEqual(self.read_native(native,"status"),closed)
                reopened = self.read_native(native,"reopen")
                self.assertEqual(reopened,{"controllerResolutions":2,"live":True,"sameAgent":False,"inputs":0})
                payload = {"kind":"chat","text":"Must not reach a replacement agent","message_id":"closed-test"}
                digest = hashlib.sha256(encode(payload).encode()).hexdigest()
                with self.assertRaisesRegex(Fault,"native_agent_unavailable"):
                    host.deliver(session,"chat-closed-test",payload,digest)
                self.assertEqual(self.read_native(native,"status"),reopened)

    def test_cold_dsh_requires_explicit_activation_and_keeps_active_goals_unloaded(self):
        sid = "isolated-dsh-cold-active-goal"
        with tempfile.TemporaryDirectory(dir="/tmp",prefix="ml-dsh-cold-goal-") as directory:
            with self.start_native(directory,sid,"cold-resume-goal") as native:
                host = DshHost(Path(directory))
                activation = DshActivation(host,sid)
                before = self.read_native(native,"status")
                self.assertEqual((before["controllerResolutions"],before["live"]),(0,False))
                self.assertEqual(len(before["modelRequests"]),1)
                self.assertEqual(activation.state,"cold")
                self.assertEqual(host.capabilities(sid),{})
                with self.assertRaisesRegex(Fault,"native_session_cold"):
                    host.busy(sid)
                with self.assertRaisesRegex(Fault,"native_cold_goal_active"):
                    activation.activate(sid,guid())
                self.assertEqual(activation.state,"cold")
                self.assertEqual(self.read_native(native,"status"),before)
                self.evidence = before | {"session":sid,"refusal":"native_cold_goal_active"}

    @unittest.skipUnless(shutil.which("openssl"),"isolated TLS certificate generator required")
    def test_managed_dsh_creates_and_forks_through_the_worker_and_recovers_a_lost_reply(self):
        self.managed_session_switches()

    def test_dsh_preparation_reopens_the_exact_native_child_after_ipc_ack_loss(self):
        sid = "isolated-dsh-preparation-source"
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="ml-dsh-prepare-"))).resolve()
        (root/"enrolled").mkdir(mode=0o700)
        ledger = root/"preparations.db"
        first_op,second_op = guid(),guid()
        plans = {}

        class InterruptedHost(DshHost):
            def request(self,operation,session,fields=None):
                if operation=="prepare-thread":
                    op = fields["operation_id"]
                    plans[op] = fields["target_session"]
                    if op==first_op:
                        super().request(operation,session,fields)
                    raise Fault(503,"adapter_connection_unconfirmed",True)
                return super().request(operation,session,fields)

        with self.start_native(root,sid,"managed-sessions") as native:
            prep = DshPreparation(InterruptedHost(root),sid,ledger)
            for op in (first_op,second_op):
                with self.assertRaisesRegex(Fault,"adapter_connection_unconfirmed"):
                    prep.prepare(sid,op,"fork")
                self.assertEqual(prep.prepared(op),{})
            self.assertEqual(self.read_native(native,"read "+plans[first_op])["parent"],sid)
            self.assertEqual(self.read_native(native,"read "+plans[second_op]),
                             {"session":plans[second_op],"exists":False})
            host = DshHost(root)
            host.request("model-change",sid,{"kind":"efforts","value":"high"})
        with self.start_native(root,sid,"managed-sessions-reopen") as native:
            prep = DshPreparation(DshHost(root),sid,ledger)
            recorded = prep.prepare(sid,first_op,"fork")
            self.assertEqual(recorded["session"],plans[first_op])
            self.assertEqual(prep.prepare(sid,first_op,"fork"),recorded)
            with self.assertRaisesRegex(Fault,"native_preparation_source_changed"):
                prep.prepare(sid,second_op,"fork")
            absent = self.read_native(native,"read "+plans[second_op])
            self.assertEqual(absent,{"session":plans[second_op],"exists":False})
            self.assertEqual(self.read_native(native,"status")["modelRequests"],[])
        output = os.environ.get("MOWULINK_DSH_PREPARATION_EVIDENCE")
        if output:
            evidence = {"native_preparation":recorded,"replayed_operation":first_op,
                        "changed_parent_operation":second_op,"changed_parent_target":absent,
                        "changed_parent_code":"native_preparation_source_changed",
                        "scope":"Installed DSH SDK, IPC response lost before adapter receipt. Local fixture profile."}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    @unittest.skipUnless(shutil.which("openssl"),"isolated TLS certificate generator required")
    def test_cold_dsh_reopens_only_the_authorized_session_and_recovers_lost_activation_reply(self):
        sid = "isolated-dsh-cold-handoff"
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="ml-dsh-cold-")))
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock))
        source_session = loop.native.session
        source_token,target_token,platform_token,source_callback,target_callback = (guid() for _ in range(5))
        source_url = self.enterContext(codex_driver.running(serve_adapter(
            loop.adapter,"oguri",{source_token:"worker/oguri"},("127.0.0.1",0),None)))
        primary = RemoteAdapter(Client(source_url,source_token),source_session)
        target = RemoteAdapter(None,sid)
        worker = Worker(loop.root/"cold-worker.db","oguri",loop.wire,primary,clock=clock,resume_targets={sid:target})
        service = WorkerService(worker,clock)
        worker_url = self.open_worker(service,{platform_token:"platform/oguri",
            source_callback:"adapter/oguri/"+source_session,target_callback:"adapter/oguri/"+sid})
        platform = Client(worker_url,platform_token)
        inbox_db = loop.root/"cold-platform.db"
        inbox = Inbound(inbox_db,platform,None,"app","trainer","chat",clock=clock)
        sink = PlatformFixture()
        views = Views(inbox_db,platform,sink,"chat","隔离 DSH",clock)
        to_worker = config(loop.root,"cold-to-worker",{"url":worker_url,"token":target_callback})
        foreign = guid()
        roles = config(loop.root,"target-roles",{target_token:"worker/oguri",foreign:"worker/foreign"})
        cert,key = loop.root/"tls.crt",loop.root/"tls.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",key],
                       capture_output=True,timeout=10,check=True)
        adapter_db = loop.root/"cold-dsh-adapter.db"
        extra = ("--worker-id","oguri","--session",sid,"--worker-connection",to_worker,"--runtime-dir",directory,
                 "--resume-on-handoff")
        package = Path(__file__).resolve().parents[1]

        def record(mid,text):
            return inbox.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":mid,
                "message_type":"text","content":encode({"text":text})})

        activation_calls = []
        def connect(url,lose_reply=False):
            transport = target.client = Client(url,target_token,cert)
            request = transport.request

            def inspect(method,path,*args,**kwargs):
                if path=="host/bound/activate":
                    activation_calls.append(args[0]["operation_id"])
                    if lose_reply:
                        self.assertTrue(loop.hermes.workers()["items"][0]["admission_paused"])
                        self.assertEqual(worker.native_session_pin,source_session)
                    reply = request(method,path,*args,**kwargs)
                    if lose_reply:
                        raise ConnectionError("lost activation receipt after DSH resolves original session")
                    return reply
                return request(method,path,*args,**kwargs)

            transport.request = inspect

        with self.start_native(directory,sid,"cold-resume") as native, \
             serve(package,"dsh-adapter",adapter_db,roles,cert,key,dict(os.environ),extra) as url:
            connect(url,lose_reply=True)
            service.step()
            initial = self.read_native(native,"status")
            self.assertEqual((initial["controllerResolutions"],initial["live"]),(0,False))
            self.assertEqual(len(initial["modelRequests"]),1)
            self.assertEqual(target.session_info()["native_load_state"],"cold")
            with self.assertRaisesRegex(Fault,"native_activation_unavailable"):
                Client(url,target_token,cert).request("POST","host/activate",{"operation_id":guid()})
            with self.assertRaisesRegex(Fault,"adapter_identity_unconfirmed"):
                RemoteAdapter(Client(url,foreign,cert),sid).activate(sid,guid())
            with self.assertRaisesRegex(Fault,"native_session_cold"):
                target.deliver("cold-wrong-input",{"kind":"chat","text":"Must not be delivered while cold"})
            inbox.deliver(record("om_dsh_cold_catalog","/resume"))
            views.poll()
            self.assertEqual(self.read_native(native,"status"),initial)
            select = record("om_dsh_cold_select","/resume "+sid)
            with self.assertRaisesRegex(Fault,"native_switch_host_unconfirmed"):
                inbox.deliver(select)
            resolved = self.read_native(native,"status")
            self.assertEqual(resolved,initial | {"controllerResolutions":1,"live":True})
            self.assertEqual(target.session_info()["native_load_state"],"loaded")
            self.assertEqual(worker.native_session_pin,source_session)
            self.assertTrue(loop.hermes.workers()["items"][0]["admission_paused"])
            waiting = loop.hermes.create(guid(),"Isolated cold DSH report","Return a native W04 result","oguri")["task_id"]
            self.assertIsNone(loop.hermes.inspect(waiting)["task"]["assignment_id"])
            service.step()
            switched = inbox.deliver(select)
            op = switched["binding_receipt"]["handoff_id"]
            self.assertEqual((switched["binding_receipt"]["session"],worker.native_session_pin),(sid,sid))
            self.assertEqual(activation_calls,[op])
            service.step()
            clock.advance(10)
            deadline = time.monotonic()+10
            while time.monotonic()<deadline:
                service.step()
                done = loop.hermes.inspect(waiting)
                public = [entry["event"] for entry in platform.request("GET","worker/display")["items"]]
                finals = [event for event in public if event["kind"]=="final" and event["session"]==sid]
                if done["task"]["state"]=="completed_reported" and finals and not target.busy():
                    break
                time.sleep(.03)
            self.assertEqual(done["task"]["state"],"completed_reported",self.read_native(native,"status"))
            self.assertEqual(len(finals),1)
            self.assertFalse(any("Seed the isolated historical session" in event.get("text","") for event in public))
            evidence = self.read_native(native,"read")
            self.assertEqual(evidence["inputs"][0],"seed-cold-dsh")
            self.assertEqual(len(evidence["inputs"]),2)
            aid = done["last_report"]["assignment_id"]
            expected = {"recorded":True,"task_state":"completed_reported","release_assignment_id":aid}
            self.assertEqual([json.loads(result["content"][0]["text"]) for result in evidence["results"]],[expected,expected])
            self.assertEqual(worker.assignment_session(aid),sid)
            self.assertIsNone(worker.hold()["held_assignment_id"])
            notice, = loop.manager_wire.frames(0)
            self.assertEqual(notice["payload"]["task_id"],waiting)
            self.assertEqual(loop.native.entries(),[])
            # Closing the native object cannot silently attach a replacement through the same plugin.
            closed = self.read_native(native,"close")
            self.assertEqual((closed["controllerResolutions"],closed["live"]),(1,False))
            with self.assertRaisesRegex(Fault,"native_agent_unavailable"):
                RemoteAdapter(Client(url,target_token,cert),sid).activate(sid,op)
            self.assertEqual(self.read_native(native,"status"),closed)

        with self.start_native(directory,sid,"cold-resume-reopen") as native, \
             serve(package,"dsh-adapter",adapter_db,roles,cert,key,dict(os.environ),extra) as url:
            connect(url)
            self.assertEqual(target.session_info()["native_load_state"],"cold")
            self.assertEqual(self.read_native(native,"status"),{"controllerResolutions":0,"live":False,"modelRequests":[]})
            clock.advance(5)
            service.step()
            self.assertEqual(service.status()["host_session"],sid)
            self.assertEqual(activation_calls,[op,op])
            self.assertEqual(inbox.deliver(select),switched)
            reopened = self.read_native(native,"read")
            self.assertEqual(reopened["modelRequests"],[])
            self.assertEqual(reopened["inputs"],evidence["inputs"])
            self.assertEqual(reopened["results"],evidence["results"])
            self.assertEqual(loop.hermes.inspect(waiting)["last_report"],done["last_report"])
        self.evidence = {"session":sid,"binding":switched["binding_receipt"],"cold_observation":initial,
            "native_activation_calls":activation_calls,"inputs":evidence["inputs"],"task_id":waiting,
            "assignment_id":aid,"native_tool_calls":[call["callId"] for call in evidence["calls"]],
            "formal_receipt":expected,"notification_id":notice["delivery_id"],"restarted_model_calls":0,
            "final_display_event":finals[0]["event_id"],"old_input_republished":False,
            "scope":"Installed DSH SDK AgentRegistry, persistence and tools, TLS adapter and isolated Router. "
                    "SessionController wiring, model and Feishu are fixtures."}

    def test_native_tool_executor_files_w04_through_the_worker(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-formal-")))
        self.formal_report(directory)

    def test_read_only_dsh_acknowledges_original_cancel_or_transfer_and_recovers_w04_reply(self):
        self.read_only_cleanup()

    def container_engine(self):
        image = os.environ.get("MOWULINK_DSH_CONTAINER_IMAGE")
        docker = os.environ.get("MOWULINK_DSH_TEST_DOCKER") or shutil.which("docker")
        if not image or not docker:
            self.skipTest("configured isolated local Docker image required")
        socket = os.environ.get("MOWULINK_DSH_TEST_DOCKER_ENDPOINT")
        if socket is None:
            socket = subprocess.check_output([docker,"context","inspect","--format","{{ .Endpoints.docker.Host }}"],
                                             text=True,timeout=5).strip()
        self.assertTrue(socket.startswith("unix:///"),socket)
        return image,[docker,"--host",socket]

    def clean_fixture_containers(self, runtime, session, cli):
        for owned in (runtime/"assignment-containers").glob("*.json"):
            entry = json.loads(owned.read_text())
            if entry.get("session")!=session or not entry.get("token"):
                continue
            ids = subprocess.check_output([*cli,"container","ls","--all","--no-trunc","--filter",
                "label=io.mowulink.assignment="+entry["token"],"--format","{{.ID}}"],text=True,timeout=8).split()
            for identity in ids:
                subprocess.run([*cli,"container","rm","--force",identity],check=True,timeout=8,capture_output=True)

    def test_dsh_workspace_container_cancel_and_transfer_remove_owned_processes(self):
        image,cli = self.container_engine()
        docker,_,socket = cli
        cases = []
        for action in ("cancel","transfer","foreign_engine"):
            with self.subTest(action=action):
                session = "isolated-dsh-contained-workspace-"+action
                directory,loop,service,_ = self.goal_worker(session,extra_workers=("tama",))
                runtime = Path(directory)
                (runtime/"assignment-container.json").write_text(encode(
                    {"docker":docker,"socket":socket.removeprefix("unix://"),"image":image}),encoding="utf-8")
                sentinel = runtime/"outside-marker"
                sentinel.write_text("never touched",encoding="utf-8")
                token = guid()
                client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
                worker = loop.worker
                alternate = FixtureHost(loop.root/"alternate.db","alternate-container-test")
                other = Worker(loop.root/"alternate-worker.db","tama",loop.extra_clients["tama"],
                               HostAdapter(loop.root/"alternate-adapter.db",alternate,alternate.session),clock=service.clock)
                order = loop.hermes.create(guid(),"Read-write containment","End the isolated write","oguri")["task_id"]
                with tool_listener(runtime,loop.adapter,client), self.start_native(directory,session,"cleanup-container") as native:
                    try:
                        other.connect()
                        worker.connect()
                        worker.poll()
                        other.poll()
                        service.clock.advance(10)
                        worker.poll()
                        worker.poll()
                        self.assertTrue(self.read_native(native,"work-started")["running"])
                        state = loop.hermes.inspect(order)["task"]
                        assignment = state["assignment_id"]
                        lease_path, = (runtime/"assignment-containers").glob("*.json")
                        lease = json.loads(lease_path.read_text())
                        container_id = lease["container_id"]
                        self.assertEqual(lease["assignment_id"],assignment)
                        self.assertEqual(lease["phase"],"active")
                        self.assertRegex(container_id,r"^[a-f0-9]{64}$")
                        def docker_cmd(*args):
                            return subprocess.check_output([*cli,*args],text=True,timeout=8).strip()
                        recorded = docker_cmd("exec",container_id,"/bin/sh","-c",
                            'cat /workspace/marker; echo; n=$(cat /workspace/pid); test -e "/proc/$n/cmdline"; cat "/proc/$n/cmdline"')
                        self.assertEqual(recorded.splitlines()[0],"owned-write")
                        self.assertIn("/bin/sleep",recorded)
                        peer_message = guid()
                        self.assertIsNone(worker.chat(peer_message,"Keep private input outside the job"))
                        next_worker = {"worker_id":"tama"} if action=="transfer" else {}
                        if action=="foreign_engine":
                            lease_path.write_text(encode(dict(lease,daemon="other-isolated-engine")),encoding="utf-8")
                        operation = "transfer" if action=="transfer" else "cancel"
                        loop.hermes.act(guid(),order,state["revision"],operation,reason="Stop original execution scope",**next_worker)
                        recipient = None
                        if action=="transfer":
                            other.poll()
                            recipient = loop.hermes.inspect(order)["task"]
                            self.assertNotEqual(recipient["assignment_id"],assignment)
                            self.assertEqual(other.hold()["held_assignment_id"],recipient["assignment_id"])
                        worker.poll()
                        observed = self.read_native(native,"read")
                        self.assertEqual(observed["turns"][0]["reason"]["kind"],"aborted")
                        report = next(row for row in observed["results"] if row["toolCallId"]=="cleanup-report-"+assignment)
                        if action=="foreign_engine":
                            self.assertTrue(report["isError"],report)
                            self.assertIn("native_cleanup_unconfirmed",encode(report))
                            self.assertEqual(worker.hold()["held_assignment_id"],assignment)
                            self.assertEqual([item["assignment_id"] for item in
                                loop.hermes.inspect(order)["pending_cleanup"]],[assignment])
                            self.assertEqual(docker_cmd("exec",container_id,"cat","/workspace/marker"),"owned-write")
                            cases.append({"action":action,"session":session,"task_id":order,"assignment_id":assignment,
                                "container_id":container_id,"native_stop":"aborted","cleanup":"native_cleanup_unconfirmed",
                                "working_container_untouched":True})
                            continue
                        self.assertFalse(report["isError"],report)
                        with worker.db.transaction() as db:
                            record = db.execute("SELECT proof,receipt FROM cleanup_reports WHERE assignment_id=?",
                                                (assignment,)).fetchone()
                        proof = json.loads(record["proof"])
                        self.assertEqual((proof["scope"],proof["workspace"]["container_id"],proof["workspace"]["state"]),
                                         ("assignment_container",container_id,"removed"))
                        self.assertEqual(json.loads(record["receipt"])["release_assignment_id"],assignment)
                        self.assertIsNone(worker.hold()["held_assignment_id"])
                        current = loop.hermes.inspect(order)
                        self.assertEqual(current["pending_cleanup"],[])
                        if recipient:
                            self.assertEqual(current["task"],recipient)
                        else:
                            self.assertEqual(current["task"]["state"],"cancelled")
                        self.assertEqual(docker_cmd("container","ls","--all","--no-trunc","--filter","id="+container_id,
                                                    "--format","{{.ID}}"),"")
                        control_kind = "task.transfer_out" if action=="transfer" else "task.cancel"
                        self.assertEqual(loop.adapter.cleanup_evidence(assignment,control_kind),proof)
                        self.assertEqual(json.loads(lease_path.read_text())["phase"],"sealed")
                        retained = worker.chat(peer_message,"Keep private input outside the job")
                        self.assertEqual(observed["inputs"].count(retained["native_id"]),1)
                        self.assertEqual(sentinel.read_text(),"never touched")
                        cases.append({"action":action,"session":session,"task_id":order,"assignment_id":assignment,
                            "new_assignment_id":recipient["assignment_id"] if recipient else None,"container_id":container_id,
                            "writable_marker_read_back":True,"detached_child_seen_before_removal":True,
                            "container_absent_after_removal":True,"workspace_scope":proof["workspace"],
                            "w04":json.loads(record["receipt"]),"retained_private_input":retained["native_id"],
                            "original_native_turn_end":observed["turns"][0]["reason"]["kind"],
                            "boundary":"Actual installed DSH SDK, Linux Docker engine, HTTP Router and Worker. Model and inputs are local fixtures."})
                    finally:
                        self.clean_fixture_containers(runtime,session,cli)
        if output := os.environ.get("MOWULINK_DSH_CONTAINER_EVIDENCE"):
            Path(output).write_text(json.dumps({"cases":cases},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_native_dsh_work_survives_one_broken_link(self):
        image,cli = self.container_engine()
        cases = []
        for broken,action in (("router","transfer"),("adapter","cancel"),("router","cancel")):
            with self.subTest(link=broken,action=action):
                session = "dsh-native-link-"+broken+"-"+action
                directory,loop,service,_ = self.goal_worker(session,extra_workers=("tama",))
                runtime = Path(directory)
                clock,worker = service.clock,loop.worker
                (runtime/"assignment-container.json").write_text(encode(
                    {"docker":cli[0],"socket":cli[2].removeprefix("unix://"),"image":image}),encoding="utf-8")
                self.addCleanup(self.clean_fixture_containers,runtime,session,cli)

                native_key,platform_key,host_key = guid(),guid(),guid()
                worker_url = self.open_worker(service,{native_key:"adapter/oguri",platform_key:"platform/oguri"})
                callback,platform = Client(worker_url,native_key),Client(worker_url,platform_key)
                sink = PlatformFixture()
                views = Views(loop.root/"outage-views.db",platform,sink,"fixture-chat","小栗帽",clock)
                manager = loop.hermes
                router = serve_loopback(loop.orders,{loop.wire.token:"worker/oguri"})
                adapter = serve_adapter(loop.adapter,"oguri",{host_key:"worker/oguri"},("127.0.0.1",0),None)
                online = Event()
                online.set()
                drops = []
                def allow_connection(_socket,_client):
                    if online.is_set():
                        return True
                    drops.append(clock())
                    return False
                (router if broken=="router" else adapter).verify_request = allow_connection

                def container(*args):
                    return subprocess.check_output([*cli,*args],text=True,timeout=8).strip()

                with (codex_driver.running(router) as router_url, codex_driver.running(adapter) as adapter_url,
                      tool_listener(runtime,loop.adapter,callback), self.start_native(runtime,session,
                      "cleanup-container-link-outage") as native):
                    worker.router = Client(router_url,loop.wire.token)
                    worker.adapter = RemoteAdapter(Client(adapter_url,host_key),session)
                    service.step()
                    clock.advance(10)
                    service.step()
                    tid = manager.create(guid(),"Isolated link failure","Keep the original native work","oguri")["task_id"]
                    service.step()
                    self.assertTrue(self.read_native(native,"work-started")["running"])
                    working = manager.inspect(tid)["task"]
                    aid = working["assignment_id"]
                    self.assertEqual(worker.hold()["held_assignment_id"],aid)
                    lease_file, = (runtime/"assignment-containers").glob("*.json")
                    cid = json.loads(lease_file.read_text())["container_id"]
                    with worker.db.transaction() as db:
                        dispatch = json.loads(db.execute("""SELECT frame FROM inbox JOIN jobs USING(delivery_id)
                            WHERE assignment_id=?""",(aid,)).fetchone()[0])
                    payload = {"kind":"task",**dispatch["payload"]}
                    receipt = loop.adapter.lookup_delivery(dispatch["delivery_id"],payload,session=session)
                    self.assertEqual(receipt["session"],session)
                    views.poll()

                    # Reject TCP connections at just one listener. Both clients keep their original endpoints.
                    online.clear()
                    clock.advance(5)
                    service.step()
                    outage = service.status()
                    views.poll()
                    notices = [(mid,body) for mid,_,body in sink.messages.values() if "连接已中断" in encode(body)]
                    self.assertEqual(len(notices),1)
                    target = "Router" if broken=="router" else "宿主适配器"
                    self.assertIn("与"+target+"的连接已中断。",encode(notices[0][1]))
                    self.assertTrue(drops)
                    self.assertEqual(outage["held_assignment_id"],aid)
                    if broken=="router":
                        self.assertFalse(outage["router_connected"])
                        self.assertTrue(outage["adapter_connected"])
                        self.assertTrue(outage["host_busy"])
                        self.assertEqual(outage["router_retry_in_seconds"],1200)
                        self.assertTrue(self.read_native(native,"continue-work")["running"])
                        self.assertEqual(container("exec",cid,"cat","/workspace/after-link-failure"),"continued")
                        first_drops = len(drops)
                        retry_at = clock()+1200
                        with loop.orders.db.transaction() as db:
                            last_seen = db.execute("SELECT last_seen FROM workers WHERE worker_id='oguri'").fetchone()[0]
                        pool_at = last_seen+loop.orders.lease_seconds+600
                        clock.advance(pool_at-clock()-1)
                        loop.orders.tick()
                        self.assertEqual(manager.inspect(tid)["task"],working)
                        clock.advance(1)
                        loop.orders.tick()
                        pooled = manager.inspect(tid)["task"]
                        self.assertEqual((pooled["state"],pooled["holder"]),("blocked",{"kind":"router"}))
                        self.assertEqual(worker.hold()["held_assignment_id"],aid)
                        clock.advance(retry_at-clock()-1)
                        service.step()
                        self.assertEqual(service.status()["router_retry_in_seconds"],1)
                        self.assertEqual(len(drops),first_drops)
                        if action=="cancel":
                            manager.act(guid(),tid,pooled["revision"],"cancel",reason="Cancel while the owner is offline")
                            pending = manager.inspect(tid)
                            self.assertEqual(pending["task"]["state"],"blocked")
                            self.assertEqual([(r["assignment_id"],r["kind"]) for r in pending["pending_cleanup"]],
                                             [(aid,"task.cancel")])
                    else:
                        self.assertTrue(outage["router_connected"])
                        self.assertFalse(outage["adapter_connected"])
                        self.assertIsNone(outage["host_busy"])
                        for _ in range(32):
                            clock.advance(20)
                            service.step()
                            loop.orders.tick()
                        observed_worker, = manager.workers(worker_id="oguri")["items"]
                        self.assertEqual([observed_worker[k] for k in ("online","busy","adapter_connected","held_assignment_id")],
                                         [True,None,False,aid])
                        self.assertEqual(manager.inspect(tid)["task"],working)
                        alert, = manager.messages(source="oguri")["items"]
                        self.assertEqual(alert["text"],"Worker 本体在线，与宿主适配器的连接已中断。")

                    self.assertEqual(container("exec",cid,"sh","-c",'kill -0 "$(cat /workspace/pid)" && cat /workspace/marker'),"owned-write")
                    views.poll()
                    self.assertEqual(sum("连接已中断" in encode(body) for _,_,body in sink.messages.values()),1)
                    online.set()
                    clock.advance(1 if broken=="router" else 5)
                    service.step()
                    recovered = service.status()
                    self.assertTrue(recovered["router_connected"] and recovered["adapter_connected"])
                    self.assertEqual(recovered["host_session"],session)
                    self.assertTrue(recovered["host_busy"])
                    if action=="transfer":
                        self.assertIsNone(recovered["held_assignment_id"])
                        self.assertEqual(manager.inspect(tid)["task"],pooled)
                        self.assertEqual(container("exec",cid,"cat","/workspace/after-link-failure"),"continued")
                        later = FixtureHost(loop.root/"transfer-host.db","next-holder-fixture")
                        next_worker = Worker(loop.root/"transfer-worker.db","tama",loop.extra_clients["tama"],
                            HostAdapter(loop.root/"transfer-adapter.db",later,later.session),clock=clock)
                        next_worker.connect()
                        next_worker.poll()
                        clock.advance(10)
                        next_worker.poll()
                        manager.act(guid(),tid,pooled["revision"],"transfer",reason="Explicitly transfer the original order",worker_id="tama")
                        next_worker.poll()
                        new_holder = manager.inspect(tid)["task"]
                        self.assertNotEqual(new_holder["assignment_id"],aid)
                        self.assertEqual(next_worker.hold()["held_assignment_id"],new_holder["assignment_id"])
                    elif broken=="adapter":
                        self.assertEqual(recovered["held_assignment_id"],aid)
                        self.assertTrue(self.read_native(native,"continue-work")["running"])
                        self.assertEqual(container("exec",cid,"cat","/workspace/after-link-failure"),"continued")
                        manager.act(guid(),tid,working["revision"],"cancel",reason="Explicitly stop the original order")
                    service.step()
                    end = self.read_native(native,"read")
                    self.assertEqual(end["inputs"].count(receipt["native_id"]),1)
                    self.assertEqual(end["turns"][0]["reason"]["kind"],"aborted")
                    self.assertEqual([call["name"] for call in end["calls"]],
                                     ["worker_workspace_shell","worker_workspace_shell","worker_report"])
                    self.assertTrue(all(not row["isError"] for row in end["results"]),end["results"])
                    report = json.loads(end["results"][-1]["content"][0]["text"])
                    self.assertEqual(report["release_assignment_id"],aid)
                    self.assertEqual(json.loads(lease_file.read_text())["phase"],"sealed")
                    self.assertEqual(container("container","ls","-aq","--filter","id="+cid),"")
                    final = manager.inspect(tid)
                    self.assertIsNone(worker.hold()["held_assignment_id"])
                    self.assertEqual(final["pending_cleanup"],[])
                    if action=="transfer":
                        self.assertEqual(final["task"],new_holder)
                    else:
                        self.assertEqual(final["task"]["state"],"cancelled")
                    cases.append({"broken_link":broken,"explicit_action":action,"task_id":tid,"assignment_id":aid,"container_id":cid,
                        "dropped_tcp_connections":len(drops),"outage":outage,"recovered":recovered,
                        "router_during_outage":pooled if broken=="router" else observed_worker,
                        **({"pending_cancel_while_offline":pending} if broken=="router" and action=="cancel" else {}),
                        "test_platform_disconnect_id":notices[0][0],"w04_cleanup":report,"final_task":final["task"],
                        "host_receipt":receipt,"native_inputs":end["inputs"],"native_calls":end["calls"],
                        "native_turns":end["turns"],"container_removed_after_explicit_control":True})
        if output := os.environ.get("MOWULINK_DSH_LINK_OUTAGE_EVIDENCE"):
            Path(output).write_text(json.dumps({"recorded_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                "boundary":"Installed DSH AgentLoop, Linux Docker, authenticated HTTP and SQLite. Local model, accelerated clock,"
                " Feishu test platform and replacement holder are fixtures.","cases":cases},
                ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_dsh_workspace_exports_artifact_and_seals_after_durable_w04_replay(self):
        image,cli = self.container_engine()
        docker,_,socket = cli
        session = "dsh-isolated-workspace-artifacts"
        directory,loop,service,_ = self.goal_worker(session)
        runtime = Path(directory)
        (runtime/"assignment-container.json").write_text(encode(
            {"docker":docker,"socket":socket.removeprefix("unix://"),"image":image}),encoding="utf-8")
        token = guid()
        client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
        worker = loop.worker
        wire = worker.router.request
        data = bytes(range(256))*2700
        digest = hashlib.sha256(data).hexdigest()
        report_attempts,offsets = [],[]
        loss = True
        self.addCleanup(self.clean_fixture_containers,runtime,session,cli)
        with tool_listener(runtime,loop.adapter,client), self.start_native(directory,session,"cleanup-container-export") as native:
            tid = loop.hermes.create(guid(),"Isolated container artifact","Write and stage the fixture bytes","oguri")["task_id"]
            def lose_receipts(method,path,*args,**kwargs):
                is_report = method=="POST" and path==f"tasks/{tid}/report"
                if is_report:
                    artifact, = args[0]["artifact_ids"]
                    report_attempts.append({"idempotency_key":args[1],"body":args[0]})
                    if loss:
                        with self.assertRaisesRegex(Fault,"attachment_not_found"):
                            loop.manager_wire.request("GET","attachments/"+artifact)
                        self.assertEqual(wire("GET","attachments/"+artifact)["bytes"],data)
                        self.assertEqual(worker.hold()["held_task_id"],tid)
                result = wire(method,path,*args,**kwargs)
                if method=="PATCH" and path.startswith("uploads/"):
                    offsets.append(result["next_offset"])
                    if len(offsets)==1:
                        raise ConnectionError("isolated Router chunk receipt lost")
                if is_report and loss:
                    raise Fault(503,"fixture_w04_receipt_lost",True)
                return result
            worker.router.request = lose_receipts
            worker.connect()
            worker.poll()
            service.clock.advance(10)
            worker.poll()
            worker.poll()
            observed = self.read_native(native,"read")
            self.assertEqual(len(observed["inputs"]),1)
            self.assertEqual([call["name"] for call in observed["calls"]],
                ["worker_workspace_shell",*["worker_workspace_export"]*3,"worker_report"])
            self.assertEqual([call["isError"] for call in observed["results"]],[False,True,True,False,True],observed["results"])
            self.assertIn("workspace_artifact_unreadable",encode(observed["results"][1]))
            self.assertIn("fixture_w04_receipt_lost",encode(observed["results"][4]))
            self.assertEqual(offsets,[256*1024,512*1024,len(data)])
            self.assertEqual(len(report_attempts),1)
            staged = json.loads(observed["results"][3]["content"][0]["text"])
            artifact, = staged["artifact_ids"]
            assignment = report_attempts[0]["body"]["assignment_id"]
            lease_path, = (runtime/"assignment-containers").glob("*.json")
            lease = json.loads(lease_path.read_text())
            self.assertEqual((lease["assignment_id"],lease["phase"]),(assignment,"active"))
            scope = {"session":session,"task_id":tid,"assignment_id":assignment}
            def persisted_receipt(w):
                with w.db.transaction() as db:
                    return db.execute("SELECT receipt FROM outbox WHERE assignment_id=?",(assignment,)).fetchone()[0]
            def container_exists():
                return bool(subprocess.check_output([*cli,"container","ls","--all","--no-trunc","--filter",
                    "id="+lease["container_id"],"--format","{{.ID}}"],timeout=8).strip())
            self.assertIsNone(persisted_receipt(worker))
            self.assertEqual(client.request("POST","worker/workspace-state",scope),{**scope,"state":"inactive"})
            self.assertEqual(worker_resume.source_admission(worker,session),("source_workspace_pending",False))
            self.assertTrue(container_exists())
            details = loop.hermes.inspect(tid)
            self.assertEqual(details["last_report"]["artifact_ids"],[artifact])
            self.assertEqual(details["task"]["state"],"completed_reported")
            file = loop.manager_wire.request("GET","attachments/"+artifact)
            self.assertEqual(file["bytes"],data)
            self.assertEqual(file["headers"]["X-Content-SHA256"],digest)

            # The new Worker replays the persisted W04 body with its original idempotency key.
            loss = False
            recovered = Worker(worker.db.path,worker.worker_id,worker.router,worker.adapter,clock=service.clock)
            service.worker = recovered
            recovered.connect()
            self.assertEqual(report_attempts,[report_attempts[0],report_attempts[0]])
            receipt = json.loads(persisted_receipt(recovered))
            self.assertEqual(receipt["release_assignment_id"],assignment)
            self.assertIsNone(recovered.hold()["held_assignment_id"])
            self.assertEqual(client.request("POST","worker/workspace-state",scope),{**scope,"state":"released"})
            until = time.monotonic()+10
            while json.loads(lease_path.read_text())["phase"]!="sealed" and time.monotonic()<until:
                time.sleep(.05)
            self.assertEqual(json.loads(lease_path.read_text())["phase"],"sealed")
            self.assertFalse(container_exists())
            self.assertEqual(worker_resume.source_admission(recovered,session),(None,False))
            self.assertEqual(loop.hermes.inspect(tid),details)
            notify, = loop.manager_wire.frames(0)
            self.assertEqual(notify["payload"]["attachment_ids"],[artifact])
            self.assertEqual(self.read_native(native,"read")["inputs"],observed["inputs"])
            with recovered.db.transaction() as db:
                local, = db.execute("SELECT attachment_id,sha256 FROM uploads").fetchall()
            self.assertEqual(local["sha256"],digest)
        if output := os.environ.get("MOWULINK_DSH_WORKSPACE_ARTIFACT_EVIDENCE"):
            evidence = {**scope,"artifact_id":artifact,"local_artifact_id":local["attachment_id"],"bytes":len(data),
                "sha256":digest,"native_input":observed["inputs"][0],"native_calls":observed["calls"],
                "upload_offsets":offsets,"symlink_rejected":True,"container_id":lease["container_id"],
                "container_present_without_w04_receipt":True,"handoff_waited_for_cleanup":True,"w04":receipt,
                "w04_attempts":report_attempts,"container_absent_after_replay":True,
                "manager_notification":notify["delivery_id"],"router_state":details["task"]["state"],
                "boundary":"Installed DSH AgentLoop, real isolated Linux Docker, Worker and Router. Local scripted model. No Feishu."}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def seed_runtime(self, session, runtime, cli, image, archive, digest, *, docker=None, execute_workspace_files=None):
        executable,_,socket = cli
        self.assertEqual(archive.stat().st_mode & 0o077,0)
        options = {"docker":docker or executable,"socket":socket.removeprefix("unix://"),"image":image,
                   "seed":{"archive":str(archive),"sha256":digest}}
        if execute_workspace_files is not None:
            options["execute_workspace_files"] = execute_workspace_files
        (runtime/"assignment-container.json").write_text(encode(options),encoding="utf-8")
        self.addCleanup(self.clean_fixture_containers,runtime,session,cli)

    def dispatch_seed_work(self, loop, clock, subject, instructions):
        task = loop.hermes.create(guid(),subject,instructions,"oguri")["task_id"]
        clock.advance(1)
        loop.worker.poll()
        clock.advance(10)
        loop.worker.poll()
        loop.worker.poll()
        return task

    def create_project_seed(self, runtime, *, compiled=False):
        repo = runtime/"test-project"
        git = ["/usr/bin/git","-C",str(repo)]
        env = {"PATH":os.defpath,"HOME":str(runtime),"GIT_CONFIG_NOSYSTEM":"1"}
        repo.mkdir()
        subprocess.run([*git,"init","--quiet"],env=env,check=True,capture_output=True,timeout=5)
        source = repo/"hello.txt"
        source.write_bytes(b"original project\n")
        script = repo/"checks"/"fixture.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\nexit 0\n",encoding="utf-8")
        script.chmod(0o700)
        files = ["hello.txt","checks/fixture.sh"]
        if compiled:
            (repo/"main.c").write_text('#include <stdio.h>\nint main(void) { puts("verified assignment build"); return 0; }\n',
                                      encoding="utf-8")
            files.append("main.c")
        subprocess.run([*git,"add",*files],env=env,check=True,timeout=5)
        subprocess.run([*git,"-c","user.name=Fixture Author","-c","user.email=fixture@example.invalid",
            "-c","commit.gpgsign=false","commit","--quiet","-m","Isolated source"],env=env,check=True,capture_output=True,timeout=5)
        archive = runtime/"approved-project.tar"
        with open(archive,"wb",opener=lambda name,flags:os.open(name,flags,0o600)) as out:
            subprocess.run([*git,"archive","--format=tar","HEAD"],env=env,stdout=out,check=True,timeout=5)
        revision = subprocess.check_output([*git,"rev-parse","HEAD"],env=env,text=True,timeout=5).strip()
        return archive,hashlib.sha256(archive.read_bytes()).hexdigest(),source,revision

    def test_dsh_workspace_seeds_private_project_and_recovers_seed_ack(self):
        image,cli = self.container_engine()
        session = "dsh-isolated-seeded-project"
        directory,loop,service,_ = self.goal_worker(session)
        runtime = Path(directory)
        seed,digest,source,revision = self.create_project_seed(runtime)
        dropped = runtime/"lost-seed-ack"
        docker = shlex.quote(cli[0])
        wrapper = runtime/"fixture-docker"
        wrapper.write_text(f"""#!/bin/sh
if [ "$5" = container ] && [ "$6" = exec ] && [ "$7" = '-i' ] && [ ! -e {shlex.quote(str(dropped))} ]; then
    {docker} "$@" || exit $?
    : > {shlex.quote(str(dropped))}
    exit 77
fi
exec {docker} "$@"
""",encoding="utf-8")
        wrapper.chmod(0o700)
        self.seed_runtime(session,runtime,cli,image,seed,digest,docker=str(wrapper))
        token = guid()
        client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
        own = runtime/"assignment-containers"
        seen = []
        with tool_listener(runtime,loop.adapter,client), self.start_native(directory,session,"cleanup-container-seed") as native:
            loop.worker.connect()
            for label in ("lose seed acknowledgement","normal second assignment"):
                task = self.dispatch_seed_work(loop,service.clock,"Seeded project",label)
                observed = self.read_native(native,"read")
                details = loop.hermes.inspect(task)
                self.assertEqual(details["task"]["state"],"completed_reported",(label,details,observed["results"]))
                status = details["last_report"]
                aid = status["assignment_id"]
                calls = [row for row in observed["results"] if row["toolCallId"].startswith("workspace-seed-"+aid+"-")]
                if not seen:
                    self.assertTrue(dropped.is_file())
                    self.assertEqual([row["isError"] for row in calls],[True,False,False,False],calls)
                    self.assertIn("workspace_seed_unconfirmed",encode(calls[0]))
                else:
                    self.assertEqual([row["isError"] for row in calls],[False,False,False],calls)
                self.assertEqual(json.loads(calls[-3]["content"][0]["text"])["exit_code"],0,calls[-3])
                artifact, = status["artifact_ids"]
                exported = loop.manager_wire.request("GET","attachments/"+artifact)
                self.assertEqual(exported["bytes"],b"modified result\n")
                self.assertEqual(source.read_bytes(),b"original project\n")
                self.assertEqual(len(observed["inputs"]),len(seen)+1)
                records = [path for path in own.glob("*.json") if json.loads(path.read_bytes())["assignment_id"]==aid]
                journal, = records
                until = time.monotonic()+8
                while (lease:=json.loads(journal.read_bytes()))["phase"]!="sealed" and time.monotonic()<until:
                    time.sleep(.05)
                self.assertEqual((lease["phase"],lease["seed_sha256"]),("sealed",digest))
                self.assertFalse(subprocess.check_output([*cli,"container","ls","--all","--filter",
                    "id="+lease["container_id"],"--format","{{.ID}}"],timeout=5).strip())
                seen.append({"task_id":task,"assignment_id":aid,"container_id":lease["container_id"],
                    "native_input":observed["inputs"][-1],"tool_ids":[row["toolCallId"] for row in calls],
                    "artifact_id":artifact,"artifact_sha256":exported["headers"]["X-Content-SHA256"],
                    "phase":lease["phase"],"state":details["task"]["state"]})
            self.assertNotEqual(seen[0]["container_id"],seen[1]["container_id"])
            notices = loop.manager_wire.frames(0)
            self.assertEqual([f["payload"]["attachment_ids"] for f in notices],[[item["artifact_id"]] for item in seen])
        if output := os.environ.get("MOWULINK_DSH_SEED_EVIDENCE"):
            evidence = {"session":session,"fixture_git_revision":revision,"seed_sha256":digest,
                "source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"first_seed_reply_lost":True,
                "cases":seen,"manager_notifications":[item["delivery_id"] for item in notices],
                "boundary":"Installed DSH AgentLoop, isolated Linux Docker, source Git fixture, Worker and Router. Local scripted model."}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_dsh_workspace_refuses_unverified_or_escaping_project_seeds(self):
        image,cli = self.container_engine()
        failures = []
        for case in ("wrong_digest","path_outside","symbolic_link","hard_link"):
            with self.subTest(case=case):
                session = "dsh-ineligible-seed-"+case
                directory,loop,service,_ = self.goal_worker(session)
                runtime = Path(directory)
                archive = runtime/"untrusted-project.tar"
                with open(archive,"wb",opener=lambda name,flags:os.open(name,flags,0o600)) as output:
                    with tarfile.open(fileobj=output,mode="w") as tar:
                        raw = b"original project\n"
                        entry = tarfile.TarInfo("hello.txt")
                        entry.size = len(raw)
                        tar.addfile(entry,BytesIO(raw))
                        if case=="path_outside":
                            tar.addfile(tarfile.TarInfo("../escaped.txt"))
                        if case in ("symbolic_link","hard_link"):
                            trap = tarfile.TarInfo("escape")
                            trap.type = tarfile.LNKTYPE if case=="hard_link" else tarfile.SYMTYPE
                            trap.linkname = "/etc/passwd"
                            tar.addfile(trap)
                digest = "0"*64 if case=="wrong_digest" else hashlib.sha256(archive.read_bytes()).hexdigest()
                self.seed_runtime(session,runtime,cli,image,archive,digest)
                token = guid()
                client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
                with tool_listener(runtime,loop.adapter,client), self.start_native(directory,session,"cleanup-container-seed-invalid") as native:
                    loop.worker.connect()
                    task = self.dispatch_seed_work(loop,service.clock,"Reject seed",case)
                    observed = self.read_native(native,"read")
                    row, = observed["results"]
                    self.assertTrue(row["isError"],row)
                    self.assertIn("workspace_seed_unconfirmed",encode(row))
                    self.assertEqual(loop.hermes.inspect(task)["task"]["state"],"working")
                    journal, = (runtime/"assignment-containers").glob("*.json")
                    lease = json.loads(journal.read_bytes())
                    self.assertEqual(lease["phase"],"unused" if case=="wrong_digest" else "seeding")
                    if lease["container_id"]:
                        subprocess.run([*cli,"exec",lease["container_id"],"sh","-c",
                            "test ! -e /workspace/project && test ! -e /workspace/escaped.txt"],
                            check=True,timeout=5,capture_output=True)
                    else:
                        self.assertEqual(case,"wrong_digest")
                    failures.append({"case":case,"task_id":task,"phase":lease["phase"],"published":False})
        if output := os.environ.get("MOWULINK_DSH_SEED_REFUSAL_EVIDENCE"):
            Path(output).write_text(json.dumps(failures,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_dsh_workspace_restart_cannot_reseed_or_confirm_lost_work(self):
        image,cli = self.container_engine()
        session = "dsh-restarted-seeded-container"
        directory,loop,service,_ = self.goal_worker(session)
        runtime = Path(directory)
        archive,digest,source,_ = self.create_project_seed(runtime)
        self.seed_runtime(session,runtime,cli,image,archive,digest)
        token = guid()
        client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
        with tool_listener(runtime,loop.adapter,client), self.start_native(directory,session,"cleanup-container-seed-restart") as native:
            loop.worker.connect()
            task = self.dispatch_seed_work(loop,service.clock,"Restart test","Test lost tmpfs")
            self.assertTrue(self.read_native(native,"workspace-ready")["running"])
            journal, = (runtime/"assignment-containers").glob("*.json")
            lease = json.loads(journal.read_bytes())
            container = lease["container_id"]
            self.assertEqual(lease["phase"],"active")
            self.assertEqual(subprocess.check_output([*cli,"exec",container,"cat","/workspace/project/hello.txt"],timeout=5),
                             b"modified result\n")
            subprocess.run([*cli,"container","restart","--timeout","1",container],check=True,timeout=10,capture_output=True)
            started = subprocess.check_output([*cli,"container","inspect","--format","{{.State.StartedAt}}",container],
                                              text=True,timeout=5).strip()
            self.assertNotEqual(started,lease["started_at"])
            subprocess.run([*cli,"exec",container,"sh","-c","test ! -e /workspace/project"],
                           check=True,timeout=5,capture_output=True)
            observed = self.read_native(native,"resume-workspace")
            self.assertEqual([row["isError"] for row in observed["results"]],[False,True,True])
            for refused in observed["results"][1:]:
                self.assertIn("assignment_workspace_restarted",encode(refused))
            self.assertEqual(source.read_bytes(),b"original project\n")
            self.assertFalse(loop.manager_wire.frames(0))
            state = loop.hermes.inspect(task)["task"]
            self.assertEqual(state["state"],"working")
            self.assertEqual(loop.worker.hold()["held_assignment_id"],lease["assignment_id"])
            loop.hermes.act(guid(),task,state["revision"],"cancel",reason="Close isolated restart test")
            self.assertEqual(loop.hermes.inspect(task)["pending_cleanup"][0]["assignment_id"],lease["assignment_id"])
            loop.worker.poll()
            after = self.read_native(native,"read")
            closed = next(row for row in after["results"] if row["toolCallId"]=="cleanup-report-"+lease["assignment_id"])
            self.assertFalse(closed["isError"],closed)
            self.assertEqual(json.loads(journal.read_bytes())["phase"],"sealed")
            self.assertIsNone(loop.worker.hold()["held_assignment_id"])
            state = loop.hermes.inspect(task)
            self.assertEqual(state["pending_cleanup"],[])
            self.assertEqual(state["task"]["state"],"cancelled")
        if output := os.environ.get("MOWULINK_DSH_SEED_RESTART_EVIDENCE"):
            evidence = {"session":session,"task_id":task,"assignment_id":lease["assignment_id"],
                "container_id":container,"started_at_before":lease["started_at"],"started_at_after":started,
                "shell_and_export_error":"assignment_workspace_restarted","released_by":"cancel_confirmed"}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_dsh_workspace_optional_execution_builds_runs_and_reports(self):
        image,cli = self.container_engine()
        tested = []
        for execute in (None,True):
            with self.subTest(execute_workspace_files=execute):
                session = "dsh-compiled-project-"+("enabled" if execute else "default")
                directory,loop,service,_ = self.goal_worker(session)
                runtime = Path(directory)
                archive,seed,source,revision = self.create_project_seed(runtime,compiled=True)
                self.seed_runtime(session,runtime,cli,image,archive,seed,execute_workspace_files=execute)
                token = guid()
                callback = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
                with tool_listener(runtime,loop.adapter,callback), self.start_native(runtime,session,"cleanup-container-build") as native:
                    loop.worker.connect()
                    task = self.dispatch_seed_work(loop,service.clock,"Compile test project","Compile the approved working copy")
                    self.assertTrue(self.read_native(native,"build-finished")["running"])
                    order = loop.hermes.inspect(task)["task"]
                    aid = order["assignment_id"]
                    self.assertEqual(loop.worker.hold()["held_assignment_id"],aid)
                    journal, = (runtime/"assignment-containers").glob("*.json")
                    lease = json.loads(journal.read_bytes())
                    self.assertEqual((lease["assignment_id"],lease["phase"]),(aid,"active"))
                    self.assertEqual(lease.get("workspace_exec"),execute)
                    container, = json.loads(subprocess.check_output([*cli,"container","inspect",lease["container_id"]],timeout=5))
                    limits = container["HostConfig"]
                    self.assertEqual((limits["NetworkMode"],limits["IpcMode"],limits["Privileged"],limits["ReadonlyRootfs"]),
                                     ("none","private",False,True))
                    self.assertEqual((limits["PidsLimit"],limits["Memory"],limits["NanoCpus"]),(64,256*1024*1024,10**9))
                    self.assertFalse(limits["Binds"])
                    self.assertFalse(limits.get("CapAdd"))
                    self.assertEqual(limits["CapDrop"],["ALL"])
                    self.assertIn("no-new-privileges",limits["SecurityOpt"])
                    self.assertEqual(container["Config"]["User"],"65534:65534")
                    self.assertTrue(all(mount["Type"]=="tmpfs" for mount in container["Mounts"]))
                    program = subprocess.check_output([*cli,"container","exec",lease["container_id"],
                                                       "cat","/workspace/build/hello"],timeout=5)
                    self.assertEqual(program[:4],b"\x7fELF")
                    observed = self.read_native(native,"finish-build")
                    self.assertEqual(len(observed["inputs"]),1)
                    self.assertEqual([c["name"] for c in observed["calls"]],
                        ["worker_workspace_shell",*(["worker_workspace_export"] if execute else []),"worker_report"])
                    self.assertTrue(all(not item["isError"] for item in observed["results"]))
                    build = json.loads(observed["results"][0]["content"][0]["text"])
                    mount = dict(line.split(" ",1) for line in build["stdout"].splitlines() if line.startswith("/"))
                    self.assertIn("noexec",mount["/tmp"].split(","))
                    self.assertEqual("noexec" in mount["/workspace"].split(","),not execute)
                    receipt = json.loads(observed["results"][-1]["content"][0]["text"])
                    state = "completed_reported" if execute else "blocked"
                    self.assertEqual(receipt,{"recorded":True,"release_assignment_id":aid,"task_state":state})
                    details = loop.hermes.inspect(task)
                    self.assertEqual(details["task"]["state"],state)
                    files = details["last_report"]["artifact_ids"]
                    notice, = loop.manager_wire.frames(0)
                    self.assertEqual(notice["payload"]["attachment_ids"],files)
                    if execute:
                        self.assertEqual(build["exit_code"],0)
                        self.assertIn("verified assignment build",build["stdout"])
                        artifact, = files
                        received = loop.manager_wire.request("GET","attachments/"+artifact)
                        self.assertEqual(received["bytes"],program)
                        self.assertEqual(received["headers"]["X-Content-SHA256"],hashlib.sha256(program).hexdigest())
                    else:
                        self.assertEqual(build["exit_code"],126)
                        self.assertIn("Permission denied",build["stderr"])
                        self.assertEqual(files,[])
                    deadline = time.monotonic()+8
                    while json.loads(journal.read_bytes())["phase"]!="sealed" and time.monotonic()<deadline:
                        time.sleep(.05)
                    self.assertEqual(json.loads(journal.read_bytes())["phase"],"sealed")
                    self.assertIsNone(loop.worker.hold()["held_assignment_id"])
                    self.assertFalse(subprocess.check_output([*cli,"container","ls","--all","--filter",
                        "id="+lease["container_id"],"--format","{{.ID}}"],timeout=5).strip())
                    self.assertFalse((source.parent/"build").exists())
                    tested.append({"execute_workspace_files":bool(execute),"session":session,"task_id":task,"assignment_id":aid,
                        "container_id":lease["container_id"],"source_revision":revision,"source_digest":seed,
                        "configured_tmpfs":limits["Tmpfs"],"actual_mount_options":mount,"build":build,"w04":receipt,
                        "artifact_ids":files,"binary_sha256":hashlib.sha256(program).hexdigest(),
                        "notify":notice["delivery_id"],"container_removed":True})
        if output := os.environ.get("MOWULINK_DSH_BUILD_EVIDENCE"):
            evidence = {"cases":tested,"scope":"Actual DSH AgentLoop, Linux Docker compiler, Worker and isolated Router with scripted model."}
            Path(output).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_dsh_workspace_execution_policy_is_pinned_until_cancel(self):
        image,cli = self.container_engine()
        tested = []
        for execute in (False,True):
            with self.subTest(previous_enable=execute):
                session = "dsh-execution-policy-"+("disable" if execute else "enable")
                directory,loop,service,_ = self.goal_worker(session)
                runtime = Path(directory)
                archive,seed,_,_ = self.create_project_seed(runtime,compiled=True)
                self.seed_runtime(session,runtime,cli,image,archive,seed,execute_workspace_files=execute)
                token = guid()
                client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
                with tool_listener(runtime,loop.adapter,client), self.start_native(runtime,session,"cleanup-container-build-hold") as native:
                    loop.worker.connect()
                    task = self.dispatch_seed_work(loop,service.clock,"Keep compiled workspace","Leave the current assignment open")
                    first = self.read_native(native,"read")
                    build = json.loads(first["results"][0]["content"][0]["text"])
                    self.assertEqual(build["exit_code"],0 if execute else 126)
                    previous = loop.hermes.inspect(task)["task"]
                    aid = previous["assignment_id"]
                    self.assertEqual(previous["state"],"working")
                    journal, = (runtime/"assignment-containers").glob("*.json")
                    lease = json.loads(journal.read_bytes())
                    container = lease["container_id"]
                options = runtime/"assignment-container.json"
                options.write_text(encode({**json.loads(options.read_bytes()),"execute_workspace_files":not execute}),
                                   encoding="utf-8")
                with tool_listener(runtime,loop.adapter,client), self.start_native(runtime,session,"cleanup-container-build-reopen") as native:
                    cold = self.read_native(native,"read")
                    self.assertEqual(cold["inputs"],first["inputs"])
                    self.assertEqual(cold["modelRequests"],[])
                    update = loop.hermes.update(guid(),task,previous["revision"],{"instructions":"Try this existing workspace"})
                    loop.worker.poll()
                    observed = self.read_native(native,"read")
                    refused,exported = observed["results"][-2:]
                    self.assertEqual(refused["toolCallId"],"workspace-build-task_update-0")
                    self.assertTrue(refused["isError"])
                    self.assertIn("workspace_execution_policy_changed",encode(refused))
                    self.assertFalse(exported["isError"],exported)
                    artifact, = json.loads(exported["content"][0]["text"])["artifact_ids"]
                    with self.assertRaisesRegex(Fault,"attachment_not_found"):
                        loop.manager_wire.request("GET","attachments/"+artifact)
                    binary = subprocess.check_output([*cli,"exec",container,"cat","/workspace/build/hello"],timeout=5)
                    self.assertEqual(loop.wire.request("GET","attachments/"+artifact)["bytes"],binary)
                    self.assertEqual(loop.worker.hold()["held_assignment_id"],aid)
                    self.assertEqual(json.loads(journal.read_bytes()),lease)
                    self.assertEqual(loop.manager_wire.frames(0),[])
                    self.assertTrue(subprocess.check_output([*cli,"container","ls","--filter","id="+container,
                        "--format","{{.ID}}"],timeout=5).strip())
                    loop.hermes.act(guid(),task,update["revision"],"cancel",reason="End the fixture assignment")
                    loop.worker.poll()
                    completed = self.read_native(native,"read")
                    last, = [row for row in completed["results"] if row["toolCallId"]=="cleanup-report-"+aid]
                    self.assertFalse(last["isError"],last)
                    self.assertEqual(json.loads(journal.read_bytes())["phase"],"sealed")
                    self.assertFalse(subprocess.check_output([*cli,"container","ls","--all","--filter","id="+container,
                        "--format","{{.ID}}"],timeout=5).strip())
                    self.assertIsNone(loop.worker.hold()["held_assignment_id"])
                    state = loop.hermes.inspect(task)
                    self.assertEqual(state["task"]["state"],"cancelled")
                    self.assertEqual(state["pending_cleanup"],[])
                tested.append({"session":session,"task_id":task,"assignment_id":aid,"container_id":container,
                    "recorded_execution":execute,"proposed_execution":not execute,"refusal":"workspace_execution_policy_changed",
                    "stage_only_artifact":artifact,"cleanup_outcome":"cancel_confirmed","native_inputs":completed["inputs"]})
        if output := os.environ.get("MOWULINK_DSH_BUILD_POLICY_EVIDENCE"):
            Path(output).write_text(json.dumps(tested,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    @unittest.skipUnless(shutil.which("openssl"),"isolated TLS certificate generator required")
    def test_dsh_workspaces_cold_restart_recovers_unreported_work_and_committed_w04(self):
        image,cli = self.container_engine()
        session = "dsh-cold-project-recovery"
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="ml-dsh-workspace-")))
        runtime = root/"host"
        runtime.mkdir(mode=0o700)
        archive,seed,source,_ = self.create_project_seed(runtime)
        self.seed_runtime(session,runtime,cli,image,archive,seed)
        cert,key = root/"tls.crt",root/"tls.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",key],
                       capture_output=True,timeout=10,check=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert,key)
        router_worker,hermes_token,adapter_token,callback_token,ops_token = (guid() for _ in range(5))
        adapter_roles = config(root,"adapter-roles",{adapter_token:"worker/oguri"})
        worker_roles = config(root,"worker-roles",{callback_token:"adapter/oguri",ops_token:"ops/oguri"})
        clock = Clock()
        orders = Orders(root/"router.db",clock,poll_seconds=1)
        lose_report = Event()
        lose_report.set()
        w04_attempts = []
        task_id = None

        def router_request(actor,method,path,body,headers):
            reply = route(orders,actor,method,path,body,headers)
            if actor=="worker/oguri" and method=="POST" and path==f"/v1/tasks/{task_id}/report":
                w04_attempts.append({"key":headers["Idempotency-Key"],"body":body})
                if lose_report.is_set():
                    raise Fault(503,"fixture_cold_w04_reply_lost",True)
            return reply

        router = serve_authenticated({router_worker:"worker/oguri",hermes_token:"hermes"},
                                     ("127.0.0.1",0),tls,router_request,orders.tick)
        self.enterContext(codex_driver.running(router))
        base = "https://127.0.0.1:"+str(router.server_port)
        manage = Client(base,hermes_token,cert)
        to_router = config(root,"to-router",{"url":base,"token":router_worker,"ca_file":str(cert)})
        package = Path(__file__).resolve().parents[1]
        env = {"HOME":str(root),"PATH":os.defpath,"PYTHONDONTWRITEBYTECODE":"1"}
        process_observations = []

        @contextmanager
        def station(state,stage,mode):
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1",0))
                worker_port = reservation.getsockname()[1]
                worker_url = f"https://127.0.0.1:{worker_port}"
                to_worker = config(root,stage+"-worker",{"url":worker_url,"token":callback_token,"ca_file":str(cert)})
                host_options = ("--worker-id","oguri","--session",session,"--runtime-dir",runtime,
                                "--worker-connection",to_worker)
                with self.start_native(runtime,session,mode) as native, \
                     serve(package,"dsh-adapter",state/"adapter.db",adapter_roles,cert,key,env,host_options) as host_url:
                    to_adapter = config(root,stage+"-adapter",{"url":host_url,"token":adapter_token,"ca_file":str(cert)})
                    worker_options = ("--worker-id","oguri","--port",str(worker_port),"--router-connection",to_router,
                                      "--adapter-session",session,"--adapter-connection",to_adapter)
                    reservation.close()
                    with serve(package,"worker",state/"worker.db",worker_roles,cert,key,env,worker_options) as url:
                        self.assertEqual(url,worker_url)
                        process_observations.append({"stage":stage,"dsh_pid":native.pid,
                            "worker_tls_port":worker_port,"adapter_tls_port":int(host_url.rsplit(":",1)[1])})
                        yield native,Client(url,ops_token,cert),Client(url,callback_token,cert)
            self.assertIsNotNone(native.poll())

        def wait_for(read,predicate):
            end = time.monotonic()+10
            while True:
                value = read()
                if predicate(value):
                    return value
                self.assertLess(time.monotonic(),end,value)
                time.sleep(.05)

        def copy_pair(source_dir,target_dir):
            target_dir.mkdir(mode=0o700)
            for name in ("adapter.db","worker.db"):
                with state_writer(source_dir/name):
                    self.assertFalse(any((source_dir/(name+suffix)).exists() for suffix in ("-journal","-wal","-shm")))
                    shutil.copy2(source_dir/name,target_dir/name)

        def read_outbox(directory):
            uri = (directory/"worker.db").as_uri()+"?mode=ro"
            with closing(sqlite3.connect(uri,uri=True)) as db:
                row = db.execute("SELECT body,receipt FROM outbox WHERE assignment_id=?",(assignment,)).fetchone()
            return None if row is None else (json.loads(row[0]),json.loads(row[1]) if row[1] else None)

        def project_file():
            return subprocess.check_output([*cli,"exec",lease["container_id"],"cat","/workspace/project/hello.txt"],timeout=5)

        def observed_start():
            return subprocess.check_output([*cli,"container","inspect","--format","{{.State.StartedAt}}",
                                            lease["container_id"]],text=True,timeout=5).strip()

        before,trial,recovered = (root/name for name in ("before","trial","recovered"))
        before.mkdir(mode=0o700)
        first_task = manage.request("POST","tasks",{"title":"Cold-restart project",
            "instructions":"Write locally and leave the formal order open","assignee_id":"oguri"},guid())
        task_id = first_task["task_id"]
        route_to_task = f"tasks/{task_id}"
        def inspect():
            return manage.request("GET",route_to_task)
        def idle():
            return manage.request("GET","workers")["items"]
        def connected(client):
            return wait_for(lambda:client.request("GET","worker/status"),
                            lambda state:state["router_connected"] and state["adapter_connected"])

        def check_workspace(callback,expected):
            scope = {"session":session,"task_id":task_id,"assignment_id":assignment}
            self.assertEqual(callback.request("POST","worker/workspace-state",scope),{**scope,"state":expected})

        with station(before,"write","cleanup-container-recovery") as (native,ops,callback):
            self.assertEqual(connected(ops)["host_session"],session)
            wait_for(idle,lambda rows:bool(rows) and rows[0]["busy"] is False)
            clock.advance(10)
            job = wait_for(inspect,lambda row:row["task"]["state"]=="working")
            assignment = job["task"]["assignment_id"]
            jobs = runtime/"assignment-containers"
            journal = wait_for(lambda:list(jobs.glob("*.json")),lambda items:len(items)==1)[0]
            lease = wait_for(lambda:json.loads(journal.read_bytes()),lambda row:row["phase"]=="active")
            initial_native = self.read_native(native,"read")
            self.assertEqual([row["name"] for row in initial_native["calls"]],["worker_workspace_shell"])
            self.assertEqual(json.loads(initial_native["results"][0]["content"][0]["text"])["exit_code"],0)
            self.assertEqual(lease["assignment_id"],assignment)
            self.assertEqual(observed_start(),lease["started_at"])
            self.assertEqual(project_file(),b"modified result\n")
            self.assertEqual(ops.request("GET","worker/status")["held_assignment_id"],assignment)
            self.assertIsNone(read_outbox(before))
            check_workspace(callback,"active")
            self.assertFalse(manage.frames(0))

        original_bytes = {name:hashlib.sha256((before/name).read_bytes()).hexdigest() for name in ("worker.db","adapter.db")}
        copy_pair(before,trial)
        self.assertEqual(project_file(),b"modified result\n")
        with station(trial,"continue","cleanup-container-recovery-reopen") as (native,ops,callback):
            self.assertEqual(connected(ops)["held_assignment_id"],assignment)
            loaded = self.read_native(native,"read")
            self.assertEqual((loaded["inputs"],loaded["results"],loaded["modelRequests"]),
                             (initial_native["inputs"],initial_native["results"],[]))
            self.assertEqual(observed_start(),lease["started_at"])
            updated = manage.request("PATCH",route_to_task,{"expected_revision":job["task"]["revision"],
                "changes":{"instructions":"Export the already modified file and use the original W04 scope"}},guid())
            self.assertEqual(updated["assignment_id"],assignment)
            reported = wait_for(inspect,lambda row:row["task"]["state"]=="completed_reported")
            submitted = self.read_native(native,"read")
            self.assertEqual([row["name"] for row in submitted["calls"]],
                ["worker_workspace_shell","worker_workspace_export","worker_report"])
            self.assertEqual([row["isError"] for row in submitted["results"]],[False,False,True])
            self.assertIn("fixture_cold_w04_reply_lost",encode(submitted["results"][-1]))
            self.assertEqual({row["command"] for row in submitted["modelRequests"]},{"task_update"})
            self.assertEqual(len(submitted["inputs"]),2)
            latest = read_outbox(trial)
            self.assertIsNone(latest[1])
            check_workspace(callback,"inactive")
            self.assertEqual(project_file(),b"modified result\n")
            self.assertEqual(json.loads(journal.read_bytes())["phase"],"active")
            artifact, = reported["last_report"]["artifact_ids"]
            self.assertEqual(manage.request("GET","attachments/"+artifact)["bytes"],b"modified result\n")

        trial_bytes = {name:hashlib.sha256((trial/name).read_bytes()).hexdigest() for name in ("worker.db","adapter.db")}
        copy_pair(trial,recovered)
        self.assertEqual(project_file(),b"modified result\n")
        loss_count = len(w04_attempts)
        self.assertGreaterEqual(loss_count,1)
        lose_report.clear()
        with station(recovered,"recover","cleanup-container-recovery-reopen") as (native,ops,callback):
            status = connected(ops)
            self.assertIsNone(status["held_assignment_id"])
            check_workspace(callback,"released")
            state = wait_for(lambda:json.loads(journal.read_bytes()),lambda row:row["phase"]=="sealed")
            self.assertEqual(state["container_id"],lease["container_id"])
            self.assertEqual(state["started_at"],lease["started_at"])
            self.assertFalse(subprocess.check_output([*cli,"container","ls","--all","--filter",
                "id="+lease["container_id"],"--format","{{.ID}}"],timeout=5).strip())
            restored = self.read_native(native,"read")
            for key in ("inputs","calls","results","turns"):
                self.assertEqual(restored[key],submitted[key])
            self.assertEqual(restored["modelRequests"],[])
            body,receipt = read_outbox(recovered)
            self.assertEqual(body,latest[0])
            self.assertEqual(receipt,{"recorded":True,"release_assignment_id":assignment,"task_state":"completed_reported"})
            self.assertEqual(inspect(),reported)
            event, = manage.frames(0)
            self.assertEqual(event["payload"]["attachment_ids"],[artifact])
            self.assertEqual(event["payload"]["task_id"],task_id)
            self.assertGreater(len(w04_attempts),loss_count)
            self.assertTrue(all(attempt=={"key":"complete-"+assignment,"body":body} for attempt in w04_attempts))
            self.assertEqual(source.read_bytes(),b"original project\n")
        for folder,expected in ((before,original_bytes),(trial,trial_bytes)):
            self.assertEqual({name:hashlib.sha256((folder/name).read_bytes()).hexdigest()
                              for name in expected},expected)
        self.assertEqual(len({entry["dsh_pid"] for entry in process_observations}),3)
        if output := os.environ.get("MOWULINK_DSH_COLD_WORKSPACE_EVIDENCE"):
            result = {"session":session,"task_id":task_id,"assignment_id":assignment,"container_id":lease["container_id"],
                "container_start":lease["started_at"],"project_seed_sha256":seed,"artifact_id":artifact,
                "native_inputs":restored["inputs"],"tools":[row["name"] for row in restored["calls"]],
                "new_model_requests_after_release_replay":len(restored["modelRequests"]),
                "w04_acknowledgements_lost":loss_count,"w04_replays":len(w04_attempts)-loss_count,
                "w04_key":w04_attempts[0]["key"],"w04_receipt":receipt,"manager_notice":event["delivery_id"],
                "container_after_w04":"removed","processes":process_observations,
                "run_scope":"Same isolated Linux engine, actual DSH native persistence and independent TLS adapter/Worker processes; scripted test model, no production credentials."}
            Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def previous_dsh_after_rollback(self,package,root,runtime,session,paths,before,job):
        old_fixture = package/"rewrite/integrations/dsh_host/native-loop-fixture.mjs"
        cert,key = root/"previous-tls.crt",root/"previous-tls.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
            "-addext","subjectAltName=IP:127.0.0.1","-keyout",key,"-out",cert],check=True,timeout=10,capture_output=True)
        worker,manager,adapter,callback,ops,platform = (guid() for _ in range(6))
        router_roles = config(root,"previous-router-roles",{worker:"worker/oguri",manager:"hermes"})
        adapter_roles = config(root,"previous-adapter-roles",{adapter:"worker/oguri"})
        worker_roles = config(root,"previous-worker-roles",{callback:"adapter/oguri",ops:"ops/oguri",platform:"platform/oguri"})
        env = {"HOME":str(root),"PATH":os.defpath,"PYTHONDONTWRITEBYTECODE":"1"}
        mode = "cleanup-container-recovery-reopen"
        with serve(package,"router",paths["router:main"],router_roles,cert,key,env) as routing, \
             self.start_native(runtime,session,mode,fixture=old_fixture) as native, socket.socket() as reserved:
            restored = self.read_native(native,"read")
            self.assertEqual((restored["inputs"],restored["calls"],restored["modelRequests"]),
                             (before["inputs"],before["calls"],[]))
            reserved.bind(("127.0.0.1",0))
            port = reserved.getsockname()[1]
            to_router = config(root,"previous-router",{"url":routing,"token":worker,"ca_file":str(cert)})
            to_worker = config(root,"previous-worker",{"url":f"https://127.0.0.1:{port}","token":callback,"ca_file":str(cert)})
            host_options = ("--worker-id","oguri","--session",session,"--runtime-dir",runtime,
                            "--worker-connection",to_worker)
            with serve(package,"dsh-adapter",paths["agent:oguri"],adapter_roles,cert,key,env,host_options) as host:
                to_adapter = config(root,"previous-adapter",{"url":host,"token":adapter,"ca_file":str(cert)})
                worker_options = ("--worker-id","oguri","--port",str(port),"--adapter-session",session,
                                  "--router-connection",to_router,"--adapter-connection",to_adapter)
                reserved.close()
                with serve(package,"worker",paths["worker:oguri"],worker_roles,cert,key,env,worker_options) as url:
                    status = Client(url,ops,cert)
                    deadline = time.monotonic()+10
                    while True:
                        observed = status.request("GET","worker/status")
                        if observed["router_connected"] and observed["adapter_connected"]:
                            break
                        self.assertLess(time.monotonic(),deadline,observed)
                        time.sleep(.05)
                    self.assertEqual(observed["host_session"],session)
                    self.assertIsNone(observed["held_assignment_id"])
                    management = Client(routing,manager,cert)
                    self.assertEqual(management.request("GET","tasks/"+job["task"]["task_id"]),job)
                    artifact, = job["last_report"]["artifact_ids"]
                    self.assertEqual(management.request("GET","attachments/"+artifact)["bytes"],bytes(range(256))*2700)
                    notice, = management.frames(0)
                    self.assertEqual(notice["payload"]["attachment_ids"],[artifact])
                    chat = {"message_id":guid(),"text":"Isolated first chat after restoring the prior package"}
                    incoming = Client(url,platform,cert)
                    receipt = incoming.request("POST","worker/chat",chat)["receipt"]
                    self.assertEqual(receipt["session"],session)
                    self.assertEqual(incoming.request("POST","worker/chat",chat)["receipt"],receipt)
                    after = self.read_native(native,"read")
                    self.assertEqual(after["inputs"],before["inputs"]+[receipt["native_id"]])
                    self.assertEqual(after["calls"],before["calls"])
                    self.assertEqual([req["command"] for req in after["modelRequests"]],["private"])
        return {"native_session":session,"private_message":chat["message_id"],"native_input":receipt["native_id"],
                "native_replays_of_formal_order":0,"previous_artifact_id":artifact,"outbound_notice":notice["delivery_id"],
                "previous_processes":["Router","Worker","DSH adapter","DSH host"]}

    def test_offline_handoff_waits_for_dsh_workspace_after_w04_release(self):
        image,cli = self.container_engine()
        docker,_,socket = cli
        session = "dsh-offline-workspace-handoff"
        directory,loop,service,_ = self.goal_worker(session)
        runtime = Path(directory)
        (runtime/"assignment-container.json").write_text(encode(
            {"docker":docker,"socket":socket.removeprefix("unix://"),"image":image}),encoding="utf-8")
        self.addCleanup(self.clean_fixture_containers,runtime,session,cli)
        loop.adapter.expected_session = session
        worker = Worker(loop.worker.db.path,"oguri",loop.wire,loop.adapter,clock=service.clock)
        service.worker = worker
        token = guid()
        client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
        task = loop.hermes.create(guid(),"Leave one unreaped workspace","Report without receiving the acknowledgement","oguri")["task_id"]
        request = loop.wire.request
        def lost_report_ack(method,path,*args,**kwargs):
            result = request(method,path,*args,**kwargs)
            if method=="POST" and path==f"tasks/{task}/report":
                raise Fault(503,"fixture_workspace_handoff_ack_lost",True)
            return result
        loop.wire.request = lost_report_ack
        with tool_listener(runtime,loop.adapter,client), self.start_native(runtime,session,"cleanup-container-export") as native:
            service.step()
            self.assertEqual(service.host_session,session)
            self.assertFalse(loop.adapter.handoff_activity().get("workspace_pending"))
            service.clock.advance(10)
            worker.poll()
            worker.poll()
            run = self.read_native(native,"read")
            self.assertIn("fixture_workspace_handoff_ack_lost",encode(run["results"][-1]))
            job = loop.hermes.inspect(task)
            self.assertEqual(job["task"]["state"],"completed_reported")
            aid = job["last_report"]["assignment_id"]
            journal, = (runtime/"assignment-containers").glob("*.json")
            lease = json.loads(journal.read_bytes())
            self.assertEqual((lease["phase"],lease["assignment_id"]),("active",aid))
        # No DSH process is listening to reap the container while W01 and W04 settle the Worker.
        loop.wire.request = request
        worker.connect()
        self.assertIsNone(worker.hold()["held_assignment_id"])
        scope = {"session":session,"task_id":task,"assignment_id":aid}
        self.assertEqual(client.request("POST","worker/workspace-state",scope),{**scope,"state":"released"})
        self.assertEqual(json.loads(journal.read_bytes()),lease)
        self.assertTrue(subprocess.check_output([*cli,"container","ls","--filter","id="+lease["container_id"],
            "--format","{{.ID}}"],timeout=5).strip())
        loop.server.shutdown()
        loop.thread.join()
        trigger = Trigger(loop.root/"trigger.db","offline-audit",service.clock)
        sources = {"router:main":loop.orders.db.path,"worker:oguri":worker.db.path,"agent:oguri":loop.adapter.db.path,
                   "hermes:main":loop.hermes.db.path,"trigger:offline":trigger.db.path}
        snapshot = loop.root/"offline"
        sealed = capture(sources,"a"*40,snapshot)["manifest_sha256"]
        self.assertEqual(verify(snapshot,sealed)["format"],3)
        from_db, = audit_handoff(snapshot,sealed)["workers"]
        self.assertEqual(from_db["issues"],["dsh_workspace_journal_required"])
        self.assertIsNone(from_db["native_session_pending"]["dsh_assignment_workspaces"])
        self.assertFalse(from_db["native_session_ledger_clear"])
        target = loop.root/"staged"
        with self.assertRaisesRegex(ValueError,"dsh_workspace_journal_required"):
            stage_native_handoff(snapshot,sealed,"oguri",session,"other-session",target)
        self.assertFalse(target.exists())

        # The normal data-copy path can preserve original state while the original
        # engine still owns the running container. Version rollback has a stricter gate.
        trial = restore(snapshot,sealed,loop.root/"data-copy")
        rollback_dir = loop.root/"version-rollback"
        with self.assertRaisesRegex(ValueError,"dsh_workspace_journal_required"):
            rollback(snapshot,sealed,trial,rollback_dir)
        self.assertFalse(rollback_dir.exists())

        incorrect = loop.root/"unrelated-runtime"
        (incorrect/"assignment-containers").mkdir(mode=0o700,parents=True)
        incorrect.chmod(0o700)
        wrong, = audit_handoff(snapshot,sealed,dsh_runtimes={"oguri":incorrect})["workers"]
        self.assertEqual(wrong["issues"],["dsh_workspace_journal_mismatch"])
        with self.assertRaisesRegex(ValueError,"dsh_workspace_journal_mismatch"):
            rollback(snapshot,sealed,trial,rollback_dir,dsh_runtimes={"oguri":incorrect})
        self.assertFalse(rollback_dir.exists())
        runtimes = {"oguri":runtime}
        before, = audit_handoff(snapshot,sealed,dsh_runtimes=runtimes)["workers"]
        self.assertEqual(before["native_session_pending"]["held_assignments"],0)
        self.assertEqual(before["native_session_pending"]["report_receipts"],0)
        self.assertEqual(before["issues"],["dsh_assignment_workspace_pending"])
        self.assertEqual(before["dsh_workspace_audit"]["pending"],[{**scope,"phase":"active"}])
        with self.assertRaisesRegex(ValueError,"dsh_assignment_workspace_pending"):
            stage_native_handoff(snapshot,sealed,"oguri",session,"other-session",target,dsh_runtimes=runtimes)
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError,"dsh_assignment_workspace_pending"):
            rollback(snapshot,sealed,trial,rollback_dir,dsh_runtimes=runtimes)
        self.assertFalse(rollback_dir.exists())

        with tool_listener(runtime,loop.adapter,client), self.start_native(
                runtime,session,"cleanup-container-recovery-reopen") as resumed:
            deadline = time.monotonic()+8
            while json.loads(journal.read_bytes())["phase"]!="sealed" and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertEqual(json.loads(journal.read_bytes())["phase"],"sealed")
            replay = self.read_native(resumed,"read")
            self.assertEqual((replay["inputs"],replay["modelRequests"]),(run["inputs"],[]))
            running_audit, = audit_handoff(snapshot,sealed,dsh_runtimes=runtimes)["workers"]
            self.assertEqual(running_audit["issues"],["dsh_workspace_host_still_running"])
            with self.assertRaisesRegex(ValueError,"dsh_workspace_host_still_running"):
                stage_native_handoff(snapshot,sealed,"oguri",session,"other-session",target,dsh_runtimes=runtimes)
            self.assertFalse(target.exists())
            with self.assertRaisesRegex(ValueError,"dsh_workspace_host_still_running"):
                rollback(snapshot,sealed,trial,rollback_dir,dsh_runtimes=runtimes)
            self.assertFalse(rollback_dir.exists())
        self.assertFalse(subprocess.check_output([*cli,"container","ls","--all","--filter","id="+lease["container_id"],
            "--format","{{.ID}}"],timeout=5).strip())
        bad_record = journal.with_name("f"*64+".json")
        bad_record.symlink_to(journal)
        try:
            invalid, = audit_handoff(snapshot,sealed,dsh_runtimes=runtimes)["workers"]
            self.assertEqual(invalid["issues"],["dsh_workspace_journal_unconfirmed"])
        finally:
            bad_record.unlink()
        after, = audit_handoff(snapshot,sealed,dsh_runtimes=runtimes)["workers"]
        self.assertTrue(after["native_session_ledger_clear"],after)
        self.assertEqual(after["dsh_workspace_audit"]["pending"],[])
        handoff = stage_native_handoff(snapshot,sealed,"oguri",session,"other-session",target,dsh_runtimes=runtimes)
        self.assertEqual(handoff["dsh_workspace_journal_sha256"],after["dsh_workspace_audit"]["fingerprint"])
        self.assertEqual(handoff["expected_session"],"other-session")
        rolled_back = rollback(snapshot,sealed,trial,rollback_dir,dsh_runtimes=runtimes)
        self.assertEqual(set(rolled_back),set(sources))
        for label,path in rolled_back.items():
            self.assertEqual(Path(path).read_bytes(),(snapshot/filename(label)).read_bytes())
        with closing(sqlite3.connect(rolled_back["worker:oguri"])) as db:
            self.assertEqual(db.execute("SELECT session FROM native_binding WHERE slot=1").fetchone()[0],session)
        with closing(sqlite3.connect(target/filename("worker:oguri"))) as db:
            self.assertEqual(db.execute("SELECT session FROM native_binding WHERE slot=1").fetchone()[0],"other-session")
        self.assertEqual(client.request("POST","worker/workspace-state",scope),{**scope,"state":"released"})
        previous = None
        if previous_release := os.environ.get("MOWULINK_DSH_TEST_PREVIOUS_RELEASE"):
            previous = self.previous_dsh_after_rollback(Path(previous_release),loop.root,runtime,session,rolled_back,replay,job)
        if output := os.environ.get("MOWULINK_DSH_OFFLINE_AUDIT_EVIDENCE"):
            observations = {"session":session,"task_id":task,"assignment_id":aid,"container_id":lease["container_id"],
                "w04_state_before_capture":job["task"]["state"],"snapshot_format":3,"snapshot_sha256":sealed,
                "missing_journal":from_db["issues"][0],"wrong_runtime":wrong["issues"][0],
                "active_journal":before["dsh_workspace_audit"],"invalid_record":invalid["issues"][0],
                "running_host":running_audit["issues"][0],
                "sealed_journal":after["dsh_workspace_audit"],"staged_session":handoff["expected_session"],
                "receipt_pending_before_reaper":before["native_session_pending"]["report_receipts"],
                "held_before_reaper":before["native_session_pending"]["held_assignments"],
                "original_inputs":replay["inputs"],"new_model_requests_after_reopen":len(replay["modelRequests"]),
                "version_rollback":{"database_restore_allowed_with_live_workspace":True,
                    "refusals":[from_db["issues"][0],wrong["issues"][0],before["issues"][0],running_audit["issues"][0]],
                    "snapshot_sha256_after_drain":sealed,"components":sorted(rolled_back),"retained_session":session},
                **({"previous_release":previous} if previous else {}),
                "scope":"Installed DSH AgentLoop, isolated Linux Docker and Router/Worker HTTP; scripted local model."}
            Path(output).write_text(json.dumps(observations,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_native_dsh_stop_alone_or_unverified_tool_keeps_cleanup_pending(self):
        self.read_only_cleanup_refusals()

    def test_native_w04_stages_private_artifact_before_sharing_it_with_manager(self):
        session = "dsh-formal-artifact"
        directory,loop,service,_command = self.goal_worker(session)
        token = guid()
        client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
        self.enterContext(tool_listener(Path(directory),loop.adapter,client))
        raw = bytes(range(256))*2200
        digest = hashlib.sha256(raw).hexdigest()
        route = "worker/media/uploads"
        uploaded = client.request("POST",route,{"name":"sample.bin","sha256":digest,"size_bytes":len(raw)},guid())
        local = client.request("PATCH",route+"/"+uploaded["upload_id"],raw,headers={"Upload-Offset":"0"})["attachment_id"]
        transfer = loop.worker.router.request
        events = []
        with self.start_native(directory,session,"formal-artifacts") as native:
            tid = loop.hermes.create(guid(),"Native artifact test",local,"oguri")["task_id"]
            def fail_final_file_ack(method,path,*args,**kwargs):
                if path==f"tasks/{tid}/report":
                    artifact, = args[0]["artifact_ids"]
                    with self.assertRaisesRegex(Fault,"attachment_not_found"):
                        loop.manager_wire.request("GET","attachments/"+artifact)
                    self.assertEqual(transfer("GET","attachments/"+artifact)["bytes"],raw)
                    self.assertEqual(loop.worker.hold()["held_task_id"],tid)
                    events.append(("report",artifact))
                result = transfer(method,path,*args,**kwargs)
                if method=="PATCH" and path.startswith("uploads/"):
                    events.append(("chunk",result["next_offset"]))
                    if result["complete"]:
                        raise ConnectionError("isolated final file receipt lost")
                return result
            loop.worker.router.request = fail_final_file_ack
            loop.worker.connect()
            loop.worker.poll()
            service.clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            observed = self.read_native(native,"read")
        self.assertEqual([call["name"] for call in observed["calls"]],
                         ["worker_stage_artifacts","worker_stage_artifacts","worker_report"])
        self.assertEqual([result["isError"] for result in observed["results"]],[True,False,False])
        self.assertEqual(len(observed["inputs"]),1)
        _stage, staged, report = observed["results"]
        artifact, = json.loads(staged["content"][0]["text"])["artifact_ids"]
        self.assertEqual(events,[("chunk",256*1024),("chunk",512*1024),("chunk",len(raw)),("report",artifact)])
        details = loop.hermes.inspect(tid)
        self.assertEqual(details["last_report"]["artifact_ids"],[artifact])
        self.assertEqual(details["task"]["state"],"completed_reported")
        self.assertIsNone(loop.worker.hold()["held_assignment_id"])
        self.assertEqual(loop.manager_wire.request("GET","attachments/"+artifact)["bytes"],raw)
        notify, = loop.manager_wire.frames(0)
        self.assertEqual(notify["payload"]["attachment_ids"],[artifact])
        self.evidence = {"session":session,"native_input":observed["inputs"][0],"calls":observed["calls"],
                         "report":json.loads(report["content"][0]["text"]),"task_id":tid,"artifact_id":artifact,
                         "local_artifact_id":local,"sha256":digest,"bytes":len(raw),"transfer":events,
                         "manager_notification":notify["delivery_id"],
                         "boundary":"Actual DSH AgentLoop and isolated Router, scripted model, no Feishu"}

    def test_goal_query_pause_and_resume_use_the_bound_native_goal(self):
        session = "dsh-real-loop-goals"
        directory,loop,service,command = self.goal_worker(session)
        with self.start_native(directory,session,"goals") as native:
            service.step()
            self.assertIn("没有目标",command("om_no_goal","/goal 状态")[1])
            self.assertIn("没有目标",command("om_no_pause","/goal 暂停")[1])
            self.assertIsNone(loop.native.goal_view(session)["goal"])
            request = "/goal 隔离持久目标\n保留 第二行"
            created,_ = command("om_create_goal",request)
            original = loop.native.goal_view(session)["goal"]
            self.assertEqual(original["objective"],"隔离持久目标\n保留 第二行")
            self.assertEqual(created["goal_receipt"]["id"],original["id"])
            self.assertEqual(command("om_create_goal",request)[0],created)
            self.assertIn("已有未完成",command("om_replace_goal","/goal 不能替换")[1])
            self.assertEqual(loop.native.goal_view(session)["goal"],original)
            current = command("om_goal","/goal")[1]
            self.assertIn(f"进行中 · 0/{original['limit']} 轮",current)
            self.assertIn("隔离持久目标",current)
            paused,text = command("om_pause","/goal 暂停")
            self.assertIn("已暂停",text)
            self.assertEqual(paused["goal_receipt"]["id"],original["id"])
            self.assertEqual(paused["goal_receipt"]["revision"],original["revision"]+1)
            change = loop.adapter.goal_change
            def lost(*args):
                change(*args)
                raise ConnectionError("lost native goal receipt")
            loop.adapter.goal_change = lost
            pending,text = command("om_resume","/goal 继续")
            self.assertIn("目标操作待核",text)
            self.assertNotIn("goal_receipt",pending)
            loop.adapter.goal_change = change
            self.assertEqual(command("om_resume","/goal 继续")[0],pending)
            self.assertEqual(command("om_pause","/goal 暂停")[0],paused)
            self.assertIn("续跑授权 已启用",command("om_goal_now","/goal")[1])
            self.assertEqual(loop.native.goal_view(session)["goal"]["revision"],original["revision"]+2)
            observed = self.read_native(native,"read")
            self.assertEqual(observed["inputs"],[])
            self.assertEqual(loop.hermes.tasks()["items"],[])

    def test_native_goal_rounds_pause_and_resume_without_becoming_router_orders(self):
        session = "dsh-real-goal-rounds"
        directory,loop,service,command = self.goal_worker(session)
        with self.start_native(directory,session,"goal-driver") as native:
            service.step()
            creation,_ = command("om_create_live_goal","/goal 推进测试目标")
            created = creation["goal_receipt"]
            self.assertTrue(self.read_native(native,"first-round")["running"])
            paused,card = command("om_pause_live_goal","/goal pause")
            self.assertIn("已暂停 · 1/2 轮",card)
            before = self.read_native(native,"read")
            self.assertEqual([row["round"] for row in before["rounds"]],[1])
            self.assertEqual([row["reason"]["kind"] for row in before["turns"]],["aborted"])
            resumed,card = command("om_resume_live_goal","/goal resume")
            self.assertIn("进行中",card)
            after = self.read_native(native,"second-round")
            self.assertEqual([row["id"] for row in after["rounds"]],[created["id"]]*2)
            self.assertEqual([row["round"] for row in after["rounds"]],[1,2])
            self.assertEqual([row["revision"] for row in after["rounds"]],
                             [created["revision"],resumed["goal_receipt"]["revision"]])
            self.assertEqual([row["reason"]["kind"] for row in after["turns"]],["aborted","completed"])
            self.assertEqual(command("om_pause_live_goal","/goal pause")[0],paused)
            self.assertEqual(command("om_resume_live_goal","/goal resume")[0],resumed)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.assertIsNone(loop.worker.hold()["held_task_id"])
            self.assertEqual(loop.manager_wire.frames(0),[])
            self.evidence = {"created":created,"paused":paused["goal_receipt"],"resumed":resumed["goal_receipt"],
                             "rounds":after["rounds"],"turns":after["turns"],
                             "router_tasks":0,"router_notifications":0,"worker_hold":loop.worker.hold(),
                             "boundary":"Native DSH goal service and continuation driver; local scripted provider"}

    def test_compact_reconciles_a_lost_start_reply_against_the_native_session(self):
        session = "dsh-real-compact"
        directory,loop,service,command = self.goal_worker(session)
        with self.start_native(directory,session,"compact") as native:
            service.step()
            self.assertTrue(service.host_compaction)
            command("om_empty","/compact")
            original = loop.adapter.compact
            def find_result(message_id):
                with loop.worker.db.transaction() as db:
                    row = db.execute("SELECT * FROM platform_compactions WHERE message_id=?",(message_id,)).fetchone()
                    notices = db.execute("SELECT payload FROM display_events WHERE event_id=?",
                                         ("compact-result-"+message_id,)).fetchall()
                return dict(row),[json.loads(item[0])["text"] for item in notices]
            worker_commands.advance_compact(service)
            empty,_ = find_result("om_empty")
            self.assertEqual(json.loads(empty["native_receipt"]),{"state":"empty"})
            for index in range(3):
                loop.worker.chat(guid(),f"Fixture paragraph {index}. "+"Many words to compress. "*120)
                self.read_native(native,"read")
            attempts = []
            def lost(session,command_id,*,start):
                result = original(session,command_id,start=start)
                if start:
                    attempts.append(command_id)
                    raise ConnectionError("native compact started, reply lost")
                return result
            loop.adapter.compact = lost
            receipt,text = command("om_compact","/compact")
            self.assertIn("等待宿主",text)
            in_progress = self.read_native(native,"first-compact")
            self.assertTrue(in_progress["compacting"])
            in_progress["adapter_busy"] = loop.adapter.busy()
            self.assertTrue(in_progress["adapter_busy"])
            worker_commands.advance_compact(service)
            self.assertEqual(find_result("om_compact")[1],[])
            self.assertEqual(command("om_compact","/compact")[0],receipt)
            self.assertIn("仍待宿主确认",command("om_compact_again","/compact")[1])
            observed = self.read_native(native,"finish-compact")
            self.assertFalse(loop.adapter.busy())
            recovered_service = WorkerService(loop.worker,service.clock)
            recovered_service.step()
            service.clock.advance(5)
            recovered_service.step()
            row,notices = find_result("om_compact")
            self.assertEqual(len(notices),1)
            self.assertIn("宿主已压缩",notices[0])
            self.assertEqual(attempts,[row["command_id"]])
            proof = json.loads(row["native_receipt"])
            events = observed["compactEvents"]
            self.assertEqual([entry["kind"] for entry in events],
                             ["compaction/start","compaction/summary","user/message","compaction/end"])
            self.assertEqual({entry["sourceCommandId"] for entry in events},set(attempts))
            self.assertEqual({entry["compactionId"] for entry in events},{proof["compaction_id"]})
            self.assertEqual(events[-1]["seq"],proof["end_seq"])
            self.assertEqual([item["command"] for item in observed["modelRequests"]],["private"]*3+["compaction"])
            self.assertEqual(len(observed["inputs"]),3)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.evidence = {"session":session,"compaction":proof,"events":events,"result_count":len(notices),
                             "start_attempts":len(attempts),"native_in_progress":in_progress,
                             "native_model_requests":observed["modelRequests"],"router_tasks":0,
                             "boundary":"Native DSH basic compaction engine and persisted surface, scripted local provider"}

    def test_dsh_native_timed_question_closes_the_original_feishu_test_card(self):
        session = "dsh-real-timed-question"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-questions-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        service = WorkerService(loop.worker,clock)
        platform_key,adapter_key = guid(),guid()
        url = self.open_worker(service,{platform_key:"platform/oguri",adapter_key:"adapter/oguri"})
        platform = Client(url,platform_key)
        relay = DshDisplay(loop.adapter,Client(url,adapter_key),"oguri")
        inbound = Inbound(loop.root/"feishu.db",platform,None,"app","trainer","test-chat")
        sink = PlatformFixture()
        views = Views(loop.root/"views.db",platform,sink,"test-chat","小栗帽",clock)
        actions = Actions(views,inbound)
        completed = []
        writes = []
        with self.start_native(directory,session,"questions") as native:
            relay.collect()
            resolve = loop.native.resolve_question
            def lose(original,answer):
                proof = resolve(original,answer)
                writes.append(original["native_question_id"])
                if answer["mode"]=="choice":
                    raise Fault(503,"adapter_connection_unconfirmed",True)
                return proof
            loop.native.resolve_question = lose
            for mode,form,options in (
                ("choice",None,{"option_id":"2"}),("text",{"reply":"C"},{}),("skip",None,{})):
                loop.worker.chat(guid(),"Question fixture "+mode)
                opened = self.read_native(native,"read")
                call_id = "native-question-"+mode
                self.assertIn(call_id,opened["continued"])
                for _ in range(4):
                    relay.collect()
                    relay.flush()
                views.poll()
                with views.db.transaction() as db:
                    row = db.execute("""SELECT qid,mid,question FROM question_cards
                                      WHERE json_extract(question,'$.text')=?""",("Pick "+mode+"?",)).fetchone()
                self.assertIsNotNone(row)
                qid,mid,raw = row
                original = json.loads(raw)
                self.assertIsNone(loop.native.lookup_answer(session,"0/0",call_id))
                value = {"question_id":qid,"mode":mode,**options}
                with self.assertRaisesRegex(Fault,"wrong_card_actor"):
                    actions.record("app","other","test-chat",mid,value,form)
                with self.assertRaisesRegex(Fault,"not_original_trainer_card"):
                    actions.record("app","trainer","test-chat","om_other",value,form)
                self.assertEqual(actions.record("app","trainer","test-chat",mid,value,form),"pending")
                actions.flush()
                observed = self.read_native(native,"read")
                self.assertNotIn(call_id,observed["continued"])
                matching = [row for row in observed["replies"] if row["call"]==call_id]
                self.assertEqual(len(matching),1)
                found, = matching
                if mode=="choice":
                    self.assertEqual(actions.record("app","trainer","test-chat",mid,value,form),"pending")
                    clock.advance(16)
                    actions.flush()
                views.poll()
                self.assertEqual(sink.calls[-1],("patch",mid))
                self.assertEqual(actions.record("app","trainer","test-chat",mid,value,form),"confirmed")
                with loop.worker.db.transaction() as db:
                    proof = json.loads(db.execute("SELECT proof FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()[0])
                self.assertEqual((proof["receipt_id"],proof["turn_id"]),(found["id"],original["turn_id"]))
                completed.append({"platform_message":mid,"question_id":qid,"proof":proof,"native_reply":found})
            loop.worker.chat(guid(),"Question fixture multi")
            multi_call = "native-question-multi"
            self.assertIn(multi_call,self.read_native(native,"read")["continued"])
            for _ in range(4):
                relay.collect()
                relay.flush()
            views.poll()
            with views.db.transaction() as db:
                qid,mid,raw = db.execute("""SELECT qid,mid,question FROM question_cards
                    WHERE json_extract(question,'$.questions[0].text')='Pick multi?'""").fetchone()
            card = next(item for item in sink.messages.values() if item[0]==mid)
            self.assertEqual([field["id"] for field in json.loads(raw)["questions"]],["primary","note","optional"])
            self.assertIn("Follow the same route",json.dumps(card[2],ensure_ascii=False))
            value = {"question_id":qid,"mode":"batch"}
            form = {"options_1":"2，1","reply_1":"同时自填","reply_2":"  记住  "}
            with self.assertRaisesRegex(Fault,"invalid_option"):
                actions.record("app","trainer","test-chat",mid,value,dict(form,options_1="2,2"))
            self.assertEqual(actions.record("app","trainer","test-chat",mid,value,form),"pending")
            self.assertEqual(actions.record("app","trainer","test-chat",mid,value,dict(form,options_1="1,2")),"pending")
            actions.flush()
            observed = self.read_native(native,"read")
            self.assertNotIn(multi_call,observed["continued"])
            found, = [row for row in observed["replies"] if row["call"]==multi_call]
            self.assertEqual(found["body"]["answers"],[{"id":"primary","selected":["A","B"],"custom":"同时自填"},
                {"id":"note","selected":[],"custom":"  记住  "},{"id":"optional","selected":[]}])
            for _ in range(4):
                relay.collect()
                relay.flush()
            views.poll()
            self.assertEqual(actions.record("app","trainer","test-chat",mid,value,form),"confirmed")
            self.assertEqual(sink.calls.count(("patch",mid)),1)
            with loop.worker.db.transaction() as db:
                proof = json.loads(db.execute("SELECT proof FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()[0])
            self.assertEqual(proof["receipt_id"],found["id"])
            completed.append({"platform_message":mid,"question_id":qid,"proof":proof,"native_reply":found})
            loop.worker.chat(guid(),"Question fixture native")
            self.assertIn("native-question-native",self.read_native(native,"read")["continued"])
            original_request = relay.worker.request
            dropped = []
            def lose_open(*args):
                result = original_request(*args)
                if args[1]=="worker/native-question":
                    dropped.append(result["question_id"])
                    raise Fault(503,"adapter_connection_unconfirmed",True)
                return result
            relay.worker.request = lose_open
            with self.assertRaisesRegex(Fault,"adapter_connection_unconfirmed"):
                for _ in range(4):
                    relay.collect()
                    relay.flush()
            views.poll()
            with views.db.transaction() as db:
                qid,mid = db.execute("SELECT qid,mid FROM question_cards WHERE qid=?",(dropped[0],)).fetchone()
            pending = {"question_id":qid,"mode":"choice","option_id":"2"}
            self.assertEqual(actions.record("app","trainer","test-chat",mid,pending,None),"pending")
            won = self.read_native(native,"answer-native")
            self.assertNotIn("native-question-native",won["continued"])
            relay.worker.request = original_request
            for _ in range(4):
                relay.collect()
                relay.flush()
            actions.flush()
            views.poll()
            self.assertEqual(sink.calls[-1],("patch",mid))
            self.assertEqual(actions.record("app","trainer","test-chat",mid,pending,None),"rejected")
            with loop.worker.db.transaction() as db:
                row = db.execute("SELECT proof FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()
            native_proof = json.loads(row[0])
            self.assertEqual(native_proof["answer"],{"mode":"choice","option_id":"1"})
            self.assertEqual([item["id"] for item in won["replies"] if item["call"]=="native-question-native"],
                             [native_proof["receipt_id"]])
            self.assertEqual(writes,["native-question-choice","native-question-text","native-question-skip",multi_call])
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.evidence = {"session":session,"questions":completed,"native_answer_writes":writes,
                             "native_won_conflict":{"platform_message":mid,"question_id":qid,"proof":native_proof,
                                                    "late_platform_choice":"2","platform_state":"rejected"},
                             "router_tasks":0,
                             "boundary":"Native DSH timed question tool and reply admission; local scripted provider, Feishu fixture"}

    def test_native_foreground_question_remains_with_its_original_answer_provider(self):
        session = "dsh-real-foreground-question"
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-foreground-")))
        native = DshHost(directory)
        native.session = session
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:native))
        adapter_key,platform_key = guid(),guid()
        url = self.open_worker(WorkerService(loop.worker,clock),
                               {adapter_key:"adapter/oguri",platform_key:"platform/oguri"})
        relay = DshDisplay(loop.adapter,Client(url,adapter_key),"oguri")
        sink = PlatformFixture()
        views = Views(loop.root/"foreground-views.db",Client(url,platform_key),sink,"test-chat","小栗帽",clock)
        with self.start_native(directory,session,"foreground-question") as process:
            relay.collect()
            loop.worker.chat(guid(),"Question fixture foreground")
            waiting = self.read_native(process,"foreground-question")
            self.assertEqual((waiting["call_id"],waiting["status"],waiting["timed"],waiting["continued"]),
                             ("native-question-foreground","running",False,0))
            target = {"turn_id":waiting["turn_id"],"native_question_id":waiting["call_id"]}
            open_state = native.request("question-open",session,target)
            self.assertIsNone(open_state["question"])
            answer = native.request("question-answer",session,{**target,"answer":{"mode":"choice","option_id":"1"}})
            self.assertIsNone(answer["proof"])
            relay.collect()
            relay.flush()
            views.poll()
            with views.db.transaction() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM question_cards").fetchone()[0],0)
            finished = self.read_native(process,"answer-foreground")
            result, = finished["results"]
            observed = json.loads(result["content"][0]["text"])
            self.assertEqual(observed,{"answers":[{"id":"primary","selected":["B"]}]})
            self.assertEqual(result["toolCallId"],waiting["call_id"])
            self.assertFalse(finished["replies"])
            self.assertIsNone(native.lookup_answer(session,waiting["turn_id"],waiting["call_id"]))
            relay.collect()
            relay.flush()
            views.poll()
            with views.db.transaction() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM question_cards").fetchone()[0],0)
            self.assertFalse(loop.hermes.tasks()["items"])
            self.evidence = {"foreground":waiting,"adapter_open_result":open_state,
                "adapter_answer_result":answer,"original_provider_result":observed,"platform_question_cards":0,
                "boundary":"Installed SDK's blocking tool, in-process model and native answer-provider test fixtures"}

    def test_dsh_standalone_question_defers_answer_while_native_session_is_busy(self):
        session = "isolated-dsh-standalone-trainer"
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-standalone-")))
        host = DshHost(directory)
        host.session = session
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=lambda _path,_name:host))
        adapter_key,platform_key,worker_key = guid(),guid(),guid()
        url = self.open_worker(WorkerService(loop.worker,clock),
                               {adapter_key:"adapter/oguri",platform_key:"platform/oguri"})
        adapter,platform = Client(url,adapter_key),Client(url,platform_key)
        port = self.enterContext(codex_driver.running(serve_adapter(loop.adapter,"oguri",
                                 {worker_key:"worker/oguri"},("127.0.0.1",0),None)))
        remote = RemoteAdapter(Client(port,worker_key),session)
        loop.worker.adapter = remote
        self.enterContext(tool_listener(directory,loop.adapter,adapter))
        sink = PlatformFixture()
        views = Views(loop.root/"standalone-dsh-views.db",platform,sink,"test-chat","小栗帽",clock)
        actions = Actions(views,Inbound(loop.root/"standalone-dsh-inbound.db",platform,None,"app","trainer","test-chat"))
        args = {"request_id":guid(),"questions":[{"title":"Choose an isolated route","options":["North","South"]}]}
        with self.start_native(directory,session,"standalone-trainer-question") as native:
            source = loop.worker.chat(guid(),"Standalone trainer fixture\n"+encode(args))
            observed = self.read_native(native,"read")
            call, = observed["calls"]
            self.assertEqual((call["name"],json.loads(call["arguments"])),("worker_ask_trainer",args))
            opened = json.loads(observed["results"][0]["content"][0]["text"])
            qid = opened["question_id"]
            replay = {"session":session,"turn_id":source["turn_id"],"tool":call["name"],
                      "call_id":call["callId"],"arguments":args}
            self.assertEqual(adapter.request("POST","worker/native-tool",replay),opened)
            self.assertEqual(adapter.request("POST","worker/native-tool",dict(replay,call_id="retry-tool-call")),opened)
            with self.assertRaisesRegex(Fault,"native_question_conflict"):
                adapter.request("POST","worker/native-tool",dict(replay,arguments=dict(args,questions=[{"title":"Changed"}])))
            with self.assertRaisesRegex(Fault,"forbidden"):
                platform.request("POST","worker/native-tool",replay)
            self.assertFalse(loop.hermes.tasks()["items"])
            self.assertEqual(self.read_native(native,"hold-question-reply"),{"busy":True})
            views.poll()
            with views.db.transaction() as db:
                mid = db.execute("SELECT mid FROM question_cards WHERE qid=?",(qid,)).fetchone()[0]
            value = {"question_id":qid,"mode":"batch"}
            data = ("app","trainer","test-chat",mid,value,{"options_1":"1"})
            self.assertEqual(actions.record(*data),"pending")
            actions.flush()
            self.assertEqual(actions.record(*data),"pending")
            with loop.worker.db.transaction() as db:
                state = tuple(db.execute("SELECT started,proof FROM trainer_reply_deliveries JOIN trainer_questions "
                                         "USING(question_id) WHERE question_id=?",(qid,)).fetchone())
            self.assertEqual(state,(0,None))
            views.poll()
            self.assertEqual(sink.calls,[("send",mid)])
            after_stop = self.read_native(native,"stop-unrelated-turn")
            self.assertEqual(after_stop["standaloneAnswers"],[])
            clock.advance(16)
            actions.flush()
            finished = self.read_native(native,"read")
            clock.advance(16)
            actions.flush()
            views.poll()
            self.assertEqual(actions.record(*data),"confirmed")
            self.assertEqual(sink.calls,[("send",mid),("patch",mid)])
            payload, = finished["standaloneAnswers"]
            self.assertEqual((payload["question_id"],payload["source_turn_id"],payload["session"]),
                             (qid,source["turn_id"],session))
            self.assertEqual(payload["answer"],{"mode":"batch","answers":[{"id":"1","option_ids":["1"]}]})
            self.assertEqual(len(finished["inputs"]),len(after_stop["inputs"])+1)
            with loop.worker.db.transaction() as db:
                proof = json.loads(db.execute("SELECT proof FROM trainer_questions WHERE question_id=?",(qid,)).fetchone()[0])
            self.assertEqual(proof["receipt_id"],finished["inputs"][-1])
            with self.assertRaisesRegex(Fault,"wrong_native_session"):
                RemoteAdapter(remote.client,"other-bound-session").lookup_delivery(proof["delivery_id"],payload,
                                                                                    session="other-bound-session")
            direct = self.read_native(native,"direct-trainer "+encode({"questions":[{"title":"A direct native question"}]}))
            direct_open = json.loads(direct["results"][-1]["content"][0]["text"])
            self.assertEqual(direct_open["state"],"queued_for_trainer")
            self.assertNotEqual(direct_open["question_id"],qid)
            with self.assertRaisesRegex(Fault,"native_question_conflict"):
                adapter.request("POST","worker/native-tool",
                                dict(replay,turn_id=proof["answer_turn_id"],call_id="retry-in-a-new-turn"))
            self.assertFalse(loop.hermes.tasks()["items"])
            self.evidence = {"session":session,"native_tool_call":call["callId"],"public_question_id":qid,
                             "original_fixture_card":mid,"busy_attempt_started":state[0],"answer_proof":proof,
                             "direct_native_question_id":direct_open["question_id"],"answer_admissions":1,
                             "boundary":"Installed DSH SDK, private host IPC and isolated Worker HTTP, local model and Feishu fixture."}

    def test_foreground_question_uses_one_native_answer_and_one_original_card(self):
        self.foreground_questions(use_tls=False)

    def test_resume_browses_only_configured_native_cold_sessions_without_activating_them(self):
        session = "dsh-native-catalog-bound"
        directory,loop,service,command = self.goal_worker(session)
        with self.start_native(directory,session,"catalog") as native:
            allowed,foreign = guid(),guid()
            server = self.enterContext(serve_adapter(loop.adapter,"oguri",
                {allowed:"worker/oguri",foreign:"worker/other"},("127.0.0.1",0),None))
            thread = Thread(target=server.serve_forever,kwargs={"poll_interval":.02})
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
            url = "http://127.0.0.1:"+str(server.server_port)
            loop.worker.adapter = RemoteAdapter(Client(url,allowed))
            with self.assertRaisesRegex(Fault,"wrong_adapter_owner"):
                Client(url,foreign).request("GET","host/session-catalog")
            service.step()
            self.assertTrue(service.host_session_catalog)
            catalog = loop.worker.adapter.session_catalog()
            ids = {item["id"] for item in catalog["items"]}
            self.assertEqual(ids,{session,*("mowulink-test-catalog-"+str(i) for i in range(11))})
            self.assertEqual(catalog["unavailable"],1)
            page1,text = command("om_catalog","/resume")
            self.assertIn("第 1/2 页",text)
            _,text = command("om_page_next","/resume next")
            self.assertIn("第 2/2 页",text)
            self.assertIn("1 个会话的记录暂不可读",text)
            self.assertIn("第 1/2 页",command("om_prev","/resume prev")[1])
            self.assertIn("第 2/2 页",command("om_page_two","/resume page 2")[1])
            _,text = command("om_title_search","/resume search 目录中的会话 10")
            self.assertIn("mowulink-test-catalog-10",text)
            self.assertNotIn("mowulink-test-catalog-0",text)
            self.assertIn("当前绑定",command("om_binding","/resume search dsh-native-catalog-bound")[1])
            self.assertIn("没有符合",command("om_hidden","/resume search 仅宿主可见")[1])
            self.assertIn("敬请期待",command("om_no_select","/resume 1")[1])
            self.assertIn("敬请期待",command("om_no_foreign_select","/resume mowulink-test-catalog-10")[1])
            self.assertEqual(command("om_catalog","/resume")[0],page1)
            result = self.read_native(native,"read")
            self.assertEqual(result["liveSessions"],[session])
            self.assertEqual(result["inputs"],[])
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.evidence = {"session":session,"allowed_catalog_entries":len(ids),"unreadable":catalog["unavailable"],
                "excluded_unconfigured_session":True,"denied_other_worker_http":403,"live_sessions_after":result["liveSessions"],
                "native_input_count":len(result["inputs"]),"search_and_pagination_confirmed":True,
                "boundary":"Native DSH SessionQueryEngine and persisted session files; isolated Worker HTTP"}

    def test_stop_targets_only_current_private_turn_and_never_replays_a_lost_result(self):
        session = "dsh-native-private-stop"
        directory,loop,service,command = self.goal_worker(session)
        with self.start_native(directory,session,"private-stop") as native:
            service.step()
            self.assertIn("已空闲",command("om_idle","/stop")[1])
            first = loop.worker.chat(guid(),"Hold private one")
            self.assertTrue(self.read_native(native,"wait-private one")["running"])
            loop.worker.chat(guid(),"Keep queued input after abort")
            receipt,card = command("om_stop_first","/stop")
            self.assertIn("回合已停止",card)
            self.assertEqual(receipt["stop_receipt"]["turn_id"],first["turn_id"])
            stopped = self.read_native(native,"read")
            queued, = stopped["inbox"]
            self.assertEqual(len(stopped["inputs"]),1)
            continued = self.read_native(native,"wake-private-queue")
            self.assertEqual(continued["inputs"],[first["native_id"],queued,"fixture-next-private"])
            self.assertEqual(continued["inbox"],[])
            loop.worker.chat(guid(),"Hold private two")
            self.assertTrue(self.read_native(native,"wait-private two")["running"])
            original = loop.adapter.stop_private
            def lose(session):
                proof = original(session)
                self.assertEqual(proof["state"],"stopped")
                raise Fault(503,"adapter_connection_unconfirmed",True)
            loop.adapter.stop_private = lose
            pending,card = command("om_stop_second","/stop")
            self.assertIn("待核",card)
            self.assertNotIn("stop_receipt",pending)
            loop.adapter.stop_private = original
            loop.worker.chat(guid(),"Hold private three")
            self.assertTrue(self.read_native(native,"wait-private three")["running"])
            self.assertEqual(command("om_stop_first","/stop")[0],receipt)
            self.assertEqual(command("om_stop_second","/stop")[0],pending)
            self.assertTrue(loop.adapter.busy())
            self.assertIn("回合已停止",command("om_stop_third","/stop")[1])
            observed = self.read_native(native,"read")
            self.assertEqual([turn["reason"]["kind"] for turn in observed["turns"]],
                             ["aborted","completed","completed","aborted","aborted"])
            self.assertEqual(len(observed["inputs"]),5)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            tid = loop.hermes.create(guid(),"Protected dispatch","Wait for Router cancel","oguri")["task_id"]
            loop.worker.poll()
            service.clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            working = loop.hermes.inspect(tid)["task"]
            self.assertEqual(working["state"],"working")
            self.assertEqual(loop.worker.hold()["held_task_id"],tid)
            self.assertIn("取消请走工单流程",command("om_stop_formal","/stop")[1])
            with self.assertRaisesRegex(Fault,"native_stop_not_private"):
                loop.adapter.stop_private(session)
            self.assertTrue(loop.adapter.busy())
            self.assertEqual(loop.hermes.inspect(tid)["task"],working)
            loop.hermes.act(guid(),tid,working["revision"],"cancel",reason="End isolated formal case")
            loop.worker.poll()
            formal = self.read_native(native,"read")
            self.assertEqual(formal["turns"][-2]["reason"]["kind"],"aborted")
            self.assertEqual(loop.worker.hold()["held_task_id"],tid)
            self.evidence = {"session":session,"first_stop":receipt["stop_receipt"],
                "turns":observed["turns"],"input_count":len(observed["inputs"]),"preserved_native_queue_item":queued,
                "lost_reply_status":"unknown","replay_did_not_stop_later_turn":True,
                "formal_stop_rejected":tid,"formal_held_pending_cleanup":True,
                "boundary":"Native DSH AgentLoop and abort records; scripted local provider"}

    def test_native_formal_question_returns_to_its_assignment_and_keeps_ownership_until_completion(self):
        session = "dsh-native-w05"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-w05-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        credential,platform_key = guid(),guid()
        service = WorkerService(loop.worker,clock)
        url = self.open_worker(service,{credential:"adapter/oguri",platform_key:"platform/oguri"})
        self.enterContext(tool_listener(Path(directory),loop.adapter,Client(url,credential)))
        platform = Client(url,platform_key)
        with self.start_native(directory,session,"work-question") as native:
            tid = loop.hermes.create(guid(),"Formal native Q&A","Ask before continuing","oguri")["task_id"]
            loop.worker.connect()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            asked = self.read_native(native,"read")
            self.assertEqual([call["name"] for call in asked["calls"]],["worker_ask"],asked["results"])
            self.assertFalse(asked["results"][0]["isError"])
            question, = loop.hermes.questions(task_id=tid)["items"]
            qid,aid = question["question_id"],question["assignment_id"]
            self.assertEqual(json.loads(asked["results"][0]["content"][0]["text"])["question_id"],qid)
            self.assertEqual(loop.worker.hold(),{"held_task_id":tid,"held_assignment_id":aid})
            self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"working")
            self.assertIsNone(loop.hermes.inspect(tid)["last_report"])
            choice = {"mode":"choice","option_id":"go"}
            key = guid()
            answer = loop.hermes.answer(key,tid,qid,choice)
            self.assertEqual(loop.hermes.answer(key,tid,qid,choice),answer)
            dropped = []
            native_deliver = loop.native.deliver
            def miss(session,delivery_id,payload,digest):
                result = native_deliver(session,delivery_id,payload,digest)
                if payload["kind"]=="question_answer":
                    dropped.append((delivery_id,payload))
                    return None
                return result
            loop.native.deliver = miss
            loop.worker.poll()
            continued = self.read_native(native,"read")
            self.assertEqual(len(dropped),1)
            did,payload = dropped[0]
            self.assertEqual((payload["task_id"],payload["assignment_id"],payload["question_id"],payload["answer"]),
                             (tid,aid,qid,choice))
            self.assertEqual(loop.worker.hold(),{"held_task_id":tid,"held_assignment_id":aid})
            self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"working")
            continuation = json.loads(continued["results"][-1]["content"][0]["text"])
            self.assertEqual(continuation,{"recorded":True,"task_state":"working"},continued["results"])
            loop.worker.poll()
            self.assertEqual(self.read_native(native,"read")["inputs"],continued["inputs"])
            task = loop.hermes.inspect(tid)["task"]
            loop.hermes.update(guid(),tid,task["revision"],{"instructions":"Use the confirmed choice and report."})
            loop.worker.poll()
            finished = self.read_native(native,"read")
            detail = loop.hermes.inspect(tid)
            self.assertEqual(detail["task"]["state"],"completed_reported")
            self.assertEqual(len(finished["inputs"]),3)
            self.assertEqual([call["name"] for call in finished["calls"]],["worker_ask","worker_report","worker_report"])
            self.assertFalse(any(item["isError"] for item in finished["results"]))
            self.assertIsNone(loop.worker.hold()["held_assignment_id"])
            with loop.adapter.db.transaction() as db:
                input_receipt = json.loads(db.execute("SELECT receipt FROM intents WHERE delivery_id=?",(did,)).fetchone()[0])
            self.assertEqual(input_receipt["native_id"],finished["inputs"][1])
            items = platform.request("GET","worker/display")["items"]
            cards = [row["event"] for row in items if row["event"].get("question_id")==qid]
            self.assertEqual([(card["kind"],card["recipient"]) for card in cards],
                             [("question_open","manager"),("question_answered","manager")])
            notices = [frame["payload"] for frame in loop.manager_wire.frames(0) if frame["payload"].get("task_id")==tid]
            self.assertEqual([item["text"] for item in notices],["Use the first route?","Finished after the confirmed answer"])
            self.evidence = {"session":session,"task_id":tid,"assignment_id":aid,"question_id":qid,
                "question_call":asked["calls"][0],"answer":choice,"answer_receipt":input_receipt,
                "continue_receipt":continuation,"reports":[json.loads(item["content"][0]["text"]) for item in finished["results"]],
                "native_inputs":finished["inputs"],"manager_notice_count":len(notices),"router_after":detail["task"]["state"],
                "boundary":"Real DSH tool executor and Router/Worker HTTP; scripted local model and manager input"}

    def test_native_goal_finishing_releases_formal_work_only_after_its_router_probe_w04(self):
        session = "dsh-native-formal-goal"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-formal-goal-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        credential = guid()
        service = WorkerService(loop.worker,clock)
        url = self.open_worker(service,{credential:"adapter/oguri"})
        self.enterContext(tool_listener(Path(directory),loop.adapter,Client(url,credential)))
        with self.start_native(directory,session,"formal-goal") as native:
            tid = loop.hermes.create(guid(),"Native long work","Run a native goal and report when asked","oguri")["task_id"]
            loop.worker.connect()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            completed = self.read_native(native,"formal-goal-round")
            self.assertEqual([x["name"] for x in completed["calls"]],["create_goal","update_goal"],completed["results"])
            self.assertTrue(all(not row["isError"] for row in completed["results"]),completed["results"])
            goal = loop.native.goal_view(session)["goal"]
            self.assertEqual(goal["phase"],"complete")
            self.assertEqual([row["id"] for row in completed["rounds"]],[goal["id"]])
            aid = loop.worker.hold()["held_assignment_id"]
            self.assertTrue(aid)
            self.assertEqual(loop.worker.hold()["held_task_id"],tid)
            before = loop.hermes.inspect(tid)
            self.assertEqual(before["task"]["state"],"working")
            self.assertIsNone(before["last_report"])
            clock.advance(1)
            loop.worker.poll()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            after = self.read_native(native,"read")
            self.assertEqual([x["name"] for x in after["calls"]],["create_goal","update_goal","worker_report"])
            self.assertFalse(after["results"][-1]["isError"])
            result = json.loads(after["results"][-1]["content"][0]["text"])
            self.assertEqual(result,{"recorded":True,"task_state":"completed_reported","release_assignment_id":aid})
            self.assertEqual(len(after["inputs"]),2)
            with loop.adapter.db.transaction() as db:
                inputs = [(json.loads(p),json.loads(r)) for p,r in db.execute("SELECT payload,receipt FROM intents ORDER BY rowid")]
            self.assertEqual([item[0]["kind"] for item in inputs],["task","task_probe"])
            self.assertTrue(all(item[0]["assignment_id"]==aid for item in inputs))
            detail = loop.hermes.inspect(tid)
            self.assertEqual(detail["task"]["state"],"completed_reported")
            self.assertIsNone(loop.worker.hold()["held_assignment_id"])
            self.evidence = {"session":session,"task_id":tid,"assignment_id":aid,"native_goal":goal,
                "goal_rounds":completed["rounds"],"native_before_probe":before["task"]["state"],
                "probe_receipt":inputs[-1][1],"w04":result,"router_final":detail["task"]["state"],
                "boundary":"Native DSH goal tools, actual scheduled continuation and Router probes; scripted local model"}

    def test_actual_dsh_loop_gives_the_same_feishu_caption_and_original_image_to_its_provider(self):
        self.native_media()

    def test_dsh_peer_image_and_native_reply_travel_on_the_same_private_chain(self):
        session = "dsh-native-peer"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-peer-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host,extra_workers=["tama","foreign"]))
        loop.worker.clock = clock
        sender_host = FixtureHost(loop.root/"sender-host.db","test-sending-host")
        sender = Worker(loop.root/"sender.db","tama",loop.extra_clients["tama"],
                        HostAdapter(loop.root/"sender-adapter.db",sender_host,sender_host.session))
        sender_key,recipient_key = guid(),guid()
        sender_api = Client(self.open_worker(WorkerService(sender,clock),{sender_key:"adapter/tama"}),sender_key)
        receiver = Client(self.open_worker(WorkerService(loop.worker,clock),{recipient_key:"adapter/oguri"}),recipient_key)
        loop.adapter.media_store = NativeFiles(Path(directory)/"media",receiver)
        self.enterContext(tool_listener(Path(directory),loop.adapter,receiver))
        sha = hashlib.sha256(PNG).hexdigest()
        upload = sender_api.request("POST","worker/media/uploads",{"name":"peer.png","sha256":sha,"size_bytes":len(PNG)},guid())
        path = "worker/media/uploads/"+upload["upload_id"]
        uploaded = sender_api.request("PATCH",path,PNG,headers={"Upload-Offset":"0"})
        mid,caption = guid(),"请回这条原消息。原图随信。"
        body = {"message_id":mid,"to_worker":"oguri","text":caption,"attachment_ids":[uploaded["attachment_id"]],
                "attachment_kinds":["image"]}
        with self.start_native(directory,session,"peer-media") as native:
            sender.connect()
            loop.worker.connect()
            accepted = sender_api.request("POST","worker/peer",body)
            self.assertEqual(sender_api.request("POST","worker/peer",body),accepted)
            status = "peers/messages/"+mid
            self.assertFalse(sender.router.request("GET",status)["worker_received"])
            original_delivery = loop.native.deliver_media
            def drop_host_reply(*args):
                original_delivery(*args)
                return None
            loop.native.deliver_media = drop_host_reply
            loop.worker.poll()
            first = self.read_native(native,"read")
            pending = sender.router.request("GET",status)
            self.assertTrue(pending["worker_received"])
            self.assertIsNone(pending["host_receipt_id"])
            loop.native.deliver_media = original_delivery
            clock.advance(5)
            loop.worker.poll()
            observed = self.read_native(native,"read")
            self.assertEqual(len(first["inputs"]),1)
            self.assertEqual(observed["inputs"],first["inputs"])
            routed = sender.router.request("GET",status)
            self.assertTrue(routed["host_receipt_id"])
            image = observed["mediaRequests"][0]
            original = json.loads(image["caption"].split("\n")[1])
            self.assertEqual((original["message_id"],original["text"]),(mid,caption))
            self.assertEqual(image["images"],[{"sha256":sha,"size":len(PNG)}])
            history_call,listed,call,lookup = observed["calls"]
            self.assertEqual(history_call["name"],"worker_peer_inbox")
            self.assertEqual(listed["name"],"worker_list_peers")
            self.assertEqual(call["name"],"worker_send_peer")
            self.assertEqual(lookup["name"],"worker_peer_status")
            args = json.loads(call["arguments"])
            self.assertEqual((args["to_worker"],args["reply_to"]),("tama",mid))
            history,directory_result,result,native_status = (
                json.loads(entry["content"][0]["text"]) for entry in observed["results"])
            received, = history["items"]
            self.assertEqual((received["message_id"],received["peer_id"],received["direction"]),(mid,"tama","in"))
            self.assertEqual(set(received),{"message_id","direction","peer_id","reply_to"})
            self.assertEqual(directory_result,{"items":[{"worker_id":"tama","online":True}],"next_cursor":None})
            self.assertEqual(result["status"],"accepted")
            self.assertEqual((native_status["message_id"],native_status["status"]),(mid,"accepted"))
            self.assertTrue(native_status["worker_received"])
            self.assertIsNone(native_status["host_receipt_id"])
            with loop.adapter.db.transaction() as db:
                proof = json.loads(db.execute("SELECT receipt FROM intents WHERE delivery_id=?",
                                              (routed["delivery_id"],)).fetchone()[0])
            replay = {"session":session,"turn_id":proof["turn_id"],"call_id":call["callId"],
                      "tool":"worker_send_peer","arguments":args}
            self.assertEqual(receiver.request("POST","worker/native-tool",replay),result)
            with self.assertRaisesRegex(Fault,"peer_message_conflict"):
                receiver.request("POST","worker/native-tool",dict(replay,arguments=dict(args,text="changed body")))
            sender.poll()
            (reply,receipt), = sender_host.entries()
            self.assertEqual((reply["message_id"],reply["reply_to"],reply["text"]),(result["message_id"],mid,args["text"]))
            with self.assertRaisesRegex(Fault,"peer_message_not_found"):
                loop.extra_clients["foreign"].request("GET",status)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.assertIsNone(loop.worker.hold()["held_task_id"])
            self.evidence = {"letter":mid,"host_input":proof,"peer_reception":routed,"actual_tool_call":call["callId"],
                "inbox_call":history_call["callId"],"native_inbox":history,
                "directory_call":listed["callId"],"native_directory":directory_result,
                "native_status_call":lookup["callId"],"native_status_before_host_receipt":native_status,
                "reply":result,"reply_to":reply["reply_to"],"reply_recipient_receipt":receipt,
                "image":image["images"][0],"native_inputs":len(observed["inputs"]),"reply_inputs":len(sender_host.entries()),
                "clock":"controlled test clock",
                "boundary":"Actual DSH native tool executor and independent Router; scripted local model and sending host fixture"}

    def test_two_real_dsh_sessions_roundtrip_a_linked_peer_message(self):
        recipient_dir,loop,service,_ = self.goal_worker("dsh-peer-destination",extra_workers=["tama"])
        sender_dir = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-peer-src-"))
        source_session = "dsh-peer-source"
        target_session = loop.adapter.bound_session()
        sender_port = DshHost(Path(sender_dir))
        sender_adapter = HostAdapter(loop.root/"ds-source-adapter.db",sender_port,source_session)
        sender = Worker(loop.root/"dsh-peer-source.db","tama",loop.extra_clients["tama"],sender_adapter)
        a,b = guid(),guid()
        source = Client(self.open_worker(WorkerService(sender,service.clock),{a:"adapter/tama"}),a)
        target = Client(self.open_worker(service,{b:"adapter/oguri"}),b)
        loop.adapter.media_store = NativeFiles(Path(recipient_dir)/"media",target)
        self.enterContext(tool_listener(Path(sender_dir),sender_adapter,source))
        self.enterContext(tool_listener(Path(recipient_dir),loop.adapter,target))
        sha = hashlib.sha256(PNG).hexdigest()
        upload = source.request("POST","worker/media/uploads",{"name":"native-peer.png","sha256":sha,
                                "size_bytes":len(PNG)},guid())
        file = source.request("PATCH","worker/media/uploads/"+upload["upload_id"],PNG,headers={"Upload-Offset":"0"})
        args = {"to_worker":"oguri","text":"One isolated Agent to another","attachment_ids":[file["attachment_id"]],
                "attachment_kinds":["image"]}
        with (self.start_native(sender_dir,source_session,"peer-source") as sender_host,
              self.start_native(recipient_dir,target_session,"peer-media") as recipient_host):
            sender.connect()
            loop.worker.connect()
            prompt = sender.chat(guid(),"Send peer fixture\n"+encode(args))
            original = self.read_native(sender_host,"read")
            sent_call, = original["calls"]
            self.assertEqual((sent_call["name"],json.loads(sent_call["arguments"])),("worker_send_peer",args))
            sent = json.loads(original["results"][0]["content"][0]["text"])
            self.assertEqual(sent["status"],"accepted")
            loop.worker.poll()
            delivered = self.read_native(recipient_host,"read")
            loop.worker.poll()
            loop.worker.poll()
            self.assertEqual(self.read_native(recipient_host,"read")["inputs"],delivered["inputs"])
            self.assertEqual(len(delivered["inputs"]),1)
            self.assertEqual(delivered["mediaRequests"][0]["images"],[{"sha256":sha,"size":len(PNG)}])
            _,_,reply_call,_ = delivered["calls"]
            reply_args = json.loads(reply_call["arguments"])
            self.assertEqual((reply_call["name"],reply_args["reply_to"]),("worker_send_peer",sent["message_id"]))
            reply = json.loads(delivered["results"][2]["content"][0]["text"])
            sender.poll()
            answered = self.read_native(sender_host,"read")
            sender.poll()
            self.assertEqual(self.read_native(sender_host,"read")["inputs"],answered["inputs"])
            self.assertEqual(len(answered["inputs"]),2)
            self.assertEqual(answered["inputs"][0],prompt["native_id"])
            self.assertEqual([call["name"] for call in answered["calls"]],["worker_send_peer"])
            self.assertEqual([request["command"] for request in answered["modelRequests"]],["private","private","peer"])
            receipts = [sender.router.request("GET","peers/messages/"+mid) for mid in
                        (sent["message_id"],reply["message_id"])]
            self.assertTrue(all(receipt["host_receipt_id"] for receipt in receipts))
            with sender_adapter.db.transaction() as db:
                native_reply = json.loads(db.execute("SELECT payload FROM intents WHERE native_id=?",
                                                     (answered["inputs"][-1],)).fetchone()[0])
            self.assertEqual((native_reply["text"],native_reply["reply_to"]),(reply_args["text"],sent["message_id"]))
            self.assertFalse(loop.hermes.tasks()["items"])
            self.assertEqual((sender.hold()["held_task_id"],loop.worker.hold()["held_task_id"]),(None,None))
            self.evidence = {"source_session":source_session,"destination_session":target_session,
                "source_tool_call":sent_call["callId"],"destination_tool_call":reply_call["callId"],
                "destination_input":delivered["inputs"][0],"source_reply_input":answered["inputs"][-1],
                "reply_to":native_reply["reply_to"],"router_receipts":receipts,"image_sha256":sha,
                "formal_task_count":0,"clock":"controlled test clock",
                "boundary":"Two actual DSH AgentLoop processes with scripted local providers"}

    @unittest.skipUnless(codex_driver.CODEX.is_file(),"bundled Codex App Server required")
    def test_codex_and_dsh_roundtrip_native_peer_image_and_linked_reply(self):
        directory,loop,service,_ = self.goal_worker("dsh-cross-peer",extra_workers=["tama"])
        target_session,clock = loop.adapter.bound_session(),service.clock
        loop.worker.clock = clock
        profile = loop.root/"codex-peer-profile"
        profile.mkdir(mode=0o700)
        model = ThreadingHTTPServer(("127.0.0.1",0),codex_driver.Provider)
        model.observed,model.media = [],[]
        url = self.enterContext(codex_driver.running(model))
        rpc = self.enterContext(codex_driver.isolated_app_server(str(profile),url))
        tools = [tool for tool in codex_driver.TOOLSET if tool["name"]=="worker_send_peer"]
        sid = rpc.request("thread/start",{"cwd":str(profile),"ephemeral":False,"dynamicTools":tools})["thread"]["id"]
        codex = AppServerHost(rpc,sid)
        codex_adapter = HostAdapter(loop.root/"codex-peer-adapter.db",codex,sid)
        sender = Worker(loop.root/"codex-peer-worker.db","tama",loop.extra_clients["tama"],codex_adapter,clock=clock)
        a,b = guid(),guid()
        source = Client(self.open_worker(WorkerService(sender,clock),{a:"adapter/tama"}),a)
        target = Client(self.open_worker(service,{b:"adapter/oguri"}),b)
        loop.adapter.media_store = NativeFiles(Path(directory)/"media",target)
        self.enterContext(tool_listener(Path(directory),loop.adapter,target))
        settled = Event()
        self.enterContext(worker_callbacks(codex,source,settled.set))
        digest = hashlib.sha256(PNG).hexdigest()
        upload = source.request("POST","worker/media/uploads",{"name":"codex-to-dsh.png","sha256":digest,
                                "size_bytes":len(PNG)},guid())
        file = source.request("PATCH","worker/media/uploads/"+upload["upload_id"],PNG,headers={"Upload-Offset":"0"})
        args = {"to_worker":"oguri","text":"Cross-host original image","attachment_ids":[file["attachment_id"]],
                "attachment_kinds":["image"]}

        def wait_for_codex():
            until = time.monotonic()+10
            while not settled.wait(.02) and time.monotonic()<until:
                sender.poll()
            self.assertTrue(settled.is_set())
            settled.clear()

        with self.start_native(directory,target_session,"peer-media") as dsh:
            sender.connect()
            loop.worker.connect()
            mid = guid()
            prompt = "Send peer fixture\n"+encode(args)
            prompt_receipt = sender.chat(mid,prompt)
            until = time.monotonic()+4
            while prompt_receipt is None and time.monotonic()<until:
                prompt_receipt = sender.chat(mid,prompt)
                time.sleep(.02)
            self.assertIsNotNone(prompt_receipt)
            wait_for_codex()
            first, = codex.thread(sid,include_turns=True)["turns"]
            sent, = [item for item in first["items"] if item.get("type")=="dynamicToolCall"]
            self.assertEqual((sent["tool"],sent["arguments"],sent["success"]),("worker_send_peer",args,True))
            posted = json.loads(sent["contentItems"][0]["text"])
            loop.worker.poll()
            dsh_result = self.read_native(dsh,"read")
            loop.worker.poll()
            loop.worker.poll()
            self.assertEqual(self.read_native(dsh,"read")["inputs"],dsh_result["inputs"])
            self.assertEqual(len(dsh_result["inputs"]),1)
            self.assertEqual(dsh_result["mediaRequests"][0]["images"],[{"sha256":digest,"size":len(PNG)}])
            _,_,call,_ = dsh_result["calls"]
            self.assertEqual(json.loads(call["arguments"])["reply_to"],posted["message_id"])
            reply = json.loads(dsh_result["results"][2]["content"][0]["text"])
            sender.poll()
            wait_for_codex()
            clock.advance(5)
            sender.poll()
            loop.worker.poll()
            first,second = codex.thread(sid,include_turns=True)["turns"]
            received, = [item for item in second["items"] if item.get("type")=="userMessage"]
            inbound = json.loads(received["content"][0]["text"].split("\n",1)[1])["payload"]
            self.assertEqual((inbound["message_id"],inbound["reply_to"]),(reply["message_id"],posted["message_id"]))
            receipts = [sender.router.request("GET","peers/messages/"+value) for value in
                        (posted["message_id"],reply["message_id"])]
            self.assertTrue(all(receipt["host_receipt_id"] for receipt in receipts),receipts)
            self.assertFalse(loop.hermes.tasks()["items"])
            self.evidence = {"codex_session":sid,"dsh_session":target_session,"source_tool":sent["id"],
                             "dsh_reply_tool":call["callId"],"source_input":prompt_receipt["native_id"],
                             "dsh_input":dsh_result["inputs"][0],"codex_reply_input":received["id"],
                             "reply_to":inbound["reply_to"],"image_sha256":digest,"router_receipts":receipts,
                             "boundary":"Actual Codex App Server and DSH AgentLoop on one Mac with scripted providers"}

    @unittest.skipUnless(codex_driver.CODEX.is_file(),"bundled Codex App Server required")
    def test_platform_resume_switches_live_codex_and_dsh_sessions(self):
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock))
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="ml-resume-dsh-"))
        profile = loop.root/"resume-codex"
        profile.mkdir(mode=0o700)
        model = ThreadingHTTPServer(("127.0.0.1",0),codex_driver.Provider)
        model.observed,model.media = [],[]
        origin = self.enterContext(codex_driver.running(model))
        sid = "isolated-dsh-resume-target"
        rpc = self.enterContext(codex_driver.isolated_app_server(profile,origin))
        codex = rpc.request("thread/start",{"cwd":str(profile),"ephemeral":False,
                            "dynamicTools":[codex_driver.REPORT_TOOL]})["thread"]["id"]
        codex_host = AppServerHost(rpc,codex)
        native = self.enterContext(self.start_native(directory,sid,"formal"))
        dsh_adapter = HostAdapter(loop.root/"live-dsh.db",DshHost(Path(directory)),sid)
        adapters = {sid:dsh_adapter,codex:HostAdapter(loop.root/"live-codex.db",codex_host,codex)}
        ports = {}
        for session,adapter in adapters.items():
            token = guid()
            url = self.enterContext(codex_driver.running(serve_adapter(adapter,"oguri",
                    {token:"worker/oguri"},("127.0.0.1",0),None)))
            ports[session] = RemoteAdapter(Client(url,token),session)
        w = Worker(loop.root/"live-worker.db","oguri",loop.wire,ports[codex],clock=clock,resume_targets={sid:ports[sid]})
        service = WorkerService(w,clock)
        roles = {guid():role for role in ("platform/oguri","ops/oguri","adapter/oguri/"+codex,"adapter/oguri/"+sid)}
        port = self.open_worker(service,roles)
        clients = {role:Client(port,token) for token,role in roles.items()}
        service.step()
        dsh_callback,codex_callback = (clients["adapter/oguri/"+session] for session in (sid,codex))
        self.enterContext(tool_listener(Path(directory),dsh_adapter,dsh_callback))
        settled = Event()
        self.enterContext(worker_callbacks(codex_host,codex_callback,settled.set))
        platform = clients["platform/oguri"]
        platform_db = loop.root/"runtime-platform.db"
        inbound = Inbound(platform_db,platform,None,"app","trainer","chat",clock=clock)
        sink = PlatformFixture()
        views = Views(platform_db,platform,sink,"chat","小栗帽",clock)

        def record(mid,text,parent=None):
            return inbound.record("app","user","trainer",{"chat_type":"p2p","chat_id":"chat","message_id":mid,
                "message_type":"text","content":encode({"text":text}),"parent_id":parent})

        before,after = record("om_codex_before_live_resume","An original private message"),"om_live_dsh_after_resume"
        try:
            inbound.deliver(before)
        except Fault as error:
            self.assertEqual(error.code,"host_receipt_unconfirmed")
        self.assertTrue(settled.wait(12))
        first = inbound.deliver(before)
        original_turns = codex_host.thread(codex,include_turns=True)["turns"]
        self.assertEqual(len(original_turns),1)
        menu = inbound.deliver(record("om_live_resume_catalog","/resume"))
        views.poll()
        with views.db.transaction() as db:
            catalog = db.execute("SELECT mid,event FROM view_attempt WHERE seq=?",(menu["output"]["seq"],)).fetchone()
        catalog_id = catalog["mid"]
        lines = json.loads(catalog["event"])["text"].splitlines()
        index = lines[lines.index(sid)-1].partition(".")[0]
        self.assertTrue(index.isdecimal())
        cmd = record("om_live_native_resume","/resume "+index,catalog_id)
        selected = inbound.deliver(cmd)
        self.assertEqual(selected["binding_receipt"]["session"],sid)
        with self.assertRaisesRegex(Fault,"native_callback_inactive"):
            codex_callback.request("POST","worker/display",{"event_id":guid(),"kind":"final","session":sid,
                                    "turn_id":first["turn_id"],"text":"Late source callback"})
        self.assertEqual(inbound.deliver(before),first)
        self.assertEqual(self.read_native(native,"read")["inputs"],[])
        record(after,"A new private message after resume")
        try:
            inbound.deliver(after)
        except Fault as error:
            self.assertEqual(error.code,"host_receipt_unconfirmed")
        self.read_native(native,"read")
        second = inbound.deliver(after)
        self.assertEqual(second["session"],sid)
        tid = loop.hermes.create(guid(),"Runtime resume","Report in the target native session","oguri")["task_id"]
        service.step()
        clock.advance(10)
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            service.step()
            order = loop.hermes.inspect(tid)
            if order["task"]["state"]=="completed_reported":
                break
            time.sleep(.025)
        self.assertEqual(order["task"]["state"],"completed_reported")
        observed = self.read_native(native,"read")
        aid = order["last_report"]["assignment_id"]
        expected = {"recorded":True,"task_state":"completed_reported","release_assignment_id":aid}
        self.assertEqual([json.loads(result["content"][0]["text"]) for result in observed["results"]],[expected,expected])
        self.assertEqual(observed["inputs"][0],second["native_id"])
        self.assertEqual(len(observed["inputs"]),2)
        self.assertEqual(w.assignment_session(aid),sid)
        self.assertIsNone(w.hold()["held_assignment_id"])
        clock.advance(5)
        service.step()
        back = record("om_live_native_return","/switch "+codex)
        returned = inbound.deliver(back)
        self.assertEqual(returned["binding_receipt"]["session"],codex)
        self.assertEqual(inbound.deliver(cmd),selected)
        self.assertEqual(inbound.deliver(after),second)
        self.assertEqual(codex_host.thread(codex,include_turns=True)["turns"],original_turns)
        self.assertEqual(self.read_native(native,"read")["inputs"],observed["inputs"])
        self.assertEqual(loop.hermes.inspect(tid)["last_report"],order["last_report"])
        notice, = loop.manager_wire.frames(0)
        self.assertEqual(notice["payload"]["task_id"],tid)
        views.poll()
        self.assertEqual(views.poll(),0)
        with views.db.transaction() as db:
            cards = dict(db.execute("""SELECT json_extract(event,'$.event_id'),mid FROM view_attempt
                WHERE json_extract(event,'$.event_id') IN (?,?)""",("command-"+cmd,"command-"+back)))
        self.assertEqual(len(cards),2)
        self.assertTrue(all(sink.calls.count(("send",mid))==1 for mid in cards.values()))
        self.evidence = {"switch":selected["binding_receipt"],"return":returned["binding_receipt"],"cards":cards,
            "catalog_card":catalog_id,"selected_number":int(index),
            "codex_input":first,"dsh_input":second,"dsh_work_input":observed["inputs"][-1],"task_id":tid,
            "assignment_id":aid,"w04_receipt":expected,"notification_id":notice["delivery_id"],
            "codex_turns_after_replay":len(original_turns),"dsh_inputs_after_replay":len(observed["inputs"]),
            "w04_result_receipts":len(observed["results"]),"new_result_notifications":1,
            "late_callback":"native_callback_inactive",
            "boundary":"Live independent Codex App Server and DSH SDK; local test models and Feishu fixture"}

    @unittest.skipUnless(codex_driver.CODEX.is_file(),"bundled Codex App Server required")
    def test_one_worker_cold_switches_between_native_engines_and_keeps_both_reports(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="ml-host-handoff-")))
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="ml-dsh-handoff-"))
        profile = root/"codex"
        profile.mkdir(mode=0o700)
        dsh_session = "isolated-dsh-worker-handoff"
        model = ThreadingHTTPServer(("127.0.0.1",0),codex_driver.Provider)
        model.observed,model.media = [],[]
        origin = self.enterContext(codex_driver.running(model))
        labels = ("router:main","worker:oguri","agent:oguri","hermes:main","trigger:source","feishu:oguri")
        clock = Clock()
        manager_native = FixtureHost(root/"manager.db","fixture-manager")
        manager_host = HostAdapter(root/"manager-adapter.db",manager_native,manager_native.session)
        sink = PlatformFixture()
        downloaded = []
        sha = hashlib.sha256(PNG).hexdigest()
        caption = "  同一张图，换工位宿主以后仍可收到。\n第二行"
        picture = {"zh_cn":{"content":[[{"tag":"text","text":caption},{"tag":"img","image_key":"img_fixture"}]]}}
        old_mid,new_mid = "om_before_switch","om_after_switch"

        def files(directory):
            return {label:directory/filename(label) for label in labels}

        def platform(paths,client):
            def resource(mid,key,kind,_limit):
                self.assertEqual((key,kind),("img_fixture","image"))
                downloaded.append(mid)
                return PNG
            path = paths["feishu:oguri"]
            return Inbound(path,client,resource,"fixture-app","trainer","test-chat",clock=clock),Views(
                path,client,sink,"test-chat","小栗帽",clock)

        def receive(inbound,mid):
            self.assertEqual(inbound.record("fixture-app","user","trainer",{"chat_type":"p2p","chat_id":"test-chat",
                "message_id":mid,"message_type":"post","content":encode(picture)}),mid)
            try:
                return inbound.deliver(mid)
            except Fault as error:
                if error.code!="host_receipt_unconfirmed":
                    raise
                return None

        def finish_report(manager,worker):
            tid = manager.create(guid(),"Native engine handoff","Submit the isolated result","oguri")["task_id"]
            worker.poll()
            clock.advance(10)
            deadline = time.monotonic()+12
            while time.monotonic()<deadline:
                worker.poll()
                record = manager.inspect(tid)
                if record["task"]["state"]=="completed_reported" and not worker.hold()["held_assignment_id"]:
                    return record
                time.sleep(.025)
            self.fail("isolated W04 did not arrive")

        def report_receipt(record):
            return {"recorded":True,"task_state":"completed_reported",
                    "release_assignment_id":record["last_report"]["assignment_id"]}

        def formal_input(adapter,worker,record):
            tid,aid = record["task"]["task_id"],record["last_report"]["assignment_id"]
            with adapter.db.transaction() as db:
                row, = db.execute("""SELECT receipt FROM intents WHERE json_extract(payload,'$.kind')='task'
                    AND json_extract(payload,'$.task_id')=? AND json_extract(payload,'$.assignment_id')=?""",
                    (tid,aid)).fetchall()
            receipt = json.loads(row[0])
            self.assertEqual(worker.assignment_session(aid),receipt["session"])
            return receipt

        def reply_card(views,receipt):
            with views.db.transaction() as db:
                row, = db.execute("""SELECT mid FROM view_attempt WHERE json_extract(event,'$.kind')='final'
                    AND json_extract(event,'$.session')=? AND json_extract(event,'$.turn_id')=?""",
                    (receipt["session"],receipt["turn_id"])).fetchall()
            mid, = row
            body, = [body for actual,kind,body in sink.messages.values() if actual==mid and kind=="interactive"]
            self.assertEqual(body["header"]["title"]["content"],"小栗帽-回复")
            self.assertEqual(sink.calls.count(("send",mid)),1)
            return mid

        original = files(root)
        with codex_driver.isolated_app_server(profile,origin) as rpc:
            codex = rpc.request("thread/start",{"cwd":str(profile),"ephemeral":False,
                                "dynamicTools":[codex_driver.REPORT_TOOL]})["thread"]["id"]
            rpc.request("turn/start",{"threadId":codex,"input":[{"type":"text","text":"Seed isolated Codex before attachment",
                                                               "text_elements":[]}]})
            deadline = time.monotonic()+10
            while rpc.event(deadline)["method"]!="turn/completed":
                pass
            host = AppServerHost(rpc,codex)
            with codex_driver.handoff_station(original,clock,host,codex,manager_host) as station:
                worker,manager,adapter,platform_client,callback = station
                adapter.media_store = NativeFiles(root/"codex-media",callback)
                inbound,views = platform(original,platform_client)
                relay = CodexDisplay(adapter,callback)
                relay.collect()
                finished = Event()
                with worker_callbacks(host,callback,finished.set,relay.observe,relay.observe_terminal):
                    receive(inbound,old_mid)
                    self.assertTrue(finished.wait(12))
                    old = inbound.deliver(old_mid)
                    self.assertEqual(old["media_sha256"],[sha])
                    relay.collect()
                    relay.flush()
                    views.poll()
                    old_card = reply_card(views,old)
                    finished.clear()
                    codex_order = finish_report(manager,worker)
                    self.assertTrue(finished.wait(12))
                    seed,first,second = host.thread(codex,include_turns=True)["turns"]
                    self.assertEqual(first["id"],old["turn_id"])
                    codex_calls = [item for item in second["items"] if item["type"]=="dynamicToolCall"]
                    self.assertEqual([json.loads(c["contentItems"][0]["text"]) for c in codex_calls],
                                     [report_receipt(codex_order)]*2)
                    codex_input = formal_input(adapter,worker,codex_order)
                    self.assertEqual(codex_input["turn_id"],second["id"])
                    self.assertEqual([item["id"] for item in second["items"] if item["type"]=="userMessage"],
                                     [codex_input["native_id"]])
                    relay.collect()
                    relay.flush()
                    views.poll()
                    expected_session = worker.session
                    before_frames = manager.router.frames(0)
                    self.assertEqual(len(before_frames),1)
        snapshot = root/"initial-snapshot"
        original_sha = capture(original,"a"*40,snapshot)["manifest_sha256"]
        trial = root/"dsh-state"
        plan = codex_driver.stage_handoff_cli(snapshot,original_sha,codex,dsh_session,trial)
        trial_paths = files(trial)
        trial_paths["agent:oguri"] = Path(plan["target_adapter_db"])

        with self.start_native(directory,dsh_session,"handoff") as process:
            host = DshHost(Path(directory))
            with codex_driver.handoff_station(trial_paths,clock,host,dsh_session,manager_host) as station:
                worker,manager,adapter,platform_client,callback = station
                self.assertEqual(worker.session,expected_session)
                adapter.media_store = NativeFiles(Path(directory)/"media",callback)
                inbound,views = platform(trial_paths,platform_client)
                relay = DshDisplay(adapter,callback,"oguri")
                relay.collect()
                with tool_listener(Path(directory),adapter,callback):
                    self.assertEqual(receive(inbound,old_mid),old)
                    self.assertEqual(self.read_native(process,"read")["inputs"],[])
                    receive(inbound,new_mid)
                    observed = self.read_native(process,"read")
                    new = inbound.deliver(new_mid)
                    self.assertEqual(observed["inputs"],[new["native_id"]])
                    self.assertEqual(new["media_sha256"],[sha])
                    self.assertEqual(observed["mediaRequests"],[
                        {"caption":caption,"images":[{"sha256":sha,"size":len(PNG)}],"partTypes":["text","image"]}])
                    relay.collect()
                    relay.flush()
                    views.poll()
                    dsh_card = reply_card(views,new)
                    dsh_order = finish_report(manager,worker)
                    dsh_input = formal_input(adapter,worker,dsh_order)
                    observed = self.read_native(process,"read")
                    self.assertEqual([json.loads(item["content"][0]["text"]) for item in observed["results"]],
                                     [report_receipt(dsh_order)]*2)
                    for _ in range(4):
                        relay.collect()
                        relay.flush()
                    views.poll()
                    self.assertEqual(receive(inbound,new_mid),new)
                    self.assertEqual(self.read_native(process,"read")["inputs"],observed["inputs"])
                    frame_old,frame_new = manager.router.frames(0)
                    self.assertEqual([frame_old],before_frames)
                    self.assertEqual(frame_new["payload"]["task_id"],dsh_order["task"]["task_id"])
                    self.assertEqual(observed["inputs"],[new["native_id"],dsh_input["native_id"]])

        snapshot_dsh = root/"dsh-snapshot"
        dsh_sha = capture(trial_paths,"a"*40,snapshot_dsh)["manifest_sha256"]
        returned = root/"return-state"
        codex_driver.stage_handoff_cli(snapshot_dsh,dsh_sha,dsh_session,codex,returned)
        paths = files(returned)
        paths["agent:oguri"] = Path(plan["previous_adapter_db"])
        model_before = len(model.observed)
        with codex_driver.isolated_app_server(profile,origin) as rpc:
            rpc.request("thread/resume",{"threadId":codex,"cwd":str(profile)})
            host = AppServerHost(rpc,codex)
            with codex_driver.handoff_station(paths,clock,host,codex,manager_host) as station:
                worker,manager,adapter,platform_client,callback = station
                self.assertEqual(worker.session,expected_session)
                inbound,views = platform(paths,platform_client)
                self.assertEqual(receive(inbound,old_mid),old)
                self.assertEqual(receive(inbound,new_mid),new)
                relay = CodexDisplay(adapter,callback)
                relay.collect()
                relay.flush()
                views.poll()
                self.assertEqual(manager.router.frames(0),[frame_old,frame_new])
                for order in (codex_order,dsh_order):
                    self.assertEqual(manager.inspect(order["task"]["task_id"])["last_report"],order["last_report"])
                self.assertEqual(host.thread(codex,include_turns=True)["turns"],[seed,first,second])
                self.assertEqual(reply_card(views,new),dsh_card)
                self.assertEqual(reply_card(views,old),old_card)
        self.assertEqual(len(model.observed),model_before)
        self.assertEqual(model.media,[{"caption":[caption],"image_sha256":[sha]}])
        self.assertEqual(downloaded,[old_mid,new_mid])
        verify(snapshot,original_sha)
        verify(snapshot_dsh,dsh_sha)
        self.evidence = {"before":old,"after":new,"image_sha256":sha,"input_resources":downloaded,
            "reply_cards":[old_card,dsh_card],"native_dsh_inputs":observed["inputs"],
            "reports":[{"task_id":record["task"]["task_id"],**report_receipt(record),
                        "notification_id":frame["delivery_id"],"host_receipt":receipt} for record,frame,receipt in
                        ((codex_order,frame_old,codex_input),(dsh_order,frame_new,dsh_input))],
            "new_model_calls_after_return":len(model.observed)-model_before,
            "boundary":"Actual bundled Codex App Server and DSH SDK on one Mac. Scripted models and Feishu test sink."}

    def test_shelved_dsh_assignment_submits_candidate_without_changing_ownership(self):
        directory,loop,service,_ = self.goal_worker("dsh-native-material",extra_workers=["tama"])
        session,clock = loop.adapter.bound_session(),service.clock
        token = guid()
        client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
        self.enterContext(tool_listener(Path(directory),loop.adapter,client))
        raw = b"evidence from native shelved assignment\n" * 16000
        digest = hashlib.sha256(raw).hexdigest()
        upload = client.request("POST","worker/media/uploads",
                                {"name":"candidate.txt","sha256":digest,"size_bytes":len(raw)},guid())
        source = client.request("PATCH","worker/media/uploads/"+upload["upload_id"],raw,
                                headers={"Upload-Offset":"0"})["attachment_id"]
        next_host = FixtureHost(loop.root/"new-holder-native.db","new-holder-session")
        next_worker = Worker(loop.root/"new-holder.db","tama",loop.extra_clients["tama"],
                             HostAdapter(loop.root/"new-holder-adapter.db",next_host,next_host.session))
        next_worker.connect()
        next_worker.poll()
        with self.start_native(directory,session,"material") as native:
            tid = loop.hermes.create(guid(),"Candidate",source,"oguri")["task_id"]
            router_request = loop.worker.router.request
            checked_scope = []

            def confirm_material_scope(method,path,*args,**kwargs):
                if method=="POST" and path==f"tasks/{tid}/materials" and not checked_scope:
                    artifact, = args[0]["artifact_ids"]
                    with self.assertRaisesRegex(Fault,"attachment_not_found"):
                        loop.manager_wire.request("GET","attachments/"+artifact)
                    self.assertEqual(router_request("GET","attachments/"+artifact)["bytes"],raw)
                    self.assertIsNone(loop.worker.hold()["held_assignment_id"])
                    checked_scope.append(artifact)
                return router_request(method,path,*args,**kwargs)

            loop.worker.router.request = confirm_material_scope
            loop.worker.connect()
            loop.worker.poll()
            clock.advance(10)
            loop.worker.poll()
            loop.worker.poll()
            initial = self.read_native(native,"read")
            aid = loop.worker.hold()["held_assignment_id"]
            self.assertEqual(len(initial["inputs"]),1)
            for seconds in (1,10,1,1,1,1):
                clock.advance(seconds)
                loop.worker.poll()
                loop.worker.poll()
                result = self.read_native(native,"read")
            stage,call = result["calls"]
            self.assertEqual([stage["name"],call["name"]],["worker_stage_artifacts","worker_submit_material"])
            args = json.loads(call["arguments"])
            self.assertEqual((args["task_id"],args["assignment_id"]),(tid,aid))
            self.assertEqual(set(args),{"task_id","assignment_id","artifact_ids"})
            self.assertEqual(args["artifact_ids"],checked_scope)
            artifact, = checked_scope
            self.assertEqual([item["isError"] for item in result["results"]],[False,False])
            material = json.loads(result["results"][1]["content"][0]["text"])
            self.assertEqual(set(material),{"received","material_id"})
            self.assertTrue(material["received"])
            details = loop.hermes.inspect(tid)
            self.assertEqual((details["task"]["state"],details["task"]["holder"]),("blocked",{"kind":"router"}))
            self.assertIsNone(details["last_report"])
            self.assertEqual([item["material_id"] for item in details["materials"]],[material["material_id"]])
            self.assertEqual(details["materials"][0]["artifact_ids"],[artifact])
            self.assertEqual(loop.manager_wire.request("GET","attachments/"+artifact)["bytes"],raw)
            with self.assertRaisesRegex(Fault,"attachment_not_found"):
                loop.extra_clients["tama"].request("GET","attachments/"+artifact)
            self.assertIsNone(loop.worker.hold()["held_assignment_id"])
            with self.assertRaisesRegex(Fault,"stale_assignment") as stale:
                loop.wire.request("POST",f"tasks/{tid}/report",{"session_id":loop.worker.session,
                                  "assignment_id":aid,"outcome":"completed","summary":"Late W04"},guid())
            self.assertEqual(stale.exception.status,409)
            with loop.adapter.db.transaction() as db:
                proof = json.loads(db.execute("""SELECT receipt FROM intents
                    WHERE json_extract(payload,'$.kind')='task.shelved'""").fetchone()[0])
            request = {"session":session,"turn_id":proof["turn_id"],"call_id":call["callId"],
                       "tool":"worker_submit_material","arguments":args}
            self.assertEqual(client.request("POST","worker/native-tool",request),material)
            with self.assertRaisesRegex(Fault,"idempotency_conflict"):
                client.request("POST","worker/native-tool",dict(request,arguments=dict(args,text="changed")))
            loop.hermes.act(guid(),tid,details["task"]["revision"],"transfer",worker_id="tama",reason="Assign again")
            next_worker.poll()
            next_worker.poll()
            new_aid = next_worker.hold()["held_assignment_id"]
            self.assertTrue(new_aid and new_aid!=aid)
            self.assertEqual(len(next_host.entries()),1)
            self.assertEqual(client.request("POST","worker/native-tool",request),material)
            with self.assertRaisesRegex(Fault,"ineligible_material"):
                client.request("POST","worker/native-tool",dict(request,call_id="new-candidate-attempt"))
            final = loop.hermes.inspect(tid)
            self.assertEqual(final["task"]["assignment_id"],new_aid)
            self.assertEqual(final["task"]["holder"],{"kind":"worker","worker_id":"tama"})
            self.assertEqual([row["assignment_id"] for row in final["pending_cleanup"]],[aid])
            self.assertEqual(len(final["materials"]),1)
            self.assertIsNone(final["last_report"])
            self.assertFalse([frame for frame in loop.manager_wire.frames(0)
                              if artifact in frame["payload"].get("attachment_ids",[])])
            self.evidence = {"session":session,"task_id":tid,"old_assignment_id":aid,"shelved_receipt":proof,
                "native_tool":call["name"],"native_call_id":call["callId"],"first_receipt":material,
                "stage_tool_call_id":stage["callId"],"local_artifact":source,"router_artifact":artifact,
                "artifact_sha256":digest,"artifact_bytes":len(raw),"manager_read_only_after_submission":True,
                "first_w04":stale.exception.status,"before_new_assignment":details["task"]["holder"],"new_assignment_id":new_aid,
                "new_holder":final["task"]["holder"],"pending_cleanup":aid,
                "final_material_count":len(final["materials"]),"final_report":final["last_report"],
                "boundary":"Actual DSH native tool and Router, scripted local model and test clock"}

    def test_native_race_after_w03_never_prequeues_a_formal_task(self):
        facts = []
        for cancelled in (False,True):
            with self.subTest(cancelled=cancelled):
                session = "dsh-admission-race-"+str(cancelled).lower()
                directory,loop,service,_ = self.goal_worker(session)
                with self.start_native(directory,session,"admission-race") as native:
                    tid = loop.hermes.create(guid(),"Check admission race","Only admit when idle","oguri")["task_id"]
                    original = loop.worker.router.request
                    during_accept = []
                    def accept(method,path,*args,**kwargs):
                        response = original(method,path,*args,**kwargs)
                        if path==f"tasks/{tid}/accept":
                            private = {"kind":"chat","text":"Hold private one","message_id":guid()}
                            during_accept.append(loop.adapter.deliver(guid(),private))
                            self.assertTrue(self.read_native(native,"wait-private one")["running"])
                        return response
                    loop.worker.router.request = accept
                    loop.worker.connect()
                    loop.worker.poll()
                    service.clock.advance(10)
                    loop.worker.poll()
                    loop.worker.poll()
                    self.assertEqual(len(during_accept),1)
                    self.assertEqual(loop.hermes.inspect(tid)["task"]["state"],"working")
                    self.assertEqual(loop.worker.hold()["held_task_id"],tid)
                    self.assertEqual(self.read_native(native,"wait-private one")["inbox"],[])
                    with loop.adapter.db.transaction() as db:
                        count = db.execute("SELECT COUNT(*) FROM intents WHERE json_extract(payload,'$.kind')='task'").fetchone()[0]
                    self.assertEqual(count,0)
                    if cancelled:
                        task = loop.hermes.inspect(tid)["task"]
                        loop.hermes.act(guid(),tid,task["revision"],"cancel",reason="Cancel before native admission")
                        loop.worker.poll()
                        # The cancellation must preserve the unrelated user's live turn.
                        self.assertTrue(self.read_native(native,"wait-private one")["running"])
                    self.read_native(native,"end-race-private")
                    loop.worker.poll()
                    observed = self.read_native(native,"read")
                    loop.worker.poll()
                    self.assertEqual(self.read_native(native,"read")["inputs"],observed["inputs"])
                    with loop.adapter.db.transaction() as db:
                        entries = [json.loads(row[0])["kind"] for row in db.execute("SELECT payload FROM intents ORDER BY rowid")]
                    self.assertEqual(entries,["chat","task.cancel" if cancelled else "task"])
                    self.assertEqual(len(observed["inputs"]),2)
                    facts.append({"session":session,"task_id":tid,"held_after_w03":True,
                        "formal_inbox_while_private_active":0,"native_input_kinds":entries,
                        "native_input_count_after_replay":len(observed["inputs"]),"cancelled_before_admission":cancelled})
        self.evidence = {"cases":facts,"boundary":"Real DSH AgentLoop and isolated Router acceptance; local scripted provider"}
