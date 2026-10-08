"""Package-level dashboard facts."""
# ruff: noqa: E701, E702

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from tests.collection_api_fixtures import (
    make_project,
    make_workspace,
    seed_package_pending_intake_review,
)
from tests.test_episode_objects import ref, verified_entries

from data.database import Episode
from data.services.collection_intake_review import review_data_package_intake
from data.services.episode_admission import record_episode_admission_fact
from data.services.episode_objects import object_entry
from data.services.package_dashboard_facts import (
    capture_facts,
    episode_start_ns,
    raw_size_bytes,
    recompute_package_dashboard_facts,
)

START_NS = 1_790_000_000_000_000_000


def _objects(raw_size: int):
    entries = verified_entries()
    entries[0] = object_entry(
        path="data.mcap", kind="data", ref=ref("raw", f"raw/{uuid4().hex}", size=raw_size)
    )
    return entries


def _timed(db, episode, *, start_ns, seconds, raw_size, attempt=2):
    episode.metadata_json = {
        **episode.metadata_json,
        "timing": {
            "start_timestamp_ns": str(start_ns),
            "end_timestamp_ns": str(start_ns + seconds * 1_000_000_000),
            "duration_s": float(seconds),
        },
    }
    db.flush()
    record_episode_admission_fact(
        db,
        episode_id=episode.id,
        attempt=attempt,
        source_fingerprint=episode.source_fingerprint,
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        qrdf_profile="ego",
        report_ref={"uri": f"db://episode/{episode.id}/report"},
        objects=_objects(raw_size),
    )


def test_raw_size_bytes_counts_only_raw_objects():
    assert raw_size_bytes(_objects(100)) == 100
    assert raw_size_bytes([]) == 0
    assert raw_size_bytes(None) == 0
    assert raw_size_bytes([{"ref": {"bucket_role": "raw", "size_bytes": True}}]) == 0


def test_episode_start_ns_reads_timing():
    assert episode_start_ns({"timing": {"start_timestamp_ns": "12"}}) == 12
    assert episode_start_ns({"timing": {"duration_s": 3}}) is None
    assert episode_start_ns(None) is None


def test_capture_facts_use_exact_seconds_raw_bytes_and_earliest_start(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    _timed(db_session, episodes[0], start_ns=START_NS + 5_000_000_000, seconds=1800, raw_size=100)
    _timed(db_session, episodes[1], start_ns=START_NS, seconds=1201, raw_size=50)
    db_session.commit()
    facts = capture_facts(db_session, package.id)
    assert facts["captured_duration_s"] == Decimal("3001.000")
    assert facts["captured_size_bytes"] == 150
    assert facts["captured_started_at"] == datetime(2026, 9, 21, 14, 13, 20)


def test_capture_facts_are_unknown_without_episodes(db_session):
    assert capture_facts(db_session, 999999999) == {
        "captured_started_at": None,
        "captured_duration_s": None,
        "captured_size_bytes": None,
    }


def test_intake_review_writes_capture_and_valid_facts(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    _timed(db_session, episodes[0], start_ns=START_NS, seconds=1800, raw_size=100)
    _timed(db_session, episodes[1], start_ns=START_NS, seconds=600, raw_size=40)
    db_session.commit()
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[episodes[1].id],
        reason="",
    )
    db_session.commit()
    db_session.refresh(package)
    assert package.captured_duration_s == Decimal("2400.000")
    assert package.captured_size_bytes == 140
    assert package.intake_valid_duration_s == Decimal("1800.000")
    assert package.intake_valid_size_bytes == 100


def test_rejected_package_has_zero_valid_facts(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    _timed(db_session, episodes[0], start_ns=START_NS, seconds=1800, raw_size=100)
    db_session.commit()
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="rejected",
        rejected_episode_ids=[],
        reason="整包不合格",
    )
    db_session.commit()
    db_session.refresh(package)
    assert package.intake_valid_duration_s == Decimal("0.000")
    assert package.intake_valid_size_bytes == 0


def test_recompute_uses_frozen_attempt_and_is_idempotent(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    _timed(db_session, episodes[0], start_ns=START_NS, seconds=1800, raw_size=100)
    _timed(db_session, episodes[1], start_ns=START_NS, seconds=600, raw_size=40)
    db_session.commit()
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )
    db_session.commit()
    for column in (
        "captured_started_at",
        "captured_duration_s",
        "captured_size_bytes",
        "intake_valid_duration_s",
        "intake_valid_size_bytes",
    ):
        setattr(package, column, None)
    db_session.commit()
    recompute_package_dashboard_facts(db_session, package)
    db_session.commit()
    first = (package.intake_valid_duration_s, package.intake_valid_size_bytes)
    recompute_package_dashboard_facts(db_session, package)
    db_session.commit()
    assert first == (Decimal("2400.000"), 140)
    assert (package.intake_valid_duration_s, package.intake_valid_size_bytes) == first
    assert package.captured_size_bytes == 140
    assert db_session.query(Episode).filter(Episode.data_package_id == package.id).count() == 2


def test_recompute_drops_frozen_fact_when_source_fingerprint_changed(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(db_session, workspace, project)
    _timed(db_session, episodes[0], start_ns=START_NS, seconds=1800, raw_size=100)
    db_session.commit()
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[episodes[1].id],
        reason="",
    )
    db_session.commit()
    episodes[0].source_fingerprint = "changed-source"
    db_session.commit()
    recompute_package_dashboard_facts(db_session, package)
    assert package.intake_valid_duration_s == Decimal("0.000")
    assert package.intake_valid_size_bytes == 0
