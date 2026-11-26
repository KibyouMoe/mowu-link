# Antigravity 桌面兼容层核对

2026 年 11 月 26 日读回本机 `/Applications/Antigravity.app` 的包版本为 **2.32.1**，包 ID 为 `com.google.antigravity`。本次核对使用应用版本元数据与 Google 文档。独立测试会话的运行证据待补。

| 兼容层调用 | 已找到的入口 | 还需验证 |
| --- | --- | --- |
| 读取训练员原生输入与多伯答复 | 工作区 `.agents/hooks.json` 可绑定钩子。宿主向钩子给出会话号、最近用户输入和 `transcript.jsonl` 的路径，可作为读取入口。 | 在独立项目中取得一对真实输入和最终答复，核对文本片段、工具流水与推理的区别，验证原生重开和飞书标准卡片。文档列出钩子字段，转录记录的内容结构需要实测。 |
| 向既有会话下发文字或叫醒空闲会话 | `PreInvocation` 可在已有调用前加消息。2.34.0 起文档还描述 MCP App 视图使用 `ui/message` 发到当前或新会话。 | 2.32.1 缺少文档所述 MCP App 入口的版本条件。未核到可从 Worker 后台按 ID 唤醒这版空闲会话的公开接口。以后验证 MCP App 时先核后台触发、目标会话控制和宿主收件去重。 |
| 停止当前执行、插话及原单清理 | `PreInvocation` 等钩子可让当前调用停止。`Stop` 给出结束原因和忙闲信号。训练员截图里的 Send Immediately 适用于客户端发送。 | 需独立证明 Worker 能取消指定的当前回合，后续输入没有误排队，原工作环境已清理。单个 Stop 事件只用于观察。 |
| Agent 工具回报 | App 官方支持在自定义设置添加 MCP 工具。钩子还可观察工具调用、步骤号及失败信号。 | 独立测试会话要实际枚举并由 Gemini 调用 `worker_report`。须核目标会话、原生输入、派发号和调用号，再测 W04 与去重。 |
| 忙闲、模型、上下文、选会话 | `Stop` 有空闲信号，钩子带 `modelName`；App 有原生会话选择 UI。 | 还没有证据能从 Worker 可靠读到当前思考强度、全量 Token 用量或搜索与改名，也缺少同一会话的程序调用验证。没有接入的命令维持原提示。 |

| 备选宿主 | 官方入口能做到的事 | 原桌面会话是否保持 |
| --- | --- | --- |
| Antigravity CLI | 交互式 `/resume` 可以从 App 克隆对话历史、上下文和工具轨迹。无头模式可用结构化输入输出，继续自己先前创建的对话 ID。[交互式恢复](https://antigravity.google/docs/cli/commands/resume/) · [无头模式](https://antigravity.google/docs/cli/headless/) | 克隆后由 CLI 继续。未核到写回原 App 会话的接口。 |
| Remote Control | 同一 Google 账号可从浏览器打开已有会话并与它交互。[Remote Control](https://antigravity.google/docs/remote-control?tab=cli) | 文档提供人工操作入口。Worker 后台按 ID 写入、取消和查询收件凭据仍缺公开契约。 |
| Remote Agents | 可以从多个客户端使用同一个远程托管会话，受 Google 账号及产品资格约束。[Remote Agents](https://antigravity.google/docs/remote-agents) | 可另选远程托管工位，原有本地会话接管未验证。 |
| Antigravity SDK | 自建 Agent 可以保存和恢复 SDK 会话。[SDK 生命周期](https://antigravity.google/docs/sdk/lifecycle/) | 与本机已运行的 App 会话共写缺少官方保证。 |

要保留多伯原桌面会话，继续核工作区钩子与实际 MCP 工具，并等待受支持的后台投递、停止入口。若允许多伯使用独立宿主，可另做 CLI 或 SDK 的隔离验收。当前只核文档与安装版本，未安装 CLI、注册工具或运行这几条备选。

独立验收先取一对问答的逐条显示证据，再试在原会话空闲时下发，以及在已知派发工作中取消。每个入口分别取得宿主、Worker 和飞书的回执。读取会话必须绑定指定会话，输出选择经验证的用户正文和 Agent 最终答复。

[Google Antigravity Hooks](https://antigravity.google/docs/hooks) · [MCP](https://antigravity.google/docs/mcp) · [MCP Apps](https://antigravity.google/docs/mcp-apps/) · [SDK 生命周期](https://antigravity.google/docs/sdk/lifecycle)
