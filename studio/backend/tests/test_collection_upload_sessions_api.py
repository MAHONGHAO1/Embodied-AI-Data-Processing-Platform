"""Collection upload session: creation, package binding, querying, and cancellation."""

from decimal import Decimal
from uuid import uuid4

from tests.collection_api_fixtures import (
    make_assigned_package,
    make_device_model,
    make_project,
    make_workspace,
)

from data.models.collection_upload import CollectionUploadSessionPackage
from data.models.data_package import DataPackage
from data.services.collection_tasks import create_collection_task


def _create(client, headers, workspace, project, package, *, mode="duance_sdk"):
    return client.post(
        "/api/v1/upload-sessions",
        headers=headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [package.package_uid],
            "upload_mode": mode,
        },
    )


def test_create_upload_session_binds_assigned_packages(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)

    response = _create(client, admin_headers, workspace, project, package)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "init"
    assert data["upload_mode"] == "duance_sdk"
    assert package.package_uid in {item["package_uid"] for item in data["packages"]}
    db_session.expire_all()
    assert db_session.get(DataPackage, package.id).status == "pending_upload"
    link = (
        db_session.query(CollectionUploadSessionPackage)
        .filter_by(upload_session_id=data["id"], data_package_id=package.id)
        .one()
    )
    assert link.package_uid == db_session.get(DataPackage, package.id).package_uid


def test_create_upload_session_rejects_pending_assignment(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = create_collection_task(
        db_session,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="unassigned",
        target_duration_hours=Decimal("2.00"),
        default_package_duration_hours=Decimal("2.00"),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    db_session.commit()
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()

    response = _create(client, admin_headers, workspace, project, package, mode="chunked")

    assert response.status_code == 409
    assert "package_not_uploadable" in response.json()["detail"]


def test_create_upload_session_rejects_unknown_and_cross_project_packages(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    other_project = make_project(db_session, workspace)
    foreign_package = make_assigned_package(db_session, workspace, other_project)

    unknown = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [f"pkg_{uuid4().hex}"],
            "upload_mode": "duance_sdk",
        },
    )
    mismatch = _create(client, admin_headers, workspace, project, foreign_package)

    assert unknown.status_code in {404, 409}
    assert mismatch.status_code == 404
    assert "package_project_mismatch" in mismatch.json()["detail"]


def test_create_upload_session_rejects_empty_packages_and_archived_project(
    client, db_session, admin_headers
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    project.status = "archived"
    db_session.commit()

    empty = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [],
            "upload_mode": "duance_sdk",
        },
    )
    archived = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [f"pkg_{uuid4().hex}"],
            "upload_mode": "duance_sdk",
        },
    )

    assert empty.status_code == 422
    assert archived.status_code == 409
    assert "archived" in archived.json()["detail"]


def test_package_cannot_join_a_second_active_session(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    first = _create(client, admin_headers, workspace, project, package)
    assert first.status_code == 200
    db_session.expire_all()
    persisted = db_session.get(DataPackage, package.id)

    second = _create(client, admin_headers, workspace, project, persisted)

    assert second.status_code == 409
    assert "package_session_busy" in second.json()["detail"]


def test_get_and_list_upload_sessions_are_workspace_scoped(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    created = _create(client, admin_headers, workspace, project, package).json()["data"]
    other_workspace = make_workspace(db_session)

    detail = client.get(
        f"/api/v1/upload-sessions/{created['id']}",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    listed = client.get(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "status": "init",
        },
    )
    hidden = client.get(
        f"/api/v1/upload-sessions/{created['id']}",
        headers=admin_headers,
        params={"workspace_id": other_workspace.id},
    )

    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == created["id"]
    assert [item["id"] for item in listed.json()["data"]["items"]] == [created["id"]]
    assert hidden.status_code == 404


def test_cancel_restores_each_package_prior_status(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    assigned = make_assigned_package(db_session, workspace, project)
    parse_failed = make_assigned_package(db_session, workspace, project)
    parse_failed.status = "parse_failed"
    db_session.commit()
    created = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [assigned.package_uid, parse_failed.package_uid],
            "upload_mode": "oss_multipart",
        },
    )
    assert created.status_code == 200

    cancelled = client.post(
        f"/api/v1/upload-sessions/{created.json()['data']['id']}/cancel",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"
    db_session.expire_all()
    assert db_session.get(DataPackage, assigned.id).status == "assigned"
    assert db_session.get(DataPackage, parse_failed.id).status == "parse_failed"


def test_cancel_rejects_terminal_session(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    created = _create(client, admin_headers, workspace, project, package).json()["data"]
    first = client.post(
        f"/api/v1/upload-sessions/{created['id']}/cancel",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    second = client.post(
        f"/api/v1/upload-sessions/{created['id']}/cancel",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )

    assert first.status_code == 200
    assert second.status_code == 409
