from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from data.database import JobRun
from data.services.job_runs import ClaimResult


@pytest.fixture(autouse=True)
def available_media_worker(monkeypatch):
    from data.tasks import batch_workers

    monkeypatch.setattr(
        batch_workers,
        "worker_status_snapshot",
        lambda: {"available": True, "worker_count": 1, "queues": {"media": True}},
        raising=False,
    )


@pytest.fixture
def make_queued_job(db_session):
    created_ids: list[str] = []

    def make(*, queue: str = "media", kind: str = "episode_preview") -> JobRun:
        job = JobRun(
            id=uuid4().hex,
            kind=kind,
            resource_type="episode",
            resource_id="1",
            idempotency_key=f"recovery-test:{uuid4().hex}",
            queue=queue,
            status="queued",
            phase="queued",
            detail_json={},
        )
        db_session.add(job)
        db_session.commit()
        created_ids.append(job.id)
        return job

    yield make

    db_session.rollback()
    db_session.query(JobRun).filter(JobRun.id.in_(created_ids)).delete(synchronize_session=False)
    db_session.commit()


def _scope_recovery_claim(monkeypatch, batch_workers, job_id: str) -> None:
    original = batch_workers.claim_recovery_dispatch

    def claim(db, candidate_id, **kwargs):
        if str(candidate_id) != str(job_id):
            return None
        return original(db, candidate_id, **kwargs)

    monkeypatch.setattr(batch_workers, "claim_recovery_dispatch", claim)


def _allow_queue_recovery(monkeypatch, batch_workers, queue: str) -> None:
    monkeypatch.setattr(batch_workers, "available_queue_slots", lambda _db: {queue: 1})
    monkeypatch.setattr(
        batch_workers,
        "worker_status_snapshot",
        lambda: {"available": True, "worker_count": 1, "queues": {queue: True}},
    )


def test_recovery_dispatches_queued_job_only_once(db_session, make_queued_job, monkeypatch):
    from data.tasks import batch_workers

    queue = f"m-{uuid4().hex[:12]}"
    job = make_queued_job(queue=queue)
    dispatched: list[tuple[str, str]] = []
    _allow_queue_recovery(monkeypatch, batch_workers, queue)
    _scope_recovery_claim(monkeypatch, batch_workers, job.id)
    monkeypatch.setattr(batch_workers, "reclaim_expired_jobs", lambda _db: [])
    monkeypatch.setattr(
        batch_workers.batch_execute_task,
        "apply_async",
        lambda *, args, queue: dispatched.append((str(args[0]), str(queue))),
    )

    batch_workers.recover_expired_batch_jobs()
    batch_workers.recover_expired_batch_jobs()

    assert dispatched == [(job.id, queue)]
    db_session.expire_all()
    assert db_session.get(JobRun, job.id).recovery_dispatched_at is not None


def test_recovery_releases_fence_when_broker_publish_fails(
    db_session, make_queued_job, monkeypatch
):
    from data.tasks import batch_workers

    queue = f"m-{uuid4().hex[:12]}"
    job = make_queued_job(queue=queue)
    attempts: list[str] = []
    _allow_queue_recovery(monkeypatch, batch_workers, queue)
    _scope_recovery_claim(monkeypatch, batch_workers, job.id)
    monkeypatch.setattr(batch_workers, "reclaim_expired_jobs", lambda _db: [])

    def fail_once(*, args, queue):
        attempts.append(str(args[0]))
        if len(attempts) == 1:
            raise ConnectionError("broker unavailable")

    monkeypatch.setattr(batch_workers.batch_execute_task, "apply_async", fail_once)

    batch_workers.recover_expired_batch_jobs()
    db_session.expire_all()
    assert db_session.get(JobRun, job.id).recovery_dispatched_at is None

    batch_workers.recover_expired_batch_jobs()
    assert attempts == [job.id, job.id]
    db_session.expire_all()
    assert db_session.get(JobRun, job.id).recovery_dispatched_at is not None


def test_recovery_does_not_starve_live_queue_behind_unavailable_queue_backlog(
    db_session, make_queued_job, monkeypatch
):
    from data.tasks import batch_workers

    for _ in range(batch_workers.RECOVERY_DISPATCH_BATCH_SIZE):
        queued = make_queued_job(queue="ingest", kind="import_parse")
        queued.created_at = datetime(2000, 1, 1)
    target_queue = f"m-{uuid4().hex[:12]}"
    target = make_queued_job(queue=target_queue)
    db_session.commit()

    dispatched: list[str] = []
    _allow_queue_recovery(monkeypatch, batch_workers, target_queue)
    _scope_recovery_claim(monkeypatch, batch_workers, target.id)
    monkeypatch.setattr(batch_workers, "reclaim_expired_jobs", lambda _db: [])
    monkeypatch.setattr(
        batch_workers.batch_execute_task,
        "apply_async",
        lambda *, args, queue: dispatched.append(str(args[0])),
    )

    result = batch_workers.recover_expired_batch_jobs()

    assert dispatched == [target.id]
    assert result["dispatched"] == [target.id]


def test_recovery_fairly_dispatches_each_live_capacity_limited_queue(
    db_session,
    make_queued_job,
    monkeypatch,
):
    """One full live queue must not hide another live queue behind the batch window."""
    from data.tasks import batch_workers

    media_queue = f"m-{uuid4().hex[:12]}"
    ingest_queue = f"i-{uuid4().hex[:12]}"
    media_jobs = [
        make_queued_job(queue=media_queue)
        for _ in range(batch_workers.RECOVERY_DISPATCH_BATCH_SIZE)
    ]
    for job in media_jobs:
        job.created_at = datetime(2000, 1, 1)
    ingest_target = make_queued_job(queue=ingest_queue, kind="import_parse")
    db_session.commit()

    original_claim = batch_workers.claim_recovery_dispatch

    def claim_only_fixture_jobs(db, candidate_id, **kwargs):
        if str(candidate_id) != ingest_target.id and str(candidate_id) not in {
            job.id for job in media_jobs
        }:
            return None
        return original_claim(db, candidate_id, **kwargs)

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(batch_workers, "reclaim_expired_jobs", lambda _db: [])
    monkeypatch.setattr(
        batch_workers,
        "available_queue_slots",
        lambda _db: {media_queue: 1, ingest_queue: 1},
    )
    monkeypatch.setattr(
        batch_workers,
        "worker_status_snapshot",
        lambda: {
            "available": True,
            "worker_count": 2,
            "queues": {media_queue: True, ingest_queue: True},
        },
    )
    monkeypatch.setattr(batch_workers, "claim_recovery_dispatch", claim_only_fixture_jobs)
    monkeypatch.setattr(
        batch_workers.batch_execute_task,
        "apply_async",
        lambda *, args, queue, **_kwargs: dispatched.append((str(args[0]), str(queue))),
    )

    result = batch_workers.recover_expired_batch_jobs()

    assert len(dispatched) == 2
    assert dispatched[0][1] == media_queue
    assert dispatched[1] == (ingest_target.id, ingest_queue)
    assert result["dispatched"] == [dispatched[0][0], ingest_target.id]


def test_slot_busy_returns_without_celery_retry(db_session, make_queued_job, monkeypatch):
    from data.tasks import batch_workers

    job = make_queued_job()
    job.recovery_dispatch_token = "delivery-token"
    job.recovery_dispatched_at = job.created_at
    job.recovery_dispatch_expires_at = job.created_at
    db_session.commit()
    monkeypatch.setattr(
        batch_workers,
        "_claim",
        lambda _db, _job_id, *, worker_id, delivery_token=None: ClaimResult(
            job=job,
            claimed=False,
            reason="queue_slot_busy",
        ),
    )

    def forbidden_retry(*_args, **_kwargs):
        raise AssertionError("queue slot contention must not create a Celery retry message")

    monkeypatch.setattr(batch_workers.batch_execute_task, "retry", forbidden_retry)

    result = batch_workers.batch_execute_task.run(job.id, "delivery-token")

    assert result == {"job_id": job.id, "status": "skipped", "reason": "queue_slot_busy"}
    db_session.expire_all()
    persisted = db_session.get(JobRun, job.id)
    assert persisted.recovery_dispatched_at is not None
    assert persisted.recovery_dispatch_expires_at > persisted.recovery_dispatched_at


def test_terminal_handler_failure_does_not_consume_a_retry(
    db_session, make_queued_job, monkeypatch
):
    """A deterministic source-limit failure must not rescan the same OSS scope."""
    from data.services.import_intake import ImportScanCandidateLimitError
    from data.tasks import batch_workers

    job = make_queued_job(queue="ingest", kind="import_scan")

    def terminal_scan_failure(_db, _job):
        raise ImportScanCandidateLimitError("import scan exceeds the candidate limit")

    monkeypatch.setitem(batch_workers._HANDLERS, "import_scan", terminal_scan_failure)

    result = batch_workers.batch_execute_task.run(job.id)

    assert result == {
        "job_id": job.id,
        "status": "failed",
        "error_code": "import_scan_failed",
    }
    db_session.expire_all()
    persisted = db_session.get(JobRun, job.id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.retry_count == 0


def test_recovery_dispatch_is_bounded_by_batch_limit(db_session, make_queued_job, monkeypatch):
    from data.tasks import batch_workers

    jobs = [make_queued_job() for _ in range(3)]
    for index, job in enumerate(jobs):
        job.created_at = datetime(2000, 1, 1) + timedelta(seconds=index)
    db_session.commit()
    dispatched: list[str] = []
    monkeypatch.setattr(batch_workers, "RECOVERY_DISPATCH_BATCH_SIZE", 2)
    monkeypatch.setattr(batch_workers, "reclaim_expired_jobs", lambda _db: [])
    monkeypatch.setattr(batch_workers, "available_queue_slots", lambda _db: {"media": 5})
    monkeypatch.setattr(
        batch_workers.batch_execute_task,
        "apply_async",
        lambda *, args, queue, **_kwargs: dispatched.append(str(args[0])),
    )

    batch_workers.recover_expired_batch_jobs()

    assert dispatched == [job.id for job in jobs[:2]]


def test_expired_execution_lease_reclaim_is_bounded_and_stable(db_session, make_queued_job):
    from data.services.job_runs import reclaim_expired_jobs

    jobs = [make_queued_job() for _ in range(3)]
    expired_at = datetime(2000, 1, 1)
    for index, job in enumerate(jobs):
        job.status = "running"
        job.phase = "running"
        job.lease_token = f"lease-{index}"
        job.lease_expires_at = expired_at
        job.created_at = expired_at + timedelta(seconds=index)
    db_session.commit()

    reclaimed = reclaim_expired_jobs(db_session, limit=2)

    assert reclaimed == [job.id for job in jobs[:2]]
    db_session.expire_all()
    assert db_session.get(JobRun, jobs[2].id).status == "running"


def test_recovery_does_not_publish_without_free_queue_slots(
    db_session, make_queued_job, monkeypatch
):
    from data.tasks import batch_workers

    make_queued_job()
    monkeypatch.setattr(batch_workers, "reclaim_expired_jobs", lambda _db: [])
    monkeypatch.setattr(batch_workers, "available_queue_slots", lambda _db: {"media": 0})

    def forbidden_publish(**_kwargs):
        raise AssertionError("recovery must not publish beyond durable queue capacity")

    monkeypatch.setattr(batch_workers.batch_execute_task, "apply_async", forbidden_publish)

    result = batch_workers.recover_expired_batch_jobs()

    assert result["dispatched"] == []


def test_delivery_lease_reserves_queue_capacity(db_session, make_queued_job, monkeypatch):
    from data.tasks import batch_workers

    queue = "reserved-test"
    leased = make_queued_job(queue=queue)
    make_queued_job(queue=queue)
    leased.recovery_dispatch_token = "delivery-token"
    leased.recovery_dispatched_at = datetime.utcnow()
    leased.recovery_dispatch_expires_at = datetime.utcnow() + timedelta(minutes=5)
    db_session.commit()
    monkeypatch.setattr(
        batch_workers, "queue_slot_count", lambda candidate: 2 if candidate == queue else 1
    )

    assert batch_workers.available_queue_slots(db_session)[queue] == 1


def test_recovery_does_not_publish_without_target_queue_worker(
    db_session,
    make_queued_job,
    monkeypatch,
):
    from data.tasks import batch_workers

    make_queued_job()
    monkeypatch.setattr(batch_workers, "reclaim_expired_jobs", lambda _db: [])
    monkeypatch.setattr(batch_workers, "available_queue_slots", lambda _db: {"media": 1})
    monkeypatch.setattr(
        batch_workers,
        "worker_status_snapshot",
        lambda: {"available": True, "worker_count": 1, "queues": {"ingest": True}},
        raising=False,
    )

    published: list[str] = []
    monkeypatch.setattr(
        batch_workers.batch_execute_task,
        "apply_async",
        lambda *, args, **_kwargs: published.append(str(args[0])),
    )

    result = batch_workers.recover_expired_batch_jobs()

    assert published == []
    assert result["dispatched"] == []
