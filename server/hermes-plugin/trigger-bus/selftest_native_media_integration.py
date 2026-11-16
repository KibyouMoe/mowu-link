"""Exercise the poll-to-native-media seam with an async Feishu adapter."""
import asyncio
import hashlib
import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path.home() / ".hermes/hermes-agent"))
import hermes_bootstrap  # noqa: F401
from gateway.config import Platform

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("native_media_plugin_test", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)


class Integration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        plugin.LOG_PATH = self.home / "test.log"
        self.calls, self.receipts = [], []
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.state = plugin._BusState()
        self.state.source = SimpleNamespace(platform=Platform.FEISHU, chat_id="oc_fixture")
        self.state.loop = self.loop
        calls = self.calls

        class Adapter:
            async def send(self, *, chat_id, content):
                calls.append(("text", chat_id, content))
                return SimpleNamespace(success=True, message_id="om_text")

            async def send_image_file(self, *, chat_id, image_path, caption):
                calls.append(("image", chat_id, image_path, caption))
                return SimpleNamespace(success=True, message_id="om_image")

        self.state.gateway = SimpleNamespace(adapters={Platform.FEISHU: Adapter()})
        self.old_receipt = plugin._native_media_receipt
        plugin._native_media_receipt = lambda payload, result: self.receipts.append(result) or True
        self.addCleanup(self.close)

    def close(self):
        self.state.stop.set()
        relay = self.state.native_media_relay
        if relay:
            relay.wake.set()
            relay.thread.join(2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)
        self.loop.close()
        plugin._native_media_receipt = self.old_receipt

    @patch.dict(os.environ, {"TRIGGER_BUS_MEDIA_RELAY": "1"})
    def test_receipt_proves_text_and_independent_native_image(self):
        data = b"gateway-image"
        sha = hashlib.sha256(data).hexdigest()
        root = self.home / "attachments/router"
        root.mkdir(parents=True)
        filename = root / (sha + ".png")
        filename.write_bytes(data)
        event = {"id": 1, "event_id": "native-one", "source": "router", "actor": "ImageWorker",
                 "kind": "worker.input.image", "_attachment_files": [
                     {"path": str(filename), "sha256": sha, "size": len(data), "media_type": "image/png"}]}
        plugin._relay_native_images(self.state, [event], self.home)
        deadline = time.monotonic() + 3
        while len(self.receipts) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([call[0] for call in self.calls], ["text", "image"])
        self.assertEqual(self.calls[1][-1], None)
        self.assertEqual(self.receipts[0]["platform"], "feishu")
        self.assertTrue(self.receipts[0]["success"])
        self.assertEqual(self.receipts[0]["images"][0]["message_id"], "om_image")
        plugin._relay_native_images(self.state, [dict(event, event_id="native-two")], self.home)
        deadline = time.monotonic() + 3
        while len(self.receipts) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([call[0] for call in self.calls].count("image"), 1)
        self.assertTrue(self.receipts[-1]["images"][0]["duplicate"])

    def test_native_card_hides_paths_and_file_protocol_text(self):
        rendered = plugin._format_event({"id": 1, "source": "router", "kind": "worker.input.image",
                                        "text": "# Files mentioned by the user:\nC:/Users/private.png"})
        self.assertIn("客户端原生图片输入", rendered)
        self.assertNotIn("C:/", rendered)
        self.assertNotIn("# Files", rendered)

    @patch.dict(os.environ, {}, clear=True)
    def test_disabled_default_keeps_local_attachment_without_media_dispatch(self):
        event = {"id": 2, "event_id": "local-only", "source": "router",
                 "kind": "worker.input.image", "text": "# Files mentioned by the user:\nC:/Users/private.png",
                 "_attachment_files": [{"path": "/local/verified-image.png"}]}
        plugin._relay_native_images(self.state, [event], self.home)
        self.assertIsNone(self.state.native_media_relay)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state.native_media_pending, {})
        rendered = plugin._format_event(event)
        self.assertIn("/local/verified-image.png", rendered)
        self.assertNotIn("自动投递", rendered)
        self.assertNotIn("C:/", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
