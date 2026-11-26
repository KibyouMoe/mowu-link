import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from .devloop import Loop
from .storage import Fault, guid
from .test_mainline import Clock
from .trigger import Trigger


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.trigger = Trigger(self.root / "trigger.db", "test-source", self.clock)
        self.other = Trigger(self.root / "other.db", "test-source", self.clock)
        self.loop = self.enterContext(Loop.isolated(self.clock, trigger_ids=[self.trigger.instance_id,self.other.instance_id]))
        self.router = self.loop.trigger_clients[self.trigger.instance_id]

    def test_isolated_instance_and_three_levels(self):
        self.assertNotEqual(self.trigger.instance_id, self.other.instance_id)
        self.assertEqual(Trigger(self.root / "trigger.db", "test-source").instance_id, self.trigger.instance_id)
        self.loop.hermes_native.is_busy = True
        for level in ("Notify","Info","Event"):
            mid = self.trigger.record(level, "文"*200)
            self.assertEqual(self.trigger.send(self.router, mid)["message_id"], mid)
        self.assertEqual(len(self.loop.manager_wire.frames(0)), 2)
        self.assertEqual(len(self.loop.hermes.messages(level="Info")["items"]), 1)
        self.loop.hermes.poll()
        self.assertEqual([p["level"] for p,_ in self.loop.hermes_native.entries()], ["Event"])
        self.loop.hermes_native.is_busy = False
        self.loop.hermes.poll()
        self.clock.advance(9)
        self.loop.hermes.poll()
        self.clock.advance(1)
        self.loop.hermes.poll()
        self.assertEqual([p["level"] for p,_ in self.loop.hermes_native.entries()], ["Event","Notify"])
        self.assertEqual(self.trigger.pending(), [])

    def test_dropped_chunk_and_final_message_receipts_recover(self):
        path = self.root / "text.txt"
        content = ("文本" * 100000).encode()
        path.write_bytes(content)
        mid = self.trigger.record("Notify", "file", [path])
        path.unlink()  # Restart must use the archived bytes.
        original, offsets = self.router.request, []
        dropped = set()
        def drop(method, route, *args, **kwargs):
            reply = original(method, route, *args, **kwargs)
            if method == "PATCH":
                offsets.append(int(kwargs["headers"]["Upload-Offset"]))
            if (method == "PATCH" or route == "messages") and method not in dropped:
                dropped.add(method)
                raise ConnectionError("ack dropped")
            return reply
        self.router.request = drop
        for _ in range(2):
            with self.assertRaises(ConnectionError):
                self.trigger.send(self.router, mid)
            self.trigger = Trigger(self.root / "trigger.db", "test-source", self.clock)
            self.assertEqual(self.trigger.pending(), [mid])
        receipt = self.trigger.send(self.router, mid)
        self.assertEqual(receipt["message_id"], mid)
        self.assertEqual(offsets.count(0), 1)
        self.assertEqual(self.trigger.pending(), [])
        self.assertEqual(len(self.loop.manager_wire.frames(0)), 1)
        msg, = self.loop.hermes.messages(message_id=mid)["items"]
        file_id, = msg["attachment_ids"]
        response = self.loop.manager_wire.request("GET", "attachments/"+file_id, headers={"Range":"bytes=240000-280000"})
        self.assertEqual((response["status"],response["bytes"]), (206,content[240000:280001]))
        whole = self.loop.manager_wire.request("GET", "attachments/"+file_id)
        self.assertEqual(whole["bytes"],content)
        self.assertEqual(whole["headers"]["X-Content-SHA256"],hashlib.sha256(content).hexdigest())
        with self.assertRaises(Fault):
            self.loop.wire.request("GET", "attachments/"+file_id)

    def test_upload_conflicts_checksums_and_scoped_reads(self):
        data = b"abcde"
        spec = {"name":"x","size_bytes":5,"sha256":hashlib.sha256(data).hexdigest()}
        uid = self.router.request("POST","uploads",spec,guid())["upload_id"]
        url = "uploads/"+uid
        def patch(pos, data):
            return self.router.request("PATCH",url,data,headers={"Upload-Offset":str(pos)})
        first = patch(0,b"ab")
        self.assertFalse(first["complete"])
        self.assertEqual(first,patch(0,b"ab"))
        for pos,chunk in ((0,b"XX"),(4,b"e")):
            with self.assertRaises(Fault) as conflict:
                patch(pos,chunk)
            self.assertEqual(conflict.exception.details["next_offset"],2)
        with self.assertRaises(Fault) as bad:
            patch(2,b"XXX")
        self.assertEqual(bad.exception.code,"digest_mismatch")
        self.assertEqual(self.router.request("GET",url)["next_offset"],2)
        with self.assertRaises(Fault):
            self.loop.trigger_clients[self.other.instance_id].request("GET",url)
        file_id = patch(2,b"cde")["attachment_id"]
        self.assertEqual(self.router.request("GET",url)["attachment_id"],file_id)
        with self.assertRaises(Fault):
            self.loop.manager_wire.request("GET","attachments/"+file_id)

    def test_invalid_messages_never_get_a_receipt_or_attachment_grant(self):
        mid = self.trigger.record("Info","local")
        with self.trigger.db.transaction() as db:
            body = json.loads(db.execute("SELECT body FROM archive").fetchone()[0])
        for changes,expected in (({"text":"字"*201},422),({"attachment_ids":[guid()]},404),
                                ({"instance_id":self.other.instance_id},403)):
            with self.assertRaises(Fault) as failure:
                self.router.request("POST","messages",dict(body,**changes),guid())
            self.assertEqual(failure.exception.status,expected)
        receipt = self.trigger.send(self.router,mid)
        replay = self.router.request("POST","messages",body,guid())
        self.assertTrue(replay["duplicate"])
        self.assertEqual(receipt["received_at"],replay["received_at"])
        with self.assertRaises(Fault) as failure:
            self.router.request("POST","messages",dict(body,text="changed"),guid())
        self.assertEqual(failure.exception.code,"message_conflict")
        self.assertEqual(len(self.loop.hermes.messages()["items"]),1)

    def test_empty_text_image_and_database_remain_exact(self):
        paths = [self.root / n for n in ("empty","image.pgm","data.db")]
        paths[0].write_bytes(b"")
        paths[1].write_bytes(b"P5\n1 1\n255\n\x7f")
        with closing(sqlite3.connect(paths[2])) as db, db:
            db.execute("CREATE TABLE fixture(n)")
            db.execute("INSERT INTO fixture VALUES (42)")
        mid = self.trigger.record("Event","files",paths)
        self.trigger.send(self.router,mid)
        refs = self.loop.hermes.messages(message_id=mid)["items"][0]["attachment_ids"]
        self.assertEqual([self.loop.manager_wire.request("GET","attachments/"+i)["bytes"] for i in refs],
                         [p.read_bytes() for p in paths])
