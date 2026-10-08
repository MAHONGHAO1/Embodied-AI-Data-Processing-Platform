#!/usr/bin/env bash
# Preflight checks before ECS Compose up (no secrets printed).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ECS_DIR="${ROOT_DIR}/infra/ecs"
ENV_FILE="${ECS_DIR}/.env"
COMPOSE_FILE="${ECS_DIR}/docker-compose.yml"
fail=0

need_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "MISSING ${path}"
    fail=1
  else
    echo "OK      ${path}"
  fi
}

echo "== QuicTrain ECS preflight (shared-edge) =="
need_file "${COMPOSE_FILE}"
need_file "${ENV_FILE}"
need_file "${ECS_DIR}/secrets/postgres_password"
need_file "${ECS_DIR}/secrets/bootstrap_admin_token"
need_file "${ECS_DIR}/secrets/admin_password"

auth_mode="open"
if [[ -f "${ENV_FILE}" ]]; then
  auth_mode=$(grep -E '^QUICTRAIN_AUTH_MODE=' "${ENV_FILE}" | head -1 | cut -d= -f2- || true)
  auth_mode="${auth_mode:-local}"
fi
if [[ "${auth_mode}" == "local" ]]; then
  if [[ ! -s "${ECS_DIR}/secrets/admin_password" ]]; then
    echo "FAIL    secrets/admin_password empty (required for AUTH_MODE=local)"
    fail=1
  else
    echo "OK      secrets/admin_password present for local multi-user"
  fi
fi

if [[ -f "${ECS_DIR}/secrets/web_password_hash" ]]; then
  echo "OK      ${ECS_DIR}/secrets/web_password_hash (optional; mlflow break-glass only)"
else
  echo "WARN    ${ECS_DIR}/secrets/web_password_hash absent (optional mlflow edge auth)"
fi

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  # Only load KEY=VALUE lines; do not echo values.
  while IFS= read -r line; do
    [[ -z "${line}" || "${line}" =~ ^# ]] && continue
    key="${line%%=*}"
    case "${key}" in
      PUBLIC_HOSTNAME|ALIYUN_OSS_BUCKET|ALIYUN_DLC_WORKSPACE_ID|ALIYUN_DLC_RESOURCE_ID|QUICTRAIN_CPFS_DATA_SOURCE_ID|QUICTRAIN_CPFS_ROOT_URI)
        value="${line#*=}"
        if [[ -z "${value}" ]]; then
          echo "EMPTY   ${key}"
          fail=1
        else
          echo "SET     ${key}"
        fi
        ;;
    esac
  done < "${ENV_FILE}"
  set +a
fi

if [[ -d /mnt/cpfs ]]; then
  echo "OK      host /mnt/cpfs present"
else
  echo "WARN    host /mnt/cpfs missing (API/Scheduler export/materialize need it on ECS)"
fi

if [[ -f /opt/caddy/docker-compose.yml ]]; then
  echo "OK      shared /opt/caddy edge present"
else
  echo "WARN    /opt/caddy missing — this host expects shared Caddy for 80/443"
fi

# Compose must not claim public edge or data-platform ports.
if grep -E '"80:80"|"443:443"' "${COMPOSE_FILE}" >/dev/null 2>&1; then
  echo "FAIL    compose still publishes 80/443 (use shared /opt/caddy)"
  fail=1
else
  echo "OK      compose does not publish 80/443"
fi
if grep -E 'gateway:' "${COMPOSE_FILE}" >/dev/null 2>&1; then
  echo "FAIL    compose still defines gateway service"
  fail=1
else
  echo "OK      compose has no in-stack gateway"
fi
for binding in '127.0.0.1:8001:8000' '127.0.0.1:3000:3000' '127.0.0.1:5001:5000'; do
  if grep -F "${binding}" "${COMPOSE_FILE}" >/dev/null 2>&1; then
    echo "OK      loopback ${binding}"
  else
    echo "FAIL    missing loopback publish ${binding}"
    fail=1
  fi
done
if grep -E '"5432:5432"|"6379:6379"|"8000:8000"' "${COMPOSE_FILE}" >/dev/null 2>&1; then
  echo "FAIL    compose must not publish 5432/6379/8000 (data platform)"
  fail=1
else
  echo "OK      compose avoids data-platform host ports 5432/6379/8000"
fi

if command -v docker >/dev/null 2>&1; then
  if docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" config >/dev/null 2>&1; then
    echo "OK      docker compose config"
  else
    echo "FAIL    docker compose config"
    fail=1
  fi
else
  echo "WARN    docker not installed on this host"
fi

if [[ "${fail}" -ne 0 ]]; then
  echo "Preflight FAILED"
  exit 1
fi
echo "Preflight PASSED"
