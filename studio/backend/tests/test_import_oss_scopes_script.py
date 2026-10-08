from __future__ import annotations

from uuid import uuid4

import pytest
from scripts.import_oss_scopes import OssScopeImportError, import_legacy_oss_scopes

from data.database import ExternalOssImportScope, TaskSet, Workspace


def _scope_target(db_session) -> tuple[Workspace, TaskSet]:
    suffix = uuid4().hex
    workspace = Workspace(name=f"legacy scope workspace {suffix}", creator="test")
    db_session.add(workspace)
    db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"legacy scope task set {suffix}")
    db_session.add(task_set)
    db_session.commit()
    return workspace, task_set


def test_legacy_oss_scope_import_is_idempotent(db_session):
    workspace, task_set = _scope_target(db_session)
    payload = [
        {
            "workspace_id": workspace.id,
            "task_set_id": task_set.id,
            "bucket": "legacy-source-bucket",
            "prefixes": ["prod/raw/v1/tasks/", "prod/manual/ego"],
        }
    ]

    first = import_legacy_oss_scopes(db_session, payload, apply=True)
    second = import_legacy_oss_scopes(db_session, payload, apply=True)

    assert first == {"created": 1, "updated": 0, "unchanged": 0}
    assert second == {"created": 0, "updated": 0, "unchanged": 1}
    row = (
        db_session.query(ExternalOssImportScope)
        .filter(
            ExternalOssImportScope.workspace_id == workspace.id,
            ExternalOssImportScope.task_set_id == task_set.id,
            ExternalOssImportScope.bucket == "legacy-source-bucket",
        )
        .one()
    )
    assert row.prefixes_json == ["prod/raw/v1/tasks", "prod/manual/ego"]
    assert row.is_enabled is True
    assert row.revision == 1


def test_legacy_oss_scope_check_fails_when_database_differs(db_session):
    workspace, task_set = _scope_target(db_session)
    payload = [
        {
            "workspace_id": workspace.id,
            "task_set_id": task_set.id,
            "bucket": "legacy-source-bucket",
            "prefixes": ["prod/raw/v1/tasks"],
        }
    ]

    with pytest.raises(OssScopeImportError, match="not synchronized"):
        import_legacy_oss_scopes(db_session, payload, check=True)


def test_legacy_oss_scope_import_rejects_unknown_workspace(db_session):
    with pytest.raises(OssScopeImportError, match="workspace does not exist"):
        import_legacy_oss_scopes(
            db_session,
            [
                {
                    "workspace_id": 999999,
                    "task_set_id": 999999,
                    "bucket": "legacy-source-bucket",
                    "prefixes": ["prod/raw/v1/tasks"],
                }
            ],
            apply=True,
        )
