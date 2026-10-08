"""Production upload scheduling and lease-fenced real QRDF admission."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from tests.collection_api_fixtures import (
    make_assigned_package,
    make_project,
    make_workspace,
)
from tests.test_qrdf_admission import _episode

from data.config import settings
from data.database import Episode, JobRun
from data.infra.object_storage import StorageObjectRef
from data.integrations.qrdf.admission import run_qrdf_admission_worker
from data.models.collection_upload import (
    CollectionUploadSession,
    CollectionUploadSessionPackage,
)
from data.services.collection_upload_parse import parse_collection_upload_session
from data.services.episode_admission import current_episode_admission_fact
from data.services.job_runs import LeaseOwnershipLost


class Provider:
    def __init__(self, db, payload):
        self.db, self.payload, self.uploads, self.paths = db, payload, {}, []
        self.on_download = None

    def download_file(self, ref, destination):
        assert not self.db.in_transaction(), "download must not hold a DB transaction"
        self.paths.append(Path(destination))
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(self.payload)
        if self.on_download:
            self.on_download()
        return StorageObjectRef(
            ref.bucket_role,
            ref.object_key,
            ref.version_id,
            ref.etag,
            len(self.payload),
            hashlib.sha256(self.payload).hexdigest(),
        )

    def put_worker_object(self, ref, source_path):
        assert not self.db.in_transaction(), "upload must not hold a DB transaction"
        self.uploads[ref.object_key] = Path(source_path).read_bytes()
        return StorageObjectRef(
            "process",
            ref.object_key,
            "pv1",
            "pe1",
            len(self.uploads[ref.object_key]),
            hashlib.sha256(self.uploads[ref.object_key]).hexdigest(),
        )

    def delete_exact(self, ref):
        self.uploads.pop(ref.object_key, None)

    def sign_get(self, ref, *, expires=900):
        assert ref.object_key in self.uploads
        assert ref.bucket_role == "process"
        return f"https://storage.test/{ref.object_key}?version={ref.version_id}"


def setup_upload(db, tmp_path, monkeypatch, *, bad_metadata=False, false_hash=False):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    root = _episode(tmp_path)
    data = (root / "data.mcap").read_bytes()
    metadata_text = (root / "metadata.json").read_text()
    metadata = json.loads(metadata_text)
    ws = make_workspace(db)
    project = make_project(db, ws)
    package = make_assigned_package(db, ws, project)
    digest = "0" * 64 if false_hash else hashlib.sha256(data).hexdigest()
    source = {
        "source_key": uuid4().hex,
        "source": {
            "episode_id": metadata["episode_id"],
            "start_ns": str(metadata["timing"]["start_timestamp_ns"]),
            "end_ns": str(metadata["timing"]["end_timestamp_ns"]),
            "metadata_sha256": hashlib.sha256(metadata_text.encode()).hexdigest(),
            "data_mcap_sha256": digest,
        },
        "metadata_text": metadata_text,
        "data_file": {"path": "data.mcap", "size_bytes": len(data), "sha256": digest},
        "staging_path": f"sources/{package.package_uid}/data.mcap",
        "raw_object_key": "raw/v2/test/source.mcap",
    }
    identity = asdict(
        StorageObjectRef("raw", source["raw_object_key"], "v1", "e1", len(data), None)
    )
    source_id = hashlib.sha256(f"{package.package_uid}:{source['source_key']}".encode()).hexdigest()
    sources = [source]
    if bad_metadata:
        sources.append({**source, "source_key": "bad", "metadata_text": "{"})
    upload = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=ws.id,
        collection_project_id=project.id,
        status="uploaded",
        upload_mode="oss_multipart",
        result_json={
            "declarations": {package.package_uid: sources},
            "oss_multipart_sources": {
                source_id: {
                    "completion_state": "completed",
                    "source_id": source_id,
                    "package_uid": package.package_uid,
                    "data_path": "data.mcap",
                    "object_key": identity["object_key"],
                    "provider_identity": identity,
                }
            },
        },
    )
    db.add(upload)
    db.flush()
    db.add(
        CollectionUploadSessionPackage(
            upload_session_id=upload.id,
            data_package_id=package.id,
            package_uid=package.package_uid,
        )
    )
    package.status = "uploading"
    db.commit()
    provider = Provider(db, data)
    monkeypatch.setattr("data.infra.storage_provider.get_storage_provider", lambda: provider)
    return upload, package, provider


def claimed_job(db, upload):
    jobs = db.query(JobRun).filter_by(kind="collection_upload_admission").all()
    job = next(j for j in jobs if j.detail_json.get("upload_session_id") == upload.id)
    job.status = "running"
    job.lease_token = uuid4().hex
    job.lease_worker_id = "test-worker"
    job.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
    job.attempt_count = 1
    db.commit()
    return job


def test_real_mixed_upload_schedules_good_and_persists_bad_source(
    db_session, tmp_path, monkeypatch
):
    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch, bad_metadata=True)
    parse_collection_upload_session(db_session, upload.id)
    db_session.rollback()  # Must not undo the successful source or its job.
    assert db_session.query(Episode).filter_by(data_package_id=package.id).count() == 1
    job = claimed_job(db_session, upload)
    run_qrdf_admission_worker(db_session, job)
    db_session.expire_all()
    episode = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    fact = current_episode_admission_fact(db_session, episode_id=episode.id)
    assert fact.output_verification_status == "verified"
    process_keys = {o["ref"]["object_key"] for o in fact.objects_json if o["kind"] != "data"}
    assert process_keys and process_keys <= set(provider.uploads)
    assert fact.report_ref_json["qrdf_runtime"]["source_commit"]
    assert upload.result_json["ready_count"] == 1
    assert upload.result_json["failed_count"] == 1
    assert upload.result_json["failed"][0]["error_code"]
    assert all(not path.exists() for path in provider.paths)
    from data.services.episode_admission import package_admission_counts

    counts = package_admission_counts(
        db_session, workspace_id=package.workspace_id, data_package_id=package.id
    )
    assert counts["ready"] == 1 and counts["failed"] == 1


def test_late_lease_cannot_publish_fact_or_process(db_session, tmp_path, monkeypatch):
    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    job = claimed_job(db_session, upload)

    def replace_lease():
        db_session.query(JobRun).filter_by(id=job.id).update(
            {"lease_token": "new-owner", "attempt_count": 2}
        )
        db_session.commit()

    provider.on_download = replace_lease
    with pytest.raises(LeaseOwnershipLost):
        run_qrdf_admission_worker(db_session, job)
    episode = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    assert current_episode_admission_fact(db_session, episode_id=episode.id) is None
    assert not provider.uploads
    assert all(not path.exists() for path in provider.paths)


@pytest.mark.parametrize("field", ["episode_path", "scratch_root"])
def test_worker_rejects_path_override(db_session, tmp_path, monkeypatch, field):
    upload, _, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    job = claimed_job(db_session, upload)
    job.detail_json = {**job.detail_json, field: str(tmp_path)}
    db_session.commit()
    with pytest.raises(ValueError, match="override"):
        run_qrdf_admission_worker(db_session, job)
    assert not provider.paths


def test_client_hash_never_becomes_verified_source_hash(db_session, tmp_path, monkeypatch):
    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch, false_hash=True)
    parse_collection_upload_session(db_session, upload.id)
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    episode = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    fact = current_episode_admission_fact(db_session, episode_id=episode.id)
    assert fact.integrity_status == "failed"
    assert fact.error_code == "SOURCE_DECLARED_HASH_MISMATCH"
    # A hash mismatch fails admission before any objects are recorded; the
    # failure itself proves the real computed digest never matched the
    # client-declared (false) one.
    assert fact.objects_json == []
    assert not provider.uploads


def test_provider_identity_replacement_fails_closed(db_session, tmp_path, monkeypatch):
    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    original = provider.download_file

    def replaced(ref, destination):
        downloaded = original(ref, destination)
        return StorageObjectRef(**{**asdict(downloaded), "version_id": "replaced"})

    provider.download_file = replaced
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    ep = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    assert (
        current_episode_admission_fact(db_session, episode_id=ep.id).error_code
        == "SOURCE_IDENTITY_CHANGED"
    )
    assert not provider.uploads


def test_late_upload_cannot_write_and_cleans_orphan(db_session, tmp_path, monkeypatch):
    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    job = claimed_job(db_session, upload)
    job_id = job.id
    original = provider.put_worker_object

    def late(ref, path):
        persisted = original(ref, path)
        db_session.query(JobRun).filter_by(id=job_id).update(
            {"lease_token": "new-owner", "attempt_count": 2}
        )
        db_session.commit()
        return persisted

    provider.put_worker_object = late
    with pytest.raises(LeaseOwnershipLost):
        run_qrdf_admission_worker(db_session, job)
    ep = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    assert current_episode_admission_fact(db_session, episode_id=ep.id) is None
    assert not provider.uploads
    assert all(not p.exists() for p in provider.paths)


def test_corrupt_source_does_not_rollback_good_episode(db_session, tmp_path, monkeypatch):
    from data.services.collection_upload_parse import (
        ensure_collection_upload_parse_job,
        parse_collection_upload_job,
    )

    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    payload = dict(upload.result_json)
    good = payload["declarations"][package.package_uid][0]
    corrupt_data = b"truncated MCAP"
    digest = hashlib.sha256(corrupt_data).hexdigest()
    bad = {
        **good,
        "source_key": "corrupt",
        "raw_object_key": "raw/v2/test/corrupt.mcap",
        "source": {**good["source"], "data_mcap_sha256": digest},
        "data_file": {
            **good["data_file"],
            "sha256": digest,
            "size_bytes": len(corrupt_data),
        },
    }
    source_id = hashlib.sha256(f"{package.package_uid}:corrupt".encode()).hexdigest()
    payload["declarations"] = {package.package_uid: [good, bad]}
    payload["oss_multipart_sources"] = {
        **payload["oss_multipart_sources"],
        source_id: {
            "completion_state": "completed",
            "provider_identity": asdict(
                StorageObjectRef("raw", bad["raw_object_key"], "v1", "e1", len(corrupt_data), None)
            ),
        },
    }
    upload.result_json = payload
    parse_job = ensure_collection_upload_parse_job(db_session, upload)
    db_session.commit()
    result = parse_collection_upload_job(db_session, parse_job)
    assert len(result["_follow_up_job_ids"]) == 2
    original = provider.download_file
    good_data = provider.payload

    def download(ref, destination):
        provider.payload = corrupt_data if ref.object_key == bad["raw_object_key"] else good_data
        return original(ref, destination)

    provider.download_file = download
    for job_id in result["_follow_up_job_ids"]:
        job = db_session.get(JobRun, job_id)
        job.status, job.lease_token, job.lease_worker_id = (
            "running",
            uuid4().hex,
            "test",
        )
        job.lease_expires_at, job.attempt_count = (
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10),
            1,
        )
        db_session.commit()
        run_qrdf_admission_worker(db_session, job)
        db_session.rollback()
    db_session.refresh(upload)
    assert upload.result_json["ready_count"] == upload.result_json["failed_count"] == 1
    assert upload.result_json["failed"][0]["error_code"] == "MCAP_UNREADABLE"
    assert provider.uploads


def test_scratch_parent_symlink_never_downloads(db_session, tmp_path, monkeypatch):
    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    root = Path(settings.storage_root)
    root.mkdir(parents=True)
    (root / "admission-scratch").symlink_to(tmp_path, target_is_directory=True)
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    ep = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    assert (
        current_episode_admission_fact(db_session, episode_id=ep.id).error_code
        == "SOURCE_SCRATCH_UNSAFE"
    )
    assert not provider.paths


def test_signed_preview_reads_process_after_scratch_cleanup(
    client, db_session, admin_headers, tmp_path, monkeypatch
):
    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    ep = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    assert all(not p.exists() for p in provider.paths)
    response = client.get(
        f"/api/v1/data-packages/{package.id}/episodes/{ep.id}/preview-urls",
        params={"workspace_id": package.workspace_id},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    streams = response.json()["data"]["streams"]
    assert streams and streams[0]["video_url"].startswith("https://storage.test/process/")
    episode_preview = client.get(
        f"/api/v1/episodes/{ep.id}/preview-url",
        headers=admin_headers,
    )
    assert episode_preview.status_code == 200, episode_preview.text
    assert episode_preview.json()["data"]["available"] is True
    assert episode_preview.json()["data"]["url"].startswith("https://storage.test/process/")
    fact = current_episode_admission_fact(db_session, episode_id=ep.id)
    from data.services.episode_objects import fact_objects, preview_streams

    # The synthetic fixture episode carries two RGB cameras (front + wrist),
    # so `preview_streams` returns more than one entry; index the first
    # rather than destructuring a single value as in the brief's snippet.
    streams = preview_streams(fact_objects(fact))
    assert streams
    stream = streams[0]
    assert b"ftyp" in provider.uploads[stream["video"].ref["object_key"]][:32]
    timeline = json.loads(provider.uploads[stream["timeline"].ref["object_key"]])
    assert timeline["entries"][0]["timestamp_ns"].isdecimal()
    wrong_package = client.get(
        f"/api/v1/data-packages/{package.id + 10000}/episodes/{ep.id}/preview-urls",
        params={"workspace_id": package.workspace_id},
        headers=admin_headers,
    )
    assert wrong_package.status_code == 404
    still_served = client.get(
        f"/api/v1/data-packages/{package.id}/episodes/{ep.id}/preview-urls",
        params={"workspace_id": package.workspace_id},
        headers=admin_headers,
    )
    # The endpoint reads the object manifest (fact.objects_json) directly;
    # every preview topic in the manifest must come back, not just the
    # first one checked above.
    assert still_served.status_code == 200
    still_streams = still_served.json()["data"]["streams"]
    assert still_streams
    assert {item["topic"] for item in still_streams} == {item["topic"] for item in streams}
    ep.source_fingerprint = "changed-after-admission"
    db_session.commit()
    stale = client.get(
        f"/api/v1/data-packages/{package.id}/episodes/{ep.id}/preview-urls",
        params={"workspace_id": package.workspace_id},
        headers=admin_headers,
    )
    assert stale.status_code == 409


def test_newer_admission_attempt_cannot_be_replaced(db_session, tmp_path, monkeypatch):
    from data.services.episode_admission import record_episode_admission_fact

    upload, _package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    job = claimed_job(db_session, upload)
    episode_id = int(job.resource_id)
    fingerprint = db_session.get(Episode, episode_id).source_fingerprint
    db_session.commit()

    def supersede():
        record_episode_admission_fact(
            db_session,
            episode_id=episode_id,
            attempt=5,
            source_fingerprint=fingerprint,
            validation_policy_version="v1",
            integrity_status="failed",
            preview_status="failed",
            output_verification_status="failed",
            report_ref={"source": "new-attempt"},
            error_code="NEW_ATTEMPT_FAILED",
        )
        db_session.commit()

    provider.on_download = supersede
    with pytest.raises(LeaseOwnershipLost):
        run_qrdf_admission_worker(db_session, job)
    fact = current_episode_admission_fact(db_session, episode_id=episode_id)
    assert fact.attempt == 5 and fact.error_code == "NEW_ATTEMPT_FAILED"
    assert not provider.uploads


def test_worker_records_object_manifest_that_rebuilds_the_episode(
    db_session, tmp_path, monkeypatch
):
    from data.services.episode_objects import (
        data_object,
        fact_objects,
        object_of_kind,
        preview_streams,
        require_verified_objects,
    )

    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    ep = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    fact = current_episode_admission_fact(db_session, episode_id=ep.id)

    objects = fact_objects(fact)
    require_verified_objects(objects)
    assert data_object(objects).role == "raw"
    assert data_object(objects).path == "data.mcap"
    metadata = object_of_kind(objects, "metadata")
    assert json.loads(provider.uploads[metadata.ref["object_key"]])["episode_id"]
    report = object_of_kind(objects, "admission_report")
    assert json.loads(provider.uploads[report.ref["object_key"]])["source_fingerprint"]
    # The synthetic fixture episode carries two RGB cameras (front + wrist),
    # so this asserts every stream is present and playable rather than
    # unpacking a single one.
    streams = preview_streams(objects)
    assert streams
    for stream in streams:
        assert b"ftyp" in provider.uploads[stream["video"].ref["object_key"]][:32]
    for item in objects:
        if item.role == "process":
            payload = provider.uploads[item.ref["object_key"]]
            assert item.ref["sha256"] == hashlib.sha256(payload).hexdigest()
            assert item.ref["size_bytes"] == len(payload)


def test_partial_process_upload_failure_has_stable_code_and_cleans(
    db_session, tmp_path, monkeypatch
):
    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    original = provider.put_worker_object

    def broken(ref, path):
        if provider.uploads:
            raise RuntimeError("private endpoint must not be a persisted error code")
        return original(ref, path)

    provider.put_worker_object = broken
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    ep = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    fact = current_episode_admission_fact(db_session, episode_id=ep.id)
    assert fact.error_code == "PROCESS_UPLOAD_FAILED"
    assert not provider.uploads
    assert all(not p.exists() for p in provider.paths)


def test_worker_does_not_upload_a_process_archive(db_session, tmp_path, monkeypatch):
    upload, _package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    assert provider.uploads
    assert not any(key.endswith(".tar") for key in provider.uploads)
    assert not any(payload[257:262] == b"ustar" for payload in provider.uploads.values())


def test_server_mode_marks_integrity_source_server(db_session, tmp_path, monkeypatch):
    upload, package, _provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)

    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))

    db_session.expire_all()
    episode = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    fact = current_episode_admission_fact(db_session, episode_id=episode.id)
    assert fact.integrity_source == "server"
    assert fact.report_ref_json["integrity_source"] == "server"
    assert "client_admission_fallback" not in fact.report_ref_json
    session = db_session.get(CollectionUploadSession, upload.id)
    [state] = session.result_json["admission_sources"].values()
    assert state["integrity_source"] == "server"
    assert "client_admission_fallback" not in state
