"""DatasetRevision APIs for the Batch / Episode domain."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from data.database import Dataset, DatasetRevision, JobRun, get_db
from data.realtime.outbox import enqueue_resource_event
from data.realtime.projections import job_run_snapshot
from data.realtime.socketio import schedule_realtime_dispatch
from data.schemas.common import (
    DatasetContainerCreate,
    DatasetRevisionExportCreate,
    DatasetRevisionForDatasetCreate,
)
from data.security.audit import emit_audit_event
from data.services.browser_object_access import issue_browser_download_url
from data.services.dataset_revision_candidates import (
    CandidateLimitExceeded,
    dataset_revision_candidates,
)
from data.services.dataset_revisions import (
    DatasetNameConflict,
    DatasetRevisionCandidatesChanged,
    IdempotencyKeyReused,
    WorkflowConflict,
    create_dataset_container,
    create_dataset_revision_for_dataset,
    enqueue_dataset_export_attempt,
    list_dataset_catalog_entries,
    list_dataset_revisions_for_dataset,
    list_dataset_summaries,
    retire_dataset_revision,
    serialize_dataset_summary,
    serialize_revision_summary,
)
from data.services.dataset_revisions import (
    get_dataset as get_dataset_record,
)
from data.services.job_runs import ResourceFencingRequired, retry_terminal_job_attempt
from data.services.task_dispatcher import (
    JobDispatchUnavailable,
    dispatch_media_job,
    require_celery_worker,
)
from data.services.workspace_access import require_workspace_actor
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success
from data.utils.storage_paths import resolve_storage_path
from data.utils.storage_uri import is_public_storage_uri

router = APIRouter(prefix="/datasets", tags=["数据集"])
revision_router = APIRouter(prefix="/dataset-revisions", tags=["数据集"])
export_router = APIRouter(prefix="/exports", tags=["数据集"])


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub") or user.get("id"))
    except (TypeError, ValueError):
        return None


def _revision_or_404(db: Session, user: dict, revision_id: int) -> DatasetRevision:
    revision = db.get(DatasetRevision, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="dataset revision does not exist")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=revision.workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="dataset revision does not exist") from exc
    return revision


def _dataset_or_404(db: Session, user: dict, dataset_id: int) -> Dataset:
    try:
        dataset = get_dataset_record(db, dataset_id)
    except WorkflowConflict as exc:
        raise HTTPException(status_code=404, detail="dataset does not exist") from exc
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=dataset.workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="dataset does not exist") from exc
    return dataset


def _export_job_or_404(db: Session, user: dict, job_id: str) -> tuple[JobRun, DatasetRevision]:
    job = db.get(JobRun, job_id)
    if job is None or job.kind != "dataset_export" or job.resource_type != "dataset_revision":
        raise HTTPException(status_code=404, detail="dataset export does not exist")
    try:
        revision_id = int(job.resource_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="dataset export does not exist") from exc
    revision = _revision_or_404(db, user, revision_id)
    if job.workspace_id != revision.workspace_id:
        raise HTTPException(status_code=404, detail="dataset export does not exist")
    return job, revision


def _public_job(job: JobRun) -> dict[str, object]:
    return {
        "id": job.id,
        "export_profile": str((job.detail_json or {}).get("export_profile") or ""),
        "status": job.status,
        "phase": job.phase,
        "progress_percent": max(0, min(100, int(job.progress_percent or 0))),
        "error_code": str(job.error_code or "")[:64],
        "realtime_version": int(job.realtime_version or 0),
        "created_at": format_api_datetime(job.created_at),
        "started_at": format_api_datetime(job.started_at),
        "finished_at": format_api_datetime(job.finished_at),
    }


def _export_uri(job: JobRun) -> str:
    value = (job.result_json or {}).get("export_uri") if isinstance(job.result_json, dict) else None
    return str(value) if isinstance(value, str) and is_public_storage_uri(value) else ""


@router.get("")
def list_datasets(
    workspace_id: int = Query(..., gt=0),
    keyword: str | None = Query(default=None, max_length=128),
    created_by_user_id: int | None = Query(default=None, gt=0),
    sort_by: Literal["name", "created_at", "revision_count", "latest_revision_at"] = Query(
        default="created_at"
    ),
    sort_order: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
        items, total = list_dataset_summaries(
            db,
            workspace_id=workspace_id,
            keyword=keyword,
            created_by_user_id=created_by_user_id,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except (ValueError, WorkflowConflict) as exc:
        raise HTTPException(status_code=404, detail="workspace does not exist") from exc
    return success({"items": items, "total": total, "limit": limit, "offset": offset})


@router.get("/catalog")
def list_dataset_catalog(
    workspace_id: int = Query(..., gt=0),
    keyword: str | None = Query(default=None, max_length=128),
    sort_by: Literal["name", "created_at", "revision_count", "latest_revision_at"] = Query(
        default="created_at"
    ),
    sort_order: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """List a safe, unified workspace dataset directory with server pagination."""
    require_permission(user, "dataset:read")
    try:
        require_workspace_actor(db, actor_id=_actor_id(user), workspace_id=workspace_id)
        items, total = list_dataset_catalog_entries(
            db,
            workspace_id=workspace_id,
            keyword=keyword,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except (ValueError, WorkflowConflict) as exc:
        raise HTTPException(status_code=404, detail="workspace does not exist") from exc
    return success({"items": items, "total": total, "limit": limit, "offset": offset})


@router.post("")
def create_dataset_container_api(
    body: DatasetContainerCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:write")
    try:
        dataset = create_dataset_container(
            db,
            workspace_id=body.workspace_id,
            name=body.name,
            description=body.description,
            actor_id=_actor_id(user) or 0,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail="only an operator or admin can create datasets"
        ) from exc
    except DatasetNameConflict as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except WorkflowConflict as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="workspace does not exist") from exc
    emit_audit_event(
        "dataset.create",
        actor=str(user.get("email") or ""),
        resource=f"dataset:{dataset.id}",
        detail={"workspace_id": dataset.workspace_id},
    )
    return success(serialize_dataset_summary(db, dataset.id))


@router.get("/{dataset_id}")
def get_dataset(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    _dataset_or_404(db, user, dataset_id)
    return success(serialize_dataset_summary(db, dataset_id))


@router.get("/{dataset_id}/revision-candidates")
def list_revision_candidates(
    dataset_id: int,
    task_set_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    try:
        return success(
            dataset_revision_candidates(
                db,
                dataset_id=dataset_id,
                task_set_id=task_set_id,
                actor_id=_actor_id(user) or 0,
            )
        )
    except CandidateLimitExceeded as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="workspace access denied") from exc
    except (ValueError, WorkflowConflict) as exc:
        raise HTTPException(
            status_code=404, detail="dataset revision candidates do not exist"
        ) from exc


@router.get("/{dataset_id}/revisions")
def list_revisions_for_dataset(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    _dataset_or_404(db, user, dataset_id)
    return success(
        {
            "items": [
                serialize_revision_summary(db, row.id)
                for row in list_dataset_revisions_for_dataset(db, dataset_id=dataset_id)
            ]
        }
    )


@router.post("/{dataset_id}/revisions")
def create_revision_for_dataset(
    dataset_id: int,
    body: DatasetRevisionForDatasetCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:write")
    dataset = _dataset_or_404(db, user, dataset_id)
    try:
        revision = create_dataset_revision_for_dataset(
            db,
            dataset_id=dataset.id,
            filter_json=body.filter_json,
            items=[item.model_dump() for item in body.items],
            actor_id=_actor_id(user) or 0,
            request_id=str(body.request_id) if body.request_id is not None else None,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail="only an operator or admin can create dataset revisions"
        ) from exc
    except DatasetRevisionCandidatesChanged as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except IdempotencyKeyReused as exc:
        raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"}) from exc
    except WorkflowConflict as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "dataset.revision.create",
        actor=str(user.get("email") or ""),
        resource=f"dataset_revision:{revision.id}",
        detail={"workspace_id": revision.workspace_id, "dataset_id": revision.dataset_id},
    )
    return success(serialize_revision_summary(db, revision.id))


@revision_router.get("/{revision_id}")
def get_revision(
    revision_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:read")
    _revision_or_404(db, user, revision_id)
    return success(serialize_revision_summary(db, revision_id))


@revision_router.post("/{revision_id}/exports")
def export_revision(
    revision_id: int,
    body: DatasetRevisionExportCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "export:write")
    _revision_or_404(db, user, revision_id)
    try:
        require_celery_worker("export")
        enqueue_result = enqueue_dataset_export_attempt(
            db,
            revision_id,
            actor_id=_actor_id(user) or 0,
            export_profile=body.export_profile,
        )
        job = enqueue_result.job
        if enqueue_result.created:
            enqueue_resource_event(
                db,
                resource=job,
                resource_type="job_run",
                event_name="job_run.updated",
                resource_snapshot=job_run_snapshot(job),
            )
            db.commit()
            db.refresh(job)
            dispatch = dispatch_media_job(job, worker_prechecked=True)
        else:
            dispatch = "already_exists"
    except JobDispatchUnavailable as exc:
        raise HTTPException(status_code=503, detail="export worker is unavailable") from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail="only an operator or admin can export dataset revisions"
        ) from exc
    except WorkflowConflict as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "dataset.revision.export",
        actor=str(user.get("email") or ""),
        resource=f"dataset_revision:{revision_id}",
        detail={
            "workspace_id": job.workspace_id,
            "job_id": job.id,
            "export_profile": body.export_profile,
        },
    )
    schedule_realtime_dispatch()
    return success({"job": _public_job(job), "dispatch": dispatch})


@export_router.get("/{job_id}")
def get_export(
    job_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "export:read")
    job, _revision = _export_job_or_404(db, user, job_id)
    return success({"job": _public_job(job), "download_available": bool(_export_uri(job))})


@export_router.get("/{job_id}/download-url")
def export_download_descriptor(
    job_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "export:read")
    job, _revision = _export_job_or_404(db, user, job_id)
    uri = _export_uri(job)
    if not uri:
        raise HTTPException(status_code=404, detail="dataset export is not available")
    profile = str((job.detail_json or {}).get("export_profile") or "dataset")
    access = issue_browser_download_url(
        uri,
        download_name=f"dataset-revision-{job.resource_id}-{profile}.zip",
        media_type="application/zip",
    )
    if access is not None:
        return success({"available": True, **access.as_payload()})
    if resolve_storage_path(uri) is None:
        raise HTTPException(status_code=404, detail="dataset export content is unavailable")
    return success(
        {
            "available": True,
            "direct": False,
            "url": f"/api/v1/exports/{job.id}/content",
            "expires_at": "",
            "media_type": "application/zip",
        }
    )


@export_router.get("/{job_id}/delivery")
def export_delivery_descriptor(
    job_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return a copyable OSS delivery URI after the normal export authorization check.

    Local/NAS exports deliberately remain non-addressable here so this endpoint
    cannot disclose a server-side storage path.
    """
    require_permission(user, "export:read")
    job, revision = _export_job_or_404(db, user, job_id)
    uri = _export_uri(job)
    oss_uri = uri if uri.startswith("oss://") else ""
    if oss_uri:
        emit_audit_event(
            "dataset.revision.delivery_uri.read",
            actor=str(user.get("email") or ""),
            resource=f"dataset_revision:{revision.id}",
            detail={"workspace_id": revision.workspace_id, "job_id": job.id},
        )
    return success({"available": bool(oss_uri), "oss_uri": oss_uri})


@export_router.get("/{job_id}/content")
def stream_local_export(
    job_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "export:read")
    job, _revision = _export_job_or_404(db, user, job_id)
    path = resolve_storage_path(_export_uri(job))
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="dataset export content is unavailable")
    return FileResponse(
        Path(path),
        media_type="application/zip",
        filename=(
            f"dataset-revision-{job.resource_id}-"
            f"{str((job.detail_json or {}).get('export_profile') or 'dataset')}.zip"
        ),
    )


@export_router.post("/{job_id}/retry")
def retry_export(
    job_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "export:write")
    source_job, revision = _export_job_or_404(db, user, job_id)
    try:
        require_celery_worker("export")
        retry_result = retry_terminal_job_attempt(db, source_job.id, actor_id=_actor_id(user) or 0)
        retry = retry_result.job
        if (
            retry.kind != "dataset_export"
            or retry.resource_type != "dataset_revision"
            or retry.resource_id != str(revision.id)
            or retry.workspace_id != revision.workspace_id
        ):
            raise WorkflowConflict("dataset export retry scope is unavailable")
        if retry_result.created:
            enqueue_resource_event(
                db,
                resource=retry,
                resource_type="job_run",
                event_name="job_run.updated",
                resource_snapshot=job_run_snapshot(retry),
            )
            db.commit()
            db.refresh(retry)
            dispatch = dispatch_media_job(retry, worker_prechecked=True)
        else:
            dispatch = "already_active"
    except JobDispatchUnavailable as exc:
        raise HTTPException(status_code=503, detail="export worker is unavailable") from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail="only an operator or admin can retry dataset exports"
        ) from exc
    except (ResourceFencingRequired, ValueError, WorkflowConflict) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    emit_audit_event(
        "dataset.revision.export",
        actor=str(user.get("email") or ""),
        resource=f"dataset_revision:{revision.id}",
        detail={
            "workspace_id": revision.workspace_id,
            "job_id": retry.id,
            "retry_of": source_job.id,
        },
    )
    schedule_realtime_dispatch()
    return success({"job": _public_job(retry), "dispatch": dispatch})


@revision_router.delete("/{revision_id}")
def retire_revision(
    revision_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "dataset:write")
    _revision_or_404(db, user, revision_id)
    try:
        revision = retire_dataset_revision(db, revision_id, actor_id=_actor_id(user) or 0)
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail="only an operator or admin can retire dataset revisions"
        ) from exc
    emit_audit_event(
        "dataset.revision.retire",
        actor=str(user.get("email") or ""),
        resource=f"dataset_revision:{revision.id}",
        detail={"workspace_id": revision.workspace_id, "dataset_id": revision.dataset_id},
    )
    return success(serialize_revision_summary(db, revision.id))
