import unittest

from . import delivery
from .devloop import Loop
from .orders import Orders
from .storage import guid, timestamp
from .test_mainline import Clock


class Archive(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock))

    def test_business_notice_is_queryable_without_creating_or_acknowledging_a_delivery(self):
        worker, manager = self.loop.worker, self.loop.hermes
        worker.connect()
        worker.poll()
        self.clock.advance(10)
        worker.poll()
        task = manager.create(guid(),"Replay","Exercise notification archive","oguri")
        worker.poll()
        native = self.loop.native.entries()[0][1]["native_id"]
        question = worker.ask(native,"call","Fixture question?",[])
        worker.report(native,task["task_id"],task["assignment_id"],"Fixture output")
        frame, report = self.loop.manager_wire.frames(0)
        results = manager.messages(source="oguri", since=timestamp(self.clock()))["items"]
        self.assertEqual([r["message_id"] for r in results],
                         [frame["payload"]["message_id"],report["payload"]["message_id"]])
        self.assertEqual(results[0]["question_id"],question["question_id"])
        self.assertEqual(results[1]["text"],"Fixture output")
        self.assertEqual(self.loop.manager_wire.frames(0),[frame,report])
        self.assertFalse(self.loop.hermes_native.entries())

    def test_upgrade_indexes_existing_notice_without_fabricating_time(self):
        orders = self.loop.orders
        notice = {"message_id":guid(),"level":"Notify","source":"oguri","text":"prior report",
                  "task_id":guid(),"attachment_ids":[]}
        with orders.db.transaction() as db:
            delivery.enqueue(db,"hermes","message.deliver",notice)
            db.execute("DROP TABLE archive_versions")
        for _ in range(2):
            Orders(orders.db.path)
        item, = self.loop.hermes.messages(message_id=notice["message_id"])["items"]
        self.assertIsNone(item.pop("received_at"))
        self.assertEqual(item,notice)
        self.assertEqual(len(self.loop.manager_wire.frames(0)),1)
