# 任务无进展看门狗

源文件为 `server/watchdog/watchdog_ping.py`。现有 cron 每 5 分钟执行
`~/.hermes/scripts/watchdog_ping.py`，部署时将源文件复制到这个入口即可生效。
宿主需有 Python 3.9+、Git 和 POSIX 文件锁支持。本机 macOS 已实跑。

## 配置

读取 `~/.hermes/task-watchdog.json`，也可通过 `--config <文件>` 指定。
默认配置见 `config.example.json`，其中的文件与工作区路径应换成本机真实路径。
Router、Trigger 令牌从文件读取，日志只记录任务号、信号数量与时间。

- `threshold_s` 默认为 900。原有 `WATCHDOG_STUCK_S` 环境变量优先。
- `quiet_night` 默认开启，沿用本机时间 00:00–08:30 静默的既有约定。
- `workers.<英文 id>.state_files` 指定状态文件，使用文件 mtime。
  应选择审批、模型配置、原生执行状态等实际操作写入的文件。
  `desktop-manager-state.json` 的轮询会不断刷新 mtime，配置时应排除这种文件和心跳文件。
- `workers.<英文 id>.workspaces` 指定该 worker 使用的 Git 工作区。
  工单自带 `workspace` 时优先使用工单路径。修改文件和非忽略的新文件使用 mtime，
  后续 HEAD 变化使用观察时刻。首次观察 HEAD 使用提交时间。
  路径不存在时跳过。远端 worker 通过 Router 进展与关联 peer 判断，本机无法读取其磁盘。
- `backoff_after=2`、`backoff_factor=2`、`backoff_window_s=3600`、
  `max_threshold_s=3600` 控制误报退避。若告警后才读到证据，而证据时间落在
  已告警的无活动区间内，记为一次误报。一小时内两次误报将阈值从 15 分钟提高到
  30 分钟，上限 60 分钟，证据老化后恢复默认阈值。

## 判定与投递

每次读取工单、worker 的 `last_progress_at`、带同一工单号的 notice/peer、
关联 peer 的回复，以及本机文件与 HEAD 变化。只有该工单创建后的活动计入。
同一 worker 正在执行其他任务时，其进展字段不计入本单。

日志示例

```text
watchdog judge task=t-files silent=1000s signals=notice:0,peer:0,fs:3,head:0,progress:0 idle=0s threshold=900s decision=active
watchdog judge task=t-files silent=1901s signals=notice:0,peer:0,fs:0,head:0,progress:0 idle=901s threshold=900s decision=alert
```

`silent` 为正式进展字段的空闲时间，`idle` 包含全部旁证，信号计数只包含当前阈值内的活动。
日志写入 `~/.hermes/logs/task-watchdog.log`，按 2 MiB 轮转并保留两份。

告警通过带鉴权的 Trigger 请求投递，沿用 `watchdog-<task_id>` 事件 id。
状态文件沿用 `~/.hermes/state/watchdog_sent.json`，旧版 `"sent"` 记录自动迁移。
同一任务持久化去重，任务被重放也只报警一次。文件锁与原子替换保护并发扫描。
投递失败保留重试资格，脚本退出码为 1。活动恢复时间晚于告警时按正常恢复处理。

## 自检与部署

```sh
python3 server/watchdog/selftest_watchdog.py
python3 server/watchdog/watchdog_ping.py --config ~/.hermes/task-watchdog.json
```

自检使用真实临时文件、Git、只读 SQLite 与带鉴权的本地 HTTP 服务，时钟加速。
覆盖文件活动抑制、真实停滞投递、重复扫描去重、HEAD、peer 分页与关联、
worker 进展、notice、误报退避、失败重试、旧状态迁移及夜间边界。
通过标准为 `OK`，并输出 `active → alert → dedup` 的实际运行日志。

部署前备份现有脚本和状态文件，使用同目录临时文件与原子替换发布脚本。
检查部署文件与仓库源码 SHA-256 一致，再核对 cron 的下一次 `last_status=ok`
及新日志时间。夜间实际运行应输出 `watchdog quiet window=00:00-08:30`。
回退时将备份脚本和状态文件一起恢复，再等同一 cron 入口执行。
