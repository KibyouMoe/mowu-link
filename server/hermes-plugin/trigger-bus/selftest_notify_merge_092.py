#!/usr/bin/env python3
"""Function-level selftest for trigger-bus 0.9.2 — 忙时多条 notify 合并为一次唤醒.

The defect this suite pins down (field log, 2026-11-08):

    23:23:47 notify NATIVE-QUEUED n=1 ids=78 busy=1
    23:23:58 notify NATIVE-QUEUED n=1 ids=79 busy=1
    23:24:08 notify NATIVE-QUEUED n=1 ids=80 busy=1
    23:24:30 injected 3 event(s) into pre_llm_call context (cursor=80)
    23:24:40 conversation turn: msg='📬 收到来自 selftest 的 Notify 消息'  (empty)
    23:24:51 conversation turn: msg='📬 收到来自 selftest 的 Notify 消息'  (empty)

Every busy notify was admitted separately, so the first turn injected the
whole queue and the other two woke with nothing to inject.  Since 0.9.2 the
first notify opens ONE merge window, later notifies only grow that batch, and
the window ends in one wake / one injection / one 📬.

    /Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 \\
        selftest_notify_merge_092.py

Covers:
  a. three notifies during one turn: handle_message/admit called ONCE; the
     two later ones are merged (journal ``notify MERGED``), silent, cursor 0
  b. the window's turn injects all three as ONE ``[3 条]`` card and moves the
     cursor 0 -> 3 in that single injection
  c. one notice message: one ``📬`` line per event (max 3 lines, the 3rd
     carrying ``…x(X-2)`` when the window is longer)
  d. after the injection no extra wake/admit happens (total == 1)
  e. unchanged paths: idle notify drains immediately; busy event still
     interrupts at once (internal=False, immediate ⚡)
  f. the HELD route (no session authority) merges the same way — one wake
  g. a window whose cards were already injected elsewhere is retired without
     an empty wake
  h. plugin.yaml / README are 0.9.2 and state 「忙时多条合并为一次唤醒」
  i. the notice shape: X=1 (one line) / X=3 (3 lines) / X=4 (3 lines, …x2) /
     X=100 (3 lines, …x98), the multi-line block deduped as a whole
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

TMP = Path(tempfile.mkdtemp(prefix="trigger-bus-selftest-092-"))
os.environ.pop("TRIGGER_BUS_DISABLED", None)
os.environ["TRIGGER_BUS_CURSOR"] = str(TMP / "cursor.json")
os.environ["TRIGGER_BUS_LOCK"] = str(TMP / "inactive.lock")
os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:9"      # nothing listens

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_bus_under_test_092",
                                              HERE / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)

plugin.LOG_PATH = TMP / "selftest.log"
plugin._ack_async = lambda *a, **k: None                  # no HTTP in tests
assert plugin.PLUGIN_VERSION == "0.9.2", plugin.PLUGIN_VERSION

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
    """Capture every record in order — the suite asserts the log sequence."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:                                  # noqa: BLE001
            pass

    def find(self, needle: str, start: int = 0):
        return [i for i, line in enumerate(self.lines[start:], start)
                if needle in line]


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
            "kind": "mail", "ts": "2026-11-08T23:24:00+08:00",
            "text": text or ("%s-%d" % (level.upper(), eid))}


def feed(state, *events):
    return plugin._merge_events(state, list(events))


def inject():
    return plugin._on_pre_llm_call(platform="feishu", session_id="s-092",
                                   user_message="hi")


def ids(state):
    return [e.get("id") for e in state.events]


memory = MemoryHandler()
plugin._get_logger().addHandler(memory)

print("trigger-bus %s selftest — 忙时多条 notify 合并为一次唤醒, gateway-free"
      % plugin.PLUGIN_VERSION)
print("tmp/log: %s\n" % plugin.LOG_PATH)

# =========================================================================== #
# a. three notifies during one turn -> ONE admission, later ones merged
# =========================================================================== #

state, adapter = new_state()
start = len(memory.lines)
feed(state, ev(1, text="FIRST"))
ok = plugin._dispatch(state, plugin._get_logger(), where="poll-1")
window = next(iter(state.inflight.values()), None)
check("a. the FIRST busy notify opens exactly one native admission",
      ok is True and len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is True
      and not state.gateway.queue_calls and not adapter._pending_messages,
      "ok=%r handled=%d queue_calls=%d slot=%s"
      % (ok, len(adapter.handled), len(state.gateway.queue_calls),
         list(adapter._pending_messages)))
check("a. it is silent and the cursor stays at 0 while queued",
      adapter.sent == [] and state.cursor == 0 and window is not None
      and window.native is True,
      "sent=%r cursor=%d native=%r"
      % (adapter.sent, state.cursor, getattr(window, "native", None)))

for eid in (2, 3):
    feed(state, ev(eid, text="LATER-%d" % eid))
    plugin._dispatch(state, plugin._get_logger(), where="poll-%d" % eid)
check("a. 3 busy notifies -> handle_message called ONLY once",
      len(adapter.handled) == 1 and len(state.inflight) == 1
      and len(window.events) == 3,
      "handled=%d inflight=%d batch=%s"
      % (len(adapter.handled), len(state.inflight),
         [e.get("id") for e in window.events]))
merged = memory.find("notify MERGED", start)
check("a. both later notifies are accumulated in the window (not re-admitted)",
      len(merged) == 2 and "total=2" in memory.lines[merged[0]]
      and "total=3" in memory.lines[merged[1]]
      and all("woke=window(no new turn)" in memory.lines[i] for i in merged),
      "merged=%r" % ([memory.lines[i] for i in merged],))
check("a. no second NATIVE-QUEUED / no send while merging",
      len(memory.find("NATIVE-QUEUED", start)) == 1 and adapter.sent == []
      and state.cursor == 0 and ids(state) == [1, 2, 3],
      "queued_logs=%d sent=%r cursor=%d local=%s"
      % (len(memory.find("NATIVE-QUEUED", start)), adapter.sent,
         state.cursor, ids(state)))

for i in range(3):
    plugin._dispatch(state, plugin._get_logger(), where="busy-tick-%d" % i)
check("a. three more ticks while the turn runs add no wake/admit/send",
      len(adapter.handled) == 1 and adapter.sent == [] and state.cursor == 0,
      "handled=%d sent=%r cursor=%d"
      % (len(adapter.handled), adapter.sent, state.cursor))

# =========================================================================== #
# b. the one turn injects the whole window: one card, cursor 0 -> 3
# =========================================================================== #

window_token = window.token
state.gateway.busy = False
context = inject()
body = context.get("context", "") if isinstance(context, dict) else ""
heads = [line for line in body.splitlines() if line.startswith("#")]
check("b. ONE injection carries all three ids in a single [3 条] card",
      context is not None and body.startswith("[3 条]")
      and len(heads) == 3 and [h.split()[0] for h in heads] == ["#1", "#2", "#3"],
      "body=%r" % (body,))
check("b. the cursor jumps 0 -> 3 in that one step (no intermediate)",
      state.cursor == 3 and not state.events and not state.inflight
      and state.notify_window is None,
      "cursor=%d queue=%d inflight=%d window=%r"
      % (state.cursor, len(state.events), len(state.inflight),
         state.notify_window))
check("b. the window delivery is proven and retired",
      state.inflight.get(window_token) is None,
      "token=%r inflight=%r" % (window_token, list(state.inflight)))

# =========================================================================== #
# c. one notice message: one 📬 line per event (0.9.2)
# =========================================================================== #

check("c. one notice message, one 📬 line per event of the window",
      adapter.sent == [(SOURCE.chat_id,
                        "\n".join([NOTIFY_TEXT] * 3))],
      "sent=%r" % (adapter.sent,))

# =========================================================================== #
# d. after the injection there is no extra wake/admit anywhere
# =========================================================================== #

for i in range(4):
    plugin._dispatch(state, plugin._get_logger(), where="post-%d" % i)
check("d. no turn is opened after delivery (total wake/admit == 1)",
      len(adapter.handled) == 1
      and len(memory.find("woke session internal=True", start)) == 1
      and len(memory.find("NATIVE-QUEUED", start)) == 1
      and len(adapter.sent) == 1
      and adapter.sent[0] == (SOURCE.chat_id, "\n".join([NOTIFY_TEXT] * 3))
      and state.cursor == 3,
      "handled=%d wakes=%d admissions=%d sent=%d cursor=%d"
      % (len(adapter.handled), len(memory.find("woke session internal=True",
                                               start)),
         len(memory.find("NATIVE-QUEUED", start)), len(adapter.sent),
         state.cursor))

# =========================================================================== #
# e. unchanged: idle notify is immediate; busy event still interrupts
# =========================================================================== #

state, adapter = new_state(busy=False)
feed(state, ev(20, text="IDLE"))
ok = plugin._dispatch(state, plugin._get_logger(), where="idle")
check("e. idle notify still drains in the same tick (one wake, no line yet)",
      ok is True and len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is True
      and adapter.sent == [],
      "ok=%r handled=%d internal=%r sent=%r"
      % (ok, len(adapter.handled),
         getattr(adapter.handled[0], "internal", None), adapter.sent))
inject()
check("e. its one 📬 arrives with the injection and moves the cursor",
      adapter.sent == [(SOURCE.chat_id, NOTIFY_TEXT)] and state.cursor == 20,
      "sent=%r cursor=%d" % (adapter.sent, state.cursor))

state, adapter = new_state(busy=True)
feed(state, ev(21, "event", source="mc_chat", actor="Steve", text="EV"))
ok = plugin._dispatch(state, plugin._get_logger(), where="event")
check("e. busy event still interrupts at once (internal=False)",
      ok is True and len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is False,
      "ok=%r handled=%d internal=%r"
      % (ok, len(adapter.handled),
         getattr(adapter.handled[0], "internal", None)))
check("e. the ⚡ line is sent immediately with that wake",
      adapter.sent == [(SOURCE.chat_id, EVENT_TEXT_MC)],
      "sent=%r" % (adapter.sent,))
inject()
check("e. the event injection adds no 📬 and moves the cursor once",
      adapter.sent == [(SOURCE.chat_id, EVENT_TEXT_MC)] and state.cursor == 21,
      "sent=%r cursor=%d" % (adapter.sent, state.cursor))

# =========================================================================== #
# f. HELD route (no session authority): same merge, one wake
# =========================================================================== #

state, adapter = new_state(authority=False)
feed(state, ev(30, text="HELD-1"))
plugin._dispatch(state, plugin._get_logger(), where="held-1")
held = next(iter(state.inflight.values()), None)
check("f. no authority: the first notify is HELD (no wake yet)",
      held is not None and held.phase == "held" and not adapter.handled,
      "phase=%r handled=%d" % (getattr(held, "phase", None),
                               len(adapter.handled)))
for eid in (31, 32):
    feed(state, ev(eid, text="HELD-%d" % eid))
    plugin._dispatch(state, plugin._get_logger(), where="held-%d" % eid)
check("f. later notifies merge into that one HELD batch (still no wake)",
      not adapter.handled and len(state.inflight) == 1
      and [e.get("id") for e in held.events] == [30, 31, 32]
      and held.phase == "held",
      "handled=%d inflight=%d batch=%s phase=%r"
      % (len(adapter.handled), len(state.inflight),
         [e.get("id") for e in held.events], held.phase))
state.gateway.busy = False
plugin._dispatch(state, plugin._get_logger(), where="held-idle")
check("f. the turn-end drain opens ONE wake for the whole window",
      len(adapter.handled) == 1
      and getattr(adapter.handled[0], "internal", None) is True
      and adapter.sent == [],
      "handled=%d internal=%r sent=%r"
      % (len(adapter.handled),
         getattr(adapter.handled[0], "internal", None), adapter.sent))
inject()
check("f. one [3 条] injection, cursor 0 -> 32, one 3-line notice",
      state.cursor == 32
      and adapter.sent == [(SOURCE.chat_id, "\n".join([NOTIFY_TEXT] * 3))]
      and len(adapter.handled) == 1,
      "cursor=%d sent=%r handled=%d"
      % (state.cursor, adapter.sent, len(adapter.handled)))

# =========================================================================== #
# g. a window already injected elsewhere is retired, never woken empty
# =========================================================================== #

state, adapter = new_state(authority=False)
feed(state, ev(40, text="ALREADY-INJECTED"))
plugin._dispatch(state, plugin._get_logger(), where="gone-1")
delivery = next(iter(state.inflight.values()), None)
state.gateway.busy = False
with state.lock:
    state.events.clear()          # another turn injected these cards
status = plugin._drain_held(state, plugin._get_logger(), delivery,
                            where="selftest")
check("g. no cards left -> the held window is retired with NO wake",
      status == "empty" and not adapter.handled and adapter.sent == []
      and not state.inflight,
      "status=%r handled=%d sent=%r inflight=%d"
      % (status, len(adapter.handled), adapter.sent, len(state.inflight)))

# =========================================================================== #
# i. notice shape: X=1 / X=3 / X=4 / X=100 (max 3 lines, …x(X-2) on the 3rd)
# =========================================================================== #

def batch(n):
    return [ev(600 + i) for i in range(n)]


one = plugin._announce_batch_text(batch(1))
check("i. X=1 -> exactly one line, no suffix",
      one == NOTIFY_TEXT and "\n" not in one, "text=%r" % (one,))
three = plugin._announce_batch_text(batch(3)).splitlines()
check("i. X=3 -> 3 lines, no suffix",
      len(three) == 3 and all(line == NOTIFY_TEXT for line in three),
      "lines=%d %r" % (len(three), three))
four = plugin._announce_batch_text(batch(4)).splitlines()
check("i. X=4 -> 3 lines, the 3rd ends with …x2",
      len(four) == 3 and four[0] == NOTIFY_TEXT and four[1] == NOTIFY_TEXT
      and four[2] == NOTIFY_TEXT + "…x2",
      "lines=%d %r" % (len(four), four))
hundred = plugin._announce_batch_text(batch(100)).splitlines()
check("i. X=100 -> 3 lines, the 3rd ends with …x98",
      len(hundred) == 3 and hundred[2].endswith("…x98")
      and hundred[2] == NOTIFY_TEXT + "…x98",
      "lines=%d last=%r" % (len(hundred), hundred[-1] if hundred else None))

# integration: an X=4 window goes out as one 3-line message
state, adapter = new_state()
for eid in (41, 42, 43, 44):
    feed(state, ev(eid, text="FOUR-%d" % eid))
    plugin._dispatch(state, plugin._get_logger(), where="four-%d" % eid)
check("i. X=4 window: one wake, one 4-card injection, one 3-line notice",
      len(adapter.handled) == 1, "handled=%d" % len(adapter.handled))
state.gateway.busy = False
context = inject()
body = context.get("context", "") if isinstance(context, dict) else ""
sent_lines = adapter.sent[0][1].splitlines() if adapter.sent else []
check("i. the X=4 delivery: cursor 0 -> 44, 3 lines, …x2 on the last",
      state.cursor == 44 and body.startswith("[4 条]")
      and len(adapter.sent) == 1 and len(sent_lines) == 3
      and sent_lines[2] == NOTIFY_TEXT + "…x2",
      "cursor=%d sent=%r body_first=%r"
      % (state.cursor, adapter.sent, body.splitlines()[:1]))

# the whole multi-line block is ONE dedupe key
clock = [9000.0]
plugin._announce_clock = lambda: clock[0]
try:
    plugin._announce_reset()
    state, adapter = new_state(busy=False)
    block3 = plugin._announce_batch_text(batch(3))
    first = plugin._send_notice_text(state, adapter, block3,
                                     plugin._get_logger())
    second = plugin._send_notice_text(state, adapter, block3,
                                      plugin._get_logger())
    check("i. the full multi-line text is deduped as a whole (2 s window)",
          first == "sent:send_message" and second == "skipped:duplicate"
          and len(adapter.sent) == 1
          and (SOURCE.chat_id, block3) in plugin._announce_recent,
          "first=%r second=%r sent=%d" % (first, second, len(adapter.sent)))
    block4 = plugin._announce_batch_text(batch(4))
    third = plugin._send_notice_text(state, adapter, block4,
                                     plugin._get_logger())
    check("i. a different multi-line block is not a duplicate",
          third == "sent:send_message" and len(adapter.sent) == 2,
          "third=%r sent=%d" % (third, len(adapter.sent)))
finally:
    plugin._announce_clock = time.monotonic
    plugin._announce_reset()

# =========================================================================== #
# h. version files: 0.9.2 + the merge rule
# =========================================================================== #

yaml_text = (HERE / "plugin.yaml").read_text(encoding="utf-8")
readme = (HERE / "README.md").read_text(encoding="utf-8")
check("h. plugin.yaml is 0.9.2", "version: 0.9.2" in yaml_text, "")
check("h. plugin.yaml states 「忙时多条 notify 合并为一次唤醒」",
      "忙时多条 notify 合并为一次唤醒" in yaml_text, "")
check("h. README is titled 0.9.2", readme.startswith("# trigger-bus (0.9.2)"),
      readme.splitlines()[0] if readme else "")
check("h. README has the 0.9.2 section and the one-window rule",
      "## 0.9.2 — 忙时多条 notify 合并为一次唤醒" in readme
      and "notify MERGED" in readme
      and "收到来自 <source> · <actor> 的 Notify 消息" in readme
      and "收到来自 <source> · <actor> 的 Event 消息" in readme, "")

# --------------------------------------------------------------------------- #
plugin._get_logger().removeHandler(memory)
failed = [name for name, ok, _ in RESULTS if not ok]
print("\n%d/%d checks passed%s"
      % (len(RESULTS) - len(failed), len(RESULTS),
         "" if not failed else "; FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
