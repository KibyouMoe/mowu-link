#!/bin/sh
# 把 Hermes / Codex 主机侧改动应用到当前用户。
# 幂等：已经改过的项会跳过。执行前会把原文件备份成 *.bak-<时间戳>。
set -eu

STAMP=$(date +%Y%m%d-%H%M%S)
HOME_DIR=${HOME}

backup() {
  [ -f "$1" ] && cp "$1" "$1.bak-$STAMP" && echo "  备份 $1 -> $1.bak-$STAMP"
}

echo "== 1. ~/.zprofile 去掉 shell 级 10808 代理 =="
if grep -q "127.0.0.1:10808" "$HOME_DIR/.zprofile" 2>/dev/null; then
  backup "$HOME_DIR/.zprofile"
  python3 - "$HOME_DIR/.zprofile" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text()
t = t.replace("""export http_proxy=http://127.0.0.1:10808
export https_proxy=http://127.0.0.1:10808""",
"""# xiliu proxy —— TUN 已接管全局路由，不再需要 shell 级代理变量。
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export no_proxy=localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8
export NO_PROXY=localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8""")
p.write_text(t)
PY
  echo "  已改"
else
  echo "  跳过（无 10808）"
fi

echo "== 2. ~/.ssh/config 删掉 github ProxyCommand =="
if grep -q "ProxyCommand nc -X connect -x 127.0.0.1:10808" "$HOME_DIR/.ssh/config" 2>/dev/null; then
  backup "$HOME_DIR/.ssh/config"
  sed -i '' '/ProxyCommand nc -X connect -x 127.0.0.1:10808/d' "$HOME_DIR/.ssh/config"
  echo "  已改"
else
  echo "  跳过（无该行）"
fi

echo "== 3. ~/.codex/config.toml approval_policy = on-request =="
if grep -q '^approval_policy = "never"' "$HOME_DIR/.codex/config.toml" 2>/dev/null; then
  backup "$HOME_DIR/.codex/config.toml"
  sed -i '' 's/^approval_policy = "never"/approval_policy = "on-request"/' "$HOME_DIR/.codex/config.toml"
  echo "  已改"
else
  echo "  跳过（已是 on-request 或未设置）"
fi

echo "== 4. ~/.codex/AGENTS.md 加「权限与提审」段 =="
if grep -q "## 权限与提审" "$HOME_DIR/.codex/AGENTS.md" 2>/dev/null; then
  echo "  跳过（已存在）"
else
  backup "$HOME_DIR/.codex/AGENTS.md"
  cat >> "$HOME_DIR/.codex/AGENTS.md" <<'MD'

## 权限与提审

需要权限时，审批由系统在工具调用处发起——你不用自己找「提审工具」，也不要因为找不到而停下。

- 动手之前先判断这一步要不要额外权限：读工作区以外的目录、控制别的 app 的界面、联网写入、改系统设置、动别人的进程或服务。
- 要就**直接调用工具**，审批会自己弹给训练员；你同时用一句话说清「要什么权限、对哪个对象、要做什么」，让训练员看得见。
- **不要先绕路。** 被拒绝就停，把原因说清楚，不换路径试第二遍。
- 确实没有对应工具（不是权限问题），就说「这个能力我这边没有」，不要硬编。
MD
  echo "  已追加"
fi

echo "== 5. ~/.hermes/router-approval-policy.json =="
if [ -f "$HOME_DIR/.hermes/router-approval-policy.json" ]; then
  echo "  跳过（已存在）"
else
  cat > "$HOME_DIR/.hermes/router-approval-policy.json" <<'JSON'
{
  "_readme": "router 侧自动审批规则。命中的单子在创建时立即 approved，记录仍完整；未命中的照常 pending 等人。字段：worker(* 通配) / kind / titlePattern(正则)。auto:false 可让单次请求跳过策略。",
  "autoApprove": [
    { "kind": "elicitation", "desc": "Computer Use app 授权：cua_repl 的 js kernel 同步等待，必须秒批" }
  ]
}
JSON
  echo "  已创建"
fi

echo "== 6. ~/.hermes/config.yaml 去掉 DDGS_PROXY =="
if grep -q "DDGS_PROXY: http://127.0.0.1:10808" "$HOME_DIR/.hermes/config.yaml" 2>/dev/null; then
  echo "  需要手动：Hermes 主配置受保护，请自己把 DDGS_PROXY 那行删掉（或注释）"
else
  echo "  跳过（无该行）"
fi

echo "== 7. LaunchAgent 注入 CODEXWORKER_APPROVAL_POLICY =="
PLIST="$HOME_DIR/Library/LaunchAgents/ai.hermes.codexworker.plist"
if [ -f "$PLIST" ] && ! grep -q "CODEXWORKER_APPROVAL_POLICY" "$PLIST"; then
  backup "$PLIST"
  sed -i '' 's/unset ALL_PROXY all_proxy/unset ALL_PROXY all_proxy/; s#; export PATH=#; export CODEXWORKER_APPROVAL_POLICY=on-request; export PATH=#' "$PLIST"
  echo "  已改（记得 launchctl unload/load）"
else
  echo "  跳过（已存在或无该文件）"
fi

echo
echo "完成。重启相关服务："
echo "  launchctl kickstart -k gui/\$(id -u)/ai.hermes.router"
echo "  launchctl unload ~/Library/LaunchAgents/ai.hermes.codexworker.plist && launchctl load ~/Library/LaunchAgents/ai.hermes.codexworker.plist"
