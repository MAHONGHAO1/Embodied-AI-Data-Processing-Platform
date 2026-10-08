"""Celery entry point for Batch / Episode JobRuns."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event, Thread
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from data.celery_app import celery_app
from data.config import settings
from data.database import (
    JOB_STATUS_QUEUED,
    JOB_STATUS_RETRY_PENDING,
    JobQueueSlot,
    JobRun,
    SessionLocal,
    Workspace,
)
from data.integrations.qrdf.admission import run_qrdf_admission_worker
from data.realtime.outbox import enqueue_resource_event, enqueue_work_queue_invalidated
from data.realtime.projections import job_run_snapshot
from data.services.collection_upload_parse import parse_collection_upload_job
from data.services.import_intake import (
    mark_terminal_import_materialize_failure,
    materialize_import_upload,
    run_import_candidate_scan,
)
from data.services.import_parser import (
    parse_import_session_original,
    run_episode_preview,
    run_episode_quality,
)
from data.services.job_queue_limits import queue_slot_count
from data.services.job_runs import (
    MAX_RETRIES,
    LeaseOwnershipLost,
    NonRetryableJobError,
    claim_job,
    claim_recovery_dispatch,
    complete_job,
    complete_recovered_job,
    defer_delivery_lease,
    fail_job,
    finish_recovery_dispatch,
    job_kind_is_recoverable,
    reclaim_expired_jobs,
    renew_job_lease,
)
from data.services.task_dispatcher import worker_status_snapshot

BatchJobHandler = Callable[[Session, JobRun], dict[str, Any] | None]
_HANDLERS: dict[str, BatchJobHandler] = {}
logger = logging.getLogger("quicdata.batch_workers")
LEASE_SECONDS = 120
HEARTBEAT_INTERVAL_SECONDS = 30
DELIVERY_LEASE_SECONDS = 120
SLOT_BUSY_RETRY_SECONDS = 30
RECOVERY_DISPATCH_BATCH_SIZE = 32


class _LeaseHeartbeat:
    """Renew a durable job lease while blocking storage work is in progress."""

    def __init__(self, *, job_id: str, lease_token: str) -> None:
        self._job_id = job_id
        self._lease_token = lease_token
        self._stop = Event()
        self._lost = Event()
        self._thread = Thread(target=self._run, name=f"job-lease-{job_id[:8]}", daemon=True)

    def __enter__(self) -> _LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._stop.set()
        self._thread.join(timeout=HEARTBEAT_INTERVAL_SECONDS + 1)

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise LeaseOwnershipLost("job lease was lost during execution")

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            db = SessionLocal()
            try:
                if not renew_job_lease(
                    db,
                    self._job_id,
                    lease_token=self._lease_token,
                    lease_seconds=LEASE_SECONDS,
                ):
                    self._lost.set()
                    return
            except Exception as exc:
                # The job remains fenced by its current expiry. A transient
                # database outage is retried at the next heartbeat instead of
                # changing the worker result into a false terminal state.
                logger.warning(
                    "job lease heartbeat failed job_id=%s error_type=%s",
                    self._job_id,
                    type(exc).__name__,
                )
            finally:
                db.close()


def register_batch_handler(kind: str, handler: BatchJobHandler) -> None:
    if not kind.strip():
        raise ValueError("batch job kind is required")
    if kind in _HANDLERS:
        raise ValueError(f"batch job handler already registered: {kind}")
    _HANDLERS[kind] = handler


def _run_import_materialize(db: Session, job: JobRun) -> dict[str, Any]:
    try:
        return materialize_import_upload(db, job)
    except Exception:
        if int(job.retry_count or 0) >= MAX_RETRIES:
            mark_terminal_import_materialize_failure(db, job)
            db.commit()
        raise


def _run_episode_preview(db: Session, job: JobRun) -> dict[str, Any]:
    return run_episode_preview(db, job)


def _run_derived_preview_batch(db: Session, job: JobRun) -> dict[str, Any]:
    from data.services.derived_preview_batch import run_derived_preview_batch

    return run_derived_preview_batch(db, job)


def _run_dataset_export(_db: Session, job: JobRun) -> dict[str, Any]:
    """Run one immutable DatasetRevision export under the current JobRun lease."""
    from data.services.dataset_export_jobs import run_dataset_revision_export

    return run_dataset_revision_export(job.id)


def _run_catalog_export(db: Session, job: JobRun) -> dict[str, Any]:
    from data.services.catalog_export_jobs import run_catalog_export

    return run_catalog_export(db, job)


def _run_native_lerobot_direct_validate(db: Session, job: JobRun) -> dict[str, Any]:
    from data.services.native_lerobot_direct_uploads import run_direct_source_validation

    return run_direct_source_validation(db, job)


def _run_episode_ai_suggestion(db: Session, job: JobRun) -> dict[str, Any]:
    from data.services.episode_multimodal import run_episode_ai_suggestion

    return run_episode_ai_suggestion(db, job)


def _claim(db: Session, job_id: str, *, worker_id: str, delivery_token: str | None = None):
    queued_job = db.get(JobRun, job_id)
    if queued_job is None:
        return None
    return claim_job(
        db,
        job_id,
        worker_id=worker_id,
        delivery_token=delivery_token,
        lease_seconds=LEASE_SECONDS,
        slot_count=queue_slot_count(queued_job.queue),
    )


@celery_app.task(name="quicdata.batch.execute", bind=True, max_retries=None)
def batch_execute_task(self, job_id: str, delivery_token: str | None = None) -> dict[str, Any]:
    """Claim, execute and settle one durable Batch / Episode JobRun."""
    if not isinstance(job_id, str) or not job_id.strip():
        return {"job_id": str(job_id), "status": "failed", "error_code": "invalid_job_id"}
    db = SessionLocal()
    try:
        worker_id = str(
            getattr(self.request, "hostname", "") or f"celery:{self.request.id or 'unknown'}"
        )
        claim = _claim(db, job_id, worker_id=worker_id, delivery_token=delivery_token)
        if claim is None:
            return {"job_id": job_id, "status": "skipped", "reason": "job_not_found"}
        if not claim.claimed or claim.job is None:
            if claim.reason == "queue_slot_busy":
                if delivery_token:
                    defer_delivery_lease(
                        db,
                        job_id,
                        token=delivery_token,
                        delay_seconds=SLOT_BUSY_RETRY_SECONDS,
                    )
            return {"job_id": job_id, "status": "skipped", "reason": claim.reason}

        handler = _HANDLERS.get(claim.job.kind)
        if handler is None:
            failed = fail_job(
                db,
                claim.job.id,
                error_code="unsupported_job_kind",
                message="no registered handler for this job",
                retryable=False,
                lease_token=claim.job.lease_token,
            )
            _enqueue_job_event(db, failed)
            return {"job_id": failed.id, "status": failed.status, "error_code": failed.error_code}

        try:
            # The API emits the queued projection. Emit the durable running
            # projection before the handler can block on storage or QRDF work.
            _enqueue_job_event(db, claim.job)
            with _LeaseHeartbeat(
                job_id=claim.job.id, lease_token=claim.job.lease_token
            ) as heartbeat:
                result = handler(db, claim.job) or {}
                heartbeat.assert_owned()
            follow_up_job_ids = _follow_up_job_ids(result)
            completed = complete_job(
                db, claim.job.id, lease_token=claim.job.lease_token, result=_public_result(result)
            )
            _enqueue_job_event(db, completed)
            _dispatch_follow_up_jobs(follow_up_job_ids)
            return {"job_id": completed.id, "status": completed.status, **_public_result(result)}
        except OperationalError:
            # The handler can have durably published an external completion
            # marker before this worker loses its database completion commit.
            # Do not turn that ambiguous state into an ordinary handler
            # failure: retain the execution lease for expiry recovery, whose
            # catalog-export reconciler verifies the immutable marker/output.
            db.rollback()
            raise
        except LeaseOwnershipLost:
            return {"job_id": claim.job.id, "status": "skipped", "reason": "lease_lost"}
        except Exception as exc:
            retryable = job_kind_is_recoverable(claim.job.kind) and not isinstance(
                exc, NonRetryableJobError
            )
            error_code = f"{claim.job.kind}_failed"[:64]
            if claim.job.kind == "native_lerobot_direct_validate" and isinstance(
                exc, NonRetryableJobError
            ):
                error_code = str(getattr(exc, "code", "") or error_code)[:64]
            failed = fail_job(
                db,
                claim.job.id,
                error_code=error_code,
                message=str(exc) or "job handler failed",
                retryable=retryable,
                lease_token=claim.job.lease_token,
            )
            _enqueue_job_event(db, failed)
            if retryable and failed.status == JOB_STATUS_RETRY_PENDING:
                _dispatch_follow_up_jobs([failed.id], countdown=5)
            return {"job_id": failed.id, "status": failed.status, "error_code": failed.error_code}
    finally:
        db.close()


@celery_app.task(name="quicdata.batch.recover-expired")
def recover_expired_batch_jobs() -> dict[str, Any]:
    """Reclaim crashed workers and redispatch retry-pending jobs."""
    db = SessionLocal()
    reclaimed: list[str] = []
    dispatched: list[str] = []
    try:
        reclaimed = reclaim_expired_jobs(db)
        recovered: list[str] = []
        recovery_failed: list[str] = []
        for job_id in list(reclaimed):
            job = db.get(JobRun, job_id)
            if job is None or job.kind != "catalog_export":
                continue
            from data.services.catalog_export_jobs import (
                recover_catalog_export_delivery,
            )

            try:
                recovered_export = recover_catalog_export_delivery(db, int(job.resource_id))
                complete_recovered_job(
                    db,
                    job_id,
                    result={
                        "export_id": recovered_export.id,
                        "status": "succeeded",
                        "oss_uri": recovered_export.oss_uri,
                        "sha256": recovered_export.sha256,
                        "size_bytes": recovered_export.size_bytes,
                        "attempt": recovered_export.attempt,
                    },
                )
                recovered.append(job_id)
            except Exception:
                # The recovery helper has already persisted a precise failed
                # attempt and cleanup/orphan report. Do not rerun this same
                # output prefix after a failed marker reconciliation.
                current = db.get(JobRun, job_id)
                if current is not None:
                    current.status = "failed"
                    current.phase = "failed"
                    current.error_code = "catalog_export_recovery_failed"
                    current.error_message = (
                        "catalog export completion marker could not be reconciled"
                    )
                    db.commit()
                recovery_failed.append(job_id)
        reclaimed = [
            job_id
            for job_id in reclaimed
            if job_id not in recovered and job_id not in recovery_failed
        ]
        capacity = available_queue_slots(db)
        worker_status = worker_status_snapshot()
        live_queues = {
            str(queue)
            for queue, available in dict(worker_status.get("queues") or {}).items()
            if available
        }
        if not worker_status.get("available"):
            live_queues.clear()
        dispatchable_queues = {queue for queue in live_queues if capacity.get(queue, 0) > 0}
        if not dispatchable_queues:
            return {
                "reclaimed": reclaimed,
                "recovered": recovered,
                "recovery_failed": recovery_failed,
                "dispatched": dispatched,
            }
        pending_by_queue: dict[str, list[str]] = {}
        queue_head_order: list[tuple[object, str, str]] = []
        for queue in sorted(dispatchable_queues):
            queue_limit = min(capacity[queue], RECOVERY_DISPATCH_BATCH_SIZE)
            rows = list(
                db.execute(
                    select(JobRun.id, JobRun.created_at)
                    .where(
                        JobRun.kind.in_(tuple(_HANDLERS)),
                        JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)),
                        JobRun.queue == queue,
                        or_(
                            JobRun.recovery_dispatch_expires_at.is_(None),
                            JobRun.recovery_dispatch_expires_at <= func.current_timestamp(),
                        ),
                    )
                    .order_by(JobRun.created_at.asc(), JobRun.id.asc())
                    .limit(queue_limit)
                )
            )
            if not rows:
                continue
            pending_by_queue[queue] = [str(job_id) for job_id, _created_at in rows]
            first_id, first_created_at = rows[0]
            queue_head_order.append((first_created_at, str(first_id), queue))

        # Take one eligible job from every live queue before taking a second.
        # A saturated media queue therefore cannot keep ingest work outside the
        # global recovery window forever.
        pending_ids: list[str] = []
        queue_order = [queue for _created_at, _job_id, queue in sorted(queue_head_order)]
        positions = dict.fromkeys(queue_order, 0)
        while len(pending_ids) < RECOVERY_DISPATCH_BATCH_SIZE:
            advanced = False
            for queue in queue_order:
                position = positions[queue]
                entries = pending_by_queue[queue]
                if position >= len(entries):
                    continue
                pending_ids.append(entries[position])
                positions[queue] = position + 1
                advanced = True
                if len(pending_ids) >= RECOVERY_DISPATCH_BATCH_SIZE:
                    break
            if not advanced:
                break
        for job_id in pending_ids:
            job = db.get(JobRun, job_id)
            if job is None:
                continue
            if str(job.queue) not in dispatchable_queues:
                continue
            if capacity.get(str(job.queue), 0) <= 0:
                continue
            token = claim_recovery_dispatch(db, job_id, lease_seconds=DELIVERY_LEASE_SECONDS)
            if token is None:
                continue
            try:
                batch_execute_task.apply_async(args=(job.id, token), queue=job.queue)
                finish_recovery_dispatch(db, job.id, token=token, succeeded=True)
                dispatched.append(job.id)
                capacity[str(job.queue)] -= 1
            except Exception as exc:
                finish_recovery_dispatch(db, job.id, token=token, succeeded=False)
                logger.warning(
                    "batch job redispatch failed job_id=%s error_type=%s",
                    job_id,
                    type(exc).__name__,
                )
        return {
            "reclaimed": reclaimed,
            "recovered": recovered,
            "recovery_failed": recovery_failed,
            "dispatched": dispatched,
        }
    finally:
        db.close()


def _dispatch_follow_up_jobs(job_ids: list[str], *, countdown: int | None = None) -> None:
    if not job_ids:
        return
    db = SessionLocal()
    try:
        jobs = list(
            db.scalars(
                select(JobRun).where(
                    JobRun.id.in_(job_ids),
                    JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)),
                )
            )
        )
        for job in jobs:
            from data.services.task_dispatcher import dispatch_media_job

            dispatch_media_job(job, worker_prechecked=True, countdown=countdown)
    finally:
        db.close()


def available_queue_slots(db: Session) -> dict[str, int]:
    """Return durable free slots for queues that currently have pending jobs."""
    queues = {
        str(queue)
        for queue in db.scalars(
            select(JobRun.queue)
            .where(
                JobRun.kind.in_(tuple(_HANDLERS)),
                JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)),
            )
            .distinct()
        )
    }
    active = {
        str(queue): int(count)
        for queue, count in db.execute(
            select(JobQueueSlot.queue, func.count(JobQueueSlot.id))
            .where(
                JobQueueSlot.job_id.is_not(None),
                JobQueueSlot.lease_expires_at > func.current_timestamp(),
            )
            .group_by(JobQueueSlot.queue)
        )
    }
    reserved = {
        str(queue): int(count)
        for queue, count in db.execute(
            select(JobRun.queue, func.count(JobRun.id))
            .where(
                JobRun.kind.in_(tuple(_HANDLERS)),
                JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)),
                JobRun.recovery_dispatch_token != "",
                JobRun.recovery_dispatch_expires_at > func.current_timestamp(),
            )
            .group_by(JobRun.queue)
        )
    }
    return {
        queue: max(0, queue_slot_count(queue) - active.get(queue, 0) - reserved.get(queue, 0))
        for queue in queues
    }


def _enqueue_job_event(db: Session, job: JobRun) -> None:
    enqueue_resource_event(
        db,
        resource=job,
        resource_type="job_run",
        event_name="job_run.updated",
        resource_snapshot=job_run_snapshot(job),
    )
    if (
        job.kind == "episode_preview"
        and job.resource_type == "episode"
        and job.workspace_id is not None
    ):
        workspace = db.get(Workspace, job.workspace_id)
        if workspace is not None:
            enqueue_work_queue_invalidated(db, workspace=workspace)
    db.commit()


def _run_dashboard_etl(db: Session, job: JobRun) -> dict[str, Any]:
    from data.services.dashboard_warehouse import run_dashboard_etl

    return run_dashboard_etl(db, job)


@celery_app.task(name="quicdata.dashboard.etl")
def dashboard_etl_beat_task() -> dict[str, Any]:
    """Periodic enqueue for the global dashboard warehouse rebuild."""
    from data.services.dashboard_scope import DashboardScope
    from data.services.dashboard_warehouse import enqueue_dashboard_etl

    db = SessionLocal()
    try:
        job = enqueue_dashboard_etl(db, scope=DashboardScope.global_scope())
        return {"job_id": job.id, "status": job.status}
    finally:
        db.close()


@celery_app.task(name="quicdata.runtime.retention")
def runtime_retention_beat_task() -> dict[str, int]:
    """Clean one bounded batch of expired runtime rows."""
    from data.services.runtime_retention import cleanup_runtime_rows

    db = SessionLocal()
    try:
        result = cleanup_runtime_rows(
            db,
            realtime_event_retention_days=settings.runtime_realtime_event_retention_days,
            job_retention_days=settings.runtime_job_retention_days,
            import_session_retention_days=settings.runtime_import_session_retention_days,
            batch_size=settings.runtime_retention_batch_size,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        logger.exception("runtime_retention_failed")
        raise
    finally:
        db.close()


def _follow_up_job_ids(result: dict[str, Any]) -> list[str]:
    raw = result.get("_follow_up_job_ids")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item]


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


register_batch_handler("import_parse", parse_import_session_original)
register_batch_handler("collection_upload_parse", parse_collection_upload_job)
register_batch_handler("collection_upload_admission", run_qrdf_admission_worker)
register_batch_handler("import_scan", run_import_candidate_scan)
register_batch_handler("import_materialize", _run_import_materialize)
register_batch_handler("episode_quality", run_episode_quality)
register_batch_handler("episode_preview", _run_episode_preview)
register_batch_handler("derived_preview_batch", _run_derived_preview_batch)
register_batch_handler("dataset_export", _run_dataset_export)
register_batch_handler("catalog_export", _run_catalog_export)
register_batch_handler("native_lerobot_direct_validate", _run_native_lerobot_direct_validate)
register_batch_handler("episode_ai_suggestion", _run_episode_ai_suggestion)
register_batch_handler("dashboard_etl", _run_dashboard_etl)
