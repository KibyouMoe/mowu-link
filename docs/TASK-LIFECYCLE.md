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

在线取消到达时，worker 在当前回合直接停止该 TaskID 对应的执行；已排队的输入可撤回，已结束的回合可直接核对。停手确认后以原 request_id 回执；收到归档回执后才清本地 TaskID。原生停止拒绝或超时时，保留 TaskID 并记录原因。

用 task_id 和 request_id 对账：Router 写完 SSE 帧记 `cancel_sent`；worker 收到记 `cancel_received`，确认停止记 `cancel_stopped`；Router 接收回执并归档记 `cancel_acked`。到期未获确认记 `cancel_timeout`。

### 打招呼后的等待（与总结索取同一套参数）

```
第 1 次   打招呼后等到它下一次 idle 满 idle_task_end_minutes
第 2 次   再等同样时长
第 3 次   再等同样时长（summary_request_max_attempts=3，含第 1 次）
三次都没等到它的取消回复 → 视为已失联：
- **直接并到②处理：TaskInfo 归档**
- **同时通知溪流**（这单是被判定失联后归档的，不是它自己确认取消的）
```

取消的结果一律是 **TaskInfo 归档**（移出运行中数组），不是改某个状态字段。
不存在 `cancelling` / `paused` 这类中间态。

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

## 异常判定（只有一条）

`bBusy=true` 且 `TaskID=0` **不是异常**：人类跟 worker 聊天、worker 自己在思考，都是这个状态。
这是正常形态，不得报警、不得记 anomaly。

真正的异常只有一种：**worker 上报了一个 Router 手上没有（已归档/已取消）的 `TaskID`**
→ 说明它还在替一张不存在的单干活 → 下发「这单已取消」并要求清零；
纠偏次数要有上限（3 次），到上限后放行派单并告警，不得永久阻塞派单。

## 通知

同一任务同一时刻只允许一条主通知；「任务完成」这类机制通知不再发送。

## 两条索取共用同一套机制

「索取任务总结」与「取消确认」走**同一套机制**（参数与应答通道相同），但**两件事各自独立，不许串成一条链**：

- 同一套等待参数：`idle_task_end_minutes`（每次等多久）+ `summary_request_max_attempts`（共试几次）
- 同一个应答通道：工具调用回执（`submit_deliverable` 那一类），不走私信抄送
- **要求总结就是要求总结**：试满次数仍拿不到总结 → 只报警给溪流，**不转取消、不叠取消次数**
- **要求取消就是要求取消**：试满次数拿不到停手确认 → 视同失联，归档并通知溪流
- 两条线的重试次数各自独立计，互不消耗
