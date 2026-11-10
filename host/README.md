# host/ · Hermes / Codex 主机侧改动

这个目录放**不属于任何仓库**、只存在于本机 `$HOME` 的配置改动。
代码改动在 `worker/` 和 `server/router/`，这里只记「机器上还动了什么」。

## 两种应用方式

```bash
cd ~ && patch -p1 --dry-run < ~/projects/mowulink/host/2026-11-11-hermes-host.patch   # 看能不能打
cd ~ && patch -p1          < ~/projects/mowulink/host/2026-11-11-hermes-host.patch   # 真打
```

或者用幂等脚本（推荐换机时用，会跳过已改过的项并备份）：

```bash
sh ~/projects/mowulink/host/apply.sh
```

patch 是**当时的快照**，只能在原始状态上应用一次；`apply.sh` 可重复跑。

## 改了什么（2026-11-11）

| 文件 | 改动 | 为什么 |
|---|---|---|
| `~/.zprofile` | 删掉 `http_proxy/https_proxy=127.0.0.1:10808`，改 `unset` + `no_proxy` | 10808 的 xray 已卸载，变量被子进程继承，导致 Codex 每个请求 TCP 秒拒 |
| `~/.ssh/config` | 删掉 `github.com-honor` 的 `ProxyCommand nc -x 127.0.0.1:10808` | 同一端口；不清掉 fetch/push 全部失败 |
| `~/.codex/config.toml` | `approval_policy` `never` → `on-request` | `never` + full access 时，本该提审的操作直接失败而不是来问 |
| `~/.codex/AGENTS.md` | 新增「权限与提审」段 | 让它主动走审批，但**不要**因为没有「提审工具」就停工 |
| `~/.hermes/router-approval-policy.json` | 新建 | router 侧自动同意 elicitation（CUA 的 app 授权，kernel 同步等待，必须秒批） |
| `~/.hermes/config.yaml` | 删 `DDGS_PROXY: http://127.0.0.1:10808` | 死代理；**该文件受保护，需手动改** |
| `ai.hermes.codexworker.plist` | 注入 `CODEXWORKER_APPROVAL_POLICY=on-request` | 打开审批通道 |

## 一个原则

主机上不再出现任何写死的代理端口。全机走 FlClash TUN（`utun4` / `198.18.0.1`），
需要代理的进程只认显式传入的 `HTTP(S)_PROXY`。历史上三处写死 10808
（`.zprofile`、`app-server.mjs` 的 `DEFAULT_PROXY`、`.ssh/config`），
每一处都单独造成过一次故障。

## 不放进来的东西

`~/.hermes/secrets/`、飞书 app_secret、router token、`mac_login.pass` —— 一律不进仓库。
