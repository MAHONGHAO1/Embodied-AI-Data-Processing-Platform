"""Unit coverage for newly filled Formal Close gaps."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from quictrain_core import new_id
from quictrain_scheduler.exporter import EXPORT_SUCCEEDED, gc_expired_exports
from quictrain_scheduler.materializer import Materializer, request_materialization
from tests.db_helpers import rebind_database

import quictrain_api.db as db
from quictrain_api.alerts import emit_alert, sls_reservation_status
from quictrain_api.auth import resolve_oidc_claims
from quictrain_api.capacity import StaticConfiguredProbe, capacity_snapshot
from quictrain_api.db import ArtifactExportRecord, ArtifactRecord, init_database
from quictrain_api.errors import ServiceError
from quictrain_api.service import seed_catalog


def test_staging_oss_materialization(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/oss.db")
    monkeypatch.setenv("QUICTRAIN_MATERIALIZATION_ROOT", str(tmp_path / "datasets"))
    monkeypatch.setenv("QUICTRAIN_OSS_STAGING_ROOT", str(tmp_path / "staging"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    staged = tmp_path / "staging" / "bucket" / "ds" / "v1"
    staged.mkdir(parents=True)
    (staged / "data.bin").write_bytes(b"abc")
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
        from quictrain_core import DatasetReadiness

        from quictrain_api.db import DatasetVersionRecord

        record = DatasetVersionRecord(
            id="dsv_oss_stage",
            project_id="prj_robot_arm",
            external_dataset_id="bucket_ds",
            name="oss",
            version="v1",
            status=DatasetReadiness.REGISTERED.value,
            format="lerobot",
            format_version="3.0",
            uri="oss://bucket/ds/v1",
            checksum="generated:oss",
            manifest={
                "id": "dsv_oss_stage",
                "dataset_id": "bucket_ds",
                "name": "oss",
                "version": "v1",
                "status": "REGISTERED",
                "format": "lerobot",
                "format_version": "3.0",
                "uri": "oss://bucket/ds/v1",
                "checksum": "generated:oss",
                "episodes": 1,
                "frames": 1,
                "duration_hours": 0.01,
                "fps": 10,
                "robot_type": "pusht",
                "camera_keys": ["observation.image"],
                "action_dim": 2,
                "state_dim": 2,
                "language_tasks": False,
            },
        )
        session.add(record)
        session.commit()
        request_materialization(session, record, actor_id="usr_demo")
        session.commit()
    Materializer(db.SessionLocal).run_once()
    with db.SessionLocal() as session:
        from quictrain_core import DatasetReadiness

        from quictrain_api.db import DatasetVersionRecord

        record = session.get(DatasetVersionRecord, "dsv_oss_stage")
        assert record.status == DatasetReadiness.READY.value
        assert Path(record.materialized_uri).joinpath("data.bin").exists()


def test_export_gc_expires_old_exports(tmp_path, monkeypatch, example_dataset_seed):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/gc.db")
    monkeypatch.setenv("QUICTRAIN_EXPORT_ROOT", str(tmp_path / "exports"))
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    rebind_database()
    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
        # Minimal job/attempt/artifact for FK
        from quictrain_core import JobState

        from quictrain_api.db import AttemptRecord, JobRecord

        job = JobRecord(
            id=new_id("job"),
            project_id="prj_robot_arm",
            creator_id="usr_demo",
            client_request_id="gc",
            display_name="gc",
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
            idempotency_key=f"{job.id}:1",
        )
        artifact = ArtifactRecord(
            id=new_id("art"),
            job_id=job.id,
            attempt_id=attempt.id,
            name="x.bin",
            kind="file",
            uri="/tmp/x",
            export_uri="file:///tmp/export",
            size_bytes=1,
            sha256="",
            retention_days=1,
        )
        export = ArtifactExportRecord(
            id=new_id("exp"),
            artifact_id=artifact.id,
            job_id=job.id,
            attempt_id=attempt.id,
            state=EXPORT_SUCCEEDED,
            source_uri="/tmp/x",
            export_uri="file:///tmp/export",
            checksum="",
            bytes_copied=1,
            actor_id="usr_demo",
            retention_days=1,
            finished_at=datetime.now(UTC) - timedelta(days=3),
        )
        session.add_all([job, attempt, artifact, export])
        session.commit()
        export_id = export.id
        artifact_id = artifact.id
        export_dir = Path(tmp_path / "exports" / export_id)
        export_dir.mkdir(parents=True)
        (export_dir / "x.bin").write_bytes(b"1")

    with db.SessionLocal() as session:
        purged = gc_expired_exports(session)
        session.commit()
        assert export_id in purged
        artifact = session.get(ArtifactRecord, artifact_id)
        assert artifact.export_uri is None
        export = session.get(ArtifactExportRecord, export_id)
        assert export.state == "EXPIRED"
    assert not export_dir.exists()


def test_alert_and_capacity_and_oidc_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/ops2.db")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_CAPACITY_DISCOVERY_MODE", "static_config")
    monkeypatch.setenv("QUICTRAIN_CAPACITY_TOTAL_GPUS", "4")
    monkeypatch.setenv("QUICTRAIN_CAPACITY_GPU_MODEL", "H20")
    rebind_database()
    snap = capacity_snapshot()
    assert snap["pools"]
    assert snap["pools"][0]["available"] is None
    assert StaticConfiguredProbe().discover()[0].total == 4

    result = emit_alert(severity="info", title="t")
    assert result["delivered"] is False
    assert sls_reservation_status()["status"] == "RESERVED_PENDING_SLS"

    monkeypatch.setenv("QUICTRAIN_AUTH_MODE", "oidc")
    monkeypatch.setenv("QUICTRAIN_OIDC_ISSUER", "https://idp.example/realms/quic")
    monkeypatch.setenv("QUICTRAIN_OIDC_AUDIENCE", "quictrain")
    monkeypatch.setenv("QUICTRAIN_OIDC_DEV_UNSIGNED", "true")
    from quictrain_api.settings import get_settings

    get_settings.cache_clear()
    payload = {
        "sub": "user-1",
        "email": "u@example.com",
        "name": "U",
        "iss": "https://idp.example/realms/quic",
        "aud": "quictrain",
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    claims = resolve_oidc_claims(f"{header}.{body}.")
    assert claims["sub"] == "user-1"

    monkeypatch.delenv("QUICTRAIN_OIDC_ISSUER", raising=False)
    get_settings.cache_clear()
    try:
        resolve_oidc_claims("a.b.c")
        raise AssertionError("expected OIDC_NOT_CONFIGURED")
    except ServiceError as exc:
        assert exc.code == "OIDC_NOT_CONFIGURED"

    init_database()
    with db.SessionLocal() as session:
        seed_catalog(session)
    from quictrain_api.main import app

    with TestClient(app) as client:
        assert client.get("/api/v1/ops/status").status_code == 200
