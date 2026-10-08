"""Workspace/TaskSet-scoped allowlist for platform-credential OSS reads."""

from __future__ import annotations

from pathlib import PurePosixPath

from sqlalchemy.orm import object_session

from data.database import Batch, ExternalOssImportScope


class OssImportScopeError(PermissionError):
    pass


def _normalized_key(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").strip("/")
    if not normalized or ".." in PurePosixPath(normalized).parts:
        raise OssImportScopeError("OSS source is outside the configured import scope")
    return normalized


def oss_import_scopes_for_batch(*, batch: Batch) -> list[dict[str, object]]:
    """Return enabled, admin-managed scopes for this Batch only."""
    db = object_session(batch)
    if db is None:
        raise OssImportScopeError("OSS import scope cannot be resolved")
    rows = (
        db.query(ExternalOssImportScope)
        .filter(
            ExternalOssImportScope.workspace_id == batch.workspace_id,
            ExternalOssImportScope.task_set_id == batch.task_set_id,
            ExternalOssImportScope.is_enabled.is_(True),
        )
        .all()
    )
    scopes = [
        {
            "workspace_id": row.workspace_id,
            "task_set_id": row.task_set_id,
            "bucket": row.bucket,
            "prefixes": list(row.prefixes_json or []),
        }
        for row in rows
    ]
    matches: list[dict[str, object]] = []
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        if int(scope.get("workspace_id") or 0) != batch.workspace_id:
            continue
        task_set_id = int(scope.get("task_set_id") or 0)
        if task_set_id != batch.task_set_id:
            continue
        bucket = str(scope.get("bucket") or "").strip()
        prefixes = [
            _normalized_key(prefix)
            for prefix in (scope.get("prefixes") or [])
            if str(prefix or "").strip()
        ]
        if bucket and prefixes:
            matches.append({"bucket": bucket, "prefixes": prefixes})
    return matches


def require_oss_import_scope(*, batch: Batch, bucket: str, keys: list[str]) -> None:
    """Fail closed unless every requested key belongs to this Batch's scope."""
    normalized_keys = [_normalized_key(key) for key in keys]
    normalized_bucket = str(bucket or "").strip()
    if not normalized_bucket or not normalized_keys:
        raise OssImportScopeError("OSS source is outside the configured import scope")
    for scope in oss_import_scopes_for_batch(batch=batch):
        if scope["bucket"] != normalized_bucket:
            continue
        prefixes = list(scope["prefixes"])
        if prefixes and all(
            any(key == prefix or key.startswith(f"{prefix}/") for prefix in prefixes)
            for key in normalized_keys
        ):
            return
    raise OssImportScopeError("OSS source is outside the configured import scope")
