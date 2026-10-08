#!/usr/bin/env bash
# QuicData environment initialization (Ubuntu / macOS / other Linux)
# Usage: bash scripts/setup.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYPI_INDEX_URL="${PYPI_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

if command -v python3 &>/dev/null; then
  PYTHON=python3
elif command -v python &>/dev/null; then
  PYTHON=python
else
  echo "错误: 未找到 Python，请先安装 Python 3.10+" >&2
  exit 1
fi

PY_MAJOR=$("$PYTHON" -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')

if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ]]; then
  echo "错误: 需要 Python 3.10+" >&2
  exit 1
fi

echo "=== QuicData 环境初始化 ==="
echo "项目目录: $ROOT"

echo "[1/4] 拉取 QRDF SDK..."
bash "$ROOT/scripts/fetch-qrdf.sh"

VENV_DIR="$ROOT/backend/.venv"

# Check if the virtualenv is intact (missing bin/activate indicates corruption; rebuild automatically)
if [[ ! -d "$VENV_DIR" ]] || [[ ! -f "$VENV_DIR/bin/activate" ]]; then
  if [[ -d "$VENV_DIR" ]]; then
    echo "[2/4] 检测到不完整的虚拟环境，重建..."
    rm -rf "$VENV_DIR"
  else
    echo "[2/4] 创建虚拟环境..."
  fi
  "$PYTHON" -m venv "$VENV_DIR"
else
  echo "[2/4] 虚拟环境已存在"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[3/4] 安装后端依赖..."
echo "PyPI 镜像: $PYPI_INDEX_URL"
python -m pip install --index-url "$PYPI_INDEX_URL" --upgrade pip -q
python -m pip install --index-url "$PYPI_INDEX_URL" "$ROOT/backend" -q

echo "[4/4] 创建数据目录并校验 QRDF..."
mkdir -p "$ROOT/backend/runtime/scratch"

# Perform runtime API verification after dependencies are installed; fetch stage only has dependency-free source gates.
if ! python "$ROOT/backend/scripts/verify_qrdf_sdk.py" --vendor-dir "$ROOT/backend/vendor/qrdf"; then
  echo "错误: 依赖已安装，但 QRDF SDK 不满足 QuicData v0.2 API。请检查 backend/vendor/qrdf 与 QRDF_GIT_REF。" >&2
  exit 1
fi

echo ""
echo "初始化完成！"
echo "  QRDF SDK: backend/vendor/qrdf/（已有完整且兼容 v0.2 API 的 SDK 才跳过拉取；强制: QRDF_FORCE_FETCH=1）"
echo "  更新 SDK: bash scripts/fetch-qrdf.sh"
echo "  强制更新: QRDF_FORCE_FETCH=1 bash scripts/fetch-qrdf.sh"
echo "  本地联调: bash scripts/sync-qrdf.sh ../qrdf"
echo "  启动服务: bash scripts/start.sh"
echo "  访问:     http://127.0.0.1:8000"
