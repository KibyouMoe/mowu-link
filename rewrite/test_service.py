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

from .service import private_config
from .storage import Fault, guid
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
    def test_three_process_worker_chat_and_assignment(self):
        from .devloop import FixtureHost
        cert,key = self.certificate()
        router_token,manager,adapter_token,callback,platform,ops = (guid() for _ in range(6))
        ids = self.write_config("routes.json",{router_token:"worker/dev",manager:"hermes"})
        _, router = self.start(ids,cert,key)
        native_db = self.root/"native.db"
        session = "isolated-fixture"
        _, adapter = self.start(self.write_config("host.json",{adapter_token:"worker/dev"}),cert,key,
                               "fixture-adapter",["--worker-id","dev","--fixture-db",native_db,"--session",session])
        def target(name,url,token):
            return self.write_config(name,{"url":url,"token":token,"ca_file":str(cert)})
        _, gateway = self.start(self.write_config("worker.json",{callback:"adapter/dev",platform:"platform/dev",ops:"ops/dev"}),
                                cert,key,"worker",["--worker-id","dev","--router-connection",
                                target("to-router",router,router_token),"--adapter-connection",target("to-host",adapter,adapter_token),
                                "--adapter-session",session])
        chat = Client(gateway,platform,cert)
        msg = {"message_id":guid(),"text":"fixture incoming text"}
        first = chat.request("POST","worker/chat",msg)
        self.assertEqual(first,chat.request("POST","worker/chat",msg))
        manager_client = Client(router,manager,cert)
        tid = manager_client.request("POST","tasks",{"assignee_id":"dev","title":"Scope","instructions":"Send one result"},guid())["task_id"]
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
        self.assertIs(status["router_connected"],True)
        self.assertIsNone(status["held_task_id"])
        self.assertEqual(len(manager_client.frames(0)),1)

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
        self.assertEqual(len(FixtureHost(native_db,proof["session"]).entries()),2)
