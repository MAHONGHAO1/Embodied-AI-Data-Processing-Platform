from __future__ import annotations

from uuid import uuid4

import pytest

from data.database import ExternalProjectRef, TaskSet, Workspace, WorkspaceMember


def _workspace(db_session) -> Workspace:
    workspace = Workspace(name=f"task set api {uuid4().hex}", creator="test")
    db_session.add(workspace)
    db_session.commit()
    return workspace


def test_task_set_create_returns_unbound_external_project(client, db_session, admin_headers):
    workspace = _workspace(db_session)

    response = client.post(
        "/api/v1/workspace/task-set/create",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": "Warehouse operations",
            "description": "Packing collection",
            "scene": "warehouse",
        },
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["workspace_id"] == workspace.id
    assert payload["name"] == "Warehouse operations"
    assert payload["external_project"] is None
    assert "project_id" not in payload


def test_task_set_create_rejects_client_supplied_external_binding(
    client, db_session, admin_headers
):
    workspace = _workspace(db_session)

    response = client.post(
        "/api/v1/workspace/task-set/create",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": "Forged binding",
            "external_project_ref_id": 42,
        },
    )

    assert response.status_code == 422


def test_task_set_create_rejects_a_blank_name(client, db_session, admin_headers):
    workspace = _workspace(db_session)

    response = client.post(
        "/api/v1/workspace/task-set/create",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "name": "   "},
    )

    assert response.status_code == 422


def test_task_set_list_projects_only_safe_external_reference_fields(
    client, db_session, admin_headers
):
    workspace = _workspace(db_session)
    external_project = ExternalProjectRef(
        workspace_id=workspace.id,
        provider="erp",
        external_project_id="external-42",
        display_name="Warehouse rollout",
        metadata_json={"secret_hint": "must-not-leak"},
    )
    db_session.add(external_project)
    db_session.flush()
    task_set = TaskSet(
        workspace_id=workspace.id,
        name="Bound task set",
        external_project_ref_id=external_project.id,
    )
    db_session.add(task_set)
    db_session.commit()

    response = client.get(
        "/api/v1/workspace/task-set/list",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )

    assert response.status_code == 200
    projection = response.json()["data"]["list"][0]["external_project"]
    assert projection == {
        "provider": "erp",
        "external_project_id": "external-42",
        "display_name": "Warehouse rollout",
        "status": "active",
        "synced_at": None,
    }
    assert "metadata_json" not in projection


def test_legacy_project_routes_are_not_registered(client, db_session, admin_headers):
    workspace = _workspace(db_session)

    response = client.get(
        "/api/v1/workspace/project/list",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )

    assert response.status_code == 404


def test_collection_batch_routes_are_not_registered(client, db_session, admin_headers):
    workspace = _workspace(db_session)

    created = client.post(
        "/api/v1/batches",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "name": "Missing scope", "batch_type": "ego"},
    )
    listed = client.get(
        "/api/v1/batches", headers=admin_headers, params={"workspace_id": workspace.id}
    )

    assert created.status_code in {404, 405}, created.text
    assert listed.status_code == 404


def test_operator_can_write_member_workspace_but_not_non_member_workspace(
    client, db_session, operator_headers
):
    allowed = _workspace(db_session)
    denied = _workspace(db_session)
    operator_id = int(client.get("/api/v1/auth/me", headers=operator_headers).json()["data"]["id"])
    db_session.add(WorkspaceMember(workspace_id=allowed.id, user_id=operator_id))
    db_session.commit()

    allowed_response = client.post(
        "/api/v1/workspace/task-set/create",
        headers=operator_headers,
        json={"workspace_id": allowed.id, "name": "Allowed"},
    )
    denied_response = client.post(
        "/api/v1/workspace/task-set/create",
        headers=operator_headers,
        json={"workspace_id": denied.id, "name": "Denied"},
    )

    assert allowed_response.status_code == 200
    assert denied_response.status_code == 403


@pytest.mark.parametrize(
    "headers_fixture", ["annotator_headers", "auditor_headers", "viewer_headers"]
)
def test_membership_does_not_expand_platform_role_permissions(
    client, db_session, request, headers_fixture
):
    headers = request.getfixturevalue(headers_fixture)
    workspace = _workspace(db_session)
    actor_id = int(client.get("/api/v1/auth/me", headers=headers).json()["data"]["id"])
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=actor_id))
    db_session.commit()

    response = client.post(
        "/api/v1/workspace/task-set/create",
        headers=headers,
        json={"workspace_id": workspace.id, "name": "Forbidden"},
    )

    assert response.status_code == 403


def test_workspace_and_task_set_options_prefer_newest_first(client, db_session, admin_headers):
    older = Workspace(name=f"older workspace {uuid4().hex}", creator="test")
    newer = Workspace(name=f"newer workspace {uuid4().hex}", creator="test")
    db_session.add_all([older, newer])
    db_session.flush()
    older_task = TaskSet(workspace_id=newer.id, name="older task set")
    newer_task = TaskSet(workspace_id=newer.id, name="newer task set")
    db_session.add_all([older_task, newer_task])
    db_session.commit()

    workspaces = client.get("/api/v1/workspace/options", headers=admin_headers)
    assert workspaces.status_code == 200
    workspace_ids = [row["id"] for row in workspaces.json()["data"]["list"]]
    assert workspace_ids.index(newer.id) < workspace_ids.index(older.id)
    assert "created_at" in workspaces.json()["data"]["list"][0]

    task_sets = client.get(
        "/api/v1/workspace/task-set/options",
        headers=admin_headers,
        params={"workspace_id": newer.id},
    )
    assert task_sets.status_code == 200
    names = [row["name"] for row in task_sets.json()["data"]["list"]]
    assert names.index("newer task set") < names.index("older task set")
    assert "created_at" in task_sets.json()["data"]["list"][0]
