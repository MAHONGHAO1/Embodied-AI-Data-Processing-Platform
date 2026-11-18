# syntax=docker/dockerfile:1
# Linux container for the local single-user workbench. No cloud or multi-user claim.
FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    ROBODATA_WEB_HOST=0.0.0.0 \
    ROBODATA_WEB_PORT=8765 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY robodata ./robodata
COPY robodata_business ./robodata_business
COPY fixtures ./fixtures
RUN uv sync --locked --no-dev
COPY --from=frontend /build/dist ./frontend/dist
RUN mkdir -p work data reports \
    && useradd --create-home --uid 10001 robodata \
    && chown -R robodata:robodata /app
USER robodata
EXPOSE 8765
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=2)"]
# Publish with: docker run --rm -p 127.0.0.1:8765:8765 robodata-workbench
CMD ["/app/.venv/bin/python", "-m", "robodata.web"]
