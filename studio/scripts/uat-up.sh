#!/usr/bin/env bash
# Deploy the Batch / Episode stack to one explicitly selected UAT compose project.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${QUICDATA_REPO_ROOT:-}" ]]; then
  ROOT="$QUICDATA_REPO_ROOT"
elif [[ -d "$SCRIPT_DIR/../deploy" ]]; then
  # Invoked from the repository's scripts/ directory.
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [[ -d "$SCRIPT_DIR/quic_studio/deploy" ]]; then
  # Invoked as the stable /opt/quicstudio/uat/uat-up.sh host wrapper.
  ROOT="$SCRIPT_DIR/quic_studio"
else
  echo "could not locate the UAT repository; set QUICDATA_REPO_ROOT" >&2
  exit 2
fi
DEPLOY_DIR="${QUICDATA_DEPLOY_DIR:-$ROOT/deploy}"
ENV_FILE="${QUICDATA_ENV_FILE:-$DEPLOY_DIR/.env}"
COMPOSE_FILE="${QUICDATA_COMPOSE_FILE:-$DEPLOY_DIR/docker-compose.prod.yml}"
PROJECT="${QUICSTUDIO_COMPOSE_PROJECT:-quicstudio-uat}"
HEALTH_URL="${UAT_HEALTH_URL:-http://127.0.0.1:18080/health}"
SECRETS_DIR="${QUICDATA_SECRETS_DIR:-${DEPLOY_SECRETS_DIR:-$DEPLOY_DIR/secrets}}"

[[ -f "$ENV_FILE" ]] || { echo "missing UAT env file: $ENV_FILE" >&2; exit 2; }
[[ -f "$COMPOSE_FILE" ]] || { echo "missing UAT compose file: $COMPOSE_FILE" >&2; exit 2; }

for secret in app_secret_key postgres_password redis_password storage_access_key_id storage_secret_access_key; do
  if [[ ! -s "$SECRETS_DIR/$secret" ]]; then
    echo "UAT prerequisite is missing: $SECRETS_DIR/$secret" >&2
    exit 2
  fi
done

image_tag="$(bash "$ROOT/scripts/deployment-image-tag.sh")"
export QUICSTUDIO_IMAGE_TAG="$image_tag"
printf 'Deploying image quicstudio-api:%s\n' "$image_tag"

compose=(docker compose --project-name "$PROJECT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
services=(api realtime-dispatcher worker-control worker-ingest worker-media worker-publish worker-export worker-analytics worker-governance celery-beat)
uat_ai_worker_enabled="${UAT_AI_WORKER_ENABLED:-}"
if [[ -z "$uat_ai_worker_enabled" ]]; then
  uat_ai_worker_enabled="$(
    awk -F= '$1 == "UAT_AI_WORKER_ENABLED" { value=$0; sub(/^[^=]*=/, "", value); print value }' "$ENV_FILE" \
      | tail -n 1 \
      | tr -d '\r"' \
      | xargs
  )"
fi
uat_ai_worker_enabled="${uat_ai_worker_enabled:-false}"
if [[ "$uat_ai_worker_enabled" != "true" && "$uat_ai_worker_enabled" != "false" ]]; then
  echo "unsupported UAT_AI_WORKER_ENABLED value: $uat_ai_worker_enabled" >&2
  exit 2
fi
if [[ "$uat_ai_worker_enabled" == "true" ]]; then
  services+=(worker-ai)
fi

"${compose[@]}" build migrate
"${compose[@]}" up -d --wait redis-state
# CORS is managed by the configured storage provider (STORAGE_ENDPOINT and
# STORAGE_BROWSER_ENDPOINT); the retired oss2-only configure helper is not run
# during deployment because it cannot configure S3-compatible providers.

"${compose[@]}" up migrate
"${compose[@]}" up -d --build --remove-orphans "${services[@]}"

for _ in $(seq 1 30); do
  if curl --fail --silent --show-error "$HEALTH_URL" >/dev/null; then
    "${compose[@]}" ps
    exit 0
  fi
  sleep 2
done

echo "UAT API did not become healthy at $HEALTH_URL" >&2
"${compose[@]}" ps >&2 || true
exit 1
