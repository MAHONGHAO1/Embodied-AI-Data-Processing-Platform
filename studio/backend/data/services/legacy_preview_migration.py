"""Explicit, transactional migration from legacy child preview jobs to batches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RETRY_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    Episode,
    JobQueueSlot,
    JobRun,
    Workspace,
)
from data.realtime.outbox import enqueue_work_queue_invalidated
from data.services.derived_preview_batch import (
    DERIVED_PREVIEW_BATCH_JOB_KIND,
    DERIVED_PREVIEW_BATCH_QUEUE,
    _batch_job_scope,
    _has_trusted_published_child_preview,
    _require_accepted_cut_review,
    _resolve_batch_children,
    _resolve_published_parent_preview,
    _source_review_scope,
    _timestamps_for_child,
    ensure_derived_preview_batch_job,
)
from data.services.job_runs import database_now

_LEGACY_JOB_KIND = "episode_preview"
_ACTIVE_LEGACY_STATUSES = frozenset(
    {JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING, JOB_STATUS_RUNNING}
)
_TERMINAL_LEGACY_STATUSES = frozenset(
    {JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}
)


class LegacyPreviewMigrationError(RuntimeError):
    """Raised when an explicit migration target cannot be migrated safely."""


@dataclass(frozen=True, order=True)
class LegacyPreviewMigrationTarget:
    source_episode_id: int
    cut_review_work_item_id: int


@dataclass(frozen=True)
class LegacyPreviewMigrationResult:
    source_episode_id: int
    cut_review_work_item_id: int
    batch_job_id: str | None
    child_count: int
    published_child_count: int
    active_legacy_count: int
    cancelled_legacy_count: int
    terminal_legacy_count: int
    active_running_lease_count: int


@dataclass(frozen=True)
class _MigrationPlan:
    target: LegacyPreviewMigrationTarget
    source: Episode
    children: tuple[Episode, ...]
    existing_batch_job_id: str | None
    nonterminal_legacy_jobs: tuple[JobRun, ...]
    terminal_legacy_count: int
    published_child_count: int
    active_running_lease_count: int
    queue_slots: tuple[JobQueueSlot, ...]

    def result(
        self,
        *,
        batch_job_id: str | None,
        cancelled_legacy_count: int,
    ) -> LegacyPreviewMigrationResult:
        return LegacyPreviewMigrationResult(
            source_episode_id=self.target.source_episode_id,
            cut_review_work_item_id=self.target.cut_review_work_item_id,
            batch_job_id=batch_job_id,
            child_count=len(self.children),
            published_child_count=self.published_child_count,
            active_legacy_count=len(self.nonterminal_legacy_jobs),
            cancelled_legacy_count=cancelled_legacy_count,
            terminal_legacy_count=self.terminal_legacy_count,
            active_running_lease_count=self.active_running_lease_count,
        )


def migrate_legacy_preview_jobs(
    db: Session,
    *,
    targets: tuple[LegacyPreviewMigrationTarget, ...],
    apply: bool,
) -> tuple[LegacyPreviewMigrationResult, ...]:
    """Check or atomically migrate only the explicitly supplied source/review pairs."""
    try:
        normalized = _normalize_targets(targets)
        now = database_now(db)
        plans = tuple(
            _inspect_target(db, target=target, lock=apply, now=now) for target in normalized
        )
        if not apply:
            results = tuple(
                plan.result(
                    batch_job_id=plan.existing_batch_job_id,
                    cancelled_legacy_count=0,
                )
                for plan in plans
            )
            db.rollback()
            return results
        if any(plan.active_running_lease_count for plan in plans):
            raise LegacyPreviewMigrationError("legacy preview migration has active worker leases")

        invalidated_workspace_ids: set[int] = set()
        results: list[LegacyPreviewMigrationResult] = []
        for plan in plans:
            batch = ensure_derived_preview_batch_job(
                db,
                source=plan.source,
                review_item_id=plan.target.cut_review_work_item_id,
                actor_id=None,
            )
            for job in plan.nonterminal_legacy_jobs:
                _mark_migrated(job, batch_job_id=batch.id, now=now)
            for slot in plan.queue_slots:
                _clear_slot(slot, now=now)
            if plan.existing_batch_job_id is None or plan.nonterminal_legacy_jobs:
                invalidated_workspace_ids.add(plan.source.workspace_id)
            results.append(
                plan.result(
                    batch_job_id=batch.id,
                    cancelled_legacy_count=len(plan.nonterminal_legacy_jobs),
                )
            )

        for workspace_id in sorted(invalidated_workspace_ids):
            workspace = db.scalar(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
            if workspace is None:
                raise LegacyPreviewMigrationError(
                    f"legacy preview migration workspace is unavailable: {workspace_id}"
                )
            enqueue_work_queue_invalidated(db, workspace=workspace)
        db.commit()
        return tuple(results)
    except LegacyPreviewMigrationError:
        db.rollback()
        raise
    except ValueError as exc:
        db.rollback()
        raise LegacyPreviewMigrationError("legacy preview migration contract is invalid") from exc
    except Exception:
        db.rollback()
        raise


def _normalize_targets(
    targets: tuple[LegacyPreviewMigrationTarget, ...],
) -> tuple[LegacyPreviewMigrationTarget, ...]:
    if not targets:
        raise LegacyPreviewMigrationError("legacy preview migration requires explicit targets")
    normalized: list[LegacyPreviewMigrationTarget] = []
    seen: set[LegacyPreviewMigrationTarget] = set()
    for target in targets:
        if (
            not isinstance(target, LegacyPreviewMigrationTarget)
            or type(target.source_episode_id) is not int
            or target.source_episode_id <= 0
            or type(target.cut_review_work_item_id) is not int
            or target.cut_review_work_item_id <= 0
        ):
            raise LegacyPreviewMigrationError("legacy preview migration target is invalid")
        if target in seen:
            raise LegacyPreviewMigrationError("legacy preview migration target is duplicated")
        seen.add(target)
        normalized.append(target)
    return tuple(sorted(normalized))


def _inspect_target(
    db: Session,
    *,
    target: LegacyPreviewMigrationTarget,
    lock: bool,
    now: datetime,
) -> _MigrationPlan:
    source_statement = select(Episode).where(Episode.id == target.source_episode_id)
    if lock:
        source_statement = source_statement.with_for_update()
    source = db.scalar(source_statement)
    if source is None:
        raise LegacyPreviewMigrationError(
            f"legacy preview migration source is unavailable: {target.source_episode_id}"
        )
    try:
        expected_scope = _source_review_scope(source, target.cut_review_work_item_id)
        _require_accepted_cut_review(
            db,
            source=source,
            review_id=target.cut_review_work_item_id,
        )
        children = _resolve_batch_children(
            db,
            source=source,
            review_id=target.cut_review_work_item_id,
        )
        parent = _resolve_published_parent_preview(db, source=source)
    except ValueError as exc:
        raise LegacyPreviewMigrationError(
            "legacy preview migration target contract is invalid: "
            f"{target.source_episode_id}:{target.cut_review_work_item_id}"
        ) from exc

    published_child_count = 0
    for child in children:
        timestamps = _timestamps_for_child(parent.frame_timestamps_ns, child=child)
        if not timestamps:
            raise LegacyPreviewMigrationError(
                "legacy preview migration child timeline is invalid: "
                f"{target.source_episode_id}:{target.cut_review_work_item_id}"
            )
        if _has_trusted_published_child_preview(
            db,
            child=child,
            parent=parent,
            expected_timestamps=timestamps,
        ):
            published_child_count += 1

    existing_batch = _existing_batch_job(
        db,
        target=target,
        expected_scope=expected_scope,
        lock=lock,
    )
    child_ids = {child.id for child in children}
    legacy_statement = (
        select(JobRun)
        .where(
            JobRun.kind == _LEGACY_JOB_KIND,
            JobRun.resource_type == "episode",
            JobRun.resource_id.in_([str(child_id) for child_id in sorted(child_ids)]),
        )
        .order_by(JobRun.created_at, JobRun.id)
    )
    if lock:
        legacy_statement = legacy_statement.with_for_update()
    legacy_jobs = tuple(db.scalars(legacy_statement).all())
    nonterminal: list[JobRun] = []
    terminal_count = 0
    active_running_lease_count = 0
    for job in legacy_jobs:
        if job.workspace_id != source.workspace_id or job.task_set_id != source.task_set_id:
            raise LegacyPreviewMigrationError(
                "legacy preview migration job scope is invalid: "
                f"{target.source_episode_id}:{target.cut_review_work_item_id}"
            )
        if job.status in _ACTIVE_LEGACY_STATUSES:
            nonterminal.append(job)
            if (
                job.status == JOB_STATUS_RUNNING
                and job.lease_expires_at is not None
                and job.lease_expires_at > now
            ):
                active_running_lease_count += 1
        elif job.status in _TERMINAL_LEGACY_STATUSES:
            terminal_count += 1
        else:
            raise LegacyPreviewMigrationError(
                "legacy preview migration job status is invalid: "
                f"{target.source_episode_id}:{target.cut_review_work_item_id}"
            )

    legacy_ids = [job.id for job in legacy_jobs]
    slot_statement = select(JobQueueSlot).where(JobQueueSlot.job_id.in_(legacy_ids))
    if lock:
        slot_statement = slot_statement.with_for_update()
    queue_slots = tuple(db.scalars(slot_statement).all()) if legacy_ids else ()
    return _MigrationPlan(
        target=target,
        source=source,
        children=children,
        existing_batch_job_id=existing_batch.id if existing_batch is not None else None,
        nonterminal_legacy_jobs=tuple(nonterminal),
        terminal_legacy_count=terminal_count,
        published_child_count=published_child_count,
        active_running_lease_count=active_running_lease_count,
        queue_slots=queue_slots,
    )


def _existing_batch_job(
    db: Session,
    *,
    target: LegacyPreviewMigrationTarget,
    expected_scope: tuple[int, int, int, int],
    lock: bool,
) -> JobRun | None:
    key = (
        f"derived-preview-batch:{target.source_episode_id}:"
        f"review:{target.cut_review_work_item_id}:v1"
    )
    statement = select(JobRun).where(JobRun.idempotency_key == key)
    if lock:
        statement = statement.with_for_update()
    existing = db.scalar(statement)
    if existing is not None and not _batch_contract_matches(
        existing, expected_scope=expected_scope
    ):
        raise LegacyPreviewMigrationError(
            "legacy preview migration batch contract conflicts: "
            f"{target.source_episode_id}:{target.cut_review_work_item_id}"
        )

    batch_statement = select(JobRun).where(
        JobRun.kind == DERIVED_PREVIEW_BATCH_JOB_KIND,
        JobRun.resource_type == "episode",
        JobRun.resource_id == str(target.source_episode_id),
    )
    if lock:
        batch_statement = batch_statement.with_for_update()
    for batch in db.scalars(batch_statement):
        if _batch_job_scope(batch) == expected_scope and (
            existing is None or batch.id != existing.id
        ):
            raise LegacyPreviewMigrationError(
                "legacy preview migration has a conflicting batch job: "
                f"{target.source_episode_id}:{target.cut_review_work_item_id}"
            )
    return existing


def _batch_contract_matches(
    job: JobRun,
    *,
    expected_scope: tuple[int, int, int, int],
) -> bool:
    return (
        job.kind == DERIVED_PREVIEW_BATCH_JOB_KIND
        and job.resource_type == "episode"
        and job.queue == DERIVED_PREVIEW_BATCH_QUEUE
        and _batch_job_scope(job) == expected_scope
    )


def _mark_migrated(job: JobRun, *, batch_job_id: str, now: datetime) -> None:
    job.status = JOB_STATUS_CANCELLED
    job.phase = JOB_STATUS_CANCELLED
    job.error_code = "migrated_to_batch"
    job.error_message = f"migrated_to_derived_preview_batch:{batch_job_id}"
    job.lease_worker_id = ""
    job.lease_token = ""
    job.lease_expires_at = None
    job.recovery_dispatch_token = ""
    job.recovery_dispatch_expires_at = None
    job.recovery_dispatched_at = None
    job.finished_at = now
    job.updated_at = now


def _clear_slot(slot: JobQueueSlot, *, now: datetime) -> None:
    slot.job_id = None
    slot.worker_id = ""
    slot.lease_expires_at = None
    slot.updated_at = now
