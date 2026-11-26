# 魔物链接重做版 · 正式工单主线

本目录对应 Wiki 版本 `9db82a32af701ba5e8af39a545f9bf0c771b5600` 的 00–04 和 02A。此切片覆盖有指定处理人的文字工单，从建单、接管和原生输入交付，到 Agent 主动报完工、溪流查验和明确结单。

## 本地复跑

在本仓库根目录使用 Python 3.11 或以上版本，只需标准库。

```sh
python3 -m rewrite.devloop
python3 -m unittest -v rewrite.test_mainline
```

第一条启动真实的重做版 Router 和两个客户端，使用系统分配的 `127.0.0.1` 端口，约 20 秒后输出一次完整交接的 JSON。每次运行生成新的身份凭据、会话内工单与派发号。Router、Worker、宿主适配器、模拟宿主的输入账本分别使用临时 SQLite 文件，退出后清理。第二条用可控时钟复验边界和故障恢复。

**怎样读结果。** `host_receipt` 来自目标宿主夹具自己的输入账本，带会话、输入号、投递号和正文摘要。`after_turn` 应为 `working`。夹具 Agent 在该输入专属的 MCP 管道调用 `worker_report` 后，W04 回应 `completed_reported` 并给出原派发号。Hermes 侧收到一条含成果正文、同一工单号的 `Notify`。最后由模拟溪流显式验收，才出现 `after_review: closed`。

## 边界与证据

| 程序入口 | 拥有的状态及操作 |
| --- | --- |
| `orders.py` | Router 的工单、处理权、派发号、版本、正式报告和幂等回执。在一个 SQLite 事务内变更工单并写入投递事件。待派和已持有均占用该 Worker 的唯一派发位置。 |
| `delivery.py`、`wire.py` | 身份绑定、HTTP、按接收方隔离的 SSE 帧和连续收件游标。开发流每次返回一批并关闭，消费者带游标重连。C02 仅确认已持久收件。 |
| `hermes.py` | 溪流侧的建单、查单、明确验收，以及待交通知。Hermes 连续空闲满十秒后逐条交付，取得宿主凭据才出队。 |
| `worker.py` | Worker 的收件、持单副本、飞书私聊消息映射和待交报告。先 W03，后调用适配器。持久保存 W04 请求，按 Router 回执中的原派发号释放。 |
| `adapter.py`、`agent_tools.py` | 目标宿主会话、投递意图、宿主接收凭据和原生输入关联。MCP 由宿主接入方绑定到当前输入，模型只给工单号、派发号及成果；普通私聊输入即使知道派发号也不能代报。 |
| `devloop.py` | 隔离装配与夹具。模拟 Agent 通过真实管道调用 MCP，模拟宿主独立落库后返回凭据；模拟溪流通过实际管理适配器收结果。 |

实测跨越本机 HTTP/SSE、MCP 管道与独立 SQLite 事务，包含 W01/W02、C01/C02、A02/A04、W03、W04 的主动完工、A06 的验收结单。Worker 上报时取真实适配器的忙闲接口；开发用的采样间隔与在线租期是可替换参数。回环采用明文 HTTP 和随机开发凭据，正式 HTTPS 与生产凭据不在本次接入范围。

宿主已收但凭据丢失时，适配器只按同一投递号查回。凭据必须匹配目标会话和原内容；无法查回就保留待核。测试还覆盖 W03/W04 写入成功后丢回包、已接派发重投、双单同时争取一个空位、忙闲翻转、失效派发首次迟报以及替换适配器。

首段的 Agent、Codex/Hermes 宿主、飞书输入与验收人均为夹具。真实客户端、飞书卡片、Trigger/附件、候选材料、转派与取消、完整掉线入池、询单和提问后续接入。各入口只接受本切片明示的业务动作。

第二段已用本机 Codex Desktop 独立聊天验收正式工单输入，复跑方式和原生回执见 [Desktop 接入实测](desktop-check.md)。上述 `rewrite.devloop` 继续提供可离线运行的夹具回环。

后续已扩展 Router 生命周期、消息附件、问答、同伴私聊及插件。以[全量替换进度](replacement-progress.md)核对各项版本和实测边界。

独立平台入口 `python3 -m rewrite.feishu_client --config <专用测试配置>` 使用飞书官方 Python SDK。配置是仅属当前用户的 JSON，显式提供 `app_id`、`app_secret`、`trainer_open_id`、`chat_id`、平台队列的 `database` 和指向 Worker 的 `worker_connection`。普通私聊及其文件保存在 Worker 端，断开 Router 仍能投递。使用配置前需要单独的测试应用和会话。无需账号即可运行 `rewrite.test_feishu_inbound` 或官方事件解析复验 `rewrite.feishu_sdk_check`。[平台消息资源说明](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/guide/faq) [飞书 Python SDK](https://github.com/larksuite/oapi-sdk-python)
