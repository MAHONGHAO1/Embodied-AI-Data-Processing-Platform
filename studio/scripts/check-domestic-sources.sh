#!/usr/bin/env bash
# Reject source and runtime dependency references that would fall back overseas.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORBIDDEN='pypi\.org|registry-1\.docker\.io|docker\.io/|deb\.debian\.org|archive\.ubuntu\.com|registry\.npmjs\.org|cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com'

if [[ "$#" -gt 0 ]]; then
  paths=("$@")
else
  paths=(
    "$ROOT/deploy/Dockerfile"
    "$ROOT/deploy/docker-compose.yml"
    "$ROOT/deploy/docker-compose.prod.yml"
    "$ROOT/deploy/.env.example"
    "$ROOT/Makefile"
    "$ROOT/scripts/setup.sh"
    "$ROOT/scripts/setup.ps1"
    "$ROOT/scripts/vendor-frontend.sh"
    "$ROOT/frontend/index.html"
    "$ROOT/frontend/vendor/manifest.json"
    "$ROOT/backend/pyproject.toml"
  )
fi

found=0
for path in "${paths[@]}"; do
  if [[ ! -f "$path" ]]; then
    echo "supply-chain check input is missing: $path" >&2
    exit 2
  fi
  if rg -n -i -- "$FORBIDDEN" "$path" >&2; then
    echo "foreign dependency source blocked: $path" >&2
    found=1
  fi
done

if [[ "$found" -ne 0 ]]; then
  exit 2
fi

echo "domestic dependency source check passed (${#paths[@]} files)"
