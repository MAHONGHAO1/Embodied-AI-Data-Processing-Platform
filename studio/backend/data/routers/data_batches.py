"""Data batches: candidate, creation, listing, and details API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from data.database import get_db
from data.models.collection_config import CollectionLabel
from data.models.data_batch import (
    DataBatch,
    DataBatchGovernanceRun,
    DataBatchLabel,
    DataBatchPackage,
    DataBatchStageRun,
)
from data.models.data_package import DataPackage
from data.security.audit import emit_audit_event
from data.services.collection_access import require_collection_workspace
from data.services.data_batches import (
    BatchConflictError,
    BatchLockedError,
    create_data_batch,
    get_data_batch,
    list_batch_candidates,
    schedule_post_batch_pipeline,
)
from data.services.governance_runs import (
    GovernanceError,
    get_governance_report,
    retry_failed_stage,
)
from data.services.package_list_projection import package_list_extras
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/data-batches", tags=["数据批"])


class CreateDataBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=128)
    data_package_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1)
    label_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    integrity_check_enabled: bool = False
    quality_check_enabled: bool = False
    compliance_check_enabled: bool = False
    annotation_enabled: bool = False
    annotator_user_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    reviewer_user_id: int | None = Field(default=None, gt=0)
    review_mode: Literal["single"] = "single"

    @model_validator(mode="after")
    def validate_unique_ids(self):
        if len(set(self.data_package_ids)) != len(self.data_package_ids):
            raise ValueError("data_package_ids must be unique")
        if len(set(self.label_ids)) != len(self.label_ids):
            raise ValueError("label_ids must be unique")
        if len(set(self.annotator_user_ids)) != len(self.annotator_user_ids):
            raise ValueError("annotator_user_ids must be unique")
        return self


class RetryGovernanceStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    stage: Literal["integrity", "quality", "compliance"]


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _require_workspace(db: Session, *, user: dict, workspace_id: int) -> None:
    try:
        require_collection_workspace(
            db,
            actor_id=_actor_id(user),
            workspace_id=workspace_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _decimal_str(value) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}"


def _package_item(package: DataPackage) -> dict[str, object]:
    return {
        "id": package.id,
        "package_uid": package.package_uid,
        "workspace_id": package.workspace_id,
        "collection_project_id": package.collection_project_id,
        "collection_task_id": package.collection_task_id,
        "status": package.status,
        "target_duration_hours": _decimal_str(package.target_duration_hours),
        "intake_valid_duration_hours": _decimal_str(package.intake_valid_duration_hours),
        "governed_valid_duration_hours": _decimal_str(package.governed_valid_duration_hours),
        "responsible_collector_id": package.responsible_collector_id,
        "operator_collector_id": package.operator_collector_id,
        "collection_device_id": package.collection_device_id,
        "created_at": format_api_datetime(package.created_at),
        "updated_at": format_api_datetime(package.updated_at),
    }


def _batch_item(batch: DataBatch) -> dict[str, object]:
    package_ids = [link.data_package_id for link in batch.packages]
    label_ids = [link.collection_label_id for link in batch.labels]
    return {
        "id": batch.id,
        "workspace_id": batch.workspace_id,
        "name": batch.name,
        "status": batch.status,
        "integrity_check_enabled": batch.integrity_check_enabled,
        "quality_check_enabled": batch.quality_check_enabled,
        "compliance_check_enabled": batch.compliance_check_enabled,
        "annotation_enabled": batch.annotation_enabled,
        "review_mode": batch.review_mode,
        "annotator_user_ids": list(batch.annotator_user_ids_json or []),
        "reviewer_user_id": batch.reviewer_user_id,
        "assignment_snapshot": batch.assignment_snapshot_json or {},
        "episode_count": batch.episode_count,
        "valid_duration_hours": _decimal_str(batch.valid_duration_hours),
        "data_package_ids": package_ids,
        "episode_ids": [link.episode_id for link in batch.episodes],
        "label_ids": label_ids,
        "created_by_user_id": batch.created_by_user_id,
        "created_at": format_api_datetime(batch.created_at),
        "updated_at": format_api_datetime(batch.updated_at),
    }


def _annotation_status(batch: DataBatch) -> str:
    if not batch.annotation_enabled:
        return "disabled"
    if batch.status == "annotating":
        return "annotating"
    if batch.status == "reviewing":
        return "reviewing"
    if batch.status in {"publishing", "published", "no_publishable_asset"}:
        return "completed"
    return "pending"


def _filter_batch_label(query, *, category: str, label_id: int | None):
    if label_id is None:
        return query
    matching_batch_ids = (
        query.session.query(DataBatchLabel.data_batch_id)
        .join(CollectionLabel, CollectionLabel.id == DataBatchLabel.collection_label_id)
        .filter(CollectionLabel.id == label_id, CollectionLabel.category == category)
    )
    return query.filter(DataBatch.id.in_(matching_batch_ids))


def _filter_annotation_status(query, annotation_status: str | None):
    if annotation_status is None:
        return query
    if annotation_status == "disabled":
        return query.filter(DataBatch.annotation_enabled.is_(False))
    query = query.filter(DataBatch.annotation_enabled.is_(True))
    if annotation_status == "annotating":
        return query.filter(DataBatch.status == "annotating")
    if annotation_status == "reviewing":
        return query.filter(DataBatch.status == "reviewing")
    if annotation_status == "completed":
        return query.filter(
            DataBatch.status.in_(("publishing", "published", "no_publishable_asset"))
        )
    return query.filter(DataBatch.status.in_(("open", "governing", "failed")))


@router.get("/candidates")
def get_batch_candidates(
    workspace_id: int = Query(..., gt=0),
    collection_project_id: int | None = Query(default=None, gt=0),
    collection_task_id: int | None = Query(default=None, gt=0),
    responsible_collector_id: int | None = Query(default=None, gt=0),
    operator_collector_id: int | None = Query(default=None, gt=0),
    collection_device_id: int | None = Query(default=None, gt=0),
    label_ids: list[int] | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    filters = {
        "collection_project_id": collection_project_id,
        "collection_task_id": collection_task_id,
        "responsible_collector_id": responsible_collector_id,
        "operator_collector_id": operator_collector_id,
        "collection_device_id": collection_device_id,
        "label_ids": label_ids or [],
    }
    packages = list_batch_candidates(db, workspace_id, filters)
    return success({"items": [_package_item(package) for package in packages]})


@router.get("")
def get_data_batches(
    workspace_id: int = Query(..., gt=0),
    collection_project_id: int | None = Query(default=None, gt=0),
    scene_label_id: int | None = Query(default=None, gt=0),
    purpose_label_id: int | None = Query(default=None, gt=0),
    training_label_id: int | None = Query(default=None, gt=0),
    modality_label_id: int | None = Query(default=None, gt=0),
    integrity_status: Literal["queued", "running", "passed", "skipped", "failed"] | None = Query(
        default=None
    ),
    quality_status: Literal["queued", "running", "passed", "skipped", "failed"] | None = Query(
        default=None
    ),
    compliance_status: Literal["queued", "running", "passed", "skipped", "failed"] | None = Query(
        default=None
    ),
    annotation_status: Literal["disabled", "pending", "annotating", "reviewing", "completed"]
    | None = Query(default=None),
    upload_completed_from: datetime | None = Query(default=None),
    upload_completed_to: datetime | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=workspace_id)

    upload_completed_at = (
        db.query(func.max(DataPackage.upload_completed_at))
        .join(DataBatchPackage, DataBatchPackage.data_package_id == DataPackage.id)
        .filter(DataBatchPackage.data_batch_id == DataBatch.id)
        .correlate(DataBatch)
        .scalar_subquery()
    )
    query = db.query(DataBatch).filter(DataBatch.workspace_id == workspace_id)
    if collection_project_id is not None:
        # A batch belongs to a project when any of its packages does.
        query = query.filter(
            DataBatch.id.in_(
                db.query(DataBatchPackage.data_batch_id)
                .join(DataPackage, DataPackage.id == DataBatchPackage.data_package_id)
                .filter(DataPackage.collection_project_id == collection_project_id)
            )
        )
    for category, label_id in (
        ("scene", scene_label_id),
        ("purpose", purpose_label_id),
        ("training", training_label_id),
        ("modality", modality_label_id),
    ):
        query = _filter_batch_label(query, category=category, label_id=label_id)
    for stage, stage_status in (
        ("integrity", integrity_status),
        ("quality", quality_status),
        ("compliance", compliance_status),
    ):
        if stage_status is not None:
            query = query.filter(
                DataBatch.governance_run.has(
                    DataBatchGovernanceRun.stages.any(stage=stage, status=stage_status)
                )
            )
    query = _filter_annotation_status(query, annotation_status)
    if upload_completed_from is not None:
        query = query.filter(upload_completed_at >= upload_completed_from)
    if upload_completed_to is not None:
        query = query.filter(upload_completed_at <= upload_completed_to)

    total = query.count()
    query = query.options(
        selectinload(DataBatch.governance_run)
        .selectinload(DataBatchGovernanceRun.stages)
        .load_only(DataBatchStageRun.stage, DataBatchStageRun.status)
    ).order_by(DataBatch.created_at.desc(), DataBatch.id.desc())
    if page is not None:
        query = query.offset((page - 1) * size).limit(size)
    batches = query.all()
    if page is None:
        return success({"items": [_batch_item(batch) for batch in batches]})

    batch_ids = [batch.id for batch in batches]
    counts = (
        dict(
            db.query(DataBatchPackage.data_batch_id, func.count())
            .filter(DataBatchPackage.data_batch_id.in_(batch_ids))
            .group_by(DataBatchPackage.data_batch_id)
            .all()
        )
        if batches
        else {}
    )
    upload_times = (
        dict(
            db.query(
                DataBatchPackage.data_batch_id,
                func.max(DataPackage.upload_completed_at),
            )
            .join(DataPackage, DataPackage.id == DataBatchPackage.data_package_id)
            .filter(DataBatchPackage.data_batch_id.in_(batch_ids))
            .group_by(DataBatchPackage.data_batch_id)
            .all()
        )
        if batches
        else {}
    )
    return success(
        {
            "items": [
                {
                    "id": batch.id,
                    "workspace_id": batch.workspace_id,
                    "name": batch.name,
                    "status": batch.status,
                    "package_count": counts.get(batch.id, 0),
                    "episode_count": batch.episode_count,
                    "valid_duration_hours": _decimal_str(batch.valid_duration_hours),
                    "annotation_enabled": batch.annotation_enabled,
                    "annotation_status": _annotation_status(batch),
                    "upload_completed_at": format_api_datetime(upload_times.get(batch.id)),
                    "created_at": format_api_datetime(batch.created_at),
                    "stages": [
                        {"stage": stage.stage, "status": stage.status}
                        for stage in batch.governance_run.stages
                    ]
                    if batch.governance_run
                    else [],
                }
                for batch in batches
            ],
            "total": total,
            "page": page,
            "size": size,
        }
    )


@router.get("/{data_batch_id}")
def get_data_batch_detail(
    data_batch_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    try:
        batch = get_data_batch(db, workspace_id=workspace_id, data_batch_id=data_batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    packages = (
        db.query(DataPackage)
        .join(DataBatchPackage, DataBatchPackage.data_package_id == DataPackage.id)
        .filter(DataBatchPackage.data_batch_id == batch.id)
        .all()
    )
    extras = package_list_extras(db, packages)
    return success(
        {
            **_batch_item(batch),
            "packages": [{**_package_item(package), **extras[package.id]} for package in packages],
        }
    )


@router.post("")
def post_data_batch(
    body: CreateDataBatchRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        batch, should_schedule_pipeline = create_data_batch(
            db,
            workspace_id=body.workspace_id,
            name=body.name,
            data_package_ids=body.data_package_ids,
            label_ids=body.label_ids,
            integrity_check_enabled=body.integrity_check_enabled,
            quality_check_enabled=body.quality_check_enabled,
            compliance_check_enabled=body.compliance_check_enabled,
            annotation_enabled=body.annotation_enabled,
            annotator_user_ids=body.annotator_user_ids,
            reviewer_user_id=body.reviewer_user_id,
            review_mode=body.review_mode,
            created_by_user_id=_actor_id(user),
        )
        db.commit()
        db.refresh(batch)
    except BatchConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BatchLockedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if should_schedule_pipeline:
        schedule_post_batch_pipeline(batch.id)

    emit_audit_event(
        "governance.batch.create",
        actor=str(user.get("email") or ""),
        resource=f"data_batch:{batch.id}",
        detail={
            "workspace_id": body.workspace_id,
            "data_package_ids": body.data_package_ids,
            "annotation_enabled": body.annotation_enabled,
        },
    )
    return success(_batch_item(batch))


def _stage_item(stage: dict) -> dict[str, object]:
    return {
        "stage": stage["stage"],
        "status": stage["status"],
        "attempt": stage["attempt"],
        "result_json": stage["result_json"],
        "error_message": stage["error_message"],
        "created_at": format_api_datetime(stage["created_at"]),
        "updated_at": format_api_datetime(stage["updated_at"]),
    }


@router.get("/{data_batch_id}/governance-report")
def get_data_batch_governance_report(
    data_batch_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    try:
        report = get_governance_report(db, workspace_id=workspace_id, batch_id=data_batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return success(
        {
            "data_batch_id": report["data_batch_id"],
            "run_id": report.get("run_id"),
            "status": report["status"],
            "stages": [_stage_item(stage) for stage in report["stages"]],
        }
    )


@router.post("/{data_batch_id}/governance/retry")
def post_data_batch_governance_retry(
    data_batch_id: int,
    body: RetryGovernanceStageRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        get_data_batch(db, workspace_id=body.workspace_id, data_batch_id=data_batch_id)
        stage = retry_failed_stage(db, batch_id=data_batch_id, stage=body.stage, sync=True)
        db.commit()
        db.refresh(stage)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GovernanceError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    emit_audit_event(
        "governance.stage.retry",
        actor=str(user.get("email") or ""),
        resource=f"data_batch:{data_batch_id}",
        detail={"workspace_id": body.workspace_id, "stage": body.stage},
    )
    return success(
        {
            "stage": stage.stage,
            "status": stage.status,
            "attempt": stage.attempt,
            "result_json": stage.result_json or {},
            "error_message": stage.error_message or "",
        }
    )
