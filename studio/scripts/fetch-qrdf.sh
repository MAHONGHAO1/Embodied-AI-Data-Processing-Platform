#!/usr/bin/env bash
# Fetch QRDF SDK from Git into backend/vendor/qrdf/ (invoked during deploy / setup)
#
# Defaults to tracking the develop branch of the QRDF repository.
# If backend/vendor/qrdf/ already contains a complete SDK satisfying the v0.2 API, remote fetch is skipped by default.
#
# Usage:
#   bash scripts/fetch-qrdf.sh
#   QRDF_FORCE_FETCH=1 bash scripts/fetch-qrdf.sh         # Force re-fetch / update
#   QRDF_GIT_REF=main bash scripts/fetch-qrdf.sh          # Switch to another branch / tag
#   QRDF_GIT_DEPTH=0 bash scripts/fetch-qrdf.sh           # Full clone (more reliable for tracking long-lived branches)
#   QRDF_LOCAL_PATH=../qrdf bash scripts/fetch-qrdf.sh    # Local development: sync from sibling directory

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_DIR="$ROOT/backend/vendor/qrdf"
CONFIG="${QRDF_ENV_FILE:-$ROOT/deploy/qrdf.env}"
DEFAULT_QRDF_GIT_URL="git@codeup.aliyun.com:6a3ce6c6a6fcee143fa25a90/QuicData/qrdf.git"

if [[ -f "$CONFIG" ]]; then
  # Support CRLF line endings (from .env saved by Windows editors), strip \r before sourcing
  # shellcheck disable=SC1090
  set -a && source <(tr -d '\r' < "$CONFIG") && set +a
fi

QRDF_GIT_URL="${QRDF_GIT_URL:-$DEFAULT_QRDF_GIT_URL}"
QRDF_GIT_REF="${QRDF_GIT_REF:-develop}"
QRDF_GIT_DEPTH="${QRDF_GIT_DEPTH:-1}"
QRDF_FORCE_FETCH="${QRDF_FORCE_FETCH:-0}"

_vendor_sdk_complete() {
  # Complete SDK: Python package entrypoint + repo root pyproject.toml exist, and not a local temporary stub
  if [[ ! -f "$VENDOR_DIR/qrdf/__init__.py" ]]; then
    return 1
  fi
  if [[ ! -f "$VENDOR_DIR/pyproject.toml" ]]; then
    return 1
  fi
  if grep -qE '0\.0\.0-stub|Minimal QRDF stub' "$VENDOR_DIR/qrdf/__init__.py" 2>/dev/null; then
    return 1
  fi
  return 0
}

_vendor_sdk_supported() {
  # QuicData requires schema provenance and contained episode data-file APIs.
  # Keep this source-level check dependency-free: fetch runs before pip install.
  local reader="$VENDOR_DIR/qrdf/mcap/reader.py"
  local episode="$VENDOR_DIR/qrdf/models/episode.py"
  local topic_layout="$VENDOR_DIR/qrdf/registry/topic_layout.py"
  if [[ ! -f "$reader" || ! -f "$episode" ]]; then
    return 1
  fi
  grep -q 'canonical_schema_name' "$reader" \
    && grep -q 'descriptor_format' "$reader" \
    && grep -q 'is_legacy_schema' "$reader" \
    && grep -q 'def get_canonical_schema_name' "$reader" \
    && grep -q 'def resolve_data_file' "$episode" \
    && grep -q 'EGO_EPISODE_TYPE_ALIASES' "$topic_layout"
}

_sync_from_local() {
  local src="$1"
  if [[ ! -d "$src/qrdf" ]]; then
    echo "错误: 本地 QRDF 路径无效（缺少 qrdf/ 包）: $src" >&2
    exit 1
  fi
  mkdir -p "$VENDOR_DIR"
  # A sibling checkout often contains deliberately ignored local notes and
  # generated artifacts. They are not deployable QRDF inputs and can break
  # the SDK's own exact-documentation checks when copied into vendor.
  local ignored_file=""
  if git -C "$src" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    ignored_file="$(mktemp "${TMPDIR:-/tmp}/qrdf-sync-ignored.XXXXXX")"
    # git reports paths relative to the checkout. Anchor them so an ignored
    # root ``data/`` does not accidentally exclude tracked ``examples/data/``.
    git -C "$src" ls-files --others --ignored --exclude-standard --directory \
      | sed 's|^|/|' > "$ignored_file"
  fi

  local -a rsync_args=(
    -a
    --delete
    --delete-excluded
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.venv' \
    --exclude='build' \
    --exclude='dist' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.egg-info' \
    --exclude='.DS_Store' \
  )
  if [[ -n "$ignored_file" ]]; then
    rsync_args+=(--exclude-from="$ignored_file")
  fi
  if ! rsync "${rsync_args[@]}" "$src/" "$VENDOR_DIR/"; then
    rm -f "$ignored_file"
    echo "错误: 从本地同步 QRDF 失败" >&2
    exit 1
  fi
  # `backend/vendor/qrdf` is ignored except for this tracked directory marker.
  # Keep it after rsync's delete pass so a local development sync is clean.
  touch "$VENDOR_DIR/.gitkeep"
  rm -f "$ignored_file"
  echo "已从本地同步 QRDF -> $VENDOR_DIR"
}

_current_git_ref() {
  if [[ ! -d "$VENDOR_DIR/.git" ]]; then
    echo ""
    return
  fi
  git -C "$VENDOR_DIR" symbolic-ref -q --short HEAD 2>/dev/null \
    || git -C "$VENDOR_DIR" rev-parse --short HEAD 2>/dev/null \
    || echo ""
}

_fetch_ref_from_origin() {
  local ref="$1"
  if [[ "$QRDF_GIT_DEPTH" != "0" && -n "$QRDF_GIT_DEPTH" ]]; then
    git -C "$VENDOR_DIR" fetch --depth "$QRDF_GIT_DEPTH" origin "$ref" 2>/dev/null || true
  fi
  git -C "$VENDOR_DIR" fetch --tags origin "$ref" 2>/dev/null \
    || git -C "$VENDOR_DIR" fetch origin "$ref" 2>/dev/null \
    || git -C "$VENDOR_DIR" fetch --unshallow origin 2>/dev/null \
    || git -C "$VENDOR_DIR" fetch origin
}

_checkout_ref() {
  local ref="$1"
  if git -C "$VENDOR_DIR" show-ref --verify --quiet "refs/remotes/origin/$ref"; then
    git -C "$VENDOR_DIR" checkout -f -B "$ref" "origin/$ref"
    git -C "$VENDOR_DIR" reset --hard "origin/$ref"
    return 0
  fi
  if git -C "$VENDOR_DIR" rev-parse --verify FETCH_HEAD >/dev/null 2>&1; then
    git -C "$VENDOR_DIR" checkout -f -B "$ref" FETCH_HEAD
    git -C "$VENDOR_DIR" reset --hard FETCH_HEAD
    return 0
  fi
  if git -C "$VENDOR_DIR" show-ref --verify --quiet "refs/heads/$ref"; then
    git -C "$VENDOR_DIR" checkout -f "$ref"
    git -C "$VENDOR_DIR" pull --ff-only origin "$ref" 2>/dev/null \
      || git -C "$VENDOR_DIR" reset --hard "$ref"
    return 0
  fi
  echo "错误: 无法切换到 QRDF 引用 '$ref'，请确认远程分支存在" >&2
  echo "  提示: git ls-remote --heads $QRDF_GIT_URL" >&2
  exit 1
}

_update_from_git() {
  echo "更新 QRDF SDK: $VENDOR_DIR ($QRDF_GIT_REF)"
  _fetch_ref_from_origin "$QRDF_GIT_REF"
  _checkout_ref "$QRDF_GIT_REF"
}

_clone_from_git() {
  echo "克隆 QRDF SDK -> $VENDOR_DIR"
  echo "  url: $QRDF_GIT_URL"
  echo "  ref: $QRDF_GIT_REF"

  local tmp_dir
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/qrdf-vendor.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp_dir'" RETURN

  local cloned=0
  if [[ "$QRDF_GIT_DEPTH" != "0" && -n "$QRDF_GIT_DEPTH" ]]; then
    if git clone --depth "$QRDF_GIT_DEPTH" --single-branch --branch "$QRDF_GIT_REF" \
      "$QRDF_GIT_URL" "$tmp_dir" 2>/dev/null; then
      cloned=1
    elif git clone --depth "$QRDF_GIT_DEPTH" "$QRDF_GIT_URL" "$tmp_dir" 2>/dev/null; then
      cloned=1
    fi
  else
    if git clone --single-branch --branch "$QRDF_GIT_REF" "$QRDF_GIT_URL" "$tmp_dir" 2>/dev/null; then
      cloned=1
    elif git clone "$QRDF_GIT_URL" "$tmp_dir" 2>/dev/null; then
      cloned=1
    fi
  fi

  if [[ "$cloned" -ne 1 ]]; then
    echo "错误: 无法克隆 QRDF 仓库: $QRDF_GIT_URL" >&2
    echo "  请检查网络、SSH 密钥及远程是否存在分支 '$QRDF_GIT_REF'" >&2
    exit 1
  fi

  if [[ -d "$tmp_dir/.git" ]]; then
    local current_ref
    current_ref="$(git -C "$tmp_dir" symbolic-ref -q --short HEAD 2>/dev/null || true)"
    if [[ "$current_ref" != "$QRDF_GIT_REF" ]]; then
      if [[ "$QRDF_GIT_DEPTH" != "0" && -n "$QRDF_GIT_DEPTH" ]]; then
        git -C "$tmp_dir" fetch --depth "$QRDF_GIT_DEPTH" origin "$QRDF_GIT_REF" 2>/dev/null || true
      fi
      git -C "$tmp_dir" fetch origin "$QRDF_GIT_REF" 2>/dev/null || true
      if git -C "$tmp_dir" show-ref --verify --quiet "refs/remotes/origin/$QRDF_GIT_REF"; then
        git -C "$tmp_dir" checkout -f -B "$QRDF_GIT_REF" "origin/$QRDF_GIT_REF"
      else
        git -C "$tmp_dir" checkout -f -B "$QRDF_GIT_REF" FETCH_HEAD
      fi
    fi
  fi

  rm -rf "$VENDOR_DIR"
  mkdir -p "$(dirname "$VENDOR_DIR")"
  mv "$tmp_dir" "$VENDOR_DIR"
  trap - RETURN
}

_fetch_from_git() {
  if ! command -v git &>/dev/null; then
    echo "错误: 未找到 git，无法拉取 QRDF SDK" >&2
    exit 1
  fi

  if [[ -d "$VENDOR_DIR/.git" ]]; then
    local current_ref
    current_ref="$(_current_git_ref)"
    # Shallow clones often track only a single branch; recloning is cleaner when switching branches
    if [[ -n "$current_ref" && "$current_ref" != "$QRDF_GIT_REF" ]]; then
      echo "检测到分支切换 ($current_ref -> $QRDF_GIT_REF)，重新克隆..."
      _clone_from_git
    else
      _update_from_git
    fi
  else
    _clone_from_git
  fi
}

_read_qrdf_version() {
  # Do not import full package (prevents failures before dependencies like numpy are installed)
  local version_file="$VENDOR_DIR/qrdf/version.py"
  if [[ ! -f "$version_file" ]]; then
    echo "unknown"
    return
  fi
  local ver
  ver="$(sed -n 's/^QRDF_VERSION[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$version_file" | head -n1)"
  if [[ -z "$ver" ]]; then
    echo "unknown"
  else
    echo "$ver"
  fi
}

_try_import_qrdf() {
  # Prefer ready backend venv; only warn on failure (setup verifies hard after installing dependencies)
  local py=""
  if [[ -x "$ROOT/backend/.venv/bin/python" ]]; then
    py="$ROOT/backend/.venv/bin/python"
  elif command -v python3 &>/dev/null; then
    py="python3"
  elif command -v python &>/dev/null; then
    py="python"
  else
    return 0
  fi

  local out
  if out="$("$py" -c "import sys; sys.path.insert(0, r'$VENDOR_DIR'); import qrdf; print('QRDF', qrdf.__version__, '->', qrdf.__file__)" 2>&1)"; then
    echo "$out"
    return 0
  fi

  if [[ "$out" == *"ModuleNotFoundError"* ]] || [[ "$out" == *"ImportError"* ]]; then
    echo "提示: QRDF 包结构就绪，但当前 Python 尚未安装运行时依赖（如 numpy）；将在 setup 安装依赖后校验完整 import"
    return 0
  fi

  echo "警告: QRDF import 校验未通过（不阻断拉取）:" >&2
  echo "$out" >&2
  return 0
}

_verify() {
  if [[ ! -f "$VENDOR_DIR/qrdf/__init__.py" ]]; then
    echo "错误: QRDF SDK 拉取后结构不正确，期望 $VENDOR_DIR/qrdf/__init__.py" >&2
    exit 1
  fi
  if [[ ! -f "$VENDOR_DIR/pyproject.toml" ]]; then
    echo "错误: QRDF SDK 拉取后缺少 pyproject.toml: $VENDOR_DIR/pyproject.toml" >&2
    exit 1
  fi
  if ! _vendor_sdk_supported; then
    echo "错误: QRDF SDK 不提供 QuicData 所需的 v0.2 API（schema provenance、get_canonical_schema_name、resolve_data_file）" >&2
    echo "  请使用兼容的 QRDF_GIT_REF，或设置 QRDF_FORCE_FETCH=1 后重试。" >&2
    exit 1
  fi
  if [[ -d "$VENDOR_DIR/.git" ]]; then
    local head branch
    branch="$(git -C "$VENDOR_DIR" symbolic-ref -q --short HEAD 2>/dev/null || echo detached)"
    head="$(git -C "$VENDOR_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo "QRDF Git: branch=$branch commit=$head version=$(_read_qrdf_version)"
  elif _vendor_sdk_complete; then
    echo "QRDF Vendor: 使用本地已有完整 SDK（非 git checkout） version=$(_read_qrdf_version)"
  else
    echo "QRDF Vendor: version=$(_read_qrdf_version)"
  fi
  _try_import_qrdf
}

echo "=== 准备 QRDF SDK (ref: $QRDF_GIT_REF) ==="

if [[ -n "${QRDF_LOCAL_PATH:-}" ]]; then
  _sync_from_local "$(cd "$QRDF_LOCAL_PATH" && pwd)"
elif [[ "$QRDF_FORCE_FETCH" != "1" ]] && _vendor_sdk_complete && _vendor_sdk_supported; then
  echo "检测到已有兼容 QRDF SDK，跳过远程拉取: $VENDOR_DIR"
  echo "  强制更新: QRDF_FORCE_FETCH=1 bash scripts/fetch-qrdf.sh"
else
  if _vendor_sdk_complete && ! _vendor_sdk_supported; then
    echo "检测到已有 QRDF SDK 缺少所需 v0.2 API，正在更新: $VENDOR_DIR"
  fi
  if [[ "$QRDF_FORCE_FETCH" == "1" ]]; then
    echo "QRDF_FORCE_FETCH=1，强制从远程拉取: $QRDF_GIT_URL"
  fi
  _fetch_from_git
fi

_verify
echo "QRDF SDK 就绪: $VENDOR_DIR"
