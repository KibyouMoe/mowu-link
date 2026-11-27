# 魔物链接重做版验收对照

程序与产品基线见 Wiki 的 00–05，程序图文版本为 `9db82a32af701ba5e8af39a545f9bf0c771b5600`。本页记录已跑通的范围和仍需具备的切换条件。源码版本、复跑命令和单项证据见[实施进度](replacement-progress.md)。

隔离协议测试使用独立 SQLite 和网络端口。真实宿主一栏只计实际运行的宿主组件。用本地模型驱动或测试平台的项目，仍需要按 03 的实际界面判据验收。

2026-11-27 起按 Wiki 05 的全量替换范围验收，共 49 项。原表 50 项中，14 项保留新版业务并移出旧版相关判据，1 项纯旧版验收移出。移出项不计通过。旧客户端、旧接口、旧运行格式和新旧并跑均退出验收。新版的停写备份、状态恢复、版本回退及写入对账继续验收。

截至 2026-11-27，49 项中 4 项已实测可替换，剩余 45 项。11 项仍需实现或隔离验证，其中 Antigravity 缺符合当前要求的受支持入口。其余 34 项已有隔离证据，仍需实际宿主、测试身份、来源、部署或授权验收。一行涉及多个宿主时，以该行尚未通过的条件计一次。已移出的旧版兼容项、旧数据处置和生产切换授权单列。

## Router、Trigger 与溪流

| 能力及原使用者 | 重做版入口 | 目前的验收范围 | 切换前补证 | 验收状态 |
| --- | --- | --- | --- | --- |
| 溪流建单、查单、修改、指定、挂起、收回与验收 | A01–A06，Hermes 管理工具 | 隔离 Router 已核空闲连续 10 秒、双端接管、修订、转派、新派发号、取消重试及入池。挂起通知没有复工。失联入池后请求取消，DSH 回线清理原容器并交 W04，Router 才标已取消；迟到清理回执不关闭新派发。[离线取消](evidence/dsh-offline-pooled-cancel.json) 同一派发的重新取消用新清理号，原回执或错号收 409，新回执丢失后查回原结果。Codex 和 DSH 的新控制均实际起原生回合并获 W04，模型为本地脚本。[回环](evidence/reissued-cancel-w04.json) · [升级](evidence/reissued-cancel-upgrade.json) | 授权的管理与宿主测试身份从实际客户端执行，核正在运行的版本。 | 部分 · 隔离已验，待实际环境 |
| Worker 在线、接管、继续、受阻、交成果 | W01–W04，适配器已登记成果文件的暂存 | HTTP/SQLite 回环已核丢回包、同键回读、旧派发 409、持单释放后停在待验收。DSH 原生工具已走通带附件的 W04，上传回执丢失无重复分块，正式报告前仅发送工位能读取。模型为本地驱动。 | 真实 Codex 工具调用与结果；DSH 真模型及其生成文件的登记；原生工作环境清理。 | 部分 · 隔离已验，待实际环境 |
| 旧处理人迟答、另交材料 | W04 与独立候选材料入口 | 隔离 Router 已核转派竞态、候选幂等、记材料后保持原状态、旧清理只作用于原派发。实际 DSH 原生工具从搁置通知暂存原件并另交纯附件候选。提交后管理端读回相同字节，转给另一工位后只回读原材料号。隔离 Hermes 原生分发可在 `router_act_on_task` 以明确的材料号结单，同号重试回原处置，Worker 原受阻回执保留。[管理工具](evidence/hermes-manager-candidate-review.json) | Codex 真会话、DSH 真模型及溪流从独立管理工位实际下载。 | 部分 · 隔离已验，待实际环境 |
| 工单提问与原题回送 | W05、A08–A09 | DSH 与独立 Codex App Server 原生工具跑通原题、同派发回送及最终 W04。管理端原答复重投不重复回送。DSH 另验答复回包丢失的查回和继续持单。 | 真人在限定的测试应用和会话看见、作答，原卡更新。 | 部分 · 隔离已验，待实际环境 |
| Trigger 输入、文件与 Info 查询 | T01–T04、A07、H02 | 真实隔离 HTTPS 上报、断传续传、缺附件和错误权限已核。邮件用标准 IMAP 客户端连本地测试服务器，游戏走临时 HTTP SSE。新版来源可按实例查看留档和缺 T04 回执数，Router 可查投递连续确认位置。 | 授权的实际测试源、原生游戏桥启动凭据，以及新版实例游标和实际收件的对账。 | 部分 · 隔离已验，待实际环境 |
| Hermes Event、Notify、Info 与 Feishu 提示 | C01–C03、H01–H02 | 真实隔离 Hermes 网关以原生控制接口创建 LOCAL 会话。Event 和 Notify 都有原生完成记录，Info 留在 Router。Notify 在 Event 释放后 11.01 秒起跑。Event 经原生工具发现调用 `router_info` 验原件，再用 `router_create_task` 建立测试单。网关冷重开后，插件双侧回执丢失仍查回原号，Router 只保留原单。模型走本地脚本 HTTP，平台使用飞书 SDK 或服务器替身。另核过 Router 断开时已入箱的 Notify 继续交付。[网关实测](evidence/hermes-gateway-trigger.json) · [离线收件](evidence/hermes-router-outage.json) 两份中转记录缺失时从真实 Hermes 原生台账补回原收件，C02 中断续齐 70 条。停写审计核 C02 确认前缀与中转正文。[恢复实测](evidence/hermes-relay-rollback-recovery.json) | 独立线上测试身份、真实 Feishu 消息号与客户端侧时间对证。 | 部分 · 隔离已验，待实际环境 |
| 同伴通信、附件与新版聊天记录 | Worker 同伴私聊、Router 授权附档、Worker 新版平台账本 | 隔离 Router 验接收方、重复回执、长文、附件摘要。实际 DSH 与 Codex App Server 已通过原生工具互发图文和关联回信，模型为本地脚本。新版普通私聊入站和飞书出站回执分别丢失后，从新版组件快照只补原记录，原游标可续查。混合回合的回复与缺首次观测时间的记录有缺证计数。[新版飞书记录](evidence/worker-feishu-chat-history.json) | 真实授权的两个测试工位互发图文并回信，真实飞书收发、鉴权和新版记录续查。 | 部分 · 隔离已验，待实际环境 |
| Worker 与宿主跨机部署 | 独立 `worker` 服务、适配器固定会话及双向 TLS | macOS Codex App Server 和 DSH SDK 分别接 Linux 虚拟机 Worker。Worker 冷重启后保留原持单，原生输入各一条，同一回合重报后 Router 待验收，工单成果通知各一条。无信任证书及身份不符被拒。DSH 本机侧使用双向 TLS，跨虚拟机两向调用使用各自身份与 HTTPS。[Codex](evidence/codex-linux-worker-restart.json) · [DSH](evidence/dsh-linux-worker-restart.json) | 实际分开机器的部署与网络规则、已登录的独立试验工位及真人通知。 | 部分 · 隔离已验，待实际环境 |
| 新版启动、停写备份与版本回退 | `rewrite.service`、`release_check`、`codex_release_check`、`state_bundle` | 已演练两个新版锁定发行包分步安装和回退。独立 Codex App Server 从新版快照恢复原会话、持单和多会话账本，前一版适配器接回原持单并交件。已有试用写入时，回退拒绝覆盖。[托管工位](evidence/codex-managed-bundle-rollback.json) Windows Server 2025 已实跑安装包、ACL 拒绝、冷重启、正式工单及两个新版之间回退，宿主使用夹具。[Windows](evidence/windows-release-rollback.json) 新版五组件停写、校验和恢复有独立入口。[恢复说明](router-rehearsal.md#重做版五组件的离线恢复) 已用锁定的两个新版发行包在 macOS 临时 HTTPS 中退版。前一包读回试用写入的一份 W04、来源附件及私聊回执，原号重试零重复，随后正常验收原单并为新私聊记索引。更早发行包只会读已有索引，已被反例拦下。宿主、模型和飞书用夹具。[试用有写入的退版](evidence/rewrite-state-bundle-release-cycle.json) macOS 和 Linux arm64 已从无旧源码的安装目录验过新版回归，Windows 已实跑带试用写入的新版间退版。[独立安装验收](evidence/clean-install-release-acceptance.json) | 从锁定的新版包独立启动各宿主，核实际跨机 profile、真实平台回执和五组件状态恢复。在不同机器验证目标发行包、原生账本和真实平台的一致恢复。 | 部分 · 隔离已验，待实际环境 |

新版独立安装包已从空账本跑通 Trigger 附件上报、真实 Hermes 原生建单、Worker W04 和同一 Hermes 原生验收。结果从待验收推进到已结单，私聊与正式单分别保留一条宿主输入。Worker 宿主、模型和飞书提供端使用夹具。[空状态演练](evidence/hermes-clean-cutover.json)

同一流程已用独立 DSH 原生会话复验。DSH 的两次 `worker_report` 通过本机双向 TLS 返回相同 W04，同一 Hermes 会话读成果并验收。飞书夹具收到与 DSH 原生私聊回合对应的一条最终回复。两端模型仍由本地脚本驱动。[双宿主实测](evidence/hermes-dsh-clean-cutover.json)

独立 Codex App Server 也完成同一冷启动流程。只读测试线程的两次原生动态工具调用得到同一 W04，Hermes 从待验收读回原成果并结单。正式派单与普通私聊各有一个原生回合，私聊最终文本已在飞书夹具对账。模型仍从本地脚本取响应，Desktop 工具注册与实际云端会话继续验收。[Codex 双宿主实测](evidence/hermes-codex-clean-cutover.json)

## Worker 篇 A01–A11

| 用例 | 已有隔离证据 | 仍须在实际两端看到 | 验收状态 |
| --- | --- | --- | --- |
| A01 上线 | 首次上线和断后恢复各记一条，重复轮询未新增。原生飞书 SDK 用本地 TCP 断链、取址请求无响应、两轮重试耗尽三种故障测过，同一进程重连，原事件只进 Worker 一次。平台明确关闭重连时记停并保留收件账本。[取址恢复](evidence/feishu-sdk-discovery-timeout.json) · [耗尽重试恢复](evidence/feishu-sdk-retry-exhausted.json) | 两次 Feishu 在线卡、实际消息号和云端重连行为。 | 部分 · 隔离已验，待实际环境 |
| A02 纯文 | DSH 真循环收到本地平台原文。Codex Desktop 有独立会话原生收件，输入走的是 Desktop 驱动。 | 从真人测试飞书到固定 Codex 会话，原文、回复、身份顺序一致。 | 部分 · 隔离已验，待实际环境 |
| A03 纯图 | DSH 和独立 Codex App Server 的原生请求均包含测试原图完整字节，模型由本地脚本替代。 | 真模型识图和 Feishu 纯图入站，Codex Desktop 独立会话同验。 | 部分 · 隔离已验，待实际环境 |
| A04 同条图文 | DSH 与独立 Codex App Server 原生输入包含原样空格、换行和原图 SHA-256，重投原消息无第二条。Windows DSH 经独立适配器进程实跑。丢本地收件及缓存后查回原回合。普通文件从原生附件库读回相同字节。[Windows 证据](evidence/windows-dsh-media.json) | 真模型按同条原文指认图像细节。 | 部分 · 隔离已验，待实际环境 |
| A05 工具与最后答复 | DSH 原生持久记录区分中间输出、工具与最终答复。独立 Codex App Server 已实测命令仍在执行时出现工具预览卡，结束后单发回复。隔离恢复沿用原发卡正文和幂等号，已有平台回执时只补 Worker 确认。换绑前审计两份展示账本，拒绝尚未投递的尾项和游标错配。[回执恢复](evidence/feishu-receipt-restore.json) · [队列审计](evidence/feishu-handoff-display-tail.json) | 真 Feishu 与 Codex Desktop 的原生工具流、原卡时序及云端回执。 | 部分 · 隔离已验，待实际环境 |
| A06 中间有图 | DSH SDK 图片与独立 Codex App Server 的公开目录原图均实测前卡、单图、后卡，文本在各自卡里继续更新。Codex 原文件删去、丢 Worker 上传回执仍只形成一份原图。隔离快照恢复后，飞书图片资源上传可退避重试，会话只显示一张原图。Windows DSH 复跑前卡、单图、后卡，发图回执掉线后沿用同一个测试消息号。[Windows 证据](evidence/windows-dsh-media.json) | 真 Feishu 消息号、Codex Desktop 的原生输出与生成图。 | 部分 · 隔离已验，待实际环境 |
| A07 问训练员 | DSH 计时题与独立 Codex App Server 已核同组多题、原卡更新和逐题跳过。DSH 计时题续答支持多选。Codex 关题回执丢失时保持待核，含密题的整组留在宿主。DSH 显式启用前台题转接后，Windows 与 macOS 的双向 TLS 工位均核两端先答、超时续答和取消。每次仅更新原测试卡一次。停写复制会话日志及指纹库后仍能查回原生回执。模型、答题客户端和飞书用夹具。[Windows 证据](evidence/windows-dsh-foreground-questions.json) · [macOS 证据](evidence/dsh-local-tls-foreground-macos.json) | 真人点卡，同时核对实际飞书与 Desktop 原题窗。 | 部分 · 隔离已验，待实际环境 |
| A08 问溪流 | Router 送原题，训练员视图为只读；答复回到原派发，DSH 走原生工具。 | 真人核对只读控件与原卡更新。 | 部分 · 隔离已验，待实际环境 |
| A09 工单双端 | 工位先存收到、更新、取消提示。丢失宿主回执时仍能看见。独立 Codex App Server 已核原派发精确中止和其他回合保留。只读工位等控制回合终止才确认取消或转派。独立可写工位实测容器内文件导出、原 W04 确认后自动回收，以及取消和转派清除原容器内脱离进程组的子进程。模型为本地脚本。[只读清理](evidence/codex-readonly-cleanup.json) · [可写工位](evidence/codex-assignment-workspace.json) | 真飞书、可加载测试工具的目标 Desktop 会话及线上模型的两端验收。 | 部分 · 隔离已验，待实际环境 |
| A10 断 Router | DSH 和独立 Codex App Server 均在 TCP 断开后完成原生写入。受控时钟核对离线入池及重连。DSH 明确转派后只清旧派发。Codex 的在途成果返回可重试的 503，原 W04 重放后如实记录 `stale_assignment`。各有一次原生输入和一张测试飞书断联卡。[DSH](evidence/dsh-worker-link-outage.json) · [Codex](evidence/codex-worker-link-outage.json) | 独立在线模型、实际跨机断链及飞书云端消息。 | 部分 · 隔离已验，待实际环境 |
| A11 断适配器 | 分别断 Worker 到 DSH 和 Codex 适配器的 TCP 链路。Router 保持本体在线、宿主忙闲未知及 W03 原持单，收到一次报警。DSH 恢复后按显式取消清理。Codex 恢复后用同一原生回合交 W04，只有一次成果和一次通知，两项的原输入各一条。[DSH](evidence/dsh-worker-link-outage.json) · [Codex](evidence/codex-worker-link-outage.json) | 实际跨机断链、在线模型、飞书客户端卡片及云端回执。 | 部分 · 隔离已验，待实际环境 |

## 宿主能用的指令及未完成边界

| 宿主或入口 | 已有证据 | 待核 | 验收状态 |
| --- | --- | --- | --- |
| 通用 Worker | `/help`、`/start`、`/commands`、`/status`、`/list` 测试平台端已核。原消息号按原回执重投。DSH 与独立 Codex App Server 通过原生 `/queue` 顺序与丢回执查证。 | 实际 Feishu 私聊的所有指令入口与交互。 | 部分 · 隔离已验，待实际环境 |
| DSH | `/context`、`/model`、`/effort`、`/queue`、`/goal`、`/compact`、`/stop` 有 SDK 或真循环证据。`/resume` 支持浏览与冷会话恢复。`/new`、`/clear` 和 `/fork` 经隔离 SDK 自动登记换绑，原生验证默认和继承的模型力度。独立只读工位已验证取消、转派、撤下旧派发排队输入、保留私聊及丢 W04 回执后重试。前台题已跑原生等待、关闭和恢复回执。可选 Linux 容器试验已证明独立读写目录、脱离原命令进程组的子进程和旧派发均能按取消及转派清理。引擎身份不符会保留待核。容器成果可经附件通道续传，W04 回执落库后自动回收。同号重试仅一次通知，回执待核期间保留容器并拒绝换绑。[取消与转派](evidence/dsh-assignment-container.json) · [附件与回收](evidence/dsh-workspace-artifacts.json) 隔离项目可按固定 Git 快照复制到每个派发。已核导入回执丢失、拒绝危险归档、修改和成果下载，以及重启后清理原派发。[项目快照](evidence/dsh-workspace-project-seed.json) · [同机冷恢复](evidence/dsh-workspace-cold-recovery.json)。实际重开 DSH、Worker 和独立适配器，原任务续用同一容器中已写入的文件，丢 W04 回执后再次重开沿原号释放。独立工位可显式启用 `/workspace` 中的执行。实际编译并运行测试 C 程序后，从 W04 读回相同 ELF。默认工位实测报受阻。配置变更时按原派发的挂载方式清理。[编译和运行](evidence/dsh-workspace-compiled-project.json)。离线换绑核对已停机工位的原账本。实测 W04 已确认、容器尚未回收时拒绝，回收到位后可准备新绑定。版本回退同样核对原工作区。两版安装包实测回收后可恢复旧版，原成果、附件与单次收件齐全。[换绑与版本回退](evidence/dsh-offline-workspace-handoff.json)  macOS 另验本机双向 TLS 下的容器成果续传和 W04 补回后的自动回收。[双向 TLS 可写工位](evidence/dsh-local-tls-workspace-macos.json) 原宿主在线或仍持写锁时，离线换绑与回退均被拒。停机后发布两套输出，旧版读回同一派发的成果。[双向 TLS 回退](evidence/dsh-tls-workspace-handoff.json) | 真实飞书、DSH 模型与客户端、现役工程工作树及容器原工作目录跨机恢复。复杂工程的依赖与构建流程另验。 | 部分 · 仍需实现或隔离验证 |
| Codex Desktop 与独立 App Server | Desktop 真实独立会话收到一条正式单，重投仍一条。独立原生 App Server 接到本地脚本模型的动态工具调用，实际完成 W04，重试回同一结果。`/model`、`/effort`、`/context` 与 `/stop` 已核原生回包。独立只读工位从飞书测试消息经 `/new`、`/clear`、`/fork` 自动登记和换绑，原消息重试复用同一会话，丢回执和重开皆已实测。原生分叉的父会话历史没有成为子会话新收件。自动登记目录已纳入离线迁移与回退。 | Desktop 测试项目未列出 `worker_report`，默认 App Server 测试 profile 无登录。两条路径的真实模型 W04 仍需各自的授权测试条件。真实账号的请求强度、跨提供方选择、真实飞书操作、Desktop 冷会话。 | 部分 · 仍需实现或隔离验证 |
| Windows 独立 Codex App Server | 真实 Windows 原生工具经 Worker 交 W04。原收件重开后查回且未重投。只读工单取消与转派凭原生终止证明释放旧派发，转派保留新持权。已核拒绝共享 profile、重复写者和 Windows 进程树释放。模型和平台为夹具。[Windows 原生回执](evidence/windows-codex-native.json) | Windows Desktop 插件启动、线上模型、可写工作区、真实飞书。 | 部分 · 仍需实现或隔离验证 |
| Windows 独立 DSH 固定会话 | 官方 Windows 包内 AgentLoop、会话持久化和工具执行实跑。双向校验证书并拒绝错会话。丢收件后查回同一输入。冷重开前宿主离线，收件保留待核，重开后查回且模型请求为零。两次原生 W04 得到同一业务回执、只发一条通知。Worker 释放原持单，Router 待验收。独立只读工位实测终止旧回合，撤下原派发的排队输入，保留普通私聊及新持有人。W04 回执丢失可按原号恢复，停写后复制原生记录到新目录可查回等同清理凭据。缺证据保持待清理，拒绝其他普通账户可读的账本。固定会话已接纯图、同条图文及普通文件，回传图片按顺序发卡，拒绝共享缓存。模型为本地脚本。[接单与报送](evidence/windows-dsh-native.json) · [取消与转派](evidence/windows-dsh-read-only-cleanup.json) · [图文](evidence/windows-dsh-media.json) | 独立 Windows Desktop 界面、真实飞书、真实模型读文件及产图，以及可写工作区清理。Windows CI 的默认 Docker 连接两次分别报告 `windows` 和不可用，均未连到当前可写工作区用例所需的 Linux 引擎。[首次](https://github.com/KibyouMoe/mowu-link/actions/runs/45894320633/job/140164337047) · [复跑](https://github.com/KibyouMoe/mowu-link/actions/runs/45894741098/job/140165827775) | 部分 · 仍需实现或隔离验证 |
| Windows 独立 DSH 托管会话 | 测试飞书入口覆盖 `/resume`、`/new`、`/clear` 和 `/fork`。丢登记回执后重开仍查回一个新会话。分支继承父会话及原力度，新回合只进分支。恢复原会话未请求模型。两份正式成果各留一条通知。拒绝未登记会话、宽权限名册与第二个回调写者。隔离回退核对三份子会话库。独立固定会话另验 `/goal` 和 `/compact`。原生目标暂停、续跑期间无 Router 单，丢压缩启动回执后原生压缩与结果通知各一次。[目标与压缩](evidence/windows-dsh-goal-compaction.json) · [Windows 实测](evidence/windows-dsh-managed-sessions.json) | 真实飞书输入、Desktop 独立会话中的同名命令及迁移后的实际身份。 | 部分 · 隔离已验，待实际环境 |
| Antigravity | 已核安装版本 2.32.1 及官方会话钩子、转录、MCP 入口。CLI 可克隆 App 对话供独立宿主使用，Remote Control 提供人工访问。 | 指定原桌面会话的后台唤醒、停止、真实 Gemini 回报及读写验收。CLI 或远程托管须先确认允许改用独立宿主。[兼容性核对](antigravity-compatibility.md) | 受阻 · 缺受支持的入口 |
| 授权和维护 | 认证的 Router、Worker 与适配器隔离已验证。普通题卡与原生授权分别处理。 | 按新版的训练员命令、原生审批、独立运维入口逐项核对真实授权，包含 Desktop 应用授权、用户身份及真实平台回执。 | 部分 · 隔离已验，待实际环境 |

Codex 与 DSH 的 Windows 可写入口共用私有运行账本与 Linux 引擎核对。Windows Server 2025 的 Codex 安装包使用 CLI 夹具验证过冷重开、原附件、错误引擎拒绝及移除回执。Windows DSH 原生包经双向 TLS 向 Worker 暂存附件并提交 W04，按原号补回丢失的回执后只通知一次、释放原派发并回收工作区。重开原会话仍仅一条输入，模型未重新执行。离线审计实测宿主写锁释放后，写账本子进程仍可拦住发布。Windows 使用 Docker CLI 夹具和本地脚本模型，Docker Desktop 的 Linux 引擎上仍待验端到端工单、成果、取消和回收。[DSH 回环](evidence/windows-dsh-assignment-workspace.json) · [Codex 可写工位](evidence/windows-codex-assignment-workspace.json) · [Windows 账本审计](evidence/windows-workspace-journal-audit.json) · [工作区回退实测](evidence/codex-workspace-writer-rollback.json)

Windows 可写工位已有独立验收入口，要求本机 Linux 引擎和精确的预载镜像。Codex 与 DSH 各核成果和清理，报告记录实际引擎、容器、输入和原派发。用例跳过或凭据不全会报失败。Windows CI 验过入口的拒绝逻辑，实际默认引擎仍为 Windows，真引擎两行维持原状态。[复跑说明](README.md#windows-可写工位与真实-linux-引擎) · [实测边界](evidence/windows-linux-workspace-acceptance-entry.json)

新版来源的停写快照可按实例核对 Trigger 留档、Router 收件、T04 与附件实际字节。已用隔离 HTTP 和两个新版安装包验证丢 T04 补回、附件校验和错批拒绝。生产来源清单、真实上游和平台回执继续验收。[复跑](router-rehearsal.md#新版-trigger-对账) · [隔离证据](evidence/rewrite-trigger-snapshot-audit.json)

邮件和游戏来源各有常驻入口。隔离环境实测邮件经 TLS 只读采集新 UID，游戏 SSE 长连接期间独立发送，Router 失联仍继续留档。常驻进程拒绝第二个写者；重启后重放丢失的 T04 沿用原消息号和原附件，Event 只留一条推送。[实测](evidence/rewrite-trigger-source-watch.json)

Linux 用户级 systemd 已演练游戏来源停写。活动采集器持锁时拒绝生成快照。首次停写后，两条原记录分别列为 Router 已收、待补 T04 和仍在发件箱。重启同名单位接续原库，再停写时两条均已对账，原附件字节一致，Event 推送一条。实际来源和生产单位接入仍待验。[复跑](README.md#邮件和游戏聊天来源) · [Linux 实测](evidence/linux-systemd-trigger-watch.json)

macOS 已用真实 launchd 验证两路来源联合停写和封存恢复。仅停一路时整批快照被拒。新目录和 Router 端口沿原号补回待发邮件及已收游戏事件，保存原回执和附件，游戏事件推送仍为一条。恢复后各收一条新记录，两路终态对账均通过，原快照未变化。邮箱、游戏桥为本地测试替身。[macOS 实测](evidence/macos-launchd-trigger-restore.json)

跨机停写可按宿主封存并合并新版组件快照。macOS Router 与 Linux 来宾本地盘 Worker 实测，W04 已落库但回执丢失时，合并快照会显示两边持单不一致及待确认成果。恢复副本后原幂等键找回原回执，原派发释放，原生夹具仅一次收件且正式成果仅一条通知。生产多机窗口与真实宿主仍待验。[操作步骤](router-rehearsal.md#跨机停写与合并快照) · [实测记录](evidence/cross-host-state-restore.json)

切换前持久停派已用隔离 HTTPS Router 验证。已送达且未完成 W03 的单退回原队列，旧派发返回 409。已完成 W03 的单在快照恢复后取回原回执并继续交 W04，只留一条成果通知。解除后再次核空闲再派发，原队首顺序保留。正式停派窗口仍待验。[操作步骤](router-rehearsal.md#切换前持久停派) · [实测记录](evidence/router-dispatch-drain.json)

Hermes 停写对账覆盖 Router 原消息、插件队列及原生适配器账本，并实际校验附件内容和读取授权。隔离真实网关的原生 Event 和 Notify 回执可逐项对应，丢回执先标待核，重开后按原生收件号补齐。缺账本、错会话、篡改缓存与附件均有反例。模型使用本地脚本，现役身份仍待验。[实测记录](evidence/hermes-native-receipt-audit.json)

切换快照按独立部署清单核对发行版本与全部组件。隔离实测漏采尚未上报的 Trigger 来源、飞书或 Hermes 宿主账本时返回待核。工位无持单但缺同伴私聊宿主回执时也能发现漏采。补齐原账本后沿原消息号找回回执，宿主只收一次。正式部署清单仍待切换前确认。[操作步骤](router-rehearsal.md#跨机停写与合并快照) · [实测记录](evidence/cutover-roster-audit.json)

同批切换联合核验已能一次检查锁定安装包、独立清单、快照摘要、Router 原停派号和两份账本。用两版安装包拒绝了未停派、错停派号、残留预约、漏采飞书、T04 缺回执和错误包摘要。完整离线证据按实机及生产授权要求继续验收。[复跑](router-rehearsal.md#同批离线切换核验) · [实测记录](evidence/offline-cutover-checkpoint.json)

停写快照核对完成、受阻的 W04，覆盖双方回执、原请求、成果或受阻条目、附件关联、字节和读取授权。两版安装包退回后读回原成果附件以及原 W04，随后在前一版完成管理验收。使用隔离 HTTPS 和 SQLite，宿主与飞书使用夹具。[操作步骤](router-rehearsal.md#保留试用写入的新版版本回退) · [实测记录](evidence/w04-sealed-rollback.json)

W05 在断线前已写入 Worker 时可随原工位恢复。真实 DSH 原生工具只调用一次，Router 已落库但回执丢失后按原问题号补账。临时 Router 另验请求未送达、原派发已转走时拒绝新提问。两版安装包实测试用时保留同一答案，前一包恢复后原派发只收到一次答复并交 W04。DSH 模型、管理输入及回退宿主为夹具。[实测记录](evidence/w05-recovery-release.json)

A11 隔离复验覆盖两条连接的故障叠加。真实 DSH 原生 W04 先落 Worker 待发箱，成果尚未到 Router 时再断开适配器。只恢复 Router 的成果入口即可沿原键确认一次 W04，宿主仍断联，原生输入及工具调用均无新增。W05 在同样断联时也能补交，答案等原宿主恢复后只收一次。DSH 模型与管理输入为测试驱动，实际飞书和分机部署待验。[实测记录](evidence/adapter-outage-router-outbox.json)

飞书停写快照已能核对 Worker 的原入站回执、出站请求和飞书消息号、卡片更新目标及菜单关联。隔离库实测丢回执按原号恢复、五类错配均阻止换绑、两个新版间回退保留试用回卡。飞书云端和原生宿主使用测试替身。[飞书回执对账](evidence/rewrite-feishu-receipt-audit.json)

飞书新消息丢回执且超过重试窗口时，原请求和同回合、同问题的后续更新进入待核。独立消息继续投递，待核项仍阻止换绑。官方 SDK 对回环服务验证两次实际新建请求、原号零重发，Windows 安装版覆盖三项相关回归。飞书云端和正式账号仍待验。[隔离实测](evidence/feishu-expired-receipt-recovery.json)

Hermes 原生收件和管理飞书通知分别对账。原生确认而平台回执缺失时，停写审计仍要求复核。同一请求号补回后，两版安装包实测回退仍读原通知。过期未知保留待核，正文或消息号错配会被拦截。该演练使用宿主和飞书夹具。[实测记录](evidence/hermes-platform-notice-snapshot.json)

训练员题卡另核飞书回填与 Worker 保存的原生关闭凭据。原题仍待核或原卡、答案、原会话及回合与账本不符时，离线换绑拒绝发布。从停写副本恢复沿原题号确认，宿主关闭和原卡更新各一次。macOS 与 Windows 上实际运行 Codex App Server 和 DSH 原生问答，模型与飞书为测试替身。[实测记录](evidence/feishu-trainer-answer-handoff-audit.json)

当前可复跑隔离联调。A10 的 Worker 恢复及新版备份回退另核了 70 帧断点续接、前缀缺件与同号异文，以及 C02 位置正确但留有失效 W03 的混搭快照。[复跑及边界](replacement-progress.md#检查点与证据) 离「可切换」仍需真实宿主、实际 Feishu 测试身份、原生工单清理，以及新版实际来源和平台回执对账。

## Codex 现役能力与新版入口

首列沿用现役注册表的工具名供能力追溯。按第二列的新版入口验收。仍需在可加载测试工具的独立 Codex 会话确认原生能力。

| 现役能力来源 | 重做版入口或归属 | 验收证据或待核项 | 验收状态 |
| --- | --- | --- | --- |
| `bridge_probe` | Worker `worker-status`，加适配器连接观测 | 独立 TLS 已实测断链、恢复、持单和忙闲未知。以新版 Codex 启动入口核对真实 Desktop 的就绪与断链状态。 | 部分 · 隔离已验，待实际环境 |
| `bridge_binding_status` | Worker 状态与固定原生会话校验 | 隔离 TLS 可区分预期会话与已确认会话，同身份错绑零输入。 | 已实测可替换 |
| `bridge_bind_manager` | 独立运行参数、停写换绑及运行中交接窗口 | 隔离冷切保留两端图文、收据、原卡和正式成果。运行中切换已在 Codex App Server 和 DSH 间往返，旧适配器的回调被拒，回执丢失和目标掉线可按原号恢复或撤回。等待首次目标确认期间仍可接工单，撤回后待处理私聊各按原号回原会话。可恢复预先登记的冷会话。运行中切换核对原适配器未关闭的审批。实测 Codex 和 DSH 原生审批未对账时保留原会话，审批关闭后才接受新切换请求。另用夹具核交接途中到达的原审批及丢解除回执。[审批实测](evidence/native-handoff-approval.json) Codex Desktop 的界面批准和真实飞书 `/resume` 待验。[入箱顺序](evidence/feishu-resume-inbox-order.json) · [预检撤回](evidence/feishu-resume-preflight-cancel.json) | 部分 · 隔离已验，待实际环境 |
| `bridge_verify_manager_binding` | 按工位授权的 `worker-status` | 平台身份被拒，运维身份可核同一会话。 | 已实测可替换 |
| `bridge_inspect_native_contract` | 运维 `worker-status` 读取当前绑定会话的 `native_capabilities` | 权限或会话对不上时不采信外部观测，断开时能力未知。DSH 与独立 Codex App Server 分别保留目录、停止、历史压缩、原生目标等实际操作的回执。 | 已实测可替换 |
| `bridge_verify_native_write_path` | 原派发或消息号、正文摘要、原生输入及回合 | 真实独立 Desktop 已核文字收件及重投去重。无人值守入口、图文与实际授权待验。 | 部分 · 仍需实现或隔离验证 |
| `bridge_start_local_manager` | Router、Worker、宿主适配器独立入口 | 临时进程与分步升级已跑。独立 App Server 工位冷开后可查回唯一输入，适配器用 TLS 与专用 profile 启动，拒绝第二个写者。仍须验证新版经受支持的 Desktop 入口独立启动。 | 部分 · 仍需实现或隔离验证 |
| `bridge_reload_business_modules` | 锁定提交的发布包、分步升级与新版状态快照回退 | 已演练停写后切到新版发行包，原收件无重复。真实宿主的维护授权、启停及回退读回待验。 | 部分 · 隔离已验，待实际环境 |
| `bridge_message` | 平台私聊入口与宿主斜杠命令 | DSH 文图、已接命令与原队列有实测。独立 Codex App Server 的目录查询、图文、排队和普通回合中止已核，可从已确认目录切到已配置的运行中或冷会话，也可自动接入原生新会话和分支。Desktop 冷会话、原生插话和真实 Feishu 仍待验。 | 部分 · 仍需实现或隔离验证 |
| `bridge_send_image` | 已授权的宿主图片事件，经 Worker 转送 | 真实 DSH 图像摘要及隔离平台顺序已核。需要独立飞书云端与 Codex 输出记录。 | 部分 · 隔离已验，待实际环境 |
| `bridge_publish_question` | 原生问题适配；无原生题窗时用 `worker_ask_trainer` | 独立 Codex App Server 和 DSH 计时题已核批量回答及原卡更新。DSH 续答可多选，前台题通过两个测试答题入口验证。Codex 组内任一密题整组留宿主。两套宿主已实测独立题卡和原会话单次答复，模型及飞书为本地替身。待验实际飞书点击、日用宿主的独立测试身份和 Desktop 原题窗。 | 部分 · 隔离已验，待实际环境 |
| `bridge_register_current_task` | W03 接管及独立原生收件 | 原派发会话已固定，错绑零投递。DSH 与独立 Codex App Server 均已核原生目标完成仍持原派发，收到正式询问后经 W04 报告。模型为本地脚本，日用环境另验。 | 部分 · 隔离已验，待实际环境 |
| `bridge_create_work_order` | 溪流经 Hermes 管理工具 `router_create_task` 正式建单 | 隔离 Hermes 网关内已通过原生工具建单并查回，模型为本地脚本。真实管理测试身份待验。 | 部分 · 隔离已验，待实际环境 |
| `bridge_work_order_status` | 溪流侧正式工单查询、工位本地持单状态 | 隔离管理查询与本地持单读回已核，仍须对照真实管理测试身份和指定宿主工位。 | 部分 · 隔离已验，待实际环境 |
| `submit_deliverable` | Agent `worker_report` 经 W04，待溪流验收 | 真实 DSH 与隔离 Codex App Server 的原生工具协议通过，模型为本地脚本。Desktop 工具注册和真实模型验收仍缺独立工位。 | 部分 · 仍需实现或隔离验证 |
| `bridge_list_workers` | 原生 `worker_list_peers` | 真 DSH 已查询对端。单独登记、在线和权限筛选已核。 | 已实测可替换 |
| `bridge_send_worker_message` | 原生 `worker_send_peer` 与三层收件查询 | 两个独立 DSH 实例互发及关联回信通过。Router 收件、对端 Worker 入箱及对端原生收件各有记录。模型为本地驱动，授权的实际跨宿主工位待验。 | 部分 · 隔离已验，待实际环境 |
| `bridge_worker_inbox` | 原生 `worker_peer_inbox` 的待收件与新版回复历史 | 隔离同伴私聊已核当前工位的待收件、最近消息和回复关联。真实登录的独立工位与 Desktop 仍待核。 | 部分 · 隔离已验，待实际环境 |

独立 App Server 还实跑了与 Desktop 预检同一份 stdio MCP 清单。工具目录可读，两次原生调用返回同一 W04，普通私聊调用被拒。审批按原生逐次请求由测试客户端确认。Desktop 共享配置与生产绑定未用于试验，真人审批和真实模型仍待验。[协议与复跑](desktop-check.md#独立-app-server-的-stdio-mcp-报送) · [实测](evidence/native-app-server-project-mcp-w04.json)

## 新版命令、审批与维护

| 入口 | 查证结果 | 切换条件 | 验收状态 |
| --- | --- | --- | --- |
| 训练员指令（`worker/command`） | 同工位 Worker 接收已认证的训练员命令，隔离宿主已核 `/model` 和 `/effort` 的设置与回读。 | 实际飞书链路、真实账号的设置与下一原生回合的模型和力度。 | 部分 · 隔离已验，待实际环境 |
| 原生审批登记与决议回执 | 隔离 Codex App Server 已核命令、文件差异、单次允许与取消。Router 未收到登记或允许回执卡住时，原生已中断，恢复按原号补记关闭。离线换绑审计还覆盖未送到 Router 的 Codex 原生审批，只有适配器核到关闭回执才清除待核。原绑定冷重开可查回 Router 已保存的关闭，原生决定零重放。原生关闭已落盘且 Router 保有原登记时，重开后可按原号补交，丢回执后按原号查回。缺原登记或原生关闭证据继续待核。[冷重开实测](evidence/codex-durable-approval-resolution.json) DSH 已核 Bash 单次允许、拒绝和超时，默认与相对工作目录可由原生宿主核实。批准前变更目录时不执行，执行已绑定后仍在批准的目录运行。冷开能查回未确认登记的原生取消，保留已有 Router 决定且零补发通知。C01 仅通知指定审批人。[Codex 命令](evidence/codex-app-server-approval-accept.json) · [文件变更](evidence/codex-app-server-file-approval.json) · [Codex 断联](evidence/codex-approval-router-outage.json) · [DSH 断联](evidence/dsh-approval-router-outage.json) · [DSH 工作目录](evidence/dsh-approval-native-working-directory.json)。Windows 与 macOS 的本机双向 TLS 已验拒绝、单次允许、超时与原号冷恢复。Windows 用实际 Git Bash 运行一次获准的 `pwd -W`，无确认目录的请求留在宿主。管理员、真实飞书和 Windows 界面未参与。[Windows 原生审批](evidence/windows-dsh-command-approval.json) | 真实审批人和飞书提示，Desktop 原生 Hook、应用授权、网络以及额外权限仍待核。普通题卡不承载审批。[操作入口](README.md#独立宿主命令审批) | 部分 · 仍需实现或隔离验证 |
| 独立 HTTPS 维护服务与维护 CLI | 独立 HTTPS 维护服务按目标授权。真实 macOS 临时 Router 已测丢回包、原号查回不重复重启、原待派单和原建单幂等键保留。另核已登记停机项恢复，以及不相符 plist 及已加载参数时保持待核。[Router 回执](evidence/host-ops-launchd.json)。新增独立维护 CLI。Linux 用户级 systemd 已接入同一维护协议，并在独立 Linux 来宾核验原进程退场、原单恢复、同号查回、登记停机项恢复及拒绝依赖联动。[Linux 实测](evidence/linux-systemd-host-ops.json)。待核重启会阻止换新请求号。维护人用原号核实旧进程退出和新代际，随后才能另发重启。[待核处理](evidence/host-ops-restart-reconciliation.json)。隔离 Hermes 由真实 LaunchAgent 启动，维护回执的新 PID 与 Hermes 原生文件及认证健康接口一致。维护服务冷重开后按原号查回，网关未再次重启。真实网关使用本地 API adapter，整体就绪报告为 `degraded`。[网关回执](evidence/hermes-gateway-maintenance.json) Windows Server 2025 SCM 从安装包验证旧进程退场、已登记停机服务恢复、HTTPS 回执丢失后按原号查回、待核时拒绝换号，以及邻近服务和原工单保全。[Windows 实测](evidence/windows-scm-host-ops.json) 独立启动器已从两套锁定发布包实际注册同一 SCM 服务、升级和退回，唯一原工单得以保留。占用状态库时拒绝启动，结束 Router 子进程后 SCM 停止，重开仍能读回原单。[Windows 发行包](evidence/windows-router-service-release.json) | 新版维护人在授权范围验证服务状态与启停。Desktop 自有子进程、实际模型及平台就绪待核。 | 部分 · 仍需实现或隔离验证 |

## 正式切换清理与旧数据处置

下列内容单列，未纳入 49 项。

- 待正式切换清理的纯旧版适配包括旧 Router 和 Trigger 订阅、游标及累计确认对接，旧工单与私聊的运行格式转换与旧版回写，以及兼容旧 `/chat` 列表及摘要的查询服务。清理底稿见 `legacy_subscription.py`、`legacy_chat.py`、`legacy_history.py`、`legacy_peers.py`，以及 `cutover.py` 的旧版迁入与回写入口、`reviewed_rollback.py` 的旧版回写流程。清理时复核调用者与共用函数，保留新版 `state_bundle` 和新版平台账本的快照、核验及恢复能力。
- 旧客户端的 Router 命令、审批及运维地址列入停用清单。相应业务按表中的新版授权入口验收。已有旧版兼容代码在生产切换获得明确授权后清理。
- 旧数据处置已定。训练员于 2026-11-27 确认可在授权的停写切换窗口内舍弃旧版历史及在途数据。无需导出、迁移、保留或恢复到新版。既有演练文档继续作为历史证据。[范围更正前的演练](router-rehearsal.md)

| 旧服务或进程 | 旧版专用数据路径 | 同步退出的持久或进程内队列 |
| --- | --- | --- |
| 旧 Router `ai.hermes.router` | Mac mini 的 `~/.hermes/router.db`、对应 WAL/SHM、`router.db.attachments/`，以及核实同库归属的成果候选目录和副本 | 任务历史、当前持单、待派 FIFO、待成果与取消询问、未决审批、同伴未读/回执、聊天历史和 Trigger 订阅游标。 |
| 旧 Trigger `ai.hermes.trigger` | Mac mini 的 `~/.hermes/trigger.db` 及对应 WAL/SHM；其他旧 Trigger 实例以登记的实际 `--db` 为准 | 旧事件档、待投递 Event/Notify/Info、订阅、确认回执和游标。 |
| 旧 Trigger 来源进程 | 各实例的旧版专用游标或待发目录；仓内游戏来源默认游标为 `server/trigger/clients/state/mc_chat_cursor.json`，安装覆盖值逐工位确认 | 各实例采集游标、待向旧 Trigger 补发的事件。原邮箱、游戏服聊天与日志继续归来源系统。 |
| 旧 Codex Worker 插件与桥接进程 | 默认 `~/.codex/state/codexworker/`，逐工位核实状态目录及专用文件，例如 `desktop-manager-state.json`、`feishu-inbox.json`、`message-journal/`、`receipt-archive/` 和旧原生收件凭据 | 旧派单待接、挂接的工单状态、飞书待入站或出站、同伴待投递、待题答复、成果与取消回执。 |
| 旧 DSH Worker 插件与桥接进程 | 默认 `~/.hermes/state/dshworker/`，逐工位核实 `dsh-plugin-state.json`、`feishu-inbox.json`、`message-journal/` 与 `receipt-archive/` | 旧派单、工位内等待队列、飞书收发、同伴待投递、题目与旧成果回执。 |
| 旧 Antigravity Worker `interface/sidecar.mjs`（如仍安装） | 以原插件的 `ANTIGRAVITY_EXECUTABLE_DATA_DIR` 实值为准，仅核对其 `worker/` 内的旧 Worker 账本、`public-journal/` 和 `submit.sock` | 旧派单待接、飞书待收、同伴待投递、旧成果和取消回执。Antigravity 原生会话、运行目录其余内容和宿主密钥均由原归属方保留。 |
| Hermes 内的旧 Router 管理入口、旧 `trigger-bus` 插件 | `~/.hermes/trigger-cursor.json` 或实例设置的 `TRIGGER_BUS_CURSOR`，以及核实归属的旧插件专用账本 | 等待插件交接的 Event/Notify/Info、旧任务通知、旧同伴回执。只退出可辨认为旧魔物链接来源的待交付项，Hermes 网关共用队列继续由网关持有。 |

Router 与 Trigger 在 Mac mini 的路径已从各自的 LaunchAgent 参数核对。其余为旧版安装默认路径或源码归属，Windows 及独立工位以切换窗口中核实的实际路径为准。切换前逐项登记主机、服务、解析后的文件路径和队列身份，确认旧写者已停且旧文件与新版和宿主资料分离，再按获批范围处置。不确定归属的项目留待核验。宿主原生会话、账号和配置、共享网关、来源本身、其他项目及新版数据都保留。新版停写备份、恢复、版本回退及写入对账仍按表验收。
