#!/usr/bin/env bash
# Local development: sync from sibling qrdf repository to vendor (bypasses Git remote)
# Usage: bash scripts/sync-qrdf.sh [local_qrdf_repo_path]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QRDF_SRC="${1:-$ROOT/../qrdf}"

export QRDF_LOCAL_PATH="$QRDF_SRC"
exec bash "$ROOT/scripts/fetch-qrdf.sh"
