# mowu-trigger

本地「外部事件收件箱」：把 Minecraft 聊天、邮件等**非飞书**来源的事件统一收进一个
常驻服务，再由 Hermes 插件 `trigger-bus` 在下一轮对话时注入当前会话。

- 默认只监听 `127.0.0.1:8791`；要跨机器用 `--host <本机局域网 IP>` 显式打开。
- 飞书私聊**不经过**本服务，继续走 Hermes 原生通道。
- 纯 Python 3 标准库（`http.server` + `sqlite3` + `threading`），无第三方依赖。
- 数据库固定 `~/.hermes/trigger.db`；日志 `trigger-server.log`（5 MB × 3 滚动）。

---

## 1. 架构：纯收件箱 + L1 客户端业务过滤 + L2 规则引擎 + 两级母线

服务端**只做四件事**：接收 → L2 规则判定 → 落盘 → 应答。它不认识来源协议，
不认识 IMAP，没有适配器；`subscriptions` 表只记每个 worker 的投递预算
（第 7 节），不是来源配置。每个来源自己带一个客户端进程。

过滤按 0.6.0 的分工拆成两层（详见第 8 节）：**L1 在客户端**，只管这个源特有的
业务语义（例：MC 不把自己说的话送回来）；**L2 在服务端**，限流、去重窗口、
优先级降级、静默期这类跨源通用规则只配置一次，全局生效。

每条事件还带一个 `level`（高到低）：`event` 立刻打断、`notify` 本轮结束后
处理、`info` 只进 worker 订阅流、`log` 只落库存档。

```
 ┌──────────────── 来源侧（每个来源一个独立进程） ────────────────┐
 │                                                              │
 │  MC bridge :8765/stream ──SSE──▶ mc_chat_client.py ─┐        │
 │                                        │            │        │
 │                              L1 business_filter     │        │
 │                              （self_message：       │        │
 │                                自己发的不回送）      │        │
 │  IMAP server ──IMAP──▶ email_client.py ─────────────┤        │
 │                                        │            │        │
 │                              L1 business_filter     │        │
 │                              （folder_not_watched）  │        │
 │                                        ├─▶ 本地母线 :8792/:8793 (localbus.py)
 │                                        │      └─ worker 同机直连，绕开 8791
 └────────────────────────────────────────┼────────────┼────────┘
                                          │            │
                                   POST /trigger       │ L1 跳过的写
                                          │            ▼
                                          │      clients/drops.log
                                          │      （reason=self_message/…）
                                          ▼
                        ┌───────────────────────────────────────┐
                        │  server.py — 纯收件箱 + L2 规则       │
                        │  127.0.0.1:8791（可 --host）          │
                        │  ~/.hermes/trigger-rules.json（热重载）│
                        │  校验 → L2 → 落盘 SQLite → 回 200      │
                        │  event_id 唯一 → duplicate            │
                        │  /blocked 审计 · /subscribe 远端母线   │
                        └───────────────┬───────────────────────┘
                                        │ GET /events (long-poll) / SSE
                                        ▼
                             trigger-bus 插件 / 远端 worker
```

三条硬规则：

1. **服务端不懂来源协议。** 想加来源 = 写一个新客户端，`server.py` 一行不改
   （只有跨源规则需要改 `trigger-rules.json`，也不用改代码）。
2. **过滤分层。** L1 只做来源特有的业务判断与信封结构校验；L2 做所有跨源通用
   规则。客户端不再读规则 JSON，服务端也不认识“自己发的消息”这种概念。
3. **at-least-once + 幂等。** 客户端超时重试时复用同一个 `event_id`，
   服务端靠 UNIQUE 约束把它变成 `duplicate:true`，不会重复入库。

同一条事件有两个出口，用一个 `event_id`：

| 出口 | 地址 | 给谁 | 持久性 |
|---|---|---|---|
| **远端母线** | `http://127.0.0.1:8791/subscribe` | 别的机器上的 worker | 落 SQLite，重启不丢 |
| 轮询接口 | `GET /events?subscriber=…&since=…` | 不方便用 SSE 的 worker | 同上 |
| **本地母线** | `http://127.0.0.1:8792/subscribe`（MC）/ `:8793`（邮件） | 同一台机器上的 worker | 内存环形缓冲 500 条，**重启即丢** |

> 本地母线只是快路径：进程重启缓冲就没了。要历史就回退到 8791 的
> `GET /events`（SQLite 全量）。

---

## 2. 启动与停止

```bash
cd /Users/kibyou/projects/mowu-trigger

# 前台（调试）
/usr/bin/python3 server.py --port 8791 --db ~/.hermes/trigger.db

# LaunchAgent（已安装，KeepAlive）
launchctl unload ~/Library/LaunchAgents/ai.hermes.trigger.plist
launchctl load   ~/Library/LaunchAgents/ai.hermes.trigger.plist
curl -s --noproxy '*' http://127.0.0.1:8791/health
```

服务参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | `8791` | 监听端口 |
| `--host` | `127.0.0.1` | 绑定地址。可以是本机某个局域网 IP（如 `192.168.3.102`）；**`0.0.0.0` 直接拒绝启动** |
| `--db` | `~/.hermes/trigger.db` | SQLite 路径 |
| `--token-file` | `~/.hermes/secrets/trigger.token` | 母线 token；不存在则自动生成 32 字节 hex 并 `chmod 600` |
| `--allow-cidr` | `192.168.3.0/24` | 绑定非回环地址时允许的网段，可重复；回环地址永远放行 |
| `--require-token-for-trigger` | 关 | 让 `POST /trigger` 也要 token（默认本机来源免 token） |
| `--subscription-ttl` | `3600` | 订阅记录的有效期（秒）；环境变量 `TRIGGER_SUBSCRIPTION_TTL` |
| `--renew-window` | `600` | 到期前多久开始提醒 `renew_required`（秒）；环境变量 `TRIGGER_RENEW_WINDOW`；`>= ttl` 会被夹到 `ttl-1` 并告警 |
| `--queue-max` | `128` | 每个订阅者的扇出队列槽数，P0/P1/P2 共用；环境变量 `TRIGGER_QUEUE_MAX`；满了按 6.2 规则丢弃 |
| `--log` | `./trigger-server.log` | 日志文件 |
| `--once` | 关 | 只处理一个 HTTP 请求就退出（自测用） |
| `--verbose` | 关 | DEBUG 级别 + stderr 输出 |

启动时会打印实际绑定地址和 token 文件位置：

```
mowu-trigger 0.7.0 listening on http://127.0.0.1:8791  [remote subscribe needs X-Trigger-Token from /Users/kibyou/.hermes/secrets/trigger.token]
```

> 0.3.0 移除了 `--config` / `--no-mc` / `--no-email`：服务端不再有来源，
> 自然也没有「禁用来源」这回事。旧的 `sources/` 适配器整体移到了
> `attic/sources/`（保留可查，不参与运行）。

### 开放给局域网

```bash
/usr/bin/python3 server.py --host 192.168.3.102 --port 8791
# 只放行指定网段（可重复；回环始终允许）
/usr/bin/python3 server.py --host 192.168.3.102 --allow-cidr 192.168.3.0/24
```

不在白名单里的连接拿到 `403 {"ok":false,"error":"forbidden"}`，`/health` 也一样。

---

## 3. 鉴权

`/subscribe`、`/events`、`/ack`、`/renew`、`/unsubscribe`、`/subscriptions` 都必须带请求头：

```
X-Trigger-Token: <~/.hermes/secrets/trigger.token 的内容>
```

- 缺失或错误 → `401 {"ok": false, "error": "unauthorized"}`，**响应体不含任何事件内容**。
- `POST /trigger` 默认不校验（本机来源）；加 `--require-token-for-trigger` 可打开。
- `/health`、`/bus/health`、`/` 不需要 token（只有计数和订阅者名字，没有正文）。

```bash
TOKEN=$(cat ~/.hermes/secrets/trigger.token)
curl -s -H "X-Trigger-Token: $TOKEN" \
  "http://127.0.0.1:8791/events?subscriber=me&since=0&limit=5"
```

---

## 4. HTTP 接口

所有响应均为 JSON，UTF-8（`/subscribe` 除外，它是 `text/event-stream`）。

### `POST /trigger`

```json
{
  "source": "mc_chat",
  "kind": "chat",
  "level": "info",
  "priority": 2,
  "actor": "Steve",
  "text": "hello",
  "payload": {"any": "json"},
  "event_id": "optional-stable-id"
}
```

- `source`、`text` 必填；`kind` 默认 `"event"`。
- `level` 是 **0.7.0 的四级投递语义**，缺省 `"info"`，按权重从高到低：

| level | 含义 | 默认 priority | 唤醒溪流 | 回合中途打断 | worker 订阅 |
|---|---|---|---|---|---|
| `event` | 最高级：立刻停手处理 | `0` | 是 | **是**（`transform_tool_result` 注入） | 是 |
| `notify` | 次高级：本轮结束后处理 | `0` | 是 | 否（下一次 `pre_llm_call` 注入） | 是 |
| `info` | 普通事件 | `2` | **否**（不注入） | 否 | 是 |
| `log` | 只存档 | `3` | 否 | 否 | **否**（不入任何订阅流） |

- 兼容：`level` 传旧值 `super` 或 `super_event` 会**映射为 `event`**，不报错；
  入库和回显一律是 `event`。其他未知值返回 400 `{"error": "bad_level"}`。
- 显式 `priority` 优先于 level 的缺省值（约定 0 最高、5 最低，服务端不强制）。
  唤醒判定 = `level in ("event", "notify")` 且最终 `priority <= 0`；`info` 即使
  带了 P0 也不会进入注入队列。L2 的 `quiet`/`downgrade` 可以把高级别降到不唤醒。
- `event_id` 缺省为 `sha256(source + text + ts)`。
- `dedupe_key` 可选，缺省 `source + ":" + event_id`；`(dedupe_key, source)` 唯一。
  重复时同样返回原 `id`，响应带 `"duplicate": true`。
- `source` 若在 `sources/*/source.json` 里声明，`text`/`actor` 等字段按声明的
  `schema` 做最小类型校验，不符返回 400 `{"error": "schema_mismatch", "field": "..."}`；
  未声明的 `source` 照收，只记一次 warn。见 `SOURCES-DECL.md`。
- 0.6.0 起，落库前先过 L2 规则（`~/.hermes/trigger-rules.json`，见第 8 节）：
  `downgrade` 会改写实际入库的 `priority`；`rate_limit` 超限可按 `on_exceed=drop`
  拦下（仍返回 200，入库但 `disposition='dropped'`、不投递）。`level=log` 不经过
  L2，直接存档。响应带 `level` / `priority` / `wake` / `delivered` /
  `disposition` / `rule` / `reason` / `rules_matched`。
- 可选 `thread_key`（或 `payload.thread_key`），供 L2 的 `dedupe`/`quiet` 按线程聚合。
- **落盘后才回 200**；重复 `event_id` 不重复入库，返回原 id：

```json
{"ok": true, "id": 6, "event_id": "selftest-fixed-001", "duplicate": true}
```

普通事件的响应示例：

```json
{"ok": true, "id": 9, "event_id": "mc_chat:sse:9", "duplicate": false,
 "level": "info", "priority": 2, "wake": false, "delivered": true,
 "disposition": "delivered", "rule": "", "reason": "", "rules_matched": []}
```

### `GET /events?subscriber=<name>&since=<id>&limit=50&wait=0&levels=event,info&include_log=1`

返回**严格大于** `since`、且该订阅者没有 receipt 的事件，按 `id` 升序，最多 `limit`
（上限 200）。**不会自动推进游标**，同一个 `since` 可以重复拉取。

**按 level 过滤（0.7.0）**：

- `subscriber=hermes-main`（以及任何以 `hermes` 开头的订阅者，即溪流自己的轮询）
  默认只返回 `event,notify`；`info` 不注入，`log` 永远不会出现。
- 普通 worker 默认返回 `event,notify,info`；`log` 永远不进订阅流。
- `?levels=` 可显式覆盖默认集合（如 `?levels=info` 只看普通事件、
  `?levels=event,info` 显式要两级）；旧值 `super` / `super_event` 会折叠成
  `event`。`?levels=log` 返回 400 —— `log` 不是可投递级别。
- 响应顶层带 `levels` 数组（本次生效的级别），每条事件带 `level`。
- `?include_log=1` 只用于**存档排查**，把 `log` 行也带出来（需要 token）。
  `log` 不是订阅流的一部分，溪流也没有常驻连接。

0.4.0 起响应是对象：事件在 `events`，订阅周期在 `subscription`（字段见第 7 节）。

```json
{"events": [{"id": 8, "event_id": "mc_chat:sse:1", "source": "mc_chat", "kind": "chat",
             "level": "event", "priority": 0, "wake": true, "actor": "Steve",
             "text": "早上好", "payload": {"player": "Steve"},
             "ts": "2026-11-08T10:21:14+08:00", "created_at": 1794104474.9}],
 "subscription": {"name": "worker-a", "expires_in": 3597.2,
                  "renew_required": false, "expired": false,
                  "renew_count": 0, "status": "active"}}
```

- `since` 省略时使用 `cursors` 表里该订阅者的 `last_id`。
- `wait=<秒>` 长轮询：有新事件立即返回；超时返回空 `events`（最长 300 秒）。
  等待时间会被**截断到 `expires_in`**，不会睡过到期点。
- `subscription.expired=true` 时 `events` **一定是空数组**，即使 `since=0`。
- 需要 token。
- 兼容：0.3.0 返回裸数组。旧 worker 用
  `data["events"] if isinstance(data, dict) else data` 即可同时吃两种。

### `GET /drops?subscriber=<name>&since=<ISO|epoch>&limit=100`

队列丢弃审计。**丢弃只写日志、不发事件通知**，这里是唯一查询入口（读
`~/.hermes/trigger-drops.jsonl`，从文件尾部倒序扫描，不会整文件载入内存）。

```json
{"drops": [{"at": "2026-11-08T16:01:54+08:00",
            "subscriber": "hermes-main", "priority": 2,
            "dropped_event_id": "mc_chat:sse:8", "dropped_seq": 8,
            "reason": "queue_full", "queue_len": 128,
            "dropped_total": 1}],
 "count": 1}
```

- 按文件尾部倒序返回（最新在前）；`subscriber` 精确匹配，省略则返回全部。
- `since` 可传 ISO-8601 或 epoch 秒，是 `at` 的**闭区间下界**；非法值 → 400。
- `limit` 默认 100，硬上限 1000；`limit < 1` → 400。
- 无匹配时返回 `{"drops": [], "count": 0}`。
- 需要 token（与 `/events` 一致）。本地母线 `clients/localbus.py` 提供同款
  `/drops`，但沿用本机母线无 token 的约定。

### `GET /blocked?source=&since=<ISO|epoch>&limit=100`

被规则拦下的记录（`rate_limit` 超限 / `dedupe` 重复）。**拦截只写日志、不投递**，
这里是唯一查询入口（读 `~/.hermes/trigger-blocked.jsonl`，从尾部倒序扫描）。

```json
{"blocked": [{"at": "2026-11-08T16:20:03+08:00", "source": "mc_chat",
              "kind": "chat", "level": "info", "actor": "Ratey",
              "event_id": "mc_chat:sse:9", "rule": "mc-rate",
              "reason": "rate_limit", "text": "hello"}],
 "count": 1}
```

- `source` 精确匹配，省略返回全部；`since` 是 `at` 的闭区间下界；`limit`
  默认 100、上限 1000。
- 字段与 JSONL 行完全一致；`reason` 常见值：`rate_limit`、`dedupe_window`。
- 需要 `X-Trigger-Token`。`log` 级别从不投递，也不会出现在这里（它不是被规则拦下）。

### `GET /subscribe?name=<subscriber>&since=<id>[&max_events=N][&levels=event,info]`（SSE，远端母线）

事件流式推送，**每条事件一行 `data: {...}`**，JSON 与 `GET /events` 相同，
额外带 `seq`（等于 `id`）。每 15 秒一行 `: ping` 心跳。

`levels` 默认 `event,notify,info`（worker 常驻连接）；`log` **永不进入 SSE**。
溪流不使用 SSE（按 turn 轮询 `GET /events`），默认只拿 `event,notify`。

```
: connected

data: {"id": 16, "seq": 16, "event_id": "mc_chat:sse:5", "source": "mc_chat", ...}

: ping
```

- `since` 省略 → 用该 name 的游标；没有游标就从「现在」开始（不刷历史）。
- 需要 token。支持 `max_events`，见第 6 节。
- 首帧可能是 `event: gap`（重连的 `since` 早于可补发范围时说明补不上的区间）；
  **队列溢出不发 `gap` / `dropped` 帧**，见 6.2。
- 进入续期窗口时推一帧 `event: subscription`，到期推 `event: expired` 后断开，
  见第 7 节。

### `POST /ack`

```json
{"subscriber": "hermes-main", "up_to_id": 42}
{"name": "remote-worker", "up_to_seq": 42}
```

两种写法等价（`name`/`up_to_seq` 是母线风格）。推进游标到 `max(旧游标, 目标)`，
并为区间内的事件写 `receipts`。

**`/ack` 只推进游标**：它既不是心跳（存活只看连接），也不是续期（续期只认
`/renew`）。

### `POST /renew`

```json
{"name": "hermes-main"}
```

```json
{"ok": true, "name": "hermes-main", "expires_in": 3600.0, "renew_count": 3}
```

重置订阅时钟：`expires_at = now + --subscription-ttl`，`renew_count + 1`。
未知 name → 404；记录已 `closed` / 超过 24h 宽限 → 410。需要 token。

### `POST /unsubscribe`（等价 `DELETE /subscriptions?name=...`）

```json
{"name": "hermes-main"}
{"ok": true, "name": "hermes-main", "status": "closed"}
```

立即停投递：记录标 `closed`，同名 SSE 连接被关闭。需要 token。

### `GET /subscriptions`

```json
{"ok": true, "count": 1, "subscription_ttl": 3600, "renew_window": 600,
 "subscriptions": [{"name": "hermes-main", "expires_in": 3520.4,
                    "renew_required": false, "expired": false, "renew_count": 2,
                    "status": "active", "since": 41, "created_at": 1794100000.1,
                    "expires_at": 1794107200.1, "last_seen": 1794103580.0}]}
```

需要 token。字段定义见第 7 节。

### `GET /health`

```json
{"ok": true, "version": "0.7.0", "levels": ["event", "notify", "info", "log"],
 "events": 13, "sources": 2, "port": 8791,
 "db_path": "/Users/kibyou/.hermes/trigger.db", "uptime_s": 5.0,
 "subscribers": [],
 "queue_max": 128, "drops_log": "~/.hermes/trigger-drops.jsonl",
 "rules": 4, "rules_mtime": "2026-11-08T16:19:11+08:00",
 "rules_file": "~/.hermes/trigger-rules.json", "rules_error": null,
 "blocked_log": "~/.hermes/trigger-blocked.jsonl",
 "subscription_ttl": 3600, "renew_window": 600,
 "subscriptions": {"active": 2, "expired": 0, "closed": 0, "total": 2}}
```

- `events` 库内总条数，`uptime_s` 进程运行秒数。
- `sources` 已加载的声明数量（不是事件来源数量；未声明的来源不计）。
- `rules` 当前生效的 L2 规则条数（含 `enabled:false`）；`rules_mtime` 是
  已加载规则文件的 mtime，文件不存在为 `null`；`rules_error` 是最近一次
  热重载失败的原因（正常为 `null`），失败时旧规则仍然生效。
- `subscribers` 是当前活着的 `/subscribe` 连接：`{name, since, delivered,
  queue, queue_max, dropped_total, blocked_reason, connected_s}` ——
  **只反映连接，不反映订阅记录**。
- `queue_max` 是每个订阅者队列的容量（默认 128），`drops_log` 是丢弃日志路径。
- `subscription_ttl` / `renew_window` 是本次进程的周期参数，`subscriptions`
  是记录状态计数。

### `GET /sources`

来源声明清单（声明元数据，不含事件内容，因此和 `/health` 一样不需要 token）：

```json
[{"name": "mc_chat", "title": "Minecraft 聊天栏", "mode": "push",
  "schema": {"text": "string", "actor": "string"},
  "policy": {"default_priority": 0, "wake": true},
  "dedupe": ["actor", "ts", "text"], "declared": true,
  "health": {"max_silence_s": 3600, "last_event_at": "2026-11-08T11:58:26+08:00",
             "silence_s": 41.2, "stale": false, "last_error": null}}]
```

- 只列已声明来源；从未有事件的声明 `last_event_at`/`silence_s` 为 `null`、
  `stale=false`；超过 `max_silence_s` 秒没有事件才 `stale=true`。
- `last_error` 恒为 `null`：服务端不探活、不跑客户端，没有来源侧错误可报。
- 声明在**进程启动时**加载一次；新增来源需要重启服务。

### `GET /bus/health`

```json
{"ok": true,
 "subscribers": [{"name": "remote-worker", "since": 42, "delivered": 17,
                  "queue": 0, "queue_max": 128, "dropped_total": 0,
                  "blocked_reason": "", "connected_s": 12.3}],
 "events": 13, "last_id": 42}
```

### `GET /stats?subscriber=<name>`

```json
{"unread": 0, "last_id": 12,
 "levels": ["event", "notify", "info", "log"],
 "by_level": {"event": 3, "notify": 1, "info": 5, "log": 2},
 "sources": [{"name": "mc_chat", "declared": true, "health": {...}},
             {"name": "selftest", "declared": false, "health": {...}}]}
```

- `sources` 与 `/sources` 同结构，但**额外包含已有事件却未声明的来源**
  （`declared: false`，`max_silence_s: null`），用于发现漏声明的客户端。
- `levels` 是四级正式值；`by_level` 是库里**未被规则丢弃**的事件按 level 计数
  （建表时就带每一级，缺的为 0）。
- `unread` 是“库里有、该订阅者还没 ack”的条数，**包含 `level=log`** 的存档
  事件（它们不会出现在 `/events` 默认投递里，这是有意的审计口径）。
- 需要 token。

### `GET /`

服务名/版本/端点列表，便于 `curl` 探活：

```json
{"ok": true, "service": "mowu-trigger", "version": "0.7.0",
 "endpoints": ["/trigger", "/sources", "/events", "/drops", "/blocked", "/ack", "/renew",
               "/subscriptions", "/unsubscribe", "/health", "/stats",
               "/subscribe", "/bus/health"]}
```

错误统一 `{"ok": false, "error": "..."}`：400 参数错、401 无 token、403 网段外、404 未知路径。

---

## 5. 事件信封与投递语义

客户端在 `POST /trigger` 之前，把来源原始数据归一化成统一信封：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source` | str | 来源名，`mc_chat` / `email` / 任意自定义；参与默认 `event_id` 计算 |
| `kind` | str | `chat` / `death` / `system` / `email` / `event`…由客户端定义 |
| `level` | str | 四级（高到低）：`event` / `notify` / `info` / `log`；缺省 `info`。旧值 `super`、`super_event` 映射为 `event` |
| `priority` | int | 0 最高（打断级）、1 重要、2 普通、3 低；缺省由 level 决定 |
| `actor` | str | 是谁：玩家名 / 发件人 |
| `text` | str | 一句话正文，Hermes 注入时直接展示 |
| `payload` | obj | 原始数据（结构化字段、uid、subject 等），服务端原样存 JSON |
| `event_id` | str | 幂等键；客户端应尽量用来源侧稳定 id |
| `seq` | int | **只在母线里出现**：`/trigger` 的 `id`（服务端）或客户端自增序号（本地母线） |

投递语义（客户端侧实现，服务端只负责去重）：

- **at-least-once**：POST 失败按 `0.5s / 1s / 2s / 4s` 退避重试，最多 4 次重试。
- **幂等**：整段重试复用同一个 `event_id`。重试成功时服务端返回
  `duplicate:true`，不产生第二条事件。
- **上行与本地发布用同一个 `event_id`，但只发布一次**：重试不会让本地母线
  出现第二条同样的消息。
- 4 次重试都失败 → 客户端记 `DROP after retries` 到自己的日志，**不静默丢**。

### 5.1 游标三层（源侧 / 订阅者 / 去重窗口）

**身份 ≠ 位置**：`event_id` 回答「这是不是同一条消息」，游标回答「我从哪继续」。
三层各管各的，不要混：

| 层 | 存哪 | 答什么问题 | 现状 |
|---|---|---|---|
| ① 源侧游标 | 客户端本地 `clients/state/<source>_cursor.json` | 在**外部系统**里读到哪了 | email：`email_cursor.json`（UIDVALIDITY + last_uid + uidnext）；mc_chat：`mc_chat_cursor.json`（`last_seq`） |
| ② 订阅者游标 | trigger 的 `cursors(subscriber, last_id)` 表 | 这条流水线**发给某个消费者**到哪了 | `since` 续传 + `POST /ack` 推进（见第 6、7 节） |
| ③ 去重窗口 | 服务端内存（L2 `dedupe` 规则） | 同一指纹短时间**重复**要不要处理 | `~/.hermes/trigger-rules.json` 配窗口与 `by`；窗口内重复事件入库但 `disposition='dropped'`，不投递 |

按源类型分开配参数，不要一套套所有源：

| 源类型 | 有历史可回看？ | lookback | 重启后 |
|---|---|---|---|
| push 型 · MC 聊天 | **没有**（每次登录聊天框清空） | **0** | 桥 `lastSeq` 变小 ⇒ 新会话，重立基线不重放 |
| pull 型 · 邮箱 | 有（IMAP 服务器保留） | **5s** | 从 last UID 继续；UIDVALIDITY 变了重置 |
| push 型 · 以后 webhook | 通常没有 | 0 | 同 MC |

**兜底写死**：游标文件丢失/损坏/字段不合法 → 不重放历史（宁漏不重），
打一条 WARNING 并以当前 `lastSeq` 立基线。`--no-cursor` 可完全关掉这一层
（回到 0.4.0 的无游标行为）。

**已知边界（不打算为它改 mod）**：桥的 `/state.events` 只有
`{lastSeq, buffered}`，没有 bootId/sessionId。如果游戏重启后、客户端重连前
新会话已经聊过几百条（`lastSeq` 超过上次存的 `last_seq`），会被误判成同一
会话；这些新消息的 `seq` 与旧会话撞号 → `event_id` 相同，服务端当
`duplicate` 丢掉，直到 `seq` 越过旧水位。正常聊天频率下可接受。

---

## 6. 订阅生命周期（本地母线与远端母线完全一致）

实现在 `buscore.py`，两边共用同一份代码。

### 6.1 存活 = 连接本身

- 每 **15 秒** 发一行 `: ping` 心跳。
- **连续 5 次写失败**，或**单次写阻塞超过 10 秒** → 立即关闭，日志
  `reason=write_failures`。
- 对端 EOF/RST（worker 进程被杀）→ **1 秒内**清理订阅者条目，日志
  `reason=client_eof`。
- **连接存活与订阅周期是两件事**：周期（`expires_at` / `renew_required` /
  `event: expired`）见第 7 节。连接活着不会自动续期，`/ack` 既不是心跳
  也不是续期。

### 6.2 队列与丢弃

每个订阅者一条有界队列，**P0 / P1 / P2 共用同一容量**（默认 **128**，可配
`--queue-max 128` 或环境变量 `TRIGGER_QUEUE_MAX`，远端母线与本地母线同参）。
发布路径只做入队，**永不阻塞发布者**，也**永远接收新消息**。

队列满时按固定规则选一个受害者，新消息照进：

1. 找队列里**最低的那一档**（priority 数值最大者，P2 最低）；
2. 丢掉**那一档里最早的一条**；
3. 没有 P2 就丢最早的 P1；只剩 P0 才丢最早的 P0。

不做折叠、不做 TTL、不给 P0 任何特权。规则只实现一份：
`buscore.victim_index()`，`server.py` 与 `clients/localbus.py` 共用。
priority 归一到三档（`buscore.normalise_priority()`：`<=0`→P0，`1`→P1，
`>=2`→P2，因此历史默认的 `priority=3` 按 P2 处理）。

每次丢弃**只做三件事**：写一行 JSONL、内存计数 `dropped_total`、队列里少一条。
队列溢出**不发任何通知**（既没有 `event: dropped`，也没有 `gap`）——日志是唯一
事实源（SSOT），谁想知道自己来查 `GET /drops`。

**下游如何判断丢失：`seq` 不连续即丢。** 例如上次收到 `seq=10`、这次收到
`seq=20`，就说明 `11..19` 被丢过（或补发窗口外），不要假设事件流是连续的。

日志固定为 `~/.hermes/trigger-drops.jsonl`，按行追加（`O_APPEND` +
`fcntl.flock` 串行，不重写整个文件），一行一条：

```json
{"at": "2026-11-08T16:01:54+08:00", "subscriber": "hermes-main", "priority": 2,
 "dropped_event_id": "mc_chat:sse:8", "dropped_seq": 8, "reason": "queue_full",
 "queue_len": 128, "dropped_total": 1}
```

| 字段 | 含义 |
|---|---|
| `at` | 丢弃时刻，ISO-8601 带本机时区（`+08:00`） |
| `subscriber` | 哪个订阅者的队列 |
| `priority` | 被丢事件归一后的档位 `0`/`1`/`2` |
| `dropped_event_id` | 被丢事件的 `event_id`；没有则退回 `id` 或 `source:seq` |
| `dropped_seq` | 被丢事件在该订阅者队列里的 `seq`（远端母线等于事件 `id`） |
| `reason` | 当前固定 `queue_full` |
| `queue_len` | 队列容量（丢弃发生时已满） |
| `dropped_total` | 该订阅者本次连接生命周期内的累计丢弃数（进程重启归零） |

本地母线（`clients/localbus.py`）规则、容量、日志完全相同：内存事件重启即丢，
但**丢弃日志照样落盘**。两边都用 `GET /drops` 查。

### 6.3 断线恢复：带 since 重连

worker 重连时带 `since=<最后收到的 seq>`：

- 缓冲/数据库里还有的会补发，**不重复、不丢**；
- 早于最旧缓冲的部分，SSE 首帧发 `event: gap` 说明丢失区间，再继续推；
- 短时间断线（缓冲里还在）不会收到 gap 帧。

### 6.4 配额：`max_events`

`/subscribe?...&max_events=3` 表示这次连接最多收 3 条。用尽后：

```
event: exhausted
data: {"last_seq": 313}
```

然后服务端关闭这条流（`reason=quota`）。worker 用 **同一个 name + `since=313`**
重新订阅即续订，配额重置。

`max_events` 省略或 `0` = 不限。

### 6.5 轮询接口的语义（`GET /events`）

`/events` 是快照 + 游标，不是流。0.4.0 起统一返回
`{"events": [...], "subscription": {...}}`；`since` 早于本地母线环形缓冲最旧
序号时，多带缺口说明：

```json
{"gap": true, "oldest_seq": 120, "last_seq": 500, "count": 50,
 "events": [...],
 "subscription": {"name": "hermes-main", "expires_in": 3590.2,
                  "renew_required": false, "expired": false,
                  "renew_count": 0, "status": "active"}}
```

订阅到期时同一接口返回 `{"events": [], "subscription": {"expired": true, ...}}`
—— 不投递，但也不删记录（24h 内可 `/renew` 复活），见第 7 节。

---

## 7. 订阅周期与续期（0.4.0）

每条订阅记录有自己的时钟，**和 SSE 连接是否活着无关**。远端母线（SQLite）和
本地母线（内存 `SubscriptionLedger`）用同一套语义，`subscription` 字段都由
`buscore.subscription_snapshot()` 生成，worker 只需要写一次解析。

### 7.1 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `name` | str | 订阅名（worker 自己的身份） |
| `expires_in` | float | 距到期还有多少秒；已到期为 `0.0` |
| `renew_required` | bool | 剩余时间 ≤ `renew_window`（默认 600s = 10 分钟）且未到期时为 `true` |
| `expired` | bool | `true` = **停止投递**：时钟到点，或记录被 `/unsubscribe` 关闭 |
| `renew_count` | int | 成功 `/renew` 的次数，从 0 开始 |
| `status` | str | `active` / `expired`（超过 24h 宽限被 janitor 标记）/ `closed`（主动退订） |

`GET /events`、`GET /subscriptions`、SSE 的 `event: subscription` 帧是同一份
字段，只有 SSE 帧是精简版（`name` / `expires_in` / `renew_required`）。

### 7.2 提前 10 分钟提醒

- 首次见到某个 `name`（第一次 `/events` 或 `/subscribe`）建记录：
  `expires_at = now + --subscription-ttl`（默认 3600s）。之后每次拉取只刷新
  `last_seen`，**不动 `expires_at`**。
- 剩余时间 ≤ `--renew-window`（默认 600s）时：
  - `GET /events` 的 `subscription.renew_required` 变成 `true`；
  - SSE 推一帧（每个周期只推一次）：

```
event: subscription
data: {"name": "hermes-main", "expires_in": 587.3, "renew_required": true}
```

- `/renew` 之后 `renew_count` 变化，下一个周期会重新提醒。
- 服务端**不替客户端续期**：看到 `renew_required=true` 就去 `POST /renew`。

### 7.3 到期行为

- 到 `expires_at` 仍未续期 → **停止投递**：
  - `GET /events` 返回 `{"events": [], "subscription": {... "expired": true}}`，
    **即使 `since=0` 也是空数组** —— 这是"不投递"，不是"把历史删了"；
  - SSE 发 `event: expired` 后断开（`reason=expired`）；
  - 新连接（`/subscribe`）直接被拒：`event: expired` 后关闭。
- 记录保留 **24 小时**（`SUBSCRIPTION_GRACE`）：期间 `POST /renew` 仍可复活，
  `renew_count` 继续累加，事件从原游标继续，一条不丢。
- 超过 24h 仍未被使用的 `active` 记录被 janitor 标成 `status=expired`，之后
  `/renew` 返回 410 —— 只能重新建立订阅。
- 过期/关闭的记录**不会被静默删除**，`GET /subscriptions` 里看得见。

### 7.4 接口

**`POST /renew`**

```json
{"name": "hermes-main"}
```

```json
{"ok": true, "name": "hermes-main", "expires_in": 3600.0, "renew_count": 3}
```

- 200：`expires_at = now + ttl`，`renew_count + 1`
- 404：没有这个 name
- 410：记录是 `closed` / 超过 24h 宽限的 `expired`，不可复活

**`POST /unsubscribe`**（远端还支持 `DELETE /subscriptions?name=...`）

```json
{"name": "hermes-main"}
{"ok": true, "name": "hermes-main", "status": "closed"}
```

立即停投递：记录标 `closed`，同名 SSE 连接被关掉。之后 `/events` 一直是
`events: []`，直到显式重新订阅。

**`GET /subscriptions`**

```json
{"ok": true, "count": 1, "subscription_ttl": 3600, "renew_window": 600,
 "subscriptions": [{"name": "hermes-main", "expires_in": 3520.4,
                    "renew_required": false, "expired": false, "renew_count": 2,
                    "status": "active", "since": 41, "created_at": 1794100000.1,
                    "expires_at": 1794107200.1, "last_seen": 1794103580.0}]}
```

本地母线同样提供 `/renew`、`/unsubscribe`、`/subscriptions`，字段一致；
`/bus/health` 额外带 `subscription_ttl`、`renew_window`、`subscriptions` 计数。

### 7.5 为什么周期不等于连接存活

三件事互不替代：

| 机制 | 管什么 | 不管什么 |
|---|---|---|
| SSE 连接 + `: ping` | 这次**连接**还在不在 | 订阅记录还能用多久 |
| `POST /ack` | 游标推进到哪 | 存活？续期？都不是 |
| `expires_at` | **投递权**还剩多久 | 连接是否活着 |

所以：worker 一小时没说话但连接一直开着 → 到点照样停投递；反过来，每秒
`/ack` 一次也不会续一秒。断线重连（`since` 续传）同样不改 `expires_at`。
这样"连接抖动"和"订阅预算"解耦：网络断了可以随便重连，但一个已经不该再收
事件的身份，不会因为重连就永久拿到数据。

### 7.6 worker 长轮询伪代码

```python
name, since = "worker-a", load_cursor()
TOKEN = read("~/.hermes/secrets/trigger.token")

while True:
    r = get(f"/events?subscriber={name}&since={since}&limit=50&wait=25",
            headers={"X-Trigger-Token": TOKEN})
    sub = r["subscription"]

    if sub["expired"]:
        # 时钟到点：先续期，再拉；绝不静默停摆
        status = post("/renew", {"name": name}, token=TOKEN)
        if status == 410:
            raise SystemExit("subscription is closed; re-subscribe explicitly")
        continue                      # 下一轮 immediately 重新拉

    if sub["renew_required"]:
        # 剩余 ≤ 10 分钟：现在就续，不必等到最后一秒
        post("/renew", {"name": name}, token=TOKEN)

    for event in r["events"]:
        handle(event)
        since = event["id"]           # 只推进本地游标

    if r["events"]:
        post("/ack", {"subscriber": name, "up_to_id": since}, token=TOKEN)
```

要点：

- `renew_required` 是**预告**，`expired` 是**事实**；两个分支都必须处理。
- `/renew` 和 `/events` 在同一轮里做完；`expired` 时要 `continue` 重拉，
  不要拿空数组当"没消息"就睡过去。
- `since` 用 `event["id"]`；本地母线的事件同时带 `seq`，两者语义相同。
- 410 说明身份已作废，重试没有意义 —— 重新 `/subscribe` 或 `/events`
  建一条新记录。
- `/renew` 本身失败（网络）不必退出：记录有 24h 宽限，下一轮重试即可。

---

## 8. L1/L2 分工与规则文件

### 8.1 谁管什么

| 层 | 位置 | 管什么 | 例子 |
|---|---|---|---|
| **L1 业务过滤** | 各源客户端（`clients/*_client.py`）+ `clients/filterwall.py` | **这个源特有**的业务语义、信封结构校验 | MC：自己发出去的消息不再送上来（`self_message`）；email：只看指定文件夹（`folder_not_watched`）；缺字段/类型错（`schema_invalid`） |
| **L2 通用机制** | `server.py` + `~/.hermes/trigger-rules.json` | **跨源共用**的规则，一处配置全局生效 | 限流（`rate_limit`）、去重窗口（`dedupe`）、静默期降级（`quiet`）、优先级降级（`downgrade`）。**不做黑白名单/admission**：单用户场景不需要，`drop` 只作为 `rate_limit` 超限时的 `on_exceed` 结果 |

边界是硬性的：

- L1 **不读**规则 JSON，不实现限流/去重/优先级映射；客户端的 `--rules` 参数
  从 0.6.0 起被忽略（只记一条 WARNING），`clients/filters/*.json` 已删除。
- L2 **不认识来源协议**。它只对信封字段（`source/kind/actor/text/priority`）
  和 `thread_key` 做判断，不为任何来源写特例代码。
- `filterwall.py` 只剩两个东西：`business_filter(envelope) -> (keep, reason)`
  钩子（默认做结构校验后放行）和 `log_skip()`（L1 跳过写 `clients/drops.log`）。

### 8.2 规则文件

路径默认 `~/.hermes/trigger-rules.json`（`--rules-file` 可改，自测必须改，
否则会碰生产配置）。**文件不存在 = 0 条规则 = 全部放行。**

热重载：每次 `POST /trigger` 前比较文件 mtime；变了就重读。读失败
（坏 JSON、根不是对象、`rules` 不是数组）时**保留上一份规则**并打一条
`WARNING rules: cannot load ... keeping N previous rule(s)`，服务不中断；
坏文件的 mtime 会被记住，同一个坏文件不会每个请求都重解析。修好文件后
下一次 POST 自动生效，无需重启。单条规则字段非法（坏正则、未知 `action.type`）
只跳过该条并 WARN，其余规则照常加载。`GET /health` 的 `rules` /
`rules_mtime` / `rules_error` 给出当前状态。

```json
{
  "version": 1,
  "rules": [
    {"name": "email-downgrade", "enabled": true,
     "match": {"source": "email"},
     "action": {"type": "downgrade", "to": 2}},

    {"name": "mc-rate", "enabled": true,
     "match": {"source": "mc_chat", "kind": "chat",
               "actor_regex": "^Ratey$"},
     "action": {"type": "rate_limit", "max": 30, "window_s": 60,
                "on_exceed": "drop"}},

    {"name": "dup-window", "enabled": true,
     "match": {"source": "*"},
     "action": {"type": "dedupe", "window_s": 30, "by": "text+actor"}},

    {"name": "quiet-thread", "enabled": true,
     "match": {"source": "*"},
     "action": {"type": "quiet", "window_s": 300, "by": "thread_key",
                "to": 2}}
  ]
}
```

### 8.3 `match`（全部可选，缺省 = 通配）

| 字段 | 类型 | 语义 |
|---|---|---|
| `source` | str | 来源名，大小写不敏感 glob：`mc_chat`、`mc_*`、`*` |
| `kind` | str | 同上，匹配信封 `kind`（`chat`/`death`/`email`/…） |
| `actor_regex` | str | 对 `actor` 做 `re.search`（IGNORECASE）；`*` = 不限 |
| `text_regex` | str | 对 `text` 做 `re.search` |
| `thread_key_regex` | str | 对 `thread_key` 做 `re.search` |
| `priority_max` | int | 只匹配 `priority <= priority_max` 的事件（0 最高） |

`thread_key` 取自 `POST /trigger` 的 `thread_key` 字段，缺省取
`payload.thread_key`。一条规则的所有非空条件必须同时满足（AND）。

### 8.4 `action`（数组顺序执行，拦截即停）

| type | 必填 | 行为 |
|---|---|---|
| `downgrade` | `to` | 把 priority 改成 `to`（数值变大 = 降级），继续下一条规则；日志记原值/新值 |
| `rate_limit` | `max`、`window_s` | 滑动窗口：窗口内最多放行 `max` 条；超出按 `on_exceed` 处理：`drop`（默认，入库但 `disposition='dropped'`、不投递）或 `downgrade`+`to`。启动时从 DB 回填最近 `window_s` 内**已投递**事件数，重启不是免费额度 |
| `dedupe` | `window_s`、`by` | 按 `by` 拼指纹（`text`、`actor`、`text+actor`、`thread_key`…），窗口内重复 → 不投递，`reason=dedupe_window` |
| `quiet` | `window_s`、`by`、`to` | 同一 key 在窗口内**已经叫醒过** → 把 priority 降到 `to`（默认 2），不丢弃 |

几条容易踩的语义：

- **没有独立的 `drop`/黑名单动作。** 只有 `rate_limit` 的 `on_exceed=drop` 和
  `dedupe` 的重复判断会产生“不投递”；两者都会入库并写审计。旧配置里的
  `{"type":"drop"}` 会在加载时被跳过并 WARN（用 `rate_limit` 的 `on_exceed` 代替）。
- **“叫醒”= `level=event` 且 `priority <= 0`**（与 `trigger-bus` 的
  `TRIGGER_BUS_WAKE_PRIORITY` 默认值一致）。`event` 缺省 P0，所以会开启静默期；
  `info` 缺省 P2，且无论 priority 多少都不会进入注入队列。窗口开启后，同 key
  的后续事件一律降到 `to`。
- `quiet` **只降级、不丢弃**：静默期内的消息仍然进 worker 订阅流，只是不叫醒溪流。
- `dedupe`/`quiet` 的 `by` 含 `thread_key` 而事件没有 `thread_key` 时，该规则
  不生效——空 key 不会把所有事件并成一条。
- `downgrade` 只能把优先级**降低**。要把某类消息提升到 P0，只能由来源客户端
  在信封里给 `level=event` / 初始 `priority`（那是它对自己业务的判断）；
  L2 不做“升级”，避免一个全局配置把无关来源全部叫醒。
- `level=log` **不经过 L2**：它只存档，规则对它既不降级也不拦截。
- 规则按数组顺序执行；被 `rate_limit` 拦下之前已经发生的 `downgrade` 会保留在
  落库的 `priority` 里。`enabled:false` 的规则跳过但仍然计入 `/health.rules`。

### 8.5 落库、审计与查询

- 每条事件都写进 `events`，带 `level`（`event`/`notify`/`info`/`log`）和
  `disposition`（`delivered`/`downgraded`/`dropped`）。老库自动
  `ALTER TABLE`，老行读作 `level='info'`、`disposition='delivered'`，不改写历史。
- `level='log'` 的事件**不会**出现在 `GET /events`（除非显式 `?include_log=1`）
  或 `GET /subscribe`，也不写任何订阅者队列；但 `events` 计数和 `/stats` 照算。
- `disposition='dropped'` 的事件（rate_limit 超限 / dedupe 重复）不会出现在
  `/events` 或 `/subscribe`，也不会计入未读数；它照样可以通过 `/blocked`
  和数据库审计。
- 每次“被规则拦下”追加一行 `~/.hermes/trigger-blocked.jsonl`
  （`--blocked-log` 可改）：
  `{"at","source","kind","level","actor","event_id","rule","reason","text"}`。
- `GET /blocked?source=&since=&limit=100`：从 JSONL 尾部倒序读，`source`
  精确过滤，`since` 是 `at` 的闭区间下界（ISO 或 epoch），`limit` 默认 100、
  上限 1000。需要 `X-Trigger-Token`。返回 `{"blocked":[...],"count":N}`。

### 8.6 自测

```bash
# L2 全动作 + 热重载 + 坏配置 + 重启回填 + /blocked（临时端口/库/规则）
python3 selftest_rules.py

# L1：假 bridge + 假 IMAP，self_message 不 POST，通用规则确实不再拦截
python3 clients/selftest_l1.py

# L1 钩子自己的单测 + drops.log 统计
cd clients
python3 filterwall.py --selftest
python3 filterwall.py --stats drops.log
```

---

## 9. MC 客户端 `clients/mc_chat_client.py`

```bash
cd /Users/kibyou/projects/mowu-trigger/clients

/usr/bin/python3 mc_chat_client.py                          # 常驻（带游标）
/usr/bin/python3 mc_chat_client.py --dry-run                # 只判定不投递
/usr/bin/python3 mc_chat_client.py --once-seconds 10        # 跑 10 秒退出
/usr/bin/python3 mc_chat_client.py --url http://127.0.0.1:8799/stream
/usr/bin/python3 mc_chat_client.py --listen-port 8892       # 本地母线换端口
/usr/bin/python3 mc_chat_client.py --no-bus                 # 不起本地母线
/usr/bin/python3 mc_chat_client.py --no-cursor              # 旧行为：不读不写游标
/usr/bin/python3 mc_chat_client.py --cursor-file /tmp/c.json --lookback 5
```

行为：

- 连 `http://127.0.0.1:8765/stream`（SSE），断线每 **5 秒**重连；
  第一次失败记一条 WARNING，之后降到 DEBUG，每 60 次才再记一条 INFO（不刷屏）。
- **源侧游标** `clients/state/mc_chat_cursor.json`：`{source, last_seq,
  saved_at, bridge}`，每处理完一条就原子写（tmp + `os.replace`）。续连时
  带 `?since=<last_seq>` 与 `Last-Event-ID: <last_seq>`；即使桥不从 `since`
  续传、把整段缓冲重放一遍，客户端也会丢掉 `seq <= last_seq` 的条目。
- **新会话检测**（MC 聊天框每次登录清空，桥没有 bootId）：连上前先
  `GET /state` 读 `events.lastSeq`。若它 **小于** 已存 `last_seq`，判定游戏
  重启 → 以当前 `lastSeq` 重立基线，**不重放**，日志
  `INFO new bridge session detected (seq went backwards X -> Y), re-baselined at Y, no replay`。
  否则视为同一会话，从 `last_seq` 继续。
- 游标文件缺失/损坏/字段不合法：**不重放历史**，以当前 `lastSeq` 立基线，
  记 `WARNING cursor missing/corrupt (...); baselined at N (no replay)`。
- `--lookback N`（MC 默认 **0**）：只有拉取型源需要（email 那类），按
  `saved_at - N 秒` 与事件 `ts` 比较，接受边界条目；事件没有 `ts` 时退化为
  精确 `last_seq` 并记一条 INFO。MC 聊天没有历史可回看，保持 0。
- `--no-cursor` 完全回到旧版：不读游标、不写游标、URL 不带 `since`。
- 优先级与级别：普通聊天 `2` / `info`；文本命中 `溪流|SuperCreek` → `0` / `event`；
  死亡消息（`slain by|died|fell from|drowned|burned|was killed`）→ `0` /
  `event` 且 `kind=death`；系统消息 → `2` / `info`。这是**来源自身**的语义判断
  （谁死了重要），L2 只能降级，不能升级；`info` 不唤醒溪流。
- **L1 业务过滤只有一条**：`sender == own_name and not --include-self` →
  `reason=self_message`，写 `clients/drops.log`，不 POST。要看/改通用规则
  （限流、去重、降级、静默期）去服务端 `~/.hermes/trigger-rules.json`
  （第 8 节）。`--rules` 仍被接受但忽略。
- 本地母线默认 `127.0.0.1:8792`：`/subscribe`、`/events?name=…`、`/ack`、
  `/renew`、`/unsubscribe`、`/subscriptions`、`/bus/health`（语义与 8791 相同）。
- 日志 `clients/mc_chat_client.log`（5 MB × 3 滚动）；L1 跳过的写 `clients/drops.log`。
- `--once-seconds N`、`--dry-run`、`--trigger-url`、`--drops-log`、`--verbose`。

没有 MC 时自测（不需要游戏）：

```bash
/usr/bin/python3 fake_sse_server.py --port 8799 &
/usr/bin/python3 mc_chat_client.py --url http://127.0.0.1:8799/stream --once-seconds 8
/usr/bin/python3 selftest_bus.py          # 两个订阅者（SSE + 轮询）比对 event_id
/usr/bin/python3 selftest_mc_cursor.py    # 游标：基线/续传/重放跳过/损坏/新会话/lookback
```

`selftest_mc_cursor.py` 会自己起 `fake_bridge.py`（带 `/state.events.lastSeq`、
`?since=`、`/reset`），全程用临时目录，不碰生产游标、dedupe 和 8791。

---

## 10. 邮箱客户端 `clients/email_client.py`

凭据从 `~/.hermes/secrets/email.env` 读取（**不读 argv、不写日志**）：

```bash
EMAIL_HOST=imap.example.com
EMAIL_PORT=993
EMAIL_USER=you@example.com
EMAIL_PASS=<IMAP 授权码，不是登录密码>
```

文件不存在或缺字段：打印中文提示 + 退码 **2**，不打错误栈、不发起任何网络请求。

```bash
/usr/bin/python3 email_client.py --once            # 拉一次就退出
/usr/bin/python3 email_client.py                   # 每 120 秒轮询
/usr/bin/python3 email_client.py --stub            # 注入两封假邮件（自测）
/usr/bin/python3 email_client.py --stub noreply    # 只注入 noreply 那封
/usr/bin/python3 email_client.py --stub --dry-run  # 只判定不投递
```

行为：

- `imaplib.IMAP4_SSL(host, 993)`，`select(folder, readonly=True)`，
  用 `BODY.PEEK[...]` 取信，**不会把邮件标为已读**。
- 游标 `clients/state/email_cursor.json`：`{folder, uidvalidity, uidnext, last_uid}`。
  首次连上记基线（`UIDNEXT-1`），不重放历史；`UIDVALIDITY` 变化自动重置基线。
- 信封：`source=email`、`kind=email`、`level=info`、`actor=发件人`、
  `text=subject — 正文前 200 字`，`payload` 带 `subject` / `uid` /
  `uidvalidity` / `from` / `date` / `folder`。
- **L1 业务过滤只有一条**：只处理 `--folder` 指定的文件夹；其它文件夹的邮件
  `reason=folder_not_watched`，写 `clients/drops.log`，不投递。`noreply` 拦截、
  限流、去重、优先级映射全部移到 L2（第 8 节），`--rules` 仍被接受但忽略。
- **不使用 IMAP IDLE**，固定 120 秒轮询（`--poll-seconds` 可改）。
  IDLE 只省延迟，不改变正确性；轮询在所有 IMAP 服务器上都可用。
- 本地母线默认 `127.0.0.1:8793`（`--listen-port` / `--no-bus` 同上）。
- `event_id = email:<uidvalidity>:<uid>`，重投不会产生重复事件。

---

## 11. 安装示例（LaunchAgent / cron）

### 10.1 服务端（已安装）

`~/Library/LaunchAgents/ai.hermes.trigger.plist`：

```xml
<key>ProgramArguments</key>
<array>
  <string>/usr/bin/python3</string>
  <string>/Users/kibyou/projects/mowu-trigger/server.py</string>
  <string>--port</string><string>8791</string>
  <string>--db</string><string>/Users/kibyou/.hermes/trigger.db</string>
</array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
```

> 要跨机器订阅，在数组里加 `--host 192.168.3.102`。**不要加 0.0.0.0。**

### 10.2 MC 客户端（常驻，示例）

`~/Library/LaunchAgents/ai.hermes.mc-chat-client.plist`：

```xml
<key>Label</key><string>ai.hermes.mc-chat-client</string>
<key>ProgramArguments</key>
<array>
  <string>/usr/bin/python3</string>
  <string>/Users/kibyou/projects/mowu-trigger/clients/mc_chat_client.py</string>
</array>
<key>WorkingDirectory</key>
<string>/Users/kibyou/projects/mowu-trigger/clients</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key>
<string>/Users/kibyou/projects/mowu-trigger/clients/launchd.mc.out.log</string>
<key>StandardErrorPath</key>
<string>/Users/kibyou/projects/mowu-trigger/clients/launchd.mc.err.log</string>
```

装上：`launchctl load ~/Library/LaunchAgents/ai.hermes.mc-chat-client.plist`。

### 10.3 邮箱客户端（二选一，**本次未安装**）

launchd 每 2 分钟跑一次 `--once`（比常驻简单，进程崩了也不影响下一轮）：

```xml
<key>Label</key><string>ai.hermes.email-client</string>
<key>ProgramArguments</key>
<array>
  <string>/usr/bin/python3</string>
  <string>/Users/kibyou/projects/mowu-trigger/clients/email_client.py</string>
  <string>--once</string>
</array>
<key>WorkingDirectory</key>
<string>/Users/kibyou/projects/mowu-trigger/clients</string>
<key>StartInterval</key><integer>120</integer>
<key>RunAtLoad</key><false/>
```

cron 写法（等价，注意 cron 不读登录环境）：

```cron
*/2 * * * * cd /Users/kibyou/projects/mowu-trigger/clients && \
  /usr/bin/python3 email_client.py --once >> /tmp/email_client.cron.log 2>&1
```

> 装之前先把 `~/.hermes/secrets/email.env` 建好，否则每轮都会以退码 2 空转。

---

## 12. 数据模型（`~/.hermes/trigger.db`）

| 表 | 用途 |
|---|---|
| `events(id, event_id UNIQUE, source, kind, level, priority, actor, text, payload_json, ts, dedupe_key, disposition, created_at)` | 所有事件，`event_id` 去重；`level`: `event`/`notify`/`info`/`log`；`disposition`: `delivered`/`downgraded`/`dropped` |
| `cursors(subscriber PK, last_id, updated_at)` | 每个订阅者已确认到的位置 |
| `receipts(event_id, subscriber, read_at, PK(event_id, subscriber))` | 已读明细，防止游标回退后重复投递 |
| `subscriptions(name PK, since, created_at, expires_at, renew_count, last_seen, status)` | 每个 worker 的投递预算（第 7 节）；`status`: `active`/`expired`/`closed` |
| `rules(id, name, match_json, action, priority, enabled)` | **保留的旧表，0.6.0 不读它**；规则来自 `~/.hermes/trigger-rules.json` |

WAL 模式；单连接 + 一把锁串行化写入。长轮询在 `Condition` 上等待，写入时
`notify_all()`，所以新事件通常在几十毫秒内唤醒等待者。SSE 订阅者由
`POST /trigger` 写入后直接 fan-out（同样不阻塞请求线程）。

---

## 13. Hermes 插件 `trigger-bus`

位置：`~/.hermes/plugins/trigger-bus/`（**本次没有启用、没有重启 gateway**）。

- 后台线程每 2 秒 `GET /events?subscriber=hermes-main&since=<cursor>`；
  游标持久化在 `~/.hermes/trigger-cursor.json`。
- 自动读取 `~/.hermes/secrets/trigger.token` 并带 `X-Trigger-Token` 头
  （`TRIGGER_TOKEN_PATH` 可覆盖）；文件缺失时插件静默降级，与本服务宕机同表现。
- **自动续期（0.4.0）**：每次拉取都看 `subscription`：
  - `renew_required=true` → 立即 `POST /renew`，`renew_count` 变化时记一行
    `RENEW name=... renew_count=... expires_in=...s`（INFO）；
  - `expired=true` → 清空内存队列（不让旧事件被注入）、记一条 WARNING，
    随即 `POST /renew` 并在同一轮重拉，绝不静默停摆；
  - `/renew` 返回 404/410（记录已 `closed` 或超过 24h 宽限）→ WARNING 一次，
    轮询继续但队列保持空，等显式重建订阅；
  - `/renew` 网络失败不退出：记录有 24h 宽限，下一轮重试。
- 新事件在内置队列里格式化为 `[EVENT <source>/<kind> <ts>] <text>`，内存
  `deque(maxlen=50)`；注入上下文与唤醒消息共用这个前缀。
- `pre_llm_call` 只注入 `event` 与 `notify`（`info`/`log` 永不注入），返回
  `{"context": "..."}`，随后清空队列、写游标、异步 `POST /ack`。
- **回合中途打断（0.5.0）**：注册 `transform_tool_result`。每次工具返回时，
  若队列里有尚未注入过的 `event`，就在结果末尾追加
  `\n\n[EVENT] 立刻停下当前任务，先处理这条：「<text 前 120 字>」（来源 <source>，<ts>）。处理完再回到刚才的事。`
  同一事件只插一次（独立游标 `interrupt_cursor` + 已注记集合 `interrupt_done`），
  `pre_llm_call` 也会查这个集合，绝不重复给。`notify` 不参与中途插入。
- **真打断 + 可辨识来源**：唤醒路径合成的 `MessageEvent` 按级别决定 `internal`：
  `event` → `internal=False`（走正常入站路径，忙时会触发 gateway 的
  「⚡ Interrupting current task」）；`notify` → `internal=True`（只唤醒空闲会话，
  忙时静默排队）。消息文本为 `[EVENT <source>/<kind> <ts>] <原文>`，并带
  `metadata["interrupt_origin"]`（`source · actor`），gateway 的忙时提示会显示
  「⚡ 来自 mc_chat · Steve 的打断」。
- **防风暴**：5 秒窗口内的多条事件合并成一条消息（展开 1 条，其余写
  「另有 N 条」），窗口长度可用 `TRIGGER_BUS_INTERRUPT_WINDOW` 调整。
- `TRIGGER_BUS_DISABLED=1` → `register()` 直接返回。

启用方式（**需要确认后再做，本次未执行**）：在 `~/.hermes/config.yaml` 的
`plugins.enabled` 里加 `trigger-bus`，然后重启 gateway。

---

## 14. 目录结构

```
mowu-trigger/
├── server.py                  # 纯收件箱 + L2 规则引擎 + 远端母线（无来源逻辑）
├── buscore.py                 # 订阅生命周期 + 队列丢弃策略（两条母线共用）
│                              # 运行期另有 ~/.hermes/trigger-drops.jsonl
│                              #           ~/.hermes/trigger-rules.json
│                              #           ~/.hermes/trigger-blocked.jsonl
├── selftest_rules.py          # 0.7.0 L2 端到端自测（临时端口/库/规则）
├── selftest_levels.py         # 0.7.0 四级投递 + 插件回合中途注入自测
├── README.md
├── trigger-server.log
├── attic/
│   ├── sources/               # 0.2.0 的拉取式适配器，已退役，仅存档
│   └── sources.json
└── clients/
    ├── filterwall.py          # L1 钩子（business_filter + schema 校验 + drops 统计）
    ├── localbus.py            # 客户端内嵌的本地母线（SSE/JSON/ack/订阅周期）
    ├── mc_chat_client.py      # MC 聊天 → L1 → /trigger + 本地母线
    ├── email_client.py        # IMAP → L1（只看指定文件夹）→ /trigger + 本地母线
    ├── fake_sse_server.py     # 无 MC 时的 SSE 测试源（无 seq/state）
    ├── fake_bridge.py         # 带 /state.events.lastSeq 的可控假桥（游标自测）
    ├── selftest_bus.py        # 两个订阅者比对 event_id/seq
    ├── selftest_lifecycle.py  # gap / EOF / resume / quota（本地与远端各跑一遍）
    ├── selftest_l1.py         # 假 bridge + 假 IMAP 的 L1 自测
    ├── selftest_mc_cursor.py  # 游标端到端：基线/续传/重放/损坏/新会话/lookback
    ├── filters/
    │   └── README.md          # 0.6.0 起规则在服务端，旧 JSON 已删除（说明）
    ├── state/                 # 运行时状态（.gitignore，模板见 state/README.md）
    │   ├── dedupe.json        # 0.5.x 去重缓存（0.6.0 不再生成，可删）
    │   ├── email_cursor.json  # IMAP 游标（自动生成）
    │   └── mc_chat_cursor.json # MC 源侧游标（自动生成）
    ├── drops.log              # L1 跳过的消息（reason=self_message/…）
    ├── mc_chat_client.log
    └── email_client.log
```

---

## 15. 已知限制

1. **打断只能挂在工具返回上**：`transform_tool_result` 在每次工具调用返回时检查
   `event`；模型正在流式生成文本时无法插入，最迟下一次 `pre_llm_call`（本轮结束）
   才注入。没有工具调用的纯文本回合里，`event` 也只能等回合边界。
2. **插件队列上限 50**，突发超出会丢最旧。
3. **本地母线只在内存**：500 条环形缓冲，客户端重启即丢；要历史回 8791。
4. **token 是明文文件**：`~/.hermes/secrets/trigger.token`（600）。没有 TLS，
   局域网内是同网段明文传输，只适合家里的可信网络。
5. SQLite 单连接串行写入，适合低频事件（每秒几十条以内）；不是消息队列。
6. 首次运行（无游标文件）会从 `since=0` 开始，可能一次性拉到最多 50 条历史积压。
7. 邮件只取 header + 正文前 200 字；不解析附件，不处理文件夹改名。
   `BODY.PEEK[TEXT]` 对大附件邮件会多传一些字节。
8. SSE 重连不发送 `Last-Event-ID`，靠 URL 里的 `since` 恢复。
9. `--once` 只处理一个请求，不适合作生产守护方式（这是给自测用的）。
10. 母线的 `blocked_reason` 只在订阅结束那一刻有意义（条目随后会被清理），
    历史原因看客户端/服务端日志。
11. **订阅周期不自动续**：`expires_at` 到点就停投递。默认 TTL 1 小时 / 提前
    10 分钟提醒；worker 必须自己看 `renew_required` 并 `POST /renew`。插件
    `trigger-bus` 已经内置自动续期，其它 worker 要自己实现（第 7.6 节有伪代码）。
12. **过期的记录占位不投递**：24h 内 `GET /subscriptions` 仍会列出来，别把它当成
    "还有数据"；判断能不能收，只看 `expired`。
13. **MC 桥没有 bootId**：只能靠 `events.lastSeq` 变小判断游戏重启。若新会话在
    客户端重连前就超过上次的 `last_seq`，会被误判为同一会话（见 5.1 的边界说明）。

---

## 16. 故障排查

| 现象 | 检查 |
|---|---|
| `/health` 连不上 | `lsof -nP -iTCP:8791 -sTCP:LISTEN`；`launchctl print gui/$(id -u)/ai.hermes.trigger` |
| `401 unauthorized` | 请求头 `X-Trigger-Token` 是否等于 `~/.hermes/secrets/trigger.token` |
| `403 forbidden` | 绑定的是局域网地址，来源 IP 不在 `--allow-cidr` 里 |
| MC 客户端一直重连 | 桥是否在跑：`lsof -nP -iTCP:8765 -sTCP:LISTEN`；`clients/mc_chat_client.log` |
| 聊天没进来 | L1：`clients/drops.log` 是否有 `self_message`；L2：`GET /blocked?source=mc_chat`、`GET /health.rules`；`~/.hermes/trigger-rules.json` 是否命中 rate/dedupe |
| 重要消息没叫醒 | `GET /events?subscriber=hermes-main` 能看到 `event,notify`；确认发送方给了 `level=event`（缺省是 `info`，故意不唤醒） |
| 想看 info 但没看到 | worker 默认 `event,notify,info`；`GET /events?subscriber=worker-a&levels=event,notify,info` 显式拉；溪流只拿 `event,notify` |
| 规则改了没生效 | `GET /health` 的 `rules` / `rules_mtime` / `rules_error`；坏 JSON 会保留旧规则并打 WARNING |
| 收到 `event: gap` | 订阅者太慢，丢了 `from..to` 这段；带 `since` 重连或改用 `/events` 补 |
| 订阅莫名断开 | 日志 `reason=write_failures` / `client_eof` / `quota`；或 worker 10s 没读走数据 |
| `events` 一直为空但服务正常 | `subscription.expired` 是否为 `true`；`GET /subscriptions` 看 `expires_in`；`POST /renew` 续期 |
| 收到 `event: subscription` | 剩余 ≤ `renew_window`（默认 600s），客户端应 `POST /renew`，不是错误 |
| 收到 `event: expired` | 时钟到点/已退订；24h 内 `POST /renew` 可复活，之后只能重建订阅 |
| `/renew` 返回 410 | 记录 `closed` 或超过 24h 宽限，不可复活；重新 `/subscribe` 或 `/events` 建新记录 |
| 日志里没有 `RENEW` 行 | 插件是否加载 0.4.0；`tail -f ~/.hermes/plugins/trigger-bus/trigger-bus.log` |
| 邮件客户端退码 2 | `~/.hermes/secrets/email.env` 是否存在且四个字段齐全 |
| 邮件重复投递 | `clients/state/email_cursor.json` 是否可写；UIDVALIDITY 是否在变 |
| 事件重复 | 投递方是否每次生成新 `event_id`；默认 id 含 `ts`，同文本不同时间不算重复 |
| 看服务日志 | `tail -f trigger-server.log`（5 MB 自动滚动） |
