"""Package level desensitization declarations are recorded, never assumed."""

from decimal import Decimal
from uuid import uuid4


def _seed_package(db_session):
    from data.database import Workspace, WorkspaceMember
    from data.models.collection_core import CollectionProject, CollectionTask
    from data.models.data_package import DataPackage

    suffix = uuid4().hex[:8]
    workspace = Workspace(name=f"Desensitization workspace {suffix}")
    db_session.add(workspace)
    db_session.flush()
    project = CollectionProject(workspace_id=workspace.id, name=f"Desensitization project {suffix}")
    db_session.add(project)
    db_session.flush()
    task = CollectionTask(
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name=f"Desensitization task {suffix}",
        target_duration_hours=Decimal("2.00"),
        default_package_duration_hours=Decimal("2.00"),
    )
    db_session.add(task)
    db_session.flush()
    package = DataPackage(
        package_uid=f"pkg-desens-{suffix}",
        workspace_id=workspace.id,
        collection_project_id=project.id,
        collection_task_id=task.id,
        status="pending_assignment",
        target_duration_hours=Decimal("2.00"),
    )
    db_session.add(package)
    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=1))
    db_session.commit()
    return workspace, package


def test_undeclared_package_reports_unknown(client, db_session, admin_headers):
    workspace, package = _seed_package(db_session)

    response = client.get(
        f"/api/v1/data-packages/{package.id}",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["desensitization"]["status"] == "unknown"


def test_declaration_is_recorded_without_gating_the_package(client, db_session, admin_headers):
    workspace, package = _seed_package(db_session)

    declared = client.post(
        f"/api/v1/data-packages/{package.id}/desensitization-declaration",
        json={
            "workspace_id": workspace.id,
            "status": "declared",
            "by": "张明",
            "tool": "duance",
            "policy_version": "v1",
        },
        headers=admin_headers,
    )

    assert declared.status_code == 200
    declaration = declared.json()["data"]["desensitization"]
    assert declaration["status"] == "declared"
    assert declaration["by"] == "张明"
    assert declaration["tool"] == "duance"
    assert declaration["policy_version"] == "v1"
    assert declaration["at"]

    detail = client.get(
        f"/api/v1/data-packages/{package.id}",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    ).json()["data"]["desensitization"]
    assert detail["status"] == "declared"
