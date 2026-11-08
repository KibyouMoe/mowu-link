# clients/filters — 已退役（0.6.0）

0.5.x 的客户端规则文件（`mc_chat.json`、`email.json`）已经删除。

- **L1**（本目录所属的客户端层）只剩来源特有的业务判断：
  MC 的 `self_message`、email 的 `folder_not_watched`，写在各自客户端里，
  不再读 JSON。
- **L2**（限流、去重窗口、优先级降级、静默期）统一在服务端：
  `~/.hermes/trigger-rules.json`。格式与语义见仓库 README 第 8 节。

旧脚本仍可传 `--rules`，参数会被接受但忽略，并在客户端日志里记一条 WARNING。
