"""Client-precheck admission: server re-judgement, parse state, worker mode and fallback."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from tests.collection_api_fixtures import make_assigned_package, make_project, make_workspace
from tests.test_collection_admission_worker import claimed_job
from tests.test_qrdf_admission import _episode

from data.config import settings
from data.database import Episode
from data.infra.object_storage import (
    ObjectStorageError,
    StorageObjectNotFound,
    StorageObjectRef,
)
from data.integrations.qrdf.admission import run_qrdf_admission_worker
from data.models.collection_upload import (
    CollectionUploadSession,
    CollectionUploadSessionPackage,
)
from data.services.client_admission import (
    ClientReportInvalid,
    client_admission_state,
    judge_client_report,
    preview_file_id,
)
from data.services.collection_upload_parse import parse_collection_upload_session
from data.services.episode_admission import current_episode_admission_fact

NON_BLOCKING_ERROR = {"severity": "ERROR", "code": "NO_ACTION_TOPIC", "message": "no action"}


class ObjectStore:
    """In-memory storage provider keyed by object key; records every download."""

    def __init__(self, db):
        self.db = db
        self.objects: dict[str, bytes] = {}
        self.identity: dict[str, tuple[str | None, str]] = {}
        self.downloads: list[tuple[str, str]] = []

    def put(self, key, data, *, version_id="v1", etag="e1"):
        self.objects[key] = data
        self.identity[key] = (version_id, etag)

    def head(self, ref):
        if ref.object_key not in self.objects:
            raise StorageObjectNotFound(ref.object_key)
        version_id, etag = self.identity[ref.object_key]
        size = len(self.objects[ref.object_key])
        return StorageObjectRef(ref.bucket_role, ref.object_key, version_id, etag, size, None)

    def download_file(self, ref, destination):
        assert not self.db.in_transaction(), "download must not hold a DB transaction"
        self.downloads.append((ref.bucket_role, ref.object_key))
        head = self.head(ref)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(self.objects[ref.object_key])
        return head

    def put_worker_object(self, ref, source_path):
        assert not self.db.in_transaction(), "upload must not hold a DB transaction"
        data = Path(source_path).read_bytes()
        self.put(ref.object_key, data, version_id="pv1", etag="pe1")
        digest = hashlib.sha256(data).hexdigest()
        return StorageObjectRef("process", ref.object_key, "pv1", "pe1", len(data), digest)

    def delete_exact(self, ref):
        self.objects.pop(ref.object_key, None)

    def sign_get(self, ref, *, expires=900):
        assert ref.object_key in self.objects
        return f"https://storage.test/{ref.object_key}"


def generate_previews(root: Path) -> list[str]:
    """Run the SDK preview generation the client runs and list the published files."""
    from qrdf.reader.episode import Episode as QRDFEpisode

    episode = QRDFEpisode(root)
    episode.generate_rgb_previews(camera_topics=None, max_edge=1280, keyframe_interval_s=1.0)
    manifest = episode.load_rgb_preview_manifest()
    paths = ["media/preview/manifest.json"]
    for stream in manifest.streams:
        paths.append(f"media/preview/{stream.video_path}")
        paths.append(f"media/preview/{stream.timeline_path}")
    return sorted(paths)


def setup_client_upload(
    db,
    tmp_path,
    monkeypatch,
    *,
    qrdf_version="0.2.1",
    issues=None,
    restyle_metadata=False,
    drop_preview=False,
):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    root = _episode(tmp_path)
    preview_paths = generate_previews(root)
    data = (root / "data.mcap").read_bytes()
    metadata_text = (root / "metadata.json").read_text(encoding="utf-8")
    if restyle_metadata:
        # Same content, different bytes: the client preview fingerprint is stale.
        metadata_text = json.dumps(json.loads(metadata_text), indent=2)
    metadata = json.loads(metadata_text)
    workspace = make_workspace(db)
    project = make_project(db, workspace)
    package = make_assigned_package(db, workspace, project)
    store = ObjectStore(db)
    digest = hashlib.sha256(data).hexdigest()
    raw_key = f"raw/v2/test/{uuid4().hex}/source.mcap"
    store.put(raw_key, data)
    source_key = uuid4().hex
    source_id = hashlib.sha256(f"{package.package_uid}:{source_key}".encode()).hexdigest()
    files = []
    uploads = {}
    for index, relative in enumerate(preview_paths):
        payload = (root / relative).read_bytes()
        file_id = preview_file_id(source_id, relative)
        key = (
            f"process/v2/workspaces/{workspace.id}/collection-uploads/test/"
            f"{uuid4().hex}/{Path(relative).name}"
        )
        store.put(key, payload, version_id=None, etag=f"preview-etag-{index}")
        files.append(
            {
                "path": relative,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "file_id": file_id,
                "object_key": key,
            }
        )
        uploads[file_id] = {
            "completion_state": "completed",
            "file_id": file_id,
            "object_key": key,
            "provider_identity": {
                "bucket_role": "process",
                "object_key": key,
                "version_id": None,
                "etag": f"preview-etag-{index}",
                "size_bytes": len(payload),
                "sha256": None,
            },
        }
    if drop_preview:
        store.objects.pop(files[-1]["object_key"])
    source = {
        "source_key": source_key,
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
        "raw_object_key": raw_key,
        "client_admission": {
            "qrdf_version": qrdf_version,
            "policy_version": "v1",
            "report": {
                "ok": True,
                "issues": [NON_BLOCKING_ERROR] if issues is None else issues,
                "media_validation": {"ok": True, "issues": []},
            },
            "files": files,
        },
    }
    identity = asdict(StorageObjectRef("raw", raw_key, "v1", "e1", len(data), None))
    upload = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=workspace.id,
        collection_project_id=project.id,
        status="uploaded",
        upload_mode="oss_multipart",
        result_json={
            "declarations": {package.package_uid: [source]},
            "oss_multipart_sources": {
                source_id: {
                    "completion_state": "completed",
                    "source_id": source_id,
                    "package_uid": package.package_uid,
                    "data_path": "data.mcap",
                    "object_key": raw_key,
                    "provider_identity": identity,
                }
            },
            "oss_multipart_files": uploads,
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
    monkeypatch.setattr("data.infra.storage_provider.get_storage_provider", lambda: store)
    return upload, package, store


def test_server_rejudges_issue_codes_and_ignores_client_ok():
    status, issues = judge_client_report(
        {"ok": True, "issues": [{"severity": "ERROR", "code": "MCAP_UNREADABLE"}]}
    )
    assert status == "failed"
    assert issues == [
        {"severity": "ERROR", "code": "MCAP_UNREADABLE", "message": "", "path": None, "topic": None}
    ]
    tolerated = {"ok": False, "issues": [NON_BLOCKING_ERROR, {"severity": "WARNING", "code": "X"}]}
    assert judge_client_report(tolerated)[0] == "passed"
    unknown = {"ok": True, "issues": [{"severity": "ERROR", "code": "A_NEW_SDK_CODE"}]}
    assert judge_client_report(unknown)[0] == "failed"


@pytest.mark.parametrize(
    "report",
    [
        None,
        {"ok": True},
        {"issues": "none"},
        {"issues": [{"severity": "ERROR"}]},
        {"issues": [{"severity": "FATAL", "code": "X"}]},
        {"issues": [{"severity": "ERROR", "code": ""}]},
        {"issues": ["MCAP_UNREADABLE"]},
    ],
)
def test_malformed_client_report_is_invalid(report):
    with pytest.raises(ClientReportInvalid):
        judge_client_report(report)


def test_state_carries_declared_files_with_completed_identities():
    identity = {
        "bucket_role": "process",
        "object_key": "k1",
        "version_id": None,
        "etag": "e",
        "size_bytes": 3,
        "sha256": None,
    }
    source = {
        "client_admission": {
            "qrdf_version": "0.2.1",
            "policy_version": "v1",
            "report": {"ok": True, "issues": [], "media_validation": None},
            "files": [
                {
                    "path": "media/preview/manifest.json",
                    "size_bytes": 3,
                    "sha256": "a" * 64,
                    "file_id": "f1",
                    "object_key": "k1",
                },
                {
                    "path": "media/preview/g/x_rgb.mp4",
                    "size_bytes": 4,
                    "sha256": "b" * 64,
                    "file_id": "f2",
                    "object_key": "k2",
                },
            ],
        }
    }
    result = {
        "oss_multipart_files": {
            "f1": {"completion_state": "completed", "provider_identity": identity},
            "f2": {"completion_state": "completing"},
        }
    }

    state = client_admission_state(result, source=source)

    files = state["client_admission"]["files"]
    assert files[0] == {
        "path": "media/preview/manifest.json",
        "size_bytes": 3,
        "sha256": "a" * 64,
        "object": identity,
    }
    assert files[1]["object"] is None
    assert state["client_admission"]["qrdf_version"] == "0.2.1"
    assert client_admission_state(result, source={}) == {}


def test_parse_records_client_admission_on_the_source_state(db_session, tmp_path, monkeypatch):
    upload, _package, _store = setup_client_upload(db_session, tmp_path, monkeypatch)

    parse_collection_upload_session(db_session, upload.id)

    db_session.expire_all()
    stored = db_session.get(CollectionUploadSession, upload.id)
    [state] = stored.result_json["admission_sources"].values()
    declared = state["client_admission"]
    assert declared["qrdf_version"] == "0.2.1"
    assert declared["files"]
    assert all(item["object"]["bucket_role"] == "process" for item in declared["files"])


def _run(db, upload, package):
    parse_collection_upload_session(db, upload.id)
    run_qrdf_admission_worker(db, claimed_job(db, upload))
    db.expire_all()
    episode = db.query(Episode).filter_by(data_package_id=package.id).one()
    fact = current_episode_admission_fact(db, episode_id=episode.id)
    [state] = db.get(CollectionUploadSession, upload.id).result_json["admission_sources"].values()
    return fact, state


def _stored_report(fact, store):
    entry = next(item for item in fact.objects_json if item["kind"] == "admission_report")
    return json.loads(store.objects[entry["ref"]["object_key"]])


def test_client_mode_admits_without_downloading_the_mcap(db_session, tmp_path, monkeypatch):
    upload, package, store = setup_client_upload(db_session, tmp_path, monkeypatch)

    fact, state = _run(db_session, upload, package)

    assert fact.integrity_source == "client"
    assert (fact.integrity_status, fact.preview_status, fact.output_verification_status) == (
        "passed",
        "ready",
        "verified",
    )
    assert store.downloads, "preview files are downloaded and verified"
    assert all(role == "process" for role, _key in store.downloads)
    kinds = {entry["kind"] for entry in fact.objects_json}
    assert {
        "data",
        "metadata",
        "admission_report",
        "preview_manifest",
        "preview_video",
        "preview_timeline",
    } <= kinds
    declared = state["client_admission"]["files"]
    preview_keys = {
        entry["ref"]["object_key"]
        for entry in fact.objects_json
        if entry["kind"].startswith("preview_")
    }
    assert preview_keys == {item["object"]["object_key"] for item in declared}
    data_entry = next(entry for entry in fact.objects_json if entry["kind"] == "data")
    assert data_entry["ref"]["sha256"] == state["declared_sha256"]
    report = _stored_report(fact, store)
    assert report == fact.report_ref_json
    assert report["integrity_source"] == "client"
    assert report["client_admission"]["judgement"]["integrity_status"] == "passed"
    assert "client_admission_fallback" not in report
    assert state["integrity_source"] == "client"
    assert "client_admission_fallback" not in state


def test_client_ok_with_blocking_code_fails_without_fallback(db_session, tmp_path, monkeypatch):
    upload, package, store = setup_client_upload(
        db_session,
        tmp_path,
        monkeypatch,
        issues=[{"severity": "ERROR", "code": "MCAP_UNREADABLE", "message": "truncated"}],
    )

    fact, state = _run(db_session, upload, package)

    assert fact.integrity_source == "client"
    assert fact.integrity_status == "failed"
    assert fact.output_verification_status == "failed"
    assert fact.error_code == "MCAP_UNREADABLE"
    assert store.downloads == []
    assert "client_admission_fallback" not in fact.report_ref_json
    assert fact.report_ref_json["client_admission"]["report"]["ok"] is True
    assert state["status"] == "failed"
    assert state["integrity_source"] == "client"


@pytest.mark.parametrize(
    "options, disable, reason",
    [
        ({"qrdf_version": "0.1.9"}, False, "client_qrdf_version_not_accepted"),
        ({}, True, "client_admission_disabled"),
        ({"restyle_metadata": True}, False, "client_preview_invalid"),
        ({"drop_preview": True}, False, "client_preview_object_missing"),
    ],
)
def test_unusable_client_result_falls_back_to_server_mode(
    db_session, tmp_path, monkeypatch, options, disable, reason
):
    upload, package, store = setup_client_upload(db_session, tmp_path, monkeypatch, **options)
    if disable:
        monkeypatch.setattr(settings, "accept_client_admission", False)

    fact, state = _run(db_session, upload, package)

    assert fact.integrity_source == "server"
    assert fact.output_verification_status == "verified"
    assert any(role == "raw" for role, _key in store.downloads), "server mode downloads the MCAP"
    assert fact.report_ref_json["client_admission_fallback"] == reason
    assert _stored_report(fact, store)["client_admission_fallback"] == reason
    assert state["integrity_source"] == "server"
    assert state["client_admission_fallback"] == reason
    if reason == "client_preview_invalid":
        detail = fact.report_ref_json["client_admission_fallback_detail"]
        assert detail["error_code"] == "PREVIEW_MEDIA_STALE"


def test_client_mode_partial_process_upload_is_cleaned_like_server_mode(
    db_session, tmp_path, monkeypatch
):
    upload, package, store = setup_client_upload(db_session, tmp_path, monkeypatch)
    client_keys = set(store.objects)
    original = store.put_worker_object
    published = []

    def broken(ref, path):
        if published:
            raise RuntimeError("private endpoint must not be a persisted error code")
        persisted = original(ref, path)
        published.append(persisted.object_key)
        return persisted

    store.put_worker_object = broken

    fact, state = _run(db_session, upload, package)

    assert fact.error_code == "PROCESS_UPLOAD_FAILED"
    assert fact.output_verification_status == "failed"
    assert fact.objects_json == []
    assert published and not any(key in store.objects for key in published)
    # Client-uploaded preview objects and the raw MCAP are not worker objects.
    assert set(store.objects) == client_keys
    assert not any(role == "raw" for role, _key in store.downloads)
    assert state["status"] == "failed"


def _raw_key(store):
    return next(key for key in store.objects if key.startswith("raw/"))


def _preview_key(store, suffix):
    return next(key for key in store.objects if key.startswith("process/") and key.endswith(suffix))


def _assert_fallback(fact, state, reason, *, verified=True):
    assert fact.integrity_source == "server"
    assert fact.report_ref_json["client_admission_fallback"] == reason
    assert state["integrity_source"] == "server"
    assert state["client_admission_fallback"] == reason
    assert (fact.output_verification_status == "verified") is verified


@pytest.mark.parametrize(
    "target, reason",
    [("raw", "client_source_object_mismatch"), ("preview", "client_preview_object_missing")],
)
def test_unreadable_head_falls_back_to_server_mode(
    db_session, tmp_path, monkeypatch, target, reason
):
    upload, package, store = setup_client_upload(db_session, tmp_path, monkeypatch)
    key = _raw_key(store) if target == "raw" else _preview_key(store, "manifest.json")
    original = store.head
    failed = []

    def flaky_head(ref):
        # Only the first HEAD fails: server mode must still be able to read it.
        if ref.object_key == key and not failed:
            failed.append(key)
            raise ObjectStorageError("OSS head failed: connection reset")
        return original(ref)

    store.head = flaky_head

    fact, state = _run(db_session, upload, package)

    assert failed
    _assert_fallback(fact, state, reason)
    assert fact.report_ref_json["client_admission_fallback_detail"] == {
        "error_type": "ObjectStorageError"
    }
    assert any(role == "raw" for role, _key in store.downloads)


def test_provider_digest_disagreeing_with_declaration_falls_back(db_session, tmp_path, monkeypatch):
    upload, package, store = setup_client_upload(db_session, tmp_path, monkeypatch)
    raw_key = _raw_key(store)
    original = store.head

    def head_with_digest(ref):
        head = original(ref)
        if ref.object_key == raw_key:
            return StorageObjectRef(
                head.bucket_role,
                head.object_key,
                head.version_id,
                head.etag,
                head.size_bytes,
                "0" * 64,
            )
        return head

    store.head = head_with_digest

    fact, state = _run(db_session, upload, package)

    _assert_fallback(fact, state, "client_source_object_mismatch")
    data_entry = next(entry for entry in fact.objects_json if entry["kind"] == "data")
    # Server mode measured the real bytes; the declared digest was not trusted blindly.
    assert data_entry["ref"]["sha256"] == state["declared_sha256"]
    assert any(role == "raw" for role, _key in store.downloads)


@pytest.mark.parametrize("change", ["etag", "size"])
def test_raw_object_changed_after_upload_falls_back(db_session, tmp_path, monkeypatch, change):
    upload, package, store = setup_client_upload(db_session, tmp_path, monkeypatch)
    raw_key = _raw_key(store)
    if change == "etag":
        store.identity[raw_key] = ("v1", "replaced-etag")
    else:
        store.objects[raw_key] = store.objects[raw_key] + b"x"

    fact, state = _run(db_session, upload, package)

    # Server mode then pins the same identity and fails closed on the change.
    _assert_fallback(fact, state, "client_source_object_mismatch", verified=False)
    assert fact.integrity_status == "failed"
    assert fact.error_code == "SOURCE_IDENTITY_CHANGED"
    assert not any(role == "process" for role, _key in store.downloads)


def test_preview_bytes_not_matching_declared_digest_fall_back(db_session, tmp_path, monkeypatch):
    upload, package, store = setup_client_upload(db_session, tmp_path, monkeypatch)
    key = _preview_key(store, ".timeline.json")
    payload = store.objects[key]
    # Same size, so HEAD agrees; only the downloaded bytes betray the change.
    store.objects[key] = payload[:-1] + (b" " if payload[-1:] != b" " else b"\n")

    fact, state = _run(db_session, upload, package)

    _assert_fallback(fact, state, "client_preview_hash_mismatch")
    assert any(role == "process" and k == key for role, k in store.downloads)
    assert any(role == "raw" for role, _key in store.downloads)
