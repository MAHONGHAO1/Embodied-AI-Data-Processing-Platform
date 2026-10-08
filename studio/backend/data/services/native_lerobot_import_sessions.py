"""Shared, server-side native LeRobot scan snapshots and batch aggregation."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import (
    Batch,
    ExternalOssImportScope,
    JobRun,
    NativeLerobotDataset,
    NativeLerobotImportSelection,
    NativeLerobotImportSession,
    NativeLerobotScanCandidate,
    NativeLerobotScanSnapshot,
    TaskSet,
)
from data.realtime.outbox import enqueue_resource_event
from data.realtime.projections import native_lerobot_scan_snapshot
from data.services.job_runs import (
    NonRetryableJobError,
    create_or_get_job_in_transaction,
    database_now,
)
from data.services.native_lerobot_datasets import (
    NativeLerobotMarker,
    NativeLerobotMarkerError,
    _scope_allows_key,
    _scope_allows_prefix,
    discover_native_lerobot_markers,
    platform_native_lerobot_uri,
    read_native_lerobot_marker,
)
from data.services.workspace_access import require_workspace_actor

NATIVE_LEROBOT_SCAN_KIND = "native_lerobot_scan"
NATIVE_LEROBOT_SCAN_QUEUE = "export"
_SCAN_ERROR_CODES = frozenset({"scan_invalid", "scan_unavailable", "scan_state_invalid"})
_LIVE_SCAN_JOB_STATUSES = frozenset({"queued", "running", "retry_pending"})
_SESSION_RESULT_STATUSES = frozenset(
    {"registered", "skipped_duplicate", "skipped_stale", "skipped_invalid", "skipped_forbidden"}
)
_MAX_SESSION_CANDIDATES = 100


class NativeLerobotScanFailure(NonRetryableJobError):
    """A stable, non-sensitive terminal scan failure."""

    def __init__(self, code: str):
        self.code = code if code in _SCAN_ERROR_CODES else "scan_invalid"
        super().__init__(self.code)


class NativeLerobotSessionError(ValueError):
    """A stable validation failure for the native LeRobot session API."""


class NativeLerobotSessionConflictError(NativeLerobotSessionError):
    """A client request conflicts with a durable session submission."""


@dataclass(frozen=True)
class NativeLerobotSubmitResult:
    session: NativeLerobotImportSession
    selections: tuple[NativeLerobotImportSelection, ...]
    copy_jobs: tuple[JobRun, ...]
    replayed: bool = False


@dataclass(frozen=True)
class NativeLerobotScanRequest:
    snapshot: NativeLerobotScanSnapshot
    job: JobRun
    created: bool

    def __iter__(self):
        # Keep the compact tuple calling convention used by service callers.
        yield self.snapshot
        yield self.job
        yield self.created


def request_native_lerobot_scan(
    db: Session,
    *,
    workspace_id: int,
    task_set_id: int,
    actor_id: int,
) -> NativeLerobotScanRequest:
    """Create one durable scan per task-set scope, or return its in-flight job."""
    _locked_task_set_actor(
        db, workspace_id=workspace_id, task_set_id=task_set_id, actor_id=actor_id
    )
    in_flight = list(
        db.scalars(
            select(NativeLerobotScanSnapshot)
            .where(
                NativeLerobotScanSnapshot.workspace_id == workspace_id,
                NativeLerobotScanSnapshot.task_set_id == task_set_id,
                NativeLerobotScanSnapshot.status.in_(("queued", "running")),
            )
            .order_by(
                NativeLerobotScanSnapshot.created_at.asc(), NativeLerobotScanSnapshot.id.asc()
            )
            .with_for_update()
        )
    )
    for snapshot in in_flight:
        job = _live_snapshot_job(db, snapshot)
        if job is not None:
            return NativeLerobotScanRequest(snapshot=snapshot, job=job, created=False)
        _supersede_stranded_snapshot(db, snapshot)

    snapshot = NativeLerobotScanSnapshot(
        id=str(uuid4()),
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        requested_by_user_id=actor_id,
        status="queued",
        is_current=False,
    )
    db.add(snapshot)
    db.flush()
    job = create_or_get_job_in_transaction(
        db,
        kind=NATIVE_LEROBOT_SCAN_KIND,
        resource_type="native_lerobot_scan_snapshot",
        resource_id=snapshot.id,
        idempotency_key=f"native-lerobot-scan:{snapshot.id}",
        queue=NATIVE_LEROBOT_SCAN_QUEUE,
        actor_id=actor_id,
        workspace_id=workspace_id,
        task_set_id=task_set_id,
    )
    snapshot.job_id = job.id
    db.flush()
    return NativeLerobotScanRequest(snapshot=snapshot, job=job, created=True)


def run_native_lerobot_scan(db: Session, job: JobRun) -> dict[str, int]:
    """Persist a bounded scan result without exposing its source locator."""
    snapshot = _locked_snapshot_for_job(db, job)
    if snapshot.status not in {"queued", "running"}:
        raise NativeLerobotScanFailure("scan_state_invalid")
    snapshot.status = "running"
    snapshot.started_at = snapshot.started_at or database_now(db)
    snapshot.error_code = ""
    db.flush()
    _enqueue_scan_snapshot_event(db, snapshot)

    invalid_count = 0

    def record_invalid(_scope: object, _marker_key: str, _code: str) -> None:
        nonlocal invalid_count
        invalid_count += 1

    try:
        discovered = discover_native_lerobot_markers(
            db,
            workspace_id=snapshot.workspace_id,
            task_set_id=snapshot.task_set_id,
            on_invalid=record_invalid,
        )
        for scope, marker in discovered:
            _persist_valid_candidate(
                db,
                snapshot=snapshot,
                marker=marker,
                scope_id=scope.id,
                scope_revision=scope.revision,
            )
    except NativeLerobotScanFailure as exc:
        _fail_snapshot(db, snapshot, exc.code)
        raise
    except Exception as exc:
        _fail_snapshot(db, snapshot, "scan_unavailable")
        raise NativeLerobotScanFailure("scan_unavailable") from exc

    snapshot.candidate_count = len(discovered) + invalid_count
    snapshot.valid_count = len(discovered)
    snapshot.invalid_count = invalid_count
    snapshot.status = "succeeded"
    snapshot.finished_at = database_now(db)
    snapshot.error_code = ""
    _promote_current_snapshot(db, snapshot)
    db.flush()
    _enqueue_scan_snapshot_event(db, snapshot)
    return {
        "candidate_count": snapshot.candidate_count,
        "valid_count": snapshot.valid_count,
        "invalid_count": snapshot.invalid_count,
    }


def current_native_lerobot_scan_snapshot(
    db: Session,
    *,
    workspace_id: int,
    task_set_id: int,
) -> NativeLerobotScanSnapshot | None:
    return db.scalar(
        select(NativeLerobotScanSnapshot)
        .where(
            NativeLerobotScanSnapshot.workspace_id == workspace_id,
            NativeLerobotScanSnapshot.task_set_id == task_set_id,
            NativeLerobotScanSnapshot.status == "succeeded",
            NativeLerobotScanSnapshot.is_current.is_(True),
        )
        .with_for_update()
    )


def native_lerobot_scan_candidate_item(row: NativeLerobotScanCandidate) -> dict[str, object]:
    """Return a browser-safe candidate projection with no source locator."""
    return {
        "id": row.id,
        "scan_snapshot_id": row.scan_snapshot_id,
        "robot_type": row.robot_type,
        "dataset_id": row.dataset_id,
        "file_count": row.file_count,
        "total_size": row.total_size,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "manifest_sha256": row.manifest_sha256,
        "marker_sha256": row.marker_sha256,
        "status": row.status,
        "error_code": row.error_code,
    }


def native_lerobot_scan_snapshot_item(row: NativeLerobotScanSnapshot) -> dict[str, object]:
    """Return scan state without its source topology or persisted locator."""
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "task_set_id": row.task_set_id,
        "job_id": row.job_id,
        "status": row.status,
        "is_current": row.is_current,
        "candidate_count": row.candidate_count,
        "valid_count": row.valid_count,
        "invalid_count": row.invalid_count,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def native_lerobot_import_session_item(row: NativeLerobotImportSession) -> dict[str, object]:
    """Return session progress with no candidate locator or source authority."""
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "workspace_id": row.workspace_id,
        "task_set_id": row.task_set_id,
        "scan_snapshot_id": row.scan_snapshot_id,
        "status": row.status,
        "selected_count": row.selected_count,
        "registered_count": row.registered_count,
        "skipped_count": row.skipped_count,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def native_lerobot_selection_item(row: NativeLerobotImportSelection) -> dict[str, object]:
    """Project one immutable outcome, including only stable public IDs."""
    return {
        "candidate_id": row.scan_candidate_id,
        "native_dataset_id": row.native_lerobot_dataset_id,
        "status": row.result_status,
        "error_code": row.error_code,
    }


def create_native_lerobot_import_session(
    db: Session,
    *,
    batch_id: int,
    actor_id: int,
) -> NativeLerobotImportSession:
    """Bind an empty LeRobot batch to the current successful scan snapshot."""
    batch = _locked_lerobot_batch_actor(db, batch_id=batch_id, actor_id=actor_id)
    snapshot = current_native_lerobot_scan_snapshot(
        db,
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
    )
    if snapshot is None:
        raise NativeLerobotSessionError("native lerobot scan snapshot is unavailable")
    session = NativeLerobotImportSession(
        id=str(uuid4()),
        batch_id=batch.id,
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        scan_snapshot_id=snapshot.id,
        created_by_user_id=actor_id,
        status="draft",
    )
    db.add(session)
    db.flush()
    return session


def list_native_lerobot_import_sessions(
    db: Session,
    *,
    batch_id: int,
    actor_id: int,
) -> list[NativeLerobotImportSession]:
    """List only sessions attached to an actor-authorized LeRobot batch."""
    batch = _locked_lerobot_batch_actor(db, batch_id=batch_id, actor_id=actor_id)
    return list(
        db.scalars(
            select(NativeLerobotImportSession)
            .where(NativeLerobotImportSession.batch_id == batch.id)
            .order_by(
                NativeLerobotImportSession.created_at.asc(), NativeLerobotImportSession.id.asc()
            )
        )
    )


def native_lerobot_current_snapshot_for_actor(
    db: Session,
    *,
    workspace_id: int,
    task_set_id: int,
    actor_id: int,
) -> NativeLerobotScanSnapshot | None:
    _locked_task_set_actor(
        db, workspace_id=workspace_id, task_set_id=task_set_id, actor_id=actor_id
    )
    return current_native_lerobot_scan_snapshot(
        db, workspace_id=workspace_id, task_set_id=task_set_id
    )


def native_lerobot_snapshot_candidates_for_actor(
    db: Session,
    *,
    snapshot_id: str,
    actor_id: int,
) -> tuple[NativeLerobotScanSnapshot, list[NativeLerobotScanCandidate]]:
    snapshot = db.scalar(
        select(NativeLerobotScanSnapshot)
        .where(NativeLerobotScanSnapshot.id == _canonical_uuid(snapshot_id))
        .with_for_update()
    )
    if snapshot is None:
        raise NativeLerobotSessionError("native lerobot scan snapshot is unavailable")
    _locked_task_set_actor(
        db,
        workspace_id=snapshot.workspace_id,
        task_set_id=snapshot.task_set_id,
        actor_id=actor_id,
    )
    rows = list(
        db.scalars(
            select(NativeLerobotScanCandidate)
            .where(NativeLerobotScanCandidate.scan_snapshot_id == snapshot.id)
            .order_by(
                NativeLerobotScanCandidate.created_at.asc(), NativeLerobotScanCandidate.id.asc()
            )
        )
    )
    return snapshot, rows


def submit_native_lerobot_session(
    db: Session,
    *,
    session_id: str,
    candidate_ids: list[str],
    request_id: str,
    actor_id: int,
) -> NativeLerobotSubmitResult:
    """Revalidate selected scan rows and durably queue one copy per valid row."""
    canonical_request_id = _canonical_uuid(request_id)
    canonical_candidate_ids = _validated_candidate_ids(candidate_ids)
    session = _locked_native_lerobot_session(db, session_id=session_id)
    batch = _locked_lerobot_batch_actor(db, batch_id=session.batch_id, actor_id=actor_id)
    if batch.workspace_id != session.workspace_id or batch.task_set_id != session.task_set_id:
        raise NativeLerobotSessionError("native lerobot session scope is unavailable")
    if session.submit_request_id:
        if session.submit_request_id != canonical_request_id:
            raise NativeLerobotSessionConflictError("native lerobot session is already submitted")
        selections = _locked_session_selections(db, session)
        _assert_replay_matches(selections, canonical_candidate_ids)
        return NativeLerobotSubmitResult(
            session=session,
            selections=tuple(_ordered_selections(selections, canonical_candidate_ids)),
            copy_jobs=tuple(_jobs_for_selections(db, selections)),
            replayed=True,
        )
    existing_request = db.scalar(
        select(NativeLerobotImportSession)
        .where(NativeLerobotImportSession.submit_request_id == canonical_request_id)
        .with_for_update()
    )
    if existing_request is not None:
        raise NativeLerobotSessionConflictError("native lerobot request id is already in use")
    if session.status != "draft" or not session.scan_snapshot_id:
        raise NativeLerobotSessionConflictError("native lerobot session is not submit-ready")

    candidates = _locked_session_candidates(
        db, session=session, candidate_ids=canonical_candidate_ids
    )
    scopes = _locked_candidate_scopes(db, candidates)
    ordered_candidates = [candidates[candidate_id] for candidate_id in canonical_candidate_ids]
    selections: list[NativeLerobotImportSelection] = []
    jobs: list[JobRun] = []
    # The savepoint keeps expected duplicate races local while unexpected
    # persistence failures still abort the caller's outer transaction.
    try:
        with db.begin_nested():
            for candidate in ordered_candidates:
                try:
                    require_workspace_actor(
                        db, actor_id=actor_id, workspace_id=session.workspace_id
                    )
                except PermissionError:
                    selection, job = (
                        _new_selection(
                            session,
                            candidate,
                            status="skipped_forbidden",
                            error_code="workspace_access_changed",
                        ),
                        None,
                    )
                else:
                    selection, job = _register_or_skip_candidate(
                        db,
                        session=session,
                        candidate=candidate,
                        scope=scopes.get(candidate.source_scope_id),
                        actor_id=actor_id,
                    )
                selections.append(selection)
                if job is not None:
                    jobs.append(job)
            session.submit_request_id = canonical_request_id
            session.selected_count = len(selections)
            session.registered_count = sum(row.result_status == "registered" for row in selections)
            session.skipped_count = len(selections) - session.registered_count
            session.status = "completed" if session.skipped_count == 0 else "completed_with_skips"
            session.error_code = ""
            refresh_native_lerobot_batch_status(db, session.batch_id)
            db.flush()
    except IntegrityError as exc:
        if not _is_submit_request_conflict(exc):
            raise
        db.expire_all()
        winner = db.scalar(
            select(NativeLerobotImportSession)
            .where(NativeLerobotImportSession.submit_request_id == canonical_request_id)
            .with_for_update()
        )
        if winner is None:
            raise
        if winner.id == session_id:
            winner_selections = _locked_session_selections(db, winner)
            _assert_replay_matches(winner_selections, canonical_candidate_ids)
            return NativeLerobotSubmitResult(
                session=winner,
                selections=tuple(_ordered_selections(winner_selections, canonical_candidate_ids)),
                copy_jobs=tuple(_jobs_for_selections(db, winner_selections)),
                replayed=True,
            )
        raise NativeLerobotSessionConflictError(
            "native lerobot request id is already in use"
        ) from exc
    return NativeLerobotSubmitResult(
        session=session, selections=tuple(selections), copy_jobs=tuple(jobs)
    )


def _locked_lerobot_batch_actor(db: Session, *, batch_id: int, actor_id: int) -> Batch:
    if not isinstance(batch_id, int) or isinstance(batch_id, bool) or batch_id <= 0:
        raise NativeLerobotSessionError("native lerobot batch is unavailable")
    # Copy workers lock source scope before the parent batch.  Session submit
    # follows that order by leaving this immutable parent row unlocked until
    # refresh_native_lerobot_batch_status acquires it after scope revalidation.
    batch = db.scalar(select(Batch).where(Batch.id == batch_id))
    if batch is None or batch.batch_type != "lerobot":
        raise NativeLerobotSessionError("native lerobot batch is unavailable")
    require_workspace_actor(db, actor_id=actor_id, workspace_id=batch.workspace_id)
    task_set = db.get(TaskSet, batch.task_set_id)
    if task_set is None or task_set.workspace_id != batch.workspace_id:
        raise PermissionError("native lerobot batch scope is unavailable")
    return batch


def _locked_native_lerobot_session(db: Session, *, session_id: str) -> NativeLerobotImportSession:
    session = db.scalar(
        select(NativeLerobotImportSession)
        .where(NativeLerobotImportSession.id == _canonical_uuid(session_id))
        .with_for_update()
    )
    if session is None:
        raise NativeLerobotSessionError("native lerobot session is unavailable")
    return session


def _canonical_uuid(value: str) -> str:
    try:
        raw = str(value)
        parsed = UUID(raw)
    except (TypeError, ValueError, AttributeError) as exc:
        raise NativeLerobotSessionError("native lerobot identifier is invalid") from exc
    canonical = str(parsed)
    if raw != canonical:
        raise NativeLerobotSessionError("native lerobot identifier is invalid")
    return canonical


def _validated_candidate_ids(candidate_ids: list[str]) -> list[str]:
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or len(candidate_ids) > _MAX_SESSION_CANDIDATES
    ):
        raise NativeLerobotSessionError("candidate_ids must contain 1-100 items")
    canonical = [_canonical_uuid(value) for value in candidate_ids]
    if len(set(canonical)) != len(canonical):
        raise NativeLerobotSessionError("candidate_ids must not contain duplicates")
    return canonical


def _locked_session_candidates(
    db: Session,
    *,
    session: NativeLerobotImportSession,
    candidate_ids: list[str],
) -> dict[str, NativeLerobotScanCandidate]:
    rows = list(
        db.scalars(
            select(NativeLerobotScanCandidate)
            .where(
                NativeLerobotScanCandidate.scan_snapshot_id == session.scan_snapshot_id,
                NativeLerobotScanCandidate.id.in_(candidate_ids),
            )
            .order_by(NativeLerobotScanCandidate.id.asc())
            .with_for_update()
        )
    )
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(candidate_ids):
        raise NativeLerobotSessionError("candidate does not belong to the scan snapshot")
    return by_id


def _locked_candidate_scopes(
    db: Session,
    candidates: dict[str, NativeLerobotScanCandidate],
) -> dict[int, ExternalOssImportScope]:
    scope_ids = sorted(
        {row.source_scope_id for row in candidates.values() if row.source_scope_id is not None}
    )
    if not scope_ids:
        return {}
    rows = list(
        db.scalars(
            select(ExternalOssImportScope)
            .where(ExternalOssImportScope.id.in_(scope_ids))
            .order_by(ExternalOssImportScope.id.asc())
            .with_for_update()
        )
    )
    return {row.id: row for row in rows}


def _register_or_skip_candidate(
    db: Session,
    *,
    session: NativeLerobotImportSession,
    candidate: NativeLerobotScanCandidate,
    scope: ExternalOssImportScope | None,
    actor_id: int,
) -> tuple[NativeLerobotImportSelection, JobRun | None]:
    status, error_code, marker = _candidate_revalidation(
        candidate=candidate, session=session, scope=scope
    )
    if status != "registered" or marker is None:
        return _new_selection(session, candidate, status=status, error_code=error_code), None
    existing = db.scalar(
        select(NativeLerobotDataset)
        .where(
            NativeLerobotDataset.workspace_id == session.workspace_id,
            NativeLerobotDataset.source_oss_uri == marker.oss_uri,
            NativeLerobotDataset.marker_sha256 == marker.marker_sha256,
        )
        .with_for_update()
    )
    if existing is not None:
        return _new_selection(
            session, candidate, status="skipped_duplicate", error_code="already_registered"
        ), None
    try:
        with db.begin_nested():
            dataset = NativeLerobotDataset(
                workspace_id=session.workspace_id,
                task_set_id=session.task_set_id,
                batch_id=session.batch_id,
                import_session_id=session.id,
                name=f"{marker.robot_type}/{marker.dataset_id}",
                source_oss_uri=marker.oss_uri,
                source_scope_id=scope.id,
                source_scope_revision=scope.revision,
                oss_uri=None,
                robot_type=marker.robot_type,
                dataset_id=marker.dataset_id,
                file_count=marker.file_count,
                total_size=marker.total_size,
                completed_at=marker.completed_at.replace(tzinfo=None),
                manifest_sha256=marker.manifest_sha256,
                marker_sha256=marker.marker_sha256,
                objects_json=[
                    {
                        "path": item.path,
                        "size": item.size,
                        "sha256": item.sha256,
                    }
                    for item in marker.objects
                ],
                status="active",
                copy_status="queued",
                created_by_user_id=actor_id,
            )
            db.add(dataset)
            db.flush()
            dataset.oss_uri = platform_native_lerobot_uri(
                workspace_id=session.workspace_id,
                task_set_id=session.task_set_id,
                batch_id=session.batch_id,
                native_dataset_id=dataset.id,
                robot_type=marker.robot_type,
                dataset_id=marker.dataset_id,
            )
            job = create_or_get_job_in_transaction(
                db,
                kind="native_lerobot_copy",
                resource_type="native_lerobot_dataset",
                resource_id=dataset.id,
                idempotency_key=f"native-copy:{dataset.id}:{marker.marker_sha256}",
                queue="export",
                actor_id=actor_id,
                workspace_id=session.workspace_id,
                task_set_id=session.task_set_id,
                detail={
                    "marker_sha256": marker.marker_sha256,
                    "file_count": marker.file_count,
                    "total_size": marker.total_size,
                },
            )
            dataset.last_copy_job_id = job.id
            selection = _new_selection(
                session,
                candidate,
                status="registered",
                error_code="",
                native_dataset_id=dataset.id,
            )
            db.flush()
    except IntegrityError as exc:
        if not _is_known_native_duplicate(exc):
            raise
        existing = db.scalar(
            select(NativeLerobotDataset)
            .where(
                NativeLerobotDataset.workspace_id == session.workspace_id,
                NativeLerobotDataset.source_oss_uri == marker.oss_uri,
                NativeLerobotDataset.marker_sha256 == marker.marker_sha256,
            )
            .with_for_update()
        )
        if existing is None:
            raise
        return _new_selection(
            session, candidate, status="skipped_duplicate", error_code="already_registered"
        ), None
    return selection, job


def _is_known_native_duplicate(exc: IntegrityError) -> bool:
    diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
    return (
        getattr(diagnostic, "constraint_name", None)
        == "uq_native_lerobot_datasets_workspace_source_marker"
    )


def _is_submit_request_conflict(exc: IntegrityError) -> bool:
    diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
    return (
        getattr(diagnostic, "constraint_name", None)
        == "uq_native_lerobot_import_sessions_submit_request"
    )


def _candidate_revalidation(
    *,
    candidate: NativeLerobotScanCandidate,
    session: NativeLerobotImportSession,
    scope: ExternalOssImportScope | None,
) -> tuple[str, str, NativeLerobotMarker | None]:
    if candidate.status != "valid":
        return "skipped_invalid", candidate.error_code or "candidate_invalid", None
    if (
        scope is None
        or not scope.is_enabled
        or candidate.source_scope_revision is None
        or scope.revision != candidate.source_scope_revision
        or scope.workspace_id != session.workspace_id
        or scope.task_set_id != session.task_set_id
        or scope.bucket != candidate.source_bucket
        or not _scope_allows_key(scope, candidate.marker_key)
    ):
        return "skipped_stale", "source_scope_changed", None
    try:
        marker = read_native_lerobot_marker(bucket=scope.bucket, marker_key=candidate.marker_key)
    except NativeLerobotMarkerError:
        # A marker that is unavailable or no longer valid is a stale scan row,
        # not an API failure.  The source exception itself is never projected.
        return "skipped_stale", "marker_unavailable", None
    if (
        not _scope_allows_prefix(scope, marker.marker_key.removesuffix("complete.json"))
        or not hmac.compare_digest(marker.marker_sha256, candidate.marker_sha256)
        or not hmac.compare_digest(marker.manifest_sha256, candidate.manifest_sha256)
        or marker.robot_type != candidate.robot_type
        or marker.dataset_id != candidate.dataset_id
        or marker.file_count != candidate.file_count
        or marker.total_size != candidate.total_size
    ):
        return "skipped_stale", "marker_changed", None
    return "registered", "", marker


def _new_selection(
    session: NativeLerobotImportSession,
    candidate: NativeLerobotScanCandidate,
    *,
    status: str,
    error_code: str,
    native_dataset_id: int | None = None,
) -> NativeLerobotImportSelection:
    if status not in _SESSION_RESULT_STATUSES:
        raise RuntimeError("native lerobot selection status is invalid")
    row = NativeLerobotImportSelection(
        id=str(uuid4()),
        import_session_id=session.id,
        scan_candidate_id=candidate.id,
        native_lerobot_dataset_id=native_dataset_id,
        result_status=status,
        error_code=error_code,
    )
    # Add before returning so the session result and its durable copy job share
    # the same transaction boundary.
    session.selections.append(row)
    return row


def _locked_session_selections(
    db: Session,
    session: NativeLerobotImportSession,
) -> list[NativeLerobotImportSelection]:
    return list(
        db.scalars(
            select(NativeLerobotImportSelection)
            .where(NativeLerobotImportSelection.import_session_id == session.id)
            .with_for_update()
        )
    )


def _assert_replay_matches(
    selections: list[NativeLerobotImportSelection], candidate_ids: list[str]
) -> None:
    if {row.scan_candidate_id for row in selections} != set(candidate_ids) or len(
        selections
    ) != len(candidate_ids):
        raise NativeLerobotSessionConflictError(
            "native lerobot request payload conflicts with prior submission"
        )


def _ordered_selections(
    selections: list[NativeLerobotImportSelection], candidate_ids: list[str]
) -> list[NativeLerobotImportSelection]:
    by_candidate = {row.scan_candidate_id: row for row in selections}
    return [by_candidate[candidate_id] for candidate_id in candidate_ids]


def _jobs_for_selections(
    db: Session, selections: list[NativeLerobotImportSelection]
) -> list[JobRun]:
    ids = {
        row.native_lerobot_dataset_id
        for row in selections
        if row.native_lerobot_dataset_id is not None
    }
    if not ids:
        return []
    rows = list(
        db.scalars(
            select(JobRun)
            .where(
                JobRun.resource_type == "native_lerobot_dataset",
                JobRun.resource_id.in_([str(item) for item in ids]),
                JobRun.kind == "native_lerobot_copy",
            )
            .with_for_update()
        )
    )
    return sorted(rows, key=lambda job: job.id)


def refresh_native_lerobot_batch_status(db: Session, batch_id: int) -> str:
    """Derive a LeRobot batch state from all active child copy states."""
    batch = db.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())
    if batch is None or batch.batch_type != "lerobot":
        raise ValueError("native lerobot batch is unavailable")
    states = {
        row.copy_status
        for row in db.scalars(
            select(NativeLerobotDataset).where(
                NativeLerobotDataset.batch_id == batch.id,
                NativeLerobotDataset.status == "active",
            )
        )
    }
    if not states:
        status = "created"
    elif states & {"queued", "running", "backfill_pending"}:
        status = "processing"
    elif states == {"succeeded"}:
        status = "ready"
    elif "succeeded" in states:
        status = "partial_failed"
    else:
        status = "failed"
    batch.status = status
    db.flush()
    return status


def _locked_task_set_actor(
    db: Session, *, workspace_id: int, task_set_id: int, actor_id: int
) -> TaskSet:
    if not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id <= 0:
        raise PermissionError("native lerobot scan actor is unavailable")
    task_set = db.scalar(select(TaskSet).where(TaskSet.id == task_set_id).with_for_update())
    if task_set is None or task_set.workspace_id != workspace_id:
        raise PermissionError("native lerobot scan scope is unavailable")
    require_workspace_actor(db, actor_id=actor_id, workspace_id=workspace_id)
    return task_set


def _live_snapshot_job(db: Session, snapshot: NativeLerobotScanSnapshot) -> JobRun | None:
    if not snapshot.job_id:
        return None
    job = db.scalar(select(JobRun).where(JobRun.id == snapshot.job_id).with_for_update())
    if (
        job is None
        or job.kind != NATIVE_LEROBOT_SCAN_KIND
        or job.resource_type != "native_lerobot_scan_snapshot"
        or job.resource_id != snapshot.id
        or job.workspace_id != snapshot.workspace_id
        or job.task_set_id != snapshot.task_set_id
        or job.queue != NATIVE_LEROBOT_SCAN_QUEUE
        or job.status not in _LIVE_SCAN_JOB_STATUSES
    ):
        return None
    return job


def _supersede_stranded_snapshot(db: Session, snapshot: NativeLerobotScanSnapshot) -> None:
    snapshot.status = "superseded"
    snapshot.is_current = False
    snapshot.error_code = "scan_state_invalid"
    snapshot.finished_at = database_now(db)
    db.flush()


def _snapshot_job(db: Session, snapshot: NativeLerobotScanSnapshot) -> JobRun:
    if not snapshot.job_id:
        raise NativeLerobotScanFailure("scan_state_invalid")
    job = db.scalar(select(JobRun).where(JobRun.id == snapshot.job_id).with_for_update())
    if (
        job is None
        or job.kind != NATIVE_LEROBOT_SCAN_KIND
        or job.resource_type != "native_lerobot_scan_snapshot"
        or job.resource_id != snapshot.id
        or job.workspace_id != snapshot.workspace_id
        or job.task_set_id != snapshot.task_set_id
        or job.queue != NATIVE_LEROBOT_SCAN_QUEUE
    ):
        raise NativeLerobotScanFailure("scan_state_invalid")
    return job


def _locked_snapshot_for_job(db: Session, job: JobRun) -> NativeLerobotScanSnapshot:
    if job.kind != NATIVE_LEROBOT_SCAN_KIND or job.resource_type != "native_lerobot_scan_snapshot":
        raise NativeLerobotScanFailure("scan_state_invalid")
    snapshot = db.scalar(
        select(NativeLerobotScanSnapshot)
        .where(NativeLerobotScanSnapshot.id == job.resource_id)
        .with_for_update()
    )
    if snapshot is None or snapshot.job_id != job.id:
        raise NativeLerobotScanFailure("scan_state_invalid")
    _snapshot_job(db, snapshot)
    return snapshot


def _persist_valid_candidate(
    db: Session,
    *,
    snapshot: NativeLerobotScanSnapshot,
    marker: NativeLerobotMarker,
    scope_id: int,
    scope_revision: int,
) -> None:
    db.add(
        NativeLerobotScanCandidate(
            id=str(uuid4()),
            scan_snapshot_id=snapshot.id,
            source_scope_id=scope_id,
            source_scope_revision=scope_revision,
            source_bucket=marker.bucket,
            marker_key=marker.marker_key,
            robot_type=marker.robot_type,
            dataset_id=marker.dataset_id,
            file_count=marker.file_count,
            total_size=marker.total_size,
            completed_at=marker.completed_at.replace(tzinfo=None),
            manifest_sha256=marker.manifest_sha256,
            marker_sha256=marker.marker_sha256,
            locator_json={
                "scope_id": scope_id,
                "scope_revision": scope_revision,
                "bucket": marker.bucket,
                "marker_key": marker.marker_key,
            },
            status="valid",
        )
    )


def _promote_current_snapshot(db: Session, snapshot: NativeLerobotScanSnapshot) -> None:
    current = list(
        db.scalars(
            select(NativeLerobotScanSnapshot)
            .where(
                NativeLerobotScanSnapshot.workspace_id == snapshot.workspace_id,
                NativeLerobotScanSnapshot.task_set_id == snapshot.task_set_id,
                NativeLerobotScanSnapshot.is_current.is_(True),
            )
            .with_for_update()
        )
    )
    for row in current:
        row.is_current = False
    db.flush()
    snapshot.is_current = True


def _fail_snapshot(db: Session, snapshot: NativeLerobotScanSnapshot, code: str) -> None:
    snapshot.status = "failed"
    snapshot.is_current = False
    snapshot.error_code = code
    snapshot.finished_at = database_now(db)
    db.flush()
    _enqueue_scan_snapshot_event(db, snapshot)


def _enqueue_scan_snapshot_event(db: Session, snapshot: NativeLerobotScanSnapshot) -> None:
    enqueue_resource_event(
        db,
        resource=snapshot,
        resource_type="native_lerobot_scan_snapshot",
        event_name="native_lerobot_scan_snapshot.updated",
        resource_snapshot=native_lerobot_scan_snapshot(snapshot),
    )
