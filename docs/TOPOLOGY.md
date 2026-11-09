# 拓扑定稿（Trigger / Super Agent / Router / Worker）

> 这份是**我们自己整理出来的拓扑**，不是官方图的翻译。官方只有子系统文档、没有整体图；
> 下面每一条都能在 `research/OFFICIAL-TOPOLOGY-COMPARE.md` 找到出处或标注 `[自定]`。
>
> 读者：溪流（Router 侧实现）、各 worker 适配器作者、桌宠（表现层）。
> 变更规则：改这张图必须同时改 `ARCHITECTURE.md` 与 `research/INDEX.md` 的登记。

---

## 1. 总体分层

```
                         ┌──────────────────────────────────────────┐
                         │  训练员（唯一人类）                         │
                         └───────┬──────────────────────┬───────────┘
                                 │ 飞书私聊（原生通道，不进总线）│ 看桌宠/电视
                                 ▼                      ▼
┌──────────────────────────────┐        ┌──────────────────────────────┐
│  sources 层（消息源）           │        │  表现层（桌宠 / 电视）          │
│  email │ mc_chat │ cron │ …  │        │  只订阅状态，不参与调度          │
│  各带本地母线（内部用）         │        └──────────────▲───────────────┘
└───────────┬──────────────────┘                       │ 状态流
            │ POST /trigger（一消息一请求，幂等）          │
            ▼                                          │
┌──────────────────────────────┐                        │
│  trigger :8791               │                        │
│  events / cursors / receipts │                        │
│  = 收件箱 + 地址簿 + 唤醒器     │                        │
│  ★ worker 的订阅面            │                        │
└──────┬───────────────┬───────┘                        │
       │ 注入/唤醒(P0)   │ /events?wait=25 /subscribe     │
       ▼               │                                │
┌──────────────────────────────┐   │                     │
│  super agent（溪流）           │   │                     │
│  整理 → 判断 → 拆解 → 选目的地  │   │                     │
│  → 验收 → 汇报                 │   │                     │
└───────────┬──────────────────┘   │                     │
            │ submit/poll/result/cancel（唯一协议）        │
            ▼                       │                     │
┌──────────────────────────────┐   │                     │
│  router :8790                │   │                     │
│  task / worker / job / lease │   │                     │
│  approval / mandate / outbox │   │                     │
│  adapter: dsh│codex│claude│… │   │                     │
└───────────┬──────────────────┘   │                     │
            │ adapter 内部协议（不暴露给上游）                │
            ▼                       │                     │
┌──────────────────────────────┐   │                     │
│  worker 层（常驻角色）          │◀──┘                     │
│  DSH │ Codex │ Claude │ …     │─────────────────────────┘
│  身份 + 收件箱游标 + 记忆 + 心跳  │  状态事件（idle/working/blocked/error）
└──────────────────────────────┘
```

**两条流分开**：

- **控制流**：我 → router → worker（派活、取消）
- **感知流**：sources → trigger →（我 / worker 订阅）

---

## 2. 每层的职责与禁止事项

| 层 | 只做 | 绝不做 |
|---|---|---|
| sources | 连源、归一化信封、过滤墙、POST + 本地发布 | 理解任务、决定路由、持久化业务状态 |
| trigger | 收、去重、落库、游标、唤醒、fan-out 订阅 | 转发订阅到 sources、执行业务动作、重新理解语义 |
| super agent | 整理信息、判断意图、拆任务、选目的地、验收、汇报 | 直接和 worker 谈协议、自己下手干活 |
| router | 状态机、派发、租约、重试、adapter、approval、job 记账 | 改写需求、读 worker 代码、暴露 worker 专有字段 |
| worker | 执行、产出、写 action 事件 | 越 mandate 行动、直接给我发消息（一律经 trigger） |
| 表现层 | 订阅状态、渲染五态 | 参与调度、改任务状态 |

> 判定口径：**谁能改谁的状态**。上游只能通过 verb 改下游，下游只能通过事件上报；不存在双向直连。

---

## 3. 状态机（五个，互相独立）

### 3.1 Task（router 内）

```
           submit
             │
             ▼
        ┌─────────┐   lease    ┌─────────┐
        │ queued  │───────────▶│ running │
        └─────────┘            └────┬────┘
             ▲                     │
             │ lease 过期           ├──▶ succeeded   （必须 job 已结算 + schema 通过）
             └─────────────────────┤
                                   ├──▶ failed      （reason: crashed|timeout|schema|worker_error）
                                   ├──▶ cancelled   （我主动 cancel，或 approval 拒绝）
                                   └──▶ blocked     （缺输入 / 越界 / worker offline）
```

- `interrupted` **不是状态**，是 `failed` / `cancelled` 上的 `reason`（官方叫 TurnEndReason，我们收进 reason 字段）。
- 进入 `succeeded` 的前置条件写死：**没有未结算的 job，且 output_schema 校验通过**。

### 3.2 Worker（router 的 worker 表）

```
   offline ──心跳──▶ idle ──领任务──▶ busy ──交付──▶ idle
      ▲                │                │
      └── 超时无心跳 ───┴────────────────┘
   quiescent（收尾中：turn 已结束但 job 未结算）
```

### 3.3 Job（事件流，不建独立表）

```
task_id + kind=job_started ──▶ job_settled(ok|fail)
约束：同一 task 存在未 settled 的 job ⇒ 该 task 不允许 succeeded
```

### 3.4 Subscription（trigger）

```
active ──到期未续──▶ expired（保留 24h，可 since 恢复）
active ──5 次写失败/EOF──▶ closed(reason)
active ──max_events 用尽──▶ exhausted(last_seq)
```

### 3.5 Approval（router，fail-closed）

```
请求动作 ──命中白名单──▶ allowed_once ──▶ 执行
        ──未命中/不可用/超时──▶ denied ──▶ task 转 blocked
```

---

## 4. 契约

### 4.1 任务信封（我 → router）

```json
{"task_id":"t-...","parent":"root","kind":"dsh|codex_local|codex_remote|claude|hermes",
 "goal":"一句话目标","context":"必要背景与绝对路径","constraints":["..."],
 "workspace":"/abs/path","output_schema":{},"deadline_s":5400,
 "depends_on":[],"mandate":"mandate-id","sandbox":"read-only|workspace-write|danger-full-access"}
```

### 4.2 结果（worker → router）

```json
{"task_id":"t-...","status":"done|failed|blocked|needs_input",
 "summary":"...","reason":"crashed|timeout|schema|interrupted|worker_error",
 "artifacts":[{"type":"file|transcript","uri":"...","sha1":"..."}],
 "evidence":["日志行","截图绝对路径"],"metrics":{"wall_s":0}}
```

**硬要求**：`artifacts` 必须带 `transcript`（每任务可回读的执行日志指针）——这是 G1，缺了等于把“它做了什么”丢掉。

### 4.3 事件（worker → trigger / router → trigger）

```json
{"source":"worker:dsh","kind":"task.started|task.finished|job_started|job_settled|action",
 "task_id":"t-...","actor":"worker-id","text":"...","payload":{}}
```

规则：**action 事件必写**（做了什么 / 为什么 / 结果），否则不可审计、不可撤销。

---

## 5. 官方概念落在我们哪一层

| 官方 | 我们 | 说明 |
|---|---|---|
| Cordis 插件树 / bundle-patch | ❌ 不引入 | 用 `profile + adapter` 顶替；Hermes 无 DI 容器 |
| append-only SessionEvent（唯一事实源） | **worker 的 `transcript` artifact** | 不自建日志库，把日志变成交付物（G1） |
| TurnEndReason（7 种） | `result.reason` 枚举 | 区分失败/被打断/超时（G14） |
| job（活过 turn） | `event.kind=job_started/job_settled` | 不加表，靠事件 + 前置条件（G3） |
| AgentStatus(idle/running) | worker 四态 + quiescent | 任务态与执行体态分离（G2） |
| approval（fail-closed、allowed-once） | router 的 approval 门 | 越界即 blocked，不做审批 UI（G4） |
| sandbox 三档 | worker 启动参数 `sandbox` | 默认 workspace-write，全权必须显式（G5） |
| Agent Teams 任务 DAG | `task.depends_on` 一列 | 不建 DAG 调度器（G8） |
| goals（跨轮目标） | ❌ 不补 | 与 task 五态重叠 |
| webhook runtime | ❌ 不补 | trigger 已更强 |
| compaction / image offload | ❌ 不补 | 压缩归宿主（Hermes / DSH） |
| Thread | ❌ 不是官方核心概念 | 别再用这个词 |

---

## 6. 一图看缺口

```
   sources ──▶ trigger ──▶ 溪流 ──▶ router ──▶ worker
                 │                   │           │
                 │                   │           ├─ G5 sandbox 三档（启动层）
                 │                   │           └─ G1 transcript（adapter 契约）
                 │                   ├─ G2 worker 状态机
                 │                   ├─ G3 job 记账
                 │                   ├─ G4 approval 门
                 │                   ├─ G6 schema 校验
                 │                   ├─ G8 depends_on
                 │                   └─ G14 reason 枚举
                 └─ 订阅 fan-out：有界队列 / gap / 写失败断开（已实现，扩容到 worker）
```

---

## 7. 最小可用上线顺序（按依赖，不是按价值）

```
① router 骨架：task 五态 + submit/poll/result/cancel + SQLite + flock
② dsh adapter：ACP stdio，一 worker 一 profile，transcript 指针
③ trigger 扩容：worker 订阅面 + gap + 心跳
④ G2/G3/G14：状态机补全（三个一起做，改同一张表）
⑤ G6：schema 校验
⑥ G4/G5：approval + sandbox 档位
⑦ G8：depends_on
⑧ 桌宠接状态流
```

理由：①②没有就没法自测；③④决定“失败能不能看出来”；⑤⑥是“敢不敢让它碰真东西”的门槛。

---

## 8. Sources 客户端契约（每个源独立开发，结构必须统一）

> 官方的四个 poller 只是 GitHub 的实现细节，**不值得抄**。值得抄的是「每个源与系统的接缝长什么样」——
> 官方那边接缝是 `SourceKind`（声明）+ `scope()`（准入）+ `Intake.receive`（归一/去重）；
> 我们这边接缝是 `source.json`（声明）+ `filterwall`（准入）+ `POST /trigger`（归一/去重）。

### 8.1 一个 source 客户端必须提供七件事

> 用大白话写，别用英文术语糊人。左列是术语（代码里用），右列是它到底在说什么。

| # | 术语 | 大白话 |
|---|---|---|
| ① | connect | **连不上要自己重连**。不是“保证永不掉线”（做不到），是掉线后别死、按 1s→2s→4s→8s 退避重试，日志写清在重试 |
| ② | poll / on_event | **怎么拿数据，二选一**：<br>· 轮询 = 我们隔一会儿去问一次“有新东西吗”（邮箱、GitHub）<br>· 推送 = 它来一条就告诉我们一条（MC 聊天、webhook） |
| ③ | normalize | **归一化**：把各来源五花八门的字段，装进同一个信封结构（谁说的、什么时候、说了什么、什么类型、多急） |
| ④ | filterwall | **过滤墙**：决定这条要不要放过去。<br>**但所有收到的都必须留底**——放行的记一条，拦下的记一条 + 原因；任何人都要能查“拦了什么、为什么拦” |
| ⑤ | publish | **发出去**：把信封 POST 给 trigger；失败按 0.5/1/2/4 秒重试；同时往本地母线发一份（同一个事件号） |
| ⑥ | health | **保活**：定期说“我还活着 + 上次成功是什么时候 + 连续失败几次 + 最后一条报错是什么”，上层据此判断这个源是不是死了 |
| ⑦ | bounded queue | **队列有上限**：积压到上限就丢最旧的，并记下“丢了几条”，不能无限吃内存 |

### 8.1.1 传输方式：一律走本地 HTTP，不走进程内直调

**训练员定的原则（2026-11-08）**：现在所有来源都跑在同一台机器上，但**不许图省事直接 import 函数或共享内存**，一律走 `127.0.0.1` 的 HTTP/TCP。

- 理由：将来某个源搬到别的机器上，只改一个地址，不改代码结构；
- 代价：多一次序列化（几毫秒），换来的是“位置无关”；
- 现状：`POST http://127.0.0.1:8791/trigger`、本地母线 `http://127.0.0.1:8792|8793`，都已经是这个形态，继续沿用。

### 8.1.3 三层过滤的职责划分（2026-11-08 训练员修正）

**原来的分法错了**：把白/黑名单、限流、去重、优先级映射放在客户端，等于每个源各实现一遍，规则一改要改 N 份。

**正确分法**：

| 层 | 在哪 | 管什么 | 举例 |
|---|---|---|---|
| L1 业务过滤 | **source 客户端** | 这个源**特有的业务语义**：跟业务无关的别送 | 邮箱只送「收件箱 + 特定文件夹」；MC 只送「聊天栏，不看系统消息」；仓库只送「指定 repo」 |
| L2 通用机制 | **trigger** | 跨源共用的平台能力，一处配置全局生效 | 白名单 / 黑名单、限流（每分钟 N 条）、去重窗口、优先级映射、静默期、要不要唤醒 |
| L3 语义判断 | super agent | 这条到底什么意思、要不要动手 | 判断“这是新任务还是追问”、“要不要回话” |

判据：**规则里如果出现“某个源专有的东西”（邮件文件夹、MC 玩家名、仓库名），放 L1；只要是“任何源都适用”的（限流、黑白名单、优先级），放 L2。**

后果：`clients/filterwall.py` 里的通用部分要上移到 trigger 的规则引擎；客户端只留一个轻量的“业务范围”钩子（送不送这类内容）。

### 8.1.5 消息四级（训练员定稿，2026-11-08；实现见 trigger-bus 0.9.2）

信封必带 `level`，一个字段同时表达“多要紧”和“怎么送达”：

| level | 含义 | 推给溪流 | worker 订阅 | 落库 |
|---|---|---|---|---|
| `event` | 引导：立刻停手处理 | **会（硬打断，含在飞工具）** | 会 | 是 |
| `notify` | 排队：本轮结束后接手 | 会（空闲立即唤醒；忙则静默排队） | 会 | 是 |
| `info` | 普通事件 | 不会（要就提权） | 会 | 是 |
| `log` | 只留档 | 不会 | 不会 | 是 |

- **飞书（训练员本人）走 steering（`busy_input_mode: interrupt`）**，不判断、不降级——训练员说话永远打断。
- 其它来源默认 `info`；缺省 `level=info`：旧客户端不带时，不打扰我；需要的源要显式标 `event`/`notify`。
- `event` 的实现是**真打断**（训练员明确要求）：唤醒文本 `⚡ 来自 <source> · <actor> 的打断`，`internal=False` 走正常入站；不合并、每条都打断。
- `notify` 的实现是**原生队列**（`adapter.handle_message → admit_native`），绝不再用会杀工具的 `_pending_messages` 槽：
  - 空闲：立即唤醒，78～96ms 级；
  - 忙：同一会话**只 admit 一次**，后续 `MERGED` 进同一批（`woke=window(no new turn)`），轮末一次注入全部事件（一张 `[N 条]` 卡片、cursor 一次推到最大）。
- **可见通知的时机**：`event` 到达即发 `⚡ 收到来自 <source> · <actor> 的 Event 消息`；`notify` **排队期间完全静默**，只在真正注入上下文那一刻发一条多行 `📬`（每条一行，最多 3 行，超出则第 3 行后缀 `…x(总数-2)`）。理由：通知的时间必须等于落地的时间。
- **送达判据**：事件被真正注入上下文才推进游标 / ACK（`confirmed by context injection`），排队、被拒、超时都不推进（至少一次）。
- `priority` 只排“叫醒的先后”：`event` 缺省 0，`notify` 缺省 0（排队路径），`info` 缺省 2，`log` 不参与投递。


### 8.1.6 临时提权（代替“我自己订阅”）

**为什么不是订阅**：溪流是按 turn 跑的，没有常驻连接，无法像 worker 那样长期挂着订阅。

**改成租约式提权**：

```
POST /promote {source, kind?, thread_key?, ttl_s:3600}
  → {promotion_id, expires_at, scope}
生效期间：匹配 scope 的 info 消息按「提权」处理
        → 进我的注入队列（priority 仍按 info=2，不打断，只是能看见）
到期前 10 分钟：trigger 发一条 event 问我
        kind=promotion.renew, payload={promotion_id, expires_in:600,
        text:"这个提权还有 10 分钟到期，还要不要？"}
        → 我回 POST /promote/<id>/renew 就续；
        → 我不回、或回 drop，到期自动失效（不报错、不追问）
失效后：info 又对我不可见；日志里记一条 ended(reason=expired|dropped)
```

- 默认 TTL 1 小时，续期窗口 10 分钟（与已有的订阅周期机制一致，不另造一套）。
- 同一个 scope 重复 `/promote` 不新建，只延长 TTL（幂等）。
- `GET /promotions` 列出当前生效的提权（我用来回答“我现在在看哪些 info”）。

判据：**溪流不订阅，只提权；提权是有期限的，到期必须自己决定要不要续。**

```
每条收到的消息都留底，两类都要留：
  放行  → {audit_id, source, event_id, decision:"pass", at, envelope摘要}
  拦下  → {audit_id, source, event_id, decision:"block", reason, at, envelope摘要}
查询     clients/audit.py --source mc_chat --blocked --since 1h   （本地 SQLite，不上报也不占 trigger）
保留     默认 7 天滚动删除；被拦记录不参与业务，只供排查
```

判据：**任何一条被吞掉的消息，都要能回答“谁拦的、为什么、什么时候”。** 只写日志文件不算——日志会被轮转掉，必须有可查询的库。

### 8.2 责任边界（写死，越界就是返工）

| 位置 | 负责 | 不负责 |
|---|---|---|
| source 客户端 | 连源、归一、过滤、重发、游标 | 判断这条消息该不该叫醒我、拆任务、任何业务语义 |
| `source.json` | 声明能力边界（schema/priority/健康阈值/dedupe 键） | 实施细节、任何代码 |
| trigger | 收、去重、落库、游标、唤醒、fan-out | 懂某个源怎么抓、决定优先级（优先级由 source 的 policy 给默认值） |
| super agent | 判定要不要打断、怎么回、是否拆任务 | 连源、解析原始协议 |

### 8.3 加一个新源 = 三步

```
1. 写 sources/<name>/source.json      （name / mode / schema / policy / health / dedupe）
2. 写 clients/<name>_client.py        （七件事，照她现有两个客户端抄骨架）
3. 启进程（LaunchAgent 或 nohup）+ 在计划任务里登记；不需要改 server.py 一行
```

**判据**：如果加源要改 `server.py`，说明接缝设计错了，往回收，不要给那个源开特例。

### 8.4 三层游标（最容易混的一处）

**身份 ≠ 位置**：`event_id` 回答「这是不是同一条消息」，游标回答「我从哪继续」。

| 层 | 存哪 | 答什么问题 | 我们现状 |
|---|---|---|---|
| ① 源侧游标 | 客户端本地 `clients/state/<name>_cursor.json` | 在**外部系统**里读到哪了（GitHub 通知 id / IMAP UID+UIDVALIDITY / bridge lastSeq） | email ✅（UIDVALIDITY + last UID + UIDNEXT 基线）；**mc_chat ❌ 没有持久游标** |
| ② 订阅者游标 | trigger 的 `cursors(subscriber,last_id)` | 这条流水线**发给某消费者**到哪了 | ✅ 有，`since` 续传 + `POST /ack` 推进 |
| ③ 去重窗口 | 客户端 `dedupe.json`（哈希 + 时间戳） | 同一条消息短时间**重复投递**要不要处理 | ✅ 有（防重处理，不是游标） |

官方细节（可借）：

```
游标键    github-cursor:<n>，四个 poller 各自独立
迟写容忍  cursorAt() 回退 5s —— 上游写入比游标慢时不会漏
过滤也记账 被过滤的 trigger 依然落库 + 写 disposition（可回溯「为什么没理这条」）
```

我们的缺口与判据：

```
① mc_chat 必须补持久游标：重连带 since=<最后 lastSeq>，并把 lastSeq 落盘
② 拉取型源统一 lookback_s（默认 5s），防「上游 commit 晚于游标」的边界丢信
③ 游标文件损坏/丢失时的行为要写死：默认不重放历史（宁漏不重），并打一条 warn
```

**按源类型分开，不要一套参数套所有源**（2026-11-08 训练员指出）：

| 源类型 | 有历史可回看？ | lookback | 重启后怎么办 |
|---|---|---|---|
| push 型 · MC 聊天 | **没有**（每次登录聊天框清空） | **0**（不回看） | 检测 bridge 会话是否变了：`lastSeq < 已存 last_seq` ⇒ 新会话，重立基线不重放 |
| pull 型 · 邮箱 | 有（IMAP 服务器保留） | 5s | 从 last UID 继续；UIDVALIDITY 变了重置 |
| push 型 · 以后的 webhook | 通常没有 | 0 | 同 MC |

已知边界：桥接 `/state.events` 目前只有 `{lastSeq, buffered}`，没有 bootId。若新会话在客户端重连前就超过上次的 lastSeq（要几百条聊天才可能），会被误判为同一会话——可接受，不为它改 mod。

### 8.6 队列与丢弃策略（2026-11-08 定）

**先分清两种积压**：

```
A. 下游暂时慢（秒~分钟）→ 队列缓冲，本职
B. 下游不接收（挂了/没人）→ 不该缓冲，断连 + 告警
```

先处理 B：订阅者连续 5 次写失败或心跳超时 → `closed(reason=dead)`，不许继续堆。队列只在 A 起作用。

**信封新增两个可选字段**（来自 source 的 policy）：

```
priority   0 / 1 / 2
fold_key   可折叠键，同键只留最新（例：ci.status:<repo>）
ttl_s      过期即无意义（MC 死亡救援 60、CI 结果 3600）
```

**淘汰规则（训练员定，就一条）**：

```
队列满 → 找「当前队列里最低优先级」那一级，丢掉那级里最早的一条，然后写日志
        优先级顺序：P0 最高（值 0）→ P1 → P2 最低（值 2）
        即：有 P2 先丢 P2 最早的；没有 P2 才丢 P1 最早的；只剩 P0 才丢 P0 最早的
不搞折叠 / 不搞 TTL / 不给 P0 特权（后面真需要了再加，先简单）
```

举例（队列 128 已满）：

```
队列  P0×3  P1×120  P2×5   来一条 P0  → 丢 P2 里最早的那条
队列  P0×3  P1×125  P2×0   来一条 P0  → 没有 P2，丢 P1 里最早的那条
队列  P0×128 P1×0   P2×0   来一条 P0  → 只剩 P0，丢 P0 里最早的那条
```

**换句话说：新消息永远能进，代价由最低优先级的那一级承担。**

**分级配额（2026-11-08 训练员定，统一 128）**：

```
每个订阅者队列     128 条（P0/P1/P2 同额，不做特殊待遇）
满了怎么办        丢「同一优先级里最早的那条」，并留档
不要特殊化 P0     一样按此规则丢；但不许静默——每次丢弃都写日志
```

**留档要求（训练员明确）**：每次丢弃必须写进持久日志（不是只在内存里计数）：

```
{at, subscriber, priority, dropped_event_id, dropped_seq, reason:"queue_full",
 queue_len:128, dropped_total: N}
落盘位置  ~/.hermes/trigger-drops.jsonl（滚动，按天切）
```

**通知（训练员定：不推，只查）**：

```
不向订阅者推送 dropped 事件 —— 丢弃只写 jsonl（SSOT）
下游自己判断漏投：seq 不连续（从 10 跳到 20 = 漏了 11..19）
要查丢弃明细：GET /drops?subscriber=<name>&since=<ISO>&limit=100
```

判据：**系统不主动解释自己，但必须能被问清楚。** 任何“我错过了什么”的问题，都能用一条查询回答。

### 8.7 命名（暂缓，2026-11-08 训练员定）

- 训练员提出过把 trigger 服务改名 `messagebox`（它本质是消息匣，trigger 只是“叫醒”这个动作）。
- **决定：先不改名**，整条流程跑通后再统一改；现在文档与代码继续用 `trigger`。
- 改名时按一次性迁移做：端点别名（`/trigger` 保留）+ 库复制不删旧 + LaunchAgent/插件目录/token 一起换 + 版本 bump。

### 8.8 和官方的差异（知道就行，不追平）

| 维度 | 官方 | 我们 | 为什么不一样 |
|---|---|---|---|
| 抓取方式 | 全轮询 | push 为主（push 型源），pull 型源自己轮询 | 我们要秒级响应（MC 聊天 0.4s 唤醒），轮询做不到 |
| 过滤位置 | 核心层两级（scope + policy） | 客户端 filterwall | 一个源挂掉不拖垮其它源；代价是各客户端要自己实现一遍规则 |
| 声明 | `SourceKind` 对象，UI/授权/健康自动生成 | `source.json` + `/sources` | 我们不做面板，声明只服务校验与健康判定 |
| 失败隔离 | 四个 poller 各自失败 | 每个客户端是独立进程 | 一样，甚至更彻底 |


