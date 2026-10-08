"""Storage provider selection: MinIO or Aliyun OSS.

QuicStudio stores every object in the configured S3-compatible provider.
There is no local disk, mirror or hybrid fallback: when the provider is not
configured the runtime fails closed with ``StorageNotReady``.
"""

from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger("quicdata.storage_mode")

StorageMode = Literal["minio", "aliyun_oss"]
DEFAULT_STORAGE_MODE: StorageMode = "minio"
VALID_STORAGE_MODES = frozenset({"minio", "aliyun_oss"})
_MODE_ALIASES = {
    "minio": "minio",
    "aliyun": "aliyun_oss",
    "aliyun-oss": "aliyun_oss",
    "aliyun_oss": "aliyun_oss",
    "oss": "aliyun_oss",
    "s3": "aliyun_oss",
    "s3-compatible": "aliyun_oss",
    "s3_compatible": "aliyun_oss",
}


def _runtime_config() -> dict:
    # config imports this module while initializing persisted storage settings.
    # Keep the dependency lazy so importing storage_mode first is also valid.
    from data.config import get_runtime_config

    return get_runtime_config()


def _settings():
    from data.config import settings

    return settings


def normalize_storage_mode(raw: str | None) -> StorageMode:
    """Return the supported provider for ``raw``; reject legacy disk modes."""

    value = str(raw or DEFAULT_STORAGE_MODE).strip().lower()
    mode = _MODE_ALIASES.get(value)
    if mode is None:
        raise ValueError(f"不支持的 storage_mode: {raw!r}（允许: minio|aliyun_oss）")
    return mode  # type: ignore[return-value]


def get_configured_storage_mode() -> StorageMode:
    """Read the configured provider (runtime config first, then settings)."""

    storage = _runtime_config().get("storage") or {}
    raw = (
        storage.get("storage_mode")
        or getattr(_settings(), "storage_provider", None)
        or DEFAULT_STORAGE_MODE
    )
    return normalize_storage_mode(str(raw))


def oss_reachable() -> bool:
    """Whether the new provider is configured; it is never a disk fallback."""

    cfg = _settings()
    return all(
        bool(str(getattr(cfg, name, "") or "").strip())
        for name in ("storage_endpoint", "storage_access_key_id", "storage_secret_access_key")
    )


def get_effective_storage_mode() -> StorageMode:
    """Effective provider: configuration validated against reachability."""

    if not oss_reachable():
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady(
            "object storage provider is not configured; disk fallback is disabled"
        )
    return get_configured_storage_mode()


def uses_cloud_uri_authority(mode: StorageMode | None = None) -> bool:
    """Whether task canonical path is written as oss://."""

    get_effective_storage_mode()
    return True


def keep_local_object_mirror(mode: StorageMode | None = None) -> bool:
    """Long-term local mirrors are no longer supported."""

    get_effective_storage_mode()
    return False


def can_purge_cloud_mirrors() -> bool:
    """Whether cleaning a legacy storage/cloud/ mirror is allowed."""

    return oss_reachable()


def allow_oss_import(mode: StorageMode | None = None) -> bool:
    """Whether oss:// external addresses may be used as data input."""

    m = mode or get_effective_storage_mode()
    return m in VALID_STORAGE_MODES


def prefer_temp_materialize(mode: StorageMode | None = None) -> bool:
    """Materialization is always temporary because no local mirror exists."""

    get_effective_storage_mode()
    return True


def apply_storage_mode_derived_fields(storage: dict) -> dict:
    """Derive type / uri_scheme / cloud_enabled from the configured provider."""

    provider = normalize_storage_mode(getattr(_settings(), "storage_provider", None))
    out = dict(storage)
    out.update(
        {
            "storage_mode": provider,
            "provider": provider,
            "type": "oss",
            "uri_scheme": "oss",
            "cloud_enabled": True,
            "keep_local_cache": False,
        }
    )
    return out


def validate_storage_mode_payload(data: dict) -> dict:
    """Validate a settings payload that still carries ``storage_mode``."""

    if "storage_mode" in data:
        raw = str(data.get("storage_mode") or "").strip()
        if not raw:
            raise ValueError("storage_mode 不能为空（允许: minio|aliyun_oss）")
        data = {**data, "storage_mode": normalize_storage_mode(raw)}
    return data


def storage_mode_public_view() -> dict:
    from data.config import settings

    storage = _runtime_config().get("storage") or {}
    buckets = storage.get("buckets") or {}
    return {
        "mode": str(
            storage.get("storage_mode") or settings.storage_provider or DEFAULT_STORAGE_MODE
        ),
        "provider": str(getattr(settings, "storage_provider", "") or ""),
        "endpoint_configured": bool(getattr(settings, "storage_endpoint", "")),
        "ready": bool(oss_reachable()),
        "buckets": {
            role: str(buckets.get(role) or getattr(settings, f"oss_bucket_{role}", "") or "")
            for role in ("raw", "process", "export")
        },
    }
