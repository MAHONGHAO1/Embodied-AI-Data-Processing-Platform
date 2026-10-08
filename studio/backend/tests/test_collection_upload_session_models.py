"""Collection upload session model and Episode collection ownership."""

from uuid import uuid4

import pytest

from data.database import Episode, SessionLocal, Workspace
from data.models.collection_core import CollectionProject, CollectionTask
from data.models.collection_upload import CollectionUploadSession, CollectionUploadSessionPackage
from data.models.data_package import DataPackage


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _scope(db):
    from data.database import PersonnelProfile

    ws = Workspace(name=f"ws-up-{uuid4().hex}")
    db.add(ws)
    db.commit()
    project = CollectionProject(workspace_id=ws.id, name=f"P-{ws.id}")
    db.add(project)
    db.commit()
    task = CollectionTask(
        workspace_id=ws.id,
        collection_project_id=project.id,
        name=f"T-{ws.id}",
        target_duration_hours=2,
    )
    db.add(task)
    db.commit()
    owner = PersonnelProfile(name="o", profile_key=str(uuid4().int))
    operator = PersonnelProfile(name="p", profile_key=str(uuid4().int))
    db.add_all([owner, operator])
    db.commit()
    return ws, project, task, owner, operator


def test_upload_session_can_link_multiple_packages(db):
    ws, project, task, owner, operator = _scope(db)
    packages = []
    for _ in range(2):
        pkg = DataPackage(
            workspace_id=ws.id,
            collection_project_id=project.id,
            collection_task_id=task.id,
            package_uid=f"pkg_{uuid4().hex}",
            target_duration_hours=1,
            status="assigned",
            responsible_collector_id=owner.id,
            operator_collector_id=operator.id,
        )
        db.add(pkg)
        packages.append(pkg)
    db.commit()
    session = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=ws.id,
        collection_project_id=project.id,
        status="init",
        upload_mode="duance_sdk",
        created_by_user_id=None,
    )
    db.add(session)
    db.flush()
    for pkg in packages:
        db.add(
            CollectionUploadSessionPackage(
                upload_session_id=session.id,
                data_package_id=pkg.id,
                package_uid=pkg.package_uid,
            )
        )
    db.commit()
    assert len(session.package_links) == 2


def test_collection_episode_does_not_require_legacy_batch(db):
    ws, project, task, owner, operator = _scope(db)
    pkg = DataPackage(
        workspace_id=ws.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        package_uid=f"pkg_{uuid4().hex}",
        target_duration_hours=1,
        status="assigned",
        responsible_collector_id=owner.id,
        operator_collector_id=operator.id,
    )
    db.add(pkg)
    db.commit()
    episode = Episode(
        episode_uid=f"ep-{uuid4().hex}",
        workspace_id=ws.id,
        task_set_id=None,
        batch_id=None,
        data_package_id=pkg.id,
        kind="source",
        modality="rgb",
        validity_status="valid",
    )
    db.add(episode)
    db.commit()
    assert episode.data_package_id == pkg.id
    assert episode.batch_id is None
