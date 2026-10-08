"""Catalog datasets: immutable version asset manifest, export reference protection, and LeRobot direct import cannot convert to QRDF."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_intake_approved_package,
)

from data.database import Batch, JobRun, NativeLerobotDataset
from data.infra.object_storage import (
    ObjectStorageError,
    StorageObjectIntegrityError,
    StorageObjectNotFound,
    StorageObjectRef,
)
from data.models.annotation_work import AnnotationWorkItem, ReviewWorkItem
from data.models.catalog_dataset import CatalogDatasetVersion
from data.models.data_asset import DataAsset
from data.models.data_batch import DataBatch
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.catalog_datasets import (
    CatalogDatasetNotSupported,
    _download_ref,
    _head_only,
)
from data.services.data_assets import publish_data_asset
from data.services.data_batches import create_data_batch


def _create_publishable_batch(db, *, name: str | None = None):
    workspace = make_workspace(db)
    project = make_project(db, workspace)
    package = seed_intake_approved_package(db, workspace, project, episode_hours=(1.0, 0.5))
    # seed_intake_approved_package already records a verified admission fact
    # with a complete objects_json manifest (tests.test_episode_objects.
    # verified_entries()); no per-episode override is needed any more.
    package.governed_valid_duration_hours = Decimal("1.50")
    batch, _ = create_data_batch(
        db,
        workspace_id=workspace.id,
        name=name or f"CatAsset-{uuid4().hex[:6]}",
        data_package_ids=[package.id],
        label_ids=[],
        integrity_check_enabled=False,
        quality_check_enabled=False,
        compliance_check_enabled=False,
        annotation_enabled=False,
        annotator_user_ids=[],
        reviewer_user_id=None,
        review_mode="single",
        created_by_user_id=None,
    )
    batch.status = "publishing"
    db.commit()
    db.refresh(batch)
    return batch


def _publish_asset(db) -> int:
    batch = _create_publishable_batch(db)
    asset = publish_data_asset(db, batch_id=batch.id)
    db.commit()
    assert asset is not None
    return asset.id


def _create_catalog_dataset(client, headers, *, name: str | None = None) -> dict:
    response = client.post(
        "/api/v1/catalog-datasets",
        headers=headers,
        json={
            "name": name or f"Catalog-{uuid4().hex[:8]}",
            "description": "task5",
            "source_kind": "qrdf_assets",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _seed_native_lerobot_dataset(db) -> NativeLerobotDataset:
    from data.database import TaskSet

    workspace = make_workspace(db)
    task_set = TaskSet(workspace_id=workspace.id, name=f"lr-ts-{uuid4().hex[:6]}")
    db.add(task_set)
    db.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"lerobot-batch-{uuid4().hex[:6]}",
        batch_type="lerobot",
        status="ready",
    )
    db.add(batch)
    db.flush()
    marker = uuid4().hex + "a" * (64 - 32)
    dataset_id = f"数据集-{marker[:8]}"
    objects = [{"path": "data/episode-数据.parquet", "size": 1, "sha256": "d" * 64}]
    manifest_sha256 = hashlib.sha256(
        json.dumps(
            {"robot_type": "so101", "dataset_id": dataset_id, "objects": objects},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    marker_payload = {
        "schema_version": 1,
        "robot_type": "so101",
        "dataset_id": dataset_id,
        "objects": objects,
        "file_count": 1,
        "total_size": 1,
        "manifest_sha256": manifest_sha256,
        "completed_at": "2026-09-17T12:00:00Z",
    }
    marker = hashlib.sha256(
        json.dumps(marker_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    row = NativeLerobotDataset(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        name=f"native-{marker[:8]}",
        source_oss_uri=f"oss://quic-data-platform/native/{marker}/",
        oss_uri=f"oss://platform/native/{marker}/",
        robot_type="so101",
        dataset_id=dataset_id,
        file_count=1,
        total_size=1,
        completed_at=datetime(2026, 9, 17, 12, 0, 0),
        manifest_sha256=manifest_sha256,
        marker_sha256=marker,
        objects_json=objects,
        status="active",
        copy_status="succeeded",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_native_lerobot_direct_declaration_is_global_and_never_creates_collection_state(
    client, db_session, admin_headers
):
    """A direct native source is not allowed to enter the collection workflow.

    This catches an implementation that reuses the legacy
    ``ExternalOssImportScope -> Batch`` registration path merely to get a
    native LeRobot upload into the catalog.
    """
    workspace = make_workspace(db_session)
    before = {
        "batches": db_session.query(Batch).count(),
        "data_batches": db_session.query(DataBatch).count(),
        "assets": db_session.query(DataAsset).count(),
        "facts": db_session.query(EpisodeAdmissionFact).count(),
        "annotation_work": db_session.query(AnnotationWorkItem).count(),
        "review_work": db_session.query(ReviewWorkItem).count(),
    }
    payload = b"native-lerobot-declaration"
    response = client.post(
        "/api/v1/native-lerobot-direct-uploads",
        headers=admin_headers,
        json={
            "name": f"Native direct {uuid4().hex[:8]}",
            "description": "direct raw source",
            "source_workspace_id": workspace.id,
            "robot_type": "unknown",
            "dataset_id": f"native-{uuid4().hex[:8]}",
            "objects": [
                {
                    "path": "meta/info.json",
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
        },
    )

    assert response.status_code == 200, response.text
    item = response.json()["data"]
    assert item["status"] == "declared"
    assert item["source_workspace_id"] == workspace.id
    assert item["catalog_dataset_id"] is None
    assert item["objects"][0]["object_key"].startswith(
        f"export/v1/native-lerobot-direct/{item['id']}/"
    )
    assert {
        "batches": db_session.query(Batch).count(),
        "data_batches": db_session.query(DataBatch).count(),
        "assets": db_session.query(DataAsset).count(),
        "facts": db_session.query(EpisodeAdmissionFact).count(),
        "annotation_work": db_session.query(AnnotationWorkItem).count(),
        "review_work": db_session.query(ReviewWorkItem).count(),
    } == before


def test_native_lerobot_direct_system_failure_retries_the_same_frozen_source(
    client, db_session, admin_headers, monkeypatch
):
    """A retryable infrastructure failure keeps the immutable raw attempt.

    A manual retry receives a distinct JobRun.  The source must be rebound to
    that job in the same transaction so the new delivery is not rejected as a
    stale attempt while the old job remains immutable for audit.
    """
    from data.models.native_lerobot_direct import NativeLerobotDirectSource
    from data.services.native_lerobot_direct_uploads import (
        NativeLerobotDirectValidationError,
        run_direct_source_validation,
    )

    workspace = make_workspace(db_session)
    payload = b"retryable-native-direct-source"
    declared = client.post(
        "/api/v1/native-lerobot-direct-uploads",
        headers=admin_headers,
        json={
            "name": f"Native retry {uuid4().hex[:8]}",
            "source_workspace_id": workspace.id,
            "robot_type": "unknown",
            "dataset_id": f"native-retry-{uuid4().hex[:8]}",
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
    source = db_session.get(NativeLerobotDirectSource, source_id)
    assert source is not None
    original_key = source.objects[0].object_key
    failed_job = JobRun(
        id=uuid4().hex,
        kind="native_lerobot_direct_validate",
        resource_type="native_lerobot_direct_source",
        resource_id=source.id,
        idempotency_key=f"native-direct-retry-{uuid4().hex}",
        queue="ingest",
        status="failed",
        phase="failed",
        detail_json={"native_lerobot_direct_source_id": source.id},
    )
    source.status = "failed"
    source.error_code = "native_lerobot_direct_storage_unavailable"
    source.error_message = "transient provider timeout"
    source.validation_job_id = failed_job.id
    db_session.add(failed_job)
    db_session.commit()
    dispatched: list[str] = []
    monkeypatch.setattr(
        "data.routers.native_lerobot_direct_uploads.dispatch_media_job",
        lambda job, **_kwargs: dispatched.append(job.id),
    )

    retried = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{source.id}/retry",
        headers=admin_headers,
    )
    assert retried.status_code == 200, retried.text
    result = db_session.get(JobRun, retried.json()["data"]["validation_job_id"])
    assert result is not None
    db_session.refresh(source)

    assert result.id != failed_job.id
    assert result.detail_json["retry_of_job_id"] == failed_job.id
    assert source.status == "queued"
    assert source.validation_job_id == result.id
    assert source.objects[0].object_key == original_key
    assert dispatched == [result.id]
    tracked = client.get(f"/api/v1/jobs/{result.id}", headers=admin_headers)
    assert tracked.status_code == 200, tracked.text
    assert tracked.json()["data"]["job"]["id"] == result.id
    generic_retry = client.post(f"/api/v1/jobs/{result.id}/retry", headers=admin_headers)
    assert generic_retry.status_code == 409, generic_retry.text
    assert "native LeRobot source retry" in generic_retry.json()["message"]
    with pytest.raises(NativeLerobotDirectValidationError, match="no longer current"):
        run_direct_source_validation(db_session, failed_job)
    # The stale-worker check deliberately takes a row lock before rejecting;
    # release it before exercising the independent HTTP retry request.
    db_session.rollback()

    repeated = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{source.id}/retry",
        headers=admin_headers,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["validation_job_id"] == result.id
    assert dispatched == [result.id, result.id]


def _queued_native_lerobot_direct_source(db_session):
    from data.models.native_lerobot_direct import NativeLerobotDirectSource
    from data.services.native_lerobot_direct_uploads import create_direct_source

    payload = b"native-direct-provider-classification"
    source = create_direct_source(
        db_session,
        name=f"Native direct validation {uuid4().hex[:8]}",
        description="provider failure classification",
        source_workspace_id=None,
        robot_type="unknown",
        dataset_id=f"native-{uuid4().hex[:8]}",
        objects=[
            {
                "path": "meta/info.json",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        created_by_user_id=None,
    )
    item = source.objects[0]
    item.status = "completed"
    item.provider_ref_json = {
        "bucket_role": "export",
        "object_key": item.object_key,
        "version_id": "v1",
        "etag": "etag-1",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    job = JobRun(
        id=uuid4().hex,
        kind="native_lerobot_direct_validate",
        resource_type="native_lerobot_direct_source",
        resource_id=source.id,
        idempotency_key=f"native-direct-classification-{uuid4().hex}",
        queue="ingest",
        status="queued",
        phase="queued",
        detail_json={"native_lerobot_direct_source_id": source.id},
    )
    source.status = "queued"
    source.validation_job_id = job.id
    db_session.add(job)
    db_session.commit()
    db_session.refresh(source)
    assert isinstance(source, NativeLerobotDirectSource)
    return source, job


def test_native_lerobot_direct_late_job_cannot_report_success_after_retry(
    db_session,
):
    """A stale delivery must fail even after its replacement has succeeded."""
    from data.services.native_lerobot_direct_uploads import (
        NativeLerobotDirectValidationError,
        run_direct_source_validation,
    )

    source, stale_job = _queued_native_lerobot_direct_source(db_session)
    current_job = JobRun(
        id=uuid4().hex,
        kind="native_lerobot_direct_validate",
        resource_type="native_lerobot_direct_source",
        resource_id=source.id,
        idempotency_key=f"native-direct-current-{uuid4().hex}",
        queue="ingest",
        status="succeeded",
        phase="succeeded",
        detail_json={"native_lerobot_direct_source_id": source.id},
    )
    source.validation_job_id = current_job.id
    source.status = "succeeded"
    db_session.add(current_job)
    db_session.commit()

    with pytest.raises(NativeLerobotDirectValidationError, match="no longer current"):
        run_direct_source_validation(db_session, stale_job)


@pytest.mark.parametrize("source_status", ["queued", "running"])
def test_native_lerobot_direct_retry_recovers_exhausted_reclaimed_job(
    client, admin_headers, db_session, monkeypatch, source_status
):
    """A reclaimed terminal job may be replaced despite a stale source projection."""
    from data.services.job_runs import MAX_RETRIES
    from data.services.native_lerobot_direct_uploads import create_direct_source

    source = create_direct_source(
        db_session,
        name=f"Native reclaimed retry {uuid4().hex[:8]}",
        description="",
        source_workspace_id=None,
        robot_type="unknown",
        dataset_id=f"native-reclaimed-{uuid4().hex[:8]}",
        objects=[{"path": "meta/info.json", "size_bytes": 1, "sha256": "a" * 64}],
        created_by_user_id=None,
    )
    exhausted = JobRun(
        id=uuid4().hex,
        kind="native_lerobot_direct_validate",
        resource_type="native_lerobot_direct_source",
        resource_id=source.id,
        idempotency_key=f"native-direct-reclaimed-{uuid4().hex}",
        queue="ingest",
        status="failed",
        phase="failed",
        retry_count=MAX_RETRIES,
        error_code="worker_lease_expired",
        error_message="worker lease expired before the job completed",
        detail_json={"native_lerobot_direct_source_id": source.id},
    )
    source.status = source_status
    source.validation_job_id = exhausted.id
    db_session.add(exhausted)
    db_session.commit()

    dispatched: list[str] = []
    monkeypatch.setattr(
        "data.routers.native_lerobot_direct_uploads.dispatch_media_job",
        lambda job, **_kwargs: dispatched.append(job.id),
    )
    generic = client.post(f"/api/v1/jobs/{exhausted.id}/retry", headers=admin_headers)
    assert generic.status_code == 409, generic.text

    retried = client.post(
        f"/api/v1/native-lerobot-direct-uploads/{source.id}/retry",
        headers=admin_headers,
    )
    assert retried.status_code == 200, retried.text
    retry = db_session.get(JobRun, retried.json()["data"]["validation_job_id"])
    assert retry is not None
    db_session.refresh(source)
    assert retry.id != exhausted.id
    assert retry.detail_json["retry_of_job_id"] == exhausted.id
    assert source.status == "queued"
    assert source.validation_job_id == retry.id
    assert dispatched == [retry.id]


def test_native_lerobot_direct_same_catalog_name_creates_next_version(db_session):
    """Repeated direct imports are immutable versions of one global dataset."""
    from data.services.native_lerobot_direct_uploads import (
        _create_direct_catalog_version,
        create_direct_source,
    )

    name = f"Native direct versions {uuid4().hex[:8]}"
    source_a = create_direct_source(
        db_session,
        name=name,
        description="first direct version",
        source_workspace_id=None,
        robot_type="unknown",
        dataset_id=f"native-a-{uuid4().hex[:8]}",
        objects=[{"path": "meta/info.json", "size_bytes": 1, "sha256": "a" * 64}],
        created_by_user_id=None,
    )
    dataset_a, version_a = _create_direct_catalog_version(
        db_session,
        source=source_a,
        snapshot={"source_id": source_a.id, "generation": 1},
    )
    source_b = create_direct_source(
        db_session,
        name=name,
        description="second direct version",
        source_workspace_id=None,
        robot_type="unknown",
        dataset_id=f"native-b-{uuid4().hex[:8]}",
        objects=[{"path": "meta/info.json", "size_bytes": 1, "sha256": "b" * 64}],
        created_by_user_id=None,
    )
    dataset_b, version_b = _create_direct_catalog_version(
        db_session,
        source=source_b,
        snapshot={"source_id": source_b.id, "generation": 2},
    )

    assert dataset_b.id == dataset_a.id
    assert dataset_b.source_kind == "lerobot_direct"
    assert version_a.version == 1
    assert version_b.version == 2
    assert version_b.source_snapshot_json == {"source_id": source_b.id, "generation": 2}


def test_direct_catalog_version_cannot_be_deleted_while_source_references_it(
    client, db_session, admin_headers
):
    """Direct source provenance must block destructive catalog version deletion."""
    from data.services.native_lerobot_direct_uploads import (
        _create_direct_catalog_version,
        create_direct_source,
    )

    source = create_direct_source(
        db_session,
        name=f"Native direct retained {uuid4().hex[:8]}",
        description="direct catalog retention",
        source_workspace_id=None,
        robot_type="unknown",
        dataset_id=f"native-retained-{uuid4().hex[:8]}",
        objects=[{"path": "meta/info.json", "size_bytes": 1, "sha256": "a" * 64}],
        created_by_user_id=None,
    )
    dataset, version = _create_direct_catalog_version(
        db_session,
        source=source,
        snapshot={"source_id": source.id},
    )
    source.catalog_dataset_id = dataset.id
    source.catalog_dataset_version_id = version.id
    db_session.commit()

    deleted = client.delete(
        f"/api/v1/catalog-datasets/versions/{version.id}",
        headers=admin_headers,
    )

    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["detail"]["code"] == "version_referenced"
    assert "archive" in deleted.json()["detail"]["message"]
    assert db_session.get(CatalogDatasetVersion, version.id) is not None

    archived = client.post(
        f"/api/v1/catalog-datasets/versions/{version.id}/archive",
        headers=admin_headers,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["data"]["status"] == "archived"


def test_native_lerobot_direct_catalog_conflict_keeps_raw_marker_evidence(db_session, monkeypatch):
    """A catalog conflict after raw marker write must retain exact evidence.

    This catches moving ``marker_ref_json`` assignment after catalog creation:
    the deterministic raw marker exists in storage, but a failed source would
    otherwise have no durable identity for an operator to inspect or reconcile.
    """
    from data.services.catalog_datasets import create_catalog_dataset
    from data.services.native_lerobot_direct_uploads import (
        NativeLerobotDirectValidationError,
        run_direct_source_validation,
    )

    source, job = _queued_native_lerobot_direct_source(db_session)
    create_catalog_dataset(
        db_session,
        name=source.name,
        description="conflicting QRDF catalog",
        source_kind="qrdf_assets",
        created_by_user_id=None,
    )
    db_session.commit()
    marker = StorageObjectRef(
        "export",
        f"export/v1/native-lerobot-direct/{source.id}/complete.json",
        "marker-v1",
        "marker-etag",
        17,
        "a" * 64,
    )
    monkeypatch.setattr(
        "data.services.native_lerobot_direct_uploads._materialize_direct_source",
        lambda *_args, **_kwargs: [{"path": "meta/info.json", "ref": {}}],
    )
    monkeypatch.setattr(
        "data.services.native_lerobot_direct_uploads._persist_direct_marker",
        lambda *_args, **_kwargs: marker,
    )

    with pytest.raises(NativeLerobotDirectValidationError, match="different source kind"):
        run_direct_source_validation(db_session, job)

    db_session.refresh(source)
    assert source.status == "failed"
    assert source.error_code == "native_lerobot_direct_catalog_failed"
    assert source.marker_ref_json == {
        "bucket_role": "export",
        "object_key": marker.object_key,
        "version_id": "marker-v1",
        "etag": "marker-etag",
        "size_bytes": 17,
        "sha256": "a" * 64,
    }


def test_native_lerobot_direct_cancel_reclaims_only_its_exact_raw_state(db_session, monkeypatch):
    """Cancellation must reclaim provider orphans without a bucket-wide sweep."""
    from data.services.native_lerobot_direct_uploads import (
        cancel_direct_source_upload,
        create_direct_source,
    )

    payload = b"native-direct-cancel"
    source = create_direct_source(
        db_session,
        name=f"Native direct cancel {uuid4().hex[:8]}",
        description="cancel exact raw source",
        source_workspace_id=None,
        robot_type="unknown",
        dataset_id=f"native-cancel-{uuid4().hex[:8]}",
        objects=[
            {
                "path": "meta/info.json",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            {
                "path": "data/chunk-000.parquet",
                "size_bytes": 4,
                "sha256": "a" * 64,
            },
        ],
        created_by_user_id=None,
    )
    uploading, completed = source.objects
    uploading.status = "uploading"
    # Simulate the crash gap after CreateMultipart succeeds but before the
    # upload ID is committed to the source object row.
    uploading.upload_id = ""
    completed.status = "completed"
    completed.provider_ref_json = {
        "bucket_role": "export",
        "object_key": completed.object_key,
        "version_id": "completed-v1",
        "etag": "completed-etag",
        "size_bytes": completed.size_bytes,
        "sha256": completed.sha256,
    }
    source.status = "uploading"
    db_session.commit()

    calls: list[tuple[str, str, str | None]] = []
    active_uploads = {uploading.object_key: {"provider-orphan"}}
    completed_exists = True

    class _Provider:
        def list_multipart_upload_ids(self, ref):
            calls.append(("list", ref.object_key, None))
            return tuple(sorted(active_uploads.get(ref.object_key, set())))

        def abort_multipart(self, ref, upload_id):
            calls.append(("abort", ref.object_key, upload_id))
            active_uploads.get(ref.object_key, set()).discard(upload_id)

        def head(self, ref):
            nonlocal completed_exists
            calls.append(("head", ref.object_key, None))
            if ref.object_key == completed.object_key and completed_exists:
                return StorageObjectRef(
                    "export",
                    completed.object_key,
                    "completed-v1",
                    "completed-etag",
                    completed.size_bytes,
                    completed.sha256,
                )
            raise StorageObjectNotFound("missing upload object")

        def delete_exact(self, ref):
            nonlocal completed_exists
            calls.append(("delete", ref.object_key, ref.version_id))
            if ref.object_key == completed.object_key:
                completed_exists = False

    monkeypatch.setattr(
        "data.services.native_lerobot_direct_uploads.get_storage_provider",
        lambda: _Provider(),
    )

    cancelled = cancel_direct_source_upload(db_session, source_id=source.id)
    db_session.commit()
    db_session.refresh(source)

    assert cancelled.status == "cancelled"
    assert source.status == "cancelled"
    assert [item.status for item in source.objects] == ["cancelled", "cancelled"]
    assert all(not item.upload_id and not item.provider_ref_json for item in source.objects)
    assert ("abort", uploading.object_key, "provider-orphan") in calls
    assert ("delete", completed.object_key, "completed-v1") in calls
    assert all(
        object_key in {uploading.object_key, completed.object_key}
        for _operation, object_key, _value in calls
    )


def test_native_lerobot_direct_validation_keeps_transient_storage_retryable(
    db_session, monkeypatch
):
    """A provider outage must not turn a frozen input into a new upload attempt."""
    from data.services.native_lerobot_direct_uploads import run_direct_source_validation

    source, job = _queued_native_lerobot_direct_source(db_session)

    class _UnavailableProvider:
        def head(self, _ref):
            raise ObjectStorageError("temporary storage outage")

    monkeypatch.setattr(
        "data.services.native_lerobot_direct_uploads.get_storage_provider",
        lambda: _UnavailableProvider(),
    )

    with pytest.raises(ObjectStorageError, match="temporary storage outage"):
        run_direct_source_validation(db_session, job)

    db_session.refresh(source)
    assert source.status == "failed"
    assert source.error_code == "native_lerobot_direct_storage_unavailable"


def test_native_lerobot_direct_validation_requires_new_attempt_for_missing_raw_object(
    db_session, monkeypatch
):
    """A missing frozen object is content loss, not a retryable provider outage."""
    from data.services.native_lerobot_direct_uploads import (
        NativeLerobotDirectValidationError,
        run_direct_source_validation,
    )

    source, job = _queued_native_lerobot_direct_source(db_session)

    class _MissingProvider:
        def head(self, _ref):
            raise StorageObjectNotFound("raw object no longer exists")

    monkeypatch.setattr(
        "data.services.native_lerobot_direct_uploads.get_storage_provider",
        lambda: _MissingProvider(),
    )

    with pytest.raises(NativeLerobotDirectValidationError, match="export object is unavailable"):
        run_direct_source_validation(db_session, job)

    db_session.refresh(source)
    assert source.status == "failed"
    assert source.error_code == "native_lerobot_direct_object_unavailable"


def test_native_lerobot_direct_validation_requires_new_attempt_for_corrupt_raw_object(
    db_session, monkeypatch
):
    """A downloaded identity mismatch is source corruption, not an outage."""
    from data.services.native_lerobot_direct_uploads import (
        NativeLerobotDirectValidationError,
        run_direct_source_validation,
    )

    source, job = _queued_native_lerobot_direct_source(db_session)

    class _CorruptProvider:
        def head(self, ref):
            return ref

        def download_file(self, _ref, _destination):
            raise StorageObjectIntegrityError("downloaded object sha256 does not match")

    monkeypatch.setattr(
        "data.services.native_lerobot_direct_uploads.get_storage_provider",
        lambda: _CorruptProvider(),
    )

    with pytest.raises(NativeLerobotDirectValidationError, match="export object changed"):
        run_direct_source_validation(db_session, job)

    db_session.refresh(source)
    assert source.status == "failed"
    assert source.error_code == "native_lerobot_direct_object_changed"


def test_version_asset_list_immutable_and_unique(client, db_session, admin_headers):
    asset_a = _publish_asset(db_session)
    asset_b = _publish_asset(db_session)
    dataset = _create_catalog_dataset(client, admin_headers)

    created = client.post(
        f"/api/v1/catalog-datasets/{dataset['id']}/versions",
        headers=admin_headers,
        json={"data_asset_ids": [asset_a, asset_b, asset_a]},
    )
    assert created.status_code == 200, created.text
    version = created.json()["data"]
    assert version["version"] == 1
    assert version["data_asset_ids"] == [asset_a, asset_b]

    empty = client.post(
        f"/api/v1/catalog-datasets/{dataset['id']}/versions",
        headers=admin_headers,
        json={"data_asset_ids": []},
    )
    assert empty.status_code == 422

    listed = client.get(
        f"/api/v1/catalog-datasets/{dataset['id']}/versions",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    items = listed.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["data_asset_ids"] == [asset_a, asset_b]

    # No mutate-in-place endpoint: content change requires a new version.
    second = client.post(
        f"/api/v1/catalog-datasets/{dataset['id']}/versions",
        headers=admin_headers,
        json={"data_asset_ids": [asset_b]},
    )
    assert second.status_code == 200
    assert second.json()["data"]["version"] == 2
    assert second.json()["data"]["data_asset_ids"] == [asset_b]


def test_export_blocks_version_delete(client, db_session, admin_headers):
    asset_id = _publish_asset(db_session)
    dataset = _create_catalog_dataset(client, admin_headers)
    created = client.post(
        f"/api/v1/catalog-datasets/{dataset['id']}/versions",
        headers=admin_headers,
        json={"data_asset_ids": [asset_id]},
    )
    assert created.status_code == 200
    version_id = created.json()["data"]["id"]

    delete_ok = client.delete(
        f"/api/v1/catalog-datasets/versions/{version_id}",
        headers=admin_headers,
    )
    assert delete_ok.status_code == 200

    # Recreate version and export — then delete must be blocked.
    created2 = client.post(
        f"/api/v1/catalog-datasets/{dataset['id']}/versions",
        headers=admin_headers,
        json={"data_asset_ids": [asset_id]},
    )
    version_id = created2.json()["data"]["id"]
    exported = client.post(
        f"/api/v1/catalog-datasets/versions/{version_id}/export",
        headers=admin_headers,
        json={"format": "qrdf_0_2"},
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["data"]["format"] == "qrdf_0_2"

    blocked = client.delete(
        f"/api/v1/catalog-datasets/versions/{version_id}",
        headers=admin_headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "version_referenced"

    archived = client.post(
        f"/api/v1/catalog-datasets/versions/{version_id}/archive",
        headers=admin_headers,
    )
    assert archived.status_code == 200
    assert archived.json()["data"]["status"] == "archived"


def test_lerobot_direct_cannot_export_qrdf(client, db_session, admin_headers):
    """A direct-source catalog version only supports LeRobot export."""
    from data.services.native_lerobot_direct_uploads import (
        _create_direct_catalog_version,
        create_direct_source,
    )

    source = create_direct_source(
        db_session,
        name=f"LR-Direct-{uuid4().hex[:8]}",
        description="direct export format check",
        source_workspace_id=None,
        robot_type="unknown",
        dataset_id=f"direct-{uuid4().hex[:8]}",
        objects=[{"path": "meta/info.json", "size_bytes": 1, "sha256": "a" * 64}],
        created_by_user_id=None,
    )
    _dataset, version = _create_direct_catalog_version(
        db_session,
        source=source,
        snapshot={"schema": "quicstudio.lerobot-direct-source.v1", "source_id": source.id},
    )
    db_session.commit()

    refused = client.post(
        f"/api/v1/catalog-datasets/versions/{version.id}/export",
        headers=admin_headers,
        json={"format": "qrdf_0_2"},
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "not_supported"


def test_native_transport_rejects_wrong_head_identity():
    class WrongHead:
        def head(self, _ref):
            return StorageObjectRef("process", "other/key", "v1", "etag", 1, None)

    with pytest.raises(CatalogDatasetNotSupported, match="identity"):
        _head_only(WrongHead(), "raw/root/data/file")


def test_native_transport_rejects_head_download_identity_drift(tmp_path):
    headed = StorageObjectRef("raw", "raw/root/data/file", "v1", "etag-1", 1, None)

    class Drift:
        def download_file(self, _ref, _destination):
            return StorageObjectRef("raw", "raw/root/data/file", "v2", "etag-2", 1, "a" * 64)

    with pytest.raises(CatalogDatasetNotSupported, match="identity"):
        _download_ref(Drift(), headed, tmp_path / "file")


def test_direct_lerobot_validation_uses_real_sdk_and_freezes_provider_identity(
    db_session, monkeypatch, tmp_path
):
    """The new direct path materializes an SDK-valid frozen raw manifest."""
    from pathlib import Path

    from qrdf.converters.qrdf_to_lerobot import convert_dataset
    from qrdf.converters.source.examples import SyntheticTeleOpImporter
    from qrdf.converters.validate_lerobot import (
        validate_lerobot_dataset as sdk_validate,
    )

    from data.config import settings
    from data.services.native_lerobot_direct_uploads import (
        NativeLerobotDirectValidationError,
        create_direct_source,
        queue_direct_source_validation,
        run_direct_source_validation,
    )

    qrdf_dir = tmp_path / "qrdf"
    lerobot_dir = tmp_path / "lerobot"
    scratch_dir = tmp_path / "scratch"
    qrdf_dir.mkdir()
    scratch_dir.mkdir()
    monkeypatch.setattr(settings, "scratch_root", str(scratch_dir))
    monkeypatch.setattr(settings, "scratch_max_bytes", 100_000_000)
    SyntheticTeleOpImporter().import_episode(
        "/synthetic/source",
        qrdf_dir,
        num_steps=3,
        include_cameras=False,
    )
    convert_dataset(qrdf_dir, lerobot_dir, lerobot_version="v3.0")
    assert sdk_validate(lerobot_dir).ok

    payloads = {
        path.relative_to(lerobot_dir).as_posix(): path.read_bytes()
        for path in sorted(lerobot_dir.rglob("*"))
        if path.is_file() and path.relative_to(lerobot_dir).parts[0] in {"data", "meta", "videos"}
    }
    manifest = [
        {
            "path": path,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for path, payload in payloads.items()
    ]
    persisted: dict[str, bytes] = {}
    omit_info_keys: set[str] = set()

    class _Provider:
        def _payload(self, ref):
            if ref.object_key in persisted:
                return persisted[ref.object_key]
            path = ref.object_key.split("/objects/", 1)[1]
            return payloads[path]

        def head(self, ref):
            data = self._payload(ref)
            digest = hashlib.sha256(data).hexdigest()
            return StorageObjectRef(
                "export", ref.object_key, f"v-{digest[:12]}", digest, len(data), digest
            )

        def download_file(self, ref, destination):
            data = self._payload(ref)
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            if ref.object_key not in omit_info_keys:
                path.write_bytes(data)
            return self.head(ref)

        def put_worker_object(self, ref, source_path):
            data = Path(source_path).read_bytes()
            persisted[ref.object_key] = data
            return self.head(ref)

    provider = _Provider()
    monkeypatch.setattr(
        "data.services.native_lerobot_direct_uploads.get_storage_provider",
        lambda: provider,
    )

    def queue_source(name: str):
        source = create_direct_source(
            db_session,
            name=name,
            description="real SDK direct source",
            source_workspace_id=None,
            robot_type="unknown",
            dataset_id=f"sdk-{uuid4().hex[:8]}",
            objects=manifest,
            created_by_user_id=None,
        )
        for item in source.objects:
            payload = payloads[item.path]
            digest = hashlib.sha256(payload).hexdigest()
            item.status = "completed"
            item.provider_ref_json = {
                "bucket_role": "export",
                "object_key": item.object_key,
                "version_id": f"v-{digest[:12]}",
                "etag": digest,
                "size_bytes": len(payload),
                "sha256": digest,
            }
        source, job = queue_direct_source_validation(db_session, source_id=source.id, actor_id=None)
        db_session.commit()
        return source, job

    source, job = queue_source(f"LR-SDK-{uuid4().hex[:8]}")
    result = run_direct_source_validation(db_session, job)
    db_session.commit()
    db_session.refresh(source)
    assert result["status"] == "succeeded"
    assert source.catalog_dataset_version_id
    version = db_session.get(CatalogDatasetVersion, source.catalog_dataset_version_id)
    assert version is not None
    assert version.source_snapshot_json["manifest_sha256"] == source.manifest_sha256
    assert len(version.source_snapshot_json["objects"]) == len(payloads)
    assert source.marker_ref_json["bucket_role"] == "export"

    broken, broken_job = queue_source(f"LR-SDK-Bad-{uuid4().hex[:8]}")
    broken_info_key = next(
        item.object_key for item in broken.objects if item.path == "meta/info.json"
    )
    omit_info_keys.add(broken_info_key)
    with pytest.raises(NativeLerobotDirectValidationError, match="SDK validation failed"):
        run_direct_source_validation(db_session, broken_job)
    db_session.refresh(broken)
    assert broken.status == "failed"
    assert broken.error_code == "native_lerobot_direct_validation_failed"
    assert broken.catalog_dataset_version_id is None


def test_placeholder_export_cannot_register(client, db_session, admin_headers):
    asset_id = _publish_asset(db_session)
    dataset = _create_catalog_dataset(client, admin_headers)
    created = client.post(
        f"/api/v1/catalog-datasets/{dataset['id']}/versions",
        headers=admin_headers,
        json={"data_asset_ids": [asset_id]},
    )
    version_id = created.json()["data"]["id"]
    exported = client.post(
        f"/api/v1/catalog-datasets/versions/{version_id}/export",
        headers=admin_headers,
        json={"format": "qrdf_0_2"},
    )
    assert exported.status_code == 200, exported.text
    body = exported.json()["data"]
    assert body.get("oss_uri") in (None, "")

    anonymous = client.post("/api/v1/train/catalog-registrations", json={"version_id": version_id})
    assert anonymous.status_code == 401

    blocked = client.post(
        "/api/v1/train/catalog-registrations",
        headers=admin_headers,
        json={"version_id": version_id},
    )
    assert blocked.status_code == 409, blocked.text


@pytest.mark.skipif(sys.version_info >= (3, 11), reason="Python 3.10 compatibility guard")
def test_train_plane_stays_off_on_py310(client):
    health = client.get("/api/train/health")
    assert health.status_code == 404
