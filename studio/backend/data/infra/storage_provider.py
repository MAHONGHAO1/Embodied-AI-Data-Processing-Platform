"""Runtime factory for the immutable QuicStudio storage providers."""

from __future__ import annotations

from typing import Any

from data.config import Settings, settings
from data.infra.aliyun_object_storage import AliyunObjectStorage
from data.infra.object_storage import StorageNotReady
from data.infra.s3_object_storage import S3ObjectStorage
from data.services.storage_bucket_config import StorageBucketConfig


def get_storage_provider(source: Settings | None = None) -> Any:
    cfg = source or settings
    provider = str(getattr(cfg, "storage_provider", "minio") or "").strip().lower()
    if provider in {"minio", "s3", "s3-compatible"}:
        endpoint = str(getattr(cfg, "storage_endpoint", "") or "").strip()
        access = str(getattr(cfg, "storage_access_key_id", "") or "").strip()
        secret = str(getattr(cfg, "storage_secret_access_key", "") or "").strip()
        if not endpoint or not access or not secret:
            raise StorageNotReady("MinIO storage requires endpoint and credentials")
        buckets = {
            "raw": cfg.oss_bucket_raw,
            "process": cfg.oss_bucket_process,
            "export": cfg.oss_bucket_export,
        }
        environment = "prod" if cfg.is_production else ("test" if cfg.test_mode else "dev")
        return S3ObjectStorage(
            StorageBucketConfig.from_overrides(environment, buckets),
            endpoint_url=endpoint,
            access_key_id=access,
            secret_access_key=secret,
            region_name=getattr(cfg, "oss_region", "us-east-1"),
            presign_expires=int(getattr(cfg, "oss_browser_url_ttl_seconds", 900)),
            browser_endpoint_url=getattr(cfg, "storage_browser_endpoint", "")
            or getattr(cfg, "oss_browser_endpoint", "")
            or None,
        )
    if provider in {"aliyun", "oss", "aliyun-oss"}:
        endpoint = str(getattr(cfg, "storage_endpoint", "") or "").strip()
        access = str(getattr(cfg, "storage_access_key_id", "") or "").strip()
        secret = str(getattr(cfg, "storage_secret_access_key", "") or "").strip()
        if not endpoint or not access or not secret:
            raise StorageNotReady("Aliyun OSS storage requires endpoint and credentials")
        buckets = {
            "raw": cfg.oss_bucket_raw,
            "process": cfg.oss_bucket_process,
            "export": cfg.oss_bucket_export,
        }
        environment = "prod" if cfg.is_production else ("test" if cfg.test_mode else "dev")
        return AliyunObjectStorage(
            StorageBucketConfig.from_overrides(environment, buckets),
            endpoint_url=endpoint,
            access_key_id=access,
            access_key_secret=secret,
            region_name=getattr(cfg, "oss_region", "cn-beijing"),
            presign_expires=int(getattr(cfg, "oss_browser_url_ttl_seconds", 900)),
            browser_endpoint_url=getattr(cfg, "storage_browser_endpoint", "")
            or getattr(cfg, "oss_browser_endpoint", "")
            or None,
        )
    raise StorageNotReady(f"unsupported storage provider: {provider}")
