"""Collection upload byte entry: manifest binding and part completion."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from tests.collection_api_fixtures import (
    make_assigned_package,
    make_project,
    make_workspace,
)

from data.models.collection_upload import CollectionUploadSession
from data.models.data_package import DataPackage


def _minimal_duance_payload(package_uid: str) -> dict:
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": f"ep-{uuid4().hex[:12]}",
        "timing": {
            "start_timestamp_ns": "0",
            "end_timestamp_ns": "3600000000000",
        },
        "capture": {"mode": "ego", "app_version": "test-1"},
        "privacy_sensitive": False,
        "data_file": "data.mcap",
    }
    metadata_text = json.dumps(metadata, ensure_ascii=False)
    return {
        "package_uid": package_uid,
        "source": {
            "episode_id": metadata["episode_id"],
            "start_ns": "0",
            "end_ns": "3600000000000",
            "metadata_sha256": hashlib.sha256(metadata_text.encode()).hexdigest(),
            "data_mcap_sha256": hashlib.sha256(b"mcap").hexdigest(),
        },
        "metadata_text": metadata_text,
        "data_file": {
            "path": "data.mcap",
            "size_bytes": 4,
            "sha256": hashlib.sha256(b"mcap").hexdigest(),
        },
    }


def _create_session(client, headers, workspace, project, package, mode):
    response = client.post(
        "/api/v1/upload-sessions",
        headers=headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [package.package_uid],
            "upload_mode": mode,
        },
    )
    assert response.status_code == 200
    return response.json()["data"]["id"]


def test_declare_rejects_package_uid_outside_session(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package_a = make_assigned_package(db_session, workspace, project)
    package_b = make_assigned_package(db_session, workspace, project)
    session_id = _create_session(client, admin_headers, workspace, project, package_a, "duance_sdk")

    response = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [_minimal_duance_payload(package_b.package_uid)],
        },
    )

    assert response.status_code == 409
    assert "package_not_in_session" in response.json()["detail"]


def test_chunked_upload_happy_path_marks_session_and_package_uploaded(
    client, db_session, admin_headers, tmp_path, monkeypatch
):
    from data.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    session_id = _create_session(client, admin_headers, workspace, project, package, "chunked")
    declaration = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [_minimal_duance_payload(package.package_uid)],
        },
    )
    assert declaration.status_code == 200
    db_session.expire_all()
    declared_session = db_session.get(CollectionUploadSession, session_id)
    stored_source = declared_session.result_json["declarations"][package.package_uid][0]
    source_id = declaration.json()["data"]["sources"][0]["source_id"]
    assert stored_source["staging_path"] == f"sources/{package.package_uid}/{source_id}/data.mcap"

    init = client.post(
        f"/api/v1/upload-sessions/{session_id}/chunked/init",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "total_chunks": 1},
    )
    assert init.status_code == 200
    assert init.json()["data"]["status"] == "uploading"
    assert init.json()["data"]["uploaded_chunk_indices"] == []
    put = client.put(
        f"/api/v1/upload-sessions/{session_id}/chunked/0",
        headers={**admin_headers, "Content-Type": "application/octet-stream"},
        content=b"mcap",
    )
    assert put.status_code == 200
    assert put.json()["data"]["uploaded_chunks"] == 1
    assert put.json()["data"]["uploaded_chunk_indices"] == [0]
    resumed = client.post(
        f"/api/v1/upload-sessions/{session_id}/chunked/init",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "total_chunks": 1},
    )
    assert resumed.json()["data"]["uploaded_chunk_indices"] == [0]
    done = client.post(
        f"/api/v1/upload-sessions/{session_id}/chunked/complete",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )

    assert done.status_code == 200
    assert done.json()["data"]["status"] == "uploaded"
    assert "bucket" not in json.dumps(done.json())
    assert "storage_uri" not in json.dumps(done.json())
    db_session.expire_all()
    persisted = db_session.get(DataPackage, package.id)
    assert persisted.status == "uploading"
    assert persisted.upload_completed_at is not None
    staged = Path(tmp_path, "collection-uploads", session_id, "upload.bin")
    assert staged.read_bytes() == b"mcap"


def test_declaration_validates_duance_manifest(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    session_id = _create_session(client, admin_headers, workspace, project, package, "duance_sdk")
    item = _minimal_duance_payload(package.package_uid)
    item["source"]["metadata_sha256"] = "0" * 64

    response = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "items": [item]},
    )

    assert response.status_code == 422
    assert "metadata SHA-256" in response.json()["detail"]


def test_oss_upload_reconciles_provider_parts_without_exposing_locator(
    client, db_session, admin_headers, monkeypatch
):
    from data.infra import oss_client

    provider = {"completed": False}

    def complete_upload(*_):
        provider["completed"] = True

    def object_info(*_):
        if not provider["completed"]:
            return None
        return oss_client.OSSObjectInfo(
            size=4,
            etag="object-etag",
            crc64="1234",
            version_id=None,
            metadata={},
        )

    monkeypatch.setattr(oss_client, "init_browser_multipart_upload", lambda *_: "upload-1")
    monkeypatch.setattr(
        oss_client,
        "sign_browser_upload_part",
        lambda *args: (
            f"https://uploads.example.test/part/{args[-1]}",
            300,
            {"Content-Type": "application/octet-stream"},
        ),
    )
    monkeypatch.setattr(
        oss_client,
        "list_browser_multipart_parts",
        lambda *_: [
            oss_client.OSSMultipartPart(number=1, etag="etag-1", size=4),
        ],
    )
    monkeypatch.setattr(oss_client, "complete_browser_multipart_upload", complete_upload)
    monkeypatch.setattr(oss_client, "object_info", object_info)
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    session_id = _create_session(
        client, admin_headers, workspace, project, package, "oss_multipart"
    )
    declared = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [_minimal_duance_payload(package.package_uid)],
        },
    )
    assert declared.status_code == 200

    initialized = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/init",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "total_size_bytes": 4},
    )
    signed = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/sign-part",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "part_number": 1},
    )
    completed = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/complete",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "parts": [
                {"part_number": 1, "etag": "etag-1"},
            ],
        },
    )

    assert initialized.status_code == 200
    assert initialized.json()["data"]["total_parts"] == 1
    assert signed.status_code == 200
    assert signed.json()["data"]["content_length"] == 4
    assert signed.json()["data"]["url"].startswith("https://")
    assert completed.status_code == 200
    assert completed.json()["data"]["status"] == "uploaded"
    for response in (initialized, signed, completed):
        body = json.dumps(response.json())
        assert '"bucket"' not in body
        assert '"object_key"' not in body
        assert '"storage_uri"' not in body


def test_oss_completion_recovers_after_provider_success_before_db_transition(
    client, db_session, admin_headers, monkeypatch
):
    from data.infra import oss_client
    from data.services import collection_upload_intake

    provider = {"completed": False}

    def list_parts(*_):
        if provider["completed"]:
            raise ValueError("upload id was consumed")
        return [oss_client.OSSMultipartPart(number=1, etag="etag-1", size=4)]

    def complete_upload(*_):
        provider["completed"] = True

    def object_info(*_):
        if not provider["completed"]:
            return None
        return oss_client.OSSObjectInfo(
            size=4,
            etag="object-etag",
            crc64="1234",
            version_id=None,
            metadata={},
        )

    actual_mark_uploaded = collection_upload_intake.mark_upload_session_uploaded
    transition_attempts = 0

    def fail_first_transition(*args, **kwargs):
        nonlocal transition_attempts
        transition_attempts += 1
        if transition_attempts == 1:
            raise ValueError("simulated database transition failure")
        return actual_mark_uploaded(*args, **kwargs)

    monkeypatch.setattr(oss_client, "init_browser_multipart_upload", lambda *_: "upload-replay")
    monkeypatch.setattr(oss_client, "list_browser_multipart_parts", list_parts)
    monkeypatch.setattr(oss_client, "complete_browser_multipart_upload", complete_upload)
    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(
        collection_upload_intake,
        "mark_upload_session_uploaded",
        fail_first_transition,
    )
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    session_id = _create_session(
        client, admin_headers, workspace, project, package, "oss_multipart"
    )
    declaration = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [_minimal_duance_payload(package.package_uid)],
        },
    )
    assert declaration.status_code == 200
    initialized = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/init",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "total_size_bytes": 4},
    )
    assert initialized.status_code == 200
    completion_body = {
        "workspace_id": workspace.id,
        "parts": [{"part_number": 1, "etag": "etag-1"}],
    }

    first = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/complete",
        headers=admin_headers,
        json=completion_body,
    )
    db_session.expire_all()
    persisted = db_session.get(CollectionUploadSession, session_id)
    oss_state = persisted.result_json["oss_multipart"]
    assert oss_state["completion_state"] == "completing"
    assert oss_state["completion_parts"] == [{"part_number": 1, "etag": "etag-1"}]
    retried = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/complete",
        headers=admin_headers,
        json=completion_body,
    )

    assert first.status_code == 422
    assert retried.status_code == 200
    assert retried.json()["data"]["status"] == "uploaded"


def test_oss_multipart_accepts_each_declared_source_before_parse_dispatch(
    client, db_session, admin_headers, monkeypatch
):
    from data.infra import oss_client

    completed: set[str] = set()

    def init_upload(_bucket, object_key):
        return f"upload-{object_key.rsplit('/', 2)[-2]}"

    def object_info(_bucket, object_key):
        if object_key not in completed:
            return None
        return oss_client.OSSObjectInfo(
            size=4, etag="object-etag", crc64="1234", version_id=None, metadata={}
        )

    def complete_upload(_bucket, object_key, _upload_id, _parts):
        completed.add(object_key)

    monkeypatch.setattr(oss_client, "init_browser_multipart_upload", init_upload)
    monkeypatch.setattr(
        oss_client,
        "sign_browser_upload_part",
        lambda *_args: ("https://uploads.example.test/part", 300, {}),
    )
    monkeypatch.setattr(
        oss_client,
        "list_browser_multipart_parts",
        lambda *_args: [oss_client.OSSMultipartPart(number=1, etag="etag-1", size=4)],
    )
    monkeypatch.setattr(oss_client, "complete_browser_multipart_upload", complete_upload)
    monkeypatch.setattr(oss_client, "object_info", object_info)

    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    packages = [
        make_assigned_package(db_session, workspace, project),
        make_assigned_package(db_session, workspace, project),
    ]
    created = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [package.package_uid for package in packages],
            "upload_mode": "oss_multipart",
        },
    )
    assert created.status_code == 200
    session_id = created.json()["data"]["id"]
    declared = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [_minimal_duance_payload(package.package_uid) for package in packages],
        },
    )
    assert declared.status_code == 200
    source_ids = [item["source_id"] for item in declared.json()["data"]["sources"]]

    for index, source_id in enumerate(source_ids):
        initialized = client.post(
            f"/api/v1/upload-sessions/{session_id}/oss/init",
            headers=admin_headers,
            json={
                "workspace_id": workspace.id,
                "total_size_bytes": 4,
                "source_id": source_id,
            },
        )
        assert initialized.status_code == 200, initialized.text
        signed = client.post(
            f"/api/v1/upload-sessions/{session_id}/oss/sign-part",
            headers=admin_headers,
            json={"workspace_id": workspace.id, "part_number": 1, "source_id": source_id},
        )
        assert signed.status_code == 200
        completed_response = client.post(
            f"/api/v1/upload-sessions/{session_id}/oss/complete",
            headers=admin_headers,
            json={
                "workspace_id": workspace.id,
                "source_id": source_id,
                "parts": [{"part_number": 1, "etag": "etag-1"}],
            },
        )
        assert completed_response.status_code == 200
        expected_status = "uploading" if index == 0 else "uploaded"
        assert completed_response.json()["data"]["status"] == expected_status
