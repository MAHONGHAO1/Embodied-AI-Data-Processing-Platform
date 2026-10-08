"""Plan④ Task 6: RBAC matrix, audit event registry, cross-workspace asset list."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_intake_approved_package,
)

from data.security.audit import KNOWN_EVENTS
from data.services.data_assets import publish_data_asset
from data.services.data_batches import create_data_batch

_PLAN4_EVENTS = frozenset(
    {
        "governance.batch.create",
        "governance.stage.retry",
        "governance.qc.drop",
        "annotation.assign",
        "annotation.reassign",
        "annotation.submit",
        "review.approve",
        "review.return",
        "review.reassign",
        "asset.publish",
        "catalog.dataset.create",
        "catalog.version.create",
        "catalog.version.archive",
        "catalog.version.export",
    }
)


def _create_publishable_batch(db, *, name: str | None = None):
    workspace = make_workspace(db)
    project = make_project(db, workspace)
    package = seed_intake_approved_package(db, workspace, project, episode_hours=(1.0, 0.5))
    # seed_intake_approved_package already records a verified admission fact
    # with a complete objects_json manifest.
    package.governed_valid_duration_hours = Decimal("1.50")
    batch, _ = create_data_batch(
        db,
        workspace_id=workspace.id,
        name=name or f"Rbac-{uuid4().hex[:6]}",
        data_package_ids=[package.id],
        label_ids=[],
        integrity_check_enabled=False,
        quality_check_enabled=False,
        compliance_check_enabled=False,
        annotation_enabled=False,
        annotator_user_ids=[],
        reviewer_user_id=None,
        review_mode="single",
        created_by_user_id=None,
    )
    batch.status = "publishing"
    db.commit()
    db.refresh(batch)
    return workspace, batch


def test_plan4_known_events_registered():
    missing = sorted(_PLAN4_EVENTS - KNOWN_EVENTS)
    assert missing == [], f"KNOWN_EVENTS missing: {missing}"


def test_annotator_cannot_create_batch(client, db_session, annotator_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = seed_intake_approved_package(db_session, workspace, project)
    response = client.post(
        "/api/v1/data-batches",
        headers=annotator_headers,
        json={
            "workspace_id": workspace.id,
            "name": "Forbidden-Batch",
            "data_package_ids": [package.id],
            "label_ids": [],
            "integrity_check_enabled": False,
            "quality_check_enabled": False,
            "compliance_check_enabled": False,
            "annotation_enabled": False,
            "annotator_user_ids": [],
            "reviewer_user_id": None,
            "review_mode": "single",
        },
    )
    assert response.status_code == 403


def test_auditor_cannot_create_catalog_dataset(client, auditor_headers):
    response = client.post(
        "/api/v1/catalog-datasets",
        headers=auditor_headers,
        json={
            "name": f"Nope-{uuid4().hex[:6]}",
            "description": "",
            "source_kind": "qrdf_assets",
        },
    )
    assert response.status_code == 403


def test_annotator_cannot_create_catalog_dataset(client, annotator_headers):
    response = client.post(
        "/api/v1/catalog-datasets",
        headers=annotator_headers,
        json={
            "name": f"AnnNope-{uuid4().hex[:6]}",
            "description": "",
            "source_kind": "qrdf_assets",
        },
    )
    assert response.status_code == 403


def test_annotator_cannot_export_catalog_version(
    client, db_session, admin_headers, annotator_headers
):
    _ws, batch = _create_publishable_batch(db_session)
    asset = publish_data_asset(db_session, batch_id=batch.id)
    db_session.commit()
    assert asset is not None

    created = client.post(
        "/api/v1/catalog-datasets",
        headers=admin_headers,
        json={
            "name": f"ExportGate-{uuid4().hex[:6]}",
            "description": "",
            "source_kind": "qrdf_assets",
        },
    )
    assert created.status_code == 200, created.text
    dataset_id = created.json()["data"]["id"]

    versioned = client.post(
        f"/api/v1/catalog-datasets/{dataset_id}/versions",
        headers=admin_headers,
        json={"data_asset_ids": [asset.id]},
    )
    assert versioned.status_code == 200, versioned.text
    version_id = versioned.json()["data"]["id"]

    exported = client.post(
        f"/api/v1/catalog-datasets/versions/{version_id}/export",
        headers=annotator_headers,
        json={"format": "qrdf_0_2"},
    )
    assert exported.status_code == 403


def test_auditor_can_get_assets_and_catalog(client, db_session, admin_headers, auditor_headers):
    _ws, batch = _create_publishable_batch(db_session)
    asset = publish_data_asset(db_session, batch_id=batch.id)
    db_session.commit()
    assert asset is not None

    created = client.post(
        "/api/v1/catalog-datasets",
        headers=admin_headers,
        json={
            "name": f"AudRead-{uuid4().hex[:6]}",
            "description": "",
            "source_kind": "qrdf_assets",
        },
    )
    assert created.status_code == 200, created.text
    dataset_id = created.json()["data"]["id"]

    assets = client.get("/api/v1/data-assets", headers=auditor_headers)
    assert assets.status_code == 200
    assert asset.id in {item["id"] for item in assets.json()["data"]["items"]}

    catalog = client.get("/api/v1/catalog-datasets", headers=auditor_headers)
    assert catalog.status_code == 200
    assert dataset_id in {item["id"] for item in catalog.json()["data"]["items"]}


def test_annotator_can_list_assets_read_only(client, db_session, annotator_headers):
    _ws, batch = _create_publishable_batch(db_session)
    asset = publish_data_asset(db_session, batch_id=batch.id)
    db_session.commit()
    assert asset is not None

    listed = client.get("/api/v1/data-assets", headers=annotator_headers)
    assert listed.status_code == 200
    ids = {item["id"] for item in listed.json()["data"]["items"]}
    assert asset.id in ids


def test_auditor_cannot_annotate_without_capability(client, db_session, auditor_headers):
    workspace = make_workspace(db_session)
    response = client.get(
        "/api/v1/annotation-work-items",
        headers=auditor_headers,
        params={"workspace_id": workspace.id},
    )
    assert response.status_code == 403


def test_asset_list_includes_other_workspace_sources(client, db_session, admin_headers):
    ws_a, batch_a = _create_publishable_batch(db_session, name="Asset-A")
    ws_b, batch_b = _create_publishable_batch(db_session, name="Asset-B")
    asset_a = publish_data_asset(db_session, batch_id=batch_a.id)
    asset_b = publish_data_asset(db_session, batch_id=batch_b.id)
    db_session.commit()
    assert asset_a is not None and asset_b is not None
    assert ws_a.id != ws_b.id

    listed = client.get("/api/v1/data-assets", headers=admin_headers)
    assert listed.status_code == 200
    ids = {item["id"] for item in listed.json()["data"]["items"]}
    assert asset_a.id in ids
    assert asset_b.id in ids
