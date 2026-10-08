"""Durable long-running job state and database-backed queue leases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RETRY_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_TERMINAL_STATUSES,
    JobQueueSlot,
    JobRun,
    User,
)
from data.services.job_queue_limits import require_job_kind_queue
from data.services.legacy_retirement import RETIRED_JOB_KINDS

MAX_RETRIES = 1
MAX_ERROR_MESSAGE_LENGTH = 1000
RECOVERABLE_JOB_KINDS = frozenset(
    {
        "import_scan",
        "import_materialize",
        "import_parse",
        "collection_upload_parse",
        "collection_upload_admission",
        "native_lerobot_direct_validate",
        "episode_quality",
        "episode_preview",
        "derived_preview_batch",
        "dataset_export",
        "catalog_export",
        "dashboard_etl",
        "episode_ai_suggestion",
    }
)
UNFENCED_RETRY_PUBLIC_MESSAGE = "job retry requires resource fencing"


@dataclass(frozen=True)
class ClaimResult:
    job: JobRun | None
    claimed: bool
    reason: str = ""


@dataclass(frozen=True)
class ManualRetryResult:
    """The newest retry attempt plus whether this call created it."""

    job: JobRun
    created: bool


class LeaseOwnershipLost(RuntimeError):
    """Raised when a stale worker tries to mutate a reclaimed job."""


class ResourceFencingRequired(ValueError):
    """Raised when a job kind lacks the resource fencing required for retry."""


class NonRetryableJobError(ValueError):
    """A deterministic handler failure which must settle the current JobRun."""


def require_recoverable_job_kind(kind: str) -> None:
    if not job_kind_is_recoverable(kind):
        raise ResourceFencingRequired("resource fencing is required before this job can be retried")


def create_or_get_job(
    db: Session,
    *,
    kind: str,
    resource_type: str,
    resource_id: str | int,
    idempotency_key: str,
    queue: str,
    actor_id: int | None = None,
    workspace_id: int | None = None,
    task_set_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> JobRun:
    """Create a job exactly once and commit the caller's current transaction."""
    job = create_or_get_job_in_transaction(
        db,
        kind=kind,
        resource_type=resource_type,
        resource_id=resource_id,
        idempotency_key=idempotency_key,
        queue=queue,
        actor_id=actor_id,
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        detail=detail,
    )
    db.commit()
    db.refresh(job)
    return job


def create_or_get_job_in_transaction(
    db: Session,
    *,
    kind: str,
    resource_type: str,
    resource_id: str | int,
    idempotency_key: str,
    queue: str,
    actor_id: int | None = None,
    workspace_id: int | None = None,
    task_set_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> JobRun:
    """Create an idempotent job without committing or rolling back the caller.

    Services that create a business row and its durable JobRun together must
    retain control of their outer transaction.  The uniqueness race is limited
    to a savepoint so a collision cannot discard the business row being
    created alongside the job.
    """
    if kind in RETIRED_JOB_KINDS:
        raise ValueError("legacy_workflow_retired")
    _require_nonempty(kind, "kind")
    _require_nonempty(resource_type, "resource_type")
    _require_nonempty(str(resource_id), "resource_id")
    _require_nonempty(idempotency_key, "idempotency_key")
    _require_nonempty(queue, "queue")
    require_job_kind_queue(kind, queue)
    if workspace_id is not None and (
        not isinstance(workspace_id, int) or isinstance(workspace_id, bool) or workspace_id <= 0
    ):
        raise ValueError("workspace_id must be a positive integer")
    if task_set_id is not None and (
        workspace_id is None
        or not isinstance(task_set_id, int)
        or isinstance(task_set_id, bool)
        or task_set_id <= 0
    ):
        raise ValueError("task_set_id requires a workspace and must be a positive integer")

    existing = db.scalar(select(JobRun).where(JobRun.idempotency_key == idempotency_key))
    if existing is not None:
        return existing

    now = database_now(db)
    job = JobRun(
        id=uuid4().hex,
        kind=kind,
        resource_type=resource_type,
        resource_id=str(resource_id),
        idempotency_key=idempotency_key,
        queue=queue,
        actor_id=actor_id,
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        status=JOB_STATUS_QUEUED,
        phase=JOB_STATUS_QUEUED,
        detail_json=dict(detail or {}),
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(JobRun).where(JobRun.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return existing
    return job


def claim_job(
    db: Session,
    job_id: str,
    *,
    worker_id: str,
    delivery_token: str | None = None,
    lease_seconds: int = 120,
    slot_count: int = 1,
) -> ClaimResult:
    """Atomically claim a queued job and one durable queue slot.

    ``slot_count`` is a deployment capacity, not caller-controlled job data. It
    defaults to one so media work remains globally serial until an operator
    explicitly increases the queue capacity.
    """
    _require_nonempty(worker_id, "worker_id")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    if slot_count <= 0:
        raise ValueError("slot_count must be positive")

    now = database_now(db)
    job = db.get(JobRun, job_id)
    if job is None:
        raise KeyError(f"job not found: {job_id}")
    if job.status in JOB_TERMINAL_STATUSES:
        return _unclaimed(db, job, "terminal_status")
    if job.status == JOB_STATUS_RUNNING:
        active = db.scalar(
            select(JobRun.id).where(
                JobRun.id == job.id,
                JobRun.lease_expires_at > func.current_timestamp(),
            )
        )
        reason = "job_lease_active" if active else "job_lease_expired"
        return _unclaimed(db, job, reason)
    if job.status not in {JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING}:
        return _unclaimed(db, job, "job_not_claimable")
    if delivery_token:
        delivery_is_current = db.scalar(
            select(JobRun.id).where(
                JobRun.id == job.id,
                JobRun.recovery_dispatch_token == delivery_token,
                JobRun.recovery_dispatch_expires_at > func.current_timestamp(),
            )
        )
        if delivery_is_current is None:
            return _unclaimed(db, job, "stale_delivery")
    elif str(job.recovery_dispatch_token or ""):
        # Compatibility permits broker messages created before delivery tokens
        # existed, but an untagged message must never overtake a newer delivery.
        return _unclaimed(db, job, "stale_delivery")
    if job.actor_id is not None:
        actor = db.get(User, job.actor_id)
        if actor is None or not actor.is_active:
            _mark_cancelled(db, job, "job actor is inactive", now=now)
            db.commit()
            return ClaimResult(job=job, claimed=False, reason="actor_inactive")

    slot_id = _claim_available_slot(
        db,
        job.queue,
        job_id=job.id,
        worker_id=worker_id,
        slot_count=slot_count,
        now=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
    )
    if slot_id is None:
        return _unclaimed(db, job, "queue_slot_busy")

    lease_expires_at = now + timedelta(seconds=lease_seconds)
    lease_token = uuid4().hex
    claimed = db.execute(
        update(JobRun)
        .where(
            JobRun.id == job.id,
            JobRun.status == job.status,
            JobRun.attempt_count == int(job.attempt_count or 0),
        )
        .values(
            status=JOB_STATUS_RUNNING,
            phase="running",
            attempt_count=int(job.attempt_count or 0) + 1,
            started_at=now,
            finished_at=None,
            lease_worker_id=worker_id,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            recovery_dispatch_token="",
            recovery_dispatch_expires_at=None,
            recovery_dispatched_at=None,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if claimed.rowcount != 1:
        db.rollback()
        current = db.get(JobRun, job_id)
        return ClaimResult(job=current, claimed=False, reason="concurrent_claim")
    db.commit()
    claimed_job = db.get(JobRun, job_id)
    db.refresh(claimed_job)
    return ClaimResult(job=claimed_job, claimed=True)


def update_job_progress(
    db: Session,
    job_id: str,
    *,
    phase: str,
    percent: int,
    lease_token: str,
    detail: dict[str, Any] | None = None,
) -> JobRun:
    """Persist bounded, non-sensitive worker progress for polling clients."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    _require_nonempty(phase, "phase")

    if db.get(JobRun, job_id) is None:
        raise KeyError(f"job not found: {job_id}")
    now = database_now(db)
    values: dict[str, Any] = {
        "phase": phase,
        "progress_percent": percent,
        "updated_at": now,
    }
    if detail is not None:
        values["detail_json"] = dict(detail)
    changed = db.execute(
        update(JobRun).where(*_active_lease_conditions(job_id, lease_token)).values(**values),
        execution_options={"synchronize_session": False},
    )
    if changed.rowcount != 1:
        db.rollback()
        raise LeaseOwnershipLost("job lease no longer owns progress updates")
    db.commit()
    job = db.get(JobRun, job_id)
    db.refresh(job)
    return job


def complete_job(
    db: Session,
    job_id: str,
    *,
    lease_token: str,
    result: dict[str, Any] | None = None,
) -> JobRun:
    """Mark a running job successful and return its queue slot."""
    now = database_now(db)
    changed = db.execute(
        update(JobRun)
        .where(*_active_lease_conditions(job_id, lease_token))
        .values(
            status=JOB_STATUS_SUCCEEDED,
            phase="completed",
            progress_percent=100,
            result_json=dict(result or {}),
            error_code="",
            error_message="",
            lease_worker_id="",
            lease_token="",
            lease_expires_at=None,
            finished_at=now,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if changed.rowcount != 1:
        db.rollback()
        raise LeaseOwnershipLost("job lease no longer owns completion")
    _release_job_slot(db, job_id, now=now)
    db.commit()
    job = db.get(JobRun, job_id)
    db.refresh(job)
    return job


def complete_recovered_job(
    db: Session, job_id: str, *, result: dict[str, Any] | None = None
) -> JobRun:
    """Complete a delivery recovered after its worker lease expired."""
    now = database_now(db)
    changed = db.execute(
        update(JobRun)
        .where(
            JobRun.id == job_id, JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING))
        )
        .values(
            status=JOB_STATUS_SUCCEEDED,
            phase="completed",
            progress_percent=100,
            result_json=dict(result or {}),
            error_code="",
            error_message="",
            lease_worker_id="",
            lease_token="",
            lease_expires_at=None,
            finished_at=now,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if changed.rowcount != 1:
        raise LeaseOwnershipLost("recovered job is no longer claimable")
    _release_job_slot(db, job_id, now=now)
    db.commit()
    job = db.get(JobRun, job_id)
    db.refresh(job)
    return job


def fail_job(
    db: Session,
    job_id: str,
    *,
    error_code: str,
    message: str,
    retryable: bool,
    lease_token: str,
) -> JobRun:
    """Persist one retry at most; subsequent failures become terminal."""
    _require_nonempty(error_code, "error_code")
    job = db.get(JobRun, job_id)
    if job is None:
        raise KeyError(f"job not found: {job_id}")
    now = database_now(db)
    if job_kind_is_recoverable(job.kind) and retryable and int(job.retry_count or 0) < MAX_RETRIES:
        retry_count = int(job.retry_count or 0) + 1
        status = JOB_STATUS_RETRY_PENDING
        finished_at = None
    else:
        retry_count = int(job.retry_count or 0)
        status = JOB_STATUS_FAILED
        finished_at = now
    changed = db.execute(
        update(JobRun)
        .where(
            *_active_lease_conditions(job_id, lease_token),
            JobRun.retry_count == int(job.retry_count or 0),
        )
        .values(
            error_code=error_code[:64],
            error_message=_safe_error_message(message),
            lease_worker_id="",
            lease_token="",
            lease_expires_at=None,
            retry_count=retry_count,
            status=status,
            phase=status,
            finished_at=finished_at,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if changed.rowcount != 1:
        db.rollback()
        raise LeaseOwnershipLost("job lease no longer owns failure transition")
    _release_job_slot(db, job_id, now=now)
    db.commit()
    job = db.get(JobRun, job_id)
    db.refresh(job)
    return job


def cancel_job(
    db: Session,
    job_id: str,
    *,
    message: str = "job cancelled",
    lease_token: str | None = None,
) -> JobRun:
    """Mark a job cancelled after its worker has cooperatively stopped."""
    job = _locked_job(db, job_id)
    if job is None:
        raise KeyError(f"job not found: {job_id}")
    if job.status in JOB_TERMINAL_STATUSES:
        db.commit()
        return job
    if job.status == JOB_STATUS_RUNNING:
        now = database_now(db)
        changed = db.execute(
            update(JobRun)
            .where(*_active_lease_conditions(job_id, lease_token))
            .values(
                status=JOB_STATUS_CANCELLED,
                phase=JOB_STATUS_CANCELLED,
                error_code="cancelled",
                error_message=_safe_error_message(message),
                lease_worker_id="",
                lease_token="",
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        if changed.rowcount != 1:
            db.rollback()
            raise LeaseOwnershipLost("job lease no longer owns cancellation")
        _release_job_slot(db, job_id, now=now)
        db.commit()
        cancelled = db.get(JobRun, job_id)
        db.refresh(cancelled)
        return cancelled
    _mark_cancelled(db, job, message, now=database_now(db))
    db.commit()
    return job


def cancel_queued_jobs_for_resource(
    db: Session,
    *,
    kind: str,
    resource_type: str,
    resource_id: str | int,
    message: str = "job cancelled before execution",
) -> list[str]:
    """Cancel only jobs that have not acquired a worker lease yet."""
    jobs = list(
        db.scalars(
            select(JobRun)
            .where(
                JobRun.kind == kind,
                JobRun.resource_type == resource_type,
                JobRun.resource_id == str(resource_id),
                JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)),
            )
            .with_for_update()
        )
    )
    now = database_now(db)
    for job in jobs:
        _mark_cancelled(db, job, message, now=now)
    db.commit()
    return [job.id for job in jobs]


def cancel_unclaimed_jobs_for_actor(
    db: Session,
    *,
    actor_id: int,
    message: str = "job actor authorization changed before execution",
) -> list[str]:
    """Cancel jobs that have not acquired a lease for one user."""
    jobs = list(
        db.scalars(
            select(JobRun)
            .where(
                JobRun.actor_id == int(actor_id),
                JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)),
            )
            .with_for_update()
        )
    )
    now = database_now(db)
    for job in jobs:
        _mark_cancelled(db, job, message, now=now)
    db.flush()
    return [job.id for job in jobs]


def retry_terminal_job(db: Session, job_id: str, *, actor_id: int | None = None) -> JobRun:
    """Create one durable manual retry attempt for a failed or cancelled job.

    Automatic recovery stays on the original JobRun so its worker lease history
    remains contiguous. A human retry instead creates a distinct job and keeps
    the terminal record immutable for audit and troubleshooting. Repeated UI
    clicks return the newest active attempt instead of publishing duplicates.
    """
    return retry_terminal_job_attempt(db, job_id, actor_id=actor_id).job


def retry_terminal_job_attempt(
    db: Session, job_id: str, *, actor_id: int | None = None
) -> ManualRetryResult:
    """Return the active manual retry or create exactly one new attempt."""
    result = retry_terminal_job_attempt_in_transaction(db, job_id, actor_id=actor_id)
    db.commit()
    db.refresh(result.job)
    return result


def retry_terminal_job_attempt_in_transaction(
    db: Session,
    job_id: str,
    *,
    actor_id: int | None = None,
) -> ManualRetryResult:
    """Return a retry attempt without committing the caller's transaction.

    Resource services use this variant when their own state projection must be
    committed atomically with a new manual JobRun.  The uniqueness race stays
    within a savepoint, so it cannot roll back the caller's outer transaction.
    """
    source = _locked_job(db, job_id)
    if source is None:
        raise KeyError(f"job not found: {job_id}")
    if source.status not in {JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}:
        raise ValueError("only failed or cancelled jobs may be retried")
    require_recoverable_job_kind(source.kind)

    source_detail = dict(source.detail_json or {})
    root_key = str(source_detail.get("retry_root_key") or source.idempotency_key)
    prefix = f"{root_key}:manual-retry:"
    attempts = list(
        db.scalars(
            select(JobRun)
            .where(JobRun.idempotency_key.startswith(prefix))
            .order_by(JobRun.created_at.desc(), JobRun.id.desc())
            .with_for_update()
        )
    )
    latest = attempts[0] if attempts else source
    if latest.status not in JOB_TERMINAL_STATUSES:
        return ManualRetryResult(job=latest, created=False)

    retry_attempt = _manual_retry_attempt(latest) + 1
    detail = dict(source_detail)
    detail.update(
        {
            "retry_root_key": root_key,
            "retry_of_job_id": source.id,
            "retry_attempt": retry_attempt,
        }
    )
    now = database_now(db)
    retry = JobRun(
        id=uuid4().hex,
        kind=source.kind,
        resource_type=source.resource_type,
        resource_id=source.resource_id,
        workspace_id=source.workspace_id,
        task_set_id=source.task_set_id,
        idempotency_key=f"{prefix}{retry_attempt}",
        queue=source.queue,
        actor_id=actor_id,
        status=JOB_STATUS_QUEUED,
        phase=JOB_STATUS_QUEUED,
        detail_json=detail,
        created_at=now,
        updated_at=now,
    )
    db.add(retry)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(JobRun).where(JobRun.idempotency_key == retry.idempotency_key))
        if existing is None:
            raise
        return ManualRetryResult(job=existing, created=False)
    return ManualRetryResult(job=retry, created=True)


def reclaim_expired_jobs(db: Session, *, limit: int = 32) -> list[str]:
    """Release leases left by crashed workers and make at most one retry available."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise ValueError("expired job reclaim limit must be between 1 and 1000")
    now = database_now(db)
    jobs = list(
        db.scalars(
            select(JobRun)
            .where(
                JobRun.status == JOB_STATUS_RUNNING,
                JobRun.lease_expires_at.is_not(None),
                JobRun.lease_expires_at <= func.current_timestamp(),
            )
            .order_by(JobRun.lease_expires_at.asc(), JobRun.created_at.asc(), JobRun.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    reclaimed: list[str] = []
    reclaim_reasons: dict[str, str] = {}
    for job in jobs:
        auto_recoverable = job_kind_is_recoverable(job.kind)
        retryable = auto_recoverable and int(job.retry_count or 0) < MAX_RETRIES
        next_status = JOB_STATUS_RETRY_PENDING if retryable else JOB_STATUS_FAILED
        if job.kind == "episode_ai_suggestion":
            error_code = "ai_worker_lease_expired"
            error_message = "AI worker lease expired before the suggestion completed"
        elif auto_recoverable:
            error_code = "worker_lease_expired"
            error_message = "worker lease expired before the job completed"
        else:
            error_code = "resource_fencing_required"
            error_message = (
                "worker lease expired; resource recovery requires fenced operator handling"
            )
        changed = db.execute(
            update(JobRun)
            .where(
                JobRun.id == job.id,
                JobRun.status == JOB_STATUS_RUNNING,
                JobRun.lease_token == job.lease_token,
                JobRun.lease_expires_at.is_not(None),
                JobRun.lease_expires_at <= func.current_timestamp(),
            )
            .values(
                error_code=error_code,
                error_message=error_message,
                lease_worker_id="",
                lease_token="",
                lease_expires_at=None,
                retry_count=int(job.retry_count or 0) + (1 if retryable else 0),
                status=next_status,
                phase=next_status,
                finished_at=None if retryable else now,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        if changed.rowcount == 1:
            _release_job_slot(db, job.id, now=now)
            reclaimed.append(job.id)
            reclaim_reasons[job.id] = error_code
    db.commit()
    if reclaimed:
        from data.security.audit import emit_audit_event

        for job in jobs:
            if job.id in reclaimed and not job_kind_is_recoverable(job.kind):
                emit_audit_event(
                    "job.recovery_required",
                    resource=job.id,
                    detail={"kind": job.kind, "reason": reclaim_reasons[job.id]},
                    level="warning",
                )
    return reclaimed


def _locked_job(db: Session, job_id: str) -> JobRun | None:
    return db.scalar(select(JobRun).where(JobRun.id == job_id).with_for_update())


def _require_running_job(
    db: Session,
    job_id: str,
    *,
    lease_token: str,
) -> JobRun:
    job = db.scalar(
        select(JobRun).where(*_active_lease_conditions(job_id, lease_token)).with_for_update()
    )
    if job is None:
        raise KeyError(f"job not found: {job_id}")
    return job


def renew_job_lease(
    db: Session,
    job_id: str,
    *,
    lease_token: str,
    lease_seconds: int = 120,
) -> bool:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now = database_now(db)
    expires_at = now + timedelta(seconds=lease_seconds)
    changed = db.execute(
        update(JobRun)
        .where(*_active_lease_conditions(job_id, lease_token))
        .values(lease_expires_at=expires_at, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    if changed.rowcount != 1:
        db.rollback()
        return False
    db.execute(
        update(JobQueueSlot)
        .where(JobQueueSlot.job_id == job_id)
        .values(lease_expires_at=expires_at, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return True


def assert_job_lease(db: Session, job_id: str, *, lease_token: str) -> JobRun:
    job = db.scalar(
        select(JobRun).where(*_active_lease_conditions(job_id, lease_token)).with_for_update()
    )
    if job is None:
        db.rollback()
        raise LeaseOwnershipLost("job lease is no longer active")
    db.commit()
    return job


def _require_lease_token(job: JobRun, lease_token: str | None) -> None:
    if not lease_token or job.lease_token != lease_token:
        raise LeaseOwnershipLost("job lease token no longer owns this attempt")


def _active_lease_conditions(job_id: str, lease_token: str | None) -> tuple[Any, ...]:
    if not lease_token:
        return (JobRun.id == job_id, JobRun.id.is_(None))
    return (
        JobRun.id == job_id,
        JobRun.status == JOB_STATUS_RUNNING,
        JobRun.lease_token == lease_token,
        JobRun.lease_expires_at.is_not(None),
        JobRun.lease_expires_at > func.now(),
    )


def _require_active_lease(job: JobRun, lease_token: str | None, now: datetime) -> None:
    _require_lease_token(job, lease_token)
    if not _lease_is_active(job.lease_expires_at, now):
        raise LeaseOwnershipLost("job lease has expired")


def _claim_available_slot(
    db: Session,
    queue: str,
    *,
    job_id: str,
    worker_id: str,
    slot_count: int,
    now: datetime,
    lease_expires_at: datetime,
) -> int | None:
    """Claim a durable slot with a row-count CAS (also effective on SQLite)."""
    slot_numbers = list(range(1, slot_count + 1))
    for slot_number in slot_numbers:
        _ensure_queue_slot(db, queue, slot_number)

    slots = list(
        db.scalars(
            select(JobQueueSlot)
            .where(
                JobQueueSlot.queue == queue,
                JobQueueSlot.slot_number.in_(slot_numbers),
            )
            .order_by(JobQueueSlot.slot_number)
        )
    )
    if len(slots) != slot_count:
        raise RuntimeError(f"could not initialize queue slots for {queue}")

    for slot in slots:
        claimed = db.execute(
            update(JobQueueSlot)
            .where(
                JobQueueSlot.id == slot.id,
                or_(
                    JobQueueSlot.job_id.is_(None),
                    JobQueueSlot.lease_expires_at.is_(None),
                    JobQueueSlot.lease_expires_at <= func.current_timestamp(),
                ),
            )
            .values(
                job_id=job_id,
                worker_id=worker_id,
                lease_expires_at=lease_expires_at,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        if claimed.rowcount == 1:
            return slot.id
    return None


def _ensure_queue_slot(db: Session, queue: str, slot_number: int) -> None:
    existing = db.scalar(
        select(JobQueueSlot).where(
            JobQueueSlot.queue == queue,
            JobQueueSlot.slot_number == slot_number,
        )
    )
    if existing is not None:
        return
    try:
        with db.begin_nested():
            now = database_now(db)
            db.add(
                JobQueueSlot(
                    queue=queue,
                    slot_number=slot_number,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
    except IntegrityError:
        # Another worker initialized the same slot first; lock it below.
        pass


def _release_job_slot(db: Session, job_id: str, *, now: datetime) -> None:
    slots = list(
        db.scalars(select(JobQueueSlot).where(JobQueueSlot.job_id == job_id).with_for_update())
    )
    for slot in slots:
        _clear_slot(slot, now=now)


def _mark_cancelled(db: Session, job: JobRun, message: str, *, now: datetime) -> None:
    job.status = JOB_STATUS_CANCELLED
    job.phase = JOB_STATUS_CANCELLED
    job.error_code = "cancelled"
    job.error_message = _safe_error_message(message)
    job.lease_worker_id = ""
    job.lease_token = ""
    job.lease_expires_at = None
    job.finished_at = now
    job.updated_at = now
    _release_job_slot(db, job.id, now=now)


def _unclaimed(db: Session, job: JobRun, reason: str) -> ClaimResult:
    """Release a PostgreSQL FOR UPDATE lock before reporting an unclaimed job."""
    db.commit()
    return ClaimResult(job=job, claimed=False, reason=reason)


def _clear_slot(slot: JobQueueSlot, *, now: datetime) -> None:
    slot.job_id = None
    slot.worker_id = ""
    slot.lease_expires_at = None
    slot.updated_at = now


def _lease_is_active(expires_at: datetime | None, now: datetime) -> bool:
    return expires_at is not None and expires_at > now


def database_now(db: Session) -> datetime:
    from datetime import datetime as system_datetime

    value = db.scalar(select(func.current_timestamp()))
    if isinstance(value, system_datetime):
        return value.replace(tzinfo=None)
    return system_datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def mark_dispatch_uncertain(db: Session, job_id: str) -> JobRun:
    """Keep an ambiguously dispatched job recoverable without clobbering a worker claim."""
    now = database_now(db)
    db.execute(
        update(JobRun)
        .where(
            JobRun.id == job_id,
            JobRun.kind.in_(RECOVERABLE_JOB_KINDS),
            JobRun.status == JOB_STATUS_QUEUED,
        )
        .values(
            status=JOB_STATUS_RETRY_PENDING,
            phase=JOB_STATUS_RETRY_PENDING,
            error_code="job_dispatch_uncertain",
            error_message="job dispatch outcome is uncertain",
            finished_at=None,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    db.expire_all()
    job = db.get(JobRun, job_id)
    if job is None:
        raise KeyError(f"job not found: {job_id}")
    db.refresh(job)
    return job


def acquire_delivery_lease(
    db: Session,
    job_id: str,
    *,
    lease_seconds: int = 30,
) -> str | None:
    """Acquire the broker-delivery lease shared by initial and recovery sends."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    token = uuid4().hex
    now = database_now(db)
    changed = db.execute(
        update(JobRun)
        .where(
            JobRun.id == job_id,
            JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)),
            or_(
                JobRun.recovery_dispatch_expires_at.is_(None),
                JobRun.recovery_dispatch_expires_at <= func.current_timestamp(),
            ),
        )
        .values(
            recovery_dispatch_token=token,
            recovery_dispatch_expires_at=now + timedelta(seconds=lease_seconds),
            recovery_dispatched_at=None,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return token if changed.rowcount == 1 else None


def mark_delivery_published(db: Session, job_id: str, *, token: str) -> bool:
    """Mark a matching delivery as published while keeping its expiry fence."""
    now = database_now(db)
    changed = db.execute(
        update(JobRun)
        .where(
            JobRun.id == job_id,
            JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)),
            JobRun.recovery_dispatch_token == token,
            JobRun.recovery_dispatch_expires_at > func.current_timestamp(),
        )
        .values(recovery_dispatched_at=now, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return changed.rowcount == 1


def release_delivery_lease(db: Session, job_id: str, *, token: str) -> bool:
    """Release only the delivery attempt that failed before broker acceptance."""
    now = database_now(db)
    changed = db.execute(
        update(JobRun)
        .where(
            JobRun.id == job_id,
            JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)),
            JobRun.recovery_dispatch_token == token,
        )
        .values(
            recovery_dispatch_token="",
            recovery_dispatch_expires_at=None,
            recovery_dispatched_at=None,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return changed.rowcount == 1


def defer_delivery_lease(db: Session, job_id: str, *, token: str, delay_seconds: int = 30) -> bool:
    """Delay recovery after a delivered message found no durable execution slot."""
    if delay_seconds <= 0:
        raise ValueError("delay_seconds must be positive")
    now = database_now(db)
    changed = db.execute(
        update(JobRun)
        .where(
            JobRun.id == job_id,
            JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)),
            JobRun.recovery_dispatch_token == token,
        )
        .values(
            recovery_dispatch_expires_at=now + timedelta(seconds=delay_seconds),
            recovery_dispatched_at=now,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return changed.rowcount == 1


# Compatibility names for callers and migrations created before the dispatch
# fence covered initial delivery as well as Beat recovery.
claim_recovery_dispatch = acquire_delivery_lease


def finish_recovery_dispatch(db: Session, job_id: str, *, token: str, succeeded: bool) -> bool:
    if succeeded:
        return mark_delivery_published(db, job_id, token=token)
    return release_delivery_lease(db, job_id, token=token)


def release_recovery_dispatch(db: Session, job_id: str) -> bool:
    job = db.get(JobRun, job_id)
    token = str(job.recovery_dispatch_token or "") if job is not None else ""
    if not token:
        return False
    return release_delivery_lease(db, job_id, token=token)


def _safe_error_message(message: str) -> str:
    return str(message).replace("\n", " ").replace("\r", " ")[:MAX_ERROR_MESSAGE_LENGTH]


def job_kind_is_recoverable(kind: str) -> bool:
    return kind in RECOVERABLE_JOB_KINDS


def _manual_retry_attempt(job: JobRun) -> int:
    detail = job.detail_json if isinstance(job.detail_json, dict) else {}
    try:
        return max(1, int(detail.get("retry_attempt") or 1))
    except (TypeError, ValueError):
        return 1


def _require_nonempty(value: str, name: str) -> None:
    if not str(value).strip():
        raise ValueError(f"{name} is required")
