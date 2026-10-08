#!/usr/bin/env bash
# Clean up PostgreSQL Batch / Episode business data and local storage.
# Usage:
#   bash scripts/reset-data.sh              # Retain users/workspaces/workspace_members
#   bash scripts/reset-data.sh --full       # Full reset (including users, re-seeded on reboot)
#   bash scripts/reset-data.sh --keep-users # Same as default
#   bash scripts/reset-data.sh --full -y    # Full reset, skip running confirmation

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
STORAGE="$ROOT/deploy/runtime/scratch"
PYTHON_BIN=""
for candidate in "$BACKEND/.venv/bin/python" "$ROOT/.venv/bin/python"; do
  if [ -x "$candidate" ] && "$candidate" -c 'import sqlalchemy' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "未找到包含后端依赖的虚拟环境，请先运行: bash scripts/setup.sh" >&2
  exit 1
fi

FULL=false
ASSUME_YES=false
for arg in "$@"; do
  case "$arg" in
    --full) FULL=true ;;
    --keep-users) FULL=false ;;
    -y|--yes) ASSUME_YES=true ;;
    -h|--help)
      echo "用法: bash scripts/reset-data.sh [--full|--keep-users] [-y]"
      exit 0
      ;;
  esac
done

echo "=== QuicData 数据清理 ==="
echo "存储目录: $STORAGE"
echo "模式: $([ "$FULL" = true ] && echo '完全重置' || echo '保留用户、工作空间与成员关系')"

if [ "$ASSUME_YES" != true ]; then
  if pgrep -f "gunicorn.*data.main:app" >/dev/null 2>&1 \
    || pgrep -f "uvicorn.*data.main:app" >/dev/null 2>&1 \
    || pgrep -f "python.*run.py" >/dev/null 2>&1; then
    echo "警告: 检测到后端可能仍在运行，建议先停止服务再清理，避免文件占用。"
    read -r -p "是否继续? [y/N] " ans
    case "$ans" in
      y|Y|yes|YES) ;;
      *) exit 1 ;;
    esac
  fi
fi

# ---------- Clean local storage files ----------
for sub in hot chunks exports previews cold cloud; do
  if [ -d "$STORAGE/$sub" ]; then
    find "$STORAGE/$sub" -mindepth 1 -delete
    echo "已清空 storage/$sub/"
  fi
done

# ---------- Clean database ----------
cd "$BACKEND"

BUSINESS_TABLES="dataset_episode_items, dataset_revisions, datasets, published_episodes, episode_reviews, episode_annotations, episode_artifacts, episodes, import_attempts, import_sessions, batch_logs, batches, task_labels, embodiments, work_items, job_runs, security_audit_events, projects"
ALL_TABLES="$BUSINESS_TABLES, workspace_members, users, workspaces"

if [ "$FULL" = true ]; then
  RESET_TARGET="$ALL_TABLES"
  echo "将清空所有表（含 users/workspaces/workspace_members）"
else
  RESET_TARGET="$BUSINESS_TABLES"
  echo "将清空业务表（保留 users/workspaces/workspace_members）"
fi

RESET_TARGET="$RESET_TARGET" "$PYTHON_BIN" <<'PY'
import os
import sys

sys.path.insert(0, '.')
from sqlalchemy import inspect, text
from data.database import engine

tables = [name.strip() for name in os.environ['RESET_TARGET'].split(',')]
existing = set(inspect(engine).get_table_names())
tables = [name for name in tables if name in existing]
if engine.dialect.name != 'postgresql':
    raise RuntimeError('reset-data.sh requires PostgreSQL')
quoted = ', '.join(f'"{name}"' for name in tables)
if quoted:
    with engine.begin() as conn:
        conn.execute(text(f'TRUNCATE {quoted} RESTART IDENTITY CASCADE'))
print('PostgreSQL 清空完成，ID 已重置')

with engine.connect() as conn:
    for name in tables:
        count = conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar()
        print(f'  {name}: {count} rows')
PY

# ---------- Redis ----------
if command -v redis-cli >/dev/null 2>&1; then
  redis-cli -n 0 FLUSHDB >/dev/null 2>&1 && echo "已清空 Redis DB0" || true
  redis-cli -n 1 FLUSHDB >/dev/null 2>&1 && echo "已清空 Redis DB1" || true
  redis-cli -n 2 FLUSHDB >/dev/null 2>&1 && echo "已清空 Redis DB2" || true
fi

echo "=== 清理完成，请重启后端: bash scripts/start.sh ==="
