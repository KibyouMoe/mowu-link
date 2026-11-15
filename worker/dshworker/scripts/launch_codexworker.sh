#!/bin/sh
# CodexWorker 启动器 · macOS / Linux
# Windows 用同目录的 launch_codexworker.cmd。
#
# 与 Windows 版等价：先找 Codex 宿主注入的 Node，再找 Hermes 自带的 Node，
# 最后退回系统 PATH 上的 node。
set -u

if [ "$#" -eq 0 ]; then
  echo "CodexWorker could not start: missing MCP server path." >&2
  exit 64
fi

if [ -n "${CODEX_MCP_NODE_PATH:-}" ] && [ -x "$CODEX_MCP_NODE_PATH" ]; then
  exec "$CODEX_MCP_NODE_PATH" "$@"
fi
if [ -n "${CODEX_BROWSER_USE_NODE_PATH:-}" ] && [ -x "$CODEX_BROWSER_USE_NODE_PATH" ]; then
  exec "$CODEX_BROWSER_USE_NODE_PATH" "$@"
fi
if [ -n "${CODEX_ELECTRON_RESOURCES_PATH:-}" ] && [ -x "$CODEX_ELECTRON_RESOURCES_PATH/cua_node/bin/node" ]; then
  exec "$CODEX_ELECTRON_RESOURCES_PATH/cua_node/bin/node" "$@"
fi

# Hermes 自带 Node 运行时（~/.hermes/tools/node-<ver>-<platform>/bin/node）
for candidate in "${HOME:-}/.hermes/tools/"node-*/bin/node; do
  [ -x "$candidate" ] && exec "$candidate" "$@"
done

if command -v node >/dev/null 2>&1; then
  exec node "$@"
fi

echo "CodexWorker could not find a Node runtime. Install Node 20+ or set CODEX_MCP_NODE_PATH." >&2
exit 127
