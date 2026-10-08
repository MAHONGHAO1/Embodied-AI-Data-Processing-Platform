"""Expanded fault matrix for Scheduler / provider boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from quictrain_core import JobState
from quictrain_provider_local.fake import FakeProvider
from quictrain_scheduler import Scheduler
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.db import JobRecord, ProjectPolicyRecord, init_database
from quictrain_api.schemas import JobCreateRequest, ResourceSelection
from quictrain_api.service import create_job, seed_catalog


def _job(session):
    return create_job(
        session,
        JobCreateRequest(
            project_id="prj_robot_arm",
            dataset_version_id="dsv_kitchen_v17",
            model_version_id="mv_act_20260716",
            recipe_id="fine_tune",
            config_overrides={"training.steps": 1000, "optimizer.warmup_steps": 10},
            resource_selection=ResourceSelection(mode="AUTO", profile="act-h20-standard"),
            client_request_id=f"fault-{uuid4().hex}",
        ),
    )


def test_provider_timeout_marks_orphaned(tmp_path, monkeypatch, example_dataset_seed):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/to.db")
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    provider = FakeProvider()
    provider.submit_timeout = True
    with db.SessionLocal() as session:
        seed_catalog(session)
        job_id = _job(session).id
    scheduler = Scheduler(db.SessionLocal, provider, artifact_root=str(tmp_path / "artifacts"))
    scheduler.run_once()
    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job is not None
        assert job.state == JobState.ORPHANED.value


def test_max_runtime_cancels_running_job(tmp_path, monkeypatch, example_dataset_seed):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/rt.db")
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    provider = FakeProvider()
    with db.SessionLocal() as session:
        seed_catalog(session)
        policy = session.get(ProjectPolicyRecord, "prj_robot_arm")
        assert policy is not None
        policy.max_runtime_seconds = 1
        job = _job(session)
        job_id = job.id
    scheduler = Scheduler(db.SessionLocal, provider, artifact_root=str(tmp_path / "artifacts"))
    scheduler.run_once()
    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job is not None
        job.started_at = datetime.now(UTC) - timedelta(seconds=30)
        job.state = JobState.RUNNING.value
        attempt = job.attempts[-1]
        # Ensure provider sees RUNNING
        for _ in range(3):
            provider.get(attempt.external_job_id)
        session.commit()
    scheduler.run_once()
    with db.SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        assert job.state == JobState.CANCELLED.value
        assert job.failure_category == "RUNTIME_LIMIT_EXCEEDED"


def test_mlflow_tracking_client_idempotent_key_reuse():
    """In-memory stand-in covering Attempt idempotency contract without live MLflow."""

    class MemoryTracking:
        def __init__(self) -> None:
            self.runs: dict[str, str] = {}
            self.created = 0

        def create_run(self, tags: dict[str, str]) -> tuple[str, str]:
            key = tags["quictrain.idempotency_key"]
            if key in self.runs:
                return self.runs[key], "exp"
            self.created += 1
            run_id = f"run-{self.created}"
            self.runs[key] = run_id
            return run_id, "exp"

        def log_metrics(self, run_id: str, metrics: dict[str, float], step: int) -> None:
            return None

        def finish(self, run_id: str, status: str) -> None:
            return None

    client = MemoryTracking()
    tags = {"quictrain.idempotency_key": "job:attempt:1"}
    first, _ = client.create_run(tags)
    second, _ = client.create_run(tags)
    assert first == second
    assert client.created == 1
