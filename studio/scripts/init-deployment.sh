#!/usr/bin/env bash
# Initialize one isolated deployment's runtime directories and Docker secrets.
# This script is intentionally idempotent and never overwrites an existing secret.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/init-deployment.sh \
  --env-file PATH --runtime-dir PATH --secrets-dir PATH --label NAME
EOF
}

env_file=""
runtime_dir=""
secrets_dir=""
label="deployment"

while (($#)); do
  case "$1" in
    --env-file)
      env_file="${2:-}"
      shift 2
      ;;
    --runtime-dir)
      runtime_dir="${2:-}"
      shift 2
      ;;
    --secrets-dir)
      secrets_dir="${2:-}"
      shift 2
      ;;
    --label)
      label="${2:-}"
      shift 2
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

[[ -f "$env_file" ]] || {
  echo "Missing $label environment file: $env_file" >&2
  exit 2
}
[[ -n "$runtime_dir" && -n "$secrets_dir" ]] || {
  echo "runtime and secrets directories are required" >&2
  exit 2
}
command -v openssl >/dev/null || {
  echo "openssl is required to generate deployment secrets" >&2
  exit 2
}

mkdir -p \
  "$runtime_dir/postgres" \
  "$runtime_dir/redis-state" \
  "$runtime_dir/redis-broker" \
  "$runtime_dir/scratch" \
  "$runtime_dir/backups" \
  "$secrets_dir"
chmod 700 "$secrets_dir"

for secret in app_secret_key postgres_password redis_password; do
  if [[ ! -s "$secrets_dir/$secret" ]]; then
    umask 077
    openssl rand -hex 32 > "$secrets_dir/$secret"
    echo "Created secret file $secrets_dir/$secret"
  fi
done

# Storage credentials are created as placeholders so the deployment layout is
# complete; run-with-secrets.sh will fail closed until both files are populated.
for secret in storage_access_key_id storage_secret_access_key; do
  if [[ ! -e "$secrets_dir/$secret" ]]; then
    umask 077
    : > "$secrets_dir/$secret"
    echo "Created empty OSS secret file $secrets_dir/$secret"
  fi
done

if [[ ! -f "$secrets_dir/admin_bootstrap_consumed" && ! -s "$secrets_dir/admin_bootstrap_password" ]]; then
  umask 077
  openssl rand -hex 32 > "$secrets_dir/admin_bootstrap_password"
  echo "Created one-time administrator password file at $secrets_dir/admin_bootstrap_password"
fi

chmod 600 \
  "$secrets_dir/app_secret_key" \
  "$secrets_dir/postgres_password" \
  "$secrets_dir/redis_password" \
  "$secrets_dir/storage_access_key_id" \
  "$secrets_dir/storage_secret_access_key"
if [[ -f "$secrets_dir/admin_bootstrap_password" ]]; then
  chmod 600 "$secrets_dir/admin_bootstrap_password"
fi

echo "$label runtime directories prepared under $runtime_dir."
