#!/usr/bin/env bash
# Pre-deployment dependency fetch (CI/CD entrypoint)
# Usage: bash deploy/bootstrap.sh
#
# QRDF: Remote fetch is skipped only if a complete SDK matching v0.2 API already exists.
# Force update: QRDF_FORCE_FETCH=1 bash deploy/bootstrap.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== QuicData Deploy Bootstrap ==="

if [[ -f "$ROOT/deploy/qrdf.env.example" && ! -f "$ROOT/deploy/qrdf.env" ]]; then
  echo "提示: 可复制 deploy/qrdf.env.example -> deploy/qrdf.env 并配置 QRDF_GIT_URL / QRDF_GIT_REF"
fi

if [[ -f "$ROOT/backend/vendor/qrdf/qrdf/__init__.py" && -f "$ROOT/backend/vendor/qrdf/pyproject.toml" ]]; then
  echo "提示: setup 会校验现有 QRDF 是否满足 v0.2 API；不满足时会自动更新（QRDF_FORCE_FETCH=1 可强制更新）"
fi

bash "$ROOT/scripts/setup.sh"

echo "Deploy bootstrap 完成。"
echo "  基础设施: make infra-up"
echo "            # 覆盖镜像前缀: DOCKER_REGISTRY_PREFIX=registry.example.cn/library make infra-up"
echo "  应用启动: bash scripts/start.sh"
