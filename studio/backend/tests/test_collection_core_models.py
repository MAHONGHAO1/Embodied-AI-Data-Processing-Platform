"""Collection project and collection task models."""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import SessionLocal, Workspace
from data.models.collection_config import CollectionDeviceModel, CollectionLabel
from data.models.collection_core import CollectionProject, CollectionTask, CollectionTaskLabel


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def workspace(db):
    entry = Workspace(name=f"ws-core-{uuid4().hex}")
    db.add(entry)
    db.commit()
    return entry


def test_project_defaults_to_enabled(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="厨房采集")
    db.add(project)
    db.commit()
    assert project.status == "enabled"


def test_project_rejects_unknown_status(db, workspace):
    db.add(CollectionProject(workspace_id=workspace.id, name="X", status="paused"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_project_name_is_unique_within_workspace_ignoring_padding(db, workspace):
    db.add(CollectionProject(workspace_id=workspace.id, name="厨房"))
    db.commit()
    db.add(CollectionProject(workspace_id=workspace.id, name="  厨房 "))
    with pytest.raises(IntegrityError):
        db.commit()


def test_task_capture_mode_is_offline_only(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P1")
    db.add(project)
    db.commit()
    db.add(
        CollectionTask(
            workspace_id=workspace.id,
            collection_project_id=project.id,
            name="T1",
            target_duration_hours=10,
            capture_mode="online",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_task_defaults(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P2")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="T2",
        target_duration_hours=10,
    )
    db.add(task)
    db.commit()
    assert task.capture_mode == "offline"
    assert float(task.default_package_duration_hours) == 2.0


def test_task_target_duration_must_be_positive(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P3")
    db.add(project)
    db.commit()
    db.add(
        CollectionTask(
            workspace_id=workspace.id,
            collection_project_id=project.id,
            name="T3",
            target_duration_hours=0,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_task_accepts_multiple_labels_across_categories(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P4")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="T4",
        target_duration_hours=4,
    )
    scene = CollectionLabel(category="scene", name=f"客厅-{workspace.id}")
    purpose = CollectionLabel(category="purpose", name=f"正式任务-{workspace.id}")
    db.add_all([task, scene, purpose])
    db.commit()
    db.add_all(
        [
            CollectionTaskLabel(collection_task_id=task.id, collection_label_id=scene.id),
            CollectionTaskLabel(collection_task_id=task.id, collection_label_id=purpose.id),
        ]
    )
    db.commit()
    assert db.query(CollectionTaskLabel).filter_by(collection_task_id=task.id).count() == 2


def test_task_label_pair_cannot_repeat(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P5")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="T5",
        target_duration_hours=4,
    )
    label = CollectionLabel(category="training", name=f"后训练-{workspace.id}")
    db.add_all([task, label])
    db.commit()
    db.add(CollectionTaskLabel(collection_task_id=task.id, collection_label_id=label.id))
    db.commit()
    db.add(CollectionTaskLabel(collection_task_id=task.id, collection_label_id=label.id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_task_binds_one_device_model(db, workspace):
    project = CollectionProject(workspace_id=workspace.id, name="P6")
    device_model = CollectionDeviceModel(
        vendor=f"V{workspace.id}", model="M1", device_type="iphone"
    )
    db.add_all([project, device_model])
    db.commit()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="T6",
        target_duration_hours=4,
        device_model_id=device_model.id,
    )
    db.add(task)
    db.commit()
    assert task.device_model.model == "M1"
