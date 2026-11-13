# Worker 状态缺口（router 看不见「人工对话」）

> 2026-11-13 23:02 实测记录。训练员要求：**先记录，等小栗帽忙完再转告**，不要立刻派活。

## 现象

训练员正在飞书里和小栗帽聊天，但 `GET /workers` 里她显示为空闲：

```json
{
  "name": "OguriCap",
  "online": true,
  "busy": false,
  "state": "idle",
  "task_id": "t-39764a83c3",          // 该任务早已 done
  "progress_at": "2026-11-09T19:21:44+08:00",  // 四天前
  "last_seen_iso": "2026-11-13T23:02:13+08:00"  // 心跳是活的
}
```

结果：router 判断她空闲，我这边一派人就插进他们正在进行的对话里（训练员原话：「我和小栗帽正聊着呢，你的消息就过来了」）。

## 根因（两条）

1. **任务完成后不清 `task_id`**：`t-39764a83c3` 已经 `done`，字段仍挂在那里，于是永远显示「上一条任务」。
2. **人工 turn 不改变 `state`**：训练员与小栗帽的对话走 worker 自己的飞书长连接，对 router 完全透明；`busy` 只在 router 派发的任务里翻转。

## 建议修法（供小栗帽参考，未实施）

- worker 心跳增加来源字段：`turn_source: router | trainer`；
- 人工对话进行中把 `busy`（或 `state`）置为占用态，例如 `state=chatting`；
- 任务结束立即清 `task_id`，并同步更新 `progress_at`；
- router 侧：`busy=true` 时把派活排队（`queued`），不打断人工对话；人工对话结束后按队列顺序放行。

## 验收口径

- 训练员在飞书与 worker 对话期间，`GET /workers` 必须显示占用态；
- 对话结束后自动恢复 `idle`；
- 占用期间派任务应返回 `queued`（或 `202`），不得插进对话；
- 任务完成后 `task_id` 为空，`progress_at` 与完成时间一致。
