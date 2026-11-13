# Worker 0.2.39：提问状态同步与聊天中接收溪流任务

训练员反馈：在飞书回答提问后，Codex客户端仍保留待回答窗口；
聊天持续进行时，溪流的任务只显示转发消息，正文迟迟未进入原会话。

## 改动

- interface/public-questions.mjs：依据本机客户端现有公开消息格式，保留
  sourceQuestionItemIds、完整原题及真实答案，组成send_user_message_question_reply结构。
  客户端根据这些问题ID识别已回答，而非根据普通文字推断。
- question-relay.mjs：原生异步提问使用该结构送回；普通无原生面板的提问继续原格式。
- desktop-manager.mjs：原生答复结构不加额外来源文字，保持客户端可识别；
  原卡片更新、来源记录、重复回答幂等和原会话守卫沿用。
- cli.mjs：router任务和订阅事件取消投递前等待idle，直接沿用已经验证的busy follow-up；
  保留原会话、任务marker、确定性messageId和管理器单写入路径。
- mcp-business.mjs：修正旧版“外部回答不能同步原生面板”的说明。
- 共享业务三文件两侧逐字节一致；相关公共辅助文件同步，不重启DSH、router或gateway。

本机客户端代码只做读取，未修改应用文件或私有窗口状态。
旧的已回答问题没有自动重放，避免把历史答案作为新的指令重复执行。

## 验证

实际生产Node v26.12.1：
node --test worker/codexworker/tests/{native-public-questions,question-routing,question-cards,router-tasks,router-client,primary-session-cli}.test.mjs
81 tests / 81 pass / 0 fail / 0 skipped。

新增完整CLI+本地SSE+忙碌假引擎回归：
旧HEAD复现busy router ingress timed out；修复后原回合inProgress时已收到任务，并返回done。
没有连接真实router或发送测试飞书消息。

扩展管理器回归：220 tests / 217 pass / 2 fail / 1 skip。
两条图片旧用例均在未修改HEAD独立复现同一断言失败：
automatic image relay sends ten displayed images：0 !== 20；
Wiki public previews auto-send：2 !== 4。
本次相关提问与路由用例全部通过。
CLI假引擎测试还显式清除继承的生产共享socket，避免测试连接实际会话。

日志：
- /tmp/worker-question-router-node26-final.log
- /tmp/worker-question-router-tests-20261112-234611.log
- /tmp/worker-question-router-head-baseline.log
- /tmp/worker-router-busy-before-fix.log

## 上线状态

已获正式工具审批，仅向ai.hermes.codexworker发送SIGTERM，优雅重载完成：
0.2.38 → 0.2.39；原threadId保持01a1c413-6bd5-7e10-a388-665d44d2b6ec；
ready=true、feishuConnected=true、routerConnected=true。
不重启共享Codex app-server，不新建主会话，不重启DSH、router或gateway。
生产客户端实际窗口收起与溪流在聊天中的新消息仍需端到端实测，不以隔离用例代替。

部署与日志归档：/Users/kibyou/Workspace/OguriCap/artifacts/worker-0.2.39；未提交或推送。
