import hashlib
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import URLError

from .approvals import TTL_SECONDS, route as approval_route
from .delivery import read as delivery_read
from .orders import Orders
from .service import private_config
from .storage import Fault, encode, guid
from .trigger import Trigger
from .wire import Client
from .host_wire import RemoteAdapter


CLI = [sys.executable,"-m","rewrite.service"]
ROOT = Path(__file__).resolve().parent.parent


class Service(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def command(self,*args):
        run = subprocess.run([*CLI,*map(str,args)],capture_output=True,text=True,cwd=ROOT,timeout=12,check=True)
        return json.loads(run.stdout)

    def write_config(self, name, value):
        path = self.root / name
        with open(path,"x",opener=lambda name, flags: os.open(name,flags,0o600)) as stream:
            json.dump(value,stream)
        return path

    def start(self, identities, cert, key, role="router", extra=()):
        process = subprocess.Popen([*CLI,role,"--db",self.root/(role+".db"),"--identities",identities,
                                   "--cert",cert,"--key",key,*extra],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                   text=True,cwd=ROOT)
        self.addCleanup(self.stop,process)
        ready = Queue()
        Thread(target=lambda: ready.put(process.stdout.readline()),daemon=True).start()
        info = json.loads(ready.get(timeout=10))
        self.assertTrue(info["tls"])
        if role=="router":
            self.assertEqual(info["dispatch_draining"],"--drain-dispatch" in extra)
        return process,"https://127.0.0.1:"+str(info["port"])

    def certificate(self):
        cert,key = self.root/"test.crt", self.root/"test.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                        "-subj","/CN=localhost","-addext","subjectAltName=IP:127.0.0.1,DNS:localhost",
                        "-keyout",key,"-out",cert],capture_output=True,check=True,timeout=10)
        return cert,key

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture certificate generator unavailable")
    def test_daemon_rejects_second_writer_and_cold_restart_recovers_same_state(self):
        cert,key = self.certificate()
        token = guid()
        identities = self.write_config("router-roles.json",{token:"hermes"})
        primary,url = self.start(identities,cert,key)
        created = Client(url,token,cert).request("POST","tasks",{"title":"Keep this state","instructions":"Isolated"},
                                               guid())
        conflict = subprocess.run([*CLI,"router","--db",self.root/"router.db","--identities",identities,
                                   "--cert",cert,"--key",key],cwd=ROOT,capture_output=True,text=True,timeout=8)
        self.assertEqual((conflict.returncode,conflict.stdout,conflict.stderr),
                         (1,"","state_writer_already_running\n"))
        self.assertIsNone(primary.poll())
        self.assertEqual(Client(url,token,cert).request("GET","tasks")["items"][0]["task_id"],created["task_id"])
        primary.kill()
        primary.wait(8)
        self.assertTrue((self.root/"router.db.writer").is_file())
        _,reopened = self.start(identities,cert,key,extra=("--drain-dispatch",))
        task = Client(reopened,token,cert).request("GET","tasks/"+created["task_id"])["task"]
        self.assertEqual(task["title"],"Keep this state")

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture certificate generator unavailable")
    def test_cli_tls_restart_preserves_identity_file_and_receipts(self):
        cert,key = self.certificate()
        src = ["trigger","--db",self.root/"source.db","--name","trial"]
        source_id = self.command(*src,"identity")["instance_id"]
        sender,manager = guid(),guid()
        ids = self.write_config("roles.json",{sender:"trigger/"+source_id,manager:"hermes"})
        proc,url = self.start(ids,cert,key)
        conn = self.write_config("send.json",{"url":url,"token":sender,"ca_file":str(cert)})
        note = self.root/"note"
        note.write_bytes(b"isolated content")
        mid = self.command(*src,"record","--level","Info","--text","check","--file",note)["message_id"]
        receipt, = self.command(*src,"send","--connection",conn)["receipts"]
        self.assertEqual(receipt["message_id"],mid)
        with self.assertRaises(URLError):
            Client(url,manager).request("GET","messages")
        with self.assertRaises(Fault) as denied:
            Client(url,guid(),cert).request("GET","messages")
        self.assertEqual(denied.exception.status,401)
        self.stop(proc)
        _,url2 = self.start(ids,cert,key)
        msg, = Client(url2,manager,cert).request("GET","messages")["items"]
        self.assertEqual(msg["instance_id"],source_id)
        self.assertEqual(Client(url2,manager,cert).request("GET","attachments/"+msg["attachment_ids"][0])["bytes"],
                         note.read_bytes())
        self.assertEqual(self.command(*src,"identity")["instance_id"],source_id)

    @unittest.skipUnless(os.name == "posix","POSIX file modes only")
    def test_public_config_rejected(self):
        path = self.write_config("roles.json",{})
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            private_config(path)
        with self.assertRaises(ValueError):
            Client("http://example.test","unused")

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture certificate generator unavailable")
    def test_source_receipts_and_consumer_cursor_are_observed_independently(self):
        cert,key = self.certificate()
        source_path = self.root/"watched-source.db"
        source_arg = ("trigger-status","--db",source_path)
        missing = subprocess.run([*CLI,*map(str,source_arg)],cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual((missing.returncode,json.loads(missing.stderr)),(1,{"error":"trigger_archive_missing"}))
        self.assertFalse(source_path.exists())
        source = Trigger(source_path,"test-source")
        sender,manager,other = (guid() for _ in range(3))
        ids = self.write_config("status-roles.json",{sender:"trigger/"+source.instance_id,manager:"hermes",
                                                     other:"worker/unrelated"})
        process,url = self.start(ids,cert,key)
        writer,reader,unrelated = (Client(url,token,cert) for token in (sender,manager,other))
        reader_config = self.write_config("reader.json",{"url":url,"token":manager,"ca_file":str(cert)})
        consumer_arg = ("delivery-status","--connection",reader_config)

        def counts():
            observation = self.command(*consumer_arg)
            self.assertEqual(observation["consumer"],"hermes")
            return tuple(observation[key] for key in ("latest_cursor","confirmed_cursor","awaiting_consumer_ack"))

        self.assertEqual(counts(),(0,0,0))
        note = source.record("Notify","Private fixture body")
        event = source.record("Event","Second fixture body")
        info = source.record("Info","Archive-only fixture body")
        snapshot = source_path.read_bytes()
        status = self.command(*source_arg)
        self.assertEqual((status["instance_id"],status["archived"],status["awaiting_router_receipt"]),
                         (source.instance_id,3,3))
        self.assertIsNone(status["latest_router_received_at"])
        self.assertEqual(source_path.read_bytes(),snapshot)

        request = writer.request
        def drop(*args,**kwargs):
            request(*args,**kwargs)
            raise ConnectionError("fixture loses T04 receipt")
        writer.request = drop
        with self.assertRaises(ConnectionError):
            source.send(writer,note)
        writer.request = request
        local_after_loss = self.command(*source_arg)
        consumer_after_loss = counts()
        self.assertEqual(local_after_loss["awaiting_router_receipt"],3)
        self.assertEqual(consumer_after_loss,(1,0,1))
        for mid in (note,event,info):
            source.send(writer,mid)
        status = self.command(*source_arg)
        self.assertEqual((status["archived"],status["awaiting_router_receipt"]),(3,0))
        self.assertTrue(status["latest_router_received_at"])
        self.assertNotIn("fixture body",json.dumps(status))
        self.assertEqual(counts(),(2,0,2))
        self.assertEqual(unrelated.request("GET","deliveries/status"),
                         {"consumer":"worker/unrelated","latest_cursor":0,"confirmed_cursor":0,"awaiting_consumer_ack":0})
        with self.assertRaisesRegex(Fault,"forbidden"):
            writer.request("GET","deliveries/status")
        with self.assertRaisesRegex(Fault,"unknown_status_filter"):
            unrelated.request("GET","deliveries/status?consumer=hermes")
        first,second = reader.frames(0)
        self.assertEqual([first["payload"]["message_id"],second["payload"]["message_id"]],[note,event])
        self.assertEqual(reader.ack(second)["confirmed_cursor"],0)
        self.assertEqual(counts(),(2,0,1))
        self.stop(process)
        _,new_url = self.start(ids,cert,key)
        new_reader = Client(new_url,manager,cert)
        reboot = new_reader.request("GET","deliveries/status")
        self.assertEqual((reboot["latest_cursor"],reboot["confirmed_cursor"],reboot["awaiting_consumer_ack"]),(2,0,1))
        self.assertEqual(new_reader.ack(first)["confirmed_cursor"],2)
        closed = new_reader.request("GET","deliveries/status")
        self.assertEqual(closed["awaiting_consumer_ack"],0)
        self.evidence = {"messages":[note,event,info],"source_after_lost_t04":local_after_loss,
            "router_after_lost_t04":dict(zip(("latest_cursor","confirmed_cursor","awaiting_consumer_ack"),consumer_after_loss)),
            "source_after_replay":status,"out_of_order_ack_after_restart":reboot,"all_acks":closed,
            "boundary":"Temporary TLS Router, disposable identities and synthetic consumer ACKs. No host read receipt."}

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture certificate generator unavailable")
    def test_command_approval_requires_a_separate_operator_and_keeps_the_original_decision(self):
        cert,key = self.certificate()
        worker,operator,other,manager = (guid() for _ in range(4))
        ids = self.write_config("approval-roles.json",
                                {worker:"worker/dev",operator:"approver/dev",other:"approver/other",manager:"hermes"})
        process,url = self.start(ids,cert,key)
        w,approver,outsider,hermes = (Client(url,token,cert) for token in (worker,operator,other,manager))
        route = "approvals/dev/"+guid()
        session = guid()
        w.request("PUT","workers/dev/session",{"session_id":session},guid())
        body = {"session_id":session,"request":{"session":"dedicated","turn_id":"approval-turn",
                "item_id":"read-only-command","cwd":"/isolated","command":"pwd",
                "environment_id":None,"decisions":["accept","cancel"]}}
        first = w.request("POST",route,body)
        self.assertEqual(first,w.request("POST",route,body))
        notice, = approver.frames(0)
        self.assertEqual((notice["kind"],notice["payload"]),
                         ("approval.requested",{"approval_id":first["approval_id"],"worker_id":"dev"}))
        self.assertEqual(hermes.frames(0),[])
        with self.assertRaisesRegex(Fault,"delivery_not_found"):
            hermes.ack(notice)
        self.assertEqual(approver.ack(notice)["confirmed_cursor"],1)
        operator_cfg = self.write_config("operator.json",{"url":url,"token":operator,"ca_file":str(cert)})
        cli = ("approvals","--connection",operator_cfg,"--worker-id","dev")
        self.assertEqual(self.command(*cli,"list"),{"items":[first]})
        self.assertEqual(self.command(*cli,"show",first["approval_id"]),first)
        wrong = {**body,"request":dict(body["request"],command="ls")}
        with self.assertRaisesRegex(Fault,"approval_request_conflict"):
            w.request("POST",route,wrong)
        view, = approver.request("GET","approvals/dev")["items"]
        self.assertEqual(first,view)
        self.assertEqual(view["state"],"pending")
        decision = {"request_digest":view["request_digest"],"decision":"cancel"}
        for client in (w,outsider,hermes):
            with self.assertRaisesRegex(Fault,"forbidden"):
                client.request("PUT",route+"/decision",decision)
            with self.assertRaisesRegex(Fault,"forbidden"):
                client.request("GET",route)
        with self.assertRaisesRegex(Fault,"approval_view_changed"):
            approver.request("PUT",route+"/decision",dict(decision,request_digest="stale"))
        with self.assertRaisesRegex(Fault,"unsupported_approval_decision"):
            approver.request("PUT",route+"/decision",dict(decision,decision="acceptForSession"))
        chosen = self.command(*cli,"decide",first["approval_id"],"--request-digest",view["request_digest"],
                              "--decision","cancel")
        self.assertEqual((chosen["state"],chosen["attempted_response"]),("decided",None))
        self.assertEqual(w.request("POST",route,body),chosen)
        with self.assertRaisesRegex(Fault,"approval_decision_conflict"):
            approver.request("PUT",route+"/decision",dict(decision,decision="accept"))
        self.stop(process)
        _,url = self.start(ids,cert,key)
        w,approver = Client(url,worker,cert),Client(url,operator,cert)
        self.assertEqual(approver.request("PUT",route+"/decision",decision),chosen)
        proof = {"session_id":session,"request_digest":decision["request_digest"],
                 "attempted_response":{"source":"operator","decision":"cancel"}}
        closed = w.request("PUT",route+"/closed",proof)
        self.assertEqual(closed["state"],"native_closed")
        self.assertEqual(w.request("PUT",route+"/closed",proof),closed)
        another = "approvals/dev/"+guid()
        w.request("POST",another,body)
        w.request("PUT","workers/dev/session",{"session_id":guid()},guid())
        self.assertEqual(approver.request("GET",another)["state"],"disconnected")
        with self.assertRaisesRegex(Fault,"approval_not_pending"):
            approver.request("PUT",another+"/decision",decision)
        with self.assertRaisesRegex(Fault,"stale_session"):
            w.request("POST",another,body)
        with self.assertRaisesRegex(Fault,"approval_relay_unavailable"):
            w.request("POST","approvals/no-operator/"+guid(),body)

    def test_command_approval_expiry_retains_unconfirmed_native_state(self):
        now = 1000
        orders = Orders(self.root/"expiring.db",clock=lambda:now)
        binding = {"session_id":guid()}
        with orders.db.transaction() as db:
            orders.session(db,"dev",binding)
        route = ["approvals","dev",guid()]
        request = {"session":"isolated","turn_id":"original-turn","item_id":"original-command","cwd":"/isolated",
                   "command":"pwd","environment_id":None,"decisions":["accept","cancel"]}
        offer = approval_route(orders,"worker/dev","POST",route,dict(binding,request=request))
        self.assertEqual(offer["expires_at"],now+TTL_SECONDS)
        now += TTL_SECONDS
        decision = {"request_digest":offer["request_digest"],"decision":"accept"}
        with self.assertRaisesRegex(Fault,"approval_not_pending"):
            approval_route(orders,"approver/dev","PUT",[*route,"decision"],decision)
        row = approval_route(orders,"approver/dev","GET",route,{})
        self.assertEqual((row["state"],row["decision"],row["closed_at"]),("expired",None,None))
        proof = dict(binding,request_digest=offer["request_digest"],attempted_response={"source":"expiry","decision":"cancel"})
        row = approval_route(orders,"worker/dev","PUT",[*route,"closed"],proof)
        self.assertEqual((row["state"],row["decision"],row["attempted_response"]),
                         ("native_closed",None,{"source":"expiry","decision":"cancel"}))

    def test_native_deadline_closes_unregistered_request_without_opening_approval(self):
        orders = Orders(self.root/"deadline.db",clock=lambda:1000)
        binding = {"session_id":guid()}
        with orders.db.transaction() as db:
            orders.session(db,"dev",binding)
        entry = ["approvals","dev",guid()]
        closed = [*entry,"closed"]
        request = {"session":"isolated","turn_id":"original-turn","item_id":"original-command","cwd":"/isolated",
                   "command":"pwd","environment_id":None,"decisions":["accept","cancel"]}
        digest = hashlib.sha256(encode(request).encode()).hexdigest()
        attempted = {"source":"native_deadline","decision":"cancel"}
        proof = dict(binding,request_digest=digest,attempted_response=attempted,request=request)
        malformed = [
            ("approver/dev",proof,"forbidden"),
            ("worker/dev",dict(proof,session_id=guid()),"stale_session"),
            ("worker/dev",dict(proof,request=dict(request,command="ls")),"approval_request_conflict"),
            ("worker/dev",dict(proof,attempted_response={"source":"native_deadline","decision":"accept"}),
             "invalid_approval_closure"),
        ]
        for actor,body,error in malformed:
            with self.subTest(error=error), self.assertRaisesRegex(Fault,error):
                approval_route(orders,actor,"PUT",closed,body)
        self.assertEqual(approval_route(orders,"approver/dev","GET",entry[:2],{}),{"items":[]})
        receipt = approval_route(orders,"worker/dev","PUT",closed,proof)
        self.assertEqual((receipt["state"],receipt["decision"],receipt["attempted_response"]),
                         ("native_closed",None,attempted))
        self.assertEqual(approval_route(orders,"worker/dev","PUT",closed,proof),receipt)
        self.assertEqual(approval_route(orders,"worker/dev","POST",entry,dict(binding,request=request)),receipt)
        with self.assertRaisesRegex(Fault,"approval_not_pending"):
            approval_route(orders,"approver/dev","PUT",[*entry,"decision"],{"request_digest":digest,"decision":"accept"})
        with orders.db.transaction() as db:
            self.assertEqual(delivery_read(db,"approver/dev",0),[])

    def test_dsh_no_notice_closure_requires_original_non_grant_proof(self):
        orders = Orders(self.root/"dsh-deadline.db",clock=lambda:1000)
        binding = {"session_id":guid()}
        with orders.db.transaction() as db:
            orders.session(db,"dev",binding)
        aid = guid()
        entry = ["approvals","dev",aid]
        closed = [*entry,"closed"]
        request = {"session":"isolated","turn_id":"7/2","item_id":aid,"cwd":"/isolated","command":"pwd",
                   "environment_id":None,"decisions":["accept","cancel"]}
        proof = {"approval_id":aid,"turn_id":"7/2","asked_seq":9,"decided_seq":12,"outcome":"cancelled"}
        digest = hashlib.sha256(encode(request).encode()).hexdigest()
        body = dict(binding,request_digest=digest,attempted_response=None,request=request,native_proof=proof)
        rejected = [
            (dict(proof,approval_id=guid()),"native_approval_proof_unconfirmed"),
            (dict(proof,turn_id="7/3"),"native_approval_proof_unconfirmed"),
            (dict(proof,decided_seq=9),"native_approval_proof_unconfirmed"),
            (dict(proof,outcome="allowed-once"),"native_approval_proof_unconfirmed"),
        ]
        for bad_proof,error in rejected:
            with self.assertRaisesRegex(Fault,error):
                approval_route(orders,"worker/dev","PUT",closed,dict(body,native_proof=bad_proof))
        self.assertEqual(approval_route(orders,"approver/dev","GET",entry[:2],{}),{"items":[]})
        receipt = approval_route(orders,"worker/dev","PUT",closed,body)
        self.assertEqual(receipt["native_proof"],proof)
        self.assertIsNone(receipt["attempted_response"])
        self.assertIsNone(receipt["decision"])
        self.assertEqual(receipt["state"],"native_closed")
        with self.assertRaisesRegex(Fault,"approval_closure_conflict"):
            approval_route(orders,"worker/dev","PUT",closed,dict(body,native_proof=dict(proof,decided_seq=13)))
        restarted = Orders(self.root/"dsh-deadline.db",clock=lambda:1001)
        self.assertEqual(approval_route(restarted,"worker/dev","PUT",closed,body),receipt)
        self.assertEqual(approval_route(restarted,"worker/dev","POST",entry,dict(binding,request=request)),receipt)
        with restarted.db.transaction() as db:
            self.assertEqual(delivery_read(db,"approver/dev",0),[])

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture certificate generator unavailable")
    def test_three_process_worker_chat_and_assignment(self):
        from .devloop import FixtureHost
        cert,key = self.certificate()
        router_token,manager,adapter_token,callback,platform,ops,handoff = (guid() for _ in range(7))
        ids = self.write_config("routes.json",{router_token:"worker/dev",manager:"hermes"})
        _, router = self.start(ids,cert,key)
        native_db = self.root/"native.db"
        session = "isolated-fixture"
        _, adapter = self.start(self.write_config("host.json",{adapter_token:"worker/dev"}),cert,key,
                               "fixture-adapter",["--worker-id","dev","--fixture-db",native_db,"--session",session])
        def target(name,url,token):
            return self.write_config(name,{"url":url,"token":token,"ca_file":str(cert)})
        worker_roles = self.write_config("worker.json",{callback:"adapter/dev",platform:"platform/dev",
                                                        ops:"ops/dev",handoff:"handoff/dev"})
        worker_flags = ["--worker-id","dev","--router-connection",target("to-router",router,router_token),
                        "--adapter-connection",target("to-host",adapter,adapter_token)]
        worker_process,gateway = self.start(worker_roles,cert,key,"worker",[*worker_flags,"--adapter-session",session])
        chat = Client(gateway,platform,cert)
        msg = {"message_id":guid(),"text":"fixture incoming text"}
        first = chat.request("POST","worker/chat",msg)
        self.assertEqual(first,chat.request("POST","worker/chat",msg))
        manager_client = Client(router,manager,cert)
        handoff_id = guid()
        cli = ("handoff-admission","--connection",target("to-handoff",gateway,handoff),"--handoff-id",handoff_id)
        self.assertEqual(self.command(*cli,"begin"),{"handoff_id":handoff_id,"state":"frozen"})
        tid = manager_client.request("POST","tasks",{"assignee_id":"dev","title":"Scope","instructions":"Send one result"},guid())["task_id"]
        self.assertIsNone(manager_client.request("GET","tasks/"+tid)["task"]["assignment_id"])
        self.assertEqual(self.command(*cli,"end"),{"handoff_id":handoff_id,"state":"released"})
        native = FixtureHost(native_db,session)
        deadline = time.monotonic()+32
        while time.monotonic()<deadline:
            entries = native.entries()
            if len(entries)>=2:
                break
            time.sleep(0.15)
        self.assertEqual([p["kind"] for p,_ in entries],["chat","task"])
        task,receipt = entries[1]
        aid = task["assignment_id"]
        caller = Client(gateway,callback,cert)
        body = {"session":session,"turn_id":receipt["turn_id"],"tool":"worker_report",
                "arguments":{"task_id":tid,"assignment_id":aid,"summary":"controlled host report"}}
        with self.assertRaises(Fault):
            chat.request("POST","worker/native-tool",body)
        with self.assertRaises(Fault):
            caller.request("POST","worker/native-tool",dict(body,turn_id=first["receipt"]["turn_id"]))
        result = caller.request("POST","worker/native-tool",body)
        self.assertEqual(result,caller.request("POST","worker/native-tool",body))
        self.assertEqual(result["task_state"],"completed_reported")
        self.assertIsNone(Client(gateway,ops,cert).request("GET","worker/status")["held_assignment_id"])
        with self.assertRaisesRegex(Fault,"forbidden"):
            chat.request("GET","worker/status")
        status = self.command("worker-status","--worker-id","dev","--connection",target("to-ops",gateway,ops))
        self.assertEqual((status["host_session"],status["expected_host_session"]),(session,session))
        self.assertIs(status["host_busy"],False)
        self.assertEqual(status["native_capabilities"],
                         {"next_turn":False,"context_usage":False,"compaction":False,"session_catalog":False,
                          "stop_private":False,"model_view":False,"model_change":False,"goal_control":False})
        self.assertIs(status["router_connected"],True)
        self.assertIsNone(status["held_task_id"])
        self.assertEqual(len(manager_client.frames(0)),1)
        self.stop(worker_process)
        for flags in ([],["--adapter-session","unplanned-session"]):
            refusal = subprocess.run([*CLI,"worker","--db",self.root/"worker.db","--identities",worker_roles,
                "--cert",cert,"--key",key,*worker_flags,*flags],cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertEqual((refusal.returncode,refusal.stdout,json.loads(refusal.stderr)),
                             (1,"",{"error":"native_binding_change_requires_handoff"}))
        _,reopened = self.start(worker_roles,cert,key,"worker",[*worker_flags,"--adapter-session",session])
        self.assertEqual(Client(reopened,platform,cert).request("POST","worker/chat",msg),first)
        self.assertEqual(len(native.entries()),2)

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture certificate generator unavailable")
    def test_runtime_resume_uses_opted_in_connection_map(self):
        from .devloop import FixtureHost

        cert,key = self.certificate()
        router_token,platform,ops = (guid() for _ in range(3))
        _,router = self.start(self.write_config("router-roles.json",{router_token:"worker/dev"}),cert,key)

        def connection_file(name,url,token):
            return self.write_config(name,{"url":url,"token":token,"ca_file":str(cert)})

        connections,hosts,roles = {},{},{platform:"platform/dev",ops:"ops/dev"}
        for session in ("initial","target"):
            reader = guid()
            roles[guid()] = "adapter/dev/"+session
            path = self.root/(session+".native.db")
            _,url = self.start(self.write_config(session+".roles",{reader:"worker/dev"}),cert,key,"fixture-adapter",
                               ["--worker-id","dev","--db",self.root/(session+".adapter.db"),"--fixture-db",path,
                                "--session",session])
            hosts[session] = FixtureHost(path,session)
            connections[session] = connection_file(session+".connection",url,reader)

        ids = self.write_config("worker-roles.json",roles)
        targets = self.write_config("resume-targets.json",{"target":connections["target"].name})
        flags = ["--worker-id","dev","--router-connection",connection_file("to-router",router,router_token),
                 "--adapter-connection",connections["initial"],"--adapter-session","initial","--resume-targets",targets]
        process,gateway = self.start(ids,cert,key,"worker",flags)
        platform_client = Client(gateway,platform,cert)

        def status(url):
            return Client(url,ops,cert).request("GET","worker/status")

        def wait_ready(url,session):
            deadline = time.monotonic()+8
            while time.monotonic()<deadline:
                state = status(url)
                if state["host_session"]==session and state["router_connected"]:
                    return
                time.sleep(0.05)
            self.fail("Worker did not confirm the expected binding: "+repr(state))

        wait_ready(gateway,"initial")
        first = {"message_id":guid(),"text":"Original session before TLS switch"}
        before = platform_client.request("POST","worker/chat",first)
        cmd = {"message_id":guid(),"text":"/resume target"}
        switched = platform_client.request("POST","worker/command",cmd)
        self.assertEqual(switched["receipt"]["binding_receipt"]["session"],"target")
        wait_ready(gateway,"target")
        second = {"message_id":guid(),"text":"Selected session after TLS switch"}
        after = platform_client.request("POST","worker/chat",second)
        self.assertEqual((before["receipt"]["session"],after["receipt"]["session"]),("initial","target"))

        self.stop(process)
        _,reopened = self.start(ids,cert,key,"worker",flags)
        wait_ready(reopened,"target")
        again = Client(reopened,platform,cert)
        self.assertEqual(again.request("POST","worker/command",cmd),switched)
        self.assertEqual(again.request("POST","worker/chat",first),before)
        self.assertEqual(again.request("POST","worker/chat",second),after)
        display = again.request("GET","worker/display")["items"]
        self.assertEqual(sum(item["event"]["event_id"]=="command-"+cmd["message_id"] for item in display),1)
        self.assertEqual([len(host.entries()) for host in hosts.values()],[1,1])

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture certificate generator unavailable")
    def test_pinned_worker_rejects_same_authenticated_adapter_when_session_differs(self):
        from .devloop import FixtureHost
        cert,key = self.certificate()
        adapter_token = guid()
        native_db = self.root/"native.db"
        _, url = self.start(self.write_config("host.json",{adapter_token:"worker/dev"}),cert,key,"fixture-adapter",
                            ["--worker-id","dev","--fixture-db",native_db,"--session","new-unintended-session"])
        old = RemoteAdapter(Client(url,adapter_token,cert),"old-pinned-session")
        with self.assertRaisesRegex(Fault,"wrong_native_session"):
            old.busy()
        with self.assertRaisesRegex(Fault,"wrong_native_session"):
            old.deliver(guid(),{"kind":"chat","text":"Must remain outside the wrong session"})
        unpinned = RemoteAdapter(Client(url,adapter_token,cert))
        with self.assertRaisesRegex(Fault,"wrong_native_session"):
            unpinned.deliver(guid(),{"kind":"chat","text":"Chosen by an earlier read"},session="old-pinned-session")
        self.assertEqual(FixtureHost(native_db,"new-unintended-session").entries(),[])
        current = RemoteAdapter(Client(url,adapter_token,cert),"new-unintended-session")
        body = {"kind":"chat","message_id":"test-native","text":"Dedicated test input"}
        self.assertIsNone(current.lookup_delivery("chat-test-native",body,session="new-unintended-session"))
        self.assertEqual(FixtureHost(native_db,"new-unintended-session").entries(),[])
        proof = current.deliver("chat-test-native",body)
        self.assertEqual(current.lookup_delivery("chat-test-native",body,session="new-unintended-session"),proof)
        self.assertEqual(proof["session"],"new-unintended-session")
        did = guid()
        peer = {"kind":"peer","message_id":guid(),"to_worker":"dev","text":"Original private message"}
        self.assertIsNone(current.lookup_delivery(did,peer,session=proof["session"]))
        peer_proof = current.deliver(did,peer,session=proof["session"])
        self.assertEqual(current.lookup_delivery(did,peer,session=proof["session"]),peer_proof)
        with self.assertRaisesRegex(Fault,"invalid_host_input"):
            current.lookup_delivery(did,dict(peer,to_worker="other"),session=proof["session"])
        did = guid()
        task = {"kind":"task","task_id":guid(),"assignment_id":guid(),"instructions":"Isolated task"}
        self.assertIsNone(current.lookup_delivery(did,task,session=proof["session"]))
        task_proof = current.deliver(did,task,session=proof["session"])
        self.assertEqual(current.lookup_delivery(did,task,session=proof["session"]),task_proof)
        with self.assertRaisesRegex(Fault,"delivery_conflict"):
            current.lookup_delivery(did,dict(task,assignment_id=guid()),session=proof["session"])
        self.assertEqual(len(FixtureHost(native_db,proof["session"]).entries()),3)
