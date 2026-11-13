# CodexWorker 0.3 — Desktop 插件

插件通过 Codex Desktop 注入的原生任务通道使用既有会话。Desktop 启动插件时交接给
生命周期监督器，短暂的 MCP inventory 退出后收发继续；Desktop 退出时关闭所属 Worker。
独立 Worker CLI、私有 app-server、headless 启动和失败后的其他入口均已移除。
Node 是插件执行环境，不是另一个模型宿主。

## 安装与配置

需要 macOS 或 Windows Desktop、可用的原生任务工具和 Node.js 20+。
本包 `.mcp.json` 从安装根的 `server.mjs` 启动，禁止只复制顶层 `.mjs` 而漏掉 `interface/`。
按本地 marketplace 的正常安装流程加载完整插件；不要直接编辑客户端受管缓存。
当前 manifest 是 `.codex-plugin/plugin.json`，声明 `requires_local_executor: true`。

默认状态目录为 `<用户主目录>/.codex/state/codexworker`。
已有安装保留原状态目录、绑定、公开日志游标、消息 UUID 和凭据。若原实例另有状态目录，
在默认目录创建私有 `worker-instance.local.json`：

```json
{"stateDir":"<本机既有状态目录的绝对路径>"}
```

也可由宿主显式提供 `CODEXWORKER_STATE_DIR`。上述设置只选择状态目录，不启动 CLI，
不读取旧 `desktop-cli.local.json`，不创建或替换会话。

在实际状态目录配置 `desktop-config.local.json`（凭据和机器配置不进 Git）：

```json
{
  "autoStart": true,
  "host": "127.0.0.1",
  "port": 8905,
  "machine": "OguriCap",
  "bridgeName": "小栗帽",
  "operatingMode": "feishu-local",
  "tokenFile": "<本机鉴权令牌文件绝对路径>",
  "workspaceRoots": ["<已有会话工作目录>"],
  "publicJournalRoot": "<Codex主目录>/sessions",
  "router": {
    "enabled": true,
    "url": "http://<router所在机器>:8790",
    "tokenFile": "<本机router令牌文件绝对路径>"
  },
  "feishu": {
    "configFile": "<本机飞书凭据文件绝对路径>",
    "inboundEnabled": true,
    "trainerOpenIdWhitelist": ["<当前飞书应用的OpenID>"],
    "onlineNotificationEnabled": true,
    "streamEnabled": true,
    "transcriptEnabled": true,
    "toolProgressEnabled": true
  }
}
```

`autoStart` 缺项等于 true；false 保留插件工具和绑定，但阻止自动启动、原生连接及兼容回退。
非法值拒绝启动。运行中的监督器检查开关并只关闭自己托管的进程；重新启用后可再次启动。
`feishu.onlineNotificationEnabled` 独立控制上线/重连消息，不代表进程是否在线。

新安装在所选 Desktop 聊天调用 `bridge_bind_manager`、`bridge_register_current_task` 完成绑定；
已有安装使用原绑定和原主会话。无有效绑定时工具可诊断，但不偷偷新建会话。

## 聊天、工单和目标

普通 worker 交流使用 router 持久消息通道：

- `bridge_list_workers`：先核对准确的 worker 名称与在线情况。
- `bridge_send_worker_message`：`messageId` UUID、`to`、`text`，回复附 `replyTo`。
  图片用已上传的附件 ID；原字节单独传输，不发送其他机器的本地路径。
- `bridge_worker_inbox`：只读自己的待确认收件箱。

普通消息不创建任务或目标，不自动互相回复。router 的 `queued` 只表示已保存，
`accepted` 表示收件 Worker 已持久接受，均不代表对方已经回答。
SSE 和离线轮询重放同一 ID；入站经同一会话队列，公开输入/输出继续抄送飞书。

确定项目才用 `bridge_create_work_order`：稳定的 `messageId`、`worker`、`goal`，可附 `context`。
同一项目的补充、纠正和验收归同一单，不为每条聊天另开工单。
`bridge_work_order_status(taskId)` 核对 router 的持久状态和最终结果。

正式工单默认要求原生目标模式：先查已有目标，允许时建立新目标；已有目标不能被擅自替换、
标记完成或暂停。是否成功以原生工具实际返回为准；引擎没有目标工具时明确说明能力边界。
不设默认任务墙钟期限，只有显式 `deadlineS/deadline_s` 生效；无进展检测默认 30 分钟，
已知等待用户回答不算模型停滞。创建和消息重试复用原 UUID，未知结果不盲目换 ID 重发。

新工单先核对 router `/health` 的 `capabilities.workOrders=idempotent-goal-v1`；
旧服务会忽略创建 UUID，插件会明确要求升级并阻止 POST。router 尚未装载新 API 或凭据暂缺时，
飞书普通收发仍独立保留；只有依赖 router 的工具暂不可用。

## 原会话、飞书与审批

原生引擎负责上下文压缩，Worker 保存公开记录和投递回执，不回注第二份语义记忆。
飞书正文编辑同一张卡片；工具进度同卡累积、1.5 秒节流、40 字预览，只展示公开活动。
原生提问保留原问题 ID，单题点选即发送，自行输入用发送；输入框与跳过/发送同行。
输入超过图像消息限制时支持文件型图片；公开输出图片复用附件 ID 和回执，避免重复发送。

router 审批必须有 handler，拒绝、异常和超时均明确拒绝；Desktop 管理原生工具审批的路径
以其真实协议为准，不把声明的能力当成已实测能力。

## 生命周期与更新验收

`GET /health`（Bearer 鉴权）应看到业务版本 `0.3.0`、原会话 ID、监听 PID、飞书和 router 状态。
固定层验收还需 `businessHotReload.bootstrapVersion=0.3.0`，
Windows 的 `desktopLifetimeLaunchMode=windows-desktop-parent`，Mac 为 `desktop-owned`。
必须核对实际源提交/哈希、安装源、缓存装载和服务进程；版本号相同不能替代这些检查。

固定启动层、清单和新入口需正常重新装载 Desktop；后续业务代际热换保留收件、队列、UUID 和回执。
热换的忙态必须包含 router 任务/消息；停机先终止 router 观察再关闭 manager，不把生命周期退出
误报为任务失败，不强制取消已被引擎接受的未知写入。

每次平台相关修改都测试 macOS 与 Windows 分支，并分别记录原生平台运行结果；
模拟通过不能代替目标机实测。POSIX 私有文件仍要求 0600，Windows 检查其真实权限语义。

最低实际安装验收：

1. 实际发布包冷启动 `initialize` / `tools/list`，然后在正常 Desktop 中调用 Worker 工具。
2. 无聊天启动后，飞书真实消息进入原会话并自动回复；Desktop 普通输入与同轮输出抄送。
3. Desktop 完全退出后原 Worker 退出，重新打开自动恢复原会话、飞书和 router，且只有一个 writer。
4. `autoStart:false` 的冷启动无网络、原生写入或额外服务；插件工具和绑定可读。
5. router 正式任务往返带回唯一标记，普通 worker 消息双向交流和离线重放不产生工单。
6. 图片核对源字节、SHA256、下载内容和真实回执；外部接口接受不替代真人或原生回流验收。

适当测试：`node --test --test-reporter=tap tests/*.test.mjs`，
router：`ROUTER_TEST_PORT=18790 python3 server/router/selftest_router.py`（仓库根目录）。
完整命令、真实输出和未验项记录在仓库 `docs/desktop-only-progress.md`。
