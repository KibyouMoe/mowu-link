---
name: mowulink-worker-skill
description: 作为 mowulink Worker 冷启动、接收和执行工单、回传结果、提问、传图及与其他 Worker 协作。适用于 Codex、DSH 和已接入的其他引擎。
---

# mowulink Worker

以本机已部署的 Worker 身份在原主会话工作。明确的项目工单使用任务通道，普通聊天、补充引导和 peer 私聊使用消息通道。公开输入、回复和图片沿既有飞书绑定同步。

本文件按源码 `82516af01d5de749293fc1ec504f83f85cce7f83` 校准，包含独立使用所需的字段、请求工具和验收判据。先读取实际健康信息，再采用该引擎已声明的能力。

## 冷启动需要的部署输入

| 输入 | 如何取得与验收 |
| --- | --- |
| 本机 `identity.json` 的绝对路径 | 由安装者提供，读取下节字段，核对分配给自己的名字 |
| `configFile`、`stateDir` | 从 identity 读取并展开 `~`，保留实际目录与大小写 |
| router 地址与令牌文件 | 读取运行配置 `router.url`、`router.tokenFile`。环境覆盖使用 `CODEXWORKER_ROUTER_URL`、`CODEXWORKER_ROUTER_TOKEN_FILE` |
| 本机 Worker HTTP 地址与令牌文件 | 配置 `host`、`port`、`tokenFile`。通常为回环地址，和 router 凭据分别核对 |
| 原主会话、工作目录 | `GET /health` 的 `integrations.selectedThreadId`、`workspace`。以真实绑定为准 |
| 可用引擎与执行权限 | 引擎能读取原会话、提交输入、观察进度、停止指定执行，工具授权按当前环境处理 |
| 飞书应用和聊天绑定 | 已有本机私密配置，健康信息中 `integrations.feishu.connected=true`，另做真实往返验收 |

缺项时向溪流列出具体缺失项和已完成检查，等待对应部署资料。令牌保留在本机文件，公开汇报只摘取状态与非敏感回执字段。

macOS 可用 `python3`，Windows 可用 `py -3`。下文 `python3` 命令在 Windows 替换为 `py -3`。JSON 文件统一保存为 UTF-8，Windows 路径可写成 `C:/Users/Keith/...`，避免反斜杠转义。Python 请求工具也接受 UTF-8 BOM。

## 可复制的 HTTP 请求工具

在自己的操作目录保存以下内容为 `mwu.py`，再保存本机 `mowulink-local.json`。这两个文件由本节内容生成。

```json
{
  "routerUrl": "http://127.0.0.1:8790",
  "routerTokenFile": "~/.hermes/secrets/router.token",
  "bridgeUrl": "http://127.0.0.1:8905",
  "bridgeTokenFile": "~/.hermes/secrets/router.token"
}
```

将四个值换成安装者给出的实际配置。远程 router 使用实际局域网或已授权地址。本机 bridge 保持自己的地址。两份令牌文件可以相同，认证头各自独立。

```python
import argparse
import hashlib
import json
import mimetypes
import pathlib
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


parser = argparse.ArgumentParser()
parser.add_argument("service", choices=["router", "bridge"])
parser.add_argument("method", choices=["GET", "POST"])
parser.add_argument("path")
parser.add_argument("--config", default="mowulink-local.json")
parser.add_argument("--data", help="UTF-8 JSON request file")
parser.add_argument("--upload", help="absolute image path for POST /attachments")
parser.add_argument("--out", help="destination for GET /attachments/ID")
parser.add_argument("--stream", action="store_true")
args = parser.parse_args()
if not args.path.startswith("/") or args.data and args.upload:
    parser.error("Use an API path and one request body source")
config = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8-sig"))
token_path = pathlib.Path(config[args.service + "TokenFile"]).expanduser()
token = token_path.read_text(encoding="utf-8-sig").strip()
base = config[args.service + "Url"].rstrip("/")
headers = {"X-Router-Token": token} if args.service == "router" else {
    "Authorization": "Bearer " + token
}
body = None
if args.data:
    value = json.loads(pathlib.Path(args.data).read_text(encoding="utf-8-sig"))
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    headers["Content-Type"] = "application/json"
if args.upload:
    path = pathlib.Path(args.upload).expanduser()
    if args.service != "router" or args.method != "POST" or args.path != "/attachments":
        parser.error("Upload uses router POST /attachments")
    if not path.is_absolute() or not path.is_file() or not 0 < path.stat().st_size <= 100000000:
        parser.error("Image must be an absolute regular file of 1..100000000 bytes")
    body = path.read_bytes()
    headers["X-Attachment-SHA256"] = hashlib.sha256(body).hexdigest()
    headers["Content-Type"] = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
if args.out and (args.method != "GET" or not args.path.startswith("/attachments/")):
    parser.error("Download uses GET /attachments/ID")
request = urllib.request.Request(base + args.path, data=body, headers=headers, method=args.method)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
sys.stdout.reconfigure(encoding="utf-8")
try:
    with opener.open(request, timeout=60) as response:
        if args.stream:
            for line in response:
                print(line.decode("utf-8").rstrip("\r\n"), flush=True)
        else:
            data = response.read()
            if args.out:
                expected = args.path.rsplit("/", 1)[-1]
                actual = hashlib.sha256(data).hexdigest()
                if actual != expected or response.headers.get("X-Attachment-SHA256") != actual:
                    raise ValueError("Attachment SHA256 mismatch")
                output = pathlib.Path(args.out).expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                if output.exists() and output.read_bytes() != data:
                    raise ValueError("Destination already contains different bytes")
                output.write_bytes(data)
                print(json.dumps({"path": str(output), "size": len(data), "sha256": actual}))
            else:
                result = json.loads(data.decode("utf-8"))
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if result.get("ok") is False:
                    sys.exit(3)
except urllib.error.HTTPError as error:
    print(json.dumps({"httpStatus": error.code, "body": error.read().decode("utf-8", errors="replace")}, ensure_ascii=False))
    sys.exit(2)
except (OSError, ValueError, urllib.error.URLError) as error:
    print(json.dumps({"error": str(error)}, ensure_ascii=False))
    sys.exit(2)
```

请求工具直连配置中的服务。60 秒用于单次网络观察，任务运行时间由引擎与下文的进度判据管理。写请求异常时先查询原请求回执，再决定重试。

```bash
python3 mwu.py bridge GET /health
python3 mwu.py router GET /workers
```

健康验收分别记录以下字段。

- 进程在线使用 `alive`、`ready`、实际 PID 和启动时间。
- 引擎可用使用原会话的真实读取、一次输入及对应回合结果。当前 `managerReady=false` 的 staged 适配器仍可能具有部分可用能力，逐项读取 `capabilities`。
- 飞书使用 `integrations.feishu.connected` 和真实入站、出站 `message_id`。
- router 使用 `integrations.router.registered`、`connected`、`GET /workers` 的本人记录。
- 原生外部输入使用 `capabilities.externalChatInput="native-user-input"`，再检查忙碌回合里的实际 user 输入和图片引用。

## 1. 身份与注册

`identity.json` 的实际结构如下，示例身份供新工位按分配值填写。

```json
{
  "bridge": "CodexWorker",
  "name": "ExampleWorker",
  "display": "示例工位",
  "port": 8905,
  "configFile": "~/.codex/state/codexworker/desktop-config.local.json",
  "transport": "desktop-pipe",
  "stateDir": "~/.codex/state/codexworker"
}
```

| 字段 | 判据 |
| --- | --- |
| `bridge` | 插件标识，Codex 为 `CodexWorker`，DSH 为 `DshWorker` |
| `name` | router 精确身份，团队取英文罗马字，如 `OguriCap`、`TamamoCross`。大小写保留。服务端校验非空且最多 128 字符 |
| `display` | 面向人的名称，可用中文 |
| `port` | 本机已分配且唯一的 bridge 端口，验收时核对真正监听者 |
| `configFile` | 本机运行配置路径 |
| `transport` | 已安装的适配器。Codex 为 `desktop-pipe`，DSH 为 `dsh`。第三引擎按自己已注册的适配器配置 |
| `stateDir` | 持久控制状态目录，重开后继续使用原目录 |

现有插件已经负责注册、SSE、心跳和结果回传时，直接使用该插件。查询其健康信息和回执即可。

新适配器或明确的注册恢复可以生成 `register.json`。将以下代码保存为 `make-register.py`，参数为真实 identity 绝对路径和已核对的运行版本。

```python
import json
import pathlib
import sys

identity = json.loads(pathlib.Path(sys.argv[1]).expanduser().read_text(encoding="utf-8-sig"))
body = {"name": identity["name"], "version": sys.argv[2], "note": identity["display"]}
pathlib.Path("register.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
```

```bash
python3 mwu.py router POST /register --data register.json
```

注册字段为必填 `name`，可选 `version`、`note`、`control_url`、布尔值 `control_sse`。
`version` 填实际运行版本。`control_url` 填 router 真正可达的 Worker HTTP origin，DSH 管理器取消接口使用它。
实现了 SSE `task_cancel` 消费与执行确认的适配器可报 `control_sse=true`。
注册返回 `ok=true`，`new` 表示首次登记，`queued` 为排队数。`online` 还取决于实际 SSE 连接。

## 2. 接活、串行和目标模式

```bash
python3 mwu.py router GET "/tasks?worker=ExampleWorker&limit=100"
```

将查询中的名字换成 identity 的 `name`。`GET /tasks` 返回状态快照，实际派单使用下列 SSE 入口。

```bash
python3 mwu.py router GET "/stream?name=ExampleWorker&version=0.3.0" --stream
```

该 SSE 命令仅供新适配器的唯一消费者使用。同名新连接会替换旧连接，先确认现有插件没有持有本人连接。

| SSE 事件 | 动作 |
| --- | --- |
| `task` | 解析 JSON，读取 `payload.task ?? payload`，以 `task_id` 进入持久去重和串行队列 |
| `task_cancel` | 立即走第 7 节的指定执行取消，可在长任务期间处理 |
| `worker_message` | 走第 9 节普通消息入站，与飞书采用同一原生用户输入路径 |
| `ping`、注释行 | 维持连接，按 SSE 帧边界处理，等待完整 JSON |

断开后以 1、2、4 秒逐步退避，最多 30 秒，重新注册并打开唯一 SSE。router 会重新投递尚未结束的任务。

新适配器需要保留以下控制记录，写盘完成后再触发原生输入。

- 精确 `task_id`、原 `threadId`、原生 `turnId`、稳定的输入 `messageId`。
- 原始请求正文、派发阶段和确认结果，包括 `accepted`、`unknown`、明确未派发。
- 最终 `/result` 正文、发送回执、取消标记。

同一 `task_id` 再到达时先读取这些记录。已接受的输入继续观察原 turn，已结束的任务读取结果，未知派发先核对原生记录。保持原 messageId 和请求正文。当前 router 的正式任务通道由结果与取消确认推进，任务消费使用上述控制记录完成幂等。

一个工位串行执行项目任务。训练员正在使用主会话时，正式任务留在队列，空闲后自动继续。飞书和 peer 普通引导立即进入原会话，保持当前 task_id，公开显示发送者与附件。

接正式工单时执行以下目标判据。

1. 有原生 `get_goal` 就先调用，读取工具真实返回。
2. 当前没有未完成目标时，用 `create_goal` 建立工单目标。token 预算只采用用户明确给出的值。
3. 已有目标时，把同项目的补充、纠正和验收关联到原目标，保留原主会话。独立冲突项目排队并回报现有目标。
4. 完成逐项验收后才能把目标标为 `complete`。暂停按用户明确指令处理。同一真实阻塞连续出现至少三个目标回合，含首次触发轮，并确认已无可推进步骤后才标 `blocked`。用户恢复后重新开始阻塞计数。
5. 引擎没有原生目标 API 时，明确记录能力缺失，用工单与执行记录维持该目标，回执注明实际采用的方式。

任务常见字段为 `task_id`、`worker`、`goal`、`context`、`workspace`、`urgency`、`execution_mode="goal"`、可选的 `deadline_s`。保持已绑定工作目录，先核对任务要求与实际目录。当前 deadline 用于预计完成提示，继续按真实进度等待。

## 3. 完成与失败回执

插件管理的任务由插件发送结果。手动修复结果回传或新适配器使用以下 API。

```bash
python3 mwu.py router POST /result --data result.json
```

| `/result` 字段 | 值 |
| --- | --- |
| `name` | identity 的精确 `name` |
| `task_id` | 收到的原任务号 |
| `status` | `done` 或 `failed` |
| `summary` | 实际结果、验收和剩余项，现有客户端截取最多 20000 字符 |
| `artifacts` | 数组，当前 HTTP 服务保存调用方提供的 JSON 条目 |
| `evidence` | 数组，可含 `{"type":"codex_thread","thread_id":"原会话ID","turn_id":"原回合ID"}` 及命令、日志、媒体回执等公开证据 |

`summary` 按“完成了哪些交付、验证命令与真实结果、未验项、需要用户或溪流处理的具体事项”组织。报源码、安装文件、实际装载与外部验收各自的真实状态。

完成判据为请求的交付物存在、格式与行为满足要求、对应验收已实际执行、原会话及副作用状态已确认。仍在等审批、答案、原生执行结果或必需的人眼验收时，继续等待并上报当前进展。

失败摘要写具体阶段、原始报错、已取得的结果、保留的位置和下一步所需条件。检查原 turn 的 `error`、状态及工具退出信息。原生 `completed` 后仍有来源问题等待回答时，先走原问题的回答与续接，再判断整张工单的完成状态。

回执 HTTP 200 后继续核对 `state`。`duplicate=true` 表示服务端已有终态结果，读取原任务确认内容。`state=cancelling` 表示仍等待执行停止。取消确认使用第 7 节接口。

## 4. 上报溪流与重开协调

```bash
python3 mwu.py router POST /notice --data notice.json
```

| `/notice` 字段 | 约束 |
| --- | --- |
| `worker` | 本工位 identity.name，必填 |
| `kind` | 自描述类别，如 `worker.status`、`worker.blocked`、`worker.restart_request`，默认 `worker.notice` |
| `text` | 必填，1–8000 字符，写事实、证据和具体需要 |
| `urgency` | `normal` 默认，映射 trigger 的 `notify`。`urgent` 映射 `event`，能引导溪流当前回合 |
| `attachments` | 可选，`[{"id":"已上传图的64位SHA256","name":"显示名.png"}]` |
| `message_id` | 可选稳定标识，最多 128 字符，由字母、数字、`-`、`_` 组成，重试沿用 |

先处理当前授权内可验证、可恢复的问题。需要新增授权、凭据、外部操作者，或故障影响其他服务、原会话/投递结果未知时，上报具体对象和错误。消息投递的幂等依据是持久标识与原正文，重试保持两者一致。

验收 `ok=true`、`trigger_id`、`event_id` 和 `level`。HTTP 200 且 `ok=false,error=trigger_unavailable` 表示上报尚未成功。

自己需要重开时，先保存原会话、目标、stateDir、进度、应用路径或 bundle id、旧 PID/启动时间及待验项。核对工位地址与本机进程归属，优先请同一台机器上的在线 Agent 按自身权限和既有审批流程协助正常退出、打开原客户端。同机没有在线 Agent 时，用 `worker.restart_request` 的 urgent notice 请溪流联系训练员。训练员很少注意到本端直接发出的选择卡片，重开协调默认走上述协作路线。

恢复验收记录旧进程退出、新 PID/启动时间、唯一 writer、同一 threadId、飞书/router 连接与一次真实收发。遇到拒绝返回原文和具体对象。已有服务的重启使用 router 的 `POST /ops/services/{name}/restart`，可用名字先通过 `GET /ops/services` 查询并沿任务授权执行。

## 5. 提问与四字段回答

优先使用引擎原生提问工具。已集成的 Worker 会沿原 questionId 投递卡片并回填答案。正式工单发起者为溪流时，提问由溪流回答。普通训练员会话的问题走训练员绑定。

查询接口为 `GET /bridge/questions/{questionId}`，回答接口为 `POST /bridge/questions/{questionId}/answer`。
questionId 使用当前问题的真实 UUID，原通知会提供完整 URL。GET 返回 `questionId`、`threadId`、`questions`、`recipient`、`status`、`answers`、`sourceTurnId` 等。每次按这些原始值核对。将原通知中的真实回答 JSON 保存为 `answer.json`，用 HTTP 工具的 bridge 认证执行 POST。

`/answer` 的请求字段如下。

| 字段 | 精确来源 |
| --- | --- |
| `actor` | 固定小写 `hermes`，对应由溪流回答的问题 |
| `threadId` | GET 与原问题通知中的原会话 ID |
| `taskId` | `recipient.taskId`，没有则显式 `null` |
| `nonce` | 原 `worker.question` 通知中的值。GET 问题接口没有返回 nonce |
| `answers` | 字符串数组，每题一条、按原顺序，填写真实回答，长度各 1–8000 字符 |

溪流在原通知中获得可复制的回答 JSON。Worker 本机需要诊断原 nonce 时，读取 `stateDir/question-actions.json` 的 `entries[questionId].nonce`。nonce 保持原值。跳过用原通道的跳过动作，协议答案为 `（用户选择跳过此问题，未提供答案）`。

| 错误 | 检查与下一步 |
| --- | --- |
| 401 `unauthorized` | 本机 bridge 使用 Bearer 认证，核对它自己的 tokenFile |
| 403 `loopback_required` | 生命周期与回答端点限制回环访问，由 Worker 本机获授权的操作者执行 |
| 403 `question_recipient_mismatch` | 核对 recipient.kind 为 hermes、actor 的大小写、原 threadId、原 taskId 或 null、原 nonce |
| 404 `question_not_found` | 核对调用的是问题所属工位与原 questionId |
| 409 `selected_target_changed`、`source_task_ended`、`question_closed` | 查询原问题与原任务，按真实关闭或绑定状态上报 |
| 400 `invalid_question_answer` | 按题数填写真实非空答案，保留原顺序 |

`answer_pending`、`dispatching` 继续查询。`answered` 或 `skipped` 后核对 `answers`、`answeredBy`、`cardUpdated` 和原生答案/面板。来源 turn、任务或绑定失效时会过期，当前问题采用 `source-turn-no-independent-ttl` 策略。

过期卡片对应的原生面板残留时，可在本机对原 questionId 执行 `POST /bridge/questions/{id}/sync-native`，正文 `{}`。先检查当前能力和同步状态。`nativePanelSync=accepted` 还需要原生界面关闭证据。`unknown`、`dispatching` 保留回执继续核对。过期撤回保持 `status=expired`、`answers=null`。

新引擎若只具备普通聊天而缺少原生提问能力，可用 `POST /bridge/questions` 发布一个公开问题，必填 UUIDv4 `questionId`、原 `threadId`、`questions:[{"title":"完整问题","options":["选项一","选项二"]}]`。允许 1–3 题，每题可选 1–6 个选项。绑定原生 async 问题还需要原 `sourceTurnId` 和等长的 `sourceQuestionItemIds`，按引擎真正返回的值填写。权限申请沿工具本身的审批机制处理。

## 6. 图片传输与验收

图片先落为真实绝对路径。输出一条独立的公开媒体消息，内容形如下一行，正文说明放在另一条消息。

```text
MEDIA: /absolute/path/probe.png
```

Windows 的示例为 `MEDIA: C:/Users/Keith/Workspace/probe.png`。保留源字节，跨工位传递使用上传后的附件 ID。当前 router 接受 PNG、JPEG、GIF、WebP，单图 1–100000000 字节，每条最多 10 个附件。

```bash
python3 mwu.py router POST /attachments --upload /absolute/path/probe.png
```

核对上传回执 `attachment.id`、`sha256`、`size`、`media_type`，其中 id 与源文件完整 SHA256 相同。

```bash
python3 mwu.py router GET /attachments/实际64位附件ID --out /absolute/path/received.png
```

请求工具验证下载内容、URL 中的附件 ID 和响应 SHA256 头一致，再写接收文件。

向溪流交付使用 `/notice` 的 attachments，向 peer 使用第 9 节消息附件。公开回执列出发送者、接收者、实际落地路径、字节数、64 位 SHA256、router message_id 或 trigger_id。

飞书图片验收另列真实飞书 `message_id`、`deliveryType=image|file`、源字节数与 SHA256。当前发送器在明确的图片大小拒绝后可转为独立 file 消息。自动出图通道已经处理的图片沿原回执核对。验收同时检查独立媒体消息和实际显示/下载内容，记录接收字节 SHA256 与源文件的比较结果。

| 本机位置 | 用途 |
| --- | --- |
| `stateDir/image-output-state.json` | `receipts[messageId]` 的媒体发送阶段、状态、资源 key 和结果，结果含 feishuMessageId、imageBytes、imageSha256、deliveryType |
| `stateDir/desktop-manager-state.json` | 自动公开图片 `autoImages`、原生输入 receipts、公开问题、会话与选择信息 |
| `stateDir/feishu-images/` | 飞书图片与图片文件入站缓存 |
| `stateDir/worker-messages/<本工位名SHA256>/images/` | peer 图片原字节，目录哈希为 UTF-8 identity.name 的 SHA256 |
| `stateDir/worker-messages/<本工位名SHA256>/inbox/`、`outbox/` | 普通消息的原请求、派发和媒体回执 |

出现“客户端显示图片，飞书没有”时，按原生公开 item/`deliverables/presented` 或 MEDIA、`autoImages`、媒体发送账本、飞书 message_id 的顺序定位。`unknown` 状态先核对原媒体 UUID 和资源回执，保留原请求与源字节。

## 7. 取消必须停止执行

取消入口为 `POST /tasks/{task_id}/cancel`。router 可能返回 `state=cancelling`，继续等待 Worker 原生停止确认。现有 bridge 提供 `POST /manager/tasks/{task_id}/cancel`，router 的 `control_url` 会指向所属工位。

| 场景 | 成功判据 |
| --- | --- |
| 尚未原生派发 | 精确移除该任务输入、写入取消标记，后续重投保持取消 |
| Codex 正在执行 | 精确绑定 task_id、messageId、threadId、turnId，原 turn 终态，`executionStopped=true`、`pendingCommands=0`、`pendingTerminals=0`。`nativeStatus=interrupted` 是其中一项证据 |
| DSH 正在执行 | 对原 agent 调 `agent.cancel({kind:"user"})`，等待原生 idle 与原 turn 终态。观察窗口结束而执行仍忙时维持 cancelling |
| 原生状态未知或停止失败 | 保留 cancelling、真实错误与关联 ID，向溪流上报 |

支持 SSE 控制的新适配器确认停止后调用 `POST /tasks/{task_id}/cancel-ack`，正文含 `name` 和 `outcome`。outcome 至少包含相同 `task_id`、`ok=true`、`state="cancelled"`，并携带实际原生停止证据。API 返回 `state=cancelled` 后再放行后续任务。

task_id 的取消标记需要落盘。较早任务的延迟取消只作用于其原 turn，后续回合继续按自己的身份运行。

## 8. 心跳、进度和停滞

新适配器的心跳使用 `POST /heartbeat`。现有集成默认约 5 秒，底层独立 RouterClient 默认 30 秒，读取实际配置的 `router.heartbeatMs`。下例示意正在处理训练员普通输入的工位，字段值按真实状态填写。

```json
{
  "name": "ExampleWorker",
  "state": "running",
  "task_id": null,
  "turn_source": "trainer",
  "progress_at": 1794740000.25
}
```

| 字段/状态 | 语义 |
| --- | --- |
| `last_seen` | router 最近接到心跳的时间 |
| `progress_at` | 最近真实进展的 Unix 秒，可带小数，服务端单调保存 |
| `task_id` | router 任务精确 ID，训练员普通聊天可为 null |
| `turn_source` | `router`、`trainer` 或 null |
| 空闲 | `state="idle"`、task_id=null、turn_source=null，结束时保留真实最后进展时间 |

真实进展包括新原生事件序号、正文增量、工具开始/结束或输出、状态变更、收到原生回答。轮询时钟、elapsed、重复快照和仅连接存活的心跳使用其观察语义。

默认 `noProgressTimeoutMs=1800000`，可通过本机配置或 `CODEXWORKER_NO_PROGRESS_TIMEOUT_MS` 设置正毫秒值。连续到达该间隔且无新事件时报告“无进展超时”，携带原任务、turn 和最后进展时间。已知等待提问答案期间继续保留等待状态。持续产出新事件的长任务继续执行。

先核对原生执行、provider 请求、工具子进程、审批/提问等待和连接状态，给出具体证据。引擎在线、模型可运行、飞书/router 连通分别验收。

## 9. peer 普通私聊与防回环

已集成的工位优先使用 `bridge_send_worker_message`，参数为 UUID `messageId`、`to`、公开 `text`、可选 `replyTo` 和 `attachments:[{id,name}]`。
HTTP 对应 `POST /messages`，字段如下。

| 字段 | 值 |
| --- | --- |
| `message_id` | 保存一次并复用的 UUID |
| `from`、`to` | 精确 identity.name，溪流的 peer 身份为 `Hermes` |
| `text` | 真实公开正文，1–20000 字符 |
| `reply_to` | 可选，原消息 UUID，属于同一对工位 |
| `attachments` | 可选，最多 10 个已上传图片 `{id,name}` |

新消息通常返回 202、`status=queued`。`GET /messages/{message_id}` 查询原回执。
收件方持久接受并完成原生派发确认后 `POST /messages/{message_id}/ack`，正文 `{"worker":"接收方的精确name"}`。
`accepted` 表示收件方已确认接收，随后按实际回复与交付验收。

新消费者也可以 `GET /messages?worker=名字&after=序号&limit=100` 读取未 ACK 消息，按 `has_more` 和 `next_after` 分页。历史查询使用 `view=history` 和 `before`/`next_before`，读取历史保持只读。

同一 message_id 的重复请求合并到原记录，正文变化返回 409 `message_id_conflict`。保存入站游标与逐条 ACK。普通私聊保持原 task 与主会话，采用飞书同一原生用户输入路径。

客户端和飞书的显示正文采用以下结构，时间与图片使用对应通道的时间和媒体组件。

```text
来自 TamamoCross：
玉藻收到
```

模型需要的 message_id、reply_to、发送者身份和防回环规则放入 metadata/system。显示组件读取投递记录，重复读取保持幂等。收到回复后按内容判断是否需要进一步交流，单纯 ACK 或对方已要求结束时完成该次交流。明确的一次测试回复按一次执行，用原消息关联，接着观察双向记录的条数保持稳定。

## 10. 本次真正使用的模型与思考强度

| 引擎 | 配置与实际依据 |
| --- | --- |
| Codex Desktop | 当前客户端/会话选项与 CODEX_HOME 下 config.toml 的 model、model_reasoning_effort。按该 turn 的原生 turn_context 核对 model 和 effort |
| DSH | 插件显式 provider/model/reasoningEffort 优先于 agentDefaultModel.currentSelection() 的初始化回落。原会话已有的 model/selection 与实际 request/header 继续由引擎管理 |
| 第三引擎 | 记录该次引擎解析后的 provider/model/effort、请求或 turn 标识、时间及配置来源，再用真实请求元数据核对 |

Codex 的原生日志默认在 `~/.codex/sessions`，设置了 CODEX_HOME 时使用该目录下的 sessions，Worker 的 `publicJournalRoot` 可显式指向它。按原 threadId 查找名称含该 ID 的 rollout JSONL 文件。stateDir 单独管理控制状态。把下面代码保存为 `codex-model.py`，参数为真实 journal 路径和本次原生 turnId。

```python
import json
import pathlib
import sys

found = None
with pathlib.Path(sys.argv[1]).expanduser().open(encoding="utf-8") as stream:
    for line in stream:
        row = json.loads(line)
        payload = row.get("payload", {})
        if row.get("type") == "turn_context" and payload.get("turn_id") == sys.argv[2]:
            found = {key: payload.get(key) for key in ["turn_id", "model", "effort"]}
if found is None:
    raise SystemExit("No exact native turn_context found")
print(json.dumps(found, ensure_ascii=False))
```

DSH 从 `stateDir/dsh-plugin-state.json` 的 threads 中按主 threadId 找到原生 `sessionId`。默认日志位于 `~/.dsh/sessions` 下，可按该 sessionId 目录查找 `session.v4.jsonl.zstd`，自定义位置由安装者提供。`request/header` 的 `data.header.config` 记录 provider、model、reasoningEffort，`data.header.adapterDefaults.reasoningEffort=true` 时报告 effort 使用适配器默认。按前面的 `turn/start`、`step/start` 关联 turn/step，并保存请求事件 seq/time。当前 request/header 的 data 字段为 header 和 reason。`model/selection` 表示选择状态，实际请求以 request/header 为依据。

压缩日志 `session.v4.jsonl.zstd` 含多个 Zstd 帧，可用具有 `zstdDecompressSync` 的 Node 运行以下 `dsh-model.mjs`。参数依次为真实日志路径、原生 turn 数字、step 数字。

```javascript
import fs from "node:fs";
import { zstdDecompressSync } from "node:zlib";

const input = fs.readFileSync(process.argv[2]);
const chunks = [];
let offset = 0;
while (offset < input.length) {
  const result = zstdDecompressSync(input.subarray(offset), { info: true });
  if (result.engine.bytesWritten <= 0) throw new Error("No frame progress");
  offset += result.engine.bytesWritten;
  chunks.push(result.buffer);
}
const events = Buffer.concat(chunks).toString("utf8").trim().split("\n").map(line => JSON.parse(line));
const matches = [];
let turn = null, step = null;
for (const event of events) {
  if (event.type === "turn/start") { turn = event.data.turn; step = null; }
  if (event.type === "step/start") { turn = event.data.turn; step = event.data.step; }
  if (event.type === "request/header" && turn === Number(process.argv[3]) && step === Number(process.argv[4]))
    matches.push(event);
  if (event.type === "turn/end") { turn = null; step = null; }
}
if (!matches.length) throw new Error("No exact native request/header found");
for (const event of matches) {
  const header = event.data.header;
  console.log(JSON.stringify({ seq: event.seq, time: event.time,
    turn: Number(process.argv[3]), step: Number(process.argv[4]),
    provider: header.config.provider, model: header.config.model,
    reasoningEffort: header.adapterDefaults?.reasoningEffort === true
      ? "adapter-default" : header.config.reasoningEffort ?? "unrecorded" }));
}
```

模型验收回执写原生 turn/step、请求时间、实际 model、实际 effort 或默认/未记录，以及证据路径。工位名字、profile 默认值、插件启动日志可用于定位，最终报告使用该次请求记录。

## 安装与冷启动自测

把本目录复制为目标引擎技能目录下的 `mowulink-worker-skill/SKILL.md`。Codex 默认目录为 `~/.codex/skills`，Hermes 常见目录为 `~/.hermes/skills`，DSH 使用该宿主已配置的技能目录。按宿主正常方式刷新技能发现，并确认 name 与 description 可见。

只带这份文件检查以下情景，每项列出采用的字段、请求与判据。

- 新工位持有 identity 与本机配置，查询健康、本人注册和唯一 SSE，能指出原主会话与实际执行能力。
- 同一任务投递两次，只有一次原生执行，原 task_id/messageId/turnId 可查，结果回传采用 done 或 failed。
- 普通飞书或 peer 引导在现有 turn 内成为 user 输入，时间早于该 turn 结束，当前任务 ID 保持，任务列表新增数为零。
- 回答溪流来源问题时取得原 actor/threadId/taskId/nonce，遇到 403 按错误体核对来源与回环限制。
- 两端传图取得相同大小与 SHA256，飞书有独立 image/file 消息及真实 message_id，客户端实际渲染原图。
- 取消时观察原生执行和工具停止后确认，原任务结果与后续回合分别可查。
- 长任务持续产生事件时继续运行，停滞时报告真实最后进展时间。等待人类回答保持等待语义。
- 需要自己重开时优先联系同机在线 Agent，缺少协作方时上报溪流转告训练员，重开后恢复同一主会话。
- 模型报告来自本次原生请求，Mac 与 Windows 的真实验收、平台模拟和未验项分别记录。

### 外部依赖清单

本技能包含控制流程、HTTP 工具、字段与诊断脚本。执行仍需外部提供已授权的身份/绑定、router 与 bridge 服务、令牌文件、原会话日志和目标引擎工具。飞书实测需要该工位自己的应用配置与聊天绑定。Python 用于 HTTP 和 JSON，DSH 压缩日志诊断需要支持相应 Zstd API 的 Node。第三引擎需要提供原会话恢复、输入、进度、提问/媒体和指定执行停止的适配能力，逐项声明未实现项。取得这些部署输入后，按本文件的命令和判据完成工作。
