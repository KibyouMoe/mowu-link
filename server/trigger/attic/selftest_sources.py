#!/usr/bin/env python3
"""Source-adapter self-test (no real MC bridge or mailbox needed).

1. Runs a tiny local SSE server on 127.0.0.1:8799, points MCChatSource at it
   and asserts the frame becomes exactly one ingest call with the right fields.
2. Instantiates EmailIMAPSource with a missing secrets file and asserts it
   degrades to status "disabled" without raising.
3. Instantiates EmailIMAPSource in dry-run mode with fake credentials and
   asserts one loop iteration reports "ok" without opening a connection.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources.mc_chat import MCChatSource
from sources.email_imap import EmailIMAPSource
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
lg = logging.getLogger("selftest")

SSE_PORT = 8799
received = []
lock = threading.Lock()


class SSEHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/stream":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        frames = [
            ("chat", "42",
             '{"player":"Notch","message":"hello from sse","priority":4}'),
            ("chat", "43", '"plain string frame"'),
        ]
        for name, fid, data in frames:
            chunk = "event: %s\nid: %s\ndata: %s\n\n" % (name, fid, data)
            self.wfile.write(chunk.encode())
            self.wfile.flush()
            time.sleep(0.2)
        time.sleep(3)


def ingest(**kwargs):
    with lock:
        received.append(kwargs)
    return {"ok": True, "id": len(received), "duplicate": False}


srv = ThreadingHTTPServer(("127.0.0.1", SSE_PORT), SSEHandler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()

mc = MCChatSource({"enabled": True,
                   "url": "http://127.0.0.1:%d/stream" % SSE_PORT,
                   "reconnect_seconds": 1}, ingest, lg)
mc.start()
deadline = time.time() + 5
while time.time() < deadline and len(received) < 2:
    time.sleep(0.1)
mc.stop()
srv.shutdown()

print("mc health after stop:", mc.healthy())
print("mc emitted %d event(s):" % len(received))
for ev in received:
    print("  ", json.dumps(ev, ensure_ascii=False))

assert len(received) == 2, "expected 2 SSE events, got %d" % len(received)
assert received[0]["source"] == "mc_chat"
assert received[0]["kind"] == "chat"
assert received[0]["actor"] == "Notch"
assert received[0]["text"] == "hello from sse"
assert received[0]["priority"] == 4
assert received[0]["event_id"] == "mc_chat:sse:42"
assert received[1]["text"] == "plain string frame"
assert received[1]["event_id"] == "mc_chat:sse:43"
print("PASS mc_chat: SSE frames parsed, mapped, deduped by SSE id")

missing = EmailIMAPSource(
    {"enabled": True, "host": "imap.example.com", "port": 993,
     "ssl": True, "poll_seconds": 1,
     "secrets_file": "/nonexistent/email.env"},
    ingest, lg)
missing.start()
missing.stop()
print("email (missing secrets) health:", missing.healthy())
assert missing.healthy() == "disabled"
print("PASS email: missing secrets -> disabled, no exception")

dry = EmailIMAPSource(
    {"enabled": True, "host": "imap.example.com", "port": 993,
     "ssl": True, "poll_seconds": 0.5, "dry_run": True,
     "secrets_file": "/nonexistent/email.env"},
    ingest, lg)
import os
os.environ["TRIGGER_EMAIL_DRY_RUN"] = "0"
# dry_run flag must come from config even when env is absent; credentials still
# needed to enter the loop, so give it a temp env file.
tmp = Path("/Users/kibyou/.hermes/cache/scratch/email-test.env")
tmp.write_text("EMAIL_USER=test@example.com\nEMAIL_PASSWORD=fake\n")
dry.secrets_path = tmp
dry.start()
time.sleep(0.8)
print("email dry-run health:", dry.healthy())
dry.stop()
assert dry.healthy() == "ok", "dry-run should report ok without connecting"
print("PASS email: dry-run loop runs without network")

print("\nRESULT: PASS  (mc_chat SSE parsing + email graceful degradation)")
