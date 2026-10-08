"""Data batch models: one-time assignment, governance toggle, and review mode extension flags."""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import SessionLocal, Workspace
from data.models.collection_core import CollectionProject, CollectionTask
from data.models.data_batch import DataBatch, DataBatchPackage
from data.models.data_package import DataPackage


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def context(db):
    workspace = Workspace(name=f"ws-batch-{uuid4().hex}")
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
    return workspace, project, task


def _package(db, context, suffix: str) -> DataPackage:
    workspace, project, task = context
    entry = DataPackage(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        package_uid=f"pkg-batch-{workspace.id}-{suffix}",
        target_duration_hours=2,
    )
    db.add(entry)
    db.commit()
    return entry


def test_batch_defaults_to_single_review_and_no_governance(db, context):
    workspace, _, _ = context
    batch = DataBatch(workspace_id=workspace.id, name=f"B-{workspace.id}")
    db.add(batch)
    db.commit()
    assert batch.review_mode == "single"
    assert batch.integrity_check_enabled is False
    assert batch.quality_check_enabled is False
    assert batch.compliance_check_enabled is False
    assert batch.annotation_enabled is False


def test_batch_rejects_unknown_review_mode(db, context):
    workspace, _, _ = context
    db.add(DataBatch(workspace_id=workspace.id, name=f"B2-{workspace.id}", review_mode="triple"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_batch_name_is_unique_within_workspace_ignoring_padding(db, context):
    workspace, _, _ = context
    db.add(DataBatch(workspace_id=workspace.id, name="夜间批次"))
    db.commit()
    db.add(DataBatch(workspace_id=workspace.id, name=" 夜间批次 "))
    with pytest.raises(IntegrityError):
        db.commit()


def test_package_can_join_a_batch(db, context):
    workspace, _, _ = context
    batch = DataBatch(workspace_id=workspace.id, name=f"B3-{workspace.id}")
    package = _package(db, context, "a")
    db.add(batch)
    db.commit()
    db.add(DataBatchPackage(data_batch_id=batch.id, data_package_id=package.id))
    db.commit()
    assert db.query(DataBatchPackage).filter_by(data_batch_id=batch.id).count() == 1


def test_package_can_belong_to_two_batches_when_episode_members_differ(db, context):
    workspace, _, _ = context
    first = DataBatch(workspace_id=workspace.id, name=f"B4-{workspace.id}")
    second = DataBatch(workspace_id=workspace.id, name=f"B5-{workspace.id}")
    package = _package(db, context, "b")
    db.add_all([first, second])
    db.commit()
    db.add(DataBatchPackage(data_batch_id=first.id, data_package_id=package.id))
    db.commit()
    db.add(DataBatchPackage(data_batch_id=second.id, data_package_id=package.id))
    db.commit()
    assert db.query(DataBatchPackage).filter_by(data_package_id=package.id).count() == 2
