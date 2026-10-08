"""Workstream E — ops floor: no fake GPU, quotas, provider disable."""

from fastapi.testclient import TestClient
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.db import init_database
from quictrain_api.errors import ServiceError
from quictrain_api.ops import capacity_snapshot, enforce_submission_limits
from quictrain_api.service import seed_catalog


def test_capacity_has_no_static_fake_gpu(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/ops.db")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)

    from quictrain_api.main import app

    with TestClient(app) as client:
        dashboard = client.get("/api/v1/dashboard").json()
        assert dashboard["gpu_pools"] == []
        assert dashboard["capacity"]["pools"] == []
        resources = client.get("/api/v1/resources").json()
        assert resources["pools"] == []


def test_provider_disable_and_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/quota.db")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_PROVIDER_DISABLED", "true")
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
        try:
            enforce_submission_limits(
                session, project_id="prj_robot_arm", resource_profile_id="act-h20-standard"
            )
            raise AssertionError("expected provider disabled")
        except ServiceError as exc:
            assert exc.code == "PROVIDER_DISABLED"

    assert capacity_snapshot()["provider_disabled"] is True


def test_concurrent_job_and_gpu_quota(tmp_path, monkeypatch, example_dataset_seed):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/quota2.db")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_PROVIDER_DISABLED", "false")
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
        from quictrain_core import JobState, new_id

        from quictrain_api.db import JobRecord, ProjectPolicyRecord

        policy = session.get(ProjectPolicyRecord, "prj_robot_arm")
        assert policy is not None
        policy.max_concurrent_jobs = 1
        policy.max_gpus = 1
        session.add(
            JobRecord(
                id=new_id("job"),
                project_id="prj_robot_arm",
                creator_id="usr_demo",
                client_request_id="active-1",
                display_name="active",
                state=JobState.RUNNING.value,
                stage="TRAIN",
                dataset_version_id="dsv_kitchen_v17",
                model_version_id="mv_act_20260716",
                model_id="act",
                recipe_id="fine_tune",
                resource_profile_id="act-h20-standard",
                schema_hash="h",
                config_hash="c",
                schema_snapshot={},
                user_overrides={},
                resolved_config={},
                source_snapshot={},
            )
        )
        session.commit()
        try:
            enforce_submission_limits(
                session, project_id="prj_robot_arm", resource_profile_id="act-h20-standard"
            )
            raise AssertionError("expected job quota exceeded")
        except ServiceError as exc:
            assert exc.code == "PROJECT_JOB_QUOTA_EXCEEDED"

        policy.max_concurrent_jobs = 8
        policy.max_gpus = 1
        session.commit()
        try:
            enforce_submission_limits(
                session, project_id="prj_robot_arm", resource_profile_id="act-h20-standard"
            )
            raise AssertionError("expected gpu quota exceeded")
        except ServiceError as exc:
            assert exc.code == "PROJECT_GPU_QUOTA_EXCEEDED"
