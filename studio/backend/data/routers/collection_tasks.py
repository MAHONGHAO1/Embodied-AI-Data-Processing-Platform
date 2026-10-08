"""Workspace-scoped collection task management API."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from data.database import get_db
from data.models.collection_core import CollectionTask
from data.models.data_package import DataPackage
from data.security.audit import emit_audit_event
from data.services.collection_access import require_collection_workspace
from data.services.collection_tasks import (
    OFFLINE_MANIFEST_COLUMNS,
    ArchivedCollectionProjectError,
    list_collection_tasks_with_progress,
    list_task_packages,
    offline_manifest_rows,
    task_capture_periods,
)
from data.services.collection_tasks import (
    create_collection_task as create_collection_task_record,
)
from data.services.collection_tasks import (
    get_collection_task as get_collection_task_record,
)
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/collection-tasks", tags=["采集任务"])

_MIN_DURATION = Decimal("0.01")
_MAX_DURATION = Decimal("99999999.99")


class CollectionTaskCreateRequest(BaseModel):
    """Request parameters for creating a collection task."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    collection_project_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    target_duration_hours: Decimal = Field(
        ge=_MIN_DURATION,
        le=_MAX_DURATION,
        max_digits=10,
        decimal_places=2,
    )
    default_package_duration_hours: Decimal = Field(
        default=Decimal("2.00"),
        ge=_MIN_DURATION,
        le=_MAX_DURATION,
        max_digits=10,
        decimal_places=2,
    )
    sop_text: str = ""
    device_model_id: int | None = Field(default=None, gt=0)
    label_ids: list[int] = Field(default_factory=list)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _decimal(value: Decimal | None) -> str:
    return f"{Decimal(value or 0):.2f}"


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


def _package_item(package: DataPackage) -> dict[str, object]:
    return {
        "id": package.id,
        "package_uid": package.package_uid,
        "workspace_id": package.workspace_id,
        "collection_project_id": package.collection_project_id,
        "collection_task_id": package.collection_task_id,
        "status": package.status,
        "collector_id": package.operator_collector_id,
        "responsible_collector_id": package.responsible_collector_id,
        "operator_collector_id": package.operator_collector_id,
        "collection_device_id": package.collection_device_id,
        "assigned_at": format_api_datetime(package.assigned_at),
        "captured_started_at": format_api_datetime(package.captured_started_at),
        "upload_completed_at": format_api_datetime(package.upload_completed_at),
        "created_at": format_api_datetime(package.created_at),
        "updated_at": format_api_datetime(package.updated_at),
        "target_duration_hours": _decimal(package.target_duration_hours),
        "captured_duration_hours": (
            _decimal(package.captured_duration_hours)
            if package.captured_duration_hours is not None
            else None
        ),
        "intake_valid_duration_hours": (
            _decimal(package.intake_valid_duration_hours)
            if package.intake_valid_duration_hours is not None
            else None
        ),
    }


def _task_item(
    task: CollectionTask,
    *,
    package_count: int,
    assigned_count: int,
    intake_valid_duration_hours: Decimal,
    pending_assignment_count: int = 0,
    capture_period: tuple[datetime, datetime] | None = None,
    packages: list[DataPackage] | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": task.id,
        "workspace_id": task.workspace_id,
        "collection_project_id": task.collection_project_id,
        "name": task.name,
        "description": task.description,
        "target_duration_hours": _decimal(task.target_duration_hours),
        "default_package_duration_hours": _decimal(task.default_package_duration_hours),
        "capture_mode": task.capture_mode,
        "sop_text": task.sop_text,
        "device_model_id": task.device_model_id,
        "label_ids": [association.collection_label_id for association in task.labels],
        "created_by_user_id": task.created_by_user_id,
        "created_by_user_email": (
            task.created_by_user.email if getattr(task, "created_by_user", None) else None
        ),
        "package_count": package_count,
        "assigned_count": assigned_count,
        "pending_assignment_count": pending_assignment_count,
        "intake_valid_duration_hours": _decimal(intake_valid_duration_hours),
        # Earliest and latest package capture start; null until a package has capture facts.
        "captured_started_from": format_api_datetime(capture_period[0]) if capture_period else None,
        "captured_started_to": format_api_datetime(capture_period[1]) if capture_period else None,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }
    if packages is not None:
        item["packages"] = [_package_item(package) for package in packages]
    return item


def _progress_for_task(
    db: Session,
    *,
    task: CollectionTask,
) -> tuple[int, int, Decimal, int]:
    rows = list_collection_tasks_with_progress(
        db,
        workspace_id=task.workspace_id,
        collection_project_id=task.collection_project_id,
    )
    for candidate, package_count, assigned_count, intake_duration, pending in rows:
        if candidate.id == task.id:
            return package_count, assigned_count, intake_duration, pending
    return 0, 0, Decimal("0.00"), 0


@router.get("")
def list_collection_tasks(
    workspace_id: int = Query(..., gt=0),
    collection_project_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """List collection tasks and their progress statistics within the workspace."""
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    rows = list_collection_tasks_with_progress(
        db,
        workspace_id=workspace_id,
        collection_project_id=collection_project_id,
    )
    periods = task_capture_periods(db, task_ids=[row[0].id for row in rows])
    return success(
        {
            "items": [
                _task_item(
                    task,
                    package_count=package_count,
                    assigned_count=assigned_count,
                    intake_valid_duration_hours=intake_duration,
                    pending_assignment_count=pending,
                    capture_period=periods.get(task.id),
                )
                for task, package_count, assigned_count, intake_duration, pending in rows
            ]
        }
    )


@router.post("")
def create_collection_task(
    body: CollectionTaskCreateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Create a collection task and automatically initialize pending data packages based on target duration and default package duration."""
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        task = create_collection_task_record(
            db,
            workspace_id=body.workspace_id,
            collection_project_id=body.collection_project_id,
            name=body.name,
            description=body.description,
            target_duration_hours=body.target_duration_hours,
            default_package_duration_hours=body.default_package_duration_hours,
            sop_text=body.sop_text,
            device_model_id=body.device_model_id,
            label_ids=body.label_ids,
            created_by_user_id=_actor_id(user),
        )
        db.commit()
        db.refresh(task)
    except ArchivedCollectionProjectError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    packages = list_task_packages(db, task_id=task.id)
    emit_audit_event(
        "collection.task.create",
        actor=str(user.get("email") or ""),
        resource=f"collection_task:{task.id}",
        detail={
            "workspace_id": body.workspace_id,
            "collection_project_id": body.collection_project_id,
            "package_count": len(packages),
        },
    )
    return success(
        _task_item(
            task,
            package_count=len(packages),
            assigned_count=0,
            intake_valid_duration_hours=Decimal("0.00"),
            pending_assignment_count=len(packages),
            packages=packages,
        )
    )


@router.get("/{task_id}/offline-manifest")
def collection_task_offline_manifest(
    task_id: int,
    workspace_id: int = Query(..., gt=0),
    format: str = Query(default="json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Package level manifest an offline tool downloads before collection."""

    require_permission(user, "episode:read")
    try:
        require_collection_workspace(db, actor_id=_actor_id(user), workspace_id=workspace_id)
        rows = offline_manifest_rows(db, task_id=task_id, workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    revision = rows[0]["manifest_revision"] if rows else ""
    if format == "csv":
        columns = list(OFFLINE_MANIFEST_COLUMNS)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        return PlainTextResponse(
            buffer.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"X-Manifest-Revision": str(revision)},
        )
    return success({"items": rows, "manifest_revision": revision})


@router.get("/{task_id}")
def get_collection_task(
    task_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Get single collection task details and associated data packages."""
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    try:
        task = get_collection_task_record(
            db,
            workspace_id=workspace_id,
            task_id=task_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    package_count, assigned_count, intake_duration, pending = _progress_for_task(db, task=task)
    return success(
        _task_item(
            task,
            package_count=package_count,
            assigned_count=assigned_count,
            intake_valid_duration_hours=intake_duration,
            pending_assignment_count=pending,
            capture_period=task_capture_periods(db, task_ids=[task.id]).get(task.id),
            packages=list_task_packages(db, task_id=task.id),
        )
    )


@router.get("/{task_id}/packages")
def list_collection_task_packages(
    task_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """List all data packages under the specified collection task."""
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    try:
        task = get_collection_task_record(
            db,
            workspace_id=workspace_id,
            task_id=task_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    packages = list_task_packages(db, task_id=task.id)
    return success(
        {
            "items": [
                {
                    **_package_item(package),
                    "responsible_collector_id": package.responsible_collector_id,
                    "operator_collector_id": package.operator_collector_id,
                    "collector_id": package.responsible_collector_id
                    or package.operator_collector_id,
                    "collection_device_id": package.collection_device_id,
                    "assigned_at": (
                        format_api_datetime(package.assigned_at)
                        if package.assigned_at is not None
                        else None
                    ),
                }
                for package in packages
            ]
        }
    )
