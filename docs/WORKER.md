# Worker 规范（定稿 2026-11-09）

> 本文是 worker 的唯一口径。实现见 `worker/`，Router 协议见 `ROUTER.md`。
> **命名规则：Router、日志、任务里一律用英文标识**（`TamamoCross` / `OguriCap`）；
> 中文名只存在于人读的对话里，程序不认中文。

---

## 一、Worker 是什么

```
一个常驻进程，托管一个 agent 运行时（DSH 或 Codex）
它只负责「接活、干活、交活、看护自己」，不做判断
```

- 一个 worker 只跑一个 agent，串行执行：同一时间只处理一件事。
- worker 自己的名字由它自己持有（`worker.json` 的 `name`），注册时上报给 Router；Router 只照抄，不分配、不改写。

---

## 二、职责清单

| # | 职责 | 说明 | 状态 |
|---|---|---|---|
| 1 | 建立连接 | 向 Router `/register`，然后挂 `GET /stream` 长连接；断线按 1/2/4/8/16/30 秒退避重连，每次重连先重新注册 | ✅ |
| 2 | 取任务 | 从连接上收任务，跑子命令（`dsh --profile headless` / `codex exec`），串行执行 | ✅ |
| 3 | 汇报结果 | `POST /result`：`done` / `failed` + 摘要（stdout 末 4000 字）+ 证据（退出码、耗时） | ✅ |
| 4 | 自身看门狗 | 任务期间监控子进程：超过 `stall_seconds`（默认 300）既无 stdout 增长也无 CPU 变化 → 判定卡死 → 告警 → 杀进程组 → 结果报 `failed`，摘要写 `stalled` | ✅ |
| 5 | 订阅信息 | `--subscribe` 声明订阅 → Router 落到 trigger；worker 拉 `GET /subscriptions/pending` 落盘；掉线即由 Router 注销订阅（订阅不留存） | ✅ |
| 6 | 向 Hermes 递话 | 任务开始/完成/卡死经 Router `POST /notice`，按 `urgency` 决定 `notify`（排队）还是 `event`（打断） | ✅ |

**明确不做**：判断任务给谁、判断消息要不要通知训练员、操作别的 worker、改自己的配置。

---

## 三、名字与身份

```json
// worker/<type>/worker.json
{
  "name": "OguriCap",          // 英文标识，Router 唯一认的字段
  "router": "http://127.0.0.1:8790",
  "token_file": "~/.hermes/secrets/router.token"
}
```

| worker | 英文标识 | 人读称呼 | agent 运行时 |
|---|---|---|---|
| codex | `OguriCap` | 小栗帽 | `codex exec` |
| dsh | `TamamoCross` | 玉藻十字 | `dsh --profile headless` |

飞书应用（已建，暂未接 worker）：`玉藻十字` = `cli_aa7b940a09789cf1`，`小栗帽` = `cli_aa7b9477df381cfa`，
凭据在 `~/.hermes/secrets/feishu-tsurumaru.json` / `feishu-oguri.json`。

---

## 四、三条链路

```
派活    Hermes → Router /tasks → worker（SSE 下发）
汇报    worker → Router /result → Router 写 trigger(notify) → Hermes
订阅    worker → Router /subscriptions → trigger；trigger → Router → worker 拉取
```

- Hermes 与 worker 之间**没有直连**；所有交互都经过 Router。
- Router 不知道 worker 会什么、在干什么，只知道「在不在线、忙不忙、积压几条」。

---

## 五、失败与边界

```
agent 非零退出      → failed + 错误摘要
超时               → 杀进程组 + failed
卡死（看门狗）      → 告警 + 杀进程组 + failed
Router 不可达      → 退避重连；任务不丢（Router 侧 at-least-once）
worker 自己崩溃     → LaunchAgent 拉起；重连后重新注册
```

- 任务重投语义：Router 至少一次投递，worker 侧按 `task_id` 去重。
- 订阅消息**不留存**：断了就不补，重连后由 worker 自己重新声明。

---

## 七、路线：桥要通用化（训练员 2026-11-09 定）

现在这套桥（`worker/codexworker/`，源自 Codex Desktop 插件）是**绑 Codex 的**。
最终形态应该是：**一套桥，多个 agent 适配器**。

```
桥（共用）     会话 / 卡片 / 飞书出口 / 入站分流 / 进度节流 / 历史
  ├─ adapter: codex   codex exec --json / resume
  └─ adapter: dsh     dsh --profile headless --session-id
```

- 桥不认具体 agent，只认适配器接口：新建会话、续会话、读增量、列历史。
- CodexWorker 只是「桥 + codex 适配器」的第一个实例；DSH 之后接同一个桥。
- 所以：任何写进桥里的逻辑，都不许假定 agent 是 Codex（不许写死 `codex` 命令、Codex 的 NDJSON 字段名）。

## 八、常驻方式

| worker | LaunchAgent | 日志 |
|---|---|---|
| TamamoCross | `ai.hermes.dsh-worker` | `~/.hermes/logs/dsh-worker.log` |
| OguriCap | `ai.hermes.codex-worker` | `~/.hermes/logs/codex-worker.log` |

两个都是 `RunAtLoad + KeepAlive`，崩了自动拉起；启动脚本 `worker/<type>/start-worker.sh` 负责注入代理、
`DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic`（DSH 必需）等环境。
