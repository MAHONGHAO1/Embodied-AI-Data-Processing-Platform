"""Task 4: immutable source manifests for assets and catalog versions."""

from __future__ import annotations

import copy
from uuid import uuid4

import pytest
from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_intake_approved_package,
)

from data.database import Episode, EpisodeAnnotation
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.catalog_datasets import (
    create_catalog_dataset,
    create_catalog_version,
)
from data.services.data_assets import publish_data_asset
from data.services.data_batches import create_data_batch
from data.services.episode_admission import record_episode_admission_fact


def _batch_with_source(db):
    workspace = make_workspace(db)
    project = make_project(db, workspace)
    package = seed_intake_approved_package(db, workspace, project, episode_hours=(1.0,))
    episode = db.query(Episode).filter(Episode.data_package_id == package.id).one()
    # seed_intake_approved_package already records a verified admission fact
    # with a complete objects_json manifest (its "data" entry has version_id
    # "v1", matched by the assertions below).
    db.add(
        EpisodeAnnotation(
            episode_id=episode.id,
            version=1,
            payload_json={"label": "before"},
        )
    )
    batch, _ = create_data_batch(
        db,
        workspace_id=workspace.id,
        name=f"Asset-{uuid4().hex[:8]}",
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
    return batch, episode


def test_asset_snapshot_freezes_annotation_and_admission_refs(db_session):
    batch, episode = _batch_with_source(db_session)
    asset = publish_data_asset(db_session, batch_id=batch.id)
    assert asset is not None
    db_session.commit()
    before = copy.deepcopy(asset.source_snapshot_json)

    db_session.add(
        EpisodeAnnotation(
            episode_id=episode.id,
            version=2,
            payload_json={"label": "after"},
        )
    )
    db_session.commit()
    db_session.expire(asset)
    assert asset.source_snapshot_json == before
    assert before["episodes"][0]["annotation_revision"]["version"] == 1
    assert before["episodes"][0]["files"][0]["ref"]["version_id"] == "v1"


def test_catalog_version_freezes_asset_snapshot_and_order(db_session):
    batch, _episode = _batch_with_source(db_session)
    asset = publish_data_asset(db_session, batch_id=batch.id)
    db_session.commit()
    dataset = create_catalog_dataset(db_session, name=f"Catalog-{uuid4().hex[:8]}")
    version = create_catalog_version(db_session, dataset_id=dataset.id, data_asset_ids=[asset.id])
    db_session.commit()
    frozen = copy.deepcopy(version.source_snapshot_json)
    asset.source_snapshot_json["episodes"][0]["annotation_revision"]["version"] = 999
    db_session.commit()
    db_session.expire(version)
    assert version.source_snapshot_json == frozen
    assert frozen["assets"][0]["asset_id"] == asset.id
    assert frozen["assets"][0]["position"] == 1


def _v2_entries():
    """A verified manifest with a distinct ref identity from verified_entries()'s "v1"
    data, so the snapshot-freezing assertion below can tell attempt 1's objects apart
    from attempt 2's.
    """
    from data.services.episode_objects import object_entry

    def ref(role: str, key: str) -> dict:
        return {
            "bucket_role": role,
            "object_key": key,
            "version_id": "v2",
            "etag": "new",
            "size_bytes": 2,
            "sha256": "b" * 64,
        }

    return [
        object_entry(path="new.mcap", kind="data", ref=ref("raw", "raw/new.mcap")),
        object_entry(
            path="metadata.json",
            kind="metadata",
            ref=ref("process", "process/new/metadata.json"),
        ),
        object_entry(
            path="admission-report.json",
            kind="admission_report",
            ref=ref("process", "process/new/admission-report.json"),
        ),
    ]


def test_asset_uses_batch_admission_attempt_after_newer_current_fact(db_session):
    batch, episode = _batch_with_source(db_session)
    record_episode_admission_fact(
        db_session,
        episode_id=episode.id,
        attempt=2,
        source_fingerprint=episode.source_fingerprint,
        validation_policy_version="v2",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        qrdf_profile="ego",
        report_ref={"uri": "db://episode/new-report"},
        objects=_v2_entries(),
    )
    db_session.commit()

    asset = publish_data_asset(db_session, batch_id=batch.id)
    assert asset is not None
    assert asset.source_snapshot_json["episodes"][0]["admission_attempt"] == 1
    assert asset.source_snapshot_json["episodes"][0]["files"][0]["ref"]["version_id"] == "v1"


def test_asset_rejects_unsafe_source_relative_path(db_session):
    batch, episode = _batch_with_source(db_session)
    fact = (
        db_session.query(EpisodeAdmissionFact)
        .filter(EpisodeAdmissionFact.episode_id == episode.id)
        .one()
    )
    objects = copy.deepcopy(fact.objects_json)
    for entry in objects:
        if entry.get("kind") == "data":
            entry["path"] = "../escape.mcap"
    fact.objects_json = objects
    db_session.commit()

    with pytest.raises(ValueError, match="episode_object_path_unsafe"):
        publish_data_asset(db_session, batch_id=batch.id)
    db_session.rollback()
