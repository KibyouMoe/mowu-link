import base64
import hashlib
import json
from threading import Thread
import unittest

from .devloop import Loop
from .feishu_inbound import Inbound
from .native_files import NativeFiles
from .storage import Fault, encode
from .wire import Client
from .worker_service import WorkerService


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6yX8AAAAASUVORK5CYII=")


class PlatformCase(unittest.TestCase):
    def setUp(self):
        self.loop = self.enterContext(Loop.isolated())
        w = self.loop.worker
        self.server = WorkerService(w).listen({"test-platform":"platform/oguri","test-adapter":"adapter/oguri",
                  "other":"adapter/other"},("127.0.0.1",0),None)
        thread = Thread(target=self.server.serve_forever,kwargs={"poll_interval":0.01},daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        url = "http://127.0.0.1:"+str(self.server.server_port)
        self.platform,self.adapter,self.other = [Client(url,key) for key in ("test-platform","test-adapter","other")]
        self.loop.adapter.media_store = NativeFiles(self.loop.root/"cache",self.adapter)
        self.data = {("om_post","img_one","image"):PNG,("om_pic","img_one","image"):PNG,
                     ("om_doc","file_doc","file"):b"# handoff\n"*40000}
        self.reads = []
        def fetch(mid,key,kind,limit):
            self.reads.append((mid,key,kind))
            return self.data[mid,key,kind]
        self.inbound = Inbound(self.loop.root/"feishu.db",self.platform,fetch,"app","trainer","private-chat")

    def record(self, mid, kind, content, sender="trainer", sender_type="user", chat="private-chat"):
        message = {"chat_id":chat,"chat_type":"p2p","message_type":kind,
                   "message_id":mid,"content":json.dumps(content)}
        return self.inbound.record("app",sender_type,sender,message)


class PlatformInput(PlatformCase):
    def test_single_captioned_input_with_image_bytes_and_replay_after_lost_receipt(self):
        caption = "这张图是什么颜色？"
        post = {"zh_cn":{"content":[[{"tag":"text","text":caption},{"tag":"img","image_key":"img_one"}]]}}
        mid = self.record("om_post","post",post)
        self.assertEqual(self.inbound.pending(),[mid])
        original = self.platform.request
        def lose(*args,**kw):
            result = original(*args,**kw)
            if args[1]=="worker/chat":
                raise OSError("lost worker receipt")
            return result
        self.platform.request = lose
        with self.assertRaises(OSError):
            self.inbound.deliver(mid)
        self.platform.request = original
        self.record(mid,"post",post)
        receipt = self.inbound.deliver(mid)
        (body,native), = self.loop.native.entries()
        self.assertEqual(native,receipt)
        self.assertEqual((body["text"],receipt["media_sha256"]), (caption,[hashlib.sha256(PNG).hexdigest()]))
        self.assertEqual((self.loop.root/"cache"/receipt["media_sha256"][0]).read_bytes(),PNG)
        self.assertEqual(self.reads,[(mid,"img_one","image")])
        self.assertFalse(self.inbound.pending())
        self.assertFalse(self.loop.worker.hold()["held_task_id"])
        self.assertFalse(self.loop.hermes.tasks()["items"])

    def test_plain_text_pure_image_and_nonimage_document(self):
        for mid,kind,data in [("om_text","text",{"text":"  原话  "}),("om_pic","image",{"image_key":"img_one"}),
                              ("om_doc","file",{"file_key":"file_doc","file_name":"交接.md.txt"})]:
            self.inbound.deliver(self.record(mid,kind,data))
        text,pic,doc = self.loop.native.entries()
        self.assertEqual(text[0]["text"],"  原话  ")
        self.assertEqual(pic[0]["text"],"")
        meta, = doc[0]["attachments"]
        self.assertEqual(meta["name"],"交接.md.txt")
        self.assertEqual((self.loop.root/"cache"/meta["sha256"]).read_bytes(),self.data["om_doc","file_doc","file"])
        with self.assertRaises(Fault) as denied:
            self.other.request("GET","worker/media/files/"+meta["attachment_id"])
        self.assertEqual(denied.exception.status,403)

    def test_sender_binding_and_unsupported_content_do_not_block_following_input(self):
        self.assertIsNone(self.record("om_bot","text",{"text":"loop"},sender="bot",sender_type="app"))
        self.assertIsNone(self.record("om_other","text",{"text":"other"},sender="else"))
        self.assertIsNone(self.record("om_group","text",{"text":"other"},chat="other-chat"))
        bad = self.record("om_bad","post",{"content":[[{"tag":"unknown"}]]})
        with self.assertRaises(Fault):
            self.inbound.deliver(bad)
        ok = self.record("om_good","text",{"text":"kept"})
        self.assertEqual(self.inbound.pending(),[ok])
        self.inbound.deliver(ok)
        self.assertEqual(len(self.loop.native.entries()),1)

    def test_unavailable_media_host_does_not_acknowledge_metadata_as_an_image(self):
        self.loop.adapter.media_store = None
        mid = self.record("om_pic","image",{"image_key":"img_one"})
        with self.assertRaises(Fault) as failure:
            self.inbound.deliver(mid)
        self.assertEqual(failure.exception.code,"native_media_input_unavailable")
        self.assertFalse(self.loop.native.entries())
        with self.loop.adapter.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intents").fetchone()[0],0)
