#!/usr/bin/env bash
# Check Docker permissions and start infrastructure (PostgreSQL / Redis)
# Usage:
#   bash deploy/ensure-infra.sh
#   bash deploy/ensure-infra.sh --fix      # Fix docker group / socket (requires sudo)
#   bash deploy/ensure-infra.sh --mirror   # Write registry-mirrors and restart Docker (requires sudo)
#   bash deploy/ensure-infra.sh --fix --mirror
#   bash deploy/ensure-infra.sh --down
#
# Common errors:
#   permission denied ... docker.sock  → --fix
#   dial tcp ... registry-1.docker.io ... i/o timeout → --mirror
#     or: DOCKER_REGISTRY_PREFIX=docker.m.daocloud.io/library bash deploy/ensure-infra.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/docker-compose.yml"
MIRROR_EXAMPLE="$ROOT/deploy/docker-daemon-mirrors.json.example"
DO_FIX=0
DO_MIRROR=0
DO_DOWN=0

# Default registry mirrors (probed reachable locally; overridable via env, comma-separated)
DEFAULT_MIRRORS="${DOCKER_REGISTRY_MIRRORS:-https://docker.m.daocloud.io,https://docker.1ms.run,https://docker.1panel.live}"

for arg in "$@"; do
  case "$arg" in
    --fix|-f) DO_FIX=1 ;;
    --mirror|-m) DO_MIRROR=1 ;;
    --down|-d) DO_DOWN=1 ;;
    --help|-h)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "未知参数: $arg（支持 --fix / --mirror / --down / --help）" >&2
      exit 1
      ;;
  esac
done

_have_docker_cli() {
  command -v docker &>/dev/null
}

_run_as_docker_group() {
  if id -nG 2>/dev/null | grep -qw docker; then
    "$@"
  elif command -v sg &>/dev/null && getent group docker &>/dev/null; then
    local cmd
    cmd="$(printf '%q ' "$@")"
    sg docker -c "$cmd"
  else
    "$@"
  fi
}

_compose() {
  # Default to registry mirror proxy; deployers can override via DOCKER_REGISTRY_PREFIX.
  _run_as_docker_group env \
    "DOCKER_REGISTRY_PREFIX=${DOCKER_REGISTRY_PREFIX:-docker.m.daocloud.io/library}" \
    docker compose -f "$COMPOSE_FILE" "$@"
}

_print_install_hint() {
  cat >&2 <<'EOF'
错误: 未找到 docker 命令。

路径 A — snap（Ubuntu 交互提示常见）:
  sudo snap install docker
  bash deploy/ensure-infra.sh --fix --mirror

路径 B — apt（Ubuntu 官方源；注意不是 docker-compose-plugin）:
  sudo apt update
  sudo apt install -y docker.io docker-compose-v2
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"
  newgrp docker
  bash deploy/ensure-infra.sh --mirror
EOF
}

_print_permission_hint() {
  local sock="${DOCKER_HOST:-unix:///var/run/docker.sock}"
  sock="${sock#unix://}"
  cat >&2 <<EOF
错误: 当前用户无法访问 Docker API（permission denied）。

诊断:
  用户: $(id -un)  组: $(id -nG)
  socket: $(ls -la "$sock" 2>/dev/null || echo "不存在: $sock")
  docker 组: $(getent group docker || echo "不存在")

一键修复（需要 sudo）:
  bash deploy/ensure-infra.sh --fix

临时绕过（不推荐长期）:
  sudo docker compose -f deploy/docker-compose.yml up -d
EOF
}

_print_registry_hint() {
  cat >&2 <<'EOF'
错误: 无法从 Docker Hub 拉取镜像（常见于访问 registry-1.docker.io 超时）。

方案 1 — 配置 daemon 镜像加速（推荐）:
  bash deploy/ensure-infra.sh --mirror

方案 2 — 拉取时改用国内代理前缀（不改 daemon）:
  DOCKER_REGISTRY_PREFIX=docker.m.daocloud.io/library bash deploy/ensure-infra.sh

可选加速地址示例:
  https://docker.m.daocloud.io
  https://docker.1ms.run
  https://docker.1panel.live

自定义加速列表（逗号分隔）:
  DOCKER_REGISTRY_MIRRORS=https://docker.1ms.run,https://docker.m.daocloud.io \
    bash deploy/ensure-infra.sh --mirror
EOF
}

_daemon_json_path() {
  if [[ -f /var/snap/docker/current/config/daemon.json ]]; then
    echo /var/snap/docker/current/config/daemon.json
    return
  fi
  echo /etc/docker/daemon.json
}

_restart_docker() {
  local sock="/var/run/docker.sock"
  if command -v snap &>/dev/null && snap list docker &>/dev/null 2>&1; then
    sudo snap restart docker
  elif command -v systemctl &>/dev/null; then
    sudo systemctl daemon-reload 2>/dev/null || true
    sudo systemctl restart docker
  else
    echo "警告: 无法自动重启 Docker，请手动重启后再试" >&2
    return 1
  fi
  for _ in $(seq 1 30); do
    [[ -S "$sock" ]] && _run_as_docker_group docker info &>/dev/null && return 0
    sleep 0.5
  done
  # Snap occasionally resets socket group ownership
  if [[ -S "$sock" ]] && [[ "$(stat -c '%G' "$sock" 2>/dev/null || true)" != "docker" ]]; then
    sudo chgrp docker "$sock" 2>/dev/null || true
    sudo chmod 660 "$sock" 2>/dev/null || true
  fi
  _run_as_docker_group docker info &>/dev/null
}

_fix_docker_access() {
  local sock="/var/run/docker.sock"

  if ! command -v sudo &>/dev/null; then
    echo "错误: 需要 sudo 才能修复 Docker 权限" >&2
    exit 1
  fi

  echo "=== 修复 Docker 访问权限 ==="
  echo "将创建 docker 组（若不存在）、把 $(id -un) 加入该组，并校正 socket 属组"

  sudo groupadd --system docker 2>/dev/null || true
  sudo usermod -aG docker "$(id -un)"

  if [[ ! -S "$sock" ]]; then
    echo "提示: $sock 尚不存在，尝试启动 Docker 服务..."
    if command -v snap &>/dev/null && snap list docker &>/dev/null 2>&1; then
      sudo snap start docker 2>/dev/null || sudo snap restart docker
    elif command -v systemctl &>/dev/null; then
      sudo systemctl start docker
    fi
  fi

  if [[ -S "$sock" ]]; then
    sudo chgrp docker "$sock"
    sudo chmod 660 "$sock"
  fi

  if command -v snap &>/dev/null && snap list docker &>/dev/null 2>&1; then
    echo "检测到 snap 版 Docker，重启服务以使属组生效..."
    _restart_docker || true
    if [[ -S "$sock" ]] && [[ "$(stat -c '%G' "$sock" 2>/dev/null || true)" != "docker" ]]; then
      sudo chgrp docker "$sock"
      sudo chmod 660 "$sock"
    fi
  fi

  echo "权限修复命令已执行。"
  if _run_as_docker_group docker info &>/dev/null; then
    echo "Docker API 已可访问（通过 docker 组）。"
  else
    echo "提示: 若仍然 permission denied，请执行 newgrp docker 或重新登录后再试。" >&2
  fi
}

_apply_registry_mirrors() {
  if ! command -v sudo &>/dev/null; then
    echo "错误: 需要 sudo 才能写入 Docker daemon 镜像加速配置" >&2
    exit 1
  fi
  if ! command -v python3 &>/dev/null; then
    echo "错误: 需要 python3 以合并 daemon.json" >&2
    exit 1
  fi

  local target
  target="$(_daemon_json_path)"
  echo "=== 配置 Docker Hub 镜像加速 ==="
  echo "目标文件: $target"
  echo "镜像列表: $DEFAULT_MIRRORS"

  local tmp
  tmp="$(mktemp)"
  # shellcheck disable=SC2064
  trap "rm -f '$tmp'" RETURN

  DOCKER_MIRROR_TARGET="$target" \
  DOCKER_MIRROR_LIST="$DEFAULT_MIRRORS" \
  DOCKER_MIRROR_EXAMPLE="$MIRROR_EXAMPLE" \
  python3 - <<'PY' > "$tmp"
import json, os, pathlib

target = pathlib.Path(os.environ["DOCKER_MIRROR_TARGET"])
mirrors = [x.strip() for x in os.environ["DOCKER_MIRROR_LIST"].split(",") if x.strip()]
example = pathlib.Path(os.environ.get("DOCKER_MIRROR_EXAMPLE", ""))

cfg = {}
if target.is_file():
    cfg = json.loads(target.read_text(encoding="utf-8") or "{}")
elif example.is_file():
    cfg = json.loads(example.read_text(encoding="utf-8") or "{}")

# Deduplicate preserving order
existing = cfg.get("registry-mirrors") or []
merged = []
for m in list(mirrors) + list(existing):
    if m and m not in merged:
        merged.append(m)
cfg["registry-mirrors"] = merged
print(json.dumps(cfg, indent=4, ensure_ascii=False))
PY

  if [[ "$target" == /etc/docker/daemon.json ]]; then
    sudo mkdir -p /etc/docker
  fi
  sudo cp "$tmp" "$target"
  sudo chmod 644 "$target"

  echo "已写入 registry-mirrors，正在重启 Docker..."
  if ! _restart_docker; then
    echo "错误: Docker 重启后仍不可用，请检查 $target 语法" >&2
    exit 1
  fi

  echo "当前 Registry Mirrors:"
  _run_as_docker_group docker info 2>/dev/null | sed -n '/Registry Mirrors/,/^[^ ]/p' | head -n 20 || true
}

_looks_like_registry_error() {
  local msg="$1"
  [[ "$msg" == *registry-1.docker.io* ]] \
    || [[ "$msg" == *"i/o timeout"* ]] \
    || [[ "$msg" == *"failed to resolve reference"* ]] \
    || [[ "$msg" == *"TLS handshake timeout"* ]] \
    || { [[ "$msg" == *"connection refused"* ]] && [[ "$msg" == *docker.io* ]]; }
}

echo "=== QuicData 基础设施 ==="

if ! _have_docker_cli; then
  _print_install_hint
  exit 1
fi

if [[ "$DO_FIX" -eq 1 ]]; then
  _fix_docker_access
fi

if [[ "$DO_MIRROR" -eq 1 ]]; then
  _apply_registry_mirrors
fi

if ! _run_as_docker_group docker info &>/dev/null; then
  _print_permission_hint
  exit 1
fi

if [[ "$DO_DOWN" -eq 1 ]]; then
  echo "停止基础设施..."
  _compose down
  echo "已停止。"
  exit 0
fi

echo "启动 PostgreSQL / Redis..."
if [[ -n "${DOCKER_REGISTRY_PREFIX:-}" ]]; then
  echo "使用 DOCKER_REGISTRY_PREFIX=${DOCKER_REGISTRY_PREFIX}"
fi

set +e
up_out="$(_compose up -d 2>&1)"
up_code=$?
set -e
printf '%s\n' "$up_out"

if [[ "$up_code" -ne 0 ]]; then
  if _looks_like_registry_error "$up_out"; then
    echo "" >&2
    _print_registry_hint
  fi
  exit "$up_code"
fi

echo ""
echo "服务状态:"
_compose ps

echo ""
echo "基础设施就绪。接下来可:"
echo "  1) 配置 backend/.env（可从 backend/.env.example 复制）"
echo "  2) bash scripts/start.sh"
echo "  3) （完整栈）bash scripts/start-worker.sh"
