# DSH 图片与提问去重验收

工单 `t-fa78bba7df`，分支 `feat/dsh-worker-fixes`，基线 `feat/dsh-worker-compat@f247fdd6a99a16ca19e6985835b21b2c7bf072b7`。

## 图片根因与前后对照

生产会话 `d9fb1019-66c4-44db-9165-9c4778c53472` 的 `publicMessages` 中有 30 个相对路径图片引用，公开文本形如 `![…](evidence/…png)`。公共解析器只接收绝对路径 Markdown，DSH 也未转换 `MEDIA:` 指令，因而相对图片会进入文字卡片，媒体出站账本为 0。

只读检查得到以下实际结果。末轮 `turn.text` 中另外 11 个引用包含这些公开消息的重复，计数采用 `publicMessages` 的 30 个。

```text
threadId=d9fb1019-66c4-44db-9165-9c4778c53472
sessionId=session-93b7fc94-e7c9-421c-9cfa-6175b14dc7bb
publicMessages.relativeMarkdownImages=30
autoImageReceipts=0

evidence/20261114-codex-restart/hook-pending-after-d0c112a.png
exists=true bytes=167718
sha256=bd804dadb91873e2a39c8b088bd0426fae5879cb3d58cb0a67e92b1f387e8085

evidence/20261114-codex-restart/hook-pending-cleared-2100.png
exists=true bytes=162845
sha256=2f7e7a396ed6aeb29c5ff3cf120454d1facffd182c0b747af74285ab4ce381dc
```

修复前现象　绝对 Markdown 正常，相对 Markdown、绝对 `MEDIA:`、带前后文的 `MEDIA:` 与大图 `MEDIA:` 都得到 0 条媒体消息，并把原始标记投到文字卡片。

修复后现象　以上五种格式各产生 1 条独立媒体消息，前后文各自投递，媒体 JSON 仅含 `image_key` 或 `file_key`。小图走 `msg_type=image`，超过 10,000,000 字节的大图走 `msg_type=file`，上传 SHA256 与原件一致。

证明用例　`node worker/dshworker/tests/repro-media-question.mjs --expect-fixed` 以及全量收集的 `dsh-media-question.test.mjs`、`dsh-public-media.test.mjs`、`dsh-compat.test.mjs`。

入站图片与图片文件各重复投递 20 次，基线和修复后均为 1 次下载、1 次原生输入、1 条收信回执，SHA256 一致。DSH 原生输入继续携带缓存路径提示，模型实际看图依赖其原生读图工具。

### 实现

- `interface/public-media.mjs` 按原会话 cwd 解析相对 Markdown 与独立 `MEDIA:` 行，保留代码示例和远程链接，流式标记完整后才发布图片。
- `interface/public-events.mjs` 为新公开消息记录 `mediaVersion=1` 和提交状态。
- `interface/inprocess.mjs` 将媒体、前文、后文投影成稳定且独立的公开 item，复用现有媒体发送账本。历史消息保留原投影与原 ID，历史相对图片继续由旧游标处理。
- 两侧 `public-image-relay.mjs` 同步增加可选路径解析回调。DSH 传入该回调，Codex 默认调用走原绝对路径解析。37 个公共根模块哈希一致。

## 提问出口逐项审计

生产只有 1 条问题记录，现有记录足以定位身份和收据，尚不能确认训练员当时所见的具体两处界面。本次按指令完成代码审计与重复投递复现。

| 入口或出口 | 行为与重复风险 | 幂等依据与处理 |
| --- | --- | --- |
| `interface/questions.mjs` 的 `user-questions/request` 订阅 | 基线对同一 session/call 重复追加原生问题并启动原生回答 provider，pending map 覆盖之前的等待者 | 已修为 `(sessionId, callId)` 共用同一个 Promise、provider 与公开 item。同身份异内容报冲突，已答复的重放返回原答案 |
| `interface/public-questions.mjs` 的 `projectPublicQuestion` | 基线公开 UUID 包含外层 turnId，同一原生问题在重建的包装回合下得到不同 UUID | 新问题 UUID 取原生 `(sessionId, callId)`，持久化原 thread/sourceTurn。历史问题载入时锚定原 UUID，保留原卡和 nonce |
| `desktop-manager.mjs` 的 `publicItems`、`scan`、`drainPublicTexts` | 扫描和流式更新都可能再次发现同一问题 | 均调用 `QuestionRelay.publish`，相同 questionId 的 in-flight 和持久化账本收敛为同一卡片 |
| `desktop-manager.mjs` 的 `captureTaskQuestions` | router 任务澄清会直接 publish，同时还会进入公开 outbox | 同一个 questionId，existingCard 只接受该问题已持久化的 UUID/messageId。本次重复 20 次实际 capture 调用仍为 1 张卡 |
| `POST /bridge/questions` | 手工或外部结构化发布可重试 | 调用同一个 publish。同 ID 同内容复用，异内容返回 question_conflict |
| `mcp-business.mjs` 的 `publishQuestion` | 显式 questionId 可以安全重试，缺省 ID 时每次生成随机 UUID，调用者重复创建会被视为不同问题 | 这是两侧共同的公开工具契约，单列调用约束。DSH 原生问题走带身份的适配入口 |
| `interface/desktop-observer.mjs` 的原生订阅 | Worker 回合已有逐回合观察器，Desktop 也有全局观察器 | 全局投影仅处理 nativeDesktop 回合，Worker 回合直接返回。session/event 按 session+seq 游标过滤，原生答案只回填已观察到的 call |
| `interface/journal-items.mjs` 的公开日志投影 | 解析结构化 question，可能与扫描碰到同一条公开记录 | 仍使用引擎 projector。DSH 的 desktopJournal capability 为 false，当前 DSH 运行路径采用进程内快照 |
| `router-runtime.mjs` 的订阅消费 | 相同订阅事件重投可以重复进入消费函数 | `subscriptionMessageId(source,id)` 稳定，manager 收信回执过滤重复。该入口投递普通消息，未直接创建问题卡 |
| `QuestionRelay.forwardToInitiator` | Hermes 发起的回合发 1 条 worker.question notice，并给训练员显示只读卡。publish 和周期 process 可同时调用此函数 | 已确认共同的并发竞态，见下方独立问题。通常状态 sending/sent/unknown 会阻止再次发送，任务查询的异步间隙尚有竞争 |
| `feishu.mjs` 的 `sendQuestionCard`、`updateQuestionCard` | POST 创建，PATCH 更新原卡 | action.messageUuid 固定，messageId 固定。安全去重窗口从首次可能发送计时，未知结果沿用原 UUID |
| 飞书回调 `acceptCard` 与 agent 回答 `acceptAgentAnswer` | 重复点击、回调重投、相同 agent 回答重试 | nonce 绑定原卡，event_id 去重单次回调，nativeMessageId 绑定一次答案回填，终态阻止再次作答 |

### 身份分工

- `questionId` 标识整个问题批次，也是 publish、卡片、公开问题收据的主幂等键。DSH 的新批次由原生 sessionId/callId 派生。
- `sourceQuestionItemIds` 标识原生批次中的每个子问题，用于精确回填。恢复后的外层 turnId 不参与新问题 UUID。
- `event_id` 标识一次投递或回调事件。同一问题在不同事件中被重复发现时，仍由 questionId 收敛。
- `nonce` 校验回答对应原卡与绑定，持续使用原值。它独立于投递去重。
- `messageUuid` 负责飞书消息创建去重，`nativeMessageId` 负责一次回答的原生写入回执。

### N 次重复的真实结果

同一脚本同时调用原生请求、公开 publisher、任务 capture、扫描与 outbox，最后再重复 20 次回答操作。原生 wrapper turnId 被刻意改成 20 个值，以覆盖身份漂移。

```text
# f247fdd 基线，当前隔离脚本和 fixture 原样覆盖到临时源码快照后运行
recipient=trainer repeats=20 nativeQuestionItems=20 nativeProviders=20
publicQuestionIds=20 cards=20 receipts=20 answerReceipts=1 answeredWaiters=1 nonces=20

recipient=hermes repeats=20 nativeQuestionItems=20 nativeProviders=20
publicQuestionIds=20 cards=20 receipts=20 routerNotices=1 answerReceipts=1 answeredWaiters=1 nonces=20

# feat/dsh-worker-fixes
recipient=trainer repeats=20 taskCaptureCalls=20 nativeQuestionItems=1 nativeProviders=1
publicQuestionIds=1 cards=1 receipts=1 routerNotices=0 answerReceipts=1 answeredWaiters=20 nonces=1

recipient=hermes repeats=20 taskCaptureCalls=20 nativeQuestionItems=1 nativeProviders=1
publicQuestionIds=1 cards=1 receipts=1 routerNotices=1 answerReceipts=1 answeredWaiters=20 nonces=1
```

基线命令中的 `--expect-fixed` 会因旧实现失败而退出。基线复现采用不带断言参数的命令读取计数，修复后加参数执行断言。

```bash
node worker/dshworker/tests/repro-media-question.mjs
node worker/dshworker/tests/repro-media-question.mjs --expect-fixed
```

完整小图 SHA256 为 `312e1ed663786627529bee6281a867d849f1781a9f8ecffcaa7a7ecb558b9da5`。超过图片上传阈值的测试载荷为原 PNG 加尾部填充，SHA256 为 `808122bbc8ca7e16b2303e1519487f7b6fceb777ac55cf1a8610be8734a8db38`，用于验证原字节转文件上传。

## Codex 共同路径另列问题

### router 通知并发重复

`QuestionRelay.forwardToInitiator` 在进入时检查 routerDelivery，随后等待 getTask，再写 sending。两个调用都能在 getTask 期间通过旧状态检查，随后各发一次 notice。真实调用方包括 `deliver` 和每秒 `process`，慢 router 查询足以形成重叠。

隔离脚本保留真实 `QuestionRelay.serial`，仅替换持久化和网络为内存 sink，用 barrier 让两次 getTask 查询重叠。

```bash
node worker/dshworker/tests/repro-question-forward-race.mjs
```

```json
{"engine":"codex","simultaneousForwardCalls":2,"routerNotices":2,"finalStatus":"sent"}
{"engine":"dsh","simultaneousForwardCalls":2,"routerNotices":2,"finalStatus":"sent"}
```

该问题仍可复现，按工单的 Codex 共同问题单列要求留待后续。建议另单将状态复核与 sending 占位放入同一次 serial，或为每个 questionId 共用通知发送 Promise，补慢查询与后台 process 重叠用例。

### 图片与显式发布

Codex 的公共图片解析默认也只接收绝对路径 Markdown。相对路径和 `MEDIA:` 的支持由本轮 DSH 适配器启用，Codex 对应能力另列待办。MCP 发布问题省略 questionId 时，每次调用生成新 UUID，调用方应保存显式 ID 来重试。

## 验证

macOS arm64 原生 Node v22.22.0 的定向回归命令及结果如下。

```bash
node --test worker/dshworker/tests/dsh-public-media.test.mjs worker/dshworker/tests/dsh-media-question.test.mjs worker/dshworker/tests/dsh-questions.test.mjs worker/dshworker/tests/dsh-compat.test.mjs
```

```text
# tests 32
# pass 32
# fail 0
# cancelled 0
# skipped 0
```

双侧全量和 router 实际命令及结果如下。

```bash
ROUTER_TEST_PORT=18790 node worker/check-compat.mjs
```

```text
platform=darwin node=v22.22.0
shared_root_modules=37 matched=37 mismatched=0
codexworker passed=834 failed=0 skipped=8 total=842 exit=0 signal=none
dshworker passed=805 failed=0 skipped=8 total=813 exit=0 signal=none
router TOTAL 138/138 PASS exit=0 signal=none
WORKER_COMPAT_PASS
```

本机另以原生 Node v26.13.1 执行 `node worker/check-compat.mjs --workers-only`，同样得到 Codex 834 通过、0 失败、8 跳过，DSH 805 通过、0 失败、8 跳过。最终原生 Windows 结果在提交后的 CI 记录中补充。

首轮 CI 的 macOS Node 26 暴露了夹具启动竞态，压缩用例读到 requested。原 send 辅助函数只等待一次 setImmediate，实际 ensureAgent 仍可能在写 checkpoint，随后测试提前发出 turn/end。用 100ms 原生创建延迟复现得到 `send-returned nativeAgentStatus=idle nativeInputs=0`，150ms 后才为 `running/1`。夹具现等待真实 user/message 或明确终态，finish 等待原生回合完成及 checkpoint，再做公开输出断言。

Windows 测试包含真实路径、命名管道和进程生命周期，DSH 引擎与飞书 HTTP 使用隔离 fixture。真实 DSH 模型看图与手机端卡片外观另需生产验收。

## 生效边界

当前生产 cordis patch 仍指向 `/Users/kibyou/projects/mowulink-desktop-only/worker/dshworker/index.js`。本轮产物在独立修复分支中，隔离链路回归通过后，部署新版并重载 DSH 插件才能让生产使用这些适配。router 与 Hermes gateway 没有新增部署依赖。
