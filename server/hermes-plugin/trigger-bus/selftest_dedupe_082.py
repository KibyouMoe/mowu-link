#!/usr/bin/env python3
"""Function-level selftest for trigger-bus 0.8.2 — one event, one route.

No gateway process, no trigger service, no network.  Every hook is called
directly; the gateway is a FakeGateway that records ``handle_message`` /
``_queue_or_replace_pending_event`` and nothing else.

    /Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 \
        selftest_dedupe_082.py

Covers the 0.8.2 contract:
  a. an event that already triggered a wake is NEVER inserted by the next
     five transform_tool_result calls (wake claim ``woken_ids``); the card
     still arrives exactly once through pre_llm_call in the wake's turn;
  b. an event that transform_tool_result already inserted is NOT woken
     afterwards (at-most-once kept, even if the event is re-attached);
  c. every wake failure releases the claim, so transform_tool_result inserts
     again as the fallback: hand-over raise, synchronous gateway refusal, and
     the TTL retry of an unproven wake;
  d. the wake first line is still exactly
     ``⚡ 来自 <source> · <actor> 的打断`` and nothing else;
  e. the notify wake is exactly
     ``📬 收到来自 <source> · <actor> 的 Notify 消息`` (one line).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERMES = Path("/Users/kibyou/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES))
import hermes_bootstrap  # noqa: E402,F401  (puts the runtime deps on sys.path)

TMP = Path(tempfile.mkdtemp(prefix="trigger-bus-selftest-082-"))
os.environ.pop("TRIGGER_BUS_DISABLED", None)
os.environ["TRIGGER_BUS_CURSOR"] = str(TMP / "cursor.json")
os.environ["TRIGGER_BUS_LOCK"] = str(TMP / "inactive.lock")
os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:9"      # nothing listens
os.environ["TRIGGER_BUS_BATCH_TTL"] = "120"

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_bus_under_test_082",
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


class FakeAdapter:
    """The bits of gateway.platforms.base the plugin is allowed to touch."""

    def __init__(self) -> None:
        self._pending_messages = {}
        self.handled = []
        self.reply_on_handle = None
        self.raise_on_handle = None

    def get_pending_message(self, session_key):
        return self._pending_messages.pop(session_key, None)

    def handle_message(self, event):
        if self.raise_on_handle is not None:
            raise self.raise_on_handle
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


def ev(eid: int, level: str = "event", text: str = "", **extra) -> dict:
    event = {"id": eid, "level": level, "source": "selftest",
             "actor": "Tester", "kind": "chat",
             "ts": "2026-11-08T20:42:00+08:00",
             "text": text or ("%s-%d" % (level.upper(), eid))}
    event.update(extra)
    return event


def feed(state, *events) -> int:
    return plugin._merge_events(state, list(events))


def transform(result: str = "TOOL-OUT", tool_name: str = "terminal", **kw):
    return plugin._on_transform_tool_result(tool_name=tool_name, args={},
                                            result=result, **kw)


print("trigger-bus %s selftest — one event, one route (wake XOR mid-turn)"
      % plugin.PLUGIN_VERSION)
print("tmp/log: %s\n" % plugin.LOG_PATH)

# =========================================================================== #
# a. wake first: the next five tool results must NOT insert that event
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(101, "event", text="WAKE-FIRST",
               thread_key="mc_chat:global"))
plugin._dispatch(state, plugin._get_logger(), where="poll")

check("a. the event wake ran once (internal=false)",
      len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is False,
      "handled=%d internal=%s" % (len(adapter.handled),
                                  getattr(adapter.handled[0], "internal", None)))
check("a. wake first line == '⚡ 来自 selftest · Tester 的打断'",
      getattr(adapter.handled[0], "text", "") == "⚡ 来自 selftest · Tester 的打断",
      "text=%r" % (getattr(adapter.handled[0], "text", ""),))
check("a. the woken event carries the wake claim (woken_ids)",
      plugin._event_key({"id": 101}) in state.woken_ids,
      "woken=%s local=%s" % (list(state.woken_ids),
                             [e.get("id") for e in state.events]))

unchanged = [transform("TOOL-OUT-%d" % i) for i in range(5)]
check("a. next 5 transform_tool_result calls insert nothing (None = untouched)",
      all(r is None for r in unchanged),
      "results=%r" % (unchanged,))
check("a. no '⚡ 立刻处理' / '#101' leaked into any tool result",
      all(r is None for r in unchanged),
      "clean=%s" % [r is None for r in unchanged])
check("a. the claim survives all five tool results; event stays queued",
      plugin._event_key({"id": 101}) in state.woken_ids
      and [e.get("id") for e in state.events] == [101],
      "woken=%s local=%s" % (list(state.woken_ids),
                             [e.get("id") for e in state.events]))

injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-082a",
                                   user_message=adapter.handled[0].text)
body = injected.get("context", "") if isinstance(injected, dict) else ""
check("a. pre_llm_call injects that card exactly once",
      body.splitlines()[:1] == ["#101 event selftest/Tester 20:42 thr=mc_chat:global"]
      and body.splitlines()[1:2] == ["> WAKE-FIRST"],
      "context=%r" % (body,))
check("a. injection clears the wake claim + ledger, cursor advances",
      state.cursor == 101 and not state.inflight and not state.queued_keys
      and not state.woken_ids,
      "cursor=%d inflight=%d queued=%d woken=%d"
      % (state.cursor, len(state.inflight), len(state.queued_keys),
         len(state.woken_ids)))
after = [transform("AFTER-%d" % i) for i in range(2)]
check("a. once delivered, later tool results insert nothing either",
      after == [None, None], "results=%r" % (after,))

# =========================================================================== #
# b. transform first: the wake must not carry that event afterwards
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(102, "event", text="TOOL-FIRST"))
first = transform("TOOL-B")
check("b. transform_tool_result inserts the queued event mid-turn",
      isinstance(first, str) and first.startswith("TOOL-B")
      and "⚡ 立刻处理：" in first
      and "#102 event selftest/Tester 20:42" in first
      and "> TOOL-FIRST" in first,
      "insert=%r" % (first,))
check("b. the mid-turn insert consumed it from the local queue",
      [e.get("id") for e in state.events] == []
      and plugin._event_key({"id": 102}) in state.interrupt_done,
      "local=%s done=%s" % ([e.get("id") for e in state.events],
                            list(state.interrupt_done)))

plugin._dispatch(state, plugin._get_logger(), where="poll-after-insert")
check("b. a dispatch right after the insert wakes nothing",
      adapter.handled == [] and state.gateway.queue_calls == [],
      "handled=%d queue_calls=%d" % (len(adapter.handled),
                                     len(state.gateway.queue_calls)))
check("b. wake text for that batch is empty (nothing to interrupt about)",
      plugin._interrupt_text([]) == "", "")

# The race the field bug came from: a failed delivery re-attaches the batch
# while the tool hook already inserted it.  It still must not be woken.
reattached = feed(state, ev(102, "event", text="TOOL-FIRST"))   # blocked by high_water
plugin._reattach_batch(state, [{"id": 102, "level": "event",
                                "source": "selftest", "actor": "Tester",
                                "ts": "2026-11-08T20:42:00+08:00",
                                "text": "TOOL-FIRST"}])
plugin._dispatch(state, plugin._get_logger(), where="poll-reattached")
check("b. a re-attached, already-inserted event is not woken (at-most-once)",
      adapter.handled == [] and state.gateway.queue_calls == [],
      "handled=%d queue_calls=%d reattached_merge=%d"
      % (len(adapter.handled), len(state.gateway.queue_calls), reattached))
check("b. pre_llm_call in the next turn injects nothing for it",
      plugin._on_pre_llm_call(platform="feishu", session_id="s-082b",
                              user_message="hi") is None, "")

# =========================================================================== #
# c. wake failure releases the claim -> transform_tool_result is the fallback
# =========================================================================== #

# c1. the hand-over itself raises: _interrupt_wake returns False, claim gone.
state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(103, "event", text="HANDOVER-RAISE"))
adapter.raise_on_handle = RuntimeError("adapter exploded")
plugin._dispatch(state, plugin._get_logger(), where="poll-raise")
check("c1. failed hand-over: no claim left, event still queued",
      not state.woken_ids and not state.inflight
      and [e.get("id") for e in state.events] == [103],
      "woken=%s inflight=%d local=%s"
      % (list(state.woken_ids), len(state.inflight),
         [e.get("id") for e in state.events]))
fallback = transform("TOOL-C1")
check("c1. transform_tool_result inserts it after the failed wake",
      isinstance(fallback, str) and "⚡ 立刻处理：" in fallback
      and "#103 event" in fallback and "> HANDOVER-RAISE" in fallback,
      "insert=%r" % (fallback,))

# c2. the gateway refuses the turn synchronously (the real lease-timeout
#     answer): _wake returns True, the done-callback fails the delivery.
state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(104, "event", text="GATEWAY-REFUSED"))
adapter.reply_on_handle = "⏳ another turn is running, please resend"
plugin._dispatch(state, plugin._get_logger(), where="poll-refused")
check("c2. refused wake: the claim is released, event stays queued",
      not state.woken_ids and not state.inflight
      and [e.get("id") for e in state.events] == [104],
      "woken=%s inflight=%d local=%s"
      % (list(state.woken_ids), len(state.inflight),
         [e.get("id") for e in state.events]))
fallback = transform("TOOL-C2")
check("c2. transform_tool_result is the fallback after the refusal",
      isinstance(fallback, str) and "#104 event" in fallback
      and "> GATEWAY-REFUSED" in fallback,
      "insert=%r" % (fallback,))

# c3. an accepted wake that never proves delivery: the TTL retry clears the
#     claim, so the next tool result can still insert it.
state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(105, "event", text="TTL-UNPROVEN"))
plugin._dispatch(state, plugin._get_logger(), where="poll-ttl")
check("c3. accepted wake holds the claim while it waits for proof",
      plugin._event_key({"id": 105}) in state.woken_ids and state.inflight,
      "woken=%s inflight=%d" % (list(state.woken_ids), len(state.inflight)))
for delivery in state.inflight.values():
    delivery.created_at -= (plugin.BATCH_TTL + 1)
plugin._retry_inflight(state, plugin._get_logger())
check("c3. TTL retry releases the claim (unproven wake is retryable)",
      not state.woken_ids and not state.inflight
      and [e.get("id") for e in state.events] == [105],
      "woken=%s inflight=%d local=%s"
      % (list(state.woken_ids), len(state.inflight),
         [e.get("id") for e in state.events]))
fallback = transform("TOOL-C3")
check("c3. transform_tool_result inserts it after the TTL expiry",
      isinstance(fallback, str) and "#105 event" in fallback
      and "> TTL-UNPROVEN" in fallback,
      "insert=%r" % (fallback,))

# =========================================================================== #
# d. release: version + wake line + one route per event, end to end
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(110, "event", text="ONE-ROUTE-A"),
     ev(111, "event", text="ONE-ROUTE-B"))
plugin._dispatch(state, plugin._get_logger(), where="poll-batch")
wake = adapter.handled[0].text if adapter.handled else ""
check("d. a 2-event batch wakes once, first line is the strict origin line",
      wake == "⚡ 来自 selftest · Tester 的打断" and len(wake.splitlines()) == 1
      and len(adapter.handled) == 1,
      "wake=%r handled=%d" % (wake, len(adapter.handled)))
check("d. both events are claimed, none can be inserted mid-turn",
      all(plugin._event_key({"id": i}) in state.woken_ids for i in (110, 111)),
      "woken=%s" % (list(state.woken_ids),))
mixed = [transform("BATCH-%d" % i) for i in range(5)]
check("d. five tool results during that window stay untouched (None)",
      mixed == [None] * 5, "results=%r" % (mixed,))
injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-082d",
                                   user_message=wake)
body = injected.get("context", "") if isinstance(injected, dict) else ""
check("d. the wake's turn injects both cards once, in order",
      body.startswith("[2 条]") and "#110 event" in body and "#111 event" in body,
      "context=%r" % (body[:60],))
check("d. ledger + claims clean after the single injection",
      state.cursor == 111 and not state.inflight and not state.woken_ids,
      "cursor=%d inflight=%d woken=%d"
      % (state.cursor, len(state.inflight), len(state.woken_ids)))

yaml_text = (HERE / "plugin.yaml").read_text(encoding="utf-8")
check("d. plugin.yaml version matches PLUGIN_VERSION",
      ("version: %s" % plugin.PLUGIN_VERSION) in yaml_text, "")
check("d. plugin.yaml documents '一条 event 只走一条路'",
      "一条 event 只走一条路" in yaml_text, "")

# =========================================================================== #
# e. the notify wake line (0.8.2 Chinese wording), event line untouched
# =========================================================================== #

state, adapter = new_state()
# 0.9.0: a busy notify is held plugin-side, so the wake text is produced by the
# idle enqueue path (same _build_wake_event, same text).
feed(state, ev(120, "notify", source="email", actor="HR",
               text="周报：本周进度正常", thread_key="email:a1b2",
               ts="2026-11-08T19:58:00+08:00"))
plugin._dispatch(state, plugin._get_logger(), where="poll-notify")
queued = state.gateway.queue_calls[0][1] if state.gateway.queue_calls else None
ntext = getattr(queued, "text", "")
check("e. notify wake == '📬 收到来自 email · HR 的 Notify 消息'",
      ntext == "📬 收到来自 email · HR 的 Notify 消息", "text=%r" % (ntext,))
check("e. notify wake is one line, no card body / no interrupt header",
      len(ntext.splitlines()) == 1 and "#120" not in ntext
      and "\n> " not in ntext and "⚡" not in ntext and "打断" not in ntext,
      "lines=%d" % len(ntext.splitlines()))
check("e. notify rides the gateway queue head, then that exact event is drained",
      queued is not None and getattr(queued, "internal", None) is True
      and adapter.handled == [queued],
      "internal=%s handled=%d same=%s"
      % (getattr(queued, "internal", None), len(adapter.handled),
         bool(adapter.handled and adapter.handled[0] is queued)))
check("e. event wake does NOT use the '收到来自…Event 消息' shape",
      plugin._interrupt_text([ev(121, "event")])
      == "⚡ 来自 selftest · Tester 的打断"
      and "收到来自" not in plugin._interrupt_text([ev(121, "event")])
      and "Event 消息" not in plugin._interrupt_text([ev(121, "event")]),
      "event=%r" % (plugin._interrupt_text([ev(121, "event")]),))
check("e. the notify cards still wait for pre_llm_call",
      [e.get("id") for e in state.events] == [120]
      and plugin._event_key({"id": 120}) not in state.woken_ids,
      "local=%s woken=%s" % ([e.get("id") for e in state.events],
                             list(state.woken_ids)))
injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-082e",
                                   user_message=ntext)
body = injected.get("context", "") if isinstance(injected, dict) else ""
check("e. the notify turn injects its card once",
      "#120 notify email/HR 19:58 thr=email:a1b2" in body
      and "> 周报：本周进度正常" in body,
      "context=%r" % (body,))

# --------------------------------------------------------------------------- #
failed = [name for name, ok, _ in RESULTS if not ok]
print("\n%d/%d checks passed%s"
      % (len(RESULTS) - len(failed), len(RESULTS),
         "" if not failed else "; FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
