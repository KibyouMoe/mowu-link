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

以上为本机 macOS Node v22.22.0。此次新增事件映射的 Windows 原生 CI 另随提交核对。

界面目测当前遇到 Mac 锁屏。Computer Use 返回 `The Mac is locked and automatic unlock could not unlock it. Ask the user to unlock the Mac manually before continuing.` 已发出手动解锁请求，B 的当前证据是原生真实事件、实际飞书媒体消息与原件下载校验。

## 后续

C 将核对同一图片通过 router 附件通道交给溪流后的真实落地文件、大小与 SHA256。A 将核对飞书入站的原生 image attachment 和 DSH 客户端接收，当前生产健康接口仍报告 cached-path-prompt。
