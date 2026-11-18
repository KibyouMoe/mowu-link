"""Loopback acceptance of mechanical settlement, review, and private deliverables."""

import hashlib
import importlib.util
import sys
import unittest
import uuid
from pathlib import Path

import stream_fixture as layer


class PeerDeliveryTests(unittest.TestCase):
    call = layer.StreamFixture.call
    close_worker = layer.StreamFixture.close_worker
    versioned_stream = layer.StreamFixture.versioned_stream
    stream_event = layer.StreamFixture.stream_event

    def setUp(self):
        layer.StreamFixture.setUp(self)
        self.close_worker()
        self.stream, self.gen = self.versioned_stream()
        self.sender = {"name": self.name, "connection_generation": self.gen}
        self.server.verdict_token = "reviewer-fixture"
        self.context = {"card_id": "card-甲", "board_id": "board-乙"}

    def test_lossless_long_peer_through_reviewer_format_and_task_lookup(self):
        text = "甲🐴，\n " * 10000
        body = {
            "message_id": str(uuid.uuid4()),
            "from": self.name,
            "to": "Hermes",
            "text": text,
            "task_id": "t-correlated",
        }
        self.assertEqual(len(text), 50000)
        code, result = self.call("/messages", body)
        self.assertEqual((code, result["status"]), (200, "accepted"))
        saved = self.call("/messages/" + body["message_id"], method="GET")[1]["message"]
        self.assertEqual(saved["text"], text)
        self.assertTrue(self.call("/messages", body)[1]["duplicate"])
        notices = [n for n in self.sent if n["kind"] == "peer.message"]
        self.assertEqual(len(notices), 25)
        for i, n in enumerate(notices, 1):
            self.assertIn(f"{i}/25 · {body['message_id']} · ", n["text"])
        with self.store._lock:
            sizes = [
                r[0]
                for r in self.store._conn.execute(
                    "SELECT length(data) FROM peer_text_chunks ORDER BY part"
                )
            ]
        self.assertTrue(all(x <= 65536 for x in sizes))
        self.assertEqual(sum(sizes), len(text.encode()))
        plugin_dir = Path(__file__).resolve().parents[1] / "hermes-plugin" / "trigger-bus"
        spec = importlib.util.spec_from_file_location("peer_renderer_test", plugin_dir / "__init__.py")
        renderer = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = renderer
        self.addCleanup(sys.modules.pop, spec.name)
        spec.loader.exec_module(renderer)
        cards = renderer._format_events(notices)
        pos = 0
        for n in notices:
            at = cards.index(n["text"], pos)
            pos = at + len(n["text"])
        pieces = [n["text"].split(body["message_id"] + " · ", 1)[1] for n in notices]
        self.assertEqual("".join(pieces), text)
        self.call(
            "/messages",
            {**body, "message_id": str(uuid.uuid4()), "task_id": "t-another", "text": "other"},
        )
        result = self.call("/messages?task_id=t-correlated", method="GET")[1]
        self.assertEqual([m["message_id"] for m in result["messages"]], [body["message_id"]])
        digest = hashlib.sha256(text.encode()).hexdigest()
        print(
            f"⑫ chars=50000 bytes={len(text.encode())} DB chunks={sizes} rendered=25/25 SHA256={digest}; "
            "⑬ GET /messages?task_id=t-correlated matched=1 unrelated=0"
        )

    def test_partial_root_forward_resumes_before_next_peer(self):
        ids = [str(uuid.uuid4()) for _ in range(2)]
        first = {"message_id": ids[0], "from": self.name, "to": "Hermes", "text": "文" * 4001}
        accepted = []

        def trigger(payload):
            if len(accepted) == 1:
                return False, {"error": "test offline"}
            accepted.append(payload["event_id"])
            return True, {"id": len(accepted)}

        self.server.trigger_client.post_trigger.side_effect = trigger
        _, pending = self.call("/messages", first)
        seq = pending["message"]["seq"]
        self.assertEqual((pending["status"], pending["message"]["acked_at"]), ("queued", None))
        self.server.trigger_client.post_trigger.side_effect = lambda p: (
            accepted.append(p["event_id"]) or True,
            {},
        )
        _, following = self.call("/messages", {**first, "message_id": ids[1], "text": "下一条"})
        self.assertEqual(
            accepted,
            [f"peer:{seq}:1", f"peer:{seq}:2", f"peer:{seq}:3", "peer:%s" % following["message"]["seq"]],
        )
        retried = self.call("/messages", first)[1]
        self.assertEqual((retried["status"], retried["duplicate"], len(accepted)), ("accepted", True, 4))
        self.assertEqual(self.call("/messages", {**first, "text": "冲突"})[0], 409)

    def test_peer_larger_than_generic_http_body_survives_sse_and_ack(self):
        msg = {"message_id": str(uuid.uuid4()), "from": "Hermes", "to": self.name, "text": "甲" * 400000}
        code, queued = self.call("/messages", msg)
        self.assertEqual((code, queued["status"]), (202, "queued"))
        self.assertEqual(
            self.stream_event(self.stream), ("worker_message_ref", {"message_id": msg["message_id"]})
        )
        inbox = self.call("/messages?worker=" + self.name, method="GET")[1]["messages"]
        self.assertEqual([m["text"] for m in inbox], [msg["text"]])
        ack = self.call("/messages/" + msg["message_id"] + "/ack", {"worker": self.name})[1]
        self.assertEqual(ack["status"], "accepted")
        self.assertEqual(
            self.call("/messages", msg)[1]["message"]["acked_at"], ack["message"]["acked_at"]
        )
