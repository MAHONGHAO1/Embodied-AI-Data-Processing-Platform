import pytest
from sqlalchemy.exc import IntegrityError

from data.database import Batch, CollectionDevice, Episode, ExternalProjectRef, TaskSet, Workspace


def test_batch_episode_relationships(db_session):
    workspace = Workspace(name="model relationship workspace", creator="test")
    db_session.add(workspace)
    db_session.flush()

    task_set = TaskSet(workspace_id=workspace.id, name="model relationship task set")
    batch = Batch(
        workspace_id=workspace.id,
        task_set=task_set,
        name="teleop import",
        batch_type="teleop",
    )
    source = Episode(
        workspace_id=workspace.id,
        task_set=task_set,
        batch=batch,
        kind="source",
        modality="teleop",
        episode_uid="model-source-episode",
    )
    derived = Episode(
        workspace_id=workspace.id,
        task_set=task_set,
        batch=batch,
        kind="derived",
        modality="teleop",
        episode_uid="model-derived-episode",
        parent=source,
        source_start_ns=10,
        source_end_ns=20,
    )

    db_session.add_all([task_set, batch, source, derived])
    db_session.commit()

    assert derived.parent_episode_id == source.id
    assert {episode.id for episode in batch.episodes} == {source.id, derived.id}
    assert source.derived_episodes == [derived]


def test_batch_contract_only_keeps_modality_fields(db_session):
    workspace = Workspace(name="narrow batch workspace", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name="narrow batch task set")
    batch = Batch(
        workspace_id=workspace.id,
        task_set=task_set,
        name="ego import",
        batch_type="ego",
    )
    db_session.add_all([task_set, batch])
    db_session.flush()

    assert not hasattr(batch, "pipeline_key")
    assert not hasattr(batch, "source_type")


def test_task_set_external_project_projection_defaults_to_unbound(db_session):
    workspace = Workspace(name="unbound task set workspace", creator="test")
    task_set = TaskSet(workspace=workspace, name="unbound task set")
    db_session.add(task_set)
    db_session.commit()

    assert task_set.external_project_ref_id is None
    assert task_set.external_project is None


def test_task_set_external_project_reference_is_workspace_scoped(db_session):
    first = Workspace(name="external project first workspace", creator="test")
    second = Workspace(name="external project second workspace", creator="test")
    db_session.add_all([first, second])
    db_session.flush()
    external_project = ExternalProjectRef(
        workspace_id=second.id,
        provider="erp",
        external_project_id="project-2",
        display_name="External",
    )
    db_session.add(external_project)
    db_session.flush()
    db_session.add(
        TaskSet(
            workspace_id=first.id,
            name="invalid cross workspace task set",
            external_project_ref_id=external_project.id,
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_collection_device_sn_is_unique_inside_workspace(db_session):
    workspace = Workspace(name="device uniqueness workspace", creator="test")
    db_session.add(workspace)
    db_session.flush()
    db_session.add_all(
        [
            CollectionDevice(
                workspace_id=workspace.id,
                name="Phone A",
                device_type="phone",
                serial_number="SN-1",
            ),
            CollectionDevice(
                workspace_id=workspace.id,
                name="Phone B",
                device_type="phone",
                serial_number="SN-1",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
