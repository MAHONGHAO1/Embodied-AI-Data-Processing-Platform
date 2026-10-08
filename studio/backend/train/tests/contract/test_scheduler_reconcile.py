"""Workstream F — scheduler reconciliation fault injection harness."""

from uuid import uuid4

from quictrain_core import JobState
from quictrain_provider_local.fake import FakeProvider
from quictrain_scheduler import Scheduler
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.db import AttemptRecord, JobRecord, init_database
from quictrain_api.schemas import JobCreateRequest, ResourceSelection
from quictrain_api.service import create_job, seed_catalog


def _create_queued_job(session):
    request = JobCreateRequest(
        project_id="prj_robot_arm",
        dataset_version_id="dsv_kitchen_v17",
        model_version_id="mv_act_20260716",
        recipe_id="fine_tune",
        config_overrides={"training.steps": 1000, "optimizer.warmup_steps": 10},
        resource_selection=ResourceSelection(mode="AUTO", profile="act-h20-standard"),
        client_request_id=f"fault-{uuid4().hex}",
    )
    return create_job(session, request)


def test_idempotent_submit_and_orphan_recovery(tmp_path, monkeypatch, example_dataset_seed):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/fault.db")
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    provider = FakeProvider()
    with db.SessionLocal() as session:
        seed_catalog(session)
        job = _create_queued_job(session)
        job_id = job.id

    scheduler = Scheduler(db.SessionLocal, provider, artifact_root=str(tmp_path / "artifacts"))
    scheduler.run_once()

    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job is not None
        attempt = job.attempts[-1]
        key = attempt.idempotency_key
        external = attempt.external_job_id
        assert external

    again = provider.find_by_idempotency_key(key)
    assert again is not None
    assert again.external_id == external
    assert len(provider._by_external_id) == 1

    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        attempt = session.get(AttemptRecord, job.attempts[-1].id)
        attempt.external_job_id = None
        job.state = JobState.ORPHANED.value
        session.commit()

    scheduler.run_once()
    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job.attempts[-1].external_job_id == external
        assert job.state in {
            JobState.PROVISIONING.value,
            JobState.RUNNING.value,
            JobState.SUCCEEDED.value,
        }


def test_cancel_race_reaches_cancelled(tmp_path, monkeypatch, example_dataset_seed):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/cancel.db")
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    provider = FakeProvider()
    with db.SessionLocal() as session:
        seed_catalog(session)
        job = _create_queued_job(session)
        job_id = job.id

    scheduler = Scheduler(db.SessionLocal, provider, artifact_root=str(tmp_path / "artifacts"))
    scheduler.run_once()

    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        job.state = JobState.CANCEL_REQUESTED.value
        session.commit()

    scheduler.run_once()
    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job.state == JobState.CANCELLED.value


def test_fake_provider_restart_orphans_then_recovers(tmp_path, monkeypatch, example_dataset_seed):
    """Simulate uvicorn --reload: DB keeps active attempts, FakeProvider memory is empty."""

    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/restart.db")
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    provider = FakeProvider()
    with db.SessionLocal() as session:
        seed_catalog(session)
        job = _create_queued_job(session)
        job_id = job.id

    scheduler = Scheduler(db.SessionLocal, provider, artifact_root=str(tmp_path / "artifacts"))
    scheduler.run_once()

    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job is not None
        assert job.state in {
            JobState.PROVISIONING.value,
            JobState.RUNNING.value,
            JobState.SUCCEEDED.value,
        }
        stale_external = job.attempts[-1].external_job_id
        assert stale_external

    # Process restart: new FakeProvider has no in-memory records.
    fresh = FakeProvider()
    restarted = Scheduler(db.SessionLocal, fresh, artifact_root=str(tmp_path / "artifacts"))
    restarted.run_once()

    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job is not None
        assert job.state == JobState.ORPHANED.value

    restarted.run_once()
    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job is not None
        attempt = job.attempts[-1]
        assert attempt.external_job_id
        assert attempt.external_job_id in fresh._by_external_id
        assert job.state in {
            JobState.PROVISIONING.value,
            JobState.RUNNING.value,
            JobState.SUCCEEDED.value,
            JobState.SUBMITTING.value,
        }
        # Same attempt_id ⇒ same fake-{attempt_id} shape after re-submit into empty memory.
        assert attempt.external_job_id == stale_external or attempt.external_job_id.startswith(
            "fake-"
        )
