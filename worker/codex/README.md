# Codex worker

Router 的常驻 worker：接任务 → `codex exec` 执行 → 回结果。与 `worker/dsh/` 同构，目录独立。

```
Router :8790 ──SSE /stream──▶ codex-worker ──spawn──▶ codex exec ──POST /result──▶ Router
```

## 启动

```sh
# 前台（Ctrl-C / SIGTERM 优雅退出）
./start-worker.sh

# 后台
./start-worker.sh -d

# 临时覆盖配置（命令行在最后，优先级最高）
./start-worker.sh --name 阿码 --router http://127.0.0.1:8790

# 指定二进制 / 代理
CODEX_BIN=/path/to/codex CODEX_HTTP_PROXY=http://127.0.0.1:10808 ./start-worker.sh
```

启动脚本会做三件事（`worker.py` 启动时也会再兜一次）：

1. `unset ALL_PROXY all_proxy`——codex 不认 socks5，留着会超时；
2. `HTTP_PROXY=HTTPS_PROXY=http://127.0.0.1:10808`（可用 `CODEX_HTTP_PROXY` / `--proxy` 覆盖，`--proxy none` 走直连）；
3. 把 `~/.hermes/tools/node-26.12.1-darwin-arm64/bin` 塞进 `PATH`，让 `codex` 自动探测命中。

配置默认读同目录 `worker.json`，命令行参数优先。token 默认读
`~/.hermes/secrets/router.token`（第一行；`Bearer ` 前缀可有可无）。文件不存在时
worker 仍会启动，只是不带鉴权；鉴权头同时发 `X-Router-Token`（mowu Router 用）
和标准的 `Authorization` 头。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--config` | 无 | JSON 配置；字段名与下表的参数名一致（下划线形式） |
| `--router` | `http://127.0.0.1:8790` | Router 地址 |
| `--name` | `codex` | worker 名字，Router 按名字挂任务 |
| `--token-file` | `~/.hermes/secrets/router.token` | Router token 文件 |
| `--workspace` | 当前目录 | 默认工作目录（任务里的 `workspace` 可覆盖） |
| `--timeout` | `900` | 单任务超时秒数；到点杀进程组，结果报 failed |
| `--codex-bin` | `auto` | codex 可执行文件；`auto` = 先 PATH，再固定兜底路径 |
| `--proxy` | `http://127.0.0.1:10808` | codex 用的 http 代理；`none`/空 = 直连 |
| `--heartbeat-interval` | `20` | 任务运行中心跳间隔（秒） |
| `--idle-heartbeat` | `60` | 空闲心跳间隔（秒） |
| `--idle-timeout` | `90` | SSE 静默多久判定断线 |
| `--log-file` | `~/.hermes/logs/codex-worker.log` | 只追加，不轮转 |
| `--verbose` | 关 | 同时把日志打到 stderr |

`codex` 查找顺序：`--codex-bin` 显式值 > `PATH` 里的 `codex` >
`~/.hermes/tools/node-26.12.1-darwin-arm64/bin/codex`。显式传了不存在的路径会直接报错退出（码 2）。

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

执行命令（任务提示 = `goal` + 可选 `## Context` 段）：

```sh
codex exec --skip-git-repo-check "<goal + context>"
```

超长提示（>200KB）或提示以 `-` 开头时改走 stdin（`codex exec --skip-git-repo-check -`）。
codex 的 stdout 末尾 4000 字进 `summary`（ANSI 颜色码会先清掉），stderr 在预算内附在后面。

沙箱与审批沿用 `~/.codex/config.toml`（本机实测 `sandbox: read-only` / `approval: never`）。
需要 codex 真正改文件时，把该配置里的 `sandbox_mode` 调成 `workspace-write`——
worker 不额外传 `--sandbox`，保持 `codex exec --skip-git-repo-check + 任务文本` 这条命令干净。

结果：成功 `status=done`，`evidence=[{"type":"exit","code":0,"duration_ms":…}]`；
非零退出 / 超时 / 中断为 `status=failed`，`summary` 给出原因并附输出尾部，
`evidence` 形如 `{"type":"exit","code":3}` 或
`{"type":"exit","reason":"timeout","timeout_s":900,"duration_ms":…}`。

断线重连：1s→2s→4s→8s→16s→30s 封顶，每次重连先重新 `/register`。
SSE 读超时只是“这一刻没数据”（空闲时 Router 只有 `: ping`），不算断线；
只有读到 EOF、连接重置/其他 IO 异常，或连续 `--idle-timeout`（默认 90s）
一个字节都没收到才重连。
SIGTERM/SIGINT：杀掉当前子进程组、把任务以 interrupted 上报（尽力）、退出 0。

## 自测

```sh
./selftest_worker.py        # 假 Router + 假 codex，约 25 秒，不调用真模型、不联网
```

覆盖：注册、收任务、codex 参数与代理环境注入、心跳、4000 字截尾、失败退出码、
超时、串行不重叠、断线重连并重新注册、SIGTERM（空闲/执行中）、重复任务去重、
配置加载、二进制探测优先级、语法检查、纯函数、SSE 空闲不误判断线。
