# 现役工具替换进度

外层单 t-124f6357c5。程序契约版本 9db82a32。此表逐项记录可替换条件。「实测」限定到表中指定的宿主、平台和接口。只有全表核验、迁移回退演练和溪流独立复验齐全，才能提请生产切换。

## 检查点与证据

W03 后断在首次原生收件回程时，Worker 已保留原派发与宿主待核状态。隔离测试切到同名空账本，重复轮询两次无新输入；恢复旧账本后查原收件号，工单仍由原 Worker 持有。TLS 查回端口核对收件号、会话及原派发内容。实际 DSH 在宿主尚未写入前明确拒绝忙时派发，原私聊结束后才收到正式单；若先取消，只收到取消。复跑 `python3 -m unittest -v rewrite.test_mainline rewrite.test_service rewrite.test_dsh_native_loop.NativeLoop.test_native_race_after_w03_never_prequeues_a_formal_task`。

`/queue` 实测在本地 HTTP 发出前已将投递标为待核。原宿主收件后丢回执，换到同会话号的空账本后两次复核均无写入，接回原账本查到原输入号。仅各有一条待核与恢复提示。真实 DSH SDK 的排队测试同时验证两条消息依原次序进入独立回合。复跑 `python3 -m unittest -v rewrite.test_worker_commands rewrite.test_dsh_sdk`。平台及模型使用夹具。

Worker 后台遇到临时 SQLite 写入错误会继续轮询。隔离测试注入一次锁错误，原线程恢复后接管待派工单，宿主只有一条输入。另用实际 HTTP 返回非 JSON、缺会话、数组和损坏的 SSE，核对宿主状态标为待核，Router 连接保持，恢复后可继续接单。Router 暂断时同伴状态保留本地 Router 收件时间，对端两层留空。复跑 `python3 -m unittest -v rewrite.test_link_health rewrite.test_peers`。Router 与 Worker 运行于本机隔离环境，故障与宿主用夹具。

飞书入站及出站循环遇到暂时数据库锁、无效上游响应仍会继续运行。隔离回归先注入入箱读取锁和显示响应错误，再让实际 Worker 写入但返回缺回执的数据。平台保留待核，退避后查回原凭据，宿主只有一次输入，题卡队列仍继续。复跑 `python3 -m unittest -v rewrite.test_feishu_inbound.PlatformInput.test_platform_queue_recovers_after_locked_inbox_and_a_malformed_worker_receipt`。故障为注入，Worker 传输实际运行，未连飞书云端。

DSH 原生 `worker_peer_inbox` 已验证从自己的收件箱补查原消息号和回复关系。隔离回归核了待回执游标、确认后移出待办、原样重复读取、历史不含正文。原生 DSH 在收件回合先查历史再发回复，只产生一条回复。复跑 `python3 -m unittest -v rewrite.test_peers.Peers.test_inbox_cursor_and_history_are_read_only_and_keep_reply_correlation rewrite.test_dsh_native_loop.NativeLoop.test_dsh_peer_image_and_native_reply_travel_on_the_same_private_chain`。

飞书官方 Python SDK 1.7.5 通过实际回环 WebSocket 验证 TCP 中断后自愈。测试直接断开连接，无 WebSocket 关闭帧。SDK 第二次取地址并重连，三次事件回包为 200，其中一条是断线前事件重发。共享入箱只有两条，另一个进程重新打开入箱并交给实际 Worker，宿主输入也只有两条。连续 6 次通过。复跑带 SDK 的 `python3 -m rewrite.feishu_reconnect_check`。[传输记录](evidence/feishu-sdk-reconnect.json)。未连真实飞书，原生宿主用夹具。

两套独立原生 DSH 进程已在同机跑通同伴图文及关联回信。双方 AgentLoop 都通过宿主工具执行器调用 `worker_send_peer`，Router 两条原消息分别有接收方原生凭据。输入原图摘要一致，回复带原消息号，两端重读都无重复输入，Router 无工单。连续 10 次通过。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_two_real_dsh_sessions_roundtrip_a_linked_peer_message`。[双宿主原生记录](evidence/dsh-native-peer-two-hosts.json)。两端模型由本地脚本驱动；不涉及真实飞书或跨物理机器。

Linux arm64 回归使用提交 `318e905a241229c2de276bd5917293fc969507bc`，通过 125 项，33 项因依赖 macOS DSH 跳过，核对 125 个发行文件。macOS Worker 经独立 TLS 接入 Linux Router 和测试宿主，只见原输入和通知各一条。该跨系统试验在一台物理机上运行，模型和两宿主用夹具。

同伴私聊的接收绑定现在也随投递保存。隔离实测在原生收件后丢回包，依次更换为另一会话、同会话但空账本的适配器，再接回原账本。前两步保留待核，原会话始终只有一条输入，另一会话为零。接回时查原凭据并回告 Router。远端 TLS 查证路径拒绝错误接收方。复跑 `python3 -m unittest -v rewrite.test_peers.Peers.test_uncertain_peer_receipt_never_writes_to_another_session_or_a_fresh_ledger rewrite.test_service.Service.test_pinned_worker_rejects_same_authenticated_adapter_when_session_differs`。

DSH 原生 `worker_list_peers` 已验证可读到登记的对端，再用原生工具回复对方。独立 Router 验了分页、在线筛选、未登记身份拒绝，以及查询结果无工单或持单字段。管理查询权限照旧。复跑 `python3 -m unittest -v rewrite.test_peers.Peers.test_only_registered_peers_see_worker_ids_without_task_ownership rewrite.test_dsh_native_loop.NativeLoop.test_dsh_peer_image_and_native_reply_travel_on_the_same_private_chain`。[原生目录与私聊记录](evidence/dsh-native-peer.json)。模型为本地驱动。

真实 DSH 收到 Router 的搁置通知后，通过原生 `worker_submit_material` 留下一份文字候选。同一工单的迟到 W04 仍回 409，正式报告为空，交材料时持权留在 Router。转给另一测试工位后，原调用只取原材料号，新调用拒绝，第二派发仍由新工位持有，待清理仍指向第一派发。连续 12 次通过。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_shelved_dsh_assignment_submits_candidate_without_changing_ownership rewrite.test_pool rewrite.test_work_files`。[原生记录](evidence/dsh-native-material.json)。第二工位宿主与模型为夹具。附件候选由独立协议回归覆盖。

DSH 原生 `worker_peer_status` 回读 Router 收件、Worker 入箱及原生回执。真 DSH 工具实测，原生回包丢失时先见前两层，第三层为空；后台续办后才有原生确认。另以系统级夹具从发送前断线复跑，工具返回原消息号和 pending，对端读不到该未入账记录。重复查询不补发。复跑 `python3 -m unittest -v rewrite.test_peers.Peers.test_agent_can_read_pending_outgoing_message_without_claiming_acceptance rewrite.test_dsh_native_loop.NativeLoop.test_dsh_peer_image_and_native_reply_travel_on_the_same_private_chain`。

发件 Worker 已能自动恢复待确认的同伴私聊。实测 Router 提交前断线、提交后丢回包两支，重开原 Worker 后不再调用发送工具，待发送队列分别用原键补交或查回，对端各只收一条。未登记工位的拒绝保存在本地，后台不重复发送。复跑 `python3 -m unittest -v rewrite.test_peers.Peers.test_outgoing_peer_send_recovers_both_sides_of_router_accept_without_a_new_call`。Router、Worker 和 HTTP 实跑，宿主与时钟为夹具。

实际 DSH AgentLoop 收到同伴的一条带原图私聊，经原生工具执行器调用 `worker_send_peer` 回复原消息。隔离 Router 可分别观察三层收件。原生回执丢失时按原投递查回，同一工具回调重投返回同号，换正文拒绝，发送端只收一次回复。原图摘要吻合，第三工位读不到消息，没建工单。连续 20 次通过。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_peer_image_and_native_reply_travel_on_the_same_private_chain`。[实测记录](evidence/dsh-native-peer.json)。接收端是实际 DSH，模型和另一工位宿主是夹具。

新版派发在 W03 前固定原生会话。隔离 Router 接管成功后立即换到另一个已认证的适配器，旧单保持 working，另一会话没有收到它；单独给另一会话构造同号输入后，那里发出的 W04 被拒。重开原绑定后，原派发只有一次原生输入，正式 W04 才释放持单。复跑 `python3 -m unittest -v rewrite.test_mainline.Mainline.test_assignment_keeps_original_native_session_across_replacement`。Router、Worker 及传输实际运行，宿主为夹具。

首次确认宿主会话前断链时，普通私聊先留在 Worker 入箱。隔离测试在探测过程能读回已提交的消息，此时没有原生输入。重开 Worker 后同号继续首次投递，回执可重查，原生账本一条。复跑 `python3 -m unittest -v rewrite.test_mainline.Mainline.test_private_chat_pending_before_adapter_probe_resumes_once`。宿主为夹具，此处只验证入箱到首次写入的边界。

已加独立工位的只读运维检查。三进程 TLS 中，测试平台身份读状态被拒，专用运维身份读到预期和确认的同一会话及当前持单。两个真实回环断链试验分别核 Router 重连倒计时及宿主忙闲未知，另一链保持可观察。复跑 `python3 -m unittest -v rewrite.test_link_health rewrite.test_service`。原生宿主为夹具。

旧飞书私聊增加离线专用导出。用实际旧 RouterStore 写两工位记录，按一工位分页、筛选，并逐项比对旧查询的原 ID、方向、消息号、原会话、时间和文本。另一工位记录未导出，原库摘要未变，坏快照拒绝，不触发新 Router 或平台。复跑 `python3 -m unittest -v rewrite.test_cutover.Cutover.test_export_legacy_chat_keeps_original_fields_and_private_scope`。未取生产私聊日志；旧在线旁听的现有使用方另核。

用两版发布包实跑 Worker 与适配器逐个升级、回退。旧包、先升适配器、升完整套、旧包回退返回同一原生回执；升级版 Worker 已保存从原账本查到的会话和回执。各阶段原生账本只记一条，三份原快照摘要未变。[复跑命令](router-rehearsal.md#worker-和适配器逐个升级)与[原始输出](evidence/worker-pair-release-trial.json)。独立进程和回环 TLS 实跑，宿主与平台为夹具。

DSH 真正关闭原生 Agent 后，旧适配器的忙闲、输出游标和目录检查均报 503。原生注册表没有新增实例，解析器没有再调。宿主明确用同会话 ID 恢复出另一个 Agent，旧适配器写入仍被拒，新实例为零输入。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_closed_native_binding_never_resumes_or_sends_to_a_new_agent`。实际安装包的 AgentRegistry、持久会话与私有 Unix 套接字，测试控制器调用原生恢复。没有触发真实模型、飞书或生产会话。

普通私聊经 Worker 重启或更换适配器时，同飞书消息号已确认为原会话则返回原回执；首次投递状态未知时只查回。隔离实测更换为另一会话，三条旧消息（含无绑定的历史记录）都未写入新会话，只有新消息进入。即使原会话的适配器账本空了也没有二次写入；恢复原账本后查到两条原回执。复跑 `python3 -m unittest -v rewrite.test_mainline.Mainline.test_chat_replay_stays_with_original_native_session_after_restart rewrite.test_service.Service.test_pinned_worker_rejects_same_authenticated_adapter_when_session_differs`。SQLite、HTTP/TLS 与重启接续实测，宿主为夹具。主动换绑仍待验。

Worker 支持用 `--adapter-session` 固定 HTTPS 目标会话。专用请求路径会在服务端逐次核会话；适配器地址及认证相同但原生会话换了，忙闲和发送均回 409，错误会话零输入。指定正确测试会话才取得本机收件。复跑 `python3 -m unittest -v rewrite.test_service`。这里验证的是进程、TLS 与绑定拒绝，宿主为夹具。

DSH 在 W03 的回包之后、原生收件之前突然变忙时，会在入原生队列前明确拒收。适配器只对明确没发起写入的状态允许后续重试。真宿主复跑两支：自然空闲后原派发只收一次；此间先收到 Router 取消，宿主只收取消指令，普通对话照常完成，原派发执行从未开始。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_race_after_w03_never_prequeues_a_formal_task`。[原生事实](evidence/dsh-native-admission-race.json)。实际 DSH 与隔离 Router，本地模型。

[验收对照](acceptance-matrix.md) 汇总旧入口、Worker 篇 A01–A11、原生宿主证据与切换前剩余条件。

DSH 图文与纯图已通过真实 AgentLoop 的输入边界。分别给飞书夹具一条带原始空格和换行的图文及一条纯图，原生模型传输各读到同一原图的完整字节和摘要。两次重送共两条原生输入，飞书夹具各一张最终回复。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_actual_dsh_loop_gives_the_same_feishu_caption_and_original_image_to_its_provider`。[原生图文证据](evidence/dsh-native-media.json)。模型是读取实际字节的本地夹具，图像语义识别和飞书云端待验。

正式工单的 DSH 长目标已在隔离环境走通。原生工具创建目标，宿主跑一轮原生续办并标记完成；此时 Router 仍为 working，Worker 仍持原派发。Router 空闲后发原单询问，模型在该原生回合自行调用 `worker_report`。W04 后才释放，Router 待验收。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_goal_finishing_releases_formal_work_only_after_its_router_probe_w04`。[长任务证据](evidence/dsh-native-formal-goal.json)。模型为本地驱动。自动续办轮本身的 W04 来源仍缺独立授权链，本路径从 Router 的正式询问取得原派发。

旧邮件和游戏游标已有离线导入。邮件从原 UID 10 接续，只收 11 与 12；错误 UIDVALIDITY 保留 10，没进新档。游戏核对原地址，序号落后时不打开流；恢复后从 15 接续，只收 16 与 17。重导入后位置不退。复跑 `python3 -m unittest -v rewrite.test_feeds`，邮件协议夹具、真实临时 SSE 传输。[交接规程](router-rehearsal.md#旧采集来源交接)。旧游戏桥没有启动标识，生产游标与旧队列在授权窗口对账。

旧 Trigger 0.7.0 的归档已通过隔离保全与回退测试。用原版 TriggerStore 建 5 条不同处置的事件及订阅，确认源文件摘要未变、恢复档案逐字节相同、真实旧程序仍只查到原来两条未读记录。活跃日志和被篡改的成员会阻止恢复。Mac mini 只读盘点确认仍有 1,492 条记录及 1 个 active 订阅，[聚合证据](evidence/legacy-trigger-status.json)。未复制生产原文，未启用订阅。复跑 `python3 -m unittest -v rewrite.test_trigger_archive`，操作见[旧 Trigger 归档](router-rehearsal.md#旧-trigger-历史和订阅账本)。

工位收到原单更新、取消、转接和搁置即保存飞书提示。原生收件有独立凭据，控制提示也不会当作已经停止或清理。隔离实测，更新输入已进宿主但回包丢失、取消还没进宿主时，飞书待发队列均已有对应正文。恢复后原生各一条，飞书无重复，Router 仍待取消清理。复跑 `python3 -m unittest -v rewrite.test_link_health.LinkHealth.test_task_cards_use_worker_acceptance_independently_of_host_receipts`。真实工单与 Worker，宿主和飞书为夹具。

DSH 的正式题已跑通原生 `worker_ask`、Router 回题和原派发续办。答复回包故意丢失，原生会话只有一条答复输入；`continue_requested` 保留同一个派发和持单。溪流更新原单后，下一原生回合自行用 W04 交成果，Router 待验收，工位释放。飞书展示中的两条题卡事件均归溪流，训练员无作答入口。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_formal_question_returns_to_its_assignment_and_keeps_ownership_until_completion`。[正式题答复与回执](evidence/dsh-native-work-question.json)。真实 DSH 工具执行器和隔离 Router，模型、经理输入为测试驱动。

DSH `/stop` 已走真实 AgentLoop。普通回合留下原生中止记录，原生排队内容在下一次输入后照常入场。丢回包时显示待核，同号重投不影响之后的回合。实际隔离 Router 下发正式单后，飞书命令和宿主端私聊停止入口都拒绝停止工单，原 Router 取消路径仍可中止并保留清理待核。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_stop_targets_only_current_private_turn_and_never_replays_a_lost_result`。[原生停止记录](evidence/dsh-native-stop.json)。宿主真实，模型为本地夹具。

DSH `/resume` 已通过宿主原生查询读取当前会话和显式获准的历史目录。隔离测试读回 12 个条目，其中 11 个是已关闭的会话。验证两页、按标题或 ID 搜索、缺失记录提示及另一 Worker 收 403。未配置的会话连标题都没有进入适配器回应。浏览前后仅原会话处于活动，无原生输入、无新工单。换绑仍待接。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_resume_browses_only_configured_native_cold_sessions_without_activating_them`。[原生目录证据](evidence/dsh-native-catalog.json)。

DSH 原生计时题过前台等待期后，受支持的单题可接入训练员飞书题卡。原生工具在隔离 DSH AgentLoop 里自行运行并记录待回答状态。测试平台分别提交选择、自定义与跳过，验证原回合和原生题号、原卡更新、非绑定用户和非原卡拒绝。选择回包丢失后只从原生会话查回，三题各一次原生答复，多选保持在 DSH。另测开卡回包丢失，原生入口先选第一项，迟到的飞书第二项被拒，原卡按实际入场的答复更新。Router 无工单。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_native_timed_question_closes_the_original_feishu_test_card`。[原生记录](evidence/dsh-native-questions.json)。真实 DSH 工具及回合，模型为本地夹具，飞书为测试平台；原生前台阻塞题和真人飞书待验。

DSH `/compact` 已从绑定的 Worker 指令入口调用安装包的原生压缩服务。隔离测试丢弃首次提交回包，压缩期间和 Worker 服务重新初始化后重查原号，只观察到一次摘要请求、一组原生开始/摘要/替换/结束记录和一条压缩结果卡。DSH 在维护期间仍返回 idle，适配器会将自己发起的在途压缩报为忙碌。无可压缩历史时显示实情，缺证超时为待核。Router 无工单。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_compact_reconciles_a_lost_start_reply_against_the_native_session rewrite.test_worker_commands`。[原生压缩证据](evidence/dsh-native-compact.json)。真实 DSH 压缩引擎，模型为本地夹具。

DSH `/goal 目标正文` 在绑定会话通过原生服务建立目标，并使用宿主默认轮次。实测保留换行、同号重送原回执、已有未完成目标时拒绝替换。轮次驱动的验收改为由此入口建立目标后再暂停和恢复，Router 始终无工单。证据覆盖真实 DSH 循环与本地测试模型。[目标回执与两轮记录](evidence/dsh-native-goal.json)。

DSH 原生目标续跑已通过隔离测试。安装包自带的轮次驱动实际入场第 1 轮。用 Worker `/goal pause` 后第 1 轮中止，`/goal resume` 后同一目标的新版本入场第 2 轮并完成该回合。两条旧指令重送只取原回执。Router 始终无工单、无通知，Worker 无持单。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_goal_rounds_pause_and_resume_without_becoming_router_orders`，macOS 全套 133 项通过。模型传输为本地夹具。

DSH 的飞书 `/goal` 查询、暂停和继续接到原生目标服务。隔离回归查到同一目标的版本变化，暂停和继续各一条原生记录。丢失继续回执时保留待核，重送旧消息没有重复变更。同一会话未出现普通模型输入，Router 未建单。未加载目标服务的会话提示暂未接入。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_goal_query_pause_and_resume_use_the_bound_native_goal`，macOS 全套 132 项通过。本项核验目标服务与持久状态，自动续跑另验。

提交 `ab112dc8a6ee60e89b0af5bba184d9526750bbf6` 的 Linux arm64 复验通过 111 项，20 项 macOS DSH 测试跳过。跨 TLS 测试复核 105 个发行文件，原生宿主夹具只记一条输入、Router 只新增一条成果通知，认证反例通过。

Antigravity 桌面兼容层新增接口对照。本机 App 为 2.32.1。Google 文档提供工作区钩子、原生会话及转录路径和 MCP 工具入口。MCP App 的消息能力从 2.34.0 起，当前仍缺按指定会话后台唤醒和取消的实测。先留隔离验收入口，不以日志或资料推断飞书全链已通。[Antigravity 兼容层核对](antigravity-compatibility.md)。

Hermes 插件已能向单独登记的飞书会话投递收件提示。模拟 Notify 在 Hermes 忙时成功入箱，正文等空闲 10 秒，飞书只收到来源与等级。丢失平台回执后，重启续用同一消息号。超出安全重试窗口的提示保留待核，更换绑定也不会转发旧提示，后续 Event 照常进入宿主。复跑 `python3 -m unittest -v rewrite.test_manager_notices` 和带 SDK 的 `python3 -m rewrite.feishu_sdk_check`，macOS 全套 131 项通过。Router 与 Trigger 走实际 HTTP，通知测试的 Hermes 宿主及飞书返回为夹具。另跑真实 Hermes 隔离 LOCAL 输入，等待 11.5 秒后交 Notify，Event 续跑查回同一原生收件。正式飞书会话另验。

派单飞书卡现在与本体 W03 接管一起入库，卡片明确写「已接单，正在交给宿主」。此前宿主回执丢失会留下持单、漏掉派单卡。隔离回归主动抛弃原生回执，本体仍产生一张工单卡。查回原回执后只有一次宿主输入和一张派单卡。复跑 `python3 -m unittest -v rewrite.test_mainline rewrite.test_link_health rewrite.test_dsh_native_loop`，macOS 全套 129 项通过。原生控制指令仍各自按实际宿主回执显示。

DSH 的真实原生工具执行器已通过隔离 Worker 向 Router 报完工。复跑时发现首调用可能早于适配器写宿主收件凭据。修正后，Worker 只查询已存在的投递意图和指定原生输入，并核对原回合再放行。隔离测试主动丢弃原生投递回执，两个原生 `worker_report` 调用仍取同一业务回执。发出 W04 时 Worker 持原单，收到回执后清持单，Router 只发一条通知并停在待验收。回读宿主只存在一条工单输入。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop`，同场景连续 30 次通过，macOS 全套 128 项通过。实测使用实际 DSH 运行循环和工具执行器、临时 Router/Worker 及本地测试模型。真实账户和 Codex 原生 W04 另验。[原生与业务回执](evidence/dsh-native-w04.json)。

DSH 隔离环境已使用安装包中的真实 AgentLoop、原生工具执行器和 V4 会话存储。回合 1 在模型流中止，已排队的普通聊天和取消指令分别进入回合 2、3。控制投递重试仍返回回合 3 的原凭据。原生模型请求及后续两条请求均带已选 `high`。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop`，macOS 全套 127 项通过。本项采用进程内测试模型及直接适配器调用。工作环境清理仍需独立凭据。[原生回合记录](evidence/dsh-native-loop.json)。

DSH 原生停止请求限定到已确认过的工单回合。适配器将原派发的输入号和回合号交给插件。在原生会话仍处于这一回合、只有该工单输入且会话位置一致时，插件发出保留原收件箱的停止请求。普通私聊回合保持运行。隔离回归中丢弃取消指令的本地回执后，插件从原生日志查回同一收件，停止调用仍只有一次。相关测试用实际 DSH SDK 读写原生日志，执行停止的 Agent 与模型为夹具。停止请求、取消指令送达及原工作区清理确认分别核验。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk`，macOS 全套 126 项通过。

DSH 取消的待核分支已复跑。隔离 SDK 先记原回合中止，再向同一会话发送原派发的取消请求。两次试报均因缺少工作环境清理凭据返回 `native_cleanup_unconfirmed`，Router 保留原派发与清理记录，Worker 保留持单并只发一张待核卡。另用通用宿主夹具提供可核凭据，验证 Router 回执后才发确认卡。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk.DshSdk.test_cancel_stops_reporting_at_missing_native_cleanup_even_after_an_aborted_turn rewrite.test_cleanup`，macOS 全套 124 项通过。DSH 停止及工作区清理的正向实机验收仍待补。

DSH 的目录翻页和数字选择已接入。菜单产生后要等飞书返回消息号，Worker 才接受编号。模拟飞书已收、Worker 尚未记菜单回执时，旧编号不会误选，重投原卡仍只有一个平台消息号。力度菜单打开后手动换到另一模型，即使两模型都支持同一编号和强度，旧菜单也被宿主拒绝。重新查询后可换到 high。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk.DshSdk.test_numbered_model_choices_use_the_last_published_page_and_effort_checks_its_route`，macOS 全套 123 项通过。实际 DSH SDK 和 HTTP 已运行，飞书和目录为夹具。

回退回放同时验证了真实管理工具生成的 `hermes-…` 建单请求号。旧 Router 只接收 UUID，演练按来源生成稳定的旧请求号，并在计划中保留原号。两次生成计划一致，经旧 HTTP 重送仅入队一次。前一版 `8ecfd7ce9418862cb98eea0cb1df69d7b15af498` 在 Linux arm64 复核了 98 个发行文件，108 项通过，14 项 macOS DSH 测试跳过。跨 TLS 复跑仍只有一条夹具宿主输入和一条成果通知。

仅增加未派工单的回退分支已完成隔离回放。用真实新 Router 建两单，审核两条均能用旧目标模式，在全新目录恢复旧库，再经真实旧 Router HTTP 以原请求号回放。两条首次均为 202，重复取到原旧单号，改正文为 409，原队首保持，新增两单标题、正文和顺序一致。新试运行库与原快照保留，macOS 全套 122 项通过。已派发、已产生成果或其他新写入仍需对账。[复跑入口与审核方法](router-rehearsal.md#试运行只增加未派工单时的回退演练)。

旧 Router 回退新增只读对账。在停写副本用同版重建基线后核对每张表。实际隔离 Router 新建一单时可定位原请求号；W03 接管并交宿主后，该单退出候选，差异列出 Worker 与投递记录，直接回退仍拒绝。无处理人及旧端未知工位也分开标明。对账前后试运行库摘要一致，篡改基线被拒。复跑 `python3 -m unittest -v rewrite.test_cutover`，macOS 全套 121 项通过。已有新写入的实际回迁仍待演练。

DSH 已在独立会话验证 `/model 提供方/模型` 与 `/effort 强度`。原生 SDK 写入并读回 `model/selection`，指令回执带会话、选择和原生序号。忙碌或不支持的选项被拒。模拟原生写入后丢回包、Worker 落回执前中断，原消息均留待核。手动选了新值以后重送同号，新选择未被覆盖。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk`。SDK、原生日志与两段 HTTP 实跑，模型目录及飞书为夹具，Worker 默认值同步、数字选项、真实模型新请求仍待验。

DSH 飞书 `/model` 和 `/effort` 已核对绑定会话当前选择、最近持久化的模型请求配置及目录。真实 SDK 中会话已选 high、最近请求仍为 medium 时，卡片显示两份状态。目录最多展示 200 项，每页十项，单个提供方失败时保留其余条目。回放原事件保持原卡，观察过程没有新增回合或模型选择记录。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk.DshSdk.test_model_and_effort_distinguish_live_selection_from_recorded_request`，macOS 全套 118 项通过。目录、模型请求和飞书由测试驱动提供，切换和默认配置同步待接。

历史私聊提交 `c99fa4353eaa7102c5e7c24604d36673590343a0` 已在 Linux arm64 再验。发行包 97 个文件复核通过，回归 106 项通过，11 项 macOS DSH 测试跳过。macOS Worker 跨 TLS 交接后的宿主夹具输入仍为一条，W04 只生成一条通知。

旧 Router 私聊迁移保留原消息号、回复关联、工单号、确认或放弃时间、分段正文和附件。测试由实际旧 RouterStore 写入数据，两个新端 HTTP 入口查回，原图逐字节一致。第三方和平台入口无法读取，收件人直接用历史附件引用提交 W04 时被拒，持单仍在。原号重送未唤醒宿主，未确认消息或正文中间缺段会阻止生成试运行库。复跑 `python3 -m unittest -v rewrite.test_cutover`，macOS 全套 117 项通过。飞书私聊记录与订阅另验。

DSH 的飞书 `/context` 已接入绑定会话的版本 4 原生日志。隔离测试的第一条模型回包为输入 46、总计 51、窗口 32768；下一条改用另一路由且缺用量，三个数均显示「未记录」，同号重送仍读回第一张卡。错误 Worker 身份被适配器拒绝。真实 DSH SDK、SQLite、Unix 套接字和两段 HTTP 已运行，模型事件与飞书为测试输入；真实账户消费另验。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk.DshSdk.test_context_command_reads_only_committed_usage_for_the_same_route`。macOS 全套 115 项通过。

同伴消息和宿主回执按单条恢复。故障复现时，首条私聊的错误连续三轮阻挡后续私聊和新工单，接收工位原生输入为零、工单一直待派。修正后，同一时序下第二条私聊与正式派发均送达；首条凭据指向错误会话时保持待核。重启并查回原凭据后，首条只进宿主一次。一条上行回执暂时失败时其他回执照常提交。测试使用真实 Router、Worker、SQLite、HTTP，宿主与时钟为夹具。复跑 `python3 -m unittest -v rewrite.test_peers`，七项通过，macOS 全套 114 项通过。

macOS Worker 与 Linux arm64 Router、宿主适配器已通过两个独立 TLS 端口核验。两端安装同一提交 `2025c300`，Linux 复核 92 个发行文件。重投后宿主日志仅一条输入，回合结束仍为 working，夹具 Agent 报告后转待验收，只发一条通知。Linux 回归 102 项通过、10 项因 macOS DSH SDK 缺席跳过。实测使用同机 Linux 来宾，跨物理设备、真实模型调用另验。[原始记录](evidence/linux-cross-os.json)和[复跑说明](README.md#跨系统验收)。

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
| 工位点对点消息 /messages、消息 ack；飞书与 Worker 的聊天记录 /chat、订阅与 notice | Worker 私聊与 Router 投递；训练员私聊由 Worker 对接 | 同伴收发、确认、附件和旧同伴历史已用隔离 HTTP 验证；宿主输入为夹具，旧飞书聊天记录、订阅与真人工位另验 |
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
