import socketserver
from sqlite3 import OperationalError
from threading import Event, Thread
import unittest
from unittest.mock import patch

from . import display
from .adapter import HostAdapter
from .devloop import FixtureHost, Loop
from .host_wire import SESSION_HEADER, RemoteAdapter
from .storage import Fault, guid
from .test_mainline import Clock
from .wire import Client, serve_authenticated
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
        outage = self.service.status()
        self.assertEqual(outage["router_retry_in_seconds"],1200)
        self.assertIsNone(outage["host_error"])
        self.assertEqual(outage["host_session"],self.loop.adapter.bound_session())
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
        self.assertIsNone(self.service.status()["router_retry_in_seconds"])
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
        outage = self.service.status()
        self.assertEqual(outage["host_error"],"adapter_connection_unconfirmed")
        self.assertIsNone(outage["host_session"])
        self.assertIsNone(outage["host_busy"])
        self.assertIsNone(outage["native_capabilities"])
        self.assertEqual(outage["held_task_id"],task["task_id"])
        self.assertEqual(self.manager.inspect(task["task_id"])["task"]["state"],"working")
        self.assertEqual(len(self.events("disconnect")),1)
        self.assertIn("适配器",self.events("disconnect")[0]["text"])
        alerts = self.manager.messages(source="oguri")["items"]
        self.assertEqual([m["text"] for m in alerts],["Worker 本体在线，与宿主适配器的连接已中断。"])
        self.worker.adapter = self.loop.adapter
        self.clock.advance(5)
        self.service.step()
        self.assertTrue(self.manager.workers()["items"][0]["adapter_connected"])
        self.assertIsNone(self.service.status()["host_error"])
        self.assertIs(self.service.status()["native_capabilities"]["session_catalog"],False)

    def test_status_does_not_merge_capabilities_and_activity_from_different_sessions(self):
        session = self.loop.adapter.bound_session()
        token = guid()
        def handle(_actor,_method,path,_body,headers):
            if path=="/v1/host/session":
                return 200,"application/json",{"session":session,"session_catalog":True}
            if path=="/v1/host/bound/activity" and headers.get(SESSION_HEADER)==session:
                raise Fault(409,"wrong_native_session")
            return 200,"application/json",{"busy":False}
        server = self.enterContext(serve_authenticated({token:"worker/oguri"},("127.0.0.1",0),None,handle))
        thread = Thread(target=server.serve_forever,kwargs={"poll_interval":0.01},daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.worker.adapter = RemoteAdapter(Client("http://127.0.0.1:"+str(server.server_port),token))
        self.clock.advance(5)
        self.service.step()
        status = self.service.status()
        self.assertTrue(status["router_connected"])
        self.assertFalse(status["adapter_connected"])
        self.assertEqual(status["host_error"],"wrong_native_session")
        self.assertIsNone(status["host_session"])
        self.assertIsNone(status["host_busy"])
        self.assertIsNone(status["native_capabilities"])
        query = guid()
        with self.assertRaisesRegex(Fault,"wrong_native_session"):
            self.worker.observe(query)
        with self.worker.db.transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM samples WHERE query_id=?",(query,)).fetchone())

    def test_bad_wire_observation_leaves_host_unknown_and_recovers_without_reconnect(self):
        session = self.loop.adapter.bound_session()
        original_router = self.worker.router.request
        bad = {"/v1/host/session":b"<html>not a receipt</html>",
               "/v1/host/activity":b'{"busy":false}',
               "/v1/deliveries/stream":b'data: {"delivery_id":\n'}
        def handle(_actor,_method,path,_body,_headers):
            headers = "text/event-stream" if "stream" in path else "application/json"
            return 200,headers,bad[path]
        token = guid()
        server = self.enterContext(serve_authenticated({token:"worker/oguri"},("127.0.0.1",0),None,handle))
        thread = Thread(target=server.serve_forever,kwargs={"poll_interval":0.01},daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        damaged = Client("http://127.0.0.1:"+str(server.server_port),token)
        self.worker.adapter = RemoteAdapter(damaged)
        for body,error in ((bad["/v1/host/session"],"invalid_wire_reply"),
                           (b'{"capabilities":[]}',"invalid_host_observation"),(b'[]',"invalid_host_observation")):
            bad["/v1/host/session"] = body
            self.clock.advance(5)
            self.service.step()
            status = self.service.status()
            self.assertTrue(status["router_connected"])
            self.assertFalse(status["adapter_connected"])
            self.assertEqual(status["host_error"],error)
            self.assertIsNone(status["host_session"])
            self.assertIsNone(status["host_busy"])
        self.worker.adapter = self.loop.adapter
        self.clock.advance(5)
        with patch.object(self.worker.router,"request",
                          side_effect=lambda method,path,*args,**kwargs:
                          damaged.request(method,path,*args,**kwargs) if path=="deliveries/stream"
                          else original_router(method,path,*args,**kwargs)):
            self.service.step()
            self.assertEqual(self.service.status()["last_error"],"invalid_wire_reply")
            self.assertEqual(self.service.status()["host_session"],session)
        self.service.step()
        self.assertIsNone(self.service.status()["last_error"])
        self.clock.advance(10)
        self.service.step()
        self.dispatched()
        self.assertEqual(len(self.loop.native.entries()),1)

    def test_storage_error_does_not_end_worker_pump(self):
        task = self.manager.create(guid(),"Storage recovery","Repeat the same event","oguri")
        first_wait, continue_poll, recovered = Event(), Event(), Event()
        request = self.worker.poll
        def before_poll():
            if not first_wait.is_set():
                raise OperationalError("database is locked")
            request()
            recovered.set()
        def wait(_seconds):
            first_wait.set()
            continue_poll.wait(6)
        with patch.object(self.worker,"poll",side_effect=before_poll),patch.object(self.service.stop,"wait",side_effect=wait):
            self.service.thread.start()
            try:
                self.assertTrue(first_wait.wait(4))
                self.assertTrue(self.service.thread.is_alive())
                self.assertEqual(self.service.status()["last_error"],"worker_storage_unconfirmed")
                self.assertIsNone(self.worker.hold()["held_task_id"])
                continue_poll.set()
                self.assertTrue(recovered.wait(6))
            finally:
                self.service.stop.set()
                continue_poll.set()
                self.service.thread.join(6)
        self.assertFalse(self.service.thread.is_alive())
        self.assertIsNone(self.service.status()["last_error"])
        self.assertEqual(self.worker.hold()["held_task_id"],task["task_id"])
        self.assertEqual(len(self.loop.native.entries()),1)

    def test_task_cards_use_worker_acceptance_independently_of_host_receipts(self):
        unknown = self.manager.create(guid(),"Offline","Do not display as delivered","other")
        self.assertFalse(self.events("task"))
        task = self.dispatched()
        tid = task["task_id"]
        original = self.worker.adapter.deliver
        def disconnected(did,payload,**kwargs):
            if payload["kind"]=="task_update":
                original(did,payload,**kwargs)
                return None
            if payload["kind"]=="task.cancel":
                raise Fault(503,"adapter_connection_unconfirmed",True)
            return original(did,payload,**kwargs)
        self.worker.adapter.deliver = disconnected
        status = self.manager.inspect(tid)["task"]
        updated = self.manager.update(guid(),tid,status["revision"],{"instructions":"Revised"})
        self.service.step()
        self.assertEqual(len(self.events("task")),2)
        self.assertIn("工位已收到工单更新",self.events("task")[-1]["text"])
        self.assertEqual([p["kind"] for p,_ in self.loop.native.entries()],["task","task_update"])
        self.manager.act(guid(),tid,updated["revision"],"cancel",reason="Operator requested")
        self.service.step()
        self.service.step()
        events = self.events("task")
        self.assertEqual(len(events),3)
        self.assertEqual([t in event["text"] for t,event in zip(("Original","Revised","Operator requested"),events)],
                         [True,True,True])
        self.assertTrue(all(tid in event["text"] and unknown["task_id"] not in event["text"] for event in events))
        self.assertEqual(events[2]["session"],"")
        self.assertEqual([p["kind"] for p,_ in self.loop.native.entries()],["task","task_update"])
        self.assertEqual(self.manager.inspect(tid)["task"]["state"],"working")
        self.worker.adapter.deliver = original
        self.clock.advance(5)
        self.service.step()
        self.service.step()
        self.assertEqual(self.events("task"),events)
        self.assertEqual([p["kind"] for p,_ in self.loop.native.entries()],["task","task_update"])
        self.clock.advance(10)
        self.service.step()
        self.service.step()
        controls = [frame["delivery_id"] for frame in self.loop.wire.frames(0) if frame["kind"]=="task.cancel"]
        self.assertEqual(len(controls),2)
        self.assertEqual(self.loop.native.entries()[-1][1]["delivery_id"],controls[-1])
        self.assertEqual([p["kind"] for p,_ in self.loop.native.entries()],["task","task_update","task.cancel"])
        self.assertEqual(self.worker.hold()["held_task_id"],tid)

    def test_written_control_with_missing_receipt_is_never_sent_to_fresh_ledger(self):
        task = self.dispatched()
        tid = task["task_id"]
        original = self.worker.adapter
        original_send = original.deliver
        fresh_host = FixtureHost(self.loop.root/"fresh-controls-host.db",original.bound_session())
        fresh = HostAdapter(self.loop.root/"fresh-controls-adapter.db",fresh_host,fresh_host.session)
        def lost(did,body,**kwargs):
            original_send(did,body,**kwargs)
            return None
        original.deliver = lost
        working = self.manager.inspect(tid)["task"]
        updated = self.manager.update(guid(),tid,working["revision"],{"instructions":"Original update"})
        for kind in ("task_update","task.cancel"):
            if kind=="task.cancel":
                self.manager.act(guid(),tid,updated["revision"],"cancel",reason="Original cancel")
            self.worker.poll()
            entries = self.loop.native.entries()
            self.assertEqual(entries[-1][0]["kind"],kind)
            self.worker.adapter = fresh
            self.worker.poll()
            self.worker.poll()
            self.assertEqual(fresh_host.entries(),[])
            self.worker.adapter = original
            self.worker.poll()
            self.assertEqual(self.loop.native.entries(),entries)
        self.assertEqual([payload["kind"] for payload,_ in self.loop.native.entries()],["task","task_update","task.cancel"])
        self.assertEqual(self.worker.hold()["held_task_id"],tid)
        self.assertEqual(self.manager.inspect(tid)["task"]["state"],"working")
