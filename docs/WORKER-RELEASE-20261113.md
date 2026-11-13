# Worker 本轮交付整理

## 已完成

- 飞书与 Codex 原生提问双向同步，回答更新原卡片；支持跳过，并将问题交给原任务发起方。
- 聊天忙碌时接收 router 指令，保留原会话、确定性消息 ID 和单写入通道。
- 图片通过独立鉴权附件接口上传/下载；聊天引用 SHA256 图片 ID，支持仅上传取 ID。
- Hermes 接收端校验原图字节，修复首次人工消息之前的冷启动投递。
- Codex 历史改为元数据、turn 和 item 分页读取，避免生图后全量历史超过 16 MiB 导致 Worker 无法恢复。
- Codex/DSH 共享三业务文件逐字节一致；DSH 引擎能力对齐另行验收，不把文件同步视为运行验收。

## 本次提交前验证

- Node 26：14 组相关测试，184/184 通过；包含分页、CLI 原会话重启、忙态 router 投递、提问和附件。
- Router：`ROUTER_TEST_PORT=18790 python3 server/router/selftest_router.py`，77/77 通过。
- 附件闭环：使用 Hermes 的 Python 3.14 环境运行 `server/router/selftest_attachments.py`，13/13 通过。
- 系统 Python 版本不足以加载当前 Hermes；CLI 假引擎已补齐分页协议后重跑通过。
- 原会话最近 30 turns / 1229 items 实读通过；Worker ready、飞书及 router connected。
- 全新三色图片实机附件事件 319：1,129,272 字节，源文件与接收端 SHA256 均为
  `31a18f2400d54726b6d4d77a22dbe34bda5ed7605c1b88ad92f0bf9495f38fe8`。

本轮未跑全量旧 Desktop 测试，不以以上定向测试声称全量无历史失败。
生产 Codex Worker、router 和 Hermes 附件插件已分别在此前步骤加载；本次提交推送不重启服务。
原生 Codex 后台及主会话保持不变；独立图片 CLI 的更新无需重启。
