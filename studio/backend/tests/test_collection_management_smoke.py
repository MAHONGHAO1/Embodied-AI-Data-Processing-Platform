"""Collection management API main path end-to-end smoke test."""

from collection_api_fixtures import (
    make_collector,
    make_device_model,
    make_workspace,
)


def test_collection_management_end_to_end_smoke(
    client,
    db_session,
    admin_headers,
):
    workspace = make_workspace(db_session)
    device = make_device_model(db_session)
    responsible = make_collector(db_session, workspace, name="Smoke Responsible")
    operator = make_collector(db_session, workspace, name="Smoke Operator")

    project_response = client.post(
        "/api/v1/collection-projects",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": "Smoke Project",
            "description": "collection management smoke path",
        },
    )
    assert project_response.status_code == 200
    project = project_response.json()["data"]
    assert project["status"] == "enabled"

    task_response = client.post(
        "/api/v1/collection-tasks",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project["id"],
            "name": "Smoke Task",
            "target_duration_hours": "5.00",
            "default_package_duration_hours": "2.00",
            "device_model_id": device.id,
            "label_ids": [],
        },
    )
    assert task_response.status_code == 200
    task = task_response.json()["data"]
    assert task["package_count"] == 3
    assert [row["target_duration_hours"] for row in task["packages"]] == [
        "2.00",
        "2.00",
        "1.00",
    ]
    assert {row["status"] for row in task["packages"]} == {"pending_assignment"}

    assigned_package, resized_package, deleted_package = task["packages"]
    assign_response = client.post(
        f"/api/v1/data-packages/{assigned_package['id']}/assign",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "responsible_collector_id": responsible.id,
            "operator_collector_id": operator.id,
        },
    )
    assert assign_response.status_code == 200
    assigned = assign_response.json()["data"]
    assert assigned["status"] == "assigned"
    assert assigned["offline_manifest"]["package_uid"] == assigned["package_uid"]

    adjust_response = client.post(
        "/api/v1/data-packages/adjust",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_task_id": task["id"],
            "operations": [
                {
                    "op": "resize",
                    "data_package_id": resized_package["id"],
                    "target_duration_hours": "1.50",
                },
                {
                    "op": "delete",
                    "data_package_id": deleted_package["id"],
                },
            ],
        },
    )
    assert adjust_response.status_code == 200
    adjusted_packages = adjust_response.json()["data"]["packages"]
    assert len(adjusted_packages) == 2
    assert (
        next(row for row in adjusted_packages if row["id"] == resized_package["id"])[
            "target_duration_hours"
        ]
        == "1.50"
    )

    void_response = client.post(
        f"/api/v1/data-packages/{assigned_package['id']}/void",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "reason": "smoke path replacement"},
    )
    assert void_response.status_code == 200
    assert void_response.json()["data"]["status"] == "voided"

    overview_response = client.get(
        "/api/v1/collection-overview",
        headers=admin_headers,
        params={
            "workspace_id": workspace.id,
            "collection_project_id": project["id"],
        },
    )
    assert overview_response.status_code == 200
    overview = overview_response.json()["data"]
    assert overview["target_duration_hours"] == "5.00"
    assert overview["intake_valid_duration_hours"] == "0.00"
    assert overview["package_counts"] == {
        "pending_assignment": 1,
        "assigned": 0,
        "voided": 1,
        "other": 0,
    }
    assert overview["duration_basis"] == "intake_valid_duration_hours"
