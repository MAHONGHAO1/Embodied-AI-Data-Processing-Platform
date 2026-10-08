from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from collector_fixtures import make_collector

from data.database import (
    Batch,
    CollectionDevice,
    Dataset,
    DatasetRevision,
    Episode,
    EpisodeArtifact,
    PublishedEpisode,
    TaskSet,
    User,
    WorkItem,
    Workspace,
    WorkspaceMember,
)


def _workspace_fixture(db_session):
    actor = db_session.query(User).order_by(User.id).first()
    suffix = uuid4().hex
    workspace = Workspace(name=f"list-workspace-{suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=actor.id))
    task_sets = [
        TaskSet(workspace_id=workspace.id, name=f"list-task-a-{suffix}"),
        TaskSet(workspace_id=workspace.id, name=f"list-task-b-{suffix}"),
    ]
    db_session.add_all(task_sets)
    db_session.flush()
    batches = [
        Batch(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            name=f"list-batch-{index}-{suffix}",
            batch_type="ego",
        )
        for index, task_set in enumerate(task_sets)
    ]
    db_session.add_all(batches)
    db_session.flush()
    return actor, workspace, task_sets, batches


def _episode(db_session, *, workspace, task_set, batch, suffix, kind="source"):
    episode = Episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        episode_uid=f"list-{suffix}-{uuid4().hex}",
        kind=kind,
        modality="ego",
        quality_status="passed",
        metadata_json={"metrics": {"duration_s": 3.0}},
    )
    db_session.add(episode)
    db_session.flush()
    return episode


def _publish(db_session, *, actor, episode, checksum="a" * 64):
    artifact = EpisodeArtifact(
        episode_id=episode.id,
        artifact_type="official_qrdf",
        storage_role="official",
        storage_uri=f"oss://official/{uuid4().hex}.qrdf",
        checksum_sha256=checksum,
        size_bytes=128,
    )
    db_session.add(artifact)
    db_session.flush()
    published = PublishedEpisode(
        episode_id=episode.id,
        official_artifact_id=artifact.id,
        publisher_user_id=actor.id,
        output_profile="qrdf",
        manifest_hash="b" * 64,
    )
    db_session.add(published)
    db_session.flush()
    return published


def test_assets_return_only_valid_published_episodes_across_task_sets(db_session):
    from data.services.episodes import list_episode_assets_page

    actor, workspace, task_sets, batches = _workspace_fixture(db_session)
    first = _episode(
        db_session,
        workspace=workspace,
        task_set=task_sets[0],
        batch=batches[0],
        suffix="first",
    )
    second = _episode(
        db_session,
        workspace=workspace,
        task_set=task_sets[1],
        batch=batches[1],
        suffix="second",
    )
    unpublished = _episode(
        db_session,
        workspace=workspace,
        task_set=task_sets[0],
        batch=batches[0],
        suffix="unpublished",
    )
    invalid = _episode(
        db_session,
        workspace=workspace,
        task_set=task_sets[1],
        batch=batches[1],
        suffix="invalid",
    )
    first_publication = _publish(db_session, actor=actor, episode=first)
    second_publication = _publish(db_session, actor=actor, episode=second)
    _publish(db_session, actor=actor, episode=invalid, checksum="bad")
    db_session.commit()

    rows, total = list_episode_assets_page(
        db_session,
        workspace_id=workspace.id,
        limit=50,
        offset=0,
        sort_by="published_at",
        sort_order="asc",
    )

    assert total == 2
    assert [episode.id for episode, _publication in rows] == [first.id, second.id]
    assert [publication.id for _episode, publication in rows] == [
        first_publication.id,
        second_publication.id,
    ]
    assert unpublished.id not in {episode.id for episode, _publication in rows}
    assert invalid.id not in {episode.id for episode, _publication in rows}


def test_assets_are_workspace_isolated_and_allow_explicit_task_set_filter(db_session):
    from data.services.episodes import list_episode_assets_page

    actor, workspace, task_sets, batches = _workspace_fixture(db_session)
    first = _episode(
        db_session,
        workspace=workspace,
        task_set=task_sets[0],
        batch=batches[0],
        suffix="first",
    )
    second = _episode(
        db_session,
        workspace=workspace,
        task_set=task_sets[1],
        batch=batches[1],
        suffix="second",
    )
    _publish(db_session, actor=actor, episode=first)
    _publish(db_session, actor=actor, episode=second)
    foreign_actor, foreign_workspace, foreign_task_sets, foreign_batches = _workspace_fixture(
        db_session
    )
    foreign = _episode(
        db_session,
        workspace=foreign_workspace,
        task_set=foreign_task_sets[0],
        batch=foreign_batches[0],
        suffix="foreign",
    )
    _publish(db_session, actor=foreign_actor, episode=foreign)
    db_session.commit()

    rows, total = list_episode_assets_page(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_sets[1].id,
        limit=50,
        offset=0,
    )

    assert total == 1
    assert [episode.id for episode, _publication in rows] == [second.id]


def test_asset_filter_resources_must_belong_to_requested_workspace(
    client,
    admin_headers,
    db_session,
):
    _actor, workspace, _task_sets, _batches = _workspace_fixture(db_session)
    _foreign_actor, foreign_workspace, _foreign_task_sets, _foreign_batches = _workspace_fixture(
        db_session
    )
    collector = make_collector(
        db_session,
        workspace_id=foreign_workspace.id,
        name=f"foreign-collector-{uuid4().hex}",
    )
    device = CollectionDevice(
        workspace_id=foreign_workspace.id,
        name=f"foreign-device-{uuid4().hex}",
        device_type="iphone",
        serial_number=f"foreign-{uuid4().hex}",
    )
    db_session.add_all([collector, device])
    db_session.commit()

    for parameter, value in (
        ("collector_profile_id", collector.id),
        ("collection_device_id", device.id),
    ):
        response = client.get(
            "/api/v1/episodes/assets",
            params={"workspace_id": workspace.id, parameter: value},
            headers=admin_headers,
        )
        assert response.status_code == 403

    from data.services.collector_profiles import add_workspace_membership

    add_workspace_membership(db_session, workspace_id=workspace.id, profile_id=collector.id)
    collector.is_active = False
    db_session.commit()
    for member_workspace in (workspace, foreign_workspace):
        response = client.get(
            "/api/v1/episodes/assets",
            params={"workspace_id": member_workspace.id, "collector_profile_id": collector.id},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text


def test_work_queue_sort_is_stable_for_equal_operation_times(db_session):
    from data.services.work_queue_projection import list_work_queue

    actor, workspace, task_sets, batches = _workspace_fixture(db_session)
    fixed_time = datetime(2026, 8, 24, 10, 0, 0)
    item_ids = []
    for index in range(3):
        episode = _episode(
            db_session,
            workspace=workspace,
            task_set=task_sets[index % 2],
            batch=batches[index % 2],
            suffix=f"queue-{index}",
        )
        item = WorkItem(
            workspace_id=workspace.id,
            episode_id=episode.id,
            kind="cut",
            status="pending",
            created_by_user_id=actor.id,
            created_at=fixed_time,
            updated_at=fixed_time,
        )
        db_session.add(item)
        db_session.flush()
        item_ids.append(item.id)
    db_session.commit()

    page = list_work_queue(
        db_session,
        workspace_id=workspace.id,
        actor_id=actor.id,
        stage="cut",
        sort_by="updated_at",
        sort_order="desc",
    )

    assert [row["work_item"]["id"] for row in page["items"]] == sorted(item_ids, reverse=True)
    assert all(row["work_item"]["updated_at"] for row in page["items"])


def test_duration_sort_tolerates_malformed_legacy_metrics(db_session):
    from data.services.episodes import list_episode_assets_page
    from data.services.work_queue_projection import list_work_queue

    actor, workspace, task_sets, batches = _workspace_fixture(db_session)
    malformed = _episode(
        db_session,
        workspace=workspace,
        task_set=task_sets[0],
        batch=batches[0],
        suffix="malformed-duration",
    )
    malformed.metadata_json = {"metrics": {"duration_s": "unknown"}}
    _publish(db_session, actor=actor, episode=malformed)
    item = WorkItem(
        workspace_id=workspace.id,
        episode_id=malformed.id,
        kind="cut",
        status="pending",
        created_by_user_id=actor.id,
    )
    db_session.add(item)
    db_session.commit()

    asset_rows, asset_total = list_episode_assets_page(
        db_session,
        workspace_id=workspace.id,
        limit=50,
        offset=0,
        sort_by="duration",
        sort_order="asc",
    )
    queue_page = list_work_queue(
        db_session,
        workspace_id=workspace.id,
        actor_id=actor.id,
        stage="cut",
        sort_by="duration",
        sort_order="asc",
    )

    assert asset_total == 1
    assert [episode.id for episode, _publication in asset_rows] == [malformed.id]
    assert [row["work_item"]["id"] for row in queue_page["items"]] == [item.id]


def test_work_queue_rejects_unknown_sort_fields(db_session):
    from data.services.work_queue_projection import list_work_queue

    actor, workspace, _task_sets, _batches = _workspace_fixture(db_session)
    with pytest.raises(ValueError, match="invalid work queue sort"):
        list_work_queue(
            db_session,
            workspace_id=workspace.id,
            actor_id=actor.id,
            stage="cut",
            sort_by="draft_json",
        )


def test_dataset_summaries_filter_and_sort_workspace_aggregates(db_session):
    from data.services.dataset_revisions import list_dataset_summaries

    actor, workspace, _task_sets, _batches = _workspace_fixture(db_session)
    old = Dataset(workspace_id=workspace.id, name=f"old-{uuid4().hex}", created_by_user_id=actor.id)
    recent = Dataset(
        workspace_id=workspace.id, name=f"recent-{uuid4().hex}", created_by_user_id=actor.id
    )
    db_session.add_all([old, recent])
    db_session.flush()
    db_session.add_all(
        [
            DatasetRevision(
                dataset_id=recent.id,
                workspace_id=workspace.id,
                version=version,
                created_by_user_id=actor.id,
                filter_json={},
                manifest_hash=uuid4().hex.ljust(64, "0")[:64],
                episode_count=0,
            )
            for version in (1, 2)
        ]
    )
    db_session.commit()

    rows, total = list_dataset_summaries(
        db_session,
        workspace_id=workspace.id,
        limit=50,
        offset=0,
        keyword="recent",
        created_by_user_id=actor.id,
        sort_by="revision_count",
        sort_order="desc",
    )

    assert total == 1
    assert [row["id"] for row in rows] == [recent.id]
    assert rows[0]["revision_count"] == 2
    assert rows[0]["latest_revision_at"] is not None
    assert rows[0]["created_by_user_id"] == actor.id
