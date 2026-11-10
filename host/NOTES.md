# 本次改动清单

- `.zprofile` 去掉 shell 级 10808 代理，改成 unset + no_proxy（TUN 接管）
- `.ssh/config` 删掉 github.com-honor 的 ProxyCommand（10808 失效导致 fetch/push 全失败）
- `.codex/config.toml`：`approval_policy` never → on-request（full access 下需要审批会被直接拒）
- `.codex/AGENTS.md` 加「权限与提审」段：直接调用工具让审批自己弹出，别因为找不到提审工具就停工
- `~/.hermes/config.yaml` 去掉 DDGS_PROXY 指向的失效端口（**受保护，需你手动执行**）
- 新建 `~/.hermes/router-approval-policy.json`：router 侧自动同意规则
- codexworker 的 LaunchAgent 注入 `CODEXWORKER_APPROVAL_POLICY=on-request`
