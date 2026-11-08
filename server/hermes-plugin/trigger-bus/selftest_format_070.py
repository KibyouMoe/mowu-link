#!/usr/bin/env python3
"""Card-format selftest for trigger-bus 0.7.0.

Covers the 1 / 3 / 8 event cases, field completeness, the 100-character clip,
newline collapsing, the mid-turn ``⚡ 立刻处理：`` insert and the wake text.
No gateway process, no trigger service, no network.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

HERMES = Path("/Users/kibyou/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES))
import hermes_bootstrap  # noqa: E402,F401  (runtime deps on sys.path)

TMP = Path(tempfile.mkdtemp(prefix="trigger-bus-format-"))
os.environ.pop("TRIGGER_BUS_DISABLED", None)
os.environ["TRIGGER_BUS_CURSOR"] = str(TMP / "cursor.json")
os.environ["TRIGGER_BUS_LOCK"] = str(TMP / "inactive.lock")
os.environ["TRIGGER_BUS_URL"] = "http://127.0.0.1:9"      # nothing listens

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("trigger_bus_format_under_test",
                                              HERE / "__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)

# The real log file must not be polluted by a test run.
plugin.LOG_PATH = TMP / "selftest.log"
plugin._ack_async = lambda *a, **k: None                  # no HTTP in tests
assert plugin.PLUGIN_VERSION.startswith("0."), plugin.PLUGIN_VERSION
assert plugin.TEXT_CLIP_CHARS == 100, plugin.TEXT_CLIP_CHARS
assert plugin.MAX_EXPANDED_EVENTS == 5, plugin.MAX_EXPANDED_EVENTS

from gateway.config import Platform                      # noqa: E402
from gateway.session import SessionSource                 # noqa: E402


class FakeAdapter:
    def __init__(self) -> None:
        self.events = []

    async def handle_message(self, event) -> None:
        self.events.append(event)


class FakeGateway:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapters = {Platform.FEISHU: adapter}


ADAPTER = FakeAdapter()
SOURCE = SessionSource(platform=Platform.FEISHU, chat_id="oc_selftest",
                       chat_type="dm", user_id="ou_selftest")
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print("%s  %-64s %s" % ("PASS" if ok else "FAIL", name, detail),
          flush=True)


def show(label: str, text: str) -> None:
    print("--- %s ---" % label)
    print(text)
    print("--- end: %d chars, %d lines ---\n"
          % (len(text), len(text.splitlines())))


def card(eid: int, level: str, source: str, actor: str, text: str,
         ts: str, **extra) -> dict:
    event = {"id": eid, "event_id": "fmt-%d" % eid, "level": level,
             "source": source, "kind": "chat", "actor": actor,
             "text": text, "ts": ts}
    event.update(extra)
    return event


def new_state():
    """A fresh _BusState bound to the fake gateway, cursor reset to 0."""
    for leftover in (os.environ["TRIGGER_BUS_CURSOR"],
                     os.environ["TRIGGER_BUS_CURSOR"] + ".tmp"):
        try:
            Path(leftover).unlink()
        except OSError:
            pass
    state = plugin._BusState()
    state.gateway = FakeGateway(ADAPTER)
    state.source = SOURCE
    state.loop = LOOP
    ADAPTER.events.clear()
    plugin._STATE = state
    return state


E1 = card(52, "event", "mc_chat", "Steve", "溪流 快来",
          "2026-11-08T20:01:00+08:00", thread_key="mc_chat:global")
E2 = card(53, "notify", "email", "HR", "周报：本周进度…",
          "2026-11-08T19:58:12+08:00", thread_key="email:a1b2")
E3 = card(54, "event", "mc_chat", "Alex", "这矿真难挖",
          "2026-11-08T20:00:40+08:00")
EXTRA = [card(55 + index, "notify", "discord", "Ops",
              "队列事件 %d" % (55 + index),
              "2026-11-08T20:%02d:00+08:00" % (2 + index))
         for index in range(5)]

HEAD_RE = re.compile(r"^#\d+ (?:event|notify) \S+ \d{2}:\d{2}"
                     r"(?: thr=\S+)?$")

print("trigger-bus %s card-format selftest — gateway-free" % plugin.PLUGIN_VERSION)
print("tmp/log: %s\n" % plugin.LOG_PATH)

# =========================================================================== #
# 1 event
# =========================================================================== #

single = plugin._format_events([E1])
show("1 event", single)
check("1. one event -> one card, no summary line",
      single == ("#52 event mc_chat/Steve 20:01 thr=mc_chat:global\n"
                 "> 溪流 快来"),
      "%d chars" % len(single))
check("1. header fields complete and in fixed order",
      single.splitlines()[0] ==
      "#52 event mc_chat/Steve 20:01 thr=mc_chat:global",
      single.splitlines()[0])
check("1. text is its own '> ' line",
      single.splitlines()[1] == "> 溪流 快来", single.splitlines()[1])

# field completeness / optional fields
head1 = plugin._format_event(E1).splitlines()[0]
head2 = plugin._format_event(E2).splitlines()[0]
head3 = plugin._format_event(E3).splitlines()[0]
check("fields. id/level/source/actor/time/thread on every header",
      all(HEAD_RE.match(plugin._format_event(e).splitlines()[0])
          for e in (E1, E2, E3)),
      "%s | %s | %s" % (head1, head2, head3))
check("fields. thread key rendered when present", "thr=email:a1b2" in head2,
      head2)
check("fields. no thr= when the event has no thread key",
      " thr=" not in head3, head3)
payload_copy = dict(E2)
payload_copy.pop("thread_key")
payload_copy["payload"] = {"thread_key": "email:a1b2"}
check("fields. thread_key falls back to payload",
      "thr=email:a1b2" in plugin._format_event(payload_copy), "")
no_actor = card(90, "info", "selftest", "", "no actor",
                "2026-11-08T09:05:00+08:00")
check("fields. empty actor -> source only, no slash",
      plugin._format_event(no_actor).splitlines()[0] ==
      "#90 info selftest 09:05", plugin._format_event(no_actor))
no_ts = card(91, "notify", "selftest", "Bot", "no ts", "")
check("fields. missing ts -> --:-- (header still 2 lines)",
      plugin._format_event(no_ts).splitlines()[0] ==
      "#91 notify selftest/Bot --:--", plugin._format_event(no_ts))
check("fields. every card is exactly 2 lines",
      all(len(plugin._format_event(e).splitlines()) == 2
          for e in (E1, E2, E3, no_actor, no_ts)), "")

# =========================================================================== #
# 3 events
# =========================================================================== #

three = plugin._format_events([E1, E2, E3])
show("3 events", three)
lines3 = three.splitlines()
check("3. batch header '[3 条]'", lines3[0] == "[3 条]", lines3[0])
check("3. all 3 cards expanded",
      sum(1 for line in lines3 if line.startswith("#")) == 3
      and len(lines3) == 7,
      "%d cards / %d lines" % (sum(1 for line in lines3
                                   if line.startswith("#")), len(lines3)))
check("3. no '+K more' when nothing is hidden",
      not any(line.startswith("+") for line in lines3), "")
check("3. each header is followed by its '> ' text line",
      all(lines3[index + 1].startswith("> ") for index in (1, 3, 5)), "")

# =========================================================================== #
# 8 events
# =========================================================================== #

eight = plugin._format_events([E1, E2, E3] + EXTRA)
show("8 events", eight)
lines8 = eight.splitlines()
cards8 = [line for line in lines8 if line.startswith("#")]
check("8. batch header '[8 条]'", lines8[0] == "[8 条]", lines8[0])
check("8. exactly 5 cards expanded", len(cards8) == 5,
      "%d cards" % len(cards8))
check("8. last line '+3 more'", lines8[-1] == "+3 more", lines8[-1])
check("8. 12 lines total (1 header + 5*2 + 1 tail)", len(lines8) == 12,
      "%d lines" % len(lines8))
check("8. hidden events 57/58/59 absent",
      not any("#%d " % eid in eight for eid in (57, 58, 59)), "")
check("8. visible events 52/53/54/55/56 present",
      all("#%d " % eid in eight for eid in (52, 53, 54, 55, 56)), "")

# =========================================================================== #
# clipping (100 chars + …)
# =========================================================================== #

long_text = "字" * 120
long_card = plugin._format_event(
    card(60, "event", "mc_chat", "Steve", long_text,
         "2026-11-08T20:07:00+08:00"))
payload = long_card.splitlines()[1][2:]          # drop "> "
show("120-char text", long_card)
check("clip. text after '> ' <= 101 chars (100 + …)", len(payload) <= 101,
      "text=%d" % len(payload))
check("clip. ellipsis appended", payload.endswith("…"), repr(payload[-3:]))
check("clip. exactly 100 chars kept", payload[:-1] == "字" * 100,
      "kept=%d" % (len(payload) - 1))
check("clip. whole line is 2 + <=101 chars", len(long_card.splitlines()[1]) <= 103,
      "line=%d" % len(long_card.splitlines()[1]))

emoji_text = "字" * 98 + "👨\u200d👩\u200d👧\u200d👦" + "尾" * 30
emoji_card = plugin._format_event(
    card(61, "event", "mc_chat", "Steve", emoji_text,
         "2026-11-08T20:08:00+08:00"))
emoji_payload = emoji_card.splitlines()[1][2:]
show("emoji at the clip boundary", emoji_card)
emoji_last = emoji_payload[:-1][-1:] if emoji_payload else ""
check("clip. no dangling ZWJ / variation selector / skin tone",
      emoji_last not in ("\u200d", "\ufe0e", "\ufe0f")
      and not ("\U0001F3FB" <= emoji_last <= "\U0001F3FF"),
      repr(emoji_payload[-4:]))
check("clip. no replacement char / lone surrogate",
      "\ufffd" not in emoji_payload
      and all(not (0xD800 <= ord(char) <= 0xDFFF)
              for char in emoji_payload), "")
check("clip. emoji clipped on a whole-code-point boundary",
      "👨" in emoji_payload and len(emoji_payload) <= 101,
      "text=%d" % len(emoji_payload))

multiline = plugin._format_event(
    card(62, "notify", "email", "HR", "第一行\n第二行\t第三行",
         "2026-11-08T20:09:00+08:00"))
check("clip. newlines/tabs collapse to spaces",
      multiline.splitlines()[1] == "> 第一行 第二行 第三行", multiline)

# =========================================================================== #
# seam 2: transform_tool_result (⚡ 立刻处理：)
# =========================================================================== #

state = new_state()
plugin._merge_events(state, [E1])
inserted = plugin._on_transform_tool_result(tool_name="terminal",
                                            result="output")
show("transform_tool_result insert", inserted or "")
check("transform. '⚡ 立刻处理：' on its own line before the card",
      inserted == "output\n\n⚡ 立刻处理：\n" + plugin._format_event(E1),
      repr((inserted or "")[:40]))
check("transform. card header follows the prefix",
      bool(inserted) and inserted.splitlines()[3] ==
      "#52 event mc_chat/Steve 20:01 thr=mc_chat:global",
      inserted.splitlines()[3] if inserted else "")
check("transform. at most once per event",
      plugin._on_transform_tool_result(tool_name="terminal",
                                       result="output") is None, "")

# =========================================================================== #
# seam 1: pre_llm_call context
# =========================================================================== #

state = new_state()
plugin._merge_events(state, [E1, E2, E3])
injected = plugin._on_pre_llm_call(platform="feishu", session_id="s-fmt",
                                   user_message="hi")
context = injected.get("context", "") if isinstance(injected, dict) else ""
show("pre_llm_call context", context)
check("pre_llm_call. context is the same 3-card batch", context == three,
      "%d chars" % len(context))
check("pre_llm_call. cursor advanced and queue drained",
      state.cursor >= 54 and not state.events,
      "cursor=%d queue=%d" % (state.cursor, len(state.events)))

# =========================================================================== #
# seam 3: wake MessageEvent.text
# =========================================================================== #

state = new_state()
plugin._merge_events(state, [E1, E2, E3])
woke = plugin._wake(state, [E1, E2, E3])
deadline = time.monotonic() + 3.0
while not ADAPTER.events and time.monotonic() < deadline:
    time.sleep(0.02)
message = ADAPTER.events[0] if ADAPTER.events else None
show("wake MessageEvent.text", message.text if message else "")
# 0.8.1: the wake is the origin line only; the cards are injected by
# pre_llm_call in the turn this wake starts (format unchanged: `three`).
HEADER = "⚡ 来自 mc_chat · Steve 的打断"
check("wake. text is the interrupter line only (no cards)",
      woke and message is not None and message.text == HEADER,
      "%r" % (message.text if message else ""))
check("wake. the 3 cards stay in the local queue for pre_llm_call",
      [e.get("id") for e in state.events] == [52, 53, 54],
      "local=%s" % [e.get("id") for e in state.events])
check("wake. MessageEvent carries no metadata (body-only origin)",
      message is not None and not getattr(message, "metadata", None),
      "metadata=%r" % (getattr(message, "metadata", "<none>"),))
check("wake. event level still internal=False (steer semantics unchanged)",
      message is not None and message.internal is False,
      "internal=%s" % (message.internal if message else None))
check("wake. no markdown table characters", "|" not in (message.text
                                                        if message else "|"),
      "")

# --------------------------------------------------------------------------- #
failed = [name for name, ok, _ in RESULTS if not ok]
print("\n%d/%d checks passed%s"
      % (len(RESULTS) - len(failed), len(RESULTS),
         "" if not failed else "; FAILED: " + ", ".join(failed)))
sys.exit(1 if failed else 0)
