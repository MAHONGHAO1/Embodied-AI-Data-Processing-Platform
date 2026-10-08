#!/usr/bin/env bash
# QuicData service startup (Ubuntu / macOS / other Linux)
# Usage: bash scripts/start.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN=""
for candidate in "$ROOT/backend/.venv/bin/python" "$ROOT/.venv/bin/python"; do
  if [[ -x "$candidate" ]] && "$candidate" -c 'import gunicorn, uvicorn' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "未找到包含后端依赖的虚拟环境，先运行: bash scripts/setup.sh" >&2
  exit 1
fi

echo "=== QuicData v0.2 启动 ==="
echo "平台: $(uname -s)"
export ENVIRONMENT="${ENVIRONMENT:-development}"
API_HOST_VALUE="${API_HOST:-0.0.0.0}"
API_PORT_VALUE="${API_PORT:-8000}"
echo "监听: ${API_HOST_VALUE}:${API_PORT_VALUE}"
echo "本机: http://127.0.0.1:${API_PORT_VALUE}"
LAN_IP=""
if [[ "$(uname -s)" == "Darwin" ]] && command -v ipconfig >/dev/null 2>&1; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
elif command -v hostname >/dev/null 2>&1; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi
if [[ -n "$LAN_IP" ]]; then
  echo "局域网: http://${LAN_IP}:${API_PORT_VALUE}"
fi
echo "API 文档: http://127.0.0.1:${API_PORT_VALUE}/docs"
echo "开发演示数据: make dev-seed"
echo "生产管理员: make prod-bootstrap-admin ADMIN_EMAIL=admin@example.com"
echo "按 Ctrl+C 停止服务"
echo ""

cd "$ROOT/backend"
if ! "$PYTHON_BIN" -c 'from data.runtime import assert_schema_current; assert_schema_current()'; then
  echo "数据库尚未迁移到当前版本，先运行: make db-upgrade" >&2
  exit 1
fi

exec "$PYTHON_BIN" -m gunicorn data.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers "${WEB_CONCURRENCY:-1}" \
  --bind "${API_HOST:-0.0.0.0}:${API_PORT:-8000}" \
  --timeout "${GUNICORN_TIMEOUT:-300}"
