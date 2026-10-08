from pathlib import Path
from uuid import uuid4

import pytest

from data.database import Episode, EpisodeArtifact, JobRun, TaskSet, User, Workspace
from data.infra.oss_client import OSSObjectInfo
from data.services.artifact_operations import (
    complete_artifact_operation,
    ensure_artifact_operation,
    materialize_artifact_operation,
)
from data.services.batches import create_batch


def _context(db_session, *, storage_uri: str):
    actor = db_session.query(User).order_by(User.id).first()
    workspace = Workspace(name=f"artifact operation {uuid4().hex}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"artifact project {uuid4().hex}")
    db_session.add(project)
    db_session.flush()
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="artifact batch",
        batch_type="teleop",
        actor_id=actor.id,
    )
    episode = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        episode_uid=f"artifact-episode-{uuid4().hex}",
        kind="source",
        modality="teleop",
    )
    db_session.add(episode)
    db_session.flush()
    artifact = EpisodeArtifact(
        episode_id=episode.id,
        artifact_type="process_preview",
        storage_role="process",
        storage_uri=storage_uri,
        checksum_sha256="",
        size_bytes=0,
    )
    job = JobRun(
        id=uuid4().hex,
        kind="episode_preview",
        resource_type="episode",
        resource_id=str(episode.id),
        workspace_id=episode.workspace_id,
        task_set_id=episode.task_set_id,
        idempotency_key=f"artifact-job-{uuid4().hex}",
        queue="media",
        status="queued",
        phase="queued",
    )
    db_session.add_all((artifact, job))
    db_session.flush()
    return artifact, job


def test_external_write_intent_is_committed_before_materialization_and_is_resumable(
    db_session,
    tmp_path,
    monkeypatch,
):
    from data.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    storage_uri = f"nas://process/v1/episode/preview-{uuid4().hex}.mp4"
    artifact, job = _context(db_session, storage_uri=storage_uri)
    source = tmp_path / "preview.mp4"
    source.write_bytes(b"preview-bytes")

    operation = ensure_artifact_operation(
        db_session,
        artifact=artifact,
        job=job,
        operation_kind="process_preview_publish",
        source_path=source,
    )
    db_session.commit()
    operation_id = operation.id

    assert db_session.get(type(operation), operation_id).status == "pending"

    materialize_artifact_operation(db_session, operation_id=operation_id, source_path=source)
    complete_artifact_operation(db_session, operation_id=operation_id)
    db_session.commit()

    assert db_session.get(type(operation), operation_id).status == "published"
    assert (tmp_path / storage_uri.removeprefix("nas://")).read_bytes() == b"preview-bytes"

    # A retry after the external write must reuse the exact target, never overwrite it.
    source.write_bytes(b"different-bytes")
    materialize_artifact_operation(db_session, operation_id=operation_id, source_path=source)
    assert (tmp_path / storage_uri.removeprefix("nas://")).read_bytes() == b"preview-bytes"


def test_operation_target_is_single_object_not_a_batch_root(db_session, tmp_path, monkeypatch):
    from data.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    artifact, job = _context(
        db_session, storage_uri=f"nas://process/v1/episode/preview-{uuid4().hex}.mp4"
    )
    source = tmp_path / "preview.mp4"
    source.write_bytes(b"preview")
    operation = ensure_artifact_operation(
        db_session,
        artifact=artifact,
        job=job,
        operation_kind="process_preview_publish",
        source_path=source,
    )
    db_session.commit()

    materialize_artifact_operation(db_session, operation_id=operation.id, source_path=source)
    assert (tmp_path / artifact.storage_uri.removeprefix("nas://")).is_file()
    assert not (tmp_path / "process/v1/episode").is_symlink()


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_operation_accepts_canonical_platform_versions(db_session, tmp_path, monkeypatch, version):
    from data.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    scope = "projects/1" if version == "v1" else "task-sets/1"
    artifact, job = _context(
        db_session,
        storage_uri=f"nas://process/{version}/workspaces/1/{scope}/preview-{uuid4().hex}.mp4",
    )
    source = tmp_path / f"preview-{version}.mp4"
    source.write_bytes(b"preview")

    operation = ensure_artifact_operation(
        db_session,
        artifact=artifact,
        job=job,
        operation_kind="process_preview_publish",
        source_path=source,
    )

    assert operation.target_uri == artifact.storage_uri


def test_directory_artifact_operation_rejects_nested_symlinks(db_session, tmp_path, monkeypatch):
    from data.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    artifact, job = _context(
        db_session, storage_uri=f"nas://process/v1/episode/dataset-{uuid4().hex}"
    )
    source = tmp_path / "dataset"
    source.mkdir()
    (source / "dataset.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text('{"private": true}', encoding="utf-8")
    (source / "linked.json").symlink_to(outside)

    with pytest.raises(ValueError, match="contains a symlink"):
        ensure_artifact_operation(
            db_session,
            artifact=artifact,
            job=job,
            operation_kind="process_preview_publish",
            source_path=source,
        )


def test_artifact_operation_rejects_a_symlinked_source_root(db_session, tmp_path, monkeypatch):
    from data.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    artifact, job = _context(
        db_session, storage_uri=f"nas://process/v1/episode/preview-{uuid4().hex}.mp4"
    )
    source = tmp_path / "preview.mp4"
    source.write_bytes(b"preview")
    linked_source = tmp_path / "preview-link.mp4"
    linked_source.symlink_to(source)

    with pytest.raises(ValueError, match="source is a symlink"):
        ensure_artifact_operation(
            db_session,
            artifact=artifact,
            job=job,
            operation_kind="process_preview_publish",
            source_path=linked_source,
        )


def test_existing_cloud_target_with_same_size_but_different_content_is_rejected(
    db_session, tmp_path, monkeypatch
):
    from data.config import settings
    from data.infra import oss_client
    from data.services import artifact_operations

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    artifact, job = _context(
        db_session,
        storage_uri=f"oss://process-bucket/process/v1/episode/preview-{uuid4().hex}.mp4",
    )
    source = tmp_path / "preview.mp4"
    source.write_bytes(b"good-data")
    operation = ensure_artifact_operation(
        db_session,
        artifact=artifact,
        job=job,
        operation_kind="process_preview_publish",
        source_path=source,
    )
    db_session.commit()

    monkeypatch.setattr(artifact_operations, "uses_cloud_uri_authority", lambda: True)
    monkeypatch.setattr(oss_client, "object_exists", lambda _bucket, _key: True)
    monkeypatch.setattr(
        oss_client,
        "object_info",
        lambda _bucket, _key: OSSObjectInfo(
            size=len(b"good-data"), etag="same-size", crc64=None, version_id=None, metadata={}
        ),
    )

    def download_wrong_bytes(destination, _bucket, key):
        (destination / Path(key).name).write_bytes(b"bad--data")
        return destination

    monkeypatch.setattr(oss_client, "download_to", download_wrong_bytes)
    monkeypatch.setattr(
        oss_client,
        "upload_file",
        lambda *args, **kwargs: pytest.fail(
            "an existing mismatched object must not be overwritten"
        ),
    )

    with pytest.raises(FileExistsError, match="different data"):
        materialize_artifact_operation(db_session, operation_id=operation.id, source_path=source)


def test_partial_cloud_directory_is_cleaned_by_exact_manifest_and_retried(
    db_session, tmp_path, monkeypatch
):
    from data.config import settings
    from data.infra import oss_client
    from data.services import artifact_operations

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    artifact, job = _context(
        db_session,
        storage_uri=f"oss://process-bucket/process/v1/episode/dataset-{uuid4().hex}",
    )
    source = tmp_path / "dataset"
    (source / "episodes" / "episode_000001").mkdir(parents=True)
    (source / "dataset.json").write_text("{}", encoding="utf-8")
    (source / "episodes" / "episode_000001" / "metadata.json").write_text(
        "metadata", encoding="utf-8"
    )
    operation = ensure_artifact_operation(
        db_session,
        artifact=artifact,
        job=job,
        operation_kind="process_preview_publish",
        source_path=source,
    )
    db_session.commit()

    monkeypatch.setattr(artifact_operations, "uses_cloud_uri_authority", lambda: True)
    bucket = "process-bucket"
    target_prefix = artifact.storage_uri.removeprefix(f"oss://{bucket}/")
    objects = {f"{target_prefix}/dataset.json": b"{}"}
    deleted: list[str] = []
    uploaded: list[tuple[str, str]] = []

    def object_info(_bucket, key):
        data = objects.get(key)
        if data is None:
            return None
        return OSSObjectInfo(size=len(data), etag="etag", crc64=None, version_id=None, metadata={})

    def download_to(destination, _bucket, _key):
        prefix = f"{target_prefix}/"
        for object_key, data in objects.items():
            target = destination / object_key.removeprefix(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return destination

    def delete_object(_bucket, key):
        deleted.append(key)
        objects.pop(key, None)
        return True

    def upload_file(source_path, _bucket, key, *, forbid_overwrite=False, **_kwargs):
        assert forbid_overwrite is True
        uploaded.append((_bucket, key))
        for item in source_path.rglob("*"):
            if item.is_file():
                objects[f"{key}/{item.relative_to(source_path).as_posix()}"] = item.read_bytes()
        return f"oss://{_bucket}/{key}"

    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(oss_client, "object_exists", lambda _bucket, key: key in objects)
    monkeypatch.setattr(oss_client, "download_to", download_to)
    monkeypatch.setattr(oss_client, "delete_object", delete_object)
    monkeypatch.setattr(oss_client, "upload_file", upload_file)

    materialize_artifact_operation(db_session, operation_id=operation.id, source_path=source)

    assert set(deleted) == {
        f"{target_prefix}/dataset.json",
        f"{target_prefix}/episodes/episode_000001/metadata.json",
    }
    assert target_prefix not in deleted
    assert uploaded == [(bucket, target_prefix)]
    assert db_session.get(type(operation), operation.id).status == "pending"
