# mowu-trigger 自测报告

> **归档说明（2026-11-08）**：本文件是 **0.2.0**（服务端内置 `sources/` 适配器）
> 的自测记录，保留作为历史证据。0.3.0 起架构改为「纯收件箱 + 客户端过滤墙」，
> 服务端不再有来源，第 11 项（`sources/mc_chat.py` SSE 解析）已不适用。
> 当前版本（0.7.0）的自测见 README 第 8.6 节：`selftest_rules.py`（L2）、
> `selftest_levels.py`（四级投递 + 回合中途注入）、`clients/selftest_l1.py`（L1）、
> `selftest_sources.sh`、`clients/selftest_lifecycle.py`。
> 对应的旧脚本 `selftest_sources.py` 已移到 `attic/`。

- 日期：2026-11-08（Asia/Shanghai）
- 解释器：`/usr/bin/python3` 3.9.6（macOS 自带；服务与插件均只依赖标准库）
- 服务版本：`0.2.0`；插件版本：`0.1.0`
- 结论：**全部真实执行并通过**。下面每一段都是实际命令输出，最后附清理情况。

| # | 项目 | 结果 |
|---|---|---|
| 1 | 服务启动 / `/health` | PASS |
| 2 | `POST /trigger` + 自定义 `event_id` 去重 | PASS（重复返回 `duplicate:true`、同一 id） |
| 3 | `GET /events` 顺序 / 不自动推进游标 | PASS（同 `since` 重复返回同样 2 条） |
| 4 | `POST /ack` + `/stats` | PASS（ack 后 unread 逐条降到 0） |
| 5 | 长轮询空转 | PASS（`wait=5` → **5.036 s**） |
| 6 | 长轮询被新事件唤醒 | PASS（1 s 后投递 → **1.063 s** 返回，实际唤醒约 60 ms） |
| 7 | 插件导入 / 假 ctx / hook 注入 | PASS（context 含 4 行 `[TRIGGER ...]`） |
| 8 | hook 后 ACK / 游标持久化 | PASS（`/stats` unread=0、cursor 文件写入 id=4） |
| 9 | `TRIGGER_BUS_DISABLED=1` | PASS（hook 返回 None） |
| 10 | 插件在服务不可用时静默降级 | PASS（hook 返回 None，日志 1 条 WARNING） |
| 11 | mc_chat SSE 帧解析 | PASS（假 SSE 服务，2 帧 → 2 条事件） |
| 12 | email 缺凭据 / dry-run | PASS（disabled / ok，均不抛异常） |
| 13 | 8765 不可用时服务不受影响 | PASS（`/health` sources.mc_chat=error，`/trigger` 正常） |
| 14 | 8765 真实 MC bridge 连接 | PASS（`mc_chat: connected`） |
| 15 | `--once` 单请求模式 | PASS（返回后端口关闭） |

---

## 1. 启动与 /health

```bash
cd /Users/kibyou/projects/mowu-trigger
/usr/bin/python3 server.py --port 8791 --db /Users/kibyou/.hermes/trigger.db --no-mc
```

（自测时加 `--no-mc` 避免真实 MC 聊天干扰断言；正常启动不加。）

```bash
$ curl -sS http://127.0.0.1:8791/health
{"ok": true, "version": "0.2.0", "events": 0, "port": 8791,
 "sources": {"mc_chat": "disabled", "email": "disabled"}}
```

日志：

```
2026-11-08 00:41:28,236 INFO    trigger: store ready: /Users/kibyou/.hermes/trigger.db
2026-11-08 00:41:28,246 INFO    trigger: mowu-trigger 0.2.0 listening on http://127.0.0.1:8791 ...
2026-11-08 00:41:28,247 INFO    trigger.mc_chat: source thread started
2026-11-08 00:41:28,248 INFO    trigger.email: source disabled by config
```

监听确认（只绑回环）：

```
$ lsof -nP -iTCP:8791 -sTCP:LISTEN
Python  55368 kibyou  8u  IPv4 ... TCP 127.0.0.1:8791 (LISTEN)
```

## 2. 投递、去重、读取、ack

脚本：`./selftest_http.sh`（完整输出见 `selftest_http.out`）。

```bash
$ curl -sS -X POST http://127.0.0.1:8791/trigger -H 'Content-Type: application/json' \
    -d '{"source":"mc_chat","kind":"chat","actor":"Steve","text":"hello from HTTP test"}'
{"ok": true, "id": 1, "event_id": "b47fe99c...c55f", "duplicate": false}

$ curl -sS -X POST http://127.0.0.1:8791/trigger -H 'Content-Type: application/json' \
    -d '{"source":"email","kind":"mail","priority":2,"actor":"alice@example.com",
         "text":"Subject: build finished","event_id":"test-fixed-id-1794069924"}'
{"ok": true, "id": 2, "event_id": "test-fixed-id-1794069924", "duplicate": false}

# 同一 event_id 再投一次
{"ok": true, "id": 2, "event_id": "test-fixed-id-1794069924", "duplicate": true}
```

读取（`since=0`，按 id 升序，只返回未读）：

```json
[
  {"id": 1, "source": "mc_chat", "kind": "chat", "actor": "Steve",
   "text": "hello from HTTP test", "ts": "2026-11-08T00:45:24+08:00", ...},
  {"id": 2, "source": "email", "kind": "mail", "priority": 2,
   "actor": "alice@example.com", "text": "Subject: build finished", ...}
]
```

同一 `since` 再读一次——**游标不会自动前进**：

```
count=2 ids=[1, 2]
```

逐条 ack：

```bash
$ curl -sS -X POST http://127.0.0.1:8791/ack -H 'Content-Type: application/json' \
    -d '{"subscriber":"hermes-selftest","up_to_id":1}'
{"ok": true, "subscriber": "hermes-selftest", "last_id": 1, "acked": 1}

$ curl -sS 'http://127.0.0.1:8791/events?subscriber=hermes-selftest&since=0&limit=50'
count=1 ids=[2] texts=['Subject: build finished']

$ curl -sS -X POST http://127.0.0.1:8791/ack -H 'Content-Type: application/json' \
    -d '{"subscriber":"hermes-selftest","up_to_id":2}'
{"ok": true, "subscriber": "hermes-selftest", "last_id": 2, "acked": 1}

$ curl -sS 'http://127.0.0.1:8791/events?subscriber=hermes-selftest&since=0&limit=50'
[]

$ curl -sS 'http://127.0.0.1:8791/stats?subscriber=hermes-selftest'
{"unread": 0, "last_id": 2}
```

另测 `since` 省略时使用已存游标：返回 `count=2 ids=[4, 5]`（当时库里 4、5 未读），
说明省略 `since` 走的是 `cursors.last_id`。

## 3. 长轮询（实测数字）

空转（`since` 已在最新，`wait=5`）：

```bash
$ curl -sS -o /tmp/mowu-lp-empty.json -w '%{time_total}' \
    'http://127.0.0.1:8791/events?subscriber=hermes-selftest&since=3&wait=5&limit=50'
time_total=5.036258s body=[]
```

并发唤醒：先挂起 `wait=15`，1 秒后 `POST /trigger`：

```bash
$ curl ... '.../events?...&since=2&wait=15' -w '%{time_total}' > /tmp/mowu-lp-wake.time &
$ sleep 1; curl -sS -X POST .../trigger -d '{"source":"mc_chat",...,"text":"wake up long poll"}'
{"ok": true, "id": 3, ...}

$ cat /tmp/mowu-lp-wake.time
1.062554
$ cat /tmp/mowu-lp-wake.json
[{"id": 3, "source": "mc_chat", "kind": "chat", "actor": "Alex",
  "text": "wake up long poll", "ts": "2026-11-08T00:45:31+08:00", ...}]
```

- 空转：**5.036 s**（请求 5 s 超时）
- 唤醒：**1.063 s**（含脚本 `sleep 1`，服务端实际唤醒约 **60 ms**）

## 4. 插件导入与 hook

命令与输出（完整见 `selftest_plugin.out`）：

```bash
$ /usr/bin/python3 selftest_plugin.py
import OK: module=trigger-bus version=0.1.0 file=/Users/kibyou/.hermes/plugins/trigger-bus/__init__.py
POST /trigger -> {"ok": true, "id": 4, "event_id": "73273a89...bd5d", "duplicate": false}
register_hook('pre_llm_call') captured
hook returned keys: ['context']
hook context:
[TRIGGER-BUS] 4 external event(s) since the last turn:
[TRIGGER mc_chat/chat 2026-11-08T00:45:24+08:00] hello from HTTP test
[TRIGGER email/mail 2026-11-08T00:45:24+08:00] Subject: build finished
[TRIGGER mc_chat/chat 2026-11-08T00:45:31+08:00] wake up long poll
[TRIGGER mc_chat/chat 2026-11-08T00:45:32+08:00] plugin hook test 65875b
second hook call (queue should be empty): None
GET /stats after hook -> {"unread": 0, "last_id": 4}
cursor file -> {"last_id": 4, "subscriber": "hermes-plugin-test-a869119c"}
hook with TRIGGER_BUS_DISABLED=1 -> None

RESULT: PASS  (import, register, hook context, dedupe/clear, ACK, cursor persistence, disabled switch)
```

关键点：

- 导入方式：`sys.path` 指向 `~/.hermes/plugins`，`importlib.import_module("trigger-bus")` 成功。
- fake ctx 只实现 `register_hook`，确认插件没有碰任何 gateway 私有对象。
- 返回契约是 `{"context": "..."}`，与 `live-time` 插件一致。
- 第二次调用返回 `None`（队列已清空，不会重复注入）。
- hook 返回后 `/stats` 的 `unread` 变 0 → 异步 `POST /ack` 真的发出去了。
- 游标文件真实落盘，内容为 `{"last_id":4,...}`。

另外跑了一次「服务不可达」：

```bash
$ /usr/bin/python3 selftest_plugin_offline.py
hook result with server down: None
RESULT: PASS  (offline: register + poll failed silently, hook returned None)
```

插件日志（`~/.hermes/plugins/trigger-bus/trigger-bus.log`）：

```
2026-11-08 00:44:31,570 WARNING trigger-server unreachable (<urlopen error [Errno 61] Connection refused>); degrading silently
2026-11-08 00:44:33,647 DEBUG   poll #2 failed: <urlopen error [Errno 61] Connection refused>
```

只记 1 条 WARNING，后续降 DEBUG，不刷屏、不抛异常。

## 5. 适配器

`selftest_sources.py` 自带一个 8799 的假 SSE 服务并直接驱动适配器：

```bash
$ /usr/bin/python3 selftest_sources.py
INFO selftest.mc_chat: connected to http://127.0.0.1:8799/stream
mc emitted 2 event(s):
   {"source":"mc_chat","kind":"chat","priority":4,"actor":"Notch",
    "text":"hello from sse","payload":{...},"event_id":"mc_chat:sse:42"}
   {"source":"mc_chat","kind":"chat","priority":3,"actor":"",
    "text":"plain string frame","payload":"plain string frame","event_id":"mc_chat:sse:43"}
PASS mc_chat: SSE frames parsed, mapped, deduped by SSE id
email (missing secrets) health: disabled
PASS email: missing secrets -> disabled, no exception
email dry-run health: ok
PASS email: dry-run loop runs without network
```

- SSE 的 `id: 42` 进入 `event_id = mc_chat:sse:42`，重连重放也不会重复入库。
- 邮箱凭据文件不存在：只记 INFO、状态 `disabled`、线程正常结束，进程不退出。
- `dry_run` 模式：不建连接也能跑循环并报 `ok`。

## 6. 8765 不可用（关键验收）

用一份测试配置把 mc_chat 指向死端口 8799、email 打开但无凭据，起在 8793：

```bash
$ curl -sS http://127.0.0.1:8793/health
{"ok": true, "version": "0.2.0", "events": 0, "port": 8793,
 "sources": {"mc_chat": "error", "email": "disabled"}}

$ curl -sS -X POST http://127.0.0.1:8793/trigger -H 'Content-Type: application/json' \
    -d '{"source":"probe","kind":"manual","text":"trigger works while mc upstream is down"}'
{"ok": true, "id": 1, "event_id": "dead-mc-probe-1", "duplicate": false}

# 6 秒后再查（已重连至少一次）
{"ok": true, "version": "0.2.0", "events": 1, "port": 8793,
 "sources": {"mc_chat": "error", "email": "disabled"}}
```

日志里 12 秒内只有 **1 条** WARNING：

```
2026-11-08 00:42:33,712 WARNING trigger.mc_chat: mc_chat unavailable (URLError: <urlopen error [Errno 61] Connection refused>); retrying every 5s
```

结论：MC 上游挂掉不影响启动、`/health`、`/trigger`，也不会刷日志。

## 7. 8765 真实 MC bridge

自测期间 `127.0.0.1:8765` 实际有一个 Java SSE bridge 在监听（`lsof` 显示 PID 49757），
用默认 `sources.json` 起在 8794：

```bash
$ curl -sS http://127.0.0.1:8794/health
{"ok": true, "version": "0.2.0", "events": 0, "port": 8794,
 "sources": {"mc_chat": "connected", "email": "disabled"}}

2026-11-08 00:44:43,967 INFO    trigger.mc_chat: connected to http://127.0.0.1:8765/stream
```

说明适配器对真实 bridge 的握手/SSE 读取可用（本轮没有玩家发言，因此事件数为 0）。

## 8. `--once`

```bash
$ /usr/bin/python3 server.py --port 8796 --db ... --no-mc --no-email --once &
$ curl -sS http://127.0.0.1:8796/health
{"ok": true, "version": "0.2.0", "events": 0, "port": 8796,
 "sources": {"mc_chat": "disabled", "email": "disabled"}}
$ lsof -nP -iTCP:8796 -sTCP:LISTEN
8796 closed (expected)
```

## 9. 数据与日志

```
$ sqlite3 ~/.hermes/trigger.db ".tables"
cursors     events     receipts     rules

$ sqlite3 ~/.hermes/trigger.db "select id,source,kind,actor,text from events;"
1|mc_chat|chat|Steve|hello from HTTP test
2|email|mail|alice@example.com|Subject: build finished
3|mc_chat|chat|Alex|wake up long poll
4|mc_chat|chat|TestBot|plugin hook test 65875b

$ ls -l trigger-server.log
11281  trigger-server.log        # RotatingFileHandler maxBytes=5MB, backupCount=3
```

证据采集完成后已清空测试行（表结构保留，服务启动时自动建表）：
`events / cursors / receipts` 均为 0 行，避免真实使用时把自测事件当成积压注入。

## 10. 环境发现（重要）

本机设置了 `HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:10808` 且没有 `NO_PROXY`。
`urllib` 因此把发往 127.0.0.1 的请求也交给代理：测试中一个关闭的端口返回了
`HTTP Error 502: Bad Gateway`，而不是连接拒绝。已在**服务端 mc_chat 与插件**里改成
`build_opener(ProxyHandler({}))` 强制直连，修复后离线测试正确显示
`[Errno 61] Connection refused`，真实 bridge 也正常连接。

## 11. 未启用 / 未改动

- `~/.hermes/config.yaml` 未修改，`plugins.enabled` 中没有 `trigger-bus`（grep 为空）。
- Hermes gateway 未重启、未触碰。
- `/Users/kibyou/minecraft/tfg-bot/mod/` 未触碰。
- 插件只是写好放在 `~/.hermes/plugins/trigger-bus/`，等确认后再启用。

## 12. 遗留问题

1. **没有主动唤醒**：事件只在下一轮 `pre_llm_call` 注入；用户不发消息就不会被推送。
2. 队列上限 50，突发超过 50 条会丢最旧，并把游标推进到已见最大 id（不补投）。
3. `rules` 表仍是占位，没有规则引擎。
4. 无鉴权/无 TLS，仅靠回环绑定。
5. 首次运行游标为 0 时会拉取历史未读（最多 50/次），需要的话手动改
   `~/.hermes/trigger-cursor.json`。
6. 邮件只取主题/头部，不取正文；附件不解析。
7. SSE 重连不带 `Last-Event-ID` 头，只靠 `id:` 去重。
