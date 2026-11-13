# DSH 桌面独立轮次抄送

在已绑定的 DSH 原会话中，客户端直接发送消息后，Worker 现在常驻观察原生
`session/event` / `agent/assistant-stream`，把桌面轮次投影到共享 manager。
不会创建替代原会话，也不自动接管其他桌面聊天。

## 实现

- `worker/dshworker/interface/desktop-observer.mjs`：从原生 turn/start 建立投影，
  观察真实 user/message、公开正文、工具进度、提问和 continued 答案；等 root 与
  子 agent 空闲再结算。忽略 runtime-context、私有 reasoning 和工具原始输出。
- `interface/inprocess.mjs`：安装/关闭常驻观察器；Worker 自己发送的消息登记原生
  message ID 去回声，保留内容相同但 ID 不同的桌面输入。轮次结束释放登记。
  恢复原 agent 后记录当前事件基线，不重播旧历史。
- 仍由已有 questions/public-events 和共享业务层完成提问卡、流式正文及工具卡。
  桌面输入含图片时留 `[DSH 桌面图片附件]` 标记；本次不复制入站原图二进制。
  助手公开正文中的图片路径继续进入已有图片发送通道。
- `dsh-approval.test.mjs` 的旧 mock 改为按事件名称保存审批回调，避免新增监听器
  覆盖该 mock 的单个变量；审批行为未变。

`cli.mjs`、`desktop-manager.mjs`、`mcp-business.mjs` 两侧逐字节一致，均未修改。
Codex Worker / router / gateway 无代码改动，未重启。
Token 用量按训练员要求不做。

## 自动验证

```sh
node --test \
  worker/dshworker/tests/dsh-desktop-observer.test.mjs \
  worker/dshworker/tests/dsh-public-events.test.mjs \
  worker/dshworker/tests/dsh-questions.test.mjs \
  worker/dshworker/tests/dsh-progress.test.mjs \
  worker/dshworker/tests/dsh-approval.test.mjs \
  worker/dshworker/tests/native-public-questions.test.mjs \
  worker/dshworker/tests/question-routing.test.mjs \
  worker/codexworker/tests/native-public-questions.test.mjs \
  worker/codexworker/tests/question-routing.test.mjs
```

Node 26.12.1，真实输出 `/tmp/tamamo-desktop-unit.log`：

```text
tests 117
pass 117
fail 0
```

其中新增 12 条，包括真实 DesktopManager 配合模拟原生事件/发送器的集成测试：
桌面输入产生一次 message.observed、输入/输出都进入发送通道、没有反向再调用
send_message_to_thread。该测试不能替代飞书平台的真实接收回执。

## 部署与实机状态

DSH 空闲时备份状态到 `/tmp/tamamo-desktop-observer-backup-20261113`，正常关闭并
重新打开 DeepSeek Harness。8906 ready=true，Feishu connected=true，原 Worker
thread `d9fb1019-66c4-44db-9165-9c4778c53472` 和原生 session
`session-93b7fc94-e7c9-421c-9cfa-6175b14dc7bb` 均保持不变。

2026-11-13 08:01:43–08:01:48 UTC，已直接通过 DSH 桌面输入框完成实机验收。
测试标记 `DESKTOP-20261113-01`，没有经过 router 派单或 Worker 发消息接口。
原生轮次 `dsh-desktop-session-93b7fc94-e7c9-421c-9cfa-6175b14dc7bb-807`
记录 `nativeDesktop=true`、`status=completed`，原 sessionId 保持不变。

- manager seq 406：`message.observed`，`origin=native`、`alreadyDispatched=true`。
- seq 407/409：terminal running/completed，真实命令 `echo TAMAMO_DESKTOP_OK`。
- seq 408：工具进度；seq 410：最终正文；seq 411：turn.completed。
- 飞书输入回执：`om_x100b6058704c2c80b49e90d7258cb8f`。
- 飞书工具卡回执：`om_x100b6058707ac4bcb0edc7f165ddd15`。
- 飞书最终回复回执：`om_x100b60587000284ccf90ccd3b14ca43`。
- 三项 outbox 均 `status=sent`、`complete=true`、`lastError=null`。
- 最终回复包含命令输出 `TAMAMO_DESKTOP_OK` 和“桌面抄送验收完成”。

桌面截图：`/Users/kibyou/Workspace/OguriCap/tamamo-desktop-ui.png`；证据图片不入 Git。
这条回复较短，轮次结束前的单独正文增量没有被轮询采到；不能据此声称本轮实机
另外验证了长文本流式逐字刷新。原生独立入口、输入/工具/输出抄送已验证。

Computer Use 的 getApp 超时后，使用本机 CuaDriver CLI；首次从 Codex 子进程
启动的驱动继承调用方 TCC 身份，虽已允许 CuaDriver，实际捕获仍拒绝。
训练员确认授权后，正常重开驱动并经 LaunchServices 独立启动，
`permissions status --json` 报 `attribution=driver-daemon`、responsible_ppid=1，
Accessibility/Screen Recording 均 true，随后窗口读取、输入和发送成功。
未跳过权限检查，未修改 TCC 数据库，未更改 DSH/Codex/router 服务。
