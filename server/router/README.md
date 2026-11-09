# mowu-router 0.3.0

`docs/ROUTER.md` 的 ①②③ 落地：worker 在线表、任务派发/收回、订阅表，
**订阅转发真的接上 trigger 了**（0.1.0 是 stub）；0.3.0 再加**聊天旁听**
（只存不发，不打扰 Hermes）。**④ worker SDK 不在本次范围**。

一句话：Router 只搬运事实（谁在线、谁忙、每个 worker 积压几条、谁在听什么、
刚才聊了啥），不懂能力、不懂任务语义；语义判断留在 Hermes。

产物：

```
server/router/server.py              服务本体（python stdlib + sqlite + fcntl）
server/router/selftest_router.py     真起进程、真 HTTP、真 SSE 的端到端自测
server/router/ai.hermes.router.plist LaunchAgent 模板（未 load）
server/router/README.md              本文件
```

---

## 一、启动

```bash
/usr/bin/python3 server/router/server.py
```

默认值（全部可用参数覆盖）：

| 项 | 默认值 |
|---|---|
| 监听 | `127.0.0.1:8790`（只允许 loopback） |
| 库 | `~/.hermes/router.db` |
| token | `~/.hermes/secrets/router.token`（不存在则生成 32 字节 hex，0600） |
| 单实例锁 | `~/.hermes/router.lock`（`fcntl.flock`，第二个进程直接退出 1） |
| 日志 | `~/.hermes/router.log`（5MB × 3 轮转，同时输出 stderr） |
| 结果回流 | `http://127.0.0.1:8791/trigger`，token 读 `~/.hermes/secrets/trigger.token` |
| 订阅转发 | 由 `--trigger-url` 推导 trigger 根地址（去掉结尾 `/trigger`） |
| 积压上限 | 每 worker 128（`--backlog-max`） |
| SSE 心跳 | `: ping` 每 15s（`--ping-interval`） |

验证：

```bash
curl -s http://127.0.0.1:8790/health
```

`/health` 是唯一不要求 token 的接口（同 trigger 的 `/health`），便于探活；
其余接口全部要求 `X-Router-Token`。

### LaunchAgent

`ai.hermes.router.plist` 只是模板，**没有 load**。是否常驻、何时启动由上层决定。
需要时再执行（本仓库不会自动做）：

```bash
cp server/router/ai.hermes.router.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.router.plist
```

---

## 二、接口速查

所有 JSON，UTF-8。除 `/health` 外都要 `X-Router-Token: <token>`。

### Worker 侧

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/register` | `{name, version?, note?}` | `{ok, name, queued, online, new}` |
| GET | `/stream?name=&version=` | SSE 长连接 | 见第四节 |
| POST | `/heartbeat` | `{name, state?, progress_at?, task_id?}` | `{ok, name, online, busy, queued, last_seen}` |
| POST | `/result` | `{name, task_id, status, summary, artifacts?, evidence?}` | `{ok, task_id, state, notified, dispatched_next}` |
| POST | `/notice` | `{worker, text, kind?, urgency?}` | `{ok, trigger_id, level}` 或 `{ok:false, error}` |
| GET | `/subscriptions/pending?worker=&limit=` | — | `{ok, worker, count, events[], acked}` |

- `status` 只接受 `done` / `failed`。
- 同一任务重复上报是幂等的：已终态的任务返回 `duplicate:true`，不重复 notify。
- `name` 必须与任务归属一致，否则 400 `worker_mismatch`。
- `/result` 会先落库、再尝试给 trigger 写一条 notify（3s 超时）。
  trigger 不可用时**不影响交活**，响应里 `notified:false`，日志留 WARNING；不重试。

### `POST /notice`：worker 往 Hermes 递话

```
{worker: "波旁", text: "缺一个 config 示例", kind?: "worker.notice",
 urgency?: "normal" | "urgent"}
```

转成一条 trigger 事件：

```
source   = "router"
kind     = body.kind（默认 "worker.notice"）
actor    = worker
level    = urgency == "urgent" ? "event" : "notify"
event_id = "notice:" + sha256(worker + "\n" + text + "\n" + 当前分钟)[:24]
```

同一 worker、同一 text 在同一分钟内重复提交会得到同一个 `event_id`，
由 trigger 侧去重（响应 `duplicate:true`，`trigger_id` 仍指向已有事件）。

成功：`200 {ok:true, trigger_id:<int>, level:"event"|"notify", event_id, duplicate}`
失败（trigger 挂了/没 token）：`200 {ok:false, error:"trigger_unavailable", detail}`，
**不抛 5xx 给 worker**，消息由 worker 自行决定是否重试。

### Hermes 侧

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/workers` | `{ok, count, workers:[{name, online, busy, queued, last_seen, last_progress_at, last_progress_at_iso, version, state, task_id, ...}]}` |
| POST | `/tasks` | `{worker, goal, context?, workspace?, urgency?, deadline_s?, queue_if_offline?}` |
| POST | `/tasks/{id}/cancel` | 取消排队中或运行中的任务 |
| GET | `/tasks/{id}` | `{ok, task_id, state, task}` |
| GET | `/tasks?worker=&state=&limit=` | 默认 100，最大 500 |
| GET | `/health` | `{ok, version, workers, tasks, tasks_by_state, online, subscriptions, ...}` |

`last_progress_at` 来自最近一次 `POST /heartbeat` 的 `progress_at`（epoch 秒），
看门狗用它判断 worker 是否卡死；`last_seen` 是任意请求/心跳的到达时间，
两者语义不同，不要混用。

### 订阅

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/subscriptions` | `{worker, source, kinds?}` → 201 `{ok, id, trigger_id, trigger_since, ...}`；trigger 失败 → 502 且回滚表 |
| GET | `/subscriptions?worker=&active=1` | 列表，`kinds` 为数组，带 `trigger_id` |
| DELETE | `/subscriptions/{id}` | 物理删行 + 关闭 trigger 侧订阅 |
| GET | `/subscriptions/pending?worker=&limit=` | 拉 trigger 队列、ACK、返回匹配事件 |

`kinds` 为空数组表示该 source 的全部 kind；支持列表或逗号分隔字符串。

### 聊天旁听（0.3.0 新增，只存不发）

Worker（或替它记录的进程）把双向聊天逐条抄给 Router。Router **只落库**：
不写 trigger、不发 notify、不叫醒 Hermes；Hermes 想看时自己来读。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat/log` | `{worker, direction:"in"\|"out", text, session_id?, ts?, message_id?, meta?}` → `{ok, id, duplicate}` |
| GET | `/chat/{worker}/messages?since=&limit=50&direction=` | 按 id 升序；字段 `id/worker/direction/text/session_id/ts/message_id` |
| GET | `/chat/{worker}/summary?minutes=60` | `{ok, worker, minutes, since, count, in, out, last}` |

表结构：

```sql
chat_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, worker TEXT, direction TEXT,
  text TEXT, session_id TEXT, ts REAL, message_id TEXT, meta TEXT
)
```

语义：

- **幂等**：同 `(worker, message_id, direction)` 重复写入返回原 `id` +
  `duplicate:true`，不新增行。`message_id` 为空则不参与去重（每次都是新行）；
  同一个 `message_id` 换个方向（in ↔ out）算另一条。
- **`ts`** 省略时用服务端当前时间；接受 epoch 秒或 ISO-8601 字符串。
- **`limit`** 1–500，默认 50。不带 `since` 时返回**最近** `limit` 条（仍按 id
  升序），适合“看刚才聊了啥”；带 `since` 时返回 `id > since` 的**最早**
  `limit` 条，适合游标轮询、不丢消息。`since` 也接受 epoch/ISO 时间
  （≥1e9 或带小数点视为时间戳），此时语义是 `ts >= since`。
- **`direction`** 只接受 `in`（训练员/对方说的）或 `out`（worker 说的）。
- **`summary`** 的 `minutes` 默认 60；`in`/`out` 是窗口内条数，`last` 是窗口内
  最后一条完整消息，没有则 `null`、计数为 0。
- **不校验 worker 是否已注册**，也不写 `workers` 表、不改 `last_seen`：
  记录聊天不等于“这个 worker 在线”。
- `meta` 原样落库（对象/数组序列化为 JSON 字符串，字符串照存，≤16000 字符）；
  `messages` 响应不返回 `meta`，需要时直接查库。

---

## 三、派活：三态与状态机

```
worker 不存在                          → 400 unknown_worker（Router 不猜）
worker 在线且空闲                      → 200 {task_id, state:"running", dispatched:true}
worker 在线但忙                        → 202 {task_id, state:"queued", queued:true, queued_ahead:N}
worker 离线 + queue_if_offline=false   → 409 {error:"worker_offline", worker}
worker 离线 + queue_if_offline=true    → 202 {task_id, queued:true, queued_ahead:N}
排队/积压已达 128                      → 429 {error:"backlog_full", queued:128, limit:128}
```

三态是固定的：**200 = 现在就推下去了**，**202 = 收下但在排队**，
**409 = 不在线且没让堆**。`ROUTER.md` 的语义表只列了「在线空闲 → 200」和
「离线 + `queue_if_offline` → 202 / 409」；「在线但忙」原文没给状态码，这里与
离线积压统一归入 queued 的 202，body 里再用 `dispatched` / `queued` 区分。
`queued_ahead` 包含正在跑的那条（它也在前面）。

`queue_if_offline` 缺省为 `false`，必须是 JSON 布尔；是否堆任务由 Hermes 决定。

任务状态：

```
queued ──dispatch──> running ──/result done──> done
   │                    │      └─/result failed─> failed
   │                    └──cancel──> cancelled
   └──────cancel──────> cancelled
```

- 正常派发（`POST /tasks` 且空闲、交活后续推）每次只推一条，因此同一时刻通常
  只有一条 `running`；`busy` 即「有 running」。
- 离线积压在上线时**按创建顺序一次性推**（`flush_backlog`）；这一次可能同时
  有多条 `running`（`ROUTER.md`「上线时按顺序一次性推」的直译），全部交回后
  才继续推后续排队任务。
- 断线时，该 worker 的 `running` 任务回到 `queued`；重连后会再次下发。
  语义是 **at-least-once**，worker 需按 `task_id` 幂等。
- 取消运行中的任务会立即释放 `busy` 并尝试派下一条；worker 之后迟到的
  `/result` 会被忽略（任务已 `cancelled`，返回 `duplicate:true`）。
- `urgency` 只记录并透传给 worker（`urgent` / `normal`），排队仍是 FIFO；
  `deadline_s` 落库并算出 `deadline_at`，0.3.0 仍不做超时回收。

---

## 四、SSE 协议

```
GET /stream?name=%E6%B3%A2%E6%97%81&version=fake-1
X-Router-Token: ...
```

响应 `text/event-stream`：

```
: connected

: ping

event: task
data: {"type":"task","task_id":"t-1a2b3c4d5e","worker":"波旁","goal":"...",
       "context":null,"workspace":null,"urgency":"normal","state":"running",
       "created_at":1789...,"started_at":...,"deadline_s":null,"deadline_at":null,
       "result":null}
```

- **连接本身就是在线的唯一依据**：不靠 heartbeat 判断存活。
- 心跳只是注释行 `: ping`（默认 15s），客户端忽略即可。
- 一个名字同时只有一条流；新流会替换旧流（旧的发 `replaced` 后关闭）。
- 断开（EOF/RST/写失败）→ `online=0`，**名字与积压保留**；
  **该 worker 的全部订阅立即删除并通知 trigger**（订阅不留存）。
- 重连后先从队列补推积压，再继续实时推。
- 服务端会主动探测对端 EOF（`select` + `MSG_PEEK`），通常 1s 内标记 offline。

---

## 五、结果回流

```
Worker → Router : POST /result {name, task_id, status, summary, artifacts, evidence}
Router → trigger: POST /trigger
  {
    "source": "router",
    "kind": "task.result",
    "level": "notify",
    "text": "t-88 完成：…",
    "actor": "波旁",
    "event_id": "t-88:done",
    "payload": {"task_id": "t-88", "worker": "波旁", "status": "done"}
  }
  Header: X-Trigger-Token: <~/.hermes/secrets/trigger.token>
```

Router 不直接跟 Hermes 说话，只写一条 notify。`event_id = <task_id>:<status>`，
trigger 侧按 `event_id` 去重，重试安全。

---

## 六、订阅转发：真的接上了 trigger

### trigger 0.7.x 的实际形状

trigger 没有 `POST /subscribe`：它是**游标总线**。

- 订阅记录在第一次 `GET /events?subscriber=<name>&since=<id>` 时惰性创建；
- 消息留在库里，`POST /ack {subscriber, up_to_id}` 推进游标（写 receipts），
  之后 `GET /events` 不再返回；
- 订阅有租约（默认 3600s），进入续租窗口要用 `POST /renew {name}`；
- `DELETE /subscriptions?name=` 把记录置为 `closed`。**closed 的名字不能复活**
  （`/renew` 返回 410），所以 Router 每次重声明都要换一个名字。

Router 的桥接完全用这些公开接口，不改 trigger 代码。

### 注册（`POST /subscriptions`）

1. 先写 `subscriptions` 表（`trigger_id` 为空）；
2. 读 `GET /bus/health` 的 `last_id` 作为基线 `trigger_since`，保证**不回放历史**；
3. `GET /events?subscriber=router:<worker>&since=<基线>&limit=1` 创建/触碰订阅，
   校验返回的 `subscription.status == "active"`；
4. 把实际订阅名写入 `subscriptions.trigger_id`，并在 `subscription_cursors`
   记录游标。

命名规则：

- 该 worker 当前只有这一条订阅 → `router:<worker>`（与 `ROUTER.md` 一致）；
- 已有其它订阅，或 `router:<worker>` 在 trigger 侧已 closed（断线后重声明）→
  `router:<worker>:<subscriptions.id>`，每条订阅一个独立队列。

trigger 任一步失败 → **删掉刚插入的行**，返回 `502 {ok:false,
error:"trigger_unavailable", detail}`。不会留下“本地有、trigger 没有”的幽灵订阅。

### 拉取（`GET /subscriptions/pending`）

```
GET /subscriptions/pending?worker=波旁&limit=50
→ {ok, worker, count, events:[{id, event_id, source, kind, level, text,
                              actor, ts, payload, subscription_id, trigger_id}],
   acked}
```

对该 worker 的每条订阅：

1. 用本地游标 `since` 调 trigger `GET /events?subscriber=<trigger_id>&since=&limit=`；
2. 按 `source` 与 `kinds` 过滤（trigger 不认 source/kind，过滤在 Router 这层）；
3. 对**本次取到的最大 id** 调 `POST /ack`，成功后推进本地游标。
   被过滤掉的事件也一并 ACK——订阅不留存，没匹配上的不补发；
4. 租约快到期（`renew_required`）就顺手 `POST /renew`。

幂等语义是 **at-least-once**：ACK 失败时本地游标不前进，下次会重复投递。

### 删除 / 断线 / 重启

- `DELETE /subscriptions/{id}`：先 `DELETE /subscriptions?name=` 关 trigger 侧，
  再物理删本地行与游标。trigger 关不掉时仍删本地行，响应 `trigger_closed:false`
  （worker 的“不听”意图优先）。
- **SSE 断开**：立刻删该 worker 全部订阅并逐条关闭 trigger 订阅；不建积压。
- **重连不自动重建**：worker 按自己的初始化流程重新 `POST /subscriptions`。
- **Router 进程启动**：所有 worker 都不在线，启动时清空全部订阅并关闭 trigger
  侧记录（`purge_all_subscriptions`）。

---

## 七、与 `docs/ROUTER.md` 对应关系

| ROUTER.md | 状态 | 实现位置 |
|---|---|---|
| ① workers：register / heartbeat / GET /workers | 完成 | `RouterStore` workers 段 + `_register` / `_heartbeat` / `_workers` |
| ① 连接不落库，SSE 即在线 | 完成 | `RouterServer.conns`（内存）+ `attach` / `detach` |
| ② tasks：派活 / 队列 / 三态 | 完成 | `_task_create` + `claim_next_queued` + `flush_backlog` |
| ② result → 状态迁移 → notify | 完成 | `_result` + `TriggerNotifier.notify_result` |
| ② cancel / get / list | 完成 | `_task_cancel` / `_task_get` / `_tasks_list` |
| ③ subscriptions 表 + CRUD | 完成 | `subscriptions` 表 + handler |
| ③ trigger 注册 / 删除 / 拉取 / ACK | 完成 | `TriggerSubscriptionBridge` + `subscription_cursors` |
| ③ 断线删订阅、订阅不留存 | 完成 | `RouterServer.detach` → `purge_subscriptions` |
| ④ worker SDK | 未做（本次范围外） | — |
| §二 `queue_if_offline` 语义 | 完成 | 见第三节 |
| §二 积压上限 128 | 完成 | `--backlog-max`，超限 429 |
| §五 结果回流 | 完成 | 见第五节 |
| worker → Hermes 递话 | 0.2.0 新增 | `POST /notice` |
| ⑨ 聊天旁听（只存不发） | 0.3.0 新增 | `chat_messages` 表 + `_chat_log` / `_chat_messages` / `_chat_summary` |

---

## 八、自测

```bash
/usr/bin/python3 server/router/selftest_router.py
# 端口被占时：
ROUTER_TEST_PORT=8799 /usr/bin/python3 server/router/selftest_router.py
```

它**不是单元测试**：会真的启动两个进程（一个临时 trigger 实例 + 一个
监听 `127.0.0.1:8790` 的 router），用真实 HTTP 和真实 SSE 跑完整链路。
临时库/token/锁/日志都在 `tempfile.mkdtemp()` 里，不碰生产
`~/.hermes/router.db` 和 `~/.hermes/trigger.db`。

覆盖（当前 **56/56 PASS**）：

1. token 生成（64 hex，0600）、`/health` 形状、无 token → 401、单实例 flock
2. register → SSE 上线 → 心跳注释行 → 空闲立即派活（200 dispatched）
3. `last_progress_at` 来自 heartbeat 的 `progress_at`
4. 交活 → `done` → trigger 库出现 `source=router kind=task.result level=notify`
5. 断线 → offline（名字保留）→ 409 → `queue_if_offline` 202 →
   重连按顺序补推两条积压 → 交活
6. 忙时第二条排队（不提前推）→ 第一条交活后第二条自动下发
7. 取消排队 / 运行中任务；迟到 result 被忽略
8. 离线积压 128 条全部 202，第 129 条 429 `backlog_full`
9. 订阅：201 → `trigger_id` → trigger 侧 active → pending 拉到事件 →
   第二次 pending 为空（ACK 生效）→ kinds 过滤 → DELETE 后 trigger closed
10. `POST /notice`：normal → `level=notify`；urgent → `level=event`；
    同分钟重复 → 同一个 `event_id`、`duplicate:true`、trigger 只有一行
11. 删除后重声明 → 新 trigger 名（`router:<worker>:<id>`）→ 新队列可投递
12. SSE 断开 → 本地订阅清空 + trigger 订阅 closed；重连不自动重建
13. trigger 挂掉：`POST /subscriptions` → 502 且表回滚；
    `POST /notice` → 200 `ok:false`（不是 5xx）
14. 最终 `/health` 计数、trigger notify 条数、`GET /tasks?limit=500` 取回 128 条
15. 聊天旁听：写入/读回字段一致（含 `meta` 落库）→ 同
    `(worker, message_id, direction)` 重复 `duplicate:true` 且不新增 →
    同 `message_id` 换方向是新行 → `limit` / `since`（id 与 epoch）/
    `direction` 过滤 → `summary` 60/180 分钟 in/out 计数与 `last`

---

## 九、已知边界 / 设计选择

- **at-least-once**：断线重连会把 `running` 任务重发，worker 必须按 `task_id` 幂等；
  pending 的 ACK 失败也会重复投递。
- **一个名字一条流**：新连接替换旧连接；不做多设备同名的负载均衡。
- **没有 per-worker 鉴权**：0.3.0 只有一把共享 `router.token`（`ROUTER.md` §八
  的「首次注册发 token」仍待定）。
- **urgency 不抢队**：只透传，排队 FIFO。
- **deadline_s 不触发回收**：只记录。
- **notify 不重试**：trigger 挂了就丢这一条（日志有 WARNING，响应 `notified:false`）。
- **trigger 订阅名不可复用**：closed 是终态，重声明自动换 `router:<worker>:<id>`；
  trigger 侧会留下 closed 记录（trigger 自己的审计行，不删）。
- **订阅租约**：trigger 默认 3600s；只有 `pending` 被调用时才会续租。worker 应
  至少每分钟拉一次 pending，否则超过租约后 trigger 停止投递（Router 响应里的
  `expired` 会点名）。
- **ACK 的 receipts 成本**：trigger 的 `/ack` 会为区间内每条事件写 receipt，
  新订阅第一次 ACK 会覆盖基线之后的事件；这是 trigger 0.7.x 的既有语义。
- **chat 只存不发**：`chat_messages` 不写 trigger、不生成 notify；Hermes 不读
  就没有任何人被通知到。
- **chat 没有保留策略**：0.3.0 不做 TTL/自动清理，`(worker, ts)` 索引留给后续
  按时间窗清理。
- **SQLite 单连接 + 进程内锁**：够 loopback 小流量用；多进程写由 flock 挡住。
