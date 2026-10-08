"""Review work item API (single-review mode for the current phase)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from data.database import User, get_db
from data.models.annotation_work import ReviewWorkItem
from data.services.governance_runs import schedule_asset_creation
from data.services.review_work_items import (
    ReviewWorkConflict,
    ReviewWorkError,
    ReviewWorkForbidden,
    approve_review_work_item,
    list_review_work_items_page,
    reassign_review_work_item,
    return_review_work_item,
)
from data.services.work_item_scope import work_item_workspaces
from data.services.workspace_access import require_actor
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/review-work-items", tags=["审核工作项"])


class WorkspaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)


class ReviewDecisionBody(WorkspaceBody):
    expected_submission_id: int | None = Field(default=None, gt=0)
    expected_generation: int | None = Field(default=None, ge=1)


class ReturnRequest(ReviewDecisionBody):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_non_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("reason must not be empty")
        return cleaned


class ReassignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    to_user_id: int = Field(gt=0)
    reason: str = Field(min_length=1)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


def _require_worker(db: Session, *, user: dict) -> User:
    """Review is not scoped by workspace membership; the item assignment authorizes."""
    try:
        return require_actor(db, actor_id=_actor_id(user))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _item_payload(item: ReviewWorkItem) -> dict[str, object]:
    source = (item.submission.source_json or {}).get("source") if item.submission else None
    source = source if isinstance(source, dict) and source else None
    return {
        "id": item.id,
        "annotation_work_item_id": item.annotation_work_item_id,
        "episode_members": item.annotation_item.episode_members_json or [],
        "data_batch_id": item.data_batch_id,
        "data_batch_name": item.batch.name if item.batch else "",
        "data_package_id": item.annotation_item.data_package_id,
        "data_package_uid": (
            item.annotation_item.package.package_uid if item.annotation_item.package else ""
        ),
        "workspace_id": item.workspace_id,
        "workspace_name": item.workspace.name if item.workspace else "",
        "assignee_user_id": item.assignee_user_id,
        "assignee_email": item.assignee.email if item.assignee else "",
        "status": item.status,
        "reason": item.reason or "",
        "generation": item.generation,
        "submission_id": item.submission_id,
        "historical_result_uncertain": item.submission_id is None
        and item.annotation_item.status in {"submitted", "done"},
        "reassign_reason": item.reassign_reason or "",
        "created_at": format_api_datetime(item.created_at),
        "updated_at": format_api_datetime(item.updated_at),
        "source": source,
        "confidence": (source or {}).get("confidence"),
    }


def _map_service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ReviewWorkForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ReviewWorkConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ReviewWorkError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("")
def get_review_work_items(
    workspace_id: int | None = Query(default=None, gt=0),
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:review")
    actor = _require_worker(db, user=user)
    page = list_review_work_items_page(
        db, workspace_id=workspace_id, actor=actor, limit=limit, offset=offset
    )
    return success(
        {
            **page,
            "items": [_item_payload(item) for item in page["items"]],
            "workspaces": work_item_workspaces(db, ReviewWorkItem, actor=actor),
        }
    )


@router.post("/{item_id}/approve")
def post_approve_review_work_item(
    item_id: int,
    body: ReviewDecisionBody,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:review")
    actor = _require_worker(db, user=user)
    try:
        item, should_schedule_asset = approve_review_work_item(
            db,
            workspace_id=body.workspace_id,
            item_id=item_id,
            actor=actor,
            expected_submission_id=body.expected_submission_id,
            expected_generation=body.expected_generation,
        )
        db.commit()
        db.refresh(item)
    except (ReviewWorkForbidden, ReviewWorkConflict, ReviewWorkError, LookupError) as exc:
        db.rollback()
        raise _map_service_error(exc) from exc
    if should_schedule_asset:
        schedule_asset_creation(item.data_batch_id)
    return success(_item_payload(item))


@router.post("/{item_id}/return")
def post_return_review_work_item(
    item_id: int,
    body: ReturnRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:review")
    actor = _require_worker(db, user=user)
    try:
        item = return_review_work_item(
            db,
            workspace_id=body.workspace_id,
            item_id=item_id,
            actor=actor,
            reason=body.reason,
            expected_submission_id=body.expected_submission_id,
            expected_generation=body.expected_generation,
        )
        db.commit()
        db.refresh(item)
    except (ReviewWorkForbidden, ReviewWorkConflict, ReviewWorkError, LookupError) as exc:
        db.rollback()
        raise _map_service_error(exc) from exc
    return success(_item_payload(item))


@router.post("/{item_id}/reassign")
def post_reassign_review_work_item(
    item_id: int,
    body: ReassignRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    actor = _require_worker(db, user=user)
    try:
        actor = require_actor(db, actor_id=actor.id)
        item = reassign_review_work_item(
            db,
            workspace_id=body.workspace_id,
            item_id=item_id,
            to_user_id=body.to_user_id,
            reason=body.reason,
            actor=actor,
        )
        db.commit()
        db.refresh(item)
    except (ReviewWorkForbidden, ReviewWorkConflict, ReviewWorkError, LookupError) as exc:
        db.rollback()
        raise _map_service_error(exc) from exc
    return success(_item_payload(item))


@router.get("/{item_id}/workbench")
def review_package_workbench(
    item_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    from data.routers.annotation_work_items import _map_service_error as map_projection_error
    from data.services.annotation_work_items import AnnotationWorkError, AnnotationWorkForbidden
    from data.services.package_annotation_workbench import package_workbench

    require_permission(user, "episode:review")
    actor = _require_worker(db, user=user)
    try:
        return success(
            package_workbench(
                db, workspace_id=workspace_id, item_id=item_id, actor=actor, review=True
            )
        )
    except (AnnotationWorkError, AnnotationWorkForbidden, LookupError) as exc:
        raise map_projection_error(exc) from exc


@router.get("/{item_id}/episodes/{episode_id}/workbench")
def review_episode_workbench(
    item_id: int,
    episode_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    from data.routers.annotation_work_items import _map_service_error as map_projection_error
    from data.services.annotation_work_items import AnnotationWorkError, AnnotationWorkForbidden
    from data.services.package_annotation_workbench import episode_workbench

    require_permission(user, "episode:review")
    actor = _require_worker(db, user=user)
    try:
        return success(
            episode_workbench(
                db,
                workspace_id=workspace_id,
                item_id=item_id,
                episode_id=episode_id,
                actor=actor,
                review=True,
            )
        )
    except (AnnotationWorkError, AnnotationWorkForbidden, LookupError) as exc:
        raise map_projection_error(exc) from exc
