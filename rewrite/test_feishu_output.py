import hashlib
import unittest

from .feishu_output import Views
from .storage import Fault
from .test_feishu_inbound import PlatformCase, PNG
from .test_mainline import Clock


class PlatformFixture:
    def __init__(self):
        self.messages = {}
        self.images = []
        self.calls = []
        self.lose_next = False

    def send(self, chat, kind, body, key):
        record = self.messages.setdefault(key,["om_"+str(len(self.messages)+1),kind,body])
        assert record[1:] == [kind,body]
        self.calls.append(("send",record[0]))
        if self.lose_next:
            self.lose_next = False
            raise ConnectionError("platform accepted, acknowledgement lost")
        return record[0]

    def create(self, chat, body, key):
        return self.send(chat,"interactive",body,key)

    def patch(self, mid, body):
        record, = [r for r in self.messages.values() if r[0]==mid]
        record[2] = body
        self.calls.append(("patch",mid))
        return mid

    def upload_image(self,data):
        self.images.append(data)
        return "img_fixture_"+str(len(self.images))


class PlatformOutput(PlatformCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.sink = PlatformFixture()
        self.db = self.loop.root/"views.db"
        self.views = self.reopen()

    def reopen(self):
        return Views(self.db,self.platform,self.sink,"private-chat","小栗帽",self.clock)

    def event(self,eid,kind,**payload):
        body = {"event_id":eid,"session":self.loop.native.session,"turn_id":"turn","kind":kind,**payload}
        return self.adapter.request("POST","worker/display",body)

    def image(self):
        status = self.adapter.request("POST","worker/media/uploads",{"name":"photo.png","size_bytes":len(PNG),
                                      "sha256":hashlib.sha256(PNG).hexdigest()},"fixture-image")
        done = self.adapter.request("PATCH","worker/media/uploads/"+status["upload_id"],PNG,headers={"Upload-Offset":"0"})
        return done["attachment_id"]

    def test_tools_final_and_segments_straddling_an_image(self):
        self.event("1","native_input",text="Look at the picture")
        self.event("2","tool",text="Read the test picture",tool_name="view_image")
        self.event("3","progress",text="Before ")
        self.event("4","progress",text="image")
        self.event("5","image",attachment_id=self.image())
        self.event("6","progress",text="After image")
        final = self.event("7","final",text="Done")
        self.assertEqual(self.event("7","final",text="Done"),final)
        self.assertEqual(self.views.poll(),7)
        self.assertEqual(self.reopen().poll(),0)
        rows = list(self.sink.messages.values())
        self.assertEqual([r[1] for r in rows],["interactive"]*3+["image","interactive","interactive"])
        cards = [row[2] for row in rows if row[1]=="interactive"]
        self.assertEqual([c["header"]["title"]["content"] for c in cards],
                        ["小栗帽·原生输入","小栗帽-工具","小栗帽-输出","小栗帽-输出","小栗帽-回复"])
        self.assertEqual(cards[2]["elements"][0]["text"]["content"],"Before image")
        self.assertEqual(self.sink.images,[PNG])
        self.assertIn(("patch",rows[2][0]),self.sink.calls)
        self.assertFalse(self.loop.hermes.tasks()["items"])

    def test_image_ack_lost_reuses_upload_and_platform_message_key(self):
        self.event("img","image",attachment_id=self.image())
        self.sink.lose_next = True
        with self.assertRaises(ConnectionError):
            self.views.poll()
        self.assertEqual(self.reopen().poll(),1)
        self.assertEqual(len(self.sink.images),1)
        self.assertEqual(len(self.sink.messages),1)

    def test_after_platform_dedup_window_unknown_is_not_resent(self):
        self.event("7","final",text="Done")
        self.sink.lose_next = True
        with self.assertRaises(ConnectionError):
            self.views.poll()
        self.clock.advance(3601)
        with self.assertRaises(Fault) as failure:
            self.reopen().poll()
        self.assertEqual(failure.exception.code,"platform_receipt_unknown")
        self.assertEqual(len(self.sink.calls),1)

    def test_foreign_session_and_raw_tool_arguments_are_rejected(self):
        with self.assertRaises(Fault):
            self.adapter.request("POST","worker/display",{"event_id":"x","session":"other","turn_id":"turn",
                                 "kind":"final","text":"wrong session"})
        with self.assertRaises(Fault):
            self.event("raw","tool",text="preview",tool_name="tool",arguments={"internal":"private"})
        self.assertEqual(self.views.poll(),0)
