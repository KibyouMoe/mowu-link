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

App Server 指定回合的文字投影复用原 Worker 的飞书卡片。独立原生输入保留原空格和换行，最终答复单独成卡。只读取已结束回合，桥接投递、内部工具调用及参数不出卡。稳定事件号令重投返回同一回执。连续 12 次通过。[卡片试验记录](evidence/codex-app-server-native-display.json)。输入为隔离驱动，模型和飞书为夹具；常驻监听、原生图文和飞书云端另验。

独立 App Server 的图文与纯图使用原生 `localImage`。飞书夹具按原消息上传，适配器先核私有缓存的长度与摘要，再递交到固定会话。同条原文和图实际出现在模型请求，原生 App Server 添加的图片元数据单独核对。丢回执后按原客户端标识查回原生输入，两次试验输入分别只出现一条。重复 12 轮通过。[图片试验记录](evidence/codex-app-server-native-media.json)。目前只接已登记图像，模型、飞书为夹具。
