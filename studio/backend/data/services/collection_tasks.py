"""Collection task creation, validation, and package progress query service."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from data.models.collection_config import (
    COLLECTION_LABEL_CATEGORIES,
    CollectionDeviceModel,
    CollectionLabel,
)
from data.models.collection_core import CollectionProject, CollectionTask, CollectionTaskLabel
from data.models.data_package import DataPackage
from data.services.resource_names import normalized_name

_DURATION_QUANTUM = Decimal("0.01")
_MAX_DURATION = Decimal("99999999.99")
MAX_PACKAGES_PER_TASK = 10_000


class ArchivedCollectionProjectError(ValueError):
    """Raised when attempting to create tasks or add packages under an archived project."""


def split_package_durations(target: Decimal, default_pkg: Decimal) -> list[Decimal]:
    """Split task target duration into multiple full package durations and an optional remainder duration."""
    if not target.is_finite() or not default_pkg.is_finite():
        raise ValueError("durations must fit NUMERIC(10,2)")
    if target <= 0 or default_pkg <= 0:
        raise ValueError("durations must be positive")
    if (
        target > _MAX_DURATION
        or default_pkg > _MAX_DURATION
        or target != target.quantize(_DURATION_QUANTUM)
        or default_pkg != default_pkg.quantize(_DURATION_QUANTUM)
    ):
        raise ValueError("durations must fit NUMERIC(10,2)")
    full, remainder = divmod(target, default_pkg)
    if full + bool(remainder) > MAX_PACKAGES_PER_TASK:
        raise ValueError(f"a task may generate at most {MAX_PACKAGES_PER_TASK} packages")
    durations = [default_pkg] * int(full)
    if remainder > 0:
        durations.append(remainder)
    if not durations:
        raise ValueError("no packages generated")
    return durations


def _require_enabled_project(
    db: Session,
    *,
    workspace_id: int,
    collection_project_id: int,
) -> CollectionProject:
    project = (
        db.query(CollectionProject)
        .filter(
            CollectionProject.id == collection_project_id,
            CollectionProject.workspace_id == workspace_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if project is None:
        raise LookupError("collection project does not exist in this workspace")
    if project.status != "enabled":
        raise ArchivedCollectionProjectError("archived collection project cannot accept new tasks")
    return project


def _validate_device_model(db: Session, device_model_id: int | None) -> None:
    if device_model_id is None:
        return
    device_model = db.get(CollectionDeviceModel, device_model_id)
    if device_model is None or not device_model.is_active:
        raise ValueError("active collection device model does not exist")


def _active_labels(db: Session, label_ids: list[int]) -> list[CollectionLabel]:
    unique_ids = list(dict.fromkeys(label_ids))
    if not unique_ids:
        return []
    labels = db.query(CollectionLabel).filter(CollectionLabel.id.in_(unique_ids)).all()
    valid_by_id = {
        label.id: label
        for label in labels
        if label.is_active and label.category in COLLECTION_LABEL_CATEGORIES
    }
    if set(valid_by_id) != set(unique_ids):
        raise ValueError("all collection labels must exist, be active, and have a valid category")
    return [valid_by_id[label_id] for label_id in unique_ids]


def create_collection_task(
    db: Session,
    *,
    workspace_id: int,
    collection_project_id: int,
    name: str,
    target_duration_hours: Decimal,
    default_package_duration_hours: Decimal = Decimal("2.00"),
    description: str = "",
    sop_text: str = "",
    device_model_id: int | None = None,
    label_ids: list[int],
    created_by_user_id: int | None,
) -> CollectionTask:
    """Create offline collection task and atomically stage initial pending data packages."""
    _require_enabled_project(
        db,
        workspace_id=workspace_id,
        collection_project_id=collection_project_id,
    )
    _validate_device_model(db, device_model_id)
    labels = _active_labels(db, label_ids)
    durations = split_package_durations(
        Decimal(target_duration_hours),
        Decimal(default_package_duration_hours),
    )
    task = CollectionTask(
        workspace_id=workspace_id,
        collection_project_id=collection_project_id,
        name=normalized_name(name),
        description=description.strip(),
        target_duration_hours=target_duration_hours,
        default_package_duration_hours=default_package_duration_hours,
        capture_mode="offline",
        sop_text=sop_text.strip(),
        device_model_id=device_model_id,
        created_by_user_id=created_by_user_id,
    )
    db.add(task)
    db.flush()
    db.add_all(
        [
            CollectionTaskLabel(
                collection_task_id=task.id,
                collection_label_id=label.id,
            )
            for label in labels
        ]
    )
    db.add_all(
        [
            DataPackage(
                package_uid=f"pkg_{uuid4().hex}",
                workspace_id=workspace_id,
                collection_project_id=collection_project_id,
                collection_task_id=task.id,
                status="pending_assignment",
                target_duration_hours=duration,
            )
            for duration in durations
        ]
    )
    db.flush()
    return task


def list_collection_tasks_with_progress(
    db: Session,
    *,
    workspace_id: int,
    collection_project_id: int | None = None,
) -> list[tuple[CollectionTask, int, int, Decimal, int]]:
    """Query workspace task list and aggregate package progress."""
    query = (
        db.query(
            CollectionTask,
            func.count(DataPackage.id).label("package_count"),
            func.sum(
                case(
                    (DataPackage.responsible_collector_id.is_not(None), 1),
                    else_=0,
                )
            ).label("assigned_count"),
            func.sum(case((DataPackage.status == "pending_assignment", 1), else_=0)).label(
                "pending_assignment_count"
            ),
            func.coalesce(
                func.sum(DataPackage.intake_valid_duration_hours),
                Decimal("0.00"),
            ).label("intake_valid_duration_hours"),
        )
        .outerjoin(DataPackage, DataPackage.collection_task_id == CollectionTask.id)
        .filter(CollectionTask.workspace_id == workspace_id)
        .group_by(CollectionTask.id)
    )
    if collection_project_id is not None:
        query = query.filter(CollectionTask.collection_project_id == collection_project_id)
    rows = query.order_by(CollectionTask.created_at.desc(), CollectionTask.id.desc()).all()
    return [
        (
            task,
            int(package_count),
            int(assigned_count or 0),
            Decimal(intake_duration),
            int(pending or 0),
        )
        for task, package_count, assigned_count, pending, intake_duration in rows
    ]


def task_capture_periods(
    db: Session,
    *,
    task_ids: list[int],
) -> dict[int, tuple[datetime, datetime]]:
    """Earliest and latest package capture start per task, from package capture facts."""
    if not task_ids:
        return {}
    rows = (
        db.query(
            DataPackage.collection_task_id,
            func.min(DataPackage.captured_started_at),
            func.max(DataPackage.captured_started_at),
        )
        .filter(
            DataPackage.collection_task_id.in_(task_ids),
            DataPackage.captured_started_at.is_not(None),
        )
        .group_by(DataPackage.collection_task_id)
        .all()
    )
    return {task_id: (first, last) for task_id, first, last in rows}


def get_collection_task(
    db: Session,
    *,
    workspace_id: int,
    task_id: int,
) -> CollectionTask:
    """Get collection task record in specified workspace."""
    task = (
        db.query(CollectionTask)
        .filter(
            CollectionTask.id == task_id,
            CollectionTask.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if task is None:
        raise LookupError("collection task does not exist")
    return task


OFFLINE_MANIFEST_COLUMNS = (
    "package_uid",
    "collection_project",
    "collection_task",
    "modality",
    "target_duration_hours",
    "expected_files",
    "required_metadata",
    "upload_status",
    "desensitization_status",
    "manifest_revision",
)


def offline_manifest_rows(
    db: Session, *, task_id: int, workspace_id: int
) -> list[dict[str, object]]:
    """Package level rows for the offline collection manifest contract."""

    task = db.get(CollectionTask, task_id)
    if task is None or int(task.workspace_id) != int(workspace_id):
        raise LookupError("collection task does not exist")
    project = db.get(CollectionProject, task.collection_project_id)
    packages = (
        db.query(DataPackage)
        .filter(DataPackage.collection_task_id == task.id)
        .order_by(DataPackage.id.asc())
        .all()
    )
    revision = f"{task.id}:{task.updated_at.isoformat() if task.updated_at else ''}"
    rows: list[dict[str, object]] = []
    for package in packages:
        facts = package.qrdf_facts_json or {}
        declaration = facts.get("desensitization") or {}
        rows.append(
            {
                "package_uid": package.package_uid,
                "collection_project": project.name if project is not None else "",
                "collection_task": task.name,
                "modality": task.capture_mode or "ego",
                "target_duration_hours": str(package.target_duration_hours or "0"),
                "expected_files": "*.mcap,metadata.json",
                "required_metadata": "collector,device_sn,captured_at",
                "upload_status": package.status,
                "desensitization_status": str(declaration.get("status") or "unknown"),
                "manifest_revision": revision,
            }
        )
    return rows


def list_task_packages(db: Session, *, task_id: int) -> list[DataPackage]:
    """List all data packages under specified collection task."""
    return (
        db.query(DataPackage)
        .filter(DataPackage.collection_task_id == task_id)
        .order_by(DataPackage.id.asc())
        .all()
    )
