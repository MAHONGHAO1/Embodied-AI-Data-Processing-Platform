"""Intake review records and Episode validity tri-state."""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import Batch, Episode, SessionLocal, TaskSet, Workspace
from data.models.collection_core import CollectionProject, CollectionTask
from data.models.data_package import (
    EPISODE_VALIDITY_STATUSES,
    DataPackage,
    PackageIntakeReview,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def package(db):
    workspace = Workspace(name=f"ws-intake-{uuid4().hex}")
    db.add(workspace)
    db.commit()
    project = CollectionProject(workspace_id=workspace.id, name=f"P-{workspace.id}")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"T-{workspace.id}",
        target_duration_hours=8,
    )
    db.add(task)
    db.commit()
    entry = DataPackage(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        package_uid=f"pkg-intake-{workspace.id}",
        target_duration_hours=2,
    )
    db.add(entry)
    db.commit()
    return entry


@pytest.fixture
def legacy_scope(db):
    """Episode still retains legacy non-null foreign keys task_set_id / batch_id; provides minimal records here."""
    task_set = db.query(TaskSet).order_by(TaskSet.id).first()
    batch = db.query(Batch).order_by(Batch.id).first()
    if task_set is None or batch is None:
        workspace = Workspace(name=f"ws-legacy-episode-{uuid4().hex}")
        db.add(workspace)
        db.commit()
        task_set = TaskSet(workspace_id=workspace.id, name=f"ts-legacy-{workspace.id}")
        db.add(task_set)
        db.commit()
        batch = Batch(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            name=f"batch-legacy-{workspace.id}",
            batch_type="ego",
        )
        db.add(batch)
        db.commit()
    return task_set.id, batch.id


def _episode(package, legacy_scope, suffix: str, **overrides) -> Episode:
    task_set_id, batch_id = legacy_scope
    fields = {
        "episode_uid": f"ep-{package.id}-{suffix}",
        "workspace_id": package.workspace_id,
        "task_set_id": task_set_id,
        "batch_id": batch_id,
        "kind": "source",
        "modality": "video",
    }
    fields.update(overrides)
    return Episode(**fields)


def test_validity_statuses_are_the_specified_three():
    assert set(EPISODE_VALIDITY_STATUSES) == {"valid", "intake_rejected", "qc_dropped"}


def test_episode_defaults_to_valid(db, package, legacy_scope):
    episode = _episode(package, legacy_scope, "1")
    db.add(episode)
    db.commit()
    assert episode.validity_status == "valid"
    assert episode.data_package_id is None


def test_episode_rejects_unknown_validity_status(db, package, legacy_scope):
    db.add(_episode(package, legacy_scope, "2", validity_status="discarded"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_episode_can_belong_to_a_data_package(db, package, legacy_scope):
    episode = _episode(
        package,
        legacy_scope,
        "3",
        data_package_id=package.id,
        task_set_id=None,
        batch_id=None,
    )
    db.add(episode)
    db.commit()
    assert episode.data_package_id == package.id


def test_intake_review_records_verdict_and_bulk_flag(db, package):
    review = PackageIntakeReview(
        data_package_id=package.id,
        verdict="approved",
        is_bulk=True,
        rejected_episode_ids_json=[],
    )
    db.add(review)
    db.commit()
    assert review.verdict == "approved"
    assert review.is_bulk is True


def test_intake_review_rejects_unknown_verdict(db, package):
    db.add(PackageIntakeReview(data_package_id=package.id, verdict="partial"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_package_can_have_multiple_episode_scoped_intake_reviews(db, package):
    db.add(PackageIntakeReview(data_package_id=package.id, verdict="approved"))
    db.commit()
    db.add(PackageIntakeReview(data_package_id=package.id, verdict="rejected"))
    db.commit()
    assert (
        db.query(PackageIntakeReview)
        .filter(PackageIntakeReview.data_package_id == package.id)
        .count()
        == 2
    )
