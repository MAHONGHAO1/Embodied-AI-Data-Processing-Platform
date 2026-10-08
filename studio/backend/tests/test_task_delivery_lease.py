from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from data.database import JobRun
from data.services.job_runs import database_now


@pytest.fixture
def queued_delivery_job(db_session):
    job = JobRun(
        id=uuid4().hex,
        kind="episode_preview",
        resource_type="episode",
        resource_id="1",
        idempotency_key=f"delivery-test:{uuid4().hex}",
        queue="media",
        status="queued",
        phase="queued",
        detail_json={},
    )
    db_session.add(job)
    db_session.commit()
    yield job
    db_session.rollback()
    db_session.query(JobRun).filter(JobRun.id == job.id).delete(synchronize_session=False)
    db_session.commit()


def test_initial_dispatch_acquires_delivery_lease_once(
    db_session, queued_delivery_job, monkeypatch
):
    from data.services import task_dispatcher
    from data.tasks import batch_workers

    published: list[tuple[tuple[str, str], str]] = []
    monkeypatch.setattr(
        batch_workers.batch_execute_task,
        "apply_async",
        lambda *, args, queue, **_kwargs: (
            published.append((args, queue)) or type("Result", (), {"id": "one"})()
        ),
    )

    first = task_dispatcher.dispatch_media_job(queued_delivery_job, worker_prechecked=True)
    second = task_dispatcher.dispatch_media_job(queued_delivery_job, worker_prechecked=True)

    assert first == "celery:one"
    assert second == "already_dispatched"
    assert len(published) == 1
    ((job_id, token), queue) = published[0]
    assert job_id == queued_delivery_job.id
    assert token
    assert queue == "media"
    db_session.expire_all()
    persisted = db_session.get(JobRun, queued_delivery_job.id)
    assert persisted.recovery_dispatch_token == token
    assert persisted.recovery_dispatched_at is not None
    assert persisted.recovery_dispatch_expires_at is not None


def test_broker_failure_releases_only_matching_delivery_lease(
    db_session, queued_delivery_job, monkeypatch
):
    from data.services import task_dispatcher
    from data.tasks import batch_workers

    attempts = 0

    def publish(*, args, queue, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("broker unavailable")
        return type("Result", (), {"id": f"attempt-{attempts}"})()

    monkeypatch.setattr(batch_workers.batch_execute_task, "apply_async", publish)

    with pytest.raises(ConnectionError):
        task_dispatcher.dispatch_media_job(queued_delivery_job, worker_prechecked=True)

    db_session.expire_all()
    failed = db_session.get(JobRun, queued_delivery_job.id)
    assert failed.recovery_dispatch_token == ""
    assert failed.recovery_dispatch_expires_at is None
    assert failed.recovery_dispatched_at is None

    assert (
        task_dispatcher.dispatch_media_job(queued_delivery_job, worker_prechecked=True)
        == "celery:attempt-2"
    )


def test_expired_published_delivery_can_be_dispatched_again(
    db_session, queued_delivery_job, monkeypatch
):
    from data.services import task_dispatcher
    from data.tasks import batch_workers

    tokens: list[str] = []

    def publish(*, args, queue, **_kwargs):
        tokens.append(str(args[1]))
        return type("Result", (), {"id": str(len(tokens))})()

    monkeypatch.setattr(batch_workers.batch_execute_task, "apply_async", publish)

    task_dispatcher.dispatch_media_job(queued_delivery_job, worker_prechecked=True)
    db_session.query(JobRun).filter(JobRun.id == queued_delivery_job.id).update(
        {JobRun.recovery_dispatch_expires_at: database_now(db_session) - timedelta(seconds=1)},
        synchronize_session=False,
    )
    db_session.commit()
    task_dispatcher.dispatch_media_job(queued_delivery_job, worker_prechecked=True)

    assert len(tokens) == 2
    assert tokens[0] != tokens[1]


def test_worker_rejects_stale_delivery_token_without_claiming(
    db_session, queued_delivery_job, monkeypatch
):
    from data.services.job_runs import acquire_delivery_lease, mark_delivery_published
    from data.tasks import batch_workers

    token = acquire_delivery_lease(db_session, queued_delivery_job.id, lease_seconds=60)
    assert token
    assert mark_delivery_published(db_session, queued_delivery_job.id, token=token)

    result = batch_workers.batch_execute_task.run(queued_delivery_job.id, "stale-token")

    assert result == {
        "job_id": queued_delivery_job.id,
        "status": "skipped",
        "reason": "stale_delivery",
    }
    db_session.expire_all()
    assert db_session.get(JobRun, queued_delivery_job.id).status == "queued"


def test_dispatch_succeeds_when_worker_claims_before_publish_marker(
    db_session, queued_delivery_job, monkeypatch
):
    from data.services import task_dispatcher
    from data.tasks import batch_workers

    def publish_then_claim(*, args, queue, **_kwargs):
        job_id, token = args
        db_session.expire_all()
        job = db_session.get(JobRun, job_id)
        job.status = "running"
        job.phase = "running"
        job.recovery_dispatch_token = ""
        job.recovery_dispatch_expires_at = None
        job.recovery_dispatched_at = None
        db_session.commit()
        return type("Result", (), {"id": "fast-worker"})()

    monkeypatch.setattr(batch_workers.batch_execute_task, "apply_async", publish_then_claim)

    assert (
        task_dispatcher.dispatch_media_job(queued_delivery_job, worker_prechecked=True)
        == "celery:fast-worker"
    )


def test_celery_delivery_uses_late_ack_and_single_prefetch():
    from data.celery_app import celery_app

    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.broker_transport_options["visibility_timeout"] >= 3600


def test_delivery_lease_does_not_require_execution_retry_support(
    db_session, queued_delivery_job, monkeypatch
):
    from data.services import task_dispatcher
    from data.tasks import batch_workers

    queued_delivery_job.kind = "dashboard_etl"
    queued_delivery_job.queue = "general"
    db_session.commit()
    monkeypatch.setattr(
        batch_workers.batch_execute_task,
        "apply_async",
        lambda *, args, queue, **_kwargs: type("Result", (), {"id": "dashboard"})(),
    )

    assert (
        task_dispatcher.dispatch_media_job(queued_delivery_job, worker_prechecked=True)
        == "celery:dashboard"
    )
