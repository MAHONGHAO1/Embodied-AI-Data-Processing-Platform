"""Authorized directory APIs for externally registered native LeRobot roots."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import NativeLerobotBundle, NativeLerobotDataset, TaskSet, get_db
from data.infra.redis_client import RedisUnavailableError
from data.realtime.outbox import enqueue_resource_event
from data.realtime.projections import job_run_snapshot
from data.realtime.socketio import schedule_realtime_dispatch
from data.security.audit import emit_audit_event
from data.security.signed_url import consume_download_jti, verify_signed_download_token
from data.services.native_lerobot_bundles import (
    NativeLerobotBundleError,
    issue_native_lerobot_bundle_download,
    native_lerobot_bundle_item,
    request_native_lerobot_bundle,
)
from data.services.native_lerobot_datasets import native_lerobot_dataset_item
from data.services.native_lerobot_import_sessions import refresh_native_lerobot_batch_status
from data.services.native_lerobot_replication import (
    NativeLerobotCopyRetryError,
    NativeLerobotSourceReauthorizationError,
    reauthorize_native_lerobot_source,
    retry_native_lerobot_copy,
)
from data.services.task_dispatcher import dispatch_media_job
from data.services.workspace_access import require_workspace_actor
from data.utils.helpers import get_current_user, get_optional_user, require_permission, success
from data.utils.storage_paths import resolve_storage_path

router = APIRouter(prefix="/native-lerobot-datasets", tags=["原生 LeRobot 数据集"])
logger = logging.getLogger("quicdata.native_lerobot_datasets")
_LEGACY_NATIVE_LEROBOT_RETIRED_DETAIL = (
    "legacy native LeRobot mutation is retired; use /native-lerobot-direct-uploads"
)


class NativeLerobotDatasetUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "archived"]


class NativeLerobotSourceReauthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_token: str = Field(min_length=1, max_length=4096)


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub") or user.get("id"))
    except (TypeError, ValueError):
        return None


def _raise_legacy_native_lerobot_retired() -> None:
    """Historical source and bundle records remain readable only."""
    raise HTTPException(status_code=410, detail=_LEGACY_NATIVE_LEROBOT_RETIRED_DETAIL)


def _dataset_or_404(db: Session, *, dataset_id: int, user: dict) -> NativeLerobotDataset:
    row = db.get(NativeLerobotDataset, dataset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="native LeRobot dataset does not exist")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=row.workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail="native LeRobot dataset does not exist"
        ) from exc
    return row


def _bundle_or_404(
    db: Session,
    *,
    dataset: NativeLerobotDataset,
    bundle_id: int,
) -> NativeLerobotBundle:
    bundle = db.get(NativeLerobotBundle, bundle_id)
    if bundle is None or bundle.native_lerobot_dataset_id != dataset.id:
        raise HTTPException(status_code=404, detail="native LeRobot bundle does not exist")
    return bundle


def _require_task_set_in_workspace(db: Session, *, workspace_id: int, task_set_id: int) -> None:
    task_set = db.get(TaskSet, task_set_id)
    if task_set is None or task_set.workspace_id != workspace_id:
        raise HTTPException(status_code=422, detail="task set scope is invalid")


@router.get("")
def list_native_lerobot_datasets(
    workspace_id: int = Query(..., gt=0),
    task_set_id: int | None = Query(default=None, gt=0),
    include_archived: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    if task_set_id is not None:
        _require_task_set_in_workspace(db, workspace_id=workspace_id, task_set_id=task_set_id)
    query = db.query(NativeLerobotDataset).filter(NativeLerobotDataset.workspace_id == workspace_id)
    if task_set_id is not None:
        query = query.filter(NativeLerobotDataset.task_set_id == task_set_id)
    if not include_archived:
        query = query.filter(NativeLerobotDataset.status == "active")
    rows = query.order_by(
        NativeLerobotDataset.created_at.desc(), NativeLerobotDataset.id.desc()
    ).all()
    return success({"items": [native_lerobot_dataset_item(row) for row in rows]})


@router.get("/{dataset_id}")
def get_native_lerobot_dataset(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    return success(
        native_lerobot_dataset_item(_dataset_or_404(db, dataset_id=dataset_id, user=user))
    )


@router.get("/{dataset_id}/oss-uri")
def get_native_lerobot_dataset_oss_uri(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    row = _dataset_or_404(db, dataset_id=dataset_id, user=user)
    if row.status != "active" or row.copy_status != "succeeded" or not row.oss_uri:
        raise HTTPException(status_code=409, detail="platform copy is not ready")
    emit_audit_event(
        "native_lerobot_dataset.oss_uri.read",
        actor=str(user.get("email") or ""),
        resource=f"native_lerobot_dataset:{row.id}",
        detail={"workspace_id": row.workspace_id, "task_set_id": row.task_set_id},
    )
    return success({"oss_uri": row.oss_uri})


@router.post("/{dataset_id}/bundles")
def request_native_lerobot_dataset_bundle(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    require_permission(user, "export:write")
    _raise_legacy_native_lerobot_retired()
    row = _dataset_or_404(db, dataset_id=dataset_id, user=user)
    try:
        bundle, job, created = request_native_lerobot_bundle(
            db,
            dataset_id=row.id,
            actor_id=_actor_id(user),
        )
        if created and job is not None:
            enqueue_resource_event(
                db,
                resource=job,
                resource_type="job_run",
                event_name="job_run.updated",
                resource_snapshot=job_run_snapshot(job),
            )
        db.commit()
        db.refresh(bundle)
        if job is not None:
            db.refresh(job)
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except KeyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=404, detail="native LeRobot dataset does not exist"
        ) from exc
    except NativeLerobotBundleError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    dispatch = _dispatch_native_bundle_after_commit(row=row, job=job, created=created, user=user)
    emit_audit_event(
        "native_lerobot_dataset.bundle.request",
        actor=str(user.get("email") or ""),
        resource=f"native_lerobot_dataset:{row.id}",
        detail={
            "bundle_id": bundle.id,
            "job_id": job.id if job is not None else "",
            "created": created,
            "dispatch": dispatch,
        },
    )
    schedule_realtime_dispatch()
    return success(
        {
            "bundle": native_lerobot_bundle_item(bundle),
            "job": _copy_job_item(job) if job is not None else None,
            "dispatch": dispatch,
        }
    )


@router.get("/{dataset_id}/bundles/{bundle_id}")
def get_native_lerobot_dataset_bundle(
    dataset_id: int,
    bundle_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    row = _dataset_or_404(db, dataset_id=dataset_id, user=user)
    bundle = _bundle_or_404(db, dataset=row, bundle_id=bundle_id)
    return success(native_lerobot_bundle_item(bundle))


@router.get("/{dataset_id}/bundles/{bundle_id}/download")
def native_lerobot_bundle_download_descriptor(
    dataset_id: int,
    bundle_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "export:read")
    row = _dataset_or_404(db, dataset_id=dataset_id, user=user)
    _bundle_or_404(db, dataset=row, bundle_id=bundle_id)
    try:
        payload = issue_native_lerobot_bundle_download(
            db,
            dataset_id=row.id,
            bundle_id=bundle_id,
            actor_id=_actor_id(user),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="native LeRobot bundle does not exist") from exc
    except NativeLerobotBundleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    emit_audit_event(
        "native_lerobot_dataset.bundle.download.read",
        actor=str(user.get("email") or ""),
        resource=f"native_lerobot_dataset:{row.id}",
        detail={"bundle_id": bundle_id, "direct": bool(payload["direct"])},
    )
    return success(payload)


@router.get("/{dataset_id}/bundles/{bundle_id}/content")
def stream_native_lerobot_bundle(
    dataset_id: int,
    bundle_id: int,
    sig: str | None = Query(default=None, max_length=4096),
    db: Session = Depends(get_db),
    user: dict | None = Depends(get_optional_user),
):
    signed_payload: dict[str, object] | None = None
    if sig:
        try:
            signed_payload = verify_signed_download_token(
                sig,
                resource_type="native_lerobot_bundle",
                resource_id=bundle_id,
            )
            actor_id = _actor_id({"sub": signed_payload.get("sub")})
            signed_dataset_id = signed_payload.get("dataset_id")
        except ValueError as exc:
            raise HTTPException(
                status_code=401, detail="native LeRobot bundle signature is invalid or expired"
            ) from exc
        if (
            actor_id is None
            or type(signed_dataset_id) is not int
            or signed_dataset_id != dataset_id
        ):
            raise HTTPException(
                status_code=401, detail="native LeRobot bundle signature is invalid or expired"
            )
        actor_label = str(actor_id)
        access_user = {"sub": actor_id}
    else:
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        require_permission(user, "export:read")
        actor_id = _actor_id(user)
        actor_label = str(user.get("email") or "")
        access_user = user

    row = _dataset_or_404(db, dataset_id=dataset_id, user=access_user)
    bundle = _bundle_or_404(db, dataset=row, bundle_id=bundle_id)
    try:
        issue_native_lerobot_bundle_download(
            db,
            dataset_id=row.id,
            bundle_id=bundle.id,
            actor_id=actor_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="native LeRobot bundle does not exist") from exc
    except NativeLerobotBundleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if signed_payload is not None:
        max_uses = int(signed_payload.get("max_uses") or 1)
        if not consume_download_jti(str(signed_payload.get("jti") or ""), max_uses):
            raise HTTPException(
                status_code=403, detail="native LeRobot bundle signature use limit reached"
            )
    path = resolve_storage_path(bundle.bundle_uri)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="platform bundle content is unavailable")
    emit_audit_event(
        "native_lerobot_dataset.bundle.content.read",
        actor=actor_label,
        resource=f"native_lerobot_dataset:{row.id}",
        detail={"bundle_id": bundle.id},
    )
    return FileResponse(
        Path(path),
        media_type="application/zip",
        filename=f"lerobot-{row.dataset_id}-{row.marker_sha256[:12]}.zip",
    )


@router.post("/{dataset_id}/copy/retry")
def retry_native_lerobot_dataset_copy(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "batch:write")
    _raise_legacy_native_lerobot_retired()
    row = _dataset_or_404(db, dataset_id=dataset_id, user=user)
    try:
        result = retry_native_lerobot_copy(
            db,
            dataset_id=row.id,
            actor_id=_actor_id(user),
        )
        job = result.job
        if result.created:
            enqueue_resource_event(
                db,
                resource=job,
                resource_type="job_run",
                event_name="job_run.updated",
                resource_snapshot=job_run_snapshot(job),
            )
        db.commit()
        db.refresh(row)
        db.refresh(job)
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except KeyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=404, detail="native LeRobot dataset does not exist"
        ) from exc
    except (NativeLerobotCopyRetryError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    dispatch = _dispatch_native_copy_after_commit(
        row=row, job=job, created=result.created, user=user
    )
    emit_audit_event(
        "native_lerobot_dataset.copy.retry",
        actor=str(user.get("email") or ""),
        resource=f"native_lerobot_dataset:{row.id}",
        detail={"job_id": job.id, "created": result.created, "dispatch": dispatch},
    )
    schedule_realtime_dispatch()
    return success(
        {
            "native_dataset": native_lerobot_dataset_item(row),
            "job": _copy_job_item(job),
            "dispatch": dispatch,
        }
    )


@router.post("/{dataset_id}/source-reauthorization")
def reauthorize_native_lerobot_dataset_source(
    dataset_id: int,
    body: NativeLerobotSourceReauthorizationRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "batch:write")
    _raise_legacy_native_lerobot_retired()
    row = _dataset_or_404(db, dataset_id=dataset_id, user=user)
    try:
        result = reauthorize_native_lerobot_source(
            db,
            dataset_id=row.id,
            candidate_token=body.candidate_token,
            actor_id=_actor_id(user),
        )
        job = result.job
        if result.created:
            enqueue_resource_event(
                db,
                resource=job,
                resource_type="job_run",
                event_name="job_run.updated",
                resource_snapshot=job_run_snapshot(job),
            )
        db.commit()
        db.refresh(row)
        db.refresh(job)
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except KeyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=404, detail="native LeRobot dataset does not exist"
        ) from exc
    except RedisUnavailableError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503, detail="candidate reference service is unavailable"
        ) from exc
    except (
        NativeLerobotSourceReauthorizationError,
        NativeLerobotCopyRetryError,
        ValueError,
    ) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    dispatch = _dispatch_native_copy_after_commit(
        row=row, job=job, created=result.created, user=user
    )
    emit_audit_event(
        "native_lerobot_dataset.source.reauthorize",
        actor=str(user.get("email") or ""),
        resource=f"native_lerobot_dataset:{row.id}",
        detail={"job_id": job.id, "created": result.created, "dispatch": dispatch},
    )
    schedule_realtime_dispatch()
    return success(
        {
            "native_dataset": native_lerobot_dataset_item(row),
            "job": _copy_job_item(job),
            "dispatch": dispatch,
        }
    )


@router.patch("/{dataset_id}")
def update_native_lerobot_dataset(
    dataset_id: int,
    body: NativeLerobotDatasetUpdateRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:write")
    _raise_legacy_native_lerobot_retired()
    row = _dataset_or_404(db, dataset_id=dataset_id, user=user)
    row = db.scalar(
        select(NativeLerobotDataset).where(NativeLerobotDataset.id == row.id).with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="native LeRobot dataset does not exist")
    if body.status != "archived" or row.status != "active" or row.copy_status != "succeeded":
        raise HTTPException(
            status_code=409, detail="only a successful active platform copy may be archived"
        )
    row.status = "archived"
    # SessionLocal disables autoflush, so persist this transition before the
    # aggregate query decides which child rows remain active.
    db.flush()
    refresh_native_lerobot_batch_status(db, row.batch_id)
    db.commit()
    db.refresh(row)
    emit_audit_event(
        "native_lerobot_dataset.archive",
        actor=str(user.get("email") or ""),
        resource=f"native_lerobot_dataset:{row.id}",
        detail={
            "workspace_id": row.workspace_id,
            "task_set_id": row.task_set_id,
            "status": row.status,
        },
    )
    return success(native_lerobot_dataset_item(row))


def _copy_job_item(job) -> dict[str, object]:
    return {
        "id": job.id,
        "status": job.status,
        "phase": job.phase,
        "progress_percent": max(0, min(100, int(job.progress_percent or 0))),
    }


def _dispatch_native_copy_after_commit(
    *, row: NativeLerobotDataset, job, created: bool, user: dict
) -> str:
    if not created:
        return "already_active"
    try:
        return dispatch_media_job(job, worker_prechecked=True)
    except Exception as exc:
        logger.warning(
            "native copy dispatch failed dataset_id=%s job_id=%s error_type=%s",
            row.id,
            job.id,
            type(exc).__name__,
        )
        emit_audit_event(
            "native_lerobot_dataset.copy.dispatch",
            actor=str(user.get("email") or ""),
            resource=f"native_lerobot_dataset:{row.id}",
            detail={"job_id": job.id, "dispatch": "failed"},
            level="warning",
        )
        return "queued"


def _dispatch_native_bundle_after_commit(
    *, row: NativeLerobotDataset, job, created: bool, user: dict
) -> str:
    if not created or job is None:
        return "already_active"
    try:
        return dispatch_media_job(job, worker_prechecked=True)
    except Exception as exc:
        logger.warning(
            "native bundle dispatch failed dataset_id=%s job_id=%s error_type=%s",
            row.id,
            job.id,
            type(exc).__name__,
        )
        emit_audit_event(
            "native_lerobot_dataset.bundle.dispatch",
            actor=str(user.get("email") or ""),
            resource=f"native_lerobot_dataset:{row.id}",
            detail={"job_id": job.id, "dispatch": "failed"},
            level="warning",
        )
        return "queued"
