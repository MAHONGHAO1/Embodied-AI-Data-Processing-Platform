#!/usr/bin/env bash
# Run one local development service inside screen and record its direct process ID.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
service="${1:-}"
pid_directory="$ROOT/deploy/runtime/.dev-screen-pids"
log_directory="$ROOT/deploy/runtime/logs"

PYTHON_BIN=""
for candidate in "$ROOT/backend/.venv/bin/python" "$ROOT/.venv/bin/python"; do
  if [[ -x "$candidate" ]] && "$candidate" -c 'import celery' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

case "$service" in
  api|frontend|control|ingest|media|publish|export|analytics|governance|ai|beat) ;;
  *)
    echo "unknown local service: ${service:-<empty>}" >&2
    exit 2
    ;;
esac

mkdir -p "$pid_directory" "$log_directory"
pid_file="$pid_directory/$service.pid"
log_file="$log_directory/$service.log"
: > "$log_file"
echo "$$" > "$pid_file"
exec > >(tee -a "$log_file") 2>&1

case "$service" in
  api)
    exec bash "$ROOT/scripts/start.sh"
    ;;
  frontend)
    frontend_python="${FRONTEND_PYTHON:-python3}"
    frontend_args=(
      "$ROOT/frontend/serve_preview.py"
      --host "${FRONTEND_HOST:-127.0.0.1}"
      --port "${FRONTEND_PORT:-8090}"
      --backend "${FRONTEND_BACKEND:-http://127.0.0.1:${LOCAL_API_PORT:-8000}}"
    )
    case "${FRONTEND_MOCK:-false}" in
      1|true|TRUE|yes|YES) frontend_args+=(--mock) ;;
    esac
    exec "$frontend_python" "${frontend_args[@]}"
    ;;
  control|ingest|media|publish|export|analytics|governance|ai)
    exec bash "$ROOT/scripts/start-worker.sh" "$service"
    ;;
  beat)
    [[ -n "$PYTHON_BIN" ]] || { echo "未找到包含后端依赖的虚拟环境，先运行: bash scripts/setup.sh" >&2; exit 1; }
    (cd "$ROOT/backend" && "$PYTHON_BIN" -m scripts.verify_redis)
    cd "$ROOT/backend"
    exec "$PYTHON_BIN" -m celery -A data.celery_app:celery_app beat --loglevel="${CELERY_LOGLEVEL:-INFO}"
    ;;
esac
