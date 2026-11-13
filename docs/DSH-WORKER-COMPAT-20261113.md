# DSH Worker 0.3.1 接口对齐验收

对照 Codex Worker 发布提交 `51868d1`，按玉藻只读核对任务
`t-1f57f4aeb7` 补齐接口，业务版本继续使用 `0.2.41`。

## 修改范围

- `interface/public-events.mjs`：原生 text-delta/assistant-message 保留每段公开
  正文与稳定 item ID；工具 call/result 使用真实 `toolCallId` 关联，交给已有
  同卡累积、1500ms 节流、40 字预览；过滤内部推理和原始工具输出。
- `interface/inprocess.mjs`：resume 失败保留原 sessionId、明确报错；新会话先
  checkpoint 再接输入；忙时使用原生 `agent.steer`；原生 `/compact` 不新建会话。
  Desktop 的压缩服务在 preset 隔离层，必须用官方 `commands.find/execute`
  解析当前 agent 的 `/compact`，不能从宿主猜测压缩服务或自己写摘要。
- `interface/questions.mjs`、`interface/public-questions.mjs`：真实
  `user-questions/request` 绑定 session/call/question 身份；普通澄清进入公共
  提问卡，审批/计划审阅意图继续交给原生处理。答案直接结算原生 waiter；超时后
  的 continued 问题调用官方 `userQuestions.answer`。支持选项、自由文本、跳过、
  取消和 Desktop 实际答案投影，不把选项点击另排成一个无法解除等待的普通输入。
- `interface/task-control.mjs`：把实际 router client 交给共享 QuestionRelay；
  提问由原任务发起方回答；取消可定位 busy steer/澄清续接的原任务。旧回复移除
  内部推理后的投影升级只更新已看过的基线，避免再次发送历史回复。
- `index.js`：开启共享留档和工具进度；router 的 context 保留；等待真实澄清
  不误判无进展；原生问题续接后继续跟踪回答轮。机器身份/状态目录/凭据配置使用
  `DSHWORKER_*` 或本插件 identity，不继承其他 Worker 的同名 Codex 设置。
- 两侧 `question-relay.mjs` 同步将原生身份验证委托给 `interface/`。
  Codex 原身份格式及行为不变。三个约定业务文件 `cli.mjs`、
  `desktop-manager.mjs`、`mcp-business.mjs` 两侧逐字节一致且本轮未改。

## 验证

Node 使用本机 Node 26.12.1：

```sh
node --test \
  worker/dshworker/tests/dsh-public-events.test.mjs \
  worker/dshworker/tests/dsh-questions.test.mjs \
  worker/dshworker/tests/dsh-progress.test.mjs \
  worker/dshworker/tests/dsh-approval.test.mjs \
  worker/dshworker/tests/native-public-questions.test.mjs \
  worker/dshworker/tests/question-routing.test.mjs \
  worker/codexworker/tests/native-public-questions.test.mjs \
  worker/codexworker/tests/question-routing.test.mjs
```

日志：`/tmp/tamamo-compat-unit-20261113.log`。
原 DSH 路由测试还在断言只有两个按钮，已与 Codex 一致，验证两个选项加跳过共三个。

```text
tests 105
pass 105
fail 0
```

`ROUTER_TEST_PORT=18790 python3 server/router/selftest_router.py`
输出 `TOTAL 77/77 PASS`，日志 `/tmp/tamamo-compat-router-20261113.log`。
本轮没有重新跑整套带已知历史失败的 Node 全量；以上为受影响接口和共享提问回归。

## 原生实机证据

所有验收沿用同一 Worker thread
`d9fb1019-66c4-44db-9165-9c4778c53472` 和原生 session
`session-93b7fc94-e7c9-421c-9cfa-6175b14dc7bb`。

| 验收 | 实际结果 |
| --- | --- |
| 正文和工具 `t-79ffe12d95` | done，最终 `TAMAMO_STREAM_OK`；manager seq 320 为 item.delta，321/323 为 tool.activity，325 为 turn.completed |
| 提问 `t-f47e5f8ed6` | 原生 question `70b412e5-6b9a-45f4-854d-dd21a40186ce` 发给 Hermes；answerActor=hermes、answers=[继续]、status=answered；任务 done，回复“玉藻提问验收：继续” |
| 图片 `t-e9d1ff6e48` | 玉藻运行共享附件脚本；已验收三色图 1129272 字节；imageId `31a18f2400d54726b6d4d77a22dbe34bda5ed7605c1b88ad92f0bf9495f38fe8`；trigger_id=328，event_id=`notice:2861784a452ffe26288a8739`，notify，duplicate=false |
| 原生压缩 | request `dbed289c-0c74-4ee8-a597-41835807bfb5`；05:16:10Z 请求，05:16:27Z completed，compactions=1；没有 Worker 摘要回注 |
| 压缩后续接 `t-60624d1a1f` | 不读文件、不调用工具，正确取回 `TAMAMO-NATIVE-20261113-C7`，任务 done，sessionId 不变 |

HTTP `/health` 实际 ready=true，业务版本 0.2.41、引擎 0.3.1，compact=true，
toolProgress.enabled=true、source=native-notifications。
飞书发送成功是平台接受回执，不冒充人工目测：本轮 CUA 读取 DSH 窗口超时。

## 部署及边界

此接口代码需正常重新打开 DSH Desktop 才加载；实机已执行。router、gateway、
Codex app-server 未重启。几次正常关闭/重开后仍恢复上述原生 session。

首次从 Codex 调试进程打开 DSH 时暴露了旧的环境继承问题：CODEXWORKER_STATE_DIR
令 DSH 误选 Codex 状态目录。写入两个派生配置后，独占 writer 锁拒绝启动。
已修复命名空间，按仍在运行的 Codex 主会话和机器配置恢复派生配置，隔离误生成
的 DSH 临时状态；Codex 原会话/历史未替换，8905 保持 ready。
本地恢复记录在 `/tmp/tamamo-misdirected-config-20261113`，DSH 操作前备份在
`/tmp/tamamo-pre-compat-20261113-130024`，均不入 Git。

本次实机验证覆盖 Worker 发起的原生轮次。DSH Desktop 独立发起一个不经过
Worker 的新轮次时，全量消息抄送仍需单独接入/验收，不能据此声称 Desktop
所有入口已经完全一致。原生 Desktop 点击答案、跳过和 continued 边界有单测，
本轮真实作答端为 Hermes。token 用量数字尚未投影；不伪造 token 计数。

后续：桌面独立轮次适配与验收状态见 [DSH-DESKTOP-RELAY-20261113.md](DSH-DESKTOP-RELAY-20261113.md)。
