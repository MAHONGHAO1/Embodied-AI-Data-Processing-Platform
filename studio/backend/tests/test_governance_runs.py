"""Data batch governance: QC dropout, duration writeback, skipping, and failure retry."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from tests.collection_api_fixtures import (
    make_annotator_reviewer,
    make_project,
    make_workspace,
    seed_intake_approved_package,
    seed_package_pending_intake_review,
)

from data.database import Episode
from data.models.data_batch import DataBatchStageRun
from data.services.data_batches import create_data_batch
from data.services.governance_runs import (
    get_governance_report,
    retry_failed_stage,
    run_governance,
)


def _episodes_for(db, package_id: int) -> list[Episode]:
    return (
        db.query(Episode)
        .filter(Episode.data_package_id == package_id)
        .order_by(Episode.id.asc())
        .all()
    )


def _create_quality_batch(db, *, annotation_enabled: bool = False):
    workspace = make_workspace(db)
    project = make_project(db, workspace)
    package = seed_intake_approved_package(db, workspace, project, episode_hours=(1.0, 1.0))
    ann = rev = None
    annotator_ids: list[int] = []
    reviewer_id = None
    if annotation_enabled:
        ann, rev = make_annotator_reviewer(db, workspace)
        annotator_ids = [ann.id]
        reviewer_id = rev.id
    batch, _ = create_data_batch(
        db,
        workspace_id=workspace.id,
        name="Gov-Quality",
        data_package_ids=[package.id],
        label_ids=[],
        integrity_check_enabled=False,
        quality_check_enabled=True,
        compliance_check_enabled=False,
        annotation_enabled=annotation_enabled,
        annotator_user_ids=annotator_ids,
        reviewer_user_id=reviewer_id,
        review_mode="single",
        created_by_user_id=None,
    )
    db.commit()
    db.refresh(batch)
    db.refresh(package)
    episodes = _episodes_for(db, package.id)
    db.commit()
    return workspace, batch, package, episodes


def test_quality_stage_marks_qc_dropped_and_updates_governed_duration(db_session):
    # seed batch with 2 valid episodes on one package; inject QC drop for episode A
    _workspace, batch, package, episodes = _create_quality_batch(db_session)
    episode_a, episode_b = episodes
    meta = dict(episode_a.metadata_json or {})
    meta["quality"] = {"pass": False, "reason": "blurry"}
    episode_a.metadata_json = meta
    db_session.commit()

    run_governance(db_session, batch_id=batch.id, sync=True)
    db_session.commit()

    db_session.refresh(episode_a)
    db_session.refresh(episode_b)
    db_session.refresh(package)
    assert episode_a.validity_status == "qc_dropped"
    assert episode_b.validity_status == "valid"
    assert episode_a.metadata_json["qc"]["reason"] == "blurry"
    assert package.governed_valid_duration_hours == Decimal("1.00")  # only B


def test_retry_does_not_revive_qc_dropped(db_session):
    """Integrity fails first; retry continues into quality drop; qc_dropped stays."""
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = seed_intake_approved_package(db_session, workspace, project, episode_hours=(1.0, 1.0))
    episodes = _episodes_for(db_session, package.id)
    episode_a, episode_b = episodes

    # Integrity fails on B first so quality never runs; A carries a QC fail mark.
    meta_b = dict(episode_b.metadata_json or {})
    meta_b["integrity"] = {"ok": False, "reason": "missing-file"}
    episode_b.metadata_json = meta_b
    meta_a = dict(episode_a.metadata_json or {})
    meta_a["quality"] = {"pass": False, "reason": "noise"}
    episode_a.metadata_json = meta_a
    db_session.commit()

    batch, _ = create_data_batch(
        db_session,
        workspace_id=workspace.id,
        name="Gov-Retry-QC",
        data_package_ids=[package.id],
        label_ids=[],
        integrity_check_enabled=True,
        quality_check_enabled=True,
        compliance_check_enabled=False,
        annotation_enabled=False,
        annotator_user_ids=[],
        reviewer_user_id=None,
        review_mode="single",
        created_by_user_id=None,
    )
    db_session.commit()

    run_governance(db_session, batch_id=batch.id, sync=True)
    db_session.commit()
    db_session.refresh(batch)
    integrity_stage = (
        db_session.query(DataBatchStageRun)
        .filter_by(run_id=batch.governance_run.id, stage="integrity")
        .one()
    )
    quality_stage = (
        db_session.query(DataBatchStageRun)
        .filter_by(run_id=batch.governance_run.id, stage="quality")
        .one()
    )
    assert integrity_stage.status == "failed"
    assert quality_stage.status == "queued"
    db_session.refresh(episode_a)
    assert episode_a.validity_status == "valid"

    # Clear integrity failure so retry can pass, then continue into quality.
    meta_b = dict(episode_b.metadata_json or {})
    meta_b["integrity"] = {"ok": True}
    episode_b.metadata_json = meta_b
    db_session.commit()

    retry_failed_stage(db_session, batch_id=batch.id, stage="integrity")
    db_session.commit()

    db_session.refresh(episode_a)
    db_session.refresh(episode_b)
    db_session.refresh(package)
    db_session.refresh(integrity_stage)
    db_session.refresh(quality_stage)
    assert integrity_stage.status == "passed"
    assert quality_stage.status == "passed"
    assert episode_a.validity_status == "qc_dropped"
    assert episode_b.validity_status == "valid"
    assert package.governed_valid_duration_hours == Decimal("1.00")


def test_disabled_stages_are_skipped_without_side_effects(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = seed_intake_approved_package(db_session, workspace, project)
    batch, _ = create_data_batch(
        db_session,
        workspace_id=workspace.id,
        name="Gov-Skip",
        data_package_ids=[package.id],
        label_ids=[],
        integrity_check_enabled=False,
        quality_check_enabled=False,
        compliance_check_enabled=True,
        annotation_enabled=False,
        annotator_user_ids=[],
        reviewer_user_id=None,
        review_mode="single",
        created_by_user_id=None,
    )
    db_session.commit()

    with mock.patch("data.services.governance_runs.schedule_asset_creation") as asset_stub:
        run_governance(db_session, batch_id=batch.id, sync=True)
        db_session.commit()
        asset_stub.assert_called_once_with(batch.id, db_session)

    stages = {stage.stage: stage.status for stage in batch.governance_run.stages}
    assert stages["integrity"] == "skipped"
    assert stages["quality"] == "skipped"
    assert stages["compliance"] == "passed"
    db_session.refresh(batch)
    assert batch.governance_run.status == "passed"


def test_all_qc_dropped_marks_batch_no_publishable_asset(db_session):
    _workspace, batch, package, episodes = _create_quality_batch(db_session)
    for episode in episodes:
        meta = dict(episode.metadata_json or {})
        meta["quality"] = {"pass": False, "reason": "all-bad"}
        episode.metadata_json = meta
    db_session.commit()

    with (
        mock.patch("data.services.governance_runs.schedule_asset_creation") as asset_stub,
        mock.patch("data.services.governance_runs.schedule_annotation_assignment") as ann_stub,
    ):
        run_governance(db_session, batch_id=batch.id, sync=True)
        db_session.commit()
        asset_stub.assert_not_called()
        ann_stub.assert_not_called()

    db_session.refresh(batch)
    db_session.refresh(package)
    assert batch.status == "no_publishable_asset"
    assert package.governed_valid_duration_hours == Decimal("0.00")


def test_annotation_enabled_schedules_assignment_after_governance(db_session):
    _workspace, batch, package, episodes = _create_quality_batch(
        db_session, annotation_enabled=True
    )
    assert all(ep.validity_status == "valid" for ep in episodes)

    with (
        mock.patch("data.services.governance_runs.schedule_annotation_assignment") as ann_stub,
        mock.patch("data.services.governance_runs.schedule_asset_creation") as asset_stub,
    ):
        run_governance(db_session, batch_id=batch.id, sync=True)
        db_session.commit()
        ann_stub.assert_called_once_with(batch.id, db_session)
        asset_stub.assert_not_called()

    db_session.refresh(batch)
    db_session.refresh(package)
    assert batch.status == "annotating"
    assert package.governed_valid_duration_hours == Decimal("2.00")


def test_integrity_failure_sets_stage_failed_and_batch_failed(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = seed_intake_approved_package(db_session, workspace, project)
    episodes = _episodes_for(db_session, package.id)
    meta = dict(episodes[0].metadata_json or {})
    meta["integrity"] = {"ok": False, "reason": "missing-file"}
    episodes[0].metadata_json = meta
    db_session.commit()

    batch, _ = create_data_batch(
        db_session,
        workspace_id=workspace.id,
        name="Gov-Integrity-Fail",
        data_package_ids=[package.id],
        label_ids=[],
        integrity_check_enabled=True,
        quality_check_enabled=False,
        compliance_check_enabled=False,
        annotation_enabled=False,
        annotator_user_ids=[],
        reviewer_user_id=None,
        review_mode="single",
        created_by_user_id=None,
    )
    db_session.commit()

    run_governance(db_session, batch_id=batch.id, sync=True)
    db_session.commit()

    db_session.refresh(batch)
    stage = (
        db_session.query(DataBatchStageRun)
        .filter_by(run_id=batch.governance_run.id, stage="integrity")
        .one()
    )
    assert stage.status == "failed"
    assert batch.status == "failed"
    assert batch.governance_run.status == "failed"


def test_quality_drop_via_result_json_injection(db_session):
    _workspace, batch, package, episodes = _create_quality_batch(db_session)
    episode_a, episode_b = episodes
    quality_stage = (
        db_session.query(DataBatchStageRun)
        .filter_by(run_id=batch.governance_run.id, stage="quality")
        .one()
    )
    quality_stage.result_json = {
        "drop_episode_ids": [episode_a.id],
        "drop_reasons": {str(episode_a.id): "injected"},
    }
    db_session.commit()

    run_governance(db_session, batch_id=batch.id, sync=True)
    db_session.commit()

    db_session.refresh(episode_a)
    db_session.refresh(episode_b)
    db_session.refresh(package)
    assert episode_a.validity_status == "qc_dropped"
    assert episode_b.validity_status == "valid"
    assert package.governed_valid_duration_hours == Decimal("1.00")


def test_governance_report_and_retry_api(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = seed_intake_approved_package(db_session, workspace, project)
    episodes = _episodes_for(db_session, package.id)
    meta = dict(episodes[0].metadata_json or {})
    meta["integrity"] = {"ok": False, "reason": "broken"}
    episodes[0].metadata_json = meta
    db_session.commit()

    created = client.post(
        "/api/v1/data-batches",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "name": "Gov-API",
            "data_package_ids": [package.id],
            "label_ids": [],
            "integrity_check_enabled": True,
            "quality_check_enabled": False,
            "compliance_check_enabled": False,
            "annotation_enabled": False,
            "annotator_user_ids": [],
            "reviewer_user_id": None,
            "review_mode": "single",
        },
    )
    assert created.status_code == 200
    batch_id = created.json()["data"]["id"]

    # Create path schedules async; run sync for deterministic report.
    run_governance(db_session, batch_id=batch_id, sync=True)
    db_session.commit()

    report = client.get(
        f"/api/v1/data-batches/{batch_id}/governance-report",
        headers=admin_headers,
        params={"workspace_id": workspace.id},
    )
    assert report.status_code == 200
    payload = report.json()["data"]
    assert payload["status"] == "failed"
    stages = {item["stage"]: item for item in payload["stages"]}
    assert stages["integrity"]["status"] == "failed"

    # Clear fail marker then retry (API path is sync=True → expect passed).
    meta = dict(episodes[0].metadata_json or {})
    meta["integrity"] = {"ok": True}
    episodes[0].metadata_json = meta
    db_session.commit()

    with mock.patch("data.tasks.governance_tasks.execute_governance_run.delay") as delay_stub:
        retried = client.post(
            f"/api/v1/data-batches/{batch_id}/governance/retry",
            headers=admin_headers,
            json={"workspace_id": workspace.id, "stage": "integrity"},
        )
        delay_stub.assert_not_called()
    assert retried.status_code == 200
    assert retried.json()["data"]["stage"] == "integrity"
    assert retried.json()["data"]["status"] == "passed"

    report2 = get_governance_report(db_session, workspace_id=workspace.id, batch_id=batch_id)
    integrity = next(s for s in report2["stages"] if s["stage"] == "integrity")
    assert integrity["status"] == "passed"
    assert integrity["attempt"] >= 2


def test_intake_rejected_episodes_are_ignored_by_quality(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0, 1.0)
    )
    from data.services.collection_intake_review import review_data_package_intake

    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[episodes[0].id],
        reason="manual",
    )
    db_session.commit()
    db_session.refresh(package)

    batch, _ = create_data_batch(
        db_session,
        workspace_id=workspace.id,
        name="Gov-Ignore-Rejected",
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

    run_governance(db_session, batch_id=batch.id, sync=True)
    db_session.commit()

    db_session.refresh(episodes[0])
    db_session.refresh(episodes[1])
    db_session.refresh(package)
    assert episodes[0].validity_status == "intake_rejected"
    assert episodes[1].validity_status == "valid"
    assert package.governed_valid_duration_hours == Decimal("1.00")
