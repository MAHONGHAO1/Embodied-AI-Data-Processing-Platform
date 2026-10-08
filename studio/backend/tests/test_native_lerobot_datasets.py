from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError, IntegrityError

from data.database import (
    Batch,
    Episode,
    ExternalOssImportScope,
    ImportSession,
    JobRun,
    NativeLerobotDataset,
    NativeLerobotImportSelection,
    NativeLerobotImportSession,
    NativeLerobotScanCandidate,
    NativeLerobotScanSnapshot,
    TaskSet,
    User,
    WorkItem,
    Workspace,
)
from data.infra import oss_client
from data.infra.oss_client import OSSDirectoryPage, OSSObjectInfo


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _marker(
    *, robot_type: str = "so101", dataset_id: str = "demo"
) -> tuple[dict[str, object], str]:
    objects = [
        {"path": "data/chunk-000/file-000.parquet", "size": 12, "sha256": "a" * 64},
        {"path": "meta/info.json", "size": 3, "sha256": "b" * 64},
        {"path": "videos/chunk-000/observation.mp4", "size": 42, "sha256": "c" * 64},
    ]
    manifest = _canonical_json(
        {"robot_type": robot_type, "dataset_id": dataset_id, "objects": objects}
    )
    marker = {
        "schema_version": 1,
        "robot_type": robot_type,
        "dataset_id": dataset_id,
        "objects": objects,
        "file_count": len(objects),
        "total_size": sum(int(item["size"]) for item in objects),
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "completed_at": "2026-08-27T10:15:30Z",
    }
    return marker, hashlib.sha256(_canonical_json(marker)).hexdigest()


def _scope_context(db_session):
    actor = db_session.query(User).filter(User.email == "admin@quicdata.com").one()
    suffix = uuid4().hex[:10]
    workspace = Workspace(name=f"native lerobot workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"native lerobot task set {suffix}")
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
    db_session.commit()
    return actor, workspace, task_set, scope


def _native_dataset(db_session, *, batch_id: int, marker_sha256: str):
    workspace_id, task_set_id = (
        db_session.query(Batch.workspace_id, Batch.task_set_id).filter(Batch.id == batch_id).one()
    )
    return NativeLerobotDataset(
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        batch_id=batch_id,
        name=f"native dataset {marker_sha256[:8]}",
        source_oss_uri=f"oss://test-raw/native/{marker_sha256}/",
        oss_uri=f"oss://platform/native/{marker_sha256}/",
        robot_type="so101",
        dataset_id=f"dataset-{marker_sha256[:8]}",
        file_count=1,
        total_size=1,
        completed_at=datetime(2026, 8, 28, 10, 15, 30),
        manifest_sha256="c" * 64,
        marker_sha256=marker_sha256,
    )


def test_lerobot_batch_allows_two_native_datasets_and_partial_failure(db_session):
    _actor, workspace, task_set, _scope = _scope_context(db_session)
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="multiple native datasets",
        batch_type="lerobot",
        status="processing",
    )
    db_session.add(batch)
    db_session.flush()
    first = _native_dataset(db_session, batch_id=batch.id, marker_sha256="a" * 64)
    second = _native_dataset(db_session, batch_id=batch.id, marker_sha256="b" * 64)
    db_session.add_all([first, second])
    db_session.commit()

    db_session.refresh(batch)
    assert {row.id for row in batch.native_lerobot_datasets} == {first.id, second.id}
    assert first.import_session_id is None
    batch.status = "partial_failed"
    db_session.commit()


def test_only_one_succeeded_current_snapshot_exists_per_workspace_and_task_set(db_session):
    _actor, workspace, task_set, _scope = _scope_context(db_session)
    first = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        status="succeeded",
        is_current=True,
    )
    second = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        status="succeeded",
        is_current=True,
    )
    db_session.add(first)
    db_session.commit()
    db_session.add(second)

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_lerobot_import_selection_is_unique_per_session_and_candidate(db_session):
    _actor, workspace, task_set, _scope = _scope_context(db_session)
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="selection batch",
        batch_type="lerobot",
        status="created",
    )
    snapshot = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        status="succeeded",
        is_current=True,
    )
    session = NativeLerobotImportSession(
        id=str(uuid4()),
        batch=batch,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        scan_snapshot_id=snapshot.id,
    )
    candidate = NativeLerobotScanCandidate(
        id=str(uuid4()),
        scan_snapshot_id=snapshot.id,
        robot_type="so101",
        dataset_id="selection-test",
        file_count=1,
        total_size=1,
        marker_sha256="d" * 64,
    )
    db_session.add_all([snapshot, session, candidate])
    db_session.flush()
    db_session.add_all(
        [
            NativeLerobotImportSelection(
                id=str(uuid4()),
                import_session_id=session.id,
                scan_candidate_id=candidate.id,
                result_status="registered",
            ),
            NativeLerobotImportSelection(
                id=str(uuid4()),
                import_session_id=session.id,
                scan_candidate_id=candidate.id,
                result_status="skipped_duplicate",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize("batch_type", ("ego", "teleop"))
def test_native_lerobot_children_reject_non_lerobot_batches(db_session, batch_type):
    _actor, workspace, task_set, _scope = _scope_context(db_session)
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"{batch_type} batch",
        batch_type=batch_type,
        status="created",
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(_native_dataset(db_session, batch_id=batch.id, marker_sha256="e" * 64))

    with pytest.raises(DBAPIError, match="native_lerobot_dataset_batch_scope"):
        db_session.commit()
    db_session.rollback()


def test_native_lerobot_children_reject_cross_workspace_batch_and_snapshot_scopes(db_session):
    _actor, first_workspace, first_task_set, _first_scope = _scope_context(db_session)
    _actor, second_workspace, second_task_set, _second_scope = _scope_context(db_session)
    first_batch = Batch(
        workspace_id=first_workspace.id,
        task_set_id=first_task_set.id,
        name="first native batch",
        batch_type="lerobot",
        status="created",
    )
    db_session.add(first_batch)
    db_session.flush()
    cross_scope_dataset = _native_dataset(
        db_session,
        batch_id=first_batch.id,
        marker_sha256="f" * 64,
    )
    cross_scope_dataset.workspace_id = second_workspace.id
    cross_scope_dataset.task_set_id = second_task_set.id
    db_session.add(cross_scope_dataset)
    with pytest.raises(DBAPIError, match="native_lerobot_dataset_batch_scope"):
        db_session.commit()
    db_session.rollback()

    snapshot = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=second_workspace.id,
        task_set_id=second_task_set.id,
        status="succeeded",
        is_current=True,
    )
    db_session.add_all([first_batch, snapshot])
    db_session.flush()
    db_session.add(
        NativeLerobotImportSession(
            id=str(uuid4()),
            batch_id=first_batch.id,
            workspace_id=first_workspace.id,
            task_set_id=first_task_set.id,
            scan_snapshot_id=snapshot.id,
        )
    )
    with pytest.raises(DBAPIError, match="native_lerobot_import_session_scope"):
        db_session.commit()
    db_session.rollback()


def test_native_lerobot_selection_rejects_cross_snapshot_and_dataset_identity_mismatch(db_session):
    _actor, workspace, task_set, _scope = _scope_context(db_session)
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="selection invariant batch",
        batch_type="lerobot",
        status="created",
    )
    first_snapshot = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        status="succeeded",
        is_current=True,
    )
    second_snapshot = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        status="failed",
    )
    session = NativeLerobotImportSession(
        id=str(uuid4()),
        batch=batch,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        scan_snapshot_id=first_snapshot.id,
    )
    cross_snapshot_candidate = NativeLerobotScanCandidate(
        id=str(uuid4()),
        scan_snapshot_id=second_snapshot.id,
        robot_type="so101",
        dataset_id="cross-snapshot",
        file_count=1,
        total_size=1,
        marker_sha256="a" * 64,
    )
    db_session.add_all([first_snapshot, second_snapshot, session, cross_snapshot_candidate])
    db_session.flush()
    db_session.add(
        NativeLerobotImportSelection(
            id=str(uuid4()),
            import_session_id=session.id,
            scan_candidate_id=cross_snapshot_candidate.id,
            result_status="skipped_stale",
        )
    )
    with pytest.raises(DBAPIError, match="native_lerobot_import_selection_scope"):
        db_session.commit()
    db_session.rollback()

    matching_candidate = NativeLerobotScanCandidate(
        id=str(uuid4()),
        scan_snapshot_id=first_snapshot.id,
        robot_type="so101",
        dataset_id="expected-id",
        file_count=1,
        total_size=1,
        marker_sha256="b" * 64,
    )
    db_session.add_all([batch, first_snapshot, session, matching_candidate])
    db_session.flush()
    mismatched_dataset = _native_dataset(db_session, batch_id=batch.id, marker_sha256="c" * 64)
    mismatched_dataset.import_session_id = session.id
    db_session.add(mismatched_dataset)
    db_session.flush()
    db_session.add(
        NativeLerobotImportSelection(
            id=str(uuid4()),
            import_session_id=session.id,
            scan_candidate_id=matching_candidate.id,
            native_lerobot_dataset_id=mismatched_dataset.id,
            result_status="registered",
        )
    )
    with pytest.raises(DBAPIError, match="native_lerobot_import_selection_dataset_identity"):
        db_session.commit()
    db_session.rollback()


def _persist_valid_lerobot_selection_chain(db_session):
    _actor, workspace, task_set, scope = _scope_context(db_session)
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="valid invariant batch",
        batch_type="lerobot",
        status="created",
    )
    snapshot = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        status="succeeded",
        is_current=True,
    )
    session = NativeLerobotImportSession(
        id=str(uuid4()),
        batch=batch,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        scan_snapshot_id=snapshot.id,
    )
    db_session.add_all([batch, snapshot, session])
    db_session.flush()
    dataset = _native_dataset(db_session, batch_id=batch.id, marker_sha256="1" * 64)
    dataset.import_session_id = session.id
    candidate = NativeLerobotScanCandidate(
        id=str(uuid4()),
        scan_snapshot_id=snapshot.id,
        source_scope_id=scope.id,
        robot_type=dataset.robot_type,
        dataset_id=dataset.dataset_id,
        file_count=dataset.file_count,
        total_size=dataset.total_size,
        marker_sha256=dataset.marker_sha256,
    )
    db_session.add_all([dataset, candidate])
    db_session.flush()
    db_session.add(
        NativeLerobotImportSelection(
            id=str(uuid4()),
            import_session_id=session.id,
            scan_candidate_id=candidate.id,
            native_lerobot_dataset_id=dataset.id,
            result_status="registered",
        )
    )
    db_session.commit()
    return workspace, task_set, batch, session, snapshot, candidate, dataset


def test_lerobot_parent_updates_cannot_invalidate_existing_children(db_session):
    workspace, task_set, batch, _session, _snapshot, _candidate, _dataset = (
        _persist_valid_lerobot_selection_chain(db_session)
    )
    batch.batch_type = "ego"

    with pytest.raises(DBAPIError, match="native_lerobot_batch_parent_scope"):
        db_session.commit()
    db_session.rollback()

    batch.workspace_id = workspace.id + 10_000
    with pytest.raises(DBAPIError, match="native_lerobot_batch_parent_scope"):
        db_session.commit()
    db_session.rollback()


def test_lerobot_candidate_and_dataset_updates_cannot_invalidate_selection(db_session):
    _workspace, _task_set, _batch, _session, _snapshot, candidate, _dataset = (
        _persist_valid_lerobot_selection_chain(db_session)
    )
    candidate.marker_sha256 = "2" * 64

    with pytest.raises(DBAPIError, match="native_lerobot_candidate_parent_scope"):
        db_session.commit()
    db_session.rollback()

    _workspace, _task_set, _batch, _session, _snapshot, _candidate, dataset = (
        _persist_valid_lerobot_selection_chain(db_session)
    )
    dataset.dataset_id = "mutated-source-identity"

    with pytest.raises(DBAPIError, match="native_lerobot_dataset_parent_scope"):
        db_session.commit()
    db_session.rollback()


def test_native_lerobot_dataset_rejects_same_scope_session_from_another_batch(db_session):
    _actor, workspace, task_set, _scope = _scope_context(db_session)
    first_batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="first batch",
        batch_type="lerobot",
        status="created",
    )
    second_batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="second batch",
        batch_type="lerobot",
        status="created",
    )
    session = NativeLerobotImportSession(
        id=str(uuid4()),
        batch=first_batch,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
    )
    db_session.add_all([first_batch, second_batch, session])
    db_session.flush()
    dataset = _native_dataset(db_session, batch_id=second_batch.id, marker_sha256="3" * 64)
    dataset.import_session_id = session.id
    db_session.add(dataset)

    with pytest.raises(DBAPIError, match="native_lerobot_dataset_import_session_batch"):
        db_session.commit()
    db_session.rollback()


def test_native_lerobot_candidate_rejects_source_scope_from_another_workspace(db_session):
    _actor, workspace, task_set, _scope = _scope_context(db_session)
    _actor, _other_workspace, _other_task_set, other_scope = _scope_context(db_session)
    snapshot = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        status="succeeded",
        is_current=True,
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.add(
        NativeLerobotScanCandidate(
            id=str(uuid4()),
            scan_snapshot_id=snapshot.id,
            source_scope_id=other_scope.id,
            robot_type="so101",
            dataset_id="cross-scope-candidate",
            file_count=1,
            total_size=1,
            marker_sha256="4" * 64,
        )
    )

    with pytest.raises(DBAPIError, match="native_lerobot_candidate_source_scope"):
        db_session.commit()
    db_session.rollback()


def test_selection_rejects_legacy_dataset_from_another_same_scope_batch(db_session):
    _actor, workspace, task_set, scope = _scope_context(db_session)
    first_batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="selection batch",
        batch_type="lerobot",
        status="created",
    )
    second_batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="legacy dataset batch",
        batch_type="lerobot",
        status="created",
    )
    snapshot = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        status="succeeded",
        is_current=True,
    )
    session = NativeLerobotImportSession(
        id=str(uuid4()),
        batch=first_batch,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        scan_snapshot_id=snapshot.id,
    )
    db_session.add_all([first_batch, second_batch, snapshot, session])
    db_session.flush()
    legacy_dataset = _native_dataset(db_session, batch_id=second_batch.id, marker_sha256="5" * 64)
    candidate = NativeLerobotScanCandidate(
        id=str(uuid4()),
        scan_snapshot_id=snapshot.id,
        source_scope_id=scope.id,
        robot_type=legacy_dataset.robot_type,
        dataset_id=legacy_dataset.dataset_id,
        file_count=legacy_dataset.file_count,
        total_size=legacy_dataset.total_size,
        marker_sha256=legacy_dataset.marker_sha256,
    )
    db_session.add_all([legacy_dataset, candidate])
    db_session.flush()
    db_session.add(
        NativeLerobotImportSelection(
            id=str(uuid4()),
            import_session_id=session.id,
            scan_candidate_id=candidate.id,
            native_lerobot_dataset_id=legacy_dataset.id,
            result_status="registered",
        )
    )

    with pytest.raises(DBAPIError, match="native_lerobot_import_selection_dataset_batch"):
        db_session.commit()
    db_session.rollback()


def test_session_parent_updates_cannot_invalidate_existing_selection_or_dataset(db_session):
    workspace, task_set, _batch, session, _snapshot, _candidate, _dataset = (
        _persist_valid_lerobot_selection_chain(db_session)
    )
    replacement_snapshot = NativeLerobotScanSnapshot(
        id=str(uuid4()), workspace_id=workspace.id, task_set_id=task_set.id, status="failed"
    )
    db_session.add(replacement_snapshot)
    db_session.commit()
    session.scan_snapshot_id = replacement_snapshot.id

    with pytest.raises(DBAPIError, match="native_lerobot_import_session_parent_scope"):
        db_session.commit()
    db_session.rollback()

    workspace, task_set, _batch, session, _snapshot, _candidate, _dataset = (
        _persist_valid_lerobot_selection_chain(db_session)
    )
    replacement_batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="replacement batch",
        batch_type="lerobot",
        status="created",
    )
    db_session.add(replacement_batch)
    db_session.commit()
    session.batch_id = replacement_batch.id

    with pytest.raises(DBAPIError, match="native_lerobot_import_session_parent_scope"):
        db_session.commit()
    db_session.rollback()


def test_snapshot_and_source_scope_parent_updates_cannot_invalidate_candidate(db_session):
    workspace, task_set, _batch, _session, snapshot, _candidate, _dataset = (
        _persist_valid_lerobot_selection_chain(db_session)
    )
    _actor, other_workspace, other_task_set, _other_scope = _scope_context(db_session)
    snapshot.workspace_id = other_workspace.id
    snapshot.task_set_id = other_task_set.id

    with pytest.raises(DBAPIError, match="native_lerobot_scan_snapshot_parent_scope"):
        db_session.commit()
    db_session.rollback()

    _workspace, _task_set, _batch, _session, _snapshot, candidate, _dataset = (
        _persist_valid_lerobot_selection_chain(db_session)
    )
    scope = db_session.get(ExternalOssImportScope, candidate.source_scope_id)
    scope.workspace_id = other_workspace.id
    scope.task_set_id = other_task_set.id

    with pytest.raises(DBAPIError, match="external_oss_import_scope_parent_scope"):
        db_session.commit()
    db_session.rollback()


def test_snapshot_and_session_reject_task_sets_from_another_workspace(db_session):
    _actor, first_workspace, _first_task_set, _first_scope = _scope_context(db_session)
    _actor, _second_workspace, second_task_set, _second_scope = _scope_context(db_session)
    db_session.add(
        NativeLerobotScanSnapshot(
            id=str(uuid4()),
            workspace_id=first_workspace.id,
            task_set_id=second_task_set.id,
            status="succeeded",
            is_current=True,
        )
    )
    with pytest.raises(DBAPIError, match="native_lerobot_task_set_scope"):
        db_session.commit()
    db_session.rollback()

    batch = Batch(
        workspace_id=first_workspace.id,
        task_set_id=second_task_set.id,
        name="noncanonical session batch",
        batch_type="lerobot",
        status="created",
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(
        NativeLerobotImportSession(
            id=str(uuid4()),
            batch_id=batch.id,
            workspace_id=first_workspace.id,
            task_set_id=second_task_set.id,
        )
    )
    with pytest.raises(DBAPIError, match="native_lerobot_task_set_scope"):
        db_session.commit()
    db_session.rollback()


def test_task_set_workspace_update_rejects_referenced_lerobot_scope(db_session):
    _workspace, task_set, _batch, _session, _snapshot, candidate, _dataset = (
        _persist_valid_lerobot_selection_chain(db_session)
    )
    _actor, other_workspace, _other_task_set, _other_scope = _scope_context(db_session)
    assert candidate.source_scope_id is not None
    task_set.workspace_id = other_workspace.id

    with pytest.raises(DBAPIError, match="native_lerobot_task_set_parent_scope"):
        db_session.commit()
    db_session.rollback()


def _install_marker_listing(monkeypatch, marker: dict[str, object], marker_sha256: str):
    from data.infra import oss_client

    root = "prod/raw/robot/"
    robot_root = f"{root}{marker['robot_type']}/"
    dataset_root = f"{robot_root}{marker['dataset_id']}/"
    marker_key = f"{dataset_root}complete.json"
    calls: list[tuple[str, str]] = []

    def fake_directories(bucket: str, prefix: str, *, continuation_token, max_keys):
        calls.append((bucket, prefix))
        assert continuation_token is None
        assert 1 <= max_keys <= 1_000
        if prefix == root:
            return OSSDirectoryPage(prefixes=(robot_root,), next_token=None)
        if prefix == robot_root:
            return OSSDirectoryPage(prefixes=(dataset_root,), next_token=None)
        raise AssertionError(f"payload must not be listed: {prefix}")

    monkeypatch.setattr(oss_client, "list_prefix_directory_page", fake_directories)
    monkeypatch.setattr(
        oss_client,
        "object_info",
        lambda bucket, key: (
            OSSObjectInfo(
                size=len(_canonical_json(marker)),
                etag="marker-etag",
                crc64=None,
                version_id=None,
                metadata={"x-oss-meta-sha256": marker_sha256},
            )
            if (bucket, key) == ("test-raw", marker_key)
            else None
        ),
    )
    monkeypatch.setattr(
        oss_client,
        "read_json_object",
        lambda bucket, key, *, max_bytes, if_match=None: (
            marker
            if (bucket, key) == ("test-raw", marker_key)
            else (_ for _ in ()).throw(AssertionError("unexpected object read"))
        ),
    )
    return calls


def test_discovery_reads_only_complete_marker_and_returns_safe_candidate(db_session, monkeypatch):
    from data.services.native_lerobot_datasets import discover_native_lerobot_candidates

    actor, workspace, task_set, _scope = _scope_context(db_session)
    marker, marker_sha256 = _marker()
    calls = _install_marker_listing(monkeypatch, marker, marker_sha256)

    candidates = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.robot_type == "so101"
    assert candidate.dataset_id == "demo"
    assert candidate.file_count == 3
    assert candidate.total_size == 57
    assert candidate.completed_at == datetime(2026, 8, 27, 10, 15, 30, tzinfo=timezone.utc)
    assert candidate.oss_uri == "oss://test-raw/prod/raw/robot/so101/demo/"
    assert candidate.marker_sha256 == marker_sha256
    assert candidate.token
    assert calls == [
        ("test-raw", "prod/raw/robot/"),
        ("test-raw", "prod/raw/robot/so101/"),
    ]


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda marker: marker.__setitem__("schema_version", 2), "marker_schema_unsupported"),
        (lambda marker: marker.__setitem__("file_count", 99), "marker_summary_invalid"),
        (
            lambda marker: marker["objects"].__setitem__(
                1,
                {"path": "data/../escape.parquet", "size": 3, "sha256": "b" * 64},
            ),
            "marker_objects_invalid",
        ),
    ],
)
def test_invalid_marker_is_not_a_candidate(db_session, monkeypatch, mutate, expected_code):
    from data.services.native_lerobot_datasets import (
        NativeLerobotMarkerError,
        read_native_lerobot_marker,
    )

    marker, _marker_sha256 = _marker()
    mutate(marker)
    marker_sha256 = hashlib.sha256(_canonical_json(marker)).hexdigest()
    _install_marker_listing(monkeypatch, marker, marker_sha256)

    with pytest.raises(NativeLerobotMarkerError) as raised:
        read_native_lerobot_marker(
            bucket="test-raw",
            marker_key="prod/raw/robot/so101/demo/complete.json",
        )

    assert raised.value.code == expected_code


def test_marker_rejects_total_size_outside_postgresql_bigint(db_session, monkeypatch):
    from data.services.native_lerobot_datasets import (
        NativeLerobotMarkerError,
        read_native_lerobot_marker,
    )

    marker, _marker_sha256 = _marker()
    oversized_object = {
        "path": "data/chunk-000/file-000.parquet",
        "size": 2**63,
        "sha256": "a" * 64,
    }
    marker["objects"] = [oversized_object]
    marker["file_count"] = 1
    marker["total_size"] = oversized_object["size"]
    marker["manifest_sha256"] = hashlib.sha256(
        _canonical_json(
            {
                "robot_type": marker["robot_type"],
                "dataset_id": marker["dataset_id"],
                "objects": marker["objects"],
            }
        )
    ).hexdigest()
    marker_sha256 = hashlib.sha256(_canonical_json(marker)).hexdigest()
    _install_marker_listing(monkeypatch, marker, marker_sha256)

    with pytest.raises(NativeLerobotMarkerError) as raised:
        read_native_lerobot_marker(
            bucket="test-raw",
            marker_key="prod/raw/robot/so101/demo/complete.json",
        )

    assert raised.value.code == "marker_summary_invalid"


def test_registering_candidate_queues_platform_copy_and_no_qrdf_workflow_rows(
    db_session, monkeypatch
):
    from data.database import NativeLerobotDataset
    from data.services.native_lerobot_datasets import (
        discover_native_lerobot_candidates,
        register_native_lerobot_dataset,
    )

    actor, workspace, task_set, _scope = _scope_context(db_session)
    marker, marker_sha256 = _marker()
    _install_marker_listing(monkeypatch, marker, marker_sha256)
    candidate = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]

    batch, native_dataset, job = register_native_lerobot_dataset(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="SO101 kitchen demo",
        candidate_token=candidate.token,
        actor_id=actor.id,
    )
    db_session.commit()

    assert batch.batch_type == "lerobot"
    assert batch.status == "processing"
    assert native_dataset.batch_id == batch.id
    assert native_dataset.source_oss_uri == "oss://test-raw/prod/raw/robot/so101/demo/"
    assert native_dataset.oss_uri == (
        f"oss://{oss_client.bucket_name('export')}/exports/v1/native-lerobot/"
        f"workspaces/{workspace.id}/task-sets/{task_set.id}/batches/{batch.id}/so101/demo/"
    )
    assert native_dataset.status == "active"
    assert native_dataset.copy_status == "queued"
    assert native_dataset.source_scope_id == _scope.id
    assert native_dataset.source_scope_revision == _scope.revision
    assert native_dataset.last_copy_job_id == job.id
    assert job.kind == "native_lerobot_copy"
    assert job.resource_type == "native_lerobot_dataset"
    assert job.resource_id == str(native_dataset.id)
    assert job.queue == "export"
    assert (
        db_session.query(NativeLerobotDataset).filter_by(id=native_dataset.id).one().batch_id
        == batch.id
    )
    assert db_session.query(ImportSession).filter_by(batch_id=batch.id).count() == 0
    assert db_session.query(Episode).filter_by(batch_id=batch.id).count() == 0
    assert db_session.query(WorkItem).filter_by(batch_id=batch.id).count() == 0


def test_registering_candidate_rolls_back_batch_copy_record_and_job_together(
    db_session, monkeypatch
):
    from data.database import NativeLerobotDataset
    from data.services.native_lerobot_datasets import (
        discover_native_lerobot_candidates,
        register_native_lerobot_dataset,
    )

    actor, workspace, task_set, _scope = _scope_context(db_session)
    marker, marker_sha256 = _marker()
    _install_marker_listing(monkeypatch, marker, marker_sha256)
    candidate = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]

    batch, native_dataset, job = register_native_lerobot_dataset(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="rolled back platform copy",
        candidate_token=candidate.token,
        actor_id=actor.id,
    )
    batch_id = batch.id
    native_dataset_id = native_dataset.id
    job_id = job.id
    db_session.rollback()

    assert db_session.get(Batch, batch_id) is None
    assert db_session.get(NativeLerobotDataset, native_dataset_id) is None
    assert db_session.get(JobRun, job_id) is None


def test_validated_marker_preserves_immutable_sorted_object_snapshot(db_session, monkeypatch):
    from data.services.native_lerobot_datasets import (
        NativeLerobotObject,
        read_native_lerobot_marker,
    )

    marker, marker_sha256 = _marker()
    _install_marker_listing(monkeypatch, marker, marker_sha256)

    parsed = read_native_lerobot_marker(
        bucket="test-raw",
        marker_key="prod/raw/robot/so101/demo/complete.json",
    )

    assert parsed.objects == (
        NativeLerobotObject(path="data/chunk-000/file-000.parquet", size=12, sha256="a" * 64),
        NativeLerobotObject(path="meta/info.json", size=3, sha256="b" * 64),
        NativeLerobotObject(path="videos/chunk-000/observation.mp4", size=42, sha256="c" * 64),
    )


def test_registration_locks_scope_before_final_marker_revalidation(db_session, monkeypatch):
    from data.services.native_lerobot_datasets import (
        discover_native_lerobot_candidates,
        register_native_lerobot_dataset,
    )

    actor, workspace, task_set, _scope = _scope_context(db_session)
    marker, marker_sha256 = _marker()
    _install_marker_listing(monkeypatch, marker, marker_sha256)
    candidate = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]
    statements: list[str] = []

    def capture_scope_query(conn, cursor, statement, parameters, context, executemany):
        if "external_oss_import_scopes" in statement:
            statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", capture_scope_query)
    try:
        register_native_lerobot_dataset(
            db_session,
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            name="locked scope registration",
            candidate_token=candidate.token,
            actor_id=actor.id,
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture_scope_query)
        db_session.rollback()

    assert any("FOR UPDATE" in statement.upper() for statement in statements)


def test_registration_rejects_tampered_candidate_token_without_writing(db_session, monkeypatch):
    from data.database import NativeLerobotDataset
    from data.services.native_lerobot_datasets import (
        NativeLerobotCandidateTokenError,
        discover_native_lerobot_candidates,
        register_native_lerobot_dataset,
    )

    actor, workspace, task_set, _scope = _scope_context(db_session)
    marker, marker_sha256 = _marker()
    _install_marker_listing(monkeypatch, marker, marker_sha256)
    candidate = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]
    before_batches = db_session.query(Batch).count()

    with pytest.raises(NativeLerobotCandidateTokenError):
        register_native_lerobot_dataset(
            db_session,
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            name="tampered",
            candidate_token=f"{candidate.token}x",
            actor_id=actor.id,
        )

    assert db_session.query(Batch).count() == before_batches
    assert (
        db_session.query(NativeLerobotDataset)
        .filter(NativeLerobotDataset.workspace_id == workspace.id)
        .count()
        == 0
    )


@pytest.mark.parametrize("scope_change", ("disabled", "revised"))
def test_registration_rejects_revoked_candidate_scope(db_session, monkeypatch, scope_change):
    from data.database import NativeLerobotDataset
    from data.services.native_lerobot_datasets import (
        NativeLerobotCandidateTokenError,
        discover_native_lerobot_candidates,
        register_native_lerobot_dataset,
    )

    actor, workspace, task_set, scope = _scope_context(db_session)
    marker, marker_sha256 = _marker()
    _install_marker_listing(monkeypatch, marker, marker_sha256)
    candidate = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]
    if scope_change == "disabled":
        scope.is_enabled = False
    else:
        scope.revision += 1
    db_session.commit()

    with pytest.raises(NativeLerobotCandidateTokenError, match="candidate scope is unavailable"):
        register_native_lerobot_dataset(
            db_session,
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            name="revoked scope registration",
            candidate_token=candidate.token,
            actor_id=actor.id,
        )

    assert (
        db_session.query(NativeLerobotDataset)
        .filter(NativeLerobotDataset.workspace_id == workspace.id)
        .count()
        == 0
    )


def test_registration_rejects_expired_candidate_token(db_session, monkeypatch):
    import data.services.native_lerobot_datasets as native_lerobot_datasets

    actor, workspace, task_set, _scope = _scope_context(db_session)
    marker, marker_sha256 = _marker()
    _install_marker_listing(monkeypatch, marker, marker_sha256)
    candidate = native_lerobot_datasets.discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]
    native_lerobot_datasets.redis_service.client.delete(
        native_lerobot_datasets._candidate_reference_key(candidate.token)
    )

    with pytest.raises(
        native_lerobot_datasets.NativeLerobotCandidateTokenError,
        match="candidate token has expired",
    ):
        native_lerobot_datasets.register_native_lerobot_dataset(
            db_session,
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            name="expired token registration",
            candidate_token=candidate.token,
            actor_id=actor.id,
        )


def test_candidate_reference_is_actor_bound_and_single_use(db_session, monkeypatch):
    from data.services.native_lerobot_datasets import (
        NativeLerobotCandidateTokenError,
        _decode_candidate_token,
        discover_native_lerobot_candidates,
    )

    actor, workspace, task_set, _scope = _scope_context(db_session)
    other_actor = User(
        email=f"candidate-other-{uuid4().hex}@example.test",
        password_hash="not-used",
        role="operator",
        is_active=True,
    )
    db_session.add(other_actor)
    db_session.flush()
    marker, marker_sha256 = _marker()
    _install_marker_listing(monkeypatch, marker, marker_sha256)
    candidate = discover_native_lerobot_candidates(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )[0]

    with pytest.raises(NativeLerobotCandidateTokenError, match="candidate token is invalid"):
        _decode_candidate_token(candidate.token, actor_id=other_actor.id)
    claims = _decode_candidate_token(candidate.token, actor_id=actor.id)
    assert claims["workspace_id"] == workspace.id
    assert claims["task_set_id"] == task_set.id
    with pytest.raises(NativeLerobotCandidateTokenError, match="candidate token has expired"):
        _decode_candidate_token(candidate.token, actor_id=actor.id)
