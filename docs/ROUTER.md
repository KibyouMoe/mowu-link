# Router 骨架（Worker / 任务 / 订阅 / 结果）

> 定稿依据：2026-11-08 训练员口述 + 多次修正。
> 实现位置：`127.0.0.1:8790`，库 `~/.hermes/router.db`。
> **没有"座位"这个概念**——只有「worker」和它当前的「连接」。

---

## 一、职责边界

| 谁 | 知道什么 | 不知道什么 |
|---|---|---|
| Worker | 自己会干什么 | 别的 worker、Hermes、trigger |
| **Router** | 谁在线、谁忙、每个 worker 积压几条 | **能力、擅长、任务语义** |
| Hermes（我） | 名字 ↔ 能力 ↔ 历史亲和 | worker 的实现、worker 的会话 |
| trigger | 消息、级别、订阅关系 | worker、任务 |

**一句话**：Router 只搬运事实，Hermes 只判断语义。

---

## 二、Worker 与连接

```sql
workers(
  name         TEXT PRIMARY KEY,    -- worker 自报的名字，如 "波旁"
  online       INTEGER DEFAULT 0,
  busy         INTEGER DEFAULT 0,
  queued       INTEGER DEFAULT 0,
  last_seen    REAL,
  connected_at REAL,
  version      TEXT,
  note         TEXT                 -- 我的备注（在哪台机器），Router 不解释
)
```

**连接不落库**：worker 启动时建一条长连接（SSE 下行），Router 只在内存里记「这个名字当前连着哪条流」。断了就清掉；重连就是新的一条流——**名字不变、积压不变**。

### 接口（worker 用）

```
POST /register   {name}                        → {ok, queued}   上号
GET  /stream     ?name=波旁                     → SSE 下行（任务、消息）
POST /heartbeat  {name, state, progress_at, task_id?}
POST /result     {name, task_id, status, summary, artifacts[], evidence[]}
```

### 接口（Hermes 用）

```
GET  /workers                → [{name, online, busy, queued, last_seen, last_progress_at}]
POST /tasks {worker, goal, context?, workspace?, urgency?, deadline_s?, queue_if_offline?} → 见下表
POST /tasks/{id}/cancel
GET  /tasks/{id}
```

**`queue_if_offline` 的语义**（布尔，默认 `false`）：

```
worker 在线                       → 200 {"task_id":"t-88","dispatched":true}

worker 离线 且 queue_if_offline=false（默认） → 409 {"error":"worker_offline","worker":"波旁"}
                                     ← Hermes 自己决定：换人 / 放弃 / 改用 queue_if_offline=true

worker 离线 且 queue_if_offline=true          → 202 {"task_id":"t-88","queued":true,"queued_ahead":7}
                                     ← 收下，堆着，等它上线
```

**"要不要堆"由 Hermes 决定，Router 只如实回答"他在不在"。**

**Hermes 看不到、也不需要看到连接层的任何东西。**

---

## 三、任务

```sql
tasks(
  task_id     TEXT PRIMARY KEY,
  worker      TEXT,                 -- 按名字挂，不按连接挂
  goal        TEXT,
  context     TEXT,
  workspace   TEXT,
  urgency     TEXT,                 -- urgent | normal
  state       TEXT,                 -- queued | running | done | failed | cancelled
  created_at  REAL, started_at REAL, finished_at REAL,
  result_json TEXT
)
```

派发规则：

```
worker 在线且空闲   → 立刻从连接推下去
worker 在线但忙     → 进队列（按名字）
worker 离线         → 进积压（按名字）；上线时按顺序一次性推
worker 不存在       → 400，Hermes 自己纠错（Router 不猜）
```

---

## 四、事件订阅

```sql
subscriptions(
  id         INTEGER PRIMARY KEY,
  worker     TEXT,
  source     TEXT,                  -- mc_chat / email / ...
  kinds      TEXT,                  -- JSON 数组，空=全部
  active     INTEGER DEFAULT 1,
  created_at REAL
)
```

```
POST   /subscriptions   {worker, source, kinds?}   → Router 去 trigger 注册
DELETE /subscriptions/{id}
GET    /subscriptions?worker=
GET    /subscriptions/pending?worker=              → 取 trigger 队列并 ACK
```

投递（训练员定：订阅类消息**不留存**）：

```
trigger 有匹配消息 → 写进该订阅者在 trigger 的队列（保留到 ACK）
Router 被 worker 拉取（GET /subscriptions/pending）
                   → 按订阅表的 source/kinds 过滤 → 返回 → ACK
worker 连着      → 正常拉取
worker 没连着    → 订阅已删除，trigger 侧也关闭，消息不投给它（不建积压）
worker 掉线      → Router 立刻 DELETE 它在 trigger 的订阅，trigger 停止投递
worker 重连      → worker 自己重新声明要订阅什么（它的初始化流程）
```

理由：订阅是“实时听”，人不在听了也没用；留一堆过期消息只会污染它上线后的判断。
对比：**任务是活，要攒；订阅是听，不留。**

> **实现注记（0.2.0）**：trigger 0.7.x 没有 `POST /subscribe`，它是游标总线——
> 订阅记录由 `GET /events?subscriber=router:<worker>&since=<当前尾部>` 惰性创建，
> 消息用 `GET /events` 拉取、`POST /ack` 确认、`POST /renew` 续租。
> Router 的 `POST /subscriptions` 做的就是这套调用；`trigger_id` 存的是实际订阅名。
> closed 的订阅名不能复用，所以 worker 重连后重声明会拿到 `router:<worker>:<id>`。

---

## 五、结果回流

```
Worker → Router  : POST /result {task_id, status, summary, artifacts}
Router → trigger : POST /trigger {level:"notify", source:"router",
                                  kind:"task.result", text:"t-88 完成：…",
                                  payload:{task_id, worker, status}}
trigger → Hermes : notify 排队；空闲则叫醒
```

**Router 不直接跟 Hermes 说话**——只往 trigger 写一条 notify。

---

## 六、时序

```
worker 启动
  └─ POST /register {name:"波旁"}      → 上号（顺便告诉它积压几条）
  └─ GET /stream?name=波旁             → 挂长连接
Hermes 派活
  └─ POST /tasks {worker:"波旁", goal}  → t-88
Router
  └─ 在线且空闲 → 通过 SSE 推给波旁
波旁干活
  └─ POST /result {task_id:"t-88", status:"done", ...}
Router
  └─ POST /trigger {level:"notify", text:"t-88 完成"}
trigger
  └─ 我空闲？叫醒 / 忙？排队
```

---

## 七、最小实现顺序

```
① workers + register + heartbeat + GET /workers     （先能看见谁在）
② tasks + 队列 + result + notify 回流               （再能派活收活）
③ subscriptions + trigger 转发                      （再接事件流）
④ worker SDK（一个 python 文件）                     （最后给 worker 用）
```

①②做完就能用假 worker 跑通「派活 → 执行 → 交活 → notify 回来」。

---

## 八、待定

- trigger → Router 的转发端口与鉴权（复用 trigger token / 单独一把 router token）
- worker 注册鉴权（首次注册发 token，之后带）
- 积压上限（暂定 128，与 trigger 一致）
