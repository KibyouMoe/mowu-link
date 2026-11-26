import hashlib
from threading import Thread
import unittest

from . import peer_retry
from .adapter import HostAdapter
from .devloop import FixtureHost, Loop
from .native_files import NativeFiles
from .storage import Fault, guid
from .test_mainline import Clock
from .worker import Worker
from .wire import Client
from .worker_service import WorkerService


class Peers(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.loop = self.enterContext(Loop.isolated(self.clock,extra_workers=["tama","other"]))
        self.a = self.loop.worker
        root = self.loop.root
        self.host = FixtureHost(root/"other-host.db","other-session")
        self.b = Worker(root/"other-worker.db","tama",self.loop.extra_clients["tama"],
                        HostAdapter(root/"other-adapter.db",self.host,self.host.session),clock=self.clock)
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

    def adapter_client(self,w):
        token = guid()
        srv = self.enterContext(WorkerService(w).listen({token:"adapter/"+w.worker_id},("127.0.0.1",0),None))
        thread = Thread(target=srv.serve_forever,kwargs={"poll_interval":.02})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(srv.shutdown)
        return Client("http://127.0.0.1:"+str(srv.server_port),token)

    def test_sender_distinguishes_router_worker_and_host_even_after_receipt_loss(self):
        mid = guid()
        self.a.send_peer(mid,"tama","请确认收件")
        sender = self.adapter_client(self.a)
        path = "worker/peers/"+mid
        accepted = sender.request("GET",path)
        self.assertFalse(accepted["worker_received"])
        self.assertIsNone(accepted["host_receipt_id"])
        route = "peers/messages/"+mid
        with self.assertRaisesRegex(Fault,"peer_message_not_found"):
            self.loop.extra_clients["other"].request("GET",route)
        with self.assertRaisesRegex(Fault,"forbidden"):
            self.loop.manager_wire.request("GET",route)
        original_deliver = self.b.adapter.deliver
        def lost_native_reply(*args,**kwargs):
            original_deliver(*args,**kwargs)
            return None
        self.b.adapter.deliver = lost_native_reply
        self.b.poll()
        only_worker = sender.request("GET",path)
        self.assertTrue(only_worker["worker_received"])
        self.assertIsNone(only_worker["host_receipt_id"])
        fake = {"session_id":self.a.session,"delivery_id":only_worker["delivery_id"],"receipt_id":guid()}
        with self.assertRaisesRegex(Fault,"wrong_peer_recipient"):
            self.a.router.request("POST",route+"/receipt",fake,guid())
        with self.assertRaisesRegex(Fault,"peer_delivery_unconfirmed"):
            self.b.router.request("POST",route+"/receipt",{**fake,"session_id":self.b.session,"delivery_id":guid()},guid())
        self.b.adapter.deliver = original_deliver
        original_request = self.b.router.request
        def lose(method,endpoint,*args,**kw):
            result = original_request(method,endpoint,*args,**kw)
            if endpoint==route+"/receipt":
                raise ConnectionError("peer proof accepted, response lost")
            return result
        self.b.router.request = lose
        self.clock.advance(5)
        self.b.poll()
        observed = sender.request("GET",path)
        self.assertTrue(observed["host_receipt_id"])
        self.assertEqual(observed["accepted_at"],accepted["accepted_at"])
        self.b.router.request = original_request
        self.b = Worker(self.loop.root/"other-worker.db","tama",self.b.router,self.b.adapter,clock=self.clock)
        self.clock.advance(5)
        self.b.poll()
        self.assertEqual(sender.request("GET",path),observed)
        self.assertEqual(len(self.host.entries()),1)
        with self.b.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM peer_receipt_acks").fetchone()[0],1)
        with self.assertRaisesRegex(Fault,"peer_receipt_conflict"):
            self.b.router.request("POST",route+"/receipt",{**fake,"session_id":self.b.session},guid())
        self.assertFalse(self.loop.hermes.tasks()["items"])

    def test_only_intended_peer_receives_bytes_after_both_transfers_are_interrupted(self):
        sender,recipient = self.adapter_client(self.a),self.adapter_client(self.b)
        cache = self.loop.root/"recipient-cache"
        self.b.adapter.media_store = NativeFiles(cache,recipient)
        content = ("两个工位独立保存的文件。\n"*16000).encode()
        sha = hashlib.sha256(content).hexdigest()
        status = sender.request("POST","worker/media/uploads",{"name":"交接.md","sha256":sha,"size_bytes":len(content)},guid())
        chunk = 256*1024
        for pos in range(0,len(content),chunk):
            status = sender.request("PATCH","worker/media/uploads/"+status["upload_id"],content[pos:pos+chunk],
                                    headers={"Upload-Offset":str(pos)})
        local_id = status["attachment_id"]
        msg = {"message_id":guid(),"to_worker":"tama","text":"","attachment_ids":[local_id]}
        original = self.a.router.request
        patches = []
        def lose(*args,**kwargs):
            reply = original(*args,**kwargs)
            if args[0]=="PATCH":
                patches.append(int(kwargs["headers"]["Upload-Offset"]))
                if len(patches)==1:
                    raise Fault(503,"fixture_lost_chunk_ack")
            return reply
        self.a.router.request = lose
        with self.assertRaisesRegex(Fault,"fixture_lost_chunk_ack"):
            sender.request("POST","worker/peer",msg)
        self.assertEqual(sender.request("POST","worker/peer",msg),sender.request("POST","worker/peer",msg))
        self.assertEqual(patches,list(range(0,len(content),chunk)))
        wire = self.b.router.request
        ranges = []
        def interrupt(*args,**kwargs):
            if args[1].startswith("attachments/"):
                ranges.append(kwargs["headers"]["Range"])
                if len(ranges)==2:
                    raise ConnectionError("fixture receiver disconnected")
            return wire(*args,**kwargs)
        self.b.router.request = interrupt
        self.b.poll()
        self.assertFalse(self.host.entries())
        self.b = Worker(self.loop.root/"other-worker.db","tama",self.b.router,self.b.adapter,clock=self.clock)
        self.clock.advance(5)
        self.b.poll()
        self.b.poll()
        self.assertEqual(ranges[1],ranges[2])
        self.assertEqual(len(self.host.entries()),1)
        native,proof = self.host.entries()[0]
        self.assertEqual((native["kind"],native["attachments"][0]["kind"],proof["media_sha256"]),("peer","file",[sha]))
        self.assertEqual((cache/sha).read_bytes(),content)
        self.assertFalse(self.b.hold()["held_task_id"])
        remote_id = self.b.peer_history()[0]["body"]["attachments"][0]["attachment_id"]
        self.assertNotEqual(remote_id,local_id)
        with self.assertRaisesRegex(Fault,"attachment_not_found"):
            self.loop.manager_wire.request("GET","attachments/"+remote_id)
        with self.assertRaisesRegex(Fault,"peer_file_not_owned"):
            self.b.router.request("POST","peers/oguri/messages",{"session_id":self.b.session,"message_id":guid(),
                                  "text":"","attachment_ids":[remote_id]},guid())
        with self.assertRaisesRegex(Fault,"peer_message_conflict"):
            sender.request("POST","worker/peer",{**msg,"text":"changed"})

    def test_empty_and_unauthorized_files_get_no_phantom_or_partial_delivery(self):
        sender,recipient = self.adapter_client(self.a),self.adapter_client(self.b)
        self.b.adapter.media_store = NativeFiles(self.loop.root/"cache",recipient)
        empty = sender.request("POST","worker/media/uploads",{"name":"empty","size_bytes":0,
                     "sha256":hashlib.sha256(b"").hexdigest()},guid())["attachment_id"]
        mid = guid()
        with self.assertRaisesRegex(Fault,"attachment_not_found"):
            sender.request("POST","worker/peer",{"message_id":mid,"to_worker":"tama","attachment_ids":[empty,guid()]})
        self.assertFalse(self.a.peer_history())
        sender.request("POST","worker/peer",{"message_id":mid,"to_worker":"tama","attachment_ids":[empty]})
        wire = self.b.router.request
        reads = []
        def observe(method,route,*args,**kwargs):
            if route.startswith("attachments/"):
                reads.append((method,kwargs))
            return wire(method,route,*args,**kwargs)
        self.b.router.request = observe
        self.b.poll()
        self.assertEqual(reads,[("GET",{"max_bytes":0})])
        (body,receipt), = self.host.entries()
        self.assertEqual(receipt["media_sha256"],[hashlib.sha256(b"").hexdigest()])
        self.assertEqual(body["attachments"][0]["size_bytes"],0)

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

    def test_unknown_peer_does_not_block_other_chat_or_formal_dispatch(self):
        failed,healthy = guid(),guid()
        self.a.send_peer(failed,"tama","first")
        self.a.send_peer(healthy,"tama","second")
        tid = self.loop.hermes.create(guid(),"Independent dispatch","Continue working","tama")["task_id"]
        receive = self.b.adapter.deliver
        lookup = self.b.adapter.lookup_delivery
        calls = []
        def inconsistent(receipt,did,payload):
            if payload.get("message_id")==failed:
                calls.append(did)
                return {**receipt,"session":"different-session"}
            return receipt
        self.b.adapter.deliver = lambda did,payload,**kwargs:inconsistent(receive(did,payload,**kwargs),did,payload)
        self.b.adapter.lookup_delivery = lambda did,payload,**kwargs:inconsistent(lookup(did,payload,**kwargs),did,payload)
        recipient = self.adapter_client(self.b)
        sender = self.adapter_client(self.a)
        path = "worker/peers/"+failed
        self.b.poll()
        observed = recipient.request("GET",path)
        self.assertIsNone(observed["host_receipt_id"])
        pending, = observed["local_pending"]
        self.assertEqual((pending["phase"],pending["attempts"],pending["failure"]),("delivery",1,"peer_host_unconfirmed"))
        self.assertNotIn("local_pending",sender.request("GET",path))
        self.assertTrue(sender.request("GET","worker/peers/"+healthy)["host_receipt_id"])
        self.b.poll()
        self.assertEqual(len(calls),1)
        self.clock.advance(10)
        self.b.poll()
        self.b.poll()
        self.assertEqual(self.b.hold()["held_task_id"],tid)
        self.assertEqual(len(calls),2)
        self.b.adapter.deliver = receive
        self.b.adapter.lookup_delivery = lookup
        self.b = Worker(self.loop.root/"other-worker.db","tama",self.b.router,self.b.adapter,clock=self.clock)
        self.b.poll()
        self.assertIsNone(sender.request("GET",path)["host_receipt_id"])
        self.clock.advance(10)
        self.b.poll()
        self.assertTrue(sender.request("GET",path)["host_receipt_id"])
        self.assertEqual(recipient.request("GET",path)["local_pending"],[])
        self.assertEqual([p["kind"] for p,_ in self.host.entries()],["peer","peer","task"])
        self.assertEqual(self.loop.hermes.inspect(tid)["task"]["state"],"working")

    def test_outgoing_peer_send_recovers_both_sides_of_router_accept_without_a_new_call(self):
        self.a.clock = self.clock
        request = self.a.router.request
        pending = [guid(),guid()]
        rejected = guid()
        with self.assertRaisesRegex(Fault,"peer_not_registered"):
            self.a.send_peer(rejected,"missing","terminal result")
        sends = []
        by_id = "peers/messages/"
        def drop(method,path,*args,**kwargs):
            if method=="POST" and path=="peers/tama/messages":
                mid = args[0]["message_id"]
                sends.append((mid,args[1]))
                if mid==pending[0] and sends.count((mid,"peer-"+mid))==1:
                    request(method,path,*args,**kwargs)
                    raise ConnectionError("response lost after Router commit")
                if mid==pending[1] and sends.count((mid,"peer-"+mid))==1:
                    raise ConnectionError("connection closed before Router commit")
            return request(method,path,*args,**kwargs)
        self.a.router.request = drop
        for mid in pending:
            with self.assertRaises(ConnectionError):
                self.a.send_peer(mid,"tama","keep exact ID "+mid)
        self.assertFalse(self.a.router.request("GET",by_id+pending[0])["worker_received"])
        with self.assertRaisesRegex(Fault,"peer_message_not_found"):
            self.a.router.request("GET",by_id+pending[1])
        self.a = Worker(self.a.db.path,self.a.worker_id,self.a.router,self.a.adapter,clock=self.clock)
        self.a.connect()
        self.clock.advance(4)
        self.a.poll()
        self.assertEqual(len(sends),2)
        self.clock.advance(1)
        self.a.poll()
        self.a.poll()
        self.assertEqual([mid for mid,_key in sends],pending+pending)
        self.assertTrue(all(key=="peer-"+mid for mid,key in sends))
        self.b.poll()
        self.b.poll()
        self.assertEqual([payload["message_id"] for payload,_proof in self.host.entries()],pending)
        self.assertTrue(all(self.a.router.request("GET",by_id+mid)["host_receipt_id"] for mid in pending))
        self.assertEqual([peer_retry.waiting(self.a,mid) for mid in pending],[[],[]])
        self.assertEqual(peer_retry.waiting(self.a,rejected)[0]["phase"],"send_rejected")
        self.assertFalse(self.loop.hermes.tasks()["items"])

    def test_agent_can_read_pending_outgoing_message_without_claiming_acceptance(self):
        self.a.clock = self.clock
        adapter = self.adapter_client(self.a)
        target = self.adapter_client(self.b)
        turn = self.a.chat(guid(),"Send one isolated peer message")
        caller = {"session":turn["session"],"turn_id":turn["turn_id"],"call_id":"reply-call"}
        args = {"to_worker":"tama","text":"Original outgoing body"}
        send = self.a.router.request
        def before_commit(method,path,*more,**options):
            if method=="POST" and path=="peers/tama/messages":
                raise ConnectionError()
            return send(method,path,*more,**options)
        self.a.router.request = before_commit
        result = adapter.request("POST","worker/native-tool",dict(caller,tool="worker_send_peer",arguments=args))
        mid = result["message_id"]
        self.assertEqual((result["status"],result["accepted_at"],result["host_receipt_id"]),("pending",None,None))
        lookup = dict(caller,tool="worker_peer_status",arguments={"message_id":mid})
        self.assertEqual(adapter.request("POST","worker/native-tool",lookup),result)
        with self.assertRaisesRegex(Fault,"peer_message_not_found"):
            target.request("GET","worker/peers/"+mid)
        self.a.router.request = send
        self.clock.advance(5)
        self.a.poll()
        self.b.poll()
        confirmed = adapter.request("POST","worker/native-tool",lookup)
        self.assertEqual(confirmed["status"],"accepted")
        self.assertIsNotNone(confirmed["host_receipt_id"])
        self.assertEqual([body["message_id"] for body,_proof in self.host.entries()],[mid])

    def test_only_registered_peers_see_worker_ids_without_task_ownership(self):
        path = "peers/workers"
        with self.assertRaisesRegex(Fault,"unregistered_worker"):
            self.loop.extra_clients["other"].request("GET",path)
        with self.assertRaisesRegex(Fault,"forbidden"):
            self.loop.manager_wire.request("GET",path)
        with self.assertRaisesRegex(Fault,"forbidden"):
            self.a.router.request("GET","workers")
        other = Worker(self.loop.root/"unavailable-worker.db","other",self.loop.extra_clients["other"],self.a.adapter)
        other.connect()
        self.clock.advance(31)
        self.a.connect()
        self.b.connect()
        first = self.a.router.request("GET",path+"?limit=1")
        self.assertEqual(first["items"],[{"worker_id":"tama","online":True}])
        page = self.a.router.request("GET",path+"?cursor="+first["next_cursor"])
        self.assertEqual(page,{"items":[{"worker_id":"other","online":False}],"next_cursor":None})
        self.assertEqual(self.a.router.request("GET",path+"?online=false"),page)
        self.assertEqual(self.a.router.request("GET",path+"?online=true")["items"],first["items"])
        with self.assertRaisesRegex(Fault,"invalid_peer_filters"):
            self.a.router.request("GET",path+"?worker_id=tama")
        self.assertFalse(self.loop.hermes.tasks()["items"])

    def test_uncertain_peer_receipt_never_writes_to_another_session_or_a_fresh_ledger(self):
        mid = guid()
        self.a.send_peer(mid,"tama","Keep my original receipt")
        status = "peers/messages/"+mid
        original = self.b.adapter
        dispatch = original.deliver
        def lost_after_input(*args,**kwargs):
            dispatch(*args,**kwargs)
            return None
        original.deliver = lost_after_input
        self.b.poll()
        self.assertEqual(len(self.host.entries()),1)
        self.assertIsNone(self.a.router.request("GET",status)["host_receipt_id"])
        original.deliver = dispatch
        other_host = FixtureHost(self.loop.root/"wrong-peer-host.db","wrong-peer-session")
        other = HostAdapter(self.loop.root/"wrong-peer-adapter.db",other_host,other_host.session)
        self.b = Worker(self.b.db.path,"tama",self.b.router,other,clock=self.clock)
        self.clock.advance(5)
        self.b.poll()
        self.assertEqual(other_host.entries(),[])
        self.assertEqual(peer_retry.waiting(self.b,mid)[0]["failure"],"peer_native_session_changed")
        self.b.adapter = HostAdapter(self.loop.root/"fresh-peer-ledger.db",self.host,self.host.session)
        self.clock.advance(10)
        self.b.poll()
        self.assertEqual(len(self.host.entries()),1)
        self.assertIsNone(self.a.router.request("GET",status)["host_receipt_id"])
        self.b.adapter = original
        self.clock.advance(20)
        self.b.poll()
        self.assertTrue(self.a.router.request("GET",status)["host_receipt_id"])
        self.assertEqual(len(self.host.entries()),1)
        self.assertEqual(other_host.entries(),[])

    def test_lost_receipt_does_not_starve_another_confirmation(self):
        first,second = guid(),guid()
        self.a.send_peer(first,"tama","one")
        self.a.send_peer(second,"tama","two")
        request = self.b.router.request
        def fail(method,path,*args,**kwargs):
            if path=="peers/messages/"+first+"/receipt":
                raise Fault(503,"peer_receipt_temporarily_unavailable")
            return request(method,path,*args,**kwargs)
        self.b.router.request = fail
        self.b.poll()
        recipient = self.adapter_client(self.b)
        pending = recipient.request("GET","worker/peers/"+first)
        self.assertIsNone(pending["host_receipt_id"])
        self.assertEqual(pending["local_pending"][0]["phase"],"receipt")
        self.assertTrue(recipient.request("GET","worker/peers/"+second)["host_receipt_id"])
        self.b.router.request = request
        self.clock.advance(5)
        self.b.poll()
        recovered = recipient.request("GET","worker/peers/"+first)
        self.assertTrue(recovered["host_receipt_id"])
        self.assertEqual(recovered["local_pending"],[])
        self.assertEqual(len(self.host.entries()),2)
