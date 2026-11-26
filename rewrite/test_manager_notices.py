import tempfile
from pathlib import Path
import unittest

from .devloop import Loop
from .hermes import HermesAdapter
from .manager_notices import ManagerNotices
from .test_feishu_output import PlatformFixture
from .test_mainline import Clock
from .trigger import Trigger


class ManagerNoticeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = Clock()
        self.trigger = Trigger(self.root/"trigger.db","隔离来源\n",self.clock)
        self.loop = self.enterContext(Loop.isolated(self.clock,trigger_ids=[self.trigger.instance_id]))
        self.sink = PlatformFixture()
        self.notices = ManagerNotices(self.sink,"oc_isolated_manager","fixture-app",self.clock)
        self.loop.hermes.notices = self.notices

    def send(self,level):
        mid = self.trigger.record(level,"只给指定宿主的测试正文")
        self.trigger.send(self.loop.trigger_clients[self.trigger.instance_id],mid)
        return mid

    def test_notice_uses_receipt_time_even_while_notify_waits_for_host_idle(self):
        self.loop.hermes_native.is_busy = True
        mid = self.send("Notify")
        self.send("Info")
        self.assertIsNone(self.loop.hermes.poll())
        self.assertEqual(self.loop.hermes_native.entries(),[])
        self.sink.lose_next = True
        self.assertIsNone(self.notices.flush(self.loop.hermes.db))
        self.assertEqual(len(self.sink.messages),1)
        platform_id,kind,content = next(iter(self.sink.messages.values()))
        self.assertEqual((kind,content),("text",{"text":"已收到 隔离来源 的 Notify 消息。"}))
        self.assertIsNone(self.notices.flush(self.loop.hermes.db))
        self.clock.advance(5)
        restored = HermesAdapter(self.loop.root/"hermes-adapter.db",self.loop.manager_wire,
                                 self.loop.hermes.host,self.clock,notices=self.notices)
        self.assertEqual(self.notices.flush(restored.db),platform_id)
        self.assertEqual(len(self.sink.messages),1)
        self.assertEqual(self.loop.hermes_native.entries(),[])
        self.loop.hermes_native.is_busy = False
        self.assertIsNone(restored.poll())
        self.clock.advance(10)
        self.assertEqual(restored.poll()["delivery_id"],mid)
        self.assertEqual(len(self.loop.hermes_native.entries()),1)
        with restored.db.transaction() as db:
            notices = db.execute("SELECT message_id,platform_id FROM manager_notices").fetchall()
        self.assertEqual([tuple(row) for row in notices],[(mid,platform_id)])

    def test_expired_ambiguous_notice_is_not_retargeted_and_does_not_block_a_later_event(self):
        first = self.send("Event")
        self.assertEqual(self.loop.hermes.poll()["delivery_id"],first)
        self.sink.lose_next = True
        self.assertIsNone(self.notices.flush(self.loop.hermes.db))
        self.clock.advance(55*60)
        another = ManagerNotices(self.sink,"oc_different_chat","fixture-app",self.clock)
        self.assertIsNone(another.flush(self.loop.hermes.db))
        self.assertIsNone(self.notices.flush(self.loop.hermes.db))
        self.assertEqual(len(self.sink.calls),1)
        later = self.send("Event")
        self.assertEqual(self.loop.hermes.poll()["delivery_id"],later)
        self.assertIsNotNone(self.notices.flush(self.loop.hermes.db))
        self.assertEqual(len(self.sink.messages),2)
        with self.loop.hermes.db.transaction() as db:
            row = db.execute("SELECT error,platform_id FROM manager_notices WHERE message_id=?",(first,)).fetchone()
        self.assertEqual(tuple(row),("receipt_unknown",None))
