"""Collection overview API."""

from decimal import Decimal

from collection_api_fixtures import make_device_model, make_project, make_workspace

from data.models.data_package import DataPackage
from data.services.collection_tasks import create_collection_task


def test_overview_sums_intake_valid_duration(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    device = make_device_model(db_session)
    task = create_collection_task(
        db_session,
        workspace_id=workspace.id,
        collection_project_id=project.id,
        name="overview",
        target_duration_hours=Decimal("2.00"),
        default_package_duration_hours=Decimal("2.00"),
        device_model_id=device.id,
        label_ids=[],
        created_by_user_id=None,
    )
    package = db_session.query(DataPackage).filter(DataPackage.collection_task_id == task.id).one()
    package.intake_valid_duration_hours = Decimal("1.50")
    db_session.commit()

    response = client.get(
        "/api/v1/collection-overview",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {
        "workspace_id": workspace.id,
        "collection_project_id": project.id,
        "target_duration_hours": "2.00",
        "intake_valid_duration_hours": "1.50",
        "target_duration_s": 7200.0,
        "captured_duration_s": 0.0,
        "intake_valid_duration_s": 0.0,
        "package_counts": {
            "pending_assignment": 1,
            "assigned": 0,
            "voided": 0,
            "other": 0,
        },
        "duration_basis": "intake_valid_duration_s",
    }
