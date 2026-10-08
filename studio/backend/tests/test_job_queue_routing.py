from __future__ import annotations

from uuid import uuid4

import pytest

from data.services.derived_preview_batch import (
    DERIVED_PREVIEW_BATCH_JOB_KIND,
    DERIVED_PREVIEW_BATCH_QUEUE,
)
from data.services.job_queue_limits import expected_queue_for_job_kind
from data.services.job_runs import create_or_get_job


@pytest.mark.parametrize(
    ("kind", "queue"),
    (
        ("import_scan", "ingest"),
        ("import_materialize", "ingest"),
        ("import_parse", "ingest"),
        ("native_lerobot_direct_validate", "ingest"),
        ("episode_quality", "media"),
        ("episode_preview", "media"),
        ("derived_preview_batch", "media"),
        ("episode_publish", "publish"),
        ("dataset_export", "export"),
        ("native_lerobot_scan", "export"),
        ("native_lerobot_copy", "export"),
        ("native_lerobot_bundle", "export"),
        ("dashboard_etl", "analytics"),
        ("episode_ai_suggestion", "ai"),
    ),
)
def test_job_kind_has_one_authoritative_queue(kind, queue):
    assert expected_queue_for_job_kind(kind) == queue


def test_known_job_kind_rejects_wrong_queue(db_session):
    with pytest.raises(ValueError, match="must use queue analytics"):
        create_or_get_job(
            db_session,
            kind="dashboard_etl",
            resource_type="platform",
            resource_id="global",
            idempotency_key=f"queue-routing:{uuid4().hex}",
            queue="general",
        )


def test_derived_preview_batch_uses_its_authoritative_queue():
    assert (
        expected_queue_for_job_kind(DERIVED_PREVIEW_BATCH_JOB_KIND) == DERIVED_PREVIEW_BATCH_QUEUE
    )


def test_native_lerobot_copy_has_a_handler_and_uses_recoverable_export_leases():
    from data.services.job_runs import job_kind_is_recoverable
    from data.tasks.batch_workers import _HANDLERS

    assert "native_lerobot_copy" in _HANDLERS
    assert job_kind_is_recoverable("native_lerobot_copy")


def test_native_lerobot_scan_has_a_handler_and_uses_recoverable_export_leases():
    from data.services.job_runs import job_kind_is_recoverable
    from data.tasks.batch_workers import _HANDLERS

    assert "native_lerobot_scan" in _HANDLERS
    assert job_kind_is_recoverable("native_lerobot_scan")


def test_native_lerobot_direct_validation_has_a_handler_and_uses_recoverable_ingest_leases():
    from data.services.job_runs import job_kind_is_recoverable
    from data.tasks.batch_workers import _HANDLERS

    assert "native_lerobot_direct_validate" in _HANDLERS
    assert job_kind_is_recoverable("native_lerobot_direct_validate")


def test_celery_routes_control_and_analytics_tasks_separately():
    from data.celery_app import celery_app

    queue_names = {queue.name for queue in celery_app.conf.task_queues}
    assert {"control", "ingest", "media", "publish", "export", "analytics", "ai"} <= queue_names
    assert celery_app.conf.task_default_queue == "control"
    assert celery_app.conf.task_routes["quicdata.batch.recover-expired"]["queue"] == "control"
    assert celery_app.conf.task_routes["quicdata.dashboard.etl"]["queue"] == "analytics"
    assert celery_app.conf.task_routes["quicdata.runtime.retention"]["queue"] == "control"
    assert celery_app.conf.beat_schedule["runtime-retention-hourly"] == {
        "task": "quicdata.runtime.retention",
        "schedule": 3600.0,
    }
