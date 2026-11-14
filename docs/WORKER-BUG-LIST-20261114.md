# Worker / Router 缺陷清单（交小栗帽）

> 汇总日期：2026-11-14 21:37　来源：训练员要求"一股脑给小栗帽，别一件件派"
> 每条都带实测证据（任务号 / 时间 / 原始报错），不是推测。

## A. 缺陷（按严重度）

### A1　人工对话期间派活直接失败
- **现象**：训练员正在 Desktop 会话里与 worker 聊天时派任务，任务立刻失败：
  `task runner error: Native send_message_to_thread failed; inspect the Desktop task before retrying.`
- **实测**：`t-97590760e6`（11-14 01:28）、`t-782747f272`（01:29）、`t-82a8c3060a`（01:31）三连失败。
- **加重证据**：训练员中断对话后重发，**仍然同样报错**——说明不只是"占用"，原生发送通道本身会断，需要同时排查 Desktop 管道存活。
- **期望**：派活前判定该 worker 是否处于人工会话中 → 排队（`queued`）并在会话结束后自动放行；通道不可用时给出可执行说明（"需等人工对话结束再重试"），不要只回内部码。
- ⚠️ **已知的假失败**（训练员 11-14 22:15 确认）：报 `Native delegated input could not be read completely` 时，**内容其实已经送达 worker**，她能完整看到。所以：
  - 看到这条报错**不要立刻重发**，先问一句「收到了吗」，否则同一工单会被投多次；
  - 真正要修的是"报错与实况不符"——要么投递成功就别报错，要么失败就真别送达。

### A2　人工对话对 router 完全不可见
- **现象**：训练员与小栗帽聊天期间，`GET /workers` 显示 `busy=false, state=idle`，router 判定空闲并插播任务。
- **实测**：11-13 23:02，`OguriCap` 字段为 `online=true / busy=false / state=idle / task_id=t-39764a83c3（该单已 done）/ progress_at=2026-11-09`。
- **期望**：心跳增加 `turn_source: router | trainer`；人工对话进行中置占用态；router 见占用态排队而非插播。

### A3　墙钟 deadline 会掐死在跑的任务
- **现象**：任务处于 `turn inProgress` 仍被判 `failed: 显式 deadline_s 已到期`。
- **实测**：`t-a87d62569a`（Rudolf，11-13 21:39）、`t-4a4bad8c63`（qicao，180s）。
- **期望**：默认不设 deadline；判卡死只看"连续 30 分钟无新事件/进度"。传了 deadline 也只作提示，不杀在跑的 turn。

### A4　「取消」不透传到 worker
- **现象**：`POST /tasks/{id}/cancel` 返回 `{"ok":true,"state":"cancelled"}`，同响应里 `cancel_delivery: {"ok":false,"error":"worker_control_unavailable"}`。
- **实测**：`t-8de207e0f5`（11-14 00:51）。
- **后果**：router 记 cancelled，worker 侧仍在跑，两边状态分裂。
- **期望**：取消必须送达执行体；送达失败要如实返回并保留 `cancelling` 中间态，不能直接标 cancelled。

### A5　任务完成后 task_id 不清、progress_at 陈旧
- **现象**：`GET /workers` 的 `task_id` 仍指向已 `done` 的任务，`progress_at` 停在数天前。
- **实测**：11-13 23:02，`task_id=t-39764a83c3`（已 done）、`progress_at=2026-11-09T19:21`。
- **期望**：完成即清 `task_id`，`progress_at` 同步到完成时刻。

## B. 功能需求

### B1　Wiki 自动同步（自动提交 / 自动拉取 / 冲突上报）
- **目标仓库**（本机，同一台 Mac mini，可直接读）：
  - workdir `/Users/kibyou/projects/HonorOfLord-Wiki`，branch `main`
  - remote `git@github.com-honor:KibyouMoe/HonorOfLord-Wiki.git`（SSH 别名 `github.com-honor`，已在 `~/.ssh/config`）
- **现有拉取入口**（就是今天报「拉取失败」的那个）：
  - 后端 `studio/server.mjs:201-263`：refresh 路由执行 `git pull --ff-only`，前端按钮「拉取更新」在 `studio/app.js:200`；服务端 `.mjs` 有变动时校验语法后退出进程，由 launchd 拉起
  - 服务 `com.kibyou.hol-studio`（`~/Library/LaunchAgents/com.kibyou.hol-studio.plist`），端口 5174
- **今天失败的原因**：本地 ahead 2 / 远端 ahead 19，`--ff-only` 无法快进 → `fatal: Not possible to fast-forward`。已手工 rebase + push 修复（`fec68f2e`）。
- **口径**：本仓只放文档/策划案，逻辑改动另开仓库 → 自动提交可放宽，不必逐条人工审。
- **自动提交**：有变更 → 按目录归类提交（消息写清改了什么），普通 push，绝不 force。
- **自动拉取**：`fetch` + `pull --rebase --autostash`，避免本地攒提交导致 `fatal: Not possible to fast-forward`（11-14 已实际踩到，网页端自动更新因此失败）。
- **冲突处理**：不自动猜——保留现场（备份分支带时间戳），把冲突文件清单/diff 摘要作为一条任务提交 router，由溪流判断后分派，处理完再续。
- **上报字段**：`repo / branch / conflicted files / 起始 commit / 备份 ref`。
- **安全线**：只允许 ff 或 rebase；禁 `reset --hard`、禁 force push。

## C. 附带项

### C1　collector 分支 2 条 macOS 断言
- 分支 `origin/collector/rudolf-desktop-lifecycle-20261113`，21/23 测试通过。
- 2 条失败同因：断言直接比较路径字符串，macOS 上 `/var/...` 与 `realpath` 后的 `/private/var/...` 不等（代码行为是对的）。
- **修法**：断言前先 `realpathSync()`，或比较时忽略 `/private` 前缀。

## D. 验收口径（每条修完都要能这样验）

| 项 | 验收方式 |
|---|---|
| A1/A2 | 训练员在飞书与 worker 对话期间派活 → 返回 `queued`，对话结束后自动执行；`/workers` 显示占用态 |
| A3 | 派一个跑 5 分钟的任务 → 不设 deadline 时不失败；设了 deadline 也不该杀在跑的 turn |
| A4 | cancel 后 worker 侧确实停止（查 worker 日志/进程），router 状态与之一致 |
| A5 | 任务 done 后立即查 `/workers`：`task_id` 为空、`progress_at` = 完成时间 |
| B1 | 本地改一个 md → 自动提交并 push；制造冲突 → 自动生成备份分支 + router 收到一条冲突任务 |
