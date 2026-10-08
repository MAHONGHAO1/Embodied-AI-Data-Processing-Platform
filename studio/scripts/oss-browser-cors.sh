#!/usr/bin/env bash
# Check or explicitly apply the exact raw-Bucket CORS rule for browser uploads.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: bash scripts/oss-browser-cors.sh --env-file PATH --compose-file PATH [options]

Options:
  --project-name NAME  Docker Compose project name.
  --apply              Append missing rules. Default is read-only check.
  --allow-http         Permit explicit HTTP Origins for isolated UAT only.
EOF
}

env_file=""
compose_file=""
project_name=""
apply=false
allow_http=false

while (($#)); do
  case "$1" in
    --env-file)
      env_file="${2:-}"
      shift 2
      ;;
    --compose-file)
      compose_file="${2:-}"
      shift 2
      ;;
    --project-name)
      project_name="${2:-}"
      shift 2
      ;;
    --apply)
      apply=true
      shift
      ;;
    --allow-http)
      allow_http=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -f "$env_file" ]] || { echo "missing environment file: $env_file" >&2; exit 2; }
[[ -f "$compose_file" ]] || { echo "missing compose file: $compose_file" >&2; exit 2; }

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

read_env_value() {
  awk -F= -v key="$1" '
    $1 == key {
      value = $0
      sub(/^[^=]*=/, "", value)
      result = value
    }
    END { print result }
  ' "$env_file"
}

unquote_env_value() {
  local value
  value="$(trim "$1")"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  printf '%s' "$value"
}

direct_enabled="$(unquote_env_value "$(read_env_value OSS_BROWSER_DIRECT_ENABLED)")"
case "${direct_enabled,,}" in
  ""|false|0|no|off)
    echo "OSS browser-direct upload is disabled; skip raw Bucket CORS preflight."
    exit 0
    ;;
  true|1|yes|on)
    ;;
  *)
    echo "unsupported OSS_BROWSER_DIRECT_ENABLED value: $direct_enabled" >&2
    exit 2
    ;;
esac

configured_origins="$(unquote_env_value "$(read_env_value CORS_ORIGINS)")"
[[ -n "$configured_origins" ]] || {
  echo "CORS_ORIGINS is required when OSS browser-direct upload is enabled" >&2
  exit 2
}

IFS=',' read -r -a raw_origins <<< "$configured_origins"
cors_args=()
for raw_origin in "${raw_origins[@]}"; do
  origin="$(trim "$raw_origin")"
  [[ -n "$origin" ]] || {
    echo "CORS_ORIGINS contains an empty origin" >&2
    exit 2
  }
  cors_args+=(--origin "$origin")
done

if [[ "$apply" == true ]]; then
  cors_args+=(--apply)
fi
if [[ "$allow_http" == true ]]; then
  cors_args+=(--allow-http)
fi

# Keep direct invocations traceable to the same verified revision as make and
# uat-up.sh, rather than inheriting a stale QUICSTUDIO_IMAGE_TAG from deploy/.env.
image_tag="$(bash "$SCRIPT_DIR/deployment-image-tag.sh")"
export QUICSTUDIO_IMAGE_TAG="$image_tag"

compose=(docker compose)
if [[ -n "$project_name" ]]; then
  compose+=(--project-name "$project_name")
fi
compose+=(--env-file "$env_file" -f "$compose_file")

"${compose[@]}" run --rm --no-deps api python -m scripts.configure_oss_browser_cors "${cors_args[@]}"
