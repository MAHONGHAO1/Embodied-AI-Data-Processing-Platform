#!/usr/bin/env bash
# Manage the local development application processes. Docker owns PostgreSQL and Redis.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Start the API, local frontend preview, and explicit Batch / Episode JobRun
# services. Legacy Task workers are not included.
SERVICES=(api frontend control ingest media publish export analytics governance beat)

compose() {
  docker compose -f "$ROOT/deploy/docker-compose.yml" "$@"
}

require_screen() {
  command -v screen >/dev/null 2>&1 || {
    echo "screen is required for local application processes" >&2
    exit 2
  }
}

session_name() {
  case "$1" in
    api) printf '%s\n' 'quicstudio-dev-api' ;;
    frontend) printf '%s\n' 'quicstudio-dev-frontend' ;;
    control|ingest|media|publish|export|analytics|governance) printf 'quicstudio-dev-worker-%s\n' "$1" ;;
    beat) printf '%s\n' 'quicstudio-dev-beat' ;;
    *)
      echo "unknown local service: $1 (expected api|frontend|control|ingest|media|publish|export|analytics|governance|beat)" >&2
      return 2
    ;;
  esac
}

session_exists() {
  local session="$1"
  local sessions
  sessions="$(screen -ls 2>/dev/null || true)"
  [[ "$sessions" == *".${session}"* ]]
}

service_pid_file() {
  printf '%s/deploy/runtime/.dev-screen-pids/%s.pid\n' "$ROOT" "$1"
}

service_log_file() {
  printf '%s/deploy/runtime/logs/%s.log\n' "$ROOT" "$1"
}

service_pid_is_running() {
  local pid_file="$1"
  local pid
  [[ -f "$pid_file" ]] || return 1
  pid="$(tr -d '[:space:]' < "$pid_file")"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

wait_for_pid_exit() {
  local pid="$1"
  local attempts="$2"
  local attempt
  for attempt in $(seq 1 "$attempts"); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  return 1
}

ensure_port_available() {
  local host="$1"
  local port="$2"
  local service_name="$3"
  local port_variable="$4"
  local python_bin=""
  local candidate
  for candidate in "$ROOT/.venv/bin/python" "$ROOT/backend/.venv/bin/python"; do
    if [[ -x "$candidate" ]]; then
      python_bin="$candidate"
      break
    fi
  done
  if [[ -z "$python_bin" ]]; then
    python_bin="$(command -v python3 || true)"
  fi
  [[ -n "$python_bin" ]] || {
    echo "python3 is required to verify the local API port" >&2
    return 2
  }

  "$python_bin" - "$host" "$port" "$service_name" "$port_variable" <<'PY'
import socket
import sys

host, port, service_name, port_variable = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
family = socket.AF_INET6 if ':' in host else socket.AF_INET
with socket.socket(family, socket.SOCK_STREAM) as probe:
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
    except OSError as error:
        raise SystemExit(
            f"Local {service_name} port {host}:{port} is already in use. "
            f"Stop the local process or choose {port_variable}=<port> before make dev-up. "
            f"({error})"
        )
PY
}

ensure_api_port_available() {
  ensure_port_available \
    "${LOCAL_API_HOST:-127.0.0.1}" \
    "${LOCAL_API_PORT:-8000}" \
    "API" \
    "LOCAL_API_PORT"
}

ensure_frontend_port_available() {
  ensure_port_available \
    "${FRONTEND_HOST:-127.0.0.1}" \
    "${FRONTEND_PORT:-8090}" \
    "frontend" \
    "FRONTEND_PORT"
}

start_service() {
  local service="$1"
  local session
  local pid_file
  local attempt
  local root_quoted

  session="$(session_name "$service")"
  pid_file="$(service_pid_file "$service")"
  if session_exists "$session"; then
    if service_pid_is_running "$pid_file"; then
      echo "screen session already running: $session"
      return 0
    fi
    echo "removing stale screen session: $session"
    screen -S "$session" -X quit || true
    rm -f "$pid_file"
  fi

  case "$service" in
    api) ensure_api_port_available ;;
    frontend) ensure_frontend_port_available ;;
  esac

  root_quoted="$(printf '%q' "$ROOT")"
  screen -U -dmS "$session" bash -lc "cd ${root_quoted} && exec make dev-run SERVICE=${service}"
  for attempt in $(seq 1 20); do
    if service_pid_is_running "$pid_file"; then
      echo "started screen session: $session"
      return 0
    fi
    session_exists "$session" || break
    sleep 0.1
  done
  screen -S "$session" -X quit >/dev/null 2>&1 || true
  rm -f "$pid_file"
  echo "service exited before it became ready: $service" >&2
  return 1
}

start_all() {
  require_screen
  local service
  for service in "${SERVICES[@]}"; do
    start_service "$service"
  done
}

stop_all() {
  require_screen
  local service
  for service in "${SERVICES[@]}"; do
    stop_service "$service"
  done
}

stop_service() {
  local service="$1"
  local session
  local pid_file
  local pid
  local was_managed=false

  session="$(session_name "$service")"
  pid_file="$(service_pid_file "$service")"
  if session_exists "$session" && service_pid_is_running "$pid_file"; then
    was_managed=true
    pid="$(tr -d '[:space:]' < "$pid_file")"
    if [[ "$service" == "frontend" ]]; then
      # Detached macOS Screen sessions can ignore SIGINT in Python's inherited
      # signal disposition. SIGTERM terminates the preview server reliably.
      kill -TERM "$pid" 2>/dev/null || true
      if ! wait_for_pid_exit "$pid" 5; then
        kill -KILL "$pid" 2>/dev/null || true
        wait_for_pid_exit "$pid" 5 || true
      fi
    else
      kill -INT "$pid" 2>/dev/null || true
      if ! wait_for_pid_exit "$pid" 15; then
        kill -TERM "$pid" 2>/dev/null || true
        if ! wait_for_pid_exit "$pid" 10; then
          kill -KILL "$pid" 2>/dev/null || true
          wait_for_pid_exit "$pid" 5 || true
        fi
      fi
    fi
  fi
  if session_exists "$session"; then
    was_managed=true
    screen -S "$session" -X quit || true
  fi
  rm -f "$pid_file"
  if [[ "$was_managed" == true ]]; then
    echo "stopped screen session: $session"
  fi
}

show_status() {
  require_screen
  echo "Docker infrastructure:"
  compose ps || true
  echo
  echo "Screen application processes:"
  local service
  local session
  local pid_file
  for service in "${SERVICES[@]}"; do
    session="$(session_name "$service")"
    pid_file="$(service_pid_file "$service")"
    if session_exists "$session" && service_pid_is_running "$pid_file"; then
      printf '  %-8s running  %s\n' "$service" "$session"
    elif session_exists "$session"; then
      printf '  %-8s unhealthy %s\n' "$service" "$session"
    else
      printf '  %-8s stopped  %s\n' "$service" "$session"
    fi
  done
}

attach_service() {
  require_screen
  local session
  session="$(session_name "$1")"
  session_exists "$session" || {
    echo "screen session is not running: $session" >&2
    exit 1
  }
  exec screen -r "$session"
}

show_logs() {
  local log_file
  session_name "$1" >/dev/null
  log_file="$(service_log_file "$1")"
  [[ -f "$log_file" ]] || {
    echo "no local log file for service: $1" >&2
    exit 1
  }
  tail -n "${SCREEN_LOG_LINES:-200}" "$log_file"
}

wait_for_infra() {
  local attempt
  for attempt in $(seq 1 30); do
    if compose exec -T postgres pg_isready -U quicdata >/dev/null 2>&1 \
      && compose exec -T redis redis-cli ping 2>/dev/null | grep -qx 'PONG'; then
      echo "PostgreSQL and Redis are ready"
      return 0
    fi
    sleep 1
  done
  echo "local PostgreSQL or Redis did not become ready within 30 seconds" >&2
  return 1
}

wipe_local_runtime() {
  [[ "${CONFIRM_DEV_WIPE:-}" == "DELETE_LOCAL_RUNTIME" ]] || {
    echo "Refusing to remove local runtime. Re-run with CONFIRM_DEV_WIPE=DELETE_LOCAL_RUNTIME." >&2
    exit 2
  }

  stop_all
  compose down

  local path
  local wipe_paths=(
    "$ROOT/deploy/runtime/postgres"
    "$ROOT/deploy/runtime/redis"
    "$ROOT/deploy/runtime/scratch"
    "$ROOT/deploy/runtime/backups"
    "$ROOT/deploy/runtime/logs"
  )
  for path in "${wipe_paths[@]}"; do
    case "$path" in
      "$ROOT"/deploy/runtime/*) ;;
      *)
        echo "refusing to remove an unexpected path: $path" >&2
        exit 2
        ;;
    esac
    rm -rf "$path"
    mkdir -p "$path"
  done
  echo "Removed local development runtime data under $ROOT/deploy/runtime/."
}

usage() {
  cat <<'EOF'
Usage: bash scripts/dev-screen.sh <up|down|restart|status|attach|logs|wait-infra|wipe> [service]

Services: api frontend control ingest media publish export analytics governance ai beat
EOF
}

action="${1:-}"
case "$action" in
  up)
    if [[ -n "${2:-}" ]]; then
      require_screen
      start_service "$2"
    else
      start_all
    fi
    ;;
  status) show_status ;;
  attach)
    [[ -n "${2:-}" ]] || { usage >&2; exit 2; }
    attach_service "$2"
    ;;
  logs)
    [[ -n "${2:-}" ]] || { usage >&2; exit 2; }
    show_logs "$2"
    ;;
  down)
    if [[ -n "${2:-}" ]]; then
      require_screen
      stop_service "$2"
    else
      stop_all
    fi
    ;;
  restart)
    [[ -n "${2:-}" ]] || { usage >&2; exit 2; }
    require_screen
    stop_service "$2"
    start_service "$2"
    ;;
  wait-infra) wait_for_infra ;;
  wipe) wipe_local_runtime ;;
  *)
    usage >&2
    exit 2
    ;;
esac
