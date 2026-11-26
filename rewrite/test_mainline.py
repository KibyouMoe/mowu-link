import hashlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

from .adapter import HostAdapter
from .devloop import FixtureHost, Loop, agent_report
from .storage import Fault, encode, guid
from .wire import Client
from .worker import Worker


class Clock:
    def __init__(self):
        self.now = 1_800_000_000

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Mainline(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.stack = self.enterContext(ExitStack())
        self.loop = self.stack.enter_context(Loop.isolated(self.clock))
        self.w = self.loop.worker
        self.m = self.loop.hermes

    def create(self, title="First"):
        return self.m.create(guid(), title, "Write a result", "oguri")["task_id"]

    def idle(self):
        self.w.connect()
        self.w.poll()
        self.clock.advance(10)
        self.w.poll()

    def dispatch(self):
        tid = self.create()
        self.idle()
        self.w.poll()
        payload, receipt = self.loop.native.entries()[0]
        return tid, payload["assignment_id"], receipt

    def args(self, tid, aid):
        return {"task_id": tid, "assignment_id": aid, "summary": "Fixture Agent result."}

    def fault(self, code, action):
        with self.assertRaises(Fault) as failure:
            action()
        self.assertEqual(failure.exception.code, code)
        return failure.exception.status

    def test_chat_turn_end_and_active_report(self):
        tid, aid, receipt = self.dispatch()
        self.assertEqual(self.w.hold()["held_assignment_id"], aid)
        chat = self.w.chat("feishu-msg", "private question")
        self.assertEqual(self.w.chat("feishu-msg", "private question"), chat)
        self.w.turn_finished(chat["native_id"], "private answer")
        with self.w.db.transaction() as db:
            self.assertEqual(db.execute("SELECT reply FROM chats").fetchone()[0], "private answer")
        denied = agent_report(self.w, chat["native_id"], self.args(tid, aid))
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["content"][0]["text"], "wrong_report_origin")
        self.w.turn_finished(receipt["native_id"], "a convincing final answer")
        details = self.m.inspect(tid)
        self.assertEqual(details["task"]["state"], "working")
        self.assertIsNone(details["last_report"])
        self.assertFalse(self.loop.manager_wire.frames(0))
        self.fault("not_reported", lambda: self.m.approve(guid(), tid, details["task"]["revision"]))
        result = agent_report(self.w, receipt["native_id"], self.args(tid, aid))
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["release_assignment_id"], aid)
        self.assertEqual(result, agent_report(self.w, receipt["native_id"], self.args(tid, aid)))
        self.assertIsNone(self.w.hold()["held_task_id"])
        self.loop.hermes_native.is_busy = True
        self.assertIsNone(self.m.poll())
        self.loop.hermes_native.is_busy = False
        self.assertIsNone(self.m.poll())
        self.clock.advance(10)
        self.assertIsNotNone(self.m.poll())
        self.assertIsNone(self.m.poll())
        notify = self.loop.hermes_native.entries()
        self.assertEqual([(p["task_id"], p["level"], p["text"]) for p, _ in notify],
                         [(tid, "Notify", self.args(tid, aid)["summary"])])
        details = self.m.inspect(tid)
        self.assertEqual(details["last_report"]["assignment_id"], aid)
        self.assertEqual(details["task"]["state"], "completed_reported")
        key = guid()
        approved = self.m.approve(key, tid, details["task"]["revision"])
        self.assertEqual(approved["task"]["state"], "closed")
        self.assertEqual(self.m.approve(key, tid, details["task"]["revision"]), approved)

    def test_busy_offline_idle_and_one_assignment(self):
        tid = self.create()
        self.assertIsNone(self.m.inspect(tid)["task"]["assignment_id"])
        self.loop.native.is_busy = True
        self.w.connect()
        self.w.poll()
        self.clock.advance(20)
        self.w.poll()
        self.assertIsNone(self.m.inspect(tid)["task"]["assignment_id"])
        self.loop.native.is_busy = False
        self.clock.advance(1)
        self.w.poll()
        self.clock.advance(9)
        self.w.poll()
        self.assertIsNone(self.m.inspect(tid)["task"]["assignment_id"])
        self.clock.advance(1)
        self.w.poll()
        self.assertIsNotNone(self.m.inspect(tid)["task"]["assignment_id"])
        other = self.create("Second")
        self.assertIsNone(self.m.inspect(other)["task"]["assignment_id"])
        self.w.poll()
        self.clock.advance(12)
        self.w.poll()
        self.assertEqual(self.w.hold()["held_task_id"], tid)
        self.assertIsNone(self.m.inspect(other)["task"]["assignment_id"])
        self.assertEqual(len(self.loop.native.entries()), 1)

    def test_transport_ack_is_not_w03_and_w03_is_not_host_receipt(self):
        tid = self.create()
        self.idle()
        frame = next(f for f in self.loop.wire.frames(0) if f["kind"] == "task.dispatch")
        with self.w.db.transaction() as db:
            db.execute("INSERT INTO inbox(delivery_id,frame) VALUES (?,?)", (frame["delivery_id"], json.dumps(frame)))
        self.loop.wire.ack(frame)
        self.assertEqual(self.m.inspect(tid)["task"]["holder"], {"kind": "router"})
        observed = []
        original = self.loop.native.deliver
        def at_delivery(session, did, payload, digest):
            observed.append((self.m.inspect(tid)["task"]["holder"], self.loop.native.lookup(session, did)))
            return original(session, did, payload, digest)
        self.loop.native.deliver = at_delivery
        self.assertTrue(self.w.process(frame))
        self.assertEqual(observed, [({"kind": "worker", "worker_id": "oguri"}, None)])
        for duplicate in (frame, dict(frame, delivery_id=guid())):
            self.assertTrue(self.w.process(duplicate))
        self.assertEqual(len(self.loop.native.entries()), 1)

    def test_dispatch_notice_is_durable_at_w03_while_native_receipt_is_unknown(self):
        tid = self.create("Visible order")
        self.idle()
        original = self.loop.native.deliver
        def lose_receipt(*args):
            original(*args)
            return None
        self.loop.native.deliver = lose_receipt
        self.w.poll()
        self.assertEqual(self.m.inspect(tid)["task"]["state"],"working")
        self.assertEqual(self.w.hold()["held_task_id"],tid)
        with self.w.db.transaction() as db:
            queued, = db.execute("SELECT payload FROM display_events").fetchall()
            self.assertEqual(db.execute("SELECT host_state FROM jobs").fetchone()[0],"unknown")
        notice = json.loads(queued["payload"])
        self.assertIn("溪流派单 · Visible order",notice["text"])
        self.assertIn("已接单",notice["text"])
        self.assertIn(tid,notice["text"])
        self.w.poll()
        self.assertEqual(len(self.loop.native.entries()),1)
        with self.w.db.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM display_events").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT host_state FROM jobs").fetchone()[0],"received")

    def test_worker_rechecks_busy_between_offer_and_accept(self):
        tid = self.create()
        self.idle()
        offered = self.m.inspect(tid)["task"]["assignment_id"]
        self.loop.native.is_busy = True
        self.w.poll()
        self.assertFalse(self.loop.native.entries())
        self.clock.advance(2)
        self.w.poll()
        self.assertIsNone(self.m.inspect(tid)["task"]["assignment_id"])
        self.loop.native.is_busy = False
        self.clock.advance(1)
        self.w.poll()
        self.clock.advance(10)
        self.w.poll()
        self.w.poll()
        p, _ = self.loop.native.entries()[0]
        self.assertNotEqual(p["assignment_id"], offered)
        self.assertEqual(self.w.hold()["held_assignment_id"], p["assignment_id"])

    def test_lease_expires_before_new_dispatch(self):
        self.idle()
        self.clock.advance(31)
        tid = self.create()
        self.assertIsNone(self.m.inspect(tid)["task"]["assignment_id"])
        self.w.poll()
        self.assertIsNone(self.m.inspect(tid)["task"]["assignment_id"])
        self.clock.advance(10)
        self.w.poll()
        self.assertIsNotNone(self.m.inspect(tid)["task"]["assignment_id"])

    def test_concurrent_creates_reserve_one_slot(self):
        self.idle()
        with ThreadPoolExecutor(2) as pool:
            ids = list(pool.map(self.create, ("A", "B")))
        self.assertEqual(sum(bool(self.m.inspect(t)["task"]["assignment_id"]) for t in ids), 1)

    def test_auth_idempotency_and_stale_first_report(self):
        key = guid()
        t = self.m.create(key, "one", "job", "oguri")
        self.assertEqual(t, self.m.create(key, "one", "job", "oguri"))
        self.fault("idempotency_conflict", lambda: self.m.create(key, "two", "job", "oguri"))
        self.fault("forbidden", lambda: self.loop.wire.request("POST", "tasks", {}, guid()))
        self.fault("forbidden", lambda: self.loop.wire.request("PUT", "workers/someone/session", {}, guid()))
        unauth = Client(self.loop.wire.url, guid())
        self.fault("unauthenticated", lambda: unauth.frames(0))
        self.idle()
        self.w.poll()
        p, receipt = self.loop.native.entries()[0]
        aid, tid = p["assignment_id"], t["task_id"]
        body = {"assignment_id": aid, "session_id": self.w.session, "outcome": "completed", "summary": ""}
        self.fault("invalid_summary", lambda: self.loop.wire.request("POST", f"tasks/{tid}/report", body, guid()))
        self.w.report(receipt["native_id"], **self.args(tid, aid))
        body["summary"] = "different late result"
        self.assertEqual(self.fault("stale_assignment", lambda: self.loop.wire.request(
            "POST", f"tasks/{tid}/report", body, guid())), 409)
        self.assertEqual(len(self.loop.manager_wire.frames(0)), 1)

    def test_lost_w04_response_recovered_without_second_notify(self):
        tid, aid, rec = self.dispatch()
        original = self.loop.wire.request
        def drop(method, path, *args, **kwargs):
            reply = original(method, path, *args, **kwargs)
            if path.endswith("/report"):
                raise ConnectionError("dropped after commit")
            return reply
        self.loop.wire.request = drop
        with self.assertRaises(ConnectionError):
            self.w.report(rec["native_id"], **self.args(tid, aid))
        self.assertEqual(self.m.inspect(tid)["task"]["state"], "completed_reported")
        self.assertEqual(self.w.hold()["held_assignment_id"], aid)
        self.loop.wire.request = original
        restored = Worker(self.loop.root / "worker.db", "oguri", self.loop.wire, self.loop.adapter)
        self.assertEqual(restored.connect()["release_assignment_ids"], [aid])
        self.assertIsNone(restored.hold()["held_task_id"])
        self.assertTrue(restored.report(rec["native_id"], **self.args(tid, aid))["recorded"])
        self.assertEqual(len(self.loop.manager_wire.frames(0)), 1)

    def test_lost_w03_response_worker_recovers_from_durable_inbox(self):
        tid = self.create()
        self.idle()
        original = self.loop.wire.request
        def drop(method, path, *args, **kwargs):
            response = original(method, path, *args, **kwargs)
            if path.endswith("/accept"):
                raise ConnectionError("dropped after accept")
            return response
        self.loop.wire.request = drop
        with self.assertRaises(ConnectionError):
            self.w.poll()
        self.assertEqual(self.m.inspect(tid)["task"]["state"], "working")
        self.assertFalse(self.loop.native.entries())
        self.loop.wire.request = original
        restored = Worker(self.loop.root / "worker.db", "oguri", self.loop.wire, self.loop.adapter)
        restored.connect()
        restored.poll()
        restored.poll()
        self.assertEqual(restored.hold()["held_task_id"], tid)
        self.assertEqual(len(self.loop.native.entries()), 1)

    def test_host_commit_lost_receipt_can_recover_or_remain_unknown(self):
        tid = self.create()
        self.idle()
        original = self.loop.native.deliver
        def crash(*args):
            original(*args)
            raise ConnectionError("adapter died before saving native receipt")
        self.loop.native.deliver = crash
        with self.assertRaises(ConnectionError):
            self.w.poll()
        self.assertEqual(self.m.inspect(tid)["task"]["state"], "working")
        self.w.adapter = HostAdapter(self.loop.root / "agent-adapter.db", self.loop.native, self.loop.native.session)
        lookup = self.loop.native.lookup
        self.loop.native.lookup = lambda session, did: None
        self.w.poll()
        native_id = self.loop.native.entries()[0][1]["native_id"]
        with self.w.db.transaction() as db:
            self.assertEqual(db.execute("SELECT host_state FROM jobs").fetchone()[0], "unknown")
        self.fault("unconfirmed_host_input", lambda: self.w.adapter.origin(native_id))
        self.loop.native.lookup = lookup
        self.w.poll()
        self.assertEqual(self.w.adapter.origin(native_id)["task_id"], tid)
        self.assertEqual(len(self.loop.native.entries()), 1)

    def test_worker_uses_replaceable_adapter(self):
        # Only the injected adapter changes; Router and Worker execute the same code.
        class OtherAdapter:
            def __init__(self):
                self.inputs = {}
            def busy(self):
                return False
            def bound_session(self):
                return "other-agent-session"
            def deliver(self, did, payload, *, session=None):
                if session is not None:
                    assert session == self.bound_session()
                native = "alternate/" + did
                self.inputs.setdefault(native, payload)
                return {"native_id":native,"session":self.bound_session(),"delivery_id":did,
                        "digest":hashlib.sha256(encode(payload).encode()).hexdigest()}
            def origin(self, native_id):
                return self.inputs[native_id]
        adapter = OtherAdapter()
        self.w.adapter = adapter
        tid = self.create()
        self.idle()
        self.w.poll()
        chat = self.w.chat("question", "text")
        self.w.turn_finished(chat["native_id"], "answer")
        native, p = next((n, p) for n, p in adapter.inputs.items() if p["kind"] == "task")
        self.assertTrue(self.w.report(native, **self.args(tid, p["assignment_id"]))["recorded"])
        self.assertEqual(self.m.inspect(tid)["task"]["state"], "completed_reported")

    def test_chat_replay_stays_with_original_native_session_after_restart(self):
        complete = self.w.chat("received","already there")
        native_write = self.loop.native.deliver
        def lose_reply(*args):
            native_write(*args)
            return None
        self.loop.native.deliver = lose_reply
        self.assertIsNone(self.w.chat("pending","written but unconfirmed"))
        self.loop.native.deliver = native_write
        with self.w.db.transaction() as db:
            # Simulate pre-upgrade Worker state for rows with and without a known native intent.
            db.execute("INSERT INTO chats(message_id,text) VALUES ('legacy-unknown','old without proof')")
            db.execute("INSERT INTO chats(message_id,text) VALUES ('legacy-proven','old delivery')")
        old_payload = {"kind":"chat","message_id":"legacy-proven","text":"old delivery"}
        self.loop.native.deliver = lose_reply
        self.assertIsNone(self.loop.adapter.deliver("chat-legacy-proven",old_payload))
        self.loop.native.deliver = native_write

        native = FixtureHost(self.loop.root/"other-native.db","other-native-session")
        adapter = HostAdapter(self.loop.root/"other-adapter.db",native,native.session)
        replaced = Worker(self.w.db.path,self.w.worker_id,self.w.router,adapter)
        self.assertEqual(replaced.chat("received","already there"),complete)
        self.fault("chat_conflict",lambda: replaced.chat("received","changed"))
        self.assertIsNone(replaced.chat("pending","written but unconfirmed"))
        self.assertIsNone(replaced.chat("legacy-unknown","old without proof"))
        self.assertIsNone(replaced.chat("legacy-proven","old delivery"))
        self.assertEqual(native.entries(),[])
        self.assertEqual(replaced.chat("fresh","new destination")["session"],native.session)
        self.assertEqual(len(native.entries()),1)

        # A fresh ledger for even the same session must never resubmit an uncertain chat.
        empty_ledger = HostAdapter(self.loop.root/"empty-adapter.db",self.loop.native,self.loop.native.session)
        recovered = Worker(self.w.db.path,self.w.worker_id,self.w.router,empty_ledger)
        self.assertIsNone(recovered.chat("pending","written but unconfirmed"))

        for mid, body in (("pending","written but unconfirmed"),("legacy-proven","old delivery")):
            proof = self.w.chat(mid,body)
            self.assertEqual(proof["session"],self.loop.native.session)
            self.assertEqual(replaced.chat(mid,body),proof)
        self.assertEqual(len(self.loop.native.entries()),3)
        self.assertEqual(len(native.entries()),1)

    def test_wrong_native_session_receipt_stays_unconfirmed(self):
        tid = self.create()
        self.idle()
        original = self.loop.native.deliver
        def wrong_session(*args):
            return dict(original(*args), session="unrelated-session")
        self.loop.native.deliver = wrong_session
        self.fault("host_receipt_mismatch", self.w.poll)
        p, receipt = self.loop.native.entries()[0]
        self.fault("unconfirmed_host_input", lambda: self.w.adapter.origin(receipt["native_id"]))
        self.assertIsNone(self.m.inspect(tid)["last_report"])
        self.w.poll()
        self.assertEqual(self.w.adapter.origin(receipt["native_id"]), p)
        self.assertEqual(len(self.loop.native.entries()), 1)

    def test_delivery_cursor_waits_for_contiguous_acks(self):
        self.idle()
        self.create()
        self.clock.advance(1)
        frames = self.loop.wire.frames(0)
        first, second = frames[-2:]
        self.assertEqual(first["kind"], "task.dispatch")
        self.assertEqual(second["kind"], "worker.status_query")
        self.fault("delivery_not_found", lambda: self.loop.manager_wire.ack(second))
        self.assertLess(self.loop.wire.ack(second)["confirmed_cursor"], first["cursor"])
        self.fault("unconfirmed_cursor", lambda: self.loop.wire.frames(second["cursor"]))
        self.assertEqual(self.loop.wire.ack(first)["confirmed_cursor"], second["cursor"])

    def test_presence_retry_replays_same_sample(self):
        self.w.connect()
        requests = []
        original = self.loop.wire.request
        def drop(method, path, *args, **kwargs):
            reply = original(method, path, *args, **kwargs)
            if path.endswith("/presence"):
                requests.append(args)
                if len(requests) == 1:
                    raise ConnectionError("lost presence ack")
            return reply
        self.loop.wire.request = drop
        with self.assertRaises(ConnectionError):
            self.w.poll()
        self.loop.native.is_busy = True
        self.w.poll()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.clock.advance(1)
        self.w.poll()
        self.assertTrue(requests[-1][0]["busy"])


if __name__ == "__main__":
    unittest.main()
