# trigger-bus (0.9.4)

Deliver events from the local **mowu-trigger** service (127.0.0.1:8791) into a
Hermes session — including when the trainer is not talking.

The service owns ingestion and storage; this plugin is only a consumer: a
daemon thread polls `GET /events` every 2 s from a persisted cursor. Every
failure is swallowed to `trigger-bus.log`, so a dead service leaves the agent
behaving exactly as if the plugin were absent.

## 唤醒消息 = 一行来源；卡片走注入路径

The wake message is **exactly one line** — no card, no metadata:

```
⚡ 来自 selftest · Tester 的打断          event  (interrupt)
📬 收到来自 email · HR 的 Notify 消息    notify (queue)
```

The `event` line is a **contract with the gateway**: it matches the strict
prefix `⚡ 来自 <source> · <actor> 的打断` and must never be changed. Alongside
the gateway's own English `⚡ Interrupting current task …` hint, the session
also shows one extra Chinese line that this plugin sends itself (0.8.3/0.8.4):

```
⚡ 收到来自 selftest · Tester 的 Event 消息
```

Never start an event wake with that line: the wake text must stay
`⚡ 来自 … 的打断`. The `notify` wake line is the 0.8.2 Chinese notice
`📬 收到来自 <source> · <actor> 的 Notify 消息`. Since 0.8.5 the plugin also
sends that same line through the adapter so it becomes visible in Feishu, and
**since 0.9.1 it sends it only when the notify is really delivered** (the
`pre_llm_call` injection), never when it is queued:

```
📬 收到来自 email · HR 的 Notify 消息
```

Both notices share one mechanism (immediate inline send, one per wake, 2 s
`(chat, text)` dedupe); the texts differ, so the two levels never suppress
each other. `info` / `log` send nothing.

The cards of that same batch are injected into the turn the wake starts, by the
usual `pre_llm_call` path, in the unchanged two-line format:

```
#57 event selftest/Tester 20:42 thr=mc_chat:global
> 打断测试：…
```

`<source> · <actor>` is the same origin the 0.8.0 gateway ack used to read from
`metadata["interrupt_origin"]`. **No metadata is set at all** — the gateway
session envelope (`gateway/session_envelope.py`) is a closed contract and
refuses every non-WhatsApp native metadata with
`Native admission refused: invalid_params (metadata)`. In 0.8.0 that refusal
dropped the very message that had just interrupted the turn. `notify`
additionally no longer writes `metadata["trigger_bus_ids"]` (nothing read it).

Why the cards are not in the wake: a one-line body is trivially admissible,
cannot be mistaken for the trainer's own text, and keeps delivery proof where
it always was — `pre_llm_call` injecting the batch. Because the wake carries no
cards, the injection is the only place the card can appear; the cursor still
delivers each batch exactly once.

## 额外一条中文提示 — the plugin sends its own Chinese notice (0.8.3–0.8.5)

A wake produces two messages in Feishu: the gateway's own English
`⚡ Interrupting current task …` hint for an event (or the queued internal
turn for a notify), and one short Chinese line that this plugin sends itself.
The gateway has two separate send paths for that hint and neither could be
made to add the line, so the plugin no longer depends on gateway internals —
it uses the adapter it already calls `handle_message` on.

The line is chosen by the woken event's level (0.8.5):

```
event  → ⚡ 收到来自 <source> · <actor> 的 Event 消息
notify → 📬 收到来自 <source> · <actor> 的 Notify 消息
```

**Immediate, and never twice (0.8.4).** An event's line is sent inline, right
after `adapter.handle_message(ev)` — no sleep and no timer; the order against
the English ack is not engineered. A notify's line is sent at the delivery
proof instead (0.9.1, see below). What *is* guaranteed is that the same line
cannot appear twice: besides "one per wake", an identical `(chat, text)` sent
within **2 s** is skipped in-process.

**0.8.5 定稿语义：立即发送、不发两次、无延迟、event/notify 各一条。** event 的 ⚡
发送就发生在唤醒路径上，没有 `sleep`、没有 `call_later`、没有 delay 开关；一次唤醒
只发一条，同一 `chat + 完整文本` 在 2 秒内不重发（容量 32，超出丢最旧；发送失败会
释放占位）。**0.9.1 起 notify 的 📬 不在入队时发，只在真正注入上下文时发**（与
`DELIVERED` / 游标推进同一判定点）。两种文本不同，去重 key 也就不同 —— event 与
notify 互不干扰。`info` / `log` 不发。

```python
adapter.send_message(chat_id, "⚡ 收到来自 <source> · <actor> 的 Event 消息")
adapter.send_message(chat_id, "📬 收到来自 <source> · <actor> 的 Notify 消息")
```

- **immediate**: the send happens on the wake path itself; no background
  thread, no `call_later`, no extra env knob.
- **thread safety**: a coroutine `send` is handed to the gateway loop captured
  by `pre_gateway_dispatch` with `run_coroutine_threadsafe`. With no usable
  loop the notice is executed synchronously on the calling thread and the
  fallback is logged (`no captured gateway loop; sending synchronously`).
- **dedupe**: an identical `(chat, text)` sent within `ANNOUNCE_DEDUPE_WINDOW`
  (2.0 s) is skipped (`skipped:duplicate`). The in-process set keeps 32
  entries, oldest dropped; a failed send releases its entry so a retry is not
  blocked.
- **probed by name**: `send_message`, then `send`, then `_send_text`, then
  `_send_plain_message` — the first callable that exists wins; an adapter with
  none of them is logged (`wake notice skipped: … exposes none of …`)
  and the wake continues untouched. Feishu's adapter exposes the async
  `send(chat_id, content)`, which is scheduled on the captured gateway loop.
- **chat id** comes from `state.source.chat_id` (the source captured by
  `pre_gateway_dispatch`).
- **both injectable levels**: an `event` wake sends
  `⚡ 收到来自 … 的 Event 消息` immediately; a `notify` sends
  `📬 收到来自 … 的 Notify 消息` when it is really injected (0.9.1). `info` /
  `log` send nothing.
- **one per batch**: a merged notify batch sends one message carrying one
  `📬` line per event (max 3 lines; see 0.9.2 below); a merged event batch
  names its first event and sends one `⚡`.
  A tick holding both levels speaks twice at two honest moments — `⚡` at the
  event's wake (unchanged) and `📬` when the notify batch is really injected.
- **best-effort**: the whole extra send sits in `try/except`; any failure is
  logged and swallowed, so the wake that was already handed over still counts
  as handed over (`handle_message` was called), never as failed.

## 0.9.3 — info 静默入上下文（不唤醒）

`info` used to be worker-only: the pull never asked for it, so the agent never
saw one. 0.9.3 pulls it in the **same** `GET /events` request
(`levels=event,notify,info`, overridable with `TRIGGER_BUS_LEVELS`) and gives it
exactly one route — **silent context**:

- **never a wake**: info does not call `handle_message`, does not open a turn,
  does not enter the gateway queue, is not eligible for
  `transform_tool_result`, and produces **no visible message** (no `📡`, no
  notice, no send of any kind);
- **only into a turn that already exists**: the injection seam is
  `pre_llm_call`, so info rides along only when the trainer (or any other
  caller) was going to speak anyway. With no active turn it simply waits in
  its own bounded backlog (`INFO_QUEUE_MAX`, default 100) — the cursor stays
  where it was;
- **after the cards**: in a turn that also carries event/notify, their cards
  come first and info gets its own last section titled `— info —`;
- **at most 10 per turn** (`MAX_INFO_PER_INJECT`): 12 queued info → 10 cards
  now, the remaining 2 on the next turn. Truncation never drops a row;
- **format**: one card is the standard two-line card with `· info` in the
  level slot; several cards add `[N 条 info]` on the first line; the text is
  clipped to the same 100 characters + `…`:

  ```
  — info —
  [3 条 info]
  #42 · info mc_chat/Steve 11:44 thr=mc_chat:global
  > 矿洞修好了
  #43 · info codex 11:45
  > build 完成
  #44 · info dsh 11:46
  > 排队中
  ```

- **cursor**: moved (and ACKed) only when the block is really injected, to the
  highest injected info id — exactly the same "delivery proof" rule as
  event/notify. A wake, a queue entry or a fetch never moves it for info.

Boundary in one line: **info is read when I speak, never the reason I speak.**

## 0.9.2 — 忙时多条 notify 合并为一次唤醒

0.9.0/0.9.1 admitted **every** busy notify separately, so three notifies that
arrived during one turn became three native admissions and three turns. The
first turn's `pre_llm_call` drained the whole local queue (3 cards, cursor
0 → 80, one `📬`); the other two woke with **nothing to inject**:

```
23:23:47 notify NATIVE-QUEUED n=1 ids=78 busy=1
23:23:58 notify NATIVE-QUEUED n=1 ids=79 busy=1
23:24:08 notify NATIVE-QUEUED n=1 ids=80 busy=1
23:24:30 injected 3 event(s) into pre_llm_call context (cursor=80)   ← the first turn
23:24:40 conversation turn: msg='📬 收到来自 selftest 的 Notify 消息'  ← empty
23:24:51 conversation turn: msg='📬 收到来自 selftest 的 Notify 消息'  ← empty
```

0.9.2 keeps **one merge window per busy session** (`_BusState.notify_window`):

- the **first** notify may open the one turn — the native admission
  (`handle_message` → `admit_native`), or a plugin-side `HELD` batch when no
  session authority exists;
- **every later notify is merged into that same delivery** — journal
  `notify MERGED n=1 ids=79 into=q:78 total=2 busy=1 woke=window(no new turn)`
  — and never calls `handle_message`/`admit_native` again;
- the window closes in `_forget_delivery`, i.e. exactly when its batch is
  proven delivered (or fails and is retried);
- the one turn's `pre_llm_call` therefore injects the whole window together:
  **one** `[N 条]` card and the cursor advanced **once** to the maximum id;
- the visible notice is **one `📬` line per event** of that batch (the same
  source repeats the same line), capped at **3 lines**: X ≤ 3 shows X lines
  with no suffix; X > 3 shows 3 lines and the 3rd ends with `…x(X-2)`
  (X=5 → `…x3`, X=100 → `…x98`). The block is one send and one
  `(chat, text)` dedupe key; X=1 is one plain line;
- **no empty wake**: a `HELD` window whose cards were already injected by
  another turn is retired silently (`_drain_held` → `"empty"`) instead of
  opening a turn with nothing to inject.

Unchanged: the `event` hard interrupt (`internal=False`, immediate `⚡`, still
one line), an idle notify's immediate queue + drain, the wake texts, and the
2 s `(chat, text)` dedupe (one message per wake; the multi-line notice is
compared as a whole). Journal of the fixed timeline:

```
23:23:47 notify NATIVE-QUEUED n=1 ids=78 busy=1
23:23:58 notify MERGED n=1 ids=79 into=q:78 total=2 busy=1 woke=window(no new turn)
23:24:08 notify MERGED n=1 ids=80 into=q:78 total=3 busy=1 woke=window(no new turn)
23:24:30 injected 3 event(s) into pre_llm_call context (cursor=80)
23:24:30 wake notice handed to send: 📬 收到来自 selftest 的 Notify 消息 …(+2 lines)
```

## 0.9.1 — notify 通知在送达时发，排队不发

0.9.0 sent the visible `📬` at **enqueue** time, so a busy-time notify told the
trainer "收到" long before it arrived:

```
23:06:11 notify NATIVE-QUEUED n=1 ids=73 busy=1
23:06:12 wake notice (send) delivered      ← 📬 shown here …
23:06:57 DELIVERED mode=queue ids=73 cursor=73   ← … but it arrived here
```

0.9.1 moves the line to the **delivery proof**:

- `NATIVE-QUEUED` and `HELD` are **silent** — nothing is sent while a notify is
  queued, held or in flight.
- `_announce_on_delivery()` is called from `_confirm_injected()` /
  `_confirm_from_message()` — the exact point that logs `DELIVERED` and lets
  the cursor/ACK move — and sends exactly one
  `📬 收到来自 <source> · <actor> 的 Notify 消息`, naming the batch's **first**
  event. `_Delivery.announced` makes a twice-confirmed batch speak once.
- `event` is untouched: wake-time hard interrupt + immediate
  `⚡ 收到来自 … 的 Event 消息`.
- The 2 s `(chat, text)` dedupe and its 32-entry cap, the notify queueing
  routes, the batch capacity and the cursor rule are all unchanged.

Journal of the fixed timeline (same scenario):

```
23:06:11 notify NATIVE-QUEUED n=1 ids=73 busy=1
23:06:57 DELIVERED mode=queue ids=73 confirmed by context injection (cursor=73)
23:06:57 wake notice handed to send: 📬 收到来自 selftest 的 Notify 消息
```

## 一条 event 只走一条路 — one event, one route (0.8.2)

An `event` has two possible delivery channels: the **wake** (interrupt, then
`pre_llm_call` in the turn it starts) and the **mid-turn insert**
(`transform_tool_result`). In 0.8.1 a wake could race the tool call that was
already in flight: the wake was issued, then the *same* event was also
appended to that tool result, and the new turn's `pre_llm_call` injected the
card a second time.

0.8.2 makes the two channels mutually exclusive:

- `_BusState.woken_ids` is the wake claim. It is written **before** the
  hand-over, so a concurrent `transform_tool_result` can never slip in between
  the wake and its mark. `transform_tool_result` (`_take_interrupt`) only
  inserts events that are **not** claimed.
- The claim is released by `_forget_delivery`: on a failed hand-over, on a
  gateway refusal/raise (the lease-timeout answer), and on the TTL retry of an
  unproven delivery. That is what keeps the mid-turn insert working as the
  **fallback** — a wake that fails never swallows the event.
- The reverse direction keeps 0.8.1's at-most-once: an event already inserted
  by `transform_tool_result` is in `interrupt_done`, and the wake selector
  skips it too. A re-attached batch cannot be woken again.
- Delivery proof is unchanged: only the cards entering the model's context
  (`pre_llm_call` or the one mid-turn insert) advance the cursor and ACK.


## 排队 vs 中断 — two semantics, chosen by level alone

| level    | 忙 (a turn is running) | 闲 (nothing running) |
|----------|------------------------|----------------------|
| `event`  | **中断** — `handle_message(internal=False)`: the running turn is interrupted at once | **起一轮** — `handle_message(internal=False)` |
| `notify` | **排队** — `handle_message(internal=True)`: Hermes' own busy queue (`admit_native` FIFO) runs it after this turn really finishes; never the adapter pending slot, never an interrupt | **入队 + drain** — `_queue_or_replace_pending_event`, then the head is processed immediately (`handle_message`) |
| `info`   | **静默入上下文** — rides the running turn's `pre_llm_call` in a trailing `— info —` block; no wake, no queue, no message | **等待** — stays in its backlog, no wake, no queue, no message (0.9.3) |
| `log`    | never injected (archive-only) | never injected |

No extra field, flag or priority influences this: the level alone does.

### 排队 (notify)

**0.9.0 — a busy notify never touches the adapter's pending slot.** That slot is
the server's "start this as the next turn" slot: as soon as *any* turn ended
(including one an interrupt cut short) the adapter started a new turn from it,
and starting a turn interrupts the running one and kills its in-flight tool
process. Field log: `notify QUEUED 22:35:32` (busy=1) → `DELIVERED 22:35:57`
while `sleep 50` (started 22:35:16) was still running — it died at 22:35:56.

While a turn runs the plugin now calls what the idle path calls anyway:

```python
adapter.handle_message(event)        # event.internal = True, body = 📬 …
```

With `runner.session_authority` present (the predicate
`BasePlatformAdapter._shared_runtime_owns_input` uses) this is Hermes' **own
busy queue** — the same one the WebUI uses:

```
gateway/platforms/base.py:4017         _handle_message_while_active
gateway/session_ingress_burst.py:102   dispatch_busy_burst
gateway/session_ingress.py:497,39      dispatch_shared_busy -> admit_message
gateway/session_authority.py:522       SessionAuthority.admit_native
gateway/session_authority.py:1309      SessionAuthority._drain  (claims the head only while no turn executes)
```

"Turn finished" is therefore Hermes' own notion, not a plugin guess, and
`busy_input_mode: interrupt` is never consulted for these internal events.
Without an authority the plugin holds the batch in its own queue (`notify HELD
… phase=held reason=turn_still_running`) and `_drain_idle` wakes it only once
`_busy()` is False.

Idle notify is unchanged — queued into the gateway's per-session queue:

```python
gateway._queue_or_replace_pending_event(session_key, event)   # internal=True
```

- the gateway drains that queue itself at the end of the running turn — this
  plugin runs **no timer and no self-made wake** for an idle notify;
- the routing key comes from the session source captured by
  `pre_gateway_dispatch` (`gateway._session_key_for_source(source)`, else
  `gateway.session.build_session_key(source)`), e.g.
  `agent:main:feishu:dm:oc_9add9b05d0d7bddb6ecc3ebda8294859`; without any
  source it degrades to the `session_id` reported by `pre_llm_call`, and the
  log always names which one was used
  (`session key for the gateway pending queue: … (from …)`);
- idle enqueue: today's gateway exposes no "drain the head now" entry, so the
  plugin probes a candidate list with `getattr`
  (`_drain_pending_queue`, `_hm_drain_pending_queue`, `_process_pending_queue`,
  `_drain_queued_events`, `_drain_pending_messages`) and only calls one when it
  exists. Otherwise it pops the head with `adapter.get_pending_message` — only
  when that head *is* our event — and hands the same event to
  `adapter.handle_message`. The strategy is logged:
  `drain: no gateway drain entry (…); used adapter.get_pending_message + handle_message for session_key=…`.
- visible notice (0.8.5): a successful queue call also sends one
  `📬 收到来自 <source> · <actor> 的 Notify 消息` to the source chat through
  the adapter — immediate, one per wake, deduped on `(chat, text)`; without it
  the `internal=True` turn is invisible in Feishu.

### 中断 (event)

Unchanged from 0.7.0: a real `MessageEvent(internal=False)` through
`adapter.handle_message`. Busy ⇒ the gateway interrupts/steers the live turn;
idle ⇒ a new turn starts. The one-line body names the sender
(`⚡ 来自 mc_chat · Steve 的打断`); the cards wait in the local queue and that
turn's `pre_llm_call` injects them.

## 送达判据 — delivery is proven, never assumed

A hand-over is **not** a delivery. Nothing advances the cursor and nothing is
ACKed when a wake is queued or handed to `handle_message`:

1. the cards stay in the local queue and the batch is recorded as *in flight*
   (its `event_id`s enter a de-duplication set, so the same batch is not woken
   twice while it waits);
2. the batch counts as **delivered only when the cards are injected into the
   model's context** — `pre_llm_call` handing them over as this turn's
   `context`, or `transform_tool_result` inserting one mid-turn. Only then:
   cursor → max(delivered ids), `POST /ack`, ledger entry removed. Log:
   `DELIVERED mode=… confirmed by context injection (ids=… cursor=…)`;
3. queued, in-flight, rejected and timed-out batches never move the cursor.

At-least-once every other way:

| situation | what happens |
|---|---|
| gateway refuses the queue call (no adapter, queue cap) | `_gateway_accepted` stays false → event stays in the local queue, WARN, retried next tick |
| no gateway / no adapter captured yet | event stays queued, WARN, retried on the next poll tick |
| idle drain raises or `handle_message` returns a rejection string (lease timeout: "another turn is running") | the wake's marks are dropped, WARN `delivery FAILED (…)`, cards stay queued, retried next tick |
| nothing proves delivery within `TRIGGER_BUS_BATCH_TTL` (120 s) | the marks are cleared (cards never left the queue), next poll wakes again |
| a notify still parked in the gateway queue at TTL | not a failure: its clock is refreshed, no duplicate |

Deduplication: an `event_id` already queued, woken or in flight is never sent
again (re-polls, replayed batches and retry ticks cannot duplicate it).

## Injection format

The `pre_llm_call` context and the mid-turn `transform_tool_result` insert
render the same compact two-line card. The wake message is one line and carries
no card (see above).

One event (no summary line):

```
#52 event mc_chat/Steve 20:01 thr=mc_chat:global
> 溪流 快来
```

A batch adds `[N 条]`, expands at most five cards, then counts the rest on one
line:

```
[3 条]
#52 event mc_chat/Steve 20:01 thr=mc_chat:global
> 溪流 快来
#53 notify email/HR 19:58 thr=email:a1b2
> 周报：本周进度…
#54 notify mc_chat/Alex 20:00 thr=mc_chat:global
> 这矿真难挖
```

Rules:

- header order is fixed: `#id level source/actor HH:MM`; the header ends with
  `thr=<thread_key>` only when the event carries a thread key (`thread_key`,
  else `topic`, else the same keys inside `payload`);
- the original text is a separate line starting with `> `; all whitespace and
  newlines collapse to single spaces, and the text is clipped to **100
  characters** plus `…` (code-point boundary, so a Chinese character or emoji
  is never cut in half);
- more than five events: five cards and `+K more` (e.g. `+3 more`);
- mid-turn insertion puts `⚡ 立刻处理：` on its own line in front of the card
  block;
- no markdown tables — the message is also shown in Feishu, which mangles them.

`event` keeps its mid-turn channel: `transform_tool_result` appends the card
(prefixed `⚡ 立刻处理：`) to a tool result when an event is queued, not yet
injected **and not already claimed by a wake** (0.8.2 `woken_ids`), at most
once per event. `notify` is never eligible there — it waits in the queue.

Since 0.9.3 `info` is injected by `pre_llm_call` only, as one extra section
**after** the event/notify cards. It never wakes, never queues and sends
nothing; at most 10 cards ride one turn and the rest stay queued:

```
[2 条]
#20 event mc_chat/Steve 11:41
> 快来
#21 notify email/HR 11:42
> 周报

— info —
[3 条 info]
#22 · info codex 11:43
> build 完成
#23 · info dsh 11:43
> 队列 2
#24 · info mc_bridge 11:44
> 已连接
```

A single info card has no `[N 条 info]` line; its text obeys the same 100-char
clip and its `thr=` suffix is identical. `— info —` appears on its own line and
the block is always last — event/notify order is never changed by info.

## Busy flag

`pre_llm_call` sets it, `post_llm_call` clears it; when the gateway exposes
`_is_session_running`, that wins. A turn from a background platform
(`subagent`, `cron`, `internal`, `worker`, `batch`) never touches it: it is not
the trainer's turn. `TRIGGER_BUS_TURN_ACTIVE_TTL` (1800 s) guards a stale flag.

## Hooks

`pre_llm_call` (injection + delivery proof), `post_llm_call` (clears busy, one
dispatch for work that arrived after the last tool batch),
`transform_tool_result` (mid-turn event), `pre_gateway_dispatch` (captures the
live gateway + session source; without it only injection works, never a wake).

## Environment

| variable | default | meaning |
|---|---|---|
| `TRIGGER_BUS_URL` | `http://127.0.0.1:8791` | service base URL |
| `TRIGGER_BUS_SUBSCRIBER` | `hermes-main` | subscription name |
| `TRIGGER_BUS_CURSOR` | `~/.hermes/trigger-cursor.json` | persisted cursor |
| `TRIGGER_BUS_POLL_INTERVAL` | `2` | poll seconds |
| `TRIGGER_BUS_BATCH_TTL` | `120` | seconds without delivery proof before a batch is retried |
| `TRIGGER_BUS_POST_TURN_DELAY` | `1` | settle delay before the post-turn dispatch (s) |
| `TRIGGER_BUS_TURN_ACTIVE_TTL` | `1800` | stale busy-flag guard (s) |
| `TRIGGER_BUS_NON_TURN_PLATFORMS` | `subagent,cron,internal,worker,batch` | platforms whose turns never count as busy |
| `TRIGGER_BUS_LEVELS` | `event,notify,info` | levels the pull asks for (info is silent context, never a wake) |
| `TRIGGER_BUS_DISABLED` | — | `1` makes `register()` a no-op |
| `TRIGGER_TOKEN_PATH` | `~/.hermes/secrets/trigger.token` | `X-Trigger-Token` |

`TRIGGER_BUS_NOTIFY_WAKE_DELAY` is still accepted as an alias for
`TRIGGER_BUS_POST_TURN_DELAY`; the 0.7 notify-wake interval knob is gone
(notify no longer wakes on its own).

## Tests

```bash
# from this directory, with the gateway's interpreter
/Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 selftest_format_070.py
/Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 selftest_queue_interrupt_080.py
/Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 selftest_wake_text_081.py
/Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 selftest_dedupe_082.py
/Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 selftest_announce_083.py
/Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 selftest_notify_notice_085.py
/Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 selftest_notify_delivery_091.py
/Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 selftest_notify_merge_092.py
/Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 selftest_info_inject_093.py

# 0.9.0: the REAL gateway classes (BasePlatformAdapter + the FIFO branch), no gateway
/Users/kibyou/.hermes/tools/python-3.14.9*/bin/python3 probe_native_queue_entry.py
```

Function-level only: no gateway process, no network, no service.

- `selftest_format_070.py` — the card renderer (1 / 3 / 8 events, field
  completeness, `[N 条]`, `+K more`, 100-character clipping, newline collapse,
  the `⚡ 立刻处理：` mid-turn insert, the one-line wake text).
- `selftest_queue_interrupt_080.py` — the queue/interrupt contract. 0.9.0
  cases: busy+notify ⇒ **held** (`phase=held`, 0 queue calls, no
  `handle_message`, no slot write, no DELIVERED); repeat ticks while the turn
  runs still deliver nothing; the turn ending wakes exactly one
  `handle_message(internal=True)`; busy+notify **with a session authority** ⇒ the
  native route (`handle_message` → FIFO, delivery marked `native`, slot empty,
  never re-handed); the reported scenario (`sleep 50` in flight, then the tool
  returns) in case q. Unchanged 0.8.0 items: idle+notify ⇒ queued then drained;
  busy+event ⇒ `handle_message(internal=False)`; cursor untouched while queued
  and advanced only by the injection; the same `event_id` twice ⇒ one enqueue;
  no gateway / no adapter ⇒ stays queued with a WARN; a raising drain ⇒ event
  stays queued and is retried; plus the real
  `gateway.run_busy.GatewayBusySessionMixin._queue_or_replace_pending_event`
  driven through a stub to prove the acceptance receipt.
- `selftest_wake_text_081.py` — the 0.8.1 contract (notify literal updated to
  the 0.8.2 wording): the event wake is exactly
  `⚡ 来自 selftest · Tester 的打断` and the notify wake exactly
  `📬 收到来自 email · HR 的 Notify 消息` (one line, no `#id`, no `> ` body);
  both `MessageEvent.metadata` are empty; the real
  `gateway.session_envelope._event_clause` accepts that event and still refuses
  the 0.8.0 `interrupt_origin` metadata with `metadata`; after the wake,
  `pre_llm_call` injects the same batch exactly once in the unchanged card
  format, a second call injects nothing, and three polls produce one wake.
- `selftest_dedupe_082.py` — the 0.8.2 one-route contract: an event that
  triggered a wake is **not** inserted by the next five
  `transform_tool_result` calls and is injected once by the wake turn's
  `pre_llm_call`; an event already inserted mid-turn is not woken (even when
  re-attached); every wake failure releases the claim so the mid-turn insert
  still fires as the fallback — a raising hand-over, a synchronous gateway
  refusal (`⏳ another turn is running…`) and the TTL retry of an unproven
  wake; the event wake line stays byte-for-byte
  `⚡ 来自 <source> · <actor> 的打断` and the notify line is exactly
  `📬 收到来自 email · HR 的 Notify 消息`.
- `selftest_announce_083.py` — the extra notice with a FakeAdapter that
  records `send_message` calls: an event wake sends **exactly once** with the
  text `⚡ 收到来自 selftest · Tester 的 Event 消息` to `oc_selftest`; a notify
  sends **nothing** at enqueue (0.9.1) and its one `📬` line when the injection
  proves delivery; a merged batch names its first event; an adapter without
  a send method is logged and skipped; a raising `send_message` and a returned
  `SendResult(success=False)` both leave `handle_message` called and the wake
  successful, with the refusal reported as `failed:…`; an async `send` is
  scheduled on the captured loop; the 2 s `(chat, text)` guard, its 32-entry
  cap (oldest dropped) and the async-failure release are exercised with a fake
  clock; `plugin.yaml`/README say 0.9.1.
- `probe_native_queue_entry.py` — not a selftest: it imports the real
  `gateway.platforms.base.BasePlatformAdapter` and proves the 0.9.0 routing.
  With `runner.session_authority`, `_handle_message_while_active` sends a busy
  internal event to `dispatch_busy_burst` (durable FIFO) and never to the
  busy-input handler; without it, the busy handler runs. Then the plugin is
  driven on that same real adapter: the busy notify reaches the
  `handle_message`/`admit_message` seam with `internal=True`,
  `adapter._pending_messages` stays empty, the delivery is `native`, the cursor
  stays 0 (nothing is DELIVERED before the FIFO runs it).
- `selftest_notify_notice_085.py` — the 0.8.5 notice suite, rewritten for the
  0.9.1 timing: a busy notify is **silent** at `NATIVE-QUEUED` (zero sends,
  cursor untouched, card still queued), an idle notify is silent at dispatch,
  and in both cases the one `📬 收到来自 email · HR 的 Notify 消息` is sent by
  the `pre_llm_call` injection that proves delivery (a second call adds
  nothing); a merged notify batch names its first event and still sends one
  line; an event+notify tick keeps the event's immediate ⚡ (both orders) and
  adds the notify's 📬 only at its injection; the `(chat, text)` keys of the
  two texts are independent inside the 2 s window; a raising send neither
  breaks the hand-over nor the delivery proof; `info` / `log` send nothing; an
  idle **event** wake still takes the interrupt path (`internal=False`, no
  queue call), sends one ⚡ line, and the cursor moves exactly once when
  `pre_llm_call` proves the injection (a second call injects nothing).
- `selftest_notify_delivery_091.py` — the 0.9.1 timeline regression (the field
  log above): busy notify enqueued ⇒ zero send calls and no `wake notice` line,
  three more ticks while the turn runs still send nothing, then the injection
  sends exactly one 📬 **after** the `DELIVERED` record and moves the cursor in
  the same step; the HELD route is silent through enqueue, repeated ticks and
  the turn-end drain; a batch confirmed twice (and `_announce_on_delivery`
  called twice) still speaks once; a 3-notify batch sends one 📬 line per
  event (0.9.2); an idle notify and an event are pinned (⚡ immediate, no 📬 at
  injection); the 2 s dedupe still trims deliveries; `plugin.yaml` / README are
  0.9.2 and state `notify 通知在送达时发，排队不发`.
- `selftest_notify_merge_092.py` — the 0.9.2 busy-window regression (the field
  log above): three notifies arriving during one turn call
  `handle_message`/`admit_native` **once** (two `notify MERGED` lines), stay
  silent and leave the cursor at 0; the one turn's `pre_llm_call` injects all
  three as ONE `[3 条]` card and advances the cursor 0 → 3 in one step; the
  one notice message carries one `📬` line per event (X=4 → 3 lines ending
  `…x2`, X=100 → 3 lines ending `…x98`, X=1 → one plain line) and the whole
  block is one dedupe key; no further wake/admit happens after the injection
  (`len(handled) == 1`, one `NATIVE-QUEUED`). The HELD route merges the same
  way (one wake, three cards), a window already injected elsewhere is retired
  without an empty wake, an idle notify still drains immediately and a busy
  `event` still interrupts at once (`internal=False`, immediate ⚡).


## 0.9.4 聊天图片附件

Worker 先以原始字节上传到 router 的鉴权 POST /attachments，再在 /notice 的
attachments 中引用上传回执。事件只带附件 ID、文件名、MIME、大小和 SHA256，
不带发送端本地路径。接收端从自己配置的 router 下载，校验长度和 SHA256 后，
以 PHOTO + media_urls/media_types 进入 Hermes 原生消息；缓存文件属于接收端。
文字上下文也列出接收端附件，供图片工具读取；下载失败不会退回发送端路径，
不会推进本批事件游标，下一次轮询重试。

跨机器部署：Worker 使用原有 router.url / CODEXWORKER_ROUTER_URL；
Hermes 设置 TRIGGER_BUS_ROUTER_URL，凭据由 ROUTER_TOKEN_PATH 指向本机 router token。
router 仍保持 loopback 监听，经已有认证隧道/入口访问，不为传图额外开放公网端口。
普通消息格式与 notify/event 的语义保持不变，默认图片通知为 notify。
目前支持 PNG/JPEG/GIF/WebP，每张最多 100MB，每条最多 10 张；原始字节不转码。
部署需加载新版 router 与 trigger-bus 插件，不需要重启 Codex 共享后台或 DSH。
