import socketserver
from threading import Thread
import unittest

from . import display
from .devloop import Loop
from .host_wire import RemoteAdapter
from .storage import Fault, guid
from .test_mainline import Clock
from .wire import Client
from .worker_service import WorkerService


class LinkHealth(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock))
        self.worker,self.manager = self.loop.worker,self.loop.hermes
        self.service = WorkerService(self.worker,self.clock)
        # Drop real requests without depending on the machine's TCP timeout policy.
        class Dropped(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
        dropping = self.enterContext(socketserver.TCPServer(("127.0.0.1",0),Dropped))
        thread = Thread(target=dropping.serve_forever,kwargs={"poll_interval":0.01},daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(dropping.shutdown)
        self.unavailable = Client("http://127.0.0.1:"+str(dropping.server_address[1]),"unused-local-token")
        self.service.step()
        self.clock.advance(10)
        self.service.step()

    def events(self,kind):
        return [r["event"] for r in display.read(self.worker,"/v1/worker/display")["items"]
                if r["event"]["kind"]==kind]

    def dispatched(self):
        task = self.manager.create(guid(),"Dispatch","Original","oguri")
        self.service.step()
        self.assertEqual(self.worker.hold()["held_task_id"],task["task_id"])
        return task

    def test_online_once_real_router_outage_keeps_host_and_work_then_recovers(self):
        task = self.dispatched()
        self.assertEqual(len(self.events("online")),1)
        self.worker.router = self.unavailable
        self.service.step()
        self.service.step()
        self.assertFalse(self.service.router_connected)
        self.assertTrue(self.service.adapter_connected)
        self.assertEqual(len(self.events("disconnect")),1)
        self.assertIn("Router",self.events("disconnect")[0]["text"])
        self.assertEqual(self.worker.hold()["held_task_id"],task["task_id"])
        observed = self.loop.native.entries()[0][1]
        display.native(self.worker,{"event_id":"ev","session":observed["session"],"turn_id":observed["turn_id"],
                                   "kind":"progress","text":"fixture host still working"})
        self.assertEqual(len(self.events("progress")),1)
        self.worker.router = self.loop.wire
        self.clock.advance(1199)
        self.service.step()
        self.assertFalse(self.service.router_connected)
        self.clock.advance(1)
        self.service.step()
        self.service.step()
        self.assertTrue(self.service.router_connected)
        self.assertEqual(len(self.events("online")),2)
        self.assertEqual(self.manager.inspect(task["task_id"])["task"]["state"],"blocked")
        self.assertIsNone(self.worker.hold()["held_assignment_id"])

    def test_adapter_outage_is_reported_to_router_no_idle_or_false_offline(self):
        task = self.dispatched()
        self.worker.adapter = RemoteAdapter(self.unavailable)
        for _ in range(31):
            self.clock.advance(20)
            self.service.step()
        row, = self.manager.workers(worker_id="oguri")["items"]
        self.assertTrue(row["online"])
        self.assertIsNone(row["busy"])
        self.assertFalse(row["adapter_connected"])
        self.assertTrue(self.service.router_connected)
        self.assertFalse(self.service.adapter_connected)
        self.assertEqual(self.manager.inspect(task["task_id"])["task"]["state"],"working")
        self.assertEqual(len(self.events("disconnect")),1)
        self.assertIn("适配器",self.events("disconnect")[0]["text"])
        alerts = self.manager.messages(source="oguri")["items"]
        self.assertEqual([m["text"] for m in alerts],["Worker 本体在线，与宿主适配器的连接已中断。"])
        self.worker.adapter = self.loop.adapter
        self.clock.advance(5)
        self.service.step()
        self.assertTrue(self.manager.workers()["items"][0]["adapter_connected"])

    def test_task_visibility_waits_for_host_receipt_and_replays_once(self):
        unknown = self.manager.create(guid(),"Offline","Do not display as delivered","other")
        self.assertFalse(self.events("task"))
        task = self.dispatched()
        tid = task["task_id"]
        status = self.manager.inspect(tid)["task"]
        updated = self.manager.update(guid(),tid,status["revision"],{"instructions":"Revised"})
        self.service.step()
        self.manager.act(guid(),tid,updated["revision"],"cancel",reason="Operator requested")
        self.service.step()
        self.service.step()
        events = self.events("task")
        self.assertEqual(len(events),3)
        self.assertEqual([t in event["text"] for t,event in zip(("Original","Revised","Operator requested"),events)],
                         [True,True,True])
        self.assertTrue(all(tid in event["text"] and unknown["task_id"] not in event["text"] for event in events))
        self.assertEqual(self.manager.inspect(tid)["task"]["state"],"working")
