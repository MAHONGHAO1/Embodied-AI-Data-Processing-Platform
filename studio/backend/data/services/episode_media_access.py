"""Authorized media access for the Batch / Episode API.

This module intentionally does not import legacy Task storage services.  The
new domain only signs one already-authorized process artifact and never accepts
an arbitrary browser-supplied bucket or key.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from data.infra import oss_client
from data.security.signed_url import create_signed_download_token
from data.utils.storage_paths import (
    cloud_path_is_safe,
    is_under_storage_root,
    resolve_storage_path,
    storage_root_path,
)
from data.utils.storage_uri import is_cloud_uri, parse_storage_uri

logger = logging.getLogger(__name__)

_PREVIEW_MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
_LOCAL_PREVIEW_TTL_SECONDS = 600
_LOCAL_PREVIEW_MAX_USES = 64
_PREVIEW_SIGN_TIMEOUT_SECONDS = 3.0
_preview_sign_executor = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="episode-preview-sign"
)


@dataclass(frozen=True)
class EpisodeMediaAccess:
    url: str
    expires_at: str
    media_type: str
    direct: bool = True

    def as_payload(self) -> dict[str, object]:
        return {
            "direct": self.direct,
            "url": self.url,
            "expires_at": self.expires_at,
            "media_type": self.media_type,
        }


def _run_with_timeout(fn, *, timeout_seconds: float):
    future = _preview_sign_executor.submit(fn)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeout:
        future.cancel()
        raise TimeoutError("episode preview signing timed out") from None


def issue_episode_preview_url(
    storage_uri: str,
    *,
    download_name: str,
    media_type: str,
    timeout_seconds: float = _PREVIEW_SIGN_TIMEOUT_SECONDS,
) -> EpisodeMediaAccess | None:
    """Sign one process preview object after the router has checked ownership.

    OSS existence checks and signing are bounded so workbench reads never hang
    indefinitely when object storage is slow or unreachable.
    """
    if not oss_client.browser_direct_enabled() or not is_cloud_uri(storage_uri):
        return None
    try:
        bucket, key = parse_storage_uri(storage_uri)
    except ValueError:
        return None
    process_bucket = oss_client.bucket_name("process")
    if bucket != process_bucket or not key.startswith(("process/v1/", "process/v2/")):
        return None
    if not cloud_path_is_safe(bucket, key) or not oss_client.browser_direct_object_allowed(
        bucket, key
    ):
        return None
    try:

        def _sign() -> str | None:
            if not oss_client.object_exists(bucket, key):
                return None
            return oss_client.sign_browser_get_url(
                bucket, key, download_name=download_name, inline=True
            )

        signed_url = _run_with_timeout(_sign, timeout_seconds=timeout_seconds)
    except TimeoutError:
        logger.warning("episode preview direct signing timed out")
        return None
    except Exception as exc:
        logger.warning("episode preview direct signing failed error_type=%s", type(exc).__name__)
        return None
    if not signed_url:
        return None
    ttl = oss_client.browser_direct_ttl_seconds()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    return EpisodeMediaAccess(url=signed_url, expires_at=expires_at, media_type=media_type)


def resolve_episode_preview_file(storage_uri: str, *, media_type: str) -> Path | None:
    """Resolve one persisted process preview without accepting a caller path.

    This is only used by the authenticated same-origin fallback when local
    development uses a cloud mirror in place of a real OSS browser endpoint.
    """
    try:
        bucket, key = parse_storage_uri(storage_uri)
    except ValueError:
        return None
    if not key.startswith(("process/v1/", "process/v2/")) or not cloud_path_is_safe(bucket, key):
        return None
    if is_cloud_uri(storage_uri) and bucket != oss_client.bucket_name("process"):
        return None
    if not storage_uri.startswith(("oss://", "nas://")):
        return None
    path = resolve_storage_path(storage_uri)
    if (
        path is None
        or not path.is_file()
        or not is_under_storage_root(path, root=storage_root_path())
    ):
        return None
    expected_media_type = _PREVIEW_MEDIA_TYPES.get(path.suffix.lower())
    actual_media_type = media_type.lower().split(";", 1)[0].strip()
    if expected_media_type != actual_media_type:
        return None
    return path


def issue_episode_preview_fallback_url(
    storage_uri: str,
    *,
    episode_id: int,
    actor_id: int | None,
    media_type: str,
) -> EpisodeMediaAccess | None:
    """Issue a short-lived same-origin Range URL only for a local mirror.

    Production and UAT use ``issue_episode_preview_url`` above, which keeps
    media delivery on OSS.  This fallback neither exposes a storage URI nor
    causes request-time downloads from OSS.
    """
    if actor_id is None or actor_id <= 0 or oss_client.is_oss_configured():
        return None
    if resolve_episode_preview_file(storage_uri, media_type=media_type) is None:
        return None
    token = create_signed_download_token(
        resource_type="episode_preview",
        resource_id=episode_id,
        subject=str(actor_id),
        ttl_seconds=_LOCAL_PREVIEW_TTL_SECONDS,
        max_uses=_LOCAL_PREVIEW_MAX_USES,
    )
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=_LOCAL_PREVIEW_TTL_SECONDS)
    ).isoformat()
    return EpisodeMediaAccess(
        url=f"/api/v1/episodes/{episode_id}/preview/media?sig={quote(token, safe='')}",
        expires_at=expires_at,
        media_type=media_type,
        direct=False,
    )
