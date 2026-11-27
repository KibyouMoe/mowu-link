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
        self.lose_upload = False

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

    def reply(self, chat, mid, topic, body, key):
        return self.send(mid,"reply",{"parent":mid,"card":body},key)

    def patch(self, mid, body):
        record, = [r for r in self.messages.values() if r[0]==mid]
        record[2] = body
        self.calls.append(("patch",mid))
        return mid

    def upload_image(self,data):
        self.images.append(data)
        if self.lose_upload:
            self.lose_upload = False
            raise ConnectionError("platform stored image, upload acknowledgement lost")
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

    def test_rejection_replies_once_to_the_trainer_input(self):
        self.assertIsNone(self.record("om_bot","sticker",{},sender_type="app"))
        mid = self.record("om_bad","sticker",{})
        with self.assertRaises(Fault):
            self.inbound.deliver(mid)
        self.views.input_notices(self.inbound)
        with self.assertRaises(Fault):
            self.inbound.deliver(mid)
        self.reopen().input_notices(self.inbound)
        (acked,kind,body), = list(self.sink.messages.values())
        self.assertEqual((kind,body["parent"]),("reply",mid))
        self.assertIn("暂时无法读取",body["card"]["elements"][0]["text"]["content"])
        with self.inbound.db.transaction() as db:
            self.assertEqual(tuple(db.execute("SELECT platform_mid,outcome FROM input_notices").fetchone()),(acked,"sent"))
        self.assertEqual(len(self.sink.calls),1)
        self.assertFalse(self.loop.native.entries())

    def test_lost_host_and_platform_receipts_recover_in_order(self):
        mid = self.record("om_lost","text",{"text":"private content"})
        original = self.platform.request
        def lose(*args):
            original(*args)
            raise ConnectionError("private transport text")
        self.platform.request = lose
        with self.assertRaises(ConnectionError):
            self.inbound.deliver(mid)
        self.sink.lose_next = True
        with self.assertLogs(level="WARNING"):
            self.views.input_notices(self.inbound)
        self.platform.request = original
        self.inbound.deliver(mid)
        self.clock.advance(6)
        self.reopen().input_notices(self.inbound)
        self.views.input_notices(self.inbound)
        rows = [r[2] for r in self.sink.messages.values()]
        self.assertEqual([row["parent"] for row in rows],[mid,mid])
        texts = [row["card"]["elements"][0]["text"]["content"] for row in rows]
        self.assertIn("正在核对",texts[0])
        self.assertIn("已确认原会话收到",texts[1])
        self.assertNotIn("private",str(rows))
        self.assertEqual(len(self.loop.native.entries()),1)
        self.assertEqual(len(self.sink.calls),3)

    def test_expired_notice_does_not_block_another_message_or_resend(self):
        mid = self.record("om_before","sticker",{})
        with self.assertRaises(Fault):
            self.inbound.deliver(mid)
        self.sink.lose_next = True
        with self.assertLogs(level="WARNING"):
            self.views.input_notices(self.inbound)
        self.clock.advance(3600)
        later = self.record("om_after","sticker",{})
        with self.assertRaises(Fault):
            self.inbound.deliver(later)
        self.reopen().input_notices(self.inbound)
        with self.inbound.db.transaction() as db:
            self.assertEqual([tuple(r) for r in db.execute("SELECT mid,outcome FROM input_notices ORDER BY seq")],
                             [(mid,"receipt_unknown"),(later,"sent")])
        self.assertEqual(len(self.sink.calls),2)

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

    def test_card_update_retries_original_target_and_body_after_renderer_change(self):
        self.event("part1","progress",text="Before")
        self.assertEqual(self.views.poll(),1)
        self.event("part2","progress",text=" and after")
        patch = self.sink.patch
        writes = []

        def lose(mid,body):
            writes.append((mid,body))
            patch(mid,body)
            raise ConnectionError("platform patch receipt lost")

        self.sink.patch = lose
        with self.assertRaisesRegex(ConnectionError,"patch receipt lost"):
            self.views.poll()
        def confirm(mid,body):
            writes.append((mid,body))
            return patch(mid,body)

        self.sink.patch = confirm
        self.clock.advance(3601)
        resumed = self.reopen()
        resumed.name = "另一显示名"
        self.assertEqual(resumed.poll(),1)
        self.assertEqual(len(writes),2)
        self.assertEqual(writes[0],writes[1])
        self.assertEqual(writes[1][1]["header"]["title"]["content"],"小栗帽-输出")
        self.assertEqual(writes[1][1]["elements"][0]["text"]["content"],"Before and after")
        self.assertEqual(len(self.sink.messages),1)

    def test_after_platform_dedup_window_unknown_is_not_resent(self):
        original = self.event("7","progress",text="Before")
        self.sink.lose_next = True
        with self.assertRaises(ConnectionError):
            self.views.poll()
        same_turn = self.event("8","final",text="Same turn")
        independent = self.event("9","final",text="Independent turn",turn_id="next-turn")
        self.clock.advance(3601)
        resumed = self.reopen()
        with self.assertLogs(level="WARNING"):
            self.assertEqual(resumed.poll(),1)
        self.assertEqual(self.reopen().poll(),0)
        self.assertEqual(self.sink.calls,[("send","om_1"),("send","om_2")])
        self.assertEqual(self.sink.messages[next(reversed(self.sink.messages))][2]["elements"][0]["text"]["content"],
                         "Independent turn")
        with resumed.db.transaction() as db:
            self.assertEqual([tuple(r) for r in db.execute("SELECT seq,root_seq FROM view_receipt_review ORDER BY seq")],
                             [(original["seq"],original["seq"]),(same_turn["seq"],original["seq"])])
            self.assertEqual(db.execute("SELECT cursor FROM view_cursor").fetchone()[0],independent["seq"])
            self.assertIsNone(db.execute("SELECT mid FROM view_attempt WHERE seq=?",(original["seq"],)).fetchone()[0])

    def test_legacy_unconfirmed_card_without_frozen_request_requires_original_sender(self):
        self.event("legacy-card","final",text="Original card")
        self.sink.lose_next = True
        with self.assertRaises(ConnectionError):
            self.views.poll()
        with self.views.db.transaction() as db:
            db.execute("DROP TABLE view_writes")  # shape of an original package's pending record
        with self.assertRaisesRegex(Fault,"legacy_platform_write_unconfirmed"):
            self.reopen().poll()
        self.assertEqual(len(self.sink.calls),1)

    def test_foreign_session_and_raw_tool_arguments_are_rejected(self):
        with self.assertRaises(Fault):
            self.adapter.request("POST","worker/display",{"event_id":"x","session":"other","turn_id":"turn",
                                 "kind":"final","text":"wrong session"})
        with self.assertRaises(Fault):
            self.event("raw","tool",text="preview",tool_name="tool",arguments={"internal":"private"})
        self.assertEqual(self.views.poll(),0)
