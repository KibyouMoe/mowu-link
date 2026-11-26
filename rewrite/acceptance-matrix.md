# 魔物链接重做版验收对照

程序与产品基线见 Wiki 的 00–05，程序图文版本为 `9db82a32af701ba5e8af39a545f9bf0c771b5600`。本页记录已跑通的范围和仍需具备的切换条件。源码版本、复跑命令和单项证据见[实施进度](replacement-progress.md)。

隔离协议测试使用独立 SQLite 和网络端口。真实宿主一栏只计实际运行的宿主组件。用本地模型驱动或测试平台的项目，仍需要按 03 的实际界面判据验收。

## Router、Trigger 与溪流

| 能力及原使用者 | 重做版入口 | 目前的验收范围 | 切换前补证 |
| --- | --- | --- | --- |
| 溪流建单、查单、修改、指定、挂起、收回与验收 | A01–A06，Hermes 管理工具 | 隔离 Router 已核空闲连续 10 秒、双端接管、修订、转派、新派发号、取消重试及入池。挂起通知没有复工。 | 真正授权的溪流测试身份从管理界面执行；核实际运行版本。 |
| Worker 在线、接管、继续、受阻、交成果 | W01–W04，适配器已登记成果文件的暂存 | HTTP/SQLite 回环已核丢回包、同键回读、旧派发 409、持单释放后停在待验收。DSH 原生工具已走通带附件的 W04，上传回执丢失无重复分块，正式报告前仅发送工位能读取。模型为本地驱动。 | 真实 Codex 工具调用与结果；DSH 真模型及其生成文件的登记；原生工作环境清理。 |
| 旧处理人迟答、另交材料 | W04 与独立候选材料入口 | 隔离 Router 已核转派竞态、候选幂等、记材料后保持原状态、旧清理只作用于原派发。实际 DSH 原生工具从搁置通知暂存原件并另交纯附件候选。提交后管理端读回相同字节，转给另一工位后只回读原材料号。 | Codex 真会话、DSH 真模型及溪流从独立管理工位实际下载。 |
| 工单提问与原题回送 | W05、A08–A09 | DSH 与独立 Codex App Server 原生工具跑通原题、同派发回送及最终 W04。管理端原答复重投不重复回送。DSH 另验答复回包丢失的查回和继续持单。 | 真人在限定的测试应用和会话看见、作答，原卡更新。 |
| Trigger 输入、文件与 Info 查询 | T01–T04、A07、H02 | 真实隔离 HTTPS 上报、断传续传、缺附件和错误权限已核。邮件用标准 IMAP 客户端连本地测试服务器，游戏走临时 HTTP SSE。试用采集按 T04 和宿主收件核清后，旧客户端各从已核位置取下一条。桥回退或待核回执阻止准备。[源端续接](evidence/feed-source-return.json) | 授权的实际测试源、生产消费位置和游戏桥原生启动标识。 |
| Hermes Event、Notify、Info 与 Feishu 提示 | C01–C03、H01–H02 | 真实隔离 Hermes 插件与本地会话。Notify 空闲 10 秒逐条，Info 未唤醒。Router 断开时，已入箱的 Notify 继续发送，丢失的带附件 Event 回执查回原生原号。平台使用飞书 SDK 或服务器替身。[离线收件](evidence/hermes-router-outage.json) | 独立线上测试身份、真实 Feishu 消息号与客户端侧时间对证。 |
| 旧 Trigger 订阅、历史、策略 | 来源实例游标、离线归档、受审核的迁入和累计确认回退 | 现役 0.7.0 只读盘点查到一个活跃订阅。隔离实测迁入未读批次，T04 丢回执后重开和重建均幂等，附件逐字节一致。已用真实旧 Trigger HTTP 验证按连续收件回退，前序未知时拒绝准备，重试旧确认不再新增，原未投递尾项保留。[迁入回执](evidence/trigger-unread-handoff.json) · [回退证据](evidence/trigger-return-prefix.json) | 现役消费者和旧待发送队列的切换窗口，新来源及平台回执对账，其他部署规则与在用的旧订阅调用。 |
| 同伴通信、附件、旧聊天 | Worker 私聊、Router 授权附档与历史、旧飞书私聊离线导出 | 隔离 Router 验接收方、重复回执、长文、附件摘要。同机实际 DSH 与 Codex App Server 已通过原生工具互发图文和关联回信，两端模型为本地脚本。旧同伴记录仅原参与人查询，旧飞书私聊可在离线副本按工位分页导出。 | 真实授权的两个测试工位互发；旧飞书私聊档的实际保留范围和仍使用旁听接口的消费者另验。 |
| 进程启动、版本回退与历史保全 | `rewrite.service`、`release_check`、`codex_release_check`、`state_bundle`、`cutover` | 两版 Router 与 Worker 分步安装和回退。独立 Codex App Server 已核原会话、单次待验成果和多会话快照复原，前一版适配器接回原持单。隔离演练涵盖七库恢复、原旧工单与附件、已验收迁入单原号归档和结果私有保留。已验收原单与未派新单可一起回退；回放中断后按原号重送无重复。在途迁入单可冻结新派发，恢复原派发并查回丢失的 W04，验收后再回旧版。[混合回退](evidence/cutover-mixed-rollback.json) · [在途排空](evidence/cutover-inflight-drain.json) | 宿主无法核回、派发已变化及其他试用写入的回迁，真实 profile 跨机、Windows 启动和生产切换窗口。 |

## Worker 篇 A01–A11

| 用例 | 已有隔离证据 | 仍须在实际两端看到 |
| --- | --- | --- |
| A01 上线 | 首次上线和断后恢复各记一条，重复轮询未新增。原生飞书 SDK 用本地 TCP 断链、取址请求无响应、两轮重试耗尽三种故障测过，同一进程重连，原事件只进 Worker 一次。平台明确关闭重连时记停并保留收件账本。[取址恢复](evidence/feishu-sdk-discovery-timeout.json) · [耗尽重试恢复](evidence/feishu-sdk-retry-exhausted.json) | 两次 Feishu 在线卡、实际消息号和云端重连行为。 |
| A02 纯文 | DSH 真循环收到本地平台原文。Codex Desktop 有独立会话原生收件，输入走的是 Desktop 驱动。 | 从真人测试飞书到固定 Codex 会话，原文、回复、身份顺序一致。 |
| A03 纯图 | DSH 和独立 Codex App Server 的原生请求均包含测试原图完整字节，模型由本地脚本替代。 | 真模型识图和 Feishu 纯图入站，Codex Desktop 独立会话同验。 |
| A04 同条图文 | DSH 与独立 Codex App Server 原生输入包含原样空格、换行和原图 SHA-256，重投原消息无第二条。 | 真模型按同条原文指认图像细节。 |
| A05 工具与最后答复 | DSH 原生持久记录区分中间输出、工具与最终答复。独立 Codex App Server 已实测命令仍在执行时出现工具预览卡，结束后单发回复。隔离恢复沿用原发卡正文和幂等号，已有平台回执时只补 Worker 确认。换绑前审计两份展示账本，拒绝尚未投递的尾项和游标错配。[回执恢复](evidence/feishu-receipt-restore.json) · [队列审计](evidence/feishu-handoff-display-tail.json) | 真 Feishu 与 Codex Desktop 的原生工具流、原卡时序及云端回执。 |
| A06 中间有图 | DSH SDK 图片与独立 Codex App Server 的公开目录原图均实测前卡、单图、后卡，文本在各自卡里继续更新。Codex 原文件删去、丢 Worker 上传回执仍只形成一份原图。隔离快照恢复后，飞书图片资源上传可退避重试，会话只显示一张原图。 | 真 Feishu 消息号、Codex Desktop 的原生输出与生成图。 |
| A07 问训练员 | DSH 计时题与独立 Codex App Server 已核同组多题、原卡更新和逐题跳过。DSH 计时题续答支持多选。Codex 关题回执丢失时保持待核，含密题的整组留在宿主。DSH 显式启用前台题转接后，两端先答、超时续答和取消都已核同一张测试卡。模型、答题客户端和飞书用夹具。 | 真人点卡，同时核对实际飞书与 Desktop 原题窗。 |
| A08 问溪流 | Router 送原题，训练员视图为只读；答复回到原派发，DSH 走原生工具。 | 真人核对只读控件与原卡更新。 |
| A09 工单双端 | 工位先存收到、更新、取消提示。丢失宿主回执时仍能看见。独立 Codex App Server 的 Router 取消已核原派发精确中止、其他回合保留，以及待清理不释放。 | 同一实际飞书聊天与目标 Codex Desktop 会话的派单、更新和取消，及各宿主资源清理凭据。 |
| A10 断 Router | 真实回环故障不关闭宿主，测试宿主继续上报进展；20 分钟重连与 Router 侧离线计时分开。 | 正在工作的实际独立宿主继续执行，Feishu 同步可见。 |
| A11 断适配器 | 真实回环分段断连，Router 看到仍在线、宿主忙闲未知，飞书队列只有该链路提示。 | 真机两端实测和恢复。 |

## 宿主能用的指令及未完成边界

| 宿主或入口 | 已有证据 | 待核 |
| --- | --- | --- |
| 通用 Worker | `/help`、`/start`、`/commands`、`/status`、`/list` 测试平台端已核。原消息号按原回执重投。DSH 与独立 Codex App Server 通过原生 `/queue` 顺序与丢回执查证。 | 实际 Feishu 私聊的所有指令入口与交互。 |
| DSH | `/context`、`/model`、`/effort`、`/queue`、`/goal`、`/compact`、`/stop` 有 SDK 或真循环证据。`/resume` 支持浏览与冷会话恢复。`/new`、`/clear` 和 `/fork` 经隔离 SDK 自动登记换绑，原生验证默认和继承的模型力度。独立只读工位已验证取消、转派、撤下旧派发排队输入、保留私聊及丢 W04 回执后重试。前台题已跑原生等待、关闭和恢复回执。 | 真实飞书操作、真实 DSH 模型与客户端验收、读写工作区清理凭据及跨机原生历史恢复。 |
| Codex Desktop 与独立 App Server | Desktop 真实独立会话收到一条正式单，重投仍一条。独立原生 App Server 接到本地脚本模型的动态工具调用，实际完成 W04，重试回同一结果。`/model`、`/effort`、`/context` 与 `/stop` 已核原生回包。独立只读工位从飞书测试消息经 `/new`、`/clear`、`/fork` 自动登记和换绑，原消息重试复用同一会话，丢回执和重开皆已实测。原生分叉的父会话历史没有成为子会话新收件。自动登记目录已纳入离线迁移与回退。 | Desktop 测试项目未列出 `worker_report`，默认 App Server 测试 profile 无登录。两条路径的真实模型 W04 仍需各自的授权测试条件。真实账号的请求强度、跨提供方选择、真实飞书操作、Desktop 冷会话。 |
| Antigravity | 已核安装版本 2.32.1 及官方会话钩子、转录、MCP 入口。CLI 可克隆 App 对话供独立宿主使用，Remote Control 提供人工访问。 | 指定原桌面会话的后台唤醒、停止、真实 Gemini 回报及读写验收。CLI 或远程托管须先确认允许改用独立宿主。[兼容性核对](antigravity-compatibility.md) |
| 授权和维护 | 认证的 Router/Worker/适配器隔离已验证；普通题卡不能执行原生授权。 | 旧 `/approvals`、`/commands`、`/ops/services` 中实际仍在使用的入口，与宿主授权逐项验证。 |

当前可以复跑隔离联调。离「可切换」还差真实宿主、实际 Feishu 测试身份、原生清理和活跃消费端的迁移对账。

## Codex 现役桥工具逐项对照

工具名取自维修主会话的现役注册表。表中写明哪一段已验。迁移时仍需在可加载测试工具的独立 Codex 会话确认原生能力。

| 现役工具 | 重做版入口或归属 | 可核的迁移条件 |
| --- | --- | --- |
| `bridge_probe` | Worker `worker-status`，加适配器连接观测 | 独立 TLS 实测断链、恢复、持单和忙闲未知。现役 Codex 启动链路切换须单验。 |
| `bridge_binding_status` | Worker 状态与固定原生会话校验 | 隔离 TLS 可区分预期会话与已确认会话，同身份错绑零输入。 |
| `bridge_bind_manager` | 独立运行参数、停写换绑及运行中交接窗口 | 隔离冷切保留两端图文、收据、原卡和正式成果。运行中切换已在 Codex App Server 和 DSH 间往返，旧适配器的回调被拒，回执丢失和目标掉线可按原号恢复或撤回。等待首次目标确认期间仍可接工单，撤回后待处理私聊各按原号回原会话。可恢复预先登记的冷会话。Codex Desktop 的界面批准和真实飞书 `/resume` 待验。[入箱顺序](evidence/feishu-resume-inbox-order.json) · [预检撤回](evidence/feishu-resume-preflight-cancel.json) |
| `bridge_verify_manager_binding` | 按工位授权的 `worker-status` | 平台身份被拒，运维身份可核同一会话。 |
| `bridge_inspect_native_contract` | 运维 `worker-status` 读取当前绑定会话的 `native_capabilities` | 权限或会话对不上时不采信外部观测，断开时能力未知。DSH 与独立 Codex App Server 分别保留目录、停止、历史压缩、原生目标等实际操作的回执。 |
| `bridge_verify_native_write_path` | 原派发或消息号、正文摘要、原生输入及回合 | 真实独立 Desktop 已核文字收件及重投去重。无人值守入口、图文与实际授权待验。 |
| `bridge_start_local_manager` | Router、Worker、宿主适配器独立入口 | 临时进程与分步升级已跑。独立 App Server 工位初始化能冷开查回唯一输入，适配器已用 TLS 与专用 profile 启动，拒绝第二个写者。现役 Desktop 受信启动链仍待单验。 |
| `bridge_reload_business_modules` | 锁定提交的发布包、分步升级与原快照回退 | 已演练冷切，原收件无重复。原位热加载及现役启停授权尚未核。 |
| `bridge_message` | 平台私聊入口与宿主斜杠命令 | DSH 文图、已接命令与原队列有实测。独立 Codex App Server 的目录查询、图文、排队和普通回合中止已核，可从已确认目录切到已配置的运行中或冷会话，也可自动接入原生新会话和分支。Desktop 冷会话、原生插话和真实 Feishu 仍待验。 |
| `bridge_send_image` | 已授权的宿主图片事件，经 Worker 转送 | 真实 DSH 图像摘要及隔离平台顺序已核。需要独立飞书云端与 Codex 输出记录。 |
| `bridge_publish_question` | 原生问题适配；无原生题窗时用 `worker_ask_trainer` | 独立 Codex App Server 和 DSH 计时题已核批量回答及原卡更新。DSH 续答可多选，前台题通过两个测试答题入口验证。Codex 组内任一密题整组留宿主。两套宿主已实测独立题卡和原会话单次答复，模型及飞书为本地替身。待验实际飞书点击、日用宿主的独立测试身份和 Desktop 原题窗。 |
| `bridge_register_current_task` | W03 接管及独立原生收件 | 原派发会话已固定，错绑零投递。DSH 与独立 Codex App Server 均已核原生目标完成仍持原派发，收到正式询问后经 W04 报告。模型为本地脚本，日用环境另验。 |
| `bridge_create_work_order` | 溪流侧的正式建单与授权 | 已有隔离 Hermes 管理入口。现役 Worker 直接建单的调用边界及是否保留需要定案。 |
| `bridge_work_order_status` | 溪流侧正式工单查询、工位本地持单状态 | 已核管理查询和本地状态，Worker 直接按旧单查他端成果的兼容范围待定。 |
| `submit_deliverable` | Agent `worker_report` 经 W04，待溪流验收 | 真实 DSH 与隔离 Codex App Server 的原生工具协议通过，模型为本地脚本。Desktop 工具注册和真实模型验收仍缺独立工位。 |
| `bridge_list_workers` | 原生 `worker_list_peers` | 真 DSH 已查询对端。单独登记、在线和权限筛选已核。 |
| `bridge_send_worker_message` | 原生 `worker_send_peer` 与三层收件查询 | 两个独立 DSH 实例互发及回原消息通过，模型为本地驱动。需要授权的实际跨宿主工位。 |
| `bridge_worker_inbox` | 原生 `worker_peer_inbox` 的待办和回复历史 | 真实 DSH 查原信后关联回复，游标和只读性有回归。需 Codex 工具实机复验。 |

## 旧命令、审批与维护的实际边界

| 入口 | 查证结果 | 切换条件 |
| --- | --- | --- |
| `POST /commands`、`/commands/responses` | 旧 Codex 与 DSH 的 `desktop-manager.mjs` 通过 Router 转发 `/model` 和 `/effort`，`router-runtime.mjs` 按原请求号回复。重做版由同工位的 `worker/command` 接收已认证的训练员命令，隔离宿主已核设置与回读。 | 实际飞书链路与真实账号的模型设置仍待核。源码查到旧插件消费者，其他调用方需由使用者核对后才可停旧入口。 |
| `/approvals` | 隔离 Codex App Server 已核命令、文件差异、单次允许与取消。Router 未收到登记或允许回执卡住时，原生已中断，恢复按原号补记关闭。DSH 已核 Bash 单次允许、拒绝和超时，默认与相对工作目录可由原生宿主核实。批准前变更目录时不执行，执行已绑定后仍在批准的目录运行。冷开能查回未确认登记的原生取消，保留已有 Router 决定且零补发通知。C01 仅通知指定审批人。[Codex 命令](evidence/codex-app-server-approval-accept.json) · [文件变更](evidence/codex-app-server-file-approval.json) · [Codex 断联](evidence/codex-approval-router-outage.json) · [DSH 断联](evidence/dsh-approval-router-outage.json) · [DSH 工作目录](evidence/dsh-approval-native-working-directory.json) | 真实审批人和飞书提示，Desktop 原生 Hook、应用授权、网络以及额外权限仍待核。普通题卡不承载审批。[操作入口](README.md#独立宿主命令审批) |
| `/ops/services`、`/ops/services/{name}/restart` | 本机溪流的 `gateway-restart` 运维说明明确引用旧接口；Codex 运维说明也用它查询 LaunchAgent。重做版独立 HTTPS 维护服务按目标授权。真实 macOS 临时 Router 已测丢回包、原号查回不重复重启、原待派单和原建单幂等键保留。另核已登记停机项恢复，以及不相符 plist 及已加载参数时保持待核。[回执](evidence/host-ops-launchd.json) | 现役调用迁移、实际授权、Desktop 自有子进程、Hermes 网关实际运行形态和其他系统的管理入口仍待独立核对。 |
