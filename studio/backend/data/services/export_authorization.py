"""Immutable authorization scopes for durable exports."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from data.database import Dataset, ExportJob, Project, QrdfData, User
from data.services.workspace_access import require_actor, require_project_actor, require_qrdf_actor

EXPORT_SCOPE_VERSION = 1


class ExportScopeError(ValueError):
    """An export source set cannot be represented by one authorized scope."""


def build_export_authorization_scope(
    db: Session,
    *,
    qrdf_ids: list[int],
    dataset_id: int | None,
    actor_id: int | None,
) -> dict[str, object]:
    """Validate every export source and return its immutable tenant scope."""
    actor = require_actor(db, actor_id=actor_id)
    dataset = _dataset_for_scope(db, dataset_id)
    scoped_qrdf_ids = _positive_int_list(qrdf_ids, allow_empty=True)
    if not scoped_qrdf_ids:
        raise ExportScopeError("export query has no matched QRDF data")
    qrdf_records = _qrdf_records_for_scope(db, scoped_qrdf_ids, actor_id=actor.id)
    project_ids = {record.project_id for record in qrdf_records}
    if dataset is not None:
        project_ids.add(dataset.project_id)
    if not project_ids:
        raise ExportScopeError("export query has no matched QRDF data")

    projects = _projects_for_scope(db, project_ids)
    workspace_ids = {project.workspace_id for project in projects}
    if len(workspace_ids) != 1:
        raise ExportScopeError("export query spans multiple workspaces")
    for project in projects:
        require_project_actor(db, actor_id=actor.id, project_id=project.id)

    return {
        "version": EXPORT_SCOPE_VERSION,
        "creator_actor_id": actor.id,
        "workspace_ids": sorted(workspace_ids),
        "project_ids": sorted(project_ids),
        "qrdf_ids": sorted({record.id for record in qrdf_records}),
        "dataset_id": dataset.id if dataset is not None else None,
    }


def actor_can_access_export_job(
    db: Session,
    *,
    actor_id: int | None,
    export: ExportJob,
) -> bool:
    """Default deny unless an intact scope and every scoped project is accessible."""
    try:
        actor = require_actor(db, actor_id=actor_id)
        scope = validated_export_authorization_scope(db, export)
        for project_id in scope["project_ids"]:
            require_project_actor(db, actor_id=actor.id, project_id=project_id)
        return True
    except (ExportScopeError, PermissionError, TypeError, ValueError):
        return False


def validated_export_authorization_scope(db: Session, export: ExportJob) -> dict[str, object]:
    """Return an intact export scope, or raise instead of allowing a partial read."""
    return _validated_export_scope(db, export)


def _dataset_for_scope(db: Session, dataset_id: int | None) -> Dataset | None:
    if dataset_id is None:
        return None
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise ExportScopeError("export dataset does not exist")
    return dataset


def _qrdf_records_for_scope(
    db: Session,
    qrdf_ids: list[int],
    *,
    actor_id: int,
) -> list[QrdfData]:
    records: list[QrdfData] = []
    for qrdf_id in sorted(set(qrdf_ids)):
        record = db.get(QrdfData, qrdf_id)
        if record is None:
            raise ExportScopeError("export query references unavailable QRDF data")
        try:
            require_qrdf_actor(db, actor_id=actor_id, qrdf=record)
        except PermissionError as exc:
            try:
                require_project_actor(db, actor_id=actor_id, project_id=record.project_id)
            except PermissionError:
                raise
            except ValueError as project_exc:
                raise ExportScopeError("export project does not exist") from project_exc
            raise ExportScopeError("QRDF task scope is unavailable") from exc
        except ValueError as exc:
            raise ExportScopeError("QRDF task scope is unavailable") from exc
        records.append(record)
    return records


def _projects_for_scope(db: Session, project_ids: set[int]) -> list[Project]:
    projects: list[Project] = []
    for project_id in sorted(project_ids):
        project = db.get(Project, project_id)
        if project is None:
            raise ExportScopeError("export project does not exist")
        projects.append(project)
    return projects


def _validated_export_scope(db: Session, export: ExportJob) -> dict[str, object]:
    raw = export.authorization_scope_json
    if not isinstance(raw, dict) or raw.get("version") != EXPORT_SCOPE_VERSION:
        raise ExportScopeError("export authorization scope is unavailable")
    creator_actor_id = _positive_int(raw.get("creator_actor_id"))
    workspace_ids = _positive_int_list(raw.get("workspace_ids"))
    project_ids = _positive_int_list(raw.get("project_ids"))
    qrdf_ids = _positive_int_list(raw.get("qrdf_ids"))
    dataset_id = _optional_positive_int(raw.get("dataset_id"))
    if creator_actor_id is None or not workspace_ids or not project_ids:
        raise ExportScopeError("export authorization scope is unavailable")
    if db.get(User, creator_actor_id) is None:
        raise ExportScopeError("export authorization scope is unavailable")
    if export.dataset_id not in (None, 0) and dataset_id != export.dataset_id:
        raise ExportScopeError("export authorization scope is unavailable")
    if export.project_id is not None and export.project_id not in project_ids:
        raise ExportScopeError("export authorization scope is unavailable")

    dataset = _dataset_for_scope(db, dataset_id)
    try:
        records = _qrdf_records_for_scope(db, qrdf_ids, actor_id=creator_actor_id)
    except PermissionError as exc:
        raise ExportScopeError("export authorization scope is unavailable") from exc
    expected_project_ids = {record.project_id for record in records}
    if dataset is not None:
        expected_project_ids.add(dataset.project_id)
    if set(project_ids) != expected_project_ids:
        raise ExportScopeError("export authorization scope is unavailable")
    projects = _projects_for_scope(db, expected_project_ids)
    if set(workspace_ids) != {project.workspace_id for project in projects}:
        raise ExportScopeError("export authorization scope is unavailable")
    if len(workspace_ids) != 1:
        raise ExportScopeError("export authorization scope is unavailable")
    return {
        "creator_actor_id": creator_actor_id,
        "workspace_ids": workspace_ids,
        "project_ids": project_ids,
        "qrdf_ids": qrdf_ids,
        "dataset_id": dataset_id,
    }


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    return _positive_int(value)


def _positive_int_list(value: Any, *, allow_empty: bool = False) -> list[int]:
    if not isinstance(value, list):
        raise ExportScopeError("export authorization scope is unavailable")
    parsed = [_positive_int(item) for item in value]
    if any(item is None for item in parsed):
        raise ExportScopeError("export authorization scope is unavailable")
    result = [int(item) for item in parsed if item is not None]
    if len(result) != len(set(result)) or (not allow_empty and not result):
        raise ExportScopeError("export authorization scope is unavailable")
    return result
