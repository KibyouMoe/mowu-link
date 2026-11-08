#!/usr/bin/env python3
"""Function-level selftest for trigger-bus 0.8.1 — a one-line wake.

Kept as the 0.8.1 regression suite (0.8.2 only changed the notify literal and
the plugin version assertion).

No gateway process, no trigger service, no network.  Every hook is called
directly; the gateway is a FakeGateway that records ``handle_message`` /
``_queue_or_replace_pending_event`` and nothing else.

    /Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 \\
        selftest_wake_text_081.py

Covers the 0.8.1 contract:
  a. event wake  -> text is EXACTLY "⚡ 来自 <src> · <actor> 的打断" (one line,
                    no #id, no '> ' text), metadata empty
  b. notify wake -> text is EXACTLY "📬 收到来自 <src> · <actor> 的 Notify 消息"
                    (0.8.2), metadata empty
  c. the REAL gateway envelope clause accepts both, and still refuses the 0.8.0
     metadata shape with 'metadata'
  d. after the wake, pre_llm_call injects the same batch ONCE in the unchanged
     card format; the second pre_llm_call injects nothing (no duplicate), the
     cursor advances, the ledger is clean
  e. a woken batch is not woken twice while it waits for that injection
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERMES = Path("/Users/kibyou/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES))
import hermes_bootstrap  # noqa: E402,F401  (puts the runtime deps on sys.path)

TMP = Path(tempfile.mkdtemp(prefix="trigger-bus-selftest-081-"))
os.environ.pop("TRIGGER_BUS_DISABLED", None)
os.environ["TRIGGER_BUS_CURSOR"] = str(TMP / "cursor.json")
os.environ["TRIGGER_BUS_LOCK"] = str(TMP / "inactive.lock")
os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:9"      # nothing listens
os.environ["TRIGGER_BUS_BATCH_TTL"] = "120"

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_bus_under_test_081",
                                              HERE / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)

plugin.LOG_PATH = TMP / "selftest.log"
plugin._ack_async = lambda *a, **k: None                  # no HTTP in tests
assert plugin.PLUGIN_VERSION in ("0.8.3", "0.8.4", "0.8.5", "0.9.0",
                                 "0.9.1"), \
    plugin.PLUGIN_VERSION

from gateway.config import Platform                      # noqa: E402
from gateway.platforms.event import MessageEvent          # noqa: E402
from gateway.session import SessionSource                 # noqa: E402
from gateway.session_envelope import _event_clause        # noqa: E402

SOURCE = SessionSource(platform=Platform.FEISHU, chat_id="oc_selftest",
                       chat_type="dm", user_id="ou_selftest")
ROUTING_KEY = "agent:main:feishu:dm:oc_selftest"


class FakeAdapter:
    """The bits of gateway.platforms.base the plugin is allowed to touch."""

    def __init__(self) -> None:
        self._pending_messages = {}
        self.handled = []
        self.reply_on_handle = None

    def get_pending_message(self, session_key):
        return self._pending_messages.pop(session_key, None)

    def handle_message(self, event):
        self.handled.append(event)
        return self.reply_on_handle


class FakeGateway:
    def __init__(self, adapter: FakeAdapter, *, busy: bool = False) -> None:
        self.adapters = {Platform.FEISHU: adapter}
        self.queue_calls = []
        self.busy = busy

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
    print("%s  %-64s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def new_state(**overrides):
    for leftover in (os.environ["TRIGGER_BUS_CURSOR"],
                     os.environ["TRIGGER_BUS_CURSOR"] + ".tmp"):
        try:
            Path(leftover).unlink()
        except OSError:
            pass
    state = plugin._BusState()
    adapter = FakeAdapter()
    state.gateway = FakeGateway(adapter)
    state.source = SOURCE
    state.loop = None
    for key, value in overrides.items():
        setattr(state, key, value)
    plugin._STATE = state
    return state, adapter


def ev(eid: int, level: str = "notify", text: str = "", **extra) -> dict:
    event = {"id": eid, "level": level, "source": "selftest",
             "actor": "Tester", "kind": "chat",
             "ts": "2026-11-08T20:42:00+08:00",
             "text": text or ("%s-%d" % (level.upper(), eid))}
    event.update(extra)
    return event


def feed(state, *events) -> int:
    return plugin._merge_events(state, list(events))


def no_metadata(event) -> bool:
    """The empty default dict is the contract; None is accepted too."""
    return getattr(event, "metadata", {}) in ({}, None)


print("trigger-bus %s selftest — one-line wake, cards injected, gateway-free"
      % plugin.PLUGIN_VERSION)
print("tmp/log: %s\n" % plugin.LOG_PATH)

RUNNER = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False))

# =========================================================================== #
# a. event -> the interrupt line, and nothing else
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(57, "event", text="打断测试：溪流正在忙，这条应该立刻打断他",
               thread_key="mc_chat:global"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
event = adapter.handled[0] if adapter.handled else None
text = getattr(event, "text", "")

check("a. event wake -> handle_message ran (interrupt path)",
      event is not None and event.internal is False,
      "handled=%d internal=%s" % (len(adapter.handled),
                                  getattr(event, "internal", None)))
check("a. event wake text == '⚡ 来自 selftest · Tester 的打断'",
      text == "⚡ 来自 selftest · Tester 的打断", "text=%r" % (text,))
check("a. it is one line, no #id and no '> ' body",
      len(text.splitlines()) == 1 and "#" not in text
      and "\n> " not in text and "打断测试" not in text,
      "lines=%d" % len(text.splitlines()))
check("a. event MessageEvent.metadata is empty (no non-native keys)",
      event is not None and no_metadata(event),
      "metadata=%r" % (getattr(event, "metadata", "<none>"),))
check("a. the cards stay in the local queue for pre_llm_call",
      [e.get("id") for e in state.events] == [57],
      "local=%s" % [e.get("id") for e in state.events])

# =========================================================================== #
# b. notify -> the notice line, and nothing else
# =========================================================================== #

state, adapter = new_state()
# 0.9.0: a busy notify is held plugin-side, so the wake text is produced by the
# idle enqueue path (same _build_wake_event, same text).
feed(state, ev(58, "notify", source="email", actor="HR",
               text="周报：本周进度正常", thread_key="email:a1b2",
               ts="2026-11-08T19:58:00+08:00"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
queued = state.gateway.queue_calls[0][1] if state.gateway.queue_calls else None
ntext = getattr(queued, "text", "")

check("b. notify wake text == '📬 收到来自 email · HR 的 Notify 消息'",
      ntext == "📬 收到来自 email · HR 的 Notify 消息", "text=%r" % (ntext,))
check("b. it is one line, no card body",
      len(ntext.splitlines()) == 1 and "#58" not in ntext
      and "\n> " not in ntext,
      "lines=%d" % len(ntext.splitlines()))
check("b. notify MessageEvent.metadata is empty",
      queued is not None and no_metadata(queued),
      "metadata=%r" % (getattr(queued, "metadata", "<none>"),))
check("b. notify never carries the interrupt header",
      "⚡" not in ntext and "打断" not in ntext,
      "has_flash=%s has_break=%s" % ("⚡" in ntext, "打断" in ntext))
check("b. the notify cards stay in the local queue too",
      [e.get("id") for e in state.events] == [58],
      "local=%s" % [e.get("id") for e in state.events])

# =========================================================================== #
# c. the REAL gateway envelope clause: new shape admitted, 0.8.0 shape refused
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(59, "event", text="ENVELOPE", thread_key="mc_chat:global"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
live_event = adapter.handled[0]
clause = _event_clause(RUNNER, live_event, None, False)
check("c. real _event_clause accepts the 0.8.1 event wake",
      clause is None, "clause=%r metadata=%r" % (clause, live_event.metadata))

legacy = MessageEvent(
    text="legacy", source=SOURCE, internal=False, inherit_channel_pin=True,
    metadata={"interrupt_origin": "selftest · Tester"})
legacy_clause = _event_clause(RUNNER, legacy, None, False)
check("c. real _event_clause still refuses the 0.8.0 metadata shape",
      legacy_clause == "metadata", "clause=%r" % (legacy_clause,))

# =========================================================================== #
# d. the cards still arrive — once — through pre_llm_call, in card format
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(60, "event", text="PROVE-WITH-INJECTION",
               thread_key="mc_chat:global"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
wake = adapter.handled[0].text
# The turn the wake started carries the wake line as its message; the plugin
# must inject the cards into exactly that turn.
injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-081",
                                   user_message=wake)
body = injected.get("context", "") if isinstance(injected, dict) else ""
check("d. pre_llm_call injects the woken batch in card format",
      body.splitlines()[:1] == ["#60 event selftest/Tester 20:42 thr=mc_chat:global"]
      and body.splitlines()[1:2] == ["> PROVE-WITH-INJECTION"],
      "context=%r" % (body,))
check("d. cursor advanced and the ledger is clean after the injection",
      state.cursor == 60 and not state.inflight and not state.queued_keys,
      "cursor=%d inflight=%d marks=%d"
      % (state.cursor, len(state.inflight), len(state.queued_keys)))
second = plugin._on_pre_llm_call(platform="feishu", session_id="s-081",
                                 user_message="anything else")
check("d. a second pre_llm_call injects nothing (no duplicate)",
      second is None, "second=%r" % (second,))

# The notify flavour: the gateway drains the one-line wake, then pre_llm_call
# injects the cards of that same batch, once.
state, adapter = new_state()
feed(state, ev(70, "notify", text="NOTIFY-CARDS"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
# idle notify: queued, then the plugin drains the head itself
drained = (adapter.handled[0] if adapter.handled
           else adapter.get_pending_message(ROUTING_KEY))
injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-081b",
                                   user_message=drained.text)
body = injected.get("context", "") if isinstance(injected, dict) else ""
check("d. notify: the card is injected once the wake turn starts",
      "#70 notify selftest/Tester 20:42" in body and "> NOTIFY-CARDS" in body,
      "context=%r" % (body,))
check("d. notify: cursor advanced, ledger clean, no re-queue",
      state.cursor == 70 and not state.inflight
      and len(state.gateway.queue_calls) == 1,
      "cursor=%d inflight=%d queue_calls=%d"
      % (state.cursor, len(state.inflight), len(state.gateway.queue_calls)))

# =========================================================================== #
# e. one wake per batch: the poller must not nudge again while it waits
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(80, "event", text="ONCE-WAKE"))
plugin._dispatch(state, plugin._get_logger(), where="poll-1")
plugin._dispatch(state, plugin._get_logger(), where="poll-2")
plugin._dispatch(state, plugin._get_logger(), where="poll-3")
check("e. three polls -> exactly one interrupt wake",
      len(adapter.handled) == 1 and state.cursor == 0,
      "handled=%d cursor=%d" % (len(adapter.handled), state.cursor))
plugin._on_pre_llm_call(platform="feishu", session_id="s-081c",
                        user_message=adapter.handled[0].text)
check("e. the injection clears the mark; nothing is left inflight",
      state.cursor == 80 and not state.inflight and not state.events,
      "cursor=%d inflight=%d queue=%s"
      % (state.cursor, len(state.inflight),
         [e.get("id") for e in state.events]))

# A refused interrupt releases the mark so the next poll retries it.
state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(81, "event", text="REFUSED-THEN-OK"))
adapter.reply_on_handle = "⏳ another turn is running, please resend"
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("e. a rejected interrupt keeps the event queued and drops the mark",
      [e.get("id") for e in state.events] == [81] and not state.inflight,
      "local=%s inflight=%d"
      % ([e.get("id") for e in state.events], len(state.inflight)))

# A batch is one wake, one injection, both cards.
state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(90, "event", text="BATCH-1"), ev(91, "event", text="BATCH-2"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
batch_wake = adapter.handled[0].text if adapter.handled else ""
injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-081d",
                                   user_message=batch_wake)
body = injected.get("context", "") if isinstance(injected, dict) else ""
check("e. a 2-event batch wakes once with the first origin",
      batch_wake == "⚡ 来自 selftest · Tester 的打断"
      and len(adapter.handled) == 1,
      "wake=%r handled=%d" % (batch_wake, len(adapter.handled)))
check("e. both cards injected once, in order, no repeat",
      body.startswith("[2 条]") and "#90 event" in body and "#91 event" in body
      and state.cursor == 91 and not state.inflight,
      "context=%r cursor=%d" % (body[:40], state.cursor))
check("e. the next turn gets nothing",
      plugin._on_pre_llm_call(platform="feishu", session_id="s-081d",
                              user_message="hi") is None, "")

# --------------------------------------------------------------------------- #
failed = [name for name, ok, _ in RESULTS if not ok]
print("\n%d/%d checks passed%s"
      % (len(RESULTS) - len(failed), len(RESULTS),
         "" if not failed else "; FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
