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

首段的 Agent、Codex/Hermes 宿主、飞书输入与验收人均为夹具。真实客户端、飞书卡片、Trigger/附件、候选材料、转派与取消、完整掉线入池、询单和提问后续接入。各入口只接受本切片明示的业务动作。

第二段已用本机 Codex Desktop 独立聊天验收正式工单输入，复跑方式和原生回执见 [Desktop 接入实测](desktop-check.md)。上述 `rewrite.devloop` 继续提供可离线运行的夹具回环。

后续已扩展 Router 生命周期、消息附件、问答、同伴私聊及插件。以[全量替换进度](replacement-progress.md)核对各项版本和实测边界。

### 同伴私聊与附件

接收工位查询指定消息时，`local_pending` 列出投递或确认失败的阶段、错误、尝试次数和下次重试时间。未拿到可靠凭据时继续沿用原投递号，按单条从 5 秒退避至最多 5 分钟。重启后沿用同一记录，其他同伴消息和工单可继续处理。

从旧 Router 带入的已确认或已放弃私聊由原参与人按来源 UUID 查询。Router 入口为 `GET /v1/history/{source_id}/peers`，宿主经绑定的 Worker 访问 `GET /v1/worker/history/{source_id}/peers`。列表用 `limit` 和 `next_cursor` 分页。消息详情路径再附原消息号，长正文按 `next_part` 逐段读取。宿主可向详情路径下的 `/file` 提交 `{"position":0}`，取得本工位媒体引用供查看。历史记录只有原 Router 确认时间或放弃时间，迁移过程不产生新的投递或宿主回执。见[迁移演练](router-rehearsal.md#复跑)。

同伴消息的发件和收件宿主可通过各自 Worker 的 `GET /v1/worker/peers/{message_id}` 查回交接进度。`accepted_at` 记录 Router 收件，`worker_received` 表示接收 Worker 已确认入箱；`host_receipt_id` 有值表示接收 Worker 已核指定宿主的原生凭据并向 Router 报备。宿主会话及原生收件详情留在接收工位。上述回执都不代表对方已经回复。

同伴附件由发送宿主先交给本工位的媒体入口，再向 `POST /v1/worker/peer` 提交 `to_worker`、`message_id`、可选的 `text`、`reply_to` 和本地 `attachment_ids`。需要区分图文时按位置给 `attachment_kinds`，默认按普通文件。Router 核对发送工位持有附件，收件工位以自己的身份分块取得并校验，再从本地媒体入口交给自己的宿主。该接口返回 Router 收件凭据，原生收件凭据记录在接收 Worker 的同伴历史中。

### 飞书私聊的异常回执

绑定的训练员私聊发生格式、附件或收件问题时，平台进程在原消息下回复状态卡。平台已尝试而回执丢失时，沿用原 UUID 和卡片正文，首次尝试满 55 分钟后标记 `receipt_unknown`，继续处理其他消息。等待中已查实且尚未发送的提示自动撤销。若已提示待核，查到宿主凭据后再发一条恢复确认。消息的受理状态在 `platform_events`，平台回复的状态和消息号在 `input_notices`。[飞书回复消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/reply.md)

### DSH 固定会话与飞书卡片

在独立 DSH 测试配置加载 `integrations/dsh_host/index.mjs`，指定测试会话的 `session` 和仅当前用户可读写的绝对目录 `directory`。单独启动 `python3 -m rewrite.service dsh-adapter --help` 所示的适配器，将 `--session`、`--runtime-dir` 分别指向同一会话和目录。收图需宿主提供 `attachments` 服务；`--worker-connection` 指向适配器访问专用 Worker 的连接配置。固定会话插件中仅登记 Agent 业务工具，本体负责发送飞书卡片。

图文在同一次输入进入 DSH，宿主保留规范化图片和普通文件引用。适配器从 DSH 已持久写入的公开会话事件生成原生输入、过程输出、工具、图片与最终答复。工具预览仅包含可判明的命令动词。首次绑定以插件接入时的位置为起点；续跑通过原会话、位置和上一条记录摘要核对。在中断的回合中，已有回复草稿按过程输出显示。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk`，该套件使用本机 DSH SDK、临时工位及测试驱动，模型和飞书云端另验。

绑定 DSH 会话后，飞书 `/model`、`/effort` 显示会话当前选择、最近已记入原生日志的请求配置和宿主公开目录，十项一页。用 `next`、`prev` 或 `page 2` 等查看后续页。目录缺失会在卡片提示。同号重试沿用已生成的原卡。

绑定会话空闲时，用 `/model 提供方/模型` 切换路由，用 `/effort 强度` 选择当前路由支持的强度，或用 `/effort default` 随模型默认。两条命令也可带最近确认送达的菜单编号。宿主当前模型与力度菜单所示路由不符时，需重看菜单。宿主校验选项后保存该会话的原生记录，确认回执包含会话、选择和原生序号。若回执未到，原消息显示待核且不会重做切换。通过 `/model` 或 `/effort` 可查看此刻的状态。Worker 默认值同步及真实模型调用待验。

收到取消或转派时，如果原生会话仍在执行可核对的原派发回合，DSH 插件请求停止，保留其他待处理消息，再交控制请求。原生输入号、回合号或会话位置无法对准时，插件仅递交控制请求。没有清理凭据时，Worker 保留原派发的待核状态。

启用原生目标服务的 DSH 会话可用 `/goal` 查询、`/goal 目标正文` 新建、`/goal pause` 暂停、`/goal resume` 恢复。原生服务使用自己的默认轮次，已有未完成目标时拒绝替换。状态卡显示原生阶段、轮次和续跑授权。未确认的变更保留待核，重送同号不会再次修改。续跑由 DSH 宿主负责。

启用原生压缩服务的 DSH 会话支持 `/compact`。Worker 先保存指令，宿主空闲时开始压缩。回执丢失后只按原指令号查原生记录。宿主保存了摘要、替换记录及结束条目后，飞书才显示已压缩的条数。连续五分钟无法确认时提示待核。摘要正文保留在宿主。

DSH 会话可通过 `/resume` 查看宿主目录。插件配置 `catalog_sessions` 可额外添加获准展示的原生会话 ID，默认只查询当前绑定。列表支持 `next`、`prev`、`page 页码` 和 `search 标题或会话号`。会话记录仅供浏览，换绑功能仍待接。

### 隔离安装与升级

在受信任的构建机选定一个完整提交号。以下命令可从源码工作区运行。构建只取该次提交中受 Git 跟踪的 `rewrite/`，包括该版的宿主入口、说明和测试。将归档传给测试机时，经独立可信渠道核对提交号与输出的 SHA-256。安装目标取一个尚不存在的版本目录，运行状态和连接配置使用单独的私有目录。

```sh
python3 -m rewrite.release build --repository . --revision "$REVISION" --destination ./build
python3 -m rewrite.release install --archive ./build/mowulink.tar --revision "$REVISION" --sha256 "$SHA256" --destination "./releases/$REVISION"
python3 -m rewrite.release verify --archive ./build/mowulink.tar --revision "$REVISION" --sha256 "$SHA256" --destination "./releases/$REVISION"
(cd "./releases/$REVISION" && python3 -B -m rewrite.service router --help)
```

目标机器需要 Python 3.11 或以上。安装好的版本在自己的目录下启动，`-B` 禁止写入字节码缓存。Router、Worker、Trigger 使用标准库。飞书需要独立 Python 环境中的官方 SDK，DSH 与 Hermes 使用宿主提供的插件依赖。逐字节核验会识别未登记的文件及已改内容。保留已验证的前一版本目录可供回退。

升级先停隔离写者，按[离线恢复](router-rehearsal.md)采集组件清单和状态摘要。在新状态目录恢复副本，仅让新包在该副本上启动。先核原持单、投递凭据、未送消息和附件，再引入新工作。返回原版本前停新写者，通过 `state_bundle rollback` 核对试用期间的写入；新写入先逐笔对账。原生宿主绑定、认证及平台投递记录由相应所有者另核。

### Hermes 专用会话

将 `integrations/hermes_manager` 安装到专用测试 profile 的插件目录，令该环境能够导入 `rewrite`。插件的 `connection_file` 指向用户私有 JSON，含 Router 地址 `url`、该插件的 `token`、插件数据库路径 `database`、指定会话 `session_id`，私有 CA 可用 `ca_file`。八个管理工具在指定会话生效。

已有宿主授权的独立 LOCAL 会话可额外配置 `input_session_key`，取同一会话已登记的原生路由键。插件检查自身所属 profile、当前宿主、原会话 ID、路由键和宿主现有的投递授权。接收方通过 Hermes 插件输入接口递交，从 Hermes 的原生输入台账核对生产者、消息正文和回执。附件以经校验的 Router 引用交给会话，查看文件时用 `router_info(message_id, attachment_id)`。未知回执保留在插件队列的 `failure` 字段；重试只查原生记录。Notify 队列维持原顺序，其他 Event 可继续接收。显式关闭或卸载插件时停止接收。

隔离复跑使用 `python3 -m rewrite.hermes_check --host-input --hermes-source <源码仓> --hermes-python <带依赖的测试Python>`。探针准备全新的 profile、测试授权和临时 Router，实际运行插件及会话输入台账，模型响应由本地测试函数生成。客户端只开放 TUI/CLI、缺少插件输入授权或会话不匹配时，宿主收件保持未确认。

该测试 profile 可另配 `notice_chat_id` 和 `notice_feishu_file`，前者是明确授权的飞书会话，后者指向用户私有的 SDK 凭据配置路径，包含 `app_id` 和 `app_secret`。需要同一环境安装飞书 SDK。Router 消息持久入箱时，插件只排一条来源和等级的飞书提示，正文仍走 Hermes 原生输入。飞书失败不改变原生输入状态。提示使用同一标识重试，超出回执可安全重试的窗口后保留待核。绑定变化不会转发旧提示。复跑用 `python3 -m unittest -v rewrite.test_manager_notices`。

### 邮件和游戏聊天来源

在独立环境运行 `python3 -m rewrite.feeds mail --config <邮箱测试配置>` 可只读采一次。仅属当前用户的配置包含 database、host、user、password，可选 port、folder 和 ca_file。首次在邮箱当前 UID 建立基线，后续保存发件人、主题和原来源规定的 200 字正文预览。组合后的信息超过 200 字时，消息携带完整预览文本附件。切换邮箱或文件夹时使用新库。

游戏来源用 `python3 -m rewrite.feeds game --config <游戏桥测试配置>`，读取显式设置的 origin 和 database，可指定 player。客户端读取 /state，再带原序号订阅 /stream，连接结束即退出。每次重开均从上次持久位置继续。旧游戏桥缺少启动标识，只能以序号下降识别重开。读不到稳定序号时停止采集，保留已确认的位置。

两个采集器可在 Router 离线时工作。另用 `python3 -m rewrite.service trigger --db <来源库> --name email send --connection <Router测试连接>` 发送已留档的信息；游戏使用同一命令并将名称换为 mc_chat。拿到 Router 业务回执后移出待发送列表。这里只提供显式启动入口。

已绑定的适配器在观察到原生题窗后，将原会话、回合、原生题号、题面和选项提交至 `POST /v1/worker/native-question`。平台通过已配置的应用、训练员、私聊和原卡 ID 核对真人回调后将答案持久排队。适配器保留关闭意图，只有原宿主返回与原题、原回合及答案一致的关闭凭据，Worker 才发布已回答事件。DSH 已接入原生计时题超时后仍可作答的单题。选择、自定义和跳过均需 DSH 持久记录原生答复才会更新原卡。DSH 端先答时，原卡显示原生答案，冲突的飞书回调不能覆盖它。阻塞题、多题和多选仍由 DSH 原界面处理。授权和计划审批使用各自原生入口。溪流的工单问题和答复继续走 W05/A09，飞书上可读。

平台配置中的 `worker_name` 用于飞书卡片标题。宿主适配器以自身身份向 Worker 的 `POST /v1/worker/display` 交已观测的公开事件，携带稳定事件号、目标会话和回合。每段增量文字仅发新增部分；独立的最终答复事件才显示为「回复」。工具事件仅接受工具名和用于展示的预览文本。查询或抄送都不会写正式完工报告。

独立平台入口 `python3 -m rewrite.feishu_client --config <专用测试配置>` 使用飞书官方 Python SDK。配置是仅属当前用户的 JSON，显式提供 `app_id`、`app_secret`、`trainer_open_id`、`chat_id`、平台队列的 `database` 和指向 Worker 的 `worker_connection`。普通私聊及其文件保存在 Worker 端，断开 Router 仍能投递。使用配置前需要单独的测试应用和会话。无需账号即可运行 `rewrite.test_feishu_inbound` 或官方事件解析复验 `rewrite.feishu_sdk_check`。[平台消息资源说明](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/guide/faq) [飞书 Python SDK](https://github.com/larksuite/oapi-sdk-python)
### 跨系统验收

开发机需 Python 3.12 或更新版本、Git、OpenSSL 和已运行的本地 Linux Docker 引擎。先准备含 Python 3.12+、Git、tar、OpenSSL 的 Linux 测试镜像。此次实测使用官方 `python:3.13-bookworm` 的 Linux arm64 manifest `sha256:0a1254df0519003d33fd4ab8ad26aff1154e50b3c93aa5ec978e83bf83ba084c`。各引擎对已安装镜像的 ID 表示可能不同，`--image` 使用本机 `docker image inspect --format '{{.Id}}' <镜像>` 的完整结果。

```sh
python3 -m rewrite.linux_check --repository . \
  --revision 2025c3001d1019016b832ff7d578eb1aa56c7899 \
  --image sha256:0a1254df0519003d33fd4ab8ad26aff1154e50b3c93aa5ec978e83bf83ba084c \
  --socket "$HOME/.colima/default/docker.sock"
```

脚本从指定提交制作发行包，两端各自安装。Worker 在开发机运行，Router 和宿主适配器在全新 Linux 容器里运行，各用独立的身份和 HTTPS 端口。两个端口只映射到开发机回环地址。容器以普通用户运行，根目录只读，临时盘保存测试数据库。结束时清理测试容器、专用网络及一次性身份。测试重投后独立读回宿主输入数，核对签发者校验、未登记身份、W03 持单、回合结束与 W04 待验收。Agent 与宿主为夹具。两种系统运行在一台物理机上。原始记录见[Linux 跨系统验收记录](evidence/linux-cross-os.json)。
### DSH 会话用量

训练员在飞书私聊发送 `/context` 可查询绑定 DSH 会话最近一次模型回包的路由、回合、步骤、输入与总用量。窗口取自该回包之前同一路由的原生请求记录。原生端缺某一数值时按缺项显示，指令本身不向模型发起推理。宿主适配器的 `GET /v1/host/context` 只允许本工位读取。
