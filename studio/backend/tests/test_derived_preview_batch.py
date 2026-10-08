"""Security and recovery contracts for derived preview batch jobs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from data.database import (
    ArtifactOperation,
    Batch,
    Episode,
    EpisodeArtifact,
    JobRun,
    RealtimeEvent,
    TaskSet,
    User,
    WorkItem,
    Workspace,
)
from data.integrations.qrdf.preview_export import PreviewSegmentFacts
from data.services import derived_preview_batch


def _batch_preview_context(db_session):
    suffix = uuid4().hex
    actor = db_session.query(User).order_by(User.id).first()
    workspace = Workspace(name=f"derived preview workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"derived preview task set {suffix}")
    db_session.add(task_set)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"derived preview batch {suffix}",
        batch_type="ego",
    )
    db_session.add(batch)
    db_session.flush()
    source = Episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        episode_uid=f"derived-preview-source-{suffix}",
        kind="source",
        modality="ego",
        quality_status="passed",
        source_fingerprint=uuid4().hex + uuid4().hex[:32],
    )
    db_session.add(source)
    db_session.flush()
    cut_item = WorkItem(
        workspace_id=workspace.id,
        episode_id=source.id,
        kind="cut",
        status="accepted",
        created_by_user_id=actor.id,
    )
    db_session.add(cut_item)
    db_session.flush()
    review = WorkItem(
        workspace_id=workspace.id,
        episode_id=source.id,
        kind="review",
        review_target_kind="cut",
        review_of_work_item_id=cut_item.id,
        status="accepted",
        created_by_user_id=actor.id,
    )
    db_session.add(review)
    db_session.flush()
    children = []
    for index, (start_ns, end_ns) in enumerate(((100, 300), (300, 500), (500, 700)), start=1):
        child = Episode(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            batch_id=batch.id,
            episode_uid=f"derived-preview-child-{index}-{suffix}",
            kind="derived",
            parent_episode_id=source.id,
            derivation_version=1,
            modality="ego",
            quality_status="passed",
            source_fingerprint=source.source_fingerprint,
            source_start_ns=start_ns,
            source_end_ns=end_ns,
            metadata_json={"lineage": {"cut_review_work_item_id": review.id}},
        )
        db_session.add(child)
        children.append(child)
    db_session.flush()
    job = JobRun(
        id=uuid4().hex,
        kind="derived_preview_batch",
        resource_type="episode",
        resource_id=str(source.id),
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        idempotency_key=f"derived-preview-batch:{source.id}:review:{review.id}:v1",
        queue="media",
        actor_id=actor.id,
        status="queued",
        phase="queued",
        detail_json={"source_episode_id": source.id, "cut_review_work_item_id": review.id},
    )
    db_session.add(job)
    db_session.commit()
    return workspace, source, review, tuple(children), job


def _published_parent_preview(db_session, *, source: Episode) -> EpisodeArtifact:
    payload = b"published parent preview"
    checksum = hashlib.sha256(payload).hexdigest()
    artifact = EpisodeArtifact(
        episode_id=source.id,
        artifact_type="process_preview",
        storage_role="process",
        storage_uri=f"nas://process/v2/parents/{source.id}/preview.mp4",
        checksum_sha256=checksum,
        size_bytes=len(payload),
        manifest_hash=checksum,
        retention_policy="permanent",
        metadata_json={
            "media_type": "video/mp4",
            "reference_topic": "/camera/front/rgb",
            "encoded_fps": 20.0,
            "frame_count": 6,
            "frame_timestamps_ns": ["100", "200", "300", "400", "500", "600"],
        },
    )
    db_session.add(artifact)
    db_session.flush()
    db_session.add(
        ArtifactOperation(
            id=uuid4().hex,
            artifact_id=artifact.id,
            operation_kind="process_preview_publish",
            status="published",
            target_uri=artifact.storage_uri,
            checksum_sha256=checksum,
            size_bytes=len(payload),
            manifest_json={"kind": "file", "entries": []},
        )
    )
    db_session.commit()
    return artifact


def _install_fake_batch_io(
    monkeypatch,
    *,
    parent_preview: EpisodeArtifact,
    materialize_calls: list[int],
    encode_calls: list[tuple[int, ...]],
) -> None:
    def materialize_parent_preview(
        *, source: Episode, artifact: EpisodeArtifact, stage: Path
    ) -> Path:
        assert artifact.id == parent_preview.id
        materialize_calls.append(source.id)
        stage.mkdir(parents=True, exist_ok=True)
        source_path = stage / "source.mp4"
        source_path.write_bytes(b"published parent preview")
        return source_path

    def encode_segments(
        source_path: Path,
        *,
        source_timestamps_ns: tuple[int, ...],
        segments,
        fps: float,
    ) -> tuple[PreviewSegmentFacts, ...]:
        assert source_path.read_bytes() == b"published parent preview"
        assert source_timestamps_ns == (100, 200, 300, 400, 500, 600)
        assert fps == 20.0
        encode_calls.append(tuple(segment.episode_id for segment in segments))
        facts: list[PreviewSegmentFacts] = []
        for segment in segments:
            timestamps = tuple(
                timestamp
                for timestamp in source_timestamps_ns
                if segment.start_ns <= timestamp < segment.end_ns
            )
            segment.output_path.parent.mkdir(parents=True, exist_ok=True)
            segment.output_path.write_bytes(f"preview-{segment.episode_id}".encode("ascii"))
            facts.append(
                PreviewSegmentFacts(
                    episode_id=segment.episode_id,
                    path=segment.output_path,
                    frame_timestamps_ns=timestamps,
                )
            )
        return tuple(facts)

    monkeypatch.setattr(
        derived_preview_batch,
        "_materialize_parent_preview",
        materialize_parent_preview,
        raising=False,
    )
    monkeypatch.setattr(
        derived_preview_batch,
        "export_preview_segments_from_mp4",
        encode_segments,
        raising=False,
    )


def test_run_derived_preview_batch_materializes_once_and_publishes_each_child(
    db_session,
    tmp_storage: Path,
    monkeypatch,
) -> None:
    workspace, source, _review, children, job = _batch_preview_context(db_session)
    parent_preview = _published_parent_preview(db_session, source=source)
    materialize_calls: list[int] = []
    encode_calls: list[tuple[int, ...]] = []
    _install_fake_batch_io(
        monkeypatch,
        parent_preview=parent_preview,
        materialize_calls=materialize_calls,
        encode_calls=encode_calls,
    )

    result = derived_preview_batch.run_derived_preview_batch(db_session, job)

    assert materialize_calls == [source.id]
    assert encode_calls == [tuple(child.id for child in children)]
    assert result == {
        "source_episode_id": source.id,
        "total_count": 3,
        "completed_count": 3,
        "failed_count": 0,
    }
    db_session.expire_all()
    for child, expected_timestamps in zip(
        children, ((100, 200), (300, 400), (500, 600)), strict=True
    ):
        artifact = (
            db_session.query(EpisodeArtifact)
            .filter_by(
                episode_id=child.id,
                artifact_type="process_preview",
            )
            .one()
        )
        operation = (
            db_session.query(ArtifactOperation)
            .filter_by(
                artifact_id=artifact.id,
                operation_kind="process_preview_publish",
            )
            .one()
        )
        assert operation.status == "published"
        assert artifact.metadata_json["generated_by_job_id"] == job.id
        assert artifact.metadata_json["reference_topic"] == "/camera/front/rgb"
        assert artifact.metadata_json["frame_timestamps_ns"] == [
            str(value) for value in expected_timestamps
        ]
    assert (
        db_session.query(RealtimeEvent)
        .filter(
            RealtimeEvent.resource_type == "work_queue",
            RealtimeEvent.resource_id == str(workspace.id),
            RealtimeEvent.event_name == "work_queue.invalidated",
        )
        .count()
        == 1
    )
    assert db_session.get(JobRun, job.id).detail_json == {
        "source_episode_id": source.id,
        "cut_review_work_item_id": _review.id,
        "total_count": 3,
        "completed_count": 3,
        "failed_count": 0,
        "preview_events_pending": False,
    }
    assert not (tmp_storage / "hot" / "derived-preview-batch" / str(source.id) / job.id).exists()


def test_run_derived_preview_batch_retries_one_pending_invalidation_without_reencoding(
    db_session,
    tmp_storage: Path,
    monkeypatch,
) -> None:
    workspace, source, review, children, job = _batch_preview_context(db_session)
    parent_preview = _published_parent_preview(db_session, source=source)
    materialize_calls: list[int] = []
    encode_calls: list[tuple[int, ...]] = []
    _install_fake_batch_io(
        monkeypatch,
        parent_preview=parent_preview,
        materialize_calls=materialize_calls,
        encode_calls=encode_calls,
    )
    original_enqueue = derived_preview_batch._enqueue_batch_preview_events
    enqueue_attempts: list[int] = []

    def fail_enqueue(*_args, **_kwargs) -> None:
        enqueue_attempts.append(1)
        raise RuntimeError("simulated outbox failure")

    monkeypatch.setattr(derived_preview_batch, "_enqueue_batch_preview_events", fail_enqueue)
    with pytest.raises(RuntimeError, match="simulated outbox failure"):
        derived_preview_batch.run_derived_preview_batch(db_session, job)

    assert materialize_calls == [source.id]
    assert encode_calls == [tuple(child.id for child in children)]
    assert enqueue_attempts == [1]
    assert db_session.get(JobRun, job.id).detail_json == {
        "source_episode_id": source.id,
        "cut_review_work_item_id": review.id,
        "total_count": 3,
        "completed_count": 3,
        "failed_count": 0,
        "preview_events_pending": True,
    }
    assert (
        db_session.query(RealtimeEvent)
        .filter(
            RealtimeEvent.resource_type == "work_queue",
            RealtimeEvent.resource_id == str(workspace.id),
            RealtimeEvent.event_name == "work_queue.invalidated",
        )
        .count()
        == 0
    )

    monkeypatch.setattr(derived_preview_batch, "_enqueue_batch_preview_events", original_enqueue)
    result = derived_preview_batch.run_derived_preview_batch(db_session, job)

    assert result == {
        "source_episode_id": source.id,
        "total_count": 3,
        "completed_count": 3,
        "failed_count": 0,
    }
    assert materialize_calls == [source.id]
    assert encode_calls == [tuple(child.id for child in children)]
    assert (
        db_session.query(RealtimeEvent)
        .filter(
            RealtimeEvent.resource_type == "work_queue",
            RealtimeEvent.resource_id == str(workspace.id),
            RealtimeEvent.event_name == "work_queue.invalidated",
        )
        .count()
        == 1
    )
    assert (
        db_session.query(RealtimeEvent)
        .filter(
            RealtimeEvent.resource_type == "episode",
            RealtimeEvent.resource_id.in_([str(source.id), *(str(child.id) for child in children)]),
            RealtimeEvent.event_name == "episode.updated",
        )
        .count()
        == 0
    )
    assert db_session.get(JobRun, job.id).detail_json == {
        "source_episode_id": source.id,
        "cut_review_work_item_id": review.id,
        "total_count": 3,
        "completed_count": 3,
        "failed_count": 0,
        "preview_events_pending": False,
    }
    assert not (tmp_storage / "hot" / "derived-preview-batch" / str(source.id) / job.id).exists()

    derived_preview_batch.run_derived_preview_batch(db_session, job)
    assert (
        db_session.query(RealtimeEvent)
        .filter(
            RealtimeEvent.resource_type == "work_queue",
            RealtimeEvent.resource_id == str(workspace.id),
            RealtimeEvent.event_name == "work_queue.invalidated",
        )
        .count()
        == 1
    )


def test_run_derived_preview_batch_retries_only_unpublished_child(
    db_session,
    tmp_storage: Path,
    monkeypatch,
) -> None:
    _workspace, source, _review, children, job = _batch_preview_context(db_session)
    parent_preview = _published_parent_preview(db_session, source=source)
    materialize_calls: list[int] = []
    encode_calls: list[tuple[int, ...]] = []
    _install_fake_batch_io(
        monkeypatch,
        parent_preview=parent_preview,
        materialize_calls=materialize_calls,
        encode_calls=encode_calls,
    )
    failing_child_id = children[1].id
    original_materialize = derived_preview_batch.materialize_artifact_operation

    def fail_one_child(db, *, operation_id: str, source_path: Path):
        operation = db.get(ArtifactOperation, operation_id)
        assert operation is not None
        artifact = db.get(EpisodeArtifact, operation.artifact_id)
        assert artifact is not None
        if artifact.episode_id == failing_child_id:
            raise OSError("simulated publish failure")
        return original_materialize(db, operation_id=operation_id, source_path=source_path)

    monkeypatch.setattr(derived_preview_batch, "materialize_artifact_operation", fail_one_child)
    with pytest.raises(ValueError, match="derived preview batch generation failed") as error:
        derived_preview_batch.run_derived_preview_batch(db_session, job)
    assert parent_preview.storage_uri not in str(error.value)
    assert encode_calls == [tuple(child.id for child in children)]
    assert not (tmp_storage / "hot" / "derived-preview-batch" / str(source.id) / job.id).exists()
    assert {
        artifact.episode_id
        for artifact in db_session.query(EpisodeArtifact)
        .filter(EpisodeArtifact.artifact_type == "process_preview")
        .all()
        if artifact.episode_id in {child.id for child in children}
        and db_session.query(ArtifactOperation)
        .filter_by(
            artifact_id=artifact.id, operation_kind="process_preview_publish", status="published"
        )
        .count()
        == 1
    } == {children[0].id, children[2].id}

    monkeypatch.setattr(
        derived_preview_batch, "materialize_artifact_operation", original_materialize
    )
    result = derived_preview_batch.run_derived_preview_batch(db_session, job)

    assert result["completed_count"] == 3
    assert result["failed_count"] == 0
    assert materialize_calls == [source.id, source.id]
    assert encode_calls == [
        tuple(child.id for child in children),
        (failing_child_id,),
    ]
    assert db_session.get(EpisodeArtifact, parent_preview.id) is not None
    assert not (tmp_storage / "hot" / "derived-preview-batch" / str(source.id) / job.id).exists()


@pytest.mark.parametrize("invalid_parent", ("missing", "unpublished", "timeline"))
def test_run_derived_preview_batch_rejects_untrusted_parent_before_materialization(
    db_session,
    tmp_storage: Path,
    monkeypatch,
    invalid_parent: str,
) -> None:
    _workspace, source, _review, _children, job = _batch_preview_context(db_session)
    if invalid_parent in {"unpublished", "timeline"}:
        parent_preview = _published_parent_preview(db_session, source=source)
    if invalid_parent == "unpublished":
        operation = (
            db_session.query(ArtifactOperation).filter_by(artifact_id=parent_preview.id).one()
        )
        operation.status = "pending"
        db_session.commit()
    if invalid_parent == "timeline":
        parent_preview.metadata_json = {
            **parent_preview.metadata_json,
            "frame_timestamps_ns": ["100", "100"],
            "frame_count": 2,
        }
        db_session.commit()
    materialize_calls: list[object] = []
    encode_calls: list[object] = []

    def unexpected_materialize(**_kwargs) -> Path:
        materialize_calls.append(object())
        raise AssertionError("parent materialization must not run")

    def unexpected_encode(*_args, **_kwargs):
        encode_calls.append(object())
        raise AssertionError("encoding must not run")

    monkeypatch.setattr(
        derived_preview_batch, "_materialize_parent_preview", unexpected_materialize
    )
    monkeypatch.setattr(
        derived_preview_batch, "export_preview_segments_from_mp4", unexpected_encode
    )

    with pytest.raises(ValueError, match="derived preview parent preview is unavailable") as error:
        derived_preview_batch.run_derived_preview_batch(db_session, job)

    assert not materialize_calls
    assert not encode_calls
    assert "nas://" not in str(error.value)
    assert db_session.get(JobRun, job.id).detail_json == {
        "source_episode_id": source.id,
        "cut_review_work_item_id": _review.id,
        "total_count": 3,
        "completed_count": 0,
        "failed_count": 3,
    }
    assert not (tmp_storage / "hot" / "derived-preview-batch" / str(source.id) / job.id).exists()


def test_run_derived_preview_batch_rejects_parent_checksum_mismatch_and_cleans_stage(
    db_session,
    tmp_storage: Path,
    monkeypatch,
) -> None:
    _workspace, source, _review, _children, job = _batch_preview_context(db_session)
    parent_preview = _published_parent_preview(db_session, source=source)
    encode_calls: list[object] = []

    def wrong_parent(*, stage: Path, **_kwargs) -> Path:
        source_path = stage / "source.mp4"
        source_path.write_bytes(b"X" * len(b"published parent preview"))
        return source_path

    def unexpected_encode(*_args, **_kwargs):
        encode_calls.append(object())
        raise AssertionError("encoding must not run")

    monkeypatch.setattr(derived_preview_batch, "_materialize_parent_preview", wrong_parent)
    monkeypatch.setattr(
        derived_preview_batch, "export_preview_segments_from_mp4", unexpected_encode
    )

    with pytest.raises(ValueError, match="derived preview parent preview is unavailable") as error:
        derived_preview_batch.run_derived_preview_batch(db_session, job)

    assert parent_preview.storage_uri not in str(error.value)
    assert not encode_calls
    assert db_session.get(JobRun, job.id).detail_json == {
        "source_episode_id": source.id,
        "cut_review_work_item_id": _review.id,
        "total_count": 3,
        "completed_count": 0,
        "failed_count": 3,
    }
    assert not (tmp_storage / "hot" / "derived-preview-batch" / str(source.id) / job.id).exists()


def test_derived_preview_batch_rejects_symlinked_stage_parent(
    db_session,
    tmp_storage: Path,
) -> None:
    _workspace, source, _review, _children, job = _batch_preview_context(db_session)
    outside_stage = tmp_storage / "other-stage-root"
    outside_stage.mkdir()
    hot = tmp_storage / "hot"
    hot.rmdir()
    hot.symlink_to(outside_stage, target_is_directory=True)

    with pytest.raises(ValueError, match="derived preview stage"):
        derived_preview_batch._prepare_batch_stage(source_id=source.id, job_id=job.id)

    assert not (outside_stage / "derived-preview-batch").exists()


@pytest.mark.parametrize(
    "storage_uri",
    (
        "oss://unexpected-bucket/process/v2/parents/1/preview.mp4",
        "oss://process/process/v3/parents/1/preview.mp4",
    ),
)
def test_derived_preview_batch_rejects_untrusted_oss_parent_uri(
    db_session,
    tmp_storage: Path,
    monkeypatch,
    storage_uri: str,
) -> None:
    _workspace, source, _review, _children, _job = _batch_preview_context(db_session)
    parent_preview = _published_parent_preview(db_session, source=source)
    parent_preview.storage_uri = storage_uri
    download_calls: list[object] = []
    monkeypatch.setattr(
        derived_preview_batch.oss_client,
        "download_to",
        lambda *_args, **_kwargs: download_calls.append(object()),
    )
    stage = tmp_storage / "hot" / "derived-preview-batch" / str(source.id) / "stage"
    stage.mkdir(parents=True)

    with pytest.raises(ValueError, match="derived preview parent storage URI is invalid"):
        derived_preview_batch._materialize_parent_preview(
            source=source, artifact=parent_preview, stage=stage
        )

    assert not download_calls


def test_derived_preview_batch_rejects_symlinked_nas_parent(
    db_session,
    tmp_storage: Path,
) -> None:
    _workspace, source, _review, _children, _job = _batch_preview_context(db_session)
    parent_preview = _published_parent_preview(db_session, source=source)
    parent_path = tmp_storage / "process" / "v2" / "parents" / str(source.id) / "preview.mp4"
    parent_path.parent.mkdir(parents=True)
    target = tmp_storage / "different-parent.mp4"
    target.write_bytes(b"not trusted")
    parent_path.symlink_to(target)
    stage = tmp_storage / "hot" / "derived-preview-batch" / str(source.id) / "stage"
    stage.mkdir(parents=True)

    with pytest.raises(ValueError, match="derived preview parent storage URI is invalid"):
        derived_preview_batch._materialize_parent_preview(
            source=source, artifact=parent_preview, stage=stage
        )


def test_derived_preview_batch_rejects_nas_parent_with_symlinked_ancestor(
    db_session,
    tmp_storage: Path,
) -> None:
    _workspace, source, _review, _children, _job = _batch_preview_context(db_session)
    parent_preview = _published_parent_preview(db_session, source=source)
    process_root = tmp_storage / "process"
    process_root.mkdir()
    alternate_root = tmp_storage / "alternate-process"
    (alternate_root / "v2" / "parents" / str(source.id)).mkdir(parents=True)
    (alternate_root / "v2" / "parents" / str(source.id) / "preview.mp4").write_bytes(b"not trusted")
    process_root.rmdir()
    process_root.symlink_to(alternate_root, target_is_directory=True)
    stage = tmp_storage / "hot" / "derived-preview-batch" / str(source.id) / "stage"
    stage.mkdir(parents=True)

    with pytest.raises(ValueError, match="derived preview parent storage URI is invalid"):
        derived_preview_batch._materialize_parent_preview(
            source=source, artifact=parent_preview, stage=stage
        )


def test_batch_worker_registers_derived_preview_batch_handler(monkeypatch) -> None:
    from data.tasks import batch_workers

    calls: list[tuple[object, object]] = []

    def fake_run(db, job):
        calls.append((db, job))
        return {"completed_count": 1}

    monkeypatch.setattr(derived_preview_batch, "run_derived_preview_batch", fake_run, raising=False)
    db = object()
    job = object()

    assert batch_workers._HANDLERS["derived_preview_batch"](db, job) == {"completed_count": 1}
    assert calls == [(db, job)]
