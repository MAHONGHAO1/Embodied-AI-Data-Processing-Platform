"""Declarations carry client admission and receive one file_id per preview file."""

from copy import deepcopy

import pytest
from tests.collection_api_fixtures import make_assigned_package, make_project, make_workspace
from tests.test_client_admission_contract import doc_examples
from tests.test_collection_upload_intake_api import _create_session, _minimal_duance_payload

from data.models.collection_upload import CollectionUploadSession
from data.schemas.client_admission import DeclarationResponse
from data.services.client_admission import (
    ClientAdmissionDeclarationError,
    normalize_client_admission,
    preview_file_id,
)


def client_admission_block() -> dict:
    return deepcopy(doc_examples()["declaration-item"]["client_admission"])


def _session(client, db_session, admin_headers, mode="oss_multipart"):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    session_id = _create_session(client, admin_headers, workspace, project, package, mode)
    return workspace, package, session_id


def _declare(client, admin_headers, workspace, session_id, item):
    return client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "items": [item]},
    )


def test_documented_declaration_example_binds_file_ids_to_paths():
    examples = doc_examples()
    request = examples["declaration-item"]["client_admission"]
    [source] = examples["declaration-response"]["sources"]
    returned = source["preview_files"]
    assert [item["path"] for item in returned] == sorted(item["path"] for item in request["files"])
    for item in returned:
        assert item["file_id"] == preview_file_id(source["source_id"], item["path"])


def test_declaration_returns_preview_file_ids_and_stores_server_keys(
    client, db_session, admin_headers
):
    workspace, package, session_id = _session(client, db_session, admin_headers)
    item = {
        **_minimal_duance_payload(package.package_uid),
        "client_admission": client_admission_block(),
    }

    response = _declare(client, admin_headers, workspace, session_id, item)

    assert response.status_code == 200, response.text
    body = DeclarationResponse.model_validate(response.json()["data"])
    [source] = body.sources
    paths = sorted(entry["path"] for entry in item["client_admission"]["files"])
    assert [entry.path for entry in source.preview_files] == paths
    for entry in source.preview_files:
        assert entry.file_id == preview_file_id(source.source_id, entry.path)
    db_session.expire_all()
    stored = db_session.get(CollectionUploadSession, session_id).result_json["declarations"]
    files = stored[package.package_uid][0]["client_admission"]["files"]
    prefix = f"process/v2/workspaces/{workspace.id}/collection-uploads/{session_id}/"
    for entry in files:
        assert entry["object_key"].startswith(prefix)
        assert entry["object_key"].endswith("/" + entry["path"].rsplit("/", 1)[-1])
    assert '"object_key"' not in response.text


def test_replayed_declaration_keeps_file_ids_and_object_keys(client, db_session, admin_headers):
    workspace, package, session_id = _session(client, db_session, admin_headers)
    item = {
        **_minimal_duance_payload(package.package_uid),
        "client_admission": client_admission_block(),
    }
    first = _declare(client, admin_headers, workspace, session_id, item)
    db_session.expire_all()
    keys = [
        entry["object_key"]
        for entry in db_session.get(CollectionUploadSession, session_id).result_json[
            "declarations"
        ][package.package_uid][0]["client_admission"]["files"]
    ]

    second = _declare(client, admin_headers, workspace, session_id, item)

    assert second.status_code == 200, second.text
    assert second.json()["data"]["sources"] == first.json()["data"]["sources"]
    db_session.expire_all()
    replayed = db_session.get(CollectionUploadSession, session_id).result_json["declarations"]
    assert [
        entry["object_key"]
        for entry in replayed[package.package_uid][0]["client_admission"]["files"]
    ] == keys


def test_declaration_without_client_admission_is_unchanged(client, db_session, admin_headers):
    workspace, package, session_id = _session(client, db_session, admin_headers)

    response = _declare(
        client, admin_headers, workspace, session_id, _minimal_duance_payload(package.package_uid)
    )

    assert response.status_code == 200, response.text
    [source] = response.json()["data"]["sources"]
    assert set(source) == {"source_id", "package_uid", "episode_id"}
    db_session.expire_all()
    stored = db_session.get(CollectionUploadSession, session_id).result_json["declarations"]
    assert "client_admission" not in stored[package.package_uid][0]


def test_client_admission_requires_oss_multipart(client, db_session, admin_headers):
    workspace, package, session_id = _session(client, db_session, admin_headers, mode="chunked")
    item = {
        **_minimal_duance_payload(package.package_uid),
        "client_admission": client_admission_block(),
    }

    response = _declare(client, admin_headers, workspace, session_id, item)

    assert response.status_code == 422
    assert response.json()["detail"] == "client_admission_requires_oss_multipart"


@pytest.mark.parametrize(
    "path, code",
    [
        ("data.mcap", "client_admission_path_outside_preview"),
        ("media/other/x.mp4", "client_admission_path_outside_preview"),
        ("media/preview", "client_admission_path_outside_preview"),
        ("/media/preview/x.mp4", "client_admission_path_unsafe"),
        ("media/preview/../data.mcap", "client_admission_path_unsafe"),
        ("media/preview//x.mp4", "client_admission_path_unsafe"),
        ("media/preview/./x.mp4", "client_admission_path_unsafe"),
        ("media\\preview\\x.mp4", "client_admission_path_unsafe"),
        ("media/preview/.hidden", "client_admission_path_unsafe"),
        ("media/preview/a b.mp4", "client_admission_path_unsafe"),
    ],
)
def test_preview_paths_outside_or_unsafe_are_rejected(path, code):
    block = client_admission_block()
    block["files"][1]["path"] = path
    with pytest.raises(ClientAdmissionDeclarationError) as error:
        normalize_client_admission(block)
    assert error.value.code == code


def test_duplicate_path_and_missing_manifest_are_rejected():
    duplicate = client_admission_block()
    duplicate["files"].append(dict(duplicate["files"][1]))
    with pytest.raises(ClientAdmissionDeclarationError, match="client_admission_path_duplicate"):
        normalize_client_admission(duplicate)
    no_manifest = client_admission_block()
    no_manifest["files"] = no_manifest["files"][1:]
    with pytest.raises(ClientAdmissionDeclarationError, match="client_admission_manifest_missing"):
        normalize_client_admission(no_manifest)
    oversized = client_admission_block()
    oversized["report"]["padding"] = "x" * (1024 * 1024)
    with pytest.raises(ClientAdmissionDeclarationError, match="client_admission_report_too_large"):
        normalize_client_admission(oversized)


def test_non_rgb_episode_may_declare_no_preview_files():
    block = client_admission_block()
    block["files"] = []
    assert normalize_client_admission(block)["files"] == []


def test_http_rejects_bad_paths_and_malformed_fields(client, db_session, admin_headers):
    workspace, package, session_id = _session(client, db_session, admin_headers)
    outside = client_admission_block()
    outside["files"][1]["path"] = "data.mcap"
    response = _declare(
        client,
        admin_headers,
        workspace,
        session_id,
        {**_minimal_duance_payload(package.package_uid), "client_admission": outside},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "client_admission_path_outside_preview"
    malformed = client_admission_block()
    malformed["files"][0]["sha256"] = "not-a-digest"
    response = _declare(
        client,
        admin_headers,
        workspace,
        session_id,
        {**_minimal_duance_payload(package.package_uid), "client_admission": malformed},
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
