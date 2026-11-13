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

## 补充验收：长正文、桌面图片与全量回归

2026-11-13，当前提交 `fbea7b4`；以下测试未修改运行代码、未重启生产服务。

### 长正文实机通过

直接在 DSH 桌面发送 `STREAM-20261113-02`，要求约 2500 字、12 个段落，
没有调用工具。原生投影轮次后缀 `-827` 完成，正文包含 STREAM-BEGIN/STREAM-END。
采到 12 次 item.delta，累计长度依次为
`24,362,523,671,854,1021,1205,1388,1578,1762,2192,2566`。

- 同一个 outbox UUID：`b046e5aa36bfe9280181b97bfe6fd5f4`。
- 同一张飞书卡片：`om_x100b60582b770c84b4981063f264f5b`。
- 创建成功，12 次更新均 accepted、API code=0；最终 sent/complete=true。
- 本地证据：`/tmp/tamamo-stream-evidence.jsonl`；桌面截图
  `/Users/kibyou/Workspace/OguriCap/tamamo-stream-after.png`。

这补齐了上面短回复验收未覆盖的长正文流式测试。

### 桌面图片：识图通过，原图抄送未通过

通过系统剪贴板把真实 PNG 粘贴到 DSH 输入框，确认出现缩略图后发送
`IMAGE-20261113-03`。投影轮次后缀 `-837` 完成，模型正确描述绿色/黑方块、
黄色/蓝圆、红色/白星三块图案，证明原生模型确实收到图片。

manager seq 427 的 message.observed 只有 `[DSH 桌面图片附件]` 加文字提示；
autoImages 仍为 0。桌面入站原图没有进入飞书图片通道，这一缺口尚未修复。
源图 `attachment-tricolor-t-bfb7cdafbf.png`：1672×941、1129272 字节，SHA256
`31a18f2400d54726b6d4d77a22dbe34bda5ed7605c1b88ad92f0bf9495f38fe8`。
截图 `/Users/kibyou/Workspace/OguriCap/tamamo-image-result.png`。

### 全量回归并非全绿

使用 Node 26.12.1，移除宿主继承的 Worker 环境变量后运行；此前带宿主环境的
结果存在额外失败，不作为最终统计。20 秒超时仅用于隔离测试中的挂起用例，
没有修改生产任务超时。两个 worker 分别执行：

```sh
env -u CODEXWORKER_NATIVE_TRANSPORT -u CODEXWORKER_STATE_DIR -u CODEXWORKER_IDENTITY \
  node --test --test-timeout=20000 --test-force-exit --test-reporter=tap \
  worker/dshworker/tests/*.test.mjs
# 第二次将上面的 dshworker 换成 codexworker
ROUTER_TEST_PORT=18790 python3 server/router/selftest_router.py
```

| 版本 / 套件 | tests | pass | fail | cancelled | skipped |
|---|---:|---:|---:|---:|---:|
| 当前 DSH | 629 | 600 | 20 | 2 | 7 |
| 基线 b2169a1 DSH | 617 | 588 | 20 | 2 | 7 |
| 当前 Codex | 635 | 605 | 22 | 1 | 7 |
| 基线 b2169a1 Codex | 635 | 605 | 22 | 1 | 7 |

基线从 git archive 解到独立临时目录，使用相同环境和测试参数。
逐项比较 not-ok 用例名称，两侧均无新增、无消失；这只证明本轮没有新增失败，
不代表已有失败无影响。仍包括图片自动发送断言、热更新测试夹具、未确认投递
处理的 lastError 异常，以及旧卡片竞争用例超时；需要另行定位和修复。

真实完整输出：`/tmp/tamamo-full-clean.tap`、`/tmp/oguri-full-clean.tap`、
`/tmp/dshworker-baseline-clean.tap`、`/tmp/codexworker-baseline-clean.tap`。
router 输出 `/tmp/router-full-20261113.log` 最后一行是 `TOTAL 77/77 PASS`。
本轮只更新验收文档，无需重启任何组件。
