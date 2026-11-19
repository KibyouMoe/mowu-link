# 任务生命周期规格（2026-11-18 训练员定稿）

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

**派单前的判断只看目标 worker 的 `TaskID`，不看忙闲**：

- `TaskID == 0` → **直接派**
- `TaskID != 0` → **排队**，等它手上的单收尾或取消后自动派发

## 正常结束

触发：Router 发现该 worker `bBusy=false` 满 `idle_task_end_minutes`（默认 1，配置文件可调），
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
第 2 次   等该 worker 再次 idle 满 idle_task_end_minutes 后重试
第 3 次   再等满同样时长后重试
```

- 次数上限 `summary_request_max_attempts`（默认 3，含第 1 次），可配置。
- 最后一次等待期满仍无总结 → 向溪流报警一次，任务保留，可继续接收总结；取消须单独发起。

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

在线取消到达时，worker 在当前回合直接停止该 TaskID 对应的执行；已排队的输入可撤回，已结束的回合可直接核对。停手确认后以原 request_id 回执；收到归档回执后才清本地 TaskID。Codex 缺少原生停手能力时，worker 停止任务的后续派发，并用同一个取消请求回报「已收到，本轮结束后停」。Router 记录 `cancel_deferred`，保留 TaskID。确认该回合已退出后，worker 走同一成果回执接口提交 `reason=user_cancel`，再清 TaskID。原生停止出现其他错误时，保留 TaskID 并记录原因。

用 task_id 和 request_id 对账：Router 写完 SSE 帧记 `cancel_sent`；worker 收到记 `cancel_received`，确认停止记 `cancel_stopped`；Router 接收回执并归档记 `cancel_acked`。到期未获确认记 `cancel_timeout`，安排下一次请求时记 `cancel_retry`。

### 打招呼后的等待（与总结索取同一套参数）

```
第 1 次   worker 持有本单时立即发取消
第 2 次   worker 仍持有且空闲满 idle_task_end_minutes 时重发
第 3 次   再等满同样的空闲间隔（含第 1 次最多 3 次，配置更低时从低）
三次都没等到它的取消回复 → 视为已失联：
- **直接并到②处理：TaskInfo 归档**
- **同时通知溪流**（这单是被判定失联后归档的，不是它自己确认取消的）
```

取消的结果一律是 **TaskInfo 归档**（移出运行中数组），不是保留一个终态。

## 注册握手

注册携带本地 TaskID。仍由该 worker 持有时，Router 返回「这单还在」、`resume_task_id` 与 `resume_prompt`；连接后复用原任务及派发回执续接，保持同一 TaskID。若该单正在等取消确认，重连时重发同一取消请求。已取消的单返回 `clear_task_id` 和取消提示；已收总结的单返回归档确认。TaskID 为 0 时正常注册。

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
  - 计时满 `idle_task_end_minutes` → 判任务结束，进入索取总结
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
worker 持有这张 TaskID  → 发取消指令（让它知道并清掉），空闲时轮询重发，等回执
worker 没有这张 TaskID  → 直接归档，一条指令都不用发
                         （它压根没接这单，没有"不知道被取消"的问题）
```

`cancelling` 期间的行为：
- **不得索取总结**（这张单已经不要了，不是正常收尾）
- 有持有时：**空闲轮询下发**，只要 worker 闲下来就再发一次，然后等它下一次空闲
- 无持有时：**立即归档**（reason=worker_cancel / cancelled_without_ack），不等回执、不等重试
- 收到回执 → 清 TaskID、归档（reason=worker_cancel）
- 重试耗尽仍无回执 → 视同失联：归档 + 通知溪流（reason=archive_unconfirmed）
- **从第一次请求起最多 180 秒**。持续忙碌、心跳静默和重连均不延长截止时间。到期归档为 `archive_unconfirmed` 并通知溪流，释放 Router 派单位。worker 仍报旧 TaskID 时走既有清零纠偏。

**同一个触发点，按状态分派动作**（训练员 2026-11-19）：

```
Worker 闲下来
├─ 状态 running    → 发「成果总结」请求
└─ 状态 cancelling → 再发一次「取消指令」
```

也就是说「闲下来该做什么」由任务状态决定，不是两套计时器。

**正常收口不经过 `cancelling`**：空闲满 `idle_task_end_minutes` → 索取总结 → 归档。

## 送达与超时必须分开（训练员 2026-11-19 定）

Router 发出的每一条请求都要先回答一个问题：**它的回合到底起没起来？**

```
有回合证据（拿到 turnId / turn_started）
  → 才能进入"等成果"的计时；等不到才叫「超时」

没有回合证据
  → 这不是超时，是「未送达」：请求根本没到她模型手上
  → 立即重发或换通道，不计入超时次数、不干等
```

判断依据只能是**原生侧证据**（turnId / turn_started），
`dispatch_confirmed`（worker 说我持有这张单）只证明 worker 层收到，不证明模型看到。

处理规则：
- 未送达 → 立刻重发（同一次尝试内），连续 3 次未送达 → 换通道/换会话，仍无 → 报警
- 已送达但无成果 → 按 `idle_task_end_minutes` 等，等满才计一次超时

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

- 共用 `idle_task_end_minutes` 和 `summary_request_max_attempts`。取消额外受最多 3 次、总计 180 秒的限制
- 同一个应答通道：工具调用回执（`submit_deliverable` 那一类），不走私信抄送
- **要求总结就是要求总结**：试满次数仍拿不到总结 → 只报警给溪流，**不转取消、不叠取消次数**
- **要求取消就是要求取消**：试满次数拿不到停手确认 → 视同失联，归档并通知溪流
- 两条线的重试次数各自独立计，互不消耗
