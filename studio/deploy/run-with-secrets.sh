#!/usr/bin/env bash
# Build process-local service URLs from Docker Compose file secrets.
set -euo pipefail

SECRETS_DIR="${SECRETS_DIR:-/run/secrets}"

read_secret() {
  local name="$1"
  local path="${SECRETS_DIR}/${name}"
  if [[ ! -r "$path" ]]; then
    echo "required runtime secret is unavailable: ${name}" >&2
    exit 2
  fi

  local value
  value="$(tr -d '\r\n' < "$path")"
  if [[ -z "$value" ]]; then
    echo "required runtime secret is empty: ${name}" >&2
    exit 2
  fi
  printf '%s' "$value"
}

read_optional_secret() {
  local name="$1"
  local path="${SECRETS_DIR}/${name}"
  if [[ ! -e "$path" ]]; then
    printf ''
    return
  fi
  if [[ ! -r "$path" ]]; then
    echo "optional runtime secret is unreadable: ${name}" >&2
    exit 2
  fi
  tr -d '\r\n' < "$path"
}

postgres_password="$(read_secret postgres_password)"
redis_password="$(read_secret redis_password)"
app_secret_key="$(read_secret app_secret_key)"
export SECRET_KEY="$app_secret_key"
export DATABASE_URL="postgresql+psycopg://quicdata:${postgres_password}@postgres:5432/quicdata?connect_timeout=5"
export REDIS_URL="redis://:${redis_password}@redis-state:6379/0"
export CELERY_BROKER_URL="redis://:${redis_password}@redis-broker:6379/0"
export CELERY_RESULT_BACKEND="redis://:${redis_password}@redis-broker:6379/1"

storage_access_key_id="$(read_secret storage_access_key_id)"
storage_secret_access_key="$(read_secret storage_secret_access_key)"
if [[ -z "$storage_access_key_id" || -z "$storage_secret_access_key" ]]; then
    echo "storage credentials must provide both storage_access_key_id and storage_secret_access_key" >&2
    exit 2
fi
export STORAGE_ACCESS_KEY_ID="$storage_access_key_id"
export STORAGE_SECRET_ACCESS_KEY="$storage_secret_access_key"

python -m scripts.verify_redis
exec "$@"
