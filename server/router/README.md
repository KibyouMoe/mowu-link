# mowu-router 0.4.0

## 模型与思考力度命令

`POST /commands` 使用 `X-Router-Token`，JSON 包含 `worker`、稳定 UUID `message_id`、
当前 `thread_id` 与原文 `text`。router 统一解析 `/model`、`/effort`，通过既有 SSE
`worker_command` 转发，响应由 `/commands/responses` 按 request_id、worker 和连接代次关联。
该控制通道不创建工单。

不带参数列目录，每页 10 条。`next`、`prev`、`page N` 翻页，其他参数按原生模型名或力度解析。
列表标记 `current`、`config`、`current,config`。设置命令读回原生会话后提交 worker 默认配置，
相同消息标识重试返回原回执。

各 worker 的 `stateDir/model-settings.local.json` 保存默认模型与默认力度。仅飞书 `/new`
使用它创建新会话。已有会话使用自己的原生设置。目录、力度与当前设置由各引擎适配层提供。
支持情况可查 worker health 的 `capabilities.modelSettings`。

## Worker 互聊与项目工单

普通聊天不建任务：`POST /messages`，请求包含稳定 UUID `message_id`、已注册的精确
`from` / `to`、`text`，可附 `task_id`、`reply_to` 和已上传的附件 ID 数组 `attachments`。
所有请求沿用 `X-Router-Token`；共享令牌内的身份属于受信客户端声明，不是独立的每 worker 鉴权。
首次保存 202、完全相同重试 200、同 ID 改内容 409；持久化后通过 SSE `worker_message` 投递。

`GET /messages?worker=NAME&after=0&limit=100` 查询未 ACK 队列，按 `next_after` / `has_more`
继续分页；`GET /messages/<UUID>` 查原消息。接收 Worker 持久接受后调用
`POST /messages/<UUID>/ack {"worker":"收件方精确名"}`。queued 是 router 已保存，accepted 是
Worker 已接受，均不表示模型已回答；回复显式携带 reply_to。断线重放不换 ID，不自动互相回复。

收件人为 `Hermes`（大小写不敏感）或 `溪流` 时，消息先持久化，再向 trigger 提交
`source=私聊`、`kind=peer.message`、`level=notify`，`event_id=peer:<seq>`。
私信只要求正文非空。超过 64 KiB 的 UTF-8 正文在同一事务内分块存储，读取时按块序号还原。
溪流每段最多收 2000 个字符，多段时每段带 1/N 和原 message_id。收件端保留全文、换行与空白。
`GET /messages?task_id=t-…&after=0&limit=100` 按发送次序返回关联私信。
此路径返回 HTTP 200。trigger 接受后写入 `acked_at`，消息及 GET 查询的 `status=delivered`
表示已交给溪流的 notify 通道。POST 的顶层发送回执沿用 `status=accepted`，嵌套 message
显示 delivered，以兼容现有 Worker。普通 Worker 的 202/queued 和自主 ACK 流程照原协议执行。
trigger 不可用时返回 200/queued 并记 WARNING。使用相同 UUID、相同正文重试可补投唤醒，
单段沿用 `peer:<seq>`，多段用 `peer:<seq>:<part>`，分段投递进度持久化。已写 delivery ACK 的重试直接读回原记录。

自检运行 `ROUTER_TEST_PORT=<空闲端口> python3 server/router/selftest_router.py`。
`peer secretary wake`、`peer concurrent retries`、`peer exact retry repairs a failed wake` 与
`peer root delivery survives router restart` 应为 PASS，最终 TOTAL 全通过。

### 任务生命周期

任务有固定的执行人。正在执行的清单、等待派发的 FIFO 和归档可分别核对。
Worker 每五秒报告完整的 `bBusy` 与 `TaskID`。回合结束后继续持有原编号。
Router 在连续空闲满配置时间后通过 SSE 请求总结。Worker 调用原有的
`submit_deliverable` 工具。Router 原子保存总结、清除占用和归档，并持久记录待发送的
TaskInfo 与总结包。触发器恢复后会补送同一事件。溪流在 Router 之外验收。

通过 `--lifecycle-config <path>` 或 `ROUTER_LIFECYCLE_CONFIG` 指定 JSON 配置。
默认值为 `{"idle_task_end_seconds":30,"summary_request_max_attempts":3}`。
第一次请求计入总数。每次重试都需要重新观察到完整的连续空闲时段。
取消确认复用同一请求通道。详见[定稿规格](../../docs/TASK-LIFECYCLE.md)。

### Worker 显示名

在服务机器的 `~/.hermes/router-worker-names.json` 配置名称，Router 每次读取使用当前文件。
测试环境可通过 `ROUTER_WORKER_NAMES_FILE` 指定隔离配置。文件缺失、无效或没有对应键时，
显示名回退到原 ID。

```json
{"OguriCap":"小栗帽","TamamoCross":"玉藻十字","qicao-codexworker":"气槽","Rudolf":"鲁道夫象征","Hermes":"溪流"}
```

`GET /workers` 的 `name` 保留英文 ID，新增 `display_name`。peer、任务结果、审批与通知的
可见 actor/正文使用显示名，结构化路由 payload 保留英文 ID。
peer 的 `from`、`to`、inbox 与 ACK 接受配置中的唯一中文名。内部存储与 GET/SSE 统一为英文 ID。
POST 发送回执回显请求中的 from/to，别名发生转换时附 sender_id/recipient_id，兼容现有发送方校验。
同一 UUID 重试仍须使用原请求正文。重名显示名无法唯一解析时，调用方使用英文 ID。

明确项目才 `POST /tasks`：原字段外支持 `message_id` UUID 幂等和 `execution_mode:"goal"`
（由 Worker 解释）；任务 ID 与幂等信息在同一 SQLite 行原子保存，相同重试不再派单。
同一项目的补充、纠正和验收应沿原工单；普通聊天走 /messages。
Worker 在原会话检查/建立原生目标；router 不伪造引擎目标工具成功。
没有默认墙钟期限；`deadline_s` 只用作展示，不参与任何执行或恢复判断。

这些接口需正常加载此版服务后生效；只更新文件不等于正在运行的 router 已更新。

`/health.capabilities` 声明 `workOrders: idempotent-goal-v1` 和 `peerMessages: durable-ack-v1`。
新版客户端先核对工单协议，再 POST /tasks；旧版服务缺少该声明时拒绝提交，防止忽略 UUID 而重复派单。

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
| 等待派发 | 每个 Worker 一个持久 FIFO |
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
| POST | `/heartbeat` | `{name,bBusy,TaskID,seq,stream}` | `{ok,name,online,busy}` |
| POST | `/tasks/{id}/deliverable` | `{request_id,worker,summary,summary_short?,artifacts,evidence,risks}` | 200 archived 或 duplicate，400 字段错误，409 请求不匹配 |
| POST | `/result` | `{name, task_id, status, summary, summary_short?, artifacts?, evidence?, outcome?, error_code?}` | 旧接口；返回 409 及专用报送路径 |
| POST | `/notice` | `{worker, text, kind?, urgency?, task_id?, status?}` | `{ok, trigger_id, level, duplicate}` 或 `{ok:false, error}` |
| GET | `/subscriptions/pending?worker=&limit=` | — | `{ok, worker, count, events[], acked}` |

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

旧 `task.result` 通知返回 409；任务总结仅通过成果工具发送一次。


### Hermes 侧

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/workers` | `{ok, count, workers:[{name, online, busy, queued, last_seen, last_progress_at, last_progress_at_iso, version, state, task_id, ...}]}` |
| POST | `/tasks` | `{worker, goal, context?, workspace?, urgency?, deadline_s?, queue_if_offline?}` |
| POST | `/tasks/{id}/cancel` | 撤回队列中的单，或向在办单的执行人请求取消 |
| GET | `/tasks/{id}` | `{ok, task_id, task}` |
| GET | `/tasks?worker=&active=&limit=` | 默认 100，最大 500 |
| GET | `/health` | `{ok, version, workers, tasks, active_tasks, online, subscriptions, ...}` |

`last_progress_at` 来自最近一次 `POST /heartbeat` 的 `progress_at`（epoch 秒），
看门狗用它判断 worker 是否卡死；`last_seen` 是任意请求/心跳的到达时间，
两者语义不同，不要混用。

### WorkerStatus v2

注册返回 `task_lifecycle_v2=true`。流响应的 `X-Router-Generation` 标识当前连接。
每条心跳携带完整状态、连接标识和递增序号。Router 在序号断档时主动索取整段采样，
拒收过期连接的数据。`bBusy=true, TaskID=0` 属正常形态（人类在跟它聊天），**不再告警**。

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

### 审批通道（worker 提问，人回答）

worker 在执行命令/改文件前需要人类点头，但子进程的 JSON-RPC 请求不能直接
转给飞书。Router 只存一张审批单：worker 提申请 → trigger 推一条
`approval.request` 通知（level `event`）→ 溪流批准/拒绝 → worker 轮询结果。

| 方法 | 路径 | 请求 / 说明 |
|---|---|---|
| POST | `/approvals` | `{worker, taskId?/task_id?, kind, title, detail?, options?, ttl_s?}` → `{ok, approval_id, status:"pending", expires_at, remaining_s, notified}` |
| GET | `/approvals/{id}` | `{ok, approval:{approval_id, worker, task_id, kind, title, detail, options, status, decision, note, created_at_iso, expires_at_iso, remaining_s, notified}}` |
| GET | `/approvals?worker=&status=&limit=` | 列表，默认 50、最大 200；`status` ∈ `pending/approved/denied/expired` |
| POST | `/approvals/{id}/decide` | `{decision:"approved"\|"denied", note?}` → `{ok, approval_id, status, decision, note, decided_at_iso}` |

语义：

- `status` 只有 `pending` / `approved` / `denied` / `expired`。`pending` 超过
  `ttl_s`（默认 **600 秒**，硬上限也是 600）自动变 `expired`：读的时候懒判定，
  服务端还有 30 秒一次的清扫线程，所以没人轮询也会过期。
- 已决定/已过期的单子再 `decide` → `409 already_decided` / `409 approval_expired`；
  未知 id → `404 unknown_approval`。
- 提交时写一条 trigger 事件：`kind="approval.request"`、`level="event"`、
  `event_id="approval:<approval_id>"`、`payload.approval_id`。trigger 挂掉时
  审批单仍然创建成功（`notified:false`），**不返回 5xx**。
- Router 不执行任何命令，也不校验 worker 是否已注册：它只是传话。

---

## 三、派活与取消

### Worker 澄清问题

`GET /questions/{worker}/{questionId}` 和 `POST /questions/{worker}/{questionId}/answer`
使用 `X-Router-Token`，经 worker 已有鉴权 SSE 通道访问其原问题状态，支持不同机器。
GET 返回完整题面、选项、`status`、`answeredBy`、`nativePanelSync`、`cardUpdated` 和 `answerRequest`。
将 `answerRequest.answers` 换成逐题选项原文或自由文本，再 POST 到同一 URL 的 `/answer`。
全部跳过时使用 `action:"skip"`，保留原 `actor`、`threadId`、`nonce`。
`actor` 使用 `hermes` 或 `Hermes`，原任务号只作关联元数据；异步原生问题在任务/回合结束后继续有效。
每个问题按 worker、原生问题 ID、会话和独立 nonce 绑定；错用另一 worker 的 nonce 返回 403。
离线返回 503，请求等待 worker 响应最多 5 秒，超时 504，可原样重试。
重试答案沿用持久问题状态和原生消息 UUID，原生投递不明时保留待核实状态。
此通道不创建工单，也不占工单等待位；worker 本机 `/bridge/questions/{id}` 保持兼容。

待派发任务按接受顺序保存在 SQLite —— **排队只发生在 Router**，worker 手上恒为一个 TaskID。
执行人 TaskID 为零时立刻投递；非零时排队等它清 0。`bBusy` 不参与派单判断。

取消只看 TaskID：对上就立刻下发、优先处理，不等空闲、不等回合结束；对不上直接归档。
没有"等这一轮跑完再确认"这种中间态。取消发出后同样按空闲节奏重发，次数用尽归档 + 报警。
详见[定稿规格](../../docs/TASK-LIFECYCLE.md)。

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
       "task_info":{"TaskID":"t-1a2b3c4d5e","TaskName":"示例","TaskDesc":"...","TaskOwner":"波旁"}}
```

- **连接本身就是在线的唯一依据**：不靠 heartbeat 判断存活。
- 心跳只是注释行 `: ping`（默认 15s），客户端忽略即可。
- 一个名字同时只有一条流；新流会替换旧流（旧的发 `replaced` 后关闭）。
- 断开（EOF/RST/写失败）→ `online=0`，**名字与积压保留**；
  **该 worker 的全部订阅立即删除并通知 trigger**（订阅不留存）。
- 重连后先从队列补推积压，再继续实时推。
- 服务端会主动探测对端 EOF（`select` + `MSG_PEEK`），通常 1s 内标记 offline。

---

## 五、成果与采样接口

| 调用 | 作用 |
|---|---|
| SSE `deliverable_request` | 带 task_id、request_id、purpose、attempt、issued_at 与固定提示。purpose 为 summary 或 cancel |
| POST `/tasks/{id}/deliverable` | 原线程工具确认，成功后归档。摘要 ≤200 字。重复应答幂等，迟到应答留痕 |
| POST `/workers/{name}/request-status` | 立即通过当前连接索取状态序列 |
| POST `/workers/{name}/status-history` | 主动索取可从本连接第 1 个采样开始分批读回整串；断序补取自确认点继续，核对连接代际与连续序号。断档会重置空闲计时 |
| GET `/tasks?active=true` | 当前运行清单，移除 active 过滤可同时查看排队与归档 |

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

## 七、自测

在独立端口运行 `ROUTER_TEST_PORT=<空闲端口> python3 server/router/selftest_router.py`。
在 `server/router` 下用 Hermes 的 Python 执行 `python -m unittest discover -p 'selftest_*.py' -v`，
覆盖生命周期、并发回执、取消、状态序列、长私信与附件流转。

## 八、已知边界 / 设计选择

- **at-least-once**：断线重连可重放正在执行的任务，worker 必须按 `task_id` 幂等；
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
