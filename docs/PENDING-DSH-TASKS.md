# 待派给 DSH 的任务队列

> 训练员 2026-11-08 23:41 定：这条要**由 DSH 自己做**，不是我或子代理代劳。
> Router + DSH worker 起来后，第一件事就是通过 Router 把下面的任务派给 DSH。

## T1 — 给 DSH 装上 computer use（优先级：高）

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
