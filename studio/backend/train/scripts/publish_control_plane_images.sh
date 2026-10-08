#!/usr/bin/env bash
# Build (and optionally push) QuicTrain control-plane images for ACR.
# Do NOT use the LeRobot runtime repository for these tags.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY="${CONTROL_PLANE_REGISTRY:-quic-robot-registry-vpc.cn-beijing.cr.aliyuncs.com/quicrobot}"
TAG="${IMAGE_TAG:-$(git -C "${ROOT_DIR}" rev-parse --short HEAD)}"
PUSH_IMAGE="${PUSH_IMAGE:-0}"
PLATFORM="${PLATFORM:-linux/amd64}"

API_IMAGE="${REGISTRY}/quictrain-api:${TAG}"
SCHEDULER_IMAGE="${REGISTRY}/quictrain-scheduler:${TAG}"
WEB_IMAGE="${REGISTRY}/quictrain-web:${TAG}"
MLFLOW_IMAGE="${REGISTRY}/quictrain-mlflow:${TAG}"

build_one() {
  local dockerfile="$1"
  local image_ref="$2"
  shift 2
  docker buildx build \
    --platform "${PLATFORM}" \
    --file "${ROOT_DIR}/${dockerfile}" \
    --tag "${image_ref}" \
    --load \
    "$@" \
    "${ROOT_DIR}"
  if [[ "${PUSH_IMAGE}" == "1" ]]; then
    docker push "${image_ref}"
    docker buildx imagetools inspect "${image_ref}" | sed -n '1,20p'
  fi
  printf '%s\n' "${image_ref}"
}

build_one "apps/api/Dockerfile" "${API_IMAGE}"
build_one "services/scheduler/Dockerfile" "${SCHEDULER_IMAGE}"
build_one "apps/web/Dockerfile" "${WEB_IMAGE}" \
  --build-arg "NEXT_PUBLIC_API_BASE_URL=" \
  --build-arg "NEXT_PUBLIC_ALLOW_DEMO_MODE=false" \
  --build-arg "NEXT_PUBLIC_AUTH_TOKEN=${NEXT_PUBLIC_AUTH_TOKEN:-}" \
  --build-arg "NEXT_PUBLIC_QUIC_PROJECT=${NEXT_PUBLIC_QUIC_PROJECT:-}" \
  --build-arg "NEXT_PUBLIC_QUIC_ACTOR=${NEXT_PUBLIC_QUIC_ACTOR:-}"
build_one "infra/mlflow/Dockerfile" "${MLFLOW_IMAGE}"

cat <<EOF

Control-plane images ready (tag=${TAG}).
Pin by digest after push, then set on ECS:

  QUICTRAIN_API_IMAGE=${API_IMAGE}
  QUICTRAIN_SCHEDULER_IMAGE=${SCHEDULER_IMAGE}
  QUICTRAIN_WEB_IMAGE=${WEB_IMAGE}
  QUICTRAIN_MLFLOW_IMAGE=${MLFLOW_IMAGE}

Publish: PUSH_IMAGE=1 ${0##*/}
Runbook: docs/runbooks/control-plane-image-publish.md
EOF
