# 现役工具替换进度

外层单 t-124f6357c5。程序契约版本 9db82a32。此表逐项记录可替换条件。「实测」限定到表中指定的宿主、平台和接口。只有全表核验、迁移回退演练和溪流独立复验齐全，才能提请生产切换。

## 检查点与证据

| 范围 | 当前结果 | 复验入口与原始证据 | 下一项 |
| --- | --- | --- | --- |
| 正式主线 17fccee | 真实临时 HTTP Router 与 Worker；双宿主及 Agent 为夹具，14 项 | `python3 -m rewrite.devloop`、`rewrite/test_mainline.py` | 扩充 A/W 接口 |
| Desktop 指定会话 f4f7ba7 | macOS 原生输入确认，原派发重投 0 条额外输入；原生回合结束仍持单 | `desktop-check.md` 中的实际会话、原生输入与命令；`rewrite/test_codex_desktop.py` 5 项 | 独立测试会话工具注册 |
| W04 工具入口 2f69249 | macOS Desktop 当前隔离项目加载受阻。重做 MCP 子进程、私有套接字、Worker、Router 和 SQLite 联通；5 项新增，其中同回合多输入拒报。工具调用者和原生元数据为夹具 | `evidence/desktop-w04-probe.json`；`python3 -m unittest -v rewrite.test_project_report` | 取得可加载工具的专用测试宿主，再取原生工具调用项和业务回执 |
| A01/A03/A05、A06 assign（bb1a4e5） | 真实隔离 HTTP 已覆盖名单、工单列表分页、原单指定与修订。旧派发指令已变时，旧帧不能让宿主执行过时内容；已接管的修改用原派发通知一次。新增 4 项，累计 28 项 | `python3 -m unittest discover -s rewrite -t . -v`，`test_management.py` | 转派与旧清理 |
| 无法完成池、提醒与材料（5d7fd19） | 真实隔离 Router、SQLite、Worker，原生宿主和时钟为夹具。5 项新增，累计 33 项。覆盖续办保留持单、主动报阻、离线确认后连续 600 秒入池、上线清零重计、延后提醒、独立材料幂等及与恢复竞态。材料由认证 Worker 调用 `POST /v1/tasks/{task_id}/materials`，附件待接。 | `python3 -m unittest -v rewrite.test_pool`；`test_stale_completed_is_not_material` 与 `test_new_dispatch_permanently_excludes_new_old_material` | 各宿主接入 |
| 转派、取消与旧清理 | 两个独立身份的 Worker、HTTP Router 与 SQLite 实跑。甲离线期间乙接管后，甲迟报完工被拒。补旧清理不动乙，旧材料仍可查。取消先留持单，收到停止与清理的宿主证据后才确认。宿主的清理凭据由测试替身提供。3 项新增，累计 36 项。 | `python3 -m unittest -v rewrite.test_cleanup` | 原生清理凭据、询单与取消的三次重试 |
| R8 询单及取消重试 | 真实隔离进程通信，宿主与时钟为夹具。两类请求分别累计；首次加三次重试仍无答复才入池并通知搁置。忙闲切换重新计时，继续请求保留原派发。3 项新增，累计 39 项。 | `python3 -m unittest -v rewrite.test_inquiries`；转派基线 259a196 | 真宿主停止、清理和原生回执仍待验 |
| 原单多题 W05/A08/A09 | 真实隔离 HTTP 与 SQLite，发问、作答方为测试驱动。每题由 Worker 分配 GUID；丢收件回执后原调用查回原题。选择、文本、跳过各有精确校验，转派后仍向原提问人送答案，答题不能收回工单。累计 43 项。 | `python3 -m unittest -v rewrite.test_questions` | 原生工具注册、真人双端题卡；R8 重试提交 2a0a757 |
| T01–04、C03 和分级收件 | 两个临时 Trigger 身份、SQLite、HTTP、文件下载实际运行。空文件、小图和 SQLite 文件逐字节核对；大文件跨分块 Range 成功。丢分块回执后按偏移续传、丢消息回执后只入账一次，超长或缺附件整条拒收。Event/Notify/Info 用受控宿主验证。累计 48 项。 | `python3 -m unittest -v rewrite.test_trigger`，问答基线 d0fe4dd | 真实来源接入、真实 Hermes、Worker 成果附件与双端界面 |
| 独立启动与 HTTPS | macOS 两个真实进程、临时证书和身份。Trigger 可离线记件，独立 Router 重启后仍能查到同一信息与附件。错误身份返回 401，未知签发者拒连。传输使用回环网卡，尚未作为跨机器实测。累计 50 项。 | `python3 -m unittest -v rewrite.test_service`；命令 `python3 -m rewrite.service router --help`、`trigger --help`；附件基线 dfc1c31 | 独立 Worker/宿主链、远端测试设备 |
| Worker 本体和远端适配器 | 三个真实独立进程分别启动 Router、Worker、测试宿主适配器，以 HTTPS 和各自身份对接。重复平台私聊只有一条宿主输入；平台身份不能调用原生回报，普通私聊的回合不能代报，正式回报重试只通知一次。宿主和调用者仍是替身，测试在 macOS 回环网卡上执行。 | `python3 -m unittest -v rewrite.test_service.Service.test_three_process_worker_chat_and_assignment`，累计 51 项；TLS 基线 991ccc9 | 独立平台实机、真实宿主证据 |
| 同伴私聊 | 两个 Worker 通过 Router 互发与关联回复；回执丢失重试后目标只收一次。同伴输入不能代报正式成果，查询不建单。真实 HTTP，宿主输入为夹具。 | `python3 -m unittest -v rewrite.test_peers`（2 项）；三进程基线 0111cc1 | 真实工位对端、飞书镜像 |
| W04 成果附件和候选附件 | Worker 上传的文件可随正式报告授权给溪流。一份跨身份文件使整笔事务回滚；改正后成果、通知各一次。仅附文件的晚到候选只记材料。 | `python3 -m unittest -v rewrite.test_work_files`（2 项），同伴基线 c962875 | 用户界面下载和生产身份待验 |
| 全量功能 | 下列旧入口已确认存在；迁移和各平台回退尚待实测 | 入口表对应源文件及注册表 | 双端题卡、运维验收 |
| 飞书展示 A05/A06 | 专用适配器身份将指定会话的公开事件交给 Worker。隔离平台记录显示工具卡、增量更新原卡、独立图片、图后新卡和最终回复顺序正确。图片发送回执丢失后沿用原图片与消息键；超过平台去重时限保留待核。4 项测试使用平台替身。 | `python3 -m unittest -v rewrite.test_feishu_output`；[飞书发送消息与 UUID 有效期](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create?lang=en-US) | 实际平台消息 ID、各原生宿主事件接入另验 |
| 飞书私聊入站 A02–A04 与普通文件 | 实际飞书 Python SDK 1.7.5 在空白环境解析两次同一模拟回调，队列仅存一条。隔离 Worker 通过 HTTP 分块收文件；模拟宿主收到同一输入中的原文和原始图片字节。丢回执重试不重复输入。测试还覆盖纯图、Markdown 文件、非训练员和机器人消息。文本专用宿主明确报无法交图。累计 67 项全量回归通过。 | `python3 -m unittest -v rewrite.test_feishu_inbound`；带 SDK 的测试 Python 运行 `-m rewrite.feishu_sdk_check` | 飞书云端收发、原生识图及界面待验 |
| 旧 Router 离线迁移演练 | 实际旧 RouterStore 在临时目录产生归档、排队和附件。备份校验、稳定队列映射和零新写入回退均实跑。旧程序重开恢复库，原下一张单、归档理由与附件均吻合。有在途、摘要损坏或新库已写拒绝覆盖。 | [复跑与回退界线](router-rehearsal.md)，`python3 -m unittest -v rewrite.test_cutover`（4 项） | 整体多组件、带新增业务回迁及新端历史查询另验 |
| A07 业务消息留档 | 完工、提问、入池、提醒和取消确认与各自的通知同时入账，按原 ID 可查。历史投递缺接收时间时返回空。查询不生成新帧，不确认宿主收到。 | `python3 -m unittest -v rewrite.test_archive`（2 项），成果附件基线 488b996 | 平台与宿主可见性另验 |

## 宿主与来源复验

同伴收件的三种观测可由发件工位按原消息号查询。第一次查询只有 Router 收件时间，接收 Worker 记入收件箱并确认传输后可见 `worker_received`，此时宿主收件号仍为空。指定宿主交回独立凭据后，Router 单独保存来自接收 Worker 的确认号。丢失这次回执再重开接收 Worker，查回同一号，原生输入仍只有一条。同伴消息的另一端可查，第三个工位及管理端被拒。复跑 `rewrite.test_peers.Peers.test_sender_distinguishes_router_worker_and_host_even_after_receipt_loss`。宿主用夹具，数据库、权限及两段 HTTP 实跑。

同伴私聊现可转交附件。发送 Worker 到 Router 的首块回执丢失，接收 Worker 又在第二块断开；两个方向均从已确认的字节偏移续传。接收工位重开后只生成一个原生输入，文件摘要吻合，双方无持单。空文件经过零字节读取验证；混入无权访问的附件会拒绝整条消息，未向管理端授权附件。复跑 `python3 -m unittest -v rewrite.test_peers`。两个 Worker 接口、Router HTTP、分块存储与本机文件实际运行，接收宿主使用夹具。累计 111 项回归通过。

旧 Router 的真实 HTTP 建单暴露出迁移夹具遗漏的差异。旧端给每张普通单写入默认目标元数据，原转换器把它一律拦截。现已在临时端口从旧 HTTP 派单入口产生待派单及已归档单，旧建单重试只记一份；迁到新 Router，原键仍查回同单，修改正文被拒，已归档单用旧键也无法复活。Worker 取得同一新单号，测试宿主读到目标约束、完整正文与原上下文。早期快照仍可读。复跑 `python3 -m unittest -v rewrite.test_cutover`，6 项通过；两端 Router、HTTP 与数据库实跑，宿主为夹具。原生目标执行待验。

飞书入站异常已有独立回执。隔离 DSH 在写入用户消息后抛错，Worker 收到 503，重试从原会话查回同一条输入。分别模拟宿主回执与平台回复回执丢失，训练员原消息先得到一条待核提醒，查回后得到一条确认；超出去重窗口的回复保留待核，其他输入仍能收到反馈。SDK 1.7.5 以模拟返回值核过话题父消息和消息卡参数。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk rewrite.test_feishu_output` 与 `python3 -m rewrite.feishu_sdk_check`。全量 108 项；实际飞书平台与模型未参与。

DSH 的 A05/A06 已从 SDK 的 V4 会话日志取证。Worker 接收四条有序展示事件，飞书替身分别生成原生输入、过程、工具与回复卡。丢 Worker 展示回执后重建发布器，平台仍仅收四张。另一次展示依次收到图前文字、单独图片、图后文字；平台收到的图片和 SDK 实际读取的对象 SHA-256 相同。对非图片记录或错误记录摘要的取图均拒绝。中断回合只出过程输出，正式单即便已有最终聊天文本，仍保留原持单。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk`。仅覆盖已持久化的用户及模型公开内容和工具调用；模型生成、实时逐字流、真实飞书仍待验收。

DSH 图文输入已用随附 SDK 的 `LocalAttachmentStore` 实测。飞书入站夹具提供一张可解码的 1×1 PNG 和原留言，原生用户消息保留两块内容；SDK 独立读回图像对象，归一化后的 SHA-256 与宿主引用相符。另发的 368,000 字节 Markdown 文件由 SDK 按文件读取，摘要与上传原件一致。清空适配器凭据和原图缓存后，重取原图查回同一原生收件号，未再生成输入。损坏图或只接收文字的测试模型均无收件。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk rewrite.test_feishu_inbound`。DSH SDK 及本机附件库、进程和 HTTP 实跑；飞书事件、Agent 和模型路由是夹具，视觉理解尚未验收。全量回归 101 项。

迁移后已实测溪流按来源 UUID 和旧工单号查询历史。真实旧 RouterStore 生成的成果、归档原因及时间通过新 Router 与 Hermes 管理工具查回，排队单能查到新单号。历史归档单未重派，查询前后试运行库摘要完全相同。复跑 `python3 -m unittest -v rewrite.test_cutover` 共 5 项，全量回归 93 项。人工整理旧格式记录以及试运行后的新增业务回迁另验。

Hermes 本地专用会话 H01/H02 已取得两条独立原生收件号。Event 进入指定会话后，将插件两份收件记录清空，仍由 Hermes 台账找回同号，宿主只执行了一次。测试回合释放后 10.94 秒收到 Notify，Info 没有新增输入。来源附件通过引用和原消息授权取回并验 SHA-256。执行记录在 [Hermes 原生台账](evidence/hermes-local-input.json)，源码版本 464bd0dd659f336bd0bf468bf02cad8aac211136。模型处理函数由夹具代替。普通回归另测待核 Event 与 Notify 队首，以及两次观察之间出现新回合时重新计时。此项覆盖独立 LOCAL 会话，飞书云端和日用网关未验。

A07 附件与 H02 文件交接在隔离 Router 实跑。用 `router_info` 同时指定消息号和附件号，可取得校验过的本机文件。测试在 640,000 字节原件的第二段断开，重建插件后从第 262,144 字节续取；原件和空文件各自的 SHA-256 均与宿主夹具的独立读取相符。两条 Info 即使共用缓存，交换消息号也无法取另一条的附件。复跑 `python3 -m unittest -v rewrite.test_manager_files`，全量 90 项通过。宿主收件仍为夹具，真实 Hermes 输入回执另验。

版本包的构建、安装、核验和升级入口见 [隔离安装](README.md#隔离安装与升级)。从 b31cee5 升到 5de36ad，再退回前版，三个实际临时 Router 进程先后启用同一张待派单，仅原单一张。原始工单号 `2bd9d658-d841-4a7e-9f28-e52086a56c64`；两版源码包 SHA-256 分别为 `8ca669d7142031d022a5a37ae124aaa8f739113c70c40fab2eaf120c6c954d25`、`318fa45ec72c8ab1fa1edde64f4bd56bc4d10c17d8f5e9be33aff43abb316a37`。复验命令在 [回退演练](router-rehearsal.md)。七库试运行出现新业务写入时，整组回退另外实测拒绝。

五组件离线备份的复跑入口为 `python3 -m unittest -v rewrite.test_state_bundle`，步骤见 [离线恢复](router-rehearsal.md)。七库从同一停写点恢复，原持单、未交的私聊和通知、待上传附件都实跑续接。一次已有写锁、错误角色或清单摘要不匹配会阻止发布恢复目录。真实云端状态及跨机器停写窗口另验。上一关 DSH 提交 b31cee5，完整回归 84 项。

DSH 的适配器可在独立宿主进程加载 `integrations/dsh_host/index.mjs`，只对显式指定的会话注册 `worker_ask` 和 `worker_report`。宿主插件与同机适配器经用户私有目录里的 Unix 套接字通信；适配器通过 HTTPS 与 Worker 通信。接入参数是会话 ID 和绝对目录，启动器说明见 `python3 -m rewrite.service dsh-adapter --help`。宿主侧不持有 Router 凭据。

复跑 `python3 -m unittest -v rewrite.test_dsh_sdk`。本机 DSH 随附 SDK 0.2.2-rc.5 在全新临时用户目录里加载插件，由 SDK 自己提供工具表、调用分发、会话存储和 V4 日志查回。清掉适配器收件凭据后，SDK 日志恢复了原凭据，输入仍为一条。问答和完工分别到达隔离 Router，同一模型调用号的重放只形成一次成果通知；私聊、同回合有手动插话均拒报。Agent 输入、模型输出及工具调用是受控测试驱动生成，未使用测试账号或真人会话。DSH 真实模型、附图、原生清理和平台展示仍未验。Desktop 的两项阻点独立保留。

来源采集复验入口为 `python3 -m unittest -v rewrite.test_feeds`。Python 自带的 IMAP 客户端实际通过 TLS 连接临时邮箱服务器，使用 EXAMINE 和 BODY.PEEK 只读取件。真实 SSE 连接从测试游戏桥取回聊天；本人消息过滤，紧急聊天交 Event，普通聊天、邮件入 Info。每条消息留档与推进读取位置同事务，写入中断重开时保留旧位置，重见同序号按内容核对。两项通信运行于本机回环，邮箱、游戏数据和服务端均为测试替身。旧订阅、来源限流及游戏桥无启动标识时的同序号重启识别另验。

Worker 篇 A07/A08 双端题卡的复跑入口为 `python3 -m unittest -v rewrite.test_question_cards`。实际 HTTP 和 SQLite 连接了 Worker 与独立适配器；飞书和宿主题窗由夹具代替。训练员选择、自填、跳过后，原卡均等原题关闭凭据到达才更新。宿主丢回执时只查原题，重试未再提交答案。问溪流的卡保留完整题面和选项，训练员的回调被拒；溪流回答后更新同一卡。飞书 SDK 1.7.5 收两次模拟按钮事件，答案队列只写一条。累计 78 项回归通过，真实人员点击、云端卡片及客户端题窗仍待专用工位验证。

Worker 篇 A01、A09–A11 的隔离复跑入口为 `python3 -m unittest -v rewrite.test_link_health`。真实回环 HTTP 断线期间，测试宿主的进展仍进入本体，原派发在本地保留。受控时钟满 20 分钟后重连，Router 已将原单标为受阻，Worker 据此解除持单。单断适配器时本体仍可达，Router 将宿主忙闲标为未知，只报一次警。工单卡在宿主收件后显示派发、更新和取消正文。适配器链路诊断接口只允许当前 Worker 会话报状态。共 74 项离线回归，原生与飞书界面另验。

### 溪流侧管理工具的独立验证

Hermes 源码版本 `a2de7418a3405307f8b3abce01a66d5d71351edc`。从 Git 跟踪的源码建立临时副本，用本机已有的独立测试 Python 3.14 环境、全新配置和测试身份加载插件。Hermes 自己登记全部 8 个管理工具，实际调用经过其工具分发和中间件，再通过 HTTP 建单。同原生调用号重试返回同单，其他会话收到 403。原始输出在 `evidence/hermes-manager-registration.json`。调用由测试驱动发起，未启动模型、网关或飞书。A07 业务存档提交 61d7721；本轮全量回归 59 项通过。

复跑用 `python3 -m rewrite.hermes_check --hermes-source <Hermes源码仓> --hermes-python <带完整依赖的独立测试Python>`。缺少运行依赖的 Python 3.12 副本在导入 ruamel.yaml 时失败。普通离线回归包括 `rewrite.test_hermes_tools` 和 `rewrite.test_archive`。

### Codex 两种测试入口

在新建的临时工作目录配置项目级 stdio MCP，再在相同目录打开独立测试会话。原生测试提示只要求枚举本回合的工具。回读得到空数组，本机同目录的 `codex mcp get mowulink_w04_test` 返回未找到服务，私有运行目录也没有握手或调用记录。仅凭以上现象无法区分该版本的项目加载策略和其他宿主配置原因。测试后已删除专用项目配置。宿主对项目配置有信任要求，Desktop 的共享配置会影响其他使用者。[Codex MCP 官方说明](https://developers.openai.com/codex/mcp)

`report_scope stage|clean`（`python3 -m rewrite.report_scope`）仅用于专用项目。测试启动器须先在仅属当前用户的临时目录建立监听。模型工具参数只有业务字段。入口读取 tools/call 的宿主线程、回合元数据，用适配器的已确认收件查回唯一输入，再到 Worker 逐项核对工单和派发。查不到、同回合有多个已知输入、会话不符或私聊均拒报。开发用 Unix 通道信任同一系统用户，不能证明同用户的任意本地进程由 Desktop 启动；生产认证和跨机传输另验。

另以该机 Desktop 随附的 `codex app-server --stdio` 建立一次独立原生协议连接，临时用户目录和配置均为空。初始化成功，协议提供线程级 `dynamicTools`，但 `account/read` 返回无账号、需要认证。该进程未发起模型回合。此路径可用独立测试帐号继续验原生模型，Desktop 项目加载仍需单验。

上面两次探测分别记录于 `evidence/desktop-w04-probe.json` 和 `evidence/app-server-empty-profile.json`。溪流已核实当前没有可用的已授权测试工位。真实 W04 继续列为未验；其他项目继续推进。

## 旧入口与替代条件

DSH 的 `/queue 正文` 已接入固定会话。飞书命令回执证明 Worker 已持久排队；宿主收件由独立输入号和回合号证明。忙时两条消息都留在 Worker，重建 Worker 后按原顺序进入两个独立回合。首条原生回执在传回途中丢失，复查原号后输入总数仍为三条（原回合一条和排队两条）。适配器拒绝将原消息交给另一会话。DSH SDK 在临时目录实际加载插件并落 V4 日志，Agent 驱动和飞书输入由夹具提供。旧宿主未声明下一回合投递能力时提示未接入。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk rewrite.test_worker_commands`。

旧 Codex 的实际命令入口是 `Plugins/Codex/desktop-manager.mjs` 的 `message` 和指令分发器。除帮助列出的主要命令，还处理 `/start /commands /clear /switch /list /stop`。Antigravity 的 `interface/feishu-commands.mjs` 明确限定固定会话，历史切换另列。仓内 `client/desktop/README.md` 和 `client/mobile/README.md` 仅记未来客户端，本仓这两个目录没有可执行消费者。

重做版的纯文本指令现由绑定的飞书训练员交给 Worker。隔离 HTTP 实测 `/help`、`/status`、固定会话查询和同会话选择。状态卡分别显示两条连接、宿主最近忙闲、本地工单号与派发号。帮助别名为 `/start` 和 `/commands`。此时 `/resume` 仅能查看当前会话，同会话编号及 ID 可确认；历史翻页、检索、换绑仍待接。未知指令给出「暂不开放」。尚无执行回读的命令不会送入普通对话。重试丢失的 Worker 回执只出现一张指令卡；旧版已交给宿主的同号消息可查回旧收据。四项新增回归 `python3 -m unittest -v rewrite.test_worker_commands`，全量 97 项。飞书卡片使用平台替身，真实云端另验。

旧 Router、Trigger 以此分支的 `server/` 和 `sources/` 为依据；宿主插件以独立 MowuLinkWorkerSkill 的 `f399148df99ed58568f4931aed414e3a83fa7a25` 为依据。已安装的 Hermes `trigger-bus` 与当前维修会话的 MCP 工具表亦已核过。源码声明只证明存在。每行的迁移条件在对应使用端实测通过之前，保留旧入口。

| 旧入口与使用者 | 新入口／归属 | 已有证据；迁移条件 |
| --- | --- | --- |
| 溪流、客户端使用 Router 的 /workers、/tasks、/tasks/{id}、/tasks/{id}/cancel；Worker 用 /register、/heartbeat、/stream、/result、/tasks/{id}/deliverable（server/router/server.py） | A01–06、C01–02、W01–04 | 建单收件和验收已有主线；须逐项验证改派、取消、旧清理、晚报与管理查询 |
| 工位点对点消息 /messages、消息 ack；飞书与 Worker 的聊天记录 /chat、订阅与 notice | Worker 私聊与 Router 投递；训练员私聊由 Worker 对接 | 新私聊夹具可隔离工单语义；跨机收发、关联、历史与订阅迁移未验 |
| 工位问答 /questions、/questions/responses，审批 /approvals，远端命令 /commands、/commands/responses，运维 /ops/services | W05、A08–09；宿主授权及运维能力单列 | 验收要覆盖原题、原请求发起人和权限；保留独立审批边界，禁止用问答代批准 |
| Trigger 来源 sources/email/client.py、sources/mc_chat、server/trigger/clients/mc_chat_client.py；/trigger、/sources、/stats、/events、/drops、/blocked、/subscribe、/ack、/renew、/unsubscribe | 来源实例持久队列、T01–04，订阅、健康和运维另测 | 两来源都有登记；按启动状态另核。须复现断传、重发、按实例查发件与接收 |
| Hermes trigger-bus 本机已安装；register 挂 pre_gateway_dispatch、pre_llm_call、transform_tool_result、post_llm_call；旧附件获取 | HermesRouterAdapter C01–03、H01–02、A07 | Notify 空闲 10 秒已有宿主夹具；Event、Info、文件和真实 Hermes 单列 |
| Codex/DSH 私聊入出站、图文、文件、图片回传、流式卡、题卡、按钮（Plugins/{Codex,DSH}/feishu*.mjs、question-*.mjs） | Worker 与平台适配器 A01–A11 测例 | 回归须有消息与卡 ID、原始附件，工具进展和最终输出区分；真人卡验收条件另记 |
| Codex 飞书 /resume（搜索、分页、选会话）、/status、/context、/compact、/new、/fork、/model、/effort、/goal（查询/暂停/继续）、/queue（feishu-commands.mjs 与 business-runtime.mjs） | 各宿主会话与能力接口 | 每个命令分别核实际执行和回读；选中模型、力度不能只核菜单文字。DSH 和 Antigravity 独立核 |
| 当前 Codex 工具 bridge_probe、bridge_inspect_native_contract、bridge_bind_manager、bridge_binding_status、bridge_verify_manager_binding、bridge_start_local_manager、bridge_verify_native_write_path、bridge_reload_business_modules | 宿主适配器、安装与恢复入口 | 目标绑定及真收件有部分证据；其余逐项核失效回退 |
| bridge_register_current_task、bridge_create_work_order、bridge_work_order_status、submit_deliverable | Worker 工单接管、管理工具与 W04 | 旧工具表实见；新工具尚缺真模型调用 |
| bridge_message、bridge_send_image、bridge_publish_question、bridge_list_workers、bridge_send_worker_message、bridge_worker_inbox | 平台输出、问答、同伴联络 | 需用测试身份核回执、去重、接收方及权限 |
| DSH 插件 index.js、dsh-session-catalog.mjs；Antigravity/interface/sidecar.mjs、deliverable-mcp.mjs | 可替换 AgentAdapter | 各宿主分别登记支持、缺口及实测；Codex 成绩不能转记 |
| 旧附件 /attachments、订阅 /subscriptions、运维命令、Codex/DSH 的安装更新、Windows 启动器与 macOS 恢复脚本 | T01–03/C03、注册鉴权、隔离安装与升级回退 | 两平台均须有已登记工具消费者及恢复证据；既有停用巡检保持停用，确认退役另记 |
| client/desktop、client/mobile、host/apply.sh 等兼容使用方 | 管理和消息接口的客户端 | 尚待逐个核实际调用和版本覆盖，不能凭目录名判定退役 |

源码侧全表仍在补齐，尤其是客户端独有入口、单个来源的使用状态、DSH/Antigravity 的命令差异、安装与权限场景。迁移、回退与生产切换均未验收。
