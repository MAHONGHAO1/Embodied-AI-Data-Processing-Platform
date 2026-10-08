"""Deployment-owned capacity limits for durable JobRun queues."""

from __future__ import annotations

import os
import re

JOB_KIND_QUEUES = {
    "import_scan": "ingest",
    "import_materialize": "ingest",
    "import_parse": "ingest",
    "collection_upload_parse": "ingest",
    "collection_upload_admission": "ingest",
    "native_lerobot_direct_validate": "ingest",
    "episode_quality": "media",
    "episode_preview": "media",
    "derived_preview_batch": "media",
    "dataset_export": "export",
    "catalog_export": "export",
    "dashboard_etl": "analytics",
    "episode_ai_suggestion": "ai",
}
KNOWN_JOB_QUEUES = ("control", "ingest", "media", "publish", "export", "analytics", "ai", "general")
MAX_QUEUE_SLOT_COUNT = 32


def expected_queue_for_job_kind(kind: str) -> str | None:
    """Return the authoritative queue for a current JobRun kind, if known."""
    return JOB_KIND_QUEUES.get(str(kind or "").strip())


def require_job_kind_queue(kind: str, queue: str) -> None:
    expected = expected_queue_for_job_kind(kind)
    if expected is not None and str(queue or "").strip() != expected:
        raise ValueError(f"job kind {kind} must use queue {expected}")


def queue_slot_count(queue: str) -> int:
    """Read an explicit queue capacity, defaulting to one durable slot.

    Celery worker concurrency may be larger than this value, but the database
    lease remains the cross-process authority. Reject invalid settings instead
    of silently converting them into an unexpected amount of media work.
    """
    normalized_queue = str(queue or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", normalized_queue):
        raise RuntimeError(
            "job queue name must contain lowercase letters, digits, hyphens, or underscores"
        )

    env_name = f"JOB_QUEUE_SLOTS_{normalized_queue.upper().replace('-', '_')}"
    raw_value = os.getenv(env_name, "1").strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{env_name} must be a positive integer") from exc
    if not 1 <= value <= MAX_QUEUE_SLOT_COUNT:
        raise RuntimeError(f"{env_name} must be between 1 and {MAX_QUEUE_SLOT_COUNT}")
    return value


def validate_queue_slot_config() -> None:
    """Fail worker startup before any task can observe invalid capacity."""
    for queue in KNOWN_JOB_QUEUES:
        queue_slot_count(queue)
