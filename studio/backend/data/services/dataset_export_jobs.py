"""Durable export execution for immutable DatasetRevision manifests."""

from __future__ import annotations

import hmac
import uuid
from pathlib import Path

from data.config import settings
from data.database import DatasetRevision, JobRun, SessionLocal
from data.integrations.qrdf import (
    export_published_sample_revision,
    export_qrdf_revision,
    options_for_manifest,
)
from data.services.cloud_storage import cleanup_export_staging, publish_export_output
from data.services.dataset_revisions import DATASET_EXPORT_JOB_KIND, build_revision_manifest
from data.services.workflow_conflict import WorkflowConflict


def run_dataset_revision_export(job_id: str) -> dict[str, object]:
    """Materialize and export exactly the revision fact frozen in ``JobRun``.

    The worker never accepts a browser path, dataset filter, or item list.  It
    verifies the immutable revision manifest before touching an official QRDF
    artifact, and only publishes a finished ZIP to the export namespace.
    """
    db = SessionLocal()
    try:
        job = db.get(JobRun, job_id)
        if (
            job is None
            or job.kind != DATASET_EXPORT_JOB_KIND
            or job.resource_type != "dataset_revision"
        ):
            raise WorkflowConflict("dataset export job does not exist")
        revision = db.get(DatasetRevision, _revision_id(job.resource_id))
        if revision is None:
            raise WorkflowConflict("dataset revision does not exist")
        detail = dict(job.detail_json or {})
        manifest = detail.get("manifest")
        manifest_hash = detail.get("manifest_hash")
        if not isinstance(manifest, dict) or not isinstance(manifest_hash, str):
            raise WorkflowConflict("dataset export manifest is unavailable")
        current_manifest = build_revision_manifest(db, revision.id)
        if manifest != current_manifest or not hmac.compare_digest(
            manifest_hash, revision.manifest_hash
        ):
            raise WorkflowConflict("dataset export manifest integrity check failed")
        profile = str(detail.get("export_profile") or "")
        if profile not in {"lerobot", "qrdf"}:
            raise WorkflowConflict("unsupported dataset export profile")
        export_options = detail.get("export_options")
        if export_options is not None and not isinstance(export_options, dict):
            raise WorkflowConflict("dataset export profile is invalid")
        options = None
        if profile == "lerobot":
            try:
                options = options_for_manifest(manifest, export_options)
            except ValueError as exc:
                raise WorkflowConflict(str(exc)) from exc
        elif export_options:
            raise WorkflowConflict("QRDF export profile does not accept options")
        workspace_id = _positive_int(manifest.get("workspace_id"), "workspace")
        dataset_id = _positive_int(manifest.get("dataset_id"), "dataset")
        version = f"r{_positive_int(manifest.get('version'), 'dataset revision version')}"
    finally:
        db.close()

    cleanup_export_staging(job_id)
    export_dir = Path(settings.storage_root) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    work_dir = export_dir / f"export_{job_id}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=False)
    try:
        if profile == "lerobot":
            result = export_published_sample_revision(manifest, work_dir, options=options)
            lerobot_version = result.report.lerobot_version
            result_payload = {
                "lerobot_version": lerobot_version,
                "item_count": len(result.report.items),
                "input_manifest_sha256": result.report.input_manifest_sha256,
                "artifact_manifest_sha256": result.report.artifact_manifest_sha256,
            }
        else:
            result = export_qrdf_revision(manifest, work_dir)
            lerobot_version = None
            result_payload = {
                "item_count": result.item_count,
                "dataset_manifest_sha256": result.dataset_manifest_sha256,
                "source_manifest_sha256": result.source_manifest_sha256,
            }
        export_uri = publish_export_output(
            result.archive_path,
            export_job_id=f"revision-{job_id}",
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version=version,
            lerobot_version=lerobot_version,
            template=profile,
            export_task_id=job_id,
        )
        return {
            "dataset_revision_id": int(manifest["revision_id"]),
            "manifest_hash": manifest_hash,
            "export_uri": export_uri,
            "export_profile": profile,
            **result_payload,
        }
    finally:
        # This only matches this job's temporary exports/ directory.  Official
        # source packages stay immutable and are released by their materialize
        # context manager, not by recursive cloud mirror cleanup.
        cleanup_export_staging(job_id)


def _revision_id(value: object) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise WorkflowConflict("dataset revision id is invalid") from exc
    if parsed <= 0:
        raise WorkflowConflict("dataset revision id is invalid")
    return parsed


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise WorkflowConflict(f"{label} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowConflict(f"{label} is invalid") from exc
    if parsed <= 0:
        raise WorkflowConflict(f"{label} is invalid")
    return parsed
