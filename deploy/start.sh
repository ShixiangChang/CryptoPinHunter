#!/usr/bin/env bash
# 启动实盘：daemon 守护 4 进程（live_feed 采集 / live_trader 交易 / live_status 看板 / serve HTTP）
# 用法：
#   bash deploy/start.sh        后台启动（nohup，写日志）
#   bash deploy/start.sh fg     前台启动（调试，Ctrl+C 退出）
#   bash deploy/start.sh stop   停止后台 daemon
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY=$(command -v python3 || command -v python)
fi
mkdir -p logs

if [ "${1:-}" = "stop" ]; then
  if [ -f logs/daemon.pid ]; then
    PID=$(cat logs/daemon.pid)
    echo "==> 停止 daemon (PID=$PID)"
    kill "$PID" 2>/dev/null || true
    rm -f logs/daemon.pid
  fi
  exit 0
fi

if [ "${1:-}" = "fg" ]; then
  echo "==> 前台启动 daemon（Ctrl+C 退出）"
  exec "$PY" deploy/daemon.py
fi

echo "==> 后台启动 daemon（守护 live_feed / live_trader / live_status / serve）"
nohup "$PY" deploy/daemon.py > logs/daemon.log 2>&1 &
echo $! > logs/daemon.pid
echo "已启动，PID=$(cat logs/daemon.pid)"
echo "  看板: http://127.0.0.1:8777"
echo "  日志: logs/daemon.log"
echo "  停止: bash deploy/start.sh stop"
