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
- 三次都拿不到 → 向溪流报警 → 转取消流程。

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

## 通知

同一任务同一时刻只允许一条主通知；「任务完成」这类机制通知不再发送。
