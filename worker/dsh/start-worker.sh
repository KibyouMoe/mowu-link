#!/bin/sh
# dsh worker 启动脚本。
#
#   ./start-worker.sh            前台运行（Ctrl-C 优雅退出）
#   ./start-worker.sh -d         后台运行（nohup，pid 写到 ~/.hermes/run/）
#   ./start-worker.sh --name 波旁 --router http://127.0.0.1:8790
#
# 配置默认取同目录 worker.json；命令行参数覆盖配置。
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=${PYTHON:-/usr/bin/python3}
CONFIG=${WORKER_CONFIG:-"$DIR/worker.json"}
LOG_DIR="${HOME}/.hermes/logs"
CONSOLE_LOG="${LOG_DIR}/dsh-worker.console.log"
PID_DIR="${HOME}/.hermes/run"
PID_FILE="${PID_DIR}/dsh-worker.pid"

mkdir -p "$LOG_DIR" "$PID_DIR"

if [ "${1:-}" = "-d" ] || [ "${1:-}" = "--daemon" ]; then
    shift
    nohup "$PY" "$DIR/worker.py" --config "$CONFIG" "$@" \
        >>"$CONSOLE_LOG" 2>&1 &
    echo $! >"$PID_FILE"
    echo "dsh worker started pid=$(cat "$PID_FILE") config=$CONFIG"
    echo "log: $CONSOLE_LOG（worker 自身日志在 worker.json 的 log_file）"
    exit 0
fi

exec "$PY" "$DIR/worker.py" --config "$CONFIG" "$@"
