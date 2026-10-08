"""Collection label dictionary API."""

from collection_api_fixtures import make_label, make_workspace


def test_create_label_and_list_by_category(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    created = client.post(
        "/api/v1/collection-labels",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "category": "scene", "name": "Kitchen"},
    )
    assert created.status_code == 200
    label_id = created.json()["data"]["id"]

    listed = client.get(
        "/api/v1/collection-labels",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "category": "scene"},
    )
    assert listed.status_code == 200
    names = [row["name"] for row in listed.json()["data"]["items"]]
    assert "Kitchen" in names
    assert label_id in {row["id"] for row in listed.json()["data"]["items"]}


def test_duplicate_label_name_conflicts(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    make_label(db_session, category="purpose", name="PickPlace")
    response = client.post(
        "/api/v1/collection-labels",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "category": "purpose", "name": " pickplace "},
    )
    assert response.status_code == 409


def test_deactivate_label_hides_from_default_list(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    label = make_label(db_session, category="training", name="Grip")
    response = client.post(
        f"/api/v1/collection-labels/{label.id}/deactivate",
        headers=admin_headers,
        json={"workspace_id": workspace.id},
    )
    assert response.status_code == 200
    listed = client.get(
        "/api/v1/collection-labels",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "category": "training"},
    )
    assert label.id not in {row["id"] for row in listed.json()["data"]["items"]}


def test_annotator_cannot_create_label(client, db_session, annotator_headers):
    workspace = make_workspace(db_session)
    response = client.post(
        "/api/v1/collection-labels",
        headers=annotator_headers,
        json={"workspace_id": workspace.id, "category": "scene", "name": "Nope"},
    )
    assert response.status_code == 403
