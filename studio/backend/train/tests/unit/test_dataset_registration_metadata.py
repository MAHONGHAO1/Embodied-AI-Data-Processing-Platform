"""Dataset registration preserves unknown metadata and archive readiness boundaries."""

import pytest
from fastapi.testclient import TestClient
from tests.db_helpers import rebind_database

import quictrain_api.db as db


@pytest.fixture
def registration_client(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICTRAIN_DATABASE_URL", f"sqlite:///{tmp_path}/registration.db")
    monkeypatch.setenv("QUICTRAIN_AUTH_MODE", "open")
    monkeypatch.setenv("QUICTRAIN_EMBEDDED_SCHEDULER", "false")
    monkeypatch.setenv("QUICTRAIN_SEED_EXAMPLE_DATASETS", "false")
    rebind_database()
    from quictrain_api.main import app

    with TestClient(app) as client:
        yield client


def request_body(**overrides):
    return {
        "client_request_id": "manual-register",
        "display_name": "Operator declared dataset",
        "uri": "oss://bucket/dataset/",
        **overrides,
    }


def test_name_and_uri_only_register_unknown_metadata(registration_client):
    response = registration_client.post("/api/v1/datasets", json=request_body())
    assert response.status_code == 201
    assert response.json()["status"] == "REGISTERED"
    dataset_id = response.json()["dataset_version_id"]
    listed = registration_client.get("/api/v1/datasets").json()["items"]
    manifest = next(item for item in listed if item["id"] == dataset_id)
    for field in (
        "episodes",
        "frames",
        "fps",
        "duration_hours",
        "robot_type",
        "camera_keys",
        "action_dim",
        "state_dim",
        "language_tasks",
    ):
        assert manifest[field] is None
    compatibility = registration_client.get(
        f"/api/v1/datasets/{dataset_id}/compatibility",
        params={"model_version_id": "act"},
    ).json()
    assert compatibility["compatible"] is False
    assert {item["code"] for item in compatibility["issues"]} >= {
        "DATASET_NOT_READY",
        "DATASET_METADATA_REQUIRED",
    }


def test_ready_registration_requires_actual_metadata(registration_client):
    response = registration_client.post("/api/v1/datasets", json=request_body(status="READY"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DATASET_METADATA_REQUIRED"
    assert registration_client.get("/api/v1/datasets").json()["items"] == []


def test_register_and_upsert_cannot_return_another_projects_version(registration_client):
    body = request_body(dataset_id="shared-name", version="v1", checksum="sha256:" + "a" * 64)
    created = registration_client.post("/api/v1/datasets", json=body)
    assert created.status_code == 201
    with db.SessionLocal() as session:
        session.add(
            db.ProjectMembershipRecord(
                id="other-project-admin", project_id="prj_other", user_id="usr_demo", role="admin"
            )
        )
        session.commit()
    headers = {"X-Quic-Project": "prj_other"}
    denied = registration_client.post("/api/v1/datasets", json=body, headers=headers)
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "DATASET_VERSION_PROJECT_CONFLICT"
    upsert = registration_client.post(
        "/api/v1/integrations/data-platform/dataset-versions:upsert",
        headers={**headers, "Idempotency-Key": "other-project-event"},
        json={
            "event_id": "other-project-event",
            "event_type": "dataset_version.published",
            "occurred_at": "2026-09-23T00:00:00Z",
            "dataset_version": {
                "external_dataset_id": "shared-name",
                "external_version_id": "v1",
                "display_name": "Other project",
                "uri": body["uri"],
                "checksum": body["checksum"],
                "episodes": 2,
                "frames": 20,
                "duration_hours": 2 / 3600,
                "fps": 10,
                "robot_type": "test",
                "camera_keys": ["observation.images.head"],
                "action_dim": 7,
                "state_dim": 7,
            },
        },
    )
    assert upsert.status_code == 409
    assert upsert.json()["error"]["code"] == "DATASET_VERSION_PROJECT_CONFLICT"
    assert created.json()["dataset_version_id"] not in denied.text + upsert.text
    assert registration_client.get("/api/v1/datasets", headers=headers).json()["items"] == []


def test_tar_archive_cannot_register_as_ready_or_start_materialization(registration_client):
    body = request_body(uri="oss://bucket/dataset.tar.gz")
    rejected = registration_client.post("/api/v1/datasets", json={**body, "status": "READY"})
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "DATASET_ARCHIVE_NOT_READY"
    created = registration_client.post("/api/v1/datasets", json=body)
    assert created.status_code == 201
    dataset_id = created.json()["dataset_version_id"]
    materialization = registration_client.post(f"/api/v1/datasets/{dataset_id}/materializations")
    assert materialization.status_code == 409
    assert materialization.json()["error"]["code"] == "DATASET_ARCHIVE_MATERIALIZATION_UNSUPPORTED"
    with db.SessionLocal() as session:
        assert session.get(db.DatasetVersionRecord, dataset_id).status == "REGISTERED"
        assert session.query(db.MaterializationAttemptRecord).count() == 0


def test_legacy_queued_archive_materialization_cannot_become_ready(registration_client, tmp_path):
    from quictrain_scheduler.materializer import Materializer

    source = tmp_path / "dataset.tar.gz"
    source.write_bytes(b"archive content must not be treated as a dataset directory")
    created = registration_client.post("/api/v1/datasets", json=request_body(uri=source.as_uri()))
    dataset_id = created.json()["dataset_version_id"]
    with db.SessionLocal() as session:
        dataset = session.get(db.DatasetVersionRecord, dataset_id)
        attempt = db.MaterializationAttemptRecord(
            id="legacy-archive-attempt",
            dataset_version_id=dataset_id,
            number=1,
            state="PENDING",
            lease_key="legacy-archive-attempt",
            source_uri=source.as_uri(),
            target_uri=str(tmp_path / "materialized"),
            checksum=dataset.checksum,
            actor_id="usr_demo",
        )
        session.add(attempt)
        session.commit()
    assert Materializer(db.SessionLocal).run_once() is True
    with db.SessionLocal() as session:
        assert session.get(db.DatasetVersionRecord, dataset_id).status == "FAILED"
        assert (
            session.get(db.MaterializationAttemptRecord, "legacy-archive-attempt").state == "FAILED"
        )
    assert not (tmp_path / "materialized").exists()
