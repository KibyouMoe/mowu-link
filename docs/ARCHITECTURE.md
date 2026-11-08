# MowuLink 技术架构（2026-11-08）

> 产品设计见 `DESIGN.md`（桌宠定位、自治循环、零惩罚）。本文只写**技术骨架**：
> Trigger 输入总线、Task 工作单元、Worker 长期工位、桌宠表现层，以及它们怎么咬合。
> 一句话：**Trigger 是输入总线，Task 是工作单元，Worker 是长期工位，桌宠是 Worker 的显示器。**

---

## 0. 现状（已落地，可运行）

| 部件 | 位置 | 状态 |
|---|---|---|
| trigger 收件箱 | `~/projects/mowu-trigger/server.py` → 127.0.0.1:8791 | ✅ 0.4.0，LaunchAgent `ai.hermes.trigger` |
| MC 源客户端 | `clients/mc_chat_client.py` | ✅ SSE + 过滤墙 + 上行；本地母线 8792 |
| 邮箱源客户端 | `clients/email_client.py` | ✅ IMAP + 过滤墙；等 `~/.hermes/secrets/email.env` |
| Hermes 插件 | `~/.hermes/plugins/trigger-bus/` | ✅ 注入 + 自动续期；唤醒代码就绪，待重启验证 |
| 电视桌面 | `~/projects/mowu-tv` | ✅ 已替代小米桌面；屏保映射待做 |
| Task / Worker / 桌宠 | 未开工 | 本文第 2–4 节 |

---

## 1. Trigger：输入总线

### 1.1 信封

```json
{
  "source": "mc_chat|email|schedule|manual",
  "kind": "chat|death|email|digest",
  "event_id": "幂等键（缺省 sha256(source+text+ts)）",
  "priority": 0,
  "actor": "kibyou",
  "text": "……",
  "ts": "2026-11-08T11:00:00+08:00",
  "payload": {}
}
```

`priority`：0 = 打断（死亡、被点名）；1 = 排队（邮件、任务完成）；2 = 摘要（日报）。

### 1.2 通道

```
源 ──► 客户端过滤墙 ──┬─► 本地母线（8792/8793，内存，~20ms）
                      └─► trigger-server 8791（SQLite，局域网可订阅，需 token）
                                  ├─► Hermes 插件（拉取 → 注入 / 唤醒）
                                  └─► 远端 worker（长轮询 / SSE）
```

三种消费方式，一个信封：

| 方式 | 用途 | 特点 |
|---|---|---|
| SSE `/subscribe` | 本机 / 可信网络 | 实时推送，断线重连带 `since` |
| 长轮询 `/events?wait=25` | 远端 worker | 一次请求一次等待，无长连接、无注册 |
| 注入 / 唤醒 | Hermes 自己 | 见 1.5 |

### 1.3 投递语义

- **at-least-once + 幂等**：重发同一 `event_id` → `duplicate:true`，不重复入库
- **落盘才回 200**：`ok:true` 表示已持久化，不是“收到了看看”
- **入库 ≠ 已读**：订阅者各自 `since` 游标，注入后 `POST /ack`

### 1.4 订阅生命周期

| 概念 | 规则 |
|---|---|
| 订阅周期 | 默认 3600s（`--subscription-ttl`） |
| 提醒窗口 | 剩 600s 时响应带 `renew_required:true`（`--renew-window`） |
| 到期 | 停投（`events:[]` + `expired:true`），记录保留 24h 可恢复 |
| 续期 | `POST /renew {name}`，`renew_count+1` |
| 连接存活 | **与周期无关**：写失败 5 次 / 写超时 10s → 断连接 |

理由：周期是「还要不要继续听」的预算，由 worker 那边的 AI 决定；连接是否活着是传输层的事，两者不混。

### 1.5 注入 vs 唤醒

| 路径 | 触发 | 效果 |
|---|---|---|
| `pre_llm_call` 注入 | 仅在已有对话轮次 | 事件作为上下文附在那一轮，零打扰 |
| 唤醒（synthetic turn） | P0 事件到达且空闲 | 起一个新 turn，我主动决定是否告知训练员 |

唤醒节流：最少间隔 30s；只对 `priority<=0` 唤醒；P1/P2 留到下次对话注入。

### 1.6 过滤墙（客户端）

规则字段：`allow_senders` / `deny_patterns` / `match_subject` / `match_body` / `min_priority` / `rate_limit` / `dedupe_window_seconds` / `priority_map`。

**静默丢弃禁止**：每条拦截写 `clients/drops.log`（含 reason、rule、text_head）。

---

## 2. Task：工作单元

```
submit ─► queued ─► running ─► succeeded
             │          ├─► failed ─► retry(≤N) ─► queued
             │          └─► cancelled
             └─► lease 过期 ─► queued（worker 失联回收）
```

```sql
task(id, parent_id, title, spec_uri, state, assignee,
     created_at, started_at, ended_at, attempts, result_uri, summary)
lease(task_id, worker_id, expires_at)
event(id, task_id, worker_id, kind, payload_json, ts)
```

- `spec` / `result` 正文落盘 `~/.hermes/worker-store/<task_id>/`，库里只存 URI（对应官方 Super Agent 的「截断 + read:<uri> 分页」）
- 租约 60s，心跳 20s 续；进程崩溃 → 租约到期 → 任务自动回队
- 任务库独立于 `state.db`：`~/.hermes/router.db`（备份与迁移互不牵连）

---

## 3. Worker：长期工位

一只 worker = 一个**长期槽位**，不是一次任务；任务进来才忙，任务结束回到 idle。

| kind | 适配方式 |
|---|---|
| `hermes` | Hermes 子 Agent |
| `codex_local` | 本机 Codex CLI |
| `codex_remote` | 其他电脑的 Codex |
| `dsh` | 独立 DSH 实例 |

统一适配器接口：

```python
class Adapter:
    async def run(self, task) -> AsyncIterator[Event]   # started/log/tool/done
```

### 3.1 授权卡（mandate）

默认只能上报，不能自主行动：

```yaml
worker: mc-bot
mandate:
  - when: {kind: death, actor: kibyou}
    may: [teleport_to_death, pickup_items, report]
    escalate_if: [coords_unknown, op_denied]
  - when: {kind: chat, text_matches: "(溪流|SuperCreek)"}
    may: [reply, follow_player]
  - when: {kind: chat, text_matches: "(挖|建|合成|改配置)"}
    escalate: router
report: every_action
```

### 3.2 两条铁律

1. **可绕 router，不可绕可观测性**：worker 自主动作写 action log（`in_reply_to=event_id`）；router 处理同一事件前先查，避免重复动作
2. **快慢路径看同一批 event_id**：worker 走本地母线（~20ms），router 走队列（可能几十秒），但两边 `event_id` 一致 → 不会两套账

---

## 4. 桌宠：Worker 的显示器

一只宠 = 一行 `worker` 记录，长驻，不随任务创建/销毁。

| worker.status | 桌宠表现 |
|---|---|
| `idle` | 闲逛 / 打瞌睡 |
| `working` | 忙碌 + 头顶任务名 |
| `blocked` | 冒问号（等确认） |
| `error` | 冒黑线，点开看最后 5 条事件 |
| `offline` | 灰掉 |

- 表现层只订阅状态流，**不参与调度**；桌宠崩了不影响任务进程
- 平台：Windows（MowuLink）+ 手机客户端同角色同存档
- 桌宠与产品设计（自治循环、零惩罚、日报）见 `DESIGN.md`

---

## 5. 并发（本机三线程）

| Lane | 内容 | 通道 | 冲突 |
|---|---|---|---|
| A | MC | in-process bridge（8765） | 零 |
| B | 桌宠开发 | GUI（透明窗、动画） | 中 |
| C | 魔物村 | UE 编辑器 | 高（单实例重型） |

仲裁器 `~/.hermes/locks/`（`fcntl.flock` + TTL，进程崩溃自动释放）：

```
gui.foreground      互斥，一次一个（UE / 桌宠抢前台时排队）
window.<pid>.<wid>  同窗口互斥（AX 快照会互相覆盖）
clipboard           全局唯一，加锁 + 用后还原
screen.capture      只读，可并发
```

依据：cua-driver 0.21 默认 `delivery_mode=background`，按 pid 投递、不抢焦点；`foreground` 是最后手段，只有它需要排队。

---

## 6. 待办

| # | 事项 | 阻塞 |
|---|---|---|
| 1 | 唤醒链路真机验证 | 需 gateway 重启 |
| 2 | 电视屏保映射 + 卸 `com.mitv.gallery` | 电视开机 + ADB |
| 3 | 邮箱凭据 | 训练员提供 `email.env` |
| 4 | Task / Worker 表与协议（`router.db` + submit/poll/result） | 未开工 |
| 5 | 桌宠原型（状态订阅 → 表现层） | 未开工 |
