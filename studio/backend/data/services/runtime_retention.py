"""Bounded retention for recoverable runtime rows.

Security audit events and business assets are intentionally outside this
service. Cleanup is limited to rows whose terminal state is explicit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session

from data.database import (
    JOB_TERMINAL_STATUSES,
    ArtifactOperation,
    Episode,
    EpisodeArtifact,
    ImportAttempt,
    ImportCandidate,
    ImportSession,
    JobQueueSlot,
    JobRun,
    RealtimeEvent,
    TaskSetSourceImport,
)
from data.services.import_intake import (
    cleanup_expired_import_staging,
    expire_direct_multipart_upload,
)
from data.services.native_lerobot_bundles import cleanup_expired_native_lerobot_bundles

logger = logging.getLogger("quicdata.runtime_retention")
_DELETABLE_IMPORT_STATUSES = frozenset({"failed", "cancelled", "superseded"})


def cleanup_runtime_rows(
    db: Session,
    *,
    now: datetime | None = None,
    realtime_event_retention_days: int,
    job_retention_days: int,
    import_session_retention_days: int,
    batch_size: int,
    expire_direct_uploads: bool = True,
) -> dict[str, int]:
    """Clean one bounded batch without committing the caller's transaction."""
    _validate_retention_inputs(
        realtime_event_retention_days=realtime_event_retention_days,
        job_retention_days=job_retention_days,
        import_session_retention_days=import_session_retention_days,
        batch_size=batch_size,
    )
    current = now or datetime.utcnow()
    result = {
        "direct_uploads_expired": 0,
        "realtime_events_deleted": 0,
        "job_runs_deleted": 0,
        "import_sessions_deleted": 0,
        "native_lerobot_bundles_expired": 0,
        "errors": 0,
    }

    if expire_direct_uploads:
        direct_session_ids = list(
            db.scalars(
                select(ImportAttempt.import_session_id)
                .join(ImportSession, ImportSession.id == ImportAttempt.import_session_id)
                .where(
                    ImportAttempt.status == "active",
                    ImportSession.status == "uploading",
                    ImportAttempt.result_json["upload_mode"].astext == "oss_multipart",
                )
                .order_by(ImportAttempt.created_at.asc(), ImportAttempt.id.asc())
                .limit(batch_size)
            )
        )
        for import_session_id in direct_session_ids:
            try:
                with db.begin_nested():
                    if expire_direct_multipart_upload(
                        db,
                        import_session_id=str(import_session_id),
                        now=current,
                    ):
                        result["direct_uploads_expired"] += 1
            except Exception as exc:
                result["errors"] += 1
                logger.warning(
                    "runtime_retention_direct_upload_failed import_session_id=%s error=%s",
                    import_session_id,
                    type(exc).__name__,
                )

    event_cutoff = current - timedelta(days=realtime_event_retention_days)
    event_ids = list(
        db.scalars(
            select(RealtimeEvent.event_id)
            .where(
                RealtimeEvent.published_at.is_not(None),
                RealtimeEvent.published_at < event_cutoff,
            )
            .order_by(RealtimeEvent.published_at.asc(), RealtimeEvent.event_id.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    if event_ids:
        deleted = db.execute(delete(RealtimeEvent).where(RealtimeEvent.event_id.in_(event_ids)))
        result["realtime_events_deleted"] = int(deleted.rowcount or 0)

    job_cutoff = current - timedelta(days=job_retention_days)
    job_ids = list(
        db.scalars(
            select(JobRun.id)
            .where(
                JobRun.status.in_(JOB_TERMINAL_STATUSES),
                func.coalesce(JobRun.finished_at, JobRun.updated_at) < job_cutoff,
            )
            .order_by(
                func.coalesce(JobRun.finished_at, JobRun.updated_at).asc(),
                JobRun.id.asc(),
            )
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    if job_ids:
        db.execute(
            update(ArtifactOperation)
            .where(ArtifactOperation.job_id.in_(job_ids))
            .values(job_id=None)
        )
        db.execute(
            update(JobQueueSlot)
            .where(JobQueueSlot.job_id.in_(job_ids))
            .values(job_id=None, worker_id="", lease_expires_at=None)
        )
        deleted = db.execute(delete(JobRun).where(JobRun.id.in_(job_ids)))
        result["job_runs_deleted"] = int(deleted.rowcount or 0)

    import_cutoff = current - timedelta(days=import_session_retention_days)
    import_session_ids = list(
        db.scalars(
            select(ImportSession.id)
            .where(
                ImportSession.status.in_(_DELETABLE_IMPORT_STATUSES),
                ImportSession.updated_at < import_cutoff,
                ~exists(select(Episode.id).where(Episode.import_session_id == ImportSession.id)),
                ~exists(
                    select(EpisodeArtifact.id).where(
                        EpisodeArtifact.import_session_id == ImportSession.id
                    )
                ),
            )
            .order_by(ImportSession.updated_at.asc(), ImportSession.id.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    removable_session_ids: list[str] = []
    for import_session_id in import_session_ids:
        import_session = db.get(ImportSession, import_session_id)
        if import_session is None:
            continue
        if import_session.retention_until is not None and import_session.retention_until <= current:
            try:
                with db.begin_nested():
                    cleanup_expired_import_staging(
                        db,
                        import_session_id=import_session.id,
                        now=current,
                    )
            except Exception as exc:
                result["errors"] += 1
                logger.warning(
                    "runtime_retention_import_staging_failed import_session_id=%s error=%s",
                    import_session.id,
                    type(exc).__name__,
                )
                continue
        removable_session_ids.append(str(import_session.id))
    if removable_session_ids:
        db.execute(
            update(TaskSetSourceImport)
            .where(TaskSetSourceImport.import_session_id.in_(removable_session_ids))
            .values(import_session_id=None)
        )
        db.execute(
            delete(ImportCandidate).where(
                ImportCandidate.import_session_id.in_(removable_session_ids)
            )
        )
        db.execute(
            delete(ImportAttempt).where(ImportAttempt.import_session_id.in_(removable_session_ids))
        )
        deleted = db.execute(
            delete(ImportSession).where(ImportSession.id.in_(removable_session_ids))
        )
        result["import_sessions_deleted"] = int(deleted.rowcount or 0)

    result["native_lerobot_bundles_expired"] = cleanup_expired_native_lerobot_bundles(
        db,
        limit=batch_size,
        now=current,
    )

    db.flush()
    return result


def _validate_retention_inputs(
    *,
    realtime_event_retention_days: int,
    job_retention_days: int,
    import_session_retention_days: int,
    batch_size: int,
) -> None:
    for name, value in (
        ("realtime_event_retention_days", realtime_event_retention_days),
        ("job_retention_days", job_retention_days),
        ("import_session_retention_days", import_session_retention_days),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= 5_000
    ):
        raise ValueError("batch_size must be between 1 and 5000")
