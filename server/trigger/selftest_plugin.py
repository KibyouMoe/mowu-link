#!/usr/bin/env python3
"""Plugin self-test: load trigger-bus with a fake ctx, exercise the hook.

Uses a throw-away subscriber + cursor file so it cannot disturb hermes-main.
Requires the server on 127.0.0.1:8791 to be running.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import time
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8791"
SUB = "hermes-plugin-test-" + uuid.uuid4().hex[:8]
CURSOR = Path("/Users/kibyou/.hermes/cache/scratch/trigger-plugin-test-cursor.json")
PLUGIN_PARENT = "/Users/kibyou/.hermes/plugins"

os.environ["TRIGGER_BUS_URL"] = BASE
os.environ["TRIGGER_BUS_SUBSCRIBER"] = SUB
os.environ["TRIGGER_BUS_CURSOR"] = str(CURSOR)
os.environ.pop("TRIGGER_BUS_DISABLED", None)
try:
    CURSOR.unlink()
except FileNotFoundError:
    pass

sys.path.insert(0, PLUGIN_PARENT)
mod = importlib.import_module("trigger-bus")
print("import OK: module=%s version=%s file=%s"
      % (mod.__name__, mod.PLUGIN_VERSION, mod.__file__))

hooks = {}


class FakeCtx:
    def register_hook(self, name, fn):
        hooks[name] = fn
        print("register_hook(%r) captured" % name)


text = "plugin hook test " + uuid.uuid4().hex[:6]
body = json.dumps({"source": "mc_chat", "kind": "chat", "actor": "TestBot",
                   "text": text, "level": "event"}).encode()
req = urllib.request.Request(BASE + "/trigger", data=body, method="POST",
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=5) as resp:
    posted = json.loads(resp.read())
print("POST /trigger ->", json.dumps(posted))

mod.register(FakeCtx())
assert "pre_llm_call" in hooks, "pre_llm_call was not registered"

deadline = time.time() + 8
result = None
while time.time() < deadline:
    result = hooks["pre_llm_call"](session_id="test-session",
                                  user_message="hi", platform="cli")
    if result:
        break
    time.sleep(0.25)

assert result, "hook returned no context within 8s (poll thread did not deliver)"
context = result["context"]
print("hook returned keys:", sorted(result))
print("hook context:\n" + context)
assert "TRIGGER" in context, "no TRIGGER line in context"
pat = re.compile(r"^\[EVENT mc_chat/chat .+\] " + re.escape(text) + r"$", re.M)
assert pat.search(context), "EVENT line has unexpected format"

second = hooks["pre_llm_call"](session_id="test-session", user_message="again")
print("second hook call (queue should be empty):", second)
assert second is None, "queue was not cleared after injection"

time.sleep(1.0)
_TOKEN = (Path.home() / ".hermes" / "secrets" / "trigger.token")
_TOKEN = _TOKEN.read_text().strip() if _TOKEN.exists() else ""
_req = urllib.request.Request(BASE + "/stats?subscriber=" + SUB,
                              headers={"X-Trigger-Token": _TOKEN} if _TOKEN else {})
with urllib.request.urlopen(_req, timeout=5) as resp:
    stats = json.loads(resp.read())
print("GET /stats after hook ->", json.dumps(stats))
assert stats["unread"] == 0, "ACK did not advance the server cursor"
assert stats["last_id"] == posted["id"], "cursor did not reach the event id"

saved = json.loads(CURSOR.read_text())
print("cursor file ->", json.dumps(saved))
assert saved["last_id"] == posted["id"], "cursor file not persisted"

# disabled switch must make the hook inert
os.environ["TRIGGER_BUS_DISABLED"] = "1"
print("hook with TRIGGER_BUS_DISABLED=1 ->",
      hooks["pre_llm_call"](session_id="test-session"))
assert hooks["pre_llm_call"](session_id="test-session") is None
os.environ.pop("TRIGGER_BUS_DISABLED", None)

print("\nRESULT: PASS  (import, register, hook context, dedupe/clear, ACK, "
      "cursor persistence, disabled switch)")
