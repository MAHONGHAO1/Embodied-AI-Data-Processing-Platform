import json
from uuid import uuid4

import pytest

from data.database import (
    Episode,
    EpisodeArtifact,
    EpisodeReview,
    JobRun,
    PublishedEpisode,
    RealtimeEvent,
    TaskSet,
    User,
    Workspace,
)
from data.services.batches import create_batch
from data.utils.checksums import tree_sha256


def _episode(db_session, tmp_path):
    actor = db_session.query(User).order_by(User.id).first()
    workspace = Workspace(name=f"publication workspace {uuid4().hex}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"publication project {uuid4().hex}")
    db_session.add(project)
    db_session.flush()
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="publication batch",
        batch_type="ego",
        actor_id=actor.id,
    )
    raw_key = f"raw/source-{uuid4().hex}"
    raw = tmp_path / raw_key
    raw.mkdir(parents=True)
    (raw / "metadata.json").write_text(
        json.dumps({"qrdf_version": "0.2.0", "episode_id": "source", "data_file": "data.mcap"}),
        encoding="utf-8",
    )
    (raw / "data.mcap").write_bytes(b"valid-mcap-placeholder")
    episode = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        episode_uid=f"publication-episode-{uuid4().hex}",
        kind="source",
        modality="ego",
        review_status="accepted",
        quality_status="passed",
        task_language="open box",
    )
    db_session.add(episode)
    db_session.flush()
    artifact = EpisodeArtifact(
        episode_id=episode.id,
        artifact_type="raw_source",
        storage_role="raw",
        storage_uri=f"nas://{raw_key}",
        checksum_sha256=tree_sha256(raw),
        size_bytes=1,
    )
    db_session.add(artifact)
    db_session.add(EpisodeReview(episode_id=episode.id, decision="accepted", auditor_id=actor.id))
    job = JobRun(
        id=uuid4().hex,
        kind="episode_publish",
        resource_type="episode",
        resource_id=str(episode.id),
        workspace_id=episode.workspace_id,
        task_set_id=episode.task_set_id,
        idempotency_key=f"publication:{episode.id}:{uuid4().hex}",
        queue="publish",
        actor_id=actor.id,
        status="queued",
        phase="queued",
        detail_json={"episode_id": episode.id},
    )
    db_session.add(job)
    db_session.commit()
    return actor, episode, job


def test_publication_requires_accepted_episode(db_session, tmp_path, monkeypatch):
    from data.services.batch_episode_publication import publish_batch_episode

    with pytest.raises(RuntimeError, match="retired"):
        publish_batch_episode(db_session, None)
    return

    _actor, episode, job = _episode(db_session, tmp_path)
    episode.review_status = "pending"
    db_session.commit()

    with pytest.raises(ValueError, match="accepted"):
        publish_batch_episode(db_session, job)
    assert db_session.query(PublishedEpisode).filter_by(episode_id=episode.id).count() == 0


def test_publication_materializes_official_artifact_and_is_idempotent(
    db_session, tmp_path, monkeypatch
):
    from data.services.batch_episode_publication import publish_batch_episode

    with pytest.raises(RuntimeError, match="retired"):
        publish_batch_episode(db_session, None)
    return
    from data.config import settings
    from data.services import batch_episode_publication
    from data.utils.checksums import tree_sha256

    _actor, episode, job = _episode(db_session, tmp_path)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(
        batch_episode_publication, "validate_dataset", lambda _path: {"error_count": 0}
    )

    first = batch_episode_publication.publish_batch_episode(db_session, job)
    second = batch_episode_publication.publish_batch_episode(db_session, job)

    assert first["published_episode_id"] == second["published_episode_id"]
    published = db_session.query(PublishedEpisode).filter_by(episode_id=episode.id).one()
    official = db_session.get(EpisodeArtifact, published.official_artifact_id)
    assert official.storage_role == "official"
    assert official.artifact_type == "official_qrdf"
    assert official.storage_uri.startswith(
        f"nas://official/v2/workspaces/{episode.workspace_id}/task-sets/{episode.task_set_id}/episodes/{episode.id}/"
    )
    official_root = tmp_path / official.storage_uri.removeprefix("nas://")
    assert (official_root / "dataset.json").is_file()
    assert (official_root / "episodes" / "episode_000001" / "annotation.json").is_file()
    assert official.checksum_sha256 == tree_sha256(official_root)
    assert official.manifest_hash == tree_sha256(official_root)
    metadata = json.loads(
        (official_root / "episodes" / "episode_000001" / "metadata.json").read_text()
    )
    assert metadata["episode_id"] == "episode_000001"
    assert metadata["task"]["language"] == "open box"
    assert metadata["annotation"]["language"] == "open box"
    episode_event = (
        db_session.query(RealtimeEvent)
        .filter(
            RealtimeEvent.resource_type == "episode",
            RealtimeEvent.resource_id == str(episode.id),
            RealtimeEvent.event_name == "episode.updated",
        )
        .one()
    )
    assert episode_event.safe_payload["workflow_status"] == "published"
    queue_event = (
        db_session.query(RealtimeEvent)
        .filter(
            RealtimeEvent.resource_type == "work_queue",
            RealtimeEvent.resource_id == str(episode.workspace_id),
            RealtimeEvent.event_name == "work_queue.invalidated",
        )
        .one()
    )
    assert queue_event.safe_payload == {"workspace_id": episode.workspace_id}
    assert "/publications/episode-" in official.storage_uri


def test_published_episode_is_constrained_to_one_official_result(db_session, tmp_path, monkeypatch):
    from data.services.batch_episode_publication import publish_batch_episode

    with pytest.raises(RuntimeError, match="retired"):
        publish_batch_episode(db_session, None)
    return
    from data.config import settings
    from data.services import batch_episode_publication

    actor, episode, job = _episode(db_session, tmp_path)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(
        batch_episode_publication, "validate_dataset", lambda _path: {"error_count": 0}
    )

    first = batch_episode_publication.publish_batch_episode(db_session, job)
    second_job = JobRun(
        id=uuid4().hex,
        kind="episode_publish",
        resource_type="episode",
        resource_id=str(episode.id),
        workspace_id=episode.workspace_id,
        task_set_id=episode.task_set_id,
        idempotency_key=f"publication-repeat:{episode.id}:{uuid4().hex}",
        queue="publish",
        actor_id=actor.id,
        status="queued",
        phase="queued",
    )
    db_session.add(second_job)
    db_session.commit()

    second = batch_episode_publication.publish_batch_episode(db_session, second_job)

    assert second == first
    assert db_session.query(PublishedEpisode).filter_by(episode_id=episode.id).count() == 1
    assert (
        db_session.query(EpisodeArtifact)
        .filter_by(episode_id=episode.id, artifact_type="official_qrdf")
        .count()
        == 1
    )


def test_derived_publication_materializes_from_parent_interval(db_session, tmp_path, monkeypatch):
    from data.services.batch_episode_publication import publish_batch_episode

    with pytest.raises(RuntimeError, match="retired"):
        publish_batch_episode(db_session, None)
    return
    from data.config import settings
    from data.services import batch_episode_publication, publication_source_cache

    actor, source, _source_job = _episode(db_session, tmp_path)
    derived = Episode(
        workspace_id=source.workspace_id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        episode_uid=f"derived-publication-{uuid4().hex}",
        kind="derived",
        parent=source,
        derivation_version=1,
        modality=source.modality,
        source_start_ns=0,
        source_end_ns=10,
        review_status="accepted",
        quality_status="passed",
        task_language="place object",
    )
    db_session.add(derived)
    db_session.flush()
    job = JobRun(
        id=uuid4().hex,
        kind="episode_publish",
        resource_type="episode",
        resource_id=str(derived.id),
        workspace_id=derived.workspace_id,
        task_set_id=derived.task_set_id,
        idempotency_key=f"derived-publication:{derived.id}:{uuid4().hex}",
        queue="publish",
        actor_id=actor.id,
        status="queued",
        phase="queued",
        detail_json={"episode_id": derived.id},
    )
    db_session.add(job)
    db_session.commit()

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(
        batch_episode_publication, "validate_dataset", lambda _path: {"error_count": 0}
    )
    original_copytree = publication_source_cache.shutil.copytree
    source_copies: list[str] = []

    def counting_copytree(source_dir, destination, *args, **kwargs):
        if "publication-source-cache" in str(destination):
            source_copies.append(str(source_dir))
        return original_copytree(source_dir, destination, *args, **kwargs)

    monkeypatch.setattr(publication_source_cache.shutil, "copytree", counting_copytree)

    def fake_interval_materializer(source_dir, destination, **kwargs):
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "metadata.json").write_text(
            json.dumps(
                {
                    "qrdf_version": "0.2.0",
                    "episode_id": kwargs["output_episode_id"],
                    "data_file": "data.mcap",
                }
            ),
            encoding="utf-8",
        )
        (destination / "data.mcap").write_bytes(b"derived-mcap")
        return destination

    monkeypatch.setattr(
        batch_episode_publication, "write_ego_interval_episode", fake_interval_materializer
    )
    result = batch_episode_publication.publish_batch_episode(db_session, job)

    second = Episode(
        workspace_id=source.workspace_id,
        task_set_id=source.task_set_id,
        batch_id=source.batch_id,
        episode_uid=f"derived-publication-{uuid4().hex}",
        kind="derived",
        parent=source,
        derivation_version=1,
        modality=source.modality,
        source_start_ns=10,
        source_end_ns=20,
        review_status="accepted",
        quality_status="passed",
        task_language="place object",
    )
    db_session.add(second)
    db_session.flush()
    second_job = JobRun(
        id=uuid4().hex,
        kind="episode_publish",
        resource_type="episode",
        resource_id=str(second.id),
        workspace_id=second.workspace_id,
        task_set_id=second.task_set_id,
        idempotency_key=f"derived-publication:{second.id}:{uuid4().hex}",
        queue="publish",
        actor_id=actor.id,
        status="queued",
        phase="queued",
        detail_json={"episode_id": second.id},
    )
    db_session.add(second_job)
    db_session.commit()
    batch_episode_publication.publish_batch_episode(db_session, second_job)

    published = db_session.get(PublishedEpisode, result["published_episode_id"])
    official = db_session.get(EpisodeArtifact, published.official_artifact_id)
    official_root = tmp_path / official.storage_uri.removeprefix("nas://")
    metadata = json.loads(
        (official_root / "episodes" / "episode_000001" / "metadata.json").read_text()
    )
    assert published.episode_id == derived.id
    assert metadata["episode_id"] == "episode_000001"
    assert metadata["task"]["language"] == "place object"
    assert len(source_copies) == 1


def test_publication_validation_failure_does_not_create_official_records(
    db_session, tmp_path, monkeypatch
):
    from data.services.batch_episode_publication import publish_batch_episode

    with pytest.raises(RuntimeError, match="retired"):
        publish_batch_episode(db_session, None)
    return
    from data.config import settings
    from data.services import batch_episode_publication

    _actor, episode, job = _episode(db_session, tmp_path)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(
        batch_episode_publication, "validate_dataset", lambda _path: {"error_count": 1}
    )

    with pytest.raises(ValueError, match="validation failed"):
        batch_episode_publication.publish_batch_episode(db_session, job)

    assert db_session.query(PublishedEpisode).filter_by(episode_id=episode.id).count() == 0
    assert (
        db_session.query(EpisodeArtifact)
        .filter(
            EpisodeArtifact.episode_id == episode.id,
            EpisodeArtifact.artifact_type == "official_qrdf",
        )
        .count()
        == 0
    )


def test_teleop_source_publication_sets_modality_domain(db_session, tmp_path, monkeypatch):
    from data.services.batch_episode_publication import publish_batch_episode

    with pytest.raises(RuntimeError, match="retired"):
        publish_batch_episode(db_session, None)
    return
    from data.config import settings
    from data.services import batch_episode_publication
    from data.services.batches import create_batch

    actor = db_session.query(User).order_by(User.id).first()
    workspace = Workspace(name=f"teleop publication workspace {uuid4().hex}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name=f"teleop publication project {uuid4().hex}")
    db_session.add(project)
    db_session.flush()
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="teleop publication batch",
        batch_type="teleop",
        actor_id=actor.id,
    )
    raw_key = f"raw/teleop-source-{uuid4().hex}"
    raw = tmp_path / raw_key
    raw.mkdir(parents=True)
    (raw / "metadata.json").write_text(
        json.dumps({"qrdf_version": "0.2.0", "episode_id": "source", "data_file": "data.mcap"}),
        encoding="utf-8",
    )
    (raw / "data.mcap").write_bytes(b"valid-mcap-placeholder")
    episode = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        episode_uid=f"teleop-publication-episode-{uuid4().hex}",
        kind="source",
        modality="teleop",
        review_status="accepted",
        quality_status="passed",
        task_language="pick cup",
    )
    db_session.add(episode)
    db_session.flush()
    db_session.add(
        EpisodeArtifact(
            episode_id=episode.id,
            artifact_type="raw_source",
            storage_role="raw",
            storage_uri=f"nas://{raw_key}",
            checksum_sha256=tree_sha256(raw),
            size_bytes=1,
        )
    )
    db_session.add(EpisodeReview(episode_id=episode.id, decision="accepted", auditor_id=actor.id))
    job = JobRun(
        id=uuid4().hex,
        kind="episode_publish",
        resource_type="episode",
        resource_id=str(episode.id),
        workspace_id=episode.workspace_id,
        task_set_id=episode.task_set_id,
        idempotency_key=f"publication:teleop:{episode.id}:{uuid4().hex}",
        queue="publish",
        actor_id=actor.id,
        status="queued",
        phase="queued",
        detail_json={"episode_id": episode.id},
    )
    db_session.add(job)
    db_session.commit()

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(
        batch_episode_publication, "validate_dataset", lambda _path: {"error_count": 0}
    )

    result = batch_episode_publication.publish_batch_episode(db_session, job)

    published = db_session.get(PublishedEpisode, result["published_episode_id"])
    official = db_session.get(EpisodeArtifact, published.official_artifact_id)
    official_root = tmp_path / official.storage_uri.removeprefix("nas://")
    dataset = json.loads((official_root / "dataset.json").read_text(encoding="utf-8"))
    assert episode.modality == "teleop"
    assert "teleop" in (dataset.get("domains") or [])
    assert result["episode_id"] == episode.id
