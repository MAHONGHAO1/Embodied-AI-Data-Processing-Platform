"""Export job core logic (shared between Celery and threads), integrating QRDF to LeRobot Converter."""

import hmac
import re
import uuid
from contextlib import ExitStack
from pathlib import Path

from data.config import settings
from data.database import (
    Dataset,
    DatasetRevision,
    ExportJob,
    JobRun,
    Project,
    QrdfData,
    SessionLocal,
)
from data.integrations.qrdf import export_lerobot
from data.services.cloud_storage import (
    cleanup_export_job_cloud_mirrors,
    cleanup_export_staging,
    cleanup_local_staging,
    materialize_for_processing,
    publish_export_output,
)
from data.services.dataset_revisions import (
    DATASET_EXPORT_JOB_KIND,
    build_revision_manifest,
)
from data.services.export_authorization import (
    ExportScopeError,
    validated_export_authorization_scope,
)
from data.services.workflow_conflict import WorkflowConflict
from data.utils.checksums import tree_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _set_export_progress(job_id: int, progress: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(ExportJob, job_id)
        if job and job.progress < progress:
            job.progress = min(progress, 99)
            db.commit()
    finally:
        db.close()


def run_export(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(ExportJob, job_id)
        if not job:
            return
        try:
            scope = validated_export_authorization_scope(db, job)
        except ExportScopeError:
            _fail_export_scope(job)
            db.commit()
            return
        job.status = "PROCESSING"
        job.progress = 5
        db.commit()
        params = job.params_json or {}
        export_template = params.get("export_template", "generic")
        lerobot_version = params.get("lerobot_version", "v3.0")
        _project_id = int(scope["project_ids"][0])
        _dataset_id = scope["dataset_id"]
        # Resolve workspace_id and dataset version
        _ws_id = None
        _ds_version = None
        if _project_id:
            proj = db.get(Project, _project_id)
            if proj:
                _ws_id = proj.workspace_id
        if _dataset_id:
            ds = db.get(Dataset, _dataset_id)
            if ds:
                _ds_version = ds.version
    finally:
        db.close()

    db = SessionLocal()
    try:
        job = db.get(ExportJob, job_id)
        if not job:
            return
        try:
            scope = validated_export_authorization_scope(db, job)
            qrdf_ids = list(scope["qrdf_ids"])
            qrdf_records = [db.get(QrdfData, qrdf_id) for qrdf_id in qrdf_ids]
            if not qrdf_records or any(
                item is None or not item.storage_path for item in qrdf_records
            ):
                raise ExportScopeError("export QRDF scope is unavailable")
            storage_paths = [item.storage_path for item in qrdf_records if item is not None]
        except ExportScopeError:
            _fail_export_scope(job)
            db.commit()
            return
        job.progress = 10
        db.commit()
    finally:
        db.close()

    if not storage_paths:
        db = SessionLocal()
        try:
            job = db.get(ExportJob, job_id)
            if job:
                job.status = "FAILED"
                job.progress = 100
                job.error_message = f"导出任务 {job_id} 未关联有效 QRDF 数据"
                db.commit()
        finally:
            db.close()
        return

    _set_export_progress(job_id, 15)

    cleanup_export_staging(job_id)

    export_dir = Path(settings.storage_root) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    work_dir = export_dir / f"export_{job_id}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    export_cloud_uri: str | None = None
    try:
        with ExitStack() as stack:
            materialized_paths: list[str] = []
            for sp in storage_paths:
                local = stack.enter_context(materialize_for_processing(sp))
                if local:
                    materialized_paths.append(str(local))
            if not materialized_paths:
                raise FileNotFoundError("无法从 OSS/本地定位任何 QRDF 数据集用于导出")
            export_file = export_lerobot(
                materialized_paths,
                work_dir,
                export_template=export_template,
                lerobot_version=lerobot_version,
                on_progress=lambda p: _set_export_progress(job_id, p),
            )
        export_cloud_uri = publish_export_output(
            export_file,
            export_job_id=job_id,
            workspace_id=_ws_id,
            project_id=_project_id,
            dataset_id=_dataset_id,
            version=_ds_version,
            lerobot_version=lerobot_version,
            template=export_template,
        )
        download_target = export_cloud_uri
        cleanup_local_staging(export_file, work_dir)
        db = SessionLocal()
        try:
            job = db.get(ExportJob, job_id)
            if job:
                job.status = "COMPLETED"
                job.progress = 100
                job.download_url = download_target
                params = dict(job.params_json or {})
                params["storage_uri"] = download_target
                job.params_json = params
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        db = SessionLocal()
        try:
            job = db.get(ExportJob, job_id)
            if job:
                job.status = "FAILED"
                job.progress = 100
                job.error_message = str(exc)
                db.commit()
        finally:
            db.close()
    finally:
        cleanup_export_staging(job_id)
        cleanup_export_job_cloud_mirrors(
            export_uri=export_cloud_uri,
            source_storage_paths=storage_paths,
        )


def _fail_export_scope(job: ExportJob) -> None:
    job.status = "FAILED"
    job.progress = 100
    job.error_message = "export authorization scope is unavailable"


def run_dataset_revision_export(job_id: str) -> dict[str, object]:
    """Materialize one training export from the JobRun's fixed revision manifest.

    Revision membership is never reselected here. The manifest stored when the
    job was enqueued is compared with the immutable revision fact before any
    package is read, then it is the only source list supplied to the converter.
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
        revision = db.get(DatasetRevision, int(job.resource_id))
        if revision is None:
            raise WorkflowConflict("dataset revision does not exist")
        detail = dict(job.detail_json or {})
        manifest = detail.get("manifest")
        manifest_hash = detail.get("manifest_hash")
        if not isinstance(manifest, dict) or not isinstance(manifest_hash, str):
            raise WorkflowConflict("dataset export manifest is unavailable")
        current_manifest = build_revision_manifest(db, revision.id)
        if manifest != current_manifest or manifest_hash != revision.manifest_hash:
            raise WorkflowConflict("dataset export manifest integrity check failed")
        profile = str(detail.get("export_profile") or "")
        if profile != "lerobot":
            raise WorkflowConflict("unsupported dataset export profile")
        items = manifest.get("items")
        if not isinstance(items, list) or not items:
            raise WorkflowConflict("dataset export manifest has no published episodes")
        package_inputs = [_dataset_export_package_input(item) for item in items]
        if any(item is None for item in package_inputs):
            raise WorkflowConflict("dataset export manifest is unavailable")
        package_uris = [item[0] for item in package_inputs if item is not None]
        package_checksums = [item[1] for item in package_inputs if item is not None]
        split_assignments = [item[2] for item in package_inputs if item is not None]
        workspace_id = int(manifest["workspace_id"])
        dataset_id = int(manifest["dataset_id"])
        version = f"r{int(manifest['version'])}"
    finally:
        db.close()

    cleanup_export_staging(job_id)
    export_dir = Path(settings.storage_root) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    work_dir = export_dir / f"export_{job_id}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    export_uri: str | None = None
    try:
        with ExitStack() as stack:
            paths: list[str] = []
            for uri, expected_checksum in zip(package_uris, package_checksums, strict=True):
                local = stack.enter_context(materialize_for_processing(uri))
                if local is None or not local.is_dir() or not local.exists():
                    raise WorkflowConflict("dataset export manifest has no materializable packages")
                try:
                    actual_checksum = tree_sha256(local)
                except (OSError, ValueError) as exc:
                    raise WorkflowConflict(
                        "dataset export package checksum is unavailable"
                    ) from exc
                if not hmac.compare_digest(actual_checksum, expected_checksum):
                    raise WorkflowConflict("dataset export package checksum changed")
                paths.append(str(local))
            export_file = export_lerobot(
                paths,
                work_dir,
                export_template="generic",
                lerobot_version="v3.0",
                split_assignments=split_assignments,
            )
        export_uri = publish_export_output(
            export_file,
            export_job_id=f"revision-{job_id}",
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            version=version,
            lerobot_version="v3.0",
            template=profile,
            export_task_id=job_id,
        )
        cleanup_local_staging(export_file, work_dir)
        return {
            "dataset_revision_id": int(manifest["revision_id"]),
            "manifest_hash": manifest_hash,
            "export_uri": export_uri,
        }
    finally:
        cleanup_export_staging(job_id)
        cleanup_export_job_cloud_mirrors(
            export_uri=export_uri,
            source_storage_paths=[str(uri) for uri in package_uris],
        )


def _dataset_export_package_input(item: object) -> tuple[str, str, str] | None:
    if not isinstance(item, dict):
        return None
    uri = item.get("package_uri")
    checksum = item.get("package_checksum")
    split = item.get("split")
    if (
        not isinstance(uri, str)
        or not uri
        or not isinstance(checksum, str)
        or not _SHA256.fullmatch(checksum)
        or split not in {"train", "val", "test"}
    ):
        return None
    return uri, checksum.lower(), split
