# CodexWorker · Codex Desktop 插件

0.2.38 是 DSH 接口的 **desktop-native 分阶段移植**，不是另一套 Codex 会话宿主。
所有会话写入使用客户端注入的原生任务工具；公开输出增量读取已核验的本机 Desktop
完成事件日志。没有启动 Codex 可执行文件或第二个模型宿主的代码。本版本已清空与
Hermes/溪流通信的旧层（入站入口与 `/report`、`/heartbeat` 上报），只保留 Desktop
原生任务与飞书。

0.2.1 修复原生调用缺少 `callerSource: "codex"` 的兼容问题；依据本机官方
`codex-app-tools 0.1.5` 包装的实际请求形状。该字段缺失时可以列出工具，但调用会被
桌面端以 `Invalid app tool request` 拒绝。已通过实际 stdio 插件、注入管道和原生测试
聊天的 HTTP→任务→事件闭环，不再仅以工具中继作为依据。

## 运行方式（CLI）

`bin/codexworker --port 8905` 由 `identity.json` 决定引擎：`transport: "app-server"`
选择 `codex/transport/app-server.mjs`（`codex app-server` JSON-RPC），`stateDir` 保持
`~/.codex/state/codexworker`。选择顺序为 `identity.transport` →
`CODEXWORKER_NATIVE_TRANSPORT` → 拒绝启动。`cli.mjs`、`desktop-manager.mjs`、
`mcp-business.mjs` 与 `worker/dshworker` 逐字节相同，引擎差异只经 transport 能力
声明：app-server 未声明 `capabilities`，因此 fork / desktopJournal / callerTurn
全部按 Desktop 兼容语义为 `true`，行为与改造前一致。

## 已实现

- 0.2.38提供明确放弃旧文字的本机鉴权入口。保留原UUID和unknown结果，只增加
  训练员已放弃的审核记录，解除该条对后续回传的阻挡；不会重发或伪标sent。
  仅作用于同一当前绑定的确切unknown条目，写盘失败不解除阻挡；未审核未知项
  仍保留保护。详见[旧回复放弃与后续恢复](../../references/abandoned-text-release-20261102.md)。

- 0.2.37为文字发送保存分块UUID、载荷哈希、发送阶段、脱敏API码和成功消息ID，
  unknown保留正文。新增只读回执核查：每次最多3条/每条3页、同指纹5分钟退避，
  只写独立诊断报告，绝不重发或修改原账本。空搜索结果不是未送达证明，正文相同
  也不能证明原UUID；旧无回执消息仍需人工核对。见[核查记录](../../references/text-receipt-audit-20261102.md)。

- 0.2.36保护不可读的状态文件：初始化读取/结构校验失败时，清理只释放自有资源，
  不把默认空状态写回原回执文件。脚本分别报告代码全零损坏、状态损坏和待人工
  核对的状态恢复，不把HTTP在线当成会话/回执已恢复。禁止自动回滚旧会话状态。
  `scripts/installation-recovery.mjs --prepare <已核实规范源码> <本机状态目录>`
  由维护时显式固定安装身份和发布哈希；看门狗只能从该固定版本原子恢复同长度
  全零业务mjs，先备份损坏文件。源码变化、合法编辑、截断、路径/身份不符均不
  自动覆盖。固定启动、受管缓存、凭据、会话、队列和未知回执不参与代码修复。
  恢复后给原supervisor最多90秒正常启动预算，不另起writer或杀健康实例。
  证据和未完成的状态核对见[安装损坏验收](../../references/installation-corruption-validation-20261101.md)。

- 0.2.35 修复输入刚被原生接口接受就撤掉 Typing 表情的问题。接收回执和等待回复
  独立保存，不阻塞后续输入或队列；仅把输入之后的新公开消息与其关联，飞书确认
  发送成功才撤表情。积压旧输出、旧卡片延迟返回和未知发送不能误清后来输入；
  热加载继续保留等待，终态清理也不能越过尚未确认的公开发送。
  见[Typing 清理时机验收](../../references/typing-progress-validation-20261028.md)。

- 0.2.34 修复切换聊天后 `wait_threads` 仍携带首次绑定旧轮次的问题：调用者仍是
  原绑定管理聊天，轮次从经过文件/线程/cwd 核验的真实生命周期日志刷新，游标持久保存。
  普通本机 follow-up 使用明确核验的空闲/忙时合同，免除状态读取前提；queue 与指定
  轮次回答仍校验最新执行状态。只读状态查询失败时，可使用目标日志的真实状态，
  不把未知当成空闲，不重放未知写入。图文有序卡片与 `/resume` 行为保持。
  见[实时调用轮次验收](../../references/live-status-caller-validation-20261028.md)。

- 0.2.32 的 `/resume` 按训练员最近发起/继续对话的活动时间排序，每页10个，可用
  `next`/`prev`/`page N` 翻页、`search 关键词` 搜索，也可按完整标题或ID恢复。
  只读本机主会话目录补齐分组和大轮次的轻量元数据，不读取整轮来列目录；排除
  归档、子agent、远程/云端和原工作区之外的任务。最近活动用Desktop recency，
  加上飞书明确恢复/切换/接受新输入的时间；后台输出不会顶掉真实用户活动。
  编号仍从0开始，翻页和编号选择不重排上次列表；没有固定5条总量限制。
  见[活动排序与翻页验收](../../references/resume-recency-validation-20261028.md)。

- 0.2.31将大轮次作为正常场景：输入只查询原生活动状态，不再被整轮历史超限拒收；
  公开文字、已展示图片和生成图按真实完成事件增量同步，持久字节游标和 UUID。
  巨大工具输出/base64跳过保留小字段，原生ID去重，未知发送不重放，切换不串话。
  210MB真实记录首次遍历约0.37秒，下一次无新增为0字节。采用 Hermes Agent 的
  原生 post/text 输出，表格/代码块与长消息分段保留；新增 `/help`、`/goal`、`/queue`。
  目标命令交给当前 Codex 的原生目标工具，发送成功不等于目标已设置。自检跟踪
  增量追赶/正常/未知发送；冷启动先确保脚本看门狗，再等 Bridge 就绪。
  详见[大轮次与 Hermes 输出验收](../../references/large-turn-journal-validation-20261028.md)。

- 0.2.30保留为没有可核验本地日志时的后备：30秒退避后只探测一轮，不反复重载。当前已绑定
  本机会话可使用原生wait_threads的真实最新公开文字快照，标记“历史受限”；
  复用消息ID去重、发送前落盘、保存未知结果，未确认发送不重放。此模式不伪造
  历史页、不推断输入关联、不派发排队输入，也不声称补齐中间消息或图片。
  正常历史可读后自动切回；审计保持degraded，明确这个客户端硬限制。
  详见[单轮超限与受限输出验收](../../references/single-turn-limit-validation-20261025.md)。
- 0.2.29修复/resume只剩一个候选：兼容原生侧栏schema v4，补查sections.itemKeys
  中存在、但顶层threads/pinnedThreads未返回元数据的本机Codex会话。通过原生
  单轮只读查询核对真实ID、标题、host和工作区，再与顶层列表去重排序；最多补查
  12项、4并发，未读取引用会提示列表不完整。仍展示最近5个候选，查看列表不
  切换会话或投递输入，编号选择沿用原候选快照。时间戳统一秒/毫秒/ISO格式。
  保留CodexWorkercd33e9c的双模式脚本自检修复，详见
  [分组会话恢复列表验收](../../references/resume-sidebar-validation-20261025.md)。
- 2026-10-24脚本兼容补丁：只读审计、常驻调度和每轮短检查统一解析本机
  `codexworker`状态目录，遵从现有writer的USERPROFILE/HOME，并支持显式
  stateDir进行只读审计；不再默认读取
  气槽专用目录。自动恢复同时支持既有desktop-bridge与feishu-local，必须重新
  核对本机配置中的桥名称、machine、原生通道与运行模式；不能为监控关闭Hermes
  或放宽身份校验。诊断提示要求保留原模式和Hermes通道。每轮结果附带
  rulesetVersion=20261024-desktop-bridge-v1，以确认新短进程实际加载了规则。
- 0.2.28修复飞书心跳正常但当前会话输出同步已失败的漏检。Desktop对批量历史
  返回确切的通用请求拒绝时，同一会话只读窗口按10/5/2/1降级并短暂缓存，保留
  完整最新轮次，不冒称是尺寸错误或已读回旧正文。selected relay公开错误、失败
  次数、最后成功同步时间；脚本检查该状态和120秒同步停滞，旧业务则读取本机
  选中会话状态兜底。持续失败最多请求一次正常业务重载，仍失败才升级诊断。
  有界业务交接期间延后检查，正常空闲会话仍持续验证同步，不以“很久没新消息”
  误报。详见[历史读取失败与漏检修复](../../references/relay-health-validation-20261022.md)。
- 0.2.27+codex.20261021.script1把外层巡检改为独立常驻脚本；原每5分钟模型
  heartbeat已暂停。正常检查及成功恢复不消耗模型token；持续故障确认后，最多
  自动恢复一次，仍失败才向既有维护会话提交一条诊断任务。持久化发送意图，未知
  发送不重试，同事故去重及一小时升级冷却。每次检查使用新短进程，后续验证过的
  修复写入watchdog-remedies.mjs后会自动在下一次检查加载。诊断任务必须回填
  修复代码和复现测试。业务版本和飞书内置看门狗仍为0.2.27，现有writer无需重启。
  详见[脚本巡检验收](../../references/script-watchdog-validation-20261021.md)。
- 0.2.27增加独立于重连循环的3分钟飞书看门狗，主动发送应用心跳，15秒无响应
  则退役该连接；失败/卡住的重连循环按代际重新启动。健康连接只探测，不发送聊天
  消息或重复上线通知。健康接口公开最近收帧、发ping、连接时间和巡检/修复计数。
  启动守护器使用可信机器sidecar指向的用户安装源，避免插件缓存被清理后无限
  启动缺失server.mjs；已有健康进程的短暂业务交接按连续45秒故障计时。
  固定层验收新增desktopWatchdogVersion=0.2.27，保留原0.2.23安全启动门禁；
  本次必须正常重载客户端才能更新固定层，业务热换不能替代。只读诊断入口为
  scripts/check-desktop-health.mjs，可供独立于Bridge进程的5分钟会话巡检调用。
  详见[主动看门狗验收](../../references/connection-watchdog-validation-20261021.md)。
- 0.2.26新增飞书本机监控模式`operatingMode=feishu-local`：飞书使用本机Codex
  原生适配器，不包含 Hermes 局域网入口或远端上报/心跳。示例为
  `desktop-config.feishu-local.example.json`，原配置另行本机备份。默认`desktop-bridge`
  保留同一原生适配器组合方式；配置错误仍拒绝。模式变更走正常
  全业务交接，固定启动层保持0.2.23。详见
  [外出网络下的本机飞书监控验收](../../references/feishu-local-monitor-validation-20261020.md)。
- 0.2.25修复大历史会话无法/resume切换：识别原生历史读取上限及RPC层尺寸错误，
  切换仅验证当前单轮；普通同步遇到尺寸拒绝时按10/5/2/1收缩，30秒或新轮次时
  重试扩大，session.historyWindow明示实际范围。旧巨型轮次即使单轮仍超限时不会
  冒称恢复。选择建立历史边界，窗口扩大后只基线化新出现的旧轮次/图片一次，防止
  补发旧内容；实际新输出继续转发。本轮只改业务，固定层保持0.2.23，详见
  [大历史切换修复](../../references/large-history-resume-validation-20261018.md)。
- 0.2.24修复“连接恢复”通知约48秒重复的问题。长会话请求10轮并带原始工具输出
  会触发Desktop响应超限，旧代码把请求失败标成通道断开，进而被45秒保活检查
  重建。公开同步现在使用includeOutputs=false，保留公开消息及原生图片节点。
  原生插话正文仍按确切条目、单轮游标补读，完整段落/来源保留，缓存有界；不把
  截断正文当作送达关联，无法完整读取时明确报该请求不可用，不猜测或重发输入。
  显式success=false仅表示请求被原生工具拒绝，通道已回答；真实管道故障仍为未就绪，
  未知原生写入仍不重试。本轮只改业务，可在0.2.23固定层上正常热换。详情及实机
  观察见[恢复通知循环修复](../../references/recovery-notice-loop-validation-20261016.md)。
- 0.2.23 修复嵌套 Windows inventory Job 回收后 Bridge 消失的启动缺口。
  0.2.14 的 breakaway 可以只脱离内层 Job，仍被不允许 breakaway 的外层清理 Job
  回收；新增两层 Job 隔离测试已复现旧版 HTTP ECONNREFUSED。
  启动器改为使用 Windows 正式 `PROC_THREAD_ATTRIBUTE_PARENT_PROCESS`，仅以当次
  已核验的 Desktop MSIX 主进程为父进程，继承它自己的 Job/Token；不改 Job 限制、
  不提权、不换 broker、不安装登录任务。握住 owner handle 后复核映像、创建时间
  和存活状态，防 PID 重用；所有拒绝均停止该启动路径。管道只留在当次内存环境。
  MCP 所有请求都等交接结果再创建业务 worker，避免同时到达的 tools/list 继承
  未确定的冷启动门禁。CIM 查询错误按未知处理；保活日志仅落本机有界状态文件。
  当前固定层验收锚点为 `bootstrapVersion=0.2.23`、
  `desktopLifetimeLaunchMode=windows-desktop-parent`、`desktopStartupHandoff.active=true`；
  下述0.2.14锚点是历史记录。必须正常重开客户端加载固定层，业务热加载不能替代。
  若旧入口由普通MCP承载，升级先暂停自动业务激活以保留收发；带确切0.2.23维护
  标记的暂停仅在新启动交接实际确认后自动恢复，训练员原有的普通暂停不被改动。
  隔离测试覆盖 EOF、并发 inventory、单层/双层 Job 及进程树回收，并持续检查10秒；
  真实无聊天冷启动和首条飞书回环仍须分别验收。详见
  [嵌套启动回收修复记录](../../references/desktop-startup-nested-jobs-20261011.md)。
- 0.2.22修复初始化失败后的MCP代理永久拒绝连接。READY=0的非writer实例只能
  连接Bearer鉴权、原生ready及桥名称校验通过的已有writer；没有健康入口仍返回
  desktop_lifetime_unavailable/retryable=true，不启动另一个writer。CHILD=1但
  READY=0仍明确拒绝，既有生命周期保护不降低。固定启动层仍0.2.14。本机失效
  `.dsh/secrets`配置另经验证原账号/原Token后迁入机器`.codex/secrets`，不携带
  秘密分发、不切换会话；详见[凭据路径与代理恢复验收](../../references/credential-recovery-validation-20261011.md)。
- 0.2.21按真实手机截图精简提问卡片：短选项只显示在按钮中，不在正文再列一遍；
  长选项正文完整保留，按钮仅显示编号。去除重复状态、浮窗说明和整段会话UUID，
  后台绑定、原问题ID/nonce/答案均不变；已回答仍保留完整题目、选项和所选答案。
  当前绑定的已知旧卡只PATCH原ID换版，不重新发卡、不重投原生答案。外部提问不再
  额外调用原生提问面板，避免飞书已答而本地widget还等答案；这是提问路线修正，
  不是原生面板自动回填功能。本机0.2.20已取得真人单题按钮回流，详情见验收文档。
- 0.2.20为明确发布的普通澄清问题增加飞书JSON 2.0选项按钮及长连接卡片回调。
  单题点击即提交，多题分别选择后提交；无选项的自由说明仍走普通文字入站。
  完整题目、完整选项和所选答案保留。独立持久化question-actions.json使慢原生
  写入不阻塞回调ACK；核对训练员白名单、确切消息/聊天、随机nonce和绑定代际，
  不相信回调自带的答案正文/目标。重复点击不重复投递，未知原生写入不自动重发。
  卡片绑定发布时的原生活动turn；原turn结束/中断或接入会话切换时自动PATCH原卡为
  已过期、去除按钮，不发替代卡。回答后显示已回答及完整答案，取消显示已取消。
  不另设卡片TTL：已核对本机异步提问浮窗30秒收起只清选中UI，不一定失效。
  无活动源turn时发布关闭态卡片，不接受旧问题来启动另一个turn。
  普通文字回答由助手使用settle-question.mjs精确收尾，不重发答案；若显式绑定
  sourceQuestionItemIds，可观察公开native答复标记。不是原生面板自动拦截/回填。
  原生工具没有原子expectedTurnId，发送前复核不冒充原子闸门；完整manager仍未启用。
  已知旧文字卡无message_id，不能伪称全部旧卡已经改过。Hermes终态继续使用原
  四字段progress上报。只改业务层，固定bootstrap仍0.2.14。详见
  [交互提问卡片验收](../../references/question-cards-validation-20261005.md)。
- 0.2.19修复飞书网络假死时无限等待close/error的缺口。原代码心跳发送异常被
  忽略、没有握手/响应期限；连接仍OPEN时可以永久卡住。现在握手最多15秒，
  open立即发送首次ping，每次待回应心跳最多15秒；响应或事件帧证明链路存活，
  正常空闲无需聊天。沿用服务端PingInterval，未收到配置时默认120秒，因此不能
  承诺静默断线立即发现；发送明确失败则立即结束该次连接，5秒后自动重试。
  重连不依赖旧连接发出close，旧代的迟到回调不能清除新连接，stop清理定时器。
  `integrations.feishu.connection`公开脱敏阶段、尝试计数、超时/心跳周期与错误代码；
  `capabilities.feishuReconnectPolicy=bounded-handshake-heartbeat`。
  只更新业务层，未改变固定启动、绑定、原UUID收件箱、native未知写入纪律或Hermes
  服务端。真实本地WebSocket静默丢帧测试已恢复并收到输入，不重启客户端。
  此修复不证明此前保活器为何退出，不以网络测试关闭BRIDGE-STARTUP-005。
  详见[网络恢复验收](../../references/network-recovery-validation-20261004.md)。
- 0.2.18修复热加载在连续后台工作下长期pending的问题。沿用既有固定层
  inspect/quiesce合同，业务层收到交接意图先暂停新轮询、排队调度、上报、图片和
  心跳，排空当前操作；不是修改启动模块，也不强杀旧writer。新HTTP/MCP业务
  返回503/工具错误`bridge_reloading`、retryable=true（尚未开始原生操作）；
  鉴权后的快照/控制读取仍可用。飞书仍按原message_id持久化并ACK，延后调度，
  下一代恢复原UUID。原生任务继续执行，不以任务忙态阻止交接。
  默认45秒超时恢复旧服务、30秒退避；坏候选校验失败也恢复后台工作。health/runtime
  的businessHotReload.handoff暴露策略、阶段、暂停状态与脱敏阻塞计数，不暴露内容。
  完成须核对实际代际/业务PID/SHA，不能以reloadScheduled或重启成功代替热换验收。
  多聊天MCP代理不拥有监听server，不能在交接窗口启动writer；只由已有保活器的
  lifecycle child建立常驻入口。冷代理有限等待或明确拒绝，reload工具转发到
  实际writer，不能以代理的业务版本冒充入口升级。
  详见[主动交接验收](../../references/hot-handoff-validation-20261004.md)。
- 0.2.17 修复图片遇到短暂网络失败便永久停发的问题。持久化上传/消息/完成阶段、
  image_key、首次可能发送时间和各阶段尝试次数；上传与消息阶段各最多三次。
  消息重试始终复用原UUID、原image_key和原目标；按飞书官方一小时UUID去重保证，
  实现采用首次可能发送起50分钟的保守窗口，重试、Token等待和重启均不延长窗口。
  服务端明确权限拒绝不重试，窗口过期或预算耗尽也不盲发。后台文字入站不等图片。
  记录脱敏错误阶段、异常类型/网络码、上游HTTP状态与Feishu码，不存原始异常正文、
  Token/响应体或路径。收到真实回执但本地保存失败时仅重试保存，不重复POST。
  新版中断记录可按已保存image_key恢复；旧版未知记录仍不自动恢复。
  授权维护者可用loopback `POST /bridge/images/recover`，正文仍只有imagePath、
  threadId与原messageId，恢复同一当前绑定代际中的未知公开图片；字节/目标指纹
  和原UUID必须匹配，旧记录从原createdAt计50分钟，超窗拒绝，不改账清零/换UUID。
  health能力为`feishuImageRecovery=bounded-same-uuid-50m`。该条仅替代下面历史
  图片出站“任何未知结果永不重试”规则，不扩展未知原生任务输入的重试权限。
  详见[图片可靠发送与恢复验收](../../references/image-recovery-validation-20261004.md)。
- 0.2.16 按训练员新授权修正图片目录策略：当前绑定聊天公开展示的本地图片，
  即使归档到工作区以外的 Wiki 目录，也自动回传；授权的手动发图也不限本机目录。
  每次仅授权那个确切文件，不扩大目录，不接受HTTP/MCP root/force覆盖参数。
  自动发送仍须来自当前绑定的持久化公开图片队列，不扫描工具/推理或任意文件；
  入站缓存及其真实路径别名不自动回传。鉴权、本机路径、绑定代际、PNG/JPEG/GIF/
  WebP、10MiB、UUID与未知结果不重发仍保留。health新增
  `feishuPublicImagePathPolicy=exact-public-display`。
  0.2.15明确在上传前被目录检查拒绝的条目，仅在同一绑定的新原生回读中找到时，
  复用原UUID恢复一次，并保留previousFailure；不重置已送达/未知/取消/旧绑定记录。
  以下0.2.15目录限制为历史策略，由本条替代，不再要求逐机器加图片目录白名单。
- 0.2.15 按训练员授权，自动把飞书当前选中聊天的新公开图片作为真正的 image 消息
  回传：覆盖已完成的原生 `imageGeneration.savedPath`、`imageView.path` 和助手
  回复中的绝对本地 Markdown 图片。不扫描目录、命令/工具输出、私有推理或提示词，
  不回传用户输入原图与飞书入站缓存。同一 turn 同一路径重复预览只发一次；多图
  按发现顺序独立发送，文字入站不等上传完成。切换及发送前复核绑定代际，切走再
  切回也不会让旧在途图继续发送；已开始的网络请求仍不能撤回。
  允许根除现有工作区外，只增加本机 `<CODEX_HOME>/generated_images/<当前threadId>`，
  不放开整个用户目录或其他聊天的图片。仍限 PNG/JPEG/GIF/WebP、10MiB，远端
  设备图片必须由该设备的 Bridge 发送，不能把远端 C: 路径当本机文件读取。
  持久化自动队列与固定 UUID，复用图片回执；未知发送结果不重发，失败反馈错误码，
  不向飞书泄露路径/秘密。鉴权 health 的 `integrations.feishu.publicImages` 提供
  pending/sent/failed/canceled 与 lastError；`capabilities.feishuPublicImageSync`
  为 `selected-thread-public-images`。`feishu.autoImageOutput:false` 可关闭。
  首次升级/登记/切回时基线化既有图片，不突然补发历史；之后的新公开图片才回传。
  这是业务更新，固定启动层仍0.2.14，不要求重启客户端；必须核对实际代际/SHA。
  [图片自动回传验收](../../references/public-image-relay-validation-20261004.md)。
- 0.2.14 补上 Windows 启动探测 Job 回收的生命周期缺口：Node `detached:true`
  不保证脱离短 inventory 的资源 Job；只关闭 stdin/EOF 的测试不足以验证常驻。
  新增本地隐藏启动器，在 Windows 明确允许的条件下交接到 Desktop 生命周期，
  仍核对真实 MSIX/PID/创建时间，并在 Desktop 退出时关闭自己托管的子进程。
  管道仅继承当次内存环境，不落盘、不上命令行，不启动独立 Codex/模型。
  启动器和隔离测试使用训练员授权的进程级 `RemoteSigned`；不改全局策略，
  不使用 `Bypass`、不覆盖组织策略。Job 不允许交接时明确失败，不换通道绕行，
  后续业务调用也不能偷偷建立随短 MCP 消失的冷启动 writer。
  固定层验收同时要求 `desktopStartupHandoff.active=true`、
  `businessHotReload.bootstrapVersion=0.2.14` 和
  `businessHotReload.desktopLifetimeLaunchMode=windows-job-breakaway`。
  本机隔离 Job 回收测试中旧0.2.13连接断开，新0.2.14保留同一存活 writer；
  137项回归及 stdio 测试通过，不等于真实“不发聊天的冷启动”已经验收。
- 0.2.13 修复首次启动的常驻交接：MCP initialize 回复之前，等待同一真实 Desktop
  生命周期的保活器和鉴权 HTTP writer 确认就绪；短暂 inventory 连接不能先启动
  一个随自己退出的飞书入口。保活器自己的 adapter 才负责冷启动，原 MCP 只代理。
  冷态进程查询有限重试，运行中的一次 CIM 异常只暂缓检查，不关闭健康 adapter；
  明确 Desktop 退出仍停止其子进程。启动超时增至60秒，保活交接最多40秒。
  `desktop-lifecycle-events.jsonl` 保存非秘密阶段/PID/退出码，64KiB滚动并保留一份。
  当时固定层以0.2.13为验收锚点；后续实测仍有启动探测被回收的缺口，
  当前应按上面的0.2.14启动方式和固定层字段验收，业务版本号不够。
  不为此强杀旧 writer、重启工作中的客户端或改受管缓存。
- 0.2.13 上线通知默认等待同一飞书连接稳定30秒，再验证当前原生会话可读取；
  中途断开的短连接不发成功卡片。机器可调整 `feishu.onlineStableMs`（0～300000）。
  同一 Desktop 生命周期内后续真实重连标题为“连接恢复”，不再重复“已上线”；
  最近成功通知单独持久化，热换抑制不丢失恢复分类。未接入会话只说连接已建立，
  已绑定但不可读取则有限重试，不报会话就绪。30秒稳定检查不承诺之后永不掉线，
  飞书 API 接受也不替代训练员真实首条消息/表情/回复验收。
- 0.2.11 增加 `bridge_send_image` / 鉴权 loopback `POST /bridge/images`，上传并发送
  真正的飞书 image 消息。参数为 `imagePath`、可选 `threadId`、`messageId` UUIDv4；
  默认工具目标取当前调用聊天，必须等于飞书当前选中目标，不能指定任意 chatId。
  文件只来自允许工作区、state/feishu-images 或0.2.15的当前聊天生成目录，限制 10MiB 及 raster 签名；
  不接受 URL、SVG、越界路径或其他设备本地路径。只有训练员授权的图片才能发送。
  独立 `image-output-state.json` 保存字节/目标指纹与回执，操作租约避免并发覆盖；
  精确重试不重复发图，变更内容返回 conflict，未知结果不自动重试。结果 sent 是
  API 接受，不等于人已打开。上传后及实际发送前复核选择，不向新会话误派。
- 0.2.12 将 0.2.11 的图片模块热加载扩为**全业务代际热加载**：飞书收发、
  命令、会话调度、公开提问、图片及 MCP 业务处理都在同一可替换业务层。
  只固定启动/生命周期、stdio 传输及热加载器；不启动另一个 Codex 或模型。
  `bridge_reload_business_modules`、鉴权 loopback
  `POST /bridge/runtime/reload`（空对象），`GET /bridge/runtime` 查实际哈希/业务版本。
  机器 state 中 `runtime-source.local.json` 的绝对 `sourceRoot` 指向用户拥有的
  插件开发源，约每秒检查整个根目录业务 `.mjs` 图及私有配置变化。HTTP 不能传任意
  源码/路径，配置/密钥不进 Git，私有配置哈希不对外展示。多文件修改前设
  `autoReload:false`，完整修改与测试后恢复 true，避免半成品被自动激活；显式 reload
  仍可越过这个维护暂停。相同源码/配置不重复激活。
  候选隔离导入、校验业务 API/状态版本和工具清单，忙时延后；空闲交接持久化状态、
  队列和 UUID 回执。坏代码保留旧层，候选启动失败恢复原 writer，不强杀或重发未知
  原生写入。稳定 stdio 启动层不退出，HTTP/SSE/飞书可能短暂重连，不承诺零间隙。
  旧业务子进程退出即释放整个 ESM 图，已测连续35代，不再受旧图片加载器32次限制。
  飞书先写持久化收件箱再 ACK，处理失败保留未完成项；图片在收件时固定目标。
  工具清单变化发标准 list_changed，客户端是否即时刷新新增工具仍待真实验收；权限
  和 manifest/.mcp 配置不是业务热加载可绕过的对象。热换首次连接不伪报“重新上线”。
  `reloadScheduled:true` 只表示已调度，须再查实际 generation/SHA、reloadPending 与
  lastReloadError。首次加载0.2.12的新固定启动层仍需正常插件/客户端重载一次；
  已加载之后业务源码更新不需要重启 Codex。架构与分阶段证据见
  [全业务热加载验收](../../references/business-hot-reload-validation-20261003.md)；
  0.2.11 历史图片验收见 [图片出站与热加载验收](../../references/image-output-hot-reload-validation-20261003.md)。

- Bearer 鉴权，包括 `/health`；默认只监听 `127.0.0.1:8903`。
- 现有原生任务登记、发送消息、原生历史回读；可带 `hostId`，走桌面“设备”连接。
- DSH 兼容接口：`/threads`、`/turn`（wait=true/false）、任务读取、last-turn、resume、release/unsubscribe。
- Manager 分阶段接口：messages、sessions、adopt、select、alias PATCH、events（SSE/拉取/Last-Event-ID）。
- `/new` 自动命名，`/fork` 显式指定来源；`/list`、`/switch`、`/status`、`//` 转义。
- `/resume` 从原生桌面候选中按更新时间列出最近最多 5 条本机允许工作区聊天；
  `/resume 0`–`/resume 4` 按刚显示的列表选择，登记未管理聊天而不重发历史。
  不自动刷新数字指向；无列表/越界/目标消失时明确拒绝，不回落到其他聊天。
  0.2.10 改为字面量“编号 0｜标题”，避免飞书 Markdown 自动改成 1～5；回复顶部
  显示当前接入标题，对应候选标记【当前】，当前会话不在五条候选时也单独说明。
  查询列表不改变绑定。
- 2026-10-04 业务修复：飞书只转发当前选中会话的公开过程、最终回复与 Hermes
  输入回显，`/resume`、`/switch`、`/new`、`/clear` 的选择均沿用同一目标。
  未选中会话继续执行并记录终态，但不向飞书混发；再次切回
  不重放被屏蔽的旧输出。Token 等待、长正文分块和鉴权重试前也复核选择。
  切换前已开始的 HTTP 请求可能稍后到达，不能撤回它冒充严格零在途；不会继续
  开始发送旧会话的新内容。鉴权 health 的 `capabilities.feishuOutputRouting` 为
  `selected-thread-only` 时表明该策略已加载。
- `/clear` 与 `/new` 一样创建并选中新聊天，旧历史不删除。
- UUIDv4 幂等、重复消息指纹验证、每条队列消息独立原生 turn、重启恢复。
  JSON 字段顺序不影响幂等重试，升级前保存的回执仍兼容；实际改动内容明确拒绝。
- 0.2.4 原生输入只传训练员/Hermes 的原文，不再把 `[Hermes Bridge messageId=… source=…]`
  加进正文。消息关联在机器本地持久化：以绑定的原生发送任务、正文摘要和发送前的
  turn 快照确认回读；相同文字的 FIFO 消息分别关联，歧义明确拒绝、绝不猜测或重发。
  原生发送任务必须先绑定；旧版已在途标记仍可回读，已经保存的旧聊天记录不改写。
  客户端的跨会话原生消息卡片样式不属于此修复，不承诺变成普通 composer 用户气泡。
- 0.2.10 按训练员明确授权，在真实飞书入站的正文前加 `飞书：`，Hermes 入站加
  `超级溪流：`；前缀与第一句同行，原始段落/空行不删改。原始回执、幂等指纹及
  Hermes report 保留原文，原生关联按实际带前缀的正文核对，旧在途消息不重发。
  斜杠命令仍先解析，普通本地 MCP 消息不伪装成飞书。可设 `messageSourceLabels:false`
  保持旧显示。客户端的 delegated user bubble 默认折叠两行，Bridge 没有默认展开
  参数；这不是入站截断，来源前缀也不等于修复客户端折叠，不改受管应用二进制。
- 0.2.5 普通飞书输入和 `bridge_message` 默认即时送入；本机活动聊天通过客户端原生
  `send_message_to_thread` 接收，不再固定等待 turn 完成。本机发送的来源为目标聊天
  本身，不借用旧验证聊天或另起模型。UUID/来源仍不进入正文。回读确认输入在预期
  活动 turn 中才发 `turn.steered`；若发送期间原 turn 已结束、新 turn 接收，则发
  `turn.started`。0.2.5 曾让连续输入等待前一条历史确认，0.2.6 已移除该阻塞；显式
  `delivery:"queue"` 仍逐条在空闲时启动新 turn。机器配置可用
  `feishu.defaultDelivery:"queue"` 主动选择旧排队行为，缺省为 `steer`。
- 0.2.6 支持连续多次插话：原生发送接口成功返回即释放下一次发送，不等上一条被模型
  读取、回读出现或整轮结束。发送保持 FIFO，每条原文独立插入，不合并、不重复发送。
  `message.dispatched` 表示原生接口已接受；`dispatch_pending` 只表示真实 turn 关联
  仍待后台回读，并非输入排队。每条输入分别持久化关联，慢速背景读取不占用入站写锁。
  同文批次仅在回读中按可核对的原生 FIFO 一一关联；部分回读不影响继续投递，额外
  匹配或未知写入不猜测、不自动重试。旧单条在途状态与未发送 steer 队列可恢复。
- 同一机器多个 MCP 插件实例共用一个 HTTP writer；OS 命名管道租约防止注册表并发覆盖。
- 训练员原生输入与公开助手输出的增量观察；原生输入只观察上报，不二次调度。
- DSH 飞书模块移植：卡片、分块、token 刷新、WebSocket 入站、公开文字节拍。
  飞书入站必须有训练员白名单；不会给未知访客自动开放执行权限。
- 0.2.10 曾在 Bridge 启动/飞书重连成功后发送 `CodexWorker-上线通知`（跟随 bridgeName），
  包含上海时区上线时间及飞书当前绑定原生会话的最新标题、会话 ID。只读取元数据，
  不创建或切换任务、不发送原生提示、不读取私有历史。目标失效如实说明，不改派。
  唯一 writer 发送，每次真实连接一次，失败最多三次且复用同一 Feishu uuid/正文；
  心跳不触发通知。连接状态的 `feishu.onlineNotice` 和机器状态记录 sent/pending/
  failed/unavailable，不能把 API 接受声称为训练员实际看到。可显式设置
  `feishu.onlineNotificationEnabled:false` 关闭。
  0.2.13 的稳定检查、失败就绪不宣告以及“连接恢复”分类替代上述即连即报行为；
  health 另有 stabilizing 阶段，不代表正在等待原生任务完成。
- 公开中间消息标题为 `CodexWorker-输出`，最终答复标题为 `CodexWorker-回复`，跟随 bridgeName。
  0.2.3 修复适配器最终答复漏传 title、错误退回裸桥名称的问题，保留原 DSH 语义。
- 飞书入站添加 Typing reaction，重复消息不重复添加；0.2.6 在对应公开过程卡片被
  飞书成功接受后即可移除，不再等整轮结束。失败发送不会假装已送达，较早的过程
  卡片不清除较晚输入的表情；命令完成、拒绝与原生终态仍作为清理兜底。
  Reaction 添加较慢时也记住清理要求；表情 API 失败不阻止原生消息调度。
- `/bridge/integrations` 提供鉴权后的真实连接状态；
  `POST /bridge/integrations/reload` 只重读机器配置中的飞书，不重启原生任务。
- 0.2.8 增加 `bridge_publish_question` / loopback `POST /bridge/questions`：明确发布
  完整公开提问和选项到飞书 `CodexWorker-提问` 卡片。每个
  questionId UUIDv4 持久化，精确重试不重复已成功发送；失败给 pending/unavailable，
  不假装已送达。该出口不启动原生 turn，不向 LAN 开放。外部文字回答走普通入站。
  原生历史不含 Codex 提问面板，所以这是显式提问出口，不能声称自动拦截所有旧面板。
  插件 instructions 与技能要求外部对话先用此出口，不以 Codex-only 面板作为唯一提问。
- 0.2.8 接收白名单训练员的独立 image 消息，使用匹配消息的资源下载 API，限制
  10MiB 和 PNG/JPEG/WebP/GIF 签名，缓存到本机 state/feishu-images，再把可实际查看的
  文件路径交给当前原生聊天。模型必须用 view_image 看像素；不是把 image_key 当图片，
  也不宣称原生 send 接口已支持附件。原生权限/工作区不变，不向其他设备发本机路径。
  下载失败明确反馈；下载期间选中会话变化则拒绝误派。缓存、Token、资源均不进 Git。

## 已知 Bug

- **BRIDGE-CONTEXT-002**：上下文恢复后助手错误回到已搁置任务；本次没有旧指令
  再入站证据。**BRIDGE-DELIVERY-003**：打开飞书时收到旧消息的训练员报告，
  还缺确切消息/发送时间，待定位，不能声称与前者同源。详见已知Bug记录。

- **BRIDGE-UI-001 · 未修复，训练员确认暂不处理**：真实飞书多回车消息完整到达
  当前原生聊天，但转发气泡默认只预览两行，空行挤占预览；本机截图的「显示更多」
  按钮对比度过低。当前原生发送合同没有默认展开、预览行数或样式控制接口。
  来源前缀修复与传输测试不能关闭此 Bug；不删空行、不改客户端，也不增加未获
  选择的替代面板。复现、接口证据与关闭条件见
  [已知 Bug 记录](../../references/known-bugs.md#bridge-ui-001多回车消息的原生气泡回顾不完整展开按钮对比度过低)。

## 明确未完成

`managerReady` 与 `nativeTaskReady` **仍为 false**。不要把这个分阶段版本交给
要求 1.1.1 全门禁的 Hermes manager 客户端当作正式真桥。

- 当前原生任务工具没有硬中断接口。活动任务 `/stop` 返回 501，不发送伪装的“停下”提示；
  训练员可在 Codex Desktop 中按 Stop。空闲 stop 返回幂等成功。
- 本机同轮输入已通过原生工具实测；不提供原子 `expectedTurnId` 闸门或强制终止。
  不伪造底层 `turn/steer` RPC，`turn.steered` 是原生历史确认后的调度事件。
  远端忙态尚未验证，返回 writer_conflict，可显式 queue，不自动降级。
- 原生历史扫描不是逐 token 流，标称扫描周期 1 秒；工具延迟/会话数量会影响实际同步时间。
  当前读取窗口为最近 10 turns、每项最多 20000 字符，超长或窗口跳过不能称为完整无损同步。
- 不支持逐 turn 覆盖 sandbox/safetyMode；传入非空值明确拒绝，沿用客户端任务本身策略。
- compact 没有原生映射，返回 501。
- 远端发消息路径已有 mock 契约测试，未向正在工作的气槽任务发测试消息。
- 富文本 post 内图片、文件/音视频输入及超限图片的文件出站降级尚未接入。本轮独立原图输入
  和提问显示仍需真实飞书验收，不以 API 接受代替客户端显示或模型看见原图。
- 2026-10-02 单条飞书→当前原生聊天→回复→飞书/Hermes 接收服务回环已通过。
  持续入站已启用；普通消息及切换命令还需训练员在线上操作验收。

## 0.2.33 同一管理上下文兼容

选中的本机聊天同时是管理上下文时，不调用客户端禁止的 `wait_threads` 自等待。
已验证支持空闲及忙时追加的原生接口直接接收普通消息，以持久 UUID 与原生回执为准。
queue 和指定轮次回答仍读取真实执行状态：核验本地日志的线程、cwd、文件身份与选择
代际后，增量读取 `task_started`、`task_complete`、`turn_aborted`，并在写入前复查。
未追上或不完整的日志不能代表空闲。状态未知不放行队列；未知发送结果不重试。
其他管理上下文继续使用原生状态接口；0.2.32 的恢复列表分页、搜索及用户活动排序保留。
自检将本地状态身份故障单独上报，不用重复重载代替真实轮次状态。

普通回复保留标题和会话 ID，使用 Markdown 卡片。正文中显示的本地图片拆出卡片，
通过同一个持久顺序发送“前段文字卡片 → 实际图片 → 后段文字卡片”；不把本地路径
直接交给飞书渲染，也不重复上传已送达的预览。代码内图片示例不上传；长文本继续使用
Hermes 围栏闭合与 Unicode 分块。未知发送回执阻挡后续片段，沿用原 UUID，禁止重放。

## 本机配置与装载

权威源码在本技能的 `assets/desktop-plugin/`；本机开发源目录为
`C:/Users/keith/plugins/codexworker`。不要手改受管插件缓存或应用二进制。
分发包的 `.mcp.json` 使用 `cwd:"."`，由客户端解析为实际安装的插件根目录，
相对 launcher/server 路径随该安装移动，不能写死到旧开发目录。依据本机当前
官方 `codex-app-tools` 原包及其安装缓存的实际解析结果；无需猜测根目录环境变量。
更新安装源时必须包括 `.mcp.json` 和完整运行文件，不能只替换 manifest。
更新开发源后，在客户端正常装载新入口；正在运行的旧 Node 实例不会自动热替换。
首次加载0.2.12固定启动层后，业务层按上述来源自动检查并在安全空闲点热换；
启动/加载器本身、插件 manifest 和权限清单变化仍走正常插件装载。
同一 Desktop 生命周期的 supervisor/writer 租约会保留健康旧实例，新 MCP 可能
代理旧 HTTP writer；只看 MCP serverInfo 或 manifest 不能证明升级。正常安装新版
后应在其他任务安全结束时完整退出/重开客户端，再核对鉴权 `/health` 的实际版本、
飞书连接及 `/manager/sessions` 的选中目标。不得为更新修改租约、强抢旧 writer
或删除机器 state。执行层拒绝仍须正常复核，打包修正不代表已部署或解除拦截。
本机一次定向停止旧 Bridge 后，现有 MCP 连接返回 `Transport closed`，未自动重启。
为不重启其他工作聊天，`scripts/run-desktop-bridge.mjs --desktop-pid <verified PID>`
可在已有 Desktop 注入句柄下维持同一 MCP 插件服务，仅服务本次 Desktop 生命周期。
它只托管 Node 传输，不启动 Codex 可执行文件或运行独立模型；Desktop 退出即关闭。
2026-10-02 后续复查中，本聊天的 Bridge MCP probe/bind 工具已可调用；这与旧 HTTP
writer 是否完成版本替换是两项独立检查。下次正常重载仍由已安装插件启动同一适配器，
共用 writer 租约避免双收。不要在已有 writer 存活时重复启动；启动后读取真实连接状态。

机器配置放在 `.codex/state/codexworker/desktop-config.local.json`，按
`desktop-config.example.json` 填写。此文件、Token、飞书密钥、事件/状态均不得进 Git。
配置缺省时从已绑定的管理任务读取 cwd、从原生项目列表匹配 projectId，读取本机已有
`.codex/secrets/codex-bridge-codexworker.token`。配置或 Token 不满足要求时明确报错。
插件初始化会尝试启动 HTTP 适配器；也可调用 `bridge_start_local_manager`。

0.2.9 在插件首次加载时启动隐藏的 Desktop 生命周期保活器，继承当次注入的管道，
核对真实 MSIX 祖先进程、PID 和创建时间。不保存管道、不添加系统登录任务、不启动
独立 Codex/模型。MCP inventory 连接立即关闭也必须先完成保活器交接；现有 writer
正常时只观察，不抢占。入口消失时按有界退避重启 Node 传输，恢复幂等状态，不重发
未知原生消息。Desktop 退出关闭自己托管的子进程，多次初始化由 OS 租约去重。
本机 `desktop-lifecycle-status.json` 只存 PID/创建时间/阶段等非秘密元数据，要和
鉴权 health 一起核对。旧 PID 在重启后可能被复用；Windows 以已取得的 OS writer
租约为准，不再据旧 diagnostic PID 误判。2026-10-03 已实测旧 Bridge 停止后约
3 秒自动接替、飞书重连、目标不变及 inventory EOF 后常驻存活。再次完整 Windows
重启尚未验收；客户端根本未加载插件时不保证入口可用。

`bridge_register_current_task` 登记当前原生聊天（或明确授权的目标/hostId），不 fork，
不发送消息。`bridge_message` 使用同一管理入口处理消息与命令。模型调用它给其他
任务发消息前仍必须取得训练员授权；Hermes 通过已配置 Token 调用 HTTP 的权限单独管理。

## 事件订阅（router）

配置里加一个数组即开启；不写数组 = 不订阅，行为与旧版完全一致：

```json
{
  "subscriptions": [{ "source": "test", "kinds": ["chat"] }],
  "subscriptionPollMs": 5000
}
```

- `source` 必填，`kinds` 为空/省略表示该来源全部 kind。
- worker 连上 router 的 SSE 后自己 `POST /subscriptions` 声明；SSE 掉线后 router
  会删订阅，重连成功时按配置重放一遍声明（先删掉本进程已知/残留的订阅，避免
  同一事件投两次）。`stop()` 会 `DELETE` 自己的订阅。
- 后台按 `subscriptionPollMs`（默认 5 秒）拉 `GET /subscriptions/pending`，事件
  串行处理，不会并发写同一条会话。
- 每条事件两个动作：投给当前选中会话的 agent，正文前缀
  `[订阅事件 source=<source> kind=<kind> id=<id>]`；同时用本 worker 的飞书身份
  给 `taskBoardOpenId` 发一行 `订阅事件 · <source> · <正文前 80 字>`。
- `CODEXWORKER_FEISHU_DRY_RUN=1` 时飞书只记 `subscription_notify_dry_run`
  日志，不真发，供隔离实例验收。
- 日志：`subscription_declared` / `subscription_poll` / `subscription_event` /
  `subscription_event_finished`（含 `status`、`reply` 前 200 字）/
  `subscription_unregistered`。订阅是实时听，掉线期间的事件不补。

## 验证

在插件目录执行：

```powershell
node --test tests/desktop-manager.test.mjs
node --test tests/desktop-lifecycle.test.mjs
node --test tests/image-output.test.mjs
node --test tests/public-image-relay.test.mjs
node --test tests/plugin-runtime.test.mjs
node --test tests/startup-handoff.test.mjs
node --test tests/feishu-reconnect.test.mjs
node --test tests/question-cards.test.mjs
node tests/server.test.mjs
```

`startup-handoff.test.mjs` 在真实 Windows MSIX 祖先下运行启动/保活/HTTP 子进程，
但用独立临时 state、随机本地端口和假原生管道；没有生产飞书/Hermes 或真实任务写入。
分别验证冷态交接先于 inventory EOF 和 Windows inventory Job 回收，其后同一
writer 存活。测试 Job 只管理测试拥有的 inventory，关闭后会回收其成员；不操作
生产进程。Windows 拒绝生命周期交接时必须失败，不得更改 Job 限制。
非该环境时会明确 skip。
不能将隔离通过冒充当前客户端已加载固定层或真实飞书冷启动验收。

`tests/live-desktop-acceptance.mjs` 是本机验收驱动，原生调用必须由当前已授权的
Codex turn 中继；它不是一个自行连接/启动 Codex 的运行入口。只可对测试任务运行。
位置参数为目标 threadId、workspace、relayFile、实际中继发送任务 threadId；最后
一项必须和原生回读中的来源一致，不能为了通过验收而猜测或伪造。

`tests/live-plugin-acceptance.mjs` 则启动真实 stdio 插件进行 MCP 初始化、鉴权 HTTP
发消息、幂等重试和配对终态回读。必须继承当前桌面端注入的原生管道，并显式传入
已授权的测试聊天 ID；可用 `HERMES_ACCEPTANCE_PLUGIN_ROOT` 指定本机已安装源目录。
可选的第二个位置参数可给先前测试的 messageId，复用其原始请求恢复验收，不另发新消息。
测试结束关闭自己的插件进程，不中断原生任务。原生任务超过等待上限时不能当作失败
重发，应先读取原生终态，再以原 messageId 恢复。测试进程不是正式常驻插件实例。

`scripts/send-feishu-image.mjs <absolute imagePath> <selected threadId> <UUIDv4>
--staged-acceptance` 是训练员授权图片出站的单次模块验收。在旧插件仍工作时可只
验证新模块，读取已有鉴权 health/选择及机器外部飞书密钥，不启动另一个 Bridge、
WS收信或原生任务。回读验证真实 image 消息；输出明确区分模块验收与常驻运行版本。
重试复用同一UUID，不能因没有看到图就自动另发一条。
0.2.17可在授权恢复明确的当前unknown公开图片时追加`--recover-unknown`；仅在
原createdAt的50分钟窗口内复用原UUID/字节指纹，复核原绑定代际，不清账/另建UUID。
脚本不修改manager自动队列，输出autoQueueUpdated=false；新版实际加载后再调用
`/bridge/images/recover`取sent回执同步原自动记录。单次模块恢复不证明常驻已升级。

## 飞书切换约束

2026-09-30 仅准备移植，保留 DSH、不发送线上消息。2026-10-02 训练员授权今天
联通飞书与超级溪流，并指定当前聊天为入口；只读检查未发现运行中的 DSH 服务后，
已进行有时限的单条线上验收且完整回环通过。随后训练员明确授权启用，机器配置
已装入 0.2.2 原生适配器，飞书持续连接和 Hermes 上报已启用。保留当前聊天作为
初始目标，可用 /resume 或 /new /clear 切换。仍需核对普通消息、原生显示、训练员
插话上报和回复卡片，并保留回切 DSH 的操作。不能让两个桥抢收同一应用的消息。
阶段证据见 `../../references/feishu-superstream-contact-validation-20261002.md`。

输入/事件中不复制 private reasoning、工具参数、工具结果和认证元数据。
事件按类型保留 60 分钟/30 天，并受 256MiB 事件区上限约束；清理发生后提高
replayFloor，旧游标返回 replay_too_old 与线程恢复指引。状态/幂等表/上报 outbox
不计入这个事件区上限，需在正式生产门禁前补总存储治理。Hermes 原有上报合同没有
幂等键，网络失败重试为至少一次，不能承诺 Webhook 恰好一次。

来源、哈希和改动说明见 `THIRD_PARTY_NOTICES.md`。
