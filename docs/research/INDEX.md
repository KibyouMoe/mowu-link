# 调研档案索引（Super Agent / Router / Worker / DSH）

> 用途：**任何调研开工前先读这里**。已有的先复用，只有真增量才新建文件；新文件必须在下面登记。
> 违反后果：同一题目被反复派子代理重复调研（2026-11-08 已发生 5~6 次，训练员当场指出）。

## 怎么用（硬规则）

1. 派调研子代理前，`grep` 本索引关键词；命中就先读对应文件，任务书里写「基于已有 §X，只补 Y」。
2. 调研结果**合并进既有文件**优先于新建文件；确实新题才新建，并立刻在此登记（日期 + 来源 + 路径 + 一句话结论）。
3. 每条登记必须写**来源**（仓库 + commit / 文档 URL / 实测环境），否则下次无法判断是否过期。
4. 结论与决策分离：调研档案只放"官方/源码怎么说"，我们的取舍写 `ARCHITECTURE.md`。

---

## D. 定稿文档（我们自己的整理，不是官方翻译）

### D0. `TOPOLOGY.md` — 拓扑定稿
- **路径**：`~/projects/mowulink/TOPOLOGY.md`
- **回答**：五层拓扑图（sources / trigger / super agent / router / worker）、每层职责与禁止事项、五个状态机（task / worker / job / subscription / approval）、任务与结果契约、官方概念落位表、缺口落位图、最小上线顺序。
- **性质**：**[自定]** 整理稿，官方只有子系统文档、没有整体拓扑图。
- **日期**：2026-11-08

### D1. 官方 Super Agent 拓扑图（SVG + PNG）
- **路径**：`~/projects/mowulink/diagrams/official-superagent-topology.svg` / `.png`（3200×2360，2x）
- **回答**：官方原型（`dsh-superagent-prototype`）的完整拓扑：四段读图（输入准入 → 领域状态 → 双 preset → 驱动与存储）、五概念主循环、三级过滤、Report 五跳回传。
- **来源**：A1 报告 + `superagent-dsh-prototype.md`，非官方图（官方没有整体拓扑图）。
- **日期**：2026-11-08

## A. 官方原型源码拆解

### A1. `dsh-superagent-prototype` 完整报告
- **路径**：`~/projects/superagent-study/REPORT.md`（31.8 KB，391 行）
- **回答**：官方 super-agent 原型的插件/技能机制、Router/Worker 边界、上下文管理、状态与事件；哪些论文设计其实没实现。
- **来源**：`github.com/deepseek-ai/dsh-superagent-prototype`，commit `8c32a9dc5d72c033816c6a8110b37f40cf94fba4`（2026-10-31，单 commit，MIT），纯静态阅读，未运行。
- **三句结论**：① 不是插件系统，是 bundle + 三种扩展点（cordis 插件 / YAML preset / 数据声明的 SourceKind）；② Router/Worker 靠宿主边界隔离，不靠 prompt；③ 上下文是 inline 截断 + 可寻址读指针 + 分页游标，**没有 spill 文件、没有链式召回**。
- **原文仓库副本**：`~/projects/superagent-study/dsh-superagent-prototype/`
- **日期**：2026-11-08

### A2. 同题精简版（技能内引用）
- **路径**：`~/.hermes/skills/worker-orchestration-protocol/references/superagent-dsh-prototype.md`（4.9 KB）
- **定位**：A1 的**摘要**，供技能加载时快速引用；细节以 A1 为准。两者内容重叠，**不要第三次重写同题**。

## B. 运行时与接入

### B1. DSH 本机安装 + ACP worker 运行时
- **路径**：`~/.hermes/skills/worker-orchestration-protocol/references/dsh-acp-worker-runtime.md`（9.3 KB）
- **回答**：DSH 怎么装、ACP stdio 怎么当常驻 worker（`session/new|prompt|resume`）、stdout 只走协议的坑。
- **实测环境**：macOS arm64，dsh 0.2.2-rc.1。
- **注**：该文写于 npm 全局安装时期；**2026-11-08 已改为客户端版**（`/Applications/DeepSeek Harness.app`，CLI 由客户端菜单「Manage dsh Command…」生成 `/usr/local/bin/dsh`），npm 版已卸载。ACP 部分仍适用，安装部分以本索引 C1 为准。

### B2. Trigger 总线实现
- **路径**：`~/.hermes/skills/worker-orchestration-protocol/references/trigger-bus-implementation.md`（15 KB）
- **回答**：8791 服务端 + 插件注入/唤醒的实现细节、单实例锁、游标、优先级 bug 等实战坑。

## C. 本机现状（实测，非调研）

### C1. DSH 客户端版（当前形态）
- **形态**：`/Applications/DeepSeek Harness.app` 0.2.2-rc.1（arm64），DMG `/tmp/dsh-desktop.dmg`
- **CLI**：`/usr/local/bin/dsh` → 应用内 `runtime/cli/bin/dsh`；`dsh --version` = 0.2.2-rc.1；安装入口是应用菜单 **Manage dsh Command…**（GUI 安装要管理员密码，已用同款安装器 + 免密 sudo 完成）
- **技能目录**：`~/.dsh/skills` = `KibyouMoe/DSHSkillSync` 仓库（29 个技能）
- **本地 API**：`127.0.0.1:57698`，需内部 token（401），暂不使用
- **日期**：2026-11-08

### C2. Worker 编排技能（协议与决策）
- **路径**：`~/.hermes/skills/worker-orchestration-protocol/SKILL.md`
- **回答**：Router 守则、Trigger 层、快路径、mandate、状态层、适配器要点、主数据流（sources → trigger → super agent → router → worker）。

---

## D. 进行中

（暂无）

## E. 官方拓扑对比

### E1. 官方 DSH 核心拓扑 vs 我们的 Trigger/Router/Worker
- **路径**：`~/projects/mowulink/research/OFFICIAL-TOPOLOGY-COMPARE.md`（278 行）
- **回答**：官方 DSH **核心运行时**（非 superagent 原型）的分层、事件模型、Agent 句柄、subagent/Agent Teams/jobs/goals/approval/sandbox/compaction/webhook 的一手定义；与我们的 sources→trigger→super agent→router→worker 逐项对比；14 条缺项的该补/不该补判断与落层。
- **来源**：`github.com/deepseek-ai/deepseek-harness` commit `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`（docs/architecture.zh.md + docs/subsystems/*.md）；官方文档站 `deepseek-harness.github.io`；本机 `/Applications/DeepSeek Harness.app` 0.2.2-rc.1 的 `app.asar` 字符串 + `~/.dsh/profiles/desktop/`。
- **一句话结论**：我们缺的不是「再一层编排」，而是执行侧的三样保障——每任务可回读的 session 日志、任务态/执行体态分离（含 job 记账）、per-action approval + sandbox 三档；webhook/goals/DAG/Cordis 都不该补。
- **与 A1/B1 的边界**：A1 讲原型 bundle（Router/Worker/Trigger/Thread），B1 讲安装与 ACP 接入；本文只讲 DSH **核心**运行时，并澄清 `thread` 不属于核心、`profile ≠ agent preset`。
- **日期**：2026-11-08

---

## 待登记模板

```
### X. <标题>
- 路径：
- 回答：
- 来源（仓库+commit / URL / 实测环境）：
- 一句话结论：
- 日期：
```
