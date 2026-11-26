"""Run the installed DSH AgentLoop with a local deterministic provider and a fresh profile."""
import hashlib
import json
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import os
import shutil
from pathlib import Path
import select
import subprocess
import tempfile
from threading import Event, Thread
import time
import unittest

from . import test_codex_app_server as codex_driver, worker_commands
from .adapter import HostAdapter
from .approvals import TTL_SECONDS
from .codex_app_server import AppServerHost, worker_callbacks
from .codex_display import CodexDisplay
from .devloop import FixtureHost, Loop
from .dsh_display import DshDisplay
from .dsh_port import DshActivation, DshHost, tool_listener
from .dsh_session_family import DshPreparation
from .feishu_inbound import Inbound
from .feishu_output import Views
from .host_wire import RemoteAdapter, serve_adapter
from .native_files import NativeFiles
from .orders import Orders
from .release_check import config, serve
from .question_cards import Actions
from .state_bundle import agent_pending, capture, filename, restore, rollback, verify
from .storage import Fault, encode, guid
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .trigger import Trigger
from .test_dsh_sdk import APP, EXE
from .test_feishu_inbound import PNG
from .wire import Client, serve_loopback
from .worker import Worker
from .worker_service import WorkerService


FIXTURE = Path(__file__).parent/"integrations/dsh_host/native-loop-fixture.mjs"


def environment(directory):
    return {"ELECTRON_RUN_AS_NODE":"1","HOME":directory,"TMPDIR":directory,
            "XDG_CONFIG_HOME":directory,"PATH":os.defpath}


def observe():
    with tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-loop-") as directory:
        result = subprocess.run([EXE,FIXTURE.resolve(),APP/"Resources/app.asar/dsh",directory,"dsh-isolated-real-loop"],
                                env=environment(directory),cwd=directory,timeout=12,text=True,capture_output=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)


@unittest.skipUnless(EXE.is_file(),"installed macOS DSH SDK required")
class NativeLoop(unittest.TestCase):
    def read_native(self,native,command=None):
        if command is not None:
            native.stdin.write(command+"\n")
            native.stdin.flush()
        self.assertTrue(select.select([native.stdout],[],[],10)[0],"DSH did not return a native observation")
        return json.loads(native.stdout.readline())

    @contextmanager
    def start_native(self,directory,session,mode):
        with subprocess.Popen([EXE,FIXTURE.resolve(),APP/"Resources/app.asar/dsh",directory,session,mode],
                              env=environment(directory),cwd=directory,stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as native:
            try:
                self.assertEqual(self.read_native(native),{"ready":True,"session":session})
                yield native
            finally:
                try:
                    tail,errors = native.communicate("exit\n",timeout=6)
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
        session = "isolated-dsh-bash-approvals"
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-permission-")))
        worker_token,operator,manager,callback,platform = (guid() for _ in range(5))
        advance = 0
        orders = Orders(directory/"router.db",clock=lambda:time.time()+advance)
        router = self.enterContext(codex_driver.running(serve_loopback(orders,{
            worker_token:"worker/dev",operator:"approver/dev",manager:"hermes"})))
        host = DshHost(directory)
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
            self.assertEqual((seen["request"]["session"],seen["request"]["command"]),(session,"pwd"))
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

        with tool_listener(directory,adapter,callback_wire), self.start_native(directory,session,"command-approvals") as native:
            worker.chat(guid(),"DSH approval fixture implicit")
            local = self.read_native(native,"read")
            first_local,last_local = local["approvals"]
            self.assertEqual(first_local["data"]["callId"],"fixture-bash-implicit")
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
            self.assertIn(str(directory.resolve()),encode(result))

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
        reopened = HostAdapter(directory/"adapter.db",DshHost(directory),session)
        block_closure.clear()
        with tool_listener(directory,reopened,callback_wire), self.start_native(directory,session,"command-approvals-reopen") as native:
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
                         "implicit_directory_used_native_answerer":True,
                         "successful_bash_calls":1,
                         "boundary":"Installed DSH AgentLoop, ApprovalService and Bash provider; test model and operator identity"}

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
        sid = "isolated-dsh-managed-root"
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="ml-dsh-managed-"))).resolve()
        registry = root/"enrolled"
        registry.mkdir(mode=0o700)
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock))
        incoming_token,callback_token,tls_token,foreign = (guid() for _ in range(4))
        cert,key = root/"tls.crt",root/"tls.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                        "-addext","subjectAltName=IP:127.0.0.1","-out",cert,"-keyout",key],
                       capture_output=True,timeout=10,check=True)
        roles = config(root,"adapter-roles.json",{tls_token:"worker/oguri",foreign:"worker/other"})
        callback_key = os.urandom(32)
        package = Path(__file__).resolve().parents[1]
        enrollment = []

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
                with self.start_native(root, sid, mode) as native, \
                     serve(package,"dsh-adapter",root/"adapter.db",roles,cert,key,dict(os.environ),extra) as host_url:
                    actual = Client(host_url,tls_token,cert)
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
            "scope":"Isolated installed DSH SDK, real TLS adapter and rewrite Router/Worker, fixture model and platform."}
        output = os.environ.get("MOWULINK_DSH_MANAGED_EVIDENCE")
        if output:
            Path(output).write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

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
        session = "dsh-real-loop-formal"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-formal-"))
        class LateDeliveryReceipt(DshHost):
            def deliver(self,actual_session,did,payload,digest):
                proof = super().deliver(actual_session,did,payload,digest)
                return None if payload["kind"]=="task" else proof
        def host(_path,_name):
            port = LateDeliveryReceipt(Path(directory))
            port.session = session
            return port
        clock = Clock()
        loop = self.enterContext(Loop.isolated(clock,host_factory=host))
        credential = guid()
        service = WorkerService(loop.worker,clock)
        client = Client(self.open_worker(service,{credential:"adapter/oguri"}),credential)
        self.enterContext(tool_listener(Path(directory),loop.adapter,client))
        router_requests = []
        with self.start_native(directory,session,"formal") as native:
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

    def goal_worker(self,session,extra_workers=()):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-goals-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
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
        return directory,loop,service,command

    def test_read_only_dsh_acknowledges_original_cancel_or_transfer_and_recovers_w04_reply(self):
        cases = []
        for action in ("cancel","transfer"):
            with self.subTest(action=action):
                session = "dsh-read-only-"+action
                directory,loop,service,_command = self.goal_worker(session,extra_workers=("tama",))
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
                with tool_listener(runtime,loop.adapter,client), self.start_native(directory,session,"cleanup-read-only") as native:
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
                    "dropped_router_w04_receipts":int(action=="cancel"),"recorded_w04_replays":len(cleanup_receipts)})
        output = os.environ.get("MOWULINK_DSH_CLEANUP_EVIDENCE")
        if output:
            Path(output).write_text(json.dumps({"cases":cases,
                "boundary":"Installed DSH SDK with opt-in read-only assignments, local model, real isolated Router."},
                ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    def test_native_dsh_stop_alone_or_unverified_tool_keeps_cleanup_pending(self):
        cases = []
        for mode in ("cleanup-unscoped","cleanup-forbidden-tool"):
            with self.subTest(mode=mode):
                sid = "dsh-"+mode
                directory,loop,service,_command = self.goal_worker(sid)
                token = guid()
                client = Client(self.open_worker(service,{token:"adapter/oguri"}),token)
                w = loop.worker
                runtime = Path(directory)
                with tool_listener(runtime,loop.adapter,client), self.start_native(directory,sid,mode) as native:
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
        evidence = []
        for outcome in ("feishu-first","native-first","canceled","timed-out"):
            with self.subTest(outcome=outcome):
                evidence.append(self.foreground_relay(outcome))
        self.evidence = {"cases":evidence,
            "boundary":"Installed DSH AgentLoop, question SDK and persistence. Local model, native answer provider and Feishu test platform."}

    def foreground_relay(self,outcome):
        session = "dsh-real-question-"+outcome
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-live-q-")))
        native = DshHost(directory)
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
        with self.start_native(directory,session,fixture) as process:
            project()
            loop.worker.chat(guid(),"Question fixture foreground")
            waiting = self.read_native(process,"foreground-question")
            self.assertEqual((waiting["status"],waiting["continued"]),("running",0))
            self.assertEqual(waiting["timed"],outcome=="timed-out")
            project()
            with views.db.transaction() as db:
                qid,mid = tuple(db.execute("SELECT qid,mid FROM question_cards").fetchone())
            target = {"turn_id":waiting["turn_id"],"native_question_id":waiting["call_id"]}
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
        if outcome=="feishu-first":
            with self.start_native(directory,session,"foreground-relay-reopen") as process:
                self.assertEqual(native.lookup_answer(session,waiting["turn_id"],waiting["call_id"]),proof)
                readback = self.read_native(process,"read")
                self.assertEqual(readback["results"],observed["results"])
                self.assertEqual(sink.calls.count(("patch",mid)),1)
                result["reopened_receipt_id"] = proof["receipt_id"]
        return result

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
        session = "dsh-native-media"
        directory = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp",prefix="mldsh-media-"))
        def host(_path,_name):
            port = DshHost(Path(directory))
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
        def fetch(mid,key,kind,limit):
            downloads.append((mid,key,kind))
            return PNG
        inbound = Inbound(loop.root/"feishu.db",platform,fetch,"app","trainer","test-chat")
        sink = PlatformFixture()
        views = Views(loop.root/"views.db",platform,sink,"test-chat","小栗帽",clock)
        caption = "  图上有哪些细节？\n同一条原文。 "
        post = {"zh_cn":{"content":[[{"tag":"text","text":caption},{"tag":"img","image_key":"img_fixture"}]]}}
        def record(mid,kind,content):
            return inbound.record("app","user","trainer",{"chat_id":"test-chat","chat_type":"p2p","message_type":kind,
                "message_id":mid,"content":json.dumps(content,ensure_ascii=False)})
        with self.start_native(directory,session,"media") as native:
            relay.collect()
            one = record("om_dsh_caption","post",post)
            proof = inbound.deliver(one)
            self.assertEqual(inbound.deliver(record(one,"post",post)),proof)
            self.read_native(native,"read")
            two = record("om_dsh_image","image",{"image_key":"img_fixture"})
            pure = inbound.deliver(two)
            self.assertEqual(inbound.deliver(two),pure)
            observed = self.read_native(native,"read")
            sha = hashlib.sha256(PNG).hexdigest()
            self.assertEqual(observed["mediaRequests"],[
                {"caption":caption,"images":[{"sha256":sha,"size":len(PNG)}],"partTypes":["text","image"]},
                {"caption":"","images":[{"sha256":sha,"size":len(PNG)}],"partTypes":["image"]},
            ])
            self.assertEqual(observed["inputs"],[proof["native_id"],pure["native_id"]])
            self.assertEqual(proof["media_sha256"],[sha])
            self.assertEqual(pure["media_sha256"],[sha])
            for _ in range(8):
                relay.collect()
                relay.flush()
            views.poll()
            rows = [item[2] for item in sink.messages.values()]
            finals = [r for r in rows if r["header"]["title"]["content"]=="小栗帽-回复"]
            self.assertEqual(len(finals),2)
            self.assertTrue(all("Local fixture received 1 image(s)." in json.dumps(f) for f in finals))
            platform_items = platform.request("GET","worker/display")["items"]
            self.assertEqual([item["event"]["text"] for item in platform_items if item["event"]["kind"]=="native_input"],[caption])
            self.assertEqual(len(downloads),2)
            self.assertEqual(loop.hermes.tasks()["items"],[])
            self.evidence = {"session":session,"captioned_receipt":proof,"image_only_receipt":pure,
                "provider_messages":observed["mediaRequests"],"download_count":len(downloads),
                "original_input_count":len(observed["inputs"]),"reply_cards":len(finals),
                "boundary":"Actual DSH run loop and attachment storage; Feishu and model provider are fixtures"}

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
