#!/usr/bin/env python3
"""Plugin degradation test: trigger-server unreachable must be silent.

Points the plugin at a closed port, registers with a fake ctx, calls the hook
and asserts it returns None without raising. Run with the real service stopped
on that port (8799 is only used by selftest_sources.py, which has exited).
"""

import importlib
import os
import sys
import time

os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:8799"
os.environ["TRIGGER_BUS_SUBSCRIBER"] = "offline-test"
os.environ["TRIGGER_BUS_CURSOR"] = "/Users/kibyou/.hermes/cache/scratch/trigger-offline-cursor.json"
os.environ.pop("TRIGGER_BUS_DISABLED", None)
sys.path.insert(0, "/Users/kibyou/.hermes/plugins")

mod = importlib.import_module("trigger-bus")
hooks = {}


class Ctx:
    def register_hook(self, name, fn):
        hooks[name] = fn


mod.register(Ctx())
time.sleep(2.5)  # let at least one failing poll happen
result = hooks["pre_llm_call"](session_id="offline", user_message="hi")
print("hook result with server down:", result)
assert result is None, "hook must not raise or return context when offline"
print("RESULT: PASS  (offline: register + poll failed silently, hook returned None)")
