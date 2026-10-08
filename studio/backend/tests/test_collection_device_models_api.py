"""Collection device model catalog API."""

from collection_api_fixtures import make_device_model, make_workspace


def test_list_device_models_returns_seeded_active_rows(client, db_session, admin_headers):
    from data.services.collection_device_models import ensure_default_device_models

    workspace = make_workspace(db_session)
    ensure_default_device_models(db_session)
    db_session.commit()

    response = client.get(
        "/api/v1/collection-device-models",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) >= 2
    assert {"id", "vendor", "model", "device_type", "modalities", "is_active"} <= set(items[0])


def test_ensure_default_device_models_is_idempotent(db_session):
    from data.models.collection_config import CollectionDeviceModel
    from data.services.collection_device_models import ensure_default_device_models

    initial_count = db_session.query(CollectionDeviceModel).count()
    ensure_default_device_models(db_session)
    db_session.commit()
    seeded_count = db_session.query(CollectionDeviceModel).count()
    ensure_default_device_models(db_session)
    db_session.commit()

    assert seeded_count >= initial_count
    assert db_session.query(CollectionDeviceModel).count() == seeded_count


def test_list_device_models_excludes_inactive_by_default(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    model = make_device_model(db_session)
    model.is_active = False
    db_session.commit()

    response = client.get(
        "/api/v1/collection-device-models",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert model.id not in {item["id"] for item in response.json()["data"]["items"]}

    response = client.get(
        "/api/v1/collection-device-models",
        headers=admin_headers,
        params={"workspace_id": workspace.id, "include_inactive": True},
    )
    assert model.id in {item["id"] for item in response.json()["data"]["items"]}


def test_no_public_create_endpoint(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    response = client.post(
        "/api/v1/collection-device-models",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "vendor": "X",
            "model": "Y",
            "device_type": "ego",
        },
    )
    assert response.status_code in {404, 405, 422}
