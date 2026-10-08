"""Collection upload and intake review main path end-to-end smoke test."""

import hashlib
import json
from uuid import uuid4

from collection_api_fixtures import (
    make_collector,
    make_device_model,
    make_workspace,
)

from data.database import Episode
from data.models.collection_upload import CollectionUploadSession
from data.models.data_package import DataPackage, PackageIntakeReview
from data.services.collection_upload_parse import parse_collection_upload_session


def _declared_source(package_uid: str, episode_id: str) -> dict:
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": episode_id,
        "timing": {
            "start_timestamp_ns": "0",
            "end_timestamp_ns": "3600000000000",
        },
        "capture": {"mode": "ego", "app_version": "smoke-1"},
        "privacy_sensitive": False,
        "data_file": "data.mcap",
    }
    metadata_text = json.dumps(metadata)
    return {
        "package_uid": package_uid,
        "source": {
            "episode_id": episode_id,
            "start_ns": "0",
            "end_ns": "3600000000000",
            "metadata_sha256": hashlib.sha256(metadata_text.encode()).hexdigest(),
            "data_mcap_sha256": "1" * 64,
        },
        "metadata_text": metadata_text,
        "data_file": {
            "path": "data.mcap",
            "size_bytes": 4,
            "sha256": "1" * 64,
        },
    }


def _parse_fixture_source(package_uid: str, *, privacy_sensitive: bool) -> dict:
    return {
        "package_uid": package_uid,
        "episode_id": f"ep-{uuid4().hex[:12]}",
        "start_ns": 0,
        "end_ns": 3_600_000_000_000,
        "capture_mode": "ego",
        "capture_app_version": "smoke-1",
        "privacy_sensitive": privacy_sensitive,
        "modality": "rgb",
        "source_fingerprint": uuid4().hex,
    }


def test_collection_upload_and_intake_review_end_to_end_smoke(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    device = make_device_model(db_session)
    responsible = make_collector(db_session, workspace, name="Smoke Responsible")
    operator = make_collector(db_session, workspace, name="Smoke Operator")

    project_response = client.post(
        "/api/v1/collection-projects",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": "Upload Intake Smoke Project",
            "description": "upload and intake smoke path",
        },
    )
    assert project_response.status_code == 200
    project = project_response.json()["data"]

    task_response = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project["id"],
            "name": "Upload Intake Smoke Task",
            "target_duration_hours": "3.00",
            "default_package_duration_hours": "1.00",
            "device_model_id": device.id,
            "label_ids": [],
        },
    )
    assert task_response.status_code == 200
    task = task_response.json()["data"]
    assert len(task["packages"]) == 3

    packages = []
    for package in task["packages"]:
        assigned_response = client.post(
            f"/api/v1/data-packages/{package['id']}/assign",
            headers=admin_headers,
            json={
                "workspace_id": workspace.id,
                "responsible_collector_id": responsible.id,
                "operator_collector_id": operator.id,
            },
        )
        assert assigned_response.status_code == 200
        packages.append(assigned_response.json()["data"])

    session_response = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project["id"],
            "package_uids": [package["package_uid"] for package in packages],
            "upload_mode": "duance_sdk",
        },
    )
    assert session_response.status_code == 200
    upload_session_id = session_response.json()["data"]["id"]

    declaration_response = client.post(
        f"/api/v1/upload-sessions/{upload_session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [
                _declared_source(package["package_uid"], f"declared-{index}")
                for index, package in enumerate(packages, start=1)
            ],
        },
    )
    assert declaration_response.status_code == 200
    assert declaration_response.json()["data"]["declared_packages"] == 3

    upload_session = db_session.get(CollectionUploadSession, upload_session_id)
    upload_session.result_json = {
        "parse_fixture_mode": True,
        "declarations": [
            _parse_fixture_source(packages[0]["package_uid"], privacy_sensitive=False),
            _parse_fixture_source(packages[0]["package_uid"], privacy_sensitive=True),
            _parse_fixture_source(packages[1]["package_uid"], privacy_sensitive=False),
            _parse_fixture_source(packages[2]["package_uid"], privacy_sensitive=False),
        ],
    }
    upload_session.status = "uploaded"
    db_session.commit()
    parse_collection_upload_session(db_session, session_id=upload_session_id)

    first_detail_response = client.get(
        f"/api/v1/data-packages/{packages[0]['id']}",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert first_detail_response.status_code == 200
    first_detail = first_detail_response.json()["data"]
    assert first_detail["status"] == "pending_intake_review"
    assert len(first_detail["episodes"]) == 2
    rejected_episode_id = first_detail["episodes"][0]["id"]

    approve_response = client.post(
        f"/api/v1/data-packages/{packages[0]['id']}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "approved",
            "rejected_episode_ids": [rejected_episode_id],
        },
    )
    assert approve_response.status_code == 200
    assert approve_response.json()["data"]["intake_valid_duration_hours"] == "1.00"
    db_session.expire_all()
    assert db_session.get(Episode, rejected_episode_id).validity_status == ("intake_rejected")

    overview_response = client.get(
        "/api/v1/collection-overview",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "collection_project_id": project["id"],
        },
    )
    assert overview_response.status_code == 200
    assert overview_response.json()["data"]["intake_valid_duration_hours"] == "1.00"

    bulk_response = client.post(
        "/api/v1/data-packages/intake-review/bulk-approve",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "data_package_ids": [packages[1]["id"]],
        },
    )
    assert bulk_response.status_code == 200
    assert bulk_response.json()["data"]["approved_count"] == 1
    db_session.expire_all()
    bulk_review = (
        db_session.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == packages[1]["id"])
        .one()
    )
    assert bulk_review.is_bulk is True

    package_count_before_reject = (
        db_session.query(DataPackage).filter(DataPackage.collection_task_id == task["id"]).count()
    )
    reject_response = client.post(
        f"/api/v1/data-packages/{packages[2]['id']}/intake-review",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "verdict": "rejected",
            "reason": "smoke rejection",
        },
    )
    assert reject_response.status_code == 200
    assert reject_response.json()["data"]["status"] == "voided"
    package_count_after_reject = (
        db_session.query(DataPackage).filter(DataPackage.collection_task_id == task["id"]).count()
    )
    assert package_count_after_reject == package_count_before_reject == 3
    assert (
        db_session.query(Episode)
        .filter(
            Episode.data_package_id.in_([package["id"] for package in packages]),
            Episode.task_set_id.is_not(None),
        )
        .count()
        == 0
    )
    assert (
        db_session.query(Episode)
        .filter(
            Episode.data_package_id.in_([package["id"] for package in packages]),
            Episode.batch_id.is_not(None),
        )
        .count()
        == 0
    )
