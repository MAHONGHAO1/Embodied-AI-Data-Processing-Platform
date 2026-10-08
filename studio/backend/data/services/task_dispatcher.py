"""Celery-only dispatch boundary for work that can outlive an HTTP request."""

from __future__ import annotations

import threading
from copy import deepcopy
from time import monotonic

from data.config import settings
from data.database import JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING, JobRun, SessionLocal
from data.services.job_runs import (
    acquire_delivery_lease,
    mark_delivery_published,
    release_delivery_lease,
)

DELIVERY_LEASE_SECONDS = 120
_worker_status_lock = threading.Lock()
_worker_status_cached_at = 0.0
_worker_status_cache: dict | None = None


class JobDispatchUnavailable(RuntimeError):
    """Raised before state mutation when no worker consumes the requested queue."""


def _probe_worker_status() -> dict:
    """Broadcast one bounded Celery inspection and return a public summary."""
    if not settings.celery_broker_url:
        return {"available": False, "worker_count": 0, "queues": {}}
    conn = None
    try:
        from data.celery_app import celery_app

        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1)
        inspect = celery_app.control.inspect(timeout=0.8)
        if not inspect:
            return {"available": False, "worker_count": 0, "queues": {}}
        ping = inspect.ping() or {}
        if not ping:
            return {"available": False, "worker_count": 0, "queues": {}}
        active_queues = inspect.active_queues() or {}
        queue_names = {
            str(item.get("name") or "")
            for worker_name, worker_queues in active_queues.items()
            if worker_name in ping
            for item in worker_queues or []
            if str(item.get("name") or "")
        }
        return {
            "available": True,
            "worker_count": len(ping),
            "queues": dict.fromkeys(sorted(queue_names), True),
        }
    except Exception:
        return {"available": False, "worker_count": 0, "queues": {}}
    finally:
        if conn is not None:
            conn.release()


def _reset_worker_status_cache() -> None:
    """Clear process-local inspection state for tests and explicit recovery."""
    global _worker_status_cache, _worker_status_cached_at
    with _worker_status_lock:
        _worker_status_cache = None
        _worker_status_cached_at = 0.0


def worker_status_snapshot(*, force_refresh: bool = False) -> dict:
    """Return a short-lived Celery worker summary without exposing hostnames."""
    global _worker_status_cache, _worker_status_cached_at
    now = monotonic()
    ttl = float(settings.celery_inspect_cache_seconds)
    with _worker_status_lock:
        if (
            not force_refresh
            and _worker_status_cache is not None
            and now - _worker_status_cached_at < ttl
        ):
            cached = deepcopy(_worker_status_cache)
            cached["cache_age_seconds"] = max(0, int(now - _worker_status_cached_at))
            return cached
        snapshot = _probe_worker_status()
        _worker_status_cache = deepcopy(snapshot)
        _worker_status_cached_at = now
    result = deepcopy(snapshot)
    result["cache_age_seconds"] = 0
    return result


def celery_available(queue: str | None = None) -> bool:
    """Return cached knowledge of whether a live worker consumes ``queue``.

    A broker alone is insufficient: publishing to Redis without a matching worker
    turns an otherwise actionable operation into an invisible stuck job.
    """
    snapshot = worker_status_snapshot()
    if not snapshot["available"]:
        return False
    if queue is None:
        return True
    return bool(snapshot["queues"].get(queue))


def require_celery_worker(queue: str) -> None:
    if not celery_available(queue):
        raise JobDispatchUnavailable(f"{queue} worker is unavailable")


def publish_leased_job(
    job_id: str,
    *,
    queue: str,
    delivery_token: str,
    countdown: int | None = None,
) -> str:
    """Publish one already-fenced JobRun without acquiring a second lease."""
    from data.tasks.batch_workers import batch_execute_task

    options = {"queue": queue}
    if countdown is not None:
        options["countdown"] = max(0, int(countdown))
    async_result = batch_execute_task.apply_async(args=(job_id, delivery_token), **options)
    result_id = str(getattr(async_result, "id", "") or "unknown")
    return f"celery:{result_id}"


def dispatch_media_job(
    job: JobRun,
    *,
    worker_prechecked: bool = False,
    countdown: int | None = None,
) -> str:
    """Enqueue a durable JobRun by ID, never by storage path or callable.

    Callers that have already checked a queue before committing a state change
    set ``worker_prechecked`` so the durable job is always published once it
    exists. A worker going away after that preflight leaves the message in the
    broker for a later worker instead of leaving an unpublished database row.
    """
    if not worker_prechecked:
        require_celery_worker(job.queue)
    job_id = str(job.id)
    queue = str(job.queue)
    db = SessionLocal()
    try:
        token = acquire_delivery_lease(db, job_id, lease_seconds=DELIVERY_LEASE_SECONDS)
        if token is None:
            return "already_dispatched"
        try:
            dispatch_id = publish_leased_job(
                job_id,
                queue=queue,
                delivery_token=token,
                countdown=countdown,
            )
        except Exception:
            release_delivery_lease(db, job_id, token=token)
            raise
        if not mark_delivery_published(db, job_id, token=token):
            db.expire_all()
            current = db.get(JobRun, job_id)
            if current is None or current.status in {JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING}:
                raise JobDispatchUnavailable(
                    "job delivery lease changed before broker publish was recorded"
                )
        return dispatch_id
    finally:
        db.close()


def dispatch_preprocess(task_id: int, operator: str = "system") -> str:
    raise JobDispatchUnavailable(
        "legacy Task preprocessing is not available in the Batch / Episode domain"
    )


def cancel_preprocess_job(task_id: int) -> None:
    raise JobDispatchUnavailable(
        "legacy Task preprocessing is not available in the Batch / Episode domain"
    )


def dispatch_export(job_id: int) -> str:
    raise JobDispatchUnavailable(
        "legacy Task export is not available in the Batch / Episode domain"
    )


def dispatch_preprocess_batch(
    task_ids: list[int],
    operator: str = "system",
    *,
    max_concurrency: int = 1,
) -> dict:
    raise JobDispatchUnavailable(
        "legacy Task preprocessing is not available in the Batch / Episode domain"
    )
