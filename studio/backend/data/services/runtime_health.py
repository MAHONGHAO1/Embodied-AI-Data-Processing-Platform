"""Bounded, credential-free runtime capacity health projections."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from data.database import JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING, JobRun, RealtimeEvent
from data.infra.redis_client import redis_service
from data.services.job_queue_limits import KNOWN_JOB_QUEUES
from data.services.task_dispatcher import worker_status_snapshot

logger = logging.getLogger("quicdata.runtime_health")


class DatabaseUnavailableError(RuntimeError):
    """Stable dependency error that never includes database connection details."""

    def __init__(self) -> None:
        super().__init__("database_unavailable")


def require_database_health(db: Session) -> None:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Database health check failed", extra={"error_type": type(exc).__name__})
        raise DatabaseUnavailableError() from exc


def build_runtime_health_snapshot(
    db: Session,
    *,
    now: datetime | None = None,
    storage_root: str | Path,
    worker_status_provider: Callable[[], dict[str, Any]] = worker_status_snapshot,
    redis_ping: Callable[[], None] = redis_service.ping_required,
) -> dict[str, Any]:
    """Return admin-only capacity facts with no locators or credentials."""
    current = now or datetime.utcnow()
    require_database_health(db)
    redis_ping()

    queued_rows = db.execute(
        select(
            JobRun.queue,
            func.count(JobRun.id),
            func.min(JobRun.created_at),
        )
        .where(JobRun.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RETRY_PENDING)))
        .group_by(JobRun.queue)
    ).all()
    queued_by_name = {str(queue): (int(depth), oldest) for queue, depth, oldest in queued_rows}
    queue_names = sorted(set(KNOWN_JOB_QUEUES).union(queued_by_name))
    queue_items = []
    for queue in queue_names:
        depth, oldest = queued_by_name.get(queue, (0, None))
        queue_items.append(
            {
                "queue": queue,
                "depth": depth,
                "oldest_age_seconds": _age_seconds(current, oldest),
            }
        )

    pending_count, pending_oldest = db.execute(
        select(func.count(RealtimeEvent.event_id), func.min(RealtimeEvent.created_at)).where(
            RealtimeEvent.published_at.is_(None)
        )
    ).one()
    storage = _storage_capacity(Path(storage_root))

    return {
        "database": True,
        "redis": True,
        "workers": worker_status_provider(),
        "queues": {
            "total_depth": sum(item["depth"] for item in queue_items),
            "items": queue_items,
        },
        "outbox": {
            "pending": int(pending_count or 0),
            "oldest_age_seconds": _age_seconds(current, pending_oldest),
        },
        "storage": storage,
    }


def _age_seconds(now: datetime, moment: datetime | None) -> int | None:
    if moment is None:
        return None
    return max(0, int((now - moment).total_seconds()))


def _storage_capacity(root: Path) -> dict[str, int | float | bool]:
    target = root
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = shutil.disk_usage(target)
    return {
        "configured_root_exists": root.exists(),
        "total_bytes": int(usage.total),
        "free_bytes": int(usage.free),
        "used_percent": round((usage.used / usage.total) * 100, 2) if usage.total else 0.0,
    }
