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
| A07 业务消息留档 | 完工、提问、入池、提醒和取消确认与各自的通知同时入账，按原 ID 可查。历史投递缺接收时间时返回空。查询不生成新帧，不确认宿主收到。 | `python3 -m unittest -v rewrite.test_archive`（2 项），成果附件基线 488b996 | 平台与宿主可见性另验 |

## Desktop W04 的复验边界

### 溪流侧管理工具的独立验证

Hermes 源码版本 `a2de7418a3405307f8b3abce01a66d5d71351edc`。从 Git 跟踪的源码建立临时副本，用本机已有的独立测试 Python 3.14 环境、全新配置和测试身份加载插件。Hermes 自己登记全部 8 个管理工具，实际调用经过其工具分发和中间件，再通过 HTTP 建单。同原生调用号重试返回同单，其他会话收到 403。原始输出在 `evidence/hermes-manager-registration.json`。调用由测试驱动发起，未启动模型、网关或飞书。A07 业务存档提交 61d7721；本轮全量回归 59 项通过。

复跑用 `python3 -m rewrite.hermes_check --hermes-source <Hermes源码仓> --hermes-python <带完整依赖的独立测试Python>`。缺少运行依赖的 Python 3.12 副本在导入 ruamel.yaml 时失败。普通离线回归包括 `rewrite.test_hermes_tools` 和 `rewrite.test_archive`。

### Codex 两种测试入口

在新建的临时工作目录配置项目级 stdio MCP，再在相同目录打开独立测试会话。原生测试提示只要求枚举本回合的工具。回读得到空数组，本机同目录的 `codex mcp get mowulink_w04_test` 返回未找到服务，私有运行目录也没有握手或调用记录。仅凭以上现象无法区分该版本的项目加载策略和其他宿主配置原因。测试后已删除专用项目配置。宿主对项目配置有信任要求，Desktop 的共享配置会影响其他使用者。[Codex MCP 官方说明](https://developers.openai.com/codex/mcp)

`report_scope stage|clean`（`python3 -m rewrite.report_scope`）仅用于专用项目。测试启动器须先在仅属当前用户的临时目录建立监听。模型工具参数只有业务字段。入口读取 tools/call 的宿主线程、回合元数据，用适配器的已确认收件查回唯一输入，再到 Worker 逐项核对工单和派发。查不到、同回合有多个已知输入、会话不符或私聊均拒报。开发用 Unix 通道信任同一系统用户，不能证明同用户的任意本地进程由 Desktop 启动；生产认证和跨机传输另验。

另以该机 Desktop 随附的 `codex app-server --stdio` 建立一次独立原生协议连接，临时用户目录和配置均为空。初始化成功，协议提供线程级 `dynamicTools`，但 `account/read` 返回无账号、需要认证。该进程未发起模型回合。此路径可用独立测试帐号继续验原生模型，Desktop 项目加载仍需单验。

上面两次探测分别记录于 `evidence/desktop-w04-probe.json` 和 `evidence/app-server-empty-profile.json`。溪流已核实当前没有可用的已授权测试工位。真实 W04 继续列为未验；其他项目继续推进。

## 旧入口与替代条件

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
