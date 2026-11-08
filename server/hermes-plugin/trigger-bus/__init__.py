"""trigger-bus — pull external events and inject them into the current turn.

Architecture: a separate long-lived service (``mowu-trigger``, loopback
127.0.0.1:8791) owns ingestion and storage. This plugin is only a consumer:

  * a daemon thread polls ``GET /events`` every 2 s for the subscriber
    ``hermes-main`` (configurable) starting after a persisted cursor;
  * the ``{subscription}`` block of that response drives the subscription
    cycle: ``renew_required`` -> ``POST /renew`` immediately, so the pull path
    never dies of an expired clock; ``expired`` -> clear the in-memory queue,
    log one WARNING, then renew before the next poll;
  * new events are formatted as a compact two-line card each and buffered in
    memory (deque, max 50 — oldest dropped);
  * the ``pre_llm_call`` hook drains that buffer into ``{"context": ...}``,
    which Hermes appends to the current user message (same contract as the
    live-time plugin), then persists the cursor and POSTs ``/ack``;
  * the ``transform_tool_result`` hook is the mid-turn channel: when an
    ``event`` (top level) is still uninjected, its note is appended to the
    tool output so the model sees it on the very next step.  At-most-once
    per event via a separate cursor + marked set that ``pre_llm_call`` also
    checks, so the same event is never injected twice.
  * only ``event`` and ``notify`` are ever injected; ``info`` is worker-only
    and ``log`` is archive-only.
  * the level decides the mode and nothing else does (0.8.0):
        event  -> interrupt : busy => the running turn is interrupted at once;
                              idle  => starts a turn (``internal=False``);
        notify -> queue     : busy => handed to Hermes' own busy queue
                              (``handle_message(internal=True)`` -> session
                              authority FIFO) and run after the turn really ends;
                              0.9.0 never parks it in the adapter's pending slot,
                              which used to open a turn inside a running one.
                              idle => queued then drained (never interrupts);
    the wake message is one line naming the sender
    (``⚡ 来自 <source> · <actor> 的打断`` / ``📬 收到来自 <source> · <actor> 的
    Notify 消息``, 0.8.2), never in ``MessageEvent.metadata``: the gateway
    session envelope refuses any non-native metadata
    (``invalid_params (metadata)``).  The cards themselves ride the normal
    ``pre_llm_call`` injection in the turn the wake starts.

0.6.0 — ``notify`` no longer waits for the trainer to speak again
(superseded by the 0.8.0 queue; kept as history):

  * ``pre_llm_call`` marks a turn active, ``post_llm_call`` clears it, so the
    poller knows whether anything is running.  A subagent/cron turn never
    touches that flag (it is not the trainer's turn).
  * idle (nothing running): a pulled ``notify`` wakes the session at once
    (``internal=True``, within one poll tick) instead of sitting in the queue
    until the next human message.
  * busy: the notify stays queued — cursor untouched — and is logged as
    ``notify deferred (busy)``; the ``post_llm_call`` path delivers it one
    second after the turn really ends.
  * both paths share one claim: same batch woken once, a global
    ``NOTIFY_WAKE_MIN_INTERVAL`` (10 s) between notify wakes, and a turn that
    was itself started by a notify wake never wakes another.
  * a successful wake consumes the batch (cursor advanced + ACKed) because
    the synthesized inbound message *is* the delivery; ``pre_llm_call`` in
    the new turn therefore never injects the same events twice.  A wake the
    gateway could not accept leaves the batch queued for the next attempt.

0.7.0 — every injection seam renders the same compact two-line card:

    #52 event mc_chat/Steve 20:01 thr=mc_chat:global
    > 溪流 快来

  * one card per event: header ``#id level source/actor HH:MM`` plus
    ``thr=<thread_key>`` when the event carries a thread key, then the text
    on its own ``> `` line;
  * a batch adds ``[N 条]`` on top, expands at most five cards and ends with
    ``+K more``; a single event has no summary line;
  * text is clipped to 100 characters plus ``…`` on a character boundary
    (newlines collapse to spaces), never a markdown table;
  * the mid-turn insertion (``transform_tool_result``) puts ``⚡ 立刻处理：`` on
    its own line in front of the card;
  * the synthesized wake message uses exactly the same card text.

0.8.0 — two delivery semantics instead of one wake path:

  * ``event`` -> **interrupt**: unchanged ``internal=False`` path.  Busy: the
    running turn is interrupted/steered at once; idle: a new turn starts.
  * ``notify`` -> **queue**: the card is built as a real ``MessageEvent(
    internal=True)`` and handed to the gateway's own per-session queue
    (``_queue_or_replace_pending_event(session_key, event)``).  The gateway
    drains that queue itself at the end of the running turn; this plugin no
    longer runs a timer or its own wake to nudge a notify.
  * idle enqueue: the gateway has no public "drain the head now" entry, so the
    plugin looks one up by name (``getattr`` over a candidate list) and only
    calls it when it exists; otherwise it pops the head with the adapter's
    ``get_pending_message`` and hands that same event to ``handle_message``.
    The strategy used is written to the log once.
  * **delivery proof**: neither path advances the cursor or ACKs when it hands
    the event over.  A batch only counts as delivered when ``pre_llm_call``
    injects those cards into the turn's context (or
    ``transform_tool_result`` inserts one mid-turn).  Queued, in-flight,
    rejected and timed-out batches never move the cursor.
  * at-least-once: every batch that is not proven delivered stays in an
    in-memory ledger; after ``TRIGGER_BUS_BATCH_TTL`` seconds without proof its
    marks are cleared and the next poll retries it.  A gateway refusal (lease
    timeout, no adapter, queue cap) is detected the same way and never loses
    the event.
  * de-duplication: an ``event_id`` already queued, woken or in flight is
    never enqueued a second time.

0.8.1 — the wake is ONE origin line, and no ``metadata`` at all:

  * 0.8.0 put ``metadata["interrupt_origin"]`` on the synthesized wake event so
    the gateway's busy ack could name the interrupter.  The gateway session
    envelope is a closed contract (``gateway/session_envelope.py``): any
    non-WhatsApp metadata is refused with ``invalid_params (metadata)``, so the
    interrupt was shown and then the message was dropped.
  * ``_build_wake_event`` now sets no metadata at all (the empty default dict
    passes ``_native_metadata``).  The whole wake message is the origin line:
        event  -> ``⚡ 来自 selftest · Tester 的打断``
        notify -> ``📬 来自 email · HR 的通知``
  * the cards are **not** carried by the wake.  They stay in the local queue and
    the turn the wake starts injects them through ``pre_llm_call`` in the
    unchanged ``#id level source/actor HH:MM`` + ``> text`` format — the same
    path that has always proved delivery, so there is no duplicate to guard
    against and the existing cursor still sends each batch exactly once.
  * the notify additionally no longer writes ``metadata["trigger_bus_ids"]``
    (nothing read it; identity checks use the event object).
  * the mid-turn injection (``transform_tool_result``) is unchanged: no origin
    line, just the card.

0.8.2 — one event takes exactly ONE route (no double delivery):

  * the duplicate seen in the field: an ``event`` was handed to ``_wake``
    (interrupt initiated), its interrupt raced the current tool call, and the
    **same** event was then also inserted by ``transform_tool_result``; the
    new turn's ``pre_llm_call`` injected the card a second time.
  * ``_BusState.woken_ids`` is the wake claim: the batch is marked **before**
    the hand-over (so the tool hook can never win a race against a wake that
    is already on the loop), and ``transform_tool_result`` only takes events
    that are **not** marked.  The claim is dropped by ``_forget_delivery`` —
    i.e. when the gateway refuses/raises (``_interrupt_done``), when the
    hand-over itself fails, and when the TTL retry clears the ledger — so the
    mid-turn insert still works as the fallback the moment a wake fails.
  * the reverse direction is unchanged (at-most-once): an event that
    ``transform_tool_result`` already inserted sits in ``interrupt_done`` and
    is skipped by the wake selector as well, so a later dispatch can never
    wake it again.
  * therefore every event travels exactly one route — wake **or** mid-turn
    insert.  The wake lines are:
        event  -> ``⚡ 来自 <source> · <actor> 的打断``   (byte-for-byte 0.8.1;
                  the gateway matches this prefix to add its Chinese hint line)
        notify -> ``📬 收到来自 <source> · <actor> 的 Notify 消息``  (0.8.2)
    both one line only; the cards still ride ``pre_llm_call``.

0.8.3 — an event wake also says so in Chinese, from the plugin itself:

  * the gateway prints its own English ``⚡ Interrupting current task …`` hint,
    and two separate send paths there never picked up the extra Chinese line.
    The plugin therefore sends it itself, through the adapter it already uses
    for ``handle_message``: ``⚡ 收到来自 <source> · <actor> 的 Event 消息``.
  * only ``internal=False`` (an ``event`` wake) sends it; a ``notify`` wake
    already *is* ``📬 收到来自 … 的 Notify 消息`` and sends nothing extra.
  * the send entry is probed by name (``send_message`` / ``send`` /
    ``_send_text`` / ``_send_plain_message``) and the chat id comes from the
    captured source; an adapter without any of them is logged and skipped.
  * one message per wake, naming the batch's first event.
  * best-effort: the whole send is wrapped in try/except, every failure is
    only logged, and the wake hand-over is never affected.

0.8.4 — the Chinese notice is sent at once, but can never appear twice:

  * the send stays inline, right after ``adapter.handle_message`` returned —
    no sleep, no timer, ordering against the English ack is not engineered.
  * thread safety: a coroutine send runs on the gateway loop captured in
    ``pre_gateway_dispatch`` (``run_coroutine_threadsafe``); with no usable
    loop the notice is sent synchronously on the calling thread and the
    fallback is logged.
  * duplicate guard on top of "one per wake": an identical ``(chat, text)``
    sent within ``ANNOUNCE_DEDUPE_WINDOW`` (2 s) is skipped, so a re-entrant
    wake or a second scheduler cannot produce the line twice.  The set holds
    at most ``ANNOUNCE_DEDUPE_MAX`` (32) entries, oldest dropped; a failed
    send releases its entry so a retry is not blocked.

0.8.5 — a notify wake also shows a Chinese notice, same mechanism:

  * the visible notice is level-aware now: ``event`` keeps
    ``⚡ 收到来自 <source> · <actor> 的 Event 消息`` byte-for-byte, ``notify``
    adds ``📬 收到来自 <source> · <actor> 的 Notify 消息``.  Both go through
    the same adapter probe, the same immediate inline send and the same 2 s
    ``(chat, text)`` dedupe; ``info`` / ``log`` send nothing.
  * the texts differ, so the ``(chat, text)`` dedupe key differs too — an
    event and a notify from the same origin never suppress each other.
  * still one visible notice per wake.  ``_dispatch`` owns it: when one tick
    holds both levels, the merged batch's FIRST event names the line and its
    level picks the text (``notify`` first -> 📬, ``event`` first -> ⚡).
  * nothing else moves: wake text, ``internal`` flags, queueing, cursor/ACK,
    cards and the delivery ledger are untouched.

0.9.0 — ``notify`` is queued until the TURN ends, not until the next gap in it:

  * the defect: a ``notify`` arriving while a turn was running was parked in the
    gateway's per-session pending slot (``_queue_or_replace_pending_event``).
    That slot is the server's "start this as the next turn" slot, so as soon as
    ANY turn ended — a normal one, or one an interrupt cut short — the adapter
    started a new turn from it, and starting a turn interrupts the running one
    and kills its in-flight tool process (``[Command interrupted]``, exit 130).
    Field log: ``notify QUEUED 22:35:32`` (busy=1) → ``DELIVERED 22:35:57``
    while ``sleep 50`` (started 22:35:16) was still running; it died at 22:35:56.
  * the fix reuses Hermes' OWN busy queue — the same one the WebUI uses — instead
    of a plugin-made turn-end guess.  When a notify arrives while the session is
    running, the plugin calls ``adapter.handle_message(event)`` exactly as the
    idle path does: on a busy session that lands in the session authority's
    durable FIFO and its drain runs the row only after the current turn has
    finished.  Entry points (nothing in Hermes is modified):

        gateway/platforms/base.py:4017   _handle_message_while_active
        gateway/session_ingress_burst.py:102  dispatch_busy_burst
        gateway/session_ingress.py:497   dispatch_shared_busy -> :39 admit_message
        gateway/session_authority.py:522 SessionAuthority.admit_native
        gateway/session_authority.py:1309 SessionAuthority._drain (claims the head
                                          only while no turn is executing)

    It is used only when ``runner.session_authority`` exists — the exact
    predicate ``BasePlatformAdapter._shared_runtime_owns_input`` uses — so a
    deployment without the authority never falls into the busy-input mode
    (``busy_input_mode: interrupt`` stays untouched).
  * without an authority the plugin holds the batch in its OWN queue instead:
    no gateway slot write, no wake, no handle_message.  ``notify HELD`` is
    logged with ``busy=1 phase=held`` and the cursor does not move; only
    ``_drain_idle`` wakes it once ``_busy()`` is False.
  * in both routes the wake is ``internal=True`` and the cards still ride the
    new turn's ``pre_llm_call``, which is the delivery proof and the only place
    the cursor/ACK move.
  * idle-time notify is unchanged: it is queued and drained in the same tick,
    exactly as in 0.8.0.  ``event`` is unchanged in both states (hard interrupt
    when busy, ``internal=False`` wake when idle).
  * a batch the FIFO never proves (turn never ends / gateway gone) keeps the
    normal TTL ledger; a native-queued batch is never re-woken (that would queue
    it twice) — the durable row owns its own execution.

0.9.1 — the ``📬`` notice is sent when the notify is DELIVERED, never when queued:

  * the defect: 0.9.0 sent the visible ``📬 收到来自 <src> · <actor> 的 Notify
    消息`` at ENQUEUE time.  Field log: ``notify NATIVE-QUEUED 23:06:11`` with
    the notice delivered 23:06:12, while the real injection — and the cursor —
    only moved at ``DELIVERED 23:06:57``.  The trainer saw "收到" 45 s before he
    had actually received anything.
  * the fix: enqueue/queueing is SILENT (``NATIVE-QUEUED`` and ``HELD`` send
    nothing).  The line is sent by :func:`_announce_on_delivery`, called from
    :func:`_confirm_injected` — the same decision point that logs ``DELIVERED``
    and moves the cursor/ACK.  One line per batch (the batch's FIRST event names
    the origin), guarded by ``_Delivery.announced`` so a batch confirmed twice
    still speaks once.
  * ``event`` is untouched: the hard interrupt and its immediate
    ``⚡ 收到来自 … 的 Event 消息`` stay exactly as they were.
  * a tick that merges both levels now speaks twice at two honest moments: the
    ``⚡`` at the event's wake (unchanged) and the ``📬`` when the notify batch
    is really injected.  A merged notify-only batch is still ONE ``📬``.

Design rules:
  * never import Hermes internals — only the hook argument dict is used;
  * never block the model call: the hook does no network I/O (ACK is sent by
    a short-lived daemon thread);
  * every failure is swallowed and logged to ``trigger-bus.log``; the agent
    behaves exactly as if the plugin were absent when the service is down;
  * ``TRIGGER_BUS_DISABLED=1`` makes ``register()`` a no-op.

Environment overrides (all optional):
    TRIGGER_BUS_URL          default http://127.0.0.1:8791
    TRIGGER_BUS_SUBSCRIBER   default hermes-main
    TRIGGER_BUS_CURSOR       default ~/.hermes/trigger-cursor.json
    TRIGGER_BUS_POLL_INTERVAL default 2 (seconds)
    TRIGGER_BUS_BATCH_TTL    default 120 (seconds without delivery proof
                             before the batch is retried)
    TRIGGER_BUS_POST_TURN_DELAY default 1 (seconds; post-turn dispatch delay)
    TRIGGER_BUS_TURN_ACTIVE_TTL default 1800 (seconds; stale busy-flag guard)
    TRIGGER_BUS_DISABLED=1   disable entirely
    TRIGGER_TOKEN_PATH       default ~/.hermes/secrets/trigger.token
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

PLUGIN_VERSION = "0.9.1"
MAX_QUEUE = 50
HTTP_TIMEOUT = 5.0
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
# 0.7.0 card format: text is clipped to TEXT_CLIP_CHARS + "…" and a batch
# expands at most MAX_EXPANDED_EVENTS cards before "+K more".
TEXT_CLIP_CHARS = 100
MAX_EXPANDED_EVENTS = 5

# Injection policy (four-level bus):
#   event   top level: exactly ONE route per event (0.8.2) — either the
#           interrupt wake (internal=false, steering a running or starting an
#           idle turn, cards injected by that turn's pre_llm_call) or the
#           mid-turn transform_tool_result insert; never both;
#   notify  next level: turn-boundary injection plus a queued, non-interrupting
#           wake (internal=true) when idle and right after a turn ends;
#   info    worker-only;  log  archive-only -> neither is ever injected.
INJECT_LEVELS = ("event", "notify")
INTERRUPT_LEVELS = ("event",)
LEGACY_LEVELS = {"super": "event", "super_event": "event"}
INTERRUPT_DONE_MAX = 500                  # bounded at-most-once memory

DEFAULT_BASE_URL = "http://127.0.0.1:8791"
DEFAULT_SUBSCRIBER = "hermes-main"
DEFAULT_CURSOR = Path.home() / ".hermes" / "trigger-cursor.json"
DEFAULT_LOCK = Path.home() / ".hermes" / "trigger-bus.lock"
DEFAULT_TOKEN_PATH = Path.home() / ".hermes" / "secrets" / "trigger.token"
LOG_PATH = Path(__file__).resolve().parent / "trigger-bus.log"

_STATE: Optional["_BusState"] = None
_TOKEN: Optional[str] = None
_LOGGER: Optional[logging.Logger] = None
_LOGGER_LOCK = threading.Lock()
_CURSOR_IO_LOCK = threading.Lock()
# Loopback traffic must never go through HTTP(S)_PROXY from the environment:
# a proxy in front of 127.0.0.1 can 502 or leak local requests.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #

def _get_logger() -> logging.Logger:
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER
    with _LOGGER_LOCK:
        if _LOGGER is not None:
            return _LOGGER
        logger = logging.getLogger("trigger-bus")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        if not logger.handlers:
            try:
                handler: logging.Handler = RotatingFileHandler(
                    str(LOG_PATH), maxBytes=LOG_MAX_BYTES,
                    backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
                handler.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)-7s %(message)s"))
                handler.setLevel(logging.DEBUG)
            except OSError:
                handler = logging.NullHandler()
            logger.addHandler(handler)
        _LOGGER = logger
    return _LOGGER


def _disabled() -> bool:
    return os.environ.get("TRIGGER_BUS_DISABLED", "").strip().lower() in {
        "1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
        return value if value > 0 else default
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# cursor persistence
# --------------------------------------------------------------------------- #

def _load_cursor(path: Path) -> int:
    for candidate in (path, Path(str(path) + ".json")):
        try:
            raw = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            for key in ("last_id", "cursor", "id"):
                if key in data:
                    try:
                        return max(0, int(data[key]))
                    except (TypeError, ValueError):
                        pass
            # also tolerate {"cursors": {"<subscriber>": 12}}
            nested = data.get("cursors")
            if isinstance(nested, dict):
                for value in nested.values():
                    try:
                        return max(0, int(value))
                    except (TypeError, ValueError):
                        pass
        elif isinstance(data, (int, float)):
            return max(0, int(data))
    # default ~/.hermes/trigger-cursor.json may have been written by a first
    # run with a different name; nothing else to try.
    return 0


def _save_cursor(path: Path, cursor: int, subscriber: str) -> None:
    with _CURSOR_IO_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            payload = {"last_id": int(cursor), "subscriber": subscriber}
            tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            _get_logger().warning("cannot persist cursor to %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

class _Delivery:
    """One batch handed to the gateway, waiting for proof it reached the context.

    ``mode`` is ``"queue"`` (notify: gateway pending queue) or ``"interrupt"``
    (event: ``internal=False`` wake).  ``marker`` is a normalised text marker
    for a legacy card-bearing wake; since 0.8.1 wakes carry only the origin
    line, so the marker is empty and proof comes from
    :func:`_confirm_injected` (the cards entering the model's context).
    Nothing here touches the cursor.
    """

    __slots__ = ("token", "events", "text", "marker", "mode", "session_key",
                 "created_at", "attempts", "phase", "event", "native",
                 "announced")

    def __init__(self, token: str, events: List[Dict[str, Any]], text: str,
                 mode: str, session_key: str) -> None:
        self.token = token
        self.events = events
        self.text = text
        self.marker = " ".join(text.split())[:160]
        self.mode = mode
        self.session_key = session_key
        self.created_at = time.monotonic()
        self.attempts = 1
        self.phase = "queued"          # queued/held -> in_flight -> (removed)
        self.event: Any = None         # the MessageEvent handed to the gateway
        # 0.9.0: handed to Hermes' own busy queue (session authority FIFO) instead
        # of the adapter's pending slot; the FIFO owns the turn-end timing.
        self.native = False
        # 0.9.1: the visible 📬 line went out (only ever set at delivery proof).
        self.announced = False

    def describe(self) -> str:
        ids = ",".join(str(e.get("id")) for e in self.events)
        return "mode=%s ids=%s phase=%s attempts=%d" % (
            self.mode, ids or "-", self.phase, self.attempts)


class _BusState:
    def __init__(self) -> None:
        self.lock = threading.Lock()          # guards events/high_water/cursor
        self.ack_lock = threading.Lock()      # serialises ACK requests
        self.events: Deque[Dict[str, Any]] = deque(maxlen=MAX_QUEUE)
        self.base_url = os.environ.get("TRIGGER_BUS_URL", DEFAULT_BASE_URL).rstrip("/")
        self.subscriber = (os.environ.get("TRIGGER_BUS_SUBSCRIBER",
                                          DEFAULT_SUBSCRIBER).strip()
                           or DEFAULT_SUBSCRIBER)
        self.cursor_path = Path(os.path.expanduser(
            os.environ.get("TRIGGER_BUS_CURSOR") or str(DEFAULT_CURSOR)))
        self.cursor = _load_cursor(self.cursor_path)
        self.high_water = self.cursor
        self.acked = self.cursor
        self.pending_ack = 0
        self.dropped = 0
        # Mid-turn event injection: its own cursor + at-most-once marked set,
        # so pre_llm_call can never hand the same event out twice.
        self.interrupt_cursor = self.cursor
        self.interrupt_done: "OrderedDict[str, None]" = OrderedDict()
        # 0.8.2 wake claim: an event handed to _wake (interrupt initiated) is
        # marked here BEFORE the hand-over, so transform_tool_result can never
        # insert the same event mid-turn while that wake is already running.
        # _forget_delivery drops the mark (refused / raised / TTL retry), which
        # is what keeps the mid-turn insert alive as the fallback.
        self.woken_ids: "OrderedDict[str, None]" = OrderedDict()
        # Subscription cycle (0.4.0): the record has its own clock, so these
        # fields exist purely for logging + "did something change" decisions.
        self.renew_count: Optional[int] = None
        self.renew_failures = 0
        self.expired = False
        self.sub_status = ""
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.fail_count = 0
        # 0.8.0 delivery ledger.  A batch handed to the gateway is NOT
        # delivered yet: it stays here until pre_llm_call receives the card as
        # this turn's user_message.  ``queued_keys`` (event_id / event_key) is
        # the de-duplication set for "already queued or in flight".
        self.inflight: "OrderedDict[str, _Delivery]" = OrderedDict()
        self.queued_keys: "OrderedDict[str, None]" = OrderedDict()
        # Routing key for the gateway's own pending queue.  Resolved once from
        # the source captured by pre_gateway_dispatch; falls back to the
        # session_id pre_llm_call reports (logged explicitly either way).
        self.session_id = ""
        self.session_key: Optional[str] = None
        self.session_key_how = ""
        self.drain_entry = ""            # which drain entry we called, if any
        # Wake path: captured from pre_gateway_dispatch (the only hook that
        # hands us the running gateway + the session source).
        self.gateway: Any = None
        self.source: Any = None
        self.loop: Any = None
        self.last_wake: float = float("-inf")
        self.last_wake_warn: float = float("-inf")
        # 0.6.0 proactive-delivery bookkeeping (still used for the log throttle
        # and the stale busy-flag guard).
        self.turn_active = False
        self.turn_active_since = 0.0
        self.post_turn_marks: "OrderedDict[str, None]" = OrderedDict()


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def _auth_headers() -> Dict[str, str]:
    """mowu-trigger 0.4.0 requires X-Trigger-Token on /events, /ack and /renew.

    The token lives in ~/.hermes/secrets/trigger.token (auto-generated by the
    server, mode 600). Missing file => no header; the server answers 401 and
    the plugin degrades exactly as it does when the service is down.
    """
    global _TOKEN
    if _TOKEN is None:
        path = Path(os.path.expanduser(
            os.environ.get("TRIGGER_TOKEN_PATH") or str(DEFAULT_TOKEN_PATH)))
        try:
            _TOKEN = path.read_text(encoding="utf-8").strip()
        except OSError:
            _TOKEN = ""
    return {"X-Trigger-Token": _TOKEN} if _TOKEN else {}


def _fetch_events(state: _BusState, since: int
                  ) -> tuple:
    """GET /events → ``(events, subscription)``.

    ``subscription`` is ``None`` for a pre-0.4.0 server that still answers with
    a bare array, so an older server keeps working (it just never asks to
    renew, because it has no clock).
    """
    query = urllib.parse.urlencode({
        "subscriber": state.subscriber,
        "since": since,
        "limit": MAX_QUEUE,
    })
    url = "%s/events?%s" % (state.base_url, query)
    req = urllib.request.Request(url, headers=dict({
        "Accept": "application/json",
        "User-Agent": "hermes-trigger-bus/%s" % PLUGIN_VERSION,
    }, **_auth_headers()))
    with _OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read(1_000_000)
    data = json.loads(raw.decode("utf-8"))
    subscription: Optional[Dict[str, Any]] = None
    if isinstance(data, dict):
        sub = data.get("subscription")
        subscription = sub if isinstance(sub, dict) else None
        data = data.get("events") or []
    if not isinstance(data, list):
        raise ValueError("unexpected /events payload: %r" % type(data))
    return [e for e in data if isinstance(e, dict)], subscription


def _post_json(state: _BusState, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        state.base_url + path, data=data, method="POST",
        headers=dict({"Content-Type": "application/json; charset=utf-8",
                      "Accept": "application/json",
                      "User-Agent": "hermes-trigger-bus/%s" % PLUGIN_VERSION},
                     **_auth_headers()))
    with _OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read(65536)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _renew(state: _BusState, logger: logging.Logger) -> bool:
    """POST /renew for our own subscriber. Logs one INFO per renew_count change.

    Failure is never fatal: the poll loop keeps running and retries on the next
    tick (the server keeps the record alive for 24 h after expiry).
    """
    try:
        reply = _post_json(state, "/renew", {"name": state.subscriber})
    except urllib.error.HTTPError as exc:
        state.renew_failures += 1
        # 410 = the record is closed/expired past the grace window: renewing
        # will never work again, so say it once and stop shouting.
        if exc.code in (404, 410):
            if state.sub_status != "unrenewable":
                state.sub_status = "unrenewable"
                logger.warning("RENEW refused (%d %s) for %s: subscription is "
                               "not renewable; polling continues with an "
                               "empty queue until it is re-created",
                               exc.code, exc.reason, state.subscriber)
        elif state.renew_failures == 1:
            logger.warning("RENEW failed: %s", exc)
        else:
            logger.debug("RENEW failed (#%d): %s", state.renew_failures, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - never kill the poller
        state.renew_failures += 1
        if state.renew_failures == 1:
            logger.warning("RENEW failed: %s", exc)
        else:
            logger.debug("RENEW failed (#%d): %s", state.renew_failures, exc)
        return False

    state.renew_failures = 0
    state.expired = False
    count = reply.get("renew_count")
    expires_in = reply.get("expires_in")
    if count != state.renew_count:
        state.renew_count = count
        logger.info("RENEW name=%s renew_count=%s expires_in=%ss (ttl reset)",
                    state.subscriber, count, expires_in)
    else:
        logger.debug("RENEW name=%s renew_count=%s expires_in=%ss",
                     state.subscriber, count, expires_in)
    return True


def _note_subscription(state: _BusState, subscription: Optional[Dict[str, Any]],
                       logger: logging.Logger) -> bool:
    """React to the ``{subscription}`` block. True if an expiry was revived.

    Called before merging a batch, so an expired record can never leave old
    events queued for injection.
    """
    if not subscription:
        return False
    status = str(subscription.get("status") or "")
    state.sub_status = status

    if subscription.get("expired"):
        with state.lock:
            cleared = len(state.events)
            state.events.clear()
        if not state.expired:
            state.expired = True
            logger.warning("subscription %s EXPIRED (status=%s): delivery "
                           "stopped, cleared %d queued event(s); renewing "
                           "before the next poll", state.subscriber, status,
                           cleared)
        else:
            logger.debug("subscription %s still expired (status=%s)",
                         state.subscriber, status)
        if status == "active":
            # Clock ran out but still inside the 24 h grace: revive at once
            # and let the caller pull again in this same tick.
            return _renew(state, logger)
        return False

    if state.expired:
        state.expired = False
        logger.info("subscription %s is delivering again (expires_in=%ss)",
                    state.subscriber, subscription.get("expires_in"))

    if subscription.get("renew_required"):
        _renew(state, logger)
    return False


def _ack_now(state: _BusState, up_to: int) -> bool:
    """Send one ACK. Caller must hold state.ack_lock."""
    target = max(int(up_to), state.pending_ack)
    if target <= state.acked:
        return True
    try:
        _post_json(state, "/ack",
                   {"subscriber": state.subscriber, "up_to_id": target})
    except Exception as exc:
        state.pending_ack = max(state.pending_ack, target)
        _get_logger().debug("ACK up_to=%d failed: %s", target, exc)
        return False
    state.acked = target
    if state.pending_ack <= target:
        state.pending_ack = 0
    _get_logger().debug("ACKed up_to=%d", target)
    return True


def _ack_async(state: _BusState, up_to: int) -> None:
    """Fire-and-forget ACK so the hook never waits on the network."""
    def run() -> None:
        with state.ack_lock:
            _ack_now(state, up_to)

    threading.Thread(target=run, name="trigger-bus-ack", daemon=True).start()


# --------------------------------------------------------------------------- #
# polling
# --------------------------------------------------------------------------- #

def _merge_events(state: _BusState, events: List[Dict[str, Any]]) -> int:
    added = 0
    with state.lock:
        for event in events:
            try:
                event_id = int(event.get("id"))
            except (TypeError, ValueError):
                continue
            if event_id <= state.high_water:
                continue
            if len(state.events) == state.events.maxlen:
                state.dropped += 1
            state.events.append(event)
            state.high_water = event_id
            added += 1
    return added


def _poll_loop(state: _BusState) -> None:
    logger = _get_logger()
    interval = _env_float("TRIGGER_BUS_POLL_INTERVAL", 2.0)
    logger.info("poll thread started: subscriber=%s base=%s cursor=%d "
                "interval=%.1fs", state.subscriber, state.base_url,
                state.cursor, interval)
    while not state.stop.is_set():
        if _disabled():
            logger.info("TRIGGER_BUS_DISABLED set at runtime; poll loop stops")
            return
        if state.pending_ack > state.acked:
            with state.ack_lock:
                _ack_now(state, state.pending_ack)
        try:
            events, subscription = _fetch_events(state, state.high_water)
            revived = _note_subscription(state, subscription, logger)
            if revived:
                # The clock had run out but /renew revived the record; pull
                # once more right now so a 2 s poll tick is not a delivery gap.
                events, subscription = _fetch_events(state, state.high_water)
                _note_subscription(state, subscription, logger)
            if subscription is not None and subscription.get("expired"):
                events = []          # never deliver what we no longer own
            added = _merge_events(state, events)
            if added:
                logger.info("pulled %d event(s), queue=%d", added,
                            len(state.events))
            if state.fail_count:
                logger.info("trigger-server reachable again")
                state.fail_count = 0
        except Exception as exc:
            state.fail_count += 1
            if state.fail_count == 1:
                logger.warning("trigger-server unreachable (%s); degrading "
                               "silently", exc)
            else:
                logger.debug("poll #%d failed: %s", state.fail_count, exc)
        # Level decides the mode (event = steer, notify = queue) and this runs
        # on every tick — not only when new events land — so a notify
        # deferred by the minimum interval is retried the moment the window
        # opens, never waiting for the trainer to speak first.
        try:
            _proactive_wake(state, logger, where="poll")
        except Exception:  # noqa: BLE001 - a wake bug must not stop polling
            logger.exception("proactive wake failed; events stay queued")
        if state.stop.wait(interval):
            return


# --------------------------------------------------------------------------- #
# hook
# --------------------------------------------------------------------------- #

def _level(event: Dict[str, Any]) -> str:
    """Canonical level; legacy ``super`` / ``super_event`` fold to ``event``."""
    level = str(event.get("level") or "info").strip().lower()
    return LEGACY_LEVELS.get(level, level)


def _event_key(event: Dict[str, Any]) -> str:
    event_id = str(event.get("event_id") or "").strip()
    return event_id or "#%s" % event.get("id")


_HHMM_RE = re.compile(r"(?:[T ]|^)(\d{1,2}):(\d{2})")


def _clip_text(value: Any, limit: int = TEXT_CLIP_CHARS) -> str:
    """One line of event text, clipped on a character boundary.

    ``str`` slicing is code-point based, so a UTF-8 byte can never be split.
    A trailing ZWJ / variation selector / skin-tone modifier is dropped too:
    keeping it would render as a half-built emoji sequence.
    """
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    text = text[:limit]
    while text and (text[-1] == "\u200d"
                    or text[-1] in "\ufe0e\ufe0f"
                    or "\U0001F3FB" <= text[-1] <= "\U0001F3FF"):
        text = text[:-1]
    return text.rstrip() + "…"


def _short_time(value: Any) -> str:
    """``HH:MM`` from an ISO timestamp or epoch; ``--:--`` when unusable."""
    raw = str(value or "").strip()
    if not raw:
        return "--:--"
    match = _HHMM_RE.search(raw)
    if match:
        return "%02d:%s" % (int(match.group(1)), match.group(2))
    try:
        stamp = float(raw)
    except ValueError:
        return "--:--"
    if stamp > 1e12:                      # milliseconds
        stamp /= 1000.0
    try:
        return time.strftime("%H:%M", time.localtime(stamp))
    except (OverflowError, OSError, ValueError):
        return "--:--"


def _thread_key(event: Dict[str, Any]) -> str:
    """Thread/topic key, if the envelope carries one (else empty)."""
    value = event.get("thread_key") or event.get("topic")
    payload = event.get("payload")
    if not value and isinstance(payload, dict):
        value = payload.get("thread_key") or payload.get("topic")
    return " ".join(str(value or "").split())


def _event_origin(event: Dict[str, Any]) -> str:
    """``source`` or ``source/actor``; actor is omitted when empty."""
    source = " ".join(str(event.get("source") or "").split()) or "unknown"
    actor = " ".join(str(event.get("actor") or "").split())
    return "%s/%s" % (source, actor) if actor else source


def _event_head(event: Dict[str, Any]) -> str:
    """``#id level source/actor HH:MM`` + optional ``thr=<thread_key>``."""
    head = "#%s %s %s %s" % (
        event.get("id") if event.get("id") is not None else "?",
        _level(event), _event_origin(event), _short_time(event.get("ts")))
    thread = _thread_key(event)
    if thread:
        head += " thr=%s" % thread
    return head


def _format_event(event: Dict[str, Any]) -> str:
    """One event as the two-line card every delivery seam shares."""
    return "%s\n> %s" % (_event_head(event), _clip_text(event.get("text")))


def _format_events(events: List[Dict[str, Any]],
                   limit: int = MAX_EXPANDED_EVENTS) -> str:
    """A batch: ``[N 条]`` header, at most ``limit`` cards, then ``+K more``.

    A single event has no summary line.  Markdown tables are deliberately not
    used: the wake message is also shown in Feishu, which mangles tables.
    """
    events = [e for e in events if isinstance(e, dict)]
    if not events:
        return ""
    if len(events) == 1:
        return _format_event(events[0])
    blocks = [_format_event(e) for e in events[:limit]]
    hidden = len(events) - len(blocks)
    if hidden > 0:
        blocks.append("+%d more" % hidden)
    return "\n".join(["[%d 条]" % len(events)] + blocks)


def _format_interrupt(event: Dict[str, Any]) -> str:
    """Mid-turn insertion: the same card behind a one-line directive."""
    return "\n\n⚡ 立刻处理：\n" + _format_event(event)


def _wake_line(events: List[Dict[str, Any]], icon: str, verb: str,
               lead: str = "来自") -> str:
    """The whole wake message, one line: ``⚡ 来自 mc_chat · Steve 的打断``.

    The cards are deliberately **not** carried here (0.8.1): they stay in the
    local queue and the turn this wake starts injects them through
    ``pre_llm_call``.  A one-line body cannot be confused with a user message,
    stays far below any envelope limit, and the delivery proof remains what it
    always was — the cards reaching the model's context.

    The gateway refuses every non-native ``MessageEvent.metadata``
    (``invalid_params (metadata)``), so the origin must travel in the text.
    0.8.2 keeps the event line byte-for-byte (the gateway matches its strict
    ``⚡ 来自 … 的打断`` prefix) and gives notify its own Chinese notice line.
    """
    events = [e for e in events if isinstance(e, dict)]
    if not events:
        return ""
    primary = next((e for e in events if _level(e) == "event"), events[0])
    return "%s %s %s %s" % (icon, lead, _interrupt_origin(primary), verb)


def _interrupt_text(events: List[Dict[str, Any]]) -> str:
    """``event`` wake: ``⚡ 来自 <source> · <actor> 的打断`` (strict, unchanged)."""
    return _wake_line(events, "⚡", "的打断")


def _notify_text(events: List[Dict[str, Any]]) -> str:
    """``notify`` wake: ``📬 收到来自 <source> · <actor> 的 Notify 消息``."""
    return _wake_line(events, "📬", "的 Notify 消息", lead="收到来自")


def _take_events(state: "_BusState",
                 levels: Optional[tuple] = None) -> Optional[tuple]:
    """Atomically drain the queue; shared by the inject path and the wake path
    so the same events are never delivered twice.

    ``levels`` restricts what may be injected (the agent only ever takes
    event + notify).  Anything else still sitting in the queue is discarded
    here: info/log must never reach a session.
    """
    with state.lock:
        if not state.events:
            return None
        if levels is None:
            events = list(state.events)
        else:
            events = [e for e in state.events if _level(e) in levels]
        if not events:
            return None
        skipped = len(state.events) - len(events)
        state.events.clear()
        ids = [int(e["id"]) for e in events if e.get("id") is not None]
        up_to = max(ids + [state.high_water])
        state.cursor = max(state.cursor, up_to)
        cursor = state.cursor
        dropped, state.dropped = state.dropped, 0
    if skipped:
        _get_logger().debug("discarded %d non-injectable event(s)", skipped)
    _save_cursor(state.cursor_path, cursor, state.subscriber)
    _ack_async(state, cursor)
    return events, cursor, dropped


def _take_interrupt(state: "_BusState") -> Optional[Dict[str, Any]]:
    """Remove and return one not-yet-injected top-level ``event``, or ``None``.

    ``notify`` never interrupts, so it is not eligible here.  At-most-once
    per event: a separate cursor plus a bounded marked set of event keys.
    The shared ACK cursor is deliberately *not* moved here — acking an event
    that arrived after an uninjected one would mark that earlier event read.

    0.8.2: an event already claimed by an interrupt wake (``woken_ids``) is
    skipped.  That wake opens the turn that injects it through
    ``pre_llm_call``; inserting it here as well is exactly the double delivery
    this release removes.  If that wake fails, ``_forget_delivery`` drops the
    claim and this loop starts seeing the event again (fallback).
    """
    with state.lock:
        for index, event in enumerate(state.events):
            if _level(event) not in INTERRUPT_LEVELS:
                continue
            key = _event_key(event)
            if key in state.interrupt_done:
                continue
            if key in state.woken_ids:
                continue
            del state.events[index]
            try:
                state.interrupt_cursor = max(state.interrupt_cursor,
                                             int(event.get("id") or 0))
            except (TypeError, ValueError):
                pass
            state.interrupt_done[key] = None
            while len(state.interrupt_done) > INTERRUPT_DONE_MAX:
                state.interrupt_done.popitem(last=False)
            return event
    return None


def _interrupt_injected(state: "_BusState", event: Dict[str, Any]) -> bool:
    with state.lock:
        return _event_key(event) in state.interrupt_done


# 0.8.0 delivery policy.
POST_TURN_DELAY = _env_float("TRIGGER_BUS_POST_TURN_DELAY",
                             _env_float("TRIGGER_BUS_NOTIFY_WAKE_DELAY", 1.0))
BATCH_TTL = _env_float("TRIGGER_BUS_BATCH_TTL", 120.0)
TURN_ACTIVE_TTL = _env_float("TRIGGER_BUS_TURN_ACTIVE_TTL", 1800.0)
QUEUED_KEYS_MAX = 4000
# Background turns are never the trainer's turn: they must not set/clear the
# busy flag.
NON_TURN_PLATFORMS = frozenset(
    part.strip().lower()
    for part in (os.environ.get("TRIGGER_BUS_NON_TURN_PLATFORMS")
                 or "subagent,cron,internal,worker,batch").split(",")
    if part.strip())
# Gateway entry points that would process the head of a session's pending
# queue.  None of them exists in today's gateway; they are probed with
# getattr (never assumed) and the probe result is logged once.
# ``_hm_handle_running_session_message`` is deliberately NOT in this list: it
# is the BUSY fast path, and for an internal event it would queue the card a
# second time (run_inbound.py: internal + no command -> _queue_or_replace).
DRAIN_ENTRY_NAMES = (
    "_drain_pending_queue", "_hm_drain_pending_queue",
    "_process_pending_queue", "_drain_queued_events",
    "_drain_pending_messages",
)
BUSY_ONLY_ENTRY = "_hm_handle_running_session_message"


def _on_pre_gateway_dispatch(**_kwargs: Any) -> None:
    """Capture the running gateway + the last session source.

    pre_llm_call alone can never wake an idle session: with no incoming
    message there is no LLM call to inject into. This hook is the only one
    that hands a plugin the live gateway object.
    """
    state = _STATE
    if state is None or _disabled():
        return None
    gateway = _kwargs.get("gateway")
    event = _kwargs.get("event")
    source = getattr(event, "source", None)
    if gateway is not None and source is not None:
        first = state.gateway is None
        chat_id = getattr(source, "chat_id", None)
        if state.source is not None and getattr(state.source, "chat_id", None) != chat_id:
            # A different chat: the routing key must be derived again.
            state.session_key, state.session_key_how = None, ""
        state.gateway, state.source = gateway, source
        if first:
            _get_logger().info("captured gateway + session source from "
                               "pre_gateway_dispatch (chat=%s)",
                               getattr(source, "chat_id", "?"))
        try:
            state.loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
    return None


DEFAULT_WAKE_CHAT_ID = "oc_9add9b05d0d7bddb6ecc3ebda8294859"
DEFAULT_WAKE_USER_ID = "ou_e5c3f85b727b639d84caa47f74d24311"


def _resolve_source(state: "_BusState") -> None:
    """Last resort: build the home Feishu source from env/defaults."""
    if state.source is not None:
        return
    try:
        from gateway.config import Platform
        from gateway.session import SessionSource
        state.source = SessionSource(
            platform=Platform.FEISHU,
            chat_id=os.environ.get("TRIGGER_WAKE_CHAT_ID", DEFAULT_WAKE_CHAT_ID),
            chat_type="dm",
            user_id=os.environ.get("TRIGGER_WAKE_USER_ID", DEFAULT_WAKE_USER_ID),
        )
        _get_logger().info("built fallback wake source for chat=%s",
                           state.source.chat_id)
    except Exception as exc:
        _get_logger().warning("fallback source build failed: %s", exc)


def _resolve_gateway(state: "_BusState") -> None:
    """Fall back to the gateway's process-global runner when the hook capture
    never fired (hooks are wired at startup; a hot reload cannot rewire them)."""
    if state.gateway is not None:
        return
    try:
        from gateway import run_api
        runner = getattr(run_api, "_ACTIVE_RUNNER", None)
        if runner is not None:
            state.gateway = runner
            _get_logger().info("resolved gateway via gateway.run_api._ACTIVE_RUNNER")
    except Exception as exc:
        _get_logger().debug("gateway resolve failed: %s", exc)


def _interrupt_origin(event: Dict[str, Any]) -> str:
    """Human-readable origin for the wake line (``mc_chat · Steve``)."""
    source = str(event.get("source") or "unknown").strip() or "unknown"
    actor = str(event.get("actor") or "").strip()
    return "%s · %s" % (source, actor) if actor else source


# 0.8.3: an event wake sends one extra short Chinese message of its own,
# straight through the adapter — the gateway's two send paths for its English
# "Interrupting current task" hint never carried this line.  The send entry
# differs per adapter, so it is probed by name; the first callable wins.
#
# 0.8.4: still exactly one line, sent immediately after handle_message, but
# with a second in-process guard so a re-entrant wake can never show it twice:
# an identical (chat, text) sent within ANNOUNCE_DEDUPE_WINDOW (2 s) is
# dropped.  Ordering against the gateway's English ack is deliberately not
# engineered — only "never twice" matters.
ANNOUNCE_SEND_METHODS = ("send_message", "send", "_send_text",
                         "_send_plain_message")
ANNOUNCE_DEDUPE_WINDOW = 2.0      # seconds; identical (chat, text) is a dupe
ANNOUNCE_DEDUPE_MAX = 32          # entries kept, oldest dropped first

# Guarded by ``_announce_lock``; the clock indirection lets the selftests pin
# a fake clock instead of sleeping.
_announce_recent: "OrderedDict[tuple, float]" = OrderedDict()
_announce_lock = threading.Lock()
_announce_clock = time.monotonic


def _event_announce_text(event: Dict[str, Any]) -> str:
    """``⚡ 收到来自 <source> · <actor> 的 Event 消息`` (0.8.3)."""
    return "⚡ 收到来自 %s 的 Event 消息" % _interrupt_origin(event)


def _notify_announce_text(event: Dict[str, Any]) -> str:
    """``📬 收到来自 <source> · <actor> 的 Notify 消息`` (0.8.5)."""
    return "📬 收到来自 %s 的 Notify 消息" % _interrupt_origin(event)


def _announce_text(event: Dict[str, Any]) -> str:
    """The one visible line for a wake, picked by that event's level (0.8.5).

    ``notify`` gets its own Chinese notice; every other injectable level keeps
    the 0.8.3 ``event`` wording byte-for-byte.  The two texts differ, so the
    ``(chat, text)`` dedupe key can never collide across levels.
    """
    if _level(event) == "notify":
        return _notify_announce_text(event)
    return _event_announce_text(event)


def _announce_claim(chat_id: str, text: str,
                    logger: logging.Logger) -> Optional[float]:
    """Reserve ``(chat, text)`` for sending; ``None`` when it is a duplicate.

    A stamp less than ``ANNOUNCE_DEDUPE_WINDOW`` old means the identical text
    already went to this chat — skip it.  The returned stamp lets a failed
    send release its own claim, so it never blocks a later retry.
    """
    key = (chat_id, text)
    now = _announce_clock()
    with _announce_lock:
        last = _announce_recent.get(key)
        if last is not None and 0.0 <= now - last < ANNOUNCE_DEDUPE_WINDOW:
            _announce_recent.move_to_end(key)
            logger.info("wake notice skipped: identical text sent to "
                        "%s %.2fs ago (dedupe window %.1fs)",
                        chat_id, now - last, ANNOUNCE_DEDUPE_WINDOW)
            return None
        _announce_recent[key] = now
        _announce_recent.move_to_end(key)
        while len(_announce_recent) > ANNOUNCE_DEDUPE_MAX:
            _announce_recent.popitem(last=False)
        return now


def _announce_release(chat_id: str, text: str, stamp: float) -> None:
    """Drop a claim whose send failed (only if it is still ours)."""
    with _announce_lock:
        key = (chat_id, text)
        if _announce_recent.get(key) == stamp:
            _announce_recent.pop(key, None)


def _announce_reset() -> None:
    """Forget every recent stamp — selftests only, never used by a wake."""
    with _announce_lock:
        _announce_recent.clear()


def _announce_send_callable(adapter: Any) -> tuple:
    """``(name, callable)`` for the adapter's text-send entry, or two Nones."""
    if adapter is None:
        return None, None
    for name in ANNOUNCE_SEND_METHODS:
        try:
            fn = getattr(adapter, name, None)
        except Exception:
            continue
        if callable(fn):
            return name, fn
    return None, None


def _send_result_error(result: Any) -> str:
    """``""`` for a successful SendResult (or None), else the reason.

    A send can answer ``SendResult(success=False, error="Not connected")``
    without raising; that is a failure too and must only be logged.
    """
    if result is None or isinstance(result, str):
        return ""
    if getattr(result, "success", True):
        return ""
    return str(getattr(result, "error", "") or "send returned success=False")


def _announce_done(name: str, logger: logging.Logger, chat_id: str,
                   text: str, stamp: float) -> Any:
    """Consume the scheduled coroutine's future so a failure is not silent.

    0.8.4: an async send that raises or answers ``SendResult(success=False)``
    is a failed send too, so its dedupe claim is released — exactly like the
    synchronous path — and a later retry inside the window is not blocked.
    """
    def _callback(future: Any) -> None:
        try:
            result = future.result()
        except Exception as exc:
            _announce_release(chat_id, text, stamp)
            logger.warning("wake notice (%s) failed: %s", name, exc)
            return
        error = _send_result_error(result)
        if error:
            _announce_release(chat_id, text, stamp)
            logger.warning("wake notice (%s) not delivered: %s",
                           name, error)
        else:
            logger.debug("wake notice (%s) delivered", name)
    return _callback


def _deliver_announcement(state: "_BusState", name: str, fn: Any,
                          chat_id: str, text: str,
                          logger: logging.Logger) -> str:
    """The actual send, deduped and thread-safe (0.8.4).

    Runs on the caller's thread, immediately (no delay, no timer).  A
    coroutine send goes to the gateway loop captured by
    ``pre_gateway_dispatch``; without a usable loop it is executed here to
    completion and the fallback is logged.  Returns ``sent:<method>``,
    ``skipped:duplicate`` or ``failed:<error>``.
    """
    stamp = _announce_claim(chat_id, text, logger)
    if stamp is None:
        return "skipped:duplicate"
    try:
        outcome = fn(chat_id, text)
        if asyncio.iscoroutine(outcome):
            loop = getattr(state, "loop", None)
            running = bool(getattr(loop, "is_running", lambda: False)()) \
                if loop is not None else False
            if running:
                future = asyncio.run_coroutine_threadsafe(outcome, loop)
                future.add_done_callback(
                    _announce_done(name, logger, chat_id, text, stamp))
            else:
                logger.info("wake notice (%s): no captured gateway "
                            "loop; sending synchronously", name)
                result = _run_coroutine_here(outcome, name, logger)
                error = _send_result_error(result)
                if error:
                    _announce_release(chat_id, text, stamp)
                    logger.warning("wake notice (%s) not delivered: %s",
                                   name, error)
                    return "failed:%s" % error
        else:
            error = _send_result_error(outcome)
            if error:
                _announce_release(chat_id, text, stamp)
                logger.warning("wake notice (%s) not delivered: %s",
                               name, error)
                return "failed:%s" % error
        logger.info("wake notice handed to %s for %s: %s",
                    name, chat_id, text)
        return "sent:%s" % name
    except Exception as exc:  # never let the extra line affect the wake
        _announce_release(chat_id, text, stamp)
        logger.warning("wake notice failed: %s", exc)
        return "failed:%s" % exc


def _run_coroutine_here(outcome: Any, name: str,
                        logger: logging.Logger) -> Any:
    """Finish a coroutine send on this thread (no gateway loop available).

    Uses ``asyncio.run``; if the caller already has a running loop, the
    coroutine is scheduled there instead (fire-and-forget).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(outcome)
    asyncio.ensure_future(outcome)
    logger.info("wake notice (%s) scheduled on the caller's loop "
                "(no gateway loop captured)", name)
    return None


def _send_wake_notice(state: "_BusState", adapter: Any,
                      primary: Dict[str, Any],
                      logger: logging.Logger) -> str:
    """One extra Chinese notice, sent at once when the moment is right (0.8.5).

    The event's level picks the line (``⚡ … Event 消息`` / ``📬 … Notify
    消息``).  An ``event`` calls it right after the hand-over; a ``notify``
    calls it from :func:`_announce_on_delivery`, i.e. only once the batch is
    really injected (0.9.1).  Everything else is the 0.8.3/0.8.4 mechanism:
    immediate (no sleep, no ``call_later``), exactly one message per wake, plus
    an in-process 2-second dedupe on ``(chat, text)`` so a re-entrant wake or a
    second scheduler cannot produce the same line twice.

    Best-effort by contract: every failure is logged and swallowed — it can
    never undo a wake.  Returns ``sent:<method>``, ``skipped:<why>`` or
    ``failed:<error>`` for the log/tests.
    """
    try:
        text = _announce_text(primary)
        chat_id = str(getattr(getattr(state, "source", None), "chat_id", "") or "")
        if not chat_id:
            logger.info("wake notice skipped: captured source has no "
                        "chat_id")
            return "skipped:no-chat-id"
        name, fn = _announce_send_callable(adapter)
        if fn is None:
            logger.info("wake notice skipped: %s exposes none of %s",
                        type(adapter).__name__,
                        ", ".join(ANNOUNCE_SEND_METHODS))
            return "skipped:no-send-method"
        return _deliver_announcement(state, name, fn, chat_id, text,
                                     logger)
    except Exception as exc:  # never let the extra line affect the wake
        logger.warning("wake notice failed: %s", exc)
        return "failed:%s" % exc


def _send_event_announcement(state: "_BusState", adapter: Any,
                             primary: Dict[str, Any],
                             logger: logging.Logger) -> str:
    """0.8.3 name kept for callers/tests; 0.8.5 makes it level-aware."""
    return _send_wake_notice(state, adapter, primary, logger)


def _announce_on_delivery(state: "_BusState", delivery: "_Delivery",
                          logger: logging.Logger) -> str:
    """0.9.1: the ONE ``📬`` line, sent when the notify really reached the turn.

    Called from :func:`_confirm_injected` / :func:`_confirm_from_message` —
    i.e. at the exact decision point that logs ``DELIVERED`` and lets the
    cursor/ACK move.  Nothing is sent while the batch is queued (``NATIVE-
    QUEUED``), held (``HELD``) or in flight: at that moment the trainer has not
    received anything, so a "收到" line would be both early and untrue.

    Exactly one line per batch, naming the batch's FIRST event (a merged batch
    keeps the 0.8.5 "first origin" rule).  ``delivery.announced`` is the
    in-process guard, so a batch confirmed by two injections still speaks once;
    the 2 s ``(chat, text)`` dedupe of :func:`_send_wake_notice` is untouched.
    ``event`` deliveries never come here — their ⚡ line is sent at wake time.
    """
    try:
        if delivery.mode != "queue" or not delivery.events:
            return "skipped:not-notify"
        first = delivery.events[0]
        if _level(first) != "notify":
            return "skipped:not-notify"
        if delivery.announced:
            return "skipped:already-announced"
        # Claim before sending: a re-entrant confirmation cannot speak twice.
        delivery.announced = True
        adapter = _adapter_for(state)
        if adapter is None:
            logger.info("notify notice skipped: no adapter at delivery time")
            return "skipped:no-adapter"
        return _send_wake_notice(state, adapter, first, logger)
    except Exception as exc:  # never let the extra line affect the delivery
        logger.warning("notify notice failed: %s", exc)
        return "failed:%s" % exc


def _build_wake_event(text: str, internal: bool, source: Any) -> Any:
    """Build the MessageEvent exactly as the gateway expects it.

    ``metadata`` is deliberately left at its default empty dict: the gateway
    session envelope is a closed contract and refuses any non-WhatsApp native
    metadata (``invalid_params (metadata)``), which silently dropped the
    interrupt.  The origin is written into ``text`` instead.
    """
    from gateway.platforms.event import MessageEvent, MessageType
    return MessageEvent(
        text=text, message_type=MessageType.TEXT, source=source,
        internal=internal, inherit_channel_pin=True,
    )


def _adapter_for(state: "_BusState", source: Any = None) -> Any:
    """The delivery adapter for *source* (Feishu first, then any adapter)."""
    gateway = state.gateway
    if gateway is None:
        return None
    source = source if source is not None else state.source
    fn = getattr(gateway, "_delivery_adapter_for", None)
    if callable(fn) and source is not None:
        try:
            adapter = fn(source)
            if adapter is not None:
                return adapter
        except Exception:
            pass
    adapters = getattr(gateway, "adapters", None) or {}
    try:
        from gateway.config import Platform
        adapter = adapters.get(Platform.FEISHU) if hasattr(adapters, "get") else None
        if adapter is not None:
            return adapter
    except Exception:
        pass
    try:
        return next(iter(adapters.values()), None)
    except Exception:
        return None


def _session_key_for(state: "_BusState") -> tuple:
    """``(routing_key, how)`` for the gateway's per-session pending queue.

    Preferred: the gateway's own derivation from the *source captured by
    ``pre_gateway_dispatch``* (``_session_key_for_source``), then the shared
    ``build_session_key``.  Without a source it degrades to the ``session_id``
    reported by ``pre_llm_call``.  The choice is logged once, by name.
    """
    if state.session_key and state.session_key_how.startswith("gateway."):
        # Only a source-derived key is cached: a session_id fallback must be
        # retried once pre_gateway_dispatch hands us the real source.
        return state.session_key, state.session_key_how
    key, how = "", ""
    gateway, source = state.gateway, state.source
    if gateway is not None and source is not None:
        fn = getattr(gateway, "_session_key_for_source", None)
        if callable(fn):
            try:
                key = str(fn(source) or "")
                how = "gateway._session_key_for_source(source)"
            except Exception as exc:
                _get_logger().debug("_session_key_for_source failed: %s", exc)
        if not key:
            try:
                from gateway.session import build_session_key
                key = str(build_session_key(source) or "")
                how = "gateway.session.build_session_key(source)"
            except Exception as exc:
                _get_logger().debug("build_session_key failed: %s", exc)
    if not key and state.session_id:
        key = str(state.session_id)
        how = "session_id fallback (no captured session source)"
    if key and (key != state.session_key or how != state.session_key_how):
        state.session_key, state.session_key_how = key, how
        _get_logger().info("session key for the gateway pending queue: %s "
                           "(from %s)", key, how)
    return key, how


def _wake(state: "_BusState", events: List[Dict[str, Any]], *,
          internal: bool = False, text: Optional[str] = None,
          on_done: Any = None, announce: bool = True) -> bool:
    """Hand a synthesized inbound message to the adapter (``handle_message``).

    ``event`` uses ``internal=False``: the normal inbound path, so a busy
    session is interrupted (or steered) and an idle one starts a turn.
    ``notify`` never comes here any more — it goes through the gateway's own
    pending queue.  ``announce=False`` lets :func:`_dispatch` own the single
    visible notice of a tick that merges both levels (0.8.5).
    Returns True when the coroutine was handed to the loop.
    """
    _resolve_gateway(state)
    _resolve_source(state)
    if state.gateway is None or state.source is None:
        now = time.monotonic()
        if now - getattr(state, "last_wake_warn", -1e9) > 60:
            state.last_wake_warn = now
            _get_logger().info(
                "wake requested but no gateway captured yet "
                "(gateway=%s source=%s); keeping %d event(s) queued "
                "(retried on the next poll / turn end)", state.gateway is not None,
                state.source is not None, len(events))
        return False
    primary = next((e for e in events if _level(e) == "event"), events[0])
    origin = _interrupt_origin(primary)
    if text is None:
        # One origin line only; the cards are injected by pre_llm_call.
        text = _notify_text(events) if internal else _interrupt_text(events)
    try:
        adapter = _adapter_for(state)
        if adapter is None:
            _get_logger().info("wake skipped: no adapter available; batch "
                               "stays queued for the next attempt")
            return False
        ev = _build_wake_event(text, internal, state.source)
        outcome = adapter.handle_message(ev)
        if asyncio.iscoroutine(outcome):
            if state.loop is None:
                _get_logger().warning("wake not handed over: no event loop "
                                      "captured yet; batch stays queued")
                outcome.close()
                return False
            future = asyncio.run_coroutine_threadsafe(outcome, state.loop)
            if on_done is not None:
                future.add_done_callback(on_done)
        elif on_done is not None and isinstance(outcome, str) and outcome.strip():
            # A synchronous adapter answered with a notice: report it now.
            on_done(outcome)
        if not internal and announce:
            # 0.8.4: sent immediately after the hand-over; the helper dedupes
            # (same chat + text within 2 s is dropped) and swallows + logs
            # every failure, so a failed notice can never turn a handed-over
            # wake into a failed one.  0.8.5: _dispatch passes announce=False
            # when it merges both levels into one notice.
            _send_wake_notice(state, adapter, primary, _get_logger())
        state.last_wake = time.monotonic()
        _get_logger().info(
            "woke session internal=%s origin=%r with %d event(s): %s",
            internal, origin, len(events), text.splitlines()[0][:100])
        return True
    except Exception as exc:  # never let the wake path kill the poller
        _get_logger().warning("wake failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# 0.6.0: proactive notify delivery (idle poller + turn end)
# --------------------------------------------------------------------------- #

def _unread_locked(state: "_BusState") -> List[Dict[str, Any]]:
    """Injectable events still queued. Caller holds ``state.lock``."""
    return [e for e in state.events if _level(e) in INJECT_LEVELS]


def _reattach_batch(state: "_BusState",
                    batch: List[Dict[str, Any]]) -> None:
    """Put a batch back at the head of the queue, in order (legacy safety)."""
    with state.lock:
        for event in reversed(batch):
            state.events.appendleft(event)


def _commit_delivery(state: "_BusState",
                     batch: List[Dict[str, Any]]) -> int:
    """Legacy proof path: advance the cursor + ACK for a text-matched batch.

    Only the delivered ids move the cursor (never ``high_water``), so an
    event that arrived while the wake was in flight is not marked read.
    """
    ids = [int(e["id"]) for e in batch if e.get("id") is not None]
    with state.lock:
        if ids:
            state.cursor = max(state.cursor, max(ids))
        cursor = state.cursor
    _save_cursor(state.cursor_path, cursor, state.subscriber)
    _ack_async(state, cursor)
    return cursor


def _busy(state: "_BusState", logger: Optional[logging.Logger] = None) -> bool:
    """Is a turn running for this session right now?

    The gateway's own ``_is_session_running`` wins when it exists (it knows
    about claimed slots the hook flag may have missed); otherwise the
    pre/post-LLM flag is used, with the stale-TTL guard.
    """
    now = time.monotonic()
    if (state.turn_active and state.turn_active_since
            and now - state.turn_active_since > TURN_ACTIVE_TTL):
        if logger is not None:
            logger.warning("turn flag stale after %.0fs; treating as idle",
                           now - state.turn_active_since)
        state.turn_active = False
    key, how = _session_key_for(state)
    gateway = state.gateway
    if key and gateway is not None and not how.startswith("session_id fallback"):
        # Only a source-derived routing key can be trusted with the gateway's
        # own slot lookup; a session_id is not a routing key.
        fn = getattr(gateway, "_is_session_running", None)
        if callable(fn):
            try:
                return bool(fn(key))
            except Exception:
                pass
    return bool(state.turn_active)


def _register_delivery(state: "_BusState", token: str,
                       batch: List[Dict[str, Any]], text: str, mode: str,
                       session_key: str, event: Any, *,
                       prove_by_text: bool = False) -> "_Delivery":
    """Record a handed-over batch as *not yet delivered* (cursor untouched).

    Since 0.8.1 a wake carries only the origin line, so ``prove_by_text`` is
    off: the echo of the wake itself must never count as delivery.  Proof
    comes from :func:`_confirm_injected` — the cards really entering the
    model's context via ``pre_llm_call`` / ``transform_tool_result``.
    """
    delivery = _Delivery(token, batch, text, mode, session_key)
    delivery.event = event
    if not prove_by_text:
        delivery.marker = ""
    with state.lock:
        state.inflight[token] = delivery
        for item in batch:
            state.queued_keys[_event_key(item)] = None
        while len(state.queued_keys) > QUEUED_KEYS_MAX:
            state.queued_keys.popitem(last=False)
    return delivery


def _mark_woken(state: "_BusState", batch: List[Dict[str, Any]]) -> None:
    """0.8.2: claim a batch for the wake path before it is handed over.

    Called *before* ``_wake`` so ``transform_tool_result`` (another thread)
    cannot slip an insert in between the hand-over and the mark.  The claim is
    released by :func:`_forget_delivery`, i.e. on any failed hand-over, on a
    gateway refusal/raise and on the TTL retry — never while the wake is still
    expected to prove delivery through ``pre_llm_call``.
    """
    with state.lock:
        for event in batch:
            state.woken_ids[_event_key(event)] = None
        while len(state.woken_ids) > QUEUED_KEYS_MAX:
            state.woken_ids.popitem(last=False)


def _forget_delivery(state: "_BusState", token: str) -> Optional["_Delivery"]:
    """Drop a delivery from the ledger and free its de-duplication keys."""
    with state.lock:
        delivery = state.inflight.pop(token, None)
        if delivery is not None:
            for item in delivery.events:
                key = _event_key(item)
                state.queued_keys.pop(key, None)
                # 0.8.2: the wake claim dies with the delivery, so a refused
                # wake hands the event back to transform_tool_result.
                state.woken_ids.pop(key, None)
    return delivery


def _delivery_failed(state: "_BusState", logger: logging.Logger,
                     delivery: "_Delivery", reason: str) -> None:
    """A hand-over that cannot be delivered: keep the events, retry later."""
    _forget_delivery(state, delivery.token)
    if delivery.mode == "queue" and _still_in_gateway_queue(state, delivery):
        # The gateway still holds this exact event: re-queueing a copy would
        # deliver it twice.  Only its clock is reset.
        delivery.created_at = time.monotonic()
        delivery.phase = "queued"
        with state.lock:
            state.inflight[delivery.token] = delivery
            for item in delivery.events:
                state.queued_keys[_event_key(item)] = None
        logger.warning(
            "delivery FAILED (%s) %s but the event is still parked in the "
            "gateway queue; kept in place, cursor stays %d",
            reason, delivery.describe(), state.cursor)
        return
    # The cards were never detached from the local queue (0.8.1), so there is
    # nothing to put back: clearing the marks is all the retry needs.
    if not _events_queued(state, delivery.events):
        _reattach_batch(state, delivery.events)
    logger.warning(
        "delivery FAILED (%s) %s; %d event(s) stay in the local queue, "
        "cursor stays %d, retry on the next poll",
        reason, delivery.describe(), len(delivery.events), state.cursor)


def _events_queued(state: "_BusState",
                   batch: List[Dict[str, Any]]) -> bool:
    """Is every event of *batch* still resident in the local queue?"""
    with state.lock:
        resident = {id(event) for event in state.events}
    return all(id(event) in resident for event in batch)


def _still_in_gateway_queue(state: "_BusState", delivery: "_Delivery") -> bool:
    """Is this exact event still parked in the gateway's pending slot/FIFO?"""
    if delivery.event is None or delivery.native:
        # 0.9.0: a held batch never entered the gateway, and a native-queued batch
        # lives in the session authority's durable FIFO (state.db), not in the
        # adapter slot — ``slot.get() is None`` must not read as "still parked".
        return False
    adapter = _adapter_for(state)
    slot = getattr(adapter, "_pending_messages", None) if adapter is not None else None
    if isinstance(slot, dict) and slot.get(delivery.session_key) is delivery.event:
        return True
    gateway = state.gateway
    fn = getattr(gateway, "_overflow_queue", None)
    if callable(fn):
        try:
            overflow = fn(delivery.session_key) or []
        except Exception:
            overflow = []
        if any(event is delivery.event for event in overflow):
            return True
    return False


def _retry_inflight(state: "_BusState", logger: logging.Logger) -> int:
    """At-least-once: a batch not proven delivered within ``BATCH_TTL`` retries.

    A notify still sitting in the gateway's own queue is *not* a failure — the
    gateway has not drained it yet; only its clock is refreshed.  Since 0.8.1
    the cards never leave the local queue, so a stale wake only needs its
    dedupe marks cleared to be retried; nothing is re-attached (that would
    duplicate a batch that is already resident).
    """
    now = time.monotonic()
    with state.lock:
        stale = [d for d in state.inflight.values()
                 if now - d.created_at >= BATCH_TTL]
    retried = 0
    for delivery in stale:
        if delivery.native:
            # 0.9.0: handed to the session authority's durable FIFO. Its drain
            # runs the row when the running turn finishes; re-waking would queue
            # the same notify twice. Only the clock is refreshed.
            delivery.created_at = now
            logger.debug("native-queued notify still waiting for its FIFO turn "
                         "(%s); not retried", delivery.describe())
            continue
        if delivery.mode == "queue" and _still_in_gateway_queue(state, delivery):
            delivery.created_at = now
            logger.debug("delivery still waiting in the gateway queue (%s); "
                         "not retried", delivery.describe())
            continue
        still_queued = _events_queued(state, delivery.events)
        _forget_delivery(state, delivery.token)
        if not still_queued:
            _reattach_batch(state, delivery.events)
        retried += 1
        logger.warning(
            "delivery not PROVEN after %.0fs (%s); %s, next poll retries",
            now - delivery.created_at, delivery.describe(),
            "%d event(s) still in the local queue" % len(delivery.events)
            if still_queued else "requeued %d event(s)" % len(delivery.events))
    return retried


def _run_on_gateway_loop(state: "_BusState", fn: Any, *args: Any,
                         timeout: float = 2.0) -> bool:
    """Run a sync gateway call on the gateway's loop thread when possible.

    The gateway mutates ``_pending_messages`` / session state from its loop, so
    queueing from the poller thread through ``call_soon_threadsafe`` keeps the
    two serialised.  Falls back to an inline call when no loop was captured
    (tests, or a hook capture that never fired).  False = the loop did not run
    it in time; the caller keeps the event and retries.
    """
    loop = state.loop
    if loop is None:
        fn(*args)
        return True
    done = threading.Event()
    box: Dict[str, BaseException] = {}

    def _run() -> None:
        try:
            fn(*args)
        except BaseException as exc:  # noqa: BLE001 - re-raised on our thread
            box["error"] = exc
        finally:
            done.set()

    try:
        loop.call_soon_threadsafe(_run)
    except Exception as exc:  # closed/foreign loop
        _get_logger().warning("gateway loop refused the call (%s); calling inline", exc)
        fn(*args)
        return True
    if not done.wait(timeout):
        _get_logger().warning("gateway loop did not run the queue call within "
                              "%.0fs; event kept for a retry", timeout)
        return False
    error = box.get("error")
    if error is not None:
        raise error
    return True


def _native_queue_available(state: "_BusState") -> bool:
    """Is Hermes' own busy queue reachable?  (The same FIFO the WebUI uses.)

    That is the mechanism the trainer wants reused instead of a plugin-made
    "drain at end of turn": with ``runner.session_authority`` present,
    ``BasePlatformAdapter._shared_runtime_owns_input`` is true, so a
    ``handle_message`` on a BUSY session never goes through the busy-input mode
    (interrupt/steer); it is committed as a durable admission row and executed
    by the authority's FIFO once the running turn has finished:

        adapter.handle_message(event)                  gateway/platforms/base.py:4045
          -> _handle_message_while_active              gateway/platforms/base.py:4017
          -> dispatch_busy_burst                       gateway/session_ingress_burst.py:102
          -> dispatch_shared_busy -> admit_message     gateway/session_ingress.py:497,39
          -> SessionAuthority.admit_native             gateway/session_authority.py:522
        and the next row is claimed only when no turn is executing:
          SessionAuthority._drain / _claim_with_retry  gateway/session_authority.py:1309

    "Turn finished" is therefore Hermes' own notion (the producer turn closes),
    not a guess made here.
    """
    return getattr(state.gateway, "session_authority", None) is not None


def _notify_enqueue(state: "_BusState", logger: logging.Logger,
                    batch: List[Dict[str, Any]], *, where: str) -> bool:
    """``notify`` -> queue (internal=True); never interrupt, never inside a turn.

    Idle: the gateway's own per-session pending queue, drained in this tick.
    Busy (0.9.0): Hermes' own busy queue via ``handle_message`` — the session
    authority's durable FIFO runs it after the current turn finishes — with a
    plugin-side held batch as the fallback when no authority is reachable.

    No timer, no self-made wake: the gateway drains this queue itself at the
    end of the running turn.  Nothing is ACKed here; the batch is only marked
    in flight and waits for ``pre_llm_call`` to prove it reached the context.
    """
    _resolve_gateway(state)
    _resolve_source(state)
    if not batch:
        return False
    with state.lock:
        fresh = [e for e in batch if _event_key(e) not in state.queued_keys]
    if not fresh:
        logger.debug("notify dedupe: ids=%s already queued or in flight "
                     "(where=%s)", ",".join(_event_key(e) for e in batch), where)
        return False
    session_key, how = _session_key_for(state)
    if _busy(state, logger):
        # 0.9.0 — "queue" means AT THE TURN BOUNDARY, not "at the next gap".
        # The gateway's pending slot is drained at ANY turn end (including one an
        # interrupt cut short) and the adapter then starts a new turn from it;
        # starting a turn interrupts the running one and kills its in-flight
        # tool process.  A notify that arrives while the trainer's turn still
        # runs therefore never goes there.
        text = _notify_text(fresh)
        ids = [int(e["id"]) for e in fresh if e.get("id") is not None]
        token = "q:" + (",".join(str(i) for i in ids) or _event_key(fresh[0]))
        if (state.source is not None and _adapter_for(state) is not None
                and _native_queue_available(state)):
            # Preferred: Hermes' own queue (the WebUI one).  handle_message on a
            # busy session commits a durable admission and its FIFO runs it
            # after this turn is over — no plugin-side turn-end guessing.
            if _wake(state, fresh, internal=True, text=text, announce=False):
                delivery = _register_delivery(state, token, fresh, text,
                                              "queue", session_key, None)
                delivery.phase = "in_flight"
                delivery.native = True
                logger.info("notify NATIVE-QUEUED n=%d ids=%s session_key=%s "
                            "via=%s busy=1 route=handle_message->admit_native "
                            "where=%s",
                            len(fresh), ",".join(str(i) for i in ids) or "-",
                            session_key or "-", how or "-", where)
                return True
            logger.warning("native queue hand-over failed; holding ids=%s "
                           "locally for the turn boundary (where=%s)",
                           ",".join(str(i) for i in ids) or "-", where)
        # Fallback (no session authority / no source yet): keep it HERE.  No
        # slot write, no wake, no handle_message.  _drain_idle() wakes it only
        # after _busy() is False again.
        delivery = _register_delivery(state, token, fresh, text, "queue",
                                      session_key, None)
        delivery.phase = "held"
        logger.info("notify HELD n=%d ids=%s session_key=%s via=%s busy=1 "
                    "phase=held reason=turn_still_running where=%s",
                    len(fresh), ",".join(str(i) for i in ids) or "-",
                    session_key or "-", how or "-", where)
        return True
    gateway = state.gateway
    if gateway is None:
        logger.warning("notify cannot be queued: no gateway captured yet; "
                       "%d event(s) stay in the local queue (where=%s)",
                       len(fresh), where)
        return False
    queue_fn = getattr(gateway, "_queue_or_replace_pending_event", None)
    if not callable(queue_fn):
        logger.warning("notify cannot be queued: gateway has no "
                       "_queue_or_replace_pending_event; %d event(s) stay "
                       "queued (where=%s)", len(fresh), where)
        return False
    if not session_key:
        logger.warning("notify cannot be queued: no session key yet (no "
                       "captured source and no session_id); %d event(s) stay "
                       "queued (where=%s)", len(fresh), where)
        return False
    if _adapter_for(state) is None:
        logger.warning("notify cannot be queued: gateway exposes no adapter; "
                       "%d event(s) stay queued (where=%s)", len(fresh), where)
        return False
    text = _notify_text(fresh)
    event = _build_wake_event(text, True, state.source)
    ids = [int(e["id"]) for e in fresh if e.get("id") is not None]
    try:
        ran = _run_on_gateway_loop(state, queue_fn, session_key, event)
        accepted = ran and getattr(event, "_gateway_accepted", False) is True
    except Exception as exc:  # noqa: BLE001 - a refused queue is never a loss
        accepted = False
        logger.warning("gateway queue call raised: %s", exc)
    if not accepted:
        logger.warning(
            "gateway REFUSED the queued notify (no adapter / queue cap / no "
            "drainable slot) n=%d session_key=%s via=%s; event(s) kept in the "
            "local queue, retry on next poll (where=%s)",
            len(fresh), session_key, how, where)
        return False
    token = "q:" + (",".join(str(i) for i in ids) or _event_key(fresh[0]))
    _register_delivery(state, token, fresh, text, "queue", session_key, event)
    logger.info("notify QUEUED n=%d ids=%s session_key=%s via=%s "
                "internal=true busy=%d where=%s",
                len(fresh), ",".join(str(i) for i in ids) or "-",
                session_key, how, 1 if _busy(state) else 0, where)
    return True


def _queue_drain_entry(state: "_BusState") -> tuple:
    """Find the gateway's "process the head of the queue" entry by name.

    Probed with ``getattr`` only: today's gateway exposes none of these, and
    the plugin must never invent one.  Returns ``(name, callable)`` or ``("", None)``.
    """
    gateway = state.gateway
    for name in DRAIN_ENTRY_NAMES:
        fn = getattr(gateway, name, None)
        if callable(fn):
            return name, fn
    return "", None


def _after_handle_message(state: "_BusState", logger: logging.Logger,
                          delivery: "_Delivery", result: Any,
                          error: Optional[BaseException]) -> None:
    """Inspect the drained turn's outcome (never blocks the poller)."""
    if delivery.token not in state.inflight:   # already proven by pre_llm_call
        return
    if error is not None:
        _delivery_failed(state, logger, delivery, "handle_message raised: %s" % error)
        return
    if isinstance(result, str) and result.strip():
        # A returned string is a notice/rejection, not a turn that ran (the
        # lease-timeout path answers "another turn is running").
        _delivery_failed(state, logger, delivery,
                         "gateway rejected the turn: %r" % result.strip()[:120])
        return
    logger.debug("drain: handle_message accepted %s; waiting for pre_llm_call "
                 "proof", delivery.describe())


def _drain_held(state: "_BusState", logger: logging.Logger,
                delivery: "_Delivery", *, where: str) -> str:
    """0.9.0: wake a notify that was held back while the turn was still running.

    Only reachable from :func:`_drain_idle`, i.e. with ``_busy()`` False: the
    turn really ended.  The wake is ``internal=True`` — the exact call the idle
    path has always made — and the cards ride that new turn's ``pre_llm_call``,
    which is the delivery proof.  Nothing here touches the cursor.
    """
    if _busy(state, logger):
        return "busy"
    text = delivery.text or _notify_text(delivery.events)
    token = delivery.token
    if not _wake(state, delivery.events, internal=True, text=text,
                 on_done=_interrupt_done(state, logger, token), announce=False):
        # No gateway/source/loop yet: keep it queued, retry on the next tick.
        return "blocked"
    delivery.phase = "in_flight"
    ids = ", ".join(str(e.get("id")) for e in delivery.events)
    logger.info("notify DRAINED (turn finished) %s ids=%s cursor stays %d "
                "woke=internal=true where=%s",
                delivery.describe(), ids or "-", state.cursor, where)
    # No notice here: 0.9.1 sends the 📬 line only when the batch is really
    # injected (pre_llm_call proof), never at HELD and never at this drain.
    return "drained"


def _drain_head(state: "_BusState", logger: logging.Logger,
                delivery: "_Delivery", *, where: str) -> str:
    """Process the head of this session's pending queue (idle only).

    Strategy, in order: a batch held back by 0.9.0 (never handed over) is woken
    with ``_wake(internal=True)``; else a gateway drain entry found by name;
    else pop the head with ``adapter.get_pending_message`` — only when it is
    *our* event — and hand that same event to ``adapter.handle_message``.  The
    strategy is logged (once per kind) so the journal always says which one ran.
    """
    if delivery.event is None:
        return _drain_held(state, logger, delivery, where=where)
    adapter = _adapter_for(state)
    if adapter is None:
        return "no-adapter"
    name, fn = _queue_drain_entry(state)
    if fn is not None:
        try:
            outcome = fn(delivery.session_key)
            if asyncio.iscoroutine(outcome):
                asyncio.run_coroutine_threadsafe(outcome, state.loop)
            delivery.phase = "in_flight"
            if state.drain_entry != name:
                state.drain_entry = name
                logger.info("drain: using gateway.%s(session_key=%s)",
                            name, delivery.session_key)
            return "drained"
        except Exception as exc:  # noqa: BLE001
            logger.warning("drain: gateway.%s raised: %s", name, exc)
            return "error"
    slot = getattr(adapter, "_pending_messages", None)
    head = slot.get(delivery.session_key) if isinstance(slot, dict) else None
    if head is None:
        delivery.phase = "in_flight"
        logger.info("drain: pending slot already empty for %s (gateway drained "
                    "it itself, where=%s); waiting for pre_llm_call proof",
                    delivery.session_key, where)
        return "empty"
    if head is not delivery.event:
        logger.info("drain: queue head for %s is another message; our notify "
                    "keeps its place behind it (where=%s)",
                    delivery.session_key, where)
        return "blocked"
    pop = getattr(adapter, "get_pending_message", None)
    if not callable(pop):
        logger.warning(
            "drain: no gateway drain entry (%s) and no "
            "adapter.get_pending_message; the notify stays queued until the "
            "next turn ends", "/".join(DRAIN_ENTRY_NAMES))
        return "no-entry"
    head = pop(delivery.session_key)
    if head is not delivery.event:
        if head is not None and isinstance(slot, dict):
            slot[delivery.session_key] = head
        return "blocked"
    delivery.phase = "in_flight"
    try:
        outcome = adapter.handle_message(head)
    except Exception as exc:  # noqa: BLE001 - a sync refusal is a normal refusal
        delivery.phase = "queued"
        logger.warning("drain: handle_message raised: %s", exc)
        return "error"
    if asyncio.iscoroutine(outcome):
        if state.loop is None:
            outcome.close()
            delivery.phase = "queued"
            return "error"
        try:
            future = asyncio.run_coroutine_threadsafe(outcome, state.loop)
        except Exception as exc:  # stale loop (gateway restarted)
            logger.warning("drain: cannot schedule handle_message: %s", exc)
            delivery.phase = "queued"
            return "error"
        future.add_done_callback(
            lambda fut: _after_handle_message(
                state, logger, delivery,
                None if fut.cancelled() else (None if fut.exception() else fut.result()),
                None if fut.cancelled() else fut.exception()))
    else:
        _after_handle_message(state, logger, delivery, outcome, None)
    if state.drain_entry != "adapter.get_pending_message+handle_message":
        state.drain_entry = "adapter.get_pending_message+handle_message"
        logger.info(
            "drain: no gateway drain entry (%s); used "
            "adapter.get_pending_message + handle_message for session_key=%s "
            "(where=%s). %s is busy-only and was not called: the session is idle",
            "/".join(DRAIN_ENTRY_NAMES) or "none",
            delivery.session_key, where, BUSY_ONLY_ENTRY)
    return "drained"


def _drain_idle(state: "_BusState", logger: logging.Logger, *, where: str) -> bool:
    """When nothing runs, start the oldest queued notify batch."""
    if _busy(state, logger):
        return False
    with state.lock:
        waiting = [d for d in state.inflight.values()
                   if d.mode == "queue" and d.phase in ("queued", "held")]
    if not waiting:
        return False
    delivery = waiting[0]
    status = _drain_head(state, logger, delivery, where=where)
    if status in ("drained", "empty"):
        return True
    if status == "error":
        _delivery_failed(state, logger, delivery, "drain failed")
    return False


def _interrupt_done(state: "_BusState", logger: logging.Logger, token: str) -> Any:
    """Outcome hook for an interrupt wake: a rejection requeues the batch."""

    def _callback(outcome: Any) -> None:
        delivery = state.inflight.get(token)
        if delivery is None:          # already proven by pre_llm_call
            return
        value: Any = outcome
        error: Optional[BaseException] = None
        if hasattr(value, "result") and callable(getattr(value, "result")):
            try:
                value = value.result()
            except BaseException as exc:  # noqa: BLE001 - includes CancelledError
                error, value = exc, None
        if error is not None:
            _delivery_failed(state, logger, delivery,
                             "handle_message raised: %s" % error)
            return
        if isinstance(value, str) and value.strip():
            _delivery_failed(state, logger, delivery,
                             "gateway rejected the turn: %r" % value.strip()[:120])

    return _callback


def _interrupt_wake(state: "_BusState", logger: logging.Logger,
                    batch: List[Dict[str, Any]], *, where: str,
                    announce: bool = True) -> bool:
    """``event`` -> interrupt: the unchanged ``internal=False`` path.

    Busy: the running turn is interrupted at once; idle: a turn starts.  The
    wake message is only the interrupter line — the cards stay in the local
    queue and the turn it starts injects them via ``pre_llm_call`` (0.8.1).
    The ids are marked in the ledger so the same event is not woken twice
    before that injection proves it, and (0.8.2) the ``woken_ids`` claim keeps
    ``transform_tool_result`` from inserting the same card mid-turn.  Every
    failed hand-over releases that claim (see ``_forget_delivery``).
    """
    _resolve_gateway(state)
    _resolve_source(state)
    if not batch:
        return False
    if state.gateway is None or state.source is None:
        logger.warning("event interrupt cannot be handed over (gateway=%s "
                       "source=%s); %d event(s) stay queued (where=%s)",
                       state.gateway is not None, state.source is not None,
                       len(batch), where)
        return False
    session_key, how = _session_key_for(state)
    if not session_key:
        logger.warning("event interrupt has no session key yet (no captured "
                       "source, no session_id); delivery proof will not be "
                       "matched, the event stays retryable")
    busy = _busy(state, logger)
    text = _interrupt_text(batch)
    ids = [int(e["id"]) for e in batch if e.get("id") is not None]
    token = "i:" + (",".join(str(i) for i in ids) or _event_key(batch[0]))
    # Register BEFORE the hand-over: the new turn's pre_llm_call can fire as
    # soon as the coroutine lands on the loop, and it must find the receipt.
    delivery = _register_delivery(state, token, batch, text, "interrupt",
                                  session_key or "", None)
    # 0.8.2: claim the batch for this wake before it is handed over, so the
    # mid-turn hook cannot insert the same card while the interrupt lands.
    # _forget_delivery (hand-over failure / refusal / TTL) releases it.
    _mark_woken(state, batch)
    if not _wake(state, batch, internal=False, text=text,
                 on_done=_interrupt_done(state, logger, token),
                 announce=announce):
        _forget_delivery(state, token)
        logger.warning("event interrupt not handed to the gateway; %d "
                       "event(s) kept queued (where=%s)", len(batch), where)
        return False
    logger.info("event INTERRUPT n=%d ids=%s internal=false busy=%d "
                "session_key=%s via=%s where=%s cards=queued-for-pre_llm_call",
                len(batch), ",".join(str(i) for i in ids) or "-", 1 if busy else 0,
                session_key or "-", how or "-", where)
    return True


def _dispatch(state: "_BusState", logger: logging.Logger, *, where: str) -> bool:
    """One delivery attempt for everything unread.  The level alone decides.

    ``event`` -> interrupt (``internal=False``); ``notify`` -> gateway queue
    (``internal=True``).  Queued batches are drained when idle, and any batch
    without delivery proof past its TTL is retried first.
    """
    _retry_inflight(state, logger)
    with state.lock:
        # A batch already woken/queued is not woken again until it is injected
        # (or its TTL clears the marks): the cards now wait in this queue.
        # 0.8.2: an event transform_tool_result already inserted mid-turn is
        # never woken afterwards either — one event, one route.
        unread = [e for e in _unread_locked(state)
                  if _event_key(e) not in state.queued_keys
                  and not (_level(e) in INTERRUPT_LEVELS
                           and _event_key(e) in state.interrupt_done)]
    events = [e for e in unread if _level(e) == "event"]
    notifies = [e for e in unread if _level(e) == "notify"]
    did = False
    interrupted = False
    notify_queued = False
    try:
        if events:
            # 0.8.5: _dispatch sends the one visible notice for this tick, so a
            # merged event+notify batch never produces two lines.
            interrupted = _interrupt_wake(state, logger, events, where=where,
                                          announce=False)
            did = interrupted or did
        if notifies:
            notify_queued = _notify_enqueue(state, logger, notifies, where=where)
            did = notify_queued or did
        if not interrupted:
            # One turn at a time: do not start a queued notify in the same tick
            # an interrupt just claimed the session (the lease would refuse it).
            did = _drain_idle(state, logger, where=where) or did
    finally:
        # 0.8.5: exactly one visible notice per wake, even if a later leg of
        # this tick raises.  0.9.1: only an EVENT wake still speaks here — its
        # ⚡ line is immediate and unchanged.  A notify batch is silent while it
        # is queued/held; its 📬 line is sent by _announce_on_delivery when the
        # cards are really injected (the DELIVERED/cursor point).
        notice = None
        if unread:
            first = unread[0]
            if _level(first) == "event" and interrupted:
                notice = first
            elif interrupted and events:
                notice = events[0]
        if notice is not None:
            adapter = _adapter_for(state)
            if adapter is not None:
                _send_wake_notice(state, adapter, notice, logger)
    return did


def _proactive_wake(state: "_BusState", logger: logging.Logger, *,
                    where: str) -> bool:
    """0.7 name kept for callers/tests: one shared delivery attempt."""
    return _dispatch(state, logger, where=where)


def _message_text(value: Any) -> str:
    """Flatten a hook's ``user_message`` (str or content-block list) to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content"):
                    piece = item.get(key)
                    if isinstance(piece, str):
                        parts.append(piece)
                        break
        return " ".join(parts)
    return str(value or "")


def _confirm_from_message(state: "_BusState", user_message: Any) -> int:
    """Legacy text proof: a card-bearing wake echoed as ``user_message``.

    Since 0.8.1 wakes carry no cards and register no marker, so this path is
    inert; :func:`_confirm_injected` is the only real proof.  Kept so a
    text-bearing wake (tests / an older deployment) still commits.
    """
    text = " ".join(_message_text(user_message).split())
    if not text:
        return 0
    with state.lock:
        hits = [d for d in state.inflight.values()
                if d.marker and d.marker in text]
    for delivery in hits:
        cursor = _commit_delivery(state, delivery.events)
        _forget_delivery(state, delivery.token)
        ids = ",".join(str(e.get("id")) for e in delivery.events)
        _get_logger().info(
            "DELIVERED %s confirmed by pre_llm_call (ids=%s cursor=%d ACKed)",
            delivery.describe(), ids, cursor)
        # 0.9.1: the 📬 line belongs to this moment, not to the enqueue.
        _announce_on_delivery(state, delivery, _get_logger())
    return len(hits)


def _confirm_injected(state: "_BusState",
                      events: List[Dict[str, Any]]) -> int:
    """Delivery proof: this batch really entered the model's context.

    The wake message no longer carries the cards (0.8.1), so the only honest
    proof is the injection itself: ``pre_llm_call`` handing the batch to the
    turn it started, or ``transform_tool_result`` appending the card mid-turn.
    A delivery is proven when every event it holds is in *events*.  The cursor
    and the ACK already moved in :func:`_take_events`; this only clears the
    ledger and its de-duplication marks.
    """
    keys = {_event_key(e) for e in events if isinstance(e, dict)}
    if not keys:
        return 0
    with state.lock:
        hits = [d for d in state.inflight.values()
                if keys.issuperset({_event_key(e) for e in d.events})]
    for delivery in hits:
        _forget_delivery(state, delivery.token)
        ids = ",".join(str(e.get("id")) for e in delivery.events)
        _get_logger().info(
            "DELIVERED %s confirmed by context injection (ids=%s cursor=%d)",
            delivery.describe(), ids, state.cursor)
        # 0.9.1: the ONE visible 📬 line, at the same decision point as
        # DELIVERED/cursor — never at enqueue, never at HELD/NATIVE-QUEUED.
        _announce_on_delivery(state, delivery, _get_logger())
    return len(hits)


def _post_turn_dispatch(state: "_BusState", key: str) -> None:
    """Wait out the settle delay, then one shared delivery attempt."""
    try:
        if POST_TURN_DELAY > 0:
            time.sleep(POST_TURN_DELAY)
        _dispatch(state, _get_logger(), where="post_llm_call")
    except Exception:  # noqa: BLE001 - a thread must never die loudly here
        _get_logger().exception("post-turn dispatch failed (%s)", key)


def _on_post_llm_call(**kwargs: Any) -> None:
    """The turn really ended: clear the busy flag, then deliver what waited.

    A queued ``notify`` needs nothing from this hook any more — the gateway
    drains its own pending queue at the end of the turn.  This dispatch only
    matters for an ``event`` that arrived after the last tool batch, and for a
    notify whose turn-end drain the gateway missed (idle drain catches it).
    """
    try:
        if _disabled():
            return None
        state = _STATE
        if state is None:
            return None
        platform = str(kwargs.get("platform") or "").strip().lower()
        if platform in NON_TURN_PLATFORMS:
            # Background turn: must not clear the trainer's busy flag.
            return None
        with state.lock:
            state.turn_active = False
            state.turn_active_since = 0.0
        session_id = str(kwargs.get("session_id") or "")
        turn_id = str(kwargs.get("turn_id") or "")
        key = "%s|%s" % (session_id, turn_id)
        with state.lock:
            if key in state.post_turn_marks:
                return None
            has_work = bool(_unread_locked(state)) or any(
                d.mode == "queue" and d.phase in ("queued", "held")
                for d in state.inflight.values())
            if not has_work:
                _get_logger().debug("post_llm_call: nothing unread; no dispatch")
                return None
            state.post_turn_marks[key] = None
            while len(state.post_turn_marks) > 200:
                state.post_turn_marks.popitem(last=False)
        threading.Thread(target=_post_turn_dispatch, args=(state, key),
                         name="trigger-bus-post-dispatch", daemon=True).start()
    except Exception:  # noqa: BLE001 - never break the reply path
        _get_logger().exception("post_llm_call failed; nothing dispatched")
    return None


def _on_pre_llm_call(**_kwargs: Any) -> Optional[Dict[str, str]]:
    if _disabled():
        return None
    try:
        state = _STATE
        if state is None:
            return None

        # Delivery proof.  The wake only carries the origin line, so the proof
        # is the cards entering the context below (never the wake echoed back
        # as ``user_message``).  ``_confirm_from_message`` stays for a legacy
        # text-bearing wake; current wakes register no text marker.
        session_id = str(_kwargs.get("session_id") or "")
        if session_id:
            state.session_id = session_id
        if not state.session_key:
            _session_key_for(state)
        _confirm_from_message(state, _kwargs.get("user_message"))

        # Busy flag: from here until post_llm_call this session is running a
        # turn, so the idle poller must not stack another wake on top.  A
        # subagent/cron turn is background work and must not claim it.
        platform = str(_kwargs.get("platform") or "").strip().lower()
        if platform not in NON_TURN_PLATFORMS:
            with state.lock:
                state.turn_active = True
                state.turn_active_since = time.monotonic()

        taken = _take_events(state, levels=INJECT_LEVELS)
        if taken is None:
            return None
        events, cursor, dropped = taken

        # Belt and braces: an event already inserted mid-turn by
        # transform_tool_result must not be handed out again here.
        events = [e for e in events
                  if _level(e) not in INTERRUPT_LEVELS
                  or not _interrupt_injected(state, e)]
        if not events:
            return None

        context = _format_events(events)
        if dropped:
            context += ("\n（队列已满，较早的 %d 条事件被丢弃）" % dropped)
        _confirm_injected(state, events)
        _get_logger().info("injected %d event(s) into pre_llm_call context "
                           "(cursor=%d, chars=%d)", len(events), cursor,
                           len(context))
        return {"context": context}
    except Exception:  # noqa: BLE001 - never break the model call
        _get_logger().exception("pre_llm_call failed; no context injected")
        return None


def _on_transform_tool_result(tool_name: str = "", args: Any = None,
                              result: Any = None, **_: Any) -> Optional[str]:
    """Mid-turn interrupt: append a not-yet-injected top-level event to a tool result.

    ``pre_llm_call`` fires once per turn, so it cannot interrupt work already
    in progress.  Every tool result passes through this hook instead: when an
    ``event`` is queued, the note is appended to the output and the model
    sees it on the very next step.  ``notify`` is deliberately not eligible:
    it is queued, never interrupts, and is delivered right after the turn.
    ``_take_interrupt`` guarantees at-most-once, and 0.8.2 also skips every
    event already claimed by a wake (``woken_ids``) so one event never takes
    both routes.  A wake that fails releases the claim, which is exactly when
    this hook becomes the fallback.
    """
    try:
        if _disabled():
            return None
        state = _STATE
        if state is None or not isinstance(result, str):
            return None
        event = _take_interrupt(state)
        if event is None:
            return None
        note = _format_interrupt(event)
        _confirm_injected(state, [event])
        _get_logger().info(
            "EVENT inserted mid-turn via %s: source=%s id=%s chars=%d",
            tool_name or "?", event.get("source"), event.get("id"),
            len(str(event.get("text") or "")))
        return result + note
    except Exception:  # noqa: BLE001 - never break the tool loop
        _get_logger().exception(
            "transform_tool_result failed; no EVENT inserted")
        return None


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #

_SINGLETON_LOCK = None


def _acquire_singleton_lock() -> bool:
    """Only one plugin instance may poll: hot reload/disable leaves old hooks alive
    in the gateway until a restart, and each import gets its own module-level state.
    A process-wide file lock keeps a single poller; the others register nothing."""
    global _SINGLETON_LOCK
    if _SINGLETON_LOCK is not None:
        return True
    try:
        import fcntl
        path = Path(os.path.expanduser(
            os.environ.get("TRIGGER_BUS_LOCK") or str(DEFAULT_LOCK)))
        handle = open(path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _SINGLETON_LOCK = handle
        return True
    except OSError:
        return False


def register(ctx) -> None:
    global _STATE
    logger = _get_logger()
    if _disabled():
        logger.info("TRIGGER_BUS_DISABLED=1; plugin inactive")
        return
    if _STATE is not None:
        logger.debug("register() called twice; ignoring")
        return
    if not _acquire_singleton_lock():
        logger.info("another trigger-bus instance owns the poll lock; "
                    "this import stays inactive")
        return
    state = _BusState()
    if state.loop is None:
        try:
            state.loop = asyncio.get_event_loop_policy().get_event_loop()
        except Exception:
            state.loop = None
    _STATE = state
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    try:
        ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    except Exception:
        logger.exception("transform_tool_result hook unavailable; top-level "
                         "event will only be injected at the turn boundary")
    try:
        ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    except Exception as exc:
        logger.warning("pre_gateway_dispatch hook unavailable (%s); "
                       "wake-on-event disabled, inject-only mode", exc)
    try:
        ctx.register_hook("post_llm_call", _on_post_llm_call)
    except Exception as exc:
        logger.warning("post_llm_call hook unavailable (%s); notify will "
                       "wait for the next turn boundary", exc)
    thread = threading.Thread(target=_poll_loop, args=(state,),
                              name="trigger-bus-poll", daemon=True)
    state.thread = thread
    thread.start()
    logger.info("registered pre_llm_call + post_llm_call + "
                "transform_tool_result hooks; cursor=%d path=%s",
                state.cursor, state.cursor_path)
