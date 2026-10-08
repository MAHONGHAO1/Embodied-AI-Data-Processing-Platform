from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from data.database import (
    Batch,
    ExternalOssImportScope,
    JobRun,
    NativeLerobotDataset,
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


def _object_info(
    *, size: int, etag: str, sha256: str, copy_origin: str | None = None
) -> OSSObjectInfo:
    metadata = {"x-oss-meta-sha256": sha256}
    if copy_origin is not None:
        metadata["x-oss-meta-quicdata-copy-origin"] = copy_origin
    return OSSObjectInfo(
        size=size,
        etag=etag,
        crc64=None,
        version_id=None,
        metadata=metadata,
    )


def _queued_native_copy(db_session):
    actor = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    suffix = uuid4().hex[:10]
    workspace = Workspace(name=f"native copy workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"native copy task set {suffix}")
    db_session.add(task_set)
    db_session.flush()
    scope = ExternalOssImportScope(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        bucket="test-raw",
        prefixes_json=["prod/raw/robot"],
        is_enabled=True,
        revision=1,
        created_by_user_id=actor.id,
    )
    db_session.add(scope)
    db_session.flush()

    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="native copy",
        batch_type="lerobot",
        status="processing",
        created_by_user_id=actor.id,
    )
    db_session.add(batch)
    db_session.flush()

    marker_key = f"prod/raw/robot/so101/demo/{COMPLETE_MARKER_NAME}"
    objects = (
        NativeLerobotObject(path="data/chunk-000/file-000.parquet", size=12, sha256="a" * 64),
        NativeLerobotObject(path="meta/info.json", size=3, sha256="b" * 64),
        NativeLerobotObject(path="videos/chunk-000/observation.mp4", size=42, sha256="c" * 64),
    )
    marker = NativeLerobotMarker(
        bucket=scope.bucket,
        marker_key=marker_key,
        oss_uri="oss://test-raw/prod/raw/robot/so101/demo/",
        robot_type="so101",
        dataset_id="demo",
        file_count=len(objects),
        total_size=sum(item.size for item in objects),
        completed_at=datetime(2026, 8, 27, 10, 15, 30),
        manifest_sha256="d" * 64,
        marker_sha256="e" * 64,
        objects=objects,
    )
    row = NativeLerobotDataset(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        name="native copy",
        source_oss_uri=marker.oss_uri,
        source_scope_id=scope.id,
        source_scope_revision=scope.revision,
        oss_uri=platform_native_lerobot_uri(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            batch_id=batch.id,
            robot_type=marker.robot_type,
            dataset_id=marker.dataset_id,
        ),
        robot_type=marker.robot_type,
        dataset_id=marker.dataset_id,
        file_count=marker.file_count,
        total_size=marker.total_size,
        completed_at=marker.completed_at,
        manifest_sha256=marker.manifest_sha256,
        marker_sha256=marker.marker_sha256,
        status="active",
        copy_status="queued",
        created_by_user_id=actor.id,
    )
    db_session.add(row)
    db_session.flush()
    job = JobRun(
        id=uuid4().hex,
        kind="native_lerobot_copy",
        resource_type="native_lerobot_dataset",
        resource_id=str(row.id),
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        idempotency_key=f"native-copy:{row.id}:{marker.marker_sha256}",
        queue="export",
        actor_id=actor.id,
        status="running",
        phase="running",
        lease_token="native-copy-lease",
        lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
        detail_json={
            "marker_sha256": marker.marker_sha256,
            "file_count": marker.file_count,
            "total_size": marker.total_size,
        },
    )
    db_session.add(job)
    row.last_copy_job_id = job.id
    db_session.commit()
    return row, job, marker, scope


def _install_copy_storage(monkeypatch, marker: NativeLerobotMarker):
    source_root = marker.marker_key.removesuffix(COMPLETE_MARKER_NAME)
    source_info = {
        (marker.bucket, marker.marker_key): _object_info(
            size=97,
            etag="marker-etag",
            sha256=marker.marker_sha256,
        ),
    }
    for item in marker.objects:
        source_info[(marker.bucket, f"{source_root}{item.path}")] = _object_info(
            size=item.size,
            etag=f"source-{item.sha256[:8]}",
            sha256=item.sha256,
        )
    target_info: dict[tuple[str, str], OSSObjectInfo] = {}
    copied: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_info(bucket: str, key: str):
        return source_info.get((bucket, key)) or target_info.get((bucket, key))

    def fake_copy(*args, **kwargs):
        src_bucket, src_key, dst_bucket, dst_key = args[:4]
        copied.append((args, kwargs))
        source = source_info[(src_bucket, src_key)]
        metadata = dict(kwargs["metadata"])
        target_info[(dst_bucket, dst_key)] = OSSObjectInfo(
            size=source.size,
            etag=f"target-{len(copied)}",
            crc64=None,
            version_id=None,
            metadata=metadata,
        )
        return f"oss://{dst_bucket}/{dst_key}"

    monkeypatch.setattr(oss_client, "object_info", fake_info)
    monkeypatch.setattr(oss_client, "copy_object", fake_copy)
    monkeypatch.setattr(
        oss_client,
        "multipart_copy_immutable_object",
        lambda *args, **kwargs: fake_copy(*args, **kwargs),
    )
    return source_info, target_info, copied


def test_copy_worker_copies_marker_objects_and_writes_target_marker_last(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication

    row, job, marker, _scope = _queued_native_copy(db_session)
    _source_info, _target_info, copied = _install_copy_storage(monkeypatch, marker)
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)

    result = replication.run_native_lerobot_copy(db_session, job)

    assert result["copied_files"] == marker.file_count
    assert result["verified_files"] == marker.file_count
    assert copied[-1][0][1] == marker.marker_key
    assert all(call[1]["forbid_overwrite"] is True for call in copied)
    assert all(call[1]["source_etag"] for call in copied)
    db_session.refresh(row)
    assert row.copy_status == "succeeded"
    assert row.copy_error_code == ""
    assert db_session.get(Batch, row.batch_id).status == "ready"


def test_copy_worker_rejects_scope_revision_change_without_copying(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication
    from data.services.job_runs import NonRetryableJobError

    row, job, marker, scope = _queued_native_copy(db_session)
    _source_info, _target_info, copied = _install_copy_storage(monkeypatch, marker)
    scope.revision += 1
    db_session.commit()

    with pytest.raises(NonRetryableJobError, match="source_scope_changed"):
        replication.run_native_lerobot_copy(db_session, job)

    assert copied == []
    db_session.refresh(row)
    assert row.copy_status == "failed"
    assert row.copy_error_code == "source_scope_changed"
    assert db_session.get(Batch, row.batch_id).status == "failed"


def test_copy_worker_requires_scope_to_cover_the_entire_dataset_root(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication
    from data.services.job_runs import NonRetryableJobError

    row, job, marker, scope = _queued_native_copy(db_session)
    _source_info, _target_info, copied = _install_copy_storage(monkeypatch, marker)
    # A marker-only scope may validate the completion marker, but it must never
    # authorize replication of data/, meta/, and videos/ beneath that root.
    scope.prefixes_json = [marker.marker_key]
    db_session.commit()
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)

    with pytest.raises(NonRetryableJobError, match="source_scope_changed"):
        replication.run_native_lerobot_copy(db_session, job)

    assert copied == []
    db_session.refresh(row)
    assert row.copy_status == "failed"
    assert row.copy_error_code == "source_scope_changed"


def test_copy_worker_rejects_source_metadata_mismatch_without_copying(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication
    from data.services.job_runs import NonRetryableJobError

    row, job, marker, _scope = _queued_native_copy(db_session)
    source_info, _target_info, copied = _install_copy_storage(monkeypatch, marker)
    first_key = (marker.bucket, f"prod/raw/robot/so101/demo/{marker.objects[0].path}")
    source_info[first_key] = _object_info(
        size=marker.objects[0].size, etag="changed", sha256="f" * 64
    )
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)

    with pytest.raises(NonRetryableJobError, match="source_changed"):
        replication.run_native_lerobot_copy(db_session, job)

    assert copied == []
    db_session.refresh(row)
    assert row.copy_status == "failed"
    assert row.copy_error_code == "source_changed"


def test_copy_worker_reuses_verified_object_and_rejects_target_conflict(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication
    from data.services.job_runs import NonRetryableJobError

    row, job, marker, _scope = _queued_native_copy(db_session)
    _source_info, target_info, copied = _install_copy_storage(monkeypatch, marker)
    target_bucket = oss_client.bucket_name("export")
    target_prefix = row.oss_uri.removeprefix(f"oss://{target_bucket}/")
    first = marker.objects[0]
    target_info[(target_bucket, f"{target_prefix}{first.path}")] = _object_info(
        size=first.size,
        etag="verified-target",
        sha256=first.sha256,
        copy_origin=first.sha256,
    )
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)

    result = replication.run_native_lerobot_copy(db_session, job)

    assert result["reused_files"] == 1
    assert all(call[0][1] != f"prod/raw/robot/so101/demo/{first.path}" for call in copied)

    conflict_row, conflict_job, conflict_marker, _scope = _queued_native_copy(db_session)
    _source_info, target_info, copied = _install_copy_storage(monkeypatch, conflict_marker)
    conflict_bucket = oss_client.bucket_name("export")
    conflict_prefix = conflict_row.oss_uri.removeprefix(f"oss://{conflict_bucket}/")
    target_info[(conflict_bucket, f"{conflict_prefix}{conflict_marker.objects[0].path}")] = (
        _object_info(
            size=conflict_marker.objects[0].size + 1,
            etag="conflict-target",
            sha256=conflict_marker.objects[0].sha256,
        )
    )
    monkeypatch.setattr(
        replication, "read_native_lerobot_marker", lambda **_kwargs: conflict_marker
    )

    with pytest.raises(NonRetryableJobError, match="target_conflict"):
        replication.run_native_lerobot_copy(db_session, conflict_job)

    assert copied == []
    db_session.refresh(conflict_row)
    assert conflict_row.copy_status == "failed"
    assert conflict_row.copy_error_code == "target_conflict"


def test_copy_worker_requeues_transient_provider_failure_before_retry_budget_is_exhausted(
    db_session, monkeypatch
):
    from data.services import native_lerobot_replication as replication

    row, job, marker, _scope = _queued_native_copy(db_session)
    _source_info, _target_info, _copied = _install_copy_storage(monkeypatch, marker)
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)
    monkeypatch.setattr(
        oss_client,
        "object_info",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError()),
    )

    with pytest.raises(replication.NativeLerobotCopyTransientError):
        replication.run_native_lerobot_copy(db_session, job)

    db_session.refresh(row)
    assert row.copy_status == "queued"
    assert row.copy_error_code == ""
    assert db_session.get(Batch, row.batch_id).status == "processing"


def test_copy_worker_marks_terminal_provider_failure_after_retry_budget(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication
    from data.services.job_runs import MAX_RETRIES

    row, job, marker, _scope = _queued_native_copy(db_session)
    _source_info, _target_info, _copied = _install_copy_storage(monkeypatch, marker)
    job.retry_count = MAX_RETRIES
    db_session.commit()
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)
    monkeypatch.setattr(
        oss_client,
        "object_info",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError()),
    )

    with pytest.raises(replication.NativeLerobotCopyTransientError):
        replication.run_native_lerobot_copy(db_session, job)

    db_session.refresh(row)
    assert row.copy_status == "failed"
    assert row.copy_error_code == "copy_unavailable"
    assert db_session.get(Batch, row.batch_id).status == "failed"


def test_copy_worker_requires_source_etag_before_any_conditional_copy(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication
    from data.services.job_runs import NonRetryableJobError

    row, job, marker, _scope = _queued_native_copy(db_session)
    source_info, _target_info, copied = _install_copy_storage(monkeypatch, marker)
    source_key = (marker.bucket, f"prod/raw/robot/so101/demo/{marker.objects[0].path}")
    source_info[source_key] = _object_info(
        size=marker.objects[0].size,
        etag="",
        sha256=marker.objects[0].sha256,
    )
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)

    with pytest.raises(NonRetryableJobError, match="source_identity_unavailable"):
        replication.run_native_lerobot_copy(db_session, job)

    assert copied == []
    db_session.refresh(row)
    assert row.copy_error_code == "source_identity_unavailable"


def test_copy_worker_converts_provider_access_denial_to_controlled_failure(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication
    from data.services.job_runs import NonRetryableJobError

    class AccessDenied(Exception):
        status = 403
        code = "AccessDenied"

    row, job, marker, _scope = _queued_native_copy(db_session)
    _source_info, _target_info, copied = _install_copy_storage(monkeypatch, marker)
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)
    monkeypatch.setattr(
        oss_client, "object_info", lambda *_args, **_kwargs: (_ for _ in ()).throw(AccessDenied())
    )

    with pytest.raises(NonRetryableJobError, match="copy_access_denied"):
        replication.run_native_lerobot_copy(db_session, job)

    assert copied == []
    db_session.refresh(row)
    assert row.copy_error_code == "copy_access_denied"


def test_manual_copy_retry_updates_native_projection_in_the_same_transaction(db_session):
    from data.services import native_lerobot_replication as replication

    row, job, _marker, _scope = _queued_native_copy(db_session)
    row.copy_status = "failed"
    row.copy_error_code = "source_changed"
    job.status = "failed"
    job.phase = "failed"
    job.lease_token = ""
    db_session.commit()

    retry = replication.retry_native_lerobot_copy(
        db_session, dataset_id=row.id, actor_id=job.actor_id
    )

    assert retry.created is True
    assert retry.job.id != job.id
    assert retry.job.kind == "native_lerobot_copy"
    assert retry.job.status == "queued"
    assert row.last_copy_job_id == retry.job.id
    assert row.copy_status == "queued"
    assert row.copy_error_code == ""
    assert "source_oss_uri" not in retry.job.detail_json

    db_session.rollback()
    db_session.expire_all()
    restored = db_session.get(NativeLerobotDataset, row.id)
    assert restored.copy_status == "failed"
    assert restored.last_copy_job_id == job.id
    assert db_session.get(JobRun, retry.job.id) is None


def test_source_reauthorization_requires_same_snapshot_and_queues_new_copy(db_session, monkeypatch):
    from data.services import native_lerobot_replication as replication
    from data.services.native_lerobot_datasets import _encode_candidate_token

    row, job, marker, scope = _queued_native_copy(db_session)
    row.copy_status = "failed"
    row.copy_error_code = "source_scope_changed"
    job.status = "failed"
    job.phase = "failed"
    job.lease_token = ""
    scope.revision += 1
    db_session.commit()
    token = _encode_candidate_token(
        workspace_id=row.workspace_id,
        task_set_id=row.task_set_id,
        actor_id=job.actor_id,
        scope=scope,
        marker=marker,
    )
    monkeypatch.setattr(replication, "read_native_lerobot_marker", lambda **_kwargs: marker)

    retry = replication.reauthorize_native_lerobot_source(
        db_session,
        dataset_id=row.id,
        candidate_token=token,
        actor_id=job.actor_id,
    )

    assert retry.created is True
    assert retry.job.status == "queued"
    assert row.source_scope_id == scope.id
    assert row.source_scope_revision == scope.revision
    assert row.last_copy_job_id == retry.job.id
    assert row.copy_status == "queued"
    assert row.copy_error_code == ""
