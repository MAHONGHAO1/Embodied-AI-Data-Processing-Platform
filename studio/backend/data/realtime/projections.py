"""Explicit browser-safe realtime projections for Batch / Episode resources."""

from __future__ import annotations

from data.config import settings
from data.database import (
    Batch,
    Episode,
    ImportSession,
    JobRun,
    NativeLerobotImportSession,
    NativeLerobotScanSnapshot,
    WorkItem,
)

_IMPORT_SCAN_STATUSES = frozenset(
    {"queued", "running", "retry_pending", "succeeded", "failed", "cancelled"}
)
_IMPORT_SCAN_ERROR_CODES = frozenset(
    {"import_scan_candidate_limit", "import_scan_failed", "worker_lease_expired"}
)


def import_session_public_status(import_session: ImportSession) -> str:
    """Return the user-facing import state without changing worker recovery state."""
    result = import_session.result_json if isinstance(import_session.result_json, dict) else {}
    materialize_status = result.get("materialize_status")
    if import_session.status == "uploading" and materialize_status == "queued":
        return "materialize_queued"
    if import_session.status == "uploading" and materialize_status == "failed":
        return "failed"
    return import_session.status


def import_session_scan_snapshot(import_session: ImportSession) -> dict[str, object] | None:
    """Project the durable scan state without leaking JobRun internals."""
    result = import_session.result_json if isinstance(import_session.result_json, dict) else {}
    job_id = result.get("scan_job_id")
    if (
        not isinstance(job_id, str)
        or len(job_id) != 32
        or any(char not in "0123456789abcdef" for char in job_id)
    ):
        return None
    status = result.get("scan_status")
    if status not in _IMPORT_SCAN_STATUSES:
        status = "queued"
    payload: dict[str, object] = {"job_id": job_id, "status": status}
    count = result.get("scan_candidate_count")
    if (
        isinstance(count, int)
        and not isinstance(count, bool)
        and 0 <= count <= settings.import_scan_candidate_limit
    ):
        payload["candidate_count"] = count
    error_code = result.get("scan_error_code")
    if error_code in _IMPORT_SCAN_ERROR_CODES:
        payload["error_code"] = error_code
    return payload


def batch_snapshot(batch: Batch) -> dict[str, object]:
    return {
        "id": batch.id,
        "workspace_id": batch.workspace_id,
        "task_set_id": batch.task_set_id,
        "name": batch.name,
        "batch_type": batch.batch_type,
        "status": batch.status,
    }


def native_lerobot_scan_snapshot(snapshot: NativeLerobotScanSnapshot) -> dict[str, object]:
    """Project durable scan state without source buckets, keys, or locators."""
    return {
        "id": snapshot.id,
        "workspace_id": snapshot.workspace_id,
        "task_set_id": snapshot.task_set_id,
        "job_id": snapshot.job_id,
        "status": snapshot.status,
        "is_current": snapshot.is_current,
        "candidate_count": snapshot.candidate_count,
        "valid_count": snapshot.valid_count,
        "invalid_count": snapshot.invalid_count,
        "error_code": snapshot.error_code,
    }


def native_lerobot_import_session_snapshot(
    session: NativeLerobotImportSession,
) -> dict[str, object]:
    """Project batch session progress without selection source identity."""
    return {
        "id": session.id,
        "batch_id": session.batch_id,
        "workspace_id": session.workspace_id,
        "task_set_id": session.task_set_id,
        "scan_snapshot_id": session.scan_snapshot_id,
        "status": session.status,
        "selected_count": session.selected_count,
        "registered_count": session.registered_count,
        "skipped_count": session.skipped_count,
        "error_code": session.error_code,
    }


def import_session_snapshot(import_session: ImportSession) -> dict[str, object]:
    result = import_session.result_json if isinstance(import_session.result_json, dict) else {}
    materialize_job_id = result.get("materialize_job_id")
    materialize_status = result.get("materialize_status")
    snapshot: dict[str, object] = {
        "id": import_session.id,
        "batch_id": import_session.batch_id,
        "task_label_id": import_session.task_label_id,
        "default_collector_profile_id": import_session.default_collector_profile_id,
        "default_collection_device_id": import_session.default_collection_device_id,
        "import_type": import_session.import_type,
        "status": import_session_public_status(import_session),
        "original_name": import_session.original_name,
        "source_date_from": import_session.source_date_from.isoformat()
        if import_session.source_date_from
        else None,
        "source_date_to_exclusive": import_session.source_date_to_exclusive.isoformat()
        if import_session.source_date_to_exclusive
        else None,
        "scan": import_session_scan_snapshot(import_session),
    }
    if isinstance(materialize_job_id, str):
        snapshot["materialize_job_id"] = materialize_job_id
    if materialize_status in {"queued", "completed"}:
        snapshot["materialize_status"] = materialize_status
    for key in ("imported_source_count", "failed_source_count"):
        count = _safe_source_count(result.get(key))
        if count is not None:
            snapshot[key] = count
    return snapshot


def _safe_source_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if 0 <= count <= 500 else None


def job_run_snapshot(job: JobRun) -> dict[str, object]:
    return {
        "id": job.id,
        "resource_type": job.resource_type,
        "resource_id": job.resource_id,
        "status": job.status,
        "phase": job.phase,
        "progress_percent": job.progress_percent,
    }


def episode_snapshot(episode: Episode) -> dict[str, object]:
    return {
        "id": episode.id,
        "batch_id": episode.batch_id,
        "kind": episode.kind,
        "quality_status": "passed"
        if episode.quality_status == "profiled"
        else episode.quality_status,
        "workflow_status": episode.workflow_status,
        "annotation_status": episode.annotation_status,
        "review_status": episode.review_status,
    }


def work_item_snapshot(item: WorkItem) -> dict[str, object]:
    return {
        "id": item.id,
        "workspace_id": item.workspace_id,
        "episode_id": item.episode_id,
        "kind": item.kind,
        "status": item.status,
        "assignee_user_id": item.assignee_user_id,
    }
