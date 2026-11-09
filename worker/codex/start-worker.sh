#!/bin/sh
# codex worker 启动脚本。
#
#   ./start-worker.sh            前台运行（Ctrl-C 优雅退出）
#   ./start-worker.sh -d         后台运行（nohup，pid 写到 ~/.hermes/run/）
#   ./start-worker.sh --name 阿码 --router http://127.0.0.1:8790
#
# 配置默认取同目录 worker.json；命令行参数覆盖配置。
set -eu

# --- 运行环境（实测，别删）--------------------------------------------------
# 1) codex 不认 socks 代理（ALL_PROXY=socks5 会超时），只能给 http(s) 代理；
#    所以先清 ALL_PROXY/all_proxy，再把 HTTP(S)_PROXY 指到本机 http 代理。
# 2) codex 装在 ~/.hermes/tools/node-*/bin 下，把该目录塞进 PATH，
#    worker 的 auto 探测就能直接命中。
# 3) 登录态在 ~/.codex（教学员自己的 ChatGPT 账号），不需要 API key。
if [ -f "${HOME}/.hermes/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${HOME}/.hermes/.env"
    set +a
fi
unset ALL_PROXY all_proxy 2>/dev/null || true
export HTTP_PROXY="${CODEX_HTTP_PROXY:-http://127.0.0.1:10808}"
export HTTPS_PROXY="$HTTP_PROXY"
export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"
export no_proxy="$NO_PROXY"
export PATH="${HOME}/.hermes/tools/node-26.12.1-darwin-arm64/bin:${PATH}"


DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=${PYTHON:-/usr/bin/python3}
CONFIG=${WORKER_CONFIG:-"$DIR/worker.json"}
LOG_DIR="${HOME}/.hermes/logs"
CONSOLE_LOG="${LOG_DIR}/codex-worker.console.log"
PID_DIR="${HOME}/.hermes/run"
PID_FILE="${PID_DIR}/codex-worker.pid"

mkdir -p "$LOG_DIR" "$PID_DIR"

# 环境里的代理/二进制覆盖配置（命令行参数在最后，优先级更高）。
DAEMON=0
if [ "${1:-}" = "-d" ] || [ "${1:-}" = "--daemon" ]; then
    DAEMON=1
    shift
fi
CODEX_BIN="${CODEX_BIN:-auto}"
CODEX_HTTP_PROXY="${CODEX_HTTP_PROXY:-$HTTP_PROXY}"

if [ "$DAEMON" = "1" ]; then
    nohup "$PY" "$DIR/worker.py" --config "$CONFIG" \
        --codex-bin "$CODEX_BIN" --proxy "$CODEX_HTTP_PROXY" "$@" \
        >>"$CONSOLE_LOG" 2>&1 &
    echo $! >"$PID_FILE"
    echo "codex worker started pid=$(cat "$PID_FILE") config=$CONFIG"
    echo "log: $CONSOLE_LOG（worker 自身日志在 worker.json 的 log_file）"
    exit 0
fi

exec "$PY" "$DIR/worker.py" --config "$CONFIG" \
    --codex-bin "$CODEX_BIN" --proxy "$CODEX_HTTP_PROXY" "$@"
