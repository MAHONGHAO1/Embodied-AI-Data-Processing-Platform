"""Data assets: 1:1 batch publication, zero assets on full dropout, and listing without implicit workspace filtering."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_intake_approved_package,
)

from data.database import Episode
from data.models.data_asset import DataAsset
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.data_assets import DataAssetSnapshotError, publish_data_asset
from data.services.data_batches import create_data_batch


def _episodes_for(db, package_id: int) -> list[Episode]:
    return (
        db.query(Episode)
        .filter(Episode.data_package_id == package_id)
        .order_by(Episode.id.asc())
        .all()
    )


def _create_publishable_batch(db, *, name: str | None = None):
    workspace = make_workspace(db)
    project = make_project(db, workspace)
    package = seed_intake_approved_package(db, workspace, project, episode_hours=(1.0, 0.5))
    package.governed_valid_duration_hours = Decimal("1.50")
    # seed_intake_approved_package already records a verified admission fact
    # with a complete objects_json manifest.
    batch, _ = create_data_batch(
        db,
        workspace_id=workspace.id,
        name=name or f"Asset-{uuid4().hex[:6]}",
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
    db.refresh(package)
    return workspace, batch, package


def test_asset_one_to_one_and_idempotent(db_session):
    _workspace, batch, package = _create_publishable_batch(db_session)
    episodes = _episodes_for(db_session, package.id)

    a1 = publish_data_asset(db_session, batch_id=batch.id)
    db_session.commit()
    a2 = publish_data_asset(db_session, batch_id=batch.id)
    db_session.commit()

    assert a1 is not None
    assert a2 is not None
    assert a1.id == a2.id
    assert db_session.query(DataAsset).filter_by(data_batch_id=batch.id).count() == 1

    db_session.refresh(batch)
    db_session.refresh(package)
    assert batch.status == "published"
    assert package.status == "published"
    assert a1.workspace_id == batch.workspace_id
    assert a1.governed_valid_duration_hours == Decimal("1.50")
    assert sorted(a1.episode_ids_json) == sorted(ep.id for ep in episodes)
    assert package.id in (a1.source_json.get("data_package_ids") or [])
    assert a1.source_json["package_snapshots"][0]["package_uid"] == package.package_uid
    assert {item["episode_id"] for item in a1.source_json["episode_snapshots"]} == {
        episode.id for episode in episodes
    }


def test_all_qc_dropped_yields_no_asset(db_session):
    _workspace, batch, package = _create_publishable_batch(db_session)
    for episode in _episodes_for(db_session, package.id):
        episode.validity_status = "qc_dropped"
    package.governed_valid_duration_hours = Decimal("0.00")
    db_session.commit()

    result = publish_data_asset(db_session, batch_id=batch.id)
    db_session.commit()

    assert result is None
    db_session.refresh(batch)
    assert batch.status == "no_publishable_asset"
    assert db_session.query(DataAsset).filter_by(data_batch_id=batch.id).count() == 0


def test_asset_rejects_empty_or_unverified_raw_manifest(db_session):
    _workspace, batch, package = _create_publishable_batch(db_session)
    episode = _episodes_for(db_session, package.id)[0]
    fact = (
        db_session.query(EpisodeAdmissionFact)
        .filter(EpisodeAdmissionFact.episode_id == episode.id)
        .one()
    )
    fact.objects_json = []
    db_session.commit()
    with pytest.raises(DataAssetSnapshotError, match="episode_object_missing_data"):
        publish_data_asset(db_session, batch_id=batch.id)
    assert db_session.query(DataAsset).filter_by(data_batch_id=batch.id).count() == 0
    db_session.rollback()

    fact.objects_json = [
        {
            "path": "data.mcap",
            "role": "raw",
            "kind": "data",
            "ref": {
                "bucket_role": "raw",
                "object_key": f"raw/{episode.id}/data.mcap",
                "version_id": "v1",
                "etag": "etag",
                "size_bytes": 1,
            },
        }
    ]
    db_session.commit()
    with pytest.raises(DataAssetSnapshotError, match="episode_object_ref_invalid"):
        publish_data_asset(db_session, batch_id=batch.id)
    assert db_session.query(DataAsset).filter_by(data_batch_id=batch.id).count() == 0


def test_list_assets_not_implicitly_scoped_to_current_workspace(client, db_session, admin_headers):
    ws_a, batch_a, _pkg_a = _create_publishable_batch(db_session, name="Asset-A")
    ws_b, batch_b, _pkg_b = _create_publishable_batch(db_session, name="Asset-B")
    asset_a = publish_data_asset(db_session, batch_id=batch_a.id)
    asset_b = publish_data_asset(db_session, batch_id=batch_b.id)
    db_session.commit()
    assert asset_a is not None and asset_b is not None

    listed = client.get("/api/v1/data-assets", headers=admin_headers)
    assert listed.status_code == 200
    ids = {item["id"] for item in listed.json()["data"]["items"]}
    assert asset_a.id in ids
    assert asset_b.id in ids

    filtered = client.get(
        "/api/v1/data-assets",
        headers=admin_headers,
        params={"source_workspace_id": ws_a.id},
    )
    assert filtered.status_code == 200
    filtered_ids = {item["id"] for item in filtered.json()["data"]["items"]}
    assert asset_a.id in filtered_ids
    assert asset_b.id not in filtered_ids

    detail = client.get(
        f"/api/v1/data-assets/{asset_b.id}",
        headers=admin_headers,
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == asset_b.id
    assert detail.json()["data"]["workspace_id"] == ws_b.id
