"""Data package list query and pending package adjustment service."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session, selectinload

from data.database import Episode, PersonnelProfile, WorkspacePersonnelProfile
from data.models.collection_config import CollectionLabel
from data.models.collection_core import CollectionProject, CollectionTask, CollectionTaskLabel
from data.models.data_package import (
    DATA_PACKAGE_STATUSES,
    DataPackage,
    PackageIntakeReview,
)
from data.security.audit import add_transaction_audit
from data.services.collection_tasks import ArchivedCollectionProjectError
from data.utils.formatting import format_api_datetime

_DURATION_QUANTUM = Decimal("0.01")
_MAX_DURATION = Decimal("99999999.99")


class AssignmentLockedError(ValueError):
    """Raised when structural adjustments are attempted on a package with locked assignment state."""


class PackageStateConflictError(ValueError):
    """Raised when data package current state does not support the requested operation."""


def _validate_duration(value: Decimal) -> Decimal:
    duration = Decimal(value)
    if (
        not duration.is_finite()
        or duration <= 0
        or duration > _MAX_DURATION
        or duration != duration.quantize(_DURATION_QUANTUM)
    ):
        raise ValueError("target_duration_hours must fit NUMERIC(10,2) and be positive")
    return duration


def _require_task(
    db: Session,
    *,
    workspace_id: int,
    collection_task_id: int,
) -> CollectionTask:
    task = (
        db.query(CollectionTask)
        .filter(
            CollectionTask.id == collection_task_id,
            CollectionTask.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if task is None:
        raise LookupError("collection task does not exist in this workspace")
    return task


def _require_enabled_project_for_update(
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
        raise ArchivedCollectionProjectError(
            "archived collection project cannot accept new data packages"
        )
    return project


def _require_task_package(
    db: Session,
    *,
    workspace_id: int,
    collection_task_id: int,
    data_package_id: int,
) -> DataPackage:
    package = (
        db.query(DataPackage)
        .filter(
            DataPackage.id == data_package_id,
            DataPackage.workspace_id == workspace_id,
            DataPackage.collection_task_id == collection_task_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if package is None:
        raise LookupError("data package does not exist in this collection task")
    if package.status != "pending_assignment":
        raise AssignmentLockedError("assignment_locked: data package can no longer be adjusted")
    return package


def _require_workspace_package(
    db: Session,
    *,
    workspace_id: int,
    data_package_id: int,
) -> DataPackage:
    package = (
        db.query(DataPackage)
        .filter(
            DataPackage.id == data_package_id,
            DataPackage.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if package is None:
        raise LookupError("data package does not exist in this workspace")
    return package


def _require_active_workspace_collector(
    db: Session,
    *,
    workspace_id: int,
    collector_id: int,
) -> PersonnelProfile:
    collector = (
        db.query(PersonnelProfile)
        .join(
            WorkspacePersonnelProfile,
            WorkspacePersonnelProfile.personnel_profile_id == PersonnelProfile.id,
        )
        .filter(
            PersonnelProfile.id == collector_id,
            PersonnelProfile.is_active.is_(True),
            WorkspacePersonnelProfile.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if collector is None:
        raise ValueError("collector must exist, be active, and belong to the package workspace")
    return collector


def build_offline_manifest(db: Session, *, package: DataPackage) -> dict[str, object]:
    """Construct immutable package assignment manifest consumed by offline collectors."""
    if package.status in {"pending_assignment", "voided"}:
        raise PackageStateConflictError(
            "offline manifest is unavailable for the current package status"
        )
    task = db.get(CollectionTask, package.collection_task_id)
    if task is None:
        raise LookupError("collection task does not exist")
    return {
        "schema_version": 1,
        "package_uid": package.package_uid,
        "workspace_id": package.workspace_id,
        "collection_project_id": package.collection_project_id,
        "collection_task_id": package.collection_task_id,
        "target_duration_hours": f"{Decimal(package.target_duration_hours):.2f}",
        "collector_id": package.operator_collector_id,
        "responsible_collector_id": package.responsible_collector_id,
        "operator_collector_id": package.operator_collector_id,
        "device_model_id": task.device_model_id,
        "assigned_at": format_api_datetime(package.assigned_at),
    }


def assign_data_package(
    db: Session,
    *,
    workspace_id: int,
    data_package_id: int,
    responsible_collector_id: int,
    operator_collector_id: int,
) -> tuple[DataPackage, dict[str, object]]:
    """Assign responsible person and operator roles for data package and generate offline configuration manifest."""
    _require_active_workspace_collector(
        db,
        workspace_id=workspace_id,
        collector_id=responsible_collector_id,
    )
    _require_active_workspace_collector(
        db,
        workspace_id=workspace_id,
        collector_id=operator_collector_id,
    )
    if responsible_collector_id != operator_collector_id:
        raise ValueError("one_collector_per_package_required")
    assigned_at = datetime.utcnow()
    assigned_id = db.execute(
        update(DataPackage)
        .where(
            DataPackage.id == data_package_id,
            DataPackage.workspace_id == workspace_id,
            DataPackage.status == "pending_assignment",
        )
        .values(
            responsible_collector_id=responsible_collector_id,
            operator_collector_id=operator_collector_id,
            assigned_at=assigned_at,
            status="assigned",
        )
        .returning(DataPackage.id)
    ).scalar_one_or_none()
    if assigned_id is None:
        _require_workspace_package(
            db,
            workspace_id=workspace_id,
            data_package_id=data_package_id,
        )
        raise AssignmentLockedError("assignment_locked: data package is already assigned")
    package = db.get(DataPackage, assigned_id)
    if package is None:
        raise LookupError("assigned data package no longer exists")
    return package, build_offline_manifest(db, package=package)


def get_offline_manifest(
    db: Session,
    *,
    workspace_id: int,
    data_package_id: int,
) -> dict[str, object]:
    """Get offline collection manifest for specified data package."""
    package = _require_workspace_package(
        db,
        workspace_id=workspace_id,
        data_package_id=data_package_id,
    )
    return build_offline_manifest(db, package=package)


def void_data_package(
    db: Session,
    *,
    workspace_id: int,
    data_package_id: int,
) -> DataPackage:
    """Void an assigned data package before upload begins."""
    changed = db.execute(
        update(DataPackage)
        .where(
            DataPackage.id == data_package_id,
            DataPackage.workspace_id == workspace_id,
            DataPackage.status.in_(("assigned", "pending_upload")),
        )
        .values(status="voided", updated_at=datetime.utcnow())
        .returning(DataPackage.id)
    ).scalar_one_or_none()
    package = _require_workspace_package(
        db, workspace_id=workspace_id, data_package_id=data_package_id
    )
    if changed is None:
        raise PackageStateConflictError(
            f"data package in status {package.status!r} cannot be voided"
        )
    db.flush()
    return package


def list_data_packages(
    db: Session,
    *,
    workspace_id: int,
    collection_project_id: int | list[int] | tuple[int, ...] | None = None,
    collection_task_id: int | None = None,
    status: str | None = None,
    operator_collector_id: int | None = None,
    collection_device_id: int | None = None,
    purpose_label_id: int | None = None,
    scene_label_id: int | None = None,
    modality_label_id: int | None = None,
    training_label_id: int | None = None,
    created_by_user_id: int | None = None,
    upload_completed_from: datetime | None = None,
    upload_completed_to: datetime | None = None,
    page: int | None = None,
    size: int = 50,
    with_total: bool = False,
) -> list[DataPackage] | tuple[list[DataPackage], int]:
    """Query list of matching data packages under workspace."""
    if status is not None and status not in DATA_PACKAGE_STATUSES:
        raise ValueError(f"unsupported data package status: {status}")
    query = db.query(DataPackage).filter(DataPackage.workspace_id == workspace_id)
    if collection_project_id is not None:
        project_ids = (
            collection_project_id
            if isinstance(collection_project_id, (list, tuple))
            else [collection_project_id]
        )
        project_ids = [int(project_id) for project_id in project_ids if int(project_id) > 0]
        if project_ids:
            query = query.filter(DataPackage.collection_project_id.in_(project_ids))
    if collection_task_id is not None:
        query = query.filter(DataPackage.collection_task_id == collection_task_id)
    if status is not None:
        query = query.filter(DataPackage.status == status)
    if operator_collector_id is not None:
        query = query.filter(DataPackage.operator_collector_id == operator_collector_id)
    if created_by_user_id is not None:
        query = query.filter(DataPackage.created_by_user_id == created_by_user_id)
    if upload_completed_from is not None:
        query = query.filter(DataPackage.upload_completed_at >= upload_completed_from)
    if upload_completed_to is not None:
        query = query.filter(DataPackage.upload_completed_at <= upload_completed_to)
    if collection_device_id is not None:
        query = query.filter(DataPackage.collection_device_id == collection_device_id)
    for category, label_id in (
        ("purpose", purpose_label_id),
        ("scene", scene_label_id),
        ("modality", modality_label_id),
        ("training", training_label_id),
    ):
        if label_id is not None:
            task_ids = (
                db.query(CollectionTaskLabel.collection_task_id)
                .join(
                    CollectionLabel, CollectionLabel.id == CollectionTaskLabel.collection_label_id
                )
                .filter(CollectionLabel.id == label_id, CollectionLabel.category == category)
            )
            query = query.filter(DataPackage.collection_task_id.in_(task_ids))
    total = query.count() if with_total else None
    query = query.options(selectinload(DataPackage.created_by)).order_by(
        DataPackage.created_at.desc(), DataPackage.id.desc()
    )
    if page is not None:
        query = query.offset((page - 1) * size).limit(size)
    rows = query.all()
    return (rows, total) if with_total else rows


def get_data_package_detail(
    db: Session,
    *,
    workspace_id: int,
    data_package_id: int,
) -> tuple[DataPackage, list[Episode], PackageIntakeReview | None]:
    """Load a single workspace data package with scoped Episodes and intake review records."""
    package = _require_workspace_package(
        db,
        workspace_id=workspace_id,
        data_package_id=data_package_id,
    )
    episodes = (
        db.query(Episode)
        .filter(
            Episode.workspace_id == workspace_id,
            Episode.data_package_id == data_package_id,
        )
        .order_by(Episode.id.asc())
        .all()
    )
    intake_review = (
        db.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == data_package_id)
        .order_by(
            PackageIntakeReview.reviewed_at.desc(),
            PackageIntakeReview.id.desc(),
        )
        .limit(1)
        .one_or_none()
    )
    return package, episodes, intake_review


def adjust_pending_packages(
    db: Session,
    *,
    workspace_id: int,
    collection_task_id: int,
    operations: list[dict[str, object]],
) -> list[DataPackage]:
    """Perform atomic batch adjustments (add, delete, update target duration) on pending assignment packages."""
    task = _require_task(
        db,
        workspace_id=workspace_id,
        collection_task_id=collection_task_id,
    )
    if any(operation["op"] == "add" for operation in operations):
        _require_enabled_project_for_update(
            db,
            workspace_id=workspace_id,
            collection_project_id=task.collection_project_id,
        )
    # Serialize structural edits with assignment on the same package set.
    db.query(DataPackage.id).filter(DataPackage.collection_task_id == task.id).order_by(
        DataPackage.id
    ).with_for_update().all()
    for operation in operations:
        op = operation["op"]
        if op == "add":
            db.add(
                DataPackage(
                    package_uid=f"pkg_{uuid4().hex}",
                    workspace_id=task.workspace_id,
                    collection_project_id=task.collection_project_id,
                    collection_task_id=task.id,
                    status="pending_assignment",
                    target_duration_hours=_validate_duration(
                        Decimal(operation["target_duration_hours"])
                    ),
                )
            )
        elif op == "split":
            package = _require_task_package(
                db,
                workspace_id=workspace_id,
                collection_task_id=task.id,
                data_package_id=int(operation["data_package_id"]),
            )
            durations = [_validate_duration(Decimal(v)) for v in operation["durations"]]
            if len(durations) < 2 or sum(durations) != package.target_duration_hours:
                raise ValueError("split_duration_must_be_preserved")
            package.target_duration_hours = durations[0]
            for duration in durations[1:]:
                db.add(
                    DataPackage(
                        package_uid=f"pkg_{uuid4().hex}",
                        workspace_id=workspace_id,
                        collection_project_id=task.collection_project_id,
                        collection_task_id=task.id,
                        status="pending_assignment",
                        target_duration_hours=duration,
                        supplement_for_package_id=package.supplement_for_package_id,
                        supplement_reason=package.supplement_reason,
                        created_by_user_id=package.created_by_user_id,
                    )
                )
        elif op == "merge":
            ids = sorted({int(v) for v in operation["data_package_ids"]})
            if len(ids) < 2:
                raise ValueError("merge_requires_two_packages")
            packages = [
                _require_task_package(
                    db, workspace_id=workspace_id, collection_task_id=task.id, data_package_id=i
                )
                for i in ids
            ]
            if len({(p.supplement_for_package_id, p.supplement_reason) for p in packages}) != 1:
                raise ValueError("cannot_merge_different_supplement_origins")
            packages[0].target_duration_hours = _validate_duration(
                sum(p.target_duration_hours for p in packages)
            )
            for package in packages[1:]:
                db.delete(package)
        elif op == "delete":
            package = _require_task_package(
                db,
                workspace_id=workspace_id,
                collection_task_id=task.id,
                data_package_id=int(operation["data_package_id"]),
            )
            db.delete(package)
        elif op == "resize":
            package = _require_task_package(
                db,
                workspace_id=workspace_id,
                collection_task_id=task.id,
                data_package_id=int(operation["data_package_id"]),
            )
            package.target_duration_hours = _validate_duration(
                Decimal(operation["target_duration_hours"])
            )
        else:
            raise ValueError(f"unsupported package adjustment operation: {op}")
        db.flush()
    return list_data_packages(
        db,
        workspace_id=workspace_id,
        collection_task_id=task.id,
    )


def create_supplement_package(
    db: Session,
    *,
    workspace_id: int,
    source_package_id: int,
    target_duration_hours: Decimal,
    reason: str,
    actor_id: int,
    client_request_id: str,
) -> DataPackage:
    """Create an ordinary unassigned package in the original task, without changing its target."""
    import hashlib

    source = _require_workspace_package(
        db, workspace_id=workspace_id, data_package_id=source_package_id
    )
    _require_enabled_project_for_update(
        db, workspace_id=workspace_id, collection_project_id=source.collection_project_id
    )
    source = db.query(DataPackage).filter(DataPackage.id == source.id).with_for_update().one()
    if (
        not db.query(PackageIntakeReview.id)
        .filter(PackageIntakeReview.data_package_id == source.id)
        .first()
    ):
        raise PackageStateConflictError("supplement_requires_final_intake")
    duration = _validate_duration(target_duration_hours)
    reason = reason.strip()
    if not reason:
        raise ValueError("supplement_reason_required")
    uid = (
        "sup_"
        + hashlib.sha256(f"{workspace_id}:{actor_id}:{client_request_id}".encode()).hexdigest()[:48]
    )
    package = db.query(DataPackage).filter(DataPackage.package_uid == uid).one_or_none()
    if package is not None:
        if (
            package.supplement_for_package_id != source.id
            or package.target_duration_hours != duration
            or package.supplement_reason != reason
        ):
            raise PackageStateConflictError("supplement_request_conflict")
        return package
    package = DataPackage(
        package_uid=uid,
        workspace_id=workspace_id,
        collection_project_id=source.collection_project_id,
        collection_task_id=source.collection_task_id,
        target_duration_hours=duration,
        status="pending_assignment",
        supplement_for_package_id=source.id,
        supplement_reason=reason,
        created_by_user_id=actor_id,
    )
    db.add(package)
    db.flush()
    add_transaction_audit(
        db,
        "collection.package.supplement",
        actor_id=actor_id,
        workspace_id=workspace_id,
        resource_type="data_package",
        resource_id=package.id,
        detail={"source_package_id": source.id, "reason": reason},
    )
    return package
