"""Data batches: candidate listing, batch creation locking, and one-time package attribution."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import Episode, User
from data.models.collection_config import CollectionLabel
from data.models.collection_core import CollectionTaskLabel
from data.models.data_batch import (
    GOVERNANCE_STAGES,
    DataBatch,
    DataBatchEpisode,
    DataBatchGovernanceRun,
    DataBatchLabel,
    DataBatchPackage,
    DataBatchStageRun,
)
from data.models.data_package import DataPackage
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.collection_intake_review import _episode_duration_hours
from data.services.episode_admission import eligible_package_episodes
from data.services.resource_names import is_constraint_conflict

_BATCH_LABEL_CATEGORIES = frozenset({"scene", "purpose", "modality", "training"})
_BATCH_NAME_CONSTRAINT = "uq_data_batches_workspace_normalized_name"
_PACKAGE_MEMBERSHIP_CONSTRAINT = "uq_data_batch_packages_batch_package"
_EPISODE_MEMBERSHIP_CONSTRAINT = "uq_data_batch_episodes_episode"
_ANNOTATOR_ROLE = "annotator"
_REVIEWER_ROLE = "auditor"


class BatchLockedError(ValueError):
    """Raised when a caller attempts to mutate a locked batch definition."""

    def __init__(self, message: str = "batch_locked"):
        super().__init__(message)


class BatchConflictError(ValueError):
    """Raised for create-time conflicts such as already-batched packages."""


def schedule_post_batch_pipeline(batch_id: int, *, sync: bool = False) -> None:
    """Continue after batch create: governance, then assignment or assets.

    Prefer async Celery (``sync=False``). Tests may pass ``sync=True``.
    """
    if sync:
        from data.database import SessionLocal
        from data.services.governance_runs import run_governance

        db = SessionLocal()
        try:
            run_governance(db, batch_id=batch_id, sync=True)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return

    from data.tasks.governance_tasks import execute_governance_run

    execute_governance_run.delay(batch_id)


def list_batch_candidates(
    db: Session,
    workspace_id: int,
    filters: dict[str, Any] | None = None,
) -> list[DataPackage]:
    """Return intake-approved packages not yet belonging to any data batch."""
    filters = filters or {}
    query = db.query(DataPackage).filter(
        DataPackage.workspace_id == workspace_id,
        DataPackage.status == "intake_approved",
        ~DataPackage.id.in_(db.query(DataBatchPackage.data_package_id)),
    )
    project_id = filters.get("collection_project_id")
    if project_id is not None:
        query = query.filter(DataPackage.collection_project_id == int(project_id))
    task_id = filters.get("collection_task_id")
    if task_id is not None:
        query = query.filter(DataPackage.collection_task_id == int(task_id))
    responsible_id = filters.get("responsible_collector_id")
    if responsible_id is not None:
        query = query.filter(DataPackage.responsible_collector_id == int(responsible_id))
    operator_id = filters.get("operator_collector_id")
    if operator_id is not None:
        query = query.filter(DataPackage.operator_collector_id == int(operator_id))
    device_id = filters.get("collection_device_id")
    if device_id is not None:
        query = query.filter(DataPackage.collection_device_id == int(device_id))
    label_ids = [int(label_id) for label_id in (filters.get("label_ids") or []) if label_id]
    # Package inherits scene/purpose/modality/training via its collection task labels.
    for label_id in label_ids:
        query = query.filter(
            DataPackage.collection_task_id.in_(
                db.query(CollectionTaskLabel.collection_task_id).filter(
                    CollectionTaskLabel.collection_label_id == label_id
                )
            )
        )
    packages = query.order_by(DataPackage.id.asc()).all()
    occupied = {row[0] for row in db.query(DataBatchEpisode.episode_id).all()}
    return [
        package
        for package in packages
        if eligible_package_episodes(
            db,
            workspace_id=workspace_id,
            data_package_id=package.id,
            occupied_episode_ids=occupied,
        )
    ]


def list_data_batches(db: Session, *, workspace_id: int) -> list[DataBatch]:
    return (
        db.query(DataBatch)
        .filter(DataBatch.workspace_id == workspace_id)
        .order_by(DataBatch.created_at.desc(), DataBatch.id.desc())
        .all()
    )


def get_data_batch(db: Session, *, workspace_id: int, data_batch_id: int) -> DataBatch:
    batch = (
        db.query(DataBatch)
        .filter(
            DataBatch.id == data_batch_id,
            DataBatch.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if batch is None:
        raise LookupError("data batch does not exist in this workspace")
    return batch


def _require_active_users(db: Session, *, user_ids: list[int]) -> list[User]:
    """Annotation and review are not workspace-scoped, so any active user of the role may work."""
    if not user_ids:
        return []
    unique_ids = sorted(set(user_ids))
    users = db.query(User).filter(User.id.in_(unique_ids), User.is_active.is_(True)).all()
    found = {user.id: user for user in users}
    missing = [user_id for user_id in unique_ids if user_id not in found]
    if missing:
        raise ValueError(f"users not found: {missing}")
    return [found[user_id] for user_id in unique_ids]


def _validate_labels(db: Session, label_ids: list[int]) -> list[CollectionLabel]:
    if not label_ids:
        return []
    unique_ids = sorted(set(label_ids))
    if len(unique_ids) != len(label_ids):
        raise ValueError("label_ids must be unique")
    labels = (
        db.query(CollectionLabel)
        .filter(CollectionLabel.id.in_(unique_ids), CollectionLabel.is_active.is_(True))
        .all()
    )
    found = {label.id: label for label in labels}
    missing = [label_id for label_id in unique_ids if label_id not in found]
    if missing:
        raise ValueError(f"labels not found or inactive: {missing}")
    for label in labels:
        if label.category not in _BATCH_LABEL_CATEGORIES:
            raise ValueError("data batch labels must be scene/purpose/modality/training")
    return [found[label_id] for label_id in unique_ids]


def _claim_packages(
    db: Session, *, workspace_id: int, data_package_ids: list[int]
) -> tuple[list[DataPackage], list[tuple[DataPackage, Episode, EpisodeAdmissionFact]]]:
    if not data_package_ids:
        raise ValueError("data_package_ids must not be empty")
    unique_ids = sorted(set(data_package_ids))
    if len(unique_ids) != len(data_package_ids):
        raise ValueError("data_package_ids must be unique")

    packages = (
        db.query(DataPackage)
        .filter(
            DataPackage.workspace_id == workspace_id,
            DataPackage.id.in_(unique_ids),
        )
        .order_by(DataPackage.id.asc())
        .with_for_update()
        .all()
    )
    found_ids = {package.id for package in packages}
    missing = [package_id for package_id in unique_ids if package_id not in found_ids]
    if missing:
        raise LookupError(f"data packages not found: {missing}")

    for package in packages:
        already_batched = (
            db.query(DataBatchPackage.id)
            .filter(DataBatchPackage.data_package_id == package.id)
            .first()
            is not None
        )
        if already_batched:
            raise BatchConflictError("already_batched")
        if package.status != "intake_approved":
            raise BatchConflictError("package_not_intake_approved")

    # Acquire all Episode locks globally by ID, even when IDs from different
    # packages interleave. Workers also lock Episode before changing facts.
    db.query(Episode.id).filter(
        Episode.workspace_id == workspace_id,
        Episode.data_package_id.in_(unique_ids),
    ).order_by(Episode.id.asc()).with_for_update().all()
    occupied = {
        row[0]
        for row in db.query(DataBatchEpisode.episode_id)
        .filter(DataBatchEpisode.data_package_id.in_(unique_ids))
        .order_by(DataBatchEpisode.episode_id.asc())
        .with_for_update()
        .all()
    }
    members: list[tuple[DataPackage, Episode, EpisodeAdmissionFact]] = []
    for package in packages:
        available = eligible_package_episodes(
            db,
            workspace_id=workspace_id,
            data_package_id=package.id,
            occupied_episode_ids=occupied,
            for_update=False,
        )
        if not available:
            already_member = eligible_package_episodes(
                db,
                workspace_id=workspace_id,
                data_package_id=package.id,
                occupied_episode_ids=(),
            )
            if already_member:
                raise BatchConflictError("already_batched_episode")
            raise BatchConflictError("no_eligible_episodes")
        members.extend((package, episode, fact) for episode, fact in available)
        occupied.update(episode.id for episode, _fact in available)

    result = db.execute(
        update(DataPackage)
        .where(
            DataPackage.workspace_id == workspace_id,
            DataPackage.id.in_(unique_ids),
            DataPackage.status == "intake_approved",
            ~DataPackage.id.in_(db.query(DataBatchPackage.data_package_id)),
        )
        .values(status="batched")
    )
    if result.rowcount != len(unique_ids):
        raise BatchConflictError("package_state_changed")

    refreshed = (
        db.query(DataPackage)
        .filter(DataPackage.id.in_(unique_ids))
        .order_by(DataPackage.id.asc())
        .all()
    )
    refreshed_by_id = {package.id: package for package in refreshed}
    return refreshed, [
        (refreshed_by_id[package.id], episode, fact) for package, episode, fact in members
    ]


def _create_governance_run(db: Session, batch: DataBatch) -> DataBatchGovernanceRun:
    run = DataBatchGovernanceRun(data_batch_id=batch.id, status="queued")
    db.add(run)
    db.flush()
    enabled = {
        "integrity": batch.integrity_check_enabled,
        "quality": batch.quality_check_enabled,
        "compliance": batch.compliance_check_enabled,
    }
    for stage in GOVERNANCE_STAGES:
        db.add(
            DataBatchStageRun(
                run_id=run.id,
                stage=stage,
                status="queued" if enabled[stage] else "skipped",
                attempt=1,
                result_json={},
                error_message="",
            )
        )
    db.flush()
    return run


def create_data_batch(
    db: Session,
    *,
    workspace_id: int,
    name: str,
    data_package_ids: list[int],
    label_ids: list[int],
    integrity_check_enabled: bool,
    quality_check_enabled: bool,
    compliance_check_enabled: bool,
    annotation_enabled: bool,
    annotator_user_ids: list[int],
    reviewer_user_id: int | None,
    review_mode: str,
    created_by_user_id: int | None,
) -> tuple[DataBatch, bool]:
    """Create a locked data batch and claim packages atomically.

    Returns ``(batch, should_schedule_pipeline)``. Callers must commit first,
    then invoke ``schedule_post_batch_pipeline`` when the flag is True.
    """
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValueError("name is required")
    if review_mode != "single":
        raise ValueError("review_mode must be single")

    if annotation_enabled:
        if not annotator_user_ids:
            raise ValueError("annotator_user_ids required when annotation_enabled")
        if reviewer_user_id is None:
            raise ValueError("reviewer_user_id required when annotation_enabled")
    else:
        if annotator_user_ids:
            raise ValueError("annotator_user_ids must be empty when annotation disabled")
        if reviewer_user_id is not None:
            raise ValueError("reviewer_user_id must be null when annotation disabled")

    annotators = _require_active_users(db, user_ids=list(annotator_user_ids))
    for user in annotators:
        if user.role != _ANNOTATOR_ROLE:
            raise ValueError("annotator_user_ids must reference annotator users")

    reviewer: User | None = None
    if reviewer_user_id is not None:
        reviewer = _require_active_users(db, user_ids=[reviewer_user_id])[0]
        if reviewer.role != _REVIEWER_ROLE:
            raise ValueError("reviewer_user_id must reference a reviewer user")

    labels = _validate_labels(db, list(label_ids))
    packages, episode_members = _claim_packages(
        db, workspace_id=workspace_id, data_package_ids=list(data_package_ids)
    )

    any_governance = integrity_check_enabled or quality_check_enabled or compliance_check_enabled
    batch = DataBatch(
        workspace_id=workspace_id,
        name=cleaned_name,
        status="governing" if any_governance else "open",
        integrity_check_enabled=integrity_check_enabled,
        quality_check_enabled=quality_check_enabled,
        compliance_check_enabled=compliance_check_enabled,
        annotation_enabled=annotation_enabled,
        review_mode=review_mode,
        annotator_user_ids_json=[user.id for user in annotators],
        reviewer_user_id=reviewer.id if reviewer else None,
        assignment_snapshot_json={},
        episode_count=len(episode_members),
        valid_duration_hours=sum(
            (_episode_duration_hours(episode) for _package, episode, _fact in episode_members),
            Decimal(0),
        ).quantize(Decimal("0.01")),
        created_by_user_id=created_by_user_id,
    )
    db.add(batch)
    try:
        db.flush()
    except IntegrityError as exc:
        if is_constraint_conflict(exc, _BATCH_NAME_CONSTRAINT):
            raise BatchConflictError("data batch name already exists in this workspace") from exc
        raise

    for package in packages:
        db.add(DataBatchPackage(data_batch_id=batch.id, data_package_id=package.id))
    for package, episode, fact in episode_members:
        db.add(
            DataBatchEpisode(
                data_batch_id=batch.id,
                episode_id=episode.id,
                data_package_id=package.id,
                admission_attempt=fact.attempt,
                duration_hours=_episode_duration_hours(episode).quantize(Decimal("0.01")),
            )
        )
    for label in labels:
        db.add(DataBatchLabel(data_batch_id=batch.id, collection_label_id=label.id))
    try:
        db.flush()
    except IntegrityError as exc:
        if is_constraint_conflict(exc, _PACKAGE_MEMBERSHIP_CONSTRAINT):
            raise BatchConflictError("package_already_in_batch") from exc
        if is_constraint_conflict(exc, _EPISODE_MEMBERSHIP_CONSTRAINT):
            raise BatchConflictError("already_batched_episode") from exc
        raise

    if any_governance:
        _create_governance_run(db, batch)

    db.flush()
    db.refresh(batch)
    # Always schedule: governance when enabled, else annotation/asset hooks.
    return batch, True


def reject_batch_definition_change() -> None:
    """Definition is immutable after create; callers map this to HTTP 409."""
    raise BatchLockedError("batch_locked")
