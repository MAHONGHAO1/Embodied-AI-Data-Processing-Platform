"""Storage path resolution (aligned with QRDF SDK path conventions) and scratch_root sandboxing."""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote

from data.config import settings
from data.utils.storage_uri import is_cloud_uri

logger = logging.getLogger("quicdata.storage")
_PUBLICATION_CLOUD_PATH = re.compile(
    r"^(?P<root>episodes/ws[1-9]\d*/proj[1-9]\d*/task[1-9]\d*/" r"job-[A-Za-z0-9_-]{1,36})(?:/.*)?$"
)
_UNSAFE_ENCODED_CLOUD_COMPONENT = re.compile(
    r"%(?:00|25|2e|2f|5c)",
    re.IGNORECASE,
)


def storage_root_path() -> Path:
    return Path(settings.scratch_root).expanduser().resolve()


def is_under_storage_root(path: Path | str, *, root: Path | None = None) -> bool:
    """Return True if *path* is contained within the storage_root sandbox (resolved via realpath)."""
    try:
        candidate = Path(path).resolve()
        base = root or storage_root_path()
        if candidate == base:
            return True
        return base in candidate.parents
    except (OSError, RuntimeError, ValueError):
        return False


def publication_completion_key(key: str) -> str | None:
    """Return the stable marker key required by a publication root or descendant."""
    match = _PUBLICATION_CLOUD_PATH.fullmatch(key)
    if match is None:
        return None
    return f"{match.group('root')}.complete.json"


def cloud_path_is_safe(bucket: str, key: str) -> bool:
    """Validate already-decoded provider components before any provider or file access."""
    if not isinstance(bucket, str) or not bucket or not isinstance(key, str):
        return False
    if _unsafe_cloud_component(bucket, allow_path=False):
        return False
    return not _unsafe_cloud_component(key, allow_path=True)


def _unsafe_cloud_component(value: str, *, allow_path: bool) -> bool:
    if "\x00" in value or "\\" in value or _UNSAFE_ENCODED_CLOUD_COMPONENT.search(value):
        return True
    decoded = unquote(value)
    if decoded != value and (
        "\x00" in decoded or "\\" in decoded or _UNSAFE_ENCODED_CLOUD_COMPONENT.search(decoded)
    ):
        return True
    if not allow_path:
        return "/" in decoded or decoded in {".", ".."} or bool(PureWindowsPath(decoded).drive)
    if not decoded:
        return False
    if decoded.startswith(("/", "~")):
        return True
    if PurePosixPath(decoded).is_absolute() or PureWindowsPath(decoded).drive:
        return True
    return any(part in {".", ".."} for part in decoded.split("/"))


def resolve_cloud_mirror_path(bucket: str, key: str) -> Path | None:
    """Compose one provider mirror path and prove it stays in its bucket sandbox."""
    if not cloud_path_is_safe(bucket, key):
        return None
    try:
        root = storage_root_path()
        cloud_root = (root / "cloud").resolve()
        bucket_root = (cloud_root / bucket).resolve()
        candidate = (bucket_root / key).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not is_under_storage_root(cloud_root, root=root):
        return None
    if bucket_root == cloud_root or not is_under_storage_root(bucket_root, root=cloud_root):
        return None
    if not is_under_storage_root(candidate, root=bucket_root):
        return None
    return candidate


def cloud_path_has_required_authority(bucket: str, key: str) -> bool:
    """Default-deny incomplete publication prefixes; other cloud paths are unchanged."""
    if not cloud_path_is_safe(bucket, key):
        logger.info("denied unsafe cloud storage path")
        return False
    completion_key = publication_completion_key(key)
    if completion_key is None:
        return True
    if resolve_cloud_mirror_path(bucket, completion_key) is None:
        logger.info("denied publication marker outside the cloud mirror sandbox")
        return False
    from data.infra import oss_client

    try:
        if oss_client.object_exists(bucket, completion_key):
            return True
        logger.info("publication cloud prefix is not complete")
    except Exception as exc:
        logger.warning(
            "publication completion check failed error_type=%s",
            type(exc).__name__,
        )
    return False


def _normalize_path_param(path_param: str) -> str:
    """Normalize path separators (Windows/Unix) and strip well-known root prefixes."""
    text = path_param.strip().replace("\\", "/")
    # Traversal segments are rejected by downstream validation; only strip the root prefix here.
    root_name = Path(settings.storage_root).name
    for prefix in (f"{root_name}/", "runtime/storage/", "backend/runtime/storage/"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    if ":" in text.split("/")[0]:
        parts = PureWindowsPath(path_param.replace("/", "\\")).parts
        if "exports" in parts:
            idx = parts.index("exports")
            text = "/".join(parts[idx:])
        else:
            text = parts[-1] if parts else text
    return text


def _reject_unsafe_relative(normalized: str) -> bool:
    if not normalized or normalized.startswith("/"):
        return True
    parts = PurePosixPath(normalized).parts
    if any(p == ".." for p in parts):
        return True
    if PurePosixPath(normalized).is_absolute():
        return True
    return False


def resolve_storage_path(storage_path: str | None) -> Path | None:
    """Resolve a DB storage_path to a local absolute path that must reside inside storage_root.

    Cloud oss:// paths must be materialized first via cloud_storage.materialize_for_processing.
    In local-mirror mode, oss:// is mapped to storage_root/cloud/{bucket}/{key}.
    Pure-local nas://bucket/rel is mapped to storage_root/rel.
    """
    if not storage_path:
        return None
    if storage_path.startswith("nas://"):
        from data.utils.storage_uri import parse_storage_uri

        try:
            _bucket, key = parse_storage_uri(storage_path)
        except ValueError:
            return None
        root = storage_root_path()
        candidate = (root / key.lstrip("/")).resolve()
        if candidate.exists() and is_under_storage_root(candidate, root=root):
            return candidate
        return None
    if is_cloud_uri(storage_path):
        from data.utils.storage_uri import parse_storage_uri

        try:
            bucket, key = parse_storage_uri(storage_path)
        except ValueError:
            return None
        if not cloud_path_has_required_authority(bucket, key):
            return None
        root = storage_root_path()
        mirror = resolve_cloud_mirror_path(bucket, key)
        prefix_dir = resolve_cloud_mirror_path(bucket, key.rstrip("/"))
        if mirror is None or prefix_dir is None:
            return None
        if is_under_storage_root(mirror, root=root) and mirror.is_file():
            return mirror.resolve()
        if is_under_storage_root(mirror, root=root) and mirror.is_dir() and any(mirror.iterdir()):
            return mirror.resolve()
        if (
            is_under_storage_root(prefix_dir, root=root)
            and prefix_dir.is_dir()
            and any(prefix_dir.iterdir())
        ):
            return prefix_dir.resolve()
        from data.infra import oss_client

        if oss_client.is_oss_configured():
            try:
                download_dest = mirror.parent if mirror.suffix else prefix_dir
                if not is_under_storage_root(download_dest, root=root):
                    logger.warning("oss download destination is outside the storage sandbox")
                    return None
                oss_client.download_to(download_dest, bucket, key)
            except Exception as exc:
                logger.warning(
                    "oss materialize failed error_type=%s",
                    type(exc).__name__,
                )
            for candidate in (mirror, prefix_dir):
                if is_under_storage_root(candidate, root=root) and candidate.exists():
                    if candidate.is_file() or (candidate.is_dir() and any(candidate.iterdir())):
                        return candidate.resolve()
        return None

    normalized = _normalize_path_param(storage_path)
    root = storage_root_path()

    # P0: Deny direct access via absolute paths — only safe paths relative to storage_root are allowed.
    raw = Path(storage_path.strip())
    if raw.is_absolute():
        try:
            resolved = raw.resolve()
        except OSError:
            return None
        if is_under_storage_root(resolved, root=root) and resolved.exists():
            return resolved
        logger.info("denied absolute path outside the storage sandbox")
        try:
            from data.security.audit import emit_audit_event

            emit_audit_event(
                "security.path.denied",
                resource="storage_path",
                detail={"reason": "absolute_outside_root"},
                level="warning",
            )
        except Exception:
            pass
        return None

    if _reject_unsafe_relative(normalized):
        logger.info("denied unsafe relative storage path")
        try:
            from data.security.audit import emit_audit_event

            emit_audit_event(
                "security.path.denied",
                resource="storage_path",
                detail={"reason": "unsafe_relative"},
                level="warning",
            )
        except Exception:
            pass
        return None

    for candidate in (root / normalized, root / Path(normalized).name):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not is_under_storage_root(resolved, root=root):
            continue
        if resolved.exists():
            return resolved
    return None


def resolve_file_for_serve(path_param: str) -> Path | None:
    """Resolve the path parameter for /files/preview and /files/download (sandbox enforced).

    Args:
        path_param: Raw path query parameter value.

    Returns:
        Resolved absolute Path inside the storage root, or None if the path is unsafe.
    """
    if not path_param:
        return None

    # Explicitly reject absolute paths and traversal sequences (from query parameters).
    stripped = path_param.strip().replace("\\", "/")
    if (
        stripped.startswith("/")
        or stripped.startswith("~")
        or ".." in PurePosixPath(stripped).parts
    ):
        try:
            from data.security.audit import emit_audit_event

            emit_audit_event(
                "security.path.denied",
                resource="served_file",
                detail={"reason": "serve_path_rejected"},
                level="warning",
            )
        except Exception:
            pass
        return None

    normalized = _normalize_path_param(path_param)
    resolved = resolve_storage_path(normalized)
    root = storage_root_path()
    if resolved and resolved.is_file() and is_under_storage_root(resolved, root=root):
        return resolved

    name = PurePosixPath(normalized).name
    if not name or name in {".", ".."}:
        return None

    exports_dir = root / "exports"
    direct = exports_dir / name
    if direct.is_file() and is_under_storage_root(direct, root=root):
        return direct.resolve()

    if exports_dir.is_dir():
        for nested in exports_dir.glob(f"*/{name}"):
            if nested.is_file() and is_under_storage_root(nested, root=root):
                return nested.resolve()

    return None
