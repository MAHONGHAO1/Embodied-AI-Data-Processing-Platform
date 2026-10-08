"""Storage path and URI conversion utilities (nas:// / oss://)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from data.config import get_runtime_config, settings


def is_cloud_uri(path: str | None) -> bool:
    return bool(path and path.startswith("oss://"))


def parse_storage_uri(uri: str) -> tuple[str, str]:
    """Parse an oss://bucket/key or nas://bucket/key URI and return (bucket, key)."""
    if "://" not in uri:
        raise ValueError(f"非法存储 URI: {uri}")
    scheme, rest = uri.split("://", 1)
    if scheme not in ("oss", "nas"):
        raise ValueError(f"不支持的 URI 协议: {scheme}")
    if "/" not in rest:
        return rest, ""
    bucket, key = rest.split("/", 1)
    return bucket, unquote(key)


def is_public_storage_uri(uri: str | None) -> bool:
    """Return True if the URI is safe to expose in public responses (no credentials or path traversal)."""
    if not isinstance(uri, str) or not uri:
        return False
    parsed = urlsplit(uri)
    decoded_path = unquote(parsed.path)
    lowered = unquote(uri).lower()
    return bool(
        parsed.scheme in {"oss", "nas"}
        and parsed.netloc
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and "\\" not in decoded_path
        and ".." not in PurePosixPath(decoded_path).parts
        and not any(
            marker in lowered for marker in ("access_key=", "password=", "secret=", "token=")
        )
    )


def _public_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ""
    normalized = value.replace("\\", "/")
    lowered = unquote(normalized).lower()
    parts = PurePosixPath(unquote(normalized)).parts
    if (
        normalized.startswith(("/", "~"))
        or (len(normalized) > 1 and normalized[1] == ":")
        or ".." in parts
        or "://" in normalized
        or "?" in normalized
        or "#" in normalized
        or "\x00" in normalized
        or any(marker in lowered for marker in ("access_key=", "password=", "secret=", "token="))
    ):
        return ""
    return normalized


def local_mirror_to_oss_uri(path: str | None) -> str:
    """Restore a storage/cloud/{bucket}/... local mirror path to its oss:// URI."""
    if not path or is_cloud_uri(path):
        return path or ""
    try:
        root = (Path(settings.storage_root) / "cloud").resolve()
        rel = Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return ""
    if not rel or "/" not in rel:
        return ""
    bucket, key = rel.split("/", 1)
    return f"oss://{bucket}/{key}"


def join_storage_uri(base_uri: str, relative_path: str) -> str:
    """Append a relative path to an oss:// or nas:// base URI."""
    if not base_uri or not relative_path:
        return base_uri or relative_path or ""
    if not is_public_storage_uri(base_uri):
        return relative_path
    scheme = base_uri.split("://", 1)[0]
    bucket, prefix = parse_storage_uri(base_uri.rstrip("/") + "/")
    rel = _public_relative_path(relative_path)
    if not rel:
        return ""
    if prefix:
        key = f"{prefix.rstrip('/')}/{rel}"
    else:
        key = rel
    return f"{scheme}://{bucket}/{key}"


def to_storage_uri(local_path: str) -> str:
    """Convert a local path to a protocol URI, aligned with the 4.2.2 merge response format."""
    if not local_path:
        return ""
    if is_cloud_uri(local_path):
        return local_path
    mirror = local_mirror_to_oss_uri(local_path)
    if mirror:
        return mirror
    storage = get_runtime_config().get("storage", {})
    uri_scheme = storage.get("uri_scheme", "nas")
    bucket = storage.get("bucket", "quicdata-hot")
    try:
        root = Path(settings.storage_root).resolve()
        p = Path(local_path).resolve()
        rel = p.relative_to(root).as_posix()
    except ValueError:
        rel = Path(local_path).name
    if uri_scheme == "oss":
        raw_bucket = (storage.get("buckets") or {}).get("raw") or "quic-data-platform"
        return f"oss://{raw_bucket}/{rel}"
    return f"nas://{bucket}/{rel}"


def to_display_storage_uri(
    path: str | None,
    *,
    cloud_uri: str | None = None,
    bucket: str | None = None,
    key: str | None = None,
) -> str:
    """Return the user-visible canonical storage address.

    hybrid/cloud: prefer the oss:// canonical URI.
    local: prefer nas:// or a relative path; do not fabricate an oss:// URI.
    """
    from data.services.storage_mode import uses_cloud_uri_authority

    if cloud_uri and is_public_storage_uri(cloud_uri):
        return cloud_uri
    if cloud_uri and "://" in cloud_uri:
        return ""
    if path and path.startswith(("oss://", "nas://")):
        return path if is_public_storage_uri(path) else ""
    if path and "://" in path:
        return ""
    if bucket and key and uses_cloud_uri_authority():
        candidate = f"oss://{bucket}/{key.lstrip('/')}"
        return candidate if is_public_storage_uri(candidate) else ""
    mirror = local_mirror_to_oss_uri(path)
    if mirror and uses_cloud_uri_authority() and is_public_storage_uri(mirror):
        return mirror
    if path:
        converted = to_storage_uri(path)
        if (
            is_public_storage_uri(converted)
            and converted.startswith("oss://")
            and uses_cloud_uri_authority()
        ):
            return converted
        if is_public_storage_uri(converted) and converted.startswith("nas://"):
            return converted
    return path or ""


def qrdf_storage_display_uri(record: Any) -> str:
    """Return the display storage URI for a QrdfData record."""
    path = getattr(record, "storage_path", None) or ""
    if is_public_storage_uri(path):
        return path
    meta = getattr(record, "metadata_json", None) or {}
    for uri in (
        meta.get("cloud_uri"),
        meta.get("official_uri"),
        (meta.get("qrdf_write") or {}).get("storage_path"),
    ):
        if uri and is_public_storage_uri(str(uri)):
            return str(uri)
    return to_display_storage_uri(path)


def export_job_storage_display_uri(job: Any) -> str:
    """Return the display storage URI for an export job (oss:// terminal paths only, excluding local temp files)."""
    params = getattr(job, "params_json", None) or {}
    if is_public_storage_uri(params.get("storage_uri")):
        return str(params["storage_uri"])
    download_url = getattr(job, "download_url", None) or ""
    if is_public_storage_uri(download_url):
        return download_url
    return ""


def task_storage_display_uri(task: Any) -> str:
    """Return the best display storage URI from a Task record."""
    meta = getattr(task, "metadata_json", None) or {}
    collect = meta.get("collect") or {}
    preprocess = meta.get("preprocess") or {}
    mcap = meta.get("mcap_to_qrdf") or {}
    storage_path = getattr(task, "storage_path", None) or ""

    for uri in (
        storage_path,
        preprocess.get("cloud_uri"),
        mcap.get("cloud_uri"),
        mcap.get("storage_path"),
        preprocess.get("qrdf_dataset_path"),
        collect.get("raw_uri"),
    ):
        if uri and is_public_storage_uri(str(uri)):
            return str(uri)

    if (
        collect.get("source") == "oss_import"
        and collect.get("source_bucket")
        and collect.get("source_key")
    ):
        candidate = f"oss://{collect['source_bucket']}/{collect['source_key'].lstrip('/')}"
        return candidate if is_public_storage_uri(candidate) else ""
    if collect.get("source") == "oss_import" and collect.get("bucket") and collect.get("key"):
        candidate = f"oss://{collect['bucket']}/{collect['key'].lstrip('/')}"
        return candidate if is_public_storage_uri(candidate) else ""

    return to_display_storage_uri(storage_path)


def enrich_dataset_files_for_display(
    base_storage: str | None,
    files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a public file DTO containing only the relative path, size, and canonical storage URI."""
    display_base = to_display_storage_uri(base_storage) if base_storage else ""
    enriched: list[dict[str, Any]] = []
    for item in files:
        relative_path = _public_relative_path(item.get("relative_path"))
        if not relative_path:
            continue
        size_bytes = item.get("size_bytes")
        public_item = {
            "relative_path": relative_path,
            "size_bytes": size_bytes if isinstance(size_bytes, int) and size_bytes >= 0 else 0,
        }
        if item.get("truncated"):
            public_item["truncated"] = True
            enriched.append(public_item)
            continue
        rel = public_item["relative_path"]
        if display_base.startswith(("oss://", "nas://")):
            public_item["path_display"] = (
                join_storage_uri(display_base, rel) if rel else display_base
            )
        enriched.append(public_item)
    return enriched


def to_download_uri(local_path: str) -> str:
    """Generate an authorizable download URI (HTTP API or cloud-authoritative oss://).

    Separated from ``to_storage_uri`` / ``to_display_storage_uri``:
    - **Display/authority addresses** (nas://, oss:// synthesized from config) → use display/storage functions.
    - **This function** is only for actual downloadable links.

    Rules:
    1. Input is already oss:// → return as-is.
    2. Path is under storage/cloud/ mirror → restore oss://.
    3. Other local files inside the sandbox → ``/api/v1/files/download?path=<relative_path>`` (P0 sandbox enforced).
    4. Outside the sandbox → empty string.

    No longer synthesizes local disk-only paths into oss:// based on runtime ``uri_scheme``
    to avoid conflicting with P0 ("downloads go through authorized API, never expose absolute paths").
    """
    if not local_path:
        return ""
    if is_cloud_uri(local_path):
        return local_path
    mirror = local_mirror_to_oss_uri(local_path)
    if mirror:
        return mirror
    path = Path(local_path).resolve()
    root = Path(settings.storage_root).resolve()
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return ""
    if not rel or ".." in Path(rel).parts:
        return ""
    return f"/api/v1/files/download?path={quote(rel, safe='/')}"
