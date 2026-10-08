ARG DOCKER_REGISTRY_PREFIX=docker.m.daocloud.io/library
FROM ${DOCKER_REGISTRY_PREFIX}/python:3.11-slim

ARG APT_MIRROR_URL
ARG APT_SECURITY_MIRROR_URL
ARG PYPI_INDEX_URL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/backend

WORKDIR /app

COPY backend/pyproject.toml /tmp/quicstudio-backend/pyproject.toml
COPY backend/data /tmp/quicstudio-backend/data
COPY backend/scripts /tmp/quicstudio-backend/scripts
RUN test -n "${APT_MIRROR_URL}" \
    && test -n "${APT_SECURITY_MIRROR_URL}" \
    && test -n "${PYPI_INDEX_URL}" \
    && if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
         sed -i -E \
           -e "s|^URIs: .*/debian-security/?$|URIs: ${APT_SECURITY_MIRROR_URL}|" \
           -e "s|^URIs: .*/debian/?$|URIs: ${APT_MIRROR_URL}|" \
           /etc/apt/sources.list.d/debian.sources; \
       fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" /tmp/quicstudio-backend

COPY backend /app/backend
COPY frontend /app/frontend
COPY alembic.ini /app/alembic.ini
COPY deploy/run-with-secrets.sh /app/deploy/run-with-secrets.sh
COPY deploy/external-deps/checksums.sha256 /app/deploy/external-deps/checksums.sha256
RUN chmod 0555 /app/deploy/run-with-secrets.sh

WORKDIR /app/backend
