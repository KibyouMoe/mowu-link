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
# 4) 飞书长连接要 lark_oapi，系统 python3(3.9) 没装；默认用专用 venv，
#    可用 --python /path/to/python 或 PYTHON=... 覆盖。
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
CONFIG=${WORKER_CONFIG:-"$DIR/worker.json"}
LOG_DIR="${HOME}/.hermes/logs"
CONSOLE_LOG="${LOG_DIR}/codex-worker.console.log"
PID_DIR="${HOME}/.hermes/run"
PID_FILE="${PID_DIR}/codex-worker.pid"
DEFAULT_PY="${HOME}/.hermes/venvs/mowu-worker/bin/python"

mkdir -p "$LOG_DIR" "$PID_DIR"

# --python（任意位置）= 覆盖解释器；-d 可出现在任意位置；其余参数留给 worker.py。
DAEMON=0
PY_SPEC=""
_REST=$(mktemp "${TMPDIR:-/tmp}/codex-worker-args.XXXXXX")
trap 'rm -f "$_REST"' EXIT
while [ $# -gt 0 ]; do
    case "$1" in
        -d|--daemon) DAEMON=1; shift ;;
        --python) PY_SPEC="${2:-}"; shift 2 ;;
        --python=*) PY_SPEC="${1#*=}"; shift ;;
        *) printf '%s\n' "$1" >>"$_REST"; shift ;;
    esac
done
set --
while IFS= read -r _arg; do
    set -- "$@" "$_arg"
done <"$_REST"
rm -f "$_REST"
trap - EXIT

# 优先级：--python > $PYTHON > 专用 venv > 系统 python3
PY=${PY_SPEC:-${PYTHON:-$DEFAULT_PY}}
if [ ! -x "$PY" ]; then
    if [ -n "$PY_SPEC" ]; then
        echo "codex worker: --python 不可执行：$PY" >&2
        exit 2
    fi
    PY=/usr/bin/python3
fi

# 环境里的代理/二进制覆盖配置（命令行参数在最后，优先级更高）。
CODEX_BIN="${CODEX_BIN:-auto}"
CODEX_HTTP_PROXY="${CODEX_HTTP_PROXY:-$HTTP_PROXY}"

if [ "$DAEMON" = "1" ]; then
    nohup "$PY" "$DIR/worker.py" --config "$CONFIG" \
        --codex-bin "$CODEX_BIN" --proxy "$CODEX_HTTP_PROXY" "$@" \
        >>"$CONSOLE_LOG" 2>&1 &
    echo $! >"$PID_FILE"
    echo "codex worker started pid=$(cat "$PID_FILE") config=$CONFIG python=$PY"
    echo "log: $CONSOLE_LOG（worker 自身日志在 worker.json 的 log_file）"
    exit 0
fi

exec "$PY" "$DIR/worker.py" --config "$CONFIG" \
    --codex-bin "$CODEX_BIN" --proxy "$CODEX_HTTP_PROXY" "$@"
