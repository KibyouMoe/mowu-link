import unittest
from unittest.mock import patch

from .devloop import Loop
from .storage import guid
from .test_mainline import Clock
from . import delivery


class ManagerQueue(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock))
        self.manager = self.loop.hermes

    def enqueue(self,level):
        mid = guid()
        with self.loop.orders.db.transaction() as db:
            delivery.enqueue(db,"hermes","message.deliver",
                             {"message_id":mid,"level":level,"source":"fixture","text":"for test","attachment_ids":[]})
        return mid

    def test_unknown_event_yields_to_next_event_and_notify_keeps_fifo(self):
        a,b = self.enqueue("Event"),self.enqueue("Event")
        n,m = self.enqueue("Notify"),self.enqueue("Notify")
        host = self.manager.host.host
        send = host.deliver
        calls = []
        def lose(session,did,payload,digest):
            calls.append(did)
            return None if did in (a,n) else send(session,did,payload,digest)
        host.deliver = lose
        with patch("rewrite.hermes.time.time",self.clock):
            self.assertIsNone(self.manager.poll())
            self.assertEqual(self.manager.poll()["delivery_id"],b)
            self.clock.advance(10)
            self.manager.poll()  # Lookup for a cannot prove receipt.
            self.manager.poll()  # The head Notify has an unknown receipt too.
            self.assertIsNone(self.manager.poll())
            self.assertEqual(calls,[a,b,n])
            with self.manager.db.transaction() as db:
                rows = {row["message_id"]:row for row in db.execute("SELECT * FROM queue")}
                self.assertEqual(rows[a]["failure"],"host_receipt_unknown")
                self.assertEqual(rows[n]["failure"],"host_receipt_unknown")
                self.assertIsNone(rows[m]["failure"])
            self.assertEqual(len(host.entries()),1)

    def test_unknown_activity_and_unobserved_turn_reset_the_notify_clock(self):
        n = self.enqueue("Notify")
        observation = [False,1]
        self.manager.host.observe_activity = lambda:tuple(observation)
        self.manager.poll()
        self.clock.advance(11)
        observation[:] = [None,None]
        self.assertIsNone(self.manager.poll())
        observation[:] = [False,2]
        self.manager.poll()
        self.clock.advance(11)
        observation[1] = 3  # A whole turn ran between samples.
        self.assertIsNone(self.manager.poll())
        self.clock.advance(9)
        self.assertIsNone(self.manager.poll())
        self.clock.advance(1)
        self.assertEqual(self.manager.poll()["delivery_id"],n)
