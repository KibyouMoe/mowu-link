import base64
import hashlib
import json
from sqlite3 import OperationalError
from threading import Thread
import unittest
from unittest.mock import Mock

from .devloop import Loop
from .feishu_client import pump_once
from .feishu_inbound import Inbound
from .native_files import NativeFiles
from .storage import Fault, encode
from .wire import Client
from .worker_service import WorkerService


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNQSFjwHwAD5AIge4YO3AAAAABJRU5ErkJggg==")


class PlatformCase(unittest.TestCase):
    def setUp(self):
        self.loop = self.enterContext(Loop.isolated())
        w = self.loop.worker
        self.service = WorkerService(w)
        self.server = self.service.listen({"test-platform":"platform/oguri","test-adapter":"adapter/oguri",
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
    def test_platform_queue_recovers_after_locked_inbox_and_a_malformed_worker_receipt(self):
        mid = self.record("om_transport_retry","text",{"text":"original body"})
        clock = [1.0]
        self.inbound.clock = lambda:clock[0]
        pending = self.inbound.pending
        self.inbound.pending = Mock(side_effect=[OperationalError("isolated lock"),[mid]])
        views,actions = Mock(),Mock()
        views.poll.side_effect = [ValueError("isolated malformed output"),None,None]
        request = self.platform.request
        def malformed_after_write(method,path,*args,**kwargs):
            reply = request(method,path,*args,**kwargs)
            return {} if path=="worker/chat" else reply
        self.platform.request = malformed_after_write
        with self.assertLogs(level="WARNING") as logs:
            pump_once(self.inbound,views,actions)
            self.assertEqual(self.loop.native.entries(),[])
            pump_once(self.inbound,views,actions)
        self.assertTrue(any("host_receipt_unconfirmed" in line for line in logs.output))
        self.assertEqual(views.poll.call_count,2)
        self.assertEqual(actions.flush.call_count,2)
        self.inbound.pending = pending
        self.assertFalse(self.inbound.pending())
        clock[0] += 5
        self.assertEqual(self.inbound.pending(),[mid])
        self.platform.request = request
        pump_once(self.inbound,views,actions)
        self.assertEqual(self.inbound.pending(),[])
        self.assertEqual(actions.flush.call_count,3)
        self.assertEqual([payload["message_id"] for payload,_receipt in self.loop.native.entries()],[mid])

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
        self.assertEqual((meta["kind"],pic[0]["attachments"][0]["kind"]),("file","image"))
        with self.assertRaisesRegex(Fault,"chat_conflict"):
            self.platform.request("POST","worker/chat",{"message_id":"om_doc","text":"",
                "attachment_ids":[meta["attachment_id"]],"attachment_kinds":["image"]})
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
