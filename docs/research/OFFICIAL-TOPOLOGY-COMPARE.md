# 官方 DeepSeek Harness 拓扑 vs 我们的 Trigger / Router / Worker

> 调研时间：2026-11-08 14:57（CST）。官方来源：`github.com/deepseek-ai/deepseek-harness`，本文所有引用钉在 commit `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`（下称「官方仓库」）；官方文档站 `https://deepseek-harness.github.io/deepseek-harness/` 为同一套文档的 VitePress 站。
> 本机实证：`/Applications/DeepSeek Harness.app` 0.2.2-rc.1（arm64），`~/.dsh/profiles/desktop/`。
> 引用标记：**[A]** architecture.zh.md · **[S]** session.md · **[C]** core.md · **[T]** subagent.md · **[AT]** agent-team.md · **[J]** jobs.md · **[G]** goal.md · **[P]** approval.md · **[B]** sandbox.md · **[M]** compaction.md · **[W]** webhook.md · **[ASAR]** 本机 app.asar 字符串原文。
> 凡标 `[推断]` 的是我的判断，不是官方说法。

---

## 零、与已有档案的差异（先读这节）

### 0.1 已有档案已经回答的，本文不重复

| 主题 | 已有档案 | 本文处理 |
|---|---|---|
| 原型的三种扩展点（cordis 插件 / YAML preset / SourceKind）、工具与技能注册 | **A1** `~/projects/superagent-study/REPORT.md` §2–§3；**A2** 技能摘要同题 | 不重写。§三 只在「router ↔ 原型 Router」一行引用 A1 §4 |
| 原型 Router/Worker 的四道墙、工具白名单、并发槽、5 跳回报 | **A1** §4.1–§4.5 | 不重写。§四 的 mandate/隔离判断沿用 A1 §7 |
| 原型 Trigger/Thread/订阅/两级确定性过滤/Effects 队列 | **A1** §5.1–§5.4 | 不重写。本文只澄清：**`thread` 不是 DSH 核心概念**（见 0.3） |
| 原型的上下文：inline 截断 + 读指针 + 分页游标，无 spill / 无向量召回 | **A1** §6.1–§6.6 | 不重写，也不复核其 grep 结论。本文只讲宿主的 compaction seam（§1.6） |
| DSH 安装、`dsh --profile`、ACP stdio、`session/resume`、模型名/端点两个坑、客户端混装 | **B1** `references/dsh-acp-worker-runtime.md` §1–§6 | 不重写。本文只补 **profile ≠ agent preset** 这一层区分（§1.3） |
| 「不要照抄」清单（cordis 运行时、每 ticket 常驻 Session、危险全权、自研面板） | **A1** §7；`SKILL.md`「不要照抄」 | 沿用，不重新论证 |

### 0.2 本文新增（只有这些）

1. **DSH 核心运行时**（不是 superagent 原型）的分层：Cordis 插件树 → profile/bundle/patch → core 包 → capability seam → agent preset。A1/B1 都没系统讲这一层。
2. **事件模型**：三个事件域、`SessionEventMap`、`TurnEndReasonMap` 七种结束原因、durability contract。
3. **Agent 句柄**：`send / followup / steer / inject` 四种投递 + `AgentStatus` 只有 `idle|running` + `whenIdle / runMaintenance / cancel(cause)`。
4. **六个子系统的一手定义**：jobs、goals、approval、sandbox、compaction、webhook runtime——A1 只提到原型怎么用宿主，没有这些字段级语义。
5. **subagent 的 spawn vs fork、`outputSchema`、`maxDepth`、durable catalog；Agent Teams 的任务 DAG（`blockedBy` / `writeScopes` / CAS revision）**。
6. **澄清 `thread` 的归属**：`architecture.zh.md` / `core.md` / `session.md` 三份官方核心文档 grep `thread` 均 0 次命中；它只属于 superagent 原型的领域模型（A1 §5.3）。
7. **本机 asar 实证**：出厂客户端的 `SessionEventMap` 里确实合并了 `job/registered`、`job/settled`、`agent-preset/selected`、`sandbox/mode`、`command/run`、`hook/invoked`；包清单里有 `dsh-experimental-agent-team`、`dsh-experimental-tool-agent-team`（§1.7）。
8. **一处边界修正**：A1 的「worker 是每 ticket 一个常驻 Session」是**原型 executor** 的设计，不是 DSH 核心。DSH 核心的 Agent 是 Session 的驱动器，`AgentStatus` 只有两态，`disposal` 明确「不是第三种可观察状态」[C]。两层不要混。

### 0.3 明确没做的事

- 没有官方拓扑图可抄（见 1.0），本文的层图是按官方文档的包表和 `ctx.*` 键重画的。
- 没运行 DSH 代码、没读 asar 里的压缩 JS（只做 `grep -a` 字符串取证）。
- 没复核 A1 的 spill/recall 零命中结论；原型内部细节以 A1 为准。

---

## 一、官方拓扑（附来源与原文）

### 1.0 有没有官方拓扑图：没有

官方 `docs/architecture.zh.md` 是**文字分层 + 包表 + 一段 turn 流程代码块**，没有一张端到端架构图；文档站也没有 `/architecture` 路由（实测 404）。所以本节的图是**我按官方文字重绘的**：框线摆位属于 `[推断]`，每个概念都能在 1.1–1.6 找到出处。

### 1.1 分层（官方原文）

```
L0 Cordis 内核        插件树；无特权内核，一切注册都是可撤销副作用
L1 启动组装           profile → bundles（有序）→ cordis.patch.yml → home patch → --patch
L2 核心包             session / system-prompt / tools / agent / agent-loop / scope / llm
L3 能力 seam          llm fs shell subprocess terminals sandbox approval jobs
                      webhookRuntime goals commands sessionTitle sessionProjections
                      compaction subagent agentTeams
L4 交互面             Web / CLI(headless) / SDK JSON-RPC / ACP / Desktop(Electron)
L5 上层应用           superagent 原型等 bundle（不在核心里）
```

- **L0**：「产品的每一部分都是插件，包括模型适配器、工具注册表、会话日志，以及 agent loop（智能体循环）本身，因此每个都可以从配置替换。」「不存在需要打补丁的特权内核：扩展 dsh 的方式是把插件挂载到其他插件旁边。」[A]
- **L1**：「**profile** 是存放在 Harness home 中的具名组装。它列出自己叠放的组合包……**组合包**是 Cordis 配置项及其挂载代码的分发格式。」「各层依次应用在空条目列表之上：profile 中有序的组合包、其 `cordis.patch.yml`、home patch，最后是 `--patch` overlay。」随包模板：`web`、`headless`、`sdk`、`sdk-minimal`、`acp`。[A]
- **L2**：核心包表（session / system-prompt / tools / agent / agent-loop / scope / llm）及各自 `ctx` 键，见 [A] 「核心包」表。
- **L3**：「一个 **seam** 是一项可替换能力，包含三种角色：声明接口的 **Service Definition**、实现它的 **Service Provider**、以及使用它的 **Consumer**（通常是面向模型的工具）。」[A]
- **L4/L5**：`dsh` 是唯一受支持的 Node 启动器；桌面端「在签名资源中携带精确匹配的 dsh 生产运行时，并拥有保留的 `$DSH_HOME/profiles/desktop`」[A]；superagent 原型是 bundle，不是内核（A1 已证）。

### 1.2 三个事件域（官方原文）

- 「**会话事件**是追加到日志并通过 `session/event` 广播的持久事实。当某个事实必须在重新加载后仍然存在时，使用它。」
- 「**Agent 事件**（`agent/*`）携带活跃 `Agent`：inbox、步骤、状态、请求、验证、续跑。要观察或拦截进行中的工作时，使用它。」
- 「**能力事件**无需导入循环即可向某个 seam（`fs/*`、`tools/*`、`telemetry/*`）附加策略和适配器。」[A]

判据很干脆：**要活过重启 → session 事件；要看/拦进行中的工作 → agent 事件；要换实现 → 能力事件。**

### 1.3 profile 与 agent preset 是两层，别混

| | profile / bundle | agent preset |
|---|---|---|
| 作用域 | 一个 dsh 进程的整棵插件树 | 单个 Session/Agent 的能力集 |
| 声明 | `package.json` 的 `dsh.profile.bundles` + `cordis.patch.yml` | YAML 插件树（`PresetDefinition`） |
| 生效 | 启动时组装，改配置走 HMR/重启 | Agent 创建时 `mount`，记录 `agent-preset/selected` |
| 官方原文 | 「profile 是存放在 Harness home 中的具名组装」[A] | 「Registry of YAML-declared presets and the revisions live Agents retain.」[C] |

「让某个会话拥有不同的能力集合 → 组装一个 agent preset；其中的服务行需要 `isolate` realm」[A]。**B1 里「每个 worker 一个 profile」只是进程级隔离；要做到「同一进程里不同任务不同工具集」，官方用的是 preset，不是 profile。**

### 1.4 turn / step（官方流程，节选）

```
turn/start
  claim next-step input plus one queued message
  snapshot Session model selection; assemble prompt sections + step-admitted tool schemas
  -> agent/pre-step
     step/start
     agent/request -> prepareCall
     append entered messages as user/message
     stream the bound prepared call -> llm/stream
       assistant/message | assistant/attempt
     tool/call* -> tools/pre-execute -> tools/execute -> tools/post-execute -> tool/result*
     step/end
     tools owe another request, or next-step input arrived -> claim -> next step
  -> agent/turn-stopping
turn/end
```

定义：「一个**步骤**是一次模型请求加上它调用的工具。一个**轮次**包含零个或多个步骤：它在领取首条输入之前打开，并在不再欠下任何工作时关闭。」[A]

### 1.5 Session 日志是唯一事实源

- 「`Session` 是一份由类型化 `SessionEvent` 组成的**仅追加日志**，是 agent（智能体）完整交互历史的唯一真源。」（文档站中文 session.md）
- 「**模型可见即已记录。** 每个模型请求都必须能从日志重建。新增模型可见输入需要会话事件。」[A]
- Durability contract：「durable log persists every event losslessly，每个 Assistant attempt 是一个 `assistant/message` 或 `assistant/attempt`……`seq` stays contiguous」[S]
- 历史是**投影**出来的：`deriveMessages()` 从日志投影模型历史；fork / resume / transcript / 遥测都从持久 settlement 派生 [A][S]。
- `TurnEndReasonMap` 七种结束原因：`completed` / `aborted(reason)` / `blocked` / `error(LlmFailure)` / `max-tokens` / `interrupted` / `forked`；其中 `interrupted` 由崩溃恢复**补写**，`forked` 由 fork seed 合成，「the loop never emits this marker live」[S]。
- 磁盘格式有 generation 与迁移链：`session.vN.jsonl[.zstd]`，提交过的 generation「绝不重命名、替换或删除」[A]。

### 1.6 八个子系统的一手定义（只摘与分层有关的）

| 子系统 | 官方定义（节选） | 关键点 |
|---|---|---|
| **jobs** [J] | `JobStatus = 'running' \| 'stopping' \| 'completed' \| 'killed' \| 'failed'`；`job/registered` / `job/settled` 写在 **owner Session** 里 | 「Turn completion does not imply that every owned job stopped.」job 可以活过 turn/end |
| **subagent** [T] | `startActivation` 统一本地子会话与外部执行；能力位 `agentOptions/agentPreset/outputSchema/depthLimit` | **spawn** 可独立 preset，**fork** 继承父的当前 composition；`outputSchema` 给结构化返回；有 durable catalog，可 cold resume |
| **Agent Teams** [AT] | `TeamTaskSnapshot{revision, status, ownerId, blockedBy[], writeScopes[]}` | 共享任务 DAG；`blockedBy` 必须无环；`writeScopes` 是**建议性路径前缀，不是锁**；每次变更 revision+1 做 CAS |
| **goals** [G] | `GoalPhase = 'active' \| 'paused' \| 'blocked' \| 'complete'`；`GoalRef{id, revision}`；`maxGoalRounds` | 同会话的长期目标，和 turn 分开；activation 是进程内、phase 是持久 |
| **approval** [P] | `ApprovalOutcome = 'allowed-once' \| 'rejected' \| 'cancelled' \| 'unavailable'`；`ApprovalPolicy = 'ask' \| 'never'` | 「Callers fail closed unless it is `allowed-once`」；`unavailable` 也当拒绝；有 `approval/asked` + `approval/decided` 审计对 |
| **sandbox** [B] | `SandboxMode = 'read-only' \| 'workspace-write' \| 'danger-full-access'`；`SandboxEnforcement = 'full' \| 'partial'` | 只管文件效果，不管网络/进程可见性；`partial` 必须如实上报 |
| **compaction** [M] | `compaction/start` / `compaction/summary` / `compaction/end` 三个 **log-only** 事件；摘要通过 `user/message` + `surfaceOp: {op:'replace'}` 进 surface | 不扩 `SurfaceEventType`；`start` 无 `end` 就是崩溃留下的孤儿锁，可检测 |
| **webhook** [W] | 认证后的外部投递 → 可选创建普通 root Session | **「The runtime has no queue, retry, deduplication, execution status, crash replay」**——它故意不做总线 |

### 1.7 本机 asar 实证（出厂 0.2.2-rc.1）

`grep -a` `/Applications/DeepSeek Harness.app/Contents/Resources/app.asar`，原文片段（取自不同位置的字符串，引号形式随原文件）：

```
'agent-preset/selected': { agentPreset: string; };
'job/registered': JobRegisteredData;
'job/settled': JobSettledData;
'sandbox/mode': { mode: SandboxMode; source?: 'delegation'; };
'command/run': { commandId: CommandId; name: string; args?: string; source: ...
'hook/invoked': disposition([
```

另有包清单原文：`"name": "@deepseek-ai/dsh-experimental-agent-team"`、`dsh-experimental-tool-agent-team`、`dsh-experimental-client-ui-agent-team`；`mcpServers` 在 asar 中出现 197 次，`subagent` 3174 次，`agentPreset` 923 次，`ctx.jobs` 95 次，`session/resume` 32 次。

本机 `~/.dsh/profiles/desktop/` 只有四个文件，`cordis.yml` 是空数组，`cordis.patch.yml` 只覆盖 `agent-default-model`（`deepseek-account/deepseek-flash`）和三个 UI 插件——**说明出厂桌面端的层级确实是「空根 + bundle + 用户 patch」，与 [A] 的描述一致**。

---

## 二、我们的拓扑（以任务书为准）

```
origin（飞书/邮箱/MC 聊天/cron；每个源带本地母线）
   │  POST /trigger（一条消息一个请求，元数据随行）
   ▼
trigger 总线 127.0.0.1:8791（只存 / 去重 / 游标 / 叫醒；SQLite）
   │  注入（pre_llm_call） / 唤醒（synthetic turn）
   ▼
super agent（Hermes 主会话：整理信息、判断意图、拆任务）
   │  submit（统一信封）
   ▼
router 127.0.0.1:8790（分发 + 五态状态机 + adapter）
   │  adapter
   ▼
worker（DSH / Codex / Claude / 其它；可回订 trigger 的流）
```

- 任务态固定五个：`queued / running / succeeded / failed / cancelled`；`lease` 过期自动回 `queued`。
- 投递语义：at-least-once + 幂等（同 `event_id` 返回 `duplicate:true`）；每个订阅者独立游标；订阅有周期（默认 3600s，剩 600s 提醒续期）。
- 我们已有的「层」：origin 客户端 / trigger 总线 / super agent / router / worker adapter / 桌宠表现层；状态在 `router.db` 与 `trigger.db`，正文落 `~/.hermes/worker-store/`。

---

## 三、逐项对比表

| # | 层级 | 官方 DSH | 我们 | 差距 |
|---|---|---|---|---|
| 1 | 进程内核 | Cordis 插件树，无特权内核，一切可替换 [A] | Hermes 单体 + 插件 hook + 独立 trigger/router 进程 | **不同范式，不可比**；我们的扩展点是适配器与插件，不是 DI 容器 |
| 2 | 启动组装 | profile → 有序 bundle → patch → `--patch` [A] | 每个 worker 一个 DSH profile（B1 §2）；router 用环境变量/配置文件 | 部分：有 profile，但无 bundle/patch 分层与 HMR |
| 3 | 每会话能力集 | **agent preset**（YAML 插件树 + `isolate` realm + revision 保留）[A][C] | per-worker profile + 工具白名单；无 per-task preset | 部分：隔离粒度只到 worker，不到任务 |
| 4 | 事实源 | append-only `SessionEvent` 日志，`deriveMessages()` 投影 [S] | `router.db` 的 task/event 表 + worker 自带 transcript（DSH 有，Hermes 一次性没有） | 部分：任务级有账，执行级不全 |
| 5 | 轮次模型 | `turn/step` 两级；`TurnEndReasonMap` 七种 [A][S] | task 五态 + `attempts`；无 turn/step 两级 | 部分：粒度粗，分不清「回合结束」与「任务结束」 |
| 6 | 执行体状态 | `AgentStatus = 'idle' \| 'running'`；disposal 不是状态；`whenIdle()` / `runMaintenance()` / `cancel(cause)` [C] | worker 表现层五态 `idle/working/blocked/error/offline`；无 quiescence 概念 | 部分：有状态名，无「真正停稳」信号 |
| 7 | 输入投递 | `followup`（下一 turn）/ `steer`（最近 step）/ `inject`（不唤醒）/ `send(target,wakeup)` [C] | trigger 的「注入」与「唤醒」两种；router 一次性任务书 | 部分：缺 steer 与 next-step 粒度 |
| 8 | 权限 | **per-action approval**，fail-closed，`allowed-once`，`ask/never`，审计对 [P] | **mandate 授权卡**（事前、按 when/may/escalate_if） | **缺运行时门**：mandate 管范围，管不住单次动作 |
| 9 | 隔离 | sandbox 三档 + enforcement `full/partial` 如实上报 [B] | per-worker profile + 目录 + 工具白名单（A1 §3.3 已证原型几乎无隔离） | 部分：没有「写权限档位」这个显式开关 |
| 10 | 后台任务 | `ctx.jobs` + `job_*` 工具；`job/registered`、`job/settled` 入 session log；5 状态 [J] | task 即工作单元，无子 job；长命令靠 adapter 的 timeout | **缺**：turn 结束后仍在跑的进程没有账 |
| 11 | 子代理 | spawn vs fork、`outputSchema`、`maxDepth`、durable catalog、cold resume [T] | `delegate_task` 一次性；result 是自由文本 | 部分：无 fork、无结构化校验、无 catalog |
| 12 | 多 agent 协作 | Agent Teams 共享任务 DAG：`blockedBy` / `writeScopes` / CAS revision [AT] | router task 的 `parent_id` 树；无依赖边 | 缺（但见 §四 判断：只补一列） |
| 13 | 长期目标 | goals：`active/paused/blocked/complete` + revision + `maxGoalRounds` [G] | 无 goal 概念；长期目标在对话/DESIGN 里 | 缺（判断：不该补） |
| 14 | 上下文压缩 | compaction seam，三个 log-only 事件 + `surfaceOp replace` [M] | Hermes 自己的压缩；router 不管上下文 | 部分：能力有，可复现性/审计无 |
| 15 | 外部事件入口 | webhook runtime：**无队列/重试/去重/崩溃重放**，直接建 Session [W] | trigger：at-least-once + 幂等 + 游标 + 订阅周期 | **我们更强**；不要向下对齐 |
| 16 | 崩溃恢复 | `interrupted` 事后补写；generation 迁移；resume [S][A] | lease 过期回 `queued`；task 标 failed | 部分：任务能回队，但「被打断」与「失败」同义 |
| 17 | 工具/技能 | `ctx.tools` + `ctx.systemPrompt` + `SKILL.md`（description 即检索键）[A]（A1 §3） | Hermes tools + skills，机制等价 | 有（等价物） |
| 18 | MCP / hooks | ACP `session/new` 带 `mcpServers`；`hook/invoked` 日志桥 [ASAR] | Hermes 原生 MCP + plugin hooks | 有（等价物） |
| 19 | 可观测 | `session/event` + projections + `approval/asked`、`approval/decided` 审计对 [A][P] | `event` 表 + 日志 + 桌宠状态订阅 | 部分：无统一事件词汇表 |

---

## 四、缺项清单与判断（该补 / 不该补 + 补在哪层）

> 判断基准：单机、单用户、并发 ≤3 的个人场景；不引入企业级调度/权限系统；能借宿主的不自建。

| # | 缺项 | 判断 | 补在哪层 | 理由（一句话） |
|---|---|---|---|---|
| G1 | **每任务可回读的执行日志**（session log 等价物） | **该补** | **worker adapter 契约** | 现在 Hermes/Codex worker 跑完只剩 summary，无法复核「改了什么/为什么失败」；DSH worker 用 `session/resume` 已天然满足，只需把 `session_id + transcript 指针` 写进 result |
| G2 | **任务态与执行体态分离**（idle/running/quiescent） | **该补** | **router** | `succeeded` 只表示这一轮交活，不代表 worker 停稳；桌宠也会把「收尾中」显示成「空闲」 |
| G3 | **后台 job 记账**（`job/registered` + `job/settled`） | **该补（轻量）** | **router + adapter 事件流** | 渲染/转码/长编译会活过 turn；没有 job 账就只能靠超时猜，且无法回答「它还在跑吗」 |
| G4 | **per-action approval 门**（fail-closed、`allowed-once`） | **该补** | **worker adapter 的工具执行前 + mandate 扩展** | mandate 是静态授权，agent 扩范围后仍会执行；个人机器上「删文件/推送/发消息」值得一次确认，不做 UI，越界即 `blocked` |
| G5 | **sandbox 三档**（read-only / workspace-write / danger-full-access） | **该补** | **worker 启动层（profile / adapter 参数）** | B1 已用 profile 做能力边界，但没有「写权限档位」；把「全权」从默认改成显式，成本几乎为零 |
| G6 | **结构化结果校验**（`outputSchema`） | **该补** | **router 信封** | 任务书已有 `output_schema` 字段，缺的是 adapter 落地校验；否则 router 无法程序化验收 |
| G7 | **子代理 fork（带父上下文分叉）** | **部分该补** | **router（adapter 能力位）** | 用于「同一任务换方案重试」；`maxDepth` 一列即可，durable catalog 不必（task 表已有） |
| G8 | **Agent Teams 任务 DAG** | **不该补整套** | router 表加 `depends_on` 一列 | 个人场景并发 ≤3，DAG 调度器/写域建议是过度设计；但「B 等 A」用一列 + ready 判定就够 |
| G9 | **goals（跨轮目标状态机）** | **不该补** | — | 与 task 五态 + super agent 对话重叠，落库会两套账；长期目标写在 DESIGN/日记更合适 |
| G10 | **webhook runtime** | **不该补** | （如接 GitHub，只做 origin 客户端） | 官方自己写明无队列/重试/去重/崩溃重放；我们 trigger 已覆盖且更强，向下对齐是倒退 |
| G11 | **Cordis 插件运行时 / bundle-patch 多层** | **不该补** | — | A1 §7 已判定；Hermes 无 DI 容器，引入成本远大于收益。保留「每 worker 一个 profile」即可 |
| G12 | **compaction seam / image offload** | **不该补（自建）** | — | 压缩归宿主（Hermes / DSH worker）；router 只需保证 result 有 `read:` 指针（已有） |
| G13 | **session fork（turn 边界并行分叉）** | **暂不该补** | — | 只在「同一任务并行试两种方案」时需要；真要做，git worktree 更直接 |
| G14 | **`interrupted` 作为独立结束原因** | **该补** | **router 状态机** | 现在崩溃/超时都归 `failed`；区分「失败」与「被打断」才能决定要不要重试、要不要人看 |

**三条最值得做的补充说明**

- **G1 的关键不是自建日志库**，而是把「执行日志」变成 adapter 的**交付物**：`result.artifacts` 里加 `{type:"transcript", uri, sha1}`。DSH 用 ACP `session/resume`，Codex 用 `--json` 事件流，Hermes 用子会话 id——三种实现，一个契约。
- **G4 与 mandate 是两层，不是替代**：mandate 回答「这类事允不允许」，approval 回答「这一次放不放行」。官方把 approval 做成 fail-closed 的闭集（`unavailable` 也算拒绝），这个默认值比任何 prompt 都可靠 [P]。
- **G3 的最小实现**：`task` 表加 `parent_task`，`event` 表加 `kind=job_started|job_settled`，settle 前 task 不允许进 `succeeded`。不需要独立 `jobs` 表。

---

## 五、最有价值的三条（只说三条）

1. **Session 日志作为唯一事实源，且「模型可见即已记录」。**
   理由：它一次性解决四件事——复核（能重放每一步）、恢复（`interrupted` 补写而不是猜）、审计（审批对入日志）、压缩（摘要也是日志事件）。我们的对应缺口是 G1：worker 只回 summary，等于把「它到底做了什么」丢掉了。补在 adapter 契约，成本最低、收益最大。

2. **任务态与执行体态分离，并用 job 记账表达「turn 结束 ≠ 工作结束」。**
   理由：官方的 `AgentStatus` 只有 `idle|running`，job 却可以活过 `turn/end` [C][J]；我们只有一个五态机，天生会把「后台还在编译」误判为 `succeeded`。这条直接决定桌宠显示是否可信、超时重试是否安全（G2 + G3）。

3. **per-action approval + sandbox 三档，默认 fail-closed。**
   理由：mandate 是事前授权，防不住 agent 在范围内自己扩动作；官方用闭集结果 + `unavailable` 即拒绝把「忘记处理」变成「拒绝执行」[P]，再配三档写权限 [B]，个人机器上的风险面立刻可控。实现只需「白名单 + 越界 blocked」，不需要审批 UI（G4 + G5）。

---

## 六、来源清单

**官方仓库（pinned commit `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`）**

- 架构总览：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/architecture.zh.md
- 会话：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/session.md
- 核心 / Agent 句柄 / preset 注册表：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/core.md
- 子代理：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/subagent.md
- Agent Teams：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/agent-team.md
- 后台任务：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/jobs.md
- 同会话目标：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/goal.md
- 用户审批：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/approval.md
- 进程沙箱：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/sandbox.md
- 压缩：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/compaction.md
- Webhook：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/webhook.md
- 包分组：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/README.md
- CLI/profile README：https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/apps/cli/README.zh.md

**官方文档站（同一套文档，VitePress）**

- 根：https://deepseek-harness.github.io/deepseek-harness/
- 例：https://deepseek-harness.github.io/deepseek-harness/reference/subsystems/session.md （实测 200；`/architecture` 无此路由）

**本机实证**

- `/Applications/DeepSeek Harness.app/Contents/Resources/app.asar`（0.2.2-rc.1，`grep -a`）
- `~/.dsh/profiles/desktop/{package.json,cordis.yml,cordis.patch.yml}`

**已有档案（本文引用但不重复）**

- A1：`~/projects/superagent-study/REPORT.md`（原型 commit `8c32a9dc`）
- A2：`~/.hermes/skills/worker-orchestration-protocol/references/superagent-dsh-prototype.md`
- B1：`~/.hermes/skills/worker-orchestration-protocol/references/dsh-acp-worker-runtime.md`

**未验证 / 限制**

- 官方没有端到端拓扑图；§1.1 的框线摆位是 `[推断]`，概念本身均有引用。
- 未运行 DSH 代码；asar 只做字符串取证，未反编译。
- 官方文档站是 SPA，部分页面路由要靠 `.md` 后缀直达；GitHub pinned blob 是更稳的引用锚点。
