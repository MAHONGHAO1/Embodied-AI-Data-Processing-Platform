"""Plan 4 end-to-end smoke test: Governance -> Annotation -> Review -> Asset -> Catalog Version/Export."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from tests.collection_api_fixtures import (
    make_annotator_reviewer,
    make_project,
    make_workspace,
    seed_intake_approved_package,
)

from data.database import Episode
from data.models.annotation_work import AnnotationWorkItem, ReviewWorkItem
from data.models.data_asset import DataAsset
from data.services.data_batches import create_data_batch
from data.services.governance_runs import run_governance, schedule_asset_creation
from data.utils.helpers import create_access_token


def _headers_for(user) -> dict[str, str]:
    token = create_access_token(user.id, user.email, user.role)
    return {"Authorization": f"Bearer {token}"}


def _episodes_for(db, package_id: int) -> list[Episode]:
    return (
        db.query(Episode)
        .filter(Episode.data_package_id == package_id)
        .order_by(Episode.id.asc())
        .all()
    )


def _ensure_source_facts(db, episodes: list[Episode]) -> None:
    """Normalize episode timing; admission facts already carry a verified
    objects_json manifest from seed_intake_approved_package.
    """
    for episode in episodes:
        metadata = dict(episode.metadata_json or {})
        timing = dict(metadata.get("timing") or {})
        timing["start_timestamp_ns"] = "0"
        timing["end_timestamp_ns"] = str(int(timing["duration_s"] * 1_000_000_000))
        metadata["timing"] = timing
        episode.metadata_json = metadata


def _publish_skip_governance_asset(db, *, name: str | None = None):
    """Skip-gov + annotation-off: asset must come from finalize alone (no manual publish)."""
    workspace = make_workspace(db)
    project = make_project(db, workspace)
    package = seed_intake_approved_package(db, workspace, project, episode_hours=(1.0, 0.5))
    _ensure_source_facts(db, _episodes_for(db, package.id))
    package.governed_valid_duration_hours = Decimal("1.50")
    batch, _ = create_data_batch(
        db,
        workspace_id=workspace.id,
        name=name or f"SkipGov-{uuid4().hex[:6]}",
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
    db.commit()
    db.refresh(package)
    assert package.status == "batched"

    run_governance(db, batch_id=batch.id, sync=True)
    db.commit()
    db.refresh(batch)
    db.refresh(package)
    asset = db.query(DataAsset).filter(DataAsset.data_batch_id == batch.id).one_or_none()
    assert asset is not None, "finalize must auto-publish without manual publish_data_asset"
    assert batch.status == "published"
    assert package.status == "published"
    return workspace, batch, asset


def test_full_pipeline_governance_annotate_review_asset_catalog(
    client, db_session, admin_headers, monkeypatch
):
    """intake_approved×N -> QC drops 1 Episode -> Annotation -> Single review -> Asset -> Cross-workspace version -> Export/archive protection."""
    monkeypatch.setattr(
        "data.routers.review_work_items.schedule_asset_creation",
        lambda batch_id: schedule_asset_creation(batch_id, sync=True),
    )

    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    packages = [
        seed_intake_approved_package(db_session, workspace, project, episode_hours=(1.0, 1.0)),
        seed_intake_approved_package(db_session, workspace, project, episode_hours=(0.5,)),
    ]
    for package in packages:
        _ensure_source_facts(db_session, _episodes_for(db_session, package.id))
    annotator, reviewer = make_annotator_reviewer(db_session, workspace)

    batch, _ = create_data_batch(
        db_session,
        workspace_id=workspace.id,
        name=f"Smoke-Main-{uuid4().hex[:6]}",
        data_package_ids=[p.id for p in packages],
        label_ids=[],
        integrity_check_enabled=False,
        quality_check_enabled=True,
        compliance_check_enabled=False,
        annotation_enabled=True,
        annotator_user_ids=[annotator.id],
        reviewer_user_id=reviewer.id,
        review_mode="single",
        created_by_user_id=None,
    )
    db_session.commit()

    # Drop exactly one Episode via QC on the first package.
    drop_episode = _episodes_for(db_session, packages[0].id)[0]
    meta = dict(drop_episode.metadata_json or {})
    meta["quality"] = {"pass": False, "reason": "smoke-blurry"}
    drop_episode.metadata_json = meta
    db_session.commit()

    run_governance(db_session, batch_id=batch.id, sync=True)
    db_session.commit()

    db_session.refresh(drop_episode)
    db_session.refresh(batch)
    assert drop_episode.validity_status == "qc_dropped"
    assert batch.status == "annotating"

    ann_items = (
        db_session.query(AnnotationWorkItem)
        .filter(AnnotationWorkItem.data_batch_id == batch.id)
        .order_by(AnnotationWorkItem.id.asc())
        .all()
    )
    assert len(ann_items) == 2

    for item in ann_items:
        payloads = {}
        for member in item.episode_members_json:
            episode = db_session.get(Episode, member["episode_id"])
            payloads[str(episode.id)] = {
                "kind": "episode_annotation",
                "qrdf_version": "0.2.0",
                "source": {"episode_id": episode.episode_uid, "data_sha256": "a" * 64},
                "episode": {"outcome": "success"},
                "tracks": [
                    {
                        "name": "high_level_subtask",
                        "items": [
                            {
                                "id": "01998211-1234-7000-8000-123456789abc",
                                "target": {
                                    "type": "time_range",
                                    "start_ns": "0",
                                    "end_ns": episode.metadata_json["timing"]["end_timestamp_ns"],
                                },
                                "high_level_subtask": "Complete the collection task",
                            }
                        ],
                    }
                ],
            }
        saved = client.patch(
            f"/api/v1/annotation-work-items/{item.id}",
            headers=_headers_for(annotator),
            json={"workspace_id": workspace.id, "draft_json": {"episodes": payloads}},
        )
        assert saved.status_code == 200, saved.text
        submitted = client.post(
            f"/api/v1/annotation-work-items/{item.id}/submit",
            headers=_headers_for(annotator),
            json={"workspace_id": workspace.id},
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["data"]["status"] == "submitted"

    rev_items = (
        db_session.query(ReviewWorkItem)
        .filter(ReviewWorkItem.data_batch_id == batch.id)
        .order_by(ReviewWorkItem.id.asc())
        .all()
    )
    assert len(rev_items) == 2
    for rev in rev_items:
        approved = client.post(
            f"/api/v1/review-work-items/{rev.id}/approve",
            headers=_headers_for(reviewer),
            json={"workspace_id": workspace.id},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["data"]["status"] == "approved"

    db_session.refresh(batch)
    assets = db_session.query(DataAsset).filter(DataAsset.data_batch_id == batch.id).all()
    assert len(assets) == 1
    asset_a = assets[0]
    assert batch.status == "published"
    # Dropped episode excluded; remaining: 1.0 + 0.5 from pkg0/pkg1.
    assert Decimal(str(asset_a.governed_valid_duration_hours)) == Decimal("1.50")
    assert drop_episode.id not in asset_a.episode_ids_json

    # Second workspace asset (skip governance, annotation off) joined into catalog version.
    _ws_b, _batch_b, asset_b = _publish_skip_governance_asset(db_session, name="Smoke-Cross-WS")
    assert asset_b.workspace_id != asset_a.workspace_id

    dataset = client.post(
        "/api/v1/catalog-datasets",
        headers=admin_headers,
        json={
            "name": f"Smoke-Catalog-{uuid4().hex[:8]}",
            "description": "plan4 e2e",
            "source_kind": "qrdf_assets",
        },
    )
    assert dataset.status_code == 200, dataset.text
    dataset_id = dataset.json()["data"]["id"]

    version = client.post(
        f"/api/v1/catalog-datasets/{dataset_id}/versions",
        headers=admin_headers,
        json={"data_asset_ids": [asset_a.id, asset_b.id]},
    )
    assert version.status_code == 200, version.text
    version_body = version.json()["data"]
    assert version_body["version"] == 1
    assert version_body["data_asset_ids"] == [asset_a.id, asset_b.id]
    version_id = version_body["id"]

    exported = client.post(
        f"/api/v1/catalog-datasets/versions/{version_id}/export",
        headers=admin_headers,
        json={"format": "lerobot_3_0"},
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["data"]["format"] == "lerobot_3_0"

    blocked = client.delete(
        f"/api/v1/catalog-datasets/versions/{version_id}",
        headers=admin_headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "version_referenced"

    archived = client.post(
        f"/api/v1/catalog-datasets/versions/{version_id}/archive",
        headers=admin_headers,
    )
    assert archived.status_code == 200
    assert archived.json()["data"]["status"] == "archived"


def test_skip_all_governance_and_annotation_publishes_asset_directly(db_session):
    workspace, batch, asset = _publish_skip_governance_asset(db_session)
    db_session.refresh(batch)
    assert batch.status == "published"
    assert asset.workspace_id == workspace.id
    assert db_session.query(DataAsset).filter_by(data_batch_id=batch.id).count() == 1


def test_all_qc_dropped_yields_no_publishable_asset(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = seed_intake_approved_package(db_session, workspace, project, episode_hours=(1.0, 1.0))
    batch, _ = create_data_batch(
        db_session,
        workspace_id=workspace.id,
        name=f"Smoke-AllQC-{uuid4().hex[:6]}",
        data_package_ids=[package.id],
        label_ids=[],
        integrity_check_enabled=False,
        quality_check_enabled=True,
        compliance_check_enabled=False,
        annotation_enabled=False,
        annotator_user_ids=[],
        reviewer_user_id=None,
        review_mode="single",
        created_by_user_id=None,
    )
    db_session.commit()

    for episode in _episodes_for(db_session, package.id):
        meta = dict(episode.metadata_json or {})
        meta["quality"] = {"pass": False, "reason": "all-bad"}
        episode.metadata_json = meta
    db_session.commit()

    run_governance(db_session, batch_id=batch.id, sync=True)
    db_session.commit()

    db_session.refresh(batch)
    db_session.refresh(package)
    episodes = _episodes_for(db_session, package.id)
    assert episodes
    assert all(ep.validity_status == "qc_dropped" for ep in episodes)
    assert batch.status == "no_publishable_asset"
    assert package.status == "batched"
    assert db_session.query(DataAsset).filter_by(data_batch_id=batch.id).count() == 0
