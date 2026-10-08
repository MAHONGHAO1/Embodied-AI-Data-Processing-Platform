from __future__ import annotations

import os

from quictrain_core import ComputeProvider
from quictrain_provider_aliyun_dlc import (
    AliyunDLCProvider,
    AliyunDLCSettings,
    OSSArtifactClient,
    create_sdk_client,
)
from quictrain_provider_local import FakeProvider

from .settings import get_settings

_providers: dict[str, ComputeProvider] = {}
_artifact_client: OSSArtifactClient | None = None


def get_provider(provider_id: str | None = None) -> ComputeProvider:
    """Resolve a compute provider. ``provider_id`` selects a pool; omit for deployment default."""

    settings = get_settings()
    key = provider_id or settings.provider
    cached = _providers.get(key)
    if cached is not None:
        return cached

    if key == "fake":
        provider: ComputeProvider = FakeProvider()
    elif key == "aliyun_dlc":
        dlc_settings = AliyunDLCSettings(
            region_id=_required("ALIYUN_REGION_ID"),
            workspace_id=_required("ALIYUN_DLC_WORKSPACE_ID"),
            resource_id=_required("ALIYUN_DLC_RESOURCE_ID"),
            ram_role_arn=os.environ.get("ALIYUN_RAM_ROLE_ARN"),
            ecs_spec=os.environ.get("ALIYUN_DLC_ECS_SPEC"),
            gpu_type=os.environ.get("ALIYUN_DLC_GPU_TYPE") or None,
            job_max_running_time_minutes=int(
                os.environ.get("ALIYUN_DLC_JOB_MAX_RUNNING_TIME_MINUTES", "60")
            ),
            priority=int(os.environ.get("ALIYUN_DLC_PRIORITY", "1")),
            accessibility=os.environ.get("ALIYUN_DLC_ACCESSIBILITY", "PUBLIC"),
            endpoint=os.environ.get("ALIYUN_DLC_ENDPOINT"),
        )
        provider = AliyunDLCProvider(dlc_settings, create_sdk_client(dlc_settings))
    else:
        raise RuntimeError(f"Unsupported provider: {key}")
    _providers[key] = provider
    return provider


def build_provider_registry() -> tuple[ComputeProvider, dict[str, ComputeProvider]]:
    """Default provider plus optional pools (fake + aliyun_dlc when configured).

    Embedded Studio UAT can keep ``QUICTRAIN_PROVIDER=fake`` for local-sim while
    still routing ``*-4090-*`` / ``*-h20-*`` profiles to DLC when credentials exist.
    """

    import logging

    logger = logging.getLogger(__name__)
    providers: dict[str, ComputeProvider] = {}
    try:
        providers["fake"] = get_provider("fake")
    except Exception as exc:  # pragma: no cover - fake always constructible
        logger.warning("fake provider unavailable: %s", exc)
    try:
        providers["aliyun_dlc"] = get_provider("aliyun_dlc")
    except Exception as exc:
        logger.info("aliyun_dlc provider not registered: %s", exc)
    default = get_provider()
    providers.setdefault(
        getattr(default, "provider_id", None)
        or ("fake" if default.__class__.__name__ == "FakeProvider" else "aliyun_dlc"),
        default,
    )
    return default, providers


def set_provider(provider: ComputeProvider, provider_id: str | None = None) -> None:
    """Inject a provider for tests / embedded scheduler. Defaults to settings.provider id."""

    settings = get_settings()
    key = provider_id or settings.provider
    _providers[key] = provider


def clear_providers() -> None:
    _providers.clear()


def list_registered_providers() -> dict[str, ComputeProvider]:
    return dict(_providers)


def get_artifact_client() -> OSSArtifactClient | None:
    global _artifact_client
    settings = get_settings()
    if not settings.artifact_root.startswith("oss://"):
        return None
    if _artifact_client is None:
        _artifact_client = OSSArtifactClient(
            region=os.environ.get("ALIYUN_REGION_ID", "cn-beijing"),
            endpoint=os.environ.get("ALIYUN_OSS_ENDPOINT"),
        )
    return _artifact_client


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required Aliyun setting: {name}")
    return value
