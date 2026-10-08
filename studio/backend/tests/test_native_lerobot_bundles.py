from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from data.database import (
    Batch,
    NativeLerobotBundle,
    NativeLerobotDataset,
    NativeLerobotImportSession,
    TaskSet,
    User,
    Workspace,
)
from data.infra import oss_client
from data.infra.oss_client import OSSObjectInfo
from data.services.native_lerobot_datasets import (
    COMPLETE_MARKER_NAME,
    NativeLerobotMarker,
    NativeLerobotObject,
    platform_native_lerobot_uri,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _object_info(*, size: int, sha256: str) -> OSSObjectInfo:
    return OSSObjectInfo(
        size=size,
        etag=f"etag-{sha256[:8]}",
        crc64=None,
        version_id=None,
        metadata={
            "x-oss-meta-sha256": sha256,
            "x-oss-meta-quicdata-copy-origin": sha256,
        },
    )


def _succeeded_native_dataset(db_session):
    actor = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    workspace = Workspace(
        name=f"native bundle workspace {datetime.utcnow().timestamp()}", creator="test"
    )
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name="native bundle task set")
    db_session.add(task_set)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="native bundle batch",
        batch_type="lerobot",
        status="ready",
        created_by_user_id=actor.id,
    )
    db_session.add(batch)
    db_session.flush()

    contents = {
        "data/demo.parquet": b"parquet-data",
        "meta/info.json": b'{"episode_count": 1}',
        "videos/demo.mp4": b"video-data",
    }
    objects = tuple(
        NativeLerobotObject(path=path, size=len(payload), sha256=_sha256(payload))
        for path, payload in sorted(contents.items())
    )
    marker_payload = {
        "schema_version": 1,
        "robot_type": "so101",
        "dataset_id": "bundle-demo",
        "objects": [
            {"path": item.path, "size": item.size, "sha256": item.sha256} for item in objects
        ],
        "file_count": len(objects),
        "total_size": sum(item.size for item in objects),
        "manifest_sha256": _sha256(
            _canonical_json(
                {
                    "robot_type": "so101",
                    "dataset_id": "bundle-demo",
                    "objects": [
                        {"path": item.path, "size": item.size, "sha256": item.sha256}
                        for item in objects
                    ],
                }
            )
        ),
        "completed_at": "2026-08-28T10:15:30Z",
    }
    marker_sha256 = _sha256(_canonical_json(marker_payload))
    platform_uri = platform_native_lerobot_uri(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        robot_type="so101",
        dataset_id="bundle-demo",
    )
    dataset = NativeLerobotDataset(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        name="native bundle dataset",
        source_oss_uri="oss://external-source/prod/raw/robot/so101/bundle-demo/",
        source_scope_id=None,
        source_scope_revision=None,
        oss_uri=platform_uri,
        robot_type="so101",
        dataset_id="bundle-demo",
        file_count=len(objects),
        total_size=sum(item.size for item in objects),
        completed_at=datetime(2026, 8, 28, 10, 15, 30),
        manifest_sha256=marker_payload["manifest_sha256"],
        marker_sha256=marker_sha256,
        status="active",
        copy_status="succeeded",
        created_by_user_id=actor.id,
    )
    db_session.add(dataset)
    db_session.commit()
    bucket = oss_client.bucket_name("export")
    marker = NativeLerobotMarker(
        bucket=bucket,
        marker_key=(platform_uri.removeprefix(f"oss://{bucket}/") + COMPLETE_MARKER_NAME),
        oss_uri=platform_uri,
        robot_type="so101",
        dataset_id="bundle-demo",
        file_count=len(objects),
        total_size=sum(item.size for item in objects),
        completed_at=datetime(2026, 8, 28, 10, 15, 30),
        manifest_sha256=marker_payload["manifest_sha256"],
        marker_sha256=marker_sha256,
        objects=objects,
    )
    return actor, dataset, marker, contents, _canonical_json(marker_payload)


def _install_platform_copy(monkeypatch, marker, contents: dict[str, bytes], marker_bytes: bytes):
    from data.services import native_lerobot_bundles as bundles

    platform_infos = {
        (marker.bucket, marker.marker_key): _object_info(
            size=len(marker_bytes), sha256=marker.marker_sha256
        )
    }
    target_root = marker.marker_key.removesuffix(COMPLETE_MARKER_NAME)
    for item in marker.objects:
        platform_infos[(marker.bucket, f"{target_root}{item.path}")] = _object_info(
            size=item.size,
            sha256=item.sha256,
        )
    source_reads: list[tuple[str, str]] = []
    uploaded: list[tuple[str, str, list[str], dict[str, str]]] = []

    def fake_info(bucket: str, key: str):
        source_reads.append((bucket, key))
        return platform_infos.get((bucket, key))

    def fake_download(local_file: Path, bucket: str, key: str):
        assert bucket == marker.bucket
        assert key.startswith(target_root)
        relative = key.removeprefix(target_root)
        payload = marker_bytes if relative == COMPLETE_MARKER_NAME else contents[relative]
        local_file.parent.mkdir(parents=True, exist_ok=True)
        local_file.write_bytes(payload)
        return local_file

    def fake_upload(local_file: Path, bucket: str, key: str, **kwargs):
        with zipfile.ZipFile(local_file) as archive:
            names = archive.namelist()
        metadata = dict(kwargs.get("metadata") or {})
        uploaded.append((bucket, key, names, metadata))
        platform_infos[(bucket, key)] = OSSObjectInfo(
            size=local_file.stat().st_size,
            etag="bundle-etag",
            crc64=None,
            version_id=None,
            metadata=metadata,
        )
        return f"oss://{bucket}/{key}"

    monkeypatch.setattr(bundles, "read_platform_native_lerobot_marker", lambda **_kwargs: marker)
    monkeypatch.setattr(oss_client, "object_info", fake_info)
    monkeypatch.setattr(oss_client, "download_object_to_file", fake_download)
    monkeypatch.setattr(oss_client, "upload_file", fake_upload)
    return source_reads, uploaded


def test_bundle_contains_one_dataset_root_and_marker_from_platform_copy(
    db_session, monkeypatch, tmp_storage
):
    from data.services.native_lerobot_bundles import (
        request_native_lerobot_bundle,
        run_native_lerobot_bundle,
    )

    actor, dataset, marker, contents, marker_bytes = _succeeded_native_dataset(db_session)
    reads, uploaded = _install_platform_copy(monkeypatch, marker, contents, marker_bytes)

    bundle, job, created = request_native_lerobot_bundle(
        db_session,
        dataset_id=dataset.id,
        actor_id=actor.id,
    )

    assert created is True
    result = run_native_lerobot_bundle(db_session, job, temp_root=tmp_storage)

    assert result["file_count"] == dataset.file_count
    assert uploaded == [
        (
            oss_client.bucket_name("export"),
            uploaded[0][1],
            [
                f"{dataset.dataset_id}/data/demo.parquet",
                f"{dataset.dataset_id}/meta/info.json",
                f"{dataset.dataset_id}/videos/demo.mp4",
                f"{dataset.dataset_id}/complete.json",
            ],
            {
                "x-oss-meta-sha256": uploaded[0][3]["x-oss-meta-sha256"],
                "x-oss-meta-marker-sha256": marker.marker_sha256,
            },
        )
    ]
    assert all(
        "external-source" not in bucket and "prod/raw/robot" not in key for bucket, key in reads
    )
    db_session.refresh(bundle)
    assert bundle.status == "succeeded"
    assert bundle.bundle_uri.startswith("oss://")


def test_session_created_dataset_copy_can_complete_individual_bundle(
    db_session, monkeypatch, tmp_storage
):
    """A session destination includes the stable native dataset ID end to end."""
    from data.services.native_lerobot_bundles import (
        request_native_lerobot_bundle,
        run_native_lerobot_bundle,
    )

    actor, dataset, marker, contents, marker_bytes = _succeeded_native_dataset(db_session)
    session = NativeLerobotImportSession(
        id="7d6f7a2d-5e4a-48d0-a5b4-3b42b91d2ec6",
        batch_id=dataset.batch_id,
        workspace_id=dataset.workspace_id,
        task_set_id=dataset.task_set_id,
        created_by_user_id=actor.id,
        status="completed",
        selected_count=1,
        registered_count=1,
    )
    db_session.add(session)
    db_session.flush()
    dataset.import_session_id = session.id
    dataset.oss_uri = platform_native_lerobot_uri(
        workspace_id=dataset.workspace_id,
        task_set_id=dataset.task_set_id,
        batch_id=dataset.batch_id,
        native_dataset_id=dataset.id,
        robot_type=dataset.robot_type,
        dataset_id=dataset.dataset_id,
    )
    bucket = oss_client.bucket_name("export")
    marker = replace(
        marker,
        marker_key=dataset.oss_uri.removeprefix(f"oss://{bucket}/") + COMPLETE_MARKER_NAME,
        oss_uri=dataset.oss_uri,
    )
    db_session.commit()
    _reads, uploaded = _install_platform_copy(monkeypatch, marker, contents, marker_bytes)

    bundle, job, created = request_native_lerobot_bundle(
        db_session,
        dataset_id=dataset.id,
        actor_id=actor.id,
    )
    assert created is True
    assert job is not None

    result = run_native_lerobot_bundle(db_session, job, temp_root=tmp_storage)

    assert result["file_count"] == dataset.file_count
    assert uploaded
    db_session.refresh(bundle)
    assert bundle.status == "succeeded"


def test_bundle_request_reuses_unexpired_same_marker_without_reading_source(
    db_session, monkeypatch
):
    from data.services.native_lerobot_bundles import request_native_lerobot_bundle

    actor, dataset, marker, contents, marker_bytes = _succeeded_native_dataset(db_session)
    _reads, _uploaded = _install_platform_copy(monkeypatch, marker, contents, marker_bytes)
    existing = NativeLerobotBundle(
        native_lerobot_dataset_id=dataset.id,
        marker_sha256=dataset.marker_sha256,
        status="succeeded",
        bundle_uri=(
            f"oss://{oss_client.bucket_name('export')}/exports/v1/native-lerobot-bundles/"
            f"workspaces/{dataset.workspace_id}/datasets/{dataset.id}/{dataset.marker_sha256}.zip"
        ),
        expires_at=datetime.utcnow() + timedelta(days=1),
    )
    db_session.add(existing)
    db_session.commit()
    monkeypatch.setattr(
        oss_client,
        "download_object_to_file",
        lambda *_args, **_kwargs: pytest.fail(
            "bundle reuse must not download external or platform payloads"
        ),
    )

    reused, job, created = request_native_lerobot_bundle(
        db_session,
        dataset_id=dataset.id,
        actor_id=actor.id,
    )

    assert reused.id == existing.id
    assert job is None
    assert created is False


def test_cleanup_expires_only_exact_bundle_uri(db_session, monkeypatch):
    from data.services.native_lerobot_bundles import (
        cleanup_expired_native_lerobot_bundles,
    )

    _actor, dataset, _marker, _contents, _marker_bytes = _succeeded_native_dataset(db_session)
    bundle = NativeLerobotBundle(
        native_lerobot_dataset_id=dataset.id,
        marker_sha256=dataset.marker_sha256,
        status="succeeded",
        bundle_uri=(
            f"oss://{oss_client.bucket_name('export')}/exports/v1/native-lerobot-bundles/"
            f"workspaces/{dataset.workspace_id}/datasets/{dataset.id}/{dataset.marker_sha256}.zip"
        ),
        expires_at=datetime.utcnow() - timedelta(seconds=1),
    )
    db_session.add(bundle)
    db_session.commit()
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        oss_client, "delete_object", lambda bucket, key: deleted.append((bucket, key)) or True
    )

    result = cleanup_expired_native_lerobot_bundles(db_session, limit=10)

    assert result == 1
    assert deleted == [
        (
            oss_client.bucket_name("export"),
            f"exports/v1/native-lerobot-bundles/workspaces/{dataset.workspace_id}/"
            f"datasets/{dataset.id}/{dataset.marker_sha256}.zip",
        )
    ]
    db_session.refresh(bundle)
    assert bundle.status == "expired"


def test_bundle_job_is_recoverable_and_registered_on_export_worker():
    from data.services.job_runs import job_kind_is_recoverable
    from data.tasks.batch_workers import _HANDLERS

    assert job_kind_is_recoverable("native_lerobot_bundle") is True
    assert "native_lerobot_bundle" in _HANDLERS


def test_legacy_bundle_creation_api_is_retired(client, admin_headers, db_session, monkeypatch):
    _actor, dataset, marker, contents, marker_bytes = _succeeded_native_dataset(db_session)
    _install_platform_copy(monkeypatch, marker, contents, marker_bytes)

    created = client.post(
        f"/api/v1/native-lerobot-datasets/{dataset.id}/bundles",
        headers=admin_headers,
    )

    assert created.status_code == 410, created.text
    assert (
        db_session.query(NativeLerobotBundle)
        .filter(NativeLerobotBundle.native_lerobot_dataset_id == dataset.id)
        .count()
        == 0
    )


def test_historical_successful_child_is_readable_but_not_bundle_deliverable(
    client, admin_headers, db_session, monkeypatch
):
    actor, dataset, marker, contents, marker_bytes = _succeeded_native_dataset(db_session)
    failed_sibling = NativeLerobotDataset(
        workspace_id=dataset.workspace_id,
        task_set_id=dataset.task_set_id,
        batch_id=dataset.batch_id,
        name="failed sibling",
        source_oss_uri="oss://external-source/prod/raw/robot/so101/failed-sibling/",
        oss_uri="oss://platform/failed-sibling/",
        robot_type="so101",
        dataset_id="failed-sibling",
        file_count=1,
        total_size=1,
        completed_at=dataset.completed_at,
        manifest_sha256="e" * 64,
        marker_sha256="f" * 64,
        copy_status="failed",
        created_by_user_id=actor.id,
    )
    db_session.add(failed_sibling)
    db_session.flush()
    dataset.batch.status = "partial_failed"
    db_session.commit()
    _install_platform_copy(monkeypatch, marker, contents, marker_bytes)

    platform_uri = client.get(
        f"/api/v1/native-lerobot-datasets/{dataset.id}/oss-uri",
        headers=admin_headers,
    )
    bundle = client.post(
        f"/api/v1/native-lerobot-datasets/{dataset.id}/bundles",
        headers=admin_headers,
    )

    assert platform_uri.status_code == 200, platform_uri.text
    assert platform_uri.json()["data"]["oss_uri"] == dataset.oss_uri
    assert bundle.status_code == 410, bundle.text
    db_session.refresh(failed_sibling)
    assert failed_sibling.status == "active"
    assert failed_sibling.copy_status == "failed"


def test_legacy_bundle_download_creation_is_retired(
    client, admin_headers, db_session, monkeypatch, tmp_storage
):
    _actor, dataset, marker, contents, marker_bytes = _succeeded_native_dataset(db_session)
    _install_platform_copy(monkeypatch, marker, contents, marker_bytes)
    created = client.post(
        f"/api/v1/native-lerobot-datasets/{dataset.id}/bundles",
        headers=admin_headers,
    )
    assert created.status_code == 410, created.text


def test_expired_bundle_reuses_same_immutable_target_for_the_same_marker(
    db_session, monkeypatch, tmp_storage
):
    from data.services.native_lerobot_bundles import (
        request_native_lerobot_bundle,
        run_native_lerobot_bundle,
    )

    actor, dataset, marker, contents, marker_bytes = _succeeded_native_dataset(db_session)
    _reads, uploaded = _install_platform_copy(monkeypatch, marker, contents, marker_bytes)
    bundle, first_job, created = request_native_lerobot_bundle(
        db_session,
        dataset_id=dataset.id,
        actor_id=actor.id,
    )
    assert created is True
    run_native_lerobot_bundle(db_session, first_job, temp_root=tmp_storage)
    db_session.commit()
    first_sha256 = uploaded[0][3]["x-oss-meta-sha256"]

    bundle.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    first_download = oss_client.download_object_to_file

    def download_with_a_different_staging_mtime(*args, **kwargs):
        path = first_download(*args, **kwargs)
        os.utime(path, (1_893_456_000, 1_893_456_000))
        return path

    monkeypatch.setattr(
        oss_client, "download_object_to_file", download_with_a_different_staging_mtime
    )
    queued, second_job, created = request_native_lerobot_bundle(
        db_session,
        dataset_id=dataset.id,
        actor_id=actor.id,
    )
    assert queued.id == bundle.id
    assert created is True
    assert second_job is not None
    result = run_native_lerobot_bundle(db_session, second_job, temp_root=tmp_storage)

    assert result["file_count"] == dataset.file_count
    assert uploaded == [uploaded[0]]
    assert uploaded[0][3]["x-oss-meta-sha256"] == first_sha256
