"""Opt-in real MinIO lifecycle checks for the Data backend.

These tests deliberately require ``RUN_REAL_MINIO_E2E=1``.  The ordinary test
suite uses an in-memory transport for legacy paths; this module proves that
the signed multipart API reaches the disposable MinIO instance and that a
corrupt QRDF source is isolated by the real durable parse worker.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.collection_api_fixtures import (
    make_annotator_reviewer,
    make_assigned_package,
    make_project,
    make_workspace,
)

from data.database import Episode, JobRun
from data.models.annotation_work import AnnotationWorkItem, ReviewWorkItem
from data.models.catalog_dataset import CatalogDatasetExport, CatalogDatasetVersion
from data.models.collection_upload import CollectionUploadSession
from data.models.data_asset import DataAsset
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.duance_imports import build_duance_import_manifest


def _user_headers(user):
    from data.utils.helpers import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user.id, user.email, user.role)}"}


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_MINIO_E2E") != "1",
    reason="set RUN_REAL_MINIO_E2E=1 to run against the disposable MinIO service",
)


def _multipart_upload(
    client, headers, *, workspace_id: int, project_id: int, package_uid: str, payload: bytes
):
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": f"e2e-{uuid4().hex}",
        "timing": {"start_timestamp_ns": 0, "end_timestamp_ns": 1_000_000_000},
        "capture": {"mode": "ego", "app_version": "e2e"},
        "sensors": {"cameras": []},
        "data_file": "data.mcap",
    }
    metadata_text = json.dumps(metadata, separators=(",", ":"))
    digest = hashlib.sha256(payload).hexdigest()
    metadata_digest = hashlib.sha256(metadata_text.encode()).hexdigest()
    created = client.post(
        "/api/v1/upload-sessions",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "collection_project_id": project_id,
            "package_uids": [package_uid],
            "upload_mode": "oss_multipart",
        },
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["data"]["id"]
    declared = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "items": [
                {
                    "package_uid": package_uid,
                    "source": {
                        "episode_id": metadata["episode_id"],
                        "start_ns": "0",
                        "end_ns": "1000000000",
                        "metadata_sha256": metadata_digest,
                        "data_mcap_sha256": digest,
                    },
                    "metadata_text": metadata_text,
                    "data_file": {
                        "path": "data.mcap",
                        "size_bytes": len(payload),
                        "sha256": digest,
                    },
                }
            ],
        },
    )
    assert declared.status_code == 200, declared.text
    initialized = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/init",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "total_size_bytes": len(payload),
            "content_type": "application/octet-stream",
        },
    )
    assert initialized.status_code == 200, initialized.text
    signed = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/sign-part",
        headers=headers,
        json={"workspace_id": workspace_id, "part_number": 1},
    )
    assert signed.status_code == 200, signed.text
    upload_url = signed.json()["data"]["url"]
    request = urllib.request.Request(upload_url, data=payload, method="PUT")
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
        etag = response.headers.get("ETag")
    assert etag
    completed = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/complete",
        headers=headers,
        json={"workspace_id": workspace_id, "parts": [{"part_number": 1, "etag": etag.strip('"')}]},
    )
    assert completed.status_code == 200, completed.text
    return session_id, digest


def test_real_minio_signed_multipart_and_corrupt_qrdf_isolated(
    client, db_session, admin_headers, monkeypatch
):
    """Upload through signed HTTP, then run the actual durable parse handler."""
    # Loopback is intentionally rejected for production browser CORS, but the
    # disposable MinIO fixture has no public hostname.  This opt-in test
    # explicitly enables the provider browser endpoint for its own process.
    from data.config import settings
    from data.infra import oss_client

    monkeypatch.setattr(settings, "storage_browser_endpoint", "http://127.0.0.1:19000")
    monkeypatch.setattr(settings, "oss_browser_direct_enabled", True)
    monkeypatch.setattr(oss_client, "_is_public_oss_browser_endpoint", lambda _value: True)
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    payload = b"this is intentionally corrupt QRDF data"
    session_id, digest = _multipart_upload(
        client,
        admin_headers,
        workspace_id=workspace.id,
        project_id=project.id,
        package_uid=package.package_uid,
        payload=payload,
    )
    db_session.expire_all()
    session = db_session.get(CollectionUploadSession, session_id)
    assert session is not None
    result_json = session.result_json or {}
    source = result_json.get("oss_multipart")
    if not isinstance(source, dict):
        source = next(iter((result_json.get("oss_multipart_sources") or {}).values()))
    assert source["completion_state"] == "completed"
    assert source["provider_identity"]["size_bytes"] == len(payload)
    assert source["provider_identity"]["etag"]

    job = db_session.scalar(
        select(JobRun).where(
            JobRun.kind == "collection_upload_parse", JobRun.resource_id == session_id
        )
    )
    assert job is not None
    from data.tasks.batch_workers import batch_execute_task

    result = batch_execute_task.run(job.id, job.recovery_dispatch_token)
    assert result["status"] == "succeeded"
    db_session.expire_all()
    refreshed = db_session.get(CollectionUploadSession, session_id)
    assert refreshed is not None
    # Parse accepts the declared envelope and schedules the real admission
    # worker; malformed MCAP rejection is recorded at that later gate.
    assert refreshed.status == "succeeded"
    admission_jobs = db_session.scalars(
        select(JobRun).where(
            JobRun.kind == "collection_upload_admission",
            JobRun.resource_id == session_id,
        )
    ).all()
    for admission_job in admission_jobs:
        admission_result = batch_execute_task.run(
            admission_job.id, admission_job.recovery_dispatch_token
        )
        assert admission_result["status"] == "succeeded"


def _run_real_minio_qrdf_batch_admits_eight_and_isolates_two_corrupt(
    client, db_session, admin_headers, monkeypatch, tmp_path: Path, request
):
    """Real QRDF SDK payloads survive signed multipart, parse, and admission."""
    from qrdf.converters.source.examples import SyntheticTeleOpImporter

    from data.config import settings
    from data.infra import oss_client

    monkeypatch.setattr(settings, "storage_browser_endpoint", "http://127.0.0.1:19000")
    monkeypatch.setattr(settings, "oss_browser_direct_enabled", True)
    # Task 7B intentionally invokes this real flow twice for independent
    # workspaces; never share a worker scratch tree between those runs.
    storage_root = tmp_path / f"worker-storage-{uuid4().hex}"
    storage_root.mkdir()
    monkeypatch.setattr(settings, "storage_root", str(storage_root))
    monkeypatch.setattr(oss_client, "_is_public_oss_browser_endpoint", lambda _value: True)
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project, hours="8.00")

    # Produce every valid source with the QRDF SDK.  The two corrupt sources
    # retain valid metadata and declaration hashes; only their MCAP bytes are
    # replaced, so admission must fail on MCAP integrity.
    importer = SyntheticTeleOpImporter()
    declarations = []
    payloads = []
    source_ids = []
    expected_digests = {}
    cleanup_refs = []

    def remember(value):
        cleanup_refs.append(
            {
                k: value[k]
                for k in ("bucket_role", "object_key", "version_id", "etag", "size_bytes", "sha256")
            }
        )

    def cleanup_objects():
        from data.infra.object_storage import StorageObjectRef
        from data.infra.storage_provider import get_storage_provider

        provider = get_storage_provider()
        errors = []
        for value in cleanup_refs:
            ref = StorageObjectRef(**value)
            try:
                provider.delete_exact(ref)
                try:
                    provider.head(ref)
                    errors.append(f"object still exists: {ref.object_key}:{ref.version_id}")
                except Exception:
                    pass
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise AssertionError("MinIO cleanup failed: " + "; ".join(errors))

    request.addfinalizer(cleanup_objects)
    with TemporaryDirectory(dir=tmp_path) as fixture_dir:
        fixture_root = Path(fixture_dir)
        for index in range(10):
            dataset = fixture_root / f"source-{index}"
            episode_uid = f"episode_{index:06d}"
            importer.import_episode(dataset, dataset, episode_id=episode_uid, num_steps=8)
            episode_root = dataset / "episodes" / episode_uid
            metadata = json.loads((episode_root / "metadata.json").read_text(encoding="utf-8"))
            metadata["data_file"] = f"data-{index}.mcap"
            metadata_text = json.dumps(metadata, separators=(",", ":"))
            payload = (episode_root / "data.mcap").read_bytes()
            if index >= 8:
                payload = b"corrupt MCAP payload"
            data_digest = hashlib.sha256(payload).hexdigest()
            metadata_digest = hashlib.sha256(metadata_text.encode()).hexdigest()
            source = {
                "episode_id": metadata["episode_id"],
                "start_ns": str(metadata["timing"]["start_timestamp_ns"]),
                "end_ns": str(metadata["timing"]["end_timestamp_ns"]),
                "metadata_sha256": metadata_digest,
                "data_mcap_sha256": data_digest,
            }
            data_file = {
                "path": f"data-{index}.mcap",
                "size_bytes": len(payload),
                "sha256": data_digest,
            }
            manifest = build_duance_import_manifest(
                source=source, metadata_text=metadata_text, data_file=data_file
            )
            source_id = hashlib.sha256(
                f"{package.package_uid}:{manifest.source_key}".encode()
            ).hexdigest()
            declarations.append(
                {
                    "package_uid": package.package_uid,
                    "source": source,
                    "metadata_text": metadata_text,
                    "data_file": data_file,
                }
            )
            payloads.append(payload)
            source_ids.append(source_id)
            expected_digests[source_id] = data_digest

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
        assert created.status_code == 200, created.text
        session_id = created.json()["data"]["id"]
        declared = client.post(
            f"/api/v1/upload-sessions/{session_id}/declarations",
            headers=admin_headers,
            json={"workspace_id": workspace.id, "items": declarations},
        )
        assert declared.status_code == 200, declared.text

        for source_id, payload in zip(source_ids, payloads, strict=False):
            initialized = client.post(
                f"/api/v1/upload-sessions/{session_id}/oss/init",
                headers=admin_headers,
                json={
                    "workspace_id": workspace.id,
                    "total_size_bytes": len(payload),
                    "content_type": "application/octet-stream",
                    "source_id": source_id,
                },
            )
            assert initialized.status_code == 200, initialized.text
            signed = client.post(
                f"/api/v1/upload-sessions/{session_id}/oss/sign-part",
                headers=admin_headers,
                json={"workspace_id": workspace.id, "part_number": 1, "source_id": source_id},
            )
            assert signed.status_code == 200, signed.text
            request = urllib.request.Request(
                signed.json()["data"]["url"], data=payload, method="PUT"
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                assert response.status == 200
                etag = response.headers.get("ETag")
            assert etag
            completed = client.post(
                f"/api/v1/upload-sessions/{session_id}/oss/complete",
                headers=admin_headers,
                json={
                    "workspace_id": workspace.id,
                    "parts": [{"part_number": 1, "etag": etag.strip('"')}],
                    "source_id": source_id,
                },
            )
            assert completed.status_code == 200, completed.text

    db_session.expire_all()
    parse_job = db_session.scalar(
        select(JobRun).where(
            JobRun.kind == "collection_upload_parse",
            JobRun.resource_id == session_id,
        )
    )
    assert parse_job is not None
    from data.tasks.batch_workers import batch_execute_task

    assert (
        batch_execute_task.run(parse_job.id, parse_job.recovery_dispatch_token)["status"]
        == "succeeded"
    )
    db_session.expire_all()
    admission_jobs = db_session.scalars(
        select(JobRun).where(
            JobRun.kind == "collection_upload_admission",
            JobRun.resource_type == "episode",
            JobRun.detail_json["upload_session_id"].as_string() == session_id,
        )
    ).all()
    assert len(admission_jobs) == 10
    for admission_job in admission_jobs:
        assert (
            batch_execute_task.run(admission_job.id, admission_job.recovery_dispatch_token)[
                "status"
            ]
            == "succeeded"
        )

    db_session.expire_all()
    session = db_session.get(CollectionUploadSession, session_id)
    assert session is not None
    states = list((session.result_json or {}).get("admission_sources", {}).values())
    assert len(states) == 10
    by_source = {state["source_id"]: state for state in states}
    assert set(by_source) == set(source_ids)
    assert sum(state.get("status") == "ready" for state in states) == 8, [
        (state.get("status"), state.get("error_code")) for state in states
    ]
    failed = [state for state in states if state.get("status") == "failed"]
    assert len(failed) == 2
    assert all(state.get("error_code") for state in failed)
    episodes = db_session.scalars(
        select(Episode).where(Episode.data_package_id == package.id).order_by(Episode.id)
    ).all()
    assert len(episodes) == 10
    facts = [
        db_session.query(EpisodeAdmissionFact).filter_by(episode_id=ep.id, is_current=True).one()
        for ep in episodes
    ]
    assert (
        sum(
            f.integrity_status == "passed"
            and f.preview_status == "ready"
            and f.output_verification_status == "verified"
            for f in facts
        )
        == 8
    )
    assert sum(f.integrity_status == "failed" for f in facts) == 2
    provider = __import__(
        "data.infra.storage_provider", fromlist=["get_storage_provider"]
    ).get_storage_provider()
    StorageObjectRef = __import__(
        "data.infra.object_storage", fromlist=["StorageObjectRef"]
    ).StorageObjectRef
    all_raw = []
    for state in states:
        raw = state["source_object"]
        remember(raw)
        assert raw["object_key"] and raw.get("version_id") or raw.get("etag")
        checked = provider.head(
            StorageObjectRef(
                **{
                    k: raw[k]
                    for k in (
                        "bucket_role",
                        "object_key",
                        "version_id",
                        "etag",
                        "size_bytes",
                        "sha256",
                    )
                }
            )
        )
        assert checked.object_key == raw["object_key"] and checked.size_bytes == raw["size_bytes"]
        all_raw.append(
            (raw["object_key"], raw.get("version_id"), raw.get("etag"), state["source_id"])
        )
    assert len({item[:3] for item in all_raw}) == 10
    assert {item[3] for item in all_raw} == set(source_ids)
    from data.services.episode_objects import data_object, fact_objects, object_of_kind

    raw_refs = []
    process_refs = []
    for episode, fact in zip(episodes, facts, strict=False):
        state = next(item for item in states if item.get("episode_id") == episode.id)
        if fact.integrity_status == "passed":
            assert fact.error_code == ""
            assert fact.objects_json and fact.report_ref_json
            assert fact.preview_status == "ready"
            assert fact.output_verification_status == "verified"
            objects = fact_objects(fact)
            source_ref = data_object(objects).ref
            assert source_ref["sha256"] == expected_digests[state["source_id"]]
            raw_refs.append(source_ref)
            # The admission_report object is unique per attempt (uuid4 upload
            # prefix), so it stands in for "this attempt's process objects"
            # the same way the single process tar ref used to.
            process_ref = object_of_kind(objects, "admission_report").ref
            process_refs.append(process_ref)
            assert (
                provider.head(
                    __import__(
                        "data.infra.object_storage", fromlist=["StorageObjectRef"]
                    ).StorageObjectRef(
                        **{
                            k: source_ref[k]
                            for k in (
                                "bucket_role",
                                "object_key",
                                "version_id",
                                "etag",
                                "size_bytes",
                                "sha256",
                            )
                        }
                    )
                ).object_key
                == source_ref["object_key"]
            )
            for item in objects:
                if item.role != "process":
                    continue
                remember(item.ref)
                assert (
                    provider.head(
                        __import__(
                            "data.infra.object_storage", fromlist=["StorageObjectRef"]
                        ).StorageObjectRef(
                            **{
                                k: item.ref[k]
                                for k in (
                                    "bucket_role",
                                    "object_key",
                                    "version_id",
                                    "etag",
                                    "size_bytes",
                                    "sha256",
                                )
                            }
                        )
                    ).object_key
                    == item.ref["object_key"]
                )
        else:
            assert fact.error_code
            assert fact.integrity_status == "failed"
            assert fact.preview_status in {"failed", "not_applicable"}
            assert fact.output_verification_status == "failed"
            assert fact.objects_json == []
    assert len({(r["object_key"], r["version_id"], r["etag"]) for r in raw_refs}) == 8
    assert len({(r["object_key"], r["version_id"], r["etag"]) for r in process_refs}) == 8

    calls = {"head": 0, "get": 0}
    provider_type = type(provider)
    original_head = provider_type.head
    original_download = provider_type.download_file

    def deny_head(*args, **kwargs):
        calls["head"] += 1
        raise AssertionError("provider HEAD forbidden during DB-only gate")

    def deny_get(*args, **kwargs):
        calls["get"] += 1
        raise AssertionError("provider GET forbidden during DB-only gate")

    monkeypatch.setattr(provider_type, "head", deny_head)
    monkeypatch.setattr(provider_type, "download_file", deny_get)
    reviewed = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "verdict": "approved"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert len(reviewed.json()["data"]["accepted_episode_ids"]) == 8
    annotator, reviewer = make_annotator_reviewer(db_session, workspace)
    batch_response = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": f"e2e-{session_id}",
            "data_package_ids": [package.id],
            "annotation_enabled": True,
            "annotator_user_ids": [annotator.id],
            "reviewer_user_id": reviewer.id,
            "review_mode": "single",
        },
    )
    assert batch_response.status_code == 200, batch_response.text
    batch_item = batch_response.json()["data"]
    assert batch_item["episode_count"] == 8
    assert set(batch_item["episode_ids"]) == {
        episode.id
        for episode, fact in zip(episodes, facts, strict=False)
        if fact.integrity_status == "passed"
    }
    from data.models.data_batch import DataBatchEpisode

    persisted_members = (
        db_session.query(DataBatchEpisode).filter_by(data_batch_id=batch_item["id"]).all()
    )
    assert {row.episode_id for row in persisted_members} == set(batch_item["episode_ids"])
    assert len(persisted_members) == 8
    assert calls == {"head": 0, "get": 0}
    db_session.expire_all()
    package = db_session.get(type(package), package.id)
    assert (
        sum(
            item.get("status") == "failed"
            for item in (package.qrdf_facts_json or {})
            .get("source_admission", {})
            .get("sources", [])
        )
        == 2
    )
    monkeypatch.setattr(provider_type, "head", original_head)
    monkeypatch.setattr(provider_type, "download_file", original_download)


def test_real_minio_qrdf_batch_admits_eight_and_isolates_two_corrupt(
    client, db_session, admin_headers, monkeypatch, tmp_path: Path, request
):
    """Real QRDF SDK payloads survive signed multipart, parse, and admission."""
    _run_real_minio_qrdf_batch_admits_eight_and_isolates_two_corrupt(
        client, db_session, admin_headers, monkeypatch, tmp_path, request
    )


def test_real_minio_orphan_multipart_is_cancelled(
    client, db_session, admin_headers, monkeypatch, request
):
    """Cancellation leaves no active multipart upload for the exact source."""
    workspace = make_workspace(db_session)
    from data.config import settings
    from data.infra import oss_client

    monkeypatch.setattr(settings, "storage_browser_endpoint", "http://127.0.0.1:19000")
    monkeypatch.setattr(settings, "oss_browser_direct_enabled", True)
    monkeypatch.setattr(oss_client, "_is_public_oss_browser_endpoint", lambda _value: True)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
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
    assert created.status_code == 200
    session_id = created.json()["data"]["id"]
    payload = b"orphan multipart payload"
    metadata_text = json.dumps(
        {
            "qrdf_version": "0.2.0",
            "episode_id": "orphan",
            "data_file": "data.mcap",
            "timing": {"start_timestamp_ns": 0, "end_timestamp_ns": 1},
            "capture": {"mode": "ego", "app_version": "e2e"},
            "sensors": {"cameras": []},
        }
    )
    digest = hashlib.sha256(payload).hexdigest()
    declared = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [
                {
                    "package_uid": package.package_uid,
                    "source": {
                        "episode_id": "orphan",
                        "start_ns": "0",
                        "end_ns": "1",
                        "metadata_sha256": hashlib.sha256(metadata_text.encode()).hexdigest(),
                        "data_mcap_sha256": digest,
                    },
                    "metadata_text": metadata_text,
                    "data_file": {
                        "path": "data.mcap",
                        "size_bytes": len(payload),
                        "sha256": digest,
                    },
                }
            ],
        },
    )
    assert declared.status_code == 200, declared.text
    initialized = client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/init",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "total_size_bytes": len(payload),
            "content_type": "application/octet-stream",
        },
    )
    assert initialized.status_code == 200, initialized.text
    db_session.expire_all()
    stored = db_session.get(CollectionUploadSession, session_id)
    declaration = (stored.result_json or {}).get("oss_multipart")
    assert isinstance(declaration, dict)
    upload_id = declaration["upload_id"]
    object_key = declaration["object_key"]
    unrelated_package = make_assigned_package(db_session, workspace, project)
    unrelated_created = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [unrelated_package.package_uid],
            "upload_mode": "oss_multipart",
        },
    )
    assert unrelated_created.status_code == 200
    unrelated_id = unrelated_created.json()["data"]["id"]
    unrelated_payload = b"unrelated-active-part"
    unrelated_metadata = json.dumps(
        {
            "qrdf_version": "0.2.0",
            "episode_id": "unrelated",
            "data_file": "data.mcap",
            "timing": {"start_timestamp_ns": 0, "end_timestamp_ns": 1},
            "capture": {"mode": "ego", "app_version": "e2e"},
            "sensors": {"cameras": []},
        },
        separators=(",", ":"),
    )
    unrelated_digest = hashlib.sha256(unrelated_payload).hexdigest()
    unrelated_source = {
        "episode_id": "unrelated",
        "start_ns": "0",
        "end_ns": "1",
        "metadata_sha256": hashlib.sha256(unrelated_metadata.encode()).hexdigest(),
        "data_mcap_sha256": unrelated_digest,
    }
    declared = client.post(
        f"/api/v1/upload-sessions/{unrelated_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [
                {
                    "package_uid": unrelated_package.package_uid,
                    "source": unrelated_source,
                    "metadata_text": unrelated_metadata,
                    "data_file": {
                        "path": "data.mcap",
                        "size_bytes": len(unrelated_payload),
                        "sha256": unrelated_digest,
                    },
                }
            ],
        },
    )
    assert declared.status_code == 200, declared.text
    from data.services.duance_imports import build_duance_import_manifest

    source_key = build_duance_import_manifest(
        source=unrelated_source,
        metadata_text=unrelated_metadata,
        data_file={
            "path": "data.mcap",
            "size_bytes": len(unrelated_payload),
            "sha256": unrelated_digest,
        },
    ).source_key
    unrelated_source_id = hashlib.sha256(
        f"{unrelated_package.package_uid}:{source_key}".encode()
    ).hexdigest()
    unrelated_init = client.post(
        f"/api/v1/upload-sessions/{unrelated_id}/oss/init",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "total_size_bytes": len(unrelated_payload),
            "source_id": unrelated_source_id,
        },
    )
    assert unrelated_init.status_code == 200, unrelated_init.text
    unrelated_sign = client.post(
        f"/api/v1/upload-sessions/{unrelated_id}/oss/sign-part",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "part_number": 1, "source_id": unrelated_source_id},
    )
    assert unrelated_sign.status_code == 200, unrelated_sign.text
    with urllib.request.urlopen(
        urllib.request.Request(
            unrelated_sign.json()["data"]["url"], data=unrelated_payload, method="PUT"
        ),
        timeout=30,
    ) as response:
        assert response.status == 200
    db_session.expire_all()
    unrelated_record = db_session.get(CollectionUploadSession, unrelated_id)
    unrelated_result = unrelated_record.result_json or {}
    unrelated_decl = (unrelated_result.get("oss_multipart_sources", {}) or {}).get(
        unrelated_source_id
    ) or unrelated_result.get("oss_multipart")
    assert unrelated_decl and unrelated_decl["upload_id"]
    unrelated_upload_id = unrelated_decl["upload_id"]
    unrelated_object_key = unrelated_decl["object_key"]

    def cleanup_unrelated():
        provider = __import__(
            "data.infra.storage_provider", fromlist=["get_storage_provider"]
        ).get_storage_provider()
        bucket_name = provider.bucket_config.buckets["raw"]
        provider.client.abort_multipart_upload(
            Bucket=bucket_name, Key=unrelated_object_key, UploadId=unrelated_upload_id
        )

    request.addfinalizer(cleanup_unrelated)
    completed_package = make_assigned_package(db_session, workspace, project)
    completed_id, _ = _multipart_upload(
        client,
        admin_headers,
        workspace_id=workspace.id,
        project_id=project.id,
        package_uid=completed_package.package_uid,
        payload=b"completed-object",
    )
    completed_session = db_session.get(CollectionUploadSession, completed_id)
    completed_decl = (completed_session.result_json or {}).get("oss_multipart") or next(
        iter((completed_session.result_json or {}).get("oss_multipart_sources", {}).values())
    )
    completed_ref = completed_decl["provider_identity"]

    def cleanup_completed():
        from data.infra.object_storage import StorageObjectRef
        from data.infra.storage_provider import get_storage_provider

        provider = get_storage_provider()
        ref = StorageObjectRef(**completed_ref)
        provider.delete_exact(ref)
        try:
            provider.head(ref)
        except Exception:
            return
        raise AssertionError("completed cancellation object cleanup failed")

    request.addfinalizer(cleanup_completed)
    cancelled = client.post(
        f"/api/v1/upload-sessions/{session_id}/cancel",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"
    from data.infra.storage_provider import get_storage_provider

    provider = get_storage_provider()
    bucket = provider.bucket_config.buckets["raw"]
    listed = provider.client.list_multipart_uploads(Bucket=bucket, Prefix=object_key)
    assert all(item.get("UploadId") != upload_id for item in listed.get("Uploads", []))
    unrelated_listed = provider.client.list_multipart_uploads(
        Bucket=bucket, Prefix=unrelated_object_key
    )
    assert any(
        item.get("UploadId") == unrelated_upload_id for item in unrelated_listed.get("Uploads", [])
    )
    provider.client.abort_multipart_upload(
        Bucket=bucket, Key=unrelated_object_key, UploadId=unrelated_upload_id
    )
    assert (
        provider.head(
            __import__("data.infra.object_storage", fromlist=["StorageObjectRef"]).StorageObjectRef(
                **completed_ref
            )
        ).object_key
        == completed_ref["object_key"]
    )


def test_real_minio_all_corrupt_package_cannot_create_empty_batch(
    client, db_session, admin_headers, monkeypatch, tmp_path: Path, request
):
    """A separately uploaded corrupt package has zero eligible episodes."""
    from data.config import settings
    from data.infra import oss_client
    from data.tasks.batch_workers import batch_execute_task

    monkeypatch.setattr(settings, "storage_browser_endpoint", "http://127.0.0.1:19000")
    monkeypatch.setattr(settings, "oss_browser_direct_enabled", True)
    root = tmp_path / "all-corrupt-storage"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_root", str(root))
    monkeypatch.setattr(oss_client, "_is_public_oss_browser_endpoint", lambda _value: True)
    cleanup_refs = []

    def remember(value):
        cleanup_refs.append(
            {
                k: value[k]
                for k in ("bucket_role", "object_key", "version_id", "etag", "size_bytes", "sha256")
            }
        )

    def cleanup_objects():
        from data.infra.object_storage import StorageObjectRef
        from data.infra.storage_provider import get_storage_provider

        provider = get_storage_provider()
        errors = []
        for value in cleanup_refs:
            ref = StorageObjectRef(**value)
            try:
                provider.delete_exact(ref)
                try:
                    provider.head(ref)
                    errors.append(f"object still exists: {ref.object_key}:{ref.version_id}")
                except Exception:
                    pass
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise AssertionError("MinIO cleanup failed: " + "; ".join(errors))

    request.addfinalizer(cleanup_objects)
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project, hours="1.00")
    session_id, _ = _multipart_upload(
        client,
        admin_headers,
        workspace_id=workspace.id,
        project_id=project.id,
        package_uid=package.package_uid,
        payload=b"all-corrupt-mcap",
    )
    job = db_session.scalar(
        select(JobRun).where(
            JobRun.kind == "collection_upload_parse", JobRun.resource_id == session_id
        )
    )
    assert job is not None
    assert batch_execute_task.run(job.id, job.recovery_dispatch_token)["status"] == "succeeded"
    admission = db_session.scalar(
        select(JobRun).where(
            JobRun.kind == "collection_upload_admission",
            JobRun.resource_type == "episode",
            JobRun.detail_json["upload_session_id"].as_string() == session_id,
        )
    )
    assert admission is not None
    assert (
        batch_execute_task.run(admission.id, admission.recovery_dispatch_token)["status"]
        == "succeeded"
    )
    db_session.expire_all()
    corrupt_session = db_session.get(CollectionUploadSession, session_id)
    corrupt_decl = (corrupt_session.result_json or {}).get("oss_multipart") or next(
        iter((corrupt_session.result_json or {}).get("oss_multipart_sources", {}).values())
    )
    remember(corrupt_decl["provider_identity"])
    reviewed = client.post(
        f"/api/v1/data-packages/{package.id}/intake-review",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "verdict": "approved"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["data"]["accepted_episode_ids"] == []
    rejected = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": f"empty-{session_id}",
            "data_package_ids": [package.id],
        },
    )
    assert rejected.status_code in {409, 422}
    assert (
        db_session.query(__import__("data.models.data_batch", fromlist=["DataBatch"]).DataBatch)
        .filter_by(name=f"empty-{session_id}")
        .one_or_none()
        is None
    )


def test_real_minio_governed_assets_catalog_exports_and_train_delivery(
    client, db_session, admin_headers, monkeypatch, tmp_path: Path, request
):
    """Carry two real QRDF admissions through annotation, export and Train read.

    This deliberately reuses the Task 7A uploader twice instead of seeding an
    admission fact: each asset therefore has an independently parsed QRDF
    source, eight admitted episodes and two quarantined corrupt sources.
    """
    train_env = {
        name: os.environ.get(name, "").strip()
        for name in (
            "TEST_TRAIN_STORAGE_ENDPOINT",
            "TEST_TRAIN_STORAGE_ACCESS_KEY_ID",
            "TEST_TRAIN_STORAGE_SECRET_ACCESS_KEY",
        )
    }
    if not all(train_env.values()):
        pytest.skip(
            "set TEST_TRAIN_STORAGE_ENDPOINT, TEST_TRAIN_STORAGE_ACCESS_KEY_ID, "
            "and TEST_TRAIN_STORAGE_SECRET_ACCESS_KEY for the read-only Train delivery check"
        )

    # This is intentionally a real API/signed-multipart/durable-worker setup,
    # not a fixture fact. Its finalizers remain attached to this request and
    # delete every raw/process version after this test completes.
    _run_real_minio_qrdf_batch_admits_eight_and_isolates_two_corrupt(
        client, db_session, admin_headers, monkeypatch, tmp_path, request
    )
    _run_real_minio_qrdf_batch_admits_eight_and_isolates_two_corrupt(
        client, db_session, admin_headers, monkeypatch, tmp_path, request
    )

    from data.models.data_batch import DataBatch
    from data.tasks.asset_tasks import execute_asset_publish
    from data.tasks.batch_workers import batch_execute_task
    from data.tasks.governance_tasks import execute_governance_run

    db_session.expire_all()
    batches = db_session.scalars(
        select(DataBatch).where(DataBatch.name.like("e2e-%")).order_by(DataBatch.id.desc()).limit(2)
    ).all()
    assert len(batches) == 2
    assert len({batch.workspace_id for batch in batches}) == 2
    for batch in batches:
        # Annotation-enabled governance deliberately ends at the real
        # ``annotating`` gate; the subsequent API work-item lifecycle below
        # is what advances the batch to publication.
        assert execute_governance_run.run(batch.id)["status"] == "annotating"

    # API submission/approval is the only workflow mutation here.  The
    # production Celery publish task then creates the immutable asset.
    for batch in batches:
        annotations = db_session.scalars(
            select(AnnotationWorkItem)
            .where(AnnotationWorkItem.data_batch_id == batch.id)
            .order_by(AnnotationWorkItem.id)
        ).all()
        # The governed API assigns one work item per admitted package; this
        # package contains all eight accepted episodes, so the item member
        # snapshot (rather than row count) is the eight-episode contract.
        assert sum(len(item.episode_members_json or []) for item in annotations) == 8
        annotator = db_session.get(
            __import__("data.database", fromlist=["User"]).User, annotations[0].assignee_user_id
        )
        assert annotator is not None
        for item in annotations:
            episode_payloads = {
                str(member["episode_id"]): {
                    "kind": "episode_annotation",
                    "qrdf_version": "0.2.0",
                    "source": {"episode_id": "placeholder", "data_sha256": "0" * 64},
                    "episode": {"outcome": "success"},
                    "tracks": [],
                }
                for member in item.episode_members_json
            }
            drafted = client.patch(
                f"/api/v1/annotation-work-items/{item.id}",
                headers=_user_headers(annotator),
                json={
                    "workspace_id": batch.workspace_id,
                    "draft_json": {"episodes": episode_payloads},
                },
            )
            assert drafted.status_code == 200, drafted.text
            response = client.post(
                f"/api/v1/annotation-work-items/{item.id}/submit",
                headers=_user_headers(annotator),
                json={"workspace_id": batch.workspace_id},
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["status"] == "submitted"

        db_session.expire_all()
        reviews = db_session.scalars(
            select(ReviewWorkItem)
            .where(ReviewWorkItem.data_batch_id == batch.id)
            .order_by(ReviewWorkItem.id)
        ).all()
        assert len(reviews) == len(annotations)
        reviewer = db_session.get(
            __import__("data.database", fromlist=["User"]).User, reviews[0].assignee_user_id
        )
        assert reviewer is not None
        for item in reviews:
            response = client.post(
                f"/api/v1/review-work-items/{item.id}/approve",
                headers=_user_headers(reviewer),
                json={"workspace_id": batch.workspace_id},
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["status"] == "approved"
        # Explicit synchronous task invocation retains the production task
        # boundary while avoiding a test dependence on a running Celery worker.
        assert execute_asset_publish.run(batch.id)["data_asset_id"]

    db_session.expire_all()
    assets = db_session.scalars(
        select(DataAsset).where(DataAsset.data_batch_id.in_([batch.id for batch in batches]))
    ).all()
    assert len(assets) == 2
    assert {asset.workspace_id for asset in assets} == {batch.workspace_id for batch in batches}
    assert all(len(asset.episode_ids_json) == 8 for asset in assets)
    assert all(asset.source_snapshot_id and asset.source_snapshot_json for asset in assets)
    for asset in assets:
        revisions = [
            episode["annotation_revision"] for episode in asset.source_snapshot_json["episodes"]
        ]
        assert len(revisions) == 8
        assert all(
            revision.get("id")
            and revision.get("payload", {}).get("episode", {}).get("outcome") == "success"
            for revision in revisions
        )
    assert all(db_session.get(DataBatch, batch.id).status == "published" for batch in batches)

    dataset_response = client.post(
        "/api/v1/catalog-datasets",
        headers=admin_headers,
        json={
            "name": f"real-e2e-{uuid4().hex}",
            "description": "real MinIO task 7B",
            "source_kind": "qrdf_assets",
        },
    )
    assert dataset_response.status_code == 200, dataset_response.text
    dataset_id = dataset_response.json()["data"]["id"]

    # Register immediately: a failure while creating a version or dispatching
    # an export must still discover and delete every exact persisted object.
    def cleanup_catalog():
        from botocore.exceptions import ClientError

        from data.database import SessionLocal
        from data.infra.object_storage import StorageObjectRef
        from data.infra.storage_provider import get_storage_provider
        from data.models.catalog_dataset import (
            CatalogDataset,
            CatalogDatasetVersionAsset,
        )

        cleanup_db = SessionLocal()
        errors = []
        refs: list[StorageObjectRef] = []
        try:
            version_ids = list(
                cleanup_db.scalars(
                    select(CatalogDatasetVersion.id).where(
                        CatalogDatasetVersion.dataset_id == dataset_id
                    )
                )
            )
            exports = (
                cleanup_db.scalars(
                    select(CatalogDatasetExport).where(
                        CatalogDatasetExport.version_id.in_(version_ids)
                    )
                ).all()
                if version_ids
                else []
            )
            for export in exports:
                for value in list((export.manifest_json or {}).values()) + list(
                    (export.cleanup_json or {}).get("planned_refs", [])
                ):
                    if isinstance(value, dict) and value.get("bucket_role") == "export":
                        try:
                            refs.append(
                                StorageObjectRef(
                                    **{
                                        key: value[key]
                                        for key in (
                                            "bucket_role",
                                            "object_key",
                                            "version_id",
                                            "etag",
                                            "size_bytes",
                                            "sha256",
                                        )
                                    }
                                )
                            )
                        except (KeyError, TypeError) as exc:
                            errors.append(f"invalid persisted export cleanup identity: {exc}")
        except Exception as exc:
            cleanup_db.rollback()
            errors.append(f"catalog cleanup discovery failed: {exc}")

        provider = get_storage_provider()
        seen: set[tuple[str, str, str | None]] = set()
        for ref in refs:
            identity = (ref.bucket_role, ref.object_key, ref.version_id)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                kwargs = {
                    "Bucket": provider.bucket_config.buckets[ref.bucket_role],
                    "Key": ref.object_key,
                }
                if ref.version_id:
                    kwargs["VersionId"] = ref.version_id
                try:
                    response = provider.client.head_object(**kwargs)
                except ClientError as exc:
                    code = str((exc.response.get("Error") or {}).get("Code") or "")
                    if code in {"404", "NoSuchKey", "NotFound", "NoSuchVersion"}:
                        continue
                    errors.append(f"export lookup failed: {exc}")
                    continue
                exact_ref = StorageObjectRef(
                    bucket_role=ref.bucket_role,
                    object_key=ref.object_key,
                    version_id=str(response.get("VersionId") or ref.version_id or "") or None,
                    etag=str(response.get("ETag") or ref.etag).strip('"'),
                    size_bytes=int(response.get("ContentLength") or 0),
                    sha256=str((response.get("Metadata") or {}).get("sha256") or ref.sha256 or "")
                    or None,
                )
                provider.delete_exact(exact_ref)
                verify_kwargs = {
                    "Bucket": provider.bucket_config.buckets[exact_ref.bucket_role],
                    "Key": exact_ref.object_key,
                }
                if exact_ref.version_id:
                    verify_kwargs["VersionId"] = exact_ref.version_id
                try:
                    provider.client.head_object(**verify_kwargs)
                    errors.append(
                        f"export still exists: {exact_ref.object_key}:{exact_ref.version_id}"
                    )
                except ClientError as exc:
                    code = str((exc.response.get("Error") or {}).get("Code") or "")
                    if code not in {"404", "NoSuchKey", "NotFound", "NoSuchVersion"}:
                        errors.append(f"export absence verification failed: {exc}")
            except Exception as exc:
                errors.append(f"export cleanup failed: {exc}")

        if not errors:
            try:
                export_ids = [str(export.id) for export in exports]
                if export_ids:
                    cleanup_db.query(JobRun).filter(
                        JobRun.kind == "catalog_export", JobRun.resource_id.in_(export_ids)
                    ).delete(synchronize_session=False)
                if version_ids:
                    cleanup_db.query(CatalogDatasetExport).filter(
                        CatalogDatasetExport.version_id.in_(version_ids)
                    ).delete(synchronize_session=False)
                    cleanup_db.query(CatalogDatasetVersionAsset).filter(
                        CatalogDatasetVersionAsset.version_id.in_(version_ids)
                    ).delete(synchronize_session=False)
                    cleanup_db.query(CatalogDatasetVersion).filter(
                        CatalogDatasetVersion.id.in_(version_ids)
                    ).delete(synchronize_session=False)
                cleanup_db.query(CatalogDataset).filter(CatalogDataset.id == dataset_id).delete(
                    synchronize_session=False
                )
                cleanup_db.commit()
                if (
                    version_ids
                    and cleanup_db.get(CatalogDatasetVersion, version_ids[0]) is not None
                ):
                    errors.append("targeted catalog version still exists")
            except Exception as exc:
                cleanup_db.rollback()
                errors.append(f"catalog row cleanup failed: {exc}")
        else:
            cleanup_db.rollback()
        cleanup_db.close()
        if errors:
            raise AssertionError("catalog export cleanup failed: " + "; ".join(errors))

    request.addfinalizer(cleanup_catalog)

    version_response = client.post(
        f"/api/v1/catalog-datasets/{dataset_id}/versions",
        headers=admin_headers,
        json={"data_asset_ids": [asset.id for asset in assets]},
    )
    assert version_response.status_code == 200, version_response.text
    version_payload = version_response.json()["data"]
    version_id = version_payload["id"]
    assert version_payload["data_asset_ids"] == [asset.id for asset in assets]
    assert len(version_payload["source_snapshot_json"].get("assets", [])) == 2
    frozen_snapshot = version_payload["source_snapshot_json"]

    def run_export(export_format: str, *, key: str):
        response = client.post(
            f"/api/v1/catalog-datasets/versions/{version_id}/export",
            headers=admin_headers,
            json={"format": export_format, "idempotency_key": key},
        )
        assert response.status_code == 200, response.text
        export_id = response.json()["data"]["id"]
        db_session.expire_all()
        job = db_session.scalar(
            select(JobRun).where(
                JobRun.kind == "catalog_export",
                JobRun.resource_type == "artifact",
                JobRun.resource_id == str(export_id),
            )
        )
        assert job is not None
        outcome = batch_execute_task.run(job.id, job.recovery_dispatch_token)
        assert outcome["status"] == "succeeded", outcome
        db_session.expire_all()
        descriptor_response = client.get(
            f"/api/v1/catalog-datasets/exports/{export_id}", headers=admin_headers
        )
        assert descriptor_response.status_code == 200, descriptor_response.text
        descriptor = descriptor_response.json()["data"]
        assert descriptor["status"] == "succeeded"
        assert descriptor["oss_uri"] and descriptor["size_bytes"] > 0 and descriptor["sha256"]
        assert not any(
            "credential" in key.lower() or "secret" in key.lower() or "access_key" in key.lower()
            for key in descriptor
        )
        return descriptor

    # A one-shot provider-boundary fault must fail durably, clean its precise
    # attempt, and leave a preserved failed row before API retry creates a new
    # prefix.  No source rows or fake job handlers are involved.
    from data.infra.storage_provider import get_storage_provider
    from data.services.catalog_export_jobs import CatalogExportError

    provider = get_storage_provider()
    original_put = type(provider).put_worker_object
    fault = {"raised": False}

    def fail_once(self, ref, source_path):
        if ref.bucket_role == "export" and not fault["raised"]:
            fault["raised"] = True
            # This represents a deterministic storage rejection rather than a
            # transport interruption, so the durable job records the attempt
            # as terminally failed and the API retry path is exercised.
            # Simulate a storage provider that committed the write before it
            # reported a terminal rejection. The handler must discover and
            # delete that exact planned artifact identity.
            original_put(self, ref, source_path)
            raise CatalogExportError("task7b deterministic export provider fault")
        return original_put(self, ref, source_path)

    monkeypatch.setattr(type(provider), "put_worker_object", fail_once)
    failed_response = client.post(
        f"/api/v1/catalog-datasets/versions/{version_id}/export",
        headers=admin_headers,
        json={"format": "qrdf_0_2", "idempotency_key": f"real-failure-{uuid4().hex}"},
    )
    assert failed_response.status_code == 200, failed_response.text
    failed_id = failed_response.json()["data"]["id"]
    failed_job = db_session.scalar(
        select(JobRun).where(JobRun.kind == "catalog_export", JobRun.resource_id == str(failed_id))
    )
    assert failed_job is not None
    failed_outcome = batch_execute_task.run(failed_job.id, failed_job.recovery_dispatch_token)
    assert failed_outcome["status"] == "failed"
    db_session.expire_all()
    failed = db_session.get(CatalogDatasetExport, failed_id)
    assert failed and failed.status == "failed" and failed.error_message
    assert failed.cleanup_json and failed.cleanup_json.get("planned_refs")
    deleted_refs = failed.cleanup_json.get("deleted_refs") or []
    assert len(deleted_refs) == 1
    from botocore.exceptions import ClientError

    from data.infra.object_storage import StorageObjectRef

    failed_ref = StorageObjectRef(**deleted_refs[0])
    assert failed_ref.object_key == f"{failed.output_prefix}/dataset.tar.gz"
    with pytest.raises(ClientError) as missing:
        provider.client.head_object(
            Bucket=provider.bucket_config.buckets[failed_ref.bucket_role],
            Key=failed_ref.object_key,
            VersionId=failed_ref.version_id,
        )
    assert str((missing.value.response.get("Error") or {}).get("Code") or "") in {
        "404",
        "NoSuchKey",
        "NotFound",
        "NoSuchVersion",
    }
    assert not failed.cleanup_json.get("orphan_refs")
    monkeypatch.setattr(type(provider), "put_worker_object", original_put)
    retry_response = client.post(
        f"/api/v1/catalog-datasets/exports/{failed_id}/retry", headers=admin_headers
    )
    assert retry_response.status_code == 200, retry_response.text
    retry_id = retry_response.json()["data"]["id"]
    retry_job = db_session.scalar(
        select(JobRun).where(JobRun.kind == "catalog_export", JobRun.resource_id == str(retry_id))
    )
    assert retry_job is not None
    assert (
        batch_execute_task.run(retry_job.id, retry_job.recovery_dispatch_token)["status"]
        == "succeeded"
    )
    db_session.expire_all()
    qrdf_descriptor_response = client.get(
        f"/api/v1/catalog-datasets/exports/{retry_id}", headers=admin_headers
    )
    assert qrdf_descriptor_response.status_code == 200, qrdf_descriptor_response.text
    qrdf_export = qrdf_descriptor_response.json()["data"]
    assert qrdf_export["status"] == "succeeded"
    assert qrdf_export["attempt"] == failed.attempt + 1
    assert qrdf_export["oss_uri"]
    assert not qrdf_export["oss_uri"].endswith(failed.output_prefix + "/dataset.tar.gz")

    lerobot_export = run_export("lerobot_3_0", key=f"real-lerobot-{uuid4().hex}")
    assert qrdf_export["oss_uri"].rsplit("/", 1)[0] != lerobot_export["oss_uri"].rsplit("/", 1)[0]

    # Train gets only explicit read-only credentials, never the API/admin
    # provider client.  oss_uri is identity-only, so parse it before GetObject.
    import boto3

    def verify_train_delivery(descriptor, expected_format: str):
        parsed = urlsplit(descriptor["oss_uri"])
        assert parsed.scheme == "oss" and parsed.netloc
        artifact_key = parsed.path.lstrip("/")
        response = train.get_object(Bucket=parsed.netloc, Key=artifact_key)
        payload = response["Body"].read()
        assert len(payload) == descriptor["size_bytes"]
        assert hashlib.sha256(payload).hexdigest() == descriptor["sha256"]
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            names = archive.getnames()
            assert "quicstudio_export.json" in names
            manifest = json.load(archive.extractfile("quicstudio_export.json"))
            assert manifest["format"] == expected_format
            assert manifest["attempt"] == descriptor["attempt"]
            if expected_format == "qrdf_0_2":
                source_manifest = json.load(archive.extractfile("quicdata_manifest.json"))
                assert len(source_manifest["episodes"]) == 16
                assert all(
                    item["annotation_revision"].get("id") for item in source_manifest["episodes"]
                )
                assert sum(name.endswith("annotation.json") for name in names) == 16
            else:
                info = json.load(archive.extractfile("meta/info.json"))
                assert info["total_episodes"] == 16
        completion_key = artifact_key.rsplit("/", 1)[0] + "/completion.json"
        completion = json.loads(
            train.get_object(Bucket=parsed.netloc, Key=completion_key)["Body"].read()
        )
        assert completion["sha256"] == descriptor["sha256"]
        assert completion["attempt"] == descriptor["attempt"]
        assert completion["format"] == expected_format

    train = boto3.client(
        "s3",
        endpoint_url=train_env["TEST_TRAIN_STORAGE_ENDPOINT"],
        aws_access_key_id=train_env["TEST_TRAIN_STORAGE_ACCESS_KEY_ID"],
        aws_secret_access_key=train_env["TEST_TRAIN_STORAGE_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("TEST_TRAIN_STORAGE_REGION", "us-east-1"),
    )
    verify_train_delivery(qrdf_export, "qrdf_0_2")
    verify_train_delivery(lerobot_export, "lerobot_3_0")

    db_session.expire_all()
    version = db_session.get(CatalogDatasetVersion, version_id)
    assert version and version.source_snapshot_json == frozen_snapshot


def test_real_minio_native_lerobot_direct_upload_stays_out_of_collection_workflow(
    client, db_session, admin_headers, monkeypatch, tmp_path: Path, request
):
    """A complete native source is validated from raw without a batch or asset.

    This catches the legacy implementation that discovers an external scope,
    creates a ``Batch``, and copies the native dataset to ``export`` before it
    can appear in the catalog.
    """
    from qrdf.converters.qrdf_to_lerobot import convert_dataset
    from qrdf.converters.source.examples import SyntheticTeleOpImporter
    from qrdf.converters.validate_lerobot import validate_lerobot_dataset

    from data.config import settings
    from data.infra import oss_client
    from data.infra.object_storage import (
        ObjectStorageError,
        StorageObjectNotFound,
        StorageObjectRef,
    )
    from data.infra.storage_provider import get_storage_provider
    from data.models.data_batch import DataBatch
    from data.models.native_lerobot_direct import NativeLerobotDirectSource
    from data.tasks.batch_workers import batch_execute_task

    monkeypatch.setattr(settings, "storage_browser_endpoint", "http://127.0.0.1:19000")
    monkeypatch.setattr(settings, "oss_browser_direct_enabled", True)
    monkeypatch.setattr(oss_client, "_is_public_oss_browser_endpoint", lambda _value: True)
    scratch = tmp_path / f"native-lerobot-scratch-{uuid4().hex}"
    scratch.mkdir()
    monkeypatch.setattr(settings, "scratch_root", str(scratch))

    qrdf_dir = tmp_path / "native-qrdf"
    lerobot_dir = tmp_path / "native-lerobot"
    qrdf_dir.mkdir()
    SyntheticTeleOpImporter().import_episode(
        "/synthetic/native-source",
        qrdf_dir,
        num_steps=3,
        include_cameras=False,
    )
    convert_dataset(qrdf_dir, lerobot_dir, lerobot_version="v3.0")
    assert validate_lerobot_dataset(lerobot_dir).ok
    payloads = {
        path.relative_to(lerobot_dir).as_posix(): path.read_bytes()
        for path in sorted(lerobot_dir.rglob("*"))
        if path.is_file() and path.relative_to(lerobot_dir).parts[0] in {"data", "meta", "videos"}
    }
    assert payloads

    workspace = make_workspace(db_session)
    from data.database import Batch
    from data.models.annotation_work import AnnotationWorkItem, ReviewWorkItem

    before = {
        "batches": db_session.query(Batch).count(),
        "data_batches": db_session.query(DataBatch).count(),
        "assets": db_session.query(DataAsset).count(),
        "facts": db_session.query(EpisodeAdmissionFact).count(),
        "annotation": db_session.query(AnnotationWorkItem).count(),
        "review": db_session.query(ReviewWorkItem).count(),
    }
    declared = client.post(
        "/api/v1/native-lerobot-direct-uploads",
        headers=admin_headers,
        json={
            "name": f"Native MinIO {uuid4().hex[:8]}",
            "description": "real direct source",
            "source_workspace_id": workspace.id,
            "robot_type": "unknown",
            "dataset_id": f"native-e2e-{uuid4().hex[:8]}",
            "objects": [
                {
                    "path": path,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for path, payload in payloads.items()
            ],
        },
    )
    assert declared.status_code == 200, declared.text
    source_id = declared.json()["data"]["id"]
    declared_raw_refs = {
        path: StorageObjectRef(
            bucket_role="export",
            object_key=f"export/v1/native-lerobot-direct/{source_id}/objects/{path}",
            version_id=None,
            etag="",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        for path, payload in payloads.items()
    }
    cleanup_refs_by_source: dict[str, list[StorageObjectRef]] = {
        source_id: [
            *declared_raw_refs.values(),
            StorageObjectRef(
                bucket_role="export",
                object_key=f"export/v1/native-lerobot-direct/{source_id}/complete.json",
                version_id=None,
                etag="",
                size_bytes=0,
                sha256=None,
            ),
        ]
    }
    cleanup_uploads_by_source: dict[str, list[tuple[StorageObjectRef, str]]] = {source_id: []}

    def cleanup_exact_raw_objects() -> None:
        from botocore.exceptions import BotoCoreError, ClientError
        from sqlalchemy.exc import SQLAlchemyError

        from data.database import JobQueueSlot
        from data.models.catalog_dataset import (
            CatalogDataset,
            CatalogDatasetVersionAsset,
        )

        provider = get_storage_provider()
        db_session.rollback()
        refs = [ref for values in cleanup_refs_by_source.values() for ref in values]
        multipart_refs = [
            ref
            for ref in refs
            if ref.bucket_role in {"raw", "export"} and "/objects/" in ref.object_key
        ]
        sources: list[NativeLerobotDirectSource] = []
        version_ids: set[int] = set()
        dataset_ids: set[int] = set()
        exports: list[CatalogDatasetExport] = []
        uploads = [value for values in cleanup_uploads_by_source.values() for value in values]
        for cleanup_source_id in cleanup_refs_by_source:
            source = db_session.get(NativeLerobotDirectSource, cleanup_source_id)
            if source is None:
                continue
            sources.append(source)
            for item in source.objects:
                ref_json = item.provider_ref_json or {}
                if ref_json:
                    refs.append(StorageObjectRef(**ref_json))
                multipart_refs.append(
                    StorageObjectRef(
                        bucket_role="export",
                        object_key=item.object_key,
                        version_id=None,
                        etag="",
                        size_bytes=int(item.size_bytes),
                        sha256=item.sha256,
                    )
                )
                if item.upload_id:
                    uploads.append(
                        (
                            StorageObjectRef(
                                bucket_role="export",
                                object_key=item.object_key,
                                version_id=None,
                                etag="",
                                size_bytes=int(item.size_bytes),
                                sha256=item.sha256,
                            ),
                            str(item.upload_id),
                        )
                    )
            marker = source.marker_ref_json or {}
            if marker:
                refs.append(StorageObjectRef(**marker))
            if source.catalog_dataset_version_id:
                version_ids.add(int(source.catalog_dataset_version_id))
            if source.catalog_dataset_id:
                dataset_ids.add(int(source.catalog_dataset_id))

        for version_id in version_ids:
            source_exports = db_session.scalars(
                select(CatalogDatasetExport).where(CatalogDatasetExport.version_id == version_id)
            ).all()
            exports.extend(source_exports)
            for export in source_exports:
                if export.output_prefix:
                    refs.extend(
                        (
                            StorageObjectRef(
                                bucket_role="export",
                                object_key=f"{export.output_prefix}/dataset.tar.gz",
                                version_id=None,
                                etag="",
                                size_bytes=0,
                                sha256=None,
                            ),
                            StorageObjectRef(
                                bucket_role="export",
                                object_key=f"{export.output_prefix}/completion.json",
                                version_id=None,
                                etag="",
                                size_bytes=0,
                                sha256=None,
                            ),
                        )
                    )
                values = list((export.manifest_json or {}).values()) + list(
                    (export.cleanup_json or {}).get("planned_refs", [])
                )
                for value in values:
                    if isinstance(value, dict) and value.get("bucket_role") == "export":
                        refs.append(StorageObjectRef(**value))
        errors: list[str] = []

        # The concrete MinIO provider exposes exact multipart listing. Only a
        # matching declared key/upload ID is ever aborted; broad prefix cleanup
        # could erase another test or a separately declared upload.
        storage_client = getattr(provider, "client", None)
        bucket_config = getattr(provider, "bucket_config", None)
        buckets = getattr(bucket_config, "buckets", {}) if bucket_config is not None else {}
        # Persisted upload IDs are included as a second source of truth. The
        # exact-key listing also catches a provider-created multipart whose
        # database update was lost before its upload ID could be persisted.
        multipart_refs.extend(ref for ref, _upload_id in uploads)
        seen_multipart_refs: set[str] = set()
        for ref in multipart_refs:
            if ref.object_key in seen_multipart_refs:
                continue
            seen_multipart_refs.add(ref.object_key)
            target_bucket = buckets.get(ref.bucket_role)
            if storage_client is None or not target_bucket:
                errors.append(f"multipart cleanup is unavailable for {ref.object_key}")
                continue
            try:
                active_before = [
                    str(item.get("UploadId") or "")
                    for item in storage_client.list_multipart_uploads(
                        Bucket=target_bucket,
                        Prefix=ref.object_key,
                    ).get("Uploads", [])
                    if item.get("Key") == ref.object_key
                ]
                for upload_id in active_before:
                    provider.abort_multipart(ref, upload_id)
                active_after = [
                    str(item.get("UploadId") or "")
                    for item in storage_client.list_multipart_uploads(
                        Bucket=target_bucket,
                        Prefix=ref.object_key,
                    ).get("Uploads", [])
                    if item.get("Key") == ref.object_key
                ]
                if active_after:
                    errors.append(f"multipart cleanup retained {ref.object_key}")
            except (BotoCoreError, ClientError, ObjectStorageError) as exc:
                errors.append(f"multipart cleanup failed {ref.object_key}: {exc}")

        seen: set[tuple[str, str, str | None]] = set()
        for ref in refs:
            identity = (ref.bucket_role, ref.object_key, ref.version_id)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                persisted = provider.head(ref)
            except StorageObjectNotFound:
                continue
            except ObjectStorageError as exc:
                errors.append(f"object lookup failed {ref.object_key}: {exc}")
                continue
            try:
                provider.delete_exact(persisted)
                try:
                    provider.head(persisted)
                except StorageObjectNotFound:
                    pass
                except ObjectStorageError as exc:
                    errors.append(f"object verification failed {ref.object_key}: {exc}")
                else:
                    errors.append(f"object cleanup retained {ref.object_key}")
            except ObjectStorageError as exc:
                errors.append(f"object cleanup failed {ref.object_key}: {exc}")
        if errors:
            raise AssertionError("native LeRobot direct cleanup failed: " + "; ".join(errors))

        try:
            export_ids = [str(export.id) for export in exports]
            source_ids = [source.id for source in sources]
            job_ids: list[str] = []
            if export_ids:
                job_ids.extend(
                    str(job_id)
                    for job_id in db_session.scalars(
                        select(JobRun.id).where(
                            JobRun.kind == "catalog_export",
                            JobRun.resource_id.in_(export_ids),
                        )
                    )
                )
            if source_ids:
                job_ids.extend(
                    str(job_id)
                    for job_id in db_session.scalars(
                        select(JobRun.id).where(
                            JobRun.kind == "native_lerobot_direct_validate",
                            JobRun.resource_id.in_(source_ids),
                        )
                    )
                )
            if job_ids:
                db_session.query(JobQueueSlot).filter(JobQueueSlot.job_id.in_(job_ids)).delete(
                    synchronize_session=False
                )
            if export_ids:
                db_session.query(JobRun).filter(
                    JobRun.kind == "catalog_export",
                    JobRun.resource_id.in_(export_ids),
                ).delete(synchronize_session=False)
                db_session.query(CatalogDatasetExport).filter(
                    CatalogDatasetExport.id.in_([export.id for export in exports])
                ).delete(synchronize_session=False)
            if source_ids:
                db_session.query(JobRun).filter(
                    JobRun.kind == "native_lerobot_direct_validate",
                    JobRun.resource_id.in_(source_ids),
                ).delete(synchronize_session=False)
            for source in sources:
                db_session.delete(source)
            db_session.flush()
            for version_id in version_ids:
                db_session.query(CatalogDatasetVersionAsset).filter(
                    CatalogDatasetVersionAsset.version_id == version_id
                ).delete(synchronize_session=False)
                db_session.query(CatalogDatasetVersion).filter(
                    CatalogDatasetVersion.id == version_id
                ).delete(synchronize_session=False)
            for dataset_id in dataset_ids:
                has_versions = (
                    db_session.query(CatalogDatasetVersion.id)
                    .filter(CatalogDatasetVersion.dataset_id == dataset_id)
                    .first()
                )
                if has_versions is None:
                    db_session.query(CatalogDataset).filter(CatalogDataset.id == dataset_id).delete(
                        synchronize_session=False
                    )
            db_session.commit()
        except SQLAlchemyError as exc:
            db_session.rollback()
            raise AssertionError(f"native LeRobot direct database cleanup failed: {exc}") from exc

    request.addfinalizer(cleanup_exact_raw_objects)
    declared_items = {item["path"]: item for item in declared.json()["data"]["objects"]}
    assert set(declared_items) == set(payloads)
    for path, payload in payloads.items():
        item = declared_items[path]
        initialized = client.post(
            f"/api/v1/native-lerobot-direct-uploads/{source_id}/objects/{item['id']}/multipart/init",
            headers=admin_headers,
        )
        assert initialized.status_code == 200, initialized.text
        db_session.expire_all()
        persisted_source = db_session.get(NativeLerobotDirectSource, source_id)
        assert persisted_source is not None
        persisted_item = next(row for row in persisted_source.objects if row.id == item["id"])
        assert persisted_item.upload_id
        cleanup_uploads_by_source[source_id].append(
            (declared_raw_refs[path], str(persisted_item.upload_id))
        )
        signed = client.post(
            f"/api/v1/native-lerobot-direct-uploads/{source_id}/objects/{item['id']}/multipart/sign-part",
            headers=admin_headers,
            json={"part_number": 1},
        )
        assert signed.status_code == 200, signed.text
        with urllib.request.urlopen(
            urllib.request.Request(signed.json()["data"]["url"], data=payload, method="PUT"),
            timeout=30,
        ) as response:
            assert response.status == 200
            etag = response.headers.get("ETag")
        assert etag
        completed = client.post(
            f"/api/v1/native-lerobot-direct-uploads/{source_id}/objects/{item['id']}/multipart/complete",
            headers=admin_headers,
            json={"parts": [{"part_number": 1, "etag": etag.strip('"')}]},
        )
        assert completed.status_code == 200, completed.text

    # Simulate the crash gap after the provider creates an exact-key multipart
    # upload but before its upload ID is durably recorded. The cancellation API
    # must enumerate the provider state, not merely trust the database column.
    cleanup_probe_payload = b"native-lerobot-direct-cleanup-probe"
    cleanup_probe = client.post(
        "/api/v1/native-lerobot-direct-uploads",
        headers=admin_headers,
        json={
            "name": f"Native cleanup probe {uuid4().hex[:8]}",
            "description": "real multipart cleanup probe",
            "source_workspace_id": workspace.id,
            "robot_type": "unknown",
            "dataset_id": f"native-cleanup-{uuid4().hex[:8]}",
            "objects": [
                {
                    "path": "meta/cleanup-probe.json",
                    "size_bytes": len(cleanup_probe_payload),
                    "sha256": hashlib.sha256(cleanup_probe_payload).hexdigest(),
                }
            ],
        },
    )
    assert cleanup_probe.status_code == 200, cleanup_probe.text
    cleanup_probe_data = cleanup_probe.json()["data"]
    cleanup_probe_id = cleanup_probe_data["id"]
    cleanup_probe_ref = StorageObjectRef(
        bucket_role="export",
        object_key=(
            f"export/v1/native-lerobot-direct/{cleanup_probe_id}/objects/meta/cleanup-probe.json"
        ),
        version_id=None,
        etag="",
        size_bytes=len(cleanup_probe_payload),
        sha256=hashlib.sha256(cleanup_probe_payload).hexdigest(),
    )
    cleanup_refs_by_source[cleanup_probe_id] = [
        cleanup_probe_ref,
        StorageObjectRef(
            bucket_role="export",
            object_key=f"export/v1/native-lerobot-direct/{cleanup_probe_id}/complete.json",
            version_id=None,
            etag="",
            size_bytes=0,
            sha256=None,
        ),
    ]
    cleanup_uploads_by_source[cleanup_probe_id] = []
    cleanup_probe_item = cleanup_probe_data["objects"][0]
    probe_initialized = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{cleanup_probe_id}/objects/"
        f"{cleanup_probe_item['id']}/multipart/init",
        headers=admin_headers,
    )
    assert probe_initialized.status_code == 200, probe_initialized.text
    db_session.expire_all()
    persisted_probe = db_session.get(NativeLerobotDirectSource, cleanup_probe_id)
    assert persisted_probe is not None and persisted_probe.objects[0].upload_id
    cleanup_probe_upload_id = str(persisted_probe.objects[0].upload_id)
    cleanup_uploads_by_source[cleanup_probe_id].append((cleanup_probe_ref, cleanup_probe_upload_id))
    probe_signed = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{cleanup_probe_id}/objects/"
        f"{cleanup_probe_item['id']}/multipart/sign-part",
        headers=admin_headers,
        json={"part_number": 1},
    )
    assert probe_signed.status_code == 200, probe_signed.text
    with urllib.request.urlopen(
        urllib.request.Request(
            probe_signed.json()["data"]["url"],
            data=cleanup_probe_payload,
            method="PUT",
        ),
        timeout=30,
    ) as response:
        assert response.status == 200
    probe_provider = get_storage_provider()
    active_probe_uploads = probe_provider.client.list_multipart_uploads(
        Bucket=probe_provider.bucket_config.buckets["export"],
        Prefix=cleanup_probe_ref.object_key,
    ).get("Uploads", [])
    assert any(
        item.get("Key") == cleanup_probe_ref.object_key
        and item.get("UploadId") == cleanup_probe_upload_id
        for item in active_probe_uploads
    )
    persisted_probe.objects[0].upload_id = ""
    db_session.commit()
    cancelled_probe = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{cleanup_probe_id}/cancel",
        headers=admin_headers,
    )
    assert cancelled_probe.status_code == 200, cancelled_probe.text
    assert cancelled_probe.json()["data"]["status"] == "cancelled"
    assert cancelled_probe.json()["data"]["objects"][0]["status"] == "cancelled"
    assert probe_provider.list_multipart_upload_ids(cleanup_probe_ref) == ()
    with pytest.raises(StorageObjectNotFound):
        probe_provider.head(cleanup_probe_ref)

    queued = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{source_id}/complete",
        headers=admin_headers,
    )
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["data"]["validation_job_id"]
    job = db_session.get(JobRun, job_id)
    assert job is not None
    from datetime import datetime, timedelta, timezone

    from data.database import JobQueueSlot, SessionLocal
    from data.services.job_runs import acquire_delivery_lease, reclaim_expired_jobs

    # Reclaim the lease *while* the old worker is blocked at the actual raw
    # provider boundary. Its transaction may leave the deterministic marker
    # in MinIO, but it must not commit source/catalog state after its lease is
    # gone; the current delivery below owns any reconciliation.
    old_delivery_token = str(job.recovery_dispatch_token or "")
    if not old_delivery_token:
        old_delivery_token = str(acquire_delivery_lease(db_session, job.id) or "")
    assert old_delivery_token
    provider = get_storage_provider()
    original_direct_download = type(provider).download_file
    lease_reclaimed = {"done": False}

    def reclaim_while_old_worker_reads_raw(self, ref, destination):
        if ref.bucket_role in {"raw", "export"} and not lease_reclaimed["done"]:
            lease_reclaimed["done"] = True
            recovery_db = SessionLocal()
            try:
                running = recovery_db.get(JobRun, job.id)
                assert running is not None and running.status == "running"
                running.lease_expires_at = datetime.now(timezone.utc).replace(
                    tzinfo=None
                ) - timedelta(seconds=1)
                recovery_db.commit()
                assert reclaim_expired_jobs(recovery_db) == [job.id]
            finally:
                recovery_db.close()
        return original_direct_download(self, ref, destination)

    monkeypatch.setattr(
        type(provider),
        "download_file",
        reclaim_while_old_worker_reads_raw,
    )
    stale_delivery = batch_execute_task.run(job.id, old_delivery_token)
    monkeypatch.setattr(type(provider), "download_file", original_direct_download)
    assert lease_reclaimed["done"]
    assert stale_delivery == {
        "job_id": job.id,
        "status": "skipped",
        "reason": "lease_lost",
    }
    db_session.expire_all()
    fenced_source = db_session.get(NativeLerobotDirectSource, source_id)
    assert fenced_source is not None
    assert fenced_source.status == "queued"
    assert not fenced_source.catalog_dataset_id
    assert not fenced_source.catalog_dataset_version_id
    assert not fenced_source.marker_ref_json
    result = batch_execute_task.run(job.id, None)
    assert result["status"] == "succeeded", result
    assert db_session.query(JobQueueSlot).filter(JobQueueSlot.job_id == job.id).count() == 0

    db_session.expire_all()
    source = db_session.get(NativeLerobotDirectSource, source_id)
    assert source is not None
    assert source.status == "succeeded"
    assert source.catalog_dataset_id and source.catalog_dataset_version_id
    assert source.marker_ref_json["bucket_role"] == "export"
    assert all(item.provider_ref_json["bucket_role"] == "export" for item in source.objects)
    assert {
        "batches": db_session.query(Batch).count(),
        "data_batches": db_session.query(DataBatch).count(),
        "assets": db_session.query(DataAsset).count(),
        "facts": db_session.query(EpisodeAdmissionFact).count(),
        "annotation": db_session.query(AnnotationWorkItem).count(),
        "review": db_session.query(ReviewWorkItem).count(),
    } == before
    native_version = db_session.get(CatalogDatasetVersion, source.catalog_dataset_version_id)
    assert native_version is not None
    assert native_version.dataset.source_kind == "lerobot_direct"
    assert native_version.source_snapshot_json["native_lerobot_direct_source_id"] == source.id
    assert (
        db_session.query(CatalogDatasetExport)
        .filter(CatalogDatasetExport.version_id == native_version.id)
        .count()
        == 0
    )
    catalog_listing = client.get("/api/v1/catalog-datasets", headers=admin_headers)
    assert catalog_listing.status_code == 200, catalog_listing.text
    assert native_version.dataset_id in {
        item["id"] for item in catalog_listing.json()["data"]["items"]
    }
    rejected = client.post(
        f"/api/v1/catalog-datasets/versions/{native_version.id}/export",
        headers=admin_headers,
        json={"format": "qrdf_0_2", "idempotency_key": f"native-qrdf-{uuid4().hex}"},
    )
    assert rejected.status_code == 422, rejected.text

    exported = client.post(
        f"/api/v1/catalog-datasets/versions/{native_version.id}/export",
        headers=admin_headers,
        json={"format": "lerobot_3_0", "idempotency_key": f"native-lerobot-{uuid4().hex}"},
    )
    assert exported.status_code == 200, exported.text
    export_id = exported.json()["data"]["id"]
    export_job = db_session.scalar(
        select(JobRun).where(
            JobRun.kind == "catalog_export",
            JobRun.resource_id == str(export_id),
        )
    )
    assert export_job is not None
    provider = get_storage_provider()
    original_download = type(provider).download_file
    timeout = {"raised": False}

    def timeout_once(self, ref, destination):
        if ref.bucket_role in {"raw", "export"} and not timeout["raised"]:
            timeout["raised"] = True
            raise TimeoutError("task7c one-shot export download timeout")
        return original_download(self, ref, destination)

    # Keep MinIO in the nominal path. Only the first external read is delayed
    # by this narrow proxy so the worker must persist a terminal failed
    # attempt and clean its planned objects. An operator retry must allocate a
    # new immutable output prefix rather than rerunning this failed attempt.
    monkeypatch.setattr(type(provider), "download_file", timeout_once)
    timed_out = batch_execute_task.run(export_job.id, export_job.recovery_dispatch_token)
    assert timed_out == {
        "job_id": export_job.id,
        "status": "failed",
        "error_code": "catalog_export_failed",
    }
    db_session.expire_all()
    failed_export = db_session.get(CatalogDatasetExport, export_id)
    assert failed_export is not None
    assert failed_export.status == "failed"
    assert failed_export.error_code == "catalog_export_failed"
    assert "one-shot export download timeout" in failed_export.error_message
    assert failed_export.cleanup_json and failed_export.cleanup_json["planned_refs"]
    assert not failed_export.cleanup_json.get("deleted_refs")
    assert not failed_export.cleanup_json.get("orphan_refs")
    for ref_json in failed_export.cleanup_json["planned_refs"]:
        with pytest.raises(ObjectStorageError):
            provider.head(StorageObjectRef(**ref_json))

    monkeypatch.setattr(type(provider), "download_file", original_download)
    retry_response = client.post(
        f"/api/v1/catalog-datasets/exports/{export_id}/retry",
        headers=admin_headers,
    )
    assert retry_response.status_code == 200, retry_response.text
    retry_export_id = retry_response.json()["data"]["id"]
    assert retry_export_id != export_id
    db_session.expire_all()
    retained_failed_export = db_session.get(CatalogDatasetExport, export_id)
    retry_export = db_session.get(CatalogDatasetExport, retry_export_id)
    assert retained_failed_export is not None and retry_export is not None
    assert retained_failed_export.status == "failed"
    assert retry_export.attempt == retained_failed_export.attempt + 1
    assert retry_export.output_prefix != retained_failed_export.output_prefix
    retry_delivery = db_session.scalar(
        select(JobRun).where(
            JobRun.kind == "catalog_export",
            JobRun.resource_id == str(retry_export_id),
        )
    )
    assert retry_delivery is not None
    export_result = batch_execute_task.run(
        retry_delivery.id, retry_delivery.recovery_dispatch_token or None
    )
    assert export_result["status"] == "succeeded", export_result
    delivered = client.get(
        f"/api/v1/catalog-datasets/exports/{retry_export_id}", headers=admin_headers
    )
    assert delivered.status_code == 200, delivered.text
    descriptor = delivered.json()["data"]
    assert descriptor["format"] == "lerobot_3_0"
    assert descriptor["oss_uri"].startswith("oss://")
    db_session.expire_all()
    export_row = db_session.get(CatalogDatasetExport, retry_export_id)
    assert export_row is not None
    artifact = StorageObjectRef(**export_row.manifest_json["artifact"])
    assert artifact.bucket_role == "export"
    archive_path = tmp_path / "native-lerobot-direct-export.tar.gz"
    get_storage_provider().download_file(artifact, str(archive_path))
    output_path = tmp_path / "native-lerobot-direct-export"
    output_path.mkdir()
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(output_path, filter="data")
    assert validate_lerobot_dataset(output_path).ok

    # Simulate losing the database completion transaction after the production
    # export handler has written the immutable artifact and marker to MinIO.
    # The execution lease must remain recoverable; a later recovery pass reads
    # the marker rather than rerunning the output prefix or claiming a false
    # successful JobRun.
    from sqlalchemy.exc import OperationalError

    from data.tasks import batch_workers

    completion_fault = client.post(
        f"/api/v1/catalog-datasets/versions/{native_version.id}/export",
        headers=admin_headers,
        json={
            "format": "lerobot_3_0",
            "idempotency_key": f"native-completion-{uuid4().hex}",
        },
    )
    assert completion_fault.status_code == 200, completion_fault.text
    completion_export_id = completion_fault.json()["data"]["id"]
    completion_job = db_session.scalar(
        select(JobRun).where(
            JobRun.kind == "catalog_export",
            JobRun.resource_id == str(completion_export_id),
        )
    )
    assert completion_job is not None
    original_complete_job = batch_workers.complete_job
    completion_faulted = {"raised": False}

    def fail_completion_commit(db, current_job_id, **kwargs):
        if current_job_id != completion_job.id or completion_faulted["raised"]:
            return original_complete_job(db, current_job_id, **kwargs)

        original_commit = db.commit

        def fail_once_at_completion_commit():
            completion_faulted["raised"] = True
            raise OperationalError(
                "COMMIT",
                {},
                RuntimeError("task7c completion commit interrupted"),
            )

        # ``complete_job`` has already updated JobRun and released its queue
        # slot when it reaches this commit. The outer worker rollback must
        # preserve recovery of the same immutable export attempt.
        monkeypatch.setattr(db, "commit", fail_once_at_completion_commit)
        try:
            return original_complete_job(db, current_job_id, **kwargs)
        finally:
            monkeypatch.setattr(db, "commit", original_commit)

    monkeypatch.setattr(batch_workers, "complete_job", fail_completion_commit)
    with pytest.raises(OperationalError, match="completion commit interrupted"):
        batch_workers.batch_execute_task.run(
            completion_job.id,
            completion_job.recovery_dispatch_token,
        )
    db_session.expire_all()
    interrupted_job = db_session.get(JobRun, completion_job.id)
    interrupted_export = db_session.get(CatalogDatasetExport, completion_export_id)
    assert interrupted_job is not None and interrupted_export is not None
    assert interrupted_job.status == "running"
    assert interrupted_export.status == "queued"
    assert not interrupted_export.oss_uri
    interrupted_job.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        seconds=1
    )
    db_session.commit()

    recovered = batch_workers.recover_expired_batch_jobs.run()
    assert completion_job.id in recovered["recovered"]
    db_session.expire_all()
    recovered_job = db_session.get(JobRun, completion_job.id)
    recovered_export = db_session.get(CatalogDatasetExport, completion_export_id)
    assert recovered_job is not None and recovered_export is not None
    assert recovered_job.status == "succeeded"
    assert recovered_export.status == "succeeded"
    assert recovered_export.oss_uri
    expected_keys = {
        f"{recovered_export.output_prefix}/dataset.tar.gz",
        f"{recovered_export.output_prefix}/completion.json",
    }
    listing = provider.client.list_objects_v2(
        Bucket=provider.bucket_config.buckets["export"],
        Prefix=f"{recovered_export.output_prefix}/",
    )
    assert {item["Key"] for item in listing.get("Contents", [])} == expected_keys

    # A deterministic tiny scratch budget is the safe real-worker analogue of
    # disk exhaustion. The export must fail before a deliverable object or
    # marker is published and its temporary tree must be removed exactly.
    original_scratch_max_bytes = settings.scratch_max_bytes
    monkeypatch.setattr(settings, "scratch_max_bytes", 1)
    scratch_limited = client.post(
        f"/api/v1/catalog-datasets/versions/{native_version.id}/export",
        headers=admin_headers,
        json={
            "format": "lerobot_3_0",
            "idempotency_key": f"native-scratch-{uuid4().hex}",
        },
    )
    assert scratch_limited.status_code == 200, scratch_limited.text
    scratch_export_id = scratch_limited.json()["data"]["id"]
    scratch_job = db_session.scalar(
        select(JobRun).where(
            JobRun.kind == "catalog_export",
            JobRun.resource_id == str(scratch_export_id),
        )
    )
    assert scratch_job is not None
    scratch_outcome = batch_execute_task.run(scratch_job.id, scratch_job.recovery_dispatch_token)
    assert scratch_outcome == {
        "job_id": scratch_job.id,
        "status": "failed",
        "error_code": "catalog_export_failed",
    }
    db_session.expire_all()
    scratch_export = db_session.get(CatalogDatasetExport, scratch_export_id)
    assert scratch_export is not None
    assert scratch_export.status == "failed"
    assert scratch_export.error_code == "catalog_export_failed"
    assert "scratch budget exceeded" in scratch_export.error_message
    assert scratch_export.cleanup_json and scratch_export.cleanup_json["planned_refs"]
    assert not scratch_export.cleanup_json.get("deleted_refs")
    assert not scratch_export.cleanup_json.get("orphan_refs")
    for ref_json in scratch_export.cleanup_json["planned_refs"]:
        with pytest.raises(ObjectStorageError):
            provider.head(StorageObjectRef(**ref_json))
    assert list(scratch.iterdir()) == []

    # A terminal failure is retained as evidence, but an operator can create
    # an isolated new attempt once the underlying system condition is fixed.
    # The retry must never reuse the failed prefix or mutate the old record.
    monkeypatch.setattr(settings, "scratch_max_bytes", original_scratch_max_bytes)
    explicit_retry = client.post(
        f"/api/v1/catalog-datasets/exports/{scratch_export_id}/retry",
        headers=admin_headers,
    )
    assert explicit_retry.status_code == 200, explicit_retry.text
    retry_export_id = explicit_retry.json()["data"]["id"]
    assert retry_export_id != scratch_export_id
    db_session.expire_all()
    retry_export = db_session.get(CatalogDatasetExport, retry_export_id)
    retained_failed_export = db_session.get(CatalogDatasetExport, scratch_export_id)
    assert retry_export is not None and retained_failed_export is not None
    assert retained_failed_export.status == "failed"
    assert retry_export.attempt == retained_failed_export.attempt + 1
    assert retry_export.output_prefix != retained_failed_export.output_prefix
    retry_job = db_session.scalar(
        select(JobRun).where(
            JobRun.kind == "catalog_export",
            JobRun.resource_id == str(retry_export_id),
        )
    )
    assert retry_job is not None
    retry_outcome = batch_execute_task.run(retry_job.id, retry_job.recovery_dispatch_token)
    assert retry_outcome["status"] == "succeeded", retry_outcome
    db_session.expire_all()
    retried_export = db_session.get(CatalogDatasetExport, retry_export_id)
    assert retried_export is not None
    assert retried_export.status == "succeeded"
    assert retried_export.oss_uri
    assert retried_export.oss_uri.endswith("/dataset.tar.gz")


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("delete", "native_lerobot_direct_object_unavailable"),
        # The frozen version itself is removed before replacement, so a
        # version-aware provider correctly reports it as unavailable rather
        # than reading the newly written current object.
        ("replace", "native_lerobot_direct_object_unavailable"),
    ],
)
def test_real_minio_native_lerobot_direct_source_fails_closed_after_raw_mutation(
    client,
    db_session,
    admin_headers,
    monkeypatch,
    request,
    tmp_path: Path,
    mutation: str,
    expected_error: str,
):
    """A deleted or replaced frozen raw object cannot reach the catalog.

    This catches a validator that trusts the declaration or resolves a prefix
    again after upload, rather than downloading the exact persisted identity.
    """
    from data.config import settings
    from data.infra import oss_client
    from data.infra.object_storage import (
        ObjectStorageError,
        StorageObjectNotFound,
        StorageObjectRef,
    )
    from data.infra.storage_provider import get_storage_provider
    from data.models.native_lerobot_direct import NativeLerobotDirectSource
    from data.tasks.batch_workers import batch_execute_task

    monkeypatch.setattr(settings, "storage_browser_endpoint", "http://127.0.0.1:19000")
    monkeypatch.setattr(settings, "oss_browser_direct_enabled", True)
    monkeypatch.setattr(oss_client, "_is_public_oss_browser_endpoint", lambda _value: True)
    workspace = make_workspace(db_session)
    before = {
        "assets": db_session.query(DataAsset).count(),
        "facts": db_session.query(EpisodeAdmissionFact).count(),
        "annotation": db_session.query(AnnotationWorkItem).count(),
        "review": db_session.query(ReviewWorkItem).count(),
    }
    payload = b"frozen-direct-native-object"
    declared = client.post(
        "/api/v1/native-lerobot-direct-uploads",
        headers=admin_headers,
        json={
            "name": f"Native deleted {uuid4().hex[:8]}",
            "description": "identity deletion e2e",
            "source_workspace_id": workspace.id,
            "robot_type": "unknown",
            "dataset_id": f"native-deleted-{uuid4().hex[:8]}",
            "objects": [
                {
                    "path": "meta/info.json",
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
        },
    )
    assert declared.status_code == 200, declared.text
    source_id = declared.json()["data"]["id"]
    item = declared.json()["data"]["objects"][0]
    declared_raw_ref = StorageObjectRef(
        bucket_role="export",
        object_key=f"export/v1/native-lerobot-direct/{source_id}/objects/meta/info.json",
        version_id=None,
        etag="",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    declared_marker_ref = StorageObjectRef(
        bucket_role="export",
        object_key=f"export/v1/native-lerobot-direct/{source_id}/complete.json",
        version_id=None,
        etag="",
        size_bytes=0,
        sha256=None,
    )
    unrelated_refs: list[StorageObjectRef] = []

    def cleanup_source_row() -> None:
        from botocore.exceptions import BotoCoreError, ClientError
        from sqlalchemy.exc import SQLAlchemyError

        from data.database import JobQueueSlot

        provider = get_storage_provider()
        db_session.rollback()
        refs = [declared_raw_ref, declared_marker_ref, *unrelated_refs]
        source = db_session.get(NativeLerobotDirectSource, source_id)
        if source is not None:
            for source_object in source.objects:
                ref_json = source_object.provider_ref_json or {}
                if ref_json:
                    refs.append(StorageObjectRef(**ref_json))

        # The source ID is part of the server-owned key, so listing this exact
        # key cannot touch another source even if an UploadId was created by
        # MinIO before the database could persist it.
        errors: list[str] = []
        storage_client = getattr(provider, "client", None)
        bucket_config = getattr(provider, "bucket_config", None)
        export_bucket = (
            getattr(bucket_config, "buckets", {}).get("export")
            if bucket_config is not None
            else None
        )
        if storage_client is None or not export_bucket:
            errors.append("direct source multipart cleanup is unavailable")
        else:
            try:
                active_uploads = [
                    str(upload.get("UploadId") or "")
                    for upload in storage_client.list_multipart_uploads(
                        Bucket=export_bucket,
                        Prefix=declared_raw_ref.object_key,
                    ).get("Uploads", [])
                    if upload.get("Key") == declared_raw_ref.object_key
                ]
                for upload_id in active_uploads:
                    provider.abort_multipart(declared_raw_ref, upload_id)
                remaining_uploads = [
                    str(upload.get("UploadId") or "")
                    for upload in storage_client.list_multipart_uploads(
                        Bucket=export_bucket,
                        Prefix=declared_raw_ref.object_key,
                    ).get("Uploads", [])
                    if upload.get("Key") == declared_raw_ref.object_key
                ]
                if remaining_uploads:
                    errors.append("direct source multipart cleanup retained upload")
            except (BotoCoreError, ClientError, ObjectStorageError) as exc:
                errors.append(f"direct source multipart cleanup failed: {exc}")

        seen: set[tuple[str, str, str | None]] = set()
        for ref in refs:
            identity = (ref.bucket_role, ref.object_key, ref.version_id)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                persisted = provider.head(ref)
            except StorageObjectNotFound:
                continue
            except ObjectStorageError as exc:
                errors.append(f"direct source object lookup failed {ref.object_key}: {exc}")
                continue
            try:
                provider.delete_exact(persisted)
                try:
                    provider.head(persisted)
                except StorageObjectNotFound:
                    pass
                except ObjectStorageError as exc:
                    errors.append(
                        f"direct source object verification failed {ref.object_key}: {exc}"
                    )
                else:
                    errors.append(f"direct source object cleanup retained {ref.object_key}")
            except ObjectStorageError as exc:
                errors.append(f"direct source object cleanup failed {ref.object_key}: {exc}")
        if errors:
            raise AssertionError(
                "native LeRobot direct deletion cleanup failed: " + "; ".join(errors)
            )

        if source is None:
            return
        try:
            job_ids = [
                str(job_id)
                for job_id in db_session.scalars(
                    select(JobRun.id).where(
                        JobRun.kind == "native_lerobot_direct_validate",
                        JobRun.resource_id == source.id,
                    )
                )
            ]
            if job_ids:
                db_session.query(JobQueueSlot).filter(JobQueueSlot.job_id.in_(job_ids)).delete(
                    synchronize_session=False
                )
                db_session.query(JobRun).filter(JobRun.id.in_(job_ids)).delete(
                    synchronize_session=False
                )
            db_session.delete(source)
            db_session.commit()
        except SQLAlchemyError as exc:
            db_session.rollback()
            raise AssertionError(
                f"native LeRobot direct deletion database cleanup failed: {exc}"
            ) from exc

    request.addfinalizer(cleanup_source_row)
    initialized = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{source_id}/objects/{item['id']}/multipart/init",
        headers=admin_headers,
    )
    assert initialized.status_code == 200, initialized.text
    signed = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{source_id}/objects/{item['id']}/multipart/sign-part",
        headers=admin_headers,
        json={"part_number": 1},
    )
    assert signed.status_code == 200, signed.text
    with urllib.request.urlopen(
        urllib.request.Request(signed.json()["data"]["url"], data=payload, method="PUT"),
        timeout=30,
    ) as response:
        etag = response.headers.get("ETag")
        assert response.status == 200 and etag
    completed = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{source_id}/objects/{item['id']}/multipart/complete",
        headers=admin_headers,
        json={"parts": [{"part_number": 1, "etag": etag.strip('"')}]},
    )
    assert completed.status_code == 200, completed.text
    db_session.expire_all()
    source = db_session.get(NativeLerobotDirectSource, source_id)
    assert source is not None
    raw_ref = StorageObjectRef(**source.objects[0].provider_ref_json)
    provider = get_storage_provider()
    unrelated_payload = b"task7c unrelated raw object"
    unrelated_path = tmp_path / "unrelated-raw.bin"
    unrelated_path.write_bytes(unrelated_payload)
    unrelated_ref = provider.put_worker_object(
        StorageObjectRef(
            "raw",
            f"raw/v1/task7c-unrelated/{uuid4().hex}/preserved.bin",
            None,
            "",
            len(unrelated_payload),
            hashlib.sha256(unrelated_payload).hexdigest(),
        ),
        str(unrelated_path),
    )
    unrelated_refs.append(unrelated_ref)
    assert provider.head(unrelated_ref).object_key == unrelated_ref.object_key
    if mutation == "delete":
        provider.delete_exact(raw_ref)
        with pytest.raises(ObjectStorageError):
            provider.head(raw_ref)
    else:
        # The test removes the frozen identity before putting different bytes
        # at the same key. With versioning enabled, merely making a new version
        # is harmless because the frozen version remains readable; replacing
        # the frozen input itself must fail closed.
        provider.delete_exact(raw_ref)
        replacement = b"replacement-direct-native-object"
        storage_client = getattr(provider, "client", None)
        export_bucket = getattr(getattr(provider, "bucket_config", None), "buckets", {}).get(
            "export"
        )
        assert storage_client is not None and export_bucket
        storage_client.put_object(
            Bucket=export_bucket,
            Key=raw_ref.object_key,
            Body=replacement,
            ContentLength=len(replacement),
            Metadata={"sha256": hashlib.sha256(replacement).hexdigest()},
        )
        replaced = provider.head(
            StorageObjectRef("export", raw_ref.object_key, None, "", len(replacement), None)
        )
        assert replaced.sha256 == hashlib.sha256(replacement).hexdigest()

    queued = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{source_id}/complete",
        headers=admin_headers,
    )
    assert queued.status_code == 200, queued.text
    job = db_session.get(JobRun, queued.json()["data"]["validation_job_id"])
    assert job is not None
    outcome = batch_execute_task.run(job.id, job.recovery_dispatch_token)
    assert outcome == {
        "job_id": job.id,
        "status": "failed",
        "error_code": expected_error,
    }
    db_session.expire_all()
    source = db_session.get(NativeLerobotDirectSource, source_id)
    assert source is not None
    assert source.status == "failed"
    assert source.error_code == expected_error
    assert source.catalog_dataset_id is None
    assert source.catalog_dataset_version_id is None
    assert provider.head(unrelated_ref).object_key == unrelated_ref.object_key
    assert {
        "assets": db_session.query(DataAsset).count(),
        "facts": db_session.query(EpisodeAdmissionFact).count(),
        "annotation": db_session.query(AnnotationWorkItem).count(),
        "review": db_session.query(ReviewWorkItem).count(),
    } == before
    retry = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{source_id}/complete",
        headers=admin_headers,
    )
    assert retry.status_code == 422
    assert "new upload attempt" in retry.json()["detail"]
    system_retry = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{source_id}/retry",
        headers=admin_headers,
    )
    assert system_retry.status_code == 422
    assert "new upload attempt" in system_retry.json()["detail"]
