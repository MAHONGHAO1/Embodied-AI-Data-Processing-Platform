"""Preview files upload to the process bucket by file_id with signed Content-MD5."""

import re

import pytest
from tests.collection_api_fixtures import make_assigned_package, make_project, make_workspace
from tests.test_client_admission_declaration import client_admission_block
from tests.test_collection_upload_intake_api import _create_session, _minimal_duance_payload

from data.infra import oss_client
from data.models.collection_upload import CollectionUploadSession
from data.schemas.client_admission import SignedPartResponse

MD5 = "1B2M2Y8AsgTpgAmY7PhCfg=="
PREVIEW_KEY_RE = re.compile(
    r"process/v2/workspaces/[1-9][0-9]*/collection-uploads/[0-9a-f-]{36}/[0-9a-f]{32}/[^/]+"
)


class FakeMultipart:
    """Records provider calls; an object exists once its multipart upload completes."""

    def __init__(self, sizes_by_name: dict[str, int]):
        self.sizes_by_name = sizes_by_name
        self.inits: list[tuple[str, str]] = []
        self.signed: list[tuple] = []
        self.completed: set[str] = set()

    def size_of(self, key: str) -> int:
        return self.sizes_by_name[key.rsplit("/", 1)[-1]]

    def init(self, bucket, key):
        self.inits.append((bucket, key))
        return f"upload-{len(self.inits)}"

    def sign(self, *args):
        self.signed.append(args)
        headers = {"Content-Type": "application/octet-stream"}
        # oss_client.sign_browser_upload_part omits the content_md5 positional
        # argument entirely for MCAP parts that carry none, so its presence
        # (not its value) is what this stub uses to decide whether to add the
        # Content-MD5 header, mirroring the real call-site contract.
        if len(args) == 5:
            headers["Content-MD5"] = args[4]
        return f"https://uploads.example.test/{args[2]}/{args[3]}", 300, headers

    def list_parts(self, _bucket, key, _upload_id):
        return [oss_client.OSSMultipartPart(number=1, etag="etag-1", size=self.size_of(key))]

    def complete(self, _bucket, key, _upload_id, _parts):
        self.completed.add(key)

    def object_info(self, _bucket, key):
        if key not in self.completed:
            return None
        return oss_client.OSSObjectInfo(
            size=self.size_of(key),
            etag=f"etag-{key[-8:]}",
            crc64="1234",
            version_id=None,
            metadata={},
        )


def _small_block():
    block = client_admission_block()
    for entry in block["files"]:
        entry["size_bytes"] = 16
    return block


@pytest.fixture
def declared(client, db_session, admin_headers, monkeypatch):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    session_id = _create_session(
        client, admin_headers, workspace, project, package, "oss_multipart"
    )
    block = _small_block()
    sizes = {"upload.bin": 4}
    sizes.update({entry["path"].rsplit("/", 1)[-1]: 16 for entry in block["files"]})
    fake = FakeMultipart(sizes)
    monkeypatch.setattr(oss_client, "init_browser_multipart_upload", fake.init)
    monkeypatch.setattr(oss_client, "sign_browser_upload_part", fake.sign)
    monkeypatch.setattr(oss_client, "list_browser_multipart_parts", fake.list_parts)
    monkeypatch.setattr(oss_client, "complete_browser_multipart_upload", fake.complete)
    monkeypatch.setattr(oss_client, "object_info", fake.object_info)
    response = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [{**_minimal_duance_payload(package.package_uid), "client_admission": block}],
        },
    )
    assert response.status_code == 200, response.text
    [source] = response.json()["data"]["sources"]
    return workspace, session_id, source, fake


def _post(client, headers, session_id, action, body):
    return client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/{action}", headers=headers, json=body
    )


def _upload(client, headers, workspace, session_id, target, size, *, md5=MD5):
    initialized = _post(
        client,
        headers,
        session_id,
        "init",
        {"workspace_id": workspace.id, "total_size_bytes": size, **target},
    )
    assert initialized.status_code == 200, initialized.text
    signed = _post(
        client,
        headers,
        session_id,
        "sign-part",
        {
            "workspace_id": workspace.id,
            "part_number": 1,
            **target,
            **({"content_md5": md5} if md5 else {}),
        },
    )
    assert signed.status_code == 200, signed.text
    completed = _post(
        client,
        headers,
        session_id,
        "complete",
        {"workspace_id": workspace.id, "parts": [{"part_number": 1, "etag": "etag-1"}], **target},
    )
    assert completed.status_code == 200, completed.text
    return signed.json()["data"], completed.json()["data"]["status"]


def test_session_is_uploaded_only_after_mcap_and_every_preview(
    client, db_session, admin_headers, declared
):
    workspace, session_id, source, fake = declared

    _signed, status = _upload(
        client, admin_headers, workspace, session_id, {"source_id": source["source_id"]}, 4
    )
    assert status == "uploading"
    statuses = []
    for entry in source["preview_files"]:
        signed, status = _upload(
            client,
            admin_headers,
            workspace,
            session_id,
            {"file_id": entry["file_id"]},
            entry["size_bytes"],
        )
        SignedPartResponse.model_validate(signed)
        assert signed["headers"]["Content-MD5"] == MD5
        statuses.append(status)

    assert statuses == ["uploading"] * (len(statuses) - 1) + ["uploaded"]
    process_bucket = oss_client.bucket_name("process")
    preview_inits = [(bucket, key) for bucket, key in fake.inits if bucket == process_bucket]
    assert len(preview_inits) == len(source["preview_files"])
    assert all(PREVIEW_KEY_RE.fullmatch(key) for _bucket, key in preview_inits)
    db_session.expire_all()
    stored = db_session.get(CollectionUploadSession, session_id).result_json["oss_multipart_files"]
    for entry in source["preview_files"]:
        identity = stored[entry["file_id"]]["provider_identity"]
        assert stored[entry["file_id"]]["completion_state"] == "completed"
        assert identity["bucket_role"] == "process" and identity["sha256"] is None


def test_client_admission_parts_all_require_content_md5(client, admin_headers, declared):
    workspace, session_id, source, fake = declared
    for target, size in (
        ({"source_id": source["source_id"]}, 4),
        ({"file_id": source["preview_files"][0]["file_id"]}, 16),
    ):
        _post(
            client,
            admin_headers,
            session_id,
            "init",
            {"workspace_id": workspace.id, "total_size_bytes": size, **target},
        )
        missing = _post(
            client,
            admin_headers,
            session_id,
            "sign-part",
            {"workspace_id": workspace.id, "part_number": 1, **target},
        )
        assert missing.status_code == 422
        assert missing.json()["detail"] == "content_md5_required"
    assert fake.signed == []

    signed = _post(
        client,
        admin_headers,
        session_id,
        "sign-part",
        {
            "workspace_id": workspace.id,
            "part_number": 1,
            "source_id": source["source_id"],
            "content_md5": MD5,
        },
    )
    assert signed.status_code == 200
    assert signed.json()["data"]["headers"]["Content-MD5"] == MD5


def test_cancel_aborts_preview_multipart_uploads(client, admin_headers, declared, monkeypatch):
    workspace, session_id, source, _fake = declared
    aborted = []
    monkeypatch.setattr(
        oss_client,
        "abort_browser_multipart_upload",
        lambda bucket, key, upload_id: aborted.append((bucket, key, upload_id)),
    )
    init = _post(
        client,
        admin_headers,
        session_id,
        "init",
        {
            "workspace_id": workspace.id,
            "total_size_bytes": 16,
            "file_id": source["preview_files"][0]["file_id"],
        },
    )
    assert init.status_code == 200, init.text

    cancelled = client.post(
        f"/api/v1/upload-sessions/{session_id}/cancel",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert [(b, u) for b, _k, u in aborted] == [(oss_client.bucket_name("process"), "upload-1")]


def test_preview_init_is_resumable_with_the_same_file_id(client, admin_headers, declared):
    workspace, session_id, source, fake = declared
    body = {
        "workspace_id": workspace.id,
        "total_size_bytes": 16,
        "file_id": source["preview_files"][0]["file_id"],
    }

    first = _post(client, admin_headers, session_id, "init", body)
    second = _post(client, admin_headers, session_id, "init", body)

    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert len(fake.inits) == 1


def test_preview_target_errors(client, admin_headers, declared):
    workspace, session_id, source, _fake = declared
    file_id = source["preview_files"][0]["file_id"]
    wrong_size = _post(
        client,
        admin_headers,
        session_id,
        "init",
        {"workspace_id": workspace.id, "total_size_bytes": 17, "file_id": file_id},
    )
    assert wrong_size.status_code == 422
    both = _post(
        client,
        admin_headers,
        session_id,
        "init",
        {
            "workspace_id": workspace.id,
            "total_size_bytes": 16,
            "file_id": file_id,
            "source_id": source["source_id"],
        },
    )
    assert both.status_code == 422
    unknown = _post(
        client,
        admin_headers,
        session_id,
        "init",
        {"workspace_id": workspace.id, "total_size_bytes": 16, "file_id": "f" * 64},
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "file_id does not belong to this upload session"


def test_file_id_declared_in_a_second_session_is_rejected_on_the_first(
    client, db_session, admin_headers, declared
):
    workspace, session_id, _source, _fake = declared

    other_workspace = make_workspace(db_session)
    other_project = make_project(db_session, other_workspace)
    other_package = make_assigned_package(db_session, other_workspace, other_project)
    other_session_id = _create_session(
        client, admin_headers, other_workspace, other_project, other_package, "oss_multipart"
    )
    other_response = client.post(
        f"/api/v1/upload-sessions/{other_session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": other_workspace.id,
            "items": [
                {
                    **_minimal_duance_payload(other_package.package_uid),
                    "client_admission": _small_block(),
                }
            ],
        },
    )
    assert other_response.status_code == 200, other_response.text
    [other_source] = other_response.json()["data"]["sources"]
    other_file_id = other_source["preview_files"][0]["file_id"]

    rejected = _post(
        client,
        admin_headers,
        session_id,
        "init",
        {"workspace_id": workspace.id, "total_size_bytes": 16, "file_id": other_file_id},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "file_id does not belong to this upload session"


def test_preview_complete_before_init_fails_cleanly(client, admin_headers, declared):
    workspace, session_id, source, _fake = declared
    file_id = source["preview_files"][0]["file_id"]

    # Move the session into "uploading" via the MCAP source so the 422 below
    # actually exercises "no declaration for this file_id yet" rather than
    # the unrelated 409 for a session still in "init".
    initialized = _post(
        client,
        admin_headers,
        session_id,
        "init",
        {"workspace_id": workspace.id, "total_size_bytes": 4, "source_id": source["source_id"]},
    )
    assert initialized.status_code == 200, initialized.text

    completed = _post(
        client,
        admin_headers,
        session_id,
        "complete",
        {
            "workspace_id": workspace.id,
            "parts": [{"part_number": 1, "etag": "etag-1"}],
            "file_id": file_id,
        },
    )

    assert completed.status_code == 422
    assert completed.json()["detail"] == "multipart upload is not initialized"


def test_completing_every_preview_before_the_mcap_does_not_upload_the_session(
    client, admin_headers, declared
):
    workspace, session_id, source, _fake = declared

    statuses = []
    for entry in source["preview_files"]:
        _signed, status = _upload(
            client,
            admin_headers,
            workspace,
            session_id,
            {"file_id": entry["file_id"]},
            entry["size_bytes"],
        )
        statuses.append(status)
    assert statuses == ["uploading"] * len(statuses)

    _signed, status = _upload(
        client, admin_headers, workspace, session_id, {"source_id": source["source_id"]}, 4
    )
    assert status == "uploaded"
