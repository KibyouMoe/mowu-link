# clients/state — 运行时状态（不提交）

这个目录里除了本文件和 `*.example.json`，都是进程自动生成的运行时数据，
已在仓库根 `.gitignore` 里排除。**不要把线上游标提交上来。**

| 文件 | 谁写 | 内容 | 删了/坏了会怎样 |
|---|---|---|---|
| `mc_chat_cursor.json` | `mc_chat_client.py` | `{source, last_seq, saved_at, bridge}` | 不重放；以 bridge 当前 `events.lastSeq` 立新基线，记一条 WARNING |
| `email_cursor.json` | `email_client.py` | `{folder, uidvalidity, uidnext, last_uid}` | 首次连上以 `UIDNEXT-1` 立基线，不重放历史；UIDVALIDITY 变了自动重置 |
| `dedupe.json` | 0.5.x `filterwall.py`（已退役） | `{source, saved_at, entries: {sha1: ts}}` | 0.6.0 不再生成，可安全删除；去重窗口已移到服务端 L2 规则 |

约定：

- 写盘一律 **tmp + `os.replace`**（原子替换），进程被杀不会留下半个 JSON。
- 游标语义是「在源里读到哪了」，不是「投递成功到哪了」，也不是
  「这条是不是重复」（后者看 `event_id` / `dedupe_key`）。
- 格式模板见 `mc_chat_cursor.example.json`；真实文件不会被 git 跟踪。
