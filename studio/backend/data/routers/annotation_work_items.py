"""Annotation work items API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from data.database import User, get_db
from data.models.annotation_work import AnnotationWorkItem
from data.services.annotation_work_items import (
    AnnotationWorkConflict,
    AnnotationWorkError,
    AnnotationWorkForbidden,
    batch_submit_annotation_items,
    list_annotation_work_items_page,
    reassign_annotation_work_item,
    save_annotation_draft,
    submit_annotation_work_item,
)
from data.services.work_item_scope import work_item_workspaces
from data.services.workspace_access import require_actor
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/annotation-work-items", tags=["标注工作项"])


class SaveDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    draft_json: dict[str, Any] = Field(default_factory=dict)
    base_version: int | None = Field(default=None, ge=0)
    expected_generation: int | None = Field(default=None, ge=1)


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    base_version: int | None = Field(default=None, ge=0)
    expected_generation: int | None = Field(default=None, ge=1)


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
    """Annotation is not scoped by workspace membership; the item assignment authorizes."""
    try:
        return require_actor(db, actor_id=_actor_id(user))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _item_payload(item: AnnotationWorkItem) -> dict[str, object]:
    return {
        "id": item.id,
        "data_batch_id": item.data_batch_id,
        "data_batch_name": item.batch.name if item.batch else "",
        "data_package_id": item.data_package_id,
        "data_package_uid": item.package.package_uid if item.package else "",
        "workspace_id": item.workspace_id,
        "workspace_name": item.workspace.name if item.workspace else "",
        "assignee_user_id": item.assignee_user_id,
        "assignee_email": item.assignee.email if item.assignee else "",
        "status": item.status,
        "draft_json": item.draft_json or {},
        "episode_members": item.episode_members_json or [],
        "draft_version": item.draft_version,
        "generation": item.generation,
        "submission_id": item.current_submission_id,
        "reassign_reason": item.reassign_reason or "",
        "return_reason": item.return_reason
        or (
            (item.review_item.reason or "")
            if getattr(item, "review_item", None) is not None
            else ""
        ),
        "created_at": format_api_datetime(item.created_at),
        "updated_at": format_api_datetime(item.updated_at),
    }


def _map_service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AnnotationWorkForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, AnnotationWorkConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AnnotationWorkError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("")
def get_annotation_work_items(
    workspace_id: int | None = Query(default=None, gt=0),
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:annotate")
    actor = _require_worker(db, user=user)
    page = list_annotation_work_items_page(
        db, workspace_id=workspace_id, actor=actor, limit=limit, offset=offset
    )
    return success(
        {
            **page,
            "items": [_item_payload(item) for item in page["items"]],
            "workspaces": work_item_workspaces(db, AnnotationWorkItem, actor=actor),
        }
    )


@router.patch("/{item_id}")
def patch_annotation_work_item(
    item_id: int,
    body: SaveDraftRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:annotate")
    actor = _require_worker(db, user=user)
    try:
        item = save_annotation_draft(
            db,
            workspace_id=body.workspace_id,
            item_id=item_id,
            actor=actor,
            draft_json=body.draft_json,
            base_version=body.base_version,
            expected_generation=body.expected_generation,
        )
        db.commit()
        db.refresh(item)
    except (
        AnnotationWorkForbidden,
        AnnotationWorkConflict,
        AnnotationWorkError,
        LookupError,
    ) as exc:
        db.rollback()
        raise _map_service_error(exc) from exc
    return success(_item_payload(item))


@router.post("/{item_id}/submit")
def post_submit_annotation_work_item(
    item_id: int,
    body: SubmitRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:annotate")
    actor = _require_worker(db, user=user)
    try:
        item = submit_annotation_work_item(
            db,
            workspace_id=body.workspace_id,
            item_id=item_id,
            actor=actor,
            base_version=body.base_version,
            expected_generation=body.expected_generation,
        )
        db.commit()
        db.refresh(item)
    except (
        AnnotationWorkForbidden,
        AnnotationWorkConflict,
        AnnotationWorkError,
        LookupError,
    ) as exc:
        db.rollback()
        raise _map_service_error(exc) from exc
    return success(_item_payload(item))


@router.post("/{item_id}/reassign")
def post_reassign_annotation_work_item(
    item_id: int,
    body: ReassignRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    actor = _require_worker(db, user=user)
    try:
        item = reassign_annotation_work_item(
            db,
            workspace_id=body.workspace_id,
            item_id=item_id,
            to_user_id=body.to_user_id,
            reason=body.reason,
            actor=actor,
        )
        db.commit()
        db.refresh(item)
    except (
        AnnotationWorkForbidden,
        AnnotationWorkConflict,
        AnnotationWorkError,
        LookupError,
    ) as exc:
        db.rollback()
        raise _map_service_error(exc) from exc
    return success(_item_payload(item))


class BatchSubmitItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_item_id: int = Field(gt=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] | None = None
    review_required: bool = True


class BatchSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: int = Field(gt=0)
    client_request_id: str = Field(min_length=1, max_length=128)
    items: list[BatchSubmitItem] = Field(min_length=1, max_length=200)


@router.post("/batch-submit")
def batch_submit_annotation_items_endpoint(
    body: BatchSubmitRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Submit many annotation results for work items dispatched to the caller."""

    require_permission(user, "episode:annotate")
    actor = _require_worker(db, user=user)
    try:
        result = batch_submit_annotation_items(
            db,
            workspace_id=body.workspace_id,
            actor=actor,
            client_request_id=body.client_request_id,
            items=[item.model_dump() for item in body.items],
        )
    except AnnotationWorkConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AnnotationWorkForbidden as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AnnotationWorkError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return success(result)


@router.get("/{item_id}/workbench")
def annotation_package_workbench(
    item_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    from data.services.package_annotation_workbench import package_workbench

    require_permission(user, "episode:annotate")
    actor = _require_worker(db, user=user)
    try:
        return success(
            package_workbench(db, workspace_id=workspace_id, item_id=item_id, actor=actor)
        )
    except (AnnotationWorkError, AnnotationWorkForbidden, LookupError) as exc:
        raise _map_service_error(exc) from exc


@router.get("/{item_id}/episodes/{episode_id}/workbench")
def annotation_episode_workbench(
    item_id: int,
    episode_id: int,
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    from data.services.package_annotation_workbench import episode_workbench

    require_permission(user, "episode:annotate")
    actor = _require_worker(db, user=user)
    try:
        return success(
            episode_workbench(
                db, workspace_id=workspace_id, item_id=item_id, episode_id=episode_id, actor=actor
            )
        )
    except (AnnotationWorkError, AnnotationWorkForbidden, LookupError) as exc:
        raise _map_service_error(exc) from exc
