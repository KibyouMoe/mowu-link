#!/usr/bin/env python3
"""Function-level selftest for trigger-bus 0.8.0 — queue vs interrupt.

No gateway process, no trigger service, no network.  Every hook is called
directly; the gateway is a FakeGateway that records the two entry points the
plugin is allowed to use and emulates the real acceptance receipt
(``_gateway_accepted``) of ``_queue_or_replace_pending_event``.

    /Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 \
        selftest_queue_interrupt_080.py

Covers the 0.8.0 contract, items a-g:
  a. busy + notify   -> 1 queue call, handle_message untouched
  b. idle + notify   -> queued, then drained (handle_message on that event)
  c. busy + event    -> handle_message(internal=False)
  d. cursor          -> untouched while queued; advanced after pre_llm_call
  e. same event_id   -> enqueued exactly once
  f. no gateway/adapter -> event stays queued, WARN in the log
  g. drain raises    -> event stays queued, next round retries
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERMES = Path("/Users/kibyou/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES))
import hermes_bootstrap  # noqa: E402,F401  (puts the runtime deps on sys.path)

TMP = Path(tempfile.mkdtemp(prefix="trigger-bus-selftest-080-"))
os.environ.pop("TRIGGER_BUS_DISABLED", None)
os.environ["TRIGGER_BUS_CURSOR"] = str(TMP / "cursor.json")
os.environ["TRIGGER_BUS_LOCK"] = str(TMP / "inactive.lock")
os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:9"      # nothing listens
os.environ["TRIGGER_BUS_BATCH_TTL"] = "120"

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_bus_under_test",
                                              HERE / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)

plugin.LOG_PATH = TMP / "selftest.log"
plugin._ack_async = lambda *a, **k: None                  # no HTTP in tests
assert plugin.PLUGIN_VERSION.startswith(("0.8.", "0.9.")), plugin.PLUGIN_VERSION

from gateway.config import Platform                      # noqa: E402
from gateway.session import SessionSource                 # noqa: E402

SOURCE = SessionSource(platform=Platform.FEISHU, chat_id="oc_selftest",
                       chat_type="dm", user_id="ou_selftest")
ROUTING_KEY = "agent:main:feishu:dm:oc_selftest"


class FakeAdapter:
    """The bits of gateway.platforms.base the plugin is allowed to touch."""

    def __init__(self) -> None:
        self._pending_messages = {}        # session_key -> MessageEvent
        self.handled = []
        self.raise_on_handle = False
        self.reply_on_handle = None
        self.raise_text = "turn lease wait timed out after 5s"

    def get_pending_message(self, session_key):
        return self._pending_messages.pop(session_key, None)

    def handle_message(self, event):
        if self.raise_on_handle:
            raise RuntimeError(self.raise_text)
        self.handled.append(event)
        return self.reply_on_handle


class FakeGateway:
    """Records the two gateway entry points and mirrors the real receipt."""

    def __init__(self, adapter: FakeAdapter, *, busy: bool = False,
                 accept: bool = True, key: str = ROUTING_KEY) -> None:
        self.adapters = {Platform.FEISHU: adapter}
        self.queue_calls = []              # [(session_key, MessageEvent)]
        self.queue_threads = []            # ident of the calling thread
        self.handle_calls = 0              # busy-callback style entry (unused)
        self.busy = busy
        self.accept = accept
        self.key = key

    def _delivery_adapter_for(self, source):
        return self.adapters.get(Platform.FEISHU)

    def _session_key_for_source(self, source):
        return self.key

    def _is_session_running(self, session_key):
        return self.busy

    def _queue_or_replace_pending_event(self, session_key, event):
        """Mirror of run_busy.py: slot first, FIFO overflow after that;
        ``_gateway_accepted`` is the admission receipt the plugin checks."""
        self.queue_calls.append((session_key, event))
        self.queue_threads.append(threading.get_ident())
        if not self.accept:
            return                          # real gateway: silent early return
        slot = self.adapters[Platform.FEISHU]._pending_messages
        slot[session_key] = event
        event._gateway_accepted = True


class NativeQueueGateway(FakeGateway):
    """FakeGateway + ``session_authority``: the predicate the adapter's
    ``_shared_runtime_owns_input`` uses, so the plugin must take Hermes' own
    busy queue (``handle_message`` -> ``admit_native``) instead of the slot."""

    def __init__(self, adapter: FakeAdapter, **kwargs) -> None:
        super().__init__(adapter, **kwargs)
        self.session_authority = object()


RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print("%s  %-62s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


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
    event = {"id": eid, "level": level, "source": "selftest", "kind": "chat",
             "ts": "2026-11-08T20:00:00+08:00",
             "text": text or ("%s-%d" % (level.upper(), eid))}
    event.update(extra)
    return event


def feed(state, *events) -> int:
    return plugin._merge_events(state, list(events))


def log_text() -> str:
    path = Path(plugin.LOG_PATH)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def mark() -> int:
    return len(log_text())


def tail(offset: int) -> str:
    return log_text()[offset:]


def ids_of(events):
    return [e.get("id") for e in events]


print("trigger-bus %s selftest — queue vs interrupt, gateway-free"
      % plugin.PLUGIN_VERSION)
print("tmp/log: %s\n" % plugin.LOG_PATH)

# =========================================================================== #
# Part 0 — the injection seams still behave (no regression from 0.7.0)
# =========================================================================== #

state, adapter = new_state()
feed(state, ev(1, "info", text="INFO-OK"), ev(2, "log", text="LOG-OK"),
     ev(3, "notify", text="NOTIFY-OK"), ev(4, "event", text="EVENT-OK"))
injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-0",
                                   user_message="hi")
body = injected.get("context", "") if isinstance(injected, dict) else ""
check("0a. pre_llm_call injects notify+event, never info/log",
      "NOTIFY-OK" in body and "EVENT-OK" in body
      and "INFO-OK" not in body and "LOG-OK" not in body,
      "context=%r" % body[:60])
check("0a. direct injection still moves the cursor", state.cursor >= 4,
      "cursor=%d" % state.cursor)

state, adapter = new_state()
feed(state, ev(5, "event", text="TOOL-EVENT"))
first = plugin._on_transform_tool_result(tool_name="terminal", result="out")
second = plugin._on_transform_tool_result(tool_name="terminal", result="out")
check("0b. transform_tool_result inserts an event exactly once",
      isinstance(first, str) and "⚡ 立刻处理：" in first and second is None,
      "tail=%r" % ((first or "")[-24:]))

state, adapter = new_state()
feed(state, ev(6, "notify", text="TOOL-NOTIFY"))
check("0b. transform_tool_result never inserts a notify",
      plugin._on_transform_tool_result(tool_name="terminal",
                                       result="out") is None, "")

# =========================================================================== #
# Part 1 — session key: captured source first, session_id as the fallback
# =========================================================================== #

state, adapter = new_state()
key, how = plugin._session_key_for(state)
check("1a. session key comes from the captured source",
      key == ROUTING_KEY and "_session_key_for_source" in how,
      "%s via %s" % (key, how))
check("1a. the choice is written to the log",
      "session key for the gateway pending queue: %s" % ROUTING_KEY in log_text(),
      "")

state, adapter = new_state()
state.source, state.gateway = None, None
state.session_id = "sess-fallback-9"
key, how = plugin._session_key_for(state)
check("1b. no source -> session_id fallback, logged by name",
      key == "sess-fallback-9" and "session_id fallback" in how,
      "%s via %s" % (key, how))

# =========================================================================== #
# a. busy + notify -> held by the plugin, never the gateway's pending slot
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(101, "notify", text="BUSY-NOTIFY"))
offset = mark()
did = plugin._dispatch(state, plugin._get_logger(), where="poll")
gw = state.gateway
check("a. busy + notify -> 0 _queue_or_replace_pending_event (0.9.0 hold)",
      did is True and len(gw.queue_calls) == 0,
      "queue_calls=%d" % len(gw.queue_calls))
check("a. busy + notify -> handle_message NOT called",
      not adapter.handled, "handled=%d" % len(adapter.handled))
held = next(iter(state.inflight.values()), None)
check("a. the batch is held with no gateway event attached",
      held is not None and held.event is None and held.phase == "held",
      "held=%s phase=%s" % (held is not None, getattr(held, "phase", None)))
check("a. the hold uses the source-derived routing key",
      held is not None and held.session_key == ROUTING_KEY,
      "key=%s" % (getattr(held, "session_key", "-"),))
check("a. batch waits in the local queue, cursor NOT advanced",
      ids_of(state.events) == [101] and state.cursor == 0,
      "local=%s cursor=%d" % (ids_of(state.events), state.cursor))
check("a. the held wake is the one-line notice (cards stay behind)",
      held is not None
      and held.text == "📬 收到来自 selftest 的 Notify 消息"
      and not adapter._pending_messages,
      "text=%r slot=%s" % (getattr(held, "text", ""),
                           list(adapter._pending_messages)))
check("a. log says notify HELD (busy=1, turn still running)",
      "notify HELD n=1 ids=101" in tail(offset) and "busy=1" in tail(offset)
      and "reason=turn_still_running" in tail(offset), "")

# =========================================================================== #
# b. idle + notify -> enqueue then drain with handle_message
# =========================================================================== #

state, adapter = new_state()
feed(state, ev(111, "notify", text="IDLE-NOTIFY"))
offset = mark()
did = plugin._dispatch(state, plugin._get_logger(), where="poll")
gw = state.gateway
check("b. idle + notify -> enqueued once", len(gw.queue_calls) == 1,
      "queue_calls=%d" % len(gw.queue_calls))
check("b. idle + notify -> queued head drained via handle_message",
      len(adapter.handled) == 1 and adapter.handled[0] is gw.queue_calls[0][1],
      "handled=%d same_event=%s"
      % (len(adapter.handled),
         bool(adapter.handled and adapter.handled[0] is gw.queue_calls[0][1])))
check("b. the drained event is no longer parked in the adapter slot",
      ROUTING_KEY not in adapter._pending_messages, "")
check("b. log names the drain strategy",
      "no gateway drain entry" in tail(offset)
      and "adapter.get_pending_message + handle_message" in tail(offset), "")
check("b. no gateway drain entry exists on today's runner",
      plugin._queue_drain_entry(state) == ("", None),
      "probed=%s" % (plugin.DRAIN_ENTRY_NAMES,))

# =========================================================================== #
# c. busy + event -> interrupt (internal=False) via handle_message
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(121, "event", text="BUSY-EVENT"))
offset = mark()
did = plugin._dispatch(state, plugin._get_logger(), where="poll")
interrupt_event = adapter.handled[0] if adapter.handled else None
check("c. busy + event -> handle_message(internal=False)",
      did is True and len(adapter.handled) == 1
      and interrupt_event is not None
      and interrupt_event.internal is False,
      "handled=%d internal=%s"
      % (len(adapter.handled), getattr(interrupt_event, "internal", None)))
check("c. event interrupt does NOT use the notify queue",
      not state.gateway.queue_calls, "queue_calls=%d" % len(state.gateway.queue_calls))
check("c. log says event INTERRUPT internal=false busy=1",
      "event INTERRUPT n=1 ids=121 internal=false busy=1" in tail(offset), "")

# =========================================================================== #
# d. cursor: queued != delivered; only pre_llm_call proves it
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(131, "notify", text="PROVE-ME"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
held = next(iter(state.inflight.values()))
check("d. held batch: cursor still 0, one delivery in the ledger",
      state.cursor == 0 and len(state.inflight) == 1 and held.event is None,
      "cursor=%d inflight=%d event=%s"
      % (state.cursor, len(state.inflight), held.event))
check("d. the held wake is the notice line; the card waits locally",
      held.text == "📬 收到来自 selftest 的 Notify 消息"
      and ids_of(state.events) == [131],
      "wake=%r local=%s" % (held.text, ids_of(state.events)))

# The turn is STILL running (a tool is in flight): more ticks deliver nothing.
plugin._dispatch(state, plugin._get_logger(), where="poll")
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("d. repeat ticks while the turn runs deliver nothing",
      not adapter.handled and state.cursor == 0
      and not state.gateway.queue_calls and len(state.inflight) == 1,
      "handled=%d cursor=%d queue_calls=%d"
      % (len(adapter.handled), state.cursor, len(state.gateway.queue_calls)))

# Turn over: the plugin wakes the held batch itself (internal=True).
state.gateway.busy = False
plugin._dispatch(state, plugin._get_logger(), where="post_llm_call")
wake_line = adapter.handled[0].text if adapter.handled else ""
check("d. turn over -> held batch woken via handle_message(internal=True)",
      len(adapter.handled) == 1 and adapter.handled[0].internal is True
      and wake_line == "📬 收到来自 selftest 的 Notify 消息",
      "handled=%d internal=%s text=%r"
      % (len(adapter.handled), getattr(adapter.handled[0], "internal", None)
         if adapter.handled else None, wake_line))

# A normal turn is the turn the wake starts (or the next one): the cards go in
# through the unchanged injection path, which is the delivery proof.
injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-d",
                                   user_message=wake_line)
body = injected.get("context", "") if isinstance(injected, dict) else ""
check("d. pre_llm_call injects the card and advances the cursor",
      state.cursor == 131 and "#131 notify selftest 20:00" in body
      and "> PROVE-ME" in body,
      "cursor=%d context=%r" % (state.cursor, body[:48]))
check("d. delivery left the ledger (no retry, no double delivery)",
      not state.inflight and not state.queued_keys,
      "inflight=%d queued_keys=%d" % (len(state.inflight), len(state.queued_keys)))

# =========================================================================== #
# e. same event_id twice -> exactly one enqueue
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
replay = ev(141, "notify", text="ONCE-ONLY")
batch = list(plugin._merge_events(state, [replay]) and [replay])
plugin._notify_enqueue(state, plugin._get_logger(), batch, where="t1")
plugin._notify_enqueue(state, plugin._get_logger(), batch, where="t2")
check("e. same event_id twice -> one held batch, no gateway write",
      len(state.inflight) == 1 and not state.gateway.queue_calls,
      "inflight=%d queue_calls=%d"
      % (len(state.inflight), len(state.gateway.queue_calls)))
# Re-polling the server cannot duplicate it either: the id is in the ledger.
feed(state, ev(141, "notify", text="ONCE-ONLY"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("e. a re-pulled copy is not queued again",
      len(state.inflight) == 1 and not state.gateway.queue_calls,
      "inflight=%d queue_calls=%d"
      % (len(state.inflight), len(state.gateway.queue_calls)))

# =========================================================================== #
# f. no gateway / no adapter -> stays queued, WARN, never lost
# =========================================================================== #

state, adapter = new_state()
feed(state, ev(151, "notify", text="NO-GATEWAY"))
state.gateway = None
real_resolver = plugin._resolve_gateway
plugin._resolve_gateway = lambda *_: None
offset = mark()
did = plugin._dispatch(state, plugin._get_logger(), where="poll")
mid = tail(offset)
plugin._resolve_gateway = real_resolver
check("f. no gateway -> not delivered, event still queued",
      did is False and ids_of(state.events) == [151] and state.cursor == 0,
      "queue=%s cursor=%d" % (ids_of(state.events), state.cursor))
check("f. no gateway -> WARN explains the retention",
      "WARNING" in mid and "no gateway captured" in mid, "")

state, adapter = new_state()
feed(state, ev(152, "notify", text="NO-ADAPTER"))
state.gateway.adapters.clear()
offset = mark()
did = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("f. no adapter -> no queue call, event still queued",
      did is False and ids_of(state.events) == [152]
      and not state.gateway.queue_calls,
      "queue=%s queue_calls=%d"
      % (ids_of(state.events), len(state.gateway.queue_calls)))
check("f. no adapter -> WARN says so",
      "no adapter" in tail(offset), "")

# A gateway that refuses admission (queue cap / no drainable slot) keeps it too.
# (0.9.0: a busy notify is held plugin-side and never reaches the gateway, so the
# refusal path is exercised on the idle enqueue, the only place it can happen.)
state, adapter = new_state()
state.gateway.accept = False
feed(state, ev(153, "notify", text="REFUSED"))
offset = mark()
did = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("f. gateway refusal -> event back in the local queue",
      did is False and ids_of(state.events) == [153] and state.cursor == 0,
      "queue=%s cursor=%d" % (ids_of(state.events), state.cursor))
check("f. gateway refusal -> WARN 'REFUSED the queued notify'",
      "REFUSED the queued notify" in tail(offset), "")

# =========================================================================== #
# g. drain raises (gateway rejects) -> still queued, next round retries
# =========================================================================== #

state, adapter = new_state()
feed(state, ev(161, "notify", text="REJECT-THEN-OK"))
adapter.raise_on_handle = True
offset = mark()
did = plugin._dispatch(state, plugin._get_logger(), where="poll")
call_count = len(state.gateway.queue_calls)
check("g. drain raised -> event back in the local queue",
      ids_of(state.events) == [161],
      "queue=%s did=%s" % (ids_of(state.events), did))
check("g. drain raised -> cursor untouched, ledger clean",
      state.cursor == 0 and not state.inflight,
      "cursor=%d inflight=%d" % (state.cursor, len(state.inflight)))
check("g. drain raised -> WARN records the failure",
      "delivery FAILED" in tail(offset) and "retry on the next poll" in tail(offset), "")

adapter.raise_on_handle = False
did = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("g. next round re-enqueues and delivers",
      did is True and len(state.gateway.queue_calls) == call_count + 1
      and len(adapter.handled) == 1,
      "queue_calls=%d handled=%d"
      % (len(state.gateway.queue_calls), len(adapter.handled)))

# =========================================================================== #
# h. at-least-once: a delivery the gateway no longer holds is retried
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(171, "notify", text="LOST-SLOT"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
assert len(state.inflight) == 1
delivery = next(iter(state.inflight.values()))
delivery.created_at = time.monotonic() - plugin.BATCH_TTL - 1
offset = mark()
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("h. held batch past TTL -> marks cleared and held again, never lost",
      len(state.inflight) == 1 and ids_of(state.events) == [171]
      and state.cursor == 0 and not state.gateway.queue_calls,
      "inflight=%d local=%s cursor=%d queue_calls=%d"
      % (len(state.inflight), ids_of(state.events), state.cursor,
         len(state.gateway.queue_calls)))
check("h. the retry is logged",
      "delivery not PROVEN" in tail(offset), "")

# =========================================================================== #
# i. notify never interrupts; event never rides in the queue
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
feed(state, ev(181, "notify", text="N"), ev(182, "event", text="E"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
held_levels = [d.mode for d in state.inflight.values() if d.mode == "queue"]
handled_levels = [e.internal for e in adapter.handled]
check("i. notify -> held(queue), event -> interrupt(handle_message internal=False)",
      held_levels == ["queue"] and handled_levels == [False]
      and not state.gateway.queue_calls,
      "held=%s handled=%s queue_calls=%d"
      % (held_levels, handled_levels, len(state.gateway.queue_calls)))

# =========================================================================== #
# p. 0.9.0 — busy + notify WITH a session authority -> Hermes' own queue
# =========================================================================== #

state, adapter = new_state()
state.gateway = NativeQueueGateway(adapter, busy=True)
feed(state, ev(241, "notify", text="NATIVE-QUEUE"))
offset = mark()
did = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("p. busy + notify + session authority -> handle_message (native queue)",
      did is True and len(adapter.handled) == 1
      and adapter.handled[0].internal is True
      and not state.gateway.queue_calls,
      "handled=%d internal=%s queue_calls=%d"
      % (len(adapter.handled),
         getattr(adapter.handled[0], "internal", None) if adapter.handled else None,
         len(state.gateway.queue_calls)))
native = next(iter(state.inflight.values()), None)
check("p. the batch is marked native-queued (not held, not slot-parked)",
      native is not None and native.native is True
      and native.phase == "in_flight" and not adapter._pending_messages,
      "native=%s phase=%s slot=%s"
      % (getattr(native, "native", None), getattr(native, "phase", None),
         list(adapter._pending_messages)))
check("p. log names the native admission route",
      "notify NATIVE-QUEUED" in tail(offset)
      and "admit_native" in tail(offset)
      and "busy=1" in tail(offset), "")
# Repeat ticks must not queue it twice (the durable row owns its execution).
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("p. a second tick does not hand the same batch over again",
      len(adapter.handled) == 1 and len(state.inflight) == 1
      and state.cursor == 0,
      "handled=%d inflight=%d cursor=%d"
      % (len(adapter.handled), len(state.inflight), state.cursor))
# The queued turn finally runs: pre_llm_call is still the only proof.
injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-p",
                                   user_message=adapter.handled[0].text)
body = injected.get("context", "") if isinstance(injected, dict) else ""
check("p. the queued turn injects the card and advances the cursor once",
      state.cursor == 241 and "#241 notify selftest 20:00" in body
      and "> NATIVE-QUEUE" in body,
      "cursor=%d context=%r" % (state.cursor, body[:48]))

# =========================================================================== #
# q. 0.9.0 — the reported scenario: sleep 50 in flight, notify arrives
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True                      # a turn is running its tool
feed(state, ev(251, "notify", text="SLEEP-50"))
tick = mark()
for _ in range(5):                             # the tool keeps running
    plugin._dispatch(state, plugin._get_logger(), where="poll")
during = tail(tick)
check("q. tool in flight: QUEUED/HELD, no DELIVERED, no wake at all",
      not adapter.handled and state.cursor == 0
      and "DELIVERED" not in during and "notify HELD" in during
      and not state.gateway.queue_calls,
      "handled=%d cursor=%d queue_calls=%d"
      % (len(adapter.handled), state.cursor, len(state.gateway.queue_calls)))
# The tool returns and the turn finishes: NOW the batch may open its own turn.
state.gateway.busy = False
plugin._dispatch(state, plugin._get_logger(), where="post_llm_call")
after = tail(tick)
check("q. turn finished -> one internal=True wake, logged as the turn boundary",
      len(adapter.handled) == 1 and adapter.handled[0].internal is True
      and "notify DRAINED (turn finished)" in after,
      "handled=%d internal=%s"
      % (len(adapter.handled),
         getattr(adapter.handled[0], "internal", None) if adapter.handled else None))
injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-q",
                                   user_message=adapter.handled[0].text)
body = injected.get("context", "") if isinstance(injected, dict) else ""
check("q. the new turn injects the card, cursor moves exactly once",
      state.cursor == 251 and "> SLEEP-50" in body
      and tail(tick).count("DELIVERED") == 1,
      "cursor=%d delivered_lines=%d"
      % (state.cursor, tail(tick).count("DELIVERED")))

# =========================================================================== #
# l. the poll loop itself drives notify (queue) and event (interrupt)
# =========================================================================== #

state, adapter = new_state()
os.environ["TRIGGER_BUS_POLL_INTERVAL"] = "0.1"
served = {"calls": 0}


def fake_fetch(_state, _since):
    served["calls"] += 1
    if served["calls"] == 1:
        return [ev(201, "notify", text="POLL-NOTIFY"),
                ev(202, "event", text="POLL-EVENT")], {"status": "active"}
    return [], {"status": "active"}


real_fetch = plugin._fetch_events
plugin._fetch_events = fake_fetch
poller = threading.Thread(target=plugin._poll_loop, args=(state,), daemon=True)
poller.start()
delivered = False
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline and not delivered:
    delivered = len(state.gateway.queue_calls) >= 1 and len(adapter.handled) >= 2
    time.sleep(0.02)
state.stop.set()
poller.join(timeout=2)
plugin._fetch_events = real_fetch
os.environ["TRIGGER_BUS_POLL_INTERVAL"] = "2"
check("l. poll tick queues the notify and interrupts for the event",
      len(state.gateway.queue_calls) == 1
      and state.gateway.queue_calls[0][1].internal is True
      and sorted(e.internal for e in adapter.handled) == [False, True],
      "queue_calls=%d handled_internal=%s polls=%d"
      % (len(state.gateway.queue_calls),
         [e.internal for e in adapter.handled], served["calls"]))

# =========================================================================== #
# m. post_llm_call clears the busy flag and dispatches what arrived
# =========================================================================== #

plugin.POST_TURN_DELAY = 0.05
state, adapter = new_state()
state.turn_active = True
state.turn_active_since = time.monotonic()
feed(state, ev(211, "event", text="AFTER-TURN"))
plugin._on_post_llm_call(session_id="s-m", turn_id="t-m",
                         assistant_response="done", platform="feishu")
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline and not adapter.handled:
    time.sleep(0.02)
check("m. post_llm_call clears busy and interrupts for a pending event",
      state.turn_active is False and len(adapter.handled) == 1
      and adapter.handled[0].internal is False,
      "turn_active=%s handled=%d"
      % (state.turn_active, len(adapter.handled)))

# =========================================================================== #
# k. compatibility with the REAL run_busy.py queue method (no gateway process)
# =========================================================================== #

from types import SimpleNamespace                        # noqa: E402

from gateway.run_busy import GatewayBusySessionMixin     # noqa: E402


class RealQueueStub(GatewayBusySessionMixin):
    """Calls the real ``_queue_or_replace_pending_event`` unbound.

    It brings its own ``_SECURITY_METADATA_KEYS`` and receipt semantics; the
    stub only supplies what that method reads off ``self``.
    """

    _BUSY_QUEUE_MAX_PENDING = 10

    def __init__(self, adapter):
        self.adapter = adapter
        self.adapters = {Platform.FEISHU: adapter}
        self._empty_state = SimpleNamespace(
            conversation=SimpleNamespace(queued_events=[]))

    def _delivery_adapter_for(self, source):
        return self.adapter

    def _peek_session_state(self, session_key):
        return self._empty_state

    def _session_state(self, session_key):
        return self._empty_state


state, adapter = new_state()
real_gateway = RealQueueStub(adapter)
state.gateway = real_gateway
state.source = SOURCE
feed(state, ev(191, "notify", text="REAL-METHOD"))
moved = plugin._notify_enqueue(state, plugin._get_logger(), state.events.copy(),
                               where="real")
check("k. real run_busy._queue_or_replace_pending_event accepts our event",
      moved is True and len(state.inflight) == 1, "moved=%s" % moved)
real_event = adapter._pending_messages.get(ROUTING_KEY)
check("k. real method parks it in adapter._pending_messages + receipt",
      real_event is not None and getattr(real_event, "_gateway_accepted", False) is True,
      "parked=%s accepted=%s"
      % (real_event is not None, getattr(real_event, "_gateway_accepted", None)))
check("k. real method keeps it internal=True and TEXT",
      real_event is not None and real_event.internal is True
      and getattr(real_event.message_type, "value", real_event.message_type) == "text",
      "internal=%s type=%s" % (getattr(real_event, "internal", None),
                               getattr(real_event, "message_type", None)))
plugin._on_pre_llm_call(platform="feishu", session_id="s-k",
                        user_message=real_event.text)
check("k. pre_llm_call proof advances the cursor after the real method",
      state.cursor == 191 and not state.inflight,
      "cursor=%d inflight=%d" % (state.cursor, len(state.inflight)))

# Two notifies while the turn runs: since 0.9.2 the second one joins the SAME
# busy window instead of opening a second HELD batch — one wake, one batch.
state, adapter = new_state()
real_gateway = RealQueueStub(adapter)
state.gateway = real_gateway
state.source = SOURCE
state.turn_active = True
state.turn_active_since = time.monotonic()
feed(state, ev(192, "notify", text="FIRST-QUEUED"))
plugin._dispatch(state, plugin._get_logger(), where="real-2")
feed(state, ev(193, "notify", text="SECOND-QUEUED"))
plugin._dispatch(state, plugin._get_logger(), where="real-2")
first_event = adapter._pending_messages.get(ROUTING_KEY)
overflow = real_gateway._empty_state.conversation.queued_events
check("k. two notifies while busy -> ONE held batch (0.9.2), no slot/overflow write",
      len(state.inflight) == 1 and first_event is None
      and not overflow
      and ids_of(state.events) == [192, 193]
      and all(d.phase == "held" for d in state.inflight.values())
      and len(next(iter(state.inflight.values())).events) == 2,
      "inflight=%d slot=%s overflow=%s local=%s"
      % (len(state.inflight), first_event is not None, len(overflow),
         ids_of(state.events)))

# =========================================================================== #
# o. an interrupt refused by the gateway ("another turn is running") is kept
# =========================================================================== #

state, adapter = new_state()
state.gateway.busy = True
adapter.reply_on_handle = "⏳ another turn is running, please resend"
feed(state, ev(231, "event", text="LEASE-REJECTED"))
offset = mark()
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("o. interrupt rejected by the lease -> event back in the local queue",
      ids_of(state.events) == [231] and state.cursor == 0
      and not state.inflight,
      "queue=%s cursor=%d inflight=%d"
      % (ids_of(state.events), state.cursor, len(state.inflight)))
check("o. the rejection is logged with the gateway reply",
      "gateway rejected the turn" in tail(offset) and "another turn is running" in tail(offset), "")

adapter.reply_on_handle = None
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("o. retry hands the same event over again",
      ids_of(state.events) == [231] and len(adapter.handled) == 2
      and state.inflight and state.cursor == 0,
      "local=%s handled=%d inflight=%d"
      % (ids_of(state.events), len(adapter.handled), len(state.inflight)))

# =========================================================================== #
# n. with a live loop captured, the queue call runs on the gateway's thread
# =========================================================================== #

import asyncio                                           # noqa: E402

LOOP = asyncio.new_event_loop()
LOOP_THREAD = threading.Thread(target=LOOP.run_forever, daemon=True)
LOOP_THREAD.start()
loop_ident = []
LOOP.call_soon_threadsafe(lambda: loop_ident.append(threading.get_ident()))
deadline = time.monotonic() + 2.0
while not loop_ident and time.monotonic() < deadline:
    time.sleep(0.01)

state, adapter = new_state()
state.loop = LOOP
# Idle enqueue: the only path that still calls the gateway (0.9.0 holds a busy
# notify plugin-side), so this still proves the loop-thread marshalling.
feed(state, ev(221, "notify", text="ON-THE-LOOP"))
plugin._dispatch(state, plugin._get_logger(), where="poll")
check("n. queue call executed on the gateway loop thread",
      len(state.gateway.queue_calls) == 1
      and state.gateway.queue_threads[0] == loop_ident[0],
      "call_thread=%s loop_thread=%s"
      % (state.gateway.queue_threads, loop_ident))
check("n. the receipt still comes back synchronously",
      len(state.inflight) == 1 and state.cursor == 0,
      "inflight=%d cursor=%d" % (len(state.inflight), state.cursor))
LOOP.call_soon_threadsafe(LOOP.stop)

# =========================================================================== #
# j. register() wires the four hooks
# =========================================================================== #

class FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn


plugin._STATE = None
ctx = FakeCtx()
plugin.register(ctx)
registered = sorted(ctx.hooks)
check("j. register() wires all four hooks",
      {"pre_llm_call", "post_llm_call", "transform_tool_result",
       "pre_gateway_dispatch"} <= set(registered),
      "hooks=%s" % registered)
if plugin._STATE is not None:
    plugin._STATE.stop.set()

# --------------------------------------------------------------------------- #
failed = [name for name, ok, _ in RESULTS if not ok]
print("\n%d/%d checks passed%s"
      % (len(RESULTS) - len(failed), len(RESULTS),
         "" if not failed else "; FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
