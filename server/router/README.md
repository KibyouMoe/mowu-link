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

### 任务结束、成果与验收

注册回执的 `idle_probe=true` 表示启用收工探测。Router 默认每 60 秒检查各 Worker，连接变化、状态翻转和任务派发也会触发检查。当前连接上的新鲜心跳显示连续空闲 120 秒，且 Worker 仍有 running 任务时，Router 发出一次成果询问。断线、换连接、心跳间隔超过 90 秒或重新变忙会中断连续空闲计时。`GET /workers` 的 `watchdog` 给出连接、活动、任务、检查时间、检查次数和询问次数。

Router 通过 SSE 发送 `deliverable_request {request_id, task_id, template, issued_at}`。一个任务只有一个待答 UUID。5 分钟无应答且仍可接收时重发同一请求；再过 5 分钟仍无回执，标记 `deliverable_missing` 并给溪流一条 Event。断线期间保留请求；任务仍在执行态时可以晚交。每次新请求的 `issued_at` 是首次发问的 Unix 秒时间戳。

Worker 在原线程回答，通过 `POST /tasks/{task_id}/deliverable` 平铺提交 `request_id, worker, summary, artifacts, evidence, risks`，另可带 `summary_short`。两个摘要各限 200 个 Unicode 字符，后三项为字符串数组。超长 HTTP 400 会给出实际长度，需沿用原 UUID 重写。请求 ID 错误返回 409，Worker 归属错误返回 400。同一 UUID 首次接收时持久化成果并转 `reviewing`，向溪流推送一次；后续提交返回 `duplicate=true`。私信可带 task_id 关联补充材料。questions 继续处理互动提问。

溪流在任务详情带验收凭据可查看超长原稿。派单 context 可带 `card_id/board_id`，任务列表和成果通知原样回传。

溪流通过 `POST /tasks/{id}/verdict` 提交 `status=done|failed, reason, evidence[]`，除 Router 令牌外须提供 `X-Review-Token`（与服务令牌相邻的 `.review` 文件，仅交给验收方）。决定记入 `review_state/verdict`。30 分钟未验收提醒一次，报送成果与超时均不代替验收。

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
（默认且唯一模式）；任务 ID 与幂等信息在同一 SQLite 行原子保存，相同重试不再派单。
同一项目的补充、纠正和验收应沿原工单；普通聊天走 /messages。
Worker 在原会话检查/建立原生目标；router 不伪造引擎目标工具成功。
没有默认墙钟期限，只有显式 deadline_s 才建立 deadline_at，重连保留该原始时点。

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
| 积压上限 | 每 worker 0（`--backlog-max`，接受非负整数） |
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
| POST | `/heartbeat` | `{name, state?, progress_at?, task_id?, turn_views?}` | `{ok, name, online, busy, queued, last_seen}` |
| POST | `/tasks/{id}/deliverable` | `{request_id,worker,summary,summary_short?,artifacts,evidence,risks}` | 200 reviewing 或 duplicate，400 字段错误，409 请求不匹配 |
| POST | `/result` | `{name, task_id, status, summary, summary_short?, artifacts?, evidence?, outcome?, error_code?}` | 旧接口；返回 409 及专用报送路径 |
| POST | `/result/responses` | `{request_id, worker, status, body}` | 旧在途观察器请求的兼容回执 |
| POST | `/notice` | `{worker, text, kind?, urgency?, task_id?, status?}` | `{ok, trigger_id, level, duplicate}` 或 `{ok:false, error}` |
| GET | `/subscriptions/pending?worker=&limit=` | — | `{ok, worker, count, events[], acked}` |

- 验收接口 `/tasks/{id}/verdict` 的 status 接受 `done` 或 `failed`。重复提交已接收的成果返回 `duplicate:true`。
- 任务的运行、暂停、取消仍由执行协议管理。成果回执和验收分别走 `/deliverable` 与 `/verdict`，详情见“任务结束、成果与验收”。
- 已持久化的历史成果流程可继续使用原格式完成报送。

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

#### 任务终态通知

worker 交活时携带 `kind="task.result"`、`task_id` 和 `status`。

```json
{"worker":"OguriCap","kind":"task.result","task_id":"t-example","status":"done","text":"已完成并验证"}
```

| 归一化终态 | trigger source | level |
|---|---|---|
| `done` | `任务完成` | `event` |
| `failed` | `任务失败` | `event` |
| `cancelled` | `任务取消` | `notify` |

终态级别与 `urgency` 无关。`event` 沿用 trigger 的打断路径；
重试回执记录首次实际投递级别。

状态去除首尾空白并转小写，`canceled` 归一为 `cancelled`。
省略 `status` 时采用该工单的已存终态，运行中的工单默认提交 done 候选。
完成通知要求真实 `task_id` 与归属 worker；运行中的 done/failed 进入同一收口闸门。
显式非终态通知继续按普通通知处理。cancelled 候选等待原取消确认。

`POST /notice` 与内部 `notify_result()` 共用 SQLite `task_notice_dedup`，
键为 `(task_id, 归一化终态)`，trigger `event_id` 固定为 `<task_id>:<终态>`。
首个被 trigger 接受的通知保留原文及级别，后续重复返回原 `trigger_id` 与
`duplicate:true`，记录 `notice dedup task=… state=…`。
同一工单的不同终态分别投递。任务终态键与文本内容无关。

回执仅在 trigger 接受后写入，trigger 不可用时返回 `ok:false` 并允许重试。
稳定 `event_id` 也让 trigger 已接受、响应丢失的重试命中原事件。
Router 重启保留去重记录，每次读写清理满 24 小时的记录，并在写入后只保留
最近 500 条。普通通知沿用上述分钟去重，私聊沿用 `source="私聊"`。

### Hermes 侧

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/workers` | `{ok, count, workers:[{name, online, busy, queued, last_seen, last_progress_at, last_progress_at_iso, version, state, task_id, ...}]}` |
| POST | `/tasks` | `{worker, goal, context?, workspace?, urgency?, deadline_s?, queue_if_offline?}` |
| POST | `/tasks/{id}/cancel` | 取消排队、运行或暂停中的任务 |
| GET | `/tasks/{id}` | `{ok, task_id, state, task}` |
| GET | `/tasks?worker=&state=&limit=` | 默认 100，最大 500 |
| GET | `/health` | `{ok, version, workers, tasks, tasks_by_state, online, subscriptions, ...}` |

`last_progress_at` 来自最近一次 `POST /heartbeat` 的 `progress_at`（epoch 秒），
看门狗用它判断 worker 是否卡死；`last_seen` 是任意请求/心跳的到达时间，
两者语义不同，不要混用。

### 两层状态协议 v2

- `/register` 回 `state_layers: 2`。支持的 Worker 用 `/stream?state_layers=2` 接收
  `X-Router-Generation`，在心跳同时上报 `connection_generation` 和 `turn_views`。
  Router 拒绝旧连接，对同一连接保留最新采样序号。投递与结算由任务层决定。
- 新派单带 `execution_epoch`。断开后保留该工单，重连用 `task_recovery` 请求核查原回合。
  切换到 v2 时，已投递但仍排在队首的旧工单也先核查。首次派发仍用 `task`。
- Worker 调用 `POST /tasks/{id}/execution`，传 `name`、`connection_generation`、
  `expected_epoch` 和 `state`。暂停附 `pause_reason`，可取
  `session_unavailable`、`goal_conflict`、`resume_unconfirmed`、`recovery_expired`。
  确认恢复后用 `running`、新 `execution_epoch` 和 `evidence`（线程、回合、投递 ID）。
  重复回执幂等，旧代际、已取消或终态拒绝变更。新派单遇到暂停返回 409 和原因。
- `finished_execution_epoch` 心跳仅记录活动。收工探测使用连续空闲时长。
  旧 Worker 的结果仍可保存为候选；升级后经同任务恢复及原生核对可继续处理。

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

## 三、派活与状态机

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

默认 `--backlog-max 1`，每个 worker 同时投递一张工单。该配置控制等待容量。

```
worker 不存在                          → 400 unknown_worker
worker 在线、任务位空闲、既有队列为空    → 200 {task_id, state:"running", dispatched:true}
已有 running 或未到期的 cancelling      → 409 {error:"worker_busy", task_id, turn_source, retry_when}
已有 paused                             → 409 {error:"worker_paused", task_id}
已有一张等待单                          → 409 {error:"worker_busy", task_id, turn_source, retry_when}
worker 离线 + queue_if_offline=false    → 409 worker_offline
worker 离线 + queue_if_offline=true     → 200 queued（仍限一个等待位）
```

`worker_busy` 的 `task_id` 优先指向运行或取消中的真实工单，其次指向最早的等待单。
`retry_when` 保留旧值 `current_task_accepted_and_worker_idle`，实际重试条件由任务占用决定。
拒收的创建 UUID 不占位，可原样重试。已接受工单的同 UUID、同请求重试返回原工单
及 `duplicate:true`，包括排队、过期失败和终态；改请求内容返回 `message_id_conflict`。

等待单 TTL 默认 **21600 秒（6 小时）**，服务可用 `--queue-ttl-s 21600` 调整，
单次 `POST /tasks` 可用正数 `queue_ttl_s` 覆盖。`GET /tasks/{id}` 返回 `queue_expires_at`。
到期且从未下发的单进入 `failed`，`result.error="queue_expired"`，释放等待位并通知结果。
检查点包含接单、派发前与后台清扫（最多间隔 30 秒）。旧库中可确认尚未下发的行按
`created_at + 服务 TTL` 计算；已下发或历史下发状态未知的恢复工单保留原有恢复语义。
运行中、已下发后断线恢复的单不受排队 TTL 限制。匹配原工单的活动 router 心跳可将
断线重排的运行单恢复为 `running`，它旁边的看守单仍只占一个等待位。

各等待容量的重连均按 FIFO 每次恢复一条，前单完成后自动续推。
显式 0 保留零等待模式：忙碌时拒收新单，离线拒收，恢复已接受的单仍受占用检查。

显式设置大于 1 的容量（例如 `--backlog-max 128`）时沿用排队协议。

```
worker 离线 + queue_if_offline=false   → 409 {error:"worker_offline", worker}
worker 离线 + queue_if_offline=true    → 202 {task_id, queued:true, queued_ahead:N}
排队/积压已达配置容量                  → 429 {error:"backlog_full", queued, limit}
```

`queued_ahead` 是前面的等待任务数。`queue_if_offline` 缺省为 `false`，必须是 JSON 布尔。

任务状态：

```
queued ──dispatch──> running ──成果回执──> reviewing ──verdict──> done / failed
   │                    └──cancel──> cancelling ──停工确认──> cancelled
   │                                  └──60 秒无确认──> paused (cancel_unconfirmed)
   └──────cancel──────> cancelled
```

- `workers.busy` 只投影 `queued/running/paused`。原生对话活动由既有 `state/turn_source` 表达。
  仅有 `cancelling` 时，`busy=false`，派单仍会在取消收尾期间返回真正的工单占用。
  已接受的等待任务按 FIFO 单次推一条。
- `POST /tasks/{id}/execution` 接受当前连接、原执行代际及暂停原因。恢复需要相同原生身份的证据与新代际。
  旧协议重连按任务 ID 重投，支持分层状态的连接先核对在途执行。
- 取消请求经 SSE 或已登记的回环控制地址传递。停工 ACK 携带任务 ID、当前连接代际、执行代际、
  已停止的原生回合及剩余命令数，或能核对的未下发回执。无确认满 60 秒进入 `paused/cancel_unconfirmed`，
  后续派单返回 HTTP 409 `worker_paused`。重新发起同任务取消可重查停工。普通恢复请求不能解除取消占位。
- 经人工核实需要放行时，调用鉴权 `POST /tasks/{id}/cancel-release`，提交
  `{confirm:true, execution_epoch, reason}`。终态记录 `manual_release:true`、人工说明和时间，
  `cancel_confirmed:false` 明确保留原生确认缺失。迟到的普通结果只能收到已有终态。
- 心跳的 `turn_source` 接受 `trainer`、`router` 或 `null`。
  可选的 `turn_views` 按 thread ID 组织，条目为 `{phase,turnId,observedAt,seq,generation,fresh}`，
  `phase` 可为 `thinking/outputting/idle`。写入 `workers.turn_views_json`，缺省为 SQL NULL。
  调度和看板继续使用既有数据源，省略采样的心跳不更新该列。
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
Router → Worker : SSE deliverable_request {request_id, task_id, template, issued_at}
Worker → Router : POST /tasks/{id}/deliverable {request_id, worker, summary, artifacts, evidence, risks}
Router → 溪流   : task.deliverable，任务进入 reviewing
溪流 → Router   : POST /tasks/{id}/verdict {status, reason, evidence}
Router → trigger: POST /trigger
  {
    "source": "任务完成",
    "kind": "task.result",
    "level": "event",
    "text": "t-88 完成：…",
    "actor": "波旁",
    "event_id": "t-88:done",
    "payload": {"task_id": "t-88", "worker": "波旁", "status": "done"}
  }
  Header: X-Trigger-Token: <~/.hermes/secrets/trigger.token>
```

Router 按终态选择上表的 trigger 来源名与级别。
内部结果与 worker 的任务终态通知共用去重记录和 `event_id`，先到者保留。

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
| §二 积压上限 | 完成 | `--backlog-max` 默认 1，离线第一张等待单 200、第二张 409；显式大容量超限 429 |
| §五 结果回流 | 完成 | 见第五节 |
| worker → Hermes 递话 | 0.2.0 新增 | `POST /notice` |
| ⑨ 聊天旁听（只存不发） | 0.3.0 新增 | `chat_messages` 表 + `_chat_log` / `_chat_messages` / `_chat_summary` |

---

## 八、自测

```bash
/usr/bin/python3 server/router/selftest_router.py
# 端口被占时：
ROUTER_TEST_PORT=18790 python3 server/router/selftest_router.py
# 真等取消期限，使用隔离 Router：
ROUTER_TEST_REAL_CANCEL_TIMEOUT=1 python3 server/router/selftest_two_layer_state.py -v
```

自测启动临时 trigger 和 router 进程，用真实 HTTP 和真实 SSE 跑完整链路。
历史排队主套件显式设置 `--backlog-max 128`，工单幂等套件显式设置容量 2。
零等待套件显式设置 `--backlog-max 0`；默认一格套件用真实 HTTP/curl 验证 200/queued 与 409，覆盖 TTL 清扫、自动续推、并发和恢复。
连接竞争检查通过 HTTP 请求与确定性故障注入，覆盖连接消失和 `push` 返回失败的分支。
临时库/token/锁/日志都在 `tempfile.mkdtemp()` 里，不碰生产
`~/.hermes/router.db` 和 `~/.hermes/trigger.db`。

覆盖项目如下。

1. token 生成（64 hex，0600）、`/health` 形状、无 token → 401、单实例 flock
2. register → SSE 上线 → 心跳注释行 → 空闲立即派活（200 dispatched）
3. `last_progress_at` 来自 heartbeat 的 `progress_at`
4. 交活 → `done` → trigger 库出现 `source=任务完成 kind=task.result level=event`
5. 断线 → offline（名字保留）→ 409 → `queue_if_offline` 202 →
   重连按顺序补推两条积压 → 交活
6. 已接受的积压按 FIFO 续投：第一条交活后第二条自动下发
7. 取消排队 / 运行中任务；迟到 result 被忽略
8. 离线积压 128 条全部 202，第 129 条 429 `backlog_full`
9. 订阅：201 → `trigger_id` → trigger 侧 active → pending 拉到事件 →
   第二次 pending 为空（ACK 生效）→ kinds 过滤 → DELETE 后 trigger closed
10. `POST /notice`：normal → `level=notify`；urgent → `level=event`；
    同分钟重复 → 同一个 `event_id`、`duplicate:true`、trigger 只有一行
    任务终态覆盖两入口的两种顺序、并发、异文重复、不同工单及不同终态、
    trigger 失败后重试、重建实例后的持久化去重、500 条及 24 小时边界
11. 删除后重声明 → 新 trigger 名（`router:<worker>:<id>`）→ 新队列可投递
12. SSE 断开 → 本地订阅清空 + trigger 订阅 closed；重连不自动重建
13. trigger 挂掉：`POST /subscriptions` → 502 且表回滚；
    `POST /notice` → 200 `ok:false`（不是 5xx）
14. 最终 `/health` 计数、trigger notify 条数、`GET /tasks?limit=500` 取回 128 条
15. 聊天旁听：写入/读回字段一致（含 `meta` 落库）→ 同
    `(worker, message_id, direction)` 重复 `duplicate:true` 且不新增 →
    同 `message_id` 换方向是新行 → `limit` / `since`（id 与 epoch）/
    `direction` 过滤 → `summary` 60/180 分钟 in/out 计数与 `last`
16. 零等待位及正容量均按实存工单占用拒绝并发派单，返回 `worker_busy` 和原工单 ID。
    trainer 活动回合继续可读，对话活动不占任务位。
17. 拒收 UUID 在空闲后重试成功，已接受 UUID 在忙碌、取消中、终态和离线时精确幂等
18. 八个不同 UUID 并发只有一个派发赢家，其余 UUID 可重试；拒收后的数据库校验
19. `queue_if_offline=true` 在零容量离线时返回 409，已接受的旧排队任务可重连恢复
20. 确定性模拟 `push` 失败与连接消失，清理未接受任务并释放 UUID，随后重试成功
21. 默认一格、显式 `--backlog-max 0`、负容量拒绝和构造器默认值
22. 旧单恢复推送失败且连接在线、worker 空闲时，八个并发新请求均拒收，
    返回最早旧单 ID 与 `retry_when`；旧单精确重试、FIFO 恢复及拒收 UUID 后续重试

macOS 原生执行上述 HTTP/SSE、数据库、权限、单实例锁与进程生命周期检查。
连接竞争和缓存 `busy=false` 的检查属于故障分支模拟。
Windows 原生执行待验，当前 router 的 `fcntl` 单实例锁依赖 POSIX；
Windows 路径、大小写、文件权限、IPC 与进程生命周期需在可运行的原生环境验收。

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
