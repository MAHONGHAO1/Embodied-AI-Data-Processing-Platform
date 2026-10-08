#!/usr/bin/env bash

# QuicData start (Linux/macOS) — same as start.sh, LAN-friendly hints



ROOT="$(cd "$(dirname "$0")/.." && pwd)"

exec bash "$ROOT/scripts/start.sh"

