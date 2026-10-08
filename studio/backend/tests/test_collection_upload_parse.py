"""Collection upload parsing: persist Episode and advance to pending_intake_review."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from tests.collection_api_fixtures import (
    make_assigned_package,
    make_project,
    make_workspace,
)

from data.config import settings
from data.database import Episode, JobRun
from data.models.collection_upload import (
    CollectionUploadSession,
    CollectionUploadSessionPackage,
)
from data.services.collection_upload_intake import collection_upload_staging_dir
from data.services.collection_upload_parse import (
    CollectionUploadParseError,
    _prepare_uploaded_sources,
    ensure_collection_upload_parse_job,
    parse_collection_upload_session,
)
from data.services.duance_imports import build_duance_import_manifest


def _fixture_session(db_session, workspace, project, package, **declaration_overrides):
    declaration = {
        "package_uid": package.package_uid,
        "episode_id": f"ep-{uuid4().hex[:12]}",
        "start_ns": 0,
        "end_ns": 3_600_000_000_000,
        "capture_mode": "ego",
        "capture_app_version": "test-1",
        "privacy_sensitive": False,
        "modality": "rgb",
        "source_fingerprint": uuid4().hex,
    }
    declaration.update(declaration_overrides)
    upload_session = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=workspace.id,
        collection_project_id=project.id,
        status="uploaded",
        upload_mode="duance_sdk",
        result_json={
            "parse_fixture_mode": True,
            "declarations": [declaration],
        },
    )
    db_session.add(upload_session)
    db_session.flush()
    db_session.add(
        CollectionUploadSessionPackage(
            upload_session_id=upload_session.id,
            data_package_id=package.id,
            package_uid=package.package_uid,
        )
    )
    package.status = "uploading"
    db_session.commit()
    return upload_session


def test_parse_creates_package_episode_and_moves_to_pending_review(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    upload_session = _fixture_session(db_session, workspace, project, package)

    parse_collection_upload_session(db_session, session_id=upload_session.id)

    db_session.refresh(package)
    db_session.refresh(upload_session)
    episode = db_session.query(Episode).filter(Episode.data_package_id == package.id).one()
    assert episode.task_set_id is None
    assert episode.batch_id is None
    assert episode.validity_status == "valid"
    assert episode.metadata_json["timing"]["duration_s"] == 3600.0
    assert package.status == "pending_intake_review"
    assert package.upload_completed_at is not None
    assert package.capture_mode == "ego"
    assert package.captured_duration_hours == Decimal("1.00")
    assert package.qrdf_facts_json == {
        "capture": {"mode": "ego", "app_version": "test-1"},
        "integrity": {"status": "passed", "algorithm": "sha256"},
        "preview": {"available": True},
        "episodes": [
            {
                "episode_id": episode.episode_uid,
                "privacy_sensitive": False,
            }
        ],
    }
    assert upload_session.status == "succeeded"
    assert (
        db_session.query(JobRun)
        .filter(
            JobRun.workspace_id == workspace.id,
            JobRun.kind.in_(("episode_quality", "episode_preview")),
        )
        .count()
        == 0
    )


def test_parse_reuses_package_episode_by_source_fingerprint(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    fingerprint = uuid4().hex
    upload_session = _fixture_session(
        db_session,
        workspace,
        project,
        package,
        source_fingerprint=fingerprint,
    )

    parse_collection_upload_session(db_session, session_id=upload_session.id)
    first_id = db_session.query(Episode.id).filter(Episode.data_package_id == package.id).scalar()
    upload_session.status = "uploaded"
    package.status = "uploading"
    db_session.commit()
    parse_collection_upload_session(db_session, session_id=upload_session.id)

    episodes = db_session.query(Episode).filter(Episode.data_package_id == package.id).all()
    assert [episode.id for episode in episodes] == [first_id]


def test_duplicate_source_fingerprint_counts_duration_once(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    fingerprint = uuid4().hex
    upload_session = _fixture_session(
        db_session,
        workspace,
        project,
        package,
        source_fingerprint=fingerprint,
    )
    result = dict(upload_session.result_json)
    result["declarations"] = [
        result["declarations"][0],
        {
            **result["declarations"][0],
            "episode_id": f"ep-{uuid4().hex[:12]}",
        },
    ]
    upload_session.result_json = result
    db_session.commit()

    parse_collection_upload_session(db_session, session_id=upload_session.id)

    db_session.refresh(package)
    assert package.captured_duration_hours == Decimal("1.00")
    assert db_session.query(Episode).filter(Episode.data_package_id == package.id).count() == 1


def test_privacy_value_is_preserved_per_episode_for_mixed_package(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    upload_session = _fixture_session(
        db_session,
        workspace,
        project,
        package,
        privacy_sensitive="false",
    )
    result = dict(upload_session.result_json)
    result["declarations"] = [
        result["declarations"][0],
        {
            **result["declarations"][0],
            "episode_id": f"ep-{uuid4().hex[:12]}",
            "source_fingerprint": uuid4().hex,
            "privacy_sensitive": True,
        },
    ]
    upload_session.result_json = result
    db_session.commit()

    parse_collection_upload_session(db_session, session_id=upload_session.id)

    episodes = (
        db_session.query(Episode)
        .filter(Episode.data_package_id == package.id)
        .order_by(Episode.id)
        .all()
    )
    assert [episode.metadata_json["privacy_sensitive"] for episode in episodes] == [
        "false",
        True,
    ]
    db_session.refresh(package)
    assert package.qrdf_facts_json["episodes"] == [
        {
            "episode_id": episodes[0].episode_uid,
            "privacy_sensitive": "false",
        },
        {
            "episode_id": episodes[1].episode_uid,
            "privacy_sensitive": True,
        },
    ]


def _persisted_declaration(
    package_uid: str,
    *,
    episode_id: str,
    data_path: str,
    data: bytes,
    privacy_sensitive,
) -> dict:
    metadata = {
        "qrdf_version": "0.2.0",
        "episode_id": episode_id,
        "data_file": data_path,
        "timing": {
            "start_timestamp_ns": 0,
            "end_timestamp_ns": 3_600_000_000_000,
            "duration_s": 3600.0,
        },
        "sensors": {"cameras": []},
        "capture": {"mode": "ego", "app_version": "test-2"},
        "privacy_sensitive": privacy_sensitive,
    }
    metadata_text = json.dumps(metadata, ensure_ascii=False)
    data_sha256 = hashlib.sha256(data).hexdigest()
    manifest = build_duance_import_manifest(
        source={
            "episode_id": episode_id,
            "start_ns": "0",
            "end_ns": "3600000000000",
            "metadata_sha256": hashlib.sha256(metadata_text.encode("utf-8")).hexdigest(),
            "data_mcap_sha256": data_sha256,
        },
        metadata_text=metadata_text,
        data_file={
            "path": data_path,
            "size_bytes": len(data),
            "sha256": data_sha256,
        },
    )
    stored = manifest.to_storage()
    stored["staging_path"] = f"sources/{package_uid}/{data_path}"
    return stored


def test_nonfixture_multi_package_uses_per_declaration_staged_files(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    packages = [
        make_assigned_package(db_session, workspace, project),
        make_assigned_package(db_session, workspace, project),
    ]
    data_by_package = {
        packages[0].package_uid: b"first-mcap",
        packages[1].package_uid: b"second-mcap",
    }
    declarations = {
        package.package_uid: [
            _persisted_declaration(
                package.package_uid,
                episode_id=f"ep-{index}",
                data_path=f"episode-{index}/data.mcap",
                data=data_by_package[package.package_uid],
                privacy_sensitive=index == 2,
            )
        ]
        for index, package in enumerate(packages, start=1)
    }
    upload_session = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=workspace.id,
        collection_project_id=project.id,
        status="uploaded",
        upload_mode="chunked",
        result_json={"declarations": declarations},
    )
    db_session.add(upload_session)
    db_session.flush()
    for package in packages:
        package.status = "uploading"
        db_session.add(
            CollectionUploadSessionPackage(
                upload_session_id=upload_session.id,
                data_package_id=package.id,
                package_uid=package.package_uid,
            )
        )
    staging = collection_upload_staging_dir(upload_session.id)
    for package in packages:
        declaration = declarations[package.package_uid][0]
        target = Path(staging, declaration["staging_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data_by_package[package.package_uid])
    db_session.commit()

    parse_collection_upload_session(db_session, session_id=upload_session.id)

    for package in packages:
        db_session.refresh(package)
        episode = db_session.query(Episode).filter(Episode.data_package_id == package.id).one()
        assert package.status == "pending_intake_review"
        assert package.captured_duration_hours == Decimal("1.00")
        assert episode.metadata_json["privacy_sensitive"] is (package is packages[1])


def test_multi_source_rejects_total_declared_size_over_upload_limit(monkeypatch, tmp_path):
    from data.services import collection_upload_parse

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(collection_upload_parse, "MAX_IMPORT_TOTAL_BYTES", 10)
    upload_session = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=1,
        collection_project_id=1,
        status="uploaded",
        upload_mode="chunked",
    )
    declarations = {
        "pkg_a": [
            {
                "data_file": {"path": "a.mcap", "size_bytes": 6},
                "staging_path": "sources/pkg_a/a.mcap",
            }
        ],
        "pkg_b": [
            {
                "data_file": {"path": "b.mcap", "size_bytes": 5},
                "staging_path": "sources/pkg_b/b.mcap",
            }
        ],
    }

    with pytest.raises(
        CollectionUploadParseError,
        match="source_total_size_limit_exceeded",
    ):
        _prepare_uploaded_sources(upload_session, declarations)


def test_multi_source_rejects_missing_staged_files_without_zip_fallback(monkeypatch, tmp_path):
    from data.services import collection_upload_parse

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    upload_session = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=1,
        collection_project_id=1,
        status="uploaded",
        upload_mode="chunked",
    )
    declarations = {
        "pkg_a": [
            {
                "data_file": {"path": "a.mcap", "size_bytes": 4},
                "staging_path": "sources/pkg_a/a.mcap",
            }
        ],
        "pkg_b": [
            {
                "data_file": {"path": "b.mcap", "size_bytes": 4},
                "staging_path": "sources/pkg_b/b.mcap",
            }
        ],
    }
    staging = collection_upload_staging_dir(upload_session.id)
    staging.mkdir(parents=True)
    (staging / "upload.bin").write_bytes(b"not-a-zip-and-not-used")

    with pytest.raises(CollectionUploadParseError, match="uploaded_data_unavailable"):
        collection_upload_parse._prepare_uploaded_sources(upload_session, declarations)


def test_parse_rejects_lerobot_and_marks_package_failed(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    upload_session = _fixture_session(
        db_session,
        workspace,
        project,
        package,
        source_kind="lerobot",
    )

    with pytest.raises(ValueError, match="lerobot_not_supported"):
        parse_collection_upload_session(db_session, session_id=upload_session.id)

    db_session.refresh(package)
    db_session.refresh(upload_session)
    assert package.status == "parse_failed"
    assert package.parse_error_code == "lerobot_not_supported_on_collection_upload"
    assert upload_session.status == "failed"
    assert upload_session.error_code == "lerobot_not_supported_on_collection_upload"


def test_fixture_mode_is_rejected_outside_test_environment(db_session, monkeypatch):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    upload_session = _fixture_session(db_session, workspace, project, package)
    monkeypatch.setenv("TEST_MODE", "false")
    monkeypatch.setattr(settings, "test_mode", False)

    with pytest.raises(ValueError, match="parse_fixture_mode_not_allowed"):
        parse_collection_upload_session(db_session, session_id=upload_session.id)

    db_session.refresh(package)
    assert package.status == "parse_failed"
    assert package.parse_error_code == "parse_fixture_mode_not_allowed"


def test_parse_job_is_idempotent_and_uses_ingest_queue(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    upload_session = _fixture_session(db_session, workspace, project, package)

    first = ensure_collection_upload_parse_job(db_session, upload_session)
    second = ensure_collection_upload_parse_job(db_session, upload_session)

    assert second.id == first.id
    assert first.kind == "collection_upload_parse"
    assert first.resource_type == "platform"
    assert first.resource_id == upload_session.id
    assert first.queue == "ingest"
