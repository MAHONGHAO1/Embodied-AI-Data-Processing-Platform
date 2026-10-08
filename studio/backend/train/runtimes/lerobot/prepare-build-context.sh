#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="${RUNTIME_DIR}/upstream.lock"
BUILD_DIR="${RUNTIME_DIR}/.build"
ARCHIVE="${BUILD_DIR}/lerobot.tar.gz"

LEROBOT_REF="$(awk -F= '$1 == "ref" { print $2 }' "${LOCK_FILE}")"
EXPECTED_SHA256="$(awk -F= '$1 == "source_sha256" { print $2 }' "${LOCK_FILE}")"

if [[ -f "${ARCHIVE}" ]]; then
  if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL_SHA256="$(sha256sum "${ARCHIVE}" | awk '{ print $1 }')"
  else
    ACTUAL_SHA256="$(shasum -a 256 "${ARCHIVE}" | awk '{ print $1 }')"
  fi
  if [[ "${ACTUAL_SHA256}" == "${EXPECTED_SHA256}" ]]; then
    exit 0
  fi
fi

mkdir -p "${BUILD_DIR}"
TEMP_ARCHIVE="$(mktemp "${BUILD_DIR}/lerobot.XXXXXX.tar.gz")"
trap 'rm -f "${TEMP_ARCHIVE}"' EXIT

curl --fail --location --retry 3 \
  "https://github.com/huggingface/lerobot/archive/${LEROBOT_REF}.tar.gz" \
  --output "${TEMP_ARCHIVE}"

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL_SHA256="$(sha256sum "${TEMP_ARCHIVE}" | awk '{ print $1 }')"
else
  ACTUAL_SHA256="$(shasum -a 256 "${TEMP_ARCHIVE}" | awk '{ print $1 }')"
fi

if [[ "${ACTUAL_SHA256}" != "${EXPECTED_SHA256}" ]]; then
  echo "LeRobot archive checksum mismatch: expected ${EXPECTED_SHA256}, got ${ACTUAL_SHA256}" >&2
  exit 1
fi

mv "${TEMP_ARCHIVE}" "${ARCHIVE}"
