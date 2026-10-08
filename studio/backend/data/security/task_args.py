"""Celery / asynchronous task argument security validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from data.utils.storage_paths import is_under_storage_root, storage_root_path


class TaskArgSecurityError(ValueError):
    pass


def assert_id_only_kwargs(allowed: set[str], kwargs: dict[str, Any]) -> None:
    """Reject undeclared path-like argument names from entering tasks."""
    banned_suffixes = ("_path", "_dir", "_file", "_uri")
    for key, value in kwargs.items():
        if key not in allowed and any(key.endswith(s) for s in banned_suffixes):
            raise TaskArgSecurityError(f"task arg not allowed: {key}")
        if (
            isinstance(value, str)
            and ("/" in value or "\\" in value)
            and key.endswith(("_path", "_dir", "_file"))
        ):
            raise TaskArgSecurityError(f"raw filesystem path forbidden in task arg: {key}")


def resolve_task_local_path(path_value: str | Path | None) -> Path | None:
    """Secondary resolution inside Worker: only allow paths within storage_root."""
    if not path_value:
        return None
    text = str(path_value).strip()
    if not text:
        return None
    root = storage_root_path()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / text
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise TaskArgSecurityError(f"invalid path: {path_value}") from exc
    if not is_under_storage_root(resolved, root=root):
        raise TaskArgSecurityError(f"path escapes storage_root: {path_value}")
    return resolved


def validate_preprocess_task_args(task_id: Any, operator: Any = "system") -> tuple[int, str]:
    if not isinstance(task_id, int) or task_id <= 0:
        raise TaskArgSecurityError("task_id must be positive int")
    op = str(operator or "system")[:128]
    return task_id, op
