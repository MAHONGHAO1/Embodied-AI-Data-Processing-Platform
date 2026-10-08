#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKERFILE="${DOCKERFILE:-${ROOT_DIR}/runtimes/lerobot/Dockerfile.patch}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-quic-robot-registry-vpc.cn-beijing.cr.aliyuncs.com/quicrobot/quictrain-lerobot-runtime}"
IMAGE_TAG="${IMAGE_TAG:-v0.5.1-pytorch2.10.0-cu128-r3}"
CONTAINER_ENGINE="${CONTAINER_ENGINE:-docker}"
PUSH_IMAGE="${PUSH_IMAGE:-0}"
IMAGE_REF="${IMAGE_REPOSITORY}:${IMAGE_TAG}"

if grep -q "runtimes/lerobot/.build/lerobot.tar.gz" "${DOCKERFILE}"; then
  "${ROOT_DIR}/runtimes/lerobot/prepare-build-context.sh"
fi

case "${CONTAINER_ENGINE}" in
  docker)
    docker buildx build \
      --platform linux/amd64 \
      --file "${DOCKERFILE}" \
      --tag "${IMAGE_REF}" \
      --load \
      "${ROOT_DIR}"
    if [[ "${PUSH_IMAGE}" == "1" ]]; then
      docker push "${IMAGE_REF}"
    fi
    ;;
  buildah)
    buildah bud \
      --arch amd64 \
      --isolation chroot \
      --file "${DOCKERFILE}" \
      --tag "${IMAGE_REF}" \
      "${ROOT_DIR}"
    if [[ "${PUSH_IMAGE}" == "1" ]]; then
      buildah push "${IMAGE_REF}" "docker://${IMAGE_REF}"
    fi
    ;;
  *)
    echo "Unsupported CONTAINER_ENGINE=${CONTAINER_ENGINE}; use docker or buildah." >&2
    exit 2
    ;;
esac

printf '%s\n' "${IMAGE_REF}"
