from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from data.config import settings
from data.database import JobRun, RealtimeEvent
from data.services import task_dispatcher
from data.services.runtime_health import (
    DatabaseUnavailableError,
    build_runtime_health_snapshot,
    require_database_health,
)


def _job(*, queue: str, status: str, created_at: datetime) -> JobRun:
    identifier = str(uuid4())
    return JobRun(
        id=identifier,
        kind="episode_preview",
        resource_type="episode",
        resource_id=str(uuid4()),
        idempotency_key=f"runtime-health:{identifier}",
        queue=queue,
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


def test_celery_worker_status_is_cached_for_short_health_window(monkeypatch):
    probes = []

    def probe():
        probes.append(True)
        return {
            "available": True,
            "worker_count": 2,
            "queues": {"media": True, "publish": True},
        }

    monkeypatch.setattr(settings, "celery_inspect_cache_seconds", 10)
    monkeypatch.setattr(task_dispatcher, "_probe_worker_status", probe)
    task_dispatcher._reset_worker_status_cache()

    assert task_dispatcher.celery_available("media") is True
    assert task_dispatcher.celery_available("media") is True
    assert task_dispatcher.worker_status_snapshot()["worker_count"] == 2
    assert len(probes) == 1


def test_runtime_health_reports_bounded_capacity_without_secrets(db_session, tmp_path):
    now = datetime.utcnow()
    queue = f"rh-{uuid4().hex[:20]}"
    db_session.add_all(
        [
            _job(queue=queue, status="queued", created_at=now - timedelta(seconds=125)),
            _job(queue=queue, status="retry_pending", created_at=now - timedelta(seconds=30)),
            _job(queue="publish", status="running", created_at=now - timedelta(hours=1)),
            RealtimeEvent(
                event_id=str(uuid4()),
                resource_type="work_queue",
                resource_id="1",
                resource_version=1,
                event_name="work_queue.invalidated",
                safe_payload={"workspace_id": 1},
                created_at=now - timedelta(seconds=181),
            ),
        ]
    )
    db_session.commit()

    snapshot = build_runtime_health_snapshot(
        db_session,
        now=now,
        storage_root=tmp_path,
        worker_status_provider=lambda: {
            "available": True,
            "worker_count": 2,
            "queues": {"media": True, "publish": True},
            "cache_age_seconds": 0,
        },
    )

    queue_health = next(item for item in snapshot["queues"]["items"] if item["queue"] == queue)
    assert snapshot["queues"]["total_depth"] >= 2
    assert queue_health == {"queue": queue, "depth": 2, "oldest_age_seconds": 125}
    assert snapshot["outbox"]["pending"] >= 1
    assert snapshot["outbox"]["oldest_age_seconds"] >= 181
    assert snapshot["storage"]["free_bytes"] > 0
    assert snapshot["workers"]["worker_count"] == 2
    serialized = str(snapshot).lower()
    assert "redis://" not in serialized
    assert "postgresql" not in serialized
    assert "storage_root" not in serialized


def test_database_health_translates_connection_details_to_stable_error():
    class BrokenSession:
        def execute(self, _statement):
            raise OSError("postgresql://user:password@secret-host/database")

    with pytest.raises(DatabaseUnavailableError, match="database_unavailable"):
        require_database_health(BrokenSession())


def test_runtime_health_endpoint_is_admin_only(client, admin_headers, viewer_headers):
    denied = client.get("/api/v1/platform-settings/runtime-health", headers=viewer_headers)
    allowed = client.get("/api/v1/platform-settings/runtime-health", headers=admin_headers)

    assert denied.status_code == 403
    assert allowed.status_code == 200
    data = allowed.json()["data"]
    assert {"database", "redis", "workers", "queues", "outbox", "storage"} <= set(data)
