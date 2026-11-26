import unittest

from .adapter import HostAdapter
from .devloop import FixtureHost, Loop
from .storage import Fault, guid
from .test_mainline import Clock
from .worker import Worker


class Peers(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock,extra_workers=["tama"]))
        self.a = self.loop.worker
        root = self.loop.root
        self.host = FixtureHost(root/"other-host.db","other-session")
        self.b = Worker(root/"other-worker.db","tama",self.loop.extra_clients["tama"],
                        HostAdapter(root/"other-adapter.db",self.host,self.host.session))
        self.a.connect()
        self.b.connect()

    def test_original_reply_link_and_formal_isolation(self):
        mid = guid()
        receipt = self.a.send_peer(mid,"tama","hello")
        self.assertEqual(receipt,self.a.send_peer(mid,"tama","hello"))
        self.assertEqual(receipt["status"],"accepted")
        self.assertEqual(self.host.entries(),[])
        self.host.is_busy = True
        self.b.poll()
        payload,native = self.host.entries()[0]
        self.assertEqual((payload["kind"],payload["from_worker"],payload["to_worker"]),("peer","oguri","tama"))
        response = guid()
        self.b.send_peer(response,"oguri","received",mid)
        self.a.poll()
        self.assertEqual(self.loop.native.entries()[0][0]["reply_to"],mid)
        self.assertEqual(self.b.peer_history()[0]["body"]["reply_to"],mid)
        self.assertIsNone(self.b.hold()["held_task_id"])
        self.assertFalse(self.loop.hermes.tasks()["items"])
        with self.assertRaises(Fault) as wrong:
            self.b.report(native["native_id"],guid(),guid(),"not a task")
        self.assertEqual(wrong.exception.code,"wrong_report_origin")

    def test_lost_accept_retries_without_new_host_input(self):
        mid = guid()
        request = self.a.router.request
        def drop(*args):
            reply = request(*args)
            raise ConnectionError("lost accept")
        self.a.router.request = drop
        with self.assertRaises(ConnectionError):
            self.a.send_peer(mid,"tama","hello")
        self.a.router.request = request
        self.a.send_peer(mid,"tama","hello")
        self.b.poll()
        self.assertEqual(len(self.host.entries()),1)
        with self.assertRaises(Fault):
            self.b.send_peer(guid(),"oguri","answer",guid())
        with self.assertRaises(Fault):
            self.a.send_peer(mid,"tama","different")
