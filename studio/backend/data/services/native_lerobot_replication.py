"""Guarded platform-owned replication for native LeRobot datasets.

The source URI recorded on ``NativeLerobotDataset`` is audit-only.  A worker
must re-authorize the persisted scope and marker before copying any object, and
may only write the exact objects named by that marker into the deterministic
platform destination.  It never enumerates or exposes the source prefix.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import Batch, ExternalOssImportScope, JobRun, NativeLerobotDataset, User
from data.infra import oss_client
from data.infra.oss_client import OSSObjectInfo
from data.services.job_runs import (
    MAX_RETRIES,
    ManualRetryResult,
    NonRetryableJobError,
    create_or_get_job_in_transaction,
    database_now,
    retry_terminal_job_attempt_in_transaction,
)
from data.services.native_lerobot_datasets import (
    COMPLETE_MARKER_NAME,
    LEROBOT_ROOT_PREFIX,
    MARKER_MAX_BYTES,
    NativeLerobotCandidateTokenError,
    NativeLerobotMarker,
    NativeLerobotMarkerError,
    _decode_candidate_token,
    _scope_allows_key,
    _scope_allows_prefix,
    platform_native_lerobot_uri,
    read_native_lerobot_marker,
)

NATIVE_LEROBOT_COPY_KIND = "native_lerobot_copy"
NATIVE_LEROBOT_COPY_QUEUE = "export"
_COPY_ORIGIN_HEADER = "x-oss-meta-quicdata-copy-origin"

_COPY_ERROR_MESSAGES = {
    "copy_access_denied": "The platform copy is not authorized to access the selected source or destination.",
    "copy_state_invalid": "The platform copy record is not in a valid state.",
    "copy_unavailable": "The platform copy could not be completed.",
    "source_changed": "The selected source changed before the platform copy completed.",
    "source_identity_unavailable": "The selected source cannot be copied safely because its identity is unavailable.",
    "source_scope_changed": "The source authorization changed before the platform copy started.",
    "target_conflict": "The platform destination already contains conflicting data.",
}


class NativeLerobotCopyFailure(NonRetryableJobError):
    """A stable, non-sensitive native copy failure code."""

    def __init__(self, code: str):
        if code not in _COPY_ERROR_MESSAGES:
            code = "copy_state_invalid"
        self.code = code
        super().__init__(code)


class NativeLerobotCopyTransientError(RuntimeError):
    """A temporary provider failure that may use the durable retry budget."""


class NativeLerobotCopyRetryError(ValueError):
    """A safe conflict returned when a native copy cannot be manually retried."""


class NativeLerobotSourceReauthorizationError(ValueError):
    """A safe conflict returned when a candidate cannot rebind a source scope."""


class NativeLerobotBackfillError(ValueError):
    """A safe operator error for the explicit historical backfill command."""


@dataclass(frozen=True)
class NativeLerobotBackfillPage:
    """One bounded native backfill page, with no source URI in its projection."""

    scanned_count: int
    queued_jobs: tuple[JobRun, ...]
    failed_count: int
    next_after_native_id: int | None


def run_native_lerobot_copy(db: Session, job: JobRun) -> dict[str, int]:
    """Copy one marker-authorized native LeRobot dataset into the platform bucket.

    The caller owns the JobRun lease.  This function intentionally leaves the
    transaction open so ``complete_job`` or ``fail_job`` can fence the resource
    state and the JobRun transition together.
    """
    if job.kind != NATIVE_LEROBOT_COPY_KIND or job.resource_type != "native_lerobot_dataset":
        raise NativeLerobotCopyFailure("copy_state_invalid")
    try:
        dataset_id = int(job.resource_id)
    except (TypeError, ValueError) as exc:
        raise NativeLerobotCopyFailure("copy_state_invalid") from exc
    if dataset_id <= 0:
        raise NativeLerobotCopyFailure("copy_state_invalid")

    row = db.scalar(
        select(NativeLerobotDataset).where(NativeLerobotDataset.id == dataset_id).with_for_update()
    )
    if row is None:
        raise NativeLerobotCopyFailure("copy_state_invalid")
    if row.last_copy_job_id != job.id:
        # A manual retry or reauthorization already superseded this attempt.
        raise NativeLerobotCopyFailure("copy_state_invalid")
    if row.status != "active" or row.copy_status not in {"queued", "running", "failed"}:
        _fail_copy(db, row, "copy_state_invalid")

    scope = None
    if row.source_scope_id is not None:
        scope = db.scalar(
            select(ExternalOssImportScope)
            .where(ExternalOssImportScope.id == row.source_scope_id)
            .with_for_update()
        )
    if scope is None:
        _fail_copy(db, row, "source_scope_changed")

    source_root, marker_key, target_bucket, target_prefix = _validate_copy_locations(
        db,
        row=row,
        scope=scope,
    )
    batch = db.get(Batch, row.batch_id)
    if batch is None or batch.batch_type != "lerobot" or batch.workspace_id != row.workspace_id:
        _fail_copy(db, row, "copy_state_invalid")

    now = database_now(db)
    row.copy_status = "running"
    row.copy_started_at = now
    row.copy_finished_at = None
    row.copy_error_code = ""
    row.copy_error_message = ""
    from data.services.native_lerobot_import_sessions import refresh_native_lerobot_batch_status

    refresh_native_lerobot_batch_status(db, row.batch_id)
    db.flush()

    try:
        marker = _read_current_marker(
            db,
            row=row,
            scope=scope,
            marker_key=marker_key,
            source_root=source_root,
        )
        copied_files = 0
        reused_files = 0
        verified_bytes = 0
        for item in marker.objects:
            source_key = f"{source_root}{item.path}"
            target_key = f"{target_prefix}{item.path}"
            source_info = _source_info_or_fail(
                db,
                row=row,
                bucket=scope.bucket,
                key=source_key,
                expected_size=item.size,
                expected_sha256=item.sha256,
            )
            copied = _copy_exact_object(
                db,
                row=row,
                source_bucket=scope.bucket,
                source_key=source_key,
                source_info=source_info,
                target_bucket=target_bucket,
                target_key=target_key,
                expected_sha256=item.sha256,
            )
            copied_files += int(copied)
            reused_files += int(not copied)
            verified_bytes += item.size

        # Re-read the source authority after payloads.  A changed marker must
        # never be published at the platform target after an earlier snapshot.
        marker = _read_current_marker(
            db,
            row=row,
            scope=scope,
            marker_key=marker_key,
            source_root=source_root,
        )
        marker_info = _source_info_or_fail(
            db,
            row=row,
            bucket=scope.bucket,
            key=marker.marker_key,
            expected_size=None,
            expected_sha256=marker.marker_sha256,
        )
        if marker_info.size > MARKER_MAX_BYTES:
            _fail_copy(db, row, "source_changed")
        _copy_exact_object(
            db,
            row=row,
            source_bucket=scope.bucket,
            source_key=marker.marker_key,
            source_info=marker_info,
            target_bucket=target_bucket,
            target_key=f"{target_prefix}{COMPLETE_MARKER_NAME}",
            expected_sha256=marker.marker_sha256,
        )
    except NativeLerobotCopyFailure:
        raise
    except NativeLerobotCopyTransientError:
        _mark_transient_copy_failure(db, row=row, job=job)
        raise
    except Exception as exc:
        _mark_transient_copy_failure(db, row=row, job=job)
        raise NativeLerobotCopyTransientError("platform copy is temporarily unavailable") from exc

    _finish_copy_success(
        db,
        row=row,
        job=job,
        verified_files=marker.file_count,
        verified_bytes=verified_bytes,
    )
    return {
        "copied_files": marker.file_count,
        "newly_copied_files": copied_files,
        "reused_files": reused_files,
        "verified_files": marker.file_count,
        "verified_bytes": verified_bytes,
        "file_count": marker.file_count,
        "total_size": marker.total_size,
    }


def retry_native_lerobot_copy(
    db: Session,
    *,
    dataset_id: int,
    actor_id: int | None,
) -> ManualRetryResult:
    """Queue one manual retry while atomically updating the native projection."""
    row = _locked_native_dataset(db, dataset_id)
    _require_native_actor(db, actor_id)
    if row.status != "active" or row.copy_status != "failed":
        raise NativeLerobotCopyRetryError("native copy is not eligible for retry")
    if row.copy_error_code == "source_scope_changed":
        raise NativeLerobotCopyRetryError("source reauthorization is required before retry")
    scope = _locked_current_scope_for_retry(db, row)
    _validate_current_scope_for_retry(row, scope)
    if not row.last_copy_job_id:
        raise NativeLerobotCopyRetryError("native copy has no retryable job")
    result = retry_terminal_job_attempt_in_transaction(db, row.last_copy_job_id, actor_id=actor_id)
    _validate_copy_job_scope(row, result.job)
    if result.created:
        _queue_copy_projection(db, row=row, job=result.job)
    return result


def reauthorize_native_lerobot_source(
    db: Session,
    *,
    dataset_id: int,
    candidate_token: str,
    actor_id: int | None,
) -> ManualRetryResult:
    """Rebind a failed snapshot to the same currently authorized source only."""
    try:
        payload = _decode_candidate_token(candidate_token, actor_id=actor_id)
    except NativeLerobotCandidateTokenError as exc:
        raise NativeLerobotSourceReauthorizationError("candidate token is invalid") from exc

    row = _locked_native_dataset(db, dataset_id)
    _require_native_actor(db, actor_id)
    if (
        row.status != "active"
        or (row.copy_status == "failed" and row.copy_error_code != "source_scope_changed")
        or row.copy_status not in {"failed", "backfill_pending"}
    ):
        raise NativeLerobotSourceReauthorizationError(
            "native copy is not eligible for source reauthorization"
        )
    if payload["workspace_id"] != row.workspace_id or payload["task_set_id"] != row.task_set_id:
        raise NativeLerobotSourceReauthorizationError(
            "candidate does not belong to this dataset scope"
        )

    scope = db.scalar(
        select(ExternalOssImportScope)
        .where(ExternalOssImportScope.id == payload["scope_id"])
        .with_for_update()
    )
    if (
        scope is None
        or not scope.is_enabled
        or scope.workspace_id != row.workspace_id
        or scope.task_set_id != row.task_set_id
        or scope.revision != payload["scope_revision"]
        or scope.bucket != payload["bucket"]
        or not _scope_allows_key(scope, str(payload["marker_key"]))
        or not _scope_allows_prefix(
            scope,
            f"{LEROBOT_ROOT_PREFIX}/{row.robot_type}/{row.dataset_id}",
        )
    ):
        raise NativeLerobotSourceReauthorizationError("candidate scope is unavailable")
    try:
        marker = read_native_lerobot_marker(
            bucket=scope.bucket, marker_key=str(payload["marker_key"])
        )
    except NativeLerobotMarkerError as exc:
        raise NativeLerobotSourceReauthorizationError("candidate source is unavailable") from exc
    if (
        not hmac.compare_digest(marker.marker_sha256, str(payload["marker_sha256"]))
        or marker.oss_uri != row.source_oss_uri
        or marker.marker_sha256 != row.marker_sha256
        or marker.manifest_sha256 != row.manifest_sha256
        or marker.robot_type != row.robot_type
        or marker.dataset_id != row.dataset_id
        or marker.file_count != row.file_count
        or marker.total_size != row.total_size
    ):
        raise NativeLerobotSourceReauthorizationError(
            "candidate must match the existing source snapshot"
        )

    idempotency_key = (
        f"native-copy-reauthorize:{row.id}:{row.marker_sha256}:{scope.id}:{scope.revision}"
    )
    existing = db.scalar(
        select(JobRun).where(JobRun.idempotency_key == idempotency_key).with_for_update()
    )
    if existing is None:
        job = create_or_get_job_in_transaction(
            db,
            kind=NATIVE_LEROBOT_COPY_KIND,
            resource_type="native_lerobot_dataset",
            resource_id=row.id,
            idempotency_key=idempotency_key,
            queue=NATIVE_LEROBOT_COPY_QUEUE,
            actor_id=actor_id,
            workspace_id=row.workspace_id,
            task_set_id=row.task_set_id,
            detail={
                "marker_sha256": row.marker_sha256,
                "file_count": row.file_count,
                "total_size": row.total_size,
                "reauthorized_scope_id": scope.id,
                "reauthorized_scope_revision": scope.revision,
            },
        )
        result = ManualRetryResult(job=job, created=True)
    elif existing.status in {"failed", "cancelled"}:
        result = retry_terminal_job_attempt_in_transaction(db, existing.id, actor_id=actor_id)
    else:
        result = ManualRetryResult(job=existing, created=False)

    _validate_copy_job_scope(row, result.job)
    row.source_scope_id = scope.id
    row.source_scope_revision = scope.revision
    if result.created:
        _queue_copy_projection(db, row=row, job=result.job)
    return result


def backfill_native_lerobot_page(
    db: Session,
    *,
    scope_id: int | None,
    limit: int,
    after_native_id: int | None,
    apply: bool,
) -> NativeLerobotBackfillPage:
    """Inspect or queue one bounded page of migration-era native rows.

    The caller chooses one currently authorized scope.  A check never mutates
    rows or reads external marker objects.  An apply locks that scope and each
    selected row, revalidates its exact marker, then binds the immutable scope
    revision and queues the normal platform-copy worker.
    """
    _validate_backfill_arguments(scope_id=scope_id, limit=limit, after_native_id=after_native_id)
    scope_query = select(ExternalOssImportScope).where(ExternalOssImportScope.id == scope_id)
    if apply:
        scope_query = scope_query.with_for_update()
    scope = db.scalar(scope_query)
    if scope is None or not scope.is_enabled:
        raise NativeLerobotBackfillError("source scope is unavailable")

    rows_query = (
        select(NativeLerobotDataset)
        .where(
            NativeLerobotDataset.workspace_id == scope.workspace_id,
            NativeLerobotDataset.task_set_id == scope.task_set_id,
            NativeLerobotDataset.copy_status == "backfill_pending",
        )
        .order_by(NativeLerobotDataset.id.asc())
        .limit(limit)
    )
    if after_native_id is not None:
        rows_query = rows_query.where(NativeLerobotDataset.id > after_native_id)
    if apply:
        rows_query = rows_query.with_for_update(skip_locked=True)
    rows = tuple(db.scalars(rows_query))
    next_after_native_id = rows[-1].id if len(rows) == limit else None
    if not apply:
        return NativeLerobotBackfillPage(
            scanned_count=len(rows),
            queued_jobs=(),
            failed_count=0,
            next_after_native_id=next_after_native_id,
        )

    queued_jobs: list[JobRun] = []
    failed_count = 0
    for row in rows:
        outcome = _backfill_native_lerobot_row(db, row=row, scope=scope)
        if outcome == "failed":
            failed_count += 1
        elif isinstance(outcome, JobRun):
            queued_jobs.append(outcome)
    db.flush()
    return NativeLerobotBackfillPage(
        scanned_count=len(rows),
        queued_jobs=tuple(queued_jobs),
        failed_count=failed_count,
        next_after_native_id=next_after_native_id,
    )


def _validate_backfill_arguments(
    *,
    scope_id: int | None,
    limit: int,
    after_native_id: int | None,
) -> None:
    if isinstance(scope_id, bool) or not isinstance(scope_id, int) or scope_id <= 0:
        raise NativeLerobotBackfillError("scope id is required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise NativeLerobotBackfillError("backfill limit must be between 1 and 1000")
    if after_native_id is not None and (
        isinstance(after_native_id, bool)
        or not isinstance(after_native_id, int)
        or after_native_id < 0
    ):
        raise NativeLerobotBackfillError("backfill cursor is invalid")


def _backfill_native_lerobot_row(
    db: Session,
    *,
    row: NativeLerobotDataset,
    scope: ExternalOssImportScope,
) -> JobRun | str | None:
    source_root = f"{LEROBOT_ROOT_PREFIX}/{row.robot_type}/{row.dataset_id}/"
    marker_key = f"{source_root}{COMPLETE_MARKER_NAME}"
    expected_source_uri = f"oss://{scope.bucket}/{source_root}"
    if row.source_oss_uri != expected_source_uri or not _scope_allows_prefix(scope, source_root):
        # A manually selected scope cannot be guessed or repointed.  Leave the
        # record pending for an operator to explicitly choose the right scope.
        return None
    try:
        expected_target_uri = platform_native_lerobot_uri(
            workspace_id=row.workspace_id,
            task_set_id=row.task_set_id,
            batch_id=row.batch_id,
            native_dataset_id=row.id if row.import_session_id is not None else None,
            robot_type=row.robot_type,
            dataset_id=row.dataset_id,
        )
    except NativeLerobotMarkerError:
        _set_copy_failure(db, row, "copy_state_invalid")
        return "failed"
    if row.oss_uri != expected_target_uri:
        _set_copy_failure(db, row, "target_conflict")
        return "failed"
    try:
        marker = read_native_lerobot_marker(bucket=scope.bucket, marker_key=marker_key)
    except Exception:
        _set_copy_failure(db, row, "source_changed")
        return "failed"
    if (
        marker.bucket != scope.bucket
        or marker.marker_key != marker_key
        or marker.oss_uri != expected_source_uri
        or marker.robot_type != row.robot_type
        or marker.dataset_id != row.dataset_id
        or marker.file_count != row.file_count
        or marker.total_size != row.total_size
        or marker.manifest_sha256 != row.manifest_sha256
        or not hmac.compare_digest(marker.marker_sha256, row.marker_sha256)
    ):
        _set_copy_failure(db, row, "source_changed")
        return "failed"

    idempotency_key = (
        f"native-copy-backfill:{row.id}:{row.marker_sha256}:{scope.id}:{scope.revision}"
    )
    existing = db.scalar(
        select(JobRun).where(JobRun.idempotency_key == idempotency_key).with_for_update()
    )
    if existing is None:
        job = create_or_get_job_in_transaction(
            db,
            kind=NATIVE_LEROBOT_COPY_KIND,
            resource_type="native_lerobot_dataset",
            resource_id=row.id,
            idempotency_key=idempotency_key,
            queue=NATIVE_LEROBOT_COPY_QUEUE,
            actor_id=None,
            workspace_id=row.workspace_id,
            task_set_id=row.task_set_id,
            detail={
                "marker_sha256": row.marker_sha256,
                "file_count": row.file_count,
                "total_size": row.total_size,
                "backfill_scope_id": scope.id,
                "backfill_scope_revision": scope.revision,
            },
        )
    elif existing.status in {"queued", "retry_pending"}:
        job = existing
    elif existing.status in {"failed", "cancelled"}:
        job = retry_terminal_job_attempt_in_transaction(db, existing.id, actor_id=None).job
    else:
        _set_copy_failure(db, row, "copy_state_invalid")
        return "failed"

    row.source_scope_id = scope.id
    row.source_scope_revision = scope.revision
    try:
        _validate_copy_job_scope(row, job)
        _queue_copy_projection(db, row=row, job=job)
    except NativeLerobotCopyRetryError:
        _set_copy_failure(db, row, "copy_state_invalid")
        return "failed"
    return job


def _locked_native_dataset(db: Session, dataset_id: int) -> NativeLerobotDataset:
    if isinstance(dataset_id, bool) or not isinstance(dataset_id, int) or dataset_id <= 0:
        raise NativeLerobotCopyRetryError("native dataset id is invalid")
    row = db.scalar(
        select(NativeLerobotDataset).where(NativeLerobotDataset.id == dataset_id).with_for_update()
    )
    if row is None:
        raise KeyError(f"native dataset not found: {dataset_id}")
    return row


def _require_native_actor(db: Session, actor_id: int | None) -> User:
    actor = db.get(User, actor_id) if actor_id is not None else None
    if actor is None or not actor.is_active:
        raise PermissionError("native copy actor is unavailable")
    return actor


def _locked_current_scope_for_retry(
    db: Session,
    row: NativeLerobotDataset,
) -> ExternalOssImportScope:
    if row.source_scope_id is None:
        raise NativeLerobotCopyRetryError("source reauthorization is required before retry")
    scope = db.scalar(
        select(ExternalOssImportScope)
        .where(ExternalOssImportScope.id == row.source_scope_id)
        .with_for_update()
    )
    if scope is None:
        raise NativeLerobotCopyRetryError("source reauthorization is required before retry")
    return scope


def _validate_current_scope_for_retry(
    row: NativeLerobotDataset,
    scope: ExternalOssImportScope,
) -> None:
    source_root = f"{LEROBOT_ROOT_PREFIX}/{row.robot_type}/{row.dataset_id}/"
    if (
        not scope.is_enabled
        or scope.workspace_id != row.workspace_id
        or scope.task_set_id != row.task_set_id
        or scope.revision != row.source_scope_revision
        or row.source_oss_uri != f"oss://{scope.bucket}/{source_root}"
        or not _scope_allows_prefix(scope, source_root)
    ):
        raise NativeLerobotCopyRetryError("source reauthorization is required before retry")


def _validate_copy_job_scope(row: NativeLerobotDataset, job: JobRun) -> None:
    if (
        job.kind != NATIVE_LEROBOT_COPY_KIND
        or job.resource_type != "native_lerobot_dataset"
        or job.resource_id != str(row.id)
        or job.workspace_id != row.workspace_id
        or job.task_set_id != row.task_set_id
        or job.queue != NATIVE_LEROBOT_COPY_QUEUE
    ):
        raise NativeLerobotCopyRetryError("native copy job scope is invalid")


def _queue_copy_projection(db: Session, *, row: NativeLerobotDataset, job: JobRun) -> None:
    batch = db.get(Batch, row.batch_id)
    if batch is None or batch.batch_type != "lerobot":
        raise NativeLerobotCopyRetryError("native copy batch is unavailable")
    row.last_copy_job_id = job.id
    row.copy_status = "queued"
    row.copy_started_at = None
    row.copy_finished_at = None
    row.copy_error_code = ""
    row.copy_error_message = ""
    from data.services.native_lerobot_import_sessions import refresh_native_lerobot_batch_status

    refresh_native_lerobot_batch_status(db, batch.id)
    db.flush()


def _validate_copy_locations(
    db: Session,
    *,
    row: NativeLerobotDataset,
    scope: ExternalOssImportScope,
) -> tuple[str, str, str, str]:
    source_root = f"{LEROBOT_ROOT_PREFIX}/{row.robot_type}/{row.dataset_id}/"
    marker_key = f"{source_root}{COMPLETE_MARKER_NAME}"
    expected_source_uri = f"oss://{scope.bucket}/{source_root}"
    if (
        not scope.is_enabled
        or scope.workspace_id != row.workspace_id
        or scope.task_set_id != row.task_set_id
        or row.source_scope_revision != scope.revision
        or row.source_oss_uri != expected_source_uri
        or not _scope_allows_prefix(scope, source_root)
    ):
        _fail_copy(db, row, "source_scope_changed")

    try:
        expected_target_uri = platform_native_lerobot_uri(
            workspace_id=row.workspace_id,
            task_set_id=row.task_set_id,
            batch_id=row.batch_id,
            native_dataset_id=row.id if row.import_session_id is not None else None,
            robot_type=row.robot_type,
            dataset_id=row.dataset_id,
        )
    except NativeLerobotMarkerError:
        _fail_copy(db, row, "copy_state_invalid")
    if row.oss_uri != expected_target_uri:
        _fail_copy(db, row, "target_conflict")
    target_bucket = oss_client.bucket_name("export")
    target_prefix = expected_target_uri.removeprefix(f"oss://{target_bucket}/")
    if not target_prefix or target_prefix == expected_target_uri or not target_prefix.endswith("/"):
        _fail_copy(db, row, "copy_state_invalid")
    if scope.bucket == target_bucket and _prefixes_overlap(source_root, target_prefix):
        _fail_copy(db, row, "target_conflict")
    return source_root, marker_key, target_bucket, target_prefix


def _read_current_marker(
    db: Session,
    *,
    row: NativeLerobotDataset,
    scope: ExternalOssImportScope,
    marker_key: str,
    source_root: str,
) -> NativeLerobotMarker:
    try:
        marker = read_native_lerobot_marker(bucket=scope.bucket, marker_key=marker_key)
    except NativeLerobotMarkerError as exc:
        if exc.code == "marker_unavailable":
            raise NativeLerobotCopyTransientError(
                "platform copy source is temporarily unavailable"
            ) from exc
        _fail_copy(db, row, "source_changed")
    except Exception as exc:
        if _is_access_denied(exc):
            _fail_copy(db, row, "copy_access_denied")
        raise NativeLerobotCopyTransientError(
            "platform copy source is temporarily unavailable"
        ) from exc

    if (
        marker.bucket != scope.bucket
        or marker.marker_key != marker_key
        or marker.oss_uri != f"oss://{scope.bucket}/{source_root}"
        or marker.robot_type != row.robot_type
        or marker.dataset_id != row.dataset_id
        or marker.file_count != row.file_count
        or marker.total_size != row.total_size
        or marker.manifest_sha256 != row.manifest_sha256
        or marker.marker_sha256 != row.marker_sha256
    ):
        _fail_copy(db, row, "source_changed")
    return marker


def _source_info_or_fail(
    db: Session,
    *,
    row: NativeLerobotDataset,
    bucket: str,
    key: str,
    expected_size: int | None,
    expected_sha256: str,
) -> OSSObjectInfo:
    try:
        info = oss_client.object_info(bucket, key)
    except Exception as exc:
        if _is_access_denied(exc):
            _fail_copy(db, row, "copy_access_denied")
        raise NativeLerobotCopyTransientError(
            "platform copy source is temporarily unavailable"
        ) from exc
    if (
        info is None
        or (expected_size is not None and info.size != expected_size)
        or info.metadata.get("x-oss-meta-sha256") != expected_sha256
    ):
        _fail_copy(db, row, "source_changed")
    if not str(info.etag or "").strip():
        _fail_copy(db, row, "source_identity_unavailable")
    return info


def _copy_exact_object(
    db: Session,
    *,
    row: NativeLerobotDataset,
    source_bucket: str,
    source_key: str,
    source_info: OSSObjectInfo,
    target_bucket: str,
    target_key: str,
    expected_sha256: str,
) -> bool:
    existing = _target_info_or_fail(db, row=row, bucket=target_bucket, key=target_key)
    if existing is not None:
        if _target_matches(existing, size=source_info.size, sha256=expected_sha256):
            return False
        _fail_copy(db, row, "target_conflict")

    try:
        if oss_client.requires_multipart_copy(source_info.size):
            oss_client.multipart_copy_immutable_object(
                source_bucket,
                source_key,
                target_bucket,
                target_key,
                source_size=source_info.size,
                source_etag=source_info.etag,
                source_version_id=source_info.version_id,
                copy_origin=expected_sha256,
            )
        else:
            oss_client.copy_object(
                source_bucket,
                source_key,
                target_bucket,
                target_key,
                source_etag=source_info.etag,
                source_version_id=source_info.version_id,
                forbid_overwrite=True,
                metadata={
                    "x-oss-meta-sha256": expected_sha256,
                    _COPY_ORIGIN_HEADER: expected_sha256,
                },
            )
    except FileExistsError:
        existing = _target_info_or_fail(db, row=row, bucket=target_bucket, key=target_key)
        if existing is not None and _target_matches(
            existing, size=source_info.size, sha256=expected_sha256
        ):
            return False
        _fail_copy(db, row, "target_conflict")
    except Exception as exc:
        if _is_access_denied(exc):
            _fail_copy(db, row, "copy_access_denied")
        raise NativeLerobotCopyTransientError("platform copy is temporarily unavailable") from exc

    copied = _target_info_or_fail(db, row=row, bucket=target_bucket, key=target_key)
    if copied is None or not _target_matches(copied, size=source_info.size, sha256=expected_sha256):
        _fail_copy(db, row, "target_conflict")
    return True


def _target_info_or_fail(
    db: Session,
    *,
    row: NativeLerobotDataset,
    bucket: str,
    key: str,
) -> OSSObjectInfo | None:
    try:
        return oss_client.object_info(bucket, key)
    except Exception as exc:
        if _is_access_denied(exc):
            _fail_copy(db, row, "copy_access_denied")
        raise NativeLerobotCopyTransientError(
            "platform copy destination is temporarily unavailable"
        ) from exc


def _target_matches(info: OSSObjectInfo, *, size: int, sha256: str) -> bool:
    return bool(
        info.size == size
        and info.metadata.get("x-oss-meta-sha256") == sha256
        and info.metadata.get(_COPY_ORIGIN_HEADER) == sha256
    )


def _finish_copy_success(
    db: Session,
    *,
    row: NativeLerobotDataset,
    job: JobRun,
    verified_files: int,
    verified_bytes: int,
) -> None:
    batch = db.get(Batch, row.batch_id)
    if batch is None:
        _fail_copy(db, row, "copy_state_invalid")
    now = database_now(db)
    row.copy_status = "succeeded"
    row.copy_finished_at = now
    row.copy_error_code = ""
    row.copy_error_message = ""
    row.last_copy_job_id = job.id
    from data.services.native_lerobot_import_sessions import refresh_native_lerobot_batch_status

    refresh_native_lerobot_batch_status(db, batch.id)
    detail = dict(job.detail_json or {})
    detail.update(
        {
            "file_count": verified_files,
            "total_size": verified_bytes,
            "verified_files": verified_files,
            "verified_bytes": verified_bytes,
        }
    )
    job.detail_json = detail
    db.flush()


def _mark_transient_copy_failure(db: Session, *, row: NativeLerobotDataset, job: JobRun) -> None:
    if int(job.retry_count or 0) < MAX_RETRIES:
        row.copy_status = "queued"
        row.copy_error_code = ""
        row.copy_error_message = ""
        row.copy_finished_at = None
        from data.services.native_lerobot_import_sessions import refresh_native_lerobot_batch_status

        refresh_native_lerobot_batch_status(db, row.batch_id)
        db.flush()
        return
    _set_copy_failure(db, row, "copy_unavailable")


def _fail_copy(db: Session, row: NativeLerobotDataset, code: str) -> NoReturn:
    _set_copy_failure(db, row, code)
    raise NativeLerobotCopyFailure(code)


def _set_copy_failure(db: Session, row: NativeLerobotDataset, code: str) -> None:
    now = database_now(db)
    row.copy_status = "failed"
    row.copy_finished_at = now
    row.copy_error_code = code
    row.copy_error_message = _COPY_ERROR_MESSAGES[code]
    batch = db.get(Batch, row.batch_id)
    if batch is not None and batch.batch_type == "lerobot":
        from data.services.native_lerobot_import_sessions import refresh_native_lerobot_batch_status

        refresh_native_lerobot_batch_status(db, batch.id)
    db.flush()


def _prefixes_overlap(left: str, right: str) -> bool:
    left_root = left.rstrip("/")
    right_root = right.rstrip("/")
    return (
        left_root == right_root
        or left_root.startswith(f"{right_root}/")
        or right_root.startswith(f"{left_root}/")
    )


def _is_access_denied(exc: Exception) -> bool:
    status = getattr(exc, "status", None)
    code = str(getattr(exc, "code", "") or "").lower()
    message = str(exc).lower()
    return status in {401, 403} or "accessdenied" in code or "access denied" in message
