#!/usr/bin/env python3
"""fake_sse_server.py — throwaway SSE emitter for testing mc_chat_client.

Emits 5 chat frames on ``GET /stream`` (1 death, 1 溪流, 2 identical texts,
1 plain), then sends ``: ping`` heartbeats so a client can be stopped cleanly.

    python3 fake_sse_server.py --port 8799
    python3 mc_chat_client.py --url http://127.0.0.1:8799/stream --once-seconds 8

Frame ids include the server start time, so repeated runs never collide with
old ``mc_chat:sse:<id>`` rows in the inbox.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List

EVENTS: List[dict] = [
    {"player": "Steve", "message": "早上好"},
    {"player": "Steve", "message": "早上好"},
    {"player": "Alex", "message": "Steve was slain by Zombie"},
    {"player": "Alex", "message": "溪流 来了"},
    {"player": "Heron", "message": "go mining"},
]

RUN_ID = str(int(time.time()))
DELAY = 0.05


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-sse/1.0"

    def do_GET(self):  # noqa: N802 - stdlib signature
        if not self.path.startswith("/stream"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for index, event in enumerate(EVENTS, start=1):
                frame = "id: %s-%d\ndata: %s\n\n" % (
                    RUN_ID, index, json.dumps(event, ensure_ascii=False))
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
                time.sleep(DELAY)
            for _ in range(120):
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--delay", type=float, default=0.05,
                        help="seconds between frames (default %(default)s)")
    args = parser.parse_args()
    global DELAY
    DELAY = args.delay
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("fake SSE on http://127.0.0.1:%d/stream (run_id=%s)" %
          (args.port, RUN_ID), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
