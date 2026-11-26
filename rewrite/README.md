# 魔物链接重做版 · 正式工单主线

本目录对应 Wiki 版本 `9db82a32af701ba5e8af39a545f9bf0c771b5600` 的 00–04 和 02A。此切片覆盖有指定处理人的文字工单，从建单、接管和原生输入交付，到 Agent 主动报完工、溪流查验和明确结单。

## 本地复跑

在本仓库根目录使用 Python 3.11 或以上版本，只需标准库。

```sh
python3 -m rewrite.devloop
python3 -m unittest -v rewrite.test_mainline
```

第一条启动真实的重做版 Router 和两个客户端，使用系统分配的 `127.0.0.1` 端口，约 20 秒后输出一次完整交接的 JSON。每次运行生成新的身份凭据、会话内工单与派发号。Router、Worker、宿主适配器、模拟宿主的输入账本分别使用临时 SQLite 文件，退出后清理。第二条用可控时钟复验边界和故障恢复。

**怎样读结果。** `host_receipt` 来自目标宿主夹具自己的输入账本，带会话、输入号、投递号和正文摘要。`after_turn` 应为 `working`。夹具 Agent 在该输入专属的 MCP 管道调用 `worker_report` 后，W04 回应 `completed_reported` 并给出原派发号。Hermes 侧收到一条含成果正文、同一工单号的 `Notify`。最后由模拟溪流显式验收，才出现 `after_review: closed`。

完整回归从仓库根目录运行 `python3 -B -m unittest discover -q -s rewrite -t .`。独立发布包只带 `rewrite/`。从安装目录跑全套时，把固定提交的 `server/router`、`server/trigger` 和 `sources/email` 导出到另一个临时目录，将 `MOWULINK_TEST_LEGACY_ROOT` 指向该目录。旧源码只用于迁移与回退用例。

## 边界与证据

| 程序入口 | 拥有的状态及操作 |
| --- | --- |
| `orders.py` | Router 的工单、处理权、派发号、版本、正式报告和幂等回执。在一个 SQLite 事务内变更工单并写入投递事件。待派和已持有均占用该 Worker 的唯一派发位置。 |
| `delivery.py`、`wire.py` | 身份绑定、HTTP、按接收方隔离的 SSE 帧和连续收件游标。开发流每次返回一批并关闭，消费者带游标重连。C02 仅确认已持久收件。 |
| `hermes.py` | 溪流侧的建单、查单、明确验收，以及待交通知。Hermes 连续空闲满十秒后逐条交付，取得宿主凭据才出队。 |
| `worker.py` | Worker 的收件、持单副本、飞书私聊消息映射和待交报告。先 W03，后调用适配器。持久保存 W04 请求，按 Router 回执中的原派发号释放。 |
| `adapter.py`、`agent_tools.py` | 目标宿主会话、投递意图、宿主接收凭据和原生输入关联。MCP 由宿主接入方绑定到当前输入，模型只给工单号、派发号及成果；普通私聊输入即使知道派发号也不能代报。 |
| `devloop.py` | 隔离装配与夹具。模拟 Agent 通过真实管道调用 MCP，模拟宿主独立落库后返回凭据；模拟溪流通过实际管理适配器收结果。 |

实测跨越本机 HTTP/SSE、MCP 管道与独立 SQLite 事务，包含 W01/W02、C01/C02、A02/A04、W03、W04 的主动完工、A06 的验收结单。Worker 上报时取真实适配器的忙闲接口；开发用的采样间隔与在线租期是可替换参数。回环采用明文 HTTP 和随机开发凭据，正式 HTTPS 与生产凭据不在本次接入范围。

宿主已收但凭据丢失时，适配器只按同一投递号查回。凭据必须匹配目标会话和原内容；无法查回就保留待核。测试还覆盖 W03/W04 写入成功后丢回包、已接派发重投、双单同时争取一个空位、忙闲翻转、失效派发首次迟报以及替换适配器。

DSH 同一工单来源的回合可用 `worker_submit_material` 单独提交候选文字或已经授权的附件引用。Router 仍核对原派发、入池状态、当前持权和有没有再次派发。材料回执只返回材料号，原样重投返回同一号。新的材料在再派后会被拒绝。W04 的失效派发错误继续按原协议返回。

已核验的 DSH 工单回合可用 `worker_stage_artifacts` 暂存本工位适配器拥有的文件，拿到 Router 的 `artifact_ids` 后再随 W04 或候选材料提交。暂存只收已登记的附件号。Router 收完整文件后仍只向原工位开放，正式回报或候选材料写入后才让溪流读取。

首段的 Agent、Codex/Hermes 宿主、飞书输入与验收人均为夹具。真实客户端、飞书卡片、Trigger/附件、候选材料、转派与取消、完整掉线入池、询单和提问后续接入。各入口只接受本切片明示的业务动作。

第二段已用本机 Codex Desktop 独立聊天验收正式工单输入，复跑方式和原生回执见 [Desktop 接入实测](desktop-check.md)。上述 `rewrite.devloop` 继续提供可离线运行的夹具回环。

后续已扩展 Router 生命周期、消息附件、问答、同伴私聊及插件。以[全量替换进度](replacement-progress.md)核对各项版本和实测边界。

### 同伴私聊与附件

接收工位查询指定消息时，`local_pending` 列出投递或确认失败的阶段、错误、尝试次数和下次重试时间。未拿到可靠凭据时继续沿用原投递号，按单条从 5 秒退避至最多 5 分钟。重启后沿用同一记录，其他同伴消息和工单可继续处理。

接收同伴私聊开始宿主投递前，Worker 保存原生会话。此后收件不明只查原记录。更换会话或重新创建适配器账本时，待核消息不会再写一遍。旧版未回执记录也仅查回。升级先更换适配器，再更换 Worker，并保留原账本。

从旧 Router 带入的已确认或已放弃私聊由原参与人按来源 UUID 查询。Router 入口为 `GET /v1/history/{source_id}/peers`，宿主经绑定的 Worker 访问 `GET /v1/worker/history/{source_id}/peers`。列表用 `limit` 和 `next_cursor` 分页。消息详情路径再附原消息号，长正文按 `next_part` 逐段读取。宿主可向详情路径下的 `/file` 提交 `{"position":0}`，取得本工位媒体引用供查看。历史记录只有原 Router 确认时间或放弃时间，迁移过程不产生新的投递或宿主回执。见[迁移演练](router-rehearsal.md#复跑)。

同伴消息的发件和收件宿主可通过各自 Worker 的 `GET /v1/worker/peers/{message_id}` 查回交接进度。`accepted_at` 记录 Router 收件，`worker_received` 表示接收 Worker 已确认入箱；`host_receipt_id` 有值表示接收 Worker 已核指定宿主的原生凭据并向 Router 报备。宿主会话及原生收件详情留在接收工位。上述回执都不代表对方已经回复。

同伴附件由发送宿主先交给本工位的媒体入口，再向 `POST /v1/worker/peer` 提交 `to_worker`、`message_id`、可选的 `text`、`reply_to` 和本地 `attachment_ids`。需要区分图文时按位置给 `attachment_kinds`，默认按普通文件。Router 核对发送工位持有附件，收件工位以自己的身份分块取得并校验，再从本地媒体入口交给自己的宿主。该接口返回 Router 收件凭据，原生收件凭据记录在接收 Worker 的同伴历史中。

DSH 的 `worker_send_peer` 可在已核对的原生回合中直接联系指定工位。回复带原消息号作为 `reply_to`。Worker 从原会话、原回合和工具调用号生成稳定消息号，同一调用重试取原 Router 回执。附件只接受本工位已有授权的媒体引用。此工具没有建工单、答题或报成果权限。

发件 Worker 在首次请求前保留原信与附件引用。连接失败或 Router 回执待核时，恢复后续用原消息号和原请求键。没有登记的收件工位等明确拒绝会停止后台重试。已确认入箱的私聊继续单独核对对端收件。

原生发送返回 `pending` 时已保留消息号。用 `worker_peer_status` 按此号查 Router、对端 Worker 和原生宿主三层收件；观察不到的状态留空。对端限原参与者查询。

DSH 可用 `worker_list_peers` 浏览已登记的其他工位。它只返回工位 ID 与 Router 连接状态，可按在线筛选和分页。工单与持单明细仍由管理端查看。

`worker_peer_inbox` 可只读查看当前工位收到、尚待宿主确认的同伴消息。带 `view: "history"` 时返回最近二十条的消息号、方向、对端和原回复关联。读收件箱无需重新投递或确认。

### 飞书私聊的异常回执

绑定的训练员私聊发生格式、附件或收件问题时，平台进程在原消息下回复状态卡。平台已尝试而回执丢失时，沿用原 UUID 和卡片正文，首次尝试满 55 分钟后标记 `receipt_unknown`，继续处理其他消息。等待中已查实且尚未发送的提示自动撤销。若已提示待核，查到宿主凭据后再发一条恢复确认。消息的受理状态在 `platform_events`，平台回复的状态和消息号在 `input_notices`。[飞书回复消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/reply.md)

### 来源留档与通知积压

```sh
python3 -B -m rewrite.service trigger-status --db /path/to/source.db
python3 -B -m rewrite.service delivery-status --connection /path/to/consumer.json
```

`trigger-status` 读取现有来源库的实例、已留档数和仍缺 T04 回执的条数。文件不存在就返回错误。已生成时间来自来源本机时钟。第二条查询已认证接收者在 Router 的最新投递游标、连续确认位置及仍未确认条数。后发帧先确认时，连续游标停在缺口前。Info 没有推送帧。C02 只证明接收端入箱，宿主收件仍由各适配器核对。两条命令均只输出汇总。

### 训练员原生题卡

训练员可以在原卡一次提交同一公开请求里的多道题。按题目下方的序号选择，支持多选的题用逗号隔开，留空跳过。公开原题允许自填时才提供输入框。Worker 等同一会话和原请求的宿主关闭凭据再更新原卡。Codex App Server 每题单选，DSH 计时题进入原生续答后可多选。测试使用飞书 JSON 2.0 表单的批量回调结构，实际飞书交互待验。[飞书卡片组件](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/component-json-v2-overview?lang=en-US)

```sh
python3 -B -m unittest -v rewrite.test_question_cards \
  rewrite.test_codex_app_server.CodexAppServer.test_native_trainer_question_closes_original_card_only_after_app_server_receipt \
  rewrite.test_dsh_native_loop.NativeLoop.test_dsh_native_timed_question_closes_the_original_feishu_test_card
```

### Codex 独立工位的断联与恢复

本机安装包的 App Server 在独立 profile 中接到一张正式单。分别断开 Worker 到 Router 和到适配器的 TCP 链路，原生回合都继续写完测试文件。断 Router 后，W04 待发队列保留成果并返回可重试的 503。原派发已入池时，重连重放得到 `stale_assignment`。断适配器时 Router 保留原持单和在线状态，宿主忙闲未知。原生工具恢复后重报，Router 记录一次正式成果并待验收。两项各一条原生工单输入，飞书断联提示由测试平台收取。[实测](evidence/codex-worker-link-outage.json)

```sh
python3 -B -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_native_codex_work_and_w04_across_router_and_adapter_outages rewrite.test_link_health
```

设置 `MOWULINK_CODEX_LINK_OUTAGE_EVIDENCE` 为临时文件路径可输出原生回合与业务回执。模型和计时由本地测试驱动，真实 Desktop 独立账号与飞书另验。

另有 macOS 与 Linux 虚拟机的双向 TLS 演练。Router 和 Codex App Server 位于 macOS，Worker 在 Linux 虚拟机内由 `rewrite.service worker` 启动。适配器通过原端口回连 Worker，重启后复用原 Worker 库。原生回合在 Worker 停止时继续工作，恢复后由同一回合交出一次 W04。[跨虚拟机实测](evidence/codex-linux-worker-restart.json)

```sh
MOWULINK_CODEX_CROSS_GUEST_IMAGE=<已有-linux-arm64-镜像完整ID> \
  python3 -B -m unittest -v rewrite.test_cross_guest
```

该测试需要本地 Linux 虚拟机中的 Docker、有双向回环转发的配置，以及 Docker 可挂载的本机目录。Colima 使用 `host.lima.internal`。可分别通过 `MOWULINK_CODEX_CROSS_GUEST_HOST` 和 `MOWULINK_CODEX_CROSS_GUEST_SHARED` 指定 Linux 可达的宿主别名和临时共享目录。测试使用临时端口、独立证书和会话，模型由本地脚本驱动。可设 `MOWULINK_CODEX_CROSS_GUEST_EVIDENCE` 保存回执对照。

### DSH 固定会话与飞书卡片

在独立 DSH 测试配置加载 `integrations/dsh_host/index.mjs`，指定测试会话的 `session` 和仅当前用户可读写的绝对目录 `directory`。单独启动 `python3 -m rewrite.service dsh-adapter --help` 所示的适配器，将 `--session`、`--runtime-dir` 分别指向同一会话和目录。收图需宿主提供 `attachments` 服务；`--worker-connection` 指向适配器访问专用 Worker 的连接配置。固定会话插件中仅登记 Agent 业务工具，本体负责发送飞书卡片。

图文在同一次输入进入 DSH，宿主保留规范化图片和普通文件引用。适配器从 DSH 已持久写入的公开会话事件生成原生输入、过程输出、工具、图片与最终答复。工具预览仅包含可判明的命令动词。首次绑定以插件接入时的位置为起点；续跑通过原会话、位置和上一条记录摘要核对。在中断的回合中，已有回复草稿按过程输出显示。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk`，该套件使用本机 DSH SDK、临时工位及测试驱动，模型和飞书云端另验。

绑定 DSH 会话后，飞书 `/model`、`/effort` 显示会话当前选择、最近已记入原生日志的请求配置和宿主公开目录，十项一页。用 `next`、`prev` 或 `page 2` 等查看后续页。目录缺失会在卡片提示。同号重试沿用已生成的原卡。

绑定会话空闲时，用 `/model 提供方/模型` 切换路由，用 `/effort 强度` 选择当前路由支持的强度，或用 `/effort default` 随模型默认。两条命令也可带最近确认送达的菜单编号。宿主当前模型与力度菜单所示路由不符时，需重看菜单。宿主校验选项后保存该会话的原生记录，确认回执包含会话、选择和原生序号。若回执未到，原消息显示待核且不会重做切换。通过 `/model` 或 `/effort` 可查看此刻的状态。Worker 默认值同步及真实模型调用待验。

收到取消或转派时，如果原生会话仍在执行可核对的原派发回合，DSH 插件请求停止，保留其他待处理消息，再交控制请求。原生输入号、回合号或会话位置无法对准时，插件仅递交控制请求。没有清理凭据时，Worker 保留原派发的待核状态。

独立只读 DSH 工位可在宿主插件打开 `read_only_assignments`。该配置只适合会话内仅需查询和工单回报的工位。宿主会限制该会话的原生工具执行，并保存派发输入是在这条限制下交付的本地记录。收到取消或转派时，只撤下该旧派发已登记的排队输入，之后读回实际回合结束和原生收件箱撤销事件。工位随后才能经模型的 `worker_report` 确认旧派发清理。回执按原派发保存和重试。普通读写工位仍需实际工作区清理凭据。

停写迁移时，随原生宿主记录保留运行目录里的 `read-only-admissions`。适配器 SQLite 单独保存已核实的原生撤销事件。已提交给 Router 的回执由 Worker 的发件账本负责查回。

复跑 `python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_read_only_dsh_acknowledges_original_cancel_or_transfer_and_recovers_w04_reply rewrite.test_dsh_native_loop.NativeLoop.test_native_dsh_stop_alone_or_unverified_tool_keeps_cleanup_pending`。[只读工位证据](evidence/dsh-read-only-cleanup.json) · [待核反例](evidence/dsh-native-cleanup-refusal.json)

独立 DSH 测试工位可配置 `assignment_container`，提供本机 Docker CLI 的绝对路径 `docker`、已授权的本地 Linux 引擎 Unix 套接字绝对路径 `socket` 和已经存在的 Linux 镜像 ID `image`。该模式与 `read_only_assignments`、`relay_command_approvals` 互斥。镜像需要 `/bin/sh`、`/bin/sleep` 和 `python3`，且无默认持久卷。工具 `worker_workspace_shell` 只能由原生回合中与原派发匹配的调用执行。每个派发拥有自己的容器和内存工作目录，使用普通用户、只读根文件系统、有限资源和独立网络命名空间。没有项目目录、凭据或 Docker 套接字挂载，也不会自动拉镜像。

取消或转派时，DSH 仍核对原生回合结束和原收件箱撤销，宿主同时收尾该派发的容器。以原引擎、容器 ID、标签及派发账本核对移除，确认后才交 W04。引擎身份不符或无法确认时保留原派发待核。同一派发重复查回执沿用原记录。测试时用 `MOWULINK_DSH_CONTAINER_IMAGE=<已有镜像完整ID> python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_workspace_container_cancel_and_transfer_remove_owned_processes`。实验范围为无网络的私有临时工作目录。升级或回退前先用支持该配置的版本收尾原派发并核对容器。旧版插件无法识别这个配置。现役 DSH 工作树接入与活工作目录跨机恢复仍待验。[原生实测](evidence/dsh-assignment-container.json) · [Docker 容器移除](https://docs.docker.com/reference/cli/docker/container/rm/) · [临时存储](https://docs.docker.com/engine/storage/tmpfs/)

A10/A11 各切断一个独立回环监听器，使用本机安装的 DSH、现有 Docker 镜像、独立 Router 和临时库。复跑 `MOWULINK_DSH_CONTAINER_IMAGE=<已有镜像完整ID> python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_dsh_work_survives_one_broken_link rewrite.test_link_health`。设 `MOWULINK_DSH_LINK_OUTAGE_EVIDENCE` 为临时文件路径可保存派发、原生输入和清理回执。两项各核原生输入仅一条。时间由测试时钟推进，模型与飞书由本地夹具驱动。[断联实测](evidence/dsh-worker-link-outage.json)

原工单因失联入池后，若溪流明确要求取消，详情的 `pending_cleanup` 仍列原派发。原工位重连后收到停止指令，清理并交 W04 才标为 `cancelled`。若工单后来已续派，迟到的清理确认仅结束旧派发的清理。Worker 主动交回的受阻单已有 W04，可以取消池中待办。复跑上面的原生断联测试及 `python3 -B -m unittest -v rewrite.test_pool`。[掉线入池后取消](evidence/dsh-offline-pooled-cancel.json)

模型可调用 `worker_workspace_export`，提交 `/workspace` 下的相对路径。单文件限 16 MiB，拒绝符号链接和非普通文件。Worker 校验内容摘要，经原有附件通道暂存，续传按原偏移和附件 ID。成果工具 `worker_report` 接收暂存工具返回的 `artifact_ids`，由 Router 在 W04 正式记录后给验收人读取权限。成功或阻塞报告要等 Worker 持久化 Router 的释放回执，宿主才回收工作区。回执未确认时保留容器和原会话绑定，重开 Worker 后沿原 W04 重取回执。单纯结束原生回合不会触发回收。启用容器工位时成对升级 Worker 和 DSH 适配器。新版宿主需要 Worker 的工作区状态回执，旧 Worker 缺少该接口时工作区工具会拒绝执行。

复跑 `MOWULINK_DSH_CONTAINER_IMAGE=<已有镜像完整ID> python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_workspace_exports_artifact_and_seals_after_durable_w04_replay`。真实 DSH AgentLoop、Linux Docker、Worker 和 Router 完成导出和回收。测试输入和模型工具调用由本地脚本驱动。[附件和回收证据](evidence/dsh-workspace-artifacts.json)

需要在隔离工位处理项目文件时，可在 `assignment_container` 增加 `"seed":{"archive":"/absolute/private/project.tar","sha256":"<64 位 SHA-256>"}`。由工位操作者审核并固定项目版本，再用 `git archive --format=tar <完整提交SHA>` 生成不超过 24 MiB 的未压缩归档。用仅所有者可读写的文件保存归档；可在生成目录先执行 `umask 077`，用 `shasum -a 256` 核对摘要。归档最多 4096 个普通文件或目录条目，拒绝符号链接、硬链接、路径越界、`.git` 和特殊文件。模型的第一条工作区命令在副本完整导入并核实后运行。每个派发从同一固定版本各复制一份到 `/workspace/project`。源工作树继续由原操作者管理，改动由 `worker_workspace_export` 和 W04 交回。

归档导入回执丢失时，同派发可重试初始化，留在容器中的已发布目录会复用。由工单工具修改过的容器如果重启，原始启动标识不匹配时拒绝写入和导出，不能用快照恢复已丢失的改动。取消仍按原容器和原派发验证清理。一个工位使用一个经审核的项目快照。轮换快照前核对旧派发工作区已回收，并保留原包及摘要以支持未完成初始化查回。现役项目使用、构建依赖与文件回合并由各项目另验。实测默认工作目录带有 `noexec`，镜像内解释器可运行脚本。需要直接执行容器内构建产物时，可在独立测试工位的 `assignment_container` 内显式设 `"execute_workspace_files": true`。该选项只作用于新派发的 `/workspace`，`/tmp` 继续使用默认挂载。隔离容器仍使用普通用户、只读根、无网络和原资源上限。编译器与依赖由预先选定的镜像提供。原派发记住容器的挂载方式。重开时修改开关会拒绝原派发继续运行命令，原文件仍可导出，按原配置和容器记录完成回收。更换工位配置前先排清旧派发。回退到旧版也应先在当前版本完成原派发的回收，再恢复旧版工位配置。

原生 DSH 对照实测编译了同一 C 程序。默认工位执行返回 126，经 W04 报受阻。启用工位直接运行返回 0，验收人读回相同 ELF，W04 待验收。另验已有派发的开关改动双向均被拒，取消后原容器被移除。复跑 `MOWULINK_DSH_CONTAINER_IMAGE=<已有镜像完整ID> python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_workspace_optional_execution_builds_runs_and_reports rewrite.test_dsh_native_loop.NativeLoop.test_dsh_workspace_execution_policy_is_pinned_until_cancel`。[编译、运行和切换证据](evidence/dsh-workspace-compiled-project.json)

复跑 `MOWULINK_DSH_CONTAINER_IMAGE=<已有镜像完整ID> python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_workspace_seeds_private_project_and_recovers_seed_ack rewrite.test_dsh_native_loop.NativeLoop.test_dsh_workspace_refuses_unverified_or_escaping_project_seeds rewrite.test_dsh_native_loop.NativeLoop.test_dsh_workspace_restart_cannot_reseed_or_confirm_lost_work`。[项目快照实测](evidence/dsh-workspace-project-seed.json)

同机冷恢复时保留原 DSH 会话目录、私有运行目录和仍运行的原 Linux 引擎，停写后再复制 Worker 与适配器的 SQLite 账本。重开仍用原身份与绑定。在途派发可继续用原容器内的改动。Router 已记录成果、本地还没收到 W04 回执时，重开的 Worker 从原发件账本重取回执，DSH 再按原容器回收。这个演练覆盖宿主和两个 HTTPS 进程均停机重建。容器工作目录位于引擎内存，跨机交接前应按原派发导出成果并确认回收。

复跑 `MOWULINK_DSH_CONTAINER_IMAGE=<已有镜像完整ID> python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_workspaces_cold_restart_recovers_unreported_work_and_committed_w04`。[同引擎冷恢复证据](evidence/dsh-workspace-cold-recovery.json)

离线换绑使用格式 3 快照时，`audit-handoff` 与 `stage-native-handoff` 接收 `--dsh-runtimes`，用其指向已停机的原私有运行目录。工作区未回收或原记录无法核验时保留原绑定，即使 W04 已确认。操作步骤见[离线恢复说明](router-rehearsal.md#重做版五组件的离线恢复)。实跑 `MOWULINK_DSH_CONTAINER_IMAGE=<已有镜像完整ID> python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_offline_handoff_waits_for_dsh_workspace_after_w04_release rewrite.test_state_bundle.Bundle.test_handoff_rechecks_dsh_workspace_journal_before_publishing`。[换绑实测](evidence/dsh-offline-workspace-handoff.json)

版本回退也接受同一份 `--dsh-runtimes` 配置。普通 `restore` 可以准备接回原引擎的数据副本。用已安装的 DSH SDK、本机 Linux 引擎和两版 Git 发行包，从临时 HTTPS 端口复跑版本回退。前一版使用已复验的保留版本，测试镜像使用本机已存在的完整镜像 ID。

```sh
python3 -B -m rewrite.release_check --repository . --component dsh-workspace-rollback \
  --previous a469849b7815aaaf9eb6e9be504afac3c2ae4e4b --new "$TESTED_REVISION" \
  --dsh-image "$TEST_DOCKER_IMAGE"
```




启用原生目标服务的 DSH 会话可用 `/goal` 查询、`/goal 目标正文` 新建、`/goal pause` 暂停、`/goal resume` 恢复。原生服务使用自己的默认轮次，已有未完成目标时拒绝替换。状态卡显示原生阶段、轮次和续跑授权。未确认的变更保留待核，重送同号不会再次修改。续跑由 DSH 宿主负责。

启用原生压缩服务的 DSH 会话支持 `/compact`。Worker 先保存指令，宿主空闲时开始压缩。回执丢失后只按原指令号查原生记录。宿主保存了摘要、替换记录及结束条目后，飞书才显示已压缩的条数。连续五分钟无法确认时提示待核。摘要正文保留在宿主。

DSH 会话可通过 `/resume` 查看宿主目录。插件配置 `catalog_sessions` 可额外添加获准展示的原生会话 ID，默认只查询当前绑定。列表支持 `next`、`prev`、`page 页码` 和 `search 标题或会话号`。目录授权只用于浏览。切换到已经运行的会话另见[飞书切换会话](#飞书切换会话)。

插件只在初始显式绑定时通过 DSH 打开配置指定的会话。绑定后检查活动实例是否仍为原实例。关闭或重新打开同一个会话 ID 后，旧绑定会报告宿主不可用，状态检查不会唤醒会话，旧适配器请求也不会交给新实例。

DSH 的 `/stop` 可中断当前已确认的单条普通对话回合，宿主收件箱中的后续输入保留。Worker 查看原回合的中止记录后才确认。工单的停止、清理和回执继续走 Router；原生目标可用 `/goal pause`。

`/queue` 的消息在首次递交前保存写入意图。收件待核时只向原会话的适配器查凭据，不能通过清空或更换适配器账本再送。升级前核对原队列、保留原账本。

### 隔离安装与升级

启动 Worker 时可加 `--adapter-session <原生会话ID>`，将适配器访问固定到该会话。固定请求使用独立路径，适配器逐次比对。Worker 首次收到同一会话的有效忙闲回包后，保存这个绑定。此后删除参数或直接改为另一个会话会拒绝启动。适配器投递库也固定到原生会话。隔离换绑用新的状态副本和目标适配器库，见[停写换绑演练](router-rehearsal.md#隔离工位冷换原生会话)。

### 原生交接窗口

为独立测试工位在 Worker 的身份配置里明确登记 `handoff/<Worker ID>`，使用此角色的专用连接文件。只读 `ops`、平台和其他 Worker 身份均无此权限。为一次交接生成一个 UUID，开始、查询故障和解除都沿用此号。

```sh
python3 -B -m rewrite.service handoff-admission --connection "$HANDOFF_CONNECTION" --handoff-id "$HANDOFF_ID" begin
python3 -B -m rewrite.service worker-status --connection "$STATUS_CONNECTION" --worker-id "$WORKER_ID"
python3 -B -m rewrite.service handoff-admission --connection "$HANDOFF_CONNECTION" --handoff-id "$HANDOFF_ID" end
```

开始回包为 `frozen` 时，Router 已停止为此工位预约新派发。已有预约、持单、待处理业务或正在运行的原生回合会拒绝开始。工位在线状态仍持续更新。窗口内飞书入站与同伴信各自保留在原队列，待解除后按原号进入工位。确认 `released` 后还要等一次新的忙闲观测，再从头计算连续空闲时间。回包丢失时 Worker 保留原交接号，重开或重试使用同一号恢复。升级回退及停机换绑前先确认已解除。

此入口可单独暂停和恢复派发，也用于[飞书切换会话](#飞书切换会话)。

新版接管的新派发在发出 W03 前固定其空闲宿主会话。接管回包到达后若会话已变，工位保留原持单，原派发、更新、取消和原题答复都不交给另一会话。另一会话的报告不能触发 W04。升级前的在途旧派发没有这份本地记录，演练时先完成旧单对账，并保留原工位的显式会话配置。

正式派发首次尝试前，Worker 先写入宿主收件待核状态。之后只用原派发的投递号向原会话查凭据。空账本查不到记录时保留持单。仅在适配器确认宿主忙碌且未写入时，才允许等待下一次接收。先升级适配器以提供新查回端口，再升级 Worker，保留两个状态目录。

更新、询单、取消、转派交接、搁置通知和原题答复各沿用原 Router 投递号。Worker 首次送宿主前记下写入意图，回执丢失只查原号。升级前遗留的待核投递也只查回。Router 按原协议发出另一个取消或询单投递时，工位按新投递号处理。

普通飞书私聊先按飞书消息号入箱，再确认原生会话并保留目标后开始投递。首次查询会话就失联时，消息保留在入箱，连接恢复后同号可继续首次投递。已有目标但收件不明时，重试只查回该会话的凭据。升级前没有绑定记录的旧消息，只有原适配器能出示匹配凭据才确认。升级时先在隔离环境更新适配器，再更新 Worker，保留双方的数据目录。新版 Worker 的私聊投递和查回均使用会话校验路径。

恢复联调前，可用专用 `ops/<工位ID>` 身份读 Worker 最近一次观测。下列命令中的 `WORKER_ID` 是测试工位，`OPS_CONNECTION` 是它的私有连接配置。

```sh
python3 -m rewrite.service worker-status --worker-id "$WORKER_ID" --connection "$OPS_CONNECTION"
```

回执列出 Router 与适配器各自的连接、预期与已证实的会话、忙闲、观测距今秒数和当前持单。Router 断开时可见距下次重连秒数。最近一次成功观测的 `native_capabilities` 包含下一回合排队、上下文用量、压缩、会话目录、停止私聊、读与改模型、原生目标。它反映适配器的能力声明，实际执行另有回执。适配器失联或会话校验失败时，当前会话、忙闲与这份能力声明均为未知。查询只读已有观测。

宿主状态回包缺失或格式错误时，Worker 将该宿主会话和忙闲标为待核，继续维护 Router 连接。后台轮询遇到临时数据库写入故障后会继续，并保留待处理投递。Router 暂不可查询时，发件 Worker 可显示本地已存的 Router 收件时间，对端 Worker 和宿主收件仍留空。

在受信任的构建机选定一个完整提交号。以下命令可从源码工作区运行。构建只取该次提交中受 Git 跟踪的 `rewrite/`，包括该版的宿主入口、说明和测试。将归档传给测试机时，经独立可信渠道核对提交号与输出的 SHA-256。安装目标取一个尚不存在的版本目录，运行状态和连接配置使用单独的私有目录。

```sh
python3 -m rewrite.release build --repository . --revision "$REVISION" --destination ./build
python3 -m rewrite.release install --archive ./build/mowulink.tar --revision "$REVISION" --sha256 "$SHA256" --destination "./releases/$REVISION"
python3 -m rewrite.release verify --archive ./build/mowulink.tar --revision "$REVISION" --sha256 "$SHA256" --destination "./releases/$REVISION"
(cd "./releases/$REVISION" && python3 -B -m rewrite.service router --help)
```

目标机器需要 Python 3.11 或以上。安装好的版本在自己的目录下启动，`-B` 禁止写入字节码缓存。Router、Worker、Trigger 使用标准库。飞书需要独立 Python 环境中的官方 SDK，DSH 与 Hermes 使用宿主提供的插件依赖。逐字节核验会识别未登记的文件及已改内容。保留已验证的前一版本目录可供回退。

新版 Router、Worker、宿主适配器及飞书平台入口运行时持有相邻的 `.writer` 状态锁。同一状态路径已被占用时，第二个实例以 `state_writer_already_running` 退出。进程退出即释放锁，目录内保留空的锁文件供下次使用。离线备份遇到占用会拒绝开始；切换仍须停掉使用旧版入口的写者。

升级先停隔离写者，按[离线恢复](router-rehearsal.md)采集组件清单和状态摘要。在新状态目录恢复副本，仅让新包在该副本上启动。先核原持单、投递凭据、未送消息和附件，再引入新工作。返回原版本前停新写者，通过 `state_bundle rollback` 核对试用期间的写入；新写入先逐笔对账。原生宿主绑定、认证及平台投递记录由相应所有者另核。

### 飞书切换会话

启动 Worker 时，保留原会话的 `--adapter-session` 与 `--adapter-connection`，另传 `--resume-targets /path/to/targets.json`。这是仅文件所有者可读写的 JSON 对象，例如 `{"test-other-session":"other-adapter.json"}`。值是适配器的 TLS 连接配置路径，可用相对该文件的路径。每个适配器已运行且固定到对应的宿主会话，使用自己的持久账本。

Worker 为每个候选会话和原会话登记各自的 `adapter/<Worker ID>/<会话 ID>` 回调身份。令牌各不相同。启用切换时，Worker 会拒绝同工位的旧式通用适配器身份。保留 `handoff/<Worker ID>` 的独立授权供运维中止未提交的切换。

绑定的飞书训练员可发 `/resume` 看目录，再用 `/resume 编号`、`/resume 完整标题` 或 `/resume 会话ID` 切换。`/switch 会话ID` 复用同一条切换链。目录沿用旧版从 0 编号的规则，每页十项，翻页保持本次列表顺序。按编号选择前需查到卡片送达凭据。可回复对应页的目录卡。刷新、搜索或成功切换后，旧目录卡会失效。同名会话请用 ID，标为「仅可浏览」的会话需先配置适配器。

Worker 先确认没有持单和待处理的原生业务，暂停派发，核对两个会话均空闲后保存新绑定。Router 确认解除才返回切换卡。新消息使用新会话，已投递消息重试只读原回执。Worker 重开时以已提交的绑定为准，旧会话身份发来的新回调会被拒。

Worker 在暂停派发之前和写入新绑定之前，会用固定会话查询原适配器是否还有未确认的原生审批。Codex 和 DSH 适配器都从自己的审批账本判断。即使原生已空闲、Router 尚未收到登记，待确认审批也会保留原会话。若交接已经暂停派发，则用原交接号解除，丢回执后沿用原号。原审批按原号核对后，可发一条新切换命令。新 Worker 遇到暂未提供该观察能力的旧适配器会提示升级，普通对话沿用原会话。测试限于已接入的审批和独立宿主，日常客户端及飞书真机另验。[审批期间切换实测](evidence/native-handoff-approval.json)

飞书入箱按本地收件顺序处理切换。`/resume` 的选择、`/switch` 和新建、清空、分支会话的命令要等之前已收的消息取得回执或明确失败。目标暂不可达时，后收的消息等同一切换命令有结果，再交给当时已确认的会话。待核及等待顺序跨进程重开保留。普通消息间仍可各自重试，已记下的展示卡片可继续出向。[入箱顺序复验](evidence/feishu-resume-inbox-order.json)

目标已确定但还在探测时，`worker-status` 显示 `native_switch.phase=checking`。原工位继续接收正式派单和原生回调。飞书后收的私聊仍在入箱等待原请求。用输出中的 `native_switch.handoff_id` 执行 `handoff-admission ... end` 可撤回，Worker 记录原命令的撤回回执，后续私聊继续进原会话。这个阶段尚未请求 Router 暂停派发。[预检撤回复验](evidence/feishu-resume-preflight-cancel.json)

切换到 `starting` 时，目标断开会保留原会话及交接窗口。仍可用原号解除，等 Router 确认后继续使用原会话。到达 `bound` 时，恢复沿用已经提交的目标，目标重新可读后才解除交接。升级或回退前先核完正在探测或切换的原命令。此路径已使用隔离 Codex App Server、DSH、真实 TLS 进程和飞书夹具验证。[目录与原生回执](evidence/native-resume-catalog.json)

已准备好的独立 Codex App Server 历史会话可用 `codex-adapter --resume-on-handoff`。将适配器的固定 `--session`、`--cwd` 和独立 profile 对齐，再将其连接配置加入 Worker 的 `--resume-targets`。[Codex 冷会话证据](evidence/codex-cold-resume.json)

DSH 历史会话在宿主插件中设 `resume_on_handoff: true`，并以 `dsh-adapter --resume-on-handoff` 启动对应的适配器。两端使用相同的 `session` 和宿主运行目录。隔离宿主须提供 `sessionQuery.inspectSession` 和已持久保存的目标会话。适配器连接也须加入 Worker 的 `--resume-targets`。DSH 核对原生历史后通过 SessionController 恢复，再为这一原生 Agent 安装业务工具。已绑定对象关闭后，原插件会报告宿主不可用。宿主和适配器重开后，可按已提交的交接回执恢复当前会话。[DSH 冷会话证据](evidence/dsh-cold-resume.json)

两端均可先启动适配器并保持会话冷态。浏览目录不会打开会话。Worker 等 Router 确认暂停派发才用原交接号恢复会话，核对会话和忙闲后提交绑定。发现活动原生目标时，保留原会话并解除交接。Codex Desktop 日常会话仍待接。

隔离 Codex 适配器可启用 `--prepare-sessions`。固定会话入口 `POST /v1/host/bound/prepare-thread` 接收原操作 UUID 和 `new` 或 `fork`，沿用工作区、模型及思考强度准备只读测试会话，并记录原生会话 ID。只启用该选项时，仍可按新 ID 显式配置独立适配器，再走已有的 `/resume` 交接。

要在隔离工位启用自动换绑，适配器同时传入 `--managed-sessions-dir`，路径为已有的绝对私有目录（POSIX 权限 `0700`），Worker 传入 `--session-callback-key`，其文件为仅所有者可读写的 JSON，包含一个名为 `key` 的 32 字节随机十六进制密钥。起始适配器仍由 `--adapter-session` 固定，起始回调也仍绑定该会话。每个新会话取得单独回调身份，只有当前会话可提交原生回调。`/new` 和 `/clear` 新开会话，`/fork` 保留父会话历史。Router 先确认暂停派发，Worker 才准备、登记并启用新会话。确认新绑定后才恢复派发。原飞书消息重投返回原绑定回执，旧会话可用 `/resume 会话ID` 切回。

登记回执丢失时，按原操作号查回已登记会话。原生创建结果未到且没有确认的会话 ID 时，保留本次交接待核，不重发创建。重开后只恢复 Worker 已确认绑定的会话。离线备份需明确提供登记目录，恢复用原私有密钥重建回调身份。[自动登记工位的恢复与回退](router-rehearsal.md#codex-自动登记工位的恢复与回退)

复跑 `python3 -B -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_managed_native_commands_bind_without_manual_enrollment_and_restore`。使用已安装的原生 App Server，以及测试飞书消息和本地脚本模型。未使用现役账号或会话。[原生自动换绑证据](evidence/codex-managed-native-sessions.json) · [原生会话准备与显式接入证据](evidence/codex-native-thread-preparation.json)

DSH 的自动换绑使用同一套 Worker 交接。先在独立 DSH 宿主插件中配置 `managed_sessions_dir`，再用同一绝对路径启动 `dsh-adapter --managed-sessions-dir`。目录在宿主与适配器所在机器上保留，POSIX 权限为 `0700`。起始会话须已保存原生记录。Worker 启用相同的固定会话和私有 `--session-callback-key`。两端复用固定的 SDK 会话控制器。

`/new` 与 `/clear` 使用可选的 `new_session_selection`。该对象包含 `provider`、`model` 和可省略的 `effort`，省略整个对象时沿用当前会话的选择。原生分支保存父会话标识、已核的事件边界与当前模型和力度。创建或登记回执丢失时按原操作读回，只有原生记录可核时才激活。原会话在分支写入前已变化时，保留原会话并解除派发暂停。

复跑 `python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_managed_dsh_creates_and_forks_through_the_worker_and_recovers_a_lost_reply rewrite.test_dsh_native_loop.NativeLoop.test_dsh_preparation_reopens_the_exact_native_child_after_ipc_ack_loss`。DSH SDK 和 TLS 链路实跑，模型和飞书使用夹具。[自动换绑证据](evidence/dsh-managed-native-sessions.json) · [原生写入待核证据](evidence/dsh-native-preparation-recovery.json)

### Hermes 专用会话

将 `integrations/hermes_manager` 安装到专用测试 profile 的插件目录，令该环境能够导入 `rewrite`。插件的 `connection_file` 指向用户私有 JSON，含 Router 地址 `url`、该插件的 `token`、插件数据库路径 `database`、指定会话 `session_id`，私有 CA 可用 `ca_file`。八个管理工具在指定会话生效。

已有宿主授权的独立 LOCAL 会话可额外配置 `input_session_key`，取同一会话已登记的原生路由键。插件检查自身所属 profile、当前宿主、原会话 ID、路由键和宿主现有的投递授权。接收方通过 Hermes 插件输入接口递交，从 Hermes 的原生输入台账核对生产者、消息正文和回执。附件以经校验的 Router 引用交给会话，查看文件时用 `router_info(message_id, attachment_id)`。未知回执保留在插件队列的 `failure` 字段；重试只查原生记录。Notify 队列维持原顺序，其他 Event 可继续接收。显式关闭或卸载插件时停止接收。

Router 拉取与原生输入分别运行。Router 断开时，已在插件入箱的文本和已核实的原收件凭据可继续交付或查回，Notify 仍以原生会话连续空闲满 10 秒为准。附件元数据首次核实后随原投递保留。还缺附件数据的消息等待 Router，原消息号不变；已确认失败的单次连接尝试会短暂退避。同一工位升级时保留插件队列、原生适配器账本及附件缓存，旧库中尚未保存的附件元数据需要等 Router 恢复。[离线收件实测](evidence/hermes-router-outage.json)

隔离复跑使用 `python3 -m rewrite.hermes_check --host-input --hermes-source <源码仓> --hermes-python <带依赖的测试Python>`。探针准备全新的 profile、测试授权和临时 Router，实际运行插件及会话输入台账，模型响应由本地测试函数生成。客户端只开放 TUI/CLI、缺少插件输入授权或会话不匹配时，宿主收件保持未确认。

该测试 profile 可另配 `notice_chat_id` 和 `notice_feishu_file`，前者是明确授权的飞书会话，后者指向用户私有的 SDK 凭据配置路径，包含 `app_id` 和 `app_secret`。需要同一环境安装飞书 SDK。Router 消息持久入箱时，插件只排一条来源和等级的飞书提示，正文仍走 Hermes 原生输入。飞书失败不改变原生输入状态。提示使用同一标识重试，超出回执可安全重试的窗口后保留待核。绑定变化不会转发旧提示。复跑用 `python3 -m unittest -v rewrite.test_manager_notices`。

### 邮件和游戏聊天来源

在独立环境运行 `python3 -m rewrite.feeds mail --config <邮箱测试配置>` 可只读采一次。仅属当前用户的配置包含 database、host、user、password，可选 port、folder 和 ca_file。首次在邮箱当前 UID 建立基线，后续保存发件人、主题和原来源规定的 200 字正文预览。组合后的信息超过 200 字时，消息携带完整预览文本附件。切换邮箱或文件夹时使用新库。

游戏来源用 `python3 -m rewrite.feeds game --config <游戏桥测试配置>`，读取显式设置的 origin 和 database，可指定 player。客户端读取 /state，再带原序号订阅 /stream，连接结束即退出。每次重开均从上次持久位置继续。旧游戏桥缺少启动标识，只能以序号下降识别重开。读不到稳定序号时停止采集，保留已确认的位置。

两个采集器可在 Router 离线时工作。另用 `python3 -m rewrite.service trigger --db <来源库> --name email send --connection <Router测试连接>` 发送已留档的信息；游戏使用同一命令并将名称换为 mc_chat。拿到 Router 业务回执后移出待发送列表。这里只提供显式启动入口。

迁移旧采集器时，用 `rewrite.feeds` 的 `--import-legacy-cursor` 把经核对的游标导入新来源库。这一步离线完成。邮件核对原账号与文件夹，首次采集再验证 UIDVALIDITY；游戏核对同一桥接地址与连续运行的实例。遇到旧位置无法确认时保留待核。见[旧采集来源交接](router-rehearsal.md#旧采集来源交接)。

已绑定的适配器在观察到原生题窗后，将原会话、回合、原生题号、题面和选项提交至 `POST /v1/worker/native-question`。平台通过已配置的应用、训练员、私聊和原卡 ID 核对真人回调后将答案持久排队。只有原宿主返回与原题、原回合及答案一致的关闭凭据，Worker 才发布已回答事件。DSH 计时题在超时后可从原卡续答，支持选择、自定义、跳过、批量题和多选。授权和计划审批使用各自的原生入口。溪流的工单问题和答复走 W05/A09，飞书上可读。

独立 DSH 工位可在宿主插件配置中显式启用 `relay_foreground_questions: true`，让公开的原生 `ask_user_question` 在前台等待时也出现飞书题卡。先更新 Worker、飞书投影及适配器到同版本。原客户端和飞书共用同一次原生请求，先被宿主采纳的答案生效。前台答案凭原生工具结果更新原卡，超时仍可在同一张卡续答。宿主取消原回合时，回合结束记录使原卡显示已关闭、未答。权限请求、计划审批和无法唯一对应原工具调用的题保留在原生端。选项默认关闭。迁移测试工位时一并保留原生会话日志、适配器库及宿主私有 runtime 目录内的 `question-*.open` 记录，文件只含原生调用指纹。

复跑 `python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_foreground_question_uses_one_native_answer_and_one_original_card`。真实 DSH SDK 和会话日志实跑，答题客户端、模型和飞书用本地测试替身。[前台题回执](evidence/dsh-foreground-question-relay.json)

若原本没有原生题窗，固定会话里的 Agent 可用 `worker_ask_trainer` 发独立公开题卡。工具接受一至三道题，每题可给选项，也可留空收文字。返回 `queued_for_trainer` 表示 Worker 已登记，发卡时不启动宿主回合。原卡通过既有训练员身份校验收取选择、自填及跳过。答复在原固定会话空闲且无正式持单时，作为新的原生输入交回。Worker 持久保存原卡号、原回合和答复；只有查到相符的原生收件后，才把原卡更新为已回答。明确未开始的忙碌拒绝可以重试，确认不了是否写入时只查原号。正式工单仍用 `worker_ask` 问溪流。

复验时保留 Worker、宿主适配器和平台状态库。新开独立 Codex App Server 会话时可登记 `worker_ask_trainer`，已绑定现役 Desktop 会话需单独验其工具加载。DSH 插件使用同一工具表。复跑独立题卡证据所列两项测试，模型和飞书均为本地测试替身。[独立题卡证据](evidence/standalone-trainer-questions.json)

### 飞书入口

平台配置中的 `worker_name` 用于飞书卡片标题。宿主适配器以自身身份向 Worker 的 `POST /v1/worker/display` 交已观测的公开事件，携带稳定事件号、目标会话和回合。每段增量文字仅发新增部分；独立的最终答复事件才显示为「回复」。工具事件仅接受工具名和用于展示的预览文本。查询或抄送都不会写正式完工报告。

独立平台入口 `python3 -m rewrite.feishu_client --config <专用测试配置>` 使用飞书官方 Python SDK。配置是仅属当前用户的 JSON，显式提供 `app_id`、`app_secret`、`trainer_open_id`、`chat_id`、平台队列的 `database` 和指向 Worker 的 `worker_connection`。普通私聊及其文件保存在 Worker 端，断开 Router 仍能投递。使用配置前需要单独的测试应用和会话。无需账号即可运行 `rewrite.test_feishu_inbound` 或官方事件解析复验 `rewrite.feishu_sdk_check`。[平台消息资源说明](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/guide/faq) [飞书 Python SDK](https://github.com/larksuite/oapi-sdk-python)

平台进程对获取长连接地址的 HTTP 请求设置 15 秒的连接和读超时，触发后由 SDK 按飞书下发的间隔重试。有限重试耗尽时记录收件不可用，等待至少 30 秒且不短于平台下发的重连间隔，再由同一 SDK 实例开始下一轮。明确下发零次重试时记为停止收件。握手、收件和回执继续使用官方 SDK。该接入已用 `lark-oapi==1.7.5` 验证，升级 SDK 后复跑以下故障场景再切换。[SDK 重连实现](https://github.com/larksuite/oapi-sdk-python/blob/v1.7.5/lark_oapi/ws/client.py)

在带飞书 SDK 的测试 Python 中运行 `python3 -B -m rewrite.feishu_reconnect_check`，可断开临时 WebSocket 的底层 TCP，观察 SDK 重新取地址并建立新连接。加 `--stall-discovery` 可让首次重连的取地址请求一直没有响应，确认同进程会再次取址。加 `--stall-discovery --raw-sdk` 复跑原 SDK 的三秒阻塞对照。旧事件重发后仍只有一条本地输入，新事件照常交给 Worker。无需云端账号，服务端和原生宿主均由隔离测试提供。[断连记录](evidence/feishu-sdk-discovery-timeout.json)

同一命令加 `--exhaust-retries` 模拟两轮重连各遇一次 HTTP 503，确认记录两次暂停，同进程重连，旧事件保留断线前已取得的 Worker 收件凭据。加 `--exhaust-retries --raw-sdk` 看原 SDK 停止取址的对照。加 `--platform-stop` 验证平台给出零次重试时没有再次拨号，日志明确显示停止收件。夹具缩短等待间隔，真实平台另验。[耗尽重试记录](evidence/feishu-sdk-retry-exhausted.json)

飞书平台进程的本地队列遇到暂时数据库锁或无法核对的上游响应，会保留入箱和卡片游标并继续处理。Worker 回执缺字段时先保留待核，沿用同一消息号查回。

### 独立宿主命令审批

在隔离 Router 的身份配置中，为指定测试工位单独登记 `approver/<工位ID>`。状态目录由 Router 运行账号独占。Adapter 显式启用 `--relay-command-approvals` 后，可以转交宿主本来就会发出的普通命令审批。宿主的沙箱和提审策略由工位所有者按原方式配置。工单管理人、Worker、飞书身份与其他工位的审批人无权替这条请求作决定。

下列命令使用获准的审批人连接文件。先核对 `show` 的会话、工作目录、环境和完整命令，再使用同条记录的审批号与请求摘要。命令原文只可在审批入口查看。C01 中的 `approval.requested` 只给该审批身份，正文仅含工位与审批号，按 C02 确认收件。

```sh
python3 -m rewrite.service approvals --connection "$APPROVER_CONNECTION" --worker-id "$WORKER_ID" list
python3 -m rewrite.service approvals --connection "$APPROVER_CONNECTION" --worker-id "$WORKER_ID" show "$APPROVAL_ID"
python3 -m rewrite.service approvals --connection "$APPROVER_CONNECTION" --worker-id "$WORKER_ID" decide "$APPROVAL_ID" --request-digest "$REQUEST_DIGEST" --decision cancel
```

决定包括宿主实际给出的单次 `accept` 或 `cancel`。已登记决定显示 `decided`，只有同一原生请求关闭后才显示 `native_closed`。关闭回执证明原审批请求结束，命令是否执行仍看原生回合。决定超时或 Worker 会话已变时拒绝提交新决定。超过旧系统的十分钟期限，原生请求支持取消且仍在等待时，转发一次取消并注明来源为超时。独立 Codex App Server 已实测单次允许和取消。重复允许沿用原回执，原生回合只有一次测试目录内的 `pwd`。[原生测试证据](evidence/codex-app-server-approval-accept.json)

独立 Codex 文件变更另用 `--relay-file-approvals` 启用。审批 CLI 的 `show` 返回 `kind=file_change`、绝对路径、操作和完整原生差异，包含本次变更的请求摘要。差异只保存在 Router 的私有审批库，由该工位的审批身份读取。C01 仍只通知工位与审批号。适配器匹配同一会话、回合和原生条目后才提交，缺少完整原生差异或请求包含目录授权时，按原请求号取消。只允许批准本次或取消。审批仍由原生宿主执行。原生应用授权、网络和额外权限请求待核。复跑 `python3 -B -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_native_file_approval_binds_to_exact_changes_and_rejects_missing_or_broad_requests`。[实测](evidence/codex-app-server-file-approval.json)

可取消的 Codex 请求从收到原生审批起使用本地十分钟时限。即使中间请求没有响应，适配器也按原生请求号取消一次，超时后收到的 Router 决定不会送给宿主。收到原生关闭凭据后，恢复连接便按同一审批号写入 `native_deadline`。Router 原有的操作人决定仍可查询。首次登记未到达时，Router 直接补记已关闭记录，补记和迟到登记均不会发审批通知。缺少原生关闭凭据保持待核。分步升级先更新 Router 和 Worker，再更新 Codex 适配器，使三段都能接收这种关闭记录。复跑 `python3 -B -m unittest -v rewrite.test_service.Service.test_native_deadline_closes_unregistered_request_without_opening_approval rewrite.test_codex_app_server.CodexAppServer.test_native_approval_deadline_survives_lost_intake_and_late_router_accept`。[断联实测](evidence/codex-approval-router-outage.json)

启用 Codex 命令或文件审批的适配器在向 Worker 登记前，把审批号、原生会话、回合、条目与请求摘要写入自己的数据库。收到原生关闭通知后，另存该请求号与曾尝试发送的单次决定。命令、路径和差异仍留在原有路径。适配器重开后先从原绑定 Worker 查找 Router 已保存的关闭回执。如果 Router 已登记原请求但尚未关闭，且适配器保存了确切的原生关闭，便按同号补交。命令和差异继续由 Router 使用原登记正文核对，回包只带身份和摘要。只以关闭回执消除待核。原生关闭未保存、Router 从未收到登记或原绑定无法核实时，继续待核。恢复无需重发原生决定、重开回合或重发审批通知。分步升级先更新 Router、Worker，再更新 Codex 适配器。[冷重开实测](evidence/codex-durable-approval-resolution.json) · [离线换绑](router-rehearsal.md#重做版五组件的离线恢复)

DSH 在独立宿主插件的参数中显式配置 `relay_command_approvals: true`，沿用同一个审批 CLI。适配器只转接绑定 Agent 的原生 `bash` 请求，按同一原生回合找到完整工具参数。普通请求省略 `workdir` 或传相对路径时，适配器用宿主的当前工作目录和文件系统解析出实际绝对路径。审批记录保存该目录及原工具参数。转回允许前与执行前都重查目录。在原生执行管线中，只把获准的绝对路径传给这一次 Bash。目录变更使重查失败时该命令不执行。目录无法核实，或未取得同一次原生执行上下文时，交给宿主原生审批。原生沙箱策略独立执行。复跑 `python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_approval_pins_implicit_and_relative_native_working_directory`。[原生目录实测](evidence/dsh-approval-native-working-directory.json)

DSH 适配器先保留原请求和单次决定的发送意图。只有在原会话的持久历史读到同号 `approval/asked` 与 `approval/decided`，才确认请求关闭。重开时使用原适配器库、原 DSH 会话和原工位，待核关闭可按该记录补回。测试执行的允许命令只有隔离工作目录内的 `pwd`。

DSH 未收到 Router 的登记回执而宿主已经取消、拒绝或确认不可用时，恢复直接按原号补闭合。审批 CLI 可在 `native_proof` 看到同一回合的两个原生序号和实际结局。Router 从未收件时补闭合不会发审批通知。Router 若先登记了操作人决定，则保留该决定和原生结局。原生证明缺失、对不上，或未确认登记却出现允许时，保留待核。分步升级先更新 Router 和 Worker，再更新 DSH 适配器。复跑 `python3 -B -m unittest -v rewrite.test_service.Service.test_dsh_no_notice_closure_requires_original_non_grant_proof rewrite.test_dsh_native_loop.NativeLoop.test_dsh_unacknowledged_approval_recovers_without_a_late_notification`。[断联恢复](evidence/dsh-approval-router-outage.json)

### macOS 独立维护入口

`python3 -m rewrite.service host-ops --manifest <登记表> --db <独立维护库> --identities <维护身份表> --cert <证书> --key <证书私钥>` 启动独立 HTTPS 服务。默认绑定本机。将登记表、身份文件和维护库放在各自适用的私有目录中。登记表只列已授权托管的当前用户 LaunchAgent，格式为 `{"router":{"label":"<准确标签>","plist":"<绝对路径>"}}`，分别核对已加载的 plist 路径、程序及参数。同一个标签只可登记一次。此服务独立于 Router，无默认目标、无定时巡检。

身份表使用独立令牌。`host-observer` 可查全部登记项和回执，`host-maintainer/<服务名>` 只可查和操作这一项。旧 Router、普通 Worker、网关和平台凭据不具备维护权限。由外部获准的维护账号使用这些接口。

| 接口 | 判据 |
| --- | --- |
| `GET /v1/ops/services` 或 `GET /v1/ops/services/{name}` | 返回已核对的服务、PID、启动时间和 `generation`。正在换代、已有任务的加载参数不匹配或读不到状态时返回待核。 |
| `POST /v1/ops/services/{name}/restart` | 传 `request_id`（UUID）、`expected_generation` 和 `reason`。`202` 只表示已登记。重复使用原请求号可查原回执。 |
| `GET /v1/ops/services/{name}/restarts/{request_id}` | `process_replaced` 表示原 PID 已退出且查到当前新进程。`process_started` 表示已加载的停机项恢复。`not_attempted` 表示操作前状态已变。`unconfirmed` 要按原号核查，重开维护服务不会补做。 |

进程回执后再查服务自己的接口和状态数据。已加载的 LaunchAgent 重启沿用 launchd 当前配置。维护服务不能为 Desktop 子进程、未登记的网关或其他系统代做启停。生产接管需另核安装归属、明确维护授权及回退路径。复跑 `python3 -B -m unittest -v rewrite.test_host_ops`，测试使用三个临时用户 LaunchAgent，完成后卸载。[隔离回执](evidence/host-ops-launchd.json)

### 跨系统验收

开发机需 Python 3.12 或更新版本、Git、OpenSSL 和已运行的本地 Linux Docker 引擎。先准备含 Python 3.12+、Git、tar、OpenSSL 的 Linux 测试镜像。此次实测使用官方 `python:3.13-bookworm` 的 Linux arm64 manifest `sha256:0a1254df0519003d33fd4ab8ad26aff1154e50b3c93aa5ec978e83bf83ba084c`。各引擎对已安装镜像的 ID 表示可能不同，`--image` 使用本机 `docker image inspect --format '{{.Id}}' <镜像>` 的完整结果。

```sh
python3 -m rewrite.linux_check --repository . \
  --revision 2025c3001d1019016b832ff7d578eb1aa56c7899 \
  --image sha256:0a1254df0519003d33fd4ab8ad26aff1154e50b3c93aa5ec978e83bf83ba084c \
  --socket "$HOME/.colima/default/docker.sock"
```

脚本从指定提交制作发行包，两端各自安装。Worker 在开发机运行，Router 和宿主适配器在全新 Linux 容器里运行，各用独立的身份和 HTTPS 端口。两个端口只映射到开发机回环地址。容器以普通用户运行，根目录只读，临时盘保存测试数据库。结束时清理测试容器、专用网络及一次性身份。测试重投后独立读回宿主输入数，核对签发者校验、未登记身份、W03 持单、回合结束与 W04 待验收。Agent 与宿主为夹具。两种系统运行在一台物理机上。原始记录见[Linux 跨系统验收记录](evidence/linux-cross-os.json)。

#### Windows 安装与回退

在 Windows 上将身份 JSON、TLS 私钥和状态库放入同一服务身份已保护的独立目录。使用 Windows ACL，只给该运行身份、SYSTEM 和受信任的本机管理员所需访问。服务用已打开的文件句柄检查身份、连接和其他私有配置的所有者及 ACL。配置向其他普通用户授予读取或写入时，服务拒绝启动。权限检查只读。TLS 私钥和状态库的保护由目录 ACL 承担。[Win32 文件权限读取接口](https://learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo)

安装包按 `rewrite.release build/install/verify` 校验提交、归档摘要和逐文件字节。Windows 原生验收从已校验的安装包执行，临时库放进带中文与空格的目录，进程只在本机回环监听。复跑用受保护的测试目录、Python 3.13、Git 和 OpenSSL，命令见 [Windows 测试入口](../.github/workflows/rewrite-windows.yml)。旧版与验证版各自启动，核对 Router 的原队首和 Worker 的原收件，回退后无重复。宿主和模型用夹具，实际 Windows Desktop 启动与真实平台仍待验。[Windows 实测](evidence/windows-release-rollback.json)

Windows 另从官方发布包运行 [独立 Codex App Server](desktop-check.md#windows-独立-app-server)，实测原生工具经 W04 交成果、原收件查回、只读派发取消及转派，并核对 Windows profile ACL 和进程回收。模型、飞书和接替工位用夹具。旧新版 Router 和 Worker 回退继续每轮验证。

#### Windows 独立 DSH 固定会话

只在独立工位启用。宿主插件设置 `local_tls: true`，适配器启动参数加 `--local-tls`。两端配置同一个固定会话及绝对运行目录。该目录须位于本机受保护的路径下，目录及下列三个文件仅允许运行身份、SYSTEM 和受信任的本机管理员访问。插件用本机 Windows PowerShell 只读检查权限，按设备既有脚本执行策略运行。

- `dsh-link.json` 填两个不同且未占用的端口，字段为 `host_port` 和 `worker_port`，范围为 1024–65535。两个监听都绑定 `127.0.0.1`。
- `dsh-link.crt` 与 `dsh-link.key` 是该工位专用的自签证书及私钥。证书须含 IP SAN `127.0.0.1` 及服务端和客户端用途。两边都校验对端证书。不要复用 Router、飞书或登录凭据。证书、私钥、端口变更时按同一工位的双端配置重新启动测试实例。
- 此入口覆盖固定会话的文本收件和原生工具。托管会话、命令审批、前台题转接、只读派发清理、工单容器与图片入站仍使用各自已有 Unix 套接字接入。显式启用未支持的插件选项会被拒绝。

复跑时先为官方 Windows DSH 包校验 SHA-512，再解包到临时目录。用已检查权限的独立临时目录运行协定用例，环境变量 `MOWULINK_TEST_DSH_BINARY` 指向解包所得的原始应用可执行文件。运行 `python -B -m unittest -v rewrite.test_dsh_native_loop.LocalTlsLoop`。CI 的下载校验和完整步骤见 [Windows 验收入口](../.github/workflows/rewrite-windows.yml)。用例实际运行包内 DSH AgentLoop、原生持久化及工具执行，通过真实 Router 和 Worker 发回独立工单。模型与操作人使用本地测试驱动。已在 [Windows Server 2025](evidence/windows-dsh-native.json) 的官方包和 [macOS](evidence/dsh-local-tls-macos.json) 的已装包验证。已核宿主停机时保留原收件待核。用同一目录和端口重开原生宿主后，可查回同一收件，保留一条输入且无新模型请求。实际 Desktop 窗口、真实模型和飞书另验。

### DSH 会话用量

训练员在飞书私聊发送 `/context` 可查询绑定 DSH 会话最近一次模型回包的路由、回合、步骤、输入与总用量。窗口取自该回包之前同一路由的原生请求记录。原生端缺某一数值时按缺项显示，指令本身不向模型发起推理。宿主适配器的 `GET /v1/host/context` 只允许本工位读取。
