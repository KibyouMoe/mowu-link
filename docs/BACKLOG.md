# 待办总账（BACKLOG）

> 训练员口述的待做项，按记录时间倒序。**只是记录，不代表已排期。**
> 派活前先在这里对齐，避免「事情越积越多但没人知道有哪些」。

## 2026-11-14 新增

### 批量修复清单（一并交小栗帽，别一件件派）

训练员原话：「把问题记个账，之后一股脑给小栗帽让他修」。以下四条同属 worker/router 状态与取消链路，建议一次派完、一次验收：

1. **「取消」不透传到 worker**
   - 现象：`POST /tasks/{id}/cancel` 返回 `{"ok":true,"state":"cancelled"}`，但同一次响应里 `cancel_delivery: {"ok":false,"error":"worker_control_unavailable"}`。
   - 后果：router 侧记 cancelled，worker 侧仍在跑/仍在报结果，两边状态分裂。
   - 期望：cancel 必须送达到执行体（`agent.cancel()` 之类），送达失败要如实返回并保留 `cancelling` 中间态，不能直接标 cancelled。

2. **墙钟 deadline 会掐死在跑的任务**
   - 现象：`deadline_s=180` 的任务在 `turn inProgress` 状态下被判 `failed: 显式 deadline_s 已到期`（11-13 两例：`t-a87d62569a` Rudolf、`t-4a4bad8c63` qicao）。
   - 期望：默认不设 deadline；判卡死只看「长时间无新事件/进度」。传了 deadline 也只是提示，不因它杀在跑的 turn。

3. **任务完成后 `task_id` 不清、`progress_at` 陈旧**
   - 现象：`GET /workers` 里 `task_id` 仍指向已 `done` 的任务，`progress_at` 停在几天前。
   - 期望：完成即清 `task_id`，`progress_at` 同步到完成时刻。

4. **人工对话对 router 不可见**（详见 `docs/WORKER-STATE-GAP-20261113.md`）
   - 期望：心跳带 `turn_source`，人工对话期间置占用态，router 对占用中的 worker 排队而非插播。

5. **人工对话期间派活会直接失败（不只是打扰）**
   - 现象：训练员正在 Desktop 会话里与 worker 聊天时派任务，任务失败为
     `task runner error: Native send_message_to_thread failed; inspect the Desktop task before retrying.`（11-14 01:28，`t-97590760e6`，`qicao-codexworker`）。
   - 说明：这不是「插播打断」，而是原生发送通道被人工会话占用后**直接报错**，任务等于白派。
   - 期望：与第 4 条同源修复——派活前判定该 worker 是否处于人工会话中；是则排队（`queued`）并在人工会话结束后自动放行，绝不能把任务发进去然后失败。
   - 附加：失败时不要只回内部码，要给「需等人工对话结束再重试」这种可执行说明。
   - **补充证据（11-14 01:29）**：训练员停止对话后立刻重发（`t-782747f272`），仍然同样报错
     `Native send_message_to_thread failed`——说明该症状不只在「正在聊天」时出现，**原生发送通道本身已断**；
     排查方向应包含 Desktop 管道存活（进程/会话是否仍绑定），而不只是人工会话占用。



### 自研看板（替代 Trello）
- 训练员补充：界面就放在**我们自己的 Wiki 工作台**（studio，5174）上，不再考虑独立的 Web/CLI/飞书三选一。
- 已在推进：派给 `qicao-codexworker`（笔记本），任务 `t-97590760e6`，v1 范围＝列+卡片+拖拽+标签+截止日+过滤+本地 SQLite，admin 读写 / guest 只读，不接 Trello API。
- 其余待定（同步策略、手机端）等 v1 出来再谈。

## 2026-11-13 新增


### 1. 自研看板（替代 Trello）
- **背景**：目前所有日程、记录、排期都在 Trello 上；训练员希望实现「自己的 Trello」。
- **现状参考**：日报里的 IN_PROGRESS 卡片（外传·贸易吧魔物村制作中 / 基础对话设计 / 主要UI美术设计 / 游戏地图要素完成 / NPC人物美术设计 / 所有作物美术设计 / 第一季基本流程实现 / 所有动物美术设计 / 交易链策划设计）就是从 Trello 读的。
- **待定（开工前必须先问）**：
  1. 与现有 Trello 的关系：只读镜像 / 双向同步 / 彻底替换？
  2. 数据落在哪：`~/.hermes/` 的 sqlite，还是并入 router 的库？
  3. 界面形态：Web（本机 5174 那种）/ 命令行 / 飞书卡片 / 三者都要？
  4. 首版必做能力：看板拖拽、标签、截止日、检查清单、附件、筛选搜索——哪些是第一天就要的？
  5. 多端：手机能不能看（训练员在外用手机派活是常态）。
- **建议路径**：先做「读 Trello → 本地看板展示」的只读版，验证日常是否够用，再谈写回。

### 2. Worker 状态缺口（已记录，待转告）
见 `docs/WORKER-STATE-GAP-20261113.md`（commit 79dda18）。训练员要求等他忙完再转告小栗帽。

## 更早的挂账

- **MC 相关改动拆独立仓库**：`docs/PENDING-DSH-TASKS.md` 与 `server/trigger/clients/mc_chat_client.py` 目前仍在 mowulink 工作区未提交。
- **半人马资产**：马年结束前骑到马之后做（训练员原话「然后我们就把游戏里面的半人马做出来」）。
- **Rudolf 的 desktop-lifecycle 补丁**：collector 分支 `collector/rudolf-desktop-lifecycle-20261113`，21/23 测试通过，2 条失败是 macOS `/private/var` 路径断言问题。
