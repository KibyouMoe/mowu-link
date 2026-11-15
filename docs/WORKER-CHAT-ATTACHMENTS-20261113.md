# Worker → 溪流聊天附件

需求：以图片附件发送给另一个 agent，允许不同机器，不依赖共享文件路径。

## 图片 ID 与独立数据传输

只上传、取得 ID，暂不发送聊天通知：

~~~bash
node worker/codexworker/scripts/send-router-image.mjs --upload-only --image /path/to/image.png
~~~

标准输出仅为 `{"ok":true,"imageId":"<sha256>"}`。原图字节只走
POST /attachments，接收端通过鉴权 GET /attachments/{imageId} 下载；
聊天/任务消息只携带 ID 和必要说明，不内嵌 base64，不依赖发送端路径。
相同原图重复上传得到相同 ID。上传完成与通知发送是两个独立动作。
需要通知溪流时仍可使用下方的 --message-id 发送方式。
此入口是独立 CLI，无需重启任何服务；不改变生图工具本身的输出协议。

## 协议
1. Worker 鉴权 POST /attachments：二进制原图、Content-Length、
   X-Attachment-SHA256。router 返回 content-addressed ID、大小、MIME、摘要。
2. POST /notice：正常通知正文 + message_id + attachments:[{id,name}]；
   router 从自己的附件存储补全可信元数据，经 trigger 的 payload 送到 Hermes。
   消息没有发送端路径、token 或任意下载 URL。
3. Hermes 从自己配置的 router 鉴权 GET /attachments/{id}；
   限长、拒绝跳转、核对完整 SHA256 后原子写入本机附件缓存。
4. native wake 使用 PHOTO + media_urls/media_types；同一 trigger 事件的文字
   上下文也引用接收端已校验附件，支持 mid-turn / 合并队列时通过图片工具读取。
   下载失败保留本批事件用于重试，不能说已收图。
5. 普通文字事件保持原协议。notify 不打断，urgent 才转成 event。
   相同消息 UUID、正文、附件重复投递使用同一 event_id，不盲目重发新消息。

上传/下载都需要现有 X-Router-Token。支持 PNG/JPEG/GIF/WebP，
单图 100,000,000 字节、单条最多 10 张，原始字节不重新编码。
跨机器 Worker 与 Hermes 各自配置 router URL 和自己的凭据文件，
通过已有网络入口或认证隧道连接 router；没有放开本机 loopback 的监听限制。

## 验证
- 附件闭环：13/13（真实 HTTP router + trigger，隔离数据库/凭据/缓存；
  删除发送端源文件后接收端仍能下载完全一致字节；验证 native PHOTO 字段）。
- Node 发送端 + router client + question routing：32/32。
- ROUTER_TEST_PORT=18790 python3 server/router/selftest_router.py：77/77。
- trigger-bus 全部兼容自测：9 组，336/336。
- 生产 notify 附件 314：冷启动后自动拿到真实 gateway loop，完成 native handover、
  pre_llm_call 注入与 ACK；接收端 SHA256 与发送端一致。
- 溪流实际回复：左半橙色背景 + 白色正方形，右半蓝色背景 + 红色三角形；
  与新生成测试图完全一致。公开回复 message 283854，原 Hermes 会话保持不变。
- 跨机器部署协议已实现并做隔离目录/HTTP 验证，本轮没有第二台物理机器实测。

## 加载
已通过系统审批正常重载 router 0.3.1 和 Hermes trigger-bus 0.9.4。
实机验收发现并修复冷启动前没有人工 dispatch 就拿不到 loop 的旧缺口，
新增异步交付回归；仅 gateway 再加载一次修复，原始已确认附件不重发。
Codex Worker 进程未重启、主 threadId 不变；DSH 和 Codex 共享后台未重启。
插件有常驻线程和 singleton 锁，不做不可靠的 force reload；
gateway 正常重启才能切换插件，router 也需要正常重载。
Worker 发送 CLI 是独立调用，新代码无需重启 Worker。
不需要重启 Codex 共享后台、DSH 或创建新主会话。
