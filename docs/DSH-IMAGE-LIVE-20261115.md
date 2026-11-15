# DSH 图片实机验收

工单 `t-7a4258f76a` 按 B、C、A 顺序推进。B 的回执于 2026-11-15 12:33 左右先单独交给溪流。

## B 已通过

玉藻原主会话调用原生 `present` 后，Worker 自动向玉藻的飞书私聊投递 1 条图片消息。经飞书接口读回消息并下载原件，原字节哈希与源文件相同。

### 实际断点

DSH 的 `read_image` 返回 `type=image` 的 attachment 引用，包含 attachmentId、mediaType、bytes、width、height、name。此次引用为 `sha256:fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f`，800×450、3305 字节。

公开呈图由 `present` 工具产生 `deliverables/presented`，里面是文件路径。客户端使用同一个事件渲染交付卡片。基线适配器没有处理这个事件，生产媒体账本为 0。

```json
{"type":"deliverables/presented","seq":7889,"data":{"turn":97,"callId":"call_00_7xfV16O9mS1yitU1beWx5630","files":[{"path":"/Users/kibyou/Workspace/TamamoCross/evidence/b-acceptance/media-repro-20261115-121956-32381.png","description":"800×450 test PNG: left green with black square, right yellow with blue circle, red diamond at bottom center"}]}}
```

此次补接根会话的显式呈图事件，将本地 raster 文件投影为独立的 `MEDIA:绝对路径` 公开项。原生 attachment 读图结果继续留在读取通道，呈图由 `present` 事件授权。重复事件、后续同路径 MEDIA 行共用原媒体收据。

### 运行更新

本机已启用 DSH 的原生 HMR profile watcher。先备份 patch，再停用旧 Worker 插件，等待 8906 端口释放，随后将插件入口切到修复工作树并启用。

```text
pluginRoot=/Users/kibyou/projects/mowulink-dsh-worker-fixes/worker/dshworker
profile=/Users/kibyou/.dsh/profiles/desktop/cordis.patch.yml
backup=/Users/kibyou/.hermes/state/dshworker/deployment-backups/cordis.patch.20261115-123019.yml
DSH host PID=19902
threadId=d9fb1019-66c4-44db-9165-9c4778c53472
sessionId=session-93b7fc94-e7c9-421c-9cfa-6175b14dc7bb

2026-11-15T04:30:57.525Z agent_resumed seq=7906
2026-11-15T04:30:57.533Z business_host_context pid=19902 pluginRoot=/Users/kibyou/projects/mowulink-dsh-worker-fixes/worker/dshworker
2026-11-15T04:30:58.919Z business_started threadId=d9fb1019-66c4-44db-9165-9c4778c53472
2026-11-15T04:30:59.021Z router_connected
```

更新后的健康接口确认飞书和 router 均 connected，selectedThreadId 为原主会话。正常插件更新在原 DSH 宿主进程内完成。

### 真实发送与读回

再次让玉藻在原会话调用 `present`，收到第 7924 条 `deliverables/presented`，原生 turn 为 98。发送账本的 itemKey 包含 `dsh-presented-…call_00_ET_I6zKU7jJNO92XRYiZ0794624-0-media-…`，对应此次原生呈图投影。

源文件为 `/Users/kibyou/Workspace/TamamoCross/evidence/b-acceptance/media-repro-20261115-121956-32381.png`，左绿底黑方块、右黄底蓝圆，下方红菱形。

```json
{"message_id":"om_x100b60a7157e4cacce5efd1630e315c","msg_type":"image","content_keys":["image_key"],"source_bytes":3305,"downloaded_bytes":3305,"source_sha256":"fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f","downloaded_sha256":"fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f","download_path":"/tmp/dsh-b-feishu-original-630e315c.png","content_type":"image/png","upload_attempts":1,"send_attempts":1}
```

读回调用使用当前玉藻飞书应用的既有凭据，在内存中获取 tenant token，GET `/open-apis/im/v1/messages/{message_id}` 核对 msg_type 与单一 image_key，再 GET 该消息的 `/resources/{image_key}?type=image` 下载原件核对 SHA256。令牌和图片二进制均留在独立通道。

### 回归

```bash
node --test worker/dshworker/tests/dsh-compat.test.mjs worker/dshworker/tests/dsh-public-events.test.mjs worker/dshworker/tests/dsh-public-media.test.mjs worker/dshworker/tests/dsh-media-question.test.mjs
ROUTER_TEST_PORT=18790 node worker/check-compat.mjs
```

```text
targeted tests=33 pass=33 fail=0 skipped=0
shared_root_modules=37 matched=37 mismatched=0
codexworker passed=834 failed=0 skipped=8 total=842
dshworker passed=807 failed=0 skipped=8 total=815
router TOTAL 138/138 PASS
WORKER_COMPAT_PASS
```

以上为本机 macOS Node v22.22.0。B 提交 `ede78d9de62973c7207332968ec6dde3789ded2f` 的 Mac／Windows Node 22、26 四组原生 CI 均 success。[B 的原生 CI](https://github.com/KibyouMoe/mowu-link/actions/runs/43677573039)

| 平台与 Node | Codex 通过 / 失败 / 跳过 / 总数 | DSH 通过 / 失败 / 跳过 / 总数 | router |
| --- | --- | --- | --- |
| Mac 22、26 各组 | 834 / 0 / 8 / 842 | 807 / 0 / 8 / 815 | 每组 138/138 PASS |
| Windows 22、26 各组 | 818 / 0 / 24 / 842 | 806 / 0 / 9 / 815 | POSIX 服务自测在 Mac 运行 |

界面验收最初遇到 Mac 锁屏。训练员解锁后，于 13:10 授权小栗帽从本机飞书代发同一测试图，DSH 原生图片气泡和原图预览均实际显示。B 的独立证据为原生呈图事件、实际飞书媒体消息与原件下载校验。

## C 已通过

玉藻在原会话执行现有 `scripts/send-router-image.mjs`，通过独立二进制上传与附件 ID 通知交给溪流。溪流在工单 `t-0149ea6a8c` 中确认下载文件、尺寸和画面，小栗帽另从接收路径读取原件复核。

```json
{"path":"/Users/kibyou/.hermes/attachments/router/fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f.png","bytes":3305,"sha256":"fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f","message_id":"2b93bfb4-d2aa-4ae2-940b-2ce6ebbd217a","event_id":"notice:255dc44ab8c57854ff61e208","trigger_id":373,"receiverConfirmationTask":"t-0149ea6a8c"}
```

接收文件 PNG 800×450，源文件、router 内容寻址 ID、溪流落地文件的完整 SHA256 相同。

## A 已通过

修复版本 `f282119163c321927bd01312a5c586c66bff19f9` 已部署，健康接口报告 `feishuImageInput=native-local-image`。原生 `user/message` 保存图片 attachment 引用，DSH 客户端可点击图片气泡打开原图。

实现采用 DSH 自己的 `attachments.saveImages` 批量校验、规范化和保存附件，向原 Agent followup/steer 投递 `type=image`、`attachment={attachmentId,mediaType,bytes,width,height,name}`，传输中使用原生引用。图片输入预备失败按现有 nativeImageOutcome 契约明确报告 notAttempted。公共根业务继续由传输声明的 imageInput 能力启用同一条飞书接收入口。

新增 9 项用例覆盖图片、文件和多图图文入站，20 次重复只写 1 次原生输入，忙碌回合 steer，存储失败、无效批次、关闭期间写入、旧引擎路径回落，以及图片标题按普通内容处理。

```text
node --test worker/dshworker/tests/dsh-image-input.test.mjs
tests=9 pass=9 fail=0 skipped=0

ROUTER_TEST_PORT=18790 node worker/check-compat.mjs
shared_root_modules=37 matched=37 mismatched=0
codexworker passed=834 failed=0 skipped=8 total=842
dshworker passed=816 failed=0 skipped=8 total=824
router TOTAL 138/138 PASS
WORKER_COMPAT_PASS
```

### 原生部署

DSH HMR 按 profile 和已链接包的根目录清理模块缓存。此次入口使用新的 detached worktree 路径，为绝对导入提供完整的新模块 URI，并通过原生 profile watcher 正常停用、排空和载入 Worker 插件。

```text
commit=f282119163c321927bd01312a5c586c66bff19f9
pluginRoot=/Users/kibyou/projects/mowulink-dsh-image-native/worker/dshworker
profile=/Users/kibyou/.dsh/profiles/desktop/cordis.patch.yml
backup=/Users/kibyou/.hermes/state/dshworker/deployment-backups/cordis.patch.20261115-130206.yml
DSH host PID=19902
threadId=d9fb1019-66c4-44db-9165-9c4778c53472
sessionId=session-93b7fc94-e7c9-421c-9cfa-6175b14dc7bb

2026-11-15T05:02:33.317Z plugin_loaded
2026-11-15T05:02:33.431Z agent_resumed seq=8261
2026-11-15T05:02:33.441Z business_host_context pid=19902 pluginRoot=/Users/kibyou/projects/mowulink-dsh-image-native/worker/dshworker
2026-11-15T05:02:34.876Z business_started
2026-11-15T05:02:34.975Z router_connected
```

更新后主会话及宿主 PID 保持原值，飞书和 router 均 connected。12:46 的旧收图记录早于此次部署，新的原生验收采用下面的 13:10 入站事件。

### 真实飞书入站

按训练员授权，小栗帽在飞书的玉藻十字私聊中选择测试 PNG，点击发送一次。飞书界面显示独立图片消息，DSH 同一个主会话出现图片气泡。通过 Computer Use 点击该气泡，原图预览显示左绿底黑方块、右黄底蓝圆和底部红菱形。

| 项目 | 真实值 |
| --- | --- |
| 事件时间 | 2026-11-15 13:10:45.146 UTC+8 |
| 原生事件 | `user/message`，seq=8283，`content[].type=image` |
| 飞书 message_id | `om_x100b60a7a3f524acb0ef1d5c68134e8` |
| 原生 message id | `98c0968c-fb0f-45b0-84eb-9cc66244b0dd` |
| 入站路径 | `/Users/kibyou/.hermes/state/dshworker/feishu-images/eb62a77b4fc2bbc080b2069ecf118e31aa4d42dbb69e1d841961f4f3f0a7156b.png` |
| DSH 内容寻址对象 | `/Users/kibyou/.dsh/attachments/v1/objects/fa/fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f` |
| 字节与尺寸 | 3305 字节，800×450 |
| SHA256 | `fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f` |

原生日志读取使用 `zstd -dc` 解压会话的串联帧，再筛选 seq≥8261 的 `user/message`。以下为该事件的实际附件字段。

```json
{"type":"image","attachment":{"attachmentId":"sha256:fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f","mediaType":"image/png","width":800,"height":450,"bytes":3305,"name":"eb62a77b4fc2bbc080b2069ecf118e31aa4d42dbb69e1d841961f4f3f0a7156b.png"}}
```

对源图、B 飞书下载原件、A 入站缓存、DSH 内容寻址对象和 C 溪流落地文件执行 `shasum -a 256`，实际输出一致。DSH 对相同图片复用已有内容寻址对象，新入站的关联证据为 seq=8283 及相同的原生 message id、飞书收据。玉藻在原会话第 8310 条公开回复中独立确认 native attachment 引用、原始字节和实际画面。

```text
fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f  /Users/kibyou/Workspace/TamamoCross/evidence/b-acceptance/media-repro-20261115-121956-32381.png
fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f  /tmp/dsh-b-feishu-original-630e315c.png
fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f  /Users/kibyou/.hermes/state/dshworker/feishu-images/eb62a77b4fc2bbc080b2069ecf118e31aa4d42dbb69e1d841961f4f3f0a7156b.png
fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f  /Users/kibyou/.dsh/attachments/v1/objects/fa/fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f
fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f  /Users/kibyou/.hermes/attachments/router/fae67cd73585da2064cf3f2e61c96a024a5569b63e23cb4825fa3438e9949f0f.png
```

### 最终跨平台回归

`f282119` 的 Mac／Windows Node 22、26 四组原生 CI 全部 success，公共根模块每组均为 37/37 SHA256 一致。[最终原生 CI](https://github.com/KibyouMoe/mowu-link/actions/runs/43679210385)

| 平台与 Node | Codex 通过 / 失败 / 跳过 / 总数 | DSH 通过 / 失败 / 跳过 / 总数 | router |
| --- | --- | --- | --- |
| Mac 22、26 各组 | 834 / 0 / 8 / 842 | 816 / 0 / 8 / 824 | 每组 138/138 PASS |
| Windows 22、26 各组 | 818 / 0 / 24 / 842 | 815 / 0 / 9 / 824 | POSIX 服务自测在 Mac 运行 |

Mac 本机完成真实 DSH／飞书／router 图片验收，Windows 完成原生 CI 的路径、协议和图片投递回归。Windows 交互式 DSH／飞书客户端目测待当地机器验收。
