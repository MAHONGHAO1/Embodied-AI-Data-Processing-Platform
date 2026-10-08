#!/usr/bin/env bash
# QuicData one-click background launcher for API + Celery Worker (Ubuntu / Linux / macOS)
# Usage:
#   bash scripts/start-all.sh        # Start
#   bash scripts/start-all.sh status # View status
#   bash scripts/start-all.sh stop   # Stop
#   bash scripts/start-all.sh logs   # View logs in real time

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
LOG_DIR="$BACKEND/logs"
API_LOG="$LOG_DIR/api.log"
WORKER_LOG="$LOG_DIR/worker.log"
PID_DIR="$BACKEND/.pids"
API_PID_FILE="$PID_DIR/api.pid"
WORKER_PID_FILE="$PID_DIR/worker.pid"

mkdir -p "$LOG_DIR" "$PID_DIR"

_api_pid() { cat "$API_PID_FILE" 2>/dev/null || true; }
_worker_pid() { cat "$WORKER_PID_FILE" 2>/dev/null || true; }

_is_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

_start() {
  if [[ ! -d "$BACKEND/.venv" ]]; then
    echo "错误: 虚拟环境不存在，请先运行: bash scripts/setup.sh" >&2
    exit 1
  fi

  local api_pid worker_pid
  api_pid="$(_api_pid)"
  worker_pid="$(_worker_pid)"

  if _is_running "$api_pid"; then
    echo "API 已在运行 (pid=$api_pid)"
  else
    echo "启动 API -> $API_LOG"
    # shellcheck disable=SC1091
    source "$BACKEND/.venv/bin/activate"
    nohup bash "$ROOT/scripts/start.sh" > "$API_LOG" 2>&1 &
    echo $! > "$API_PID_FILE"
  fi

  if _is_running "$worker_pid"; then
    echo "Worker 已在运行 (pid=$worker_pid)"
  else
    echo "启动 Worker -> $WORKER_LOG"
    # shellcheck disable=SC1091
    source "$BACKEND/.venv/bin/activate"
    nohup bash "$ROOT/scripts/start-worker.sh" > "$WORKER_LOG" 2>&1 &
    echo $! > "$WORKER_PID_FILE"
  fi

  sleep 2
  _status
  echo ""
  echo "查看日志: bash scripts/start-all.sh logs"
}

_status() {
  local api_pid worker_pid
  api_pid="$(_api_pid)"
  worker_pid="$(_worker_pid)"

  echo "=== QuicData 进程状态 ==="
  if _is_running "$api_pid"; then
    echo "API:     运行中 (pid=$api_pid)"
  else
    echo "API:     未运行"
  fi

  if _is_running "$worker_pid"; then
    echo "Worker:  运行中 (pid=$worker_pid)"
  else
    echo "Worker:  未运行"
  fi

  if _is_running "$api_pid"; then
    echo ""
    echo "健康检查:"
    curl -s http://127.0.0.1:8000/health | python3 -m json.tool 2>/dev/null || echo "  健康检查失败"
  fi
}

_stop() {
  local api_pid worker_pid
  api_pid="$(_api_pid)"
  worker_pid="$(_worker_pid)"

  if _is_running "$api_pid"; then
    echo "停止 API (pid=$api_pid)"
    kill "$api_pid" 2>/dev/null || true
    sleep 1
    _is_running "$api_pid" && kill -9 "$api_pid" 2>/dev/null || true
  fi

  if _is_running "$worker_pid"; then
    echo "停止 Worker (pid=$worker_pid)"
    kill "$worker_pid" 2>/dev/null || true
    sleep 1
    _is_running "$worker_pid" && kill -9 "$worker_pid" 2>/dev/null || true
  fi

  rm -f "$API_PID_FILE" "$WORKER_PID_FILE"
  echo "已停止"
}

_logs() {
  echo "按 Ctrl+C 退出日志查看"
  tail -f "$API_LOG" "$WORKER_LOG" 2>/dev/null || true
}

case "${1:-start}" in
  start) _start ;;
  status) _status ;;
  stop) _stop ;;
  logs) _logs ;;
  -h|--help)
    echo "用法: bash scripts/start-all.sh [start|status|stop|logs]"
    exit 0
    ;;
  *)
    echo "未知参数: $1" >&2
    echo "用法: bash scripts/start-all.sh [start|status|stop|logs]" >&2
    exit 1
    ;;
esac
