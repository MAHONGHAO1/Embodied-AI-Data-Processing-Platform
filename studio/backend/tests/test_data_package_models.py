"""Data package models: status values, assignment constraints, and dual valid duration."""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import PersonnelProfile, SessionLocal, Workspace
from data.models.collection_core import CollectionProject, CollectionTask
from data.models.data_package import DATA_PACKAGE_TERMINAL_STATUSES, DataPackage


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def task(db):
    workspace = Workspace(name=f"ws-pkg-{uuid4().hex}")
    db.add(workspace)
    db.commit()
    project = CollectionProject(workspace_id=workspace.id, name=f"P-{workspace.id}")
    db.add(project)
    db.commit()
    entry = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"T-{workspace.id}",
        target_duration_hours=8,
    )
    db.add(entry)
    db.commit()
    return entry


def _collector(db, key: str) -> PersonnelProfile:
    profile = PersonnelProfile(name=f"c{key}", profile_key=key)
    db.add(profile)
    db.commit()
    return profile


def _package(task, suffix: str, **overrides) -> DataPackage:
    fields = {
        "workspace_id": task.workspace_id,
        "collection_project_id": task.collection_project_id,
        "collection_task_id": task.id,
        "package_uid": f"pkg-{task.id}-{suffix}",
        "target_duration_hours": 2,
    }
    fields.update(overrides)
    return DataPackage(**fields)


def test_terminal_statuses_are_parse_failed_and_voided():
    assert DATA_PACKAGE_TERMINAL_STATUSES == frozenset({"parse_failed", "voided"})


def test_package_starts_pending_assignment(db, task):
    package = _package(task, "1")
    db.add(package)
    db.commit()
    assert package.status == "pending_assignment"
    assert package.intake_valid_duration_hours is None
    assert package.governed_valid_duration_hours is None


def test_package_rejects_unknown_status(db, task):
    db.add(_package(task, "2", status="reassigned"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_package_uid_is_globally_unique(db, task):
    db.add(_package(task, "dup"))
    db.commit()
    db.add(_package(task, "dup"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_assigned_package_requires_both_collectors(db, task):
    owner = _collector(db, f"{task.id}01")
    db.add(_package(task, "3", status="assigned", responsible_collector_id=owner.id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_assignment_with_both_collectors_succeeds(db, task):
    owner = _collector(db, f"{task.id}02")
    operator = _collector(db, f"{task.id}03")
    package = _package(
        task,
        "4",
        status="assigned",
        responsible_collector_id=owner.id,
        operator_collector_id=operator.id,
    )
    db.add(package)
    db.commit()
    assert package.responsible_collector.id == owner.id
    assert package.operator_collector.id == operator.id


def test_valid_durations_cannot_be_negative(db, task):
    db.add(_package(task, "5", intake_valid_duration_hours=-1))
    with pytest.raises(IntegrityError):
        db.commit()


def test_governed_duration_cannot_exceed_intake_duration(db, task):
    db.add(_package(task, "6", intake_valid_duration_hours=2, governed_valid_duration_hours=3))
    with pytest.raises(IntegrityError):
        db.commit()
