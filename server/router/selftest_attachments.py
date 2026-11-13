#!/usr/bin/env python3
"""Real Worker -> HTTP router -> trigger -> receiver -> native media acceptance.
All services, tokens, storage and source files are isolated from production.
"""
import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from unittest.mock import patch

# Bootstrap before creating any test service; it may re-exec into Hermes' runtime.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get("HERMES_SOURCE", str(Path.home()/".hermes/hermes-agent")))
import hermes_bootstrap

import selftest_router as harness
from attachments import AttachmentStore, MAX_ATTACHMENT_BYTES

ROOT = Path(__file__).resolve().parents[2]
NODE = os.environ.get("NODE_BINARY") or shutil.which("node")
PLUGIN_DIR = ROOT / "server/hermes-plugin/trigger-bus"


def load_plugin():
    spec = importlib.util.spec_from_file_location("test_chat_attachment_bus", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def png_bytes():
    def chunk(kind, data):
        raw = kind + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\x00\x00\xff"))
            + chunk(b"IEND", b""))


class ChatAttachments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="worker-chat-attachments-")
        cls.work = Path(cls.temp.name)
        cls.procs = []
        cls.handles = []
        cls.base = "http://127.0.0.1:%d" % harness.free_port()
        cls.trigger = "http://127.0.0.1:%d" % harness.free_port()
        for name, argv in [
            ("trigger", [str(harness.TRIGGER), "--port", cls.trigger.rsplit(":",1)[1],
                         "--db", str(cls.work/"trigger.db"), "--token-file", str(cls.work/"trigger.token"),
                         "--rules-file", str(cls.work/"rules.json"), "--blocked-log", str(cls.work/"blocked.log"),
                         "--log", str(cls.work/"trigger.log")]),
            ("router", [str(harness.ROUTER), "--port", cls.base.rsplit(":",1)[1],
                        "--db", str(cls.work/"router.db"), "--token-file", str(cls.work/"router.token"),
                        "--lock", str(cls.work/"router.lock"), "--log", str(cls.work/"router.log"),
                        "--trigger-url", cls.trigger+"/trigger",
                        "--trigger-token-file", str(cls.work/"trigger.token")])]:
            proc, handle = harness.start_process([sys.executable]+argv, cls.work/(name+".out"))
            cls.procs.append(proc); cls.handles.append(handle)
            if not harness.wait_http((cls.base if name=="router" else cls.trigger)+"/health"):
                raise RuntimeError((cls.work/(name+".out")).read_text())
        cls.token = (cls.work/"router.token").read_text().strip()
        cls.trigger_token = (cls.work/"trigger.token").read_text().strip()
        cls.plugin = load_plugin()
        cls.plugin.LOG_PATH = cls.work/"plugin.log"
        cls.raw = png_bytes()
        cls.sha = hashlib.sha256(cls.raw).hexdigest()

    @classmethod
    def tearDownClass(cls):
        for proc in cls.procs:
            proc.terminate()
            proc.wait(timeout=5)
        for handle in cls.handles:
            handle.close()
        cls.temp.cleanup()

    def request(self, method, route, raw=None, headers=None, auth=True):
        header = {"X-Router-Token": self.token} if auth else {}
        header.update(headers or {})
        request = urllib.request.Request(self.base+route, data=raw, method=method, headers=header)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read(), dict(error.headers)
            finally:
                error.close()

    def upload(self):
        status, raw, _ = self.request("POST", "/attachments", self.raw, {"X-Attachment-SHA256": self.sha})
        self.assertEqual(status, 201, raw)
        return json.loads(raw)["attachment"]

    def receive(self, event, cache=None, **kwargs):
        return self.plugin.receive_attachments(event, base_url=self.base,
            token_path=self.work/"router.token", cache_dir=cache or self.work/"receiver-cache", **kwargs)

    def event(self, record):
        return {"id": 1, "source": "router", "actor":"OguriCap", "level":"notify",
                "text":"image test", "payload":{"attachments":[record]}}

    def test_upload_download_original_bytes_and_auth(self):
        record = self.upload()
        status, data, headers = self.request("GET", record["path"])
        self.assertEqual((status, data), (200,self.raw))
        self.assertEqual(headers["X-Attachment-SHA256"], self.sha)
        self.assertEqual(self.request("GET", record["path"], auth=False)[0],401)
        self.assertEqual(self.request("POST","/attachments",self.raw,auth=False)[0],401)

    def test_worker_http_trigger_receiver_closed_loop_without_source_file(self):
        source = self.work/"sender"/"test.png"; source.parent.mkdir(exist_ok=True); source.write_bytes(self.raw)
        options = {"imagePath":str(source), "messageId":"d4ce4b62-576b-4e58-a0ea-168b72928d58",
                   "worker":"ImageWorker","url":self.base,"token":self.token,
                   "text":"附件完整性验收","urgency":"normal"}
        script = ("import {sendRouterImage} from "+json.dumps((ROOT/"worker/codexworker/router-attachments.mjs").as_uri())+";"
                  +"console.log(JSON.stringify(await sendRouterImage("+json.dumps(options)+")));")
        result = subprocess.run([NODE,"--input-type=module","-e",script],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(result.stdout)
        self.assertTrue(receipt["ok"])
        source.unlink()
        status, pending = harness.trigger_http("GET",self.trigger+"/events?subscriber=hermes-attachment-test&since=0",
                                               token=self.trigger_token)
        self.assertEqual(status,200)
        event = next(e for e in pending["events"] if e["event_id"]==receipt["event_id"])
        self.assertNotIn(str(source), json.dumps(event))
        self.assertEqual(event["level"],"notify")
        accepted=self.receive(event)
        local=Path(accepted["_attachment_files"][0]["path"])
        self.assertEqual(local.read_bytes(),self.raw)
        self.assertNotEqual(local,source)
        self.assertIn(str(local),self.plugin._format_event(accepted))
        # Same UUID/body is one trigger event even on a repeated notice.
        status, again=harness.http("POST",self.base+"/notice",{"worker":"ImageWorker",
            "text":"附件完整性验收","kind":"worker.image","urgency":"normal",
            "message_id":options["messageId"],"attachments":[{"id":self.sha,"name":"test.png"}]},self.token)
        self.assertEqual(status,200)
        self.assertEqual(again["event_id"],receipt["event_id"])
        self.assertTrue(again.get("duplicate"))

    def test_upload_rejects_bad_digest_type_size_and_unknown_reference(self):
        for data, sha in [(self.raw,"0"*64),(b"not an image",hashlib.sha256(b"not an image").hexdigest())]:
            self.assertEqual(self.request("POST","/attachments",data,{"X-Attachment-SHA256":sha})[0],400)
        self.assertEqual(self.request("POST","/attachments",b"",{"Content-Length":str(MAX_ATTACHMENT_BYTES+1),
                                                               "X-Attachment-SHA256":self.sha})[0],400)
        status,_=harness.http("POST",self.base+"/notice",{"worker":"ImageWorker","text":"bad",
                              "attachments":[{"id":"0"*64}]},self.token)
        self.assertEqual(status,400)
        self.assertEqual(self.request("GET","/attachments/not-an-id")[0],400)
        self.assertFalse(list((self.work/"router.db.attachments").glob(".upload-*")))

    def test_receiver_rejects_foreign_urls_ids_and_oversize(self):
        for edits in [{"path":"http://evil.invalid/image.png"},{"id":"../secret"},
                      {"size":MAX_ATTACHMENT_BYTES+1},{"sha256":"0"*64}]:
            record=self.upload();record.update(edits)
            with self.assertRaises(ValueError):
                self.receive(self.event(record))

    def test_receiver_repairs_corrupt_cache_and_reuses_verified_cache(self):
        event=self.event(self.upload()); accepted=self.receive(event)
        file=Path(accepted["_attachment_files"][0]["path"])
        file.write_bytes(b"broken")
        self.receive(event)
        self.assertEqual(file.read_bytes(),self.raw)
        with patch.object(self.plugin.receive_attachments.__globals__["urllib"].request,"build_opener",
                          side_effect=AssertionError("cache should not need network")):
            # Give a no-network opener; its open must never run for a verified cache.
            class NoNetwork:
                def open(self,*a,**k): raise AssertionError("network")
            self.receive(event,opener=NoNetwork())

    def test_receiver_hash_mismatch_leaves_no_partial_file(self):
        class Response(io.BytesIO):
            status=200
            def __init__(self,raw):
                super().__init__(b"x"*len(raw))
                self.headers={"Content-Length":str(len(raw)),"Content-Type":"image/png"}
        class Opener:
            def open(inner,*args,**kwargs): return Response(self.raw)
        cache=self.work/"bad-receiver"
        with self.assertRaisesRegex(ValueError,"SHA256 mismatch"):
            self.receive(self.event(self.upload()),cache=cache,opener=Opener())
        self.assertEqual(list(cache.iterdir()),[])

    def test_binary_attachment_is_not_limited_by_json_body_ceiling(self):
        raw=self.raw + bytes(1_100_000)
        sha=hashlib.sha256(raw).hexdigest()
        status,receipt,_=self.request("POST","/attachments",raw,{"X-Attachment-SHA256":sha})
        self.assertEqual(status,201,receipt)
        record=json.loads(receipt)["attachment"]
        accepted=self.receive(self.event(record))
        self.assertEqual(Path(accepted["_attachment_files"][0]["path"]).read_bytes(),raw)

    def test_download_redirect_is_refused_without_leaking_auth(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        requests=[]
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(inner):
                requests.append(inner.path)
                inner.send_response(302)
                inner.send_header("Location","/unexpected-redirect")
                inner.send_header("Content-Length","0")
                inner.end_headers()
        server=ThreadingHTTPServer(("127.0.0.1",0),Redirect)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            event=self.event(self.upload())
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.plugin.receive_attachments(event,base_url="http://127.0.0.1:"+str(server.server_port),
                    token_path=self.work/"router.token",cache_dir=self.work/"redirect-cache")
            caught.exception.close()
            self.assertEqual(caught.exception.code,302)
            self.assertEqual(requests,["/attachments/"+self.sha])
        finally:
            server.shutdown();server.server_close();thread.join(2)

    def test_source_path_cannot_be_used_as_attachment(self):
        event=self.event(self.upload())
        event["payload"]["attachments"]=[{"image_path":"/tmp/source.png"}]
        with self.assertRaises(ValueError):
            self.receive(event)

    def test_native_message_contains_recipient_attachment(self):
        hermes=Path(os.environ.get("HERMES_SOURCE",str(Path.home()/".hermes/hermes-agent")))
        sys.path.insert(0,str(hermes))
        import hermes_bootstrap
        from gateway.platforms.event import MessageType
        from gateway.session_envelope import _media_clause
        event=self.receive(self.event(self.upload()))
        native=self.plugin._build_wake_event("attachment test",False,None,[event])
        self.assertEqual(native.message_type,MessageType.PHOTO)
        self.assertEqual(native.media_types,["image/png"])
        self.assertEqual(Path(native.media_urls[0]).read_bytes(),self.raw)
        self.assertEqual(native.metadata,{})
        self.assertIsNone(_media_clause(native))
        self.assertFalse(native.allow_gateway_control)

    def test_cold_start_delivers_native_attachment_before_any_human_dispatch(self):
        import asyncio
        from types import SimpleNamespace
        event=self.receive(self.event(self.upload()))
        loop=asyncio.new_event_loop()
        ready=threading.Event();done=threading.Event();received=[]
        class Adapter:
            async def handle_message(inner,native):
                received.append(native);done.set()
        source=SimpleNamespace(chat_id="test-chat")
        gateway=SimpleNamespace(_loop_heartbeat_task=SimpleNamespace(get_loop=lambda:loop),
                                _delivery_adapter_for=lambda source:Adapter())
        state=SimpleNamespace(gateway=gateway,source=source,loop=None,last_wake=0)
        def run():
            asyncio.set_event_loop(loop);loop.call_soon(ready.set);loop.run_forever()
        thread=threading.Thread(target=run,daemon=True);thread.start();ready.wait(2)
        try:
            self.assertTrue(self.plugin._wake(state,[event],internal=True,announce=False))
            self.assertTrue(done.wait(2))
            self.assertIs(state.loop,loop)
            self.assertEqual(Path(received[0].media_urls[0]).read_bytes(),self.raw)
        finally:
            loop.call_soon_threadsafe(loop.stop);thread.join(2);loop.close()

    def test_store_accepts_fragmented_stream_and_cleans_truncation(self):
        store=AttachmentStore(self.work/"fragmented")
        class Fragmented(io.BytesIO):
            def read(self,n=-1): return super().read(min(n,3))
        self.assertEqual(store.put(Fragmented(self.raw),len(self.raw),self.sha)["sha256"],self.sha)
        with self.assertRaisesRegex(ValueError,"incomplete"):
            store.put(io.BytesIO(self.raw[:10]),len(self.raw),self.sha)
        self.assertFalse(list(store.root.glob(".upload-*")))

    def test_duplicate_names_are_display_only(self):
        record=self.upload()
        status,data=harness.http("POST",self.base+"/notice",{"worker":"ImageWorker","text":"names",
            "attachments":[{"id":record["id"],"name":"../../other\nname.png"}]},self.token)
        self.assertEqual(status,200)
        self.assertTrue(data["ok"])


if __name__=="__main__":
    unittest.main(verbosity=2)
