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
```

配置默认读同目录 `worker.json`，命令行参数优先。token 默认读
`~/.hermes/secrets/router.token`（第一行；`Bearer ` 前缀可有可无）。文件不存在时
worker 仍会启动，只是不带鉴权。鉴权头同时发 `X-Router-Token`（mowu Router 用）
和 `Authorization: Bearer`（兼容其它实现）。

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
| `--idle-timeout` | `90` | SSE 静默多久判定断线 |
| `--log-file` | `~/.hermes/logs/dsh-worker.log` | 只追加，不轮转 |
| `--verbose` | 关 | 同时把日志打到 stderr |

## 协议

与 `docs/ROUTER.md` §二 / §五 一致：

```
POST /register   {name, version}                → {ok, queued}
GET  /stream?name=<name>                        → SSE
POST /heartbeat  {name, state, progress_at, task_id?, ts, ...}
POST /result     {name, task_id, status, summary, artifacts[], evidence[]}
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

## 自测

```sh
./selftest_worker.py        # 假 Router + 假 dsh，约 20 秒，不调用真模型
```

覆盖：注册、收任务、心跳、结果字段、4000 字截尾、失败退出码、超时、
串行不重叠、断线重连并重新注册、SIGTERM 优雅退出、重复任务去重、配置加载、
语法检查、纯函数、SSE 空闲/心跳不误判断线、真断才重连。
