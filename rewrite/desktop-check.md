# Codex Desktop 独立会话收件验收

在打开的本机 Codex Desktop 内，测试驱动调用宿主提供的 `create_thread`、`send_message_to_thread` 和 `read_thread`。新建无项目的测试会话后，驱动固定其本机宿主号、会话号与工作目录，并核对它与生产 Worker 当前绑定不同。Worker 经 W03 接管后，适配器才发出正式输入。Python 进程只能请求读取固定会话或投递工单，无法指定另一个会话；运行凭据由 Desktop 保管。

原生输入以 Desktop 委托项出现在会话中。适配器核对该项的工具来源、发件会话、目标会话、投递号与完整工单内容，再返回原生输入号和回合号。查回使用宿主自己的会话记录。发送调用即使已返回成功，也须取得上述证据才记作已收。发生丢回包、读取不全或超时，沿用原投递意图，只查回。相同投递号对应两项、会话不同或内容摘要不符均报冲突。

## 本机实测

| 观测 | 结果 |
| --- | --- |
| 独立会话 | `01a21527-24e0-7380-9a9f-dec0fddf1549`，测试前仅有一个初始化回合 |
| 隔离工单 / 派发 | `82633282-9eb0-4791-b41c-1a770688eda2` / `e3181553-0d38-4015-8ee7-4f23e5609c5d` |
| 投递号 | `238ebaba-66f7-4a37-b704-4654ddd22417` |
| Desktop 回合 / 输入项 | `01a21529-79cc-73d2-908d-3213471857cd` / `fco_01a21529-79cc-73d2-908d-322db32d76d2` |
| 原生最终回复 | 「独立宿主已收到工单」 |
| 同帧重投后 | 底层发送共 1 次；目标会话共 2 个回合，其中正式工单输入共 1 条。回读可按原投递号定位同一输入项。 |
| 工单状态 | W03 已接管。原生回合结束后持单副本仍是原派发，Router 为 `working`，正式报告为空。 |

本次真实运行的是本机 Desktop 及模型、重做版 Worker、Agent 适配器、临时 HTTP Router 和 SQLite。建单来自测试驱动，空闲计时用可控时钟。未接入生产 Worker、飞书和真实 Hermes。真实模型只回复收件，此次未挂载真实会话的 W04 工具；W04 另由首段的 MCP 夹具回归覆盖。当前接入依赖运行在 Desktop 内的工具驱动；独立后台 Worker 自行启动和无人值守写入仍待验证。

本机只读核对的 Desktop 工具 `send_message_to_thread` 接收会话、宿主和纯文本，可附模型与力度选项。当前接入无法用这条工具发送原图。旧 Worker 在 Desktop 启动的同一个原生连接上另有 `localImage` 写入、同回合精确中止与题窗入口。旧实现要求合法的来源台账、目标绑定、原消息号及文件缓存校验，并使用旧工单号格式（参考其源码 `ae6117c8a3a8157ad1710496c2f7fc5de5f13dcb`）。重做版需对接自己的投递记录并在授权测试工位实测这条连接。

## 复跑

仓库根目录运行下列命令。需要 Python 3.11 或更高版本。

```sh
python3 -m unittest -v rewrite.test_mainline rewrite.test_codex_desktop
```

原有 14 项外，新增 5 项覆盖发送回执丢失、重启后只读查回、错误发件人或输入类型、截断或重复凭据、错误目标及引导消息。

实机复跑需要已打开的本机 Codex Desktop，并能调用上述聊天工具和 Worker 的只读绑定查询。在 Desktop 的工具执行环境运行下列代码，将工作目录改为本分支的实际绝对路径。本机驱动使用 macOS 的临时终端，Windows 尚未验收。每次复跑会新建一个测试会话、一张临时工单并消耗一个测试回复，临时 Router 使用随机回环端口和临时目录。

```js
const workdir = "/Users/kibyou/Workspace/OguriCap/worktrees/mowulink-formal-mainline";
const driver = await tools.exec_command({
  cmd: "cat rewrite/desktop_check.js", workdir, max_output_tokens: 3500
});
if (driver.exit_code !== 0) throw new Error("找不到本分支的 Desktop 驱动");
text(await eval("(" + driver.output + ")")(tools, workdir));
```

成功输出含 `status=verified`、两个原生标识、`native_inputs=1`、`send_calls=1` 和 `w04=null`。缺少完整证据会返回待核或错误。当前测试只读取至多十个回合、每回合六十四项，超过读取边界会保留待核。

## 独立 App Server 的 W04 协议验收

另启动 Desktop 随附的 Codex App Server，只监听当前进程的标准输入输出，使用空白配置和回环 HTTP 测试模型。原生线程加载 `worker_report`，同一正式派发从 Router 经 Worker 进入线程。按投递号填写原生 `clientUserMessageId`，从 `thread/read` 核对该标识、输入项、正文和回合。读历史暂不可用时保留待核，重复派发只查原历史。

测试模型发出两次工具调用。由真实 App Server 送出 `item/tool/call`，Worker 核对同一原生线程、回合与原派发，再走 W04。轮换线程号和回合号的回调分别被拒绝。两次合法调用返回同一正式回执，只产生一条待验收通知。原生历史只见一个工单输入，保留两个已完成的工具调用项。[原生试验记录](evidence/codex-app-server-scripted-w04.json)。

复跑 `python3 -m unittest -v rewrite.test_codex_app_server`，需要本机 ChatGPT Desktop 随附的 Codex 可执行文件。此验收连续通过 30 次。模型为本地脚本，无登录账户，Desktop 界面和真实模型 W04 仍待有授权的测试工位。

同一套件另在原生工具请求尚未回执时停止试验进程，清空适配器数据库中的宿主凭据，再由另一个 App Server 进程以 `thread/resume` 打开同一测试线程。原输入 ID、客户端投递标识和回合均可从宿主历史查回。重投没有产生第二次输入或模型请求，Router 仍持原派发，成果与待验通知为空。连续 20 次通过。[恢复试验记录](evidence/codex-app-server-native-reconnect.json)。

App Server 原生中止会核对指定线程、活跃回合和唯一原生输入。错误回合在原生 API 被拒绝，正确回合回读为 `interrupted`，随后查询取得原凭据。Router 取消请求仍待清理，Worker 仍持原派发。连续 15 次通过。[中止试验记录](evidence/codex-app-server-native-interrupt.json)。这段验证由隔离驱动请求原生中止，未验证 Router 取消自动触发宿主停止或资源清理。

已另用真正的 Router 取消验证适配器控制入口。原派发仍在执行时，Worker 收到取消会中止精确原生回合并交取消消息。会话忙于其他回合时，宿主拒绝将取消写入该回合，空闲后才能接收。同一时期的新正式输入也在写入前被拒。资源清理待核时 W04 取消确认仍被拒，工位保留原持单。两个场景各连续 12 次通过。[Router 取消记录](evidence/codex-app-server-router-cancel.json)。

专用只读工位可在 `codex-adapter` 加 `--read-only-assignments`。该模式使用只读沙箱，关闭审批、命令执行与原生目标续跑，并要求没有额外 MCP 服务。适配器在写入前记录原派发的读写边界。收到 Agent 的 `cancel_confirmed` 或 `transfer_confirmed` 时，控制回合仍在运行会返回 `native_cleanup_waiting`。Worker 保存原请求，等 App Server 提供完整的终止回合记录再交 W04。旧的无登记回合、受控回合历史不全，以及只读工位的工单或控制回合有工作区工具记录时，清理保持待核。

```sh
python3 -B -m unittest -v \
  rewrite.test_codex_app_server.CodexAppServer.test_native_readonly_assignment_cleanup_waits_for_control_end_and_preserves_new_holder
```

四个场景用了实际 App Server、隔离 Router 和本地模型。取消在原控制回合结束后确认。转派仅释放旧派发。带命令记录的原回合和 W04 请求后仍启动命令的控制回合均保持待核。可设置 `MOWULINK_CODEX_CLEANUP_EVIDENCE` 记录原生回合、策略及 Router 回执。[原生清理实测](evidence/codex-readonly-cleanup.json)

取消或转派到达时，原输入可能已进入原生会话，适配器却还没有记下凭据。适配器先按原投递号、正文和会话查回，再中止匹配的回合。尚未确认停止时只重试未写入的控制指令；原输入和已发控制输入仍只查回。另在控制回合结束、Router 仍待清理时关闭独立适配器，从原 profile、原 SQLite 和本地 TLS 重开，沿同一回合凭据交 W04。[独立服务和冷恢复回执](evidence/codex-readonly-cleanup-service.json)

App Server 指定回合的文字投影复用原 Worker 的飞书卡片。独立原生输入保留原空格和换行，最终答复单独成卡。只读取已结束回合，桥接投递、内部工具调用及参数不出卡。稳定事件号令重投返回同一回执。连续 12 次通过。[卡片试验记录](evidence/codex-app-server-native-display.json)。输入为隔离驱动，模型和飞书为夹具；常驻监听、原生图文和飞书云端另验。

独立 App Server 的图文与纯图使用原生 `localImage`。飞书夹具按原消息上传，适配器先核私有缓存的长度与摘要，再递交到固定会话。同条原文和图实际出现在模型请求，原生 App Server 添加的图片元数据单独核对。丢回执后按原客户端标识查回原生输入，两次试验输入分别只出现一条。重复 12 轮通过。[图片试验记录](evidence/codex-app-server-native-media.json)。目前只接已登记图像，模型、飞书为夹具。

## Windows 独立 App Server

Windows Server 2025 从 OpenAI 发布的 `0.171.0-alpha.13.2` 原生包启动 App Server。验收使用临时 profile、本地脚本模型和独立 Router、Worker。安装包在测试前后逐文件一致。一个原生输入产生两次 `worker_report` 调用，Router 只记一次成果和通知，状态为待验收。重开后按原投递号查回输入，原生会话未多执行。只读工位的取消和转派须等控制回合终止再交 W04。转派释放旧派发并保留新工位持权。另用无清理证据和非目标回合验证拒绝与保留。[Windows 原生证据](evidence/windows-codex-native.json) · [官方原生包](https://github.com/openai/codex/releases/expanded_assets/rust-v0.171.0-alpha.13.2)

Windows 适配器先核专用 profile 目录 ACL，在原生协议初始化前把该进程登记到独占 Job Object。用独立辅助进程验证适配器退出时，被登记的宿主及子进程一同停止。无需调整系统权限或共享客户端配置。[Windows Job Object](https://learn.microsoft.com/windows/win32/procthread/job-objects)

本地复跑使用受保护的测试临时目录，将 `MOWULINK_TEST_CODEX_BINARY` 指向已校验的 Windows 包中 `bin\codex.exe` 的绝对路径。完整命令在 [Windows 测试入口](../.github/workflows/rewrite-windows.yml)。此回归覆盖独立 App Server。Windows Desktop 插件入口、线上模型、真实飞书及 Windows 容器工作区另验。

## 隔离可写工位

单独准备获准的测试 profile、工作目录和属主独占的短路径运行目录。用 `codex-provision --tool worker_report --tool worker_workspace_shell --tool worker_workspace_export` 登记工具，将返回的固定会话交给 `codex-adapter --assignment-workspace /绝对路径/workspace.json`。JSON 文件权限为 600，填写 `runtime`、`node`、`docker`、`socket` 的绝对路径和已审核的本地 `image` 完整镜像 ID。可选的 `seed` 与 `execute_workspace_files` 沿用 [DSH 工位的容器配置](README.md#dsh-固定会话与飞书卡片)。此模式与 `--read-only-assignments`、审批转接互斥。App Server 本身保持只读，工作区位于原派发专用的无网络 Linux 容器。

`worker_workspace_shell` 仅接受原生会话中正在运行的正式工单回合。适配器核对原派发接管、原生输入和收件凭据。回执未知的命令留待核，同一原生调用号不会执行第二次。`worker_workspace_export` 按工作区相对路径读取文件，核长度与摘要后暂存；将其返回的附件号填进 `worker_report` 才会交给验收方。Router 已写成果但 Worker 尚未核到回执时，保留原容器。取消和转派等控制回合结束，再核对原派发容器已移除，随后交清理回执。换绑或版本回退前按[原工作区账本](router-rehearsal.md#重做版五组件的离线恢复)对账。

```sh
MOWULINK_DSH_CONTAINER_IMAGE=<已安装的固定镜像ID> python3 -B -m unittest -v \
  rewrite.test_codex_workspace.CodexWorkspaceTest.test_native_workspace_artifact_release_cancel_and_transfer
```

需要本机安装的 Codex App Server、Node、OpenSSL 和本地 Docker Unix socket。用 `MOWULINK_CODEX_WORKSPACE_EVIDENCE` 保存回执和标识。试验包含丢 W04 回执后的冷恢复。只在原工作区回收后发布五组件回退状态。在 Git 工作树复跑时，可另设 `MOWULINK_CODEX_WORKSPACE_PREVIOUS=7c53afa41d6639b43c808f872f0c318620cebbd4`，构建并启动旧版 Router、Worker、只读 Codex 适配器，检查原会话、原成果和私聊去重。试验用真实 App Server、Docker 和隔离 Router，模型与接替工位由本地脚本驱动。[成果、回退、取消和转派凭据](evidence/codex-assignment-workspace.json)

## 独立适配器启动

独立 App Server 的 `/model`、`/effort` 针对当前提供方查询模型目录，按原生会话设置更新并读回。目录未列出的模型仍可显示原选择；其未确认选项不可写。原编号菜单需平台确认送达，换模型后旧力度菜单失效。实际本地请求验证 Sol 转 Astra、`max` 与重开后 `low`。原生选择的 `ultra` 在本地 Responses 提供方请求中为 `xhigh`，真实账号的请求强度另验，卡片注明暂无实际请求回读。连续 12 轮通过。[设置与请求记录](evidence/codex-app-server-native-selection.json)。

`/context` 根据原生用量事件核对最近已观察到的回包及回合号。启动后没有事件时显示未记录。连续 10 次实际回读成功，模型是本地脚本。[用量记录](evidence/codex-app-server-native-context.json)。

独立 App Server 的平台 `/stop` 只中止本工位拥有收件凭据的单输入普通私聊，回执包含原生回合号与输入号。收件未知、原生混合输入或正式工单走各自的核验流程。丢回包后同消息号重投读回原待核卡，后续普通回合仍在运行。连续 20 轮通过，平台和模型用夹具。[原生中止记录](evidence/codex-app-server-private-stop.json)。

`/queue` 在工位保存输入，到原会话空闲后顺序递交。原生 `turn/start` 前复核忙闲，明确未写可等到下一次空闲，结果未知则只查原输入。实测原回合和两条排队消息各占一个回合，丢回包查回原收件，连续 16 次通过。[队列记录](evidence/codex-app-server-native-queue.json)。平台和模型用夹具。

`/resume` 从独立 profile 的原生索引浏览当前账号、同一工作目录的会话。标题缺失时显示未命名，归档和其他工作目录不显示。连续 12 轮核对目录、搜索和同号重投，历史会话始终未加载，无额外模型请求。选择编号仍提示切换敬请期待。[目录记录](evidence/codex-app-server-native-catalog.json)。

`/compact` 在空闲且无活动目标的绑定会话发起原生历史压缩。Worker 侧回执丢失时，新 App Server 可在冷启动后查回同一压缩回合和原生压缩项，重复请求回同一凭据。App Server 自己的原始确认丢失时，指令保持待核，原消息号不会再发起压缩。原生接口未返回历史条数或压缩后用量，因此完成提示只写已确认。同一套测试连续 15 轮通过，平台与模型为夹具。[压缩记录](evidence/codex-app-server-native-compaction.json)。

`/goal` 已接独立原生目标的查看、建立、暂停和继续。平台授权身份的明确指令携带原生用户来源。已有目标保留，建立时沿用宿主的预算配置。回执读取原生创建时间、状态和使用量。继续回执丢失后，重投同消息号仍显示待核，之后的暂停保持生效。连续 15 轮通过，平台和模型为夹具。[目标记录](evidence/codex-app-server-native-goal.json)。

新建工位时准备独立工作目录、属主独占的 Codex 测试 profile 及明确获准的测试模型连接。初始化命令会向该模型发送一条只读初始化消息，再关闭并重开 App Server，确认同一个线程、回合及输入号已由宿主保存。

```sh
python3 -m rewrite.service codex-provision \
  --codex-binary "$CODEX_BINARY" --codex-profile "$TEST_CODEX_PROFILE" --cwd "$TEST_PROJECT" \
  --tool worker_report --tool worker_ask
```

省略 `--tool` 时只登记 `worker_report`，可指定的名称来自 `agent-toolset.json`。读取成功回执里的 `thread_id` 配置隔离 Worker。命令输出请求登记的工具名，是否能执行仍须经原生调用验证。若初始化失败而返回线程号或回合号，先查该原生会话，命令不会重发初始化消息。默认无登录的 profile 在创建线程前即返回 `codex_test_account_required`。初始化中出现工具或审批请求会终止，未给该请求授权。真实 App Server 与本地脚本模型已连续跑通 10 次冷启动、回读及后续 W04。[初始化与回执记录](evidence/codex-native-provision.json)。

原生目标模式另有 12 轮隔离实测。原生工具创建并完成目标，宿主自动续办回合没有来自 Router 的独立输入，其报告请求被拒，工位继续持原派发。Router 正式询问后，带原派发收件凭据的新回合发出两次相同 W04，Router 只留一次报告和一次待验通知。[原生长目标回执](evidence/codex-app-server-native-formal-goal.json)

隔离环境以原生 `thread/fork` 准备测试子会话，再给它一个单独的适配器库。首次连接时库保存此刻已有的投递号，冷开不再扫描到后来真正收到的新输入。历史输入留在原生界面，复制来的投递号没有子会话收件凭据。未确认的父会话投递应回原会话核查。飞书指令发起与切换尚未接。[原生分叉回执](evidence/codex-app-server-native-fork.json)

文件审批仍待实测。只读隔离 App Server 的本地脚本模型工具表包含 `exec_command` 等八项，未列出 `apply_patch`。该次只读回合正常结束，试验文件没有创建，也没有原生文件审批事件。App Server 能生成文件审批的协议定义；实际工具、差异清单和宿主关闭回执仍要在支持该工具的独立工位一起核对。

适配器启动命令如下。

```sh
python3 -m rewrite.service codex-adapter \
  --db "$TEST_ADAPTER_DB" --identities "$TEST_ADAPTER_ROLES" \
  --cert "$TEST_TLS_CERT" --key "$TEST_TLS_KEY" \
  --worker-id "$TEST_WORKER_ID" --worker-connection "$TEST_ADAPTER_TO_WORKER" \
  --session "$TEST_NATIVE_THREAD" --codex-binary "$CODEX_BINARY" \
  --codex-profile "$TEST_CODEX_PROFILE" --cwd "$TEST_PROJECT"
```

`--media-cache` 可指定适配器拥有的绝对图像缓存目录。同一专用 profile 已启动时，第二个进程会被拒绝。默认 Desktop profile 不用于此入口。若原生配置要求 OpenAI 登录，须有独立授权的测试账号。适配器从自己的 App Server 标准输入输出接工具请求，只按原线程和原回合交给 Worker 的业务入口；生产 Desktop 插件的加载和授权仍需另验。

当前已用独立进程、TLS 和真实 App Server 连续跑通 12 次 W04。错工作目录和第二个 profile 写者均拒绝，原生工具回放只留一条 Router 通知。测试模型为本地脚本。[进程与回执记录](evidence/codex-adapter-process-w04.json)。

独立适配器同时记录首次绑定时已存在的回合，之后的新回合才进入出向队列。回执丢失时重传相同展示事件，原 Worker 只收一条；同一账本恢复后也不会重复抄送。独立进程跑过 10 轮，测试飞书每次只得到原样式的工单和回复两张卡。[出向试验记录](evidence/codex-adapter-output.json)。回合输出与 W04 投递各自推进。

后续版本还监听原生项的开始和结束通知。同一原生回合的输入、已完成的公开过程发言、执行中的命令工具会在回合结束前进入各自的飞书卡，最终答复在宿主确认回合完成后单发。工具只抄已识别的命令动词，参数、终端输出及内部 Worker 工具不出卡。丢工具回执后查到相同卡，最终对账没有第二张工具卡。连续 12 轮通过，[卡片与回执](evidence/codex-app-server-live-tool-card.json)。App Server 实际执行本地试验命令，模型和飞书是夹具。

指定 `--public-image-root` 可在独立工位抄送该专用目录中的原生 `imageView`。目录须由属主独占，文件及子路径不接受符号链接。原图按同一附件号重传，原文件在回执丢失后删去也不会重复上传。图前卡完成后单发原图，随后在另一张卡更新文字。实测原生历史曾短暂把完成回合标为中断，适配器等终止通知和历史一致后才发最终答复。连续 20 轮通过。[顺序与原图记录](evidence/codex-app-server-native-image-split.json)。实际 App Server 查看的是批准目录中的试验文件，模型和飞书用夹具，生成图及 Desktop 实际原生图片另验。

独立工位可在测试 profile 使用 `features.default_mode_request_user_input = true` 验原生题卡。当前支持带选项、可自填的单个非密题且无自动解题时限。测试平台回传选择、自填和跳过，适配器按原生请求号确认 `serverRequest/resolved` 后才更新原卡。缺回执时，同卡仍待核，同输入不会向宿主答第二次。密题、其他表单以及同原生题的新传输请求均拒绝。连续 10 轮通过。App Server 和 Worker 实际运行，飞书动作和模型为夹具，Desktop 窗口和真人点卡另验。[题卡与原生回执](evidence/codex-app-server-native-trainer-question.json)。

独立 App Server 也可将 `worker_ask` 交给原工单。测试管理端的原答复经同一派发返回同一原生线程，生成后续原生回合，再由该回合调用 W04。原题和成果各一条通知，原答复重投不产生第二个答复回合。连续 12 次通过。[原生问答记录](evidence/codex-app-server-native-question.json)。这里验证的是工单问溪流的协议，模型和管理操作者使用夹具。
