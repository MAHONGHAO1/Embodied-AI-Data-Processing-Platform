from __future__ import annotations

from uuid import uuid4

import pytest

from data.database import (
    Batch,
    Dataset,
    Episode,
    EpisodeArtifact,
    PublishedEpisode,
    TaskLabel,
    TaskSet,
    User,
    Workspace,
    WorkspaceMember,
)


def _published_episode(db, *, actor, episode, suffix: str):
    artifact = EpisodeArtifact(
        episode_id=episode.id,
        artifact_type="official_qrdf",
        storage_role="official",
        storage_uri=f"nas://official/{suffix}-{uuid4().hex}",
        checksum_sha256=suffix[0] * 64,
        size_bytes=128,
    )
    preview = EpisodeArtifact(
        episode_id=episode.id,
        artifact_type="process_preview",
        storage_role="process",
        storage_uri=f"nas://process/v1/{suffix}-{uuid4().hex}.mp4",
        checksum_sha256="f" * 64,
        size_bytes=64,
        metadata_json={"media_type": "video/mp4"},
    )
    db.add_all([artifact, preview])
    db.flush()
    db.add(
        PublishedEpisode(
            episode_id=episode.id,
            official_artifact_id=artifact.id,
            publisher_user_id=actor.id,
            output_profile="qrdf",
            manifest_hash="b" * 64,
        )
    )


def _candidate_fixture(db_session):
    actor = db_session.query(User).order_by(User.id).first()
    workspace = Workspace(name=f"candidate-{uuid4().hex}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=actor.id))
    task_set = TaskSet(workspace_id=workspace.id, name=f"candidate-task-{uuid4().hex}")
    db_session.add(task_set)
    db_session.flush()
    task_label = TaskLabel(
        key=f"box-{uuid4().hex[:8]}",
        name="叠盒子",
    )
    db_session.add(task_label)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="candidate batch",
        batch_type="ego",
    )
    db_session.add(batch)
    db_session.flush()
    source = Episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        episode_uid=f"source-{uuid4().hex}",
        kind="source",
        modality="ego",
        task_label_id=task_label.id,
        source_fingerprint="1" * 64,
        metadata_json={
            "metrics": {"duration_s": 20},
            "timing": {"start_timestamp_ns": 1_000_000_000, "end_timestamp_ns": 21_000_000_000},
        },
    )
    db_session.add(source)
    db_session.flush()
    first = Episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        episode_uid=f"derived-first-{uuid4().hex}",
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        task_label_id=task_label.id,
        source_fingerprint="2" * 64,
        source_start_ns=2_000_000_000,
        source_end_ns=5_000_000_000,
        metadata_json={"metrics": {"duration_s": 3}},
    )
    second = Episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        episode_uid=f"derived-second-{uuid4().hex}",
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        task_label_id=task_label.id,
        source_fingerprint="3" * 64,
        source_start_ns=8_000_000_000,
        source_end_ns=12_000_000_000,
        metadata_json={"metrics": {"duration_s": 4}},
    )
    unpublished = Episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        episode_uid=f"unpublished-{uuid4().hex}",
        kind="derived",
        parent_episode_id=source.id,
        derivation_version=1,
        modality="ego",
        source_fingerprint="4" * 64,
        source_start_ns=13_000_000_000,
        source_end_ns=15_000_000_000,
        metadata_json={"metrics": {"duration_s": 2}},
    )
    db_session.add_all([first, second, unpublished])
    db_session.flush()
    _published_episode(db_session, actor=actor, episode=source, suffix="a-source")
    _published_episode(db_session, actor=actor, episode=first, suffix="c-first")
    _published_episode(db_session, actor=actor, episode=second, suffix="d-second")
    dataset = Dataset(workspace_id=workspace.id, name=f"dataset-{uuid4().hex}")
    db_session.add(dataset)
    db_session.commit()
    return actor, workspace, task_set, dataset, source, first, second, unpublished


def test_candidate_projection_groups_published_episodes_without_storage_details(db_session):
    from data.services.dataset_revision_candidates import dataset_revision_candidates

    actor, workspace, task_set, dataset, source, first, second, unpublished = _candidate_fixture(
        db_session
    )

    payload = dataset_revision_candidates(
        db_session,
        dataset_id=dataset.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )

    assert payload["workspace_id"] == workspace.id
    assert payload["summary"] == {"candidate_count": 3, "source_count": 1, "duration_s": 27.0}
    assert len(payload["groups"]) == 1
    group = payload["groups"][0]
    assert group["source"]["id"] == source.id
    assert group["source"]["timeline_start_ns"] == "1000000000"
    assert group["source"]["timeline_end_ns"] == "21000000000"
    assert [item["episode_id"] for item in group["candidates"]] == [source.id, first.id, second.id]
    assert group["candidates"][0]["candidate_kind"] == "full_source"
    assert group["candidates"][1]["candidate_kind"] == "derived"
    assert unpublished.id not in {item["episode_id"] for item in group["candidates"]}
    assert group["candidates"][1]["task_label"] == {"id": first.task_label_id, "name": "叠盒子"}
    assert group["candidates"][1]["preview_available"] is True
    serialized = repr(payload)
    assert "storage_uri" not in serialized
    assert "nas://" not in serialized
    assert "artifacts" not in serialized


def test_candidate_projection_rejects_a_task_set_outside_dataset_workspace(db_session):
    from data.services.dataset_revision_candidates import dataset_revision_candidates

    actor, _workspace, _task_set, dataset, *_ = _candidate_fixture(db_session)
    foreign_workspace = Workspace(name=f"foreign-{uuid4().hex}", creator="test")
    db_session.add(foreign_workspace)
    db_session.flush()
    foreign_task_set = TaskSet(
        workspace_id=foreign_workspace.id, name=f"foreign-task-{uuid4().hex}"
    )
    db_session.add(foreign_task_set)
    db_session.commit()

    with pytest.raises(ValueError, match="task set does not exist"):
        dataset_revision_candidates(
            db_session,
            dataset_id=dataset.id,
            task_set_id=foreign_task_set.id,
            actor_id=actor.id,
        )


def test_candidate_projection_defaults_to_all_task_sets_in_the_workspace(db_session):
    from data.services.dataset_revision_candidates import dataset_revision_candidates

    actor, workspace, task_set, dataset, source, first, second, _unpublished = _candidate_fixture(
        db_session
    )
    other_task_set = TaskSet(workspace_id=workspace.id, name=f"candidate-other-{uuid4().hex}")
    db_session.add(other_task_set)
    db_session.flush()
    other_batch = Batch(
        workspace_id=workspace.id,
        task_set_id=other_task_set.id,
        name=f"candidate-other-batch-{uuid4().hex}",
        batch_type="ego",
    )
    db_session.add(other_batch)
    db_session.flush()
    other_source = Episode(
        workspace_id=workspace.id,
        task_set_id=other_task_set.id,
        batch_id=other_batch.id,
        episode_uid=f"candidate-other-source-{uuid4().hex}",
        kind="source",
        modality="ego",
        source_fingerprint="9" * 64,
        metadata_json={
            "metrics": {"duration_s": 5},
            "timing": {"start_timestamp_ns": 1, "end_timestamp_ns": 5_000_000_001},
        },
    )
    db_session.add(other_source)
    db_session.flush()
    _published_episode(db_session, actor=actor, episode=other_source, suffix="e-other")
    db_session.commit()

    payload = dataset_revision_candidates(db_session, dataset_id=dataset.id, actor_id=actor.id)

    assert payload["task_set_id"] is None
    assert {group["source"]["id"] for group in payload["groups"]} == {source.id, other_source.id}
    assert {group["source"]["task_set_id"] for group in payload["groups"]} == {
        task_set.id,
        other_task_set.id,
    }
    assert {
        candidate["task_set_id"] for group in payload["groups"] for candidate in group["candidates"]
    } == {
        task_set.id,
        other_task_set.id,
    }
    assert {first.id, second.id}.issubset(
        {
            candidate["episode_id"]
            for group in payload["groups"]
            for candidate in group["candidates"]
        }
    )


def test_candidate_projection_marks_missing_source_timeline_unavailable(db_session):
    from data.services.dataset_revision_candidates import dataset_revision_candidates

    actor, _workspace, task_set, dataset, source, *_ = _candidate_fixture(db_session)
    source.metadata_json = {"metrics": {"duration_s": 20}}
    db_session.commit()

    payload = dataset_revision_candidates(
        db_session,
        dataset_id=dataset.id,
        task_set_id=task_set.id,
        actor_id=actor.id,
    )

    projected = payload["groups"][0]["source"]
    assert projected["timeline_available"] is False
    assert projected["timeline_start_ns"] is None
    assert projected["timeline_end_ns"] is None


def test_revision_candidate_api_returns_the_safe_grouped_projection(
    client, admin_headers, db_session
):
    _actor, workspace, task_set, dataset, source, first, second, _unpublished = _candidate_fixture(
        db_session
    )

    response = client.get(
        f"/api/v1/datasets/{dataset.id}/revision-candidates",
        params={"task_set_id": task_set.id},
        headers=admin_headers,
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["workspace_id"] == workspace.id
    assert payload["groups"][0]["source"]["id"] == source.id
    assert [item["episode_id"] for item in payload["groups"][0]["candidates"]] == [
        source.id,
        first.id,
        second.id,
    ]
    serialized = repr(payload)
    assert "storage_uri" not in serialized
    assert "nas://" not in serialized


def test_revision_candidate_api_hides_a_task_set_outside_the_dataset_workspace(
    client, admin_headers, db_session
):
    _actor, _workspace, _task_set, dataset, *_ = _candidate_fixture(db_session)
    foreign_workspace = Workspace(name=f"api-foreign-{uuid4().hex}", creator="test")
    db_session.add(foreign_workspace)
    db_session.flush()
    foreign_task_set = TaskSet(
        workspace_id=foreign_workspace.id,
        name=f"api-foreign-task-{uuid4().hex}",
    )
    db_session.add(foreign_task_set)
    db_session.commit()

    response = client.get(
        f"/api/v1/datasets/{dataset.id}/revision-candidates",
        params={"task_set_id": foreign_task_set.id},
        headers=admin_headers,
    )

    assert response.status_code == 404
