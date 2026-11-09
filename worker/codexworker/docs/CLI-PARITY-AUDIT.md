# CLI 与桌面路径能力差异审计

- 审计时间：2026-11-09 16:00–16:35（CST）
- 仓库：`~/projects/mowulink`，目录 `worker/codexworker/`
- 代码基线：`ac964a0`（含 CLI 飞书表情回执修复）
- 线上实例：`bin/codexworker --config <本机配置>`（backend=cli，port 8905，feishu 已连接）
- 断言口径：只写亲眼看到的代码行、单测结果或实测输出；未实发的飞书卡片/图片操作在文中标明依据。

## 结论摘要

- 桌面路径对外 **53 项**能力：CLI **已接入 9 项**、**部分接入 9 项**、**未接入 35 项**。
- 真正可用的核心链路只有一条：**收文本 → codex exec → 输出卡 → 回复卡**，加上正确透传的 `-m` 和 `exec resume`。
- 最大的结构性差异不是某个功能，而是执行模型：桌面是**常驻管道 + 可轮询的原生会话**；CLI 是**一次性非交互子进程**。因此 token 流、审批、思考摘要、运行中插话、事件重放这些能力不能靠“补一个函数”，要么换 `app-server`，要么接受降级。
- P0 共 9 项，其中 5 项已接入（收文本、输出卡、回复卡、续会话、模型）；**真正缺的 P0 是 4 项**：token 级流式、`/stop` 中断、`/resume` 历史入口、`GET /threads` 的完整视野。图片、审批/提问卡、会话切换属 P1，但都是每天会碰到的“怎么没有”。
- 已验证为“真的通了”的四件事：`-m gpt-6.1-sol` 进 argv、第二轮 `exec resume <id>`、输出卡按 1.8s 节流 PATCH 同一张卡（单测 13/13）、表情回执按 `message_id` 各自加删（`ac964a0`）。

## 一、能力矩阵

| 能力 | 桌面实现位置 | CLI 状态 | 差什么 | 优先级 |
| --- | --- | --- | --- | --- |
| **A. 聊天主链路** | | | | |
| 飞书 p2p 文本收单并执行一轮 | `desktop-manager.mjs:1542-1552, 1609-1654, 756-812` | 已接入 | feishu-cli.mjs:372-384, 471-599；仅 p2p 文本，与桌面一致 | P0 |
| 收到消息立即加表情回执，回合结束移除 | `desktop-manager.mjs:1619-1628, 1656-1671` | 部分接入 | feishu-cli.mjs:393-430（`ac964a0`）。reactionId 只存内存，崩溃/重启后表情留在原消息；桌面把 reactionId 写进 feishuReceipts | P1 |
| 「输出」进度卡：同一张卡持续更新 | `desktop-manager.mjs:1194-1204 + feishu.mjs:294-359` | 已接入 | feishu-cli.mjs:177-273（OutputCard，1800ms 节流，PATCH 失败退化新卡）。实现不同：桌面每拍发新消息，CLI PATCH 同一张卡 | P0 |
| 💭 思考摘要推送 | `feishu.mjs:313-320, 331-344` | 未接入 | `codex exec --json` 不产生 reasoning 事件，CLI 无内容可推 | P2 |
| 文本增量流式（token/段落级） | `desktop-manager.mjs:1180-1184 + FeishuStreamBeat.feedChunk` | 未接入 | 实测 codex 只在消息完成时给整段 agent_message；CLI 无 delta 可转发，tokenStreaming=false | P0 |
| 「回复」终稿卡（正文 + 会话号） | `desktop-manager.mjs:1190-1193` | 已接入 | feishu-cli.mjs:565-573 + markdownPayload 的 note 会话号 | P0 |
| 同 threadId 连续追问（resume） | `desktop-manager.mjs:868-871（send_message_to_thread 带 threadId）` | 已接入 | native-cli.mjs:358-363, 395-404；实测第二轮 argv 为 `exec resume <sessionId>` | P0 |
| 运行中插话（steer） | `desktop-manager.mjs:793-809, 882` | 未接入 | feishu-cli.mjs:458-469 对同一 open_id 串行：后一条必须等前一轮结束，无法送进正在执行的回合 | P1 |
| /queue 与 delivery=queue | `desktop-manager.mjs:769, 778-783, 935-951` | 部分接入 | 只有进程内 per-open_id Promise 链；无 /queue 命令、无队列位置、重启即丢 | P2 |
| 入站消息幂等（重复投递只跑一次） | `desktop-manager.mjs:658-688, 1545-1547` | 部分接入 | feishu-cli.mjs:386-389：内存 seen Map，上限 500，重启后同一 message_id 会重跑 | P1 |
| 入站持久化 inbox（崩溃不丢） | `desktop-manager.mjs:1570-1607（feishu-inbox.json）` | 未接入 | CLI 收到即进内存队列，进程退出则丢 | P1 |
| 消息来源标签（飞书：/超级溪流：） | `desktop-manager.mjs:829-834` | 未接入 | CLI 原样投喂文本，模型无法区分来源 | P2 |
| 发送者白名单 / owner | `desktop-manager.mjs:1544, configureIntegrations 302-312` | 已接入 | feishu-cli.mjs:309-310, 326-330, 379-383；差异：CLI 空名单自动认第一个发送者，桌面要求显式名单 | P2 |
| **B. 斜杠命令与会话控制** | | | | |
| /help /start /commands | `desktop-manager.mjs:995 + feishu-commands.mjs:1-18` | 未接入 | CLI 会把 "/help" 当 prompt 发给模型 | P1 |
| /new（原生新建会话） | `desktop-manager.mjs:1066-1070` | 部分接入 | HTTP POST /threads 只建本地占位会话（首轮才有 codex session）；飞书无命令；无 projectId 语义 | P1 |
| /fork | `desktop-manager.mjs:1071-1078` | 未接入 | HTTP 返 501；但 `codex exec fork <id>` 实际存在，可实现 | P2 |
| /stop 中断 | `desktop-manager.mjs:1056-1061` | 未接入 | HTTP 501。桌面也 501 但会提示去 Codex 点停止；CLI 连子进程 kill 接口都没有，用户无处可停 | P0 |
| /compact | `desktop-manager.mjs:1838` | 未接入 | 两边都 501；桌面明说用客户端原生控件 | P2 |
| /list | `desktop-manager.mjs:999` | 未接入 | 无 | P2 |
| /resume 列表/翻页/搜索/按编号或标题恢复 | `desktop-manager.mjs:1000-1039 + resume-list.mjs` | 未接入 | CLI 无候选收集、无分页、无按名恢复；这是“历史”能力的入口 | P0 |
| /switch 切换选中会话 | `desktop-manager.mjs:1040-1054` | 未接入 | CLI 没有 selectedThreadId 概念 | P1 |
| /status | `desktop-manager.mjs:1055` | 未接入 | 无 | P2 |
| /goal status/pause/resume | `desktop-manager.mjs:765-768 + feishu-commands.mjs:20-27` | 未接入 | goalPrompt 未被 CLI 引用 | P2 |
| /clear（/new 别名） | `desktop-manager.mjs:994` | 未接入 | 随 /new 一起缺 | P2 |
| **C. 历史与标题** | | | | |
| GET /threads 列表 | `desktop-manager.mjs:1782（原生 list_threads）` | 部分接入 | cli.mjs:437-440 只读 SessionStore 自建会话；native-cli.history() 能扫 CODEX_HOME rollouts 但路由没用它，外部 codex 会话不出现 | P0 |
| GET /threads/:id 转录 | `desktop-manager.mjs:1826-1829` | 部分接入 | cli.mjs:510-512 读桥自建 jsonl；无原生 tool/图片节点，无窗口降级 | P1 |
| last-turn | `desktop-manager.mjs:1824-1828` | 部分接入 | cli.mjs:513-517 返回 turn 对象；桌面返回 lastUserText/lastResult | P1 |
| 会话标题/重命名 | `desktop-manager.mjs:1816-1823（set_thread_title）` | 未接入 | 无 PATCH 路由；exec 无 set_title，标题只取首条消息前 80 字 | P1 |
| 选中会话 selectedThreadId / select 路由 | `desktop-manager.mjs:1781, 1815` | 未接入 | cli.mjs:483 把列表第一条当 selectedThreadId，没有选择语义 | P1 |
| adopt 已有 codex 会话 | `desktop-manager.mjs:690-704, 1791` | 已接入 | cli.mjs:493-505 + native-cli.mjs:338-349；需已知 threadId，不能像桌面那样从 list_threads 发现 | P1 |
| 历史窗口降级/超大 turn 处理 | `native-history.mjs:16-48` | 未接入 | CLI 直接读 store，没有 native_snapshot_too_large 语义 | P2 |
| **D. 图片** | | | | |
| 飞书图片入站 → view_image | `feishu-images.mjs:50-74 + desktop-manager.mjs:1633` | 未接入 | feishu-cli.mjs:438-443 明确回「CLI 模式暂只支持文本」；`codex exec -i/--image` 未接 | P1 |
| 公开展示图出站（自动推图片卡） | `public-image-relay.mjs + feishu-image-output.mjs + desktop-manager.mjs:1755` | 未接入 | CLI 无 public journal、无上传/发送链路 | P1 |
| 图片恢复 / 50 分钟去重 | `desktop-manager.mjs:388-410 + SEND_DEDUP_WINDOW_MS` | 未接入 | 无 | P2 |
| **E. 交互与审批** | | | | |
| 提问卡（clarification 发布/选项/提交/过期） | `question-relay.mjs:89-231 + question-cards.mjs` | 未接入 | CLI 没有 MCP 工具面，也没有 /bridge/questions | P1 |
| 卡片按钮回调 | `desktop-manager.mjs:306-308 + feishu.mjs:731-737` | 未接入 | feishu-cli.mjs:331-337 构造 WS client 时没传 onCardAction，点卡只会收到「当前桥不支持」 | P1 |
| 工具审批交互 | `desktop-manager.mjs:465-480（由 Codex Desktop 原生 UI 承担）` | 未接入 | CLI 非交互：approval_policy 固定（默认 never），没有逐命令放行；exec 只有 --approve-for-me 之类的整体策略 | P1 |
| **F. 运维与观测** | | | | |
| 事件流 SSE | `desktop-manager.mjs:1770-1779` | 部分接入 | cli.mjs:466-477 有 SSE 和 15s 心跳；事件只在内存 500 条，seq 不落盘、无 replayFloor，重启不能续传 | P1 |
| 上报 xiliu /report | `desktop-manager.mjs:1502-1529` | 未接入 | CLI 完全没有 outbox/report | P2 |
| 心跳 /heartbeat | `desktop-manager.mjs:1531-1540, 1699-1702` | 未接入 | 无 | P2 |
| 上线公告 | `online-announcement.mjs + desktop-manager.mjs:309` | 未接入 | 无 | P2 |
| /bridge/integrations 与 reload | `desktop-manager.mjs:1759-1760` | 未接入 | 无 | P2 |
| /bridge/runtime 与业务热重载 | `desktop-manager.mjs:1746-1757 + business-runtime.mjs` | 未接入 | 无 | P2 |
| /bridge/text-receipts/abandon | `desktop-manager.mjs:1741-1745` | 未接入 | 无 | P2 |
| writer lease 单写者 | `desktop-manager.mjs:205-239` | 未接入 | CLI 无进程级租约，两个实例会同时写同一 stateDir | P2 |
| Hermes ingress 第二监听 | `desktop-manager.mjs:1686-1695 + hermes-ingress.mjs` | 未接入 | 无 | P2 |
| workspace 根目录约束 | `desktop-manager.mjs:505-514` | 未接入 | native-cli 只检查 cwd 是否存在，任意路径可跑；桌面强制 workspaceRoots | P1 |
| 鉴权强度 | `desktop-manager.mjs:1710-1712` | 部分接入 | cli.mjs:347-352 回环地址无 token 也放行；桌面始终要求 ≥24 字符 Bearer | P2 |
| health / capabilities 自描述 | `desktop-manager.mjs:449-481` | 已接入 | cli.mjs:303-323，字段更明确（capabilities + limits） | P2 |
| 多设备 hostId | `desktop-manager.mjs:1782, read/register 的 hostId` | 未接入 | CLI 只有本地 | P2 |
| **M. 模型与执行参数** | | | | |
| 模型选择 codex.model → `-m` | `Desktop 界面/配置选择模型` | 已接入 | cli.mjs:261 → native-cli.mjs:412；实测 argv 含 `-m gpt-6.1-sol`（首轮与 resume 都有） | P0 |
| reasoningEffort | `Desktop 界面/配置` | 未接入 | native-cli 没有该字段；实测配置 `reasoningEffort: medium`，argv 无 `model_reasoning_effort` | P1 |
| sandbox / approvalPolicy / 代理 / extraArgs | `Desktop 由原生会话管理` | 已接入 | native-cli.mjs:407-414, 102-111；桌面无对应配置项，属 CLI 专有能力 | P1 |

## 二、高优先项实测记录

### 1. 模型参数：`codex.model` 是否真的进 `codex exec -m`

**实测（假 codex 脚本记录 argv）**。配置 `codex.model=gpt-6.1-sol`、`codex.reasoningEffort=medium`，起一个 `--port 0` 的隔离实例（`CODEXWORKER_STATE_DIR` 指到 scratch），发两轮 `/turn`：

```
exec --skip-git-repo-check --json --sandbox workspace-write -c approval_policy="never" -m gpt-6.1-sol -
exec resume 11111111-1111-4111-8111-111111111111 --skip-git-repo-check --json -c approval_policy="never" -c sandbox_mode="workspace-write" -m gpt-6.1-sol -
```

- `codex.model` **已接入**：首轮和 resume 都带 `-m`（`cli.mjs:261` → `native-cli.mjs:412`）。
- `codex.reasoningEffort` **未接入**：`native-cli.mjs` 没有这个字段，argv 里没有任何 `model_reasoning_effort`；`cli.mjs:250-266` 也没有把它传进 adapter。配置里写了等于没写。
- 线上 `/health` 的 `nativeCli.model` 也是 `gpt-6.1-sol`，`tokenStreaming: false`。

### 2. 流式进度：是否真的 1.5–2s 更新同一张卡

**机制实测（假 codex，事件间隔 1s/1s/2s，SSE 记录到达时间）**：

```
0.64s turn.started   0.65s tool.started   1.80s tool.completed
2.96s item.completed "第一段"
5.08s item.completed "第一段，第二段"
5.09s turn.completed
```

- 每个 `item.completed` 都立刻推给 SSE，`OutputCard` 按 1800ms 节流 PATCH 同一条消息；`node --test tests/feishu-cli.test.mjs` **13/13 通过**，其中含 “the 输出 card rewrites in place at most once per stream interval”。
- **但文本不是 token 级**。用真 codex（`codex-cli 0.167.0`）跑一次真实调用并给每行打时间戳：

```
0.25s thread.started
0.76s turn.started
6.29s item.completed agent_message "递归就是把问题拆成更小的同类问题……"
6.31s turn.completed
```

  中间 5.5 秒没有任何文本事件。纯文本回合里，卡上只有 `⏳ 执行中（Ns）`，正文在结束时一次性出现；只有发生工具调用时才有中间行。
- 结论：**卡是真的、节流是真的、流不是**。这是 `codex exec --json` 的事件粒度决定的，不是 `feishu-cli.mjs` 的 bug。

### 3. 追问 / 连续会话：同一 threadId 是否 resume

- 第二轮 argv 实测为 `exec resume <sessionId>`（上面第 1 节第二行）。
- 飞书侧 `~/.codex/state/codexworker/feishu-sessions.json` 持久化 `open_id → threadId`（实测存在，1 个会话）。
- 单测覆盖重启后继续同一 thread：`sessions persist atomically and a restart resumes from the stored thread`。
- 结论：**已接入且可用**。

### 4. 中断（`/stop`）

```
POST /threads/<id>/stop    → 501 {"error":"capability_unavailable","detail":"The native-cli backend does not implement stop."}
POST /threads/<id>/compact → 501
PATCH /manager/sessions/<id> {"alias":"x"} → 404
```

- CLI 没有任何按 turn 终止子进程的入口：`adapter.close()` 只在桥退出时 kill 全部 child；`running` Map 只用于 409 去重。
- 桌面 `/stop` 同样返回 501，但会明确告诉用户“去 Codex Desktop 点停止”。CLI 既没有按钮也没有进程控制，用户完全无处可停。
- 结论：**未接入**；且这是 CLI 相对桌面“更容易补”的一项（child 句柄就在 `native-cli.mjs` 里）。

### 5. 图片输入 / 输出

- 入站：`feishu-cli.mjs:438-443` 对 `message_type !== "text"` 直接拒绝，并回提示「CLI 模式暂只支持文本消息：图片不会转发给 Codex。」；`cli.mjs` / `native-cli.mjs` 全文没有 `--image` / `-i`。
- 出站：`cli.mjs` 没有 `/bridge/images`，`public-image-relay.mjs`、`feishu-image-output.mjs` 从未被 CLI 引用。
- 结论：**都未接入**，且是明确的产品性拒绝，不是漏报错。

### 6. 审批交互 / 卡片按钮

- CLI 构造 `FeishuWebsocketClient` 时只传 `onMessage`、`onConnected`（`feishu-cli.mjs:331-337`），没有 `onCardAction`；`feishu.mjs:731-737` 在没有回调时返回 toast「当前桥不支持此提问卡片」。
- 审批：`approvalPolicy` 只作为 `-c approval_policy="never"` 传给 `codex exec`，非交互进程没有逐命令放行通道；CLI 没有 `--ask-for-approval` 之类的接线。
- 结论：**提问卡、卡片回调、工具审批都未接入**。桌面这边提问卡是完整的（发布/选项/提交/过期/回调去重），审批则本来就由 Codex Desktop 原生 UI 承担，桥不代理。

### 7. `/threads` 历史

- HTTP `GET /threads` 存在，但只读 `SessionStore`（`cli.mjs:437-440`）——即“桥自己建过的会话”。
- `native-cli.mjs` 里明明有 `history()` 会扫 `$CODEX_HOME/sessions/**/*.jsonl` 并合并外部 codex 会话，但 `cli.mjs` 从未调用它（grep 0 命中）。所以直接用 `codex` CLI 开的会话不会出现在列表里，只能靠已知 id `adopt`。
- 飞书侧没有任何 `/threads` 或 `/resume` 命令，用户看不到历史列表。
- 结论：**部分接入**（有列表，但视野比桌面窄）；飞书入口**未接入**。

### 8. 表情回执（正在修的那项）

- `ac964a0` 的 `feishu-cli.mjs` 已经在 `accept()` 里先 `addReaction`，回合 `finally` 里 `removeReaction`；单测 “the trainer's message is reacted first, and the reaction is dropped after the turn” 通过（13/13）。
- 与桌面仍有差距：reactionId 只存 `this.reactions`（内存 Map），进程崩溃/重启后无法把旧表情删掉；桌面把 `reactionId` 落进 `feishuReceipts` 并在 `finishFeishuInput` 里补偿删除。

## 三、统计

| 分组 | 已接入 | 部分接入 | 未接入 | 小计 |
| --- | --- | --- | --- | --- |
| A. 聊天主链路 | 5 | 3 | 5 | 13 |
| B. 斜杠命令与会话控制 | 0 | 1 | 10 | 11 |
| C. 历史与标题 | 1 | 3 | 3 | 7 |
| D. 图片 | 0 | 0 | 3 | 3 |
| E. 交互与审批 | 0 | 0 | 3 | 3 |
| F. 运维与观测 | 1 | 2 | 10 | 13 |
| M. 模型与执行参数 | 2 | 0 | 1 | 3 |
| **合计** | **9** | **9** | **35** | **53** |

按优先级：P0 **9 项**、P1 **21 项**、P2 **23 项**。

## 四、建议补的顺序（8 项）

1. **斜杠命令最小集 + 历史/切换（P0）**。先在 `feishu-cli.mjs` 的 `#turn` 之前加一层命令解析，复用 `feishu-commands.mjs` 的 `BRIDGE_HELP`，实现 `/help`、`/status`、`/new`、`/list`、`/switch`、`/resume`（列表 + 编号恢复）。历史数据现成：`SessionStore.list()` 已经能给出标题/时间/条数，`adapter.adopt()` 能接管 codex 会话；缺的只是“把列表渲染成飞书文本 + 记住候选编号”的会话状态。没有这一层，用户在飞书里打 `/new` 会被当成 prompt 发给模型，这是当前最伤体验的坑。

2. **`/stop` 中断（P0）**。`native-cli.mjs` 持有 child 进程集合，给 adapter 增加 `interrupt(threadId)`：按 thread 找到当前 child，`SIGTERM` 后 2s `SIGKILL`，把 turn 标记为 `interrupted` 并 emit `turn.failed`；`cli.mjs` 把 `/threads/:id/stop` 从 501 改成调用它，飞书 `/stop` 复用同一入口。桌面的 501 是因为管道不给中断能力，CLI 没有这个借口；长任务跑飞时这是唯一止血手段。

3. **图片入站（P1）**。`codex exec` 本身支持 `-i/--image`，`feishu-images.mjs` 的下载/校验/缓存逻辑可以直接复用（只依赖 `notifier.downloadImage` 和 `stateDir`）。在 `#admit` 放行 image，落盘后把 `imagePrompt(filename)` 作为正文、把路径塞进 `dispatch({ images })`，`argsFor()` 里追加 `-i <path>`。注意 resume 模式也要带图，且 `codex exec resume` 的 `-i` 位置要和 `exec` 区分测试。

4. **图片出站（P1）**。这条比入站难：桌面的前提是 `public-image-relay` 从“公开展示的 transcript 节点”里取路径，CLI 没有这个公共日志。最小可行版是只处理**最终回复 markdown 里的本地图片**（复用 `markdownImageSpans`/`localImagePath`），经 `feishu-image-output.mjs` 的上传+发送链路发出，并复用 50 分钟去重表。不要扫描 tool output，否则会把私有路径发出去。

5. **提问卡 + 卡片回调（P1）**。CLI 没有 MCP 工具面，所以“模型主动发提问卡”暂时做不到；但可以先把 `/bridge/questions` 的 HTTP 面 + `QuestionRelay` 搬过来，让外部（Hermes）能发卡，并在 WS client 上补 `onCardAction → questions.acceptCard`。等 `codex exec` 侧有办法把提问送回模型（或改用 app-server）再补“答案投递回原 turn”。这一步的价值主要是让飞书不再是“只能收文本”的单向通道。

6. **入站持久化与幂等（P1）**。现在 `seen` 是内存 Map、队列是内存 Promise 链：进程重启会重跑同一条消息，崩溃会丢已 ACK 的消息。把桌面的 `feishu-inbox.json` + `feishuReceipts` 思路搬过来即可（JSON + 原子 rename，key 用 `feishuMessageId()` 的稳定 digest）。这一项不增加新功能，但决定“敢不敢让它跑长任务”。

7. **参数级补齐：reasoningEffort / fork / compact（P1–P2）**。`reasoningEffort` 只需在 `cli.mjs` 读 `codex.reasoningEffort`、在 `#sharedArgs` 里 `-c model_reasoning_effort="..."`；`fork` 用现成的 `codex exec fork <id>`；`compact` 先确认 `codex exec` 是否有对应子命令，没有就维持诚实的 501。这三项都属“配置里写了但没生效”或“CLI 明明有能力却报 501”，补齐成本低、观感提升大。

8. **运维面：上报/心跳/公告/热重载/lease/ingress（P2）**。这些是桌面为“多设备 + 长期托管”长出来的，CLI 若只作为本机单实例桥，可以最后做。优先做 `writer lease`（防两个实例写同一 stateDir）和 `xiliu` 上报/心跳（Hermes 侧的可观测性）；`/bridge/runtime` 热重载和 Hermes ingress 可以等 CLI 真的承担多设备接入时再说。

## 五、复现方法（无凭据）

```sh
# 1) 假 codex：记录 argv，按固定间隔吐 NDJSON
mkdir -p /tmp/cw-audit/fakebin /tmp/cw-audit/state
cat > /tmp/cw-audit/fakebin/codex <<'EOF'
#!/bin/sh
D="$(dirname "$0")/.."
[ "$1" = "--version" ] && { echo "codex-cli 0.0.0-fake"; exit 0; }
printf '%s\n' "$*" >> "$D/argv.log"
cat > /dev/null
printf '%s\n' '{"type":"thread.started","thread_id":"11111111-1111-4111-8111-111111111111"}'
printf '%s\n' '{"type":"item.started","item":{"id":"i1","type":"command_execution","command":"ls"}}'
sleep 1
printf '%s\n' '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"ls","exit_code":0,"aggregated_output":"ok"}}'
sleep 1
printf '%s\n' '{"type":"item.completed","item":{"id":"i2","type":"agent_message","text":"第一段"}}'
sleep 2
printf '%s\n' '{"type":"item.completed","item":{"id":"i3","type":"agent_message","text":"第一段，第二段"}}'
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":20}}'
EOF
chmod +x /tmp/cw-audit/fakebin/codex

# 2) 隔离配置（不要指向线上 stateDir）
cat > /tmp/cw-audit/config.json <<'EOF'
{"host":"127.0.0.1","port":0,"backend":"cli","workspace":"/tmp/cw-audit",
 "codex":{"bin":"/tmp/cw-audit/fakebin/codex","home":"/tmp/cw-audit/codexhome",
          "model":"gpt-6.1-sol","reasoningEffort":"medium"}}
EOF

# 3) 起实例并观测
CODEXWORKER_STATE_DIR=/tmp/cw-audit/state node cli.mjs --config /tmp/cw-audit/config.json
curl -sS -X POST http://127.0.0.1:<port>/threads -d '{}'
curl -sS -X POST http://127.0.0.1:<port>/turn -d '{"threadId":"<id>","text":"hi"}'
cat /tmp/cw-audit/argv.log          # 看 -m / resume
curl -sS -N "http://127.0.0.1:<port>/manager/events?stream=1"   # 看事件时间线
curl -sS -X POST http://127.0.0.1:<port>/threads/<id>/stop   # 501
```

飞书侧未做实发验证：文档里关于卡片 PATCH、提问卡、图片出站的判断来自代码与单测（`node --test tests/feishu-cli.test.mjs`，13/13），没有向真实飞书聊天发测试消息。
