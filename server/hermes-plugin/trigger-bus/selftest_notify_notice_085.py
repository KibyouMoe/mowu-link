#!/usr/bin/env python3
"""Function-level selftest for trigger-bus 0.9.1 — the notify notice timing.

No gateway process, no trigger service, no network.  Every hook is called
directly; the gateway is a FakeGateway and the adapter a FakeAdapter that
records ``handle_message``, queue calls and every send call.

    /Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 \\
        selftest_notify_notice_085.py

This file started as the 0.8.5 notice suite; since 0.9.1 it pins WHEN the
``📬`` line is sent.  Covers:
  a. a notify is SILENT at enqueue (NATIVE-QUEUED and HELD): zero send calls,
     cursor untouched, the card still queued; the one
     "📬 收到来自 <source> · <actor> 的 Notify 消息" is sent by the injection
     that proves delivery (pre_llm_call), once, and a second call adds nothing
  b. one notice per batch: a merged notify batch names its FIRST event, a
     second dispatch in the same mark window sends nothing, and a later idle
     drain adds no second line
  c. a tick that merges notify + event: the event keeps its immediate ⚡ (both
     orders) and the notify's 📬 only lands when its cards are injected
  d. the dedupe key is chat + full text: event and notify lines never suppress
     each other inside the 2 s window, the same notify is dropped, another
     chat is not, and the window still expires
  e. info / log never send anything
  f. a failing send never affects the notify queue hand-over or the delivery
     proof
  g. plugin.yaml / README are 0.9.1 and document both texts
  h. idle event wake: handle_message(internal=False), exactly one ⚡ line
     immediately, no queue call at all, and the cursor moves exactly once when
     pre_llm_call proves the injection (a second call injects nothing)
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

TMP = Path(tempfile.mkdtemp(prefix="trigger-bus-selftest-085-"))
os.environ.pop("TRIGGER_BUS_DISABLED", None)
os.environ["TRIGGER_BUS_CURSOR"] = str(TMP / "cursor.json")
os.environ["TRIGGER_BUS_LOCK"] = str(TMP / "inactive.lock")
os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:9"      # nothing listens
os.environ["TRIGGER_BUS_BATCH_TTL"] = "120"

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_bus_under_test_085",
                                              HERE / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)

plugin.LOG_PATH = TMP / "selftest.log"
plugin._ack_async = lambda *a, **k: None                  # no HTTP in tests
assert plugin.PLUGIN_VERSION in ("0.8.5", "0.9.0", "0.9.1", "0.9.2", "0.9.3", "0.9.4"), plugin.PLUGIN_VERSION

from gateway.config import Platform                      # noqa: E402
from gateway.session import SessionSource                 # noqa: E402

SOURCE = SessionSource(platform=Platform.FEISHU, chat_id="oc_selftest",
                       chat_type="dm", user_id="ou_selftest")
ROUTING_KEY = "agent:main:feishu:dm:oc_selftest"
EVENT_TEXT = "⚡ 收到来自 email · HR 的 Event 消息"       # ev() default origin
EVENT_TEXT_MC = "⚡ 收到来自 mc_chat · Steve 的 Event 消息"  # mc_chat cases
NOTIFY_TEXT = "📬 收到来自 email · HR 的 Notify 消息"


class FakeAdapter:
    """The bits of gateway.platforms.base the plugin is allowed to touch."""

    def __init__(self) -> None:
        self._pending_messages = {}
        self.handled = []
        self.sent = []                 # [(chat_id, text), ...]
        self.send_error = None

    def get_pending_message(self, session_key):
        return self._pending_messages.pop(session_key, None)

    def handle_message(self, event):
        self.handled.append(event)
        return None

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
        await asyncio.sleep(0)
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=True)


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
    # The 0.8.4 duplicate guard is process-wide by design; each case below is
    # an independent delivery, so the harness clears it first (otherwise a later
    # case would be suppressed by an earlier one, not by its own wake).
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


def ev(eid: int, level: str = "notify", text: str = "", **extra) -> dict:
    event = {"id": eid, "level": level, "source": "email", "actor": "HR",
             "kind": "mail", "ts": "2026-11-08T22:30:00+08:00",
             "text": text or ("%s-%d" % (level.upper(), eid))}
    event.update(extra)
    return event


def feed(state, *events) -> int:
    return plugin._merge_events(state, list(events))


def inject():
    """The delivery proof: pre_llm_call hands the cards to the turn."""
    return plugin._on_pre_llm_call(platform="feishu", session_id="s-085",
                                   user_message="hi")


print("trigger-bus %s selftest — notify notice timing, gateway-free"
      % plugin.PLUGIN_VERSION)
print("tmp/log: %s\n" % plugin.LOG_PATH)

# =========================================================================== #
# a. busy notify: SILENT at enqueue, exactly one 📬 at the delivery proof
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
state.gateway.session_authority = object()   # 0.9.0: native busy queue reachable
feed(state, ev(301, "notify", source="email", actor="HR", text="NOTIFY-ONE"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
queued = adapter.handled[0] if adapter.handled else None
check("a. notify wake: one native-queue hand-over, no pending-slot write",
      len(state.gateway.queue_calls) == 0 and len(adapter.handled) == 1
      and not adapter._pending_messages,
      "queue_calls=%d handled=%d slot=%s"
      % (len(state.gateway.queue_calls), len(adapter.handled),
         list(adapter._pending_messages)))
check("a. 0.9.1: NATIVE-QUEUED sends NOTHING (no visible message)",
      adapter.sent == [], "sent=%r" % (adapter.sent,))
check("a. the queued wake is still internal=True",
      getattr(queued, "internal", None) is True,
      "internal=%r" % (getattr(queued, "internal", None),))
check("a. the notify wake text is unchanged (0.8.2 wording)",
      getattr(queued, "text", None) == NOTIFY_TEXT,
      "text=%r" % (getattr(queued, "text", None),))
check("a. cursor untouched and the card still in the local queue",
      state.cursor == 0 and [e.get("id") for e in state.events] == [301],
      "cursor=%d queue=%s" % (state.cursor,
                              [e.get("id") for e in state.events]))
check("a. dispatch still reports a delivery attempt", ok is True, "ok=%r" % (ok,))

context = inject()
body = context.get("context", "") if isinstance(context, dict) else ""
check("a. the delivery proof injects the card and moves the cursor once",
      state.cursor == 301 and not state.events and "NOTIFY-ONE" in body
      and not state.inflight,
      "cursor=%d queue=%d inflight=%d body=%r"
      % (state.cursor, len(state.events), len(state.inflight), body))
check("a. 0.9.1: exactly one 📬 line, sent with that injection",
      adapter.sent == [(SOURCE.chat_id, NOTIFY_TEXT)],
      "sent=%r" % (adapter.sent,))
again = inject()
check("a. a second proof adds no second line and moves no cursor",
      again is None and state.cursor == 301 and len(adapter.sent) == 1,
      "again=%r cursor=%d sent=%d"
      % (again, state.cursor, len(adapter.sent)))

# idle notify: queued then drained — still silent until it is injected
state, adapter = new_state()
feed(state, ev(302, "notify", text="NOTIFY-IDLE"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("a. idle notify drained via handle_message",
      len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is True,
      "handled=%d internal=%r"
      % (len(adapter.handled),
         getattr(adapter.handled[0], "internal", None)))
check("a. idle notify is silent at dispatch time (0.9.1)",
      adapter.sent == [], "sent=%r" % (adapter.sent,))
inject()
check("a. idle notify sends its one 📬 only at delivery",
      adapter.sent == [(SOURCE.chat_id, NOTIFY_TEXT)],
      "sent=%r" % (adapter.sent,))

# async send (Feishu's real shape) rides the captured gateway loop
state, adapter = new_state(adapter=AsyncSendAdapter())
state.gateway.busy = True
state.gateway.session_authority = object()   # 0.9.0: native busy queue reachable
loop = asyncio.new_event_loop()
thread = threading.Thread(target=loop.run_forever, name="selftest-085-loop",
                          daemon=True)
thread.start()
state.loop = loop
try:
    feed(state, ev(303, "notify", text="ASYNC-NOTIFY"))
    plugin._dispatch(state, plugin._get_logger(), where="poll")
    check("a. async adapter: still silent at enqueue",
          adapter.sent == [], "sent=%r" % (adapter.sent,))
    inject()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not adapter.sent:
        time.sleep(0.02)
finally:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()
check("a. async send() carries the 📬 text on the captured loop, at delivery",
      adapter.sent == [(SOURCE.chat_id, NOTIFY_TEXT)],
      "sent=%r" % (adapter.sent,))

# a failing send never affects the queue hand-over nor the delivery proof
state, adapter = new_state()
state.gateway.busy = True
state.gateway.session_authority = object()   # 0.9.0: native busy queue reachable
adapter.send_error = RuntimeError("feishu send exploded")
feed(state, ev(304, "notify", text="SEND-RAISES"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("f. a raising send leaves the notify handed over and dispatch True",
      ok is True and len(adapter.handled) == 1 and adapter.sent == [],
      "ok=%r handled=%d sent=%r"
      % (ok, len(adapter.handled), adapter.sent))
inject()
check("f. the delivery proof still commits when the notice send fails",
      state.cursor == 304 and not state.inflight and adapter.sent == [],
      "cursor=%d inflight=%d sent=%r"
      % (state.cursor, len(state.inflight), adapter.sent))

# =========================================================================== #
# b. one notice per batch
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
state.gateway.session_authority = object()   # 0.9.0: native busy queue reachable
feed(state, ev(305, "notify", source="email", actor="HR", text="FIRST"),
     ev(306, "notify", source="calendar", actor="Boss", text="SECOND"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("b. a merged 2-notify wake is silent while it is queued",
      adapter.sent == [], "sent=%r" % (adapter.sent,))
check("b. both notifies ride ONE wake",
      len(adapter.handled) == 1
      and len(getattr(adapter.handled[0], "text", "")) > 0,
      "handled=%d" % len(adapter.handled))
inject()
check("b. the delivery sends exactly one message for the whole batch",
      len(adapter.sent) == 1, "sent=%r" % (adapter.sent,))
check("b. 0.9.2: one 📬 line per notify, same order as the batch",
      adapter.sent == [(SOURCE.chat_id,
                        "📬 收到来自 email · HR 的 Notify 消息\n"
                        "📬 收到来自 calendar · Boss 的 Notify 消息")],
      "sent=%r" % (adapter.sent,))
plugin._dispatch(state, plugin._get_logger(), where="poll-2")
check("b. a duplicate dispatch adds neither a notice nor a second wake",
      len(adapter.sent) == 1 and len(adapter.handled) == 1,
      "sent=%d handled=%d" % (len(adapter.sent),
                              len(adapter.handled)))
# a later idle tick has nothing left to drain
state.gateway.busy = False
plugin._dispatch(state, plugin._get_logger(), where="poll-3")
check("b. a later idle drain adds no second notice",
      len(adapter.handled) == 1 and len(adapter.sent) == 1,
      "handled=%d sent=%d" % (len(adapter.handled), len(adapter.sent)))

# =========================================================================== #
# c. notify + event in one tick: ⚡ immediate (event unchanged), 📬 at delivery
# =========================================================================== #

# c1: notify first
state, adapter = new_state()
state.gateway.busy = True
state.gateway.session_authority = object()   # 0.9.0: native busy queue reachable
feed(state, ev(307, "notify", source="email", actor="HR", text="MIX-N"),
     ev(308, "event", source="mc_chat", actor="Steve", text="MIX-E"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("c. mixed (notify first): both routes still run",
      sorted(getattr(e, "internal", None) for e in adapter.handled) == [False, True]
      and len(state.gateway.queue_calls) == 0,
      "handled=%d queue_calls=%d"
      % (len(adapter.handled), len(state.gateway.queue_calls)))
check("c. mixed (notify first): only the event's ⚡ goes out now",
      adapter.sent == [(SOURCE.chat_id, EVENT_TEXT_MC)],
      "sent=%r" % (adapter.sent,))
inject()
check("c. mixed: the notify's 📬 lands with its injection, one per level",
      adapter.sent == [(SOURCE.chat_id, EVENT_TEXT_MC),
                       (SOURCE.chat_id, NOTIFY_TEXT)],
      "sent=%r" % (adapter.sent,))

# c2: event first
state, adapter = new_state()
state.gateway.busy = True
state.gateway.session_authority = object()   # 0.9.0: native busy queue reachable
feed(state, ev(309, "event", source="mc_chat", actor="Steve", text="MIX-E"),
     ev(310, "notify", source="email", actor="HR", text="MIX-N"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("c. mixed (event first): both routes still run",
      sorted(getattr(e, "internal", None) for e in adapter.handled) == [False, True]
      and len(state.gateway.queue_calls) == 0,
      "handled=%d queue_calls=%d"
      % (len(adapter.handled), len(state.gateway.queue_calls)))
check("c. mixed (event first): exactly the ⚡ line, immediately",
      adapter.sent == [(SOURCE.chat_id, EVENT_TEXT_MC)],
      "sent=%r" % (adapter.sent,))
inject()
check("c. mixed (event first): the 📬 follows at delivery, once",
      adapter.sent == [(SOURCE.chat_id, EVENT_TEXT_MC),
                       (SOURCE.chat_id, NOTIFY_TEXT)],
      "sent=%r" % (adapter.sent,))

# =========================================================================== #
# d. dedupe key = chat + full text; the two levels never collide
# =========================================================================== #

clock = [2000.0]
plugin._announce_clock = lambda: clock[0]
plugin._announce_reset()
logger = plugin._get_logger()
try:
    state, adapter = new_state()
    # same chat, same instant: event then notify -> both lines go out
    plugin._send_wake_notice(state, adapter, ev(311, "event"), logger)
    plugin._send_wake_notice(state, adapter, ev(312, "notify"), logger)
    check("d. event + notify in one 2 s window: both texts are sent",
          adapter.sent == [(SOURCE.chat_id, EVENT_TEXT),
                           (SOURCE.chat_id, NOTIFY_TEXT)],
          "sent=%r" % (adapter.sent,))
    check("d. both full texts are distinct dedupe keys",
          (SOURCE.chat_id, EVENT_TEXT) in plugin._announce_recent
          and (SOURCE.chat_id, NOTIFY_TEXT) in plugin._announce_recent,
          "keys=%r" % (list(plugin._announce_recent),))

    # the identical notify text is dropped inside the window
    status = plugin._send_wake_notice(state, adapter, ev(313, "notify"), logger)
    check("d. the identical 📬 line inside 2 s is skipped",
          status == "skipped:duplicate" and len(adapter.sent) == 2,
          "status=%r sent=%d" % (status, len(adapter.sent)))

    # same text, another chat: not a duplicate
    other = SimpleNamespace(chat_id="oc_other")
    state.source = other
    status = plugin._send_wake_notice(state, adapter, ev(314, "notify"), logger)
    check("d. the same text to another chat is not a duplicate",
          status == "sent:send_message"
          and adapter.sent[-1] == ("oc_other", NOTIFY_TEXT),
          "status=%r sent=%r" % (status, adapter.sent))
    state.source = SOURCE

    # after the window the same notify line may be sent again
    clock[0] += plugin.ANNOUNCE_DEDUPE_WINDOW + 0.01
    status = plugin._send_wake_notice(state, adapter, ev(315, "notify"), logger)
    check("d. after the 2 s window the 📬 line is sent again",
          status == "sent:send_message" and len(adapter.sent) == 4,
          "status=%r sent=%d" % (status, len(adapter.sent)))
finally:
    plugin._announce_clock = time.monotonic
    plugin._announce_reset()

# =========================================================================== #
# e. info / log never send anything
# =========================================================================== #

state, adapter = new_state()
feed(state, ev(316, "info", text="INFO-ONLY"),
     ev(317, "log", text="LOG-ONLY"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("e. info/log: no wake, no queue call, no send",
      ok is False and not adapter.handled and not state.gateway.queue_calls
      and adapter.sent == [],
      "ok=%r handled=%d queued=%d sent=%r"
      % (ok, len(adapter.handled), len(state.gateway.queue_calls),
         adapter.sent))
check("e. info/log stay out of the injectable set",
      [e.get("id") for e in plugin._unread_locked(state)] == []
      or all(plugin._level(e) not in plugin.INJECT_LEVELS
             for e in plugin._unread_locked(state)),
      "unread=%s" % [(e.get("id"), plugin._level(e))
                     for e in plugin._unread_locked(state)])

# =========================================================================== #
# h. idle event wake: normal interrupt path, one ⚡ line at once, never the
#    queue branch, cursor moves exactly once on delivery proof
# =========================================================================== #

state, adapter = new_state()
check("h. the case really starts idle", state.gateway.busy is False,
      "busy=%r" % state.gateway.busy)
feed(state, ev(320, "event", source="mc_chat", actor="Steve",
               text="IDLE-EVENT"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("h. idle event: handle_message called once with internal=False",
      ok is True and len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is False,
      "ok=%r handled=%d internal=%r"
      % (ok, len(adapter.handled),
         getattr(adapter.handled[0], "internal", None) if adapter.handled else None))
check("h. idle event: exactly one ⚡ notice, sent at once",
      adapter.sent == [(SOURCE.chat_id, EVENT_TEXT_MC)],
      "sent=%r" % (adapter.sent,))
check("h. idle event never takes the queue branch",
      state.gateway.queue_calls == [],
      "queue_calls=%r" % (state.gateway.queue_calls,))
check("h. the wake itself moves no cursor (delivery not proven yet)",
      state.cursor == 0 and [e.get("id") for e in state.events] == [320],
      "cursor=%d queue=%s" % (state.cursor,
                              [e.get("id") for e in state.events]))

context = inject()
body = context.get("context", "") if isinstance(context, dict) else ""
check("h. delivery proof injects the card and moves the cursor once",
      state.cursor == 320 and not state.events and "IDLE-EVENT" in body
      and not state.inflight,
      "cursor=%d queue=%d inflight=%d body=%r"
      % (state.cursor, len(state.events), len(state.inflight), body))
check("h. still exactly one notice after the injection",
      adapter.sent == [(SOURCE.chat_id, EVENT_TEXT_MC)],
      "sent=%r" % (adapter.sent,))

again = inject()
check("h. a second pre_llm_call injects nothing and moves no cursor",
      again is None and state.cursor == 320,
      "again=%r cursor=%d" % (again, state.cursor))

# =========================================================================== #
# g. version files document both texts and the 0.9.1 timing rule
# =========================================================================== #

yaml_text = (HERE / "plugin.yaml").read_text(encoding="utf-8")
readme = (HERE / "README.md").read_text(encoding="utf-8")
check("g. plugin.yaml carries the current version and documents both texts",
      ("version: %s" % plugin.PLUGIN_VERSION) in yaml_text
      and "收到来自 <source> · <actor> 的 Event 消息" in yaml_text
      and "收到来自 <source> · <actor> 的 Notify 消息" in yaml_text, "")
check("g. README is titled with the current version and documents both texts",
      readme.startswith("# trigger-bus (%s)" % plugin.PLUGIN_VERSION)
      and "收到来自 <source> · <actor> 的 Event 消息" in readme
      and "收到来自 <source> · <actor> 的 Notify 消息" in readme, "")
check("g. README states the 0.9.1 rule (notify 通知在送达时发，排队不发)",
      "notify 通知在送达时发，排队不发" in readme, "")

# --------------------------------------------------------------------------- #
failed = [name for name, ok, _ in RESULTS if not ok]
print("\n%d/%d checks passed%s"
      % (len(RESULTS) - len(failed), len(RESULTS),
         "" if not failed else "; FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
