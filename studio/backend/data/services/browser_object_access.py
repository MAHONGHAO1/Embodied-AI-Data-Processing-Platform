"""Issue short-lived browser OSS reads after a router has authorized an object.

Server-side OSS I/O deliberately stays in ``data.infra.oss_client``'s normal
client, which uses the deployment's internal endpoint. This module only asks
that client to sign a public URL after it has resolved a server-owned object.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data.infra import oss_client
from data.services.cloud_storage import verified_publish_snapshot
from data.utils.storage_paths import cloud_path_is_safe, is_under_storage_root, storage_root_path
from data.utils.storage_uri import is_cloud_uri, parse_storage_uri

logger = logging.getLogger(__name__)

_CACHE_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_RESOURCE_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_DIRECT_PREVIEW_MEDIA_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}


@dataclass(frozen=True)
class BrowserDirectAccess:
    """Ephemeral object URL returned only to an already-authorized caller."""

    url: str
    expires_at: str
    media_type: str

    def as_payload(self) -> dict[str, object]:
        return {
            "direct": True,
            "url": self.url,
            "expires_at": self.expires_at,
            "media_type": self.media_type,
        }


def unavailable_payload(media_type: str) -> dict[str, object]:
    """A successful descriptor response that tells the frontend to use API fallback."""
    return {
        "direct": False,
        "url": "",
        "expires_at": "",
        "media_type": media_type,
    }


def _expires_at() -> str:
    ttl = oss_client.browser_direct_ttl_seconds()
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()


def _access_from_signed_url(url: str, media_type: str) -> BrowserDirectAccess | None:
    if not url:
        return None
    return BrowserDirectAccess(url=url, expires_at=_expires_at(), media_type=media_type)


def _is_exact_safe_cloud_object(uri: str) -> tuple[str, str] | None:
    if not is_cloud_uri(uri):
        return None
    try:
        bucket, key = parse_storage_uri(uri)
    except ValueError:
        return None
    if not key or key.endswith("/") or not cloud_path_is_safe(bucket, key):
        return None
    return bucket, key


def issue_browser_download_url(
    storage_uri: str | None,
    *,
    download_name: str = "",
    media_type: str = "application/octet-stream",
) -> BrowserDirectAccess | None:
    """Sign one existing cloud object, otherwise leave the legacy API as fallback."""
    if not oss_client.browser_direct_enabled():
        return None
    target = _is_exact_safe_cloud_object(str(storage_uri or ""))
    if target is None:
        return None
    bucket, key = target
    if not oss_client.browser_direct_object_allowed(bucket, key):
        return None
    try:
        if not oss_client.object_exists(bucket, key):
            return None
        signed_url = oss_client.sign_browser_get_url(
            bucket,
            key,
            download_name=download_name,
        )
    except Exception as exc:
        # Direct read is an optimization. Preserve the authorized API fallback,
        # but retain an observable failure without logging the signed URL/key.
        logger.warning(
            "browser direct download issuance unavailable error_type=%s",
            type(exc).__name__,
        )
        return None
    return _access_from_signed_url(signed_url, media_type)


def _validate_preview_source(
    path: Path,
    *,
    resource_type: str,
    resource_id: int | str,
    media_type: str,
) -> tuple[Path, str] | None:
    if not _CACHE_SCOPE_RE.fullmatch(resource_type):
        raise ValueError("browser preview resource type is invalid")
    if not _RESOURCE_ID_RE.fullmatch(str(resource_id)):
        raise ValueError("browser preview resource id is invalid")
    source = Path(path)
    if not is_under_storage_root(source, root=storage_root_path()):
        raise ValueError("browser preview source is outside storage_root")
    suffix = source.suffix.lower()
    expected_media_type = _DIRECT_PREVIEW_MEDIA_BY_SUFFIX.get(suffix)
    actual_media_type = media_type.lower().split(";", 1)[0].strip()
    if expected_media_type != actual_media_type:
        return None
    return source, suffix


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def issue_browser_preview_url(
    preview_path: Path | str,
    *,
    resource_type: str,
    resource_id: int | str,
    media_type: str,
) -> BrowserDirectAccess | None:
    """Publish one verified local preview artifact to process OSS and sign it.

    The key contains only a fixed scope, a stable numeric resource ID, and a
    content digest plus a fixed supported suffix. It never contains an external
    path, filename, or signed URL.
    """
    if not oss_client.browser_direct_enabled():
        return None
    validated = _validate_preview_source(
        Path(preview_path),
        resource_type=resource_type,
        resource_id=resource_id,
        media_type=media_type,
    )
    if validated is None:
        return None
    source, suffix = validated
    bucket = oss_client.bucket_name("process")
    try:
        with verified_publish_snapshot(source) as snapshot:
            digest = _sha256_file(snapshot)
            key = f"browser-previews/v1/{resource_type}/{resource_id}/{digest}{suffix}"
            if not oss_client.browser_direct_object_allowed(bucket, key):
                return None
            if not oss_client.object_exists(bucket, key):
                oss_client.upload_file(snapshot, bucket, key, content_type=media_type)
            signed_url = oss_client.sign_browser_get_url(bucket, key)
    except Exception as exc:
        logger.warning(
            "browser direct preview issuance unavailable resource_type=%s error_type=%s",
            resource_type,
            type(exc).__name__,
        )
        return None
    return _access_from_signed_url(signed_url, media_type)
