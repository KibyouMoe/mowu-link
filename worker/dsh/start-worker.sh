#!/bin/sh
# dsh worker 启动脚本。
#
#   ./start-worker.sh            前台运行（Ctrl-C 优雅退出）
#   ./start-worker.sh -d         后台运行（nohup，pid 写到 ~/.hermes/run/）
#   ./start-worker.sh --name 波旁 --router http://127.0.0.1:8790
#
# 配置默认取同目录 worker.json；命令行参数覆盖配置。
set -eu

# --- 运行环境（2026-11-09 实测，别删）---------------------------------------
# 1) dsh 的 deepseek-official 路由走 Anthropic Messages 协议；官方 OpenAI 端点
#    (https://api.deepseek.com) 会返回 404。必须指到 /anthropic。
# 2) dsh 不认 SOCKS 代理（ALL_PROXY=socks5 会报错），只能给 http(s) 代理。
# 3) 密钥从 ~/.hermes/.env 取（DEEPSEEK_API_KEY）。
# 4) 飞书长连接要 lark_oapi，系统 python3(3.9) 没装；默认用专用 venv，
#    可用 --python /path/to/python 或 PYTHON=... 覆盖。
if [ -f "${HOME}/.hermes/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${HOME}/.hermes/.env"
    set +a
fi
unset ALL_PROXY all_proxy 2>/dev/null || true
export HTTP_PROXY="${DSH_HTTP_PROXY:-http://127.0.0.1:10808}"
export HTTPS_PROXY="${DSH_HTTP_PROXY:-http://127.0.0.1:10808}"
export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY"
export DEEPSEEK_BASE_URL="${DSH_DEEPSEEK_BASE_URL:-https://api.deepseek.com/anthropic}"


DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG=${WORKER_CONFIG:-"$DIR/worker.json"}
LOG_DIR="${HOME}/.hermes/logs"
CONSOLE_LOG="${LOG_DIR}/dsh-worker.console.log"
PID_DIR="${HOME}/.hermes/run"
PID_FILE="${PID_DIR}/dsh-worker.pid"
DEFAULT_PY="${HOME}/.hermes/venvs/mowu-worker/bin/python"

mkdir -p "$LOG_DIR" "$PID_DIR"

# --python（任意位置）= 覆盖解释器；其余参数原序留给 worker.py。
PY_SPEC=""
_REST=$(mktemp "${TMPDIR:-/tmp}/dsh-worker-args.XXXXXX")
trap 'rm -f "$_REST"' EXIT
while [ $# -gt 0 ]; do
    case "$1" in
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
        echo "dsh worker: --python 不可执行：$PY" >&2
        exit 2
    fi
    PY=/usr/bin/python3
fi

if [ "${1:-}" = "-d" ] || [ "${1:-}" = "--daemon" ]; then
    shift
    nohup "$PY" "$DIR/worker.py" --config "$CONFIG" "$@" \
        >>"$CONSOLE_LOG" 2>&1 &
    echo $! >"$PID_FILE"
    echo "dsh worker started pid=$(cat "$PID_FILE") config=$CONFIG python=$PY"
    echo "log: $CONSOLE_LOG（worker 自身日志在 worker.json 的 log_file）"
    exit 0
fi

exec "$PY" "$DIR/worker.py" --config "$CONFIG" "$@"
