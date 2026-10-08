#!/usr/bin/env bash
# Create one atomic PostgreSQL logical backup on an explicitly selected host.

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: backup-deployment.sh --ssh-target HOST --deploy-dir ABSOLUTE_PATH [options]

Options:
  --compose-project NAME  Docker Compose project name (default: quicstudio)
  --retention-days DAYS   Delete matching backups older than DAYS (default: 14)
  --dry-run               Validate and print the credential-free operation
EOF
}

ssh_target=""
deploy_dir=""
compose_project="quicstudio"
retention_days="14"
dry_run="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh-target)
      [[ $# -ge 2 ]] || { echo "--ssh-target requires a value" >&2; exit 2; }
      ssh_target="$2"
      shift 2
      ;;
    --deploy-dir)
      [[ $# -ge 2 ]] || { echo "--deploy-dir requires a value" >&2; exit 2; }
      deploy_dir="$2"
      shift 2
      ;;
    --compose-project)
      [[ $# -ge 2 ]] || { echo "--compose-project requires a value" >&2; exit 2; }
      compose_project="$2"
      shift 2
      ;;
    --retention-days)
      [[ $# -ge 2 ]] || { echo "--retention-days requires a value" >&2; exit 2; }
      retention_days="$2"
      shift 2
      ;;
    --dry-run)
      dry_run="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ "$ssh_target" =~ ^[A-Za-z0-9_.@-]+$ ]] || {
  echo "--ssh-target must be an SSH config alias or user@host" >&2
  exit 2
}
[[ "$deploy_dir" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
  echo "--deploy-dir must be an absolute path without whitespace" >&2
  exit 2
}
if [[ "/$deploy_dir/" == *"/../"* || "/$deploy_dir/" == *"/./"* || "$deploy_dir" == "/" ]]; then
  echo "--deploy-dir must identify one deployment directory without dot segments" >&2
  exit 2
fi
[[ "$compose_project" =~ ^[A-Za-z0-9_-]+$ ]] || {
  echo "--compose-project contains unsupported characters" >&2
  exit 2
}
[[ "$retention_days" =~ ^[0-9]+$ ]] || {
  echo "--retention-days must be an integer" >&2
  exit 2
}
if (( retention_days < 1 || retention_days > 3650 )); then
  echo "--retention-days must be between 1 and 3650" >&2
  exit 2
fi

if [[ "$dry_run" == "true" ]]; then
  printf 'ssh %s: cd %s; docker compose project=%s pg_dump custom-format; retain %s days\n' \
    "$ssh_target" "$deploy_dir" "$compose_project" "$retention_days"
  exit 0
fi

ssh -- "$ssh_target" bash -s -- "$deploy_dir" "$compose_project" "$retention_days" <<'REMOTE_BACKUP'
set -euo pipefail

deploy_dir="$1"
compose_project="$2"
retention_days="$3"
cd "$deploy_dir"

env_file="deploy/.env"
compose_file="deploy/docker-compose.prod.yml"
backup_dir="deploy/runtime/backups"
[[ -f "$env_file" && -f "$compose_file" ]] || {
  echo "deployment compose inputs are unavailable" >&2
  exit 2
}
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
target="$backup_dir/quicstudio-.dump"
temporary="$target.tmp"
compose=(docker compose --project-name "$compose_project" --env-file "$env_file" -f "$compose_file")

cleanup() {
  rm -f -- "$temporary"
}
trap cleanup EXIT

"${compose[@]}" exec -T --interactive=false postgres pg_dump -U quicdata -d quicdata -Fc > "$temporary"
[[ -s "$temporary" ]] || { echo "database backup is empty" >&2; exit 1; }
chmod 600 "$temporary"
mv -- "$temporary" "$target"
sha256sum "$target" > "$target.sha256"
chmod 600 "$target.sha256"

find "$backup_dir" -maxdepth 1 -type f -name 'quicstudio-*.dump' -mtime "+$retention_days" -delete
find "$backup_dir" -maxdepth 1 -type f -name 'quicdata-*.dump.sha256' -mtime "+$retention_days" -delete
printf 'backup_created file=%s checksum=%s\n' "$(basename "$target")" "$(basename "$target.sha256")"
REMOTE_BACKUP
