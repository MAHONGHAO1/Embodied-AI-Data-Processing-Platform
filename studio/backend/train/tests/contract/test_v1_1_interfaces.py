"""V1.1 Workstreams I–L: hybrid pools, Job immutability, Dataset upsert, artifact kinds."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from quictrain_core import ARTIFACT_KINDS, infer_artifact_kind
from quictrain_model_specs import get_model
from quictrain_provider_local.fake import FakeProvider
from quictrain_scheduler import Scheduler
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.db import JobRecord, init_database
from quictrain_api.schemas import JobCreateRequest, ResourceSelection
from quictrain_api.service import create_job, retry_job, seed_catalog, selectable_resource_profiles


def test_infer_artifact_kind_contract():
    assert infer_artifact_kind("final_model.safetensors") == "checkpoint"
    assert infer_artifact_kind("resolved_config.json") == "config"
    assert infer_artifact_kind("source.json") == "config"
    assert infer_artifact_kind("environment.json") == "config"
    assert infer_artifact_kind("train.log") == "log"
    assert infer_artifact_kind("summary.json") == "summary"
    assert infer_artifact_kind("artifact_manifest.json") == "manifest"
    assert infer_artifact_kind("export-bundle.json") == "export"
    assert infer_artifact_kind("weird.bin") == "metadata"
    assert infer_artifact_kind("checkpoint.safetensors") in ARTIFACT_KINDS


def test_resource_profiles_bind_provider_and_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/hybrid.db")
    monkeypatch.setenv("QUICTRAIN_PROVIDER", "fake")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
        local = session.get(db.ResourceProfileRecord, "act-local-sim")
        h20 = session.get(db.ResourceProfileRecord, "act-h20-standard")
        assert local is not None and local.provider_id == "fake" and local.pool_id == "local-sim"
        assert h20 is not None and h20.provider_id == "aliyun_dlc"
        assert h20.pool_id == "cn-beijing-h20-8"

    model = get_model("mv_act_20260716")
    recipe = model.recipes[0]
    profiles = selectable_resource_profiles(model.id, recipe.resource_profiles)
    assert profiles[0].id == "act-local-sim"
    assert any(p.id == "act-h20-standard" for p in profiles)


def test_scheduler_selects_provider_by_profile(tmp_path, monkeypatch, example_dataset_seed):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/pool.db")
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_PROVIDER", "fake")
    rebind_database()
    init_database()

    fake = FakeProvider()
    dlc_stub = FakeProvider()
    scheduler = Scheduler(
        db.SessionLocal,
        fake,
        providers={"fake": fake, "aliyun_dlc": dlc_stub},
        artifact_root=str(tmp_path / "artifacts"),
    )

    with db.SessionLocal() as session:
        seed_catalog(session)
        local_job = create_job(
            session,
            JobCreateRequest(
                project_id="prj_robot_arm",
                dataset_version_id="dsv_kitchen_v17",
                model_version_id="mv_act_20260716",
                recipe_id="fine_tune",
                config_overrides={"training.steps": 1000, "optimizer.warmup_steps": 10},
                resource_selection=ResourceSelection(mode="AUTO", profile="act-local-sim"),
                client_request_id=f"local-{uuid4().hex}",
            ),
        )
        cloud_job = create_job(
            session,
            JobCreateRequest(
                project_id="prj_robot_arm",
                dataset_version_id="dsv_kitchen_v17",
                model_version_id="mv_act_20260716",
                recipe_id="fine_tune",
                config_overrides={"training.steps": 1000, "optimizer.warmup_steps": 10},
                resource_selection=ResourceSelection(mode="AUTO", profile="act-h20-standard"),
                client_request_id=f"cloud-{uuid4().hex}",
            ),
        )
        local_id, cloud_id = local_job.id, cloud_job.id

    for _ in range(8):
        scheduler.run_once()

    with db.SessionLocal() as session:
        local = session.get(JobRecord, local_id)
        cloud = session.get(JobRecord, cloud_id)
        assert local is not None and cloud is not None
        assert local.attempts[-1].provider == "fake"
        assert cloud.attempts[-1].provider == "aliyun_dlc"
        assert cloud.attempts[-1].provider_payload.get("pool_id") == "cn-beijing-h20-8"
        assert local.state == "SUCCEEDED"
        assert cloud.state == "SUCCEEDED"
        kinds = {a.kind for a in local.artifacts}
        assert "checkpoint" in kinds
        assert "summary" in kinds
        assert "config" in kinds


def test_job_resolved_config_immutable_retry_vs_clone(tmp_path, monkeypatch, example_dataset_seed):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/jobschema.db")
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_AUTH_MODE", "open")
    rebind_database()
    init_database()

    with db.SessionLocal() as session:
        seed_catalog(session)

    from quictrain_api.main import app

    with TestClient(app) as client:
        routes = {route.path for route in app.routes}
        assert "/api/v1/jobs/{job_id}" in routes
        job_methods = {
            method
            for route in app.routes
            if getattr(route, "path", None) == "/api/v1/jobs/{job_id}"
            for method in (getattr(route, "methods", None) or set())
        }
        assert "PATCH" not in job_methods
        assert "PUT" not in job_methods

        create = client.post(
            "/api/v1/jobs",
            json={
                "project_id": "prj_robot_arm",
                "dataset_version_id": "dsv_kitchen_v17",
                "model_version_id": "mv_act_20260716",
                "recipe_id": "fine_tune",
                "config_overrides": {"training.steps": 1000, "optimizer.warmup_steps": 10},
                "resource_selection": {"mode": "AUTO", "profile": "act-local-sim"},
                "client_request_id": f"imm-{uuid4().hex}",
                "display_name": "immutable-job",
            },
        )
        assert create.status_code == 202
        job_id = create.json()["job_id"]
        detail = client.get(f"/api/v1/jobs/{job_id}").json()
        config_hash = detail["config_hash"]
        resolved = detail["resolved_config"]

        # Force FAILED so retry is allowed.
        with db.SessionLocal() as session:
            job = session.get(JobRecord, job_id)
            assert job is not None
            job.state = "FAILED"
            job.failure_category = "TEST"
            session.commit()
            retry_job(session, job)

        after_retry = client.get(f"/api/v1/jobs/{job_id}").json()
        assert after_retry["config_hash"] == config_hash
        assert after_retry["resolved_config"] == resolved

        cloned = client.post(f"/api/v1/jobs/{job_id}/clone")
        assert cloned.status_code == 202
        clone_id = cloned.json()["job_id"]
        assert clone_id != job_id
        clone_detail = client.get(f"/api/v1/jobs/{clone_id}").json()
        assert clone_detail["parent_job_id"] == job_id
        assert clone_detail["config_hash"] == config_hash
        assert clone_detail["user_overrides"] == detail["user_overrides"]


def test_dataset_upsert_requires_fields_and_checksum_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/dsv.db")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_AUTH_MODE", "open")
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)

    from quictrain_api.main import app

    base = {
        "external_dataset_id": "quicdata_contract",
        "external_version_id": "v1",
        "display_name": "Contract DSV",
        "uri": "oss://quicdata/contract/v1",
        "checksum": "generated:contract-v1",
        "episodes": 1,
        "frames": 10,
        "duration_hours": 0.01,
        "fps": 10,
        "robot_type": "pusht",
        "camera_keys": ["observation.image"],
        "action_dim": 2,
        "state_dim": 2,
    }

    with TestClient(app) as client:
        missing = client.post(
            "/api/v1/integrations/data-platform/dataset-versions:upsert",
            headers={"Idempotency-Key": "evt-miss"},
            json={
                "event_id": "evt-miss",
                "event_type": "dataset_version.published",
                "occurred_at": "2026-08-27T00:00:00Z",
                "dataset_version": {
                    "external_dataset_id": "x",
                    "external_version_id": "v1",
                    "display_name": "x",
                    "uri": "oss://x",
                    "checksum": "c",
                },
            },
        )
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "DATASET_VERSION_INVALID"

        ok = client.post(
            "/api/v1/integrations/data-platform/dataset-versions:upsert",
            headers={"Idempotency-Key": "evt-ok"},
            json={
                "event_id": "evt-ok",
                "event_type": "dataset_version.published",
                "occurred_at": "2026-08-27T00:00:00Z",
                "dataset_version": base,
            },
        )
        assert ok.status_code == 200
        dsv_id = ok.json()["dataset_version_id"]

        conflict = client.post(
            "/api/v1/integrations/data-platform/dataset-versions:upsert",
            headers={"Idempotency-Key": "evt-conflict"},
            json={
                "event_id": "evt-conflict",
                "event_type": "dataset_version.published",
                "occurred_at": "2026-08-27T00:00:00Z",
                "dataset_version": {**base, "checksum": "generated:other"},
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IMMUTABLE_VERSION_CONFLICT"

        again = client.post(
            "/api/v1/integrations/data-platform/dataset-versions:upsert",
            headers={"Idempotency-Key": "evt-again"},
            json={
                "event_id": "evt-again",
                "event_type": "dataset_version.published",
                "occurred_at": "2026-08-27T00:00:00Z",
                "dataset_version": base,
            },
        )
        assert again.status_code == 200
        assert again.json() == {"dataset_version_id": dsv_id, "created": False}


def test_dataset_register_oss_uri_for_training_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/dsv_reg.db")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_AUTH_MODE", "open")
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)

    from quictrain_api.main import app

    with TestClient(app) as client:
        bad = client.post(
            "/api/v1/datasets",
            json={
                "client_request_id": "web_reg_bad",
                "display_name": "Bad URI",
                "uri": "https://example.com/ds",
                "version": "v1",
            },
        )
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "DATASET_URI_UNSUPPORTED"

        created = client.post(
            "/api/v1/datasets",
            json={
                "client_request_id": "web_reg_ok",
                "display_name": "OSS Wet Tissue",
                "uri": "oss://oss-pai-demo/quictrain/datasets/lerobot/wet-tissue-v1",
                "version": "v1",
                "dataset_id": "wet-tissue",
                "status": "READY",
                "episodes": 6,
                "frames": 4000,
                "fps": 30,
                "robot_type": "bi_piperx_follower",
                "camera_keys": ["observation.images.laptop", "observation.images.wrist"],
                "action_dim": 14,
                "state_dim": 14,
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["created"] is True
        assert body["status"] == "READY"
        assert body["uri"].startswith("oss://")
        dsv_id = body["dataset_version_id"]

        listed = client.get("/api/v1/datasets")
        assert listed.status_code == 200
        match = next(item for item in listed.json()["items"] if item["id"] == dsv_id)
        assert match["uri"] == body["uri"]
        assert match["status"] == "READY"

        again = client.post(
            "/api/v1/datasets",
            json={
                "client_request_id": "web_reg_again",
                "display_name": "OSS Wet Tissue",
                "uri": "oss://oss-pai-demo/quictrain/datasets/lerobot/wet-tissue-v1",
                "version": "v1",
                "dataset_id": "wet-tissue",
                "checksum": body["checksum"],
            },
        )
        assert again.status_code == 200
        assert again.json()["created"] is False
        assert again.json()["dataset_version_id"] == dsv_id
