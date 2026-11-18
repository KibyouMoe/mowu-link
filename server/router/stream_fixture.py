"""Isolated Router HTTP and SSE acceptance for the two state layers."""

from http import client as http_client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import server as router
from selftest_router import FakeWorker, http


class StreamFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="router-state-p1-")
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "router.db"
        with closing(sqlite3.connect(path)) as legacy:
            legacy.execute("""CREATE TABLE workers (
                name TEXT PRIMARY KEY, online INTEGER DEFAULT 0, busy INTEGER DEFAULT 0,
                queued INTEGER DEFAULT 0, last_seen REAL, connected_at REAL,
                version TEXT, note TEXT, state TEXT, progress_at REAL, task_id TEXT)""")
            legacy.execute("INSERT INTO workers(name, busy) VALUES ('legacy-worker', 1)")
            legacy.commit()
        self.name = "legacy-worker"
        self.store = router.RouterStore(str(path))
        self.addCleanup(self.store.close)
        self.sent = []
        trigger = mock.Mock()
        trigger.post_trigger.side_effect = lambda payload: (
            self.sent.append(payload) or True,
            {"id": len(self.sent)},
        )
        self.server = router.RouterServer(
            ("127.0.0.1", 0),
            self.store,
            "phase1-token",
            router.TriggerNotifier(trigger, self.store),
            mock.Mock(),
            ping_interval=0.2,
        )
        self.addCleanup(self.server.server_close)
        port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.shutdown)
        self.call("/register", {"name": self.name, "version": "legacy"})
        self.client = FakeWorker(self.name, self.server.token, port=port)
        self.client.start()
        self.addCleanup(self.close_worker)
        self.assertTrue(self.client.connected.wait(3))
        self.assertIsNone(self.client.error)

    def close_worker(self):
        conn = self.server.current_conn(self.name)
        if conn is not None:
            conn.close("selftest complete")
        self.client.stop()
        until = time.monotonic() + 2
        while self.server.online(self.name) and time.monotonic() < until:
            time.sleep(0.02)
        self.assertFalse(self.server.online(self.name))

    def call(self, route, body=None, method="POST"):
        return http(method, self.base + route, body, self.server.token)

    def versioned_stream(self):
        port = self.server.server_address[1]
        connection = http_client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request(
            "GET",
            "/stream?name=%s&state_layers=2" % self.name,
            headers={"X-Router-Token": self.server.token},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.addCleanup(connection.close)
        self.addCleanup(response.close)
        return response, response.getheader("X-Router-Generation")

    def stream_event(self, response):
        while True:
            raw = response.readline()
            self.assertTrue(raw, "SSE closed before the expected event")
            line = raw.decode("utf-8").strip()
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
                data = response.readline().decode("utf-8").partition(":")[2]
                return event, json.loads(data)
