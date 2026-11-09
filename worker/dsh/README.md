# DSH worker

Router 的常驻 worker：接任务 → `dsh --profile headless` 执行 → 回结果。

```
Router :8790 ──SSE /stream──▶ dsh-worker ──spawn──▶ dsh --profile headless ──POST /result──▶ Router
```

## 启动

```sh
# 前台（Ctrl-C / SIGTERM 优雅退出）
./start-worker.sh

# 后台
./start-worker.sh -d

# 临时覆盖配置
./start-worker.sh --name 波旁 --router http://127.0.0.1:8790

# 指定解释器（默认 ~/.hermes/venvs/mowu-worker/bin/python，带 lark_oapi）
./start-worker.sh --python /path/to/python
```

配置默认读同目录 `worker.json`，命令行参数优先。token 默认读
`~/.hermes/secrets/router.token`（第一行；`Bearer ` 前缀可有可无）。文件不存在时
worker 仍会启动，只是不带鉴权。鉴权头同时发 `X-Router-Token`（mowu Router 用）
和 `Authorization: Bearer`（兼容其它实现）。

飞书同步需要 `lark_oapi`。首次准备专用 venv：

```sh
/opt/homebrew/bin/python3.12 -m venv ~/.hermes/venvs/mowu-worker
~/.hermes/venvs/mowu-worker/bin/python -m pip install lark_oapi requests
```

`start-worker.sh` 默认就用这个 venv；没装 lark 时会自动跳过飞书（任务照跑）。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--config` | 无 | JSON 配置；字段名与下表的参数名一致（下划线形式） |
| `--router` | `http://127.0.0.1:8790` | Router 地址 |
| `--name` | `dsh` | worker 名字，Router 按名字挂任务 |
| `--token-file` | `~/.hermes/secrets/router.token` | Router token 文件 |
| `--workspace` | 当前目录 | 默认工作目录（任务里的 `workspace` 可覆盖） |
| `--permission-mode` | `workspace-write` | `read-only` / `workspace-write` / `danger-full-access` |
| `--timeout` | `900` | 单任务超时秒数；到点杀进程组，结果报 failed |
| `--dsh-bin` | `dsh` | dsh 可执行文件（PATH 或绝对路径） |
| `--dsh-profile` | `headless` | dsh profile |
| `--approval-policy` | `never` | 无人值守不等人批；传空串则不向 dsh 传该参数 |
| `--heartbeat-interval` | `20` | 任务运行中心跳间隔（秒） |
| `--idle-heartbeat` | `60` | 空闲心跳间隔（秒） |
| `--idle-timeout` / `--idle-reconnect` | `90` | SSE 连续多久无任何字节判定断线并重连 |
| `--stall-seconds` | `300` | 任务期间无 stdout 且进程组 CPU 无变化多久判定卡死 |
| `--feishu-creds` | `~/.hermes/secrets/feishu-tsurumaru.json` | 飞书应用凭据（app_id/app_secret） |
| `--feishu-open-id` | 空 | 训练员 open_id；空=用凭据文件里的 `open_id` |
| `--no-feishu` | 关 | 不连飞书（自测/排障） |
| `--chat` / `--no-chat` | 关（`worker.json` 里 `chat_enabled: true` 开） | 飞书私信直接跟 dsh 聊天（持久 session / 流式进度 / 排队） |
| `--chat-state` | `~/.hermes/state/dsh-chat-state.json` | open_id → session 状态文件（原子写） |
| `--chat-timeout` | `300` | 单轮聊天超时秒数；到点杀进程组并回错误摘要 |
| `--chat-queue` | `3` | 忙碌时聊天最多排队条数，超出回一句忙线提示 |
| `--chat-stream-interval` | `1.6` | 流式进度节流间隔（秒），只编辑同一条消息 |
| `--subscribe` | 空 | 启动后订阅的 source，逗号分隔（如 `mc_chat,email`） |
| `--subscribe-interval` | `15` | `/subscriptions/pending` 轮询间隔（秒） |
| `--subscription-feed` | `~/.hermes/logs/<name>-subscription-feed.jsonl` | 订阅消息落盘文件 |
| `--log-file` | `~/.hermes/logs/dsh-worker.log` | 只追加，不轮转 |
| `--verbose` | 关 | 同时把日志打到 stderr |

## 协议

与 `docs/ROUTER.md` §二 / §五 一致：

```
POST /register   {name, version}                → {ok, queued}
GET  /stream?name=<name>                        → SSE
POST /heartbeat  {name, state, progress_at, task_id?, ts, ...}
POST /result     {name, task_id, status, summary, artifacts[], evidence[]}
POST /notice     {worker, text, urgency}        → 飞书入向转给 Hermes
POST /chat/log   {worker, direction, session_id, message_id, text, ts}
                                                → 飞书聊天旁听（失败不影响聊天）
POST /subscriptions {worker, source}            → 声明订阅
GET  /subscriptions/pending?worker=             → 拉取订阅消息
```

`/stream` 的任务事件接受几种包法（宽松解析）：

```
event: task
data: {"task_id":"t-88","goal":"…","context":"…"}

data: {"type":"task","task":{"task_id":"t-88","goal":"…"}}
```

非任务事件（`type` 为 `message`/`ping` 等）只记日志、忽略。同一 `task_id`
重复下发时**不重复执行**，只把缓存结果再上报一次。

执行命令（任务提示 = `goal` + 可选 `## Context` 段，作为单个 argv 传入）：

```sh
dsh --profile headless --permission-mode workspace-write --approval-policy never "<goal + context>"
```

结果：成功 `status=done`，`summary` 取 stdout 末尾 4000 字；
非零退出 / 超时 / 中断为 `status=failed`，`summary` 给出原因并附输出尾部，
`evidence` 形如 `{"type":"exit","code":0}` 或
`{"type":"exit","reason":"timeout","timeout_s":900}`。

断线重连：1s→2s→4s→8s→16s→30s 封顶，每次重连先重新 `/register`。
SSE 读超时只是“这一刻没数据”（空闲时 Router 只有 `: ping`），不算断线；
只有读到 EOF、连接重置/其他 IO 异常，或连续 `--idle-timeout`（默认 90s）
一个字节都没收到才重连。
SIGTERM/SIGINT：杀掉当前子进程组、把任务以 interrupted 上报（尽力）、退出 0。

## 飞书 / 看门狗 / 订阅

飞书长连接用 `lark_oapi` 的 WebSocket 模式（无需公网回调）。只处理训练员的
**p2p 文本私信**。`chat_enabled`（`--chat`）时走聊天：每条私信立即回
“（思考中…）”，然后跑 `dsh --profile headless --json`，成功后把回复发回飞书
（超过 1500 字分条）；运行中把公开文本/工具进度按 `--chat-stream-interval`
节流成一条“进行中”消息（编辑同一条，不逐事件刷屏）。每个 open_id 一条持久
session（`--chat-state` 原子写）：首轮新建，后续 `--session-id` 续聊；agent
非零退出/超时把错误摘要发回飞书，并把 session 标 suspicious，下一轮自动重建。
同一 worker 一次只跑一个 agent；Router 任务优先，忙碌时聊天最多排
`--chat-queue` 条，超出回“前面还在忙，稍后回你”。每条 in/out 都旁听
`POST /chat/log`，Router 没接上只记日志。

未开聊天（或旧行为）时，消息转 `POST /notice`（`urgency=urgent`，唤醒 Hermes），
成功后回一句“收到，已转给溪流”；群聊、他人、非文本一律忽略。任务开始/完成/
失败/卡死会主动给训练员发私信。`open_id` 是应用维度的，默认取凭据文件里的
`open_id`（`--feishu-open-id` 可覆盖）。

看门狗在任务期间同时盯 **stdout 字节数** 和 **进程组累计 CPU 时间**：两者连续
`--stall-seconds`（默认 300）都没变化才判定卡死（避免把“闷头算不打印”误杀）。
判定后先写日志、发飞书告警，再 SIGTERM→SIGKILL 整个进程组，结果上报
`status=failed`、`summary="stalled: no output for Ns"`、`evidence.reason=stalled`。
SSE 侧沿用 `--idle-reconnect`（默认 90s）无字节即重连的老逻辑。

`--subscribe mc_chat,email` 会在每次（重）连后重新 `POST /subscriptions`，并按
`--subscribe-interval` 轮询 `/subscriptions/pending`；消息逐条追加到
`subscription-feed.jsonl`，飞书侧按 60 秒窗口汇总（窗口内最多一条）。

## 自测

```sh
./selftest_worker.py        # 假 Router + 假 dsh + 假飞书，约 40 秒，不调用真模型
```

覆盖：注册、收任务、心跳、结果字段、4000 字截尾、失败退出码、超时、卡死看门狗
（判定+杀进程+飞书告警）、飞书入向/出向/过滤/重连、订阅声明/落盘/限流、
串行不重叠、断线重连并重新注册、SIGTERM 优雅退出、重复任务去重、配置加载、
语法检查、纯函数、SSE 空闲/心跳不误判断线、真断才重连，以及聊天全链路：
首轮 session 落盘/次轮 `--session-id`、流式节流（≤2 条）、忙时排队与超额拒绝、
`/chat/log` 字段、非零退出/超时的可疑标记与重建、1500 字分条、任务/聊天互斥。
