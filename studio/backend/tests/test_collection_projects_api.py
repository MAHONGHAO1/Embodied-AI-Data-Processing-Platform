"""Collection project management API."""

from uuid import uuid4

from collection_api_fixtures import make_project, make_workspace

from data.database import User


def test_create_list_and_archive_project(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    created = client.post(
        "/api/v1/collection-projects",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "name": "Alpha", "description": "demo"},
    )
    assert created.status_code == 200
    project_id = created.json()["data"]["id"]
    assert created.json()["data"]["status"] == "enabled"

    listed = client.get(
        "/api/v1/collection-projects",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert listed.status_code == 200
    assert project_id in {row["id"] for row in listed.json()["data"]["items"]}

    archived = client.post(
        f"/api/v1/collection-projects/{project_id}/archive",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert archived.status_code == 200
    assert archived.json()["data"]["status"] == "archived"

    archived_again = client.post(
        f"/api/v1/collection-projects/{project_id}/archive",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert archived_again.status_code == 200


def test_duplicate_project_name_in_workspace_conflicts(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    make_project(db_session, workspace, name="Same")
    response = client.post(
        "/api/v1/collection-projects",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "name": " same "},
    )
    assert response.status_code == 409


def test_patch_project_and_filter_by_status(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    enabled = make_project(db_session, workspace, name="Enabled")
    archived = make_project(db_session, workspace, name="Archived")
    archived.status = "archived"
    db_session.commit()

    updated = client.patch(
        f"/api/v1/collection-projects/{enabled.id}",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "name": " Updated ", "description": " changed "},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["name"] == "Updated"
    assert updated.json()["data"]["description"] == "changed"

    listed = client.get(
        "/api/v1/collection-projects",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "status": "archived"},
    )
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]["items"]] == [archived.id]


def test_archived_project_cannot_be_patched(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    archived = client.post(
        f"/api/v1/collection-projects/{project.id}/archive",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert archived.status_code == 200

    response = client.patch(
        f"/api/v1/collection-projects/{project.id}",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "description": "nope"},
    )
    assert response.status_code == 409


def test_create_project_rejects_missing_owner(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    response = client.post(
        "/api/v1/collection-projects",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": "Missing owner",
            "owner_user_id": 2_147_483_647,
        },
    )
    assert response.status_code == 404


def test_update_project_rejects_inactive_owner(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    inactive_owner = User(
        email=f"inactive-owner-{uuid4().hex}@example.test",
        password_hash="not-used",
        role="viewer",
        is_active=False,
    )
    db_session.add(inactive_owner)
    db_session.commit()

    response = client.patch(
        f"/api/v1/collection-projects/{project.id}",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "owner_user_id": inactive_owner.id},
    )
    assert response.status_code == 404
