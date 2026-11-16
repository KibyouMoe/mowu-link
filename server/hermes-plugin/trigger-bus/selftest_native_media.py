"""Offline regression tests for the durable gateway image sender."""
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("native_media_under_test", Path(__file__).with_name("media_relay.py"))
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class NativeMediaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "images"
        self.cache.mkdir()
        self.calls, self.receipts = [], []
        self.now = 1000
        self.fail_image = False
        self.relay = module.NativeImageRelay(self.root / "media.sqlite", self.cache,
            self.deliver, self.receipt, clock=lambda: self.now, start_worker=False)

    def deliver(self, kind, chat, content, message_id=None):
        self.calls.append((kind, chat, content, message_id))
        return {"success": not (kind == "image" and self.fail_image),
                "platform": "feishu", "message_id": message_id or "om_" + str(len(self.calls))}

    def receipt(self, payload, result):
        self.receipts.append((payload, result))
        return True

    def event(self, identity="e1", data=b"image-A"):
        sha = hashlib.sha256(data).hexdigest()
        path = self.cache / (sha + ".png")
        path.write_bytes(data)
        return {"event_id": identity, "actor": "ImageWorker", "kind": "worker.input.image",
                "_attachment_files": [{"path": str(path), "sha256": sha, "size": len(data), "media_type": "image/png"}]}

    def drain(self):
        while self.relay.process_one():
            pass
        while self.relay.acknowledge_one():
            pass

    def test_text_then_independent_image_and_real_receipt(self):
        self.relay.submit(self.event(), "oc_chat")
        self.drain()
        self.assertEqual([call[0] for call in self.calls], ["text", "image"])
        self.assertEqual(self.calls[0][2], module.TITLE)
        result = self.receipts[0][1]
        self.assertTrue(result["success"])
        self.assertEqual(result["text_message_id"], "om_1")
        self.assertEqual(result["images"][0]["message_id"], "om_2")
        self.assertNotIn(str(self.cache), json.dumps(result))

    def test_same_event_and_same_sha_are_deduplicated_within_ten_minutes(self):
        event = self.event()
        self.assertTrue(self.relay.submit(event, "oc_chat"))
        self.assertFalse(self.relay.submit(event, "oc_chat"))
        self.drain()
        self.now += 599
        self.relay.submit(self.event("e2"), "oc_chat")
        self.drain()
        self.assertEqual([c[0] for c in self.calls].count("image"), 1)
        self.assertTrue(self.receipts[-1][1]["images"][0]["duplicate"])
        self.now += 2
        self.relay.submit(self.event("e3"), "oc_chat")
        self.drain()
        self.assertEqual([c[0] for c in self.calls].count("image"), 2)

    def test_dedup_survives_recreation_and_destinations_are_independent(self):
        self.relay.submit(self.event(), "oc_chat")
        self.drain()
        self.relay = module.NativeImageRelay(self.root / "media.sqlite", self.cache,
            self.deliver, self.receipt, clock=lambda: self.now, start_worker=False)
        self.relay.submit(self.event("e2"), "oc_chat")
        self.relay.submit(self.event("e3"), "oc_other")
        self.drain()
        self.assertEqual([c[0] for c in self.calls].count("image"), 2)

    def test_distinct_image_is_never_replaced_by_the_previous_id(self):
        first, second = self.event(), self.event("e2", b"image-B")
        self.relay.submit(first, "oc_chat")
        self.relay.submit(second, "oc_chat")
        self.drain()
        self.assertNotEqual(self.receipts[0][1]["images"][0]["sha256"],
                            self.receipts[1][1]["images"][0]["sha256"])

    def test_image_failure_edits_original_card_and_never_silently_succeeds(self):
        self.fail_image = True
        self.relay.submit(self.event(), "oc_chat")
        self.drain()
        self.assertEqual([c[0] for c in self.calls], ["text", "image", "edit"])
        self.assertIn("图片发送失败", self.calls[-1][2])
        self.assertEqual(self.calls[-1][3], "om_1")
        self.assertFalse(self.receipts[0][1]["success"])

    def test_unconfirmed_claim_is_not_resent_after_restart(self):
        event = self.event()
        sha = event["_attachment_files"][0]["sha256"]
        with sqlite3.connect(self.root / "media.sqlite") as db:
            db.execute("INSERT INTO media VALUES (?,?,?,'dispatching',NULL,NULL)", ("oc_chat", sha, self.now))
        self.relay.submit(event, "oc_chat")
        self.drain()
        self.assertNotIn("image", [c[0] for c in self.calls])
        self.assertFalse(self.receipts[0][1]["success"])

    def test_download_or_upload_failure_is_a_text_only_card(self):
        self.relay.submit({"event_id": "failure", "actor": "ImageWorker", "_attachment_error": "图片过大未转发"}, "oc_chat")
        self.drain()
        self.assertEqual([c[0] for c in self.calls], ["text", "edit"])
        self.assertIn("图片过大未转发", self.calls[-1][2])

    def test_modified_cache_bytes_are_not_sent(self):
        event = self.event()
        Path(event["_attachment_files"][0]["path"]).write_bytes(b"changed")
        self.relay.submit(event, "oc_chat")
        self.drain()
        self.assertNotIn("image", [c[0] for c in self.calls])
        self.assertFalse(self.receipts[0][1]["success"])

    def test_success_without_a_real_platform_message_id_is_rejected(self):
        send = self.relay.deliver
        self.relay.deliver = lambda kind, *args: {"success": True, "platform": "feishu", "message_id": None} if kind == "image" else send(kind, *args)
        self.relay.submit(self.event(), "oc_chat")
        self.drain()
        self.assertFalse(self.receipts[0][1]["success"])
        self.assertIn("图片发送失败", self.calls[-1][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
