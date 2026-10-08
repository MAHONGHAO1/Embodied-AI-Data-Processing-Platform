"""End-to-end Formal Close flow: register → materialize → train → export → download."""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from quictrain_scheduler.exporter import Exporter
from quictrain_scheduler.materializer import Materializer
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.db import init_database
from quictrain_api.service import seed_catalog


def test_formal_close_flow_materialize_train_export(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/flow.db")
    monkeypatch.setenv("QUICTRAIN_MATERIALIZATION_ROOT", str(tmp_path / "datasets"))
    monkeypatch.setenv("QUICTRAIN_EXPORT_ROOT", str(tmp_path / "exports"))
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_PROVIDER", "fake")
    monkeypatch.setenv("QUICTRAIN_AUTH_MODE", "open")
    monkeypatch.setenv("QUICTRAIN_OSS_STAGING_ROOT", str(tmp_path / "oss-staging"))
    rebind_database()

    # Stage an "OSS" dataset under the staging root.
    staged = tmp_path / "oss-staging" / "quicdata" / "flow" / "v1"
    staged.mkdir(parents=True)
    (staged / "meta.json").write_text('{"episodes":1}', encoding="utf-8")
    source_uri = "oss://quicdata/flow/v1"

    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)

    from quictrain_provider_local.fake import FakeProvider
    from quictrain_scheduler import Scheduler

    from quictrain_api.main import app

    provider = FakeProvider()
    scheduler = Scheduler(db.SessionLocal, provider, artifact_root=str(tmp_path / "artifacts"))

    with TestClient(app) as client:
        # Ops reservation surface
        ops = client.get("/api/v1/ops/status").json()
        assert "evidence_reservations" in ops
        assert ops["sls"]["status"] == "RESERVED_PENDING_SLS"

        upsert = client.post(
            "/api/v1/integrations/data-platform/dataset-versions:upsert",
            headers={"Idempotency-Key": "evt-flow-1"},
            json={
                "event_id": "evt-flow-1",
                "event_type": "dataset_version.published",
                "occurred_at": "2026-08-20T00:00:00Z",
                "dataset_version": {
                    "external_dataset_id": "quicdata_flow",
                    "external_version_id": "v1",
                    "display_name": "Flow 物化样本",
                    "uri": source_uri,
                    "checksum": "generated:flow-v1",
                    "status": "REGISTERED",
                    "format": "lerobot",
                    "format_version": "3.0",
                    "episodes": 1,
                    "frames": 10,
                    "duration_hours": 0.01,
                    "fps": 10,
                    "robot_type": "pusht",
                    "camera_keys": ["observation.image"],
                    "action_dim": 2,
                    "state_dim": 2,
                    "language_tasks": False,
                },
            },
        )
        assert upsert.status_code == 200
        dsv_id = upsert.json()["dataset_version_id"]

        blocked_id = f"flow-blocked-{uuid4().hex}"
        blocked = client.post(
            "/api/v1/jobs",
            json={
                "project_id": "prj_robot_arm",
                "dataset_version_id": dsv_id,
                "model_version_id": "mv_act_20260716",
                "recipe_id": "fine_tune",
                "config_overrides": {
                    "training.steps": 1000,
                    "optimizer.warmup_steps": 10,
                },
                "resource_selection": {"mode": "AUTO", "profile": "act-h20-standard"},
                "client_request_id": blocked_id,
            },
            headers={"Idempotency-Key": blocked_id},
        )
        # Either 409 DATASET_NOT_READY or 400 JOB_VALIDATION_BLOCKED
        assert blocked.status_code in {400, 409}

        mat = client.post(f"/api/v1/datasets/{dsv_id}/materializations")
        assert mat.status_code == 202
        Materializer(db.SessionLocal).run_once()

        detail = client.get(f"/api/v1/datasets/{dsv_id}").json()
        assert detail["status"] == "READY"
        assert Path(detail["materialized_uri"]).exists()

        req_id = f"flow-ok-{uuid4().hex}"
        created = client.post(
            "/api/v1/jobs",
            json={
                "project_id": "prj_robot_arm",
                "dataset_version_id": dsv_id,
                "model_version_id": "mv_act_20260716",
                "recipe_id": "fine_tune",
                "config_overrides": {
                    "training.steps": 1000,
                    "optimizer.warmup_steps": 10,
                },
                "resource_selection": {"mode": "AUTO", "profile": "act-h20-standard"},
                "client_request_id": req_id,
                "display_name": "flow-e2e",
            },
            headers={"Idempotency-Key": req_id},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        state = None
        deadline = time.time() + 12
        while time.time() < deadline:
            scheduler.run_once()
            state = client.get(f"/api/v1/jobs/{job_id}").json()["state"]
            if state == "SUCCEEDED":
                break
            time.sleep(0.15)
        assert state == "SUCCEEDED"
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        assert job["artifacts"]
        large = next(
            (
                item
                for item in job["artifacts"]
                if item["name"].endswith(".safetensors") or item["size_bytes"] > 100
            ),
            job["artifacts"][0],
        )

        # Force a CPFS-like local path without export_uri for 409 path.
        with db.SessionLocal() as session:
            from quictrain_api.db import ArtifactRecord

            artifact = session.get(ArtifactRecord, large["id"])
            assert artifact is not None
            blob = tmp_path / "cpfs-check" / "model.safetensors"
            blob.parent.mkdir(parents=True)
            blob.write_bytes(b"checkpoint-bytes" * 100)
            artifact.uri = str(blob)
            artifact.export_uri = None
            artifact.size_bytes = blob.stat().st_size
            artifact.sha256 = ""
            artifact.name = "model.safetensors"
            session.commit()
            artifact_id = artifact.id

        denied = client.get(f"/api/v1/artifacts/{artifact_id}/download", follow_redirects=False)
        assert denied.status_code == 409
        exported = client.post(f"/api/v1/artifacts/{artifact_id}/export")
        assert exported.status_code == 202
        assert Exporter(db.SessionLocal).run_once() is True
        with db.SessionLocal() as session:
            from sqlalchemy import select

            from quictrain_api.db import ArtifactExportRecord, ArtifactRecord

            exports = list(
                session.scalars(
                    select(ArtifactExportRecord).where(
                        ArtifactExportRecord.artifact_id == artifact_id
                    )
                )
            )
            artifact = session.get(ArtifactRecord, artifact_id)
            assert artifact is not None
            assert exports, "expected export records"
            assert exports[0].state == "SUCCEEDED", (
                exports[0].state,
                exports[0].error_message,
                exports[0].source_uri,
            )
            assert artifact.export_uri
        download = client.get(f"/api/v1/artifacts/{artifact_id}/download", follow_redirects=False)
        assert download.status_code == 307
        assert "local-export.invalid" in download.headers["location"]

        audits = client.get("/api/v1/admin/audit").json()["items"]
        actions = {item["action"] for item in audits}
        assert "dataset.materialize.succeeded" in actions
        assert "artifact.export.succeeded" in actions
