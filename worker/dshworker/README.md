# DSHWorker — Harness 进程内插件

通过 `index.js` 的 Cordis 插件注入 `agents`、`sessions`、`agentDefaultModel`，引擎适配在
`interface/inprocess.mjs`。保留原会话，多轮运行；恢复失败不静默换新会话。
独立 CLI/headless 入口已移除。引擎差异只能放 interface/，共享 manager、MCP 业务和
router/飞书模块与 CodexWorker 逐字节一致。

运行服务由 Harness/Cordis 管理，不用 Codex Desktop 的生命周期监督器替代。
既有 DSH 任务控制、审批与引擎配置仍由 index.js 接入；更新生产插件前按其正常服务流程验收。
共享功能、router 普通交流/正式工单、目标模式和双平台测试规则见 `../codexworker/README.md`。
本次只同步源码，不能据此宣称正在运行的 DSH 已更新。

## 原生上下文与会话恢复

防回环说明与消息关联通过 `system-prompt/assemble` 的 `worker-chat-policy` section 提供。
原生 prompt registry 生成 system 消息，普通私聊使用下述原生用户输入流程。

`interface/session-recovery.mjs` 在当前 V4 Zstd 持久化解码完成、角色校验之前，
将错位或未知消息角色转换为可忽略的恢复记录，并写 `session_role_skipped` 告警。
告警包含会话 ID、seq、事件类型、期望与实际角色。seq、时间、物理截断位置保持，
原始文件由原生持久化服务保管，坏消息正文通过可忽略记录跳过。
物理解码阶段提前拒绝的角色在完整 Zstd 流的内存副本中隔离，再交回原生校验。
`sessions.prepare` 同时覆盖直接 seed 恢复。插件卸载恢复原方法，重组期间按实例计数。
此持久化接入点依赖当前 Harness V4 Zstd 后端的 `readZstdPrefix`，后端升级时需重新核验。

公共回归执行 `node --test worker/dshworker/tests/dsh-session-recovery.test.mjs`。
有已安装 Harness 包时，设置 `DSH_NATIVE_TEST_ROOT` 为包含其 node_modules 的目录，
同一命令会另运行真实 V4 持久化、混入 system 与未知角色的冷读回归。

## 飞书提问表单（布局 9）

按提供的 Codex 截图采用靛蓝标题、纵向编号选项、底部输入和右对齐的「跳过」「发送」。
省去待答状态的编号与操作说明，保留终态及失败原因；选项保留边框并用 ☐/☑ 标识未选/已选，选中项高亮，灰色标题仅用于过期。
每道题都有紧凑回答框，包括没有预设选项的题目；每题最多 1000 字。
最后一道题的输入框与「跳过」「发送」放在同一行；已有换行草稿继续以多行方式显示。
单题点击选项立即发送所选答案；自行输入内容后点「发送」。
多题仍保留草稿，填写完后统一发送；「跳过」保留已填内容，只将空白题目标记为跳过。
选择操作携带当前表单全部字段，其他题的草稿一并持久化，恢复后继续保留。
答案回填原生问题 ID，原飞书卡片更新为已回答；不再另发一张输入卡。
旧布局回调继续兼容，身份、聊天、消息、nonce 和原会话绑定校验保持不变。
router 发起的问题仍由溪流经原回答接口作答，训练员侧只展示状态。

验收覆盖表单回调、草稿恢复、空白/非法输入、重复操作、客户端答复同步和路由归属；
接口接受卡片与真人看到、提交成功分开记录。只更新源码不会改变已运行的插件；
需正常重载 Worker。共享引擎的原会话保留，不需要重启 Codex 或 router。
本次结果与待验项见[表单验收记录](../../docs/question-form-validation-20261113.md)。

## 私聊输入

peer 与飞书消息使用原生用户输入，运行中通过 agent.steer 进入当前回合。
发送者单独一行，正文换行，时间与原生附件沿用客户端显示。
回复关联元数据通过 `bridge_worker_inbox({"view":"history"})` 查询。
