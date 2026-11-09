# Worker 名册（Roster）

> 每接一个 worker，先在这里落一行。**真实名称用英文**（Router 唯一认的），口语名只给人看。
> 规范见 `WORKER.md`；任务明细以 Router `/tasks?worker=<name>` 为准，本文只记要点。

| 真实名称 | 口语名 | 来源 | Agent | 模型 | 擅长 |
|---|---|---|---|---|---|
| `TamamoCross` | 玉藻十字 | 本机 M6 / `worker/dsh` | DeepSeek Harness（headless） | `deepseek-flash` | 调研、读文档、GUI 操作、长任务 |
| `OguriCap` | 小栗帽 | 本机 M6 / `worker/codex` | Codex CLI（`codex exec`） | OpenAI 账号默认（未指定 `-m`） | 写码、改码、跑测试、重构 |

---

## 一、两套名字，别混

```
桥实例名（代码、目录、进程）   CodexWorker / DSHWorker
worker 身份名（Router 认的）   OguriCap / TamamoCross
```

- 桥是壳，身份是里面那个人。一个桥可以换身份，身份也可以换桥。
- 目录：`worker/codexworker/`（已建）、`worker/dshworker/`（待建）。

---

## TamamoCross（玉藻十字）

```yaml
name: TamamoCross
alias: 玉藻十字 / 玉藻
origin:
  host: 192.168.3.102（M6 Mac mini，溪流本机）
  path: ~/projects/mowulink/worker/dsh/
  launchd: ai.hermes.dsh-worker
  log: ~/.hermes/logs/dsh-worker.log
agent:
  runtime: DeepSeek Harness
  command: dsh --profile headless --session-id <id> --json "<task>"
  model: deepseek-flash
  env:
    DEEPSEEK_BASE_URL: https://api.deepseek.com/anthropic   # 必须，否则 /messages 走官方根域 404
    HTTPS_PROXY: http://127.0.0.1:10808                      # dsh 不认 socks
strengths:
  - 调研与长文档阅读（能自己翻仓库、读 skill、比对实现）
  - 电脑操作（computer use 已装并验证：截图 + 点击 + 键盘）
  - 需要「边看边判断」的长任务（超时上限 3600s）
weaknesses:
  - 单次任务偏慢（研究型任务十几分钟到半小时）
  - 容易在细节上自作主张，需要验收
personality: |
  开朗、直率、干劲足，接到活先说「好，我来」。
  干活时话多，会边做边报进度；遇到不确定会主动问，不会硬猜。
  有点冒失：动作快、偶尔漏细节，所以它旁边必须有看门狗盯着。
tasks:
  recorded_under: TamamoCross
  note: 改名前的历史任务记在旧名 dsh 名下；从现在起按 TamamoCross 记录
  recent: []
```

---

## OguriCap（小栗帽）

```yaml
name: OguriCap
alias: 小栗帽 / 小栗
origin:
  host: 192.168.3.102（M6 Mac mini，溪流本机）
  path: ~/projects/mowulink/worker/codex/
  launchd: ai.hermes.codex-worker
  log: ~/.hermes/logs/codex-worker.log
agent:
  runtime: Codex CLI
  command: codex exec --skip-git-repo-check "<task>"（续聊用 codex exec resume <id>）
  model: OpenAI 账号默认（ChatGPT 登录，未指定 -m）
  env:
    HTTPS_PROXY: http://127.0.0.1:10808   # codex 不认 socks
strengths:
  - 写代码 / 改代码 / 重构（会自己读仓库、跑测试、改完自查）
  - 一次能跑通整条实现链，不需要我盯着每一步
weaknesses:
  - 话少，过程不解释，出问题只能从退出码和 stderr 看
  - 依赖 ChatGPT 账号额度，代理必须对
personality: |
  寡言、一本正经、不客套。问它什么，它给结果，不铺垫、不解释。
  对自己的产出有洁癖，改完会自己跑一遍再交。
  出错时不掩饰，直接把错误原文贴上来。
tasks:
  recorded_under: OguriCap
  note: 改名前的历史任务记在旧名 codex 名下；从现在起按 OguriCap 记录
  recent: []
```

---

## 维护规则

1. **新增 worker**：先在本文加一行表格 + 一段详表，再去接 Router。
2. **改名**：真实名改 `worker.json` 的 `name` 并重启；本文同步；Router 不认中文，别名只写在这里。
3. **任务记录**：不手写明细，用 `GET /tasks?worker=<name>` 查；本文只保留「最近 3 条要点」。
4. **能力变化**（比如新装了某个插件）：更新 `strengths` / `weaknesses`，别让它停留在过时印象。
