# Desktop 消息与安装路径兼容修复

规范源码为本仓库 `worker/codexworker`；部署机器使用完整技能快照，不维护各自的业务分叉。
DSH 的三份共享业务文件保持逐字节相同，协议格式适配放在 `interface/`。

## 修复

- `interface/native-content.mjs` 显式接受 Text/text、Image/image、LocalImage/localImage，
  支持公开消息的 PascalCase/camelCase 类型。正文、路径和所有 ID 原样保留。
  未知或畸形内容报错，不悄悄变成空消息，不转发 reasoning 或原始工具输出。
- `interface/journal-items.mjs` 承接原生公开事件投影；增量日志读取继续保持游标、
  身份校验和图片输入去重。小写原生提问/答案仍保留原 questionItemId。
- `router-tasks.mjs` 通过适配器解析原生消息，兼容大小写格式与 newest_first 页顺序；
  CLI 补读结果按精确 turnId 读取，避免取到另一个更新回合的回复。
- `interface/journal-paths.mjs` 从 CODEX_HOME（缺省 ~/.codex）定位 sessions，
  不再用 Worker stateDir 向上两级猜测；CLI 将显式 publicJournalRoot 传入生成配置。
  因此 state/<name>/cli 这类嵌套状态目录不再需要为日志创建 Junction。

## 验证

在清除宿主 CODEXWORKER_NATIVE_TRANSPORT、CODEXWORKER_STATE_DIR、CODEXWORKER_IDENTITY
覆盖值的环境运行 Node 26.12.1；测试仅使用临时文件和本地协议桩，不发送真实消息。

```sh
node --test --test-reporter=tap \
  worker/{codexworker,dshworker}/tests/{desktop-protocol-compat,local-public-journal,router-tasks,native-public-questions,message-transcript,context-transport,tool-activity-transport,question-routing}.test.mjs
node --test --test-reporter=tap --test-name-pattern='CLI preserves explicit journal root' \
  worker/{codexworker,dshworker}/tests/primary-session-cli.test.mjs
```

实际结果：第一组 177 tests / 177 pass / 0 fail，第二组 2 tests / 2 pass / 0 fail。
日志 `/tmp/worker-protocol-regression.tap`、`/tmp/worker-journal-config.tap`。
新增兼容测试包括大小写混合内容、非公开内容隔离、畸形格式报错、提问答案关联、
router 回合匹配、图片去重、半条日志写入与重启后游标续读，以及日志路径和配置落盘。

本次未重跑已有失败的全量历史套件。Windows 笔记本已清理环境，需重新安装快照，
在实际 Desktop 版本上验收飞书↔会话、Desktop→飞书、router→原会话→router 三条链路；
上述自动测试不能替代该实机验收。没有重启当前 Codex/DSH/飞书服务。
