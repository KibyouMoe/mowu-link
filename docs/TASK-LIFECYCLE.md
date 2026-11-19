# 任务生命周期规格（2026-11-18 训练员定稿）

## 两条重试节奏（训练员 2026-11-19 原文）

```
索取  立刻发一次 → 没回执 → 攒满闲置 30 秒再发 → 次数用尽：报警 + 进入取消状态
取消  立刻发一次 → 没回执 → 攒满闲置 30 秒再发 → 次数用尽：归档 + 报警
```

**空闲计时的清零规则（训练员 2026-11-19）**：只要看到 `bBusy=true`，累计的空闲计时立刻清零。
忙说明它在干活，闲着的那段不能算数；必须等它重新回到 idle 再从 0 攒满 30 秒。

关键差别：**索取次数用尽时只报警、不归档**，把任务转进取消状态，由取消流程去收尾；
**取消次数用尽才归档 + 报警**，结束。

## 六条铁律（训练员 2026-11-19 原话定稿，任何实现不得偏离）

```
1) worker 手上恒一个 TaskID，不存在两单/待处理列表
2) 派单只看 TaskID，bBusy 不参与；为0就立刻插进去，排队只在 Router
3) 取消只看 TaskID，bBusy 不参与；对就立刻插进去，不许"等跑完再确认"
4) 索取发出去不管，攒满 idle 才重发，收到即停
5) 取消发出去不管，攒满 idle 才重发，收到即停
6) 全局唯一时间参数 idle_task_end_seconds=30，没有别的"等多久判失败"
```

## 数据结构

```
TaskInfo         TaskID / TaskName / TaskDesc / TaskOwner
WorkerStatus     bBusy / TaskID
```

- 没有 `bRunning`、没有 `reviewing / done / failed / cancelled` 这类状态字段。
- 任务是否在跑，看它是否还在“正在运行任务”数组里；取消 = 归档，移出该数组。
- Router 只观察 worker 的 `bBusy` 与 `TaskID`，观察止步于此。
- **`TaskOwner` 一旦生成就不再改**：任务固定属于派出去的那个执行人；要换人做，就重新生成一个新任务（新 TaskID）。

## 派单

Router 生成 `TaskInfo` → 发起 Dispatch 协议把 TaskInfo 交给目标 Worker → Worker 收下，
`WorkerStatus.TaskID` 即该任务。Worker 长期持有，直到成功收尾或取消。

goal 模式由 worker 自己决定，Router 不管。

**派单前的判断只看目标 worker 的 `TaskID`，不看忙闲**（`bBusy` 的唯一用途是累计空闲、判断这一轮是否结束；它不 gate 派单，也不 gate 取消）：

- `TaskID == 0` → **直接派**
- `TaskID != 0` → **排队**，等它手上的单收尾或取消后自动派发

## 正常结束

触发：Router 发现该 worker `bBusy=false` 满 `idle_task_end_seconds`（默认 30 秒，配置文件可调），
且 `WorkerStatus.TaskID != 0` → 判定任务已结束。

行为：

1. 执行任务结束流程，向 Worker 索取任务总结；
2. 拿到总结 → `WorkerStatus.TaskID` 置 0；
3. `TaskInfo` 与总结一起打包发给溪流；
4. Router 内 `TaskInfo` 归档，移出“正在运行任务”数组。

**验收不在 Router 侧发生**：TaskInfo 已归档，后续由溪流自己处理（要返工就开新单）。

## 总结索取的重试

```
第 1 次   判定结束后立刻索取
第 2 次   等该 worker 再次 idle 满 idle_task_end_seconds 后重试
第 3 次   再等满同样时长后重试
```

- 次数上限 `summary_request_max_attempts`（默认 3，含第 1 次），可配置。
- 最后一次索取后再攒满一个空闲阈值仍无总结 → 归档并向溪流报警一次。取消须单独发起。

## 取消

```
① 连接正常
   Router 打招呼「这单要取消」
   → worker 回「好，我自己取消」
   → worker 自己发 cancel 命令
   → Router 收到 → TaskInfo 归档

② 已失联
   Router 直接 TaskInfo 归档（不等待、不设中间态）
   → 该 worker 下次注册时带着手上的 TaskID 来问「这单还在吗」
   → Router 回「已取消」→ 自动下发取消提示
   → worker 回「好，我取消」，自己清掉本地 TaskID
```

在线取消到达时，worker 在当前回合直接停止该 TaskID 对应的执行；已排队的输入可撤回，已结束的回合可直接核对。Worker 以原 request_id 回执，收到归档回执后立即持久清零。缺少原生停手能力时封存本地执行，回执标 `execution_stopped=false`。

**取消只看 TaskID，不看忙闲**（训练员 2026-11-19）：Router 核对该 worker 手上的 TaskID 是否就是这单——是，就立刻把取消消息插进去，让它优先处理；忙不忙都不影响下发。worker 收到后必须把这条消息排到队首立即处理，停手、确认、清 TaskID。无原生停手能力时也立即回执并清号。

用 task_id 和 request_id 对账：Router 写完 SSE 帧记 `cancel_sent`；worker 收到记 `cancel_received`，确认停止记 `cancel_stopped`；Router 接收回执并归档记 `cancel_acked`。安排下一次请求时记 `cancel_retry`，索取次数耗尽记 `request_exhausted`。

### 取消的重发

```
第 1 次   worker 持有本单时立即发取消
后续      请求已写出，收到新的匹配 TaskID 心跳且回执未到时优先重发
次数上限  summary_request_max_attempts（默认 3，含第 1 次）
次数用尽，且新心跳显示仍持有本单 → 视为已失联：
- **直接并到②处理：TaskInfo 归档**
- **同时通知溪流**（这单是被判定失联后归档的，不是它自己确认取消的）
```

取消的结果一律是 **TaskInfo 归档**（移出运行中数组），不是保留一个终态。

## 注册握手

注册携带本地 TaskID。仍由该 worker 持有时，Router 返回「这单还在」、`resume_task_id` 与 `resume_prompt`；连接后复用原任务及派发回执续接，保持同一 TaskID。若该单正在等取消确认，匹配 TaskID 的新连接优先重发同一个取消请求 ID。已取消的单返回 `clear_task_id` 和取消提示；已收总结的单返回归档确认。TaskID 为 0 时正常注册。

## 心跳

Worker 每 **5 秒**上报一次 **完整的 `WorkerStatus`**（`bBusy` + `TaskID` 两个字段都必须带），
不省略字段、不做增量上报。Router 侧以这份心跳为唯一观察来源，
`TaskID` 在 worker 手上不清就一直报，直到 Router 确认收尾或取消。

**`TaskID` 与回合（turn）必须解耦**：它属于任务，不属于某一次回合。
回合结束、goal 模式重启、会话切换，都不影响 `TaskID` 的持有；
只有 Router 确认「总结已收到」或「这单已取消」两件事，才允许清。

### 空闲计时（**由 Router 计时，Worker 只报状态**）

- Worker：每 5 秒产生一个 `WorkerStatus` 采样（`bBusy` + `TaskID`），推送一份；
  Router 主动来要数据时，把这一整串序列甩过去
- **Router 侧维护 `idle_since`**：
  - 收到「`bBusy=false` 且 `TaskID != 0`」→ 记下 `idle_since`（已在计时中就保持不动）
  - 其他任何状态（busy / TaskID=0）→ 清掉 `idle_since`，不累计
  - 计时满 `idle_task_end_seconds` → 判任务结束，进入索取总结
- Worker 不自算时长、不自判结束；它只负责如实报

## 任务对外只有两个状态：运行中 / 取消中

Router 侧对一张任务只保留两个状态（训练员 2026-11-19 定）：

```
running     运行中：派发出去、等它自己收口
cancelling  取消中：已经向 worker 要取消协议，正在等回执
```

进入 `cancelling` 的条件是溪流或训练员下发取消。Router 根据运行中任务和已记录的取消请求返回这两个状态。排队和归档记录没有运行状态。

**取消指令存在的意义**（训练员 2026-11-19）：
取消指令不是流程仪式，**它是为了让 worker 知道"这单已经被取消了"，从而清掉自己手上的 TaskID**。
所以判断标准只有一个：**worker 手上到底有没有这张单**。

```
worker 持有这张 TaskID  → 立刻下发取消指令（让它知道并清掉），优先插入，不等它空闲
worker 没有这张 TaskID  → 直接归档，一条指令都不用发
                         （它压根没接这单，没有"不知道被取消"的问题）
```

`cancelling` 期间的行为：
- **不得索取总结**（这张单已经不要了，不是正常收尾）
- 仍持有时，新心跳触发优先重发，只核对 TaskID
- 无持有时：**立即归档**（reason=worker_cancel / cancelled_without_ack），不等回执、不等重试
- 收到回执 → 清 TaskID、归档（reason=worker_cancel）
- 重试耗尽仍无回执 → 视同失联：归档 + 通知溪流（reason=archive_unconfirmed）
- 归档后若 worker 仍报旧 TaskID，走清零纠偏。

空闲时长用于正常收口。取消由请求和匹配的 TaskID 心跳驱动。

**正常收口不经过 `cancelling`**：空闲满 `idle_task_end_seconds` → 索取总结 → 归档。

## 索取请求不设时间限制（训练员 2026-11-19 定，最终口径）

**这套重试的目的只有一个：防止 worker 一直收不到 Router 的消息。**
它不是惩罚执行慢，也不是给请求设截止时间——只是"没送到就再送一次"。

Router 向 worker 索取总结：**发出去就不管了**，不给这条请求设超时。

发完那一刻，Router 侧把这一轮**当成繁忙**处理：

```
发送总结请求
  → 立刻把该 worker 的 idle 计时清零（默认他这一轮"在忙"）
  → 若他仍然闲着，就从这个 0 重新累计
  → 闲置时间满 idle_task_end_seconds → 触发下一次协议重试
  → 每次重发都再清零，如此循环
连续 N 次都要不到（N = summary_request_max_attempts）→ 归档 + 报警
```

**收到回应就立刻停止重试**：他可能 1 秒内就处理完并回了——
这时 pending 立刻清掉，哪怕紧接着又 idle，也不许再发同一条请求。
重发只发生在「没拿到回应」这一种情况。

**整套里唯一与时间有关的参数只有一个：`idle_task_end_seconds`**。
请求发出后只累计经心跳确认的空闲时长。

## 异常判定（只有一条）

`bBusy=true` 且 `TaskID=0` **不是异常**：人类跟 worker 聊天、worker 自己在思考，都是这个状态。
这是正常形态，不得报警、不得记 anomaly。

真正的异常只有一种：**worker 上报了一个 Router 手上没有（已归档/已取消）的 `TaskID`**
→ 说明它还在替一张不存在的单干活 → 下发「这单已取消」并要求清零；
纠偏次数要有上限（3 次），到上限后放行派单并告警，不得永久阻塞派单。

### worker 自己把单丢了（TaskID 回到 0，但没交成果）

触发：Router 手上还有一张「已派发未归档」的单，而该 worker 心跳报 `TaskID=0`。 取消中的任务继续走取消流程。首次交接尚未获得 worker 持有确认时，Router 从 SSE 写出派单帧起等待一个心跳周期，以免旧的空闲采样误归档新单。

处理（训练员 2026-11-19 定）：**Router 直接把自己这张单归档**，不追问、不重发索取。

```
归档 reason = worker_dropped_task
worker 侧：TaskID 已是 0，无需再清
溪流侧：收到一条归档通知即可（不必等成果）
```

不允许出现「单子永远挂在 running、没人问、也没人管」的状态。

如果旧数据里只占了 active_tasks，缺少派发记录且 worker 未持有该 TaskID，Router 将该任务放回派单队尾，先派已有队首。待派任务保持原 TaskID。Router 在同一事务记录新任务的占位和派发事实。

## 通知

同一任务同一时刻只允许一条主通知；「任务完成」这类机制通知不再发送。

## 两条索取共用同一套机制

「索取任务总结」与「取消确认」走**同一套机制**（参数与应答通道相同），但**两件事各自独立，不许串成一条链**：

- 两种请求共用次数配置 `summary_request_max_attempts`。只有总结使用空闲阈值 `idle_task_end_seconds`。
- 同一个应答通道：工具调用回执（`submit_deliverable` 那一类），不走私信抄送
- **要求总结就是要求总结**：试满次数仍拿不到总结 → 归档并报警给溪流，**不转取消、不叠取消次数**
- **要求取消就是要求取消**：试满次数拿不到停手确认 → 视同失联，归档并通知溪流
- 两条线的重试次数各自独立计，互不消耗

- **worker 手上有且只有一个 TaskID**（训练员 2026-11-19）：要么是 0（没有任务），要么是某一个具体任务；不存在「手上两单」，也不存在「待处理列表」。这是整套模型的核心不变量。
- **worker 手上不能有排队的信息**（训练员 2026-11-19）：worker 一次只处理一件事，收到的消息立刻处理，自己不许积压队列。所有排队都在 Router 的 `dispatch_queue` 里完成——TaskID 为 0 才下发，TaskID 非 0 就不发，因此 worker 永远不会同时拿到两张单。
- 排队的活只存在于 Router 的 `dispatch_queue`
