# 来源声明 / 健康判定 / 去重键（mowu-trigger 0.5.0）

0.4.0 的信封里只有 `source` 一个名字，服务端不知道这个源该带什么字段、多久没消息
算异常、重发时凭什么判重。0.5.0 补上三件事，仍然**不给任何来源写一行特例代码**：

| 能力 | 落点 | 一句话 |
|---|---|---|
| 来源声明 | `sources/<name>/source.json` | 每个源自己声明字段与策略，服务端只读 |
| 健康判定 | `GET /sources`、`GET /stats` | 按声明的 `max_silence_s` 判 `stale`，不探活 |
| 去重键 | `events.dedupe_key` + 唯一索引 | 显式 `dedupe_key`，缺省 `source:event_id` |

版本：`0.4.0` → `0.5.0`。**生产 8791 未重启**（仍跑 0.4.0，待统一 `launchctl kickstart`）。

---

## 1. 设计边界

服务端对来源的全部知识就是声明文件里的四个东西：字段表、策略、健康阈值、去重字段
清单。它**不 import 客户端、不连 IMAP/SSE、不认识任何协议**，只做：

1. **列出**：`GET /sources` 把声明原样列出来（外加按事件表算出的健康快照）；
2. **校验字段**：`POST /trigger` 时按 `schema` 做最小类型检查；
3. **判健康**：拿该 source 在 `events` 里的 `max(ts)` 和 `max_silence_s` 比。

因此「加一个源」= 加目录 + 加声明 + 加客户端脚本，**不改 `server.py`**。
未声明的 source 不会断流：照收、只在该进程内记一次 `WARNING`。

---

## 2. 声明文件

路径固定为 `sources/<目录名>/source.json`（`--sources-dir` 可改根目录，默认即项目下的
`sources/`）。启动时扫描一次，**新增/修改声明需要重启服务**。

### 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | 建议 | 与信封里的 `source` 完全一致；缺省取目录名。含空白/斜杠或超过 64 字符视为非法 |
| `title` | str | 否 | 人类可读名，缺省同 `name` |
| `mode` | str | 否 | 约定 `push`（长连接/SSE 推）或 `pull`（定时拉）。服务端不解释，只展示 |
| `schema` | obj | 否 | `{信封字段: 类型}`，类型只能是 `string` / `int` / `bool` / `list` / `dict` |
| `policy` | obj | 否 | 原样透传（`default_priority`、`wake` 等由客户端解释） |
| `health.max_silence_s` | num | 否 | 超过这么多秒没有新事件判 `stale`；`null` 表示不判 |
| `dedupe` | [str] | 否 | 该源客户端**约定**用哪些字段拼去重键，仅供人看/客户端复用；服务端不据此计算 |

未列出的顶层字段会被忽略（前向兼容）。声明文件解析失败（坏 JSON、`schema` 类型名拼错、
`max_silence_s` ≤ 0 等）只 `log.warning` 跳过该文件，**不影响启动**，也不会让该 source 断流
（它退回「未声明」待遇：照收 + warn）。

### 模板

```json
{
  "name": "<与信封 source 一致>",
  "title": "<人类可读名>",
  "mode": "push",
  "schema": {
    "text": "string",
    "actor": "string"
  },
  "policy": {
    "default_priority": 2,
    "wake": true
  },
  "health": {
    "max_silence_s": 3600
  },
  "dedupe": ["actor", "ts", "text"]
}
```

### 现有声明

- `sources/mc_chat/source.json`：`mode=push`，`max_silence_s=3600`，`default_priority=0`，
  要求 `text`/`actor` 均为 string。
- `sources/email/source.json`：`mode=pull`，`max_silence_s=7200`，`default_priority=1`，
  同样要求 `text`/`actor` 为 string。

---

## 3. 接口变化（只增不改）

### `GET /sources`（新增，不需要 token）

返回**顶层 JSON 数组**，按 `name` 排序，只含已声明来源：

```json
[{"name": "mc_chat", "title": "Minecraft 聊天栏", "mode": "push",
  "schema": {"text": "string", "actor": "string"},
  "policy": {"default_priority": 0, "wake": true},
  "dedupe": ["actor", "ts", "text"],
  "declared": true,
  "health": {"max_silence_s": 3600,
             "last_event_at": "2026-11-08T11:58:26+08:00",
             "silence_s": 41.2, "stale": false, "last_error": null}}]
```

- 不含事件正文，故与 `/health` 同级，不要求 `X-Trigger-Token`（只监听回环/白名单网段）。
- `dedupe`、`declared` 是 0.5.0 新增字段：前者把声明里的去重字段清单透出来，后者区分
  「声明来源」与 `/stats` 里的「野生来源」。其余字段与声明一致。
- 健康字段语义：
  - `last_event_at`：`SELECT MAX(ts) FROM events WHERE source=?`，没有任何事件为 `null`；
  - `silence_s`：现在与该 `ts` 的秒差（保留 1 位）；无事件为 `null`；
  - `stale`：仅当有事件、`max_silence_s` 非空、且 `silence_s > max_silence_s` 时为 `true`；
    **无任何事件时 `stale=false`**（不知道 ≠ 坏了）；
  - `last_error`：恒为 `null`。服务端不跑客户端、不探协议，没有来源侧错误可报；字段先占位，
    将来要填也只能由客户端自报，不能靠服务端猜。
- `ts` 不是可解析的 ISO-8601 时，`silence_s` 回退用该行的 `created_at`（落库时间）；
  `last_event_at` 仍原样返回客户端给的字符串。

### `GET /stats?subscriber=<name>`（原有字段 + `sources`）

```json
{"unread": 2, "last_id": 0,
 "sources": [{"name": "mc_chat", "...": "...", "declared": true,  "health": {"...": "..."}},
             {"name": "selftest", "title": "selftest", "mode": null, "schema": {},
              "policy": {}, "dedupe": [], "declared": false,
              "health": {"max_silence_s": null, "last_event_at": "2026-11-08T15:22:53+08:00",
                         "silence_s": 2.2, "stale": false, "last_error": null}}]}
```

与 `/sources` 同结构，但**额外列出「事件表里出现过、声明表里没有」的来源**
（`declared=false`，`mode=null`，`max_silence_s=null`）。这是发现漏声明客户端的入口：
`/sources` 不列它，`/stats` 一定看得到。仍需要 token。

### `GET /health`（原有字段 + 两个）

```json
{"ok": true, "version": "0.5.0", "events": 13, "sources": 2, "port": 8791,
 "db_path": "/Users/kibyou/.hermes/trigger.db", "uptime_s": 5.0, "subscribers": [],
 "subscription_ttl": 3600, "renew_window": 600,
 "subscriptions": {"active": 2, "expired": 0, "closed": 0, "total": 2}}
```

- `version` 改为 `0.5.0`；新增 `sources` = **已加载声明数**（不是来源数，`manual` 这种
  未声明来源不计）。
- 其余字段语义不变。

### `POST /trigger`（新增可选字段 + 一类 400）

- 新增可选 `dedupe_key`（str，≤512 字符）。缺省 `source + ":" + event_id`。
- `source` 已声明时，按 `schema` 做**最小校验**：字段必须存在、值不能是 `null`/纯空白字符串、
  类型必须是声明的那一种（`int` 不接受 `true/false`）。任一不符：

```json
{"error": "schema_mismatch", "field": "text", "ok": false}
```

  HTTP 400。`ok:false` 与全站错误响应保持一致，`error`/`field` 为约定字段。
- `source` 未声明时**不校验**、照收 200，进程内首次出现记一次：

```
WARNING trigger: sources: undeclared source 'selftest' is posting events — accepted anyway
                 (declare it as sources/selftest/source.json)
```

- 响应新增 `dedupe_key`（回显最终使用的键），`ok/id/event_id/duplicate` 语义不变。
  重复（`event_id` 撞或 `(dedupe_key, source)` 撞）仍是 200 + `"duplicate": true` + 原 `id`。

---

## 4. 去重键

- `events` 表新增列 `dedupe_key TEXT`；唯一索引 `idx_events_dedupe (dedupe_key, source)`
  —— 同一个键可以出现在不同 source 上，**不会**互相判重。
- 迁移幂等：先用 `PRAGMA table_info(events)` 判断列是否存在，缺则 `ALTER TABLE ADD COLUMN`，
  随后把旧行回填为 `source || ':' || event_id`，最后 `CREATE UNIQUE INDEX IF NOT EXISTS`。
  没有迁移框架、没有版本号表，重复启动无副作用（新库建表时已带该列，跳过 ALTER/回填）。
- 判重顺序：先按 `event_id` 找，找不到再按 `(dedupe_key, source)` 找；返回既有行的
  `id`/`event_id`/`dedupe_key`。
- 兼容：旧行回填后，客户端用显式 `dedupe_key="mc_chat:<旧 event_id>"` 重发也会被判重。
- `dedupe` 声明数组（如 `["actor","ts","text"]`）是**客户端约定**：客户端如果想按内容判重，
  自己拼好放进 `dedupe_key`；服务端只存不算，避免替来源决定业务语义。

---

## 5. 加一个新源：三步

1. **建声明**：`mkdir -p sources/<name> && $EDITOR sources/<name>/source.json`，照上面模板填
   `name`（必须等于信封里的 `source`）、`schema`、`max_silence_s`、`policy`、`dedupe`。
2. **写客户端**：放 `clients/<name>_client.py`，自己负责连接、归一化成统一信封
   （`source/kind/priority/actor/text/payload/event_id`，可选 `dedupe_key`）、过滤墙、重试、
   `POST /trigger`。客户端崩了不影响服务端，也不影响别的源。
3. **重启并验证**：
   ```bash
   launchctl kickstart -k gui/$(id -u)/ai.hermes.trigger
   curl --noproxy '*' http://127.0.0.1:8791/sources | python3 -m json.tool | grep '"name"'
   curl --noproxy '*' -H "X-Trigger-Token: $(cat ~/.hermes/secrets/trigger.token)" \
        "http://127.0.0.1:8791/stats?subscriber=me" | grep -o '"name": "<name>"'
   ```
   第一步看到你的源 = 声明加载成功；第二步在 `sources` 里出现 `declared: true` = 有事件进来了。
   如果 `/sources` 没有但 `/stats` 有（`declared:false`），说明声明文件名/`name` 拼错或没重启。

> 改 `server.py` 不是步骤之一。只要声明格式不变，加源永远只动 `sources/` 和 `clients/`。

---

## 6. 自测记录（真实命令与输出）

环境：macOS，Python 3.9.6，临时实例 `--port 18791 --db /tmp/t5.db`，**未触碰生产 8791**。

### 6.1 语法

```console
$ python3 -c 'import py_compile;py_compile.compile("server.py",doraise=True)' && echo COMPILE_OK
COMPILE_OK
```

### 6.2 声明被列出

```console
$ python3 server.py --port 18791 --db /tmp/t5.db --log /tmp/t5.log
mowu-trigger 0.5.0 listening on http://127.0.0.1:18791  [remote subscribe needs X-Trigger-Token from /Users/kibyou/.hermes/secrets/trigger.token]

$ curl --noproxy '*' http://127.0.0.1:18791/sources
[
  {
    "name": "email",
    "title": "邮箱（IMAP 拉取）",
    "mode": "pull",
    "schema": {
      "text": "string",
      "actor": "string"
    },
    "policy": {
      "default_priority": 1,
      "wake": true
    },
    "dedupe": [
      "actor",
      "ts",
      "text"
    ],
    "declared": true,
    "health": {
      "max_silence_s": 7200,
      "last_event_at": null,
      "silence_s": null,
      "stale": false,
      "last_error": null
    }
  },
  {
    "name": "mc_chat",
    "title": "Minecraft 聊天栏",
    "mode": "push",
    "schema": {
      "text": "string",
      "actor": "string"
    },
    "policy": {
      "default_priority": 0,
      "wake": true
    },
    "dedupe": [
      "actor",
      "ts",
      "text"
    ],
    "declared": true,
    "health": {
      "max_silence_s": 3600,
      "last_event_at": null,
      "silence_s": null,
      "stale": false,
      "last_error": null
    }
  }
]
```

```console
$ curl --noproxy '*' http://127.0.0.1:18791/health
{"ok": true, "version": "0.5.0", "events": 0, "sources": 2, "port": 18791,
 "db_path": "/tmp/t5.db", "uptime_s": 2.7, "subscribers": [],
 "subscription_ttl": 3600, "renew_window": 600,
 "subscriptions": {"active": 0, "expired": 0, "closed": 0, "total": 0}}
```

### 6.3 合法事件 + 同键重发

```console
$ curl --noproxy '*' -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:18791/trigger \
    -H 'Content-Type: application/json' \
    -d '{"source":"mc_chat","text":"hello from selftest","actor":"Steve","event_id":"t5-evt-1","dedupe_key":"t5-dedupe-1"}'
{"ok": true, "id": 1, "event_id": "t5-evt-1", "dedupe_key": "t5-dedupe-1", "duplicate": false}
HTTP 200

$ curl --noproxy '*' -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:18791/trigger \
    -H 'Content-Type: application/json' \
    -d '{"source":"mc_chat","text":"hello again","actor":"Steve","event_id":"t5-evt-2","dedupe_key":"t5-dedupe-1"}'
{"ok": true, "id": 1, "event_id": "t5-evt-1", "dedupe_key": "t5-dedupe-1", "duplicate": true}
HTTP 200
```

第二条 `event_id` 不同、`dedupe_key` 相同 → 判重，返回已有 `id=1`。

### 6.4 schema 不合法

```console
$ curl --noproxy '*' -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:18791/trigger \
    -H 'Content-Type: application/json' -d '{"source":"mc_chat","text":123,"actor":"Steve"}'
{"error": "schema_mismatch", "field": "text", "ok": false}
HTTP 400

$ curl --noproxy '*' -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:18791/trigger \
    -H 'Content-Type: application/json' -d '{"source":"mc_chat","text":"nobody"}'
{"error": "schema_mismatch", "field": "actor", "ok": false}
HTTP 400
```

日志：`WARNING trigger: schema_mismatch source=mc_chat field=text declared=string`

### 6.5 未声明 source：照收，`/sources` 不列，`/stats` 可见

```console
$ curl --noproxy '*' -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:18791/trigger \
    -H 'Content-Type: application/json' -d '{"source":"selftest","text":"undeclared source","event_id":"t5-evt-3"}'
{"ok": true, "id": 2, "event_id": "t5-evt-3", "dedupe_key": "selftest:t5-evt-3", "duplicate": false}
HTTP 200

$ curl --noproxy '*' http://127.0.0.1:18791/sources | python3 -c 'import json,sys;print([e["name"] for e in json.load(sys.stdin)])'
['email', 'mc_chat']

$ curl --noproxy '*' -H "X-Trigger-Token: $(cat ~/.hermes/secrets/trigger.token)" \
    "http://127.0.0.1:18791/stats?subscriber=selftest"
unread= 2 last_id= 0
[
  ... (email / mc_chat 同 /sources) ...,
  {
    "name": "selftest",
    "title": "selftest",
    "mode": null,
    "schema": {},
    "policy": {},
    "dedupe": [],
    "declared": false,
    "health": {
      "max_silence_s": null,
      "last_event_at": "2026-11-08T15:22:53+08:00",
      "silence_s": 2.2,
      "stale": false,
      "last_error": null
    }
  }
]
```

日志（只 warn 一次，后续同 source 不再刷）：

```
WARNING trigger: sources: undeclared source 'selftest' is posting events — accepted anyway
                 (declare it as sources/selftest/source.json)
```

### 6.6 关掉临时实例，端口释放

```console
$ kill 4564; lsof -nP -iTCP:18791 -sTCP:LISTEN
(no listener: port 18791 released)

$ tail -2 /tmp/t5.log
2026-11-08 15:22:55,209 INFO    trigger: signal 15 received; shutting down
2026-11-08 15:22:55,705 INFO    trigger: stopped
```

生产实例未动：

```console
$ curl --noproxy '*' http://127.0.0.1:8791/health | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["version"], d["uptime_s"])'
0.4.0 14916.4
```

### 6.7 可重复的一键自测

新增 `selftest_sources.sh`（自选端口/库，绝不碰 8791），13 项断言：

```console
$ ./selftest_sources.sh
PASS  server up on 127.0.0.1:18791
PASS  GET /sources lists mc_chat + email
PASS  GET /sources exposes mode/declared
PASS  valid mc_chat event -> 200, duplicate=false
PASS  same dedupe_key -> duplicate:true, same id
PASS  text=int -> 400 schema_mismatch field=text
PASS  missing actor -> 400 schema_mismatch field=actor
PASS  undeclared source accepted -> 200
PASS  GET /sources does not list selftest
PASS  GET /stats shows selftest as declared=false
PASS  same dedupe_key on another source -> accepted
PASS  GET /health version=0.5.0 sources=2
PASS  port 18791 released after SIGTERM
-------------------------------------------
13 passed, 0 failed  (port=18791 db=/tmp/t5.db log=/tmp/t5.log)
```

### 6.8 迁移与坏声明（附加）

用 0.4.0 的旧表结构建库（`events` 无 `dedupe_key`）并放一条 10:00、一条 14:30 的旧事件，
再在声明目录里混入一个坏 JSON：

```console
$ sqlite3 /tmp/t5old.db "pragma table_info(events);" | tail -3
7|payload_json|TEXT|0||0
8|ts|TEXT|0||0
9|created_at|REAL|0||0

$ python3 server.py --port 18793 --db /tmp/t5old.db --sources-dir /tmp/t5sources --log /tmp/t5m.log
# /tmp/t5m.log:
INFO    trigger: store migration: added events.dedupe_key
INFO    trigger: store migration: backfilled 2 dedupe_key value(s)
INFO    trigger: store ready: /tmp/t5old.db
WARNING trigger: sources: skipping /tmp/t5sources/bad/source.json: Expecting property name enclosed in double quotes: line 1 column 3 (char 2)
INFO    trigger: sources: 2 declaration(s) from /tmp/t5sources: email, mc_chat
INFO    trigger: mowu-trigger 0.5.0 listening on http://127.0.0.1:18793 (db=/tmp/t5old.db, inbox-only)

$ curl -sS --noproxy '*' http://127.0.0.1:18793/sources | python3 -c 'import json,sys
[print(e["name"], "max_silence_s=", e["health"]["max_silence_s"], "last_event_at=", e["health"]["last_event_at"],
       "silence_s=", e["health"]["silence_s"], "stale=", e["health"]["stale"]) for e in json.load(sys.stdin)]'
email   max_silence_s= 7200 last_event_at= 2026-11-08T14:30:00+08:00 silence_s= 3096.4 stale= False
mc_chat max_silence_s= 3600 last_event_at= 2026-11-08T10:00:00+08:00 silence_s= 19296.4 stale= True

$ sqlite3 /tmp/t5old.db "pragma table_info(events);" | tail -1
10|dedupe_key|TEXT|0||0
$ sqlite3 /tmp/t5old.db "select event_id,source,dedupe_key from events;"
old-a|mc_chat|mc_chat:old-a
old-b|email|email:old-b
$ sqlite3 /tmp/t5old.db "select sql from sqlite_master where name='idx_events_dedupe';"
CREATE UNIQUE INDEX idx_events_dedupe ON events(dedupe_key, source)

# 用显式 dedupe_key 撞迁移后的旧行 —— 判重，不新增
$ curl ... -d '{"source":"mc_chat","text":"anything else","actor":"Steve","event_id":"brand-new","dedupe_key":"mc_chat:old-a"}'
{"ok": true, "id": 1, "event_id": "old-a", "dedupe_key": "mc_chat:old-a", "duplicate": true}
$ sqlite3 /tmp/t5old.db "select count(*) from events;"
2
```

坏声明只跳过、不拦启动；旧库自动加列/回填/建索引；`silence_s` 超过阈值正确翻 `stale=true`。

---

## 7. 已知边界

- **生产未重启**：8791 仍是 0.4.0 进程（内存里是旧代码）。部署时 `launchctl kickstart -k`；
  首次启动会对生产库做 `ALTER TABLE` + 回填（35 行，瞬时完成），建议先备份
  `~/.hermes/trigger.db`。
- `last_error` 恒 `null`：服务端不跑客户端，无法知道 IMAP 登录失败之类的错误。要让这个字段
  有价值，只能由客户端另发一条 `source=<name> kind=client_error` 的事件，或后续加自报接口。
- `/sources` 无 token：只暴露声明与时间戳，不含事件正文；如果连这也不想暴露，把它挪进
  `_require_token()` 一行即可（`/stats` 已要求 token）。
- 声明只在启动时读一次；改 `max_silence_s`、加源都要重启。
- `schema` 是**最小校验**：只查声明里列出的字段，不禁止额外字段，不做长度/格式/枚举校验。
  复杂格式（邮箱、时间）留给客户端或 `policy` 层。
- `events` 里已有的 `manual` 来源没有声明：升级后会以 `declared:false` 出现在 `/stats`，
  并每次进程启动后首次出现时 warn 一次。要消掉就补 `sources/manual/source.json`，或确认它
  不是长期来源。
- `ts` 由客户端提供（服务端不校验格式）。无法解析成 ISO-8601 时健康计算回退 `created_at`，
  `last_event_at` 仍显示原始字符串。

---

## 8. 变更文件

| 文件 | 变化 |
|---|---|
| `server.py` | `VERSION=0.5.0`；`SourceRegistry`（扫描/校验/健康）；`GET /sources`；`/stats`+`sources`；`/health`+`sources`；`POST /trigger` 的 `dedupe_key` 与 `schema_mismatch`；`events.dedupe_key` 与唯一索引的幂等迁移 |
| `sources/mc_chat/source.json` | 新增声明 |
| `sources/email/source.json` | 新增声明 |
| `selftest_sources.sh` | 新增，13 项断言的端到端自测 |
| `SOURCES-DECL.md` | 本文件 |
| `README.md` | 同步 0.5.0 的 `/sources`、`/stats.sources`、`/health.sources`、`dedupe_key` 说明 |

---

## 9. 后续版本：0.6.0 的 L1/L2 分工

来源声明负责「这个源长什么样」，**不负责过滤策略**。0.6.0 把过滤拆成两层后，
本文件的边界仍然成立，只是多了一条指向：

| 层 | 落点 | 与声明的关系 |
|---|---|---|
| L1 业务过滤 | 各源客户端 + `clients/filterwall.py` | 声明里的 `schema` 仍在服务端做入口校验；客户端只保留该源特有的业务语义（MC `self_message`、email `folder_not_watched`） |
| L2 通用机制 | `server.py` + `~/.hermes/trigger-rules.json` | 与 `source.json` 无关，跨源统一配置：`downgrade`/`rate_limit`/`dedupe`/`quiet`（`drop` 只是 `rate_limit` 超限时的 `on_exceed` 结果；不做黑白名单） |

- 规则文件格式、`match`/`action` 字段、热重载、`GET /blocked`、`events.level` /
  `events.disposition` 全部见 **README 第 8 节「L1/L2 分工与规则文件」**。
- 0.6.0 信封多了一个 `level`：`event` 唤醒+投递、`info` 只进 worker 订阅流、
  `log` 只存档。`info` 不会进入 Hermes 的注入队列；客户端不给 level 时按
  `info` 处理（不会突然刷屏）。
- 声明里的 `policy.default_priority` 仍只是给客户端/人看的提示，服务端不据此
  计算优先级；`level` 的缺省 priority 由服务端定（event=0/info=2/log=3）。
  L2 的 `downgrade` 只能降级，升级仍由来源在信封里决定。
- 加一个新源依旧不需要改 `server.py`；如果新源需要通用限流/去重，只改
  `trigger-rules.json`，也不需要改声明。
