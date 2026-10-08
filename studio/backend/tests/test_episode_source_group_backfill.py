"""Tests for the bounded, database-only legacy source-group backfill."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy import func, select

from data.database import Episode, TaskSet, User, Workspace
from data.services.batches import create_batch
from data.services.capture_provenance import backfill_episode_source_groups


def _source_context(db_session):
    suffix = uuid4().hex
    actor = db_session.query(User).order_by(User.id).first()
    assert actor is not None
    workspace = Workspace(name=f"source group backfill {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"source group backfill {suffix}")
    db_session.add(task_set)
    db_session.flush()
    batch = create_batch(
        db_session,
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"source group backfill {suffix}",
        batch_type="ego",
        actor_id=actor.id,
    )
    return workspace, task_set, batch


def _source_episode(
    *, workspace_id: int, task_set_id: int, batch_id: int, suffix: str, legacy_task_id: object
):
    return Episode(
        episode_uid=f"source-group-backfill-{suffix}",
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        batch_id=batch_id,
        kind="source",
        modality="ego",
        quality_status="passed",
        metadata_json={"import": {"legacy_ego_task_id": legacy_task_id}},
    )


def test_backfill_advances_by_episode_id_and_only_uses_reliable_legacy_metadata(db_session):
    cursor_before_fixture = db_session.scalar(select(func.max(Episode.id))) or 0
    workspace, task_set, batch = _source_context(db_session)
    valid = _source_episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        suffix="valid",
        legacy_task_id="  Morning  Line A  ",
    )
    absent = _source_episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        suffix="absent",
        legacy_task_id=None,
    )
    invalid = _source_episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        suffix="invalid",
        legacy_task_id="\x1cunsafe",
    )
    db_session.add_all((valid, absent, invalid))
    db_session.flush()

    first_page = backfill_episode_source_groups(
        db_session,
        limit=2,
        after_episode_id=cursor_before_fixture,
        apply=True,
    )

    assert first_page.scanned_count == 2
    assert first_page.eligible_count == 1
    assert first_page.updated_count == 1
    assert first_page.next_after_episode_id == absent.id
    assert valid.reported_source_group_name == "Morning Line A"
    assert valid.reported_source_group_key == (
        f"legacy_sg1_{hashlib.sha256(b'Morning Line A').hexdigest()}"
    )
    assert valid.reported_source_group_status == "legacy"
    assert absent.reported_source_group_status == "missing"

    db_session.commit()
    second_page = backfill_episode_source_groups(
        db_session,
        limit=2,
        after_episode_id=first_page.next_after_episode_id,
        apply=True,
    )

    assert second_page.scanned_count == 1
    assert second_page.eligible_count == 0
    assert second_page.updated_count == 0
    assert second_page.next_after_episode_id is None
    assert invalid.reported_source_group_status == "missing"


def test_backfill_check_mode_never_mutates_episode_rows(db_session):
    cursor_before_fixture = db_session.scalar(select(func.max(Episode.id))) or 0
    workspace, task_set, batch = _source_context(db_session)
    source = _source_episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        suffix="check-only",
        legacy_task_id="night shift",
    )
    db_session.add(source)
    db_session.flush()

    page = backfill_episode_source_groups(
        db_session,
        limit=10,
        after_episode_id=cursor_before_fixture,
        apply=False,
    )

    assert page.scanned_count == 1
    assert page.eligible_count == 1
    assert page.updated_count == 0
    assert source.reported_source_group_status == "missing"
