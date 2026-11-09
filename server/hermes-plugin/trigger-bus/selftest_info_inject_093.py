#!/usr/bin/env python3
"""Function-level selftest for trigger-bus 0.9.3 — info 静默注入上下文（不唤醒）.

The contract this suite pins down:

    拉取：GET /events?levels=event,notify,info  (one round-trip, no extra call)
    有回合：pre_llm_call 把未注入的 info 拼进上下文并推进游标
    无回合：不 wake、不排队、不发任何可见消息

Covers:
  a. idle-turn injection: 3 info rows -> ONE ``— info —`` section, cursor
     advances to the max id, zero sends, zero handle_message calls
  b. no turn running: info arrives -> _dispatch returns False, nothing is
     queued/woken/sent, the cursor never moves; post_llm_call starts no
     dispatch; transform_tool_result never inserts info
  c. 12 info rows -> 10 injected now, 2 kept and injected on the NEXT turn
     (truncation never drops a row), cursor 0 -> 10 -> 12
  d. event/notify + info in the same turn: cards first, ``— info —`` last,
     each level injected exactly once (second call injects nothing)
  e. format details: single card has no ``[1 条 info]`` header, ``· info``
     level slot, 100-char clip, pull asks for info in the same request
  f. version files: plugin.yaml / README are 0.9.3 and state the boundary
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

HERMES = Path("/Users/kibyou/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES))
import hermes_bootstrap  # noqa: E402,F401  (puts the runtime deps on sys.path)

TMP = Path(tempfile.mkdtemp(prefix="trigger-bus-selftest-093-"))
os.environ.pop("TRIGGER_BUS_DISABLED", None)
os.environ["TRIGGER_BUS_CURSOR"] = str(TMP / "cursor.json")
os.environ["TRIGGER_BUS_LOCK"] = str(TMP / "inactive.lock")
os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:9"      # nothing listens
os.environ.pop("TRIGGER_BUS_LEVELS", None)

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_bus_under_test_093",
                                              HERE / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)

plugin.LOG_PATH = TMP / "selftest.log"
plugin._ack_async = lambda *a, **k: None                  # no HTTP in tests
assert plugin.PLUGIN_VERSION == "0.9.3", plugin.PLUGIN_VERSION
assert plugin.MAX_INFO_PER_INJECT == 10, plugin.MAX_INFO_PER_INJECT

from gateway.config import Platform                      # noqa: E402
from gateway.session import SessionSource                 # noqa: E402

SOURCE = SessionSource(platform=Platform.FEISHU, chat_id="oc_selftest",
                       chat_type="dm", user_id="ou_selftest")
ROUTING_KEY = "agent:main:feishu:dm:oc_selftest"


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


class FakeResponse:
    def __init__(self, payload) -> None:
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self, _limit=None):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records the request URL so the levels parameter is really inspected."""

    def __init__(self, payload) -> None:
        self.payload = payload
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        return FakeResponse(self.payload)


class MemoryHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:                                  # noqa: BLE001
            pass


RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print("%s  %-72s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def new_state(busy=False):
    plugin._announce_reset()
    for leftover in (os.environ["TRIGGER_BUS_CURSOR"],
                     os.environ["TRIGGER_BUS_CURSOR"] + ".tmp"):
        try:
            Path(leftover).unlink()
        except OSError:
            pass
    state = plugin._BusState()
    adapter = FakeAdapter()
    state.gateway = FakeGateway(adapter)
    state.gateway.busy = busy
    state.gateway.session_authority = object()
    state.source = SOURCE
    state.loop = None
    plugin._STATE = state
    return state, adapter


def info(eid, text="", source="mc_chat", actor="Steve"):
    return {"id": eid, "level": "info", "source": source, "actor": actor,
            "kind": "note", "ts": "2026-11-09T11:44:00+08:00",
            "text": text or ("INFO-%d" % eid)}


def ev(eid, level="notify", source="email", actor="HR", text=""):
    return {"id": eid, "level": level, "source": source, "actor": actor,
            "kind": "mail", "ts": "2026-11-09T11:44:00+08:00",
            "text": text or ("%s-%d" % (level.upper(), eid))}


def feed(state, *events):
    return plugin._merge_events(state, list(events))


def inject():
    return plugin._on_pre_llm_call(platform="feishu", session_id="s-093",
                                   user_message="hi")


def body_of(context):
    return context.get("context", "") if isinstance(context, dict) else ""


memory = MemoryHandler()
plugin._get_logger().addHandler(memory)

print("trigger-bus %s selftest — info 静默注入上下文（不唤醒）, gateway-free"
      % plugin.PLUGIN_VERSION)
print("tmp/log: %s\n" % plugin.LOG_PATH)

# =========================================================================== #
# a. active turn + 3 info -> ONE injection, cursor to max id, zero sends
# =========================================================================== #

state, adapter = new_state()
feed(state, info(1, "FIRST"), info(2, "SECOND"), info(3, "THIRD"))
check("a. info never enters the wakeable queue", plugin._unread_locked(state) == []
      and list(state.events) == [] and len(state.info_events) == 3,
      "events=%r info=%d" % (list(state.events), len(state.info_events)))
check("a. pulling info alone does not move the cursor",
      state.cursor == 0 and state.high_water == 3, "cursor=%d hw=%d"
      % (state.cursor, state.high_water))

context = inject()
body = body_of(context)
cards = [line for line in body.splitlines() if line.startswith("#")]
check("a. 3 info -> one [3 条 info] section with 3 cards",
      body.startswith("— info —\n[3 条 info]") and len(cards) == 3,
      "first=%r cards=%d" % (body.splitlines()[:2], len(cards)))
check("a. cursor advanced to the max id (3) after the real injection",
      state.cursor == 3 and state.info_cursor == 3, "cursor=%d info_cursor=%d"
      % (state.cursor, state.info_cursor))
check("a. nothing was woken, queued or sent",
      adapter.handled == [] and adapter.sent == []
      and state.gateway.queue_calls == [] and not state.inflight,
      "handled=%d sent=%d queue_calls=%d"
      % (len(adapter.handled), len(adapter.sent), len(state.gateway.queue_calls)))
check("a. the next turn injects nothing (at most once)",
      inject() is None and len(state.info_events) == 0,
      "info_left=%d" % len(state.info_events))

# =========================================================================== #
# b. no turn running: info arrives -> no wake / queue / send, cursor still
# =========================================================================== #

state, adapter = new_state()
feed(state, info(4, "WHILE-IDLE"))
did = plugin._dispatch(state, plugin._get_logger(), where="poll")
check("b. info alone -> _dispatch False, no handle_message, no send",
      did is False and adapter.handled == [] and adapter.sent == []
      and state.gateway.queue_calls == [], "did=%r handled=%d sent=%d"
      % (did, len(adapter.handled), len(adapter.sent)))
check("b. info stays in its backlog, cursor untouched, nothing in flight",
      len(state.info_events) == 1 and state.cursor == 0
      and not state.inflight and not state.woken_ids,
      "info=%d cursor=%d inflight=%d" % (len(state.info_events),
                                         state.cursor, len(state.inflight)))

post_calls = []
real_post = plugin._post_turn_dispatch
plugin._post_turn_dispatch = lambda *a, **k: post_calls.append(a)
try:
    plugin._on_post_llm_call(platform="feishu", session_id="s-093",
                             turn_id="t-093")
finally:
    plugin._post_turn_dispatch = real_post
check("b. a turn ending starts no dispatch for info (never an empty wake)",
      post_calls == [] and len(state.info_events) == 1,
      "post_calls=%d info=%d" % (len(post_calls), len(state.info_events)))

tool = plugin._on_transform_tool_result(tool_name="read_file", result="OUT")
check("b. transform_tool_result never inserts info",
      tool is None and plugin._take_interrupt(state) is None
      and len(state.info_events) == 1,
      "tool=%r info=%d" % (tool, len(state.info_events)))

# =========================================================================== #
# c. 12 info -> 10 now, 2 next turn (truncation keeps the rest)
# =========================================================================== #

state, adapter = new_state()
feed(state, *[info(eid, "ROW-%d" % eid) for eid in range(1, 13)])
first_body = body_of(inject())
first_ids = [line.split()[0] for line in first_body.splitlines()
             if line.startswith("#")]
check("c. 12 info -> first turn injects exactly 10 cards",
      first_body.startswith("— info —\n[10 条 info]") and len(first_ids) == 10,
      "header=%r cards=%d" % (first_body.splitlines()[:1], len(first_ids)))
check("c. the remaining 2 stay queued, cursor 0 -> 10",
      len(state.info_events) == 2 and state.cursor == 10
      and [e["id"] for e in state.info_events] == [11, 12],
      "left=%r cursor=%d" % ([e["id"] for e in state.info_events],
                             state.cursor))
second_body = body_of(inject())
second_ids = [line.split()[0] for line in second_body.splitlines()
              if line.startswith("#")]
check("c. the next turn injects the remaining 2, cursor 10 -> 12",
      second_body.startswith("— info —\n[2 条 info]")
      and second_ids == ["#11", "#12"] and state.cursor == 12
      and len(state.info_events) == 0,
      "header=%r ids=%r cursor=%d" % (second_body.splitlines()[:1],
                                      second_ids, state.cursor))
check("c. no send / wake on either info injection",
      adapter.sent == [] and adapter.handled == [], "sent=%d handled=%d"
      % (len(adapter.sent), len(adapter.handled)))

# =========================================================================== #
# d. event + notify + info in one turn: cards first, — info — last, once each
# =========================================================================== #

state, adapter = new_state()
feed(state, ev(20, level="event", source="mc_chat", actor="Steve",
               text="HARD"),
     ev(21, level="notify", text="MAIL"), info(22, "NOTE"))
context = inject()
body = body_of(context)
sections = body.split("\n\n")
check("d. event/notify cards first, then — info — as its own last section",
      len(sections) == 2
      and sections[0].startswith("[2 条]\n#20 event ")
      and "\n#21 notify " in sections[0]
      and sections[1].startswith("— info —\n#22 · info "),
      "sections=%r" % [s.splitlines()[0] for s in sections])
check("d. the info section comes after every event/notify card",
      body.index("#21 notify ") < body.index("— info —")
      and body.index("— info —") == len(sections[0]) + 2,
      "info_at=%d len_cards=%d" % (body.index("— info —"), len(sections[0])))
check("d. every level left its queue after the one injection",
      list(state.events) == [] and len(state.info_events) == 0
      and state.cursor == 22, "events=%d info=%d cursor=%d"
      % (len(state.events), len(state.info_events), state.cursor))
check("d. a second injection is empty (no duplicate cards)",
      inject() is None, "")
check("d. info still sent no visible message",
      adapter.sent == [], "sent=%r" % (adapter.sent,))

# =========================================================================== #
# e. format + pull details
# =========================================================================== #

state, adapter = new_state()
feed(state, info(30, "ONLY-ONE"))
single = body_of(inject())
check("e. a single info card has no [1 条 info] header line",
      single.startswith("— info —\n#30 · info ") and "条 info" not in single,
      "first=%r" % single.splitlines()[:2])

long_text = "长" * 300
state, adapter = new_state()
feed(state, info(31, long_text))
line = [l for l in body_of(inject()).splitlines() if l.startswith("> ")][0]
check("e. info text is clipped at 100 chars + … (same rule as event/notify)",
      line == "> " + "长" * 100 + "…", "len=%d tail=%r"
      % (len(line), line[-6:]))

state, adapter = new_state()
feed(state, info(32, "THREADED"))
state.info_events[0]["thread_key"] = "mc_chat:global"
head = body_of(inject()).splitlines()[1]
check("e. info card keeps the standard thr=<thread_key> suffix",
      head == "#32 · info mc_chat/Steve 11:44 thr=mc_chat:global",
      "head=%r" % head)

# The pull must name info explicitly (the server defaults hermes* to
# event,notify), in the SAME request.
state, adapter = new_state()
opener = FakeOpener({"events": [info(40, "PULLED")],
                     "subscription": {"status": "active", "expired": False}})
real_opener = plugin._OPENER
plugin._OPENER = opener
try:
    pulled, _sub = plugin._fetch_events(state, 0)
finally:
    plugin._OPENER = real_opener
url = opener.urls[0] if opener.urls else ""
check("e. one GET /events asks for levels=event,notify,info",
      len(opener.urls) == 1 and "levels=event%2Cnotify%2Cinfo" in url,
      "url=%r" % url)
check("e. the same response returns the info row (one round-trip)",
      [e["id"] for e in pulled] == [40]
      and plugin._level(pulled[0]) == "info", "ids=%r" % [e["id"] for e in pulled])
check("e. TRIGGER_BUS_LEVELS default is event,notify,info",
      state.pull_levels == "event,notify,info", "levels=%r" % state.pull_levels)

# log evidence: the journal says info was injected with its ids
check("e. journal records 'info INJECT' with ids and remaining count",
      any(line.startswith("info INJECT n=1 ids=30")
          for line in memory.lines), "")

# =========================================================================== #
# f. version files
# =========================================================================== #

yaml_text = (HERE / "plugin.yaml").read_text(encoding="utf-8")
readme = (HERE / "README.md").read_text(encoding="utf-8")
check("f. plugin.yaml is 0.9.3", "version: 0.9.3" in yaml_text, "")
check("f. plugin.yaml states 「info 静默入上下文」 and the no-wake boundary",
      "info 静默入上下文" in yaml_text and "never wakes" in yaml_text.lower(), "")
check("f. README is titled 0.9.3", readme.startswith("# trigger-bus (0.9.3)"),
      readme.splitlines()[0] if readme else "")
check("f. README has the 0.9.3 section and the boundary rules",
      "## 0.9.3 — info 静默入上下文（不唤醒）" in readme
      and "— info —" in readme and "MAX_INFO_PER_INJECT" in readme, "")

# --------------------------------------------------------------------------- #
plugin._get_logger().removeHandler(memory)
failed = [name for name, ok, _ in RESULTS if not ok]
print("\n%d/%d checks passed%s"
      % (len(RESULTS) - len(failed), len(RESULTS),
         "" if not failed else "; FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
