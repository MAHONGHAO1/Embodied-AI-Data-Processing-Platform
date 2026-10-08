"""Real byte transport through declarations, chunk upload, parsing and review."""

import base64
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
from mcap.writer import Writer
from tests.collection_api_fixtures import (
    make_assigned_package,
    make_project,
    make_workspace,
)
from tests.test_client_admission_worker import generate_previews
from tests.test_collection_admission_worker import claimed_job
from tests.test_episode_objects import verified_entries
from tests.test_qrdf_admission import _episode

from data.config import settings
from data.database import Episode
from data.infra import oss_client
from data.infra.object_storage import StorageObjectNotFound, StorageObjectRef
from data.integrations.qrdf.admission import run_qrdf_admission_worker
from data.models.collection_upload import CollectionUploadSession
from data.models.data_package import DataPackage
from data.services.collection_upload_parse import parse_collection_upload_session
from data.services.episode_admission import (
    current_episode_admission_fact,
    record_episode_admission_fact,
)
from data.services.episode_objects import fact_objects


def _payload(package, index):
    stream = io.BytesIO()
    writer = Writer(stream)
    writer.start()
    writer.add_metadata("source", {"index": str(index)})
    writer.finish()
    data = stream.getvalue()
    episode_id = f"episode-{index}"
    metadata_text = json.dumps(
        {
            "qrdf_version": "0.2.0",
            "episode_id": episode_id,
            "data_file": f"{episode_id}/data.mcap",
            "timing": {
                "start_timestamp_ns": 0,
                "end_timestamp_ns": 3600000000000,
                "duration_s": 3600,
            },
            "sensors": {"cameras": []},
            "capture": {"mode": "ego", "app_version": "test"},
        }
    )
    digest = hashlib.sha256(data).hexdigest()
    return data, {
        "package_uid": package.package_uid,
        "source": {
            "episode_id": episode_id,
            "start_ns": "0",
            "end_ns": "3600000000000",
            "metadata_sha256": hashlib.sha256(metadata_text.encode()).hexdigest(),
            "data_mcap_sha256": digest,
        },
        "metadata_text": metadata_text,
        "data_file": {
            "path": f"{episode_id}/data.mcap",
            "size_bytes": len(data),
            "sha256": digest,
        },
    }


def _setup(client, db, headers, *, count=2, mode="duance_sdk"):
    workspace = make_workspace(db)
    project = make_project(db, workspace)
    packages = [make_assigned_package(db, workspace, project) for _ in range(count)]
    response = client.post(
        "/api/v1/upload-sessions",
        headers=headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [p.package_uid for p in packages],
            "upload_mode": mode,
        },
    )
    assert response.status_code == 200
    base = f"/api/v1/upload-sessions/{response.json()['data']['id']}"
    payloads = [_payload(package, i) for i, package in enumerate(packages)]
    response = client.post(
        f"{base}/declarations",
        headers=headers,
        json={
            "workspace_id": workspace.id,
            "items": [p[1] for p in payloads],
        },
    )
    assert response.status_code == 200
    return workspace, packages, base, payloads, response.json()["data"]["sources"]


@pytest.mark.parametrize("count", [1, 2])
def test_sdk_bytes_reach_intake_review_without_fixture_injection(
    client,
    db_session,
    admin_headers,
    tmp_path,
    monkeypatch,
    count,
):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    ws, packages, base, payloads, sources = _setup(client, db_session, admin_headers, count=count)
    for (content, _), source in zip(payloads, sources, strict=False):
        source_id = source["source_id"]
        initialized = client.post(
            f"{base}/chunked/init",
            headers=admin_headers,
            json={
                "workspace_id": ws.id,
                "source_id": source_id,
                "total_chunks": 2,
            },
        )
        assert initialized.status_code == 200
        midpoint = len(content) // 2
        for index, chunk in enumerate((content[:midpoint], content[midpoint:])):
            uploaded = client.put(
                f"{base}/chunked/{index}",
                params={"source_id": source_id},
                headers=admin_headers,
                content=chunk,
            )
            assert uploaded.status_code == 200
        # Every declared source is required; partial session completion is rejected.
        if source is sources[0] and count > 1:
            incomplete = client.post(
                f"{base}/chunked/complete",
                headers=admin_headers,
                json={"workspace_id": ws.id},
            )
            assert incomplete.status_code == 422
    completed = client.post(
        f"{base}/chunked/complete", headers=admin_headers, json={"workspace_id": ws.id}
    )
    assert completed.status_code == 200, completed.text
    replay = client.post(
        f"{base}/chunked/complete", headers=admin_headers, json={"workspace_id": ws.id}
    )
    assert replay.status_code == 200
    # Completing a session freezes its bytes: stale clients cannot re-initialize it.
    late = client.post(
        f"{base}/chunked/init",
        headers=admin_headers,
        json={
            "workspace_id": ws.id,
            "source_id": sources[0]["source_id"],
            "total_chunks": 2,
        },
    )
    assert late.status_code == 409
    session_id = base.rsplit("/", 1)[1]
    db_session.expire_all()
    parse_collection_upload_session(db_session, session_id)
    assert db_session.get(CollectionUploadSession, session_id).status == "succeeded"
    preview_manifest = Path(
        tmp_path, "collection-uploads", session_id, "media", "preview", "manifest.json"
    )
    preview_manifest.parent.mkdir(parents=True, exist_ok=True)
    preview_manifest.write_text("{}", encoding="utf-8")
    for package in packages:
        package.qrdf_facts_json = {
            **(package.qrdf_facts_json or {}),
            "preview": {"available": True},
        }
        for episode in db_session.query(Episode).filter(Episode.data_package_id == package.id):
            record_episode_admission_fact(
                db_session,
                episode_id=episode.id,
                attempt=2,
                source_fingerprint=episode.source_fingerprint,
                validation_policy_version="v1",
                integrity_status="passed",
                preview_status="ready",
                output_verification_status="verified",
                qrdf_profile=episode.modality,
                report_ref={"source": "test-preview-manifest"},
                objects=verified_entries(),
            )
    db_session.commit()
    for package in packages:
        db_session.refresh(package)
        assert package.status == "pending_intake_review"
        assert db_session.query(Episode).filter_by(data_package_id=package.id).count() == 1
        reviewed = client.post(
            f"/api/v1/data-packages/{package.id}/intake-review",
            headers=admin_headers,
            json={"workspace_id": ws.id, "verdict": "approved"},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["data"]["intake_valid_duration_hours"] == "1.00"


def test_chunk_source_binding_checksum_and_retry(
    client, db_session, admin_headers, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    ws, _, base, payloads, sources = _setup(client, db_session, admin_headers)
    for value in (None, "0" * 64):
        response = client.post(
            f"{base}/chunked/init",
            headers=admin_headers,
            json={
                "workspace_id": ws.id,
                "source_id": value,
                "total_chunks": 1,
            },
        )
        assert response.status_code == 422
    for (content, _), source in zip(payloads, sources, strict=False):
        init = client.post(
            f"{base}/chunked/init",
            headers=admin_headers,
            json={
                "workspace_id": ws.id,
                "source_id": source["source_id"],
                "total_chunks": 1,
            },
        )
        assert init.status_code == 200
        put = client.put(
            f"{base}/chunked/0",
            params={"source_id": source["source_id"]},
            headers=admin_headers,
            content=b"x" * len(content),
        )
        assert put.status_code == 200
    failed = client.post(
        f"{base}/chunked/complete", headers=admin_headers, json={"workspace_id": ws.id}
    )
    assert failed.status_code == 422
    assert "checksum" in failed.json()["detail"]
    assert not list(tmp_path.rglob(".assembled-*.tmp"))
    for (content, _), source in zip(payloads, sources, strict=False):
        put = client.put(
            f"{base}/chunked/0",
            params={"source_id": source["source_id"]},
            headers=admin_headers,
            content=content,
        )
        assert put.status_code == 200
    assert (
        client.post(
            f"{base}/chunked/complete",
            headers=admin_headers,
            json={"workspace_id": ws.id},
        ).status_code
        == 200
    )


def test_cancel_uploaded_session_releases_package_for_reupload(
    client, db_session, admin_headers, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    ws, packages, base, payloads, sources = _setup(client, db_session, admin_headers, count=1)
    source_id = sources[0]["source_id"]
    client.post(
        f"{base}/chunked/init",
        headers=admin_headers,
        json={
            "workspace_id": ws.id,
            "source_id": source_id,
            "total_chunks": 1,
        },
    ).raise_for_status()
    client.put(
        f"{base}/chunked/0",
        params={"source_id": source_id},
        headers=admin_headers,
        content=payloads[0][0],
    ).raise_for_status()
    client.post(
        f"{base}/chunked/complete", headers=admin_headers, json={"workspace_id": ws.id}
    ).raise_for_status()
    cancelled = client.post(f"{base}/cancel", headers=admin_headers, json={"workspace_id": ws.id})
    assert cancelled.status_code == 200
    db_session.expire_all()
    package = db_session.get(DataPackage, packages[0].id)
    assert package.status == "assigned"
    assert package.upload_completed_at is None
    retried = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": ws.id,
            "collection_project_id": package.collection_project_id,
            "package_uids": [package.package_uid],
            "upload_mode": "duance_sdk",
        },
    )
    assert retried.status_code == 200


def test_oss_uninitialized_operations_fail_cleanly(client, db_session, admin_headers):
    ws, _, base, _, _ = _setup(client, db_session, admin_headers, count=1, mode="oss_multipart")
    signed = client.post(
        f"{base}/oss/sign-part",
        headers=admin_headers,
        json={"workspace_id": ws.id, "part_number": 1},
    )
    completed = client.post(
        f"{base}/oss/complete",
        headers=admin_headers,
        json={"workspace_id": ws.id, "parts": [{"part_number": 1, "etag": "x"}]},
    )
    assert signed.status_code == 409
    assert completed.status_code == 409


def test_same_package_accepts_two_episode_files_named_data_mcap(
    client, db_session, admin_headers, tmp_path, monkeypatch
):
    """Normal QRDF episode directories must not need metadata rewriting."""
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    ws = make_workspace(db_session)
    project = make_project(db_session, ws)
    package = make_assigned_package(db_session, ws, project)
    created = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": ws.id,
            "collection_project_id": project.id,
            "package_uids": [package.package_uid],
            "upload_mode": "chunked",
        },
    )
    base = "/api/v1/upload-sessions/" + created.json()["data"]["id"]
    payloads = []
    for index in range(2):
        content, payload = _payload(package, index)
        metadata = json.loads(payload["metadata_text"])
        metadata["data_file"] = "data.mcap"
        payload["metadata_text"] = json.dumps(metadata)
        payload["source"]["metadata_sha256"] = hashlib.sha256(
            payload["metadata_text"].encode()
        ).hexdigest()
        payload["data_file"]["path"] = "data.mcap"
        payloads.append((content, payload))
    body = {"workspace_id": ws.id, "items": [payload for _, payload in payloads]}
    declared = client.post(base + "/declarations", headers=admin_headers, json=body)
    assert declared.status_code == 200, declared.text
    sources = declared.json()["data"]["sources"]
    # Replays preserve each server-owned storage identity.
    replay = client.post(base + "/declarations", headers=admin_headers, json=body)
    assert replay.json()["data"]["sources"] == sources
    for (content, _), source in zip(payloads, sources, strict=True):
        initialized = client.post(
            base + "/chunked/init",
            headers=admin_headers,
            json={
                "workspace_id": ws.id,
                "source_id": source["source_id"],
                "total_chunks": 1,
            },
        )
        assert initialized.status_code == 200
        written = client.put(
            base + "/chunked/0",
            params={"source_id": source["source_id"]},
            headers=admin_headers,
            content=content,
        )
        assert written.status_code == 200
    completed = client.post(
        base + "/chunked/complete", headers=admin_headers, json={"workspace_id": ws.id}
    )
    assert completed.status_code == 200, completed.text
    db_session.expire_all()
    parse_collection_upload_session(db_session, created.json()["data"]["id"])
    episodes = db_session.query(Episode).filter_by(data_package_id=package.id).all()
    assert len(episodes) == 2
    assert len({episode.source_fingerprint for episode in episodes}) == 2


class FakeCloud:
    """One in-memory object store behind both the browser multipart API and the provider."""

    def __init__(self, db):
        self.db = db
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict] = {}
        self.downloads: list[tuple[str, str]] = []

    @staticmethod
    def _etag(key):
        return "etag-" + hashlib.sha256(key.encode()).hexdigest()[:16]

    def init(self, bucket, key):
        upload_id = f"upload-{uuid4().hex}"
        self.uploads[upload_id] = {"bucket": bucket, "key": key, "parts": {}}
        return upload_id

    def sign(self, bucket, key, upload_id, part_number, content_md5=None):
        headers = {"Content-Type": "application/octet-stream"}
        if content_md5:
            headers["Content-MD5"] = content_md5
        return f"https://oss.test/{upload_id}/{part_number}", 300, headers

    def put_part(self, url, content, headers):
        """What the storage service does with one signed UploadPart request."""
        _prefix, upload_id, part = url.rsplit("/", 2)
        digest = base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()
        assert headers.get("Content-MD5") == digest, "storage rejects a corrupted part"
        self.uploads[upload_id]["parts"][int(part)] = content
        return f"part-etag-{part}"

    def list_parts(self, _bucket, _key, upload_id):
        parts = self.uploads[upload_id]["parts"]
        return [
            oss_client.OSSMultipartPart(
                number=number, etag=f"part-etag-{number}", size=len(parts[number])
            )
            for number in sorted(parts)
        ]

    def complete(self, _bucket, key, upload_id, parts):
        stored = self.uploads[upload_id]["parts"]
        self.objects[key] = b"".join(stored[part.number] for part in parts)

    def object_info(self, _bucket, key):
        if key not in self.objects:
            return None
        return oss_client.OSSObjectInfo(
            size=len(self.objects[key]),
            etag=self._etag(key),
            crc64="1234",
            version_id=None,
            metadata={},
        )

    def head(self, ref):
        if ref.object_key not in self.objects:
            raise StorageObjectNotFound(ref.object_key)
        size = len(self.objects[ref.object_key])
        return StorageObjectRef(
            ref.bucket_role, ref.object_key, None, self._etag(ref.object_key), size, None
        )

    def download_file(self, ref, destination):
        assert not self.db.in_transaction(), "download must not hold a DB transaction"
        self.downloads.append((ref.bucket_role, ref.object_key))
        head = self.head(ref)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(self.objects[ref.object_key])
        return head

    def put_worker_object(self, ref, source_path):
        data = Path(source_path).read_bytes()
        self.objects[ref.object_key] = data
        return StorageObjectRef(
            "process",
            ref.object_key,
            None,
            self._etag(ref.object_key),
            len(data),
            hashlib.sha256(data).hexdigest(),
        )

    def delete_exact(self, ref):
        self.objects.pop(ref.object_key, None)

    def sign_get(self, ref, *, expires=900):
        assert ref.object_key in self.objects
        return f"https://oss.test/get/{ref.object_key}"


def _upload_file(client, headers, cloud, session_id, workspace_id, target, payload):
    base = f"/api/v1/upload-sessions/{session_id}/oss"
    initialized = client.post(
        f"{base}/init",
        headers=headers,
        json={"workspace_id": workspace_id, "total_size_bytes": len(payload), **target},
    )
    assert initialized.status_code == 200, initialized.text
    part_size = initialized.json()["data"]["part_size_bytes"]
    parts = []
    for index in range(initialized.json()["data"]["total_parts"]):
        chunk = payload[index * part_size : (index + 1) * part_size]
        md5 = base64.b64encode(hashlib.md5(chunk, usedforsecurity=False).digest()).decode()
        signed = client.post(
            f"{base}/sign-part",
            headers=headers,
            json={
                "workspace_id": workspace_id,
                "part_number": index + 1,
                "content_md5": md5,
                **target,
            },
        )
        assert signed.status_code == 200, signed.text
        data = signed.json()["data"]
        parts.append(
            {"part_number": index + 1, "etag": cloud.put_part(data["url"], chunk, data["headers"])}
        )
    completed = client.post(
        f"{base}/complete",
        headers=headers,
        json={"workspace_id": workspace_id, "parts": parts, **target},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()["data"]["status"]


def test_client_precheck_bytes_reach_review_without_downloading_the_mcap(
    client, db_session, admin_headers, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    cloud = FakeCloud(db_session)
    monkeypatch.setattr(oss_client, "init_browser_multipart_upload", cloud.init)
    monkeypatch.setattr(oss_client, "sign_browser_upload_part", cloud.sign)
    monkeypatch.setattr(oss_client, "list_browser_multipart_parts", cloud.list_parts)
    monkeypatch.setattr(oss_client, "complete_browser_multipart_upload", cloud.complete)
    monkeypatch.setattr(oss_client, "object_info", cloud.object_info)
    monkeypatch.setattr("data.infra.storage_provider.get_storage_provider", lambda: cloud)

    root = _episode(tmp_path)
    preview_paths = generate_previews(root)
    data = (root / "data.mcap").read_bytes()
    metadata_text = (root / "metadata.json").read_text(encoding="utf-8")
    metadata = json.loads(metadata_text)
    digest = hashlib.sha256(data).hexdigest()
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    capabilities = client.get(
        "/api/v1/upload-sessions/capabilities",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    ).json()["data"]["client_admission"]
    assert capabilities["enabled"] is True
    created = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [package.package_uid],
            "upload_mode": "oss_multipart",
        },
    )
    session_id = created.json()["data"]["id"]
    item = {
        "package_uid": package.package_uid,
        "source": {
            "episode_id": metadata["episode_id"],
            "start_ns": str(metadata["timing"]["start_timestamp_ns"]),
            "end_ns": str(metadata["timing"]["end_timestamp_ns"]),
            "metadata_sha256": hashlib.sha256(metadata_text.encode()).hexdigest(),
            "data_mcap_sha256": digest,
        },
        "metadata_text": metadata_text,
        "data_file": {"path": "data.mcap", "size_bytes": len(data), "sha256": digest},
        "client_admission": {
            "qrdf_version": capabilities["accepted_qrdf_versions"][0],
            "policy_version": capabilities["accepted_policy_versions"][0],
            "report": {"ok": True, "issues": [], "media_validation": {"ok": True, "issues": []}},
            "files": [
                {
                    "path": path,
                    "size_bytes": (root / path).stat().st_size,
                    "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
                }
                for path in preview_paths
            ],
        },
    }
    declared = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "items": [item]},
    )
    assert declared.status_code == 200, declared.text
    [source] = declared.json()["data"]["sources"]

    statuses = [
        _upload_file(
            client,
            admin_headers,
            cloud,
            session_id,
            workspace.id,
            {"source_id": source["source_id"]},
            data,
        )
    ]
    for entry in source["preview_files"]:
        statuses.append(
            _upload_file(
                client,
                admin_headers,
                cloud,
                session_id,
                workspace.id,
                {"file_id": entry["file_id"]},
                (root / entry["path"]).read_bytes(),
            )
        )
    assert statuses[-1] == "uploaded" and set(statuses[:-1]) == {"uploading"}

    db_session.expire_all()  # HTTP requests advanced rows through their own sessions.
    parse_collection_upload_session(db_session, session_id)
    upload = db_session.get(CollectionUploadSession, session_id)
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    db_session.expire_all()

    result = db_session.get(CollectionUploadSession, session_id).result_json
    preview_keys = {f["object_key"] for f in result["oss_multipart_files"].values()}
    raw_key = result["oss_multipart"]["object_key"]
    assert len(preview_keys) == len(preview_paths) > 0
    assert {key for _role, key in cloud.downloads} == preview_keys
    assert raw_key not in {key for _role, key in cloud.downloads}
    assert all(role == "process" for role, _key in cloud.downloads)
    detail = client.get(
        f"/api/v1/data-packages/{package.id}",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    ).json()["data"]
    assert detail["status"] == "pending_intake_review"
    [episode] = detail["episodes"]
    assert episode["integrity_source"] == "client"
    assert episode["preview_available"] is True
    [status] = detail["qrdf_facts"]["source_admission"]["sources"]
    assert status["source_id"] == source["source_id"]
    assert status["status"] == "ready" and status["integrity_source"] == "client"
    previews = client.get(
        f"/api/v1/data-packages/{package.id}/episodes/{episode['id']}/preview-urls",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    )
    assert previews.status_code == 200, previews.text
    assert previews.json()["data"]["streams"]

    fact = current_episode_admission_fact(db_session, episode_id=episode["id"])
    assert fact.integrity_source == "client"
    objects = fact_objects(fact)
    by_kind: dict[str, list] = {}
    for obj in objects:
        by_kind.setdefault(obj.kind, []).append(obj)
    assert [o.path for o in by_kind["data"]] == ["data.mcap"]
    assert by_kind["data"][0].ref["object_key"] == raw_key
    assert [o.path for o in by_kind["metadata"]] == ["metadata.json"]
    assert [o.path for o in by_kind["admission_report"]] == ["admission-report.json"]
    assert [o.path for o in by_kind["preview_manifest"]] == ["media/preview/manifest.json"]
    assert {o.path for o in by_kind["preview_video"]} == {
        p for p in preview_paths if p.endswith(".mp4")
    }
    assert {o.path for o in by_kind["preview_timeline"]} == {
        p for p in preview_paths if p.endswith(".timeline.json")
    }
