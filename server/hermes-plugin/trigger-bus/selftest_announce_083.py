#!/usr/bin/env python3
"""Function-level selftest for trigger-bus 0.8.4/0.8.5 — the extra Chinese notice.

No gateway process, no trigger service, no network.  Every hook is called
directly; the gateway is a FakeGateway and the adapter a FakeAdapter that
records ``handle_message`` and every send call.

    /Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 \\
        selftest_announce_083.py

Covers the 0.8.4 contract (0.8.5 updates the notify case):
  a. event wake -> adapter.send_message called EXACTLY once, text exactly
     "⚡ 收到来自 selftest · Tester 的 Event 消息", chat_id = source.chat_id,
     and the send has already happened when the helper returns (no timer)
  b. notify wake -> exactly one send of "📬 收到来自 <source> · <actor> 的
     Notify 消息" (0.8.5; before this it sent nothing)
  c. send_message raises / answers SendResult(success=False) -> the wake still
     succeeds; the result is reported as failed, never as sent
  d. one message per wake; a merged event batch names its FIRST event
  e. an adapter without any send entry is logged and skipped, wake unaffected
  f. an async ``send`` (Feishu's real shape) is scheduled on the captured loop
  g. plugin.yaml + README are 0.8.4
  h. 0.8.4 dedupe: identical (chat, text) inside 2 s is dropped, sends again
     after the window, 32-entry cap drops the oldest, and a failed async send
     releases its claim so a retry is not blocked
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

HERMES = Path("/Users/kibyou/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES))
import hermes_bootstrap  # noqa: E402,F401  (puts the runtime deps on sys.path)

TMP = Path(tempfile.mkdtemp(prefix="trigger-bus-selftest-083-"))
os.environ.pop("TRIGGER_BUS_DISABLED", None)
os.environ["TRIGGER_BUS_CURSOR"] = str(TMP / "cursor.json")
os.environ["TRIGGER_BUS_LOCK"] = str(TMP / "inactive.lock")
os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:9"      # nothing listens
os.environ["TRIGGER_BUS_BATCH_TTL"] = "120"

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_bus_under_test_083",
                                              HERE / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)

plugin.LOG_PATH = TMP / "selftest.log"
plugin._ack_async = lambda *a, **k: None                  # no HTTP in tests
assert plugin.PLUGIN_VERSION in ("0.8.3", "0.8.4", "0.8.5", "0.9.0",
                                 "0.9.1", "0.9.2", "0.9.3", "0.9.4"), \
    plugin.PLUGIN_VERSION

from gateway.config import Platform                      # noqa: E402
from gateway.session import SessionSource                 # noqa: E402

SOURCE = SessionSource(platform=Platform.FEISHU, chat_id="oc_selftest",
                       chat_type="dm", user_id="ou_selftest")
ROUTING_KEY = "agent:main:feishu:dm:oc_selftest"
EXPECTED = "⚡ 收到来自 selftest · Tester 的 Event 消息"


class FakeAdapter:
    """The bits of gateway.platforms.base the plugin is allowed to touch."""

    def __init__(self) -> None:
        self._pending_messages = {}
        self.handled = []
        self.reply_on_handle = None
        self.sent = []                 # [(chat_id, text), ...]
        self.send_error = None

    def get_pending_message(self, session_key):
        return self._pending_messages.pop(session_key, None)

    def handle_message(self, event):
        self.handled.append(event)
        return self.reply_on_handle

    def send_message(self, chat_id, text):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((chat_id, text))
        return "ok"


class NoSendAdapter(FakeAdapter):
    """No send entry at all: the plugin must log and skip, never fail."""

    send_message = None


class AsyncSendAdapter(NoSendAdapter):
    """Feishu's real shape: one async ``send(chat_id, content)``."""

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=True)


class RefusingSendAdapter(FakeAdapter):
    """A send that answers SendResult(success=False) instead of raising."""

    def send_message(self, chat_id, text):
        return SimpleNamespace(success=False, error="Not connected")


class AsyncRefusingSendAdapter(NoSendAdapter):
    """Feishu's shape, but the coroutine answers SendResult(success=False)."""

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=False, error="Not connected")


class FakeGateway:
    def __init__(self, adapter) -> None:
        self.adapters = {Platform.FEISHU: adapter}
        self.queue_calls = []
        self.busy = False

    def _delivery_adapter_for(self, source):
        return self.adapters.get(Platform.FEISHU)

    def _session_key_for_source(self, source):
        return ROUTING_KEY

    def _is_session_running(self, session_key):
        return self.busy

    def _queue_or_replace_pending_event(self, session_key, event):
        self.queue_calls.append((session_key, event))
        self.adapters[Platform.FEISHU]._pending_messages[session_key] = event
        event._gateway_accepted = True


RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print("%s  %-70s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def new_state(adapter=None, **overrides):
    # Each case below is an independent wake.  The 0.8.4 duplicate guard is
    # intentionally process-wide (a re-entrant wake must not repeat a line),
    # so the harness clears it per case — otherwise a later case that sends
    # the same (chat, text) would be suppressed by an earlier case, not by
    # its own wake.
    plugin._announce_reset()
    for leftover in (os.environ["TRIGGER_BUS_CURSOR"],
                     os.environ["TRIGGER_BUS_CURSOR"] + ".tmp"):
        try:
            Path(leftover).unlink()
        except OSError:
            pass
    state = plugin._BusState()
    adapter = adapter if adapter is not None else FakeAdapter()
    state.gateway = FakeGateway(adapter)
    state.source = SOURCE
    state.loop = None
    for key, value in overrides.items():
        setattr(state, key, value)
    plugin._STATE = state
    return state, adapter


def ev(eid: int, level: str = "event", text: str = "", **extra) -> dict:
    event = {"id": eid, "level": level, "source": "selftest",
             "actor": "Tester", "kind": "chat",
             "ts": "2026-11-08T21:30:00+08:00",
             "text": text or ("%s-%d" % (level.upper(), eid))}
    event.update(extra)
    return event


def feed(state, *events) -> int:
    return plugin._merge_events(state, list(events))


print("trigger-bus %s selftest — extra Chinese Event notice, gateway-free"
      % plugin.PLUGIN_VERSION)
print("tmp/log: %s\n" % plugin.LOG_PATH)

# =========================================================================== #
# a. event wake -> exactly one send_message, exact text, source chat id
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(201, "event", text="NOTICE-ONE"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("a. event wake still hands over handle_message",
      len(adapter.handled) == 1 and adapter.handled[0].internal is False,
      "handled=%d internal=%s"
      % (len(adapter.handled), getattr(adapter.handled[0], "internal", None)))
check("a. send_message called exactly once",
      len(adapter.sent) == 1, "sent=%r" % (adapter.sent,))
check("a. text is exactly '%s'" % EXPECTED,
      adapter.sent == [(SOURCE.chat_id, EXPECTED)], "sent=%r" % (adapter.sent,))
check("a. chat_id comes from state.source.chat_id",
      adapter.sent and adapter.sent[0][0] == "oc_selftest",
      "chat_id=%r" % (adapter.sent[0][0] if adapter.sent else None,))
check("a. dispatch returned True", ok is True, "ok=%r" % (ok,))

# one wake, one message — a second dispatch in the same mark window sends none
plugin._dispatch(state, plugin._get_logger(), where="poll-2")
check("a. a duplicate dispatch sends no second message",
      len(adapter.sent) == 1 and len(adapter.handled) == 1,
      "sent=%d handled=%d" % (len(adapter.sent), len(adapter.handled)))

# =========================================================================== #
# b. notify -> the 📬 line belongs to DELIVERY, not to the enqueue (0.9.1)
# =========================================================================== #

NOTIFY_EXPECTED = "📬 收到来自 email · HR 的 Notify 消息"

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(202, "notify", source="email", actor="HR", text="NOTIFY-ONLY"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
held = next(iter(state.inflight.values()), None)
check("b. busy notify held plugin-side, no wake and no slot write",
      held is not None and not adapter.handled
      and not state.gateway.queue_calls and not adapter._pending_messages,
      "held=%s handled=%d slot=%s"
      % (held is not None, len(adapter.handled),
         list(adapter._pending_messages)))
check("b. 0.9.1: the enqueue sends NOTHING (queued is silent)",
      adapter.sent == [], "sent=%r" % (adapter.sent,))
check("b. the held notify carries the 0.8.2 wake line and waits",
      held is not None and held.text == NOTIFY_EXPECTED
      and held.phase == "held",
      "text=%r phase=%r" % (getattr(held, "text", None),
                            getattr(held, "phase", None)))
plugin._on_pre_llm_call(platform="feishu", session_id="s-083",
                        user_message="hi")
check("b. 0.9.1: the delivery proof sends exactly one 📬 line",
      adapter.sent == [(SOURCE.chat_id, NOTIFY_EXPECTED)],
      "sent=%r" % (adapter.sent,))

# idle notify: queued then drained by the plugin — silent until it is delivered
state, adapter = new_state()
feed(state, ev(203, "notify", text="NOTIFY-IDLE"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("b. idle notify drained via handle_message(internal=True), silent so far",
      len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is True
      and adapter.sent == [],
      "handled=%d internal=%r sent=%r"
      % (len(adapter.handled),
         getattr(adapter.handled[0], "internal", None) if adapter.handled else None,
         adapter.sent))
plugin._on_pre_llm_call(platform="feishu", session_id="s-083",
                        user_message="hi")
check("b. idle notify notice uses the default origin, once, at delivery",
      adapter.sent == [(SOURCE.chat_id,
                        "📬 收到来自 selftest · Tester 的 Notify 消息")],
      "sent=%r" % (adapter.sent,))

# =========================================================================== #
# c. send raises -> the wake is unaffected
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
adapter.send_error = RuntimeError("feishu send exploded")
feed(state, ev(204, "event", text="SEND-RAISES"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("c. handle_message still called when send_message raises",
      len(adapter.handled) == 1, "handled=%d" % len(adapter.handled))
check("c. the wake is still reported as succeeded", ok is True, "ok=%r" % (ok,))
check("c. the batch is in flight (delivery proof pending), not lost",
      len(state.inflight) == 1 and "i:204" in state.inflight,
      "inflight=%s" % list(state.inflight))
check("c. no send recorded (the raise was swallowed)",
      adapter.sent == [], "sent=%r" % (adapter.sent,))

# a returned SendResult(success=False) is a logged failure too — never a wake
state, adapter = new_state(adapter=RefusingSendAdapter())
state.gateway.busy = True
feed(state, ev(214, "event", text="SEND-REFUSES"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("c. SendResult(success=False): wake still succeeds",
      ok is True and len(adapter.handled) == 1,
      "ok=%r handled=%d" % (ok, len(adapter.handled)))
status = plugin._send_event_announcement(state, adapter, ev(215), plugin._get_logger())
check("c. SendResult(success=False) is reported as failed, not sent",
      status == "failed:Not connected", "status=%r" % (status,))

# =========================================================================== #
# d. one message per wake; a merged batch names its FIRST event
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(205, "event", source="selftest", actor="Tester", text="FIRST"),
     ev(206, "event", source="other", actor="Someone", text="SECOND"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("d. a merged 2-event wake sends exactly one message",
      len(adapter.sent) == 1, "sent=%r" % (adapter.sent,))
check("d. the merged message names the FIRST event's origin",
      adapter.sent and adapter.sent[0][1] == EXPECTED,
      "text=%r" % (adapter.sent[0][1] if adapter.sent else None,))
check("d. a 2-event event wake is still one handle_message",
      len(adapter.handled) == 1, "handled=%d" % len(adapter.handled))

# =========================================================================== #
# e. adapter without any send entry -> logged + skipped, wake unaffected
# =========================================================================== #

state, adapter = new_state(adapter=NoSendAdapter())
state.gateway.busy = True
feed(state, ev(207, "event", text="NO-SEND-METHOD"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("e. no send entry: handle_message still ran",
      len(adapter.handled) == 1, "handled=%d" % len(adapter.handled))
check("e. no send entry: dispatch still True and nothing sent",
      ok is True and adapter.sent == [], "ok=%r sent=%r" % (ok, adapter.sent))
status = plugin._send_event_announcement(state, adapter, ev(208), plugin._get_logger())
check("e. helper reports skipped:no-send-method",
      status == "skipped:no-send-method", "status=%r" % (status,))

# =========================================================================== #
# f. async send (Feishu's real shape) is scheduled on the captured loop
# =========================================================================== #

state, adapter = new_state(adapter=AsyncSendAdapter())
state.gateway.busy = True
loop = asyncio.new_event_loop()
thread = threading.Thread(target=loop.run_forever, name="selftest-083-loop",
                          daemon=True)
thread.start()
state.loop = loop
try:
    feed(state, ev(209, "event", text="ASYNC-SEND"))
    plugin._dispatch(state, plugin._get_logger(), where="poll")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not adapter.sent:
        time.sleep(0.02)
finally:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()
check("f. async send() ran on the captured loop with the exact text",
      adapter.sent == [("oc_selftest", EXPECTED)], "sent=%r" % (adapter.sent,))

# =========================================================================== #
# h. 0.8.4 guard: 2 s window, 32-entry cap, failed async send releases
# =========================================================================== #

clock = [1000.0]
plugin._announce_clock = lambda: clock[0]
plugin._announce_reset()
logger = plugin._get_logger()
try:
    # h1. identical (chat, text) inside the window: the second event still
    #     wakes the session, but no second line is sent.
    state, adapter = new_state()
    state.gateway.busy = True
    feed(state, ev(220, "event", text="DEDUPE-FIRST"))
    plugin._dispatch(state, plugin._get_logger(), where="poll")
    feed(state, ev(221, "event", text="DEDUPE-SECOND"))
    plugin._dispatch(state, plugin._get_logger(), where="poll")
    check("h. identical (chat, text) inside 2 s sends only one line",
          len(adapter.sent) == 1 and len(adapter.handled) == 2,
          "sent=%d handled=%d" % (len(adapter.sent), len(adapter.handled)))

    # h2. after the window the same line may be sent again.
    clock[0] += plugin.ANNOUNCE_DEDUPE_WINDOW + 0.01
    feed(state, ev(222, "event", text="DEDUPE-THIRD"))
    plugin._dispatch(state, plugin._get_logger(), where="poll")
    check("h. the same line is sent again after the 2 s window",
          len(adapter.sent) == 2, "sent=%d" % len(adapter.sent))

    # h3. capacity 32, oldest dropped first.
    plugin._announce_reset()
    for i in range(plugin.ANNOUNCE_DEDUPE_MAX):
        plugin._announce_claim("oc_cap", "cap-%d" % i, logger)
    check("h. the duplicate guard holds at most 32 entries",
          len(plugin._announce_recent) == plugin.ANNOUNCE_DEDUPE_MAX == 32,
          "size=%d" % len(plugin._announce_recent))
    plugin._announce_claim("oc_cap", "cap-32", logger)
    check("h. the 33rd entry drops the oldest",
          len(plugin._announce_recent) == 32
          and ("oc_cap", "cap-0") not in plugin._announce_recent,
          "size=%d oldest_live=%s"
          % (len(plugin._announce_recent),
             ("oc_cap", "cap-0") in plugin._announce_recent))
    check("h. an evicted (chat, text) is sendable again",
          plugin._announce_claim("oc_cap", "cap-0", logger) is not None, "")

    # h4. a sync send is already done when the helper returns: no timer.
    plugin._announce_reset()
    state, adapter = new_state()
    status = plugin._send_event_announcement(state, adapter, ev(223), logger)
    check("h. the sync notice is sent before the helper returns (no timer)",
          status == "sent:send_message" and len(adapter.sent) == 1,
          "status=%r sent=%r" % (status, adapter.sent))

    # h5. an async SendResult(success=False) releases its dedupe claim.
    plugin._announce_reset()
    state, adapter = new_state(adapter=AsyncRefusingSendAdapter())
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="selftest-084-loop",
                              daemon=True)
    thread.start()
    state.loop = loop
    try:
        first = plugin._send_event_announcement(state, adapter, ev(224), logger)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and plugin._announce_recent:
            time.sleep(0.02)
        second = plugin._send_event_announcement(state, adapter, ev(225), logger)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(adapter.sent) < 2:
            time.sleep(0.02)
        check("h. a failed async send releases its claim for a retry",
              first == "sent:send" and second == "sent:send"
              and len(adapter.sent) == 2,
              "first=%r second=%r sent=%d"
              % (first, second, len(adapter.sent)))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
finally:
    plugin._announce_clock = time.monotonic
    plugin._announce_reset()

# =========================================================================== #
# g. version files
# =========================================================================== #

yaml_text = (HERE / "plugin.yaml").read_text(encoding="utf-8")
readme = (HERE / "README.md").read_text(encoding="utf-8")
check("g. plugin.yaml version matches PLUGIN_VERSION",
      ("version: %s" % plugin.PLUGIN_VERSION) in yaml_text, "")
check("g. plugin.yaml documents both notices",
      "收到来自 <source> · <actor> 的 Event 消息" in yaml_text
      and "收到来自 <source> · <actor> 的 Notify 消息" in yaml_text, "")
check("g. README is titled with the current version and documents both notices",
      readme.startswith("# trigger-bus (%s)" % plugin.PLUGIN_VERSION)
      and "收到来自 <source> · <actor> 的 Event 消息" in readme
      and "收到来自 <source> · <actor> 的 Notify 消息" in readme, "")

# --------------------------------------------------------------------------- #
failed = [name for name, ok, _ in RESULTS if not ok]
print("\n%d/%d checks passed%s"
      % (len(RESULTS) - len(failed), len(RESULTS),
         "" if not failed else "; FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
