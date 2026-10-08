from __future__ import annotations

from uuid import uuid4

import pytest

from data.database import Batch, ExternalOssImportScope, TaskSet, Workspace


def _workspace_project(db_session):
    workspace = Workspace(name=f"settings-{uuid4().hex[:8]}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    project = TaskSet(workspace_id=workspace.id, name="ego")
    db_session.add(project)
    db_session.commit()
    return workspace, project


def test_platform_settings_are_admin_only(
    client,
    admin_headers,
    viewer_headers,
):
    denied = client.get("/api/v1/platform-settings", headers=viewer_headers)
    initial = client.get("/api/v1/platform-settings", headers=admin_headers)

    assert denied.status_code == 403
    assert initial.status_code == 200
    assert initial.json()["code"] == 200


def test_disabled_database_scope_is_not_returned(db_session):
    from data.services import oss_import_scope

    workspace, project = _workspace_project(db_session)
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="scope suppression",
        batch_type="ego",
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(
        ExternalOssImportScope(
            workspace_id=workspace.id,
            task_set_id=project.id,
            bucket="ego-source-data",
            prefixes_json=["prod/raw/v1/tasks"],
            is_enabled=False,
        )
    )
    db_session.commit()
    assert oss_import_scope.oss_import_scopes_for_batch(batch=batch) == []


def test_database_is_the_only_oss_import_scope_source(db_session):
    from data.services import oss_import_scope

    workspace, task_set = _workspace_project(db_session)
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name="database-only scope",
        batch_type="ego",
    )
    db_session.add(batch)
    db_session.commit()
    assert oss_import_scope.oss_import_scopes_for_batch(batch=batch) == []


def test_detached_batch_cannot_fall_back_to_environment_scope(db_session):
    from data.services.oss_import_scope import OssImportScopeError, oss_import_scopes_for_batch

    workspace, project = _workspace_project(db_session)
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=project.id,
        name="detached scope",
        batch_type="ego",
    )
    db_session.add(batch)
    db_session.commit()
    db_session.expunge(batch)

    with pytest.raises(OssImportScopeError, match="cannot be resolved"):
        oss_import_scopes_for_batch(batch=batch)
