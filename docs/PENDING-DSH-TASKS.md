# 待派给 DSH 的任务队列

> 训练员 2026-11-08 23:41 定：这条要**由 DSH 自己做**，不是我或子代理代劳。
> Router + DSH worker 起来后，第一件事就是通过 Router 把下面的任务派给 DSH。

## T1 — 给 DSH 装上 computer use（✅ 已完成 2026-11-09 09:25）

训练员原话：「实现一下 DSH 自己的 computer use，网上应该有一大堆可以下载的，下载下来就好了，不管是插件还是技能。」

交付要求：

1. 自己调研 DSH 的插件/技能生态（`dsh plugin add`、`~/.dsh/skills`），并上网找现成方案
   （优先官方 / 高星 / MIT 或 Apache / macOS 可用 / 依赖少）。
2. 实装（技能或插件均可），落在 `~/.dsh/skills/computer-use/`，并提交 `DSHSkillSync`（main）。
3. 真实跑通：截图一张（验证尺寸）+ 鼠标移动/点击一次无害位置，贴证据。
4. 文档写清 macOS 权限坑：屏幕录制 + 辅助功能，未授权时的报错长什么样。

边界：

- 不装来路不明的二进制；每个下载写清来源 URL + 许可证。
- 不动 Hermes 代码、不动 `config.yaml`。
- 不接系统级输入注入去操控 Minecraft（那是另一件事的禁区）。

## 派发方式（Router 上线后）

```
POST /tasks {
  worker: "dsh",
  goal: "给 DSH 装上 computer use（技能或插件），真实跑通截图与点击",
  context: <本文件 T1 全文>,
  workspace: "/Users/kibyou/.dsh/skills",
  urgency: "normal",
  queue_if_offline: true
}
```

---

## B1 — DSH headless 的模型路由（阻塞项，2026-11-09 00:05）

现状：`dsh --profile headless` 在 worker 里跑得起来，但模型调用失败：

```
[llm-deepseek-api-key] model listing from https://api.deepseek.com/models loaded 2 model(s)   ← key 有效
dsh: HTTP_404: DeepSeek Messages request failed (404)                                          ← 路由不对
```

原因：DSH 的 `deepseek-official` 路由走 **Anthropic Messages** 协议（`/messages`），
而 `api.deepseek.com` 只提供 OpenAI 兼容的 `chat/completions`。

两条路：
1. 在 DSH 的 Models 页给 headless 配一个 Messages 兼容的端点/密钥（训练员手工，一次性）；
2. 写一个本地小代理：`/v1/messages` → 翻译成 `https://api.deepseek.com/chat/completions`，
   再让 `DEEPSEEK_BASE_URL` 指向 `http://127.0.0.1:8795`。（推荐，全自动）
