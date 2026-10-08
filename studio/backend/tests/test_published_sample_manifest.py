import json
from uuid import uuid4

import pytest

from data.database import (
    Batch,
    DatasetItem,
    Episode,
    EpisodeArtifact,
    PublishedEpisode,
    TaskSet,
    User,
    Workspace,
    WorkspaceMember,
)
from data.services.dataset_revisions import (
    DatasetItemInput,
    _coerce_item,
    build_revision_manifest,
    create_dataset_revision,
)
from data.services.published_sample_manifest import (
    build_export_sidecar,
    calculate_effective_range,
    canonical_manifest_hash,
)
from data.services.workflow_conflict import WorkflowConflict


def test_dataset_item_input_accepts_a_published_sample_range():
    item = _coerce_item(
        {
            "episode_id": 7,
            "split": "train",
            "sample_id": "sample-7",
            "core_start_ns": 100,
            "core_end_ns": 200,
            "effective_start_ns": 90,
            "effective_end_ns": 210,
            "task": "pick up the box",
        }
    )

    assert isinstance(item, DatasetItemInput)
    assert item.sample_id == "sample-7"
    assert item.effective_start_ns == 90
    assert item.effective_end_ns == 210
    assert DatasetItem.__tablename__ == "dataset_episode_items"


def test_effective_range_is_half_open_and_clamped_to_target():
    assert calculate_effective_range(
        core_start_ns=200,
        core_end_ns=400,
        target_start_ns=100,
        target_end_ns=500,
        pre_roll_s=0.0000002,
        post_roll_s=0.0000002,
    ) == (100, 500)


def test_manifest_hash_is_independent_of_mapping_order():
    assert canonical_manifest_hash({"b": 2, "a": 1}) == canonical_manifest_hash({"a": 1, "b": 2})


def test_sidecar_contains_business_mapping_without_paths():
    sidecar = build_export_sidecar(
        {"revision_id": 4, "items": [{"sample_id": "sample-1", "position": 0}]},
        {"input_manifest_sha256": "a" * 64, "artifact_manifest_sha256": "b" * 64},
    )
    assert sidecar["revision_id"] == 4
    assert sidecar["report"]["input_manifest_sha256"] == "a" * 64
    assert "source_qrdf_path" not in str(sidecar)


def test_sidecar_removes_location_and_credential_shaped_fields():
    sidecar = build_export_sidecar(
        {
            "revision_id": 4,
            "items": [
                {
                    "sample_id": "sample-1",
                    "package_uri": "oss://private-bucket/official/secret",
                    "temporary_path": "/runtime/storage/exports/private",
                    "preview_url": "https://signed.example/private?signature=secret",
                    "signature": "secret",
                    "credential": "secret",
                }
            ],
        },
        {"result_path": "/tmp/export", "signed_url": "https://signed.example/private"},
    )

    encoded = json.dumps(sidecar)
    assert "private-bucket" not in encoded
    assert "/runtime/storage" not in encoded
    assert "signed.example" not in encoded
    assert "secret" not in encoded


def test_revision_allows_multiple_samples_from_one_published_episode(db_session):
    actor = db_session.query(User).order_by(User.id).first()
    workspace = Workspace(name=f"sample workspace {uuid4().hex}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=actor.id))
    project = TaskSet(workspace_id=workspace.id, name=f"sample project {uuid4().hex}")
    db_session.add(project)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="sample batch",
        batch_type="ego",
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(
        Episode(
            workspace_id=workspace.id,
            task_set_id=project.id,
            batch_id=batch.id,
            episode_uid=f"unpublished-{uuid4().hex}",
            kind="source",
            modality="ego",
            source_fingerprint="f" * 64,
            metadata_json={"timing": {"start_timestamp_ns": 0, "end_timestamp_ns": 1000}},
        )
    )
    db_session.flush()
    episode = Episode(
        workspace_id=workspace.id,
        task_set_id=project.id,
        batch_id=batch.id,
        episode_uid=f"source-{uuid4().hex}",
        kind="source",
        modality="ego",
        source_fingerprint="e" * 64,
        task_language="pick up the box",
        metadata_json={"timing": {"start_timestamp_ns": 0, "end_timestamp_ns": 1000}},
    )
    db_session.add(episode)
    db_session.flush()
    artifact = EpisodeArtifact(
        episode_id=episode.id,
        artifact_type="official_qrdf",
        storage_role="official",
        storage_uri=f"nas://official/{uuid4().hex}",
        checksum_sha256="c" * 64,
        size_bytes=10,
    )
    db_session.add(artifact)
    db_session.flush()
    published = PublishedEpisode(
        episode_id=episode.id,
        official_artifact_id=artifact.id,
        publisher_user_id=actor.id,
        output_profile="qrdf",
        manifest_hash="d" * 64,
    )
    db_session.add(published)
    db_session.commit()

    item_specs = [
        DatasetItemInput(
            episode_id=episode.id,
            sample_id="sample-a",
            core_start_ns=100,
            core_end_ns=300,
            effective_start_ns=0,
            effective_end_ns=400,
            task="pick up the box",
        ),
        DatasetItemInput(
            episode_id=episode.id,
            sample_id="sample-a",
            core_start_ns=500,
            core_end_ns=700,
            effective_start_ns=400,
            effective_end_ns=800,
            task="pick up the box",
        ),
    ]
    episode.source_fingerprint = ""
    db_session.commit()
    with pytest.raises(WorkflowConflict, match="source fingerprint"):
        create_dataset_revision(
            db_session,
            workspace_id=workspace.id,
            name=f"samples-missing-fingerprint-{uuid4().hex}",
            actor_id=actor.id,
            items=item_specs,
        )
    episode.source_fingerprint = "e" * 64
    db_session.commit()

    episode.task_language = ""
    db_session.commit()
    with pytest.raises(WorkflowConflict, match="task is required"):
        create_dataset_revision(
            db_session,
            workspace_id=workspace.id,
            name=f"episode-missing-task-{uuid4().hex}",
            actor_id=actor.id,
            items=[DatasetItemInput(episode_id=episode.id)],
        )
    episode.task_language = "pick up the box"
    db_session.commit()

    revision = create_dataset_revision(
        db_session,
        workspace_id=workspace.id,
        name=f"samples-{uuid4().hex}",
        actor_id=actor.id,
        items=item_specs,
    )

    assert revision.episode_count == 2
    assert db_session.query(DatasetItem).filter_by(revision_id=revision.id).count() == 2

    with pytest.raises(WorkflowConflict, match="duplicate revision item"):
        create_dataset_revision(
            db_session,
            workspace_id=workspace.id,
            name=f"duplicate-sample-{uuid4().hex}",
            actor_id=actor.id,
            items=[item_specs[0], item_specs[0]],
        )

    artifact.storage_role = "raw"
    db_session.commit()
    with pytest.raises(WorkflowConflict, match="official artifact"):
        build_revision_manifest(db_session, revision.id)
