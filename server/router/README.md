# mowu-router 0.1.0

`docs/ROUTER.md` 的 ①②③ 落地：worker 在线表、任务派发/收回、订阅表。
**④ worker SDK 不在本次范围**。

一句话：Router 只搬运事实（谁在线、谁忙、每个 worker 积压几条），
不懂能力、不懂任务语义；语义判断留在 Hermes。

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
| GET | `/stream?name=&version=` | SSE 长连接 | 见下 |
| POST | `/heartbeat` | `{name, state?, progress_at?, task_id?}` | `{ok, name, online, busy, queued, last_seen}` |
| POST | `/result` | `{name, task_id, status, summary, artifacts?, evidence?}` | `{ok, task_id, state, notified, dispatched_next}` |

- `status` 只接受 `done` / `failed`。
- 同一任务重复上报是幂等的：已终态的任务返回 `duplicate:true`，不重复 notify。
- `name` 必须与任务归属一致，否则 400 `worker_mismatch`。
- `/result` 会先落库、再尝试给 trigger 写一条 notify（3s 超时）。
  trigger 不可用时**不影响交活**，响应里 `notified:false`，日志留 WARNING；不重试。

### Hermes 侧

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/workers` | `{ok, count, workers:[{name, online, busy, queued, last_seen, version, state, ...}]}` |
| POST | `/tasks` | `{worker, goal, context?, workspace?, urgency?, deadline_s?, queue_if_offline?}` |
| POST | `/tasks/{id}/cancel` | 取消排队中或运行中的任务 |
| GET | `/tasks/{id}` | `{ok, task_id, state, task}` |
| GET | `/tasks?worker=&state=&limit=` | 默认 100，最大 500 |
| GET | `/health` | `{ok, version, workers, tasks, tasks_by_state, online, ...}` |

### 订阅

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/subscriptions` | `{worker, source, kinds?}` → 201 `{ok, id, ...}` |
| GET | `/subscriptions?worker=&active=1` | 列表，`kinds` 为数组 |
| DELETE | `/subscriptions/{id}` | `active=0`（保留行，不物理删） |

**订阅转发是 stub**：0.1.0 只把订阅落库，`TriggerSubscriptionBridge.enabled=False`，
不会真的去 trigger 注册、也不会接收 trigger 推来的消息。见第六节。

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
  `deadline_s` 落库并算出 `deadline_at`，0.1.0 不做超时回收。

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
- 断开（EOF/RST/写失败）→ `online=0`，**名字与积压保留**。
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

## 六、订阅：已落库，转发未接（stub）

表结构与 `ROUTER.md` §四一致；`POST/GET/DELETE` 都可用。0.1.0 的边界：

- `POST /subscriptions` 只入库，`trigger_forwarded:false`；
  `TriggerSubscriptionBridge.register()` 只写日志。
- `DELETE` 只把 `active` 置 0，不通知 trigger。
- worker 掉线时 `TriggerSubscriptionBridge.worker_offline()` 只记一条「本应删除 N 条」。
- 还没有「trigger → Router → worker」的消息入口；按 `ROUTER.md`，订阅类消息
  **不留存**，worker 没连着就直接丢。

接下一层时只需要实现 `TriggerSubscriptionBridge` 的三个方法（register /
unregister / worker_offline）并加一个 trigger 回调入口，表结构不用动。

---

## 七、与 `docs/ROUTER.md` 对应关系

| ROUTER.md | 状态 | 实现位置 |
|---|---|---|
| ① workers：register / heartbeat / GET /workers | 完成 | `RouterStore` workers 段 + `_register` / `_heartbeat` / `_workers` |
| ① 连接不落库，SSE 即在线 | 完成 | `RouterServer.conns`（内存）+ `attach` / `detach` |
| ② tasks：派活 / 队列 / 三态 | 完成 | `_task_create` + `claim_next_queued` + `flush_backlog` |
| ② result → 状态迁移 → notify | 完成 | `_result` + `TriggerNotifier.notify_result` |
| ② cancel / get / list | 完成 | `_task_cancel` / `_task_get` / `_tasks_list` |
| ③ subscriptions 表 + CRUD | 完成（表） | `subscriptions` 表 + 三个 handler |
| ③ trigger 转发 / 反向消息 | **stub** | `TriggerSubscriptionBridge` |
| ④ worker SDK | 未做（本次范围外） | — |
| §二 `queue_if_offline` 语义 | 完成 | 见第三节 |
| §二 积压上限 128 | 完成 | `--backlog-max`，超限 429 |
| §五 结果回流 | 完成 | 见第五节 |

---

## 八、自测

```bash
/usr/bin/python3 server/router/selftest_router.py
```

它**不是单元测试**：会真的启动两个进程（一个临时 trigger 实例 + 一个
监听 `127.0.0.1:8790` 的 router），用真实 HTTP 和真实 SSE 跑完整链路。
临时库/token/锁/日志都在 `tempfile.mkdtemp()` 里，不碰生产
`~/.hermes/router.db` 和 `~/.hermes/trigger.db`。

覆盖（当前 **32/32 PASS**）：

1. token 生成（64 hex，0600）、`/health` 形状、无 token → 401、单实例 flock
2. register → SSE 上线 → 心跳注释行 → 空闲立即派活（200 dispatched）
3. 交活 → `done` → trigger 库出现 `source=router kind=task.result level=notify`
   `event_id=<task_id>:done`
4. 断线 → offline（名字保留）→ 409 → `queue_if_offline` 202 →
   重连按顺序补推两条积压 → 交活
5. 忙时第二条排队（不提前推）→ 第一条交活后第二条自动下发
6. 取消排队任务（worker 从未收到）→ cancelled
7. 取消运行中任务 → cancelled、busy 释放、迟到 result 被忽略
8. 离线积压 128 条全部 202，第 129 条 429 `backlog_full`，重连前
   `GET /workers` 仍显示 `queued:128`
9. 订阅 POST 201 → GET → DELETE `active:false`
10. 最终 `/health` 计数、trigger notify 条数、`GET /tasks?limit=500` 取回 128 条

端口被占用时会直接报错退出；可用 `ROUTER_TEST_PORT=8799` 换端口。

---

## 九、已知边界 / 设计选择

- **at-least-once**：断线重连会把 `running` 任务重发，worker 必须按 `task_id` 幂等。
- **一个名字一条流**：新连接替换旧连接；不做多设备同名的负载均衡。
- **没有 per-worker 鉴权**：0.1.0 只有一把共享 `router.token`（`ROUTER.md` §八
  的「首次注册发 token」仍待定）。
- **urgency 不抢队**：只透传，排队 FIFO。
- **deadline_s 不触发回收**：只记录。
- **notify 不重试**：trigger 挂了就丢这一条（日志有 WARNING，响应 `notified:false`）。
- **订阅转发未接**：见第六节。
- **SQLite 单连接 + 进程内锁**：够 loopback 小流量用；多进程写由 flock 挡住。
