#!/usr/bin/env bash
# Encrypted-at-rest friendly pg_dump helper for Workstream D.
# Supports --check (syntax/tooling dry-run without requiring a live database).
set -euo pipefail

if [[ "${1:-}" == "--check" ]]; then
  missing=0
  for tool in pg_dump gzip sha256sum; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
      echo "missing tooling: ${tool}"
      missing=1
    fi
  done
  if [[ "${missing}" -ne 0 ]]; then
    echo "postgres_backup.sh --check: install PostgreSQL client tools to enable live dumps"
    exit 1
  fi
  echo "postgres_backup.sh tooling ok (pg_dump/gzip/sha256sum present)"
  exit 0
fi

: "${QUICTRAIN_DATABASE_URL:?QUICTRAIN_DATABASE_URL is required}"
BACKUP_DIR="${QUICTRAIN_BACKUP_DIR:-./backups}"
mkdir -p "${BACKUP_DIR}"
stamp="$(date -u +%Y%m%d-%H%M%S)"
out="${BACKUP_DIR}/quictrain-pg-${stamp}.dump.gz"

echo "Writing ${out}"
pg_dump --format=custom --no-owner --no-acl "${QUICTRAIN_DATABASE_URL}" | gzip -c > "${out}"
sha256sum "${out}" | tee "${out}.sha256"

if [[ -n "${QUICTRAIN_BACKUP_OSS_URI:-}" ]]; then
  echo "Upload to ${QUICTRAIN_BACKUP_OSS_URI} using your OSS CLI/credential chain (not stored in-repo)."
fi

echo "Backup complete: ${out}"
echo "Store the dump on encrypted disk or encrypted object storage; do not commit backups."
