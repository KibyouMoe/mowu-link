import copy
import tempfile
import unittest
from pathlib import Path
from xml.sax.saxutils import escape

from .adapter import HostAdapter
from .codex_desktop import CodexDesktop, envelope
from .storage import Fault


def item(method, prompt, source="owner", item_id="input"):
    xml = f"<codex_delegation><source_thread_id>{source}</source_thread_id><input>{escape(prompt)}</input></codex_delegation>"
    return {"id": item_id, "type": "functionCallOutput", "namespace": "codex_app", "name": method,
            "output": {"text": xml, "truncated": False}}


class TestPort:
    def __init__(self):
        self.sends = []
        self.snap = {"thread": {"id": "test", "kind": "codex", "hostId": "local", "status": {"type": "idle"}},
                     "page": {"hasMore": False}, "turns": [
                         {"id": "init", "items": [item("create_thread", "bootstrap", item_id="setup")]}]}
        self.accept = True
        self.drop = False

    def send(self, prompt):
        self.sends.append(prompt)
        if self.accept:
            self.snap["turns"].insert(0, {"id": "turn", "items": [item("send_message_to_thread", prompt)]})
        if self.drop:
            raise OSError("lost send acknowledgement")

    def read(self):
        return copy.deepcopy(self.snap)


class DesktopEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.port = TestPort()
        self.host = CodexDesktop(self.port, "test", "bootstrap")
        self.ledger = Path(self.tmp) / "intents.db"
        self.adapter = HostAdapter(self.ledger, self.host, "test")
        self.payload = {"kind": "task", "task_id": "T", "assignment_id": "A", "instructions": "hello <&>"}
        self.prompt = envelope("test", "D", self.payload)

    def test_lost_send_ack_still_recovers_exact_native_input(self):
        self.port.drop = True
        receipt = self.adapter.deliver("D", self.payload)
        self.assertEqual((receipt["native_id"], receipt["turn_id"], receipt["assignment_id"]), ("input", "turn", "A"))
        self.assertEqual(self.adapter.origin("input"), self.payload)
        self.assertEqual(self.port.sends, [self.prompt])

    def test_send_ack_without_evidence_never_resubmits_after_restart(self):
        self.port.accept = False
        self.assertIsNone(self.adapter.deliver("D", self.payload))
        restored = HostAdapter(self.ledger, CodexDesktop(self.port, "test", "bootstrap"), "test")
        self.assertIsNone(restored.deliver("D", self.payload))
        self.port.snap["turns"].insert(0, {"id": "late", "items": [item("send_message_to_thread", self.prompt)]})
        self.assertEqual(restored.deliver("D", self.payload)["turn_id"], "late")
        self.assertEqual(self.port.sends, [self.prompt])

    def test_source_and_host_role_required(self):
        actual = item("send_message_to_thread", self.prompt)
        items = [dict(actual, type="agentMessage"), dict(actual, namespace="other"),
                 item("send_message_to_thread", self.prompt, source="unrelated"),
                 item("create_thread", self.prompt)]
        self.port.snap["turns"].append({"id": "other", "items": items})
        self.assertIsNone(self.host.lookup("test", "D"))

    def test_partial_duplicate_and_tampered_evidence_fail_closed(self):
        self.port.send(self.prompt)
        turn = self.port.snap["turns"][0]
        other = item("send_message_to_thread", self.prompt, item_id="input-2")
        turn["items"].append(other)
        other["output"]["truncated"] = True
        self.assertIsNone(self.host.lookup("test", "D"))
        other["output"]["truncated"] = False
        self.port.snap["page"]["hasMore"] = True
        self.assertIsNone(self.host.lookup("test", "D"))
        self.port.snap["page"]["hasMore"] = False
        with self.assertRaisesRegex(Fault, "duplicate_desktop_input"):
            self.host.lookup("test", "D")
        other["output"]["text"] = other["output"]["text"].replace('"task_id":"T"', '"task_id":"X"')
        with self.assertRaisesRegex(Fault, "desktop_input_mismatch"):
            self.host.lookup("test", "D")

    def test_wrong_session_or_bootstrap_cannot_write(self):
        with self.assertRaisesRegex(Fault, "desktop_binding_unconfirmed"):
            CodexDesktop(self.port, "test", "other")
        self.port.snap["thread"]["id"] = "different"
        with self.assertRaisesRegex(Fault, "desktop_target_mismatch"):
            self.adapter.deliver("D", self.payload)
        self.assertEqual(self.port.sends, [])


if __name__ == "__main__":
    unittest.main()
