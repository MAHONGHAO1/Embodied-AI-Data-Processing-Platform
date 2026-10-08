"""Workstream B — artifact export without proxying checkpoint bytes."""

from pathlib import Path

from fastapi.testclient import TestClient
from quictrain_core import JobState, new_id
from quictrain_scheduler.exporter import Exporter
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.db import ArtifactRecord, AttemptRecord, JobRecord, init_database
from quictrain_api.service import seed_catalog


def test_export_then_download_redirect(tmp_path, monkeypatch, example_dataset_seed):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/exp.db")
    monkeypatch.setenv("QUICTRAIN_EXPORT_ROOT", str(tmp_path / "exports"))
    monkeypatch.setenv("QUICTRAIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_PROVIDER", "fake")
    rebind_database()

    source = tmp_path / "checkpoint"
    source.mkdir()
    blob = source / "model.safetensors"
    blob.write_bytes(b"x" * 4096)

    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
        job = JobRecord(
            id=new_id("job"),
            project_id="prj_robot_arm",
            creator_id="usr_demo",
            client_request_id="export-test",
            display_name="export-test",
            state=JobState.SUCCEEDED.value,
            stage="DONE",
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
        attempt = AttemptRecord(
            id=new_id("att"),
            job_id=job.id,
            number=1,
            state="SUCCEEDED",
            provider="fake",
            idempotency_key=f"{job.id}:attempt:1",
            provider_payload={},
        )
        artifact = ArtifactRecord(
            id=new_id("art"),
            job_id=job.id,
            attempt_id=attempt.id,
            name="model.safetensors",
            kind="checkpoint",
            uri=str(blob),
            size_bytes=4096,
            sha256="",
        )
        session.add_all([job, attempt, artifact])
        session.commit()
        artifact_id = artifact.id

    from quictrain_api.main import app

    with TestClient(app) as client:
        blocked = client.get(f"/api/v1/artifacts/{artifact_id}/download", follow_redirects=False)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "ARTIFACT_EXPORT_REQUIRED"

        created = client.post(f"/api/v1/artifacts/{artifact_id}/export")
        assert created.status_code == 202
        export_id = created.json()["export_id"]

        Exporter(db.SessionLocal).run_once()

        with db.SessionLocal() as session:
            artifact = session.get(ArtifactRecord, artifact_id)
            assert artifact is not None
            assert artifact.export_uri
            assert Path(artifact.export_uri.removeprefix("file://")).exists()

        download = client.get(f"/api/v1/artifacts/{artifact_id}/download", follow_redirects=False)
        assert download.status_code == 307
        assert "local-export.invalid" in download.headers["location"]
        assert (
            export_id
            in client.get(f"/api/v1/artifacts/{artifact_id}/exports").json()["items"][0]["id"]
        )
