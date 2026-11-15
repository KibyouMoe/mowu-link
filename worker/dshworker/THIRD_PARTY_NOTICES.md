# DSH Hermes Bridge 来源

## 本副本（CodexWorker）

- 直接复制自 `CodexSyncArea` 仓库 `codex-skills/operate-codex-bridge/assets/desktop-plugin/`
  （源提交 `208db6d`），上游版本 0.2.38，未做行为改写。
- 改动范围：插件名 / MCP server 名 / 状态目录 / 日志前缀 → CodexWorker；
  新增 `transport/`（Desktop 管道与 app-server 两个实现）；
  新增 CLI 入口与跨平台启动脚本（`launch_codexworker.sh`）。
- `plugin.json` 的 `author.name` 保留原作者署名，不冒名。
- 下方为上游来源与许可记录，原样保留。

- 仓库：`git@github.com:KibyouMoe/DSHPluginSync.git`
- 源提交：`8d6b9102379f447c27acc74541cde3f3b3380a0f`（2026-09-21）
- 包：`@dsh/hermes-bridge` 1.0.0，package.json 声明 license 为 MIT。
- 原 `hermes-bridge/lib/index.js` SHA-256：
  `AB6AEA1EAC437A023AE752F960557A405EDA11CFF5D01BFE886EAB4B092EE6CA`
- 原 `hermes-bridge/lib/feishu.js` SHA-256：
  `ED93384846FDF49045042E1727B8767C8FC316EB83681E42C70BF68D2A3574D3`

`feishu.mjs` 复制原飞书实现，仅将 console.log 改为 console.error，防止污染 stdio MCP。
`desktop-manager.mjs` 复用原 HTTP 返回形态和 Hermes report/heartbeat 合同，重写宿主
适配、登记、幂等、公开历史观察、队列和事件持久化，不依赖 DSH ctx.agents。

没有复制训练员配置、飞书 app_secret、Token 或任何机器本地对话历史。
源 checkout 未附独立 LICENSE 文本；此记录保留源包 MIT 声明，不虚构作者版权署名。

## Hermes Agent 飞书输出格式（0.2.31）

- 上游：https://github.com/NousResearch/hermes-agent
- 提交：`74885f997bece88c1393bd174b25b32338336cb6`
- 文件：`plugins/platforms/feishu/adapter.py` 的 Markdown 检测、原生 post/文本选择和
  `_build_markdown_post_rows`；后者逐段移植到 `hermes-feishu-format.mjs`。
- `gateway/platforms/base.py` 的代码围栏分段行为用于新的分段实现。
- 已对照上游收发分离、持久去重、串行处理及 `/goal` 命令语义；Codex 继续使用自身
  原生任务/目标工具，没有复制或启动 Hermes 模型宿主、目标循环或另一个收信进程。
- 不包含 Hermes 配置、凭证、用户数据或本地历史。普通输出用原生 post/text，
  已有交互提问卡仍保留其原始回调和权限验证。

MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
