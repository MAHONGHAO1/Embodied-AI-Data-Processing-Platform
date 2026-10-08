"""QRDF episode path compatibility without weakening storage containment."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

_EPISODE_ID_PATTERN = re.compile(r"^episode_\d{6}$")
_EXTERNAL_EPISODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
DEFAULT_LEGACY_PREVIEW_FILE = "preview.mp4"

logger = logging.getLogger(__name__)


def validate_qrdf_episode_id(value: object) -> str:
    """Use the current SDK validation when available, with a safe v0.2 fallback."""
    try:
        from qrdf.utils.paths import validate_episode_id
    except ModuleNotFoundError as exc:
        if exc.name != "qrdf.utils.paths":
            raise
        if not isinstance(value, str) or _EPISODE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("episode ID must match episode_XXXXXX") from None
        return value
    return validate_episode_id(value)


def validate_qrdf_external_episode_id(value: object) -> str:
    """Validate a safe raw-producer ID without relaxing canonical dataset IDs."""
    try:
        from qrdf.utils.paths import validate_external_episode_id
    except ModuleNotFoundError as exc:
        if exc.name != "qrdf.utils.paths":
            raise
        if (
            not isinstance(value, str)
            or _EXTERNAL_EPISODE_ID_PATTERN.fullmatch(value) is None
            or value.endswith((".", " "))
            or PureWindowsPath(value).is_reserved()
        ):
            raise ValueError("episode ID must be a safe portable external identifier") from None
        return value
    return validate_external_episode_id(value)


def resolve_qrdf_episode_data_file(metadata: object, episode_dir: Path | str) -> Path:
    """Resolve ``metadata.data_file`` inside its episode for both supported SDKs."""
    episode_path = Path(episode_dir)
    native_resolver = getattr(metadata, "resolve_data_file", None)
    if callable(native_resolver):
        return Path(native_resolver(episode_path))

    data_file = getattr(metadata, "data_file", None)
    if not isinstance(data_file, str) or not data_file or "\x00" in data_file or "\\" in data_file:
        raise ValueError("data_file must be a non-empty portable relative path")
    posix_path = PurePosixPath(data_file)
    windows_path = PureWindowsPath(data_file)
    if (
        posix_path.is_absolute()
        or windows_path.anchor
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise ValueError("data_file must be a portable relative path")

    root = episode_path.resolve()
    candidate = root.joinpath(*posix_path.parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("data_file must resolve inside the episode directory") from exc
    return resolved


def resolve_legacy_qrdf_preview_file(episode_dir: Path | str) -> Path:
    """Resolve the historical ``preview_file`` field without reviving its SDK API.

    Current QRDF preview discovery is manifest-backed under ``media/preview``.
    This helper exists only for read-only access to older packages that recorded
    a top-level ``metadata.json.preview_file``.  Invalid historical values do
    not escape the episode directory and fall back to the former default name.
    """
    root = Path(episode_dir)
    preview_name = _legacy_preview_name(root)
    try:
        return _resolve_qrdf_relative_path(
            root,
            preview_name,
            field_name="legacy QRDF preview_file",
        )
    except ValueError:
        logger.warning(
            "Ignoring unsafe legacy QRDF preview_file; using the default preview name",
            extra={"qrdf_legacy_preview": "unsafe_value"},
        )
        return _resolve_qrdf_relative_path(
            root,
            DEFAULT_LEGACY_PREVIEW_FILE,
            field_name="legacy QRDF preview_file",
        )


def _legacy_preview_name(episode_dir: Path) -> object:
    metadata_path = episode_dir / "metadata.json"
    if metadata_path.is_symlink():
        logger.warning(
            "Ignoring legacy QRDF metadata.json symbolic link for preview compatibility",
            extra={"qrdf_legacy_preview": "metadata_symlink"},
        )
        return DEFAULT_LEGACY_PREVIEW_FILE
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return DEFAULT_LEGACY_PREVIEW_FILE
    if not isinstance(metadata, dict):
        return DEFAULT_LEGACY_PREVIEW_FILE
    return metadata.get("preview_file") or DEFAULT_LEGACY_PREVIEW_FILE


def _resolve_qrdf_relative_path(root: Path, value: object, *, field_name: str) -> Path:
    """Use the SDK resolver when present, retaining a safe v0.2 fallback."""
    try:
        from qrdf.utils.paths import resolve_path_within_directory
    except ModuleNotFoundError as exc:
        if exc.name != "qrdf.utils.paths":
            raise
    else:
        return Path(resolve_path_within_directory(root, value, field_name=field_name))

    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"{field_name} must be a non-empty portable relative path")
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.anchor
        or ".." in posix_path.parts
        or ".." in windows_path.parts
        or not posix_path.parts
    ):
        raise ValueError(f"{field_name} must be a portable relative path")

    candidate = root.joinpath(*posix_path.parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{field_name} must resolve inside the episode directory") from exc
    return candidate
