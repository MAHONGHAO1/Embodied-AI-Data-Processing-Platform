"""Package review facts refresh the warehouse without rewriting source Episodes."""

from datetime import datetime, timedelta

import pytest
from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_package_pending_intake_review,
)
from tests.test_package_annotation_workbench_api import draft, request, review_body, save, submit
from tests.test_package_annotation_workbench_api import package_work as _package_work_fixture

from data.database import DwdEpisodeFact
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.collection_intake_review import review_data_package_intake
from data.services.collection_overview import get_collection_overview
from data.services.dashboard_scope import DashboardScope
from data.services.dashboard_warehouse import (
    align_bucket_start,
    build_dwd,
    build_live_overview_payload,
    refresh_ads,
    roll_dws,
)
from data.services.data_assets import publish_data_asset

package_work = _package_work_fixture


def _overview(work):
    return build_live_overview_payload(
        work["db"], DashboardScope.for_workspace(work["workspace"].id)
    )


@pytest.mark.parametrize("all_invalid", [False, True])
def test_reviewed_package_states_and_duration_bases_invalidate_without_source_writes(
    package_work, all_invalid
):
    work, db = package_work, package_work["db"]
    source_states = [
        (e.workflow_status, e.quality_status, e.annotation_status, e.review_status, e.updated_at)
        for e in work["episodes"]
    ]
    initial = _overview(work)
    assert initial["kpis"]["annotation_completed"] == 0
    assert initial["kpis"]["source_duration_s"] == 10.0
    assert initial["kpis"]["intake_valid_duration_s"] == 10.0
    assert next(q for q in initial["today_queues"] if q["key"] == "separated")["pending"] == 2
    db.commit()
    assert build_dwd(db, etl_at=datetime.utcnow()) == 0

    payload = draft(work, all_invalid=all_invalid)
    if not all_invalid:
        second = str(work["episodes"][1].id)
        payload["episodes"][second] = draft(work, all_invalid=True)["episodes"][second]
    save(work, payload)
    assert submit(work).status_code == 200
    submitted = _overview(work)
    assert next(q for q in submitted["today_queues"] if q["key"] == "annotated")["pending"] == 2
    assert submitted["kpis"]["annotation_completed"] == 0
    db.commit()

    approved = request(work, "post", "/approve", review_body(work), review=True)
    assert approved.status_code == 200, approved.text
    reviewed = _overview(work)
    assert reviewed["kpis"]["annotation_completed"] == 2
    assert reviewed["kpis"]["annotation_completion_rate"] == 1.0
    assert reviewed["kpis"]["annotation_effective_duration_s"] == (0.0 if all_invalid else 1.0)
    assert sum(q["pending"] for q in reviewed["today_queues"]) == (0 if all_invalid else 1)
    db.commit()

    asset = publish_data_asset(db, batch_id=work["batch"].id)
    db.commit()
    final = _overview(work)
    assert (asset is None) is all_invalid
    assert final["kpis"]["qrdf_baseline_count"] == (0 if all_invalid else 1)
    assert final["kpis"]["total_duration_s"] == 10.0
    assert final["kpis"]["source_duration_s"] == 10.0
    assert final["kpis"]["intake_valid_duration_s"] == 10.0
    assert final["kpis"]["annotation_effective_duration_s"] == (0.0 if all_invalid else 1.0)
    assert sum(q["pending"] for q in final["today_queues"]) == 0
    db.refresh(work["batch"])
    assert work["batch"].status == ("no_publishable_asset" if all_invalid else "published")
    for episode, expected in zip(work["episodes"], source_states, strict=True):
        db.refresh(episode)
        assert (
            episode.workflow_status,
            episode.quality_status,
            episode.annotation_status,
            episode.review_status,
            episode.updated_at,
        ) == expected
        fact = db.get(DwdEpisodeFact, episode.id)
        db.refresh(fact)
        assert fact.queue_key is None
        assert fact.workflow_status == (
            "published" if asset and episode.id in asset.episode_ids_json else "no_valid_segments"
        )
    db.commit()
    assert build_dwd(db, etl_at=datetime.utcnow()) == 0

    # Live and persisted DWS/ADS use the same projection and timestamp basis.
    scope = DashboardScope.for_workspace(work["workspace"].id)
    bucket = align_bucket_start()
    roll_dws(db, scope=scope, bucket_start=bucket, etl_at=datetime.utcnow())
    stored = refresh_ads(
        db,
        scope=scope,
        bucket_start=bucket,
        etl_at=datetime.utcnow(),
        etl_job_id="package-lifecycle",
    )
    assert stored["kpis"] == final["kpis"]
    assert stored["time_zone"] == "UTC"

    # A projection-version backfill must work even when all source clocks are unchanged.
    fact.projection_version = 1
    db.flush()
    assert build_dwd(db, etl_at=datetime.utcnow()) == 1
    db.refresh(fact)
    assert fact.projection_version == 2


def test_intake_and_precise_collection_seconds_do_not_include_rejections_or_rounded_hours(
    db_session,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(0.01, 0.01)
    )
    for episode in episodes:
        episode.metadata_json = {
            "timing": {
                "duration_s": 10,
                "start_timestamp_ns": "1790058698624850944",
                "end_timestamp_ns": "1790058708624850945",
            }
        }
    db_session.commit()
    scope = DashboardScope.for_workspace(workspace.id)
    pending = build_live_overview_payload(db_session, scope)
    assert pending["kpis"]["intake_valid_duration_s"] == 0.0
    db_session.commit()
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[episodes[0].id],
        reason="Reject blurred source",
    )
    db_session.commit()
    approved = build_live_overview_payload(db_session, scope)
    assert approved["kpis"]["source_duration_s"] == 20.000000002
    assert approved["kpis"]["intake_valid_duration_s"] == 10.000000001
    assert sum(q["pending"] for q in approved["today_queues"]) == 1
    assert db_session.get(DwdEpisodeFact, episodes[0].id).queue_key is None
    exact = get_collection_overview(db_session, workspace_id=workspace.id)
    assert exact["captured_duration_s"] == 20.000000002
    assert exact["intake_valid_duration_s"] == 10.000000001
    assert exact["intake_valid_duration_hours"] == 0
    assert exact["duration_basis"] == "intake_valid_duration_s"

    empty_workspace = make_workspace(db_session)
    empty = build_live_overview_payload(
        db_session, DashboardScope.for_workspace(empty_workspace.id)
    )
    assert empty["kpis"]["total_episodes"] == 0
    assert empty["kpis"]["source_duration_s"] == 0.0
    assert empty["collect_trend_7d"] == []
    assert empty["device_distribution"] == {"total": 0, "items": []}
    assert sum(q["pending"] for q in empty["today_queues"]) == 0
    assert (
        get_collection_overview(db_session, workspace_id=empty_workspace.id)[
            "intake_valid_duration_s"
        ]
        == 0.0
    )


def test_partial_intake_excluded_admission_is_terminal_without_becoming_valid(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(0.01, 0.01)
    )
    fact = db_session.query(EpisodeAdmissionFact).filter_by(episode_id=episodes[0].id).one()
    fact.integrity_status = "failed"
    db_session.commit()
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="Partial acceptance",
    )
    db_session.commit()
    payload = build_live_overview_payload(db_session, DashboardScope.for_workspace(workspace.id))
    assert payload["kpis"]["source_duration_s"] == 72.0
    assert payload["kpis"]["intake_valid_duration_s"] == 36.0
    assert sum(q["pending"] for q in payload["today_queues"]) == 1
    excluded = db_session.get(DwdEpisodeFact, episodes[0].id)
    assert excluded.workflow_status == "intake_excluded"
    assert excluded.queue_key is None
    assert not excluded.annotation_eligible
    assert not excluded.is_qrdf_baseline
    # The review's frozen exclusion is projected without adding a source-side state.
    db_session.refresh(episodes[0])
    assert episodes[0].validity_status == "valid"


def test_future_dependency_timestamp_cannot_hide_later_work_item_updates(package_work):
    work, db = package_work, package_work["db"]
    future = datetime.utcnow() + timedelta(days=7)
    work["package"].updated_at = future
    db.commit()
    _overview(work)
    db.commit()
    fact = db.get(DwdEpisodeFact, work["episodes"][0].id)
    assert fact.source_updated_at == future
    old_revision = fact.source_revision

    save(work)
    assert submit(work).status_code == 200
    payload = _overview(work)
    assert next(q for q in payload["today_queues"] if q["key"] == "annotated")["pending"] == 2
    db.refresh(fact)
    assert fact.source_updated_at == future
    assert fact.source_revision != old_revision
    db.commit()
    assert build_dwd(db, etl_at=datetime.utcnow()) == 0

    # Correcting the future clock is itself a dependency change, even though
    # the greatest timestamp moves backwards.
    work["package"].updated_at = datetime.utcnow()
    db.commit()
    assert build_dwd(db, etl_at=datetime.utcnow()) == 2
    db.refresh(fact)
    assert fact.source_updated_at < future
