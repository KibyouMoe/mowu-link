import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import URLError

from .service import private_config
from .storage import Fault, guid
from .wire import Client


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

    def start(self, identities, cert, key):
        process = subprocess.Popen([*CLI,"router","--db",self.root/"router.db","--identities",identities,
                                   "--cert",cert,"--key",key],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                   text=True,cwd=ROOT)
        self.addCleanup(self.stop,process)
        ready = Queue()
        Thread(target=lambda: ready.put(process.stdout.readline()),daemon=True).start()
        info = json.loads(ready.get(timeout=10))
        self.assertTrue(info["tls"])
        return process,"https://127.0.0.1:"+str(info["port"])

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
        cert,key = self.root/"test.crt", self.root/"test.key"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                        "-subj","/CN=localhost","-addext","subjectAltName=IP:127.0.0.1,DNS:localhost",
                        "-keyout",key,"-out",cert],capture_output=True,check=True,timeout=10)
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
