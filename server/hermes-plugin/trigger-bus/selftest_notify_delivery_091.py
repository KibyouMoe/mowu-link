#!/usr/bin/env python3
"""Function-level selftest for trigger-bus 0.9.1 — 📬 only when DELIVERED.

The 0.9.0 defect this suite pins down (field log, 2026-11-08):

    23:06:11 notify NATIVE-QUEUED n=1 ids=73 … busy=1
    23:06:12 wake notice (send) delivered      ← 📬 shown here …
    23:06:57 DELIVERED mode=queue ids=73 … cursor=73   ← … but it arrived here

Since 0.9.1 the enqueue is silent and the ONE visible line is sent at the
delivery proof — the same decision point as ``DELIVERED`` and the cursor.

    /Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 \\
        selftest_notify_delivery_091.py

Covers:
  a. the reported timeline: busy notify enqueued ⇒ zero send calls, cursor not
     moved; repeated ticks while the turn runs still send nothing; the
     pre_llm_call injection sends exactly one 📬, AFTER the DELIVERED record,
     and moves the cursor in the same step
  b. the HELD route (no session authority) is silent too — enqueue, repeat
     ticks and the turn-end drain all send nothing; only the injection does
  c. the same batch confirmed twice still speaks once (ledger + announced flag)
  d. a merged 3-notify batch: one 📬, first source
  e. an idle notify: silent at dispatch, one 📬 at delivery
  f. event unchanged: ⚡ immediately at the wake, never a 📬 at injection
  g. the 2 s (chat, text) dedupe still applies at delivery time; capacity,
     window and cursor rule unchanged
  h. plugin.yaml / README say 0.9.1 and state the queue-is-silent rule
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

HERMES = Path("/Users/kibyou/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES))
import hermes_bootstrap  # noqa: E402,F401  (puts the runtime deps on sys.path)

TMP = Path(tempfile.mkdtemp(prefix="trigger-bus-selftest-091-"))
os.environ.pop("TRIGGER_BUS_DISABLED", None)
os.environ["TRIGGER_BUS_CURSOR"] = str(TMP / "cursor.json")
os.environ["TRIGGER_BUS_LOCK"] = str(TMP / "inactive.lock")
os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:9"      # nothing listens

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_bus_under_test_091",
                                              HERE / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)

plugin.LOG_PATH = TMP / "selftest.log"
plugin._ack_async = lambda *a, **k: None                  # no HTTP in tests
assert plugin.PLUGIN_VERSION in ("0.9.2", "0.9.3", "0.9.4"), plugin.PLUGIN_VERSION

from gateway.config import Platform                      # noqa: E402
from gateway.session import SessionSource                 # noqa: E402

SOURCE = SessionSource(platform=Platform.FEISHU, chat_id="oc_selftest",
                       chat_type="dm", user_id="ou_selftest")
ROUTING_KEY = "agent:main:feishu:dm:oc_selftest"
NOTIFY_TEXT = "📬 收到来自 email · HR 的 Notify 消息"
EVENT_TEXT_MC = "⚡ 收到来自 mc_chat · Steve 的 Event 消息"


class FakeAdapter:
    def __init__(self) -> None:
        self._pending_messages = {}
        self.handled = []
        self.sent = []

    def get_pending_message(self, session_key):
        return self._pending_messages.pop(session_key, None)

    def handle_message(self, event):
        self.handled.append(event)
        return None

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return "ok"


class FakeGateway:
    def __init__(self, adapter) -> None:
        self.adapters = {Platform.FEISHU: adapter}
        self.queue_calls = []
        self.busy = True

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


class MemoryHandler(logging.Handler):
    """Capture every record *in order* — the suite asserts the log sequence."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:                                  # noqa: BLE001
            pass

    def find(self, needle: str):
        return [i for i, line in enumerate(self.lines) if needle in line]


RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print("%s  %-70s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def new_state(adapter=None, authority=True, busy=True):
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
    state.gateway.busy = busy
    if authority:
        state.gateway.session_authority = object()   # 0.9.0 native busy queue
    state.source = SOURCE
    state.loop = None
    plugin._STATE = state
    return state, adapter


def ev(eid, level="notify", source="email", actor="HR", text=""):
    return {"id": eid, "level": level, "source": source, "actor": actor,
            "kind": "mail", "ts": "2026-11-08T23:06:00+08:00",
            "text": text or ("%s-%d" % (level.upper(), eid))}


def feed(state, *events):
    return plugin._merge_events(state, list(events))


def inject():
    return plugin._on_pre_llm_call(platform="feishu", session_id="s-091",
                                   user_message="hi")


memory = MemoryHandler()
plugin._get_logger().addHandler(memory)

print("trigger-bus %s selftest — 📬 only when delivered, gateway-free"
      % plugin.PLUGIN_VERSION)
print("tmp/log: %s\n" % plugin.LOG_PATH)

# =========================================================================== #
# a. the reported timeline: enqueue silent, delivery speaks once
# =========================================================================== #

state, adapter = new_state()
start = len(memory.lines)
feed(state, ev(401, "notify", source="email", actor="HR", text="FIELD-LOG"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
delivery = next(iter(state.inflight.values()), None)
check("a. busy notify took the native route (one hand-over, no slot write)",
      ok is True and len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is True
      and len(state.gateway.queue_calls) == 0
      and not adapter._pending_messages,
      "ok=%r handled=%d queue_calls=%d slot=%s"
      % (ok, len(adapter.handled), len(state.gateway.queue_calls),
         list(adapter._pending_messages)))
check("a. NATIVE-QUEUED: not a single send call (the defect is gone)",
      adapter.sent == [], "sent=%r" % (adapter.sent,))
check("a. the log proves the enqueue happened and no notice went out",
      any("NATIVE-QUEUED" in line for line in memory.lines[start:])
      and not any("wake notice" in line for line in memory.lines[start:]),
      "new_log=%r" % (memory.lines[start:],))
check("a. cursor untouched, card still queued, delivery in flight",
      state.cursor == 0 and [e.get("id") for e in state.events] == [401]
      and delivery is not None and delivery.phase == "in_flight"
      and delivery.native is True,
      "cursor=%d queue=%s phase=%r native=%r"
      % (state.cursor, [e.get("id") for e in state.events],
         getattr(delivery, "phase", None), getattr(delivery, "native", None)))

# the turn is still running: every later tick stays silent
for i in range(3):
    plugin._dispatch(state, plugin._get_logger(), where="poll-%d" % (i + 2))
check("a. three more ticks while the turn runs send nothing",
      adapter.sent == [] and len(adapter.handled) == 1 and state.cursor == 0,
      "sent=%r handled=%d cursor=%d"
      % (adapter.sent, len(adapter.handled), state.cursor))

mark = len(memory.lines)
context = inject()
body = context.get("context", "") if isinstance(context, dict) else ""
sent_at = len(adapter.sent)
delivered = memory.find("DELIVERED")
noticed = [i for i in memory.find("wake notice") if i >= mark]
check("a. the injection delivers the batch and moves the cursor",
      "FIELD-LOG" in body and state.cursor == 401 and not state.events
      and not state.inflight,
      "body=%r cursor=%d queue=%d inflight=%d"
      % (body, state.cursor, len(state.events), len(state.inflight)))
check("a. exactly one 📬 and it is the expected text",
      adapter.sent == [(SOURCE.chat_id, NOTIFY_TEXT)],
      "sent=%r" % (adapter.sent,))
check("a. 📬 is sent in the DELIVERED step, never before it",
      bool(delivered) and bool(noticed)
      and delivered[-1] < noticed[0] < len(memory.lines),
      "delivered=%r noticed=%r"
      % (delivered[-3:], noticed[:3]))
check("a. that line was not sent at the enqueue (it appears after the mark)",
      sent_at == 1, "sent=%d" % sent_at)

# =========================================================================== #
# b. the HELD route is silent until the injected turn
# =========================================================================== #

state, adapter = new_state(authority=False)
feed(state, ev(402, "notify", text="HELD-ONE"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
held = next(iter(state.inflight.values()), None)
check("b. no authority: the notify is HELD plugin-side (no wake)",
      ok is True and held is not None and held.phase == "held"
      and not adapter.handled and not state.gateway.queue_calls,
      "ok=%r phase=%r handled=%d queue_calls=%d"
      % (ok, getattr(held, "phase", None), len(adapter.handled),
         len(state.gateway.queue_calls)))
check("b. HELD sends nothing (0.9.1)", adapter.sent == [],
      "sent=%r" % (adapter.sent,))
for i in range(3):
    plugin._dispatch(state, plugin._get_logger(), where="poll-held-%d" % i)
check("b. repeat ticks while the turn runs stay silent",
      adapter.sent == [] and state.cursor == 0,
      "sent=%r cursor=%d" % (adapter.sent, state.cursor))
state.gateway.busy = False
plugin._dispatch(state, plugin._get_logger(), where="poll-idle")
check("b. the turn-end drain wakes it (internal=True) and still says nothing",
      len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is True
      and adapter.sent == [],
      "handled=%d internal=%r sent=%r"
      % (len(adapter.handled),
         getattr(adapter.handled[0], "internal", None), adapter.sent))
inject()
check("b. only the injection sends the one 📬",
      adapter.sent == [(SOURCE.chat_id, NOTIFY_TEXT)] and state.cursor == 402,
      "sent=%r cursor=%d" % (adapter.sent, state.cursor))

# =========================================================================== #
# c. a batch confirmed twice still speaks once
# =========================================================================== #

state, adapter = new_state()
feed(state, ev(403, "notify", text="ONCE-PLEASE"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
batch = list(next(iter(state.inflight.values())).events)
first = inject()
second = plugin._confirm_injected(state, batch)     # same batch, again
check("c. the second confirmation of the same batch is a no-op",
      first is not None and second == 0, "first=%r second=%r" % (first, second))
check("c. still exactly one 📬 for that batch",
      adapter.sent == [(SOURCE.chat_id, NOTIFY_TEXT)],
      "sent=%r" % (adapter.sent,))

# the belt-and-braces guard itself
state, adapter = new_state()
delivery = plugin._register_delivery(state, "q:404", [ev(404)], NOTIFY_TEXT,
                                     "queue", ROUTING_KEY, None)
one = plugin._announce_on_delivery(state, delivery, plugin._get_logger())
two = plugin._announce_on_delivery(state, delivery, plugin._get_logger())
check("c. _announce_on_delivery is once-only per delivery",
      one == "sent:send_message" and two == "skipped:already-announced"
      and len(adapter.sent) == 1,
      "one=%r two=%r sent=%r" % (one, two, adapter.sent))
check("c. an event delivery never announces through this path",
      plugin._announce_on_delivery(
          state,
          plugin._register_delivery(state, "i:405",
                                    [ev(405, "event", source="mc_chat",
                                        actor="Steve")],
                                    "⚡ 来自 mc_chat · Steve 的打断",
                                    "interrupt", ROUTING_KEY, None),
          plugin._get_logger()) == "skipped:not-notify",
      "sent=%r" % (adapter.sent,))

# =========================================================================== #
# d. merged batch: one 📬, first source
# =========================================================================== #

state, adapter = new_state()
feed(state, ev(406, "notify", source="email", actor="HR"),
     ev(407, "notify", source="calendar", actor="Boss"),
     ev(408, "notify", source="mc_chat", actor="Steve"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("d. a 3-notify batch: one wake and no line while queued",
      len(adapter.handled) == 1 and adapter.sent == [],
      "handled=%d sent=%r" % (len(adapter.handled), adapter.sent))
inject()
check("d. 0.9.2: one 📬 line per event of the merged batch",
      adapter.sent == [(SOURCE.chat_id,
                        "📬 收到来自 email · HR 的 Notify 消息\n"
                        "📬 收到来自 calendar · Boss 的 Notify 消息\n"
                        "📬 收到来自 mc_chat · Steve 的 Notify 消息")],
      "sent=%r" % (adapter.sent,))

# =========================================================================== #
# e. idle notify: silent at dispatch, one 📬 at delivery
# =========================================================================== #

state, adapter = new_state(busy=False)
feed(state, ev(409, "notify", text="IDLE"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("e. idle notify drained in the same tick, still no visible line",
      ok is True and len(adapter.handled) == 1 and adapter.sent == [],
      "ok=%r handled=%d sent=%r" % (ok, len(adapter.handled), adapter.sent))
inject()
check("e. the idle batch's 📬 arrives with its injection",
      adapter.sent == [(SOURCE.chat_id, NOTIFY_TEXT)],
      "sent=%r" % (adapter.sent,))

# =========================================================================== #
# f. event unchanged: ⚡ at the wake, never a 📬 at injection
# =========================================================================== #

state, adapter = new_state(busy=False)
feed(state, ev(410, "event", source="mc_chat", actor="Steve", text="EV"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("f. event still interrupts at once (internal=False)",
      ok is True and len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is False,
      "ok=%r handled=%d" % (ok, len(adapter.handled)))
check("f. the ⚡ line is sent immediately with the wake",
      adapter.sent == [(SOURCE.chat_id, EVENT_TEXT_MC)],
      "sent=%r" % (adapter.sent,))
inject()
check("f. the event's own injection adds no 📬",
      adapter.sent == [(SOURCE.chat_id, EVENT_TEXT_MC)] and state.cursor == 410,
      "sent=%r cursor=%d" % (adapter.sent, state.cursor))

# =========================================================================== #
# g. the 2 s (chat, text) dedupe still applies at delivery time
# =========================================================================== #

clock = [5000.0]
plugin._announce_clock = lambda: clock[0]
try:
    state, adapter = new_state()
    feed(state, ev(411, "notify", text="DEDUPE-A"))
    plugin._dispatch(state, plugin._get_logger(), where="poll")
    inject()
    check("g. first delivery sends its 📬", len(adapter.sent) == 1,
          "sent=%r" % (adapter.sent,))
    feed(state, ev(412, "notify", text="DEDUPE-B"))
    plugin._dispatch(state, plugin._get_logger(), where="poll-2")
    inject()
    check("g. an identical line inside 2 s is still dropped",
          len(adapter.sent) == 1 and state.cursor == 412,
          "sent=%r cursor=%d" % (adapter.sent, state.cursor))
    clock[0] += plugin.ANNOUNCE_DEDUPE_WINDOW + 0.01
    feed(state, ev(413, "notify", text="DEDUPE-C"))
    plugin._dispatch(state, plugin._get_logger(), where="poll-3")
    inject()
    check("g. after the window the next delivery sends again",
          len(adapter.sent) == 2 and state.cursor == 413,
          "sent=%r cursor=%d" % (adapter.sent, state.cursor))
finally:
    plugin._announce_clock = time.monotonic
    plugin._announce_reset()

# =========================================================================== #
# h. version files: 0.9.1 + the queue-is-silent rule
# =========================================================================== #

yaml_text = (HERE / "plugin.yaml").read_text(encoding="utf-8")
readme = (HERE / "README.md").read_text(encoding="utf-8")
check("h. plugin.yaml matches PLUGIN_VERSION",
      ("version: %s" % plugin.PLUGIN_VERSION) in yaml_text, "")
check("h. plugin.yaml states the delivery-time rule",
      "notify 的可见通知只在真正送达时发，排队不发" in yaml_text, "")
check("h. README is titled with the current version",
      readme.startswith("# trigger-bus (%s)" % plugin.PLUGIN_VERSION),
      readme.splitlines()[0] if readme else "")
check("h. README has the 0.9.1 section and both texts",
      "## 0.9.1 — notify 通知在送达时发，排队不发" in readme
      and "收到来自 <source> · <actor> 的 Notify 消息" in readme
      and "收到来自 <source> · <actor> 的 Event 消息" in readme, "")

# --------------------------------------------------------------------------- #
plugin._get_logger().removeHandler(memory)
failed = [name for name, ok, _ in RESULTS if not ok]
print("\n%d/%d checks passed%s"
      % (len(RESULTS) - len(failed), len(RESULTS),
         "" if not failed else "; FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
