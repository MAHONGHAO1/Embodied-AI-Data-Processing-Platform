"""Alibaba Cloud OSS client wrapper; falls back to local directory mirror when credentials are not configured (for dev/test)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from data.config import _is_public_oss_browser_endpoint, get_runtime_config, settings
from data.security.browser_oss import exact_bucket_origin

logger = logging.getLogger(__name__)

_oss2: Any | None = None
try:
    import oss2 as _oss2_module

    _oss2 = _oss2_module
except ImportError:
    pass


# Upload retries (network jitter tolerance)
_MAX_RETRIES = 3
_RETRY_BACKOFF = (1, 2, 4)

# Large file multipart upload threshold and part size
_MULTIPART_THRESHOLD = 100 * 1024 * 1024  # Files >= 100MB use multipart upload
_MULTIPART_PART_SIZE = 10 * 1024 * 1024  # 10MB per part
_MULTIPART_THREADS = 4

# EGO raw ingest uses this separate server-side multipart-copy implementation
# only after the caller has opted in.  It deliberately does not share the
# resumable local-file upload path above: no source bytes may traverse the API
# process while ingesting an OSS source object.
_EGO_MULTIPART_COPY_PART_SIZE = 256 * 1024 * 1024
_EGO_MULTIPART_COPY_MAX_PARTS = 10_000
_EGO_MULTIPART_COPY_MAX_PART_SIZE = 5 * 1024 * 1024 * 1024
_EGO_MULTIPART_COPY_MIN_PART_SIZE = 100 * 1024
_EGO_MULTIPART_COPY_FORMAT = "ego-multipart-v1"
_EGO_MULTIPART_COPY_FORMAT_HEADER = "x-oss-meta-quicdata-copy-format"
_IMMUTABLE_MULTIPART_COPY_ORIGIN_HEADER = "x-oss-meta-quicdata-copy-origin"
_EGO_MULTIPART_COPY_ORIGIN_HEADER = _IMMUTABLE_MULTIPART_COPY_ORIGIN_HEADER
_OSS_METADATA_HEADER_RE = re.compile(r"^x-oss-meta-[a-z0-9][a-z0-9-]{0,127}$")
_MAX_OSS_METADATA_ENTRIES = 16
_MAX_OSS_METADATA_VALUE_LENGTH = 1024
_LOCAL_MIRROR_DIRECTORY_CACHE_MAX_KEYS = 100_000
_LOCAL_MIRROR_DIRECTORY_CACHE_MAX_ENTRIES = 8
_LOCAL_MIRROR_DIRECTORY_CACHE: OrderedDict[tuple[str, int, int, int], tuple[str, ...]] = (
    OrderedDict()
)


def _new_provider_ref(bucket: str, key: str):
    """Resolve a logical bucket role for the provider-backed multipart path."""
    from data.infra.object_storage import StorageObjectRef
    from data.infra.storage_provider import get_storage_provider

    roles = {
        str(settings.oss_bucket_raw): "raw",
        str(settings.oss_bucket_process): "process",
        str(settings.oss_bucket_export): "export",
    }
    role = roles.get(str(bucket))
    if role is None:
        return None
    return get_storage_provider(), StorageObjectRef(role, str(key), None, "", 0)


def _provider_bucket_role(bucket: str) -> str | None:
    return {
        str(settings.oss_bucket_raw): "raw",
        str(settings.oss_bucket_process): "process",
        str(settings.oss_bucket_export): "export",
    }.get(str(bucket))


def _reject_unsupported_provider_bucket(bucket: str) -> None:
    """Reject buckets outside the provider's logical role boundary."""
    if _provider_bucket_role(bucket) is None:
        raise ValueError("bucket is outside the configured provider boundary")


@dataclass(frozen=True)
class OSSObjectInfo:
    """The bounded identity returned by a provider HEAD request."""

    size: int
    etag: str
    crc64: str | None
    version_id: str | None
    metadata: dict[str, str]


@dataclass(frozen=True)
class OSSMultipartPart:
    """One provider-confirmed multipart part, with no signed capability data."""

    number: int
    etag: str
    size: int


@dataclass(frozen=True)
class OSSListPage:
    """One bounded provider listing page.

    The continuation token is a server-side resume cursor.  Callers must not
    place it into an API DTO, signed URL, audit label, or browser state.
    """

    objects: Sequence[dict[str, object]]
    next_token: str | None


@dataclass(frozen=True)
class OSSDirectoryPage:
    """One bounded page of immediate child prefixes under an OSS root."""

    prefixes: Sequence[str]
    next_token: str | None


class OSSMultipartCopyPolicyError(ValueError):
    """The configured OSS topology cannot satisfy EGO no-overwrite semantics."""


def _with_retry(func):
    """Perform exponential backoff retries on OSS operations to withstand transient network jitter."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF[attempt])
    assert last_exc is not None
    raise last_exc


def _storage_config() -> dict[str, Any]:
    return get_runtime_config().get("storage", {})


def bucket_name(role: str) -> str:
    """Resolve bucket name by canonical role: raw / process / official / export."""
    buckets = _storage_config().get("buckets") or {}
    defaults = {
        "raw": "quic-data-platform",
        "process": "quic-process-qrdf",
        "official": "quic-qrdf",
        "export": "quic-lerobot",
    }
    return str(buckets.get(role) or defaults.get(role, "quic-data-platform"))


def is_oss_configured() -> bool:
    from data.config import _looks_like_placeholder_credential

    if settings.test_mode:
        return False
    oss_cfg = _storage_config().get("oss") or {}
    ak = str(oss_cfg.get("access_key_id") or "").strip()
    sk_raw = oss_cfg.get("access_key_secret")
    if isinstance(sk_raw, dict):
        # Ciphertext envelope should not appear in in-memory plaintext config; treated as unconfigured
        return False
    sk = str(sk_raw or "").strip()
    if not ak or not sk:
        return False
    if _looks_like_placeholder_credential(ak) or _looks_like_placeholder_credential(sk):
        return False
    return True


def cloud_enabled() -> bool:
    cfg = _storage_config()
    return cfg.get("type") == "oss" or bool(cfg.get("cloud_enabled"))


def _local_mirror_root() -> Path:
    return Path(settings.storage_root) / "cloud"


def _mirror_path(bucket: str, key: str) -> Path:
    return _local_mirror_root() / bucket / key


def _get_bucket(bucket: str):
    if settings.test_mode or not _oss2 or not is_oss_configured():
        return None
    oss_cfg = _storage_config().get("oss") or {}
    endpoint = oss_cfg.get("endpoint", "oss-cn-beijing.aliyuncs.com")
    auth = _oss2.Auth(oss_cfg["access_key_id"], oss_cfg["access_key_secret"])
    return _oss2.Bucket(auth, endpoint, bucket)


_BROWSER_DIRECT_ROLE_PREFIXES = {
    "process": ("process/v1/", "process/", "browser-previews/v1/"),
    "official": ("official/v1/", "datasets/", "episodes/"),
    "export": ("exports/v1/", "exports/"),
}
_AI_INPUT_KEY_RE = re.compile(r"ai-inputs/v1/episodes/[1-9][0-9]*/[0-9a-f]{64}\.mp4")
_RAW_IMPORT_OBJECT_RE = re.compile(
    r"raw/v2/workspaces/[1-9][0-9]*/task-sets/[1-9][0-9]*/batches/[1-9][0-9]*/"
    r"imports/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"original/[1-9][0-9]*/[^/\\\x00]{1,256}"
)
_RAW_COLLECTION_UPLOAD_OBJECT_RE = re.compile(
    r"raw/v2/workspaces/[1-9][0-9]*/collection-uploads/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"(?:[0-9a-f]{32}/)?upload\.bin"
)
_PROCESS_COLLECTION_PREVIEW_OBJECT_RE = re.compile(
    r"process/v2/workspaces/[1-9][0-9]*/collection-uploads/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"[0-9a-f]{32}/[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}"
)
_CONTENT_MD5_RE = re.compile(r"[A-Za-z0-9+/]{22}==")
_RAW_SOURCE_BROWSER_OBJECT_RE = re.compile(
    r"raw/v2/workspaces/[1-9][0-9]*/task-sets/[1-9][0-9]*/batches/[1-9][0-9]*/"
    r"episodes/[1-9][0-9]*/source/[1-9][0-9]*/"
    r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,127}/){0,7}[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
)
_OSS_MULTIPART_MAX_PARTS = 10_000


def _browser_direct_config() -> dict[str, Any]:
    value = _storage_config().get("browser_direct") or {}
    return value if isinstance(value, dict) else {}


def _is_public_browser_endpoint(value: object) -> bool:
    return _is_public_oss_browser_endpoint(str(value or ""))


def _is_public_browser_url(value: object) -> bool:
    """Validate a signed object URL, whose path is expected to be non-empty."""
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        endpoint = f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        return False
    return bool(parsed.path and parsed.path != "/" and _is_public_browser_endpoint(endpoint))


def _configured_browser_endpoint() -> str:
    return str(
        getattr(settings, "storage_browser_endpoint", "")
        or getattr(settings, "oss_browser_endpoint", "")
        or getattr(settings, "storage_endpoint", "")
        or ""
    ).strip()


def _is_development_loopback_upload_url(value: object) -> bool:
    """Allow part uploads to the configured loopback endpoint in development.

    ``sign_browser_upload_part`` signs both Duance collection multipart uploads
    and browser-import part URLs (``import_intake.sign_direct_multipart_part``).
    Production and test mode keep the public-HTTPS rule. A development process
    may sign only the exact loopback endpoint it was configured with
    (``storage_browser_endpoint``, else ``oss_browser_endpoint``, else
    ``storage_endpoint``). The host must be ``127.0.0.1``, ``localhost``, or
    ``::1``, and the scheme and port must match that endpoint. A private LAN
    address or any other host still fails closed.
    """
    if settings.is_production or settings.test_mode:
        return False
    if settings.environment.strip().lower() != "development":
        return False
    configured = _configured_browser_endpoint()
    try:
        parsed = urlsplit(str(value or "").strip())
        expected = urlsplit(configured)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    expected_host = (expected.hostname or "").lower().rstrip(".")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    if expected.scheme not in {"http", "https"} or expected_host != host:
        return False
    if parsed.scheme != expected.scheme or parsed.port != expected.port:
        return False
    return bool(parsed.path and parsed.path != "/")


def _browser_upload_url_allowed(value: object) -> bool:
    return _is_public_browser_url(value) or _is_development_loopback_upload_url(value)


def browser_direct_enabled() -> bool:
    """Whether this process can issue real public OSS URLs for browser reads."""
    cfg = _browser_direct_config()
    legacy_enabled = bool(
        cfg.get("enabled")
        and _is_public_browser_endpoint(cfg.get("endpoint"))
        and cloud_enabled()
        and _oss2 is not None
        and is_oss_configured()
    )
    provider_enabled = bool(
        (cfg.get("enabled") or getattr(settings, "oss_browser_direct_enabled", False))
        and str(getattr(settings, "storage_endpoint", "") or "").strip()
        and str(getattr(settings, "storage_access_key_id", "") or "").strip()
        and str(getattr(settings, "storage_secret_access_key", "") or "").strip()
        and str(
            getattr(settings, "storage_browser_endpoint", "")
            or getattr(settings, "oss_browser_endpoint", "")
            or ""
        ).strip()
        and _is_public_oss_browser_endpoint(
            str(
                getattr(settings, "storage_browser_endpoint", "")
                or getattr(settings, "oss_browser_endpoint", "")
                or ""
            )
        )
    )
    return legacy_enabled or provider_enabled


def browser_direct_ttl_seconds() -> int:
    """Return a bounded TTL even if a runtime config was mutated unexpectedly."""
    raw = _browser_direct_config().get("ttl_seconds", 900)
    try:
        ttl = int(raw)
    except (TypeError, ValueError):
        return 0
    return ttl if 60 <= ttl <= 3600 else 0


def browser_multipart_upload_enabled() -> bool:
    """Whether exact raw import writes can be signed for a browser."""
    if browser_direct_enabled():
        return True
    return bool(
        str(getattr(settings, "storage_endpoint", "") or "").strip()
        and str(getattr(settings, "storage_access_key_id", "") or "").strip()
        and str(getattr(settings, "storage_secret_access_key", "") or "").strip()
        and str(
            getattr(settings, "storage_browser_endpoint", "")
            or getattr(settings, "oss_browser_endpoint", "")
            or ""
        ).strip()
    )


def _validate_browser_multipart_object(bucket: str, key: str) -> None:
    key_text = str(key or "")
    raw_allowed = bucket == bucket_name("raw") and bool(
        _RAW_IMPORT_OBJECT_RE.fullmatch(key_text)
        or _RAW_COLLECTION_UPLOAD_OBJECT_RE.fullmatch(key_text)
    )
    # Client-generated collection previews are the only browser uploads that
    # may target the process bucket.
    preview_allowed = bucket == bucket_name("process") and bool(
        _PROCESS_COLLECTION_PREVIEW_OBJECT_RE.fullmatch(key_text)
    )
    if not (raw_allowed or preview_allowed) or not _provider_path_is_safe(bucket, key):
        raise ValueError("browser multipart object is not allowed")


def _validate_content_md5(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _CONTENT_MD5_RE.fullmatch(value) is None:
        raise ValueError("multipart part Content-MD5 is invalid")
    return value


def _validate_multipart_upload_id(value: object) -> str:
    upload_id = str(value or "")
    if (
        not 1 <= len(upload_id) <= 512
        or not upload_id.isascii()
        or any(not character.isprintable() or character.isspace() for character in upload_id)
    ):
        raise ValueError("multipart upload id is invalid")
    return upload_id


def _validate_multipart_part_number(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("multipart part number is invalid")
    try:
        part_number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("multipart part number is invalid") from exc
    if not 1 <= part_number <= _OSS_MULTIPART_MAX_PARTS:
        raise ValueError("multipart part number is invalid")
    return part_number


def _normalize_multipart_etag(value: object) -> str:
    etag = str(value or "").strip()
    if len(etag) >= 2 and etag.startswith('"') and etag.endswith('"'):
        etag = etag[1:-1]
    if (
        not 1 <= len(etag) <= 256
        or not etag.isascii()
        or any(not character.isprintable() for character in etag)
    ):
        raise ValueError("multipart part ETag is invalid")
    return etag


def init_browser_multipart_upload(bucket: str, key: str) -> str:
    """Create one immutable multipart upload for a canonical raw import key."""
    _validate_browser_multipart_object(bucket, key)
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        return _validate_multipart_upload_id(provider.create_multipart(ref))
    if not browser_multipart_upload_enabled():
        raise ValueError("browser multipart upload is unavailable")
    client = _get_bucket(bucket)
    if client is None:
        raise ValueError("browser multipart upload requires an OSS provider")
    from data.security.sse_kms import get_sse_kms_policy

    headers = dict(get_sse_kms_policy().build_headers())
    headers["x-oss-forbid-overwrite"] = "true"
    result = _with_retry(lambda: client.init_multipart_upload(key, headers=headers))
    return _validate_multipart_upload_id(getattr(result, "upload_id", None))


def sign_browser_upload_part(
    bucket: str,
    key: str,
    upload_id: str,
    part_number: int,
    content_md5: str | None = None,
) -> tuple[str, int, dict[str, str]]:
    """Sign only one numbered UploadPart request for a persisted upload.

    A given ``content_md5`` is part of the signature, so the storage service
    rejects a part whose bytes do not match the digest the client declared.
    """
    _validate_browser_multipart_object(bucket, key)
    checked_upload_id = _validate_multipart_upload_id(upload_id)
    checked_part_number = _validate_multipart_part_number(part_number)
    checked_md5 = _validate_content_md5(content_md5)
    headers = {"Content-Type": "application/octet-stream"}
    if checked_md5:
        headers["Content-MD5"] = checked_md5
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        if checked_md5:
            url = provider.sign_part(
                ref, checked_upload_id, checked_part_number, content_md5=checked_md5
            )
        else:
            url = provider.sign_part(ref, checked_upload_id, checked_part_number)
        if not _browser_upload_url_allowed(url):
            raise ValueError("provider returned an invalid browser upload URL")
        return url, browser_direct_ttl_seconds(), headers
    ttl = browser_direct_ttl_seconds()
    client = _get_browser_bucket(bucket)
    if client is None or ttl <= 0:
        raise ValueError("browser multipart upload is unavailable")
    url = client.sign_url(
        "PUT",
        key,
        ttl,
        headers=headers,
        params={"uploadId": checked_upload_id, "partNumber": str(checked_part_number)},
        slash_safe=True,
    )
    if not _browser_upload_url_allowed(url):
        raise ValueError("provider returned an invalid browser upload URL")
    return url, ttl, headers


def list_browser_multipart_parts(bucket: str, key: str, upload_id: str) -> list[OSSMultipartPart]:
    """Read the provider's bounded part manifest rather than trusting the client."""
    _validate_browser_multipart_object(bucket, key)
    checked_upload_id = _validate_multipart_upload_id(upload_id)
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        return [
            OSSMultipartPart(part.number, part.etag, part.size_bytes)
            for part in provider.list_parts(ref, checked_upload_id)
        ]
    client = _get_bucket(bucket)
    if client is None:
        raise ValueError("browser multipart upload requires an OSS provider")
    marker = ""
    parts: list[OSSMultipartPart] = []
    while True:
        result = _with_retry(
            lambda m=marker: client.list_parts(key, checked_upload_id, marker=m, max_parts=1000)
        )
        for item in list(getattr(result, "parts", None) or []):
            number = _validate_multipart_part_number(getattr(item, "part_number", None))
            try:
                size = int(getattr(item, "size", None))
            except (TypeError, ValueError) as exc:
                raise ValueError("multipart part size is invalid") from exc
            if size <= 0:
                raise ValueError("multipart part size is invalid")
            parts.append(
                OSSMultipartPart(
                    number=number,
                    etag=_normalize_multipart_etag(getattr(item, "etag", None)),
                    size=size,
                )
            )
        if len(parts) > _OSS_MULTIPART_MAX_PARTS:
            raise ValueError("multipart upload exceeds the part limit")
        if not bool(getattr(result, "is_truncated", False)):
            break
        next_marker = str(getattr(result, "next_marker", "") or "")
        if not next_marker or next_marker == marker:
            raise ValueError("multipart part listing did not advance")
        marker = next_marker
    numbers = [part.number for part in parts]
    if len(numbers) != len(set(numbers)):
        raise ValueError("multipart upload contains duplicate parts")
    return sorted(parts, key=lambda part: part.number)


def complete_browser_multipart_upload(
    bucket: str,
    key: str,
    upload_id: str,
    parts: list[OSSMultipartPart],
) -> None:
    """Complete exactly the provider-confirmed manifest for one raw object."""
    _validate_browser_multipart_object(bucket, key)
    checked_upload_id = _validate_multipart_upload_id(upload_id)
    if not parts or len(parts) > _OSS_MULTIPART_MAX_PARTS:
        raise ValueError("multipart part manifest is invalid")
    checked = [
        OSSMultipartPart(
            number=_validate_multipart_part_number(part.number),
            etag=_normalize_multipart_etag(part.etag),
            size=int(part.size),
        )
        for part in parts
    ]
    if any(part.size <= 0 for part in checked) or [part.number for part in checked] != list(
        range(1, len(checked) + 1)
    ):
        raise ValueError("multipart part manifest is invalid")
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        from data.infra.object_storage import MultipartPart

        provider, ref = provider_ref
        provider.complete_multipart(
            ref,
            checked_upload_id,
            [MultipartPart(part.number, part.etag, part.size) for part in checked],
        )
        return
    client = _get_bucket(bucket)
    if client is None or _oss2 is None:
        raise ValueError("browser multipart upload requires an OSS provider")
    provider_parts = [
        _oss2.models.PartInfo(part.number, part.etag, size=part.size) for part in checked
    ]
    _with_retry(
        lambda: client.complete_multipart_upload(
            key,
            checked_upload_id,
            provider_parts,
            headers={"x-oss-forbid-overwrite": "true"},
        )
    )


def abort_browser_multipart_upload(bucket: str, key: str, upload_id: str) -> None:
    """Abort only the exact raw multipart upload recorded by an ImportSession."""
    _validate_browser_multipart_object(bucket, key)
    checked_upload_id = _validate_multipart_upload_id(upload_id)
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        provider.abort_multipart(ref, checked_upload_id)
        return
    client = _get_bucket(bucket)
    if client is None:
        raise ValueError("browser multipart upload requires an OSS provider")
    _with_retry(lambda: client.abort_multipart_upload(key, checked_upload_id))


def _browser_direct_role_matches(bucket: str, key: str) -> bool:
    """Allow only a non-raw role's canonical prefix, even in a shared bucket."""
    return (
        bucket == bucket_name("raw") and _RAW_SOURCE_BROWSER_OBJECT_RE.fullmatch(key) is not None
    ) or any(
        bucket == bucket_name(role) and any(key.startswith(prefix) for prefix in prefixes)
        for role, prefixes in _BROWSER_DIRECT_ROLE_PREFIXES.items()
    )


def _validate_browser_direct_object(bucket: str, key: str) -> None:
    if (
        not _browser_direct_role_matches(bucket, key)
        or not _provider_path_is_safe(bucket, key)
        or not key
        or key.endswith("/")
    ):
        raise ValueError("browser direct object is not allowed")


def browser_direct_object_allowed(bucket: str, key: str) -> bool:
    """Check an exact target before a caller performs any OSS operation."""
    try:
        _validate_browser_direct_object(bucket, key)
    except ValueError:
        return False
    return True


def _get_browser_bucket(bucket: str):
    """Build a V4 client for the public browser endpoint only.

    This is intentionally separate from `_get_bucket`: all server-side I/O
    continues to use the deployment's internal endpoint.
    """
    if not browser_direct_enabled():
        return None
    oss_cfg = _storage_config().get("oss") or {}
    endpoint = str(_browser_direct_config().get("endpoint") or "").strip()
    # Keep signing subject to the same virtual-host origin derivation used by
    # CSP and Bucket CORS checks. An IP endpoint cannot be safely scoped here.
    if exact_bucket_origin(endpoint, bucket) is None:
        return None
    auth = _oss2.AuthV4(oss_cfg["access_key_id"], oss_cfg["access_key_secret"])
    return _oss2.Bucket(auth, endpoint, bucket, region=oss_cfg.get("region"))


def _safe_download_filename(value: str) -> str:
    candidate = Path(str(value or "").replace("\\", "/")).name
    cleaned = "".join(char for char in candidate if ord(char) >= 32 and char not in {'"', "\\"})
    return cleaned[:180] or "download"


def sign_browser_get_url(
    bucket: str,
    key: str,
    *,
    expires: int | None = None,
    download_name: str = "",
    inline: bool = False,
) -> str:
    """Sign one allowed OSS object for direct browser GET/Range access.

    An empty result means browser direct reads are unavailable; malformed or
    unapproved object references raise before touching an OSS client.
    """
    _validate_browser_direct_object(bucket, key)
    ttl = int(expires) if expires is not None else browser_direct_ttl_seconds()
    if not 60 <= ttl <= 3600:
        return ""
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        info = provider.head(ref)
        signed = provider.sign_get(info, expires=ttl)
        if not _is_public_browser_url(signed):
            raise ValueError("provider returned an invalid browser URL")
        return signed
    client = _get_browser_bucket(bucket)
    if client is None:
        return ""
    params: dict[str, str] | None = None
    if download_name:
        filename = quote(_safe_download_filename(download_name), safe="")
        params = {
            "response-content-disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{filename}",
        }
    signed_url = client.sign_url("GET", key, ttl, params=params, slash_safe=True)
    if not _is_public_browser_url(signed_url):
        raise ValueError("browser direct signing returned a non-public URL")
    return signed_url


def sign_ai_input_get_url(
    bucket: str,
    key: str,
    *,
    endpoint: str,
    expires: int,
) -> str:
    """Sign one content-addressed AI preview without exposing broader prefixes."""
    if (
        bucket != bucket_name("process")
        or not _AI_INPUT_KEY_RE.fullmatch(str(key or ""))
        or not _provider_path_is_safe(bucket, key)
        or not _is_public_oss_browser_endpoint(endpoint)
        or not 60 <= int(expires) <= 3600
        or not cloud_enabled()
        or not is_oss_configured()
        or _oss2 is None
    ):
        raise ValueError("AI input direct signing is unavailable")
    oss_cfg = _storage_config().get("oss") or {}
    auth = _oss2.AuthV4(oss_cfg["access_key_id"], oss_cfg["access_key_secret"])
    client = _oss2.Bucket(auth, endpoint, bucket, region=oss_cfg.get("region"))
    signed_url = client.sign_url("GET", key, int(expires), slash_safe=True)
    if not _is_public_browser_url(signed_url):
        raise ValueError("AI input signing returned a non-public URL")
    return signed_url


def _put_object(
    client,
    key: str,
    local_file: Path,
    *,
    content_type: str = "",
    forbid_overwrite: bool = False,
    metadata: Mapping[str, str] | None = None,
) -> None:
    """Single file upload: large files use multipart upload, small files use direct put, both with retries."""
    from data.security.sse_kms import get_sse_kms_policy

    request_headers = dict(get_sse_kms_policy().build_headers())
    if content_type:
        request_headers["Content-Type"] = content_type
    if forbid_overwrite:
        request_headers["x-oss-forbid-overwrite"] = "true"
    request_headers.update(_safe_oss_metadata(metadata))
    size = local_file.stat().st_size
    if _oss2 and size >= _MULTIPART_THRESHOLD:

        def _do_multipart() -> None:
            store_root = Path(tempfile.gettempdir()) / "quicdata_oss_store"
            store_root.mkdir(parents=True, exist_ok=True)
            store = _oss2.ResumableStore(root=str(store_root))
            kwargs: dict[str, Any] = {
                "part_size": _MULTIPART_PART_SIZE,
                "num_threads": _MULTIPART_THREADS,
                "store": store,
            }
            if request_headers:
                kwargs["headers"] = request_headers
            _oss2.resumable_upload(client, key, str(local_file), **kwargs)

        _with_retry(_do_multipart)
    else:

        def _do_put() -> None:
            if request_headers:
                # oss2 put_object_from_file supports headers
                client.put_object_from_file(key, str(local_file), headers=request_headers)
            else:
                client.put_object_from_file(key, str(local_file))

        _with_retry(_do_put)


def _sync_local_mirror(local_path: Path, bucket: str, key: str) -> None:
    """Keep local mirror replica synchronously after uploading to cloud (standalone mode avoids downloading from cloud during preview)."""
    mirror = _mirror_path(bucket, key)
    try:
        if local_path.is_file():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, mirror)
        elif local_path.is_dir():
            # Force overwrite to avoid local mirror retaining stale files after data update at same path
            if mirror.exists():
                shutil.rmtree(mirror, ignore_errors=True)
            mirror.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(local_path, mirror)
    except Exception:
        logger.debug("本地镜像同步失败（不影响上云）: %s/%s", bucket, key)


def upload_file(
    local_path: Path,
    bucket: str,
    key: str,
    *,
    content_type: str = "",
    forbid_overwrite: bool = False,
    metadata: Mapping[str, str] | None = None,
) -> str:
    """Upload local file or directory to OSS (or local mirror), returning oss:// URI."""
    local_path = local_path.resolve()
    if not local_path.exists():
        raise FileNotFoundError(f"上传路径不存在: {local_path}")
    uri = f"oss://{bucket}/{key}"

    normalized_content_type = str(content_type or "").strip()
    normalized_metadata = _safe_oss_metadata(metadata)
    _reject_unsupported_provider_bucket(bucket)
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        if local_path.is_dir():
            from data.infra.object_storage import StorageNotReady

            for item in sorted(local_path.rglob("*")):
                if item.is_file() and not item.is_symlink():
                    child_key = f"{key.rstrip('/')}/{item.relative_to(local_path).as_posix()}"
                    child_ref = _new_provider_ref(bucket, child_key)
                    if child_ref is None:
                        raise StorageNotReady("provider bucket role is unavailable")
                    child_ref[0].put_worker_object(child_ref[1], str(item))
        else:
            provider.put_worker_object(ref, str(local_path))
        return uri
    client = _get_bucket(bucket)
    if client is None and not settings.test_mode:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady(
            "legacy OSS writes are disabled; use the configured object storage provider"
        )
    if client:
        if local_path.is_file():
            _put_object(
                client,
                key,
                local_path,
                content_type=normalized_content_type,
                forbid_overwrite=forbid_overwrite,
                metadata=normalized_metadata,
            )
        elif local_path.is_dir():
            if normalized_content_type or normalized_metadata:
                raise ValueError(
                    "content type and metadata are only supported for a single uploaded file"
                )
            for item in local_path.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(local_path).as_posix()
                    _put_object(
                        client,
                        f"{key.rstrip('/')}/{rel}",
                        item,
                        forbid_overwrite=forbid_overwrite,
                    )
        # Keep local mirror replica synchronously after uploading to cloud (standalone mode avoids downloading from cloud during preview)
        if _storage_config().get("keep_local_cache", True):
            _sync_local_mirror(local_path, bucket, key)
        return uri

    dest = _mirror_path(bucket, key)
    if local_path.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if forbid_overwrite and dest.exists():
            raise FileExistsError(f"目标对象已存在: oss://{bucket}/{key}")
        shutil.copy2(local_path, dest)
    elif local_path.is_dir():
        if normalized_content_type:
            raise ValueError("content type is only supported for a single uploaded file")
        if normalized_metadata:
            raise ValueError("metadata is only supported for a single uploaded file")
        if forbid_overwrite and dest.exists():
            raise FileExistsError(f"目标对象已存在: oss://{bucket}/{key}")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(local_path, dest)
    return uri


def download_to(local_dest: Path, bucket: str, key: str) -> Path:
    """Download from OSS (or local mirror) to a local directory."""
    from data.utils.storage_paths import (
        cloud_path_is_safe,
        is_under_storage_root,
        storage_root_path,
    )

    if not cloud_path_is_safe(bucket, key):
        raise ValueError("provider object path is invalid")
    _reject_unsupported_provider_bucket(bucket)
    local_dest = local_dest.resolve()
    if not is_under_storage_root(local_dest, root=storage_root_path()):
        raise ValueError("provider download destination is outside the storage sandbox")
    local_dest.mkdir(parents=True, exist_ok=True)

    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        try:
            verified = provider.head(ref)
        except Exception as exc:
            if not _provider_not_found(exc):
                raise
            return _download_provider_prefix(provider, ref, local_dest)
        target = _bounded_download_target(local_dest, Path(key).name)
        _reject_download_symlink_path(local_dest, Path(key).name)
        provider.download_file(verified, str(target))
        return local_dest

    client = _get_bucket(bucket)
    if client:
        if key.endswith("/") or not _object_exists(client, key):
            _download_prefix(client, bucket, key, local_dest)
        else:
            target = _bounded_download_target(local_dest, Path(key).name)
            client.get_object_to_file(key, str(target))
        return local_dest

    return copy_local_mirror_to(local_dest, bucket, key)


def download_object_to_file(local_file: Path, bucket: str, key: str) -> Path:
    """Download one exact object into the storage sandbox without prefix fallback."""
    _require_bounded_provider_path(bucket, key)
    target = _bounded_storage_file(local_file)
    if object_info(bucket, key) is None:
        raise FileNotFoundError(f"object does not exist: oss://{bucket}/{key}")

    client = _get_bucket(bucket)
    if client:
        target.parent.mkdir(parents=True, exist_ok=True)
        client.get_object_to_file(key, str(target))
        return target

    with _verified_local_mirror(bucket, key) as snapshot:
        if not snapshot.is_file() or snapshot.is_symlink():
            raise FileNotFoundError(f"object does not exist: oss://{bucket}/{key}")
        _replace_with_verified_snapshot(snapshot, target)
    return target


def copy_local_mirror_to(
    local_dest: Path,
    bucket: str,
    key: str,
    *,
    include_source_root: bool = False,
) -> Path:
    """Copy one verified local mirror without following any source symlink."""
    from data.utils.storage_paths import is_under_storage_root, storage_root_path

    local_dest = Path(local_dest).resolve()
    if not is_under_storage_root(local_dest, root=storage_root_path()):
        raise ValueError("provider download destination is outside the storage sandbox")
    with _verified_local_mirror(bucket, key) as snapshot:
        local_dest.mkdir(parents=True, exist_ok=True)
        if include_source_root or snapshot.is_file():
            target = _bounded_download_target(local_dest, snapshot.name)
            _replace_with_verified_snapshot(snapshot, target)
        else:
            for item in snapshot.iterdir():
                target = _bounded_download_target(local_dest, item.name)
                _replace_with_verified_snapshot(item, target)
    return local_dest


def copy_object(
    src_bucket: str,
    src_key: str,
    dst_bucket: str,
    dst_key: str,
    *,
    source_etag: str | None = None,
    source_version_id: str | None = None,
    forbid_overwrite: bool = False,
    metadata: Mapping[str, str] | None = None,
) -> str:
    """Copy object across buckets (or local mirror copy).

    Cloud environment prioritizes OSS server-side CopyObject to avoid pulling data back to client before re-uploading.
    """
    src_mirror = _require_bounded_provider_path(src_bucket, src_key)
    dst_mirror = _require_bounded_provider_path(dst_bucket, dst_key)
    uri = f"oss://{dst_bucket}/{dst_key}"
    src_provider = _new_provider_ref(src_bucket, src_key)
    dst_provider = _new_provider_ref(dst_bucket, dst_key)
    if src_provider is not None and dst_provider is not None:
        provider, src_ref = src_provider
        _dst_provider, dst_ref = dst_provider
        verified = provider.head(src_ref)
        if source_etag and verified.etag.strip('"') != str(source_etag).strip('"'):
            raise ValueError("source ETag precondition failed")
        if source_version_id and verified.version_id != source_version_id:
            raise ValueError("source version precondition failed")
        if metadata:
            raise ValueError("provider copy metadata replacement is unsupported")
        if forbid_overwrite:
            try:
                _dst_provider.head(dst_ref)
            except Exception:
                pass
            else:
                raise FileExistsError(f"目标对象已存在: oss://{dst_bucket}/{dst_key}")
        import tempfile

        Path(settings.scratch_root).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="quic-copy-", dir=settings.scratch_root) as staged:
            provider.download_file(verified, staged.name)
            provider.put_worker_object(dst_ref, staged.name)
        return uri
    client = _get_bucket(src_bucket)
    dst_client = _get_bucket(dst_bucket)
    copy_headers: dict[str, str] = {}
    copy_metadata = _safe_oss_metadata(metadata)
    if source_etag is not None:
        copy_headers["x-oss-copy-source-if-match"] = _safe_precondition_value(
            source_etag,
            label="source ETag",
        )
    if forbid_overwrite:
        copy_headers["x-oss-forbid-overwrite"] = "true"
    if copy_metadata:
        copy_headers["x-oss-metadata-directive"] = "REPLACE"
        copy_headers.update(copy_metadata)
    copy_params = (
        {"versionId": _safe_precondition_value(source_version_id, label="source version id")}
        if source_version_id is not None
        else None
    )

    if client and dst_client:
        target_client = client if src_bucket == dst_bucket else dst_client
        if src_bucket == dst_bucket:
            if copy_headers or copy_params:
                target_client.copy_object(
                    src_bucket,
                    src_key,
                    dst_key,
                    headers=copy_headers or None,
                    params=copy_params,
                )
            else:
                target_client.copy_object(src_bucket, src_key, dst_key)
        else:
            # OSS server-side cross-bucket copy (CopyObject), data does not pass through client
            if copy_headers or copy_params:
                target_client.copy_object(
                    src_bucket,
                    src_key,
                    dst_key,
                    headers=copy_headers or None,
                    params=copy_params,
                )
            else:
                target_client.copy_object(src_bucket, src_key, dst_key)
        if _storage_config().get("keep_local_cache", True):
            try:
                if src_mirror.exists():
                    with _verified_local_mirror(src_bucket, src_key) as snapshot:
                        _replace_with_verified_snapshot(snapshot, dst_mirror)
                else:
                    download_to(
                        dst_mirror.parent if dst_mirror.suffix else dst_mirror, dst_bucket, dst_key
                    )
            except Exception:
                logger.debug("copy_object 后本地镜像同步失败: %s/%s", dst_bucket, dst_key)
        return uri

    if not settings.test_mode:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady(
            "legacy OSS copy is disabled; use the configured object storage provider"
        )
    if not src_mirror.exists():
        raise FileNotFoundError(f"源对象不存在: oss://{src_bucket}/{src_key}")
    if source_etag is not None or source_version_id is not None or copy_metadata:
        # A local mirror cannot satisfy the provider's immutable source
        # preconditions.  Fail closed instead of pretending it is an OSS copy.
        raise ValueError("conditional CopyObject requires an OSS provider")
    if forbid_overwrite and dst_mirror.exists():
        raise FileExistsError(f"目标对象已存在: oss://{dst_bucket}/{dst_key}")
    with _verified_local_mirror(src_bucket, src_key) as snapshot:
        _replace_with_verified_snapshot(snapshot, dst_mirror)
    return uri


def multipart_copy_immutable_object(
    src_bucket: str,
    src_key: str,
    dst_bucket: str,
    dst_key: str,
    *,
    source_size: int,
    source_etag: str,
    source_version_id: str | None,
    copy_origin: str,
) -> str:
    """Copy one explicit immutable object with guarded multipart semantics.

    This is intentionally not a prefix operation.  The caller must pass a
    source ETag observed from an authority marker validation, and the target
    records the marker object's SHA-256 as controlled metadata for later
    idempotent verification.
    """
    _require_bounded_provider_path(src_bucket, src_key)
    _require_bounded_provider_path(dst_bucket, dst_key)
    if isinstance(source_size, bool) or not isinstance(source_size, int) or source_size <= 0:
        raise ValueError("multipart copy source size is invalid")
    checked_etag = _safe_precondition_value(source_etag, label="source ETag")
    checked_version_id = (
        _safe_precondition_value(source_version_id, label="source version id")
        if source_version_id is not None
        else None
    )
    checked_origin = _safe_multipart_copy_origin(copy_origin)

    from data.security.sse_kms import get_sse_kms_policy

    init_headers = dict(get_sse_kms_policy().build_headers())
    init_headers.update(
        {
            "x-oss-forbid-overwrite": "true",
            "x-oss-meta-sha256": checked_origin,
            _IMMUTABLE_MULTIPART_COPY_ORIGIN_HEADER: checked_origin,
        }
    )
    return _run_immutable_multipart_copy(
        src_bucket,
        src_key,
        dst_bucket,
        dst_key,
        source_size=source_size,
        source_etag=checked_etag,
        source_version_id=checked_version_id,
        init_headers=init_headers,
        target_matches=lambda: _immutable_multipart_copy_target_matches(
            dst_bucket,
            dst_key,
            source_size=source_size,
            copy_origin=checked_origin,
        ),
    )


def requires_multipart_copy(size: int) -> bool:
    """Return whether an immutable provider copy must use multipart copying."""
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("copy source size is invalid")
    return size >= _MULTIPART_THRESHOLD


def multipart_copy_object(
    src_bucket: str,
    src_key: str,
    dst_bucket: str,
    dst_key: str,
    *,
    source_size: int,
    source_etag: str,
    source_version_id: str | None,
    copy_origin: str,
) -> str:
    """Copy one immutable EGO MCAP inside OSS with multipart UploadPartCopy.

    The destination is guarded by x-oss-forbid-overwrite at both multipart-init
    and complete. That guarantee is invalid on a versioned target bucket, so
    versioning is checked at runtime and anything other than disabled fails
    closed. Each source range carries the assessed ETag and, when available,
    source version ID. The target multipart ETag/CRC cannot be compared to the
    source, therefore callers verify size plus controlled copy-origin metadata
    after this returns.
    """
    _require_bounded_provider_path(src_bucket, src_key)
    _require_bounded_provider_path(dst_bucket, dst_key)
    if src_key.rsplit("/", 1)[-1] != "data.mcap" or dst_key.rsplit("/", 1)[-1] != "data.mcap":
        raise ValueError("EGO multipart copy is restricted to data.mcap")
    if isinstance(source_size, bool) or not isinstance(source_size, int) or source_size <= 0:
        raise ValueError("multipart copy source size is invalid")
    checked_etag = _safe_precondition_value(source_etag, label="source ETag")
    checked_version_id = (
        _safe_precondition_value(source_version_id, label="source version id")
        if source_version_id is not None
        else None
    )
    checked_origin = _safe_multipart_copy_origin(copy_origin)

    from data.security.sse_kms import get_sse_kms_policy

    init_headers = dict(get_sse_kms_policy().build_headers())
    init_headers.update(
        {
            "x-oss-forbid-overwrite": "true",
            _EGO_MULTIPART_COPY_FORMAT_HEADER: _EGO_MULTIPART_COPY_FORMAT,
            _EGO_MULTIPART_COPY_ORIGIN_HEADER: checked_origin,
        }
    )
    return _run_immutable_multipart_copy(
        src_bucket,
        src_key,
        dst_bucket,
        dst_key,
        source_size=source_size,
        source_etag=checked_etag,
        source_version_id=checked_version_id,
        init_headers=init_headers,
        target_matches=lambda: _multipart_copy_target_matches(
            dst_bucket,
            dst_key,
            source_size=source_size,
            copy_origin=checked_origin,
        ),
    )


def _run_immutable_multipart_copy(
    src_bucket: str,
    src_key: str,
    dst_bucket: str,
    dst_key: str,
    *,
    source_size: int,
    source_etag: str,
    source_version_id: str | None,
    init_headers: Mapping[str, str],
    target_matches: Callable[[], bool],
) -> str:
    """Execute one guarded multipart CopyObject with a caller-owned matcher."""
    source_client = _get_bucket(src_bucket)
    target_client = _get_bucket(dst_bucket)
    if not source_client or not target_client or not _oss2:
        # A local mirror cannot provide range-level source identity conditions
        # or atomic destination no-overwrite semantics.
        raise OSSMultipartCopyPolicyError(
            "conditional multipart CopyObject requires an OSS provider"
        )
    _require_bucket_versioning_disabled(target_client)

    part_headers = {"x-oss-copy-source-if-match": source_etag}
    part_size = _multipart_copy_part_size(source_size)
    uri = f"oss://{dst_bucket}/{dst_key}"
    upload_id: str | None = None
    complete_started = False

    try:
        initiated = target_client.init_multipart_upload(dst_key, headers=dict(init_headers))
        upload_id = str(getattr(initiated, "upload_id", "") or "").strip()
        if not upload_id:
            raise ValueError("OSS multipart init response is missing upload id")

        parts = []
        for part_number, start in enumerate(range(0, source_size, part_size), start=1):
            end = min(start + part_size, source_size) - 1
            # oss2 mutates params to add uploadId/partNumber, so do not reuse
            # the source-version mapping across parts.
            part_params = {"versionId": source_version_id} if source_version_id else None
            result = target_client.upload_part_copy(
                src_bucket,
                src_key,
                (start, end),
                dst_key,
                upload_id,
                part_number,
                headers=dict(part_headers),
                params=part_params,
            )
            part_etag = str(getattr(result, "etag", "") or "").strip()
            if not part_etag:
                raise ValueError("OSS UploadPartCopy response is missing ETag")
            parts.append(_oss2.models.PartInfo(part_number, part_etag, size=end - start + 1))

        complete_started = True
        target_client.complete_multipart_upload(
            dst_key,
            upload_id,
            parts,
            headers={"x-oss-forbid-overwrite": "true"},
        )
        return uri
    except Exception:
        # A lost completion response is ambiguous: do not blindly retry
        # CompleteMultipartUpload. First prove that the immutable target was
        # created, otherwise abort this incomplete upload and let the job retry
        # from its normal fenced state transition.
        target_exists_with_expected_identity = False
        if upload_id is not None:
            try:
                target_exists_with_expected_identity = target_matches()
            except Exception:
                logger.warning(
                    "unable to verify OSS multipart copy target after failure", exc_info=True
                )
        if complete_started and target_exists_with_expected_identity:
            return uri
        if upload_id is not None:
            _abort_multipart_copy_safely(target_client, dst_key, upload_id)
        if target_exists_with_expected_identity:
            # A concurrent writer may have completed the same immutable target
            # while this upload was being aborted. The caller re-verifies it.
            return uri
        raise


def _multipart_copy_part_size(source_size: int) -> int:
    """Choose a bounded part size that cannot exceed OSS's 10,000-part limit."""
    minimum_for_part_limit = (
        source_size + _EGO_MULTIPART_COPY_MAX_PARTS - 1
    ) // _EGO_MULTIPART_COPY_MAX_PARTS
    part_size = max(_EGO_MULTIPART_COPY_PART_SIZE, minimum_for_part_limit)
    if part_size > _EGO_MULTIPART_COPY_MAX_PART_SIZE:
        raise ValueError("multipart copy source is too large for OSS part limits")
    part_count = (source_size + part_size - 1) // part_size
    if part_count <= 0 or part_count > _EGO_MULTIPART_COPY_MAX_PARTS:
        raise ValueError("multipart copy source exceeds OSS part limits")
    if part_count > 1 and part_size < _EGO_MULTIPART_COPY_MIN_PART_SIZE:
        raise ValueError("multipart copy part size is below the OSS minimum")
    return part_size


def _safe_multipart_copy_origin(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("multipart copy origin is invalid")
    normalized = value.strip()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("multipart copy origin is invalid")
    return normalized


def _require_bucket_versioning_disabled(client) -> None:
    """Fail closed unless the target bucket has object versioning disabled."""
    get_versioning = getattr(client, "get_bucket_versioning", None)
    if not callable(get_versioning):
        raise OSSMultipartCopyPolicyError("cannot verify raw bucket versioning")
    try:
        result = get_versioning()
    except Exception as exc:
        raise OSSMultipartCopyPolicyError("cannot verify raw bucket versioning") from exc
    if result is None or not hasattr(result, "status"):
        raise OSSMultipartCopyPolicyError("cannot verify raw bucket versioning")
    status = result.status
    if status is None:
        return
    if isinstance(status, str) and status.strip().lower() == "disabled":
        return
    raise OSSMultipartCopyPolicyError(
        "raw bucket versioning must be disabled for multipart no-overwrite copy"
    )


def _multipart_copy_target_matches(
    bucket: str,
    key: str,
    *,
    source_size: int,
    copy_origin: str,
) -> bool:
    info = object_info(bucket, key)
    return bool(
        info is not None
        and info.size == source_size
        and info.metadata.get(_EGO_MULTIPART_COPY_FORMAT_HEADER) == _EGO_MULTIPART_COPY_FORMAT
        and info.metadata.get(_EGO_MULTIPART_COPY_ORIGIN_HEADER) == copy_origin
    )


def _immutable_multipart_copy_target_matches(
    bucket: str,
    key: str,
    *,
    source_size: int,
    copy_origin: str,
) -> bool:
    info = object_info(bucket, key)
    return bool(
        info is not None
        and info.size == source_size
        and info.metadata.get("x-oss-meta-sha256") == copy_origin
        and info.metadata.get(_IMMUTABLE_MULTIPART_COPY_ORIGIN_HEADER) == copy_origin
    )


def _abort_multipart_copy_safely(client, key: str, upload_id: str) -> None:
    try:
        client.abort_multipart_upload(key, upload_id)
    except Exception:
        # Leave a durable task failure and an observable log. OSS lifecycle
        # cleanup remains the final backstop for a transport-level abort loss.
        logger.warning("OSS multipart copy abort failed", exc_info=True)


def copy_prefix(src_bucket: str, src_prefix: str, dst_bucket: str, dst_prefix: str) -> str:
    """Copy all objects under the prefix."""
    if not settings.test_mode and _new_provider_ref(src_bucket, src_prefix or "prefix") is not None:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady("legacy prefix copy requires a provider-native manifest operation")
    src_mirror = _require_bounded_provider_path(src_bucket, src_prefix.rstrip("/"))
    dst_mirror = _require_bounded_provider_path(dst_bucket, dst_prefix.rstrip("/"))
    prefix = src_prefix.rstrip("/") + "/"
    dst_base = dst_prefix.rstrip("/")
    client = _get_bucket(src_bucket)

    if client:
        objects: list[tuple[str, str]] = []
        for obj in _oss2.ObjectIterator(client, prefix=prefix):
            object_key = getattr(obj, "key", None)
            if (
                not isinstance(object_key, str)
                or not object_key.startswith(prefix)
                or not _provider_path_is_safe(src_bucket, object_key)
            ):
                raise ValueError("provider object path is invalid")
            rel = object_key[len(prefix) :]
            if not rel:
                continue
            dst_key = f"{dst_base}/{rel}"
            _require_bounded_provider_path(dst_bucket, dst_key)
            objects.append((object_key, dst_key))
        for object_key, dst_key in objects:
            copy_object(src_bucket, object_key, dst_bucket, dst_key)
        # Local mirror sync: prefer direct copy from source mirror (faster than cloud download in standalone mode)
        if _storage_config().get("keep_local_cache", True):
            try:
                if src_mirror.exists() and (src_mirror.is_file() or any(src_mirror.iterdir())):
                    with _verified_local_mirror(
                        src_bucket,
                        src_prefix.rstrip("/"),
                    ) as snapshot:
                        _replace_with_verified_snapshot(snapshot, dst_mirror)
                else:
                    download_to(dst_mirror, dst_bucket, dst_base)
            except Exception:
                logger.debug("copy_prefix 后本地镜像同步失败: %s/%s", dst_bucket, dst_base)
        return f"oss://{dst_bucket}/{dst_base}/"

    if not src_mirror.exists():
        raise FileNotFoundError(f"源前缀不存在: oss://{src_bucket}/{prefix}")
    with _verified_local_mirror(src_bucket, src_prefix.rstrip("/")) as snapshot:
        _replace_with_verified_snapshot(snapshot, dst_mirror)
    return f"oss://{dst_bucket}/{dst_base}/"


def _object_exists(client, key: str) -> bool:
    try:
        client.head_object(key)
        return True
    except Exception as exc:
        if _is_confirmed_object_absence(exc):
            return False
        raise


def _is_confirmed_object_absence(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 404 and getattr(exc, "code", None) == "NoSuchKey"


def _is_provider_object_absence(exc: Exception) -> bool:
    """Keep legacy optional-HEAD helpers compatible with typed providers."""
    from data.infra.object_storage import StorageObjectNotFound

    return isinstance(exc, StorageObjectNotFound) or (
        "not found" in str(exc).lower() or "404" in str(exc)
    )


def object_exists(bucket: str, key: str) -> bool:
    """Return whether one exact provider object or local mirror file exists."""
    if not _provider_path_is_safe(bucket, key):
        return False
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        try:
            provider.head(ref)
            return True
        except Exception as exc:
            if _is_provider_object_absence(exc):
                return False
            raise
    client = _get_bucket(bucket)
    if client:
        return _object_exists(client, key)
    if not settings.test_mode:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady("legacy OSS object lookup is disabled; use the configured provider")
    return _bounded_mirror_path(bucket, key).is_file()


def object_info(bucket: str, key: str) -> OSSObjectInfo | None:
    """Return one exact object HEAD identity without reading its body.

    The function deliberately exposes only object identity fields needed by
    server-side ingest verification.  It never returns endpoints, signed URLs,
    credentials, or arbitrary response headers.
    """
    if not _provider_path_is_safe(bucket, key):
        raise ValueError("provider object path is invalid")
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        try:
            persisted = provider.head(ref)
        except Exception as exc:
            if _is_provider_object_absence(exc):
                return None
            raise
        return OSSObjectInfo(
            size=persisted.size_bytes,
            etag=persisted.etag,
            crc64=persisted.crc64,
            version_id=persisted.version_id,
            metadata={"sha256": persisted.sha256} if persisted.sha256 else {},
        )
    client = _get_bucket(bucket)
    if client:
        try:
            response = client.head_object(key)
        except Exception as exc:
            if _is_confirmed_object_absence(exc):
                return None
            raise
        headers = {
            str(name).lower(): str(value)
            for name, value in dict(getattr(response, "headers", {}) or {}).items()
            if isinstance(name, str) and isinstance(value, (str, int))
        }
        raw_size = getattr(response, "content_length", None)
        if raw_size is None:
            raw_size = headers.get("content-length")
        try:
            size = int(raw_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("provider HEAD response has an invalid content length") from exc
        if size < 0:
            raise ValueError("provider HEAD response has an invalid content length")
        etag = str(getattr(response, "etag", "") or headers.get("etag") or "").strip()
        if not etag:
            raise ValueError("provider HEAD response is missing ETag")
        crc64 = headers.get("x-oss-hash-crc64ecma")
        version_id = headers.get("x-oss-version-id")
        metadata = {
            name: value for name, value in headers.items() if name.startswith("x-oss-meta-")
        }
        return OSSObjectInfo(
            size=size,
            etag=etag,
            crc64=crc64,
            version_id=version_id or None,
            metadata=metadata,
        )
    if not settings.test_mode:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady("legacy OSS object lookup is disabled; use the configured provider")

    source = _bounded_mirror_path(bucket, key)
    if not source.is_file() or source.is_symlink():
        return None
    return OSSObjectInfo(
        size=source.stat().st_size,
        etag="",
        crc64=None,
        version_id=None,
        metadata={},
    )


def try_create_json_object(bucket: str, key: str, payload: dict[str, Any]) -> bool:
    """Atomically create a bounded JSON authority, reusing only an exact payload."""
    if not _provider_path_is_safe(bucket, key):
        raise ValueError("provider object path is invalid")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("provider JSON object is too large")
    client = _get_bucket(bucket)
    if client:
        from data.security.sse_kms import get_sse_kms_policy

        headers = dict(get_sse_kms_policy().build_headers())
        headers["x-oss-forbid-overwrite"] = "true"
        try:
            _with_retry(lambda: client.put_object(key, encoded, headers=headers))
            return True
        except Exception as exc:
            if not _is_forbid_overwrite_conflict(exc):
                raise
            try:
                return read_json_object(bucket, key) == payload
            except Exception:
                return False

    target = _bounded_mirror_path(bucket, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = _bounded_mirror_path(bucket, key)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        try:
            return read_json_object(bucket, key) == payload
        except Exception:
            return False
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return True


def _is_forbid_overwrite_conflict(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 409 and getattr(exc, "code", None) in {
        "FileAlreadyExists",
        "ObjectAlreadyExists",
    }


def _bounded_mirror_path(bucket: str, key: str) -> Path:
    from data.utils.storage_paths import resolve_cloud_mirror_path

    target = resolve_cloud_mirror_path(bucket, key)
    if target is None:
        raise ValueError("provider object path is invalid")
    return target


def _require_bounded_provider_path(bucket: str, key: str) -> Path:
    if not _provider_path_is_safe(bucket, key):
        raise ValueError("provider object path is invalid")
    return _bounded_mirror_path(bucket, key)


@contextmanager
def _verified_local_mirror(bucket: str, key: str) -> Iterator[Path]:
    source = _require_bounded_provider_path(bucket, key)
    unresolved = _mirror_path(bucket, key)
    if unresolved.is_symlink():
        raise ValueError("provider mirror source contains a symlink")
    if not source.exists():
        raise FileNotFoundError(
            f"OSS 对象不存在: oss://{bucket}/{key}"
            "（本地 cloud/ 镜像缺失，且未配置可用的远程 OSS。"
            "无 OSS 时请勿清理权威镜像，或重新接入/入库数据）"
        )
    from data.services.cloud_storage import verified_publish_snapshot

    with verified_publish_snapshot(source) as snapshot:
        yield snapshot


def _replace_with_verified_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _provider_path_is_safe(bucket: str, key: str) -> bool:
    from data.utils.storage_paths import cloud_path_is_safe

    return cloud_path_is_safe(bucket, key)


def decode_json_object(payload: bytes | str) -> dict[str, Any]:
    """Decode one authority JSON object without accepting duplicate keys."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name, item in pairs:
            if name in value:
                raise ValueError("JSON object contains a duplicate key")
            value[name] = item
        return value

    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    value = json.loads(text, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("JSON value must be an object")
    return value


def _safe_precondition_value(value: object, *, label: str) -> str:
    """Reject header/query injection in provider-supplied identity values."""
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    normalized = value.strip()
    if not normalized or "\r" in normalized or "\n" in normalized or "\x00" in normalized:
        raise ValueError(f"{label} is invalid")
    return normalized


def _safe_oss_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    """Validate the small controlled metadata surface used by immutable exports."""
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping) or len(metadata) > _MAX_OSS_METADATA_ENTRIES:
        raise ValueError("OSS metadata is invalid")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in metadata.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise ValueError("OSS metadata is invalid")
        key = raw_key.lower()
        value = raw_value.strip()
        if (
            _OSS_METADATA_HEADER_RE.fullmatch(key) is None
            or not value
            or len(value) > _MAX_OSS_METADATA_VALUE_LENGTH
            or not value.isascii()
            or any(not character.isprintable() for character in value)
            or "\r" in value
            or "\n" in value
            or "\x00" in value
            or key in normalized
        ):
            raise ValueError("OSS metadata is invalid")
        normalized[key] = value
    return normalized


def read_json_object(
    bucket: str,
    key: str,
    *,
    max_bytes: int = 64 * 1024,
    if_match: str | None = None,
) -> dict[str, Any]:
    """Read one bounded JSON object for internal authority checks."""
    if not _provider_path_is_safe(bucket, key):
        raise ValueError("provider object path is invalid")
    client = _get_bucket(bucket)
    if client:
        if if_match is None:
            payload = client.get_object(key).read(max_bytes + 1)
        else:
            payload = client.get_object(
                key,
                headers={"If-Match": _safe_precondition_value(if_match, label="JSON ETag")},
            ).read(max_bytes + 1)
    else:
        source = _bounded_mirror_path(bucket, key)
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError("provider object is unavailable")
        if source.stat().st_size > max_bytes:
            raise ValueError("provider JSON object is too large")
        payload = source.read_bytes()
    if len(payload) > max_bytes:
        raise ValueError("provider JSON object is too large")
    try:
        return decode_json_object(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("provider JSON object is invalid") from exc


def _download_prefix(client, bucket: str, prefix: str, local_dest: Path) -> None:
    prefix = prefix.rstrip("/") + "/"
    for obj in _oss2.ObjectIterator(client, prefix=prefix):
        rel = obj.key[len(prefix) :]
        if not rel:
            continue
        target = _bounded_download_target(local_dest, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        client.get_object_to_file(obj.key, str(target))


def _provider_not_found(exc: Exception) -> bool:
    """Recognize only provider not-found responses; preserve other failures."""
    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        status = getattr(current, "status", None)
        code = str(getattr(current, "code", "") or "").lower()
        response = getattr(current, "response", None)
        error_code = (
            str((response or {}).get("Error", {}).get("Code", "") or "").lower()
            if isinstance(response, dict)
            else ""
        )
        message = str(current).lower()
        if (
            status == 404
            or code in {"404", "nosuchkey", "notfound", "no_such_key"}
            or error_code in {"404", "nosuchkey", "notfound"}
            or "404" in message
            or "not found" in message
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _download_provider_prefix(provider: object, ref: object, local_dest: Path) -> Path:
    """Preflight and materialize one provider directory under its exact boundary."""
    from data.infra.object_storage import StorageListPage

    role = ref.bucket_role
    prefix = str(ref.object_key)
    if not prefix or prefix.endswith("/"):
        prefix = prefix.rstrip("/")
    objects = []
    token = None
    for _ in range(10_000):
        page = provider.list_prefix(role, prefix, continuation_token=token, max_keys=1000)
        if not isinstance(page, StorageListPage):
            raise ValueError("provider prefix listing returned an invalid page")
        objects.extend(page.objects)
        if len(objects) > 100_000:
            raise ValueError("provider prefix exceeds the download object limit")
        next_token = page.next_token
        if next_token is None:
            break
        if next_token == token:
            raise ValueError("provider prefix continuation did not advance")
        token = next_token
    else:
        raise ValueError("provider prefix listing exceeded the page limit")
    if not objects:
        raise FileNotFoundError(f"object prefix does not exist: oss://{ref.bucket_role}/{prefix}")

    rel_paths: list[tuple[object, str]] = []
    seen: set[str] = set()
    for item in objects:
        key = str(getattr(item, "object_key", "") or "")
        boundary = prefix.rstrip("/") + "/"
        if not key.startswith(boundary) or key == boundary:
            continue
        relative = key[len(boundary) :]
        target = _bounded_download_target(local_dest, relative)
        _reject_download_symlink_path(local_dest, relative)
        normalized = target.relative_to(local_dest.resolve()).as_posix()
        if normalized in seen:
            raise ValueError("provider prefix contains duplicate object paths")
        seen.add(normalized)
        rel_paths.append((item, relative))
    if not rel_paths:
        raise FileNotFoundError(f"object prefix does not exist: {prefix}")
    for _, relative in rel_paths:
        parts = Path(relative).parts
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in seen:
                raise ValueError("provider prefix contains file/directory collision")
            ancestor = local_dest.resolve().joinpath(*parts[:index])
            if ancestor.exists() and not ancestor.is_dir():
                raise ValueError("provider download target collides with an existing file")
    for _item, relative in rel_paths:
        target = _bounded_download_target(local_dest, relative)
        if target.exists() and target.is_symlink():
            raise ValueError("provider download target must not be a symlink")
        if target.exists():
            raise ValueError("provider download target already exists")
    for item, relative in rel_paths:
        target = _bounded_download_target(local_dest, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        provider.download_file(item, str(target))
    return local_dest


def _bounded_download_target(local_dest: Path, relative_key: str) -> Path:
    from data.utils.storage_paths import cloud_path_is_safe, is_under_storage_root

    if not cloud_path_is_safe("provider", relative_key):
        raise ValueError("provider object key escaped the download sandbox")
    base = local_dest.resolve()
    target = (base / relative_key).resolve()
    if not is_under_storage_root(target, root=base):
        raise ValueError("provider object key escaped the download sandbox")
    return target


def _reject_download_symlink_path(local_dest: Path, relative_key: str) -> None:
    """Reject existing links anywhere in a destination path before writing."""
    base = local_dest.resolve()
    current = base
    for part in Path(relative_key).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("provider download target must not contain a symlink")


def _bounded_storage_file(local_file: Path) -> Path:
    """Resolve one writable file beneath storage_root without following escapes."""
    from data.utils.storage_paths import is_under_storage_root, storage_root_path

    candidate = Path(local_file)
    if candidate.name in {"", ".", ".."}:
        raise ValueError("provider download target is invalid")
    try:
        parent = candidate.parent.resolve()
        target = (parent / candidate.name).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("provider download target is invalid") from exc
    if not is_under_storage_root(target, root=storage_root_path()):
        raise ValueError("provider download destination is outside the storage sandbox")
    if target.exists() and target.is_symlink():
        raise ValueError("provider download target must not be a symlink")
    return target


def mirror_is_complete(bucket: str, key: str, mirror: Path) -> bool:
    """Verify whether local cloud/ mirror covers all objects under OSS prefix.

    When OSS is not configured or unreachable, the mirror is authoritative data and considered complete (returns True);
    when OSS is reachable, missing any OSS object in mirror is treated as incomplete,
    prompting callers to download from source to avoid using stale or partial mirrors for export, preview, etc.
    """
    client = _get_bucket(bucket)
    if not client:
        return True
    # Single file object
    if not key.endswith("/") and _object_exists(client, key):
        return mirror.is_file() and mirror.stat().st_size > 0
    prefix = key.rstrip("/") + "/"
    for obj in _oss2.ObjectIterator(client, prefix=prefix):
        rel = obj.key[len(prefix) :]
        if not rel or rel.endswith("/"):
            continue
        if not (mirror / rel).is_file():
            return False
    return True


def list_prefix_page(
    bucket: str,
    prefix: str,
    *,
    continuation_token: str | None,
    max_keys: int,
) -> OSSListPage:
    """List exactly one safe, bounded page without materializing a full prefix.

    Cloud OSS uses its native marker while the local mirror uses the last
    returned relative key as a stable cursor.  Both forms remain internal to
    the worker/database; callers never return them through browser endpoints.
    """
    if not settings.test_mode and _new_provider_ref(bucket, prefix or "listing") is not None:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady("legacy prefix listing requires a provider-native manifest operation")
    norm_prefix = _normalized_listing_prefix(bucket, prefix)
    checked_max_keys = _validate_listing_max_keys(max_keys)
    checked_token = _validate_listing_token(bucket, norm_prefix, continuation_token)
    client = _get_bucket(bucket)
    if client:
        result = _with_retry(
            lambda: client.list_objects(
                prefix=norm_prefix,
                marker=checked_token or "",
                max_keys=checked_max_keys,
            )
        )
        objects = tuple(
            item
            for item in (
                _listing_object_view(obj, bucket, norm_prefix)
                for obj in list(getattr(result, "object_list", []) or [])
            )
            if item is not None
        )
        next_token = None
        if bool(getattr(result, "is_truncated", False)):
            next_token = _validate_listing_token(
                bucket,
                norm_prefix,
                getattr(result, "next_marker", None),
            )
            if next_token is None or next_token == checked_token:
                raise ValueError("OSS prefix continuation did not advance")
        return OSSListPage(objects=objects, next_token=next_token)

    return _list_local_mirror_page(
        bucket,
        norm_prefix,
        continuation_token=checked_token,
        max_keys=checked_max_keys,
    )


def list_prefix_directory_page(
    bucket: str,
    prefix: str,
    *,
    continuation_token: str | None,
    max_keys: int,
) -> OSSDirectoryPage:
    """List immediate child directories without walking each package payload.

    This is used only by the worker-side V2 compatibility scanner: existing
    flat source packages appear as direct child directories while the new
    ``date=...`` layout is scanned through its own bounded date/hour cursor.
    """
    if not settings.test_mode and _new_provider_ref(bucket, prefix or "listing") is not None:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady(
            "legacy directory listing requires a provider-native manifest operation"
        )
    norm_prefix = _normalized_listing_prefix(bucket, prefix)
    checked_max_keys = _validate_listing_max_keys(max_keys)
    checked_token = _validate_listing_token(bucket, norm_prefix, continuation_token)
    client = _get_bucket(bucket)
    if client:
        result = _with_retry(
            lambda: client.list_objects(
                prefix=norm_prefix,
                delimiter="/",
                marker=checked_token or "",
                max_keys=checked_max_keys,
            )
        )
        prefixes = tuple(
            _validated_listing_directory_prefix(bucket, norm_prefix, item)
            for item in list(getattr(result, "prefix_list", []) or [])
        )
        next_token = None
        if bool(getattr(result, "is_truncated", False)):
            next_token = _validate_listing_token(
                bucket,
                norm_prefix,
                getattr(result, "next_marker", None),
            )
            if next_token is None or next_token == checked_token:
                raise ValueError("OSS prefix continuation did not advance")
        return OSSDirectoryPage(prefixes=prefixes, next_token=next_token)

    return _list_local_mirror_directory_page(
        bucket,
        norm_prefix,
        continuation_token=checked_token,
        max_keys=checked_max_keys,
    )


def _normalized_listing_prefix(bucket: str, prefix: object) -> str:
    if (
        not isinstance(prefix, str)
        or prefix != prefix.strip()
        or "\\" in prefix
        or "\x00" in prefix
    ):
        raise ValueError("OSS prefix is invalid")
    normalized = prefix.rstrip("/")
    if not normalized:
        return ""
    # Validate as an object key because cloud_path_is_safe intentionally also
    # permits an empty key for storage-root operations.
    if not _provider_path_is_safe(bucket, f"{normalized}/.listing-probe"):
        raise ValueError("OSS prefix is invalid")
    return f"{normalized}/"


def _validate_listing_max_keys(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000:
        raise ValueError("OSS page size is invalid")
    return value


def _validate_listing_token(bucket: str, prefix: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 2_048:
        raise ValueError("OSS continuation token is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("OSS continuation token is invalid")
    if not value.startswith(prefix) or not _provider_path_is_safe(bucket, value):
        raise ValueError("OSS continuation token is invalid")
    return value


def _listing_object_view(obj: object, bucket: str, prefix: str) -> dict[str, object] | None:
    key = str(getattr(obj, "key", "") or "")
    if not key or not key.startswith(prefix) or not _provider_path_is_safe(bucket, key):
        return None
    try:
        size = int(getattr(obj, "size", 0))
    except (TypeError, ValueError):
        return None
    if size < 0:
        return None
    last_modified = getattr(obj, "last_modified", None)
    if isinstance(last_modified, datetime):
        rendered_last_modified: str | None = last_modified.strftime("%Y-%m-%d %H:%M:%S")
    elif last_modified is None:
        rendered_last_modified = None
    else:
        rendered_last_modified = str(last_modified)
    return {"key": key, "size": size, "last_modified": rendered_last_modified}


def _validated_listing_directory_prefix(bucket: str, prefix: str, value: object) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or not value.endswith("/"):
        raise ValueError("OSS directory listing contains an invalid prefix")
    child = value[len(prefix) : -1]
    if not child or "/" in child or not _provider_path_is_safe(bucket, f"{value}.listing-probe"):
        raise ValueError("OSS directory listing contains an invalid prefix")
    return value


def _local_mirror_listing_paths(bucket: str, prefix: str) -> tuple[Path, Path]:
    base = _mirror_path(bucket, "")
    mirror = _mirror_path(bucket, prefix.rstrip("/"))
    _require_non_symlink_local_mirror_path(base=base, target=mirror)
    if not mirror.exists():
        raise FileNotFoundError
    try:
        base_resolved = base.resolve()
        mirror_resolved = mirror.resolve()
        if mirror_resolved != base_resolved and base_resolved not in mirror_resolved.parents:
            raise ValueError("provider mirror escaped bucket sandbox")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("provider mirror is invalid") from exc
    return base, mirror


def _require_non_symlink_local_mirror_path(*, base: Path, target: Path) -> None:
    """Reject a mirror path when any component under its bucket is a link."""
    try:
        relative_parts = target.relative_to(base).parts
    except ValueError as exc:
        raise ValueError("provider mirror escaped bucket sandbox") from exc

    current = base
    if current.is_symlink():
        raise ValueError("provider mirror contains a symlink")
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("provider mirror contains a symlink")


def _local_mirror_relative_key(*, base: Path, path: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError as exc:
        raise ValueError("provider mirror escaped bucket sandbox") from exc


def _local_mirror_entry_can_follow_cursor(key: str, continuation_token: str | None) -> bool:
    """Whether a file or directory can contain a key after this cursor."""
    if continuation_token is None:
        return True
    return key > continuation_token or continuation_token.startswith(f"{key}/")


def _iter_local_mirror_files(
    *,
    base: Path,
    bucket: str,
    prefix: str,
    path: Path,
    continuation_token: str | None,
) -> Iterator[tuple[str, os.stat_result]]:
    """Yield lexicographic files after a cursor without rewalking prior trees.

    A local mirror has no provider-side continuation primitive.  Traversing
    sorted directories lets a continuation prune every completed sibling
    subtree, while the caller stops as soon as it has one page plus its
    sentinel.  That preserves the cloud page contract without an O(N) walk
    for every page.
    """
    if path.is_symlink():
        raise ValueError("provider mirror contains a symlink")
    if path.is_file():
        key = _local_mirror_relative_key(base=base, path=path)
        if not key.startswith(prefix) or not _provider_path_is_safe(bucket, key):
            raise ValueError("provider mirror contains an invalid object key")
        if continuation_token is None or key > continuation_token:
            try:
                yield key, path.stat()
            except OSError as exc:
                raise ValueError("provider mirror is invalid") from exc
        return
    if not path.is_dir():
        return
    try:
        with os.scandir(path) as entries:
            ordered_entries = sorted(entries, key=lambda entry: entry.name)
    except OSError as exc:
        raise ValueError("provider mirror is invalid") from exc

    for entry in ordered_entries:
        entry_path = Path(entry.path)
        key = _local_mirror_relative_key(base=base, path=entry_path)
        if not key.startswith(prefix) or not _provider_path_is_safe(bucket, key):
            raise ValueError("provider mirror contains an invalid object key")
        if not _local_mirror_entry_can_follow_cursor(key, continuation_token):
            continue
        if entry.is_symlink():
            raise ValueError("provider mirror contains a symlink")
        if entry.is_dir(follow_symlinks=False):
            yield from _iter_local_mirror_files(
                base=base,
                bucket=bucket,
                prefix=prefix,
                path=entry_path,
                continuation_token=continuation_token,
            )
        elif entry.is_file(follow_symlinks=False) and (
            continuation_token is None or key > continuation_token
        ):
            try:
                yield key, entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError("provider mirror is invalid") from exc


def _cached_local_mirror_directories(
    *, base: Path, mirror: Path, bucket: str, prefix: str
) -> tuple[str, ...]:
    """Build a bounded direct-directory index once per mirror generation.

    Flat V2 compatibility scans paginate immediate episode directories.  A
    small in-process cache avoids rescanning that same directory for every
    continuation page.  If the directory is too large to index safely, fail
    explicitly rather than converting local fallback into an unbounded worker
    operation.
    """
    try:
        stat = mirror.stat()
    except OSError as exc:
        raise ValueError("provider mirror is invalid") from exc
    cache_key = (str(mirror), int(stat.st_dev), int(stat.st_ino), int(stat.st_mtime_ns))
    cached = _LOCAL_MIRROR_DIRECTORY_CACHE.get(cache_key)
    if cached is not None:
        _LOCAL_MIRROR_DIRECTORY_CACHE.move_to_end(cache_key)
        return cached

    prefixes: list[str] = []
    try:
        with os.scandir(mirror) as entries:
            for entry in entries:
                entry_path = Path(entry.path)
                if entry.is_symlink():
                    raise ValueError("provider mirror contains a symlink")
                if not entry.is_dir(follow_symlinks=False):
                    continue
                key = f"{_local_mirror_relative_key(base=base, path=entry_path)}/"
                prefixes.append(_validated_listing_directory_prefix(bucket, prefix, key))
                if len(prefixes) > _LOCAL_MIRROR_DIRECTORY_CACHE_MAX_KEYS:
                    raise ValueError("provider mirror directory exceeds the listing limit")
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("provider mirror is invalid") from exc

    cached = tuple(sorted(prefixes))
    _LOCAL_MIRROR_DIRECTORY_CACHE[cache_key] = cached
    _LOCAL_MIRROR_DIRECTORY_CACHE.move_to_end(cache_key)
    while len(_LOCAL_MIRROR_DIRECTORY_CACHE) > _LOCAL_MIRROR_DIRECTORY_CACHE_MAX_ENTRIES:
        _LOCAL_MIRROR_DIRECTORY_CACHE.popitem(last=False)
    return cached


def _list_local_mirror_page(
    bucket: str,
    prefix: str,
    *,
    continuation_token: str | None,
    max_keys: int,
) -> OSSListPage:
    try:
        base, mirror = _local_mirror_listing_paths(bucket, prefix)
    except FileNotFoundError:
        return OSSListPage(objects=(), next_token=None)

    selected: list[tuple[str, os.stat_result]] = []
    for entry in _iter_local_mirror_files(
        base=base,
        bucket=bucket,
        prefix=prefix,
        path=mirror,
        continuation_token=continuation_token,
    ):
        selected.append(entry)
        if len(selected) > max_keys:
            break
    if not selected:
        return OSSListPage(objects=(), next_token=None)
    page_entries = selected[:max_keys]
    objects = [
        {
            "key": key,
            "size": stat.st_size,
            "last_modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for key, stat in page_entries
    ]
    return OSSListPage(
        objects=tuple(objects),
        next_token=str(objects[-1]["key"]) if len(selected) > max_keys else None,
    )


def _list_local_mirror_directory_page(
    bucket: str,
    prefix: str,
    *,
    continuation_token: str | None,
    max_keys: int,
) -> OSSDirectoryPage:
    try:
        base, mirror = _local_mirror_listing_paths(bucket, prefix)
    except FileNotFoundError:
        return OSSDirectoryPage(prefixes=(), next_token=None)
    if mirror.is_file():
        return OSSDirectoryPage(prefixes=(), next_token=None)

    prefixes = _cached_local_mirror_directories(
        base=base, mirror=mirror, bucket=bucket, prefix=prefix
    )
    start = bisect_right(prefixes, continuation_token) if continuation_token is not None else 0
    selected = prefixes[start : start + max_keys + 1]
    if not selected:
        return OSSDirectoryPage(prefixes=(), next_token=None)
    page_prefixes = selected[:max_keys]
    return OSSDirectoryPage(
        prefixes=page_prefixes,
        next_token=page_prefixes[-1] if len(selected) > max_keys else None,
    )


def list_prefix(
    bucket: str,
    prefix: str,
    extension_filter: str | None = None,
    *,
    limit: int | None = None,
) -> list[dict]:
    """List object inventory under specified OSS prefix (cloud-first, fallback to local mirror when OSS unavailable)."""
    if not settings.test_mode and _new_provider_ref(bucket, prefix or "listing") is not None:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady("legacy prefix listing requires a provider-native manifest operation")
    client = _get_bucket(bucket)
    norm_prefix = prefix.rstrip("/") + "/" if prefix else ""
    exts = [e.strip().lower() for e in (extension_filter or "").split(",") if e.strip()]

    def _match_ext(key: str) -> bool:
        if not exts:
            return True
        return any(key.lower().endswith(e) for e in exts)

    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000
    ):
        raise ValueError("OSS prefix limit is invalid")
    result: list[dict] = []
    if client:
        for obj in _oss2.ObjectIterator(client, prefix=norm_prefix):
            key = obj.key
            if not key or key == norm_prefix:
                continue
            if not _match_ext(key):
                continue
            last_modified = None
            if hasattr(obj, "last_modified") and obj.last_modified:
                try:
                    last_modified = obj.last_modified.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    last_modified = str(obj.last_modified)
            result.append({"key": key, "size": obj.size, "last_modified": last_modified})
            if limit is not None and len(result) >= limit:
                return result
        return result

    if not settings.test_mode:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady("legacy OSS listing is disabled; use the configured provider")

    # Fallback to local mirror when OSS client is unavailable
    base = _mirror_path(bucket, "")
    mirror = _mirror_path(bucket, prefix.rstrip("/"))
    if not mirror.exists():
        return []
    files = mirror.rglob("*") if mirror.is_dir() else [mirror]
    for f in files:
        if not f.is_file():
            continue
        key = f.relative_to(base).as_posix()
        if not _match_ext(key):
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        result.append(
            {
                "key": key,
                "size": f.stat().st_size,
                "last_modified": mtime.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        if limit is not None and len(result) >= limit:
            return result
    return result


def delete_object(bucket: str, key: str) -> bool:
    """Delete a single OSS object (or local mirror file/directory), returning whether it was deleted."""
    client = _get_bucket(bucket)
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        try:
            provider.delete_exact(provider.head(ref))
            return True
        except Exception as exc:
            if "not found" in str(exc).lower() or "404" in str(exc):
                return False
            raise
    if client:
        _with_retry(lambda: client.delete_object(key))
        return True
    if not settings.test_mode:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady(
            "legacy OSS deletes are disabled; use the configured object storage provider"
        )
    target = _mirror_path(bucket, key)
    if target.is_file():
        target.unlink(missing_ok=True)
        return True
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
        return True
    return False


def delete_prefix(bucket: str, prefix: str) -> int:
    """Batch delete all OSS objects under prefix (or local mirror directory), returning the count of deleted objects."""
    client = _get_bucket(bucket)
    if client:
        prefix = prefix.rstrip("/") + "/"
        keys = [obj.key for obj in _oss2.ObjectIterator(client, prefix=prefix)] if _oss2 else []
        deleted = 0
        for i in range(0, len(keys), 1000):
            batch = keys[i : i + 1000]
            if batch:
                _with_retry(lambda b=batch: client.batch_delete_objects(b))
                deleted += len(batch)
        return deleted

    if not settings.test_mode:
        from data.infra.object_storage import StorageNotReady

        raise StorageNotReady(
            "legacy OSS deletes are disabled; use the configured object storage provider"
        )
    target = _mirror_path(bucket, prefix.rstrip("/"))
    if target.is_dir():
        count = sum(1 for _ in target.rglob("*") if _.is_file())
        shutil.rmtree(target, ignore_errors=True)
        return count
    if target.is_file():
        target.unlink(missing_ok=True)
        return 1
    return 0
