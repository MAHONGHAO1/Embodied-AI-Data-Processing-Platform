"""Read and recover durable background jobs without exposing worker internals."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import JOB_STATUS_CANCELLED, JOB_STATUS_FAILED, JobRun, get_db
from data.realtime.outbox import enqueue_resource_event
from data.realtime.projections import job_run_snapshot
from data.realtime.socketio import schedule_realtime_dispatch
from data.security.audit import emit_audit_event
from data.services.job_access import (
    actor_can_access_job,
    actor_can_access_job_resource,
    job_has_consistent_resource_scope,
    job_resource_read_permission,
)
from data.services.job_runs import (
    MAX_RETRIES,
    job_kind_is_recoverable,
    retry_terminal_job_attempt,
)
from data.services.task_dispatcher import (
    JobDispatchUnavailable,
    dispatch_media_job,
    require_celery_worker,
)
from data.utils.formatting import format_api_datetime
from data.utils.helpers import get_current_user, require_permission, success

router = APIRouter(prefix="/jobs", tags=["durable jobs"])

_RETRY_PERMISSION_BY_KIND = {
    "dataset_export": "export:write",
    "import_scan": "import:write",
    "import_materialize": "import:write",
    "import_parse": "import:write",
    "episode_quality": "episode:write",
    "episode_preview": "episode:write",
    "derived_preview_batch": "episode:write",
    "episode_publish": "episode:write",
    "episode_ai_suggestion": "episode:annotate",
}
_PUBLIC_ERROR_MESSAGES = {
    "media_execution_failed": "media job failed",
    "worker_lease_expired": "worker lease expired before completion",
    "cancelled": "job cancelled",
    "unsupported_media_job_kind": "job type is unavailable",
    "resource_fencing_required": "job requires fenced operator recovery",
}
_SAFE_PHASE = re.compile(r"^[a-z0-9_-]{1,64}$")


def _require_resource_read(user: dict, resource_type: str) -> None:
    permission = job_resource_read_permission(resource_type)
    if not permission:
        raise HTTPException(status_code=404, detail="job resource does not exist")
    require_permission(user, permission)


def _require_retry_permission(user: dict, job: JobRun) -> None:
    # Retrying can enqueue media or export work. Restrict it to operational
    # roles even if a narrower read role can inspect job progress.
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="only an admin may retry jobs")
    permission = _RETRY_PERMISSION_BY_KIND.get(job.kind)
    if not permission:
        raise HTTPException(status_code=400, detail="job type does not support retry")
    require_permission(user, permission)
    if not job_kind_is_recoverable(job.kind):
        emit_audit_event(
            "job.retry.denied",
            actor=str(user.get("email") or ""),
            resource=job.id,
            detail={"kind": job.kind, "reason": "resource_fencing_required"},
            level="warning",
        )
        raise HTTPException(status_code=409, detail="job retry requires resource fencing")


def _actor_id(user: dict) -> int | None:
    try:
        return int(user.get("sub") or user.get("id"))
    except (TypeError, ValueError):
        return None


def _public_job(job: JobRun) -> dict[str, object]:
    error_code = str(job.error_code or "")
    terminal_failure = job.status in {JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}
    retryable = (
        terminal_failure
        and job_kind_is_recoverable(job.kind)
        and error_code not in {"unsupported_media_job_kind", "resource_fencing_required"}
    )
    return {
        "id": job.id,
        "kind": job.kind,
        "resource_type": job.resource_type,
        "resource_id": _public_resource_id(job.resource_id),
        "status": job.status,
        "phase": _public_phase(job.phase),
        "progress_percent": max(0, min(100, int(job.progress_percent or 0))),
        "attempt": _logical_attempt(job),
        "max_attempts": MAX_RETRIES + 1,
        "error_code": error_code
        if error_code in _PUBLIC_ERROR_MESSAGES
        else ("job_failed" if terminal_failure else ""),
        "error_message": _public_error_message(job) if terminal_failure else "",
        "retryable": retryable,
        "realtime_version": max(0, int(job.realtime_version or 0)),
        "created_at": format_api_datetime(job.created_at),
        "started_at": format_api_datetime(job.started_at),
        "finished_at": format_api_datetime(job.finished_at),
    }


def _public_resource_id(value: object) -> str:
    text = str(value or "")[:128]
    return text if "/" not in text and "\\" not in text and ".." not in text else "redacted"


def _public_phase(value: object) -> str:
    text = str(value or "")
    return text if _SAFE_PHASE.fullmatch(text) else "working"


def _logical_attempt(job: JobRun) -> int:
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    try:
        return max(1, int(detail.get("retry_attempt") or 1))
    except (TypeError, ValueError):
        return 1


def _public_error_message(job: JobRun) -> str:
    return _PUBLIC_ERROR_MESSAGES.get(str(job.error_code or ""), "job failed")


@router.get("")
def list_jobs(
    resource_type: str = Query(..., min_length=1, max_length=64),
    resource_id: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Return job progress only for one explicitly-authorized resource."""
    _require_resource_read(user, resource_type)
    if not actor_can_access_job_resource(
        db,
        actor_id=_actor_id(user),
        resource_type=resource_type,
        resource_id=resource_id,
    ):
        return success({"items": []})
    jobs = list(
        db.scalars(
            select(JobRun)
            .where(JobRun.resource_type == resource_type, JobRun.resource_id == resource_id)
            .order_by(JobRun.created_at.desc(), JobRun.id.desc())
            .limit(limit)
        )
    )
    return success(
        {
            "items": [
                _public_job(job) for job in jobs if job_has_consistent_resource_scope(db, job=job)
            ]
        }
    )


@router.get("/{job_id}")
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    job = db.get(JobRun, job_id)
    if job is None:
        return JSONResponse(
            status_code=404, content={"code": 404, "message": "job does not exist", "data": None}
        )
    _require_resource_read(user, job.resource_type)
    if not actor_can_access_job(db, actor_id=_actor_id(user), job=job):
        return JSONResponse(
            status_code=404, content={"code": 404, "message": "job does not exist", "data": None}
        )
    return success({"job": _public_job(job)})


@router.post("/{job_id}/retry")
def retry_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    job = db.get(JobRun, job_id)
    if job is None:
        return JSONResponse(
            status_code=404, content={"code": 404, "message": "job does not exist", "data": None}
        )
    _require_resource_read(user, job.resource_type)
    if not actor_can_access_job(db, actor_id=_actor_id(user), job=job):
        return JSONResponse(
            status_code=404, content={"code": 404, "message": "job does not exist", "data": None}
        )
    if job.resource_type == "native_lerobot_direct_source":
        return JSONResponse(
            status_code=409,
            content={
                "code": 409,
                "message": "native LeRobot source retry must use its source retry endpoint",
                "data": None,
            },
        )
    _require_retry_permission(user, job)
    try:
        require_celery_worker(job.queue)
        actor_id = _actor_id(user)
        retry_result = retry_terminal_job_attempt(db, job.id, actor_id=actor_id)
        retry = retry_result.job
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
            dispatch_media_job(retry, worker_prechecked=True)
    except JobDispatchUnavailable:
        return JSONResponse(
            status_code=503,
            content={"code": 503, "message": "job worker is unavailable", "data": None},
        )
    except PermissionError as exc:
        return JSONResponse(
            status_code=403, content={"code": 403, "message": str(exc), "data": None}
        )
    except (KeyError, ValueError) as exc:
        return JSONResponse(
            status_code=409, content={"code": 409, "message": str(exc), "data": None}
        )
    if retry_result.created:
        emit_audit_event(
            "job.retry",
            actor=str(user.get("email") or ""),
            resource=f"job:{job.id}",
            detail={"kind": job.kind, "retry_job_id": retry.id},
        )
        schedule_realtime_dispatch()
    return success({"job": _public_job(retry)})
