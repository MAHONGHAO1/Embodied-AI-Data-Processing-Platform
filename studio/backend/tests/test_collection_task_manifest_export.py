"""Package level offline manifest for tools that collect without network access."""

from decimal import Decimal
from uuid import uuid4


def _seed(db_session):
    from data.database import Workspace, WorkspaceMember
    from data.models.collection_core import CollectionProject, CollectionTask
    from data.models.data_package import DataPackage

    suffix = uuid4().hex[:8]
    workspace = Workspace(name=f"Manifest workspace {suffix}")
    db_session.add(workspace)
    db_session.flush()
    project = CollectionProject(workspace_id=workspace.id, name=f"Manifest project {suffix}")
    db_session.add(project)
    db_session.flush()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"Manifest task {suffix}",
        target_duration_hours=Decimal("2.00"),
        default_package_duration_hours=Decimal("2.00"),
    )
    db_session.add(task)
    db_session.flush()
    package = DataPackage(
        package_uid=f"pkg-manifest-{suffix}",
        workspace_id=workspace.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        status="pending_assignment",
        target_duration_hours=Decimal("2.00"),
    )
    db_session.add(package)
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=1))
    db_session.commit()
    return workspace, task, package


def test_offline_manifest_rows_are_package_level(client, db_session, admin_headers):
    workspace, task, _ = _seed(db_session)

    response = client.get(
        f"/api/v1/collection-tasks/{task.id}/offline-manifest",
        params={"workspace_id": workspace.id, "format": "json"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    item = payload["items"][0]
    for key in (
        "package_uid",
        "collection_project",
        "collection_task",
        "expected_files",
        "required_metadata",
        "upload_status",
        "desensitization_status",
        "manifest_revision",
    ):
        assert key in item
    assert item["package_uid"].startswith("pkg-manifest-")
    assert item["desensitization_status"] == "unknown"
    assert payload["manifest_revision"]


def test_offline_manifest_csv_matches_the_json_contract(client, db_session, admin_headers):
    workspace, task, _ = _seed(db_session)

    response = client.get(
        f"/api/v1/collection-tasks/{task.id}/offline-manifest",
        params={"workspace_id": workspace.id, "format": "csv"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("package_uid,collection_project,collection_task")
    assert "pkg-manifest-" in lines[1]
