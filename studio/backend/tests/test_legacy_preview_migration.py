"""Contracts for the explicit one-time legacy preview migration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from data.database import (
    ArtifactOperation,
    Batch,
    Episode,
    EpisodeArtifact,
    JobQueueSlot,
    JobRun,
    RealtimeEvent,
    TaskSet,
    User,
    WorkItem,
    Workspace,
)


@dataclass(frozen=True)
class _MigrationContext:
    workspace: Workspace
    source: Episode
    review: WorkItem
    children: tuple[Episode, ...]
    legacy_jobs: tuple[JobRun, ...]


def _migration_context(db_session, *, workspace: Workspace | None = None) -> _MigrationContext:
    suffix = uuid4().hex
    actor = db_session.query(User).order_by(User.id).first()
    if workspace is None:
        workspace = Workspace(name=f"legacy migration workspace {suffix}", creator="test")
        db_session.add(workspace)
        db_session.flush()
    task_set = TaskSet(workspace_id=workspace.id, name=f"legacy migration task set {suffix}")
    db_session.add(task_set)
    db_session.flush()
    batch = Batch(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        name=f"legacy migration batch {suffix}",
        batch_type="ego",
    )
    db_session.add(batch)
    db_session.flush()
    source = Episode(
        workspace_id=workspace.id,
        task_set_id=task_set.id,
        batch_id=batch.id,
        episode_uid=f"legacy-migration-source-{suffix}",
        kind="source",
        modality="ego",
        quality_status="passed",
        source_fingerprint=uuid4().hex + uuid4().hex,
    )
    db_session.add(source)
    db_session.flush()
    cut_item = WorkItem(
        workspace_id=workspace.id,
        episode_id=source.id,
        kind="cut",
        status="accepted",
        created_by_user_id=actor.id,
    )
    db_session.add(cut_item)
    db_session.flush()
    review = WorkItem(
        workspace_id=workspace.id,
        episode_id=source.id,
        kind="review",
        review_target_kind="cut",
        review_of_work_item_id=cut_item.id,
        status="accepted",
        created_by_user_id=actor.id,
    )
    db_session.add(review)
    db_session.flush()
    children: list[Episode] = []
    for index, (start_ns, end_ns) in enumerate(((100, 300), (300, 500), (500, 700)), start=1):
        child = Episode(
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            batch_id=batch.id,
            episode_uid=f"legacy-migration-child-{index}-{suffix}",
            kind="derived",
            parent_episode_id=source.id,
            derivation_version=1,
            modality="ego",
            quality_status="passed",
            source_fingerprint=source.source_fingerprint,
            source_start_ns=start_ns,
            source_end_ns=end_ns,
            metadata_json={"lineage": {"cut_review_work_item_id": review.id}},
        )
        db_session.add(child)
        children.append(child)
    db_session.flush()
    _published_preview(
        db_session,
        episode=source,
        timestamps=(100, 200, 300, 400, 500, 600),
    )
    _published_preview(db_session, episode=children[0], timestamps=(100, 200))

    statuses = ("queued", "retry_pending", "succeeded")
    legacy_jobs: list[JobRun] = []
    for index, (child, status) in enumerate(zip(children, statuses, strict=True), start=1):
        job = JobRun(
            id=uuid4().hex,
            kind="episode_preview",
            resource_type="episode",
            resource_id=str(child.id),
            workspace_id=workspace.id,
            task_set_id=task_set.id,
            idempotency_key=f"legacy-migration-preview-{uuid4().hex}",
            queue="media",
            status=status,
            phase=status,
        )
        db_session.add(job)
        legacy_jobs.append(job)
        if status in {"queued", "retry_pending"}:
            db_session.flush()
            db_session.add(
                JobQueueSlot(
                    queue="media",
                    slot_number=source.id * 10 + index,
                    job_id=job.id,
                    worker_id=f"legacy-worker-{index}",
                    lease_expires_at=datetime.utcnow() - timedelta(minutes=1),
                )
            )
    db_session.commit()
    return _MigrationContext(
        workspace=workspace,
        source=source,
        review=review,
        children=tuple(children),
        legacy_jobs=tuple(legacy_jobs),
    )


def _published_preview(
    db_session, *, episode: Episode, timestamps: tuple[int, ...]
) -> EpisodeArtifact:
    payload = f"preview-{episode.id}-{timestamps}".encode("ascii")
    checksum = hashlib.sha256(payload).hexdigest()
    artifact = EpisodeArtifact(
        episode_id=episode.id,
        artifact_type="process_preview",
        storage_role="process",
        storage_uri=f"nas://process/v2/migration/{episode.id}/{uuid4().hex}.mp4",
        checksum_sha256=checksum,
        size_bytes=len(payload),
        manifest_hash=checksum,
        retention_policy="permanent",
        metadata_json={
            "media_type": "video/mp4",
            "reference_topic": "/camera/front/rgb",
            "encoded_fps": 20.0,
            "frame_count": len(timestamps),
            "frame_timestamps_ns": [str(value) for value in timestamps],
        },
    )
    db_session.add(artifact)
    db_session.flush()
    db_session.add(
        ArtifactOperation(
            id=uuid4().hex,
            artifact_id=artifact.id,
            operation_kind="process_preview_publish",
            status="published",
            target_uri=artifact.storage_uri,
            checksum_sha256=checksum,
            size_bytes=len(payload),
            manifest_json={"kind": "file", "entries": []},
        )
    )
    return artifact


def _target(context: _MigrationContext):
    from data.services.legacy_preview_migration import LegacyPreviewMigrationTarget

    return LegacyPreviewMigrationTarget(
        source_episode_id=context.source.id,
        cut_review_work_item_id=context.review.id,
    )


def test_preview_reports_counts_without_mutating(db_session):
    from data.services.legacy_preview_migration import migrate_legacy_preview_jobs

    context = _migration_context(db_session)

    results = migrate_legacy_preview_jobs(db_session, targets=(_target(context),), apply=False)

    assert len(results) == 1
    assert results[0].batch_job_id is None
    assert results[0].child_count == 3
    assert results[0].published_child_count == 1
    assert results[0].active_legacy_count == 2
    assert results[0].cancelled_legacy_count == 0
    assert results[0].terminal_legacy_count == 1
    assert results[0].active_running_lease_count == 0
    assert (
        db_session.query(JobRun)
        .filter_by(kind="derived_preview_batch", resource_id=str(context.source.id))
        .count()
        == 0
    )
    assert [db_session.get(JobRun, job.id).status for job in context.legacy_jobs] == [
        "queued",
        "retry_pending",
        "succeeded",
    ]


def test_apply_creates_one_batch_and_cancels_only_nonterminal_legacy_jobs(db_session):
    from data.services.legacy_preview_migration import migrate_legacy_preview_jobs

    context = _migration_context(db_session)

    results = migrate_legacy_preview_jobs(db_session, targets=(_target(context),), apply=True)

    batch = (
        db_session.query(JobRun)
        .filter_by(kind="derived_preview_batch", resource_id=str(context.source.id))
        .one()
    )
    assert results[0].batch_job_id == batch.id
    assert results[0].cancelled_legacy_count == 2
    assert batch.status == "queued"
    queued, retry_pending, succeeded = (
        db_session.get(JobRun, job.id) for job in context.legacy_jobs
    )
    assert queued.status == retry_pending.status == "cancelled"
    assert queued.phase == retry_pending.phase == "cancelled"
    assert queued.error_code == retry_pending.error_code == "migrated_to_batch"
    assert (
        queued.error_message
        == retry_pending.error_message
        == (f"migrated_to_derived_preview_batch:{batch.id}")
    )
    assert succeeded.status == "succeeded"
    assert (
        db_session.query(JobQueueSlot)
        .filter(JobQueueSlot.job_id.in_([job.id for job in context.legacy_jobs]))
        .count()
        == 0
    )


def test_apply_is_idempotent(db_session):
    from data.services.legacy_preview_migration import migrate_legacy_preview_jobs

    context = _migration_context(db_session)
    first = migrate_legacy_preview_jobs(db_session, targets=(_target(context),), apply=True)

    second = migrate_legacy_preview_jobs(db_session, targets=(_target(context),), apply=True)

    assert second[0].batch_job_id == first[0].batch_job_id
    assert second[0].cancelled_legacy_count == 0
    assert (
        db_session.query(JobRun)
        .filter_by(kind="derived_preview_batch", resource_id=str(context.source.id))
        .count()
        == 1
    )


def test_apply_rejects_active_running_lease_and_rolls_back_all_targets(db_session):
    from data.services.legacy_preview_migration import (
        LegacyPreviewMigrationError,
        migrate_legacy_preview_jobs,
    )

    first = _migration_context(db_session)
    second = _migration_context(db_session)
    active = JobRun(
        id=uuid4().hex,
        kind="episode_preview",
        resource_type="episode",
        resource_id=str(second.children[0].id),
        workspace_id=second.workspace.id,
        task_set_id=second.source.task_set_id,
        idempotency_key=f"active-legacy-preview-{uuid4().hex}",
        queue="media",
        status="running",
        phase="running",
        lease_worker_id="active-worker",
        lease_token=uuid4().hex,
        lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
    )
    db_session.add(active)
    db_session.commit()

    with pytest.raises(LegacyPreviewMigrationError, match="active worker leases"):
        migrate_legacy_preview_jobs(
            db_session,
            targets=(_target(first), _target(second)),
            apply=True,
        )

    assert (
        db_session.query(JobRun)
        .filter(
            JobRun.kind == "derived_preview_batch",
            JobRun.resource_id.in_((str(first.source.id), str(second.source.id))),
        )
        .count()
        == 0
    )
    assert db_session.get(JobRun, first.legacy_jobs[0].id).status == "queued"
    assert db_session.get(JobRun, active.id).status == "running"


@pytest.mark.parametrize("invalid_contract", ("review", "parent_preview"))
def test_migration_rejects_unaccepted_review_or_untrusted_parent(db_session, invalid_contract):
    from data.services.legacy_preview_migration import (
        LegacyPreviewMigrationError,
        migrate_legacy_preview_jobs,
    )

    context = _migration_context(db_session)
    if invalid_contract == "review":
        context.review.status = "submitted"
    else:
        operation = (
            db_session.query(ArtifactOperation)
            .join(EpisodeArtifact, ArtifactOperation.artifact_id == EpisodeArtifact.id)
            .filter(EpisodeArtifact.episode_id == context.source.id)
            .one()
        )
        operation.status = "cleanup_pending"
    db_session.commit()

    with pytest.raises(LegacyPreviewMigrationError):
        migrate_legacy_preview_jobs(db_session, targets=(_target(context),), apply=True)

    assert (
        db_session.query(JobRun)
        .filter_by(kind="derived_preview_batch", resource_id=str(context.source.id))
        .count()
        == 0
    )
    assert db_session.get(JobRun, context.legacy_jobs[0].id).status == "queued"


def test_apply_releases_legacy_queue_slots_and_invalidates_workspace_once(db_session):
    from data.services.legacy_preview_migration import migrate_legacy_preview_jobs

    workspace = Workspace(name=f"shared migration workspace {uuid4().hex}", creator="test")
    db_session.add(workspace)
    db_session.commit()
    first = _migration_context(db_session, workspace=workspace)
    second = _migration_context(db_session, workspace=workspace)

    migrate_legacy_preview_jobs(
        db_session,
        targets=(_target(first), _target(second)),
        apply=True,
    )

    assert (
        db_session.query(JobQueueSlot)
        .filter(
            JobQueueSlot.job_id.in_(
                [job.id for context in (first, second) for job in context.legacy_jobs]
            )
        )
        .count()
        == 0
    )
    assert (
        db_session.query(RealtimeEvent)
        .filter_by(
            resource_type="work_queue",
            resource_id=str(workspace.id),
            event_name="work_queue.invalidated",
        )
        .count()
        == 1
    )


def test_parse_source_review_accepts_positive_ids():
    from scripts.migrate_legacy_derived_preview_jobs import parse_source_review

    from data.services.legacy_preview_migration import LegacyPreviewMigrationTarget

    assert parse_source_review("29:70") == LegacyPreviewMigrationTarget(29, 70)


@pytest.mark.parametrize("value", ("", "29", "29:", ":70", "0:70", "29:-1", "x:70"))
def test_parse_source_review_rejects_invalid_values(value):
    import argparse

    from scripts.migrate_legacy_derived_preview_jobs import parse_source_review

    with pytest.raises(argparse.ArgumentTypeError):
        parse_source_review(value)


def test_cli_requires_exactly_one_mode():
    from scripts.migrate_legacy_derived_preview_jobs import _parser

    parser = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--source-review", "29:70"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--source-review", "29:70", "--check", "--apply"])


def test_cli_rejects_duplicate_targets_before_schema_check(monkeypatch):
    from scripts import migrate_legacy_derived_preview_jobs as command

    monkeypatch.setattr(
        command,
        "assert_schema_current",
        lambda: pytest.fail("schema check must not run for duplicate targets"),
    )

    with pytest.raises(SystemExit):
        command.main(
            [
                "--source-review",
                "29:70",
                "--source-review",
                "29:70",
                "--check",
            ]
        )


def test_cli_checks_schema_and_prints_bounded_json(monkeypatch, capsys):
    from data.services.legacy_preview_migration import LegacyPreviewMigrationResult
    from scripts import migrate_legacy_derived_preview_jobs as command

    calls: list[object] = []

    class _FakeSession:
        def close(self):
            calls.append("closed")

    def fake_migrate(db, *, targets, apply):
        calls.append((db, targets, apply))
        return (
            LegacyPreviewMigrationResult(
                source_episode_id=29,
                cut_review_work_item_id=70,
                batch_job_id=None,
                child_count=338,
                published_child_count=0,
                active_legacy_count=338,
                cancelled_legacy_count=0,
                terminal_legacy_count=0,
                active_running_lease_count=0,
            ),
        )

    session = _FakeSession()
    monkeypatch.setattr(command, "assert_schema_current", lambda: calls.append("schema"))
    monkeypatch.setattr(command, "SessionLocal", lambda: session)
    monkeypatch.setattr(command, "migrate_legacy_preview_jobs", fake_migrate)

    assert command.main(["--source-review", "29:70", "--check"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "mode": "check",
        "targets": [
            {
                "active_legacy_count": 338,
                "active_running_lease_count": 0,
                "batch_job_id": None,
                "cancelled_legacy_count": 0,
                "child_count": 338,
                "cut_review_work_item_id": 70,
                "published_child_count": 0,
                "source_episode_id": 29,
                "terminal_legacy_count": 0,
            }
        ],
    }
    assert calls[0] == "schema"
    assert calls[-1] == "closed"
