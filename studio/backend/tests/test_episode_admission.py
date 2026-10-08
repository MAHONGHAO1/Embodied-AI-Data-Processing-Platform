"""Episode-level admission facts and database-only intake gates."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from tests.collection_api_fixtures import (
    make_annotator_reviewer,
    make_project,
    make_workspace,
    seed_package_pending_intake_review,
)
from tests.test_episode_objects import verified_entries

from data.database import Episode, SessionLocal
from data.models.data_batch import DataBatchEpisode
from data.models.episode_admission import EpisodeAdmissionFact
from data.services.collection_intake_review import (
    IntakeReviewConflictError,
    review_data_package_intake,
)
from data.services.data_batches import (
    BatchConflictError,
    create_data_batch,
    list_batch_candidates,
)
from data.services.episode_admission import (
    EpisodeAdmissionConflictError,
    admission_eligibility,
    current_episode_admission_fact,
    package_admission_counts,
    record_episode_admission_fact,
)


def _valid_fact(db_session, episode: Episode, *, attempt: int = 1):
    current = current_episode_admission_fact(db_session, episode_id=episode.id)
    if (
        current is not None
        and current.attempt == attempt
        and current.source_fingerprint == episode.source_fingerprint
        and current.validation_policy_version == "v1"
        and current.integrity_status == "passed"
        and current.output_verification_status == "verified"
    ):
        return current
    return record_episode_admission_fact(
        db_session,
        episode_id=episode.id,
        attempt=attempt,
        source_fingerprint=episode.source_fingerprint,
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready" if episode.modality == "rgb" else "not_applicable",
        output_verification_status="verified",
        qrdf_profile="ego",
        report_ref={"uri": f"db://episode/{episode.id}/report"},
        objects=verified_entries(),
    )


def _create_batch(db_session, workspace_id: int, package_ids: list[int], name: str):
    return create_data_batch(
        db_session,
        workspace_id=workspace_id,
        name=name,
        data_package_ids=package_ids,
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


@pytest.fixture
def deny_storage(monkeypatch):
    """Any accidental storage access fails immediately during admission APIs."""
    from data.infra import oss_client

    names = (
        "bucket_name",
        "is_oss_configured",
        "cloud_enabled",
        "object_exists",
        "object_info",
        "list_prefix",
        "list_prefix_page",
        "list_prefix_directory_page",
        "download_to",
        "download_object_to_file",
        "upload_file",
        "copy_object",
    )

    def deny(*_args, **_kwargs):
        raise AssertionError("admission review/batch must not access storage")

    for name in names:
        monkeypatch.setattr(oss_client, name, deny)


def test_review_and_batch_exclude_failed_episode_without_storage(db_session, deny_storage):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,) * 10
    )
    for episode in episodes[:8]:
        _valid_fact(db_session, episode)
    for episode in episodes[8:]:
        record_episode_admission_fact(
            db_session,
            episode_id=episode.id,
            attempt=2,
            source_fingerprint=episode.source_fingerprint,
            validation_policy_version="v1",
            integrity_status="failed",
            preview_status="failed",
            output_verification_status="failed",
            error_code="bad_capture",
            error_message="broken source",
        )
    db_session.commit()

    reviewed_package, review = review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )
    assert reviewed_package.status == "intake_approved"
    assert len(review.accepted_episode_ids_json) == 8
    assert len(review.excluded_episodes_json) == 2
    assert {item["episode_id"] for item in review.excluded_episodes_json} == {
        episodes[8].id,
        episodes[9].id,
    }
    db_session.commit()

    batch, _ = _create_batch(db_session, workspace.id, [package.id], "accepted-only")
    db_session.commit()
    members = (
        db_session.query(DataBatchEpisode).filter(DataBatchEpisode.data_batch_id == batch.id).all()
    )
    assert {row.episode_id for row in members} == {episode.id for episode in episodes[:8]}
    assert batch.episode_count == 8


def test_repair_creates_new_attempt_and_only_repaired_episode_enters_new_batch(
    db_session,
    deny_storage,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,) * 10
    )
    for episode in episodes[8:]:
        record_episode_admission_fact(
            db_session,
            episode_id=episode.id,
            attempt=2,
            source_fingerprint=episode.source_fingerprint,
            validation_policy_version="v1",
            integrity_status="failed",
            preview_status="failed",
            output_verification_status="failed",
            error_code="repair_required",
        )
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
    first_batch, _ = _create_batch(db_session, workspace.id, [package.id], "first")
    db_session.commit()
    assert {
        row.episode_id
        for row in db_session.query(DataBatchEpisode).filter(
            DataBatchEpisode.data_batch_id == first_batch.id
        )
    } == {episode.id for episode in episodes[:8]}

    for episode in episodes[8:]:
        episode.source_fingerprint = "repaired-source"
        db_session.flush()
        _valid_fact(db_session, episode, attempt=3)
    db_session.commit()

    # A repaired current fact is not a candidate until a human binds that
    # attempt in a fresh approved review.
    assert package not in list_batch_candidates(db_session, workspace.id)
    with pytest.raises(BatchConflictError, match="(no_eligible_episodes|already_batched_episode)"):
        _create_batch(db_session, workspace.id, [package.id], "before-recheck")
    db_session.rollback()
    # A package can be reviewed again after a repair; only the new current
    # attempt is eligible and the prior batch remains immutable.
    review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="recheck",
    )
    db_session.commit()
    assert str(package.intake_valid_duration_hours) == "10.00"
    second_batch, _ = _create_batch(db_session, workspace.id, [package.id], "second")
    db_session.commit()
    assert {
        row.episode_id
        for row in db_session.query(DataBatchEpisode).filter(
            DataBatchEpisode.data_batch_id == second_batch.id
        )
    } == {episode.id for episode in episodes[8:]}
    assert first_batch.episode_count == 8
    assert first_batch.valid_duration_hours == Decimal("8.00")
    assert second_batch.episode_count == 2
    assert second_batch.valid_duration_hours == Decimal("2.00")


def test_running_and_all_failed_facts_are_not_batchable(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0, 1.0)
    )
    record_episode_admission_fact(
        db_session,
        episode_id=episodes[0].id,
        attempt=2,
        source_fingerprint=episodes[0].source_fingerprint,
        validation_policy_version="v1",
        integrity_status="running",
        preview_status="running",
        output_verification_status="running",
    )
    record_episode_admission_fact(
        db_session,
        episode_id=episodes[1].id,
        attempt=2,
        source_fingerprint=episodes[1].source_fingerprint,
        validation_policy_version="v1",
        integrity_status="failed",
        preview_status="failed",
        output_verification_status="failed",
    )
    db_session.commit()

    _package, review = review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )
    assert review.accepted_episode_ids_json == []
    assert {item["reason"] for item in review.excluded_episodes_json} == {
        "integrity_running",
        "integrity_failed",
    }
    assert package_admission_counts(
        db_session, workspace_id=workspace.id, data_package_id=package.id
    ) == {"ready": 0, "running": 1, "failed": 1, "reviewed": 0}
    before_batches = db_session.query(DataBatchEpisode).count()
    with pytest.raises(BatchConflictError, match="no_eligible_episodes"):
        _create_batch(db_session, workspace.id, [package.id], "all-failed")
    db_session.rollback()
    assert db_session.query(DataBatchEpisode).count() == before_batches


def test_episode_occupancy_is_global_and_concurrent_claim_is_conflict(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0, 1.0)
    )
    for episode in episodes:
        _valid_fact(db_session, episode)
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
    first, _ = _create_batch(db_session, workspace.id, [package.id], "owner")
    db_session.commit()
    assert first is not None

    package.status = "intake_approved"
    db_session.commit()
    with pytest.raises(BatchConflictError, match="already_batched_episode"):
        _create_batch(db_session, workspace.id, [package.id], "contender")


def test_two_sessions_racing_for_one_episode_only_one_batch_wins(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, _episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
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
    workspace_id = workspace.id
    package_id = package.id
    before_batches = db_session.query(DataBatchEpisode).count()

    def claim(name: str):
        session = SessionLocal()
        try:
            _batch, _ = _create_batch(session, workspace_id, [package_id], name)
            session.commit()
            return "won"
        except BatchConflictError:
            session.rollback()
            return "lost"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ("race-a", "race-b")))
    assert sorted(outcomes) == ["lost", "won"]
    assert db_session.query(DataBatchEpisode).count() == before_batches + 1


def test_admission_fact_is_fail_closed_for_stale_source_and_unknown_policy(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, (episode,) = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    _valid_fact(db_session, episode)
    db_session.commit()
    episode.source_fingerprint = "changed-source"
    db_session.commit()

    _package, review = review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )
    assert review.accepted_episode_ids_json == []
    assert review.excluded_episodes_json == [
        {"episode_id": episode.id, "reason": "source_fingerprint_changed"}
    ]

    fact = (
        db_session.query(EpisodeAdmissionFact)
        .filter(EpisodeAdmissionFact.episode_id == episode.id)
        .one()
    )
    assert fact.source_fingerprint != episode.source_fingerprint


def test_source_repair_requires_new_attempt_and_review_does_not_repeat_old_episode(
    db_session,
):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, (episode,) = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    _package, first_review = review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )
    db_session.commit()
    assert first_review.accepted_episode_ids_json == [episode.id]

    episode.source_fingerprint = "repaired-source"
    db_session.commit()
    with pytest.raises(IntakeReviewConflictError, match="already_reviewed"):
        review_data_package_intake(
            db_session,
            workspace_id=workspace.id,
            data_package_id=package.id,
            reviewer_user_id=None,
            verdict="approved",
            rejected_episode_ids=[],
            reason="",
        )
    db_session.rollback()

    _valid_fact(db_session, episode, attempt=2)
    db_session.commit()
    _package, second_review = review_data_package_intake(
        db_session,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="repaired",
    )
    assert second_review.accepted_episode_ids_json == [episode.id]


def test_admission_fact_rejects_non_json_report_ref(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    _package, (episode,) = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    with pytest.raises(ValueError, match="report_ref must be JSON serializable"):
        record_episode_admission_fact(
            db_session,
            episode_id=episode.id,
            attempt=2,
            source_fingerprint=episode.source_fingerprint,
            validation_policy_version="v1",
            integrity_status="passed",
            preview_status="ready",
            output_verification_status="verified",
            report_ref={"object": object()},
        )

    fact = record_episode_admission_fact(
        db_session,
        episode_id=episode.id,
        attempt=3,
        source_fingerprint=episode.source_fingerprint,
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        report_ref={"uri": "db://report"},
        objects=verified_entries(),
    )
    assert fact.objects_json[0]["ref"]["bucket_role"] == "raw"


def test_stale_worker_attempt_cannot_overwrite_current_fact(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    _package, (episode,) = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    _valid_fact(db_session, episode, attempt=2)
    db_session.commit()
    with pytest.raises(EpisodeAdmissionConflictError, match="admission_attempt_conflict"):
        record_episode_admission_fact(
            db_session,
            episode_id=episode.id,
            attempt=2,
            source_fingerprint=episode.source_fingerprint,
            validation_policy_version="v1",
            integrity_status="failed",
            preview_status="failed",
            output_verification_status="failed",
        )
    db_session.rollback()
    with pytest.raises(EpisodeAdmissionConflictError, match="stale_admission_attempt"):
        record_episode_admission_fact(
            db_session,
            episode_id=episode.id,
            attempt=1,
            source_fingerprint=episode.source_fingerprint,
            validation_policy_version="v1",
            integrity_status="failed",
            preview_status="failed",
            output_verification_status="failed",
        )
    db_session.rollback()
    current = (
        db_session.query(EpisodeAdmissionFact)
        .filter(
            EpisodeAdmissionFact.episode_id == episode.id,
            EpisodeAdmissionFact.is_current.is_(True),
        )
        .one()
    )
    assert current.attempt == 2
    assert current.integrity_status == "passed"


def _approve(db, workspace, package):
    return review_data_package_intake(
        db,
        workspace_id=workspace.id,
        data_package_id=package.id,
        reviewer_user_id=None,
        verdict="approved",
        rejected_episode_ids=[],
        reason="",
    )[1]


def test_review_counts_bind_current_attempt_and_three_reviews_keep_history(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0, 2.0, 3.0)
    )
    for episode in episodes[1:]:
        record_episode_admission_fact(
            db_session,
            episode_id=episode.id,
            attempt=2,
            source_fingerprint=episode.source_fingerprint,
            validation_policy_version="v1",
            integrity_status="failed",
            preview_status="failed",
            output_verification_status="failed",
        )
    assert (
        package_admission_counts(db_session, workspace_id=workspace.id, data_package_id=package.id)[
            "reviewed"
        ]
        == 0
    )
    _approve(db_session, workspace, package)
    assert package.intake_valid_duration_hours == Decimal("1.00")
    assert (
        package_admission_counts(db_session, workspace_id=workspace.id, data_package_id=package.id)[
            "reviewed"
        ]
        == 1
    )
    for index, expected in ((1, "3.00"), (2, "6.00")):
        _valid_fact(db_session, episodes[index], attempt=3)
        review = _approve(db_session, workspace, package)
        assert review.accepted_episode_ids_json == [episodes[index].id]
        assert package.intake_valid_duration_hours == Decimal(expected)
    _valid_fact(db_session, episodes[0], attempt=2)
    assert (
        package_admission_counts(db_session, workspace_id=workspace.id, data_package_id=package.id)[
            "reviewed"
        ]
        == 2
    )


def test_same_attempt_payload_is_idempotent_and_changes_conflict(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    _, (episode,) = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    payload = {
        "episode_id": episode.id,
        "attempt": 2,
        "source_fingerprint": episode.source_fingerprint,
        "validation_policy_version": "v1",
        "integrity_status": "passed",
        "preview_status": "ready",
        "output_verification_status": "verified",
        "report_ref": {"uri": "db://report"},
        "objects": verified_entries(),
    }
    fact = record_episode_admission_fact(db_session, **payload)
    db_session.commit()
    checked_at = fact.checked_at
    assert record_episode_admission_fact(db_session, **payload).id == fact.id
    assert fact.checked_at == checked_at
    for changes in (
        {"preview_status": "failed"},
        {"source_fingerprint": "other"},
        {"validation_policy_version": "v2"},
        {"report_ref": {"uri": "db://different"}},
    ):
        with pytest.raises(EpisodeAdmissionConflictError, match="admission_attempt_conflict"):
            record_episode_admission_fact(db_session, **(payload | changes))


def test_empty_fingerprints_and_composite_detail_fail_closed(db_session):
    from data.routers.collection_packages import _episode_item

    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    _, (episode,) = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    fact = current_episode_admission_fact(db_session, episode_id=episode.id)
    episode.source_fingerprint = fact.source_fingerprint = ""
    assert admission_eligibility(episode, fact) == (False, "source_fingerprint_missing")
    episode.source_fingerprint = fact.source_fingerprint = "source"
    fact.preview_status = "running"
    assert _episode_item(episode, admission_fact=fact)["admission_status"] == "running"
    fact.preview_status = "ready"
    fact.output_verification_status = "failed"
    assert _episode_item(episode, admission_fact=fact)["admission_status"] == "failed"


def test_annotation_and_governance_use_frozen_batch_members(db_session, deny_storage):
    from data.models.data_batch import DataBatchGovernanceRun
    from data.routers.annotation_work_items import _item_payload
    from data.services.annotation_work_items import assign_work_items_for_batch
    from data.services.governance_runs import _writeback_governed_duration

    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, episodes = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0, 2.0)
    )
    record_episode_admission_fact(
        db_session,
        episode_id=episodes[1].id,
        attempt=2,
        source_fingerprint=episodes[1].source_fingerprint,
        validation_policy_version="v1",
        integrity_status="failed",
        preview_status="failed",
        output_verification_status="failed",
    )
    _approve(db_session, workspace, package)
    first, _ = _create_batch(db_session, workspace.id, [package.id], "snapshot-first")
    _valid_fact(db_session, episodes[1], attempt=3)
    _approve(db_session, workspace, package)
    second, _ = _create_batch(db_session, workspace.id, [package.id], "snapshot-second")
    # Change mutable package/episode fields and the current worker attempt after batching.
    package.intake_valid_duration_hours = Decimal("99.00")
    package.governed_valid_duration_hours = Decimal("99.00")
    episodes[0].metadata_json = {"timing": {"duration_hours": 50}}
    _valid_fact(db_session, episodes[0], attempt=2)
    annotator, reviewer = make_annotator_reviewer(db_session, workspace)
    for batch, episode, attempt, hours in (
        (first, episodes[0], 1, "1.00"),
        (second, episodes[1], 3, "2.00"),
    ):
        batch.annotation_enabled = True
        batch.annotator_user_ids_json = [annotator.id]
        batch.reviewer_user_id = reviewer.id
        items = assign_work_items_for_batch(db_session, batch_id=batch.id)
        assert len(items) == 1
        expected = [
            {
                "episode_id": episode.id,
                "admission_attempt": attempt,
                "duration_hours": hours,
            }
        ]
        assert items[0].episode_members_json == expected
        assert _item_payload(items[0])["episode_members"] == expected
        assert batch.assignment_snapshot_json["assignments"][0]["episode_members"] == expected
        _writeback_governed_duration(db_session, batch_id=batch.id, package_ids=[package.id])
        assert batch.valid_duration_hours == Decimal(hours)
        assert (
            assign_work_items_for_batch(db_session, batch_id=batch.id)[0].episode_members_json
            == expected
        )
        db_session.add(DataBatchGovernanceRun(data_batch_id=batch.id, status="passed"))
        db_session.flush()
    assert package.governed_valid_duration_hours == Decimal("3.00")


@pytest.mark.parametrize("overlap", [True, False])
def test_multi_package_opposite_order_claims_do_not_deadlock(db_session, overlap):
    from sqlalchemy import text

    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package_ids = []
    for _ in range(4):
        package, _ = seed_package_pending_intake_review(
            db_session, workspace, project, episode_hours=(1.0, 1.0)
        )
        _approve(db_session, workspace, package)
        package_ids.append(package.id)
    db_session.commit()
    workspace_id = workspace.id
    barrier = Barrier(2)

    def claim(args):
        name, ids = args
        with SessionLocal() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            try:
                _create_batch(session, workspace_id, ids, name)
                session.commit()
                return "won"
            except BatchConflictError:
                session.rollback()
                return "lost"

    second_package_ids = package_ids[1:3] if overlap else package_ids[2:]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                claim,
                (
                    ("multi-a", package_ids[:2]),
                    ("multi-b", list(reversed(second_package_ids))),
                ),
            )
        )
    assert sorted(outcomes) == (["lost", "won"] if overlap else ["won", "won"])


def _admission_episode(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    _package, (episode,) = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    return episode.id, episode.source_fingerprint


def test_verified_fact_stores_validated_objects(db_session):
    episode_id, fingerprint = _admission_episode(db_session)
    fact = record_episode_admission_fact(
        db_session,
        episode_id=episode_id,
        attempt=2,
        source_fingerprint=fingerprint,
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        report_ref={"ok": True},
        objects=verified_entries(),
    )
    db_session.commit()
    assert fact.objects_json == verified_entries()


def test_verified_fact_rejects_incomplete_objects(db_session):
    episode_id, fingerprint = _admission_episode(db_session)
    incomplete = [e for e in verified_entries() if e["kind"] != "metadata"]
    with pytest.raises(
        ValueError, match="episode_objects_unverified:episode_object_missing_metadata"
    ):
        record_episode_admission_fact(
            db_session,
            episode_id=episode_id,
            attempt=2,
            source_fingerprint=fingerprint,
            validation_policy_version="v1",
            integrity_status="passed",
            preview_status="ready",
            output_verification_status="verified",
            report_ref={"ok": True},
            objects=incomplete,
        )


def test_verified_fact_rejects_empty_objects(db_session):
    """Task 8 contract phase: objects are mandatory for every verified fact."""
    episode_id, fingerprint = _admission_episode(db_session)
    with pytest.raises(ValueError, match="episode_objects_unverified:episode_object_missing_data"):
        record_episode_admission_fact(
            db_session,
            episode_id=episode_id,
            attempt=2,
            source_fingerprint=fingerprint,
            validation_policy_version="v1",
            integrity_status="passed",
            preview_status="ready",
            output_verification_status="verified",
            report_ref={"ok": True},
            objects=[],
        )


def test_admission_fact_records_integrity_source(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    _package, (episode,) = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    assert current_episode_admission_fact(db_session, episode_id=episode.id).integrity_source == (
        "server"
    )
    fact = record_episode_admission_fact(
        db_session,
        episode_id=episode.id,
        attempt=2,
        source_fingerprint=episode.source_fingerprint,
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        report_ref={"ok": True},
        objects=verified_entries(),
        integrity_source="client",
    )
    db_session.commit()
    assert fact.integrity_source == "client"
    with pytest.raises(ValueError, match="integrity_source must be client or server"):
        record_episode_admission_fact(
            db_session,
            episode_id=episode.id,
            attempt=3,
            source_fingerprint=episode.source_fingerprint,
            validation_policy_version="v1",
            integrity_status="passed",
            preview_status="ready",
            output_verification_status="verified",
            report_ref={"ok": True},
            objects=verified_entries(),
            integrity_source="duance",
        )


def test_integrity_source_column_is_constrained():
    from sqlalchemy import inspect

    from data.database import engine

    inspector = inspect(engine)
    columns = {
        column["name"]: column for column in inspector.get_columns("episode_admission_facts")
    }
    assert columns["integrity_source"]["nullable"] is False
    checks = {item["name"] for item in inspector.get_check_constraints("episode_admission_facts")}
    assert "ck_episode_admission_facts_integrity_source" in checks
