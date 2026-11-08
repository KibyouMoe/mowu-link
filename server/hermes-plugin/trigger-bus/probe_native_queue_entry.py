#!/usr/bin/env python3
"""Real-code probe for trigger-bus 0.9.0 — the native busy queue, no gateway.

Unlike the selftests (which fake the adapter), this drives the REAL Hermes
classes:

  A. ``BasePlatformAdapter._handle_message_while_active`` — proves the branch:
     with ``runner.session_authority`` set, a busy internal event goes to
     ``dispatch_busy_burst`` (the durable FIFO) and the busy-input (interrupt)
     handler is never consulted; without it, the busy handler runs.
  B. the plugin itself, on that same real adapter: a busy-time notify must reach
     ``_message_handler`` (the ``admit_message`` seam) with ``internal=True``,
     leave ``adapter._pending_messages`` empty, and log ``notify NATIVE-QUEUED``.

Run:
    /Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 probe_native_queue_entry.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERMES = Path("/Users/kibyou/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES))
import hermes_bootstrap  # noqa: E402,F401

TMP = Path(tempfile.mkdtemp(prefix="trigger-bus-probe-"))
os.environ.pop("TRIGGER_BUS_DISABLED", None)
os.environ["TRIGGER_BUS_CURSOR"] = str(TMP / "cursor.json")
os.environ["TRIGGER_BUS_LOCK"] = str(TMP / "inactive.lock")
os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:9"

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_bus_probe", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)
plugin.LOG_PATH = TMP / "probe.log"
plugin._ack_async = lambda *a, **k: None

from gateway.config import Platform                        # noqa: E402
from gateway.platforms.base import BasePlatformAdapter      # noqa: E402
from gateway.platforms.event import MessageEvent, MessageType  # noqa: E402
from gateway.session import SessionSource                   # noqa: E402
import gateway.session_ingress_burst as burst               # noqa: E402

SOURCE = SessionSource(platform=Platform.FEISHU, chat_id="oc_probe",
                       chat_type="dm", user_id="ou_probe")
KEY = "agent:main:feishu:dm:oc_probe"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("%s  %-70s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


class Concrete(BasePlatformAdapter):
    async def connect(self, *a, **k):
        return True

    async def disconnect(self, *a, **k):
        return None

    async def get_chat_info(self, *a, **k):
        return {}

    async def send(self, *a, **k):
        return None


class Runner:
    """Stands in for gateway.run's runner: bound ``handle_message`` + authority."""

    def __init__(self, adapter, authority):
        self.adapters = {Platform.FEISHU: adapter}
        self.session_authority = authority
        self.handled = []

    def _delivery_adapter_for(self, source):
        return self.adapters.get(Platform.FEISHU)

    def _session_key_for_source(self, source):
        return KEY

    def _is_session_running(self, session_key):
        return True                      # a turn is running its tool right now

    def _queue_or_replace_pending_event(self, session_key, event):
        raise AssertionError("0.9.0 must not park a busy notify in the pending slot")

    async def handle_message(self, event):
        # = gateway.run.handle_message -> session_ingress.admit_message
        self.handled.append(event)
        return ""


def build(authority):
    adapter = Concrete.__new__(Concrete)
    runner = Runner(adapter, authority)
    adapter._message_handler = runner.handle_message     # BOUND: __self__ has the authority
    adapter.gateway_runner = runner
    adapter._canonicalize = lambda source: None
    adapter.platform = Platform.FEISHU
    adapter.config = SimpleNamespace(extra={})
    adapter._active_sessions = {KEY: asyncio.Event()}    # a live turn owns the guard
    adapter._pending_messages = {}
    adapter._requeue_counts = {}
    adapter._session_tasks = {}
    adapter._background_tasks = set()
    return adapter, runner


def wake():
    return MessageEvent(text="📬 收到来自 selftest 的 Notify 消息", source=SOURCE,
                        internal=True, message_type=MessageType.TEXT)


async def part_a():
    """Real adapter branch: authority -> FIFO, no authority -> busy handler."""
    seen = {"fifo": 0, "busy": 0}
    real = burst.dispatch_busy_burst

    async def fake(adapter, event, session_key):
        seen["fifo"] += 1

    async def busy_handler(event, session_key):
        seen["busy"] += 1
        return True

    burst.dispatch_busy_burst = fake
    try:
        adapter, _ = build(None)
        adapter._busy_session_handler = busy_handler
        check("A1. no session_authority -> _shared_runtime_owns_input is False",
              adapter._shared_runtime_owns_input(wake()) is False)
        await adapter._handle_message_while_active(wake(), KEY)
        check("A2. no session_authority -> the busy-input handler runs",
              seen == {"fifo": 0, "busy": 1}, "seen=%s" % seen)

        seen.update(fifo=0, busy=0)
        adapter, _ = build(object())
        adapter._busy_session_handler = busy_handler
        check("A3. session_authority -> _shared_runtime_owns_input is True",
              adapter._shared_runtime_owns_input(wake()) is True)
        await adapter._handle_message_while_active(wake(), KEY)
        check("A4. session_authority -> durable FIFO, busy handler never runs",
              seen == {"fifo": 1, "busy": 0}, "seen=%s" % seen)
    finally:
        burst.dispatch_busy_burst = real


async def part_b():
    """The plugin on the real adapter: busy notify goes to the FIFO seam."""
    adapter, runner = build(object())
    state = plugin._BusState()
    state.gateway = runner
    state.source = SOURCE
    state.loop = asyncio.get_running_loop()
    plugin._STATE = state
    plugin._merge_events(state, [{"id": 1, "level": "notify", "source": "selftest",
                                  "kind": "chat", "ts": "2026-11-08T23:00:00+08:00",
                                  "text": "SLEEP-50"}])
    plugin._dispatch(state, plugin._get_logger(), where="probe")
    for _ in range(100):
        if runner.handled:
            break
        await asyncio.sleep(0.02)

    delivery = next(iter(state.inflight.values()), None)
    text = getattr(runner.handled[0], "text", "") if runner.handled else ""
    check("B1. plugin hands the busy notify to handle_message (FIFO seam)",
          len(runner.handled) == 1, "handled=%d" % len(runner.handled))
    check("B2. the wake event is internal=True with the 📬 line",
          bool(runner.handled) and runner.handled[0].internal is True
          and text == "📬 收到来自 selftest 的 Notify 消息", "text=%r" % (text,))
    check("B3. adapter._pending_messages stays EMPTY (the slot killed the tool)",
          not adapter._pending_messages, "slot=%s" % (adapter._pending_messages,))
    check("B4. the delivery is marked native and stays unproven (cursor 0)",
          getattr(delivery, "native", False) is True and state.cursor == 0,
          "native=%s phase=%s cursor=%d"
          % (getattr(delivery, "native", None), getattr(delivery, "phase", None),
             state.cursor))
    check("B5. the journal names the native admission route",
          "notify NATIVE-QUEUED" in plugin.LOG_PATH.read_text(encoding="utf-8")
          and "admit_native" in plugin.LOG_PATH.read_text(encoding="utf-8"))


async def main():
    print("trigger-bus %s probe — real gateway code, no gateway process\n"
          % plugin.PLUGIN_VERSION)
    await part_a()
    await part_b()
    failed = [name for name, ok in RESULTS if not ok]
    print("\n%d/%d checks passed%s"
          % (len(RESULTS) - len(failed), len(RESULTS),
             "" if not failed else "; FAILED: " + ", ".join(failed)))
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
