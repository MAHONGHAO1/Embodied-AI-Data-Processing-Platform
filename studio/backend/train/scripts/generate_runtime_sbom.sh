#!/usr/bin/env bash
# Generate SBOM + vulnerability scan for the production runtime digest (Workstream G).
set -euo pipefail

DIGEST="${QUICTRAIN_RUNTIME_DIGEST:-sha256:385f4e8b44269ebb01b71408087beb4709a09c47988859ffce2089e80b922b08}"
IMAGE="${QUICTRAIN_RUNTIME_IMAGE:-quic-robot-registry-vpc.cn-beijing.cr.aliyuncs.com/quicrobot/quictrain-lerobot-runtime@${DIGEST}}"
OUT_DIR="${1:-docs/evidence/sbom}"
mkdir -p "${OUT_DIR}"
stamp="$(date -u +%Y%m%d)"

if command -v syft >/dev/null 2>&1; then
  syft "${IMAGE}" -o cyclonedx-json > "${OUT_DIR}/runtime-r3-${stamp}.cdx.json"
elif command -v docker >/dev/null 2>&1 && docker buildx version >/dev/null 2>&1; then
  echo "syft not found; write a placeholder pointer only"
  printf '%s\n' "{\"image\":\"${IMAGE}\",\"digest\":\"${DIGEST}\",\"status\":\"pending_syft\"}" \
    > "${OUT_DIR}/runtime-r3-${stamp}.pending.json"
else
  printf '%s\n' "{\"image\":\"${IMAGE}\",\"digest\":\"${DIGEST}\",\"status\":\"pending_tooling\"}" \
    > "${OUT_DIR}/runtime-r3-${stamp}.pending.json"
fi

if command -v trivy >/dev/null 2>&1; then
  trivy image --format json --output "${OUT_DIR}/runtime-r3-${stamp}.trivy.json" "${IMAGE}" || true
fi

echo "SBOM/scan outputs under ${OUT_DIR}. Archive immutably and note high findings in AGENT_HISTORY."
