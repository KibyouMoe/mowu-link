# Worker 名册（Roster）

> 每接一个 worker，先在这里落一行。**真实名称用英文**（Router 唯一认的），口语名只给人看。
> 规范见 `WORKER.md`；任务明细以 Router `/tasks?worker=<name>` 为准，本文只记要点。

| 真实名称 | 口语名 | 来源 | Agent | 模型 | 擅长 |
|---|---|---|---|---|---|
| `OguriCap` | 小栗帽 | 本机 M6 / `worker/codexworker` | Codex app-server（`codex app-server` stdio） | `gpt-6.1-sol` / effort `max` | 写码、改码、跑测试、重构 |
| `TamamoCross` | 玉藻十字 | **已退役**（原 `worker/dsh`，2026-11-10 删除） | 待重建为 cordis 插件（Node） | `deepseek-flash` | 调研、读文档、GUI 操作、长任务 |

---

## 一、两套名字，别混

```
桥实例名（代码、目录、进程）   CodexWorker / DSHWorker
worker 身份名（Router 认的）   OguriCap / TamamoCross
```

- 桥是壳，身份是里面那个人。一个桥可以换身份，身份也可以换桥。
- 目录：`worker/codexworker/`（已建、在用）、`worker/dshworker/`（待建，cordis 插件版）。

## 二、重做原则（2026-11-10 定）

- 两个 worker **同构**：都是 Node，都走 `router` 协议，都不直连飞书。
- 汇报一律走 `router /notice` → trigger → 溪流转达；worker 不持有飞书出口。
- 上下文合并：router 任务复用主会话（`state.selectedThreadId`），不另开 thread。
- 任务消息形态：标题「来自超级溪流的指令（任务 t-xxx）」+ 正文，无重复终态消息。

---

## OguriCap（小栗帽）

```yaml
name: OguriCap
alias: 小栗帽 / 小栗
status: 在用
origin:
  host: 192.168.3.102（M6 Mac mini，溪流本机）
  path: ~/projects/mowulink/worker/codexworker/
  entry: bin/codexworker
  launchd: ai.hermes.codexworker
  log: ~/.hermes/logs/codexworker.out.log / .err.log
agent:
  runtime: codex app-server（JSON-RPC over stdio）
  transport: codex/transport/app-server.mjs（另一套 desktop-pipe 用于 Desktop 内）
  model: gpt-6.1-sol
  reasoning_effort: max
  context: 500k 窗口，可用 475k
  env:
    HTTPS_PROXY: http://127.0.0.1:10808   # codex 不认 socks，ALL_PROXY 必须 unset
strengths:
  - 写代码 / 改代码 / 重构（会自己读仓库、跑测试、改完自查）
  - Router 任务与飞书对话共用同一 thread（上下文连续）
weaknesses:
  - 话少，过程不解释，出问题只能从退出码和 stderr 看
personality: |
  寡言、一本正经、不客套。问它什么，它给结果，不铺垫、不解释。
  对自己的产出有洁癖，改完会自己跑一遍再交。
  出错时不掩饰，直接把错误原文贴上来。
tasks:
  recorded_under: OguriCap
```

---

## TamamoCross（玉藻十字）

```yaml
name: TamamoCross
alias: 玉藻十字 / 玉藻
status: 已退役（2026-11-10）—— Python 版删除，待重做为 cordis 插件
retired_reason: |
  飞书层三个问题反复出现：open_id 跨 app、文本消息不能编辑、ws 静默断连。
  训练员 2026-11-10 决定走 B 方案：与 CodexWorker 同构，用 cordis 插件重写。
origin:
  host: 192.168.3.102（M6 Mac mini）
  path: worker/dshworker/（待建）
  agent_runtime: DeepSeek Harness（cordis / Node）
  plugin_api: dsh plugin → 转发 pnpm，profile 的 package.json bundles
  profile: ~/.dsh/profiles/headless/
agent:
  model: deepseek-flash
  env:
    DEEPSEEK_BASE_URL: https://api.deepseek.com/anthropic   # 必须，根域走 Messages 会 404
    HTTPS_PROXY: http://127.0.0.1:10808                      # dsh 不认 socks
personality: |
  开朗、直率、干劲足，接到活先说「好，咱来」。
  关西腔、急性子、嘴硬心软；干活时会边做边报，卡住直接说卡在哪。
  对钱和成本敏感，会主动算时间与依赖。
tasks:
  recorded_under: TamamoCross
  note: 旧 Python 版时期的历史任务仍记在 TamamoCross 名下
```

---

## 维护规则

1. **新增 worker**：先在本文加一行表格 + 一段详表，再去接 Router。
2. **改名**：真实名改 `worker.json` 的 `name` 并重启；本文同步；Router 不认中文，别名只写在这里。
3. **任务记录**：不手写明细，用 `GET /tasks?worker=<name>` 查；本文只保留「最近 3 条要点」。
4. **能力变化**（比如新装了某个插件）：更新 `strengths` / `weaknesses`，别让它停留在过时印象。
