"""Unified Episode-owned human work queue."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from data.database import Episode, JobRun, WorkItem, Workspace, get_db
from data.realtime.outbox import (
    enqueue_resource_event,
    enqueue_work_queue_invalidated,
    enqueue_work_queue_range_invalidated,
)
from data.realtime.projections import episode_snapshot, job_run_snapshot, work_item_snapshot
from data.realtime.socketio import schedule_realtime_dispatch
from data.security.audit import emit_audit_event
from data.services.episode_workbench import (
    DraftConflict,
    WorkbenchError,
    review_draft,
    save_draft,
    submit_draft,
)
from data.services.task_dispatcher import dispatch_media_job
from data.services.work_queue_projection import (
    claim_work_item,
    continue_work_item,
    list_work_queue,
    release_work_item,
    work_item_summary,
)
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/work-queue", tags=["统一工作队列"])
logger = logging.getLogger("quicdata.work_queue")


class ReviewRequest(BaseModel):
    decision: str = Field(pattern="^(accepted|rejected)$")
    note: str = Field(default="", max_length=2000)
    collector_profile_id: int | None = Field(default=None, gt=0)
    collection_device_id: int | None = Field(default=None, gt=0)


class ReleaseRequest(BaseModel):
    note: str = Field(default="", max_length=2000)


class DraftRequest(BaseModel):
    base_version: int = Field(ge=0)
    payload: dict[str, object]


class SubmitRequest(BaseModel):
    base_version: int = Field(ge=0)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub"))
    except (TypeError, ValueError):
        return None


@router.get("")
def get_work_queue(
    workspace_id: int = Query(..., gt=0),
    stage: str = Query(..., pattern="^(cut|annotation|review|completed)$"),
    review_target_kind: Literal["cut", "annotation"] | None = Query(default=None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    task_set_id: int | None = Query(default=None, gt=0),
    task_label_id: int | None = Query(default=None, gt=0),
    status: Literal["pending", "assigned", "in_progress", "submitted", "rejected", "accepted"]
    | None = Query(default=None),
    assignee_user_id: int | None = Query(default=None, gt=0),
    episode_keyword: str | None = Query(default=None, max_length=128),
    updated_from: datetime | None = Query(default=None),
    updated_to: datetime | None = Query(default=None),
    sort_by: Literal["created_at", "updated_at", "episode_uid", "duration"] | None = Query(
        default=None
    ),
    sort_order: Literal["asc", "desc"] | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "episode:read")
    try:
        return success(
            list_work_queue(
                db,
                workspace_id=workspace_id,
                actor_id=_actor_id(user),
                stage=stage,
                review_target_kind=review_target_kind,
                limit=limit,
                offset=offset,
                task_set_id=task_set_id,
                task_label_id=task_label_id,
                status=status,
                assignee_user_id=assignee_user_id,
                episode_keyword=episode_keyword,
                updated_from=updated_from,
                updated_to=updated_to,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/items/{work_item_id}/claim")
def claim(work_item_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    return _mutate(db, user, work_item_id, "claim")


@router.post("/items/{work_item_id}/continue")
def continue_item(
    work_item_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    return _mutate(db, user, work_item_id, "continue")


@router.post("/items/{work_item_id}/release")
def release(
    work_item_id: int,
    body: ReleaseRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    return _mutate(db, user, work_item_id, "release", note=body.note)


@router.post("/items/{work_item_id}/submit")
def submit(
    work_item_id: int,
    body: SubmitRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    return _mutate(db, user, work_item_id, "submit", base_version=body.base_version)


@router.put("/items/{work_item_id}/draft")
def save_work_item_draft(
    work_item_id: int,
    body: DraftRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    item = db.get(WorkItem, work_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="episode work item does not exist")
    require_permission(user, "episode:annotate")
    actor_id = _actor_id(user)
    try:
        item = save_draft(
            db,
            work_item_id=work_item_id,
            actor_id=actor_id,
            base_version=body.base_version,
            payload=body.payload,
        )
        episode = db.get(Episode, item.episode_id)
        workspace = db.get(Workspace, item.workspace_id)
        if episode is None or workspace is None:
            raise ValueError("work item scope is unavailable")
        enqueue_resource_event(
            db,
            resource=item,
            resource_type="work_item",
            event_name="work_item.updated",
            resource_snapshot=work_item_snapshot(item),
        )
        enqueue_work_queue_invalidated(db, workspace=workspace)
        db.commit()
        db.refresh(item)
    except DraftConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, WorkbenchError) as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "work_item.draft_saved",
        actor=user.get("email"),
        resource=f"work_item:{item.id}",
        detail={
            "episode_id": item.episode_id,
            "kind": item.kind,
            "draft_version": item.draft_version,
        },
    )
    schedule_realtime_dispatch()
    return success(
        {
            "draft": {
                "kind": item.kind,
                "version": item.draft_version,
                "payload": dict(item.draft_json or {}),
            },
            "work_item": work_item_summary(db, item=item, actor_id=actor_id),
            # Lets the browser acknowledge the durable outbox event emitted by
            # this save without treating its own update as a remote reload.
            "work_item_realtime_version": int(item.realtime_version or 0),
        }
    )


@router.post("/items/{work_item_id}/review")
def review(
    work_item_id: int,
    body: ReviewRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    collector_profile_id: int | None | object
    if "collector_profile_id" in body.model_fields_set:
        collector_profile_id = body.collector_profile_id
    else:
        collector_profile_id = _UNSET_REVIEW_COLLECTOR
    collection_device_id: int | None | object
    if "collection_device_id" in body.model_fields_set:
        collection_device_id = body.collection_device_id
    else:
        collection_device_id = _UNSET_REVIEW_DEVICE
    return _mutate(
        db,
        user,
        work_item_id,
        "review",
        decision=body.decision,
        note=body.note,
        collector_profile_id=collector_profile_id,
        collection_device_id=collection_device_id,
    )


_UNSET_REVIEW_COLLECTOR = object()
_UNSET_REVIEW_DEVICE = object()


def _mutate(
    db: Session,
    user: dict,
    work_item_id: int,
    action: str,
    *,
    decision: str = "",
    note: str = "",
    base_version: int | None = None,
    collector_profile_id: int | None | object = _UNSET_REVIEW_COLLECTOR,
    collection_device_id: int | None | object = _UNSET_REVIEW_DEVICE,
):
    item = db.get(WorkItem, work_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="episode work item does not exist")
    require_permission(user, "episode:review" if item.kind == "review" else "episode:annotate")
    actor_id = _actor_id(user)
    try:
        publication: JobRun | None = None
        preview_jobs: list[JobRun] = []
        related_items: list[WorkItem] = []
        derived_episodes: list[Episode] = []
        mutation_applied = True
        if action == "claim":
            item = claim_work_item(db, work_item_id=work_item_id, actor_id=actor_id)
        elif action == "continue":
            item = continue_work_item(db, work_item_id=work_item_id, actor_id=actor_id)
        elif action == "release":
            item = release_work_item(db, work_item_id=work_item_id, actor_id=actor_id, note=note)
        elif action == "submit":
            if base_version is None:
                raise ValueError("base_version is required")
            item, review_item = submit_draft(
                db,
                work_item_id=work_item_id,
                actor_id=actor_id,
                base_version=base_version,
            )
            related_items.append(review_item)
            enqueue_resource_event(
                db,
                resource=review_item,
                resource_type="work_item",
                event_name="work_item.updated",
                resource_snapshot=work_item_snapshot(review_item),
            )
        elif action == "review":
            kwargs: dict[str, object] = {}
            if collector_profile_id is not _UNSET_REVIEW_COLLECTOR:
                kwargs["collector_profile_id"] = collector_profile_id
            if collection_device_id is not _UNSET_REVIEW_DEVICE:
                kwargs["collection_device_id"] = collection_device_id
            result = review_draft(
                db,
                work_item_id=work_item_id,
                actor_id=actor_id,
                decision=decision,
                note=note,
                **kwargs,
            )
            item = result.review_item
            publication = result.publication
            related_items.append(result.submitted_item)
            derived_episodes.extend(result.derived_episodes)
            preview_jobs.extend(result.preview_jobs)
            mutation_applied = not result.replayed
        else:
            raise ValueError("unsupported work queue action")
        episode = db.get(Episode, item.episode_id)
        if episode is None:
            raise ValueError("work item episode is unavailable")
        workspace = db.get(Workspace, item.workspace_id)
        if workspace is None:
            raise ValueError("work item workspace is unavailable")
        if mutation_applied:
            enqueue_resource_event(
                db,
                resource=item,
                resource_type="work_item",
                event_name="work_item.updated",
                resource_snapshot=work_item_snapshot(item),
            )
            enqueue_resource_event(
                db,
                resource=episode,
                resource_type="episode",
                event_name="episode.updated",
                resource_snapshot=episode_snapshot(episode),
            )
            for related_item in related_items:
                enqueue_resource_event(
                    db,
                    resource=related_item,
                    resource_type="work_item",
                    event_name="work_item.updated",
                    resource_snapshot=work_item_snapshot(related_item),
                )
            if publication is not None:
                enqueue_resource_event(
                    db,
                    resource=publication,
                    resource_type="job_run",
                    event_name="publication.updated",
                    resource_snapshot=job_run_snapshot(publication),
                )
            for preview_job in preview_jobs:
                enqueue_resource_event(
                    db,
                    resource=preview_job,
                    resource_type="job_run",
                    event_name="job_run.updated",
                    resource_snapshot=job_run_snapshot(preview_job),
                )
            if derived_episodes:
                enqueue_work_queue_range_invalidated(
                    db,
                    workspace=workspace,
                    source=episode,
                    affected_episode_count=len(derived_episodes),
                )
            else:
                enqueue_work_queue_invalidated(db, workspace=workspace)
        db.commit()
        db.refresh(item)
    except DraftConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, WorkbenchError) as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        f"work_item.{action}",
        actor=user.get("email"),
        resource=f"work_item:{item.id}",
        detail={"episode_id": item.episode_id, "kind": item.kind},
    )
    if mutation_applied and publication is not None:
        try:
            dispatch_media_job(publication, worker_prechecked=True)
        except Exception as exc:
            logger.warning(
                "publication job remains queued job_id=%s error_type=%s",
                publication.id,
                type(exc).__name__,
            )
    for preview_job in preview_jobs if mutation_applied else ():
        try:
            dispatch_media_job(preview_job, worker_prechecked=True)
        except Exception as exc:
            logger.warning(
                "derived preview job remains queued job_id=%s error_type=%s",
                preview_job.id,
                type(exc).__name__,
            )
    schedule_realtime_dispatch()
    return success(
        {
            "work_item": work_item_summary(db, item=item, actor_id=actor_id),
            # The caller can suppress the durable outbox event for its own
            # mutation while still applying later external updates.
            "work_item_realtime_version": int(item.realtime_version or 0),
        }
    )
