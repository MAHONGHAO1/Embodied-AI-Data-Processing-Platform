"""Object-store abstraction for dataset materialization (CPU-only).

Production OSS uses optional aliyun SDK; local/tests use filesystem or in-memory staging.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)


class ObjectStore(Protocol):
    def fetch_to_directory(self, uri: str, destination: Path) -> int:
        """Copy object/prefix into destination directory. Returns bytes copied."""


class LocalPathObjectStore:
    """Handles file:// and absolute paths."""

    def fetch_to_directory(self, uri: str, destination: Path) -> int:
        source = _path_from_uri(uri)
        if source is None or not source.exists():
            raise FileNotFoundError(uri)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
        if source.is_dir():
            shutil.copytree(source, destination)
            return sum(p.stat().st_size for p in destination.rglob("*") if p.is_file())
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination.stat().st_size


class StagingObjectStore:
    """Maps oss://bucket/key → local staging root for tests / offline rehearsal."""

    def __init__(self, staging_root: Path) -> None:
        self.staging_root = staging_root

    def fetch_to_directory(self, uri: str, destination: Path) -> int:
        if not uri.startswith("oss://"):
            raise ValueError(f"StagingObjectStore only accepts oss://, got {uri}")
        # oss://bucket/prefix → staging_root/bucket/prefix
        without = uri.removeprefix("oss://")
        mapped = self.staging_root / without
        if not mapped.exists():
            raise FileNotFoundError(
                f"OSS staging miss for {uri}; place bytes at {mapped} "
                "(production uses OssObjectStore / DLC copy)."
            )
        local = LocalPathObjectStore()
        return local.fetch_to_directory(str(mapped), destination)


class OssObjectStore:
    """Production OSS pull via alibabacloud_oss_v2 when installed; else raises ReservedError."""

    def fetch_to_directory(self, uri: str, destination: Path) -> int:
        try:
            from quictrain_provider_aliyun_dlc.oss import OSSArtifactClient, parse_oss_uri
        except ImportError as exc:
            raise RuntimeError(
                "OSS_SDK_UNAVAILABLE: install quictrain[aliyun] and configure RAM Role/STS. "
                "Reserved for cloud materialization evidence."
            ) from exc
        location = parse_oss_uri(uri if "://" in uri else f"oss://{uri}")
        client = OSSArtifactClient(region="cn-beijing", endpoint=location.endpoint)
        destination.parent.mkdir(parents=True, exist_ok=True)

        # Prefer prefix-tree materialization into the CPFS layout directory.
        try:
            keys = client.list_keys(uri if uri.endswith("/") else f"{uri.rstrip('/')}/")
        except Exception as exc:
            LOGGER.warning(
                "OSS list_keys failed for %s (%s); falling back to single object",
                uri,
                exc,
            )
            keys = []

        if keys:
            if destination.exists():
                shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
            destination.mkdir(parents=True, exist_ok=True)
            prefix = location.key.rstrip("/") + "/"
            total = 0
            for key in keys:
                relative = key[len(prefix) :] if key.startswith(prefix) else Path(key).name
                if not relative:
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                object_uri = f"oss://{location.bucket}/{key}"
                data = client.read_bytes(object_uri)
                target.write_bytes(data)
                total += len(data)
            if total == 0:
                raise FileNotFoundError(f"OSS prefix empty: {uri}")
            return total

        # Single-object fallback (exact key, no trailing slash listing).
        data = client.read_bytes(uri.rstrip("/"))
        if destination.exists():
            shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return len(data)


def _path_from_uri(uri: str) -> Path | None:
    if uri.startswith("file://"):
        return Path(urlparse(uri).path)
    if uri.startswith("/") or uri.startswith("./"):
        return Path(uri)
    return None


def resolve_object_store(*, oss_staging_root: str | None = None) -> ObjectStore:
    from quictrain_api.settings import get_settings

    settings = get_settings()
    staging = oss_staging_root or settings.oss_staging_root
    if staging:
        return StagingObjectStore(Path(staging))
    return CompositeObjectStore()


class CompositeObjectStore:
    def __init__(self) -> None:
        self._local = LocalPathObjectStore()
        self._oss = OssObjectStore()

    def fetch_to_directory(self, uri: str, destination: Path) -> int:
        if uri.startswith("oss://"):
            return self._oss.fetch_to_directory(uri, destination)
        return self._local.fetch_to_directory(uri, destination)
