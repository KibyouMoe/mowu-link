# 现役工具替换进度

外层单 t-124f6357c5。程序契约版本 9db82a32。此表逐项记录可替换条件。「实测」限定到表中指定的宿主、平台和接口。只有全表核验、迁移回退演练和溪流独立复验齐全，才能提请生产切换。

## 检查点与证据

| 范围 | 当前结果 | 复验入口与原始证据 | 下一项 |
| --- | --- | --- | --- |
| 正式主线 17fccee | 真实临时 HTTP Router 与 Worker；双宿主及 Agent 为夹具，14 项 | `python3 -m rewrite.devloop`、`rewrite/test_mainline.py` | 扩充 A/W 接口 |
| Desktop 指定会话 f4f7ba7 | macOS 原生输入确认，原派发重投 0 条额外输入；原生回合结束仍持单 | `desktop-check.md` 中的实际会话、原生输入与命令；`rewrite/test_codex_desktop.py` 5 项 | 独立测试会话工具注册 |
| W04 工具入口 | macOS Desktop 当前隔离项目加载受阻。重做 MCP 子进程、私有套接字、Worker、Router 和 SQLite 联通；5 项新增，其中同回合多输入拒报。工具调用者和原生元数据为夹具 | `evidence/desktop-w04-probe.json`；`python3 -m unittest -v rewrite.test_project_report` | 取得可加载 MCP 的专用测试宿主，再取原生工具调用项和业务回执 |
| 全量功能 | 下列旧入口已确认存在；迁移和各平台回退尚待实测 | 入口表对应源文件及注册表 | 生命周期、附件、提问、运维验收 |

## Desktop W04 的复验边界

在新建的临时工作目录配置项目级 stdio MCP，再在相同目录打开独立测试会话。原生测试提示只要求枚举本回合的工具。回读得到空数组，本机同目录的 `codex mcp get mowulink_w04_test` 返回未找到服务，私有运行目录也没有握手或调用记录。仅凭以上现象无法区分该版本的项目加载策略和其他宿主配置原因。测试后已删除专用项目配置。宿主对项目配置有信任要求，Desktop 的共享配置会影响其他使用者。[Codex MCP 官方说明](https://developers.openai.com/codex/mcp)

`report_scope stage|clean`（`python3 -m rewrite.report_scope`）仅用于专用项目。测试启动器须先在仅属当前用户的临时目录建立监听。模型工具参数只有业务字段。入口读取 tools/call 的宿主线程、回合元数据，用适配器的已确认收件查回唯一输入，再到 Worker 逐项核对工单和派发。查不到、同回合有多个已知输入、会话不符或私聊均拒报。开发用 Unix 通道信任同一系统用户，不能证明同用户的任意本地进程由 Desktop 启动；生产认证和跨机传输另验。

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
