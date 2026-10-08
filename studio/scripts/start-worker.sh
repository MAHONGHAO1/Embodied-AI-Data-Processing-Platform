#!/usr/bin/env bash
# Start one explicit Batch / Episode JobRun worker. Legacy Task workers are not
# restored; the queue is selected by the caller and enforced by Celery.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUEUE="${1:-${CELERY_QUEUE:-control}}"
case "$QUEUE" in
  ingest|media|publish|export|analytics|governance|ai) QUEUES="$QUEUE" ;;
  control) QUEUES="control,general" ;;
  *) echo "unsupported Batch worker queue: $QUEUE" >&2; exit 2 ;;
esac

PYTHON_BIN=""
for candidate in "$ROOT/backend/.venv/bin/python" "$ROOT/.venv/bin/python"; do
  if [[ -x "$candidate" ]] && "$candidate" -c 'import celery' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
[[ -n "$PYTHON_BIN" ]] || { echo "未找到包含后端依赖的虚拟环境，先运行: bash scripts/setup.sh" >&2; exit 1; }

cd "$ROOT/backend"
"$PYTHON_BIN" -m scripts.verify_redis
exec "$PYTHON_BIN" -m celery -A data.celery_app:celery_app worker \
  --loglevel="${CELERY_LOGLEVEL:-INFO}" \
  --queues="$QUEUES" \
  --concurrency="${CELERY_CONCURRENCY:-1}" \
  --max-tasks-per-child="${CELERY_MAX_TASKS_PER_CHILD:-50}" \
  --max-memory-per-child="${CELERY_MAX_MEMORY_PER_CHILD_KB:-1048576}" \
  --hostname="worker-${QUEUE}@%h"
