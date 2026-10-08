# syntax=docker/dockerfile:1.7
FROM python:3.11.11-slim-bookworm@sha256:081075da77b2b55c23c088251026fb69a7b2bf92471e491ff5fd75c192fd38e5
WORKDIR /opt/quictrain
COPY pyproject.toml README.md ./
COPY alembic.ini ./
COPY migrations migrations
COPY src src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://mirrors.aliyun.com/pypi/simple/ \
    '.[postgres,aliyun]' 'mlflow-skinny==3.3.2'
ENV QUICTRAIN_DATABASE_URL=postgresql+psycopg://quictrain@postgres:5432/quictrain
ENV QUICTRAIN_EMBEDDED_SCHEDULER=0
CMD ["python", "-m", "quictrain_scheduler.main"]
