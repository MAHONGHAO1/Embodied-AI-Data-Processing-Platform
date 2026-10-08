#!/usr/bin/env bash
# Restore drill onto an independent database URL (Workstream D).
# Supports --check (tooling dry-run).
set -euo pipefail

if [[ "${1:-}" == "--check" ]]; then
  missing=0
  for tool in pg_restore gzip; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
      echo "missing tooling: ${tool}"
      missing=1
    fi
  done
  if [[ "${missing}" -ne 0 ]]; then
    echo "postgres_restore_drill.sh --check: install PostgreSQL client tools to enable restore drills"
    exit 1
  fi
  echo "postgres_restore_drill.sh tooling ok (pg_restore/gzip present)"
  exit 0
fi

archive="${1:?usage: postgres_restore_drill.sh <dump.gz>}"
: "${QUICTRAIN_RESTORE_DATABASE_URL:?set QUICTRAIN_RESTORE_DATABASE_URL to the independent instance}"

tmp="$(mktemp)"
gzip -dc "${archive}" > "${tmp}"
pg_restore --clean --if-exists --no-owner --no-acl -d "${QUICTRAIN_RESTORE_DATABASE_URL}" "${tmp}"
rm -f "${tmp}"

echo "Restore finished. Next:"
echo "  1) QUICTRAIN_DATABASE_URL=\$QUICTRAIN_RESTORE_DATABASE_URL alembic current"
echo "  2) Sample Job/Attempt/Artifact rows from acceptance evidence"
echo "  3) Start API+Scheduler against the restored URL and confirm claim health"
echo "  4) Fill docs/evidence/backup/RESTORE_DRILL.md"
