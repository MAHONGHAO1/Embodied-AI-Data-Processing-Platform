# syntax=docker/dockerfile:1.7
ARG PYTORCH_IMAGE=docker.io/pytorch/pytorch:2.10.0-cuda12.8-cudnn9-runtime@sha256:b85566342b86d13a67712e9315d40cdc2dad7f8d86df1aff3831f80835edbcca
FROM ${PYTORCH_IMAGE}

ARG LEROBOT_REF=1396b9fab7aecddd10006c33c47a487ffdcb54b4
ARG LEROBOT_SOURCE_SHA256=fe0b3079a517f0c60bdf325afc7832d81ae384bd1d022f4e802e7da8f73606dd
ARG LEROBOT_VERSION=v0.5.1
ARG QUICTRAIN_RUNTIME_VERSION=0.2.0
LABEL org.opencontainers.image.title="QuicTrain LeRobot runtime"
LABEL org.opencontainers.image.description="GPU training runtime for QuicTrain ACT and pi05 models"
LABEL org.opencontainers.image.source="https://github.com/quicrobot/QuicTrain"
LABEL org.opencontainers.image.version="${LEROBOT_VERSION}-quictrain.${QUICTRAIN_RUNTIME_VERSION}"
LABEL org.opencontainers.image.vendor="QuicRobot"
LABEL ai.quictrain.lerobot-ref="${LEROBOT_REF}"
LABEL ai.quictrain.lerobot-version="${LEROBOT_VERSION}"
LABEL ai.quictrain.runtime-version="${QUICTRAIN_RUNTIME_VERSION}"

WORKDIR /opt/quictrain
COPY src/quictrain_runner /opt/quictrain/src/quictrain_runner
COPY runtimes/lerobot /opt/quictrain/runtimes/lerobot

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        git \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY runtimes/lerobot/.build/lerobot.tar.gz /tmp/lerobot.tar.gz
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN echo "${LEROBOT_SOURCE_SHA256}  /tmp/lerobot.tar.gz" | sha256sum -c - \
    && mkdir /opt/lerobot \
    && tar -xzf /tmp/lerobot.tar.gz --strip-components=1 -C /opt/lerobot \
    && pip install --break-system-packages --no-cache-dir --index-url "${PIP_INDEX_URL}" \
        "/opt/lerobot[pi]" "pydantic==2.11.7" "click==8.3.1" \
    && pip check \
    && python -c "import torch; assert torch.__version__.startswith('2.10.'); assert torch.version.cuda == '12.8'" \
    && python -c "from lerobot.policies.act.configuration_act import ACTConfig; from lerobot.policies.pi05.configuration_pi05 import PI05Config" \
    && pip freeze --all > /opt/quictrain/runtime-requirements.txt \
    && rm /tmp/lerobot.tar.gz

ENV PYTHONPATH="/opt/quictrain/src:/opt/quictrain/runtimes/lerobot" \
    QUICTRAIN_EXECUTE_TRAINING=1 \
    QUICTRAIN_WORK_ROOT=/tmp/quictrain \
    HF_HOME=/cache/huggingface \
    TORCH_HOME=/cache/torch \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "quictrain_runner"]
