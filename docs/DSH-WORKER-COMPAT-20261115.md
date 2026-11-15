# DSH Worker 兼容核对

工单 t-201f80dd39，基线 `feat/worker-code-tidy` 的 `47ef737b9f4de4a5a0bf2944197b332cc6dcb596`，实现分支 `feat/dsh-worker-compat`。

## 逐项结果

| 项目 | 基线现状 | 本轮改动 | 验证 |
| --- | --- | --- | --- |
| 流式正文 | 已按原生 attempt ID 累积正文，支持多段。进度轮询只读取结束时的 `turn.text`，缺少 `latestAssistantMessageId`。业务目录还暴露了需要真实 latest turn 的快照捷径，空线程首条消息会被挡住。 | 原生轮询返回实时公开正文、稳定消息 ID 和等待输入状态。DSH 业务适配器使用完整进程内快照，DSH 原生 API 提供 `wait_threads`。 | `dsh-public-events.test.mjs` 的实时进度用例与 `dsh-compat.test.mjs` 的首条消息、增量正文用例通过。同一段产生 POST/PATCH，第二段使用独立卡片，轮次结束前可读。 |
| 工具进度 | 已订阅 `tool/call` 和 `tool/result`，按共同 Hermes 格式投影工具进度。 | 经新的业务适配器验证完整链路，继续使用共同节流、预览和累计卡片逻辑。 | `dsh-compat.test.mjs` 记录同一工具 ID 的 running/completed，飞书进度包含命令预览，原始工具结果留在引擎内。 |
| 会话恢复 | 已有 `dsh_session_resume_failed` 显式错误并保留原 ID。成功返回的句柄身份缺少校验，失败的原生 error item 会被共同正文投影过滤。 | 校验实际 session ID，失败时清理本次句柄及缓存。恢复失败投影为带原会话 ID 的公开错误提示，进入飞书正文。 | 恢复异常、错误句柄、创建前落盘用例通过。共享 manager 集成测试确认错误提示送入飞书出站，并保留原 session ID 和唯一线程。 |
| 上下文压缩 | 已支持 scoped `/compact` 原生命令和 compactNow。首次创建 agent 后，manager 的工具目录没有刷新。子会话压缩也会投影为父会话压缩。 | 首次创建或恢复 agent 后刷新原生压缩能力。父会话只接收自身压缩生命周期。 | scoped 命令、无可压缩历史、忙碌拒绝和子会话隔离用例通过。集成压缩后原 session ID、线程数和已有消息回执均通过核对。摘要留在原生引擎。 |
| 提问回答 | 已支持选项、自由输入、跳过、Desktop 作答和原生超时后继续回答。提问等待期间缺少立即落盘，多请求之一结束会提前清掉等待标记，重载后的继续回答会在恢复 agent 前验证失败。 | 发布时保存问题。等待状态按当前 turn 的剩余请求计算。继续回答先恢复同一原生 session。重载时清理已中断 turn 的活动等待标记，保存原问题身份。 | `dsh-questions.test.mjs` 覆盖三种答案、Desktop 作答、原生超时继续、重载继续、并行等待和落盘。`dsh-progress.test.mjs` 验证等待回答超过 30 分钟仍有效。集成卡片点选回填原问题，真实临时状态文件重载后身份可核对。 |
| 图片附件 | 鉴权下载、文件形式大图、出站附件和去重使用共同业务模块。DSH 入站采用真实缓存文件路径提示，Codex 可声明 native-local-image。 | 验证 DSH 真实缓存字节和中间 commentary 图片投影。 | 集成测试核对入站 PNG SHA256、交付给原会话的缓存路径、出站字节数和 SHA256，并确认同图一次投递。共同图片单元测试随双侧全量执行。 |

## 逐字节范围与适配差异

- `worker/codexworker/*.mjs` 与 `worker/dshworker/*.mjs` 共 37 个公共根模块，SHA256 全部相同。检查使用两侧文件名并集，额外文件、缺失文件和大小写差异均会失败。
- DSH 的 `index.js` 是 Cordis 引擎入口，本轮把原有注册实现收进 `interface/transport/inprocess.mjs`。进程内 agents、事件、原生问题身份、scoped 压缩和审批控制由 DSH 的 `interface/` 适配。
- DSH 业务状态读取采用完整进程内快照。原生 API 的 `wait_threads` 继续可调用。Codex 使用其 Desktop 管道、原生日志和状态观察能力。
- DSH 图片入站能力为 cached-path-prompt，Codex 可声明 native-local-image。两者共用下载、字节验证、附件回执及出站去重实现。
- 原生提问的计时由 DSH SDK 管理。原生超时后的 continued 问题通过同一 session 的 userQuestions 服务回答。Worker 使用 source-turn 归属和共同卡片规则。

## 自动检查

在仓库根目录执行完整检查。

```sh
ROUTER_TEST_PORT=18790 node worker/check-compat.mjs
```

脚本保存逐模块 SHA256、两侧完整 TAP 和 router 输出到系统临时目录，并以非零退出码报告失败。

仅核对哈希可执行 `node worker/check-compat.mjs --hash-only`。

Windows 执行 `node worker/check-compat.mjs --workers-only`，覆盖哈希与双侧全量。完整 router 入口依赖 POSIX 的 `fcntl` 和权限位，router 完整自测由 macOS 作业执行。Worker 全量内的既有 router HTTP/SSE/SQLite 集成用例包含 Windows 锁适配。

CI 的 macos-latest / windows-latest、Node 22 / 26 矩阵使用同一检查脚本，macOS 作业另执行 router 完整自测。测试 Python 可通过 `CODEXWORKER_TEST_PYTHON` 指定，完整检查脚本的 Python 可通过 `WORKER_COMPAT_PYTHON` 指定，路径含空格时作为一个可执行文件参数传递。

## 本机真实结果

macOS，Node v22.22.0，Python 3.9.6。

```text
shared_root_modules=37 matched=37 mismatched=0
codexworker passed=834 failed=0 skipped=8 total=842 exit=0 signal=none
dshworker passed=791 failed=0 skipped=8 total=799 exit=0 signal=none
router TOTAL 138/138 PASS exit=0 signal=none
WORKER_COMPAT_PASS
```

修复前的定向回归为 24 项中 18 通过、6 失败，修复后的五个定向测试文件共 45 项通过。

DSH 事件按本机安装 SDK 的 AssistantStreamFrame、SessionEventMap、UserQuestions 和 Compaction 原生合同构造。集成测试使用真实共享 manager、卡片投影、图片出站及临时持久化文件，模型和飞书回执采用隔离替身。生产 DSH、真实飞书收发和 Windows 交互式 Desktop 验收列为部署后待验项。

## Codex 侧独立待验项

公共 `publicItems()` 当前过滤独立的原生 `error` item。对 Codex 侧输入单独 error item 的只读核对返回 `[]`，其飞书错误正文可见性需要独立验收。DSH 的恢复失败通过引擎适配生成明确的公开提示。
